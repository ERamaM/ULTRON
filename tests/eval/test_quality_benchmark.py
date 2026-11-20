"""Labelled quality benchmark: corpus contract, metrics, gates and the committed baseline."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
from pathlib import Path

import pytest
import quality_benchmark as qb

CORPUS = qb.CORPUS_DIR
CASE_COUNT = 210


def _case(case_id, relevant, status, rank=None, **slices):
    fields = {"language": "en", "remote_mode": "eu", "vendor": "greenhouse", "role_track": "core", **slices}
    return {
        "id": case_id,
        **fields,
        "truth": {"relevant": relevant},
        "prediction": {"status": status, "rank": rank},
        "first_loss_stage": None,
    }


@pytest.fixture(scope="module")
def report():
    return qb.run_benchmark()


# --- corpus ---------------------------------------------------------------------------------------


def test_corpus_is_valid_synthetic_and_complete():
    meta, records = qb.load_corpus()
    assert len(records) == CASE_COUNT == meta["minimum_cases"]
    assert len({case["id"] for case, _ in records}) == CASE_COUNT
    for case, fixture in records:
        assert fixture["synthetic"] is True
        assert fixture["apply_url"] == f"https://jobs.example.test/{case['id']}"
    text = "".join(path.read_text(encoding="utf-8") for path in sorted(CORPUS.rglob("*.*")) if path.is_file())
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w+", text), "the corpus must not contain email addresses"
    hosts = set(re.findall(r"https?://([^/\"\s]+)", text))
    assert hosts <= {"jobs.example.test"}, hosts


def test_cases_cover_both_languages_and_every_region():
    _meta, records = qb.load_corpus()
    assert {case["language"] for case, _ in records} == {"en", "es"}
    assert {case["remote_mode"] for case, _ in records} == qb.ENUMS["remote_mode"]


def _copy_corpus(tmp_path: Path) -> Path:
    root = tmp_path / "benchmark"
    shutil.copytree(CORPUS, root, ignore=shutil.ignore_patterns("baseline.json", "README.md"))
    return root


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: _edit(r / "cases" / "en-core-001.yaml", "profile: ml-engineering-remote-eu", "profile: nobody"),
         "profile='nobody'"),
        (lambda r: _edit(r / "cases" / "da-dup-001.yaml", "relevant: true", "relevant: false"), "duplicate_group"),
        (lambda r: _edit(r / "cases" / "da-dup-001.yaml", "duplicate_group: da-dup-a", "duplicate_group: ../x"),
         "duplicate_group must be"),
        (lambda r: _edit(r / "fixtures" / "da-dup-001.json", '"employer": "analytics-co"', '"employer": "A Co"'),
         "employer must be"),
    ],
)
def test_profiles_groups_and_employers_are_validated(tmp_path, mutate, message):
    root = _copy_corpus(tmp_path)
    mutate(root)
    with pytest.raises(qb.CorpusError, match=re.escape(message)):
        qb.load_corpus(root)


def test_every_profile_has_a_file_and_enough_labelled_cases():
    _meta, records = qb.load_corpus()
    for slug in qb.BENCHMARK_PROFILES:
        assert qb.load_profile(slug)["slug"] in {slug, "ai-ml-remote-eu"}
        labelled = [case for case, _ in records if case["profile"] == slug and case["labels"]["relevant"] is not None]
        assert len(labelled) >= qb.MIN_SLICE_SAMPLE, slug


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: (r / "cases" / "en-core-001.yaml").write_text("labels: [", encoding="utf-8"), "unreadable"),
        (lambda r: (r / "fixtures" / "en-core-001.json").unlink(), "unreadable"),
        (
            lambda r: (r / "cases" / "en-core-001.yaml").write_text(
                (r / "cases" / "en-core-001.yaml").read_text(encoding="utf-8").replace("split: dev", "split: test"),
                encoding="utf-8",
            ),
            "split",
        ),
        (
            lambda r: (r / "fixtures" / "en-core-001.json").write_text(
                (r / "fixtures" / "en-core-001.json").read_text(encoding="utf-8").replace(
                    '"synthetic": true', '"synthetic": false'
                ),
                encoding="utf-8",
            ),
            "synthetic",
        ),
        (lambda r: (r / "cases" / "es-negative-120.yaml").unlink(), "minimum_cases"),
    ],
)
def test_invalid_corpus_is_rejected(tmp_path, mutate, message):
    root = _copy_corpus(tmp_path)
    mutate(root)
    with pytest.raises(qb.CorpusError, match=message):
        qb.load_corpus(root)


def test_validate_exit_codes(tmp_path, monkeypatch, capsys):
    assert qb.main(["validate"]) == 0
    assert json.loads(capsys.readouterr().out) == {"valid": True, "cases": CASE_COUNT}
    root = _copy_corpus(tmp_path)
    (root / "corpus.yaml").write_text("schema_version: 9\n", encoding="utf-8")
    monkeypatch.setattr(qb, "CORPUS_DIR", root)
    assert qb.main(["validate"]) == 2
    assert json.loads(capsys.readouterr().out)["valid"] is False


# --- metrics on hand-computed inputs --------------------------------------------------------------


def test_classification_metrics_by_hand():
    results = [
        _case("a", True, "keep"),
        _case("b", True, "discard"),
        _case("c", True, "unsure"),
        _case("d", False, "keep"),
        _case("e", False, "discard"),
        _case("f", None, "keep"),
    ]
    metrics = qb.classification_metrics(results)
    assert metrics["confusion_matrix"] == {"tp": 1, "fp": 1, "tn": 1, "fn": 2}
    assert metrics["sample_size"] == 5
    assert metrics["precision"] == 0.5
    assert metrics["recall"] == pytest.approx(1 / 3)
    assert metrics["f1"] == pytest.approx(0.4)
    assert metrics["false_negative_rate"] == pytest.approx(2 / 3)
    assert metrics["false_positive_rate"] == 0.5
    assert metrics["abstention_rate"] == pytest.approx(1 / 6)


def test_ranking_metrics_by_hand():
    results = [
        _case("a", False, "keep", rank=1),
        _case("b", True, "keep", rank=2),
        _case("c", True, "unsure", rank=3),
        _case("d", True, "discard"),
    ]
    metrics = qb.ranking_metrics(results)
    assert metrics["shortlist_size"] == 3
    assert metrics["relevant_total"] == 3
    assert metrics["precision_at_5"] == pytest.approx(2 / 5)
    assert metrics["precision_at_10"] == pytest.approx(2 / 10)
    assert metrics["recall_at_10"] == pytest.approx(2 / 3)
    assert metrics["mrr"] == 0.5
    ideal = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert metrics["ndcg_at_10"] == pytest.approx((1 / math.log2(3) + 1 / math.log2(4)) / ideal)


def _grouped(case_id, relevant, status, group, rank=None):
    return {**_case(case_id, relevant, status, rank=rank), "duplicate_group": group}


def test_a_duplicate_group_is_scored_as_one_unit():
    results = [
        _grouped("a1", True, "keep", "g", rank=1),
        _grouped("a2", True, "keep", "g", rank=2),
        _grouped("a3", True, "discard", "g"),
        _case("b", True, "discard"),
        _case("c", False, "keep", rank=3),
    ]
    metrics = qb.classification_metrics(results)
    assert metrics["confusion_matrix"] == {"tp": 1, "fp": 1, "tn": 0, "fn": 1}
    ranking = qb.ranking_metrics(results)
    assert ranking["duplicate_rows"] == 1 and ranking["relevant_total"] == 2
    assert ranking["precision_at_5"] == pytest.approx(1 / 5)  # the second row of group g earns no gain


def test_merging_a_duplicate_into_a_kept_sibling_is_not_a_new_false_negative():
    base = qb.build_report([_grouped("a1", True, "keep", "g", 1), _grouped("a2", True, "keep", "g", 2)],
                           corpus_version="t", agent_queue=0)
    merged = qb.build_report([_grouped("a1", True, "keep", "g", 1), _grouped("a2", True, "discard", "g")],
                             corpus_version="t", agent_queue=0)
    assert qb.compare_reports(base, merged).passed
    assert merged["metrics"]["duplicate_rows"] == 0 and base["metrics"]["duplicate_rows"] == 1
    lost = qb.build_report([_grouped("a1", True, "discard", "g"), _grouped("a2", True, "unsure", "g", 1)],
                           corpus_version="t", agent_queue=0)
    assert "new false negative: group:g" in qb.compare_reports(base, lost).regressions


def test_slices_flag_small_samples():
    results = [_case(str(i), i % 2 == 0, "keep", language="en") for i in range(10)]
    results.append(_case("x", True, "keep", language="es"))
    slices = qb.slice_metrics(results)
    assert slices["language"]["en"]["status"] == "measured"
    assert slices["language"]["es"]["status"] == "insufficient_sample"
    assert set(slices) == {"language", "region", "vendor", "role_track", "profile"}


# --- gates ----------------------------------------------------------------------------------------


def _synthetic_report(statuses):
    results = [_case(f"c{i:02d}", relevant, status) for i, (relevant, status) in enumerate(statuses)]
    return qb.build_report(results, corpus_version="test", agent_queue=0)


BASE = [(True, "keep")] * 8 + [(True, "discard")] * 2 + [(False, "discard")] * 10


def test_identical_reports_pass():
    result = qb.compare_reports(_synthetic_report(BASE), _synthetic_report(BASE))
    assert result.passed and not result.regressions


def test_new_false_negative_is_a_regression_unless_justified():
    worse = list(BASE)
    worse[0] = (True, "discard")
    result = qb.compare_reports(_synthetic_report(BASE), _synthetic_report(worse))
    assert not result.passed
    assert "new false negative: c00" in result.regressions
    assert any(r.startswith("global recall declined") for r in result.regressions)
    assert any(r.startswith("slice language=en recall declined") for r in result.regressions)


def test_small_global_drop_within_tolerance_passes():
    base = [(True, "keep")] * 100 + [(False, "discard")] * 100
    candidate = list(base)
    candidate[150] = (False, "keep")  # precision 1.0 -> 0.990..., a drop below one point
    result = qb.compare_reports(_synthetic_report(base), _synthetic_report(candidate))
    assert result.passed, result.regressions


def test_a_global_decline_of_exactly_one_point_passes():
    base = [(True, "keep")] * 99 + [(False, "discard")] * 10
    candidate = list(base)
    candidate[-1] = (False, "keep")  # precision 1.0 -> 99/100 = 0.99; 1.0 - 0.99 > 0.01 in floats
    result = qb.compare_reports(_synthetic_report(base), _synthetic_report(candidate))
    assert result.passed, result.regressions
    candidate[-2] = (False, "keep")  # 99/101: a decline above one point still fails
    assert not qb.compare_reports(_synthetic_report(base), _synthetic_report(candidate)).passed


def test_a_slice_decline_of_exactly_five_points_passes():
    def cases(extra_false_keep: bool):
        items = [_case(f"en{i}", True, "keep", language="en") for i in range(19)]
        items.append(_case("en-neg", False, "keep" if extra_false_keep else "discard", language="en"))
        items += [_case(f"es{i}", True, "keep", language="es") for i in range(1000)]
        return qb.build_report(items, corpus_version="t", agent_queue=0)

    base, candidate = cases(False), cases(True)  # slice en precision 1.0 -> 19/20 = 0.95
    assert candidate["metrics"]["slices"]["language"]["en"]["precision"] == 0.95
    result = qb.compare_reports(base, candidate)
    assert result.passed, result.regressions


def test_slice_gate_needs_ten_cases():
    base = qb.build_report(
        [_case(f"a{i}", True, "keep", vendor="lever") for i in range(4)]
        + [_case(f"b{i}", False, "discard", vendor="ashby") for i in range(40)],
        corpus_version="t", agent_queue=0,
    )
    result = qb.compare_reports(base, copy.deepcopy(base))
    assert "vendor=lever sample=4" in result.insufficient_samples


def test_missing_slice_and_case_are_regressions():
    base = _synthetic_report(BASE)
    candidate = copy.deepcopy(base)
    del candidate["metrics"]["slices"]["vendor"]["greenhouse"]
    candidate["cases"] = candidate["cases"][1:]
    result = qb.compare_reports(base, candidate)
    assert "slice vendor=greenhouse is missing" in result.regressions
    assert "case c00 is missing" in result.regressions


def test_compare_rejects_reports_of_another_schema():
    with pytest.raises(qb.CorpusError):
        qb.compare_reports({"schema_version": 99}, _synthetic_report(BASE))


def test_compare_exit_codes(tmp_path, monkeypatch, reports_dir, capsys):
    base, worse = _synthetic_report(BASE), _synthetic_report([(True, "discard")] * 10 + [(False, "discard")] * 10)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(qb.dump_report(base), encoding="utf-8")
    monkeypatch.setattr(qb, "BASELINE_PATH", baseline)
    reports_dir.mkdir()
    (reports_dir / "same.json").write_text(qb.dump_report(base), encoding="utf-8")
    (reports_dir / "worse.json").write_text(qb.dump_report(worse), encoding="utf-8")
    (reports_dir / "broken.json").write_text("{", encoding="utf-8")
    assert qb.main(["compare", "same.json"]) == 0
    assert qb.main(["compare", "worse.json"]) == 1
    assert qb.main(["compare", "broken.json"]) == 2
    assert qb.main(["compare", "missing.json"]) == 2
    capsys.readouterr()


@pytest.mark.parametrize("name", ["../same.json", "sub/same.json", "..", "/abs/same.json", "C:\\x\\same.json"])
def test_report_names_must_be_plain_file_names(name, reports_dir, capsys):
    reports_dir.mkdir()
    (reports_dir / "same.json").write_text("{}", encoding="utf-8")
    assert qb.main(["compare", name]) == 2
    assert qb.main(["run", "--output", name]) == 2
    errors = [json.loads(line)["error"] for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert len(errors) == 2 and all("plain file name" in error for error in errors)


# --- replay over the real corpus ------------------------------------------------------------------


def test_replay_is_deterministic_and_leaves_the_home_untouched(report, tmp_path):
    home = os.environ["ULTRON_HOME"]
    assert qb.run_benchmark() == report
    assert os.environ["ULTRON_HOME"] == home
    assert not Path(home).exists() or not any((Path(home) / "runs").glob("*"))


def test_tools_never_write_the_labels():
    import measure_quality

    labelled = [*sorted(CORPUS.rglob("*.*")), measure_quality.LABELS_PATH, measure_quality.CORPUS_PATH]
    before = {path: path.read_bytes() for path in labelled if path.is_file()}
    qb.run_benchmark()
    measure_quality.measure()
    assert {path: path.read_bytes() for path in labelled if path.is_file()} == before
    assert sorted(CORPUS.rglob("*.*")) == sorted(p for p in before if CORPUS in p.parents)


def test_every_relevant_miss_has_a_losing_stage(report):
    units = {id(unit) for unit in qb.scoring_units(report["cases"])}
    for case in report["cases"]:
        missed = case["truth"]["relevant"] is True and case["prediction"]["status"] != "keep" and id(case) in units
        assert (case["first_loss_stage"] is not None) == missed, case["id"]
    losses = report["metrics"]["first_loss_counts"]
    assert sum(losses.values()) == report["metrics"]["classification"]["confusion_matrix"]["fn"]


def test_committed_baseline_passes_the_gates(report):
    baseline = json.loads(qb.BASELINE_PATH.read_text(encoding="utf-8"))
    result = qb.compare_reports(baseline, report)
    assert result.passed, result.regressions


def test_run_writes_json_and_markdown(reports_dir, capsys):
    assert qb.main(["run", "--output", "candidate.json"]) == 0
    assert json.loads(capsys.readouterr().out)["report"] == str(reports_dir / "candidate.json")
    data = json.loads((reports_dir / "candidate.json").read_text(encoding="utf-8"))
    assert data["case_count"] == CASE_COUNT
    markdown = (reports_dir / "candidate.md").read_text(encoding="utf-8")
    assert "| recall |" in markdown and "First losing stage" in markdown
    assert qb.main(["compare", "candidate.json"]) == 0
    assert qb.main(["compare"]) == 0
    capsys.readouterr()


def test_run_refuses_a_symlinked_markdown_target_before_writing(tmp_path, reports_dir, capsys):
    outside = tmp_path / "outside.md"
    outside.write_text("keep me", encoding="utf-8")
    reports_dir.mkdir()
    try:
        (reports_dir / "candidate.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available on this system")
    assert qb.main(["run", "--output", "candidate.json"]) == 2
    assert "not a regular file" in json.loads(capsys.readouterr().out)["error"]
    assert outside.read_text(encoding="utf-8") == "keep me"
    assert not (reports_dir / "candidate.json").exists()


def test_update_baseline_writes_only_the_committed_file(tmp_path, monkeypatch, reports_dir, capsys):
    target = tmp_path / "baseline.json"
    monkeypatch.setattr(qb, "BASELINE_PATH", target)
    assert qb.main(["run", "--update-baseline"]) == 0
    assert json.loads(target.read_text(encoding="utf-8"))["case_count"] == CASE_COUNT
    assert not reports_dir.exists()
    assert capsys.readouterr().out == ""


def test_run_output_must_be_json_so_the_summary_cannot_overwrite_it():
    assert qb.main(["run", "--output", "candidate.md"]) == 2

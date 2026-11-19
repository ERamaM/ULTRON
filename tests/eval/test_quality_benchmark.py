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
    assert len(records) == 120 == meta["minimum_cases"]
    assert len({case["id"] for case, _ in records}) == 120
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
    assert json.loads(capsys.readouterr().out) == {"valid": True, "cases": 120}
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


def test_slices_flag_small_samples():
    results = [_case(str(i), i % 2 == 0, "keep", language="en") for i in range(10)]
    results.append(_case("x", True, "keep", language="es"))
    slices = qb.slice_metrics(results)
    assert slices["language"]["en"]["status"] == "measured"
    assert slices["language"]["es"]["status"] == "insufficient_sample"
    assert set(slices) == {"language", "region", "vendor", "role_track"}


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


def test_compare_exit_codes(tmp_path, capsys):
    base, worse = _synthetic_report(BASE), _synthetic_report([(True, "discard")] * 10 + [(False, "discard")] * 10)
    paths = {}
    for name, data in (("base", base), ("worse", worse)):
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(qb.dump_report(data), encoding="utf-8")
    assert qb.main(["compare", str(paths["base"]), str(paths["base"])]) == 0
    assert qb.main(["compare", str(paths["base"]), str(paths["worse"])]) == 1
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    assert qb.main(["compare", str(tmp_path / "broken.json"), str(paths["base"])]) == 2
    capsys.readouterr()


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
    for case in report["cases"]:
        missed = case["truth"]["relevant"] is True and case["prediction"]["status"] != "keep"
        assert (case["first_loss_stage"] is not None) == missed, case["id"]
    losses = report["metrics"]["first_loss_counts"]
    assert sum(losses.values()) == report["metrics"]["classification"]["confusion_matrix"]["fn"]


def test_committed_baseline_passes_the_gates(report):
    baseline = json.loads(qb.BASELINE_PATH.read_text(encoding="utf-8"))
    result = qb.compare_reports(baseline, report)
    assert result.passed, result.regressions


def test_run_writes_json_and_markdown(tmp_path, capsys):
    output = tmp_path / "candidate.json"
    assert qb.main(["run", "--output", str(output)]) == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["case_count"] == 120
    markdown = output.with_suffix(".md").read_text(encoding="utf-8")
    assert "| recall |" in markdown and "First losing stage" in markdown
    assert qb.main(["compare", str(qb.BASELINE_PATH)]) == 0
    capsys.readouterr()

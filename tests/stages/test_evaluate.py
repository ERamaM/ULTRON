from __future__ import annotations

import json

from stage_helpers import make_run

from ultron.cli import main
from ultron.storage import runs

AMBIG = ["en-ambiguous-shape-01", "en-ambiguous-shape-06", "q-seniority-ambiguous"]


def _ev(**kw):
    base = {"keep": True, "verdict": "keep", "is_senior": True, "is_allowed_family": True, "is_full_remote": True}
    return {**base, **kw}


def _write(tmp_path, rows):
    p = tmp_path / "ev.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(p)


def _filtered(capsys):
    run_id = make_run(AMBIG)
    assert main(["filter", "--run", run_id]) == 0
    capsys.readouterr()
    return run_id


def test_record_requires_filter(tmp_path, capsys):
    run_id = make_run(AMBIG)
    p = _write(tmp_path, [{"identity_key": "id:x", "evaluation": _ev()}])
    assert main(["evaluate", "record", "--run", run_id, "--from", p]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "stage_order"


def test_record_rejects_unknown_identity(tmp_path, capsys):
    run_id = _filtered(capsys)
    p = _write(tmp_path, [{"identity_key": "id:nope", "evaluation": _ev()}])
    assert main(["evaluate", "record", "--run", run_id, "--from", p]) != 0
    assert "error" in json.loads(capsys.readouterr().out)
    assert not (runs.run_dir(run_id) / "evaluations.jsonl").exists()


def test_record_rejects_invalid_verdict(tmp_path, capsys):
    run_id = _filtered(capsys)
    p = _write(tmp_path, [
        {"identity_key": f"id:{AMBIG[0]}", "evaluation": _ev(verdict="maybe")},
        {"identity_key": f"id:{AMBIG[1]}", "evaluation": _ev()},
    ])
    assert main(["evaluate", "record", "--run", run_id, "--from", p]) != 0
    out = json.loads(capsys.readouterr().out)
    assert out["error"] and out["line"] == 1
    assert not (runs.run_dir(run_id) / "evaluations.jsonl").exists()


def test_record_all_or_nothing_on_late_bad_line(tmp_path, capsys):
    run_id = _filtered(capsys)
    p = _write(tmp_path, [
        {"identity_key": f"id:{AMBIG[0]}", "evaluation": _ev()},
        {"identity_key": f"id:{AMBIG[1]}", "evaluation": _ev(verdict="maybe")},
    ])
    assert main(["evaluate", "record", "--run", run_id, "--from", p]) != 0
    assert json.loads(capsys.readouterr().out)["line"] == 2
    assert not (runs.run_dir(run_id) / "evaluations.jsonl").exists()


def test_record_last_wins_and_forces_llm(tmp_path, capsys):
    run_id = _filtered(capsys)
    key = f"id:{AMBIG[0]}"
    p = _write(tmp_path, [
        {"identity_key": key, "evaluation": _ev(fit_signals=["first"])},
        {"identity_key": key, "evaluation": _ev(fit_signals=["second"], evaluation_source="heuristic_fallback")},
    ])
    assert main(["evaluate", "record", "--run", run_id, "--from", p]) == 0
    capsys.readouterr()
    rows = runs.read_jsonl(runs.run_dir(run_id) / "evaluations.jsonl")
    assert len(rows) == 1
    assert rows[0]["evaluation"]["fit_signals"] == ["second"]
    assert rows[0]["evaluation"]["evaluation_source"] == "agent"
    assert "evaluate" in runs.load_run(run_id)["stages_done"]
    # merge with existing file
    p2 = _write(tmp_path, [{"identity_key": f"id:{AMBIG[1]}", "evaluation": _ev()}])
    assert main(["evaluate", "record", "--run", run_id, "--from", p2]) == 0
    assert len(runs.read_jsonl(runs.run_dir(run_id) / "evaluations.jsonl")) == 2


def test_record_accepts_line_separator_inside_a_value(tmp_path, capsys):
    run_id = _filtered(capsys)
    p = tmp_path / "ev.jsonl"
    row = {"identity_key": f"id:{AMBIG[0]}", "evaluation": _ev(fit_signals=["Remote\u2028LATAM"])}
    p.write_bytes((json.dumps(row, ensure_ascii=False) + "\r\n").encode("utf-8"))
    assert main(["evaluate", "record", "--run", run_id, "--from", str(p)]) == 0, capsys.readouterr().out
    rows = runs.read_jsonl(runs.run_dir(run_id) / "evaluations.jsonl")
    assert rows[0]["evaluation"]["fit_signals"] == ["Remote\u2028LATAM"]


def test_pending_batch_is_compact(tmp_path, capsys):
    run_id = _filtered(capsys)
    d = runs.run_dir(run_id)
    rows = runs.read_jsonl(d / "ambiguous.jsonl")
    assert len(rows) >= 2
    for row in rows:
        row["job"]["body_text"] = "x" * 5000
    runs.write_jsonl(d / "ambiguous.jsonl", rows)
    assert main(["evaluate", "pending", "--run", run_id, "--limit", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["items"]) == 2
    assert all(len(i["body_excerpt"]) <= 1500 for i in out["items"])
    assert all("geo_excerpt" in i for i in out["items"])
    assert out["items"][0]["identity_key"] == rows[0]["identity_key"]
    # recorded ones drop out of pending
    p = _write(tmp_path, [{"identity_key": rows[0]["identity_key"], "evaluation": _ev()}])
    assert main(["evaluate", "record", "--run", run_id, "--from", p]) == 0
    capsys.readouterr()
    assert main(["evaluate", "pending", "--run", run_id]) == 0
    items = json.loads(capsys.readouterr().out)["items"]
    assert rows[0]["identity_key"] not in {i["identity_key"] for i in items}


def test_pending_returns_unsure_first_then_keeps_in_file_order(tmp_path, capsys):
    """R15: heuristic ``unsure`` rows come first, then the rest; file order kept within each group."""
    run_id = _filtered(capsys)
    d = runs.run_dir(run_id)
    template = runs.read_jsonl(d / "ambiguous.jsonl")[0]
    verdicts = ["keep", "unsure", "keep", "unsure", "keep"]
    rows = []
    for i, verdict in enumerate(verdicts):
        row = json.loads(json.dumps(template))
        row["identity_key"] = f"k{i}"
        row["heuristic"]["verdict"] = verdict
        rows.append(row)
    runs.write_jsonl(d / "ambiguous.jsonl", rows)

    assert main(["evaluate", "pending", "--run", run_id, "--limit", "10"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [i["identity_key"] for i in out["items"]] == ["k1", "k3", "k0", "k2", "k4"]

    p = _write(tmp_path, [{"identity_key": "k1", "evaluation": _ev()}])
    assert main(["evaluate", "record", "--run", run_id, "--from", p]) == 0
    capsys.readouterr()
    assert main(["evaluate", "pending", "--run", run_id, "--limit", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pending"] == 4
    assert [i["identity_key"] for i in out["items"]] == ["k3", "k0"]


def test_pending_and_finalize_include_kept_outside_ambiguity_queue(tmp_path, capsys):
    kept_key = "id:en-positive-shape-01"
    unsure_key = "id:en-ambiguous-shape-01"
    run_id = make_run([kept_key.removeprefix("id:"), unsure_key.removeprefix("id:")])
    run = runs.load_run(run_id)
    run["settings"]["llm_ambiguity_threshold"] = 100
    runs._write_run(run_id, run)

    assert main(["filter", "--run", run_id]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["kept"], result["ambiguous"]) == (1, 1)

    assert main(["evaluate", "pending", "--run", run_id]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [item["identity_key"] for item in result["items"]] == [unsure_key, kept_key]

    source = _write(tmp_path, [{"identity_key": unsure_key, "evaluation": _ev()}])
    assert main(["evaluate", "record", "--run", run_id, "--from", source]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (result["pending"], result["pending_ambiguous"]) == (1, 0)
    assert main(["finalize", "--run", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["heuristic_fallback"] == 1


def test_concurrent_disjoint_evaluations_preserve_every_acknowledged_verdict(tmp_path, capsys, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from threading import Event

    from ultron.stages import evaluate

    run_id = _filtered(capsys)
    keys = [f"id:{name}" for name in AMBIG[:2]]
    paths = [Path(_write(tmp_path, [{"identity_key": keys[0], "evaluation": _ev()}]))]
    paths[0] = paths[0].rename(tmp_path / "first.jsonl")
    paths.append(Path(_write(tmp_path, [{"identity_key": keys[1], "evaluation": _ev()}])))
    first_read, second_read, release = Event(), Event(), Event()
    real_read = runs.read_jsonl

    def read(path):
        rows = real_read(path)
        if Path(path).name == "evaluations.jsonl":
            if not first_read.is_set():
                first_read.set()
                assert release.wait(timeout=5)
            else:
                second_read.set()
        return rows

    monkeypatch.setattr(runs, "read_jsonl", read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(evaluate.record_evaluations, run_id, paths[0])
        assert first_read.wait(timeout=5)
        second = executor.submit(evaluate.record_evaluations, run_id, paths[1])
        try:
            if second_read.wait(timeout=0.5):
                assert second.result(timeout=5)["recorded"] == 1
        finally:
            release.set()
        assert first.result(timeout=5)["recorded"] == second.result(timeout=5)["recorded"] == 1
    final = real_read(runs.run_dir(run_id) / "evaluations.jsonl")
    assert {row["identity_key"] for row in final} == set(keys)

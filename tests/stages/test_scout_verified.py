"""Specification 30: verified finds (postings the agent opened and judged) lead a scout shortlist as judged."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from test_scout_flow import (
    AGGREGATOR,
    DEAD_AGGREGATOR,
    EMPLOYER,
    POSTING,
    _cli,
    _handler,
    _new_scout,
    _verdict,
    _write,
)

from ultron import seeds
from ultron.cli import main
from ultron.core.ranker.final_rows import dedupe_final_rows, rank_scout_rows
from ultron.infra.net import HttpError
from ultron.profiles import store as profiles
from ultron.stages import http_factory, scout, scout_verified, verified_check
from ultron.storage import ledger, runs, scout_memory

OCCUPOP_REF = "staff-nurse-ab12c"
GONE = "https://careers.example.org/jobs/closed-role"
KEEP = {"url": POSTING, "verdict": "keep", "title": "Senior Data Scientist", "company": "Acme",
        "location": "Remote - Spain", "priority": 3, "remote_policy": "remote_spain",
        "eligibility_evidence": "Remote within Spain", "fit_signals": ["Python", "ML"]}
UNSURE = {"url": EMPLOYER, "verdict": "unsure", "title": "Senior ML Engineer", "company": "Example Org", "priority": 1}
DISCARD = {"url": GONE, "verdict": "discard", "discard_reason": "on-site in another country"}


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: empty)
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(_handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    profiles.install_example()


def _add(capsys, run_id: str, rows: list[dict], name: str = "agent_finds.jsonl") -> dict:
    return _cli(capsys, "candidates", "add", "--run", run_id, "--from", _write(run_id, name, rows))


def _rows(out: dict) -> list[dict]:
    return json.loads(Path(out["outputs"]["results_json"]).read_text(encoding="utf-8"))["rows"]


def _pipeline(capsys, run_id: str) -> tuple[dict, dict, dict]:
    fetched = _cli(capsys, "fetch", "--run", run_id)
    routed = _cli(capsys, "filter", "--run", run_id)
    pending = _cli(capsys, "evaluate", "pending", "--run", run_id, "--limit", "50")
    return fetched, routed, pending


def test_verified_finds_are_stored_as_judged(capsys):
    run_id = _new_scout(capsys)["run_id"]
    out = _add(capsys, run_id, [KEEP, UNSURE, DISCARD, {"url": "https://boards.greenhouse.io/acme/jobs/4012348"}])
    assert out["verified"] == {"keep": 1, "unsure": 1, "discard": 1}
    assert [row["outcome"] for row in out["rows"]][:3] == ["verified"] * 3
    assert all(row["judged"] for row in out["rows"][:3]) and "judged" not in out["rows"][3]
    stored = scout_verified.load_verified(runs.run_dir(run_id))
    keep = stored["greenhouse:acme:4012345"]
    assert keep["priority"] == 3 and keep["eligibility"] == "high" and keep["remote_policy"] == "remote_spain"
    unsure = next(row for row in stored.values() if row["verdict"] == "unsure")
    assert unsure["eligibility"] == "unknown" and unsure["remote_policy"] == "unknown"
    candidates = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
    assert sum(1 for c in candidates if c["search_pass"] == "judged") == 3 and len(candidates) == 4


def test_invalid_verified_rows_name_the_field(capsys):
    run_id = _new_scout(capsys)["run_id"]
    bad = [
        {"url": POSTING, "verdict": "keep", "company": "Acme"},
        {"url": POSTING, "verdict": "unsure", "title": "Role"},
        {**KEEP, "priority": 7},
        {**KEEP, "priority": True},
        {**KEEP, "verdict": "maybe"},
        {**KEEP, "remote_policy": "sometimes"},
        {**KEEP, "eligibility": "certain"},
        {**KEEP, "fit_signals": "Python"},
        {**KEEP, "remote_policy": ["remote_eu"]},
        {**KEEP, "eligibility": {"level": "high"}},
    ]
    out = _add(capsys, run_id, bad)
    reasons = [row["reason"] for row in out["rows"]]
    assert all(row["outcome"] == "invalid" for row in out["rows"])
    for field, row in zip(("title", "company", "priority", "priority", "verdict", "remote_policy", "eligibility",
                           "fit_signals", "remote_policy", "eligibility"), reasons, strict=True):
        assert row.startswith(field)
    assert out["added"] == 0 and out["verified"] == {"keep": 0, "unsure": 0, "discard": 0}


def test_verified_judgment_is_cleaned_and_bounded():
    judged = scout_verified.judgment({"verdict": "keep", "title": "  Role\n" + "x" * 900, "company": "Acme",
                                      "fit_signals": [f"s{i}" for i in range(12)] + [""],
                                      "eligibility_evidence": "e" * 400, "discard_reason": "ignored"})
    assert len(judged["title"]) == scout_verified.TEXT_LIMIT and judged["title"].startswith("Role")
    assert len(judged["fit_signals"]) == scout_verified.MAX_FIT_SIGNALS
    assert len(judged["eligibility_evidence"]) == scout_verified.EVIDENCE_LIMIT
    assert judged["priority"] == 2 and judged["discard_reason"] == ""
    assert scout_verified.judgment({"verdict": "discard"})["eligibility"] == "unknown"
    assert not scout_verified.is_verified([]) and scout_verified.is_verified({"verdict": None})


def test_a_later_judgment_replaces_the_earlier_one(capsys):
    run_id = _new_scout(capsys)["run_id"]
    _add(capsys, run_id, [KEEP])
    out = _add(capsys, run_id, [{**KEEP, "verdict": "discard"}], "second.jsonl")
    assert out["verified"] == {"keep": 0, "unsure": 0, "discard": 1} and out["added"] == 0
    assert len(runs.read_jsonl(runs.run_dir(run_id) / scout_verified.JUDGED_FILE)) == 1


def test_an_unverified_find_turns_judged_when_verified_later(capsys):
    run_id = _new_scout(capsys)["run_id"]
    _add(capsys, run_id, [{"url": POSTING}])
    _add(capsys, run_id, [KEEP], "second.jsonl")
    candidates = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
    assert len(candidates) == 1 and candidates[0]["search_pass"] == "judged"
    assert _cli(capsys, "fetch", "--run", run_id)["verified_skipped"] == 1


def test_verified_finds_lead_the_shortlist_and_extras_follow(capsys):
    run_id = _new_scout(capsys)["run_id"]
    _add(capsys, run_id, [KEEP, UNSURE, DISCARD])
    _cli(capsys, "discover", "ats", "--run", run_id)  # the verified Acme posting proposes its board
    fetched, routed, pending = _pipeline(capsys, run_id)
    assert fetched["verified_skipped"] == 3
    assert routed["verified_finds"] == 2  # discover already skipped the verified posting on its board
    assert "already_verified" not in routed["discard_reasons"]
    assert pending["verified_finds"] == 2
    assert all(item["identity_key"] != "greenhouse:acme:4012345" for item in pending["items"])
    extra = pending["items"][0]["identity_key"]
    _cli(capsys, "evaluate", "record", "--run", run_id,
         "--from", _write(run_id, "agent_eval.jsonl", [_verdict(extra, "keep", 1)]))
    out = _cli(capsys, "finalize", "--run", run_id)
    assert out["verified_finds"] == 2 and out["extras"] == 1 and out["roles"] == 3
    rows = _rows(out)
    assert [r["found_by"] for r in rows] == ["agent_verified", "agent_verified", "expansion"]
    assert [r["priority"] for r in rows] == [3, 1, 1]  # keep before unsure; the verified tier before any extra
    assert rows[0]["source_label"] == "Agent find" and rows[0]["fit_signals"] == ["Python", "ML"]
    with closing(ledger.connect()) as conn:
        stored = {key: (verdict, category, digest) for key, verdict, category, digest in conn.execute(
            "SELECT identity_key, verdict, discard_category, content_hash FROM scout_verdicts")}
    discard = next(value for value in stored.values() if value[0] == "discard" and value[1] == "agent_rejected")
    assert discard[2] == "" and stored["greenhouse:acme:4012345"] == ("keep", "", "")


def test_final_cap_cuts_extras_before_verified_finds(capsys):
    run_id = _new_scout(capsys)["run_id"]
    run = runs.load_run(run_id)
    run["settings"]["max_roles"] = 2
    runs._write_run(run_id, run)
    _add(capsys, run_id, [KEEP, UNSURE])
    _cli(capsys, "discover", "ats", "--run", run_id)
    _fetched, _routed, pending = _pipeline(capsys, run_id)
    extra = pending["items"][0]["identity_key"]
    _cli(capsys, "evaluate", "record", "--run", run_id,
         "--from", _write(run_id, "agent_eval.jsonl", [_verdict(extra, "keep", 1)]))
    out = _cli(capsys, "finalize", "--run", run_id)
    assert out["final_cap"] == 1 and out["extras"] == 0
    assert {r["found_by"] for r in _rows(out)} == {"agent_verified"}


def test_memory_of_verified_finds_across_runs(capsys):
    first = _new_scout(capsys)["run_id"]
    _add(capsys, first, [KEEP, DISCARD])
    _pipeline(capsys, first)
    _cli(capsys, "finalize", "--run", first)

    second = _new_scout(capsys)["run_id"]
    out = _add(capsys, second, [{**KEEP, "priority": 1}, DISCARD])
    assert [row["memory"] for row in out["rows"]] == ["seen", "rejected"]
    assert [row["outcome"] for row in out["rows"]] == ["verified", "already_rejected"]
    assert out["verified"]["discard"] == 0
    _pipeline(capsys, second)
    rows = _rows(_cli(capsys, "finalize", "--run", second))
    assert len(rows) == 1 and rows[0]["priority"] == 1 and rows[0]["is_new"] is False


def test_a_verified_find_seen_again_by_expansion_is_carried_and_hashed(capsys):
    first = _new_scout(capsys)["run_id"]
    _add(capsys, first, [KEEP])
    _pipeline(capsys, first)
    _cli(capsys, "finalize", "--run", first)

    second = _new_scout(capsys)["run_id"]
    _add(capsys, second, [{"url": POSTING, "company": "Acme"}])  # unverified this time: fetched and routed
    _, routed, _ = _pipeline(capsys, second)
    assert routed["carried_from_memory"] == 1
    out = _cli(capsys, "finalize", "--run", second)
    assert _rows(out)[0]["found_by"] == "memory"
    with closing(ledger.connect()) as conn:
        digest = conn.execute("SELECT content_hash FROM scout_verdicts WHERE identity_key = ?",
                              ("greenhouse:acme:4012345",)).fetchone()[0]
    assert digest


def test_check_verified_compares_without_changing_verdicts(capsys):
    run_id = _new_scout(capsys)["run_id"]
    _add(capsys, run_id, [KEEP, {**UNSURE, "url": GONE}, {**DISCARD, "url": EMPLOYER}])
    out = _cli(capsys, "fetch", "--run", run_id, "--check-verified")
    assert out["checked"] == 2 and out["agent_open_fetch_closed"] == 1
    check = {row["apply_url"]: row for row in runs.read_jsonl(Path(out["check_file"]))}
    assert check[GONE]["fetch_outcome"] == "closed" and check[GONE]["agent_verdict"] == "unsure"
    assert scout_verified.verdict_counts(scout_verified.load_verified(runs.run_dir(run_id)))["unsure"] == 1
    assert not (runs.run_dir(run_id) / "postings.jsonl").exists()


def test_check_verified_is_scout_only(capsys):
    sweep = _cli(capsys, "run", "new", "--profile", profiles.EXAMPLE_SLUG)["run_id"]
    assert main(["fetch", "--run", sweep, "--check-verified"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "stage_failed"


def test_brief_asks_for_verified_finds(capsys):
    brief = _new_scout(capsys)["brief"]
    assert brief["budgets"]["shortlist_target"] == 25
    assert any("verdict" in line for line in brief["advice"])


def test_route_postings_leaves_verified_identities_out(capsys):
    run_id = _new_scout(capsys)["run_id"]
    _add(capsys, run_id, [{"url": POSTING}, {"url": EMPLOYER}])
    _cli(capsys, "fetch", "--run", run_id)
    postings = runs.read_jsonl(runs.run_dir(run_id) / "postings.jsonl")
    key = postings[0]["identity_key"]
    routed = scout.route_postings(postings, runs.load_run(run_id), {key})
    assert routed["already_verified"] == 1 and "already_verified" not in routed["reasons"]
    assert all(entry["identity_key"] != key for entry in routed["queue"] + routed["discarded"])


@pytest.mark.parametrize(("page", "expected"), [
    (HttpError("boom"), "failed"),
    ((500, "https://x.example/j", ""), "failed"),
    ((410, "https://x.example/j", ""), "closed"),
    ((200, "https://x.example/j", "<html><body>Nothing here</body></html>"), "unusable"),
])
def test_fetch_outcome_branches(monkeypatch, page, expected):
    def fake(_client, _url):
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(verified_check, "get_posting_page", fake)
    assert verified_check.fetch_outcome(None, "https://x.example/j") == expected


def test_a_kept_verified_find_puts_its_board_on_the_watchlist(capsys):
    run_id = _new_scout(capsys)["run_id"]
    _add(capsys, run_id, [KEEP])
    _cli(capsys, "discover", "ats", "--run", run_id)
    _pipeline(capsys, run_id)
    _cli(capsys, "finalize", "--run", run_id)
    listed = _cli(capsys, "watchlist", "list", "--profile", profiles.EXAMPLE_SLUG)
    assert [w["seed_key"] for w in listed["watched"]] == ["greenhouse:acme"]


def test_verified_aggregator_finds_keep_only_the_employer_url(capsys):
    run_id = _new_scout(capsys)["run_id"]
    out = _add(capsys, run_id, [{**KEEP, "url": AGGREGATOR}, {**UNSURE, "url": DEAD_AGGREGATOR}])
    assert [row["outcome"] for row in out["rows"]] == ["verified", "unresolved_aggregator"]
    stored = scout_verified.load_verified(runs.run_dir(run_id))
    assert [row["apply_url"] for row in stored.values()] == [POSTING]
    unresolved = runs.read_jsonl(runs.run_dir(run_id) / "unresolved_finds.jsonl")
    assert [row["url"] for row in unresolved] == [DEAD_AGGREGATOR]


def test_a_run_with_only_verified_finds_has_nothing_to_fetch(capsys):
    run_id = _new_scout(capsys)["run_id"]
    _add(capsys, run_id, [KEEP, UNSURE])
    fetched, routed, pending = _pipeline(capsys, run_id)
    assert fetched["fetched"] == 0 and fetched["verified_skipped"] == 2
    assert routed["queued"] == 0 and pending["items"] == []
    out = _cli(capsys, "finalize", "--run", run_id)
    assert out["verified_finds"] == 2 and out["extras"] == 0 and out["agent_evaluated"] == 0


def test_a_carried_posting_verified_later_is_exported_once(capsys):
    first = _new_scout(capsys)["run_id"]
    _add(capsys, first, [KEEP])
    _pipeline(capsys, first)
    _cli(capsys, "finalize", "--run", first)

    second = _new_scout(capsys)["run_id"]
    _add(capsys, second, [{"url": POSTING, "company": "Acme"}])
    assert _pipeline(capsys, second)[1]["carried_from_memory"] == 1
    _add(capsys, second, [{**KEEP, "priority": 1}], "late.jsonl")  # verified after the filter carried it
    out = _cli(capsys, "finalize", "--run", second)
    rows = _rows(out)
    assert len(rows) == 1 and rows[0]["found_by"] == "agent_verified" and rows[0]["priority"] == 1
    assert out["carried_from_memory"] == 0 and "duplicate_role" not in json.dumps(out)


def test_a_verified_find_wins_its_duplicate_group_whatever_the_score():
    verified = scout_verified.final_row({**scout_verified.judgment({**UNSURE}), "identity_key": "k1",
                                         "apply_url": EMPLOYER, "source_type": "employer_page"})
    extra = verified.model_copy(update={"found_by": "expansion", "evaluation_verdict": "keep", "match_score": 99.0,
                                        "apply_url": EMPLOYER + "?ref=board"})
    kept, dropped = dedupe_final_rows([extra, verified])
    assert kept[0].found_by == "agent_verified" and dropped[0].found_by == "expansion"
    ranked = rank_scout_rows([extra.model_copy(update={"apply_url": "https://x.example/other",
                                                       "title": "Other"}), verified])
    assert [row.found_by for row in ranked] == ["agent_verified", "expansion"]


def test_memory_of_an_unfetched_verified_find():
    judged = {**scout_verified.judgment(KEEP), "identity_key": "greenhouse:acme:1", "apply_url": POSTING,
              "source_type": "web_search"}
    row = scout_verified.final_row(judged)
    with closing(ledger.connect()) as conn:
        scout_memory.record_verdicts(conn, "p", "run-1", [scout_verified.memory_item(judged, row)], "crit")
        assert scout_memory.flags(conn, "p", ["greenhouse:acme:1"]) == {"greenhouse:acme:1": "seen"}
        assert scout_memory.carried_row(conn, "p", "greenhouse:acme:1", "digest", "crit") is None
        assert scout_memory.carried_unfetched(conn, "p", "greenhouse:acme:1", "crit")[1] == 3
        assert scout_memory.carried_unfetched(conn, "p", "greenhouse:acme:1", "edited") is None  # profile changed
        scout_memory.record_verdicts(conn, "p", "run-2", [{"identity_key": "greenhouse:acme:1", "verdict": "keep",
                                                           "priority": 3, "content_hash": "digest", "row": row}],
                                     "crit")
        assert scout_memory.carried_unfetched(conn, "p", "greenhouse:acme:1", "crit") is None
        assert scout_memory.carried_row(conn, "p", "greenhouse:acme:1", "digest", "crit") is not None
        assert scout_memory.carried_row(conn, "p", "greenhouse:acme:1", "changed", "crit") is None


@pytest.mark.parametrize(("body", "expected"), [('{"job": null}', "closed"), ("not json", "unusable")])
def test_fetch_outcome_reads_occupop_job_json(monkeypatch, body, expected):
    url = "https://api.occupop.com/shared/job/" + OCCUPOP_REF
    monkeypatch.setattr(verified_check, "get_posting_page", lambda _client, _url: (200, url, body))
    assert verified_check.fetch_outcome(None, url) == expected


def test_pending_skips_postings_verified_after_the_filter(capsys):
    run_id = _new_scout(capsys)["run_id"]
    _add(capsys, run_id, [{"url": POSTING}, {"url": EMPLOYER}])
    _cli(capsys, "fetch", "--run", run_id)
    _cli(capsys, "filter", "--run", run_id)
    before = _cli(capsys, "evaluate", "pending", "--run", run_id, "--limit", "50")
    assert any(item["identity_key"] == "greenhouse:acme:4012345" for item in before["items"])
    _add(capsys, run_id, [KEEP], "late.jsonl")
    after = _cli(capsys, "evaluate", "pending", "--run", run_id, "--limit", "50")
    assert all(item["identity_key"] != "greenhouse:acme:4012345" for item in after["items"])
    assert after["pending"] == before["pending"] - 1 and after["verified_finds"] == 1


def test_a_late_verified_discard_drops_the_carried_row(capsys):
    first = _new_scout(capsys)["run_id"]
    _add(capsys, first, [KEEP])
    _pipeline(capsys, first)
    _cli(capsys, "finalize", "--run", first)
    second = _new_scout(capsys)["run_id"]
    _add(capsys, second, [{"url": POSTING, "company": "Acme"}])
    assert _pipeline(capsys, second)[1]["carried_from_memory"] == 1
    _add(capsys, second, [{**KEEP, "verdict": "discard"}], "late.jsonl")
    assert _cli(capsys, "finalize", "--run", second)["roles"] == 0

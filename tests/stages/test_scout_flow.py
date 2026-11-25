"""Scout mode end to end: brief, finds, expansion, the agent queue, priorities, memory and the watch."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from ultron import seeds
from ultron.cli import main
from ultron.core.models import AtsBoardSeed
from ultron.profiles import store as profiles
from ultron.stages import http_factory
from ultron.storage import ledger, runs, watchlist

FIX = Path(__file__).resolve().parents[1] / "fixtures"
GH_PAGE = (FIX / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")
GH_BOARD = (FIX / "ats" / "greenhouse_board.json").read_bytes()
POSTING = "https://boards.greenhouse.io/acme/jobs/4012345"
OTHER_POSTING = "https://boards.greenhouse.io/acme/jobs/4012348"
EMPLOYER = "https://careers.example.org/jobs/senior-ml-engineer"
AGGREGATOR = "https://remoteok.com/remote-jobs/123-senior-ml-engineer"
DEAD_AGGREGATOR = "https://remoteok.com/remote-jobs/999-unknown"
EMPLOYER_PAGE = """<html><head><title>Senior ML Engineer - Example Org</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "JobPosting",
"title": "Senior ML Engineer", "hiringOrganization": {"name": "Example Org"},
"jobLocationType": "TELECOMMUTE", "applicantLocationRequirements": {"@type": "Country", "name": "Spain"},
"description": "Senior machine learning engineer building LLM systems. Fully remote within the EU."}</script>
</head><body><h1>Senior ML Engineer</h1><p>Senior machine learning engineer building LLM and RAG systems.
Fully remote within the European Union. Apply now.</p></body></html>"""
AGGREGATOR_PAGE = f'<html><body><a href="{POSTING}">Apply on the company site</a><a href="/jobs">More</a></body></html>'


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


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "greenhouse" in request.url.host and request.url.path.startswith("/v1/boards/acme"):
        return httpx.Response(200, content=GH_BOARD)
    if request.url.host == "boards.greenhouse.io":
        return httpx.Response(200, text=GH_PAGE)
    if url.startswith(EMPLOYER):
        return httpx.Response(200, text=EMPLOYER_PAGE)
    if url.startswith(AGGREGATOR):
        return httpx.Response(200, text=AGGREGATOR_PAGE)
    if url.startswith(DEAD_AGGREGATOR):
        return httpx.Response(200, text="<html><body>No links here</body></html>")
    return httpx.Response(404)


def _cli(capsys, *args):
    code = main(list(args))
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    return out


def _new_scout(capsys):
    return _cli(capsys, "run", "new", "--profile", profiles.EXAMPLE_SLUG, "--mode", "scout")


def _write(run_id: str, name: str, rows: list[dict]) -> str:
    path = runs.run_dir(run_id) / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def _verdict(key: str, verdict: str, priority: int | None = None) -> dict:
    keep = verdict == "keep"
    evaluation = {"verdict": verdict, "keep": keep, "discard_category": None if verdict != "discard" else "role_family",
                  "discard_reason": None if verdict != "discard" else "not a match", "confidence": "high",
                  "is_senior": True, "is_allowed_family": verdict != "discard", "is_full_remote": True,
                  "eligibility": "high", "eligibility_evidence": "Remote EU", "fit_signals": ["ML"],
                  "open_status_ok": True}
    if priority is not None:
        evaluation["priority"] = priority
    return {"identity_key": key, "evaluation": evaluation}


def test_run_new_scout_returns_a_brief(capsys):
    out = _new_scout(capsys)
    assert out["mode"] == "scout" and "query_plan" not in out and "seed_deficit" not in out
    brief = out["brief"]
    assert brief["memory"]["new_profile"] is True and brief["budgets"]["evaluation_queue"] == 60
    assert brief["profile_summary"]["target_titles"] and brief["advice"]
    assert runs.load_run(out["run_id"])["mode"] == "scout"
    assert _cli(capsys, "runs", "list")["runs"][0]["mode"] == "scout"


def test_unknown_mode_is_a_usage_error(capsys):
    assert main(["run", "new", "--profile", profiles.EXAMPLE_SLUG, "--mode", "turbo"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "usage"


def test_stage_order_differs_by_mode(capsys):
    sweep = _cli(capsys, "run", "new", "--profile", profiles.EXAMPLE_SLUG)["run_id"]
    _cli(capsys, "candidates", "add", "--run", sweep, "--from", _write(sweep, "agent_web.jsonl", [{"url": POSTING}]))
    assert main(["fetch", "--run", sweep]) == 3
    capsys.readouterr()
    scout = _new_scout(capsys)["run_id"]
    _cli(capsys, "candidates", "add", "--run", scout, "--from", _write(scout, "agent_finds.jsonl", [{"url": POSTING}]))
    assert _cli(capsys, "fetch", "--run", scout)["fetched"] == 1


def test_candidates_add_resolves_every_kind_of_find(capsys):
    run_id = _new_scout(capsys)["run_id"]
    finds = [
        {"url": POSTING, "title": "Senior Data Scientist", "company": "Acme"},
        {"url": EMPLOYER, "title": "Senior ML Engineer", "company": "Example Org"},
        {"url": AGGREGATOR, "company": "Acme"},
        {"url": DEAD_AGGREGATOR, "company": "Mystery Co", "title": "ML Engineer"},
        {"url": POSTING},
        {"url": "ftp://example.org/job"},
        {"url": "https://boards.greenhouse.io/hiringco/jobs/77", "company": "Talent Connect Recruitment"},
    ]
    out = _cli(capsys, "candidates", "add", "--run", run_id, "--from", _write(run_id, "agent_finds.jsonl", finds))
    outcomes = [row["outcome"] for row in out["rows"]]
    assert outcomes == ["ats_posting", "employer_page", "duplicate", "unresolved_aggregator", "duplicate",
                        "rejected", "ats_posting"]
    assert out["rows"][2]["canonical_url"] == POSTING  # the aggregator resolved to the employer posting
    assert all(row.get("memory") == "new" for row in out["rows"] if "identity_key" in row)
    assert out["seed_proposals"] == 1  # acme; the recruiter's board is never proposed
    folder = runs.run_dir(run_id)
    unresolved = runs.read_jsonl(folder / "unresolved_finds.jsonl")
    assert [u["company"] for u in unresolved] == ["Mystery Co"]
    candidates = runs.read_jsonl(folder / "candidates.jsonl")
    assert {c["source_type"] for c in candidates} == {"web_search", "employer_page"}
    assert not any("remoteok" in c["normalized_url"] for c in candidates)


def _full_run(capsys, *, verdicts=None):
    run_id = _new_scout(capsys)["run_id"]
    finds = [{"url": POSTING, "company": "Acme"}, {"url": EMPLOYER, "company": "Example Org"}]
    _cli(capsys, "candidates", "add", "--run", run_id, "--from", _write(run_id, "agent_finds.jsonl", finds))
    discover = _cli(capsys, "discover", "ats", "--run", run_id)
    _cli(capsys, "fetch", "--run", run_id)
    routed = _cli(capsys, "filter", "--run", run_id)
    pending = _cli(capsys, "evaluate", "pending", "--run", run_id, "--limit", "50")
    return run_id, discover, routed, pending


def test_scout_run_end_to_end(capsys):
    run_id, discover, routed, pending = _full_run(capsys)
    assert discover["mode"] == "scout" and discover["seed_groups"]["found"]["queried"] == 1
    assert discover["board_postings"]["listed"] >= 1
    report = runs.read_jsonl(runs.run_dir(run_id) / "board_report.jsonl")
    assert report and report[0]["seed_key"] == "greenhouse:acme"
    assert routed["mode"] == "scout" and routed["queued"] >= 2
    items = pending["items"]
    assert items[0]["found_by"] == "agent" and items[0]["memory"] == "new" and "hint" in items[0]
    assert any(item["found_by"] == "expansion" for item in items)
    agent_items = [i for i in items if i["found_by"] == "agent"]
    assert any(i["identity_key"] == "greenhouse:acme:4012345" for i in agent_items)  # expansion keeps the origin
    first, second = agent_items[0]["identity_key"], agent_items[1]["identity_key"]
    expansion = next(i["identity_key"] for i in items if i["found_by"] == "expansion")
    verdicts = [_verdict(first, "keep", 3), _verdict(second, "keep", 1), _verdict(expansion, "discard")]
    _cli(capsys, "evaluate", "record", "--run", run_id, "--from", _write(run_id, "agent_eval.jsonl", verdicts))
    out = _cli(capsys, "finalize", "--run", run_id)
    assert out["mode"] == "scout" and out["roles"] == 2 and out["agent_evaluated"] == 3
    assert out["not_evaluated"] == len(items) - 3
    rows = json.loads(Path(out["outputs"]["results_json"]).read_text(encoding="utf-8"))["rows"]
    assert [r["priority"] for r in rows] == [1, 3]
    assert all(r["found_by"] == "agent" for r in rows)
    with closing(ledger.connect()) as conn:
        stored = dict(conn.execute("SELECT identity_key, verdict FROM scout_verdicts").fetchall())
    assert stored[first] == "keep" and stored[expansion] == "discard"


def test_memory_carries_seen_and_skips_rejected(capsys):
    run_id, _discover, _routed, pending = _full_run(capsys)
    agent_items = [i for i in pending["items"] if i["found_by"] == "agent"]
    kept, rejected = agent_items[0]["identity_key"], agent_items[1]["identity_key"]
    verdicts = [_verdict(kept, "keep", 2), _verdict(rejected, "discard")]
    _cli(capsys, "evaluate", "record", "--run", run_id, "--from", _write(run_id, "agent_eval.jsonl", verdicts))
    _cli(capsys, "finalize", "--run", run_id)

    second = _new_scout(capsys)
    assert second["brief"]["memory"]["known_postings"] >= 2
    run2 = second["run_id"]
    finds = [{"url": POSTING, "company": "Acme"}, {"url": EMPLOYER, "company": "Example Org"}]
    added = _cli(capsys, "candidates", "add", "--run", run2, "--from", _write(run2, "agent_finds.jsonl", finds))
    assert sorted(row["memory"] for row in added["rows"]) == ["rejected", "seen"]
    _cli(capsys, "fetch", "--run", run2)
    routed = _cli(capsys, "filter", "--run", run2)
    assert routed["carried_from_memory"] == 1 and routed["discard_reasons"].get("already_rejected") == 1
    out = _cli(capsys, "finalize", "--run", run2)
    assert out["carried_from_memory"] == 1 and out["roles"] == 1
    rows = json.loads(Path(out["outputs"]["results_json"]).read_text(encoding="utf-8"))["rows"]
    assert rows[0]["found_by"] == "memory" and rows[0]["is_new"] is False


def test_watch_finds_new_postings_and_feeds_the_next_scout_run(capsys):
    out = _cli(capsys, "watch", "--profile", profiles.EXAMPLE_SLUG)
    assert out["boards"] == 0 and "empty" in out["hint"]
    seeds.add_seeds(
        [AtsBoardSeed(seed_id="greenhouse:acme", company="Acme", ats="greenhouse", board_identifier="acme",
                      priority=1, profiles=[profiles.EXAMPLE_SLUG])],
        profile_slug=profiles.EXAMPLE_SLUG,
    )
    _cli(capsys, "watchlist", "add", "--profile", profiles.EXAMPLE_SLUG, "--board", "greenhouse:acme")
    listed = _cli(capsys, "watchlist", "list", "--profile", profiles.EXAMPLE_SLUG)
    assert listed["watched"][0]["seed_key"] == "greenhouse:acme"
    first = _cli(capsys, "watch", "--profile", profiles.EXAMPLE_SLUG)
    assert first["boards"] == 1 and first["new_postings"] >= 1 and Path(first["digest"]).is_file()
    assert _cli(capsys, "watch", "--profile", profiles.EXAMPLE_SLUG)["new_postings"] == 0
    brief = _new_scout(capsys)
    assert brief["brief"]["memory"]["pending_watch"] == first["new_postings"]
    candidates = runs.read_jsonl(runs.run_dir(brief["run_id"]) / "candidates.jsonl")
    assert all(c["search_pass"] == "watch" for c in candidates)
    with closing(ledger.connect()) as conn:
        assert watchlist.pending(conn, profiles.EXAMPLE_SLUG) == []
    _cli(capsys, "watchlist", "remove", "--profile", profiles.EXAMPLE_SLUG, "--board", POSTING)
    assert _cli(capsys, "watchlist", "list", "--profile", profiles.EXAMPLE_SLUG)["watched"] == []


def test_watchlist_rejects_unknown_boards_and_profiles(capsys):
    assert main(["watchlist", "add", "--profile", profiles.EXAMPLE_SLUG, "--board", "nope"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_board"
    assert main(["watch", "--profile", "missing-profile"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "profile_not_found"
    assert main(["watch", "--profile", profiles.EXAMPLE_SLUG, "--preset", "turbo"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "unknown_preset"


def test_saved_search_runs_the_watch(capsys):
    _cli(capsys, "saved-search", "create", "daily", "--profile", profiles.EXAMPLE_SLUG)
    out = _cli(capsys, "saved-search", "run", "daily", "--watch")
    assert out["saved_search"] == "daily" and out["boards"] == 0


def test_watch_queries_a_board_outside_the_inventory(capsys):
    _cli(capsys, "watchlist", "add", "--profile", profiles.EXAMPLE_SLUG, "--board", "https://boards.greenhouse.io/acme")
    out = _cli(capsys, "watch", "--profile", profiles.EXAMPLE_SLUG)
    assert out["boards"] == 1 and out["new_postings"] >= 1


def test_expansion_keeps_watch_candidates(capsys):
    _cli(capsys, "watchlist", "add", "--profile", profiles.EXAMPLE_SLUG, "--board", "greenhouse:acme")
    _cli(capsys, "watch", "--profile", profiles.EXAMPLE_SLUG)
    run_id = _new_scout(capsys)["run_id"]
    before = {c["normalized_url"] for c in runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")}
    _cli(capsys, "discover", "ats", "--run", run_id)
    after = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
    watched = {c["normalized_url"] for c in after if c["search_pass"] == "watch"}
    assert before and before <= watched


def test_saved_search_watch_applies_overrides(capsys):
    _cli(capsys, "saved-search", "create", "tight", "--profile", profiles.EXAMPLE_SLUG,
         "--override", "max_requests_per_host=1")
    _cli(capsys, "watchlist", "add", "--profile", profiles.EXAMPLE_SLUG, "--board", "greenhouse:acme")
    out = _cli(capsys, "saved-search", "run", "tight", "--watch")
    assert all(item["requests"] <= 1 for item in out["requests_by_domain"])


def test_unevaluated_watch_postings_return_to_pending(capsys):
    _cli(capsys, "watchlist", "add", "--profile", profiles.EXAMPLE_SLUG, "--board", "greenhouse:acme")
    found = _cli(capsys, "watch", "--profile", profiles.EXAMPLE_SLUG)["new_postings"]
    run_id = _new_scout(capsys)["run_id"]
    _cli(capsys, "fetch", "--run", run_id)
    _cli(capsys, "filter", "--run", run_id)
    _cli(capsys, "finalize", "--run", run_id)  # nothing judged
    with closing(ledger.connect()) as conn:
        assert len(watchlist.pending(conn, profiles.EXAMPLE_SLUG)) == found


def test_kept_agent_find_puts_its_board_on_the_watchlist(capsys):
    run_id, _discover, _routed, pending = _full_run(capsys)
    find = next(i["identity_key"] for i in pending["items"] if i["identity_key"] == "greenhouse:acme:4012345")
    _cli(capsys, "evaluate", "record", "--run", run_id, "--from", _write(run_id, "agent_eval.jsonl", [_verdict(find, "keep", 1)]))
    _cli(capsys, "finalize", "--run", run_id)
    listed = _cli(capsys, "watchlist", "list", "--profile", profiles.EXAMPLE_SLUG)
    assert [w["seed_key"] for w in listed["watched"]] == ["greenhouse:acme"]

"""Scout mode units: find resolution, memory flags, the watchlist, routing, ranking and the v6 ledger."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing

import pytest
from pydantic import ValidationError

from ultron.core.models import FinalRow, JobEvaluation
from ultron.core.ranker.final_rows import rank_scout_rows
from ultron.infra.net import RequestBudgetExceeded
from ultron.profiles import store as profiles
from ultron.stages import scout_resolve, watch_boards
from ultron.storage import ledger, runs, scout_memory, watchlist

POSTING = "https://boards.greenhouse.io/acme/jobs/4012345"


# --- resolution -----------------------------------------------------------------------------------


@pytest.mark.parametrize("url, outcome", [
    (POSTING, "ats_posting"),
    ("https://careers.example.org/jobs/1", "employer_page"),
    ("https://remoteok.com/remote-jobs/1", "aggregator"),
    ("ftp://example.org/x", "rejected"),
    ("", "rejected"),
])
def test_classify_find(url, outcome):
    assert scout_resolve.classify_find(url).outcome == outcome


@pytest.mark.parametrize("company, expected", [
    ("Talent Connect", True), ("AIDA Recruitment", True), ("Jobgether", True), ("Acme Staffing", True),
    ("Acme", False), ("", False),
])
def test_is_recruiter(company, expected):
    assert scout_resolve.is_recruiter(company) is expected


@pytest.mark.parametrize("url, expected", [
    (POSTING, True),
    ("https://boards.greenhouse.io/acme", False),  # board root, not a posting
    ("https://www.greenhouse.com/careers", False),  # vendor marketing site
    ("https://careers.example.org/jobs/1", False),
])
def test_is_job_link(url, expected):
    assert scout_resolve.is_job_link(url) is expected


class _Client:
    def __init__(self, links=None, error=None):
        self.links, self.error = links or [], error

    def get_text(self, url, **_kwargs):
        if self.error:
            raise self.error
        body = "".join(f'<a href="{link}">x</a>' for link in self.links)
        return 200, url, f"<html><body>{body}</body></html>"


def test_resolve_aggregator_with_several_posting_links():
    other = "https://jobs.lever.co/otherco/11111111-2222-3333-4444-555555555555"
    page = _Client([other, POSTING])
    assert scout_resolve.resolve_aggregator("https://remoteok.com/1", page, 5).reason == "ambiguous_posting_links"
    picked = scout_resolve.resolve_aggregator("https://remoteok.com/1", page, 5, company="Acme Inc.")
    assert picked.url == POSTING
    same = _Client([POSTING, POSTING + "?utm_source=x"])
    assert scout_resolve.resolve_aggregator("https://remoteok.com/1", same, 5).url == POSTING


def test_board_seed_for_unknown_and_known_vendors():
    seed = watch_boards.board_seed("greenhouse:acme", "Acme")
    assert (seed.ats, seed.board_identifier, seed.company) == ("greenhouse", "acme", "Acme")
    assert watch_boards.board_seed("nosuchvendor:x") is None and watch_boards.board_seed("greenhouse:") is None


def test_resolve_aggregator_outcomes():
    resolved = scout_resolve.resolve_aggregator("https://remoteok.com/1", _Client(["javascript:x", POSTING]), 5)
    assert (resolved.outcome, resolved.url) == ("resolved_aggregator", POSTING)
    none = scout_resolve.resolve_aggregator("https://remoteok.com/1", _Client(["https://remoteok.com/2"]), 5)
    assert none.outcome == "unresolved_aggregator" and none.reason == "no_employer_posting_link"
    budget = scout_resolve.resolve_aggregator("https://remoteok.com/1", _Client(error=RequestBudgetExceeded("run")), 5)
    assert budget.outcome == "unresolved_aggregator" and budget.reason == "RequestBudgetExceeded"


# --- memory and watchlist -------------------------------------------------------------------------


def _row(title="Senior ML Engineer", company="Acme"):
    return FinalRow(title=title, company=company, apply_url=POSTING, location="Remote", remote_policy="remote_eu",
                    eligibility="high", eligibility_evidence="", fit_signals=[], freshness="")


def test_memory_flags():
    with closing(ledger.connect()) as conn:
        digest = "posting-digest"
        criteria = scout_memory.profile_hash({"criteria": {"target_titles": ["ML Engineer"]}})
        scout_memory.record_verdicts(conn, "p1", "r1", [
            {"identity_key": "k-keep", "verdict": "keep", "priority": 1, "content_hash": digest, "row": _row()},
            {"identity_key": "k-drop", "verdict": "discard", "discard_category": "role_family"},
            {"identity_key": "k-closed", "verdict": "discard", "discard_category": "closed_or_invalid"},
        ], criteria)
        conn.execute("INSERT INTO jobs (identity_key, canonical_url, first_seen, last_seen, review_state)"
                     " VALUES ('k-applied', 'u', 'x', 'x', 'applied'), ('k-archived', 'u2', 'x', 'x', 'archived'),"
                     " ('k-interview', 'u3', 'x', 'x', 'interview')")
        flags = scout_memory.flags(conn, "p1", ["k-keep", "k-drop", "k-closed", "k-applied", "k-archived", "k-new"])
        assert flags == {"k-keep": "seen", "k-drop": "rejected", "k-closed": "new", "k-applied": "applied",
                         "k-archived": "rejected", "k-new": "new"}
        assert scout_memory.flags(conn, "p2", ["k-keep", "k-interview"]) == {
            "k-keep": "new", "k-interview": "applied",
        }


def _yield(conn, profile, seed, run, kept):
    conn.execute("INSERT INTO seed_yield_runs (profile_slug, seed_key, run_id, kept) VALUES (?, ?, ?, ?)",
                 (profile, seed, run, kept))


def test_watchlist_sources_blocking_and_demotion():
    with closing(ledger.connect()) as conn:
        _yield(conn, "sibling", "greenhouse:good", "r1", 2)
        for run in range(5):
            _yield(conn, "p1", "greenhouse:stale", f"s{run}", 0)
        _yield(conn, "sibling", "greenhouse:stale", "x", 1)
        _yield(conn, "other", "greenhouse:foreign", "r9", 3)
        conn.commit()
        watchlist.add(conn, "p1", "lever:manual", "Manual Co")
        watched = watchlist.watched(conn, "p1", ["sibling"])
        assert [w["seed_key"] for w in watched] == ["lever:manual", "greenhouse:good"]
        assert watched[0]["source"] == "user" and watched[1]["source"] == "history"
        watchlist.remove(conn, "p1", "greenhouse:good")
        assert [w["seed_key"] for w in watchlist.watched(conn, "p1", ["sibling"])] == ["lever:manual"]
        assert {e["state"] for e in watchlist.entries(conn, "p1")} == {"watch", "blocked"}
        watchlist.add(conn, "p1", "greenhouse:good")
        assert "greenhouse:good" in [w["seed_key"] for w in watchlist.watched(conn, "p1", ["sibling"])]


def test_watch_pending_lifecycle():
    with closing(ledger.connect()) as conn:
        added = watchlist.add_pending(conn, "p1", [("k1", {"normalized_url": "u1"}), ("k2", {"normalized_url": "u2"})])
        assert added == ["k1", "k2"]
        assert watchlist.add_pending(conn, "p1", [("k1", {"normalized_url": "u1"})]) == []
        assert len(watchlist.pending(conn, "p1")) == 2
        assert watchlist.mark_queued(conn, "p1", ["k1"], "run") == 1
        assert [p["normalized_url"] for p in watchlist.pending(conn, "p1")] == ["u2"]
        assert watchlist.known_pending(conn, "p1") == {"k1", "k2"}


def test_siblings_share_role_family(monkeypatch):
    profiles_by_slug = {
        "me": {"criteria": {"role_family_label": "AI/ML engineering", "vocabulary_packs": ["ai-ml"]}},
        "same-label": {"criteria": {"role_family_label": "ai/ml  Engineering"}},
        "same-pack": {"criteria": {"vocabulary_packs": ["AI-ML"]}},
        "other": {"criteria": {"role_family_label": "Finance"}},
        "broken": None,
    }

    def load(slug):
        if profiles_by_slug[slug] is None:
            raise OSError("unreadable")
        return profiles_by_slug[slug]

    monkeypatch.setattr(profiles, "list_profiles", lambda: [{"slug": s} for s in profiles_by_slug] + [{"slug": ""}])
    monkeypatch.setattr(profiles, "load_profile", load)
    assert watch_boards.siblings("me", profiles_by_slug["me"]) == ["same-label", "same-pack"]


def test_ledger_v6_upgrade_from_v5(tmp_path):
    with closing(ledger.connect()) as conn:
        conn.execute("DROP TABLE watchlist")
        conn.execute("DROP TABLE watch_pending")
        conn.execute("DROP TABLE scout_verdicts")
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    with closing(ledger.connect()) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"watchlist", "watch_pending", "scout_verdicts"} <= tables
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO watchlist (profile_slug, seed_key, state, added_at) VALUES ('p', 'k', 'x', 't')")


# --- routing and ranking --------------------------------------------------------------------------


def test_rank_scout_rows_keeps_the_agents_order():
    rows = [_row("A"), _row("B"), _row("C"), _row("D")]
    rows[0].evaluation_verdict, rows[0].priority = "unsure", 1
    rows[1].priority, rows[2].priority = 3, None
    rows[3].priority, rows[3].match_score = 3, 99
    ranked = rank_scout_rows(rows)
    assert [r.title for r in ranked] == ["C", "B", "D", "A"]  # keeps, priority, then the order sent
    zebra, apple = _row("Z", company="Zebra"), _row("A", company="Apple")
    assert [r.company for r in rank_scout_rows([zebra, apple])] == ["Zebra", "Apple"]  # never alphabetical


def test_job_evaluation_priority_bounds():
    base = {"keep": True, "is_senior": True, "is_allowed_family": True, "is_full_remote": True}
    assert JobEvaluation.model_validate({**base, "priority": 1}).priority == 1
    assert JobEvaluation.model_validate(base).priority is None
    with pytest.raises(ValidationError):
        JobEvaluation.model_validate({**base, "priority": 4})


def test_new_run_rejects_unknown_modes():
    profiles.install_example()
    with pytest.raises(ValueError, match="unknown mode"):
        runs.new_run(profiles.EXAMPLE_SLUG, "balanced", mode="turbo")
    assert runs.run_mode({}) == "sweep"


def test_row_board_key_credits_agent_finds():
    from ultron.stages.final_helpers import row_board_key

    found = _row()
    found.source_type, found.source_key = "web_search", "web_search"
    assert row_board_key(found) == "greenhouse:acme"
    expanded = _row()
    expanded.source_type, expanded.source_key = "ats_api", "greenhouse:acme"
    assert row_board_key(expanded) == "greenhouse:acme"
    page = _row()
    page.apply_url, page.source_type = "https://careers.example.org/jobs/1", "employer_page"
    assert row_board_key(page) == ""


def test_duplicate_selection_honours_the_agent_priority():
    from ultron.core.ranker import dedupe_final_rows

    first, second = _row(), _row()
    first.priority, first.match_score = 3, 95
    second.priority, second.match_score = 1, 40
    kept, duplicates = dedupe_final_rows([first, second])
    assert kept == [second] and duplicates == [first]


def test_watch_digest_escapes_board_text(tmp_path, monkeypatch):
    from ultron.stages import watch as watch_stage

    found = [{"ats_company": "Evil <b>Co</b>", "ats_title": "![x](https://evil.example/t.png) Engineer",
              "ats_location": "[click](https://evil.example)", "normalized_url": "https://boards.greenhouse.io/x/jobs/1"}]
    path = watch_stage._write_digest("p1", found, "20261125T000000")
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    assert "&lt;b&gt;" in text and "<b>" not in text
    assert not re.search(r"(?<!\\)\]\(", text)  # every link bracket is escaped

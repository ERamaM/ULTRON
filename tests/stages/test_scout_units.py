"""Scout mode units: find resolution, memory flags, the watchlist, routing, ranking and the v6 ledger."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from pydantic import ValidationError

from ultron.core.models import FinalRow, JobEvaluation
from ultron.core.ranker.final_rows import rank_scout_rows
from ultron.infra.net import RequestBudgetExceeded
from ultron.profiles import store as profiles
from ultron.stages import scout, scout_resolve
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


def test_content_hash_covers_eligibility_text():
    base = scout_memory.content_hash("ML Engineer", "Remote", "Remote EU", "Open to EU residents")
    assert base != scout_memory.content_hash("ML Engineer", "Remote", "Remote EU", "US residents only")
    assert base != scout_memory.content_hash("ML Engineer", "Remote", "Remote US", "Open to EU residents")


def test_board_seed_for_unknown_and_known_vendors():
    seed = scout.board_seed("greenhouse:acme", "Acme")
    assert (seed.ats, seed.board_identifier, seed.company) == ("greenhouse", "acme", "Acme")
    assert scout.board_seed("nosuchvendor:x") is None and scout.board_seed("greenhouse:") is None


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


def test_memory_flags_and_carried_rows():
    with closing(ledger.connect()) as conn:
        digest = scout_memory.content_hash("Senior ML Engineer", "Remote")
        scout_memory.record_verdicts(conn, "p1", "r1", [
            {"identity_key": "k-keep", "verdict": "keep", "priority": 1, "content_hash": digest, "row": _row()},
            {"identity_key": "k-drop", "verdict": "discard", "discard_category": "role_family"},
            {"identity_key": "k-closed", "verdict": "discard", "discard_category": "closed_or_invalid"},
        ])
        conn.execute("INSERT INTO jobs (identity_key, canonical_url, first_seen, last_seen, review_state)"
                     " VALUES ('k-applied', 'u', 'x', 'x', 'applied'), ('k-archived', 'u2', 'x', 'x', 'archived')")
        flags = scout_memory.flags(conn, "p1", ["k-keep", "k-drop", "k-closed", "k-applied", "k-archived", "k-new"])
        assert flags == {"k-keep": "seen", "k-drop": "rejected", "k-closed": "new", "k-applied": "applied",
                         "k-archived": "rejected", "k-new": "new"}
        assert scout_memory.flags(conn, "p2", ["k-keep"]) == {"k-keep": "new"}
        carried = scout_memory.carried_row(conn, "p1", "k-keep", digest)
        assert carried is not None and carried[1] == 1 and carried[0].title == "Senior ML Engineer"
        assert scout_memory.carried_row(conn, "p1", "k-keep", "changed") is None
        assert scout_memory.known_count(conn, "p1") == 3


def test_rejected_companies():
    with closing(ledger.connect()) as conn:
        conn.execute("INSERT INTO jobs (identity_key, canonical_url, first_seen, last_seen, review_state, company,"
                     " profile_slug) VALUES ('a', 'u', 'x', 'x', 'rejected', 'Bad Co', 'p1'),"
                     " ('b', 'u2', 'x', 'x', 'rejected', 'Bad Co', 'p1'), ('c', 'u3', 'x', 'x', 'unreviewed', 'Ok', 'p1')")
        assert scout_memory.rejected_companies(conn, "p1") == ["Bad Co"]


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
    assert scout.siblings("me", profiles_by_slug["me"]) == ["same-label", "same-pack"]


def test_ledger_v6_upgrade_from_v5(tmp_path):
    with closing(ledger.connect()) as conn:
        conn.execute("DROP TABLE watchlist")
        conn.execute("DROP TABLE watch_pending")
        conn.execute("DROP TABLE scout_verdicts")
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
    with closing(ledger.connect()) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"watchlist", "watch_pending", "scout_verdicts"} <= tables
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO watchlist (profile_slug, seed_key, state, added_at) VALUES ('p', 'k', 'x', 't')")


# --- routing and ranking --------------------------------------------------------------------------


def _scout_run(cap=60):
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced", mode="scout")
    run = runs.load_run(run_id)
    run["settings"]["scout_queue_cap"] = cap
    return run


def _posting(n, *, source="web_search", search_pass="", **job):
    base = {"title": f"Senior ML Engineer {n}", "company": "Acme", "location": "Remote, Spain",
            "remote_text": "Fully remote within the EU", "apply_url": f"https://boards.greenhouse.io/acme/jobs/{n}",
            "domain": "boards.greenhouse.io",
            "body_text": "Senior machine learning engineer building LLM systems. Fully remote within the EU."}
    return {"candidate": {"source_type": source, "search_pass": search_pass}, "job": {**base, **job},
            "identity_key": f"greenhouse:acme:{n}"}


def test_route_postings_drops_only_hard_facts_and_orders_the_queue():
    rows = [
        _posting(1, source="ats_api"),
        _posting(2),
        _posting(3, source="ats_api", search_pass="watch"),
        _posting(4, title=""),
        _posting(5, apply_url="https://remoteok.com/remote-jobs/5", domain="remoteok.com"),
        _posting(6, body_text="This position has been filled and is no longer accepting applications."),
        _posting(7, title="Junior Sales Manager", body_text="Sales role in New York, on site five days a week."),
    ]
    routed = scout.route_postings(rows, _scout_run())
    reasons = {d["identity_key"]: d["reason"] for d in routed["discarded"]}
    assert reasons["greenhouse:acme:4"] == "unusable_page"
    assert reasons["greenhouse:acme:5"] == "aggregator_page"
    assert reasons.get("greenhouse:acme:6") == "expired_or_closed"
    order = [e["identity_key"] for e in routed["queue"]]
    assert order[:2] == ["greenhouse:acme:2", "greenhouse:acme:7"]  # agent finds first, whatever the hint
    assert order.index("greenhouse:acme:3") < order.index("greenhouse:acme:1")  # watch before expansion
    hinted = next(e for e in routed["queue"] if e["identity_key"] == "greenhouse:acme:7")
    assert hinted["hint"]["outcome"] == "discard" and hinted["found_by"] == "agent"


def test_route_postings_caps_the_queue():
    routed = scout.route_postings([_posting(n) for n in range(1, 6)], _scout_run(cap=2))
    assert len(routed["queue"]) == 2 and len(routed["not_evaluated"]) == 3


def test_rank_scout_rows():
    rows = [_row("A"), _row("B"), _row("C"), _row("D")]
    rows[0].evaluation_verdict, rows[0].priority = "unsure", 1
    rows[1].priority, rows[2].priority = 3, None
    rows[3].priority, rows[3].match_score = 3, 99
    ranked = rank_scout_rows(rows, {id(rows[1])})
    assert [r.title for r in ranked] == ["C", "B", "D", "A"]


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

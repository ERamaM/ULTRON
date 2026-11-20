import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from ultron import ledger, review
from ultron.cli import main
from ultron.core.models import FinalRow, RunSummary

URL = "https://boards.greenhouse.io/acme/jobs/123"


def _row(url=URL):
    return FinalRow(
        title="Data Engineer", company="Acme", apply_url=url, location="Madrid",
        remote_policy="remote_eu", eligibility="confirmed", eligibility_evidence="",
        fit_signals=[], freshness="", match_score=80, match_band="good",
    )


def _run(conn, run_id, minutes, rows, profile="p"):
    t = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minutes)
    ledger.record_run(conn, run_id, RunSummary(run_id=run_id, started_at=t, ended_at=t, ats_pages_opened=0,
                                               roles_kept=len(rows), discard_counts={}, full_query_log=[]), profile, "fast")
    ledger.record_occurrences(conn, run_id, rows, profile_slug=profile)


def _key():
    return review.resolve_key(URL)


def test_record_fills_v2_columns():
    with closing(ledger.connect()) as conn:
        _run(conn, "r1", 0, [_row()])
    (job,) = review.list_jobs()
    assert job["location"] == "Madrid" and job["match_band"] == "good" and job["profile_slug"] == "p"
    assert job["first_seen_run_id"] == "r1" and job["review_state"] == "unreviewed"


def test_set_state_rejects_invalid():
    with closing(ledger.connect()) as conn:
        _run(conn, "r1", 0, [_row()])
    with pytest.raises(ValueError):
        review.set_state(_key(), "maybe")
    review.set_state(_key(), "applied")
    assert review.list_jobs(state="applied")[0]["review_changed_at"]
    assert review.list_jobs(state="rejected") == []


def test_mark_by_url():
    with closing(ledger.connect()) as conn:
        _run(conn, "r1", 0, [_row()])
    review.set_state(URL + "?utm_source=x", "shortlisted")
    assert review.list_jobs(state="shortlisted")
    with pytest.raises(LookupError):
        review.set_state("https://nowhere.example/job/9", "applied")


def test_notes_append():
    with closing(ledger.connect()) as conn:
        _run(conn, "r1", 0, [_row()])
    review.add_note(URL, "one")
    review.add_note(URL, "two")
    notes = review.list_jobs()[0]["notes"]
    assert "one" in notes and "two" in notes


def test_feedback_upsert():
    with closing(ledger.connect()) as conn:
        _run(conn, "r1", 0, [_row()])
    review.set_feedback(URL, "not_relevant", "seniority", "too senior")
    review.set_feedback(URL, "relevant")
    with closing(ledger.connect()) as conn:
        rows = conn.execute("SELECT relevance, reason_code, note FROM job_feedback").fetchall()
    assert rows == [("relevant", "", "")]
    with pytest.raises(ValueError):
        review.set_feedback(URL, "meh")
    with pytest.raises(ValueError):
        review.set_feedback(URL, "relevant", "bogus")


def test_new_since():
    with closing(ledger.connect()) as conn:
        _run(conn, "r1", 0, [_row()])
        _run(conn, "r2", 10, [_row(), _row("https://boards.greenhouse.io/acme/jobs/456")])
    assert review.new_since("r2") == []
    assert [row["identity_key"] for row in review.new_since("r1")] == ["greenhouse:acme:456"]
    assert len(review.list_jobs(since_run="r2")) == 2
    assert len(review.list_jobs(new_only=True)) == 1
    assert review.list_jobs(profile="zzz") == []
    with pytest.raises(LookupError):
        review.new_since("nope")


def test_cli_jobs(capsys):
    with closing(ledger.connect()) as conn:
        _run(conn, "r1", 0, [_row()])
    assert main(["jobs", "mark", URL, "applied"]) == 0
    assert json.loads(capsys.readouterr().out)["review_state"] == "applied"
    assert main(["jobs", "list", "--state", "applied"]) == 0
    assert len(json.loads(capsys.readouterr().out)["jobs"]) == 1
    assert main(["jobs", "mark", URL, "maybe"]) != 0
    assert "error" in json.loads(capsys.readouterr().out)
    assert main(["jobs", "note", URL, "hi"]) == 0
    assert main(["jobs", "feedback", URL, "relevant", "--reason", "other"]) == 0
    assert main(["jobs", "new-since", "r1"]) == 0
    capsys.readouterr()
    assert main(["jobs", "mark", "https://nowhere.example/x/1", "applied"]) != 0


def test_url_resolves_a_job_recorded_with_source_hints():
    embedded = "https://careers.acme.example/jobs?ashby_jid=0b1c2d3e-4f50-6789-abcd-ef0123456789"
    row = _row(embedded).model_copy(update={"source_type": "ats_api", "source_key": "ashby:acme"})
    with closing(ledger.connect()) as conn:
        _run(conn, "r1", 0, [row])
    stored = "ashby:acme:0b1c2d3e-4f50-6789-abcd-ef0123456789"
    assert review.set_state(embedded + "&utm_source=x", "applied") == stored  # the bare URL identity differs
    assert review.add_note(embedded, "called back") == stored
    assert review.set_feedback(embedded, "relevant") == stored
    (job,) = review.list_jobs(state="applied")
    assert job["identity_key"] == stored and "called back" in job["notes"]

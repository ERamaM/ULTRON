from contextlib import closing
from datetime import UTC, datetime

from ultron import ledger
from ultron.core.models import FinalRow, RunSummary


def _row(url="https://boards.greenhouse.io/acme/jobs/123"):
    return FinalRow(
        title="Data Engineer", company="Acme", apply_url=url, location="Remote",
        remote_policy="remote_eu", eligibility="confirmed", eligibility_evidence="",
        fit_signals=[], freshness="",
    )


def _times_seen(conn):
    return conn.execute("SELECT times_seen FROM jobs").fetchone()[0]


def test_record_occurrences_idempotent():
    with closing(ledger.connect()) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        first = ledger.record_occurrences(conn, "run-1", [_row()])
        assert list(first.values()) == [True]
        again = ledger.record_occurrences(conn, "run-1", [_row()])
        assert _times_seen(conn) == 1
        assert list(again.values()) == [True]  # identical result on repeat
        other = ledger.record_occurrences(conn, "run-2", [_row()])
        assert list(other.values()) == [False]
        assert _times_seen(conn) == 2


def test_record_run_upsert():
    now = datetime.now(UTC)
    summary = RunSummary(
        run_id="run-1", started_at=now, ended_at=now, ats_pages_opened=3,
        roles_kept=5, discard_counts={"x": 1}, full_query_log=[],
    )
    with closing(ledger.connect()) as conn:
        ledger.record_run(conn, "run-1", summary, "p", "fast")
        ledger.record_run(conn, "run-1", summary, "p", "fast")
        assert conn.execute("SELECT COUNT(*), roles_kept FROM runs").fetchone() == (1, 5)


def test_removal_restores_previous_details_and_preserves_review():
    from ultron import review

    with closing(ledger.connect()) as conn:
        row = _row()
        [key] = ledger.record_occurrences(conn, "run-1", [row], profile_slug="first")
        newer = row.model_copy(update={"title": "New title"})
        ledger.record_occurrences(conn, "run-2", [newer], profile_slug="second")
        review.set_state(key, "shortlisted")
        review.add_note(key, "Keep this note")
        review.set_feedback(key, "relevant")

        ledger.record_occurrences(conn, "run-2", [])
        assert conn.execute("SELECT title, profile_slug, times_seen, last_seen_run_id FROM jobs").fetchall() == [
            (row.title, "first", 1, "run-1"),
        ]
        ledger.record_occurrences(conn, "run-1", [])
        assert conn.execute("SELECT count(*) FROM job_occurrences").fetchone() == (0,)
        [job] = review.list_jobs(state="shortlisted")
        assert job["times_seen"] == 0
        assert "Keep this note" in job["notes"]
        assert job["feedback_relevance"] == "relevant"

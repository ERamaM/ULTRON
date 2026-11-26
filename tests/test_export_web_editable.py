"""Integration coverage for the opt-in loopback ledger API."""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest

from ultron.core.models import FinalRow, RunSummary
from ultron.exports import web_server
from ultron.profiles.store import EXAMPLE_SLUG
from ultron.storage import ledger, review


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("ULTRON_HOME", str(tmp_path / "other-home"))
    path = tmp_path / "selected.sqlite3"
    stamp = datetime(2026, 11, 22, 12, tzinfo=UTC)
    row = FinalRow(title="Data Engineer", company="Acme", apply_url="https://example.com/1", location="Madrid",
                   remote_policy="remote_eu", eligibility="high", eligibility_evidence="", fit_signals=[], freshness="")
    summary = RunSummary(run_id="sample", started_at=stamp, ended_at=stamp, ats_pages_opened=0,
                         roles_kept=1, discard_counts={}, full_query_log=[])
    with closing(ledger.connect(path)) as conn:
        ledger.record_finalized_run(conn, summary, [row], profile_slug=EXAMPLE_SLUG, preset="balanced")
    return path


@contextmanager
def _running(db, **kwargs):
    server, base, port = web_server.create_server(db_path=db, **kwargs)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.03}, daemon=True)
    thread.start()
    host = urlsplit(base).hostname
    try:
        yield host, port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _request(endpoint, method, path, body=None, headers=None):
    with closing(http.client.HTTPConnection(*endpoint, timeout=5)) as conn:
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        return response.status, response.headers, response.read()


def _session(endpoint):
    status, headers, root = _request(endpoint, "GET", "/")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    match = re.search(rb'<meta name="ultron-csrf" content="([A-Za-z0-9_-]{43})">', root)
    assert match
    return match.group(1).decode("ascii")


def _submit(endpoint, token, route, fields):
    status, _, raw = _request(endpoint, "POST", route, json.dumps(fields),
                              {"Content-Type": "application/json", "X-ULTRON-Token": token})
    assert status == 200, raw
    return json.loads(raw)


def test_read_only_keeps_file_unchanged(db):
    before = db.read_bytes()
    with _running(db) as endpoint:
        assert b"ultron-csrf" not in _request(endpoint, "GET", "/")[2]
        assert json.loads(_request(endpoint, "GET", "/api/ledger")[2])["meta"]["editable"] is False
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert _request(endpoint, method, "/api/jobs/state", "{}")[0] == 405
    assert db.read_bytes() == before


def test_editable_validates_transport_and_payload_before_writing(db):
    before = db.read_bytes()
    with _running(db, editable=True) as endpoint:
        token = _session(endpoint)
        route = "/api/jobs/state"
        body = '{"identity_key":"url://example.com/1","state":"interview"}'
        auth = {"Content-Type": "application/json", "X-ULTRON-Token": token}
        for payload, headers, status in (
            (body, {}, 403), (body, {**auth, "X-ULTRON-Token": "wrong"}, 403),
            (body, {**auth, "Host": "example.com"}, 403), (body, {**auth, "Origin": "https://example.com"}, 403),
            (body, {**auth, "Content-Type": "text/plain"}, 415), (" " * 16_385, auth, 413),
            ('{"state":"interview"}', auth, 400), (body[:-1] + ',"extra":"value"}', auth, 400),
            (body[:-1] + ',"state":"applied"}', auth, 400), (body.replace("interview", "invalid"), auth, 400),
            (body.replace("interview", r"\ud800"), auth, 400),
        ):
            assert _request(endpoint, "POST", route, payload, headers)[0] == status
        assert _request(endpoint, "GET", "/", headers={"Host": "example.com"})[0] == 403
        assert token.encode() not in _request(endpoint, "GET", "/index.html")[2]
        assert token.encode() not in _request(endpoint, "GET", "/api/ledger")[2]
        assert _request(endpoint, "PUT", route, body, auth)[0] == 405
        assert _request(endpoint, "POST", "/api/unknown", body, auth)[0] == 404
    assert db.read_bytes() == before


def test_authenticated_mutations_use_selected_ledger(db):
    identity = "url://example.com/1"
    with _running(db, editable=True) as endpoint:
        token = _session(endpoint)
        changed = _submit(endpoint, token, "/api/jobs/state", {"identity_key": identity, "state": "interview"})
        assert changed["job"]["review_state"] == "interview"
        assert changed["state_counts"]["interview"] == 1
        note = _submit(endpoint, token, "/api/jobs/notes", {"identity_key": identity, "text": "Contacted recruiter."})
        assert "Contacted recruiter." in note["notes"]
        feedback = _submit(endpoint, token, "/api/jobs/feedback", {
            "identity_key": identity, "relevance": "relevant", "reason_code": "other", "profile": EXAMPLE_SLUG,
        })
        assert feedback["profile"] == EXAMPLE_SLUG
        feedback = _submit(endpoint, token, "/api/jobs/feedback", {
            "identity_key": identity, "relevance": "uncertain", "profile": EXAMPLE_SLUG, "note": "Prior evidence",
        })
        assert feedback["reason_code"] == "other"
        feedback = _submit(endpoint, token, "/api/jobs/feedback", {
            "identity_key": identity, "relevance": "relevant", "profile": EXAMPLE_SLUG,
        })
        assert feedback["reason_code"] == "other" and feedback["note"] == "Prior evidence"
        board = _submit(endpoint, token, "/api/watchlist/add", {"profile": EXAMPLE_SLUG, "board": "greenhouse:acme"})
        assert board["seed_key"] == "greenhouse:acme" and board["state"] == "watch"
        assert _submit(endpoint, token, "/api/watchlist/remove", {
            "profile": EXAMPLE_SLUG, "seed_key": board["seed_key"],
        }) == {"removed": "greenhouse:acme"}
        snapshot = json.loads(_request(endpoint, "GET", "/api/ledger")[2])
        assert snapshot["meta"]["editable"] and snapshot["meta"]["schema_version"] == 9
        [job] = snapshot["jobs"]
        assert job["review_state"] == "interview" and job["feedback_relevance"] == "relevant"
        assert "Contacted recruiter." in job["notes"]
        assert snapshot["watchlist"][0]["state"] == "blocked"
    assert not (db.parent / "other-home" / "ultron.sqlite3").exists()


def test_assets_and_session_tokens(db):
    with _running(db, editable=True) as first, _running(db, editable=True) as second:
        assert _session(first) != _session(second)
        for name in ("app.js", "views.js", "kanban.js", "actions.js", "charts.js", "dom.js",
                     "history.js", "app.css", "views.css", "kanban.css"):
            status, headers, _ = _request(first, "GET", "/assets/" + name)
            assert status == 200
            assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_migration_preserves_v8_data_and_references(tmp_path):
    path = tmp_path / "state.sqlite3"
    with closing(sqlite3.connect(path)) as conn, conn:
        for migration in (ledger._create_v1, ledger._apply_v2, ledger._apply_v3, ledger._apply_v4,
                          ledger._apply_v5, ledger._apply_v6, ledger._apply_search_scope, ledger._apply_v8):
            migration(conn)
        conn.execute("PRAGMA user_version=8")
        conn.execute("CREATE INDEX jobs_company ON jobs(company)")
        conn.execute(
            "INSERT INTO jobs (identity_key, canonical_url, first_seen, last_seen, review_state, published_at)"
            " VALUES ('k', 'https://example.com/1', '2026-11-01', '2026-11-02', 'applied', '2026-11-01')"
        )
        conn.execute("INSERT INTO job_occurrences(identity_key, run_id) VALUES ('k', 'r1')")
        conn.execute("INSERT INTO profile_job_feedback VALUES ('k', 'p1', 'relevant', '', 'note', '2026-11-02')")
        conn.execute("INSERT INTO watchlist (profile_slug, seed_key, added_at) VALUES ('p1', 'lever:acme', '2026-11-02')")
        conn.execute("INSERT INTO scout_verdicts (profile_slug, identity_key, verdict, run_id, recorded_at)"
                     " VALUES ('p1', 'k', 'keep', 'r1', '2026-11-02')")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        assert ledger.migrate(conn) == 9
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT published_at FROM jobs WHERE identity_key='k'").fetchone() == ("2026-11-01",)
        assert {row[1] for row in conn.execute("PRAGMA index_list(jobs)")} >= {"jobs_company"}
        for table in ("job_occurrences", "profile_job_feedback", "watchlist", "scout_verdicts"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (1,)
    [backup] = (tmp_path / "backups").glob("*-pre-v9.sqlite3")
    with closing(sqlite3.connect(backup)) as saved:
        assert saved.execute("PRAGMA user_version").fetchone() == (8,)
        assert saved.execute("SELECT review_state FROM jobs").fetchone() == ("applied",)
    assert review.set_state("k", "interview", db_path=path) == "k"

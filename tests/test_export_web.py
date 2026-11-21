"""Tests for export sanitization and the local read-only SQLite web viewer."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ultron
from ultron.cli import main
from ultron.core.models import FinalRow, RunSummary
from ultron.exports import sanitize, web_data, web_server
from ultron.profiles import store as profiles
from ultron.storage import ledger, review, saved

_SYNTHETIC_SUFFIX = "1234567890abcdef" * 2 + "1234"
_SYNTHETIC_GH_TOKEN = "ghp_" + _SYNTHETIC_SUFFIX
_SYNTHETIC_GH_ZWS = "gh\u200bp_" + _SYNTHETIC_SUFFIX


def _seed_full_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ULTRON_HOME", str(tmp_path))
    profiles.install_example()
    slug = profiles.EXAMPLE_SLUG
    db_path = tmp_path / "ultron.sqlite3"
    now = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    summary_1 = RunSummary(
        run_id="20261121-100000-aaaaaa",
        started_at=now,
        ended_at=now,
        ats_pages_opened=12,
        roles_kept=2,
        discard_counts={"seniority": 3, "geo_non_eu": 1},
        full_query_log=[],
    )
    summary_2 = RunSummary(
        run_id="20261121-110000-bbbbbb",
        started_at=now,
        ended_at=now,
        ats_pages_opened=8,
        roles_kept=1,
        discard_counts={"role_family": 2},
        full_query_log=[],
    )
    row_1 = FinalRow(
        title="Backend Engineer <script>alert(1)</script>",
        company="Acme Corp",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        location="Remote EU",
        remote_policy="remote_eu",
        eligibility="confirmed",
        eligibility_evidence="Remote EU",
        fit_signals=["python"],
        freshness="2026-11-21",
        source_type="greenhouse",
        source_key="greenhouse:acme",
        match_score=91,
        match_band="strong",
    )
    row_2 = FinalRow(
        title="Platform Engineer",
        company="Beta Labs",
        apply_url="https://jobs.lever.co/beta/2",
        location="Madrid",
        remote_policy="remote_spain",
        eligibility="high",
        eligibility_evidence="Madrid",
        fit_signals=["k8s"],
        freshness="2026-11-20",
        source_type="lever",
        source_key="lever:beta",
        match_score=74,
        match_band="good",
    )

    with closing(ledger.connect()) as conn:
        ledger.record_finalized_run(
            conn,
            summary_1,
            [row_1, row_2],
            profile_slug=slug,
            preset="balanced",
            seed_kept={"greenhouse:acme": 1, "lever:beta": 1},
        )
        ledger.record_finalized_run(
            conn,
            summary_2,
            [row_1],
            profile_slug=slug,
            preset="fast",
            seed_kept={"greenhouse:acme": 1, "lever:beta": 0},
        )
        conn.execute(
            "INSERT INTO diagnostics (submitted_url, identity_key, first_failed_stage, reason_code, trace_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                "https://boards.greenhouse.io/acme/jobs/99",
                "url://boards.greenhouse.io/acme/jobs/99",
                "filter",
                "geo_non_eu",
                json.dumps({"detail": f"secret {_SYNTHETIC_GH_TOKEN}"}),
                now.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO diagnostics (submitted_url, identity_key, first_failed_stage, reason_code, trace_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                "javascript:alert(1)",
                "bad-url",
                "fetch",
                "invalid_scheme",
                "{bad-json",
                now.isoformat(),
            ),
        )
        conn.commit()

    review.set_state("https://boards.greenhouse.io/acme/jobs/1", "shortlisted")
    review.add_note(
        "https://boards.greenhouse.io/acme/jobs/1",
        f"Line 1 token {_SYNTHETIC_GH_TOKEN}\nLine 2 ok",
    )
    review.set_feedback("https://jobs.lever.co/beta/2", "relevant", reason_code="other", note="Good fit")
    saved.create("daily-backend", slug, preset="balanced", prompt="Daily scan")
    assert db_path.is_file()
    return db_path


def test_sanitize_primitives():
    assert sanitize.clean_text(None) == ""
    assert sanitize.clean_text(["a", None, "b\r\nc"]) == "a, b  c"
    redacted = sanitize.clean_text(f"tok {_SYNTHETIC_GH_ZWS}")
    assert "1234567890abcdef" not in redacted
    multi = sanitize.clean_multiline_text("line1\x00\r\nline2")
    assert multi == "line1\nline2"
    assert sanitize.clean_multiline_text(None) == ""

    assert sanitize.safe_http_url("https://example.com/a") == "https://example.com/a"
    assert sanitize.safe_http_url("http://example.com") == "http://example.com"
    assert sanitize.safe_http_url("javascript:alert(1)") is None
    assert sanitize.safe_http_url("file:///etc/passwd") is None
    assert sanitize.safe_http_url("https://example.com/bad url") is None
    assert sanitize.safe_http_url("http://[invalid") is None
    assert sanitize.safe_http_url(123) is None

    assert sanitize.clamp_score(88.6) == 89
    assert sanitize.clamp_score(140) == 100
    assert sanitize.clamp_score(-5) == 0
    assert sanitize.clamp_score(True) is None
    assert sanitize.clamp_score(float("nan")) is None
    assert sanitize.normalize_token(" STRONG ", ("strong", "good"), default="unscored") == "strong"
    assert sanitize.normalize_token("bad", ("strong", "good"), default="unscored") == "unscored"


def test_build_ledger_snapshot_missing_db_does_not_create_file(tmp_path: Path):
    missing = tmp_path / "ultron.sqlite3"
    snap = web_data.build_ledger_snapshot(missing)
    assert not missing.exists()
    assert snap["meta"]["db_exists"] is False
    assert snap["summary"]["total_jobs"] == 0
    assert snap["jobs"] == []
    assert snap["runs"] == []


def test_build_ledger_snapshot_full_db_and_redaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = _seed_full_ledger(tmp_path, monkeypatch)
    snap = web_data.build_ledger_snapshot(db_path)

    assert snap["meta"]["db_exists"] is True
    assert snap["meta"]["schema_version"] == ledger.SCHEMA_VERSION
    assert snap["meta"]["db_name"] == "ultron.sqlite3"
    assert str(tmp_path) not in json.dumps(snap)

    summary = snap["summary"]
    assert summary["total_jobs"] == 2
    assert summary["new_jobs"] == 1
    assert summary["avg_score"] == 82.5
    assert summary["band_counts"]["strong"] == 1
    assert summary["band_counts"]["good"] == 1
    assert summary["state_counts"]["shortlisted"] == 1
    assert summary["state_counts"]["unreviewed"] == 1
    assert summary["feedback_counts"]["relevant"] == 1
    assert summary["total_runs"] == 2
    assert summary["seed_count"] == 2
    assert summary["saved_search_count"] == 1
    assert summary["diagnostic_count"] == 2

    by_company = {j["company"]: j for j in snap["jobs"]}
    acme = by_company["Acme Corp"]
    assert acme["review_state"] == "shortlisted"
    assert acme["times_seen"] == 2
    assert len(acme["occurrences"]) == 2
    assert "<script>alert(1)</script>" in acme["title"]
    assert "1234567890abcdef" not in acme["notes"]
    assert "\nLine 2 ok" in acme["notes"]

    diags = snap["diagnostics"]
    assert diags[0]["submitted_url"] is None
    assert "1234567890abcdef" not in json.dumps(diags[1])


def test_build_ledger_snapshot_tolerates_minimal_v1_schema(tmp_path: Path):
    db_path = tmp_path / "minimal.sqlite3"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "CREATE TABLE jobs (identity_key TEXT PRIMARY KEY, canonical_url TEXT, title TEXT, company TEXT,"
            " first_seen TEXT, last_seen TEXT, times_seen INTEGER, review_state TEXT)"
        )
        conn.execute(
            "INSERT INTO jobs VALUES ('k1', 'https://example.com/1', 'Role', 'Co', '2026-01-01', '2026-01-02', 1, 'applied')"
        )
        conn.commit()
    snap = web_data.build_ledger_snapshot(db_path)
    assert snap["meta"]["db_exists"] is True
    assert snap["summary"]["total_jobs"] == 1
    assert snap["jobs"][0]["review_state"] == "applied"
    assert snap["runs"] == []


def test_web_server_routes_headers_methods_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="loopback host"):
        web_server.validate_bind_target("0.0.0.0", 8000)
    with pytest.raises(ValueError, match="loopback host"):
        web_server.validate_bind_target("192.168.1.5", 8000)
    with pytest.raises(ValueError, match="port"):
        web_server.validate_bind_target("127.0.0.1", 70000)
    with pytest.raises(ValueError, match="port"):
        web_server.validate_bind_target("127.0.0.1", True)  # type: ignore[arg-type]

    assert web_server.build_dashboard_url("http://127.0.0.1:9000/", run_id="r 1", profile="p1") == (
        "http://127.0.0.1:9000/#run=r%201&profile=p1"
    )
    assert web_server.build_dashboard_url("http://127.0.0.1:9000/") == "http://127.0.0.1:9000/"

    db_path = _seed_full_ledger(tmp_path, monkeypatch)
    server, base_url, _port = web_server.create_server(host="127.0.0.1", port=0, db_path=db_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for route, expected_type in (
            ("", "text/html"),
            ("assets/app.css", "text/css"),
            ("assets/app.js", "application/javascript"),
            ("api/health", "application/json"),
            ("api/ledger", "application/json"),
        ):
            with urllib.request.urlopen(base_url + route, timeout=5) as resp:
                assert resp.status == 200
                assert expected_type in resp.headers.get("Content-Type", "")
                for header_name, _ in web_server.SECURITY_HEADERS:
                    assert resp.headers.get(header_name) is not None
                payload = resp.read().decode("utf-8")
                if route == "api/ledger":
                    parsed = json.loads(payload)
                    assert parsed["summary"]["total_jobs"] == 2
                elif route == "api/health":
                    assert json.loads(payload) == {"status": "ok"}

        head_req = urllib.request.Request(base_url + "api/ledger", method="HEAD")
        with urllib.request.urlopen(head_req, timeout=5) as head_resp:
            assert head_resp.status == 200
            assert head_resp.read() == b""

        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            bad_method = urllib.request.Request(base_url + "api/ledger", method=method)
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(bad_method, timeout=5)
            assert exc.value.code == 405
            assert exc.value.headers.get("Allow") == "GET, HEAD"

        for missing_route in ("does-not-exist", "assets/../../pyproject.toml"):
            with pytest.raises(urllib.error.HTTPError) as exc_404:
                urllib.request.urlopen(base_url + missing_route, timeout=5)
            assert exc_404.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_static_web_assets_disallow_unsafe_dom_apis():
    static_dir = Path(ultron.__file__).resolve().parent / "exports" / "web_static"
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    assert '<html lang="en">' in index_html
    assert "http://" not in index_html and "https://" not in index_html
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in app_js


def test_cli_export_web_json_and_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    db_path = _seed_full_ledger(tmp_path, monkeypatch)
    assert db_path.is_file()
    monkeypatch.setattr(web_server.LedgerHTTPServer, "serve_forever", lambda self: None)
    opened: list[str] = []
    monkeypatch.setattr(web_server.webbrowser, "open", lambda url: opened.append(url) or True)

    code = main(["export", "web", "--run", "20261121-100000-aaaaaa", "--profile", "backend-eu"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["host"] == "127.0.0.1"
    assert out["port"] > 0
    assert out["db_exists"] is True
    assert out["opened_browser"] is True
    assert "#run=20261121-100000-aaaaaa&profile=backend-eu" in out["url"]
    assert opened == [out["url"]]

    code_no_open = main(["export", "web", "--no-open"])
    assert code_no_open == 0
    out_no_open = json.loads(capsys.readouterr().out)
    assert out_no_open["opened_browser"] is False

    code_bad = main(["export", "web", "--host", "0.0.0.0", "--no-open"])
    assert code_bad == 2
    err = json.loads(capsys.readouterr().out)
    assert err["error"] == "export_failed"

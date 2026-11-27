from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from ultron.cli import main
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import CandidateUrl
from ultron.diagnostics import diagnose
from ultron.infra import home
from ultron.profiles import store as profiles
from ultron.stages import http_factory
from ultron.storage import runs

PAGE = (Path(__file__).resolve().parent / "fixtures" / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")
URL = "https://boards.greenhouse.io/acme/jobs/1"


def _ident(url=URL):
    return canonical_job_identity(url, source_type="ats_api", source_key="greenhouse:acme").identity_key


def _cand(url=URL):
    return CandidateUrl(query_id="greenhouse:acme", query="", source_url=url, normalized_url=url,
                        domain="boards.greenhouse.io", source_type="ats_api", source_key="greenhouse:acme")


@pytest.fixture
def run_dir():
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    runs.mark_stage(run_id, "fetch")
    return run_id, runs.run_dir(run_id)


def _diag(run_id, url=URL, **kw):
    return diagnose.diagnose(url, profile_slug=profiles.EXAMPLE_SLUG, run_id=run_id, fetch=False, **kw)


def test_not_discovered(run_dir):
    run_id, d = run_dir
    runs.write_jsonl(d / "candidates.jsonl", [_cand("https://boards.greenhouse.io/acme/jobs/99")])
    result = _diag(run_id)
    assert result.first_failed_stage == "not_discovered" and result.run_id == run_id


def test_fetch_failed(run_dir):
    run_id, d = run_dir
    runs.write_jsonl(d / "candidates.jsonl", [_cand()])
    runs.write_jsonl(d / "fetch_errors.jsonl", [{"url": URL, "error": "expired_or_closed", "status": 404}])
    result = _diag(run_id)
    assert result.first_failed_stage == "fetch_failed" and result.reason_code == "expired_or_closed"


def test_extraction_failed(run_dir):
    run_id, d = run_dir
    runs.write_jsonl(d / "candidates.jsonl", [_cand()])
    runs.write_jsonl(d / "fetch_errors.jsonl", [{"url": URL, "error": "extract_failed: KeyError('x')"}])
    assert _diag(run_id).first_failed_stage == "extraction_failed"


def test_candidate_cap(run_dir):
    run_id, d = run_dir
    runs.write_jsonl(d / "candidates.jsonl", [_cand()])
    runs.write_jsonl(d / "postings.jsonl", [])
    runs.mark_stage(run_id, "fetch")
    assert _diag(run_id).first_failed_stage == "candidate_cap"


@pytest.mark.parametrize(("stage", "expected"), [
    ("pre_filter", "prefilter"), ("heuristic_evaluator", "evaluator"), ("hard_filter", "hard_filter"),
])
def test_filter_stages(run_dir, stage, expected):
    run_id, d = run_dir
    runs.write_jsonl(d / "candidates.jsonl", [_cand()])
    runs.write_jsonl(d / "postings.jsonl", [{"identity_key": _ident(), "job": {}, "candidate": {}}])
    runs.write_jsonl(d / "discarded.jsonl", [{"identity_key": _ident(), "stage": stage, "reason": "why"}])
    result = _diag(run_id)
    assert result.first_failed_stage == expected and result.reason_code == "why"


@pytest.mark.parametrize(("stage", "expected"), [
    ("final_cap", "final_cap"), ("dedup", "dedup"), ("post_eval_geo", "hard_filter"), ("evaluator", "evaluator"),
])
def test_finalize_stages(run_dir, stage, expected):
    run_id, d = run_dir
    runs.write_jsonl(d / "candidates.jsonl", [_cand()])
    runs.write_jsonl(d / "postings.jsonl", [{"identity_key": _ident(), "job": {}, "candidate": {}}])
    runs.write_jsonl(d / "discarded.jsonl", [])
    runs.write_jsonl(d / "finalize_discards.jsonl", [{"identity_key": _ident(), "stage": stage, "reason": "r"}])
    result = _diag(run_id)
    assert result.first_failed_stage == expected and result.reason_code == "r"


def test_kept(run_dir):
    run_id, d = run_dir
    runs.write_jsonl(d / "candidates.jsonl", [_cand()])
    runs.write_jsonl(d / "postings.jsonl", [{"identity_key": _ident(), "job": {}, "candidate": {}}])
    (d / "results.json").write_text(json.dumps({"rows": [{"apply_url": URL, "source_type": "ats_api",
                                                          "source_key": "greenhouse:acme"}]}), encoding="utf-8")
    assert _diag(run_id).first_failed_stage == "kept"


def test_tracking_params_ignored(run_dir):
    run_id, d = run_dir
    plain = "https://careers.example.org/jobs/senior-ml-engineer?id=42"
    cand = CandidateUrl(query_id="q", query="", source_url=plain, normalized_url=plain, domain="careers.example.org")
    runs.write_jsonl(d / "candidates.jsonl", [cand])
    runs.write_jsonl(d / "postings.jsonl", [])
    runs.mark_stage(run_id, "fetch")
    tracked = plain + "&utm_source=x&gh_src=y&ref=z"
    result = _diag(run_id, url=tracked)
    assert result.first_failed_stage == "candidate_cap"
    assert result.identity_key == canonical_job_identity(plain).identity_key


def test_pending_when_fetch_never_ran(run_dir):
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")  # fetch not done
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", [_cand()])
    result = _diag(run_id)
    assert (result.first_failed_stage, result.reason_code) == ("pending", "not_fetched")


def test_pending_when_not_finalized(run_dir):
    run_id, d = run_dir
    runs.write_jsonl(d / "candidates.jsonl", [_cand()])
    runs.write_jsonl(d / "postings.jsonl", [{"identity_key": _ident(), "job": {}, "candidate": {}}])
    runs.mark_stage(run_id, "fetch")
    runs.write_jsonl(d / "kept.jsonl", [{"identity_key": _ident()}])
    assert (_diag(run_id).first_failed_stage, _diag(run_id).reason_code) == ("pending", "not_finalized")
    runs.write_jsonl(d / "ambiguous.jsonl", [{"identity_key": _ident()}])
    assert _diag(run_id).reason_code == "pending_evaluation"


def test_latest_run_default(run_dir):
    run_id, d = run_dir
    runs.write_jsonl(d / "candidates.jsonl", [_cand()])
    runs.write_jsonl(d / "postings.jsonl", [])
    result = diagnose.diagnose(URL, profile_slug=profiles.EXAMPLE_SLUG, fetch=False)
    assert result.run_id == run_id


def test_offline_replay_when_no_run(capsys):
    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=PAGE)),
        resolver=lambda h, p: ["93.184.216.34"], sleep=lambda s: None, min_interval_per_host=0.0,
    )
    result = diagnose.diagnose(URL, profile_slug=profiles.EXAMPLE_SLUG)
    stages = {t["stage"] for t in result.trace if t["label"] == "offline_replay"}
    assert {"pre_filter", "heuristic_evaluator", "hard_filter"} <= stages
    assert result.run_id is None
    db = sqlite3.connect(home.ultron_home() / "ultron.sqlite3")
    assert db.execute("SELECT first_failed_stage FROM diagnostics").fetchone()[0] == result.first_failed_stage
    db.close()


def test_cli_no_fetch(run_dir, capsys):
    run_id, d = run_dir
    runs.write_jsonl(d / "candidates.jsonl", [_cand()])
    runs.write_jsonl(d / "postings.jsonl", [])
    assert main(["diagnose", URL, "--profile", profiles.EXAMPLE_SLUG, "--run", run_id, "--no-fetch"]) == 0
    assert json.loads(capsys.readouterr().out)["first_failed_stage"] == "candidate_cap"
    assert main(["diagnose", URL, "--profile", profiles.EXAMPLE_SLUG, "--run", "nope"]) != 0


@pytest.mark.parametrize(("page", "allowed"), [
    ("<html><script>window.greenhouse = {}</script><h1>Senior ML Engineer</h1></html>", True),
    ("<html><h1>Senior ML Engineer</h1><p>Send us an email.</p></html>", False),
])
def test_offline_replay_uses_ats_fingerprint_for_custom_domains(page, allowed):
    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)),
        resolver=lambda h, p: ["93.184.216.34"], sleep=lambda s: None, min_interval_per_host=0.0,
    )
    result = diagnose.diagnose("https://careers.example-corp.com/jobs/1", profile_slug=profiles.EXAMPLE_SLUG)
    pre = next(t for t in result.trace if t["label"] == "offline_replay" and t["stage"] == "pre_filter")
    assert (pre["reason"] != "outside_ats_allowlist") is allowed


def test_kept_after_fetch_switched_to_the_ats_canonical_url(run_dir):
    run_id, d = run_dir
    custom = "https://careers.example.com/jobs/senior-ml-engineer"
    cand = CandidateUrl(query_id="web:1", query="", source_url=custom, normalized_url=custom,
                        domain="careers.example.com", source_type="web_search", source_key="web_search")
    original = canonical_job_identity(custom, source_type="web_search", source_key="web_search").identity_key
    runs.write_jsonl(d / "candidates.jsonl", [cand])
    runs.write_jsonl(d / "postings.jsonl", [{"identity_key": original, "job": {"apply_url": URL}, "candidate": {}}])
    (d / "results.json").write_text(json.dumps({"rows": [{"apply_url": URL, "source_type": "web_search",
                                                          "source_key": "web_search"}]}), encoding="utf-8")
    assert _diag(run_id, url=custom).first_failed_stage == "kept"  # the original URL
    assert _diag(run_id, url=URL).first_failed_stage == "kept"  # the canonical URL fetch switched to


def test_diagnose_saved_shortlist_reports_kept_and_final_cap(monkeypatch):
    from ultron.core import presets
    from ultron.toolkit.shortlist import save_shortlist

    profiles.install_example()
    resolve = presets.resolve
    monkeypatch.setattr(presets, "resolve", lambda preset: {**resolve(preset), "max_roles": 1})
    urls = [URL, URL.rsplit("/", 1)[0] + "/2"]
    result = save_shortlist(profiles.EXAMPLE_SLUG, [
        {"url": url, "title": title, "company": "Acme", "verdict": "keep", "priority": priority}
        for priority, (url, title) in enumerate(zip(urls, ("Backend Engineer", "Data Scientist"), strict=True), 1)
    ])
    assert [_diag(result["run_id"], url).first_failed_stage for url in urls] == ["kept", "final_cap"]

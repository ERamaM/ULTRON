"""The agent toolkit: ``inspect`` dossiers and ``shortlist save`` (specification 31), with a mocked network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from toolkit_helpers import AGGREGATOR, DEAD_AGGREGATOR, EMPLOYER, POSTING, handler
from toolkit_helpers import EMPLOYER_PAGE as EMPLOYER_PAGE_HTML

from ultron import seeds
from ultron.cli import main
from ultron.profiles import store as profiles
from ultron.stages import http_factory
from ultron.toolkit import dossier, inspect, shortlist

SLUG = profiles.EXAMPLE_SLUG
GONE = "https://careers.example.org/jobs/closed-role"
BOARD = "https://boards.greenhouse.io/acme"


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: empty)
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    profiles.install_example()


def _by_url(result: dict) -> dict[str, dict]:
    return {d.get("found_at") or d.get("url"): d for d in result["dossiers"] if d.get("url")}


def test_inspect_reads_every_kind_of_url():
    result = inspect.inspect_jobs(SLUG, [POSTING, EMPLOYER, AGGREGATOR, DEAD_AGGREGATOR, GONE, "ftp://example.org/x"])
    found = _by_url(result)
    assert found[POSTING]["status"] == "open" and found[POSTING]["id"] == "greenhouse:acme:4012345"
    assert found[POSTING]["memory"] == "new" and found[POSTING]["hint"]["verdict"] in {"keep", "unsure", "discard"}
    assert len(found[POSTING]["excerpt"]) <= dossier.SHORT_EXCERPT
    assert found[EMPLOYER]["status"] == "open" and found[EMPLOYER]["company"]
    assert found[AGGREGATOR]["url"] == POSTING  # followed to the employer's posting
    assert found[DEAD_AGGREGATOR]["status"] == "unresolved_aggregator"
    assert found[GONE]["status"] == "closed"
    assert found["ftp://example.org/x"]["status"] == "rejected"
    assert result["dossiers"][0]["status"] == "open" and result["requests"] > 0
    cached = inspect.cached_postings(SLUG)
    assert cached["greenhouse:acme:4012345"]["job"]["title"]


def test_inspect_detail_gives_a_longer_excerpt():
    short = _by_url(inspect.inspect_jobs(SLUG, [EMPLOYER]))[EMPLOYER]["excerpt"]
    long = _by_url(inspect.inspect_jobs(SLUG, [EMPLOYER], detail=True))[EMPLOYER]["excerpt"]
    assert len(long) >= len(short) and len(long) <= dossier.DETAIL_EXCERPT


def test_inspect_lists_matching_postings_of_boards_and_companies():
    result = inspect.inspect_jobs(SLUG, [BOARD], ["Acme"])
    listed = [d for d in result["dossiers"] if d["status"] == "listed"]
    assert listed and all(d["board"] == "greenhouse:acme" for d in listed)
    assert all(d["memory"] == "new" for d in listed)
    company = [d for d in listed if d.get("board_confirmed") is False]
    assert company and company[0]["company"] == "Acme"  # a guessed board that answered, not proven Acme's


def test_inspect_reports_a_company_without_a_board():
    result = inspect.inspect_jobs(SLUG, [], ["Nonexistent Widgets"])
    assert result["dossiers"][0]["status"] == "no_board"


def test_board_urls_are_told_from_postings():
    assert inspect.is_board_url(BOARD) and not inspect.is_board_url(POSTING)
    assert not inspect.is_board_url(EMPLOYER)


def test_save_shortlist_from_inspected_ids(capsys):
    inspect.inspect_jobs(SLUG, [POSTING])
    rows = [
        {"id": "greenhouse:acme:4012345", "verdict": "keep", "priority": 1, "remote_policy": "remote_spain"},
        {"url": EMPLOYER, "verdict": "unsure", "title": "Senior ML Engineer", "company": "Example Org", "priority": 2},
        {"url": GONE, "verdict": "discard", "discard_reason": "closed"},
        {"url": POSTING + "9", "verdict": "keep"},  # no title or company: invalid
    ]
    out = shortlist.save_shortlist(SLUG, rows)
    assert out["saved"] == {"keep": 1, "unsure": 1, "discard": 1}
    assert [row["outcome"] for row in out["not_saved"]] == ["invalid"]
    results = json.loads(Path(out["outputs"]["results_json"]).read_text(encoding="utf-8"))["rows"]
    assert [r["found_by"] for r in results] == ["agent_verified"] * 2
    assert results[0]["title"] and results[0]["company"] and results[0]["priority"] == 1  # filled from the cache
    assert main(["watchlist", "list", "--profile", SLUG]) == 0
    watched = json.loads(capsys.readouterr().out)["watched"]
    assert [w["seed_key"] for w in watched] == ["greenhouse:acme"]

    again = inspect.inspect_jobs(SLUG, [POSTING, GONE])
    flags = {d["url"]: d.get("memory") for d in again["dossiers"]}
    assert flags[POSTING] == "seen"


def test_save_shortlist_needs_rows():
    with pytest.raises(shortlist.EmptyShortlist):
        shortlist.save_shortlist(SLUG, [])


def test_cli_inspect_and_save(tmp_path, capsys):
    source = tmp_path / "urls.txt"
    source.write_text(f"# finds\n{POSTING}\n" + json.dumps({"company": "Acme"}) + "\n", encoding="utf-8")
    assert main(["inspect", "--profile", SLUG, "--from", str(source)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"].get("open") == 1 and out["status"].get("listed")
    rows = tmp_path / "shortlist.jsonl"
    rows.write_text(json.dumps({"id": "greenhouse:acme:4012345", "verdict": "keep"}) + "\n", encoding="utf-8")
    assert main(["shortlist", "save", "--profile", SLUG, "--from", str(rows)]) == 0
    assert json.loads(capsys.readouterr().out)["roles"] == 1


@pytest.mark.parametrize("args, error", [
    (["inspect", "--profile", SLUG], "usage"),
    (["inspect", "--profile", "missing-profile", "--url", POSTING], "profile_not_found"),
    (["inspect", "--profile", SLUG, "--url", POSTING, "--preset", "nope"], "unknown_preset"),
    (["inspect", "--profile", SLUG, "--from", "missing-file.txt"], "invalid_input"),
])
def test_cli_inspect_errors(capsys, args, error):
    assert main(args) != 0
    assert json.loads(capsys.readouterr().out)["error"] == error


def test_cli_save_errors(tmp_path, capsys):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert main(["shortlist", "save", "--profile", SLUG, "--from", str(empty)]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "usage"
    assert main(["shortlist", "save", "--profile", SLUG, "--from", str(tmp_path / "none.jsonl")]) != 0




def test_board_homes_are_told_from_slug_only_postings():
    assert inspect.is_board_url("https://acme.homerun.co/") and inspect.is_board_url("https://acme.homerun.co")
    assert not inspect.is_board_url("https://acme.homerun.co/senior-engineer")
    assert inspect.is_board_url("https://jobs.lever.co/acme") and inspect.is_board_url("https://boards.greenhouse.io/acme/")


def test_redirected_postings_use_the_identity_saving_records():
    moved = "https://careers.example.org/jobs/moved"

    def redirecting(request):
        if str(request.url) == moved:
            return httpx.Response(302, headers={"Location": POSTING})
        return handler(request)
    http_factory.set_test_overrides(transport=httpx.MockTransport(redirecting), resolver=lambda h, p: ["93.184.216.34"],
                                    sleep=lambda s: None, min_interval_per_host=0.0)
    (found,) = inspect.inspect_jobs(SLUG, [moved])["dossiers"]
    assert found["id"] == "greenhouse:acme:4012345" and found["found_at"] == moved
    shortlist.save_shortlist(SLUG, [{"id": found["id"], "verdict": "discard"}])
    (again,) = inspect.inspect_jobs(SLUG, [moved])["dossiers"]
    assert again["memory"] == "rejected"


def test_cache_survives_unicode_line_separators():
    folder = inspect.home.ultron_home() / inspect.CACHE_DIR / SLUG
    folder.mkdir(parents=True)
    row = {"identity_key": "k", "status": "open", "job": {"title": "A B\u0085C", "apply_url": EMPLOYER}}
    (folder / inspect.CACHE_FILE).write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    assert inspect.cached_postings(SLUG)["k"]["job"]["title"] == "A B\u0085C"


def test_rows_over_the_cap_are_reported(monkeypatch):
    monkeypatch.setattr(shortlist, "MAX_ROWS", 1)
    out = shortlist.save_shortlist(SLUG, [
        {"url": EMPLOYER, "verdict": "keep", "title": "ML", "company": "Example Org"},
        {"url": POSTING, "verdict": "keep", "title": "DS", "company": "Acme"},
    ])
    assert out["saved"]["keep"] == 1 and out["not_saved"] == [
        {"row": 2, "outcome": "over_limit", "reason": "only the first 1 rows are saved per call"}]


def test_vendor_board_homes_are_boards():
    assert inspect.is_board_url("https://api.occupop.com/api/jobs-frame/abc123token")
    assert inspect.is_board_url("https://acme.taleo.net/careersection/2/jobsearch.ftl")


def _serve(pages: dict[str, str]):
    def serving(request):
        if str(request.url) in pages:
            return httpx.Response(200, text=pages[str(request.url)])
        return handler(request)
    http_factory.set_test_overrides(transport=httpx.MockTransport(serving), resolver=lambda h, p: ["93.184.216.34"],
                                    sleep=lambda s: None, min_interval_per_host=0.0)


def test_a_page_canonical_never_claims_another_postings_identity():
    spoof = "https://careers.example.org/jobs/spoof"
    page = EMPLOYER_PAGE_HTML.replace("<head>", f'<head><link rel="canonical" href="{POSTING}">')
    _serve({spoof: page})
    (found,) = inspect.inspect_jobs(SLUG, [spoof])["dossiers"]
    assert found["id"] == "url://careers.example.org/jobs/spoof" and found["url"] == spoof
    shortlist.save_shortlist(SLUG, [{"id": found["id"], "verdict": "discard"}])
    legit = inspect.inspect_jobs(SLUG, [POSTING])["dossiers"][0]
    assert legit["id"] == "greenhouse:acme:4012345" and legit["memory"] == "new"


def test_a_board_posting_on_a_custom_domain_keeps_its_board_identity():
    custom = "https://jobs.example.org/o/senior-ml-engineer"
    folder = inspect.home.ultron_home() / inspect.CACHE_DIR / SLUG
    folder.mkdir(parents=True)
    listed = {"identity_key": "recruitee:exampleorg:senior-ml-engineer", "status": "listed", "url": custom,
              "source_type": "ats_api", "source_key": "recruitee:exampleorg",
              "job": {"apply_url": custom, "title": "Senior ML Engineer", "company": "Example Org", "location": ""}}
    (folder / inspect.CACHE_FILE).write_text(json.dumps(listed) + "\n", encoding="utf-8")
    _serve({custom: EMPLOYER_PAGE_HTML})
    (found,) = inspect.inspect_jobs(SLUG, [custom])["dossiers"]
    assert found["id"] == listed["identity_key"]
    out = shortlist.save_shortlist(SLUG, [{"id": found["id"], "verdict": "discard"}])
    assert out["saved"]["discard"] == 1
    assert inspect.inspect_jobs(SLUG, [custom])["dossiers"][0]["memory"] == "rejected"


def test_a_kept_custom_domain_posting_records_its_board_identity(capsys):
    custom = "https://jobs.example.org/o/senior-ml-engineer"
    folder = inspect.home.ultron_home() / inspect.CACHE_DIR / SLUG
    folder.mkdir(parents=True)
    listed = {"identity_key": "recruitee:exampleorg:senior-ml-engineer", "status": "listed", "url": custom,
              "source_type": "ats_api", "source_key": "recruitee:exampleorg",
              "job": {"apply_url": custom, "title": "Senior ML Engineer", "company": "Example Org", "location": ""}}
    (folder / inspect.CACHE_FILE).write_text(json.dumps(listed) + "\n", encoding="utf-8")
    out = shortlist.save_shortlist(SLUG, [{"id": listed["identity_key"], "verdict": "keep"}])
    (row,) = json.loads(Path(out["outputs"]["results_json"]).read_text(encoding="utf-8"))["rows"]
    assert row["source_key"] == "recruitee:exampleorg"
    from contextlib import closing

    from ultron.storage import ledger
    with closing(ledger.connect()) as conn:
        keys = {key for (key,) in conn.execute("SELECT identity_key FROM jobs")}
    assert listed["identity_key"] in keys
    assert main(["watchlist", "list", "--profile", SLUG]) == 0
    assert [w["seed_key"] for w in json.loads(capsys.readouterr().out)["watched"]] == ["recruitee:exampleorg"]

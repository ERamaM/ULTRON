"""The agent toolkit: ``inspect`` dossiers and ``shortlist save`` (specification 31), with a mocked network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from test_scout_flow import AGGREGATOR, DEAD_AGGREGATOR, EMPLOYER, POSTING, _handler

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
        transport=httpx.MockTransport(_handler), resolver=lambda h, p: ["93.184.216.34"],
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



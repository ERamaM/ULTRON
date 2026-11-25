"""``ultron watch`` and ``ultron watchlist`` from the command line, with a mocked network."""

from __future__ import annotations

import json

import pytest
from toolkit_helpers import cli, offline

from ultron.cli import main
from ultron.exports.web_watch import read_closed_watch
from ultron.infra import home
from ultron.profiles import store as profiles
from ultron.stages import watch as watch_stage


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    offline(tmp_path, monkeypatch)


def test_watchlist_rejects_unknown_boards_and_profiles(capsys):
    assert main(["watchlist", "add", "--profile", profiles.EXAMPLE_SLUG, "--board", "nope"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_board"
    assert main(["watch", "--profile", "missing-profile"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "profile_not_found"
    assert main(["watch", "--profile", profiles.EXAMPLE_SLUG, "--preset", "turbo"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "unknown_preset"


def test_saved_search_runs_the_watch(capsys):
    cli(capsys, "saved-search", "create", "daily", "--profile", profiles.EXAMPLE_SLUG)
    out = cli(capsys, "saved-search", "run", "daily", "--watch")
    assert out["saved_search"] == "daily" and out["boards"] == 0


def test_watch_queries_a_board_outside_the_inventory(capsys):
    cli(capsys, "watchlist", "add", "--profile", profiles.EXAMPLE_SLUG, "--board", "https://boards.greenhouse.io/acme")
    out = cli(capsys, "watch", "--profile", profiles.EXAMPLE_SLUG)
    assert out["boards"] == 1 and out["new_postings"] >= 1
    assert cli(capsys, "watch", "--profile", profiles.EXAMPLE_SLUG)["new_postings"] == 0  # recorded once


def test_saved_search_watch_applies_overrides(capsys):
    cli(capsys, "saved-search", "create", "tight", "--profile", profiles.EXAMPLE_SLUG,
        "--override", "max_requests_per_host=1")
    cli(capsys, "watchlist", "add", "--profile", profiles.EXAMPLE_SLUG, "--board", "greenhouse:acme")
    out = cli(capsys, "saved-search", "run", "tight", "--watch")
    assert all(item["requests"] <= 1 for item in out["requests_by_domain"])


def test_watchlist_list_and_remove(capsys):
    cli(capsys, "watchlist", "add", "--profile", profiles.EXAMPLE_SLUG, "--board", "greenhouse:acme")
    listed = cli(capsys, "watchlist", "list", "--profile", profiles.EXAMPLE_SLUG)
    assert [w["seed_key"] for w in listed["watched"]] == ["greenhouse:acme"]
    cli(capsys, "watchlist", "remove", "--profile", profiles.EXAMPLE_SLUG, "--board", "greenhouse:acme")
    assert cli(capsys, "watchlist", "list", "--profile", profiles.EXAMPLE_SLUG)["watched"] == []



def test_clean_watch_clears_previous_closure(capsys, monkeypatch):
    slug = profiles.EXAMPLE_SLUG
    folder = home.ultron_home() / "watch"
    url = "https://example.com/jobs/1"
    job = {"canonical_url": url, "identity_key": "key"}
    watch_stage._write_digest(slug, [], "20260101T000000", [
        {"apply_url": url, "title": "Engineer", "company": "Acme"},
    ])
    assert len(read_closed_watch(folder, [job])) == 1
    assert job["is_closed_watch"]
    monkeypatch.setattr(watch_stage, "_role_candidates", lambda *args: ([], []))
    monkeypatch.setattr(watch_stage, "recheck_closed", lambda *args: [])
    result = cli(capsys, "watch", "--profile", slug)
    assert result["new_postings"] == 0 and result["closed"] == [] and result["digest"] is None
    assert read_closed_watch(folder, [job]) == []
    assert job["is_closed_watch"] is False

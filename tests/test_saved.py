from __future__ import annotations

import json

import httpx
import pytest
from test_unattended_e2e import _handler, _no_packs  # noqa: F401  (reuse e2e helpers/fixture)

from ultron import seeds
from ultron.cli import main
from ultron.core.models import AtsBoardSeed
from ultron.profiles import store as profiles
from ultron.stages import http_factory
from ultron.storage import runs, saved


def _setup_network():
    profiles.install_example()
    seeds.add_seeds(
        [AtsBoardSeed(seed_id="greenhouse:acme", company="Acme", ats="greenhouse", board_identifier="acme",
                      priority=1, profiles=[profiles.EXAMPLE_SLUG])],
        profile_slug=profiles.EXAMPLE_SLUG,
    )
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(_handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )


def test_create_rejects_unknown_override():
    profiles.install_example()
    with pytest.raises(ValueError):
        saved.create("x", profiles.EXAMPLE_SLUG, overrides={"bogus": 1})
    with pytest.raises(ValueError):
        saved.create("x", profiles.EXAMPLE_SLUG, preset="nope")
    with pytest.raises(FileNotFoundError):
        saved.create("x", "missing-profile")
    assert saved.list_all() == []


def test_create_list_delete():
    profiles.install_example()
    saved.create("daily", profiles.EXAMPLE_SLUG, overrides={"max_roles": 2}, prompt="remote only")
    rows = saved.list_all()
    assert [r["name"] for r in rows] == ["daily"]
    assert rows[0]["overrides"] == {"max_roles": 2}
    deleted = saved.delete("daily")
    assert deleted is True
    deleted_again = saved.delete("daily")
    assert deleted_again is False


def test_overrides_applied():
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced", overrides={"max_roles": 2})
    assert runs.load_run(run_id)["settings"]["max_roles"] == 2
    with pytest.raises(ValueError):
        runs.new_run(profiles.EXAMPLE_SLUG, "balanced", overrides={"nope": 1})


def test_run_unattended_writes_digest():
    _setup_network()
    saved.create("daily", profiles.EXAMPLE_SLUG)
    initial = main(["run", "--unattended", "--profile", profiles.EXAMPLE_SLUG])
    assert initial == 0
    first = saved.run("daily", unattended=True)
    digest = (runs.run_dir(first["run_id"]) / "digest.md").read_text(encoding="utf-8")
    assert first["new_roles"] >= 1 and first["digest"].endswith("digest.md")
    rows = json.loads((runs.run_dir(first["run_id"]) / "results.json").read_text(encoding="utf-8"))["rows"]
    for r in rows:
        assert (r["title"] in digest) == bool(r["is_new_in_saved_search"])
    second = saved.run("daily", unattended=True)
    assert second["new_roles"] == 0
    digest2 = (runs.run_dir(second["run_id"]) / "digest.md").read_text(encoding="utf-8")
    assert all(r["title"] not in digest2 for r in rows)


def test_run_interactive_returns_plan():
    profiles.install_example()
    saved.create("daily", profiles.EXAMPLE_SLUG, overrides={"max_roles": 3})
    out = saved.run("daily", unattended=False)
    assert runs.load_run(out["run_id"])["settings"]["max_roles"] == 3
    assert "query_plan" in out


def test_run_unknown_name():
    with pytest.raises(LookupError):
        saved.run("ghost", unattended=True)


def test_cli_saved_search(capsys):
    profiles.install_example()
    assert main(["saved-search", "create", "d", "--profile", profiles.EXAMPLE_SLUG, "--override", "max_roles=2"]) == 0
    capsys.readouterr()
    assert main(["saved-search", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["searches"][0]["overrides"] == {"max_roles": 2}
    assert main(["saved-search", "create", "e", "--profile", profiles.EXAMPLE_SLUG, "--override", "zzz=2"]) != 0
    capsys.readouterr()
    assert main(["saved-search", "run", "ghost"]) != 0
    capsys.readouterr()
    assert main(["saved-search", "delete", "d"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] is True


@pytest.mark.parametrize("bad", ["abc", None, True, 0, -3, 1.5])
def test_create_rejects_bad_override_values(bad):
    profiles.install_example()
    with pytest.raises(ValueError, match="max_roles"):
        saved.create("x", profiles.EXAMPLE_SLUG, overrides={"max_roles": bad})


def test_new_run_rejects_bad_override_values():
    profiles.install_example()
    for bad in (0, True, "abc"):
        with pytest.raises(ValueError, match="max_roles"):
            runs.new_run(profiles.EXAMPLE_SLUG, "balanced", overrides={"max_roles": bad})
    assert runs.load_run(runs.new_run(profiles.EXAMPLE_SLUG, "balanced", overrides={"max_roles": 7}))["settings"]["max_roles"] == 7


def test_cli_rejects_bad_override_value(capsys):
    profiles.install_example()
    assert main(["saved-search", "create", "e", "--profile", profiles.EXAMPLE_SLUG, "--override", "max_roles=abc"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_value"


def _record_search_result(conn, run_id, day, profile, rows, *, search_id=None):
    from datetime import UTC, datetime

    from ultron.core.models import RunSummary
    from ultron.storage import ledger

    timestamp = datetime(2026, 11, day, tzinfo=UTC)
    summary = RunSummary(run_id=run_id, started_at=timestamp, ended_at=timestamp, ats_pages_opened=len(rows),
                         roles_kept=len(rows), discard_counts={}, full_query_log=[])
    ledger.record_finalized_run(conn, summary, rows, profile_slug=profile, preset="balanced", saved_search_id=search_id)


def test_feedback_scope_and_novelty_use_recorded_occurrences(capsys):
    from contextlib import closing

    from ultron.core.models import FinalRow
    from ultron.storage import ledger

    posting = FinalRow(title="Engineer", company="Acme", apply_url="https://boards.greenhouse.io/acme/jobs/12",
                       location="Spain", remote_policy="remote_eu", eligibility="confirmed",
                       eligibility_evidence="Spain", fit_signals=[], freshness="")
    profiles.install_example()
    saved.create("daily", profiles.EXAMPLE_SLUG)
    search_id = ledger.saved_search_identity("daily", saved.get("daily")["created_at"])
    slug = profiles.EXAMPLE_SLUG
    with closing(ledger.connect()) as conn:
        _record_search_result(conn, "monday", 2, "management", [posting])
        _record_search_result(conn, "tuesday", 3, slug, [])
        _record_search_result(conn, "wednesday", 4, slug, [], search_id=search_id)
        _record_search_result(conn, "thursday", 5, slug, [posting], search_id=search_id)
        _record_search_result(conn, "friday", 6, slug, [posting.model_copy(update={"match_score": 81, "unverified_date": "2026-10-01"})],
                              search_id=search_id)
        _record_search_result(conn, "saturday", 7, "management", [posting.model_copy(update={
            "match_score": 27, "published_at": "2026-10-10", "updated_at": "2026-10-12",
        })])

    def call(*args):
        assert main(list(args)) == 0
        return json.loads(capsys.readouterr().out)

    url = posting.apply_url
    call("jobs", "feedback", url, "uncertain")
    call("jobs", "feedback", url, "relevant", "--profile", slug)
    call("jobs", "feedback", url, "not_relevant", "--profile", "management")
    call("jobs", "mark", url, "applied")
    for profile_slug, relevance in ((slug, "relevant"), ("management", "not_relevant")):
        (job,) = call("jobs", "list", "--profile", profile_slug)["jobs"]
        assert (job["feedback_scope"], job["feedback_relevance"]) == ("profile", relevance)
        assert job["review_state"] == "applied"
        assert job["profile_slug"] == profile_slug
        assert job["match_score"] == (81 if profile_slug == slug else 27)
        dates = (job["published_at"], job["updated_at"], job["unverified_date"])
        assert dates == ((None, None, "2026-10-01") if profile_slug == slug else ("2026-10-10", "2026-10-12", None))
        assert job["last_seen_run_id"] == ("friday" if profile_slug == slug else "saturday")
        assert job["unscoped_feedback_relevance"] == "uncertain"
    assert call("jobs", "list")["jobs"][0]["feedback_scope"] == "unscoped"
    assert call("jobs", "list", "--new", "--profile", slug)["count"] == 0

    scoped = call("jobs", "list", "--new", "--since-run", "tuesday", "--profile", slug)
    assert scoped["count"] == 1
    assert scoped["scope"] == {"kind": "profile", "profile": slug}
    legacy = call("jobs", "new-since", "tuesday", "--saved-search", "daily")
    assert legacy["count"] == 1
    assert legacy["scope"]["kind"] == "profile"
    assert legacy["scope"]["requested_search"] == "daily"
    actual = call("jobs", "new-since", "wednesday", "--saved-search", "daily")
    assert actual["scope"]["kind"] == "saved_search" and actual["count"] == 1
    result, = actual["jobs"]
    assert (result["published_at"], result["updated_at"], result["unverified_date"]) == (None, None, "2026-10-01")
    assert call("jobs", "new-since", "thursday", "--saved-search", "daily")["count"] == 0
    assert call("jobs", "new-since", "tuesday")["count"] == 0
    assert main(["jobs", "new-since", "monday", "--saved-search", "daily"]) != 0
    assert "selected search scope" in capsys.readouterr().out


def test_cleaning_run_artifacts_preserves_novelty_and_scoped_labels(capsys):
    from contextlib import closing

    from ultron.core.models import FinalRow
    from ultron.storage import ledger, review

    profiles.install_example()
    slug = profiles.EXAMPLE_SLUG
    saved.create("daily", slug)
    search_id = ledger.saved_search_identity("daily", saved.get("daily")["created_at"])
    old = runs.new_run(slug, "balanced")
    newer = runs.new_run(slug, "balanced")
    vacancy = FinalRow(title="Engineer", company="Acme", apply_url="https://boards.greenhouse.io/acme/jobs/15",
                       location="Spain", remote_policy="remote_eu", eligibility="confirmed",
                       eligibility_evidence="Spain", fit_signals=[], freshness="")
    with closing(ledger.connect()) as conn:
        _record_search_result(conn, old, 1, slug, [vacancy], search_id=search_id)

    assert main(["data", "delete-run", old, "--yes"]) == 0
    capsys.readouterr()
    assert not runs.run_dir(old).exists()

    with closing(ledger.connect()) as conn:
        _record_search_result(conn, newer, 2, slug, [vacancy], search_id=search_id)
        assert ledger.new_keys_in_saved_search(conn, newer) == set()
        assert conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (old,)).fetchone() is None
    assert main(["jobs", "new-since", old, "--saved-search", "daily"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 0

    key = review.set_feedback(vacancy.apply_url, "relevant", profile=slug)
    with closing(ledger.connect()) as conn:
        ledger.record_occurrences(conn, old, [])
        ledger.record_occurrences(conn, newer, [])
        assert conn.execute("SELECT times_seen FROM jobs WHERE identity_key = ?", (key,)).fetchone() == (0,)
    assert review.set_feedback(key, "uncertain", profile=slug) == key


def test_cleaned_runs_remain_in_unscoped_history_and_scout_memory(capsys):
    from contextlib import closing

    from ultron.core.models import FinalRow
    from ultron.storage import ledger, review, scout_memory

    profiles.install_example()
    slug = profiles.EXAMPLE_SLUG
    earlier, baseline, first = [runs.new_run(slug, "balanced") for _ in range(3)]
    row = FinalRow(title="Engineer", company="Acme", apply_url="https://boards.greenhouse.io/acme/jobs/31",
                   location="Spain", remote_policy="remote_eu", eligibility="confirmed",
                   eligibility_evidence="Spain", fit_signals=[], freshness="")
    with closing(ledger.connect()) as conn:
        _record_search_result(conn, earlier, 1, slug, [])
        _record_search_result(conn, baseline, 2, slug, [])
        _record_search_result(conn, first, 3, slug, [row])
        key = conn.execute("SELECT identity_key FROM jobs").fetchone()[0]
    for run in (baseline, first):
        assert main(["data", "delete-run", run, "--yes"]) == 0
        capsys.readouterr()
    assert [job["identity_key"] for job in review.new_since(earlier)] == [key]
    assert [job["identity_key"] for job in review.new_since(baseline)] == [key]
    assert [job["identity_key"] for job in review.list_jobs(since_run=baseline)] == [key]
    with closing(ledger.connect()) as conn:
        assert scout_memory.flags(conn, slug, [key]) == {key: "seen"}

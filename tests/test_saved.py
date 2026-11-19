from __future__ import annotations

import json

import httpx
import pytest
from test_unattended_e2e import _handler, _no_packs  # noqa: F401  (reuse e2e helpers/fixture)

from ultron import http_factory, profiles, runs, saved, seeds
from ultron.cli import main
from ultron.core.models import AtsBoardSeed


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
    assert saved.delete("daily") is True
    assert saved.delete("daily") is False


def test_overrides_applied():
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced", overrides={"max_roles": 2})
    assert runs.load_run(run_id)["settings"]["max_roles"] == 2
    with pytest.raises(ValueError):
        runs.new_run(profiles.EXAMPLE_SLUG, "balanced", overrides={"nope": 1})


def test_run_unattended_writes_digest():
    _setup_network()
    saved.create("daily", profiles.EXAMPLE_SLUG)
    first = saved.run("daily", unattended=True)
    digest = (runs.run_dir(first["run_id"]) / "digest.md").read_text(encoding="utf-8")
    assert first["new_roles"] >= 1 and first["digest"].endswith("digest.md")
    rows = json.loads((runs.run_dir(first["run_id"]) / "results.json").read_text(encoding="utf-8"))["rows"]
    for r in rows:
        assert (r["title"] in digest) == bool(r["is_new"])
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

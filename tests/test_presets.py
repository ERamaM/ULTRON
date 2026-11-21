import pytest

from ultron.core import presets


def test_default_preset_is_balanced():
    assert presets.DEFAULT_PRESET == "balanced"
    assert presets.resolve(presets.DEFAULT_PRESET)["max_pages"] == 400


def test_unknown_preset_raises():
    with pytest.raises(KeyError):
        presets.resolve("nope")


def test_table_values():
    assert presets.resolve("fast")["max_search_queries"] == 20
    assert presets.resolve("deep")["max_seeds_per_run"] == 400
    assert presets.resolve("seed_boost")["new_companies_per_run"] == 120
    assert presets.resolve("balanced")["llm_ambiguity_threshold"] == 50


def test_resolve_returns_copy():
    presets.resolve("fast")["max_pages"] = 1
    assert presets.resolve("fast")["max_pages"] == 70


@pytest.mark.parametrize("value", [101, 250, 100.5])
def test_threshold_override_above_100_is_rejected(value):
    with pytest.raises(presets.PresetOverrideError, match="llm_ambiguity_threshold"):
        presets.validate_overrides({"llm_ambiguity_threshold": value})


def test_threshold_override_within_bounds_is_accepted():
    assert presets.validate_overrides({"llm_ambiguity_threshold": 100}) == {"llm_ambiguity_threshold": 100}


def test_min_roles_above_max_roles_is_rejected():
    with pytest.raises(presets.PresetOverrideError, match="min_roles"):
        presets.validate_overrides({"min_roles": 20, "max_roles": 10})
    with pytest.raises(presets.PresetOverrideError, match="min_roles"):
        presets.validate_overrides({"min_roles": 300})  # above balanced max_roles=250
    assert presets.validate_overrides({"min_roles": 10, "max_roles": 10}) == {"min_roles": 10, "max_roles": 10}


def test_min_roles_is_checked_against_the_chosen_preset():
    with pytest.raises(presets.PresetOverrideError, match="min_roles"):
        presets.validate_overrides({"min_roles": 6}, presets.resolve("fast"))  # fast max_roles=5


def test_lowering_only_max_roles_stays_allowed():
    # min_roles is not read by any stage; saved searches that only cap max_roles keep working.
    assert presets.validate_overrides({"max_roles": 2}) == {"max_roles": 2}


def test_override_errors_are_value_errors_with_the_key():
    with pytest.raises(ValueError, match="unknown override keys") as info:
        presets.validate_overrides({"bogus": 1})
    assert isinstance(info.value, presets.PresetOverrideError) and info.value.key == "bogus"


def test_saved_search_overrides_are_checked_against_its_preset():
    from ultron.profiles import store as profiles
    from ultron.storage import saved

    profiles.install_example()
    with pytest.raises(presets.PresetOverrideError, match="min_roles"):
        saved.create("quick", profiles.EXAMPLE_SLUG, preset="fast", overrides={"min_roles": 6})


def test_new_run_caps_min_roles_when_only_max_roles_is_lowered():
    from ultron.profiles import store as profiles
    from ultron.storage import runs

    profiles.install_example()
    settings = runs.load_run(runs.new_run(profiles.EXAMPLE_SLUG, "balanced", overrides={"max_roles": 2}))["settings"]
    assert settings["max_roles"] == 2 and settings["min_roles"] == 2


def test_saved_search_run_reports_a_stored_out_of_bounds_override(capsys):
    import json
    from contextlib import closing

    from ultron.cli import main
    from ultron.profiles import store as profiles
    from ultron.storage import ledger

    profiles.install_example()
    with closing(ledger.connect()) as conn, conn:  # a row stored before the bounds existed
        conn.execute(
            "INSERT INTO saved_searches (name, profile_slug, preset, prompt, overrides_json, created_at, updated_at)"
            " VALUES ('old', ?, 'balanced', '', ?, 'x', 'x')",
            (profiles.EXAMPLE_SLUG, json.dumps({"llm_ambiguity_threshold": 150})),
        )
    assert main(["saved-search", "run", "old"]) != 0
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_value" and "llm_ambiguity_threshold" in out["detail"]

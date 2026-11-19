import pytest

from ultron import presets


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

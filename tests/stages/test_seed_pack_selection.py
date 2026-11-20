"""Pack seeds reach the profiles they were built for: region scopes beyond EU and sector aliases."""

from __future__ import annotations

import pytest
import yaml

from ultron import seeds
from ultron.core.sectors import profile_sector_values
from ultron.stages.discover import _select_seeds, eligible_seed_count, seed_scope_fits


def _profile(preferred: list[str], sectors: list[str], eligibility: list[str] | None = None) -> dict:
    return {
        "slug": "synthetic",
        "criteria": {
            "sectors": sectors,
            "target_titles": ["Example Role"],
            "location_scope": {"preferred": preferred, "eligibility_terms": eligibility or []},
        },
    }


LATAM_DATA = _profile(["Remote LATAM", "Brazil", "Mexico"], ["technology"])
NURSE_IE = _profile(["Ireland", "Dublin"], ["healthcare"], ["ireland", "dublin"])
NURSE_ES = _profile(["Spain", "Madrid"], ["healthcare"])
BACKEND_EU = _profile(["Spain", "Remote EU"], ["technology"])
ANYWHERE = _profile([], ["technology"])


@pytest.mark.parametrize(("scope", "profile", "fits"), [
    ("EU", BACKEND_EU, True),
    ("EU", LATAM_DATA, False),
    ("EU", ANYWHERE, True),
    ("LATAM", LATAM_DATA, True),
    ("LATAM", BACKEND_EU, False),  # a LATAM pack never competes for an EU profile's seed slots
    ("LATAM", ANYWHERE, True),
    ("latam", LATAM_DATA, True),
    ("IE", NURSE_IE, True),
    ("IE", NURSE_ES, False),  # a country pack only serves profiles that name that country
    ("IE", BACKEND_EU, False),
    ("US", _profile(["Remote US", "Texas"], ["healthcare"]), True),
    ("US", LATAM_DATA, False),  # "Latin America" never reads as the US
    ("US", NURSE_IE, False),
    ("GLOBAL", BACKEND_EU, True),
    ("GLOBAL", LATAM_DATA, True),
    ("", BACKEND_EU, True),  # a missing scope reads as the legacy EU default
])
def test_seed_scope_fits_profile_regions(scope, profile, fits):
    assert seed_scope_fits(scope, profile) is fits


@pytest.mark.parametrize(("preferred", "fits"), [
    (["New York"], True),
    (["Texas"], True),
    (["Houston", "Austin"], True),
    (["Austin, TX"], True),  # a state code after a comma
    (["Washington DC"], True),
    (["North Carolina"], True),
    (["Georgia"], False),  # also a country: left out on purpose
    (["Tbilisi, Georgia"], False),
    (["Remote Portugal"], False),
    (["San José, Costa Rica"], False),
    (["Madrid, ES"], False),  # a two-letter code that is not a US state
])
def test_us_scope_matches_states_and_major_cities(preferred, fits):
    assert seed_scope_fits("US", _profile(preferred, ["healthcare"])) is fits


@pytest.mark.parametrize(("preferred", "scope", "fits"), [
    (["Ciudad de México"], "LATAM", True),
    (["Belo Horizonte"], "LATAM", True),
    (["Porto Alegre"], "LATAM", True),
    (["New Mexico"], "LATAM", False),  # the US state is not Mexico
    (["Albuquerque, New Mexico"], "US", True),
    (["Athlone"], "IE", True),
    (["County Kildare"], "IE", True),
    (["Tralee"], "IE", True),
])
def test_region_scopes_match_main_cities(preferred, scope, fits):
    assert seed_scope_fits(scope, _profile(preferred, ["technology"])) is fits


def test_a_us_city_profile_selects_the_us_healthcare_seeds(pack_dir):
    rows = [_row(f"ushealth{i}", "US", "pharma_health") for i in range(3)]
    (pack_dir / "p.yaml").write_text(
        yaml.safe_dump({"pack": {"id": "p", "usage": "active"}, "seeds": rows}), encoding="utf-8"
    )
    assert eligible_seed_count("synthetic", _profile(["New York"], ["healthcare"], ["new york"])) == 3


def test_latam_scope_matches_country_and_city_names_without_the_word_latam():
    assert seed_scope_fits("LATAM", _profile(["Bogotá", "Medellín"], ["technology"]))
    assert seed_scope_fits("LATAM", _profile(["Remote"], ["technology"], ["são paulo"]))
    assert not seed_scope_fits("LATAM", _profile(["Remote Portugal"], ["technology"]))


@pytest.mark.parametrize(("raw", "expected"), [
    (["healthcare"], {"pharma_health"}),
    (["Health care", "Nursing"], {"pharma_health"}),
    (["Pharma and health"], {"pharma_health"}),
    (["technology"], {"technology"}),
    (["Software", "SaaS"], {"technology"}),
    (["finance"], {"finance_insurance"}),
    (["Public sector"], {"public_education"}),
    (["underwater basket weaving"], {"underwater basket weaving"}),  # unknown values stay as written
    ([], set()),
    (None, set()),
])
def test_profile_sector_values_map_free_text_to_the_sector_vocabulary(raw, expected):
    assert profile_sector_values(raw) == expected


@pytest.fixture
def pack_dir(tmp_path, monkeypatch):
    path = tmp_path / "packs"
    path.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: path)
    return path


def _row(board: str, scope: str, sector: str) -> dict:
    return {"company": board.title(), "ats": "greenhouse", "board_identifier": board, "country_scope": scope,
            "sectors": [sector]}


def test_eligible_seed_count_picks_region_and_sector_packs(pack_dir):
    rows = (
        [_row(f"eutech{i}", "EU", "technology") for i in range(5)]
        + [_row(f"latamtech{i}", "LATAM", "technology") for i in range(3)]
        + [_row(f"iehealth{i}", "IE", "pharma_health") for i in range(2)]
    )
    (pack_dir / "p.yaml").write_text(
        yaml.safe_dump({"pack": {"id": "p", "usage": "active"}, "seeds": rows}), encoding="utf-8"
    )
    assert eligible_seed_count("synthetic", BACKEND_EU) == 5
    assert eligible_seed_count("synthetic", LATAM_DATA) == 3
    assert eligible_seed_count("synthetic", NURSE_IE) == 2
    assert eligible_seed_count("synthetic", NURSE_ES) == 0
    assert eligible_seed_count("synthetic", ANYWHERE) == 8


def test_seeds_scoped_to_another_region_are_never_queried_as_filler(pack_dir):
    # S5 balanced (session 2): with few fitting seeds, US hospital boards filled the leftover slots of an Irish
    # nursing profile. Legacy EU-scoped seeds keep their old place (last, as filler).
    rows = (
        [_row("iehealth", "IE", "pharma_health")]
        + [_row(f"ushealth{i}", "US", "pharma_health") for i in range(3)]
        + [_row(f"latamtech{i}", "LATAM", "technology") for i in range(2)]
        + [_row("eutech", "EU", "technology")]
    )
    (pack_dir / "p.yaml").write_text(
        yaml.safe_dump({"pack": {"id": "p", "usage": "active"}, "seeds": rows}), encoding="utf-8"
    )
    selected, _groups, _stats = _select_seeds("synthetic", ["healthcare"], 50, 50, geo="eu", profile=NURSE_IE)
    assert [seed.board_identifier for seed, _group in selected] == ["iehealth", "eutech"]
    selected, _groups, _stats = _select_seeds("synthetic", ["technology"], 50, 50, geo="other", profile=LATAM_DATA)
    boards = [seed.board_identifier for seed, _group in selected]
    assert boards[:2] == ["latamtech0", "latamtech1"]
    assert not any(board.startswith("ushealth") for board in boards)

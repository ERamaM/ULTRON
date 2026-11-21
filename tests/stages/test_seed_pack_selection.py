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
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: path)
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
    # Session 3: "eutech" is left out too (another sector), no longer queried as last-rank filler.
    assert [seed.board_identifier for seed, _group in selected] == ["iehealth"]
    selected, _groups, _stats = _select_seeds("synthetic", ["technology"], 50, 50, geo="other", profile=LATAM_DATA)
    boards = [seed.board_identifier for seed, _group in selected]
    # Same sector but a European pack for a LATAM profile: left out as well (session 3).
    assert boards == ["latamtech0", "latamtech1"]


def _write_pack(pack_dir, rows: list[dict]) -> None:
    (pack_dir / "p.yaml").write_text(
        yaml.safe_dump({"pack": {"id": "p", "usage": "active"}, "seeds": rows}), encoding="utf-8"
    )


def test_pack_seeds_of_another_sector_are_never_queried_as_filler(pack_dir):
    # S5 balanced (session 2): an Irish nursing profile with 3 fitting seeds queried 102 EU technology boards as
    # last-rank filler, which exposed it to a vendor 429 and added no nursing postings.
    _write_pack(pack_dir, [_row("iehealth", "IE", "pharma_health"), _row("eutech", "EU", "technology"),
                           _row("globaltech", "GLOBAL", "technology")])
    selected, groups, _stats = _select_seeds("synthetic", ["healthcare"], 50, 50, geo="eu", profile=NURSE_IE)
    assert [seed.board_identifier for seed, _group in selected] == ["iehealth"]
    assert all(group["capped"] == group["host_capped"] == 0 for group in groups.values())
    assert eligible_seed_count("synthetic", NURSE_IE) == 1


def test_a_pack_seed_without_sectors_still_fits_every_profile(pack_dir):
    _write_pack(pack_dir, [{"company": "Plain", "ats": "greenhouse", "board_identifier": "plain",
                            "country_scope": "EU"}])
    selected, _groups, _stats = _select_seeds("synthetic", ["healthcare"], 50, 50, geo="eu", profile=NURSE_IE)
    assert [(seed.board_identifier, group) for seed, group in selected] == [("plain", "pack")]


def test_profiles_without_sectors_use_packs_of_every_sector(pack_dir):
    _write_pack(pack_dir, [_row("euhealth", "EU", "pharma_health"), _row("eutech", "EU", "technology")])
    for sectors in ([], None):
        profile = _profile(["Spain"], sectors or [])
        selected, _groups, _stats = _select_seeds("synthetic", sectors, 50, 50, geo="eu", profile=profile)
        assert {seed.board_identifier for seed, _group in selected} == {"eutech", "euhealth"}


def test_free_text_sectors_outside_the_vocabulary_match_no_pack(pack_dir):
    # "fintech" maps to no sector: no pack fits, so the profile relies on generated seeds.
    _write_pack(pack_dir, [_row("euhealth", "EU", "pharma_health"), _row("eutech", "EU", "technology")])
    selected, _groups, _stats = _select_seeds("synthetic", ["fintech"], 50, 50, geo="eu",
                                              profile=_profile(["Spain"], ["fintech"]))
    assert selected == []
    assert eligible_seed_count("synthetic", _profile(["Spain"], ["fintech"])) == 0


def test_a_known_sector_next_to_free_text_still_drops_other_sector_packs(pack_dir):
    _write_pack(pack_dir, [_row("euhealth", "EU", "pharma_health"), _row("eutech", "EU", "technology")])
    profile = _profile(["Spain"], ["fintech", "software"])
    selected, _groups, _stats = _select_seeds("synthetic", ["fintech", "software"], 50, 50, geo="eu", profile=profile)
    assert [seed.board_identifier for seed, _group in selected] == ["eutech"]


def test_seeds_linked_to_the_profile_are_kept_whatever_their_sector(pack_dir, tmp_path, monkeypatch):
    _write_pack(pack_dir, [_row("eutech", "EU", "technology")])
    auto = tmp_path / "auto_seeds.yaml"
    auto.write_text(yaml.safe_dump({"seeds": [
        {"company": "Agent Tech", "ats": "lever", "board_identifier": "agenttech", "profiles": ["synthetic"],
         "source_type": "agent_generated", "sectors": ["technology"]},
        {"company": "Manual Tech", "ats": "lever", "board_identifier": "manualtech", "profiles": ["synthetic"],
         "source_type": "manual", "sectors": ["technology"]},
    ]}), encoding="utf-8")
    monkeypatch.setattr(seeds.inventory, "auto_seeds_path", lambda: auto)
    selected, _groups, _stats = _select_seeds("synthetic", ["healthcare"], 50, 50, geo="eu", profile=NURSE_IE)
    assert sorted(seed.board_identifier for seed, _group in selected) == ["agenttech", "manualtech"]

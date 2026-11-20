"""The bundled region packs: which profiles they serve, and the live evidence each row records."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from ultron.stages.discover import COLD_START_SEEDS, eligible_seed_count, seed_scope_fits

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "profiles"
BUNDLED_PACKS = Path(__file__).resolve().parents[2] / "skill" / "ultron" / "data" / "seed_packs"
REGION_PACKS = ("technology-latam.yaml", "healthcare-us.yaml", "healthcare-ie.yaml")


def _fixture(slug: str) -> dict:
    return yaml.safe_load((FIX / f"{slug}.yaml").read_text(encoding="utf-8"))


def _profile(preferred: list[str], sectors: list[str]) -> dict:
    return {"slug": "synthetic", "criteria": {
        "sectors": sectors, "target_titles": ["Example Role"], "location_scope": {"preferred": preferred},
    }}


def _rows(pack_name: str) -> list[dict]:
    data = yaml.safe_load((BUNDLED_PACKS / pack_name).read_text(encoding="utf-8"))
    return [row for row in data["seeds"] if row.get("enabled", True)]


LATAM_DATA = _profile(["Remote LATAM", "Brazil", "Mexico"], ["technology"])
NURSE_IE = _profile(["Ireland", "Dublin"], ["healthcare"])
NURSE_US = _profile(["Remote US", "Texas"], ["Health care"])
NURSE_ES = _profile(["Spain", "Madrid"], ["healthcare"])
BACKEND_EU = _profile(["Spain", "Remote EU"], ["technology"])


def _global_rows(pack_name: str) -> list[dict]:
    return [row for row in _rows(pack_name) if str(row.get("country_scope", "")).upper() == "GLOBAL"]


def test_latam_pack_ends_the_cold_start_of_latam_tech_profiles():
    expected = len(_rows("technology-latam.yaml")) + len(_global_rows("technology-eu.yaml"))
    assert expected >= COLD_START_SEEDS
    assert eligible_seed_count("synthetic", LATAM_DATA) == expected
    assert eligible_seed_count("backend-latam", _fixture("backend-latam")) == expected


def test_region_packs_leave_eu_tech_profiles_unchanged():
    eu_tech = len(_rows("technology-eu.yaml"))
    assert eligible_seed_count("ai-ml-remote-eu", _fixture("ai-ml-remote-eu")) == eu_tech
    assert eligible_seed_count("synthetic", BACKEND_EU) == eu_tech


def test_healthcare_packs_serve_nursing_profiles_in_their_country_only():
    assert len(_rows("healthcare-us.yaml")) >= COLD_START_SEEDS
    assert eligible_seed_count("synthetic", NURSE_US) == len(_rows("healthcare-us.yaml"))
    assert eligible_seed_count("synthetic", NURSE_IE) == len(_rows("healthcare-ie.yaml"))
    assert eligible_seed_count("synthetic", NURSE_ES) == 0


@pytest.mark.parametrize("pack_name", REGION_PACKS)
def test_region_pack_rows_record_their_live_verification(pack_name):
    rows = _rows(pack_name)
    assert rows
    for row in rows:
        verification = row.get("verification") or {}
        assert verification.get("postings", 0) > 0, row["board_identifier"]
        assert datetime.fromisoformat(str(verification.get("verified_at"))).year >= 2026, row["board_identifier"]


@pytest.mark.parametrize("pack_name", REGION_PACKS)
def test_region_pack_rows_carry_a_scope_the_selection_knows(pack_name):
    for row in _rows(pack_name):
        scope = row["country_scope"]
        assert scope in {"LATAM", "US", "IE"}, row["board_identifier"]
        # The scope must select at least one profile, so a typo cannot hide a row.
        assert any(seed_scope_fits(scope, p) for p in (LATAM_DATA, NURSE_IE, NURSE_US)), row["board_identifier"]

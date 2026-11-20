"""Bundled seed packs only list boards that can name a real tenant."""

from __future__ import annotations

from pathlib import Path

import yaml

from ultron.core.ats.registry import GENERIC_BOARD_IDENTIFIERS

PACKS = Path(__file__).resolve().parents[2] / "skill" / "ultron" / "data" / "seed_packs"


def _rows():
    for pack in sorted(PACKS.glob("*.yaml")):
        for row in yaml.safe_load(pack.read_text(encoding="utf-8"))["seeds"]:
            yield pack.name, row


def test_no_enabled_pack_seed_uses_a_generic_board_identifier():
    # A generic slug (such as the "app" subdomain of a vendor's own site) is never one company's board.
    generic = [
        (name, row["ats"], row["board_identifier"])
        for name, row in _rows()
        if row.get("enabled", True) and str(row["board_identifier"]).strip().lower() in GENERIC_BOARD_IDENTIFIERS
    ]
    assert generic == []


def test_pack_seeds_are_unique_per_vendor_and_board():
    keys = [(name, row["ats"], str(row["board_identifier"]).lower()) for name, row in _rows()]
    assert len(keys) == len(set(keys))

"""Seed inventory: the auto-seed file, bundled packs and their row parsing.

Seed identity: ``seed_id == f"{ats}:{board}"`` where ``(ats, board)`` is
``ultron.core.ats.identity.seed_identity`` (vendor and board lowercased, Workday keeps
its case-sensitive site path). The same pair is the dedup key.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from ultron.core.ats.identity import seed_identity
from ultron.core.models import AtsBoardSeed
from ultron.core.sectors import seed_sector_values
from ultron.infra import home

logger = logging.getLogger(__name__)


_TRUE = {"1", "true", "yes", "y", "on", "si", "sí"}
_FALSE = {"0", "false", "no", "n", "off"}


def make_seed_id(ats: str, board: str) -> str:
    """Format canonical ``<vendor>:<identifier>`` seed key."""
    vendor, ident = seed_identity(ats, board)
    return f"{vendor}:{ident}"


def auto_seeds_path() -> Path:
    """Return path to ``$ULTRON_HOME/seeds/auto_seeds.yaml``."""
    return home.ultron_home() / "seeds" / "auto_seeds.yaml"


def packs_dir() -> Path:
    """Return the bundled directory containing seed-pack YAML files."""
    return home.data_dir() / "seed_packs"


def _coerce_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _coerce_int(value: object, default: int) -> int:
    try:
        return default if value is None or str(value).strip() == "" else int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _tags(value: object) -> list[str]:
    raw = value.split(",") if isinstance(value, str) else list(value) if isinstance(value, (list, tuple, set)) else []
    return list(dict.fromkeys(str(t).strip() for t in raw if str(t or "").strip()))


def parse_rows(rows: list) -> list[AtsBoardSeed]:
    """Parse raw YAML row dictionaries into ``AtsBoardSeed`` objects."""
    seeds: list[AtsBoardSeed] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ats = str(row.get("ats", "")).strip().lower()
        board = str(row.get("board_identifier", "")).strip()
        try:
            seed_id = make_seed_id(ats, board)
            seed = AtsBoardSeed(
                seed_id=seed_id,
                company=str(row.get("company", "")).strip() or board,
                ats=ats,
                board_identifier=board,
                enabled=_coerce_bool(row.get("enabled", True)),
                priority=_coerce_int(row.get("priority", 100), 100),
                tags=_tags(row.get("tags", [])),
                profiles=row.get("profiles") or [],
                sectors=seed_sector_values(row.get("sectors", []), seed_id=seed_id, logger=logger),
                country_scope=str(row.get("country_scope", "EU")).strip() or "EU",
                source_type=str(row.get("source_type", "manual_yaml")).strip() or "manual_yaml",
                source_label=str(row.get("source_label", "Manual YAML")).strip() or "Manual YAML",
                source_url=str(row.get("source_url", "")).strip(),
                verification=row.get("verification") or {},
            )
        except (ValueError, TypeError) as exc:  # invalid row: skip, never abort the inventory
            logger.warning("Skipping invalid seed row ats=%s board=%s error=%s", ats, board, type(exc).__name__)
            continue
        if seed.company and seed.board_identifier:
            seeds.append(seed)
    return seeds


def read_yaml_seeds(path: Path) -> list[AtsBoardSeed]:
    """Read ``AtsBoardSeed`` entries from a YAML file, or return an empty list."""
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        logger.warning("Invalid seed file file=%s", path.name)
        return []
    rows = data.get("seeds", []) if isinstance(data, dict) else []
    return parse_rows(rows) if isinstance(rows, list) else []


def _read_pack_seeds() -> list[AtsBoardSeed]:
    seeds: list[AtsBoardSeed] = []
    for file in sorted(packs_dir().glob("*.yaml")):
        try:
            data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict) or (data.get("pack") or {}).get("usage") != "active":
            continue
        rows = data.get("seeds", [])
        if isinstance(rows, list):
            seeds.extend(parse_rows(rows))
    return seeds


def load_inventory(profile_slug: str | None) -> list[AtsBoardSeed]:
    """Enabled seeds: auto seeds linked to the profile first, then the bundled packs.

    Auto seeds linked only to other profiles are left out. With ``profile_slug=None``
    every auto seed comes first. Dedup key is ``seed_identity`` (first occurrence wins).
    """
    auto = [s for s in read_yaml_seeds(auto_seeds_path()) if s.enabled]
    if profile_slug is not None:
        auto = [s for s in auto if profile_slug in s.profiles]
    out: dict[tuple[str, str], AtsBoardSeed] = {}
    for seed in [*auto, *(s for s in _read_pack_seeds() if s.enabled)]:
        out.setdefault(seed_identity(seed.ats, seed.board_identifier), seed)
    return list(out.values())


# Seeds stored without confirmed ownership of the board.
UNCONFIRMED_SEED_PRIORITY = 900
UNCONFIRMED_SOURCE_TYPE = "guessed_unconfirmed"
OTHER_COMPANY_SOURCE_TYPE = "guessed_other_company"


def is_unconfirmed(seed: AtsBoardSeed) -> bool:
    """Return True if ``seed`` originates from an unconfirmed company source."""
    return seed.source_type in {UNCONFIRMED_SOURCE_TYPE, OTHER_COMPANY_SOURCE_TYPE}

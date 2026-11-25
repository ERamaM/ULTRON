"""The boards a profile watches: user-added boards, then boards whose postings this role family kept.

``watch`` re-queries them on later days and ``search`` lists their postings. A board key outside the seed
inventory still works through ``board_seed``.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import get_vendor_spec
from ultron.core.models import AtsBoardSeed
from ultron.infra import home
from ultron.profiles.families import load_profile_families, related_profiles, role_family
from ultron.seeds import load_inventory
from ultron.storage import ledger, watchlist

WATCH_PASS = "watch"


def siblings(profile_slug: str, profile: dict) -> list[str]:
    """Other profiles sharing the run's role family."""
    return related_profiles(profile_slug, role_family(profile), load_profile_families())


def watch_seeds(run: dict) -> list:
    """The inventory seeds of the profile's watchlist (user-added boards, then boards with past keeps)."""
    if not (home.ultron_home() / "ultron.sqlite3").exists():
        return []
    profile = run.get("profile_snapshot") or {}
    try:
        with closing(ledger.connect()) as conn:
            watched = watchlist.watched(conn, run["profile_slug"], siblings(run["profile_slug"], profile))
    except sqlite3.Error:
        return []
    inventory = {":".join(seed_identity(s.ats, s.board_identifier)): s for s in load_inventory(None)}
    out = []
    for item in watched:
        seed = inventory.get(item["seed_key"]) or board_seed(item["seed_key"], item["company"])
        if seed is not None:
            out.append(seed)
    return out


def board_seed(key: str, company: str = ""):
    """A board seed for a ``vendor:board`` key outside the inventory, or ``None`` for an unknown vendor."""
    vendor, _, board = key.partition(":")
    if get_vendor_spec(vendor) is None or not board:
        return None
    try:
        return AtsBoardSeed(seed_id=f"api_{vendor}_{board}", company=company or board, ats=vendor,
                            board_identifier=board, enabled=True, priority=200, tags=["watchlist"])
    except ValueError:
        return None

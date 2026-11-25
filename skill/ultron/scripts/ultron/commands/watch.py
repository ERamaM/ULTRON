"""``ultron watch`` and ``ultron watchlist list|add|remove``."""

from __future__ import annotations

import argparse
from contextlib import closing

from ultron.commands.output import emit, fail
from ultron.core import presets
from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import extract_seed_from_url, get_vendor_spec
from ultron.profiles import store as profile_store
from ultron.stages.watch import watch
from ultron.stages.watch_boards import siblings
from ultron.storage import ledger, watchlist


def _board_key(raw: str) -> tuple[str, str] | None:
    """``(seed key, company)`` of a board URL or a ``vendor:board`` key; ``None`` when it names no board."""
    text = str(raw or "").strip()
    if "://" in text:
        seed = extract_seed_from_url(text, "")
        return (":".join(seed_identity(seed.ats, seed.board_identifier)), seed.company) if seed else None
    vendor, sep, board = text.partition(":")
    if not sep or not board.strip() or get_vendor_spec(vendor.strip().lower()) is None:
        return None
    return ":".join(seed_identity(vendor.strip().lower(), board.strip())), ""


def _profile(slug: str) -> dict | None:
    try:
        return profile_store.load_profile(slug)
    except FileNotFoundError:
        return None


def _watch(args: argparse.Namespace) -> int:
    try:
        presets.resolve(args.preset)
    except KeyError as exc:
        return fail("unknown_preset", str(exc.args[0]))
    if _profile(args.profile) is None:
        return fail("profile_not_found", f"profile not found: {args.profile}")
    emit(watch(args.profile, args.preset))
    return 0


def _list(args: argparse.Namespace) -> int:
    profile = _profile(args.profile)
    if profile is None:
        return fail("profile_not_found", f"profile not found: {args.profile}")
    with closing(ledger.connect()) as conn:
        watched = watchlist.watched(conn, args.profile, siblings(args.profile, profile))
        rows = watchlist.entries(conn, args.profile)
    emit({"profile": args.profile, "watched": watched, "entries": rows})
    return 0


def _change(args: argparse.Namespace) -> int:
    if _profile(args.profile) is None:
        return fail("profile_not_found", f"profile not found: {args.profile}")
    board = _board_key(args.board)
    if board is None:
        return fail("invalid_board", "give a board URL on a supported ATS or a <vendor>:<board> key")
    with closing(ledger.connect()) as conn:
        if args.watchlist_command == "add":
            watchlist.add(conn, args.profile, board[0], board[1])
        else:
            watchlist.remove(conn, args.profile, board[0])
    emit({"profile": args.profile, args.watchlist_command: board[0]})
    return 0


def register(subparsers) -> None:
    """Register ``ultron watch`` and ``ultron watchlist``."""
    w = subparsers.add_parser("watch", help="re-query the watchlist with no agent and report new postings")
    w.add_argument("--profile", required=True)
    w.add_argument("--preset", default=presets.DEFAULT_PRESET)
    w.set_defaults(func=_watch)
    wl = subparsers.add_parser("watchlist", help="list, add or remove the boards a profile watches")
    sub = wl.add_subparsers(dest="watchlist_command", required=True, parser_class=type(wl))
    listing = sub.add_parser("list", help="boards watched for a profile")
    listing.add_argument("--profile", required=True)
    listing.set_defaults(func=_list)
    for name, text in (("add", "watch a board"), ("remove", "stop watching a board and never add it back")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--profile", required=True)
        cmd.add_argument("--board", required=True, help="board URL or <vendor>:<board> key")
        cmd.set_defaults(func=_change)

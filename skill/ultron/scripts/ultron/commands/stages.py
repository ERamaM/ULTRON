"""``ultron discover ats|feeds``, ``ultron candidates add`` and ``ultron fetch``."""

from __future__ import annotations

import argparse
from pathlib import Path

from ultron.commands.output import emit, fail, fail_stage_order
from ultron.stages.candidates import add_candidates
from ultron.stages.discover import discover_ats
from ultron.stages.feeds import discover_feeds
from ultron.stages.fetch import fetch
from ultron.storage import runs


def _guard(func, *args) -> int:
    try:
        payload = func(*args)
    except runs.RunNotFoundError as exc:
        return fail("run_not_found", str(exc))
    except runs.StageOrderError as exc:
        return fail_stage_order(exc)
    except (OSError, ValueError) as exc:
        return fail("stage_failed", str(exc))
    emit(payload)
    return 0


def _discover(args: argparse.Namespace) -> int:
    return _guard(discover_ats, args.run)


def _discover_feeds(args: argparse.Namespace) -> int:
    return _guard(discover_feeds, args.run)


def _candidates_add(args: argparse.Namespace) -> int:
    return _guard(add_candidates, args.run, Path(args.source))


def _fetch(args: argparse.Namespace) -> int:
    return _guard(fetch, args.run)


def register(subparsers) -> None:
    discover = subparsers.add_parser("discover", help="candidate discovery")
    dsub = discover.add_subparsers(dest="discover_command", required=True, parser_class=type(discover))
    ats = dsub.add_parser("ats", help="query ATS board APIs for the seed inventory")
    ats.add_argument("--run", required=True)
    ats.set_defaults(func=_discover)
    feeds = dsub.add_parser("feeds", help="read structured job feeds (candidates, seeds, company hints)")
    feeds.add_argument("--run", required=True)
    feeds.set_defaults(func=_discover_feeds)

    cands = subparsers.add_parser("candidates", help="run candidates")
    csub = cands.add_subparsers(dest="candidates_command", required=True, parser_class=type(cands))
    add = csub.add_parser("add", help="add agent web-search results (JSONL of url/title/company/query)")
    add.add_argument("--run", required=True)
    add.add_argument("--from", dest="source", required=True)
    add.set_defaults(func=_candidates_add)

    fetch_parser = subparsers.add_parser("fetch", help="download candidate pages and extract postings")
    fetch_parser.add_argument("--run", required=True)
    fetch_parser.set_defaults(func=_fetch)

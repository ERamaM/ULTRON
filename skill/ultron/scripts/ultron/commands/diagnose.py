"""``ultron diagnose`` command."""

from __future__ import annotations

import argparse

from ultron.commands.output import emit, fail
from ultron.diagnostics import diagnose as diag
from ultron.storage import runs


def _run(args: argparse.Namespace) -> int:
    try:
        result = diag.diagnose(args.url, profile_slug=args.profile, run_id=args.run, fetch=not args.no_fetch)
    except runs.RunNotFoundError as exc:
        return fail("run_not_found", str(exc))
    except FileNotFoundError as exc:
        return fail("profile_not_found", str(exc))
    emit(diag.to_dict(result))
    return 0


def register(subparsers) -> None:
    """Register the ``ultron diagnose`` command."""
    parser = subparsers.add_parser("diagnose", help="explain why a job URL did not show up")
    parser.add_argument("url")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--run")
    parser.add_argument("--no-fetch", action="store_true", help="do not download the URL for an offline replay")
    parser.set_defaults(func=_run)

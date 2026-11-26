"""CLI for optional, local SearXNG management."""

import argparse

from ultron.commands.output import emit, fail
from ultron.infra.searxng import LifecycleError, manage


def _run(args: argparse.Namespace) -> int:
    try:
        result = manage(args.searxng_action, port=getattr(args, "port", None))
    except LifecycleError as error:
        return fail("searxng_error", str(error), code=1)
    except (OSError, ValueError):
        # Lifecycle errors contain controlled text; filesystem exceptions can carry local paths.
        return fail("searxng_error", "Could not manage the local SearXNG service.", code=1)
    emit(result)
    return int(args.searxng_action in {"up", "restart"} and not result["json_healthy"])


def register(subparsers) -> None:
    """Register local SearXNG lifecycle subcommands."""
    parser = subparsers.add_parser("searxng", help="manage the optional local web-search service")
    actions = parser.add_subparsers(dest="searxng_action", required=True)
    for action in ("up", "down", "restart", "status", "diagnose"):
        command = actions.add_parser(action)
        if action in {"up", "restart"}:
            command.add_argument("--port", type=int, help="loopback port (default 8888)")
        command.set_defaults(func=_run)

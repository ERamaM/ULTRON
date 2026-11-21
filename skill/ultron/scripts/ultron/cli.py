"""ULTRON command line entry point."""

from __future__ import annotations

import argparse
import sys
import traceback

from ultron import __version__
from ultron.commands import cover_letter as cover_letter_cmd
from ultron.commands import cv as cv_cmd
from ultron.commands import diagnose as diagnose_cmd
from ultron.commands import doctor as doctor_cmd
from ultron.commands import export as export_cmd
from ultron.commands import filter as filter_cmd
from ultron.commands import import_mark3 as import_mark3_cmd
from ultron.commands import jobs as jobs_cmd
from ultron.commands import maintenance as maintenance_cmd
from ultron.commands import profile as profile_cmd
from ultron.commands import runs as runs_cmd
from ultron.commands import salary as salary_cmd
from ultron.commands import saved as saved_cmd
from ultron.commands import seeds as seeds_cmd
from ultron.commands import stages as stages_cmd
from ultron.commands import update as update_cmd
from ultron.commands.output import emit, fail

__all__ = ["COMMAND_MODULES", "build_parser", "emit", "fail", "main"]

# Stage modules that expose ``register(subparsers)``; later tasks append here.
COMMAND_MODULES: list = [profile_cmd, runs_cmd, seeds_cmd, stages_cmd, filter_cmd, jobs_cmd, saved_cmd, diagnose_cmd, cv_cmd, cover_letter_cmd, salary_cmd, export_cmd, maintenance_cmd, doctor_cmd, import_mark3_cmd, update_cmd]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise _UsageError(message)


class _UsageError(Exception):
    pass


def _cmd_version(args: argparse.Namespace) -> int:
    result = {"ultron": __version__}
    if args.check_update:
        from ultron.update.notice import release_notice

        if (notice := release_notice(__version__)) is not None:
            result["update_available"] = notice
    emit(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="ultron", description="ULTRON - Universal Locator & Triage of Roles, Openings and Niches")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    version = subparsers.add_parser("version", help="print the ULTRON version")
    version.add_argument("--check-update", action="store_true", help="also check for a newer published release")
    version.set_defaults(func=_cmd_version)
    for module in COMMAND_MODULES:
        module.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(argv)
    except _UsageError as exc:
        return fail("usage", str(exc))
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - stdout must stay one JSON object even on a bug or bad input
        traceback.print_exc(file=sys.stderr)
        return fail("internal_error", f"{type(exc).__name__}: {exc}", code=1)


if __name__ == "__main__":
    sys.exit(main())

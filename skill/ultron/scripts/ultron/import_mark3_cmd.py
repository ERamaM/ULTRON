"""``ultron import-mark3 <path> [--dry-run]`` command."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from ultron import import_mark3
from ultron.output import emit, fail
from ultron.seeds import SeedFileError


def _run(args: argparse.Namespace) -> int:
    try:
        result = import_mark3.import_mark3(Path(args.path), dry_run=args.dry_run)
    except FileNotFoundError as exc:
        return fail("mark3_not_found", str(exc))
    except (sqlite3.DatabaseError, SeedFileError) as exc:
        return fail("import_failed", str(exc))
    emit(result)
    return 0


def register(subparsers) -> None:
    parser = subparsers.add_parser("import-mark3", help="one-time import of profiles, seeds and history from MARK III")
    parser.add_argument("path", help="MARK III data folder (or its checkout, which uses <path>/data)")
    parser.add_argument("--dry-run", action="store_true", help="count what would be imported; write nothing")
    parser.set_defaults(func=_run)

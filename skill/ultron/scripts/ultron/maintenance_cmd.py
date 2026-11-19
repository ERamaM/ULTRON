"""``ultron data status|backup|check|restore|cleanup|delete-run|purge``."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from ultron import maintenance
from ultron.output import emit, fail


def _guard(fn: Callable[[argparse.Namespace], dict]) -> Callable[[argparse.Namespace], int]:
    def wrapper(args: argparse.Namespace) -> int:
        try:
            emit(fn(args))
        except maintenance.MaintenanceError as exc:
            return fail(exc.code, exc.detail)
        except OSError as exc:
            return fail("io_error", str(exc))
        return 0

    return wrapper


def register(subparsers) -> None:
    root = subparsers.add_parser("data", help="status, backup and cleanup of local ULTRON data")
    sub = root.add_subparsers(dest="data_command", required=True, parser_class=type(root))
    sub.add_parser("status", help="paths, sizes, counts").set_defaults(func=_guard(lambda a: maintenance.status()))
    sub.add_parser("check", help="integrity check and run/ledger consistency").set_defaults(
        func=_guard(lambda a: maintenance.check())
    )
    sub.add_parser("backup", help="snapshot the ledger (keeps the last 5)").set_defaults(
        func=_guard(lambda a: {"backup": str(maintenance.backup())})
    )
    p = sub.add_parser("restore", help="replace the ledger with a backup (preview unless --yes)")
    p.add_argument("path")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_guard(lambda a: maintenance.restore(Path(a.path), yes=a.yes)))
    p = sub.add_parser("cleanup", help="delete finished runs older than N days (preview unless --execute)")
    p.add_argument("--days", type=int, required=True)
    p.add_argument("--execute", action="store_true")
    p.set_defaults(func=_guard(lambda a: maintenance.cleanup(a.days, execute=a.execute)))
    p = sub.add_parser("delete-run", help="delete one run folder (preview unless --yes)")
    p.add_argument("run_id")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=_guard(lambda a: maintenance.delete_run(a.run_id, yes=a.yes)))
    p = sub.add_parser("purge", help="delete all local data except profiles/venv (preview unless --yes)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--include-backups", action="store_true")
    p.add_argument("--force", action="store_true", help="purge even with a recent unfinished run")
    p.set_defaults(func=_guard(lambda a: maintenance.purge(yes=a.yes, include_backups=a.include_backups, force=a.force)))

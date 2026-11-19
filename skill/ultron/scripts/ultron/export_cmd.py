"""``ultron export xlsx``."""

from __future__ import annotations

import argparse

from ultron import runs, xlsx
from ultron.output import emit, fail


def _xlsx(args: argparse.Namespace) -> int:
    try:
        path = xlsx.export_run_to_excel(args.run, args.out)
    except runs.RunNotFoundError as exc:
        return fail("run_not_found", str(exc))
    except xlsx.XlsxUnavailable as exc:
        return fail("xlsx_unavailable", str(exc))
    except (OSError, ValueError) as exc:
        return fail("export_failed", str(exc))
    emit({"run_id": args.run, "path": str(path)})
    return 0


def register(subparsers) -> None:
    export = subparsers.add_parser("export", help="export run results")
    sub = export.add_subparsers(dest="export_command", required=True, parser_class=type(export))
    parser = sub.add_parser("xlsx", help="write the run as an Excel workbook")
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", default=None)
    parser.set_defaults(func=_xlsx)

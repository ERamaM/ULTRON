"""``ultron export xlsx``, ``ultron export html``, and ``ultron export web``."""

from __future__ import annotations

import argparse
import sys

from ultron.commands.output import emit, fail
from ultron.exports import html_report, web_server, xlsx
from ultron.storage import runs


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


def _html(args: argparse.Namespace) -> int:
    try:
        path = html_report.export_run_to_html(args.run, args.out)
    except runs.RunNotFoundError as exc:
        return fail("run_not_found", str(exc))
    except (OSError, ValueError) as exc:
        return fail("export_failed", str(exc))
    emit({"run_id": args.run, "path": str(path)})
    return 0


def _web(args: argparse.Namespace) -> int:
    def _on_ready(info: dict) -> None:
        emit(info)
        sys.stdout.flush()
        origin = info["url"].split("#", 1)[0]
        print(f"Serving ULTRON ledger on {origin} (Ctrl+C to stop)", file=sys.stderr, flush=True)

    try:
        web_server.serve_ledger(
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
            run_id=args.run,
            profile=args.profile,
            editable=args.editable,
            on_ready=_on_ready,
        )
    except (OSError, ValueError) as exc:
        return fail("export_failed", str(exc))
    return 0


def register(subparsers) -> None:
    """Register ``ultron export`` subcommands."""
    export = subparsers.add_parser("export", help="export run results")
    sub = export.add_subparsers(dest="export_command", required=True, parser_class=type(export))
    parser = sub.add_parser("xlsx", help="write the run as an Excel workbook")
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", default=None)
    parser.set_defaults(func=_xlsx)
    page = sub.add_parser("html", help="write the run as a self-contained offline HTML report")
    page.add_argument("--run", required=True)
    page.add_argument("--out", default=None)
    page.set_defaults(func=_html)
    web = sub.add_parser("web", help="serve $ULTRON_HOME/ultron.sqlite3 in a local web viewer (read-only by default)")
    web.add_argument("--host", default="127.0.0.1", help="loopback bind address (default 127.0.0.1)")
    web.add_argument("--port", type=int, default=0, help="TCP port to listen on (0 picks an open port)")
    web.add_argument("--run", default=None, help="pre-filter the dashboard to one run ID")
    web.add_argument("--profile", default=None, help="pre-filter the dashboard to one profile slug")
    web.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    web.add_argument("--editable", action="store_true", help="enable local job and watchlist edits")
    web.set_defaults(func=_web)

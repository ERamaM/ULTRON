"""``ultron filter`` and ``ultron evaluate pending|record``."""

from __future__ import annotations

from pathlib import Path

from ultron.commands.output import emit, fail, fail_stage_order
from ultron.exports import html_report, xlsx
from ultron.stages.evaluate import RecordError, pending_batch, record_evaluations
from ultron.stages.filter import filter_run
from ultron.stages.finalize import finalize
from ultron.storage import runs


def _guard(func, *args) -> int:
    try:
        payload = func(*args)
    except runs.RunNotFoundError as exc:
        return fail("run_not_found", str(exc))
    except runs.StageOrderError as exc:
        return fail_stage_order(exc)
    except RecordError as exc:
        emit({"error": "invalid_evaluation", "detail": exc.payload["reason"], **exc.payload})
        return 2
    except (OSError, ValueError) as exc:
        return fail("stage_failed", str(exc))
    emit(payload)
    return 0


def _finalize(run_id: str, with_xlsx: bool = False, with_html: bool = False) -> dict:
    payload = finalize(run_id)
    if with_xlsx:
        try:
            payload["outputs"]["results_xlsx"] = str(xlsx.export_run_to_excel(run_id))
        except (xlsx.XlsxUnavailable, OSError, ValueError) as exc:
            # finalize already committed; report the export failure without losing its payload.
            payload["xlsx_error"] = str(exc)
    if with_html:
        try:
            payload["outputs"]["results_html"] = str(html_report.export_run_to_html(run_id))
        except (OSError, ValueError) as exc:
            payload["html_error"] = str(exc)
    return payload


def register(subparsers) -> None:
    """Register ``ultron filter`` and ``ultron evaluate`` commands."""
    flt = subparsers.add_parser("filter", help="deterministic filter and ambiguity queue")
    flt.add_argument("--run", required=True)
    flt.set_defaults(func=lambda a: _guard(filter_run, a.run))

    ev = subparsers.add_parser("evaluate", help="agent evaluations of ambiguous postings")
    sub = ev.add_subparsers(dest="evaluate_command", required=True, parser_class=type(ev))
    pend = sub.add_parser("pending", help="next compact batch of ambiguous postings")
    pend.add_argument("--run", required=True)
    pend.add_argument("--limit", type=int, default=10)
    pend.set_defaults(func=lambda a: _guard(pending_batch, a.run, a.limit))
    rec = sub.add_parser("record", help="record agent evaluations (all-or-nothing)")
    rec.add_argument("--run", required=True)
    rec.add_argument("--from", dest="source", required=True)
    rec.set_defaults(func=lambda a: _guard(record_evaluations, a.run, Path(a.source)))

    fin = subparsers.add_parser("finalize", help="merge verdicts, rank, cap, record history and export")
    fin.add_argument("--run", required=True)
    fin.add_argument("--xlsx", action="store_true", help="also write results.xlsx")
    fin.add_argument("--html", action="store_true", help="also write results.html (self-contained, offline)")
    fin.set_defaults(func=lambda a: _guard(_finalize, a.run, a.xlsx, a.html))

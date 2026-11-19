"""Export a run's results to a styled multi-sheet Excel workbook (adapted from MARK III).

openpyxl is an optional extra (``xlsx``) and is imported lazily so the rest of
ULTRON works without it. Inputs are the run's ``results.json`` plus
``discarded.jsonl`` and ``finalize_discards.jsonl`` (the Discards sheet).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ultron import runs

_HEADER_FILL = "1F3A5F"
_HEADER_FONT = "FFFFFF"
_TITLE_FONT = "1F3A5F"
_ACCENT_FILL = "E8EEF6"
_BORDER_RGB = "D0D7E2"
_KEEP_FILL = "E6F4EA"
_DISCARD_FILL = "FCE8E6"
_LABEL_FILL = "F1F4F9"

_MISSING = "openpyxl not installed: reinstall ULTRON with the xlsx extra"


class XlsxUnavailable(RuntimeError):
    """openpyxl (the ``xlsx`` extra) is not installed."""


def _col_letter(idx: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(idx)


def _neutralize_formula(value: str) -> str:
    """Keep untrusted spreadsheet text from becoming an Excel formula."""
    return "'" + value if re.match(r"^\s*[=+\-@]", value) else value


def _norm(value: Any) -> str:
    """Flatten a JSON value to a human-friendly, formula-safe string."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return _neutralize_formula(" | ".join(str(item) for item in value))
    if isinstance(value, dict):
        return _neutralize_formula(json.dumps(value, ensure_ascii=False))
    return _neutralize_formula(str(value))


def _cell_value(value: Any) -> Any:
    """Numbers and bools are written natively; only text is neutralised."""
    if isinstance(value, (bool, int, float)):
        return value
    return _norm(value)


def _count_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# (key, header, width, is_url)
_ROLE_COLUMNS = [
    ("company", "Company", 26, False),
    ("title", "Title", 44, False),
    ("location", "Location", 22, False),
    ("remote_policy", "Remote", 14, False),
    ("eligibility", "Eligibility", 16, False),
    ("eligibility_evidence", "Eligibility Evidence", 40, False),
    ("freshness", "Freshness", 14, False),
    ("fit_signals", "Fit Signals", 36, False),
    ("evaluation_verdict", "Verdict", 12, False),
    ("source_type", "Source Type", 14, False),
    ("source_label", "Source", 22, False),
    ("match_score", "Match Score", 12, False),
    ("match_band", "Match Band", 14, False),
    ("matched_requirements", "Matched", 32, False),
    ("missing_requirements", "Missing", 32, False),
    ("uncertain_requirements", "Uncertain", 32, False),
    ("apply_url", "Apply URL", 52, True),
    ("is_new", "New", 8, False),
]

_DISCARD_COLUMNS = [
    ("reason", "Reason", 26, False),
    ("stage", "Stage", 16, False),
    ("company", "Company", 24, False),
    ("title", "Title", 42, False),
    ("location", "Location", 22, False),
    ("remote_text", "Remote", 18, False),
    ("detail", "Detail", 48, False),
    ("apply_url", "URL", 52, True),
]


def _styles():
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    thin = Side(style="thin", color=_BORDER_RGB)
    return {
        "header_font": Font(bold=True, color=_HEADER_FONT, size=11),
        "header_fill": PatternFill("solid", fgColor=_HEADER_FILL),
        "header_align": Alignment(horizontal="left", vertical="center", wrap_text=True),
        "cell_align": Alignment(horizontal="left", vertical="top", wrap_text=True),
        "cell_align_nowrap": Alignment(horizontal="left", vertical="center"),
        "border": Border(left=thin, right=thin, top=thin, bottom=thin),
        "zebra": PatternFill("solid", fgColor=_ACCENT_FILL),
        "link_font": Font(color="0563C1", underline="single"),
        "title_font": Font(bold=True, size=16, color=_TITLE_FONT),
        "subtitle_font": Font(italic=True, size=10, color="6B7280"),
        "keep_fill": PatternFill("solid", fgColor=_KEEP_FILL),
        "discard_fill": PatternFill("solid", fgColor=_DISCARD_FILL),
        "label_fill": PatternFill("solid", fgColor=_LABEL_FILL),
        "label_font": Font(bold=True, color="374151"),
    }


def _is_web_link(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _write_table(ws, columns, rows, styles, *, verdict_key: str | None = None) -> None:
    start_row = 1
    for idx, (_key, _label, width, _is_url) in enumerate(columns, start=1):
        ws.column_dimensions[_col_letter(idx)].width = width
    for idx, (_key, label, _width, _is_url) in enumerate(columns, start=1):
        cell = ws.cell(row=start_row, column=idx, value=label)
        cell.font = styles["header_font"]
        cell.fill = styles["header_fill"]
        cell.alignment = styles["header_align"]
        cell.border = styles["border"]
    ws.row_dimensions[start_row].height = 22

    for r_offset, record in enumerate(rows, start=1):
        excel_row = start_row + r_offset
        zebra = r_offset % 2 == 0
        verdict = (record.get(verdict_key) if verdict_key else "") or ""
        for c_idx, (key, _label, _width, is_url) in enumerate(columns, start=1):
            value = _cell_value(record.get(key))
            cell = ws.cell(row=excel_row, column=c_idx, value=value)
            cell.border = styles["border"]
            cell.alignment = styles["cell_align"]
            if is_url and isinstance(value, str) and _is_web_link(value):
                cell.hyperlink = value
                cell.font = styles["link_font"]
                cell.alignment = styles["cell_align_nowrap"]
            if zebra:
                cell.fill = styles["zebra"]
        if verdict_key:
            v_idx = next((i for i, c in enumerate(columns, start=1) if c[0] == verdict_key), None)
            if v_idx is not None:
                fill = styles["keep_fill"] if str(verdict).lower() == "keep" else styles["discard_fill"]
                ws.cell(row=excel_row, column=v_idx).fill = fill

    last_row = start_row + len(rows)
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    ws.auto_filter.ref = f"{_col_letter(1)}{start_row}:{_col_letter(len(columns))}{max(last_row, start_row)}"
    if not rows:
        empty = ws.cell(row=start_row + 1, column=1, value="- no rows -")
        empty.font = styles["subtitle_font"]


def _build_summary_sheet(ws, run: dict, summary: dict, counts: dict[str, int], styles: dict) -> None:
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 60

    title = ws.cell(row=1, column=1, value="ULTRON - Run Report")
    title.font = styles["title_font"]
    ws.merge_cells("A1:B1")
    subtitle = ws.cell(row=2, column=1, value=_norm(run.get("prompt")) or "(no prompt)")
    subtitle.font = styles["subtitle_font"]
    ws.merge_cells("A2:B2")
    ws.row_dimensions[1].height = 26

    dc = summary.get("discard_counts")
    if not isinstance(dc, dict):
        dc = {}
    total_discards = int(sum(_count_value(v) for v in dc.values()))
    discard_label: Any = counts["discards"]
    if total_discards > counts["discards"]:
        discard_label = f"{counts['discards']} listed of {total_discards} total"

    fields: list[tuple[str, Any]] = [
        ("Run ID", summary.get("run_id") or run.get("run_id")),
        ("Profile", run.get("profile_slug")),
        ("Preset", run.get("preset")),
        ("Created", run.get("created_at")),
        ("Started", summary.get("started_at")),
        ("Ended", summary.get("ended_at")),
        ("", ""),
        ("Roles kept", counts["roles"]),
        ("Unsure kept", summary.get("unsure_to_keep_count", 0)),
        ("Discards", discard_label),
        ("Pages opened", summary.get("ats_pages_opened", 0)),
    ]
    row = 4
    for label, value in fields:
        if label == "" and value == "":
            row += 1
            continue
        lcell = ws.cell(row=row, column=1, value=label)
        lcell.fill = styles["label_fill"]
        lcell.font = styles["label_font"]
        lcell.border = styles["border"]
        lcell.alignment = styles["cell_align_nowrap"]
        vcell = ws.cell(row=row, column=2, value=_cell_value(value))
        vcell.border = styles["border"]
        vcell.alignment = styles["cell_align"]
        row += 1

    if dc:
        row += 1
        for col, text in ((1, "Discards by reason"), (2, "Count")):
            hdr = ws.cell(row=row, column=col, value=text)
            hdr.font = styles["header_font"]
            hdr.fill = styles["header_fill"]
            hdr.border = styles["border"]
        row += 1
        for reason, count in sorted(dc.items(), key=lambda kv: (-_count_value(kv[1]), str(kv[0]))):
            rc = ws.cell(row=row, column=1, value=_neutralize_formula(str(reason)))
            rc.border = styles["border"]
            rc.alignment = styles["cell_align_nowrap"]
            cc = ws.cell(row=row, column=2, value=count)
            cc.border = styles["border"]
            row += 1
    ws.freeze_panes = "A4"


def export_run_to_excel(run_id: str, out_path: Path | str | None = None) -> Path:
    """Build a styled workbook for ``run_id`` and return the written path.

    Raises ``XlsxUnavailable`` if openpyxl is missing and ``runs.RunNotFoundError``
    for an unknown run.
    """
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise XlsxUnavailable(_MISSING) from exc

    run = runs.load_run(run_id)
    out_dir = runs.run_dir(run_id)
    results_path = out_dir / "results.json"
    if not results_path.is_file():
        raise FileNotFoundError(f"results.json not found for run {run_id}; run finalize first")
    results = json.loads(results_path.read_text(encoding="utf-8"))
    rows = list(results.get("rows") or [])
    discards = [*runs.read_jsonl(out_dir / "discarded.jsonl"), *runs.read_jsonl(out_dir / "finalize_discards.jsonl")]
    counts = {"roles": len(rows), "discards": len(discards)}

    styles = _styles()
    wb = Workbook()
    try:
        summary_ws = wb.active
        summary_ws.title = "Summary"
        _build_summary_sheet(summary_ws, run, results.get("summary") or {}, counts, styles)
        _write_table(wb.create_sheet(f"Roles ({len(rows)})"), _ROLE_COLUMNS, rows, styles,
                     verdict_key="evaluation_verdict")
        _write_table(wb.create_sheet(f"Discards ({len(discards)})"), _DISCARD_COLUMNS, discards, styles,
                     verdict_key="reason")
        wb.properties.creator = "ULTRON"
        wb.properties.title = f"ULTRON run {run_id}"

        target = Path(out_path) if out_path else out_dir / "results.xlsx"
        target = target.absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        wb.save(target)
    finally:
        wb.close()
    return target

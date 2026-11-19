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
# Blue/orange instead of green/red so the cues stay distinguishable with color-vision deficiencies;
# every colored cell also carries its value as text.
_KEEP_FILL = "DCE9F7"
_DISCARD_FILL = "FDE7CF"
_LABEL_FILL = "F1F4F9"
_LINK_RGB = "0563C1"
_DATA_BAR_RGB = "3B7DD8"
_NEW_ROW_FILL = "FFF4CC"
# value -> (fill, font) pills, strongest first
_BAND_PILLS = {
    "strong": ("1F5FAD", "FFFFFF"),
    "good": ("D6E6F7", "0B3D73"),
    "borderline": ("FCE3C4", "7A3E00"),
    "ineligible": ("E5E7EB", "374151"),
}
_ELIGIBILITY_PILLS = {
    "confirmed": ("1F5FAD", "FFFFFF"),
    "high": ("D6E6F7", "0B3D73"),
    "medium": ("FCE3C4", "7A3E00"),
    "unknown": ("E5E7EB", "374151"),
}
_HEADER_HEIGHT = 30
_ROW_HEIGHT = 45  # three wrapped lines: every data row has the same height

_MISSING = "openpyxl not installed: reinstall ULTRON with the xlsx extra"


class XlsxUnavailable(RuntimeError):
    """openpyxl (the ``xlsx`` extra) is not installed."""


_NEW_LABEL = "New"


def _col_letter(idx: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(idx)


def _neutralize_formula(value: str) -> str:
    """Make untrusted text safe for a cell: drop control characters, then keep it from becoming a formula.

    openpyxl refuses (``IllegalCharacterError``) the C0 controls XML cannot hold, such as ``\\x08`` or ``\\x1b``.
    They are stripped first, so a leading control character cannot hide a formula prefix.
    """
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    value = ILLEGAL_CHARACTERS_RE.sub("", value)
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


# (key, header, width, is_url); is_url == "open" writes a short "Open" link to the same http(s) URL.
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
    ("match_score", "Match Score", 14, False),
    ("match_band", "Match Band", 14, False),
    ("matched_requirements", "Matched", 32, False),
    ("missing_requirements", "Missing", 32, False),
    ("uncertain_requirements", "Uncertain", 32, False),
    ("apply_url", "Apply URL", 52, True),
    ("is_new", "New", 10, False),
    ("apply_url", "Apply", 10, "open"),
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
        "link_font": Font(color=_LINK_RGB, underline="single"),
        "open_font": Font(color=_LINK_RGB, underline="single", bold=True),
        "cell_align_center": Alignment(horizontal="center", vertical="center"),
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
    ws.row_dimensions[start_row].height = _HEADER_HEIGHT

    for r_offset, record in enumerate(rows, start=1):
        excel_row = start_row + r_offset
        zebra = r_offset % 2 == 0
        verdict = (record.get(verdict_key) if verdict_key else "") or ""
        for c_idx, (key, _label, _width, is_url) in enumerate(columns, start=1):
            value = _cell_value(record.get(key))
            if key == "is_new":
                # A text label renders the same in every Excel locale (booleans show as TRUE/VERDADERO/WAHR).
                value = _NEW_LABEL if record.get(key) is True else None
            if is_url == "open":
                url = value if isinstance(value, str) and _is_web_link(value) else None
                cell = ws.cell(row=excel_row, column=c_idx, value="Open" if url else None)
                cell.border = styles["border"]
                cell.alignment = styles["cell_align_center"]
                if url:
                    cell.hyperlink = url
                    cell.font = styles["open_font"]
                if zebra:
                    cell.fill = styles["zebra"]
                continue
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
        ws.row_dimensions[excel_row].height = _ROW_HEIGHT

    last_row = start_row + len(rows)
    # Keep the header row and the first identifying column visible while scrolling.
    ws.freeze_panes = ws.cell(row=start_row + 1, column=2)
    ws.auto_filter.ref = f"{_col_letter(1)}{start_row}:{_col_letter(len(columns))}{max(last_row, start_row)}"
    if not rows:
        empty = ws.cell(row=start_row + 1, column=1, value="- no rows -")
        empty.font = styles["subtitle_font"]
    _print_setup(ws)
    ws.print_title_rows = "1:1"


def _print_setup(ws) -> None:
    """Landscape and one page wide when printed."""
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True


def _column_letter_for(columns, key: str) -> str:
    return _col_letter(next(i for i, c in enumerate(columns, start=1) if c[0] == key and c[3] != "open"))


def _add_role_cues(ws, columns, row_count: int) -> None:
    """Score data bar, band/eligibility pills and a highlight for new rows (conditional formats)."""
    from openpyxl.formatting.rule import CellIsRule, DataBarRule, FormulaRule
    from openpyxl.styles import Font, PatternFill

    if not row_count:
        return
    last = row_count + 1

    def solid(rgb: str) -> PatternFill:
        return PatternFill("solid", start_color=rgb, end_color=rgb)

    score = _column_letter_for(columns, "match_score")
    ws.conditional_formatting.add(
        f"{score}2:{score}{last}",
        DataBarRule(start_type="num", start_value=0, end_type="num", end_value=100, color=_DATA_BAR_RGB),
    )
    # Pills are added before the new-row highlight so they take priority in their cells.
    for key, pills in (("match_band", _BAND_PILLS), ("eligibility", _ELIGIBILITY_PILLS)):
        col = _column_letter_for(columns, key)
        for value, (fill, font) in pills.items():
            ws.conditional_formatting.add(
                f"{col}2:{col}{last}",
                CellIsRule(operator="equal", formula=[f'"{value}"'], fill=solid(fill), font=Font(color=font, bold=True)),
            )
    new_col = _column_letter_for(columns, "is_new")
    ws.conditional_formatting.add(
        f"A2:{_col_letter(len(columns))}{last}",
        FormulaRule(formula=[f'${new_col}2="{_NEW_LABEL}"'], fill=solid(_NEW_ROW_FILL), font=Font(bold=True)),
    )


def _build_summary_sheet(
    ws, run: dict, summary: dict, counts: dict[str, int], styles: dict, rows: list | None = None
) -> None:
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
            cc = ws.cell(row=row, column=2, value=_cell_value(count))
            cc.border = styles["border"]
            row += 1
    _build_shortlist_block(ws, rows or [], styles)
    ws.freeze_panes = "A4"
    _print_setup(ws)


def _build_shortlist_block(ws, rows: list, styles: dict) -> None:
    """New-since-last-run and per-band counts beside the run facts (columns D:E)."""
    from openpyxl.styles import Border, Side

    ws.column_dimensions["C"].width = 3
    ws.column_dimensions["D"].width = 24
    ws.column_dimensions["E"].width = 10
    records = [r for r in rows if isinstance(r, dict)]
    bands = [str(r.get("match_band") or "") for r in records]
    fields: list[tuple[str, int, str | None]] = [
        ("New since last run", sum(1 for r in records if r.get("is_new") is True), None),
    ]
    for band, (fill, _font) in _BAND_PILLS.items():
        fields.append((band.capitalize(), bands.count(band), fill))
    fields.append(("Unscored", sum(1 for b in bands if b not in _BAND_PILLS), None))

    for col, text in ((4, "Shortlist"), (5, "Count")):
        hdr = ws.cell(row=4, column=col, value=text)
        hdr.font = styles["header_font"]
        hdr.fill = styles["header_fill"]
        hdr.border = styles["border"]
    border = styles["border"]
    for offset, (label, count, swatch) in enumerate(fields, start=5):
        lcell = ws.cell(row=offset, column=4, value=label)
        lcell.fill = styles["label_fill"]
        lcell.font = styles["label_font"]
        lcell.border = border
        if swatch:  # a band-colored left edge ties the count to the pill color on the Roles sheet
            lcell.border = Border(left=Side(style="thick", color=swatch), right=border.right,
                                  top=border.top, bottom=border.bottom)
        ccell = ws.cell(row=offset, column=5, value=count)
        ccell.border = border
        ccell.alignment = styles["cell_align_center"]


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
        _build_summary_sheet(summary_ws, run, results.get("summary") or {}, counts, styles, rows)
        roles_ws = wb.create_sheet(f"Roles ({len(rows)})")
        _write_table(roles_ws, _ROLE_COLUMNS, rows, styles, verdict_key="evaluation_verdict")
        _add_role_cues(roles_ws, _ROLE_COLUMNS, len(rows))
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

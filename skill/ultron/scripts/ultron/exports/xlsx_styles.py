"""Shared workbook palette, table rendering and safe cell values."""

from __future__ import annotations

import json
from datetime import date
from math import ceil
from typing import Any, NamedTuple

from ultron.exports import sanitize


class Column(NamedTuple):
    """A table field, visible heading, width, and optional hyperlink behavior."""

    field: str
    label: str
    width: int
    link: str = ""


def column_letter(index: int) -> str:
    """Convert a one-based Excel column index to its letter."""
    from openpyxl.utils import get_column_letter

    return get_column_letter(index)


def cell_value(value: Any) -> Any:
    """Write numbers and dates natively and redact and neutralize user text."""
    if value is None or isinstance(value, (bool, int, float, date)):
        return value
    if isinstance(value, (list, tuple)):
        value = " | ".join(str(item) for item in value)
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False)
    text = sanitize.clean_multiline_text(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text or None


def row_height(cells: list[tuple[object, int]]) -> int:
    """Estimate wrapped lines from the visible values and column widths."""
    lines = max((sum(max(1, ceil(len(line) / max(1, width - 2))) for line in value.split("\n"))
                 for value, width in cells if isinstance(value, str)), default=1)
    return max(22, min(96, 15 * lines + 8))


def write_table(ws, columns, rows, styles, *, table_name: str, freeze: str = "A2") -> None:
    """Write a styled, printable table with native filtering on nonempty data."""
    from openpyxl.worksheet.table import Table, TableStyleInfo

    for index, col in enumerate(columns, start=1):
        ws.column_dimensions[column_letter(index)].width = col.width
        cell = ws.cell(1, index, col.label)
        cell.font = styles["header_font"]
        cell.fill = styles["header_fill"]
        cell.alignment = styles["header_align"]
    ws.row_dimensions[1].height = _HEADER_HEIGHT
    for index, record in enumerate(rows, start=2):
        values = [_write_table_cell(ws, index, pos, col, record, styles)
                  for pos, col in enumerate(columns, start=1)]
        ws.row_dimensions[index].height = row_height(values)
    if rows:
        table = Table(displayName=table_name, ref=f"A1:{column_letter(len(columns))}{len(rows) + 1}")
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(table)
    else:
        ws.cell(2, 1, "- no rows -").font = styles["subtitle_font"]
    ws.freeze_panes = freeze
    ws.sheet_view.showGridLines = False
    print_setup(ws)
    ws.print_title_rows = "1:1"


def _write_table_cell(ws, row: int, index: int, col: Column, record: dict, styles: dict) -> tuple[object, int]:
    raw = record.get(col.field)
    url = sanitize.safe_http_url(raw if col.link == "url" else record.get("apply_url")) if col.link else None
    value = ("Apply \u2197" if url else None) if col.link == "apply" else cell_value(raw)
    cell = ws.cell(row, index, value)
    if url:
        cell.hyperlink = url
        cell.font = styles["open_font"] if col.link == "apply" else styles["link_font"]
    elif col.field == "company":
        cell.font = styles["label_font"]
    if col.field == "evaluation_verdict":
        cell.fill = styles["keep_fill"] if raw == "keep" else styles["discard_fill"]
    if isinstance(value, date):
        cell.number_format = "yyyy-mm-dd"
    cell.alignment = styles["cell_align_center"] if col.width <= 15 else styles["cell_align"]
    return value, col.width


_HEADER_FILL = "1F3A5F"
_HEADER_FONT = "FFFFFF"
_TITLE_FONT = "1F3A5F"
# Blue/orange instead of green/red so the cues stay distinguishable with color-vision deficiencies;
# every colored cell also carries its value as text.
_KEEP_FILL = "DCE9F7"
_DISCARD_FILL = "FDE7CF"
_LABEL_FILL = "F1F4F9"
_LINK_RGB = "0563C1"
DATA_BAR_RGB = "3B7DD8"
NEW_ROW_FILL = "FFF4CC"
# value -> (fill, font) pills, strongest first
BAND_PILLS = {
    "strong": ("1F5FAD", "FFFFFF"),
    "good": ("D6E6F7", "0B3D73"),
    "borderline": ("FCE3C4", "7A3E00"),
    "ineligible": ("E5E7EB", "374151"),
}
ELIGIBILITY_PILLS = {
    "confirmed": ("1F5FAD", "FFFFFF"),
    "high": ("D6E6F7", "0B3D73"),
    "medium": ("FCE3C4", "7A3E00"),
    "unknown": ("E5E7EB", "374151"),
}
_HEADER_HEIGHT = 30

def workbook_styles():
    """Build the workbook palette and cell styles."""
    from openpyxl.styles import Alignment, Font, PatternFill

    return {
        "header_font": Font(bold=True, color=_HEADER_FONT, size=11),
        "header_fill": PatternFill("solid", fgColor=_HEADER_FILL),
        "header_align": Alignment(horizontal="left", vertical="center", wrap_text=True),
        "cell_align": Alignment(horizontal="left", vertical="top", wrap_text=True),
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


def print_setup(ws) -> None:
    """Landscape and one page wide when printed."""
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

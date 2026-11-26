"""Executive dashboard for the Excel export with native formulas and charts."""

from __future__ import annotations

from collections import Counter

from ultron.exports import sanitize, view_model
from ultron.exports.xlsx_styles import cell_value, column_letter, print_setup

_COLORS = ("1F5FAD", "78A9D6", "61758F", "D09243", "CBD5E1", "0C635D")


def _kpi_formulas(sheet: str, columns: dict[str, str], count: int, mode: str) -> list[tuple[str, str | int]]:
    def formula(field: str, criterion: str | None = None) -> str | int:
        column = columns.get(field)
        if not count or not column:
            return 0
        ref = f"'{sheet}'!{column}2:{column}{count + 1}"
        return f'=COUNTIF({ref},"{criterion}")' if criterion else f"=COUNTA({ref})"

    top = ("Strong Matches", formula("match_band", "strong")) if mode == "scored" else (
        "Top Priority", formula("priority", "P1"),
    )
    return [
        ("Total Roles", formula("title")), ("New Roles", formula("new", "New")),
        top, ("With Salary", formula("salary")),
        *((label, formula("status", label)) for label in ("Shortlisted", "Applied", "Interview", "Rejected")),
    ]


def _banner(ws, run: dict, summary: dict, styles: dict) -> None:
    subtitle = [run.get("profile_slug"), run.get("preset"), summary.get("ended_at") or run.get("finished_at")]
    for index, text in enumerate((
        "ULTRON \u2014 Job Search Report",
        " \u00b7 ".join(sanitize.clean_text(item) for item in subtitle if item),
        run.get("prompt") or run.get("description"),
    ), start=1):
        ws.merge_cells(start_row=index, start_column=1, end_row=index, end_column=8)
        cell = ws.cell(index, 1, cell_value(text))
        cell.font = styles["title_font"] if index == 1 else styles["subtitle_font"]
        cell.alignment = styles["cell_align"]
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[3].height = 30


def _write_kpis(ws, formulas: list[tuple[str, str | int]], styles: dict) -> None:
    for index, (label, value) in enumerate(formulas, start=1):
        header = ws.cell(5, index, label)
        header.fill = styles["header_fill"]
        header.font = styles["header_font"]
        header.alignment = styles["cell_align_center"]
        metric = ws.cell(6, index, value)
        metric.font = styles["title_font"]
        metric.fill = styles["label_fill"]
        metric.alignment = styles["cell_align_center"]
    ws.row_dimensions[5].height = 25
    ws.row_dimensions[6].height = 26


def _write_rows(ws, row: int, col: int, title: str, fields: list[tuple[str, object]], styles: dict) -> None:
    for offset, label in enumerate((title, "Value")):
        header = ws.cell(row, col + offset, label)
        header.fill = styles["header_fill"]
        header.font = styles["header_font"]
    for index, (label, value) in enumerate(fields, start=row + 1):
        ws.cell(index, col, cell_value(label)).font = styles["label_font"]
        ws.cell(index, col + 1, cell_value(value)).alignment = styles["cell_align"]


def _chart(ws, *, title: str, fields: list[tuple[str, int]], data_row: int, anchor: str, doughnut: bool,
           styles: dict) -> None:
    from openpyxl.chart import BarChart, DoughnutChart, Reference
    from openpyxl.chart.series import DataPoint

    _write_rows(ws, data_row, 4, title, fields, styles)
    chart = DoughnutChart() if doughnut else BarChart()
    chart.title = title
    chart.width = 16
    chart.height = 7.5
    if not doughnut:
        chart.type = "bar"
        chart.legend = None
    chart.add_data(Reference(ws, min_col=5, min_row=data_row + 1, max_row=data_row + len(fields)))
    chart.set_categories(Reference(ws, min_col=4, min_row=data_row + 1, max_row=data_row + len(fields)))
    chart.series[0].data_points = [
        DataPoint(idx=index, spPr=_point_style(_COLORS[index % len(_COLORS)])) for index in range(len(fields))
    ]
    ws.add_chart(chart, anchor)


def _point_style(color: str):
    from openpyxl.chart.shapes import GraphicalProperties

    return GraphicalProperties(solidFill=color)


def _breakdowns(ws, rows: list[dict], companies: list[dict], mode: str, styles: dict) -> None:
    if mode == "scored":
        counts = Counter(sanitize.normalize_token(row.get("match_band"), view_model.MATCH_BANDS, default="unscored")
                         for row in rows)
        labels = [(band.title(), counts[band]) for band in (*view_model.MATCH_BANDS, "unscored")]
        ranking = "Roles by Match Band"
    else:
        counts = Counter(view_model.normalize_priority(row.get("priority")) for row in rows)
        labels = [(f"P{level}", counts[level]) for level in (1, 2, 3)]
        ranking = "Roles by Priority"
    remote = Counter(view_model.remote_label(row.get("remote_policy")) or "Unspecified" for row in rows)
    remote_labels = [view_model.remote_label(f"remote_{scope}") for scope in ("spain", "eu", "europe", "emea", "other")]
    _chart(ws, title=ranking, fields=labels, data_row=8, anchor="G8", doughnut=True, styles=styles)
    _chart(ws, title="Roles by Remote Policy",
           fields=[(label, remote[label]) for label in (*remote_labels, "Unspecified")],
           data_row=24, anchor="G24", doughnut=False, styles=styles)
    _chart(ws, title="Top Companies", fields=[(row["company"], row["open_roles"]) for row in companies[:8]],
           data_row=40, anchor="G40", doughnut=False, styles=styles)


def _count(value: object) -> int:
    try:
        return int(sanitize.clean_text(value))
    except ValueError:
        return 0


def _run_facts(ws, run: dict, summary: dict, discard_count: int, styles: dict) -> None:
    discards = summary.get("discard_counts")
    reasons = list(discards.items()) if isinstance(discards, dict) else []
    total_discards = sum(_count(count) for _, count in reasons)
    discard_label = f"{discard_count} listed of {total_discards} total" if total_discards > discard_count else discard_count
    _write_rows(ws, 8, 1, "Run Facts", [
        ("Run ID", summary.get("run_id") or run.get("run_id")),
        ("Profile", run.get("profile_slug")), ("Preset", run.get("preset")), ("Created", run.get("created_at")),
        ("Started", summary.get("started_at")), ("Ended", summary.get("ended_at")),
        ("Unsure kept", summary.get("unsure_to_keep_count", 0)),
        ("Pages opened", summary.get("ats_pages_opened", 0)), ("Discards", discard_label),
    ], styles)
    if reasons:
        _write_rows(ws, 20, 1, "Discards by Reason", sorted(reasons, key=lambda row: (-_count(row[1]), row[0])), styles)


def build_dashboard(ws, run: dict, summary: dict, rows: list[dict], companies: list[dict], discard_count: int,
                    roles_sheet: str, columns: dict[str, str], mode: str, styles: dict) -> None:
    """Render the workbook's Summary sheet with formula-driven status counters."""
    ws.sheet_view.showGridLines = False
    for index, width in enumerate((21, 34, 21, 26, 16, 16, 16, 16, 16, 16), start=1):
        ws.column_dimensions[column_letter(index)].width = width
    _banner(ws, run, summary, styles)
    _write_kpis(ws, _kpi_formulas(roles_sheet, columns, len(rows), mode), styles)
    _run_facts(ws, run, summary, discard_count, styles)
    if rows:
        _breakdowns(ws, rows, companies, mode, styles)
    ws.freeze_panes = "A7"
    print_setup(ws)

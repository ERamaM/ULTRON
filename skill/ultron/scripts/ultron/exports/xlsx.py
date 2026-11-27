"""Export a run's results as an interactive Excel workbook."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from io import BytesIO
from pathlib import Path

from ultron.exports import sanitize, view_model
from ultron.exports.xlsx_dashboard import build_dashboard
from ultron.exports.xlsx_styles import (
    BAND_PILLS,
    DATA_BAR_RGB,
    ELIGIBILITY_PILLS,
    NEW_ROW_FILL,
    Column,
    column_letter,
    workbook_styles,
    write_table,
)
from ultron.infra.fs import atomic_create_bytes
from ultron.storage import runs

_MISSING = "openpyxl not installed: reinstall ULTRON with the xlsx extra"
_STATUS_PILLS = {
    "Shortlisted": ("D6E6F7", "0B3D73"),
    "Applied": ("1F5FAD", "FFFFFF"),
    "Interview": ("0C635D", "FFFFFF"),
    "Rejected": ("E5E7EB", "374151"),
}
_PRIORITY_PILLS = {"P1": ("1F5FAD", "FFFFFF"), "P2": ("D6E6F7", "0B3D73"), "P3": ("E5E7EB", "374151")}

_CORE_COLUMNS = (
    Column("rank", "#", 6), Column("new", "New", 8), Column("company", "Company", 24),
    Column("title", "Title", 42, "title"), Column("location", "Location", 22), Column("remote", "Remote", 16),
    Column("salary", "Salary", 20), Column("posted", "Posted", 13), Column("eligibility", "Eligibility", 14),
    Column("eligibility_evidence", "Eligibility Evidence", 38), Column("signals", "Fit Signals", 34),
    Column("evaluation_verdict", "Verdict", 11),
)
_END_COLUMNS = (
    Column("status", "My Status", 15), Column("notes", "My Notes", 30),
    Column("apply", "Apply", 11, "apply"), Column("apply_url", "Apply URL", 44, "url"),
)
_SCORE_COLUMNS = (Column("match_score", "Match Score", 14), Column("match_band", "Match Band", 14))
_OPTIONAL_COLUMNS = (
    Column("matched_requirements", "Matched", 32), Column("missing_requirements", "Missing", 32),
    Column("uncertain_requirements", "Uncertain", 32), Column("freshness", "Freshness", 24),
    Column("source_type", "Source Type", 14), Column("source_label", "Source", 22),
)
_DATE_COLUMNS = (
    Column("title", "Title", 42), Column("company", "Company", 24),
    Column("published_at", "Published", 24), Column("updated_at", "Updated", 24),
    Column("first_seen", "First Seen", 24), Column("unverified_date", "Unverified Source Date", 24),
)
_DISCARD_COLUMNS = (
    Column("reason", "Reason", 26), Column("stage", "Stage", 16), Column("company", "Company", 24),
    Column("title", "Title", 42), Column("location", "Location", 22), Column("remote_text", "Remote", 18),
    Column("detail", "Detail", 48), Column("apply_url", "URL", 52, "url"),
)


class XlsxUnavailable(RuntimeError):
    """openpyxl (the xlsx extra) is not installed."""


def _role_columns(rows: list[dict]) -> list[Column]:
    columns = list(_CORE_COLUMNS)
    if any(row["priority"] for row in rows):
        columns.insert(1, Column("priority", "Priority", 11))
    if any(row["match_score"] is not None or row["match_band"] for row in rows):
        columns.extend(_SCORE_COLUMNS)
    columns.extend(col for col in _OPTIONAL_COLUMNS if any(row.get(col.field) for row in rows))
    columns.extend(_END_COLUMNS)
    return columns


def _roles(rows: list[dict]) -> list[dict]:
    result = []
    for rank, row in enumerate(rows, start=1):
        priority = view_model.normalize_priority(row.get("priority"))
        vacancy = view_model.best_vacancy_date(row)
        title = sanitize.clean_text(row.get("title")) or "Untitled posting"
        result.append({
            **row, "rank": rank, "title": title, "new": "New" if row.get("is_new") is True else None,
            "remote": view_model.remote_label(row.get("remote_policy")),
            "priority": f"P{priority}" if priority else None,
            "posted": date.fromisoformat(vacancy["iso"]) if vacancy else row.get("published_at"),
            "signals": " \u2022 ".join(view_model.split_fit_signals(row.get("fit_signals"))),
            "match_score": sanitize.clamp_score(row.get("match_score")),
            "match_band": sanitize.normalize_token(row.get("match_band"), view_model.MATCH_BANDS, default=""),
            "source_type": row.get("source_type") if row.get("source_type") != "unknown" else "",
            "source_label": row.get("source_label") if row.get("source_label") not in {"Agent find", "Unknown source"} else "",
            "status": "To Review", "notes": None,
        })
    return result


def _roles_sheet(ws, rows, columns, styles):
    positions = {col.field: column_letter(index) for index, col in enumerate(columns, start=1)}
    freeze = "E2" if "priority" in positions else "D2"
    write_table(ws, columns, rows, styles, table_name="RolesTable", freeze=freeze)
    if rows:
        _role_rules(ws, positions, last=len(rows) + 1, end=column_letter(len(columns)))
    return positions


def _role_rules(ws, positions: dict[str, str], *, last: int, end: str) -> None:
    from openpyxl.formatting.rule import CellIsRule, DataBarRule, FormulaRule
    from openpyxl.styles import Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    status = positions["status"]
    values = ",".join(("To Review", *_STATUS_PILLS))
    validation = DataValidation(type="list", formula1=f'"{values}"', allow_blank=True, showErrorMessage=True)
    validation.error = "Select a status from the list."
    validation.add(f"{status}2:{status}{last}")
    ws.add_data_validation(validation)
    if score := positions.get("match_score"):
        ws.conditional_formatting.add(
            f"{score}2:{score}{last}",
            DataBarRule(start_type="num", start_value=0, end_type="num", end_value=100, color=DATA_BAR_RGB),
        )
    for field, pills in (("status", _STATUS_PILLS), ("priority", _PRIORITY_PILLS),
                         ("match_band", BAND_PILLS), ("eligibility", ELIGIBILITY_PILLS)):
        if col := positions.get(field):
            for label, (fill, color) in pills.items():
                ws.conditional_formatting.add(
                    f"{col}2:{col}{last}",
                    CellIsRule(operator="equal", formula=[f'"{label}"'], fill=PatternFill("solid", fgColor=fill),
                               font=Font(color=color, bold=True)),
                )
    for label, fill, font in (("Rejected", "F1F4F9", Font(color="64748B", italic=True, strike=True)),
                              ("Applied", "EBF4FF", None)):
        ws.conditional_formatting.add(
            f"A2:{end}{last}", FormulaRule(formula=[f'${status}2="{label}"'],
                                           fill=PatternFill("solid", fgColor=fill), font=font),
        )
    fields = list(positions)
    verdict = fields.index("evaluation_verdict")
    before = column_letter(verdict)
    after = column_letter(verdict + 2)
    ws.conditional_formatting.add(
        f"A2:{before}{last} {after}2:{end}{last}", FormulaRule(formula=[f'${positions["new"]}2="New"'],
                                                             fill=PatternFill("solid", fgColor=NEW_ROW_FILL),
                                                             font=Font(bold=True)),
    )


def _companies(rows: list[dict], mode: str) -> list[dict]:
    employers = defaultdict(list)
    names = {}
    for row in rows:
        company = sanitize.clean_text(row.get("company")) or "Unknown company"
        key = company.casefold()
        names.setdefault(key, company)
        employers[key].append(row)
    records = [_company_rollup(names[key], group) for key, group in employers.items()]
    return sorted(records, key=lambda row: (
        -row["open_roles"], -(row["score"] or 0) if mode == "scored" else row["priority_rank"],
        row["company"].casefold(),
    ))


def _company_rollup(company: str, group: list[dict]) -> dict:
    priorities = [p for job in group if (p := view_model.normalize_priority(job.get("priority"))) is not None]
    scores = [s for job in group if (s := sanitize.clamp_score(job.get("match_score"))) is not None]
    remote = sorted({view_model.remote_label(job.get("remote_policy")) for job in group} - {""})
    locations = list(dict.fromkeys(location for job in group if (location := sanitize.clean_text(job.get("location")))))
    url = next((link for job in group if (link := sanitize.safe_http_url(job.get("apply_url")))), None)
    return {
        "company": company, "open_roles": len(group), "new_roles": sum(job.get("is_new") is True for job in group),
        "priority": f"P{min(priorities)}" if priorities else None,
        "priority_rank": min(priorities, default=4), "score": max(scores, default=None),
        "remote": remote, "locations": locations[:5], "sample": group[0].get("title"), "url": url,
    }


def _companies_sheet(ws, rows, mode, styles):
    ranking = Column("score", "Top Score", 14) if mode == "scored" else Column("priority", "Best Priority", 14)
    cols = (
        Column("company", "Company", 26), Column("open_roles", "Open Roles", 12),
        Column("new_roles", "New Roles", 12), ranking, Column("remote", "Remote Policies", 28),
        Column("locations", "Locations", 32), Column("sample", "Sample Role", 42),
        Column("url", "Careers / Board Link", 44, "url"),
    )
    write_table(ws, cols, rows, styles, table_name="CompaniesTable")


def export_run_to_excel(run_id: str, out_path: Path | str | None = None) -> Path:
    """Write a new workbook for a finalized run, preserving any existing destination."""
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
    styles = workbook_styles()
    mode = view_model.detect_export_mode(rows)
    companies = _companies(rows, mode)
    wb = Workbook(iso_dates=True)
    try:
        summary_ws = wb.active
        summary_ws.title = "Summary"
        roles_ws = wb.create_sheet(f"Roles ({len(rows)})")
        roles = _roles(rows)
        positions = _roles_sheet(roles_ws, roles, _role_columns(roles), styles)
        _companies_sheet(wb.create_sheet(f"Companies ({len(companies)})"), companies, mode, styles)
        write_table(wb.create_sheet(f"Discards ({len(discards)})"), _DISCARD_COLUMNS, discards, styles,
                    table_name="DiscardsTable")
        if any(row.get(col.field) for row in rows for col in _DATE_COLUMNS[2:]):
            write_table(wb.create_sheet("Vacancy Dates"), _DATE_COLUMNS, rows, styles, table_name="VacancyDatesTable")
        build_dashboard(summary_ws, run, results.get("summary") or {}, rows, companies, len(discards),
                        roles_ws.title, positions, mode, styles)
        wb.properties.creator = "ULTRON"
        wb.properties.title = f"ULTRON run {run_id}"
        target = Path(out_path).absolute() if out_path else (out_dir / "results.xlsx").absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        with BytesIO() as content:
            wb.save(content)
            if not atomic_create_bytes(target, content.getvalue()):
                raise FileExistsError(f"Workbook already exists; choose a new output path: {target}")
    finally:
        wb.close()
    return target

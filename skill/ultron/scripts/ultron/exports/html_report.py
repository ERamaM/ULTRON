"""Render a run's shortlist as one self-contained, offline HTML file.

The page has inline CSS and one small inline script (allowed by a CSP hash) and makes no network
requests: no fonts, images, scripts or stylesheets are loaded from anywhere. Every value comes from
posting text or run files and is treated as untrusted: it is redacted with the same privacy boundary
the Markdown export uses, stripped of control characters and HTML-escaped. Only http(s) job links
are rendered as links.
"""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from ultron.core.job_identity import canonical_job_identity
from ultron.exports import sanitize, view_model
from ultron.exports.html_assets import CSP, REPORT_CSS, REPORT_JS
from ultron.storage import runs

MATCH_BANDS = view_model.MATCH_BANDS
_BAND_LABELS = {"strong": "Strong", "good": "Good", "borderline": "Borderline", "ineligible": "Ineligible"}
_ELIGIBILITY = ("confirmed", "high", "medium", "unknown")
# Eligibility pills reuse the band colors: the strongest evidence gets the strongest color.
_ELIGIBILITY_CLASS = {"confirmed": "strong", "high": "good", "medium": "borderline", "unknown": "ineligible"}


def _clean(value: Any) -> str:
    return sanitize.clean_text(value)


def _t(value: Any) -> str:
    """Escape untrusted text for element content and quoted attribute values."""
    return html.escape(_clean(value), quote=True)


def _safe_url(value: Any) -> str | None:
    """Return an escaped http(s) URL suitable for ``href``, or ``None``."""
    url = sanitize.safe_http_url(value)
    return html.escape(url, quote=True) if url is not None else None


def _score(value: Any) -> int | None:
    return sanitize.clamp_score(value)


def _token(value: Any, allowed: tuple[str, ...]) -> str:
    return sanitize.normalize_token(value, allowed)


def _when(value: Any) -> str:
    """``2026-11-19 12:30`` for an ISO timestamp; any other value is shown as escaped text."""
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return _t(value)


def _discard_section(discards: list[dict]) -> str:
    groups: dict[str, list[dict]] = {}
    for record in discards:
        if isinstance(record, dict):
            groups.setdefault(_clean(record.get("reason")) or "unspecified", []).append(record)
    if not groups:
        return ""
    out = ['<section class="discards" aria-labelledby="discards-h">',
           f'<h2 class="sec" id="discards-h">Discarded postings ({len(discards)})</h2>']
    for reason, records in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        out.append(f"<details><summary>{html.escape(reason.replace('_', ' '))} ({len(records)})</summary><ul>")
        for record in records:
            title = _t(record.get("title")) or "Untitled posting"
            url = _safe_url(record.get("apply_url"))
            label = f'<a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>' if url else title
            company, location, detail = (_t(record.get(k)) for k in ("company", "location", "detail"))
            extra = ", ".join(x for x in (company, location) if x)
            why = f'<br><span class="why">{detail}</span>' if detail else ""
            out.append(f"<li>{label}{' at ' + extra if extra else ''}{why}</li>")
        out.append("</ul></details>")
    out.append("</section>")
    return "".join(out)



def _identity(row: dict, url: str | None) -> str:
    explicit = _clean(row.get("identity_key"))
    if explicit or not url:
        return explicit
    identity = canonical_job_identity(url, source_type=_clean(row.get("source_type")),
                                      source_key=_clean(row.get("source_key")))
    return _clean(identity.identity_key)


def _display_job(row: dict, rank: int) -> dict:
    url = sanitize.safe_http_url(row.get("apply_url"))
    priority = view_model.normalize_priority(row.get("priority"))
    salary = _clean(row.get("salary"))
    currency, midpoint = view_model.parse_salary_midpoint(salary) or ("", "")
    date = view_model.best_vacancy_date(row) or {}
    title = _clean(row.get("title")) or "Untitled role"
    company = _clean(row.get("company")) or "Unknown company"
    signals = view_model.split_fit_signals(row.get("fit_signals"))
    remote_label = view_model.remote_label(row.get("remote_policy"))
    location = _clean(row.get("location"))
    return {
        "rank": rank, "id": _identity(row, url), "title": title, "company": company, "location": location,
        "url": url, "score": _score(row.get("match_score")), "band": _token(row.get("match_band"), MATCH_BANDS),
        "priority": f"p{priority}" if priority else "unset", "priority_label": view_model.priority_label(priority),
        "new": row.get("is_new") is True, "salary": salary, "midpoint": midpoint, "currency": currency,
        "date": date, "remote": _clean(row.get("remote_policy")).lower() if remote_label else "",
        "remote_label": remote_label, "signals": signals, "monogram": view_model.company_monogram(company),
        "eligibility": _clean(row.get("eligibility")), "evidence": _clean(row.get("eligibility_evidence")),
        "freshness": _clean(row.get("freshness")) if not date else "",
        "date_evidence": [(label, _clean(row.get(key))) for key, label in (
            ("published_at", "Published"), ("updated_at", "Updated"), ("first_seen", "First seen"),
            ("unverified_date", "Unverified source date"),
        ) if row.get(key)],
        "requirements": [(label, _clean(row.get(key))) for label, key in (
            ("Matched", "matched_requirements"), ("Missing", "missing_requirements"), ("Uncertain", "uncertain_requirements")
        ) if row.get(key)],
    }


def _attributes(job: dict) -> str:
    tokens = {
        "id": job["id"], "rank": job["rank"], "company": job["company"].lower(),
        "score": job["score"] if job["score"] is not None else -1, "band": job["band"],
        "priority": job["priority"], "new": int(job["new"]), "salary": int(bool(job["salary"])),
        "salary-mid": job["midpoint"], "currency": job["currency"], "date": job["date"].get("iso", ""),
        "remote": job["remote"], "tags": "\n".join(tag.lower() for tag in job["signals"]),
        "search": " ".join([job["title"], job["company"], job["location"], job["salary"], job["remote_label"],
                            job["evidence"], job["eligibility"], *job["signals"],
                            *(text for _, text in job["requirements"])]).lower(),
    }
    return " ".join(f'data-{key}="{html.escape(str(value), quote=True)}"' for key, value in tokens.items())


def _filter_button(label: str, field: str, token: str, css: str = "chip", *, style: str = "") -> str:
    return (f'<button type="button" class="{css}" data-filter="{field}" data-value="{_t(token)}" '
            f'aria-pressed="false"{style}>{_t(label)}</button>')


def _priority_badge(job: dict) -> str:
    if not job["priority_label"]:
        return ""
    shade = {"p1": "strong", "p2": "good", "p3": "ineligible"}[job["priority"]]
    return f'<span class="priority b-{shade}">{_t(job["priority_label"])}</span>'


def _apply_link(job: dict) -> str:
    if url := _safe_url(job["url"]):
        return (f'<a class="apply" href="{url}" target="_blank" rel="noopener noreferrer" '
                f'aria-label="Apply to {_t(job["title"])}">Apply</a>')
    return '<span class="nolink">No safe link</span>'


def _triage(job: dict) -> str:
    if not job["id"]:
        return ""
    buttons = "".join(f'<button type="button" data-state="{value}" aria-pressed="false">{label}</button>'
                      for value, label in (("shortlisted", "Shortlist"), ("applied", "Applied"), ("rejected", "Dismiss")))
    return (f'<div class="triage needs-js" aria-label="Triage for {_t(job["title"])}" role="group">{buttons}'
            '<details><summary>Note</summary><input type="text" maxlength="500" data-note '
            'aria-label="Personal note" autocomplete="off"></details></div>')


def _pills(job: dict) -> str:
    pills = []
    if job["band"] in _BAND_LABELS:
        pills.append(f'<li class="pill b-{job["band"]}">{_BAND_LABELS[job["band"]]} match</li>')
    if job["new"]:
        pills.append('<li class="pill new">New</li>')
    eligibility = job["eligibility"]
    if eligibility:
        css = _ELIGIBILITY_CLASS.get(_token(eligibility, _ELIGIBILITY), "none")
        pills.append(f'<li class="pill b-{css}">Eligibility: {_t(eligibility)}</li>')
    for label in (job["remote_label"], job["date"].get("label")):
        if label:
            pills.append(f'<li class="pill plain">{_t(label)}</li>')
    return f'<ul class="pills" aria-label="Labels">{"".join(pills)}</ul>' if pills else ""


def _evidence(job: dict) -> str:
    parts = []
    if job["evidence"] or job["freshness"] or job["date_evidence"]:
        text = "".join(f"<p>{_t(value)}</p>" for value in (job["evidence"], job["freshness"]) if value)
        dates = "".join(f"<p>{label}: {_t(value)}</p>" for label, value in job["date_evidence"])
        parts.append(f"<details><summary>Eligibility evidence</summary>{text}{dates}</details>")
    if job["requirements"]:
        reqs = "".join(f"<dt>{label}</dt><dd>{_t(text)}</dd>" for label, text in job["requirements"])
        parts.append(f"<details><summary>Requirements</summary><dl>{reqs}</dl></details>")
    return "".join(parts)


def _job_card(job: dict) -> str:
    rank, title, company, monogram = job["rank"], _t(job["title"]), _t(job["company"]), job["monogram"]
    tags = "".join(_filter_button(tag, "tag", tag.lower(), "tag") for tag in job["signals"])
    salary = f'<strong class="salary">{_t(job["salary"])}</strong>' if job["salary"] else ""
    score = job["score"]
    badge = _priority_badge(job) if score is None else (
        f'<div class="ring r-{job["band"]}" style="--s:{score}" role="img" '
        f'aria-label="Match score {score} of 100"><span>{score}</span></div>'
    )
    return "".join([
        f'<li class="job{" is-new" if job["new"] else ""}" {_attributes(job)}>',
        f'<div class="rank" aria-label="Rank {rank}">{rank}</div><div class="body">',
        (f'<div class="job-title"><div class="avatar tone-{monogram["tone"]}" aria-hidden="true">'
        f'{_t(monogram["initials"])}</div><div><h3>{title}</h3>'),
        f'<p class="org"><b>{company}</b>{" in " + _t(job["location"]) if job["location"] else ""}</p></div></div>',
        _pills(job), f'<div class="tags" aria-label="Fit signals">{tags}</div>' if tags else "",
        _evidence(job), _triage(job), f'</div><div class="side">{badge}{salary}{_apply_link(job)}</div></li>',
    ])


def _table(jobs: list[dict], mode: str) -> str:
    metric = {"toolkit": "Priority", "scored": "Score", "mixed": "Priority / Score"}[mode]
    columns = ("#", metric, "Company", "Title", "Location", "Remote", "Salary", "Posted", "Triage", "Apply")
    rows = []
    for job in jobs:
        score = job["score"]
        values = (
            _priority_badge(job) if score is None else str(score),
            _t(job["company"]), _t(job["title"]), _t(job["location"]), _t(job["remote_label"]),
            _t(job["salary"]), _t(job["date"].get("iso")), _triage(job), _apply_link(job),
        )
        cells = "".join(f"<td>{value}</td>" for value in values)
        rows.append(f'<tr data-id="{_t(job["id"])}"><th scope="row">{job["rank"]}</th>{cells}</tr>')
    headers = "".join(f'<th scope="col">{label}</th>' for label in columns)
    return (f'<div class="compact" id="job-table" tabindex="0" role="region" aria-label="Roles table" hidden>'
            f'<table><thead><tr>{headers}</tr></thead><tbody id="job-rows">{"".join(rows)}</tbody></table></div>')


def _distribution(jobs: list[dict], mode: str) -> str:
    field = "band" if mode == "scored" else "priority"
    counts = Counter(job[field] for job in jobs)
    labels = (*_BAND_LABELS.items(), ("none", "Unscored")) if field == "band" else (
        ("p1", "P1"), ("p2", "P2"), ("p3", "P3"), ("unset", "Unprioritized"),
    )
    shades = {"p1": "strong", "p2": "good", "p3": "ineligible", "unset": "none"} if field == "priority" else {}
    title = "Match Bands" if field == "band" else "Priority"
    bar, legend = [], []
    for token, label in labels:
        count = counts[token]
        if count:
            width = f' style="width:{count * 100 / len(jobs):.4f}%" aria-label="{label} ({count})"'
            bar.append(_filter_button("", field, token, f"segment b-{shades.get(token, token)}", style=width))
        legend.append(f'<li>{_filter_button(f"{label} {count}", field, token)}</li>')
    return (f'<div class="bands"><p>{title}</p><div class="strip">{"".join(bar)}</div>'
            f'<ul class="legend" aria-label="Roles by {title.lower()}">{"".join(legend)}</ul></div>')


def _hero(jobs: list[dict], mode: str, discards: int) -> str:
    salaries = view_model.summarize_job_display(jobs)["salary_summary"]
    medians = " / ".join(f"{currency} {stats['median']:,.0f}" for currency, stats in salaries["by_currency"].items())
    pay_context = f"Annual median {medians}" if medians else f"{discards} discarded" if not salaries["disclosed_count"] else ""
    new = sum(job["new"] for job in jobs)
    lead = ("Strong matches", sum(job["band"] == "strong" for job in jobs)) if mode == "scored" else (
        "P1 \u00b7 Apply first", sum(job["priority"] == "p1" for job in jobs),
    )
    tiles = [
        ("Roles kept", len(jobs), ""), ("New since last run", new, ""),
        (*lead, ""), ("Disclosed pay", salaries["disclosed_count"], pay_context),
    ]
    values = []
    for index, (label, count, context) in enumerate(tiles):
        marker = f'<span class="new">{count}</span>' if index == 1 and count else str(count)
        extra = f"<small>{_t(context)}</small>" if context else ""
        values.append(f'<div class="kpi"><dt>{label}</dt><dd>{marker}</dd>{extra}</div>')
    return '<dl class="kpis">' + "".join(values) + "</dl>"


def _toolbar(jobs: list[dict], run_id: str, mode: str) -> str:
    ranking = ("Strong", "band", "strong") if mode == "scored" else ("P1", "priority", "p1")
    policies = Counter(job["remote"] for job in jobs if job["remote"])
    chips = [("All", "all", ""), ("New only", "new", "1"), ranking,
             ("With salary", "salary", "1"), ("Shortlisted", "state", "shortlisted")]
    chips.extend((view_model.remote_label(policy), "remote", policy) for policy, _ in policies.most_common(3))
    sort = [('rank', 'Rank')]
    if mode in {"toolkit", "mixed"}:
        sort.append(("priority", "Priority"))
    if mode in {"scored", "mixed"}:
        sort.append(("score", "Score"))
    sort += [("date", "Posted date"), ("salary", "Salary"), ("company", "Company")]
    options = "".join(f'<option value="{value}">{label}</option>' for value, label in sort)
    return "".join([
        f'<div class="tools" id="tools" data-run="{_t(run_id)}" hidden>',
        '<div class="quick" aria-label="Quick filters">',
        *(_filter_button(label, field, token) for label, field, token in chips), '</div>',
        '<input type="search" id="q" placeholder="Search roles" aria-label="Search roles">',
        f'<label>Sort <select id="sort">{options}</select></label>',
        ('<div class="toggle" role="group" aria-label="View"><button type="button" class="chip" data-view="cards" '
        'aria-pressed="true">Cards</button><button type="button" class="chip" data-view="table" '
        'aria-pressed="false">Table</button></div>'),
        '<label><input type="checkbox" id="hide-dismissed"> Hide dismissed</label>',
        ('<label>Theme <select id="theme"><option value="system">System</option><option value="light">Light</option>'
        '<option value="dark">Dark</option></select></label>'),
        '<button class="chip" type="button" id="show-sync">Export triage (0)</button>',
        '<span class="count" id="count" aria-live="polite"></span><span id="storage-status" role="status"></span></div>',
    ])


_SYNC = (
    '<dialog id="sync" aria-labelledby="sync-h"><h2 id="sync-h">Sync to ULTRON</h2>'
    '<label>Shell <select id="command-shell"><option value="powershell">PowerShell</option>'
    '<option value="posix">Bash / Zsh</option></select></label>'
    '<textarea id="commands" aria-label="Triage commands" readonly rows="8"></textarea>'
    '<div class="actions"><button type="button" id="copy">Copy commands</button>'
    '<button type="button" id="reset">Reset local triage</button><button type="button" id="close-sync">Close</button></div>'
    '<p id="sync-status" role="status"></p></dialog>'
)


def render_html(run: dict, results: dict, discards: list[dict]) -> str:
    """Return the complete offline report for one run."""
    rows = [row for row in (results.get("rows") or []) if isinstance(row, dict)]
    jobs = [_display_job(row, rank) for rank, row in enumerate(rows, start=1)]
    mode = view_model.detect_export_mode(rows)
    summary = results.get("summary") if isinstance(results.get("summary"), dict) else {}
    run_id = _clean(results.get("run_id") or run.get("run_id"))
    metadata = [_t(run.get("profile_slug")), _t(run.get("preset")),
                _when(run.get("finalized_at") or summary.get("ended_at") or run.get("created_at"))]
    discard_counts = summary.get("discard_counts")
    count = sum(v for v in discard_counts.values() if type(v) is int) if isinstance(discard_counts, dict) else 0
    content = [
        _toolbar(jobs, run_id, mode), f'<ol class="jobs" id="jobs">{"".join(map(_job_card, jobs))}</ol>',
        _table(jobs, mode), '<p class="empty" id="no-matches" hidden>No roles match these filters.</p>',
        _SYNC,
    ] if jobs else ['<p class="empty">No roles made the shortlist in this run.</p>']
    distributions = ("scored", "toolkit") if mode == "mixed" else (mode,)
    return "\n".join([
        "<!doctype html>", '<html lang="en">', "<head>",
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">', '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="referrer" content="no-referrer">', '<meta name="generator" content="ULTRON">',
        f'<title>Job shortlist {_t(run_id)}</title>', f"<style>{REPORT_CSS}</style>", "</head>", "<body>",
        '<a class="skip" href="#shortlist">Skip to the shortlist</a><div class="wrap">',
        '<header class="run"><h1>Job shortlist</h1>',
        f'<p class="meta">{" \u00b7 ".join(item for item in metadata if item)}</p>',
        _hero(jobs, mode, max(len(discards), count)),
        "".join(_distribution(jobs, group) for group in distributions) if jobs else "", '</header><main>',
        '<section id="shortlist" aria-labelledby="shortlist-h"><h2 class="sec" id="shortlist-h">Ranked roles</h2>',
        *content, "</section>", _discard_section(discards), "</main></div>",
        f"<script>{REPORT_JS}</script>" if jobs else "", "</body>", "</html>", "",
    ])


def export_run_to_html(run_id: str, out_path: Path | str | None = None) -> Path:
    """Write ``results.html`` (or ``out_path``) for ``run_id`` and return the absolute path.

    Raises ``runs.RunNotFoundError`` for an unknown run and ``FileNotFoundError`` before finalize.
    """
    run = runs.load_run(run_id)
    out_dir = runs.run_dir(run_id)
    results_path = out_dir / "results.json"
    if not results_path.is_file():
        raise FileNotFoundError(f"results.json not found for run {run_id}; run finalize first")
    results = json.loads(results_path.read_text(encoding="utf-8"))
    if not isinstance(results, dict):
        raise ValueError(f"results.json for run {run_id} is not an object")  # noqa: TRY004 - maps to export_failed
    discards = [*runs.read_jsonl(out_dir / "discarded.jsonl"), *runs.read_jsonl(out_dir / "finalize_discards.jsonl")]
    target = (Path(out_path) if out_path else out_dir / "results.html").absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_html(run, results, discards))
    return target

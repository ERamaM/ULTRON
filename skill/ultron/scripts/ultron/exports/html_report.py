"""Render a run's shortlist as one self-contained, offline HTML file.

The page has inline CSS and one small inline script (allowed by a CSP hash) and makes no network
requests: no fonts, images, scripts or stylesheets are loaded from anywhere. Every value comes from
posting text or run files and is treated as untrusted: it is redacted with the same privacy boundary
the Markdown export uses, stripped of control characters and HTML-escaped. Only http(s) job links
are rendered as links.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from ultron.exports import sanitize
from ultron.storage import runs

_BANDS = ("strong", "good", "borderline", "ineligible")
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


_CSS = """
:root{color-scheme:light dark;--paper:#f2f4f7;--surface:#fff;--ink:#16202d;--muted:#566275;--line:#d7dde6;
--accent:#1f5fad;--accent-ink:#fff;--soft:#dce8f6;--soft-ink:#0b3d73;--warm:#fbe2c3;--warm-ink:#7a3e00;
--grey:#e4e7ec;--grey-ink:#3a4452;--new:#fff1bf;--new-ink:#5c4300;--ring-track:#e3e8ef;--focus:#c25e00;
--serif:"Iowan Old Style","Charter","Bitstream Charter","Sitka Text",Cambria,Georgia,serif;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans","Helvetica Neue",Arial,sans-serif}
@media (prefers-color-scheme:dark){:root{--paper:#0f151e;--surface:#172030;--ink:#e5eaf1;--muted:#9eabbd;
--line:#2b3748;--accent:#86b6f2;--accent-ink:#0b1a2d;--soft:#1d3554;--soft-ink:#cfe2fb;--warm:#4a2f12;
--warm-ink:#ffd6a6;--grey:#2a3240;--grey-ink:#cdd5e0;--new:#4a3d0c;--new-ink:#ffe9a3;--ring-track:#273244;
--focus:#ffb35c}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 var(--sans)}
a{color:var(--accent)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.wrap{max-width:62rem;margin:0 auto;padding:2.5rem 1rem 3rem}
.skip{position:absolute;left:-999px}.skip:focus{left:1rem;top:1rem;background:var(--surface);padding:.5rem}
header.run h1{font:600 clamp(1.9rem,4.5vw,2.8rem)/1.1 var(--serif);margin:0 0 .4rem;letter-spacing:-.01em}
.meta{color:var(--muted);margin:0 0 1.5rem;font-size:.95rem}
.lede{font:1.15rem/1.45 var(--serif);margin:0 0 1rem;max-width:40rem}
.lede strong{font-variant-numeric:tabular-nums}
.bands{margin:0 0 2rem}
.strip{display:flex;height:.7rem;border-radius:.35rem;overflow:hidden;background:var(--ring-track);margin:0 0 .6rem}
.strip span{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:.35rem 1.1rem;margin:0;padding:0;list-style:none;font-size:.9rem}
.legend li{display:flex;align-items:center;gap:.4rem}
.sw{width:.8rem;height:.8rem;border-radius:.2rem;display:inline-block;border:1px solid var(--line)}
.b-strong{background:var(--accent);color:var(--accent-ink)}
.b-good{background:var(--soft);color:var(--soft-ink)}
.b-borderline{background:var(--warm);color:var(--warm-ink)}
.b-ineligible,.b-none{background:var(--grey);color:var(--grey-ink)}
.tools{display:flex;flex-wrap:wrap;gap:.6rem 1rem;align-items:center;margin:0 0 1rem}
.tools[hidden]{display:none}
.tools input[type=search],.tools select{font:inherit;color:inherit;background:var(--surface);
border:1px solid var(--line);border-radius:.4rem;padding:.4rem .6rem;min-width:0}
.tools input[type=search]{flex:1 1 14rem}
.tools label{display:flex;align-items:center;gap:.4rem}
.count{color:var(--muted);font-size:.9rem;margin-left:auto}
h2.sec{font:600 1.4rem/1.2 var(--serif);margin:2.5rem 0 1rem}
ol.jobs{list-style:none;margin:0;padding:0;counter-reset:none}
.job{display:grid;grid-template-columns:2.6rem minmax(0,1fr) auto;gap:.2rem 1rem;background:var(--surface);
border:1px solid var(--line);border-radius:.6rem;padding:1.1rem 1.1rem 1rem .6rem;margin:0 0 .75rem}
.job.is-new{box-shadow:inset .3rem 0 0 var(--focus)}
.rank{font:600 1.35rem/1 var(--serif);color:var(--muted);text-align:right;padding-top:.15rem;
font-variant-numeric:tabular-nums}
.job h3{font:600 1.2rem/1.25 var(--serif);margin:0;overflow-wrap:anywhere}
.org{margin:.15rem 0 .5rem;color:var(--muted);overflow-wrap:anywhere}
.org b{color:var(--ink);font-weight:600}
.pills{display:flex;flex-wrap:wrap;gap:.35rem;margin:0 0 .5rem;padding:0;list-style:none}
.pill{font-size:.8rem;line-height:1;padding:.32rem .55rem;border-radius:1rem;font-weight:600}
.pill.plain{background:transparent;color:var(--muted);border:1px solid var(--line);font-weight:500}
.pill.new{background:var(--new);color:var(--new-ink)}
.fit{margin:0 0 .4rem;font-size:.95rem;overflow-wrap:anywhere}
details{margin:.25rem 0 0;font-size:.92rem}
summary{cursor:pointer;color:var(--accent);width:max-content;max-width:100%}
details p,details dl{margin:.35rem 0 .2rem;overflow-wrap:anywhere}
details dt{font-weight:600}details dd{margin:0 0 .3rem}
.side{display:flex;flex-direction:column;align-items:center;gap:.7rem;min-width:6rem}
.ring{--s:0;width:4.6rem;height:4.6rem;border-radius:50%;display:grid;place-items:center;
background:conic-gradient(var(--ring) calc(var(--s)*1%),var(--ring-track) 0)}
.ring span{width:3.6rem;height:3.6rem;border-radius:50%;background:var(--surface);display:grid;place-items:center;
font:600 1.3rem/1 var(--sans);font-variant-numeric:tabular-nums}
.ring small{display:block;font-size:.65rem;font-weight:500;color:var(--muted);margin-top:.1rem}
.r-strong{--ring:var(--accent)}.r-good{--ring:#5b9be0}.r-borderline{--ring:#e08a2c}.r-ineligible,.r-none{--ring:#8a94a3}
.noscore{color:var(--muted);font-size:.85rem;text-align:center}
.apply{display:inline-block;background:var(--accent);color:var(--accent-ink);text-decoration:none;font-weight:600;
padding:.5rem 1rem;border-radius:.4rem;white-space:nowrap}
.apply:hover{filter:brightness(1.08)}
.nolink{color:var(--muted);font-size:.85rem}
.empty{background:var(--surface);border:1px dashed var(--line);border-radius:.6rem;padding:1.5rem;margin:0}
.discards>details{background:var(--surface);border:1px solid var(--line);border-radius:.5rem;padding:.6rem .9rem;
margin:0 0 .5rem;font-size:1rem}
.discards>details>summary{color:var(--ink);font-weight:600}
.discards ul{margin:.6rem 0 .2rem;padding-left:1.1rem;font-size:.92rem}
.discards li{margin:0 0 .45rem;overflow-wrap:anywhere}
.discards .why{color:var(--muted)}
footer{margin-top:3rem;color:var(--muted);font-size:.85rem}
@media (max-width:40rem){.wrap{padding-top:1.5rem}
.job{grid-template-columns:minmax(0,1fr);padding:1rem}
.rank{text-align:left;font-size:1rem}
.side{flex-direction:row;justify-content:space-between;min-width:0;margin-top:.5rem}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media print{:root{--paper:#fff;--surface:#fff;--ink:#000;--muted:#333;--line:#999}
body{font-size:11pt}.tools,.skip{display:none!important}.wrap{max-width:none;padding:0}
.job,.discards>details{break-inside:avoid;box-shadow:none}
.apply{background:none;color:#000;padding:0;text-decoration:underline}
.apply::after{content:" (" attr(href) ")";font-weight:400;font-size:9pt;overflow-wrap:anywhere}
.ring{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""

# Reads only the DOM (no data is ever interpolated here); controls stay hidden without JavaScript.
_JS = """(function(){
var tools=document.getElementById('tools');var list=document.getElementById('jobs');
if(!tools||!list){return;}
tools.hidden=false;
var q=document.getElementById('q');var onlyNew=document.getElementById('only-new');
var sort=document.getElementById('sort');var count=document.getElementById('count');
var items=Array.prototype.slice.call(list.children);
function apply(){
var needle=q.value.trim().toLowerCase();var shown=0;
var key=sort.value;
items.slice().sort(function(a,b){
if(key==='score'){return (Number(b.dataset.score)-Number(a.dataset.score))||(Number(a.dataset.rank)-Number(b.dataset.rank));}
if(key==='company'){return a.dataset.company.localeCompare(b.dataset.company)||(Number(a.dataset.rank)-Number(b.dataset.rank));}
return Number(a.dataset.rank)-Number(b.dataset.rank);
}).forEach(function(li){list.appendChild(li);});
items.forEach(function(li){
var ok=(!needle||li.textContent.toLowerCase().indexOf(needle)!==-1)&&(!onlyNew.checked||li.dataset.new==='1');
li.hidden=!ok;if(ok){shown++;}
});
count.textContent=shown+' of '+items.length+' shown';
}
q.addEventListener('input',apply);onlyNew.addEventListener('change',apply);sort.addEventListener('change',apply);
apply();
})();"""

_JS_HASH = "sha256-" + base64.b64encode(hashlib.sha256(_JS.encode("utf-8")).digest()).decode("ascii")

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    f"script-src '{_JS_HASH}'; img-src 'none'; base-uri 'none'; form-action 'none'"
)


def _when(value: Any) -> str:
    """``2026-11-19 12:30`` for an ISO timestamp; any other value is shown as escaped text."""
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return _t(value)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _job_card(rank: int, row: dict) -> str:
    band = _token(row.get("match_band"), _BANDS)
    elig = _token(row.get("eligibility"), _ELIGIBILITY)
    score = _score(row.get("match_score"))
    is_new = row.get("is_new") is True
    url = _safe_url(row.get("apply_url"))
    title = _t(row.get("title")) or "Untitled role"
    company = _t(row.get("company"))
    location = _t(row.get("location"))

    pills = []
    if band != "none":
        pills.append(f'<li class="pill b-{band}">{_BAND_LABELS[band]} match</li>')
    eligibility = _t(row.get("eligibility"))
    if eligibility:
        css = f"b-{_ELIGIBILITY_CLASS[elig]}" if elig in _ELIGIBILITY_CLASS else "plain"
        pills.append(f'<li class="pill {css}">Eligibility: {eligibility}</li>')
    remote = _t(row.get("remote_policy"))
    if remote and remote != "unknown":
        pills.append(f'<li class="pill plain">{remote.replace("_", " ")}</li>')
    if is_new:
        pills.append('<li class="pill new">New</li>')

    fit = _t(row.get("fit_signals"))
    evidence = _t(row.get("eligibility_evidence"))
    dates = [
        (label, _t(row.get(key)))
        for key, label in (("published_at", "Published"), ("updated_at", "Updated"),
                           ("unverified_date", "Unverified source date"), ("first_seen", "First seen"))
        if row.get(key)
    ]
    freshness = _t(row.get("freshness"))
    reqs = [(label, _t(row.get(key))) for key, label in (
        ("matched_requirements", "Matched"), ("missing_requirements", "Missing"),
        ("uncertain_requirements", "Uncertain"))]
    reqs = [(label, text) for label, text in reqs if text]

    parts = [
        (f'<li class="job{" is-new" if is_new else ""}" data-rank="{rank}"'
         f' data-score="{score if score is not None else -1}"'
         f' data-company="{company.lower()}" data-new="{1 if is_new else 0}">'),
        f'<div class="rank" aria-hidden="true">{rank}</div>',
        '<div class="body">',
        f"<h3>{title}</h3>",
        f'<p class="org"><b>{company or "Unknown company"}</b>{" in " + location if location else ""}</p>',
    ]
    if pills:
        parts.append(f'<ul class="pills" aria-label="Labels">{"".join(pills)}</ul>')
    if fit:
        parts.append(f'<p class="fit">{fit}</p>')
    if evidence or freshness or dates:
        parts.append("<details><summary>Eligibility evidence</summary>")
        if evidence:
            parts.append(f"<p>{evidence}</p>")
        for label, value in dates:
            parts.append(f"<p>{label}: {value}</p>")
        if freshness and not any(row.get(key) for key in ("published_at", "updated_at", "unverified_date")):
            parts.append(f"<p>Date evidence: {freshness}</p>")
        parts.append("</details>")
    if reqs:
        parts.append("<details><summary>Requirements</summary><dl>")
        parts.extend(f"<dt>{label}</dt><dd>{text}</dd>" for label, text in reqs)
        parts.append("</dl></details>")
    parts.append("</div>")
    parts.append('<div class="side">')
    if score is None:
        parts.append('<p class="noscore">No score</p>')
    else:
        parts.append(f'<div class="ring r-{band}" style="--s:{score}" role="img" aria-label="Match score {score} of 100">'
                     f'<span>{score}</span></div>')
    if url:
        parts.append(f'<a class="apply" href="{url}" target="_blank" rel="noopener noreferrer">'
                     f'Apply<span class="skip"> to {title}</span></a>')
    else:
        parts.append('<span class="nolink">No safe link</span>')
    parts.append("</div></li>")
    return "".join(parts)


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


def render_html(run: dict, results: dict, discards: list[dict]) -> str:
    """Return the complete report document for one run."""
    rows = [r for r in (results.get("rows") or []) if isinstance(r, dict)]
    summary = results.get("summary") if isinstance(results.get("summary"), dict) else {}
    run_id = _t(results.get("run_id") or run.get("run_id"))
    new_count = sum(1 for r in rows if r.get("is_new") is True)
    bands = Counter(_token(r.get("match_band"), _BANDS) for r in rows)
    discard_counts = summary.get("discard_counts") if isinstance(summary.get("discard_counts"), dict) else {}
    total_discards = max(len(discards), sum(v for v in discard_counts.values() if isinstance(v, int)))
    when = _when(run.get("finalized_at") or summary.get("ended_at") or run.get("created_at"))
    meta = [x for x in (f"Profile {_t(run.get('profile_slug'))}" if run.get("profile_slug") else "",
                        f"preset {_t(run.get('preset'))}" if run.get("preset") else "",
                        f"run {run_id}" if run_id else "", f"finalized {when}" if when else "") if x]

    lede = (f"<strong>{_plural(len(rows), 'role', 'roles')}</strong> kept, "
            f"<strong>{new_count} new</strong> since the last run. "
            f"{_plural(total_discards, 'posting was', 'postings were')} discarded.")

    band_html = ""
    if rows:
        segments = "".join(
            f'<span class="b-{band}" style="width:{bands[band] * 100 / len(rows):.4f}%"></span>'
            for band in (*_BANDS, "none") if bands[band])
        legend = "".join(
            f'<li><span class="sw b-{band}"></span>{label} {bands[band]}</li>'
            for band, label in (*_BAND_LABELS.items(), ("none", "Unscored")) if bands[band] or band != "none")
        band_html = (f'<div class="bands"><div class="strip" aria-hidden="true">{segments}</div>'
                     f'<ul class="legend" aria-label="Roles by match band">{legend}</ul></div>')

    if rows:
        jobs = (
            '<div class="tools" id="tools" hidden>'
            '<input type="search" id="q" placeholder="Filter by title, company, skill" aria-label="Filter roles">'
            '<label><input type="checkbox" id="only-new"> New only</label>'
            '<label>Sort <select id="sort"><option value="rank">Rank</option><option value="score">Score</option>'
            '<option value="company">Company</option></select></label>'
            '<span class="count" id="count" aria-live="polite"></span></div>'
            f'<ol class="jobs" id="jobs">{"".join(_job_card(i, r) for i, r in enumerate(rows, start=1))}</ol>'
        )
    else:
        jobs = ('<p class="empty">No roles made the shortlist in this run. Check the discarded postings below, '
                "or widen the profile and search again.</p>")

    head_title = f"Job shortlist {run_id}".strip()
    return "\n".join([
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="referrer" content="no-referrer">',
        '<meta name="generator" content="ULTRON">',
        f"<title>{head_title}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        '<a class="skip" href="#shortlist">Skip to the shortlist</a>',
        '<div class="wrap">',
        '<header class="run">',
        "<h1>Job shortlist</h1>",
        f'<p class="meta">{", ".join(meta)}</p>' if meta else "",
        f'<p class="lede">{lede}</p>',
        band_html,
        "</header>",
        "<main>",
        '<section id="shortlist" aria-labelledby="shortlist-h">',
        '<h2 class="sec" id="shortlist-h">Ranked roles</h2>',
        jobs,
        "</section>",
        _discard_section(discards),
        "</main>",
        ("<footer><p>Generated by ULTRON. This file works offline and loads nothing from the network. "
         "Apply links open the employer's posting in a new tab.</p></footer>"),
        "</div>",
        f"<script>{_JS}</script>" if rows else "",
        "</body>",
        "</html>",
        "",
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

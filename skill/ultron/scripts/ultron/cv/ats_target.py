"""The job a CV is audited against: posting keywords, profile terms and target resolution."""

from __future__ import annotations

import itertools
import json
import re
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from ultron.core import presets
from ultron.core.ats.clients.occupop import occupop_job_ref
from ultron.core.discovery.url_filter import normalize_url
from ultron.core.extractors import parse_ats_page
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import JobRawPage
from ultron.core.privacy import redact
from ultron.core.rules.term_matching import contains_term, normalize_text
from ultron.cv import extract as cv
from ultron.cv.ats_requirements import classify_requirements
from ultron.infra import home
from ultron.infra.net import HttpError, RequestBudgetExceeded, URLRejected
from ultron.stages.fetch import get_posting_page, occupop_posting_job
from ultron.stages.http_factory import client_for_run
from ultron.storage import ledger, runs

KNOWN_SKILL_TERMS = (
    "Python", "Go", "Golang", "Rust", "Java", "Scala", "TypeScript", "JavaScript", "C++", "C#", ".NET",
    "SQL", "NoSQL", "PostgreSQL", "MySQL", "MongoDB", "Redis", "Snowflake", "BigQuery", "Redshift",
    "PyTorch", "TensorFlow", "Scikit-learn", "Pandas", "Spark", "Airflow", "dbt", "Kafka", "Flink",
    "Kubernetes", "Docker", "Terraform", "Ansible", "Linux", "AWS", "GCP", "Azure",
    "FastAPI", "Django", "Flask", "React", "Next.js", "Node.js", "GraphQL", "gRPC", "REST",
    "CI/CD", "GitHub Actions", "MLOps", "LLM", "RAG", "NLP", "Computer Vision",
    "Looker", "Tableau", "Power BI", "Excel", "Salesforce", "HubSpot", "SAP",
    "IFRS", "GAAP", "HIPAA", "GDPR", "SOC 2", "ISO 27001", "Agile", "Scrum",
)
_ACRONYM_SKIP = frozenset({
    "US", "USA", "UK", "EU", "EEA", "EMEA", "LATAM", "APAC", "CET", "CEST", "UTC", "PST", "EST",
    "HR", "CEO", "CTO", "CFO", "COO", "VP", "EEO", "URL", "PDF", "FAQ", "AM", "PM", "ID", "CV", "OR", "AND",
    "THE", "FOR", "WITH", "YOU", "OUR", "ALL", "NEW", "JOB", "INC", "LTD", "LLC", "GMBH", "SL", "SA",
})
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9/+.#-]{1,7}\b")
TECH_CANDIDATE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]{0,24}(?:[.+/#][A-Za-z0-9]{1,16}){0,2}\b")


def is_mixed_tech_token(token: str, *, allow_digit_lower: bool = False) -> bool:
    """Return True for CamelCase, punctuated tech names (Next.js, CI/CD), or optional k8s-style tokens."""
    if any(ch in token for ch in ".+/#"):
        return True
    if any(ch.isupper() for ch in token[1:]) and any(ch.islower() for ch in token):
        return True
    return allow_digit_lower and any(a.isdigit() and b.islower() for a, b in itertools.pairwise(token))




def extract_job_keywords(title: str, body: str, *, profile: dict | None = None) -> list[str]:
    """Extract bounded high-signal target keywords from a job title and description."""
    blob = f"{title}\n{body}"
    found: list[str] = []
    seen_low: set[str] = set()

    def add(term: str) -> None:
        clean = " ".join(term.split()).strip(".,;:()[]{}")
        if not clean or len(clean) < 2:
            return
        key = normalize_text(clean)
        if key in seen_low or clean.upper() in _ACRONYM_SKIP:
            return
        seen_low.add(key)
        found.append(clean)

    if isinstance(profile, dict):
        criteria = profile.get("criteria") if isinstance(profile.get("criteria"), dict) else {}
        qt = profile.get("query_templates") if isinstance(profile.get("query_templates"), dict) else {}
        for field in (
            criteria.get("target_titles"),
            criteria.get("role_focus_terms"),
            criteria.get("specialties"),
            criteria.get("role_signal_terms"),
            qt.get("technologies"),
            qt.get("specialties"),
        ):
            for term in field or []:
                if isinstance(term, str) and contains_term(blob, term):
                    add(term)

    for term in KNOWN_SKILL_TERMS:
        if term == "Go":
            if re.search(r"\bGo\b", blob) or contains_term(blob, "golang"):
                add(term)
        elif contains_term(blob, term):
            add(term)

    for token in _ACRONYM_RE.findall(blob):
        if token not in _ACRONYM_SKIP and not token.isdigit():
            add(token)

    for token in TECH_CANDIDATE_RE.findall(blob):
        if is_mixed_tech_token(token):
            add(token)

    if title.strip():
        add(title.strip())
    return found[:35]


def _profile_target_terms(profile: dict) -> list[str]:
    criteria = profile.get("criteria") if isinstance(profile.get("criteria"), dict) else {}
    qt = profile.get("query_templates") if isinstance(profile.get("query_templates"), dict) else {}
    raw: list[str] = []
    for bucket in (
        criteria.get("target_titles"),
        criteria.get("role_focus_terms"),
        criteria.get("specialties"),
        criteria.get("role_signal_terms"),
        qt.get("technologies"),
        qt.get("specialties"),
    ):
        for term in bucket or []:
            if isinstance(term, str) and term.strip():
                raw.append(term.strip())
    seen: set[str] = set()
    out: list[str] = []
    for term in raw:
        key = normalize_text(term)
        if key not in seen:
            seen.add(key)
            out.append(term)
    return out[:40]


def _find_local_job(job_arg: str, *, run_id: str | None = None) -> dict[str, Any] | None:
    ident_key = job_arg
    norm_arg = ""
    if job_arg.startswith(("http://", "https://")):
        norm_arg = normalize_url(job_arg)
        ident_key = canonical_job_identity(job_arg).identity_key

    candidate_runs: list[str] = []
    if run_id:
        runs.load_run(run_id)
        candidate_runs.append(run_id)
    else:
        db_path = home.ultron_home() / "ultron.sqlite3"
        if db_path.is_file():
            with closing(ledger.connect()) as conn:
                snapshot_key, snapshot_runs = ledger.vacancy_snapshot_runs(conn, ident_key, norm_arg)
            ident_key = snapshot_key
            candidate_runs.extend(snapshot_runs)
        available = sorted(
            runs.list_runs(), key=lambda item: (item.get("created_at") or "", item.get("run_id") or ""),
            reverse=True,
        )
        recent = [item["run_id"] for item in available if item.get("run_id") and "fetch" in (item.get("stages_done") or [])][:6]
        known = set(candidate_runs) | set(recent)
        creation = {item["run_id"]: item.get("created_at") or "" for item in available if item.get("run_id")}
        candidate_runs = sorted(known, key=lambda rid: (rid[:15], creation.get(rid, ""), rid), reverse=True)

    for rid in candidate_runs:
        folder = runs.run_dir(rid)
        for row in runs.read_jsonl(folder / "postings.jsonl"):
            job = row.get("job") if isinstance(row.get("job"), dict) else {}
            url = str(job.get("apply_url") or "")
            row_key = str(row.get("identity_key") or "")
            url_ident = canonical_job_identity(url).identity_key if url else ""
            if job_arg == row_key or ident_key in {row_key, url_ident} or (norm_arg and url and normalize_url(url) == norm_arg):
                return {"run_id": rid, "identity_key": row_key or url_ident, **job}
    return None


def _fetch_job_url(url: str, *, profile_slug: str | None) -> dict[str, Any]:
    client = client_for_run({"settings": presets.resolve("balanced"), "profile_slug": profile_slug or "ats-audit"})
    try:
        status, final_url, html = get_posting_page(client, url)
    except (HttpError, URLRejected, RequestBudgetExceeded) as exc:
        raise ValueError(f"cannot fetch job URL ({type(exc).__name__}: {exc})") from exc
    if status >= 400:
        raise ValueError(f"job URL returned HTTP {status}")
    if occupop_job_ref(url):
        job, reason = occupop_posting_job(url, html)
        if job is None:
            raise ValueError(f"cannot extract Occupop job: {reason}")
    else:
        page = JobRawPage(url=url, final_url=final_url, status_code=status, html=html)
        job = parse_ats_page(page, ats_hints={})
    return {
        "identity_key": canonical_job_identity(job.apply_url or url).identity_key,
        "title": job.title,
        "company": job.company,
        "apply_url": job.apply_url or final_url,
        "body_text": job.body_text,
    }


def _run_aggregate_terms(run_id: str) -> tuple[list[str], dict[str, Any]]:
    runs.load_run(run_id)
    folder = runs.run_dir(run_id)
    counter: Counter[str] = Counter()
    results_path = folder / "results.json"
    rows: list[dict[str, Any]] = []
    if results_path.is_file():
        rows = json.loads(results_path.read_text(encoding="utf-8")).get("rows") or []
    for row in rows:
        for sig in row.get("fit_signals") or []:
            if isinstance(sig, str) and sig.strip():
                counter[sig.strip()] += 1
        for req in row.get("matched_requirements") or []:
            if isinstance(req, str) and req.strip():
                counter[req.strip()] += 1
    kept_rows: list[dict[str, Any]] = []
    if not counter:
        kept_rows = list(runs.read_jsonl(folder / "kept.jsonl"))
        for row in kept_rows:
            ev = row.get("heuristic") or row.get("evaluation") or {}
            if not isinstance(ev, dict):
                continue
            for field in ("fit_signals", "matched_requirements"):
                for item in ev.get(field) or []:
                    if isinstance(item, str) and item.strip():
                        counter[item.strip()] += 1
    terms = [term for term, _count in counter.most_common(30)]
    return terms, {"run_id": run_id, "kept_rows_analyzed": len(rows) or len(kept_rows)}


def resolve_target(
    *,
    profile: dict | None,
    profile_slug: str | None,
    job_arg: str | None,
    job_file: Path | None,
    run_id: str | None,
    fetch: bool,
) -> tuple[str, list[str], dict[str, Any]]:
    """Resolve target keywords from a search profile, job text, and profile file."""
    if job_file is not None:
        if not job_file.is_file():
            raise FileNotFoundError(f"job file not found: {job_file}")
        if job_file.stat().st_size > cv.MAX_CV_BYTES:
            raise ValueError("job file is too large")
        raw = cv._clean_text(cv.decode_text_bytes(job_file.read_bytes()), cv.MAX_CV_CHARS)
        first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
        terms = extract_job_keywords(first_line, raw, profile=profile)
        return "job", terms, {
            "source": "job_file", "title": str(redact(first_line)), "path": str(job_file),
            "requirements": classify_requirements(raw, terms),
        }

    if job_arg:
        job_data = _find_local_job(job_arg, run_id=run_id)
        source = "local_run"
        if job_data is None:
            if not job_arg.startswith(("http://", "https://")):
                raise ValueError(f"job {job_arg!r} has no surviving local posting snapshot")
            if not fetch:
                raise ValueError("job has no surviving local posting snapshot and --no-fetch was set")
            job_data = _fetch_job_url(job_arg, profile_slug=profile_slug)
            source = "fetched_url"
        title = str(job_data.get("title") or "")
        body = str(job_data.get("body_text") or "")
        terms = extract_job_keywords(title, body, profile=profile)
        return "job", terms, {
            "source": source,
            "identity_key": job_data.get("identity_key"),
            "run_id": job_data.get("run_id"),
            "requirements": classify_requirements(body, terms),
            "title": str(redact(title)),
            "company": str(redact(str(job_data.get("company") or ""))),
            "apply_url": job_data.get("apply_url"),
        }

    if run_id and not profile_slug:
        terms, meta = _run_aggregate_terms(run_id)
        return "run_aggregate", terms, meta

    if profile is not None:
        terms = _profile_target_terms(profile)
        if run_id:
            agg_terms, _meta = _run_aggregate_terms(run_id)
            for term in agg_terms:
                if normalize_text(term) not in {normalize_text(t) for t in terms}:
                    terms.append(term)
        return "profile", terms[:40], {"profile": profile_slug, "run_id": run_id}

    return "general", [], {}


def audit_target_alignment(
    text: str, target_terms: list[str], requirements: list[dict] | None = None,
) -> tuple[int | None, dict[str, Any]]:
    """Score how well CV text covers ``target_terms`` and report missing terms."""
    if not target_terms:
        return None, {"terms_checked": 0, "matched": [], "missing": [], "coverage_pct": None}
    matched = [term for term in target_terms if contains_term(text, term)]
    missing = [term for term in target_terms if term not in matched]
    pct = round(100.0 * len(matched) / len(target_terms), 1)
    score = round(20.0 * len(matched) / len(target_terms))
    criteria = [
        {**row, "matched": [term for term in row["alternatives"] if term in matched]}
        for row in requirements or []
    ]
    stated = [row for row in criteria if row["category"] != "unspecified"]
    if stated:
        weights = {"required": 3, "preferred": 1}
        total = sum(weights[row["category"]] for row in stated)
        fulfilled = sum(weights[row["category"]] for row in stated if row["matched"])
        score = round(20.0 * fulfilled / total)
    return score, {
        "criteria": criteria,
        "terms_checked": len(target_terms),
        "matched": matched,
        "missing": missing,
        "coverage_pct": pct,
    }



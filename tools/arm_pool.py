"""Pooling and blind labels for the arm comparison: one job id per posting, arms hidden.

Only the scored rows (the top N of every repetition) are pooled. Rows are one job when their canonical
posting identity matches, or when they share company, normalized title and location. Job ids are kept
in the session state, so labels survive later ingestions in any order.
"""

from __future__ import annotations

import hashlib
from typing import Any

import yaml
from arm_metrics import LABEL_FIELDS

from ultron.core.job_identity import canonical_job_identity
from ultron.core.ranker.final_rows import normalize_title_for_dedup

PENDING = "pending"
LABEL_VALUES = (True, False, None, PENDING)
PLACEHOLDER_COMPANIES = {"", "not found", "unknown", "n/a", "company not found", "confidential",
                         "confidential company", "undisclosed", "stealth", "stealth startup"}
LABELS_HEADER = (
    "# Blind labels: one entry per pooled job, arms hidden. Replace each 'pending' with true, false or\n"
    "# null (null = cannot tell; left out of the metrics).\n"
    "# relevant: worth applying to for this profile (needs open, eligible and role_fit not false).\n"
    "# open: still accepting applications. eligible: the candidate may apply (location, remote,\n"
    "# authorization). role_fit: the role matches the profile. Several variants = same posting.\n"
)


class LabelError(ValueError):
    """The labels or spot-check file is malformed or contradictory."""


def _fold(text: str) -> str:
    return " ".join(str(text or "").casefold().split())


def row_keys(row: dict[str, str]) -> list[str]:
    """Matching keys of a row: its canonical identity, plus an alias when company, title and location are usable."""
    keys = ["id:" + canonical_job_identity(row["apply_url"]).identity_key]
    company, title = _fold(row.get("company", "")), normalize_title_for_dedup(row["title"])
    location = _fold(row.get("location", ""))
    if company not in PLACEHOLDER_COMPANIES and title and location:
        keys.append(f"alias:{company}|{title}|{location}")
    return keys


class _Clusters:
    """Union-find over row keys."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, key: str) -> str:
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, keys: list[str]) -> None:
        roots = sorted({self.find(key) for key in keys})
        for root in roots[1:]:
            self.parent[root] = roots[0]


def scored_rows(state: dict[str, Any]) -> list[tuple[str, str, int, dict[str, str]]]:
    """``(arm, rep, index, row)`` for the top N rows of every ingested repetition."""
    top_n = state["top_n"]
    return [(arm, rep, index, row)
            for arm, reps in sorted(state["arms"].items())
            for rep, entry in sorted(reps.items())
            for index, row in enumerate(entry.get("rows", [])[:top_n])]


def _job_id(identity: str) -> str:
    return "j" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def assign_ids(state: dict[str, Any]) -> dict[str, Any]:
    """Cluster the scored rows and give each cluster a job id, reusing ids already in ``state``.

    Updates ``state["job_ids"]`` (identity key -> job id). Returns ``jobs`` (job id -> title, company and
    the distinct url/location variants), ``rows`` ((arm, rep, index) -> job id) and ``merged`` (job ids
    that a new row joined into one; their labels must be checked).
    """
    rows = scored_rows(state)
    clusters = _Clusters()
    for *_, row in rows:
        clusters.union(row_keys(row))
    members: dict[str, list[tuple[str, str, int, dict[str, str]]]] = {}
    for item in rows:
        members.setdefault(clusters.find(row_keys(item[3])[0]), []).append(item)
    known = state.setdefault("job_ids", {})
    jobs: dict[str, dict[str, Any]] = {}
    row_ids: dict[tuple[str, str, int], str] = {}
    merged: list[list[str]] = []
    for group in members.values():
        identities = sorted({row_keys(row)[0] for *_, row in group})
        existing = sorted({known[key] for key in identities if key in known})
        job = existing[0] if existing else _job_id(identities[0])
        if len(existing) > 1:
            merged.append(existing)
        known.update(dict.fromkeys(identities, job))
        first = group[0][3]
        variants = sorted({(row["apply_url"], row.get("location", "")) for *_, row in group})
        jobs[job] = {"title": first["title"], "company": first.get("company", ""),
                     "variants": [{"apply_url": url, "location": loc} for url, loc in variants]}
        row_ids.update({(arm, rep, index): job for arm, rep, index, _ in group})
    return {"jobs": jobs, "rows": row_ids, "merged": merged}


def blind_order(job_id: str, salt: str) -> str:
    """A stable shuffle key, so entries never appear in arm or rank order."""
    return hashlib.sha256(f"{salt}:{job_id}".encode()).hexdigest()


def _check_value(job_id: str, field: str, value: Any) -> None:
    if not (value is None or isinstance(value, bool) or value == PENDING):
        raise LabelError(f"{job_id}: {field} must be true, false, null or pending")


def _check_consistent(job_id: str, label: dict[str, Any]) -> None:
    if label["relevant"] is True and any(label[field] is False for field in ("open", "eligible", "role_fit")):
        raise LabelError(f"{job_id}: relevant is true but open, eligible or role_fit is false")


def parse_labels(text: str, session: str, fields: tuple[str, ...] = LABEL_FIELDS) -> dict[str, dict[str, Any]]:
    """Validated labels by job id from a labels or spot-check file of ``session``."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise LabelError(f"invalid YAML{where}") from None
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise LabelError("expected a mapping with a 'jobs' list")
    if data.get("session") != session:
        raise LabelError(f"the file belongs to session {data.get('session')!r}, not {session!r}")
    labels: dict[str, dict[str, Any]] = {}
    for job in data["jobs"]:
        if not isinstance(job, dict) or not isinstance(job.get("id"), str):
            raise LabelError("every job needs an id")
        if job["id"] in labels:
            raise LabelError(f"{job['id']}: listed twice")
        for field in fields:
            _check_value(job["id"], field, job.get(field, PENDING))
        labels[job["id"]] = {field: job.get(field, PENDING) for field in fields}
    if fields == LABEL_FIELDS:
        for job_id, label in labels.items():
            _check_consistent(job_id, label)
    return labels


def scoring_labels(labels: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``labels`` with ``pending`` read as unknown (``None``)."""
    return {job: {field: (None if value == PENDING else value) for field, value in label.items()}
            for job, label in labels.items()}


def pending_jobs(labels: dict[str, dict[str, Any]], jobs: dict[str, Any]) -> list[str]:
    """Pooled jobs missing from ``labels`` or with any field still ``pending``."""
    return sorted(job for job in jobs if job not in labels or PENDING in labels[job].values())


def labels_yaml(session: str, jobs: dict[str, dict[str, Any]], existing: dict[str, dict[str, Any]]) -> str:
    """The blind labels file: every pooled job once, shuffled, known labels kept."""
    entries = []
    for job_id in sorted(jobs, key=lambda item: blind_order(item, session)):
        known = existing.get(job_id, {})
        entries.append({"id": job_id, **jobs[job_id], **{f: known.get(f, PENDING) for f in LABEL_FIELDS}})
    body = yaml.safe_dump({"session": session, "jobs": entries}, allow_unicode=True, sort_keys=False)
    return LABELS_HEADER + body


def spotcheck_yaml(session: str, jobs: dict[str, dict[str, Any]], size: int) -> str:
    """A reproducible blind sample of ``size`` pooled jobs for the user to label ``relevant``."""
    sample = sorted(jobs, key=lambda item: blind_order(item, f"spot:{session}"))[:size]
    entries = [{"id": job_id, **jobs[job_id], "relevant": PENDING} for job_id in sample]
    header = "# Spot check: set relevant to true, false or null for each job without looking at labels.yaml.\n"
    return header + yaml.safe_dump({"session": session, "jobs": entries}, allow_unicode=True, sort_keys=False)


def agreement(labels: dict[str, dict[str, Any]], spot: dict[str, dict[str, Any]], threshold: float) -> dict[str, Any]:
    """Agreement on ``relevant`` between the labels and the user's spot check."""
    judged = {job: value["relevant"] for job, value in spot.items() if value["relevant"] in (True, False)}
    compared = {job: v for job, v in judged.items() if labels.get(job, {}).get("relevant") in (True, False)}
    disagree = sorted(job for job, value in compared.items() if labels[job]["relevant"] is not value)
    rate = round(len(disagree) / len(compared), 4) if compared else None
    # A sampled job is pending until both the user and the main labels have judged it.
    pending = sum(1 for job, value in spot.items()
                  if value["relevant"] == PENDING or labels.get(job, {}).get("relevant", PENDING) == PENDING)
    return {"compared": len(compared), "pending": pending, "disagreements": disagree,
            "disagreement_rate": rate, "relabel": rate is not None and rate > threshold}

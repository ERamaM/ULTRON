"""Helpers shared by the sweep and scout ``finalize``: candidates, discards, identities and seed yields."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ultron.core.ats.identity import seed_identity
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import CandidateUrl, DiscardRecord, ExtractedJob, FinalRow
from ultron.stages.discover import SEED_ATTEMPTS_FILE
from ultron.storage import runs

TOP_N = 5


def candidate_of(raw: dict | None, job: ExtractedJob) -> CandidateUrl:
    """The candidate a posting came from, or a minimal one built from the job when it is missing or invalid."""
    try:
        return CandidateUrl.model_validate(raw or {})
    except ValidationError:
        return CandidateUrl(
            query_id="", query="", source_url=job.apply_url, normalized_url=job.apply_url, domain=job.domain or ""
        )


def discard_record(job: ExtractedJob, reason: str, detail: str = "") -> DiscardRecord:
    """A discard record for ``job``."""
    return DiscardRecord(
        apply_url=job.apply_url, reason=reason, detail=detail, title=job.title, company=job.company,
        location=job.location, remote_text=job.remote_text,
    )


def row_identity(row: FinalRow) -> str:
    """The canonical identity key of a final row."""
    return canonical_job_identity(row.apply_url, source_type=row.source_type, source_key=row.source_key).identity_key


def seed_kept(out_dir: Path, rows: list[FinalRow]) -> dict[str, int] | None:
    """Kept postings per seed this run queried (``None`` when ``discover ats`` recorded no attempts)."""
    path = out_dir / SEED_ATTEMPTS_FILE
    if not path.exists():
        return None
    kept = {str(r.get("seed_key") or ""): 0 for r in runs.read_jsonl(path) if isinstance(r, dict)}
    kept.pop("", None)
    for row in rows:
        vendor, sep, board = str(row.source_key or "").partition(":")
        if row.source_type != "ats_api" or not sep:
            continue
        key = ":".join(seed_identity(vendor, board))
        if key in kept:
            kept[key] += 1
    return kept

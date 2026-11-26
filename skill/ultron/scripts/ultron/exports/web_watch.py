"""Read bounded watch closure digests for the ledger beside them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ultron.core.job_identity import canonical_job_identity
from ultron.exports import sanitize
from ultron.storage.watch_digests import latest_closures


def read_closed_watch(folder: Path, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read the latest closure digest per profile and flag matching ledger jobs."""
    by_url = {job["canonical_url"]: job["identity_key"] for job in jobs if job["canonical_url"]}
    known_keys = {job["identity_key"] for job in jobs}
    alerts: dict[tuple[str, str], dict[str, Any]] = {}
    for profile, (stamp, rows) in latest_closures(folder).items():
        for row in rows:
            if not isinstance(row, dict) or not (url := sanitize.safe_http_url(row.get("apply_url"))):
                continue
            identity = canonical_job_identity(url)
            key = (identity.identity_key if identity.identity_key in known_keys else
                   by_url.get(identity.canonical_url, by_url.get(url, "")))
            alerts.setdefault((profile, key or url), {
                "identity_key": key,
                "title": sanitize.clean_text(row.get("title")),
                "company": sanitize.clean_text(row.get("company")),
                "url": url,
                "profile_slug": sanitize.clean_text(profile),
                "detected_at": sanitize.clean_text(row.get("detected_at")) or stamp,
            })
    closed_keys = {row["identity_key"] for row in alerts.values() if row["identity_key"]}
    for job in jobs:
        job["is_closed_watch"] = job["identity_key"] in closed_keys
    return list(alerts.values())

"""Verified finds: postings the agent opened and judged itself, taken by ULTRON as they are.

A verified find is a ``candidates add`` row with a ``verdict``. ULTRON normalizes its URL, removes duplicates and
flags it against memory, but never fetches, filters or queues it again. Its ``keep`` and ``unsure`` rows lead the
shortlist; board expansion and the watch only add extras below them (specification 30).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

from ultron.core.extractors.posting_date import iso_vacancy_date
from ultron.core.models import Eligibility, FinalRow, RemotePolicy
from ultron.core.ranker.final_rows import VERIFIED_FOUND_BY
from ultron.core.text_clean import clean_text
from ultron.exports.sanitize import clean_text as sanitize_export_text
from ultron.storage import runs

JUDGED_FILE = "judged_finds.jsonl"
JUDGED_PASS = "judged"
AGENT_REJECTED = "agent_rejected"
VERDICTS = ("keep", "unsure", "discard")
EXPORTED_VERDICTS = frozenset({"keep", "unsure"})
REMOTE_POLICIES = frozenset(get_args(RemotePolicy))
ELIGIBILITIES = frozenset(get_args(Eligibility))
TEXT_LIMIT = 500
EVIDENCE_LIMIT = 300
MAX_FIT_SIGNALS = 8
DEFAULT_PRIORITY = 2


class InvalidFind(ValueError):
    """A verified find row with a missing or invalid field; the message names the field."""


def is_verified(row: Any) -> bool:
    """Whether a ``candidates add`` row is a verified find (it carries a ``verdict``)."""
    return isinstance(row, dict) and "verdict" in row


def _text(value: Any, limit: int) -> str:
    return clean_text(value)[:limit]


def _priority(row: dict) -> int:
    value = row.get("priority", DEFAULT_PRIORITY)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3:
        raise InvalidFind("priority must be 1, 2 or 3")
    return value


def _choice(row: dict, field: str, allowed: frozenset[str], default: str) -> str:
    value = row.get(field, default)
    if not isinstance(value, str) or value not in allowed:
        raise InvalidFind(f"{field} must be one of {', '.join(sorted(allowed))}")
    return value


def _fit_signals(row: dict) -> list[str]:
    value = row.get("fit_signals", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidFind("fit_signals must be a list of strings")
    return [_text(item, 60) for item in value if clean_text(item)][:MAX_FIT_SIGNALS]


def judgment(row: dict) -> dict[str, Any]:
    """The validated judgment of a verified find row (everything but the URL); ``InvalidFind`` otherwise."""
    verdict = row.get("verdict")
    if verdict not in VERDICTS:
        raise InvalidFind("verdict must be keep, unsure or discard")
    title, company = _text(row.get("title"), TEXT_LIMIT), _text(row.get("company"), TEXT_LIMIT)
    if verdict in EXPORTED_VERDICTS and not title:
        raise InvalidFind("title is required for keep and unsure")
    if verdict in EXPORTED_VERDICTS and not company:
        raise InvalidFind("company is required for keep and unsure")
    return {
        "title": title,
        "company": company,
        "location": _text(row.get("location"), TEXT_LIMIT),
        "verdict": verdict,
        "priority": _priority(row),
        "remote_policy": _choice(row, "remote_policy", REMOTE_POLICIES, "unknown"),
        "eligibility": _choice(row, "eligibility", ELIGIBILITIES, "high" if verdict == "keep" else "unknown"),
        "eligibility_evidence": _text(row.get("eligibility_evidence"), EVIDENCE_LIMIT),
        "fit_signals": _fit_signals(row),
        "salary": sanitize_export_text(row.get("salary"))[:120] if isinstance(row.get("salary"), str) else "",
        "published_at": iso_vacancy_date(row.get("published_at")),
        "discard_reason": _text(row.get("discard_reason"), EVIDENCE_LIMIT) if verdict == "discard" else "",
    }


def load_verified(out_dir: Path) -> dict[str, dict]:
    """Verified finds of a run by identity key, the last judgment of each identity winning."""
    return {row["identity_key"]: row for row in runs.read_jsonl(Path(out_dir) / JUDGED_FILE)}


def store_verified(out_dir: Path, rows: list[dict]) -> dict[str, dict]:
    """Merge ``rows`` into the run's verified finds (a later row replaces the same identity) and save them."""
    merged = load_verified(out_dir)
    for row in rows:
        merged.pop(row["identity_key"], None)
        merged[row["identity_key"]] = row
    runs.write_jsonl(Path(out_dir) / JUDGED_FILE, merged.values())
    return merged


def verdict_counts(verified: dict[str, dict]) -> dict[str, int]:
    """How many verified finds of each verdict a run holds."""
    counts = dict.fromkeys(VERDICTS, 0)
    for row in verified.values():
        counts[row["verdict"]] += 1
    return counts


def exported_count(verified: dict[str, dict]) -> int:
    """Verified finds that will be exported (``keep`` and ``unsure``)."""
    return sum(1 for row in verified.values() if row["verdict"] in EXPORTED_VERDICTS)


def final_row(row: dict) -> FinalRow:
    """The shortlist row of a verified ``keep`` or ``unsure`` find, exactly as the agent judged it."""
    return FinalRow(
        title=row["title"], company=row["company"], apply_url=row["apply_url"], location=row["location"],
        remote_policy=row["remote_policy"], eligibility=row["eligibility"],
        eligibility_evidence=row["eligibility_evidence"], fit_signals=row["fit_signals"], freshness="",
        evaluation_verdict=row["verdict"], source_type=row["source_type"], source_key=row.get("source_key", ""),
        source_label="Agent find", salary=row.get("salary", ""), published_at=row.get("published_at"),
        priority=row["priority"], found_by=VERIFIED_FOUND_BY,
    )


def memory_item(row: dict, exported: FinalRow | None) -> dict[str, Any]:
    """The scout-memory record of a verified find; ``content_hash`` stays empty until a posting is fetched."""
    discard = row["verdict"] == "discard"
    return {
        "identity_key": row["identity_key"], "verdict": row["verdict"],
        "discard_category": AGENT_REJECTED if discard else "", "priority": None if discard else row["priority"],
        "content_hash": "", "company": row["company"], "row": exported,
    }

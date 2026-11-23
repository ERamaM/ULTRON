"""Agent evaluation records: ``evaluate record`` and ``evaluate pending``.

Both require the ``filter`` stage to be done (``StageOrderError("evaluate", "filter")``
otherwise); ``evaluate`` itself has no declared prerequisite in ``runs.STAGE_REQUIRES``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from ultron.core.models import JobEvaluation
from ultron.core.rules.geography import build_geo_body_excerpt
from ultron.core.rules.term_matching import normalize_text
from ultron.storage import runs

EXCERPT_CHARS = 1500
GEO_EXCERPT_CHARS = 800


_EVIDENCE_BOUNDARY = re.compile(r"\r?\n|(?<=[.!?])[ \t]+(?=[A-Z])")
_YEARS = r"(?:[0-9]{1,2}\+?|one|two|three|four|five|six|seven|eight|nine|ten|tres|cuatro|cinco|seis)"
_EVIDENCE_PATTERNS = {
    "Experience": re.compile(rf"\b{_YEARS}[ \t]+(?:years?|anos?)\b|\bequivalent (?:practical )?experience\b"),
    "Work policy": re.compile(r"\b(?:remot[eo]|hybrid|hibrido|onsite|on-site|presencial|office days)\b"),
    "Eligibility": re.compile(
        r"\b(?:eligible|eligibility|work authori[sz]ation|sponsorship|must reside|candidates based|"
        r"right to work|permiso de trabajo|residir|candidatos en)\b"
    ),
    "Responsibilities": re.compile(
        r"\b(?:responsibilities|responsible for|you will|what you.ll do|responsabilidades|te encargaras|"
        r"tu mision)\b"
    ),
}


_CONTINUED = re.compile(r"\b(?:not|no|is|are|be|must|should|for|en|es|son|ser)\s*$")


def _clause_segments(body: str) -> list[str]:
    segments: list[str] = []
    for raw in _EVIDENCE_BOUNDARY.split(body):
        fragment = raw.strip()
        if not fragment:
            continue
        previous = segments[-1] if segments else ""
        if previous and (previous.endswith(":") or _CONTINUED.search(normalize_text(previous))):
            segments[-1] = f"{previous}\n{fragment}"
        else:
            segments.append(fragment)
    return segments


def _evidence_excerpt(body: str) -> tuple[str, bool]:
    evidence: dict[str, list[str]] = {label: [] for label in _EVIDENCE_PATTERNS}
    segments = _clause_segments(body)
    for segment in segments:
        folded = normalize_text(segment)
        for label, pattern in _EVIDENCE_PATTERNS.items():
            entries = evidence[label]
            if pattern.search(folded) and segment not in entries and len(entries) < 3:
                entries.append(segment)
    parts = []
    selected = set()
    remaining = EXCERPT_CHARS
    for index in range(3):
        for label, entries in evidence.items():
            if index >= len(entries) or entries[index] in selected:
                continue
            fragment = entries[index]
            line = f"{label}: {fragment}"
            if len(line) + 1 <= remaining:
                parts.append(line)
                selected.add(fragment)
                remaining -= len(line) + 1
    if not parts:
        return body[:EXCERPT_CHARS], len(body) > EXCERPT_CHARS
    context = "\n".join(segment for segment in segments if segment not in selected)
    if len(body) <= EXCERPT_CHARS and len(context) + len("Context: ") > remaining:
        return body, False
    bounded_context = context[:max(0, remaining - len("Context: "))]
    if bounded_context:
        parts.append(f"Context: {bounded_context}")
    return "\n".join(parts), len(context) > len(bounded_context)


class RecordError(ValueError):
    """Invalid evaluation input; ``payload`` is the JSON error detail."""

    def __init__(self, payload: dict):
        super().__init__(json.dumps(payload, ensure_ascii=False))
        self.payload = payload


def _require_filter(run_id: str) -> None:
    if "filter" not in runs.load_run(run_id)["stages_done"]:
        raise runs.StageOrderError("evaluate", "filter")


def record_evaluations(run_id: str, path: Path) -> dict:
    """Store agent evaluation verdicts for ambiguous postings in a run."""
    _require_filter(run_id)
    out_dir = runs.run_dir(run_id)
    ambiguous = {r["identity_key"] for r in runs.read_jsonl(out_dir / "ambiguous.jsonl")}
    valid = ambiguous | {r["identity_key"] for r in runs.read_jsonl(out_dir / "kept.jsonl")}

    parsed: list[tuple[str, dict]] = []
    for number, line in enumerate(runs.jsonl_lines(Path(path).read_text(encoding="utf-8")), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise RecordError({"line": number, "reason": f"invalid JSON: {exc}"}) from None
        if not isinstance(row, dict) or not isinstance(row.get("identity_key"), str):
            raise RecordError({"line": number, "reason": "expected an object with a string identity_key"})
        key = row["identity_key"]
        if key not in valid:
            raise RecordError({
                "line": number, "identity_key": key,
                "reason": "identity_key is not in this run's ambiguous or kept postings",
            })
        try:
            evaluation = JobEvaluation.model_validate(row.get("evaluation"))
        except ValidationError as exc:
            reason = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            raise RecordError({"line": number, "identity_key": key, "reason": reason}) from None
        evaluation.evaluation_source = "agent"
        parsed.append((key, evaluation.model_dump(mode="json")))

    merged = {r["identity_key"]: r for r in runs.read_jsonl(out_dir / "evaluations.jsonl")}
    for key, evaluation in parsed:
        merged.pop(key, None)  # re-insert so file order follows the last write
        merged[key] = {"identity_key": key, "evaluation": evaluation}
    runs.write_jsonl(out_dir / "evaluations.jsonl", merged.values())
    runs.mark_stage(run_id, "evaluate")

    return {
        "recorded": len(parsed),
        "pending": len(valid - merged.keys()),
        "pending_ambiguous": len(ambiguous - merged.keys()),
    }


def pending_batch(run_id: str, limit: int = 10) -> dict:
    """Return up to ``limit`` unevaluated ambiguous postings for ``run_id``."""
    _require_filter(run_id)
    out_dir = runs.run_dir(run_id)
    done = {r["identity_key"] for r in runs.read_jsonl(out_dir / "evaluations.jsonl")}
    rows = runs.read_jsonl(out_dir / "ambiguous.jsonl") + runs.read_jsonl(out_dir / "kept.jsonl")
    todo = [r for r in rows if r["identity_key"] not in done]
    # R15: heuristic "unsure" rows first, then rescued heuristic discards (they stay discarded unless
    # evaluated), then the keeps (stable sort keeps file order within each group).
    todo.sort(key=lambda r: ((r.get("heuristic") or {}).get("verdict") != "unsure", "rescued_discard" not in r))
    items = []
    for row in todo[: max(limit, 0)]:
        job, heuristic = row["job"], row["heuristic"]
        body = job.get("body_text") or ""
        excerpt, truncated = _evidence_excerpt(body)
        items.append({
            "identity_key": row["identity_key"],
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "remote_text": job.get("remote_text", ""),
            "heuristic": {"verdict": heuristic.get("verdict"), "discard_reason": heuristic.get("discard_reason")},
            "body_excerpt": excerpt,
            "body_excerpt_truncated": truncated,
            "geo_excerpt": build_geo_body_excerpt(body, max_chars=GEO_EXCERPT_CHARS),
        })
    return {"pending": len(todo), "returned": len(items), "items": items}

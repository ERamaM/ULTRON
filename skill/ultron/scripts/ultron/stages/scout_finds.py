"""``candidates add`` in scout runs: resolve every find, and take verified finds as the agent judged them.

A find is a JSONL row with a ``url``. A row with a ``verdict`` is a verified find: the agent opened the posting and
judged it, so ULTRON only normalizes it, removes duplicates and flags it against memory (``scout_verified``). A
row without one is an unverified find: it is fetched, filtered and queued like any other candidate.

Either way, a supported ATS posting keeps its URL, an employer page is kept as an ``employer_page`` posting, and
an aggregator page is resolved to the employer posting it links to or stored as ``unresolved_aggregator``.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from typing import Any

from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import extract_seed_from_url, infer_vendor_from_host
from ultron.core.discovery.url_filter import domain_of, normalize_url
from ultron.core.models import CandidateUrl
from ultron.core.text_clean import clean_text
from ultron.infra import home
from ultron.seeds import Proposal, load_inventory
from ultron.stages import scout_verified
from ultron.stages.candidates import candidate_identity
from ultron.stages.http_factory import HOST_BLOCK_THRESHOLD, client_for_run
from ultron.stages.scout_resolve import EMPLOYER_PAGE, Resolution, classify_find, is_recruiter, resolve_aggregator
from ultron.storage import ledger, runs, scout_memory

UNRESOLVED_FILE = "unresolved_finds.jsonl"
BLOCKED_FLAGS = {"rejected": "already_rejected", "applied": "already_applied"}


def web_candidate(url: str, row: dict, query_id: str, source_type: str = "web_search", search_pass: str = "") -> CandidateUrl:
    """The candidate row of a find at ``url``; ``source_type`` is ``web_search`` or ``employer_page``."""
    title, company = clean_text(row.get("title")), clean_text(row.get("company"))
    normalized = normalize_url(url)
    label = "Employer page" if source_type == EMPLOYER_PAGE else "Web search"
    return CandidateUrl(
        query_id=query_id, query=str(row.get("query") or "").strip(), source_url=url, normalized_url=normalized,
        domain=domain_of(normalized), ats_title=title, ats_company=company, ats_location=clean_text(row.get("location")),
        source_type=source_type, source_key=source_type, source_label=label, hit_title=title, company_hint=company,
        search_pass=search_pass,
    )


def seed_proposal(url: str, company: str, known: set, proposed: set) -> dict | None:
    """A seed proposal for the supported ATS board of ``url`` when it is neither known nor proposed yet."""
    if not infer_vendor_from_host(domain_of(normalize_url(url))) or is_recruiter(company):
        return None
    seed = extract_seed_from_url(url, company)
    if seed is None:
        return None
    key = (seed.ats, seed.board_identifier)
    if key in proposed or seed_identity(*key) in known:
        return None
    proposed.add(key)
    return asdict(Proposal(company=company or seed.company, ats=seed.ats, board=seed.board_identifier, careers_url=url))


def _resolve(row: dict, run: dict, client_box: list) -> Resolution:
    url = str(row.get("url") or "").strip()
    found = classify_find(url)
    if found.outcome != "aggregator":
        return found
    if not client_box:
        client_box.append(client_for_run(run, block_threshold=HOST_BLOCK_THRESHOLD))
    return resolve_aggregator(url, client_box[0], run["settings"]["ats_api_timeout"], clean_text(row.get("company")))


def memory_flags(profile_slug: str, keys: list[str]) -> dict[str, str]:
    """The scout-memory flag of each identity key (``new`` for every key before the first ledger exists)."""
    if not keys or not (home.ultron_home() / "ultron.sqlite3").exists():
        return dict.fromkeys(keys, "new")
    with closing(ledger.connect()) as conn:
        return scout_memory.flags(conn, profile_slug, keys)


class _Batch:
    """The state one ``candidates add`` call builds before it writes the run files."""

    def __init__(self, run_id: str, run: dict) -> None:
        self.run, self.out_dir = run, runs.run_dir(run_id)
        self.candidates = runs.read_jsonl(self.out_dir / "candidates.jsonl")
        self.by_identity = {candidate_identity(row): row for row in self.candidates}
        self.known = {seed_identity(s.ats, s.board_identifier) for s in load_inventory(run["profile_slug"])}
        self.proposed = {(r.get("ats"), r.get("board"))
                         for r in runs.read_jsonl(self.out_dir / "seed_proposals_from_search.jsonl")}
        self.added = 0
        self.unresolved: list[dict] = []
        self.proposals: list[dict] = []
        self.verified: list[dict] = []

    def add_candidate(self, candidate: CandidateUrl, identity: str, *, judged: bool) -> bool:
        """Add ``candidate`` unless the run has it; a verified find marks an existing one as judged."""
        existing = self.by_identity.get(identity)
        if existing is not None:
            if judged:
                existing["search_pass"] = scout_verified.JUDGED_PASS
            return False
        row = candidate.model_dump(mode="json")
        self.candidates.append(row)
        self.by_identity[identity] = row
        self.added += 1
        if proposal := seed_proposal(candidate.source_url, candidate.ats_company, self.known, self.proposed):
            self.proposals.append(proposal)
        return True


def _first_pass(rows: list, run: dict, batch: _Batch, boards: dict[str, str]) -> list[dict]:
    """Validate, resolve and identify every row; nothing is written yet.

    ``boards`` maps posting URLs to the ``vendor:board`` that listed them (trusted, from ULTRON's own records), so
    a posting on an employer's domain keeps its board identity.
    """
    client_box: list = []
    items = []
    for number, raw in enumerate(rows, start=1):
        row = raw if isinstance(raw, dict) else {}
        item: dict[str, Any] = {"row": number, "url": str(row.get("url") or ""), "_row": row}
        if scout_verified.is_verified(row):
            try:
                item["_judgment"] = scout_verified.judgment(row)
            except scout_verified.InvalidFind as exc:
                items.append({**item, "outcome": "invalid", "reason": str(exc)})
                continue
        result = _resolve(row, run, client_box)
        item["outcome"] = result.outcome
        if result.reason:
            item["reason"] = result.reason
        if result.outcome == "unresolved_aggregator":
            batch.unresolved.append({"row": number, "url": item["url"], "outcome": result.outcome,
                                     "reason": result.reason, "company": clean_text(row.get("company")),
                                     "title": clean_text(row.get("title"))})
        if result.url:
            judged = "_judgment" in item
            item["_candidate"] = web_candidate(
                result.url, row, f"scout:{len(batch.candidates) + number:03d}", result.source_type,
                scout_verified.JUDGED_PASS if judged else "",
            )
            if board := boards.get(item["_candidate"].normalized_url):
                item["_candidate"].source_type, item["_candidate"].source_key = "ats_api", board
            item["identity_key"] = candidate_identity(item["_candidate"].model_dump())
            item["canonical_url"] = item["_candidate"].normalized_url
        items.append(item)
    return items


def _store(item: dict, batch: _Batch) -> None:
    """Record one resolved find: a verified find with its judgment, an unverified one as a candidate."""
    candidate, identity, flag = item["_candidate"], item["identity_key"], item["memory"]
    judgment = item.get("_judgment")
    if judgment is None:
        if not batch.add_candidate(candidate, identity, judged=False):
            item["outcome"] = "duplicate"
        return
    if flag in BLOCKED_FLAGS:
        item["outcome"] = BLOCKED_FLAGS[flag]
        return
    batch.add_candidate(candidate, identity, judged=True)
    board = candidate.source_key if candidate.source_type == "ats_api" else ""  # only ever set from trusted records
    batch.verified.append({**judgment, "identity_key": identity, "apply_url": candidate.normalized_url,
                           "source_type": candidate.source_type, "source_key": board, "memory": flag})
    item.update(outcome="verified", judged=True)


def _summary(items: list[dict], batch: _Batch, verified: dict[str, dict]) -> dict:
    report = [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]
    outcomes: dict[str, int] = {}
    memory: dict[str, int] = {}
    for item in report:
        outcomes[item["outcome"]] = outcomes.get(item["outcome"], 0) + 1
        if "memory" in item:
            memory[item["memory"]] = memory.get(item["memory"], 0) + 1
    return {"added": batch.added, "outcomes": outcomes, "memory": memory, "seed_proposals": len(batch.proposals),
            "verified": scout_verified.verdict_counts(verified), "rows": report}


def add_scout_finds(run_id: str, run: dict, rows: list, *, boards: dict[str, str] | None = None) -> dict:
    """Resolve the agent's finds in a scout run and report the outcome and memory flag of every row.

    Verified finds (rows with a ``verdict``) are stored in ``judged_finds.jsonl`` and never fetched or queued; a
    verified find the user rejected or applied to is reported and left out. Unverified finds become candidates
    as before. Aggregator finds that do not resolve go to ``unresolved_finds.jsonl`` and are never exported.
    Every new supported board is proposed as a seed (never one of a staffing agency) for ``discover ats``.
    ``boards`` (trusted posting URL -> ``vendor:board``) keeps the board identity of postings ULTRON listed.
    """
    batch = _Batch(run_id, run)
    items = _first_pass(rows, run, batch, boards or {})
    flags = memory_flags(run["profile_slug"], [item["identity_key"] for item in items if "identity_key" in item])
    for item in items:
        if "identity_key" in item:
            item["memory"] = flags[item["identity_key"]]
            _store(item, batch)
    runs.write_jsonl(batch.out_dir / "candidates.jsonl", batch.candidates)
    runs.append_jsonl(batch.out_dir / UNRESOLVED_FILE, batch.unresolved)
    if batch.proposals:
        runs.append_jsonl(batch.out_dir / "seed_proposals_from_search.jsonl", batch.proposals)
    verified = scout_verified.store_verified(batch.out_dir, batch.verified)
    runs.mark_stage(run_id, "candidates")
    return _summary(items, batch, verified)

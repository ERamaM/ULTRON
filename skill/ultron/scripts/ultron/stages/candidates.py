"""``candidates add``: merge agent web-search results into the run's candidates."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import extract_seed_from_url, infer_vendor_from_host
from ultron.core.discovery.url_filter import domain_of, normalize_url
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import CandidateUrl
from ultron.core.text_clean import clean_text
from ultron.infra import home
from ultron.infra.urlpolicy import URLRejected, admit_url
from ultron.seeds import Proposal, load_inventory
from ultron.stages.http_factory import HOST_BLOCK_THRESHOLD, client_for_run
from ultron.stages.scout_resolve import EMPLOYER_PAGE, Resolution, classify_find, is_recruiter, resolve_aggregator
from ultron.storage import ledger, runs, scout_memory

UNRESOLVED_FILE = "unresolved_finds.jsonl"


def candidate_identity(candidate: dict) -> str:
    """Return the canonical identity key string for a raw candidate row mapping."""
    return canonical_job_identity(
        candidate.get("source_url") or candidate.get("normalized_url") or "",
        source_type=candidate.get("source_type") or "",
        source_key=candidate.get("source_key") or "",
    ).identity_key


def _web_candidate(url: str, row: dict, query_id: str, source_type: str = "web_search") -> CandidateUrl:
    title, company = clean_text(row.get("title")), clean_text(row.get("company"))
    normalized = normalize_url(url)
    label = "Employer page" if source_type == EMPLOYER_PAGE else "Web search"
    return CandidateUrl(
        query_id=query_id, query=str(row.get("query") or "").strip(), source_url=url, normalized_url=normalized,
        domain=domain_of(normalized), ats_title=title, ats_company=company, ats_location=clean_text(row.get("location")),
        source_type=source_type, source_key=source_type, source_label=label, hit_title=title, company_hint=company,
    )


def _seed_proposal(url: str, company: str, known: set, proposed: set) -> dict | None:
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
    url = str(row.get("url") or "").strip() if isinstance(row, dict) else ""
    found = classify_find(url)
    if found.outcome != "aggregator":
        return found
    if not client_box:
        client_box.append(client_for_run(run, block_threshold=HOST_BLOCK_THRESHOLD))
    return resolve_aggregator(url, client_box[0], run["settings"]["ats_api_timeout"])


def _memory_flags(profile_slug: str, keys: list[str]) -> dict[str, str]:
    if not keys or not (home.ultron_home() / "ultron.sqlite3").exists():
        return dict.fromkeys(keys, "new")
    with closing(ledger.connect()) as conn:
        return scout_memory.flags(conn, profile_slug, keys)


def add_scout_finds(run_id: str, run: dict, rows: list) -> dict:
    """Resolve the agent's finds in a scout run and report the outcome and memory flag of every row.

    Supported ATS postings and employer pages become candidates. Aggregator finds become the employer posting
    they link to, or are stored in ``unresolved_finds.jsonl`` and never exported. Each new supported board is
    proposed as a seed (never one of a staffing agency), so ``discover ats`` verifies and expands it.
    """
    out_dir = runs.run_dir(run_id)
    existing = runs.read_jsonl(out_dir / "candidates.jsonl")
    seen = {candidate_identity(r) for r in existing}
    known = {seed_identity(s.ats, s.board_identifier) for s in load_inventory(run["profile_slug"])}
    proposed = {(r.get("ats"), r.get("board")) for r in runs.read_jsonl(out_dir / "seed_proposals_from_search.jsonl")}
    client_box: list = []
    report, added, unresolved, proposals = [], [], [], []
    for number, row in enumerate(rows, start=1):
        row = row if isinstance(row, dict) else {}
        result = _resolve(row, run, client_box)
        item = {"row": number, "url": str(row.get("url") or ""), "outcome": result.outcome}
        if result.reason:
            item["reason"] = result.reason
        if result.outcome == "unresolved_aggregator":
            unresolved.append({**item, "company": clean_text(row.get("company")), "title": clean_text(row.get("title"))})
        if not result.url:
            report.append(item)
            continue
        candidate = _web_candidate(result.url, row, f"scout:{len(existing) + len(added) + 1:03d}", result.source_type)
        identity = candidate_identity(candidate.model_dump())
        item.update(canonical_url=candidate.normalized_url, identity_key=identity)
        if identity in seen:
            item["outcome"] = "duplicate"
        else:
            seen.add(identity)
            added.append(candidate)
            if proposal := _seed_proposal(result.url, candidate.ats_company, known, proposed):
                proposals.append(proposal)
        report.append(item)
    flags = _memory_flags(run["profile_slug"], [i["identity_key"] for i in report if "identity_key" in i])
    for item in report:
        if "identity_key" in item:
            item["memory"] = flags[item["identity_key"]]
    runs.append_jsonl(out_dir / "candidates.jsonl", added)
    runs.append_jsonl(out_dir / UNRESOLVED_FILE, unresolved)
    if proposals:
        runs.append_jsonl(out_dir / "seed_proposals_from_search.jsonl", proposals)
    runs.mark_stage(run_id, "candidates")
    outcomes: dict[str, int] = {}
    for item in report:
        outcomes[item["outcome"]] = outcomes.get(item["outcome"], 0) + 1
    memory: dict[str, int] = {}
    for item in report:
        if "memory" in item:
            memory[item["memory"]] = memory.get(item["memory"], 0) + 1
    return {"added": len(added), "outcomes": outcomes, "memory": memory, "seed_proposals": len(proposals),
            "rows": report}


def add_candidates(run_id: str, path: Path) -> dict:
    """Append deduplicated web-search candidate URLs from ``path`` into a run (scout runs: resolve each find)."""
    run = runs.load_run(run_id)
    rows = runs.read_jsonl(path)
    if runs.run_mode(run) == "scout":
        return add_scout_finds(run_id, run, rows)
    out_dir = runs.run_dir(run_id)
    existing = runs.read_jsonl(out_dir / "candidates.jsonl")
    seen = {candidate_identity(row) for row in existing}
    known_seeds = {seed_identity(s.ats, s.board_identifier) for s in load_inventory(run["profile_slug"])}
    proposed = {(r.get("ats"), r.get("board")) for r in runs.read_jsonl(out_dir / "seed_proposals_from_search.jsonl")}

    added: list[CandidateUrl] = []
    duplicates = rejected = 0
    new_proposals: list[dict] = []
    for row in rows:
        url = str(row.get("url") or "").strip() if isinstance(row, dict) else ""
        try:
            admit_url(url)
        except URLRejected:
            rejected += 1
            continue
        title = clean_text(row.get("title"))
        company = clean_text(row.get("company"))
        normalized = normalize_url(url)
        candidate = CandidateUrl(
            query_id=f"web:{len(existing) + len(added) + 1:03d}",
            query=str(row.get("query") or "").strip(),
            source_url=url,
            normalized_url=normalized,
            domain=domain_of(normalized),
            ats_title=title,
            ats_company=company,
            source_type="web_search",
            source_key="web_search",
            source_label="Web search",
            hit_title=title,
            company_hint=company,
        )
        identity = candidate_identity(candidate.model_dump())
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        added.append(candidate)
        if infer_vendor_from_host(candidate.domain):
            seed = extract_seed_from_url(url, company)
            if seed is None:
                continue
            key = (seed.ats, seed.board_identifier)
            if key not in proposed and seed_identity(*key) not in known_seeds:
                proposed.add(key)
                new_proposals.append(asdict(Proposal(
                    company=company or seed.company, ats=seed.ats, board=seed.board_identifier, careers_url=url,
                )))
    runs.append_jsonl(out_dir / "candidates.jsonl", added)
    if new_proposals:
        runs.append_jsonl(out_dir / "seed_proposals_from_search.jsonl", new_proposals)
    runs.mark_stage(run_id, "candidates")
    return {"added": len(added), "duplicates": duplicates, "rejected": rejected, "seed_proposals": len(new_proposals)}

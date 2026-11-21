"""``candidates add``: merge agent web-search results into the run's candidates."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import extract_seed_from_url, infer_vendor_from_host
from ultron.core.discovery.url_filter import domain_of, normalize_url
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import CandidateUrl
from ultron.core.text_clean import clean_text
from ultron.infra.urlpolicy import URLRejected, admit_url
from ultron.seeds import Proposal, load_inventory
from ultron.storage import runs


def _identity(candidate: dict) -> str:
    return canonical_job_identity(
        candidate.get("source_url") or candidate.get("normalized_url") or "",
        source_type=candidate.get("source_type") or "",
        source_key=candidate.get("source_key") or "",
    ).identity_key


def add_candidates(run_id: str, path: Path) -> dict:
    run = runs.load_run(run_id)
    rows = runs.read_jsonl(path)
    out_dir = runs.run_dir(run_id)
    existing = runs.read_jsonl(out_dir / "candidates.jsonl")
    seen = {_identity(row) for row in existing}
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
        identity = _identity(candidate.model_dump())
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

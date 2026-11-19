"""``ultron seeds list|verify``."""

from __future__ import annotations

import argparse
from pathlib import Path

from ultron import runs, seeds
from ultron.http_factory import client_for_run
from ultron.output import emit, fail


def _list(args: argparse.Namespace) -> int:
    inventory = seeds.load_inventory(args.profile)
    emit({
        "profile": args.profile,
        "count": len(inventory),
        "seeds": [
            {"company": s.company, "ats": s.ats, "board": s.board_identifier, "linked": args.profile in s.profiles}
            for s in inventory[:30]
        ],
    })
    return 0


def _proposal(row: dict) -> seeds.Proposal:
    keys = ("company", "domain", "ats", "board", "careers_url", "source_url")
    return seeds.Proposal(**{k: str(row.get(k) or "").strip() for k in keys})


def _verify(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit < 0:
        return fail("usage", "--limit must be >= 0")
    try:
        run = runs.load_run(args.run)
    except runs.RunNotFoundError as exc:
        return fail("run_not_found", str(exc))
    try:
        rows = runs.read_jsonl(args.source)
    except (OSError, ValueError) as exc:
        return fail("invalid_proposals", str(exc))
    is_feed_proposals = Path(args.source).name == seeds.FEED_PROPOSALS_FILE
    limit = args.limit
    if limit is None and is_feed_proposals:
        limit = run["settings"]["new_companies_per_run"]
    if limit is not None:
        rows = rows[:limit]
    # R14: verification probes many slugs on few ATS hosts; only the global budget bounds it (like fetch).
    client = client_for_run(run, max_requests_per_host=run["settings"]["max_pages"])
    timeout = run["settings"]["ats_api_timeout"]
    verified: list[seeds.VerifyResult] = []
    failed: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("company") or "").strip():
            failed.append({"company": None, "reason": "invalid_proposal", "proposal": row})
            continue
        result = seeds.verify_proposal(_proposal(row), client=client, timeout=timeout, verify_only=True)
        if result.status == "verified":
            verified.append(result)
        else:
            entry = {"company": result.proposal.company, "reason": result.reason, "proposal": row}
            if result.candidate:
                entry["candidate"] = result.candidate
            failed.append(entry)
    # Companies named by feeds are not agent proposals: label them like the unattended mode does.
    if is_feed_proposals:
        for result in verified:
            if result.seed:
                result.seed = seeds.as_feed_company(result.seed)
    try:
        new_added = seeds.add_seeds([r.seed for r in verified if r.seed], profile_slug=run["profile_slug"])
    except seeds.SeedFileError as exc:
        return fail("seed_file_corrupt", str(exc))
    out = runs.run_dir(args.run)
    runs.write_jsonl(out / "seeds_verified.jsonl", (
        {"company": r.proposal.company, "postings": r.postings, "seed": r.seed.model_dump(mode="json")}
        for r in verified if r.seed
    ))
    runs.write_jsonl(out / "seeds_unresolved.jsonl", failed)
    runs.mark_stage(args.run, "seeds")
    emit({
        "verified": len(verified),
        "new": new_added,
        "unresolved": len(failed),
        # Guessed boards that answered but are not provably the company's (never stored).
        "ownership_unknown": sum(1 for f in failed if f["reason"] == "ownership_unknown"),
        "wrong_tenant": sum(1 for f in failed if f["reason"] == "wrong_tenant"),
        "unresolved_sample": [{"company": f["company"], "reason": f["reason"]} for f in failed[:5]],
    })
    return 0


def register(subparsers) -> None:
    parser = subparsers.add_parser("seeds", help="seed inventory and company verification")
    sub = parser.add_subparsers(dest="seeds_command", required=True, parser_class=type(parser))
    lst = sub.add_parser("list", help="list inventory seeds, profile-linked first")
    lst.add_argument("--profile", default=None)
    lst.set_defaults(func=_list)
    verify = sub.add_parser("verify", help="verify agent-proposed companies and store them")
    verify.add_argument("--run", required=True)
    verify.add_argument("--from", dest="source", required=True)
    verify.add_argument(
        "--limit",
        type=int,
        default=None,
        help="maximum proposals to process (defaults to new_companies_per_run for seed_proposals_from_feeds.jsonl)",
    )
    verify.set_defaults(func=_verify)

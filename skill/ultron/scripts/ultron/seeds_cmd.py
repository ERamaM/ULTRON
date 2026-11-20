"""``ultron seeds list|plan|verify``."""

from __future__ import annotations

import argparse
from pathlib import Path

from ultron import runs, seed_rounds, seeds
from ultron.http_factory import HOST_BLOCK_THRESHOLD, client_for_run
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


def _plan(args: argparse.Namespace) -> int:
    try:
        emit(seed_rounds.plan(args.run))
    except runs.RunNotFoundError as exc:
        return fail("run_not_found", str(exc))
    return 0


def _round_rows(run_id: str, rows: list) -> tuple[list[dict], list[str], dict] | str:
    """Check a round file before any request: every line must match the schema (else an error string), at most
    ``ask`` lines are kept, and companies or boards tried before are set aside as repeats."""
    ask = seed_rounds.plan(run_id)
    if not ask["more_rounds"]:
        return f"no seed round is open (status {ask['status']}); run `ultron seeds plan --run {run_id}`"
    errors = [
        f"line {i}: {'; '.join(errs)}" for i, row in enumerate(rows, 1) if (errs := seed_rounds.validate_proposal(row))
    ]
    if errors:
        return "proposals do not match the schema of `ultron seeds plan`: " + " | ".join(errors[:10])
    run = runs.load_run(run_id)
    companies, boards, _names = seed_rounds.tried(run, seed_rounds.load_rounds(run_id))
    fresh: list[dict] = []
    repeats: list[str] = []
    for row in rows:
        keys = seed_rounds.board_keys(row)
        if seeds.company_key(row["company"]) in companies or any(key in boards for key in keys):
            repeats.append(row["company"])
            continue
        boards.update(keys)
        fresh.append(row)
    if not fresh:  # nothing to verify: the round is not consumed
        return "no new proposal: every line is empty or repeats a company or board tried before"
    return fresh[: ask["ask"]], repeats, {**ask, "over_ask": max(0, len(fresh) - ask["ask"])}


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
    round_info: dict | None = None
    repeats: list[str] = []
    if args.round:
        if is_feed_proposals:
            return fail("usage", "--round takes the proposals file named by `ultron seeds plan`")
        checked = _round_rows(args.run, rows)
        if isinstance(checked, str):
            return fail("invalid_proposals", checked)
        rows, repeats, round_info = checked
    limit = args.limit
    if limit is None and is_feed_proposals:
        limit = run["settings"]["new_companies_per_run"]
    if limit is not None:
        rows = rows[:limit]
    # R14: verification probes many slugs on few ATS hosts; only the global budget bounds it (like fetch).
    client = client_for_run(
        run, max_requests_per_host=run["settings"]["max_pages"], block_threshold=HOST_BLOCK_THRESHOLD,
    )
    timeout = run["settings"]["ats_api_timeout"]
    verified: list[seeds.VerifyResult] = []
    failed: list[dict] = []
    unconfirmed: list[seeds.AtsBoardSeed] = []
    page_hops = seeds.PageHops()
    results: list[seeds.VerifyResult] = []
    fitting_before = seed_rounds.fitting_seeds(run)[0] if round_info else 0
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("company") or "").strip():
            failed.append({"company": None, "reason": "invalid_proposal", "proposal": row})
            continue
        result = seeds.verify_proposal(
            _proposal(row), client=client, timeout=timeout, verify_only=True, page_hops=page_hops,
        )
        results.append(result)
        if result.status == "verified":
            verified.append(result)
        else:
            entry = {"company": result.proposal.company, "reason": result.reason, "proposal": row}
            if result.candidate:
                entry["candidate"] = result.candidate
            seed = seeds.stored_seed(result)
            if seed is not None:
                unconfirmed.append(seed)
                entry["stored_as"] = {"company": seed.company, "tags": seed.tags}
            failed.append(entry)
    # Companies named by feeds are not agent proposals: label them like the unattended mode does.
    if is_feed_proposals:
        for result in verified:
            if result.seed:
                result.seed = seeds.as_feed_company(result.seed)
    try:
        new_added = seeds.add_seeds(
            [*(r.seed for r in verified if r.seed), *unconfirmed], profile_slug=run["profile_slug"],
        )
    except seeds.SeedFileError as exc:
        return fail("seed_file_corrupt", str(exc))
    out = runs.run_dir(args.run)
    runs.write_jsonl(out / "seeds_verified.jsonl", (
        {"company": r.proposal.company, "postings": r.postings, "seed": r.seed.model_dump(mode="json")}
        for r in verified if r.seed
    ))
    runs.write_jsonl(out / "seeds_unresolved.jsonl", failed)
    runs.mark_stage(args.run, "seeds")
    extra: dict = {}
    if round_info is not None:
        record = seed_rounds.round_record(
            number=round_info["round"], received=len(rows) + len(repeats) + round_info["over_ask"], repeats=repeats,
            rows=rows,
            results=results, fitting_before=fitting_before, fitting_after=seed_rounds.fitting_seeds(run)[0],
        )
        seed_rounds.append_round(args.run, record)
        after = seed_rounds.plan(args.run)
        extra["seed_round"] = {
            "round": record["round"], "repeats_skipped": len(repeats), "over_ask_ignored": round_info["over_ask"],
            "fitting_seeds": after["fitting_seeds"],
            "seed_target": after["seed_target"], "seed_deficit": after["seed_deficit"], "status": after["status"],
            "more_rounds": after["more_rounds"], "next_ask": after["ask"],
        }
    emit({
        "verified": len(verified),
        "new": new_added,
        "unresolved": len(failed),
        # Guessed boards that answered but are not provably the company's: stored tagged, at low priority.
        "ownership_unknown": sum(1 for f in failed if f["reason"] == "ownership_unknown"),
        "wrong_tenant": sum(1 for f in failed if f["reason"] == "wrong_tenant"),
        "unresolved_sample": [{"company": f["company"], "reason": f["reason"]} for f in failed[:5]],
        **extra,
    })
    return 0


def register(subparsers) -> None:
    parser = subparsers.add_parser("seeds", help="seed inventory and company verification")
    sub = parser.add_subparsers(dest="seeds_command", required=True, parser_class=type(parser))
    lst = sub.add_parser("list", help="list inventory seeds, profile-linked first")
    lst.add_argument("--profile", default=None)
    lst.set_defaults(func=_list)
    plan = sub.add_parser("plan", help="seed deficit of a run and the next seed-generation round to propose")
    plan.add_argument("--run", required=True)
    plan.set_defaults(func=_plan)
    verify = sub.add_parser("verify", help="verify agent-proposed companies and store them")
    verify.add_argument("--run", required=True)
    verify.add_argument("--from", dest="source", required=True)
    verify.add_argument(
        "--limit",
        type=int,
        default=None,
        help="maximum proposals to process (defaults to new_companies_per_run for seed_proposals_from_feeds.jsonl)",
    )
    verify.add_argument(
        "--round", action="store_true",
        help="the file answers `seeds plan`: check it strictly against its schema and record the round",
    )
    verify.set_defaults(func=_verify)

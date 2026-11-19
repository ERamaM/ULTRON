"""``ultron run new`` and ``ultron runs list|show``."""

from __future__ import annotations

import argparse

from ultron import presets, runs, seeds
from ultron.output import emit, fail, fail_stage_order
from ultron.stages.unattended import run_unattended


def build_query_plan(profile: dict, max_queries: int) -> list[str]:
    if max_queries <= 0:
        return []
    templates = profile.get("query_templates") or {}
    titles = list(templates.get("core_titles") or (profile.get("criteria") or {}).get("target_titles") or [])
    supplementary = templates.get("titles")
    if isinstance(supplementary, list):
        titles.extend(title.strip() for title in supplementary if isinstance(title, str) and title.strip())
    regions = list(templates.get("regions") or [])
    combos = [f"{t} {r}" for t in titles for r in regions] if regions else [str(t) for t in titles]
    broad = list(dict.fromkeys(c.strip() for c in combos if str(c).strip()))
    scopes = list(dict.fromkeys(
        str(s).strip().removeprefix("site:").strip()
        for s in (templates.get("site_scopes") or [])
        if str(s).strip().removeprefix("site:").strip()
    ))
    if not scopes or not broad:
        return broad[:max_queries]
    scoped: list[str] = []
    for round_idx in range(len(broad)):
        for scope_idx, scope in enumerate(scopes):
            scoped.append(f"site:{scope} {broad[(scope_idx + round_idx) % len(broad)]}")
            if len(scoped) >= max_queries:
                break
        if len(scoped) >= max_queries:
            break
    # Interleave broad and site:-scoped ATS queries so even small presets include both.
    plan: list[str] = []
    seen: set[str] = set()
    for i in range(max(len(broad), len(scoped))):
        for pool in (broad, scoped):
            if i < len(pool) and pool[i] not in seen:
                seen.add(pool[i])
                plan.append(pool[i])
                if len(plan) >= max_queries:
                    return plan
    return plan


def _new_payload(run_id: str) -> dict:
    """Assemble the ``run new`` output; later tasks extend this one function."""
    data = runs.load_run(run_id)
    return {
        "run_id": run_id,
        "preset": data["preset"],
        "profile": data["profile_slug"],
        "query_plan": build_query_plan(data["profile_snapshot"], data["settings"]["max_search_queries"]),
        "profile_seeds": sum(1 for s in seeds.load_inventory(data["profile_slug"]) if data["profile_slug"] in s.profiles),
        "new_companies_target": data["settings"]["new_companies_per_run"],
    }


def _run_new(args: argparse.Namespace) -> int:
    try:
        presets.resolve(args.preset)
    except KeyError as exc:
        return fail("unknown_preset", str(exc.args[0]))
    try:
        run_id = runs.new_run(args.profile, args.preset)
    except FileNotFoundError as exc:
        return fail("profile_not_found", str(exc))
    runs.mark_stage(run_id, "new")
    emit(_new_payload(run_id))
    return 0


def _run_default(args: argparse.Namespace) -> int:
    """``ultron run`` without a sub-action: only ``--unattended --profile`` is valid."""
    if not (args.unattended and args.unattended_profile):
        return fail("usage", "use `ultron run new --profile P` or `ultron run --unattended --profile P`")
    try:
        presets.resolve(args.unattended_preset)
    except KeyError as exc:
        return fail("unknown_preset", str(exc.args[0]))
    try:
        payload = run_unattended(args.unattended_profile, args.unattended_preset)
    except FileNotFoundError as exc:
        return fail("profile_not_found", str(exc))
    except runs.StageOrderError as exc:
        return fail_stage_order(exc)
    except (OSError, ValueError) as exc:
        return fail("stage_failed", str(exc))
    emit(payload)
    return 0


def _runs_list(args: argparse.Namespace) -> int:
    emit({"runs": runs.list_runs()})
    return 0


def _runs_show(args: argparse.Namespace) -> int:
    try:
        emit({"run": runs.load_run(args.run_id)})
    except runs.RunNotFoundError as exc:
        return fail("run_not_found", str(exc))
    return 0


def register(subparsers) -> None:
    run = subparsers.add_parser("run", help="start a run (`run new`, or `run --unattended --profile P`)")
    run.add_argument("--unattended", action="store_true", help="chain discover, fetch, filter and finalize")
    run.add_argument("--profile", dest="unattended_profile")
    run.add_argument("--preset", dest="unattended_preset", default=presets.DEFAULT_PRESET)
    run.set_defaults(func=_run_default)
    run_sub = run.add_subparsers(dest="run_command", required=False, parser_class=type(run))
    new = run_sub.add_parser("new", help="create a run directory")
    new.add_argument("--profile", required=True)
    new.add_argument("--preset", default=presets.DEFAULT_PRESET)
    new.set_defaults(func=_run_new)
    runs_p = subparsers.add_parser("runs", help="inspect runs")
    runs_sub = runs_p.add_subparsers(dest="runs_command", required=True, parser_class=type(runs_p))
    runs_sub.add_parser("list", help="list runs").set_defaults(func=_runs_list)
    show = runs_sub.add_parser("show", help="show a run")
    show.add_argument("run_id")
    show.set_defaults(func=_runs_show)

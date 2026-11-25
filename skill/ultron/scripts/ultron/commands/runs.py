"""``ultron run new`` and ``ultron runs list|show``."""

from __future__ import annotations

import argparse

from ultron import seeds
from ultron.commands.output import emit, fail, fail_stage_order
from ultron.core import presets
from ultron.core.discovery.query_plan import build_query_plan
from ultron.seeds import rounds as seed_rounds
from ultron.stages.discover import COLD_START_SEEDS, eligible_seed_count
from ultron.stages.unattended import run_unattended
from ultron.storage import runs


def _new_payload(run_id: str) -> dict:
    """Assemble the ``run new`` output; later tasks extend this one function."""
    data = runs.load_run(run_id)
    eligible = eligible_seed_count(data["profile_slug"], data["profile_snapshot"] or {})
    coverage = seed_rounds.coverage(data)
    return {
        "run_id": run_id,
        "preset": data["preset"],
        "profile": data["profile_slug"],
        "query_plan": build_query_plan(data["profile_snapshot"], data["settings"]["max_search_queries"]),
        "profile_seeds": sum(1 for s in seeds.load_inventory(data["profile_slug"]) if data["profile_slug"] in s.profiles),
        "new_companies_target": data["settings"]["new_companies_per_run"],
        "eligible_seeds": eligible,
        "cold_start": eligible < COLD_START_SEEDS,
        **coverage,
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
    """Register ``ultron run`` and ``ultron runs`` subcommands."""
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

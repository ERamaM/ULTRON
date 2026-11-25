"""Compare a job search run with ULTRON against an agent that searches without it.

Usage (from the repository root)::

    uv run python tools/arm_comparison.py prepare --profile <slug> --session <name> [--top-n 25] [--force] \
        [--arms baseline,ultron=balanced/sweep,scout=balanced/scout]
    uv run python tools/arm_comparison.py ingest-ultron --session <name> --rep 1 --run <run_id> [--arm ultron]
    uv run python tools/arm_comparison.py ingest-agent --session <name> --rep 1 --from <file>.jsonl [--arm baseline]
    uv run python tools/arm_comparison.py record-cost --session <name> --arm ultron --rep 1 \
        --tokens 120000 --tool-calls 85 --seconds 900
    uv run python tools/arm_comparison.py discard --session <name> --arm baseline --rep 2
    uv run python tools/arm_comparison.py pool --session <name>
    uv run python tools/arm_comparison.py spotcheck --session <name> [--size 20]
    uv run python tools/arm_comparison.py score --session <name> [--output report.json]
    uv run python tools/arm_comparison.py trace --session <name> --arm scout [--output trace.json]

``ingest-*``, ``record-cost`` and ``discard`` take ``--phase 2`` for a second-day repetition.

The tool never launches agents and never uses the network: the agents are run by hand following the
protocol in ``docs/self-improvement.md``. Every file it reads or writes is a plain file name inside the
private reports folder (``safe_paths.REPORTS_DIR``), so the profile and the shortlists stay out of the
repository. Exit codes: ``0`` success, ``2`` invalid input or incomplete experiment.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arm_arms import (
    arm_spec,
    check_complete,
    first_phase,
    parse_arms,
    phase_key,
    second_phase,
    session_arms,
    split_key,
)
from arm_brief import brief_markdown, brief_profile, profile_hash
from arm_inputs import ComparisonError, clean_row, json_object, parse_jsonl, run_seconds
from arm_metrics import COST_FIELDS, compare_arms
from arm_pool import (
    LabelError,
    agreement,
    assign_ids,
    labels_yaml,
    parse_labels,
    pending_jobs,
    scoring_labels,
    spotcheck_yaml,
)
from arm_report import report_markdown
from arm_session import (
    SCHEMA_VERSION,
    WINDOWS_RESERVED,
    artifact,
    load_state,
    read_text,
    save_state,
    session_name,
    write_text,
)
from arm_trace import load_run_view, trace_arm
from replay_run import ReplayError, read_run_files
from safe_paths import UnsafePathError, check_report_names, remove_report

from ultron import __version__
from ultron.profiles.store import load_profile

DEFAULT_TOP_N = 25
DEFAULT_SPOTCHECK = 20
RELABEL_THRESHOLD = 0.15
ULTRON_ARM = "ultron"
BASELINE_ARM = "baseline"
SESSION_SUFFIXES = (".state", ".brief", ".labels", ".spotcheck", ".trace")


# --- prepare --------------------------------------------------------------------------------------


def prepare(
    profile_slug: str, session: str, top_n: int, *, force: bool = False, arms: str | None = None,
) -> dict[str, Any]:
    """Freeze the brief and start a new session state for ``profile_slug`` with the expected ``arms``."""
    session_name(session)
    expected = parse_arms(arms)
    if top_n < 1:
        raise ComparisonError("--top-n must be at least 1")
    if not force and read_text(artifact(session, "state.json"), missing_ok=True) is not None:
        raise ComparisonError(f"session {session} already exists: pick another name or pass --force")
    try:
        full = load_profile(profile_slug)
    except (OSError, yaml.YAMLError) as exc:
        raise ComparisonError(f"profile {profile_slug!r} cannot be loaded ({type(exc).__name__})") from exc
    if not isinstance(full, dict):
        raise ComparisonError(f"profile {profile_slug!r} is not a mapping")
    profile = brief_profile(full)
    state = {
        "schema_version": SCHEMA_VERSION,
        "session": session,
        "profile_slug": profile_slug,
        "profile_sha256": profile_hash(profile),
        "profile_full_sha256": profile_hash(full),
        "ultron_version": __version__,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "top_n": top_n,
        "expected_arms": expected,
        "arms": {},
        "job_ids": {},
    }
    check_report_names(artifact(session, "brief.md"), artifact(session, "state.json"))
    for kind in ("labels.yaml", "spotcheck.yaml"):  # labels of a replaced session no longer apply
        remove_report(artifact(session, kind))
    brief_path = write_text(artifact(session, "brief.md"), brief_markdown(profile, top_n))
    save_state(state)
    return {"session": session, "brief": str(brief_path), "top_n": top_n, "arms": expected}


# --- ingest ---------------------------------------------------------------------------------------


def _rep_key(rep: int) -> str:
    if not 1 <= rep <= 99:
        raise ComparisonError("--rep must be between 1 and 99")
    return str(rep)


def _check_source_unused(reps: dict[str, Any], rep: str, source: str) -> None:
    for other_rep, entry in reps.items():
        if entry.get("source") == source and other_rep != rep:
            raise ComparisonError(f"{source} is already repetition {other_rep}")


def store_rows(state: dict[str, Any], arm: str, rep: int, rows: list[Any], **extra: Any) -> dict[str, Any]:
    """Replace the top N rows of one repetition (cost kept only when re-ingesting the same source).

    Rows below the top N are never scored, so they are neither validated nor kept.
    """
    reps = state["arms"].setdefault(arm, {})
    _check_source_unused(reps, _rep_key(rep), extra["source"])
    cleaned = [clean_row(raw, index) for index, raw in enumerate(rows[: state["top_n"]], start=1)]
    previous = reps.get(_rep_key(rep), {})
    same_source = previous.get("source") == extra.get("source")
    reps[_rep_key(rep)] = {
        "rows": cleaned,
        "ingested_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cost": previous.get("cost", {}) if same_source else {},
        **extra,
    }
    save_state(state)
    return {"session": state["session"], "arm": arm, "rep": rep, "rows": len(cleaned), "submitted": len(rows)}


def _check_run(state: dict[str, Any], run_id: str, run: dict[str, Any], spec: dict[str, str]) -> None:
    if run.get("profile_slug") != state["profile_slug"]:
        raise ComparisonError(f"run {run_id} used profile {run.get('profile_slug')!r}, not {state['profile_slug']!r}")
    snapshot = run.get("profile_snapshot")
    if not isinstance(snapshot, dict) or profile_hash(snapshot) != state["profile_full_sha256"]:
        raise ComparisonError(f"run {run_id} used a different version of the profile than the brief")
    if run.get("preset") != spec["preset"]:
        raise ComparisonError(f"run {run_id} used preset {run.get('preset')!r}, not {spec['preset']!r}")
    if (run.get("mode") or "sweep") != spec["mode"]:
        raise ComparisonError(f"run {run_id} used mode {run.get('mode') or 'sweep'!r}, not {spec['mode']!r}")
    stages = run.get("stages_done")
    if not isinstance(stages, list) or "finalize" not in stages:
        raise ComparisonError(f"run {run_id} is not finalized")


def ingest_ultron(
    session: str, rep: int, run_id: str, arm: str = ULTRON_ARM, phase: int = 1,
) -> dict[str, Any]:
    """Import a finalized ULTRON run of the session's profile as an ULTRON arm."""
    state = load_state(session)
    spec = arm_spec(state, arm)
    if spec["kind"] != "ultron":
        raise ComparisonError(f"arm {arm} is an agent arm: import it with ingest-agent")
    try:
        texts = read_run_files(run_id)
    except ReplayError as exc:
        raise ComparisonError(str(exc)) from exc
    run = json_object(texts["run.json"], f"run {run_id}: run.json")
    _check_run(state, run_id, run, spec)
    if texts.get("results.json") is None:
        raise ComparisonError(f"run {run_id} has no results.json: finalize it first")
    results = json_object(texts["results.json"], f"run {run_id}: results.json")
    if not isinstance(results.get("rows"), list):
        raise ComparisonError(f"run {run_id}: results.json has no rows list")
    summary = results.get("summary") if isinstance(results.get("summary"), dict) else {}
    home = os.environ.get("ULTRON_HOME")
    return store_rows(state, phase_key(arm, phase), rep, results["rows"], source=run_id,
                      pipeline_seconds=run_seconds(summary), ultron_home=os.path.abspath(home) if home else None)


def ingest_agent(session: str, rep: int, source: str, arm: str = BASELINE_ARM, phase: int = 1) -> dict[str, Any]:
    """Import an agent's JSONL shortlist (a file name in the reports folder) as ``arm``."""
    state = load_state(session)
    if arm_spec(state, arm)["kind"] != "agent":
        raise ComparisonError(f"arm {arm} is an ULTRON arm: import it with ingest-ultron")
    return store_rows(state, phase_key(arm, phase), rep, parse_jsonl(read_text(source) or ""), source=source)


def _check_cost(field: str, value: float) -> float | int:
    if not math.isfinite(value) or value < 0:
        raise ComparisonError(f"--{field.replace('_', '-')} must be a finite number >= 0")
    if field != "seconds":
        if value != int(value):
            raise ComparisonError(f"--{field.replace('_', '-')} must be a whole number")
        return int(value)
    return value


def record_cost(session: str, arm: str, rep: int, phase: int = 1, **cost: float | None) -> dict[str, Any]:
    """Store the agent usage of an ingested repetition; given values replace earlier ones."""
    known = {field: _check_cost(field, value) for field, value in cost.items() if value is not None}
    if not known:
        raise ComparisonError("give at least one of --tokens, --tool-calls, --seconds")
    state = load_state(session)
    arm_spec(state, arm)
    entry = state["arms"].get(phase_key(arm, phase), {}).get(_rep_key(rep))
    if entry is None:
        raise ComparisonError(f"{arm} repetition {rep} is not ingested yet")
    entry.setdefault("cost", {}).update(known)
    save_state(state)
    return {"session": session, "arm": arm, "rep": rep, "cost": entry["cost"]}


def discard(session: str, arm: str, rep: int, phase: int = 1) -> dict[str, Any]:
    """Drop one repetition (for example a baseline agent that used ULTRON); run ``pool`` again after."""
    state = load_state(session)
    arm_spec(state, arm)
    key = phase_key(arm, phase)
    reps = state["arms"].get(key, {})
    if reps.pop(_rep_key(rep), None) is None:
        raise ComparisonError(f"{arm} repetition {rep} is not ingested")
    if not reps:
        state["arms"].pop(key, None)
    save_state(state)
    return {"session": session, "discarded": {"arm": arm, "rep": rep}}


# --- pool, spot check and score -------------------------------------------------------------------


def load_labels(session: str) -> dict[str, dict[str, Any]]:
    """Validated labels by job id (empty before the first ``pool``)."""
    text = read_text(artifact(session, "labels.yaml"), missing_ok=True)
    if text is None:
        return {}
    try:
        return parse_labels(text, session)
    except LabelError as exc:
        raise ComparisonError(f"{artifact(session, 'labels.yaml')}: {exc}") from exc


def pool(session: str) -> dict[str, Any]:
    """Write the blind labels file for the scored rows; keep labels already set."""
    state = load_state(session)
    existing = load_labels(session)
    pooled = assign_ids(state)
    for group in pooled["relabel"]:  # jobs merged or split since the labels were set: judge them again
        for job in group:
            existing.pop(job, None)
    path = write_text(artifact(session, "labels.yaml"), labels_yaml(session, pooled["jobs"], existing))
    save_state(state)
    return {"labels": str(path), "jobs": len(pooled["jobs"]),
            "pending": len(pending_jobs(existing, pooled["jobs"])), "relabel_ids": pooled["relabel"]}


def spotcheck(session: str, size: int) -> dict[str, Any]:
    """Write the user's blind sample, or, once it is filled in, report the agreement with the labels."""
    if size < 1:
        raise ComparisonError("--size must be at least 1")
    name = artifact(session, "spotcheck.yaml")
    text = read_text(name, missing_ok=True)
    if text is None:
        jobs = assign_ids(load_state(session))["jobs"]
        return {"spotcheck": str(write_text(name, spotcheck_yaml(session, jobs, size))),
                "jobs": min(size, len(jobs))}
    return current_spotcheck(session, load_labels(session), assign_ids(load_state(session))["jobs"]) or {}


def current_spotcheck(
    session: str, labels: dict[str, dict[str, Any]], jobs: dict[str, Any],
) -> dict[str, Any] | None:
    """Agreement between the user's filled-in sample and the current labels (``None`` without a sample).

    It is recomputed every time, so a later relabelling or a changed pool is always reflected.
    """
    name = artifact(session, "spotcheck.yaml")
    text = read_text(name, missing_ok=True)
    if text is None:
        return None
    try:
        spot = parse_labels(text, session, fields=("relevant",))
    except LabelError as exc:
        raise ComparisonError(f"{name}: {exc}") from exc
    result = agreement(labels, spot, RELABEL_THRESHOLD)
    return {**result, "size": len(spot), "stale": sorted(job for job in spot if job not in jobs)}


def _spotcheck_problems(spot: dict[str, Any] | None) -> list[str]:
    if not spot:
        return ["no spot check recorded: run spotcheck"]
    if spot["stale"]:
        return [(f"the spot check sample has {len(spot['stale'])} jobs no longer in the pool: "
                "delete the spot check file and run spotcheck again")]
    if spot["pending"]:
        return [f"the spot check has {spot['pending']} pending jobs"]
    if not spot["compared"]:
        return ["the spot check has no true/false judgments to compare: judge more jobs or draw a new sample"]
    if spot["relabel"]:
        return ["spot check disagreement is above the threshold: relabel, then run spotcheck again"]
    return []


def build_report(session: str, *, allow_partial: bool = False) -> dict[str, Any]:
    """Metrics for every arm and repetition, cut to the session's top N."""
    state = load_state(session)
    labels = load_labels(session)
    pooled = assign_ids(state)
    missing = [job for job in pooled["jobs"] if job not in labels]
    if state["arms"] and missing:
        raise ComparisonError(f"{len(missing)} scored jobs are not in the labels file: run pool again")
    if pooled["relabel"]:  # jobs merged or split since the last pool: their old labels no longer apply
        raise ComparisonError(f"{len(pooled['relabel'])} jobs merged or split since the last pool: "
                              "run pool again and judge them")
    spot = current_spotcheck(session, labels, pooled["jobs"])
    problems = check_complete(state) + _spotcheck_problems(spot)
    if problems and not allow_partial:
        raise ComparisonError("; ".join(problems) + " (pass --allow-partial to score anyway)")
    pending = pending_jobs(labels, pooled["jobs"])
    if pending and not allow_partial:
        raise ComparisonError(f"{len(pending)} jobs still have pending labels (pass --allow-partial to score anyway)")
    ids: dict[str, dict[str, list[str]]] = {}
    costs: dict[str, dict[str, dict[str, Any]]] = {}
    for arm, reps in state["arms"].items():
        for rep, entry in reps.items():
            count = min(state["top_n"], len(entry["rows"]))
            ids.setdefault(arm, {})[rep] = [pooled["rows"][(arm, rep, index)] for index in range(count)]
            costs.setdefault(arm, {})[rep] = {f: entry.get("cost", {}).get(f) for f in COST_FIELDS}
    first = first_phase(ids)
    scored = scoring_labels(labels)
    return {
        "schema_version": SCHEMA_VERSION,
        "session": session,
        "complete": not problems and not pending,
        "problems": problems,
        "pending_jobs": len(pending),
        "profile_sha256": state["profile_sha256"],
        "spotcheck": spot,
        "ultron_version": state["ultron_version"],
        "top_n": state["top_n"],
        "expected_arms": session_arms(state),
        "pipeline_seconds": {
            key: {rep: entry.get("pipeline_seconds") for rep, entry in reps.items()}
            for key, reps in state["arms"].items() if session_arms(state).get(split_key(key)[0], {}).get("kind") == "ultron"
        },
        **compare_arms(first, scored, {key: costs[key] for key in first}),
        "second_phase": second_phase(state, ids, scored),
    }


def trace(session: str, arm: str, output: str | None = None) -> dict[str, Any]:
    """Outcome of every relevant job the first phase of ULTRON ``arm`` missed, per repetition."""
    state = load_state(session)
    if arm_spec(state, arm)["kind"] != "ultron":
        raise ComparisonError(f"arm {arm} is an agent arm: only ULTRON arms have run folders to trace")
    reps = state["arms"].get(arm) or {}
    if not reps:
        raise ComparisonError(f"arm {arm} has no repetitions to trace")
    labels = scoring_labels(load_labels(session))
    pooled = assign_ids(state)
    views = {rep: load_run_view(entry["source"], entry.get("ultron_home")) for rep, entry in reps.items()}
    result = trace_arm(state, arm, pooled, labels, views)
    if not output:
        return result
    names = _output_names(output)
    return {"trace": str(write_text(names[0], json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))),
            "relevant_missed": result["relevant_missed"], "outcome_counts": result["outcome_counts"]}


def _output_names(output: str) -> tuple[str, str]:
    if not output.endswith(".json"):
        raise ComparisonError("--output must be a .json file name (a .md summary is written next to it)")
    stem = output[: -len(".json")]
    if stem.lower().endswith(SESSION_SUFFIXES) or stem.split(".")[0].lower() in WINDOWS_RESERVED:
        raise ComparisonError(f"{output}: that name is reserved for session files")
    try:
        check_report_names(output, f"{stem}.md")
    except UnsafePathError as exc:
        raise ComparisonError(str(exc)) from exc
    return output, f"{stem}.md"


def score(session: str, output: str | None, *, allow_partial: bool = False) -> dict[str, Any]:
    """Build the report; write it with a ``.md`` summary when ``output`` is given."""
    names = _output_names(output) if output else None
    report = build_report(session, allow_partial=allow_partial)
    if not names:
        return report
    path = write_text(names[0], json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    write_text(names[1], report_markdown(report))
    return {"report": str(path), "complete": report["complete"]}


# --- CLI ------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="freeze the brief and start a session")
    prep.add_argument("--profile", required=True)
    prep.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    prep.add_argument("--force", action="store_true", help="replace an existing session")
    prep.add_argument("--arms", help="expected arms, e.g. baseline,ultron=balanced/sweep,scout=balanced/scout")
    ult = sub.add_parser("ingest-ultron", help="import a finalized ULTRON run")
    ult.add_argument("--run", required=True)
    ult.add_argument("--arm", default=ULTRON_ARM)
    agent = sub.add_parser("ingest-agent", help="import an agent JSONL shortlist from the reports folder")
    agent.add_argument("--from", dest="source", required=True)
    agent.add_argument("--arm", default=BASELINE_ARM)
    cost = sub.add_parser("record-cost", help="store the agent usage of one repetition")
    cost.add_argument("--arm", required=True)
    for name in ("--tokens", "--tool-calls", "--seconds"):
        cost.add_argument(name, type=float)
    drop = sub.add_parser("discard", help="drop one repetition")
    drop.add_argument("--arm", required=True)
    sub.add_parser("pool", help="write the blind labels file")
    spot = sub.add_parser("spotcheck", help="write the user's blind sample, then report agreement")
    spot.add_argument("--size", type=int, default=DEFAULT_SPOTCHECK)
    out = sub.add_parser("score", help="compute the metrics")
    out.add_argument("--output")
    out.add_argument("--allow-partial", action="store_true", help="score an incomplete experiment")
    tr = sub.add_parser("trace", help="explain the relevant jobs an ULTRON arm missed")
    tr.add_argument("--arm", required=True)
    tr.add_argument("--output")
    for command in sub.choices.values():
        command.add_argument("--session", required=True)
    for command in (ult, agent, cost, drop):
        command.add_argument("--rep", type=int, required=True)
        command.add_argument("--phase", type=int, default=1, help="2 for a second-day repetition")
    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "prepare":
        return prepare(args.profile, args.session, args.top_n, force=args.force, arms=args.arms)
    if args.command == "ingest-ultron":
        return ingest_ultron(args.session, args.rep, args.run, args.arm, args.phase)
    if args.command == "ingest-agent":
        return ingest_agent(args.session, args.rep, args.source, args.arm, args.phase)
    if args.command == "record-cost":
        return record_cost(args.session, args.arm, args.rep, args.phase, tokens=args.tokens,
                           tool_calls=args.tool_calls, seconds=args.seconds)
    if args.command == "discard":
        return discard(args.session, args.arm, args.rep, args.phase)
    if args.command == "trace":
        return trace(args.session, args.arm, args.output)
    if args.command == "pool":
        return pool(args.session)
    if args.command == "spotcheck":
        return spotcheck(args.session, args.size)
    return score(args.session, args.output, allow_partial=args.allow_partial)


def main(argv: list[str] | None = None) -> int:
    """Run the arm comparison CLI."""
    args = _parser().parse_args(argv)
    try:
        result = _dispatch(args)
    except (ComparisonError, UnsafePathError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"file system error ({type(exc).__name__})"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

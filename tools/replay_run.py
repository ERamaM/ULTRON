"""Replay ``filter`` + ``finalize`` over a past run's postings and diff the keep/discard decisions.

Usage (from the repository root)::

    uv run python tools/replay_run.py replay --run RUN_ID [--output arm.json] [--no-evaluations]
    uv run python tools/replay_run.py replay --run-dir PATH [--output arm.json]
    uv run python tools/replay_run.py diff arm-a.json arm-b.json

``replay`` reads ``run.json``, ``postings.jsonl`` and (unless ``--no-evaluations``) the recorded agent
``evaluations.jsonl`` of one run, re-runs the current code's filter and finalize in a throwaway
``ULTRON_HOME`` and reports, per identity, the recorded decision next to the replayed one. The source
run folder and the user's ``ULTRON_HOME`` are only read, never written.

``diff`` compares two replay reports of the same run (for example made on two branches) so a code
change shows up as a paired keep/discard delta over identical postings. Exit code ``0`` on success,
``2`` on unreadable input.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_harness import final_status, read_final_outcomes, replay
from safe_paths import UnsafePathError, contained_path

from ultron import runs

REPORT_SCHEMA_VERSION = 1


class ReplayError(ValueError):
    """The run or a report cannot be read."""


RUN_FILES = ("run.json", "postings.jsonl")
# Every file the replay reads from a run folder; each must resolve inside that folder.
READ_FILES = (*RUN_FILES, "evaluations.jsonl", "discarded.jsonl", "kept.jsonl", "ambiguous.jsonl",
              "finalize_discards.jsonl", "results.json")


def _ultron_home() -> str:
    env = os.environ.get("ULTRON_HOME")
    return os.path.realpath(Path(env).expanduser() if env else Path.home() / ".ultron")


def _check_run_folder(path: Path) -> Path:
    """A run folder: named like a run id, holding ``run.json`` and ``postings.jsonl`` as regular files.

    No file the replay reads may resolve outside the folder (a symlink to elsewhere is rejected).
    """
    if not path.is_dir() or not runs.RUN_ID_RE.fullmatch(path.name):
        raise ReplayError(f"{path.name}: not a run folder (run folders are named YYYYMMDD-HHMMSS-xxxxxx)")
    for name in READ_FILES:
        try:
            contained_path(path / name, (str(path),))
        except UnsafePathError as exc:
            raise ReplayError(f"{path.name}: {name} points outside the run folder") from exc
    if not all((path / name).is_file() for name in RUN_FILES):
        raise ReplayError(f"{path.name}: run.json and postings.jsonl are required")
    return path


def resolve_run_dir(run_id: str | None, run_path: Path | None) -> Path:
    """The run folder, resolved and validated without creating anything under ``ULTRON_HOME``.

    ``--run`` must be a run id and resolve inside ``ULTRON_HOME/runs``. ``--run-dir`` is an explicit
    run folder (for example a snapshot copy) and must pass the same folder checks.
    """
    try:
        if run_path is not None:
            return _check_run_folder(Path(os.path.realpath(run_path)))
        if not run_id or not runs.RUN_ID_RE.fullmatch(run_id):
            raise ReplayError(f"invalid run id: {run_id!r} (run ids look like YYYYMMDD-HHMMSS-xxxxxx)")
        runs_root = os.path.join(_ultron_home(), "runs")
        return _check_run_folder(contained_path(os.path.join(runs_root, run_id), (runs_root,)))
    except UnsafePathError as exc:
        raise ReplayError(str(exc)) from exc


def _recorded(path: Path) -> dict[str, str]:
    """Recorded decision per identity: ``keep``/``unsure``/``discard`` after finalize, else the filter route.

    A ranked identity whose exported row cannot be joined back (export redaction may rewrite its URL)
    is ``ranked``: it was in the shortlist, with an unknown verdict.
    """
    recorded: dict[str, str] = {}
    for row in runs.read_jsonl(path / "discarded.jsonl"):
        recorded[row["identity_key"]] = "discard"
    results_path = path / "results.json"
    if not results_path.is_file():
        entries = [*runs.read_jsonl(path / "kept.jsonl"), *runs.read_jsonl(path / "ambiguous.jsonl")]
        recorded.update({entry["identity_key"]: "pending" for entry in entries})
        return recorded
    rows = json.loads(results_path.read_text(encoding="utf-8")).get("rows") or []
    for key, outcome in read_final_outcomes(path, rows).items():
        if outcome["status"] == "discarded":
            recorded[key] = "discard"
        elif outcome["verdict"] is None:
            recorded[key] = "ranked"
        else:
            recorded[key] = "unsure" if outcome["verdict"] == "unsure" else "keep"
    return recorded


def _changed(recorded: str, replayed: str) -> bool:
    if recorded in ("pending", "missing"):
        return False
    if recorded == "ranked":
        return replayed == "discard"
    return recorded != replayed


def replay_run(path: Path, *, use_evaluations: bool = True) -> dict[str, Any]:
    path = _check_run_folder(Path(os.path.realpath(path)))
    try:
        run = json.loads((path / "run.json").read_text(encoding="utf-8"))
        postings = runs.read_jsonl(path / "postings.jsonl")
        evaluations = runs.read_jsonl(path / "evaluations.jsonl") if use_evaluations else []
    except (OSError, ValueError) as exc:
        raise ReplayError(f"{path.name}: unreadable run files: {exc}") from exc
    replayed = replay(
        postings,
        profile=run.get("profile_snapshot") or {},
        settings=run.get("settings") or {},
        evaluations=evaluations,
        profile_slug=str(run.get("profile_slug") or "replay"),
        preset=str(run.get("preset") or "replay"),
    )
    recorded = _recorded(path)
    identities = {}
    for key, outcome in replayed["identities"].items():
        filtered = outcome.get("filter") or {}
        identities[key] = {
            "recorded": recorded.get(key, "missing"),
            "replayed": final_status(outcome),
            "filter_route": filtered.get("route"),
            "stage": filtered.get("stage") or (outcome.get("final") or {}).get("stage"),
            "reason": filtered.get("reason") or (outcome.get("final") or {}).get("reason"),
            "rank": (outcome.get("final") or {}).get("rank"),
        }
    changed = sorted(k for k, v in identities.items() if _changed(v["recorded"], v["replayed"]))
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": str(run.get("run_id") or path.name),
        "used_evaluations": bool(evaluations),
        "postings": len(postings),
        "filter_summary": replayed["filter_summary"],
        "finalize_summary": replayed["finalize_summary"],
        "changed_vs_recorded": changed,
        "identities": identities,
    }


def diff_reports(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Paired keep/unsure/discard changes between two replay reports of the same postings."""
    for label, report in (("first", first), ("second", second)):
        if not isinstance(report, dict) or report.get("schema_version") != REPORT_SCHEMA_VERSION:
            raise ReplayError(f"{label} report must use schema_version {REPORT_SCHEMA_VERSION}")
    a, b = first.get("identities") or {}, second.get("identities") or {}
    changes = []
    for key in sorted(set(a) | set(b)):
        before = (a.get(key) or {}).get("replayed", "missing")
        after = (b.get(key) or {}).get("replayed", "missing")
        if before != after:
            changes.append({"identity_key": key, "before": before, "after": after,
                            "reason_after": (b.get(key) or {}).get("reason")})
    transitions: dict[str, int] = {}
    for change in changes:
        name = f"{change['before']}->{change['after']}"
        transitions[name] = transitions.get(name, 0) + 1
    return {
        "same_run": first.get("run_id") == second.get("run_id"),
        "postings": [first.get("postings"), second.get("postings")],
        "changed": len(changes),
        "transitions": dict(sorted(transitions.items())),
        "changes": changes,
    }


def _read(raw: Path) -> dict[str, Any]:
    try:
        path = contained_path(raw)
        return json.loads(path.read_text(encoding="utf-8"))
    except UnsafePathError as exc:
        raise ReplayError(str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise ReplayError(f"{Path(raw).name}: unreadable report: {exc}") from exc


def _write(raw: Path, text: str) -> None:
    try:
        path = contained_path(raw)
        path.write_text(text, encoding="utf-8", newline="\n")
    except UnsafePathError as exc:
        raise ReplayError(str(exc)) from exc
    except OSError as exc:
        raise ReplayError(f"{Path(raw).name}: cannot write the report: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay filter + finalize over a past run and diff decisions.")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("replay", help="replay one run with the current code")
    source = rep.add_mutually_exclusive_group(required=True)
    source.add_argument("--run", help="run id under ULTRON_HOME/runs")
    source.add_argument("--run-dir", type=Path, help="path to a run folder (for example a snapshot copy)")
    rep.add_argument("--output", type=Path, help="write the JSON report here (repository or temp folder)")
    rep.add_argument("--no-evaluations", action="store_true", help="ignore recorded agent evaluations")
    dif = sub.add_parser("diff", help="paired diff of two replay reports")
    dif.add_argument("first", type=Path)
    dif.add_argument("second", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "replay":
            path = resolve_run_dir(args.run, args.run_dir)
            payload = replay_run(path, use_evaluations=not args.no_evaluations)
        else:
            payload = diff_reports(_read(args.first), _read(args.second))
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if getattr(args, "output", None):
            _write(args.output, rendered)
        else:
            sys.stdout.write(rendered)
    except ReplayError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Replay ``filter`` + ``finalize`` over a past run's postings and diff the keep/discard decisions.

Usage (from the repository root)::

    uv run python tools/replay_run.py replay --run RUN_ID [--output arm-a.json] [--no-evaluations]
    uv run python tools/replay_run.py diff arm-a.json arm-b.json

``replay`` reads ``run.json``, ``postings.jsonl`` and (unless ``--no-evaluations``) the recorded agent
``evaluations.jsonl`` of one run under ``ULTRON_HOME/runs``, re-runs the current code's filter and
finalize in a throwaway ``ULTRON_HOME`` and reports, per identity, the recorded decision next to the
replayed one. The source run and the user's ``ULTRON_HOME`` are only read, never written. To replay a
run from a snapshot, point ``ULTRON_HOME`` at the snapshot.

``diff`` compares two replay reports of the same run (for example made on two branches) so a code
change shows up as a paired keep/discard delta over identical postings.

Reports are plain file names inside ``<system temp>/ultron-eval`` (see ``safe_paths.py``). Exit code
``0`` on success, ``2`` on an invalid run id, report name or unreadable input.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_harness import assign_ranks, final_status, replay
from safe_paths import UnsafePathError, inside, read_report, write_report

from ultron import runs

REPORT_SCHEMA_VERSION = 1
REQUIRED_FILES = ("run.json", "postings.jsonl")
OPTIONAL_FILES = ("evaluations.jsonl", "discarded.jsonl", "kept.jsonl", "ambiguous.jsonl",
                  "finalize_discards.jsonl", "results.json")


class ReplayError(ValueError):
    """The run or a report cannot be read."""


def read_run_files(run_id: str) -> dict[str, str | None]:
    """Text of every file the replay needs from ``ULTRON_HOME/runs/<run_id>`` (``None`` when absent).

    The run id must fully match the run-id pattern, the folder is rebuilt from ``ULTRON_HOME`` here,
    and only the fixed file names above are read. A file that resolves outside the run folder (a
    symlink to elsewhere) is rejected. Nothing is created under ``ULTRON_HOME``.
    """
    if not isinstance(run_id, str) or not runs.RUN_ID_RE.fullmatch(run_id):
        raise ReplayError(f"invalid run id: {run_id!r} (run ids look like YYYYMMDD-HHMMSS-xxxxxx)")
    env = os.environ.get("ULTRON_HOME")
    home = os.path.expanduser(env) if env else os.path.join(os.path.expanduser("~"), ".ultron")
    folder = os.path.join(home, "runs", run_id)
    if not os.path.isdir(folder):
        raise ReplayError(f"run not found: {run_id}")
    texts: dict[str, str | None] = {}
    for name in (*REQUIRED_FILES, *OPTIONAL_FILES):
        path = os.path.join(folder, name)
        if not os.path.lexists(path):
            texts[name] = None
            continue
        if not inside(path, folder) or not os.path.isfile(path):
            raise ReplayError(f"{run_id}: {name} is not a file inside the run folder")
        try:
            with open(path, encoding="utf-8") as handle:
                texts[name] = handle.read()
        except (OSError, ValueError) as exc:
            raise ReplayError(f"{run_id}: {name} is unreadable: {exc}") from exc
    missing = [name for name in REQUIRED_FILES if texts[name] is None]
    if missing:
        raise ReplayError(f"{run_id}: run.json and postings.jsonl are required")
    return texts


def _jsonl(text: str | None) -> list[dict[str, Any]]:
    return [json.loads(line) for line in runs.jsonl_lines(text or "") if line.strip()]


def _recorded(texts: dict[str, str | None]) -> dict[str, str]:
    """Recorded decision per identity: ``keep``/``unsure``/``discard`` after finalize, else the filter route.

    A ranked identity whose exported row cannot be joined back (export redaction may rewrite its URL)
    is ``ranked``: it was in the shortlist, with an unknown verdict.
    """
    recorded = {row["identity_key"]: "discard" for row in _jsonl(texts["discarded.jsonl"])}
    entries = [*_jsonl(texts["kept.jsonl"]), *_jsonl(texts["ambiguous.jsonl"])]
    if texts["results.json"] is None:
        recorded.update({entry["identity_key"]: "pending" for entry in entries})
        return recorded
    rows = json.loads(texts["results.json"]).get("rows") or []
    discarded = {row["identity_key"]: row for row in _jsonl(texts["finalize_discards.jsonl"])}
    for key, outcome in assign_ranks(entries, discarded, rows).items():
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


def replay_run(run_id: str, *, use_evaluations: bool = True) -> dict[str, Any]:
    texts = read_run_files(run_id)
    try:
        run = json.loads(texts["run.json"] or "")
        postings = _jsonl(texts["postings.jsonl"])
        evaluations = _jsonl(texts["evaluations.jsonl"]) if use_evaluations else []
        recorded = _recorded(texts)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ReplayError(f"{run_id}: malformed run files: {exc}") from exc
    replayed = replay(
        postings,
        profile=run.get("profile_snapshot") or {},
        settings=run.get("settings") or {},
        evaluations=evaluations,
        profile_slug=str(run.get("profile_slug") or "replay"),
        preset=str(run.get("preset") or "replay"),
    )
    identities = {}
    for key, outcome in replayed["identities"].items():
        filtered = outcome.get("filter") or {}
        final = outcome.get("final") or {}
        identities[key] = {
            "recorded": recorded.get(key, "missing"),
            "replayed": final_status(outcome),
            "filter_route": filtered.get("route"),
            "stage": filtered.get("stage") or final.get("stage"),
            "reason": filtered.get("reason") or final.get("reason"),
            "rank": final.get("rank"),
        }
    changed = sorted(k for k, v in identities.items() if _changed(v["recorded"], v["replayed"]))
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
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


def _read(name: str) -> dict[str, Any]:
    try:
        return json.loads(read_report(name))
    except UnsafePathError as exc:
        raise ReplayError(str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise ReplayError(f"{name}: unreadable report: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay filter + finalize over a past run and diff decisions.")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("replay", help="replay one run with the current code")
    rep.add_argument("--run", required=True, help="run id under ULTRON_HOME/runs")
    rep.add_argument("--output", help="report file name, written into the reports folder")
    rep.add_argument("--no-evaluations", action="store_true", help="ignore recorded agent evaluations")
    dif = sub.add_parser("diff", help="paired diff of two replay reports (file names in the reports folder)")
    dif.add_argument("first")
    dif.add_argument("second")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "replay":
            payload = replay_run(args.run, use_evaluations=not args.no_evaluations)
        else:
            payload = diff_reports(_read(args.first), _read(args.second))
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if getattr(args, "output", None):
            path = write_report(args.output, rendered)
            print(json.dumps({"report": str(path)}))
        else:
            sys.stdout.write(rendered)
    except (ReplayError, UnsafePathError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

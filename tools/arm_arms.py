"""Arms of an arm comparison session: their kinds, the ULTRON preset and mode each expects, and phases.

An arm is either an agent arm (its shortlist is a JSONL file the agent wrote) or an ULTRON arm (a finalized
run). ``prepare --arms`` records the arms a session expects; completeness and ingestion checks use that list.
A second phase of an arm (``--phase 2``) is stored under ``<arm>@2`` and scored apart from the first phase.
"""

from __future__ import annotations

import re
from statistics import mean
from typing import Any

from arm_inputs import ComparisonError
from arm_metrics import COST_FIELDS, ratio, relevant_ids

ARM_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
PRESET_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
MODES = ("sweep", "scout")
DEFAULT_ARMS = "baseline,ultron=balanced/sweep"
PHASE_SEPARATOR = "@"
MAX_PHASE = 2
MIN_REPETITIONS = 3


def arm_name(raw: str) -> str:
    """``raw`` when it is a valid arm name; ``ComparisonError`` otherwise."""
    if not isinstance(raw, str) or not ARM_RE.fullmatch(raw):
        raise ComparisonError(f"{raw!r}: arm names are lowercase letters, digits, '-' and '_'")
    return raw


def _ultron_spec(name: str, value: str) -> dict[str, str]:
    preset, _, mode = value.partition("/")
    mode = mode or "sweep"
    if not PRESET_RE.fullmatch(preset) or mode not in MODES:
        raise ComparisonError(f"{name}={value}: use <arm>=<preset>/<mode> with mode one of {', '.join(MODES)}")
    return {"kind": "ultron", "preset": preset, "mode": mode}


def parse_arms(text: str | None) -> dict[str, dict[str, str]]:
    """The arms of ``text``: ``baseline`` is an agent arm, ``scout=balanced/scout`` an ULTRON arm.

    A bare ``ultron`` is the classic ULTRON arm, ``ultron=balanced/sweep``.
    """
    arms: dict[str, dict[str, str]] = {}
    for item in str(text or DEFAULT_ARMS).split(","):
        name, sep, value = item.strip().partition("=")
        arm_name(name)
        if name in arms:
            raise ComparisonError(f"arm {name} is listed twice")
        if not sep and name == "ultron":  # the classic ULTRON arm needs no spec
            sep, value = "=", "balanced/sweep"
        arms[name] = _ultron_spec(name, value) if sep else {"kind": "agent"}
    if len(arms) < 2:
        raise ComparisonError("a comparison needs at least two arms")
    return arms


def session_arms(state: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The arms a session expects; sessions created before ``--arms`` expect the default two."""
    return state.get("expected_arms") or parse_arms(DEFAULT_ARMS)


def arm_spec(state: dict[str, Any], arm: str) -> dict[str, str]:
    """The spec of ``arm`` in this session; ``ComparisonError`` for an arm the session does not expect."""
    spec = session_arms(state).get(arm_name(arm))
    if spec is None:
        raise ComparisonError(f"arm {arm} is not one of this session's arms: {', '.join(session_arms(state))}")
    return spec


def phase_key(arm: str, phase: int) -> str:
    """The state key of ``arm`` in ``phase`` (the arm name itself for phase 1)."""
    if not 1 <= phase <= MAX_PHASE:
        raise ComparisonError(f"--phase must be between 1 and {MAX_PHASE}")
    return arm if phase == 1 else f"{arm}{PHASE_SEPARATOR}{phase}"


def split_key(key: str) -> tuple[str, int]:
    """``(arm, phase)`` of a state key."""
    arm, sep, phase = key.partition(PHASE_SEPARATOR)
    return arm, int(phase) if sep else 1


def first_phase(state_arms: dict[str, Any]) -> dict[str, Any]:
    """The first-phase entries of ``state_arms``."""
    return {key: reps for key, reps in state_arms.items() if split_key(key)[1] == 1}


def check_complete(state: dict[str, Any]) -> list[str]:
    """Reasons the first phase is not a complete, balanced comparison (empty when it is)."""
    expected = session_arms(state)
    arms = first_phase(state["arms"])
    problems = [f"arm {arm} has no repetitions" for arm in expected if not arms.get(arm)]
    problems += [f"unexpected arm {arm}" for arm in arms if arm not in expected]
    rep_sets = {arm: sorted(reps, key=int) for arm, reps in arms.items()}
    if len({tuple(keys) for keys in rep_sets.values()}) > 1:
        problems.append(f"arms have different repetitions: {rep_sets}")
    if any(len(keys) < MIN_REPETITIONS for keys in rep_sets.values()):
        problems.append(f"every arm needs at least {MIN_REPETITIONS} repetitions")
    problems += [f"{arm} repetition {rep} has no {field} recorded"
                 for arm, reps in sorted(arms.items()) for rep, entry in sorted(reps.items())
                 for field in COST_FIELDS if entry.get("cost", {}).get(field) is None]
    return problems


def _mean_cost(reps: dict[str, Any], field: str) -> float | None:
    values = [entry.get("cost", {}).get(field) for entry in reps.values()]
    known = [value for value in values if value is not None]
    return round(mean(known), 2) if known else None


def second_phase(
    state: dict[str, Any], ids: dict[str, dict[str, list[str]]], labels: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Second-phase results next to the first phase of the same arm.

    ``ids`` maps every state key to rep -> ranked pooled ids. A relevant job is *new* in the second phase when
    no first-phase repetition of any arm found it. Each second-phase arm reports the new relevant jobs it
    found, the new relevant jobs another second-phase arm found and it missed, and its cost per run.
    """
    first_union = {job for key, reps in ids.items() if split_key(key)[1] == 1 for rows in reps.values() for job in rows}
    later = {key: reps for key, reps in ids.items() if split_key(key)[1] > 1}
    new_relevant = relevant_ids(
        {job for reps in later.values() for rows in reps.values() for job in rows} - first_union, labels,
    )
    out: dict[str, Any] = {}
    for key, reps in sorted(later.items()):
        arm, _phase = split_key(key)
        found = relevant_ids({job for rows in reps.values() for job in rows}, labels) & new_relevant
        out[key] = {
            "repetitions": len(reps),
            "new_relevant_found": len(found),
            "new_relevant_missed": len(new_relevant - found),
            "new_relevant_recall": ratio(len(found), len(new_relevant)),
            **{f"{field}_per_run": _mean_cost(state["arms"][key], field) for field in COST_FIELDS},
            **{f"first_phase_{field}_per_run": _mean_cost(state["arms"].get(arm, {}), field)
               for field in COST_FIELDS},
        }
    return {"new_relevant_in_pool": len(new_relevant), "arms": out}

"""Seed-generation rounds: how many fitting seeds a run lacks, what the agent should propose next, and the state.

The CLI cannot generate companies itself. When a profile has fewer fitting seeds than its preset's
``seed_target`` (no bundled pack fits it, or too few boards do), ``ultron seeds plan`` prints a structured
request: the deficit, how many proposals to write (over-asked by the observed verification yield), a JSON
Schema for each proposal line and the companies already tried. The agent writes that file and
``ultron seeds verify --round`` checks it strictly against the schema, verifies it and records the round in
``seed_rounds.jsonl`` (CLI-owned, append-only). Rounds stop when the target is met, after ``seed_rounds``
rounds, after a round that added no fitting seed, or after a round in which an ATS host blocked us.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from math import ceil
from typing import get_args
from urllib.parse import urlsplit, urlunsplit

from ultron import presets, runs, seeds
from ultron.core.ats.identity import seed_identity
from ultron.core.ats.vendors import AtsVendorName
from ultron.core.rules.geography import geo_scope
from ultron.core.sectors import profile_sector_values
from ultron.stages.discover import (
    EXCLUDED_RANK,
    FEED_SOURCE_TYPES,
    _profile_region_text,
    _rank,
    _seed_request_host,
    _select_seeds,
    load_seed_yields,
    seed_key,
)

ROUNDS_FILE = "seed_rounds.jsonl"
PROPOSALS_FILE = "agent_seed_round_{n}.jsonl"
# Verification yield (fitting seeds gained per proposal checked) assumed before a run has measured one, and
# its bounds: the floor keeps the ask finite after a poor round, the ceiling never asks for fewer than needed.
PRIOR_YIELD = 0.5
MIN_YIELD = 0.25
MIN_MEASURED = 5
# At most this many tried company names are printed by ``seeds plan`` (repeats are refused at verify anyway).
MAX_EXCLUDE_SHOWN = 300
# Verification outcomes that never checked the board: they do not count in the yield or as tried.
_UNCHECKED = frozenset({"budget_exhausted", "host_blocked", "host_budget_exhausted", "page_budget_exhausted"})

PROPOSAL_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ULTRON seed proposal (one JSON object per line)",
    "type": "object",
    "additionalProperties": False,
    "required": ["company"],
    "properties": {
        "company": {"type": "string", "minLength": 1, "maxLength": 200},
        "domain": {"type": "string", "maxLength": 253},
        "ats": {"type": "string", "enum": sorted(get_args(AtsVendorName))},
        "board": {"type": "string", "minLength": 1, "maxLength": 300},
        "careers_url": {"type": "string", "pattern": "^https?://", "maxLength": 2000},
        "note": {"type": "string", "maxLength": 300},
    },
    # A round must name the board: guessing slugs from a name costs up to ~100 requests per company.
    "anyOf": [{"required": ["ats", "board"]}, {"required": ["careers_url"]}],
}


def validate_proposal(row: object, schema: dict = PROPOSAL_SCHEMA) -> list[str]:
    """Errors of one proposal against ``schema`` (the subset of JSON Schema that ``PROPOSAL_SCHEMA`` uses)."""
    if not isinstance(row, dict):
        return ["not a JSON object"]
    errors: list[str] = []
    props: dict = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        errors += [f"unknown field {key!r}" for key in row if key not in props]
    errors += [f"missing {key!r}" for key in schema.get("required", []) if key not in row]
    for key, rule in props.items():
        if key not in row:
            continue
        value = row[key]
        if rule.get("type") == "string" and not isinstance(value, str):
            errors.append(f"{key!r} must be a string")
            continue
        if len(value.strip()) < rule.get("minLength", 0):
            errors.append(f"{key!r} is empty")
        if len(value) > rule.get("maxLength", len(value)):
            errors.append(f"{key!r} is longer than {rule['maxLength']} characters")
        if "enum" in rule and value not in rule["enum"]:
            errors.append(f"{key!r} must be one of the supported ATS names")
        if "pattern" in rule and not re.search(rule["pattern"], value):
            errors.append(f"{key!r} must match {rule['pattern']}")
    alternatives = schema.get("anyOf", [])
    if alternatives and not any(all(key in row for key in alt.get("required", [])) for alt in alternatives):
        errors.append("needs 'ats' + 'board' or 'careers_url'")
    return errors


def run_settings(run: dict) -> dict:
    """The run's settings, with the preset's value for keys added after the run was created."""
    try:
        defaults = presets.resolve(run.get("preset") or presets.DEFAULT_PRESET)
    except KeyError:
        defaults = presets.resolve(presets.DEFAULT_PRESET)
    return {**defaults, **(run.get("settings") or {})}


def load_rounds(run_id: str) -> list[dict]:
    path = runs.run_dir(run_id) / ROUNDS_FILE
    return runs.read_jsonl(path) if path.exists() else []


def append_round(run_id: str, record: dict) -> None:
    runs.append_jsonl(runs.run_dir(run_id) / ROUNDS_FILE, [record])


def _counts(seed) -> bool:
    """A seed counts toward the target when it was chosen for the profile (a fitting pack seed, or one the agent or
    user added), is confirmed, and its board had open postings when verified. Feed seeds are opportunistic: a
    company-list feed links its boards to every profile, so counting them would hide a deficit."""
    if seeds.is_unconfirmed(seed) or seed.source_type in FEED_SOURCE_TYPES:
        return False
    return (seed.verification or {}).get("postings") != 0


def fitting_seeds(run: dict) -> tuple[int, list[str]]:
    """Seeds this run would query that count toward the target, measured like ``discover ats`` selects them
    (the preset's seed and per-host caps), and the request hosts already at their per-host cap."""
    profile = run.get("profile_snapshot") or {}
    settings = run_settings(run)
    selected, _groups, _stats = _select_seeds(
        run["profile_slug"], (profile.get("criteria") or {}).get("sectors"), settings["max_seeds_per_run"],
        settings["max_requests_per_host"], geo=geo_scope(profile), yields=load_seed_yields(run["profile_slug"]),
        profile=profile,
    )
    per_host: dict[str, int] = {}
    for seed, _group in selected:
        host = _seed_request_host(seed)
        per_host[host] = per_host.get(host, 0) + 1
    full = sorted(h for h, n in per_host.items() if n >= max(1, settings["max_requests_per_host"]))
    return sum(1 for seed, _group in selected if _counts(seed)), full


def coverage(run: dict) -> dict:
    """``seed_target``, ``fitting_seeds`` and ``seed_deficit`` for ``run new`` and unattended runs."""
    target = run_settings(run)["seed_target"]
    fitting = fitting_seeds(run)[0]
    return {"seed_target": target, "fitting_seeds": fitting, "seed_deficit": max(0, target - fitting)}


def observed_yield(rounds: list[dict]) -> float:
    """Fitting seeds gained per proposal actually checked in this run's rounds, clamped; the prior when few."""
    checked = sum(int(r.get("checked", 0)) for r in rounds)
    gained = sum(max(0, int(r.get("fitting_after", 0)) - int(r.get("fitting_before", 0))) for r in rounds)
    if checked < MIN_MEASURED:
        return PRIOR_YIELD
    return min(1.0, max(MIN_YIELD, gained / checked))


def board_keys(row: dict) -> list[str]:
    """The keys a proposal is tried under: ``ats:board`` (``seed_identity``) and ``url:<careers_url>`` with only
    the scheme and host folded and no trailing slash (paths can be case-sensitive: Workday ``/External``)."""
    keys = []
    if row.get("ats") and row.get("board"):
        keys.append(":".join(seed_identity(str(row["ats"]), str(row["board"]))))
    if row.get("careers_url"):
        parts = urlsplit(str(row["careers_url"]).strip())
        url = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))
        keys.append("url:" + url.rstrip("/"))
    return keys


def tried(run: dict, rounds: list[dict]) -> tuple[set[str], set[str], list[str]]:
    """What a round proposal may not repeat, and the company names to show the agent.

    A company is excluded wholesale only when one of its seeds counts toward the target (``_counts``). A board
    is excluded when the profile already holds it as a confirmed seed (its own or a fitting pack seed), or an
    earlier round checked it; a guessed board of unknown ownership stays open, so an explicit proposal can
    promote it. A company that failed in an earlier round may come back with a different board."""
    profile = run.get("profile_snapshot") or {}
    sectors = profile_sector_values((profile.get("criteria") or {}).get("sectors"))
    geo, region_text = geo_scope(profile), _profile_region_text(profile)
    companies: set[str] = set()
    boards: set[str] = set()
    names: list[str] = []
    shown: set[str] = set()
    for seed in seeds.load_inventory(run["profile_slug"]):
        if _rank(seed, run["profile_slug"], sectors, geo, region_text)[0] >= EXCLUDED_RANK:
            continue
        if not seeds.is_unconfirmed(seed):
            boards.add(seed_key(seed))
        key = seeds.company_key(seed.company)
        if key and _counts(seed):
            companies.add(key)
        if key and key not in shown:
            shown.add(key)
            names.append(seed.company)
    for record in rounds:
        boards.update(record.get("boards", []))
        for name in record.get("companies", []):
            if (key := seeds.company_key(name)) and key not in shown:
                shown.add(key)
                names.append(name)
    return companies, boards, names


def round_status(run: dict, rounds: list[dict], fitting: int) -> str:
    settings = run_settings(run)
    if fitting >= settings["seed_target"]:
        return "target_met"
    if len(rounds) >= settings["seed_rounds"]:
        return "max_rounds"
    if rounds:
        last = rounds[-1]
        if last.get("host_blocked", 0):
            return "host_blocked"
        # A round whose proposals were all left unchecked (request budget) says nothing about progress.
        if int(last.get("checked", 0)) and int(last.get("fitting_after", 0)) <= int(last.get("fitting_before", 0)):
            return "no_progress"
    return "open"


def plan(run_id: str) -> dict:
    """The next round's request (``seeds plan``); ``more_rounds`` is false when no round should run."""
    run = runs.load_run(run_id)
    settings = run_settings(run)
    rounds = load_rounds(run_id)
    fitting, full_hosts = fitting_seeds(run)
    deficit = max(0, settings["seed_target"] - fitting)
    status = round_status(run, rounds, fitting)
    rate = observed_yield(rounds)
    ask = min(settings["seed_target"], ceil(deficit / rate)) if status == "open" else 0
    _companies, _boards, names = tried(run, rounds)
    number = len(rounds) + 1
    out = {
        "round": number if status == "open" else len(rounds),
        "max_rounds": settings["seed_rounds"],
        "seed_target": settings["seed_target"],
        "fitting_seeds": fitting,
        "seed_deficit": deficit,
        "status": status,
        "more_rounds": status == "open",
        "ask": ask,
        "yield": round(rate, 2),
    }
    if status == "open":
        out.update({
            "proposals_file": PROPOSALS_FILE.format(n=number),
            "full_hosts": full_hosts,
            "exclude_companies": names[:MAX_EXCLUDE_SHOWN],
            "exclude_total": len(names),
            "schema": PROPOSAL_SCHEMA,
        })
    return out


def round_record(*, number: int, received: int, repeats: list[str], rows: list[dict], results: list,
                 fitting_before: int, fitting_after: int) -> dict:
    """The ``seed_rounds.jsonl`` row of one verified round."""
    reasons = [r.reason for r in results if r.status != "verified"]
    unchecked = sum(1 for reason in reasons if reason in _UNCHECKED)
    boards = []
    # ``results`` follows ``rows`` (a round file has no invalid line). A proposal the budget or a blocked host
    # left unchecked is not marked as tried, so a later round may propose it again.
    for row, result in zip(rows, results, strict=False):
        if result.reason not in _UNCHECKED:
            boards += board_keys(row)
    # A verified board, or one that belongs to another company, is settled; a guessed board of unknown ownership
    # stays open so an explicit proposal can still promote it.
    boards += [
        seed_key(r.seed) for r in results
        if r.seed is not None and (r.status == "verified" or r.reason == "wrong_tenant")
    ]
    return {
        "round": number,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "received": received,
        "repeats": len(repeats),
        "checked": len(results) - unchecked,
        "verified": sum(1 for r in results if r.status == "verified"),
        "budget_exhausted": reasons.count("budget_exhausted"),
        "host_blocked": reasons.count("host_blocked"),
        "fitting_before": fitting_before,
        "fitting_after": fitting_after,
        "companies": [str(row.get("company") or "") for row in rows],
        "boards": sorted(set(boards)),
    }

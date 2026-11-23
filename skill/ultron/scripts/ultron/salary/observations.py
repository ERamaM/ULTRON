"""Aggregate comparable salary disclosures from saved vacancy captures."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from ultron.core.job_identity import canonical_job_identity
from ultron.core.privacy import redact
from ultron.core.rules.term_matching import normalize_text
from ultron.salary.disclosure import detect_geo_hint, extract_salaries_from_text, salary_markets
from ultron.storage import runs


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return round(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * pct
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return round(sorted_vals[lo])
    frac = pos - lo
    return round(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def _select_runs_for_mining(run_id: str | None, profile_slug: str | None, max_runs: int) -> list[str]:
    if run_id:
        runs.load_run(run_id)  # validates id and existence
        return [run_id]
    selected: list[str] = []
    for item in runs.list_runs():
        if profile_slug and item.get("profile_slug") != profile_slug:
            continue
        if "fetch" in (item.get("stages_done") or []):
            selected.append(str(item["run_id"]))
            if len(selected) >= max_runs:
                break
    return selected


def _kept_keys_for_run(run_folder: Path) -> set[str]:
    kept: set[str] = set()
    results_path = run_folder / "results.json"
    if results_path.is_file():
        try:
            import json

            rows = json.loads(results_path.read_text(encoding="utf-8")).get("rows") or []
            for row in rows:
                url = str(row.get("apply_url") or "")
                if url:
                    kept.add(canonical_job_identity(url, source_type=str(row.get("source_type") or ""), source_key=str(row.get("source_key") or "")).identity_key)
        except (OSError, ValueError):
            # Ignore unreadable or malformed results.json and fall back to kept.jsonl below.
            pass
    for row in runs.read_jsonl(run_folder / "kept.jsonl"):
        if key := row.get("identity_key"):
            kept.add(str(key))
    return kept


_WITHDRAWAL = re.compile(
    r"\b(?:salary|pay|compensation)(?: range| band)? (?:has been |is )?(?:withdrawn|removed|no longer disclosed)\b"
    r"|\b(?:banda|rango) salarial (?:retirad[oa]|ya no publicad[oa])\b"
)
_US = "united states"
_UK = "united kingdom"


def _market(hit: dict, location: str) -> str | None:
    named = hit["markets"] or sorted(salary_markets(location))
    if named:
        return named[0] if len(named) == 1 else None
    broad = detect_geo_hint(location, is_location_field=True)
    if broad == "unspecified":
        broad = hit["geo_hint"]
    return {"us": _US, "uk": _UK, "eu": "eu"}.get(broad)


def _context(row: dict, run_id: str, observed_at: str, kept_keys: set[str]) -> tuple[str, dict, dict] | None:
    job = row.get("job")
    if not isinstance(job, dict):
        return None
    key = str(row.get("identity_key") or "")
    url = str(job.get("apply_url") or "")
    identity = key or (canonical_job_identity(url).identity_key if url else "")
    if not identity:
        return None
    return identity, job, {
        "run_id": run_id, "observed_at": observed_at, "kept": key in kept_keys,
        **{field: str(redact(str(job.get(field) or ""))) for field in ("title", "company", "location")},
    }


def _comparable_key(sample: dict) -> tuple:
    return tuple(sample.get(key) or "" for key in ("market", "currency", "period", "compensation_type", "gross_or_net"))


def _stats(items: list[dict], *, annual: bool = False) -> dict:
    field = "annual_" if annual else ""
    mids = sorted((item[field + "low"] + item[field + "high"]) / 2 for item in items)
    return {
        "count": len(items),
        "kept_count": sum(item["kept"] for item in items),
        "min": min(item[field + "low"] for item in items),
        "p25": _percentile(mids, 0.25),
        "median": _percentile(mids, 0.5),
        "p75": _percentile(mids, 0.75),
        "max": max(item[field + "high"] for item in items),
    }


def _currency_stats(items: list[dict]) -> dict:
    return {
        **_stats(items, annual=True),
        "us_pay_transparency_count": sum(item["geo_hint"] == "us" for item in items),
        "non_us_count": sum(item["geo_hint"] not in {"us", "unspecified"} for item in items),
        "unknown_geo_count": sum(item["geo_hint"] == "unspecified" for item in items),
    }


def _selected_disclosures(run_ids: list[str]) -> tuple[set[str], list[tuple[str, dict]], list[dict]]:
    latest: dict[str, tuple[str, str]] = {}
    selected: dict[tuple, tuple[tuple, dict]] = {}
    withdrawals: dict[tuple[str, str | None], tuple[tuple, dict]] = {}
    for rid in run_ids:
        observed_at = str(runs.load_run(rid).get("created_at") or "")
        capture = (observed_at, rid)
        folder = runs.run_dir(rid)
        kept_keys = _kept_keys_for_run(folder)
        for row in runs.read_jsonl(folder / "postings.jsonl"):
            if (context := _context(row, rid, observed_at, kept_keys)) is None:
                continue
            identity, job, meta = context
            latest[identity] = max(capture, latest.get(identity, capture))
            body = str(job.get("body_text") or "")
            location = f"{job.get('location') or ''} {job.get('remote_text') or ''}"
            if _WITHDRAWAL.search(normalize_text(body)):
                markets = salary_markets(body) or salary_markets(location) or {None}
                for market in markets:
                    key = (identity, market)
                    if key not in withdrawals or capture > withdrawals[key][0]:
                        withdrawals[key] = (capture, {**meta, "market": market})
            for hit in extract_salaries_from_text(body):
                market = _market(hit, location)
                region = {"eu": "eu", _US: "us", _UK: "uk"}.get(market)
                if market and not region:
                    region = detect_geo_hint(market, is_location_field=True)
                location_region = detect_geo_hint(location, is_location_field=True)
                geo_hint = region or (hit["geo_hint"] if location_region == "unspecified" else location_region)
                sample = {**hit, **meta, "market": market, "geo_hint": geo_hint}
                key = (identity, *_comparable_key(sample))
                choice = (*capture, sample["low"], sample["high"], sample["snippet"])
                if key not in selected or choice > selected[key][0]:
                    selected[key] = (choice, sample)

    samples = []
    for key, (choice, sample) in selected.items():
        identity, market = key[0], sample["market"]
        withdrawn = any(
            entry and entry[0] >= choice[:2]
            for entry in (withdrawals.get((identity, market)), withdrawals.get((identity, None)))
        )
        if not withdrawn:
            samples.append((identity, {**sample, "historical": choice[:2] < latest[identity]}))
    return set(latest), samples, sorted(
        (entry[1] for entry in withdrawals.values()),
        key=lambda item: (item["observed_at"], item["run_id"], item["market"] or "", item["title"]),
        reverse=True,
    )


def mine_run_salaries(
    run_id: str | None = None,
    *,
    profile_slug: str | None = None,
    max_runs: int = 3,
    target_markets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Summarize the latest usable disclosure per vacancy and comparable market group."""
    run_ids = _select_runs_for_mining(run_id, profile_slug, max_runs)
    seen, samples, withdrawals = _selected_disclosures(run_ids)
    by_market: dict[tuple, list[dict]] = {}
    by_currency: dict[str, dict[str, dict]] = {}
    for identity, item in sorted(
        samples, key=lambda pair: (pair[1]["historical"], not pair[1]["kept"], _comparable_key(pair[1]), pair[0]),
    ):
        by_market.setdefault(_comparable_key(item), []).append(item)
        by_currency.setdefault(item["currency"], {}).setdefault(identity, item)
    comparable_groups = []
    for key, items in sorted(by_market.items()):
        market, currency, period, compensation_type, gross_or_net = key
        comparable_groups.append({
            "market": market or None, "currency": currency, "period": period,
            "compensation_type": compensation_type or None, "gross_or_net": gross_or_net or None,
            "historical_count": sum(item["historical"] for item in items), **_stats(items),
        })
    requested = set().union(*(salary_markets(text) for text in target_markets))
    if not requested and any(detect_geo_hint(target, is_location_field=True) == "eu" for target in target_markets):
        requested.add("eu")
    geo_order = {"unspecified": 1, "us": 2}
    ranked_samples = sorted(
        (item for _, item in samples),
        key=lambda item: (not item["kept"], item["historical"], geo_order.get(item["geo_hint"], 0), -item["annual_mid"]),
    )
    return {
        "runs_scanned": run_ids,
        "postings_scanned": len(seen),
        "disclosed_count": len({identity for identity, _item in samples}),
        "by_currency": {code: _currency_stats(list(items.values())) for code, items in sorted(by_currency.items())},
        "comparable_groups": comparable_groups,
        "target_comparable_groups": [group for group in comparable_groups if group["market"] in requested],
        "samples": ranked_samples[:15],
        "withdrawals": withdrawals[:15],
    }
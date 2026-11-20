"""Build the run-scoped ``HttpClient`` (shared by every network stage)."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path

from ultron import presets, runs
from ultron.core.ats.vendors import match_vendor_by_domain
from ultron.net import HttpClient, VendorBudget

# Consecutive 403/429 answers after which a stage stops calling that host for the rest of its run.
HOST_BLOCK_THRESHOLD = 3

# Vendor level: every host of one ATS vendor (``acme.wd5.myworkdayjobs.com``, ``boards-api.greenhouse.io``) shares
# one request cap per run (preset ``max_requests_per_vendor``), one breaker and one minimum spacing. Two strikes
# (each 429, or 403 answers with no 2xx between them) block the vendor for the rest of the run, in every stage.
VENDOR_BLOCK_THRESHOLD = 2
VENDOR_MIN_INTERVAL = 0.5
# Workday serves every customer from its own tenant host but rate-limits them together (session 3). HireHive does
# too, and more tightly: ten tenant boards 0.5 s apart were followed by HTTP 429 (session 5).
VENDOR_MIN_INTERVALS = {"workday": 1.0, "hirehive": 3.0}
# Ceiling on the run's total per vendor for the stages before fetch: verification (``seeds verify`` rounds, feed
# companies, search proposals) and discover stop once the run has spent half the vendor cap, so fetch (posting pages
# and enrichment calls) always keeps at least half. Fetch and ``diagnose`` may use the whole cap.
DISCOVER_VENDOR_SHARE = 0.5
VERIFY_VENDOR_SHARE = 0.5
# Run-folder file with the vendor state, so a later stage or `seeds verify` process honours the same cap and breaker.
VENDOR_STATE_FILE = "vendor_budget.json"

logger = logging.getLogger(__name__)

# Test hook: merged into every client built by ``client_for_run`` (transport, resolver, sleep, ...).
TEST_OVERRIDES: dict = {}


def set_test_overrides(**overrides) -> None:
    TEST_OVERRIDES.update(overrides)


def reset_test_overrides() -> None:
    TEST_OVERRIDES.clear()


def vendor_for_host(host: str) -> str | None:
    """The supported ATS vendor serving ``host`` (any of its tenant or API hosts), else ``None``."""
    item = match_vendor_by_domain(host)
    return item.name if item else None


def _vendor_cap(run: dict) -> int:
    settings = run.get("settings") or {}
    cap = settings.get("max_requests_per_vendor")
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
        return cap
    # A run created before the setting existed: its preset's value (balanced for an unknown preset).
    preset = presets.PRESETS.get(str(run.get("preset") or ""), presets.PRESETS[presets.DEFAULT_PRESET])
    return preset["max_requests_per_vendor"]


def _state_path(run: dict) -> Path | None:
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    try:
        folder = runs.run_dir(run_id)
    except runs.RunNotFoundError:
        return None
    return folder / VENDOR_STATE_FILE if folder.is_dir() else None


def _save_state(path: Path, budget: VendorBudget) -> None:
    """Best effort: a failed write keeps the in-memory budget, which still bounds this client."""
    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".vendor-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(budget.to_dict(), handle)
        os.replace(tmp, path)
        tmp = None
    except OSError as exc:  # e.g. a file briefly locked by another process on Windows
        logger.debug("vendor budget state not saved to %s: %s", path, exc)
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()


def load_vendor_budget(run: dict) -> VendorBudget:
    """The run's vendor budget, read from its run folder and saved back after every change."""
    path = _state_path(run)
    state = None
    if path is not None and path.is_file():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = None
    return VendorBudget(
        max_requests=_vendor_cap(run), block_threshold=VENDOR_BLOCK_THRESHOLD, state=state,
        on_change=(lambda budget: _save_state(path, budget)) if path is not None else None,
    )


def client_for_run(run: dict, *, vendor_share: float = 1.0, **overrides) -> HttpClient:
    """Budgets come from the run's preset settings; ``overrides`` (transport, resolver, sleep, ...) win."""
    settings = run["settings"]
    options = {
        "max_requests": settings["max_pages"] + settings["max_seeds_per_run"] * 2,
        "max_requests_per_host": settings["max_requests_per_host"],
        "timeout": settings["ats_api_timeout"],
        "vendor_of": vendor_for_host,
        "vendor_budget": load_vendor_budget(run),
        "vendor_share": vendor_share,
        "min_interval_per_vendor": VENDOR_MIN_INTERVAL,
        "vendor_intervals": VENDOR_MIN_INTERVALS,
    }
    options.update(TEST_OVERRIDES)
    options.update(overrides)
    return HttpClient(**options)

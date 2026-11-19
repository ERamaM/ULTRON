"""Build the run-scoped ``HttpClient`` (shared by every network stage)."""

from __future__ import annotations

from ultron.net import HttpClient

# Test hook: merged into every client built by ``client_for_run`` (transport, resolver, sleep, ...).
TEST_OVERRIDES: dict = {}


def set_test_overrides(**overrides) -> None:
    TEST_OVERRIDES.update(overrides)


def reset_test_overrides() -> None:
    TEST_OVERRIDES.clear()


def client_for_run(run: dict, **overrides) -> HttpClient:
    """Budgets come from the run's preset settings; ``overrides`` (transport, resolver, sleep, ...) win."""
    settings = run["settings"]
    options = {
        "max_requests": settings["max_pages"] + settings["max_seeds_per_run"] * 2,
        "max_requests_per_host": settings["max_requests_per_host"],
        "timeout": settings["ats_api_timeout"],
    }
    options.update(TEST_OVERRIDES)
    options.update(overrides)
    return HttpClient(**options)

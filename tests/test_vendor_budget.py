"""Vendor-level request budget, pacing and breaker shared by every tenant host of one ATS vendor.

Session 3 got HTTP 429 from Workday after many runs spread their requests over a dozen ``*.myworkdayjobs.com``
tenants: every per-host cap and breaker counted each tenant separately, so nothing bounded the vendor as a whole.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ultron.infra.net import HttpClient, HttpError, RequestBudgetExceeded, VendorBlocked, VendorBudget
from ultron.profiles import store as profiles
from ultron.stages import http_factory
from ultron.storage import runs


def _vendor_of(host: str) -> str | None:
    return "workday" if host.endswith(".myworkdayjobs.com") else None


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_client(handler, *, budget: VendorBudget, share: float = 1.0, clock: FakeClock | None = None, **extra):
    clock = clock or FakeClock()
    options = {
        "max_requests": 500, "max_requests_per_host": 500, "min_interval_per_host": 0.0,
        "transport": httpx.MockTransport(handler), "resolver": lambda h, p: ["93.184.216.34"],
        "sleep": clock.sleep, "clock": clock, "vendor_of": _vendor_of, "vendor_budget": budget,
        "vendor_share": share,
    }
    options.update(extra)
    return HttpClient(**options)


def tenant(i: int) -> str:
    return f"https://tenant{i}.wd{1 + i % 5}.myworkdayjobs.com/x"


def test_vendor_cap_counts_requests_across_tenant_hosts():
    budget = VendorBudget(max_requests=5)
    client = make_client(lambda r: httpx.Response(200, text="ok"), budget=budget)
    for i in range(5):
        client.get_text(tenant(i))  # five different hosts, each far below its own per-host cap
    with pytest.raises(RequestBudgetExceeded) as caught:
        client.get_text(tenant(9))
    assert caught.value.scope == "vendor" and caught.value.vendor == "workday"
    assert budget.requests == {"workday": 5}
    # A host of no vendor is not limited by it.
    assert client.get_text("https://employer.example.org/")[0] == 200


def test_429s_from_two_tenants_block_every_tenant_of_the_vendor():
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        return httpx.Response(429) if request.url.host.startswith(("tenant0.", "tenant1.")) else (
            httpx.Response(200, text="ok"))

    budget = VendorBudget(max_requests=100, block_threshold=2)
    client = make_client(handler, budget=budget)
    # Strikes count per request: the retries of one rate-limited request are one strike.
    assert client.get_text(tenant(0))[0] == 429
    assert len(hits) == 3 and budget.strikes("workday") == 1 and not budget.is_blocked("workday")
    assert client.get_text(tenant(1))[0] == 429  # a second tenant: strike 2 blocks the vendor
    assert budget.blocked == {"workday"}
    with pytest.raises(VendorBlocked) as caught:
        client.get_text(tenant(2))  # a third tenant: no request is sent
    assert caught.value.vendor == "workday" and len(hits) == 6


def test_429_strikes_are_cumulative_but_403_strikes_reset_on_success():
    statuses = iter([429, 200, 200, 403, 200, 403])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), text="ok")

    budget = VendorBudget(max_requests=100, block_threshold=2)
    client = make_client(handler, budget=budget, sleep=lambda s: None)
    client.get_text(tenant(0))  # 429 then 200 on the retry: one strike, kept
    client.get_text(tenant(1))  # 200 from another tenant does not clear the 429
    assert budget.strikes("workday") == 1
    client.get_text(tenant(2))  # 403: strike 2 -> blocked
    assert budget.is_blocked("workday")


def test_two_403_in_a_row_block_but_a_success_between_them_does_not():
    budget = VendorBudget(max_requests=100, block_threshold=2)
    answers = iter([403, 200, 403, 200])
    client = make_client(lambda r: httpx.Response(next(answers), text="ok"), budget=budget)
    for i in range(4):
        client.get_text(tenant(i))
    assert not budget.is_blocked("workday")
    blocked = VendorBudget(max_requests=100, block_threshold=2)
    client = make_client(lambda r: httpx.Response(403), budget=blocked)
    client.get_text(tenant(0))
    client.get_text(tenant(1))
    assert blocked.is_blocked("workday")


def test_vendor_pacing_spaces_requests_to_different_tenants():
    clock = FakeClock()
    budget = VendorBudget(max_requests=100)
    client = make_client(
        lambda r: httpx.Response(200, text="ok"), budget=budget, clock=clock, vendor_intervals={"workday": 1.0},
    )
    client.get_text(tenant(0))
    client.get_text(tenant(1))
    client.get_text(tenant(2))
    assert clock.slept == [1.0, 1.0]
    client.get_text("https://employer.example.org/")  # not a vendor host: no vendor wait
    assert clock.slept == [1.0, 1.0]


def test_client_share_limits_one_client_but_not_the_run():
    budget = VendorBudget(max_requests=10)
    first = make_client(lambda r: httpx.Response(200, text="ok"), budget=budget, share=0.5)
    assert first.vendor_allowance("workday") == 5
    for i in range(5):
        first.get_text(tenant(i))
    assert first.vendor_allowance("workday") == 0
    with pytest.raises(RequestBudgetExceeded):
        first.get_text(tenant(6))
    second = make_client(lambda r: httpx.Response(200, text="ok"), budget=budget)
    assert second.vendor_allowance("workday") == 5  # what the run has left


def test_vendor_state_survives_into_the_next_client_of_the_same_run():
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "fast")
    run = runs.load_run(run_id)
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        return httpx.Response(429)

    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"], sleep=lambda s: None,
    )
    first = http_factory.client_for_run(run)
    for host in ("acme.wd3", "beta.wd5"):
        with pytest.raises(HttpError):
            first.request_text(f"https://{host}.myworkdayjobs.com/en-US/External")
    assert len(hits) == 6
    state = json.loads((runs.run_dir(run_id) / http_factory.VENDOR_STATE_FILE).read_text(encoding="utf-8"))
    assert state["blocked"] == ["workday"] and state["requests"] == {"workday": 6}
    # A later stage (or another `seeds verify --round` process) builds a new client: the vendor stays blocked.
    second = http_factory.client_for_run(runs.load_run(run_id))
    with pytest.raises(VendorBlocked):
        second.get_text("https://other.wd1.myworkdayjobs.com/en-US/Careers")
    assert len(hits) == 6


def test_client_for_run_maps_every_tenant_host_to_its_vendor():
    assert http_factory.vendor_for_host("jj.wd5.myworkdayjobs.com") == "workday"
    assert http_factory.vendor_for_host("careers-acme.icims.com") == "icims"
    assert http_factory.vendor_for_host("acme.hirehive.com") == "hirehive"
    assert http_factory.vendor_for_host("api.occupop.com") == "occupop"
    assert http_factory.vendor_for_host("www.candidatemanager.net") == "candidatemanager"
    assert http_factory.vendor_for_host("acme.jobs.personio.de") == "personio"
    assert http_factory.vendor_for_host("boards-api.greenhouse.io") == "greenhouse"
    assert http_factory.vendor_for_host("job-boards.greenhouse.io") == "greenhouse"
    assert http_factory.vendor_for_host("www.example.org") is None


def test_corrupt_or_missing_state_starts_a_fresh_budget():
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "fast")
    (runs.run_dir(run_id) / http_factory.VENDOR_STATE_FILE).write_text("{not json", encoding="utf-8")
    client = http_factory.client_for_run(runs.load_run(run_id))
    assert client.vendor_allowance("workday") == runs.load_run(run_id)["settings"]["max_requests_per_vendor"]
    # A run created before the setting existed still gets a cap from its preset.
    legacy = runs.load_run(run_id)
    del legacy["settings"]["max_requests_per_vendor"]
    assert http_factory.client_for_run(legacy).vendor_allowance("workday") > 0


def test_a_429_recovered_by_its_retry_still_clears_an_earlier_403_strike():
    # Codex review: (200, rate_limited=True) skipped the 2xx branch, so a stale 403 plus the 429 blocked the vendor.
    budget = VendorBudget(max_requests=100, block_threshold=3)
    budget.record("workday", 403)
    budget.record("workday", 200, rate_limited=True)
    assert budget.forbidden_streak.get("workday", 0) == 0 and budget.strikes("workday") == 1
    budget = VendorBudget(max_requests=100, block_threshold=2)
    budget.record("workday", 403)
    budget.record("workday", 200, rate_limited=True)
    assert not budget.is_blocked("workday")


def test_vendor_blocked_is_a_host_blocked_with_both_names():
    exc = VendorBlocked("workday", "acme.wd1.myworkdayjobs.com")
    assert exc.host == "acme.wd1.myworkdayjobs.com" and exc.vendor == "workday"
    assert str(exc) == "vendor_blocked: workday" and exc.status_code is None


def test_a_429_strike_is_kept_when_the_budget_interrupts_its_retries():
    # Codex review round 2: the 429 on the last allowed request, whose retry hit the vendor cap, was never recorded.
    budget = VendorBudget(max_requests=1, block_threshold=2)
    client = make_client(lambda r: httpx.Response(429), budget=budget)
    with pytest.raises(RequestBudgetExceeded):
        client.get_text(tenant(0))
    assert budget.rate_limited == {"workday": 1}


def test_a_429_strike_is_kept_when_the_global_budget_interrupts_its_retries():
    budget = VendorBudget(max_requests=100, block_threshold=2)
    client = make_client(lambda r: httpx.Response(429), budget=budget, max_requests=1)
    with pytest.raises(RequestBudgetExceeded):
        client.get_text(tenant(0))
    assert budget.rate_limited == {"workday": 1}


def test_vendor_spacing_counts_from_the_actual_send_after_a_host_wait():
    # Codex review round 2: the vendor timestamp was taken before the host wait, so the next tenant went at once.
    clock = FakeClock()
    sends: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append((request.url.host, clock.now))
        return httpx.Response(200, text="ok")

    budget = VendorBudget(max_requests=100)
    client = make_client(handler, budget=budget, clock=clock, vendor_intervals={"workday": 1.0},
                         min_interval_per_host=5.0)
    client.get_text(tenant(0))
    client.get_text(tenant(0))  # same host: waits 5 s (the host interval)
    client.get_text(tenant(1))  # other tenant: must still wait the vendor's 1 s after the previous send
    times = [t for _host, t in sends]
    assert times[1] - times[0] >= 5.0
    assert times[2] - times[1] >= 1.0
    assert clock.slept == [5.0, 1.0]  # one combined wait per request

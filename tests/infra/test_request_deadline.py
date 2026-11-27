import threading
import time

import httpx
import pytest

from ultron.infra.net import HostGate, HttpClient, RequestBudgetExceeded


def _client(handler, **options):
    return HttpClient(
        max_requests=10,
        max_requests_per_host=10,
        resolver=lambda *_: ["93.184.216.34"],
        transport=httpx.MockTransport(handler),
        **options,
    )


def test_deadline_caps_request_timeout_and_prevents_retry():
    requests = []

    def handler(request):
        requests.append(request)
        assert max(request.extensions["timeout"].values()) == pytest.approx(0.2)
        return httpx.Response(503)

    with (
        _client(handler, deadline=1.2, clock=lambda: 1.0) as client,
        pytest.raises(RequestBudgetExceeded, match="retry"),
    ):
        client.get_text("https://example.org/jobs")
    assert len(requests) == 1


def test_expired_deadline_stops_before_transport():
    with _client(lambda _: pytest.fail("expired request reached transport"), deadline=1, clock=lambda: 2) as client:
        with pytest.raises(RequestBudgetExceeded, match="time budget"):
            client.get_text("https://example.org/jobs")
        assert client.requests_made == 0


def test_gate_wait_respects_deadline():
    gate = HostGate()
    with (
        gate.lock_for("example.org"),
        _client(lambda _: pytest.fail("locked request reached transport"), deadline=time.monotonic() + 0.03,
                gate=gate) as client,
        pytest.raises(RequestBudgetExceeded, match="time budget"),
    ):
        client.get_text("https://example.org/jobs")
    assert gate.lock_for("example.org").acquire(timeout=0.01)
    gate.lock_for("example.org").release()


def test_success_before_deadline_releases_gate():
    gate = HostGate()
    with _client(lambda _: httpx.Response(200, content=b"ok"), deadline=time.monotonic() + 5, gate=gate) as client:
        assert client.get_text("https://example.org/jobs")[-1] == "ok"
    lock: threading.Lock = gate.lock_for("example.org")
    assert lock.acquire(blocking=False)
    lock.release()


def test_deadline_returns_while_resolution_is_pending():
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    outcomes = []
    dispatched = []

    def resolve(*_):
        entered.set()
        release.wait(timeout=8)
        return ["93.184.216.34"]

    def fetch():
        try:
            with HttpClient(max_requests=1, max_requests_per_host=1, resolver=resolve,
                            transport=httpx.MockTransport(dispatched.append),
                            deadline=time.monotonic() + 0.25) as client:
                client.get_text("https://example.org/jobs")
        except RequestBudgetExceeded as error:
            outcomes.append(str(error))
        finally:
            returned.set()

    caller = threading.Thread(target=fetch, daemon=True)
    caller.start()
    try:
        assert entered.wait(timeout=4)
        assert returned.wait(timeout=2), "request waited for the blocked resolver"
        assert len(outcomes) == 1 and "time budget" in outcomes[0]
    finally:
        release.set()
        caller.join(timeout=4)
    assert not dispatched

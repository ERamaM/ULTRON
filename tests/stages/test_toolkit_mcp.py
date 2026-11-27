"""``ultron mcp``: the stdio MCP server, driven in-process with injected streams and a mocked network."""

from __future__ import annotations

import io
import json

import httpx
import pytest
from toolkit_helpers import EMPLOYER, POSTING, handler

from ultron import seeds
from ultron.profiles import store as profiles
from ultron.stages import http_factory
from ultron.toolkit import mcp_server

SLUG = profiles.EXAMPLE_SLUG


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: empty)
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    profiles.install_example()


def _session(*messages) -> list[dict]:
    lines = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in messages)
    out = io.StringIO()
    assert mcp_server.serve(io.StringIO(lines + "\n"), out) == 0
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _call(name: str, arguments, message_id: int = 9) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def _payload(response: dict) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


def test_handshake_lists_all_tools():
    init, tools, ping = _session(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    )
    assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["capabilities"]["tools"]
    assert [t["name"] for t in tools["result"]["tools"]] == ["gather_candidates", "search_jobs", "inspect_jobs", "save_shortlist"]
    assert all("inputSchema" in t for t in tools["result"]["tools"]) and ping["result"] == {}


def test_unknown_protocol_version_gets_the_newest():
    (init,) = _session({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999"}})
    assert init["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSIONS[0]


def test_inspect_then_save_through_the_tools():
    inspected, saved = _session(
        _call("inspect_jobs", {"profile": SLUG, "urls": [POSTING, EMPLOYER]}, 1),
        _call("save_shortlist", {"profile": SLUG, "rows": [
            {"id": "greenhouse:acme:4012345", "verdict": "keep", "priority": 1}]}, 2),
    )
    dossiers = _payload(inspected)["dossiers"]
    assert {d["url"] for d in dossiers if d["status"] == "open"} >= {POSTING, EMPLOYER}
    assert not inspected["result"]["isError"]
    result = _payload(saved)
    assert result["saved"]["keep"] == 1 and result["roles"] == 1


@pytest.mark.parametrize(("work_mode", "eligibility", "stored_mode", "stored_eligibility"), [
    ("hybrid", "confirmed", "hybrid", "confirmed"),
    ("on-site", "high", "onsite", "high"),
    ("full_remote", "Eligible from Spain", "unknown", "unknown"),
    ("remote", "eligible", "unknown", "unknown"),
])
def test_save_mcp_keeps_work_mode_and_unknown_aliases(work_mode, eligibility, stored_mode, stored_eligibility):
    from ultron.stages.scout_verified import load_verified
    from ultron.storage import runs

    listed, _, saved = _session(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        _call("inspect_jobs", {"profile": SLUG, "urls": [POSTING]}, 2),
        _call("save_shortlist", {"profile": SLUG, "rows": [{"id": "greenhouse:acme:4012345", "verdict": "keep",
              "remote_policy": work_mode, "eligibility": eligibility, "eligibility_evidence": "Work in Spain"}]}, 3),
    )
    tool = next(item for item in listed["result"]["tools"] if item["name"] == "save_shortlist")
    fields = tool["inputSchema"]["properties"]["rows"]["items"]["properties"]
    assert {"hybrid", "onsite", "unknown"} <= set(fields["remote_policy"]["enum"])
    assert set(fields["eligibility"]["enum"]) == {"confirmed", "high", "medium", "unknown"}
    result = _payload(saved)
    assert result["saved"]["keep"] == 1 and result["not_saved"] == []
    row, = load_verified(runs.run_dir(result["run_id"])).values()
    assert (row["remote_policy"], row["eligibility"], row["eligibility_evidence"]) == (
        stored_mode, stored_eligibility, "Work in Spain")


def test_mcp_inspection_reports_an_exhausted_budget(monkeypatch):
    monkeypatch.setattr(mcp_server, "MCP_INSPECTION_SECONDS", 0.0)
    response, = _session(_call("inspect_jobs", {"profile": SLUG, "urls": [POSTING], "detail": True}))
    assert not response["result"]["isError"]
    result = _payload(response)
    assert result["partial"] and result["requests"] == 0


def test_search_through_the_tool():
    (response,) = _session(_call("search_jobs", {"profile": SLUG, "limit": 5}))
    assert "sources" in _payload(response) and not response["result"]["isError"]


@pytest.mark.parametrize("name, arguments, error", [
    ("inspect_jobs", {"profile": SLUG}, "invalid_arguments"),
    ("inspect_jobs", {"profile": SLUG, "urls": "not a list"}, "invalid_arguments"),
    ("inspect_jobs", {"profile": "", "urls": [POSTING]}, "invalid_arguments"),
    ("inspect_jobs", {"profile": "missing-profile", "urls": [POSTING]}, "profile_not_found"),
    ("search_jobs", {"profile": SLUG, "limit": "ten"}, "invalid_arguments"),
    ("search_jobs", {"profile": SLUG, "query": 5}, "invalid_arguments"),
    ("save_shortlist", {"profile": SLUG, "rows": []}, "invalid_arguments"),
    ("nope", {}, "unknown_tool"),
    ("inspect_jobs", ["not", "an", "object"], "invalid_arguments"),
])
def test_tool_errors_are_results_not_crashes(name, arguments, error):
    (response,) = _session(_call(name, arguments))
    assert response["result"]["isError"] and _payload(response)["error"] == error


def test_unexpected_failures_keep_the_server_answering(monkeypatch):
    def boom(_arguments, _budget):
        raise RuntimeError("bug")
    monkeypatch.setitem(mcp_server.HANDLERS, "search_jobs", boom)
    failed, ping = _session(_call("search_jobs", {"profile": SLUG}, 1), {"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert _payload(failed)["error"] == "internal_error" and ping["result"] == {}


def test_protocol_errors():
    parse, invalid, unknown, no_name = _session(
        "{not json", {"id": 4, "method": "ping"}, {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {}},
    )
    assert parse["error"]["code"] == mcp_server.PARSE_ERROR
    assert invalid["error"]["code"] == mcp_server.INVALID_REQUEST
    assert unknown["error"]["code"] == mcp_server.METHOD_NOT_FOUND
    assert no_name["error"]["code"] == mcp_server.INVALID_PARAMS


def test_cli_server_speaks_only_protocol_on_stdout(tmp_path):
    import os
    import subprocess
    import sys

    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    env = {**os.environ, "ULTRON_HOME": str(tmp_path / "home"), "ULTRON_NO_UPDATE_CHECK": "1"}
    done = subprocess.run([sys.executable, "-c", "import sys; from ultron.cli import main; sys.exit(main(['mcp']))"],
                          input="".join(json.dumps(m) + "\n" for m in messages), capture_output=True, text=True,
                          encoding="utf-8", env=env, timeout=60, check=False)
    lines = [json.loads(line) for line in done.stdout.splitlines()]
    assert done.returncode == 0 and [line["id"] for line in lines] == [1, 2]
    assert lines[1]["result"]["tools"][0]["name"] == "gather_candidates"


def _gather_fixture(count: int) -> dict:
    return {
        "profile": SLUG, "queries": ["site:example.test senior engineer"] * 20,
        "web_search": {"queries_run": 20, "fallback_to_agent_web_search": False},
        "sources": {"feeds": 8}, "inspected": {"open": count, "unresolved_aggregator": 2},
        "returned": count, "dossiers": [
            {"id": f"vendor:company:{i}", "url": f"https://jobs.example.test/company/job/{i}",
             "title": "Senior Machine Learning Engineer", "company": "Example Company",
             "location": "Spain", "status": "open", "remote": "", "salary": None,
             "geo_excerpt": 'Remoto desde España. Find out about "híbrido" or fully remote. ' * 6,
             "role_excerpt": "Implement models and evaluate machine learning systems at scale. " * 12}
            for i in range(count)
        ],
        "unresolved": [{"url": "https://aggregator.example.test/" + "ü" * 20_000, "company": ""}],
    }


def test_gather_mcp_keeps_sixty_ranked_dossiers_inline(monkeypatch):
    gathered = _gather_fixture(60)
    monkeypatch.setattr(mcp_server, "gather_candidates", lambda *args, select_response, **kwargs: select_response(gathered))
    (response,) = _session(_call("gather_candidates", {"profile": SLUG}))
    text = response["result"]["content"][0]["text"]
    payload = _payload(response)
    assert not response["result"]["isError"] and len(text.encode("utf-8")) <= 44_000
    assert payload["returned"] == 60 and not payload.get("truncated")
    assert [row["id"] for row in payload["dossiers"]] == [row["id"] for row in gathered["dossiers"]]
    assert "queries" not in payload and "unresolved" not in payload
    assert payload["web_search"] == gathered["web_search"] and payload["inspected"] == gathered["inspected"]
    first = payload["dossiers"][0]
    assert "remote" not in first and "salary" not in first
    assert 0 < len(first["geo_excerpt"]) <= 290 and 0 < len(first["role_excerpt"]) <= 380
    assert gathered["dossiers"][0]["remote"] == "" and len(gathered["dossiers"][0]["role_excerpt"]) > 700


def test_gather_reports_a_ranked_prefix_with_large_multibyte_urls(monkeypatch):
    gathered = _gather_fixture(10)
    gathered["sources"]["large"] = "x" * 40_000
    for row in gathered["dossiers"]:
        row["url"] += "/" + "職" * 4_000
    monkeypatch.setattr(mcp_server, "gather_candidates", lambda *args, select_response, **kwargs: select_response(gathered))
    (response,) = _session(_call("gather_candidates", {"profile": SLUG, "detail": True}))
    text = response["result"]["content"][0]["text"]
    payload = _payload(response)
    assert not response["result"]["isError"] and len(text.encode("utf-8")) <= 44_000
    assert payload["truncated"] and payload["returned"] == 3 and payload["dossiers_omitted"] == 7
    assert [row["id"] for row in payload["dossiers"]] == [row["id"] for row in gathered["dossiers"][:3]]
    assert payload["web_search"] == gathered["web_search"] and "sources" not in payload


def test_inspect_mcp_detail_keeps_full_excerpts(monkeypatch):
    result = _gather_fixture(1)
    monkeypatch.setattr(mcp_server, "inspect_jobs", lambda *args, **kwargs: result)
    (response,) = _session(_call("inspect_jobs", {"profile": SLUG, "urls": [POSTING], "detail": True}))
    assert _payload(response) == result


@pytest.mark.parametrize("large_diagnostics", [False, True])
def test_gather_detail_preserves_full_excerpts_for_a_small_batch(monkeypatch, large_diagnostics):
    gathered = _gather_fixture(2)
    gathered["unresolved"] = []
    if large_diagnostics:
        gathered["sources"]["large"] = "x" * 50_000
    dossier = gathered["dossiers"][0]
    dossier["role_excerpt"] = "R" * 2_500
    dossier["geo_excerpt"] = "G" * 350
    monkeypatch.setattr(mcp_server, "gather_candidates", lambda *args, select_response, **kwargs: select_response(gathered))
    (response,) = _session(_call("gather_candidates", {"profile": SLUG, "detail": True}))
    payload = _payload(response)
    first = payload["dossiers"][0]
    assert (first["geo_excerpt"], first["role_excerpt"]) == (dossier["geo_excerpt"], dossier["role_excerpt"])
    assert ("sources" in payload) is not large_diagnostics


def test_gather_drops_the_tail_before_cutting_below_excerpt_floors(monkeypatch):
    gathered = _gather_fixture(76)
    monkeypatch.setattr(mcp_server, "gather_candidates", lambda *args, select_response, **kwargs: select_response(gathered))
    (response,) = _session(_call("gather_candidates", {"profile": SLUG}))
    text = response["result"]["content"][0]["text"]
    payload = _payload(response)
    assert len(text.encode("utf-8")) <= 44_000 and payload["truncated"]
    count = payload["returned"]
    assert 50 <= count < 76 and payload["dossiers_omitted"] == 76 - count
    assert [row["id"] for row in payload["dossiers"]] == [row["id"] for row in gathered["dossiers"][:count]]
    assert all(len(row["geo_excerpt"]) == 220 and len(row["role_excerpt"]) == 240 for row in payload["dossiers"])


def test_inspect_mcp_caps_an_oversized_detail_batch(monkeypatch):
    result = _gather_fixture(25)
    result.update(results=25, requests=25, partial=True, status={"open": 25})
    for row in result["dossiers"]:
        row["role_excerpt"] = "R" * 2_500
    monkeypatch.setattr(mcp_server, "inspect_jobs", lambda *args, **kwargs: result)
    (response,) = _session(_call("inspect_jobs", {"profile": SLUG, "urls": [POSTING], "detail": True}))
    assert len(response["result"]["content"][0]["text"].encode("utf-8")) <= 44_000
    payload = _payload(response)
    assert payload["partial"] and payload["status"] == {"open": 25} and payload["requests"] == 25
    assert len(payload["dossiers"]) == 25
    assert all(len(row["role_excerpt"]) >= 240 for row in payload["dossiers"])


def test_workable_breaker_stops_retries_and_follow_up_calls_in_one_mcp_session(monkeypatch):
    from ultron.core.web_search.models import SearchHit, WebSearchResult
    from ultron.toolkit import gather
    from ultron.toolkit.search import SourceDiscovery

    hits = []

    def rate_limited(request):
        if request.url.host == "apply.workable.com":
            hits.append(request.url.path)
            return httpx.Response(429)
        return handler(request)

    http_factory.set_test_overrides(transport=httpx.MockTransport(rate_limited))
    workable = "https://apply.workable.com/example/j/0123456789/"
    hit = SearchHit("Senior ML Engineer", workable, "", "query", "ddg_html")
    monkeypatch.setattr(gather, "search_web", lambda *a, **k: WebSearchResult("ok", "ddg_html", 1, 1, (hit,)))
    monkeypatch.setattr(gather, "discover_sources", lambda *a, **k: SourceDiscovery([], [], {}, set()))
    first, follow_up = _session(
        _call("gather_candidates", {"profile": SLUG, "web_provider": "ddg_html"}, 1),
        _call("inspect_jobs", {"profile": SLUG, "urls": [workable, POSTING], "all": True}, 2),
    )
    assert not first["result"]["isError"] and not follow_up["result"]["isError"]
    assert _payload(follow_up)["status"]["open"] >= 1
    assert len(hits) == 2
    _session(_call("inspect_jobs", {"profile": SLUG, "urls": [workable], "all": True}, 3))
    assert len(hits) == 4


def test_mcp_save_rejects_oversized_batch_without_saving(monkeypatch):
    from ultron.toolkit.shortlist import MAX_ROWS

    def unexpected_save(*args, **kwargs):
        pytest.fail("oversized batch reached shortlist persistence")

    monkeypatch.setattr(mcp_server, "save_shortlist", unexpected_save)
    response, = _session(_call("save_shortlist", {"profile": SLUG, "rows": [{}] * (MAX_ROWS + 1)}))
    assert response["result"]["isError"]
    assert str(MAX_ROWS) in str(response["result"]["content"])



@pytest.mark.parametrize("name", ["gather_candidates", "search_jobs"])
@pytest.mark.parametrize("failure", ["write", "flush", "short_write"])
def test_failed_stdio_write_keeps_watch_results_pending(name, failure):
    from contextlib import closing

    from ultron.core.job_identity import canonical_job_identity
    from ultron.storage import ledger, watchlist

    identity = canonical_job_identity(POSTING).identity_key
    with closing(ledger.connect()) as conn:
        watchlist.add_pending(conn, SLUG, [(identity, {
            "identity_key": identity, "normalized_url": POSTING, "ats_title": "Senior Machine Learning Engineer",
            "ats_company": "Acme", "ats_location": "Spain",
        })])

    class BrokenOutput(io.StringIO):
        def write(self, text):
            if failure == "write":
                raise BrokenPipeError("synthetic disconnect")
            if failure == "short_write":
                return super().write(text[:1])
            return super().write(text)

        def flush(self):
            if failure == "flush":
                raise BrokenPipeError("synthetic disconnect")
            super().flush()

    arguments = {"profile": SLUG}
    if name == "gather_candidates":
        arguments["web_provider"] = "none"
    call = _call(name, arguments)
    with pytest.raises(OSError):
        mcp_server.serve(io.StringIO(json.dumps(call) + "\n"), BrokenOutput())
    with closing(ledger.connect()) as conn:
        assert [row["identity_key"] for row in watchlist.pending(conn, SLUG)] == [identity]
    result, = _session(call)
    assert any(row["id"] == identity and row["source"] == "watch" for row in _payload(result)["dossiers"])
    with closing(ledger.connect()) as conn:
        assert not watchlist.pending(conn, SLUG)

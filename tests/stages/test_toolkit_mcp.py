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


def test_handshake_lists_exactly_three_tools():
    init, tools, ping = _session(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    )
    assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["capabilities"]["tools"]
    assert [t["name"] for t in tools["result"]["tools"]] == ["search_jobs", "inspect_jobs", "save_shortlist"]
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
    assert {d["status"] for d in dossiers} == {"open"} and not inspected["result"]["isError"]
    result = _payload(saved)
    assert result["saved"]["keep"] == 1 and result["roles"] == 1


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
    def boom(_arguments):
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
    assert lines[1]["result"]["tools"][0]["name"] == "search_jobs"

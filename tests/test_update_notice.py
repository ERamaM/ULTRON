from __future__ import annotations

import json
import os
import time

import httpx
import pytest

from ultron import cli, update_notice


def test_version_check_reports_newer_release_once(monkeypatch, tmp_path, capsys):
    calls = []
    tag = "v0.10.0"

    def get(url, *, timeout, trust_env):
        assert trust_env is False  # proxies from the environment are ignored, as in net.py
        calls.append(url)
        return httpx.Response(200, json={"tag_name": tag}, request=httpx.Request("GET", url))

    monkeypatch.setenv("ULTRON_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "__version__", "0.9.0")
    monkeypatch.setattr(update_notice.httpx, "get", get)
    assert cli.main(["version"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ultron": "0.9.0"}
    assert not calls

    assert cli.main(["version", "--check-update"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["update_available"]["version"] == "0.10.0"
    assert result["update_available"]["url"].endswith("/releases/tag/v0.10.0")
    assert update_notice.release_notice("0.9.0") is None
    assert len(calls) == 1

    checked = tmp_path / "update-notices" / "last-checked"
    yesterday = time.time() - 90_000
    os.utime(checked, (yesterday, yesterday))
    assert update_notice.release_notice("0.9.0") is None
    assert len(calls) == 2

    os.utime(checked, (yesterday, yesterday))
    tag = "v0.11.0"
    assert update_notice.release_notice("0.9.0")["version"] == "0.11.0"


@pytest.mark.parametrize("status,payload", [
    (200, {"tag_name": "v0.1.0"}),
    (200, {"tag_name": "v1.0.0", "prerelease": True}),
    (200, {"tag_name": "../../unexpected"}),
    (404, {}),
    (429, {}),
])
def test_check_fails_quietly_without_a_new_stable_release(monkeypatch, tmp_path, status, payload):
    monkeypatch.setenv("ULTRON_HOME", str(tmp_path))

    def get(url, *, timeout, trust_env):
        assert trust_env is False  # proxies from the environment are ignored, as in net.py
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(update_notice.httpx, "get", get)
    assert update_notice.release_notice("0.1.0") is None


def test_disabled_check_does_not_contact_github(monkeypatch):
    monkeypatch.setenv("ULTRON_DISABLE_UPDATE_CHECK", "1")

    def get(*args, **kwargs):
        pytest.fail("disabled update check attempted a network request")

    monkeypatch.setattr(update_notice.httpx, "get", get)
    assert update_notice.release_notice("0.1.0") is None


def test_recent_check_with_mtime_slightly_ahead_is_not_repeated(monkeypatch, tmp_path):
    calls = []

    def get(url, *, timeout, trust_env):
        assert trust_env is False  # proxies from the environment are ignored, as in net.py
        calls.append(url)
        return httpx.Response(200, json={"tag_name": "v0.10.0"}, request=httpx.Request("GET", url))

    monkeypatch.setenv("ULTRON_HOME", str(tmp_path))
    monkeypatch.setattr(update_notice.httpx, "get", get)
    checked = tmp_path / "update-notices" / "last-checked"
    checked.parent.mkdir()
    checked.touch()
    ahead = time.time() + 2
    os.utime(checked, (ahead, ahead))
    assert update_notice.release_notice("0.9.0") is None
    assert not calls

    far_future = time.time() + 3600
    os.utime(checked, (far_future, far_future))
    update_notice.release_notice("0.9.0")
    assert len(calls) == 1

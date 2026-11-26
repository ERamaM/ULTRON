import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from ultron.cli import main
from ultron.diagnostics import doctor
from ultron.infra import searxng
from ultron.infra.fs import atomic_create_bytes
from ultron.infra.net import HttpError


def test_exclusive_file(tmp_path):
    path = tmp_path / "secret"
    assert atomic_create_bytes(path, b"initial")
    assert not atomic_create_bytes(path, b"collision")
    assert path.read_bytes() == b"initial"
    assert sorted(tmp_path.iterdir()) == [path]


def test_local_json_health(monkeypatch):
    def read(origin, url, *, timeout):
        assert origin.host == "localhost"
        if url.endswith("format=json"):
            return 400, b'{"error":"No query"}'
        if url.endswith("/bad"):
            raise httpx.ReadTimeout("timeout")
        if url.endswith("/disabled"):
            return 403, b"<html>"
        if url.endswith("/array"):
            return 200, b"[]"
        return 200, b'{"results":[]}'

    monkeypatch.setattr(searxng, "read_loopback", read)
    origin = "http://localhost:8888/"
    monkeypatch.setenv("ULTRON_SEARXNG_URL", origin)
    assert searxng.json_health()
    assert searxng.read_json(origin, "/search", timeout=1) == {"results": []}
    with pytest.raises(HttpError, match="Local SearXNG request failed"):
        searxng.read_json(origin, "/bad", timeout=1)
    with pytest.raises(HttpError) as exc:
        searxng.read_json(origin, "/disabled", timeout=1)
    assert exc.value.status_code == 403
    with pytest.raises(ValueError, match="expected an object"):
        searxng.read_json(origin, "/array", timeout=1)
    assert not searxng.json_health("https://192.168.1.1")
    for base, path in (("https://user@example.org", "/search"), ("http://0.0.0.0:8888", "/search"),
                       ("https://example.org/?v=1", "/search"), (origin, "//example.org")):
        with pytest.raises(ValueError):
            searxng.read_json(base, path, timeout=1)


def test_remote_api_uses_public_http_client(monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["max_bytes"] <= 2_000_000

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_text(self, url, *, allowed_domains):
            assert url.startswith("https://search.example.org/search?")
            assert allowed_domains == ("search.example.org",)
            return 400, url, '{"error":"No query"}'

    monkeypatch.setattr(searxng, "HttpClient", Client)
    assert searxng.json_health("https://search.example.org")
    with pytest.raises(HttpError):
        searxng.read_json("https://search.example.org", "/search?q=x", timeout=1)


@pytest.fixture
def docker(monkeypatch, tmp_path):
    monkeypatch.setenv("ULTRON_HOME", str(tmp_path / "ULTRON state"))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("ULTRON_SEARXNG_PORT", raising=False)
    monkeypatch.setattr(searxng, "docker_executable", lambda: "docker")
    monkeypatch.setattr(searxng, "json_health", lambda url=None, **_options: bool(url and url.startswith("http://127.0.0.1:")))
    state = SimpleNamespace(issued=[], container=None, compose=True, remote=False, foreign=False, daemon=True)
    identity = "a" * 64

    def run(argv, *, env, shell, timeout, cwd, **kwargs):
        del kwargs
        assert argv[0] == "docker"
        assert shell is False and timeout > 0
        args = argv[1:]
        state.issued.append(args)
        result = SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "context":
            result.stdout = "ssh://other" if state.remote else "unix:///var/run/docker.sock"
        elif args[0] == "info":
            result.returncode = 0 if state.daemon else 1
        elif args[:2] == ["container", "ls"]:
            result.stdout = identity[:12] if state.container else ""
        elif args[:2] == ["container", "inspect"]:
            assert args[-1] == identity or args[-1] == identity[:12]
            if ".Config.Env" in args[3]:
                result.stdout = f"SEARXNG_SECRET={state.secret}\n"
            else:
                result.stdout = json.dumps(state.container)
        elif args[:2] == ["compose", "version"]:
            result.returncode = 0 if state.compose else 1
        elif args[0] in {"run", "compose"}:
            state.secret = env["SEARXNG_SECRET"]
            assert state.secret not in " ".join(args)
            assert "@sha256:" in searxng.IMAGE
            if args[0] == "run":
                binding = args[args.index("--publish") + 1]
                owner = args[args.index("--label") + 1].split("=")[1]
                assert "readonly" in args[args.index("--mount") + 1]
                assert searxng.IMAGE in args
                assert args[args.index("--cpus") + 1] == "1.0"
                assert args[args.index("--memory") + 1] == "512m"
                assert args[args.index("--pids-limit") + 1] == "128"
                assert "GRANIAN_BACKPRESSURE=12" in args
            else:
                settings = json.loads(Path(args[args.index("--file") + 1]).read_text(encoding="utf-8"))
                service = settings["services"]["searxng"]
                owner, = service["labels"].values()
                binding, = service["ports"]
                assert cwd == tmp_path / "ULTRON state" / "searxng"
                assert service["image"] == searxng.IMAGE
                assert service["cpus"] == 1.0
                assert service["mem_limit"] == "512m"
                assert service["pids_limit"] == 128
                assert "GRANIAN_BACKPRESSURE=12" in service["environment"]
                assert service["volumes"][0]["read_only"]
            assert binding.startswith("127.0.0.1:")
            host, port, _ = binding.split(":")
            state.container = [identity, "running", searxng.IMAGE, owner, {"8080/tcp": [
                {"HostIp": host, "HostPort": port},
            ]}]
        elif args[:2] == ["container", "rm"]:
            assert args[-1] == identity
            state.container = None
        elif args[:2] == ["container", "start"]:
            state.container[1] = "running"
        elif args[0] == "version":
            result.stdout = "29.0.0"
        elif args[0] == "logs":
            result.stdout = f"key {state.secret}"
        else:
            pytest.fail(str(args))
        return result

    monkeypatch.setattr(searxng.subprocess, "run", run)
    return state


@pytest.mark.parametrize("compose", [True, False])
def test_lifecycle(tmp_path, docker, compose, capsys):
    docker.compose = compose
    assert searxng.manage("status")["state"] == "absent"
    assert main(["searxng", "up", "--port", "8889"]) == 0
    root = tmp_path / "ULTRON state" / "searxng"
    assert '"json_healthy": true' in capsys.readouterr().out
    key = root / "secret_key"
    assert key.read_text() == docker.secret
    assert searxng.default_url() == "http://127.0.0.1:8889"
    if os.name != "nt":
        assert key.stat().st_mode & 0o777 == 0o600
    settings = (root / "config" / "settings.yml").read_text()
    config = yaml.safe_load(settings)
    assert config["search"]["formats"] == ["html", "json"]
    enabled = {entry["name"]: not entry["disabled"] for entry in config["engines"]}
    assert all(enabled[name] for name in ("bing", "brave", "yahoo", "google", "qwant"))
    assert enabled["duckduckgo"] is False
    assert set(enabled) == set(config["use_default_settings"]["engines"]["keep_only"])
    assert config["outgoing"] == {"request_timeout": 5.0, "max_request_timeout": 7.0}
    assert docker.secret not in settings
    with pytest.raises(searxng.LifecycleError, match="restart"):
        searxng.manage("up", port=8890)
    docker.container[1] = "exited"
    assert searxng.manage("up", port=8889)["json_healthy"]
    diagnosis = searxng.manage("diagnose")
    assert diagnosis["logs"] == "key [redacted]"
    assert diagnosis["docker_version"] == "29.0.0"
    old_key = docker.secret
    assert searxng.manage("restart", port=8890)["url"] == "http://127.0.0.1:8890"
    assert docker.secret == old_key
    assert searxng.default_url() == "http://127.0.0.1:8890"
    assert searxng.manage("down")["state"] == "absent"


def test_lifecycle_refuses_remote_and_foreign(docker, monkeypatch, capsys):
    docker.remote = True
    assert main(["searxng", "down"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "searxng_error"
    docker.remote = False
    searxng.manage("up")
    docker.container[3] = "foreign"
    with pytest.raises(searxng.LifecycleError, match="another installation"):
        searxng.manage("down")
    assert docker.container
    docker.container[3] = searxng._owner(searxng._managed_root())
    docker.container[4]["8080/tcp"][0]["HostIp"] = "0.0.0.0"
    with pytest.raises(searxng.LifecycleError, match="loopback"):
        searxng.manage("status")
    with pytest.raises(searxng.LifecycleError, match="65535"):
        searxng.configured_port(1000)
    monkeypatch.setenv("ULTRON_SEARXNG_PORT", "bad")
    with pytest.raises(searxng.LifecycleError, match="integer"):
        searxng.configured_port()


def test_docker_missing_and_doctor(monkeypatch):
    monkeypatch.setattr(searxng, "docker_available", lambda: False)
    assert not searxng.manage("status")["docker_available"]
    monkeypatch.setattr(doctor, "json_health", lambda: True)
    assert doctor.check_web_search(network=True).detail == "SearXNG JSON is available."
    monkeypatch.setattr(doctor, "docker_available", lambda: True)
    assert doctor.check_web_search(network=False).fix
    monkeypatch.setattr(doctor, "docker_available", lambda: False)
    assert doctor.check_web_search(network=False).ok
    monkeypatch.setattr(doctor, "ddgs_available", lambda: True)
    assert doctor.check_web_search(network=False).detail == "Optional ddgs search is available."


def test_status_only_probes_stopped_daemon(docker, monkeypatch, capsys):
    docker.daemon = False
    monkeypatch.setattr(searxng, "start_daemon", lambda *a, **k: pytest.fail("status started Docker"))
    assert main(["searxng", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["docker_available"] is True and payload["daemon_running"] is False
    assert payload["state"] == "unknown" and payload["json_healthy"] is False
    assert payload["start_hint"]
    assert searxng.manage("down") == payload


def test_up_starts_backend_and_polls_before_starting_container(docker, monkeypatch):
    docker.daemon = False
    launched = []

    def start(endpoint, *, timeout):
        assert endpoint.startswith("unix:///") and 0 < timeout <= 4
        launched.append(endpoint)
        return True

    def sleep(_):
        docker.daemon = True

    monkeypatch.setattr(searxng, "start_daemon", start)
    monkeypatch.setattr(searxng.time, "sleep", sleep)
    state = searxng.manage("up")
    assert launched and state["daemon_running"] and state["state"] == "running" and state["json_healthy"]
    docker.container[1] = "exited"
    assert searxng.manage("status")["state"] == "exited"
    assert not searxng.manage("status")["json_healthy"]


def test_up_reports_unreachable_daemon_after_bounded_start(docker, monkeypatch):
    docker.daemon = False
    monkeypatch.setattr(searxng, "_DAEMON_START_SECONDS", 0)
    monkeypatch.setattr(searxng, "start_daemon", lambda *a, **k: pytest.fail("startup budget was exhausted"))
    state = searxng.manage("up")
    assert state["daemon_running"] is False and state["start_hint"] and docker.container is None


def test_prepare_search_skips_override_and_respects_opt_out(docker, monkeypatch):
    monkeypatch.delenv("ULTRON_SEARXNG_URL", raising=False)
    assert searxng.prepare_search() == {}
    monkeypatch.setenv("ULTRON_SEARXNG_AUTO_START", "1")
    monkeypatch.setenv("ULTRON_SEARXNG_URL", "https://example.org")
    assert searxng.prepare_search() == {}
    assert not docker.issued
    monkeypatch.delenv("ULTRON_SEARXNG_URL", raising=False)
    state = searxng.prepare_search()
    assert state == {"docker_available": True, "daemon_running": True, "json_healthy": True}
    monkeypatch.setattr(searxng, "json_health", lambda *a, **k: True)
    assert searxng.prepare_search()["json_healthy"]


def test_prepare_search_reports_missing_or_unavailable_docker(monkeypatch):
    monkeypatch.delenv("ULTRON_SEARXNG_URL", raising=False)
    monkeypatch.setenv("ULTRON_SEARXNG_AUTO_START", "1")
    monkeypatch.setattr(searxng, "json_health", lambda *a, **k: False)
    monkeypatch.setattr(searxng, "docker_executable", lambda: None)
    state = searxng.prepare_search()
    assert state["docker_available"] is False and state["install_hint"]
    monkeypatch.setattr(searxng, "docker_executable", lambda: "docker")

    def fail(*_args, **_kwargs):
        raise searxng.LifecycleError("unavailable")

    monkeypatch.setattr(searxng, "manage", fail)
    assert searxng.prepare_search()["start_hint"]


def test_up_replaces_stale_managed_settings(tmp_path, docker):
    assert searxng.manage("up")["json_healthy"]
    settings = tmp_path / "ULTRON state" / "searxng" / "config" / "settings.yml"
    settings.write_text("old settings", encoding="utf-8")
    assert searxng.manage("up")["json_healthy"]
    assert "formats: [html, json]" in settings.read_text(encoding="utf-8")
    assert any(args[:2] == ["container", "rm"] for args in docker.issued)

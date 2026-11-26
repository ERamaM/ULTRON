"""Local SearXNG lifecycle and guarded HTTP access for JSON search."""

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ultron.infra.docker_runtime import docker_executable, install_hint, start_daemon, start_hint
from ultron.infra.fs import atomic_create_bytes, atomic_write_bytes, is_link
from ultron.infra.home import data_dir, ultron_home
from ultron.infra.loopback import explicit_loopback_origin, read_loopback
from ultron.infra.net import HttpClient, HttpError, RequestBudgetExceeded
from ultron.infra.urlpolicy import admit_url

IMAGE = ("ghcr.io/searxng/searxng:2026.11.23-a7163d091"
         "@sha256:a7fc607aa6723f5a8982105bcc5ffba4d1b9fc7c6922a31a88d6e4cba49ef12a")
CONTAINER = "ultron-searxng"
_OWNER_LABEL = "org.ultron.searxng.installation"
_CONTAINER_PORT = "8080/tcp"
_SECRET_VAR = "SEARXNG_SECRET"
_ID = re.compile(r"[0-9a-f]{12,64}")
_HEALTH_CHECK = "/search?format=json"
_DAEMON_START_SECONDS = 50.0
_SEARCH_START_SECONDS = 55.0
_RESOURCES = {"cpus": 1.0, "mem_limit": "512m", "pids_limit": 128}
_CONTAINER_ENV = (
    _SECRET_VAR, "FORCE_OWNERSHIP=false", "GRANIAN_WORKERS=1", "GRANIAN_BLOCKING_THREADS=4",
    "GRANIAN_BACKPRESSURE=12", "GRANIAN_BACKLOG=128", "GRANIAN_HTTP1_HEADER_READ_TIMEOUT=3000",
)


class LifecycleError(RuntimeError):
    """The local service could not be safely managed."""


def _saved_port() -> str:
    try:
        path = _managed_root() / "port"
        if not is_link(path) and path.is_file() and path.stat().st_size <= 6:
            with path.open("r", encoding="ascii") as file:
                return file.read(6).strip()
    except (OSError, ValueError, LifecycleError):
        return ""
    return ""


def configured_port(port: int | None = None) -> int:
    """Choose and validate the local published port."""
    try:
        value = int(os.environ.get("ULTRON_SEARXNG_PORT") or _saved_port() or "8888") if port is None else port
    except ValueError as error:
        raise LifecycleError("SearXNG port must be an integer from 1024 to 65535.") from error
    if not 1024 <= value <= 65535:
        raise LifecycleError("SearXNG port must be from 1024 to 65535.")
    return value


def default_url(port: int | None = None) -> str:
    """Return the local IPv4 origin for the published port."""
    return f"http://127.0.0.1:{configured_port(port)}"


def _read(base: str, path: str, timeout: float, deadline: float | None = None) -> tuple[int, dict]:
    local = explicit_loopback_origin(base)
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("SearXNG request path must be relative to its origin")
    url = base.rstrip("/") + path
    if local:
        try:
            status, payload = read_loopback(local, url, timeout=timeout)
        except httpx.HTTPError as error:
            raise HttpError("Local SearXNG request failed") from error
    else:
        parts = urlsplit(base)
        if (parts.scheme != "https" or parts.path not in ("", "/") or "?" in base or "#" in base
                or "\\" in base or any(char.isspace() or ord(char) < 32 for char in base)):
            raise ValueError("SearXNG URL must be an explicit loopback HTTP origin or public HTTPS origin")
        origin = admit_url(base)
        with HttpClient(max_requests=2, max_requests_per_host=2, max_bytes=2_000_000,
                        min_interval_per_host=0, block_threshold=1, timeout=timeout, deadline=deadline) as client:
            status, _, payload = client.get_text(url, allowed_domains=(origin.hostname,))
    if status not in {200, 400}:
        raise HttpError("SearXNG service error", status_code=status)
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Invalid SearXNG JSON response: expected an object")  # noqa: TRY004 - invalid response data
    return status, value


def read_json(base: str, path: str, *, timeout: float, deadline: float | None = None) -> dict:
    """Read a JSON result from the configured SearXNG origin."""
    status, value = _read(base, path, timeout, deadline)
    if status != 200:
        raise HttpError("SearXNG returned an error", status_code=status)
    return value


def json_health(base: str | None = None, *, timeout: float = 1.5) -> bool:
    """Check that the JSON API is enabled without making a metasearch query."""
    try:
        base = base or os.environ.get("ULTRON_SEARXNG_URL") or default_url()
        status, value = _read(base, _HEALTH_CHECK, timeout, time.monotonic() + timeout)
        return status == 400 and value.get("error") == "No query"
    except (ValueError, LifecycleError, HttpError, RequestBudgetExceeded):
        return False


def docker_available() -> bool:
    """Return whether the Docker CLI is installed."""
    return docker_executable() is not None


def _remaining(deadline: float | None, maximum: float) -> float:
    return maximum if deadline is None else min(maximum, max(0.0, deadline - time.monotonic()))


def _docker(*args: str, timeout: float = 15, secret: str | None = None, cwd: Path | None = None,
            deadline: float | None = None):
    timeout = _remaining(deadline, timeout)
    if timeout <= 0:
        raise LifecycleError("Docker startup reached its time limit.")
    env = os.environ.copy()
    env.pop(_SECRET_VAR, None)
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    if secret is not None:
        env[_SECRET_VAR] = secret
    try:
        return subprocess.run(
            [docker_executable() or "docker", *args], shell=False, check=False, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env, cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LifecycleError("Docker could not complete the operation.") from error


def _local_endpoint(deadline: float | None) -> str:
    # Restrict bind mounts and published ports to a daemon on this machine.
    override = os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None
    result = _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}", deadline=deadline) if not override else None
    endpoint = override or (result.stdout.strip() if result.returncode == 0 else "")
    if not endpoint.startswith(("unix:///", "npipe:////./pipe/")):
        raise LifecycleError("SearXNG requires a local Docker context.")
    return endpoint


def _daemon_running(deadline: float | None) -> bool:
    try:
        return _docker("info", "--format", "{{.ServerVersion}}", timeout=2, deadline=deadline).returncode == 0
    except LifecycleError:
        return False


def _start_and_wait(endpoint: str, deadline: float | None) -> bool:
    stop = min(time.monotonic() + _DAEMON_START_SECONDS, deadline if deadline is not None else float("inf"))
    if not (timeout := _remaining(stop, 4)):
        return False
    if not start_daemon(endpoint, timeout=timeout):
        return False
    while _remaining(stop, 1) > 0:
        if _daemon_running(stop):
            return True
        time.sleep(_remaining(stop, 1))
    return False


def _managed_root() -> Path:
    root = ultron_home().resolve() / "searxng"
    if is_link(root):
        raise LifecycleError("The SearXNG directory cannot be a link.")
    return root


def _stage(root: Path) -> tuple[Path, str]:
    config = root / "config"
    settings = config / "settings.yml"
    secret_path = root / "secret_key"
    if any(is_link(path) for path in (config, settings, secret_path)):
        raise LifecycleError("SearXNG settings and secret paths cannot be links.")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    atomic_create_bytes(secret_path, secret.encode("ascii"))
    return _stage_settings(settings), _read_secret(secret_path)


def _stage_settings(settings: Path) -> Path:
    atomic_write_bytes(settings, (data_dir() / "searxng" / "settings.yml").read_bytes())
    if os.name != "nt":
        # This file has no credentials and the image reads it as a non-root user.
        settings.chmod(0o644)
    return settings


def _read_secret(path: Path) -> str:
    if is_link(path):
        raise LifecycleError("The SearXNG secret path cannot be a link.")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 64:
        raise LifecycleError("Invalid SearXNG secret file.")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise LifecycleError("SearXNG secret permissions must be 0600.")
    secret = path.read_text(encoding="ascii")
    if not all(char in "0123456789abcdef" for char in secret):
        raise LifecycleError("Invalid SearXNG secret file.")
    return secret


def _owner(root: Path) -> str:
    return hashlib.sha256(os.fsencode(os.path.normcase(root))).hexdigest()


def _inspect(root: Path, deadline: float | None) -> dict | None:
    listed = _docker("container", "ls", "-aq", "--filter", f"name=^/{CONTAINER}$", deadline=deadline)
    if listed.returncode:
        raise LifecycleError("The Docker daemon is unavailable.")
    container_id = listed.stdout.strip()
    if not container_id:
        return None
    if not _ID.fullmatch(container_id):
        raise LifecycleError("Docker returned an invalid container identity.")
    fields = ('{{json .Id}}', '{{json .State.Status}}', '{{json .Config.Image}}',
              f'{{{{json (index .Config.Labels "{_OWNER_LABEL}")}}}}', '{{json .HostConfig.PortBindings}}')
    result = _docker("container", "inspect", "--format", "[" + ",".join(fields) + "]", container_id, deadline=deadline)
    try:
        full_id, status, image, owner, bindings = json.loads(result.stdout) if result.returncode == 0 else []
        if not _ID.fullmatch(full_id) or owner != _owner(root):
            raise LifecycleError("The existing SearXNG container belongs to another installation.")
        if bindings.keys() != {_CONTAINER_PORT}:
            raise LifecycleError("The SearXNG container has unexpected port bindings.")
        binding, = bindings[_CONTAINER_PORT]
        if binding["HostIp"] != "127.0.0.1":
            raise LifecycleError("The SearXNG container is not bound exclusively to loopback.")
        return {"id": full_id, "state": status, "image": image,
                "url": default_url(configured_port(int(binding["HostPort"])))}
    except (ValueError, AttributeError, TypeError, KeyError) as error:
        raise LifecycleError("Could not verify local SearXNG container ownership and port.") from error


def _launch(root: Path, port: int, deadline: float | None) -> None:
    settings, secret = _stage(root)
    owner = _owner(root)
    publish = f"127.0.0.1:{port}:8080"
    target = "/etc/searxng/settings.yml"
    compose = _docker("compose", "version", deadline=deadline)
    if compose.returncode == 0:
        # JSON is valid Compose YAML. Escape Compose interpolation in user-selected paths.
        config = {"services": {"searxng": {
            "image": IMAGE, "container_name": CONTAINER, "ports": [publish], "environment": _CONTAINER_ENV,
            **_RESOURCES,
            "labels": {_OWNER_LABEL: owner},
            "volumes": [{"type": "bind", "source": str(settings).replace("$", "$$"), "target": target,
                         "read_only": True, "bind": {"create_host_path": False}}],
        }}}
        path = root / "compose.json"
        atomic_write_bytes(path, json.dumps(config).encode("utf-8"))
        result = _docker("compose", "--project-name", f"ultron-searxng-{owner[:12]}", "--file", str(path),
                         "up", "--detach", timeout=180, secret=secret, cwd=root, deadline=deadline)
    else:
        source = str(settings)
        if "," in source or '"' in source:
            raise LifecycleError("SearXNG requires a Docker Compose install for this configuration path.")
        environment_args = [arg for value in _CONTAINER_ENV for arg in ("--env", value)]
        result = _docker("run", "--detach", "--name", CONTAINER, "--label", f"{_OWNER_LABEL}={owner}",
                         "--publish", publish, "--mount", f"type=bind,source={source},target={target},readonly",
                         "--cpus", str(_RESOURCES["cpus"]), "--memory", _RESOURCES["mem_limit"],
                         "--pids-limit", str(_RESOURCES["pids_limit"]),
                         *environment_args, IMAGE, timeout=180, secret=secret, deadline=deadline)
    if result.returncode:
        raise LifecycleError("Docker could not launch SearXNG. Verify the daemon, port and file sharing.")


def _remove(container: dict | None, deadline: float | None) -> None:
    if container and _docker("container", "rm", "--force", container["id"], timeout=30, deadline=deadline).returncode:
        raise LifecycleError("Docker could not remove the managed SearXNG container.")


def _wait(url: str, deadline: float | None) -> bool:
    stop = time.monotonic() + _remaining(deadline, 12)
    while timeout := _remaining(stop, 1.5):
        if json_health(url, timeout=timeout):
            return True
        time.sleep(_remaining(stop, 0.5))
    return False


def _diagnostics(root: Path, container: dict | None) -> dict:
    version = _docker("version", "--format", "{{.Client.Version}}")
    docker_version = version.stdout.strip()
    details = {"docker_version": docker_version if re.fullmatch(r"[0-9A-Za-z.+-]{1,50}", docker_version) else None}
    if container is None:
        return details
    try:
        secret = _read_secret(root / "secret_key")
    except (OSError, ValueError, LifecycleError):
        return details
    inspect_env = '{{range .Config.Env}}{{println .}}{{end}}'
    runtime = _docker("container", "inspect", "--format", inspect_env, container["id"])
    if runtime.returncode or f"{_SECRET_VAR}={secret}" not in runtime.stdout.splitlines():
        return details
    logs = _docker("logs", "--tail", "30", container["id"])
    if logs.returncode == 0:
        details["logs"] = (logs.stdout + logs.stderr).replace(secret, "[redacted]")[-8000:]
    return details


def _config_drift(root: Path) -> bool:
    settings = root / "config" / "settings.yml"
    if is_link(settings.parent) or is_link(settings):
        raise LifecycleError("SearXNG settings paths cannot be links.")
    return settings.is_file() and settings.read_bytes() != (data_dir() / "searxng" / "settings.yml").read_bytes()


def _bring_up(root: Path, container: dict | None, action: str, port: int | None, deadline: float | None) -> dict | None:
    desired = default_url(port)
    if action == "restart":
        _remove(container, deadline)
        container = None
    if container and (container["url"] != desired or container["image"] != IMAGE):
        raise LifecycleError("Run ultron searxng restart to apply the requested port or image.")
    if container and _config_drift(root):
        _remove(container, deadline)
        container = None
    if container is None:
        _launch(root, configured_port(port), deadline)
    elif (container["state"] != "running"
          and _docker("container", "start", container["id"], deadline=deadline).returncode):
        raise LifecycleError("Docker could not start the managed SearXNG container.")
    container = _inspect(root, deadline)
    if container and container["url"] == desired:
        atomic_write_bytes(root / "port", f"{urlsplit(desired).port}\n".encode("ascii"))
    return container


def manage(action: str, *, port: int | None = None, deadline: float | None = None) -> dict:
    """Report Docker and container state or perform a bounded lifecycle operation."""
    if not docker_available():
        return {"docker_available": False, "daemon_running": False, "state": "missing", "json_healthy": False,
                "install_hint": install_hint()}
    starting = action in {"up", "restart"}
    if starting:
        configured_port(port)
    endpoint = _local_endpoint(deadline)
    if not _daemon_running(deadline):
        if not starting or not _start_and_wait(endpoint, deadline):
            return {"docker_available": True, "daemon_running": False, "state": "unknown", "json_healthy": False,
                    "start_hint": start_hint()}
        _local_endpoint(deadline)
    root = _managed_root()
    container = _inspect(root, deadline)
    if action == "down":
        _remove(container, deadline)
        container = None
    elif starting:
        container = _bring_up(root, container, action, port, deadline)
    payload = {"docker_available": True, "daemon_running": True, "state": "absent", "json_healthy": False}
    if container:
        healthy = container["state"] == "running" and (
            _wait(container["url"], deadline) if starting else json_health(container["url"]))
        payload.update(container, json_healthy=healthy)
    if action == "diagnose":
        payload.update(_diagnostics(root, container))
    return payload


def prepare_search() -> dict:
    """Make the managed SearXNG service available when local auto-start is enabled."""
    if os.environ.get("ULTRON_SEARXNG_URL") or os.environ.get("ULTRON_SEARXNG_AUTO_START", "").lower() in {
        "0", "false", "no", "off",
    }:
        return {}
    available = docker_available()
    try:
        if json_health() and not _config_drift(_managed_root()):
            return {"docker_available": available, "json_healthy": True,
                    **({"install_hint": install_hint()} if not available else {})}
        state = manage("up", deadline=time.monotonic() + _SEARCH_START_SECONDS)
    except (LifecycleError, OSError, ValueError):
        return {"docker_available": available, "json_healthy": False,
                "start_hint": "Run ultron searxng status or ultron searxng up to finish local setup."}
    fields = ("docker_available", "daemon_running", "json_healthy", "install_hint", "start_hint")
    return {key: state[key] for key in fields if key in state}

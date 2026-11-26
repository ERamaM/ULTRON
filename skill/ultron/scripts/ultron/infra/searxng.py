"""Local SearXNG lifecycle and guarded HTTP access for JSON search."""

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

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


class LifecycleError(RuntimeError):
    """The local service could not be safely managed."""


def configured_port(port: int | None = None) -> int:
    """Choose and validate the local published port."""
    try:
        value = int(os.environ.get("ULTRON_SEARXNG_PORT", "8888")) if port is None else port
    except ValueError as error:
        raise LifecycleError("SearXNG port must be an integer from 1024 to 65535.") from error
    if not 1024 <= value <= 65535:
        raise LifecycleError("SearXNG port must be from 1024 to 65535.")
    return value


def default_url(port: int | None = None) -> str:
    """Return the local IPv4 origin for the published port."""
    return f"http://127.0.0.1:{configured_port(port)}"


def _read(base: str, path: str, timeout: float) -> tuple[int, dict]:
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
                        min_interval_per_host=0, block_threshold=1, timeout=timeout) as client:
            status, _, payload = client.get_text(url, allowed_domains=(origin.hostname,))
    if status not in {200, 400}:
        raise HttpError("SearXNG service error", status_code=status)
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Invalid SearXNG JSON response: expected an object")  # noqa: TRY004 - invalid response data
    return status, value


def read_json(base: str, path: str, *, timeout: float) -> dict:
    """Read a JSON result from the configured SearXNG origin."""
    status, value = _read(base, path, timeout)
    if status != 200:
        raise HttpError("SearXNG returned an error", status_code=status)
    return value


def json_health(base: str | None = None, *, timeout: float = 1.5) -> bool:
    """Check that the JSON API is enabled without making a metasearch query."""
    try:
        base = base or os.environ.get("ULTRON_SEARXNG_URL") or default_url()
        status, value = _read(base, _HEALTH_CHECK, timeout)
        return status == 400 and value.get("error") == "No query"
    except (ValueError, LifecycleError, HttpError, RequestBudgetExceeded):
        return False


def docker_available() -> bool:
    """Return whether the Docker CLI is installed."""
    return shutil.which("docker") is not None


def _docker(*args: str, timeout: int = 15, secret: str | None = None, cwd: Path | None = None):
    env = os.environ.copy()
    env.pop(_SECRET_VAR, None)
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    if secret is not None:
        env[_SECRET_VAR] = secret
    try:
        return subprocess.run(
            ["docker", *args], shell=False, check=False, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env, cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LifecycleError("Docker could not complete the operation.") from error


def _check_context() -> None:
    # Restrict bind mounts and published ports to a daemon on this machine.
    override = os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None
    result = _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}") if not override else None
    endpoint = override or (result.stdout.strip() if result.returncode == 0 else "")
    if not endpoint.startswith(("unix:///", "npipe:////./pipe/")):
        raise LifecycleError("SearXNG requires a local Docker context.")


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


def _inspect(root: Path) -> dict | None:
    listed = _docker("container", "ls", "-aq", "--filter", f"name=^/{CONTAINER}$")
    if listed.returncode:
        raise LifecycleError("The Docker daemon is unavailable.")
    container_id = listed.stdout.strip()
    if not container_id:
        return None
    if not _ID.fullmatch(container_id):
        raise LifecycleError("Docker returned an invalid container identity.")
    fields = ('{{json .Id}}', '{{json .State.Status}}', '{{json .Config.Image}}',
              f'{{{{json (index .Config.Labels "{_OWNER_LABEL}")}}}}', '{{json .HostConfig.PortBindings}}')
    result = _docker("container", "inspect", "--format", "[" + ",".join(fields) + "]", container_id)
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


def _launch(root: Path, port: int) -> None:
    settings, secret = _stage(root)
    owner = _owner(root)
    publish = f"127.0.0.1:{port}:8080"
    target = "/etc/searxng/settings.yml"
    env = [_SECRET_VAR, "FORCE_OWNERSHIP=false"]
    compose = _docker("compose", "version")
    if compose.returncode == 0:
        # JSON is valid Compose YAML. Escape Compose interpolation in user-selected paths.
        config = {"services": {"searxng": {
            "image": IMAGE, "container_name": CONTAINER, "ports": [publish], "environment": env,
            "labels": {_OWNER_LABEL: owner},
            "volumes": [{"type": "bind", "source": str(settings).replace("$", "$$"), "target": target,
                         "read_only": True, "bind": {"create_host_path": False}}],
        }}}
        path = root / "compose.json"
        atomic_write_bytes(path, json.dumps(config).encode("utf-8"))
        result = _docker("compose", "--project-name", f"ultron-searxng-{owner[:12]}", "--file", str(path),
                         "up", "--detach", timeout=180, secret=secret, cwd=root)
    else:
        source = str(settings)
        if "," in source or '"' in source:
            raise LifecycleError("SearXNG requires a Docker Compose install for this configuration path.")
        result = _docker("run", "--detach", "--name", CONTAINER, "--label", f"{_OWNER_LABEL}={owner}",
                         "--publish", publish, "--mount", f"type=bind,source={source},target={target},readonly",
                         "--env", env[0], "--env", env[1], IMAGE, timeout=180, secret=secret)
    if result.returncode:
        raise LifecycleError("Docker could not launch SearXNG. Verify the daemon, port and file sharing.")


def _remove(container: dict | None) -> None:
    if container and _docker("container", "rm", "--force", container["id"], timeout=30).returncode:
        raise LifecycleError("Docker could not remove the managed SearXNG container.")


def _wait(url: str) -> bool:
    for _ in range(20):
        if json_health(url):
            return True
        time.sleep(0.5)
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


def manage(action: str, *, port: int | None = None) -> dict:
    """Perform one lifecycle operation on the installation's local service."""
    if not docker_available():
        return {"docker_available": False, "state": "missing", "json_healthy": False}
    _check_context()
    root = _managed_root()
    container = _inspect(root)
    if action == "down":
        _remove(container)
        container = None
    elif action in {"up", "restart"}:
        desired = default_url(port)
        if action == "restart":
            _remove(container)
            container = None
        if container and (container["url"] != desired or container["image"] != IMAGE):
            raise LifecycleError("Run ultron searxng restart to apply the requested port or image.")
        if container is None:
            _launch(root, configured_port(port))
        elif container["state"] != "running" and _docker("container", "start", container["id"]).returncode:
            raise LifecycleError("Docker could not start the managed SearXNG container.")
        container = _inspect(root)
    payload = {"docker_available": True, "state": "absent", "json_healthy": False}
    if container:
        probe = _wait if action in {"up", "restart"} else json_health
        payload.update(container, json_healthy=container["state"] == "running" and probe(container["url"]))
    if action == "diagnose":
        payload.update(_diagnostics(root, container))
    return payload

import subprocess
from types import SimpleNamespace

import pytest

from ultron.infra import docker_runtime


def test_windows_program_files_and_user_install(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_runtime.sys, "platform", "win32")
    monkeypatch.setattr(docker_runtime.shutil, "which", lambda _: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert docker_runtime.docker_executable() is None
    assert not docker_runtime.start_daemon("npipe:////./pipe/docker_engine", timeout=3)
    app = tmp_path / "Local" / "Programs" / "DockerDesktop"
    cli = app / "resources" / "bin" / "docker.exe"
    cli.parent.mkdir(parents=True)
    cli.touch()
    executable = app / "Docker Desktop.exe"
    executable.touch()
    assert docker_runtime.docker_executable() == str(cli)
    calls = []

    def spawn(argv, **options):
        calls.append(argv)
        assert options["shell"] is False
        assert options["stdin"] == subprocess.DEVNULL

    monkeypatch.setattr(docker_runtime.subprocess, "Popen", spawn)
    assert docker_runtime.start_daemon("npipe:////./pipe/docker_engine", timeout=3)
    assert calls == [[str(executable)]]
    assert "winget install Docker.DockerDesktop" in docker_runtime.install_hint()
    assert "WSL 2" in docker_runtime.install_hint()


def test_mac_uses_open_without_shell(monkeypatch):
    monkeypatch.setattr(docker_runtime.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(docker_runtime.subprocess, "Popen", lambda argv, **opts: calls.append((argv, opts["shell"])))
    assert docker_runtime.start_daemon("unix:///var/run/docker.sock", timeout=3)
    assert calls == [(["/usr/bin/open", "-a", "Docker"], False)]
    assert "brew install --cask docker" in docker_runtime.install_hint()
    assert "Desktop" in docker_runtime.start_hint()


@pytest.mark.parametrize(("endpoint", "options", "unit"), [
    ("unix:///var/run/docker.sock", [], "docker"),
    ("unix:///run/user/1000/docker.sock", ["--user"], "docker"),
    ("unix:///home/demo/.docker/desktop/docker.sock", ["--user"], "docker-desktop"),
])
def test_linux_starts_the_configured_service_noninteractively(monkeypatch, endpoint, options, unit):
    monkeypatch.setattr(docker_runtime.sys, "platform", "linux")
    monkeypatch.setattr(docker_runtime.shutil, "which", lambda _: "/usr/bin/docker")
    assert docker_runtime.docker_executable() == "/usr/bin/docker"

    def run(argv, **kwargs):
        assert argv == ["systemctl", *options, "--no-ask-password", "--no-block", "start", unit]
        assert kwargs["timeout"] == 3 and kwargs["shell"] is False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(docker_runtime.subprocess, "run", run)
    assert docker_runtime.start_daemon(endpoint, timeout=3)
    assert "Docker Engine" in docker_runtime.install_hint() and "service" in docker_runtime.start_hint()


def test_start_reports_platform_and_process_failures(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("systemctl", 1)

    monkeypatch.setattr(docker_runtime.sys, "platform", "freebsd")
    assert not docker_runtime.start_daemon("unix:///var/run/docker.sock", timeout=1)
    monkeypatch.setattr(docker_runtime.sys, "platform", "linux")
    monkeypatch.setattr(docker_runtime.subprocess, "run", timeout)
    assert not docker_runtime.start_daemon("unix:///var/run/docker.sock", timeout=1)

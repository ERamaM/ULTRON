"""Exercise Windows bootstrap routing with offline PowerShell stubs."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell required")


@pytest.mark.parametrize("flag", ["--dev", "--de"])
@pytest.mark.parametrize("local", [False, True], ids=["download", "checkout"])
def test_dev_bootstrap_requires_a_persistent_source(tmp_path, flag, local):
    bootstrap = tmp_path / "install.ps1"
    shutil.copyfile(ROOT / "install.ps1", bootstrap)
    if local:
        (tmp_path / "install.py").touch()
        skill = tmp_path / "skill" / "ultron"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").touch()
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text(
        """
function Invoke-WebRequest { throw 'unexpected download from offline fixture' }
function Invoke-TestUv {
    @{passed = @($args)} | ConvertTo-Json -Compress
    $global:LASTEXITCODE = 0
}
function Get-Command {
    [pscustomobject]@{CommandType = 'Application'; Source = 'Invoke-TestUv'}
}
& (Join-Path $PSScriptRoot 'install.ps1') """ + flag + " --copy\nexit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
    )
    assert "unexpected download from offline fixture" not in result.stderr
    payload = json.loads(result.stdout)
    if local:
        assert result.returncode == 0
        assert payload["passed"][-2:] == [flag, "--copy"]
    else:
        assert result.returncode == 2
        assert payload["error"] == "usage"
        assert "checkout" in payload["detail"]

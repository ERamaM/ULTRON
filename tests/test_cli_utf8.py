"""Non-ASCII titles survive the CLI's stdout on every OS (Review Focus 5)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import yaml

from ultron import home, profiles, runs

TITLE = "Señor Ingeniero \U0001f680"


def test_cli_outputs_utf8():
    source = home.data_dir() / "profiles" / f"{profiles.EXAMPLE_SLUG}.yaml"
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    data.update({"slug": "senor", "name": TITLE})
    data["criteria"]["target_titles"] = [TITLE]
    (home.ultron_home() / "profiles" / "senor.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )
    run_id = runs.new_run("senor", "fast")

    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONUTF8", "PYTHONIOENCODING"}}
    env["PYTHONIOENCODING"] = "ascii"  # hostile default: the CLI must force UTF-8 itself
    proc = subprocess.run(
        [sys.executable, "-m", "ultron.cli", "runs", "show", run_id], capture_output=True, env=env, check=False
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    out = proc.stdout.decode("utf-8")
    assert TITLE in out
    assert json.loads(out)["run"]["profile_snapshot"]["name"] == TITLE

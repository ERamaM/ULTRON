"""Make the developer tools under ``tools/`` importable by the eval tests."""

import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


@pytest.fixture(autouse=True)
def reports_dir(tmp_path, monkeypatch):
    """Send every report the tools write into this test's own folder."""
    import safe_paths

    folder = tmp_path / "reports"
    monkeypatch.setattr(safe_paths, "REPORTS_DIR", str(folder))
    return folder

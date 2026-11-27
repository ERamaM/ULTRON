import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_tag_guard_precedes_wheel_and_publication():
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    guard = workflow.index("tools/check_release_version.py")
    assert guard < workflow.index("uv build --wheel")
    assert guard < workflow.index("actions/upload-artifact")
    assert guard < workflow.index("gh release create")
    assert "if: startsWith(github.ref, 'refs/tags/')" in workflow[:guard]


def test_tag_matches_both_version_declarations():
    import tomllib

    package = ROOT / "skill" / "ultron" / "scripts"
    with (package / "pyproject.toml").open("rb") as config:
        version = tomllib.load(config)["project"]["version"]
    main = runpy.run_path(str(ROOT / "tools" / "check_release_version.py"))["main"]
    assert main([f"v{version}"]) == 0
    assert main(["v9999.0.0"]) == 1


def test_tag_rejects_source_with_only_the_runtime_version_mismatched(tmp_path):
    import shutil

    tools = tmp_path / "tools"
    tools.mkdir()
    script = tools / "check_release_version.py"
    shutil.copyfile(ROOT / "tools" / script.name, script)
    package = tmp_path / "skill" / "ultron" / "scripts"
    (package / "ultron").mkdir(parents=True)
    (package / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    (package / "ultron" / "__init__.py").write_text('__version__ = "1.2.4"\n', encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), "v1.2.3"], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "runtime version '1.2.4'" in result.stderr

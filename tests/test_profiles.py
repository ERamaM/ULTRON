import json
from pathlib import Path

import yaml

from ultron.cli import main
from ultron.infra import home
from ultron.profiles import store as profiles

FIXTURE = Path(__file__).parent / "fixtures" / "profiles" / "ai-ml-remote-eu.yaml"


def _example() -> dict:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def test_example_profile_is_valid():
    assert profiles.validate_profile(_example()) == []


def test_missing_titles_is_invalid():
    data = _example()
    data["criteria"].pop("target_titles")
    data["criteria"].pop("role_focus_terms")
    errors = profiles.validate_profile(data)
    assert any("target_titles" in e for e in errors)


def test_bad_slug_is_invalid():
    data = _example()
    data["slug"] = "Bad Slug"
    assert profiles.validate_profile(data)


def test_bad_remote_policy_and_term_lists():
    data = _example()
    data["criteria"]["remote_policy"]["required"] = "sometimes"
    data["criteria"]["exclusion_terms"] = ["ok", 3]
    errors = profiles.validate_profile(data)
    assert len(errors) == 2


def test_home_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ULTRON_HOME", str(tmp_path / "h"))
    assert home.ultron_home() == tmp_path / "h"
    for sub in ("profiles", "seeds", "runs"):
        assert (tmp_path / "h" / sub).is_dir()


def test_data_dir_has_domain_rules():
    assert (home.data_dir() / "domain_rules.yaml").is_file()
    from ultron.core.data_paths import data_dir

    assert data_dir() == home.data_dir()


def test_install_and_load_roundtrip():
    path = profiles.install_example()
    assert path == home.ultron_home() / "profiles" / "ai-ml-remote-eu.yaml"
    assert profiles.load_profile("ai-ml-remote-eu")["slug"] == "ai-ml-remote-eu"
    assert [p["slug"] for p in profiles.list_profiles()] == ["ai-ml-remote-eu"]


def test_load_falls_back_to_bundled_and_user_wins():
    assert profiles.load_profile("ai-ml-remote-eu")["name"].startswith("AI/ML")
    user = home.ultron_home() / "profiles" / "ai-ml-remote-eu.yaml"
    data = _example()
    data["name"] = "Mine"
    user.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert profiles.load_profile("ai-ml-remote-eu")["name"] == "Mine"
    assert len(profiles.list_profiles()) == 1


def test_load_missing_profile_raises():
    import pytest

    with pytest.raises(FileNotFoundError):
        profiles.load_profile("ghost")
    with pytest.raises(FileNotFoundError):
        profiles.load_profile("../evil")


def test_cli_profile_commands(capsys):
    assert main(["profile", "install-example"]) == 0
    assert json.loads(capsys.readouterr().out)["installed"].endswith("ai-ml-remote-eu.yaml")
    assert main(["profile", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["profiles"][0]["slug"] == "ai-ml-remote-eu"
    assert main(["profile", "validate", "ai-ml-remote-eu"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert main(["profile", "show", "ai-ml-remote-eu"]) == 0
    assert json.loads(capsys.readouterr().out)["profile"]["slug"] == "ai-ml-remote-eu"
    assert main(["profile", "show", "ghost"]) != 0
    assert "error" in json.loads(capsys.readouterr().out)


def test_profile_cmd_registered_and_bad_subcommand(capsys):
    from ultron import cli
    from ultron.commands import profile as profile_cmd

    assert cli.COMMAND_MODULES[0] is profile_cmd
    assert main(["profile", "bogus"]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_unexpected_exception_still_returns_json(capsys):
    # I2: a malformed user profile must not break the one-JSON-object stdout contract.
    user = home.ultron_home() / "profiles" / "broken.yaml"
    user.write_text("slug: broken\nname: [unclosed\n", encoding="utf-8")
    assert main(["profile", "show", "broken"]) == 1
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["error"] == "internal_error"
    assert out["detail"].split(":")[0].endswith("Error")  # "<ExceptionType>: <message>"
    assert "Traceback" in captured.err


def test_slug_rejects_trailing_newline():
    data = _example()
    data["slug"] = "foo\n"
    assert any("slug" in e for e in profiles.validate_profile(data))
    import pytest

    with pytest.raises(FileNotFoundError):
        profiles.load_profile("ai-ml-remote-eu\n")


def test_install_example_refuses_to_overwrite_a_changed_profile(capsys):
    # I5
    path = profiles.install_example()
    assert profiles.install_example() == path  # identical copy: nothing to overwrite
    path.write_text(path.read_text(encoding="utf-8").replace("AI/ML", "My edits"), encoding="utf-8")
    assert main(["profile", "install-example"]) != 0
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "profile_exists" and "--force" in out["detail"]
    assert "My edits" in path.read_text(encoding="utf-8")
    assert main(["profile", "install-example", "--force"]) == 0
    capsys.readouterr()
    assert "My edits" not in path.read_text(encoding="utf-8")


def test_cli_profile_validate_from_file(tmp_path, capsys):
    valid_yaml = tmp_path / "draft.yaml"
    valid_yaml.write_text(yaml.safe_dump(_example(), allow_unicode=True), encoding="utf-8")
    assert main(["profile", "validate", "--from", str(valid_yaml)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"slug": "ai-ml-remote-eu", "valid": True, "errors": []}

    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("slug: Bad Slug\n", encoding="utf-8")
    assert main(["profile", "validate", "--from", str(bad_yaml)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["valid"] is False and out["errors"]

    assert main(["profile", "validate", "--from", str(tmp_path / "missing.yaml")]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "profile_unreadable"

    assert main(["profile", "validate"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "usage"
    assert main(["profile", "validate", "ai-ml-remote-eu", "--from", str(valid_yaml)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "usage"


def test_scalar_term_lists_are_invalid():
    # A bare string used to validate, then every code path iterated it character by character:
    # single-letter role terms that match any posting and a query plan made of letters.
    data = _example()
    data["criteria"]["role_focus_terms"] = "backend engineer"
    data["criteria"]["seniority"]["excluded_terms"] = "junior"
    data["query_templates"]["core_titles"] = "Senior Backend Engineer"
    errors = profiles.validate_profile(data)
    assert any("criteria.role_focus_terms" in e and "list" in e for e in errors)
    assert any("criteria.seniority.excluded_terms" in e for e in errors)
    assert any("query_templates.core_titles" in e for e in errors)


def test_query_template_lists_must_hold_strings():
    data = _example()
    data["query_templates"]["regions"] = ["Remote EU", 3]
    assert any("query_templates.regions" in e for e in profiles.validate_profile(data))


def test_profile_sections_must_be_mappings():
    data = _example()
    data["criteria"]["seniority"] = "senior"
    data["criteria"]["remote_policy"] = "full_remote"
    data["criteria"]["location_scope"] = ["Spain", "Remote EU"]
    data["query_templates"] = ["Senior ML Engineer"]
    errors = profiles.validate_profile(data)
    for section in ("criteria.seniority", "criteria.remote_policy", "criteria.location_scope", "query_templates"):
        assert any(e.startswith(section) and "mapping" in e for e in errors), section


def test_flags_must_be_booleans():
    data = _example()
    data["criteria"]["seniority"]["required"] = "yes"
    data["enabled"] = "no"
    errors = profiles.validate_profile(data)
    assert any(e.startswith("criteria.seniority.required") for e in errors)
    assert any(e.startswith("enabled") for e in errors)


def test_empty_term_lists_and_sections_stay_valid():
    data = _example()
    data["criteria"]["exclusion_terms"] = None
    data["criteria"]["specialties"] = []
    data["criteria"]["remote_policy"] = None
    data["query_templates"] = None
    assert profiles.validate_profile(data) == []


def test_bundled_and_fixture_profiles_stay_valid():
    folders = (home.data_dir() / "profiles", FIXTURE.parent)
    for path in (p for folder in folders for p in sorted(folder.glob("*.yaml"))):
        assert profiles.validate_profile(yaml.safe_load(path.read_text(encoding="utf-8"))) == [], path.name

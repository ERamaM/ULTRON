"""Static checks on the installable skill: frontmatter, references, portable wording, valid examples."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from ultron.core.models import JobEvaluation
from ultron.profiles.store import validate_profile
from ultron.salary.estimator import SalaryEstimate
from ultron.seeds import Proposal

SKILL = Path(__file__).resolve().parents[1] / "skill" / "ultron"
SKILL_MD = SKILL / "SKILL.md"
REFERENCES = SKILL / "references"
EXPECTED_REFERENCES = {
    "search-workflow.md",
    "evaluation.md",
    "profile.md",
    "seeds.md",
    "commands.md",
    "salary.md",
    "cv-ats.md",
    "cover-letter.md",
}


def _frontmatter(text: str) -> dict:
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    assert match, "SKILL.md must start with a YAML frontmatter block"
    return yaml.safe_load(match.group(1))


def _docs() -> list[Path]:
    return [SKILL_MD, *sorted(REFERENCES.glob("*.md"))]


def _jsonl_rows(path: Path) -> list[dict]:
    """Every line of every ```jsonl fenced block, parsed."""
    blocks = re.findall(r"```jsonl\r?\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL)
    return [json.loads(line) for block in blocks for line in block.splitlines() if line.strip()]


def test_skill_frontmatter():
    meta = _frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    assert set(meta) == {"name", "description"}
    assert meta["name"] == "ultron"
    assert isinstance(meta["description"], str) and meta["description"].strip()
    assert len(meta["description"]) <= 1024


def test_skill_references_exist():
    cited = set(re.findall(r"references/([A-Za-z0-9_.-]+\.md)", SKILL_MD.read_text(encoding="utf-8")))
    assert EXPECTED_REFERENCES <= cited
    for name in cited:
        assert (REFERENCES / name).is_file(), f"SKILL.md cites missing references/{name}"


def test_skill_mentions_no_platform_tools():
    for path in _docs():
        text = path.read_text(encoding="utf-8")
        for banned in ("WebSearch", "WebFetch", "TodoWrite", "Bash("):
            assert banned not in text, f"{path.name} names platform tool {banned!r}"


def test_evaluation_examples_are_valid_records():
    records = _jsonl_rows(REFERENCES / "evaluation.md")
    assert len(records) >= 2
    verdicts = set()
    for record in records:
        assert set(record) == {"identity_key", "evaluation"}
        evaluation = JobEvaluation.model_validate(record["evaluation"])
        verdicts.add(evaluation.verdict)
    assert {"keep", "discard"} <= verdicts


def test_seed_proposal_examples_match_proposal_fields():
    rows = _jsonl_rows(REFERENCES / "seeds.md")
    assert rows
    for row in rows:
        Proposal(**row)  # unknown keys raise TypeError
        assert row["company"]


def test_candidate_examples_have_url_title_company_query():
    rows = _jsonl_rows(REFERENCES / "search-workflow.md")
    assert rows
    for row in rows:
        assert set(row) <= {"url", "title", "company", "query"}
        assert row["url"].startswith("https://")


def test_profile_schema_example_is_valid():
    text = (REFERENCES / "profile.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\r?\n(.*?)```", text, re.DOTALL)
    assert blocks
    for block in blocks:
        assert validate_profile(yaml.safe_load(block)) == []


def test_skill_install_advice_and_quoting():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "install.py --dev" not in text
    assert "install.py` (global)" in text
    assert '& "' in text  # PowerShell call operator for a quoted executable path
    assert "unsure" in text


def _stage_commands() -> set[str]:
    """Every registered stage command as the words a user types, e.g. ``discover feeds``."""
    import argparse

    from ultron.cli import build_parser

    def walk(parser: argparse.ArgumentParser, prefix: str) -> set[str]:
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs:
            return {prefix}
        out: set[str] = set()
        for action in subs:
            for name, child in action.choices.items():
                out |= walk(child, f"{prefix} {name}".strip())
        return out

    top = walk(build_parser(), "")
    return {c for c in top if c.split()[0] in {"discover", "candidates", "fetch", "filter", "evaluate", "finalize"}}


def test_references_mention_all_stage_commands():
    commands = _stage_commands()
    assert {"discover ats", "discover feeds", "fetch", "finalize", "evaluate record"} <= commands
    text = (REFERENCES / "search-workflow.md").read_text(encoding="utf-8")
    missing = sorted(c for c in commands if f"ultron {c}" not in text)
    assert not missing, f"search-workflow.md does not mention: {missing}"


def _all_cli_commands() -> set[str]:
    """Every leaf command as the words a user types, e.g. ``jobs mark``."""
    import argparse

    from ultron.cli import build_parser

    def walk(parser: argparse.ArgumentParser, prefix: str) -> set[str]:
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs:
            return {prefix}
        out: set[str] = set()
        for action in subs:
            for name, child in action.choices.items():
                out |= walk(child, f"{prefix} {name}".strip())
        return out

    return walk(build_parser(), "")


# `version` only prints the ULTRON version; there is nothing for the agent to learn about it.
UNDOCUMENTED_OK = {"version"}


def test_every_cli_command_documented():
    commands = _all_cli_commands() - UNDOCUMENTED_OK
    assert {"jobs mark", "saved-search run", "diagnose", "data purge", "export xlsx", "cv extract"} <= commands
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in _docs())
    missing = sorted(c for c in commands if f"ultron {c}" not in corpus)
    assert not missing, f"commands not documented in SKILL.md or references: {missing}"


def test_diagnose_and_workflow_doc_notes():
    diagnose = (REFERENCES / "diagnose.md").read_text(encoding="utf-8")
    assert "saved-search create --override" in diagnose
    assert "run new --override" not in diagnose
    workflow = (REFERENCES / "search-workflow.md").read_text(encoding="utf-8")
    assert "always 0" in workflow and "default presets" in workflow


def test_commands_reference_covers_every_group_and_action():
    text = (REFERENCES / "commands.md").read_text(encoding="utf-8")
    skill = SKILL_MD.read_text(encoding="utf-8")
    assert "`references/commands.md`" in skill
    groups = {
        "search": ["quick", "saved"],
        "profile": ["show", "cv", "companies"],
        "jobs": ["new", "mark", "why"],
        "runs": ["show", "excel"],
        "system": ["data"],
    }
    for group, actions in groups.items():
        assert f"### {group}" in text, f"commands.md lacks a section for {group}"
        for action in actions:
            assert f"{group} {action}" in text, f"commands.md lacks `{group} {action}`"
    for cli in ("ultron run --unattended", "ultron seeds list", "ultron jobs new-since", "ultron diagnose",
                "ultron export xlsx", "ultron doctor", "ultron profile list",
                "ultron salary analyze", "ultron salary save", "ultron cv audit", "ultron cv build-docx",
                "ultron cover-letter prepare", "ultron cover-letter build-docx"):
        assert cli in text, f"commands.md does not map to {cli}"


def test_salary_reference_json_example_is_valid():
    text = (REFERENCES / "salary.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\r?\n(.*?)```", text, re.DOTALL)
    assert blocks
    for block in blocks:
        estimate = SalaryEstimate.model_validate(json.loads(block))
        assert estimate.low <= estimate.median <= estimate.high

"""``ultron profile show|edit|history|undo|explain`` and ``save --dry-run``: reviewable, undoable profile changes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ultron.cli import main
from ultron.infra import home
from ultron.profiles import edit as profile_edit
from ultron.profiles import store as profiles
from ultron.storage import runs

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"
SLUG = "backend-chat"


def _run(capsys, *argv: str) -> tuple[int, dict]:
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _draft(**overrides) -> dict:
    data = {
        "slug": SLUG,
        "name": "Senior Backend Engineer - Remote EU",
        "criteria": {
            "role_focus_terms": ["backend engineer", "software engineer"],
            "target_titles": ["Senior Backend Engineer"],
            "seniority": {"required": True, "preferred_terms": ["senior"], "excluded_terms": ["intern"]},
            "location_scope": {"preferred": ["Spain", "Remote EU"], "eligibility_terms": ["spain", "remote eu"]},
            "exclusion_terms": ["data entry"],
            "specialties": ["Go", "Kafka"],
        },
        "query_templates": {"core_titles": ["Senior Backend Engineer"], "regions": ["Remote EU"]},
    }
    data.update(overrides)
    return data


def _save_draft(tmp_path, capsys, data: dict | None = None) -> Path:
    source = tmp_path / "draft.json"
    source.write_text(json.dumps(data or _draft(), ensure_ascii=False), encoding="utf-8")
    code, out = _run(capsys, "profile", "save", "--from", str(source))
    assert code == 0, out
    return Path(out["saved"])


def _saved() -> dict:
    return yaml.safe_load(profiles.user_path(SLUG).read_text(encoding="utf-8"))


# --- show --------------------------------------------------------------------------------------------


def test_show_reports_origin_and_path(capsys, tmp_path):
    code, out = _run(capsys, "profile", "show", "ai-ml-remote-eu")
    assert code == 0 and out["origin"] == "bundled" and out["profile"]["slug"] == "ai-ml-remote-eu"
    path = _save_draft(tmp_path, capsys)
    code, out = _run(capsys, "profile", "show", SLUG)
    assert (out["origin"], out["path"]) == ("user", str(path))


def test_existing_profiles_still_load_and_validate_unchanged():
    for path in FIXTURES.glob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert profiles.validate_profile(data) == [], path.name


# --- save: JSON drafts, dry run, history ---------------------------------------------------------------


def test_save_accepts_a_json_draft_and_dry_run_writes_nothing(tmp_path, capsys):
    source = tmp_path / "draft.json"
    source.write_text(json.dumps(_draft(), ensure_ascii=False), encoding="utf-8")
    code, out = _run(capsys, "profile", "save", "--from", str(source), "--dry-run")
    assert code == 0
    assert out["dry_run"] is True and out["exists"] is False and out["needs_force"] is False
    assert "+slug: backend-chat" in out["diff"]
    assert not profiles.user_path(SLUG).exists()
    _save_draft(tmp_path, capsys)
    code, out = _run(capsys, "profile", "save", "--from", str(source), "--dry-run")
    assert out["exists"] is True and out["needs_force"] is True and out["diff"] == ""


def test_save_dry_run_reports_invalid_profile(tmp_path, capsys):
    source = tmp_path / "bad.json"
    source.write_text(json.dumps({"slug": "Bad Slug", "name": "x", "criteria": {}}), encoding="utf-8")
    code, out = _run(capsys, "profile", "save", "--from", str(source), "--dry-run")
    assert code == 1 and out["error"] == "profile_invalid" and out["errors"]


def test_save_force_keeps_the_previous_version(tmp_path, capsys):
    _save_draft(tmp_path, capsys)
    original = profiles.user_path(SLUG).read_bytes()
    source = tmp_path / "v2.json"
    source.write_text(json.dumps(_draft(name="Second version")), encoding="utf-8")
    assert _run(capsys, "profile", "save", "--from", str(source), "--force")[0] == 0
    _code, out = _run(capsys, "profile", "history", SLUG)
    assert [v["reason"] for v in out["versions"]] == ["save", "save"]
    assert out["versions"][1]["exists"] is False  # the first save created the profile
    assert _run(capsys, "profile", "undo", SLUG)[0] == 0
    assert profiles.user_path(SLUG).read_bytes() == original


# --- edit ----------------------------------------------------------------------------------------------


def test_edit_add_remove_set_unset_in_order(tmp_path, capsys):
    _save_draft(tmp_path, capsys)
    code, out = _run(
        capsys, "profile", "edit", SLUG,
        "--add", "criteria.specialties=Rust",
        "--add", "query_templates.technologies=Rust",
        "--remove", "criteria.exclusion_terms=Data Entry",
        "--add", "criteria.exclusion_terms=manager",
        "--set", "criteria.remote_policy.required=any",
        "--set", 'criteria.location_scope.preferred=["Valencia", "Remote EU"]',
        "--unset", "query_templates.regions",
    )
    assert code == 0, out
    statuses = [(c["op"], c["path"], c["status"]) for c in out["changes"]]
    assert statuses[:3] == [
        ("add", "criteria.specialties", "added"),
        ("add", "query_templates.technologies", "added"),
        ("remove", "criteria.exclusion_terms", "removed"),
    ]
    assert out["changes"][2]["matched"] == ["data entry"]
    saved = _saved()
    assert saved["criteria"]["specialties"] == ["Go", "Kafka", "Rust"]
    assert saved["query_templates"]["technologies"] == ["Rust"]
    assert saved["criteria"]["exclusion_terms"] == ["manager"]
    assert saved["criteria"]["remote_policy"] == {"required": "any"}
    assert saved["criteria"]["location_scope"]["preferred"] == ["Valencia", "Remote EU"]
    assert "regions" not in saved["query_templates"]
    assert out["saved"] == str(profiles.user_path(SLUG)) and out["undo_version"]
    assert "+  - Rust" in out["diff"]


def test_edit_dry_run_previews_without_writing(tmp_path, capsys):
    _save_draft(tmp_path, capsys)
    before = profiles.user_path(SLUG).read_bytes()
    code, out = _run(capsys, "profile", "edit", SLUG, "--add", "criteria.specialties=Rust", "--dry-run")
    assert code == 0 and out["dry_run"] is True and out["changed"] is True and out["saved"] is None
    assert "+  - Rust" in out["diff"]
    assert profiles.user_path(SLUG).read_bytes() == before
    assert len(profiles.history(SLUG)) == 1  # only the creating save


def test_edit_terms_compare_ignoring_case_and_accents(tmp_path, capsys):
    _save_draft(tmp_path, capsys, _draft(criteria={**_draft()["criteria"], "specialties": ["Análisis"]}))
    code, out = _run(capsys, "profile", "edit", SLUG, "--add", "criteria.specialties=ANALISIS",
                     "--remove", "criteria.specialties=Cobol")
    assert code == 0
    assert [c["status"] for c in out["changes"]] == ["already_present", "not_found"]
    assert out["changed"] is False and out["saved"] is None
    assert len(profiles.history(SLUG)) == 1


def test_edit_from_operations_file_with_non_ascii_terms(tmp_path, capsys):
    _save_draft(tmp_path, capsys)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({"operations": [
        {"op": "add", "path": "criteria.location_scope.preferred", "value": ["Valencia", "España"]},
        {"op": "add", "path": "query_templates.regions", "value": "Remoto España"},
        {"op": "set", "path": "prompt", "value": "Salary floor 60k EUR; avoid gambling companies."},
    ]}, ensure_ascii=False), encoding="utf-8")
    code, out = _run(capsys, "profile", "edit", SLUG, "--from", str(ops))
    assert code == 0, out
    saved = _saved()
    assert saved["criteria"]["location_scope"]["preferred"] == ["Spain", "Remote EU", "Valencia", "España"]
    assert saved["query_templates"]["regions"] == ["Remote EU", "Remoto España"]
    assert saved["prompt"].startswith("Salary floor")


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (["--set", "slug=other"], "slug and source_cv cannot be edited"),
        (["--add", "criteria.unknown_terms=x"], "not editable"),
        (["--add", "name=x"], "only works on term lists"),
        (["--set", "criteria.specialties=Rust"], "JSON list of strings"),
        (["--set", "criteria.remote_policy.required=hybrid"], "full_remote, any"),
        (["--set", "enabled=maybe"], "true or false"),
        (["--add", "criteria.specialties="], "non-empty term"),
        (["--add", "criteria.specialties"], "PATH=VALUE"),
        ([], "no operations given"),
    ],
)
def test_edit_rejects_invalid_operations_and_changes_nothing(tmp_path, capsys, args, fragment):
    _save_draft(tmp_path, capsys)
    before = profiles.user_path(SLUG).read_bytes()
    code, out = _run(capsys, "profile", "edit", SLUG, "--add", "criteria.specialties=Rust", *args) if args else _run(
        capsys, "profile", "edit", SLUG)
    assert code == 2 and out["error"] == "edit_invalid"
    assert any(fragment in error for error in out["errors"]), out["errors"]
    assert profiles.user_path(SLUG).read_bytes() == before


def test_edit_that_breaks_the_schema_is_refused(tmp_path, capsys):
    _save_draft(tmp_path, capsys)
    before = profiles.user_path(SLUG).read_bytes()
    code, out = _run(capsys, "profile", "edit", SLUG, "--set", "criteria.role_focus_terms=[]",
                     "--set", "criteria.target_titles=[]")
    assert code == 1 and out["error"] == "profile_invalid"
    assert any("target_titles" in e for e in out["errors"]) and out["diff"]
    assert profiles.user_path(SLUG).read_bytes() == before


@pytest.mark.parametrize(
    "content",
    ["not json", '{"operations": "add"}', '[1, 2]'],
)
def test_edit_rejects_malformed_operations_files(tmp_path, capsys, content):
    _save_draft(tmp_path, capsys)
    ops = tmp_path / "ops.json"
    ops.write_text(content, encoding="utf-8")
    code, out = _run(capsys, "profile", "edit", SLUG, "--from", str(ops))
    assert code == 2 and out["error"] == "edit_invalid"


def test_edit_operation_file_errors_name_each_operation(tmp_path, capsys):
    _save_draft(tmp_path, capsys)
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "rename", "path": "name", "value": "x"},
        {"op": "set", "path": "name"},
        {"op": "add", "path": "criteria.specialties", "value": "x", "extra": 1},
    ]), encoding="utf-8")
    code, out = _run(capsys, "profile", "edit", SLUG, "--from", str(ops))
    assert code == 2
    assert [e.split(":")[0] for e in out["errors"]] == ["operation 1", "operation 2", "operation 3"]


def test_edit_missing_and_unreadable_profiles(capsys):
    code, out = _run(capsys, "profile", "edit", "ghost", "--add", "criteria.specialties=x")
    assert code == 2 and out["error"] == "profile_not_found"
    broken = home.ultron_home() / "profiles" / "broken.yaml"
    broken.write_text("slug: broken\nname: [unclosed\n", encoding="utf-8")
    code, out = _run(capsys, "profile", "edit", "broken", "--add", "criteria.specialties=x")
    assert code == 2 and out["error"] == "profile_unreadable"


def test_editing_a_bundled_profile_writes_a_user_copy_that_undo_removes(capsys):
    assert profiles.profile_origin("ai-ml-remote-eu")[0] == "bundled"
    code, out = _run(capsys, "profile", "edit", "ai-ml-remote-eu", "--add", "criteria.specialties=Rust")
    assert code == 0 and out["copied_from_bundled"] is True
    assert profiles.profile_origin("ai-ml-remote-eu")[0] == "user"
    bundled = home.data_dir() / "profiles" / "ai-ml-remote-eu.yaml"
    assert "Rust" not in bundled.read_text(encoding="utf-8")
    code, out = _run(capsys, "profile", "undo", "ai-ml-remote-eu")
    assert code == 0 and out["removes_user_copy"] is True
    assert profiles.profile_origin("ai-ml-remote-eu")[0] == "bundled"


def test_unchanged_operations_do_not_write(tmp_path, capsys):
    _save_draft(tmp_path, capsys)
    code, out = _run(capsys, "profile", "edit", SLUG, "--set", "criteria.specialties=[\"Go\", \"Kafka\"]")
    assert code == 0 and out["changes"][0]["status"] == "unchanged" and out["saved"] is None


def test_edit_creates_missing_sections():
    data = {"slug": "minimal", "name": "Minimal", "criteria": {"target_titles": ["Nurse"]}}
    edited, changes = profile_edit.apply_operations(data, [
        {"op": "add", "path": "criteria.location_scope.preferred", "value": "Canada"},
        {"op": "remove", "path": "query_templates.regions", "value": "Remote"},
        {"op": "unset", "path": "description"},
        {"op": "remove", "path": "criteria.specialties", "value": "Go"},
    ])
    assert edited["criteria"]["location_scope"] == {"preferred": ["Canada"]}
    assert "specialties" not in edited["criteria"]  # removing from a missing list adds nothing
    assert [c["status"] for c in changes] == ["added", "not_found", "not_found", "not_found"]
    assert data == {"slug": "minimal", "name": "Minimal", "criteria": {"target_titles": ["Nurse"]}}


def test_edit_refuses_a_path_under_a_non_mapping():
    data = {"slug": "odd", "name": "Odd", "criteria": {"target_titles": ["Nurse"], "seniority": "senior"}}
    with pytest.raises(profile_edit.EditError, match="not a mapping"):
        profile_edit.apply_operations(data, [{"op": "add", "path": "criteria.seniority.preferred_terms", "value": "x"}])
    with pytest.raises(profile_edit.EditError, match="not a list"):
        profile_edit.apply_operations(
            {**data, "criteria": {"target_titles": ["Nurse"], "specialties": "Go"}},
            [{"op": "add", "path": "criteria.specialties", "value": "x"}],
        )


# --- history and undo ------------------------------------------------------------------------------------


def test_undo_steps_back_through_several_edits_and_can_redo(tmp_path, capsys):
    _save_draft(tmp_path, capsys)
    v0 = profiles.user_path(SLUG).read_bytes()
    _run(capsys, "profile", "edit", SLUG, "--add", "criteria.specialties=Rust")
    v1 = profiles.user_path(SLUG).read_bytes()
    _run(capsys, "profile", "edit", SLUG, "--add", "criteria.specialties=Elixir")
    v2 = profiles.user_path(SLUG).read_bytes()

    code, out = _run(capsys, "profile", "undo", SLUG, "--dry-run")
    assert code == 0 and "-  - Elixir" in out["diff"]
    assert profiles.user_path(SLUG).read_bytes() == v2

    assert _run(capsys, "profile", "undo", SLUG)[0] == 0
    assert profiles.user_path(SLUG).read_bytes() == v1
    assert _run(capsys, "profile", "undo", SLUG)[0] == 0
    assert profiles.user_path(SLUG).read_bytes() == v0
    redo = [v for v in profiles.history(SLUG) if v["reason"] == "undo"][-1]["version"]  # oldest undo = v2
    assert _run(capsys, "profile", "undo", SLUG, "--to", redo)[0] == 0
    assert profiles.user_path(SLUG).read_bytes() == v2


def test_undo_restores_comments_byte_for_byte(capsys):
    path = profiles.user_path(SLUG)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = "# my notes\n" + yaml.safe_dump(_draft(), allow_unicode=True, sort_keys=False)
    path.write_text(original, encoding="utf-8")
    assert _run(capsys, "profile", "edit", SLUG, "--add", "criteria.specialties=Rust")[0] == 0
    assert "# my notes" not in path.read_text(encoding="utf-8")
    assert _run(capsys, "profile", "undo", SLUG)[0] == 0
    assert path.read_text(encoding="utf-8") == original


def test_undo_of_a_new_profile_removes_it(tmp_path, capsys):
    _save_draft(tmp_path, capsys)
    code, out = _run(capsys, "profile", "undo", SLUG)
    assert code == 0 and out["removes_user_copy"] is True
    assert not profiles.user_path(SLUG).exists()
    assert SLUG not in [p["slug"] for p in profiles.list_profiles()]
    redo = profiles.history(SLUG)[0]
    assert redo["reason"] == "undo" and redo["exists"] is True


def test_undo_errors(tmp_path, capsys):
    code, out = _run(capsys, "profile", "undo", "ghost")
    assert code == 2 and out["error"] == "no_history"
    code, out = _run(capsys, "profile", "undo", "../evil")
    assert code == 2 and out["error"] == "profile_not_found"
    _save_draft(tmp_path, capsys)
    code, out = _run(capsys, "profile", "undo", SLUG, "--to", "000099-20260101T000000Z-edit")
    assert code == 2 and out["error"] == "version_not_found"


def test_history_lists_versions_newest_first_and_is_bounded(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(profiles, "HISTORY_LIMIT", 3)
    _save_draft(tmp_path, capsys)
    for term in ("A1", "B2", "C3", "D4"):
        _run(capsys, "profile", "edit", SLUG, "--add", f"criteria.specialties={term}")
    code, out = _run(capsys, "profile", "history", SLUG)
    assert code == 0 and len(out["versions"]) == 3
    assert [v["reason"] for v in out["versions"]] == ["edit", "edit", "edit"]
    assert out["versions"][0]["version"] > out["versions"][-1]["version"]
    assert _run(capsys, "profile", "history", "ghost")[1]["error"] == "profile_not_found"


def test_undo_to_the_oldest_version_survives_history_pruning(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(profiles, "HISTORY_LIMIT", 3)
    _save_draft(tmp_path, capsys)
    for term in ("A1", "B2", "C3"):
        _run(capsys, "profile", "edit", SLUG, "--add", f"criteria.specialties={term}")
    oldest = profiles.history(SLUG)[-1]["version"]
    expected = (profiles._history_dir(SLUG) / f"{oldest}.yaml").read_bytes()
    code, out = _run(capsys, "profile", "undo", SLUG, "--to", oldest)
    assert code == 0, out
    assert profiles.user_path(SLUG).read_bytes() == expected


def test_edit_refuses_a_file_whose_slug_differs_from_its_name(capsys):
    path = profiles.user_path("foo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(_draft(slug="bar")), encoding="utf-8")
    code, out = _run(capsys, "profile", "edit", "foo", "--add", "criteria.specialties=Rust")
    assert code == 2 and out["error"] == "slug_mismatch"
    assert not profiles.user_path("bar").exists()


def test_history_folder_never_lists_as_a_profile(tmp_path, capsys):
    _save_draft(tmp_path, capsys)
    _run(capsys, "profile", "edit", SLUG, "--add", "criteria.specialties=Rust")
    assert [p["slug"] for p in profiles.list_profiles()].count(SLUG) == 1
    assert (home.ultron_home() / "profiles" / ".history" / SLUG).is_dir()


def test_failed_write_leaves_the_profile_and_no_temp_file(tmp_path, capsys, monkeypatch):
    _save_draft(tmp_path, capsys)
    before = profiles.user_path(SLUG).read_bytes()

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(profiles.os, "replace", broken_replace)
    with pytest.raises(OSError):
        profiles.write_profile(_draft(name="Changed"), reason="edit")
    assert profiles.user_path(SLUG).read_bytes() == before
    assert not list(profiles.user_path(SLUG).parent.glob("*.tmp"))


# --- explain ---------------------------------------------------------------------------------------------


def test_explain_summarizes_the_effective_filters(tmp_path, capsys):
    _save_draft(tmp_path, capsys)
    code, out = _run(capsys, "profile", "explain", SLUG)
    assert code == 0
    assert out["role_match"]["source"] == "role_focus_terms + target_titles"
    assert "backend engineer" in out["role_match"]["terms"]
    assert out["geography"]["scope"] == "eu"
    assert out["remote"] == {"required": "full_remote", "remote_only": True, "positive_terms": [],
                             "negative_terms": []}
    assert out["title_exclusions"]["applied"] == ["data entry", "intern"]
    assert out["query_plan"]["queries"][0] == "Senior Backend Engineer Remote EU"
    assert out["warnings"] == []


def test_explain_flags_risky_terms(tmp_path, capsys):
    criteria = {**_draft()["criteria"], "role_focus_terms": ["product manager", "Product Manager", "pm, po"],
                "exclusion_terms": ["manager"]}
    draft = tmp_path / "draft.yaml"
    draft.write_text(yaml.safe_dump(_draft(criteria=criteria, query_templates={})), encoding="utf-8")
    code, out = _run(capsys, "profile", "explain", "--from", str(draft))
    assert code == 0
    warnings = "\n".join(out["warnings"])
    assert "duplicates" in warnings and "several terms in one" in warnings
    assert "'manager' is ignored for titles" in warnings
    assert "query_templates.regions is empty" in warnings
    assert out["title_exclusions"]["ignored"] == ["manager"]


def test_explain_non_european_scope_and_usage_errors(tmp_path, capsys):
    latam = yaml.safe_load((FIXTURES / "backend-latam.yaml").read_text(encoding="utf-8"))
    draft = tmp_path / "latam.yaml"
    draft.write_text(yaml.safe_dump(latam), encoding="utf-8")
    code, out = _run(capsys, "profile", "explain", "--from", str(draft), "--preset", "fast")
    assert code == 0 and out["geography"]["scope"] == "other" and out["query_plan"]["preset"] == "fast"
    assert _run(capsys, "profile", "explain")[1]["error"] == "usage"
    assert _run(capsys, "profile", "explain", SLUG, "--preset", "bogus")[1]["error"] == "invalid_value"
    assert _run(capsys, "profile", "explain", "ghost")[1]["error"] == "profile_not_found"
    bad = tmp_path / "bad.yaml"
    bad.write_text("slug: x\nname: X\ncriteria: {}\n", encoding="utf-8")
    code, out = _run(capsys, "profile", "explain", "--from", str(bad))
    assert code == 1 and out["error"] == "profile_invalid"


def _run_with_postings(capsys) -> str:
    code, out = _run(capsys, "run", "new", "--profile", "ai-ml-remote-eu", "--preset", "fast")
    assert code == 0
    run_id = out["run_id"]
    jobs = [
        ("Senior Machine Learning Engineer", "Build LLM inference and RAG systems in production."),
        ("Senior Backend Engineer", "Build Go services and Kafka pipelines for payments."),
    ]
    rows = [
        {
            "candidate": {"source_type": "ats_api"},
            "identity_key": f"id:{i}",
            "job": {
                "title": title, "company": "Example Co", "location": "Remote - Europe",
                "remote_text": "Remote - Europe", "body_text": f"{body} Fully remote across Europe.",
                "apply_url": f"https://boards.greenhouse.io/example/jobs/{i}", "domain": "boards.greenhouse.io",
            },
        }
        for i, (title, body) in enumerate(jobs)
    ]
    runs.write_jsonl(runs.run_dir(run_id) / "postings.jsonl", rows)
    runs.mark_stage(run_id, "fetch")
    return run_id


def test_explain_replays_a_run_with_a_draft_profile(tmp_path, capsys):
    run_id = _run_with_postings(capsys)
    code, out = _run(capsys, "profile", "explain", "ai-ml-remote-eu", "--run", run_id)
    assert code == 0 and out["replay"]["postings"] == 2 and out["replay"]["compared_with_run"] is False
    assert _run(capsys, "filter", "--run", run_id)[0] == 0
    code, out = _run(capsys, "profile", "explain", "ai-ml-remote-eu", "--run", run_id)
    assert out["replay"]["compared_with_run"] is True and out["replay"]["changes"] == {}
    _save_draft(tmp_path, capsys)
    folder = runs.run_dir(run_id)
    before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    code, out = _run(capsys, "profile", "explain", SLUG, "--run", run_id)
    replay = out["replay"]
    assert replay["kept"] + replay["ambiguous"] + replay["discarded"] == 2
    assert replay["changes"], replay  # a backend profile treats the two postings differently
    example = next(iter(replay["examples"].values()))[0]
    assert set(example) >= {"title", "company", "before", "after"}
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == before  # nothing written


def test_explain_replay_errors(capsys):
    assert _run(capsys, "profile", "explain", "ai-ml-remote-eu", "--run", "nope")[1]["error"] == "run_not_found"
    code, out = _run(capsys, "run", "new", "--profile", "ai-ml-remote-eu", "--preset", "fast")
    run_id = out["run_id"]
    code, out = _run(capsys, "profile", "explain", "ai-ml-remote-eu", "--run", run_id)
    assert code == 3 and out["error"] == "stage_order"
    (runs.run_dir(run_id) / "postings.jsonl").write_text("{not json\n", encoding="utf-8")
    runs.mark_stage(run_id, "fetch")
    code, out = _run(capsys, "profile", "explain", "ai-ml-remote-eu", "--run", run_id)
    assert code == 2 and out["error"] == "stage_failed"


def test_explain_lists_built_in_title_exclusions(tmp_path, capsys):
    criteria = {**_draft()["criteria"], "seniority": {"required": False}}
    criteria.pop("exclusion_terms")
    draft = tmp_path / "draft.yaml"
    draft.write_text(yaml.safe_dump(_draft(criteria=criteria)), encoding="utf-8")
    code, out = _run(capsys, "profile", "explain", "--from", str(draft))
    assert code == 0
    exclusions = out["title_exclusions"]
    assert exclusions["applied"] == []
    assert {"manager", "director", "head", "team lead", "tech lead", "vp"} <= set(exclusions["built_in"])
    assert set(exclusions["built_in"]) <= set(exclusions["effective"])


@pytest.mark.parametrize(
    ("seniority", "titles", "required"),
    [
        (None, ["Nurse"], False),  # omitted seniority, unqualified title: any level
        ({"excluded_terms": ["intern"]}, ["Nurse"], False),  # only excluded terms: still any level
        (None, ["Senior Nurse"], True),  # a qualified title implies the level
        ({"required": True}, ["Nurse"], True),
    ],
)
def test_explain_seniority_requirement_matches_the_evaluator(tmp_path, capsys, seniority, titles, required):
    criteria = {**_draft()["criteria"], "role_focus_terms": ["nurse"], "target_titles": titles}
    if seniority is None:
        criteria.pop("seniority")
    else:
        criteria["seniority"] = seniority
    draft = tmp_path / "draft.yaml"
    draft.write_text(yaml.safe_dump(_draft(criteria=criteria, query_templates={})), encoding="utf-8")
    code, out = _run(capsys, "profile", "explain", "--from", str(draft))
    assert code == 0
    assert out["seniority"]["required"] is required


def test_edit_runs_from_file_at_its_place_in_the_order(tmp_path, capsys):
    _save_draft(tmp_path, capsys)
    reset = tmp_path / "reset.json"
    reset.write_text(json.dumps({"operations": [{"op": "set", "path": "criteria.specialties", "value": ["Go"]}]}),
                     encoding="utf-8")
    code, out = _run(capsys, "profile", "edit", SLUG, "--from", str(reset), "--add", "criteria.specialties=Rust")
    assert code == 0, out
    code, shown = _run(capsys, "profile", "show", SLUG)
    assert shown["profile"]["criteria"]["specialties"] == ["Go", "Rust"]
    code, out = _run(capsys, "profile", "edit", SLUG, "--add", "criteria.specialties=Zig", "--from", str(reset))
    assert code == 0, out
    code, shown = _run(capsys, "profile", "show", SLUG)
    assert shown["profile"]["criteria"]["specialties"] == ["Go"]  # the file ran last


def test_plain_undo_after_an_explicit_restore_brings_back_the_replaced_version(tmp_path, capsys):
    _save_draft(tmp_path, capsys)  # A
    a = profiles.user_path(SLUG).read_bytes()
    _run(capsys, "profile", "edit", SLUG, "--add", "criteria.specialties=Rust")  # B
    _run(capsys, "profile", "edit", SLUG, "--add", "criteria.specialties=Elixir")  # C
    c = profiles.user_path(SLUG).read_bytes()
    version_a = [v for v in profiles.history(SLUG) if v["reason"] == "edit"][-1]["version"]  # the copy of A
    assert _run(capsys, "profile", "undo", SLUG, "--to", version_a)[0] == 0
    assert profiles.user_path(SLUG).read_bytes() == a
    assert profiles.history(SLUG)[0]["reason"] == "restore"
    assert _run(capsys, "profile", "undo", SLUG)[0] == 0
    assert profiles.user_path(SLUG).read_bytes() == c  # not B


def test_explain_warns_about_fields_the_cli_ignores(tmp_path, capsys):
    criteria = {**_draft()["criteria"], "company_exclusions": ["Example Consulting"], "sectors": ["fintech"]}
    criteria["seniority"] = {**criteria["seniority"], "level": "mid"}
    draft = tmp_path / "draft.yaml"
    draft.write_text(yaml.safe_dump(_draft(criteria=criteria, salary_min=60000)), encoding="utf-8")
    code, out = _run(capsys, "profile", "explain", "--from", str(draft))
    assert code == 0
    warnings = "\n".join(out["warnings"])
    assert "salary_min" in warnings and "criteria.company_exclusions" in warnings
    assert "criteria.seniority.level" in warnings and "prompt" in warnings
    assert "'fintech' is not a known sector" in warnings


def test_explain_has_no_field_warnings_for_bundled_and_fixture_profiles(capsys):
    for path in sorted([*(home.data_dir() / "profiles").glob("*.yaml"), *FIXTURES.glob("*.yaml")]):
        code, out = _run(capsys, "profile", "explain", "--from", str(path))
        field_warnings = [w for w in out["warnings"] if "not a profile field" in w or "known sector" in w]
        assert code == 0 and field_warnings == [], (path.name, field_warnings)


def test_explain_accepts_sector_aliases_that_select_packs(tmp_path, capsys):
    # "healthcare" maps to pharma_health for pack selection, so explain must not call it unknown.
    criteria = {**_draft()["criteria"], "sectors": ["healthcare", "Software", "fintech"]}
    draft = tmp_path / "draft.yaml"
    draft.write_text(yaml.safe_dump(_draft(criteria=criteria)), encoding="utf-8")
    code, out = _run(capsys, "profile", "explain", "--from", str(draft))
    warnings = "\n".join(out["warnings"])
    assert code == 0
    assert "'fintech' is not a known sector" in warnings
    assert "'healthcare'" not in warnings and "'Software'" not in warnings

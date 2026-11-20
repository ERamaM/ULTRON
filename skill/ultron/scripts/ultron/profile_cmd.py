"""``ultron profile`` subcommands."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from ultron import presets, profile_edit, profile_explain, profiles, runs
from ultron.output import emit, fail, fail_stage_order


def _list(args: argparse.Namespace) -> int:
    emit({"profiles": profiles.list_profiles()})
    return 0


def _load_saved(slug: str) -> tuple[dict | None, int | None]:
    """Load a saved or bundled profile; a missing or unreadable one becomes a typed error."""
    try:
        data = profiles.load_profile(slug)
    except FileNotFoundError as exc:
        return None, fail("profile_not_found", str(exc))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return None, fail("profile_unreadable", f"{slug}: {exc}")
    if not isinstance(data, dict):
        return None, fail("profile_unreadable", f"{slug}: the file does not hold a YAML mapping")
    return data, None


def _show(args: argparse.Namespace) -> int:
    try:
        data = profiles.load_profile(args.slug)
    except FileNotFoundError as exc:
        return fail("profile_not_found", str(exc))
    origin, path = profiles.profile_origin(args.slug)
    emit({"profile": data, "origin": origin, "path": str(path)})
    return 0


def _load_yaml_source(source: str) -> tuple[object, int | None]:
    try:
        return yaml.safe_load(Path(source).read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return None, fail("profile_unreadable", str(exc))


def _validate(args: argparse.Namespace) -> int:
    if bool(args.slug) == bool(args.source):
        return fail("usage", "pass either a profile slug or --from <yaml>")
    if args.source:
        data, err = _load_yaml_source(args.source)
        if err is not None:
            return err
        slug = data.get("slug") if isinstance(data, dict) else None
    else:
        try:
            data = profiles.load_profile(args.slug)
        except FileNotFoundError as exc:
            return fail("profile_not_found", str(exc))
        slug = args.slug
    errors = profiles.validate_profile(data)
    emit({"slug": slug, "valid": not errors, "errors": errors})
    return 0 if not errors else 1


def _install(args: argparse.Namespace) -> int:
    try:
        emit({"installed": str(profiles.install_example(force=args.force))})
    except profiles.ProfileExistsError as exc:
        return fail("profile_exists", str(exc))
    return 0


def _invalid(errors: list[str], **extra) -> int:
    emit({"error": "profile_invalid", "detail": "; ".join(errors), "errors": errors, **extra})
    return 1


def _current_text(slug: str) -> str:
    """The YAML text the user's copy has now ("" when there is none)."""
    path = profiles.user_path(slug)
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _save(args: argparse.Namespace) -> int:
    data, err = _load_yaml_source(args.source)
    if err is not None:
        return err
    if args.source_cv:
        cv_path = Path(args.source_cv)
        if not cv_path.is_file():
            return fail("source_cv_not_found", f"CV file not found: {args.source_cv}")
        if isinstance(data, dict):
            data["source_cv"] = str(cv_path.resolve())
    if args.dry_run:
        errors = profiles.validate_profile(data)
        if errors:
            return _invalid(errors)
        slug = data["slug"]
        exists = profiles.user_path(slug).is_file()
        emit({
            "slug": slug, "dry_run": True, "valid": True, "exists": exists,
            "would_overwrite": exists, "needs_force": exists and not args.force,
            "diff": profile_edit.unified_diff(_current_text(slug), profiles.dump_profile(data), f"{slug}.yaml"),
        })
        return 0
    try:
        path = profiles.save_profile(data, force=args.force)
    except profiles.ProfileInvalidError as exc:
        return _invalid(exc.errors)
    except profiles.ProfileExistsError as exc:
        return fail("profile_exists", str(exc))
    emit({"saved": str(path), "slug": data["slug"]})
    return 0


def _edit(args: argparse.Namespace) -> int:
    data, err = _load_saved(args.slug)
    if err is not None:
        return err
    if data.get("slug") != args.slug:
        return fail("slug_mismatch", f"{args.slug}.yaml declares slug {data.get('slug')!r}; fix the file first "
                    "(`ultron profile validate` and `ultron profile save --force`)")
    try:
        operations = [profile_edit.parse_cli_operation(op, raw) for op, raw in (args.ops or [])]
        if args.source:
            operations.extend(profile_edit.load_operations(args.source))
        edited, changes = profile_edit.apply_operations(data, operations)
    except profile_edit.EditError as exc:
        emit({"error": "edit_invalid", "detail": str(exc), "errors": exc.errors})
        return 2
    origin, _path = profiles.profile_origin(args.slug)
    before_text = profiles.dump_profile(data)
    diff = profile_edit.unified_diff(before_text, profiles.dump_profile(edited), f"{args.slug}.yaml")
    errors = profiles.validate_profile(edited)
    payload = {"slug": args.slug, "dry_run": args.dry_run, "changed": bool(diff), "changes": changes, "diff": diff}
    if errors:
        return _invalid(errors, changes=changes, diff=diff)
    if args.dry_run or not diff:
        emit({**payload, "valid": True, "saved": None})
        return 0
    path, version = profiles.write_profile(edited, reason="edit")
    emit({**payload, "valid": True, "saved": str(path), "undo_version": version,
          "copied_from_bundled": origin == "bundled"})
    return 0


def _history(args: argparse.Namespace) -> int:
    if profiles.profile_origin(args.slug) is None and not profiles.history(args.slug):
        return fail("profile_not_found", f"profile not found: {args.slug}")
    emit({"slug": args.slug, "versions": profiles.history(args.slug)})
    return 0


def _undo(args: argparse.Namespace) -> int:
    if not profiles.SLUG_RE.fullmatch(args.slug):
        return fail("profile_not_found", f"profile not found: {args.slug}")
    try:
        version, text = profiles.version_content(args.slug, args.to)
    except profiles.VersionNotFoundError as exc:
        return fail("version_not_found", str(exc))
    except profiles.NoHistoryError as exc:
        return fail("no_history", str(exc))
    current = _current_text(args.slug)
    diff = profile_edit.unified_diff(current, text or "", f"{args.slug}.yaml")
    payload = {"slug": args.slug, "version": version, "dry_run": args.dry_run, "diff": diff,
               "removes_user_copy": text is None}
    if args.dry_run:
        emit(payload)
        return 0
    result = profiles.restore(args.slug, args.to)
    emit({**payload, "restored": result["restored"], "backup_version": result["backup"]})
    return 0


def _explain(args: argparse.Namespace) -> int:
    if bool(args.slug) == bool(args.source):
        return fail("usage", "pass either a profile slug or --from <file>")
    if args.preset not in presets.PRESETS:
        return fail("invalid_value", f"unknown preset: {args.preset!r} (choose from {', '.join(presets.PRESETS)})")
    if args.source:
        data, err = _load_yaml_source(args.source)
    else:
        data, err = _load_saved(args.slug)
    if err is not None:
        return err
    errors = profiles.validate_profile(data)
    if errors:
        return _invalid(errors)
    payload = profile_explain.explain(data, preset=args.preset)
    if args.run:
        try:
            payload["replay"] = profile_explain.replay_run(data, args.run)
        except runs.RunNotFoundError as exc:
            return fail("run_not_found", str(exc))
        except runs.StageOrderError as exc:
            return fail_stage_order(exc)
        except (OSError, ValueError) as exc:  # same mapping as `ultron filter` for unreadable run files
            return fail("stage_failed", str(exc))
    emit(payload)
    return 0


def _op(name: str):
    return lambda raw: (name, raw)


def register(subparsers) -> None:
    parser = subparsers.add_parser("profile", help="manage search profiles")
    sub = parser.add_subparsers(dest="profile_command", required=True, parser_class=type(parser))
    sub.add_parser("list", help="list profiles").set_defaults(func=_list)
    show = sub.add_parser("show", help="print a profile as JSON, with its origin and path")
    show.add_argument("slug")
    show.set_defaults(func=_show)
    validate = sub.add_parser("validate", help="validate a profile")
    validate.add_argument("slug", nargs="?", help="slug of a saved or bundled profile")
    validate.add_argument("--from", dest="source", help="path to a profile YAML file")
    validate.set_defaults(func=_validate)
    install = sub.add_parser("install-example", help="copy the bundled example profile to the ULTRON home")
    install.add_argument("--force", action="store_true", help="overwrite your copy of the example")
    install.set_defaults(func=_install)
    save = sub.add_parser("save", help="validate a YAML or JSON profile and save it to the ULTRON home")
    save.add_argument("--from", dest="source", required=True, help="path to the profile YAML or JSON")
    save.add_argument("--force", action="store_true", help="overwrite an existing profile")
    save.add_argument("--source-cv", help="path of the CV this profile was generated from (must exist)")
    save.add_argument("--dry-run", action="store_true", help="validate and show the diff without saving")
    save.set_defaults(func=_save)

    edit = sub.add_parser(
        "edit", help="apply explicit edits to a saved profile (validated, diffed, undoable)",
        description="Fields: " + ", ".join(profile_edit.EDITABLE) + ". Operations run in the order given.",
    )
    edit.add_argument("slug")
    edit.add_argument("--add", dest="ops", action="append", type=_op("add"), metavar="FIELD=TERM",
                      help="add a term to a term-list field")
    edit.add_argument("--remove", dest="ops", action="append", type=_op("remove"), metavar="FIELD=TERM",
                      help="remove a term (case and accents ignored)")
    edit.add_argument("--set", dest="ops", action="append", type=_op("set"), metavar="FIELD=VALUE",
                      help="set a field (true/false as is; a JSON list works, but prefer --from for lists)")
    edit.add_argument("--unset", dest="ops", action="append", type=_op("unset"), metavar="FIELD",
                      help="remove an optional field")
    edit.add_argument("--from", dest="source", help='JSON file with {"operations": [{"op", "path", "value"}]}')
    edit.add_argument("--dry-run", action="store_true", help="show the changes and diff without saving")
    edit.set_defaults(func=_edit)

    hist = sub.add_parser("history", help="list the saved versions of a profile")
    hist.add_argument("slug")
    hist.set_defaults(func=_history)
    undo = sub.add_parser("undo", help="restore the version before the last change (or --to a version)")
    undo.add_argument("slug")
    undo.add_argument("--to", help="version id from `ultron profile history`")
    undo.add_argument("--dry-run", action="store_true", help="show the diff without restoring")
    undo.set_defaults(func=_undo)

    explain = sub.add_parser("explain", help="show the filters, signals and queries a profile produces")
    explain.add_argument("slug", nargs="?", help="slug of a saved or bundled profile")
    explain.add_argument("--from", dest="source", help="path to a draft profile (YAML or JSON)")
    explain.add_argument("--preset", default=presets.DEFAULT_PRESET, help="preset for the query plan size")
    explain.add_argument("--run", help="replay the filter of this earlier run with the profile (writes nothing)")
    explain.set_defaults(func=_explain)

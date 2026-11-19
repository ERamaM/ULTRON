"""``ultron profile`` subcommands."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from ultron import profiles
from ultron.output import emit, fail


def _list(args: argparse.Namespace) -> int:
    emit({"profiles": profiles.list_profiles()})
    return 0


def _show(args: argparse.Namespace) -> int:
    try:
        emit({"profile": profiles.load_profile(args.slug)})
    except FileNotFoundError as exc:
        return fail("profile_not_found", str(exc))
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
    try:
        path = profiles.save_profile(data, force=args.force)
    except profiles.ProfileInvalidError as exc:
        emit({"error": "profile_invalid", "detail": str(exc), "errors": exc.errors})
        return 1
    except profiles.ProfileExistsError as exc:
        return fail("profile_exists", str(exc))
    emit({"saved": str(path), "slug": data["slug"]})
    return 0


def register(subparsers) -> None:
    parser = subparsers.add_parser("profile", help="manage search profiles")
    sub = parser.add_subparsers(dest="profile_command", required=True, parser_class=type(parser))
    sub.add_parser("list", help="list profiles").set_defaults(func=_list)
    show = sub.add_parser("show", help="print a profile")
    show.add_argument("slug")
    show.set_defaults(func=_show)
    validate = sub.add_parser("validate", help="validate a profile")
    validate.add_argument("slug", nargs="?", help="slug of a saved or bundled profile")
    validate.add_argument("--from", dest="source", help="path to a profile YAML file")
    validate.set_defaults(func=_validate)
    install = sub.add_parser("install-example", help="copy the bundled example profile to the ULTRON home")
    install.add_argument("--force", action="store_true", help="overwrite your copy of the example")
    install.set_defaults(func=_install)
    save = sub.add_parser("save", help="validate a YAML profile and save it to the ULTRON home")
    save.add_argument("--from", dest="source", required=True, help="path to the profile YAML")
    save.add_argument("--force", action="store_true", help="overwrite an existing profile")
    save.add_argument("--source-cv", help="path of the CV this profile was generated from (must exist)")
    save.set_defaults(func=_save)

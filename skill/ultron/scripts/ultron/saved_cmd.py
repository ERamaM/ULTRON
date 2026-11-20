"""``ultron saved-search create|list|delete|run``."""

from __future__ import annotations

import argparse
import json

from ultron import presets, runs, saved
from ultron.output import emit, fail, fail_stage_order


def _parse_overrides(items: list[str]) -> dict:
    out = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"override must be KEY=VALUE: {item!r}")
        try:
            out[key.strip()] = json.loads(value)
        except ValueError:
            out[key.strip()] = value
    return out


def _create(args: argparse.Namespace) -> int:
    try:
        saved.create(args.name, args.profile, args.preset, args.prompt, _parse_overrides(args.override))
    except FileNotFoundError as exc:
        return fail("profile_not_found", str(exc))
    except ValueError as exc:
        return fail("invalid_value", str(exc))
    emit({"created": args.name})
    return 0


def _list(args: argparse.Namespace) -> int:
    emit({"searches": saved.list_all()})
    return 0


def _delete(args: argparse.Namespace) -> int:
    emit({"name": args.name, "deleted": saved.delete(args.name)})
    return 0


def _run(args: argparse.Namespace) -> int:
    try:
        emit(saved.run(args.name, unattended=args.unattended))
    except LookupError as exc:
        return fail("not_found", str(exc.args[0]))
    except FileNotFoundError as exc:
        return fail("profile_not_found", str(exc))
    except runs.StageOrderError as exc:
        return fail_stage_order(exc)
    except presets.PresetOverrideError as exc:  # a stored override outside the current bounds
        return fail("invalid_value", str(exc))
    except (OSError, ValueError) as exc:
        return fail("stage_failed", str(exc))
    return 0


def register(subparsers) -> None:
    root = subparsers.add_parser("saved-search", help="named, re-runnable searches")
    sub = root.add_subparsers(dest="saved_command", required=True, parser_class=type(root))
    p = sub.add_parser("create", help="create or replace a saved search")
    p.add_argument("name")
    p.add_argument("--profile", required=True)
    p.add_argument("--preset", default=presets.DEFAULT_PRESET)
    p.add_argument("--prompt", default="")
    p.add_argument("--override", action="append", default=[], metavar="KEY=VALUE")
    p.set_defaults(func=_create)
    sub.add_parser("list", help="list saved searches").set_defaults(func=_list)
    p = sub.add_parser("delete", help="delete a saved search")
    p.add_argument("name")
    p.set_defaults(func=_delete)
    p = sub.add_parser("run", help="run a saved search")
    p.add_argument("name")
    p.add_argument("--unattended", action="store_true")
    p.set_defaults(func=_run)

"""``ultron update [--check] [--to vX.Y.Z] [--project DIR]`` command."""

from __future__ import annotations

import argparse
from pathlib import Path

from ultron import update
from ultron.output import emit, fail
from ultron.update_notice import InvalidRepository, ReleaseLookupError, parse_version


def _version(value: str) -> tuple[int, int, int]:
    version = parse_version(value)
    if version is None:
        raise argparse.ArgumentTypeError(f"{value!r} is not a release version like v1.2.3")
    return version


def _run(args: argparse.Namespace) -> int:
    try:
        if args.check:
            emit(update.check())
        else:
            emit(update.prepare_update(to=args.to, projects=[Path(p) for p in args.project]))
    except InvalidRepository as exc:
        return fail("invalid_repository", str(exc))
    except ReleaseLookupError as exc:
        return fail("update_check_failed" if args.check else "update_unavailable", str(exc), code=1)
    except update.UpdateError as exc:
        return fail(exc.error, str(exc), code=exc.code)
    except update.DownloadError as exc:
        return fail("update_unavailable", str(exc), code=1)
    except update.ArchiveError as exc:
        return fail("archive_invalid", str(exc), code=1)
    except OSError as exc:
        return fail("io_error", str(exc), code=1)
    return 0


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "update",
        help="check for a stable release, or download it and print the installer command to run (never runs it)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="only compare the installed and latest stable versions")
    mode.add_argument("--to", type=_version, metavar="vX.Y.Z", help="prepare this stable release instead of the latest")
    parser.add_argument("--project", action="append", default=[], metavar="DIR",
                        help="also refresh the project install in DIR (repeatable; the current folder is always checked)")
    parser.set_defaults(func=_run)

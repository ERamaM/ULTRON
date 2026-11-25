"""The agent toolkit commands: ``ultron inspect`` and ``ultron shortlist save``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ultron.commands.output import emit, fail
from ultron.core import presets
from ultron.profiles import store as profile_store
from ultron.storage import runs
from ultron.toolkit.inspect import inspect_jobs
from ultron.toolkit.shortlist import EmptyShortlist, save_shortlist


def _check(profile: str, preset: str) -> int | None:
    """A failure exit code for an unknown preset or profile, else ``None``."""
    try:
        presets.resolve(preset)
    except KeyError as exc:
        return fail("unknown_preset", str(exc.args[0]))
    try:
        profile_store.load_profile(profile)
    except FileNotFoundError:
        return fail("profile_not_found", f"profile not found: {profile}")
    return None


def read_inputs(path: str) -> tuple[list[str], list[str]]:
    """URLs and company names from a file: one URL per line, or JSONL rows with ``url`` or ``company``."""
    urls, companies = [], []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("{"):
            row = json.loads(text)
            if row.get("url"):
                urls.append(str(row["url"]))
            if row.get("company") and not row.get("url"):
                companies.append(str(row["company"]))
        else:
            urls.append(text)
    return urls, companies


def _inspect(args: argparse.Namespace) -> int:
    if (code := _check(args.profile, args.preset)) is not None:
        return code
    urls, companies = list(args.url or []), list(args.company or [])
    if args.source:
        try:
            more_urls, more_companies = read_inputs(args.source)
        except (OSError, ValueError) as exc:
            return fail("invalid_input", str(exc))
        urls, companies = urls + more_urls, companies + more_companies
    if not urls and not companies:
        return fail("usage", "give --url, --company or --from")
    emit(inspect_jobs(args.profile, urls, companies, detail=args.detail, preset=args.preset))
    return 0


def _save(args: argparse.Namespace) -> int:
    if (code := _check(args.profile, args.preset)) is not None:
        return code
    try:
        rows = runs.read_jsonl(args.source)
    except (OSError, ValueError) as exc:
        return fail("invalid_input", str(exc))
    try:
        emit(save_shortlist(args.profile, rows, preset=args.preset))
    except EmptyShortlist as exc:
        return fail("usage", str(exc))
    return 0


def register(subparsers) -> None:
    """Register ``inspect`` and ``shortlist save``."""
    inspect = subparsers.add_parser("inspect", help="open postings, boards and companies at once; compact dossiers")
    inspect.add_argument("--profile", required=True)
    inspect.add_argument("--url", action="append", help="a posting, aggregator or ATS board URL (repeatable)")
    inspect.add_argument("--company", action="append", help="a company name whose ATS board to list (repeatable)")
    inspect.add_argument("--from", dest="source", help="file with one URL per line, or JSONL with url/company")
    inspect.add_argument("--detail", action="store_true", help="longer posting excerpts")
    inspect.add_argument("--preset", default=presets.DEFAULT_PRESET)
    inspect.set_defaults(func=_inspect)

    shortlist = subparsers.add_parser("shortlist", help="save the agent's judged shortlist")
    sub = shortlist.add_subparsers(dest="shortlist_command", required=True)
    save = sub.add_parser("save", help="save judged postings (JSONL) as a finalized shortlist")
    save.add_argument("--profile", required=True)
    save.add_argument("--from", dest="source", required=True, help="JSONL rows with id or url, verdict, priority")
    save.add_argument("--preset", default=presets.DEFAULT_PRESET)
    save.set_defaults(func=_save)

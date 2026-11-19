"""``ultron jobs list|mark|note|feedback|new-since``."""

from __future__ import annotations

import argparse
from collections.abc import Callable

from ultron import review
from ultron.output import emit, fail


def _guard(fn: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    def wrapper(args: argparse.Namespace) -> int:
        try:
            return fn(args)
        except LookupError as exc:
            return fail("not_found", str(exc.args[0]))
        except ValueError as exc:
            return fail("invalid_value", str(exc))

    return wrapper


@_guard
def _list(args: argparse.Namespace) -> int:
    jobs = review.list_jobs(
        state=args.state, profile=args.profile, since_run=args.since_run, new_only=args.new, limit=args.limit
    )
    emit({"count": len(jobs), "jobs": jobs})
    return 0


@_guard
def _mark(args: argparse.Namespace) -> int:
    review.set_state(args.identity_key, args.state)
    emit({"identity_key": review.resolve_key(args.identity_key), "review_state": args.state})
    return 0


@_guard
def _note(args: argparse.Namespace) -> int:
    review.add_note(args.identity_key, args.text)
    emit({"identity_key": review.resolve_key(args.identity_key), "noted": True})
    return 0


@_guard
def _feedback(args: argparse.Namespace) -> int:
    review.set_feedback(args.identity_key, args.relevance, args.reason, args.note)
    emit({"identity_key": review.resolve_key(args.identity_key), "relevance": args.relevance, "reason_code": args.reason})
    return 0


@_guard
def _new_since(args: argparse.Namespace) -> int:
    jobs = review.new_since(args.run_id)
    emit({"since_run": args.run_id, "count": len(jobs), "jobs": jobs})
    return 0


def register(subparsers) -> None:
    jobs = subparsers.add_parser("jobs", help="review jobs recorded in the ledger")
    sub = jobs.add_subparsers(dest="jobs_command", required=True, parser_class=type(jobs))
    p = sub.add_parser("list", help="list jobs")
    p.add_argument("--state")
    p.add_argument("--profile")
    p.add_argument("--since-run")
    p.add_argument("--new", action="store_true", help="only jobs seen in a single run")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=_list)
    p = sub.add_parser("mark", help="set a review state")
    p.add_argument("identity_key", help="identity key or job URL")
    p.add_argument("state")
    p.set_defaults(func=_mark)
    p = sub.add_parser("note", help="append a note")
    p.add_argument("identity_key")
    p.add_argument("text")
    p.set_defaults(func=_note)
    p = sub.add_parser("feedback", help="label relevance")
    p.add_argument("identity_key")
    p.add_argument("relevance")
    p.add_argument("--reason", default="")
    p.add_argument("--note", default="")
    p.set_defaults(func=_feedback)
    p = sub.add_parser("new-since", help="jobs first seen since a run")
    p.add_argument("run_id")
    p.set_defaults(func=_new_since)

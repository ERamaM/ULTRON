"""``ultron salary analyze`` and ``ultron salary save`` subcommands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from ultron.commands._common import parse_as_of
from ultron.commands.output import emit, fail
from ultron.cv import extract as cv
from ultron.infra import home
from ultron.salary import estimator as salary
from ultron.storage import runs


def _analyze(args: argparse.Namespace) -> int:
    if not args.cv and not args.profile:
        return fail("usage", "pass --cv <path>, --profile <slug>, or both")
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        return fail("invalid_value", str(exc))
    try:
        payload = salary.analyze_salary(
            cv_path=Path(args.cv) if args.cv else None,
            profile_slug=args.profile,
            run_id=args.run,
            scan_runs=not args.no_runs,
            as_of=as_of,
        )
    except cv.CvExtractionError as exc:
        return fail(exc.code, str(exc))
    except runs.RunNotFoundError as exc:
        return fail("run_not_found", str(exc))
    except FileNotFoundError as exc:
        return fail("profile_not_found", str(exc))
    except (OSError, ValueError) as exc:
        return fail("salary_analysis_failed", str(exc))
    emit(payload)
    return 0


def _load_estimate_source(source: Path) -> dict:
    raw = source.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) if source.suffix.lower() in {".yaml", ".yml"} else json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("salary estimate file must contain a JSON object or YAML mapping")
    if "expected_salary" in data and isinstance(data["expected_salary"], dict):
        return data["expected_salary"]
    return data


def _save(args: argparse.Namespace) -> int:
    if not args.profile and not args.report and not args.out:
        return fail("usage", "pass --profile <slug> and/or --report [--out <path>]")
    source = Path(args.source)
    if not source.is_file():
        return fail("not_found", f"salary estimate file not found: {args.source}")
    try:
        estimate_data = _load_estimate_source(source)
    except (OSError, UnicodeDecodeError, TypeError, ValueError, yaml.YAMLError) as exc:
        return fail("invalid_input", str(exc))

    report_path: Path | None = None
    if args.out:
        report_path = Path(args.out)
    elif args.report:
        slug_part = args.profile or "estimate"
        report_path = home.ultron_home() / "salary" / f"{slug_part}-salary.md"

    try:
        payload = salary.save_salary_estimate(
            estimate_data,
            profile_slug=args.profile,
            dry_run=args.dry_run,
            report_path=report_path,
        )
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in err['loc']) or 'estimate'}: {err['msg']}" for err in exc.errors()]
        emit({"error": "invalid_salary_estimate", "detail": "; ".join(errors), "errors": errors})
        return 1
    except FileNotFoundError as exc:
        return fail("profile_not_found", str(exc))
    except salary.SalaryProfileSlugError as exc:
        return fail("profile_slug_mismatch", str(exc))
    except (OSError, ValueError) as exc:
        return fail("salary_save_failed", str(exc))
    emit(payload)
    return 0


def register(subparsers) -> None:
    """Register ``ultron salary`` subcommands."""
    parser = subparsers.add_parser("salary", help="expected salary estimation and market benchmarks")
    sub = parser.add_subparsers(dest="salary_command", required=True, parser_class=type(parser))

    analyze = sub.add_parser(
        "analyze", help="extract verified CV experience, mine run salary disclosures, and build research queries"
    )
    analyze.add_argument("--cv", help="path to the CV file (PDF, .docx, .txt, .md)")
    analyze.add_argument("--profile", help="saved or bundled profile slug")
    analyze.add_argument("--run", help="specific run id to mine for disclosed salaries")
    analyze.add_argument("--no-runs", action="store_true", help="skip scanning past runs for disclosed salaries")
    analyze.add_argument("--as-of", help="reference date YYYY-MM-DD for open-ended present roles")
    analyze.set_defaults(func=_analyze)

    save = sub.add_parser(
        "save", help="validate an expected salary estimate and save it to a profile and/or Markdown report"
    )
    save.add_argument("--from", dest="source", required=True, help="JSON or YAML file with SalaryEstimate fields")
    save.add_argument("--profile", help="profile slug to update with expected_salary (versioned & undoable)")
    save.add_argument("--dry-run", action="store_true", help="validate and preview diff without writing files")
    save.add_argument("--report", action="store_true", help="also write a Markdown benchmark report")
    save.add_argument("--out", help="custom Markdown report output path (implies --report)")
    save.set_defaults(func=_save)

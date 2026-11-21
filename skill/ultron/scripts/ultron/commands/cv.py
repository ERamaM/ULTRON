"""``ultron cv`` subcommands: ``extract``, ``audit``, ``verify-truth``, and ``build-docx``."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from ultron.commands.output import emit, fail
from ultron.cv import ats as cv_ats
from ultron.cv import docx_writer as cv_docx_writer
from ultron.cv import extract as cv
from ultron.cv import truth as cv_truth
from ultron.infra import home
from ultron.storage import runs


def _parse_as_of(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError(f"invalid --as-of date {raw!r}: expected YYYY-MM-DD") from exc


def _extract(args: argparse.Namespace) -> int:
    source = Path(args.path)
    try:
        result = cv.extract_cv(source)
    except cv.CvExtractionError as exc:
        return fail(exc.code, str(exc))
    out_dir = home.ultron_home() / "cv"
    out_dir.mkdir(parents=True, exist_ok=True)
    # The full file name keeps cv.pdf and cv.docx from overwriting each other's text (cv.pdf.txt, cv.docx.txt).
    target = out_dir / f"{source.name}.txt"
    target.write_text(result.text, encoding="utf-8")
    emit({
        "path": str(target),
        "chars": len(result.text),
        "pages": result.pages,
        "source_cv": str(source.resolve()),
    })
    return 0


def _audit(args: argparse.Namespace) -> int:
    if args.job and args.job_file:
        return fail("usage", "pass either --job or --job-file, not both")
    try:
        as_of = _parse_as_of(args.as_of)
    except ValueError as exc:
        return fail("invalid_value", str(exc))
    try:
        payload = cv_ats.audit_cv(
            Path(args.path),
            profile_slug=args.profile,
            job_arg=args.job,
            job_file=Path(args.job_file) if args.job_file else None,
            run_id=args.run,
            fetch=not args.no_fetch,
            as_of=as_of,
            out_path=Path(args.out) if args.out else None,
        )
    except cv.CvExtractionError as exc:
        return fail(exc.code, str(exc))
    except runs.RunNotFoundError as exc:
        return fail("run_not_found", str(exc))
    except FileNotFoundError as exc:
        code = "profile_not_found" if "profile not found" in str(exc) else "not_found"
        return fail(code, str(exc))
    except (OSError, ValueError) as exc:
        return fail("audit_failed", str(exc))
    emit(payload)
    return 0


def _verify_truth(args: argparse.Namespace) -> int:
    try:
        as_of = _parse_as_of(args.as_of)
        confirmed = cv_truth.load_confirmed_facts(Path(args.confirmed) if args.confirmed else None)
        report = cv_truth.verify_truth(
            Path(args.source),
            Path(args.optimized),
            confirmed_facts=confirmed,
            as_of=as_of,
        )
    except cv.CvExtractionError as exc:
        return fail(exc.code, str(exc))
    except FileNotFoundError as exc:
        return fail("not_found", str(exc))
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid_input", str(exc))
    emit(report)
    return 1 if (args.strict and not report["passed"]) else 0


def _build_docx(args: argparse.Namespace) -> int:
    try:
        confirmed = cv_truth.load_confirmed_facts(Path(args.confirmed) if args.confirmed else None)
        payload = cv_docx_writer.build_ats_docx(
            Path(args.source),
            out_path=Path(args.out) if args.out else None,
            font=args.font,
            force=args.force,
            source_cv=Path(args.source_cv) if args.source_cv else None,
            confirmed_facts=confirmed,
            strict=args.strict,
        )
    except cv_docx_writer.TruthfulnessError as exc:
        emit({"error": "truthfulness_failed", "detail": str(exc), "truth": exc.truth_report})
        return 1
    except cv.CvExtractionError as exc:
        return fail(exc.code, str(exc))
    except FileExistsError as exc:
        return fail("output_exists", str(exc))
    except FileNotFoundError as exc:
        return fail("not_found", str(exc))
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid_input", str(exc))
    emit(payload)
    return 0


def register(subparsers) -> None:
    parser = subparsers.add_parser("cv", help="CV helpers")
    sub = parser.add_subparsers(dest="cv_command", required=True, parser_class=type(parser))

    extract = sub.add_parser(
        "extract", help="extract CV text (PDF, .docx, .txt, .md) to $ULTRON_HOME/cv/<file name>.txt"
    )
    extract.add_argument("path")
    extract.set_defaults(func=_extract)

    audit = sub.add_parser(
        "audit",
        help="audit a CV (.docx, PDF, .tex, .txt, .md) for ATS formatting, structure, experience, and target alignment",
    )
    audit.add_argument("path")
    audit.add_argument("--profile", help="saved or bundled profile slug")
    audit.add_argument("--job", help="target job identity_key or URL (local runs first, else fetched)")
    audit.add_argument("--job-file", help="path to a plain-text or Markdown job description file")
    audit.add_argument("--run", help="run id to search for --job or aggregate target keywords")
    audit.add_argument("--no-fetch", action="store_true", help="do not fetch --job URL if absent from local runs")
    audit.add_argument("--as-of", help="reference date YYYY-MM-DD for open-ended present roles")
    audit.add_argument("--out", help="custom path for the Markdown audit report")
    audit.set_defaults(func=_audit)

    verify = sub.add_parser(
        "verify-truth",
        help="verify an optimized CV against the source CV to catch fabricated dates, metrics, credentials, or terms",
    )
    verify.add_argument("--source", required=True, help="source CV path")
    verify.add_argument("--optimized", required=True, help="optimized CV draft path (.md, .txt, .docx, .json)")
    verify.add_argument("--confirmed", help="optional JSON or text file listing user-confirmed extra facts")
    verify.add_argument("--as-of", help="reference date YYYY-MM-DD for open-ended present roles")
    verify.add_argument("--strict", action="store_true", help="exit 1 if any truthfulness violation is detected")
    verify.set_defaults(func=_verify_truth)

    build = sub.add_parser(
        "build-docx",
        help="build a clean single-column ATS-preferred .docx CV from a Markdown or JSON draft",
    )
    build.add_argument("--from", dest="source", required=True, help="path to Markdown (.md/.txt) or JSON CV draft")
    build.add_argument("--out", help="custom output .docx path (default: $ULTRON_HOME/cv/optimized/<stem>-ats.docx)")
    build.add_argument("--source-cv", help="optional original CV path to run verify-truth before/during build")
    build.add_argument("--confirmed", help="optional JSON or text file listing user-confirmed extra facts")
    build.add_argument("--font", default="Calibri", help="ATS-safe font name (default: Calibri)")
    build.add_argument("--force", action="store_true", help="overwrite an existing .docx output file")
    build.add_argument("--strict", action="store_true", help="refuse to write .docx if verify-truth fails")
    build.set_defaults(func=_build_docx)

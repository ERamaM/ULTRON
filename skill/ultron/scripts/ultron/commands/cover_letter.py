"""``ultron cover-letter`` CLI command group: templates, brief preparation, truthfulness verification, and .docx export."""

from __future__ import annotations

import argparse
from pathlib import Path

from ultron.commands._common import parse_as_of, run_build_docx, run_verify_truth
from ultron.commands.output import emit, fail
from ultron.cover_letter import brief, builder, templates, truth
from ultron.cv import extract as cv
from ultron.cv import truth as cv_truth
from ultron.infra.ooxml import WRITER_SAFE_FONTS
from ultron.storage import runs


def _templates(args: argparse.Namespace) -> int:
    if args.export:
        try:
            payload = templates.export_starter_templates(
                Path(args.export),
                template=args.template,
                force=args.force,
            )
        except FileExistsError as exc:
            return fail("output_exists", str(exc))
        except (OSError, ValueError) as exc:
            return fail("invalid_input", str(exc))
        emit(payload)
        return 0

    if args.template:
        key = args.template.strip().lower()
        if key not in templates.BUNDLED_TEMPLATES:
            return fail(
                "invalid_value",
                f"unknown template {args.template!r}; choose from {', '.join(templates.BUNDLED_TEMPLATES)}",
            )
        emit({
            "templates": [dict(templates.BUNDLED_TEMPLATES[key])],
            "placeholders": [f"{{{{{name}}}}}" for name in templates.TEMPLATE_PLACEHOLDERS],
            "multiline_placeholders": ["{{body}}", "{{paragraphs}}", "{{highlights}}"],
            "safe_fonts": sorted(WRITER_SAFE_FONTS),
        })
        return 0

    emit(templates.list_templates())
    return 0


def _prepare(args: argparse.Namespace) -> int:
    if args.job and args.job_file:
        return fail("usage", "pass either --job or --job-file, not both")
    try:
        as_of = parse_as_of(args.as_of)
    except ValueError as exc:
        return fail("invalid_value", str(exc))
    try:
        payload = brief.prepare_cover_letter_brief(
            cv_path=Path(args.cv) if args.cv else None,
            profile_slug=args.profile,
            job=args.job,
            job_file=Path(args.job_file) if args.job_file else None,
            run_id=args.run,
            fetch=not args.no_fetch,
            lang=args.lang,
            tone=args.tone,
            as_of=as_of,
        )
    except cv.CvExtractionError as exc:
        return fail(exc.code, str(exc))
    except runs.RunNotFoundError as exc:
        return fail("run_not_found", str(exc))
    except FileNotFoundError as exc:
        code = "profile_not_found" if "profile not found" in str(exc) else "not_found"
        return fail(code, str(exc))
    except (OSError, ValueError) as exc:
        return fail("prepare_failed", str(exc))
    emit(payload)
    return 0


def _verify_truth(args: argparse.Namespace) -> int:
    def _run():
        as_of = parse_as_of(args.as_of)
        confirmed = cv_truth.load_confirmed_facts(Path(args.confirmed) if args.confirmed else None)
        return truth.verify_cover_letter_truth(
            Path(args.source_cv),
            Path(args.draft),
            confirmed_facts=confirmed,
            target_company=args.company,
            as_of=as_of,
        )

    return run_verify_truth(_run, strict=args.strict)


def _build_docx(args: argparse.Namespace) -> int:
    def _run():
        as_of = parse_as_of(args.as_of)
        confirmed = cv_truth.load_confirmed_facts(Path(args.confirmed) if args.confirmed else None)
        return builder.build_cover_letter_docx(
            Path(args.source),
            out_path=Path(args.out) if args.out else None,
            out_md=Path(args.out_md) if args.out_md else None,
            template=args.template,
            template_file=Path(args.template_file) if args.template_file else None,
            font=args.font,
            force=args.force,
            source_cv=Path(args.source_cv) if args.source_cv else None,
            confirmed_facts=confirmed,
            strict=args.strict,
            as_of=as_of,
        )

    return run_build_docx(_run)


def register(subparsers) -> None:
    """Register ``ultron cover-letter`` subcommands."""
    parser = subparsers.add_parser("cover-letter", help="Cover letter preparation and Word export")
    sub = parser.add_subparsers(dest="cover_letter_command", required=True, parser_class=type(parser))

    tpl = sub.add_parser(
        "templates",
        help="list bundled Word (.docx) templates and placeholders, or export customizable starter templates",
    )
    tpl.add_argument("--export", help="directory to export starter .docx template files with {{placeholders}}")
    tpl.add_argument("--template", help="template name: classic, modern, or executive")
    tpl.add_argument("--force", action="store_true", help="overwrite existing exported starter .docx files")
    tpl.set_defaults(func=_templates)

    prep = sub.add_parser(
        "prepare",
        help="prepare a structured brief pairing verified CV achievements with a target job or profile",
    )
    prep.add_argument("--cv", help="path to source CV (.pdf, .docx, .tex, .txt, .md); optional if --profile sets source_cv")
    prep.add_argument("--profile", help="saved or bundled profile slug")
    prep.add_argument("--job", help="target job identity_key or URL (local runs first, else SSRF-safe fetch)")
    prep.add_argument("--job-file", help="path to a plain-text or Markdown job description file")
    prep.add_argument("--run", help="run id to search for --job")
    prep.add_argument("--no-fetch", action="store_true", help="do not fetch --job URL if absent from local runs")
    prep.add_argument("--lang", help="override auto-detected language code (en, es, de, fr, pt, it)")
    prep.add_argument(
        "--tone",
        default="direct",
        choices=list(brief.VALID_TONES),
        help="writing register (default: direct)",
    )
    prep.add_argument("--as-of", help="reference date YYYY-MM-DD for open-ended present roles")
    prep.set_defaults(func=_prepare)

    verify = sub.add_parser(
        "verify-truth",
        help="verify a cover letter draft against a source CV to catch fabricated dates, metrics, credentials, or skills",
    )
    verify.add_argument("--source-cv", required=True, help="source CV path")
    verify.add_argument("--draft", required=True, help="cover letter draft path (.json, .md, .txt, .docx)")
    verify.add_argument("--confirmed", help="optional JSON or text file listing user-confirmed extra facts")
    verify.add_argument("--company", help="optional target company name to authorize company vocabulary in draft")
    verify.add_argument("--strict", action="store_true", help="exit 1 if any truthfulness violation is detected")
    verify.add_argument("--as-of", help="reference date YYYY-MM-DD for letter date year and experience tenure")
    verify.set_defaults(func=_verify_truth)

    build = sub.add_parser(
        "build-docx",
        help="render a Markdown or JSON cover letter draft to a single-column Word (.docx) file",
    )
    build.add_argument("--from", dest="source", required=True, help="path to Markdown (.md/.txt) or JSON cover letter draft")
    build.add_argument("--out", help="custom output .docx path (default: $ULTRON_HOME/cover-letters/<slug>.docx)")
    build.add_argument("--out-md", help="optional clean Markdown output path")
    build.add_argument(
        "--template",
        default="modern",
        help="bundled Word template: classic, modern, or executive (default: modern)",
    )
    build.add_argument("--template-file", help="custom Word .docx template file containing {{placeholders}}")
    build.add_argument("--font", help="override ATS-safe font name (Calibri, Georgia, Cambria, etc.)")
    build.add_argument("--source-cv", help="optional original CV path to run verify-truth before/during build")
    build.add_argument("--confirmed", help="optional JSON or text file listing user-confirmed extra facts")
    build.add_argument("--strict", action="store_true", help="refuse to write output files if verify-truth fails")
    build.add_argument("--force", action="store_true", help="overwrite existing output .docx/.md files")
    build.add_argument("--as-of", help="reference date YYYY-MM-DD for letter date and experience verification")
    build.set_defaults(func=_build_docx)

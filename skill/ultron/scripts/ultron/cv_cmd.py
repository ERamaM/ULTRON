"""``ultron cv`` subcommands."""

from __future__ import annotations

import argparse
from pathlib import Path

from ultron import cv, home
from ultron.output import emit, fail


def _extract(args: argparse.Namespace) -> int:
    source = Path(args.path)
    try:
        result = cv.extract_cv(source)
    except cv.CvExtractionError as exc:
        return fail(exc.code, str(exc))
    out_dir = home.ultron_home() / "cv"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{source.stem}.txt"
    target.write_text(result.text, encoding="utf-8")
    emit({
        "path": str(target),
        "chars": len(result.text),
        "pages": result.pages,
        "source_cv": str(source.resolve()),
    })
    return 0


def register(subparsers) -> None:
    parser = subparsers.add_parser("cv", help="CV helpers")
    sub = parser.add_subparsers(dest="cv_command", required=True, parser_class=type(parser))
    extract = sub.add_parser("extract", help="extract CV text (PDF, .txt, .md) to $ULTRON_HOME/cv/<stem>.txt")
    extract.add_argument("path")
    extract.set_defaults(func=_extract)

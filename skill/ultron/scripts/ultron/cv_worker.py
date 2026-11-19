"""Bounded PDF page-text extraction for a hard-timeout child process (``python -s -P -m ultron.cv_worker``).

The parent owns the lifetime of this process. This module exits 0 unless the
interpreter itself fails, and reports the outcome as one JSON object on stdout.
"""

from __future__ import annotations

import json
import re
import sys
from io import BytesIO


def _normalize_text(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in normalized.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def extract_pdf_pages_text(content: bytes, *, max_pages: int, max_chars: int) -> list[str]:
    """Return normalized text for each non-empty page, enforcing both budgets."""

    from ultron.cv import CvDependencyMissing, CvExtractionError, CvPdfTooManyPages, CvTextTooLong

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # optional dependency: ultron[cv]
        raise CvDependencyMissing() from exc

    try:
        reader = PdfReader(BytesIO(content))
        pages = reader.pages
        if len(pages) > max_pages:
            raise CvPdfTooManyPages(max_pages)
        chunks: list[str] = []
        used_chars = 0
        for page in pages:
            page_text = _normalize_text(page.extract_text() or "")
            added = len(page_text) + (1 if chunks and page_text else 0)
            # Stop before extracting later pages once the explicit text
            # budget is exhausted; never materialize an unbounded suffix.
            if used_chars + added > max_chars:
                raise CvTextTooLong(used_chars + added, max_chars)
            if page_text:
                chunks.append(page_text)
                used_chars += added
    except CvExtractionError:
        raise
    except Exception as exc:
        raise CvExtractionError("Could not extract text from PDF", code="no_extractable_text") from exc
    return chunks


def _limits(argv: list[str]) -> tuple[int, int]:
    """Read max_pages and max_chars from the fixed argv tail."""

    if len(argv) != 3:
        raise ValueError("PDF worker requires max_pages and max_chars")
    return int(argv[1]), int(argv[2])


def _payload(content: bytes, max_pages: int, max_chars: int) -> dict[str, object]:
    from ultron.cv import CvDependencyMissing, CvPdfTooManyPages, CvTextTooLong

    try:
        pages = extract_pdf_pages_text(content, max_pages=max_pages, max_chars=max_chars)
    except CvPdfTooManyPages as exc:
        return {"error": "too_many_pages", "limit": exc.limit}
    except CvTextTooLong as exc:
        return {"error": "too_long", "length": exc.length, "limit": exc.limit}
    except CvDependencyMissing:
        return {"error": "dependency_missing"}
    except Exception:
        return {"error": "extract_failed"}
    return {"pages": pages}


def main() -> None:
    """Read one PDF from stdin and write one JSON object to stdout."""

    try:
        max_pages, max_chars = _limits(sys.argv)
        payload = _payload(sys.stdin.buffer.read(), max_pages, max_chars)
    except Exception:
        payload = {"error": "extract_failed"}
    sys.stdout.buffer.write(json.dumps(payload).encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()

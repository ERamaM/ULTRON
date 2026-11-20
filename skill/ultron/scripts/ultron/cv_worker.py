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
    from ultron.cv import _normalize_text as normalize

    return normalize(text)


_MIN_GUTTER = 3  # blank character columns that must separate two text columns
_DATE_LIKE = re.compile(r"\b(19|20)\d{2}\b|\b(present|current|today|actualidad|actual|hoy|presente)\b", re.IGNORECASE)


def _crosses(line: str, col: int) -> bool:
    return bool(line[max(col - 1, 0) : col + _MIN_GUTTER - 1].strip())


def _gutter_body(lines: list[str], col: int) -> tuple[int, int] | None:
    """``(start, end)`` of the lines that respect a gutter at ``col``; only a header and a footer may cross it."""
    crossing = [i for i, line in enumerate(lines) if _crosses(line, col)]
    start = 0
    while start < len(lines) and start in crossing:
        start += 1
    end = len(lines)
    while end > start and end - 1 in crossing:
        end -= 1
    if any(start <= i < end for i in crossing):
        return None
    return start, end


def _two_columns(lines: list[str], col: int, start: int, end: int) -> tuple[list[str], list[str]] | None:
    left = [line[:col].strip() for line in lines[start:end]]
    right = [line[col:].strip() for line in lines[start:end]]
    both = sum(1 for a, b in zip(left, right, strict=True) if a and b)
    one_sided = sum(1 for a, b in zip(left, right, strict=True) if bool(a) != bool(b))
    right_cells = [b for b in right if b]
    # Real side-by-side columns run independently: some rows hold only one of them, and the right
    # column is a dense block of consecutive lines. Right-aligned details of a single column (dates,
    # cities, a contact line) are sparse, one per role line, and must stay on their line.
    if both < 3 or one_sided < 2 or not any(b and not a for a, b in zip(left, right, strict=True)):
        return None
    if len(right_cells) * 5 < len(right) * 2 or _longest_run(right) < 3:
        return None
    if sum(1 for b in right_cells if _DATE_LIKE.search(b)) * 2 >= len(right_cells):
        return None
    return [a for a in left if a], right_cells


def _longest_run(cells: list[str]) -> int:
    best = run = 0
    for cell in cells:
        run = run + 1 if cell else 0
        best = max(best, run)
    return best


def reorder_columns(layout_text: str) -> str | None:
    """Rebuild reading order of a two-column page from pypdf's layout-mode text, or ``None`` when unsure.

    Layout mode keeps horizontal positions, so a column gutter is a band of blank character columns
    shared by every line between an optional full-width header and footer. The result lists the header,
    the whole left column, the whole right column, then the footer.
    """
    lines = [line.rstrip() for line in layout_text.split("\n") if line.strip()]
    if len(lines) < 4:
        return None
    width = max(len(line) for line in lines)
    best: tuple[int, int, int, list[str], list[str]] | None = None
    for col in range(width // 4, (width * 3) // 4):
        body = _gutter_body(lines, col)
        if body is None or body[1] - body[0] < 4:
            continue
        columns = _two_columns(lines, col, *body)
        if columns is not None and (best is None or body[1] - body[0] > best[1] - best[0]):
            best = (body[0], body[1], col, *columns)
    if best is None:
        return None
    start, end, _col, left, right = best
    blocks = ([line.strip() for line in lines[:start]], left, right, [line.strip() for line in lines[end:]])
    return "\n\n".join("\n".join(block) for block in blocks if block)


def _page_text(page) -> str:
    plain = page.extract_text() or ""
    try:
        layout = page.extract_text(extraction_mode="layout")
        reordered = reorder_columns(layout) if isinstance(layout, str) else None
    except Exception:  # older pypdf, odd fonts or a stub page: keep plain extraction
        return plain
    return reordered or plain


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
            page_text = _normalize_text(_page_text(page))
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

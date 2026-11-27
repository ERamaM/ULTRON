"""Bounded PDF page-text extraction for a hard-timeout child process (``python -s -P -m ultron.cv.worker``).

The parent owns the lifetime of this process. This module exits 0 unless the
interpreter itself fails, and reports the outcome as one JSON object on stdout.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from io import BytesIO


def _normalize_text(text: str) -> str:
    from ultron.cv.extract import _normalize_text as normalize

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
            left_column, right_column = columns
            best = (body[0], body[1], col, left_column, right_column)
    if best is None:
        return None
    start, end, _col, left, right = best
    blocks = ([line.strip() for line in lines[:start]], left, right, [line.strip() for line in lines[end:]])
    return "\n\n".join("\n".join(block) for block in blocks if block)


def _words(text: str) -> Counter:
    return Counter(re.findall(r"\w+", text.casefold()))


def _page_text(page) -> str:
    text, _two_col, _rotated = _inspect_page_text(page)
    return text


def _inspect_page_text(page) -> tuple[str, bool, bool]:
    plain = page.extract_text() or ""
    try:
        layout = page.extract_text(extraction_mode="layout")
        reordered = reorder_columns(layout) if isinstance(layout, str) else None
    except Exception:  # older pypdf, odd fonts or a stub page: keep plain extraction
        return plain, False, False
    if reordered:
        if not (_words(plain) - _words(reordered)):
            return reordered, True, False
        # Layout mode drops rotated text: report it and keep the plain extraction.
        return plain, False, True
    return plain, False, False


def _deref(obj):
    if hasattr(obj, "get_object"):
        try:
            return obj.get_object()
        except Exception:
            return obj
    return obj


def _obj_key(raw, resolved) -> tuple[str, int, int] | int:
    ref = getattr(raw, "indirect_reference", None)
    if ref is not None and hasattr(ref, "idnum"):
        return ("ref", int(ref.idnum), int(getattr(ref, "generation", 0)))
    return id(resolved)


def _walk_page_resources(
    page,
    fonts: set[str],
    *,
    max_fonts: int = 30,
    max_depth: int = 8,
    max_objects: int = 500,
) -> int:
    """Walk PDF page /Resources safely (bounded depth, object cap, cycle-safe) for fonts and images."""
    if not hasattr(page, "get"):
        return 0
    seen: set[object] = set()
    visited = 0
    images = 0

    def walk_resources(res_raw, depth: int) -> None:
        nonlocal visited, images
        if depth > max_depth or visited >= max_objects or res_raw is None:
            return
        res = _deref(res_raw)
        if not hasattr(res, "get"):
            return
        key = _obj_key(res_raw, res)
        if key in seen:
            return
        seen.add(key)
        visited += 1

        if len(fonts) < max_fonts:
            font_dict = _deref(res.get("/Font"))
            if hasattr(font_dict, "values"):
                for f_raw in font_dict.values():
                    if visited >= max_objects or len(fonts) >= max_fonts:
                        break
                    visited += 1
                    f_obj = _deref(f_raw)
                    if hasattr(f_obj, "get"):
                        base = str(f_obj.get("/BaseFont") or "").lstrip("/")
                        base = re.sub(r"^[A-Z]{6}\+", "", base)
                        if base:
                            fonts.add(base[:100])

        xobjs = _deref(res.get("/XObject"))
        if hasattr(xobjs, "values"):
            for x_raw in xobjs.values():
                if visited >= max_objects:
                    break
                x_obj = _deref(x_raw)
                x_key = _obj_key(x_raw, x_obj)
                if x_key in seen:
                    continue
                seen.add(x_key)
                visited += 1
                if hasattr(x_obj, "get"):
                    subtype = str(x_obj.get("/Subtype") or "")
                    if subtype == "/Image":
                        images += 1
                    elif subtype == "/Form":
                        walk_resources(x_obj.get("/Resources"), depth + 1)

    walk_resources(page.get("/Resources"), 0)
    return images


def _meta_str(meta, key: str) -> str:
    if meta is None:
        return ""
    val = None
    if hasattr(meta, "get"):
        val = meta.get(key)
    if not val:
        attr = key.lstrip("/").lower()
        val = getattr(meta, attr, None)
    if not val:
        return ""
    from ultron.core.privacy import redact

    text = " ".join(str(redact(str(val))).split())
    return text[:200]


def extract_pdf_pages_text(content: bytes, *, max_pages: int, max_chars: int) -> list[str]:
    """Return normalized text for each non-empty page, enforcing both budgets."""
    pages, _meta = extract_pdf_inspection(content, max_pages=max_pages, max_chars=max_chars, collect_metadata=False)
    return pages


def extract_pdf_inspection(
    content: bytes, *, max_pages: int, max_chars: int, collect_metadata: bool = True
) -> tuple[list[str], dict[str, object]]:
    """Return ``(pages, inspection_metadata)`` while enforcing page and character budgets."""

    from ultron.cv.extract import CvDependencyMissing, CvExtractionError, CvPdfTooManyPages, CvTextTooLong

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # optional dependency: ultron[cv]
        raise CvDependencyMissing() from exc

    two_col_pages: list[int] = []
    rotated_pages: list[int] = []
    chars_per_page: list[int] = []
    fonts: set[str] = set()
    image_count = 0
    raw_ligatures = 0
    raw_pua = 0
    raw_replacement = 0

    try:
        reader = PdfReader(BytesIO(content))
        pages = reader.pages
        if len(pages) > max_pages:
            raise CvPdfTooManyPages(max_pages)
        chunks: list[str] = []
        used_chars = 0
        for idx, page in enumerate(pages, start=1):
            if collect_metadata:
                raw_text, is_two_col, is_rotated = _inspect_page_text(page)
                if is_two_col:
                    two_col_pages.append(idx)
                if is_rotated:
                    rotated_pages.append(idx)
                for ch in raw_text:
                    code = ord(ch)
                    if 0xFB00 <= code <= 0xFB06:
                        raw_ligatures += 1
                    elif 0xE000 <= code <= 0xF8FF:
                        raw_pua += 1
                    elif code == 0xFFFD:
                        raw_replacement += 1
                image_count += _walk_page_resources(page, fonts)
            else:
                raw_text = _page_text(page)

            page_text = _normalize_text(raw_text)
            added = len(page_text) + (1 if chunks and page_text else 0)
            # Stop before extracting later pages once the explicit text
            # budget is exhausted; never materialize an unbounded suffix.
            if used_chars + added > max_chars:
                raise CvTextTooLong(used_chars + added, max_chars)
            if collect_metadata:
                chars_per_page.append(len(page_text))
            if page_text:
                chunks.append(page_text)
                used_chars += added
    except CvExtractionError:
        raise
    except Exception as exc:
        raise CvExtractionError("Could not extract text from PDF", code="no_extractable_text") from exc

    if not collect_metadata:
        return chunks, {"page_count": len(pages)}

    raw_meta = getattr(reader, "metadata", None)
    metadata: dict[str, object] = {
        "page_count": len(pages),
        "is_encrypted": bool(getattr(reader, "is_encrypted", False)),
        "producer": _meta_str(raw_meta, "/Producer"),
        "creator": _meta_str(raw_meta, "/Creator"),
        "two_column_pages": two_col_pages,
        "rotated_text_pages": rotated_pages,
        "image_count": image_count,
        "fonts": sorted(fonts),
        "raw_ligature_count": raw_ligatures,
        "private_use_glyph_count": raw_pua,
        "replacement_char_count": raw_replacement,
        "chars_per_page": chars_per_page,
    }
    return chunks, metadata


def _limits(argv: list[str]) -> tuple[int, int, bool]:
    """Read max_pages, max_chars and optional ``--inspect`` flag from argv."""

    if len(argv) == 3:
        return int(argv[1]), int(argv[2]), False
    if len(argv) == 4 and argv[3] == "--inspect":
        return int(argv[1]), int(argv[2]), True
    raise ValueError("PDF worker requires max_pages and max_chars (and optional --inspect)")


def _payload(content: bytes, max_pages: int, max_chars: int, *, inspect: bool = False) -> dict[str, object]:
    from ultron.cv.extract import CvDependencyMissing, CvPdfTooManyPages, CvTextTooLong

    try:
        pages, metadata = extract_pdf_inspection(
            content, max_pages=max_pages, max_chars=max_chars, collect_metadata=inspect,
        )
        return {"pages": pages, "metadata": metadata}
    except CvPdfTooManyPages as exc:
        return {"error": "too_many_pages", "limit": exc.limit}
    except CvTextTooLong as exc:
        return {"error": "too_long", "length": exc.length, "limit": exc.limit}
    except CvDependencyMissing:
        return {"error": "dependency_missing"}
    except Exception:
        return {"error": "extract_failed"}


def main() -> None:
    """Read one PDF from stdin and write one JSON object to stdout."""

    try:
        max_pages, max_chars, inspect = _limits(sys.argv)
        payload = _payload(sys.stdin.buffer.read(), max_pages, max_chars, inspect=inspect)
    except Exception:
        payload = {"error": "extract_failed"}
    sys.stdout.buffer.write(json.dumps(payload).encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()

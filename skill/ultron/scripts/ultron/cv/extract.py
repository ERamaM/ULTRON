"""CV text extraction from PDF, Word ``.docx`` or plain-text files (port of MARK III ``cv/text_extractor.py``).

Word files are read with the standard library (``ultron.cv.docx_reader``). PDF parsing runs in a child interpreter bounded by a hard timeout (``ultron.cv.worker``). Text is
never silently truncated: over-budget input is rejected with a typed ``CvExtractionError``.

``CvExtractionError.code`` is one of:
``no_extractable_text``, ``too_many_pages``, ``too_long``, ``timeout``, ``unsupported_type``,
``dependency_missing`` (pypdf is optional: ``pip install ultron[cv]``) and ``worker_failed``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import ultron

MAX_CV_BYTES = 2_500_000
MAX_CV_CHARS = 60_000
MAX_CV_PAGES = 20
PDF_TIMEOUT_SECONDS = 20.0
TEXT_SUFFIXES = {".txt", ".md"}
DOCX_SUFFIX = ".docx"


class CvExtractionError(ValueError):
    """Raised when CV input cannot be converted into bounded plain text."""

    def __init__(self, message: str, *, code: str = "no_extractable_text") -> None:
        super().__init__(message)
        self.code = code


class CvTextTooLong(CvExtractionError):
    """The complete CV exceeds the explicit character budget."""

    def __init__(self, length: int, limit: int) -> None:
        self.length = length
        self.limit = limit
        super().__init__(
            f"CV text is {length} characters; the maximum is {limit}. "
            "No partial profile was generated. Shorten the CV and retry.",
            code="too_long",
        )


class CvPdfTooManyPages(CvExtractionError):
    """The PDF exceeds the explicit page budget before extraction completes."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(
            f"CV PDF has more than {limit} pages. No partial profile was generated. Use a shorter CV and retry.",
            code="too_many_pages",
        )


class CvPdfTimeout(CvExtractionError):
    """Raised when PDF extraction exceeds the hard time limit."""

    def __init__(self) -> None:
        super().__init__("PDF extraction timed out", code="timeout")


class CvDependencyMissing(CvExtractionError):
    """The optional ``pypdf`` package is not installed."""

    def __init__(self) -> None:
        super().__init__(
            "PDF extraction requires the pypdf package (pip install 'ultron[cv]')", code="dependency_missing"
        )


class PdfRunner(Protocol):
    """Callable that returns normalized PDF page texts or raises a CV extraction error."""

    def __call__(self, content: bytes, *, max_pages: int, max_chars: int, timeout_seconds: float) -> list[str]:
        """Return one normalized text per extracted PDF page."""


_PDF_WORKER_ENV_KEYS = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "PYTHONPATH")
_FAILED = "Could not extract text from PDF"


def _pdf_worker_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in _PDF_WORKER_ENV_KEYS if key in os.environ}
    pkg_root = str(Path(ultron.__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p] if existing else []
    if pkg_root not in parts:
        parts.append(pkg_root)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _pdf_worker_flags() -> list[str]:
    # -s isolates from user site-packages; -P (Python >= 3.11) isolates sys.path from cwd
    # without -I's -E flag that strips PYTHONPATH.
    return ["-s", "-P"]


def _stderr_tail(stderr: bytes, *, max_chars: int = 240) -> str:
    from ultron.core.privacy import redact

    text = " ".join(stderr.decode("utf-8", errors="replace").strip().splitlines())
    if len(text) > max_chars:
        text = "..." + text[-(max_chars - 3) :]
    return str(redact(text)) if text else ""


def _json_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _worker_payload(stdout: bytes, returncode: int, *, max_chars: int, stderr: bytes = b"") -> dict:
    if len(stdout) > max_chars * 8 + 65536:
        raise CvExtractionError("PDF extraction output is too large", code="too_long")
    if returncode != 0:
        tail = _stderr_tail(stderr)
        detail = f"PDF worker exited with status {returncode}: {tail}" if tail else f"PDF worker exited with status {returncode}"
        raise CvExtractionError(detail, code="worker_failed")
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CvExtractionError(_FAILED) from exc
    if not isinstance(payload, dict):
        raise CvExtractionError(_FAILED)
    if "error" not in payload:
        return payload
    error = payload.get("error")
    if error == "too_many_pages":
        limit = _json_int(payload.get("limit"))
        if limit is not None:
            raise CvPdfTooManyPages(limit)
    elif error == "too_long":
        length = _json_int(payload.get("length"))
        limit = _json_int(payload.get("limit"))
        if length is not None and limit is not None:
            raise CvTextTooLong(length, limit)
    elif error == "dependency_missing":
        raise CvDependencyMissing()
    raise CvExtractionError(_FAILED)


def _validate_pages(payload: dict) -> list[str]:
    pages = payload.get("pages")
    if isinstance(pages, list) and all(isinstance(page, str) for page in pages):
        return list(pages)
    raise CvExtractionError(_FAILED)


def _run_pdf_worker(
    content: bytes,
    *,
    max_pages: int,
    max_chars: int,
    timeout_seconds: float,
    extra_args: tuple[str, ...] = (),
) -> dict:
    try:
        completed = subprocess.run(
            [sys.executable, *_pdf_worker_flags(), "-m", "ultron.cv.worker", str(max_pages), str(max_chars), *extra_args],
            input=content,
            capture_output=True,
            timeout=timeout_seconds,
            env=_pdf_worker_env(),
            cwd=tempfile.gettempdir(),
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CvPdfTimeout() from exc
    stdout = completed.stdout
    if not isinstance(stdout, bytes):
        raise CvExtractionError(_FAILED)
    stderr = completed.stderr if isinstance(completed.stderr, bytes) else b""
    return _worker_payload(stdout, completed.returncode, max_chars=max_chars, stderr=stderr)


def subprocess_pdf_runner(content: bytes, *, max_pages: int, max_chars: int, timeout_seconds: float) -> list[str]:
    """Extract PDF text in a child interpreter bounded by ``timeout_seconds``."""

    payload = _run_pdf_worker(
        content, max_pages=max_pages, max_chars=max_chars, timeout_seconds=timeout_seconds
    )
    return _validate_pages(payload)


def subprocess_pdf_inspector(
    content: bytes,
    *,
    max_pages: int = MAX_CV_PAGES,
    max_chars: int = MAX_CV_CHARS,
    timeout_seconds: float = PDF_TIMEOUT_SECONDS,
) -> tuple[list[str], dict[str, object]]:
    """Extract PDF pages and ATS structural metadata in a bounded child interpreter."""

    payload = _run_pdf_worker(
        content,
        max_pages=max_pages,
        max_chars=max_chars,
        timeout_seconds=timeout_seconds,
        extra_args=("--inspect",),
    )
    pages = _validate_pages(payload)
    meta = payload.get("metadata")
    return pages, dict(meta) if isinstance(meta, dict) else {}


# Characters that word processors and typesetters leave in CV text: invisible ones are dropped, Latin
# ligatures are expanded (so U+FB01 + "nance" reads and greps as "finance"), and the private-use bullets of
# the Symbol/Wingdings fonts become a plain bullet.
_CHAR_FIXES: dict[int, str | None] = {
    **dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None),
    **{code: unicodedata.normalize("NFKC", chr(code)) for code in range(0xFB00, 0xFB07)},
    0xF0B7: "•",
    0xF0A7: "•",
    0x2028: "\n",
    0x2029: "\n",
}


def _normalize_text(text: str) -> str:
    """Collapse every kind of horizontal whitespace (no-break and em spaces too) and runs of blank lines."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").translate(_CHAR_FIXES)
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in normalized.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _decode_text_bytes(content: bytes) -> str:
    """Decode plain-text bytes honoring BOMs, then utf-8 with latin-1 fallback."""

    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig")
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return content.decode("utf-16")
        except UnicodeDecodeError as exc:  # odd byte count or lone surrogate after a UTF-16 BOM
            raise CvExtractionError("CV text is not valid UTF-16", code="no_extractable_text") from exc
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def _clean_text(text: str, max_chars: int) -> str:
    """Normalize whitespace, then reject empty text or text beyond the character budget (never slice)."""

    clean = _normalize_text(text)
    if not clean:
        raise CvExtractionError("CV text is empty", code="no_extractable_text")
    if len(clean) > max_chars:
        raise CvTextTooLong(len(clean), max_chars)
    return clean


@dataclass(frozen=True, slots=True)
class CvText:
    """Extracted CV plain text paired with its detected page count."""
    text: str
    pages: int


def extract_cv(
    path: Path,
    *,
    max_pages: int = MAX_CV_PAGES,
    max_chars: int = MAX_CV_CHARS,
    timeout: float = PDF_TIMEOUT_SECONDS,
    pdf_runner: PdfRunner | None = None,
) -> CvText:
    """Return the cleaned CV text and its page count (``.txt``/``.md`` count as one page)."""

    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in TEXT_SUFFIXES and suffix not in {".pdf", DOCX_SUFFIX}:
        raise CvExtractionError("Unsupported CV format. Use PDF, .docx, .txt or .md.", code="unsupported_type")
    try:
        if path.stat().st_size > MAX_CV_BYTES:
            raise CvExtractionError("CV file is too large", code="too_long")
        content = path.read_bytes()
    except OSError as exc:
        raise CvExtractionError(f"Cannot read CV file: {exc}", code="unsupported_type") from exc
    if suffix in TEXT_SUFFIXES:
        return CvText(_clean_text(_decode_text_bytes(content), max_chars), 1)
    if suffix == DOCX_SUFFIX:
        from ultron.cv.docx_reader import extract_docx_text

        # The same raw-text budget as the PDF worker output, enforced while the body is parsed.
        text, page_count = extract_docx_text(content, max_chars=max_chars * 8 + 65536)
        if page_count > max_pages:  # the same page budget as a PDF
            raise CvPdfTooManyPages(max_pages)
        return CvText(_clean_text(text, max_chars), page_count)
    if pdf_runner is not None:
        pages = pdf_runner(content, max_pages=max_pages, max_chars=max_chars, timeout_seconds=timeout)
        return CvText(_clean_text("\n".join(pages), max_chars), len(pages))

    payload = _run_pdf_worker(content, max_pages=max_pages, max_chars=max_chars, timeout_seconds=timeout)
    pages = _validate_pages(payload)
    metadata = payload.get("metadata")
    page_count = _json_int(metadata.get("page_count")) if isinstance(metadata, dict) else None
    return CvText(_clean_text("\n".join(pages), max_chars), page_count if page_count is not None else len(pages))


def extract_cv_text(
    path: Path,
    *,
    max_pages: int = MAX_CV_PAGES,
    max_chars: int = MAX_CV_CHARS,
    timeout: float = PDF_TIMEOUT_SECONDS,
) -> str:
    """Return bounded plain text from a CV file (PDF, ``.docx``, ``.txt`` or ``.md``)."""

    return extract_cv(path, max_pages=max_pages, max_chars=max_chars, timeout=timeout).text

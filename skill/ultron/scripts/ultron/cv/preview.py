"""Publish local PDF and PNG previews from source or generated documents."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ultron.cv import docx_pages, docx_reader, extract, layout_profile
from ultron.infra import fs, home, pdf_raster
from ultron.infra.file_lock import open_file_lock


def _preview_stem(source: Path) -> str:
    stem = re.sub(r"[^a-zA-Z0-9._-]", "-", source.stem)[:80].strip(".-")
    if re.match(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", stem, re.IGNORECASE):
        stem = "_" + stem
    return stem or "document"


def validate_preview_options(dpi: int, max_pages: int = extract.MAX_CV_PAGES) -> None:
    """Validate interactive preview controls before making any document changes."""
    if not 72 <= dpi <= 200:
        raise ValueError("Preview DPI must be between 72 and 200.")
    if not 1 <= max_pages <= extract.MAX_CV_PAGES:
        raise ValueError(f"Preview max-pages must be between 1 and {extract.MAX_CV_PAGES}.")


def _publish_pdf(source: Path, target: Path, pdf: bytes) -> None:
    if fs.is_link(target) or (target.exists() and not target.is_file()):
        raise ValueError("Preview PDF target is not a regular file.")
    if target.resolve() == source.resolve():
        return
    marker = target.with_name(f".{target.name}.ultron-preview")
    if fs.is_link(marker) or (marker.exists() and not marker.is_file()):
        raise ValueError("Preview PDF ownership marker is not a regular file.")
    if target.exists():
        if not marker.is_file():
            raise ValueError("Preview would overwrite an existing PDF.")
        with marker.open("rb") as stream:
            recorded = stream.read(65)
        with target.open("rb") as stream:
            original = stream.read(pdf_raster.MAX_PDF_BYTES + 1)
        if len(original) > pdf_raster.MAX_PDF_BYTES or hashlib.sha256(original).hexdigest().encode("ascii") != recorded:
            raise ValueError("Preview would overwrite an existing PDF.")
        fs.atomic_write_bytes(target, pdf)
    elif not fs.atomic_create_bytes(target, pdf):
        raise ValueError("Preview would overwrite an existing PDF.")
    fs.atomic_write_text(marker, hashlib.sha256(pdf).hexdigest())


def write_reference_preview(
    source: Path, pdf: bytes | None, *, pages: int, out_dir: Path | None = None,
    category: str = "cv", dpi: int = 144, max_pages: int = extract.MAX_CV_PAGES, warning: str = "",
    rendered_pages_verified: bool = False,
) -> dict[str, Any]:
    """Persist the existing PDF and its local PNGs without modifying the source."""
    payload: dict[str, Any] = {
        "pdf_path": None, "images": [], "rasterizer": None, "dpi": dpi,
        "warnings": [warning] if warning else [],
    }
    if pdf is None:
        payload["warnings"].append("Install LibreOffice or Microsoft Word to render a reference PDF.")
        return payload
    stem = _preview_stem(source)
    try:
        folder = pdf_raster.prepare_output_directory(out_dir or home.ultron_home() / category / "previews" / stem)
        target = folder / (stem + ".pdf")
        lock_path = folder / ".ultron-preview.lock"
        if fs.is_link(lock_path) or (lock_path.exists() and not lock_path.is_file()):
            raise ValueError("Preview lock must be a regular file.")
        with closing(open_file_lock(lock_path, exclusive=True)):
            _publish_pdf(source, target, pdf)
            payload["pdf_path"] = str(target)
            # A fallback count can underestimate the actual number of rendered pages.
            raster_limit = min(max_pages, pages) if rendered_pages_verified else max_pages
            raster = pdf_raster.render_pdf_to_pngs(pdf, folder, dpi=dpi, max_pages=raster_limit)
    except ValueError as exc:
        payload["warnings"].append(str(exc))
        return payload
    except (OSError, sqlite3.Error):
        payload["warnings"].append("Choose a writable preview directory without symbolic links.")
        return payload
    payload.update(images=[str(path) for path in raster.images], rasterizer=raster.rasterizer)
    if raster.warning:
        payload["warnings"].append(raster.warning)
    if raster.images and len(raster.images) < pages:
        payload["warnings"].append(f"Preview contains {len(raster.images)} of {pages} pages.")
    return payload


def preview_cv(
    source: Path, *, out_dir: Path | None = None, dpi: int = 144, max_pages: int = extract.MAX_CV_PAGES,
) -> dict[str, Any]:
    """Inspect a DOCX or PDF CV and produce visual previews when tools are available."""
    validate_preview_options(dpi, max_pages)
    kind = source.suffix.lower().lstrip(".")
    if kind not in {"pdf", "docx"}:
        raise extract.CvExtractionError("CV preview supports DOCX and PDF.", code="unsupported_type")
    content = layout_profile.read_layout_source(source)
    warning = ""
    converter = None
    if kind == "docx":
        report = docx_reader.inspect_docx(content)
        count = docx_pages.measure_docx_pages(content, report=report, keep_pdf=True)
        pages, page_source, converter = count.pages, count.source, count.converter
        pdf, warning = count.pdf_bytes, count.warning
        metadata = asdict(report)
    else:
        _, metadata = extract.subprocess_pdf_inspector(content)
        pages = int(metadata.get("page_count", 0))
        if pages < 1:
            raise extract.CvExtractionError("The PDF has no pages.")
        page_source, pdf = "pdf_original", content
    result = write_reference_preview(
        source, pdf, pages=pages, out_dir=out_dir, dpi=dpi, max_pages=max_pages, warning=warning,
        rendered_pages_verified=page_source in {"pdf_render", "pdf_original"},
    )
    return {
        "source_file": str(source.resolve()), "file_type": kind, "pages": pages,
        "page_count_source": page_source, "page_count_converter": converter,
        "detected_layout": layout_profile.extract_layout_profile(content, file_type=kind, format_meta=metadata),
        **result,
    }

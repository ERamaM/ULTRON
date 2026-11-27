"""Render a bounded local PDF preview with optional native tools."""

from __future__ import annotations

import importlib.util
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ultron.infra.fs import is_link

MAX_PDF_BYTES = 25_000_000
_MAX_PIXELS = 2600
_MAX_PNG_BYTES = 24_000_000
_MANIFEST = ".ultron-preview-pages"
_PAGE_NAME = re.compile(r"page-([0-9]{1,5})\.png")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_END = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
_TOOLS = {"pdftoppm": ("pdftoppm",), "mutool": ("mutool",), "gs": ("gs", "gswin64c", "gswin32c")}

_GS_GEOMETRY_CHECK = """
import math, sys
from itertools import islice
from pypdf import PdfReader

dpi, pages, max_pixels = map(int, sys.argv[1:])
with open("input.pdf", "rb") as source:
    reader = PdfReader(source)
    for page in islice(reader.pages, pages):
        scale = float(page.user_unit) * dpi / 72
        for size in (page.mediabox.width, page.mediabox.height, page.cropbox.width, page.cropbox.height):
            pixels = float(size) * scale
            if not math.isfinite(pixels) or not 0 < pixels <= max_pixels:
                sys.exit(1)
"""


_PDFIUM = r"""
import struct, sys, zlib
from contextlib import closing
from pathlib import Path
import pypdfium2 as pdfium

def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

def save(page, index, dpi, limit):
    scale = min(dpi / 72, (limit - 1) / max(page.get_size()))
    with closing(page.render(scale=scale, force_bitmap_format=pdfium.raw.FPDFBitmap_BGR, rev_byteorder=True)) as bitmap:
        view = memoryview(bitmap.buffer).cast("B")
        w, h = bitmap.width, bitmap.height
        raw = b"".join(b"\0" + view[y * bitmap.stride:y * bitmap.stride + w * 3].tobytes() for y in range(h))
        header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
        Path(f"page-{index + 1}.png").write_bytes(png)

with pdfium.PdfDocument("input.pdf") as pdf:
    for index in range(min(len(pdf), int(sys.argv[2]))):
        with closing(pdf[index]) as page:
            save(page, index, int(sys.argv[1]), int(sys.argv[3]))
"""


@dataclass(frozen=True, slots=True)
class PdfRasterResult:
    """Rendered page paths and any warning about rasterizer availability or failure."""

    images: list[Path]
    rasterizer: str | None
    dpi: int
    warning: str = ""


def _find_binary(names: tuple[str, ...], env: Mapping[str, str]) -> str | None:
    cwd = Path.cwd().resolve()
    suffixes = (".exe", ".com") if os.name == "nt" else ("",)
    for entry in env.get("PATH", os.defpath).split(os.pathsep):
        directory = Path(entry.strip('"'))
        if not directory.is_absolute() or directory.resolve() == cwd:
            continue
        for name in names:
            for suffix in suffixes:
                path = (directory / (name + suffix)).resolve()
                if path.parent != cwd and path.is_file() and os.access(path, os.X_OK):
                    return str(path)
    return None


def detect_pdf_rasterizer(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """Return the preferred executable or optional PDFium interpreter."""
    settings = os.environ if env is None else env
    choice = settings.get("ULTRON_PDF_RASTERIZER", "auto").strip().lower()
    for backend, names in _TOOLS.items():
        if choice in {"auto", backend} and (path := _find_binary(names, settings)):
            return backend, path
    if choice in {"auto", "pypdfium2"} and importlib.util.find_spec("pypdfium2") is not None:
        return "pypdfium2", sys.executable
    return None


def _command(backend: str, executable: str, dpi: int, pages: int) -> list[str]:
    pixels = str(_MAX_PIXELS)
    if backend == "pdftoppm":
        return [executable, "-png", "-r", str(dpi), "-W", str(_MAX_PIXELS + 1), "-H", str(_MAX_PIXELS + 1),
                "-f", "1", "-l", str(pages), "input.pdf", "page"]
    if backend == "mutool":
        return [executable, "draw", "-F", "png", "-r", str(dpi), "-w", pixels, "-h", pixels,
                "-o", "page-%d.png", "input.pdf", f"1-{pages}"]
    if backend == "gs":
        return [executable, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=png16m",
                f"-r{dpi}", "-dFirstPage=1", f"-dLastPage={pages}", "-sOutputFile=page-%d.png", "input.pdf"]
    return [executable, "-E", "-s", "-P", "-c", _PDFIUM, str(dpi), str(pages), pixels]


def prepare_output_directory(out_dir: Path) -> Path:
    """Create a preview directory after rejecting symlink and junction components."""
    target = Path(os.path.abspath(out_dir))
    for part in (*reversed(target.parents), target):
        if is_link(part) or (part.exists() and not part.is_dir()):
            raise ValueError("Choose a preview directory without symlinks, junctions or file components.")
        part.mkdir(mode=0o700, exist_ok=True)
    return target


def _png_data(path: Path) -> bytes:
    if is_link(path) or not path.is_file():
        raise ValueError("Rasterizer did not produce regular PNG files.")
    with path.open("rb") as stream:
        data = stream.read(_MAX_PNG_BYTES + 1)
    if len(data) > _MAX_PNG_BYTES or not data.startswith(_PNG_SIGNATURE + b"\0\0\0\rIHDR") or not data.endswith(_PNG_END):
        raise ValueError("Rasterizer produced an invalid or oversized PNG.")
    width, height = struct.unpack(">II", data[16:24])
    if not (1 <= width <= _MAX_PIXELS and 1 <= height <= _MAX_PIXELS):
        raise ValueError("Rasterizer produced an oversized page image.")
    return data


def _validated_pages(scratch: Path, max_pages: int) -> list[Path]:
    indexed = sorted(
        (int(match[1]), path)
        for path in scratch.iterdir()
        if (match := _PAGE_NAME.fullmatch(path.name))
    )
    if not indexed or [index for index, _ in indexed] != list(range(1, len(indexed) + 1)) or len(indexed) > max_pages:
        raise ValueError("Rasterizer did not produce a contiguous page sequence.")
    paths = []
    for index, path in indexed:
        verified = scratch / f"verified-{index}.png"
        verified.write_bytes(_png_data(path))
        paths.append(verified)
    return paths


def _owned_page_count(manifest: Path) -> int:
    if is_link(manifest):
        raise ValueError("Preview ownership marker cannot be a link.")
    if not manifest.exists():
        return 0
    if not manifest.is_file():
        raise ValueError("Preview ownership marker must be a regular file.")
    with manifest.open("rb") as stream:
        raw = stream.read(3)
    if not re.fullmatch(rb"(?:[1-9]|1[0-9]|20)", raw):
        raise ValueError("Preview ownership marker is invalid.")
    return int(raw)


def _publish_pages(scratch: Path, out: Path, max_pages: int) -> list[Path]:
    pages = _validated_pages(scratch, max_pages)
    out = prepare_output_directory(out)
    manifest = out / _MANIFEST
    previous = _owned_page_count(manifest)
    images = [out / f"page-{index}.png" for index in range(1, len(pages) + 1)]
    for index in range(1, max(previous, len(pages)) + 1):
        target = out / f"page-{index}.png"
        if is_link(target) or (target.exists() and (index > previous or not target.is_file())):
            raise ValueError("Preview cannot replace an unrelated file or link.")
    with tempfile.TemporaryDirectory(prefix=".ultron-pages-", dir=out) as staging:
        pending = Path(staging)
        for page, image in zip(pages, images, strict=True):
            staged_image = pending / image.name
            staged_image.write_bytes(page.read_bytes())
            staged_image.chmod(0o600)
        (pending / _MANIFEST).write_text(str(len(images)), encoding="ascii")
        for image in images:
            (pending / image.name).replace(image)
        for index in range(len(images) + 1, previous + 1):
            (out / f"page-{index}.png").unlink(missing_ok=True)
        (pending / _MANIFEST).replace(manifest)
    return images


def _run_rasterizer(
    backend: str, executable: str, dpi: int, pages: int, scratch: Path, env: dict[str, str], timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    options = {
        "cwd": scratch, "env": env,
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
    }
    if backend == "gs":
        # Ghostscript otherwise allocates its raster using the supplied page geometry.
        check = [sys.executable, "-E", "-s", "-P", "-c", _GS_GEOMETRY_CHECK,
                 str(dpi), str(pages), str(_MAX_PIXELS)]
        subprocess.run(check, timeout=min(timeout, 5.0), check=True, shell=False, **options)
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise ValueError("Rasterization exhausted its time budget.")
    subprocess.run(_command(backend, executable, dpi, pages), timeout=timeout, check=True, shell=False, **options)


def render_pdf_to_pngs(
    pdf_bytes: bytes, out_dir: Path, *, dpi: int = 144, max_pages: int = 20,
    timeout: float = 30.0, env: Mapping[str, str] | None = None,
) -> PdfRasterResult:
    """Rasterize pages to a safe local directory and report recoverable failures."""
    dpi = min(200, max(72, dpi))
    max_pages = min(20, max(1, max_pages))
    try:
        detected = detect_pdf_rasterizer(env)
        if detected is None:
            return PdfRasterResult([], None, dpi, "Install Poppler or pypdfium2 to enable visual previews.")
        if len(pdf_bytes) > MAX_PDF_BYTES or not pdf_bytes.startswith(b"%PDF-"):
            raise ValueError("The PDF is invalid or exceeds the preview size limit.")
        out_dir = prepare_output_directory(out_dir)
        backend, executable = detected
        with tempfile.TemporaryDirectory(prefix="ultron-raster-") as folder:
            scratch = Path(folder)
            (scratch / "input.pdf").write_bytes(pdf_bytes)
            settings = dict(os.environ if env is None else env)
            for key in tuple(settings):
                if key.upper() in {"GS_OPTIONS", "GS_LIB", "GS_DEVICE"}:
                    settings.pop(key)
            _run_rasterizer(backend, executable, dpi, max_pages, scratch, settings, min(timeout, 30.0))
            return PdfRasterResult(_publish_pages(scratch, out_dir, max_pages), backend, dpi)
    except (OSError, ValueError, struct.error, subprocess.SubprocessError, ImportError):
        return PdfRasterResult([], None, dpi, "PDF pages could not be rendered safely. Check the preview directory and local rasterizer.")

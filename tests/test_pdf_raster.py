"""Test isolated local rasterization and bounded preview publication."""

import base64
import os
import stat
import subprocess
from types import SimpleNamespace

import pytest

from ultron.infra import pdf_raster

_PDF = b"%PDF-synthetic"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7XkAAAAASUVORK5CYII="
)


@pytest.mark.parametrize(("backend", "required"), [
    ("pdftoppm", ["-png", "-l"]), ("mutool", ["draw", "-F"]),
    ("gs", ["-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=png16m"]), ("pypdfium2", ["-E", "-s", "-P"]),
])
def test_cli_isolation_and_normalized_page_names(monkeypatch, tmp_path, backend, required):
    scratch = []
    # A fixed reading exercises round-off when subtracting from the computed deadline.
    monkeypatch.setattr(pdf_raster, "time", SimpleNamespace(monotonic=lambda: 2.02))
    out = tmp_path / "pages"
    out.mkdir()
    (out / "page-3.png").write_bytes(_PNG)
    (out / ".ultron-preview-pages").write_text("3")
    (out / "page-4.png").write_bytes(b"unrelated")
    (out / "keep.txt").touch()

    def convert(args, **kwargs):
        folder = kwargs["cwd"]
        if backend == "gs" and "-c" in args:
            assert {"-E", "-s", "-P"} <= set(args)
            assert kwargs["timeout"] == 5.0
            return subprocess.CompletedProcess(args, 0)
        scratch.append(folder)
        assert (folder / "input.pdf").read_bytes() == _PDF
        assert 0 < kwargs["timeout"] <= 30.0 and not kwargs["shell"]
        assert set(required) <= set(args)
        assert all(key not in kwargs["env"] for key in ("GS_OPTIONS", "GS_LIB"))
        (folder / "page-02.png").write_bytes(_PNG)
        (folder / "page-01.png").write_bytes(_PNG)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(pdf_raster, "detect_pdf_rasterizer", lambda _: (backend, "rasterizer"))
    monkeypatch.setattr(pdf_raster.subprocess, "run", convert)
    result = pdf_raster.render_pdf_to_pngs(
        _PDF, out, dpi=500, max_pages=99, timeout=50, env={"GS_OPTIONS": "bad", "GS_LIB": "bad"},
    )
    assert result.dpi == 200 and result.rasterizer == backend and not result.warning
    assert result.images == [out / "page-1.png", out / "page-2.png"]
    assert all(image.read_bytes() == _PNG for image in result.images)
    if os.name != "nt":
        assert all(stat.S_IMODE(image.stat().st_mode) == 0o600 for image in result.images)
    assert not (out / "page-3.png").exists() and (out / "keep.txt").exists()
    assert (out / "page-4.png").read_bytes() == b"unrelated"
    assert (out / ".ultron-preview-pages").read_text() == "2"
    assert scratch and not scratch[0].exists()


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "invalid", "gap", "huge"])
def test_raster_failure_is_nonblocking(monkeypatch, tmp_path, failure):
    scratch = []

    def convert(args, **kwargs):
        folder = kwargs["cwd"]
        scratch.append(folder)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 30)
        if failure == "nonzero":
            raise subprocess.CalledProcessError(1, args)
        data = b"invalid" if failure == "invalid" else _PNG
        if failure == "huge":
            data = data[:16] + b"\xff" * 4 + data[20:]
        (folder / ("page-2.png" if failure == "gap" else "page-1.png")).write_bytes(data)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(pdf_raster, "detect_pdf_rasterizer", lambda _: ("mutool", "mutool"))
    monkeypatch.setattr(pdf_raster.subprocess, "run", convert)
    result = pdf_raster.render_pdf_to_pngs(_PDF, tmp_path, dpi=20)
    assert not result.images and not result.rasterizer and result.warning and result.dpi == 72
    assert scratch and not scratch[0].exists()


def test_rejects_symlink_ancestor_and_output_file(monkeypatch, tmp_path):
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    link = tmp_path / "symlink"
    try:
        link.symlink_to(redirected, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symbolic links requires OS permissions")
    monkeypatch.setattr(pdf_raster, "detect_pdf_rasterizer", lambda _: ("gs", "gs"))
    monkeypatch.setattr(pdf_raster.subprocess, "run", lambda *_a, **_kw: pytest.fail("launched"))
    assert pdf_raster.render_pdf_to_pngs(_PDF, link / "child").warning
    assert not list(redirected.iterdir())


def test_discovery_precedence_disable_and_pdfium(monkeypatch, tmp_path):
    backend = tmp_path / "binary"
    monkeypatch.setattr(pdf_raster, "_find_binary", lambda names, _: str(backend) if names[0] != "mutool" else None)
    monkeypatch.setattr(pdf_raster.importlib.util, "find_spec", lambda _: object())
    assert pdf_raster.detect_pdf_rasterizer({}) == ("pdftoppm", str(backend))
    assert pdf_raster.detect_pdf_rasterizer({"ULTRON_PDF_RASTERIZER": "gs"}) == ("gs", str(backend))
    assert pdf_raster.detect_pdf_rasterizer({"ULTRON_PDF_RASTERIZER": "mutool"}) is None
    assert pdf_raster.detect_pdf_rasterizer({"ULTRON_PDF_RASTERIZER": "pypdfium2"})[0] == "pypdfium2"
    assert pdf_raster.detect_pdf_rasterizer({"ULTRON_PDF_RASTERIZER": "invalid"}) is None
    assert pdf_raster.detect_pdf_rasterizer() is None



def test_existing_unowned_page_is_preserved(monkeypatch, tmp_path):
    existing = tmp_path / "page-1.png"
    existing.write_bytes(b"unrelated")

    def convert(args, *, cwd, **kwargs):
        (cwd / "page-1.png").write_bytes(_PNG)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(pdf_raster, "detect_pdf_rasterizer", lambda _: ("pdftoppm", "unused"))
    monkeypatch.setattr(pdf_raster.subprocess, "run", convert)
    result = pdf_raster.render_pdf_to_pngs(_PDF, tmp_path)
    assert not result.images and result.warning
    assert existing.read_bytes() == b"unrelated"


def test_binary_search_ignores_working_and_relative_paths(monkeypatch, tmp_path):
    working = tmp_path / "working"
    installed = tmp_path / "installed"
    working.mkdir()
    installed.mkdir()
    monkeypatch.chdir(working)

    name = "pdftoppm.exe" if os.name == "nt" else "pdftoppm"
    for folder in (working, installed):
        candidate = folder / name
        candidate.touch()
        candidate.chmod(0o755)
    env = {"PATH": os.pathsep.join((".", "bin", str(working), str(installed)))}
    assert pdf_raster.detect_pdf_rasterizer(env) == ("pdftoppm", str(installed / name))



def test_mutool_accepts_png_at_configured_size_limit(monkeypatch, tmp_path):
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    width = pdf_raster._MAX_PIXELS
    header = struct.pack(">IIBBBBB", width, 1, 8, 0, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(width + 1)))
    png += chunk(b"IEND", b"")

    def convert(args, *, cwd, **kwargs):
        assert args[args.index("-w") + 1] == str(width)
        (cwd / "page-1.png").write_bytes(png)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(pdf_raster, "detect_pdf_rasterizer", lambda _: ("mutool", "unused"))
    monkeypatch.setattr(pdf_raster.subprocess, "run", convert)
    result = pdf_raster.render_pdf_to_pngs(_PDF, tmp_path)
    assert result.images[0].read_bytes() == png



@pytest.mark.parametrize(("width", "blocked"), [(595, False), (10_000, True)])
def test_ghostscript_geometry_is_checked_before_rendering(tmp_path, monkeypatch, width, blocked):
    import io
    from pathlib import Path

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=width, height=842)
    pdf = io.BytesIO()
    writer.write(pdf)
    real_run = subprocess.run
    launched = []

    def convert(args, **kwargs):
        if "-sDEVICE=png16m" in args:
            launched.append(True)
            Path(kwargs["cwd"], "page-1.png").write_bytes(_PNG)
            return subprocess.CompletedProcess(args, 0)
        return real_run(args, **kwargs)

    monkeypatch.setattr(pdf_raster, "detect_pdf_rasterizer", lambda _: ("gs", "gs"))
    monkeypatch.setattr(pdf_raster.subprocess, "run", convert)
    result = pdf_raster.render_pdf_to_pngs(pdf.getvalue(), tmp_path)
    assert bool(launched) is not blocked
    assert bool(result.images) is not blocked

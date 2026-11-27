"""Exercise visual preview and layout controls through normal CLI entry points."""

import io
import json
import zipfile
from pathlib import Path

import pytest

from ultron.cli import main
from ultron.cv import ats, docx_writer
from ultron.cv.layout_profile import CvLayoutStyle
from ultron.infra import docx_pdf, pdf_raster

_DRAFT = """# Jane Example
Consultant
jane@example.invalid

## Experience
### Analyst at Example
#### 2020 - 2024
- Built a reporting pipeline.
"""


def _pdf(pages=2):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def mock_render(monkeypatch):
    reference = _pdf()
    calls = []

    def convert(docx):
        calls.append(docx)
        return reference, "libreoffice"

    def raster(data, folder, *, dpi, max_pages):
        assert data == reference and max_pages >= 1
        images = []
        for page in range(1, min(2, max_pages) + 1):
            path = folder / f"page-{page}.png"
            path.touch()
            images.append(path)
        return pdf_raster.PdfRasterResult(images, "pdftoppm", dpi)

    monkeypatch.setattr(docx_pdf, "render_docx_to_pdf_bytes", convert)
    monkeypatch.setattr(pdf_raster, "render_pdf_to_pngs", raster)
    return reference, calls


def test_source_preview_docx_and_pdf_detect_layout(tmp_path, capsys, mock_render):
    reference, calls = mock_render
    docx = tmp_path / "Jane Example.docx"
    docx.write_bytes(docx_writer.render_docx_bytes(
        docx_writer.parse_markdown_cv(_DRAFT),
        layout=CvLayoutStyle(page_size="a4", header_align="center", font="Arial", accent_color="2B4C7E"),
    ))
    assert main(["cv", "preview", str(docx)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pages"] == 2 and result["page_count_source"] == "pdf_render" and len(calls) == 1
    assert [Path(path).name for path in result["images"]] == ["page-1.png", "page-2.png"]
    assert Path(result["pdf_path"]).read_bytes() == reference
    for key, expected in {"page_size": "a4", "header_align": "center", "font": "Arial", "accent_color": "2B4C7E"}.items():
        assert result["detected_layout"][key] == expected

    original = tmp_path / "cv.pdf"
    original.write_bytes(reference)
    assert main(["cv", "preview", str(original), "--out-dir", str(tmp_path), "--max-pages", "1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["page_count_source"] == "pdf_original" and result["pages"] == 2
    assert result["detected_layout"]["page_size"] == "a4" and len(result["images"]) == 1
    assert Path(result["pdf_path"]) == original and original.read_bytes() == reference
    assert any("1 of 2" in warning for warning in result["warnings"])


def test_source_matching_build_applies_json_then_cli_and_reuses_pdf(tmp_path, capsys, mock_render):
    reference, calls = mock_render
    draft = tmp_path / "draft.md"
    draft.write_text(_DRAFT, encoding="utf-8")
    source = tmp_path / "source.docx"
    source.write_bytes(docx_writer.render_docx_bytes(
        docx_writer.parse_markdown_cv(_DRAFT),
        layout=CvLayoutStyle(page_size="a4", font="Arial", header_align="center", section_style="clean"),
    ))
    layout = tmp_path / "overrides.json"
    layout.write_text('{"density":"compact","header_align":"left","section_style":"caps","date_layout":"right_tab"}')
    output = tmp_path / "custom.docx"
    preview_dir = tmp_path / "previews"
    assert main([
        "cv", "build-docx", "--from", str(draft), "--out", str(output), "--source-cv", str(source),
        "--match-source-layout", "--layout", str(layout), "--header-align", "center", "--preview",
        "--preview-dir", str(preview_dir), "--dpi", "180",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 1 and result["preview"]["dpi"] == 180
    assert Path(result["preview"]["pdf_path"]).read_bytes() == reference
    assert len(result["preview"]["images"]) == 2 and result["ats_audit"]["format_tier"] == "preferred"
    assert result["layout_used"]["font"] == "Arial" and result["layout_used"]["page_size"] == "a4"
    assert result["layout_used"]["header_align"] == "center" and result["layout_used"]["section_style"] == "caps"
    with zipfile.ZipFile(output) as archive:
        document = archive.read("word/document.xml")
        styles = archive.read("word/styles.xml")
    assert b'w:w="11906" w:h="16838"' in document and b'w:jc w:val="center"' in document
    assert b'w:tab w:val="right" w:pos="9746"' in document and b'<w:caps/>' in styles
    audit = ats.audit_generated_docx(output.read_bytes())
    fmt = audit["format_metadata"]
    assert (fmt["table_count"], fmt["textbox_count"], fmt["image_count"], fmt["header_footer_has_contact"]) == (0, 0, 0, False)


@pytest.mark.parametrize("template", ["classic", "modern", "executive"])
def test_letter_a4_preview_uses_single_conversion(tmp_path, capsys, mock_render, template):
    _, calls = mock_render
    draft = tmp_path / "letter.md"
    draft.write_text("Dear team,\n\nI am applying for the advertised role.\n\nBest regards,\nJane Example")
    output = tmp_path / "letter.docx"
    assert main([
        "cover-letter", "build-docx", "--from", str(draft), "--out", str(output), "--template", template,
        "--page-size", "a4", "--preview",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 1 and result["layout_used"]["page_size"] == "a4"
    assert len(result["preview"]["images"]) == 2
    with zipfile.ZipFile(output) as archive:
        assert b'w:w="11906" w:h="16838"' in archive.read("word/document.xml")


def test_preview_degrades_with_and_without_rendered_pdf(tmp_path, capsys):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(_pdf(1))
    assert main(["cv", "preview", str(pdf)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pdf_path"] and payload["warnings"] and not payload["images"] and payload["rasterizer"] is None
    docx = tmp_path / "cv.docx"
    docx.write_bytes(docx_writer.render_docx_bytes(docx_writer.parse_markdown_cv(_DRAFT)))
    assert main(["cv", "preview", str(docx)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["page_count_source"] == "estimated" and not payload["pdf_path"] and payload["warnings"]


@pytest.mark.parametrize("flags", [
    ("--page-size", "legal"), ("--section-style", "invalid"), ("--accent-color", "FFFFFF"),
    ("--dpi", "201", "--preview"), ("--match-source-layout",),
])
def test_invalid_build_layout_fails_cleanly(tmp_path, capsys, flags):
    draft = tmp_path / "draft.md"
    draft.write_text(_DRAFT)
    output = tmp_path / "output.docx"
    assert main(["cv", "build-docx", "--from", str(draft), "--out", str(output), *flags]) != 0
    assert json.loads(capsys.readouterr().out)["error"] and not output.exists()



def test_preview_directory_failure_keeps_document_result(tmp_path, capsys, mock_render):
    reference, _ = mock_render
    source = tmp_path / "source.pdf"
    source.write_bytes(reference)
    folder = tmp_path / "existing-file"
    folder.write_text("keep")
    assert main(["cv", "preview", str(source), "--out-dir", str(folder)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pdf_path"] is None and result["warnings"] and not result["images"]
    assert folder.read_text() == "keep" and source.read_bytes() == reference


def test_source_preview_rejects_unsupported_and_oversized_input(tmp_path, capsys, monkeypatch):
    from ultron.cv import extract

    text = tmp_path / "unsupported.txt"
    text.write_bytes(b"example")
    assert main(["cv", "preview", str(text)]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "unsupported_type"
    monkeypatch.setattr(extract, "MAX_CV_BYTES", 2)
    original = tmp_path / "oversized.pdf"
    original.write_bytes(b"%PDF-synthetic")
    assert main(["cv", "preview", str(original)]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "too_long"



def test_preview_preserves_unrelated_or_edited_pdf_and_refreshes_its_own(tmp_path, capsys, mock_render):
    reference, _ = mock_render
    source = tmp_path / "cv.docx"
    source.write_bytes(docx_writer.render_docx_bytes(docx_writer.parse_markdown_cv(_DRAFT)))
    unrelated = source.with_suffix(".pdf")
    unrelated.write_bytes(b"existing user PDF")
    argv = ["cv", "preview", str(source), "--out-dir", str(tmp_path)]

    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pdf_path"] is None and "existing PDF" in payload["warnings"][0]
    assert unrelated.read_bytes() == b"existing user PDF"

    unrelated.unlink()
    for _ in range(2):
        assert main(argv) == 0
        payload = json.loads(capsys.readouterr().out)
        assert Path(payload["pdf_path"]) == unrelated and unrelated.read_bytes() == reference
    unrelated.write_bytes(b"edited preview PDF")
    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pdf_path"] is None and "existing PDF" in payload["warnings"][0]
    assert unrelated.read_bytes() == b"edited preview PDF"

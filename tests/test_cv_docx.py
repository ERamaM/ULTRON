"""Word (.docx) CV extraction with the standard library, and the non-colliding output name."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from ultron import cli, cv, cv_docx

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _document(body: str, *, prolog: str = "") -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>{prolog}'
        f'<w:document xmlns:w="{W}" xmlns:mc="{MC}"><w:body>{body}</w:body></w:document>'
    )


def _paragraph(*runs: str) -> str:
    return "<w:p>" + "".join(f"<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r>" for text in runs) + "</w:p>"


def build_docx(document_xml: str, *, extra: dict[str, str | bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document_xml)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _write(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_paragraphs_runs_tabs_breaks_and_tables_become_text(tmp_path):
    body = (
        _paragraph("Jane Example", " - Senior Data Analyst")
        + '<w:p><w:r><w:t>SQL</w:t><w:tab/><w:t>Looker</w:t><w:br/><w:t>Python</w:t></w:r></w:p>'
        + "<w:tbl><w:tr><w:tc>" + _paragraph("2021 - 2024") + "</w:tc><w:tc>"
        + _paragraph("Analyst at Example Co") + "</w:tc></w:tr></w:tbl>"
        + _paragraph("Análisis de datos")
    )
    result = cv.extract_cv(_write(tmp_path, "cv.docx", build_docx(_document(body))))
    assert result.text.splitlines() == [
        "Jane Example - Senior Data Analyst", "SQL Looker", "Python", "2021 - 2024", "Analyst at Example Co",
        "Análisis de datos",
    ]
    assert result.pages == 1


def test_a_text_box_is_read_once_and_deleted_text_and_field_codes_are_skipped(tmp_path):
    textbox = (
        "<w:p><w:r><mc:AlternateContent><mc:Choice Requires=\"wps\"><w:drawing><w:txbxContent>"
        + _paragraph("Skills: SQL")
        + "</w:txbxContent></w:drawing></mc:Choice><mc:Fallback><w:pict><w:txbxContent>"
        + _paragraph("Skills: SQL")
        + "</w:txbxContent></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>"
    )
    hidden = "<w:p><w:del><w:r><w:delText>Old title</w:delText></w:r></w:del>" \
             "<w:r><w:instrText>HYPERLINK x</w:instrText></w:r><w:r><w:t>Profile</w:t></w:r></w:p>"
    page_break = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>' + _paragraph("Page two")
    result = cv.extract_cv(_write(tmp_path, "cv.docx", build_docx(_document(textbox + hidden + page_break))))
    assert [line for line in result.text.splitlines() if line] == ["Skills: SQL", "Profile", "Page two"]
    assert result.pages == 2


def test_headers_and_footers_are_left_out(tmp_path):
    header = f'<w:hdr xmlns:w="{W}">' + _paragraph("jane@example.com") + "</w:hdr>"
    content = build_docx(_document(_paragraph("Experience")), extra={"word/header1.xml": header})
    assert cv.extract_cv_text(_write(tmp_path, "cv.docx", content)) == "Experience"


@pytest.mark.parametrize(
    "content",
    [
        build_docx(_document(_paragraph("&xxe;"), prolog='<!DOCTYPE d [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>')),
        build_docx(_document(_paragraph("&a;"), prolog='<!DOCTYPE d [<!ENTITY a "aaaaaaaaaa">]>')),
    ],
)
def test_a_document_type_declaration_is_rejected(tmp_path, content):
    with pytest.raises(cv.CvExtractionError) as raised:
        cv.extract_cv(_write(tmp_path, "cv.docx", content))
    assert raised.value.code == "no_extractable_text"


def test_not_a_zip_missing_body_and_empty_text_are_typed_errors(tmp_path):
    for content in (b"PK\x03\x04", b"plain text", build_docx(_document("")),
                    _zip_without_document()):
        with pytest.raises(cv.CvExtractionError) as raised:
            cv.extract_cv(_write(tmp_path, "cv.docx", content))
        assert raised.value.code == "no_extractable_text"


def _zip_without_document() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/other.xml", "<x/>")
    return buffer.getvalue()


def test_a_body_that_inflates_past_the_limit_is_rejected_without_reading_it_all(tmp_path, monkeypatch):
    monkeypatch.setattr(cv_docx, "MAX_DOCUMENT_XML_BYTES", 10_000)
    body = _paragraph("x" * 50_000)  # compresses to a few hundred bytes
    with pytest.raises(cv.CvExtractionError) as raised:
        cv.extract_cv(_write(tmp_path, "cv.docx", build_docx(_document(body))))
    assert raised.value.code == "too_long"


def test_too_many_archive_entries_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(cv_docx, "MAX_ARCHIVE_ENTRIES", 5)
    extra = {f"word/media/{index}.xml": "<x/>" for index in range(10)}
    with pytest.raises(cv.CvExtractionError) as raised:
        cv.extract_cv(_write(tmp_path, "cv.docx", build_docx(_document(_paragraph("Hi")), extra=extra)))
    assert raised.value.code == "too_long"


def test_text_over_the_character_budget_is_rejected(tmp_path):
    content = build_docx(_document(_paragraph("word " * 50)))
    with pytest.raises(cv.CvTextTooLong):
        cv.extract_cv(_write(tmp_path, "cv.docx", content), max_chars=100)


def test_cli_extracts_a_docx(tmp_path, capsys):
    source = _write(tmp_path, "cv.docx", build_docx(_document(_paragraph("Senior Data Analyst"))))
    assert cli.main(["cv", "extract", str(source)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["path"]).name == "cv.docx.txt"
    assert Path(payload["path"]).read_text(encoding="utf-8") == "Senior Data Analyst"


def test_a_pdf_and_a_text_cv_with_the_same_stem_keep_separate_outputs(tmp_path, capsys, monkeypatch):
    real = cv.extract_cv
    monkeypatch.setattr(cv, "extract_cv", lambda path: cv.CvText("From the PDF", 1) if path.suffix == ".pdf" else real(path))
    pdf = _write(tmp_path, "cv.pdf", b"%PDF-1.4")
    text = _write(tmp_path, "cv.txt", b"From the text file")
    outputs = []
    for source in (pdf, text):
        assert cli.main(["cv", "extract", str(source)]) == 0
        outputs.append(Path(json.loads(capsys.readouterr().out)["path"]))
    assert [path.name for path in outputs] == ["cv.pdf.txt", "cv.txt.txt"]
    assert [path.read_text(encoding="utf-8") for path in outputs] == ["From the PDF", "From the text file"]


def test_a_utf16_body_cannot_hide_a_document_type_declaration(tmp_path):
    xml = _document(_paragraph("&a;"), prolog='<!DOCTYPE d [<!ENTITY a "aaaaaaaaaa">]>').replace("UTF-8", "UTF-16")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml.encode("utf-16"))
    content = buffer.getvalue()
    with pytest.raises(cv.CvExtractionError) as raised:
        cv.extract_cv(_write(tmp_path, "cv.docx", content))
    assert raised.value.code == "no_extractable_text"

def test_breaks_inside_a_paragraph_separate_the_text_on_both_sides(tmp_path):
    body = (
        '<w:p><w:r><w:t>Experience</w:t><w:br w:type="page"/><w:t>Education</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Left</w:t><w:br w:type="column"/><w:t>Right</w:t><w:cr/><w:t>Next</w:t>'
        "<w:tab/><w:t>Tabbed</w:t></w:r></w:p>"
    )
    result = cv.extract_cv(_write(tmp_path, "cv.docx", build_docx(_document(body))))
    assert result.text.splitlines() == ["Experience", "Education", "Left", "Right", "Next Tabbed"]
    assert result.pages == 2

@pytest.mark.parametrize("max_pages, breaks", [(cv.MAX_CV_PAGES, cv.MAX_CV_PAGES), (2, 2)])
def test_a_docx_over_the_page_budget_is_rejected_like_a_pdf(tmp_path, max_pages, breaks):
    body = "".join(_paragraph(f"Page {index}") + '<w:p><w:r><w:br w:type="page"/></w:r></w:p>' for index in range(breaks))
    path = _write(tmp_path, "cv.docx", build_docx(_document(body + _paragraph("Last"))))
    with pytest.raises(cv.CvPdfTooManyPages) as raised:
        cv.extract_cv(path, max_pages=max_pages)
    assert raised.value.code == "too_many_pages"
    assert cv.extract_cv(path, max_pages=breaks + 1).pages == breaks + 1

def test_a_text_box_inside_a_paragraph_keeps_the_text_around_it_apart(tmp_path):
    body = (
        "<w:p><w:r><w:t>Experience</w:t></w:r><w:r><w:drawing><w:txbxContent>"
        + _paragraph("Skills")
        + "</w:txbxContent></w:drawing></w:r><w:r><w:t>Education</w:t></w:r></w:p>"
        + '<w:p><w:r><w:t>Non</w:t><w:noBreakHyphen/><w:t>stop</w:t></w:r></w:p>'
    )
    result = cv.extract_cv(_write(tmp_path, "cv.docx", build_docx(_document(body))))
    assert result.text.splitlines() == ["Experience", "Skills", "Education", "Non-stop"]


def test_many_sibling_elements_fail_fast_with_a_bounded_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cv_docx, "MAX_XML_ELEMENTS", 1_000)
    body = "<w:p>" + "<w:r/>" * 5_000 + "</w:p>"
    with pytest.raises(cv.CvExtractionError) as raised:
        cv.extract_cv(_write(tmp_path, "cv.docx", build_docx(_document(body))))
    assert raised.value.code == "too_long"


def test_deep_nesting_fails_fast_with_a_bounded_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cv_docx, "MAX_XML_DEPTH", 50)
    body = "<w:p>" + "<w:r>" * 200 + "</w:r>" * 200 + "</w:p>"
    with pytest.raises(cv.CvExtractionError) as raised:
        cv.extract_cv(_write(tmp_path, "cv.docx", build_docx(_document(body))))
    assert raised.value.code == "too_long"


def test_the_default_budgets_reject_millions_of_empty_siblings():
    body = _document("<w:p>" + "<w:r/>" * (cv_docx.MAX_XML_ELEMENTS + 10) + "</w:p>").encode()
    with pytest.raises(cv.CvExtractionError) as raised:
        cv_docx._text(body)
    assert raised.value.code == "too_long"
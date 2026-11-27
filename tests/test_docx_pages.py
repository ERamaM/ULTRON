"""Exercise rendered, cached, and estimated DOCX page counts."""

import io

import pytest

from ultron.cover_letter import builder
from ultron.cv import ats, docx_pages, docx_reader, docx_writer, extract
from ultron.infra import docx_pdf


@pytest.fixture
def pdf():
    from pypdf import PdfWriter

    def make(pages):
        buffer = io.BytesIO()
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=612, height=792)
        writer.write(buffer)
        return buffer.getvalue()

    return make


def test_worker_counts_blank_pages_and_enforces_cap(pdf):
    content = pdf(3)
    assert extract.count_pdf_pages(content, max_pages=3) == 3
    with pytest.raises(extract.CvPdfTooManyPages):
        extract.count_pdf_pages(content, max_pages=2)
    with pytest.raises(extract.CvExtractionError):
        extract.count_pdf_pages(b"%PDF-invalid")


def test_generated_and_existing_cv_use_pdf_and_score_length(monkeypatch, tmp_path, pdf):
    monkeypatch.setattr(docx_pdf, "render_docx_to_pdf_bytes", lambda _: (pdf(3), "libreoffice"))
    source = tmp_path / "draft.md"
    source.write_text("# Jane Example\n\n## Experience\n\n" + "A CV paragraph about example work.\n" * 120)
    output = tmp_path / "cv.docx"
    built = docx_writer.build_ats_docx(source, out_path=output)
    audit = ats.audit_cv(output)
    for payload in (built, audit, audit["format_metadata"]):
        assert (payload["pages"], payload["page_count_source"], payload["page_count_converter"]) == (
            3, "pdf_render", "libreoffice",
        )
    assert docx_reader.inspect_docx(output.read_bytes()).explicit_page_breaks == 0
    assert "page_length_excessive" in {finding["id"] for finding in audit["findings"]}
    assert "verified via pdf_render" in ats.render_audit_markdown(audit)
    assert extract.extract_cv(output).pages == 1

    report = docx_reader.DocxStructureReport("Skills", 8, app_pages=8)
    assert docx_pages.measure_docx_pages(b"", report=report).pages == 3
    with pytest.raises(extract.CvPdfTooManyPages):
        docx_pages.measure_docx_pages(b"", report=report, max_pages=2)


def test_cover_letter_warns_and_writes_output(monkeypatch, tmp_path, pdf):
    monkeypatch.setattr(docx_pdf, "render_docx_to_pdf_bytes", lambda _: (pdf(2), "word"))
    source = tmp_path / "draft.md"
    source.write_text("Dear team,\n\nExample application letter.\n\nBest regards,\nJane Example")
    output = tmp_path / "letter.docx"
    payload = builder.build_cover_letter_docx(source, out_path=output)
    assert payload["pages"] == payload["layout"]["pages"] == 2
    assert payload["layout"]["page_count_source"] == "pdf_render"
    assert payload["layout"]["page_count_converter"] == "word"
    assert "spans 2 pages" in payload["warning"] and output.is_file()


@pytest.mark.parametrize(("report", "pages", "source"), [
    (docx_reader.DocxStructureReport("Skills", 5, explicit_page_breaks=4, rendered_page_breaks=2, app_pages=9),
     5, "docx_rendered_breaks"),
    (docx_reader.DocxStructureReport("Skills", 2, app_pages=2), 2, "docx_app_props"),
    (docx_reader.DocxStructureReport("Skills", 4, app_pages=2, explicit_page_breaks=3), 4, "estimated"),
    (docx_reader.DocxStructureReport(("Experience " * 6 + "\n") * 120, 1,
                                    heading_paragraph_count=3, list_paragraph_count=10), 3, "estimated"),
])
def test_fallback_precedence_and_wrapping(report, pages, source):
    count = docx_pages.measure_docx_pages(b"", report=report)
    assert (count.pages, count.source, count.converter) == (pages, source, None)
    assert bool(count.warning) == (source == "estimated")
    with pytest.raises(extract.CvPdfTooManyPages):
        docx_pages.measure_docx_pages(b"", report=report, max_pages=pages - 1)


@pytest.mark.parametrize(("error", "detail"), [
    (extract.CvDependencyMissing(), "pypdf"),
    (extract.CvPdfTimeout(), "worker"),
])
def test_pdf_count_failure_keeps_fallback_and_warning(monkeypatch, error, detail):
    content = docx_writer.render_docx_bytes(docx_writer.parse_markdown_cv("# Jane Example"))
    monkeypatch.setattr(docx_pdf, "render_docx_to_pdf_bytes", lambda _: (b"%PDF-stub", "libreoffice"))

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(extract, "count_pdf_pages", fail)
    count = docx_pages.measure_docx_pages(content)
    assert count.source == "estimated" and count.pages == 1
    assert detail in count.metadata()["page_count_warning"]


def test_estimation_is_marked_and_does_not_penalize_score(monkeypatch, pdf):
    text = "# Jane Example\n\n## Experience\n\n" + ("An example achievement. " * 3 + "\n") * 120
    content = docx_writer.render_docx_bytes(docx_writer.parse_markdown_cv(text))
    estimate = ats.audit_generated_docx(content)
    assert estimate["pages"] >= 2 and estimate["page_count_warning"]
    assert estimate["page_count_source"] == "estimated"
    assert [(f["id"], f["severity"]) for f in estimate["findings"] if f["id"] == "page_count_estimated"] == [
        ("page_count_estimated", "info"),
    ]
    monkeypatch.setattr(docx_pdf, "render_docx_to_pdf_bytes", lambda _: (pdf(estimate["pages"]), "word"))
    assert ats.audit_generated_docx(content)["score"] == estimate["score"]

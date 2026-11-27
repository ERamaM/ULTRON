"""Measure DOCX layout from a rendered PDF, saved pagination, or text flow."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Literal

from ultron.cv import docx_reader, extract
from ultron.infra import docx_pdf

PageCountSource = Literal["pdf_render", "docx_rendered_breaks", "docx_app_props", "estimated"]

_ESTIMATE_WARNING = (
    "Page count is estimated because neither LibreOffice nor Microsoft Word was available to render a reference PDF."
)


@dataclass(frozen=True, slots=True)
class DocxPageCount:
    """A page total with the method and any limitation of the measurement."""

    pages: int
    source: PageCountSource
    converter: str | None = None
    warning: str = ""
    pdf_bytes: bytes | None = field(default=None, repr=False, compare=False)

    def metadata(self) -> dict[str, str | None]:
        """Return the provenance fields shared by audit and build outputs."""
        result: dict[str, str | None] = {
            "page_count_source": self.source,
            "page_count_converter": self.converter,
        }
        if self.warning:
            result["page_count_warning"] = self.warning
        return result


def _estimate_pages(report: docx_reader.DocxStructureReport) -> int:
    # Letter with 7.0 in of writable width and 9.5 in of height, using roughly
    # 90 characters at 10.5 pt and 48 effective lines per page.
    lines = sum(max(1, math.ceil(len(line.expandtabs(4)) / 90)) for line in report.text.splitlines())
    spacing = report.heading_paragraph_count * 1.5 + report.list_paragraph_count * 0.25
    return max(math.ceil((lines + spacing) / 48), report.explicit_page_breaks + 1)


def _fallback(report: docx_reader.DocxStructureReport, warning: str) -> DocxPageCount:
    if report.rendered_page_breaks:
        pages = 1 + max(report.explicit_page_breaks, report.rendered_page_breaks)
        return DocxPageCount(pages, "docx_rendered_breaks", warning=warning)
    if report.app_pages is not None and report.app_pages > report.explicit_page_breaks:
        return DocxPageCount(report.app_pages, "docx_app_props", warning=warning)
    return DocxPageCount(_estimate_pages(report), "estimated", warning=warning or _ESTIMATE_WARNING)


def measure_docx_pages(
    docx_bytes: bytes, *, report: docx_reader.DocxStructureReport | None = None,
    max_pages: int = extract.MAX_CV_PAGES, keep_pdf: bool = False,
) -> DocxPageCount:
    """Return the best available pagination, applying the caller's page cap."""
    if report is None:
        report = docx_reader.inspect_docx(docx_bytes)
    warning = ""
    reference = None
    if rendered := docx_pdf.render_docx_to_pdf_bytes(docx_bytes):
        pdf, converter = rendered
        reference = pdf if keep_pdf else None
        try:
            return DocxPageCount(
                extract.count_pdf_pages(pdf, max_pages=max_pages), "pdf_render", converter, pdf_bytes=reference,
            )
        except extract.CvPdfTooManyPages:
            raise
        except extract.CvDependencyMissing:
            warning = "The reference PDF could not be counted because pypdf is unavailable."
        except extract.CvExtractionError:
            warning = "The PDF worker could not count pages in the rendered reference."
    result = _fallback(report, warning)
    if result.pages > max_pages:
        raise extract.CvPdfTooManyPages(max_pages)
    return replace(result, pdf_bytes=reference)

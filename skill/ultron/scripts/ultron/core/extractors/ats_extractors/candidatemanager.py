"""CandidateManager posting pages: only the vacancy's own fields, never the employer-themed page around them.

Each detail page wraps the vacancy in the employer's whole site (navigation, footer, cookie banner, search facets),
so the page text would mix unrelated places and roles into the posting. The vacancy lives in four ASP.NET
elements: the title heading (with the reference number in a ``<small>``), the category and location rows, and the
details block. The parser rebuilds a small page from them and hands it to the selector parser. Pages without the
details block fall back to the selector parser on the full page.
"""

from __future__ import annotations

import html as html_lib

from bs4 import BeautifulSoup

from ultron.core.models import ExtractedJob, JobRawPage

from .selectors import make_selector_parser

_parse_selectors = make_selector_parser("candidatemanager")
_PREFIX = "ctl00_masterPageBodyContentPlaceholder_"


def _field_text(soup: BeautifulSoup, name: str) -> str:
    row = soup.find(id=f"{_PREFIX}{name}")
    if row is None:
        return ""
    value = row.select_one(".form-control-plaintext") or row
    return " ".join(value.get_text(" ", strip=True).split())


def parse(page: JobRawPage, ats_hints: dict[str, str] | None = None) -> ExtractedJob:
    soup = BeautifulSoup(page.html or "", "html.parser")
    details = soup.find(id=f"{_PREFIX}jobDetailsGroup")
    if details is None:
        return _parse_selectors(page, ats_hints=ats_hints)
    heading = soup.find(id=f"{_PREFIX}jobTitleGroup")
    title = ""
    if heading is not None:
        for small in heading.find_all("small"):
            small.decompose()
        title = " ".join(heading.get_text(" ", strip=True).split())
    location = _field_text(soup, "jobLocationFieldRow")
    category = _field_text(soup, "jobCategoryFieldRow")
    esc = html_lib.escape
    rebuilt = (
        f"<html><head><title>{esc(title)}</title></head><body><main>"
        f'<h1 class="cm-job-title">{esc(title)}</h1>'
        f'<div class="cm-job-location">{esc(location)}</div>'
        f'<div class="cm-job-category">{esc(category)}</div>'
        f'<div class="cm-job-details">{details.decode_contents()}</div>'
        "</main></body></html>"
    )
    return _parse_selectors(page.model_copy(update={"html": rebuilt}), ats_hints=ats_hints)

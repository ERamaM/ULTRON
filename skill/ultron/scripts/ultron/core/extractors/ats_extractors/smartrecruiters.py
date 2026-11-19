from __future__ import annotations

from bs4 import BeautifulSoup

from ultron.core.extractors.ats_extractors.selectors import make_selector_parser
from ultron.core.models import ExtractedJob, JobRawPage
from ultron.core.text_clean import clean_text

_parse_with_selectors = make_selector_parser("smartrecruiters")
_WORKPLACE_TYPES = {"hybrid": "Hybrid", "remote": "Remote", "on_site": "On-site", "onsite": "On-site"}


def parse(page: JobRawPage, ats_hints: dict[str, str] | None = None) -> ExtractedJob:
    parsed = _parse_with_selectors(page, ats_hints)
    soup = BeautifulSoup(page.html, "html.parser")
    place = soup.select_one("spl-job-location")
    updates: dict[str, str] = {}
    if place is not None:
        location = clean_text(place.get("formattedaddress"))
        if location and not (ats_hints or {}).get("location"):
            updates["location"] = location
        workplace = _WORKPLACE_TYPES.get(str(place.get("workplacetype") or "").strip().lower(), "")
        if workplace:
            updates["remote_text"] = workplace
    date = soup.select_one("meta[itemprop='datePosted'][content]")
    if date is not None and (published := clean_text(date.get("content"))):
        updates["posting_date"] = published
    return parsed.model_copy(update=updates) if updates else parsed

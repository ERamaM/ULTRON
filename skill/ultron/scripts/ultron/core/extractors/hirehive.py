"""HireHive posting pages: the selector parser, minus the application form, with the page's own location line.

Every posting page embeds an application form whose country list ends with a "Work Remotely" option; read as page
text it made on-site jobs look remote and filled the body with country names. The page's location line
("<town>, Co. <county>, Ireland") names the country, while the JobPosting JSON-LD only has the locality and an ISO
code, so the line wins when present.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from ultron.core.models import ExtractedJob, JobRawPage

from .base import _first_text
from .selectors import make_selector_parser

_parse_selectors = make_selector_parser("hirehive")


def parse(page: JobRawPage, ats_hints: dict[str, str] | None = None) -> ExtractedJob:
    """Extract job posting fields from a HireHive HTML page."""
    soup = BeautifulSoup(page.html or "", "html.parser")
    for node in soup.select("form, select"):
        node.decompose()
    job = _parse_selectors(page.model_copy(update={"html": str(soup)}), ats_hints=ats_hints)
    location = " ".join(_first_text(soup, (".hh-job-location",)).split())
    if not location or location == job.location:
        return job
    provenance = dict(job._field_provenance)
    provenance["location"] = "selector"
    updated = job.model_copy(update={"location": location})
    updated._field_provenance = provenance
    return updated

"""Job posting extractor for Ashby pages with embedded ``window.__appData``."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from ultron.core.extractors.base import parse_with_overrides
from ultron.core.models import ExtractedJob, JobRawPage

LOCATION_NAME_RE = re.compile(r'"locationName"\s*:\s*"([^"]+)"')
WORKPLACE_RE = re.compile(r'"(?:workplaceType|remoteStatus|workplace)"\s*:\s*"([^"]+)"', re.IGNORECASE)
WORKPLACE_TYPES = {"hybrid": "Hybrid", "remote": "Remote", "onsite": "On-site"}
_APP_DATA_RE = re.compile(r"\bwindow\.__appData\s*=\s*")

# How many characters around the job-title match to search for job-scoped
# fields; embedded board data can contain many other jobs' locations.
_SCOPE_WINDOW = 4000


def _decode_json_text(value: str) -> str:
    # The captured value is a JSON string body; decode all escapes properly
    # (\uXXXX, \/, \n, ...) so e.g. "Zürich" becomes "Zürich".
    try:
        decoded = json.loads(f'"{value}"')
    except (json.JSONDecodeError, ValueError):
        decoded = value.replace("\\/", "/").replace("\\u0026", "&")
    return str(decoded).strip()


def _scoped_search(pattern: re.Pattern[str], html: str, anchor: str) -> re.Match[str] | None:
    """Find the match closest to the job's own data (anchored on its title) so
    we don't pick up fields that belong to a different job in the embedded
    board payload. Falls back to the first match anywhere."""
    anchor = (anchor or "").strip()
    if anchor:
        needles = [anchor]
        escaped = json.dumps(anchor)[1:-1]
        if escaped != anchor:
            needles.insert(0, escaped)
        for needle in needles:
            idx = html.find(needle)
            if idx == -1:
                continue
            best: re.Match[str] | None = None
            best_distance = _SCOPE_WINDOW + 1
            for match in pattern.finditer(html):
                if match.start() >= idx + len(needle):
                    distance = match.start() - (idx + len(needle))
                else:
                    distance = idx - match.end()
                if 0 <= distance < best_distance:
                    best = match
                    best_distance = distance
            if best is not None:
                return best
            break
    return pattern.search(html)


def _current_posting(page: JobRawPage) -> dict:
    match = _APP_DATA_RE.search(page.html)
    if match is None:
        return {}
    try:
        app_data, _ = json.JSONDecoder().raw_decode(page.html, match.end())
    except (ValueError, RecursionError):
        return {}
    posting = app_data.get("posting") if isinstance(app_data, dict) else None
    parts = urlsplit(page.final_url).path.strip("/").split("/")
    if isinstance(posting, dict) and len(parts) >= 2 and posting.get("id") == parts[1]:
        return posting
    return {}


def parse(page: JobRawPage, ats_hints: dict[str, str] | None = None) -> ExtractedJob:
    """Parse an Ashby posting, falling back to shared selectors as needed."""
    parsed = parse_with_overrides(
        page,
        ats_hints=ats_hints,
        use_jsonld_description=True,
        title_selectors=(
            "h1[data-testid='job-title']",
            "h1",
        ),
        company_selectors=(
            "[data-testid='company-name']",
            ".ashby-job-posting-company",
        ),
        location_selectors=(
            "[data-testid='job-location']",
            ".ashby-job-posting-location",
            ".job-location",
        ),
        remote_selectors=(
            "[data-testid='job-workplace']",
            "[data-testid='job-location']",
            ".ashby-job-posting-location",
            ".job-location",
        ),
    )

    updates: dict[str, str] = {}
    if not parsed.location:
        match = _scoped_search(LOCATION_NAME_RE, page.html, parsed.title)
        if match:
            updates["location"] = _decode_json_text(match.group(1))

    posting = _current_posting(page)
    workplace = str((ats_hints or {}).get("remote_hint") or posting.get("workplaceType") or "").strip()
    if not workplace and not parsed.remote_text:
        match = _scoped_search(WORKPLACE_RE, page.html, parsed.title)
        if match:
            workplace = _decode_json_text(match.group(1))
    workplace_type = WORKPLACE_TYPES.get(re.sub(r"[-_ ]", "", workplace.lower()))
    if workplace_type:
        # The posting's explicit workplace type takes precedence over the broad
        # TELECOMMUTE flag Ashby also emits for hybrid jobs.
        updates["remote_text"] = workplace_type
    elif workplace and not parsed.remote_text:
        updates["remote_text"] = workplace

    if not updates:
        return parsed
    return parsed.model_copy(update=updates)

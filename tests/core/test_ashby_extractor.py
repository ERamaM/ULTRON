from __future__ import annotations

from ultron.core.extractors.ats_extractors import ashby
from ultron.core.models import JobRawPage


def _page(html: str) -> JobRawPage:
    url = "https://jobs.ashbyhq.com/acme/senior-ml-engineer"
    return JobRawPage(url=url, final_url=url, status_code=200, html=html)


def test_parse_uses_data_testid_selectors_when_present():
    html = """
    <html><body>
    <h1 data-testid="job-title">Senior ML Engineer</h1>
    <div data-testid="company-name">Acme</div>
    <div data-testid="job-location">Remote - EU</div>
    <div data-testid="job-workplace">Remote</div>
    </body></html>
    """
    job = ashby.parse(_page(html))
    assert job.title == "Senior ML Engineer"
    assert job.company == "Acme"
    assert "Remote" in job.location or "EU" in job.location
    assert "Remote" in job.remote_text


def test_parse_falls_back_to_class_selectors():
    html = """
    <html><body>
    <h1>Backend Engineer</h1>
    <div class="ashby-job-posting-company">Acme Co</div>
    <div class="ashby-job-posting-location">Madrid, Spain</div>
    </body></html>
    """
    job = ashby.parse(_page(html))
    assert job.title == "Backend Engineer"
    assert job.company == "Acme Co"


def test_parse_extracts_location_and_workplace_from_embedded_json_when_selectors_miss():
    html = """
    <html><body>
    <h1>Staff Engineer</h1>
    <script>
    window.__appData = {"jobPosting": {"title": "Staff Engineer",
      "locationName": "Berlin, Germany",
      "workplaceType": "Remote"}};
    </script>
    </body></html>
    """
    job = ashby.parse(_page(html))
    assert job.title == "Staff Engineer"
    assert "Berlin" in job.location
    assert "Remote" in job.remote_text


def test_parse_json_scoped_search_prefers_match_near_title_anchor():
    # The anchor is the h1-extracted title's first occurrence in the raw HTML; the
    # locationName physically closest to that position wins (distance-based scoping).
    html = """
    <html><body>
    <h1>Target Role</h1>
    <script>
    window.__data = [
      {"title": "Target Role", "locationName": "Lisbon, Portugal"},
      {"title": "Other Role", "locationName": "Paris, France"}
    ];
    </script>
    </body></html>
    """
    job = ashby.parse(_page(html))
    assert "Lisbon" in job.location or "Portugal" in job.location


def test_parse_decodes_unicode_and_slash_escapes_in_json_matches():
    html = """
    <html><body>
    <h1>Role In Zurich</h1>
    <script>
    window.__data = {"title": "Role In Zurich", "locationName": "Z\\u00fcrich \\/ Remote"};
    </script>
    </body></html>
    """
    job = ashby.parse(_page(html))
    assert "Z" in job.location  # decoded unicode present, not the raw escape sequence
    assert "\\u00fc" not in job.location


def test_parse_no_json_match_leaves_location_and_remote_empty():
    html = "<html><body><h1>Solo Title</h1></body></html>"
    job = ashby.parse(_page(html))
    assert job.location == ""


def test_scoped_search_falls_back_to_first_match_without_anchor():
    import re

    pattern = re.compile(r'"locationName"\s*:\s*"([^"]+)"')
    html = '{"locationName": "Only Match"}'
    match = ashby._scoped_search(pattern, html, anchor="")
    assert match is not None
    assert match.group(1) == "Only Match"


def test_decode_json_text_handles_invalid_escape_gracefully():
    assert ashby._decode_json_text("Plain text") == "Plain text"
    assert ashby._decode_json_text("Berlin \\/ Remote") == "Berlin / Remote"


def test_parse_reads_jsonld_description_when_page_requires_javascript():
    html = """
    <html><head><script type="application/ld+json">
    {"@type":"JobPosting", "title":"Senior ML Engineer",
     "description":"<p>Build ML systems.</p><p>Work remotely from Spain &amp; Portugal.</p>"}
    </script></head><body><noscript>You need to enable JavaScript to run this app.</noscript></body></html>
    """
    job = ashby.parse(_page(html))
    assert job.body_text == "Build ML systems.\nWork remotely from Spain & Portugal."

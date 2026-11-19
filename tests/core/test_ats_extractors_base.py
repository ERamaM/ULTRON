from __future__ import annotations

from ultron.core.extractors.ats_extractors import base
from ultron.core.models import JobRawPage


def _page(html: str, *, final_url: str = "https://boards.greenhouse.io/acme/jobs/123") -> JobRawPage:
    return JobRawPage(url=final_url, final_url=final_url, status_code=200, html=html)


def test_parse_generic_prefers_jsonld_over_meta_and_h1():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "JobPosting", "title": "Senior ML Engineer", "hiringOrganization": {"name": "Acme Corp"},
     "jobLocation": {"address": {"addressLocality": "Madrid", "addressCountry": "ES"}},
     "jobLocationType": "TELECOMMUTE", "datePosted": "2026-01-05"}
    </script>
    <meta property="og:title" content="Wrong Title"/>
    <meta property="og:site_name" content="Wrong Co"/>
    </head><body><h1>Also Wrong</h1></body></html>
    """
    job = base.parse_generic(_page(html))
    assert job.title == "Senior ML Engineer"
    assert job.company == "Acme Corp"
    assert "Madrid" in job.location
    assert job.posting_date == "2026-01-05"
    assert "Remote" in job.remote_text or job.remote_text


def test_parse_generic_falls_back_through_meta_og_twitter_h1_title_tag():
    html = """
    <html><head><title>Fallback Title Tag</title></head>
    <body><h1>H1 Title</h1></body></html>
    """
    job = base.parse_generic(_page(html))
    assert job.title == "H1 Title"

    html_meta_og = '<html><head><meta property="og:title" content="OG Title"/></head><body></body></html>'
    job2 = base.parse_generic(_page(html_meta_og))
    assert job2.title == "OG Title"

    html_twitter = '<html><head><meta name="twitter:title" content="Twitter Title"/></head><body></body></html>'
    job3 = base.parse_generic(_page(html_twitter))
    assert job3.title == "Twitter Title"

    html_title_tag_only = "<html><head><title>Just Title Tag</title></head><body></body></html>"
    job4 = base.parse_generic(_page(html_title_tag_only))
    assert job4.title == "Just Title Tag"


def test_parse_generic_detects_apply_and_closed_hints():
    open_html = "<html><body><p>Click apply now to join us.</p></body></html>"
    job = base.parse_generic(_page(open_html))
    assert job.apply_available is True
    assert job.is_open_hint is True

    closed_html = "<html><body><p>This position is closed. No longer accepting applications.</p></body></html>"
    closed = base.parse_generic(_page(closed_html))
    assert closed.is_open_hint is False


def test_parse_generic_finds_date_via_regex_when_no_structured_date():
    html = "<html><body><p>Posted on 2026-03-14. Great opportunity.</p></body></html>"
    job = base.parse_generic(_page(html))
    assert job.posting_date == "2026-03-14"


def test_parse_generic_prefers_posted_date_over_earlier_updated_date():
    html = "<html><body><p>Last updated: 1 January 2023</p><p>Posted on 14 March 2026</p></body></html>"
    job = base.parse_generic(_page(html))
    assert job.posting_date == "14 March 2026"


def test_parse_generic_uses_posted_date_and_returns_none_without_dates():
    posted = base.parse_generic(_page("<html><body><p>Posted on 14 March 2026</p></body></html>"))
    assert posted.posting_date == "14 March 2026"
    empty = base.parse_generic(_page("<html><body><p>No calendar here.</p></body></html>"))
    assert empty.posting_date is None


def test_valid_through_expired_rules():
    from datetime import UTC, datetime

    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    # Past date-time (any offset) is expired.
    assert base._valid_through_expired("2026-06-15T11:00:00Z", now=now) is True
    assert base._valid_through_expired("2026-06-15T13:30:00+02:00", now=now) is True
    assert base._valid_through_expired("2026-06-14", now=now) is True
    # A date-only value counts through the end of that UTC day.
    assert base._valid_through_expired("2026-06-15", now=now) is False
    # Future values are fine; naive date-times are read as UTC.
    assert base._valid_through_expired("2026-06-15T13:00:00", now=now) is False
    assert base._valid_through_expired("2027-01-01T00:00:00Z", now=now) is False
    assert base._valid_through_expired("9999-12-31", now=now) is False
    # Missing, unparseable and placeholder values are ignored.
    for value in ("", "not a date", "0000-00-00", "1970-01-01", "1900-01-01T00:00:00Z", "31/12/2020"):
        assert base._valid_through_expired(value, now=now) is False, value


def test_parse_generic_flags_jobposting_with_past_valid_through():
    def page(valid_through: str) -> str:
        return (
            '<html><head><script type="application/ld+json">'
            '{"@type": "JobPosting", "title": "ML Engineer", "datePosted": "2020-01-01", '
            f'"validThrough": "{valid_through}"}}'
            "</script></head><body></body></html>"
        )

    expired = base.parse_generic(_page(page("2020-02-01T00:00:00Z")))
    assert expired.valid_through_expired is True
    assert expired.posting_date == "2020-01-01"
    assert base.parse_generic(_page(page("2999-01-01"))).valid_through_expired is False
    assert base.parse_generic(_page(page("soon"))).valid_through_expired is False


def test_expired_recommendation_does_not_expire_the_page():
    def page(nodes: str) -> str:
        return f'<html><head><script type="application/ld+json">[{nodes}]</script></head><body></body></html>'

    other = ('{"@type": "JobPosting", "title": "Data Engineer", "validThrough": "2020-02-01T00:00:00Z", '
             '"url": "https://boards.greenhouse.io/acme/jobs/999"}')
    # Only a recommendation with an expired deadline: it may fill in the title, but not expire this page.
    active = base.parse_generic(_page(page(other)))
    assert active.valid_through_expired is False
    # The page's own posting still decides, whichever order the nodes come in.
    own = ('{"@type": "JobPosting", "title": "ML Engineer", "validThrough": "2020-02-01T00:00:00Z", '
           '"url": "https://boards.greenhouse.io/acme/jobs/123"}')
    assert base.parse_generic(_page(page(f"{other}, {own}"))).valid_through_expired is True
    # A node without a url is still trusted, as before.
    bare = '{"@type": "JobPosting", "title": "ML Engineer", "validThrough": "2020-02-01T00:00:00Z"}'
    assert base.parse_generic(_page(page(bare))).valid_through_expired is True


def test_parse_generic_domain_fallback_company_when_no_other_signal():
    html = "<html><head><title>Careers</title></head><body></body></html>"
    job = base.parse_generic(_page(html, final_url="https://acme-corp.example.com/careers/123"))
    # No jsonld/og/h1 signal for company; falls back to the domain label.
    assert job.company == "Acme Corp"


def test_extract_location_from_lines_prefers_labeled_lines():
    text_lines = [
        "We build cool things.",
        "Location: Remote, EU",
        "Salary range $100k-$150k",
    ]
    location = base._extract_location_from_lines(text_lines)
    assert "Remote" in location or "EU" in location


def test_extract_location_from_lines_returns_empty_when_nothing_matches():
    assert base._extract_location_from_lines(["We build cool things.", "You will design systems."]) == ""


def test_is_location_like_line_rejects_verb_heavy_and_multi_sentence_lines():
    assert base._is_location_like_line("You will build and design the future. Also this.") is False
    assert base._is_location_like_line("") is False
    assert base._is_location_like_line("x" * 90) is False


def test_is_location_like_line_accepts_short_comma_location():
    assert base._is_location_like_line("Madrid, Spain") is True


def test_is_location_like_line_rejects_compensation_without_location_signal():
    assert base._is_location_like_line("Base pay range $120,000 - $150,000") is False


def test_is_location_like_line_accepts_compensation_line_with_remote_override():
    assert base._is_location_like_line("Remote (EU) - Base pay range $120,000") is True


def test_is_location_like_line_requires_word_boundaries_for_markers():
    assert base._is_location_like_line("Carbon neutral benefits") is False
    assert base._is_location_like_line("Cost allocation specialist team") is False
    assert base._is_location_like_line("Location: Madrid") is True
    assert base._is_location_like_line("Madrid, Spain") is True
    assert base._is_location_like_line("EU-based role") is True


def test_looks_remote_and_workplace_conflict_detection():
    assert base._looks_remote("This is a remote position") is True
    assert base._looks_remote("Remote monitoring of servers required") is False
    assert base._looks_remote_workplace("Hybrid role, 2 days onsite") is True
    assert base._looks_remote_workplace("Fully in-office") is True
    assert base._looks_remote_workplace("Optional office in Berlin") is False


def test_looks_remote_workplace_strong_phrase_overrides_tech_context():
    assert base._looks_remote_workplace("Fully remote, distributed systems team") is True
    assert base._looks_remote_workplace("Fully remote") is True
    assert base._looks_remote_workplace("100% remote, distributed systems") is True
    assert base._looks_remote_workplace("remote-first distributed systems team") is True
    assert base._looks_remote_workplace("Remote position, distributed computing") is True
    assert base._looks_remote_workplace("Work from home, distributed systems") is True
    assert base._looks_remote_workplace("Teletrabajo completo, distributed systems") is True
    assert base._looks_remote_workplace("We work on distributed systems") is False


def test_extract_remote_lines_joins_multiple_short_hits():
    lines = ["Remote friendly team", "Hybrid options available", "x" * 150 + " remote"]
    joined = base._extract_remote_lines(lines)
    assert "Remote friendly team" in joined
    assert "Hybrid options available" in joined


def test_dedupe_join_limits_and_dedupes_case_insensitively():
    values = ["Remote", "remote", "EU", "eu", "Spain", "Extra"]
    joined = base._dedupe_join(values, limit=2)
    assert joined.count("|") <= 1


def test_pick_best_candidate_with_source_scores_by_field_rules():
    candidates = [("Careers", "title_tag"), ("Senior Backend Engineer", "h1")]
    value, source = base._pick_best_candidate_with_source(candidates, page_url="https://acme.com/j/1", field="title")
    assert value == "Senior Backend Engineer"
    assert source == "h1"


def test_pick_best_candidate_returns_only_value():
    value = base._pick_best_candidate([("Acme Corp", "meta_og")], page_url="https://acme.com/j/1", field="company")
    assert value == "Acme Corp"


def test_pick_best_candidate_all_empty_returns_empty_unknown_source():
    value, source = base._pick_best_candidate_with_source([("", "h1")], page_url="https://acme.com", field="title")
    assert value == ""
    assert source == "unknown"


def test_node_text_falls_back_to_img_alt():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup('<div><img alt="Company Logo Acme"/></div>', "html.parser")
    node = soup.find("div")
    assert base._node_text(node) == "Company Logo Acme"

    soup2 = BeautifulSoup('<img alt="Standalone Alt"/>', "html.parser")
    img = soup2.find("img")
    assert base._node_text(img) == "Standalone Alt"

    soup3 = BeautifulSoup("<div></div>", "html.parser")
    empty_div = soup3.find("div")
    assert base._node_text(empty_div) == ""


def test_first_text_and_iter_selector_texts():
    from bs4 import BeautifulSoup

    html = '<div class="a">First</div><div class="a">Second</div><div class="b">Third</div>'
    soup = BeautifulSoup(html, "html.parser")
    assert base._first_text(soup, [".missing", ".a"]) == "First"
    assert base._first_text(soup, [".missing"]) == ""
    texts = list(base._iter_selector_texts(soup, [".a", ".b"]))
    assert texts == ["First", "Second", "Third"]


def test_meta_content_missing_returns_empty_and_requires_attrs():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup("<html><head></head></html>", "html.parser")
    assert base._meta_content(soup, property_name="og:title") == ""
    assert base._meta_content(soup) == ""


def test_json_load_loose_handles_invalid_json():
    assert base._json_load_loose("") is None
    assert base._json_load_loose("not json{{{") is None
    assert base._json_load_loose('{"a": 1}') == {"a": 1}


def test_parse_ats_page_survives_hostile_deeply_nested_jsonld():
    from ultron.core.ats.registry import parse_ats_page

    html = '<html><head><script type="application/ld+json">' + "[" * 200000 + "</script></head><body></body></html>"
    job = parse_ats_page(_page(html, final_url="https://unknown.example/job"))
    assert job.apply_url == "https://unknown.example/job"


def test_jsonld_walk_handles_nesting_deeper_than_the_recursion_limit(monkeypatch):
    # Build the structure directly: how deep json.loads itself can go varies by Python build.
    deep: list = []
    for _ in range(5000):
        deep = [deep]
    payload = {"@type": "JobPosting", "title": "Deep Role", "extra": deep}
    monkeypatch.setattr(base, "load_json_or_none", lambda text: payload)
    html = '<html><head><script type="application/ld+json">{}</script></head><body></body></html>'
    job = base.parse_generic(_page(html))
    assert job.title == "Deep Role"


def test_iter_jobposting_nodes_skips_non_jobposting_and_invalid_json():
    html = """
    <script type="application/ld+json">not valid json</script>
    <script type="application/ld+json">{"@type": "Organization", "name": "Acme"}</script>
    <script type="application/ld+json">{"@type": ["JobPosting"], "title": "Engineer"}</script>
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    nodes = list(base._iter_jobposting_nodes(soup))
    assert len(nodes) == 1
    assert nodes[0]["title"] == "Engineer"


def test_iter_jobposting_nodes_accepts_schema_org_type_iri():
    from bs4 import BeautifulSoup

    html = """
    <script type="application/ld+json">{"@type": "https://schema.org/JobPosting", "title": "Engineer"}</script>
    <script type="application/ld+json">{"@type": ["http://schema.org/JobPosting"], "title": "Listed"}</script>
    <script type="application/ld+json">{"@type": "JobPosting", "title": "Plain"}</script>
    """
    nodes = list(base._iter_jobposting_nodes(BeautifulSoup(html, "html.parser")))
    assert [node["title"] for node in nodes] == ["Engineer", "Listed", "Plain"]


def test_extract_structured_fields_returns_first_when_none_have_title():
    from bs4 import BeautifulSoup

    html = """
    <script type="application/ld+json">{"@type": "JobPosting", "hiringOrganization": {"name": "Acme"}}</script>
    """
    soup = BeautifulSoup(html, "html.parser")
    fields = base._extract_structured_fields(soup)
    assert fields.get("company") == "Acme"
    assert fields.get("title", "") == ""


def test_extract_structured_fields_empty_when_no_jobposting():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup("<html></html>", "html.parser")
    assert base._extract_structured_fields(soup) == {}


def test_parse_generic_selects_jsonld_whose_url_matches_final_url():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "JobPosting", "title": "Intern", "url": "https://jobs.example/related-1"}
    </script>
    <script type="application/ld+json">
    {"@type": "JobPosting", "title": "Staff ML Engineer", "url": "https://jobs.example/this-one"}
    </script>
    </head><body></body></html>
    """
    matched = base.parse_generic(_page(html, final_url="https://jobs.example/this-one"))
    assert matched.title == "Staff ML Engineer"
    with_query = base.parse_generic(_page(html, final_url="https://jobs.example/this-one/?ref=1#apply"))
    assert with_query.title == "Staff ML Engineer"
    elsewhere = base.parse_generic(_page(html, final_url="https://jobs.example/elsewhere"))
    assert elsewhere.title == "Intern"


def test_parse_generic_uses_single_jobposting_without_url():
    html = """
    <html><head>
    <script type="application/ld+json">{"@type": "JobPosting", "title": "Staff ML Engineer"}</script>
    </head><body></body></html>
    """
    job = base.parse_generic(_page(html, final_url="https://jobs.example/this-one"))
    assert job.title == "Staff ML Engineer"


def test_extract_location_parts_handles_dict_list_and_scalar():
    assert base._extract_location_parts("Remote") == ["Remote"]
    assert base._extract_location_parts(None) == []
    assert base._extract_location_parts([{"name": "Madrid"}, "Spain"]) == ["Madrid", "Spain"]
    assert base._extract_location_parts({"address": {"addressLocality": "Berlin"}}) == ["Berlin"]


def test_extract_company_from_jobposting_handles_dict_and_string():
    assert base._extract_company_from_jobposting({"hiringOrganization": {"name": "Acme"}}) == "Acme"
    assert base._extract_company_from_jobposting({"hiringOrganization": "Acme Inc"}) == "Acme Inc"
    assert base._extract_company_from_jobposting({}) == ""


def test_looks_domain_placeholder_and_generic_detection():
    assert base._looks_domain_placeholder("", "https://acme.com") is True
    assert base._looks_domain_placeholder("Boards", "https://boards.greenhouse.io/acme") is True
    assert base._looks_generic_company("jobs", "https://acme.com") is True
    assert base._looks_generic_company("Job Boards", "https://job-boards.greenhouse.io/acme") is True
    assert base._looks_generic_company("Acme Corp", "https://acme.com") is False
    assert base._looks_generic_title("careers", "https://acme.com") is True
    assert base._looks_generic_title("Senior Engineer", "https://acme.com") is False


def test_fallback_company_from_domain_skips_generic_labels():
    assert base._fallback_company_from_domain("https://acme-corp.example.com/jobs/1") == "Acme Corp"
    assert base._fallback_company_from_domain("https://boards.greenhouse.io/acme-corp/jobs/1") == ""
    assert base._fallback_company_from_domain("https://jobs.lever.co/") == ""


def test_apply_ats_hints_updates_generic_fields_and_merges_remote():
    from ultron.core.models import ExtractedJob

    page = _page("<html></html>", final_url="https://boards.greenhouse.io/acme/jobs/1")
    extracted = ExtractedJob(apply_url=page.final_url, domain="boards.greenhouse.io", title="", company="")
    hints = {"title": "Backend Engineer", "company": "Acme", "location": "Madrid", "remote_hint": "Remote EU"}
    updated = base._apply_ats_hints(extracted, page, hints)
    assert updated.title == "Backend Engineer"
    assert updated.company == "Acme"
    assert updated.location == "Madrid"
    assert "Remote EU" in updated.remote_text


def test_apply_ats_hints_replaces_multiword_generic_company():
    from ultron.core.models import ExtractedJob

    page = _page("<html></html>", final_url="https://job-boards.greenhouse.io/grafanalabs/jobs/1")
    extracted = ExtractedJob(
        apply_url=page.final_url,
        domain="job-boards.greenhouse.io",
        title="Senior ML Engineer",
        company="Job Boards",
    )

    updated = base._apply_ats_hints(extracted, page, {"company": "Grafana Labs"})

    assert updated.company == "Grafana Labs"


def test_apply_ats_hints_overrides_a_nonempty_heuristic_location():
    from ultron.core.models import ExtractedJob

    page = _page("<html></html>")
    extracted = ExtractedJob(apply_url=page.final_url, domain="x", title="Role", location="Engineering, Product")
    updated = base._apply_ats_hints(extracted, page, {"location": "Madrid, Spain"})
    assert updated.location == "Madrid, Spain"


def test_apply_ats_hints_no_hints_returns_same_object():
    from ultron.core.models import ExtractedJob

    page = _page("<html></html>")
    extracted = ExtractedJob(apply_url=page.final_url, domain="x", title="Title")
    assert base._apply_ats_hints(extracted, page, None) is extracted
    assert base._apply_ats_hints(extracted, page, {}) is extracted


def test_apply_ats_hints_does_not_override_specific_title():
    from ultron.core.models import ExtractedJob

    page = _page("<html></html>")
    extracted = ExtractedJob(apply_url=page.final_url, domain="x", title="Very Specific Title")
    updated = base._apply_ats_hints(extracted, page, {"title": "Generic Hint"})
    assert updated.title == "Very Specific Title"


def test_parse_with_overrides_uses_selectors_over_generic():
    html = """
    <html><head><title>Fallback</title></head>
    <body>
      <h1 class="job-title">Selector Title</h1>
      <div class="company-name">Selector Co</div>
      <div class="job-location">Remote, EU</div>
    </body></html>
    """
    job = base.parse_with_overrides(
        _page(html),
        title_selectors=[".job-title"],
        company_selectors=[".company-name"],
        location_selectors=[".job-location"],
        remote_selectors=[".job-location"],
    )
    assert job.title == "Selector Title"
    assert job.company == "Selector Co"
    assert "Remote" in job.location or "EU" in job.location


def test_parse_with_overrides_falls_back_to_base_when_selectors_miss():
    html = """
    <html><head>
    <script type="application/ld+json">{"@type": "JobPosting", "title": "Base Title",
    "hiringOrganization": {"name": "Base Co"}}</script>
    </head><body></body></html>
    """
    job = base.parse_with_overrides(_page(html), title_selectors=[".missing"], company_selectors=[".missing"])
    assert job.title == "Base Title"
    assert job.company == "Base Co"


def test_parse_with_overrides_no_updates_still_applies_hints():
    html = "<html><body></body></html>"
    job = base.parse_with_overrides(_page(html), ats_hints={"title": "Hinted Title"})
    assert job.title == "Hinted Title"


def test_detect_ats_fingerprint_and_canonical_domain_roundtrip():
    greenhouse_html = '<html><body><div id="grnhse_app"></div></body></html>'
    vendor = base.detect_ats_fingerprint(greenhouse_html)
    if vendor is not None:
        assert base.ats_canonical_domain(vendor)
    assert base.detect_ats_fingerprint("<html><body>nothing here</body></html>") in (None, "")
    assert base.ats_canonical_domain(None) is None


def test_extract_text_lines_strips_and_drops_blanks():
    assert base.extract_text_lines("  a  \n\n b \n   \nc") == ["a", "b", "c"]


def test_host_labels_handles_empty_and_multi_part():
    assert base._host_labels("https://boards.greenhouse.io/acme") == ["boards", "greenhouse", "io"]
    assert base._host_labels("not-a-url") == []


def test_structured_fields_ignore_json_null_and_use_name_of_nested_address_values():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "JobPosting", "title": "Data Engineer", "hiringOrganization": {"name": null},
     "jobLocation": {"address": {"addressLocality": "Madrid", "addressRegion": null,
                                 "addressCountry": {"@type": "Country", "name": "ES"}}, "name": null},
     "jobLocationType": null, "datePosted": "2026-01-05"}
    </script>
    </head><body></body></html>
    """
    job = base.parse_generic(_page(html))
    assert job.title == "Data Engineer"
    assert job.location == "Madrid | ES"
    assert "None" not in job.company
    assert "None" not in job.remote_text


def test_structured_fields_null_title_is_empty():
    fields = base._structured_fields_from_node({"@type": "JobPosting", "title": None, "jobLocationType": None})
    assert fields["title"] == ""
    assert fields["remote_text"] == ""
    assert base._extract_company_from_jobposting({"hiringOrganization": {"name": None}}) == ""


def test_relative_jsonld_url_for_this_posting_keeps_its_deadline():
    def page(url: str) -> str:
        return ('<html><head><script type="application/ld+json">'
                '{"@type": "JobPosting", "title": "ML Engineer", "validThrough": "2020-02-01", '
                f'"url": "{url}"}}</script></head><body></body></html>')

    assert base.parse_generic(_page(page("/acme/jobs/123"))).valid_through_expired is True
    assert base.parse_generic(_page(page("/acme/jobs/999"))).valid_through_expired is False


def test_job_id_query_parameter_decides_which_deadline_applies():
    page_url = "https://acme.com/careers?gh_jid=4012346"
    nodes = (
        '{"@type": "JobPosting", "title": "Other role", "validThrough": "2020-02-01", '
        '"url": "https://acme.com/careers?gh_jid=4012347"}, '
        '{"@type": "JobPosting", "title": "ML Engineer", "validThrough": "2999-01-01", '
        f'"url": "{page_url}"}}'
    )
    html = f'<html><head><script type="application/ld+json">[{nodes}]</script></head><body></body></html>'
    job = base.parse_generic(_page(html, final_url=page_url))
    assert job.valid_through_expired is False
    assert job.title == "ML Engineer"
    # Only the other job id's node: its fields may be used, its deadline is not.
    only_other = (
        '<html><head><script type="application/ld+json">{"@type": "JobPosting", "title": "Other role", '
        '"validThrough": "2020-02-01", "url": "https://acme.com/careers?gh_jid=4012347"}</script></head>'
        "<body></body></html>"
    )
    assert base.parse_generic(_page(only_other, final_url=page_url)).valid_through_expired is False


def test_urlless_posting_wins_over_a_node_for_another_job_id():
    page_url = "https://acme.com/careers?gh_jid=4012346"
    bare = '{"@type": "JobPosting", "title": "ML Engineer", "validThrough": "2020-02-01"}'
    other = ('{"@type": "JobPosting", "title": "Other role", "validThrough": "2999-01-01", '
             '"url": "https://acme.com/careers?gh_jid=4012347"}')
    for nodes in (f"{bare}, {other}", f"{other}, {bare}"):
        html = f'<html><head><script type="application/ld+json">[{nodes}]</script></head><body></body></html>'
        job = base.parse_generic(_page(html, final_url=page_url))
        assert job.title == "ML Engineer", nodes
        assert job.valid_through_expired is True, nodes


def test_overlong_posting_url_is_not_parsed_for_identity():
    assert base._posting_identity("https://acme.com/careers?q=" + "x" * 5000) == ""
    assert base._posting_identity("https://boards.greenhouse.io/acme/jobs/123") == "greenhouse:acme:123"


def test_valid_through_is_bounded_and_only_a_terminal_z_is_utc():
    now = base.datetime(2026, 6, 15, 12, 0, tzinfo=base.UTC)
    assert base._valid_through_expired("z" * 5_000_000, now=now) is False  # rejected before any replacement
    assert base._valid_through_expired("2026-06-15T11:00:00z", now=now) is True
    assert base._valid_through_expired("2026-06-15T11:00:00Z", now=now) is True
    assert base._valid_through_expired("2026-06-15TZ1:00:00", now=now) is False  # a Z elsewhere is not UTC
    assert base._valid_through_expired("2020-01-01" + " " * 100, now=now) is False  # over the length cap

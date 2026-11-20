"""Page-title suffixes (site or careers names) must not leak into extracted job titles."""

from __future__ import annotations

import pytest

from ultron.core.ats.registry import parse_ats_page
from ultron.core.extractors.ats_extractors import base
from ultron.core.models import JobRawPage
from ultron.core.rules.role_matching import is_senior_title


def _page(html: str, *, final_url: str = "https://boards.greenhouse.io/examplebus/jobs/1001") -> JobRawPage:
    return JobRawPage(url=final_url, final_url=final_url, status_code=200, html=html)


def _head(og_title: str, *, site_name: str = "", title_tag: str = "") -> str:
    site = f'<meta property="og:site_name" content="{site_name}"/>' if site_name else ""
    tag = f"<title>{title_tag}</title>" if title_tag else ""
    return f'<html><head>{tag}<meta property="og:title" content="{og_title}"/>{site}</head>'


def test_og_title_slogan_with_company_is_stripped_and_seniority_not_inferred():
    # Modelled on a live Greenhouse page whose og:title appends the employer's careers slogan.
    html = _head(
        "Data Platform Engineer (m/f/d) | Lead The Way - with ExampleBus.", site_name="ExampleBus Career"
    ) + "<body><p>Job Detail - ExampleBus Career</p></body></html>"
    hints = {"title": "Data Platform Engineer (m/f/d)", "company": "ExampleBus"}
    job = parse_ats_page(_page(html), ats_hints=hints)
    assert job.title == "Data Platform Engineer (m/f/d)"
    assert not is_senior_title(job.title)  # "Lead" came from the slogan only


def test_og_title_suffix_stripped_without_ats_hint_when_it_names_the_site():
    html = _head("Backend Engineer | Lead The Way - with ExampleBus.", site_name="ExampleBus Career")
    job = base.parse_generic(_page(html + "<body></body></html>", final_url="https://careers.examplebus.test/jobs/7"))
    assert job.title == "Backend Engineer"


@pytest.mark.parametrize(
    ("og_title", "expected"),
    [
        ("Senior Data Engineer - Acme Careers", "Senior Data Engineer"),
        ("Senior Data Engineer | Acme", "Senior Data Engineer"),
        ("Senior Data Engineer – Jobs at Acme", "Senior Data Engineer"),
        # Legitimate parts of a title are kept.
        ("Backend Engineer - Payments", "Backend Engineer - Payments"),
        ("Software Engineer - Jobs Platform", "Software Engineer - Jobs Platform"),
        ("Engineer - Payments at Scale", "Engineer - Payments at Scale"),
    ],
)
def test_title_tag_and_og_suffixes(og_title, expected):
    html = _head(og_title, site_name="Acme") + "<body></body></html>"
    job = base.parse_generic(_page(html, final_url="https://jobs.acme.test/openings/42"))
    assert job.title == expected


def test_title_tag_suffix_stripped():
    html = "<html><head><title>Machine Learning Engineer - Acme Careers</title></head><body></body></html>"
    job = base.parse_generic(_page(html, final_url="https://www.acme.test/careers/42"))
    assert job.title == "Machine Learning Engineer"


def test_ats_hint_wins_over_page_title_that_extends_it():
    html = _head("Senior Fullstack Engineer (Python/Java) - Service Agreement | Explore More - ExampleBus") + (
        "<body></body></html>"
    )
    hints = {"title": "Senior Fullstack Engineer (Python/Java) - Service Agreement"}
    job = parse_ats_page(_page(html), ats_hints=hints)
    assert job.title == "Senior Fullstack Engineer (Python/Java) - Service Agreement"


def test_jsonld_title_is_never_stripped():
    html = """
    <html><head>
    <script type="application/ld+json">{"@type": "JobPosting", "title": "Platform Engineer - Acme Cloud"}</script>
    <meta property="og:site_name" content="Acme"/>
    </head><body></body></html>
    """
    job = base.parse_generic(_page(html, final_url="https://jobs.acme.test/openings/42"))
    assert job.title == "Platform Engineer - Acme Cloud"


@pytest.mark.parametrize("h1", ["Acme", "Careers", "Acme Careers"])
def test_site_name_or_careers_h1_is_not_a_title_anchor(h1):
    html = _head("Acme | Senior Data Engineer", site_name="Acme") + f"<body><h1>{h1}</h1></body></html>"
    job = base.parse_generic(_page(html, final_url="https://jobs.acme.test/openings/42"))
    assert job.title == "Acme | Senior Data Engineer"


def test_generic_page_title_left_after_stripping_yields_to_the_ats_title():
    html = "<html><head><title>Careers - Examplecorp Careers</title></head><body></body></html>"
    hints = {"title": "Senior Security Engineer (f/m/d)", "company": "Examplecorp"}
    job = parse_ats_page(_page(html, final_url="https://boards.greenhouse.io/examplecorp/jobs/2002"), ats_hints=hints)
    assert job.title == "Senior Security Engineer (f/m/d)"

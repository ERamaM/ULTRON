"""Region-scoped eligibility precision (self-improvement sessions 3-4, benchmark cases da-scope/nu-scope/*-list).

The country check in ``other_country_restriction`` only applies to a profile that names a home country in
``location_scope.preferred``. These tests cover what releases a posting placed outside that home.
"""

from __future__ import annotations

import pytest

from ultron.core.rules.geography import other_country_restriction

SPAIN_EU = {"location_scope": {"preferred": ["Spain", "Remote EU", "Remote Europe"]}}
IRELAND = {"location_scope": {"preferred": ["Ireland", "Dublin", "Cork"], "eligibility_terms": ["limerick"]}}
LATAM = {"location_scope": {"preferred": ["Remote LATAM", "Brazil", "Mexico"]}}


# --- body adjectives that describe the company, not where it hires -----------------------------------------


@pytest.mark.parametrize(
    "criteria, location, remote, body, country",
    [
        (LATAM, "Bangalore, India", "Remote, cross-functional environment",
         "Turn support data into insights for our global support organization.", "india"),
        (SPAIN_EU, "Czechia; Bulgaria; Poland", "Remote", "Build Go services for our global payments platform.",
         "bulgaria"),
        (LATAM, "Toronto, Canada", "Remote (Canada)", "Join our international analytics team.", "canada"),
    ],
)
def test_global_or_international_in_the_body_does_not_open_a_country_posting(criteria, location, remote, body,
                                                                               country):
    assert other_country_restriction(location, remote, criteria, body) == country


@pytest.mark.parametrize(
    "criteria, location, remote, body",
    [
        (SPAIN_EU, "Germany", "Remote", "Hiring across the EU."),
        (SPAIN_EU, "Berlin, Germany", "Fully remote", "Work from anywhere."),
        (LATAM, "Toronto, Canada", "Remote", "We are a global team hiring across Latin America."),
        (LATAM, "Toronto, Canada", "Remote", "We hire worldwide."),
    ],
)
def test_a_body_that_names_where_the_company_hires_still_opens_the_posting(criteria, location, remote, body):
    assert other_country_restriction(location, remote, criteria, body) is None


def test_global_in_the_location_field_still_opens_the_posting():
    assert other_country_restriction("Remote, Global", "Remote", SPAIN_EU, "") is None


# --- the body only opens a remote job ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "criteria, location, remote, body, country",
    [
        (LATAM, "Barcelona, Spain", "",
         "Work from home two days a week and from anywhere for up to three weeks a year. We operate worldwide.",
         "spain"),
        (IRELAND, "Valencia, Spain", "", "Work from our Valencia office. A leading provider of services worldwide.",
         "spain"),
        (SPAIN_EU, "Lyon, France", "On-site", "Our clinics across Europe welcome you.", "france"),
    ],
)
def test_the_body_does_not_open_a_job_that_is_not_remote(criteria, location, remote, body, country):
    assert other_country_restriction(location, remote, criteria, body, remote_job=False) == country


def test_the_body_still_opens_a_remote_job():
    body = "Candidates can work remotely from anywhere in Spain."
    assert other_country_restriction("Berlin, Germany", "Fully remote", SPAIN_EU, body, remote_job=True) is None

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


# --- country names in the location fields: "US" and Northern Ireland ----------------------------------------


@pytest.mark.parametrize(
    "criteria, location, remote, country",
    [
        (LATAM, "US", "Remote", "united states"),
        (LATAM, "Chicago, US Remote, SF, NYC, Seattle", "Remote", "united states"),
        (SPAIN_EU, "US-SF, US-Seattle or US-Remote", "", "united states"),
        (IRELAND, "Hillsborough, Northern Ireland, United Kingdom", "On-site", "united kingdom"),
        (IRELAND, "Belfast, Northern Ireland", "On-site", "united kingdom"),
    ],
)
def test_us_and_northern_ireland_name_a_country_outside_the_home(criteria, location, remote, country):
    assert other_country_restriction(location, remote, criteria, "", remote_job=False) == country


@pytest.mark.parametrize(
    "criteria, location, remote",
    [
        (IRELAND, "Dublin, Ireland", "On-site"),
        (IRELAND, "Derry, Northern Ireland or Donegal, Ireland", "On-site"),
        ({"location_scope": {"preferred": ["United Kingdom"]}}, "Belfast, Northern Ireland", "On-site"),
        ({"location_scope": {"preferred": ["Remote US", "United States"]}}, "US", "Remote"),
        (LATAM, "Remote", "Join us from Brazil"),
        (LATAM, "Remote", "Work with us from anywhere"),
    ],
)
def test_us_and_northern_ireland_do_not_flag_a_home_posting(criteria, location, remote):
    assert other_country_restriction(location, remote, criteria, "", remote_job=False) is None


@pytest.mark.parametrize(
    "criteria, location, remote",
    [
        ({"location_scope": {"preferred": ["Ireland", "Northern Ireland"]}}, "Belfast, Northern Ireland", "On-site"),
        ({"location_scope": {"preferred": ["Ireland", "Belfast"]}}, "Belfast, Northern Ireland", "On-site"),
        ({"location_scope": {"preferred": ["Ireland", "United Kingdom"]}}, "Belfast, Northern Ireland", "On-site"),
        ({"location_scope": {"preferred": ["US"]}}, "Chicago, US Remote", "Remote"),
        ({"location_scope": {"preferred": ["United States"]}}, "US-SF or US-Remote", ""),
        ({"location_scope": {"preferred": ["United States", "Texas"]}}, "Austin, Texas, US", "On-site"),
    ],
)
def test_a_profile_that_names_northern_ireland_or_the_us_keeps_those_postings(criteria, location, remote):
    # PR #65 review: rewriting "Northern Ireland" and "US" must not hide a home place the profile names.
    assert other_country_restriction(location, remote, criteria, "", remote_job=False) is None


def test_northern_ireland_in_the_body_of_a_remote_job_is_not_ireland():
    body = "Work remotely from anywhere in Northern Ireland."
    assert other_country_restriction("Belfast", "Remote - United Kingdom", IRELAND, body) == "united kingdom"


# --- US places with no country name: state codes, state names and US state licensure ------------------------

US_LICENSURE = "Current appropriate state licensure is required. Comply with federal and state regulations."


@pytest.mark.parametrize(
    "criteria, location, remote, body, remote_job",
    [
        (IRELAND, "Lewes, DE 19958", "Onsite", "", False),
        (IRELAND, "Austin, TX", "On-site", "", False),
        (IRELAND, "Dededo, GU", "Onsite Dededo, GU", "", False),
        (IRELAND, "Nebraska (Any City)", "Field-based", "", False),
        (IRELAND, "Memphis, Tennessee", "", "", False),
        (IRELAND, "Riverside Dialysis - Clinic", "Onsite", US_LICENSURE, False),
        (IRELAND, "Wyoming", "Remote", "Multiple state licenses are a strong advantage.", True),
        (LATAM, "Riverside Clinic", "Remote", "You must be licensed in the state where your patients live.", True),
        (LATAM, "Remote - Denver, CO 80202", "Remote", "", True),
    ],
)
def test_a_us_place_without_a_country_name_is_outside_a_non_us_home(criteria, location, remote, body, remote_job):
    assert other_country_restriction(location, remote, criteria, body, remote_job=remote_job) == "united states"


@pytest.mark.parametrize(
    "criteria, location, remote, body",
    [
        (IRELAND, "Virginia, Co. Cavan", "", "NMBI registration required."),
        (IRELAND, "MULLINGAR, CO. WESTMEATH", "On-site", "Provide patient care in a State-funded day hospital."),
        (IRELAND, "Roscommon", "", "Join our nurse-led residential service in Co Roscommon."),
        (IRELAND, "Tralee", "", "The State Claims Agency covers clinical indemnity."),
        (LATAM, "Buenos Aires, AR", "Remote", ""),
        (LATAM, "Bogota, CO", "Remote", ""),
        ({"location_scope": {"preferred": ["Remote LATAM", "Argentina"]}}, "Cordoba, AR 50000", "Remote", ""),
        ({"location_scope": {"preferred": ["Remote LATAM", "Brazil"]}}, "Curitiba, PR", "Remote", ""),
        ({"location_scope": {"preferred": ["Remote LATAM", "Colombia"]}}, "Bogota, DC", "Remote", ""),
        ({"location_scope": {"preferred": ["Italy"]}}, "Milano, MI", "On-site", ""),
        ({"location_scope": {"preferred": ["Australia"]}}, "Perth, WA", "On-site", ""),
        ({"location_scope": {"preferred": ["Germany"]}}, "Berlin, DE 10115", "On-site", ""),
        ({"location_scope": {"preferred": ["United Kingdom"]}}, "Washington, Tyne and Wear", "On-site", ""),
        ({"location_scope": {"preferred": ["United States"]}}, "Lewes, DE 19958", "Onsite", US_LICENSURE),
        ({"location_scope": {"preferred": ["Remote US", "Texas"]}}, "Austin, TX", "On-site", US_LICENSURE),
    ],
)
def test_us_place_evidence_does_not_flag_a_home_or_look_alike_place(criteria, location, remote, body):
    assert other_country_restriction(location, remote, criteria, body, remote_job=False) is None

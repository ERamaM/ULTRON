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
        (LATAM, "US", "Remote", "Put the global economy within everyone's reach.", "united states"),
        (LATAM, "Toronto, Canada", "Remote", "We are a global leader in payments with international clients.",
         "canada"),
        (LATAM, "Toronto, Canada", "Remote", "Our globally distributed team ships every day.", "canada"),
        (LATAM, "Toronto, Canada", "Remote", "Read our Global Candidate Privacy Notice.", "canada"),
        (LATAM, "Toronto, Canada", "Remote", "Make a positive impact on a global scale.", "canada"),
        (LATAM, "Bangalore, India", "Remote", "Support our global engineering organization.", "india"),
        (LATAM, "Toronto, Canada", "Remote", "We serve international customers and markets.", "canada"),
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
        # PR #67 review: "global" and "international" in a hiring-scope statement still open the posting.
        (LATAM, "Toronto, Canada", "Remote (Canada)", "This is a global remote role with no country restrictions."),
        (LATAM, "Toronto, Canada", "Remote", "This role is globally remote."),
        (LATAM, "Toronto, Canada", "Remote", "Remote - Global."),
        (LATAM, "Toronto, Canada", "Remote", "We are hiring globally across all time zones."),
        (LATAM, "Toronto, Canada", "Remote", "We hire internationally."),
        (LATAM, "Toronto, Canada", "Remote", "Work from anywhere in the world."),
        (LATAM, "Toronto, Canada", "Remote", "International candidates are welcome."),
        (LATAM, "Toronto, Canada", "Remote", "The role is open to candidates globally."),
        (LATAM, "Toronto, Canada", "Remote", "We are open to international applicants."),
        (LATAM, "Toronto, Canada", "Remote", "There are no location restrictions."),
        (LATAM, "Toronto, Canada", "Remote", "We hire globally, without location restrictions."),
        # PR #67 review round 3: only a closed list of company descriptions stops these words.
        (LATAM, "Toronto, Canada", "Remote", "This is a global remote role with no travel required."),
        (LATAM, "Toronto, Canada", "Remote", "International applicants may apply."),
        (LATAM, "Toronto, Canada", "Remote", "We accept international applications."),
        # Round 4: the company-description window stops at the end of the sentence.
        (LATAM, "Toronto, Canada", "Remote", "We hire worldwide. Our team works asynchronously."),
        (LATAM, "Toronto, Canada", "Remote", "Open worldwide; team rituals are async."),
        # Round 5: only a company noun the scope word directly modifies stops it.
        (LATAM, "Toronto, Canada", "Remote", "Work remotely worldwide with our team."),
        (LATAM, "Toronto, Canada", "Remote", "We hire worldwide and our team is async."),
        (LATAM, "Toronto, Canada", "Remote", "Join a global remote team."),
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
        (IRELAND, "Wyoming", "Remote", "An active state license is required. Telehealth from home.", True),
        (IRELAND, "Riverside Clinic", "Onsite", "Applicants must hold a valid state license.", False),
        (LATAM, "Riverside Clinic", "Remote", "You must be licensed to practice in the state of Arizona.", True),
        (IRELAND, "Riverside Clinic", "Onsite", "Must hold an active Texas RN license.", False),
        (IRELAND, "Riverside Clinic", "Onsite", "Candidates must be eligible for licensure in New York.", False),
        (IRELAND, "Riverside Clinic", "Onsite", "Current appropriate state licensure and CPR certification.", False),
        (LATAM, "Remote - Denver, CO 80202", "Remote", "", True),
        (IRELAND, "Ann Arbor, MI 48104", "On-site", "", False),
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
        # PR #67 review: a five-digit postal code is not a US ZIP code when the code is a region of a home country.
        ({"location_scope": {"preferred": ["Italy"]}}, "Milano, MI 20121", "On-site", ""),
        ({"location_scope": {"preferred": ["Italy"]}}, "Catania, CT 95121", "On-site", ""),
        ({"location_scope": {"preferred": ["Italy"]}}, "Varese, VA 21100", "On-site", ""),
        ({"location_scope": {"preferred": ["Italy"]}}, "Parma, PR 43121", "On-site", ""),
        ({"location_scope": {"preferred": ["Italy"]}}, "Como, CO 22100", "On-site", ""),
        ({"location_scope": {"preferred": ["Remote LATAM", "Brazil"]}}, "Curitiba, PR 80010", "Remote", ""),
        ({"location_scope": {"preferred": ["Remote LATAM", "Brazil"]}}, "Florianopolis, SC 88010-000", "Remote", ""),
        ({"location_scope": {"preferred": ["Remote LATAM", "Colombia"]}}, "Bogota, DC 11001", "Remote", ""),
        ({"location_scope": {"preferred": ["India"]}}, "Chennai, TN 60001", "On-site", ""),
        ({"location_scope": {"preferred": ["Australia"]}}, "Perth, WA 6000", "On-site", ""),
        # A state that licenses professions in a home federation: the licence is not US evidence.
        ({"location_scope": {"preferred": ["Australia"]}}, "Northside Clinic", "On-site", US_LICENSURE),
        ({"location_scope": {"preferred": ["India"]}}, "Northside Clinic", "On-site", US_LICENSURE),
        # State names that are also places elsewhere: a department of Uruguay, a province of Bulgaria.
        ({"location_scope": {"preferred": ["Remote LATAM", "Brazil"]}}, "Sarandi Grande, Florida", "Remote", ""),
        ({"location_scope": {"preferred": ["Bulgaria"]}}, "Lom, Montana", "On-site", ""),
        ({"location_scope": {"preferred": ["Germany"]}}, "Berlin, DE 10115", "On-site", ""),
        ({"location_scope": {"preferred": ["United Kingdom"]}}, "Washington, Tyne and Wear", "On-site", ""),
        ({"location_scope": {"preferred": ["United States"]}}, "Lewes, DE 19958", "Onsite", US_LICENSURE),
        ({"location_scope": {"preferred": ["Remote US", "Texas"]}}, "Austin, TX", "On-site", US_LICENSURE),
        # Round 5: eligibility terms that spell the US ("us", "usa") make it a home.
        ({"location_scope": {"preferred": ["Remote LATAM", "Brazil"], "eligibility_terms": ["us"]}},
         "Austin, TX", "On-site", US_LICENSURE),
        ({"location_scope": {"preferred": ["Remote LATAM", "Brazil"], "eligibility_terms": ["usa"]}},
         "Austin, TX", "On-site", ""),
    ],
)
def test_us_place_evidence_does_not_flag_a_home_or_look_alike_place(criteria, location, remote, body):
    assert other_country_restriction(location, remote, criteria, body, remote_job=False) is None


# --- PR #67 review rounds 2-3: only an explicit licence requirement on the applicant is US evidence ----------


@pytest.mark.parametrize(
    "body",
    [
        "Build software to manage state licenses for our customers.",
        "Build software to manage state licenses for our current customers.",
        "Maintain our state license database and build reporting dashboards.",
        "Applicants must hold a valid nursing license.",
        "You must have a license to drive.",
        "Must hold a current professional license.",
        "Required to obtain an active RN license within 90 days.",
        "You must not hold an active state license elsewhere.",
        "No state license is required for this non-clinical role.",
        "You do not need a state licensure for this position.",
        "Current state licensure is not required.",
        "State licenses are a plus.",
        "An active state license is optional.",
        "A current state license is preferred.",
        "Active state licensure is nice to have.",
        "Ideally you hold a current state license.",
        "A valid state license would be an advantage.",
    ],
)
def test_a_state_licence_without_an_affirmative_applicant_requirement_is_not_us_evidence(body):
    assert other_country_restriction("Riverside Office", "On-site", IRELAND, body, remote_job=False) is None


def test_body_patterns_run_in_linear_time_on_adversarial_text():
    # Sonar python:S8786: no super-linear backtracking on long inputs.
    import time

    bodies = [
        "global " * 15000 + "x",
        "must hold an active " * 6000 + "x",
        "not active state license " * 4000,
        "licensed to practice in the state of " * 3000 + "x",
        ("a" * 50 + " ") * 2000,
        "global" + " " * 30000 + "x",
        "must hold" + " " * 30000 + "state license",
        "anywhere" + " \t\r\f\v" * 6000 + "in the world",
        ". " + " \t" * 15000 + "We hire worldwide.",
    ]
    start = time.perf_counter()
    for body in bodies:
        other_country_restriction("Toronto, Canada", "Remote", LATAM, body)
        other_country_restriction("Riverside", "On-site", IRELAND, body, remote_job=False)
    assert time.perf_counter() - start < 5.0

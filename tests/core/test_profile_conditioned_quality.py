"""Profile-conditioned quality rules (owner request of 2026-11-20).

Each rule only applies when the profile asks for it: the role family comes from the profile's
``role_focus_terms``/``target_titles``, the student levels from ``seniority.excluded_terms``, the
country restriction from the countries named in ``location_scope.preferred``.
"""

from __future__ import annotations

import pytest

from ultron.core.decision.unit import characterize_job, load_allowed_domains
from ultron.core.models import ExtractedJob
from ultron.core.rules.geography import other_country_restriction, profile_home_places
from ultron.core.rules.role_matching import is_allowed_family, role_family_nouns


def _analyst_profile(**seniority) -> dict:
    return {
        "slug": "analyst",
        "criteria": {
            "role_focus_terms": ["data analyst", "bi analyst"],
            "target_titles": ["Data Analyst", "Senior Data Analyst"],
            "seniority": {"required": False, **seniority},
            "remote_policy": {"required": "full_remote"},
            "location_scope": {"preferred": ["Remote LATAM", "Brazil"], "eligibility_terms": ["latam", "brazil"]},
            "role_signal_terms": ["sql", "dashboards", "reporting"],
        },
    }


def _job(title: str, *, location: str = "Brazil", remote: str = "Remote - Brazil", body: str = "") -> ExtractedJob:
    return ExtractedJob(
        title=title, company="Synthetic Employer", domain="jobs.example.test",
        apply_url="https://jobs.example.test/case", location=location, remote_text=remote,
        body_text=(body or "Write SQL and build dashboards and reporting.") + " Applications are open.",
        apply_available=True, is_open_hint=True,
    )


def _outcome(job: ExtractedJob, profile: dict) -> str:
    result = characterize_job(job, profile, load_allowed_domains(), allow_outside_ats_allowlist=True)
    return result["pipeline_characterization"]["outcome"]

DATA_ANALYST_TERMS = ["data analyst", "bi analyst", "product analyst", "Senior Data Analyst"]
NURSE_TERMS = ["registered nurse", "staff nurse", "clinical nurse", "Clinical Nurse Specialist"]
ANALYST_SIGNALS = ["sql", "dashboards", "reporting", "analytics"]
NURSE_SIGNALS = ["nursing", "patient care", "ward", "clinical"]


# --- (b) a title with no family term does not pass on signal terms alone --------------------------


def test_a_level_word_shared_with_target_titles_is_not_a_family_term():
    body = "Build product features in Python and React. Write SQL, maintain dashboards and reporting."
    assert not is_allowed_family("Senior Fullstack Engineer", body, DATA_ANALYST_TERMS, ANALYST_SIGNALS)
    assert not is_allowed_family("Senior Product Designer", body, DATA_ANALYST_TERMS, ANALYST_SIGNALS)


def test_a_qualifier_shared_with_a_role_term_is_not_a_family_term():
    body = "Lead engineers building clinical software used on nursing wards for patient care."
    assert not is_allowed_family("Engineering Lead, Clinical Operations", body, NURSE_TERMS, NURSE_SIGNALS)


def test_a_bridge_title_outside_the_profile_family_needs_a_family_term():
    body = "Build pipelines that feed Looker dashboards; SQL and dbt; reporting reliability."
    assert not is_allowed_family("Senior Data Engineer", body, DATA_ANALYST_TERMS, ANALYST_SIGNALS)


def test_a_signal_term_in_a_bridge_title_does_not_pass_an_off_family_title():
    body = "Build the services behind SQL dashboards and reporting."
    title = "Senior Software Engineer, Analytics Platform"
    assert not is_allowed_family(title, body, DATA_ANALYST_TERMS, ANALYST_SIGNALS)


def test_the_family_noun_of_a_spanish_term_is_the_role_not_its_field():
    assert role_family_nouns(["ingeniero de software", "cientifico de datos"]) == {"ingeniero", "cientifico"}
    assert role_family_nouns(["Occupational Therapist - Graduate Program"]) == {"therapist", "program"}


def test_the_spanish_title_of_the_profile_role_matches_and_its_field_does_not():
    body = "Crear tableros en Looker y consultas SQL para el equipo de producto."
    assert is_allowed_family("Analista de Datos Senior", body, DATA_ANALYST_TERMS, ANALYST_SIGNALS)
    assert not is_allowed_family("Ingeniero de Datos Senior", body, DATA_ANALYST_TERMS, ANALYST_SIGNALS)


def test_titles_with_the_family_noun_still_pass_on_signal_terms():
    body = "Write SQL, build Looker dashboards and recurring reporting."
    assert is_allowed_family("Senior Analyst, Growth", body, DATA_ANALYST_TERMS, ANALYST_SIGNALS)
    assert is_allowed_family("Senior Data Analyst", body, DATA_ANALYST_TERMS, ANALYST_SIGNALS)
    assert is_allowed_family("Nurse, Surgical Ward", "Patient care on the ward.", NURSE_TERMS, NURSE_SIGNALS)


# --- (e) student and intern levels, when the profile excludes them ---------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Working Student Data Analyst (f/m/d)",
        "Werkstudent:in Data Analyst",
        "Werkstudentin Data Analyst",
        "Data Analyst Praktikum",
        "Data Analyst Trainee",
        "Stage Data Analyst",
        "Becario Data Analyst",
        "Data Analyst Apprentice",
    ],
)
def test_student_levels_are_discarded_when_the_profile_excludes_interns(title):
    assert _outcome(_job(title), _analyst_profile(excluded_terms=["intern"])) == "discard"


@pytest.mark.parametrize("title", ["Working Student Data Analyst", "Data Analyst Trainee"])
def test_student_levels_stay_when_the_profile_does_not_exclude_them(title):
    assert _outcome(_job(title), _analyst_profile()) == "keep"
    assert _outcome(_job(title), _analyst_profile(excluded_terms=["junior"])) == "keep"


@pytest.mark.parametrize("title", ["Senior Data Analyst, Student Success", "Data Analyst, Early Stage Ventures"])
def test_student_words_that_do_not_name_a_level_are_kept(title):
    assert _outcome(_job(title), _analyst_profile(excluded_terms=["intern"])) == "keep"

# --- (c) a posting limited to one country outside the profile's home places -----------------------

SPAIN_EU = {"location_scope": {"preferred": ["Spain", "Remote EU", "Remote Europe"]}}
IRELAND = {"location_scope": {"preferred": ["Ireland", "Dublin", "Cork"]}}
LATAM = {"location_scope": {"preferred": ["Remote LATAM", "Brazil", "Mexico"]}}
EU_ONLY = {"location_scope": {"preferred": ["Remote EU", "Remote Europe"]}}


@pytest.mark.parametrize(
    "criteria, location, remote, country",
    [
        (SPAIN_EU, "Netherlands", "Remote - Netherlands", "netherlands"),
        (SPAIN_EU, "Germany", "Remote across Germany", "germany"),
        (SPAIN_EU, "Warsaw, Poland", "Remote (Poland)", "poland"),
        (IRELAND, "Madrid, Spain", "On-site", "spain"),
        (LATAM, "Toronto, Canada", "Remote (Canada)", "canada"),
        (LATAM, "Spain", "Remote - Spain", "spain"),
    ],
)
def test_a_posting_limited_to_another_country_is_flagged(criteria, location, remote, country):
    assert other_country_restriction(location, remote, criteria) == country


@pytest.mark.parametrize(
    "criteria, location, remote",
    [
        (SPAIN_EU, "Madrid, Spain", "Remote within Spain"),
        (SPAIN_EU, "Germany, Spain or Portugal", "Remote in Germany, Spain or Portugal"),
        (SPAIN_EU, "Remote EU", "Remote EU (Germany preferred)"),
        (SPAIN_EU, "Worldwide", "Remote worldwide"),
        (IRELAND, "Dublin, Ireland", "On-site"),
        (LATAM, "Lima, Peru", "Fully remote within Latin America"),
        (SPAIN_EU, "Paris", "On-site"),
        (EU_ONLY, "Germany", "Remote across Germany"),
    ],
)
def test_home_places_regions_and_profiles_without_a_home_are_not_flagged(criteria, location, remote):
    assert other_country_restriction(location, remote, criteria) is None


def test_home_places_are_the_single_places_of_the_profile():
    assert profile_home_places(LATAM) == ("brazil", "mexico")
    assert profile_home_places(EU_ONLY) == ()
    assert "madrid" in profile_home_places(SPAIN_EU)

@pytest.mark.parametrize(
    "criteria, location, remote",
    [
        ({"location_scope": {"preferred": ["Brazil"]}}, "Sao Paulo, Brasil", "Remote - Brasil"),
        ({"location_scope": {"preferred": ["United Kingdom"]}}, "London", "Remote UK"),
        ({"location_scope": {"preferred": ["Remote US", "Texas"]}}, "Austin, TX, United States", "Remote"),
        ({"location_scope": {"preferred": ["Dublin"]}}, "Cork, Ireland", "On-site"),
        ({"location_scope": {"preferred": ["Spain"], "eligibility_terms": ["portugal"]}}, "Lisbon, Portugal",
         "Remote - Portugal"),
    ],
)
def test_other_spellings_of_the_home_country_and_city_only_profiles_are_not_flagged(criteria, location, remote):
    assert other_country_restriction(location, remote, criteria) is None
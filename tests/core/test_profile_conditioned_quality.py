"""Profile-conditioned quality rules (owner request of 2026-11-20).

Each rule only applies when the profile asks for it: the role family comes from the profile's
``role_focus_terms``/``target_titles``, the student levels from ``seniority.excluded_terms``, the
country restriction from the countries named in ``location_scope.preferred``.
"""

from __future__ import annotations

from ultron.core.rules.role_matching import is_allowed_family, role_family_nouns

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

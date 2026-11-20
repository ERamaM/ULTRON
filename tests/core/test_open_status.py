from __future__ import annotations

import pytest

from ultron.core.rules.hard_filters import is_open_status_ok


def test_missing_apply_button_alone_does_not_reject():
    body = "We are looking for a Senior ML Engineer to join our team."
    assert is_open_status_ok(body, apply_available=False, is_open_hint=False) is True


def test_explicit_closed_phrase_still_rejects():
    assert is_open_status_ok("This posting closed on May 1st.", True, True) is False


def test_contextual_closed_still_rejects():
    assert is_open_status_ok("This position is now closed.", True, True) is False


def test_expired_structured_deadline_rejects():
    body = "We are looking for a Senior ML Engineer to join our team."
    assert is_open_status_ok(body, True, True, expired=True) is False
    assert is_open_status_ok(body, True, True, expired=False) is True


def test_agency_boilerplate_does_not_close_posting():
    for body in (
        "Apply now. Please note we are not accepting applications from recruitment agencies.",
        "We are no longer accepting CVs from agencies.",
        "We are not accepting applications from third-party recruiters.",
        "Ya no aceptamos solicitudes de agencias de selección.",
        "This role has applications closed via recruitment agencies; please apply directly.",
        "This role's applications are closed via recruitment agencies; please apply directly.",
        "For this position, applications have now been closed through staffing agencies.",
        "We are no longer accepting applications or CVs from recruitment agencies. Please apply directly.",
        "We are no longer accepting applications from agencies or recruiters.",
        "We are not accepting applications from recruitment agencies, headhunters, or staffing firms!",
        "Note: not accepting applications from agencies\nApply below.",
        "We are not accepting applications from agencies",
    ):
        assert is_open_status_ok(body, True, True) is True, body


def test_closed_phrases_still_close_posting():
    for body in (
        "This vacancy is closed and no longer accepting applications.",
        "We are not accepting applications for this role.",
        "Applications closed.",
        "Position filled by recruitment agency.",
        "This role is closed.",
        "This position is now closed by recruitment agency.",
        "Applications closed by recruitment agency on May 1.",
        "This role's applications were closed by the recruitment agency.",
        "Position filled.",
        "We are no longer accepting applications from agencies or individual applicants.",
        "We are no longer accepting applications from recruitment agencies, recruiters, or individual applicants.",
        "Applications closed via agencies and direct candidates.",
    ):
        assert is_open_status_ok(body, True, True) is False, body


def test_agency_exemption_requires_the_agency_list_to_end_the_sentence():
    # Known conservative limitation: any continuation after the agency list keeps main's closed reading,
    # even an unrelated affirmative clause or a time qualifier.
    for body in (
        "We are not accepting applications from agencies and candidates must apply directly.",
        "We are no longer accepting CVs from agencies at this time.",
    ):
        assert is_open_status_ok(body, True, True) is False, body


def test_closed_phrase_inside_another_word_does_not_close_posting():
    assert is_open_status_ok("Create a composition filled with color.", True, True) is True


@pytest.mark.parametrize(
    "body",
    [
        "This position has been filled.",
        "The role has been filled",
        "This job has already been filled. Thank you for your interest.",
        "This job is no longer available",
        "Sorry, this posting is no longer active.",
        "The deadline for applications has passed",
        "The application deadline has passed.",
        "This requisition is no longer active",
        "La oferta está cerrada desde junio",
        "La oferta esta cerrada desde junio",
        "La plaza ya ha sido cubierta",
        "El puesto ha sido cubierto.",
        "No se admiten más solicitudes",
        "No se admiten mas candidaturas.",
        "Esta oferta ya no está disponible",
        "Diese Stelle ist bereits besetzt",
        "Die Stelle wurde besetzt.",
        "Diese Stellenanzeige ist nicht mehr verfügbar.",
        "Cette offre n'est plus disponible",
        "Cette offre n’est plus disponible.",
        "Le poste est pourvu.",
        "Ce poste a été pourvu.",
    ],
)
def test_explicit_closed_phrases_reject_in_every_language(body):
    assert is_open_status_ok(body, True, True) is False


@pytest.mark.parametrize(
    "body",
    [
        "Once the application form has been filled, we reply within a week.",
        "The questionnaire is no longer available offline; apply online below.",
        "Die Stelle soll schnellstmöglich besetzt werden.",
        "Wir möchten die Position zum 1. Mai besetzen.",
        "Le poste est à pourvoir dès maintenant.",
        "La oferta incluye seguro médico y está abierta a candidaturas de toda la UE.",
        "Our legacy API is no longer available; you will help build its replacement.",
        "No se admiten más solicitudes de agencias de selección.",
        "Applications are accepted until the position has been filled.",
        "We review applications on a rolling basis; once the role has been filled, this page closes.",
        "La candidatura sigue abierta hasta que la plaza ha sido cubierta.",
        "Bewerbungen sind willkommen, bis die Stelle besetzt ist.",
        "Le poste est pourvu d'un véhicule de fonction.",
    ],
)
def test_open_postings_with_closed_lookalike_words_stay_open(body):
    assert is_open_status_ok(body, True, True) is True


def test_extractor_open_hint_uses_the_shared_closed_phrase_list():
    from ultron.core.extractors.ats_extractors import base
    from ultron.core.models import JobRawPage

    def hint(text: str) -> bool:
        html = f"<html><body><h1>Senior ML Engineer</h1><p>{text}</p></body></html>"
        url = "https://boards.greenhouse.io/acme/jobs/1"
        return base.parse_generic(JobRawPage(url=url, final_url=url, status_code=200, html=html)).is_open_hint

    assert hint("La plaza ya ha sido cubierta.") is False
    assert hint("Diese Stelle ist bereits besetzt.") is False
    assert hint("We are not accepting applications from recruitment agencies.") is True
    assert hint("Apply now.") is True

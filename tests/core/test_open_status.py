from __future__ import annotations

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

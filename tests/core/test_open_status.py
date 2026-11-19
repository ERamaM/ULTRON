from __future__ import annotations

from ultron.core.rules.hard_filters import is_open_status_ok


def test_missing_apply_button_alone_does_not_reject():
    body = "We are looking for a Senior ML Engineer to join our team."
    assert is_open_status_ok(body, apply_available=False, is_open_hint=False) is True


def test_explicit_closed_phrase_still_rejects():
    assert is_open_status_ok("This posting closed on May 1st.", True, True) is False


def test_contextual_closed_still_rejects():
    assert is_open_status_ok("This position is now closed.", True, True) is False


def test_agency_boilerplate_does_not_close_posting():
    for body in (
        "Apply now. Please note we are not accepting applications from recruitment agencies.",
        "We are no longer accepting CVs from agencies at this time.",
        "We are not accepting applications from third-party recruiters.",
        "Ya no aceptamos solicitudes de agencias de selección.",
        "This role has applications closed via recruitment agencies; please apply directly.",
        "This role's applications are closed via recruitment agencies; please apply directly.",
        "For this position, applications have now been closed through staffing agencies.",
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
    ):
        assert is_open_status_ok(body, True, True) is False, body


def test_closed_phrase_inside_another_word_does_not_close_posting():
    assert is_open_status_ok("Create a composition filled with color.", True, True) is True

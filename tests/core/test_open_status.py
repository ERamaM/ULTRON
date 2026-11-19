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

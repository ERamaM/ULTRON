from __future__ import annotations

from ultron.core.models import ExtractedJob
from ultron.core.rules.hard_filters import (
    apply_hard_filters,
    is_full_remote_only,
    is_open_status_ok,
    pre_filter_candidate,
)
from ultron.core.vocabulary import load_packs


def _job(**kw):
    base = {
        "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "domain": "boards.greenhouse.io",
        "title": "Senior ML Engineer",
        "company": "Acme",
        "location": "",
        "remote_text": "",
        "body_text": "",
        "apply_available": True,
        "is_open_hint": True,
    }
    base.update(kw)
    return ExtractedJob(**base)


def test_spanish_teletrabajo_counts_as_remote_hint():
    assert is_full_remote_only("100% teletrabajo") is True


def test_spanish_hybrid_is_rejected():
    assert is_full_remote_only("teletrabajo con modalidad híbrida") is False


def test_spanish_closed_phrases_reject():
    assert is_open_status_ok("La plaza cubierta. Gracias por su interés.", True, True) is False
    assert is_open_status_ok("Esta oferta cerrada desde mayo.", True, True) is False


def test_spanish_hybrid_body_discards_in_hard_filters():
    job = _job(remote_text="modalidad híbrida", body_text="3 dias de oficina en Madrid")
    ok, reason = apply_hard_filters(job, ["greenhouse.io"])
    assert ok is False
    assert reason == "hybrid_or_not_explicit_remote"


def test_empty_target_regions_do_not_imply_european_scope():
    job = _job(remote_text="Remote US only", body_text="US only")
    ok, reason = apply_hard_filters(job, ["greenhouse.io"], target_regions=[])
    assert ok is True
    assert reason is None


def test_security_clearance_without_us_context_is_not_a_region_flag():
    from ultron.core.rules.geo_eu import has_region_red_flags

    assert has_region_red_flags("Remote role. Security clearance required.") is False


def test_classification_signals_propagate_role_ambiguous(monkeypatch):
    from ultron.core.rules import classification_signals

    monkeypatch.setattr(classification_signals, "score_role_family", lambda *args, **kwargs: (0, []))
    job = _job(title="LLM role", body_text="Build an LLM platform.")
    signals = classification_signals.build_classification_signals(job, vocabulary=load_packs(["ai-ml"]))
    assert signals.role_ambiguous is True


def test_pre_filter_uses_same_role_ambiguity_with_or_without_signals():
    from ultron.core.rules.classification_signals import build_classification_signals

    job = _job(title="LLM role", remote_text="Remote EU", body_text="Build an LLM platform.")
    ai = load_packs(["ai-ml"])
    signals = build_classification_signals(job, vocabulary=ai)
    assert signals.role_ambiguous is True

    without_signals = pre_filter_candidate(job, ["greenhouse.io"], vocabulary=ai)
    with_signals = pre_filter_candidate(job, ["greenhouse.io"], classification_signals=signals, vocabulary=ai)
    assert with_signals == without_signals

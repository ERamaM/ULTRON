from __future__ import annotations

import pytest

from ultron.core.models import ClassificationSignals, ExtractedJob
from ultron.core.rules import geo_eu, geography
from ultron.core.rules import hard_filters as h
from ultron.core.rules import role_matching as roles
from ultron.core.rules.term_matching import extract_matching_evidence, normalize_text
from ultron.core.vocabulary import load_packs

AI = load_packs(["ai-ml"])


def _job(**updates):
    values = {
        "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "domain": "boards.greenhouse.io",
        "title": "Senior ML Engineer",
        "company": "Acme",
        "location": "Remote EU",
        "remote_text": "Remote EU",
        "body_text": "We hire remotely in EU.",
        "apply_available": True,
        "is_open_hint": True,
    }
    values.update(updates)
    return ExtractedJob(**values)


def test_eu_geography_checks_non_eu_structured_location_for_any_work_mode():
    for location in ("USA - MA - Amesbury", "Jinshan District, Shanghai, China", "Dhaka, Dhaka Division, bd"):
        job = _job(location=location, remote_text="", body_text="Optimize manufacturing processes.")
        assert h.apply_hard_filters(
            job, ["boards.greenhouse.io"], remote_required=False, target_regions=("Spain", "Europe"),
        ) == (False, "non_eu_remote")
        assert h.apply_hard_filters(
            job, ["boards.greenhouse.io"], remote_required=False, target_regions=("United Kingdom",), geo="other",
        ) == (True, None)


def test_filled_corporate_posting_overrides_stale_apply_hint():
    body = "We're sorry… the job you are trying to apply for has been filled. Explore other opportunities."
    assert not h.is_open_status_ok(body, apply_available=True, is_open_hint=True)


def test_geo_normalization_and_signal_helpers_cover_language_variants():
    assert normalize_text("HÍBRIDA") == "hibrida"
    assert geo_eu._has_positive_spain_signal("Remote from Spain")
    assert geo_eu._has_us_only_signal("US only")
    assert geo_eu._has_security_clearance_requirement("DoD security clearance")
    assert geo_eu._has_est_timezone_signal("EST hours")
    assert geo_eu._has_disallowed_region_terms("US only")
    assert list(geo_eu._iter_term_windows("remote EU role", "EU"))
    assert geo_eu._has_pattern_match("location: united states", geo_eu.US_REMOTE_LOCATION_PATTERNS)
    assert "eligibility" in geography.build_geo_body_excerpt("eligibility: remote EU")


def test_location_context_classifiers_and_conflicts():
    assert geo_eu._has_eu_country_in_location_context("remote from Germany")
    assert geo_eu._has_non_eu_location_in_context("remote from Canada")
    assert geo_eu._has_us_state_in_location_context("location: California")
    assert geo_eu._has_eu_eligibility_signal("remote EU")
    assert geo_eu._has_us_location_signal("United States")
    assert h._has_contextual_hybrid_conflict("hybrid 3 days onsite")
    assert h._has_contextual_onsite_conflict("on-site in office")
    assert h._has_closed_status_with_context("position is now closed")


def test_role_and_remote_classifiers_cover_allowed_and_negative_shapes():
    assert roles._role_focus_words(["backend engineer"]) == {"backend", "engineer"}
    assert roles.is_senior_title("Principal Machine Learning Engineer")
    assert not roles.is_senior_title("Junior Engineer")
    assert roles.is_senior_title("Senior Backend Engineer", ["backend engineer"])
    assert h.is_full_remote_only("remote-first")
    assert not h.is_full_remote_only("hybrid 2 days onsite")
    assert h.is_open_status_ok("open position", True, False)
    assert geo_eu.has_region_red_flags("US only")
    assert h.classify_remote_policy("Remote EU") == "remote_eu"
    assert h.classify_remote_policy("Remote from Spain") == "remote_spain"
    assert h.classify_remote_policy("No location stated") is None
    assert h.classify_eligibility("Remote EU") == "high"
    assert h.classify_eligibility("Remote from Spain") == "confirmed"
    assert h.classify_eligibility("No location stated") is None


def test_family_score_and_prefilter_handle_custom_profile_and_signal_overrides():
    score, signals = roles.score_role_family("Backend Engineer", "Build APIs", ["backend engineer"])
    assert score > 0 and signals
    assert roles.is_allowed_family("Senior Backend Engineer", "backend platform", ["backend engineer"])
    assert not roles.is_allowed_family("Finance Manager", "budgets", ["backend engineer"])
    assert (
        extract_matching_evidence("First sentence. Remote EU hiring here.", ["remote", "eu"])
        == "Remote EU hiring here."
    )
    assert roles.role_ambiguity_detected(0, "LLM role", vocabulary=AI) is True


def test_apply_hard_filters_and_prefilter_cover_soft_recall_paths():
    job = _job(remote_text="Remote US only", body_text="US only")
    soft: list[str] = []
    assert h.apply_hard_filters(
        job, ["greenhouse.io"], recall_mode_active=True, target_regions=["EU"], soft_reasons=soft
    )[0]
    assert "non_eu_remote" in soft or soft == []

    closed = _job(body_text="This job is closed and no longer accepting applications")
    assert h.apply_hard_filters(closed, ["greenhouse.io"])[1] == "expired_or_closed"
    outside = _job(domain="example.test")
    assert h.apply_hard_filters(outside, ["greenhouse.io"])[1] == "outside_ats_allowlist"

    pre_soft: list[str] = []
    accepted, reason = h.pre_filter_candidate(
        job,
        ["greenhouse.io"],
        ["machine learning"],
        classification_signals=ClassificationSignals(role_score=0, role_signals=[], role_ambiguous=True),
        target_regions=["EU"],
        recall_mode_active=True,
        soft_reasons=pre_soft,
    )
    assert accepted is True
    assert reason is None


def test_context_guards_distinguish_negations_idioms_and_false_timezone_matches():
    assert geo_eu._has_positive_spain_signal("cannot hire in Spain") is False
    assert geo_eu._has_positive_spain_signal("Remote from España") is True
    assert geo_eu._has_us_only_signal("join us only for a meeting") is False
    assert geo_eu._has_us_only_signal("USA-only applicants") is True
    assert geo_eu._has_security_clearance_requirement("No security clearance required") is False
    assert geo_eu._has_security_clearance_requirement("Security clearance not required") is False
    assert geo_eu._has_security_clearance_requirement("US DoD security clearance") is True
    assert geo_eu._has_est_timezone_signal("Est. 2012") is False
    assert geo_eu._has_est_timezone_signal("5pm EST time") is True
    assert geo_eu._has_disallowed_region_terms("security clearance required") is False
    assert geo_eu._has_disallowed_region_terms("federal security clearance") is True


def test_geo_excerpt_context_and_role_focus_guards():
    assert geography.build_geo_body_excerpt("") == ""
    body = "\n".join(["Remote EU eligibility" for _ in range(5)] + ["Unrelated text"])
    excerpt = geography.build_geo_body_excerpt(body, max_chars=250)
    assert excerpt == "Remote EU eligibility"
    assert geo_eu._has_eu_country_in_location_context("location: Germany") is True
    assert geo_eu._has_eu_country_in_location_context("Germany engineering team") is False
    assert geo_eu._has_non_eu_location_in_context("customers in Canada") is False
    assert geo_eu._has_us_state_in_location_context("Georgia, Europe") is False
    assert geo_eu._window_has_us_disqualifier("California, US") is True
    assert geo_eu._has_eu_eligibility_signal("EMEA, CET, remote") is True
    assert geo_eu._has_us_location_signal("remote from New York") is True
    assert h._has_contextual_hybrid_conflict("hybrid cloud architecture") is False
    assert h._has_contextual_hybrid_conflict("hybrid role with 2 office days") is True
    assert h._has_contextual_onsite_conflict("office attendance not required") is False
    assert h._has_closed_status_with_context("closed beta platform") is False

    assert roles.is_senior_title("Lead Marketing Manager", ["marketing manager"]) is True
    assert roles.is_senior_title("Distinguished Engineer") is True
    assert roles.is_senior_title("Engineer") is False
    assert roles._normalized_role_terms([], AI) == set(AI.role_terms)
    assert roles._normalized_role_terms([" "], AI) == set(AI.role_terms)
    assert roles._normalized_role_terms([]) == set()
    assert h._is_allowed_domain("jobs.example.com", ["example.com"]) is True
    assert h._is_allowed_domain("", ["example.com"]) is False


def test_role_score_covers_signal_families_and_custom_profile_overrides():
    _score, signals = roles.score_role_family(
        "Junior Data Scientist Manager",
        "Tableau dashboard, machine learning, LLM and APIs",
        strong_signal_terms=["machine learning"],
        data_scientist_signal_terms=["machine learning"],
        vocabulary=AI,
    )
    assert "junior_title" in signals
    assert "title:data_scientist" in signals
    assert "ds_ml_body" in signals

    score_analytics, signals_analytics = roles.score_role_family("Data Scientist", "SQL reporting and Tableau", vocabulary=AI)
    assert score_analytics < 0
    assert "ds_analytics_only" in signals_analytics

    score_bridge, signals_bridge = roles.score_role_family(
        "Software Engineer", "RAG and retrieval", strong_signal_terms=["rag"], vocabulary=AI
    )
    assert score_bridge > 0
    assert "bridge_title+ai" in signals_bridge
    assert "title_ambiguous_body_positive" in signals_bridge

    custom_score, custom_signals = roles.score_role_family(
        "Marketing Manager", "marketing campaigns", allowed_terms=["marketing manager"]
    )
    assert custom_score > 0
    assert any(signal.startswith("title:") for signal in custom_signals)


def test_remote_status_policy_and_evidence_fallbacks():
    assert h.is_full_remote_only("Remote", "hybrid cloud only") is True
    assert h.is_full_remote_only("Hybrid", "remote") is False
    assert h.is_full_remote_only("Remote, office optional", "") is True
    assert h.is_full_remote_only("", "") is False
    assert h.is_open_status_ok("", False, False) is True
    assert h.is_open_status_ok("Position filled", True, True) is False
    assert h.is_open_status_ok("closed beta access", True, True) is True

    assert geo_eu.has_region_red_flags("Remote Canada") is True
    assert geo_eu.has_region_red_flags("Remote EU") is False
    assert h.classify_remote_policy("Remote Europe") == "remote_europe"
    assert h.classify_remote_policy("Remote EMEA") == "remote_emea"
    assert h.classify_remote_policy("Remote CET hours") == "remote_europe"
    assert h.classify_eligibility("Remote EMEA, CET hours") == "medium"
    assert h.classify_eligibility("EMEA only") is None
    assert extract_matching_evidence("No matching sentence", ["remote", "eu"]) == ""
    assert extract_matching_evidence("Remote here. EU hiring.", ["eu"]) == "EU hiring."


def test_hard_filters_and_prefilter_cover_hard_and_soft_geography_remote_paths():
    us_job = _job(location="US", remote_text="Remote US only", body_text="US only")
    assert h.apply_hard_filters(us_job, ["greenhouse.io"], target_regions=["EU"])[1] == "non_eu_remote"

    remote_conflict = _job(remote_text="Hybrid", body_text="")
    assert h.apply_hard_filters(remote_conflict, ["greenhouse.io"])[1] == "hybrid_or_not_explicit_remote"

    soft: list[str] = []
    accepted, reason = h.apply_hard_filters(
        remote_conflict,
        ["greenhouse.io"],
        recall_mode_active=True,
        soft_reasons=soft,
    )
    assert accepted is True and reason is None
    assert "hybrid_or_not_explicit_remote" in soft

    no_remote = _job(remote_text="", body_text="")
    assert h.apply_hard_filters(no_remote, ["greenhouse.io"])[0] is True
    assert h.apply_hard_filters(no_remote, ["greenhouse.io"], allow_outside_ats_allowlist=True)[0] is True

    rejected = _job(title="Finance Manager", body_text="budgeting", remote_text="Remote EU")
    assert h.pre_filter_candidate(rejected, ["greenhouse.io"], ["machine learning"])[1] == "non_matching_role_family"

    signals = ClassificationSignals(role_score=5, role_ambiguous=False, remote_conflict=True)
    assert (
        h.pre_filter_candidate(_job(remote_text="Hybrid"), ["greenhouse.io"], classification_signals=signals)[1]
        == "hybrid_or_not_explicit_remote"
    )

    soft_pre: list[str] = []
    assert h.pre_filter_candidate(
        _job(remote_text="Hybrid"),
        ["greenhouse.io"],
        classification_signals=signals,
        recall_mode_active=True,
        soft_reasons=soft_pre,
    )[0]
    assert "hybrid_or_not_explicit_remote" in soft_pre

    geo_signals = ClassificationSignals(role_score=5, role_ambiguous=False, eligibility_hint=None)
    geo_soft: list[str] = []
    assert h.pre_filter_candidate(
        us_job,
        ["greenhouse.io"],
        classification_signals=geo_signals,
        target_regions=["EU"],
        recall_mode_active=True,
        soft_reasons=geo_soft,
        remote_required=False,
    )[0]
    assert "non_eu_remote" in geo_soft


def test_geo_and_context_helpers_cover_negative_and_boundary_branches():
    assert geo_eu._has_disallowed_region_terms("Remote US only") is True
    assert geo_eu._has_disallowed_region_terms("security clearance required") is False
    assert geo_eu._has_disallowed_region_terms("federal security clearance") is True

    assert geography.build_geo_body_excerpt("\n\nRemote EU " + "eligibility " * 40, max_chars=250)
    assert geography.build_geo_body_excerpt("Remote EU eligibility\nRemote Spain hiring", max_chars=250) == (
        "Remote EU eligibility\nRemote Spain hiring"
    )
    assert geo_eu._window_has_us_disqualifier("California office") is True
    assert geo_eu._window_has_us_disqualifier("New York office") is True
    assert geo_eu._window_has_us_disqualifier("United States") is True

    assert geo_eu._has_term_in_location_context("EU remote team", {"eu"}) is True
    assert geo_eu._has_term_in_location_context("Germany team", {"Germany"}, require_location_context=False) is True
    assert (
        geo_eu._has_term_in_location_context(
            "remote Germany, but California only", {"Germany"}, reject_if_us_disqualifier=True
        )
        is False
    )
    assert geo_eu._has_us_state_in_location_context("location: Georgia, Europe") is False
    assert geo_eu._has_eu_eligibility_signal("remote Spain") is True
    assert geo_eu._has_eu_eligibility_signal("remote EU") is True
    assert geo_eu._has_eu_eligibility_signal("location: Germany") is True
    assert geo_eu._has_eu_eligibility_signal("EMEA, CET, remote") is True
    assert geo_eu._has_us_location_signal("United States") is True
    assert geo_eu._has_us_location_signal("San Francisco") is True
    assert h._has_contextual_hybrid_conflict("hybrid optional") is False
    assert h._has_contextual_onsite_conflict("onsite optional") is False


def test_role_scoring_and_classifiers_cover_explicit_negative_paths():
    assert roles.is_allowed_family("Head of Marketing", "campaigns", ["machine learning"]) is False
    assert roles.is_allowed_family("Senior Tech Lead", "platform", ["machine learning"]) is False
    score, signals = roles.score_role_family("Senior Tech Lead", "finance operations")
    assert score < 0
    assert "lead_manager_title" in signals or "negative_body" in signals
    assert h.classify_remote_policy("Remote from Germany") == "remote_eu"
    assert h.classify_eligibility("Remote from Germany") == "high"
    assert extract_matching_evidence("Unrelated text. Europe hiring now.", ["remote", "europe"]) == "Europe hiring now."


def test_hard_filter_domain_and_strong_remote_override_are_real_gates():
    outside = _job(domain="not-allowed.example")
    assert h.apply_hard_filters(outside, ["greenhouse.io"]) == (False, "outside_ats_allowlist")
    accepted, reason = h.apply_hard_filters(_job(remote_text="Hybrid but remote-first"), ["greenhouse.io"])
    assert accepted is True and reason is None
    assert h.pre_filter_candidate(
        outside, ["greenhouse.io"], classification_signals=ClassificationSignals(role_score=5)
    ) == (False, "outside_ats_allowlist")


def test_prefilter_strong_remote_positive_overrides_hybrid_conflict():
    accepted, reason = h.pre_filter_candidate(
        _job(remote_text="Hybrid but remote-first"),
        ["greenhouse.io"],
        classification_signals=ClassificationSignals(role_score=5, remote_conflict=None),
    )

    assert accepted is True
    assert reason is None


def test_eu_country_terms_ignore_demonyms():
    assert geo_eu.has_region_red_flags("UK only. This role requires Romanian and English.") is True
    assert geo_eu._has_eu_country_in_location_context("location: romanian remote") is False
    assert geo_eu._has_eu_country_in_location_context("Location: Romania. Remote.") is True
    assert geo_eu.has_region_red_flags("UK only. Location: Germany remote") is False
    for demonym in (
        "romanian",
        "estonian",
        "croatian",
        "bulgarian",
        "latvian",
        "lithuanian",
        "slovenian",
        "spanish",
    ):
        assert geo_eu._has_eu_country_in_location_context(f"location: {demonym} remote") is False


def test_negated_or_bare_distributed_is_not_full_remote():
    assert h.is_full_remote_only(", This role is not remote.") is False
    assert h.is_full_remote_only("This role is no remote.") is False
    assert h.is_full_remote_only("This role is non-remote.") is False
    assert h.is_full_remote_only("This role is not-remote.") is False
    assert h.is_full_remote_only("This role isn't remote.") is False
    assert h.is_full_remote_only(", We build distributed systems.") is False
    assert h.is_full_remote_only(", This role is on-site in Berlin office.") is False
    assert h.is_full_remote_only("Fully remote position") is True
    assert h.is_full_remote_only("Remote") is True
    assert h.is_full_remote_only("distributed team") is True
    assert h.is_full_remote_only("distributed teams") is True


def test_closed_technical_suffix_and_direct_negation_are_not_status():
    assert h._has_closed_status_with_context("This job involves closed-loop control.") is False
    assert h._has_closed_status_with_context("Applications are not closed.") is False
    assert h._has_closed_status_with_context("This position isn't closed.") is False
    assert h._has_closed_status_with_context("Applications are never closed.") is False
    assert h._has_closed_status_with_context("This position is closed.") is True
    assert h.is_open_status_ok("We are no longer accepting applications.", True, True) is False


def test_security_clearance_pronoun_us_is_not_regional():
    text = "Security clearance required. Work with us. Location: Berlin"
    assert geo_eu._has_disallowed_region_terms(text) is False
    assert geo_eu.has_region_red_flags(text) is False
    # Control: an explicit US-citizenship requirement with clearance must still flag.
    assert geo_eu._has_disallowed_region_terms("Security clearance required. US citizens only.") is True


def test_distributed_work_arrangement_phrases_still_count_as_remote_hints():
    # Work-arrangement wording is a genuine remote signal; "distributed systems" is not.
    assert h.is_full_remote_only("Flexible distributed work", "") is True
    assert h.is_full_remote_only("", "Fully distributed company, async by default.") is True
    assert h.is_full_remote_only("", "We build distributed systems.") is False


def test_georgia_country_is_not_a_us_state_without_us_cue():
    assert geo_eu._has_us_state_in_location_context("Location: Georgia. Remote.") is False
    assert geo_eu._has_us_state_in_location_context("Location: Tbilisi, Georgia. Remote.") is False
    assert geo_eu._has_us_state_in_location_context("Location: Atlanta, Georgia. Remote.") is True


def test_custom_non_pack_profile_matches_role_via_body_signals_and_title_vocab():
    from ultron.core.discovery.title_vocabulary import build_title_vocabulary
    from ultron.core.vocabulary import EMPTY_PACK

    score, _ = roles.score_role_family(
        "Senior Development Engineer",
        "Lead industrialization, polymers, composites, and materials science scale-up trials.",
        allowed_terms=["product development engineer", "process engineer"],
        strong_signal_terms=["materials science", "polymers", "industrialization", "scale-up"],
        vocabulary=EMPTY_PACK,
    )
    assert score >= 2

    bad_score, _ = roles.score_role_family(
        "Sales Development Engineer",
        "Materials science polymers",
        allowed_terms=["product development engineer"],
        strong_signal_terms=["materials science", "polymers"],
        vocabulary=EMPTY_PACK,
    )
    assert bad_score < 2

    tv = build_title_vocabulary(
        {
            "role_focus_terms": ["product development engineer"],
            "target_titles": ["Lead R&D Process Engineer"],
            "seniority": {"required": False, "excluded_terms": ["intern"]},
        },
        vocabulary=EMPTY_PACK,
    )
    assert "lead" not in tv.excluded_terms
    assert "principal" not in tv.excluded_terms
    assert "intern" in tv.excluded_terms


def test_geo_eu_home_office_incidental_offices_and_india_red_flags():
    savvymoney = (
        "SavvyMoney is a US based financial technology company with partners across digital banking platforms. "
        "Work 100% remotely from your home office in Warsaw, Poland."
    )
    assert geo_eu._has_eu_country_in_location_context(savvymoney) is True
    assert geo_eu.has_region_red_flags(savvymoney) is False

    cohere = (
        "Location: Paris\nRemote\n"
        "We are headquartered in Toronto with key offices in London, New York City, San Francisco, Paris and Berlin."
    )
    assert geo_eu.has_region_red_flags(cohere) is False

    assert geo_eu.has_region_red_flags("Location: Bangalore, India. Remote.") is True
    assert geo_eu.has_region_red_flags("Location: Islamabad, Pakistan. Remote.") is True
    assert geo_eu.has_region_red_flags("Location: Lafayette, IN") is True
    assert geo_eu.has_region_red_flags("Location: Mills River, NC") is True
    assert geo_eu.has_region_red_flags("Work remotely in France or Poland, in Europe") is False


def test_onsite_eap_wellness_benefit_does_not_reject_remote_role():
    body = (
        "Join us fully remote or in one of our hubs. We're a remote-first company. "
        "Access comprehensive, evidence-based mental health care with 24/7 counseling, "
        "therapy placement, on-site support, and legal and financial guidance."
    )
    assert h.is_full_remote_only("Germany (Remote)", body) is True
    assert h.is_full_remote_only("Remote", "Must work on-site support desk 3 days a week.") is False


def test_us_dotted_remote_and_brazil_trigger_red_flags():
    assert geo_eu.has_region_red_flags("Location: U.S. (Remote)") is True
    assert geo_eu.has_region_red_flags("Location: São Paulo, São Paulo, Brazil") is True
    assert geo_eu.has_region_red_flags("Location: U.S. (Remote) or Remote Europe") is False


def test_expired_valid_through_discards_as_expired_or_closed():
    expired = _job(valid_through_expired=True)
    assert h.apply_hard_filters(expired, ["greenhouse.io"]) == (False, "expired_or_closed")
    assert h.pre_filter_candidate(expired, ["greenhouse.io"]) == (False, "expired_or_closed")
    assert h.apply_hard_filters(_job(), ["greenhouse.io"]) == (True, None)


def test_parsed_past_valid_through_survives_stage_round_trip_and_is_discarded():
    from ultron.core.extractors.ats_extractors.base import parse_generic
    from ultron.core.models import JobRawPage

    url = "https://boards.greenhouse.io/acme/jobs/1"
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type": "JobPosting", "title": "Senior ML Engineer", "validThrough": "2020-02-01T00:00:00Z"}'
        "</script></head><body>Remote EU. We hire remotely in EU.</body></html>"
    )
    parsed = parse_generic(JobRawPage(url=url, final_url=url, status_code=200, html=html))
    # Stages persist jobs with model_dump(mode="json") and rebuild them with ExtractedJob(**row).
    job = ExtractedJob(**parsed.model_dump(mode="json"))
    assert h.apply_hard_filters(job, ["greenhouse.io"]) == (False, "expired_or_closed")


def test_in_office_matches_whole_phrase_only():
    from types import SimpleNamespace

    from ultron.core.discovery.prefilter import is_clearly_ineligible_at_discovery

    for body in (
        "Our team is based in our Berlin office.",
        "Our main office in Lisbon hosts quarterly meetups.",
        "Experience in Office 365 administration.",
        "Support requests are answered within office hours.",
    ):
        assert h.is_full_remote_only("Remote", body) is True, body
        assert h.apply_hard_filters(_job(remote_text="", body_text=body), ["boards.greenhouse.io"]) == (True, None)
    for body in (
        "You will work 3 days in office.",
        "This is an on-site role.",
        "Work arrangement: on-site\nSuite 200, Main Street, Berlin.",
        "Work arrangement: on-site suite access.",
        "Work arrangement: in office\nSuite 200, Main Street, Berlin.",
        "This is an _on-site_ role.",
    ):
        assert h.is_full_remote_only("Remote", body) is False, body
    assert h.is_full_remote_only("Remote", "Experience with the in-office suite of tools.") is True

    def ineligible(location):
        posting = SimpleNamespace(title="Engineer", location=location, remote_hint="")
        return is_clearly_ineligible_at_discovery(posting, geo="any")

    assert ineligible("Berlin Office") is False
    assert ineligible("Lisbon Office") is False
    assert ineligible("Berlin, in office") is True


@pytest.mark.parametrize(
    "body",
    [
        "Build RAG pipelines. No geographic restriction or office attendance. Applications remain open.",
        "No relocation or office attendance required.",
        "Fully distributed, without travel or office attendance.",
    ],
)
def test_negated_office_attendance_in_a_list_keeps_full_remote(body):
    assert h.is_full_remote_only("Remote worldwide", body) is True


@pytest.mark.parametrize(
    "body",
    [
        "Office attendance is required twice a month.",
        "No visa sponsorship. Office attendance required on Mondays.",
        "No geographic restriction, but office attendance is expected weekly.",
    ],
)
def test_office_attendance_without_negation_is_still_a_conflict(body):
    assert h.is_full_remote_only("Remote worldwide", body) is False

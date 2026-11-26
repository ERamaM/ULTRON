"""Verified finds (specification 30): the agent's judged rows, as ``shortlist save`` records and ranks them."""

from __future__ import annotations

import pytest
from toolkit_helpers import EMPLOYER, POSTING, offline

from ultron.core.ranker.final_rows import dedupe_final_rows, rank_scout_rows
from ultron.profiles import store as profiles
from ultron.stages import scout_verified
from ultron.toolkit import shortlist

SLUG = profiles.EXAMPLE_SLUG
KEEP = {"url": POSTING, "verdict": "keep", "title": "Senior Data Scientist", "company": "Acme",
        "location": "Remote - Spain", "priority": 3, "remote_policy": "remote_spain",
        "eligibility_evidence": "Remote within Spain", "fit_signals": ["Python", "ML"]}
UNSURE = {"url": EMPLOYER, "verdict": "unsure", "title": "Senior ML Engineer", "company": "Example Org", "priority": 1}


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    offline(tmp_path, monkeypatch)


def test_invalid_rows_name_the_field_and_the_rest_are_saved():
    bad = [
        {"url": POSTING, "verdict": "keep", "company": "Acme"},
        {"url": POSTING, "verdict": "unsure", "title": "Role"},
        {**KEEP, "priority": 7},
        {**KEEP, "priority": True},
        {**KEEP, "verdict": "maybe"},
        {**KEEP, "fit_signals": "Python"},
        {**KEEP, "remote_policy": ["remote_eu"]},
        {**KEEP, "eligibility": {"level": "high"}},
    ]
    out = shortlist.save_shortlist(SLUG, [*bad, UNSURE])
    reasons = [row["reason"] for row in out["not_saved"]]
    for field, reason in zip(("title", "company", "priority", "priority", "verdict", "fit_signals",
                              "remote_policy", "eligibility"), reasons, strict=True):
        assert reason.startswith(field)
    assert out["saved"] == {"keep": 0, "unsure": 1, "discard": 0}


def test_saved_rows_keep_the_agents_order_and_fields():
    out = shortlist.save_shortlist(SLUG, [KEEP, UNSURE, {**KEEP, "url": POSTING.replace("45", "46"),
                                                         "title": "Lead Data Scientist", "priority": 1}])
    assert [row["priority"] for row in out["top"]] == [1, 3, 1]  # keeps first, then the unsure row
    assert out["verified_finds"] == 3 and out["roles"] == 3


def test_final_cap_keeps_the_best_rows(monkeypatch):
    original = shortlist.runs.new_run

    def capped(*args, **kwargs):
        run_id = original(*args, **kwargs)
        run = shortlist.runs.load_run(run_id)
        run["settings"]["max_roles"] = 1
        shortlist.runs._write_run(run_id, run)
        return run_id
    monkeypatch.setattr(shortlist.runs, "new_run", capped)
    out = shortlist.save_shortlist(SLUG, [UNSURE, KEEP])
    assert out["roles"] == 1 and out["final_cap"] == 1 and out["top"][0]["title"] == "Senior Data Scientist"


def test_verified_judgment_is_cleaned_and_bounded():
    judged = scout_verified.judgment({"verdict": "keep", "title": "  Role\n" + "x" * 900, "company": "Acme",
                                      "fit_signals": [f"s{i}" for i in range(12)] + [""],
                                      "eligibility_evidence": "e" * 400, "discard_reason": "ignored"})
    assert len(judged["title"]) == scout_verified.TEXT_LIMIT and judged["title"].startswith("Role")
    assert len(judged["fit_signals"]) == scout_verified.MAX_FIT_SIGNALS
    assert len(judged["eligibility_evidence"]) == scout_verified.EVIDENCE_LIMIT
    assert judged["priority"] == 2 and judged["discard_reason"] == ""
    assert scout_verified.judgment({"verdict": "discard"})["eligibility"] == "unknown"
    assert not scout_verified.is_verified([]) and scout_verified.is_verified({"verdict": None})


def test_a_verified_find_wins_its_duplicate_group_whatever_the_score():
    verified = scout_verified.final_row({**scout_verified.judgment({**UNSURE}), "identity_key": "k1",
                                         "apply_url": EMPLOYER, "source_type": "employer_page"})
    extra = verified.model_copy(update={"found_by": "expansion", "evaluation_verdict": "keep", "match_score": 99.0,
                                        "apply_url": EMPLOYER + "?ref=board"})
    kept, dropped = dedupe_final_rows([extra, verified])
    assert kept[0].found_by == "agent_verified" and dropped[0].found_by == "expansion"
    ranked = rank_scout_rows([extra.model_copy(update={"apply_url": "https://x.example/other",
                                                       "title": "Other"}), verified])
    assert [row.found_by for row in ranked] == ["agent_verified", "expansion"]


def test_two_verified_postings_with_the_same_title_stay_apart():
    first = scout_verified.final_row({**scout_verified.judgment(UNSURE), "identity_key": "a", "apply_url": EMPLOYER,
                                      "source_type": "employer_page"})
    second = first.model_copy(update={"apply_url": EMPLOYER + "-2"})
    extra = first.model_copy(update={"found_by": "expansion", "apply_url": EMPLOYER + "?ref=x"})
    kept, dropped = dedupe_final_rows([first, second, extra])
    assert [row.apply_url for row in kept] == [EMPLOYER, EMPLOYER + "-2"] and dropped == [extra]


def test_dedupe_groups_stay_aligned_after_verified_postings_kept_apart():
    base = scout_verified.final_row({**scout_verified.judgment(UNSURE), "identity_key": "a", "apply_url": EMPLOYER,
                                     "source_type": "employer_page"})
    a2 = base.model_copy(update={"apply_url": EMPLOYER + "-2"})
    b1 = base.model_copy(update={"title": "Data Platform Lead", "apply_url": "https://x.example/b1",
                                 "found_by": "expansion"})
    b2 = b1.model_copy(update={"apply_url": "https://x.example/b2", "found_by": "agent_verified"})
    kept, dropped = dedupe_final_rows([base, a2, b1, b2])
    assert [row.apply_url for row in kept] == [EMPLOYER, EMPLOYER + "-2", "https://x.example/b2"]
    assert dropped == [b1]

"""Arm comparison arms: parsing, phases, completeness and second-phase results."""

from __future__ import annotations

import arm_arms as aa
import pytest
from arm_inputs import ComparisonError


def test_parse_arms_default_and_custom():
    assert aa.parse_arms(None) == {"baseline": {"kind": "agent"},
                                   "ultron": {"kind": "ultron", "preset": "balanced", "mode": "sweep"}}
    arms = aa.parse_arms("baseline, ultron=balanced, scout=fast/scout")
    assert arms["ultron"] == {"kind": "ultron", "preset": "balanced", "mode": "sweep"}
    assert arms["scout"] == {"kind": "ultron", "preset": "fast", "mode": "scout"}


@pytest.mark.parametrize("text, message", [
    ("baseline", "at least two"),
    ("baseline,baseline", "listed twice"),
    ("baseline,Bad", "arm names"),
    ("baseline,scout=balanced/turbo", "mode one of"),
    ("baseline,scout=Bad Preset/scout", "preset"),
])
def test_parse_arms_rejects_bad_specs(text, message):
    with pytest.raises(ComparisonError, match=message):
        aa.parse_arms(text)


def test_session_arms_defaults_for_old_sessions():
    assert aa.session_arms({}) == aa.parse_arms(aa.DEFAULT_ARMS)
    state = {"expected_arms": aa.parse_arms("baseline,scout=balanced/scout")}
    assert aa.arm_spec(state, "scout")["mode"] == "scout"
    with pytest.raises(ComparisonError, match="not one of"):
        aa.arm_spec(state, "ultron")


def test_phase_keys():
    assert aa.phase_key("scout", 1) == "scout"
    assert aa.phase_key("scout", 2) == "scout@2"
    assert aa.split_key("scout@2") == ("scout", 2)
    assert aa.split_key("scout") == ("scout", 1)
    with pytest.raises(ComparisonError, match="phase"):
        aa.phase_key("scout", 3)
    assert aa.first_phase({"scout": 1, "scout@2": 2}) == {"scout": 1}


def _reps(count, cost=True):
    entry = {"cost": {"tokens": 1, "tool_calls": 1, "seconds": 1}} if cost else {}
    return {str(rep): dict(entry) for rep in range(1, count + 1)}


def test_check_complete_with_three_arms():
    state = {"expected_arms": aa.parse_arms("baseline,ultron,scout=balanced/scout"),
             "arms": {"baseline": _reps(3), "ultron": _reps(3), "scout": _reps(2)}}
    problems = aa.check_complete(state)
    assert any("different repetitions" in p for p in problems)
    assert any("at least 3" in p for p in problems)
    state["arms"]["scout"] = _reps(3)
    state["arms"]["scout@2"] = _reps(1, cost=False)  # second phase never blocks the first
    assert aa.check_complete(state) == []
    del state["arms"]["ultron"]
    assert "arm ultron has no repetitions" in aa.check_complete(state)


def test_second_phase_counts_new_relevant_jobs():
    labels = {job: {"relevant": job in {"a", "n1", "n2"}} for job in ("a", "b", "n1", "n2", "x")}
    ids = {"scout": {"1": ["a", "b"]}, "baseline": {"1": ["a"]},
           "scout@2": {"1": ["a", "n1", "x"]}, "baseline@2": {"1": ["n2"]}}
    state = {"arms": {"scout": {"1": {"cost": {"tokens": 1000}}}, "scout@2": {"1": {"cost": {"tokens": 300}}},
                      "baseline@2": {"1": {}}}}
    result = aa.second_phase(state, ids, labels)
    assert result["new_relevant_in_pool"] == 2
    scout = result["arms"]["scout@2"]
    assert scout["new_relevant_found"] == 1 and scout["new_relevant_missed"] == 1
    assert scout["tokens_per_run"] == 300 and scout["first_phase_tokens_per_run"] == 1000
    assert result["arms"]["baseline@2"]["tokens_per_run"] is None


def test_second_phase_is_empty_without_a_second_phase():
    assert aa.second_phase({"arms": {}}, {"scout": {"1": ["a"]}}, {}) == {"new_relevant_in_pool": 0, "arms": {}}

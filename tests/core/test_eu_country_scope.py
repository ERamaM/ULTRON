"""Profiles that target an EU or EEA country get the European scope rules."""

from __future__ import annotations

import pytest

from ultron.core.decision.unit import characterize_job, load_allowed_domains
from ultron.core.models import ExtractedJob
from ultron.core.rules.geography import geo_scope, is_european_scope


def _profile(preferred, eligibility, *, remote="full_remote", titles=("data engineer",), excluded=()):
    return {"slug": "synthetic", "criteria": {
        "role_focus_terms": list(titles), "target_titles": list(titles),
        "seniority": {"required": False}, "remote_policy": {"required": remote},
        "location_scope": {"preferred": list(preferred), "eligibility_terms": list(eligibility),
                           "excluded_terms": list(excluded)},
    }}


@pytest.mark.parametrize(
    "preferred",
    [
        ["Ireland"], ["Germany"], ["France", "Paris"], ["Remote Netherlands"], ["Czech Republic"], ["Czechia"],
        ["Portugal"], ["Norway"], ["Iceland"], ["Liechtenstein"], ["Österreich", "Austria"],
    ],
)
def test_eu_and_eea_countries_select_the_european_scope(preferred):
    assert is_european_scope(preferred)
    assert geo_scope(_profile(preferred, [])) == "eu"


@pytest.mark.parametrize(
    "preferred",
    [["LATAM"], ["Brazil", "Remote Brazil"], ["United Kingdom", "London"], ["Switzerland"], ["Canada"], ["India"]],
)
def test_non_eu_regions_keep_the_other_scope(preferred):
    assert not is_european_scope(preferred)
    assert geo_scope(_profile(preferred, [])) == "other"


def test_existing_european_tokens_still_select_the_european_scope():
    assert geo_scope(_profile(["Spain", "Remote EU", "Remote Europe"], [])) == "eu"
    assert geo_scope(_profile(["EMEA"], [])) == "eu"
    assert geo_scope(_profile([], [])) == "global"


def _job(n, *, title, location, remote_text, body):
    url = f"https://boards.greenhouse.io/examplecorp/jobs/{n}"
    return ExtractedJob(apply_url=url, domain="boards.greenhouse.io", title=title, company="Examplecorp",
                        location=location, remote_text=remote_text, body_text=body)


def _outcome(job, profile):
    result = characterize_job(job, profile, load_allowed_domains(), allow_outside_ats_allowlist=True,
                              target_regions=tuple(profile["criteria"]["location_scope"]["preferred"]))
    return result["pipeline_characterization"]["outcome"], result["pipeline_characterization"]["reason"]


def test_germany_profile_keeps_remote_germany_and_drops_us_only_remote():
    profile = _profile(["Germany", "Remote Germany"], ["germany", "berlin"])
    germany = _job(1, title="Data Engineer", location="Remote, Germany", remote_text="Remote",
                   body="Work fully remote from anywhere in Germany. Apply now.")
    us_only = _job(2, title="Data Engineer", location="Remote - US", remote_text="Remote",
                   body="Remote role, US only. Apply now.")
    assert _outcome(germany, profile) == ("keep", None)
    assert _outcome(us_only, profile) == ("discard", "non_eu_remote")


def test_ireland_onsite_profile_keeps_a_dublin_role():
    profile = _profile(["Ireland", "Dublin"], ["ireland", "dublin"], remote="any",
                       titles=("registered nurse", "staff nurse"))
    dublin = _job(3, title="Registered Nurse", location="Dublin, Ireland", remote_text="",
                  body="Staff nurse for our ward in Dublin. Patient care. Apply now.")
    assert _outcome(dublin, profile) == ("keep", None)

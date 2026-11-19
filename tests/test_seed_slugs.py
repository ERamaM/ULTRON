"""Slug guessing edge cases: generic/locale subdomains, country second-level domains, blocked generic slugs and
diacritics (MARK II company_probe ``GENERIC_INFRA_HOST_LABELS`` / blocked generic labels)."""

from __future__ import annotations

import httpx
import pytest

from ultron.net import HttpClient
from ultron.seeds import Proposal, slug_candidates, verify_proposal


def make_client(handler):
    return HttpClient(max_requests=300, max_requests_per_host=300, min_interval_per_host=0.0,
                      transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
                      sleep=lambda s: None)


JOBS = {"jobs": [{"id": 1, "title": "Role", "absolute_url": "https://boards.greenhouse.io/careers/jobs/1",
                  "location": {"name": "X"}}]}


@pytest.mark.parametrize("domain", [
    "careers.acme.com", "https://jobs.acme.io/", "es.acme.com", "boards.acme.com", "talent.acme.de",
    "www.careers.acme.com", "acme.co.uk", "jobs.acme.com.au",
])
def test_domain_label_is_the_registrable_name_not_a_generic_subdomain(domain):
    out = slug_candidates("Acme", domain)
    assert out == ["acme"], out


@pytest.mark.parametrize("domain", ["acme.github.io", "acme.notion.site", "careers.co.uk"])
def test_shared_hosting_and_bare_country_domains_give_no_domain_label(domain):
    out = slug_candidates("Zyxcorp", domain)
    assert out == ["zyxcorp"], out


def test_guessed_generic_subdomain_slug_does_not_verify_someone_elses_board():
    seen: list[str] = []

    def handler(request):
        seen.append(request.url.path)
        if "greenhouse" in request.url.host and "/careers/" in request.url.path:
            return httpx.Response(200, json=JOBS)
        return httpx.Response(404)

    res = verify_proposal(Proposal(company="Zyxcorp", domain="careers.zyxcorp.com"),
                          client=make_client(handler), timeout=5)
    assert res.status != "verified", (res.seed.ats, res.seed.board_identifier)
    assert not any("/careers/" in path for path in seen)


@pytest.mark.parametrize("company", ["Careers", "Jobs", "Remote", "Hiring", "Team"])
def test_generic_company_names_are_never_probed(company):
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=JOBS)

    assert slug_candidates(company, "") == []
    res = verify_proposal(Proposal(company=company), client=make_client(handler), timeout=5)
    assert res.status == "unresolved" and res.reason == "no_board_found"
    assert seen == []


@pytest.mark.parametrize("company,expected", [
    ("Société Générale", ["societegenerale", "societe-generale"]),
    ("Zürich Insurance", ["zurichinsurance", "zurich-insurance"]),
    ("Ñaña Labs", ["nanalabs", "nana-labs"]),
    ("Crème Brûlée GmbH", ["cremebruleegmbh", "creme-brulee-gmbh", "cremebrulee", "creme-brulee"]),
    ("Straße Ørsted AG", ["strasseorstedag", "strasse-orsted-ag", "strasseorsted", "strasse-orsted"]),
])
def test_diacritics_are_transliterated_not_dropped(company, expected):
    assert slug_candidates(company, "") == expected

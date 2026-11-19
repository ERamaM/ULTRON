"""Wrong-tenant protection: a guessed slug verifies only when the board provably belongs to the company."""

from __future__ import annotations

import json

import httpx
import pytest

from ultron import http_factory, profiles, runs
from ultron.cli import main
from ultron.net import HttpClient
from ultron.seeds import Proposal, load_inventory, verify_proposal


def make_client(handler):
    return HttpClient(max_requests=300, max_requests_per_host=300, min_interval_per_host=0.0,
                      transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
                      sleep=lambda s: None)


def gh_jobs(board: str, company_name: str | None = None) -> dict:
    job = {"id": 1, "title": "Engineer", "absolute_url": f"https://boards.greenhouse.io/{board}/jobs/1",
           "location": {"name": "Madrid"}}
    if company_name is not None:
        job["company_name"] = company_name
    return {"jobs": [job]}


def lever_jobs(board: str) -> list:
    return [{"id": "a1", "text": "Engineer", "hostedUrl": f"https://jobs.lever.co/{board}/a1",
             "categories": {"location": "Madrid"}}]


def handler_for(boards: dict[tuple[str, str], object]):
    """``boards``: (vendor, slug) -> JSON payload served for that board; everything else is a 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        for (vendor, slug), payload in boards.items():
            if vendor == "greenhouse" and host == "boards-api.greenhouse.io" and path == f"/v1/boards/{slug}/jobs":
                return httpx.Response(200, json=payload)
            if vendor == "lever" and host == "api.lever.co" and path == f"/v0/postings/{slug}":
                return httpx.Response(200, json=payload)
        return httpx.Response(404)

    return handler


def test_one_word_name_without_domain_is_ownership_unknown():
    client = make_client(handler_for({("greenhouse", "acme"): gh_jobs("acme")}))
    res = verify_proposal(Proposal("Acme"), client=client, timeout=5)
    assert (res.status, res.reason, res.seed) == ("unresolved", "ownership_unknown", None)
    assert res.candidate == {"ats": "greenhouse", "board": "acme", "postings": 1}


def test_one_word_name_needs_the_domain_slug_even_when_the_vendor_name_matches():
    client = make_client(handler_for({("greenhouse", "acme"): gh_jobs("acme", "Acme")}))
    assert verify_proposal(Proposal("Acme"), client=client, timeout=5).reason == "ownership_unknown"


def test_legal_suffix_does_not_make_a_name_multi_word():
    client = make_client(handler_for({("greenhouse", "acme"): gh_jobs("acme", "Acme")}))
    assert verify_proposal(Proposal("Acme GmbH"), client=client, timeout=5).reason == "ownership_unknown"


@pytest.mark.parametrize("domain", ["acme.com", "https://careers.acme.com/", "acme.co.uk"])
def test_domain_derived_slug_verifies(domain):
    client = make_client(handler_for({("greenhouse", "acme"): gh_jobs("acme")}))
    res = verify_proposal(Proposal("Acme", domain=domain), client=client, timeout=5)
    assert res.status == "verified" and (res.seed.ats, res.seed.board_identifier) == ("greenhouse", "acme")


def test_name_slug_differing_from_the_domain_slug_is_not_domain_derived():
    client = make_client(handler_for({("greenhouse", "acme"): gh_jobs("acme")}))
    res = verify_proposal(Proposal("Acme", domain="acmeinsurance.com"), client=client, timeout=5)
    assert res.reason == "ownership_unknown"


def test_multi_word_name_verifies_when_the_vendor_names_the_same_company():
    client = make_client(handler_for({("greenhouse", "examplelabs"): gh_jobs("examplelabs", "Example Labs, Inc.")}))
    res = verify_proposal(Proposal("Example Labs"), client=client, timeout=5)
    assert res.status == "verified" and res.seed.board_identifier == "examplelabs"


def test_diacritics_and_legal_forms_are_normalized_in_the_vendor_name():
    client = make_client(handler_for({("greenhouse", "nanalabs"): gh_jobs("nanalabs", "NANA LABS S.L.")}))
    assert verify_proposal(Proposal("Ñaña Labs"), client=client, timeout=5).status == "verified"


def test_multi_word_name_with_another_vendor_name_is_wrong_tenant():
    client = make_client(handler_for({("greenhouse", "examplelabs"): gh_jobs("examplelabs", "Example Laboratories")}))
    res = verify_proposal(Proposal("Example Labs"), client=client, timeout=5)
    assert (res.status, res.reason) == ("unresolved", "wrong_tenant")
    assert res.candidate == {"ats": "greenhouse", "board": "examplelabs", "postings": 1}


def test_multi_word_name_without_vendor_metadata_is_ownership_unknown():
    client = make_client(handler_for({("lever", "examplelabs"): lever_jobs("examplelabs")}))
    res = verify_proposal(Proposal("Example Labs"), client=client, timeout=5)
    assert (res.reason, res.candidate["ats"]) == ("ownership_unknown", "lever")


def test_wrong_tenant_hit_does_not_stop_the_search_for_the_owned_board():
    client = make_client(handler_for({
        ("greenhouse", "examplelabs"): gh_jobs("examplelabs", "Other Company"),
        ("lever", "examplelabs"): lever_jobs("examplelabs"),
    }))
    res = verify_proposal(Proposal("Example Labs", domain="examplelabs.io"), client=client, timeout=5)
    # Domain-derived slug on greenhouse is accepted first (the vendor name is only checked for name guesses).
    assert res.status == "verified" and res.seed.ats == "greenhouse"
    client = make_client(handler_for({
        ("greenhouse", "examplelabs"): gh_jobs("examplelabs", "Other Company"),
        ("greenhouse", "example-labs"): gh_jobs("example-labs", "Example Labs"),
    }))
    res = verify_proposal(Proposal("Example Labs"), client=client, timeout=5)
    assert res.status == "verified" and res.seed.board_identifier == "example-labs"


def test_ownership_unknown_outranks_wrong_tenant():
    client = make_client(handler_for({
        ("greenhouse", "examplelabs"): gh_jobs("examplelabs", "Other Company"),
        ("lever", "examplelabs"): lever_jobs("examplelabs"),
    }))
    res = verify_proposal(Proposal("Example Labs"), client=client, timeout=5)
    assert res.reason == "ownership_unknown" and res.candidate["ats"] == "lever"


def test_explicit_board_skips_the_ownership_check():
    client = make_client(handler_for({("greenhouse", "acme"): gh_jobs("acme", "Somebody Else")}))
    assert verify_proposal(Proposal("Acme", ats="greenhouse", board="acme"), client=client, timeout=5).status == "verified"
    res = verify_proposal(Proposal("Acme", careers_url="https://boards.greenhouse.io/acme"), client=client, timeout=5)
    assert res.status == "verified"


def test_seeds_verify_reports_ownership_counts_and_the_candidate(capsys, tmp_path):
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "fast")
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler_for({
            ("greenhouse", "acme"): gh_jobs("acme"),
            ("greenhouse", "examplelabs"): gh_jobs("examplelabs", "Example Laboratories"),
            ("greenhouse", "zentro"): gh_jobs("zentro"),
        })),
        resolver=lambda h, p: ["93.184.216.34"], sleep=lambda s: None, min_interval_per_host=0.0,
    )
    src = tmp_path / "proposals.jsonl"
    src.write_text("".join(json.dumps(row) + "\n" for row in (
        {"company": "Acme"},
        {"company": "Example Labs"},
        {"company": "Zentro", "domain": "zentro.example"},
    )), encoding="utf-8")
    assert main(["seeds", "verify", "--run", run_id, "--from", str(src)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["verified"], out["unresolved"], out["ownership_unknown"], out["wrong_tenant"]) == (1, 2, 1, 1)
    unresolved = {r["company"]: r for r in runs.read_jsonl(runs.run_dir(run_id) / "seeds_unresolved.jsonl")}
    assert unresolved["Acme"]["candidate"] == {"ats": "greenhouse", "board": "acme", "postings": 1}
    assert unresolved["Example Labs"]["reason"] == "wrong_tenant"
    assert [s.board_identifier for s in load_inventory(profiles.EXAMPLE_SLUG) if profiles.EXAMPLE_SLUG in s.profiles] \
        == ["zentro"]

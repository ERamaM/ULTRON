"""One capped hop from a company careers page (or an aggregator posting page) to the ATS boards it links."""

from __future__ import annotations

import httpx

from ultron.net import HttpClient
from ultron.seeds import PageHops, Proposal, verify_proposal


def make_client(handler, resolver=None):
    return HttpClient(max_requests=300, max_requests_per_host=300, min_interval_per_host=0.0,
                      transport=httpx.MockTransport(handler), resolver=resolver or (lambda h, p: ["93.184.216.34"]),
                      sleep=lambda s: None)


def gh_jobs(board: str) -> dict:
    return {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": f"https://boards.greenhouse.io/{board}/jobs/1",
                      "location": {"name": "Madrid"}}]}


def page(*links: str) -> str:
    anchors = "".join(f'<a href="{link}">link</a>' for link in links)
    return f"<html><body><h1>Careers</h1>{anchors}</body></html>"


def site(pages: dict[str, str], boards: tuple[str, ...] = (), seen: list[str] | None = None):
    """Serve ``pages`` (url -> HTML) and the Greenhouse ``boards`` with postings; 404 elsewhere."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        if url in pages:
            return httpx.Response(200, text=pages[url], headers={"content-type": "text/html"})
        for board in boards:
            if request.url.host == "boards-api.greenhouse.io" and request.url.path == f"/v1/boards/{board}/jobs":
                return httpx.Response(200, json=gh_jobs(board))
        return httpx.Response(404)

    return handler


def test_company_careers_page_leads_to_its_ats_board():
    client = make_client(site({"https://acme.example/careers": page("https://boards.greenhouse.io/acmeco")},
                              boards=("acmeco",)))
    res = verify_proposal(Proposal("Acme", careers_url="https://acme.example/careers"), client=client, timeout=5)
    assert res.status == "verified" and (res.seed.ats, res.seed.board_identifier) == ("greenhouse", "acmeco")


def test_page_on_the_proposal_domain_is_followed_whatever_its_path():
    client = make_client(site({"https://www.acme.example/about": page("https://jobs.lever.co/x"),
                               "https://www.acme.example/team": page("https://boards.greenhouse.io/acmeco")},
                              boards=("acmeco",)))
    res = verify_proposal(Proposal("Acme", domain="acme.example", careers_url="https://www.acme.example/team"),
                          client=client, timeout=5)
    assert res.status == "verified" and res.seed.board_identifier == "acmeco"


def test_careers_url_off_the_proposal_domain_is_not_fetched():
    seen: list[str] = []
    client = make_client(site({"https://other.example/careers": page("https://boards.greenhouse.io/acmeco")},
                              boards=("acmeco",), seen=seen))
    res = verify_proposal(Proposal("Acme", domain="acme.example", careers_url="https://other.example/careers"),
                          client=client, timeout=5)
    assert res.reason == "no_board_found" and seen == []


def test_aggregator_posting_page_of_a_feed_company_leads_to_its_board():
    client = make_client(site({"https://remotive.com/remote-jobs/dev/engineer-1": page(
        "https://remotive.com/", "https://boards.greenhouse.io/zentrolabs/jobs/1")}, boards=("zentrolabs",)))
    res = verify_proposal(Proposal("Zentro Labs", source_url="https://remotive.com/remote-jobs/dev/engineer-1"),
                          client=client, timeout=5)
    assert res.status == "verified" and res.seed.board_identifier == "zentrolabs"


def test_aggregator_page_with_several_boards_only_accepts_the_company_one():
    url = "https://remotive.com/remote-jobs/dev/engineer-1"
    other_boards = page("https://boards.greenhouse.io/othercorp", "https://boards.greenhouse.io/thirdcorp")
    client = make_client(site({url: other_boards}, boards=("othercorp", "thirdcorp")))
    assert verify_proposal(Proposal("Zentro Labs", source_url=url), client=client, timeout=5).status == "unresolved"
    # Even a single linked board must carry the company key (or the vendor's name for it).
    client = make_client(site({url: page("https://boards.greenhouse.io/othercorp")}, boards=("othercorp",)))
    assert verify_proposal(Proposal("Zentro Labs", source_url=url), client=client, timeout=5).status == "unresolved"
    client = make_client(site({url: page("https://boards.greenhouse.io/othercorp",
                                         "https://boards.greenhouse.io/zentro-labs")},
                              boards=("othercorp", "zentro-labs")))
    res = verify_proposal(Proposal("Zentro Labs", source_url=url), client=client, timeout=5)
    assert res.status == "verified" and res.seed.board_identifier == "zentro-labs"


def test_links_without_a_vendor_host_never_mint_a_slug_from_the_name():
    seen: list[str] = []
    client = make_client(site({"https://acme.example/careers": page("https://acme.example/jobs?gh_jid=42")},
                              boards=("acme",), seen=seen))
    res = verify_proposal(Proposal("Acme", careers_url="https://acme.example/careers"), client=client, timeout=5)
    assert res.reason == "no_board_found"
    assert not any("greenhouse" in url for url in seen)


def test_redirect_off_the_page_domain_is_not_followed():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "acme.example":
            return httpx.Response(302, headers={"Location": "https://evil.example/careers"})
        return httpx.Response(200, text=page("https://boards.greenhouse.io/evil"))

    res = verify_proposal(Proposal("Acme", careers_url="https://acme.example/careers"), client=make_client(handler),
                          timeout=5)
    assert res.reason == "no_board_found"
    assert seen == ["acme.example"]


def test_careers_page_redirecting_to_its_hosted_board_verifies():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "acme.example":
            return httpx.Response(302, headers={"Location": "https://boards.greenhouse.io/acmeco"})
        if request.url.host == "boards.greenhouse.io":
            return httpx.Response(200, text="<html><body>Acme jobs</body></html>")
        if request.url.host == "boards-api.greenhouse.io" and request.url.path == "/v1/boards/acmeco/jobs":
            return httpx.Response(200, json=gh_jobs("acmeco"))
        return httpx.Response(404)

    res = verify_proposal(Proposal("Acme", careers_url="https://acme.example/careers"), client=make_client(handler),
                          timeout=5)
    assert res.status == "verified" and res.seed.board_identifier == "acmeco"


def test_careers_url_on_a_job_site_without_domain_is_judged_like_an_aggregator():
    url = "https://jobsite.example/careers/acme-123"
    client = make_client(site({url: page("https://boards.greenhouse.io/othercorp")}, boards=("othercorp",)))
    res = verify_proposal(Proposal("Acme", careers_url=url), client=client, timeout=5)
    assert res.reason == "no_board_found"
    client = make_client(site({url: page("https://boards.greenhouse.io/othercorp", "https://boards.greenhouse.io/acme")},
                              boards=("othercorp", "acme")))
    res = verify_proposal(Proposal("Acme", careers_url=url), client=client, timeout=5)
    assert res.status == "verified" and res.seed.board_identifier == "acme"


def test_careers_page_on_a_private_address_is_never_requested():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200, text=page("https://boards.greenhouse.io/acmeco"))

    res = verify_proposal(Proposal("Acme", careers_url="https://acme.example/careers"),
                          client=make_client(handler, resolver=lambda h, p: ["10.0.0.5"]), timeout=5)
    assert res.reason == "no_board_found" and seen == []


def test_page_hops_are_capped_per_host_and_in_total():
    seen: list[str] = []
    pages = {f"https://acme{i}.example/careers": page() for i in range(4)}
    client = make_client(site(pages, seen=seen))
    hops = PageHops(total=2, per_host=1)
    for i in range(4):
        verify_proposal(Proposal(f"Acme {i}", careers_url=f"https://acme{i}.example/careers"), client=client,
                        timeout=5, page_hops=hops)
    assert len(seen) == 2
    seen.clear()
    hops = PageHops(total=5, per_host=1)
    for _ in range(3):
        verify_proposal(Proposal("Acme", careers_url="https://acme0.example/careers"), client=client, timeout=5,
                        page_hops=hops)
    assert len(seen) == 1


def test_careers_page_redirecting_into_a_blocked_vendor_is_unchecked_not_boardless():
    # Session 4 review: a page hop that the vendor breaker refused read as an empty page, so `seeds verify` said
    # no_board_found (a checked company) instead of host_blocked.
    from ultron.net import VendorBudget

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "acme.example":
            return httpx.Response(302, headers={"Location": "https://acme.wd3.myworkdayjobs.com/External"})
        return httpx.Response(200, text="unexpected")

    budget = VendorBudget(max_requests=100, state={"blocked": ["workday"]})
    client = HttpClient(max_requests=300, max_requests_per_host=300, min_interval_per_host=0.0,
                        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
                        sleep=lambda s: None, vendor_budget=budget,
                        vendor_of=lambda h: "workday" if h.endswith(".myworkdayjobs.com") else None)
    res = verify_proposal(Proposal("Acme", careers_url="https://acme.example/careers"), client=client, timeout=5)
    assert res.reason == "host_blocked"
    assert all("myworkdayjobs" not in url for url in seen)


def test_careers_page_redirecting_into_a_vendor_out_of_budget_is_unchecked_not_boardless():
    # Codex review round 2: the vendor-budget refusal on a page hop read as an empty page (no_board_found).
    from ultron.net import VendorBudget

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "acme.example":
            return httpx.Response(302, headers={"Location": "https://acme.wd3.myworkdayjobs.com/External"})
        return httpx.Response(200, text="unexpected")

    budget = VendorBudget(max_requests=4, state={"requests": {"workday": 4}})
    client = HttpClient(max_requests=300, max_requests_per_host=300, min_interval_per_host=0.0,
                        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
                        sleep=lambda s: None, vendor_budget=budget,
                        vendor_of=lambda h: "workday" if h.endswith(".myworkdayjobs.com") else None)
    res = verify_proposal(Proposal("Acme", careers_url="https://acme.example/careers"), client=client, timeout=5)
    assert res.reason == "vendor_budget_exhausted"
    assert all("myworkdayjobs" not in url for url in seen)


def test_careers_page_out_of_host_budget_is_unchecked_not_boardless():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>careers</body></html>", headers={"content-type": "text/html"})

    client = HttpClient(max_requests=300, max_requests_per_host=0, min_interval_per_host=0.0,
                        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
                        sleep=lambda s: None)
    res = verify_proposal(Proposal("Acme", careers_url="https://acme.example/careers"), client=client, timeout=5)
    assert res.reason == "host_budget_exhausted"

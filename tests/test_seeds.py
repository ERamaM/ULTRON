from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
import yaml

from ultron import seeds
from ultron.cli import main
from ultron.core.models import AtsBoardSeed
from ultron.infra.net import HttpClient, VendorBudget
from ultron.profiles import store as profiles
from ultron.seeds import Proposal, add_seeds, load_inventory, slug_candidates, verify_proposal
from ultron.stages import http_factory
from ultron.storage import runs


def make_client(handler, **kw) -> HttpClient:
    opts = {
        "max_requests": 50, "max_requests_per_host": 50, "min_interval_per_host": 0.0,
        "transport": httpx.MockTransport(handler), "resolver": lambda h, p: ["93.184.216.34"],
        "sleep": lambda s: None,
    }
    opts.update(kw)
    return HttpClient(**opts)


GH_JOBS = {"jobs": [
    {"id": i, "title": f"Role {i}", "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{i}",
     "location": {"name": "Madrid"}, "updated_at": "2026-01-01T00:00:00Z"} for i in (1, 2, 3)
]}


def gh_handler(request: httpx.Request) -> httpx.Response:
    if "greenhouse" in request.url.host and "/acme/" in request.url.path:
        return httpx.Response(200, json=GH_JOBS)
    return httpx.Response(404)


def seed(board="acme", ats="greenhouse"):
    return AtsBoardSeed(seed_id=f"{ats}:{board}", company=board.title(), ats=ats, board_identifier=board)


def test_slug_candidates_order():
    assert slug_candidates("Acme Corp", "acme.io") == ["acmecorp", "acme-corp", "acme"]
    assert slug_candidates("Acme", "https://www.acme.com/x") == ["acme"]


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("Acme GmbH", ["acmegmbh", "acme-gmbh", "acme"]),
        ("Acme S.L.", ["acmesl", "acme-s-l", "acme"]),
        ("Acme, Inc.", ["acmeinc", "acme-inc", "acme"]),
        ("Acme Robotics S.A.", ["acmeroboticssa", "acme-robotics-s-a", "acmerobotics", "acme-robotics"]),
        ("Acme Holding AG", ["acmeholdingag", "acme-holding-ag", "acmeholding", "acme-holding"]),
        ("Nordic Widgets Oy", ["nordicwidgetsoy", "nordic-widgets-oy", "nordicwidgets", "nordic-widgets"]),
        ("A&B", ["ab", "a-b", "aandb", "a-and-b"]),
        ("J.P. Widgets", ["jpwidgets", "j-p-widgets", "jp-widgets"]),
        ("Smith & Co.", ["smithco", "smith-co", "smith"]),
    ],
)
def test_slug_candidates_strip_legal_suffixes_without_domain(company, expected):
    assert slug_candidates(company, "") == expected


def test_slug_candidates_extras_are_bounded_and_keep_domain_label():
    out = slug_candidates("Acme Data Systems Ltd", "https://acmedata.example")
    assert out == ["acmedatasystemsltd", "acme-data-systems-ltd", "acmedata", "acmedatasystems", "acme-data-systems"]
    assert len(out) == len(dict.fromkeys(out))


def test_slug_candidates_keep_a_bare_legal_word():
    assert slug_candidates("GmbH", "") == ["gmbh"]
    # Product words are not legal suffixes: stripping them would probe another company's board.
    assert slug_candidates("Acme AI", "") == ["acmeai", "acme-ai"]


def test_verify_probes_suffix_stripped_slug():
    def handler(request):
        if request.url.host == "api.lever.co" and request.url.path.endswith("/acme"):
            return httpx.Response(200, json=[{"text": "Senior ML Engineer", "hostedUrl": "https://jobs.lever.co/acme/1"}])
        return httpx.Response(404)

    res = verify_proposal(Proposal("Acme GmbH"), client=make_client(handler, max_requests=200), timeout=5)
    # A one-word name without a domain slug is never proof of ownership; the probed board is reported.
    assert res.reason == "ownership_unknown" and res.candidate == {"ats": "lever", "board": "acme", "postings": 1}
    res = verify_proposal(Proposal("Acme GmbH", domain="acme.de"), client=make_client(handler, max_requests=200),
                          timeout=5)
    assert res.status == "verified" and res.seed.ats == "lever" and res.seed.board_identifier == "acme"


def test_verify_probes_literal_slugs_on_every_vendor_before_normalized_guesses():
    def handler(request):
        if "greenhouse" in request.url.host and "/acme/" in request.url.path:
            return httpx.Response(200, json=GH_JOBS)  # a different company's board
        if request.url.host == "api.lever.co" and request.url.path.rstrip("/").endswith("/acmegmbh"):
            return httpx.Response(200, json=[{"text": "Senior ML Engineer", "hostedUrl": "https://jobs.lever.co/acmegmbh/1"}])
        return httpx.Response(404)

    res = verify_proposal(Proposal("Acme GmbH"), client=make_client(handler, max_requests=200), timeout=5)
    # The literal "acmegmbh" board on a later vendor is found before the normalized "acme" guess.
    assert res.reason == "ownership_unknown"
    assert res.candidate == {"ats": "lever", "board": "acmegmbh", "postings": 1}


def test_verify_explicit_pair():
    res = verify_proposal(Proposal("Acme", ats="greenhouse", board="acme"), client=make_client(gh_handler), timeout=5)
    assert res.status == "verified" and res.postings == 3
    assert res.seed.source_type == "agent_generated" and res.seed.source_label == "Agent proposal"
    assert res.seed.verification["postings"] == 3 and res.seed.verification["verified_at"]
    assert res.seed.seed_id == "greenhouse:acme"


def test_verify_probes_until_found():
    def handler(request):
        if request.url.host == "api.lever.co":
            return httpx.Response(200, json=[{"text": "Senior ML Engineer", "hostedUrl": "https://jobs.lever.co/acme/1"}])
        return httpx.Response(404)

    res = verify_proposal(Proposal("Acme", domain="acme.io"), client=make_client(handler), timeout=5)
    assert res.status == "verified" and res.seed.ats == "lever" and res.postings == 1


def test_verify_slug_probing_rejects_empty_board():
    def handler(request):
        if request.url.host == "api.lever.co":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    res = verify_proposal(Proposal("Acme", domain="acme.io"), client=make_client(handler), timeout=5)
    assert res.status == "unresolved" and res.reason == "no_board_found"

    explicit = verify_proposal(Proposal("Acme", ats="lever", board="acme"), client=make_client(handler), timeout=5)
    assert explicit.status == "verified" and explicit.postings == 0


def test_verify_unresolved():
    res = verify_proposal(Proposal("Acme"), client=make_client(lambda r: httpx.Response(404)), timeout=5)
    assert res.status == "unresolved" and res.reason == "no_board_found" and res.seed is None


def test_verify_ashby_null_job_board_is_unresolved():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    res = verify_proposal(Proposal("Acme", domain="acme.io"), client=make_client(handler), timeout=5)
    assert res.status == "unresolved" and res.reason == "no_board_found" and res.seed is None


def test_verify_budget_exhausted():
    client = make_client(lambda r: httpx.Response(404), max_requests=1)
    res = verify_proposal(Proposal("Acme"), client=client, timeout=5)
    assert res.status == "unresolved" and res.reason == "budget_exhausted"


def test_verify_unsupported_vendor():
    p = Proposal("Acme", ats="nosuchvendor", board="acme")
    res = verify_proposal(p, client=make_client(lambda r: httpx.Response(404)), timeout=5)
    assert res.reason == "vendor_not_supported_yet"


def test_verify_unparseable_taleo_board_is_reported_as_such():
    shell = "<html><body><div id='app'></div><script src='/app.js'></script></body></html>"
    p = Proposal("Acme", ats="taleo", board="acme.taleo.net/careersection/ex")
    res = verify_proposal(p, client=make_client(lambda r: httpx.Response(200, text=shell)), timeout=5)
    assert res.status == "unresolved" and res.reason == "unparseable_board"


def test_verify_explicit_ats_board_does_not_probe_other_vendors():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "api.lever.co":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    res = verify_proposal(Proposal("Acme", ats="greenhouse", board="wrong-slug"), client=make_client(handler), timeout=5)
    assert res.status == "unresolved" and res.reason == "no_board_found"
    assert seen == ["boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"]


def test_verify_explicit_careers_url_does_not_probe_other_vendors():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(404)

    res = verify_proposal(
        Proposal("Acme", careers_url="https://jobs.ashbyhq.com/wrong-slug"),
        client=make_client(handler),
        timeout=5,
    )
    assert res.status == "unresolved" and res.reason == "no_board_found"
    assert seen == ["api.ashbyhq.com"]

    seen.clear()
    custom = verify_proposal(
        Proposal("Acme", careers_url="https://acme.example/careers"),
        client=make_client(handler),
        timeout=5,
    )
    assert custom.status == "unresolved" and custom.reason == "no_board_found"
    assert seen == ["acme.example"]  # one capped hop to the careers page itself, no slug guessing


def test_add_seeds_atomic_and_links_profile():
    assert add_seeds([seed()], profile_slug="p1") == 1
    assert add_seeds([seed()], profile_slug="p2") == 0
    rows = yaml.safe_load(seeds.auto_seeds_path().read_text(encoding="utf-8"))["seeds"]
    assert len(rows) == 1 and rows[0]["profiles"] == ["p1", "p2"]
    assert not list(seeds.auto_seeds_path().parent.glob("*.tmp"))


def test_inventory_profile_seeds_first():
    add_seeds([seed("zzz")], profile_slug="p1")
    add_seeds([seed("other")], profile_slug="p2")
    inv = load_inventory("p1")
    assert inv[0].board_identifier == "zzz"
    assert len(inv) > 1  # bundled pack follows
    assert all(s.board_identifier != "other" for s in inv)
    keys = [(s.ats, s.board_identifier.lower()) for s in inv]
    assert len(keys) == len(set(keys))


def test_inventory_dedups_pack_against_auto():
    pack_first = load_inventory(None)[0]
    add_seeds([seed(pack_first.board_identifier, pack_first.ats)], profile_slug="p1")
    inv = load_inventory("p1")
    assert inv[0].profiles == ["p1"]
    assert sum(1 for s in inv if s.board_identifier == pack_first.board_identifier) == 1


def _run(monkeypatch):
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "fast")
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(gh_handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    return run_id


def test_seeds_verify_cli(monkeypatch, capsys, tmp_path):
    run_id = _run(monkeypatch)
    src = tmp_path / "proposals.jsonl"
    src.write_text(
        json.dumps({"company": "Acme", "ats": "greenhouse", "board": "acme"}) + "\n"
        + json.dumps({"company": "Nada Ñ"}, ensure_ascii=False) + "\n", encoding="utf-8")
    assert main(["seeds", "verify", "--run", run_id, "--from", str(src)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verified"] == 1 and out["unresolved"] == 1
    assert out["unresolved_sample"][0]["reason"] == "no_board_found"
    d = runs.run_dir(run_id)
    assert len(runs.read_jsonl(d / "seeds_verified.jsonl")) == 1
    assert runs.read_jsonl(d / "seeds_unresolved.jsonl")[0]["company"] == "Nada Ñ"
    assert "seeds" in runs.load_run(run_id)["stages_done"]
    assert load_inventory(profiles.EXAMPLE_SLUG)[0].board_identifier == "acme"


def test_seeds_verify_stops_probing_a_rate_limited_vendor_host(capsys, tmp_path):
    # Slug guessing sends many requests to few vendor hosts (the per-host cap is the whole page budget): a host
    # that keeps answering 429 must get no more probes in this call.
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "fast")
    hits: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        hits[request.url.host] = hits.get(request.url.host, 0) + 1
        return httpx.Response(429 if request.url.host == "apply.workable.com" else 404)

    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    src = tmp_path / "proposals.jsonl"
    src.write_text("".join(json.dumps({"company": f"Example Health {i}", "domain": f"example-health{i}.test"}) + "\n"
                           for i in range(4)), encoding="utf-8")
    try:
        assert main(["seeds", "verify", "--run", run_id, "--from", str(src)]) == 0
    finally:
        http_factory.reset_test_overrides()
    assert json.loads(capsys.readouterr().out)["unresolved"] == 4
    assert hits["apply.workable.com"] <= 3


def test_rate_limited_explicit_board_is_reported_as_host_blocked_not_missing(capsys, tmp_path):
    # "no_board_found" tells the agent to drop the company; a board we could not ask about must say so instead.
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "fast")
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(lambda request: httpx.Response(429)), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    src = tmp_path / "proposals.jsonl"
    src.write_text("".join(json.dumps({"company": f"Example {i}", "ats": "workable", "board": f"example{i}"}) + "\n"
                           for i in range(3)), encoding="utf-8")
    try:
        assert main(["seeds", "verify", "--run", run_id, "--from", str(src)]) == 0
    finally:
        http_factory.reset_test_overrides()
    capsys.readouterr()
    reasons = [row["reason"] for row in runs.read_jsonl(runs.run_dir(run_id) / "seeds_unresolved.jsonl")]
    assert reasons == ["host_blocked"] * 3


def test_a_rate_limited_board_does_not_block_another_tenant_host_of_the_same_vendor():
    # PR #57 review: a 403 from the explicit board's host must not skip the careers_url board on another host of
    # the same vendor; only the client's host-scoped breaker decides what is blocked.
    offers = {"offers": [{"title": "Staff Nurse", "careers_url": "https://newco.recruitee.com/o/staff-nurse"}]}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "oldco.recruitee.com":
            return httpx.Response(403)
        return httpx.Response(200, json=offers)

    proposal = Proposal(company="Example Hospital", ats="recruitee", board="oldco",
                        careers_url="https://newco.recruitee.com/")
    result = verify_proposal(proposal, client=make_client(handler, block_threshold=3), timeout=5, verify_only=True)
    assert result.status == "verified" and result.seed.board_identifier == "newco"
    assert {"oldco.recruitee.com", "newco.recruitee.com"} <= set(seen)


def test_a_single_rate_limited_answer_does_not_skip_the_next_board_on_that_host():
    # Below the breaker threshold the next board on the same host is still asked (the breaker needs 3 answers).
    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        board = request.url.path.split("/")[3]
        calls[board] = calls.get(board, 0) + 1
        if board == "first" and calls[board] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=GH_JOBS)

    client = make_client(handler, block_threshold=3)
    result = verify_proposal(Proposal(company="Acme", ats="greenhouse", board="first"), client=client, timeout=5)
    assert result.status == "verified"


def test_seeds_verify_unknown_run(capsys, tmp_path):
    assert main(["seeds", "verify", "--run", "nope", "--from", str(tmp_path / "x.jsonl")]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "run_not_found"


@pytest.mark.parametrize("vendor_exhausted", [False, True])
def test_seed_budget_exhaustion_only_skips_matching_scope(vendor_exhausted):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"offers": []})

    proposal = Proposal(company="Example Hospital", ats="recruitee", board="first",
                        careers_url="https://second.recruitee.com/")
    with make_client(handler, max_requests_per_host=1, vendor_of=lambda _: "recruitee",
                     vendor_budget=VendorBudget(max_requests=1 if vendor_exhausted else 10)) as client:
        client.get_text("https://first.recruitee.com/api/offers")
        result = verify_proposal(proposal, client=client, timeout=5, verify_only=True)
    assert calls == (["first.recruitee.com"] if vendor_exhausted else ["first.recruitee.com", "second.recruitee.com"])
    if not vendor_exhausted:
        assert result.status == "verified" and result.seed.board_identifier == "second"


@pytest.mark.parametrize("directory", [False, True])
def test_invalid_seed_proposal_path_preserves_reports(monkeypatch, capsys, tmp_path, directory):
    run_id = _run(monkeypatch)
    source = tmp_path / "proposals.jsonl"
    if directory:
        source.mkdir()
    previous = runs.load_run(run_id)
    paths = [runs.run_dir(run_id) / f"seeds_{kind}.jsonl" for kind in ("verified", "unresolved")]
    for path in paths:
        path.write_bytes(b'{"company":"Previous"}\n')
    http_factory.set_test_overrides(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request")))
    assert main(["seeds", "verify", "--run", run_id, "--from", str(source)]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_proposals"
    assert all(path.read_bytes() == b'{"company":"Previous"}\n' for path in paths)
    assert runs.load_run(run_id) == previous


def test_seeds_list_cli(capsys):
    assert main(["seeds", "list", "--profile", "p1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] > 0 and len(out["seeds"]) <= 30


def test_run_new_reports_profile_seeds(capsys):
    profiles.install_example()
    add_seeds([seed()], profile_slug=profiles.EXAMPLE_SLUG)
    assert main(["run", "new", "--profile", profiles.EXAMPLE_SLUG, "--preset", "fast"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["profile_seeds"] == 1 and out["new_companies_target"] == 10


def test_client_for_run_budgets():
    from ultron.stages.http_factory import client_for_run

    run = {"settings": {"max_pages": 70, "max_seeds_per_run": 120, "max_requests_per_host": 8, "ats_api_timeout": 10}}
    c = client_for_run(run)
    assert c.max_requests == 310 and c.max_requests_per_host == 8 and c.timeout == 10
    assert pytest.approx(client_for_run(run, timeout=3).timeout) == 3


def test_verify_private_ip_rejected_does_not_crash_run(monkeypatch, capsys, tmp_path):
    run_id = _run(monkeypatch)
    http_factory.set_test_overrides(
        resolver=lambda h, p: ["10.0.0.5"] if h == "evil.example" else ["93.184.216.34"])
    src = tmp_path / "p.jsonl"
    src.write_text(
        json.dumps({"company": "Evil", "careers_url": "https://evil.example/jobs"}) + "\n"
        + json.dumps({"company": "Acme", "ats": "greenhouse", "board": "acme"}) + "\n", encoding="utf-8")
    assert main(["seeds", "verify", "--run", run_id, "--from", str(src)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verified"] == 1 and out["unresolved"] == 1
    assert "seeds" in runs.load_run(run_id)["stages_done"]
    assert (runs.run_dir(run_id) / "seeds_verified.jsonl").is_file()


def test_verify_malformed_json_is_a_miss(monkeypatch):
    def boom(*a, **k):
        raise KeyError("title")

    monkeypatch.setattr(seeds.verify, "fetch_vendor_jobs", boom)
    client = make_client(gh_handler)
    res = verify_proposal(Proposal("Acme"), client=client, timeout=5)
    assert res.status == "unresolved"


def test_verify_private_resolver_unresolved():
    client = make_client(gh_handler, resolver=lambda h, p: ["127.0.0.1"])
    res = verify_proposal(Proposal("Acme", ats="greenhouse", board="acme"), client=client, timeout=5)
    assert res.status == "unresolved" and res.reason == "no_board_found"


def test_per_host_budget_skips_vendor_but_tries_others():
    def handler(request):
        if request.url.host == "api.lever.co":
            return httpx.Response(200, json=[{"text": "AI Engineer", "hostedUrl": "https://jobs.lever.co/acme/1"}])
        return httpx.Response(404)

    client = make_client(handler, max_requests_per_host=1)
    first = verify_proposal(Proposal("Acme", domain="acme.io"), client=client, timeout=5)
    assert first.status == "verified" and first.seed.ats == "lever"
    second = verify_proposal(Proposal("Other"), client=client, timeout=5)
    assert second.reason != "budget_exhausted"


def test_global_budget_is_budget_exhausted_even_with_host_room():
    client = make_client(lambda r: httpx.Response(404), max_requests=2, max_requests_per_host=10)
    res = verify_proposal(Proposal("Acme Corp", domain="acme.io"), client=client, timeout=5)
    assert res.reason == "budget_exhausted"


def test_add_seeds_refuses_corrupt_file_and_keeps_backup():
    path = seeds.auto_seeds_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("seeds: [unclosed", encoding="utf-8")
    with pytest.raises(seeds.SeedFileError):
        add_seeds([seed()], profile_slug="p1")
    assert path.read_text(encoding="utf-8") == "seeds: [unclosed"
    assert list(path.parent.glob("auto_seeds.yaml.corrupt-*"))


def test_add_seeds_preserves_invalid_rows():
    path = seeds.auto_seeds_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("seeds:\n- {company: X, ats: nonsense, board_identifier: x}\n", encoding="utf-8")
    assert add_seeds([seed()], profile_slug="p1") == 1
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["seeds"]
    assert len(rows) == 2 and rows[0]["ats"] == "nonsense"


def test_verify_cli_corrupt_seed_file_errors(monkeypatch, capsys, tmp_path):
    run_id = _run(monkeypatch)
    seeds.auto_seeds_path().write_text("seeds: [", encoding="utf-8")
    src = tmp_path / "p.jsonl"
    src.write_text(json.dumps({"company": "Acme", "ats": "greenhouse", "board": "acme"}) + "\n", encoding="utf-8")
    assert main(["seeds", "verify", "--run", run_id, "--from", str(src)]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "seed_file_corrupt"


def _write_proposals(tmp_path, rows):
    src = tmp_path / "proposals.jsonl"
    src.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(src)


def test_seeds_verify_cli_not_limited_by_preset_per_host_cap(capsys, tmp_path):
    """R14: 35 Greenhouse proposals with balanced (per-host 30) all resolve."""
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    assert runs.load_run(run_id)["settings"]["max_requests_per_host"] < 35

    def handler(request):
        if "greenhouse" in request.url.host:
            return httpx.Response(200, json=GH_JOBS)
        return httpx.Response(404)

    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    src = _write_proposals(tmp_path, [
        {"company": f"Board {i}", "ats": "greenhouse", "board": f"board{i}"} for i in range(35)
    ])
    assert main(["seeds", "verify", "--run", run_id, "--from", src]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"verified": 35, "new": 35, "unresolved": 0, "ownership_unknown": 0, "wrong_tenant": 0,
                   "unresolved_sample": []}


def test_seeds_verify_cli_global_budget_still_exhausts(capsys, tmp_path):
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0, max_requests=3,
    )
    src = _write_proposals(tmp_path, [{"company": "Acme Corp", "domain": "acme.io"}])
    assert main(["seeds", "verify", "--run", run_id, "--from", src]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unresolved_sample"] == [{"company": "Acme Corp", "reason": "budget_exhausted"}]


@pytest.mark.parametrize(("name", "label"), [
    ("seed_proposals_from_feeds.jsonl", "feed_company"),
    ("agent_proposals.jsonl", "agent_generated"),
])
def test_seeds_verify_labels_feed_proposals_as_feed_company(monkeypatch, capsys, tmp_path, name, label):
    run_id = _run(monkeypatch)
    src = tmp_path / name
    src.write_text(json.dumps({"company": "Acme", "ats": "greenhouse", "board": "acme"}) + "\n", encoding="utf-8")
    assert main(["seeds", "verify", "--run", run_id, "--from", str(src)]) == 0
    stored = next(s for s in load_inventory(profiles.EXAMPLE_SLUG) if s.board_identifier == "acme")
    assert stored.source_type == label


def test_seeds_verify_reports_new_count_and_respects_limit(capsys, tmp_path):
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "fast")  # new_companies_per_run == 10
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=GH_JOBS)),
        resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None,
        min_interval_per_host=0.0,
    )
    rows = [{"company": f"Board {i}", "ats": "greenhouse", "board": f"board{i}"} for i in range(12)]
    feed_file = tmp_path / seeds.FEED_PROPOSALS_FILE
    feed_file.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    # Default limit for seed_proposals_from_feeds.jsonl is the run's new_companies_per_run (10).
    assert main(["seeds", "verify", "--run", run_id, "--from", str(feed_file)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"verified": 10, "new": 10, "unresolved": 0, "ownership_unknown": 0, "wrong_tenant": 0,
                   "unresolved_sample": []}

    # Re-verifying with explicit --limit 3 processes 3 rows and reports new=0 for already-stored boards.
    assert main(["seeds", "verify", "--run", run_id, "--from", str(feed_file), "--limit", "3"]) == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2 == {"verified": 3, "new": 0, "unresolved": 0, "ownership_unknown": 0, "wrong_tenant": 0,
                   "unresolved_sample": []}

    # Negative --limit is rejected as usage error.
    assert main(["seeds", "verify", "--run", run_id, "--from", str(feed_file), "--limit", "-1"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "usage"


def test_run_new_flags_cold_start_below_fifteen_eligible_seeds(capsys, tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: empty)
    profiles.install_example()
    add_seeds([seed(f"board{i}") for i in range(14)], profile_slug=profiles.EXAMPLE_SLUG)
    add_seeds([seed(f"other{i}") for i in range(5)], profile_slug="another-profile")
    assert main(["run", "new", "--profile", profiles.EXAMPLE_SLUG, "--preset", "fast"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["eligible_seeds"], out["cold_start"]) == (14, True)
    add_seeds([seed("board14")], profile_slug=profiles.EXAMPLE_SLUG)
    assert main(["run", "new", "--profile", profiles.EXAMPLE_SLUG, "--preset", "fast"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["eligible_seeds"], out["cold_start"]) == (15, False)


def _seed_lock():
    path = seeds.auto_seeds_path()
    return path.with_name(path.name + ".lock")


def test_concurrent_add_seeds_do_not_lose_each_others_rows(monkeypatch):
    real_read = seeds.store._read_raw_rows
    a_read, b_started = threading.Event(), threading.Event()

    def slow_read(path):
        rows = real_read(path)
        if threading.current_thread().name == "writer-a":
            a_read.set()
            b_started.wait(5)
            time.sleep(0.3)  # writer B reaches add_seeds (and, without a lock, finishes its write) meanwhile
        return rows

    monkeypatch.setattr(seeds.store, "_read_raw_rows", slow_read)
    a = threading.Thread(target=add_seeds, args=([seed("alpha")],), kwargs={"profile_slug": "p1"}, name="writer-a")
    a.start()
    assert a_read.wait(5)

    def writer_b():
        b_started.set()
        add_seeds([seed("beta")], profile_slug="p1")

    b = threading.Thread(target=writer_b, name="writer-b")
    b.start()
    a.join(10)
    b.join(10)
    assert {s.board_identifier for s in load_inventory("p1") if "p1" in s.profiles} == {"alpha", "beta"}


def test_a_leftover_lock_file_from_a_crashed_writer_does_not_block():
    lock = _seed_lock()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")  # the file exists, but no process holds the OS lock
    assert add_seeds([seed("gamma")], profile_slug="p1") == 1


def test_a_live_seed_lock_times_out_as_a_seed_file_error(monkeypatch):
    monkeypatch.setattr(seeds.store, "SEED_LOCK_TIMEOUT", 0.2)
    with seeds.store._seed_file_lock(seeds.auto_seeds_path()):  # another writer holds the lock
        with pytest.raises(seeds.SeedFileError):
            add_seeds([seed("delta")], profile_slug="p1")
        assert _seed_lock().exists()  # another writer's lock is left alone
    assert not seeds.auto_seeds_path().exists()
    assert add_seeds([seed("delta")], profile_slug="p1") == 1  # released: the next writer gets it


def test_a_later_verification_promotes_an_unconfirmed_stored_seed():
    guessed = seed("acme").model_copy(update={
        "source_type": seeds.UNCONFIRMED_SOURCE_TYPE, "tags": ["ownership:unknown"],
        "priority": seeds.UNCONFIRMED_SEED_PRIORITY,
    })
    assert add_seeds([guessed], profile_slug="p1") == 1
    verified = seed("acme").model_copy(update={"source_type": "agent_generated", "source_label": "Agent proposal"})
    assert add_seeds([verified], profile_slug="p2") == 0
    [stored] = [s for s in load_inventory("p2") if s.board_identifier == "acme" and s.ats == "greenhouse"]
    assert stored.source_type == "agent_generated" and "ownership:unknown" not in stored.tags
    assert stored.priority != seeds.UNCONFIRMED_SEED_PRIORITY
    assert set(stored.profiles) >= {"p1", "p2"}
    # An unconfirmed result never downgrades a stored verified seed.
    assert add_seeds([guessed], profile_slug="p3") == 0
    [again] = [s for s in load_inventory("p3") if s.board_identifier == "acme" and s.ats == "greenhouse"]
    assert again.source_type == "agent_generated" and "p3" in again.profiles


def test_budget_exhaustion_keeps_an_unconfirmed_board_already_found():
    def handler(request):
        if "greenhouse" in request.url.host and "/acmedatasystems/" in request.url.path:
            return httpx.Response(200, json=GH_JOBS)
        return httpx.Response(404)

    res = verify_proposal(Proposal("Acme Data Systems"), client=make_client(handler, max_requests=1), timeout=5)
    assert res.reason == "budget_exhausted"
    stored = seeds.stored_seed(res)
    assert stored is not None and stored.board_identifier == "acmedatasystems"
    assert stored.source_type == seeds.UNCONFIRMED_SOURCE_TYPE
    # Without a board found first, budget exhaustion still stores nothing.
    res = verify_proposal(Proposal("Other Data Systems"), client=make_client(handler, max_requests=1), timeout=5)
    assert res.reason == "budget_exhausted" and seeds.stored_seed(res) is None

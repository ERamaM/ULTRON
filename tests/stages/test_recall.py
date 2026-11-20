"""Recall fixes vs MARK III: curated seeds first, ATS-API / fingerprinted custom domains, direct ATS feed URLs."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml
from stage_helpers import CASES, CORPUS, PROFILE

from ultron import http_factory, profiles, runs, seeds
from ultron.core.decision.unit import characterize_job, load_allowed_domains
from ultron.core.models import AtsBoardSeed, CandidateUrl, ExtractedJob
from ultron.stages import discover as discover_stage
from ultron.stages.discover import discover_ats
from ultron.stages.feeds import _candidate as feed_candidate
from ultron.stages.feeds import _posting_like
from ultron.stages.fetch import fetch
from ultron.stages.filter import filter_run

FIX = Path(__file__).resolve().parents[1] / "fixtures"
GH_BOARD = (FIX / "ats" / "greenhouse_board.json").read_bytes()
LEVER_BOARD = (FIX / "ats" / "lever_board.json").read_bytes()
PAGE = (FIX / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")


@pytest.fixture
def pack_dir(tmp_path, monkeypatch):
    path = tmp_path / "packs"
    path.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: path)
    return path


def _write_pack(pack_dir: Path, rows: list[dict]) -> None:
    data = {"pack": {"id": "t", "usage": "active"}, "seeds": rows}
    (pack_dir / "t.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _pack_row(ats, board, priority, sectors=("technology",)):
    return {"company": board.title(), "ats": ats, "board_identifier": board, "priority": priority,
            "sectors": list(sectors)}


def _auto(ats, board, source_type, priority=1):
    return AtsBoardSeed(seed_id=f"{ats}:{board}", company=board.title(), ats=ats, board_identifier=board,
                        priority=priority, source_type=source_type, profiles=[profiles.EXAMPLE_SLUG])


def _setup(auto_seeds, overrides, log):
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "boards-api.greenhouse.io":
            log.append(("greenhouse", path.split("/")[3]))
            return httpx.Response(200, content=GH_BOARD)
        if host == "api.lever.co":
            log.append(("lever", path.split("/")[3]))
            return httpx.Response(200, content=LEVER_BOARD)
        return httpx.Response(404)

    profiles.install_example()
    if auto_seeds:
        seeds.add_seeds(auto_seeds, profile_slug=profiles.EXAMPLE_SLUG)
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    return runs.new_run(profiles.EXAMPLE_SLUG, "balanced", overrides)


def _deprioritize(monkeypatch, boards: list[str]) -> None:
    # Pack seeds with 5+ attempts and no keep go after every group, feed seeds included. These tests use that to
    # rank non-feed seeds after feed seeds (other-sector packs used to, but they are no longer queried at all).
    monkeypatch.setattr(discover_stage, "load_seed_yields", lambda _slug: {f"lever:{b}": (5, 0) for b in boards})


# --- seed selection -------------------------------------------------------------------------------


def test_feed_seeds_cannot_crowd_out_agent_and_pack_seeds(pack_dir):
    _write_pack(pack_dir, [_pack_row("lever", f"pack{i}", 10 + i) for i in range(3)])
    auto = [_auto("lever", f"feed{i}", "feed_discovered", priority=1) for i in range(6)]
    auto.append(_auto("lever", "agentco", "agent_generated", priority=100))
    log: list = []
    run_id = _setup(auto, {"max_seeds_per_run": 4}, log)
    out = discover_ats(run_id)
    assert [b for _, b in log] == ["agentco", "pack0", "pack1", "pack2"]
    groups = out["seed_groups"]
    assert groups["agent"]["queried"] == 1 and groups["pack"]["queried"] == 3
    assert groups["feed"]["queried"] == 0 and groups["feed"]["capped"] == 6


def test_feed_seeds_get_at_most_half_when_non_feed_seeds_exist(pack_dir, monkeypatch):
    # Deprioritized pack seeds rank after feed seeds, but the 50% reserve still applies to them.
    _write_pack(pack_dir, [_pack_row("lever", f"other{i}", 10 + i) for i in range(6)])
    _deprioritize(monkeypatch, [f"other{i}" for i in range(6)])
    auto = [_auto("lever", f"feed{i}", "feed_discovered", priority=1) for i in range(6)]
    log: list = []
    run_id = _setup(auto, {"max_seeds_per_run": 4}, log)
    out = discover_ats(run_id)
    boards = [b for _, b in log]
    assert len(boards) == 4
    assert sum(b.startswith("feed") for b in boards) == 2
    assert out["seed_groups"]["feed"]["queried"] == 2 and out["seed_groups"]["pack"]["queried"] == 2


def test_host_cap_follows_group_order_and_frees_slots(pack_dir):
    # Two greenhouse pack seeds come before three greenhouse feed seeds; the host cap of 2 is spent on
    # the pack seeds, and the capped feed seeds do not use up the seed slots (the lever seed still runs).
    _write_pack(pack_dir, [_pack_row("greenhouse", "packa", 50), _pack_row("greenhouse", "packb", 60),
                           _pack_row("lever", "packl", 70)])
    auto = [_auto("greenhouse", f"feed{i}", "feed_discovered", priority=1) for i in range(3)]
    log: list = []
    run_id = _setup(auto, {"max_seeds_per_run": 3, "max_requests_per_host": 2}, log)
    out = discover_ats(run_id)
    assert log == [("greenhouse", "packa"), ("greenhouse", "packb"), ("lever", "packl")]
    assert out["seeds_queried"] == 3 and out["seeds_skipped"] == 0
    assert out["seed_groups"]["feed"]["host_capped"] == 3


def test_feed_company_seeds_are_not_labelled_agent_generated(pack_dir, monkeypatch):
    from ultron.stages import unattended
    from ultron.stages.feeds import PROPOSALS_FILE

    log: list = []
    run_id = _setup([], None, log)
    runs.write_jsonl(runs.run_dir(run_id) / PROPOSALS_FILE, [{"company": "Nimbus", "board": "nimbus",
                                                              "ats": "greenhouse"}])
    assert unattended.verify_feed_companies(run_id)["verified"] == 1
    seed = next(s for s in seeds.load_inventory(profiles.EXAMPLE_SLUG) if s.board_identifier == "nimbus")
    assert seed.source_type == "feed_company"


# --- domain allowlist --------------------------------------------------------------------------------


def _job(case_id: str, url: str) -> dict:
    job = {**CORPUS["defaults"], **CASES[case_id]["job"]}
    job["apply_url"] = url
    job["domain"] = url.split("/")[2]
    return job


def _filter_rows(rows):
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    run = runs.load_run(run_id)
    run["profile_snapshot"] = PROFILE
    runs._write_run(run_id, run)
    runs.write_jsonl(runs.run_dir(run_id) / "postings.jsonl", rows)
    runs.mark_stage(run_id, "fetch")
    filter_run(run_id)
    discarded = {r["identity_key"]: r["reason"] for r in runs.read_jsonl(runs.run_dir(run_id) / "discarded.jsonl")}
    return discarded


def test_characterize_job_flag_defaults_to_allowlist():
    job = ExtractedJob(**_job("en-positive-shape-01", "https://www.example-corp.com/careers/1"))
    default = characterize_job(job, PROFILE, load_allowed_domains())
    assert default["pipeline_characterization"]["reason"] == "outside_ats_allowlist"
    allowed = characterize_job(job, PROFILE, load_allowed_domains(), allow_outside_ats_allowlist=True)
    assert allowed["pre_filter"]["keep"] and allowed["hard_filter"]["reason"] != "outside_ats_allowlist"


def test_ats_api_custom_domain_posting_is_kept():
    url = "https://www.example-corp.com/careers/1"
    discarded = _filter_rows([
        {"candidate": {"source_type": "ats_api"}, "job": _job("en-positive-shape-01", url), "identity_key": "a"},
    ])
    assert discarded.get("a") != "outside_ats_allowlist"


def test_web_search_custom_domain_kept_only_with_fingerprint():
    url = "https://www.example-corp.com/careers/1"
    discarded = _filter_rows([
        {"candidate": {"source_type": "web_search"}, "job": _job("en-positive-shape-01", url), "identity_key": "plain"},
        {"candidate": {"source_type": "web_search"}, "job": _job("en-positive-shape-01", url), "identity_key": "fp",
         "ats_fingerprint": True},
        {"candidate": {"source_type": "feed:remotive"}, "job": _job("en-positive-shape-01", url), "identity_key": "feed"},
    ])
    assert discarded["plain"] == "outside_ats_allowlist"
    assert discarded["feed"] == "outside_ats_allowlist"
    assert discarded.get("fp") != "outside_ats_allowlist"


def test_fetch_records_ats_fingerprint(pack_dir):
    custom = "<html><head><script>window.greenhouse = {}</script></head><body><h1>ML Engineer</h1></body></html>"
    plain = "<html><body><h1>ML Engineer</h1><p>Apply by email.</p></body></html>"

    def handler(request):
        return httpx.Response(200, text=custom if request.url.path.endswith("/gh") else plain)

    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    rows = []
    for name in ("gh", "plain"):
        url = f"https://www.example-corp.com/careers/{name}"
        rows.append(CandidateUrl(query_id="q", query="", source_url=url, normalized_url=url,
                                 domain="www.example-corp.com", source_type="web_search", source_key="web_search",
                                 ats_title="ML Engineer"))
    runs.write_jsonl(runs.run_dir(run_id) / "candidates.jsonl", rows)
    runs.mark_stage(run_id, "discover_ats")
    fetch(run_id)
    got = {r["job"]["apply_url"].rsplit("/", 1)[1]: r["ats_fingerprint"]
           for r in runs.read_jsonl(runs.run_dir(run_id) / "postings.jsonl")}
    assert got == {"gh": True, "plain": False}


# --- feeds --------------------------------------------------------------------------------------------


def test_feed_candidate_prefers_direct_ats_url():
    source = {"id": "remotive"}
    row = {"title": "Senior ML Engineer", "company_name": "Acme", "url": "https://remotive.com/remote-jobs/x-1",
           "description": "Apply at https://boards.greenhouse.io/acme/jobs/123 today"}
    candidate = feed_candidate(source, row, _posting_like(row))
    assert candidate is not None
    assert candidate.source_url == "https://boards.greenhouse.io/acme/jobs/123"
    assert candidate.source_type == "feed:remotive"


@pytest.mark.parametrize("url", [
    "https://remotive.com/remote-jobs/x-2",
    "https://www.arbeitnow.com/jobs/companies/acme/ml-engineer-1",
    "https://www.arbeitnow.co.uk/jobs/companies/acme/ml-engineer-2",
])
def test_aggregator_only_feed_row_is_not_a_candidate(url):
    row = {"title": "Senior ML Engineer", "company_name": "Acme", "url": url}
    assert feed_candidate({"id": "arbeitnow"}, row, _posting_like(row)) is None


def test_feed_row_on_company_site_is_still_a_candidate():
    row = {"title": "Senior ML Engineer", "company_name": "Acme", "url": "https://careers.acme.example/jobs/7"}
    candidate = feed_candidate({"id": "hn"}, row, _posting_like(row))
    assert candidate is not None and candidate.source_url == "https://careers.acme.example/jobs/7"


def test_fingerprint_never_overrides_the_allowlist_on_aggregator_hosts():
    discarded = _filter_rows([
        {"candidate": {"source_type": "feed:jobicy"}, "identity_key": "agg", "ats_fingerprint": True,
         "job": _job("en-positive-shape-01", "https://jobicy.com/jobs/123-senior-ml-engineer")},
        {"candidate": {"source_type": "web_search"}, "identity_key": "agg2", "ats_fingerprint": True,
         "job": _job("en-positive-shape-01", "https://www.arbeitnow.fr/jobs/companies/acme/ml")},
    ])
    assert discarded == {"agg": "outside_ats_allowlist", "agg2": "outside_ats_allowlist"}


def test_aggregator_page_linking_to_an_ats_is_not_fingerprint_allowed_in_replay():
    from ultron import diagnose

    page = '<html><h1>Senior ML Engineer</h1><a href="https://boards.greenhouse.io/acme/jobs/1">Apply</a></html>'
    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)),
        resolver=lambda h, p: ["93.184.216.34"], sleep=lambda s: None, min_interval_per_host=0.0,
    )
    result = diagnose.diagnose("https://remoteok.com/remote-jobs/1", profile_slug=profiles.EXAMPLE_SLUG)
    pre = next(t for t in result.trace if t["label"] == "offline_replay" and t["stage"] == "pre_filter")
    assert pre["reason"] == "outside_ats_allowlist"


def test_seeds_dropped_by_feed_cap_do_not_take_host_slots(pack_dir, monkeypatch):
    # Feed seeds rank before the deprioritized pack seed on the same host. The feed reserve leaves one slot
    # for the pack seed, and the feed seed it drops must not use the host's second (last) request slot.
    _write_pack(pack_dir, [_pack_row("lever", "offsector", 10)])
    _deprioritize(monkeypatch, ["offsector"])
    auto = [_auto("lever", f"feed{i}", "feed_discovered", priority=1) for i in range(3)]
    log: list = []
    run_id = _setup(auto, {"max_seeds_per_run": 2, "max_requests_per_host": 2}, log)
    out = discover_ats(run_id)
    assert [b for _, b in log] == ["feed0", "offsector"]
    assert out["seed_groups"]["pack"]["queried"] == 1 and out["seed_groups"]["pack"]["host_capped"] == 0


@pytest.mark.parametrize(("host", "expected"), [
    ("remote.workable.com", False),  # a tenant on a known ATS is never an aggregator
    ("remotive.acme.io", False),  # the middle label must be a country second-level domain
    ("remote.acme.de", False),  # generic names never match across TLDs
    ("remote.de", False),
    ("arbeitnow.co.uk", True),
    ("www.arbeitnow.co.uk", True),
    ("arbeitnow.com:443", True),
    ("user@www.arbeitnow.com:8443", True),
    ("remotive.io", True),
    ("jobs.remote.co", True),
    ("remote.co", True),
])
def test_is_aggregator_host_edges(host, expected):
    from ultron.stages.feeds import is_aggregator_host

    assert is_aggregator_host(host) is expected


def test_eu_pack_seeds_are_left_out_for_non_eu_profile(pack_dir):
    latam = yaml.safe_load((FIX / "profiles" / "backend-latam.yaml").read_text(encoding="utf-8"))
    slug = latam["slug"]
    _write_pack(
        pack_dir,
        [{**_pack_row("lever", f"eupack{i}", 10 + i), "country_scope": "EU"} for i in range(4)],
    )
    auto = [
        AtsBoardSeed(
            seed_id=f"lever:feed{i}", company=f"Feed{i}", ats="lever", board_identifier=f"feed{i}",
            priority=1, source_type="feed_discovered", profiles=[slug],
        )
        for i in range(3)
    ]
    log: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.lever.co":
            log.append(("lever", request.url.path.split("/")[3]))
            return httpx.Response(200, content=LEVER_BOARD)
        return httpx.Response(404)

    (profiles._user_dir() / f"{slug}.yaml").parent.mkdir(parents=True, exist_ok=True)
    (profiles._user_dir() / f"{slug}.yaml").write_text(yaml.safe_dump(latam), encoding="utf-8")
    seeds.add_seeds(auto, profile_slug=slug)
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    run_id = runs.new_run(slug, "balanced", {"max_seeds_per_run": 6, "max_requests_per_host": 3})
    out = discover_ats(run_id)
    boards = [b for _, b in log]
    # EU-scoped pack seeds do not fit a geo="other" profile: never queried, not even as filler (session 3).
    assert boards == ["feed0", "feed1", "feed2"]
    assert out["seed_groups"]["feed"]["queried"] == 3
    assert out["seed_groups"]["other"]["host_capped"] == out["seed_groups"]["other"]["queried"] == 0
    assert out["seed_groups"]["pack"]["queried"] == 0

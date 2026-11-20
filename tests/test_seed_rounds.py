"""Seed-generation rounds: ``run new`` coverage, ``seeds plan`` and ``seeds verify --round``."""

from __future__ import annotations

import json
from typing import get_args

import httpx
import pytest

from ultron import http_factory, profiles, runs, seed_rounds, seeds
from ultron.cli import main
from ultron.core.ats.vendors import AtsVendorName

JOBS = {"jobs": [
    {"id": i, "title": f"Role {i}", "absolute_url": f"https://boards.greenhouse.io/x/jobs/{i}",
     "location": {"name": "Madrid"}, "updated_at": "2026-01-01T00:00:00Z"} for i in (1, 2)
]}


@pytest.fixture
def no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: empty)


@pytest.fixture
def requests_log(no_packs):
    """A fast run of the example profile with no fitting seed; Greenhouse boards named ``ok*`` exist."""
    log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(str(request.url))
        board = request.url.path.split("/")[3] if request.url.path.count("/") >= 3 else ""
        if "greenhouse" in request.url.host and board.startswith("ok"):
            return httpx.Response(200, json=JOBS)
        return httpx.Response(404)

    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    profiles.install_example()
    return log


def _new_run(capsys) -> dict:
    assert main(["run", "new", "--profile", profiles.EXAMPLE_SLUG, "--preset", "fast"]) == 0
    return json.loads(capsys.readouterr().out)


def _cli(capsys, *argv) -> tuple[int, dict]:
    code = main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _write(tmp_path, name: str, rows: list[dict]) -> str:
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(path)


def _rows(prefix: str, ok: int, missing: int = 0) -> list[dict]:
    rows = [{"company": f"{prefix} Ok {i}", "ats": "greenhouse", "board": f"ok{prefix.lower()}{i}"} for i in range(ok)]
    rows += [{"company": f"{prefix} Gone {i}", "ats": "greenhouse", "board": f"gone{prefix.lower()}{i}"}
             for i in range(missing)]
    return rows


def test_run_new_reports_the_seed_deficit_of_a_profile_without_a_fitting_pack(requests_log, capsys):
    out = _new_run(capsys)
    assert (out["seed_target"], out["fitting_seeds"], out["seed_deficit"]) == (35, 0, 35)


def test_a_profile_covered_by_the_bundled_pack_has_no_deficit(capsys):
    profiles.install_example()
    out = _new_run(capsys)
    assert out["seed_deficit"] == 0 and out["fitting_seeds"] >= out["seed_target"] == 35


def test_plan_asks_for_the_deficit_over_the_prior_yield_capped_at_the_target(requests_log, capsys):
    run_id = _new_run(capsys)["run_id"]
    code, out = _cli(capsys, "seeds", "plan", "--run", run_id)
    assert code == 0
    assert out["round"] == 1 and out["max_rounds"] == 2 and out["more_rounds"] is True
    assert out["seed_deficit"] == 35 and out["ask"] == 35  # ceil(35 / 0.5) = 70, capped at the target
    assert out["proposals_file"] == "agent_seed_round_1.jsonl"
    assert out["schema"] == seed_rounds.PROPOSAL_SCHEMA
    assert requests_log == []  # planning makes no request


@pytest.mark.parametrize("bad", [
    {"company": "Domain Only", "domain": "example.org"},  # a round must name the board
    {"company": "Extra", "ats": "greenhouse", "board": "okx", "industry": "health"},
    {"company": "Bad Vendor", "ats": "notanats", "board": "okx"},
    {"company": "", "ats": "greenhouse", "board": "okx"},
    {"company": "Bad URL", "careers_url": "ftp://example.org/jobs"},
    {"company": 7, "ats": "greenhouse", "board": "okx"},
])
def test_verify_round_rejects_the_whole_file_before_any_request(requests_log, capsys, tmp_path, bad):
    run_id = _new_run(capsys)["run_id"]
    src = _write(tmp_path, "agent_seed_round_1.jsonl", [*_rows("A", 2), bad])
    code, out = _cli(capsys, "seeds", "verify", "--run", run_id, "--from", src, "--round")
    assert code == 2 and out["error"] == "invalid_proposals" and "line 3" in out["detail"]
    assert requests_log == []
    assert seed_rounds.load_rounds(run_id) == []  # no round consumed


def test_verify_round_records_progress_and_the_next_ask(requests_log, capsys, tmp_path):
    run_id = _new_run(capsys)["run_id"]
    src = _write(tmp_path, "agent_seed_round_1.jsonl", _rows("A", 8, missing=2))
    code, out = _cli(capsys, "seeds", "verify", "--run", run_id, "--from", src, "--round")
    assert code == 0 and out["verified"] == 8
    info = out["seed_round"]
    assert info["round"] == 1 and info["fitting_seeds"] == 8 and info["seed_deficit"] == 27
    assert info["more_rounds"] is True and info["status"] == "open"
    assert info["next_ask"] == 34  # ceil(27 / 0.8)
    (record,) = seed_rounds.load_rounds(run_id)
    assert (record["checked"], record["verified"], record["fitting_before"], record["fitting_after"]) == (10, 8, 0, 8)
    assert "greenhouse:gonea0" in record["boards"]
    code, out = _cli(capsys, "seeds", "plan", "--run", run_id)
    assert out["round"] == 2 and "A Gone 0" in out["exclude_companies"]


def test_companies_and_boards_tried_before_are_skipped_without_requests(requests_log, capsys, tmp_path):
    run_id = _new_run(capsys)["run_id"]
    first = _rows("A", 3, missing=1)
    _cli(capsys, "seeds", "verify", "--run", run_id, "--from",
         _write(tmp_path, "agent_seed_round_1.jsonl", first), "--round")
    before = len(requests_log)
    repeat = [first[0], {"company": "Renamed", "ats": "greenhouse", "board": first[3]["board"]}, *_rows("B", 2)]
    code, out = _cli(capsys, "seeds", "verify", "--run", run_id, "--from",
                     _write(tmp_path, "agent_seed_round_2.jsonl", repeat), "--round")
    assert code == 0 and out["verified"] == 2 and out["seed_round"]["repeats_skipped"] == 2
    assert all("oka0" not in url and "gonea0" not in url for url in requests_log[before:])
    assert seed_rounds.load_rounds(run_id)[-1]["checked"] == 2  # repeats stay out of the yield


def test_rounds_stop_at_the_preset_maximum(requests_log, capsys, tmp_path):
    run_id = _new_run(capsys)["run_id"]
    for n, prefix in ((1, "A"), (2, "B")):
        _cli(capsys, "seeds", "verify", "--run", run_id, "--from",
             _write(tmp_path, f"agent_seed_round_{n}.jsonl", _rows(prefix, 2)), "--round")
    code, out = _cli(capsys, "seeds", "plan", "--run", run_id)
    assert out["status"] == "max_rounds" and out["more_rounds"] is False and out["ask"] == 0
    assert "schema" not in out
    code, out = _cli(capsys, "seeds", "verify", "--run", run_id, "--from",
                     _write(tmp_path, "agent_seed_round_3.jsonl", _rows("C", 2)), "--round")
    assert code == 2 and out["error"] == "invalid_proposals" and "max_rounds" in out["detail"]


def test_a_round_without_a_new_fitting_seed_ends_the_rounds(requests_log, capsys, tmp_path):
    run_id = _new_run(capsys)["run_id"]
    code, out = _cli(capsys, "seeds", "verify", "--run", run_id, "--from",
                     _write(tmp_path, "agent_seed_round_1.jsonl", _rows("A", 0, missing=3)), "--round")
    assert code == 0 and out["seed_round"]["status"] == "no_progress" and out["seed_round"]["more_rounds"] is False


def test_a_blocked_host_ends_the_rounds():
    run = {"preset": "fast", "settings": {}}
    record = {"fitting_before": 0, "fitting_after": 4, "host_blocked": 2}
    assert seed_rounds.round_status(run, [record], fitting=4) == "host_blocked"
    assert seed_rounds.round_status(run, [{**record, "host_blocked": 0}], fitting=4) == "open"
    assert seed_rounds.round_status(run, [], fitting=35) == "target_met"


def test_the_yield_ignores_unchecked_proposals_and_is_clamped():
    # Session 2's S5 cold start: 3 verified of 52 proposals, 33 of them never checked (budget).
    assert seed_rounds.observed_yield([{"checked": 19, "fitting_before": 0, "fitting_after": 3}]) == 0.25
    assert seed_rounds.observed_yield([{"checked": 3, "fitting_before": 0, "fitting_after": 0}]) == 0.5  # prior
    assert seed_rounds.observed_yield([{"checked": 10, "fitting_before": 0, "fitting_after": 9}]) == 0.9


def test_runs_created_before_the_round_settings_use_the_preset_values():
    assert seed_rounds.run_settings({"preset": "balanced", "settings": {"max_pages": 5}})["seed_target"] == 75
    assert seed_rounds.run_settings({"preset": "balanced", "settings": {"seed_target": 7}})["seed_target"] == 7


def test_the_validator_follows_the_published_schema():
    schema = seed_rounds.PROPOSAL_SCHEMA
    assert schema["properties"]["ats"]["enum"] == sorted(get_args(AtsVendorName))
    good = {"company": "Acme", "ats": "workday", "board": "acme.wd3.myworkdayjobs.com/External", "note": "x"}
    assert seed_rounds.validate_proposal(good) == []
    assert seed_rounds.validate_proposal({"company": "Acme", "careers_url": "https://acme.example/careers"}) == []
    # Every rule the schema states is enforced.
    assert seed_rounds.validate_proposal({"ats": "lever", "board": "acme"}) == ["missing 'company'"]
    assert seed_rounds.validate_proposal({"company": "A" * 201, "careers_url": "https://a.example"})
    assert seed_rounds.validate_proposal({"company": "Acme", "board": "acme"}) == [
        "needs 'ats' + 'board' or 'careers_url'"]
    assert seed_rounds.validate_proposal(["not", "an", "object"]) == ["not a JSON object"]


def test_unattended_runs_report_the_seed_coverage(requests_log):
    from ultron.stages.unattended import run_unattended

    out = run_unattended(profiles.EXAMPLE_SLUG, "fast")
    assert out["stages"]["seed_coverage"] == {"seed_target": 35, "fitting_seeds": 0, "seed_deficit": 35}
    assert runs.load_run(out["run_id"])["profile_slug"] == profiles.EXAMPLE_SLUG


def test_a_failed_company_may_come_back_with_another_board(requests_log, capsys, tmp_path):
    run_id = _new_run(capsys)["run_id"]
    _cli(capsys, "seeds", "verify", "--run", run_id, "--from",
         _write(tmp_path, "agent_seed_round_1.jsonl", [*_rows("A", 1), *_rows("B", 0, missing=1)]), "--round")
    retry = [{"company": "B Gone 0", "ats": "greenhouse", "board": "okretry"}]
    code, out = _cli(capsys, "seeds", "verify", "--run", run_id, "--from",
                     _write(tmp_path, "agent_seed_round_2.jsonl", retry), "--round")
    assert code == 0 and out["verified"] == 1 and out["seed_round"]["repeats_skipped"] == 0


def test_unchecked_proposals_are_not_marked_as_tried():
    rows = [{"company": "Late", "ats": "lever", "board": "late"}, {"company": "Done", "careers_url": "https://x.example/"}]
    results = [
        seeds.VerifyResult(seeds.Proposal(company="Late"), "unresolved", None, 0, "budget_exhausted"),
        seeds.VerifyResult(seeds.Proposal(company="Done"), "unresolved", None, 0, "no_board_found"),
    ]
    record = seed_rounds.round_record(number=1, received=2, repeats=[], rows=rows, results=results,
                                      fitting_before=0, fitting_after=0)
    assert record["boards"] == ["url:https://x.example"]
    assert (record["checked"], record["budget_exhausted"]) == (1, 1)


def test_a_round_with_no_new_proposal_is_refused_without_using_a_round(requests_log, capsys, tmp_path):
    run_id = _new_run(capsys)["run_id"]
    first = _rows("A", 1)
    _cli(capsys, "seeds", "verify", "--run", run_id, "--from",
         _write(tmp_path, "agent_seed_round_1.jsonl", first), "--round")
    for rows in ([], first):
        code, out = _cli(capsys, "seeds", "verify", "--run", run_id, "--from",
                         _write(tmp_path, "agent_seed_round_2.jsonl", rows), "--round")
        assert code == 2 and out["error"] == "invalid_proposals" and "no new proposal" in out["detail"]
    assert len(seed_rounds.load_rounds(run_id)) == 1


def test_a_round_left_unchecked_by_the_budget_does_not_end_the_rounds():
    run = {"preset": "fast", "settings": {}}
    record = {"checked": 0, "budget_exhausted": 5, "fitting_before": 3, "fitting_after": 3}
    assert seed_rounds.round_status(run, [record], fitting=3) == "open"


def test_fitting_pack_seeds_count_as_already_tried(tmp_path, monkeypatch, capsys):
    import yaml

    packs = tmp_path / "packs"
    packs.mkdir()
    rows = [{"company": f"Pack {i}", "ats": "greenhouse", "board_identifier": f"pack{i}", "country_scope": "EU",
             "sectors": ["technology"]} for i in range(3)]
    rows.append({"company": "Health Co", "ats": "greenhouse", "board_identifier": "healthco", "country_scope": "EU",
                 "sectors": ["pharma_health"]})
    (packs / "p.yaml").write_text(yaml.safe_dump({"pack": {"id": "p", "usage": "active"}, "seeds": rows}),
                                  encoding="utf-8")
    monkeypatch.setattr(seeds, "packs_dir", lambda: packs)
    profiles.install_example()
    run = runs.load_run(_new_run(capsys)["run_id"])
    _companies, boards, names = seed_rounds.tried(run, [])
    assert {"greenhouse:pack0", "greenhouse:pack2"} <= boards and "greenhouse:healthco" not in boards
    assert "Pack 1" in names and "Health Co" not in names


def test_feed_seeds_do_not_count_toward_the_target(requests_log, capsys):
    # S6 fast (session 3): company-list feeds linked 56 technology boards to a US nursing profile, and
    # fitting_seeds read 75 although only the 30 pack seeds fit; the deficit would have hidden itself.
    from ultron.core.models import AtsBoardSeed

    feed = [AtsBoardSeed(seed_id=f"lever:feed{i}", company=f"Feed {i}", ats="lever", board_identifier=f"feed{i}",
                         source_type=kind) for i, kind in enumerate(["feed_discovered", "feed_company"] * 2)]
    agent = [AtsBoardSeed(seed_id="ashby:agentco", company="Agent Co", ats="ashby", board_identifier="agentco",
                          source_type="agent_generated", verification={"postings": 3})]
    seeds.add_seeds([*feed, *agent], profile_slug=profiles.EXAMPLE_SLUG)
    out = _new_run(capsys)
    assert (out["fitting_seeds"], out["seed_deficit"]) == (1, 34)

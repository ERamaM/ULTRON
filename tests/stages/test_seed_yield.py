"""Seed selection learns from each seed's past yield per profile (attempts and keeps in the ledger)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import pytest
import yaml
from stage_helpers import make_run

from ultron import home, http_factory, ledger, profiles, runs, seeds
from ultron.cli import main
from ultron.core.models import AtsBoardSeed
from ultron.stages.discover import SEED_ATTEMPTS_FILE, SeedYield, _select_seeds, discover_ats

FIX = Path(__file__).resolve().parents[1] / "fixtures"
SLUG = profiles.EXAMPLE_SLUG


@pytest.fixture
def pack_dir(tmp_path, monkeypatch):
    path = tmp_path / "packs"
    path.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: path)
    profiles.install_example()
    return path


def _write_pack(pack_dir: Path, boards: list[str], ats: str = "lever") -> None:
    rows = [
        {"company": b.title(), "ats": ats, "board_identifier": b, "priority": 10 + i, "sectors": ["technology"]}
        for i, b in enumerate(boards)
    ]
    (pack_dir / "t.yaml").write_text(
        yaml.safe_dump({"pack": {"id": "t", "usage": "active"}, "seeds": rows}), encoding="utf-8"
    )


def _boards(selected) -> list[str]:
    return [seed.board_identifier for seed, _group in selected]


def _select(limit, yields, per_host=50):
    return _select_seeds(SLUG, ["technology"], limit, per_host, yields=yields)


# --- the yield model ---------------------------------------------------------------------------------


def test_seed_yield_tiers():
    assert not SeedYield(2, 2).measured
    assert SeedYield(3, 0).measured and not SeedYield(4, 0).deprioritized
    assert SeedYield(5, 0).deprioritized and not SeedYield(5, 1).deprioritized
    assert SeedYield(4, 3).rank() < SeedYield(4, 1).rank() < SeedYield(0, 0).rank() < SeedYield(3, 0).rank()


# --- ranking -----------------------------------------------------------------------------------------


def test_no_history_keeps_static_priority_order(pack_dir):
    _write_pack(pack_dir, ["a", "b", "c", "d"])
    selected, _counts, stats = _select(10, {})
    assert _boards(selected) == ["a", "b", "c", "d"]
    assert stats == {"deprioritized": 0, "exploration_reserved": 0}


def test_seeds_with_keeps_rank_by_keep_ratio(pack_dir):
    _write_pack(pack_dir, ["a", "b", "c", "d"])
    yields = {"lever:c": (4, 3), "lever:d": (4, 1), "lever:b": (3, 0)}
    selected, _counts, _stats = _select(10, yields)
    # c (75%) > d (25%) > a (unmeasured) > b (measured, no keeps yet).
    assert _boards(selected) == ["c", "d", "a", "b"]


def test_zero_keep_seeds_are_deprioritized_not_removed(pack_dir):
    _write_pack(pack_dir, ["dead", "b", "c"])
    agent = AtsBoardSeed(seed_id="greenhouse:agentdead", company="Agentdead", ats="greenhouse",
                         board_identifier="agentdead", source_type="agent_generated", profiles=[SLUG])
    seeds.add_seeds([agent], profile_slug=SLUG)
    yields = {"lever:dead": (6, 0), "greenhouse:agentdead": (5, 0)}
    selected, counts, stats = _select(10, yields)
    # Deprioritized seeds go after every other seed, even an agent seed's group lead.
    assert _boards(selected) == ["b", "c", "agentdead", "dead"]
    assert stats["deprioritized"] == 2
    selected, counts, _stats = _select(2, yields)
    assert _boards(selected) == ["b", "c"]
    assert counts["agent"]["capped"] == 1 and counts["pack"]["capped"] == 1


def test_exploration_reserve_keeps_a_slot_for_unmeasured_seeds(pack_dir):
    boards = [f"m{i}" for i in range(12)] + ["new1", "new2"]
    _write_pack(pack_dir, boards)
    yields = {f"lever:m{i}": (10, 5) for i in range(12)}
    selected, counts, stats = _select(10, yields)
    names = _boards(selected)
    assert len(names) == 10
    assert names[:9] == [f"m{i}" for i in range(9)]
    assert names[9] == "new1"  # ceil(10% of 10) = 1 reserved slot, filled in ranked order
    assert stats["exploration_reserved"] == 1
    assert counts["pack"]["capped"] == 4


def test_exploration_reserve_above_one_keeps_the_best_ranked_unmeasured_seeds(pack_dir):
    _write_pack(pack_dir, [f"k{i:02d}" for i in range(19)] + ["u0", "u1", "u2"])
    yields = {f"lever:k{i:02d}": (5, 5) for i in range(19)}
    selected, _counts, stats = _select(20, yields)
    # ceil(10% of 20) = 2 reserved slots: the plain order already gives one to u0, the reserve adds u1.
    assert _boards(selected) == [f"k{i:02d}" for i in range(18)] + ["u0", "u1"]
    assert stats["exploration_reserved"] == 1


def test_exploration_reserve_is_at_least_one_and_deterministic(pack_dir):
    _write_pack(pack_dir, ["m0", "m1", "m2", "fresh"])
    yields = {"lever:m0": (5, 5), "lever:m1": (5, 4), "lever:m2": (5, 3)}
    first = _boards(_select(3, yields)[0])
    assert first == ["m0", "m1", "fresh"]
    assert _boards(_select(3, yields)[0]) == first


def test_exploration_reserve_respects_host_cap(pack_dir):
    _write_pack(pack_dir, ["m0", "m1", "m2", "fresh"])
    yields = {"lever:m0": (5, 5), "lever:m1": (5, 4), "lever:m2": (5, 3)}
    selected, counts, stats = _select(3, yields, per_host=2)
    assert _boards(selected) == ["m0", "fresh"]
    assert stats["exploration_reserved"] == 1
    assert counts["pack"]["host_capped"] == 2


def test_no_reserve_when_everything_fits(pack_dir):
    _write_pack(pack_dir, ["m0", "fresh"])
    selected, _counts, stats = _select(5, {"lever:m0": (5, 5)})
    assert _boards(selected) == ["m0", "fresh"]
    assert stats["exploration_reserved"] == 0


# --- discover writes the attempted seeds -------------------------------------------------------------


def test_discover_records_attempted_seeds_and_reads_yields(pack_dir):
    board = (FIX / "ats" / "lever_board.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.lever.co" and request.url.path.split("/")[3] != "ghost":
            return httpx.Response(200, content=board)
        return httpx.Response(404)

    _write_pack(pack_dir, ["good", "ghost", "dead"])
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    with closing(ledger.connect()) as conn, conn:
        conn.executemany(
            "INSERT INTO seed_yield_runs (profile_slug, seed_key, run_id, kept) VALUES (?, 'lever:dead', ?, 0)",
            [(SLUG, f"old{i}") for i in range(5)],
        )
    run_id = runs.new_run(SLUG, "balanced")
    out = discover_ats(run_id)
    assert out["seeds_deprioritized"] == 1 and out["seeds_exploration_reserved"] == 0
    rows = runs.read_jsonl(runs.run_dir(run_id) / SEED_ATTEMPTS_FILE)
    # A failing board is an attempt too; query order follows the yield ranking.
    assert [r["seed_key"] for r in rows] == ["lever:good", "lever:ghost", "lever:dead"]


def test_discover_without_a_ledger_does_not_create_one(pack_dir):
    _write_pack(pack_dir, ["good"])
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    run_id = runs.new_run(SLUG, "balanced")
    discover_ats(run_id)
    assert not (home.ultron_home() / "ultron.sqlite3").exists()


# --- finalize updates the counts ---------------------------------------------------------------------


KEEP = ["en-positive-shape-01", "en-positive-shape-02"]


def _ats_run(capsys, attempts: list[str] | None):
    run_id = make_run(KEEP)
    d = runs.run_dir(run_id)
    rows = runs.read_jsonl(d / "postings.jsonl")
    for row in rows:
        url = row["job"]["apply_url"]
        row["candidate"] = {
            "query_id": "greenhouse:Example", "query": "", "source_url": url, "normalized_url": url,
            "domain": "boards.greenhouse.io", "source_type": "ats_api", "source_key": "greenhouse:Example",
        }
    runs.write_jsonl(d / "postings.jsonl", rows)
    if attempts is not None:
        runs.write_jsonl(d / SEED_ATTEMPTS_FILE, [{"seed_key": key} for key in attempts])
    assert main(["filter", "--run", run_id]) == 0
    capsys.readouterr()
    return run_id


def _yields():
    with closing(ledger.connect()) as conn:
        return ledger.seed_yields(conn, SLUG)


def test_finalize_records_attempts_and_keeps_idempotently(capsys):
    run_id = _ats_run(capsys, ["greenhouse:example", "lever:ghost"])
    assert main(["finalize", "--run", run_id]) == 0
    roles = json.loads(capsys.readouterr().out)["roles"]
    assert roles >= 1
    assert _yields() == {"greenhouse:example": (1, 1), "lever:ghost": (1, 0)}
    assert main(["finalize", "--run", run_id]) == 0  # re-finalizing never double-counts
    assert _yields() == {"greenhouse:example": (1, 1), "lever:ghost": (1, 0)}
    second = _ats_run(capsys, ["lever:ghost"])
    assert main(["finalize", "--run", second]) == 0
    assert _yields() == {"greenhouse:example": (1, 1), "lever:ghost": (2, 0)}
    with closing(ledger.connect()) as conn:
        assert ledger.seed_yields(conn, "another-profile") == {}


def test_finalize_without_attempts_file_leaves_yields_alone(capsys):
    run_id = _ats_run(capsys, None)
    assert main(["finalize", "--run", run_id]) == 0
    assert _yields() == {}


def test_backup_and_restore_keep_seed_yields(capsys):
    from ultron import maintenance

    run_id = _ats_run(capsys, ["lever:ghost"])
    assert main(["finalize", "--run", run_id]) == 0
    target = maintenance.backup()
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT seed_key FROM seed_yield_runs").fetchall() == [("lever:ghost",)]
    with closing(ledger.connect()) as conn, conn:
        conn.execute("DELETE FROM seed_yield_runs")
    maintenance.restore(target, yes=True)
    assert _yields() == {"lever:ghost": (1, 0)}

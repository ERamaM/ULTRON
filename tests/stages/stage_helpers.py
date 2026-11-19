from __future__ import annotations

import json
from pathlib import Path

import yaml

from ultron import profiles, runs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS = json.loads((FIXTURES / "heuristic_characterization_corpus.json").read_text(encoding="utf-8"))
GOLDEN = {c["id"]: c for c in json.loads((FIXTURES / "heuristic_characterization_golden.json").read_text(encoding="utf-8"))["cases"]}
PROFILE = yaml.safe_load((FIXTURES / "profiles" / "ai-ml-remote-eu.yaml").read_text(encoding="utf-8"))
CASES = {c["id"]: c for c in CORPUS["cases"]}


def make_run(case_ids):
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    run = runs.load_run(run_id)
    run["profile_snapshot"] = PROFILE
    runs._write_run(run_id, run)
    rows = []
    for cid in case_ids:
        job = {**CORPUS["defaults"], **CASES[cid]["job"]}
        job.setdefault("apply_url", f"https://boards.greenhouse.io/example/jobs/{cid}")
        rows.append({"candidate": {}, "job": job, "identity_key": f"id:{cid}"})
    runs.write_jsonl(runs.run_dir(run_id) / "postings.jsonl", rows)
    runs.mark_stage(run_id, "fetch")
    return run_id

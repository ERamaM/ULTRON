"""Measure the 60-case heuristic corpus against its synthetic relevance labels.

Usage (from the repository root)::

    uv run python tools/measure_quality.py [--output quality.json]

Each case of ``tests/fixtures/heuristic_characterization_corpus.json`` runs through
``characterize_job`` with the bundled ``ai-ml-remote-eu`` profile, exactly as the ``filter`` stage
calls it. The decision outcome (keep, review or discard) is counted per label from
``tests/fixtures/algorithm_quality_labels.json`` together with the match-score band.

The labels are synthetic and human-owned: this tool never writes them. ``tests/eval/test_measure_quality.py``
pins the current counts; see ``docs/self-improvement.md`` for how to update that pin on purpose.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from safe_paths import UnsafePathError, write_report

from ultron.core.decision.unit import characterize_job, load_allowed_domains
from ultron.core.models import ExtractedJob
from ultron.core.ranker.match_score import SCORE_VERSION, compute_match_score
from ultron.core.ranker.profile_terms import collect_profile_term_hits
from ultron.core.rules.geography import geo_scope, location_scope
from ultron.core.rules.role_matching import role_negatives_for
from ultron.core.vocabulary import active_pack

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CORPUS_PATH = FIXTURES / "heuristic_characterization_corpus.json"
LABELS_PATH = FIXTURES / "algorithm_quality_labels.json"
PROFILE_PATH = FIXTURES / "profiles" / "ai-ml-remote-eu.yaml"
LABELS = ("relevant", "not_relevant", "ambiguous")
OUTCOMES = ("kept", "unsure", "discarded")
BANDS = ("strong", "good", "borderline", "ineligible")
_OUTCOME = {"keep": "kept", "review": "unsure", "discard": "discarded"}


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def measure() -> dict[str, Any]:
    corpus = _read(CORPUS_PATH)
    labels = _read(LABELS_PATH)["labels"]
    profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
    criteria = profile["criteria"]
    allowed = load_allowed_domains()
    pack, geo, negatives = active_pack(profile), geo_scope(profile), role_negatives_for(profile)
    target_regions = location_scope(profile)["preferred"]
    counts = {label: dict.fromkeys(OUTCOMES, 0) for label in LABELS}
    bands = {label: dict.fromkeys(BANDS, 0) for label in LABELS}
    cases = []
    for case in sorted(corpus["cases"], key=lambda item: item["id"]):
        payload = {**corpus["defaults"], **case["job"]}
        payload.setdefault("apply_url", f"https://boards.greenhouse.io/example/jobs/{case['id']}")
        job = ExtractedJob(**payload)
        result = characterize_job(
            job, profile, allowed, vocabulary=pack, geo=geo, role_negatives=negatives, target_regions=target_regions
        )
        decision = result["pipeline_characterization"]
        outcome = _OUTCOME[decision["outcome"]]
        score = compute_match_score(
            result["_evaluation"], criteria, source=job, term_hits=collect_profile_term_hits(job, criteria)
        )
        label = labels[case["id"]]["label"]
        counts[label][outcome] += 1
        bands[label][score.band] += 1
        cases.append({
            "id": case["id"],
            "label": label,
            "outcome": outcome,
            "stage": decision["stage"],
            "reason": decision["reason"] or "",
            "match_score": score.value,
            "match_band": score.band,
        })
    relevant, negative = counts["relevant"], counts["not_relevant"]
    return {
        "schema_version": 1,
        "profile_slug": profile["slug"],
        "score_version": SCORE_VERSION,
        "counts": counts,
        "relevant_kept_or_unsure_rate": (relevant["kept"] + relevant["unsure"]) / (sum(relevant.values()) or 1),
        "not_relevant_kept_rate": negative["kept"] / (sum(negative.values()) or 1),
        "score_band_by_label": bands,
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure the 60-case corpus against its synthetic labels.")
    parser.add_argument("--output", help="report file name, written into the reports folder (see safe_paths.py)")
    args = parser.parse_args(argv)
    rendered = json.dumps(measure(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if not args.output:
        sys.stdout.write(rendered)
        return 0
    try:
        path = write_report(args.output, rendered)
    except (UnsafePathError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps({"report": str(path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

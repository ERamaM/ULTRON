"""Labelled quality benchmark: deterministic offline replay, metrics and baseline gates.

Usage (from the repository root)::

    uv run python tools/quality_benchmark.py validate
    uv run python tools/quality_benchmark.py run --output quality-candidate.json
    uv run python tools/quality_benchmark.py compare tests/fixtures/benchmark/baseline.json [quality-candidate.json]

``compare`` replays the corpus itself when no candidate report is given. Exit codes: ``0`` all gates
passed, ``1`` regression, ``2`` invalid corpus or report.

The corpus (``tests/fixtures/benchmark``) is synthetic, human-owned ground truth. This tool reads it
and never writes to it; it does not use the network, an agent, the clock or the user's ULTRON_HOME.
See ``docs/self-improvement.md`` for the mapping from the corpus to ULTRON's pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_harness import final_status, losing_stage, replay
from safe_paths import UnsafePathError, contained_path

from ultron import presets

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "tests" / "fixtures" / "benchmark"
PROFILE_PATH = ROOT / "tests" / "fixtures" / "profiles" / "ai-ml-remote-eu.yaml"
BASELINE_PATH = CORPUS_DIR / "baseline.json"
REPORT_SCHEMA_VERSION = 1
PRESET = "balanced"
GLOBAL_REGRESSION_TOLERANCE = 0.01
SLICE_REGRESSION_TOLERANCE = 0.05
MIN_SLICE_SAMPLE = 10
GATED_METRICS = ("precision", "recall", "f1")
# Report slice name -> case field. "region" is the corpus remote_mode (worldwide, eu, us_only, ...).
SLICES = {"language": "language", "region": "remote_mode", "vendor": "vendor", "role_track": "role_track"}

CASE_KEYS = {
    "schema_version", "id", "profile", "language", "role_track", "seniority", "remote_mode",
    "eligibility_label", "vendor", "fixture", "split", "labels", "expected_stage", "notes",
}
LABEL_KEYS = {"relevant", "role_fit", "seniority_fit", "remote_fit", "eligibility", "open_status"}
FIXTURE_KEYS = {
    "schema_version", "synthetic", "source_kind", "vendor", "language", "title", "location",
    "remote_text", "body_text", "apply_url", "is_open_hint",
}
ENUMS = {
    "language": {"en", "es"},
    "role_track": {"core", "adjacent", "negative"},
    "seniority": {"senior", "junior", "manager", "ambiguous"},
    "remote_mode": {"worldwide", "eu", "us_only", "hybrid", "onsite", "unknown"},
    "eligibility_label": {"confirmed", "high", "medium", "unknown", "ineligible"},
    "vendor": {"greenhouse", "lever", "ashby", "generic_html", "json_api", "empty_dynamic"},
    "split": {"dev", "test"},
    "expected_stage": {"keep", "discard", "unsure"},
}
SYNTHETIC_URL_PREFIX = "https://jobs.example.test/"
# Case ids name the fixture file, so they may never contain path separators or dots.
CASE_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")


class CorpusError(ValueError):
    """The corpus or a report does not satisfy its contract."""


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    passed: bool
    regressions: tuple[str, ...]
    insufficient_samples: tuple[str, ...]


# --- corpus ---------------------------------------------------------------------------------------


def stable_split(case_id: str) -> str:
    """The corpus' stable 70/30 split: first 32 bits of sha256(id) modulo 100."""
    bucket = int(hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "dev" if bucket < 70 else "test"


def _label_errors(labels: Any) -> list[str]:
    if not isinstance(labels, dict) or set(labels) != LABEL_KEYS:
        return [f"labels must have exactly {sorted(LABEL_KEYS)}"]
    errors = [f"labels.{key} must be true, false or null" for key in sorted(LABEL_KEYS - {"eligibility"})
              if labels[key] not in (True, False, None)]
    if labels["eligibility"] not in ENUMS["eligibility_label"]:
        errors.append("labels.eligibility is invalid")
    return errors


def _check_case(case: Any, path: Path) -> list[str]:
    if not isinstance(case, dict):
        return [f"{path.name}: not a mapping"]
    if set(case) != CASE_KEYS:
        return [f"{path.name}: keys differ from the case contract: {sorted(set(case) ^ CASE_KEYS)}"]
    case_id = str(case["id"])
    checks = [
        (bool(CASE_ID_RE.fullmatch(case_id)), f"id {case_id!r} must be lowercase words joined by hyphens"),
        (case_id == path.stem, f"id {case_id!r} does not match the file name"),
        (case["split"] == stable_split(case_id), "split does not match the stable hash split"),
        (case["fixture"] == f"../fixtures/{case_id}.json", f"fixture must be ../fixtures/{case_id}.json"),
        (bool(str(case["notes"]).strip()), "notes must not be empty"),
    ]
    checks += [(case[key] in allowed, f"{key}={case[key]!r} is not one of {sorted(allowed)}")
               for key, allowed in ENUMS.items()]
    problems = [message for ok, message in checks if not ok] + _label_errors(case["labels"])
    return [f"{path.name}: {message}" for message in problems]


def _check_fixture(fixture: Any, case: dict[str, Any], name: str) -> list[str]:
    if not isinstance(fixture, dict) or set(fixture) != FIXTURE_KEYS:
        return [f"{name}: keys differ from the fixture contract"]
    errors = []
    if fixture["synthetic"] is not True:
        errors.append(f"{name}: synthetic must be true")
    if fixture["apply_url"] != SYNTHETIC_URL_PREFIX + case["id"]:
        errors.append(f"{name}: apply_url must be {SYNTHETIC_URL_PREFIX}{case['id']}")
    if fixture["vendor"] != case["vendor"] or fixture["language"] != case["language"]:
        errors.append(f"{name}: vendor/language disagree with the case")
    if not isinstance(fixture["is_open_hint"], bool) or not str(fixture["title"]).strip():
        errors.append(f"{name}: title and is_open_hint are required")
    text = " ".join(str(fixture[key]) for key in ("title", "location", "remote_text", "body_text"))
    if "@" in text or "http" in text:
        errors.append(f"{name}: free text must not contain emails or links")
    return errors


def _load_record(root: Path, path: Path) -> tuple[tuple[dict[str, Any], dict[str, Any]] | None, list[str]]:
    """One validated ``(case, fixture)`` pair, or ``None`` and the reasons it is invalid."""
    try:
        case = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return None, [f"{path.name}: unreadable: {exc}"]
    case_errors = _check_case(case, path)
    if case_errors:
        return None, case_errors
    fixture_path = root / "fixtures" / f"{case['id']}.json"
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"{fixture_path.name}: unreadable: {exc}"]
    fixture_errors = _check_fixture(fixture, case, fixture_path.name)
    return (None if fixture_errors else (case, fixture)), fixture_errors


def load_corpus(root: Path | None = None) -> tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]]]:
    """Return ``(metadata, [(case, fixture), ...])`` sorted by id; raise ``CorpusError`` when invalid."""
    root = CORPUS_DIR if root is None else root
    try:
        meta = yaml.safe_load((root / "corpus.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CorpusError(f"corpus.yaml is unreadable: {exc}") from exc
    if not isinstance(meta, dict) or meta.get("schema_version") != 1:
        raise CorpusError("corpus.yaml must declare schema_version 1")
    errors: list[str] = []
    records = []
    for path in sorted((root / "cases").glob("*.yaml")):
        record, record_errors = _load_record(root, path)
        errors.extend(record_errors)
        if record is not None:
            records.append(record)
    minimum = int(meta.get("minimum_cases") or 0)
    if not errors and len(records) < minimum:
        errors.append(f"corpus has {len(records)} cases, fewer than minimum_cases={minimum}")
    if errors:
        raise CorpusError("; ".join(errors))
    return meta, records


def load_profile() -> dict[str, Any]:
    return yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))


def posting_for(case: dict[str, Any], fixture: dict[str, Any]) -> dict[str, Any]:
    """A ``postings.jsonl`` row for one case (see the mapping in docs/self-improvement.md)."""
    url = str(fixture["apply_url"])
    return {
        "identity_key": f"benchmark:{case['id']}",
        "candidate": {
            "query_id": "", "query": "", "source_url": url, "normalized_url": url, "domain": "jobs.example.test",
            # Vendor-API postings bypass the ATS-domain allowlist exactly as in discover ats, so the corpus
            # measures the decision path rather than the synthetic host.
            "source_type": "ats_api", "source_key": f"{fixture['vendor']}:{case['id']}",
        },
        "job": {
            "apply_url": url,
            "domain": "jobs.example.test",
            "title": fixture["title"],
            "company": f"Synthetic Employer {case['id']}",
            "location": fixture["location"],
            "remote_text": fixture["remote_text"],
            "body_text": fixture["body_text"],
            "apply_available": bool(fixture["is_open_hint"]),
            "is_open_hint": bool(fixture["is_open_hint"]),
        },
    }


# --- metrics --------------------------------------------------------------------------------------


def _ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def classification_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Binary metrics over cases whose ``relevant`` label is not null. Only a confident keep is a positive."""
    binary = [r for r in results if r["truth"]["relevant"] is not None]
    positive = [r["prediction"]["status"] == "keep" for r in binary]
    truth = [r["truth"]["relevant"] is True for r in binary]
    tp = sum(p and t for p, t in zip(positive, truth, strict=True))
    fp = sum(p and not t for p, t in zip(positive, truth, strict=True))
    fn = sum(t and not p for p, t in zip(positive, truth, strict=True))
    tn = len(binary) - tp - fp - fn
    unsure = sum(r["prediction"]["status"] == "unsure" for r in results)
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    return {
        "sample_size": len(binary),
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "precision": precision,
        "recall": recall,
        "f1": _ratio(2 * precision * recall, precision + recall),
        "false_negative_rate": _ratio(fn, fn + tp),
        "false_positive_rate": _ratio(fp, fp + tn),
        "abstention_rate": _ratio(unsure, len(results)),
    }


def _dcg(gains: list[int]) -> float:
    return sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))


def ranking_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Metrics over the shortlist order finalize produced (keep and unsure rows, by rank)."""
    ranked = sorted((r for r in results if r["prediction"]["rank"] is not None), key=lambda r: r["prediction"]["rank"])
    gains = [int(r["truth"]["relevant"] is True) for r in ranked]
    relevant_total = sum(r["truth"]["relevant"] is True for r in results)
    first = next((index + 1 for index, gain in enumerate(gains) if gain), None)
    ideal = _dcg([1] * min(relevant_total, 10))
    return {
        "shortlist_size": len(ranked),
        "relevant_total": relevant_total,
        "precision_at_5": _ratio(sum(gains[:5]), 5),
        "precision_at_10": _ratio(sum(gains[:10]), 10),
        "recall_at_10": _ratio(sum(gains[:10]), relevant_total),
        "mrr": _ratio(1, first) if first else 0.0,
        "ndcg_at_10": _ratio(_dcg(gains[:10]), ideal),
    }


def slice_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for name, field in SLICES.items():
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for result in results:
            grouped[str(result[field])].append(result)
        output[name] = {}
        for value, items in sorted(grouped.items()):
            metrics = classification_metrics(items)
            metrics["status"] = "measured" if metrics["sample_size"] >= MIN_SLICE_SAMPLE else "insufficient_sample"
            output[name][value] = metrics
    return output


def build_report(results: list[dict[str, Any]], *, corpus_version: str, agent_queue: int) -> dict[str, Any]:
    losses = Counter(r["first_loss_stage"] for r in results if r["first_loss_stage"] is not None)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": "replay",
        "corpus_version": corpus_version,
        "preset": PRESET,
        "case_count": len(results),
        "metrics": {
            "classification": classification_metrics(results),
            "ranking": ranking_metrics(results),
            "slices": slice_metrics(results),
            "first_loss_counts": dict(sorted(losses.items())),
            "outcomes": dict(sorted(Counter(r["prediction"]["status"] for r in results).items())),
            "agent_queue": agent_queue,
        },
        "cases": results,
    }


# --- replay ---------------------------------------------------------------------------------------


def run_benchmark(root: Path | None = None) -> dict[str, Any]:
    """Replay every case through ULTRON's filter and finalize stages and return the report."""
    meta, records = load_corpus(root)
    postings = [posting_for(case, fixture) for case, fixture in records]
    replayed = replay(postings, profile=load_profile(), settings=presets.resolve(PRESET))
    results = []
    for (case, _fixture), posting in zip(records, postings, strict=True):
        outcome = replayed["identities"][posting["identity_key"]]
        status = final_status(outcome)
        final = outcome.get("final") or {}
        filtered = outcome.get("filter") or {}
        relevant = case["labels"]["relevant"]
        results.append({
            "id": case["id"],
            "split": case["split"],
            **{field: case[field] for field in SLICES.values()},
            "truth": dict(case["labels"]),
            "expected_stage": case["expected_stage"],
            "prediction": {
                "status": status,
                "rank": final.get("rank"),
                "match_score": final.get("match_score"),
                "match_band": final.get("match_band"),
                "filter_route": filtered.get("route"),
                "filter_stage": filtered.get("stage"),
                "reason": filtered.get("reason") or final.get("reason"),
            },
            "first_loss_stage": losing_stage(outcome) if relevant is True else None,
        })
    agent_queue = int(replayed["filter_summary"]["ambiguous"])
    return build_report(results, corpus_version=str(meta.get("corpus_version")), agent_queue=agent_queue)


# --- comparison -----------------------------------------------------------------------------------


def _check_report(report: Any, label: str) -> None:
    if not isinstance(report, dict) or report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise CorpusError(f"{label} report must use schema_version {REPORT_SCHEMA_VERSION}")
    if report.get("mode") != "replay" or not isinstance(report.get("metrics"), dict):
        raise CorpusError(f"{label} report is not a replay report")


def _declines(base: dict[str, Any], candidate: dict[str, Any], tolerance: float, label: str) -> list[str]:
    return [
        f"{label} {metric} declined by {float(base[metric]) - float(candidate[metric]):.4f}"
        for metric in GATED_METRICS
        if float(base[metric]) - float(candidate[metric]) > tolerance
    ]


def _slice_gates(baseline: dict[str, Any], candidate: dict[str, Any]) -> tuple[list[str], list[str]]:
    regressions: list[str] = []
    insufficient: list[str] = []
    for dimension, groups in baseline["metrics"]["slices"].items():
        candidate_groups = candidate["metrics"]["slices"].get(dimension) or {}
        for name, values in groups.items():
            other = candidate_groups.get(name)
            if not other:
                regressions.append(f"slice {dimension}={name} is missing")
                continue
            sample = min(int(values["sample_size"]), int(other["sample_size"]))
            if sample < MIN_SLICE_SAMPLE:
                insufficient.append(f"{dimension}={name} sample={sample}")
            else:
                regressions += _declines(values, other, SLICE_REGRESSION_TOLERANCE, f"slice {dimension}={name}")
    return regressions, insufficient


def _case_gates(baseline: dict[str, Any], candidate: dict[str, Any], justified: set[str]) -> list[str]:
    candidate_cases = {c["id"]: c for c in candidate.get("cases") or []}
    regressions = []
    for case in baseline.get("cases") or []:
        other = candidate_cases.get(case["id"])
        if other is None:
            regressions.append(f"case {case['id']} is missing")
            continue
        was_kept = case["truth"]["relevant"] is True and case["prediction"]["status"] == "keep"
        if was_kept and other["prediction"]["status"] != "keep" and case["id"] not in justified:
            regressions.append(f"new false negative: {case['id']}")
    return regressions


def compare_reports(
    baseline: dict[str, Any], candidate: dict[str, Any], *, justified_case_ids: set[str] | None = None
) -> ComparisonResult:
    """Apply the gates: global drop > 1 point, slice drop > 5 points (n >= 10), new false negatives."""
    _check_report(baseline, "baseline")
    _check_report(candidate, "candidate")
    regressions = _declines(
        baseline["metrics"]["classification"], candidate["metrics"]["classification"],
        GLOBAL_REGRESSION_TOLERANCE, "global",
    )
    slice_regressions, insufficient = _slice_gates(baseline, candidate)
    regressions += slice_regressions
    regressions += _case_gates(baseline, candidate, justified_case_ids or set())
    return ComparisonResult(not regressions, tuple(regressions), tuple(insufficient))


# --- output ---------------------------------------------------------------------------------------


def report_markdown(report: dict[str, Any]) -> str:
    classification = report["metrics"]["classification"]
    ranking = report["metrics"]["ranking"]
    lines = [
        "# ULTRON quality benchmark",
        "",
        f"Cases: {report['case_count']} (corpus {report['corpus_version']}, preset {report['preset']})",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key in ("precision", "recall", "f1", "false_negative_rate", "false_positive_rate", "abstention_rate"):
        lines.append(f"| {key} | {classification[key]:.4f} |")
    for key in ("precision_at_5", "precision_at_10", "recall_at_10", "mrr", "ndcg_at_10"):
        lines.append(f"| {key} | {ranking[key]:.4f} |")
    lines += ["", f"Confusion matrix: {classification['confusion_matrix']}", "", "First losing stage:"]
    losses = report["metrics"]["first_loss_counts"]
    lines += [f"- {stage}: {count}" for stage, count in losses.items()] or ["- none"]
    return "\n".join(lines) + "\n"


def dump_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _safe(raw: Path) -> Path:
    try:
        return contained_path(raw)
    except UnsafePathError as exc:
        raise CorpusError(str(exc)) from exc


def _read_report(raw: Path, label: str) -> dict[str, Any]:
    path = _safe(raw)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CorpusError(f"{label} report {path.name} is unreadable: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ULTRON labelled quality benchmark (offline replay).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="check the corpus contract")
    run = sub.add_parser("run", help="replay the corpus and print or write the JSON report")
    run.add_argument("--output", type=Path, help="write the JSON report here (and a .md summary next to it)")
    cmp_ = sub.add_parser("compare", help="gate a candidate report against a baseline")
    cmp_.add_argument("baseline", type=Path)
    cmp_.add_argument("candidate", type=Path, nargs="?", help="candidate report (default: replay the corpus now)")
    cmp_.add_argument("--allow-case", action="append", default=[], help="justified new false negative (repeatable)")
    return parser


def _cmd_validate(_args: argparse.Namespace) -> int:
    _meta, records = load_corpus()
    print(json.dumps({"valid": True, "cases": len(records)}))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    output = _safe(args.output) if args.output else None
    report = run_benchmark()
    if output is not None:
        try:
            output.write_text(dump_report(report), encoding="utf-8", newline="\n")
            output.with_suffix(".md").write_text(report_markdown(report), encoding="utf-8", newline="\n")
        except OSError as exc:
            raise CorpusError(f"{output.name}: cannot write the report: {exc}") from exc
    else:
        sys.stdout.write(dump_report(report))
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    baseline = _read_report(args.baseline, "baseline")
    candidate = _read_report(args.candidate, "candidate") if args.candidate else run_benchmark()
    result = compare_reports(baseline, candidate, justified_case_ids=set(args.allow_case))
    print(json.dumps({
        "passed": result.passed,
        "regressions": list(result.regressions),
        "insufficient_sample": list(result.insufficient_samples),
        "candidate": {k: candidate["metrics"]["classification"][k] for k in GATED_METRICS},
    }, indent=2))
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = {"validate": _cmd_validate, "run": _cmd_run, "compare": _cmd_compare}[args.command]
    try:
        return command(args)
    except CorpusError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""Markdown summary of an arm comparison report (mean ± sample std over repetitions)."""

from __future__ import annotations

from typing import Any

REPORT_ROWS = (
    ("Precision", "precision"), ("P@5", "precision_at_5"), ("P@10", "precision_at_10"),
    ("Relevant found", "relevant"), ("Pool recall", "pool_recall"), ("Closed rate", "closed_rate"),
    ("Ineligible rate", "ineligible_rate"), ("Role mismatch rate", "role_mismatch_rate"),
    ("Duplicate rows", "duplicate_rows"), ("Rows", "rows"), ("Tokens", "tokens"), ("Tool calls", "tool_calls"),
    ("Seconds", "seconds"),
)
ARM_ROWS = (
    ("Relevant (all reps)", "union_relevant"), ("Pool recall (all reps)", "union_pool_recall"),
    ("Only this arm", "only_this_arm_relevant"), ("Reps with 0 relevant", "zero_relevant_repetitions"),
    ("Reproducibility, all jobs (Jaccard)", "reproducibility_jaccard"),
    ("Reproducibility, relevant jobs (Jaccard)", "reproducibility_relevant_jaccard"),
)


def _value(value: Any) -> str:
    return "-" if value is None else f"{value:g}" if isinstance(value, float) else str(value)


def _cell(stat: dict[str, Any]) -> str:
    if stat["mean"] is None:
        return "-"
    return f"{stat['mean']:g} ± {stat['std']:g}" if stat["std"] is not None else f"{stat['mean']:g}"


def _row(label: str, cells: list[str]) -> str:
    return f"| {label} | " + " | ".join(cells) + " |"


def report_markdown(report: dict[str, Any]) -> str:
    """A short human summary of ``report``."""
    arms = sorted(report["arms"])
    data = report["arms"]
    lines = [f"# Arm comparison: {report['session']}", ""]
    if not report["complete"]:
        lines += ["**Partial result:** " + "; ".join(
            [*report["problems"], f"{report['pending_jobs']} jobs with pending labels"]), ""]
    lines += [
        (f"Top N: {report['top_n']} · pooled jobs: {report['pool']['jobs']} · relevant in pool: "
         f"{report['pool']['relevant']} · ULTRON {report['ultron_version']}"),
        "", _row("Metric", arms), "|---|" + "---|" * len(arms),
    ]
    lines += [_row(label, [_cell(data[a]["aggregate"][key]) for a in arms]) for label, key in REPORT_ROWS]
    lines += [_row(f"{field.replace('_', ' ').capitalize()} per relevant (pooled)",
                   [_value(data[a]["cost_per_relevant"][field]) for a in arms])
              for field in ("tokens", "tool_calls", "seconds")]
    lines += [_row(label, [_value(data[a][key]) for a in arms]) for label, key in ARM_ROWS]
    lines += ["", "Pool recall counts the relevant jobs either arm found, not the whole market.", ""]
    for pair, values in report["between_arms"].items():
        lines.append(f"- {pair}: Jaccard all {_value(values['jaccard_all'])}, "
                     f"relevant {_value(values['jaccard_relevant'])}")
    return "\n".join(lines) + "\n"

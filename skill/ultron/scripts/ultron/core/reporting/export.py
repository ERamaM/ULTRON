from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from ultron.core.models import DiscardRecord, FinalRow, RunSummary

TABLE_HEADER = "| title | company | apply_url | location | remote_policy | eligibility | eligibility_evidence | fit_signals | freshness | match_score | match_band | matched | missing | uncertain |"
TABLE_SEP = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"


def _escape_cell(value: str) -> str:
    from ultron.core.privacy import redact

    # Clean before redacting: an invisible character can split a token so the redaction misses it.
    text = str(value).replace("\r", "").replace("\n", " ").replace("\t", " ")
    text = "".join(ch for ch in text if unicodedata.category(ch) not in {"Cc", "Cf"})
    text = str(redact(text))
    text = text.replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")
    return text.replace("<", "&lt;").replace(">", "&gt;").strip()


def render_results_markdown(rows: list[FinalRow]) -> str:
    """Render final roles as a redacted Markdown table."""
    lines = [TABLE_HEADER, TABLE_SEP]
    for row in rows:
        fit = ", ".join(row.fit_signals[:3])
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_cell(row.title),
                    _escape_cell(row.company),
                    _escape_cell(row.apply_url),
                    _escape_cell(row.location),
                    _escape_cell(row.remote_policy),
                    _escape_cell(row.eligibility),
                    _escape_cell(row.eligibility_evidence),
                    _escape_cell(fit),
                    _escape_cell(row.freshness),
                    _escape_cell("" if row.match_score is None else str(row.match_score)),
                    _escape_cell(row.match_band or "unknown"),
                    _escape_cell(", ".join(row.matched_requirements)),
                    _escape_cell(", ".join(row.missing_requirements)),
                    _escape_cell(", ".join(row.uncertain_requirements)),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def render_summary_markdown(summary: RunSummary, discards: list[DiscardRecord]) -> str:
    """Render run metrics and discard diagnostics as Markdown."""
    ats_metrics = summary.ats_seed_metrics or {}
    search_metrics = summary.search_query_metrics or {}
    llm_diagnostics = summary.llm_diagnostics or {}
    discovery_metrics = (summary.seed_source_counts or {}).get("discovery_totals", {})
    lines = [
        "## Run Summary",
        "",
        f"1. ATS pages opened vs roles kept: {summary.ats_pages_opened} / {summary.roles_kept}",
        f"   Guardrail saves: {summary.guardrail_saves} | Unsure→keep: {summary.unsure_to_keep_count}"
        f" | Hard-filter overrides: {summary.hard_filter_overrides}",
    ]
    if discovery_metrics:
        lines.append(
            "   Discovery units: "
            f"{discovery_metrics.get('processed', 0)}/{discovery_metrics.get('total', 0)} processed"
            f" | with hits={discovery_metrics.get('with_hits', 0)}"
            f" | coverage={discovery_metrics.get('coverage_pct', 0.0)}%"
            f" | candidates={discovery_metrics.get('hits', 0)}"
        )
    if ats_metrics:
        lines.append(
            "   ATS seeds: "
            f"{ats_metrics.get('seeds_processed', 0)}/{ats_metrics.get('seeds_total', 0)} processed"
            f" | with hits={ats_metrics.get('seeds_with_hits', 0)}"
            f" | coverage={ats_metrics.get('seed_coverage_pct', 0.0)}%"
            f" | failed={ats_metrics.get('seeds_failed', 0)}"
        )
    if search_metrics:
        lines.append(
            "   Search queries: "
            f"{search_metrics.get('queries_processed', 0)}/{search_metrics.get('queries_total', 0)} processed"
            f" | with hits={search_metrics.get('queries_with_hits', 0)}"
            f" | coverage={search_metrics.get('query_coverage_pct', 0.0)}%"
            f" | zero-hit streak={search_metrics.get('zero_hit_query_streak_max', 0)}"
        )
        lines.append(
            "   Recall metrics: "
            f"search_fallback_triggered={summary.search_fallback_triggered}"
            f" | search_fallback_hits={summary.search_fallback_hits}"
            f" | soft_filtered_count={summary.soft_filtered_count}"
        )
    if llm_diagnostics:
        fallback = llm_diagnostics.get("heuristic_fallback", {})
        gate = llm_diagnostics.get("gate", {})
        if gate:
            lines.append(
                "   Agent gate: "
                f"threshold={gate.get('threshold', 0)}"
                f" | sent keep={gate.get('sent_heuristic_keep', 0)}"
                f" | unsure={gate.get('sent_heuristic_unsure', 0)}"
                f" | discard={gate.get('sent_heuristic_discard', 0)}"
                f" | below threshold={gate.get('skipped_below_threshold', 0)}"
                f" | clear keep={gate.get('skipped_clear_keep', 0)}"
            )
        lines.append(
            "   Agent diagnostics: "
            f"parse={llm_diagnostics.get('parse_failures', 0)}"
            f" | timeout={llm_diagnostics.get('timeout_failures', 0)}"
            f" | context={llm_diagnostics.get('context_overflow_failures', 0)}"
            f" | connection={llm_diagnostics.get('connection_failures', 0)}"
            f" | other={llm_diagnostics.get('other_failures', 0)}"
        )
        lines.append(
            "   Heuristic fallback by cause: "
            f"parse={fallback.get('parse', 0)}"
            f" | timeout={fallback.get('timeout', 0)}"
            f" | context={fallback.get('context_overflow', 0)}"
            f" | connection={fallback.get('connection', 0)}"
            f" | other={fallback.get('other', 0)}"
        )
    lines.extend(
        [
            "2. Discard reasons with counts:",
        ]
    )
    for reason, count in sorted(summary.discard_counts.items()):
        lines.append(f"- {reason}: {count}")
    lines.append("3. Full query log:")
    for row in summary.full_query_log:
        lines.append(
            f"- {_escape_cell(str(row.get('query_id', '')))}: {_escape_cell(str(row.get('query', '')))} "
            f"(hits={row.get('hits', 0)})"
        )

    if discards:
        lines.append("")
        lines.append("## Discard Samples")
        for discard in discards[:30]:
            lines.append(f"- {_escape_cell(discard.reason)}: {_escape_cell(discard.apply_url)}")
    return "\n".join(lines) + "\n"


def write_outputs(
    output_dir: str,
    run_id: str,
    rows: list[FinalRow],
    summary: RunSummary,
    discards: list[DiscardRecord],
) -> tuple[str, str, str]:
    """Write redacted run outputs and return ``(results_md, results_json, summary_md)`` paths."""
    # Exports are an explicit diagnostics boundary: redact recursively before
    # serializing any provider payload or future secret-bearing field.
    from ultron.core.privacy import redact

    run_dir = Path(output_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    results_md = run_dir / "results.md"
    results_json = run_dir / "results.json"
    summary_md = run_dir / "run_summary.md"

    results_md.write_text(render_results_markdown(rows), encoding="utf-8")
    results_json.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "rows": [redact(row.model_dump()) for row in rows],
                "summary": redact(summary.model_dump(mode="json")),
                "discards": [redact(d.model_dump()) for d in discards],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    summary_md.write_text(render_summary_markdown(summary, discards), encoding="utf-8")
    return str(results_md), str(results_json), str(summary_md)

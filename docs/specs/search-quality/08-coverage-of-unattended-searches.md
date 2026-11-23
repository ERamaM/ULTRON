# 08 · Coverage of unattended searches

Status: proposed. Implementation awaits user selection.

## Current behavior

The unattended flow already collects stage outcomes and seed coverage. A shortlist count alone cannot explain how much of the intended search completed. [Source](../../../skill/ultron/scripts/ultron/stages/unattended.py).

## Expected behavior

Summarize boards attempted, completed, failed and skipped, plus pending ambiguous vacancies and limiting budgets. Derive claims from stage records and identify stages whose evidence is unavailable.

## Acceptance cases

- Two runs returning three vacancies explain different outcomes when one completed all planned boards and another encountered outages.
- A zero-result run with adequate coverage is distinguishable from a run that fetched nothing. Historical runs with missing counters show unknown coverage.

## Verification

Replay saved synthetic stage outputs through unattended finalization. Reconcile summary counts with the underlying records. Apply the [shared verification criteria](README.md#verification).

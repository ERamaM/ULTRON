# 15 · Backfill company verification slots

Status: proposed. Implementation awaits user selection.

## Current behavior

Unattended company verification slices feed proposals before processing them. The feed producer already combines equal normalized company names. Distinct aliases that identify the same board can still occupy this window. [Source](../../../skill/ultron/scripts/ultron/stages/unattended.py).

## Expected behavior

Select distinct eligible employer-board proposals before applying the verification quota. Use canonical board identity when known and conservative matching when only employer hints exist. Retain the original HTTP limits.

## Acceptance cases

- With quota two, proposals for two aliases of A with the same verified board and one distinct employer B give distinct employers A and B a verification opportunity when budget allows.
- Distinct employers with similar names remain eligible. A budget stop records which proposals were deferred and preserves completed verification results.

## Verification

Replay feed proposals with duplicate aliases, ambiguous names and exhausted budgets. Compare attempted identities and total requests. Apply the [shared verification criteria](README.md#verification).

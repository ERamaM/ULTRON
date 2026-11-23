# 07 · Historical salary observations

Status: proposed. Implementation awaits user selection.

## Current behavior

Mining marks a vacancy as seen before checking whether its capture contains a salary. An incomplete copy can hide usable earlier evidence. [Source](../../../skill/ultron/scripts/ultron/salary/estimator.py).

## Expected behavior

Select the most recent usable comparable observation within the search window per vacancy and market. Retain its observation date and label evidence from older captures. Keep an explicitly withdrawn or superseded band distinguishable.

## Acceptance cases

- A complete older copy followed by a truncated copy yields one dated historical comparable in either traversal order.
- Two complete observations with different bands use the most recent applicable band. An explicit withdrawal never appears as current pay.

## Verification

Replay dated captures, duplicates and tied timestamps. Verify counts and deterministic selection. Coordinate comparable grouping with spec 06. Apply the [shared verification criteria](README.md#verification).

# 03 · Reliable board yield history

Status: proposed. Implementation awaits user selection.

## Current behavior

Seed prioritization uses attempts and keeps. A temporary outage can look like repeated evidence that an employer has no relevant openings. [Source](../../../skill/ultron/scripts/ultron/stages/discover.py).

## Expected behavior

Record whether discovery completed, was partial, failed or was skipped. Use completed observations for negative yield evidence. Preserve transport backoff and keep positive evidence from partial observations identifiable.

## Acceptance cases

- Five completed searches with zero relevant vacancies contribute five negative observations. Five timeouts contribute zero.
- A partial page with a relevant vacancy preserves the find. A retry of the same run cannot increment its observation twice. Legacy attempts with unknown outcome remain distinguishable.

## Verification

Replay identical board inventories with contrasting histories. Check selection, exploration reserve and network limits. Apply the [shared verification criteria](README.md#verification).

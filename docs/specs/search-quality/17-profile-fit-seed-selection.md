# 17 · Profile-fit seed selection

Status: proposed. Implementation awaits user selection.

## Current behavior

`balanced` queries at most 180 boards per run. When the inventory holds more, selection orders seeds by group, yield history and priority, and feed-discovered seeds can take every slot outside the non-feed reserve. [Source](../../../skill/ultron/scripts/ultron/stages/discover.py).

In the [baseline comparison](README.md#baseline-comparison-of-2026-11-25), the inventory held 435 seeds and each run selected 180. Of those, 44 were feed seeds, most with unverified ownership, and only 53 boards returned a candidate. Eight employers with a relevant open posting were already in the inventory and were not selected. All eight were stored under a different profile of the same role family.

## Expected behavior

Rank eligible seeds by expected yield for the active profile before spending the seed budget:

- A seed verified for another profile of the same role family or sector counts as a fit for this one.
- A seed with kept postings for a similar profile ranks above an unmeasured seed.
- Feed seeds with unverified ownership take a small, fixed share of the budget.

Keep the exploration reserve, the per-host cap and the yield demotion from [03](03-reliable-board-yield-history.md).

## Acceptance cases

- An inventory larger than the budget holds a verified seed stored for a sibling profile of the same role family. The seed is selected ahead of feed seeds with unverified ownership.
- Feed seeds with unverified ownership fill at most their fixed share, even when non-feed seeds run out. Unused slots stay empty and are reported as such, so a short inventory is visible in the summary.
- A seed with keeps for a sibling profile outranks an unmeasured seed of the same group. A seed demoted for five empty completed attempts on this profile stays last.
- The run summary reports, per group, how many seeds were selected, capped and left out.

## Verification

Unit-test selection with synthetic inventories for each case. Replay a recorded inventory and compare the selected set before and after. Then repeat the baseline comparison: the share of baseline-only relevant jobs whose employer was never queried must fall. Apply the [shared verification criteria](README.md#verification).

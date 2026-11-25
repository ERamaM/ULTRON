# 23 · Expand from found employers and the watchlist

Status: proposed. Delivery 2.

## Current behavior

`ultron discover ats` selects up to 180 seeds from the whole inventory and queries each board. [Source](../../../skill/ultron/scripts/ultron/stages/discover.py). In the comparison only 53 of 180 boards returned a candidate, and eight inventory employers with a relevant posting were not selected. The eight relevant jobs only ULTRON found came from querying board APIs, which web search does not index well.

## Expected behavior

In a scout run, `discover ats` queries a small, targeted set of boards:

- **Found employers.** Every supported board resolved from the finds of this run ([22](22-resolve-agent-finds-to-canonical-postings.md)). An employer with one matching posting often has more.
- **Watchlist.** Boards that produced a kept posting for this profile, or for a profile of the same role family, in earlier runs. The ledger already records keeps per seed and run.

It does not sweep the inventory, run seed rounds or use feed hints. The set is capped by the preset, with found employers first and then watchlist boards by keep history.

For each board, keep the postings that match the target titles or the role-family vocabulary of the profile and are not clearly ineligible, as the coarse pre-filter does today. Report per board how many postings were listed, matched and cut by the cap, so a miss on a queried board can be explained.

The watchlist is visible and editable: `ultron watchlist list|add|remove --profile <slug>`.

## Acceptance cases

- A run whose finds resolve to three supported boards queries those three boards and the watchlist, and no other inventory seed.
- A second matching posting on a found board, which the agent did not find, reaches the evaluation queue marked as found by expansion.
- A board with keeps for a sibling profile of the same role family is on the watchlist of this profile. A board with five completed attempts and no keep is not.
- With an empty watchlist and no finds, `discover ats` queries nothing and says so.
- The per-board report separates postings not listed by the vendor, rejected by the pre-filter and cut by the cap.
- Request budgets per host and per vendor are respected, and an exhausted budget is reported.

## Verification

Replay recorded board inventories with a fake client. Check the selected set, the per-board report and the budgets. Live, delivery 2 must beat the free agent on relevant jobs per run. Apply the [shared verification](README.md#verification).

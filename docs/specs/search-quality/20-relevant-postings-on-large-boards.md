# 20 · Relevant postings on large boards

Status: proposed. Implementation awaits user selection.

## Current behavior

For each queried board, discovery keeps the postings that pass a coarse title and location pre-filter, ranks them and takes at most `ats_api_max_jobs_per_seed` (35 in `balanced`). [Source](../../../skill/ultron/scripts/ultron/core/discovery/prefilter.py).

In the [baseline comparison](README.md#baseline-comparison-of-2026-11-25), 12 of 105 misses were relevant postings on boards ULTRON did query. The posting never became a candidate. The cause was not isolated: it may be the pre-filter vocabulary, the per-board cap or the pagination of the board.

## Expected behavior

First record why a posting on a queried board does not become a candidate: rejected by the title pre-filter, rejected as ineligible, cut by the per-board cap, or absent from the board response. Then fix the dominant cause:

- A posting whose title matches a target title or the role-family vocabulary is never cut by the cap while lower-ranked postings are kept.
- Target titles from the profile always count as matches for the pre-filter, including common variants such as a seniority prefix or a specialty suffix.

## Acceptance cases

- A board with more matching postings than the cap keeps the closest matches to the target titles, and the summary reports how many were cut.
- A posting titled with a target title plus a specialty in parentheses passes the pre-filter.
- A posting absent from the vendor response is reported as not listed, separately from a posting the pre-filter rejected.
- The per-seed cap and the request budgets are unchanged for boards with few postings.

## Verification

Replay recorded board inventories and list, per board, the postings dropped at each step. Add pre-filter cases to the heuristic parity suite. Then repeat the baseline comparison: the count of misses on queried boards must fall. Apply the [shared verification criteria](README.md#verification).

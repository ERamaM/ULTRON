# 18 · Role-first discovery through aggregators

Status: proposed. Implementation awaits user selection.

## Current behavior

Discovery is employer-first: it queries the boards of known seeds, and the web query plan mostly targets ATS hosts. A posting from an employer that is not a seed is found only when one of the 40 planned queries happens to return it. [Source](../../../skill/ultron/scripts/ultron/core/discovery/query_plan.py).

In the [baseline comparison](README.md#baseline-comparison-of-2026-11-25), 86 of 105 misses came from employers ULTRON never queried, and most of those employers were not in the inventory at all. The agent without ULTRON reached them by searching for the role on remote-job aggregators and then opening the board of the employer.

## Expected behavior

Add a role-first pass to the query plan. It searches job aggregators and remote-job boards for the target titles and region of the profile. Each hit is treated as an employer hint, never as a posting:

- Resolve the hint to the board of the employer through the existing seed verification.
- Query a verified board in the same run, within the request and vendor budgets.
- Keep an aggregator page out of the shortlist. The exported `apply_url` stays the posting on the employer board.

Record which pass produced each new seed, so its yield can be measured.

## Acceptance cases

- An aggregator hit names an employer whose board verifies. The employer becomes a seed, its board is queried in the same run and its matching posting reaches the filter with the employer URL.
- An aggregator hit whose employer cannot be resolved produces no candidate and no shortlist row. It is counted as unresolved in the summary.
- A recruiter listing for an unnamed client is not turned into a seed.
- The role-first pass respects `max_search_queries`, the per-host cap and the vendor budget. With the pass disabled, the query plan is unchanged.
- Aggregator page text is handled as data: instructions embedded in it are ignored, as [SKILL.md](../../../skill/ultron/SKILL.md) already requires for fetched pages.

## Verification

Use synthetic aggregator results and board fixtures for each case. Check the stored seed source and the summary counts. Then repeat the baseline comparison: relevant jobs found per run and pool recall must rise without a higher ineligible rate. Apply the [shared verification criteria](README.md#verification).

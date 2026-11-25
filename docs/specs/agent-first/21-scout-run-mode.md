# 21 · Scout run mode

Status: proposed. Delivery 1.

## Current behavior

`ultron run new` returns a query plan and a seed deficit. The skill then requires seed rounds, feed discovery, the 40 planned web searches and a sweep of up to 180 stored boards before anything is fetched. [Source](../../../skill/ultron/scripts/ultron/commands/runs.py). The stage order is enforced, so the agent cannot skip to its own finds.

## Expected behavior

Add a run mode, `scout`, chosen with `ultron run new --profile <slug> --mode scout`. The mode is stored in `run.json`. The existing behavior stays available as the default mode until [28](28-retire-the-sweep-pipeline.md).

A scout run has this order: `run new` → the agent searches freely and adds finds ([22](22-resolve-agent-finds-to-canonical-postings.md)) → `discover ats` expands ([23](23-expand-from-found-employers-and-the-watchlist.md)) → `fetch` → `filter` → evaluate ([24](24-agent-judgment-decides.md)) → `finalize`.

In scout mode:

- `run new` returns a brief, not a query plan: the profile summary, the memory of [25](25-memory-in-the-brief-and-in-the-results.md) and the request budgets. It gives search advice, such as searching by role and following aggregators to the board of the employer, but no mandatory queries.
- Seed rounds, `seeds plan` and `discover feeds` are not required and are not run.
- The agent may add finds in several batches, before and after expansion.
- Presets still set the budgets: pages, requests per host and per vendor, and the size of the evaluation queue.

`SKILL.md` documents the scout loop as a second workflow table. It tells the agent when to stop searching: when two consecutive searches add no new employer, or at the shortlist size the user asked for.

## Acceptance cases

- `run new --mode scout` creates a run whose `run.json` records the mode and whose output has a brief and no `query_plan`, `seed_deficit` or `cold_start`.
- In a scout run, `fetch` succeeds after `candidates add` alone, with no `discover` stage. In the default mode the current order is still enforced.
- An unknown mode is a usage error with the valid modes listed.
- `ultron runs list` shows the mode of each run. Runs created before this change read as the default mode.
- `ultron --help` and every command in `SKILL.md` and its references stay consistent.

## Verification

Unit-test run creation, stage order and the output contract for both modes. Run the scout loop end to end on fixtures with a fake HTTP client. Apply the [shared verification](README.md#verification).

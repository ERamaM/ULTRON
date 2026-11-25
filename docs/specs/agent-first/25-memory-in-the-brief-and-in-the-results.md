# 25 · Memory in the brief and in the results

Status: proposed.

## Current behavior

The ledger records every finalized posting and marks rows as new in `results.json`. [Source](../../../skill/ultron/scripts/ultron/storage/ledger.py). The agent learns this only at the end of a run. It can spend its search and evaluation on postings the user already saw or rejected, and a free agent has no memory at all.

## Expected behavior

Give the agent the memory at the start of a scout run and at every step:

- **In the brief.** `run new` returns counts and a compact list for the profile: employers on the watchlist, employers the user rejected, and how many postings are already known. It returns no posting bodies.
- **When resolving.** Each find comes back flagged `new`, `seen` (exported in an earlier run), `rejected` (discarded by the agent or set to `rejected` or `archived` by the user) or `applied`. [22](22-resolve-agent-finds-to-canonical-postings.md) reports the flag per row.
- **In the queue.** A `seen` posting keeps its last verdict and priority and is not queued again unless its content changed. A `rejected` or `applied` posting is not queued.
- **In the results.** The export lists new rows first within each verdict and keeps the `is_new` field. The summary says how many finds were already known, so the user sees what the search added.

A rejection carries its reason. A posting rejected for a fact that can change, such as being closed, may return if it reopens.

## Acceptance cases

- A find that was exported by an earlier run of the same profile is flagged `seen`, is not sent to the evaluation queue and appears in the results with its earlier verdict and `is_new` false.
- A find the user set to `rejected` is flagged `rejected` and never appears in the results.
- A `seen` posting whose title or location changed since the last run is queued again and marked as changed.
- A posting seen under another profile is `new` for this one.
- The brief of a profile with no history has empty lists and says the profile is new.
- The brief stays small: it has counts and names, never posting text.

## Verification

Seed a ledger fixture with exported, rejected and applied postings. Run a scout loop over finds that overlap it and check the flags, the queue and the results. Apply the [shared verification](README.md#verification).

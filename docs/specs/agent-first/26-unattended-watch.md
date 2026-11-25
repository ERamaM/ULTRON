# 26 · Unattended watch

Status: proposed.

## Current behavior

`ultron run --unattended` runs the whole sweep with heuristics only: feeds, feed companies, up to 180 boards, fetch, filter and finalize, with no web search and no evaluation. [Source](../../../skill/ultron/scripts/ultron/stages/unattended.py). Its results inherit the precision of the heuristics, which the comparison measured as poor.

## Expected behavior

Add `ultron watch --profile <slug>`. It needs no agent and uses no tokens:

- It queries only the watchlist boards of [23](23-expand-from-found-employers-and-the-watchlist.md).
- It keeps postings that match the target titles or role-family vocabulary and that the ledger has not seen for this profile.
- It writes a digest of new postings per employer, with the structured facts. It gives no verdict.
- It records the postings as `pending_review`, so they are not reported as new again.

A later scout run starts from that work. Its brief says how many watched postings are pending, and those postings enter the evaluation queue first. The agent judges only the delta.

A saved search can run the watch on a schedule, as saved searches run unattended sweeps today.

## Acceptance cases

- A watch over a watchlist of three boards sends requests to those three boards only.
- A posting already exported or rejected for the profile is not in the digest. A posting first seen by this watch is, once.
- Running the watch twice with no change on the boards gives an empty second digest.
- The next scout run for the profile lists the pending postings in its brief and serves them first in the evaluation queue.
- A board that fails is reported with its error, and the other boards are still watched. Request budgets per host and per vendor are respected.
- With an empty watchlist, the watch does nothing and says how to build one: run a scout search first.

## Verification

Replay two snapshots of the same boards with a fake client and check the digest, the ledger states and the brief of the following run. Live, the second-day measurement of [27](27-multi-arm-and-second-day-measurement.md) must pass. Apply the [shared verification](README.md#verification).

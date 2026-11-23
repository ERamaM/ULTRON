# 01 · Unique vacancies per board

Status: proposed. Implementation awaits user selection.

## Current behavior

Discovery slices ranked postings before checking whether a candidate can be emitted. A duplicate near the top consumes a place. [Source](../../../skill/ultron/scripts/ultron/stages/discover.py).

## Expected behavior

Apply the existing per-board cap to usable, unique candidates. Walk the already fetched ranked sequence in stable order until the quota is full or the sequence ends. Preserve source precedence and the request budget.

## Acceptance cases

- With cap three and ranked rows A, A, invalid, B, C, emit A, B, C in that order.
- With only A and its duplicate, emit A once. A URL already accepted from another board leaves its slot available.

## Verification

Replay a board response through discovery and compare both emitted identities and HTTP call counts. Apply the [shared verification criteria](README.md#verification).

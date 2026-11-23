# 11 · New vacancies per saved search

Status: proposed. Implementation awaits user selection.

## Current behavior

The new-only list tests a global times_seen count, and new-since uses global first discovery. These answer a different question from what first appeared in one saved search. [Source](../../../skill/ultron/scripts/ultron/storage/review.py).

## Expected behavior

Support first appearance relative to the selected search or profile using recorded occurrences. Preserve existing global queries and state the comparison scope in results. Only claim saved-search granularity where provenance supports it.

## Acceptance cases

- A vacancy discovered Monday by profile A and Thursday by B is new to B against B's Tuesday baseline.
- Rediscovery on Friday does not make it new to B again. If old records identify only the profile, report the available scope.

## Verification

Test overlapping searches, repeated runs and a baseline from another scope through the public listing command. Apply the [shared verification criteria](README.md#verification).

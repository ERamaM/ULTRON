# 16 · Preserve evidence from repeated web hits

Status: proposed. Implementation awaits user selection.

## Current behavior

When web search rediscovers an existing identity, candidate ingestion increments a duplicate counter and skips the row. Useful missing metadata or discovery provenance can be lost. [Source](../../../skill/ultron/scripts/ultron/stages/candidates.py).

## Expected behavior

Merge trustworthy missing metadata and record bounded provenance for the same vacancy while retaining one fetch candidate. Preserve authoritative ATS fields on conflict and apply existing privacy rules to stored provenance.

## Acceptance cases

- A later search result supplies the missing company for the same identity. The vacancy is fetched once and the two source observations remain traceable.
- A contradictory web title cannot overwrite a verified ATS title. Re-importing the same batch is idempotent and cannot grow provenance indefinitely.

## Verification

Exercise candidate import twice with tracking URL variants and conflicting source fields. Verify output cardinality, precedence and privacy. Apply the [shared verification criteria](README.md#verification).

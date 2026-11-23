# 02 · Publication and update dates

Status: proposed. Implementation awaits user selection.

## Current behavior

The Greenhouse detail fallback puts updated_at into posting_date when first_published is missing. Readers can mistake an edit for a new opening. [Source](../../../skill/ultron/scripts/ultron/stages/fetch.py).

## Expected behavior

Represent publication, source update and local first observation separately wherever a date is displayed or used for freshness. Preserve the source semantics through extraction, storage and export. Treat historical dates of ambiguous provenance as unverified.

## Acceptance cases

- An update yesterday with no publication date appears as updated yesterday. Publication age remains unknown.
- A posting published in January and edited in March retains January as its publication date. Reimporting it leaves its first observation intact.

## Verification

Exercise the Greenhouse fallback, final result, ledger and displayed export with a fixed clock and legacy input. Apply the [shared verification criteria](README.md#verification).

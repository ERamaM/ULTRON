# 14 · Recover the saved vacancy for a CV audit

Status: proposed. Implementation awaits user selection.

## Current behavior

Local target resolution tries the ledger's latest run and a bounded list of recent runs. An older surviving snapshot can fall outside that scan. [Source](../../../skill/ultron/scripts/ultron/cv/ats_target.py).

## Expected behavior

Use recorded vacancy occurrences to locate surviving captures by stable identity. Prefer an explicitly supplied run, otherwise the latest usable exact match. Preserve offline operation and identify the snapshot used.

## Acceptance cases

- An older matching capture is found offline even after six unrelated searches and cleanup of the latest matching run folder.
- Two same-title vacancies with different identities remain distinct. If every capture was removed, report precisely what evidence is missing.

## Verification

Use a temporary ledger and run folders. Invoke the normal CV audit with network access disabled and check the selected run. Apply the [shared verification criteria](README.md#verification).

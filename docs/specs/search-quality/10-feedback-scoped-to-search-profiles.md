# 10 · Feedback scoped to search profiles

Status: proposed. Implementation awaits user selection.

## Current behavior

Relevance feedback is upserted by vacancy identity. A user's verdict for one search profile can overwrite a different profile's assessment. [Source](../../../skill/ultron/scripts/ultron/storage/review.py).

## Expected behavior

Store and retrieve relevance judgments for the selected profile and vacancy. Keep application state shared. Expose legacy unscoped feedback explicitly and preserve it during migration.

## Acceptance cases

- The same job can be relevant for backend and irrelevant for management. Updating either verdict preserves the other.
- Marking the job applied remains visible in both contexts. A legacy verdict survives migration without being attributed to a guessed profile.

## Verification

Exercise migration and repeated feedback through the jobs commands against two profiles sharing a vacancy. Apply the [shared verification criteria](README.md#verification).

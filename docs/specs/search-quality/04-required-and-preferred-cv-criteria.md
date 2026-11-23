# 04 · Required and preferred CV criteria

Status: proposed. Implementation awaits user selection.

## Current behavior

Target alignment currently scores the fraction of extracted terms present. Each term has equal weight regardless of how the employer described it. [Source](../../../skill/ultron/scripts/ultron/cv/ats_target.py).

## Expected behavior

Classify explicit requirements, preferences and unspecified mentions with their source text. Compare CV evidence separately for each class. Preserve uncertainty for ambiguous wording and alternatives such as SQL or Python.

## Acceptance cases

- For required SQL and desirable Tableau, distinguish two CVs that each cover one term. Identify which candidate meets the stated requirement.
- Treat one verified alternative as satisfying an OR requirement. A technology in the company description alone cannot become mandatory.

## Verification

Use labelled English and Spanish postings, including negations, optional skills and unclassified text. Verify the user-facing audit as well as extraction. Apply the [shared verification criteria](README.md#verification).

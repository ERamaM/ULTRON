# 09 · Evidence excerpts for ambiguous vacancies

Status: proposed. Implementation awaits user selection.

## Current behavior

Pending evaluations include the first 1500 body characters and a separate geography excerpt. A long introduction can displace decisive responsibilities or seniority requirements. [Source](../../../skill/ultron/scripts/ultron/stages/evaluate.py).

## Expected behavior

Within a bounded text budget, select labelled excerpts for responsibilities, experience, work policy and eligibility. Preserve original wording, negation and alternatives. Indicate when evidence was truncated.

## Acceptance cases

- Moving an explicit five-year requirement below a long company introduction keeps it visible to the evaluator.
- Preserve the whole clause in 'five years preferred, or equivalent practical experience'. A vacancy without a requirement yields no invented excerpt.

## Verification

Compare pending batches for paired short and padded postings. Measure decisive-clause recall at the same text budget. Apply the [shared verification criteria](README.md#verification).

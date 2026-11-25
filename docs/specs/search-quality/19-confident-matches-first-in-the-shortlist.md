# 19 · Confident matches first in the shortlist

Status: proposed. Implementation awaits user selection.

## Current behavior

The shortlist orders rows by verdict, eligibility and match score. [Source](../../../skill/ultron/scripts/ultron/core/ranker/final_rows.py). Rows judged `unsure` are exported with the confident ones.

In the [baseline comparison](README.md#baseline-comparison-of-2026-11-25), about half of each ULTRON shortlist was `unsure`. Blind labels put P@5 at 0.40 and P@10 at 0.33, against 1.00 and 0.97 for the agent without ULTRON. A fifth of the rows were not open to the location of the candidate, and half did not match the target role closely enough to apply first.

## Expected behavior

Order the shortlist so that the first rows are the ones the candidate should apply to first:

- A confident role match with confirmed eligibility ranks above every `unsure` row.
- Within a verdict, closeness to the target titles and focus of the profile decides the order before generic signals do.
- A row whose eligibility for the profile location is unknown or weak is marked as such and ranks below rows with confirmed eligibility.

Keep `unsure` rows in the export, visibly separated from confident ones.

## Acceptance cases

- A shortlist with confident and `unsure` rows lists every confident row first, whatever the match scores.
- Two confident rows differ only in title: one matches a target title and the other an adjacent role family. The target-title row ranks first.
- A row with a remote policy limited to another region, found after evaluation, is discarded with the existing eligibility reason and never exported.
- Results exported before this change still load and render.

## Verification

Add ranking cases to the labelled benchmark and report precision at 5 and at 10 before and after. Then repeat the baseline comparison: P@5, P@10 and the ineligible rate of the ULTRON arm must improve. Apply the [shared verification criteria](README.md#verification).

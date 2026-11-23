# 12 · Precision of CV experience dates

Status: proposed. Implementation awaits user selection.

## Current behavior

Year-only ranges are resolved to month intervals by convention. That assumption propagates into tenure, seniority and salary evidence. [Source](../../../skill/ultron/scripts/ultron/cv/experience.py).

## Expected behavior

Carry source precision alongside inferred intervals. Display approximate experience when input contains years alone and avoid certifying exact minimum tenure from an estimate. Preserve overlap handling.

## Acceptance cases

- A CV listing 2022 to 2024 produces an explicitly approximate duration. Supplying start and end months yields the corresponding precise calculation.
- Two overlapping approximate roles cannot produce double-counted experience. A threshold inside the plausible range is marked uncertain.

## Verification

Use a fixed reference date and compare the experience parser, salary analysis and strict duration checks on the same timeline. Apply the [shared verification criteria](README.md#verification).

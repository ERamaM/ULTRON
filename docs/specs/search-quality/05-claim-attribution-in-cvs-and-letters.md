# 05 · Claim attribution in CVs and letters

Status: proposed. Implementation awaits user selection.

## Current behavior

The existing verifiers check individual claims against source facts. Combining authentic facts across employers can create an unsupported statement. [Source](../../../skill/ultron/scripts/ultron/cv/truth.py).

## Expected behavior

Bind measurable claims to their employer, role, outcome and unit when these are identifiable. Allow faithful paraphrases. Flag unsupported combinations and surface ambiguous attribution for review in both document workflows.

## Acceptance cases

- Given a 20 percent cost reduction at A and Kubernetes work at B, flag a draft attributing that reduction to Kubernetes at B.
- Accept the same reduction paraphrased under A. A confirmed fact that explicitly links the elements can support the combined claim.

## Verification

Run matched positive and negative fixtures through both strict verification entry points. Verify that a warning identifies the draft sentence without exposing the CV in logs. Apply the [shared verification criteria](README.md#verification).

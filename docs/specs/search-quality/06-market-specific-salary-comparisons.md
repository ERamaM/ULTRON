# 06 · Market-specific salary comparisons

Status: proposed. Implementation awaits user selection.

## Current behavior

Salary mining selects one hit per posting before computing currency summaries. Two different labour markets can share a currency. [Source](../../../skill/ultron/scripts/ultron/salary/estimator.py).

## Expected behavior

Preserve each disclosed band's market, currency, period and compensation basis when stated. Select compatible observations for the requested market before aggregation. Report distinct vacancies behind each comparable group.

## Acceptance cases

- Permuting the Spanish and German EUR bands in a posting leaves the Spanish estimate and sample count unchanged.
- Keep hourly and annual disclosures distinguishable. An unassigned band from a multinational advert stays unresolved when its market cannot be inferred.

## Verification

Use paired order-reversal fixtures and independently calculated percentiles. Check the salary analysis and its report. Apply the [shared verification criteria](README.md#verification).

# Search and career quality specifications

Specifications 01 to 16 expand the eight improvements identified during the product review. Specifications 17 to 20 come from the [baseline comparison of 2026-11-25](#baseline-comparison-of-2026-11-25). Numbers identify specifications and do not imply delivery order. Each proposal can be selected independently. Specifications 06 and 07 share the salary observation model.

Specifications 17, 18 and 20 patch the current pipeline. The [agent-first redesign](../agent-first/README.md) supersedes them and covers 19: implement the redesign instead.

- [01 · Unique vacancies per board](01-unique-vacancies-per-board.md)
- [02 · Publication and update dates](02-publication-and-update-dates.md)
- [03 · Reliable board yield history](03-reliable-board-yield-history.md)
- [04 · Required and preferred CV criteria](04-required-and-preferred-cv-criteria.md)
- [05 · Claim attribution in CVs and letters](05-claim-attribution-in-cvs-and-letters.md)
- [06 · Market-specific salary comparisons](06-market-specific-salary-comparisons.md)
- [07 · Historical salary observations](07-historical-salary-observations.md)
- [08 · Coverage of unattended searches](08-coverage-of-unattended-searches.md)
- [09 · Evidence excerpts for ambiguous vacancies](09-evidence-excerpts-for-ambiguous-vacancies.md)
- [10 · Feedback scoped to search profiles](10-feedback-scoped-to-search-profiles.md)
- [11 · New vacancies per saved search](11-new-vacancies-per-saved-search.md)
- [12 · Precision of CV experience dates](12-precision-of-cv-experience-dates.md)
- [13 · Application language for cover letters](13-application-language-for-cover-letters.md)
- [14 · Recover the saved vacancy for a CV audit](14-recover-the-saved-vacancy-for-a-cv-audit.md)
- [15 · Backfill company verification slots](15-backfill-company-verification-slots.md)
- [16 · Preserve evidence from repeated web hits](16-preserve-evidence-from-repeated-web-hits.md)
- [17 · Profile-fit seed selection](17-profile-fit-seed-selection.md)
- [18 · Role-first discovery through aggregators](18-role-first-discovery-through-aggregators.md)
- [19 · Confident matches first in the shortlist](19-confident-matches-first-in-the-shortlist.md)
- [20 · Relevant postings on large boards](20-relevant-postings-on-large-boards.md)

## Baseline comparison of 2026-11-25

A live comparison ran ULTRON 0.4.1 (`balanced`) against the same agent searching with plain web search and no ULTRON. It followed the [baseline comparison protocol](../../self-improvement.md#baseline-comparison-ultron-against-an-agent-without-it): one senior AI engineering profile, remote in the EU, three repetitions per arm, the top 25 rows scored, blind labels and a user spot check of 20 jobs with no disagreement. The pool held 71 jobs, 44 of them relevant.

| Metric (mean of 3 repetitions) | Agent without ULTRON | ULTRON |
|---|---|---|
| Precision | 0.73 | 0.38 |
| P@5 / P@10 | 1.00 / 0.97 | 0.40 / 0.33 |
| Relevant jobs per run | 18 | 7.3 |
| Relevant jobs over the 3 runs | 36 | 9 |
| Ineligible rate | 0% | 20% |
| Role mismatch rate | 27% | 51% |
| Tokens per run | 161k | 215k |
| Seconds per run | 705 | 1296 |
| Tokens per relevant job | 9.0k | 29.3k |
| Reproducibility between runs (Jaccard) | 0.25 | 0.81 |

The two arms barely overlap (Jaccard 0.04): 8 of the 9 relevant jobs ULTRON found were missed by the other arm.

The 35 relevant jobs only the other arm found were traced through the three ULTRON runs, 105 cases in all:

| What ULTRON did with the job | Cases | Specification |
|---|---|---|
| Never queried the employer | 86 | [17](17-profile-fit-seed-selection.md), [18](18-role-first-discovery-through-aggregators.md) |
| Queried the employer, but the posting never became a candidate | 12 | [20](20-relevant-postings-on-large-boards.md) |
| Discarded it as a duplicate role | 6 | [01](01-unique-vacancies-per-board.md) |
| Discarded it as not remote in the EU | 1 | none |

The misses are a discovery problem, not a filtering one. The quality of what ULTRON does export is covered by [19](19-confident-matches-first-in-the-shortlist.md).

Limits of this measurement: it is one profile, in a field where remote-job aggregators are well stocked. The labellers ran on the same model as the agent without ULTRON. Pool recall counts the jobs either arm found, not the whole market.

If the redesign is not adopted, the suggested order is 17 first, since it needs no new network traffic, then 18, 19 and 20, repeating the comparison after each one.

## Verification

For each selected proposal, reproduce the acceptance cases on the current main revision, record the baseline and exercise the public workflow after implementation. Use synthetic fixtures for CVs, salary disclosures and job history. Preserve existing output contracts for older data. For changes to search quality, run the [labelled benchmark and regression gates](../../self-improvement.md). Report measured deltas and identify any unverified live behavior.

Specifications 17 to 20 also repeat the baseline comparison with `tools/arm_comparison.py` and report its numbers next to the table above. That comparison is live evidence: it supports a change but does not replace the benchmark gates.

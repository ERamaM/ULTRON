# 24 · Agent judgment decides

Status: proposed.

## Current behavior

`ultron filter` discards postings with heuristic rules for role family, seniority, remote policy and geography before the agent sees them. The agent evaluates a queue of ambiguous postings, and the rest keep the heuristic verdict. [Source](../../../skill/ultron/scripts/ultron/stages/filter.py). The shortlist is ordered by verdict, eligibility and match score. [Source](../../../skill/ultron/scripts/ultron/core/ranker/final_rows.py).

In the comparison, a fifth of the exported rows were not open to the location of the candidate, half were weak role matches, and P@5 was 0.40 against 1.00 for the free agent.

## Expected behavior

In a scout run, the agent decides and the rules only help it:

- **Hard facts only.** `filter` discards a posting only when it is closed or expired, a duplicate, or unusable (no title or body). Every other posting goes to the evaluation queue.
- **Heuristics order the queue.** The heuristic verdict and signals are attached as hints and decide the order of the queue: postings the agent found itself first, then expansion postings by rank score. They never discard.
- **A bounded queue.** The preset caps the queue. Postings beyond the cap are reported as `not_evaluated` and are not exported, so the agent never exports a posting it has not read.
- **Structured evidence.** Each pending item gives title, company, location, remote text, posted date, salary when stated, the excerpts that mention remote policy and eligibility, and the hints.
- **The agent sets the order.** A verdict carries `keep`, `discard` or `unsure`, the existing evidence fields and a `priority` from 1 (apply first) to 3 (apply last). The shortlist orders by verdict, then priority, then eligibility. Match score only breaks ties.

`unsure` rows stay in the export, after every `keep`.

## Acceptance cases

- In a scout run, a posting the heuristics would discard as `non_matching_role_family` or `non_eu_remote` reaches the evaluation queue with that reason as a hint.
- A closed posting, and the second copy of a duplicate, are discarded without reaching the queue.
- Two kept rows with priorities 1 and 3 are exported in that order, whatever their match scores.
- A kept row with no priority reads as priority 2, so verdict files written for the default mode still record.
- A queue longer than the cap reports the count of `not_evaluated` postings, and none of them is in `results.json`.
- The default mode still filters and ranks as today, and the labelled benchmark and parity suite are unchanged.

## Verification

Unit-test the scout filter, the queue order and cap, verdict recording and ranking. Add ranking cases to the labelled benchmark. Live, P@5, P@10 and the ineligible rate must be at least as good as the free agent. Apply the [shared verification](README.md#verification).

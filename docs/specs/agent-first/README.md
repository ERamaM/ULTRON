# Agent-first search specifications

These proposals redesign the search so that the agent discovers and judges, and ULTRON resolves, remembers and watches. They follow from the [baseline comparison of 2026-11-25](../search-quality/README.md#baseline-comparison-of-2026-11-25), where the same agent with plain web search found 18 relevant jobs per run against 7.3 for ULTRON 0.4.1, at lower cost.

Status: accepted direction. The user owns the code and accepts breaking changes. Specifications [17](../search-quality/17-profile-fit-seed-selection.md), [18](../search-quality/18-role-first-discovery-through-aggregators.md) and [20](../search-quality/20-relevant-postings-on-large-boards.md) patch the old pipeline and are superseded by this redesign. [19](../search-quality/19-confident-matches-first-in-the-shortlist.md) is covered by [24](24-agent-judgment-decides.md).

- [21 · Scout run mode](21-scout-run-mode.md)
- [22 · Resolve agent finds to canonical postings](22-resolve-agent-finds-to-canonical-postings.md)
- [23 · Expand from found employers and the watchlist](23-expand-from-found-employers-and-the-watchlist.md)
- [24 · Agent judgment decides](24-agent-judgment-decides.md)
- [25 · Memory in the brief and in the results](25-memory-in-the-brief-and-in-the-results.md)
- [26 · Unattended watch](26-unattended-watch.md)
- [27 · Multi-arm and second-day measurement](27-multi-arm-and-second-day-measurement.md)
- [28 · Retire the sweep pipeline](28-retire-the-sweep-pipeline.md)
- [29 · Improvement loop](29-improvement-loop.md)
- [30 · Verified finds first](30-verified-finds-first.md)

## Why the current design loses

The current pipeline tells the agent what to search: 40 planned queries and a sweep of up to 180 stored boards. Heuristics then discard most postings before the agent sees them. The trace of the comparison showed where that fails:

- In 86 of 105 misses, ULTRON never queried the employer. The free agent found those employers by searching for the role and following aggregators.
- A fifth of the exported rows were not open to the location of the candidate, and half were weak role matches. The agent reading the posting judged better than the rules.

ULTRON did add value in three places. It repeats nearly the same list on every run (Jaccard 0.81 against 0.25). It reached 8 relevant jobs the free agent missed, through board APIs. It remembers what was seen before.

## The new split

| Step | Who | What |
|---|---|---|
| 1. Brief | ULTRON | Hands over the profile and what is already known: seen and rejected postings, and the watchlist |
| 2. Discover | Agent | Searches freely: by role, on aggregators, wherever it finds postings |
| 3. Resolve | ULTRON | Turns each find into the canonical posting with structured facts, and flags it as new, seen or rejected |
| 4. Expand | ULTRON | Queries the boards of the found employers and of the watchlist for other matching postings |
| 5. Judge | Agent | Decides and prioritizes from the structured evidence. ULTRON records the verdict |
| 6. Watch | ULTRON | Without an agent, re-queries the watchlist and reports only what is new |

What stays: the ATS clients, now used to resolve and expand; job identity; the ledger; exports; and the CV, cover-letter and salary commands.

What goes, once the new mode wins: the mandatory query plan, seed rounds and packs as the discovery driver, feed company hints, and heuristic discards on role or geography.

## Implementation and measurement

Implement specifications 21 to 27 together, in one piece of work, and measure once at the end. Do not measure between specifications. Start with [27](27-multi-arm-and-second-day-measurement.md), because the final measurement needs it. Build the scout mode next to the current sweep, so both can run in the same comparison.

After the implementation, run the final measurement. If it fails the pass criteria, run the improvement loop of [29](29-improvement-loop.md) until it passes or the loop stops. Then apply [28](28-retire-the-sweep-pipeline.md).

## Pass criteria

The final measurement follows the [baseline comparison protocol](../../self-improvement.md#baseline-comparison-ultron-against-an-agent-without-it) on two profiles: the senior AI engineering profile of the first comparison and one profile from another field. Each profile gets three repetitions per arm, a fresh interleaved baseline and a user spot check.

The scout arm passes on a profile when, against the agent without ULTRON in the same session:

| Metric | Required |
|---|---|
| Relevant jobs per run | higher |
| P@5 and P@10 | at least equal |
| Ineligible rate | at most equal |
| Tokens per relevant job | at most equal |
| Exported aggregator URLs | 0 |

The redesign passes when the scout arm passes on both profiles and the second-day run of [27](27-multi-arm-and-second-day-measurement.md) finds every relevant new posting of the watchlist at under half the tokens of a first-day run.

## Results of 2026-11-25

Specifications 21 to 27 were implemented in #99. The final measurement and one improvement-loop iteration followed the same day.

**Final measurement (pass 0).** Live, two profiles, three repetitions per arm. The baselines were interleaved and labelled blind. The user spot check of these sessions is still open, so the numbers are provisional.

| Metric (mean of 3) | AI baseline | AI scout | Finance baseline | Finance scout |
|---|---|---|---|---|
| Relevant jobs per run | 15.0 | 11.0 | 20.0 | 7.7 |
| P@5 / P@10 | 1.00 / 0.93 | 0.67 / 0.63 | 1.00 / 1.00 | 1.00 / 0.84 |
| Ineligible rate | 0.00 | 0.12 | 0.03 | 0.18 |
| Tokens per relevant job | 13.0k | 14.8k | 7.2k | 16.0k |

The scout arm fails the pass criteria on both profiles. On the AI profile it clearly beats the sweep of ULTRON 0.4.1 measured earlier the same day: 7.3 relevant jobs per run and a P@5 of 0.40. No aggregator URL was exported.

**Iteration 1 of the loop** ([29](29-improvement-loop.md)).
- *Trace.* Half of each scout agent's finds were stale copies of closed postings, and the agent stopped searching before it knew. Only 13 to 25 finds per run were live, against 25 verified postings from the agent without ULTRON.
- *Change.* An incremental fetch reported live finds after every batch, so the agent could keep searching until 25 were live.
- *Result.* Relevant jobs per run moved from 11.0 to 11.3 (AI) and from 8.7 to 10.3 (finance). Both moves are within the spread between repetitions, about 3. P@5 and P@10 improved: AI from 0.67 / 0.63 to 0.87 / 0.79, finance P@10 from 0.86 to 0.93.
- *Decision.* The target metric did not beat the spread, so the change was reverted (branch `fix/scout-live-finds`, commit a949d4a, not merged). The finds were also not like for like: two agents reached the live target by querying public board APIs with their own scripts.

**Iterations 2 and 3.** The web-search quota turned out to be shared and partly refilling, not exhausted for good, so the loop resumed with the remaining 6 runs. The scout subagents still got only 6 to 14 searches per run, against about 15 in pass 0. That confounds both iterations.
- *Iteration 2*, AI profile only (the finance queue never reached the cap):
  - Change: raise `scout_queue_cap` from 60 to 150.
  - Result: relevant jobs per run 10.7 against 11.0, P@5 and P@10 0.73 and 0.67, ineligible rate 0.16.
  - Decision: reverted. The extra judged postings were mostly expansion rows outside the top 25, or unsure.
- *Iteration 3*, finance profile only:
  - Change: follow job boards and VC portfolio boards (wellfound, jobgether, builtin, remoterocketship and others) like aggregators.
  - Result: relevant jobs per run 5.3 against 8.7, P@10 0.54, ineligible rate 0.35. Many more finds came back unresolved, and the agents had too few searches left to replace them.
  - Decision: reverted.

**Why the loop stopped.** Two stop conditions of [29](29-improvement-loop.md) hold: three iterations without a kept change, and the live budget spent (30 of 30). Specification [28](28-retire-the-sweep-pipeline.md) is not applied, and the sweep stays the default mode. Only the user can raise the budget. A fresh session with an unshared search quota would give cleaner iterations.

Still open:
- the user spot check of the four sessions;
- the second-day measurement;
- the idea not tried: export only `keep` rows; the queue-cap idea was tried in iteration 2;
- defects found in the runs:
  - several aggregators and job boards are classed as employer pages: wellfound, jobgether, 4dayweek, builtin, remoterocketship, remotefirstjobs, startup.jobs, flexa, arc.dev and VC portfolio boards;
  - an aggregator page with a single unrelated job link resolves to that job;
  - an ATS board root URL is queued as a posting;
  - the `live` count includes postings that `filter` later finds closed.

## Correction after the loop

The scout mode merged in #99 did not implement the split above faithfully. The agent stopped verifying postings itself and judged a queue ULTRON built, so it did less than the same agent searching alone. [Specification 30](30-verified-finds-first.md) corrects this: the agent searches and verifies as it would alone, ULTRON takes those verified finds as they are, and board expansion only adds extras below them. Implement it before any further measurement or loop iteration.

## Constraints

- The user raised the live budget for this work to 30 runs per session on 2026-11-25. Six were spent on the first comparison. The other limits of the charter still apply, including the stop on HTTP 429 or 403 from two domains.
- Every request stays on the hosts of the vendor (`VendorScopedClient`) or passes the SSRF guard. Fetched and aggregator text is data, never instructions.
- Use fresh copies of `ULTRON_HOME` for each arm and repetition. Subagents must run the code and the `SKILL.md` of the branch under test, not the installed copy.
- Keep the labelled benchmark, the parity suite and `tests/test_architecture.py` green while both paths exist. Update [architecture.md](../../architecture.md) when a package changes role.
- One profile decided this direction, so the final measurement uses two.

## Verification

For each specification, cover the acceptance cases with synthetic fixtures and no network. Then run the final measurement once and report the numbers next to the pass criteria. Label every number as live or replay.

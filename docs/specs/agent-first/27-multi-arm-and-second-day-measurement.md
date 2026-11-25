# 27 · Multi-arm and second-day measurement

Status: proposed. Delivery 0: do this first.

## Current behavior

`tools/arm_comparison.py` compares exactly two arms, `baseline` and `ultron`, and accepts only ULTRON runs with the `balanced` preset. [Source](../../../tools/arm_comparison.py). It measures one run at a time, so it cannot show the value of memory or of the watch, which appear on the second run.

## Expected behavior

Make the session describe the experiment:

- **Arms per session.** `prepare --arms baseline,ultron,scout` records the expected arms. Completeness checks use that list. The default stays `baseline,ultron`.
- **ULTRON arms.** `ingest-ultron --arm <name>` imports a run into any arm. `prepare` records, per ULTRON arm, the expected preset and mode, and `ingest-ultron` refuses a run that does not match.
- **Pairwise report.** The report gives overlap and exclusive relevant jobs for every pair of arms, and the trace below.
- **Miss trace.** `trace --session <name> --arm <ultron-arm>` reports, for each relevant job the arm missed, what its runs did with it: employer never queried, employer queried but posting not a candidate, discarded with stage and reason, not evaluated, or evaluated and rejected. This is the analysis done by hand for the first comparison.
- **Second-day measurement.** A session can hold a second phase for an arm. The second phase runs on the `ULTRON_HOME` left by the first, one day later or after a simulated board change. The report gives, for the second phase: new relevant jobs found, relevant new jobs missed, tokens and seconds, each next to the first phase.

Document in [self-improvement.md](../../self-improvement.md) that subagents must run the code and `SKILL.md` of the branch under test, with fresh copies of `ULTRON_HOME`, and how to pass both to the subagent.

## Acceptance cases

- A session prepared with three arms refuses to score as complete while one arm has fewer than three repetitions, and scores all three when complete.
- `ingest-ultron --arm scout` refuses a `balanced` run of the default mode when the session expects scout mode.
- A session prepared without `--arms` behaves exactly as today.
- `trace` on a fixture with one job per outcome reports each outcome once, with the stage and reason where there is one.
- A second-phase repetition is scored separately and never mixed into the first-phase means.
- All session files stay in the reports folder, and no posting or profile data is written to the repository.

## Verification

Extend the tool tests with three-arm and two-phase fixtures. Re-score the recorded two-arm session and confirm identical numbers. Apply the [shared verification](README.md#verification).

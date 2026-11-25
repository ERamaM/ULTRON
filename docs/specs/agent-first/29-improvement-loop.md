# 29 · Improvement loop

Status: proposed. Run it only when the final measurement fails the pass criteria.

## Current behavior

The [self-improvement charter](../../self-improvement.md) governs sessions that run live searches and change the code. It gates quality changes on the labelled benchmark, which replays the filter and finalize stages. It has no procedure for improving a result measured by the live baseline comparison, where the open questions are discovery, resolution and the instructions the agent follows.

## Expected behavior

When the scout arm fails a [pass criterion](README.md#pass-criteria), improve it in measured iterations. Each iteration changes one thing and has these steps:

1. **Trace.** Run `trace` from [27](27-multi-arm-and-second-day-measurement.md) on the failing profile. Group the misses and the bad rows by cause: never searched, find not resolved, employer not expanded, posting not a candidate, not evaluated, wrong verdict, wrong priority, or cost.
2. **Generate ideas.** Write at least three candidate changes for the largest cause. For each one, state the expected effect on a named metric and how the trace would show it. Changes may touch the code, the brief, or `SKILL.md` and its references.
3. **Choose one.** Pick the idea with the largest expected effect for the least risk. Record the others as not tried.
4. **Implement.** Test first for code. Keep `ruff`, the full test suite, the labelled benchmark and the parity suite green.
5. **Measure.** Re-run only the scout arm on the affected profile, three repetitions on fresh copies of `ULTRON_HOME`. Pool them with the baseline of the same session when it is from the same day. Otherwise run a fresh baseline. Label the new jobs blind.
6. **Decide.** Keep the change when the target metric improves by more than the spread between repetitions and no pass criterion gets worse. Otherwise revert it and record the result.

Keep a log in the reports folder with one entry per iteration: the cause, the ideas, the choice, the numbers before and after, and the decision. Summarize it in the PR. The log holds no posting or profile data.

The loop stops at the first of these:

- **Success.** The scout arm passes every criterion on both profiles.
- **Three iterations** without a kept change.
- **Two failed attempts** on the same cause. Mark it blocked and take the next cause.
- **Budget.** The live budget in the [constraints](README.md#constraints) is spent. Stop and ask the user: only the user can raise it.
- **Emergency stop** of the charter: HTTP 429 or 403 from two domains, or a red suite that the attempt does not recover.

Never tune rule terms, thresholds or weights by eye from live runs. Those still go through the labelled benchmark, as the charter requires. This loop is for discovery, resolution, expansion, the queue and the instructions.

When the loop stops without success, report which criteria still fail, the blocked causes and the ideas not tried. Do not apply [28](28-retire-the-sweep-pipeline.md).

## Acceptance cases

- A failed final measurement produces a trace grouped by cause before any change is made.
- Every iteration in the log has at least three ideas, one choice, and before and after numbers for the target metric.
- A change that does not beat the spread between repetitions is reverted, and the revert is in the history.
- No iteration changes more than one thing, and no iteration re-runs an arm or profile the change did not touch.
- The count of live runs is tracked in the log, and the loop stops when the budget is reached.
- The charter documents this loop next to the baseline comparison protocol.

## Verification

Check the log against the commit history and the session reports: each kept change has its measurement, and each reverted change has its revert. Confirm the run count against the budget. Apply the [shared verification](README.md#verification).

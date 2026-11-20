# Self-improvement charter

This charter governs agent sessions that run real ULTRON searches, triage what goes wrong and
change the code. It exists because such a loop has two failure modes: tuning quality to the
noise of a handful of live searches, and burning job-board rate limits through retries. Every
session follows the gates and stop conditions below. Only the user can raise a budget, a pass
limit or an attempt limit. The loop never decides to on its own.

## The loop

A **pass** runs the session's scenarios (live searches with fixed profiles and presets), triages
the results and applies fixes. A session has **at most 3 passes**.

1. Run the pending scenarios. In pass 1 that is all of them. Later passes run only scenarios that
   failed or that a fix touched.
2. Triage every failing or anomalous scenario from its archived artifacts (run folder, JSONL
   files, ledger snapshot). Classify each finding:
   - **ROBUSTNESS**: a crash, an unhandled error, a hang, a timeout handled badly, or run files
     and the ledger disagreeing.
   - **QUALITY**: irrelevant postings kept, relevant postings lost, poor ranking, or a weak
     search query.
   - **EXTERNAL**: a remote site down, blocking us or changing its format, with no ULTRON
     defect. Document it. Do not fix it.
3. Apply fixes under the gates below.
4. Check the stop conditions. If none applies, start the next pass.

## Gates by finding type

### ROBUSTNESS: fix in this session

1. Test first. Write a test that reproduces the failure with synthetic or recorded data (no
   network) and fails on the unchanged code. Then fix it and see the test pass.
2. `uv run ruff check` and the full `uv run pytest` pass after every fix.
3. One `fix:` commit per finding, naming the scenario.
4. Re-run only the affected scenario live, within the live budget.

### QUALITY: only through the labelled benchmark

1. Never tune thresholds, rule terms, queries or weights by eye from what a few live searches
   showed. Live searches produce hypotheses. The labelled benchmark confirms or rejects them.
2. Validate each hypothesis offline. Run `tools/quality_benchmark.py compare` against the
   committed baseline and `tools/measure_quality.py` against the 60-case pin. Accept the change
   only if it passes the gates. Otherwise discard it and document the result. Never iterate on
   tuning against live sites.
3. Accept at most **2 quality changes per session**. Apply them one at a time and re-measure
   the benchmark between them. Never stack them unmeasured.
4. Re-verify an accepted quality change live only if live budget remains. Otherwise the
   benchmark evidence is enough.

### EXTERNAL: document only

Record the domain, the symptom and the evidence in the session report. It is not a ULTRON
failure and it does not consume fix attempts.

## Stop conditions

Check them after every fix and at the end of every pass.

**Success.** Every scenario passes and no ROBUSTNESS finding is open. Unconfirmed quality
hypotheses are documented and do not block the end of the session.

**Exhaustion** (whichever comes first):

- 3 passes are complete. Remaining failures are documented as `blocked` with evidence.
- **2 failed fix attempts** on the same finding. It becomes `blocked`, there is no third attempt,
  and the loop continues with the other findings.
- The live budget is spent. Scenarios that could not be re-run are `unverified-live`. A fix with
  a failing-then-passing test and a green suite is accepted on that evidence.
- **Dry pass.** A full pass with no new finding and no scenario changing state ends the session.

**Emergency stop** (immediately, without finishing the pass):

- HTTP 429, HTTP 403 or a captcha from **2 or more distinct domains** stops all live runs for
  the session. Work continues offline only (replay and benchmark).
- The full suite turns red and the current attempt does not recover it. Revert the commit that
  broke it, document it and stop.
- The user withdraws consent for network access.

## Live budgets

- At most **3 live runs per scenario** and **15 live runs per session**.

## Experiment hygiene

A/B comparisons must change one thing only: the code.

- **Snapshot `ULTRON_HOME` per arm.** Runs mutate the user state: `discover feeds` auto-seeds
  companies, and finalize records `seed_yield_runs` and job history in the ledger. A second arm
  that starts from the first arm's state is contaminated. Copy the starting `ULTRON_HOME`, then
  give each arm its own copy (`ULTRON_HOME=<copy-a>`, `ULTRON_HOME=<copy-b>`). Never point an
  experiment at the user's real `~/.ultron`.
- **Keep live and replay separate.** Live runs measure the whole system against a moving world.
  Replays measure a code change over identical inputs. Never mix the two in one comparison, and
  label every number as live or replay in the report.
- **Prefer paired replays.** To compare two versions of the decision code, replay the same run
  with each version (see below) instead of running two live searches.

## Measurement tools

The tools live in `tools/` at the repository root. They are developer tools: they are not part
of the `ultron` CLI or the installed skill. None of them uses the network, an agent, the clock
or the real `ULTRON_HOME`. Replays run the real `filter` and `finalize` stages in a throwaway
`ULTRON_HOME` that is deleted afterwards. Reports are always written to and read from one
fixed folder, `<system temp>/ultron-eval`. On the command line a report is a plain file name
(`--output candidate.json`), never a path. A name with a folder or `..` is rejected with exit
code `2`. The tool prints where it wrote the report.

### Labelled benchmark (120 cases)

```sh
uv run python tools/quality_benchmark.py validate
uv run python tools/quality_benchmark.py run --output candidate.json
uv run python tools/quality_benchmark.py compare [candidate.json]
```

`compare` gates against the committed `tests/fixtures/benchmark/baseline.json`. With no
candidate name it replays the corpus itself. Exit codes: `0` all
gates passed, `1` regression, `2` invalid corpus or report.

The gates are:

- Global precision, recall or F1 dropping by more than 1 point is a regression.
- In a slice (language, region, vendor, role track) with at least 10 labelled cases, precision,
  recall or F1 dropping by more than 5 points is a regression. Smaller slices are reported as
  `insufficient_sample` and are never presented as an improvement.
- A relevant case that the baseline kept and the candidate loses is a regression ("new false
  negative"), unless it is justified with `--allow-case <id>` and explained in the PR.

The report also gives the false-negative rate, P@5, P@10, recall@10, MRR, NDCG@10, the number
of postings routed to the agent queue, and the first losing stage of every relevant case that
did not end as a confident keep. That stage is a filter stage (`pre_filter`,
`heuristic_evaluator`, `hard_filter`), a finalize stage (`evaluator`, `post_eval_geo`, `dedup`,
`final_cap`), or `unsure_in_shortlist`.

The corpus in `tests/fixtures/benchmark/` is synthetic. It came from the deterministic template
generator of the MARK II project: every text is templated, every URL is on
`jobs.example.test`, and no case describes a real person or company. It maps to ULTRON's
pipeline as follows:

| Corpus | ULTRON |
|---|---|
| fixture `title`, `location`, `remote_text`, `body_text`, `is_open_hint` | `ExtractedJob` fields; `apply_available` follows `is_open_hint` |
| fixture `apply_url` | `apply_url` and identity; the company is `Synthetic Employer <case id>` |
| `vendor` | a vendor-API posting (`source_type: ats_api`), so the ATS-domain allowlist does not apply; the vendor is only a slice |
| `profile: ml-engineering-remote-eu` | the bundled example profile `tests/fixtures/profiles/ai-ml-remote-eu.yaml` |
| `remote_mode` | the `region` slice |
| `labels.relevant: true / false` | positive / negative; `null` is left out of the binary metrics |
| prediction | the shortlist after `filter` + `finalize` with the `balanced` preset and no agent: a `keep` row is a positive, an `unsure` row is an abstention, anything else is a negative |
| ranking | the shortlist order finalize produced |

The labels are human-owned. No tool writes them. Change a label only by hand, with the reason
in the PR, and never to match what the code currently outputs.

To update the baseline after an accepted change, run
`uv run python tools/quality_benchmark.py run --update-baseline` and put the before/after
metrics in the PR.

### 60-case confusion pin

```sh
uv run python tools/measure_quality.py [--output quality.json]
```

This runs the parity corpus (`tests/fixtures/heuristic_characterization_corpus.json`) through
`characterize_job` as the `filter` stage calls it. It counts kept, unsure and discarded per
synthetic label (`tests/fixtures/algorithm_quality_labels.json`) and the match-score band per
label. `tests/eval/test_measure_quality.py` pins these counts. To change the pin on purpose, run
the tool, copy the new counts into `PINNED_COUNTS`, `PINNED_RATES` and `PINNED_BANDS`, and
explain in the PR which cases moved and why. Never edit the labels to make the pin pass.

### Paired replay of a past run

```sh
uv run python tools/replay_run.py replay --run <run-id> --output arm-a.json
# switch to the other branch, then:
uv run python tools/replay_run.py replay --run <run-id> --output arm-b.json
uv run python tools/replay_run.py diff arm-a.json arm-b.json
```

`replay` reads a run's `run.json`, `postings.jsonl` and, unless `--no-evaluations` is given,
the recorded agent `evaluations.jsonl`. It re-runs the current code's `filter` and `finalize`
and reports, for each identity, the recorded decision next to the replayed one (`keep`,
`unsure`, `discard`). `changed_vs_recorded` lists the identities whose decision changed. The
run is looked up by id under `ULTRON_HOME/runs`. To replay a run from a snapshot, set
`ULTRON_HOME` to the snapshot. The run folder and `ULTRON_HOME` are only read. A run file that
resolves outside its folder through a symlink is rejected.

To compare two code versions, replay the same run on each branch and `diff` the two reports.
The diff counts transitions such as `keep->discard` and lists each changed identity. Recorded
agent evaluations are reused only for identities still in the filter output, as finalize does.
An identity that newly becomes ambiguous falls back to its heuristic verdict.

Replay reports name a run's postings by identity key and reason only. They are still run
artifacts: keep them out of the repository, PRs and issues. The reports folder is outside the
repository for this reason.

### Current baseline

Measured on this charter's introduction (`balanced` preset, no agent):

| Benchmark | Value |
|---|---|
| 120-case precision / recall / F1 | 0.750 / 0.300 / 0.429 (tp 3, fp 1, fn 7, tn 60) |
| 120-case P@5 / P@10 / MRR / NDCG@10 | 0.400 / 0.300 / 0.333 / 0.278 |
| 120-case first losing stage | `heuristic_evaluator` 6, `pre_filter` 1 |
| 60-case relevant | 14 kept, 0 unsure, 3 discarded |
| 60-case not relevant | 5 kept, 0 unsure, 16 discarded |
| 60-case ambiguous | 6 kept, 3 unsure, 13 discarded |

Known limits of the 120-case corpus:

- Only 10 cases are labelled relevant, so one case moves recall by 10 points. Several slices with
  10 or more labelled cases have no relevant case at all.
- `relevant` ignores seniority. 3 of the 10 relevant cases are manager-level roles, but the
  example profile excludes managers. Two of the misses are these cases.
- `eligibility_label` does not appear in the fixture text.
- Spanish core titles are templated as "Senior Ingenieria Machine Learning". Five of the seven
  misses are these titles failing role-family matching, which may be an artifact of the
  template.

Review these labels by hand before reading recall as an absolute number. As they are, they work
as a regression gate.

## Vendor no-go decisions

The MARK II project closed a feasibility review of these vendors as no-go. Do not add or extend
them. Without a stable public endpoint they would need logins, tokens, a captcha solver or a
headless browser, and ULTRON uses none of these. The existing Workday, Taleo and iCIMS clients
are unaffected.

| Vendor | Decision | Main reason |
|---|---|---|
| SAP SuccessFactors | not added | No stable public endpoint and no permissive terms |
| Workday keyword search | not added | No stable public contract; restrictive terms |
| Taleo pagination | not added | No permissive terms; may require a login; limited HTML coverage |
| Oracle Recruiting Cloud | not added | Its REST endpoint is documented for Oracle's internal use only |
| iCIMS search | not added | No public unauthenticated API and no permissive terms |

Reopen a decision only with evidence of a public, unauthenticated, bounded API and terms that
allow automated access.

## Session report

Every session ends with this table, plus the live budget used (runs, requests per domain),
each quality hypothesis with its benchmark result (accepted or discarded, with metrics), the
`blocked` and EXTERNAL findings, and the final `pytest` and `ruff` status.

| Scenario | Finding | Type | Root cause | Fix (commit) | Verification evidence | Status |
|---|---|---|---|---|---|---|

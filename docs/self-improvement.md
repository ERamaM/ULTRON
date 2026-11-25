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

### Labelled benchmark (216 cases)

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
- In a slice (language, region, vendor, role track, profile) with at least 10 labelled cases, precision,
  recall or F1 dropping by more than 5 points is a regression. Smaller slices are reported as
  `insufficient_sample` and are never presented as an improvement.
- A relevant case that the baseline kept and the candidate loses is a regression ("new false
  negative"), unless it is justified with `--allow-case <id>` and explained in the PR.

The report also gives the false-negative rate, P@5, P@10, recall@10, MRR and NDCG@10 per profile
(ranks are only comparable within one profile's replay), the number of duplicate shortlist rows,
the number of postings routed to the agent queue, and the first losing stage of every relevant case that
did not end as a confident keep. That stage is a filter stage (`pre_filter`,
`heuristic_evaluator`, `hard_filter`), a finalize stage (`evaluator`, `post_eval_geo`, `dedup`,
`final_cap`), or `unsure_in_shortlist`.

The corpus in `tests/fixtures/benchmark/` is synthetic. Its first 120 cases came from the
deterministic template generator of the MARK II project; 63 hand-written cases added on
2026-11-20 cover other profiles, and 27 more of the same day (`da-scope`, `da-list`, `be-list`,
`nu-scope`, `nu-fam`) are synthesized from postings that live sessions 3 and 4 kept or lost for
region-scoped profiles. Session 7 added 6 US-location cases (`nu-scope-011` to `015`, `da-list-002`).
Every URL is on `jobs.example.test`, and no case describes a
real person or company. It maps to ULTRON's pipeline as follows:

| Corpus | ULTRON |
|---|---|
| fixture `title`, `location`, `remote_text`, `body_text`, `is_open_hint` | `ExtractedJob` fields; `apply_available` follows `is_open_hint` |
| fixture `apply_url` | `apply_url` and identity |
| fixture `employer` (optional) | the company is `Synthetic Employer <employer>` and the board `<vendor>:<employer>`; without it, the case id is used, so every case is its own employer |
| `vendor` | a vendor-API posting (`source_type: ats_api`), so the ATS-domain allowlist does not apply; the vendor is only a slice |
| `profile` | `ml-engineering-remote-eu` is the bundled example profile `tests/fixtures/profiles/ai-ml-remote-eu.yaml`; every other profile is `tests/fixtures/benchmark/profiles/<profile>.yaml`. Each profile is replayed on its own, and it is also a slice |
| `remote_mode` | the `region` slice |
| `duplicate_group` (optional) | one posting listed several times (one row per city). Its members share one label and count as one unit: the unit is kept when any member is kept, and every further row of it in a shortlist is a duplicate row |
| `labels.relevant: true / false` | positive / negative; `null` is left out of the binary metrics |
| prediction | the shortlist after `filter` + `finalize` with the `balanced` preset and no agent: a `keep` row is a positive, an `unsure` row is an abstention, anything else is a negative |
| ranking | the shortlist order finalize produced, per profile |

The labels are human-owned. No tool writes them. Change a label only by hand, with the reason
in the PR, and never to match what the code currently outputs.

**Labelling rule.** A case is `relevant: true` when its posting text fits its case profile on
every dimension: the role family, the level the profile accepts (`seniority`), the remote policy,
the location scope (someone living in the profile's target countries or regions can apply) and an
open application. It is `false` when the text shows any dimension does not fit (a closed posting,
another family, an excluded level, hybrid or on-site work for a remote-only profile, a residence
requirement outside the scope). It is `null` when the text leaves a dimension undetermined and
nothing else rules the posting out (an adjacent family, an unstated level or workplace).

**Delegated relabelling (2026-11-20).** The owner delegated one relabelling of the benchmark to an
agent on 2026-11-20. Under the rule above, 46 of the 120 legacy cases changed their `relevant`
label: manager roles were relevant against a profile that excludes managers, and on-site, hybrid
and US-only postings were `null`. The 63 new cases (a data-analyst profile with a Latin American
scope, a backend profile based in Spain, an on-site nursing profile in Ireland, and
country-restricted ML postings) were labelled from their text before they were replayed. In the
60-case labels, three remote-EMEA cases and a distinguished-level case became relevant, and a case
whose only location is a Belgian city became ambiguous. The same day the owner also asked for five
profile-conditioned quality fixes in one session; this one-time override of the two-change cap
of the QUALITY gate is recorded in that pull request. The cap itself is unchanged.

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

### Baseline comparison: ULTRON against an agent without it

```sh
uv run python tools/arm_comparison.py prepare --profile <slug> --session <name> [--top-n 25] [--force]
uv run python tools/arm_comparison.py ingest-ultron --session <name> --rep <k> --run <run-id>
uv run python tools/arm_comparison.py ingest-agent --session <name> --rep <k> --from <file>.jsonl
uv run python tools/arm_comparison.py record-cost --session <name> --arm <arm> --rep <k> \
    --tokens <n> --tool-calls <n> --seconds <n>
uv run python tools/arm_comparison.py discard --session <name> --arm <arm> --rep <k>
uv run python tools/arm_comparison.py pool --session <name>
uv run python tools/arm_comparison.py spotcheck --session <name> [--size 20]
uv run python tools/arm_comparison.py score --session <name> --output report.json [--allow-partial]
```

This live measurement asks whether ULTRON finds better postings than the same agent using plain
web search. The tool launches no agents and uses no network. It freezes a common brief, imports
both shortlists in one format, writes blind labelling files and computes the metrics.

`prepare` reads the profile from `ULTRON_HOME`. On a fresh home, loading the profile can create
ULTRON's empty folders. `ingest-ultron` reads a run's `run.json` and `results.json` the same way
`replay_run.py` does, rejecting symlinks. Nothing is written to `ULTRON_HOME`.

Session state, the brief, labels, spot check and reports live in the reports folder, because they
hold the profile and real postings. Session files are `<session>.state.json`, `.brief.md`,
`.labels.yaml` and `.spotcheck.yaml`; `score --output` refuses those names.

Protocol:

1. **Prepare.** `prepare` writes `<session>.brief.md`, the task both arms receive. It contains:
   - only the profile's `name`, `description`, `prompt` and `criteria`. `source_cv`, the slug and
     ULTRON's vocabulary packs stay out.
   - the shortlist size N (`--top-n`, default 25).
   - the JSONL output format: `title`, `company`, `apply_url` and `location`, in rank order.

   Only the top N rows of each repetition are pooled, labelled and scored, since `balanced` can
   keep up to 250 roles. `prepare` refuses an existing session unless `--force` is given.
2. **Run 3 repetitions per arm on the same day, alternating which arm goes first**:
   U1 B1, B2 U2, U3 B3.
   - Every repetition is a fresh subagent with no shared context, on the same model.
   - Both arms get the same stopping rule: deliver N ranked postings, then stop.
   - **ULTRON arm**: give each repetition its own copy of the starting `ULTRON_HOME` (see
     experiment hygiene). The subagent follows `SKILL.md` with `--preset balanced` through
     `finalize`. Then run `ingest-ultron` with the same `ULTRON_HOME`. It refuses a run that is
     not finalized, a run that did not use `balanced`, a run of another profile, a run of a changed
     profile (the whole profile is hashed, `source_cv` included), and a run already used for
     another repetition. It stores the run's own create-to-finalize time as `pipeline_seconds`.
   - **Baseline arm**: the subagent receives only the brief and is told not to use ULTRON. It may
     use web search and page fetches, and returns the JSONL. Save the JSONL in the reports folder
     and run `ingest-agent`. A file already used for another repetition is refused. Check the
     transcript for ULTRON commands, and `discard` the repetition if it used any.
   - Only the top N rows are validated and kept. A bad row below the cut does not block the
     import.
   - After every repetition, run `record-cost` with the subagent's reported tokens, tool calls
     and wall-clock seconds. Use the same source for both arms. ULTRON's cost covers its whole
     run, including the postings it processed beyond the top N: that is the price of its
     shortlist.
3. **Label blind.** `pool` clusters the scored rows into jobs. Two rows are the same job when
   their canonical posting identity matches, or when company, normalized title and location are
   all equal and none is empty or a placeholder such as `Confidential` or ULTRON's
   `company_not_found` and `title_not_found`.
   - Job ids are stored in the session, so labels survive later ingestions in any order. When a
     new row merges jobs, or a discarded repetition splits one, every cluster still gets its own
     id. `pool` resets the labels of the jobs involved and lists them under `relabel_ids`.
   - `<session>.labels.yaml` is shuffled and shows no arm. Each entry lists every URL and location
     variant of its job. The URL hosts can still hint at the source, ATS board or aggregator,
     so the blinding is imperfect. The report says so.
   - Replace each `pending` with `true`, `false` or `null` (cannot tell), opening each posting.
     `relevant: true` requires `open`, `eligible` and `role_fit` not to be `false`.
4. **Spot check.** `spotcheck` writes a reproducible blind sample (`--size`, default 20) for the
   user to label `relevant` without looking at the labels file. Run it again once the sample is
   filled in to get the disagreement rate on `relevant`. A sampled job stays pending until both
   the user and the main labels have judged it. `score` recomputes the agreement from the current
   labels, so a later relabelling is always reflected. If the pool changed and the sample now
   holds jobs that are gone, delete the spot-check file and draw a new one. A sample with no
   `true`/`false` judgments does not count. Above 15% disagreement, relabel everything and run
   `spotcheck` again.
5. **Score.** `score` refuses to run when the experiment is incomplete:
   - a label is pending;
   - an arm is missing, or has fewer than 3 repetitions;
   - the two arms have different repetition numbers;
   - a repetition has no tokens, tool calls or seconds recorded;
   - the spot check is missing, stale, unfinished, has nothing to compare, or is above the
     disagreement threshold.

   `--allow-partial` scores anyway, marks the report partial and lists the reasons. The report
   gives each repetition, then the mean ± sample std with its `n`:
   - quality: precision, P@5 and P@10, closed, ineligible and role-mismatch rates, and duplicate
     rows.
     - P@k slices the ranked positions first.
     - Repeats and empty positions count as misses.
     - Unlabelled positions are left out.
   - coverage: relevant jobs found, pool recall, the relevant jobs only one arm found, and the
     Jaccard overlap between arms.
   - cost: tokens, tool calls and seconds. Cost per relevant job is pooled: total cost divided by
     total relevant jobs, so a repetition that found nothing still counts.
   - reproducibility: mean pairwise Jaccard between the repetitions of one arm, over all jobs
     and over relevant jobs.

   `null` labels are left out of every denominator.

Pool recall counts the relevant jobs either arm found, not the whole market. These live numbers
are never mixed with replay or benchmark numbers. The 6 runs count against the live budgets above.

### Current baseline

Relabelled baseline of 2026-11-20 (`balanced` preset, no agent), before that day's quality fixes:

| Benchmark | Value |
|---|---|
| 183-case precision / recall / F1 | 0.527 / 0.879 / 0.659 (tp 29, fp 26, fn 4, tn 107), 5 duplicate rows |
| per profile (P / R) | ML 0.667 / 0.500, data analyst 0.409 / 1.000, backend 0.643 / 1.000, nurse 0.538 / 1.000 |
| 60-case relevant | 20 kept, 0 unsure, 0 discarded |
| 60-case not relevant | 1 kept, 0 unsure, 20 discarded |
| 60-case ambiguous | 3 kept, 3 unsure, 13 discarded |

After the four accepted fixes of 2026-11-20 (off-family titles, student levels, postings limited to
another country, location variants), `baseline.json` measures 1.000 / 0.879 / 0.935 (tp 29, fp 0,
fn 4, tn 133) with 0 duplicate rows; the 60-case pin did not move. A fifth hypothesis, unknown
eligibility lowering the verdict, was not accepted: the first attempt demoted seven relevant on-site
Irish nursing jobs, and the second moved no case. These numbers come from cases written for these
hypotheses, so read them as a regression gate, not as live precision.

The 27 region-scope cases lowered that baseline to 0.736 / 0.886 / 0.804 (tp 39, fp 14, fn 5,
tn 135) on 210 cases. Two quality changes were accepted, within the session cap: the body of a job
that is not full remote no longer opens a posting placed in another country, and "US" and Northern
Ireland read as countries outside an Ireland or Latin American home. Together they measure 0.796 /
0.886 / 0.839 (tp 39, fp 10, fn 5, tn 139); the 60-case pin did not move. Deferred hypotheses,
measured but not applied: "global" and "international" in the body no longer counting as a region
(0.736 -> 0.780 alone, 0.886 on top of the two accepted changes; deferred by the cap), and dropping the
qualifier "specialist" of "Clinical Nurse Specialist" from the family nouns (0.907 when stacked on all
three; breaks a Spanish family-noun test as written). Rejected: a keep that names no place of the
scope anywhere becoming unsure (new false negative: an Irish town outside the profile's terms), and
"Nursing" read as the noun "nurse" (admits nursing-home and assistant roles).

Session 7 accepted two quality changes. The deferred "global" and "international" change came first.
After review it is a closed list: these words in a body still name a region unless they directly modify
a company noun from a closed list ("the global economy", "our global support organization",
"international clients"). Ambiguous text never demotes a keep, so a negated statement ("we are not
hiring internationally") still opens a posting, which is a known limit. It measured
0.796 / 0.886 / 0.839 -> 0.886 / 0.886 / 0.886 (fp 10 -> 5) on 210 cases. Six US-location cases
followed, which brought the corpus to 216 cases at 0.833 / 0.851 / 0.842 (tp 40, fp 8, fn 7, tn 144).
Two of the six are Irish towns that the European pre-filter already loses: it reads "Virginia" and
", CO." as US places. The second change treats a US place that names no country as outside a non-US
home. The evidence is a state code at the end of a location ("Austin, TX"), a state name after a
comma or before "(Any City)", or an explicit US state-licence requirement on the applicant in the
body ("must hold an active Texas RN license", "current state licensure", "licensed to practice in the
state of Arizona"), not negated in its clause. A code that a country or another region also uses
("CA", "PR", "MI") needs a ZIP code and never counts when that country is a home. It measures
0.930 / 0.851 / 0.889 (tp 40, fp 3, fn 7, tn 149). Of the five cases it fixes, three were written for this
change, so read that number as a regression gate. Neither change created a new false negative, and
the 60-case pin did not move.

The legacy 120 cases with their original labels measured 0.750 / 0.300 / 0.429 (tp 3, fp 1,
fn 7, tn 60). The pull request that applies a quality change records its before and after
numbers; `baseline.json` always holds the latest accepted state.

Known limits of the corpus:

- The legacy 120 cases are templated for one ML profile. Spanish core titles read "Senior
  Ingenieria Machine Learning", and the four ML misses are these titles failing role-family
  matching, which may be an artifact of the template.
- `eligibility_label` does not appear in the legacy fixture text; it records the rule's reading of
  `remote_mode`.
- The added profiles have 20 to 32 labelled cases each, so one case moves a profile's recall by
  5 to 10 points.

## Open quality hypotheses

Self-improvement session 8 (2026-11-20) closed the live passes for this cycle. It ran six live
runs with no code changes, found no robustness bug, and hit no 403, 429 or challenge page. These
hypotheses were found live, ordered by how many wrong rows each caused, and are not applied. Each
one needs labelled benchmark cases before a change is tried, per the QUALITY gate above.

Wrong keeps:

1. A remote posting placed in another country (Spain, Germany, France, India) is kept when the
   eligibility is unknown.
2. US-only, "Americas" or "North America" remote postings are kept for EU and UK profiles.
3. A state-licence requirement written without the word "state" is missed: location "Wyoming",
   body "Active medical license in Wyoming".
4. Northern Ireland and Spain rows still reach an Ireland shortlist.
5. A remote posting that names no region is kept.
6. Hybrid is read inconsistently: "Full remote from Spain" and "Hybrid or fully remote" are
   discarded, while a "Hybrid working mode" benefit line is kept. A hybrid job can also read as
   remote.

Wrong discards:

7. "Anywhere in Latin America" does not open a posting for a Latin American profile.
8. Clinical Nurse Specialist and CNM titles are lost; dropping "specialist" from the family
   nouns measured 0.907 precision but breaks a Spanish family-noun test as written.
9. The European pre-filter reads "Virginia" and ", CO." in two Irish town names as US places, and
   drops a "Europe; London" designer posting for a reason not yet traced.
10. Two Barcelona postings (backend profile) and "Person in Charge (Nursing)" are discarded.

Session 8 also showed that the session 6 and 7 changes moved rows from keep to unsure, but unsure
rows still reach the shortlist, so live precision with unsure counted barely moved. Whether unsure
rows belong on the shortlist is an owner decision.

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
| Rezoomo | not added (2026-11-20) | Its terms of use forbid any systematic or automated data scraping, extraction or collection |

The Rezoomo row is a terms decision from self-improvement session 6, not part of the MARK II review: its public
company boards would be technically simple to read.

Reopen a decision only with evidence of a public, unauthenticated, bounded API and terms that
allow automated access.

## Session report

Every session ends with this table, plus the live budget used (runs, requests per domain),
each quality hypothesis with its benchmark result (accepted or discarded, with metrics), the
`blocked` and EXTERNAL findings, and the final `pytest` and `ruff` status.

| Scenario | Finding | Type | Root cause | Fix (commit) | Verification evidence | Status |
|---|---|---|---|---|---|---|

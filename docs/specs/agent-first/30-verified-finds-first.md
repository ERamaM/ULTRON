# 30 · Verified finds first

Status: proposed. Implement before any further measurement or loop iteration. It corrects how [21](21-scout-run-mode.md) to [24](24-agent-judgment-decides.md) were implemented in #99.

## Why

The accepted direction is that the agent discovers and judges, and ULTRON helps: it normalizes, remembers and adds what the agent could not see. With that split, a scout search can never be worse than the same agent searching alone, because the agent does the same work and ULTRON only adds to it.

The scout mode merged in #99 does not work that way. It changed what the agent does:

| | Agent without ULTRON | Scout agent (as merged) |
|---|---|---|
| How it finds | Searches, opens each posting, checks it is open and fits, then keeps it | Copies search hits into `candidates add` without opening them |
| Tool calls per run | 100 to 150 | about 40 |
| What it delivers | 25 postings it verified itself | 50 to 80 unverified URLs, about half of them closed |
| What it judges | Its own finds, while searching | A queue ULTRON built: about 10 of its own finds and 50 from board expansion |
| Shortlist | Its verified postings, in its own order | Its finds mixed with expansion rows, many of them `unsure` |

The [results of 2026-11-25](README.md#results-of-2026-11-25) follow from this: 11.0 relevant jobs per run against 15.0 on the AI profile, and 7.7 against 20.0 on the finance profile. The three loop iterations tuned symptoms of the same cause and were all reverted.

ULTRON became a pipeline the agent has to pass through again. This specification makes it additive.

## Expected behavior

A scout search has three parts, in this order.

1. **The agent searches and verifies, exactly as it would alone.** It opens each posting, confirms it is open, eligible and a fit, and gives it a verdict and a priority. It stops at the shortlist size the user asked for (25 by default).
2. **ULTRON takes the verified finds as they are.** It does not fetch them again, filter them or queue them. It normalizes each URL to its canonical identity, removes duplicates, and flags each find against memory (`new`, `seen`, `rejected`, `applied`).
3. **ULTRON proposes extras.** It queries the boards of the employers found and the watchlist, as today. Only those extra postings go to the evaluation queue. The agent judges them, and the ones it keeps are listed below its own verified finds, never above them and never mixed in.

The shortlist is therefore the agent's own verified list plus extras. Memory and the watch keep working on top of both.

Unverified finds stay supported: a row without a verdict behaves as it does today.

## Design

### A. Verified find rows in `ultron candidates add`

In a scout run, a JSONL row with a `verdict` is a verified find. Fields:

| Field | Required | Values | Default |
|---|---|---|---|
| `url` | yes | `http(s)` URL of the employer's own posting | |
| `verdict` | yes | `keep`, `unsure` or `discard` | |
| `title` | yes for `keep` and `unsure` | text, cut to 500 characters | |
| `company` | yes for `keep` and `unsure` | text, cut to 500 characters | |
| `location` | no | text | `""` |
| `priority` | no | 1 (apply first) to 3 (apply last) | 2 |
| `remote_policy` | no | a `RemotePolicy` value from `core/models.py` | `unknown` |
| `eligibility` | no | an `Eligibility` value from `core/models.py` | `high` for `keep`, `unknown` for `unsure` |
| `eligibility_evidence` | no | text, cut to 300 characters | `""` |
| `fit_signals` | no | list of at most 8 short strings | `[]` |
| `discard_reason` | no, `discard` only | text, cut to 300 characters | `""` |

A row with an invalid value is reported with outcome `invalid` and a reason naming the field. The other rows of the file are still processed. A row without `verdict` is an unverified find and follows the current path.

### B. Processing in `stages/candidates.py` (`add_scout_finds`)

For each verified find, in order:

1. Classify the URL with `scout_resolve.classify_find`. A rejected URL keeps outcome `rejected`.
2. An aggregator URL is resolved with `resolve_aggregator`, passing the row's company.
   - Resolved: continue with the employer URL.
   - Unresolved: outcome `unresolved_aggregator`. The row goes to `unresolved_finds.jsonl` and is not stored as a verified find. The agent must supply the employer's posting URL. An aggregator URL is never exported.
3. Compute the identity with `candidate_identity`, as for any candidate.
4. Look up the memory flag with `scout_memory.flags`.
   - `rejected` or `applied`: outcome `already_rejected` or `already_applied`. The row is not stored.
   - `seen` or `new`: continue. The flag is reported on the row.
5. Store the find in a new run file, `judged_finds.jsonl` (constant `JUDGED_FILE` in `stages/scout.py`). One row per identity. A later row for the same identity replaces the earlier one, so the agent can correct a verdict. Stored fields: `identity_key`, `apply_url` (the normalized employer URL), `title`, `company`, `location`, `verdict`, `priority`, `remote_policy`, `eligibility`, `eligibility_evidence`, `fit_signals`, `discard_reason`, `source_type` (`web_search` for a supported ATS posting, `employer_page` otherwise) and `memory`.
6. Also add the candidate to `candidates.jsonl` with `search_pass` set to `judged` (constant `JUDGED_PASS`), unless the identity is already there. This keeps two behaviors that already work:
   - `discover ats` proposes and expands the board of the find.
   - Expansion skips a posting the run already has.

   If the identity is already in `candidates.jsonl` as an unverified find, set its `search_pass` to `judged` in place.
7. Report the row with outcome `verified`, the canonical URL, the identity key, the memory flag and `judged: true`.

The command output adds `verified: {"keep": n, "unsure": n, "discard": n}` next to the existing counts.

Recruiter boards are still never proposed as seeds (`is_recruiter`).

### C. `ultron fetch` in scout runs

Candidates with `search_pass == "judged"` are skipped. No request is sent, no posting is written and no fetch error is recorded for them. The summary adds `verified_skipped`.

Add an opt-in check, `ultron fetch --run <id> --check-verified`. It fetches the verified `keep` and `unsure` finds like any candidate, but only to compare:

- It changes no verdict and adds nothing to `postings.jsonl`.
- It writes `verified_check.jsonl`, with one row per find: `identity_key`, `apply_url`, `agent_verdict` and `fetch_outcome` (`open`, `closed`, `failed` or `unusable`).
- The summary reports `agent_open_fetch_closed`.

This measures whether the fetcher reports open postings as closed. In the measurement, agents without ULTRON found open postings on boards where the scout fetch reported many as expired. That was never checked.

### D. `ultron filter` in scout runs (`stages/scout.py`, `route_postings`)

A fetched posting whose identity is in `judged_finds.jsonl` is left out of the queue. This can happen when an earlier fetch ran before the verdict was added. It is not a discard: count it in the summary as `already_verified` and write nothing to `discarded.jsonl` for it.

The queue then holds only unverified finds, watched postings and expansion postings, in today's order. The queue cap is unchanged.

The summary adds `verified_finds`: the count of `keep` and `unsure` rows in `judged_finds.jsonl`.

### E. `ultron evaluate pending` and `evaluate record`

Unchanged. `evaluate pending` adds `verified_finds` to its output, so the agent knows how many rows are already settled.

### F. `ultron finalize` in scout runs (`stages/scout_finalize.py`)

1. Build one `FinalRow` per verified find with verdict `keep` or `unsure`:
   - `title`, `company`, `apply_url`, `location`, `remote_policy`, `eligibility`, `eligibility_evidence`, `fit_signals` from the stored row.
   - `evaluation_verdict` and `priority` from the stored row.
   - `source_type` from the stored row, `source_key` empty, `source_label` set to `Agent find`.
   - `found_by` set to `agent_verified` (new value).
   - `freshness` empty, `match_score` and `match_band` unset.

   No geography gate and no heuristic evaluation run on these rows: the agent verified them.
2. Build the other rows as today, from the evaluated queue and from memory.
3. Deduplicate all rows together with `dedupe_final_rows`. In `_duplicate_quality` (`core/ranker/final_rows.py`), a row with `found_by == "agent_verified"` wins over any other row of its group. Put this first in the key. Sweep rows have an empty `found_by`, so sweep results do not change.
4. Rank with `rank_scout_rows` in two tiers:
   - Tier 0: `found_by == "agent_verified"`.
   - Tier 1: every other row (extras the agent kept, unverified finds it judged from the queue, rows carried from memory).

   The current key applies within each tier: verdict, priority, eligibility, new rows first, match score.
5. `max_roles` cuts from the end, so a verified find is never cut while an extra is kept.
6. Record memory with `scout_memory.record_verdicts`:
   - `keep` and `unsure` verified finds: with their row and priority.
   - `discard` verified finds: verdict `discard`, with `discard_category` `agent_rejected`. They are never exported and are flagged `rejected` in later runs.
   - `content_hash` is empty for verified finds, because no posting text was fetched. See G.
7. Credit boards as today: `seed_kept` uses `row_board_key`, which already reads the board from the posting URL.
8. The finalize output adds `verified_finds` (exported from tier 0) and `extras` (exported from tier 1). `top` lists the first rows in the new order.

### G. Memory of verified finds (`storage/scout_memory.py`)

A verified find has no content digest. Later runs must not ask the agent again for no reason:

- When the agent adds the same posting again as a verified find, it is flagged `seen` and the new verdict replaces the stored one.
- When a later run fetches the posting through expansion or the watch, and the stored verdict has an empty `content_hash` and the same `profile_hash`, `route_postings` carries the stored row as it does for an unchanged seen posting, and stores the digest of the fetched posting. From then on the normal rule applies: a changed digest sends the posting back to the queue.

`carried_row` needs one more outcome for this, or a second function. Keep `record_verdicts` as the only writer.

### H. Brief and skill

- `stages/scout.py`: replace `SEARCH_ADVICE` with advice for the new loop:
  - Search by role and region.
  - Open every posting before you add it.
  - Add it with your verdict and priority.
  - Never give a verdict to a posting you did not open.
  - Use the employer's own posting URL.
  - Stop at `shortlist_target`.
- The brief's `budgets` add `shortlist_target` (25).
- `skill/ultron/references/scout.md`: rewrite the loop table and the "Adding finds" and "Judging" sections. The loop becomes:

| # | Who | Action |
|---|---|---|
| 1 | CLI | `ultron run new --profile <slug> --preset <p> --mode scout` |
| 2 | you | Search, open each posting, check it is open, eligible and a fit |
| 3 | you + CLI | Write verified finds with `verdict` and `priority` → `ultron candidates add`; repeat until you have `shortlist_target` keeps |
| 4 | CLI | `ultron discover ats`, `ultron fetch`, `ultron filter` (extras only) |
| 5 | you + CLI | Judge the extras with `evaluate pending` and `evaluate record` |
| 6 | CLI | `ultron finalize` |
| 7 | you | Summarize: your verified finds first, then the extras ULTRON added, then what memory flagged |

  Include one JSONL example of a verified find and one of a discard. Keep the outcome and memory tables, adding `verified`, `invalid`, `already_rejected` and `already_applied`.
- `skill/ultron/SKILL.md` and `references/commands.md`: no new command. Mention `--check-verified` in `scout.md` only.
- Skill files stay in plain English, name no platform tool and match `ultron --help`.

### I. What does not change

- The sweep mode and its results. The labelled benchmark, the 60-case pin and the parity suite must be unchanged.
- The ledger schema. No migration is needed: `scout_verdicts` already has every column used here.
- `ultron watch`, the watchlist and `saved-search run --watch`.
- `tools/arm_comparison.py`. A scout run still ends in `results.json`.

### J. Files to touch

| File | Change |
|---|---|
| `stages/candidates.py` | Verified find rows: validation, storage, report (B). Split helpers out if the module passes about 400 lines. |
| `stages/scout.py` | `JUDGED_FILE`, `JUDGED_PASS`, advice, `shortlist_target`, routing (D), carry rule (G) |
| `stages/fetch.py` | Skip verified finds; `--check-verified` (C) |
| `commands/stages.py` | The `--check-verified` flag |
| `stages/evaluate.py` | `verified_finds` in the pending output (E) |
| `stages/scout_finalize.py` | Rows, tiers, memory, output (F) |
| `core/ranker/final_rows.py` | `agent_verified` in `_duplicate_quality`; tiers in `rank_scout_rows` |
| `storage/scout_memory.py` | First observation of a verified find (G) |
| `skill/ultron/references/scout.md` | The new loop (H) |
| `docs/architecture.md` | Only if a module is added |
| `tests/stages/test_scout_flow.py`, `tests/stages/test_scout_units.py` | The acceptance cases |

Follow the repository rules in `AGENTS.md`: public docstrings, no cross-package private imports, small functions, linear regexes, and 80% coverage on changed lines.

## Acceptance cases

Verified finds:
- A `keep` row with `url`, `title` and `company` is reported `verified`, is stored in `judged_finds.jsonl` and appears in `results.json` with `found_by` `agent_verified`, its priority and its verdict.
- `fetch` sends no request for a verified find. A run with only verified finds can go from `candidates add` to `finalize` with `fetch` and `filter` reporting nothing to do.
- A verified find never appears in `evaluate pending`.
- A `discard` row is not exported and is flagged `rejected` when added again in a later run.
- Adding the same identity twice with different verdicts keeps the last one.
- A row with `verdict` but no `title`, a `priority` of 5, or an unknown `remote_policy` is reported `invalid` with the field named, and the other rows are processed.
- A verified find on an aggregator page that resolves is stored with the employer URL. One that does not resolve is reported `unresolved_aggregator` and is not exported.
- A verified find flagged `rejected` or `applied` in memory is reported `already_rejected` or `already_applied` and is not exported.

Extras:
- `discover ats` expands the board of a verified find on a supported ATS, and a second matching posting on that board reaches the queue as `expansion`.
- A posting that is both a verified find and an expansion result is exported once, as the verified find.
- An unverified find (no `verdict`) is still fetched, queued and judged as today.

Order:
- With verified finds at priorities 1 and 3 and an extra `keep` at priority 1, the export order is: verified priority 1, verified priority 3, then the extra.
- A verified `unsure` row ranks above an extra `keep` row.
- With `max_roles` lower than the row count, extras are cut before any verified find.
- A duplicate group with a verified find and an extra keeps the verified find, whatever the match scores.

Memory:
- A verified `keep` from an earlier run that a later run reaches through expansion is carried with its stored verdict and is not queued. A second later run with a changed posting text queues it as `changed`.
- An edited profile sends a carried verified find back to the queue, as for any carried row.

Check:
- `fetch --check-verified` writes `verified_check.jsonl`, changes no verdict and adds no posting. With a fixture where the page is closed and the agent said `keep`, `agent_open_fetch_closed` is 1.

Unchanged:
- A sweep run produces the same `results.json` as before this change on the same fixtures.
- `tools/quality_benchmark.py compare` and `tools/measure_quality.py` report no change.

## Verification

Cover every acceptance case with the mocked network used in `tests/stages/test_scout_flow.py`, and the ranking and memory rules with unit tests. Run `ruff`, the full suite, the benchmark gates and the wheel build.

Then measure live, under [29](29-improvement-loop.md) and the [constraints](README.md#constraints):

- The live budget is spent (30 of 30). The user must raise it first. One scout repetition set is 3 runs per profile.
- Use a fresh session, so the web-search quota is not shared with earlier work. In the last two iterations the shared quota left scout agents with 6 to 14 searches per run.
- The scout prompt must let the agent search and open pages as freely as the baseline prompt does. Do not forbid page fetching or its own verification. State the same shortlist size, 25.
- Run `fetch --check-verified` in each repetition and report `agent_open_fetch_closed`.

Expected result, to confirm or refute:

| Metric | Expectation against the agent without ULTRON |
|---|---|
| Relevant jobs per run | At least equal from the verified finds alone; higher with extras |
| P@5 and P@10 | Equal: the first rows are the agent's own verified finds |
| Ineligible rate | Equal for verified finds; extras must not raise it |
| Tokens per relevant job | At most 1.25 times: the same search plus the extras queue |

If relevant jobs per run is not at least equal, the cause is in the agent instructions, not in the pipeline: check the transcript for a scout agent that verified fewer postings than a baseline agent.

## Out of scope

- Retiring the sweep ([28](28-retire-the-sweep-pipeline.md)). It stays blocked on the pass criteria.
- Classifying job boards such as wellfound or builtin as aggregators. Iteration 3 tried it and was reverted under a confounded measurement. With verified finds the agent supplies the employer URL, so the defect matters less. Revisit it after the next measurement.
- Enriching verified finds with fetched data such as the posting date or salary.

# Search workflow (detailed)

The full loop with every command, its input files and the JSON it prints. `ultron` below means the
executable named in `RUNTIME.md` (see SKILL.md). Every command prints **one JSON object** on stdout.

## Conventions

- **Run folder:** `<ULTRON_HOME>/runs/<run_id>/` (`ULTRON_HOME` is in `RUNTIME.md`; default `~/.ultron`).
  `run new` prints only the `run_id`; build the path yourself.
- **Your input files** (proposals, web results, evaluation batches) go in the run folder with an
  `agent_` prefix, e.g. `agent_proposals.jsonl`, `agent_web.jsonl`, `agent_eval_01.jsonl`. Never write to
  a file the CLI owns (`run.json`, `candidates.jsonl`, `postings.jsonl`, `kept.jsonl`, `ambiguous.jsonl`,
  `discarded.jsonl`, `evaluations.jsonl`, `seeds_*.jsonl`, `seed_*.jsonl`, `fetch_errors.jsonl`, `fetch_capped.jsonl`,
  `finalize_discards.jsonl`, `feed_errors.jsonl`, `seed_proposals_from_*.jsonl`, `results.*`,
  `run_summary.md`).
- **JSONL** = one JSON object per line, UTF-8 without BOM, no surrounding array. Write these files with
  your file-writing tool, not shell redirection (PowerShell redirection writes UTF-16/BOM).
- **Errors:** `{"error": "<code>", "detail": "..."}` with a non-zero exit code. `stage_order` (exit 3)
  means a prerequisite stage has not run: fetch needs `discover ats` or `discover feeds`, filter needs fetch, finalize needs
  filter, `evaluate` needs filter. Other codes: `run_not_found`, `profile_not_found`, `unknown_preset`,
  `usage`, `invalid_proposals`, `seed_file_corrupt`, `invalid_evaluation`, `stage_failed`.
- **Slow commands:** `seeds verify`, `discover feeds`, `discover ats` and `fetch` make many HTTP requests and can take
  several minutes (more with `deep` / `seed_boost`). Give them a generous timeout and wait; do not
  start a second copy of the same command for the same run.
- **Check progress:** `ultron runs list` shows each run's `stages_done`. Avoid `runs show`: it prints
  the whole profile snapshot.

## Presets

| preset | max roles kept | pages fetched | seeds queried | web queries | new companies | seed target (rounds) |
|---|---|---|---|---|---|---|
| `fast` | 5 | 70 | 120 | 20 | 10 | 35 (2) |
| `balanced` (default) | 250 | 400 | 180 | 40 | 25 | 75 (3) |
| `deep` | 250 | 4000 | 400 | 120 | 60 | 100 (3) |
| `seed_boost` | 250 | 4000 | 400 | 220 | 120 | 130 (4) |

Use `balanced` unless the user asks otherwise ("quick" → `fast`, "thorough" → `deep`, "find more
companies" → `seed_boost`).

## 1. Create the run

```
ultron run new --profile <slug> [--preset balanced]
```

Output:

```json
{"run_id": "20261116-101500-a1b2c3", "preset": "balanced", "profile": "ai-ml-remote-eu",
 "query_plan": ["Senior ML Engineer Remote Europe", "..."], "profile_seeds": 12, "new_companies_target": 25,
 "eligible_seeds": 40, "cold_start": false, "seed_target": 75, "fitting_seeds": 40, "seed_deficit": 35}
```

- `query_plan`: web-search queries for step 4 (already capped to the preset; includes `site:` ATS queries when `query_templates.site_scopes` is set).
- `profile_seeds`: verified companies already linked to this profile (reused automatically).
- `new_companies_target`: how many new companies to propose in step 2.
- `eligible_seeds`: inventory boards that fit this profile (its own seeds plus bundled packs for its
  sectors and region). `cold_start: true` means fewer than 15: add the extra ATS `site:` searches of
  step 4 (see "Cold start" there).
- `fitting_seeds`: the boards that count toward the preset's `seed_target`, counted the way step 5 selects
  them (seed and per-host limits). Feed seeds, guessed boards of unknown ownership and boards with no openings
  do not count. `seed_deficit` above 0: generate the missing companies in rounds in step 2 (`references/seeds.md`,
  "Seed rounds").

If the user has yet to select a profile, follow the interactive profile selection steps in SKILL.md
before calling `run new`. For a named saved search, use its configured profile.

## 2. Propose and verify companies

With `seed_deficit` above 0, run the seed rounds of `references/seeds.md` ("Seed rounds"):
`ultron seeds plan --run <run_id>`, write its `proposals_file`, then
`ultron seeds verify --run <run_id> --from <run folder>/<proposals_file> --round`, and repeat while
`more_rounds` is true. Round state is kept in `seed_rounds.jsonl`.

Otherwise follow `references/seeds.md` to write `agent_proposals.jsonl`, then:

```
ultron seeds verify --run <run_id> --from <run folder>/agent_proposals.jsonl
```

Output: `{"verified": 7, "new": 6, "unresolved": 3, "ownership_unknown": 1, "wrong_tenant": 0, "unresolved_sample": [{"company": "Acme", "reason": "no_board_found"}]}`

Details are in `seeds_verified.jsonl` and `seeds_unresolved.jsonl` (both rewritten on every call).
Verified companies are stored for this profile and used by `discover ats` from now on.

## 3. Structured feeds

```
ultron discover feeds --run <run_id>
```

Output: `{"sources_ok": 12, "sources_failed": 1, "sources_skipped": 0, "candidates": 140, "seeds_added": 6, "feed_boards_irrelevant": 9, "company_hints": 35, "feed_links_not_postings": 4, "requests": 40,
"requests_by_domain": [{"host": "www.arbeitnow.com", "requests": 3}, ...]}`

Reads the public job feeds listed in the skill's `data/feeds.yaml` (remote-job APIs, RSS feeds and
company lists). No agent input is needed. It does three things:

- Postings that pass the profile's coarse title filter become candidates (`source_type` `feed:<id>`),
  fetched in step 6 like any other candidate. The candidate URL is the posting's direct ATS link when
  the feed gives one. A posting whose only link is the aggregator's own page (arbeitnow, remotive,
  remoteok, jobicy...) is not a candidate: such pages are never kept, and the board it links to is
  still picked up as a seed. A link to a whole ATS board or to a company home page or careers index is
  not fetched as a posting either (`feed_links_not_postings`): its board becomes a seed and its company
  a hint. Feed rows are ranked by title fit and geography scope.
- Links to ATS boards found in the feeds are added to the inventory as seeds, so step 5 queries them
  (`seeds_added`), when at least one posting of that board passes the profile's title filter (boards of
  company-list feeds always count); boards seen only in other professions' postings are skipped
  (`feed_boards_irrelevant`).
- Company names whose ATS is unknown are written to `seed_proposals_from_feeds.jsonl` (`company_hints`,
  proposal format of `references/seeds.md`), most-mentioned companies first. Optionally verify them by passing that file directly with
  `--limit` instead of copying a subset (`--limit` defaults to the run's `new_companies_per_run` when the
  input file is `seed_proposals_from_feeds.jsonl`):
  `ultron seeds verify --run <run_id> --from <run folder>/seed_proposals_from_feeds.jsonl --limit <new_companies_target>`.
  Boards verified from that file are stored as `feed_company` seeds (not as your proposals), so they
  rank with the feed seeds in step 5.
  Do this before step 5 so the verified boards are queried in this run.

A feed that fails or changes format is recorded in `feed_errors.jsonl` and never stops the stage.
Running it again rewrites only the feed rows of `candidates.jsonl`. `fetch` needs `discover ats`
or `discover feeds` (either one is enough), but running both gives the best coverage.

## 4. Web search

No web search tool available? Skip this section, continue with step 5 (`discover ats`), and say so in the final summary.

Submit each `query_plan` string in order with its spelling, region and `site:` operator intact.
The plan already reflects the profile's regions and site scopes. For a web search tool, use the
string as the query. For a SearXNG instance, send that same string as the URL-encoded `q`
parameter. If you add a supplementary query using a profile technology, record it separately
and use the same query with each provider when comparing them. Record how many planned queries
you actually submitted if you stopped early.

**Cold start** (`cold_start: true` in `run new`): after the plan, also search the profile's two or three
main target titles on the main ATS hosts, e.g. `site:jobs.lever.co "<title>" <region>`, then
`site:job-boards.greenhouse.io`, `site:jobs.ashbyhq.com`, `site:apply.workable.com`,
`site:jobs.smartrecruiters.com` and `site:*.recruitee.com` (about 10 extra queries; record them
separately from the plan). Every posting URL on a new board becomes a seed proposal that step 5
verifies and keeps for later runs, so this is the quickest way to grow a small inventory.

Collect **individual job-posting URLs** (one opening per URL), excluding portals and aggregator
home pages. Write them as JSONL with the required `url`, plus `title`, `company` and the exact
submitted `query`.

```jsonl
{"url": "https://jobs.lever.co/example/0b1c2d3e-4f50-6789-abcd-ef0123456789", "title": "Senior ML Engineer", "company": "Example", "query": "Senior ML Engineer Remote Europe"}
{"url": "https://job-boards.greenhouse.io/sample/jobs/4012345678", "title": "Staff AI Engineer", "company": "Sample", "query": "site:job-boards.greenhouse.io Staff AI Engineer remote EU"}
```

```
ultron candidates add --run <run_id> --from <run folder>/agent_web.jsonl
```

Output: `{"added": 18, "duplicates": 3, "rejected": 1, "seed_proposals": 2}`

- `rejected`: lines without a valid http(s) `url`. `duplicates`: already in the run.
- `seed_proposals`: URLs on an ATS board that is not yet in the inventory. They are written to
  `seed_proposals_from_search.jsonl` in the proposal format; step 5 (`ultron discover ats`) verifies
  them automatically, saves them to the inventory for future runs, and queries their boards in this
  same run.

You can call `candidates add` several times; it appends.

## 5. Query ATS boards (20 vendors)

```
ultron discover ats --run <run_id>
```

Output: `{"seeds_queried": 45, "seeds_failed": 2, "seeds_skipped": 0, "candidates": 310, "requests": 50,
"requests_by_domain": [{"host": "boards-api.greenhouse.io", "requests": 12}, ...],
"seed_groups": {"agent": {...}, "pack": {...}, "other": {...}, "feed": {...}},
"seeds_deprioritized": 3, "seeds_exploration_reserved": 1,
"search_proposals_verified": 2, "search_proposals_added": 2}`

First verifies any new ATS boards from `seed_proposals_from_search.jsonl` (up to `new_companies_per_run`)
and adds them to the profile's inventory. Discovery reuses the postings gathered during verification.
Workday verification samples up to two pages, so discovery queries those boards with the normal request
budget. When a Workday board offers a country facet matching a preferred location, discovery interleaves
pages for those countries with unfiltered pages within the same budget.

Seeds can be on any of the 20 supported ATS vendors (`references/seeds.md`). Order: companies you
verified for the profile (and ATS boards discovered via web search) first, then bundled pack seeds in
the profile's sectors, then other seeds linked to the profile, then seeds found in feeds (feed seeds
never take more than half of the seed slots while curated seeds are left). Pack seeds for another sector
or region (a technology pack for a nursing profile, a US pack for a European profile) are never queried,
not even to fill leftover slots. Each ATS API gets at most
the preset's per-host limit of boards (e.g. 10 per ATS with `balanced`), filled in that order; boards
over it show as `host_capped` in `seed_groups` and do not use a seed slot. This is why the companies
you verify and find matter: they are queried first. Postings are pre-filtered by the profile's title
vocabulary and capped per company. A failing board is recorded in `seed_errors.jsonl` and never stops
the stage (`unparseable_board`: the page loaded but no listings could be read, e.g. a script-rendered Taleo
career section; it is not an empty board; `host_blocked`: the vendor host kept answering 403/429, so its
remaining boards were skipped in this run; `vendor_blocked` and `vendor_budget_exhausted`: see the vendor budget
below). `seeds_skipped` counts selected boards not queried because a request budget ran out at run time.

**Vendor budget.** Some vendors serve every company from its own host (each Workday company has its own
`*.myworkdayjobs.com` host), so per-host limits alone do not bound them. All hosts of one ATS vendor share, per run
and across every stage and `seeds verify` call of that run, one request cap (the preset's `max_requests_per_vendor`:
120 `fast`, 400 `balanced`, 1200 `deep` and `seed_boost`), a minimum spacing between requests (1 second for Workday,
0.5 seconds for the others) and one breaker. Two rate-limited requests (HTTP 429), or two "forbidden" answers
(HTTP 403) with no success between them, block the vendor for the rest of the run: its remaining boards and
postings are recorded as `vendor_blocked` and get no request. Verification and discover stop at half of the cap,
so fetch keeps the rest; discover splits its part evenly between the run's Workday boards. Boards or postings left
out by the cap are recorded as `vendor_budget_exhausted`. The `discover ats` and `fetch` outputs add
`requests_by_vendor` and `vendors_blocked`; the run folder keeps the state in `vendor_budget.json`. A blocked
vendor is not a ULTRON failure: run again later.

Seed order also learns from past runs of the same profile. `finalize` records which boards each run
queried and how many of their postings it kept. Within each group, boards that have produced kept roles
come first, ordered by how often a run kept one. Boards with fewer than 3 finalized runs follow, then
boards measured without a kept role yet. Boards queried in 5 or more runs without a single kept role
(`seeds_deprioritized`) go after all other seeds; they stay in the inventory and still run when slots
remain. When there are more seeds than slots, about 10% of the slots (at least 1) go to boards with
little history (`seeds_exploration_reserved` counts boards that got in this way), so new companies still
get explored.

## 6. Fetch postings

```
ultron fetch --run <run_id>
```

Output: `{"fetched": 210, "failed": 40, "capped": 12, "navigation_failures": 0, "requests": 250,
"requests_by_domain": [{"host": "boards.greenhouse.io", "requests": 40}, ...], "requests_by_vendor": {"greenhouse": 80},
"vendors_blocked": []}`

Downloads up to the preset's page limit (round-robin across companies; web-search hits get one turn per
submitted query), extracts each posting. `capped` counts candidates left out by the page limit; they are listed
in `fetch_capped.jsonl`. A posting skipped without a request because its vendor is blocked or out of budget does not
use a page slot. `requests_by_domain` (also in the `discover feeds` and `discover ats` outputs) lists
the five domains with most requests, busiest first, to show where the request budget went.
Failures (closed postings, 404, blocked hosts) are in `fetch_errors.jsonl`. Rows with `blocked` (captcha or
bot-challenge page) or `js_shell` (script-rendered page with no readable text) are worth opening manually.
`host_blocked` rows are URLs skipped because their host kept answering 403/429 during this run;
`vendor_blocked` rows are URLs of a vendor whose hosts did so (see the vendor budget in step 5), and
`vendor_budget_exhausted` / `skipped_vendor_budget_exhausted` rows are URLs left out by the vendor's request cap.
If at least 50 URLs failed to load at all (DNS failures, connection errors, timeouts) and they are at least half of the URLs
tried, fetch stops early: the output has `"stopped": "navigation_failures"` and the remaining URLs are recorded as
`skipped_navigation_failures`. Check the network or proxy, then run a new search.

## 7. Filter

```
ultron filter --run <run_id>
```

Output (the geography rules follow the profile's scope, see `references/profile.md`): `{"kept": 0, "ambiguous": 65, "discarded": 145, "rescued_discards": 4, "discard_reasons": {"seniority_not_senior": 60, "non_matching_role_family": 50, "hybrid_or_not_explicit_remote": 35}}`

- `discarded`: failed a strict rule (seniority, role family, remote, location, closed...). Final.
- `ambiguous`: every posting that survived the strict rules (heuristic `keep` or `unsure`). This is
  your evaluation queue. It also holds borderline heuristic discards (`rescued_discards` in the
  output, at most 20 per run, highest ambiguity first): postings that passed the domain, remote,
  geography and open-status gates but that the heuristic evaluator rejected (role family, seniority,
  remote policy) with an ambiguity score at or above the preset threshold. Their row has
  `rescued_discard: {"reason", "stage"}` and the heuristic verdict is `discard`; closed, expired,
  profile-excluded and non-ATS postings are never rescued. Unevaluated, they stay discarded at finalize.
- `kept`: postings that skip the queue; with default presets this is always 0, because every heuristic
  keep scores above the ambiguity threshold and goes to the ambiguous queue.

## 8. Evaluate the ambiguous queue (batches)

Get the next batch (default 10, never more than ~15):

```
ultron evaluate pending --run <run_id> --limit 10
```

Output:

```json
{"pending": 45, "returned": 10, "items": [{"identity_key": "...", "title": "...", "company": "...",
 "location": "...", "remote_text": "...", "heuristic": {"verdict": "unsure", "discard_reason": null},
 "body_excerpt": "first 1500 characters of the posting"}]}
```

Judge each item with `references/evaluation.md`, write one line per item to a new
`agent_eval_NN.jsonl` (`{"identity_key": ..., "evaluation": {...}}`), then:

```
ultron evaluate record --run <run_id> --from <run folder>/agent_eval_NN.jsonl
```

Output: `{"recorded": 10, "pending": 40, "pending_ambiguous": 35}`

Rules:

- `pending` returns unrecorded items with heuristic `unsure` first, then the rescued discards
  (`heuristic.verdict: "discard"`), then the heuristic keeps (file order within each group). The queue only moves when you record: record every item of a batch
  before asking for the next one.
- `record` is all-or-nothing: one bad line rejects the whole file with
  `{"error": "invalid_evaluation", "line": N, "identity_key": "...", "reason": "..."}`. Fix that line
  and record the same file again. Recording the same `identity_key` again replaces the earlier verdict.
- Budget: evaluate at least all the `unsure` items (they come first). Then evaluate the rescued
  discards and heuristic keeps as far as the preset allows: `fast` unsure only; `balanced` about 5
  batches in total; `deep` / `seed_boost` until done or the user's patience runs out. Heuristic keeps
  usually need only a quick confirmation. Items you never record keep their heuristic verdict at
  finalize (keeps stay keeps, unsure stays unsure, rescued discards stay discarded). Tell the user how
  many were left to the heuristic.
- Keep evaluations out of the conversation: write them to the file, do not echo them back.

## 9. Finalize

```
ultron finalize --run <run_id> [--xlsx] [--html]
```

Output:

```json
{"roles": 22, "new_roles": 15, "unsure": 4, "discarded": 190, "final_cap": 0,
 "agent_evaluated": 30, "heuristic_fallback": 15,
 "outputs": {"results_md": ".../results.md", "results_json": ".../results.json",
             "run_summary_md": ".../run_summary.md", "finalize_discards_jsonl": ".../finalize_discards.jsonl"},
 "top": [{"title": "...", "company": "...", "match_score": 87, "is_new": true}]}
```

- Merges your verdicts with the heuristics, ranks, removes duplicates, applies the preset's role cap
  (`final_cap` = good roles cut by the cap; `fast` keeps only 5), records history and exports.
- `unsure` rows stay in the results, flagged as unsure. A heuristic keep whose location is limited to a
  country outside the profile's home country also becomes `unsure`, with eligibility `unknown` and the note
  "location limited to <country>"; check it before recommending it.
- For a full-remote profile, a posting listed once per city (same company, title and board) becomes one
  row whose location lists the cities.
- A role you kept can still be dropped here (location conflict, duplicate, cap); those are listed in
  `finalize_discards.jsonl` with a `stage` field.
- `--xlsx` and `--html` also write `results.xlsx` / `results.html` and add `results_xlsx` / `results_html` to
  `outputs`. If that export fails, finalize still succeeds and reports `xlsx_error` / `html_error`.
- `results.json` rows carry `is_new` (never seen in an earlier run). Running finalize twice gives
  the same result and does not double-count history.

## 10. Summarize for the user

In the user's language, briefly: how many roles and how many are new; the best 3-5 (title, company,
why they fit, link from `results.md`); the unsure ones worth a manual look; main discard reasons
(from the filter output); companies added this run (`verified` from step 2); how many ambiguous
postings were left to the heuristic; where `results.md` is. Read `results.md` only as far as you need
(the top section), not the whole file.

## Unattended mode (no agent)

```
ultron run --unattended --profile <slug> [--preset balanced]
```

Chains new → discover feeds → verify the feed company hints (up to `new_companies_per_run`) →
discover ats → fetch → filter → finalize with no company generation by you, no web search and no
agent evaluation (ambiguous postings keep their heuristic verdict). Output: the finalize payload
plus `run_id` and a `stages` object with each stage's counts. Suggest it for scheduled or
zero-token runs; it searches the verified inventory plus whatever the feeds add. It cannot generate
seeds: `stages.seed_coverage` reports `fitting_seeds` and `seed_deficit`. A deficit above 0 means the
profile needs an interactive run with seed rounds first.

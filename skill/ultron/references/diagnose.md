# Diagnosing a missed job

`ultron diagnose <url> --profile <slug> [--run <id>] [--no-fetch]` answers "why did this job URL not show up?".
It looks the URL's canonical identity (tracking parameters ignored) up in the chosen run's artifacts
(`--run`, else the latest run of the profile), reports `first_failed_stage`, `reason_code` and a `trace`,
and stores the result in the ledger table `diagnostics`.

If there is no run, or the job was never discovered, and `--no-fetch` is not set, ULTRON downloads the URL and
replays extraction plus the deterministic filters. Those trace entries carry `label: offline_replay`
(`pre_filter`, `heuristic_evaluator`, `hard_filter`, `pipeline`) and show where the job would have been dropped.

## Stages and what to adjust

- `not_discovered`: no discovery step produced this URL. Get the company's board into the seed inventory (propose the
  company and run `ultron seeds verify --run <id> --from <proposals.jsonl>`), make sure relevant feeds are read, or add
  the URL directly with `ultron candidates add --run <id> --from <file>`. Check the replay
  trace: if it would also fail a filter, fix that first.
- `candidate_cap`: discovered, but never fetched because `max_pages` was reached. Use a larger preset (or
  `ultron saved-search create --override max_pages=...`; `run new` has no overrides), or narrow seeds so fewer boards compete.
- `fetch_failed`: the page errored (`expired_or_closed` for 404/410, `inaccessible`, budget exhausted, rejected URL).
  Closed postings are expected; for budget errors use a larger preset (or `ultron saved-search create --override`).
  `blocked` (a captcha or bot-challenge page) and `js_shell` (a script-rendered page with no readable text) need a
  manual look: open the URL in a browser.
- `extraction_failed`: the page was downloaded but no posting could be parsed. Prefer ATS-API seeds over scraping
  the HTML page; the page may be a client-rendered shell.
- `prefilter`: cheap early rejection (domain, obvious role/title mismatch). Adjust the profile's role terms
  and negatives. The domain allowlist is bundled data (`data/domain_rules.yaml`), not a profile setting; a domain
  rejection means that host is not an accepted job-posting source. Check the `reason_code`.
- `evaluator`: the heuristic or the agent judged the role not a fit (role family, seniority, remote/eligibility).
  Edit profile criteria: role focus terms, seniority terms, role negatives, or the vocabulary pack.
- `hard_filter`: a hard rule (location/eligibility, closed posting, disallowed domain, post-evaluation geography
  conflict). Revisit the profile's geography scope and remote policy.
- `dedup`: dropped as a duplicate of another posting of the same company and title; nothing to fix unless
  the two are genuinely different roles.
- `final_cap`: ranked below `max_roles`. Use a larger preset (or `ultron saved-search create --override max_roles=...`;
  `run new` has no overrides) or sharpen the profile so the role ranks higher.
- `pending`: a stage has not run yet, so no verdict exists. `not_fetched`: the run has not done `fetch`; run it.
  `not_finalized`: the job passed filtering but `finalize` has not run. `pending_evaluation`: it is in
  `ambiguous.jsonl` waiting for agent evaluation; evaluate, then finalize.
- `kept`: nothing failed; the job is in `results.json`.

The `reason_code` is the raw reason recorded by the failing stage; search for it in the run's `discarded.jsonl`
or `finalize_discards.jsonl` for the full record.

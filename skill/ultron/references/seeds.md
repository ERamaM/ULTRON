# Proposing and verifying companies (step 2)

ULTRON searches the job boards of a company inventory ("seeds"). Each run you add companies that fit
the active profile; the CLI checks that their public job board exists and stores the verified ones for
this profile, so later runs reuse them.

## How many and which

- Propose about `new_companies_target` companies (printed by `run new`). A few extra is fine, since
  some will not resolve.
- Run `ultron seeds list --profile <slug>` first. It prints the inventory size and up to 30 seeds
  (`linked: true` = already verified for this profile). Use them as examples of the kind of company
  that fits, and do not propose companies that are already there.
- Pick companies that **hire for this profile's roles in its region and sector**: read the profile's
  `criteria` (target titles, `sectors`, `location_scope.preferred`, `specialties`) and
  `query_templates.technologies`. Mix well-known employers with smaller and mid-size companies;
  the goal is coverage beyond the obvious names.
- Sources: your own knowledge, your web search tool (e.g. "<sector> companies hiring remote <region>",
  "<technology> startups <country>", lists of top employers in the sector), and any careers pages you
  come across while searching.
- Prefer companies that hire through a supported ATS (see below); when you already know the board,
  say so in the proposal. For a non-European profile (e.g. LATAM), pick companies that hire in the
  profile's own regions (regional tech employers, international companies hiring remotely there),
  not European ones.

## Proposal format (JSONL)

One object per line. Only `company` is required; every other field makes verification faster and
more reliable:

| field | meaning |
|---|---|
| `company` | company name (required) |
| `domain` | main website domain, e.g. `example.com` |
| `ats` | ATS name, if known (lowercase; see Supported job boards) |
| `board` | board identifier on that ATS, if known (see below) |
| `careers_url` | a URL of the company's ATS board or one of its postings; or its own careers page (on `domain`), which the CLI opens once to find the ATS board it links |

```jsonl
{"company": "Example Labs", "domain": "examplelabs.com", "ats": "greenhouse", "board": "examplelabs"}
{"company": "Sample AI", "domain": "sample.ai", "careers_url": "https://jobs.ashbyhq.com/sample-ai"}
{"company": "Demo Robotics", "domain": "demorobotics.eu"}
```

Board identifiers: the company token in the board URL, e.g. Greenhouse `boards.greenhouse.io/<board>`
or `job-boards.greenhouse.io/<board>`, Lever `jobs.lever.co/<board>`, Ashby `jobs.ashbyhq.com/<board>`,
Workable `apply.workable.com/<board>`, Recruitee `<board>.recruitee.com`, Teamtailor
`<board>.teamtailor.com`, SmartRecruiters `jobs.smartrecruiters.com/<board>`, Personio
`<board>.jobs.personio.com`. When unsure of the identifier, give the board URL as `careers_url`
instead of `ats` + `board`.

Verify:

```
ultron seeds verify --run <run_id> --from <run folder>/agent_proposals.jsonl
```

Output: `{"verified": 7, "new": 6, "unresolved": 3, "ownership_unknown": 1, "wrong_tenant": 0, "unresolved_sample": [{"company": "...", "reason": "..."}]}`.

When `ats` + `board` or `careers_url` is given, the CLI checks only that board (no slug guessing;
a `careers_url` that is the company's own careers page is opened once and the ATS boards it links are
checked) and reports its failure reason if it does not resolve; a given board is verified when its public API
answers (even with zero open postings). When neither is given, it probes slug guesses built from the
company name and domain on the probed ATSs (below), and a guessed board must have open postings and
**provably belong to the company**, because another company may own the same slug:

- a slug equal to the main label of the proposal's `domain` (`acme` for `careers.acme.com`) is accepted;
- otherwise, for a name with two or more words (legal forms such as GmbH or Inc do not count), the ATS
  must report the same company name in its postings (Greenhouse, Recruitee and SmartRecruiters do);
- a one-word or short name ("Acme", "Acme GmbH") needs the domain slug.

A guessed board that answers without that proof is not stored: it comes back as `ownership_unknown`
(no proof) or `wrong_tenant` (the ATS names another company), with the board it found under
`candidate` in `seeds_unresolved.jsonl`. Giving `domain` avoids most of these.

## Supported job boards

19 ATS vendors are supported. Two groups:

- **Probed by slug** (the CLI guesses boards from the company name/domain, in this order):
  greenhouse, lever, ashby, workable, recruitee, teamtailor, smartrecruiters, personio, pinpoint,
  homerun, breezy, jobvite, bamboohr, jazzhr, factorial.
- **Only via `careers_url` or an explicit `ats` + `board`** (the board needs a host or a vendor-specific
  locator that cannot be guessed): comeet, workday, icims, taleo.
  For these, search the web for the company's careers page, then propose it with its URL, e.g.
  `{"company": "Big Bank", "careers_url": "https://bigbank.wd3.myworkdayjobs.com/en-US/External"}`.

A proposal on an ATS outside the 19 (a custom careers site, an unknown vendor) comes back as
`vendor_not_supported_yet`, or `no_board_found` when nothing was found at all. Those companies can
still contribute postings through web search in step 4.

## One file per run

Put all of this run's proposals in one file and call `seeds verify` once. Each call has an overall
request budget (`max_pages` + 2 x `max_seeds_per_run` of the preset; about 300 requests with `fast`).
A proposal with a known `ats` + `board` (or a `careers_url` on the ATS) costs about 1 request; one
without costs up to 5 guesses on each of the 15 probed ATSs (about 22 requests per guess). If the budget runs out, the remaining companies
come back as `budget_exhausted`; verify those again in a new call, which starts with a fresh budget.

Give `ats` + `board` or `careers_url` whenever you know them; it is cheaper and more reliable.

## Handling unresolved companies

Read `<run folder>/seeds_unresolved.jsonl` (small: one line per failed company, with `reason` and
your original `proposal`). It is **rewritten** on every `seeds verify` call, so read it before the
next call.

| reason | what to do |
|---|---|
| `no_board_found` | Search the web for "<company> careers" / "<company> jobs" and look for a link to a supported ATS (including the careers-URL-only vendors); if found, retry with `ats` + `board` or `careers_url`. Otherwise drop it. |
| `vendor_not_supported_yet` | The company uses an ATS outside the 19. Drop it for now (its postings can still arrive through web search in step 4). |
| `budget_exhausted` | That call's overall request budget ran out. Verify the affected companies again in a new call. |
| `invalid_board` | The `board` is not a valid identifier for that ATS (a slug for most vendors; for workday, icims and taleo, a host on the vendor's own domain plus an optional path). Retry with the board's `careers_url` (or the company's own careers page, which the CLI follows to its board). |
| `ownership_unknown` | A guessed board answered (see `candidate`) but nothing ties it to the company. Open the board URL or search the web: if it is the company's own board (same website, same jobs), retry with that `ats` + `board`; if you only lacked the domain, retry with `domain`. Otherwise drop it. |
| `wrong_tenant` | The guessed board belongs to another company with a similar name. Look for the company's real careers page and retry with its board, or drop it. Never retry the `candidate` board. |
| `invalid_proposal` | The line had no `company`. Fix it. |

Put retries in a new file (e.g. `agent_proposals_retry.jsonl`) with only the corrected companies.
Do not spend more than one retry round per company; an unverified company is not a failure of the
run. Report to the user how many companies were added.

`discover feeds` (step 3) also writes company names it found to `seed_proposals_from_feeds.jsonl`, in
this same proposal format; pass that file directly to `ultron seeds verify --run <run_id> --from <run folder>/seed_proposals_from_feeds.jsonl --limit <new_companies_target>`
instead of copying a subset (`--limit` defaults to the run's `new_companies_per_run` for that file).
Boards verified from a file named `seed_proposals_from_feeds.jsonl` are stored with
`source_type: feed_company`; boards from any other file are stored as `agent_generated` and are
queried first in later runs.

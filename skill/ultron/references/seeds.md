# Proposing and verifying companies (step 2)

ULTRON searches the job boards of a company inventory ("seeds"). Each run you add companies that fit
the active profile; the CLI checks that their public job board exists and stores the verified ones for
this profile, so later runs reuse them.

## How many and which

- When `run new` printed `seed_deficit` above 0, follow "Seed rounds" below instead of this list's
  count: the CLI tells you how many companies to propose in each round.
- Otherwise propose about `new_companies_target` companies (printed by `run new`). A few extra is fine,
  since some will not resolve. Give `domain` for each so guessed boards can be tied to the company.
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
- Outside technology (healthcare, public sector, retail...) many employers use careers systems outside
  the 19 supported vendors, so fewer proposals verify. Still propose the full number: large employers
  (hospital groups, universities, retailers) often use Workday or iCIMS, which you can propose with
  their careers URL. Search by sector and place ("<role> jobs <city> <employer type>", "<employer> careers")
  rather than by remote or technology terms.

## Seed rounds (profiles with a seed deficit)

Bundled packs cover only some sectors and regions (technology in Europe and Latin America, healthcare
in the US and Ireland), and a pack that does not fit the profile is never queried. A profile with fewer
fitting boards than its preset's `seed_target` (35 `fast`, 75 `balanced`, 100 `deep`, 130 `seed_boost`)
gets the rest from you, in rounds:

1. `ultron seeds plan --run <run_id>` prints the next round: `ask` (how many companies to propose; it
   already allows for proposals that will not verify), `proposals_file` (the file name to write in the
   run folder), `schema` (a JSON Schema every line must match), `exclude_companies` (already in the profile or
   tried: propose one again only with a different, real board), `full_hosts` (ATS hosts already at the preset's per-host limit: more boards
   there add nothing this run, so prefer other vendors) and `more_rounds`.
2. Write up to `ask` lines (aim for `ask`) to `<run folder>/<proposals_file>`, one object per line, matching `schema`.
   Every line needs `company` plus `ats` + `board` or `careers_url`: look the board up (web search
   "<company> careers", the company's careers page) rather than guessing it. Spread the companies over
   employers that fit the profile's roles, sector and region, as in "How many and which". Prefer
   employers whose openings in the profile's region are mostly in its role family (hospitals and care
   providers for a nurse, not a global pharma board where nursing is a handful among thousands): every
   verified board counts toward the target, but a large off-family board mostly adds postings the filter
   has to discard. Fewer fitting boards is better than padding the round.
3. `ultron seeds verify --run <run_id> --from <run folder>/<proposals_file> --round` rejects the whole
   file, before any request, if a line does not match the schema (fix the lines it names and run it
   again). Otherwise it verifies the companies, skips repeats (a company the profile already has, or a
   board or careers URL tried before) and lines beyond `ask` (`over_ask_ignored`), and adds
   `seed_round` to its output: `fitting_seeds`, `seed_deficit`, `status`, `more_rounds` and `next_ask`.
4. While `more_rounds` is true, go back to 1. Rounds end when the target is met (`target_met`), after
   the preset's `seed_rounds` rounds (`max_rounds`), after a round that added no fitting board
   (`no_progress`) or after an ATS host blocked the requests (`host_blocked`). Tell the user how many
   fitting boards the profile has and, when the target was not met, the remaining deficit. Verified
   boards stay with the profile, so the next run starts with a smaller deficit.

Read `seeds_unresolved.jsonl` after each round. A company that failed may come back in the next round
only with a different, real board (see the reason table below); `budget_exhausted`, `host_blocked`,
`host_budget_exhausted` and `page_budget_exhausted` companies were never checked and may come back unchanged.

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
company name and domain on the probed ATSs (below). A guessed board must have open postings, and it is
**verified** for the company only when it provably belongs to it (another company may own the same slug):

- a slug equal to the main label of the proposal's `domain` (`acme` for `careers.acme.com`) is accepted;
- otherwise, for a name with two or more words (legal forms such as GmbH or Inc do not count), the ATS
  must report the same company name in its postings (only some ATS APIs include it, e.g. a
  `company_name` field);
- a one-word or short name ("Acme", "Acme GmbH") needs the domain slug.

A guessed board that answers without that proof is **still stored** (its postings are real, and each
result's company comes from the posting, not from the seed), but it is not counted as verified: it comes
back as `ownership_unknown` (no proof; stored under the proposal's name, tagged `ownership:unknown`) or
`wrong_tenant` (the ATS names another company; stored under that company's name, tagged
`discovered_via:<your proposal>`). Both are stored with a low priority (900) so their later yield decides
how often they are queried; `seeds_unresolved.jsonl` shows the board under `candidate` and how it was
stored under `stored_as`. Giving `domain` gets the company's own board verified instead.

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
| `host_budget_exhausted` | The call's request budget for that ATS host ran out before the board was checked. Verify the company again in a new call. |
| `page_budget_exhausted` | The company's careers page was not opened: one call opens at most 10 careers pages (2 per site). Verify the company again in a new call, or give its ATS board as `ats` + `board`. |
| `host_blocked` | The ATS host kept answering "too many requests" (or "forbidden"), so the board was not checked; the CLI stops calling that host for the rest of the call. Keep the company and verify it again in a later run; do not drop it. |
| `invalid_board` | The `board` is not a valid identifier for that ATS (a slug for most vendors; for workday, icims and taleo, a host on the vendor's own domain plus an optional path). Retry with the board's `careers_url` (or the company's own careers page, which the CLI follows to its board). |
| `unparseable_board` | The board page loaded but no listings could be read (e.g. a script-rendered Taleo career section). Drop it; its postings can still arrive through web search. |
| `ownership_unknown` | A guessed board answered (see `candidate`) and was stored tagged `ownership:unknown`, but nothing ties it to the company. Optional: if you know the company's domain or real board, retry with `domain` or `ats` + `board` to get it verified. |
| `wrong_tenant` | The guessed board belongs to another company (stored under that company's name). To find the proposed company's own board, look for its careers page and retry with that board; do not retry the `candidate` board. |
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

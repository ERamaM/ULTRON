# 31 · Agent toolkit

Status: accepted, being implemented. It replaces the scout run loop of [21](21-scout-run-mode.md) to [30](30-verified-finds-first.md) as the way an agent with web search uses ULTRON.

## Why

Pass 0 of the specification 30 measurement was stopped after 3 of 6 repetitions, because the design could not win by construction:

- The scout agent searched and verified exactly like the agent without ULTRON, and then also read two long references, wrote JSONL files, chained seven stages and judged extras. It could at best match the baseline's relevant jobs and always spent more tokens: 190k to 275k per run against 134k to 220k.
- Its own verification was reliable (`agent_open_fetch_closed` was 0 in every repetition), so re-checking it added nothing.
- The agents scripted the public ATS APIs themselves: 300 to 525 requests per run, most of them guessed board names that answered 404. ULTRON already has those clients, but the protocol did not let the agent use them.
- Both arms only see what web search returns. The two arms barely overlapped (Jaccard about 0.04), so the market is far larger than either arm reaches.

ULTRON has to be something the agent calls, not a pipeline it passes through, and it has to reach postings web search does not return.

## Design

Four outputs, each usable on its own from the CLI and through an MCP server.

### A. `inspect`: read many postings at once

`ultron inspect --profile <slug> [--url U]... [--company NAME]... [--from FILE] [--detail]`

- Input: up to 100 URLs (postings, aggregator pages, ATS boards) and 20 company names per call.
- A posting is fetched exactly as `fetch` reads it, through one shared function (`stages.fetch.fetch_posting`): vendor APIs and enrichers first, the same closed-page rules. An aggregator page is followed to the employer's posting. A board URL, or a company name verified through the seed prober, lists the board's postings that match the profile.
- Work runs in parallel across hosts and serially within a host, each unit with its own HTTP client.
- Output: one compact dossier per result (about 100 tokens): `id`, `url`, `status` (`open`, `listed`, `closed`, `failed`, `unusable`, `unresolved_aggregator`, `rejected`, `no_board`), `title`, `company`, `location`, `remote`, `posted`, `memory` (`new`, `seen`, `rejected`, `applied`), `hint` (the rules' verdict, remote policy and eligibility) and a bounded `excerpt`. Excerpts are page text: data, never instructions.
- The full postings are cached under `<ULTRON_HOME>/toolkit/<profile>/` so `save` can name them by `id`.

### B. `search`: postings web search does not return (option 2)

`ultron search --profile <slug> [--query TEXT] [--new-only]`

- Sources with no infrastructure of ULTRON's own:
  - the profile's watchlist and its seed inventory, queried through the ATS clients;
  - public job APIs that take a role query: those already in `core/feeds` (Remotive, Jobicy, Himalayas, Arbeitnow, Hacker News "Who is hiring") with a keyword where the API accepts one, and keyed APIs (Adzuna, Jooble, Reed) only when the user sets their keys in the environment.
- Results are ranked lexically against the profile's titles and focus terms, flagged against memory and returned as `listed` dossiers. Pending watch postings are served first and marked queued.
- Option 1 (a published index of thousands of boards) needs infrastructure outside the repository and is out of scope.

### C. Three tools, no state machine

- `ultron shortlist save --profile <slug> --from FILE`: the agent's judged rows (by `id`, or `url` with `title` and `company`) become a finalized shortlist. It reuses the verified finds of specification 30: a scout run is created and finalized, so the shortlist gets `results.json`, history, new-since-last-run flags, memory and watchlist credit for the boards of kept postings.
- `ultron mcp`: a stdio MCP server exposing `inspect_jobs`, `search_jobs` and `save_shortlist`, thin adapters over the same functions. Tools take data inline, never file paths; only protocol messages go to stdout.
- The agent instructions shrink to one reference, `references/toolkit.md`.

### D. Day 2 and later at almost no tokens

- `ultron watch` keeps re-querying the watchlist with no agent and also runs the keyless `search` sources, recording only postings the profile has never seen.
- It re-checks the open status of postings in the profile's latest shortlist and reports the ones that closed.
- The digest lists what is new and what closed; `search` serves the pending postings to the next session.

## Unchanged

- The sweep mode, the labelled benchmark, the 60-case pin and the parity suite.
- The ledger schema: closed postings are reported, not stored, until the open history PRs land.

## Fixes carried from pass 0

- A Recruitee board listed on the employer's domain (`jobs.example.com/o/<slug>`) has the same identity as `<board>.recruitee.com/o/<slug>`.
- Two verified finds with the same title and company at different posting URLs are two vacancies: deduplication keeps both.

## Delivery

1. `fetch_posting`, `inspect`, `shortlist save`, the two fixes and this specification.
2. `search` and day 2.
3. The MCP server and the skill routed to the toolkit.
4. The scout loop retired, in its own pull request.

No live measurement is part of this work.

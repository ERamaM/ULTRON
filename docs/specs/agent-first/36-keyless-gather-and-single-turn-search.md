# 36 · Keyless gather and single-turn search

Status: proposed. Supersedes the multi-turn agent-driven web-search loop of [31 · Agent toolkit](31-agent-toolkit.md) as the default Day-1 interactive search workflow. Keeps `inspect`, `search`, `shortlist save`, `watch`, `watchlist`, and `mcp` intact.

## Why

The blind comparison of [31 · Agent toolkit](31-agent-toolkit.md) (`#104`–`#108`, CLI runs against three baselines per profile) proved two things at once:

1. **ULTRON reaches real openings that an autonomous agent alone misses.** In a single run per profile, the toolkit arm found 9 relevant AI roles and 8 relevant Finance roles that none of the three baseline runs discovered, and Day-2 `ultron watch` ran at 0 agent tokens in ~30 seconds.
2. **Yet ULTRON still lost on Day-1 cost, speed, and Finance recall:**

| Metric | AI baseline (mean of 3) | AI toolkit (1 run) | Finance baseline (mean of 3) | Finance toolkit (1 run) |
|---|---|---|---|---|
| Relevant jobs per run | 16.3 | 16 | 22.0 | 19 |
| P@5 / P@10 | 1.00 / 0.93 | 0.80 / 0.90 | 1.00 / 1.00 | 1.00 / 1.00 |
| Ineligible rate | 0% | 0% | 3% | 14% |
| Tokens per run | 195k | 282k (+45%) | 143k | 215k (+50%) |
| Wall-clock time | 18 min | 32 min | 11 min | 42 min |
| Exclusive relevant jobs | 27 (across 3 runs) | 9 (in 1 run) | 21 (across 3 runs) | 8 (in 1 run) |

Four concrete causes drove that loss:

- **ULTRON added work on top of the agent instead of replacing it.** Because `ultron search` only queries small keyless feeds and watched/day-1 boards with zero web search, the agent still ran 40+ built-in web searches, fetched raw pages, and executed 8–9 CLI `inspect` rounds.
- **Heuristic `hint` fields misled the agent.** `posting_dossier()` attached `hint` (`verdict`, `remote_policy`, `eligibility`), which misclassified `"EMEA Remote"` and `"hybrid"` postings. That lowered precision and taught the agent to distrust dossiers and open raw web pages anyway.
- **Aggregator listings under-resolved.** Many listings (notably `himalayas.app`) ended in `unresolved_aggregator`, or arrived as unread `listed` stubs requiring extra agent turns to inspect.
- **Shell CLI round-trips inflated turns and context.** Running repeated CLI commands with temporary JSONL files cost far more tokens than one MCP call returning ready-to-judge dossiers.

To beat an autonomous agent decisively on Day 1, ULTRON must move **web discovery, ATS resolution, board expansion, aggregator fallback, and pre-ranking into Python at 0 LLM tokens**, using **zero third-party API keys**, and hand the agent 50–60 clean, verified `open` dossiers for a **single-turn judgment** with no redundant web searches or page fetches.

## Product constraints

- **Zero third-party API keys.** Web discovery must work out of the box without requiring paid or keyed search APIs (no Brave, Serper, Tavily, Google CSE, or Exa keys). Existing opt-in env-var sources (`ADZUNA_*`, `REED_API_KEY`) remain untouched and strictly optional.
- **Any role family and any region.** Query generation and lexical pre-ranking must read the profile's own titles, focus terms, specialties, `location_scope`, and `work_mode`; never hardcode tech or Europe assumptions.
- **Strict security and architecture boundaries.** Follow [docs/architecture.md](../../architecture.md): one-way package layering (`infra/` → nothing; `core/` → `core` + `infra`), SSRF-safe public fetches via `HttpClient` + `PinnedTransport`, strict loopback-only transport for local SearXNG, linear ReDoS-safe regexes, and `ultron.__file__` asset resolution.
- **Cross-platform.** Must work on Windows, macOS, and Linux, including when Docker is not installed.

## Design

### 1. Keyless web-search layer (3-tier cascade)

ULTRON introduces a keyless web-search layer ported cleanly from the proven MARK II / MARK III SearXNG + DuckDuckGo stack, adapted to ULTRON's `httpx`-based `infra/` and `core/` architecture.

Provider selection is controlled by `--web-provider` (CLI/MCP) or `ULTRON_WEB_SEARCH_PROVIDER` (env), defaulting to `auto`:

| Value | Behavior | Requirements |
|---|---|---|
| `auto` (default) | Health-checks local SearXNG (1.5 s timeout); uses `searxng` if healthy, else `ddgs` if optional extra installed, else built-in `ddg_html`. | None (Docker optional) |
| `searxng` | Local self-hosted SearXNG JSON API (or `ULTRON_SEARXNG_URL`). Multi-engine (Google, Bing, Brave, Startpage, DuckDuckGo, Qwant, Yahoo, Mojeek, etc.), parallel, immune to single-engine rate limits. | Local Docker (or user SearXNG) |
| `ddg` | Keyless multi-backend `ddgs` Python package (`ultron[search]` optional extra), falling back to `ddg_html` on failure. | `ddgs` optional extra |
| `ddg_html` | Built-in DuckDuckGo HTML/Lite client (`html.duckduckgo.com/html/` with `lite.duckduckgo.com/lite/` fallback) using ULTRON's `HttpClient` + `BeautifulSoup`. | Zero extra dependencies |
| `multi` | Runs `searxng` and `ddg_html` in parallel and deduplicates hits by normalized URL. | Local Docker |
| `none` | Skips web search (runs feeds, watch, and boards only). | None |

#### 1.1 Local self-hosted SearXNG (`ultron searxng`)

A lightweight CLI command manages a single local SearXNG container for users who have Docker installed:

- `ultron searxng up [--port 8888]` — stages bundled config, creates a per-install random secret key if missing, starts the container bound strictly to `127.0.0.1:<port>:8080`, waits for JSON health check, and prints JSON status.
- `ultron searxng down` — stops and removes the ULTRON-managed container only.
- `ultron searxng restart [--port 8888]` — restarts the container and checks health.
- `ultron searxng status` — reports Docker availability, container state, loopback URL, and JSON API health.
- `ultron searxng diagnose` — returns Docker version, container inspect summary, health probe result, and recent container logs with any secret value redacted.

Container and config invariants:

- **Pinned image digest:** pin the official `ghcr.io/searxng/searxng` image by version tag and immutable `@sha256:` digest.
- **Loopback bind only:** publish `-p 127.0.0.1:<port>:8080` (never `0.0.0.0`). Default port `8888` (overridable via `--port` or `ULTRON_SEARXNG_PORT`, validated as `1024 <= port <= 65535`).
- **Bundled `settings.yml`:** ship `skill/ultron/data/searxng/settings.yml` (included in the wheel via `force-include`, resolved from `ultron.__file__`). Enable `formats: [html, json]`, `server.limiter: false`, `server.image_proxy: false`, conservative `outgoing` timeouts (`8.0s` / `12.0s`), per-engine `suspended_times` for 429/captcha so one blocked engine never stalls the search, and the general multi-engine list (Google, Bing, DuckDuckGo, Startpage, Brave, Qwant, Yahoo, Mojeek, etc.).
- **Per-install secret:** generate a 32-byte random hex secret once under `<ULTRON_HOME>/searxng/secret_key` using `ultron.infra.fs` atomic writes with `0o600` permissions on POSIX, pass it via `SEARXNG_SECRET` to the container (replacing the `ultrasecretkey` placeholder in `settings.yml`), never log it, and redact it in `diagnose` output.
- **Cross-platform Docker support:** execute `docker` with argv lists (`shell=False`, never shell interpolation). Support `docker compose` v2 and fall back to plain `docker run` with a fixed container name (`ultron-searxng`). Copy `settings.yml` into `<ULTRON_HOME>/searxng/config/settings.yml` before bind-mounting so Docker Desktop file sharing works on macOS, Windows, and Linux even when ULTRON runs from a virtualenv or temp install folder.

#### 1.2 Loopback HTTP isolation (`ultron/infra/loopback.py`)

`infra/urlpolicy.py` rightly blocks loopback addresses for all public web traffic. Never weaken `urlpolicy.py` for SearXNG. Instead, add a dedicated, minimal loopback reader in `ultron/infra/loopback.py`:

- `ExplicitLoopbackOrigin(host, port)` and `explicit_loopback_origin(url) -> ExplicitLoopbackOrigin | None`: accepts **only** `http://127.0.0.1:<port>` or `http://localhost:<port>` with an explicit port (`1 <= port <= 65535`), path `""` or `"/"`, and no credentials or fragment.
- `read_loopback(origin, url, *, timeout, max_bytes=2_000_000) -> tuple[int, bytes]`: verifies `url` matches `origin` (scheme, host, port, no credentials/fragment), uses `httpx.Client(trust_env=False, follow_redirects=False)` so process proxy env vars (`HTTP_PROXY`/`HTTPS_PROXY`) are ignored, raises `URLRejected("redirect_rejected")` on any `3xx` status, and enforces `max_bytes` while streaming chunks.
- If a user explicitly sets `ULTRON_SEARXNG_URL` to a non-loopback `https://` origin (e.g., their own homelab server), requests MUST go through the standard SSRF-guarded `HttpClient` (`admit_url` + `PinnedTransport`), never `read_loopback`.

#### 1.3 Query clients and relaxation (`ultron/core/web_search/`)

- **SearXNG client (`searxng.py`):** executes queries in parallel with `ThreadPoolExecutor` (default 4 workers), queries `GET <base>/search?q=<query>&format=json&categories=general&pageno=<n>`, retries transient 5xx/timeouts with short backoff, caps pagination (`max_pages=1` by default, max `2`), and tries automatic relaxed variants (unquoted phrase, ATS host rewrites such as `site:boards.greenhouse.io` ↔ `site:job-boards.greenhouse.io` and `site:jobs.lever.co` ↔ `site:lever.co`) when a strict query returns zero hits.
- **DuckDuckGo HTML client (`ddg_html.py`):** sends queries through `HttpClient` restricted to `allowed_domains=("html.duckduckgo.com", "lite.duckduckgo.com")` with polite host spacing (`min_interval_per_host >= 1.5s`, bounded concurrency `1`), parses `div.result` (`a.result__a`, `.result__snippet`) with `tr` (`a.result-link`, `td.result-snippet`) fallback, skips `result--ad` and `/y.js` ad links, and unwraps `/l/?uddg=...` redirect URLs.
- **Optional `ddgs` adapter (`ddgs_client.py`):** lazy-imports `ddgs` (packaged under optional extra `search = ["ddgs>=8,<10"]` in `skill/ultron/scripts/pyproject.toml`), maps hits to `SearchHit(title, url, snippet, query, provider)`, and raises `ProviderUnavailable` cleanly when not installed.
- **Graceful degradation:** if all configured web providers fail or yield 0 hits, `cascade.py` never aborts `gather`; it records `status: "unavailable"` (or `"degraded"`) and sets `fallback_to_agent_web_search: true` so the agent knows to run its own built-in web searches as backup.

### 2. `ultron gather` and MCP `gather_candidates`

One command and one MCP tool replace the multi-round search/inspect choreography on Day 1:

- **CLI:**
  ```text
  ultron gather --profile <slug> [--query TEXT] [--limit 60] [--max-queries 20]
                [--web-provider auto|searxng|ddg|ddg_html|multi|none]
                [--new-only] [--detail] [--preset balanced]
  ```
- **MCP tool (`ultron mcp`):** `gather_candidates` with inline JSON arguments (`profile`, optional `query`, `limit` `1..100` default `60`, `max_queries` `1..40` default `20`, `web_provider`, `new_only`, `detail`). Never accepts file paths.

#### Execution pipeline (0 LLM tokens, target 20–45 s)

1. **Profile-driven query plan (`core/web_search/query_builder.py`):**
   - Builds up to `max_queries` (default 20; capped at 10 automatically when falling back to `ddg_html` to avoid rate limits) interleaved queries from the profile (or `--query` override):
     - **ATS-scoped queries (~60%):** `site:` queries across high-yield ATS hosts (`boards.greenhouse.io`, `job-boards.greenhouse.io`, `jobs.ashbyhq.com`, `jobs.lever.co`, `apply.workable.com`, `jobs.smartrecruiters.com`, `recruitee.com`, `teamtailor.com`, `jobs.personio.de`) combined with target titles and location/work-mode terms.
     - **Broad role queries (~40%):** un-scoped role + region/remote queries to surface direct employer career pages and ATS platforms outside the top hosts.
   - Respects non-European and non-remote profiles: region/work-mode qualifiers come strictly from `location_scope.preferred`, `criteria.work_mode`, and `query_templates`.

2. **Parallel multi-source discovery:**
   - Runs **in parallel**:
     - Keyless web search (`core.web_search.cascade`),
     - Pending `watch` postings,
     - Watched ATS boards (or Day-1 seed inventory boards when the watchlist is empty),
     - Keyless role APIs and feeds (`Remotive`, `Himalayas`, `Jobicy`, `arbeitnow`, `hn_who_is_hiring`; plus `Adzuna`/`Reed` only if env vars are already configured).

3. **Pre-filter, deduplicate, and skip terminal memory before fetching:**
   - Normalize and deduplicate all discovered URLs (`core/web_search/dedup.py`), preserving `(url, company_hint, source, snippet)`.
   - Extract `company_hint` from feed metadata, aggregator URL paths, or web search result titles (e.g., `"Senior ML Engineer at Acme"`, `"Acme - Staff Data Engineer"`).
   - Drop URLs rejected by `classify_find()` (`admit_url`), non-posting noise (social profiles, login pages, root domains without careers paths unless ATS board), and URLs whose canonical identity is already `rejected` or `applied` in profile memory **before** issuing HTTP fetches.
   - If `--new-only` is set, also skip identities already marked `seen`.
   - Prioritize candidate URLs for inspection (ATS postings > watched/feed listings > direct employer pages > aggregator listings) up to a bounded inspection cap (default 120 unique URLs + 20 company probes, sharing one `HostGate` and `VendorBudget`).

4. **Parallel inspection, `same_board` expansion, and aggregator fallback:**
   - Reuses `toolkit.inspect`'s parallel worker engine (`_Inspector` / extracted public helper) so ATS boards on `ashby`, `lever`, and `greenhouse` are fetched in bulk (bringing `same_board` matching siblings automatically) and every posting goes through the single canonical `stages.fetch.fetch_posting` + closed-page check.
   - Automatically reads high-ranking `listed` postings returned by feeds or non-text ATS boards so the returned batch consists predominantly of verified **`open`** dossiers with excerpts, not unread stubs.
   - **Improved aggregator resolution:**
     - Broaden `aggregator_company(url)` and title-based company extraction for common aggregator URLs (`himalayas.app/companies/<slug>/...`, `workingnomads.com`, `remoterocketship.com`, `wellfound.com`, `jobgether.com`, `builtin.com`) using linear, bounded regexes.
     - When `resolve_aggregator()` returns `unresolved_aggregator` and an employer name is known (from feed metadata, URL path, or search snippet), run the existing `company_dossiers()` fast ATS probe (`via: company_of_listing`).
     - When SearXNG is active and up to 8 top-ranked aggregator listings remain unresolved with a known company and title, issue at most 1 targeted ATS lookup query per listing (`<company> <short title> site:greenhouse.io OR site:ashbyhq.com OR site:lever.co`) to locate the canonical employer URL and inspect it.
     - Unresolved aggregator pages are never placed in `dossiers`; they are listed under `unresolved` for transparency.

5. **Local lexical pre-ranking and diversity cap (`ultron/core/ranker/lexical.py`):**
   - Ranks `open` dossiers first (then remaining `listed` dossiers only if `open` count is below `limit`) using a fast, deterministic BM25F-style lexical scorer (0 LLM tokens):
     - **Title match (highest weight):** overlap with profile `target_titles`, `role_focus_terms`, or explicit `--query`.
     - **Body/excerpt match (medium weight):** overlap with `specialties`, `must_have_terms`, `role_signal_terms`, and location/remote tokens from `location_scope.preferred` and `work_mode`.
     - **Objective freshness & provenance boosts (small tie-breakers):** `source == "watch"`, `memory == "new"`, verified ATS board posting, recent `posted` date (<= 14 days / <= 30 days).
     - **Soft down-rank (never hard discard):** postings whose title matches profile `excluded_titles` or `exclude_terms` receive a score penalty rather than silent deletion, keeping recall visible while pushing clear mismatches below the top slice.
   - **Per-company diversity cap:** keep at most `MAX_PER_COMPANY = 3` dossiers per employer in the returned top `limit` slice so one large board never crowds out other companies.
   - Deduplicate by canonical identity and `(company.casefold(), title.casefold())`, preferring `open` over `listed`.

6. **Inspect cache & response contract:**
   - Persist all returned postings into `<ULTRON_HOME>/toolkit/<profile>/inspected.jsonl` (same format as `inspect` / `search`) so `shortlist save` / `save_shortlist` can save any row by `id` alone.
   - Mark served `watch` postings as queued (same as `search`).
   - Return one compact JSON object:
     ```json
     {
       "profile": "ai-ml-remote-eu",
       "queries": ["site:jobs.ashbyhq.com \"AI Engineer\" remote europe", "..."],
       "web_search": {
         "provider": "searxng",
         "status": "ok",
         "queries_run": 20,
         "raw_hits": 248,
         "unique_urls": 134,
         "fallback_to_agent_web_search": false
       },
       "sources": {
         "watch": {"status": "ok", "results": 4},
         "boards": {"queried": 40, "failed": 1, "results": 27},
         "Remotive": {"status": "cached", "results": 6},
         "Himalayas": {"status": "ok", "results": 11}
       },
       "inspected": {
         "attempted": 118,
         "open": 74,
         "listed": 9,
         "closed": 21,
         "failed": 6,
         "unresolved_aggregator": 8,
         "skipped_memory": 14,
         "requests": 96
       },
       "returned": 60,
       "dossiers": [],
       "unresolved": []
     }
     ```

### 3. Factual dossiers — remove heuristic `hint`

Across both `ultron inspect` and `ultron gather` (and their MCP equivalents):

- **Remove `hint` from `posting_dossier()`** in `ultron/toolkit/dossier.py`, and remove `hint`-based sorting in `ultron/toolkit/inspect.py` (sort `inspect` results by `status` then input order / lexical score instead).
- **Widen excerpts slightly** so eligibility and work-mode clauses are never truncated mid-sentence:
  - `GEO_EXCERPT`: `250` → `350` chars
  - `ROLE_EXCERPT`: `500` → `700` chars (`DETAIL_ROLE_EXCERPT` stays `2500` chars)
- **Keep dossiers strictly factual:** `id`, `url`, `apply_url` (when different), `status`, `title`, `company`, `location`, `remote`, `posted`, `salary`, `memory`, `source`, `board`, `via`, `board_confirmed`, `found_at`, `geo_excerpt`, `role_excerpt`.
- **Unchanged:** `core/rules/**`, the sweep / unattended pipeline (`ultron run --unattended`), the 60-case pin, and `tests/core/test_parity_heuristics.py` remain untouched.

### 4. Single-turn agent loop (`SKILL.md`, `references/toolkit.md`, `ultron mcp`)

Update the agent skill prompts (keeping them platform-agnostic English with no vendor-specific tool names, consistent with `ultron --help`):

1. **Gather once:** call `gather_candidates` (MCP) or `ultron gather --profile <slug> --new-only` (CLI).
2. **Judge in one pass:** read the returned `open` dossiers and judge each against the profile criteria (role fit, seniority, country/region eligibility, language, work mode, exclusions).
   - **Do not run built-in web searches** when `web_search.fallback_to_agent_web_search` is `false` and the gathered batch already yields the target number of strong `keep`s (25 by default).
   - **Do not open raw web pages** for postings that have an `open` dossier; if a high-priority posting has a truncated excerpt, call `inspect_jobs` with `detail: true` first.
   - **At most one targeted follow-up:** if fewer than the target `keep`s are found (or `fallback_to_agent_web_search` is `true`), run one follow-up call (`gather_candidates` with a narrower `query`, or one batch of built-in web searches passed to `inspect_jobs`), then stop.
3. **Save and summarize:** call `save_shortlist` (MCP) or `ultron shortlist save --profile <slug> --from <file>` with `keep`, `unsure`, and `discard` rows, best first, and present the ranked summary and `results.md` path.

Update `ultron mcp` server `instructions` to:
`"Job search tools: gather_candidates (one-shot discovery and dossiers), inspect_jobs (detail or extra URLs/companies), search_jobs (watch/boards/feeds only), judge, then save_shortlist."`

Update `ultron doctor` to add a non-blocking informational check `web_search` (passes `ok: true` whenever at least built-in `ddg_html` is available, while noting in `detail` whether local SearXNG or `ddgs` is active and suggesting `ultron searxng up` in `fix` only as an optional upgrade note when Docker is detected).

## Architecture and module layout

All new modules obey [docs/architecture.md](../../architecture.md) and `tests/test_architecture.py`:

| Module / Asset | Package owner | Allowed internal imports | Responsibility |
|---|---|---|---|
| `infra/loopback.py` | `infra` | `infra.urlpolicy` | Strict loopback-only `httpx` reader (`trust_env=False`, no redirects, bounded bytes) |
| `infra/searxng.py` | `infra` | `infra.{fs, home, loopback}` | Cross-platform Docker lifecycle (`up`, `down`, `restart`, `status`, `diagnose`), `0o600` secret, log redaction |
| `skill/ultron/data/searxng/settings.yml` | package data | n/a | Bundled SearXNG config loaded via `ultron.__file__` |
| `core/web_search/models.py` | `core` | `core` | `SearchHit`, `WebSearchResult`, `WebSearchConfig` |
| `core/web_search/query_builder.py` | `core` | `core` | Profile-driven ATS dork + broad query plan and linear query-relaxation variants |
| `core/web_search/dedup.py` | `core` | `core` | URL normalization, snippet company extraction, hit deduplication |
| `core/web_search/searxng.py` | `core` | `core`, `infra.{loopback, net}` | Parallel SearXNG JSON client with quote/host relaxation and retry |
| `core/web_search/ddg_html.py` | `core` | `core`, `infra.net` | Built-in DuckDuckGo HTML/Lite client and BeautifulSoup parser |
| `core/web_search/ddgs_client.py` | `core` | `core` | Optional lazy adapter over `ddgs` (`ultron[search]`) |
| `core/web_search/cascade.py` | `core` | `core`, `infra` | `auto` / `searxng` / `ddg` / `ddg_html` / `multi` / `none` selector and orchestrator |
| `core/ranker/lexical.py` | `core` | `core` | Deterministic BM25F-style dossier pre-ranker and per-company diversity cap |
| `toolkit/gather.py` | `toolkit` | `core`, `infra`, `stages`, `storage`, `toolkit` | `gather_candidates` end-to-end orchestration and cache write |
| `commands/searxng.py` | `commands` | `commands.output`, `infra.searxng` | `ultron searxng {up,down,restart,status,diagnose}` CLI subcommands |
| `commands/toolkit.py` (updated) | `commands` | `commands.output`, `toolkit.gather` | Adds `ultron gather` CLI subcommand |
| `toolkit/mcp_server.py` (updated) | `toolkit` | `toolkit.gather` | Adds `gather_candidates` MCP tool |

Update [docs/architecture.md](../../architecture.md) in the same PR that adds each package/module. Keep every module well under 500 lines and all `__init__.py` files empty.

## Security and reliability checklist

- **SSRF & loopback boundary:** `read_loopback` never talks to non-loopback hosts, never follows redirects, and never trusts proxy env vars. Remote SearXNG URLs and all external job/aggregator/DDG URLs go exclusively through `HttpClient` (`admit_url` + `PinnedTransport` + `VendorScopedClient`).
- **Docker safety:** argv-only `subprocess.run(..., shell=False)`, pinned `@sha256:` image digest, `127.0.0.1` port binding only, `0o600` secret file, secret redaction in `diagnose`/logs, no host path traversal.
- **ReDoS safety:** all regexes in `query_builder.py`, `dedup.py`, and `aggregator_company()` use strictly linear patterns with bounded quantifiers (`S8786`).
- **Untrusted content:** search titles, snippets, and posting excerpts are treated strictly as data (length-bounded, whitespace-collapsed, never interpolated into shell commands or prompts as instructions).
- **No PII / secret leakage:** never log `SEARXNG_SECRET`, profile personal details, or env-var API keys.

## Acceptance tests (offline, synthetic, `xdist`-safe)

Every PR must maintain `>= 80%` coverage on new/changed lines and pass `pytest`, `pytest tests/test_architecture.py`, `pytest tests/core/test_parity_heuristics.py`, and `ruff check`:

1. **Loopback guard:** `read_loopback` accepts `http://127.0.0.1:8888` and `http://localhost:8888`; rejects `0.0.0.0`, external IPs, `https://`, missing port, credentials, paths, fragments, `301`/`302` redirects, oversized streams, and proxy env vars.
2. **SearXNG lifecycle:** mocked `subprocess.run` and loopback probe verify `up`/`down`/`restart`/`status`/`diagnose`, pinned digest, `127.0.0.1` port binding, `0o600` secret creation, secret redaction in logs, `docker compose` → `docker run` fallback, and config staging under `ULTRON_HOME`.
3. **Web search clients & cascade:** synthetic JSON/HTML fixtures verify SearXNG parallel queries, unquoted and ATS-host rewrite fallback, DDG HTML & Lite parsing (ad skipping, `/l/?uddg=` decoding), optional `ddgs` absence handling, `multi` dedup, and `auto` fallback setting `fallback_to_agent_web_search: true` when all providers fail.
4. **Query builder & lexical pre-ranker:** verifies non-EU and non-tech profiles produce clean role/region queries (no hardcoded EU/AI terms), BM25F ranks exact title/region matches above noisy ones, `excluded_titles` soft-penalizes without hard-dropping, and per-company cap limits any single employer to 3 slots in top `limit`.
5. **Gather & dossier contract:** mocked `gather_candidates` runs web + feeds + watch + boards, pre-skips `rejected`/`applied` memory before fetching, expands `same_board` and aggregator company fallbacks, omits `hint` from every dossier (preserving `"EMEA Remote"` and `"hybrid"` in `geo_excerpt`), populates `inspected.jsonl` so `shortlist save` succeeds by `id` alone, and exposes `gather_candidates` over `ultron mcp`.

## Live benchmark pass criteria

After implementation, run one blind, interleaved comparison via `tools/arm_comparison.py` following [docs/self-improvement.md](../../self-improvement.md#baseline-comparison-ultron-against-an-agent-without-it) on **two profiles** (AI engineering and Finance), **3 repetitions per arm**, comparing:

- `baseline`: autonomous agent with built-in web search/fetch only.
- `gather_mcp`: agent using ULTRON MCP (`gather_candidates`, `inspect_jobs`, `save_shortlist`) with local SearXNG running (`ultron searxng up`).
- Optional fallback sanity check (1 rep per profile): `gather_ddg` with `--web-provider ddg` (no Docker) to confirm graceful zero-Docker performance.

Specification 36 passes when `gather_mcp` meets **all** criteria against `baseline` on **both** profiles:

| Metric | Required vs `baseline` |
|---|---|
| Relevant jobs per run | at least equal (target: higher) |
| P@5 and P@10 | at least equal |
| Ineligible rate | at most equal (target: <= 5%) |
| Tokens per run | <= 50% of `baseline` (target: <= 80k) |
| Wall-clock time per run | <= 50% of `baseline` (target: <= 5 min) |
| Exported aggregator URLs | 0 |
| Day-2 `ultron watch` (>= 24 h later) | finds new watchlist openings at 0 agent tokens |

## Suggested delivery (phased PRs)

Implement in three small, theme-grouped pull requests:

1. **PR 1 — Keyless web-search layer & SearXNG manager:** `infra/loopback.py`, `infra/searxng.py`, bundled `data/searxng/settings.yml`, `core/web_search/*`, `commands/searxng.py`, `doctor.py` informational check, `pyproject.toml` `[search]` optional extra, `docs/architecture.md`, unit tests.
2. **PR 2 — `ultron gather`, lexical pre-ranking & factual dossiers:** `core/ranker/lexical.py`, `toolkit/gather.py`, `toolkit/dossier.py` (remove `hint`, widen excerpts), `toolkit/inspect.py` (broaden aggregator company extraction, drop `hint` sort), `toolkit/mcp_server.py` (`gather_candidates`), `commands/toolkit.py` (`ultron gather`), `docs/architecture.md`, unit tests.
3. **PR 3 — Single-turn skill prompts & docs:** `skill/ultron/SKILL.md`, `skill/ultron/references/toolkit.md`, `skill/ultron/references/commands.md`, CLI help/doc consistency tests.

No live benchmark run is required before merging PRs 1–3; run the live measurement once all three PRs are merged.

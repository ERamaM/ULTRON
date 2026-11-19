# Search profiles

A profile is a YAML file describing what the user is looking for. Every run snapshots one profile;
filters, ranking, the query plan and your evaluations all read it.

## Where profiles live

- User profiles: `<ULTRON_HOME>/profiles/<slug>.yaml` (default `~/.ultron/profiles/`). The file name
  must be the slug.
- A bundled example (`ai-ml-remote-eu`) ships with the skill. `ultron profile install-example` copies
  it into the user folder and prints its path, a good template to start from. If the user already
  edited that copy it stops with `profile_exists` instead of overwriting it; pass `--force` only if the
  user agrees to lose those edits.
- `ultron profile list` → `{"profiles": [{"slug", "name", "path"}]}` (user profiles override bundled
  ones with the same slug). `ultron profile show <slug>` prints the whole profile.

## Schema

Required: `slug`, `name`, `criteria` with a non-empty `target_titles` or `role_focus_terms`. Every
term list must be a list of strings. Terms match as whole words or phrases, ignoring case and
accents, so use short terms as they would appear in postings (`ml engineer`, not `ML Engineers`).

```yaml
slug: data-eng-remote-spain          # ^[a-z0-9][a-z0-9-]*$, same as the file name
name: Senior Data Engineer - Remote Spain/EU
description: one line for humans
enabled: true
source_cv: /home/me/cv/cv.pdf        # optional: path of the CV this profile was generated from (metadata only; null/absent otherwise)
criteria:
  sectors: [technology]              # informative
  role_family_label: Data engineering   # human name of the role family
  role_focus_terms: [data engineer, analytics engineer, etl engineer]   # core role family (STRICT)
  target_titles: [Senior Data Engineer, Staff Data Engineer]           # ideal titles
  seniority:
    required: true                   # false = any level
    preferred_terms: [senior, sr., staff, principal, lead]
    excluded_terms: [intern, junior, graduate, entry]
  remote_policy:
    required: full_remote            # full_remote | any
    positive_terms: [remote, remote-first, work from home, distributed]
    negative_terms: [hybrid, onsite, on-site, in-office]
  location_scope:
    preferred: [Spain, Remote EU, Remote Europe]     # target hiring scopes
    eligibility_terms: [spain, remote eu, remote europe, within eu, cet]
    excluded_terms: [us only, uk only, north america only]
  exclusion_terms: [manager, director, intern]       # lower the score, never reject alone
  specialties: [Spark, dbt, Airflow, streaming]      # shown as fit signals
  fallback_fit_signals: [Data]
  role_signal_terms: [data pipeline, etl, data warehouse, spark, dbt]  # body terms that confirm the family
  role_ambiguity_terms: [data, pipeline]             # terms that make a title ambiguous
  data_scientist_signal_terms: []                    # terms that make a "data scientist" title count
  vocabulary_packs: []               # optional opt-in role vocabulary, e.g. [ai-ml] for AI/ML profiles (see below)
query_templates:
  core_titles: [Senior Data Engineer, Staff Data Engineer]   # x regions = the run's query plan
  titles: [Senior Data Engineer, Analytics Engineer, Senior ETL Engineer]
  regions: [Remote Spain, Remote EU, Remote Europe]
  site_scopes: [boards.greenhouse.io, job-boards.greenhouse.io, jobs.lever.co, jobs.ashbyhq.com]
  technologies: [Spark, dbt, Airflow, Kafka, Snowflake]
  specialties: [streaming, lakehouse]
  exclusion_terms: [manager, director]
worked_examples:                     # optional; guide your evaluations
  - input: Senior Data Engineer, Remote EU, Spark pipelines, accepting applications.
    output: {verdict: keep, keep: true, is_allowed_family: true, is_senior: true, is_full_remote: true, eligibility: high}
```

### Vocabulary packs

`vocabulary_packs` names files in the bundled `data/vocabulary/` folder (today only `ai-ml`); an
unknown name fails `profile validate`. Packs are opt-in and **only for AI/ML profiles** today:

- A pack always adds family-aware signals (discovery and coarse-filter terms, title synonym groups,
  ambiguity hints) for its family.
- Its `role_terms` become the default role terms **only when the profile has no `role_focus_terms`**
  (a title-only profile). If you list `role_focus_terms`, those are used and the pack's role terms
  are not.
- With no pack (every other role family: backend, finance, sales, legal, nursing...), the profile's
  own terms alone decide: `role_focus_terms`, else `target_titles`. Role-family discard rules for
  finance/sales/legal and similar use default negative terms. They are dropped automatically when a
  word in the profile's `role_focus_terms`, `target_titles` or `role_signal_terms` starts with them
  (finance matches financial), so those profiles are not filtered out by mistake.

For a general profile, put the family in `role_focus_terms` (short terms as they appear in postings)
and list the real job titles in `target_titles`; no pack is needed.

### Geography scope

The scope is derived from `criteria.location_scope.preferred`:

| `preferred` | scope | effect |
|---|---|---|
| contains a European region (Spain, EU, Europe, EMEA, ...) | `eu` | the built-in Spain/EU/Europe remote and eligibility rules apply (UK-only, US-only etc. are out of scope) |
| non-European regions (LATAM, Brazil, Canada, India...) | `other` | no EU geography filter; eligibility and exclusion come from the profile's own terms |
| empty or missing | `global` | like `other`, with no preferred region |

For `other` and `global`:

- A posting is `eligibility: high` when it matches `location_scope.eligibility_terms` or the
  `preferred` regions (generic words like remote, worldwide, anywhere, global are ignored in
  `preferred`; "Remote LATAM" matches on "latam"). Put every spelling users would see in
  `eligibility_terms` (`latam`, `latin america`, `brazil`, `brasil`, `utc-3`).
- A posting matching `location_scope.excluded_terms` is discarded (`location_excluded`).
- Fully remote roles outside Europe get remote policy `remote_other`, so `remote_policy.required:
  full_remote` still works.
- A posting whose eligibility is unclear is not discarded by the CLI. With scope `other` it always
  goes to you to judge (`references/evaluation.md`); with `global` it only reaches you if the
  heuristics are otherwise unsure or score above the threshold. If you decide the user is not eligible, record a discard with
  `eligibility_scope`; finalize labels it `location_ineligible`.

Example for a non-European profile (backend, remote, Latin America, no pack):

```yaml
slug: backend-remote-latam
name: Senior Backend Engineer - Remote LATAM
criteria:
  role_family_label: Backend engineering
  role_focus_terms: [backend engineer, software engineer, api engineer, golang developer]
  target_titles: [Senior Backend Engineer, Staff Software Engineer]
  seniority: {required: true, preferred_terms: [senior, sr., staff, lead], excluded_terms: [intern, junior]}
  remote_policy: {required: full_remote, positive_terms: [remote, work from home], negative_terms: [hybrid, onsite]}
  location_scope:
    preferred: [Remote LATAM, Brazil, Argentina]
    eligibility_terms: [latam, latin america, brazil, brasil, argentina, americas]
    excluded_terms: [us only, europe only, emea only]
query_templates:
  core_titles: [Senior Backend Engineer]
  regions: [Remote LATAM, Remote Brazil]
```

`run new` combines the core titles with the regions first. It uses `criteria.target_titles` when
`query_templates.core_titles` is empty, then adds combinations from `query_templates.titles`.
The preset caps the plan. If `query_templates.site_scopes` is set, the plan interleaves general
queries with `site:<domain>` queries.

## Creating a profile from a CV

1. Extract the CV text: `ultron cv extract <path>` (PDF, `.txt` or `.md`) writes
   `<ULTRON_HOME>/cv/<stem>.txt` and prints `{"path", "chars", "pages", "source_cv"}` (never the text; `source_cv` is the absolute path of the original CV). Read that
   file with your file-reading ability. On failure it prints `{"error": <code>}`: `no_extractable_text`
   (blank or scanned PDF; ask for a text version), `too_many_pages` (over 20), `too_long` (over 60000
   characters or 2.5 MB), `timeout`, `unsupported_type`, `dependency_missing` (PDF support needs
   `pip install 'ultron[cv]'`), or `worker_failed` (the PDF helper process exited non-zero; `detail`
   has the stderr tail). Ask, in the user's
   language, only what the CV cannot tell you: target roles, seniority, remote/hybrid/onsite,
   countries or regions where they can work, deal-breakers.
2. Derive the fields: `role_focus_terms` and `target_titles` from their recent roles and the roles
   they want (not every past job); `seniority` from years and scope; `specialties` and
   `query_templates.technologies` from their strongest skills; `location_scope` from where they can
   legally work. Keep lists focused: 5-12 role terms, 3-6 target titles.
3. Write the new profile YAML as UTF-8 to a scratch file (outside `<ULTRON_HOME>/profiles/`, e.g. in
   your working scratch folder; use `ultron profile show ai-ml-remote-eu` or the schema above as a
   template).
4. Validate the scratch file: `ultron profile validate --from <yaml>` →
   `{"slug": "...", "valid": true, "errors": []}`. On failure it exits 1 with `"valid": false` and a
   list of `errors`; fix them and validate again.
5. Save it: `ultron profile save --from <yaml> --source-cv <source_cv>` (use the `source_cv` value
   printed by `cv extract`; the file must exist, otherwise `{"error": "source_cv_not_found"}`; only the
   path is stored, never the CV content). It validates first and refuses to overwrite an existing
   profile unless you add `--force`. Output: `{"saved": "<path>", "slug": "..."}`; invalid YAML exits
   1 with `{"error": "profile_invalid", "errors": [...]}`.
6. Show the user a short summary (roles, seniority, remote, regions) and adjust on request.

Refining later: edit `<ULTRON_HOME>/profiles/<slug>.yaml` in place and validate with
`ultron profile validate <slug>`. Runs already made keep their own snapshot.

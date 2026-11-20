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
  ones with the same slug). `ultron profile show <slug>` → `{"profile": {...}, "origin": "user" | "bundled",
  "path"}`.
- Every write by `profile save`, `profile edit`, `profile undo` and `profile install-example` first keeps the
  previous file (byte for byte, comments included) in `<ULTRON_HOME>/profiles/.history/<slug>/` (last 20
  versions). Change profiles through these commands rather than by editing the file, so every change can be
  previewed and undone.

## Schema

Required: `slug`, `name`, `criteria` with a non-empty `target_titles` or `role_focus_terms`. Every
term list must be a list of strings, even with one term (`[manager]`, not `manager`), and
`seniority`, `remote_policy`, `location_scope` and `query_templates` are mappings. Terms match as
whole words or phrases, ignoring case and accents, so use short terms as they would appear in
postings (`ml engineer`, not `ML Engineers`). The CLI ignores keys outside this schema, and
`profile explain` warns about them.

```yaml
slug: data-eng-remote-spain          # ^[a-z0-9][a-z0-9-]*$, same as the file name
name: Senior Data Engineer - Remote Spain/EU
description: one line for humans
enabled: true
source_cv: /home/me/cv/cv.pdf        # optional: path of the CV this profile was generated from (metadata only; null/absent otherwise)
prompt: Salary floor 60k EUR. Avoid gambling companies.   # optional notes for you; the CLI never applies them
criteria:
  sectors: [technology]              # bundled seed packs of these sectors are queried first; see "Sectors"
  role_family_label: Data engineering   # human name of the role family
  role_focus_terms: [data engineer, analytics engineer, etl engineer]   # core role family (STRICT)
  target_titles: [Senior Data Engineer, Staff Data Engineer]           # ideal titles
  seniority:
    required: true                   # false = any level
    preferred_terms: [senior, sr., staff, principal, lead]
    excluded_terms: [intern, junior, graduate, entry]
  remote_policy:
    required: full_remote            # full_remote (default when omitted) | any
    positive_terms: [remote, remote-first, work from home, distributed]
    negative_terms: [hybrid, onsite, on-site, in-office]
  location_scope:
    preferred: [Spain, Remote EU, Remote Europe]     # target hiring scopes
    eligibility_terms: [spain, remote eu, remote europe, within eu, cet]
    excluded_terms: [us only, uk only, north america only]
  exclusion_terms: [manager, director, intern]       # titles with these are skipped when boards and feeds are listed; later they only lower the score
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

### Sectors

`criteria.sectors` picks which bundled seed packs are queried first. Use only these values:
`technology`, `finance_insurance`, `pharma_health`, `consumer_retail`, `energy_utilities`,
`manufacturing_industrial`, `automotive`, `aerospace_defense`, `logistics_transport`,
`construction_engineering`, `telecom_media`, `marketing_advertising`, `public_education`,
`professional_services`, `other`. An unknown value (`fintech`, `healthcare`) matches no pack, so every
pack seed ranks last. Leave the list out when no sector fits. Niches such as fintech go into `prompt`
and into the companies you propose.

### Title exclusions

When job boards and feeds are listed, titles containing a term from `profile explain`'s
`title_exclusions.effective` are skipped. That list holds built-in terms (`manager`, `director`, `vp`,
`head`, `team lead`, `tech lead`; unless `seniority.required` is false, also `intern`, `junior`,
`graduate`, `entry`, `staff`, `principal`, `lead`) plus the profile's own exclusions. Compare it with the titles the
user wants: Head Nurse, Staff Nurse, Lead Teacher or Graduate Analyst would be skipped. A term is lifted
when it shares a word with `role_focus_terms`, `target_titles` or `seniority.preferred_terms`. To keep
such roles, add the title (`Head Nurse`) or the level (`graduate`), then check `explain` again.

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
| contains a European region (EU, Europe, EMEA) or an EU/EEA country (Spain, Ireland, Germany, France, Norway...) | `eu` | the built-in Spain/EU/Europe remote and eligibility rules apply (UK-only, US-only etc. are out of scope) |
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

## Creating a profile

Draft profiles are YAML or JSON files (JSON is valid input everywhere a YAML file is accepted). Write
the draft as UTF-8 to a scratch file outside `<ULTRON_HOME>/profiles/`; use the schema above or
`ultron profile show ai-ml-remote-eu` as a template.

- From preferences, with no CV: interview the user as `references/profile-interview.md` describes.
- From a CV: follow the steps below.

Either way, before saving: `ultron profile validate --from <file>`, then `ultron profile explain --from
<file>` to check what the draft will do, then `ultron profile save --from <file> --dry-run`. Save only
after the user confirms.

### From a CV

1. Extract the CV text: `ultron cv extract <path>` (PDF, `.txt` or `.md`) writes
   `<ULTRON_HOME>/cv/<stem>.txt` and prints `{"path", "chars", "pages", "source_cv"}` (never the text;
   `source_cv` is the absolute path of the original CV). Read that file with your file-reading ability.
   On failure it prints `{"error": <code>}`: `no_extractable_text` (blank or scanned PDF; ask for a text
   version), `too_many_pages` (over 20), `too_long` (over 60000 characters or 2.5 MB), `timeout`,
   `unsupported_type` (for example `.docx`: ask the user to export it as PDF or text),
   `dependency_missing` (PDF support needs `pip install 'ultron[cv]'`), or `worker_failed` (the PDF
   helper process exited non-zero; `detail` has the stderr tail).
2. Check the text before trusting it. Two-column PDFs come out column by column (main column, then
   sidebar). If job titles, dates and skills still look mixed on the same lines, ask the user rather
   than guess.
3. Derive the fields: `role_focus_terms` and `target_titles` from their recent roles and the roles
   they want (not every past job); `seniority` from years and scope; `specialties` and
   `query_templates.technologies` from their strongest skills; `location_scope` from where they can
   legally work. Keep lists focused: 5-12 role terms, 3-6 target titles. Do not turn every skill or past
   task into a term: sidebar keywords, tasks done once (for example data entry) and spoken languages are
   not role terms.
4. Summarize the draft for the user in a few lines (roles, seniority, work mode, regions, main skills)
   and ask only what the CV cannot tell you: roles they want next, roles they do not want, remote,
   hybrid or onsite, where they can legally work, deal-breakers.
5. Validate and explain the draft as above. On failure `profile validate` exits 1 with `"valid": false`
   and a list of `errors`; fix them and validate again. Fix every explain `warning`, and check
   `title_exclusions.effective` against the titles the user wants (see "Title exclusions").
6. Save it: `ultron profile save --from <file> --source-cv <source_cv>` (use the `source_cv` value
   printed by `cv extract`; the file must exist, otherwise `{"error": "source_cv_not_found"}`; only the
   path is stored, never the CV content). It validates first and refuses to overwrite an existing
   profile unless you add `--force`. Output: `{"saved": "<path>", "slug": "..."}`; an invalid file exits
   1 with `{"error": "profile_invalid", "errors": [...]}`.
7. Refine it in conversation as the next section describes.

## Refining a profile in conversation

Use this when the user corrects a saved profile, for example "I don't want management roles", "add
Rust", "I'm based in Valencia and can work for EU companies" or "remove data entry".

1. Load it with `ultron profile show <slug>` and find where each request belongs. One request often
   touches several fields (see the table below).
2. Ask only what you need to apply the request correctly, for example whether onsite or hybrid roles
   in Valencia are fine or only remote ones.
3. Preview: `ultron profile edit <slug> <operations> --dry-run`. Show the user the `changes` and the
   diff in plain words, and mention every `already_present` or `not_found` status.
4. Apply the same command without `--dry-run` once the user confirms. Then run `ultron profile explain
   <slug>` and mention any `warnings`.
5. If the user wants a change back, preview with `ultron profile undo <slug> --dry-run` and apply with
   `ultron profile undo <slug>`. Each undo steps one change back. `ultron profile history <slug>` lists
   versions, and `ultron profile undo <slug> --to <version>` restores one, including a version that an
   undo replaced (redo). A restore with `--to` counts as a change: a plain `undo` afterwards brings back
   the version it replaced.

Operations run in the order given, and nothing is saved if any of them is invalid:

- `--add FIELD=TERM` and `--remove FIELD=TERM` change one term in a term list. Terms compare ignoring
  case and accents. Adding a present term or removing an absent one changes nothing and is reported.
- `--set FIELD=VALUE` sets a text field, `true`/`false`, or `full_remote`/`any`. It replaces the whole
  value: to add a note to `prompt`, set the current text plus the new note. `--unset FIELD` removes an
  optional field.
- `--from <file>` reads operations from a UTF-8 JSON file; they run at the file's place among the other
  operations (`--from reset.json --add ...` applies the file first). Use it to replace a whole list (`set` with a
  JSON list) and for terms with accents or quotes: some shells mangle quotes and non-ASCII text in
  command arguments:

```json
{"operations": [
  {"op": "add", "path": "criteria.exclusion_terms", "value": ["manager", "head of", "director"]},
  {"op": "remove", "path": "criteria.role_signal_terms", "value": "data entry"},
  {"op": "set", "path": "criteria.location_scope.preferred", "value": ["Spain", "Valencia", "Remote EU"]},
  {"op": "set", "path": "criteria.remote_policy.required", "value": "any"}
]}
```

Editable fields are the term lists under `criteria` and `query_templates`, plus `name`, `description`,
`prompt`, `enabled`, `criteria.role_family_label`, `criteria.seniority.required` and
`criteria.remote_policy.required` (`ultron profile edit --help` lists them). `slug` and `source_cv` never
change; to rename a profile, save it under a new slug. `worked_examples` change only through `profile save
--force`. An edit rewrites the file without its comments; the history keeps the original. Editing a
bundled profile saves a user copy, and undo removes that copy again.

`edit` errors: `edit_invalid` (exit 2, with `errors`, one per bad operation), `profile_invalid` (exit
1: the result breaks the schema; the diff is included), `profile_not_found`, `profile_unreadable`.
`undo` errors: `no_history`, `version_not_found`.

| The user says | Operations |
|---|---|
| no management roles | add `manager`, `head of`, `director` to `criteria.exclusion_terms` and `query_templates.exclusion_terms`; if `profile explain` lists one under `title_exclusions.ignored`, it overlaps a role term: tell the user |
| add a skill (Rust) | add it to `criteria.specialties` and `query_templates.technologies`; to `criteria.role_signal_terms` only if the skill defines the role (a Rust developer search) |
| based in a city, can work for EU companies | add the country and `Remote EU` to `criteria.location_scope.preferred` and their spellings to `eligibility_terms`; add matching `query_templates.regions`; set `criteria.remote_policy.required` to `any` only if onsite or hybrid roles there are fine |
| add titles (MLOps roles) | add the titles to `criteria.target_titles` and `query_templates.titles`, and the short family term (`mlops engineer`) to `criteria.role_focus_terms`; role matching is strict, so a title that matches no role term is usually discarded |
| no junior roles, or only one level | add the level words to `criteria.seniority.excluded_terms` (`junior`, `intern`, `graduate`, `entry level`). For one wanted level, set `criteria.seniority.required` to `true` and list its words in `preferred_terms` (`mid`, `intermediate` for mid-level). Any level: set it to `false` |
| remove a term | find every list that holds it in `profile show`, remove it from each, and report any `not_found` |
| salary, spoken languages, companies or company types to target or avoid (consulting firms, crypto) | the CLI cannot filter these: record them in `prompt` and apply them yourself when you propose companies and evaluate postings. A word that shows in unwanted titles can also go into `criteria.exclusion_terms` (`crypto`, or `fluent german` for a required language they lack) |

`ultron profile explain <slug>` (or `--from <file>` for a draft) shows what the profile makes the CLI do,
using the same rules as a run: `role_match` (strict role terms and where they come from), `seniority`,
`remote`, `geography` (scope and its effect), `title_exclusions` (`effective` lists every term that skips
matching titles when job boards are listed: the `built_in` defaults plus the profile's `applied` terms;
`ignored` ones overlap the profile's own role terms), `fit_signals`, the
`query_plan` for `--preset` (default `balanced`), `agent_notes` (the `prompt` field) and `warnings`
(duplicated terms, several terms in one, long phrases, oversized lists, missing query regions, empty query
plan, fields the CLI ignores, unknown sectors). Add `--run <run_id>` to replay the filter of an earlier fetched run with this profile, without
writing anything: `replay` gives the new kept, ambiguous (including `rescued_discards`) and discarded counts and, when that run was
filtered, the `changes` (for example `"kept -> discarded": 3`) with a few `examples`. Only postings that
run fetched can appear.

Runs already made keep their own snapshot of the profile.


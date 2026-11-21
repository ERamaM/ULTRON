# Search Profiles

A search profile tells ULTRON **who you are targeting** (roles, seniority, keywords, languages) and **where you are eligible to work** (remote policy, target countries/regions, residency limits). Saved profiles live as YAML files in `$ULTRON_HOME/profiles/<slug>.yaml`.

## Creating a Profile

You have three ways to start:

1. **From your CV (recommended):**
   Ask your agent: *"Create a search profile from `/path/to/cv.pdf`."*
   Behind the scenes, the CLI extracts text (`ultron cv extract <path>`), your agent drafts the YAML profile strictly from verified facts and asks you to confirm any ambiguous constraints (country, remote preference, languages), then saves via:
   ```sh
   ultron profile save --from <profile.yaml> --source-cv <cv_path>
   ```
   Recording `--source-cv` lets `ultron salary` and `ultron cover-letter` reuse your CV path automatically.
2. **Guided interview (no CV required):**
   Ask your agent: *"Help me create a new search profile from scratch."* It walks through target roles, seniority, work mode, geography, and exclusions before previewing and saving.
3. **Bundled example profile:**
   Install the ready-made `ai-ml-remote-eu` template to test ULTRON right away:
   ```sh
   ultron profile install-example
   ```
   (Pass `--force` to reset it to defaults.)

## Key Profile Sections

- **`criteria.target_titles` & `role_focus_terms`:** Primary titles and technical specialties matched across job titles and descriptions.
- **`criteria.title_exclusions` & `seniority`:** Automatically reject mismatched levels (for example excluding `intern`, `manager`, or `director` when seeking senior IC roles, or rejecting `senior`/`principal` for junior/mid profiles).
- **`criteria.geography` / remote policy:** Supports both Europe (`eu`, country scopes such as `spain`, full remote EU vs hybrid/onsite) and global or country-specific markets outside Europe (`US`, `LATAM`, `APAC`, `global`).
- **`criteria.languages`:** Required or accepted posting languages (multilingual rules support English, Spanish, German, French, Portuguese, Italian, and more).
- **`criteria.sectors` & regions:** Selects which bundled company seed packs ULTRON attaches automatically alongside your own verified companies.
- **`query_templates`:** Pre-built search terms and optional ATS `site:` queries tailored to your role family.

For the full schema definition, see [references/profile.md](../../skill/ultron/references/profile.md).

## Inspecting and Replaying a Profile

Before running a live crawl, verify and inspect what filters and queries a profile produces:

```sh
ultron profile list
ultron profile show <slug>
ultron profile validate <slug>         # or: --from <draft.yaml>
ultron profile explain <slug>        # shows active filters, signals, and query plan
ultron profile explain <slug> --run <run_id>   # replays filter rules against a past run without modifying it
```

## Safe Edits & Undo

You can update a profile either by asking your agent in plain language (*"exclude management roles and add Rust"*) or via the CLI. Every change is schema-validated, diffed before saving with `--dry-run`, and versioned for rollback:

```sh
ultron profile edit <slug> --add criteria.role_focus_terms=Rust --dry-run
ultron profile edit <slug> --add criteria.role_focus_terms=Rust
ultron profile edit <slug> --remove query_templates.technologies=Go
ultron profile edit <slug> --set criteria.remote_only=true
ultron profile edit <slug> --unset criteria.notes
```

(`slug` and `source_cv` are immutable for auditability.)

Every edit saves a snapshot. Inspect versions and roll back anytime:

```sh
ultron profile history <slug>
ultron profile undo <slug> --dry-run
ultron profile undo <slug> [--to <version_id>]
```

## Inspecting Linked Companies

See which curated and auto-discovered ATS boards match a profile:

```sh
ultron seeds list --profile <slug>
```

Next: [Searches & Presets](search-and-presets.md).

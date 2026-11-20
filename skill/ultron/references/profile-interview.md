# Building a search profile by interview

Use this when the user wants a custom search without a CV, or a CV leaves too much open. You
interview; the CLI validates, explains and saves. Ask in the user's language, a few questions per
turn, and skip anything they already said. Offer examples so they can answer quickly.

## 1. Interview

Ask about these topics in this order. Stop as soon as you have enough for a first draft; the user
can refine it later.

| Topic | Ask | Goes to |
|---|---|---|
| Roles | Which jobs do you want? What would the title say? Any role you do not want? | `criteria.role_focus_terms` (short family terms such as `backend engineer`), `criteria.target_titles` (3-6 real titles), `criteria.role_family_label` |
| Seniority | Which level: junior, mid, senior, lead? Is any level a no? | `criteria.seniority`: `required: true` with `preferred_terms` and `excluded_terms`, or `required: false` for any level |
| Work mode | Remote only, or are hybrid or onsite roles fine? | `criteria.remote_policy.required`: `full_remote` or `any` |
| Locations | Where do you live? In which countries or regions can you legally work? Which time zones work? | `criteria.location_scope.preferred`, `eligibility_terms` (every spelling postings use, such as `spain`, `españa`, `cet`), `excluded_terms` (such as `us only`); `query_templates.regions` |
| Must-have and nice-to-have terms | Which skills, tools or domains should a good match mention? Which ones make the role clearly yours? | nice-to-have: `criteria.specialties`, `query_templates.technologies`; must-have for the family: `criteria.role_signal_terms` |
| Exclusions | Which words in a title mean "not for me" (manager, sales, intern)? | `criteria.exclusion_terms`, `query_templates.exclusion_terms` |
| Industry | Which industries or company types (fintech, hospitals, startups)? | `criteria.sectors` only with the fixed values in `references/profile.md` ("Sectors", such as `finance_insurance`); niches such as fintech or "no crypto" also go into `prompt` |
| Companies | Companies you want included, or avoided? | not a CLI filter: record them in `prompt`; propose the wanted ones as seeds during a search and discard postings from avoided ones when you evaluate |
| Salary | A minimum salary or currency? | not a CLI filter (postings rarely state it): record it in `prompt` and mention it when you evaluate |
| Languages | Which languages do you speak at work? | not a CLI filter: record them in `prompt`; a required language they lack can go into `criteria.exclusion_terms` (such as `fluent german`) |

Choose a slug (`^[a-z0-9][a-z0-9-]*$`, for example `backend-remote-eu`) and a short `name`. A slug that
`ultron profile list` already shows would replace that profile, so pick a new one unless the user wants
to replace it.

## 2. Draft and check

1. Write the draft as UTF-8 JSON or YAML to a scratch file outside `<ULTRON_HOME>/profiles/`, following
   the schema in `references/profile.md`. Fill only what the interview gave; leave other fields out.
2. `ultron profile validate --from <file>`: fix every listed error.
3. `ultron profile explain --from <file>`: read `role_match`, `geography.scope`, `remote`,
   `title_exclusions` and the `query_plan`, and fix anything in `warnings`. Check that no title the
   user wants contains a `title_exclusions.effective` term (`references/profile.md`, "Title
   exclusions"). Never invent fields for preferences the CLI cannot filter: they go into `prompt`.
   If the user ran a search before, add `--run <run_id>` to show how many earlier postings the
   draft keeps or discards (`replay`).
4. Summarize the draft for the user in plain words: the roles it matches, the level, the work mode,
   where they must be eligible, what it excludes, the first few search queries, and the notes that you,
   not the CLI, will apply (`prompt`). Ask for corrections and update the draft.

## 3. Save

1. `ultron profile save --from <file> --dry-run` shows whether the slug exists and the diff.
2. On confirmation: `ultron profile save --from <file>`. Add `--force` only if the user agreed to
   replace an existing profile; the old version stays restorable with `ultron profile undo <slug>`.
3. Later corrections use `ultron profile edit` (`references/profile.md`, "Refining a profile in
   conversation").
4. Offer to make it a saved search: `ultron saved-search create <name> --profile <slug> [--preset <p>]
   [--prompt <text>]`. The prompt comes back from `ultron saved-search run <name>`, so it can repeat the
   preferences you apply yourself. Scheduling is in `references/scheduling.md`.

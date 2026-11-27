# Tailored Cover Letter Workflow (`ultron cover-letter`)

Use `--preview` when building and pass `--page-size a4` or `--page-size letter` for bundled
templates so the letter matches the CV. A custom template controls its own page size.
Open every PNG in `preview.images` with vision. Check for one balanced page, clean margins,
legible text, and spillover. Make at most three review passes and repeat strict truth verification
if the text changes. If images are unavailable, report that visual review was skipped, explain
the local tool hint in `warnings`, and still deliver the DOCX.

Check `pages` and `page_count_source` (in `layout` for cover letters) after generating a DOCX. `pdf_render` means the count was verified against a temporary local PDF rendered with LibreOffice or Microsoft Word. Otherwise, ULTRON uses saved Word pagination or marks the count as `estimated`. Flag letters that exceed one page and use the source when describing an estimate.

ULTRON drafts tailored, truthful cover letters from a verified CV and a target job or search profile, verifies every claim against the source CV, and exports clean Word (`.docx`) files.

## 1. Choose a template (or inspect placeholders)

Run `ultron cover-letter templates` to list the bundled ATS-safe single-column Word templates and custom placeholder tags:

- `classic`: Georgia serif letterhead with crisp divider rule and traditional business-letter cadence.
- `modern`: Calibri contemporary layout with a deep-navy accent border on the subject block.
- `executive`: Cambria full-width dark-slate shaded header banner with balanced executive spacing.

To export customizable starter `.docx` templates containing `{{placeholder}}` tags:

```sh
ultron cover-letter templates --export <dir> [--template classic|modern|executive] [--force]
```

Supported custom placeholders: `{{candidate_name}}`, `{{candidate_email}}`, `{{candidate_phone}}`, `{{candidate_location}}`, `{{candidate_links}}`, `{{date}}`, `{{recipient_name}}`, `{{recipient_title}}`, `{{company}}`, `{{role}}`, `{{subject}}`, `{{salutation}}`, `{{opening}}`, `{{paragraphs}}`, `{{highlights}}`, `{{closing}}`, `{{signoff}}`, `{{body}}`. Multiline placeholders (`{{body}}`, `{{paragraphs}}`, `{{highlights}}`) expand cleanly across styled paragraphs when placed on their own line.

## 2. Prepare the evidence brief

Pair the candidate's CV with a job posting (`--job` from a run/ledger or URL, or `--job-file`) or a saved `--profile` (which auto-loads `source_cv` when `--cv` is omitted):

```sh
ultron cover-letter prepare [--cv <cv_path>] [--profile <slug>] [--job <key_or_url> | --job-file <path>] [--run <run_id>] [--lang <code>] [--tone formal|direct|warm] [--as-of YYYY-MM-DD]
```

Language is auto-detected from the job posting (`en`, `es`, `de`, `fr`, `pt`, `it`) and can be overridden with `--lang`. The default tone is `direct`.

## 3. Draft strictly from verified facts

Write the cover letter using ONLY facts present in the prepared brief or explicitly confirmed by the user (`confirmed_facts`):

- Do not invent skills from `keyword_overlap.missing_from_cv`. If a missing requirement matters, ask the candidate first and record confirmed details in `--confirmed`.
- Never alter numbers, percentages, currencies, credentials, or verified non-internship tenure.
- Draft cleanly in the resolved language and tone (typically 200-350 words across 3-4 short paragraphs plus optional concise highlights).

Drafts may be YAML-frontmatter Markdown (`.md`) or JSON (`.json`):

```json
{
  "candidate_name": "Ada Lovelace",
  "candidate_email": "ada@example.com",
  "date": "2026-11-21",
  "company": "Acme Corp",
  "role": "Senior Python Engineer",
  "subject": "Application: Senior Python Engineer",
  "salutation": "Dear Hiring Team,",
  "opening": "...",
  "paragraphs": ["..."],
  "highlights": ["Cut p95 API latency by 35% using Python and FastAPI."],
  "closing": "...",
  "signoff": "Best regards,",
  "language": "en",
  "tone": "direct"
}
```

## 4. Verify truthfulness and export `.docx`

Check every number, year, credential, tenure claim, and technical token against the source CV before delivering:

```sh
ultron cover-letter verify-truth --source-cv <cv_path> --draft <draft_path> [--confirmed <path>] [--company <name>] [--strict] [--as-of YYYY-MM-DD]
```

Then render the Word document (and optional Markdown copy) with `--strict` truth enforcement:

```sh
ultron cover-letter build-docx --from <draft_path> --out <out.docx> [--out-md <out.md>] [--template classic|modern|executive] [--template-file <custom.docx>] [--font <name>] --source-cv <cv_path> [--confirmed <path>] --strict [--force] [--as-of YYYY-MM-DD]
```

If verification fails with `--strict`, stop cleanly, show the exact violations, and ask the user whether to revise or add explicit `--confirmed` facts.

# Career Tools: Salary, CV Optimizer & Cover Letters

Beyond job discovery, ULTRON includes three hybrid career workflows: the CLI runs deterministic parsing, tenure computation, truthfulness audits, and Word (`.docx`) rendering; your AI agent handles market research, phrasing improvements, and draft authoring strictly within verified facts.

---

## 1. Expected Salary Estimation (`ultron salary`)

Calculates realistic compensation ranges anchored in verified non-internship tenure and real market disclosures.

- **Ask your agent:** *"What expected salary range makes sense for my CV and remote backend roles in Europe?"*
- **CLI workflow:**
  1. Analyze CV tenure (auto-loaded from `profile.source_cv` when `--profile` is set) and local run disclosures (keeping US pay-transparency ranges separate from European and other regional pay bands):
     ```sh
     ultron salary analyze [--cv <cv_path>] [--profile <slug>] [--run <run_id>] [--no-runs] [--as-of YYYY-MM-DD]
     ```
  2. Your agent runs live web market research using `research_queries` from the JSON output and presents Low / Median / High figures with period, currency, gross/net basis, confidence, and sources.
  3. Optionally save the verified `expected_salary` benchmark onto your profile (previewed first, versioned with `profile undo`):
     ```sh
     ultron salary save --profile <slug> --from <estimate.json> --dry-run
     ultron salary save --profile <slug> --from <estimate.json> [--report] [--out <path>]
     ```
     (`expected_salary` is informational benchmark data only; it never filters out job results.)

See [references/salary.md](../../skill/ultron/references/salary.md) for the complete JSON schema.

---

## 2. ATS CV Audit & Word Optimizer (`ultron cv`)

Extracts text from PDF, DOCX, LaTeX source (`.tex`), Markdown, and plain text for profile drafting. It reads LaTeX as source text and uses best-effort extraction without compiling or expanding included files.

The optimizer evaluates `.docx`, PDF, and LaTeX `.tex` CVs for parser compatibility and keyword alignment, enforces strict factual integrity on rewrites, and outputs a clean single-column `.docx`. Multi-column LaTeX/PDF layouts frequently lose text order or scramble sections in legacy ATS parsers, whereas single-column OOXML preserves reading order cleanly.

- **Ask your agent:** *"Audit my CV against this backend role and rebuild it into a clean ATS-friendly Word document."*
- **CLI workflow:**
  1. **Preview the source PDF or DOCX** and have the agent inspect the page images and `detected_layout`. Use `--out-dir`, `--dpi`, or `--max-pages` when needed.
     ```sh
     ultron cv preview <cv_path>
     ```
  2. **Audit** structure, format signals, verified work timeline, and target alignment (`--job`, `--job-file`, `--profile`, or `--run`):
     ```sh
     ultron cv audit <cv_path> [--profile <slug>] [--job <key_or_url> | --job-file <path>] [--run <run_id>] [--out <audit.md>]
     ```
  3. Agent rewrites sections truthfully and asks you about any missing keywords before citing them.
  4. **Verify truthfulness** deterministically against the original CV (+ any explicit `--confirmed` facts file) to catch invented metrics, currency changes, inflated tenure, unverified degrees/certifications, or missing tech tokens:
     ```sh
     ultron cv verify-truth --source <original_cv> --optimized <draft.md_or_json> [--confirmed <facts_file>] [--strict]
     ```
  5. **Build single-column `.docx`** (refuses to overwrite unless `--force` is given, and refuses to write files if `--strict` verification fails):
     ```sh
     ultron cv build-docx --from <draft.md_or_json> --out <out.docx> --source-cv <original_cv> --strict --preview
     ```

For a PDF or DOCX source, add `--match-source-layout` to preserve compatible layout cues.
Use `--layout <file.json>` for overrides or pass explicit options such as `--page-size a4`,
`--header-align center`, and `--date-layout right_tab`. The generated CV stays single-column.

### Check pagination and layout

When a local LibreOffice or Microsoft Word renderer is available, ULTRON counts pages in a reference
PDF and records `page_count_source: "pdf_render"`. Otherwise, the source identifies saved Word
breaks, Word properties, or an explicit estimate. An estimated count does not reduce the ATS score by itself.

Use the PNG paths returned in `preview.images` to review every generated page. Check the page count,
short overflow pages, headings stranded at page breaks, source layout cues, clipping, wrapping,
raw Markdown, and whitespace. Adjust layout first, rebuild with `--preview`, and recheck.
If you change the draft's text, repeat strict truth verification before building. Limit the loop to three passes.

Renderers and PNG rasterizers run locally. `ultron doctor` reports both capabilities. If images are
unavailable, the CLI still returns the DOCX and an explanation in `warnings`.

See [references/cv-ats.md](../../skill/ultron/references/cv-ats.md).
See the design records for [DOCX page counting](../specs/career/37-docx-pdf-page-count.md)
and the [visual review loop](../specs/career/38-cv-visual-layout-review-loop.md).

---

## 3. Automatic Cover Letter Generator (`ultron cover-letter`)

Pairs verified CV evidence with a specific job posting (`--job` or `--job-file`) or a search `--profile`, drafts in the target posting's language and requested tone, validates every claim against the source CV, and exports polished Word `.docx` (and optional `.md`) files.

- **Ask your agent:** *"Generate a cover letter for job `greenhouse:acme:42` using the modern template in a direct tone."*
- **Bundled Templates (all single-column, zero tables, zero textboxes):**
  - `classic` — Georgia serif letterhead with crisp divider rule and traditional business-letter spacing.
  - `modern` — Calibri contemporary layout with a deep-navy accent bar on the subject line (default).
  - `executive` — Cambria full-width dark-slate shaded banner header with balanced executive spacing.
- **Languages & Tones:**
  - Automatically detects posting language (`en`, `es`, `de`, `fr`, `pt`, `it`), overridable via `--lang <code>`.
  - Supports `formal`, `direct` (default), and `warm` writing registers.

### Step-by-Step Workflow

1. **Inspect templates or export customizable starter templates:**
   ```sh
   ultron cover-letter templates
   ultron cover-letter templates --export <dir> [--template classic|modern|executive] [--force]
   ```
   Custom `.docx` templates support 18 `{{placeholder}}` tags (`{{candidate_name}}`, `{{candidate_email}}`, `{{candidate_phone}}`, `{{candidate_location}}`, `{{candidate_links}}`, `{{date}}`, `{{recipient_name}}`, `{{recipient_title}}`, `{{company}}`, `{{role}}`, `{{subject}}`, `{{salutation}}`, `{{opening}}`, `{{paragraphs}}`, `{{highlights}}`, `{{closing}}`, `{{signoff}}`, `{{body}}`). Standalone `{{body}}`, `{{paragraphs}}`, and `{{highlights}}` expand across multiple styled paragraphs.
2. **Prepare the evidence brief:**
   ```sh
   ultron cover-letter prepare [--cv <cv_path>] [--profile <slug>] [--job <key_or_url> | --job-file <path>] [--run <run_id>] [--lang <code>] [--tone formal|direct|warm]
   ```
3. **Verify truthfulness** (permits current letter year `as_of.year`, verified role span years, and `--company` vocabulary without ever exempting unverified metrics or credentials):
   ```sh
   ultron cover-letter verify-truth --source-cv <cv_path> --draft <draft.md_or_json> [--confirmed <facts_file>] [--company <name>] [--strict]
   ```
4. **Render `.docx` (and optional `.md`) with `--strict` enforcement:**
   ```sh
   ultron cover-letter build-docx --from <draft.md_or_json> --out <out.docx> [--out-md <out.md>] [--template classic|modern|executive | --template-file <custom.docx>] [--font <name>] --source-cv <cv_path> [--confirmed <facts_file>] --strict [--force] [--preview] [--page-size letter|a4]
   ```

Choose `--page-size a4` for a bundled letter paired with an A4 CV. Custom templates determine
their own paper size. Build with `--preview` to check the letter against its local reference PDF,
then inspect each PNG for a balanced page, clean margins, and readable wrapping. A letter spanning
more than one page receives a warning.

See [references/cover-letter.md](../../skill/ultron/references/cover-letter.md).

Next: [Troubleshooting & Data Maintenance](troubleshooting-and-data.md).

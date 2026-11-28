# 38 · Visual PDF review loop and ATS-safe layout preservation for CVs and cover letters

Implemented in [PR #132](https://github.com/ERamaM/ULTRON/pull/132) and shipped in v1.1.0. Builds on [spec 37](37-docx-pdf-page-count.md).

The text below records the original proposal. See the linked PR for implementation and review changes.

## Problem & current behavior

Even with exact PDF-based page counting (Spec 37), generating a `.docx` CV or cover letter remains visually blind and ignores the candidate's existing visual style:

- `cv/docx_writer.py` ([source](../../../skill/ultron/scripts/ultron/cv/docx_writer.py)) hardcodes a single layout: US Letter (`12240 × 15840` twips), 0.75 in margins, left-aligned header, dark-navy bottom-bordered `Heading2`, `10.5pt` body text, and dates stacked below the role title. A European candidate's A4 CV with centered header, right-aligned dates, compact spacing, or monochrome/slate styling is overwritten with an unrelated look.
- Neither `ultron cv build-docx` nor `ultron cover-letter build-docx` produces rendered page images. The agent cannot visually inspect the original CV layout or verify how the generated `.docx` actually looks on the page (e.g., awkward 2–4 line spillover onto an extra page, orphan headings, unbalanced whitespace, date/title wrapping, or visual drift from the original document).
- `cover_letter/templates.py` ([source](../../../skill/ultron/scripts/ultron/cover_letter/templates.py)) also hardcodes US Letter page dimensions, causing mismatches when paired with A4 CVs.

## Goal

Enable a closed-loop **Source Inspect → Layout-Respecting Build → PDF Render → Vision QA → Iterate** workflow so the agent can verify with its own vision capability that every generated CV and cover letter looks polished, fits cleanly on its target page count, and respects the original CV's visual character — while strictly preserving ATS-clean single-column OOXML (zero tables, zero text boxes, zero images, body-only contact details).

## Expected behavior

### A. Local PDF → PNG rasterizer (`ultron/infra/pdf_raster.py`)

Create a stdlib-only infrastructure module `skill/ultron/scripts/ultron/infra/pdf_raster.py` (imports nothing else in `ultron`) that renders PDF pages into bounded local PNG images for agent visual inspection:

- `detect_pdf_rasterizer(env: Mapping[str, str] | None = None) -> tuple[str, str] | None`
  - Honors `ULTRON_PDF_RASTERIZER` (`"auto"` default, `"pdftoppm"`, `"mutool"`, `"gs"`, `"pypdfium2"`, `"none"`). Invalid values disable rasterization safely.
  - Discovery priority:
    1. **`pdftoppm`** (Poppler) on `PATH` (cross-platform: Linux, macOS, Windows).
    2. **`mutool`** (MuPDF) on `PATH`.
    3. **`gs` / `gswin64c` / `gswin32c`** (Ghostscript) on `PATH`.
    4. **Optional `pypdfium2`** (`importlib.util.find_spec("pypdfium2") is not None`) — no mandatory dependency added to `pyproject.toml`.
- `render_pdf_to_pngs(pdf_bytes: bytes, out_dir: Path, *, dpi: int = 144, max_pages: int = 20, timeout: float = 30.0, env: Mapping[str, str] | None = None) -> PdfRasterResult`
  - Returns a frozen dataclass `PdfRasterResult(images: list[Path], rasterizer: str | None, dpi: int, warning: str = "")`.
  - Clamps `dpi` to `72..200` and `max_pages` to `1..20`.
  - Writes `pdf_bytes` to a fixed sanitized filename (`input.pdf`) inside a fresh temporary working directory (never passing untrusted filenames to subprocesses), invokes the rasterizer with `shell=False` and strict `timeout`, then moves/writes verified PNG files as `page-1.png`, `page-2.png`, ... into `out_dir` (cleaning up stale `page-*.png` from previous preview runs in the same target folder only after confirming `out_dir` is a safe directory, never following symlinks).
  - For Ghostscript, always pass `-dSAFER -dBATCH -dNOPAUSE -sDEVICE=png16m`.
  - For optional `pypdfium2`, run rasterization in an isolated child interpreter (`sys.executable -s -P`, matching `cv/extract.py`) with timeout so untrusted PDFs cannot crash or hang the main process.
  - If no rasterizer is available or rendering fails/times out, never raise: return `images=[]`, `rasterizer=None`, and an actionable `warning` (e.g., suggesting Poppler `pdftoppm` or `pip install pypdfium2`).

### B. ATS-safe `CvLayoutStyle` & source layout extraction (`ultron/cv/layout_profile.py`)

Create `skill/ultron/scripts/ultron/cv/layout_profile.py` defining a validated, JSON-serializable `CvLayoutStyle` dataclass capturing ATS-safe visual properties:

| Field | Allowed values / bounds | Default | Notes |
|---|---|---|---|
| `page_size` | `"letter" \| "a4"` | `"letter"` | Letter `12240×15840` twips; A4 `11906×16838` twips |
| `margins` | `"compact" \| "normal" \| "wide"` | `"normal"` | `0.55"` (`792` twips) / `0.75"` (`1080` twips) / `0.95"` (`1368` twips) |
| `font` | `ooxml.WRITER_SAFE_FONTS` | `"Calibri"` | Validated via `resolve_safe_font` |
| `body_size_pt` | `9.0..12.0` | `10.5` | Half-points in OOXML (`w:sz`) |
| `heading_size_pt` | `10.5..14.0` | `11.5` | Section headings (`Heading2`) |
| `name_size_pt` | `14.0..22.0` | `17.0` | Candidate name (`Heading1`) |
| `density` | `"compact" \| "normal" \| "spacious"` | `"normal"` | Scales paragraph/bullet before/after spacing & line height (`1.03` / `1.05` / `1.12`) |
| `accent_color` | 6-char hex RGB (`^[0-9A-Fa-f]{6}$`) | `"1F3A5F"` | Rejects near-white low-contrast colors (luminance guard) |
| `header_align` | `"left" \| "center"` | `"left"` | Controls `<w:jc>` on name, headline, and contact lines |
| `section_style` | `"rule" \| "caps" \| "clean" \| "shaded"` | `"rule"` | `"rule"`: bottom border; `"caps"`: uppercase bold; `"clean"`: plain bold; `"shaded"`: light paragraph shading (0 tables, 0 textboxes) |
| `date_layout` | `"below" \| "right_tab"` | `"below"` | `"below"`: `####` meta line under role; `"right_tab"`: role + right-aligned tab stop on the same line (single paragraph, 100% ATS-safe, zero tables) |

Implement automatic best-effort extraction `extract_layout_profile(path_or_bytes, *, file_type: str, format_meta: dict[str, Any] | None = None) -> dict[str, Any]`:

- **From `.docx`** (via bounded expat metadata added to `cv/docx_reader.py`):
  - Page size from `<w:pgSz w:w="..." w:h="..."/>` (detects A4 vs Letter by nearest standard dimensions).
  - Margin bucket from `<w:pgMar>` average side margin.
  - Dominant font mapped to nearest `WRITER_SAFE_FONTS` equivalent (e.g., serif fonts → `Georgia`/`Times New Roman`, sans-serif → `Calibri`/`Arial`).
  - Header alignment from the first paragraph's `<w:jc w:val="center"/>`.
  - Accent color from the first non-black, high-contrast heading/border `<w:color>` / `<w:pBdr>`.
  - Section style hint (border present → `"rule"`, all-caps section headings → `"caps"`, paragraph shading → `"shaded"`, else `"clean"`).
- **From `.pdf`** (via `cv/worker.py` inspection metadata):
  - Extend `worker.py` `--inspect` metadata with `page_width_pt` and `page_height_pt` from page 1's MediaBox so A4 (`~595×842 pt`) vs Letter (`~612×792 pt`) is detected automatically.
  - Map dominant detected PDF font family to the closest ATS-safe font (falling back to `"Calibri"` for LaTeX Computer/Latin Modern or unknown fonts).
- Include `load_layout_profile(path: Path) -> CvLayoutStyle` and `merge_layout_overrides(base: CvLayoutStyle, **overrides) -> CvLayoutStyle` with strict validation and clear `ValueError` messages on invalid values.

### C. Layout-aware `cv/docx_writer.py` & `cover_letter/templates.py`

1. **`cv/docx_writer.py`:**
   - Update `_styles_xml`, `_render_document_xml`, `render_docx_bytes`, and `build_ats_docx` to accept `layout: CvLayoutStyle | None = None` (defaulting to the exact current defaults so existing outputs and tests remain backwards-compatible when no layout options are passed).
   - Support `"right_tab"` date layout: when `layout.date_layout == "right_tab"` and a `"subheading"` block is immediately followed by a `"meta"` block, render them on one paragraph with a right-aligned tab stop at `content_width_twips` (`page_width - left_margin - right_margin`) — zero tables, zero columns, cleanly readable by both humans and ATS parsers.
   - Support `--match-source-layout` (extracts initial `CvLayoutStyle` from `--source-cv` before applying explicit overrides from `--layout <file.json>` or CLI flags).
   - Keep every generated `.docx` strictly single-column with 0 tables, 0 text boxes, 0 images, and body-only contact info (verified by `audit_generated_docx`).
2. **`cover_letter/templates.py` & `cover_letter/builder.py`:**
   - Accept `page_size: Literal["letter", "a4"] = "letter"` on bundled cover-letter rendering so cover letters match A4 or Letter CVs cleanly.
   - Support optional `preview: bool = False` in `build_cover_letter_docx` to render reference PDF + PNG page images into `<ULTRON_HOME>/cover-letters/previews/<stem>/` and return a `preview` object in the JSON output.

### D. New `ultron cv preview` command and `--preview` build flag

1. **New subcommand `ultron cv preview <path>`** (`commands/cv.py`):
   - Accepts `.docx` or `.pdf` input (`--out-dir <dir>`, `--dpi <int>`, `--max-pages <int>`).
   - For `.docx`: converts to a local reference PDF via Spec 37's `infra.docx_pdf.render_docx_to_pdf_bytes(...)`, computes exact `pages` / `page_count_source` via `cv/docx_pages.py`, saves the preview PDF to `<preview_dir>/<stem>.pdf`, and rasterizes pages via `infra.pdf_raster.render_pdf_to_pngs(...)`.
   - For `.pdf`: validates bounds via the sandboxed PDF worker (`pages`, `page_width_pt`, `page_height_pt`), copies/references the PDF without mutating the source, and rasterizes pages to PNGs.
   - Extracts `detected_layout` (`CvLayoutStyle` dict) so the agent gets both the **visual page images** and the **structured layout baseline** in a single command!
   - Default preview folder: `<ULTRON_HOME>/cv/previews/<sanitized_stem>/`.
   - Emits a single JSON object:
     ```json
     {
       "source_file": "...",
       "file_type": "docx",
       "pages": 2,
       "page_count_source": "pdf_render",
       "page_count_converter": "libreoffice",
       "pdf_path": ".../cv-ats.pdf",
       "images": [".../page-1.png", ".../page-2.png"],
       "rasterizer": "pdftoppm",
       "detected_layout": { "...": "..." },
       "warnings": []
     }
     ```
2. **Extend `ultron cv build-docx` (`commands/cv.py`):**
   - Add layout flags:
     - `--layout <path.json>`
     - `--match-source-layout` (requires `--source-cv`)
     - `--page-size letter|a4`
     - `--margins compact|normal|wide`
     - `--density compact|normal|spacious`
     - `--header-align left|center`
     - `--section-style rule|caps|clean|shaded`
     - `--accent-color <RRGGBB>`
     - `--date-layout below|right_tab`
   - Add `--preview` (and optional `--preview-dir <dir>`, `--dpi <int>`): after building and auditing the `.docx`, reuses the reference PDF bytes (avoiding a double LibreOffice/Word conversion!) to write the preview PDF + PNG images and include `"layout_used"` and `"preview"` in the returned JSON.
3. **Extend `ultron cover-letter build-docx` (`commands/cover_letter.py`):**
   - Add `--page-size letter|a4` and `--preview` (reusing the reference PDF from Spec 37's page measurement to emit `preview: {pdf_path, images, rasterizer, warnings}`).
4. **Extend `ultron doctor` (`diagnostics/doctor.py`):**
   - Update `check_docx_pdf_renderer()` (from Spec 37) or add informational `cv_visual_preview` check (always `ok=True`, added to `PUBLIC_CHECK_IDS`) reporting whether a PDF-to-PNG rasterizer (`pdftoppm`, `mutool`, `gs`, or `pypdfium2`) is available, with a concise install hint in `fix` if missing.

### E. Agent vision QA workflow (`skill/ultron/references/`)

Update `skill/ultron/references/cv-ats.md`, `skill/ultron/references/cover-letter.md`, `skill/ultron/references/commands.md`, and `skill/ultron/SKILL.md`:

- **Before drafting a CV (source layout inspection):**
  - When optimizing an existing `.pdf` or `.docx`, run `ultron cv preview <source>` and inspect the rendered PNG page images with vision (when `images` is non-empty) alongside `detected_layout`.
  - Note the candidate's ATS-safe visual cues (A4 vs Letter, target page count, header alignment, section style, accent tone, date alignment, visual density) — while **never** imitating ATS-hostile elements (multi-column grids, layout tables, text boxes, photos, skill bars, header/footer contact details).
- **After building (`--preview`):**
  - Inspect **every** PNG image in `preview.images` with vision against an explicit visual QA checklist:
    1. **Pagination:** matches target page count with no awkward trailing page containing only a few orphan lines.
    2. **Page breaks:** no orphaned section or role headings stranded at the bottom of a page.
    3. **Layout fidelity:** respects the original CV's page size, header alignment, section styling, date placement, and visual density within ATS-safe single-column rules.
    4. **Visual cleanliness:** no clipped text, awkward wraps, raw Markdown markers (`**`, `[text](url)`), or cramped/excessive whitespace.
  - **Iterate (max 3 passes):** if any visual issue appears, adjust layout parameters (`--density`, `--margins`, `--date-layout`, `--header-align`, `--section-style`, `--layout`) or tighten verbose bullets (always re-running `ultron cv verify-truth --strict` after any text change), rebuild with `--preview`, and re-inspect the new PNGs until visually verified.
  - **Graceful degradation:** if `preview.images` is empty (no local PDF renderer or rasterizer available), inform the user that visual PNG review was skipped and explain how to enable it, while still delivering the verified `.docx`.
- **For cover letters (`references/cover-letter.md`):**
  - Build with `--preview` (and `--page-size` matching the CV) and visually inspect the rendered PNG to confirm a balanced single-page letter with clean margins and zero spillover onto page 2.
- Keep all skill wording strictly platform-agnostic (never naming platform-specific image tools, keeping `tests/test_skill_files.py` passing).

## Out of scope

- In-place binary XML surgery on arbitrary multi-column or heavily templated user `.docx` files (this spec preserves ATS-safe visual layout via `CvLayoutStyle` reconstruction; arbitrary template patching would risk preserving ATS-hostile tables/text boxes).
- Sending CV images or PDFs to any external web/cloud rendering service (all rendering and rasterization happen strictly locally; the user's own active AI agent inspects the local PNGs).
- Adding mandatory heavy binary dependencies to `pyproject.toml`.

## Security & cross-platform considerations

- **Local-only processing:** PDF conversion and PNG rasterization run strictly on local temp files; no network requests are made.
- **No path or command injection:** `shell=False` everywhere; untrusted input bytes are copied to fixed temp names (`input.docx` / `input.pdf`); output filenames are strictly `page-<n>.png`; symlinks in output directories are rejected.
- **Bounded resources:** `dpi` clamped (`72..200`), `max_pages` clamped (`1..20`), subprocess timeouts enforced (`<= 30s`), `gs` uses `-dSAFER`, `pypdfium2` runs in a bounded child process (`-s -P`), and preview folders never accumulate unrelated files or traverse outside their target directory.
- **No double rendering:** `build-docx --preview` reuses the reference PDF bytes already produced during Spec 37's page count step so LibreOffice/Word is invoked at most once per build.
- **xdist-safe tests:** All unit tests mock local converter/rasterizer binaries (or set `ULTRON_DOCX_PDF_CONVERTER=none` and `ULTRON_PDF_RASTERIZER=none`) and write only under `tmp_path`.

## Files to touch

| File | Change |
|---|---|
| `skill/ultron/scripts/ultron/infra/pdf_raster.py` | New stdlib-only local PDF → PNG rasterizer (`pdftoppm`, `mutool`, `gs`, optional sandboxed `pypdfium2`) |
| `skill/ultron/scripts/ultron/cv/worker.py` | Expose `page_width_pt` and `page_height_pt` in `--inspect` metadata |
| `skill/ultron/scripts/ultron/cv/docx_reader.py` | Extract bounded layout cues (`page_width_twips`, `page_height_twips`, `margin_twips`, `header_align`, `accent_color`, `has_section_borders`, `has_shaded_headings`) |
| `skill/ultron/scripts/ultron/cv/layout_profile.py` | New `CvLayoutStyle` model, validation, JSON loader, override merger, and `.docx`/`.pdf` layout extraction |
| `skill/ultron/scripts/ultron/cv/docx_pages.py` | Optionally return `pdf_bytes` alongside `DocxPageCount` so `--preview` avoids rendering twice |
| `skill/ultron/scripts/ultron/cv/preview.py` | New preview orchestrator combining Spec 37 PDF render, `pdf_raster`, and `layout_profile` for `.docx` and `.pdf` |
| `skill/ultron/scripts/ultron/cv/docx_writer.py` | Render according to `CvLayoutStyle` (A4/Letter, margins, density, accent color, header align, section styles, `right_tab` dates) and support `--preview` |
| `skill/ultron/scripts/ultron/cover_letter/templates.py`, `cover_letter/builder.py` | Support `page_size` (`letter` / `a4`) and optional `--preview` PNG generation |
| `skill/ultron/scripts/ultron/commands/cv.py`, `commands/cover_letter.py` | Register `ultron cv preview`, layout flags on `cv build-docx`, `--page-size` on `cover-letter build-docx`, and `--preview` on both |
| `skill/ultron/scripts/ultron/diagnostics/doctor.py` | Report PDF rasterizer availability (`cv_visual_preview` or enriched `docx_pdf_renderer`) in `doctor` and `PUBLIC_CHECK_IDS` |
| `docs/architecture.md` | Document `infra/pdf_raster.py`, `cv/layout_profile.py`, and `cv/preview.py` |
| `skill/ultron/SKILL.md`, `skill/ultron/references/cv-ats.md`, `skill/ultron/references/cover-letter.md`, `skill/ultron/references/commands.md` | Document `ultron cv preview`, layout flags, and the visual render → vision inspect → iterate QA loop |
| `tests/test_pdf_raster.py`, `tests/test_layout_profile.py`, `tests/test_cv_preview.py` (plus updates to `tests/test_cv_docx_writer.py`, `tests/test_cover_letter.py`, `tests/test_doctor.py`, `tests/test_skill_files.py`) | Standalone xdist-safe unit tests (`>= 80%` coverage on new/changed lines) |

## Acceptance cases

1. **Source preview & layout detection (`.pdf` and `.docx`):** `ultron cv preview` on an A4 `.docx` (centered header, `#2B4C7E` accent, Arial font) returns `pages`, `page_count_source`, `pdf_path`, sorted `images` (`page-1.png`, ...), and `detected_layout` with `page_size == "a4"`, `header_align == "center"`, `font == "Arial"`, and `accent_color == "2B4C7E"`. On an A4 PDF, `detected_layout["page_size"] == "a4"`.
2. **Layout-preserving ATS-safe build:** `ultron cv build-docx --match-source-layout` (or `--layout <json>` with `page_size="a4"`, `header_align="center"`, `section_style="caps"`, `date_layout="right_tab"`, `density="compact"`) produces a valid `.docx` whose OOXML has A4 `<w:pgSz>`, centered header `<w:jc w:val="center"/>`, right-aligned tab stop on role+date lines, and passes `audit_generated_docx` with `format_tier == "preferred"`, 0 tables, 0 text boxes, and 0 images.
3. **Single-pass `--preview` (no double render):** `ultron cv build-docx --preview` and `ultron cover-letter build-docx --preview` invoke `render_docx_to_pdf_bytes` at most **once**, save the preview PDF and PNGs, and include `preview.images` and `layout_used` in JSON output.
4. **Cover letter A4 parity:** `ultron cover-letter build-docx --page-size a4` writes A4 page dimensions (`11906 × 16838` twips) into `word/document.xml` for all bundled templates (`classic`, `modern`, `executive`).
5. **Graceful fallback without rasterizer:** When `ULTRON_PDF_RASTERIZER=none` (or no rasterizer is installed), `ultron cv preview` and `--preview` succeed with exit code `0`, `images: []`, `rasterizer: null`, and a clear non-empty `warnings` entry (still returning `pages` and `pdf_path` if PDF rendering succeeded).
6. **Validation & security:**
   - Invalid `accent_color` (non-hex or unreadable white `#FFFFFF`), invalid `page_size`, out-of-range `dpi`, or unknown `section_style` fail cleanly with structured CLI errors.
   - `pdf_raster` rejects symlinked output targets, clamps `dpi`/`max_pages`, passes `-dSAFER` for Ghostscript, and cleans up temp directories on timeout or non-zero exit.
   - `tests/test_skill_files.py` passes (every new CLI subcommand documented; zero platform-specific tool names in skill prompts) and `tests/test_architecture.py` passes.

## Verification

Run inside a dedicated worktree from latest `origin/main` (after Spec 37 is merged, or stacked on Spec 37's branch):

```sh
uv run ruff check
uv run pytest tests/test_architecture.py tests/test_skill_files.py
uv run pytest
uv build --wheel --out-dir dist skill/ultron/scripts
```

Check new/changed line coverage (`>= 80%`) and ensure CI, SonarCloud, CodeQL, and both Codex code and security reviews pass with zero unresolved findings.

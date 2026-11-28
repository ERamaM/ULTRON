# 37 · Reference PDF page counting for Word (`.docx`) CVs and cover letters

Implemented in [PR #131](https://github.com/ERamaM/ULTRON/pull/131) and shipped in v1.1.0.

The text below records the original proposal. See the linked PR for implementation and review changes.

## Problem & current behavior

Word (`.docx`) documents are continuous flow formats: OOXML does not require layout pagination markers, and documents generated programmatically contain no rendered page boundaries.

- `cv/docx_reader.py` ([source](../../../skill/ultron/scripts/ultron/cv/docx_reader.py)) initializes `self.pages = 1` and only increments it on explicit manual page breaks (`<w:br w:type="page"/>`). It ignores Word's cached pagination hints (`<w:lastRenderedPageBreak/>` and `docProps/app.xml` `<Pages>`).
- `cv/docx_writer.py` ([source](../../../skill/ultron/scripts/ultron/cv/docx_writer.py)) generates continuous single-column `.docx` files with zero manual page breaks, then calls `cv_ats.audit_generated_docx(docx_bytes)` before writing the file. As a result, **every generated CV reports `"pages": 1`** regardless of true length (2, 3, or 4+ pages).
- `cv/ats.py` (`_audit_content`) checks `(pages > 2 and total_years < 8.0) or pages > 3` to emit `page_length_excessive`, so length checks silently fail on continuous `.docx` CVs.
- `cover_letter/builder.py` ([source](../../../skill/ultron/scripts/ultron/cover_letter/builder.py)) inspects generated cover letters for single-column constraints via `cv_docx.inspect_docx(docx_bytes)` but cannot verify whether a cover letter fits on 1 page.

## Goal

Measure the true rendered page count of `.docx` CVs and cover letters by **converting to a temporary local PDF as the authoritative layout reference** whenever a local renderer (LibreOffice or Microsoft Word) is available, counting pages through ULTRON's sandboxed PDF worker, and falling back transparently (never silently reporting `1` as verified) when no renderer is installed.

## Expected behavior

### A. Cross-platform local DOCX → PDF renderer (`ultron/infra/docx_pdf.py`)

Create a new infrastructure module `skill/ultron/scripts/ultron/infra/docx_pdf.py` (no imports from other `ultron` packages; stdlib only) that exposes:

- `detect_pdf_converter(env: Mapping[str, str] | None = None) -> tuple[str, str] | None`
  - Returns `(backend, executable_or_label)` where `backend` is `"libreoffice"` or `"word"`, or `None` if unavailable/disabled.
  - Honors `ULTRON_DOCX_PDF_CONVERTER` env var (`"auto"` default, `"libreoffice"`, `"word"`, `"none"`). Invalid values treat conversion as disabled rather than crashing.
  - **LibreOffice discovery (preferred cross-platform):**
    - `shutil.which("soffice")` or `shutil.which("libreoffice")`.
    - Standard OS fallback paths when not on `PATH`:
      - Windows: `%ProgramFiles%\LibreOffice\program\soffice.exe`, `%ProgramFiles(x86)%\LibreOffice\program\soffice.exe`.
      - macOS: `/Applications/LibreOffice.app/Contents/MacOS/soffice`.
  - **Microsoft Word discovery (secondary fallback when LibreOffice is absent):**
    - Windows: detect installed Word COM automation (e.g. registry check for `Word.Application` ProgID via `winreg`, read-only, without launching Word).
    - macOS: `/Applications/Microsoft Word.app` exists and `osascript` is available.
    - Linux: not applicable.
- `render_docx_to_pdf_bytes(docx_bytes: bytes, *, timeout: float = 25.0, env: Mapping[str, str] | None = None) -> tuple[bytes, str] | None`
  - Returns `(pdf_bytes, backend)` on success, or `None` if no converter is available or conversion fails/times out. Never raises for missing tools or transient renderer failures so callers can fall back cleanly.
  - **Temp isolation & safe filenames:** Always write `docx_bytes` to a fixed, sanitized filename (`input.docx`) inside a fresh `tempfile.TemporaryDirectory()` (never pass untrusted user filenames into subprocess arguments or scripts) and delete the temp folder unconditionally.
  - **LibreOffice invocation:**
    - Use `shell=False` and pass a dedicated per-call user profile (`-env:UserInstallation=<temp_profile_file_uri>`) with `--headless --norestore --nodefault --nolockcheck --nofirststartwizard --convert-to pdf --outdir <temp_dir> <temp_dir>/input.docx`.
    - The isolated `UserInstallation` is mandatory so parallel invocations (`pytest-xdist` or concurrent runs) do not deadlock on LibreOffice's shared profile lock.
  - **Microsoft Word invocation (strictly sandboxed against macros):**
    - Windows: invoke PowerShell (`-NoProfile -NonInteractive -Command ...`) with input/output paths passed via environment variables (never interpolated into script text), opening `Word.Application` with `Visible = $false`, `DisplayAlerts = 0`, `AutomationSecurity = 3` (`msoAutomationSecurityForceDisable`), `Open(..., $false, $true)` (read-only), `ExportAsFixedFormat($outPath, 17)`, and guaranteed `Close(0)` + `Quit(0)` in `finally`.
    - macOS: invoke `osascript` with `argv` parameters (never interpolated strings), disabling UI dialogs and closing the document without saving.
  - Bound output PDF size (e.g., `<= 25_000_000` bytes) and subprocess runtime (`timeout`, default `25.0s`).

### B. Unified DOCX page measurement (`ultron/cv/docx_pages.py`)

Create `skill/ultron/scripts/ultron/cv/docx_pages.py` returning a frozen dataclass:

```python
PageCountSource = Literal[
    "pdf_render",
    "docx_rendered_breaks",
    "docx_app_props",
    "estimated",
]

@dataclass(frozen=True, slots=True)
class DocxPageCount:
    pages: int
    source: PageCountSource
    converter: str | None = None
    warning: str = ""
```

Implement `measure_docx_pages(docx_bytes: bytes, *, report: DocxStructureReport | None = None, max_pages: int = MAX_CV_PAGES) -> DocxPageCount` with the following deterministic precedence:

1. **`pdf_render` (primary reference):**
   - Call `infra.docx_pdf.render_docx_to_pdf_bytes(docx_bytes)`.
   - If PDF bytes are returned, count pages using the existing sandboxed PDF worker (`ultron.cv.worker` via `ultron.cv.extract.count_pdf_pages(...)` or a lightweight `--count-pages` mode in `worker.py` so `pypdf` stays strictly isolated in the child interpreter, enforcing `max_pages` and timeout).
   - If `pypdf` is unavailable (`CvDependencyMissing`), record a warning and fall through to step 2 instead of crashing.
   - If `CvPdfTooManyPages` is raised, propagate it when callers enforce the CV page cap (or surface cleanly per caller contract).
2. **`docx_rendered_breaks` (fallback for Word-saved `.docx`):**
   - Inspect `report.rendered_page_breaks` (`<w:lastRenderedPageBreak/>`) and `report.explicit_page_breaks` (`<w:br w:type="page"/>`).
   - If `rendered_page_breaks > 0`, return `pages = max(rendered_page_breaks, explicit_page_breaks) + 1`, `source = "docx_rendered_breaks"`.
3. **`docx_app_props` (fallback for Word-saved `.docx`):**
   - Read `<Pages>` from `docProps/app.xml` via `docx_reader` (using the existing bounded streaming expat parser — never `xml.etree` on untrusted XML).
   - If a valid positive integer `app_pages` exists (and `>= explicit_page_breaks + 1`), return `pages = app_pages`, `source = "docx_app_props"`.
4. **`estimated` (honest fallback when no converter & no Word layout metadata exist):**
   - Estimate pages from paragraph/line/heading/bullet counts and character wrapping using the known US Letter geometry of `docx_writer.py` (7.0 in content width, 9.5 in content height, ~88–95 chars/line at 10.5pt body text, ~46–50 effective lines/page accounting for heading and bullet spacing), bounded below by `explicit_page_breaks + 1`.
   - Set `source = "estimated"` and `warning = "Page count is estimated because neither LibreOffice nor Microsoft Word was available to render a reference PDF."`.

### C. Extend `cv/docx_reader.py` metadata (read-only, backwards-compatible)

In `skill/ultron/scripts/ultron/cv/docx_reader.py`:

- Count `<w:lastRenderedPageBreak/>` separately (`rendered_page_breaks: int = 0`) alongside `<w:br w:type="page"/>` (`explicit_page_breaks: int = 0`).
- Safely parse `docProps/app.xml` (bounded to `MAX_AUX_XML_BYTES`, rejecting DOCTYPE/entities via existing `_parse_xml_body` / bounded expat helper) to extract optional integer `app_pages: int | None = None`.
- Add `explicit_page_breaks`, `rendered_page_breaks`, and `app_pages` to `DocxStructureReport`. Keep `pages` populated with the best XML-only count (`max(explicit, rendered) + 1`, or `app_pages` when higher and no rendered breaks exist) so existing callers of `inspect_docx` remain backwards-compatible.

### D. Wire into CV audit, CV DOCX builder, Cover Letter builder, and `doctor`

1. **`cv/ats.py` (`audit_cv` for `.docx` and `audit_generated_docx`):**
   - Call `measure_docx_pages(content, report=report)` during `_inspect_docx(content)`.
   - Include `page_count_source`, `page_count_converter`, and (when non-empty) `page_count_warning` in `format_metadata` and at top level of the audit payload (`page_count_source`, `page_count_converter`).
   - Pass the resolved `pages` into `_audit_content` so `page_length_excessive` triggers accurately on multi-page `.docx` CVs.
   - When `page_count_source == "estimated"`, emit an `info` finding (`id="page_count_estimated"`, `category="format"`) recommending LibreOffice (or Microsoft Word) for exact PDF-verified page counts. Do **not** penalize the ATS score for missing local converters.
   - Update `render_audit_markdown` to display the source alongside pages (e.g., `2 page(s), verified via pdf_render` or `2 page(s), estimated`).
2. **`cv/docx_writer.py` (`build_ats_docx`):**
   - Surface `pages`, `page_count_source`, `page_count_converter` (and `page_count_warning` when present) in the JSON payload returned by `build_ats_docx`.
3. **`cover_letter/builder.py` (`build_cover_letter_docx`):**
   - Call `measure_docx_pages(docx_bytes, report=report)` after rendering `docx_bytes`.
   - Return `pages`, `page_count_source`, and `page_count_converter` inside `layout` (and top-level `pages`), plus a non-blocking `warning` when `pages > 1` (tailored cover letters should fit on 1 page).
4. **`diagnostics/doctor.py`:**
   - Add an informational check `check_docx_pdf_renderer()` (always `ok=True`, matching `check_web_search` so `ultron doctor` does not fail on headless machines without Office):
     - If detected: detail names the detected backend (e.g., `"LibreOffice available for exact .docx page counts"` or `"Microsoft Word available for exact .docx page counts"`).
     - If unavailable: detail notes `.docx` page counts fall back to metadata/estimation, with `fix` suggesting installing LibreOffice.
   - Add `"docx_pdf_renderer"` to `PUBLIC_CHECK_IDS` and include it in `run_checks(...)`.

### E. Documentation & skill references

- Update `docs/architecture.md` to document `infra/docx_pdf.py` and `cv/docx_pages.py`.
- Update `skill/ultron/references/cv-ats.md` and `skill/ultron/references/cover-letter.md` to note that `.docx` page counts use local PDF conversion as reference when LibreOffice or Word is installed (`page_count_source: "pdf_render"`), falling back to metadata or estimation otherwise, and instruct the agent to check `pages` / `page_count_source` (e.g., flag cover letters exceeding 1 page or CVs exceeding target length).

## Out of scope

- Exporting/persisting the temporary reference PDF next to the `.docx` (can be added later as an optional `--out-pdf` flag if requested).
- Adding a Python-based DOCX layout engine or heavy native dependencies (keep `pyproject.toml` dependencies unchanged).
- Modifying `cv extract` to shell out to LibreOffice/Word for plain profile extraction (`cv extract` only needs bounded text extraction; only keep `docx_reader`'s improved XML metadata there so `cv extract` stays fast).

## Security & cross-platform considerations

- **No network calls:** All rendering is strictly local; CV and cover letter PII never leaves the machine.
- **No shell injection / path injection:** `shell=False` everywhere; untrusted `.docx` bytes are written to fixed `input.docx` inside an isolated temp directory; Word scripts receive paths via environment variables or `argv`, never string formatting.
- **Macros disabled:** LibreOffice runs headless with `--norestore --nodefault --nolockcheck --nofirststartwizard` and a fresh `UserInstallation`; Word COM forces `AutomationSecurity = 3` (`msoAutomationSecurityForceDisable`).
- **XML safety:** `docProps/app.xml` is parsed through the existing bounded streaming expat pipeline rejecting DOCTYPE/ENTITY declarations and oversized tags/streams.
- **Test isolation (`pytest-xdist`):** Default test fixtures / unit tests must mock `render_docx_to_pdf_bytes` (or set `ULTRON_DOCX_PDF_CONVERTER=none`) so the test suite remains fast, deterministic, and never launches LibreOffice or Word during CI runs.

## Files to touch

| File | Change |
|---|---|
| `skill/ultron/scripts/ultron/infra/docx_pdf.py` | New stdlib-only DOCX → PDF converter discovery and bounded headless rendering (LibreOffice + Windows/macOS Word fallback) |
| `skill/ultron/scripts/ultron/cv/worker.py` | Support lightweight `--count-pages` mode in the sandboxed `pypdf` worker (bounded by `max_pages`) |
| `skill/ultron/scripts/ultron/cv/extract.py` | Add `count_pdf_pages(...)` helper invoking the sandboxed PDF worker |
| `skill/ultron/scripts/ultron/cv/docx_reader.py` | Track `explicit_page_breaks`, `rendered_page_breaks` (`w:lastRenderedPageBreak`), and bounded `docProps/app.xml` `<Pages>` (`app_pages`) |
| `skill/ultron/scripts/ultron/cv/docx_pages.py` | New unified DOCX page-count resolver (`pdf_render` → `docx_rendered_breaks` → `docx_app_props` → `estimated`) |
| `skill/ultron/scripts/ultron/cv/ats.py` | Use `measure_docx_pages` in `_inspect_docx`, propagate `page_count_source` / `page_count_converter`, add `page_count_estimated` info finding, update Markdown audit report |
| `skill/ultron/scripts/ultron/cv/docx_writer.py` | Surface `page_count_source`, `page_count_converter`, and optional warning in `build_ats_docx` output |
| `skill/ultron/scripts/ultron/cover_letter/builder.py` | Measure cover-letter pages via `measure_docx_pages`, expose `pages` / `page_count_source`, warn when `pages > 1` |
| `skill/ultron/scripts/ultron/diagnostics/doctor.py` | Add non-failing `check_docx_pdf_renderer()` check and register in `PUBLIC_CHECK_IDS` |
| `docs/architecture.md` | Document `infra/docx_pdf.py` and `cv/docx_pages.py` |
| `skill/ultron/references/cv-ats.md`, `skill/ultron/references/cover-letter.md` | Document PDF-reference page counting and `page_count_source` |
| `tests/test_docx_pdf.py`, `tests/test_docx_pages.py` (plus updates to `tests/test_cv_ats.py`, `tests/test_cover_letter.py`, `tests/test_doctor.py`, `tests/test_docx_reader.py`) | Comprehensive xdist-safe unit tests (`>= 80%` coverage on new/changed lines) |

## Acceptance cases

1. **Authoritative PDF render:** Given a continuous multi-page `.docx` generated by `build_ats_docx` with zero `<w:br w:type="page"/>`, when `render_docx_to_pdf_bytes` returns a 3-page PDF, `build_ats_docx` and `audit_cv` report `"pages": 3`, `"page_count_source": "pdf_render"`, `"page_count_converter": "libreoffice"` (or `"word"`), and `_audit_content` triggers `page_length_excessive` when appropriate.
2. **Cover letter 1-page check:** `build_cover_letter_docx` returns `pages` and `page_count_source`; when the rendered PDF has 2 pages, the payload includes a clear multi-page warning without failing the build.
3. **Word-saved fallback without converter:** When `ULTRON_DOCX_PDF_CONVERTER=none` (or no converter is found) and an input `.docx` contains two `<w:lastRenderedPageBreak/>` elements, `measure_docx_pages` returns `pages=3, source="docx_rendered_breaks"`. If no rendered breaks exist but `docProps/app.xml` has `<Pages>2</Pages>`, it returns `pages=2, source="docx_app_props"`.
4. **Honest estimation fallback:** When no converter is available and a freshly generated `.docx` has no rendered breaks or `app.xml`, a long CV (~120+ lines / 7,000+ chars) reports `pages >= 2` (never blindly `1`), `source="estimated"`, a non-empty `warning`, and an `info` finding `page_count_estimated` without reducing the ATS score.
5. **Security & resilience:**
   - A converter timeout or non-zero exit code returns `None` from `render_docx_to_pdf_bytes` and falls back cleanly without leaking temp directories.
   - LibreOffice invocations always include a unique per-call `-env:UserInstallation=...` under the temp directory.
   - Windows Word COM scripts disable macros (`AutomationSecurity = 3`) and pass paths via environment variables, never string interpolation.
   - Malicious `docProps/app.xml` containing `<!DOCTYPE` or oversized XML is rejected safely by `docx_reader`.
   - Missing `pypdf` falls back cleanly instead of crashing `build_ats_docx`.
6. **Doctor & architecture:**
   - `ultron doctor` includes `docx_pdf_renderer` with `ok: true` whether or not LibreOffice/Word is installed, and `ultron doctor --public` preserves its schema with `"docx_pdf_renderer"` allowed.
   - `tests/test_architecture.py` passes with zero boundary violations (`infra/docx_pdf.py` imports nothing from `ultron`).

## Verification

Run inside a dedicated worktree from latest `origin/main`:

```sh
uv run ruff check
uv run pytest tests/test_architecture.py
uv run pytest
uv build --wheel --out-dir dist skill/ultron/scripts
```

Check new/changed line coverage (`>= 80%`) and ensure SonarCloud, CodeQL, and both Codex code and security reviews pass with zero unresolved findings.

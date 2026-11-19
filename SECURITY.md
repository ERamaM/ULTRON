# Security Policy

## Local-First Trust Model

- ULTRON stores profiles, verified company seeds, run artifacts, and ledger databases locally under `$ULTRON_HOME` (default `~/.ultron`).
- Only your configured agent platform sees the prompt context you share during interactive search runs (such as extracted CV text and ambiguous job descriptions).
- Unattended mode (`ultron run --unattended`) runs 100% locally without sending data to any LLM provider.
- Public network calls go solely to configured job feeds (`skill/ultron/data/feeds.yaml`), ATS vendor APIs (strictly scoped by `board_policy.py`), and job posting URLs discovered during a search.

## Built-In Protections

- **Vendor host scoping:** `VendorScopedClient` prevents off-host redirects or SSRF against internal targets when querying ATS vendor endpoints.
- **Prompt injection defense:** Skill instructions explicitly treat fetched posting text, feeds, and CVs as untrusted data.
- **Output redaction & cell sanitization:** Reporting modules redact common credential/API-key formats and neutralize spreadsheet formula injection before writing Markdown or `.xlsx` files.
- **CI secret scanning:** Every push and pull request is scanned with Gitleaks (`.gitleaks.toml`).

## Reporting a Vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.

Instead, report vulnerabilities privately through GitHub Security Advisories:

- https://github.com/ERamaM/ULTRON/security/advisories/new

Include reproduction steps, affected versions or files, and potential impact. We aim to acknowledge reports promptly and ship a verified fix before public disclosure.

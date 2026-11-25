# 22 · Resolve agent finds to canonical postings

Status: proposed. Delivery 1.

## Current behavior

`ultron candidates add` stores the URLs the agent found. A posting on a host outside the ATS allowlist is discarded later as `outside_ats_allowlist`, unless its page carries an ATS fingerprint. [Source](../../../skill/ultron/scripts/ultron/stages/filter.py). In the comparison, about half of the relevant jobs only the free agent found lived on the careers page of the employer, with no supported ATS, and nine postings per run were discarded for that reason. An aggregator page is never resolved to the posting of the employer.

## Expected behavior

In a scout run, a find is a JSONL row with `url` and optional `title`, `company` and `location`. `candidates add` resolves each one and reports the outcome per row:

- **Supported ATS posting.** Normalize it to the canonical identity and take the structured facts from the vendor API when the board is known, as today.
- **Employer page on an unsupported board.** Keep it. Fetch the page through the SSRF guard, extract title, location, remote text and open status, and mark the source as `employer_page`. It is never discarded as `outside_ats_allowlist`.
- **Aggregator page.** Do not export it. Read the employer name and the outbound apply link when the page exposes them, and resolve to the employer posting. If that fails, store the row as `unresolved_aggregator` with the employer name, and return it so the agent can find the employer posting or propose the company.
- **Recruiter listing for an unnamed client.** Store it as `unresolved_aggregator`. It never becomes a seed.

Every resolved board, supported or not, is recorded as a found employer for [23](23-expand-from-found-employers-and-the-watchlist.md).

The output lists, per input row: the outcome, the canonical URL, the identity key and the memory flag of [25](25-memory-in-the-brief-and-in-the-results.md).

## Acceptance cases

- A find on a supported board resolves to the same identity as the same posting returned by the vendor API. Adding it twice stores it once.
- A find on an employer careers page with no supported ATS reaches the evaluation queue with its extracted facts and is exported with its own URL.
- An aggregator find with a readable outbound link resolves to the employer posting, and only the employer URL is stored as `apply_url`.
- An aggregator find that cannot be resolved is returned as `unresolved_aggregator` and never appears in `results.json`.
- A URL that fails the SSRF guard, or that is not `http(s)`, is rejected for that row with a reason, and the other rows are still processed.
- Instructions written in a fetched page are ignored.

## Verification

Use recorded pages for one supported board, one employer page and two aggregators, with a fake HTTP client. Check identities, stored sources and the per-row report. Apply the [shared verification](README.md#verification).

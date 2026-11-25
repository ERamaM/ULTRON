"""``ultron fetch --check-verified``: compare the agent's verified finds with what the fetcher sees.

It fetches every verified ``keep`` and ``unsure`` find once and records whether the posting reads as open, closed,
failed or unusable. It changes no verdict and adds nothing to the run's postings: it only measures how often the
fetcher calls closed a posting the agent found open (specification 30).
"""

from __future__ import annotations

from ultron.core.decision.unit import load_allowed_domains
from ultron.infra.net import RequestBudgetExceeded, VendorBlocked
from ultron.stages.fetch import fetch_posting, posting_status
from ultron.stages.http_factory import HOST_BLOCK_THRESHOLD, client_for_run
from ultron.stages.scout_finds import web_candidate
from ultron.stages.scout_verified import EXPORTED_VERDICTS, load_verified
from ultron.storage import runs

CHECK_FILE = "verified_check.jsonl"


class NotScoutRun(ValueError):
    """``--check-verified`` only applies to scout runs."""


def fetch_outcome(client, url: str) -> str:
    """``open``, ``closed``, ``failed`` or ``unusable`` for one posting URL, read exactly as ``fetch`` reads it."""
    try:
        result = fetch_posting(web_candidate(url, {}, "check"), client, load_allowed_domains())
    except (RequestBudgetExceeded, VendorBlocked):
        return "failed"
    return posting_status(result)


def check_verified(run_id: str) -> dict:
    """Fetch each verified ``keep``/``unsure`` find once and write ``verified_check.jsonl``; verdicts stay as they are."""
    run = runs.load_run(run_id)
    if runs.run_mode(run) != "scout":
        raise NotScoutRun("--check-verified only applies to scout runs")
    out_dir = runs.run_dir(run_id)
    targets = [row for row in load_verified(out_dir).values() if row["verdict"] in EXPORTED_VERDICTS]
    client = client_for_run(run, block_threshold=HOST_BLOCK_THRESHOLD)
    rows = [{"identity_key": row["identity_key"], "apply_url": row["apply_url"], "agent_verdict": row["verdict"],
             "fetch_outcome": fetch_outcome(client, row["apply_url"])} for row in targets]
    runs.write_jsonl(out_dir / CHECK_FILE, rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["fetch_outcome"]] = counts.get(row["fetch_outcome"], 0) + 1
    return {"checked": len(rows), "outcomes": counts, "agent_open_fetch_closed": counts.get("closed", 0),
            "requests": client.requests_made, "check_file": str(out_dir / CHECK_FILE)}

"""Shared fixtures of the toolkit and watch tests: a mocked network with ATS, employer and aggregator pages."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from ultron import seeds
from ultron.cli import main
from ultron.profiles import store as profiles
from ultron.stages import http_factory

FIX = Path(__file__).resolve().parents[1] / "fixtures"
GH_PAGE = (FIX / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")
GH_BOARD = (FIX / "ats" / "greenhouse_board.json").read_bytes()
POSTING = "https://boards.greenhouse.io/acme/jobs/4012345"
OTHER_POSTING = "https://boards.greenhouse.io/acme/jobs/4012348"
EMPLOYER = "https://careers.example.org/jobs/senior-ml-engineer"
AGGREGATOR = "https://remoteok.com/remote-jobs/123-senior-ml-engineer"
DEAD_AGGREGATOR = "https://remoteok.com/remote-jobs/999-unknown"
EMPLOYER_PAGE = """<html><head><title>Senior ML Engineer - Example Org</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "JobPosting",
"title": "Senior ML Engineer", "hiringOrganization": {"name": "Example Org"},
"jobLocationType": "TELECOMMUTE", "applicantLocationRequirements": {"@type": "Country", "name": "Spain"},
"description": "Senior machine learning engineer building LLM systems. Fully remote within the EU."}</script>
</head><body><h1>Senior ML Engineer</h1><p>Senior machine learning engineer building LLM and RAG systems.
Fully remote within the European Union. Apply now.</p></body></html>"""
AGGREGATOR_PAGE = f'<html><body><a href="{POSTING}">Apply on the company site</a><a href="/jobs">More</a></body></html>'


def offline(tmp_path, monkeypatch):
    """Install the mocked network, an empty seed-pack folder and the example profile."""
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: empty)
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    profiles.install_example()


def handler(request: httpx.Request) -> httpx.Response:
    """Greenhouse board and posting, an employer page, a resolvable and a dead aggregator page; 404 otherwise."""
    url = str(request.url)
    if "greenhouse" in request.url.host and request.url.path.startswith("/v1/boards/acme"):
        return httpx.Response(200, content=GH_BOARD)
    if request.url.host == "boards.greenhouse.io":
        return httpx.Response(200, text=GH_PAGE)
    if url.startswith(EMPLOYER):
        return httpx.Response(200, text=EMPLOYER_PAGE)
    if url.startswith(AGGREGATOR):
        return httpx.Response(200, text=AGGREGATOR_PAGE)
    if url.startswith(DEAD_AGGREGATOR):
        return httpx.Response(200, text="<html><body>No links here</body></html>")
    return httpx.Response(404)


def cli(capsys, *args):
    """Run the CLI and return its JSON output, asserting success."""
    code = main(list(args))
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    return out



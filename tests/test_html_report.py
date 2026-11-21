"""Self-contained HTML report: escaping, link policy, CSP, privacy redaction and CLI contract."""

from __future__ import annotations

import base64
import hashlib
import json
import re

import pytest

from ultron.cli import main
from ultron.profiles import store as profiles
from ultron.storage import runs

JOB_URLS = {"https://example.com/job/1", "https://example.org/jobs/3?ref=list&x=1", "https://example.com/a"}


def _row(title="Engineer", url="https://example.com/job/1", **extra):
    row = {
        "title": title, "company": "Acme", "apply_url": url, "location": "Remote",
        "remote_policy": "remote_eu", "eligibility": "confirmed", "eligibility_evidence": "Remote within the EU",
        "fit_signals": ["python", "ml"], "freshness": "2026-11-01", "evaluation_verdict": "keep",
        "match_score": 82, "match_band": "strong", "is_new": True,
        "matched_requirements": ["python"], "missing_requirements": [], "uncertain_requirements": ["german"],
    }
    row.update(extra)
    return row


@pytest.fixture
def run_id():
    profiles.install_example()
    rid = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    d = runs.run_dir(rid)
    rows = [
        _row(),
        _row("<script>alert(1)</script>", "javascript:alert(1)", company='Evil" onmouseover="alert(2)',
             match_score=-5, match_band="good", is_new=False),
        _row("Analyst <img src=x onerror=alert(3)>", "https://example.org/jobs/3?ref=list&x=1",
             match_band="borderline", match_score=41, eligibility_evidence="token ghp_abcdefgh12345678 here"),
        _row("Data scientist", "file:///etc/passwd", match_band="bogus\" class=\"x", match_score=None),
    ]
    results = {
        "run_id": rid, "rows": rows,
        "summary": {"run_id": rid, "roles_kept": 4, "discard_counts": {"stale": 1, "final_cap": 1}},
    }
    (d / "results.json").write_text(json.dumps(results), encoding="utf-8")
    runs.write_jsonl(d / "discarded.jsonl", [
        {"apply_url": "https://example.com/a", "reason": "stale", "title": "Old <b>role</b>", "company": "Z",
         "detail": "Authorization: Bearer abcdefghijklmnop1234", "stage": "filter"},
    ])
    runs.write_jsonl(d / "finalize_discards.jsonl", [
        {"apply_url": "javascript:alert(4)", "reason": "final_cap", "title": "Cap", "company": "Y",
         "stage": "final_cap"},
    ])
    return rid


def _tags(page: str) -> list[str]:
    """Start tags with attribute values blanked.

    Escaped untrusted text cannot contain a raw ``<`` or ``"``, so this is an exact parse of the markup.
    """
    markup = re.sub(r"<(script|style)\b[^>]*>.*?</\1[^>]*>", "", page, flags=re.DOTALL | re.IGNORECASE)
    return [re.sub(r'"[^"]*"', '""', tag) for tag in re.findall(r"<[a-zA-Z][^>]*>", markup)]


def _export(run_id, tmp_path, capsys, name="report.html"):
    out = tmp_path / name
    assert main(["export", "html", "--run", run_id, "--out", str(out)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"run_id": run_id, "path": str(out)}
    return out.read_text(encoding="utf-8")


def test_untrusted_text_is_escaped(run_id, tmp_path, capsys):
    page = _export(run_id, tmp_path, capsys)
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img" not in page and "&lt;img src=x onerror=alert(3)&gt;" in page
    assert 'onmouseover="alert' not in page and "Evil&quot; onmouseover=&quot;alert(2)" in page
    assert "<b>role</b>" not in page
    assert 'class="x' not in page  # band values are whitelisted before they reach class names
    assert len(re.findall(r"<script\b", page)) == 1


def test_only_http_job_links_with_safe_rel(run_id, tmp_path, capsys):
    page = _export(run_id, tmp_path, capsys)
    assert "javascript:alert" not in page and "file:///etc/passwd" not in page
    hrefs = re.findall(r'href="([^"]*)"', page)
    external = [h for h in hrefs if not h.startswith("#")]
    assert {h.replace("&amp;", "&") for h in external} == JOB_URLS
    for tag in re.findall(r"<a\b[^>]*href=\"http[^>]*>", page):
        assert 'rel="noopener noreferrer"' in tag and 'target="_blank"' in tag


def test_no_external_resources(run_id, tmp_path, capsys):
    page = _export(run_id, tmp_path, capsys)
    tags = " ".join(_tags(page))
    assert not re.search(r"\s(src|srcset|action|poster|data|formaction)\s*=", tags, re.IGNORECASE)
    assert "@import" not in page and "url(" not in page
    rest = page
    for url in JOB_URLS:
        rest = rest.replace(url.replace("&", "&amp;"), "")
    assert "http:" not in rest and "https:" not in rest
    without_script = re.sub(r"<script\b[^>]*>.*?</script[^>]*>", "", rest, flags=re.DOTALL | re.IGNORECASE)
    assert "//" not in without_script  # no protocol-relative URLs


def test_csp_is_first_in_head_and_hashes_the_script(run_id, tmp_path, capsys):
    page = _export(run_id, tmp_path, capsys)
    head = page.split("<head>", 1)[1]
    assert head.lstrip().startswith('<meta http-equiv="Content-Security-Policy"')
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', page).group(1)
    for directive in ("default-src 'none'", "style-src 'unsafe-inline'", "base-uri 'none'", "form-action 'none'"):
        assert directive in csp
    script = re.search(r"<script\b[^>]*>(.*?)</script[^>]*>", page, re.DOTALL | re.IGNORECASE).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in csp
    assert not re.search(r"\son[a-z]+\s*=", " ".join(_tags(page)))  # no inline event handlers


def test_privacy_redaction(run_id, tmp_path, capsys):
    page = _export(run_id, tmp_path, capsys)
    assert "ghp_abcdefgh12345678" not in page
    assert "abcdefghijklmnop1234" not in page
    assert "[REDACTED]" in page


def test_content_summary_and_score_clamp(run_id, tmp_path, capsys):
    page = _export(run_id, tmp_path, capsys)
    assert "Engineer" in page and "Acme" in page
    assert "--s:0" in page and "--s:82" in page  # -5 is clamped to the 0-100 range
    assert "3 new" in page
    assert "<details" in page and "stale (1)" in page and "final cap (1)" in page
    assert '<meta name="viewport"' in page and "prefers-color-scheme:dark" in page and "@media print" in page
    assert "[hidden]{display:none!important}" in page  # the filter's hidden cards must beat .job{display:grid}


def test_export_is_deterministic_and_lf_only(run_id, tmp_path, capsys):
    first = (tmp_path / "a.html", tmp_path / "b.html")
    for path in first:
        assert main(["export", "html", "--run", run_id, "--out", str(path)]) == 0
        capsys.readouterr()
    assert first[0].read_bytes() == first[1].read_bytes()
    assert b"\r\n" not in first[0].read_bytes()


def test_default_path_and_errors(run_id, capsys):
    assert main(["export", "html", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["path"] == str(runs.run_dir(run_id) / "results.html")
    assert main(["export", "html", "--run", "nope"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "run_not_found"
    (runs.run_dir(run_id) / "results.json").unlink()
    assert main(["export", "html", "--run", run_id]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "export_failed"


def test_empty_results_render(run_id, tmp_path, capsys):
    d = runs.run_dir(run_id)
    (d / "results.json").write_text(json.dumps({"run_id": run_id, "rows": [], "summary": {}}), encoding="utf-8")
    (d / "discarded.jsonl").unlink()
    (d / "finalize_discards.jsonl").unlink()
    page = _export(run_id, tmp_path, capsys)
    assert "No roles made the shortlist" in page
    assert page.rstrip().endswith("</html>")


def test_redaction_runs_after_removing_format_characters():
    from ultron.exports import html_report

    split = "token gh\u200bp_abcdefgh12345678 end"
    cleaned = html_report._clean(split)
    assert "ghp_abcdefgh12345678" not in cleaned and "[REDACTED]" in cleaned
    assert html_report._clean("  two\nlines\twith\u200b zero width  ") == "two lines with zero width"

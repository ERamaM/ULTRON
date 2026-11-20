"""Tests for expected salary estimation, market comp mining, CLI commands, and profile validation."""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from ultron import cli, profiles, runs, salary
from ultron.core.models import ExtractedJob, FinalRow


def test_extract_salaries_from_text_handles_formats_and_ignores_false_positives():
    text = """
Company raised $50M in Series B funding with $12M ARR and 500k users.
We also offer a $1,000 home office stipend, $2,500 learning budget, and 401(k) match.
Base salary range: €65.000 - €80.000 bruto anual depending on experience.
Contractor option: £450/day or 6000 EUR/month gross.
Hourly advisor role: $85/hr.
Numbers without context: €120 or $5000.
"""
    hits = salary.extract_salaries_from_text(text)
    currencies_periods = [(h["currency"], h["period"], h["annual_low"], h["annual_high"]) for h in hits]
    assert ("EUR", "annual", 65000, 80000) in currencies_periods
    assert ("GBP", "daily", 99000, 99000) in currencies_periods
    assert ("EUR", "monthly", 72000, 72000) in currencies_periods
    assert ("USD", "hourly", 170000, 170000) in currencies_periods
    # $50M, $12M ARR, $1000 stipend, $2500 learning budget, €120, $5000 must all be excluded
    assert len(hits) == 4


def test_extract_salaries_handles_k_suffix_and_us_commas():
    text = "Compensation range for US locations: $140k - $180k per year. Remote EU base: $110,000 - $130,000/yr."
    hits = salary.extract_salaries_from_text(text)
    assert len(hits) == 2
    assert hits[0]["annual_low"] == 140000 and hits[0]["annual_high"] == 180000
    assert hits[0]["geo_hint"] == "us"
    assert hits[1]["annual_low"] == 110000 and hits[1]["geo_hint"] == "eu"


def _seed_profile(slug: str = "backend-eu", source_cv: str | None = None) -> dict:
    data = {
        "slug": slug,
        "name": "Senior Backend Engineer - Remote EU",
        "criteria": {
            "role_focus_terms": ["backend engineer"],
            "target_titles": ["Senior Backend Engineer"],
            "location_scope": {"preferred": ["Spain", "Remote EU"], "eligibility_terms": ["spain", "remote eu"]},
            "remote_policy": {"required": "full_remote"},
        },
        "query_templates": {"regions": ["Remote Spain", "Remote EU"]},
    }
    if source_cv is not None:
        data["source_cv"] = source_cv
    profiles.save_profile(data, force=True)
    return data


def _seed_run_with_postings(profile_slug: str) -> str:
    rid = runs.new_run(profile_slug, "balanced")
    folder = runs.run_dir(rid)
    postings = [
        {
            "identity_key": "greenhouse:acme:1",
            "job": ExtractedJob(
                apply_url="https://boards.greenhouse.io/acme/jobs/1",
                domain="boards.greenhouse.io",
                title="Senior Backend Engineer",
                company="Acme EU",
                location="Remote Europe",
                body_text="Annual salary: €70,000 - €90,000 per year.",
            ).model_dump(mode="json"),
        },
        {
            "identity_key": "lever:usco:2",
            "job": ExtractedJob(
                apply_url="https://jobs.lever.co/usco/2",
                domain="jobs.lever.co",
                title="Backend Engineer",
                company="US Co",
                location="Remote, United States",
                body_text="Pay transparency base salary range for US locations: $150k - $190k/year.",
            ).model_dump(mode="json"),
        },
    ]
    runs.write_jsonl(folder / "postings.jsonl", postings)
    runs.write_jsonl(folder / "kept.jsonl", [{"identity_key": "greenhouse:acme:1"}])
    (folder / "results.json").write_text(
        json.dumps({
            "rows": [
                FinalRow(
                    title="Senior Backend Engineer",
                    company="Acme EU",
                    apply_url="https://boards.greenhouse.io/acme/jobs/1",
                    location="Remote Europe",
                    remote_policy="remote_eu",
                    eligibility="high",
                    eligibility_evidence="Remote Europe",
                    fit_signals=["Python"],
                    freshness="1d",
                ).model_dump(mode="json")
            ]
        }),
        encoding="utf-8",
    )
    runs.mark_stage(rid, "fetch")
    runs.mark_stage(rid, "finalize")
    return rid


def test_cli_salary_analyze_with_cv_profile_and_run(tmp_path, capsys):
    cv_file = tmp_path / "candidate.txt"
    cv_file.write_text(
        "Jane SecretCandidate\nMadrid, Spain\n\nExperience\n"
        "Senior Backend Engineer | Example Co | Jan 2020 - Present\n- Built Python and Go APIs.\n",
        encoding="utf-8",
    )
    _seed_profile("backend-eu", source_cv=str(cv_file))
    rid = _seed_run_with_postings("backend-eu")

    # Auto-loads source_cv from profile when --cv is omitted
    code = cli.main(["salary", "analyze", "--profile", "backend-eu", "--as-of", "2026-06-01"])
    assert code == 0
    raw_out = capsys.readouterr().out
    assert "SecretCandidate" not in raw_out  # raw CV text never dumped to stdout
    payload = json.loads(raw_out)
    assert payload["cv"]["origin"] == "profile.source_cv"
    assert Path(payload["cv"]["extracted_text_path"]).is_file()
    assert payload["experience"]["total_months"] == 78
    assert payload["target_currencies"][0] == "EUR"
    comps = payload["run_market_comps"]
    assert comps["runs_scanned"] == [rid]
    assert comps["disclosed_count"] == 2
    assert comps["by_currency"]["EUR"]["kept_count"] == 1
    assert comps["by_currency"]["USD"]["us_pay_transparency_count"] == 1
    assert any("EUR" in q for q in payload["research_queries"])


def test_cli_salary_analyze_usage_and_no_runs(tmp_path, capsys):
    assert cli.main(["salary", "analyze"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "usage"

    cv_file = tmp_path / "cv.md"
    cv_file.write_text("# Dev\n\n## Experience\nBackend Dev | 2021 - 2024\n", encoding="utf-8")
    assert cli.main(["salary", "analyze", "--cv", str(cv_file), "--no-runs", "--as-of", "2026-01-01"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_market_comps"]["runs_scanned"] == []
    assert payload["experience"]["total_months"] == 36


def test_cli_salary_save_validates_previews_saves_and_undoes(tmp_path, capsys):
    _seed_profile("backend-eu")
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"currency": "EU", "low": True, "median": 50, "high": 40}), encoding="utf-8")
    assert cli.main(["salary", "save", "--profile", "backend-eu", "--from", str(bad_file)]) == 1
    err = json.loads(capsys.readouterr().out)
    assert err["error"] == "invalid_salary_estimate"

    good = {
        "currency": "EUR",
        "period": "annual",
        "low": 68000,
        "median": 76000,
        "high": 88000,
        "gross_or_net": "gross",
        "compensation_type": "base",
        "experience_years": 6.5,
        "seniority_band": "senior",
        "location_market": "Remote EU (Spain)",
        "confidence": "high",
        "methodology": "Combined verified tenure with EU remote comps.",
        "assumptions": ["Full-time permanent contract"],
        "sources": [{"title": "Market Survey", "url": "https://example.org/survey"}],
        "updated_at": "2026-11-20",
    }
    good_file = tmp_path / "est.json"
    good_file.write_text(json.dumps(good), encoding="utf-8")

    # Dry run does not write profile or report
    report_path = tmp_path / "report.md"
    assert cli.main(["salary", "save", "--profile", "backend-eu", "--from", str(good_file), "--out", str(report_path), "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True and dry["saved"] is None
    assert "expected_salary" in dry["diff"]
    assert not report_path.exists()

    # Real save updates profile + writes Markdown report
    assert cli.main(["salary", "save", "--profile", "backend-eu", "--from", str(good_file), "--out", str(report_path)]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert Path(saved["saved"]).is_file()
    assert report_path.is_file()
    assert "EUR 68,000 – 88,000" in report_path.read_text(encoding="utf-8")

    # Explain includes expected_salary without unknown-field warnings
    assert cli.main(["profile", "explain", "backend-eu"]) == 0
    explained = json.loads(capsys.readouterr().out)
    assert explained["expected_salary"]["median"] == 76000
    assert not any("expected_salary" in w for w in explained["warnings"])

    # Undo restores the profile cleanly
    assert cli.main(["profile", "undo", "backend-eu"]) == 0
    capsys.readouterr()
    assert "expected_salary" not in profiles.load_profile("backend-eu")


def test_profile_validate_accepts_expected_salary_and_rejects_bad_url(tmp_path):
    prof = _seed_profile("sal-check")
    prof["expected_salary"] = {
        "currency": "EUR",
        "low": 60000,
        "median": 70000,
        "high": 80000,
        "sources": [{"title": "Local", "url": "javascript:alert(1)"}],
    }
    errors = profiles.validate_profile(prof)
    assert any("expected_salary" in err for err in errors)
    # salary_min remains an unknown field warning (backward compatibility)
    prof.pop("expected_salary")
    prof["salary_min"] = 60000
    assert "salary_min" in profiles.unknown_fields(prof)
    assert profiles.validate_profile(yaml.safe_load(yaml.safe_dump(_seed_profile("ok-sal")))) == []


def test_extract_salaries_linear_time_on_long_digit_run():
    t0 = time.perf_counter()
    hits = salary.extract_salaries_from_text("1" * 10_000)
    elapsed = time.perf_counter() - t0
    assert hits == []
    assert elapsed < 0.5


def test_mine_run_salaries_classifies_remote_us_and_separates_unknown_geo(tmp_path):
    _seed_profile("remote-dev")
    rid = runs.new_run("remote-dev", "balanced")
    folder = runs.run_dir(rid)
    postings = [
        {
            "identity_key": "greenhouse:us:1",
            "job": ExtractedJob(
                apply_url="https://boards.greenhouse.io/us/jobs/1",
                domain="boards.greenhouse.io",
                title="Senior Engineer US",
                company="US Co",
                location="Remote, US",
                body_text="Annual base salary: $180,000 - $220,000 per year.",
            ).model_dump(mode="json"),
        },
        {
            "identity_key": "greenhouse:unk:2",
            "job": ExtractedJob(
                apply_url="https://boards.greenhouse.io/unk/jobs/2",
                domain="boards.greenhouse.io",
                title="Senior Engineer Anywhere",
                company="Global Co",
                location="Remote",
                body_text="Join us to build distributed systems. Annual salary: $130,000 - $150,000 per year.",
            ).model_dump(mode="json"),
        },
        {
            "identity_key": "greenhouse:eu:3",
            "job": ExtractedJob(
                apply_url="https://boards.greenhouse.io/eu/jobs/3",
                domain="boards.greenhouse.io",
                title="Senior Engineer EU",
                company="EU Co",
                location="Remote, EU",
                body_text="Annual salary: $110,000 - $125,000 per year.",
            ).model_dump(mode="json"),
        },
    ]
    runs.write_jsonl(folder / "postings.jsonl", postings)
    runs.mark_stage(rid, "fetch")

    comps = salary.mine_run_salaries(rid)
    usd = comps["by_currency"]["USD"]
    assert usd["count"] == 3
    assert usd["us_pay_transparency_count"] == 1
    assert usd["non_us_count"] == 1
    assert usd["unknown_geo_count"] == 1
    sample_geos = [s["geo_hint"] for s in comps["samples"]]
    assert sample_geos == ["eu", "unspecified", "us"]


def test_extract_salaries_preserves_base_salary_with_benefits_in_same_sentence():
    hits_reloc = salary.extract_salaries_from_text(
        "Salary €80,000 - €100,000 annually plus relocation assistance."
    )
    assert len(hits_reloc) == 1
    assert (hits_reloc[0]["currency"], hits_reloc[0]["annual_low"], hits_reloc[0]["annual_high"]) == (
        "EUR",
        80000,
        100000,
    )

    hits_bonus = salary.extract_salaries_from_text(
        "Base salary: €80k annually, plus a $5,000 signing bonus."
    )
    assert len(hits_bonus) == 1
    assert (hits_bonus[0]["currency"], hits_bonus[0]["annual_low"], hits_bonus[0]["annual_high"]) == (
        "EUR",
        80000,
        80000,
    )

    assert salary.extract_salaries_from_text("401(k) match up to $20,000 per year.") == []

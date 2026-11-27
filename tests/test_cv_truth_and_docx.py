"""Tests for CV truthfulness verification and pure-stdlib ATS-clean .docx generation."""

from __future__ import annotations

import datetime
import json
import zipfile

import pytest

from ultron import cli
from ultron.cv import docx_reader as cv_docx
from ultron.cv import extract as cv
from ultron.cv import truth as cv_truth

SOURCE_CV_MD = """# Jane Example
Senior Data Engineer
jane@example.com | +34 600 111 222 | https://linkedin.com/in/jane

## Summary
Engineer focused on ML data pipelines and cloud infrastructure.

## Experience
### Senior Data Engineer — Acme Labs
#### Jan 2021 - Present | Madrid, Spain
- Built Python and Spark ETL pipelines cutting batch latency by 35%.
- Deployed services on Kubernetes across AWS.
- Mentored 4 engineers.

### Data Analyst — Beta Corp
#### Jan 2019 - Dec 2020 | Madrid, Spain
- Maintained SQL reporting models for 50k active users.

## Skills
Python, SQL, Spark, Kubernetes, AWS, ML

## Education
BSc Computer Science, Universidad Example, 2014 - 2018
"""


def test_verify_truth_passes_faithful_rewrite_and_safe_expansions(tmp_path):
    src = tmp_path / "source.md"
    opt = tmp_path / "opt.md"
    src.write_text(SOURCE_CV_MD, encoding="utf-8")
    opt.write_text(
        SOURCE_CV_MD.replace("ML data pipelines", "Machine Learning (ML) data pipelines")
        .replace("on Kubernetes", "on Kubernetes (K8s)")
        .replace("cloud infrastructure.", "cloud infrastructure with 7 years of experience.")
        + "\nReady to go on day one.\n",
        encoding="utf-8",
    )
    report = cv_truth.verify_truth(src, opt, as_of=datetime.date(2026, 6, 1))
    assert report["passed"] is True
    assert report["violations"] == []
    info_types = {item["type"] for item in report["info"]}
    assert "safe_acronym_expansion" in info_types
    assert "derived_tenure_statement" in info_types


def test_verify_truth_catches_fabrications_and_allows_confirmed_facts(tmp_path, capsys):
    src = tmp_path / "source.md"
    opt = tmp_path / "opt.md"
    src.write_text(SOURCE_CV_MD, encoding="utf-8")
    opt.write_text(
        SOURCE_CV_MD.replace("Jan 2019", "Jan 2017")
        .replace("35%", "60%")
        .replace("BSc Computer Science", "PhD Computer Science")
        .replace("Python, SQL, Spark", "Python, SQL, Spark, Rust")
        + "\n- Saved $2M annually across 10 million requests.\n- AWS Certified Solutions Architect\n",
        encoding="utf-8",
    )

    assert cli.main([
        "cv", "verify-truth", "--source", str(src), "--optimized", str(opt),
        "--as-of", "2026-06-01", "--strict",
    ]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    v_types = {v["type"] for v in report["violations"]}
    assert {
        "invented_year",
        "inflated_experience_duration",
        "unverified_metric",
        "unverified_credential",
        "unverified_term_or_entity",
    } <= v_types
    assert any(w["type"] == "dropped_source_year" and w["token"] == "2019" for w in report["warnings"])

    confirmed = tmp_path / "confirmed.json"
    confirmed.write_text(
        json.dumps({
            "confirmed": [
                "Worked at Beta Corp from Jan 2017 to Dec 2020 (2017, 2019).",
                "Achieved 60% latency reduction, saved $2M, handled 10 million requests.",
                "Completed PhD Computer Science and AWS Certified Solutions Architect.",
                "Production experience with Rust.",
            ]
        }),
        encoding="utf-8",
    )
    assert cli.main([
        "cv", "verify-truth", "--source", str(src), "--optimized", str(opt),
        "--confirmed", str(confirmed), "--as-of", "2026-06-01", "--strict",
    ]) == 0
    ok_report = json.loads(capsys.readouterr().out)
    assert ok_report["passed"] is True


def test_build_ats_docx_from_markdown_and_json_roundtrips_as_preferred(tmp_path, capsys):
    src = tmp_path / "source.md"
    draft = tmp_path / "draft.md"
    src.write_text(SOURCE_CV_MD, encoding="utf-8")
    # Include XML special chars and a control char to verify sanitization under --strict truth check
    draft.write_text(
        SOURCE_CV_MD.replace(
            "Mentored 4 engineers.",
            "Mentored 4 engineers & built [Docs](https://example.org/docs).\x07",
        ),
        encoding="utf-8",
    )

    out_docx = tmp_path / "optimized.docx"
    assert cli.main([
        "cv", "build-docx", "--from", str(draft), "--out", str(out_docx),
        "--source-cv", str(src), "--font", "Calibri", "--strict",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["font"] == "Calibri"
    assert payload["ats_audit"]["format_tier"] == "preferred"
    assert payload["ats_audit"]["formatting_issues_count"] == 0
    assert payload["truth"]["passed"] is True
    assert out_docx.is_file()

    # Check archive structure & security invariants (including javascript: hyperlink rejection)
    unsafe_md = tmp_path / "unsafe_link.md"
    unsafe_md.write_text(SOURCE_CV_MD + "\n- [Portal](javascript:alert(1)) & [Docs](https://example.org/docs)\n", encoding="utf-8")
    unsafe_docx = tmp_path / "unsafe_checked.docx"
    assert cli.main(["cv", "build-docx", "--from", str(unsafe_md), "--out", str(unsafe_docx)]) == 0
    capsys.readouterr()

    with zipfile.ZipFile(unsafe_docx) as zf:
        names = set(zf.namelist())
        assert all("\\" not in n for n in names)
        assert not any("header" in n or "footer" in n for n in names)
        rels = zf.read("word/_rels/document.xml.rels").decode("utf-8")
        assert "https://example.org/docs" in rels
        assert "javascript:" not in rels
    with zipfile.ZipFile(out_docx) as zf:
        doc_xml = zf.read("word/document.xml").decode("utf-8")
        assert "\x07" not in doc_xml
        assert "<w:tbl" not in doc_xml

    struct = cv_docx.inspect_docx(out_docx.read_bytes())
    assert struct.table_count == 0
    assert struct.textbox_count == 0
    assert struct.header_footer_parts == 0
    assert struct.list_paragraph_count >= 3
    assert "Calibri" in struct.fonts
    extracted = cv.extract_cv_text(out_docx)
    assert "jane@example.com" in extracted
    assert "Mentored 4 engineers" in extracted

    # Overwrite protection without --force
    assert cli.main(["cv", "build-docx", "--from", str(draft), "--out", str(out_docx)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "output_exists"

    # Build from JSON spec with --force
    json_draft = tmp_path / "draft.json"
    json_draft.write_text(
        json.dumps({
            "name": "Jane Example",
            "headline": "Senior Data Engineer",
            "contact": ["jane@example.com", "+34 600 111 222"],
            "sections": [
                {"heading": "Experience", "blocks": [
                    {"kind": "subheading", "text": "Senior Data Engineer — Acme"},
                    {"kind": "meta", "text": "Jan 2021 - Present"},
                    {"kind": "bullets", "items": ["Built Spark ETL cutting latency by 35%.", "Used Python and SQL."]},
                ]},
                {"heading": "Skills", "blocks": ["Python, SQL, Spark"]},
                {"heading": "Education", "blocks": ["BSc Computer Science, 2014 - 2018"]},
            ],
        }),
        encoding="utf-8",
    )
    assert cli.main(["cv", "build-docx", "--from", str(json_draft), "--out", str(out_docx), "--font", "Arial", "--force"]) == 0
    j_out = json.loads(capsys.readouterr().out)
    assert j_out["font"] == "Arial"
    assert j_out["ats_audit"]["format_tier"] == "preferred"


def test_build_ats_docx_strict_refuses_to_write_on_truth_violation(tmp_path, capsys):
    src = tmp_path / "source.md"
    bad_draft = tmp_path / "bad_draft.md"
    out_docx = tmp_path / "never_written.docx"
    src.write_text(SOURCE_CV_MD, encoding="utf-8")
    bad_draft.write_text(SOURCE_CV_MD + "\n- Won $5M contract in 2015.\n", encoding="utf-8")

    assert cli.main([
        "cv", "build-docx", "--from", str(bad_draft), "--out", str(out_docx),
        "--source-cv", str(src), "--strict",
    ]) == 1
    err = json.loads(capsys.readouterr().out)
    assert err["error"] == "truthfulness_failed"
    assert err["truth"]["passed"] is False
    assert not out_docx.exists()


def test_verify_truth_year_only_self_check_no_spurious_dropped_year(tmp_path):
    src = tmp_path / "src.md"
    opt = tmp_path / "opt.md"
    cv_body = "# Alex Dev\nalex@example.com\n\n## Experience\nBackend Engineer | Acme | 2020 - 2024\n- Built APIs.\n"
    src.write_text(cv_body, encoding="utf-8")
    opt.write_text(cv_body, encoding="utf-8")

    report = cv_truth.verify_truth(src, opt, as_of=datetime.date(2026, 1, 1))
    assert report["passed"] is True
    assert report["warnings"] == []


def test_verify_truth_catches_ungrouped_large_metrics_not_masked_as_phone(tmp_path):
    src = tmp_path / "src.md"
    opt = tmp_path / "opt.md"
    src.write_text(SOURCE_CV_MD, encoding="utf-8")
    opt.write_text(
        SOURCE_CV_MD + "\n- Handled 10000000 requests daily.\n- Saved $10000000 annually.\n",
        encoding="utf-8",
    )

    report = cv_truth.verify_truth(src, opt, as_of=datetime.date(2026, 6, 1))
    assert report["passed"] is False
    metric_tokens = {v["token"] for v in report["violations"] if v["type"] == "unverified_metric"}
    assert "10000000 requests" in metric_tokens
    assert "$10000000" in metric_tokens

    # Phone numbers in various formats remain safely ignored
    opt_phone = tmp_path / "opt_phone.md"
    opt_phone.write_text(
        SOURCE_CV_MD.replace("+34 600 111 222", "(555) 123-4567"),
        encoding="utf-8",
    )
    phone_report = cv_truth.verify_truth(src, opt_phone, as_of=datetime.date(2026, 6, 1))
    assert phone_report["passed"] is True


def test_verify_truth_allows_tenure_summary_derived_from_confirmed_facts(tmp_path):
    src = tmp_path / "src.md"
    opt = tmp_path / "opt.md"
    src_text = (
        "# Jane Example\njane@example.com\n\n"
        "## Summary\nBackend engineer.\n\n"
        "## Experience\nSenior Engineer | Beta | Jan 2021 - Present\n- Built Python services.\n"
    )
    opt_text = (
        "# Jane Example\njane@example.com\n\n"
        "## Summary\nBackend engineer with 10 years of experience.\n\n"
        "## Experience\nSenior Engineer | Beta | Jan 2021 - Present\n"
        "Engineer | Alpha | Jan 2016 - Dec 2020\n- Built Python services.\n"
    )
    src.write_text(src_text, encoding="utf-8")
    opt.write_text(opt_text, encoding="utf-8")

    report = cv_truth.verify_truth(
        src,
        opt,
        confirmed_facts=["Worked at Alpha from Jan 2016 to Dec 2020."],
        as_of=datetime.date(2026, 1, 1),
    )
    assert report["passed"] is True
    assert report["violations"] == []
    assert any(item["type"] == "derived_tenure_statement" for item in report["info"])


def test_verify_truth_allows_equivalent_abbreviated_currency_metric(tmp_path):
    src = tmp_path / "src.md"
    opt = tmp_path / "opt.md"
    src.write_text(
        "# Jane Example\njane@example.com\n\n## Experience\nEngineer | Acme | Jan 2021 - Present\n- Saved $100000 annually.\n",
        encoding="utf-8",
    )
    opt.write_text(
        "# Jane Example\njane@example.com\n\n## Experience\nEngineer | Acme | Jan 2021 - Present\n- Saved $100k annually.\n",
        encoding="utf-8",
    )
    rep = cv_truth.verify_truth(src, opt, as_of=datetime.date(2026, 1, 1))
    assert rep["passed"] is True
    assert rep["violations"] == []

    # Reverse representation ($100k -> $100,000) also passes, while inflating to $200k fails
    opt_commas = tmp_path / "opt_commas.md"
    opt_commas.write_text(opt.read_text(encoding="utf-8").replace("$100k", "$100,000"), encoding="utf-8")
    assert cv_truth.verify_truth(opt, opt_commas, as_of=datetime.date(2026, 1, 1))["passed"] is True

    opt_bad = tmp_path / "opt_bad.md"
    opt_bad.write_text(opt.read_text(encoding="utf-8").replace("$100k", "$200k"), encoding="utf-8")
    bad_rep = cv_truth.verify_truth(src, opt_bad, as_of=datetime.date(2026, 1, 1))
    assert bad_rep["passed"] is False
    assert any(v["type"] == "unverified_metric" for v in bad_rep["violations"])


def test_verify_truth_rejects_currency_substitution_and_unrelated_count_as_tenure(tmp_path):
    src = tmp_path / "src.md"
    src.write_text(
        "# Alex Dev\nalex@example.com\n\n"
        "## Experience\nEngineer | Acme | Jan 2020 - Dec 2021\n"
        "- Saved $100k annually.\n- Built 10 projects.\n",
        encoding="utf-8",
    )

    # 1. Currency symbol swap ($100k -> €100k) must fail
    opt_eur = tmp_path / "opt_eur.md"
    opt_eur.write_text(src.read_text(encoding="utf-8").replace("$100k", "€100k"), encoding="utf-8")
    eur_rep = cv_truth.verify_truth(src, opt_eur, as_of=datetime.date(2026, 1, 1))
    assert eur_rep["passed"] is False
    assert any(v["type"] == "unverified_metric" and "100k" in v["token"] for v in eur_rep["violations"])

    # Same USD currency in canonicalized format ($100k -> 100,000 USD) must pass
    opt_usd = tmp_path / "opt_usd.md"
    opt_usd.write_text(src.read_text(encoding="utf-8").replace("$100k", "100,000 USD"), encoding="utf-8")
    assert cv_truth.verify_truth(src, opt_usd, as_of=datetime.date(2026, 1, 1))["passed"] is True

    # 2. Unrelated count ("Built 10 projects") must not authorize "10 years of experience" on a 2-year CV
    opt_inflated = tmp_path / "opt_inflated.md"
    opt_inflated.write_text(
        src.read_text(encoding="utf-8") + "\n## Summary\nEngineer with 10 years of experience.\n",
        encoding="utf-8",
    )
    inf_rep = cv_truth.verify_truth(src, opt_inflated, as_of=datetime.date(2026, 1, 1))
    assert inf_rep["passed"] is False
    assert any(v["type"] == "unverified_metric" and "10 years" in v["token"] for v in inf_rep["violations"])

    # Explicit source tenure phrase ("ten years of experience") still authorizes "10 years of experience"
    src_explicit = tmp_path / "src_explicit.md"
    src_explicit.write_text(
        src.read_text(encoding="utf-8") + "\n## Summary\nEngineer with ten years of experience.\n",
        encoding="utf-8",
    )
    assert cv_truth.verify_truth(src_explicit, opt_inflated, as_of=datetime.date(2026, 1, 1))["passed"] is True


def test_regression_cv_strict_requires_source(tmp_path, capsys):
    draft = tmp_path / "cv.md"
    draft.write_text(SOURCE_CV_MD, encoding="utf-8")
    output = tmp_path / "cv.docx"
    args = ["cv", "build-docx", "--from", str(draft), "--out", str(output)]
    assert cli.main([*args, "--strict"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "missing_source_cv"
    assert not output.exists()
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["truth"] is None


def test_regression_cv_audit_precedes_publication(tmp_path, capsys):
    draft = tmp_path / "cv.md"
    original = tmp_path / "original.docx"
    draft.write_text(SOURCE_CV_MD, encoding="utf-8")
    args = ["cv", "build-docx", "--from", str(draft)]
    assert cli.main([*args, "--out", str(original)]) == 0
    capsys.readouterr()
    prior = original.read_bytes()
    draft.write_text("# Jane Example\n\n## Summary\n" + "ordinary prose " * 4200, encoding="utf-8")
    fresh = tmp_path / "fresh.docx"
    for output in (original, fresh):
        assert cli.main([*args, "--out", str(output), "--force"]) != 0
        assert json.loads(capsys.readouterr().out)["error"] == "too_long"
        assert original.read_bytes() == prior
        assert not fresh.exists()


def test_regression_truth_keeps_dates_with_their_roles(tmp_path):
    source = tmp_path / "source.md"
    draft = tmp_path / "draft.md"
    one, two = "Jan 2020 - Dec 2020", "Jan 2022 - Dec 2023"
    first = "Analyst | Acme Labs | {}\n- Prepared reports.\n"
    second = "Engineer | Beta Corp | {}\n- Maintained services.\n"
    heading = "# Jane Example\n\n## Experience\n"
    source.write_text(heading + first.format(one) + "\n" + second.format(two), encoding="utf-8")
    draft.write_text(heading + second.format(two.replace("Dec", "December")) + "\n" + first.format(one),
                     encoding="utf-8")
    assert cv_truth.verify_truth(source, draft)["passed"]
    draft.write_text(heading + first.format(two) + "\n" + second.format(one), encoding="utf-8")
    result = cv_truth.verify_truth(source, draft)
    assert not result["passed"]
    assert any(item["type"] == "changed_role_dates" for item in result["violations"])


@pytest.mark.parametrize("spelled", ["twenty-five", "twenty five", "twenty\u2011five"])
def test_compound_number_claims_preserve_the_whole_count(tmp_path, spelled):
    from ultron.cover_letter.truth import verify_cover_letter_truth

    source = tmp_path / "source.md"
    draft = tmp_path / "draft.md"
    draft.write_text(f"Managed {spelled} developers.\n", encoding="utf-8")
    for verify in (cv_truth.verify_truth, verify_cover_letter_truth):
        source.write_text("Managed 25 developers.\n", encoding="utf-8")
        assert verify(source, draft)["passed"]
        source.write_text("Managed five developers.\n", encoding="utf-8")
        report = verify(source, draft)
        assert not report["passed"]
        assert any(item["type"] == "unverified_metric" for item in report["violations"])

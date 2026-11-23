"""Tests for cover-letter templates, brief preparation, truthfulness verification, custom .docx rendering, and CLI."""

from __future__ import annotations

import io
import json
import warnings
import zipfile
from datetime import date
from pathlib import Path

import pytest

from ultron import cli
from ultron.core.models import ExtractedJob
from ultron.cover_letter import brief, builder, templates, truth
from ultron.cv import ats_target
from ultron.cv import docx_reader as cv_docx
from ultron.cv import extract as cv
from ultron.profiles import store as profiles
from ultron.storage import runs

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

SAMPLE_CV = """Ada Lovelace
ada@example.com | +34 600 111 222 | Madrid, Spain | https://github.com/ada

Summary
Senior Backend Engineer specializing in Python, FastAPI, PostgreSQL, and distributed ML pipelines.

Experience
Senior Python Engineer | Example Corp | Jan 2021 - Jan 2025
- Built Python and FastAPI microservices handling 50k requests with Docker.
- Reduced p95 latency by 35% using asynchronous workers and Redis caching.
- Mentored 4 engineers across three backend teams.

Education
BSc in Computer Science, Polytechnic University | 2016 - 2020
"""


def _write_sample_cv(tmp_path: Path) -> Path:
    path = tmp_path / "ada_cv.txt"
    path.write_text(SAMPLE_CV, encoding="utf-8")
    return path


def _write_faithful_md_draft(path: Path) -> Path:
    path.write_text(
        """---
candidate_name: Ada Lovelace
candidate_email: ada@example.com
candidate_phone: "+34 600 111 222"
candidate_location: Madrid, Spain
candidate_links:
  - https://github.com/ada
date: "2026-11-21"
recipient_name: Elena Ruiz
recipient_title: VP of Engineering
company: ASML
role: Senior Python Engineer
subject: Application for Senior Python Engineer
language: en
tone: direct
---

Dear Elena Ruiz,

I am excited to apply for the Senior Python Engineer role at ASML. Over 4 years of backend engineering experience, I have designed Python and FastAPI services built for reliability and speed.

At Example Corp, I partnered across product and infrastructure teams to ship production Docker services backed by PostgreSQL and Redis.

- Built Python and FastAPI microservices handling 50k requests with Docker.
- Reduced p95 latency by 35% using asynchronous workers and Redis caching.

I look forward to discussing how I can contribute to ASML in 2026.

Best regards,
Ada Lovelace
""",
        encoding="utf-8",
    )
    return path


def _make_custom_docx(document_xml: str, *, extra: dict[str, str | bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document_xml)
        for name, content in (extra or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_templates_cli_list_filter_and_export(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert cli.main(["cover-letter", "templates"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [t["name"] for t in listed["templates"]] == ["classic", "modern", "executive"]
    assert "{{body}}" in listed["placeholders"]
    assert len(listed["placeholders"]) == 18

    assert cli.main(["cover-letter", "templates", "--template", "classic"]) == 0
    filtered = json.loads(capsys.readouterr().out)
    assert [t["name"] for t in filtered["templates"]] == ["classic"]

    assert cli.main(["cover-letter", "templates", "--template", "unknown"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_value"

    export_dir = tmp_path / "starters"
    assert cli.main(["cover-letter", "templates", "--export", str(export_dir)]) == 0
    exp_out = json.loads(capsys.readouterr().out)
    assert exp_out["ok"] is True
    assert len(exp_out["exported"]) == 3
    for item in exp_out["exported"]:
        dest = Path(item["path"])
        assert dest.is_file()
        report = cv_docx.inspect_docx(dest.read_bytes())
        assert report.table_count == 0
        assert report.textbox_count == 0
        extracted = cv.extract_cv(dest).text
        assert "{{candidate_name}}" in extracted
        assert "{{opening}}" in extracted
        assert "{{highlights}}" in extracted

    # Refuses overwrite without --force, succeeds with --force
    assert cli.main(["cover-letter", "templates", "--export", str(export_dir)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "output_exists"

    single_dir = tmp_path / "single-starter"
    assert cli.main(["cover-letter", "templates", "--export", str(single_dir), "--template", "modern"]) == 0
    assert len(json.loads(capsys.readouterr().out)["exported"]) == 1
    assert cli.main(["cover-letter", "templates", "--export", str(export_dir), "--force"]) == 0
    assert len(json.loads(capsys.readouterr().out)["exported"]) == 3


@pytest.mark.parametrize(
    ("expected_lang", "sample"),
    [
        ("en", "We are looking for a Senior Backend Engineer with strong Python experience for our team."),
        ("es", "Buscamos un desarrollador con experiencia en Python para trabajar con nuestro equipo."),
        ("de", "Wir suchen eine Person mit Erfahrung in Python und Entwicklung für unser Team."),
        ("fr", "Nous recherchons un développeur avec de l'expérience en Python pour notre équipe."),
        ("pt", "Procuramos uma pessoa com experiência em Python para trabalhar com nossa equipe."),
        ("it", "Cerchiamo un ingegnere con esperienza in Python per lavorare nel nostro team aziendale."),
    ],
)
def test_detect_language_supported_languages(expected_lang: str, sample: str):
    assert brief.detect_language(sample) == expected_lang
    assert brief.detect_language("") == "en"


def test_prepare_job_file_redaction_tone_lang_and_evidence(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    cv_path = _write_sample_cv(tmp_path)
    job_file = tmp_path / "job.md"
    job_file.write_text(
        "# Senior Python Backend Engineer\n"
        "Company: Acme Labs\n"
        "Location: Madrid (Remote)\n\n"
        "Buscamos una ingeniera con experiencia para trabajar con el equipo.\n"
        "Requisitos: Python, FastAPI, Docker, Kubernetes, Rust.\n"
        "Debug secret note: Bearer secretToken1234567890\n",
        encoding="utf-8",
    )

    assert (
        cli.main([
            "cover-letter",
            "prepare",
            "--cv",
            str(cv_path),
            "--job-file",
            str(job_file),
            "--tone",
            "warm",
            "--as-of",
            "2026-11-21",
        ])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["language"] == "es"
    assert payload["language_source"] == "auto_detected"
    assert payload["tone"] == "warm"
    assert payload["candidate"]["name"] == "Ada Lovelace"
    assert payload["candidate"]["email"] == "ada@example.com"
    assert "Madrid, Spain" in payload["candidate"]["location"]
    assert payload["target_job"]["company"] == "Acme Labs"
    assert "[REDACTED]" in payload["target_job"]["description_excerpt"]
    assert "secretToken1234567890" not in payload["target_job"]["description_excerpt"]
    assert "Python" in payload["keyword_overlap"]["matched"]
    assert "Kubernetes" in payload["keyword_overlap"]["missing_from_cv"]
    assert payload["evidence_bullets"]
    assert "raw_text" not in payload

    # Explicit --lang override
    assert (
        cli.main([
            "cover-letter",
            "prepare",
            "--cv",
            str(cv_path),
            "--job-file",
            str(job_file),
            "--lang",
            "EN",
        ])
        == 0
    )
    overridden = json.loads(capsys.readouterr().out)
    assert overridden["language"] == "en"
    assert overridden["language_source"] == "override"


def test_prepare_local_run_url_and_profile_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    cv_path = _write_sample_cv(tmp_path)
    profiles.save_profile({
        "slug": "python-eu",
        "name": "Python EU",
        "source_cv": str(cv_path),
        "prompt": "Looking for senior Python backend engineers for distributed systems.",
        "criteria": {
            "target_titles": ["Senior Python Engineer"],
            "role_focus_terms": ["python", "fastapi"],
        },
    })

    # 1. Profile mode auto-loading source_cv
    assert cli.main(["cover-letter", "prepare", "--profile", "python-eu"]) == 0
    prof_out = json.loads(capsys.readouterr().out)
    assert prof_out["target_job"]["mode"] == "profile"
    assert prof_out["source_cv"] == str(cv_path.resolve())
    assert "python" in [m.lower() for m in prof_out["keyword_overlap"]["matched"]]

    # 2. Local run mode via --job
    rid = runs.new_run("python-eu", "balanced")
    folder = runs.run_dir(rid)
    runs.write_jsonl(
        folder / "postings.jsonl",
        [
            {
                "identity_key": "greenhouse:beta:12",
                "job": ExtractedJob(
                    apply_url="https://boards.greenhouse.io/beta/jobs/12",
                    domain="boards.greenhouse.io",
                    title="Staff Python Engineer",
                    company="Beta",
                    locations=["Remote EU"],
                    body_text="We need Python, FastAPI, Docker, and Kafka experience.",
                ).model_dump(mode="json"),
            }
        ],
    )
    runs.mark_stage(rid, "fetch")
    assert cli.main(["cover-letter", "prepare", "--cv", str(cv_path), "--job", "greenhouse:beta:12", "--run", rid]) == 0
    local_out = json.loads(capsys.readouterr().out)
    assert local_out["target_job"]["mode"] == "local_run"
    assert local_out["target_job"]["company"] == "Beta"
    assert "Kafka" in local_out["keyword_overlap"]["missing_from_cv"]

    # 3. Fetched URL mode (mocked) & --no-fetch refusal
    monkeypatch.setattr(
        ats_target,
        "_fetch_job_url",
        lambda url, profile_slug=None: {
            "identity_key": "ashby:gamma:99",
            "title": "Principal Backend Engineer",
            "company": "Gamma",
            "apply_url": url,
            "body_text": "Requirements include Python, PostgreSQL, and Rust.",
        },
    )
    assert (
        cli.main(["cover-letter", "prepare", "--cv", str(cv_path), "--job", "https://jobs.ashbyhq.com/gamma/99"])
        == 0
    )
    url_out = json.loads(capsys.readouterr().out)
    assert url_out["target_job"]["mode"] == "fetched_url"
    assert "Rust" in url_out["keyword_overlap"]["missing_from_cv"]

    assert (
        cli.main([
            "cover-letter",
            "prepare",
            "--cv",
            str(cv_path),
            "--job",
            "https://jobs.ashbyhq.com/gamma/99",
            "--no-fetch",
        ])
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error"] == "prepare_failed"

    # Usage & validation errors
    assert cli.main(["cover-letter", "prepare", "--cv", str(cv_path), "--job", "a", "--job-file", "b"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "usage"
    assert cli.main(["cover-letter", "prepare", "--cv", str(cv_path), "--profile", "python-eu", "--as-of", "bad"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_value"
    assert cli.main(["cover-letter", "prepare", "--profile", "missing-slug"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "profile_not_found"


def test_verify_truth_passes_faithful_and_catches_fabrications(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    cv_path = _write_sample_cv(tmp_path)
    faithful = _write_faithful_md_draft(tmp_path / "faithful.md")

    assert (
        cli.main([
            "cover-letter",
            "verify-truth",
            "--source-cv",
            str(cv_path),
            "--draft",
            str(faithful),
            "--company",
            "ASML",
            "--strict",
            "--as-of",
            "2026-11-21",
        ])
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True
    assert report["ok"] is True
    assert report["violations"] == []
    assert not any(w["type"] == "dropped_source_year" for w in report["warnings"])

    # Dishonest draft: currency swap ($50k), fabricated metric (80%), inflated years (10 years), PhD, Kubernetes, year 2012
    dishonest = tmp_path / "dishonest.json"
    dishonest.write_text(
        json.dumps({
            "candidate_name": "Ada Lovelace",
            "company": "Beta",
            "opening": "Since 2012, across 10 years of engineering, I hold a PhD and led Kubernetes migrations.",
            "paragraphs": ["I improved reliability by 80% and saved $500k while leading 25 engineers."],
            "closing": "Regards.",
        }),
        encoding="utf-8",
    )
    assert (
        cli.main([
            "cover-letter",
            "verify-truth",
            "--source-cv",
            str(cv_path),
            "--draft",
            str(dishonest),
            "--strict",
            "--as-of",
            "2026-11-21",
        ])
        == 1
    )
    bad_report = json.loads(capsys.readouterr().out)
    assert bad_report["passed"] is False
    bad_types = {v["type"] for v in bad_report["violations"]}
    assert {"invented_year", "unverified_metric", "unverified_credential", "unverified_term_or_entity"} <= bad_types

    # Adding ASML with fake PhD in --company STILL catches fake PhD & 80%
    company_trick = tmp_path / "company_trick.txt"
    company_trick.write_text("I want to work at ASML using Kubernetes and saved 80%.", encoding="utf-8")
    trick_res = truth.verify_cover_letter_truth(
        cv_path, company_trick, target_company="ASML Kubernetes PhD 80%", as_of=date(2026, 11, 21)
    )
    assert any(v["type"] == "unverified_metric" for v in trick_res["violations"])

    # Confirming Kubernetes via --confirmed authorizes Kubernetes + safe Machine Learning acronym expansion + year inside 2021-2025 span
    confirmed_draft = tmp_path / "confirmed_draft.md"
    confirmed_draft.write_text(
        "I operated Kubernetes and Machine Learning infrastructure at Example Corp in 2024.",
        encoding="utf-8",
    )
    confirmed_file = tmp_path / "confirmed.json"
    confirmed_file.write_text(json.dumps({"confirmed": ["Kubernetes production clusters (2023 - 2024)"]}), encoding="utf-8")
    assert (
        cli.main([
            "cover-letter",
            "verify-truth",
            "--source-cv",
            str(cv_path),
            "--draft",
            str(confirmed_draft),
            "--confirmed",
            str(confirmed_file),
            "--strict",
            "--as-of",
            "2026-11-21",
        ])
        == 0
    )
    conf_res = json.loads(capsys.readouterr().out)
    assert conf_res["passed"] is True
    assert any(item["type"] == "safe_acronym_expansion" for item in conf_res["info"])


@pytest.mark.parametrize("tpl_name", ["classic", "modern", "executive"])
def test_build_docx_all_bundled_templates_and_formats(tmp_path: Path, tpl_name: str, capsys: pytest.CaptureFixture[str]):
    cv_path = _write_sample_cv(tmp_path)
    md_draft = _write_faithful_md_draft(tmp_path / f"draft-{tpl_name}.md")
    out_docx = tmp_path / f"letter-{tpl_name}.docx"
    out_md = tmp_path / f"letter-{tpl_name}-clean.md"

    assert (
        cli.main([
            "cover-letter",
            "build-docx",
            "--from",
            str(md_draft),
            "--out",
            str(out_docx),
            "--out-md",
            str(out_md),
            "--template",
            tpl_name,
            "--source-cv",
            str(cv_path),
            "--strict",
            "--as-of",
            "2026-11-21",
        ])
        == 0
    )
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] is True
    assert res["template"] == tpl_name
    assert res["layout"]["tables"] == 0
    assert res["layout"]["textboxes"] == 0
    assert res["layout"]["multi_column_sections"] == 0
    assert res["truth"]["passed"] is True
    assert out_docx.is_file()
    assert out_md.is_file()

    # Check extracted DOCX content and round-trippable clean Markdown
    docx_text = cv.extract_cv(out_docx).text
    assert "Ada Lovelace" in docx_text
    assert "ASML" in docx_text
    assert "Reduced p95 latency by 35%" in docx_text
    parsed_roundtrip = builder.parse_markdown_cover_letter(out_md.read_text(encoding="utf-8"))
    assert parsed_roundtrip.company == "ASML"
    assert parsed_roundtrip.salutation == "Dear Elena Ruiz,"
    assert parsed_roundtrip.signoff == "Best regards,"
    assert len(parsed_roundtrip.highlights) == 2

    # Refuses overwrite unless --force
    assert cli.main(["cover-letter", "build-docx", "--from", str(md_draft), "--out", str(out_docx)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "output_exists"


def test_build_docx_json_strict_refusal_and_font_override(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    cv_path = _write_sample_cv(tmp_path)
    json_draft = tmp_path / "draft.json"
    json_draft.write_text(
        json.dumps({
            "candidate_name": "Ada Lovelace",
            "company": "Acme",
            "role": "Backend Engineer",
            "opening": "I built Rust and Kubernetes clusters for 10 years saving 95%.",
            "closing": "Thank you.",
        }),
        encoding="utf-8",
    )
    out_docx = tmp_path / "blocked.docx"
    out_md = tmp_path / "blocked.md"

    assert (
        cli.main([
            "cover-letter",
            "build-docx",
            "--from",
            str(json_draft),
            "--out",
            str(out_docx),
            "--out-md",
            str(out_md),
            "--source-cv",
            str(cv_path),
            "--strict",
            "--as-of",
            "2026-11-21",
        ])
        == 1
    )
    err_out = json.loads(capsys.readouterr().out)
    assert err_out["error"] == "truthfulness_failed"
    assert not out_docx.exists()
    assert not out_md.exists()

    # Honest JSON builds cleanly with custom --font
    honest_json = tmp_path / "honest.json"
    honest_json.write_text(
        json.dumps({
            "candidate_name": "Ada Lovelace",
            "company": "Acme",
            "opening": "At Example Corp I built Python microservices.",
            "paragraphs": ["I improved latency by 35% using Redis."],
            "closing": "Thank you for your consideration.",
            "signoff": "Sincerely,",
        }),
        encoding="utf-8",
    )
    ok_docx = tmp_path / "ok-arial.docx"
    assert cli.main(["cover-letter", "build-docx", "--from", str(honest_json), "--out", str(ok_docx), "--font", "Arial"]) == 0
    ok_out = json.loads(capsys.readouterr().out)
    assert ok_out["font"] == "Arial"
    assert "Arial" in ok_out["layout"]["fonts"]


def test_custom_template_split_runs_headers_multiline_and_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    cv_path = _write_sample_cv(tmp_path)
    draft = _write_faithful_md_draft(tmp_path / "draft.md")

    # 1. Starter round-trip: export modern template then fill via --template-file
    starters_dir = tmp_path / "starters"
    templates.export_starter_templates(starters_dir, template="modern")
    starter_tpl = starters_dir / "cover-letter-modern-template.docx"
    roundtrip_out = tmp_path / "roundtrip-custom.docx"
    assert (
        cli.main([
            "cover-letter",
            "build-docx",
            "--from",
            str(draft),
            "--out",
            str(roundtrip_out),
            "--template-file",
            str(starter_tpl),
            "--source-cv",
            str(cv_path),
            "--strict",
            "--as-of",
            "2026-11-21",
        ])
        == 0
    )
    capsys.readouterr()
    extracted = cv.extract_cv(roundtrip_out).text
    assert "{{" not in extracted
    assert "Ada Lovelace" in extracted
    assert "VP of Engineering" in extracted
    assert "Reduced p95 latency by 35%" in extracted

    # 2. Custom DOCX with split <w:r><w:t>, header1.xml, footer1.xml, empty optional {{subject}} removed, and {{body}} expanded
    custom_tpl = tmp_path / "split-custom.docx"
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}">'
        "<w:body>"
        '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{{cand</w:t></w:r><w:r><w:t>idate_name}}</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>{{recipient_title}}</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>To: {{company}} ({{role}})</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>{{salutation}}</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>{{body}}</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>{{signoff}}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    header_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:hdr xmlns:w="{W_NS}"><w:p><w:r><w:t>{{{{candidate_name}}}} | {{{{candidate_email}}}}</w:t></w:r></w:p></w:hdr>'
    )
    footer_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:ftr xmlns:w="{W_NS}"><w:p><w:r><w:t>Application dated {{{{date}}}}</w:t></w:r></w:p></w:ftr>'
    )
    custom_tpl.write_bytes(
        _make_custom_docx(doc_xml, extra={"word/header1.xml": header_xml, "word/footer1.xml": footer_xml})
    )
    minimal_doc = builder.CoverLetterDocument(
        candidate_name="Ada Lovelace",
        candidate_email="ada@example.com",
        date="2026-11-21",
        company="ASML",
        role="Backend Engineer",
        salutation="Dear Team,",
        opening="First opening paragraph.",
        paragraphs=["Second body paragraph."],
        highlights=["First key bullet."],
        closing="Closing statement.",
        signoff="Regards,",
    )
    rendered_bytes = templates.render_custom_template_docx_bytes(custom_tpl, minimal_doc)
    with zipfile.ZipFile(io.BytesIO(rendered_bytes)) as out_zip:
        out_doc = out_zip.read("word/document.xml").decode("utf-8")
        out_hdr = out_zip.read("word/header1.xml").decode("utf-8")
        out_ftr = out_zip.read("word/footer1.xml").decode("utf-8")
    assert "Ada Lovelace" in out_doc and "{{cand" not in out_doc
    assert "To: ASML (Backend Engineer)" in out_doc
    assert "{{recipient_title}}" not in out_doc
    assert "First opening paragraph." in out_doc
    assert "Second body paragraph." in out_doc
    assert "\u2022 First key bullet." in out_doc
    assert "Closing statement." in out_doc
    assert "Ada Lovelace | ada@example.com" in out_hdr
    assert "Application dated 2026-11-21" in out_ftr


def test_custom_template_security_rejections(tmp_path: Path):
    doc = builder.CoverLetterDocument(candidate_name="Ada", opening="Opening.")

    # Non-zip corrupt file
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="ZIP"):
        templates.render_custom_template_docx_bytes(bad, doc)

    # Path traversal entry
    slip = tmp_path / "slip.docx"
    slip.write_bytes(_make_custom_docx(f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r><w:t>{{{{opening}}}}</w:t></w:r></w:p></w:body></w:document>', extra={"../pwn.txt": "x"}))
    with pytest.raises(ValueError, match="traversal|unsafe"):
        templates.render_custom_template_docx_bytes(slip, doc)

    # XXE / DOCTYPE forbidden
    xxe = tmp_path / "xxe.docx"
    xxe.write_bytes(
        _make_custom_docx(
            '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            f'<w:document xmlns:w="{W_NS}"><w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>'
        )
    )
    with pytest.raises(ValueError, match="DOCTYPE"):
        templates.render_custom_template_docx_bytes(xxe, doc)

    # Missing word/document.xml
    no_doc = tmp_path / "nodoc.docx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
    no_doc.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="document.xml"):
        templates.render_custom_template_docx_bytes(no_doc, doc)

    # Duplicate ZIP entry
    dup = tmp_path / "dup.docx"
    buf2 = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buf2, "w") as zf:
            zf.writestr("word/document.xml", f'<w:document xmlns:w="{W_NS}"><w:body><w:p/></w:body></w:document>')
            zf.writestr("word/document.xml", f'<w:document xmlns:w="{W_NS}"><w:body><w:p/></w:body></w:document>')
    dup.write_bytes(buf2.getvalue())
    with pytest.raises(ValueError, match="duplicate"):
        templates.render_custom_template_docx_bytes(dup, doc)


def test_cover_letter_edge_cases_and_cli_error_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    cv_path = _write_sample_cv(tmp_path)

    # 1. Default --out path under $ULTRON_HOME/cover-letters/, --out-md overwrite guard, invalid CLI inputs
    draft_md = _write_faithful_md_draft(tmp_path / "default_out.md")
    assert cli.main(["cover-letter", "build-docx", "--from", str(draft_md)]) == 0
    built_default = json.loads(capsys.readouterr().out)
    assert Path(built_default["out_path"]).is_file()
    assert "cover-letters" in built_default["out_path"]

    existing_md = tmp_path / "already.md"
    existing_md.write_text("keep me", encoding="utf-8")
    assert (
        cli.main([
            "cover-letter",
            "build-docx",
            "--from",
            str(draft_md),
            "--out",
            str(tmp_path / "fresh.docx"),
            "--out-md",
            str(existing_md),
        ])
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error"] == "output_exists"

    # Missing draft / invalid --as-of in verify-truth & build-docx
    assert cli.main(["cover-letter", "verify-truth", "--source-cv", str(cv_path), "--draft", str(tmp_path / "nope.md")]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "not_found"
    assert cli.main(["cover-letter", "verify-truth", "--source-cv", str(cv_path), "--draft", str(draft_md), "--as-of", "bad"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_input"
    assert cli.main(["cover-letter", "build-docx", "--from", str(tmp_path / "missing.json")]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "not_found"

    # 2. Prepare with role:/company:/location: metadata in job-file + invalid tone/lang + missing job
    job_kv = tmp_path / "job_kv.txt"
    job_kv.write_text("Role: Staff Data Engineer\nCompany: Nova\nLocation: Berlin\nPython and FastAPI", encoding="utf-8")
    assert cli.main(["cover-letter", "prepare", "--cv", str(cv_path), "--job-file", str(job_kv), "--lang", "de"]) == 0
    kv_out = json.loads(capsys.readouterr().out)
    assert kv_out["target_job"]["title"] == "Staff Data Engineer"
    assert kv_out["target_job"]["company"] == "Nova"
    assert kv_out["target_job"]["location"] == "Berlin"
    assert kv_out["language"] == "de"

    assert cli.main(["cover-letter", "prepare", "--cv", str(cv_path), "--job-file", str(job_kv), "--lang", "zz"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "prepare_failed"

    # 3. Date-range inflation in cover letter truth check
    inflated_ranges = tmp_path / "inflated_ranges.md"
    inflated_ranges.write_text(
        "Experience\nSoftware Engineer | Example Corp | Jan 2016 - Jan 2025\nBuilt Python services.\n",
        encoding="utf-8",
    )
    res_inf = truth.verify_cover_letter_truth(cv_path, inflated_ranges, as_of=date(2026, 11, 21))
    assert any(v["type"] == "inflated_experience_duration" for v in res_inf["violations"])

    # 4. Standalone {{paragraphs}} and empty {{highlights}} pruning in custom template
    tpl_multi = tmp_path / "multi.docx"
    tpl_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>'
        "<w:p><w:r><w:t>{{opening}}</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>{{paragraphs}}</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>{{highlights}}</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>{{closing}}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    tpl_multi.write_bytes(_make_custom_docx(tpl_xml))
    rendered = templates.render_custom_template_docx_bytes(
        tpl_multi,
        builder.CoverLetterDocument(
            opening="Opening.",
            paragraphs=["Para one.", "Para two."],
            highlights=[],
            closing="Closing.",
        ),
    )
    with zipfile.ZipFile(io.BytesIO(rendered)) as zf:
        out_xml = zf.read("word/document.xml").decode("utf-8")
    assert "Para one." in out_xml and "Para two." in out_xml and "{{highlights}}" not in out_xml



def test_strict_verifiers_preserve_employer_and_outcome_attribution(tmp_path, capsys):
    source = tmp_path / "source.txt"
    source.write_text(
        "Ada Lovelace\nExperience\nEngineer | Atlas Systems | Jan 2021 - Jan 2022\n"
        "- Reduced costs by 20%.\nPlatform Engineer | Boreal Labs | Feb 2022 - Feb 2023\n"
        "- Ran Kubernetes clusters.\n",
        encoding="utf-8",
    )
    for command in ("cv", "cover-letter"):
        draft = tmp_path / "draft.md"
        prefix = (
            "Experience\nPlatform Engineer | Boreal Labs | Feb 2022 - Feb 2023\n"
            if command == "cv" else ""
        )
        bad_claim = "At Boreal Labs, I reduced costs by 20% with Kubernetes."
        draft.write_text(prefix + bad_claim, encoding="utf-8")
        args = [command, "verify-truth", "--source", str(source),
                "--optimized" if command == "cv" else "--draft", str(draft), "--strict"]
        assert cli.main(args) == 1
        failed = json.loads(capsys.readouterr().out)
        issue = next(row for row in failed["violations"] if row["type"] == "unsupported_claim_attribution")
        assert issue["token"] == bad_claim

        facts = tmp_path / "confirmed.txt"
        facts.write_text(bad_claim, encoding="utf-8")
        assert cli.main([*args, "--confirmed", str(facts)]) == 0
        capsys.readouterr()

        good = "At Atlas Systems, I cut operating expenses by 20%."
        draft.write_text(good, encoding="utf-8")
        assert cli.main(args) == 0
        capsys.readouterr()


def test_approximate_years_are_not_certified_as_exact_tenure(tmp_path, capsys):
    source = tmp_path / "approximate.txt"
    source.write_text(
        "Ada Lovelace\nExperience\nEngineer | Atlas Systems | 2022 - 2024\n"
        "- Built production systems.\nConsultant | Boreal Labs | 2022 - 2024\n",
        encoding="utf-8",
    )
    precise = tmp_path / "precise.txt"
    precise.write_text(
        "Ada Lovelace\nExperience\nEngineer | Atlas Systems | Jan 2022 - Dec 2023\n",
        encoding="utf-8",
    )
    ref = "2026-11-24"
    assert cli.main(["salary", "analyze", "--cv", str(source), "--no-runs", "--as-of", ref]) == 0
    experience = json.loads(capsys.readouterr().out)["experience"]
    assert experience["experience_precision"] == "approximate"
    assert experience["total_months"] == 24
    assert (experience["minimum_total_months"], experience["maximum_total_months"]) == (14, 36)
    assert experience["roles"][0]["start_precision"] == "year"
    assert cli.main(["salary", "analyze", "--cv", str(precise), "--no-runs", "--as-of", ref]) == 0
    exact = json.loads(capsys.readouterr().out)["experience"]
    assert exact["experience_precision"] == "month"
    assert exact["minimum_total_months"] == exact["maximum_total_months"] == 24

    draft = tmp_path / "draft.md"
    draft.write_text("I have 2 years of engineering experience.", encoding="utf-8")
    for command in ("cv", "cover-letter"):
        args = [command, "verify-truth", "--source", str(source),
                "--optimized" if command == "cv" else "--draft", str(draft), "--strict", "--as-of", ref]
        assert cli.main(args) == 1
        result = json.loads(capsys.readouterr().out)
        assert result["authorized_experience_month_bounds"] == [14, 36]
        assert any(row["type"] == "uncertain_experience_duration" for row in result["violations"])


def test_strict_verifiers_accept_aliases_heading_formats_and_unchanged_current_year(tmp_path, capsys):
    source = tmp_path / "source.txt"
    source.write_text(
        "Ada Lovelace\nExperience\nEngineer | Atlas Systems | Nov 2025 - 2026\n"
        "- Reduced costs by 20% using k8s.\n",
        encoding="utf-8",
    )
    draft = tmp_path / "draft.txt"
    draft.write_text(
        "Ada Lovelace\nExperience\nEngineer at Atlas Systems | Nov 2025 - 2026\n"
        "- Reduced costs by 20% using Kubernetes.\n",
        encoding="utf-8",
    )
    for command in ("cv", "cover-letter"):
        args = [command, "verify-truth", "--source", str(source),
                "--optimized" if command == "cv" else "--draft", str(draft), "--strict", "--as-of", "2026-01-01"]
        assert cli.main(args) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["authorized_experience_month_bounds"] == [3, 3]
        assert result["source_experience_months"] == 3


def test_strict_rewrites_keep_separate_metrics_and_authorized_approximate_roles(tmp_path, capsys):
    head = "Ada Lovelace\nExperience\nEngineer | Atlas Systems"
    first_role = " | Jan 2020 - Jan 2021\n"
    second_role = "Consultant | Boreal Labs | 2022 - 2024\n"
    source = tmp_path / "source.txt"
    source.write_text(
        head + " | London" + first_role
        + "- Reduced costs by 20% using Python.\n- Reduced latency by 30% using SQL.\n",
        encoding="utf-8",
    )
    draft = tmp_path / "draft.txt"
    draft.write_text(
        head + first_role + "- Reduced costs by 20% using Python and reduced latency by 30% using SQL.\n"
        + second_role,
        encoding="utf-8",
    )
    facts = tmp_path / "confirmed.txt"
    facts.write_text(second_role, encoding="utf-8")
    for command in ("cv", "cover-letter"):
        args = [command, "verify-truth", "--source", str(source),
                "--optimized" if command == "cv" else "--draft", str(draft), "--strict",
                "--as-of", "2026-11-24", "--confirmed", str(facts)]
        assert cli.main(args) == 0
        capsys.readouterr()

    source.write_text("Ada Lovelace\nExperience\nEngineer\nJan 2020 - Jan 2021\n- Reduced costs by 20%.\n",
                      encoding="utf-8")
    draft.write_text(head + first_role + "- Increased costs by 20%.\n", encoding="utf-8")
    for command in ("cv", "cover-letter"):
        args = [command, "verify-truth", "--source", str(source),
                "--optimized" if command == "cv" else "--draft", str(draft), "--strict"]
        assert cli.main(args) == 1
        assert any(item["type"] == "unsupported_claim_attribution"
                   for item in json.loads(capsys.readouterr().out)["violations"])

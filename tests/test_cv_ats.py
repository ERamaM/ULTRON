"""Tests for deterministic ATS CV formatting audit, security bounds, and target modes."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ultron import cli
from ultron.core.models import ExtractedJob, FinalRow
from ultron.cv import ats as cv_ats
from ultron.cv import ats_target
from ultron.cv import docx_reader as cv_docx
from ultron.cv import extract as cv
from ultron.cv import worker as cv_worker
from ultron.profiles import store as profiles
from ultron.storage import runs

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
SYNTHETIC = Path(__file__).parent / "fixtures" / "cv" / "synthetic"


def _document(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}" xmlns:mc="{MC}"><w:body>{body}</w:body></w:document>'
    )


def _paragraph(*runs: str) -> str:
    return "<w:p>" + "".join(f'<w:r><w:t xml:space="preserve">{t}</w:t></w:r>' for t in runs) + "</w:p>"


def _build_docx(document_xml: str, *, extra: dict[str, str | bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document_xml)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buf.getvalue()


def _two_col_pdf_from_fixture(name: str, *, right_x: float = 360) -> bytes:
    spec = json.loads((SYNTHETIC / name).read_text(encoding="utf-8"))
    lines: list[tuple[float, float, str]] = []
    y = 800.0
    for text in spec["header"]:
        lines.append((50, y, text))
        y -= 20
    for left, right in zip(spec["left"], spec["right"] + [""] * len(spec["left"]), strict=False):
        if left:
            lines.append((50, y, left))
        if right:
            lines.append((right_x, y, right))
        y -= 14
    return _custom_pdf(lines)


def _custom_pdf(
    lines: list[tuple[float, float, str]],
    *,
    font: str = "Helvetica",
    producer: str = "",
    creator: str = "",
    with_image: bool = False,
) -> bytes:
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>"]
    stream = "\n".join(
        f"BT /F1 10 Tf 1 0 0 1 {x} {y} Tm ({text.replace('(', '\\(').replace(')', '\\)')}) Tj ET"
        for x, y, text in lines
    ).encode("cp1252")
    xobj_ref = " /XObject << /Im1 7 0 R >>" if with_image else ""
    objects.append(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        f"/Resources << /Font << /F1 5 0 R >>{xobj_ref} >> >>".encode()
    )
    objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    objects.append(f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} /Encoding /WinAnsiEncoding >>".encode())
    info_num = None
    if producer or creator:
        info_num = len(objects) + 1
        parts = []
        if producer:
            parts.append(f"/Producer ({producer})")
        if creator:
            parts.append(f"/Creator ({creator})")
        objects.append(f"<< {' '.join(parts)} >>".encode())
    if with_image:
        while len(objects) < 6:
            objects.append(b"<< >>")
        objects.append(b"<< /Type /XObject /Subtype /Image /Width 10 /Height 10 >>")

    chunks = [b"%PDF-1.4\n"]
    offsets = []
    for num, obj in enumerate(objects, start=1):
        offsets.append(sum(len(c) for c in chunks))
        chunks.append(f"{num} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = sum(len(c) for c in chunks)
    table = ["xref", f"0 {len(objects) + 1}", "0000000000 65535 f "] + [f"{o:010d} 00000 n " for o in offsets]
    info_tail = f" /Info {info_num} 0 R" if info_num else ""
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{info_tail} >>\nstartxref\n{xref}\n%%EOF\n"
    return b"".join(chunks) + ("\n".join(table) + "\n").encode() + trailer.encode()


def _clean_cv_paragraphs() -> list[str]:
    return [
        "Jane Example",
        "jane@example.com | +34 600 111 222 | https://linkedin.com/in/jane",
        "Summary",
        "Senior Data Engineer with 6 years building Python, SQL, and Spark pipelines.",
        "Experience",
        "Senior Data Engineer | Acme Labs | Jan 2021 - Present",
        "- Built Spark and Airflow pipelines reducing latency by 35%.",
        "- Mentored 4 engineers and deployed Kubernetes services on AWS.",
        "- Automated CI/CD checks cutting release time by 50%.",
        "Skills",
        "Python, SQL, Spark, Airflow, Kubernetes, AWS, Terraform",
        "Education",
        "MSc Computer Science | 2015 - 2017",
    ]


def test_format_preference_matrix_clean_docx_beats_pdf_and_discourages_latex(tmp_path):
    paras = _clean_cv_paragraphs()
    clean_docx = tmp_path / "clean.docx"
    clean_docx.write_bytes(_build_docx(_document("".join(_paragraph(p) for p in paras))))

    clean_pdf = tmp_path / "clean.pdf"
    clean_pdf.write_bytes(_custom_pdf([(50, 780 - 16 * i, p) for i, p in enumerate(paras)], font="Helvetica"))

    latex_producer_pdf = tmp_path / "latex_prod.pdf"
    latex_producer_pdf.write_bytes(
        _custom_pdf([(50, 780 - 16 * i, p) for i, p in enumerate(paras)], producer="pdfTeX-1.40.25", creator="LaTeX with hyperref")
    )

    latex_font_pdf = tmp_path / "latex_font.pdf"
    latex_font_pdf.write_bytes(
        _custom_pdf([(50, 780 - 16 * i, p) for i, p in enumerate(paras)], font="LMRoman10-Regular")
    )

    design_pdf = tmp_path / "canva.pdf"
    design_pdf.write_bytes(
        _custom_pdf([(50, 780 - 16 * i, p) for i, p in enumerate(paras)], creator="Canva")
    )

    tex_file = tmp_path / "cv.tex"
    tex_file.write_text(
        r"\section{Experience} \textbf{Senior Data Engineer} Jan 2021 - Present",
        encoding="utf-8",
    )

    scanned_pdf = tmp_path / "scanned.pdf"
    scanned_pdf.write_bytes(_custom_pdf([(50, 780, "Scan")], with_image=True))

    a_docx = cv_ats.audit_cv(clean_docx, write_report=False)
    a_pdf = cv_ats.audit_cv(clean_pdf, write_report=False)
    a_lprod = cv_ats.audit_cv(latex_producer_pdf, write_report=False)
    a_lfont = cv_ats.audit_cv(latex_font_pdf, write_report=False)
    a_design = cv_ats.audit_cv(design_pdf, write_report=False)
    a_tex = cv_ats.audit_cv(tex_file, write_report=False)
    a_scan = cv_ats.audit_cv(scanned_pdf, write_report=False)

    assert a_docx["format_tier"] == "preferred"
    assert a_pdf["format_tier"] == "acceptable"
    assert a_lprod["format_tier"] == "discouraged"
    assert a_lfont["format_tier"] == "discouraged"
    assert a_design["format_tier"] == "discouraged"
    assert a_tex["format_tier"] == "unsuitable"
    assert a_scan["format_tier"] == "unsuitable"

    assert any(f["id"] == "latex_pdf_detected" for f in a_lprod["findings"])
    assert any(f["id"] == "latex_pdf_detected" for f in a_lfont["findings"])
    assert any(f["id"] == "design_tool_pdf_detected" for f in a_design["findings"])
    assert any(f["id"] == "latex_source_format" for f in a_tex["findings"])
    assert any(f["id"] == "scanned_or_sparse_pdf" for f in a_scan["findings"])
    assert a_docx["subscores"]["format_and_parseability"] > a_pdf["subscores"]["format_and_parseability"]
    assert a_pdf["subscores"]["format_and_parseability"] > a_lprod["subscores"]["format_and_parseability"]
    assert a_docx["score"] > a_lprod["score"]


def test_messy_docx_structural_flags_and_two_column_pdf(tmp_path):
    body = (
        '<w:tbl><w:tr><w:tc>' + _paragraph("Jane Doe") + "</w:tc></w:tr></w:tbl>"
        + '<w:p><w:r><w:drawing><w:txbxContent>' + _paragraph("Skills: Python ★") + "</w:txbxContent></w:drawing></w:r></w:p>"
        + '<w:p><w:del><w:r><w:delText>Old</w:delText></w:r></w:del><w:r><w:t>Experience</w:t></w:r></w:p>'
        + '<w:sectPr><w:cols w:num="2"/></w:sectPr>'
    )
    header = f'<w:hdr xmlns:w="{W}">' + _paragraph("jane@example.com | +34 600 000 000") + "</w:hdr>"
    messy_docx = tmp_path / "messy.docx"
    messy_docx.write_bytes(
        _build_docx(
            _document(body),
            extra={"word/header1.xml": header, "word/comments.xml": "<w:comments/>", "word/media/pic.png": b"PNG"},
        )
    )
    audit = cv_ats.audit_cv(messy_docx, write_report=False)
    assert audit["format_tier"] == "risky"
    ids = {f["id"] for f in audit["findings"]}
    assert {
        "tables_detected",
        "textboxes_detected",
        "multi_column_layout",
        "contact_in_header_footer",
        "embedded_images",
        "word_review_artifacts",
        "decorative_glyphs",
    } <= ids

    two_col_pdf = tmp_path / "two_col.pdf"
    two_col_pdf.write_bytes(_two_col_pdf_from_fixture("es_two_column.json", right_x=360))
    pdf_audit = cv_ats.audit_cv(two_col_pdf, write_report=False)
    assert pdf_audit["format_tier"] == "risky"
    assert any(f["id"] == "multi_column_layout" for f in pdf_audit["findings"])


def test_security_bounds_on_docx_header_and_cyclic_pdf_xobjects(tmp_path):
    bad_header = f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><w:hdr xmlns:w="{W}"/>'
    evil_docx = _build_docx(_document(_paragraph("Body")), extra={"word/header1.xml": bad_header})
    with pytest.raises(cv.CvExtractionError):
        cv_docx.inspect_docx(evil_docx)

    # Cyclic /Form XObject in PDF resources must terminate cleanly
    class CyclicRef:
        def __init__(self, target):
            self._target = target
            self.indirect_reference = SimpleNamespace(idnum=99, generation=0)

        def get_object(self):
            return self._target

    form_res: dict = {}
    form_obj = {"/Subtype": "/Form", "/Resources": form_res}
    ref = CyclicRef(form_obj)
    form_res["/XObject"] = {"/Self": ref}
    page = {"/Resources": form_res}
    fonts: set[str] = set()
    assert cv_worker._walk_page_resources(page, fonts) == 0


def test_cli_cv_audit_target_modes_and_redacted_report(tmp_path, capsys, monkeypatch):
    docx_file = tmp_path / "candidate.docx"
    docx_file.write_bytes(_build_docx(_document("".join(_paragraph(p) for p in _clean_cv_paragraphs()))))

    profiles.save_profile({
        "slug": "data-eng",
        "name": "Data Eng",
        "criteria": {
            "role_focus_terms": ["data engineer"],
            "target_titles": ["Senior Data Engineer"],
            "specialties": ["Spark", "Airflow", "dbt"],
        },
    })

    # 1. Profile target mode
    assert cli.main(["cv", "audit", str(docx_file), "--profile", "data-eng"]) == 0
    out_prof = json.loads(capsys.readouterr().out)
    assert out_prof["target"]["mode"] == "profile"
    assert "Spark" in out_prof["keyword_alignment"]["matched"]
    assert "dbt" in out_prof["keyword_alignment"]["missing"]
    assert Path(out_prof["report_path"]).is_file()

    # 2. Job file mode with secret token redaction in target metadata
    fake_token = "sk-" + "ant-" + "1234567890abcdef"
    job_file = tmp_path / "job.txt"
    job_file.write_text(f"Staff Data Engineer ({fake_token})\nMust know Python, Kafka, and dbt.", encoding="utf-8")
    assert cli.main(["cv", "audit", str(docx_file), "--job-file", str(job_file)]) == 0
    out_jf = json.loads(capsys.readouterr().out)
    assert out_jf["target"]["mode"] == "job"
    assert fake_token not in out_jf["target"]["title"]
    assert "[REDACTED]" in out_jf["target"]["title"]
    assert "Kafka" in out_jf["keyword_alignment"]["missing"]

    # 3. Local run job + run aggregate mode
    rid = runs.new_run("data-eng", "balanced")
    folder = runs.run_dir(rid)
    runs.write_jsonl(folder / "postings.jsonl", [
        {
            "identity_key": "greenhouse:acme:42",
            "job": ExtractedJob(
                apply_url="https://boards.greenhouse.io/acme/jobs/42",
                domain="boards.greenhouse.io",
                title="Senior Data Engineer",
                company="Acme",
                body_text="Requires Python, Spark, Airflow, and Snowflake.",
            ).model_dump(mode="json"),
        }
    ])
    (folder / "results.json").write_text(
        json.dumps({
            "rows": [
                FinalRow(
                    title="Senior Data Engineer",
                    company="Acme",
                    apply_url="https://boards.greenhouse.io/acme/jobs/42",
                    location="Remote EU",
                    remote_policy="remote_eu",
                    eligibility="high",
                    eligibility_evidence="Remote EU",
                    fit_signals=["Spark", "Snowflake"],
                    freshness="1d",
                ).model_dump(mode="json")
            ]
        }),
        encoding="utf-8",
    )
    runs.mark_stage(rid, "fetch")

    assert cli.main(["cv", "audit", str(docx_file), "--job", "greenhouse:acme:42", "--run", rid]) == 0
    out_job = json.loads(capsys.readouterr().out)
    assert out_job["target"]["source"] == "local_run"
    assert "Snowflake" in out_job["keyword_alignment"]["missing"]

    assert cli.main(["cv", "audit", str(docx_file), "--run", rid]) == 0
    out_agg = json.loads(capsys.readouterr().out)
    assert out_agg["target"]["mode"] == "run_aggregate"
    assert "Snowflake" in out_agg["keyword_alignment"]["missing"]

    # 4. Live URL fetch mode (mocked client_for_run + get_posting_page)
    monkeypatch.setattr(ats_target, "client_for_run", lambda _run: object())
    monkeypatch.setattr(
        ats_target,
        "get_posting_page",
        lambda _c, url: (200, url, "<html><head><title>ML Engineer at Beta</title></head><body><h1>ML Engineer</h1><p>Need PyTorch and Python.</p></body></html>"),
    )
    assert cli.main(["cv", "audit", str(docx_file), "--job", "https://boards.greenhouse.io/beta/jobs/999"]) == 0
    out_url = json.loads(capsys.readouterr().out)
    assert out_url["target"]["source"] == "fetched_url"
    assert "PyTorch" in out_url["keyword_alignment"]["missing"]

    # Usage error when passing both --job and --job-file
    assert cli.main(["cv", "audit", str(docx_file), "--job", "x", "--job-file", str(job_file)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "usage"


def test_audit_cv_run_reads_heuristic_fit_signals_from_kept_jsonl_before_finalize(tmp_path):
    docx_file = tmp_path / "candidate.docx"
    docx_file.write_bytes(_build_docx(_document("".join(_paragraph(p) for p in _clean_cv_paragraphs()))))

    profiles.save_profile({
        "slug": "ml-eu",
        "name": "ML EU",
        "criteria": {
            "role_focus_terms": ["ml engineer"],
            "target_titles": ["ML Engineer"],
        },
    })
    rid = runs.new_run("ml-eu", "balanced")
    folder = runs.run_dir(rid)
    runs.write_jsonl(folder / "kept.jsonl", [
        {
            "identity_key": "greenhouse:beta:7",
            "job": {"title": "ML Engineer", "company": "Beta"},
            "heuristic": {"fit_signals": ["PyTorch", "Kubernetes", "Airflow"], "matched_requirements": []},
        }
    ])
    runs.mark_stage(rid, "fetch")
    runs.mark_stage(rid, "filter")

    report = cv_ats.audit_cv(docx_file, run_id=rid)
    assert report["target"]["mode"] == "run_aggregate"
    assert report["target"]["kept_rows_analyzed"] == 1
    align = report["keyword_alignment"]
    assert align["terms_checked"] == 3
    all_terms = set(align["matched"]) | set(align["missing"])
    assert {"PyTorch", "Kubernetes", "Airflow"} <= all_terms
    assert report["subscores"]["target_alignment"] is not None


def test_offline_audit_finds_older_recorded_vacancy_after_latest_capture_is_removed(tmp_path, monkeypatch, capsys):
    from contextlib import closing

    from ultron.storage import ledger

    profiles.install_example()
    url = "https://boards.greenhouse.io/acme/jobs/42"
    old = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    recent = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    vacancy = FinalRow(
        title="Senior Data Engineer", company="Acme", apply_url=url, location="Remote EU",
        remote_policy="remote_eu", eligibility="high", eligibility_evidence="", fit_signals=[], freshness="",
    )
    with closing(ledger.connect()) as conn:
        (key,) = ledger.record_occurrences(conn, old, [vacancy])
        ledger.record_occurrences(conn, recent, [vacancy])
    posting = ExtractedJob(
        title=vacancy.title, company=vacancy.company, apply_url=url, domain="boards.greenhouse.io",
        body_text="Required skills: Snowflake and Python.",
    )
    runs.write_jsonl(runs.run_dir(old) / "postings.jsonl", [
        {"identity_key": key, "job": posting.model_dump(mode="json")},
    ])
    runs.mark_stage(old, "fetch")
    (runs.run_dir(recent) / "run.json").unlink()
    for _ in range(7):
        unrelated = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
        runs.mark_stage(unrelated, "fetch")
        runs.write_jsonl(runs.run_dir(unrelated) / "postings.jsonl", [{
            "identity_key": "unrelated",
            "job": posting.model_copy(update={"apply_url": url.replace("/42", "/99")}).model_dump(mode="json"),
        }])

    def fail_network(*args, **kwargs):
        pytest.fail("The audit attempted a network fetch")

    monkeypatch.setattr(ats_target, "_fetch_job_url", fail_network)
    cv_path = SYNTHETIC / "en_skills.txt"
    assert cli.main(["cv", "audit", str(cv_path), "--job", url, "--no-fetch"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["target"]["run_id"] == old
    assert output["target"]["identity_key"] == key
    assert "Snowflake" in output["keyword_alignment"]["missing"]

    (runs.run_dir(old) / "postings.jsonl").unlink()
    assert cli.main(["cv", "audit", str(cv_path), "--job", url, "--no-fetch"]) == 2
    assert "no surviving local posting snapshot" in capsys.readouterr().out


@pytest.mark.parametrize("posting", [
    "SQL or Python is required. Tableau is desirable. Our company uses Redis. Docker is not required.",
    "SQL o Python es obligatorio. Tableau es deseable. Nuestra empresa usa Redis. Docker no es obligatorio.",
])
def test_audit_prioritizes_requirements_and_preserves_alternatives(tmp_path, capsys, posting):
    job = tmp_path / "posting.txt"
    job.write_text(posting, encoding="utf-8")
    output = {}
    for skill in ("SQL", "Tableau"):
        cv = tmp_path / f"{skill}.txt"
        cv.write_text(f"Ada Lovelace\nSkills\n{skill}\n", encoding="utf-8")
        assert cli.main(["cv", "audit", str(cv), "--job-file", str(job)]) == 0
        output[skill] = json.loads(capsys.readouterr().out)

    sql, tableau = output["SQL"], output["Tableau"]
    assert sql["subscores"]["target_alignment"] > tableau["subscores"]["target_alignment"]
    evidence = sql["keyword_alignment"]["criteria"]
    required = [row for row in evidence if row["category"] == "required"]
    assert len(required) == 1
    assert set(required[0]["alternatives"]) == {"SQL", "Python"}
    assert required[0]["matched"] == ["SQL"]
    assert required[0]["evidence"] in posting
    assert any(row["category"] == "preferred" and row["alternatives"] == ["Tableau"] for row in evidence)
    assert any(row["category"] == "unspecified" and row["alternatives"] == ["Docker"] for row in evidence)
    assert any(row["category"] == "unspecified" and row["alternatives"] == ["Redis"] for row in evidence)
    assert "Required · matched" in Path(sql["report_path"]).read_text(encoding="utf-8")

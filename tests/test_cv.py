from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ultron import cli
from ultron.cv import extract as cv
from ultron.cv.worker import extract_pdf_pages_text
from ultron.profiles import store as profiles

FIXTURES = Path(__file__).parent / "fixtures" / "cv"


def build_pdf(page_texts: list[str]) -> bytes:
    """Tiny hand-written PDF: one page per entry (an empty string gives a blank page)."""
    n = len(page_texts)
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects.append(f"<< /Type /Pages /Count {n} /Kids [{kids}] >>".encode())
    font = 3 + 2 * n
    for i, text in enumerate(page_texts):
        stream = f"BT /F1 12 Tf 72 72 Td ({text}) Tj ET\n".encode("latin-1") if text else b""
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    chunks = [b"%PDF-1.4\n"]
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(sum(len(c) for c in chunks))
        chunks.append(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = sum(len(c) for c in chunks)
    lines = ["xref", f"0 {len(objects) + 1}", "0000000000 65535 f "] + [f"{o:010d} 00000 n " for o in offsets]
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return b"".join(chunks) + ("\n".join(lines) + "\n").encode() + trailer.encode()


def test_checked_in_pdfs_match_the_generator():
    # The PDFs are committed and read-only during the run: parallel workers must never rewrite shared files.
    assert (FIXTURES / "sample.pdf").read_bytes() == build_pdf(["Hello CV"])
    assert (FIXTURES / "blank.pdf").read_bytes() == build_pdf([""])


def _write(tmp_path, name, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_text_and_markdown_read_directly(tmp_path):
    path = _write(tmp_path, "cv.txt", b"  Python\r\n\r\n\r\n  Data   Engineer  ")
    assert cv.extract_cv_text(path) == "Python\n\nData Engineer"
    assert cv.extract_cv_text(_write(tmp_path, "cv.md", b"# Title")) == "# Title"


@pytest.mark.parametrize(
    ("content", "expected"),
    [(b"\xef\xbb\xbfEspa\xc3\xb1a", "España"), ("España".encode("utf-16"), "España"), (b"caf\xe9", "café")],
)
def test_plain_text_decoding(tmp_path, content, expected):
    assert cv.extract_cv_text(_write(tmp_path, "cv.txt", content)) == expected


def test_character_boundary_rejects_not_cuts(tmp_path):
    path = _write(tmp_path, "cv.txt", b"a" * 21)
    assert cv.extract_cv_text(_write(tmp_path, "ok.txt", b"a" * 20), max_chars=20) == "a" * 20
    with pytest.raises(cv.CvTextTooLong) as raised:
        cv.extract_cv_text(path, max_chars=20)
    assert (raised.value.length, raised.value.limit, raised.value.code) == (21, 20, "too_long")
    assert "No partial profile" in str(raised.value)


def test_unsupported_and_empty(tmp_path):
    with pytest.raises(cv.CvExtractionError) as raised:
        cv.extract_cv_text(_write(tmp_path, "cv.doc", b"data"))
    assert raised.value.code == "unsupported_type"
    with pytest.raises(cv.CvExtractionError) as raised:
        cv.extract_cv_text(_write(tmp_path, "e.txt", b" \n\t "))
    assert raised.value.code == "no_extractable_text"
    with pytest.raises(cv.CvExtractionError) as raised:
        cv.extract_cv_text(tmp_path / "missing.txt")
    assert raised.value.code == "unsupported_type"


def test_oversized_file_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(cv, "MAX_CV_BYTES", 4)
    with pytest.raises(cv.CvExtractionError, match="too large") as raised:
        cv.extract_cv_text(_write(tmp_path, "cv.txt", b"12345"))
    assert raised.value.code == "too_long"


def test_real_subprocess_extracts_sample_pdf():
    assert cv.extract_cv_text(FIXTURES / "sample.pdf") == "Hello CV"


def test_blank_pdf_error_code():
    with pytest.raises(cv.CvExtractionError) as raised:
        cv.extract_cv_text(FIXTURES / "blank.pdf")
    assert raised.value.code == "no_extractable_text"


def test_too_many_pages(tmp_path):
    path = _write(tmp_path, "two.pdf", build_pdf(["One", "Two"]))
    assert cv.extract_cv_text(path) == "One\nTwo"
    with pytest.raises(cv.CvPdfTooManyPages) as raised:
        cv.extract_cv_text(path, max_pages=1)
    assert (raised.value.limit, raised.value.code) == (1, "too_many_pages")


def test_pdf_over_character_limit_via_worker():
    with pytest.raises(cv.CvTextTooLong):
        cv.extract_cv_text(FIXTURES / "sample.pdf", max_chars=3)


def test_real_subprocess_timeout_is_a_hard_failure():
    with pytest.raises(cv.CvPdfTimeout, match="PDF extraction timed out") as raised:
        cv.extract_cv_text(FIXTURES / "sample.pdf", timeout=0.001)
    assert raised.value.code == "timeout"


def test_corrupt_pdf_raises_extraction_error(tmp_path):
    with pytest.raises(cv.CvExtractionError, match="Could not extract text from PDF"):
        cv.extract_cv_text(_write(tmp_path, "bad.pdf", b"%PDF-1.7\nnot-a-pdf"))


def test_worker_page_budget_rejects_before_extracting_excess_page(monkeypatch):
    extracted = []

    class Page:
        def __init__(self, n):
            self.n = n

        def extract_text(self):
            extracted.append(self.n)
            return "short"

    class Reader:
        def __init__(self, _stream):
            self.pages = [Page(n) for n in range(5)]

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=Reader))
    with pytest.raises(cv.CvPdfTooManyPages):
        extract_pdf_pages_text(b"%PDF", max_pages=3, max_chars=100)
    assert extracted == []


def test_worker_stops_once_character_budget_exhausted(monkeypatch):
    extracted = []

    class Page:
        def __init__(self, n):
            self.n = n

        def extract_text(self):
            extracted.append(self.n)
            return "x" * 11

    class Reader:
        def __init__(self, _stream):
            self.pages = [Page(1), Page(2), Page(3)]

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=Reader))
    with pytest.raises(cv.CvTextTooLong):
        extract_pdf_pages_text(b"%PDF", max_pages=9, max_chars=10)
    assert extracted == [1]


def test_missing_pypdf_is_dependency_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)  # makes ``import pypdf`` raise ImportError
    with pytest.raises(cv.CvDependencyMissing) as raised:
        extract_pdf_pages_text(b"%PDF", max_pages=1, max_chars=10)
    assert raised.value.code == "dependency_missing"


def test_subprocess_runner_translates_worker_errors(monkeypatch):
    payloads = iter(
        [
            {"error": "too_long", "length": 12, "limit": 10},
            {"error": "too_many_pages", "limit": 3},
            {"error": "dependency_missing"},
            {"error": "extract_failed"},
        ]
    )

    def fake_run(args, **kwargs):
        assert args == [sys.executable, *cv._pdf_worker_flags(), "-m", "ultron.cv.worker", "2", "10"]
        assert kwargs["shell"] is False
        assert kwargs["input"] == b"%PDF-x"
        assert kwargs["timeout"] == 7.5
        return SimpleNamespace(returncode=0, stdout=json.dumps(next(payloads)).encode(), stderr=b"")

    monkeypatch.setattr("ultron.cv.extract.subprocess.run", fake_run)

    def run():
        return cv.subprocess_pdf_runner(b"%PDF-x", max_pages=2, max_chars=10, timeout_seconds=7.5)

    with pytest.raises(cv.CvTextTooLong):
        run()
    with pytest.raises(cv.CvPdfTooManyPages):
        run()
    with pytest.raises(cv.CvDependencyMissing):
        run()
    with pytest.raises(cv.CvExtractionError, match="Could not extract"):
        run()


def test_subprocess_runner_nonzero_exit_raises_worker_failed_with_stderr_tail(monkeypatch, tmp_path, capsys):
    long_err = ("x" * 500 + "\nImportError: cannot find pypdf\n").encode("utf-8")

    def fake_run(args, **kwargs):
        assert "-I" not in args
        assert "-s" in args
        assert "-P" in args
        return SimpleNamespace(returncode=1, stdout=b"", stderr=long_err)

    monkeypatch.setattr("ultron.cv.extract.subprocess.run", fake_run)
    with pytest.raises(cv.CvExtractionError) as raised:
        cv.subprocess_pdf_runner(b"%PDF-x", max_pages=2, max_chars=10, timeout_seconds=5)
    assert raised.value.code == "worker_failed"
    assert "ImportError: cannot find pypdf" in str(raised.value)
    assert len(str(raised.value)) < 360

    pdf = _write(tmp_path, "cv.pdf", b"%PDF-1.4")
    assert cli.main(["cv", "extract", str(pdf)]) != 0
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "worker_failed"
    assert "ImportError: cannot find pypdf" in out["detail"]


def test_real_subprocess_isolates_cwd_shadow_modules(tmp_path, monkeypatch):
    (tmp_path / "pypdf.py").write_text('raise RuntimeError("shadowed pypdf")\n', encoding="utf-8")
    (tmp_path / "ultron.py").write_text('raise RuntimeError("shadowed ultron")\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cv.extract_cv_text(FIXTURES / "sample.pdf") == "Hello CV"


def test_cli_cv_extract_writes_file_not_stdout(tmp_path, capsys):
    src = tmp_path / "me.txt"
    src.write_text("Secret CV body", encoding="utf-8")
    assert cli.main(["cv", "extract", str(src)]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "Secret CV body" not in out
    assert (payload["chars"], payload["pages"]) == (14, 1)
    assert Path(payload["path"]).read_text(encoding="utf-8") == "Secret CV body"
    assert Path(payload["path"]).name == "me.txt.txt"


def test_cli_cv_extract_error_json(tmp_path, capsys):
    assert cli.main(["cv", "extract", str(_write(tmp_path, "a.odt", b"x"))]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "unsupported_type"


@pytest.mark.parametrize("content", [b"\xff\xfeH\x00i\x00\x00", b"\xff\xfe\x00\xd8"])
def test_malformed_utf16_is_a_typed_error(tmp_path, capsys, content):
    src = _write(tmp_path, "bad.txt", content)  # BOM with an odd byte count / a lone surrogate
    with pytest.raises(cv.CvExtractionError) as err:
        cv.extract_cv(src)
    assert err.value.code == "no_extractable_text"
    assert cli.main(["cv", "extract", str(src)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "no_extractable_text"


def _profile(slug="mi-perfil"):
    return {"slug": slug, "name": "Mi perfil", "criteria": {"target_titles": ["Ingeniero de datos"]}}


def test_save_profile_writes_and_validates():
    path = profiles.save_profile(_profile())
    assert path.name == "mi-perfil.yaml"
    assert profiles.load_profile("mi-perfil")["name"] == "Mi perfil"
    with pytest.raises(profiles.ProfileInvalidError):
        profiles.save_profile({"slug": "x"})


def test_save_profile_refuses_overwrite():
    profiles.save_profile(_profile())
    with pytest.raises(profiles.ProfileExistsError):
        profiles.save_profile(_profile())
    profiles.save_profile(_profile() | {"name": "Otro"}, force=True)
    assert profiles.load_profile("mi-perfil")["name"] == "Otro"


def test_cli_profile_save(tmp_path, capsys):
    src = tmp_path / "p.yaml"
    src.write_text(yaml.safe_dump(_profile(), allow_unicode=True), encoding="utf-8")
    assert cli.main(["profile", "save", "--from", str(src)]) == 0
    assert json.loads(capsys.readouterr().out)["slug"] == "mi-perfil"
    assert cli.main(["profile", "save", "--from", str(src)]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "profile_exists"
    assert cli.main(["profile", "save", "--from", str(src), "--force"]) == 0
    capsys.readouterr()
    src.write_text("slug: x\n", encoding="utf-8")
    assert cli.main(["profile", "save", "--from", str(src)]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "profile_invalid"


def test_cli_cv_extract_reports_absolute_source_cv(tmp_path, capsys, monkeypatch):
    (tmp_path / "me.txt").write_text("body", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["cv", "extract", "me.txt"]) == 0
    assert json.loads(capsys.readouterr().out)["source_cv"] == str((tmp_path / "me.txt").resolve())


def test_validate_source_cv_types():
    assert profiles.validate_profile(_profile() | {"source_cv": "/x/cv.pdf"}) == []
    assert profiles.validate_profile(_profile() | {"source_cv": None}) == []
    assert profiles.validate_profile(_profile() | {"source_cv": ""}) == []
    assert any("source_cv" in e for e in profiles.validate_profile(_profile() | {"source_cv": 3}))
    assert any("source_cv" in e for e in profiles.validate_profile(_profile() | {"source_cv": ["a"]}))


def _save_yaml(tmp_path):
    src = tmp_path / "p.yaml"
    src.write_text(yaml.safe_dump(_profile(), allow_unicode=True), encoding="utf-8")
    return src


def test_cli_profile_save_source_cv(tmp_path, capsys):
    cv_file = tmp_path / "me.pdf"
    cv_file.write_bytes(b"x")
    assert cli.main(["profile", "save", "--from", str(_save_yaml(tmp_path)), "--source-cv", str(cv_file)]) == 0
    assert json.loads(capsys.readouterr().out)["slug"] == "mi-perfil"
    assert profiles.load_profile("mi-perfil")["source_cv"] == str(cv_file.resolve())
    assert cli.main(["profile", "show", "mi-perfil"]) == 0
    assert json.loads(capsys.readouterr().out)["profile"]["source_cv"] == str(cv_file.resolve())
    assert cli.main(["profile", "list"]) == 0
    listed = {p["slug"]: p for p in json.loads(capsys.readouterr().out)["profiles"]}
    assert listed["mi-perfil"]["source_cv"] == str(cv_file.resolve())
    assert "source_cv" not in listed[profiles.EXAMPLE_SLUG]


def test_cli_profile_save_source_cv_missing(tmp_path, capsys):
    assert cli.main(["profile", "save", "--from", str(_save_yaml(tmp_path)),
                     "--source-cv", str(tmp_path / "nope.pdf")]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "source_cv_not_found"
    with pytest.raises(FileNotFoundError):
        profiles.load_profile("mi-perfil")


def test_run_snapshot_keeps_source_cv():
    from ultron.storage import runs

    profiles.save_profile(_profile() | {"source_cv": "/x/cv.pdf"})
    run_id = runs.new_run("mi-perfil", "balanced")
    assert runs.load_run(run_id)["profile_snapshot"]["source_cv"] == "/x/cv.pdf"


def test_regression_mixed_pdf_reports_physical_pages(tmp_path, capsys):
    path = _write(tmp_path, "mixed.pdf", build_pdf(["Jane Example", "", "Experience", ""]))
    assert cli.main(["cv", "extract", str(path)]) == 0
    extracted = json.loads(capsys.readouterr().out)
    assert cli.main(["cv", "audit", str(path)]) == 0
    audited = json.loads(capsys.readouterr().out)
    assert extracted["pages"] == audited["pages"] == audited["format_metadata"]["page_count"] == 4
    assert audited["format_metadata"]["chars_per_page"] == [12, 0, 10, 0]
    assert any(f["id"] == "page_length_excessive" for f in audited["findings"])



def test_cli_extract_latex_uses_shared_source_parser(tmp_path, capsys):
    from ultron.cv import truth

    (tmp_path / "private.tex").write_text("Content outside the source must never be loaded.", encoding="utf-8")
    source = _write(
        tmp_path,
        "cv.TEX",
        b"\\documentclass[margin=1in]{article}\n"
        b"\\section*{Experience}\n"
        b"\\textbf{Jane Example}\\\\Data engineering\n"
        b"\\href{https://example.invalid}{Portfolio} \\& reporting \\% improvement\n"
        b"% Omit this comment\n"
        b"\\input{private.tex}\n",
    )
    assert cli.main(["cv", "extract", str(source)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pages"] == 1
    output = Path(result["path"])
    assert output.name == "cv.TEX.txt"
    text = output.read_text(encoding="utf-8")
    assert text == truth.load_cv_plain_text(source)
    assert "Jane Example\nData engineering" in text
    assert "Portfolio (https://example.invalid) & reporting % improvement" in text
    assert "Omit this comment" not in text and "Content outside" not in text


def test_latex_extraction_rejects_over_budget_text(tmp_path):
    source = _write(tmp_path, "cv.tex", b"\\textbf{Python}")
    with pytest.raises(cv.CvTextTooLong):
        cv.extract_cv(source, max_chars=5)


def test_latex_extraction_handles_repeated_unclosed_options(tmp_path):
    malformed = b"\\geometry[" * 20_000 + b"\\section{Experience}"
    source = _write(tmp_path, "malformed.tex", malformed)
    assert cv.extract_cv(source).text.endswith("Experience")

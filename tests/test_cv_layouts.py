"""CV text extraction on synthetic CVs with tricky layouts (two columns, right-aligned dates, bullets, accents)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ultron.cv import extract as cv
from ultron.cv.worker import extract_pdf_pages_text

SYNTHETIC = Path(__file__).parent / "fixtures" / "cv" / "synthetic"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_positioned_pdf(pages: list[list[tuple]]) -> bytes:
    """One A4 page per entry; each entry lists ``(x, y, text)`` lines drawn in that order (WinAnsi Helvetica)."""
    n = len(pages)
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects.append(f"<< /Type /Pages /Count {n} /Kids [{kids}] >>".encode())
    font = 3 + 2 * n
    for i, lines in enumerate(pages):
        # An optional fourth item is the text matrix's rotation part ("0 1 -1 0" draws text rotated 90 degrees).
        body = "\n".join(
            f"BT /F1 10 Tf {(entry[3] if len(entry) > 3 else '1 0 0 1')} {entry[0]} {entry[1]} Tm "
            f"({_escape(entry[2])}) Tj ET"
            for entry in lines
        )
        stream = (body + "\n").encode("cp1252")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>".encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    chunks = [b"%PDF-1.4\n"]
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(sum(len(c) for c in chunks))
        chunks.append(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = sum(len(c) for c in chunks)
    table = ["xref", f"0 {len(objects) + 1}", "0000000000 65535 f "] + [f"{o:010d} 00000 n " for o in offsets]
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return b"".join(chunks) + ("\n".join(table) + "\n").encode() + trailer.encode()


def _layout_pdf(name: str, *, right_x: float) -> bytes:
    """Draw a fixture row by row: header lines full width, then left and right cells at the same height."""
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
    return build_positioned_pdf([lines])


def _extract(content: bytes) -> str:
    return "\n".join(extract_pdf_pages_text(content, max_pages=5, max_chars=60_000))


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def test_two_column_pdf_keeps_each_column_together():
    text = _extract(_layout_pdf("es_two_column.json", right_x=360))
    lines = _lines(text)
    # The headline must not absorb the sidebar ("Ingeniera de Datos Sénior Python, SQL, Spark").
    assert "Ingeniera de Datos Sénior" in lines
    assert "Valencia, España (remoto UE)" in lines
    assert "Python, SQL, Spark" in lines
    sidebar = ["HABILIDADES", "Python, SQL, Spark", "Airflow · Kafka · dbt", "IDIOMAS", "Español (nativo)", "Inglés (C1)"]
    start = lines.index("HABILIDADES")
    assert lines[start : start + len(sidebar)] == sidebar
    # The main column stays in reading order and the header stays first.
    assert lines[0].startswith("Lucía Pérez Ejemplo")
    assert lines.index("EXPERIENCIA") < lines.index("• Diseñé pipelines ETL con Spark y Airflow")
    assert lines.index("FORMACIÓN") < lines.index("Máster en Informática, 2016")


def test_single_column_with_right_aligned_dates_is_not_split():
    text = _extract(_layout_pdf("en_dates_right.json", right_x=430))
    lines = _lines(text)
    # Each role keeps its dates on the same line; splitting would detach them.
    assert "Example Corp, Staff Engineer Mar 2021 – Present" in lines
    assert "Sample Labs, Backend Engineer Jan 2018 – Feb 2021" in lines
    assert "Demo Inc, Junior Developer 2015 – 2017" in lines


def test_single_column_with_right_aligned_locations_is_not_split():
    lines = _lines(_extract(_layout_pdf("en_locations_right.json", right_x=430)))
    assert "Example Studio, Lead Designer Berlin, Germany" in lines
    assert "Sample Agency, Designer Remote" in lines
    assert "Demo Ltd, Junior Designer Porto, Portugal" in lines


def test_single_column_prose_pdf_unchanged():
    rows = [
        "Senior Data Engineer with eight years of experience designing batch and streaming pipelines.",
        "Built a lakehouse on Spark and Delta Lake for a payments company; mentored three engineers.",
        "Comfortable with Python, SQL, Airflow, dbt, Kafka and Terraform on AWS and GCP.",
        "Looking for fully remote roles with European companies.",
    ]
    pdf = build_positioned_pdf([[(50, 800 - 14 * i, row) for i, row in enumerate(rows)]])
    assert _lines(_extract(pdf)) == rows


def test_two_column_pdf_through_the_subprocess(tmp_path):
    path = tmp_path / "two-column.pdf"
    path.write_bytes(_layout_pdf("es_two_column.json", right_x=360))
    lines = _lines(cv.extract_cv_text(path))
    assert "Ingeniera de Datos Sénior" in lines
    assert lines.index("HABILIDADES") + 1 == lines.index("Python, SQL, Spark")


def test_layout_mode_failure_falls_back_to_plain_text(monkeypatch):
    import sys
    from types import SimpleNamespace

    class Page:
        def extract_text(self, **kwargs):
            if kwargs:
                raise ValueError("layout mode unavailable")
            return "Jane Doe Skills\nData Engineer Python"

    class Reader:
        def __init__(self, _stream):
            self.pages = [Page()]

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=Reader))
    assert extract_pdf_pages_text(b"%PDF", max_pages=1, max_chars=100) == ["Jane Doe Skills\nData Engineer Python"]


def test_column_reordering_failure_falls_back_to_plain_text(monkeypatch):
    import sys
    from types import SimpleNamespace

    from ultron.cv import worker as cv_worker

    class Page:
        def extract_text(self, **kwargs):
            return "layout text" if kwargs else "plain text"

    class Reader:
        def __init__(self, _stream):
            self.pages = [Page()]

    def broken(_text):
        raise IndexError("unexpected layout")

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=Reader))
    monkeypatch.setattr(cv_worker, "reorder_columns", broken)
    assert extract_pdf_pages_text(b"%PDF", max_pages=1, max_chars=100) == ["plain text"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Python\u00a0\u00a0\u00a0SQL", "Python SQL"),  # no-break spaces from word processors
        ("Data\u2003Engineer", "Data Engineer"),  # em space
        ("\ufb01nance and of\ufb01ce", "finance and office"),  # fi ligatures from typeset PDFs
        ("Senior\u200b Engineer", "Senior Engineer"),  # zero-width space
        ("engi\u00adneer", "engineer"),  # soft hyphen
        ("\uf0b7 Kubernetes", "• Kubernetes"),  # Symbol-font bullet exported by word processors
    ],
)
def test_text_normalization_handles_invisible_and_compatibility_characters(tmp_path, raw, expected):
    path = tmp_path / "cv.txt"
    path.write_text(raw, encoding="utf-8")
    assert cv.extract_cv_text(path) == expected


def test_worker_normalization_matches_parent():
    from ultron.cv import worker as cv_worker

    sample = "\ufb01nance\u00a0\u00a0team \uf0b7 Go\u200b"
    assert cv_worker._normalize_text(sample) == cv._normalize_text(sample) == "finance team • Go"


def test_synthetic_text_and_markdown_cvs_keep_structure():
    english = cv.extract_cv_text(SYNTHETIC / "en_skills.txt")
    assert "Languages: Go, Python, SQL" in english.splitlines()
    assert "- Designed event-driven services in Go and Kafka" in english.splitlines()
    spanish = cv.extract_cv_text(SYNTHETIC / "es_markdown.md")
    assert "**Analista Financiera Sénior** · jane.doe@example.com · Sevilla, España" in spanish.splitlines()
    assert "| Finanzas | NIIF, IFRS 9, presupuestación |" in spanish.splitlines()


def test_a_truncated_docx_is_a_typed_extraction_error(tmp_path):
    path = tmp_path / "cv.docx"
    path.write_bytes(b"PK\x03\x04")
    with pytest.raises(cv.CvExtractionError) as raised:
        cv.extract_cv_text(path)
    assert raised.value.code == "no_extractable_text"


def test_rotated_text_is_not_lost_when_columns_are_reordered():
    from ultron.cv import worker as cv_worker

    spec = json.loads((SYNTHETIC / "es_two_column.json").read_text(encoding="utf-8"))
    lines: list[tuple] = []
    y = 800.0
    for text in spec["header"]:
        lines.append((50, y, text))
        y -= 20
    for left, right in zip(spec["left"], spec["right"] + [""] * len(spec["left"]), strict=False):
        if left:
            lines.append((50, y, left))
        if right:
            lines.append((360, y, right))
        y -= 14
    content = build_positioned_pdf([lines])
    assert "HABILIDADES" in cv_worker.reorder_columns(_layout_text(content))  # the columns are recognized
    rotated = build_positioned_pdf([[*lines, (585, 200, "Contacto lucia.ejemplo", "0 1 -1 0")]])
    text = _extract(rotated)
    assert "lucia.ejemplo" in text  # layout mode drops rotated text: the plain extraction is kept
    assert "HABILIDADES" in text and "EXPERIENCIA" in text
    # Without rotated text, the reordered columns are still used.
    plain_lines = _lines(_extract(content))
    sidebar_start = plain_lines.index("HABILIDADES")
    assert plain_lines[sidebar_start + 1] == "Python, SQL, Spark"


def _layout_text(content: bytes) -> str:
    from io import BytesIO

    from pypdf import PdfReader

    return PdfReader(BytesIO(content)).pages[0].extract_text(extraction_mode="layout")

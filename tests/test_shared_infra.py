"""Unit tests for shared infrastructure helpers: fs, ooxml, and CLI common utilities."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from ultron.commands import _common
from ultron.core.extractors.text import collapse_whitespace
from ultron.cv import docx_writer as cv_writer
from ultron.cv import extract as cv
from ultron.infra import fs, ooxml
from ultron.stages.candidates import candidate_identity


def test_atomic_write_bytes_and_text_replace_safely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target_bin = tmp_path / "nested" / "data.bin"
    fs.atomic_write_bytes(target_bin, b"v1")
    fs.atomic_write_bytes(target_bin, b"v2")
    assert target_bin.read_bytes() == b"v2"

    target_txt = tmp_path / "nested" / "notes.md"
    fs.atomic_write_text(target_txt, "line1\nline2\n")
    assert target_txt.read_bytes() == b"line1\nline2\n"

    # Simulate failure during os.replace -> temp file must be removed and original preserved
    def fail_replace(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        fs.atomic_write_bytes(target_bin, b"v3")
    assert target_bin.read_bytes() == b"v2"
    leftovers = list(target_bin.parent.glob("data.bin.*.tmp"))
    assert leftovers == []


def test_is_link_regular_vs_symlink_or_junction(tmp_path: Path):
    regular = tmp_path / "plain"
    regular.mkdir()
    assert fs.is_link(regular) is False


def test_ooxml_primitives_and_escaping():
    assert ooxml.resolve_safe_font("calibri") == "Calibri"
    assert ooxml.WRITER_SAFE_FONTS == cv_writer.WRITER_SAFE_FONTS
    with pytest.raises(ValueError, match="unsupported font"):
        ooxml.resolve_safe_font("Comic Sans MS")

    assert ooxml.sanitize_xml_text("a\r\nb\x00c") == "a bc"
    styled_run = ooxml.run_xml("Note", italic=True, color="1F4E79", size=20)
    assert "<w:i/>" in styled_run and 'w:val="1F4E79"' in styled_run
    assert ooxml.xml_escape("Tom & 'Jerry' \"1\"") == "Tom &amp; &apos;Jerry&apos; &quot;1&quot;"
    assert ooxml.safe_hyperlink("https://example.com/a") == "https://example.com/a"
    assert ooxml.safe_hyperlink("mailto:ada@example.com") == "mailto:ada@example.com"
    assert ooxml.safe_hyperlink("javascript:alert(1)") is None
    assert ooxml.safe_hyperlink("") is None

    rels: list[tuple[str, str]] = []
    runs = ooxml.inline_runs_xml("See **Ada** at [Site](https://example.com) and [Bad](ftp://bad)", rels, size=20)
    assert "<w:b/>" in runs
    assert "w:hyperlink" in runs
    assert len(rels) == 1
    assert "Bad" in runs and "ftp://bad" not in runs

    assert 'Target="https://example.com"' in ooxml.document_rels_xml(rels)
    assert "word/document.xml" in ooxml.content_types_xml()
    assert "docProps/core.xml" in ooxml.root_rels_xml()
    assert "Report &amp; Title" in ooxml.core_props_xml("Report & Title")
    assert "Calibri" in ooxml.numbering_xml("Calibri")


def test_commands_common_parse_as_of_and_error_wrappers(capsys: pytest.CaptureFixture[str]):
    assert _common.parse_as_of(None) is None
    assert _common.parse_as_of(" 2026-11-21 ") == date(2026, 11, 21)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        _common.parse_as_of("not-a-date")

    assert _common.run_verify_truth(lambda: {"passed": False}, strict=True) == 1
    capsys.readouterr()
    assert _common.run_verify_truth(lambda: {"passed": True}, strict=True) == 0
    capsys.readouterr()

    def raise_cv():
        raise cv.CvExtractionError("too huge", code="too_long")

    assert _common.run_verify_truth(raise_cv, strict=False) == 2
    assert _common.run_build_docx(raise_cv) == 2

    def raise_exists():
        raise FileExistsError("exists")

    assert _common.run_build_docx(raise_exists) == 2

    def raise_missing():
        raise FileNotFoundError("missing")

    assert _common.run_verify_truth(raise_missing, strict=False) == 2
    assert _common.run_build_docx(raise_missing) == 2

    def raise_truthfulness():
        raise cv_writer.TruthfulnessError({"passed": False, "violations": ["invented metric"]})

    assert _common.run_build_docx(raise_truthfulness) == 1
    capsys.readouterr()

    def raise_value_error():
        raise ValueError("bad format")

    assert _common.run_verify_truth(raise_value_error, strict=False) == 2
    assert _common.run_build_docx(raise_value_error) == 2


def test_collapse_whitespace_and_candidate_identity():
    assert collapse_whitespace("  Senior \n\t Backend  ") == "Senior Backend"
    ident = candidate_identity({"source_url": "https://boards.greenhouse.io/acme/jobs/1?utm_source=x"})
    assert ident

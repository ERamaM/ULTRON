"""Test local renderer discovery, process isolation, and static input validation."""

import io
import os
import subprocess
import sys
import types
import zipfile

import pytest

from ultron.cv import docx_writer
from ultron.infra import docx_pdf


@pytest.fixture
def docx():
    return docx_writer.render_docx_bytes(docx_writer.parse_markdown_cv("# Jane Example"))


def test_discovery_order_and_explicit_selection(monkeypatch, tmp_path):
    app = tmp_path / "Word.app"
    app.mkdir()
    soffice = tmp_path / "soffice"
    soffice.touch()
    script = tmp_path / "osascript"
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(docx_pdf, "_APP", str(app))
    monkeypatch.setattr(docx_pdf, "_LO_MAC", str(soffice))

    def which(candidate, *, path):
        assert not path
        return str(script) if candidate == str(script) else None

    monkeypatch.setattr(docx_pdf.shutil, "which", which)
    assert docx_pdf.detect_pdf_converter({"PATH": str(tmp_path)}) == ("libreoffice", str(soffice))
    soffice.unlink()
    assert docx_pdf.detect_pdf_converter({"PATH": str(tmp_path)}) == ("word", str(script))
    for choice in ("none", "other", "libreoffice"):
        assert docx_pdf.detect_pdf_converter({"PATH": str(tmp_path), "ULTRON_DOCX_PDF_CONVERTER": choice}) is None
    assert docx_pdf.detect_pdf_converter({"PATH": str(tmp_path), "ULTRON_DOCX_PDF_CONVERTER": " Word "}) == (
        "word", str(script),
    )


def test_path_search_ignores_current_and_relative_directories(monkeypatch, tmp_path):
    working_dir = tmp_path / "working"
    working_dir.mkdir()
    monkeypatch.chdir(working_dir)
    trusted = tmp_path / "installed"
    trusted.mkdir()
    checked = []
    executable = str(trusted / ("soffice.exe" if sys.platform == "win32" else "soffice"))

    def which(candidate, *, path):
        assert not path
        checked.append(candidate)
        return executable

    monkeypatch.setattr(docx_pdf.shutil, "which", which)
    search_path = os.pathsep.join(("", ".", "bin", str(working_dir), str(trusted)))
    assert docx_pdf.detect_pdf_converter({"PATH": search_path}) == ("libreoffice", executable)
    assert checked == [str(trusted / "soffice")]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows executable lookup")
def test_windows_does_not_discover_office_from_project_directory(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "soffice.exe").touch()
    installed = tmp_path / "installed"
    installed.mkdir()
    monkeypatch.chdir(project)
    env = {"PATH": os.pathsep.join((str(project), ".", str(installed))), "ULTRON_DOCX_PDF_CONVERTER": "libreoffice"}
    assert docx_pdf.detect_pdf_converter(env) is None
    (installed / "soffice.bat").touch()
    assert docx_pdf.detect_pdf_converter(env) is None
    (installed / "soffice.exe").touch()
    assert docx_pdf.detect_pdf_converter(env) == ("libreoffice", str(installed / "soffice.exe"))


def test_windows_program_files_and_registry_discovery(monkeypatch, tmp_path):
    lo = tmp_path / "LibreOffice/program/soffice.exe"
    lo.parent.mkdir(parents=True)
    lo.touch()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(docx_pdf.shutil, "which", lambda *_a, **_kw: None)
    assert docx_pdf.detect_pdf_converter({"ProgramFiles(x86)": str(tmp_path)}) == ("libreoffice", str(lo))

    assert docx_pdf.detect_pdf_converter({"PROGRAMFILES(X86)": str(tmp_path)}) == ("libreoffice", str(lo))

    def open_key(root, subkey):
        assert (root, subkey) == (1, r"Word.Application\CLSID")
        return io.BytesIO()

    monkeypatch.setitem(sys.modules, "winreg", types.SimpleNamespace(HKEY_CLASSES_ROOT=1, OpenKey=open_key))
    powershell = tmp_path / "System32/WindowsPowerShell/v1.0/powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.touch()
    assert docx_pdf.detect_pdf_converter({"SystemRoot": str(tmp_path)}) == ("word", str(powershell))

    def registry_missing(*_args):
        raise FileNotFoundError

    monkeypatch.setitem(sys.modules, "winreg", types.SimpleNamespace(HKEY_CLASSES_ROOT=1, OpenKey=registry_missing))
    assert docx_pdf.detect_pdf_converter({"SystemRoot": str(tmp_path)}) is None


@pytest.mark.parametrize(("backend", "platform"), [("libreoffice", "linux"), ("word", "win32"), ("word", "darwin")])
def test_backends_use_isolated_files_and_cleanup(monkeypatch, docx, backend, platform):
    folders = []

    def convert(args, *, cwd, env, **kwargs):
        source = cwd / "input.docx"
        output = cwd / "input.pdf"
        folders.append(cwd)
        assert kwargs["shell"] is False and kwargs["timeout"] == 25.0
        assert source.read_bytes() == docx
        if backend == "libreoffice":
            assert args[1] == "-env:UserInstallation=" + (cwd / "profile").as_uri()
            assert "--headless" in args and args[-3:] == ["--outdir", str(cwd), str(source)]
            config = (cwd / "profile/user/registrymodifications.xcu").read_text(encoding="utf-8")
            assert 'oor:name="Link" oor:op="fuse"><value>2' in config
        elif platform == "win32":
            assert args[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
            assert "AutomationSecurity = 3" in args[4]
            assert "finally" in args[4] and "Quit(0)" in args[4] and str(source) not in args[4]
            assert env["ULTRON_DOCX_INPUT"] == str(source)
            assert env["ULTRON_DOCX_OUTPUT"] == str(output)
        else:
            assert "alerts none" in args[2] and "saving no" in args[2]
            assert args[3:] == [str(source), str(output)]
        output.write_bytes(b"%PDF-stub")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(docx_pdf, "detect_pdf_converter", lambda _: (backend, "office"))
    monkeypatch.setattr(docx_pdf.subprocess, "run", convert)
    for _ in range(2):
        assert docx_pdf.render_docx_to_pdf_bytes(docx) == (b"%PDF-stub", backend)
    assert folders[0] != folders[1]
    assert all(not folder.exists() for folder in folders)


@pytest.mark.parametrize("failure", ["timeout", "missing", "nonzero", "no_output", "oversized", "wrong_format"])
def test_failed_conversion_returns_none_and_cleans_up(monkeypatch, docx, failure):
    folders = []

    def convert(args, *, cwd, **kwargs):
        folders.append(cwd)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        if failure == "missing":
            raise FileNotFoundError
        if failure == "oversized":
            (cwd / "input.pdf").write_bytes(b"%PDF-too long")
        if failure == "wrong_format":
            (cwd / "input.pdf").write_bytes(b"wrong")
        return subprocess.CompletedProcess(args, int(failure == "nonzero"))

    monkeypatch.setattr(docx_pdf, "detect_pdf_converter", lambda _: ("libreoffice", "soffice"))
    monkeypatch.setattr(docx_pdf, "MAX_PDF_BYTES", 9)
    monkeypatch.setattr(docx_pdf.subprocess, "run", convert)
    assert docx_pdf.render_docx_to_pdf_bytes(docx) is None
    assert len(folders) == 1 and not folders[0].exists()


@pytest.mark.parametrize(("name", "payload"), [
    ("word/vbaProject.bin", b"active"),
    ("word/extra.xml", b'<!DOCTYPE root [<!ENTITY a "active">]><root>&a;</root>'),
    ("word/extra.xml", b'<Types><Override ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>'),
    ("word/extra.xml", b"<root><instrText> INCLUDETEXT remote </instrText></root>"),
    ("word/extra.xml", b'<root><fldSimple instr="DDE example"/></root>'),
    ("word/extra.xml", b'<root><a href="//example.invalid/private"/></root>'),
    ("word/extra.xml", b'<root><Relationship TargetMode="External" Target="https://example.invalid/"/></root>'),
    ("word/extra.xml", b'<root><Relationship Target="file:///private"/></root>'),
    ("word/extra.xml", b'<root><Relationship Target="%2F%2Fexample.invalid/private"/></root>'),
    ("word/extra.xml", b'<root><Relationship Type="urn:test/vbaProject" Target="bogus.xml"/></root>'),
    ("word/extra.xml", b'<?xml-stylesheet href="https://example.invalid/"?><root/>'),
    ("word/extra.xml", b'<root><attachedTemplate/></root>'),
    ("../unexpected.xml", b"<root/>"),
])
def test_renderer_never_opens_active_docx(monkeypatch, docx, name, payload):
    data = io.BytesIO(docx)
    with zipfile.ZipFile(data, "a") as archive:
        archive.writestr(name, payload)
    monkeypatch.setattr(docx_pdf, "detect_pdf_converter", lambda _: ("word", "office"))
    monkeypatch.setattr(docx_pdf.subprocess, "run", lambda *_a, **_kw: pytest.fail("renderer launched"))
    assert docx_pdf.render_docx_to_pdf_bytes(data.getvalue()) is None


def test_non_zip_is_rejected_before_rendering(monkeypatch):
    monkeypatch.setattr(docx_pdf, "detect_pdf_converter", lambda _: ("word", "office"))
    monkeypatch.setattr(docx_pdf.subprocess, "run", lambda *_a, **_kw: pytest.fail("renderer launched"))
    assert docx_pdf.render_docx_to_pdf_bytes(b"invalid") is None


def test_missing_renderer_does_not_parse_document():
    assert docx_pdf.render_docx_to_pdf_bytes(b"") is None

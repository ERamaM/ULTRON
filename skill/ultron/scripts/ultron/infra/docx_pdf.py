"""Discover local Office renderers and produce temporary DOCX layout references."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.parsers import expat

MAX_PDF_BYTES = 25_000_000
_MAX_PACKAGE_BYTES = 50_000_000
_APP = "/Applications/Microsoft Word.app"
_LO_MAC = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
_HYPERLINK = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
_STATIC_PARTS = {".xml", ".rels", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}
_LOCAL_FIELDS = {"page", "numpages", "section", "sectionpages", "pageref"}

_LO_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
  <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
    <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
  </item>
  <item oor:path="/org.openoffice.Office.Writer/Content/Update">
    <prop oor:name="Link" oor:op="fuse"><value>2</value></prop>
  </item>
</oor:items>
"""

_WORD_WINDOWS = r"""
$ErrorActionPreference = 'Stop'
$word = $null
$doc = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    $doc = $word.Documents.Open($env:ULTRON_DOCX_INPUT, $false, $true, $false)
    $doc.ExportAsFixedFormat($env:ULTRON_DOCX_OUTPUT, 17)
} finally {
    try {
        if ($null -ne $doc) { $doc.Close(0) }
    } finally {
        if ($null -ne $word) { $word.Quit(0) }
    }
}
"""

_WORD_MAC = """
on run argv
    set sourcePath to POSIX file (item 1 of argv) as text
    set pdfPath to POSIX file (item 2 of argv) as text
    tell application id "com.microsoft.Word"
        set savedAlerts to display alerts
        set sourceDoc to missing value
        try
            set display alerts to alerts none
            set sourceDoc to open file name sourcePath read only true add to recent files false
            save as sourceDoc file name pdfPath file format format PDF
            close sourceDoc saving no
        on error errorMessage number errorNumber
            try
                if sourceDoc is not missing value then close sourceDoc saving no
            end try
            set display alerts to savedAlerts
            error errorMessage number errorNumber
        end try
        set display alerts to savedAlerts
    end tell
end run
"""


def _on_path(names: tuple[str, ...], env: Mapping[str, str]) -> str | None:
    cwd = Path.cwd().resolve()
    directories = []
    for entry in env.get("PATH", os.defpath).split(os.pathsep):
        directory = Path(entry.strip('"'))
        if directory.is_absolute() and (resolved := directory.resolve()) != cwd:
            directories.append(resolved)

    for name in names:
        for directory in directories:
            # An explicit path lets which check executability and PATHEXT without prepending cwd.
            if executable := shutil.which(str(directory / name), path=""):
                resolved_executable = Path(executable).resolve()
                if resolved_executable.parent == cwd:
                    continue
                if sys.platform == "win32" and resolved_executable.suffix.casefold() not in {".exe", ".com"}:
                    continue
                return str(resolved_executable)
    return None


def _libreoffice(env: Mapping[str, str]) -> str | None:
    if executable := _on_path(("soffice", "libreoffice"), env):
        return executable
    if sys.platform == "darwin" and Path(_LO_MAC).is_file():
        return _LO_MAC
    if sys.platform == "win32":
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            if folder := env.get(key):
                path = Path(folder) / "LibreOffice" / "program" / "soffice.exe"
                if path.is_file():
                    return str(path.resolve())
    return None


def _word_is_registered() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"):
            return True
    except OSError:
        return False


def _word(env: Mapping[str, str]) -> str | None:
    if sys.platform == "win32" and _word_is_registered():
        windows = env.get("SYSTEMROOT")
        if windows:
            powershell = Path(windows) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            if powershell.is_file():
                return str(powershell.resolve())
        return _on_path(("powershell.exe",), env)
    if sys.platform == "darwin" and Path(_APP).is_dir():
        return _on_path(("osascript",), env)
    return None


def detect_pdf_converter(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """Return the selected backend and executable without launching an Office application."""
    settings = os.environ if env is None else env
    if sys.platform == "win32":
        settings = {key.upper(): value for key, value in settings.items()}
    choice = settings.get("ULTRON_DOCX_PDF_CONVERTER", "auto").strip().lower()
    if choice in {"auto", "libreoffice"} and (executable := _libreoffice(settings)):
        return "libreoffice", executable
    if choice in {"auto", "word"} and (executable := _word(settings)):
        return "word", executable
    return None


def _reject_active_content(*_args: object) -> None:
    raise ValueError("Active or unsupported DOCX content")


def _safe_relationship(attributes: dict[str, str]) -> bool:
    target = attributes.get("Target", "")
    mode = attributes.get("TargetMode", "Internal")
    kind = attributes.get("Type", "")
    if mode == "External":
        return kind == _HYPERLINK and urlsplit(target).scheme in {"http", "https", "mailto"}

    decoded = unquote(target)
    if kind.rsplit("/", 1)[-1].casefold() in {"vbaproject", "vbadata", "attachedtemplate", "oleobject", "package"}:
        return False
    local_target = bool(decoded) and ":" not in decoded and "\\" not in decoded and not decoded.startswith("//")
    return mode == "Internal" and local_target


def _check_static_xml(data: bytes) -> None:
    # Inspect every XML part, including styles, settings, relationships and embedded content types.
    # macOS Word has no equivalent to Windows COM's per-open macro security switch.
    instruction: list[str] | None = None

    def check_field(code: str) -> None:
        parts = code.split(maxsplit=1)
        if not parts or parts[0].casefold() not in _LOCAL_FIELDS:
            _reject_active_content()

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal instruction
        tag = name.rsplit(" ", 1)[-1]
        if tag == "Relationship" and not _safe_relationship(attributes):
            _reject_active_content()
        if tag in {"Default", "Override"}:
            content_type = attributes.get("ContentType", "").casefold()
            if "macro" in content_type or "vba" in content_type:
                _reject_active_content()
        if tag in {"attachedTemplate", "altChunk", "object", "control", "subDoc", "mailMerge", "htmlDiv"}:
            _reject_active_content()
        if any(k.rsplit(" ", 1)[-1] in {"src", "href", "base"} and not v.startswith("#") for k, v in attributes.items()):
            _reject_active_content()
        if tag in {"instrText", "delInstrText"}:
            instruction = []
        elif tag == "fldSimple":
            check_field(next((v for k, v in attributes.items() if k.rsplit(" ", 1)[-1] == "instr"), ""))

    def characters(data: str) -> None:
        if instruction is not None:
            instruction.append(data)

    def end(name: str) -> None:
        nonlocal instruction
        if name.rsplit(" ", 1)[-1] in {"instrText", "delInstrText"} and instruction is not None:
            check_field("".join(instruction))
            instruction = None

    parser = expat.ParserCreate(namespace_separator=" ")
    parser.StartElementHandler = start
    parser.CharacterDataHandler = characters
    parser.EndElementHandler = end
    parser.ProcessingInstructionHandler = _reject_active_content
    parser.StartDoctypeDeclHandler = _reject_active_content
    parser.EntityDeclHandler = _reject_active_content
    parser.ExternalEntityRefHandler = _reject_active_content
    parser.Parse(data, True)


def _check_static_package(content: bytes) -> None:
    if not content.startswith(b"PK\x03\x04"):
        _reject_active_content()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        parts = archive.infolist()
        if len(parts) > 2_000 or sum(part.file_size for part in parts) > _MAX_PACKAGE_BYTES:
            _reject_active_content()
        seen: set[str] = set()
        for part in parts:
            name = part.filename.casefold()
            path = PurePosixPath(name)
            if name in seen or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                _reject_active_content()
            seen.add(name)
            if part.is_dir():
                continue
            extension = ".rels" if name == "_rels/.rels" else path.suffix
            if extension not in _STATIC_PARTS or part.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                _reject_active_content()
            if extension in {".xml", ".rels"}:
                _check_static_xml(archive.read(part))
        if not {"[content_types].xml", "word/document.xml", "_rels/.rels"} <= seen:
            _reject_active_content()


def _libreoffice_command(executable: str, source: Path) -> list[str]:
    if sys.platform == "win32":
        # The console launcher waits for conversion. The GUI launcher may return before the PDF exists.
        console = Path(executable).with_suffix(".com")
        if console.is_file():
            executable = str(console)
    profile = source.parent / "profile"
    user = profile / "user"
    user.mkdir(parents=True)
    (user / "registrymodifications.xcu").write_text(_LO_SETTINGS, encoding="utf-8")
    return [
        executable, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
        "--nodefault", "--nolockcheck", "--nofirststartwizard", "--convert-to", "pdf",
        "--outdir", str(source.parent), str(source),
    ]


def _command(
    backend: str, executable: str, source: Path, output: Path, env: dict[str, str],
) -> list[str]:
    if backend == "libreoffice":
        return _libreoffice_command(executable, source)
    if sys.platform == "win32":
        env.update(ULTRON_DOCX_INPUT=str(source), ULTRON_DOCX_OUTPUT=str(output))
        return [executable, "-NoProfile", "-NonInteractive", "-Command", _WORD_WINDOWS]
    return [executable, "-e", _WORD_MAC, str(source), str(output)]


def render_docx_to_pdf_bytes(
    docx_bytes: bytes, *, timeout: float = 25.0, env: Mapping[str, str] | None = None,
) -> tuple[bytes, str] | None:
    """Render a static DOCX locally, returning PDF bytes and backend when successful."""
    settings = dict(os.environ if env is None else env)
    try:
        converter = detect_pdf_converter(settings)
        if converter is None:
            return None
        _check_static_package(docx_bytes)
        backend, executable = converter
        with tempfile.TemporaryDirectory(prefix="ultron-docx-") as folder:
            source = Path(folder).resolve() / "input.docx"
            output = source.with_suffix(".pdf")
            source.write_bytes(docx_bytes)
            completed = subprocess.run(
                _command(backend, executable, source, output, settings),
                env=settings, cwd=source.parent, timeout=timeout, shell=False, check=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if completed.returncode or output.stat().st_size > MAX_PDF_BYTES:
                return None
            with output.open("rb") as stream:
                pdf = stream.read(MAX_PDF_BYTES + 1)
            if len(pdf) <= MAX_PDF_BYTES and pdf.startswith(b"%PDF-"):
                return pdf, backend
    except (OSError, subprocess.SubprocessError, ValueError, expat.ExpatError, zipfile.BadZipFile, zlib.error, RuntimeError):
        return None
    return None

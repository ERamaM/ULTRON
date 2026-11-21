"""Downloading and unpacking a release source archive with host, size and member limits."""

from __future__ import annotations

import re
import stat
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ultron.infra.net import USER_AGENT

DOWNLOAD_TIMEOUT = 60.0
MAX_REDIRECTS = 3
MAX_DOWNLOAD_BYTES = 50_000_000
MAX_EXTRACTED_BYTES = 200_000_000
MAX_MEMBERS = 10_000
DOWNLOAD_HOSTS = frozenset({"github.com", "codeload.github.com"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_VERSION_LINE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)

# Test hook: an ``httpx`` transport for the archive download.
_TRANSPORT: httpx.BaseTransport | None = None


class DownloadError(RuntimeError):
    pass


class ArchiveError(ValueError):
    pass



# ------------------------------------------------------------------------------------------- download


def _check_download_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise DownloadError("refusing to download from a malformed URL") from exc
    if parts.scheme != "https" or parts.hostname not in DOWNLOAD_HOSTS or parts.username or parts.password:
        raise DownloadError(f"refusing to download from {parts.scheme}://{parts.hostname or ''}")
    if port not in (None, 443):
        raise DownloadError("refusing to download from a non-standard port")


def download_archive(url: str, dest: Path) -> None:
    """Stream ``url`` into ``dest``: https only, GitHub hosts only on every hop, hard size cap."""
    current = url
    try:
        with httpx.Client(transport=_TRANSPORT, follow_redirects=False, trust_env=False,
                          timeout=DOWNLOAD_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                _check_download_url(current)
                with client.stream("GET", current) as response:
                    location = response.headers.get("Location")
                    if response.status_code in REDIRECT_STATUSES and location:
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise DownloadError(f"HTTP {response.status_code} from {urlsplit(current).hostname}")
                    declared = response.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                        raise DownloadError("release archive is larger than the download limit")
                    total = 0
                    with open(dest, "wb") as out:
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > MAX_DOWNLOAD_BYTES:
                                raise DownloadError("release archive is larger than the download limit")
                            out.write(chunk)
                    return
    except httpx.HTTPError as exc:
        raise DownloadError(f"download failed ({type(exc).__name__})") from exc
    raise DownloadError(f"too many redirects (max {MAX_REDIRECTS})")


# ------------------------------------------------------------------------------------------- extraction


def _member_parts(info: zipfile.ZipInfo) -> list[str]:
    name = info.filename
    if not name or "\\" in name or ":" in name or "\x00" in name or name.startswith("/"):
        raise ArchiveError(f"unsafe path in archive: {name!r}")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise ArchiveError(f"unsafe path in archive: {name!r}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ArchiveError(f"archive member is a link: {name!r}")
    return parts


def extract_source(archive: Path, dest: Path, *, expected_version: str) -> Path:
    """Safely extract a GitHub source archive into ``dest``; return its single top-level folder.

    Rejects absolute, drive-qualified, backslash and ``..`` member names, links, duplicate names, more than
    ``MAX_MEMBERS`` entries and more than ``MAX_EXTRACTED_BYTES`` (counted while writing). The folder must hold
    ``install.py`` and ``skill/ultron/SKILL.md`` and declare ``expected_version``.
    """
    dest = Path(dest)
    root = dest.resolve()
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError("the download is not a valid zip archive") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ArchiveError(f"archive has more than {MAX_MEMBERS} entries")
        planned = [(info, _member_parts(info)) for info in infos]
        tops = {parts[0] for _, parts in planned}
        if len(tops) != 1:
            raise ArchiveError("archive must contain a single top-level folder")
        if sum(info.file_size for info in infos) > MAX_EXTRACTED_BYTES:
            raise ArchiveError("archive is too large once extracted")
        written = 0
        for info, parts in planned:
            target = dest.joinpath(*parts)
            if root != target.resolve() and root not in target.resolve().parents:
                raise ArchiveError(f"unsafe path in archive: {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as src, open(target, "xb") as out:
                    while chunk := src.read(1 << 16):
                        written += len(chunk)
                        if written > MAX_EXTRACTED_BYTES:
                            raise ArchiveError("archive is too large once extracted")
                        out.write(chunk)
            except FileExistsError as exc:
                raise ArchiveError(f"duplicate archive member: {info.filename!r}") from exc
            except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, zlib.error, RuntimeError,
                    NotImplementedError) as exc:  # corrupt, encrypted or unsupported members
                raise ArchiveError("the archive is corrupt") from exc
    source = dest / tops.pop()
    for required in ("install.py", "skill/ultron/SKILL.md"):
        if not (source / required).is_file():
            raise ArchiveError(f"archive does not contain {required}")
    init = source / "skill" / "ultron" / "scripts" / "ultron" / "__init__.py"
    try:
        match = _VERSION_LINE.search(init.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        match = None
    if match is None or match.group(1) != expected_version:
        found = match.group(1) if match else "none"
        raise ArchiveError(f"archive version {found} does not match the release {expected_version}")
    return source



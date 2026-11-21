"""CandidateManager: an employer's public vacancy list is one page on ``www.candidatemanager.net``.

The page is ``/cm/p/pJobs.aspx?mid=<mid>&sid=<sid>``; ULTRON's board identifier is ``<mid>-<sid>`` (both parts
alphanumeric and case-sensitive). One GET per board, no pagination: every vacancy is a row of one table, with a
link to ``/cm/p/pJobDetails.aspx?mid=<mid>&sid=<sid>&jid=<jid>`` and a location cell. Only detail links of the
same board on the vendor's host are kept, without their other query keys, and at most ``MAX_POSTINGS`` of them.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from ultron.core.ats.clients.base import AtsClientError, AtsHTTPClient, AtsPosting

MAX_POSTINGS = 500
HOST = "www.candidatemanager.net"
_PART_RE = re.compile(r"[A-Za-z0-9]{1,32}", re.ASCII)
_DETAIL_PATH = "/cm/p/pjobdetails.aspx"


def _clean(text: object) -> str:
    return " ".join(str(text or "").split())


def candidatemanager_board(board_identifier: str) -> tuple[str, str] | None:
    """``(mid, sid)`` of a ``<mid>-<sid>`` board identifier, or ``None`` when it is not one."""
    mid, sep, sid = str(board_identifier or "").strip().partition("-")
    if not sep or not _PART_RE.fullmatch(mid) or not _PART_RE.fullmatch(sid):
        return None
    return mid, sid


def candidatemanager_board_from_url(url: str) -> str | None:
    """The ``<mid>-<sid>`` board of a vendor board or detail URL, or ``None``."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host != "candidatemanager.net" and not host.endswith(".candidatemanager.net"):
        return None
    if parts.path.lower() not in {"/cm/p/pjobs.aspx", _DETAIL_PATH}:
        return None
    query = parse_qs(parts.query)
    mid, sid = (query.get("mid") or [""])[0], (query.get("sid") or [""])[0]
    board = f"{mid}-{sid}"
    return board if candidatemanager_board(board) else None


def _posting_url(board_url: str, href: str, mid: str, sid: str) -> str:
    """Canonical detail URL of this board's vacancy, or ``""`` for any other link."""
    parts = urlsplit(urljoin(board_url, str(href or "").strip()))
    if parts.scheme not in {"http", "https"} or (parts.hostname or "").lower() != HOST:
        return ""
    if parts.path.lower() != _DETAIL_PATH:
        return ""
    query = parse_qs(parts.query)
    jid = (query.get("jid") or [""])[0]
    if (query.get("mid") or [""])[0] != mid or (query.get("sid") or [""])[0] != sid or not _PART_RE.fullmatch(jid):
        return ""
    return f"https://{HOST}/cm/p/pJobDetails.aspx?" + urlencode({"mid": mid, "sid": sid, "jid": jid})


def fetch_candidatemanager_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    """Fetch open postings from a CandidateManager XML/HTML board."""
    board = candidatemanager_board(board_identifier)
    if board is None:
        raise AtsClientError("invalid_board")  # the board is <mid>-<sid>
    mid, sid = board
    board_url = f"https://{HOST}/cm/p/pJobs.aspx?" + urlencode({"mid": mid, "sid": sid})
    html = client.request_text(board_url, timeout=timeout)
    soup = BeautifulSoup(html or "", "html.parser")
    form = soup.find("form", attrs={"action": re.compile(r"pJobs\.aspx", re.IGNORECASE)})
    if form is None:
        # Not a vacancy list (an error page, a login page): never report it as a verified empty board.
        raise AtsClientError("unparseable_board")
    out: list[AtsPosting] = []
    seen: set[str] = set()
    for link in soup.select("table a[href]"):
        apply_url = _posting_url(board_url, str(link.get("href") or ""), mid, sid)
        if not apply_url or apply_url in seen:
            continue
        for small in link.find_all("small"):
            small.decompose()
        title = _clean(link.get_text(" ", strip=True))
        if not title:
            continue
        seen.add(apply_url)
        row = link.find_parent("tr")
        cells = row.find_all("td") if row is not None else []
        location = _clean(cells[-1].get_text(" ", strip=True)) if len(cells) > 1 else ""
        out.append(AtsPosting(title=title, apply_url=apply_url, location=location))
        if len(out) >= MAX_POSTINGS:
            break
    return out

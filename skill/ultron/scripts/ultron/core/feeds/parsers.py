"""Pure payload parsers converting RSS and public job feed responses into rows."""

from __future__ import annotations

import re
from collections.abc import Iterable

from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET


def split_title_company(title: str, source: str) -> tuple[str, str]:
    """Split a feed item title into role title and company name."""
    clean = " ".join(str(title or "").split()).strip()
    if not clean:
        return "", ""

    if source == "weworkremotely" and ":" in clean:
        left, right = clean.split(":", 1)
        company = left.strip()
        role = right.strip()
        if company and role:
            return role, company

    low = clean.lower()
    if " at " in low:
        role_part, company_part = re.split(r"\sat\s", clean, maxsplit=1, flags=re.IGNORECASE)
        role = role_part.strip()
        company = company_part.strip()
        if role and company:
            return role, company

    return clean, ""


def rows_from_rss_payload(payload: str, source: str) -> list[dict]:
    """Parse an RSS or Atom XML document into normalized feed rows."""
    text = str(payload or "").strip()
    if not text:
        return []

    try:
        root = ET.fromstring(text)
    except Exception:
        return []

    rows: list[dict] = []
    items = root.findall(".//item")
    entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    if items:
        for item in items:
            raw_title = (item.findtext("title") or "").strip()
            title, company = split_title_company(raw_title, source)
            link = (item.findtext("link") or "").strip()
            if not link:
                guid = (item.findtext("guid") or "").strip()
                if guid.startswith("http://") or guid.startswith("https://"):
                    link = guid
            if not link:
                continue
            row = {
                "title": title,
                "company_name": company,
                "url": link,
                "source": source,
            }
            description = (item.findtext("description") or "").strip()
            if description:
                row["description"] = description
            rows.append(row)
        return rows

    for entry in entries:
        raw_title = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        title, company = split_title_company(raw_title, source)
        link = ""
        for link_node in entry.findall("{http://www.w3.org/2005/Atom}link"):
            href = str(link_node.get("href") or "").strip()
            rel = str(link_node.get("rel") or "").strip().lower()
            if href and (not rel or rel == "alternate"):
                link = href
                break
        if not link:
            continue
        rows.append(
            {
                "title": title,
                "company_name": company,
                "url": link,
                "source": source,
            }
        )
    return rows


def rows_from_jobicy_api_payload(payload: dict | list) -> list[dict]:
    """Parse a Jobicy JSON payload into normalized feed rows."""
    jobs: list[dict] = []
    if isinstance(payload, dict):
        raw_jobs = payload.get("jobs")
        if isinstance(raw_jobs, list):
            jobs = [item for item in raw_jobs if isinstance(item, dict)]
    elif isinstance(payload, list):
        jobs = [item for item in payload if isinstance(item, dict)]

    rows: list[dict] = []
    for item in jobs:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        row = {
            "title": str(item.get("jobTitle") or item.get("title") or "").strip(),
            "company_name": str(item.get("companyName") or item.get("company_name") or "").strip(),
            "url": url,
            "source": "jobicy_api",
        }
        description = str(item.get("jobExcerpt") or item.get("description") or "").strip()
        if description:
            row["description"] = description
        loc = str(item.get("jobGeo") or item.get("location") or "").strip()
        if loc:
            row["location"] = loc
        rows.append(row)
    return rows


def rows_from_remoteok_payload(payload: dict | list) -> list[dict]:
    """Parse a RemoteOK JSON array payload into normalized feed rows."""
    if not isinstance(payload, list):
        return []

    rows: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        company = str(item.get("company") or "").strip()
        title = str(item.get("position") or item.get("role") or "").strip()
        row = {
            "title": title,
            "company_name": company,
            "url": url,
            "source": "remoteok",
        }
        loc = str(item.get("location") or "").strip()
        if loc:
            row["location"] = loc
        rows.append(row)
    return rows


def rows_from_themuse_payload(payload: dict | list) -> tuple[list[dict], int | None]:
    """Parse a The Muse JSON page into normalized feed rows and total page count."""
    if not isinstance(payload, dict):
        return [], None

    results = payload.get("results")
    if not isinstance(results, list):
        return [], None

    rows: list[dict] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        refs = item.get("refs")
        landing_page = refs.get("landing_page") if isinstance(refs, dict) else ""
        url = str(landing_page or item.get("url") or "").strip()
        if not url:
            continue
        company = ""
        company_obj = item.get("company")
        if isinstance(company_obj, dict):
            company = str(company_obj.get("name") or "").strip()
        title = str(item.get("name") or "").strip()
        row = {
            "title": title,
            "company_name": company,
            "url": url,
            "source": "themuse",
        }
        locs = [
            str(loc.get("name") or "").strip()
            for loc in (item.get("locations") or [])
            if isinstance(loc, dict) and str(loc.get("name") or "").strip()
        ]
        if locs:
            row["location"] = ", ".join(locs)
        rows.append(row)

    page_count = payload.get("page_count")
    if isinstance(page_count, int):
        return rows, page_count
    return rows, None


def extract_company_names_from_weworkremotely_top_html(html: str) -> list[str]:
    """Extract featured company names from WeWorkRemotely top-companies HTML."""
    text = str(html or "").strip()
    if not text:
        return []

    soup = BeautifulSoup(text, "html.parser")
    blocked = {
        "remote jobs",
        "remote job",
        "top remote companies",
        "top 100 remote companies",
        "companies",
        "company",
        "find a job",
        "post a job",
        "learn more",
        "read more",
        "view jobs",
        "apply now",
        "subscribe",
        "sign in",
        "log in",
    }
    out: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        clean = " ".join(str(raw or "").split()).strip()
        if not clean:
            return
        low = clean.lower()
        if low in blocked or ("remote" in low and ("company" in low or "companies" in low)):
            return
        if len(clean) < 2 or len(clean) > 40 or not re.search(r"[a-zA-Z]", clean):
            return
        if low in seen:
            return
        seen.add(low)
        out.append(clean)

    for tag in soup.select("nav, header, footer, aside"):
        tag.decompose()

    for node in soup.select('a[href^="/company/"] span.company'):
        add(node.get_text(" ", strip=True))
        if len(out) >= 120:
            return out[:120]
    if out:
        return out[:120]

    for selector in ("main li", "article li", "main h2", "main h3", "article h2", "article h3"):
        for node in soup.select(selector):
            add(node.get_text(" ", strip=True))
        if len(out) >= 120:
            return out[:120]

    return out[:120]


_REMOTE_ES_LINK_RE = re.compile(
    r"\[[ \t]{0,4}Open[ \t]{1,4}pos\w{1,20}[ \t]{0,4}\]\(([^)]{1,500})\)",
    re.IGNORECASE,
)


def _strip_trailing_parens(text: str) -> str:
    cur = text.strip()
    while cur.endswith(")"):
        depth = 0
        open_idx = -1
        for i in range(len(cur) - 1, -1, -1):
            if cur[i] == ")":
                depth += 1
            elif cur[i] == "(":
                depth -= 1
                if depth == 0:
                    open_idx = i
                    break
        if open_idx == -1:
            break
        cur = cur[:open_idx].strip()
    return cur


def extract_company_entries_from_remote_es_markdown(markdown_text: str) -> list[tuple[str, str]]:
    """Extract (company_name, job_board_url) tuples from remote-es README markdown."""
    text = str(markdown_text or "").strip()
    if not text:
        return []

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("* "):
            continue
        match = _REMOTE_ES_LINK_RE.search(line)
        if not match or match.start() <= 2 or not line[match.start() - 1].isspace():
            continue
        company = " ".join(line[2 : match.start()].split()).strip()
        url = match.group(1).strip()
        if not company:
            continue
        company = _strip_trailing_parens(company)
        if len(company) < 2 or len(company) > 70 or not re.search(r"[A-Za-z]", company):
            continue
        low = company.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append((company, url))
    return out


def extract_company_names_from_remotifyeurope_html(html: str, location_names: Iterable[str] = ()) -> list[str]:
    """Extract company names from RemotifyEurope server-rendered job cards.

    ``location_names`` (country/region labels to ignore) is data: it comes from ``feeds.yaml``.
    """
    text = str(html or "").strip()
    if not text:
        return []

    soup = BeautifulSoup(text, "html.parser")
    names: list[str] = []
    seen: set[str] = set()
    blocked = {
        "remote",
        "remotifyeurope",
        "login",
        "sign up",
        "register",
        "post a job",
        "all jobs",
        "new",
        "cookie",
        "privacy",
    }
    location_names = {name.lower() for name in location_names}

    for card in soup.select("[class*=job], [class*=card]"):
        parts = [p.strip() for p in card.get_text(" | ", strip=True).split("|") if p.strip()]
        if len(parts) < 2:
            continue
        candidate = parts[1].strip()
        if len(candidate) < 2 or len(candidate) > 60 or not re.search(r"[a-zA-Z]", candidate):
            continue
        low = candidate.lower()
        if low not in seen and low not in blocked and low not in location_names:
            seen.add(low)
            names.append(candidate)

    return names[:80]


def rows_from_himalayas_payload(payload: dict | list) -> list[dict]:
    """Parse a Himalayas JSON payload into normalized feed rows."""
    jobs: list[dict] = []
    if isinstance(payload, dict):
        raw_jobs = payload.get("jobs")
        if isinstance(raw_jobs, list):
            jobs = [item for item in raw_jobs if isinstance(item, dict)]
    elif isinstance(payload, list):
        jobs = [item for item in payload if isinstance(item, dict)]
    rows: list[dict] = []
    for item in jobs:
        title = str(item.get("title") or item.get("jobTitle") or "").strip()
        company = str(item.get("companyName") or item.get("company_name") or "").strip()
        if not company:
            company_obj = item.get("company")
            if isinstance(company_obj, dict):
                company = str(company_obj.get("name") or "").strip()
            elif isinstance(company_obj, str):
                company = company_obj.strip()
        url = str(item.get("url") or item.get("applyUrl") or item.get("applicationUrl") or "").strip()
        if url:
            row = {"title": title, "company_name": company, "url": url, "source": "himalayas"}
            loc_raw = item.get("locationRestrictions")
            if isinstance(loc_raw, list):
                loc = ", ".join(str(x).strip() for x in loc_raw if str(x or "").strip())
            else:
                loc = str(loc_raw or item.get("location") or "").strip()
            if loc:
                row["location"] = loc
            rows.append(row)
    return rows

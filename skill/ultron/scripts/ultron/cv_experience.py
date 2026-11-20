"""Deterministic CV section splitting and work-experience timeline analysis.

Pure standard library plus ``ultron.core.rules.term_matching``. Designed for untrusted CV
plain text: linear-time regexes only, explicit year bounds, interval merging to avoid
double-counting concurrent roles, separation of internships and education dates, and
multilingual headings/months (EN, ES, FR, DE, PT, IT).
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime

from ultron.core.rules.term_matching import contains_term, normalize_text

# Canonical section headings mapped from accent-folded lowercase labels.
_SECTION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "experience": (
        "experience",
        "work experience",
        "professional experience",
        "relevant experience",
        "employment",
        "employment history",
        "work history",
        "career history",
        "positions held",
        "experiencia",
        "experiencia laboral",
        "experiencia profesional",
        "trayectoria profesional",
        "historial laboral",
        "experience professionnelle",
        "experiences professionnelles",
        "parcours professionnel",
        "berufserfahrung",
        "beruflicher werdegang",
        "berufspraxis",
        "tatigkeiten",
        "taetigkeiten",
        "experiencia profissional",
        "historico profissional",
        "esperienza",
        "esperienza lavorativa",
        "esperienza professionale",
    ),
    "education": (
        "education",
        "academic background",
        "education and training",
        "academics",
        "qualifications",
        "formacion",
        "educacion",
        "formacion academica",
        "estudios",
        "formation",
        "formations",
        "etudes",
        "ausbildung",
        "studium",
        "bildungsweg",
        "formacao",
        "formacao academica",
        "educacao",
        "formazione",
        "istruzione",
    ),
    "skills": (
        "skills",
        "technical skills",
        "core skills",
        "key skills",
        "competencies",
        "core competencies",
        "technologies",
        "tech stack",
        "tools and technologies",
        "habilidades",
        "habilidades tecnicas",
        "competencias",
        "competencias tecnicas",
        "aptitudes",
        "competences",
        "competences techniques",
        "connaissances",
        "fahigkeiten",
        "faehigkeiten",
        "kenntnisse",
        "kompetenzen",
        "competenze",
        "competenze tecniche",
    ),
    "summary": (
        "summary",
        "professional summary",
        "executive summary",
        "profile",
        "professional profile",
        "about",
        "about me",
        "objective",
        "career objective",
        "resumen",
        "resumen profesional",
        "perfil",
        "perfil profesional",
        "sobre mi",
        "objetivo",
        "profil",
        "a propos",
        "kurzprofil",
        "uber mich",
        "ueber mich",
        "resumo",
        "resumo profissional",
        "profilo",
        "profilo professionale",
    ),
    "projects": (
        "projects",
        "key projects",
        "selected projects",
        "personal projects",
        "proyectos",
        "proyectos destacados",
        "projets",
        "projekte",
        "projetos",
        "progetti",
    ),
    "certifications": (
        "certifications",
        "certificates",
        "licenses",
        "licenses and certifications",
        "certifications and licenses",
        "courses and certifications",
        "certificaciones",
        "certificados",
        "licencias y certificaciones",
        "zertifikate",
        "zertifizierungen",
        "certificacoes",
        "certificazioni",
    ),
    "languages": (
        "languages",
        "spoken languages",
        "idiomas",
        "lenguas",
        "langues",
        "sprachen",
        "lingue",
    ),
    "publications": (
        "publications",
        "papers",
        "research",
        "research and publications",
        "publicaciones",
        "publikationen",
        "publicacoes",
        "pubblicazioni",
    ),
}

_HEADING_MAP: dict[str, str] = {
    phrase: section for section, phrases in _SECTION_SYNONYMS.items() for phrase in phrases
}

_MONTH_MAP: dict[str, int] = {
    # English
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
    # Spanish
    "ene": 1, "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abr": 4, "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "ago": 8, "agosto": 8,
    "septiembre": 9, "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "dic": 12, "diciembre": 12,
    # French
    "janv": 1, "janvier": 1,
    "fev": 2, "fevr": 2, "fevrier": 2,
    "mars": 3,
    "avr": 4, "avril": 4,
    "mai": 5,
    "juin": 6,
    "juil": 7, "juillet": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
    # German
    "januar": 1, "jaenner": 1, "janner": 1,
    "februar": 2,
    "maerz": 3, "marz": 3, "mrz": 3,
    "juni": 6,
    "juli": 7,
    "okt": 10, "oktober": 10,
    "dez": 12, "dezember": 12,
    # Portuguese
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "set": 9, "setembro": 9,
    "out": 10, "outubro": 10,
    "dezembro": 12,
    # Italian
    "gen": 1, "gennaio": 1,
    "febbraio": 2,
    "aprile": 4,
    "mag": 5, "maggio": 5,
    "giu": 6, "giugno": 6,
    "lug": 7, "luglio": 7,
    "settembre": 9,
    "ott": 10, "ottobre": 10,
    "dicembre": 12,
}

_PRESENT_TOKENS = (
    "present",
    "presente",
    "current",
    "currently",
    "now",
    "today",
    "ongoing",
    "actualidad",
    "actual",
    "hoy",
    "heute",
    "aktuell",
    "derzeit",
    "actuel",
    "actuellement",
    "atual",
    "atualmente",
    "oggi",
    "corrente",
    "in corso",
)

_MONTH_ALT = "|".join(sorted(_MONTH_MAP, key=len, reverse=True))
_PRESENT_ALT = "|".join(sorted(_PRESENT_TOKENS, key=len, reverse=True))
_YEAR = r"(?:19|20)\d{2}"
_START_ENDPOINT = (
    rf"(?:(?:{_MONTH_ALT})\.?(?:\s+de)?\s+{_YEAR}"
    rf"|{_YEAR}[-/](?:0?[1-9]|1[0-2])\b"
    rf"|(?:0?[1-9]|1[0-2])[/.]{_YEAR}"
    rf"|{_YEAR}(?![-/]\d{{1,2}}(?!\d)))"
)
_END_ENDPOINT = rf"(?:{_START_ENDPOINT}|\b(?:{_PRESENT_ALT})\b)"
_SEP = r"\s*(?:[-–—]+|\b(?:to|bis|au|ate|al|a)\b)\s*"
_RANGE_RE = re.compile(
    rf"(?<!\w)(?P<start>{_START_ENDPOINT}){_SEP}(?P<end>{_END_ENDPOINT})(?!\w)",
    re.ASCII,
)
_MONTH_YEAR_RE = re.compile(rf"^(?P<mon>{_MONTH_ALT})\.?(?:\s+de)?\s+(?P<year>{_YEAR})$")
_ISO_YM_RE = re.compile(rf"^(?P<year>{_YEAR})[-/](?P<mon>0?[1-9]|1[0-2])$")
_SLASH_MY_RE = re.compile(rf"^(?P<mon>0?[1-9]|1[0-2])[/.](?P<year>{_YEAR})$")
_YEAR_ONLY_RE = re.compile(rf"^(?P<year>{_YEAR})$")
_PRESENT_RE = re.compile(rf"^(?:{_PRESENT_ALT})$")

_INTERNSHIP_TERMS = (
    "intern",
    "internship",
    "working student",
    "werkstudent",
    "werkstudentin",
    "werkstudierende",
    "praktikum",
    "praktikant",
    "praktikantin",
    "becario",
    "becaria",
    "practicas",
    "stage",
    "stagiaire",
    "stagiair",
    "trainee",
    "estagio",
    "estagiario",
    "estagiaria",
    "apprentice",
    "apprenticeship",
    "co-op",
    "coop",
)
_INTERNSHIP_ADMIN_RE = re.compile(
    r"\b(?:internship|apprenticeship|traineeship|praktikum)\s+"
    r"(?:coordinator|manager|lead|director|specialist|recruiter)\b"
)
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*•▪▫◦‣]|\d+[.)])\s+")
_MAX_HEADING_CHARS = 60
_MIN_YEAR = 1960

_PHD_TERMS = ("phd", "ph.d", "doctorate", "doctoral", "doctor of philosophy", "doctorado", "doktor")
_MASTERS_TERMS = (
    "master",
    "masters",
    "master's",
    "msc",
    "m.sc",
    "m.s",
    "meng",
    "m.eng",
    "mba",
    "m.a",
    "magister",
    "maestria",
    "posgrado",
    "postgraduate",
    "diplom",
)
_BACHELORS_TERMS = (
    "bachelor",
    "bachelors",
    "bachelor's",
    "bsc",
    "b.sc",
    "b.s",
    "beng",
    "b.eng",
    "b.a",
    "grado",
    "licenciatura",
    "licenciado",
    "graduado",
    "licence",
)
_VOCATIONAL_TERMS = (
    "associate degree",
    "associate of",
    "formacion profesional",
    "ciclo formativo",
    "tecnico superior",
    "ausbildung",
)
_STAFF_PLUS_TERMS = (
    "staff",
    "principal",
    "distinguished",
    "architect",
    "fellow",
    "tech lead",
    "technical lead",
    "team lead",
    "lead",
)
_MANAGEMENT_TERMS = (
    "engineering manager",
    "manager",
    "head of",
    "director",
    "vp",
    "vice president",
    "cto",
    "ceo",
    "cfo",
    "coo",
    "chief",
)
_MENTORING_TERMS = (
    "mentored",
    "mentoring",
    "coached",
    "led a team",
    "led team",
    "managed a team",
    "managed team",
    "direct reports",
    "cross-functional leadership",
    "liderado",
    "mentoria",
)


@dataclass(frozen=True, slots=True)
class YearMonth:
    year: int
    month: int
    year_only: bool = False

    @property
    def index(self) -> int:
        return self.year * 12 + self.month

    def label(self) -> str:
        return str(self.year) if self.year_only else f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True, slots=True)
class ExperienceEntry:
    title_hint: str
    start: YearMonth
    end: YearMonth
    is_present: bool
    months: int
    is_internship: bool
    line_index: int
    date_text: str
    display_end: YearMonth | None = None
    matches_profile: bool | None = None

    @property
    def start_index(self) -> int:
        return self.start.index

    @property
    def end_index(self) -> int:
        return self.end.index


def _fold_with_offsets(text: str) -> tuple[str, list[int]]:
    """Return accent-folded lowercase text plus original character index per folded char."""
    folded: list[str] = []
    offsets: list[int] = []
    for offset, char in enumerate(text):
        for part in unicodedata.normalize("NFKD", char):
            if not unicodedata.combining(part):
                lower = part.lower()
                for sub in lower:
                    folded.append(sub)
                    offsets.append(offset)
    return "".join(folded), offsets


def _clean_heading_candidate(line: str) -> str:
    stripped = line.strip()
    if not stripped or len(stripped) > _MAX_HEADING_CHARS or _BULLET_PREFIX_RE.match(stripped):
        return ""
    stripped = stripped.lstrip("#").strip()
    if stripped.startswith(("**", "__")) and stripped.endswith(("**", "__")) and len(stripped) > 4:
        stripped = stripped[2:-2].strip()
    stripped = stripped.rstrip(":").strip()
    if stripped.endswith(".") and len(stripped.split()) > 3:
        return ""
    folded = " ".join(re.findall(r"[a-z0-9]+", normalize_text(stripped)))
    return folded


def match_section_heading(line: str) -> str | None:
    """Return canonical section name if ``line`` looks like a standalone section heading."""
    candidate = _clean_heading_candidate(line)
    if not candidate:
        return None
    if candidate in _HEADING_MAP:
        return _HEADING_MAP[candidate]
    # Support Markdown/table-like dual headings such as "Experience & Projects".
    for sep in (" and ", " & ", " / "):
        if sep in candidate:
            first = candidate.split(sep, 1)[0].strip()
            if first in _HEADING_MAP:
                return _HEADING_MAP[first]
    return None


def split_sections(text: str) -> tuple[dict[str, list[str]], list[dict[str, object]]]:
    """Split CV text into canonical section buckets and detected heading metadata."""
    sections: dict[str, list[str]] = {"header": []}
    headings: list[dict[str, object]] = []
    current = "header"
    for idx, raw_line in enumerate(str(text or "").splitlines()):
        section = match_section_heading(raw_line)
        if section is not None:
            current = section
            sections.setdefault(current, [])
            headings.append({"line": idx + 1, "heading": raw_line.strip(), "section": section})
            continue
        sections.setdefault(current, []).append(raw_line)
    return sections, headings


def _parse_endpoint(token: str, *, is_end: bool, as_of: date) -> tuple[YearMonth | None, bool]:
    norm = " ".join(token.split())
    if is_end and _PRESENT_RE.fullmatch(norm):
        return YearMonth(as_of.year, as_of.month, year_only=False), True
    max_year = as_of.year + 1
    if match := _MONTH_YEAR_RE.fullmatch(norm):
        year, mon = int(match.group("year")), _MONTH_MAP[match.group("mon")]
        return (YearMonth(year, mon) if _MIN_YEAR <= year <= max_year else None), False
    if match := _ISO_YM_RE.fullmatch(norm):
        year, mon = int(match.group("year")), int(match.group("mon"))
        return (YearMonth(year, mon) if _MIN_YEAR <= year <= max_year else None), False
    if match := _SLASH_MY_RE.fullmatch(norm):
        year, mon = int(match.group("year")), int(match.group("mon"))
        return (YearMonth(year, mon) if _MIN_YEAR <= year <= max_year else None), False
    if match := _YEAR_ONLY_RE.fullmatch(norm):
        year = int(match.group("year"))
        if not (_MIN_YEAR <= year <= max_year):
            return None, False
        return YearMonth(year, 12 if is_end else 1, year_only=True), False
    return None, False


def _resolve_interval(start: YearMonth, end: YearMonth, *, is_present: bool, as_of: date) -> tuple[YearMonth, YearMonth] | None:
    if start.year_only and is_present:
        start = YearMonth(start.year, 1, year_only=False)
        end = YearMonth(as_of.year, as_of.month, year_only=False)
    elif start.year_only and end.year_only:
        if start.year > end.year:
            return None
        if start.year == end.year:
            start = YearMonth(start.year, 1, year_only=True)
            end = YearMonth(start.year, 6, year_only=True)
        else:
            # Y1 - Y2 (year-only) counts (Y2 - Y1) * 12 months: Y1-01 through (Y2-1)-12.
            start = YearMonth(start.year, 1, year_only=True)
            end = YearMonth(end.year - 1, 12, year_only=True)
    elif start.year_only:
        start = YearMonth(start.year, 1, year_only=True)
    elif end.year_only:
        end_month = 12 if end.year > start.year else 6
        end = YearMonth(end.year, end_month, year_only=True)
    if end.index < start.index:
        return None
    return start, end


_TITLE_EDGE_CHARS = " -–—|·•,;:[]#*"


def _clean_title_hint(fragment: str) -> str:
    cleaned = " ".join(fragment.split()).strip(_TITLE_EDGE_CHARS)
    while cleaned.startswith("(") and cleaned.endswith(")") and len(cleaned) >= 2:
        cleaned = cleaned[1:-1].strip(_TITLE_EDGE_CHARS)
    cleaned = cleaned.lstrip(")").rstrip("(").strip(_TITLE_EDGE_CHARS)
    return cleaned if sum(ch.isalpha() for ch in cleaned) >= 2 else ""


def _prev_heading_hint(lines: list[str], line_idx: int, *, max_offset: int) -> str:
    for offset in range(1, max_offset + 1):
        prev_idx = line_idx - offset
        if prev_idx < 0:
            break
        prev = lines[prev_idx].strip()
        if not prev:
            if max_offset > 1:
                continue
            break
        if _BULLET_PREFIX_RE.match(prev) or match_section_heading(prev):
            break
        folded_prev, _ = _fold_with_offsets(prev)
        if _RANGE_RE.search(folded_prev):
            break
        if len(prev) > 100 or (prev.endswith(".") and len(prev.split()) > 6):
            break
        hint = _clean_title_hint(prev)
        if hint:
            return hint
    return ""


def _title_hint_for_line(lines: list[str], line_idx: int, span: tuple[int, int]) -> str:
    line = lines[line_idx]
    before = _clean_title_hint(line[: span[0]])
    after = _clean_title_hint(line[span[1] :])
    same_line = " · ".join(part for part in (before, after) if part)
    if not same_line:
        return _prev_heading_hint(lines, line_idx, max_offset=2)
    prev_hint = _prev_heading_hint(lines, line_idx, max_offset=2 if not before else 1)
    if prev_hint and prev_hint.casefold() not in same_line.casefold():
        return f"{prev_hint} · {same_line}"
    return same_line


def _is_internship_line(text: str) -> bool:
    norm = normalize_text(text)
    if _INTERNSHIP_ADMIN_RE.search(norm):
        return False
    return any(contains_term(norm, term) for term in _INTERNSHIP_TERMS)


def parse_date_ranges_from_lines(
    lines: list[str], *, as_of: date | None = None
) -> list[ExperienceEntry]:
    """Extract normalized date-range experience entries from ``lines``."""
    ref = as_of or datetime.now(UTC).date()
    entries: list[ExperienceEntry] = []
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        folded, offsets = _fold_with_offsets(line)
        if not folded:
            continue
        for match in _RANGE_RE.finditer(folded):
            start_raw, is_start_present = _parse_endpoint(match.group("start"), is_end=False, as_of=ref)
            end_raw, is_present = _parse_endpoint(match.group("end"), is_end=True, as_of=ref)
            if start_raw is None or end_raw is None or is_start_present:
                continue
            resolved = _resolve_interval(start_raw, end_raw, is_present=is_present, as_of=ref)
            if resolved is None:
                continue
            start, end = resolved
            months = (end.index - start.index) + 1
            orig_start = offsets[match.start()]
            orig_end = offsets[match.end() - 1] + 1
            date_text = line[orig_start:orig_end].strip()
            title_hint = _title_hint_for_line(lines, idx, (orig_start, orig_end))
            entries.append(
                ExperienceEntry(
                    title_hint=title_hint,
                    start=start,
                    end=end,
                    is_present=is_present,
                    months=months,
                    is_internship=_is_internship_line(f"{title_hint} {line}"),
                    line_index=idx,
                    date_text=date_text,
                    display_end=end_raw,
                )
            )
            break  # at most one role date range per line
    return entries


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge inclusive ``(start_idx, end_idx)`` month intervals so concurrent jobs count once."""
    if not intervals:
        return []
    sorted_iv = sorted(intervals)
    merged: list[tuple[int, int]] = [sorted_iv[0]]
    for start, end in sorted_iv[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _merged_months(intervals: list[tuple[int, int]]) -> int:
    return sum((end - start) + 1 for start, end in merge_intervals(intervals))


def _career_gaps_months(intervals: list[tuple[int, int]], *, min_gap_months: int = 4) -> int:
    merged = merge_intervals(intervals)
    total = 0
    for (_, prev_end), (next_start, _) in itertools.pairwise(merged):
        gap = next_start - prev_end - 1
        if gap >= min_gap_months:
            total += gap
    return total


def _chronology_status(entries: list[ExperienceEntry]) -> tuple[bool, str]:
    if len(entries) <= 1:
        return True, "single_or_empty"
    rev = all(
        (a.end_index > b.end_index) or (a.end_index == b.end_index and a.start_index >= b.start_index)
        for a, b in itertools.pairwise(entries)
    )
    if rev:
        return True, "reverse_chronological"
    fwd = all(
        (a.start_index < b.start_index) or (a.start_index == b.start_index and a.end_index <= b.end_index)
        for a, b in itertools.pairwise(entries)
    )
    if fwd:
        return False, "chronological"
    return False, "mixed"


def _highest_education(text: str) -> str:
    norm = normalize_text(text)
    if any(contains_term(norm, t) for t in _PHD_TERMS):
        return "phd"
    if any(contains_term(norm, t) for t in _MASTERS_TERMS):
        return "masters"
    if any(contains_term(norm, t) for t in _BACHELORS_TERMS):
        return "bachelors"
    if any(contains_term(norm, t) for t in _VOCATIONAL_TERMS):
        return "associate_or_vocational"
    return "unspecified"


def _leadership_signals(entries: list[ExperienceEntry], work_text: str) -> list[str]:
    signals: list[str] = []
    titles = " ".join(e.title_hint for e in entries if not e.is_internship)
    norm_titles = normalize_text(titles)
    norm_work = normalize_text(work_text)
    if any(contains_term(norm_titles, t) for t in _MANAGEMENT_TERMS):
        signals.append("management_title")
    if any(contains_term(norm_titles, t) for t in _STAFF_PLUS_TERMS):
        signals.append("staff_or_lead_title")
    if any(contains_term(norm_work, t) for t in _MENTORING_TERMS):
        signals.append("team_or_mentoring_scope")
    return signals


def _seniority_band(
    total_years: float,
    internship_months: int,
    entries: list[ExperienceEntry],
    leadership: list[str],
) -> str:
    non_intern = [e for e in entries if not e.is_internship]
    recent_titles = normalize_text(" ".join(e.title_hint for e in non_intern[:2]))
    if not non_intern and internship_months > 0:
        return "intern_entry"
    if any(contains_term(recent_titles, t) for t in ("cto", "ceo", "cfo", "coo", "vp", "vice president", "director", "head of")):
        return "executive"
    if "staff_or_lead_title" in leadership and total_years >= 4.0:
        return "staff_lead"
    if total_years >= 9.0 and leadership:
        return "staff_lead"
    if total_years >= 5.0 or (contains_term(recent_titles, "senior") and total_years >= 3.0):
        return "senior"
    if total_years >= 2.0:
        return "mid"
    if non_intern or total_years >= 0.5:
        return "junior"
    return "intern_entry"


def _profile_terms(profile: dict | None) -> tuple[list[str], list[str]]:
    if not isinstance(profile, dict):
        return [], []
    criteria = profile.get("criteria") if isinstance(profile.get("criteria"), dict) else {}
    role_terms = [
        str(t).strip()
        for key in ("role_focus_terms", "target_titles")
        for t in (criteria.get(key) or [])
        if isinstance(t, str) and t.strip()
    ]
    signal_terms = [
        str(t).strip()
        for key in ("role_signal_terms", "specialties")
        for t in (criteria.get(key) or [])
        if isinstance(t, str) and t.strip()
    ]
    return list(dict.fromkeys(role_terms)), list(dict.fromkeys(signal_terms))


def _role_matches_profile(
    entry: ExperienceEntry,
    lines: list[str],
    role_terms: list[str],
    signal_terms: list[str],
    *,
    next_line_index: int | None = None,
) -> bool:
    if not role_terms and not signal_terms:
        return True
    stop = min(len(lines), next_line_index if next_line_index is not None else entry.line_index + 4, entry.line_index + 6)
    context = " ".join(lines[entry.line_index : stop])
    title_blob = normalize_text(entry.title_hint)
    full_blob = normalize_text(f"{entry.title_hint} {context}")
    if any(contains_term(title_blob, t) for t in role_terms):
        return True
    if any(contains_term(full_blob, t) for t in role_terms):
        return True
    return sum(1 for t in signal_terms if contains_term(full_blob, t)) >= 2


def analyze_experience(
    text: str, *, profile: dict | None = None, as_of: date | None = None
) -> dict[str, object]:
    """Analyze CV text into deduplicated work experience, education, and seniority signals."""
    ref = as_of or datetime.now(UTC).date()
    sections, headings = split_sections(text)
    warnings: list[str] = []
    if "experience" in sections:
        work_lines = sections["experience"]
    else:
        warnings.append("no_explicit_experience_section")
        excluded = {"education", "certifications", "publications", "languages"}
        work_lines = [line for name, lines in sections.items() if name not in excluded for line in lines]

    entries = parse_date_ranges_from_lines(work_lines, as_of=ref)
    role_terms, signal_terms = _profile_terms(profile)
    has_profile = bool(role_terms or signal_terms)

    enriched: list[ExperienceEntry] = []
    for idx, entry in enumerate(entries):
        next_line = entries[idx + 1].line_index if idx + 1 < len(entries) else None
        matches = (
            _role_matches_profile(entry, work_lines, role_terms, signal_terms, next_line_index=next_line)
            if has_profile
            else None
        )
        enriched.append(
            ExperienceEntry(
                title_hint=entry.title_hint,
                start=entry.start,
                end=entry.end,
                is_present=entry.is_present,
                months=entry.months,
                is_internship=entry.is_internship,
                line_index=entry.line_index,
                date_text=entry.date_text,
                display_end=entry.display_end,
                matches_profile=matches,
            )
        )

    work_intervals = [(e.start_index, e.end_index) for e in enriched if not e.is_internship]
    intern_intervals = [(e.start_index, e.end_index) for e in enriched if e.is_internship]
    all_intervals = [(e.start_index, e.end_index) for e in enriched]

    total_months = _merged_months(work_intervals)
    intern_months = _merged_months(intern_intervals)
    combined_months = _merged_months(all_intervals)
    total_years = round(total_months / 12.0, 1)

    relevant_months = (
        _merged_months([(e.start_index, e.end_index) for e in enriched if not e.is_internship and e.matches_profile])
        if has_profile
        else None
    )
    relevant_years = round(relevant_months / 12.0, 1) if relevant_months is not None else None

    merged_work = merge_intervals(work_intervals)
    span_months = (merged_work[-1][1] - merged_work[0][0] + 1) if merged_work else 0
    gaps_months = _career_gaps_months(work_intervals)
    chronology_ok, chronology_order = _chronology_status([e for e in enriched if not e.is_internship] or enriched)

    edu_blob = "\n".join(sections.get("education", [])) or text
    highest_edu = _highest_education(edu_blob)
    work_text = "\n".join(work_lines)
    leadership = _leadership_signals(enriched, work_text)
    seniority = _seniority_band(total_years, intern_months, enriched, leadership)

    return {
        "as_of": ref.isoformat(),
        "total_months": total_months,
        "total_years": total_years,
        "internship_months": intern_months,
        "total_months_including_internships": combined_months,
        "relevant_months": relevant_months,
        "relevant_years": relevant_years,
        "career_span_years": round(span_months / 12.0, 1),
        "career_gaps_months": gaps_months,
        "chronology_ok": chronology_ok,
        "chronology_order": chronology_order,
        "highest_education": highest_edu,
        "leadership_signals": leadership,
        "inferred_seniority_band": seniority,
        "roles_count": len(enriched),
        "roles": [
            {
                "title_hint": e.title_hint,
                "start": e.start.label(),
                "end": "present" if e.is_present else (e.display_end or e.end).label(),
                "months": e.months,
                "is_internship": e.is_internship,
                "date_text": e.date_text,
                **({"matches_profile": e.matches_profile} if e.matches_profile is not None else {}),
            }
            for e in enriched
        ],
        "detected_headings": headings,
        "sections_present": [name for name in sections if name != "header" and any(ln.strip() for ln in sections[name])],
        "warnings": warnings,
    }

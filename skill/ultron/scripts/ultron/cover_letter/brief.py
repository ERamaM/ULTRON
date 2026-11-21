"""Cover-letter preparation brief: CV evidence extraction, target resolution, tone and language."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ultron.core.privacy import redact
from ultron.core.rules.term_matching import contains_term, normalize_text
from ultron.cv import ats as cv_ats
from ultron.cv import ats_target
from ultron.cv import experience as cv_experience
from ultron.cv import extract as cv
from ultron.profiles import store as profiles

VALID_TONES: tuple[str, ...] = ("formal", "direct", "warm")

TONE_GUIDES: dict[str, dict[str, str]] = {
    "formal": {
        "style": "Measured executive register, full sentences, zero contractions, structured opening and closing.",
        "salutation_hint": "Dear Hiring Manager, / Estimado/a responsable de selección,",
        "signoff_hint": "Sincerely, / Atentamente,",
        "length_hint": "3-4 crisp paragraphs (250-350 words total).",
    },
    "direct": {
        "style": "Outcome-first engineering/operator prose, active verbs, concrete proof points early, minimal filler.",
        "salutation_hint": "Hello Hiring Team, / Hola equipo de selección,",
        "signoff_hint": "Best regards, / Un cordial saludo,",
        "length_hint": "3 tight paragraphs + 2-3 optional highlight bullets (200-300 words total).",
    },
    "warm": {
        "style": "Human, collaborative voice connecting genuine product interest with verified delivery track record.",
        "salutation_hint": "Dear Team, / Hola equipo,",
        "signoff_hint": "Warm regards, / Cordialmente,",
        "length_hint": "3 conversational yet grounded paragraphs (220-320 words total).",
    },
}

SUPPORTED_LANGS: tuple[str, ...] = ("en", "es", "de", "fr", "pt", "it")

_LANG_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset({
        "the", "and", "with", "for", "you", "our", "will", "this", "that", "from", "have", "are", "your",
        "experience", "role", "team", "requirements", "responsibilities", "we", "looking",
    }),
    "es": frozenset({
        "para", "con", "los", "las", "una", "por", "como", "sobre", "del", "que", "sus", "tus",
        "experiencia", "puesto", "requisitos", "responsabilidades", "buscamos", "equipo", "trabajar", "empresa",
    }),
    "de": frozenset({
        "und", "mit", "für", "wir", "sie", "eine", "einem", "einen", "sind", "der", "die", "das",
        "erfahrung", "kenntnisse", "aufgaben", "profil", "suchen", "bereich", "entwicklung",
    }),
    "fr": frozenset({
        "pour", "avec", "nous", "vous", "dans", "une", "des", "les", "est", "sont", "sur", "notre",
        "expérience", "poste", "profil", "équipe", "missions", "recherchons", "compétences",
    }),
    "pt": frozenset({
        "para", "com", "uma", "por", "nos", "das", "dos", "que", "em", "como", "sobre", "nossa",
        "experiência", "vaga", "requisitos", "responsabilidades", "procuramos", "equipe", "equipa",
    }),
    "it": frozenset({
        "per", "con", "una", "degli", "delle", "che", "siamo", "nel", "nella", "nostro", "ruolo",
        "esperienza", "requisiti", "competenze", "cerchiamo", "lavoro", "squadra", "azienda",
    }),
}

_EMAIL_RE = cv_ats.EMAIL_RE
_URL_RE = re.compile(r"https?://[^\s<>()\"\]]{3,200}", re.IGNORECASE)
_LANG_WORD_RE = re.compile(r"[a-z\u00e0-\u00ff]{2,24}")
_BULLET_PREFIX_RE = re.compile(r"^[ \t]{0,4}(?:[-*\u2022\u25cf\u25aa]|\d{1,2}[.)])[ \t]{1,4}")
_DIGIT_SIGNAL_RE = re.compile(r"\d{1,12}[%xXkKmM+]{0,2}")
_ROLE_DATE_ONLY_RE = re.compile(r"^[0-9/\-.\s\u2013\u2014()]{4,40}$")
_LOCATION_HINT_RE = re.compile(
    r"\b(?:remote|hybrid|madrid|barcelona|berlin|munich|london|paris|lisbon|milan|rome|amsterdam|"
    r"spain|españa|germany|france|portugal|italy|uk|usa|eu)\b",
    re.IGNORECASE,
)


def detect_language(text: str) -> str:
    """Detect natural language of a job posting among ``SUPPORTED_LANGS`` (defaults to ``en``)."""
    tokens = _LANG_WORD_RE.findall((text or "").lower())
    if not tokens:
        return "en"
    scores = {lang: 0 for lang in SUPPORTED_LANGS}
    for tok in tokens:
        for lang, vocab in _LANG_STOPWORDS.items():
            if tok in vocab:
                scores[lang] += 1
    best_lang = max(SUPPORTED_LANGS, key=lambda code: (scores[code], 1 if code == "en" else 0))
    return best_lang if scores[best_lang] > 0 else "en"


def extract_candidate_header(cv_text: str) -> dict[str, Any]:
    """Extract name, email, phone, location, and public profile links from the CV header."""
    lines = [ln.strip() for ln in (cv_text or "").splitlines() if ln.strip()][:15]
    top_blob = "\n".join(lines)

    email_match = _EMAIL_RE.search(top_blob)
    email = email_match.group(0) if email_match else ""

    phone_match = cv_ats._PHONE_RE.search(top_blob)
    phone = phone_match.group(0).strip() if phone_match else ""

    links: list[str] = []
    for match in _URL_RE.finditer(top_blob):
        clean_url = str(redact(match.group(0).rstrip(".,;)")))
        if clean_url not in links:
            links.append(clean_url)

    name = ""
    location = ""
    for line in lines:
        cleaned = line.strip(" |·-\t")
        if not cleaned:
            continue
        has_contact = bool(_EMAIL_RE.search(cleaned) or _URL_RE.search(cleaned) or cv_ats._PHONE_RE.search(cleaned))
        if (
            not name
            and not has_contact
            and cv_experience.match_section_heading(cleaned) is None
            and len(cleaned) <= 72
            and not cleaned.endswith(".")
        ):
            name = str(redact(cleaned))
            continue
        if not location:
            parts = [seg.strip() for seg in re.split(r"[|·\u2022]+", cleaned) if seg.strip()]
            for part in parts:
                if _EMAIL_RE.search(part) or _URL_RE.search(part) or cv_ats._PHONE_RE.search(part):
                    continue
                if _LOCATION_HINT_RE.search(part) or ("," in part and len(part) <= 48 and part != name):
                    location = str(redact(part))
                    break

    return {
        "name": name,
        "email": str(redact(email)),
        "phone": str(redact(phone)),
        "location": location,
        "links": links[:4],
        "links_text": " | ".join(links[:4]),
    }


def extract_cv_evidence_bullets(
    cv_text: str, *, target_terms: Sequence[str], limit: int = 18
) -> list[dict[str, Any]]:
    """Extract verified CV bullets and achievement sentences ordered by job-term relevance."""
    sections, _headings = cv_experience.split_sections(cv_text or "")
    priority_sections = ("experience", "projects", "achievements", "summary")
    ordered_names = [name for name in priority_sections if name in sections]
    ordered_names.extend(
        name
        for name in sections
        if name not in ordered_names and name not in {"education", "certifications", "languages", "contact"}
    )

    candidates: list[dict[str, Any]] = []
    seen_norm: set[str] = set()
    for section_name in ordered_names:
        for raw_line in sections[section_name]:
            line = _BULLET_PREFIX_RE.sub("", raw_line).strip()
            if len(line) < 18 or _ROLE_DATE_ONLY_RE.match(line):
                continue
            if _EMAIL_RE.search(line) and len(line) < 64:
                continue
            norm = normalize_text(line)
            if norm in seen_norm:
                continue
            seen_norm.add(norm)
            matched = [term for term in target_terms if contains_term(line, term)]
            has_quantifier = bool(_DIGIT_SIGNAL_RE.search(line))
            candidates.append({
                "section": section_name,
                "text": str(redact(line)),
                "matched_terms": matched[:8],
                "score": (len(matched) * 2) + (1 if has_quantifier else 0),
            })

    candidates.sort(key=lambda item: (-item["score"], -len(item["matched_terms"])))
    return [
        {"section": item["section"], "text": item["text"], "matched_terms": item["matched_terms"]}
        for item in candidates[:limit]
    ]


def _extract_job_file_meta(raw: str) -> tuple[str, str, str]:
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    title = ""
    company = ""
    location = ""
    for line in lines[:10]:
        lower = line.lower()
        if lower.startswith("company:"):
            company = line.split(":", 1)[1].strip()
        elif lower.startswith("location:"):
            location = line.split(":", 1)[1].strip()
        elif lower.startswith(("role:", "title:")):
            title = line.split(":", 1)[1].strip()
        elif not title:
            title = line.lstrip("# ").strip()
    return title, company, location


def resolve_job_target(
    *,
    job: str | None = None,
    job_file: Path | None = None,
    profile: dict[str, Any] | None = None,
    profile_slug: str | None = None,
    run_id: str | None = None,
    fetch: bool = True,
) -> dict[str, Any]:
    """Resolve target job requirements from ``--job``, ``--job-file``, or ``--profile``."""
    if job and job_file is not None:
        raise ValueError("pass either --job or --job-file, not both")

    if job_file is not None:
        src = Path(job_file)
        if not src.is_file():
            raise FileNotFoundError(f"job file not found: {src}")
        if src.stat().st_size > cv.MAX_CV_BYTES:
            raise ValueError("job file is too large")
        raw = cv._clean_text(cv._decode_text_bytes(src.read_bytes()), cv.MAX_CV_CHARS)
        title, company, location = _extract_job_file_meta(raw)
        terms = ats_target.extract_job_keywords(title, raw, profile=profile)
        return {
            "mode": "job_file",
            "title": str(redact(title)),
            "company": str(redact(company)),
            "location": str(redact(location)),
            "apply_url": "",
            "posting_id": "",
            "terms": terms,
            "detected_language": detect_language(f"{title}\n{raw}"),
            "description_excerpt": str(redact(raw[:1600])),
            "path": str(src),
        }

    if job:
        job_data = ats_target._find_local_job(job, run_id=run_id)
        mode = "local_run"
        if job_data is None:
            if not job.startswith(("http://", "https://")):
                raise ValueError(f"job {job!r} not found in local runs")
            if not fetch:
                raise ValueError("job not found in local runs and --no-fetch was set")
            job_data = ats_target._fetch_job_url(job, profile_slug=profile_slug)
            mode = "fetched_url"
        title = str(job_data.get("title") or "")
        company = str(job_data.get("company") or "")
        body = str(job_data.get("body_text") or "")
        raw_locs = job_data.get("locations")
        loc_text = str(job_data.get("location") or "")
        if not loc_text and isinstance(raw_locs, list):
            loc_text = ", ".join(str(x) for x in raw_locs[:3] if x)
        terms = ats_target.extract_job_keywords(title, body, profile=profile)
        return {
            "mode": mode,
            "title": str(redact(title)),
            "company": str(redact(company)),
            "location": str(redact(loc_text)),
            "apply_url": str(job_data.get("apply_url") or ""),
            "posting_id": str(job_data.get("identity_key") or ""),
            "terms": terms,
            "detected_language": detect_language(f"{title}\n{body}"),
            "description_excerpt": str(redact(body[:1600])),
        }

    if profile is None:
        raise ValueError("provide a job target via --job, --job-file, or --profile")

    criteria = profile.get("criteria") if isinstance(profile.get("criteria"), dict) else {}
    titles = [str(t) for t in (criteria.get("target_titles") or []) if str(t).strip()]
    terms = ats_target._profile_target_terms(profile)
    prompt_text = str(profile.get("prompt") or profile.get("description") or "")
    return {
        "mode": "profile",
        "title": str(redact(titles[0] if titles else str(profile.get("name") or profile_slug or ""))),
        "company": "",
        "location": "",
        "apply_url": "",
        "posting_id": "",
        "terms": terms,
        "detected_language": detect_language(f"{' '.join(titles)} {prompt_text}"),
        "description_excerpt": str(redact(prompt_text[:1000])),
        "profile": profile_slug,
    }


def _resolve_source_cv(cv_path: Path | None, profile: dict[str, Any] | None) -> Path:
    if cv_path is not None:
        resolved = Path(cv_path)
        if not resolved.is_file():
            raise FileNotFoundError(f"CV file not found: {resolved}")
        return resolved
    if isinstance(profile, dict) and profile.get("source_cv"):
        candidate = Path(str(profile["source_cv"]))
        if not candidate.is_file():
            raise FileNotFoundError(f"profile source_cv not found: {candidate}")
        return candidate
    raise ValueError("provide --cv PATH or a --profile that references an existing source_cv")


def prepare_cover_letter_brief(
    *,
    cv_path: Path | None = None,
    profile_slug: str | None = None,
    job: str | None = None,
    job_file: Path | None = None,
    run_id: str | None = None,
    fetch: bool = True,
    lang: str | None = None,
    tone: str = "direct",
    as_of: date | None = None,
) -> dict[str, Any]:
    """Assemble a concise, verified cover-letter brief from a CV and target job or profile."""
    if tone not in VALID_TONES:
        raise ValueError(f"unsupported tone {tone!r}; choose one of {', '.join(VALID_TONES)}")
    if lang is not None and lang.lower() not in SUPPORTED_LANGS:
        raise ValueError(f"unsupported language {lang!r}; choose one of {', '.join(SUPPORTED_LANGS)}")

    profile = profiles.load_profile(profile_slug) if profile_slug else None
    resolved_cv = _resolve_source_cv(cv_path, profile)
    extracted = cv.extract_cv(resolved_cv)
    ref = as_of or datetime.now(UTC).date()
    exp = cv_experience.analyze_experience(extracted.text, profile=profile, as_of=ref)

    target = resolve_job_target(
        job=job,
        job_file=job_file,
        profile=profile,
        profile_slug=profile_slug,
        run_id=run_id,
        fetch=fetch,
    )
    target_terms = list(target.get("terms") or [])
    matched_terms = [term for term in target_terms if contains_term(extracted.text, term)]
    missing_terms = [term for term in target_terms if term not in matched_terms]

    resolved_lang = lang.lower() if lang else str(target.get("detected_language") or "en")
    header = extract_candidate_header(extracted.text)
    evidence = extract_cv_evidence_bullets(extracted.text, target_terms=target_terms)

    return {
        "as_of": ref.isoformat(),
        "source_cv": str(resolved_cv.resolve()),
        "profile": profile_slug,
        "language": resolved_lang,
        "language_source": "override" if lang else "auto_detected",
        "tone": tone,
        "tone_guide": TONE_GUIDES[tone],
        "candidate": header,
        "verified_experience": {
            "total_months": exp.get("total_months", 0),
            "total_years": exp.get("total_years", 0.0),
            "roles_count": len(exp.get("roles") or []),
            "recent_roles": [
                {
                    "start": r.get("start"),
                    "end": r.get("end"),
                    "months": r.get("months"),
                    "title_line": str(redact(str(r.get("line") or ""))),
                }
                for r in (exp.get("roles") or [])[:5]
            ],
        },
        "target_job": target,
        "keyword_overlap": {
            "matched": matched_terms,
            "missing_from_cv": missing_terms,
        },
        "evidence_bullets": evidence,
        "drafting_constraints": [
            "Write strictly in the resolved language and requested tone.",
            "Every metric, date, credential, company, and technical skill must come from verified CV evidence or confirmed facts.",
            "Do not claim missing_from_cv keywords unless the candidate explicitly confirmed them.",
            "Aim for a crisp single-page letter tailored to target_job.",
        ],
    }

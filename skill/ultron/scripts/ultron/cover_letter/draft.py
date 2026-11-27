"""Parse structured JSON and Markdown cover letter drafts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ultron.cover_letter import brief
from ultron.cv import extract as cv

_BULLET_LINE_RE = re.compile(r"^[ \t]{0,4}(?:[-*\u2022\u25cf])[ \t]{1,4}(.+)$")
_SALUTATION_PREFIXES = ("dear ", "hello ", "hi ", "estimad", "hola ", "sehr geehrte", "hallo ", "bonjour ", "cher ", "caro ", "prezado ", "gentile ")
_SIGNOFF_PREFIXES = (
    "sincerely", "best regards", "kind regards", "warm regards", "regards", "yours sincerely", "yours faithfully",
    "atentamente", "un cordial saludo", "cordialmente", "saludos", "mit freundlichen grüßen", "viele grüße",
    "cordialement", "bien cordialement", "atenciosamente", "cordiali saluti",
)


@dataclass
class CoverLetterDocument:
    """Structured cover-letter content ready for validation and Word rendering."""
    candidate_name: str = ""
    candidate_email: str = ""
    candidate_phone: str = ""
    candidate_location: str = ""
    candidate_links: list[str] = field(default_factory=list)
    date: str = ""
    recipient_name: str = ""
    recipient_title: str = ""
    company: str = ""
    role: str = ""
    subject: str = ""
    salutation: str = ""
    opening: str = ""
    paragraphs: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    closing: str = ""
    signoff: str = ""
    language: str = "en"
    tone: str = "direct"


def _str_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str) and val.strip():
        return [line.strip() for line in val.splitlines() if line.strip()]
    return []


def parse_json_cover_letter(data: dict[str, Any]) -> CoverLetterDocument:
    """Parse a structured cover-letter JSON mapping into :class:`CoverLetterDocument`."""
    if not isinstance(data, dict):
        raise TypeError("JSON cover letter draft must be a JSON object")
    tone = str(data.get("tone") or "direct").strip().lower()
    if tone not in brief.VALID_TONES:
        tone = "direct"
    lang = str(data.get("language") or "en").strip().lower()
    if lang not in brief.SUPPORTED_LANGS:
        lang = "en"

    doc = CoverLetterDocument(
        candidate_name=str(data.get("candidate_name") or "").strip(),
        candidate_email=str(data.get("candidate_email") or "").strip(),
        candidate_phone=str(data.get("candidate_phone") or "").strip(),
        candidate_location=str(data.get("candidate_location") or "").strip(),
        candidate_links=_str_list(data.get("candidate_links")),
        date=str(data.get("date") or "").strip(),
        recipient_name=str(data.get("recipient_name") or "").strip(),
        recipient_title=str(data.get("recipient_title") or "").strip(),
        company=str(data.get("company") or "").strip(),
        role=str(data.get("role") or "").strip(),
        subject=str(data.get("subject") or "").strip(),
        salutation=str(data.get("salutation") or "").strip(),
        opening=str(data.get("opening") or "").strip(),
        paragraphs=_str_list(data.get("paragraphs")),
        highlights=_str_list(data.get("highlights")),
        closing=str(data.get("closing") or "").strip(),
        signoff=str(data.get("signoff") or "").strip(),
        language=lang,
        tone=tone,
    )
    if not (doc.opening or doc.paragraphs or doc.highlights or doc.closing):
        raise ValueError("cover letter JSON draft must include opening, paragraphs, highlights, or closing")
    return doc


def _looks_like_salutation(block: str) -> bool:
    stripped = block.strip()
    if len(stripped) > 72 or "\n" in stripped:
        return False
    lower = stripped.lower()
    return stripped.endswith((",", ":")) and (
        lower.startswith(_SALUTATION_PREFIXES) or len(stripped.split()) <= 5
    )


def _extract_signoff(block: str, candidate_name: str) -> str | None:
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if not lines or len(lines) > 2:
        return None
    first = lines[0]
    lower = first.lower().rstrip(",:!")
    is_signoff = any(lower.startswith(prefix) for prefix in _SIGNOFF_PREFIXES) or (
        first.endswith(",") and len(first) <= 40
    )
    if not is_signoff:
        return None
    if len(lines) == 2 and candidate_name and lines[1].lower() != candidate_name.lower() and len(lines[1]) > 48:
        return None
    return first


def _group_markdown_body(body: str) -> tuple[list[str], list[str]]:
    prose_blocks: list[str] = []
    bullets: list[str] = []
    current_prose: list[str] = []

    def flush_prose() -> None:
        if current_prose:
            prose_blocks.append(" ".join(current_prose).strip())
            current_prose.clear()

    # Keep line breaks inside trailing signoff blocks separated by blank lines.
    raw_chunks = re.split(r"(?:\r?\n){2,}", body.strip()) if body.strip() else []
    for chunk in raw_chunks:
        chunk_lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if not chunk_lines:
            continue
        if all(_BULLET_LINE_RE.match(ln) for ln in chunk_lines):
            flush_prose()
            for ln in chunk_lines:
                m = _BULLET_LINE_RE.match(ln)
                if m:
                    bullets.append(m.group(1).strip())
            continue
        current_bullets: list[str] = []
        plain_lines: list[str] = []
        for ln in chunk_lines:
            m = _BULLET_LINE_RE.match(ln)
            if m:
                current_bullets.append(m.group(1).strip())
            else:
                plain_lines.append(ln)
        if plain_lines:
            # Preserve two-line blocks (e.g. signoff + signature name) when short.
            if len(plain_lines) == 2 and all(len(x) <= 64 for x in plain_lines):
                prose_blocks.append("\n".join(plain_lines))
            else:
                prose_blocks.append(" ".join(plain_lines))
        bullets.extend(current_bullets)

    flush_prose()
    return prose_blocks, bullets


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for idx in range(1, min(len(lines), 80)):
        if lines[idx].strip() != "---":
            continue
        raw_yaml = "\n".join(lines[1:idx])
        try:
            loaded = yaml.safe_load(raw_yaml) if raw_yaml.strip() else {}
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
        if not isinstance(loaded, dict):
            raise TypeError("Markdown cover letter frontmatter must be a YAML mapping")
        return loaded, "\n".join(lines[idx + 1:])
    return {}, text


def parse_markdown_cover_letter(text: str) -> CoverLetterDocument:
    """Parse Markdown with optional YAML frontmatter into :class:`CoverLetterDocument`."""
    meta, body = _split_frontmatter(text or "")
    candidate_name = str(meta.get("candidate_name") or "").strip()
    prose_blocks, bullets = _group_markdown_body(body)

    salutation = str(meta.get("salutation") or "").strip()
    if not salutation and prose_blocks and _looks_like_salutation(prose_blocks[0]):
        salutation = prose_blocks.pop(0)

    signoff = str(meta.get("signoff") or "").strip()
    if not signoff and prose_blocks:
        maybe_signoff = _extract_signoff(prose_blocks[-1], candidate_name)
        if maybe_signoff is not None:
            signoff = maybe_signoff
            prose_blocks.pop()

    # Flatten any preserved 2-line prose block into single flowing paragraph.
    prose_blocks = [" ".join(blk.splitlines()).strip() for blk in prose_blocks if blk.strip()]
    opening = str(meta.get("opening") or "").strip()
    closing = str(meta.get("closing") or "").strip()
    paragraphs = _str_list(meta.get("paragraphs"))
    highlights = _str_list(meta.get("highlights")) or bullets

    if not opening and not paragraphs and not closing:
        if len(prose_blocks) == 1:
            opening = prose_blocks[0]
        elif len(prose_blocks) == 2:
            opening, closing = prose_blocks[0], prose_blocks[1]
        elif len(prose_blocks) >= 3:
            opening = prose_blocks[0]
            paragraphs = prose_blocks[1:-1]
            closing = prose_blocks[-1]

    if not (opening or paragraphs or highlights or closing):
        raise ValueError("cover letter Markdown draft has no body content")

    tone = str(meta.get("tone") or "direct").strip().lower()
    lang = str(meta.get("language") or "en").strip().lower()
    return CoverLetterDocument(
        candidate_name=candidate_name,
        candidate_email=str(meta.get("candidate_email") or "").strip(),
        candidate_phone=str(meta.get("candidate_phone") or "").strip(),
        candidate_location=str(meta.get("candidate_location") or "").strip(),
        candidate_links=_str_list(meta.get("candidate_links")),
        date=str(meta.get("date") or "").strip(),
        recipient_name=str(meta.get("recipient_name") or "").strip(),
        recipient_title=str(meta.get("recipient_title") or "").strip(),
        company=str(meta.get("company") or "").strip(),
        role=str(meta.get("role") or "").strip(),
        subject=str(meta.get("subject") or "").strip(),
        salutation=salutation,
        opening=opening,
        paragraphs=paragraphs,
        highlights=highlights,
        closing=closing,
        signoff=signoff,
        language=lang if lang in brief.SUPPORTED_LANGS else "en",
        tone=tone if tone in brief.VALID_TONES else "direct",
    )


def load_cover_letter_draft(path: Path) -> CoverLetterDocument:
    """Load and parse a `.json` or Markdown (`.md`/`.txt`) cover letter draft."""
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"cover letter draft not found: {src}")
    if src.stat().st_size > cv.MAX_CV_BYTES:
        raise cv.CvExtractionError("cover letter draft is too large", code="too_long")

    raw = cv.decode_text_bytes(src.read_bytes())
    if src.suffix.lower() == ".json":
        return parse_json_cover_letter(json.loads(raw))
    return parse_markdown_cover_letter(raw)

"""Collect visual cues from applied Word paragraph styles during bounded XML parsing."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = WORD_NS + " "
_HEX = re.compile(r"[0-9A-F]{6}")
_PARAGRAPH_BORDERS = {"top", "left", "bottom", "right", "between", "bar", "start", "end"}


def _twips(value: str) -> int | None:
    return int(value) if len(value) <= 6 and value.isascii() and value.isdecimal() else None


@dataclass
class _Cues:
    border: bool | None = None
    shaded: bool = False
    caps: bool = False
    right_tab: bool = False
    alignment: str = ""
    accent: str = ""
    font: str = ""

    def record(self, kind: str, attrs: dict[str, str], *, paragraph_border: bool = False) -> None:
        val = attrs.get(f"{_W}val", "")
        if paragraph_border and kind in _PARAGRAPH_BORDERS and val:
            self.border = bool(self.border) or val not in {"nil", "none"}
        elif kind == "shd":
            self.shaded = val != "nil"
        elif kind == "caps":
            self.caps = val not in {"0", "false", "off"}
        elif kind == "tab":
            self.right_tab |= val == "right"
        elif kind == "jc" and val in {"left", "center"}:
            self.alignment = val
        elif kind == "rFonts" and not self.font:
            self.font = attrs.get(f"{_W}ascii") or attrs.get(f"{_W}hAnsi", "")
        color = (val if kind == "color" else attrs.get(f"{_W}color", "")).upper()
        if not self.accent and _HEX.fullmatch(color) and color not in {"000000", "FFFFFF"}:
            self.accent = color


@dataclass
class _Paragraph:
    style: str = ""
    cues: _Cues = field(default_factory=_Cues)
    text: list[str] = field(default_factory=list)


@dataclass
class _Style:
    base: str = ""
    cues: _Cues = field(default_factory=_Cues)


class DocxLayoutMetrics:
    """Extract safe layout cues while the DOCX reader validates and streams XML."""

    def __init__(self) -> None:
        self.page_width: int | None = None
        self.page_height: int | None = None
        self.margin: int | None = None
        self.header: _Paragraph | None = None
        self.paragraphs: list[_Paragraph] = []
        self.applied: dict[str, _Cues] = {}
        self.paragraph_fonts: dict[str, Counter[str]] = {}
        self.paragraph_borders: dict[str, set[bool | None]] = {}
        self.border_depth = 0
        self.defaults = _Cues()
        self.styles: dict[str, _Style] = {}
        self.default_style = ""
        self.current_style: _Style | None = None

    def start(self, name: str, attrs: dict[str, str]) -> None:
        """Record dimensions, style definitions, and used paragraph properties."""
        if not name.startswith(_W):
            return
        kind = name[len(_W):]
        value = attrs.get(f"{_W}val", "").casefold()
        if kind == "pBdr":
            self.border_depth += 1
        if kind in {"pgSz", "pgMar"}:
            self._page_geometry(kind, attrs)
        elif kind == "p":
            paragraph = _Paragraph()
            self.paragraphs.append(paragraph)
            if self.header is None:
                self.header = paragraph
        elif kind == "style":
            style_id = attrs.get(f"{_W}styleId", "").casefold()
            self.current_style = self.styles.setdefault(style_id, _Style())
            if attrs.get(f"{_W}type") == "paragraph" and attrs.get(f"{_W}default") in {"1", "true"}:
                self.default_style = style_id
        elif self.current_style is not None:
            if kind == "basedOn":
                self.current_style.base = value
            else:
                self.current_style.cues.record(kind, attrs, paragraph_border=self.border_depth > 0)
        elif self.paragraphs:
            paragraph = self.paragraphs[-1]
            if kind == "pStyle":
                paragraph.style = value
            else:
                paragraph.cues.record(kind, attrs, paragraph_border=self.border_depth > 0)
        elif kind == "rFonts":
            self.defaults.record(kind, attrs)

    def _page_geometry(self, kind: str, attrs: dict[str, str]) -> None:
        if kind == "pgSz" and self.page_width is None:
            self.page_width = _twips(attrs.get(f"{_W}w", ""))
            self.page_height = _twips(attrs.get(f"{_W}h", ""))
        elif kind == "pgMar" and self.margin is None:
            left = _twips(attrs.get(f"{_W}left", ""))
            right = _twips(attrs.get(f"{_W}right", ""))
            if left is not None and right is not None:
                self.margin = (left + right) // 2

    def characters(self, data: str) -> None:
        """Record text for estimating whether source headings appear in caps."""
        if self.paragraphs:
            self.paragraphs[-1].text.append(data)

    def end(self, name: str) -> None:
        """Save cues from a completed paragraph or leave a style definition."""
        if name == f"{_W}pBdr":
            self.border_depth -= 1
        if name == f"{_W}style":
            self.current_style = None
        if name != f"{_W}p" or not self.paragraphs:
            return
        paragraph = self.paragraphs.pop()
        cues = paragraph.cues
        cues.caps |= "".join(paragraph.text).isupper()
        paragraph.text.clear()
        self.paragraph_fonts.setdefault(paragraph.style, Counter())[cues.font] += 1
        self.paragraph_borders.setdefault(paragraph.style, set()).add(cues.border)
        combined = self.applied.setdefault(paragraph.style, _Cues())
        combined.shaded |= cues.shaded
        combined.caps |= cues.caps
        combined.right_tab |= cues.right_tab
        combined.accent = combined.accent or cues.accent

    def _chain(self, style_id: str) -> list[str]:
        chain: list[str] = []
        seen: set[str] = set()
        current = style_id or self.default_style
        while current and current not in seen and len(chain) < 64:
            chain.append(current)
            seen.add(current)
            definition = self.styles.get(current)
            current = definition.base if definition else ""
        return chain

    def _used_style_cues(self, chain: list[str]) -> list[_Cues]:
        return [self.styles[style_id].cues for style_id in chain if style_id in self.styles]

    def _applied_format(self, definitions: DocxLayoutMetrics) -> tuple[list[_Cues], bool, bool, str]:
        sections: list[_Cues] = []
        section_styles: dict[str, _Cues] = {}
        fonts: Counter[str] = Counter()
        right_tabs = False
        section_border = False
        for style_id, direct in self.applied.items():
            chain = definitions._chain(style_id)
            inherited = definitions._used_style_cues(chain)
            right_tabs |= direct.right_tab or any(cue.right_tab for cue in inherited)
            default_font = next((cue.font for cue in inherited if cue.font), definitions.defaults.font)
            for font, count in self.paragraph_fonts[style_id].items():
                fonts[font or default_font] += count
            if any(style.startswith("heading") and style != "heading1" for style in chain):
                sections.append(direct)
                default_border = next((cue.border for cue in inherited if cue.border is not None), False)
                section_border |= any(
                    default_border if border is None else border for border in self.paragraph_borders[style_id]
                )
                section_styles.update({
                    style: definitions.styles[style].cues for style in chain if style in definitions.styles
                })
        fonts.pop("", None)
        dominant = fonts.most_common(1)[0][0] if fonts else ""
        return [*sections, *section_styles.values()], right_tabs, section_border, dominant

    def report_fields(self, definitions: DocxLayoutMetrics) -> dict[str, int | str | bool | None]:
        """Return source layout properties taken only from used styles and their bases."""
        sections, right_tabs, section_border, font = self._applied_format(definitions)
        alignment = "left"
        if self.header is not None:
            header = [self.header.cues, *definitions._used_style_cues(definitions._chain(self.header.style))]
            alignment = next((cue.alignment for cue in header if cue.alignment), "left")
        return {
            "page_width_twips": self.page_width,
            "page_height_twips": self.page_height,
            "margin_twips": self.margin,
            "header_align": alignment,
            "accent_color": next((cue.accent for cue in sections if cue.accent), ""),
            "has_section_borders": section_border,
            "has_shaded_headings": any(cue.shaded for cue in sections),
            "has_caps_headings": any(cue.caps for cue in sections),
            "has_right_tabs": right_tabs,
            "dominant_font": font,
        }

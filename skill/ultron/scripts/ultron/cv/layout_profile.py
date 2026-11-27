"""Validate and reconstruct ATS-safe visual layout settings."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from ultron.cv import docx_reader, extract
from ultron.infra.ooxml import WRITER_SAFE_FONTS, resolve_safe_font

PAGE_SIZES = {"letter": (12240, 15840), "a4": (11906, 16838)}
MARGINS = {"compact": 792, "normal": 1080, "wide": 1368}
LAYOUT_CHOICES = {
    "page_size": tuple(PAGE_SIZES),
    "margins": tuple(MARGINS),
    "density": ("compact", "normal", "spacious"),
    "header_align": ("left", "center"),
    "section_style": ("rule", "caps", "clean", "shaded"),
    "date_layout": ("below", "right_tab"),
}
_POINT_BOUNDS = {"body_size_pt": (9, 12), "heading_size_pt": (10.5, 14), "name_size_pt": (14, 22)}


def _linear_color(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _accent(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError("accent_color must be six hex RGB digits.")
    luminance = sum(
        weight * _linear_color(int(value[index:index + 2], 16))
        for index, weight in zip((0, 2, 4), (0.2126, 0.7152, 0.0722), strict=True)
    )
    if 1.05 / (luminance + 0.05) < 4.5:
        raise ValueError("accent_color must have at least 4.5:1 contrast on white.")
    return value.upper()


@dataclass(frozen=True, slots=True)
class CvLayoutStyle:
    """An ATS-safe style with bounded typography and standard page geometry."""

    page_size: str = "letter"
    margins: str = "normal"
    font: str = "Calibri"
    body_size_pt: float = 10.5
    heading_size_pt: float = 11.5
    name_size_pt: float = 17.0
    density: str = "normal"
    accent_color: str = "1F3A5F"
    header_align: str = "left"
    section_style: str = "rule"
    date_layout: str = "below"

    def __post_init__(self) -> None:
        for name, options in LAYOUT_CHOICES.items():
            if getattr(self, name) not in options:
                raise ValueError(f"{name} must be one of: {', '.join(options)}.")
        for name, (low, high) in _POINT_BOUNDS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(f"{name} must be a number between {low} and {high}.")
        if not isinstance(self.font, str):
            raise ValueError("font must be an ATS-safe font name.")  # noqa: TRY004
        object.__setattr__(self, "font", resolve_safe_font(self.font))
        object.__setattr__(self, "accent_color", _accent(self.accent_color))


def load_layout_overrides(path: Path) -> dict[str, Any]:
    """Read a bounded JSON layout object, retaining only explicitly supplied values."""
    with path.open("rb") as stream:
        content = stream.read(16_385)
    if len(content) > 16_384:
        raise ValueError("Layout profile exceeds the 16 KiB limit.")
    data = json.loads(extract.decode_text_bytes(content))
    if not isinstance(data, dict):
        raise ValueError("Layout profile must be a JSON object.")  # noqa: TRY004
    merge_layout_overrides(CvLayoutStyle(), **data)
    return data


def load_layout_profile(path: Path) -> CvLayoutStyle:
    """Load and validate a JSON layout profile."""
    return merge_layout_overrides(CvLayoutStyle(), **load_layout_overrides(path))


def merge_layout_overrides(base: CvLayoutStyle, **overrides: Any) -> CvLayoutStyle:
    """Apply named layout overrides to an existing validated layout."""
    unknown = overrides.keys() - asdict(base).keys()
    if unknown:
        raise ValueError(f"Unknown layout field(s): {', '.join(sorted(unknown))}.")
    return replace(base, **overrides)


def read_layout_source(source: Path) -> bytes:
    """Read a bounded CV source for layout detection and local preview."""
    with source.open("rb") as stream:
        raw = stream.read(extract.MAX_CV_BYTES + 1)
    if len(raw) > extract.MAX_CV_BYTES:
        raise extract.CvExtractionError("CV file is too large", code="too_long")
    return raw


def _closest_page(width: object, height: object, *, scale: int = 1) -> str:
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (width, height)):
        return "letter"
    shortest, longest = sorted((width * scale, height * scale))
    return min(PAGE_SIZES, key=lambda size: math.hypot(shortest - PAGE_SIZES[size][0], longest - PAGE_SIZES[size][1]))


def _font(family: str) -> str:
    key = re.sub(r"[^a-z]", "", family.casefold())
    for safe in sorted(WRITER_SAFE_FONTS, key=len, reverse=True):
        if re.sub(r"[^a-z]", "", safe.casefold()) in key:
            return safe
    if any(word in key for word in ("times", "garamond", "palatino", "liberationserif")):
        return "Times New Roman"
    if any(word in key for word in ("helvetica", "liberationsans", "aptos", "univers")):
        return "Arial"
    return "Calibri"


def _docx_style(meta: dict[str, Any]) -> CvLayoutStyle:
    margin = meta.get("margin_twips")
    fonts = meta.get("fonts") or [""]
    style = "clean"
    for flag, value in (
        ("has_section_borders", "rule"), ("has_caps_headings", "caps"), ("has_shaded_headings", "shaded"),
    ):
        if meta.get(flag):
            style = value
            break
    try:
        accent = _accent(meta.get("accent_color", ""))
    except ValueError:
        accent = "1F3A5F"
    return CvLayoutStyle(
        page_size=_closest_page(meta.get("page_width_twips"), meta.get("page_height_twips")),
        margins=min(MARGINS, key=lambda key: abs(MARGINS[key] - margin)) if isinstance(margin, int) else "normal",
        font=_font(meta.get("dominant_font", fonts[0])),
        header_align="center" if meta.get("header_align") == "center" else "left",
        accent_color=accent,
        section_style=style,
        date_layout="right_tab" if meta.get("has_right_tabs") else "below",
    )


def extract_layout_profile(
    path_or_bytes: Path | bytes, *, file_type: str, format_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect safe source-layout cues in DOCX or PDF without copying its formatting."""
    kind = file_type.casefold().lstrip(".")
    if kind not in {"docx", "pdf"}:
        raise ValueError("Layout detection supports DOCX and PDF.")
    if format_meta is None:
        content = path_or_bytes if isinstance(path_or_bytes, bytes) else read_layout_source(path_or_bytes)
        if kind == "docx":
            format_meta = asdict(docx_reader.inspect_docx(content))
        else:
            _, format_meta = extract.subprocess_pdf_inspector(content)
    if kind == "docx":
        return asdict(_docx_style(format_meta))
    return asdict(CvLayoutStyle(
        page_size=_closest_page(format_meta.get("page_width_pt"), format_meta.get("page_height_pt"), scale=20),
        font=_font(format_meta.get("dominant_font") or next(iter(format_meta.get("fonts") or ("",)))),
    ))

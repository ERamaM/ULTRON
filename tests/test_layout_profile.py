"""Validate safe style controls and source-layout reconstruction."""

import json

import pytest

from ultron.cv import docx_writer, layout_profile
from ultron.cv.layout_profile import CvLayoutStyle


@pytest.mark.parametrize("override", [
    {"page_size": "legal"}, {"section_style": "bordered"}, {"accent_color": "ffffff"},
    {"accent_color": "abc"}, {"font": 123}, {"font": "Unsupported"}, {"body_size_pt": 13},
    {"heading_size_pt": True}, {"name_size_pt": float("nan")}, {"unknown": "value"},
])
def test_invalid_layout(override):
    with pytest.raises(ValueError):
        layout_profile.merge_layout_overrides(CvLayoutStyle(), **override)


def test_json_load_and_partial_override(tmp_path):
    path = tmp_path / "layout.json"
    path.write_text(json.dumps({"page_size": "a4", "accent_color": "2b4c7e"}))
    style = layout_profile.load_layout_profile(path)
    assert (style.page_size, style.accent_color, style.font) == ("a4", "2B4C7E", "Calibri")
    assert layout_profile.load_layout_overrides(path) == {"page_size": "a4", "accent_color": "2b4c7e"}
    path.write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        layout_profile.load_layout_profile(path)
    path.write_bytes(b" " * 16_385)
    with pytest.raises(ValueError, match="limit"):
        layout_profile.load_layout_profile(path)


@pytest.mark.parametrize("section_style", ["clean", "caps", "rule", "shaded"])
def test_docx_roundtrip_safe_layout(section_style):
    draft = docx_writer.parse_markdown_cv("# Jane Example\n\n## Skills\n\nAnalysis")
    style = CvLayoutStyle(
        page_size="a4", margins="compact", font="Arial", header_align="center",
        accent_color="2B4C7E", section_style=section_style,
    )
    content = docx_writer.render_docx_bytes(draft, layout=style)
    detected = layout_profile.extract_layout_profile(content, file_type=".docx")
    for name in ("page_size", "margins", "font", "header_align", "accent_color", "section_style"):
        assert detected[name] == getattr(style, name)


@pytest.mark.parametrize(("font", "expected"), [("Times-Roman", "Times New Roman"), ("Helvetica", "Helvetica"), ("LMRoman", "Calibri")])
def test_detect_a4_pdf_with_font_mapping(font, expected):
    style = layout_profile.extract_layout_profile(
        b"", file_type="pdf", format_meta={"page_width_pt": 595, "page_height_pt": 842, "fonts": [font]},
    )
    assert (style["page_size"], style["font"]) == ("a4", expected)

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



def test_only_applied_styles_and_their_bases_contribute_cues():
    import io
    import zipfile

    from ultron.cv.docx_layout import WORD_NS

    def document(section_style):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("word/document.xml", f"""
                <w:document xmlns:w="{WORD_NS}"><w:body>
                <w:p><w:pPr><w:pStyle w:val="Header"/></w:pPr><w:r><w:t>Jane Example</w:t></w:r></w:p>
                <w:p><w:pPr>{section_style}</w:pPr><w:r><w:t>Experience</w:t></w:r></w:p>
                </w:body></w:document>
            """)
            archive.writestr("word/styles.xml", f"""
                <w:styles xmlns:w="{WORD_NS}">
                  <w:style w:styleId="Header"><w:basedOn w:val="Centered"/></w:style>
                  <w:style w:styleId="Centered"><w:pPr><w:jc w:val="center"/></w:pPr></w:style>
                  <w:style w:styleId="Heading2"><w:basedOn w:val="CycleA"/><w:pPr><w:pBdr><w:bottom w:val="single"/></w:pBdr></w:pPr></w:style>
                  <w:style w:styleId="CycleA"><w:basedOn w:val="Heading2"/>
                    <w:pPr><w:tabs><w:tab w:val="right"/></w:tabs></w:pPr>
                    <w:rPr><w:color w:val="2B4C7E"/><w:rFonts w:ascii="Arial"/></w:rPr>
                  </w:style>
                  <w:style w:styleId="Custom"><w:basedOn w:val="Heading2"/></w:style>
                </w:styles>
            """)
        return raw.getvalue()

    unused = layout_profile.extract_layout_profile(document(""), file_type="docx")
    assert (unused["header_align"], unused["section_style"], unused["date_layout"]) == ("center", "clean", "below")
    assert unused["font"] == "Calibri"
    used = layout_profile.extract_layout_profile(document('<w:pStyle w:val="Custom"/>'), file_type="docx")
    assert (used["section_style"], used["date_layout"], used["accent_color"]) == ("rule", "right_tab", "2B4C7E")
    assert used["font"] == "Arial"



@pytest.mark.parametrize(("base_border", "direct_border", "expected"), [
    ("single", "nil", "clean"), ("single", "none", "clean"), ("single", "", "rule"), ("nil", "", "clean"),
])
def test_inherited_body_font_and_effective_heading_border(base_border, direct_border, expected):
    import io
    import zipfile

    from ultron.cv.docx_layout import WORD_NS

    buffer = io.BytesIO()
    properties = f'<w:pBdr><w:bottom w:val="{direct_border}"/></w:pBdr>' if direct_border else ""
    body = "<w:p><w:r><w:t>Normal body paragraph.</w:t></w:r></w:p>" * 20
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", f"""
            <w:document xmlns:w="{WORD_NS}"><w:body>
            <w:p><w:r><w:rPr><w:rFonts w:ascii="Georgia"/></w:rPr><w:t>Jane Example</w:t></w:r></w:p>
            <w:p><w:pPr><w:pStyle w:val="Heading2"/>{properties}</w:pPr>
                <w:r><w:t>Experience</w:t></w:r></w:p>
            {body}
            </w:body></w:document>
        """)
        archive.writestr("word/styles.xml", f"""
            <w:styles xmlns:w="{WORD_NS}">
              <w:style w:styleId="Normal" w:type="paragraph" w:default="1">
                <w:rPr><w:rFonts w:ascii="Arial"/></w:rPr>
              </w:style>
              <w:style w:styleId="Heading2"><w:basedOn w:val="Normal"/>
                <w:pPr><w:pBdr><w:bottom w:val="{base_border}"/></w:pBdr></w:pPr>
              </w:style>
            </w:styles>
        """)
    detected = layout_profile.extract_layout_profile(buffer.getvalue(), file_type="docx")
    assert detected["font"] == "Arial" and detected["section_style"] == expected



def test_heading_one_sections_are_detected_separately_from_title():
    import io
    import zipfile

    from ultron.cv.docx_layout import WORD_NS

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", f"""
            <w:document xmlns:w="{WORD_NS}"><w:body>
            <w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>Jane Example</w:t></w:r></w:p>
            <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Experience</w:t></w:r></w:p>
            <w:p><w:r><w:t>Worked on reporting.</w:t></w:r></w:p>
            </w:body></w:document>
        """)
        archive.writestr("word/styles.xml", f"""
            <w:styles xmlns:w="{WORD_NS}">
                <w:style w:styleId="Title"><w:rPr><w:color w:val="882222"/></w:rPr></w:style>
                <w:style w:styleId="Heading1"><w:pPr><w:pBdr>
                    <w:bottom w:val="single" w:color="2B4C7E"/>
                </w:pBdr></w:pPr></w:style>
            </w:styles>
        """)
    detected = layout_profile.extract_layout_profile(buffer.getvalue(), file_type="docx")
    assert (detected["section_style"], detected["accent_color"]) == ("rule", "2B4C7E")

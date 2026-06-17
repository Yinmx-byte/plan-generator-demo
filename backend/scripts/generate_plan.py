#!/usr/bin/env python3
"""Render maintenance-plan JSON into a Word document.

The document structure is supplied by the Skill-generated JSON. This script
only provides reusable rendering primitives.
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Pt, RGBColor


DEFAULT_LOGO_PATH = Path(__file__).resolve().parents[1] / "assets" / "state_grid_logo.jpeg"


ALIGNMENTS = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
}


def set_cell_border(cell, **kwargs):
    """Set borders for a table cell."""
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_borders = parse_xml(f'<w:tcBorders {nsdecls("w")}></w:tcBorders>')
    for edge, val in kwargs.items():
        element = parse_xml(
            f'<w:{edge} {nsdecls("w")} w:val="{val.get("val", "single")}" '
            f'w:sz="{val.get("sz", 4)}" w:space="0" '
            f'w:color="{val.get("color", "000000")}"/>'
        )
        tc_borders.append(element)
    tc_pr.append(tc_borders)


def add_table_borders(table):
    for row in table.rows:
        for cell in row.cells:
            for edge in ["top", "left", "bottom", "right"]:
                set_cell_border(
                    cell,
                    **{edge: {"val": "single", "sz": 4, "color": "000000"}},
                )


def set_run_font(run, font_name="仿宋_GB2312", font_size=16, bold=False):
    run.font.name = font_name
    run.font.size = Pt(font_size)
    run.font.color.rgb = RGBColor(0, 0, 0)
    run.bold = bold
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font_name)


def add_paragraph(
    doc,
    text="",
    bold=False,
    font_size=16,
    font_name="仿宋_GB2312",
    alignment=None,
    space_before=14,
    space_after=14,
    first_line_indent=1.12,
):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0 if not text else space_before)
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.line_spacing = Pt(28)
    if first_line_indent is not None:
        p.paragraph_format.first_line_indent = Cm(first_line_indent)
    if alignment is not None:
        p.alignment = alignment
    run = p.add_run(str(text))
    set_run_font(run, font_name=font_name, font_size=font_size, bold=bold)
    return p


def add_heading_text(doc, text, level=1):
    sizes = {0: 22, 1: 16, 2: 16, 3: 16}
    fonts = {0: "方正小标宋_GBK", 1: "黑体", 2: "楷体_GB2312", 3: "仿宋_GB2312"}
    style_name = f"Heading {level}" if level in {1, 2, 3} else None
    p = doc.add_paragraph(style=style_name)
    p.paragraph_format.space_before = Pt(14)
    p.paragraph_format.space_after = Pt(14)
    p.paragraph_format.line_spacing = Pt(28)
    p.paragraph_format.left_indent = Cm({1: 0, 2: 0.56, 3: 1.12}.get(level, 0))
    run = p.add_run(str(text))
    set_run_font(
        run,
        font_name=fonts.get(level, "仿宋_GB2312"),
        font_size=sizes.get(level, 16),
        bold=True,
    )
    return p


def apply_document_defaults(doc):
    style = doc.styles["Normal"]
    font = style.font
    font.name = "仿宋_GB2312"
    font.size = Pt(16)
    font.color.rgb = RGBColor(0, 0, 0)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "仿宋_GB2312")
    style.paragraph_format.space_before = Pt(14)
    style.paragraph_format.space_after = Pt(14)
    style.paragraph_format.line_spacing = Pt(28)
    style.paragraph_format.first_line_indent = Cm(1.12)

    for style_name, font_name in [
        ("Heading 1", "黑体"),
        ("Heading 2", "楷体_GB2312"),
        ("Heading 3", "仿宋_GB2312"),
    ]:
        heading_style = doc.styles[style_name]
        heading_style.font.name = font_name
        heading_style.font.size = Pt(16)
        heading_style.font.bold = True
        heading_style.font.color.rgb = RGBColor(0, 0, 0)
        heading_style.element.rPr.rFonts.set(qn("w:eastAsia"), font_name)
        heading_style.paragraph_format.space_before = Pt(14)
        heading_style.paragraph_format.space_after = Pt(14)
        heading_style.paragraph_format.line_spacing = Pt(28)

    for section in doc.sections:
        section.page_width = Cm(21)
        section.page_height = Cm(29.7)
        section.top_margin = Cm(3.4)
        section.bottom_margin = Cm(3.4)
        section.left_margin = Cm(2.8)
        section.right_margin = Cm(2.8)
        add_page_number(section)


def add_page_number(section):
    """Add a centered Chinese-style page number such as —1—."""
    paragraph = section.footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.first_line_indent = None
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)

    prefix = paragraph.add_run("—")
    set_run_font(prefix, font_name="宋体", font_size=14)
    page_run = paragraph.add_run()
    set_run_font(page_run, font_name="宋体", font_size=14)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    cached_value = OxmlElement("w:t")
    cached_value.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    page_run._r.extend([begin, instruction, separate, cached_value, end])
    suffix = paragraph.add_run("—")
    set_run_font(suffix, font_name="宋体", font_size=14)


def add_simple_table(doc, block: dict[str, Any]):
    columns = block.get("columns", [])
    rows = block.get("rows", [])
    if not columns:
        return

    table = doc.add_table(rows=len(rows) + 1, cols=len(columns))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    add_table_borders(table)
    font_size = int(block.get("font_size", 14))

    for col_idx, column in enumerate(columns):
        cell = table.rows[0].cells[col_idx]
        cell.text = str(column.get("label", column.get("key", "")))
        for paragraph in cell.paragraphs:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.first_line_indent = None
            paragraph.paragraph_format.space_before = Pt(14)
            paragraph.paragraph_format.space_after = Pt(14)
            paragraph.paragraph_format.line_spacing = Pt(28)
            for run in paragraph.runs:
                set_run_font(run, font_name="宋体", font_size=font_size, bold=True)

    for row_idx, row in enumerate(rows, 1):
        for col_idx, column in enumerate(columns):
            key = column.get("key", column.get("label", ""))
            value = row.get(key, "") if isinstance(row, dict) else ""
            cell = table.rows[row_idx].cells[col_idx]
            cell.text = str(value)
            for paragraph in cell.paragraphs:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                paragraph.paragraph_format.first_line_indent = None
                paragraph.paragraph_format.space_before = Pt(14)
                paragraph.paragraph_format.space_after = Pt(14)
                paragraph.paragraph_format.line_spacing = Pt(28)
                for run in paragraph.runs:
                    set_run_font(run, font_name="宋体", font_size=font_size)


def add_checkbox_group(doc, block: dict[str, Any]):
    items = block.get("items", [])
    per_line = int(block.get("per_line", 2))
    row_lengths = block.get("row_lengths") or []
    rendered = []
    for item in items:
        label = item.get("label", "")
        checked = item.get("checked", False)
        extra = item.get("extra", "")
        mark = "■" if checked else "□"
        rendered.append(f"{mark}  {label}{('  ' + extra) if extra else ''}")

    if row_lengths:
        offset = 0
        for row_length in row_lengths:
            row = rendered[offset : offset + int(row_length)]
            if row:
                add_paragraph(doc, "    ".join(row))
            offset += int(row_length)
        return
    for idx in range(0, len(rendered), per_line):
        add_paragraph(doc, "    ".join(rendered[idx : idx + per_line]))


def strip_manual_list_number(text: Any) -> str:
    """Remove model-supplied numbering before renderer adds its own."""
    value = str(text or "").strip()
    return re.sub(r"^\s*(?:\d+[\u3001、.,，．]|[（(]\d+[）)]|[一二三四五六七八九十]+[\u3001、])\s*", "", value)


def render_block(doc, block: dict[str, Any]):
    block_type = block.get("type", "paragraph")

    if block_type == "heading":
        add_heading_text(doc, block.get("text", ""), level=int(block.get("level", 1)))
    elif block_type == "paragraph":
        add_paragraph(
            doc,
            block.get("text", ""),
            bold=bool(block.get("bold", False)),
            font_size=int(block.get("font_size", 16)),
            alignment=ALIGNMENTS.get(block.get("align")),
            first_line_indent=1.12,
        )
    elif block_type == "paragraphs":
        for text in block.get("items", []):
            add_paragraph(doc, text, first_line_indent=1.12)
    elif block_type == "numbered_list":
        for idx, text in enumerate(block.get("items", []), 1):
            add_paragraph(doc, f"{idx}、{strip_manual_list_number(text)}", first_line_indent=1.12)
    elif block_type == "plain_list":
        prefix = block.get("prefix", "")
        for text in block.get("items", []):
            add_paragraph(doc, f"{prefix}{text}", first_line_indent=1.12)
    elif block_type == "key_values":
        for item in block.get("items", []):
            add_paragraph(
                doc,
                f'{item.get("label", "")}{item.get("separator", "：")}{item.get("value", "")}',
                first_line_indent=1.12,
            )
    elif block_type == "checkbox_group":
        add_checkbox_group(doc, block)
    elif block_type == "table":
        add_simple_table(doc, block)
    elif block_type == "spacer":
        add_paragraph(doc, "")


def normalize_text_items(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, str) and item.strip():
                items.append(item.strip())
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("description")
                if text:
                    items.extend(normalize_text_items(text))
        return items
    return [str(value).strip()] if str(value).strip() else []


def normalize_block(block: Any) -> list[dict[str, Any]]:
    if isinstance(block, str):
        return [{"type": "paragraph", "text": block}]
    if not isinstance(block, dict):
        return []
    if "type" in block:
        return [block]
    if "columns" in block and "rows" in block:
        return [{"type": "table", **block}]
    if "items" in block:
        return [{"type": "paragraphs", "items": normalize_text_items(block.get("items"))}]
    text = block.get("text") or block.get("content") or block.get("description")
    if text:
        return [{"type": "paragraph", "text": text}]
    return []


def normalize_section_blocks(section: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for block in section.get("blocks") or []:
        blocks.extend(normalize_block(block))

    if not blocks:
        for key in ("content", "body", "text", "description"):
            for text in normalize_text_items(section.get(key)):
                blocks.append({"type": "paragraph", "text": text})
        for key in ("items", "points", "steps"):
            items = normalize_text_items(section.get(key))
            if items:
                blocks.append({"type": "numbered_list" if key == "steps" else "paragraphs", "items": items})

    children = section.get("children") or section.get("subsections") or []
    for child in children:
        if isinstance(child, str):
            blocks.append({"type": "paragraph", "text": child})
            continue
        if not isinstance(child, dict):
            continue
        child_title = child.get("heading") or child.get("title")
        if child_title:
            blocks.append({"type": "heading", "text": child_title, "level": int(child.get("level", 2))})
        blocks.extend(normalize_section_blocks(child))
    return blocks


SECTION_NUMERALS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]


def numbered_section_heading(text: Any, index: int) -> str:
    heading = str(text or "").strip()
    if not heading:
        return heading
    if "、" in heading[:3]:
        return heading
    if index < len(SECTION_NUMERALS):
        return f"{SECTION_NUMERALS[index]}、{heading}"
    return heading


def render_document(doc, spec: dict[str, Any]):
    title = spec.get("title", "检修方案")
    cover = spec.get("cover", {})
    if cover is not False:
        logo_path = Path(cover.get("logo_path") or DEFAULT_LOGO_PATH)
        if logo_path.exists():
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            run = p.add_run()
            run.add_picture(str(logo_path), width=Cm(float(cover.get("logo_width_cm", 3.1))))
        for _ in range(int(cover.get("top_spacers", 7))):
            add_paragraph(doc, "", space_before=0, space_after=0, first_line_indent=None)
        add_paragraph(
            doc,
            title,
            bold=True,
            font_size=int(spec.get("title_font_size", cover.get("title_font_size", 22))),
            font_name=cover.get("title_font_name", "方正小标宋_GBK"),
            alignment=WD_ALIGN_PARAGRAPH.CENTER,
            space_before=0,
            space_after=0,
            first_line_indent=None,
        )
        for _ in range(int(cover.get("middle_spacers", 5))):
            add_paragraph(doc, "", space_before=0, space_after=0, first_line_indent=None)
        header = spec.get("header", [])
        for line in header:
            if isinstance(line, str):
                line = {"text": line}
            add_paragraph(
                doc,
                line.get("text", ""),
                font_size=16,
                font_name="仿宋_GB2312",
                alignment=ALIGNMENTS.get(line.get("align", "center")),
                space_before=0,
                space_after=0,
                first_line_indent=None,
            )
        doc.add_page_break()
    else:
        add_paragraph(
            doc,
            title,
            bold=True,
            font_size=int(spec.get("title_font_size", 22)),
            font_name=spec.get("title_font_name", "方正小标宋_GBK"),
            alignment=WD_ALIGN_PARAGRAPH.CENTER,
            first_line_indent=None,
        )

        for line in spec.get("header", []):
            if isinstance(line, str):
                line = {"text": line}
            add_paragraph(
                doc,
                line.get("text", ""),
                font_size=int(line.get("font_size", 16)),
                font_name=line.get("font_name", "仿宋_GB2312"),
                alignment=ALIGNMENTS.get(line.get("align", "center")),
                first_line_indent=None,
            )

    for section_index, section in enumerate(spec.get("sections") or spec.get("chapters") or []):
        if isinstance(section, str):
            section = {"blocks": [{"type": "paragraph", "text": section}]}
        if not isinstance(section, dict):
            continue
        heading = section.get("heading") or section.get("title")
        if heading:
            level = int(section.get("level", 1))
            if level == 1:
                heading = numbered_section_heading(heading, section_index)
            add_heading_text(doc, heading, level=level)
        for block in normalize_section_blocks(section):
            render_block(doc, block)

def build_document(data):
    """Build a Word document from Skill-generated document instructions."""
    doc = Document()
    apply_document_defaults(doc)

    spec = data.get("document")
    if not isinstance(spec, dict):
        raise ValueError("document must be an object following the Skill rendering contract")
    render_document(doc, spec)
    return doc


def main():
    parser = argparse.ArgumentParser(description="生成国网云平台检修方案 Word 文档")
    parser.add_argument("--data", required=True, help="JSON 数据文件路径")
    parser.add_argument("--output", required=True, help="输出 .docx 文件路径")
    args = parser.parse_args()

    with open(args.data, "r", encoding="utf-8") as f:
        data = json.load(f)

    doc = build_document(data)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(args.output)
    print(f"检修方案已生成: {args.output}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Render maintenance-plan JSON into a Word document.

The document structure is supplied by the Skill-generated JSON. This script
only provides reusable rendering primitives.
"""

import argparse
import json
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Pt


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


def set_run_font(run, font_name="宋体", font_size=12, bold=False):
    run.font.name = font_name
    run.font.size = Pt(font_size)
    run.bold = bold
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font_name)


def add_paragraph(
    doc,
    text="",
    bold=False,
    font_size=12,
    font_name="宋体",
    alignment=None,
    space_after=6,
    first_line_indent=None,
):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    if first_line_indent:
        p.paragraph_format.first_line_indent = Cm(first_line_indent)
    if alignment is not None:
        p.alignment = alignment
    run = p.add_run(str(text))
    set_run_font(run, font_name=font_name, font_size=font_size, bold=bold)
    return p


def add_heading_text(doc, text, level=1):
    sizes = {0: 16, 1: 14, 2: 12, 3: 12}
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(12 if level <= 2 else 6)
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run(str(text))
    set_run_font(run, font_size=sizes.get(level, 12), bold=True)
    return p


def apply_document_defaults(doc):
    style = doc.styles["Normal"]
    font = style.font
    font.name = "宋体"
    font.size = Pt(12)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    for section in doc.sections:
        section.top_margin = Cm(2.54)
        section.bottom_margin = Cm(2.54)
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2.5)


def add_simple_table(doc, block: dict[str, Any]):
    columns = block.get("columns", [])
    rows = block.get("rows", [])
    if not columns:
        return

    table = doc.add_table(rows=len(rows) + 1, cols=len(columns))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    add_table_borders(table)

    for col_idx, column in enumerate(columns):
        cell = table.rows[0].cells[col_idx]
        cell.text = str(column.get("label", column.get("key", "")))
        for paragraph in cell.paragraphs:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in paragraph.runs:
                set_run_font(run, font_size=9, bold=True)

    for row_idx, row in enumerate(rows, 1):
        for col_idx, column in enumerate(columns):
            key = column.get("key", column.get("label", ""))
            value = row.get(key, "") if isinstance(row, dict) else ""
            cell = table.rows[row_idx].cells[col_idx]
            cell.text = str(value)
            for paragraph in cell.paragraphs:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in paragraph.runs:
                    set_run_font(run, font_size=9)


def add_checkbox_group(doc, block: dict[str, Any]):
    items = block.get("items", [])
    per_line = int(block.get("per_line", 2))
    rendered = []
    for item in items:
        label = item.get("label", "")
        checked = item.get("checked", False)
        extra = item.get("extra", "")
        mark = "■" if checked else "□"
        rendered.append(f"{mark}  {label}{('  ' + extra) if extra else ''}")

    for idx in range(0, len(rendered), per_line):
        add_paragraph(doc, "    ".join(rendered[idx : idx + per_line]), first_line_indent=0.74)


def render_block(doc, block: dict[str, Any]):
    block_type = block.get("type", "paragraph")

    if block_type == "heading":
        add_heading_text(doc, block.get("text", ""), level=int(block.get("level", 1)))
    elif block_type == "paragraph":
        add_paragraph(
            doc,
            block.get("text", ""),
            bold=bool(block.get("bold", False)),
            font_size=int(block.get("font_size", 12)),
            alignment=ALIGNMENTS.get(block.get("align")),
            first_line_indent=block.get("first_line_indent"),
        )
    elif block_type == "paragraphs":
        for text in block.get("items", []):
            add_paragraph(doc, text, first_line_indent=block.get("first_line_indent"))
    elif block_type == "numbered_list":
        for idx, text in enumerate(block.get("items", []), 1):
            add_paragraph(doc, f"{idx}、{text}", first_line_indent=block.get("first_line_indent"))
    elif block_type == "plain_list":
        prefix = block.get("prefix", "")
        for text in block.get("items", []):
            add_paragraph(doc, f"{prefix}{text}", first_line_indent=block.get("first_line_indent"))
    elif block_type == "key_values":
        for item in block.get("items", []):
            add_paragraph(
                doc,
                f'{item.get("label", "")}{item.get("separator", "：")}{item.get("value", "")}',
                first_line_indent=block.get("first_line_indent", 0.74),
            )
    elif block_type == "checkbox_group":
        add_checkbox_group(doc, block)
    elif block_type == "table":
        add_simple_table(doc, block)
    elif block_type == "spacer":
        add_paragraph(doc, "")


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
            add_paragraph(doc, "", space_after=6)
        add_paragraph(
            doc,
            title,
            bold=True,
            font_size=int(spec.get("title_font_size", cover.get("title_font_size", 18))),
            alignment=WD_ALIGN_PARAGRAPH.CENTER,
            space_after=12,
        )
        for _ in range(int(cover.get("middle_spacers", 8))):
            add_paragraph(doc, "", space_after=6)
        header = spec.get("header", [])
        for line in header:
            if isinstance(line, str):
                line = {"text": line}
            add_paragraph(
                doc,
                line.get("text", ""),
                font_size=int(line.get("font_size", 12)),
                alignment=ALIGNMENTS.get(line.get("align", "center")),
                space_after=int(line.get("space_after", 6)),
            )
        doc.add_page_break()
    else:
        add_paragraph(
            doc,
            title,
            bold=True,
            font_size=int(spec.get("title_font_size", 18)),
            alignment=WD_ALIGN_PARAGRAPH.CENTER,
            space_after=12,
        )

        for line in spec.get("header", []):
            if isinstance(line, str):
                line = {"text": line}
            add_paragraph(
                doc,
                line.get("text", ""),
                font_size=int(line.get("font_size", 12)),
                alignment=ALIGNMENTS.get(line.get("align", "center")),
                space_after=int(line.get("space_after", 6)),
            )

    for section in spec.get("sections", []):
        heading = section.get("heading")
        if heading:
            add_heading_text(doc, heading, level=int(section.get("level", 1)))
        for block in section.get("blocks", []):
            render_block(doc, block)


def legacy_to_document_spec(data: dict[str, Any]) -> dict[str, Any]:
    """Convert the original hard-coded schema into the Skill-driven schema."""
    mt = data.get("maintenance_type", {})
    env = data.get("environment", {})
    schedule = data.get("schedule", {})
    personnel = data.get("personnel", {})
    risk = data.get("risk_assessment", {})
    danger = risk.get("danger_points", {})
    safety = data.get("safety_measures", {})
    impl = data.get("implementation_steps", {})
    rollback = data.get("rollback", {})
    tables = data.get("tables", {})

    type_names = ["配置变更", "组件升级", "组件扩缩容", "数据库变更", "日常维护（原硬件设备）", "其他"]
    checkbox_items = []
    for type_name in type_names:
        value = mt.get(type_name, False)
        checkbox_items.append(
            {
                "label": type_name,
                "checked": bool(value) if type_name != "其他" else bool(value),
                "extra": str(value) if type_name == "其他" and isinstance(value, str) else "",
            }
        )

    instance_blocks = [{"type": "paragraph", "text": "（4）涉及的组件实例信息：", "first_line_indent": 0.74}]
    for idx, inst in enumerate(env.get("instances", []), 1):
        instance_blocks.extend(
            [
                {"type": "paragraph", "text": f'{idx}、{inst.get("name", "")}'},
                {"type": "paragraph", "text": f'组织：{inst.get("organization", "")}', "first_line_indent": 0.74},
                {"type": "paragraph", "text": f'资源集：{inst.get("resource_set", "")}', "first_line_indent": 0.74},
            ]
        )

    sections = [
        {
            "heading": "一、背景",
            "blocks": [
                {"type": "numbered_list", "items": data.get("background", []), "first_line_indent": 0.74},
                {"type": "paragraph", "text": "以上事项项目组提报问题工单，需检修进行处理，实现问题工单闭环。", "first_line_indent": 0.74},
            ],
        },
        {"heading": "二、检修类型", "blocks": [{"type": "checkbox_group", "items": checkbox_items, "per_line": 2}]},
        {
            "heading": "三、现场环境",
            "blocks": [
                {
                    "type": "key_values",
                    "items": [
                        {"label": "（1）内网环境/外网环境", "value": env.get("network", "")},
                        {"label": "（2）实施地点", "value": env.get("location", "")},
                        {"label": "（3）专有云版本", "value": env.get("cloud_version", "")},
                    ],
                },
                *instance_blocks,
            ],
        },
        {
            "heading": "四、实施计划",
            "blocks": [
                {"type": "paragraph", "text": "4.1 检修窗口", "bold": True},
                {
                    "type": "table",
                    "columns": [
                        {"key": "year", "label": "年份"},
                        {"key": "start_time", "label": "开始时间"},
                        {"key": "end_time", "label": "结束时间"},
                    ],
                    "rows": [schedule],
                },
                {"type": "spacer"},
                {"type": "paragraph", "text": "4.2 实施人员", "bold": True},
                {
                    "type": "table",
                    "columns": [
                        {"key": "provider", "label": "方案提供人"},
                        {"key": "executor", "label": "检修执行人"},
                        {"key": "reviewer", "label": "检修复核人"},
                        {"key": "business_participant", "label": "业务系统参与人"},
                        {"key": "security_officer", "label": "安全责任人"},
                    ],
                    "rows": [personnel],
                },
            ],
        },
        {
            "heading": "五、风险评估",
            "blocks": [
                {"type": "paragraph", "text": "5.1影响范围", "bold": True},
                {"type": "paragraphs", "items": risk.get("impacts", [])},
                {"type": "paragraph", "text": "5.2危险点分析", "bold": True},
                {
                    "type": "paragraphs",
                    "items": [
                        f'（1）授权不当危险点：{danger.get("auth", "")}',
                        f'（2）备份不当危险点：{danger.get("backup", "")}',
                        f'（3）验证不当危险点：{danger.get("verify", "")}',
                        f'（4）双人复核不当危险点：{danger.get("double_check", "")}',
                    ],
                },
                {"type": "paragraph", "text": "5.3安全措施", "bold": True},
                {"type": "paragraph", "text": "5.3.1授权"},
                {
                    "type": "paragraphs",
                    "items": [
                        f'ASCM：{safety.get("auth", {}).get("ascm_scope", "")}     授权账号：{safety.get("auth", {}).get("ascm_account", "")}',
                        f'堡垒机账号：{safety.get("auth", {}).get("bastion_account", "")}',
                    ],
                },
                {"type": "paragraph", "text": "5.3.2备份"},
                {"type": "plain_list", "prefix": "", "items": [f"({i}){item}" for i, item in enumerate(safety.get("backup", []), 1)]},
                {"type": "paragraph", "text": "5.3.3验证"},
                {"type": "plain_list", "prefix": "", "items": [f"({i}){item}" for i, item in enumerate(safety.get("verify", []), 1)]},
                {"type": "paragraph", "text": "5.3.4 双人复核"},
                {"type": "plain_list", "prefix": "", "items": [f"({i}){item}" for i, item in enumerate(safety.get("double_check", []), 1)]},
            ],
        },
        {
            "heading": "六、实施步骤",
            "blocks": [
                {"type": "paragraph", "text": "6.1备份", "bold": True},
                {"type": "numbered_list", "items": impl.get("backup", [])},
                {"type": "paragraph", "text": "6.2 检修前验证", "bold": True},
                {"type": "numbered_list", "items": impl.get("pre_check", [])},
                {"type": "paragraph", "text": "6.3 检修操作", "bold": True},
                *operation_blocks(impl.get("operations", [])),
                {"type": "paragraph", "text": "6.4 检修后验证", "bold": True},
                {"type": "numbered_list", "items": impl.get("post_check", [])},
            ],
        },
        {
            "heading": "七、回滚步骤",
            "blocks": [
                {"type": "paragraph", "text": "7.1 回滚操作", "bold": True},
                {"type": "numbered_list", "items": rollback.get("operations", [])},
                {"type": "paragraph", "text": "7.2 回滚后验证", "bold": True},
                {"type": "numbered_list", "items": rollback.get("verify", [])},
            ],
        },
    ]

    sections.extend(table_sections(tables))
    return {
        "title": data.get("title", "检修方案"),
        "header": [
            {"text": data.get("department", "云运营中心平台运维处"), "font_size": 14},
            {"text": data.get("date", ""), "font_size": 12, "space_after": 12},
        ],
        "sections": sections,
    }


def operation_blocks(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = []
    for idx, operation in enumerate(operations, 1):
        blocks.append({"type": "paragraph", "text": f'6.3.{idx} {operation.get("title", "")}'})
        blocks.append({"type": "paragraphs", "items": operation.get("steps", [])})
    return blocks


def table_sections(tables: dict[str, Any]) -> list[dict[str, Any]]:
    table_defs = [
        (
            "ECS实例参数表：",
            "ecs",
            [
                ("cloud_env", "云环境"),
                ("instance_name", "实例名称"),
                ("disk", "磁盘"),
                ("image", "自定义镜像"),
                ("password", "密码"),
                ("vpc", "VPC ID或名称"),
                ("vswitch", "Vswitch ID或名称"),
                ("security_group", "安全组"),
                ("spec", "实例规格"),
                ("count", "数量"),
            ],
        ),
        (
            "PolarDB实例参数表：",
            "polardb",
            [
                ("instance_name", "实例名称"),
                ("spec", "实例规格"),
                ("storage", "存储空间"),
                ("chip_arch", "芯片架构"),
                ("db_type", "数据库类型"),
                ("db_version", "数据库版本"),
                ("vpc", "VPC ID或名称"),
                ("vswitch", "Vswitch ID或名称"),
                ("whitelist", "白名单"),
                ("count", "数量"),
                ("env", "环境"),
            ],
        ),
        (
            "MongoDB实例参数表：",
            "mongodb",
            [
                ("instance_name", "实例名称"),
                ("instance_id", "实例id"),
                ("current_spec", "当前规格"),
                ("target_spec", "目标规格"),
                ("env", "环境"),
            ],
        ),
        (
            "维护性重启节点参数表：",
            "restart_nodes",
            [
                ("department", "部门"),
                ("resource_set", "资源集"),
                ("instance_name", "实例name"),
                ("instance_ip", "实例IP"),
                ("env", "环境"),
            ],
        ),
    ]

    sections = []
    for title, key, columns in table_defs:
        rows = tables.get(key, [])
        if not rows:
            continue
        sections.append(
            {
                "blocks": [
                    {"type": "spacer"},
                    {"type": "paragraph", "text": title, "bold": True},
                    {
                        "type": "table",
                        "columns": [{"key": col_key, "label": label} for col_key, label in columns],
                        "rows": rows,
                    },
                ],
            }
        )
    return sections


def build_document(data):
    """Build a Word document from Skill-generated document instructions."""
    doc = Document()
    apply_document_defaults(doc)

    spec = data.get("document")
    if spec is None:
        spec = legacy_to_document_spec(data)
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

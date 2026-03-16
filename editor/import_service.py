import re
from collections.abc import Iterable

from docx import Document as DocxDocument
from docx.document import Document as DocxDocumentType
from docx.enum.section import WD_ORIENT, WD_SECTION_START
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from .document_schema import normalize_document_content, normalize_document_metadata


_HEADING_STYLE_RE = re.compile(r"heading\s+(\d+)", re.IGNORECASE)
_BULLET_STYLE_HINTS = ("bullet", "list bullet")
_ORDERED_STYLE_HINTS = ("number", "list number", "decimal", "roman")


def import_docx_to_tiptap(source) -> dict:
    package = import_docx_package(source)
    return package["content"]


def import_docx_package(source) -> dict:
    doc = DocxDocument(source)
    content: list[dict] = []
    current_list: dict | None = None

    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            paragraph_node, list_kind, list_identity, page_breaks = _paragraph_to_node(block)
            if list_kind:
                list_type = "bulletList" if list_kind == "bullet" else "orderedList"
                if (
                    not current_list
                    or current_list.get("type") != list_type
                    or (current_list.get("attrs") or {}).get("list_identity") != list_identity
                ):
                    if current_list:
                        content.append(current_list)
                    current_list = {
                        "type": list_type,
                        "attrs": {
                            "list_identity": list_identity or {},
                            "word_style": (paragraph_node.get("attrs") or {}).get("word_style") or {},
                            "paragraph_metrics": {},
                        },
                        "content": [],
                    }
                current_list["content"].append(
                    {
                        "type": "listItem",
                        "content": [paragraph_node],
                    }
                )
            else:
                if current_list:
                    content.append(current_list)
                    current_list = None
                content.append(paragraph_node)

            for _ in range(page_breaks):
                if current_list:
                    content.append(current_list)
                    current_list = None
                content.append({"type": "pageBreak"})
            continue

        if current_list:
            content.append(current_list)
            current_list = None

        if isinstance(block, Table):
            table_node = _table_to_node(block)
            if table_node:
                content.append(table_node)

    if current_list:
        content.append(current_list)

    if not content:
        content = [{"type": "paragraph"}]

    return {
        "content": normalize_document_content(
            {
                "type": "doc",
                "content": content,
            }
        ),
        "metadata": normalize_document_metadata(
            {
                "page_setup": _document_page_setup(doc),
                "section_metadata": [_section_metadata(section) for section in doc.sections],
            }
        ),
    }


def _iter_block_items(parent) -> Iterable[Paragraph | Table]:
    if isinstance(parent, DocxDocumentType):
        parent_elm = parent.element.body
        context = parent
    else:
        parent_elm = parent._tc
        context = parent

    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, context)
        elif isinstance(child, CT_Tbl):
            yield Table(child, context)


def _paragraph_to_node(paragraph: Paragraph) -> tuple[dict, str | None, dict, int]:
    inline_content, page_breaks = _extract_paragraph_inline_content(paragraph)
    list_kind = _paragraph_list_kind(paragraph)
    list_identity = _paragraph_list_identity(paragraph, list_kind)
    heading_level = _heading_level(paragraph)
    attrs = _paragraph_attrs(paragraph)

    if heading_level and not list_kind:
        node = {"type": "heading", "attrs": {"level": heading_level, **attrs}}
        if inline_content:
            node["content"] = inline_content
        return node, None, {}, page_breaks

    node = {"type": "paragraph"}
    if attrs:
        node["attrs"] = attrs
    if inline_content:
        node["content"] = inline_content
    return node, list_kind, list_identity, page_breaks


def _heading_level(paragraph: Paragraph) -> int | None:
    style_name = str(getattr(getattr(paragraph, "style", None), "name", "") or "").strip()
    match = _HEADING_STYLE_RE.search(style_name)
    if not match:
        return None
    return min(max(int(match.group(1)), 1), 3)


def _paragraph_list_kind(paragraph: Paragraph) -> str | None:
    style_name = str(getattr(getattr(paragraph, "style", None), "name", "") or "").strip().lower()
    if any(hint in style_name for hint in _BULLET_STYLE_HINTS):
        return "bullet"
    if any(hint in style_name for hint in _ORDERED_STYLE_HINTS):
        return "ordered"

    ppr = getattr(paragraph._p, "pPr", None)
    num_pr = getattr(ppr, "numPr", None) if ppr is not None else None
    if num_pr is not None:
        return "ordered"
    return None


def _paragraph_list_identity(paragraph: Paragraph, list_kind: str | None) -> dict:
    if not list_kind:
        return {}

    ppr = getattr(paragraph._p, "pPr", None)
    num_pr = getattr(ppr, "numPr", None) if ppr is not None else None
    num_id = None
    ilvl = None
    if num_pr is not None:
        if getattr(num_pr, "numId", None) is not None:
            num_id = str(num_pr.numId.val)
        if getattr(num_pr, "ilvl", None) is not None:
            ilvl = str(num_pr.ilvl.val)

    return {
        "kind": list_kind,
        "style_name": getattr(getattr(paragraph, "style", None), "name", "") or "",
        "num_id": num_id or "",
        "level": ilvl or "0",
    }


def _paragraph_attrs(paragraph: Paragraph) -> dict:
    attrs = {}
    alignment = paragraph.alignment
    if alignment == WD_ALIGN_PARAGRAPH.CENTER:
        attrs["textAlign"] = "center"
    elif alignment == WD_ALIGN_PARAGRAPH.RIGHT:
        attrs["textAlign"] = "right"
    elif alignment == WD_ALIGN_PARAGRAPH.JUSTIFY:
        attrs["textAlign"] = "justify"

    attrs["word_style"] = _style_metadata(getattr(paragraph, "style", None))
    attrs["paragraph_metrics"] = _paragraph_metrics(paragraph)
    return attrs


def _extract_paragraph_inline_content(paragraph: Paragraph) -> tuple[list[dict], int]:
    content: list[dict] = []
    page_breaks = 0

    for run in paragraph.runs:
        marks = _marks_for_run(run)
        for child in run._r.iterchildren():
            tag = child.tag
            if tag == qn("w:t"):
                _append_text_content(content, child.text or "", marks)
            elif tag == qn("w:tab"):
                _append_text_content(content, "\t", marks)
            elif tag == qn("w:br"):
                break_type = child.get(qn("w:type")) or ""
                if break_type == "page":
                    page_breaks += 1
                else:
                    content.append({"type": "hardBreak"})

    return content, page_breaks


def _marks_for_run(run) -> list[dict]:
    marks = []
    if run.bold:
        marks.append({"type": "bold"})
    if run.italic:
        marks.append({"type": "italic"})
    if run.underline:
        marks.append({"type": "underline"})
    if run.font.superscript:
        marks.append({"type": "superscript"})
    if run.font.strike:
        marks.append({"type": "strike"})
    run_metrics = _run_metrics(run)
    if run_metrics:
        marks.append({"type": "wordRun", "attrs": {"run_metrics": run_metrics}})
    return marks


def _append_text_content(content: list[dict], text: str, marks: list[dict]) -> None:
    if text == "":
        return

    parts = text.split("\n")
    for index, part in enumerate(parts):
        if part:
            entry = {"type": "text", "text": part}
            if marks:
                entry["marks"] = [dict(mark) for mark in marks]
            content.append(entry)
        if index < len(parts) - 1:
            content.append({"type": "hardBreak"})


def _table_to_node(table: Table) -> dict | None:
    rows = []
    has_header_row = len(table.rows) > 1 and _row_looks_like_header(table.rows[0])

    for row_index, row in enumerate(table.rows):
        cells = []
        cell_type = "tableHeader" if has_header_row and row_index == 0 else "tableCell"
        for cell in row.cells:
            cell_content = []
            for block in _iter_block_items(cell):
                if isinstance(block, Paragraph):
                    paragraph_node, _, _, page_breaks = _paragraph_to_node(block)
                    cell_content.append(paragraph_node)
                    for _ in range(page_breaks):
                        cell_content.append({"type": "pageBreak"})
            if not cell_content:
                cell_content.append({"type": "paragraph"})
            cells.append(
                {
                    "type": cell_type,
                    "content": cell_content,
                }
            )

        if cells:
            rows.append({"type": "tableRow", "content": cells})

    if not rows:
        return None
    return {
        "type": "table",
        "attrs": {
            "word_style": _style_metadata(getattr(table, "style", None)),
            "paragraph_metrics": {
                "alignment": _enum_name(table.alignment, {
                    WD_TABLE_ALIGNMENT.LEFT: "left",
                    WD_TABLE_ALIGNMENT.CENTER: "center",
                    WD_TABLE_ALIGNMENT.RIGHT: "right",
                }),
            },
        },
        "content": rows,
    }


def _row_looks_like_header(row) -> bool:
    saw_text = False
    for cell in row.cells:
        for paragraph in cell.paragraphs:
            if not paragraph.text.strip():
                continue
            saw_text = True
            visible_runs = [run for run in paragraph.runs if (run.text or "").strip()]
            if visible_runs and not all(run.bold for run in visible_runs):
                return False
    return saw_text


def _style_metadata(style) -> dict:
    if not style:
        return {}
    return {
        "name": getattr(style, "name", "") or "",
        "style_id": getattr(style, "style_id", "") or "",
    }


def _paragraph_metrics(paragraph: Paragraph) -> dict:
    fmt = paragraph.paragraph_format
    return {
        "alignment": _enum_name(
            paragraph.alignment,
            {
                WD_ALIGN_PARAGRAPH.LEFT: "left",
                WD_ALIGN_PARAGRAPH.CENTER: "center",
                WD_ALIGN_PARAGRAPH.RIGHT: "right",
                WD_ALIGN_PARAGRAPH.JUSTIFY: "justify",
            },
        ),
        "left_indent_pt": _length_points(fmt.left_indent),
        "right_indent_pt": _length_points(fmt.right_indent),
        "first_line_indent_pt": _length_points(fmt.first_line_indent),
        "space_before_pt": _length_points(fmt.space_before),
        "space_after_pt": _length_points(fmt.space_after),
        "line_spacing": _line_spacing_value(fmt.line_spacing),
        "line_spacing_rule": str(fmt.line_spacing_rule) if fmt.line_spacing_rule is not None else "",
        "keep_together": bool(fmt.keep_together) if fmt.keep_together is not None else None,
        "keep_with_next": bool(fmt.keep_with_next) if fmt.keep_with_next is not None else None,
        "page_break_before": bool(fmt.page_break_before) if fmt.page_break_before is not None else None,
        "widow_control": bool(fmt.widow_control) if fmt.widow_control is not None else None,
        "tab_stops": _tab_stops(paragraph),
    }


def _run_metrics(run) -> dict:
    font = run.font
    metrics = {
        "font_name": font.name or "",
        "font_size_pt": _length_points(font.size),
        "bold": font.bold if font.bold is not None else run.bold,
        "italic": font.italic if font.italic is not None else run.italic,
        "underline": bool(font.underline) if font.underline is not None else bool(run.underline),
        "all_caps": bool(font.all_caps) if font.all_caps is not None else None,
        "small_caps": bool(font.small_caps) if font.small_caps is not None else None,
        "strike": bool(font.strike) if font.strike is not None else None,
        "superscript": bool(font.superscript) if font.superscript is not None else None,
        "subscript": bool(font.subscript) if font.subscript is not None else None,
        "color_rgb": str(font.color.rgb) if getattr(font.color, "rgb", None) else "",
        "highlight_color": str(font.highlight_color) if font.highlight_color is not None else "",
    }
    return {key: value for key, value in metrics.items() if value not in ("", None, False)}


def _document_page_setup(doc) -> dict:
    if not doc.sections:
        return {}
    return _section_metadata(doc.sections[0])


def _section_metadata(section) -> dict:
    return {
        "start_type": _enum_name(
            section.start_type,
            {
                WD_SECTION_START.CONTINUOUS: "continuous",
                WD_SECTION_START.NEW_PAGE: "new_page",
                WD_SECTION_START.EVEN_PAGE: "even_page",
                WD_SECTION_START.ODD_PAGE: "odd_page",
            },
        ),
        "orientation": _enum_name(
            section.orientation,
            {
                WD_ORIENT.PORTRAIT: "portrait",
                WD_ORIENT.LANDSCAPE: "landscape",
            },
        ),
        "page_width_pt": _length_points(section.page_width),
        "page_height_pt": _length_points(section.page_height),
        "left_margin_pt": _length_points(section.left_margin),
        "right_margin_pt": _length_points(section.right_margin),
        "top_margin_pt": _length_points(section.top_margin),
        "bottom_margin_pt": _length_points(section.bottom_margin),
        "header_distance_pt": _length_points(section.header_distance),
        "footer_distance_pt": _length_points(section.footer_distance),
    }


def _tab_stops(paragraph: Paragraph) -> list[dict]:
    stops = []
    for tab in paragraph.paragraph_format.tab_stops:
        stops.append(
            {
                "position_pt": _length_points(tab.position),
                "alignment": _enum_name(
                    tab.alignment,
                    {
                        WD_TAB_ALIGNMENT.LEFT: "left",
                        WD_TAB_ALIGNMENT.CENTER: "center",
                        WD_TAB_ALIGNMENT.RIGHT: "right",
                        WD_TAB_ALIGNMENT.DECIMAL: "decimal",
                        WD_TAB_ALIGNMENT.BAR: "bar",
                    },
                ),
                "leader": _enum_name(
                    tab.leader,
                    {
                        WD_TAB_LEADER.SPACES: "spaces",
                        WD_TAB_LEADER.DOTS: "dots",
                        WD_TAB_LEADER.DASHES: "dashes",
                        WD_TAB_LEADER.LINES: "lines",
                        WD_TAB_LEADER.HEAVY: "heavy",
                        WD_TAB_LEADER.MIDDLE_DOT: "middle_dot",
                    },
                ),
            }
        )
    return stops


def _length_points(value):
    return round(value.pt, 3) if value is not None else None


def _line_spacing_value(value):
    if value is None:
        return None
    if hasattr(value, "pt"):
        return round(value.pt, 3)
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _enum_name(value, mapping):
    return mapping.get(value, "") if value is not None else ""

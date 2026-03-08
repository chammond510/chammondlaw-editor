import re
from collections.abc import Iterable

from docx import Document as DocxDocument
from docx.document import Document as DocxDocumentType
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph


_HEADING_STYLE_RE = re.compile(r"heading\s+(\d+)", re.IGNORECASE)
_BULLET_STYLE_HINTS = ("bullet", "list bullet")
_ORDERED_STYLE_HINTS = ("number", "list number", "decimal", "roman")


def import_docx_to_tiptap(source) -> dict:
    doc = DocxDocument(source)
    content: list[dict] = []
    current_list: dict | None = None

    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            paragraph_node, list_kind, page_breaks = _paragraph_to_node(block)
            if list_kind:
                list_type = "bulletList" if list_kind == "bullet" else "orderedList"
                if not current_list or current_list.get("type") != list_type:
                    if current_list:
                        content.append(current_list)
                    current_list = {"type": list_type, "content": []}
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
        "type": "doc",
        "content": content,
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


def _paragraph_to_node(paragraph: Paragraph) -> tuple[dict, str | None, int]:
    inline_content, page_breaks = _extract_paragraph_inline_content(paragraph)
    list_kind = _paragraph_list_kind(paragraph)
    heading_level = _heading_level(paragraph)
    attrs = _paragraph_attrs(paragraph)

    if heading_level and not list_kind:
        node = {"type": "heading", "attrs": {"level": heading_level}}
        if inline_content:
            node["content"] = inline_content
        return node, None, page_breaks

    node = {"type": "paragraph"}
    if attrs:
        node["attrs"] = attrs
    if inline_content:
        node["content"] = inline_content
    return node, list_kind, page_breaks


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


def _paragraph_attrs(paragraph: Paragraph) -> dict:
    alignment = paragraph.alignment
    if alignment == WD_ALIGN_PARAGRAPH.CENTER:
        return {"textAlign": "center"}
    if alignment == WD_ALIGN_PARAGRAPH.RIGHT:
        return {"textAlign": "right"}
    if alignment == WD_ALIGN_PARAGRAPH.JUSTIFY:
        return {"textAlign": "justify"}
    return {}


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
                    paragraph_node, _, page_breaks = _paragraph_to_node(block)
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
    return {"type": "table", "content": rows}


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

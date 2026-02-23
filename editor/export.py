"""
Export Tiptap JSON to Word (.docx) format.

Walks the Tiptap JSON document tree and maps nodes to python-docx elements.
Supports format presets for legal documents (court briefs, cover letters, declarations).
"""

import io
from docx import Document as DocxDocument
from docx.shared import Pt, Inches, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.enum.style import WD_STYLE_TYPE


FORMAT_PRESETS = {
    "court_brief": {
        "font_name": "Times New Roman",
        "font_size": 12,
        "line_spacing": WD_LINE_SPACING.DOUBLE,
        "margin_inches": 1.0,
        "paragraph_alignment": WD_ALIGN_PARAGRAPH.LEFT,
    },
    "cover_letter": {
        "font_name": "Times New Roman",
        "font_size": 12,
        "line_spacing": WD_LINE_SPACING.SINGLE,
        "margin_inches": 1.0,
        "paragraph_alignment": WD_ALIGN_PARAGRAPH.LEFT,
    },
    "declaration": {
        "font_name": "Times New Roman",
        "font_size": 12,
        "line_spacing": WD_LINE_SPACING.DOUBLE,
        "margin_inches": 1.0,
        "paragraph_alignment": WD_ALIGN_PARAGRAPH.LEFT,
        "numbered_paragraphs": True,
    },
}


def tiptap_to_docx(tiptap_json, title="Document", export_format="court_brief"):
    """Convert Tiptap JSON content to a .docx file buffer."""
    doc = DocxDocument()
    preset = FORMAT_PRESETS.get(export_format, FORMAT_PRESETS["court_brief"])

    # Set margins
    for section in doc.sections:
        section.top_margin = Inches(preset["margin_inches"])
        section.bottom_margin = Inches(preset["margin_inches"])
        section.left_margin = Inches(preset["margin_inches"])
        section.right_margin = Inches(preset["margin_inches"])

    # Configure default style
    style = doc.styles["Normal"]
    font = style.font
    font.name = preset["font_name"]
    font.size = Pt(preset["font_size"])
    pf = style.paragraph_format
    pf.line_spacing_rule = preset["line_spacing"]
    pf.space_after = Pt(0)
    pf.space_before = Pt(0)

    # Configure heading styles
    for level in range(1, 4):
        style_name = f"Heading {level}"
        if style_name in doc.styles:
            hs = doc.styles[style_name]
            hs.font.name = preset["font_name"]
            hs.font.bold = True
            if level == 1:
                hs.font.size = Pt(14)
            elif level == 2:
                hs.font.size = Pt(13)
            else:
                hs.font.size = Pt(12)
            hs.paragraph_format.space_before = Pt(12)
            hs.paragraph_format.space_after = Pt(6)

    content = tiptap_json.get("content", []) if isinstance(tiptap_json, dict) else []
    para_counter = [0]  # mutable counter for numbered paragraphs

    for node in content:
        _process_node(doc, node, preset, para_counter)

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer


def _process_node(doc, node, preset, para_counter):
    """Process a single Tiptap node into docx elements."""
    node_type = node.get("type", "")

    if node_type == "heading":
        level = node.get("attrs", {}).get("level", 1)
        level = min(max(level, 1), 3)
        text_parts = _extract_text_parts(node)
        p = doc.add_heading(level=level)
        _apply_text_parts(p, text_parts)

    elif node_type == "paragraph":
        text_parts = _extract_text_parts(node)
        # Skip empty paragraphs (just add spacing)
        if not text_parts or all(t[0].strip() == "" for t in text_parts):
            doc.add_paragraph("")
            return

        if preset.get("numbered_paragraphs"):
            para_counter[0] += 1
            p = doc.add_paragraph()
            run = p.add_run(f"{para_counter[0]}. ")
            run.font.name = preset["font_name"]
            run.font.size = Pt(preset["font_size"])
            _apply_text_parts(p, text_parts)
        else:
            p = doc.add_paragraph()
            _apply_text_parts(p, text_parts)

        # Handle text alignment
        alignment = node.get("attrs", {}).get("textAlign")
        if alignment == "center":
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif alignment == "right":
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        elif alignment == "justify":
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    elif node_type == "bulletList":
        for item in node.get("content", []):
            _process_list_item(doc, item, preset, bullet=True)

    elif node_type == "orderedList":
        for i, item in enumerate(node.get("content", []), 1):
            _process_list_item(doc, item, preset, bullet=False, number=i)

    elif node_type == "blockquote":
        for child in node.get("content", []):
            if child.get("type") == "paragraph":
                text_parts = _extract_text_parts(child)
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.5)
                p.style = doc.styles["Normal"]
                _apply_text_parts(p, text_parts)

    elif node_type == "horizontalRule":
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(6)
        run = p.add_run("_" * 60)
        run.font.size = Pt(8)
        run.font.color.rgb = None

    elif node_type == "table":
        _process_table(doc, node, preset)

    elif node_type == "hardBreak":
        pass  # Handled within text extraction


def _process_list_item(doc, item, preset, bullet=True, number=None):
    """Process a list item node."""
    for child in item.get("content", []):
        if child.get("type") == "paragraph":
            text_parts = _extract_text_parts(child)
            if bullet:
                p = doc.add_paragraph(style="List Bullet")
            else:
                p = doc.add_paragraph(style="List Number")
            _apply_text_parts(p, text_parts)
            p.style.font.name = preset["font_name"]
            p.style.font.size = Pt(preset["font_size"])


def _process_table(doc, node, preset):
    """Process a table node."""
    rows_data = node.get("content", [])
    if not rows_data:
        return

    # Determine dimensions
    num_rows = len(rows_data)
    num_cols = max(
        len(row.get("content", [])) for row in rows_data
    ) if rows_data else 1

    table = doc.add_table(rows=num_rows, cols=num_cols)
    table.style = "Table Grid"

    for r_idx, row_node in enumerate(rows_data):
        cells = row_node.get("content", [])
        for c_idx, cell_node in enumerate(cells):
            if c_idx < num_cols:
                cell = table.rows[r_idx].cells[c_idx]
                # Clear default paragraph
                cell.paragraphs[0].clear()
                for child in cell_node.get("content", []):
                    if child.get("type") == "paragraph":
                        text_parts = _extract_text_parts(child)
                        p = cell.paragraphs[0] if not cell.paragraphs[0].text else cell.add_paragraph()
                        _apply_text_parts(p, text_parts)


def _extract_text_parts(node):
    """
    Extract text and formatting from a node's content.
    Returns list of (text, marks_dict) tuples.
    """
    parts = []
    for child in node.get("content", []):
        if child.get("type") == "text":
            text = child.get("text", "")
            marks = {}
            for mark in child.get("marks", []):
                marks[mark.get("type", "")] = mark.get("attrs", {})
            parts.append((text, marks))
        elif child.get("type") == "hardBreak":
            parts.append(("\n", {}))
    return parts


def _apply_text_parts(paragraph, text_parts):
    """Apply text parts with formatting to a paragraph."""
    for text, marks in text_parts:
        run = paragraph.add_run(text)
        if "bold" in marks:
            run.bold = True
        if "italic" in marks:
            run.italic = True
        if "underline" in marks:
            run.underline = True
        if "strike" in marks:
            run.font.strike = True

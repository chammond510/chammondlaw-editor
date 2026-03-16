from __future__ import annotations

import copy
import json
import uuid


BLOCK_NODE_TYPES = {
    "paragraph",
    "heading",
    "bulletList",
    "orderedList",
    "table",
    "blockquote",
}

TEXT_CONTAINER_TYPES = {
    "paragraph",
    "heading",
    "blockquote",
}

DEFAULT_DOCUMENT_METADATA = {
    "fidelity_mode": "draft",
    "page_setup": {},
    "section_metadata": [],
    "source_docx_info": {},
    "preview_state": {},
}


def new_block_id() -> str:
    return uuid.uuid4().hex


def normalize_document_content(content: dict | None) -> dict:
    if not isinstance(content, dict):
        content = {}

    normalized = copy.deepcopy(content)
    if normalized.get("type") != "doc":
        normalized = {
            "type": "doc",
            "content": normalized.get("content", []) if isinstance(normalized.get("content"), list) else [],
        }

    nodes = normalized.get("content")
    if not isinstance(nodes, list):
        nodes = []
    normalized["content"] = [_normalize_node(node, top_level=True) for node in nodes if isinstance(node, dict)]

    if not normalized["content"]:
        normalized["content"] = [_normalize_node({"type": "paragraph"}, top_level=True)]

    return normalized


def normalize_document_metadata(
    metadata: dict | None,
    *,
    default_fidelity_mode: str = "draft",
    source_docx_info: dict | None = None,
    page_setup: dict | None = None,
    section_metadata: list[dict] | None = None,
    preview_state: dict | None = None,
) -> dict:
    normalized = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}

    for key, value in DEFAULT_DOCUMENT_METADATA.items():
        normalized.setdefault(key, copy.deepcopy(value))

    normalized["fidelity_mode"] = (
        normalized.get("fidelity_mode") if normalized.get("fidelity_mode") in {"draft", "proof"} else default_fidelity_mode
    )
    normalized["page_setup"] = normalized.get("page_setup") if isinstance(normalized.get("page_setup"), dict) else {}
    normalized["section_metadata"] = (
        normalized.get("section_metadata") if isinstance(normalized.get("section_metadata"), list) else []
    )
    normalized["source_docx_info"] = (
        normalized.get("source_docx_info") if isinstance(normalized.get("source_docx_info"), dict) else {}
    )
    normalized["preview_state"] = (
        normalized.get("preview_state") if isinstance(normalized.get("preview_state"), dict) else {}
    )

    if source_docx_info:
        normalized["source_docx_info"] = {
            **normalized["source_docx_info"],
            **copy.deepcopy(source_docx_info),
        }
    if page_setup is not None:
        normalized["page_setup"] = copy.deepcopy(page_setup)
    if section_metadata is not None:
        normalized["section_metadata"] = copy.deepcopy(section_metadata)
    if preview_state is not None:
        normalized["preview_state"] = copy.deepcopy(preview_state)

    return normalized


def summarize_top_level_blocks(content: dict | None) -> list[dict]:
    normalized = normalize_document_content(content)
    items = []
    for index, node in enumerate(normalized.get("content", [])):
        attrs = node.get("attrs") or {}
        items.append(
            {
                "index": index,
                "block_id": attrs.get("block_id") or new_block_id(),
                "type": node.get("type") or "paragraph",
                "text": node_plain_text(node),
            }
        )
    return items


def node_plain_text(node: dict | None) -> str:
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"
    if node_type == "pageBreak":
        return "\n--- page break ---\n"

    parts = []
    for child in node.get("content", []) or []:
        parts.append(node_plain_text(child))
    return "".join(parts)


def block_editor_text(node: dict | None) -> str:
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    if node_type in {"paragraph", "heading", "blockquote"}:
        return node_plain_text(node)
    if node_type in {"bulletList", "orderedList"}:
        lines = []
        for item in node.get("content", []) or []:
            lines.append(node_plain_text(item).strip())
        return "\n".join(line for line in lines if line)
    if node_type == "table":
        rows = []
        for row in node.get("content", []) or []:
            cells = []
            for cell in row.get("content", []) or []:
                cells.append(node_plain_text(cell).replace("\n", " ").strip())
            rows.append("\t".join(cells))
        return "\n".join(rows)
    return node_plain_text(node)


def replace_top_level_block_text(content: dict | None, block_id: str, text: str) -> dict:
    normalized = normalize_document_content(content)
    updated_nodes = []
    for node in normalized.get("content", []):
        attrs = node.get("attrs") or {}
        if attrs.get("block_id") != block_id:
            updated_nodes.append(node)
            continue
        updated_nodes.append(_updated_block_node(node, text))
    normalized["content"] = updated_nodes
    return normalized


def _normalize_node(node: dict, *, top_level: bool = False) -> dict:
    normalized = copy.deepcopy(node)
    node_type = normalized.get("type") or "paragraph"
    normalized["type"] = node_type

    attrs = normalized.get("attrs")
    if not isinstance(attrs, dict):
        attrs = {}

    if top_level and node_type in BLOCK_NODE_TYPES:
        attrs.setdefault("block_id", new_block_id())
        attrs.setdefault("word_style", None)
        attrs.setdefault("paragraph_metrics", {})
        if node_type in {"bulletList", "orderedList"}:
            attrs.setdefault("list_identity", {})
    elif node_type in TEXT_CONTAINER_TYPES:
        attrs.setdefault("word_style", None)
        attrs.setdefault("paragraph_metrics", {})

    if attrs:
        normalized["attrs"] = attrs
    elif "attrs" in normalized:
        normalized["attrs"] = {}

    content = normalized.get("content")
    if isinstance(content, list):
        normalized["content"] = [_normalize_child_node(item) for item in content if isinstance(item, dict)]
    elif node_type != "text":
        normalized["content"] = []

    if node_type == "text":
        normalized["text"] = normalized.get("text", "")
        marks = normalized.get("marks")
        if isinstance(marks, list):
            normalized["marks"] = [_normalize_mark(mark) for mark in marks if isinstance(mark, dict)]

    return normalized


def _normalize_child_node(node: dict) -> dict:
    node_type = node.get("type")
    if node_type == "text":
        normalized = copy.deepcopy(node)
        normalized["text"] = normalized.get("text", "")
        marks = normalized.get("marks")
        if isinstance(marks, list):
            normalized["marks"] = [_normalize_mark(mark) for mark in marks if isinstance(mark, dict)]
        return normalized
    return _normalize_node(node, top_level=False)


def _normalize_mark(mark: dict) -> dict:
    normalized = copy.deepcopy(mark)
    attrs = normalized.get("attrs")
    if attrs is not None and not isinstance(attrs, dict):
        attrs = {}
    if normalized.get("type") == "wordRun":
        attrs = attrs or {}
        attrs.setdefault("run_metrics", {})
    if attrs is not None:
        normalized["attrs"] = attrs
    return normalized


def _updated_block_node(node: dict, text: str) -> dict:
    updated = copy.deepcopy(node)
    node_type = updated.get("type")
    if node_type in {"paragraph", "heading", "blockquote"}:
        updated["content"] = _text_to_inline_content(text)
        return updated
    if node_type in {"bulletList", "orderedList"}:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        updated["content"] = [
            {
                "type": "listItem",
                "content": [
                    {
                        "type": "paragraph",
                        "content": _text_to_inline_content(line),
                    }
                ],
            }
            for line in lines
        ] or [{"type": "listItem", "content": [{"type": "paragraph"}]}]
        return updated
    if node_type == "table":
        rows = []
        for line in text.splitlines():
            if not line.strip():
                continue
            cells = []
            for cell_text in line.split("\t"):
                cells.append(
                    {
                        "type": "tableCell",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": _text_to_inline_content(cell_text.strip()),
                            }
                        ],
                    }
                )
            rows.append({"type": "tableRow", "content": cells})
        updated["content"] = rows or [{"type": "tableRow", "content": [{"type": "tableCell", "content": [{"type": "paragraph"}]}]}]
        return updated
    return updated


def _text_to_inline_content(text: str) -> list[dict]:
    if text == "":
        return []
    lines = text.splitlines()
    content: list[dict] = []
    for index, line in enumerate(lines):
        if line:
            content.append({"type": "text", "text": line})
        if index < len(lines) - 1:
            content.append({"type": "hardBreak"})
    return content


def json_attr(value):
    if value in (None, {}, [], ""):
        return ""
    return json.dumps(value, sort_keys=True)

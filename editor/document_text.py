def extract_plain_text(content, max_chars=None):
    parts = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return

        node_type = node.get("type")
        if node_type == "text":
            parts.append(node.get("text", ""))
        elif node_type == "hardBreak":
            parts.append("\n")
        elif node_type == "paragraph":
            walk(node.get("content", []))
            parts.append("\n")
        elif node_type == "heading":
            walk(node.get("content", []))
            parts.append("\n")
        elif node_type == "pageBreak":
            parts.append("\n--- page break ---\n")
        elif node_type == "footnoteReference":
            number = node.get("attrs", {}).get("number") or "?"
            parts.append(f"[{number}]")
        else:
            walk(node.get("content", []))

    walk(content if isinstance(content, dict) else {})
    text = "".join(parts).strip()
    if max_chars is not None:
        return text[:max_chars]
    return text


def clip_document_text(text, max_chars=18000, tail_chars=5000):
    normalized = (text or "").strip()
    if len(normalized) <= max_chars:
        return normalized

    head_chars = max(1000, max_chars - tail_chars)
    head = normalized[:head_chars].rstrip()
    tail = normalized[-tail_chars:].lstrip()
    omitted = max(0, len(normalized) - len(head) - len(tail))
    return (
        f"{head}\n\n"
        f"[Document excerpt clipped. {omitted} characters omitted from the middle.]\n\n"
        f"{tail}"
    )

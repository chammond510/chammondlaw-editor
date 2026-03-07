from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from django.conf import settings


USCIS_COVER_LETTER_STYLE_FAMILY = "uscis_cover_letter"
DEFAULT_COVER_LETTER_STYLE_FILENAME = "Cadeau I-612 Cover Letter.docx"
DATE_PATTERN = re.compile(
    r"^\s*(\[[Dd]ate\]|[A-Z][a-z]+ \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{2,4})"
)


@dataclass
class ResolvedStyleAnchor:
    path: str
    source: str
    title: str
    style_family: str
    metadata: dict
    exemplar_id: int | None = None


def _load_docx_document(file_path: str):
    from docx import Document as DocxDocument

    return DocxDocument(file_path)


def _paragraph_text(paragraph) -> str:
    return (paragraph.text or "").replace("\xa0", " ").strip()


def _non_empty_paragraphs(doc) -> list[dict]:
    items = []
    for index, paragraph in enumerate(doc.paragraphs):
        text = _paragraph_text(paragraph)
        if text:
            items.append({"index": index, "paragraph": paragraph, "text": text})
    return items


def _first_matching(items, predicate):
    for item in items:
        if predicate(item):
            return item
    return None


def _looks_like_salutation(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith("dear ") or lowered.startswith("to whom it may concern")


def _looks_like_re_line(text: str) -> bool:
    return text.startswith("RE:")


def _looks_like_service_line(text: str) -> bool:
    return text.startswith("Via ")


def _looks_like_exhibit_category(item) -> bool:
    paragraph = item["paragraph"]
    text = item["text"]
    if not text.endswith(":"):
        return False
    if len(text) > 100:
        return False
    has_underlined_italic_run = any(run.underline and run.italic for run in paragraph.runs)
    return bool(has_underlined_italic_run)


def _looks_like_signature_line(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith("christopher hammond") or lowered.startswith("chris hammond law firm")


def _first_paragraph_with_indent(items):
    for item in items:
        paragraph = item["paragraph"]
        fmt = paragraph.paragraph_format
        if fmt.first_line_indent and int(fmt.first_line_indent) > 0:
            return item
    return None


def extract_style_anchor_structure(file_path: str) -> dict:
    doc = _load_docx_document(file_path)
    items = _non_empty_paragraphs(doc)
    if not items:
        return {}

    service_item = _first_matching(items, lambda item: _looks_like_service_line(item["text"]))
    re_item = _first_matching(items, lambda item: _looks_like_re_line(item["text"]))
    salutation_item = _first_matching(items, lambda item: _looks_like_salutation(item["text"]))
    date_item = _first_matching(
        items,
        lambda item: (
            service_item
            and item["index"] > service_item["index"]
            and re_item
            and item["index"] < re_item["index"]
            and DATE_PATTERN.match(item["text"])
        ),
    )
    subject_item = None
    if re_item and salutation_item:
        subject_item = _first_matching(
            items,
            lambda item: re_item["index"] < item["index"] < salutation_item["index"],
        )
    intro_item = None
    if salutation_item:
        intro_item = _first_matching(items, lambda item: item["index"] > salutation_item["index"])
    section_heading_item = _first_matching(
        items,
        lambda item: (
            item["paragraph"].style.name == "List Paragraph"
            and any(run.bold for run in item["paragraph"].runs)
        ),
    )
    body_item = _first_paragraph_with_indent(items)
    exhibit_category_item = _first_matching(items, _looks_like_exhibit_category)
    closing_request_item = _first_matching(
        items,
        lambda item: item["text"].startswith("Thank you in advance"),
    )
    closing_salutation_item = _first_matching(
        items,
        lambda item: item["text"].startswith("Respectfully submitted"),
    )

    signature_lines = []
    if closing_salutation_item:
        for item in items:
            if item["index"] > closing_salutation_item["index"]:
                signature_lines.append(item["text"])

    letterhead_lines = []
    if service_item:
        for item in items:
            if item["index"] < service_item["index"]:
                letterhead_lines.append(item["text"])

    return {
        "style_family": USCIS_COVER_LETTER_STYLE_FAMILY,
        "letterhead_lines": letterhead_lines,
        "signature_lines": signature_lines,
        "markers": {
            "service_line": service_item["text"] if service_item else "",
            "date_line": date_item["text"] if date_item else "",
            "re_line": re_item["text"] if re_item else "",
            "subject_line": subject_item["text"] if subject_item else "",
            "salutation": salutation_item["text"] if salutation_item else "",
            "intro_paragraph": intro_item["text"] if intro_item else "",
            "section_heading": section_heading_item["text"] if section_heading_item else "",
            "body_paragraph": body_item["text"] if body_item else "",
            "exhibit_category": exhibit_category_item["text"] if exhibit_category_item else "",
            "closing_request": closing_request_item["text"] if closing_request_item else "",
            "closing_salutation": closing_salutation_item["text"] if closing_salutation_item else "",
        },
    }


def infer_style_family(*, export_format: str, document_type=None) -> str:
    if export_format == "cover_letter":
        return USCIS_COVER_LETTER_STYLE_FAMILY
    if document_type and getattr(document_type, "category", "") == "cover_letter":
        return USCIS_COVER_LETTER_STYLE_FAMILY
    return ""


def _default_cover_letter_anchor_path() -> Path | None:
    exemplar_dir = Path(settings.BASE_DIR) / "Exemplars"
    explicit = exemplar_dir / DEFAULT_COVER_LETTER_STYLE_FILENAME
    if explicit.exists():
        return explicit

    for candidate in sorted(exemplar_dir.glob("*Cover Letter*.docx")):
        return candidate
    return None


def resolve_style_anchor_for_document(*, user, document, export_format: str) -> ResolvedStyleAnchor | None:
    style_family = infer_style_family(export_format=export_format, document_type=document.document_type)
    if not style_family:
        return None

    from .models import Exemplar

    exemplar = (
        Exemplar.objects.filter(
            created_by=user,
            is_active=True,
            kind="style_anchor",
            style_family=style_family,
        )
        .order_by("-is_default", "-updated_at")
        .first()
    )
    if exemplar and exemplar.original_file:
        metadata = dict(exemplar.metadata or {})
        structure = metadata.get("style_anchor_structure")
        if not structure and exemplar.original_file.name.lower().endswith(".docx"):
            structure = extract_style_anchor_structure(exemplar.original_file.path)
            metadata["style_anchor_structure"] = structure
            exemplar.metadata = metadata
            exemplar.save(update_fields=["metadata", "updated_at"])
        return ResolvedStyleAnchor(
            path=exemplar.original_file.path,
            source="database",
            exemplar_id=exemplar.id,
            title=exemplar.title,
            style_family=style_family,
            metadata=metadata,
        )

    fallback_path = _default_cover_letter_anchor_path()
    if not fallback_path:
        return None

    return ResolvedStyleAnchor(
        path=str(fallback_path),
        source="filesystem",
        title=fallback_path.stem,
        style_family=style_family,
        metadata={"style_anchor_structure": extract_style_anchor_structure(str(fallback_path))},
    )


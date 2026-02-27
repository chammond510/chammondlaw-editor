import os
from pathlib import Path

import numpy as np
from docx import Document as DocxDocument
from openai import OpenAI
from pypdf import PdfReader


EMBEDDING_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")


def extract_text_from_file(file_path):
    suffix = Path(file_path).suffix.lower()

    if suffix == ".pdf":
        return _extract_pdf_text(file_path)
    if suffix == ".docx":
        return _extract_docx_text(file_path)
    if suffix in {".txt", ".md", ".rtf"}:
        return Path(file_path).read_text(encoding="utf-8", errors="ignore")
    return ""


def _extract_pdf_text(file_path):
    reader = PdfReader(file_path)
    text_parts = []
    for page in reader.pages:
        text_parts.append(page.extract_text() or "")
    return "\n".join(text_parts).strip()


def _extract_docx_text(file_path):
    doc = DocxDocument(file_path)
    paragraphs = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    return "\n".join(paragraphs).strip()


def generate_embedding(text):
    text = (text or "").strip()
    if not text:
        return []

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return []

    client = OpenAI(api_key=api_key)
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text[:12000])
    return list(resp.data[0].embedding)


def cosine_similarity(a, b):
    if not a or not b:
        return 0.0
    vec_a = np.array(a, dtype=np.float32)
    vec_b = np.array(b, dtype=np.float32)
    denom = np.linalg.norm(vec_a) * np.linalg.norm(vec_b)
    if denom == 0:
        return 0.0
    return float(np.dot(vec_a, vec_b) / denom)


def rank_exemplars(query, exemplars):
    query = (query or "").strip()
    if not query:
        for ex in exemplars:
            ex["score"] = 0.0
        return exemplars

    query_embedding = generate_embedding(query)
    lowered = query.lower()

    for ex in exemplars:
        score = 0.0
        if query_embedding and ex.get("embedding"):
            score += cosine_similarity(query_embedding, ex.get("embedding") or [])
        title = (ex.get("title") or "").lower()
        text = (ex.get("extracted_text") or "").lower()
        if lowered in title:
            score += 0.25
        if lowered in text:
            score += 0.15
        ex["score"] = score

    exemplars.sort(key=lambda x: (x["score"], x.get("updated_at") or ""), reverse=True)
    return exemplars

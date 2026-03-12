import logging
import os
from pathlib import Path
from typing import Any

from django.utils import timezone


logger = logging.getLogger(__name__)

OPENAI_CLIENT_FILE_TIMEOUT_SECONDS = int(os.environ.get("OPENAI_CLIENT_FILE_TIMEOUT_SECONDS", "60"))
OPENAI_CLIENT_FILE_ANALYSIS_MODEL = os.environ.get(
    "OPENAI_CLIENT_FILE_ANALYSIS_MODEL",
    os.environ.get("OPENAI_AGENT_MODEL", "gpt-5.4"),
)
OPENAI_CLIENT_FILE_ANALYSIS_MAX_OUTPUT_TOKENS = int(
    os.environ.get("OPENAI_CLIENT_FILE_ANALYSIS_MAX_OUTPUT_TOKENS", "2200")
)
OPENAI_CLIENT_FILE_SCAN_CHAR_THRESHOLD = int(
    os.environ.get("OPENAI_CLIENT_FILE_SCAN_CHAR_THRESHOLD", "160")
)
OPENAI_CLIENT_FILE_PURPOSE = os.environ.get("OPENAI_CLIENT_FILE_PURPOSE", "assistants").strip() or "assistants"


def openai_client_files_enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _new_openai_file_client():
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    from openai import OpenAI

    return OpenAI(api_key=api_key, timeout=OPENAI_CLIENT_FILE_TIMEOUT_SECONDS)


def _normalized_metadata(client_file) -> dict[str, Any]:
    return dict(client_file.metadata or {})


def _metadata_filename(client_file, metadata: dict[str, Any]) -> str:
    filename = str(metadata.get("filename") or "").strip()
    if filename:
        return filename
    return Path(getattr(client_file.original_file, "name", "") or "").name


def _metadata_extension(client_file, metadata: dict[str, Any]) -> str:
    extension = str(metadata.get("extension") or "").strip().lower()
    if extension:
        return extension
    return Path(_metadata_filename(client_file, metadata)).suffix.lower()


def _is_scan_candidate(client_file, metadata: dict[str, Any]) -> bool:
    extension = _metadata_extension(client_file, metadata)
    text_length = len((client_file.extracted_text or "").strip())
    return extension == ".pdf" and text_length < OPENAI_CLIENT_FILE_SCAN_CHAR_THRESHOLD


def build_client_file_warning(metadata: dict[str, Any]) -> str:
    text_extracted = bool(metadata.get("text_extracted"))
    scan_candidate = bool(metadata.get("scan_candidate"))
    openai_index_status = str(metadata.get("openai_index_status") or "").strip().lower()

    if not text_extracted:
        if openai_index_status == "completed":
            return (
                "No extractable text was found locally. The research agent will rely on "
                "OpenAI document analysis for this file."
            )
        if openai_index_status == "failed":
            return (
                "No extractable text was found locally, and OpenAI document analysis could not "
                "be prepared for this file."
            )
        return "No extractable text was found in this file."

    if scan_candidate and openai_index_status != "completed":
        return (
            "This PDF may be image-based. The research agent may need OpenAI document "
            "analysis for reliable extraction."
        )
    return ""


def _existing_document_vector_store_id(document, *, exclude_file_id: int | None = None) -> str:
    for client_file in document.client_files.all()[:200]:
        if exclude_file_id and client_file.id == exclude_file_id:
            continue
        metadata = client_file.metadata or {}
        vector_store_id = str(metadata.get("openai_vector_store_id") or "").strip()
        if vector_store_id:
            return vector_store_id
    return ""


def _save_metadata_if_changed(client_file, metadata: dict[str, Any]) -> None:
    if metadata == (client_file.metadata or {}):
        return
    client_file.metadata = metadata
    client_file.save(update_fields=["metadata", "updated_at"])


def _ensure_openai_file_id(client_file, *, client=None, metadata: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    normalized = dict(metadata or _normalized_metadata(client_file))
    openai_file_id = str(normalized.get("openai_file_id") or "").strip()
    if openai_file_id:
        return openai_file_id, normalized

    client = client or _new_openai_file_client()
    if client is None:
        return "", normalized

    file_path = Path(client_file.original_file.path)
    with file_path.open("rb") as handle:
        uploaded = client.files.create(file=handle, purpose=OPENAI_CLIENT_FILE_PURPOSE)
    openai_file_id = str(getattr(uploaded, "id", "") or "").strip()
    if openai_file_id:
        normalized["openai_file_id"] = openai_file_id
        normalized["openai_file_uploaded_at"] = timezone.now().isoformat()
    return openai_file_id, normalized


def sync_client_file_openai_index(client_file, *, client=None) -> dict[str, Any]:
    metadata = _normalized_metadata(client_file)
    metadata["scan_candidate"] = _is_scan_candidate(client_file, metadata)

    if metadata.get("openai_vector_store_file_id") and str(metadata.get("openai_index_status") or "").lower() == "completed":
        _save_metadata_if_changed(client_file, metadata)
        return metadata

    if client is None and not openai_client_files_enabled():
        metadata.setdefault("openai_index_status", "not_configured")
        _save_metadata_if_changed(client_file, metadata)
        return metadata

    try:
        client = client or _new_openai_file_client()
        if client is None:
            metadata.setdefault("openai_index_status", "not_configured")
            _save_metadata_if_changed(client_file, metadata)
            return metadata

        openai_file_id, metadata = _ensure_openai_file_id(client_file, client=client, metadata=metadata)
        if not openai_file_id:
            metadata["openai_index_status"] = "missing_file"
            _save_metadata_if_changed(client_file, metadata)
            return metadata

        vector_store_id = str(metadata.get("openai_vector_store_id") or "").strip()
        if not vector_store_id:
            vector_store_id = _existing_document_vector_store_id(
                client_file.document,
                exclude_file_id=client_file.id,
            )
        if not vector_store_id:
            vector_store = client.vector_stores.create(
                name=f"Client documents for {client_file.document.id}",
                metadata={"document_id": str(client_file.document.id)},
            )
            vector_store_id = str(getattr(vector_store, "id", "") or "").strip()
        if not vector_store_id:
            metadata["openai_index_status"] = "missing_vector_store"
            _save_metadata_if_changed(client_file, metadata)
            return metadata

        vector_store_file = client.vector_stores.files.create_and_poll(
            vector_store_id=vector_store_id,
            file_id=openai_file_id,
            attributes={
                "document_id": str(client_file.document_id),
                "client_file_id": str(client_file.id),
                "filename": _metadata_filename(client_file, metadata),
            },
        )
        metadata["openai_vector_store_id"] = vector_store_id
        metadata["openai_vector_store_file_id"] = str(getattr(vector_store_file, "id", "") or "").strip()
        metadata["openai_index_status"] = str(getattr(vector_store_file, "status", "completed") or "completed").strip()
        metadata["openai_last_indexed_at"] = timezone.now().isoformat()
        metadata.pop("openai_index_error", None)
    except Exception as exc:
        logger.exception(
            "Unable to index uploaded client document with OpenAI",
            extra={
                "document_id": str(client_file.document_id),
                "client_file_id": client_file.id,
            },
        )
        metadata["openai_index_status"] = "failed"
        metadata["openai_index_error"] = str(exc)[:500]

    _save_metadata_if_changed(client_file, metadata)
    return metadata


def _search_result_text(item) -> str:
    parts = []
    for content in getattr(item, "content", []) or []:
        if getattr(content, "type", "") != "text":
            continue
        text = str(getattr(content, "text", "") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def search_indexed_client_files(*, document, query: str, limit: int = 5, client=None) -> list[dict[str, Any]]:
    normalized_query = (query or "").strip()
    normalized_limit = max(1, min(int(limit or 5), 8))
    if not normalized_query:
        return []

    indexed_files: dict[str, Any] = {}
    by_vector_store_file_id: dict[str, Any] = {}
    vector_store_ids: set[str] = set()
    for client_file in document.client_files.all()[:200]:
        metadata = client_file.metadata or {}
        vector_store_id = str(metadata.get("openai_vector_store_id") or "").strip()
        if not vector_store_id:
            continue
        vector_store_ids.add(vector_store_id)
        indexed_files[str(client_file.id)] = client_file
        vector_store_file_id = str(metadata.get("openai_vector_store_file_id") or "").strip()
        if vector_store_file_id:
            by_vector_store_file_id[vector_store_file_id] = client_file

    if not vector_store_ids:
        return []

    client = client or _new_openai_file_client()
    if client is None:
        return []

    aggregated: dict[int, dict[str, Any]] = {}
    per_store_limit = min(max(normalized_limit * 4, 4), 16)

    for vector_store_id in vector_store_ids:
        try:
            page = client.vector_stores.search(
                vector_store_id=vector_store_id,
                query=normalized_query,
                max_num_results=per_store_limit,
                rewrite_query=True,
            )
        except Exception:
            logger.exception(
                "Unable to search OpenAI vector store for client documents",
                extra={
                    "document_id": str(document.id),
                    "vector_store_id": vector_store_id,
                },
            )
            continue

        for item in page:
            attributes = getattr(item, "attributes", None) or {}
            client_file = indexed_files.get(str(attributes.get("client_file_id") or "").strip())
            if client_file is None:
                client_file = by_vector_store_file_id.get(str(getattr(item, "file_id", "") or "").strip())
            if client_file is None:
                continue

            metadata = client_file.metadata or {}
            score = float(getattr(item, "score", 0.0) or 0.0)
            existing = aggregated.get(client_file.id)
            if existing and existing["score"] >= score:
                continue

            snippet = _search_result_text(item) or (client_file.extracted_text or "")[:500]
            aggregated[client_file.id] = {
                "id": client_file.id,
                "title": client_file.title,
                "filename": _metadata_filename(client_file, metadata),
                "extension": _metadata_extension(client_file, metadata),
                "snippet": snippet[:800],
                "score": round(score, 4),
                "text_extracted": bool(metadata.get("text_extracted")),
                "scan_candidate": bool(metadata.get("scan_candidate")),
                "openai_index_status": str(metadata.get("openai_index_status") or "").strip(),
                "retrieval_source": "openai_vector_store",
            }

    return sorted(
        aggregated.values(),
        key=lambda item: (float(item.get("score") or 0.0), str(item.get("title") or "")),
        reverse=True,
    )[:normalized_limit]


def _extract_response_text(response: Any) -> str:
    direct = str(getattr(response, "output_text", "") or "").strip()
    if direct:
        return direct

    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", "") != "message":
            continue
        for content in getattr(item, "content", []) or []:
            if getattr(content, "type", "") == "output_text":
                text = str(getattr(content, "text", "") or "").strip()
                if text:
                    chunks.append(text)
    return "\n".join(chunks).strip()


def analyze_client_file_with_input_file(client_file, *, query: str, client=None) -> dict[str, Any]:
    normalized_query = (query or "").strip()
    if not normalized_query:
        return {"error": "A document-analysis query is required."}

    metadata = _normalized_metadata(client_file)
    metadata["scan_candidate"] = _is_scan_candidate(client_file, metadata)
    if client is None and not openai_client_files_enabled():
        metadata.setdefault("openai_index_status", "not_configured")
        _save_metadata_if_changed(client_file, metadata)
        return {"error": "OpenAI document analysis is not configured for client files."}

    try:
        client = client or _new_openai_file_client()
        if client is None:
            return {"error": "OpenAI document analysis is not configured for client files."}

        openai_file_id, metadata = _ensure_openai_file_id(client_file, client=client, metadata=metadata)
        if not openai_file_id:
            _save_metadata_if_changed(client_file, metadata)
            return {"error": "The original file could not be prepared for OpenAI document analysis."}

        response = client.responses.create(
            model=OPENAI_CLIENT_FILE_ANALYSIS_MODEL,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Read the attached uploaded client document and answer the query using only "
                                "what appears in the file. If parts of the file are unreadable, image-based, "
                                "or uncertain, say so plainly.\n\nQuery:\n"
                                + normalized_query
                            ),
                        },
                        {
                            "type": "input_file",
                            "file_id": openai_file_id,
                        },
                    ],
                }
            ],
            tools=[],
            tool_choice="none",
            max_output_tokens=OPENAI_CLIENT_FILE_ANALYSIS_MAX_OUTPUT_TOKENS,
            reasoning={"effort": "medium"},
            store=False,
            truncation="auto",
        )
        analysis = _extract_response_text(response)
        metadata["openai_last_analyzed_at"] = timezone.now().isoformat()
        _save_metadata_if_changed(client_file, metadata)

        return {
            "id": client_file.id,
            "title": client_file.title,
            "filename": _metadata_filename(client_file, metadata),
            "extension": _metadata_extension(client_file, metadata),
            "metadata": metadata,
            "analysis": analysis,
        }
    except Exception as exc:
        logger.exception(
            "Unable to analyze uploaded client document with OpenAI input_file",
            extra={
                "document_id": str(client_file.document_id),
                "client_file_id": client_file.id,
            },
        )
        metadata["openai_analysis_error"] = str(exc)[:500]
        _save_metadata_if_changed(client_file, metadata)
        return {"error": f"OpenAI document analysis failed: {str(exc)[:300]}"}

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from django.utils import timezone

from .document_text import clip_document_text, extract_plain_text
from .document_file_service import rank_client_files
from .exemplar_service import rank_exemplars
from .models import DocumentClientFile, DocumentResearchRun, Exemplar

logger = logging.getLogger(__name__)

AGENT_MODEL = os.environ.get("OPENAI_AGENT_MODEL", "gpt-5.4")
AGENT_REASONING_EFFORT = os.environ.get("OPENAI_AGENT_REASONING_EFFORT", "high").strip().lower() or "high"
AGENT_MAX_TOOL_CALLS = int(os.environ.get("OPENAI_AGENT_MAX_TOOL_CALLS", "36"))
AGENT_MAX_OUTPUT_TOKENS = int(os.environ.get("OPENAI_AGENT_MAX_OUTPUT_TOKENS", "2400"))
AGENT_HTTP_TIMEOUT_SECONDS = int(os.environ.get("OPENAI_AGENT_HTTP_TIMEOUT_SECONDS", "25"))
AGENT_MAX_RUN_SECONDS = int(os.environ.get("OPENAI_AGENT_MAX_RUN_SECONDS", "480"))
AGENT_MAX_LOCAL_FUNCTION_ROUNDS = int(os.environ.get("OPENAI_AGENT_MAX_LOCAL_FUNCTION_ROUNDS", "12"))
AGENT_MAX_TOTAL_TOKENS = int(os.environ.get("OPENAI_AGENT_MAX_TOTAL_TOKENS", "180000"))
AGENT_FINALIZATION_REASONING_EFFORT = (
    os.environ.get("OPENAI_AGENT_FINALIZATION_REASONING_EFFORT", "low").strip().lower() or "low"
)
AGENT_FINALIZATION_MAX_OUTPUT_TOKENS = int(
    os.environ.get("OPENAI_AGENT_FINALIZATION_MAX_OUTPUT_TOKENS", "1400")
)
AGENT_JSON_REPAIR_REASONING_EFFORT = (
    os.environ.get("OPENAI_AGENT_JSON_REPAIR_REASONING_EFFORT", "none").strip().lower() or "none"
)
KNOWLEDGE_VECTOR_STORE_IDS = [
    value.strip()
    for value in os.environ.get("OPENAI_AGENT_KNOWLEDGE_VECTOR_STORE_ID", "").split(",")
    if value.strip()
]

_BIAEDGE_CHAT_TOOLS = [
    "search_cases",
    "get_document",
    "get_document_text",
    "get_citations",
    "get_most_cited",
    "check_validity",
    "get_holdings",
    "search_holdings_by_text",
    "get_headnotes",
    "search_headnotes",
    "search_statutes",
    "get_statute",
    "search_references",
    "get_reference",
    "get_regulatory_updates",
]
_BIAEDGE_SUGGEST_TOOLS = [
    "search_cases",
    "get_document",
    "get_document_text",
    "get_citations",
    "check_validity",
    "get_holdings",
    "search_holdings_by_text",
    "search_statutes",
    "get_statute",
    "search_references",
    "get_reference",
    "get_regulatory_updates",
]
_TOOL_INCLUDE_FIELDS = [
    "web_search_call.action.sources",
    "file_search_call.results",
]
_CHAT_TRANSCRIPT_LIMIT = 12
_MAX_FUNCTION_ROUNDS = 8
_JSON_REPAIR_MAX_OUTPUT_TOKENS = 1600
_CONTINUE_RESPONSE_MAX_OUTPUT_TOKENS = 1400
_ACTIVE_RUN_STATUSES = {"queued", "in_progress"}
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
_CHAT_DOCUMENT_MAX_CHARS = int(os.environ.get("OPENAI_AGENT_CHAT_DOCUMENT_MAX_CHARS", "12000"))
_CHAT_DOCUMENT_TAIL_CHARS = int(os.environ.get("OPENAI_AGENT_CHAT_DOCUMENT_TAIL_CHARS", "3000"))
_SUGGEST_DOCUMENT_MAX_CHARS = int(os.environ.get("OPENAI_AGENT_SUGGEST_DOCUMENT_MAX_CHARS", "16000"))
_SUGGEST_DOCUMENT_TAIL_CHARS = int(os.environ.get("OPENAI_AGENT_SUGGEST_DOCUMENT_TAIL_CHARS", "4000"))
_EDIT_DOCUMENT_MAX_CHARS = int(os.environ.get("OPENAI_AGENT_EDIT_DOCUMENT_MAX_CHARS", "8000"))
_EDIT_DOCUMENT_TAIL_CHARS = int(os.environ.get("OPENAI_AGENT_EDIT_DOCUMENT_TAIL_CHARS", "2000"))
_TOOL_OUTPUT_EXCERPT_MAX_CHARS = int(os.environ.get("OPENAI_AGENT_TOOL_OUTPUT_EXCERPT_MAX_CHARS", "1200"))
_TOOL_OUTPUT_EXCERPT_TAIL_CHARS = int(os.environ.get("OPENAI_AGENT_TOOL_OUTPUT_EXCERPT_TAIL_CHARS", "240"))
_TOOL_RESULT_DIGEST_MAX_CHARS = int(os.environ.get("OPENAI_AGENT_TOOL_RESULT_DIGEST_MAX_CHARS", "12000"))
_TOOL_RESULT_DIGEST_MAX_ITEMS = int(os.environ.get("OPENAI_AGENT_TOOL_RESULT_DIGEST_MAX_ITEMS", "12"))
_LOCAL_TOOL_TEXT_MAX_CHARS = int(os.environ.get("OPENAI_AGENT_LOCAL_TOOL_TEXT_MAX_CHARS", "6000"))
_LOCAL_TOOL_TEXT_TAIL_CHARS = int(os.environ.get("OPENAI_AGENT_LOCAL_TOOL_TEXT_TAIL_CHARS", "1200"))

_RUN_PHASE_INTAKE = "intake"
_RUN_PHASE_RESEARCH = "research"
_RUN_PHASE_VERIFY = "verify"
_RUN_PHASE_FINALIZE = "finalize"
_RUN_PHASE_PERSIST = "persist"
_RUN_PHASE_COMPLETED = "completed"
_RUN_PHASE_FAILED = "failed"
_RUN_PHASE_CANCELLED = "cancelled"

_STAGE_TO_PHASE = {
    "queued": _RUN_PHASE_INTAKE,
    "waiting_openai": _RUN_PHASE_RESEARCH,
    "running_tools": _RUN_PHASE_RESEARCH,
    "continuing": _RUN_PHASE_FINALIZE,
    "verifying_quotes": _RUN_PHASE_VERIFY,
    "forcing_final": _RUN_PHASE_FINALIZE,
    "recovering_failure": _RUN_PHASE_FINALIZE,
    "repairing_json": _RUN_PHASE_FINALIZE,
    "persisting": _RUN_PHASE_PERSIST,
    "completed": _RUN_PHASE_COMPLETED,
    "failed": _RUN_PHASE_FAILED,
    "cancelled": _RUN_PHASE_CANCELLED,
}

_MODE_TOOL_POLICY = {
    "chat": {
        "include_web_search": True,
        "web_search_context_size": "medium",
        "include_client_docs": True,
        "include_knowledge": True,
        "include_file_search": True,
    },
    "suggest": {
        "include_web_search": True,
        "web_search_context_size": "low",
        "include_client_docs": True,
        "include_knowledge": True,
        "include_file_search": True,
    },
    "edit": {
        "include_web_search": True,
        "web_search_context_size": "medium",
        "include_client_docs": True,
        "include_knowledge": True,
        "include_file_search": True,
    },
}

_CHAT_SYSTEM_PROMPT = """You are Hammond Law's document-side immigration research agent.
You work alongside the attorney inside a live drafting session.

Research operating rules:
1. Use the BIA Edge database tools first for legal authorities, holdings, statutes, regulations, policy sections, and validity checks.
2. Use the knowledge-base tools for firm exemplars, prior briefs, and internal language only when the user is asking about internal style, phrasing, formatting, or prior work product.
3. Use the document client-file tools when factual support, biographical detail, chronology, exhibits, or source language from uploaded client documents would help answer the question.
4. Use web search only when freshness matters, when the user explicitly asks for it, or when database tools do not answer the question.
5. Do not invent citations, case names, statutes, regulations, policy sections, quoted passages, or facts from uploaded client files.
6. Prefer precedential authorities in your legal analysis. If you mention unpublished decisions or non-precedential material, label that clearly.
7. If the user asks you to search the database, actually use the database tools instead of answering from memory.
8. If you rely on uploaded client documents for a factual statement or quote, retrieve the relevant client file first.
9. If controlling authority is thin or uncertain, say so directly.
10. Research efficiently. Start with targeted searches, then inspect only the strongest authorities you need.
11. Use search_client_documents or search_exemplars first, then retrieve only the one or two files you actually need.
12. Avoid repeated searches for the same citation or issue unless the earlier result was clearly insufficient.
13. Use get_document_text sparingly. Prefer short targeted excerpts and avoid requesting very large full-text pulls unless they are truly necessary.
14. Honor the Turn requirements JSON block in the input. If it requires exact quotes or full-text verification, complete that before answering.
15. Once you have enough verified authority to answer the attorney, stop calling tools and give the answer.

Output style:
- Be direct, practical, and collaborative.
- Tailor advice to the current document type and the text already drafted.
- When helpful, end with a short "Authorities to review:" list.
- Cite legal authorities in normal legal style inside the prose.
"""

_SUGGEST_SYSTEM_PROMPT = """You are Hammond Law's authority-suggestion agent for immigration drafting.
Your task is to suggest the best legal authorities for the user's selected passage and current draft.

Hard rules:
1. Final suggested authorities must be verified through BIA Edge database tools.
2. Prefer precedential cases, statutes, regulations, and published policy.
3. Use uploaded client-document tools, web search, or knowledge tools only for context; do not include web-only authorities in the final authority list.
4. Do not invent any citation, holding, proposition, or fact from uploaded client documents.
5. If the selected text is vague or under-supported, explain the gap instead of bluffing.
6. Return valid JSON only. No markdown fences, no prose outside the JSON object.
7. Research efficiently. Start broad, then narrow to the best 2 to 5 authorities for this passage.
8. Avoid repeated searches for the same citation or issue unless the earlier result was clearly insufficient.
9. Use get_document_text sparingly. Prefer short targeted excerpts and avoid requesting very large full-text pulls unless they are truly necessary.
10. Honor the Turn requirements JSON block in the input. If it requires exact quotes or full-text verification, complete that before drafting any quoted pinpoint.
11. Once you have enough verified authority to support the selection, stop calling tools and return the JSON object.

Return an object with this exact shape:
{
  "selection_summary": "short summary of the issue raised by the selected text",
  "draft_gap": "what authority or legal point the draft appears to need",
  "authorities": [
    {
      "kind": "case | statute | regulation | policy",
      "title": "authority name",
      "citation": "formal citation if available",
      "document_id": 123,
      "reference_id": null,
      "precedential_status": "precedential | non_precedential | n/a",
      "validity_status": "good_law | questioned | overruled | unknown | n/a",
      "relevance": "why this matters to the selected text",
      "suggested_use": "how the attorney should use this authority in the paragraph or section",
      "pinpoint": "brief holding, rule, or pinpoint passage"
    }
  ],
  "search_notes": "brief note about search coverage or limits",
  "next_questions": ["optional follow-up question 1", "optional follow-up question 2"]
}
"""

_EDIT_SYSTEM_PROMPT = """You are Hammond Law's document editing agent for immigration drafting.
You work inside a live draft and may research before proposing a controlled edit.

Hard rules:
1. Read the current document context and any selected text before proposing an edit.
2. Use BIA Edge first for legal authorities. Use knowledge tools for internal style and prior work product. Use document client-file tools for uploaded factual materials. Use web search only when freshness matters or the user explicitly asks for it.
3. Do not invent citations, quoted language, legal standards, case support, or facts from uploaded client documents.
4. Propose one concrete edit only. Do not rewrite the whole document unless the user explicitly asks for that and the operation is append_to_document.
5. The final answer must be valid JSON only. No markdown fences and no prose outside the JSON object.
6. proposed_text must be plain drafting text, preserving paragraph breaks but not markdown formatting.
7. If selected text was provided, use it as the target for replace_selection, insert_before_selection, insert_after_selection, or delete_selection.
8. If no selected text was provided but the user identified a location in the draft, target an exact existing heading or paragraph from the current document in target_text and use replace_selection, insert_before_selection, insert_after_selection, or delete_selection as appropriate.
9. Use append_to_document only when the user did not identify a reliable place in the existing draft.
10. Honor the Turn requirements JSON block in the input. If exact quotes are required, verify them before drafting the edit.
11. Once you have enough verified support to propose the edit, stop calling tools and return the JSON object.

Return an object with this exact shape:
{
  "edit_summary": "short summary of the proposed change",
  "rationale": "why this change improves the draft",
  "operation": "replace_selection | insert_before_selection | insert_after_selection | append_to_document | delete_selection",
  "target_text": "the text being revised or removed, or an empty string for append_to_document",
  "proposed_text": "the exact text to insert; may be empty only for delete_selection",
  "notes": "optional implementation note or drafting caveat"
}
"""


class AgentConfigurationError(ValueError):
    pass


class AgentExecutionError(RuntimeError):
    pass


class StaleResponseChainError(AgentExecutionError):
    pass


@dataclass
class ChatAgentResult:
    answer: str
    response_id: str
    tool_calls: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    used_tools: list[str]
    metadata: dict[str, Any]


@dataclass
class SuggestAgentResult:
    selection_summary: str
    draft_gap: str
    authorities: list[dict[str, Any]]
    search_notes: str
    next_questions: list[str]
    response_id: str
    tool_calls: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    raw_answer: str


@dataclass
class EditAgentResult:
    edit_summary: str
    rationale: str
    operation: str
    target_text: str
    proposed_text: str
    notes: str
    response_id: str
    tool_calls: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    raw_answer: str


def _response_status(response: Any) -> str:
    return (getattr(response, "status", "") or "").strip().lower()


def _usage_to_dict(usage: Any) -> dict[str, int]:
    if not usage:
        return {}

    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        "cached_input_tokens": int(getattr(input_details, "cached_tokens", 0) or 0),
        "reasoning_tokens": int(getattr(output_details, "reasoning_tokens", 0) or 0),
    }


def _sum_usage_by_response(usage_by_response_id: dict[str, dict[str, Any]]) -> dict[str, int]:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
    }
    for payload in usage_by_response_id.values():
        if not isinstance(payload, dict):
            continue
        for key in totals:
            totals[key] += int(payload.get(key) or 0)
    return totals


def _merge_unique_records(existing: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in list(existing or []) + list(new_items or []):
        if not isinstance(item, dict):
            continue
        key = json.dumps(item, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _response_error_message(response: Any) -> str:
    message = _extract_error_text(getattr(response, "error", None))
    if message:
        return message
    incomplete_details = getattr(response, "incomplete_details", None)
    if incomplete_details:
        reason = str(getattr(incomplete_details, "reason", "") or "").strip()
        if reason:
            return f"OpenAI response incomplete: {reason}."
    status = _response_status(response)
    if status:
        return f"OpenAI response returned status={status}."
    return "OpenAI response failed."


def _looks_like_generic_failed_status(message: str) -> bool:
    normalized = (message or "").strip().lower()
    return normalized in {
        "openai response returned status=failed.",
        "openai response failed.",
    }


def _safe_json_loads(raw: Any, default=None):
    if isinstance(raw, (dict, list)):
        return raw
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _normalize_mcp_server_url(raw_url: str) -> str:
    normalized = (raw_url or "").strip()
    if not normalized:
        return ""

    if "://" not in normalized:
        host = normalized.lstrip("/")
        scheme = "http" if host.startswith(("localhost", "127.0.0.1", "0.0.0.0")) else "https"
        normalized = f"{scheme}://{host}"

    parsed = urlparse(normalized)
    if not parsed.scheme or not parsed.netloc:
        return normalized
    if parsed.path in {"", "/"}:
        parsed = parsed._replace(path="/mcp")
        return urlunparse(parsed)
    return normalized


def _normalize_reasoning_effort(raw_effort: str) -> str:
    effort = (raw_effort or "").strip().lower()
    if effort in {"none", "low", "medium", "high", "xhigh"}:
        return effort
    return "high"


def _new_openai_client():
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise AgentConfigurationError("OPENAI_API_KEY is not configured.")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise AgentConfigurationError("The openai package is not installed.") from exc

    return OpenAI(api_key=api_key, timeout=AGENT_HTTP_TIMEOUT_SECONDS)


def _build_biaedge_mcp_tool(*, allowed_tools: list[str]) -> dict[str, Any]:
    server_url = _normalize_mcp_server_url(os.environ.get("BIAEDGE_MCP_SERVER_URL", ""))
    if not server_url:
        raise AgentConfigurationError("BIAEDGE_MCP_SERVER_URL is not configured.")

    headers = {}
    api_key = os.environ.get("BIAEDGE_MCP_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    tool = {
        "type": "mcp",
        "server_label": "biaedge",
        "server_url": server_url,
        "allowed_tools": list(allowed_tools),
        "require_approval": "never",
        "server_description": "BIA Edge immigration legal research database.",
    }
    if headers:
        tool["headers"] = headers
    return tool


def _build_web_search_tool(*, search_context_size: str = "medium") -> dict[str, Any]:
    return {
        "type": "web_search_preview",
        "search_context_size": search_context_size,
        "user_location": {
            "type": "approximate",
            "country": "US",
        },
    }


def _build_file_search_tool() -> dict[str, Any] | None:
    if not KNOWLEDGE_VECTOR_STORE_IDS:
        return None
    return {
        "type": "file_search",
        "vector_store_ids": KNOWLEDGE_VECTOR_STORE_IDS,
        "max_num_results": 8,
    }


def _search_exemplars_for_agent(*, user, query: str, limit: int = 5, document_type_slug: str = ""):
    normalized_query = (query or "").strip()
    normalized_limit = max(1, min(int(limit or 5), 8))
    lowered_query = normalized_query.lower()
    style_query = any(
        phrase in lowered_query
        for phrase in ["style", "format", "structure", "header", "signature", "exhibit", "cover letter"]
    )
    qs = Exemplar.objects.filter(created_by=user, is_active=True).select_related("document_type")
    if not style_query:
        qs = qs.exclude(kind__in=["style_anchor", "section_template"])
    if document_type_slug:
        qs = qs.filter(document_type__slug=document_type_slug)

    exemplars = []
    for exemplar in qs[:200]:
        text = exemplar.extracted_text or ""
        exemplars.append(
                {
                    "id": exemplar.id,
                    "title": exemplar.title,
                    "document_type": exemplar.document_type.name if exemplar.document_type else "",
                    "document_type_slug": exemplar.document_type.slug if exemplar.document_type else "",
                    "kind": exemplar.kind,
                    "style_family": exemplar.style_family,
                    "case_type": exemplar.case_type,
                    "outcome": exemplar.outcome,
                    "tags": exemplar.tags or [],
                    "updated_at": exemplar.updated_at.isoformat(),
                    "snippet": text[:500],
                "extracted_text": text[:4000],
                "embedding": exemplar.embedding or [],
            }
        )

    ranked = rank_exemplars(normalized_query, exemplars)
    return {
        "results": [
            {
                "id": item["id"],
                "title": item["title"],
                "document_type": item["document_type"],
                "document_type_slug": item["document_type_slug"],
                "kind": item["kind"],
                "style_family": item["style_family"],
                "case_type": item["case_type"],
                "outcome": item["outcome"],
                "tags": item["tags"],
                "snippet": item["snippet"],
                "score": round(float(item.get("score", 0.0) or 0.0), 4),
            }
            for item in ranked[:normalized_limit]
        ]
    }


def _get_exemplar_for_agent(*, user, exemplar_id: int):
    exemplar = Exemplar.objects.filter(
        created_by=user,
        id=exemplar_id,
        is_active=True,
    ).select_related("document_type").first()
    if not exemplar:
        return {"error": "Exemplar not found."}

    return {
        "id": exemplar.id,
        "title": exemplar.title,
        "document_type": exemplar.document_type.name if exemplar.document_type else "",
        "document_type_slug": exemplar.document_type.slug if exemplar.document_type else "",
        "kind": exemplar.kind,
        "style_family": exemplar.style_family,
        "case_type": exemplar.case_type,
        "outcome": exemplar.outcome,
        "tags": exemplar.tags or [],
        "metadata": exemplar.metadata or {},
        "text": clip_document_text(
            exemplar.extracted_text or "",
            max_chars=_LOCAL_TOOL_TEXT_MAX_CHARS,
            tail_chars=min(_LOCAL_TOOL_TEXT_TAIL_CHARS, _LOCAL_TOOL_TEXT_MAX_CHARS),
        ),
    }


def _search_client_files_for_agent(*, document, query: str, limit: int = 5):
    normalized_query = (query or "").strip()
    normalized_limit = max(1, min(int(limit or 5), 8))

    items = []
    for client_file in document.client_files.all()[:200]:
        text = client_file.extracted_text or ""
        items.append(
            {
                "id": client_file.id,
                "title": client_file.title,
                "filename": (client_file.metadata or {}).get("filename") or "",
                "extension": (client_file.metadata or {}).get("extension") or "",
                "updated_at": client_file.updated_at.isoformat(),
                "snippet": text[:500],
                "extracted_text": text[:5000],
                "embedding": client_file.embedding or [],
            }
        )

    ranked = rank_client_files(normalized_query, items)
    return {
        "results": [
            {
                "id": item["id"],
                "title": item["title"],
                "filename": item["filename"],
                "extension": item["extension"],
                "snippet": item["snippet"],
                "score": round(float(item.get("score", 0.0) or 0.0), 4),
            }
            for item in ranked[:normalized_limit]
        ]
    }


def _get_client_file_for_agent(*, document, file_id: int):
    client_file = DocumentClientFile.objects.filter(
        document=document,
        id=file_id,
    ).first()
    if not client_file:
        return {"error": "Client document not found."}

    return {
        "id": client_file.id,
        "title": client_file.title,
        "filename": (client_file.metadata or {}).get("filename") or "",
        "extension": (client_file.metadata or {}).get("extension") or "",
        "metadata": client_file.metadata or {},
        "text": clip_document_text(
            client_file.extracted_text or "",
            max_chars=_LOCAL_TOOL_TEXT_MAX_CHARS,
            tail_chars=min(_LOCAL_TOOL_TEXT_TAIL_CHARS, _LOCAL_TOOL_TEXT_MAX_CHARS),
        ),
    }


def _knowledge_function_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "search_exemplars",
            "description": "Search the firm's exemplar bank for prior briefs, motions, cover letters, and internal work product.",
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for in the exemplar bank.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "How many exemplar search results to return. Use a small integer from 1 to 8.",
                    },
                    "document_type_slug": {
                        "type": "string",
                        "description": "Document type slug to narrow the exemplar search, or an empty string when no filter is needed.",
                    },
                },
                "required": ["query", "limit", "document_type_slug"],
            },
        },
        {
            "type": "function",
            "name": "get_exemplar",
            "description": "Fetch the text and metadata for a specific exemplar from the firm's knowledge base.",
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "exemplar_id": {
                        "type": "integer",
                        "description": "The exemplar ID returned by search_exemplars.",
                    }
                },
                "required": ["exemplar_id"],
            },
        },
    ]


def _client_file_function_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "search_client_documents",
            "description": "Search the uploaded client documents attached to the current draft for facts, chronology, names, exhibits, and source language.",
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for in the uploaded client documents.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "How many client-document search results to return. Use a small integer from 1 to 8.",
                    },
                },
                "required": ["query", "limit"],
            },
        },
        {
            "type": "function",
            "name": "get_client_document",
            "description": "Fetch the extracted text and metadata for a specific uploaded client document attached to the current draft.",
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "file_id": {
                        "type": "integer",
                        "description": "The client document ID returned by search_client_documents.",
                    }
                },
                "required": ["file_id"],
            },
        },
    ]


def _extract_output_text(response: Any) -> str:
    direct = (getattr(response, "output_text", "") or "").strip()
    if direct:
        return direct

    chunks = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            if getattr(part, "type", None) == "output_text":
                text = getattr(part, "text", "") or ""
                if text:
                    chunks.append(text)
            elif getattr(part, "type", None) == "refusal":
                refusal = getattr(part, "refusal", "") or ""
                if refusal:
                    chunks.append(refusal)
    return "\n".join(chunks).strip()


def _compact_tool_output(raw_output: Any) -> str:
    if raw_output in (None, ""):
        return ""

    parsed = _safe_json_loads(raw_output, default=None)
    if parsed is None:
        text = str(raw_output)
    else:
        try:
            text = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), default=str)
        except Exception:
            text = str(parsed)

    return clip_document_text(
        text,
        max_chars=_TOOL_OUTPUT_EXCERPT_MAX_CHARS,
        tail_chars=min(_TOOL_OUTPUT_EXCERPT_TAIL_CHARS, _TOOL_OUTPUT_EXCERPT_MAX_CHARS),
    )


def _public_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized = []
    for item in tool_calls or []:
        if not isinstance(item, dict):
            continue
        sanitized.append(
            {
                key: value
                for key, value in item.items()
                if key not in {"output_excerpt"}
            }
        )
    return sanitized


def _join_text_fragments(fragments: list[str]) -> str:
    combined = ""
    for fragment in fragments:
        text = str(fragment or "")
        if not text:
            continue
        if not combined:
            combined = text
            continue
        if combined[-1].isspace() or text[0].isspace() or text[0] in ",.;:!?)]}":
            combined += text
        else:
            combined += " " + text
    return combined


def _requested_full_text_sources(text: str) -> set[str]:
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return set()

    quote_requested = bool(
        re.search(r"\bquote(?:s|d)?\b", normalized)
        or any(
            phrase in normalized
            for phrase in [
                "exact language",
                "verbatim",
                "actual text",
                "pull the text",
                "pull text",
                "quoted language",
            ]
        )
    )
    if not quote_requested:
        return set()

    sources: set[str] = set()
    if any(phrase in normalized for phrase in ["policy manual", "uscis policy", "uscis-pm", "uscis pm"]):
        sources.add("policy")
    if (
        re.search(r"\b(case law|cases|decisions?|holdings?)\b", normalized)
        or "quote from case" in normalized
        or "quotes from case" in normalized
    ):
        sources.add("case")
    if re.search(r"\b(statutes?|regulations?|cfr|c\.f\.r\.|u\.s\.c\.|usc|ina)\b", normalized):
        sources.add("statute")
    return sources


_FULL_TEXT_SOURCE_LABELS = {
    "policy": "policy",
    "statute": "statute_or_regulation",
    "case": "case_law",
}

_FULL_TEXT_SOURCE_TOOL_RULES = {
    "policy": "policy=search_references(source_code='uscis_pm')->get_reference",
    "statute": "statute_or_regulation=get_statute",
    "case": "case_law=get_document_text",
}


def _request_requirements(text: str) -> dict[str, Any]:
    requested_sources = sorted(_requested_full_text_sources(text))
    return {
        "requires_exact_quotes": bool(requested_sources),
        "required_full_text_sources": requested_sources,
    }


def _stage_phase(stage: str) -> str:
    normalized = (stage or "").strip().lower()
    return _STAGE_TO_PHASE.get(normalized, _RUN_PHASE_RESEARCH)


def _make_turn_requirements(
    *,
    mode: str,
    request_text: str,
    selected_text: str = "",
    has_client_files: bool = False,
    has_active_exemplars: bool = False,
) -> dict[str, Any]:
    requirements = _request_requirements(request_text)
    output_format = {
        "suggest": "suggest_json",
        "edit": "edit_json",
    }.get(mode, "chat_text")
    return {
        "mode": mode,
        "output_format": output_format,
        "requires_exact_quotes": bool(requirements["requires_exact_quotes"]),
        "required_full_text_sources": list(requirements["required_full_text_sources"]),
        "required_tool_sequence": [
            _FULL_TEXT_SOURCE_TOOL_RULES[source]
            for source in requirements["required_full_text_sources"]
            if source in _FULL_TEXT_SOURCE_TOOL_RULES
        ],
        "has_selected_text": bool((selected_text or "").strip()),
        "client_documents_available": bool(has_client_files),
        "knowledge_available": bool(has_active_exemplars),
        "web_search_policy": "freshness_or_user_request_only",
        "primary_authority_source": "biaedge",
        "finalization_rule": (
            "Do not claim exact quoted text unless the relevant full-text tool was used in this turn."
        ),
    }


def _request_requirements_block(text: str) -> str:
    requirements = _make_turn_requirements(mode="chat", request_text=text)
    return _turn_requirements_block(requirements)


def _turn_requirements_block(requirements: dict[str, Any]) -> str:
    if not isinstance(requirements, dict):
        return ""
    payload = {
        "mode": str(requirements.get("mode") or "").strip(),
        "output_format": str(requirements.get("output_format") or "").strip(),
        "requires_exact_quotes": bool(requirements.get("requires_exact_quotes")),
        "required_full_text_sources": list(requirements.get("required_full_text_sources") or []),
        "required_tool_sequence": list(requirements.get("required_tool_sequence") or []),
        "has_selected_text": bool(requirements.get("has_selected_text")),
        "client_documents_available": bool(requirements.get("client_documents_available")),
        "knowledge_available": bool(requirements.get("knowledge_available")),
        "web_search_policy": str(requirements.get("web_search_policy") or "").strip(),
        "primary_authority_source": str(requirements.get("primary_authority_source") or "").strip(),
        "finalization_rule": str(requirements.get("finalization_rule") or "").strip(),
    }
    return "Turn requirements JSON:\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)


def _extract_citations(response: Any) -> list[dict[str, Any]]:
    citations = []
    seen = set()

    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            if getattr(part, "type", None) != "output_text":
                continue
            for annotation in getattr(part, "annotations", []) or []:
                annotation_type = getattr(annotation, "type", "") or ""
                if annotation_type == "url_citation":
                    payload = {
                        "type": "web",
                        "title": getattr(annotation, "title", "") or "",
                        "url": getattr(annotation, "url", "") or "",
                    }
                elif annotation_type == "file_citation":
                    payload = {
                        "type": "file",
                        "file_id": getattr(annotation, "file_id", "") or "",
                        "filename": getattr(annotation, "filename", "") or "",
                    }
                else:
                    continue

                identity = tuple(sorted(payload.items()))
                if identity in seen:
                    continue
                seen.add(identity)
                citations.append(payload)

    return citations


def _extract_hosted_tool_calls(response: Any) -> list[dict[str, Any]]:
    tool_calls = []
    for item in getattr(response, "output", []) or []:
        item_type = getattr(item, "type", None)
        if item_type == "mcp_call":
            record = {
                "source": "biaedge",
                "type": "mcp_call",
                "name": getattr(item, "name", "") or "",
                "status": getattr(item, "status", "") or "",
                "arguments": _safe_json_loads(getattr(item, "arguments", ""), default={}) or {},
            }
            error = (getattr(item, "error", "") or "").strip()
            if error:
                record["error"] = error[:500]
            output_excerpt = _compact_tool_output(getattr(item, "output", "") or "")
            if output_excerpt:
                record["output_excerpt"] = output_excerpt
            tool_calls.append(record)
        elif item_type == "web_search_call":
            tool_calls.append(
                {
                    "source": "web",
                    "type": "web_search_call",
                    "name": "web_search",
                    "status": getattr(item, "status", "") or "",
                }
            )
        elif item_type == "file_search_call":
            tool_calls.append(
                {
                    "source": "knowledge",
                    "type": "file_search_call",
                    "name": "file_search",
                    "status": getattr(item, "status", "") or "",
                }
            )
    return tool_calls


def _tool_result_digest(tool_calls: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    total_chars = 0

    for item in tool_calls or []:
        if not isinstance(item, dict):
            continue

        excerpt = str(item.get("output_excerpt") or "").strip()
        error = str(item.get("error") or "").strip()
        if not excerpt and not error:
            continue

        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        interesting_args = {
            key: arguments[key]
            for key in ["query", "document_id", "reference_id", "source", "source_code", "limit"]
            if key in arguments and arguments[key] not in (None, "", [])
        }

        lines = [f"- {item.get('source', 'tool')}::{item.get('name', 'unknown')}"]
        if interesting_args:
            try:
                lines.append(f"  args: {json.dumps(interesting_args, ensure_ascii=False, sort_keys=True)}")
            except Exception:
                lines.append(f"  args: {interesting_args}")
        if error:
            lines.append(f"  error: {error}")
        if excerpt:
            lines.append(f"  result: {excerpt}")

        block = "\n".join(lines)
        if total_chars and total_chars + len(block) > _TOOL_RESULT_DIGEST_MAX_CHARS:
            break
        blocks.append(block)
        total_chars += len(block)
        if len(blocks) >= _TOOL_RESULT_DIGEST_MAX_ITEMS:
            break

    return "\n\n".join(blocks).strip()


def _build_evidence_pack(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    pack = {
        "legal_authorities": [],
        "client_documents": [],
        "knowledge": [],
        "web": [],
        "errors": [],
    }
    counts = {key: 0 for key in pack}

    for item in tool_calls or []:
        if not isinstance(item, dict):
            continue

        source = str(item.get("source") or "").strip()
        name = str(item.get("name") or "").strip()
        excerpt = str(item.get("output_excerpt") or "").strip()
        error = str(item.get("error") or "").strip()
        arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        record = {
            "source": source,
            "tool": name,
        }
        if arguments:
            record["args"] = {
                key: value
                for key, value in arguments.items()
                if key in {"query", "document_id", "reference_id", "source_code", "limit", "file_id", "exemplar_id"}
                and value not in ("", None, [], {})
            }
        if excerpt:
            record["excerpt"] = excerpt
        if error:
            record["error"] = error[:500]

        bucket = None
        if error and not excerpt:
            bucket = "errors"
        elif source == "biaedge":
            bucket = "legal_authorities"
        elif source == "client_docs":
            bucket = "client_documents"
        elif source == "knowledge":
            bucket = "knowledge"
        elif source == "web":
            bucket = "web"

        if not bucket:
            continue
        counts[bucket] += 1
        if len(pack[bucket]) < 4:
            pack[bucket].append(record)

    pack["counts"] = counts
    return pack


def _evidence_pack_text(pack: dict[str, Any]) -> str:
    if not isinstance(pack, dict):
        return ""

    section_order = [
        ("legal_authorities", "Legal authorities"),
        ("client_documents", "Client documents"),
        ("knowledge", "Knowledge and exemplars"),
        ("web", "Web search"),
        ("errors", "Tool issues"),
    ]
    lines: list[str] = []
    total_chars = 0

    for key, label in section_order:
        items = pack.get(key) or []
        if not isinstance(items, list) or not items:
            continue
        lines.append(f"{label}:")
        total_chars += len(lines[-1])
        for item in items:
            if not isinstance(item, dict):
                continue
            parts = [f"- {item.get('tool', 'tool')}"]
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            if args:
                try:
                    parts.append(f"args={json.dumps(args, ensure_ascii=False, sort_keys=True)}")
                except Exception:
                    parts.append(f"args={args}")
            excerpt = str(item.get("excerpt") or "").strip()
            if excerpt:
                parts.append(f"excerpt={excerpt}")
            error = str(item.get("error") or "").strip()
            if error:
                parts.append(f"error={error}")
            line = " | ".join(parts)
            if total_chars and total_chars + len(line) > _TOOL_RESULT_DIGEST_MAX_CHARS:
                return "\n".join(lines).strip()
            lines.append(line)
            total_chars += len(line)

    return "\n".join(lines).strip()


def _tool_usage_metrics(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    source_counts: dict[str, int] = {}
    tool_name_counts: dict[str, int] = {}
    for item in tool_calls or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        name = str(item.get("name") or "").strip()
        if source:
            source_counts[source] = source_counts.get(source, 0) + 1
        if name:
            tool_name_counts[name] = tool_name_counts.get(name, 0) + 1
    return {
        "tool_call_count": len(tool_calls or []),
        "tool_source_counts": source_counts,
        "tool_name_counts": tool_name_counts,
        "used_sources": sorted(source_counts.keys()),
    }


def _pending_function_calls(response: Any) -> list[Any]:
    calls = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) == "function_call":
            calls.append(item)
    return calls


def _used_tools(tool_calls: list[dict[str, Any]]) -> list[str]:
    seen = []
    for call in tool_calls:
        source = (call.get("source") or "").strip()
        if source and source not in seen:
            seen.append(source)
    return seen


def _stale_previous_response(exc: Exception) -> bool:
    text = str(exc).lower()
    return "previous_response_not_found" in text or (
        "previous_response_id" in text and "not found" in text
    )


def _extract_error_text(payload: Any) -> str:
    if isinstance(payload, dict):
        nested = payload.get("error")
        if isinstance(nested, dict):
            message = str(nested.get("message") or "").strip()
            if message:
                return message
        message = str(payload.get("message") or "").strip()
        if message:
            return message
    elif isinstance(payload, str):
        return payload.strip()
    return ""


def _openai_exception_message(exc: Exception) -> str:
    detail = _extract_error_text(getattr(exc, "body", None))
    if not detail:
        detail = str(exc).strip() or exc.__class__.__name__

    status_code = getattr(exc, "status_code", None)
    if status_code:
        return f"OpenAI request failed ({status_code}): {detail}"
    return f"OpenAI request failed: {detail}"


def _looks_like_mcp_setup_failure(exc: Exception) -> bool:
    text = " ".join(
        part
        for part in [
            str(exc).lower(),
            _extract_error_text(getattr(exc, "body", None)).lower(),
        ]
        if part
    )
    return any(
        token in text
        for token in [
            "mcp",
            "server_url",
            "remote mcp",
            "connector",
            "allowed_tools",
            "approval",
            "list tools",
            "list_tools",
            "tool type",
        ]
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None

    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    parsed = _safe_json_loads(raw)
    if isinstance(parsed, dict):
        return parsed

    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    parsed = _safe_json_loads(raw[start : end + 1])
    if isinstance(parsed, dict):
        return parsed
    return None


def _strip_markdown_fences(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _coerce_int(value: Any):
    try:
        if value in ("", None):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_authorities(items: Any) -> list[dict[str, Any]]:
    normalized = []
    if not isinstance(items, list):
        return normalized

    for item in items:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "kind": str(item.get("kind") or "").strip().lower() or "case",
                "title": str(item.get("title") or "").strip(),
                "citation": str(item.get("citation") or "").strip(),
                "document_id": _coerce_int(item.get("document_id")),
                "reference_id": _coerce_int(item.get("reference_id")),
                "precedential_status": str(item.get("precedential_status") or "").strip() or "n/a",
                "validity_status": str(item.get("validity_status") or "").strip() or "n/a",
                "relevance": str(item.get("relevance") or "").strip(),
                "suggested_use": str(item.get("suggested_use") or "").strip(),
                "pinpoint": str(item.get("pinpoint") or "").strip(),
            }
        )
    return normalized


_EDIT_OPERATIONS = {
    "replace_selection",
    "insert_before_selection",
    "insert_after_selection",
    "append_to_document",
    "delete_selection",
}

_EDIT_FALLBACK_SECTION_LABELS = {
    "edit summary": "edit_summary",
    "summary": "edit_summary",
    "why": "rationale",
    "rationale": "rationale",
    "target text": "target_text",
    "target": "target_text",
    "current text": "target_text",
    "proposed text": "proposed_text",
    "drafted result": "proposed_text",
    "replacement text": "proposed_text",
    "revised text": "proposed_text",
    "draft": "proposed_text",
    "notes": "notes",
}
_EDIT_FALLBACK_SECTION_RE = re.compile(
    r"(?im)^(edit summary|summary|why|rationale|target text|target|current text|proposed text|drafted result|replacement text|revised text|draft|notes)\s*:\s*"
)


def _structured_result_failure_message(mode: str) -> str:
    if mode == "edit":
        return "The agent did not return valid structured edit data."
    return "The agent did not return valid structured suggestion data."


def _normalize_edit_operation(value: Any, *, has_selected_text: bool, has_target_text: bool) -> str:
    operation = str(value or "").strip().lower()
    if operation not in _EDIT_OPERATIONS:
        operation = "replace_selection" if (has_selected_text or has_target_text) else "append_to_document"
    if has_selected_text and operation == "append_to_document":
        operation = "replace_selection"
    if not has_selected_text and not has_target_text and operation != "append_to_document":
        operation = "append_to_document"
    return operation


def _normalize_edit_result(payload: Any, *, request_payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    selected_text = str(request_payload.get("selected_text") or "").strip()
    has_selected_text = bool(selected_text)
    target_text = str(payload.get("target_text") or selected_text).strip()
    has_target_text = bool(target_text)
    operation = _normalize_edit_operation(
        payload.get("operation"),
        has_selected_text=has_selected_text,
        has_target_text=has_target_text,
    )
    return {
        "edit_summary": str(payload.get("edit_summary") or payload.get("summary") or "").strip(),
        "rationale": str(payload.get("rationale") or "").strip(),
        "operation": operation,
        "target_text": target_text,
        "proposed_text": str(
            payload.get("proposed_text")
            or payload.get("replacement_text")
            or payload.get("text")
            or ""
        ).strip(),
        "notes": str(payload.get("notes") or payload.get("search_notes") or "").strip(),
        "selected_text": selected_text,
        "selection_from": _coerce_int(request_payload.get("selection_from")),
        "selection_to": _coerce_int(request_payload.get("selection_to")),
    }


def _infer_edit_operation_from_request(*, request_payload: dict[str, Any], target_text: str) -> str:
    selected_text = str(request_payload.get("selected_text") or "").strip()
    if selected_text:
        return "replace_selection"

    instruction = " ".join(str(request_payload.get("instruction") or "").lower().split())
    if not target_text:
        return "append_to_document"
    if re.search(r"\b(delete|remove|strike)\b", instruction):
        return "delete_selection"
    if re.search(r"\bbefore\b", instruction):
        return "insert_before_selection"
    if re.search(r"\b(after|under|below|following)\b", instruction):
        return "insert_after_selection"
    if re.search(r"\b(add|insert|draft)\b", instruction):
        return "insert_after_selection"
    return "replace_selection"


def _fallback_edit_result_from_text(raw_answer: str, *, request_payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = _strip_markdown_fences(raw_answer)
    if not cleaned:
        return {}

    sections: dict[str, str] = {}
    matches = list(_EDIT_FALLBACK_SECTION_RE.finditer(cleaned))
    for index, match in enumerate(matches):
        label = _EDIT_FALLBACK_SECTION_LABELS.get(match.group(1).strip().lower())
        if not label:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        value = cleaned[start:end].strip()
        if value and label not in sections:
            sections[label] = value

    target_text = str(sections.get("target_text") or request_payload.get("selected_text") or "").strip()
    proposed_text = str(sections.get("proposed_text") or "").strip() or cleaned
    normalized = _normalize_edit_result(
        {
            "edit_summary": sections.get("edit_summary") or "",
            "rationale": sections.get("rationale") or "",
            "operation": _infer_edit_operation_from_request(request_payload=request_payload, target_text=target_text),
            "target_text": target_text,
            "proposed_text": proposed_text,
            "notes": sections.get("notes") or "",
        },
        request_payload=request_payload,
    )
    if normalized.get("operation") != "delete_selection" and not normalized.get("proposed_text"):
        return {}
    return normalized


class DocumentResearchAgent:
    def __init__(self, *, document, user):
        self.document = document
        self.user = user
        self.client = _new_openai_client()
        self.has_client_files = DocumentClientFile.objects.filter(
            document=document,
        ).exists()
        self.has_active_exemplars = Exemplar.objects.filter(
            created_by=user,
            is_active=True,
        ).exists()

    def start_chat_run(
        self,
        *,
        run: DocumentResearchRun,
        message: str,
        selected_text: str = "",
        previous_response_id: str = "",
        transcript_messages: list[Any] | None = None,
    ) -> DocumentResearchRun:
        normalized_message = (message or "").strip()
        if not normalized_message:
            raise AgentExecutionError("A chat message is required.")

        normalized_selection = (selected_text or "").strip()
        transcript_messages = transcript_messages or []
        run.mode = "chat"
        run.request_payload = {
            "message": normalized_message,
            "selected_text": normalized_selection,
        }
        run.previous_response_id = (previous_response_id or "").strip()
        run.metadata = self._initial_run_metadata(mode="chat", previous_response_id=run.previous_response_id)
        metadata = dict(run.metadata or {})
        metadata["requirements"] = _make_turn_requirements(
            mode="chat",
            request_text=normalized_message,
            selected_text=normalized_selection,
            has_client_files=self.has_client_files,
            has_active_exemplars=self.has_active_exemplars,
        )
        run.metadata = metadata

        used_mcp_fallback = False
        try:
            tools = self._build_tools(mode="chat", include_mcp=True)
            input_payload = self._chat_input(
                message=normalized_message,
                selected_text=normalized_selection,
                transcript_messages=transcript_messages,
            )
            try:
                response = self._create_background_response(
                    instructions=_CHAT_SYSTEM_PROMPT,
                    input_payload=input_payload,
                    tools=tools,
                    previous_response_id=run.previous_response_id or None,
                    tool_choice="auto",
                    max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
                    mode="chat",
                )
            except StaleResponseChainError:
                rebuilt_input = self._chat_input(
                    message=normalized_message,
                    selected_text=normalized_selection,
                    transcript_messages=transcript_messages,
                )
                metadata = dict(run.metadata or {})
                metadata["stale_previous_response_fallback"] = True
                metadata["used_previous_response_id"] = False
                run.metadata = metadata
                run.previous_response_id = ""
                response = self._create_background_response(
                    instructions=_CHAT_SYSTEM_PROMPT,
                    input_payload=rebuilt_input,
                    tools=tools,
                    previous_response_id=None,
                    tool_choice="auto",
                    max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
                    mode="chat",
                )
        except AgentConfigurationError as exc:
            if "BIAEDGE_MCP_SERVER_URL" not in str(exc):
                raise
            used_mcp_fallback = True
            logger.warning(
                "Document agent chat continuing without BIA Edge MCP because it is not configured."
            )
            response = self._create_background_response(
                instructions=self._chat_fallback_instructions(),
                input_payload=self._chat_input(
                    message=normalized_message,
                    selected_text=normalized_selection,
                    transcript_messages=transcript_messages,
                ),
                tools=self._build_tools(mode="chat", include_mcp=False),
                previous_response_id=None,
                tool_choice="auto",
                max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
                mode="chat",
            )
            run.previous_response_id = ""
        except AgentExecutionError as exc:
            if not self._has_mcp_tools(tools=locals().get("tools", [])) or not _looks_like_mcp_setup_failure(exc):
                raise
            used_mcp_fallback = True
            logger.warning(
                "Document agent chat retrying without BIA Edge MCP after setup failure: %s",
                exc,
            )
            response = self._create_background_response(
                instructions=self._chat_fallback_instructions(),
                input_payload=self._chat_input(
                    message=normalized_message,
                    selected_text=normalized_selection,
                    transcript_messages=transcript_messages,
                ),
                tools=self._build_tools(mode="chat", include_mcp=False),
                previous_response_id=None,
                tool_choice="auto",
                max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
                mode="chat",
            )
            run.previous_response_id = ""

        metadata = dict(run.metadata or {})
        metadata["mcp_fallback"] = used_mcp_fallback
        run.metadata = metadata
        return self._attach_started_response(
            run=run,
            response=response,
            stage="waiting_openai",
            phase=_RUN_PHASE_RESEARCH,
        )

    def start_suggest_run(
        self,
        *,
        run: DocumentResearchRun,
        selected_text: str,
        focus_note: str = "",
    ) -> DocumentResearchRun:
        normalized_selected = (selected_text or "").strip()
        if not normalized_selected:
            raise AgentExecutionError("Selected text is required for case-law suggestions.")

        run.mode = "suggest"
        run.request_payload = {
            "selected_text": normalized_selected,
            "focus_note": (focus_note or "").strip(),
        }
        run.previous_response_id = ""
        run.metadata = self._initial_run_metadata(mode="suggest", previous_response_id="")
        metadata = dict(run.metadata or {})
        metadata["requirements"] = _make_turn_requirements(
            mode="suggest",
            request_text=(focus_note or "").strip(),
            selected_text=normalized_selected,
            has_client_files=self.has_client_files,
            has_active_exemplars=self.has_active_exemplars,
        )
        run.metadata = metadata

        response = self._create_background_response(
            instructions=_SUGGEST_SYSTEM_PROMPT,
            input_payload=self._suggest_input(
                selected_text=normalized_selected,
                focus_note=(focus_note or "").strip(),
            ),
            tools=self._build_tools(mode="suggest", include_mcp=True),
            previous_response_id=None,
            tool_choice="required",
            max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
            mode="suggest",
        )
        return self._attach_started_response(
            run=run,
            response=response,
            stage="waiting_openai",
            phase=_RUN_PHASE_RESEARCH,
        )

    def start_edit_run(
        self,
        *,
        run: DocumentResearchRun,
        instruction: str,
        selected_text: str = "",
        selection_from: int | None = None,
        selection_to: int | None = None,
    ) -> DocumentResearchRun:
        normalized_instruction = (instruction or "").strip()
        if not normalized_instruction:
            raise AgentExecutionError("An edit instruction is required.")

        normalized_selected = (selected_text or "").strip()
        run.mode = "edit"
        run.request_payload = {
            "instruction": normalized_instruction,
            "selected_text": normalized_selected,
            "selection_from": selection_from,
            "selection_to": selection_to,
        }
        run.previous_response_id = ""
        run.metadata = self._initial_run_metadata(mode="edit", previous_response_id="")
        metadata = dict(run.metadata or {})
        metadata["requirements"] = _make_turn_requirements(
            mode="edit",
            request_text=normalized_instruction,
            selected_text=normalized_selected,
            has_client_files=self.has_client_files,
            has_active_exemplars=self.has_active_exemplars,
        )
        run.metadata = metadata

        response = self._create_background_response(
            instructions=_EDIT_SYSTEM_PROMPT,
            input_payload=self._edit_input(
                instruction=normalized_instruction,
                selected_text=normalized_selected,
            ),
            tools=self._build_tools(mode="edit", include_mcp=True),
            previous_response_id=None,
            tool_choice="auto",
            max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
            mode="edit",
        )
        return self._attach_started_response(
            run=run,
            response=response,
            stage="waiting_openai",
            phase=_RUN_PHASE_RESEARCH,
        )

    def advance_run(self, *, run: DocumentResearchRun) -> DocumentResearchRun:
        if run.status in _TERMINAL_RUN_STATUSES:
            return run
        if not run.response_id:
            return self._mark_run_failed(run, "The agent run is missing its OpenAI response ID.")

        budget_error = self._budget_error(run)
        if budget_error:
            recovered = self._recover_budget_overrun(run=run, reason=budget_error)
            if recovered:
                return recovered
            self.cancel_run(run=run, reason=budget_error, final_status="failed")
            return run

        try:
            response = self.client.responses.retrieve(
                run.response_id,
                include=list(_TOOL_INCLUDE_FIELDS),
            )
        except Exception as exc:
            logger.exception(
                "Document research agent response retrieval failed",
                extra={
                    "document_id": str(self.document.id),
                    "user_id": getattr(self.user, "id", None),
                    "run_id": str(run.public_id),
                    "response_id": run.response_id,
                },
            )
            return self._mark_run_failed(run, _openai_exception_message(exc))

        self._record_response_artifacts(run, response)
        budget_error = self._budget_error(run)
        if budget_error:
            recovered = self._recover_budget_overrun(run=run, reason=budget_error)
            if recovered:
                return recovered
            self.cancel_run(run=run, reason=budget_error, final_status="failed")
            return run

        status = _response_status(response)
        if status in {"queued", "in_progress"}:
            return self._update_run_state(
                run,
                status="queued" if status == "queued" else "in_progress",
                stage="waiting_openai",
            )
        if status == "cancelled":
            return self._mark_run_cancelled(run, _response_error_message(response) or "The agent run was cancelled.")
        if status == "failed":
            recovered = self._recover_failed_response(run=run, response=response)
            if recovered:
                return recovered
            return self._mark_run_failed(run, self._failed_status_message(run=run, response=response))
        if status == "incomplete":
            return self._continue_incomplete_response(run=run, response=response)
        if status != "completed":
            return self._mark_run_failed(
                run,
                _response_error_message(response) or f"OpenAI response returned status={status or 'unknown'}.",
            )

        function_calls = _pending_function_calls(response)
        if function_calls:
            return self._continue_after_function_calls(run=run, response=response, function_calls=function_calls)

        answer = self._assembled_answer(
            run=run,
            response=response,
            answer=_extract_output_text(response),
        )
        if not answer:
            return self._queue_force_final_response(run=run, response=response)

        if run.mode in {"chat", "edit"}:
            missing_sources = self._missing_full_text_sources_for_run(run=run)
            if missing_sources and not bool((run.metadata or {}).get("quote_source_verification_attempted")):
                return self._queue_quote_source_verification(
                    run=run,
                    response=response,
                    missing_sources=missing_sources,
                )

        if run.mode == "suggest":
            return self._finalize_suggest_run(run=run, response=response, answer=answer)
        if run.mode == "edit":
            return self._finalize_edit_run(run=run, response=response, answer=answer)
        return self._finalize_chat_run(run=run, response=response, answer=answer)

    def cancel_run(
        self,
        *,
        run: DocumentResearchRun,
        reason: str = "",
        final_status: str = "cancelled",
    ) -> DocumentResearchRun:
        response_id = (run.response_id or "").strip()
        if response_id and run.status in _ACTIVE_RUN_STATUSES:
            try:
                self.client.responses.cancel(response_id)
            except Exception:
                logger.warning(
                    "Unable to cancel OpenAI response",
                    extra={
                        "document_id": str(self.document.id),
                        "user_id": getattr(self.user, "id", None),
                        "run_id": str(run.public_id),
                        "response_id": response_id,
                    },
                )

        if final_status == "failed":
            return self._mark_run_failed(run, reason or "The agent run was cancelled after exceeding its budget.")
        return self._mark_run_cancelled(run, reason or "The agent run was cancelled.")

    def chat(
        self,
        *,
        message: str,
        selected_text: str = "",
        previous_response_id: str = "",
        transcript_messages: list[Any] | None = None,
    ) -> ChatAgentResult:
        normalized_message = (message or "").strip()
        if not normalized_message:
            raise AgentExecutionError("A chat message is required.")

        used_mcp_fallback = False
        try:
            tools = self._build_tools(mode="chat", include_mcp=True)
            input_text = self._chat_input(
                message=normalized_message,
                selected_text=selected_text,
                transcript_messages=[],
            )

            try:
                result = self._run_response_loop(
                    instructions=_CHAT_SYSTEM_PROMPT,
                    input_payload=input_text,
                    tools=tools,
                    previous_response_id=(previous_response_id or "").strip() or None,
                    initial_tool_choice="auto",
                )
            except StaleResponseChainError:
                rebuilt_input = self._chat_input(
                    message=normalized_message,
                    selected_text=selected_text,
                    transcript_messages=transcript_messages or [],
                )
                result = self._run_response_loop(
                    instructions=_CHAT_SYSTEM_PROMPT,
                    input_payload=rebuilt_input,
                    tools=tools,
                    previous_response_id=None,
                    initial_tool_choice="auto",
                )
        except AgentConfigurationError as exc:
            if "BIAEDGE_MCP_SERVER_URL" not in str(exc):
                raise
            used_mcp_fallback = True
            logger.warning("Document agent chat continuing without BIA Edge MCP because it is not configured.")
            result = self._run_response_loop(
                instructions=self._chat_fallback_instructions(),
                input_payload=self._chat_input(
                    message=normalized_message,
                    selected_text=selected_text,
                    transcript_messages=transcript_messages or [],
                ),
                tools=self._build_tools(mode="chat", include_mcp=False),
                previous_response_id=None,
                initial_tool_choice="auto",
            )
        except AgentExecutionError as exc:
            if not self._has_mcp_tools(tools=locals().get("tools", [])) or not _looks_like_mcp_setup_failure(exc):
                raise
            used_mcp_fallback = True
            logger.warning("Document agent chat retrying without BIA Edge MCP after setup failure: %s", exc)
            result = self._run_response_loop(
                instructions=self._chat_fallback_instructions(),
                input_payload=self._chat_input(
                    message=normalized_message,
                    selected_text=selected_text,
                    transcript_messages=transcript_messages or [],
                ),
                tools=self._build_tools(mode="chat", include_mcp=False),
                previous_response_id=None,
                initial_tool_choice="auto",
            )

        return ChatAgentResult(
            answer=result["answer"],
            response_id=result["response_id"],
            tool_calls=result["tool_calls"],
            citations=result["citations"],
            used_tools=_used_tools(result["tool_calls"]),
            metadata={
                "model": AGENT_MODEL,
                "reasoning_effort": AGENT_REASONING_EFFORT,
                "mcp_fallback": used_mcp_fallback,
            },
        )

    def suggest(
        self,
        *,
        selected_text: str,
        focus_note: str = "",
    ) -> SuggestAgentResult:
        normalized_selected = (selected_text or "").strip()
        if not normalized_selected:
            raise AgentExecutionError("Selected text is required for case-law suggestions.")

        tools = self._build_tools(mode="suggest")
        result = self._run_response_loop(
            instructions=_SUGGEST_SYSTEM_PROMPT,
            input_payload=self._suggest_input(selected_text=normalized_selected, focus_note=focus_note),
            tools=tools,
            previous_response_id=None,
            initial_tool_choice="required",
        )

        if not any(call.get("source") == "biaedge" for call in result["tool_calls"]):
            raise AgentExecutionError("The suggestion run completed without using BIA Edge tools.")

        parsed = _extract_json_object(result["answer"])
        if parsed is None:
            parsed = self._repair_suggest_json(previous_response_id=result["response_id"])

        if parsed is None:
            raise AgentExecutionError("The agent did not return valid structured suggestion data.")

        return SuggestAgentResult(
            selection_summary=str(parsed.get("selection_summary") or "").strip(),
            draft_gap=str(parsed.get("draft_gap") or "").strip(),
            authorities=_normalize_authorities(parsed.get("authorities")),
            search_notes=str(parsed.get("search_notes") or "").strip(),
            next_questions=[
                str(item).strip()
                for item in (parsed.get("next_questions") or [])
                if str(item).strip()
            ],
            response_id=result["response_id"],
            tool_calls=result["tool_calls"],
            citations=result["citations"],
            raw_answer=result["answer"],
        )

    def _build_tools(self, *, mode: str, include_mcp: bool = True) -> list[dict[str, Any]]:
        allowed_tools = _BIAEDGE_SUGGEST_TOOLS if mode == "suggest" else _BIAEDGE_CHAT_TOOLS
        policy = _MODE_TOOL_POLICY.get(mode, _MODE_TOOL_POLICY["chat"])
        tools: list[dict[str, Any]] = []
        if policy.get("include_web_search", True):
            tools.append(
                _build_web_search_tool(
                    search_context_size=str(policy.get("web_search_context_size") or "medium")
                )
            )
        if self.has_client_files and policy.get("include_client_docs", True):
            tools.extend(_client_file_function_tools())
        if self.has_active_exemplars and policy.get("include_knowledge", True):
            tools.extend(_knowledge_function_tools())
        if include_mcp:
            tools.insert(0, _build_biaedge_mcp_tool(allowed_tools=allowed_tools))
        file_search = _build_file_search_tool() if policy.get("include_file_search", True) else None
        if file_search:
            tools.append(file_search)
        return tools

    def _initial_run_metadata(self, *, mode: str, previous_response_id: str) -> dict[str, Any]:
        return {
            "model": AGENT_MODEL,
            "reasoning_effort": AGENT_REASONING_EFFORT,
            "mcp_fallback": False,
            "phase": _RUN_PHASE_INTAKE,
            "used_previous_response_id": bool(previous_response_id),
            "forced_final_attempted": False,
            "json_repair_attempted": False,
            "quote_source_verification_attempted": False,
            "continuation_attempts": 0,
            "answer_fragments": [],
            "usage_by_response_id": {},
            "phase_history": [],
            "finalization_source": "",
            "requirements": {},
            "evidence_pack": _build_evidence_pack([]),
            "metrics": {},
            "prompt_cache_key": self._prompt_cache_key(mode),
        }

    def _prompt_cache_key(self, mode: str) -> str:
        return f"document-agent:{mode}:{self.document.id}"

    def _request_text(self, *, request_payload: dict[str, Any]) -> str:
        parts = [
            str(request_payload.get("message") or "").strip(),
            str(request_payload.get("instruction") or "").strip(),
            str(request_payload.get("focus_note") or "").strip(),
        ]
        return "\n".join(part for part in parts if part)

    def _requirements_for_run(self, *, run: DocumentResearchRun) -> dict[str, Any]:
        metadata = dict(run.metadata or {})
        existing = metadata.get("requirements")
        if isinstance(existing, dict) and existing:
            return existing
        request_payload = run.request_payload or {}
        requirements = _make_turn_requirements(
            mode=run.mode,
            request_text=self._request_text(request_payload=request_payload),
            selected_text=str(request_payload.get("selected_text") or ""),
            has_client_files=self.has_client_files,
            has_active_exemplars=self.has_active_exemplars,
        )
        metadata["requirements"] = requirements
        run.metadata = metadata
        return requirements

    def _request_requirements_block_for_run(self, *, run: DocumentResearchRun) -> str:
        return _turn_requirements_block(self._requirements_for_run(run=run))

    def _requested_full_text_sources_for_run(self, *, run: DocumentResearchRun) -> set[str]:
        requirements = self._requirements_for_run(run=run)
        return {
            str(item).strip()
            for item in (requirements.get("required_full_text_sources") or [])
            if str(item).strip()
        }

    def _refresh_run_evidence_pack(self, *, run: DocumentResearchRun) -> None:
        metadata = dict(run.metadata or {})
        evidence_pack = _build_evidence_pack(run.tool_calls or [])
        metadata["evidence_pack"] = evidence_pack
        metadata["metrics"] = {
            "phase": str(metadata.get("phase") or _stage_phase(run.stage)),
            "response_count": int(run.response_count or 0),
            "local_function_rounds": int(run.local_function_rounds or 0),
            "finalization_source": str(metadata.get("finalization_source") or "").strip(),
            "evidence_counts": evidence_pack.get("counts") or {},
            **_tool_usage_metrics(run.tool_calls or []),
        }
        run.metadata = metadata

    def _set_run_phase(self, *, run: DocumentResearchRun, phase: str, stage: str | None = None) -> None:
        metadata = dict(run.metadata or {})
        history = [
            item for item in (metadata.get("phase_history") or [])
            if isinstance(item, dict)
        ]
        metadata["phase"] = phase
        if stage is not None:
            run.stage = stage
        current_stage = run.stage or stage or ""
        if not history or history[-1].get("phase") != phase or history[-1].get("stage") != current_stage:
            history.append(
                {
                    "phase": phase,
                    "stage": current_stage,
                    "at": timezone.now().isoformat(),
                }
            )
        metadata["phase_history"] = history[-12:]
        run.metadata = metadata
        self._refresh_run_evidence_pack(run=run)

    def _missing_full_text_sources_for_run(self, *, run: DocumentResearchRun) -> list[str]:
        requested_sources = self._requested_full_text_sources_for_run(run=run)
        if not requested_sources:
            return []

        tool_names = {
            str(item.get("name") or "").strip()
            for item in (run.tool_calls or [])
            if isinstance(item, dict)
        }
        missing: list[str] = []
        if "policy" in requested_sources and "get_reference" not in tool_names:
            missing.append("policy")
        if "statute" in requested_sources and "get_statute" not in tool_names:
            missing.append("statute")
        if "case" in requested_sources and "get_document_text" not in tool_names:
            missing.append("case")
        return missing

    def _append_answer_fragment(self, *, run: DocumentResearchRun, response: Any) -> None:
        fragment = _extract_output_text(response)
        if not fragment:
            return

        response_id = (getattr(response, "id", "") or "").strip()
        metadata = dict(run.metadata or {})
        fragments = [
            item for item in (metadata.get("answer_fragments") or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        if response_id and any(str(item.get("response_id") or "") == response_id for item in fragments):
            return

        fragments.append(
            {
                "response_id": response_id,
                "text": fragment,
            }
        )
        metadata["answer_fragments"] = fragments[-6:]
        run.metadata = metadata

    def _assembled_answer(self, *, run: DocumentResearchRun, response: Any, answer: str) -> str:
        fragments = [
            str(item.get("text") or "")
            for item in (dict(run.metadata or {}).get("answer_fragments") or [])
            if isinstance(item, dict)
        ]
        if answer:
            fragments.append(answer)
        return _join_text_fragments(fragments)

    def _queue_quote_source_verification(self, *, run: DocumentResearchRun, response: Any, missing_sources: list[str]) -> DocumentResearchRun:
        metadata = dict(run.metadata or {})
        if metadata.get("quote_source_verification_attempted"):
            answer = self._assembled_answer(run=run, response=response, answer=_extract_output_text(response))
            if run.mode == "edit":
                return self._finalize_edit_run(run=run, response=response, answer=answer)
            return self._finalize_chat_run(run=run, response=response, answer=answer)

        metadata["quote_source_verification_attempted"] = True
        run.metadata = metadata
        labels = [_FULL_TEXT_SOURCE_LABELS.get(source, source) for source in missing_sources]
        request_requirements = self._request_requirements_block_for_run(run=run)
        input_lines = []
        if request_requirements:
            input_lines.append(request_requirements)
        input_lines.extend(
            [
                "Verification step:",
                f"- missing_full_text_sources: {', '.join(labels)}",
                "- action: retrieve the missing full-text authorities before answering",
                "- fallback_rule: if the correct full-text tool still does not produce the text, say so plainly",
            ]
        )
        try:
            follow_up = self._create_background_response(
                instructions=self._run_instructions(run),
                input_payload="\n".join(input_lines),
                tools=self._build_tools(mode=run.mode, include_mcp=self._run_include_mcp(run)),
                previous_response_id=(getattr(response, "id", "") or "").strip() or None,
                tool_choice="auto",
                max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
                mode=run.mode,
            )
        except AgentExecutionError as exc:
            return self._mark_run_failed(run, str(exc))

        run.previous_response_id = (getattr(response, "id", "") or "").strip()
        return self._attach_started_response(
            run=run,
            response=follow_up,
            stage="verifying_quotes",
            phase=_RUN_PHASE_VERIFY,
        )

    def _run_instructions(self, run: DocumentResearchRun) -> str:
        if run.mode == "suggest":
            return _SUGGEST_SYSTEM_PROMPT
        if run.mode == "edit":
            return _EDIT_SYSTEM_PROMPT
        if self._run_include_mcp(run):
            return _CHAT_SYSTEM_PROMPT
        return self._chat_fallback_instructions()

    def _run_include_mcp(self, run: DocumentResearchRun) -> bool:
        if run.mode == "suggest":
            return True
        metadata = run.metadata or {}
        return not bool(metadata.get("mcp_fallback"))

    def _response_metadata(self) -> dict[str, str]:
        return {
            "document_id": str(self.document.id),
            "user_id": str(getattr(self.user, "id", "")),
        }

    def _create_background_response(
        self,
        *,
        instructions: str,
        input_payload: Any,
        tools: list[dict[str, Any]],
        previous_response_id: str | None,
        tool_choice: str,
        max_output_tokens: int,
        mode: str,
        reasoning_effort: str | None = None,
    ):
        request = {
            "model": AGENT_MODEL,
            "instructions": instructions,
            "input": input_payload,
            "tools": tools,
            "tool_choice": tool_choice,
            "parallel_tool_calls": True,
            "max_tool_calls": AGENT_MAX_TOOL_CALLS,
            "max_output_tokens": max_output_tokens,
            "reasoning": {"effort": _normalize_reasoning_effort(reasoning_effort or AGENT_REASONING_EFFORT)},
            "store": True,
            "background": True,
            "include": list(_TOOL_INCLUDE_FIELDS),
            "truncation": "auto",
            "metadata": self._response_metadata(),
            "prompt_cache_key": self._prompt_cache_key(mode),
            "prompt_cache_retention": "24h",
            "safety_identifier": f"user-{getattr(self.user, 'id', 'unknown')}",
        }
        if previous_response_id:
            request["previous_response_id"] = previous_response_id

        try:
            return self.client.responses.create(**request)
        except Exception as exc:
            if previous_response_id and _stale_previous_response(exc):
                raise StaleResponseChainError(str(exc)) from exc
            logger.exception(
                "Document research agent background response creation failed",
                extra={
                    "document_id": str(self.document.id),
                    "user_id": getattr(self.user, "id", None),
                    "model": AGENT_MODEL,
                    "tool_choice": tool_choice,
                    "has_previous_response_id": bool(previous_response_id),
                    "tool_types": [tool.get("type") for tool in tools if isinstance(tool, dict)],
                    "mode": mode,
                },
            )
            raise AgentExecutionError(_openai_exception_message(exc)) from exc

    def _attach_started_response(
        self,
        *,
        run: DocumentResearchRun,
        response: Any,
        stage: str,
        phase: str | None = None,
    ) -> DocumentResearchRun:
        run.response_id = getattr(response, "id", "") or ""
        run.response_count = int(run.response_count or 0) + 1
        status = _response_status(response)
        if status == "queued":
            run.status = "queued"
        elif status in {"failed", "cancelled"}:
            run.status = "failed" if status == "failed" else "cancelled"
            run.error_message = _response_error_message(response)
            run.completed_at = timezone.now()
        else:
            run.status = "in_progress"
        self._record_response_artifacts(run, response)
        self._set_run_phase(run=run, phase=phase or _stage_phase(stage), stage=stage)
        run.save(
            update_fields=[
                "status",
                "stage",
                "response_id",
                "response_count",
                "request_payload",
                "previous_response_id",
                "local_function_rounds",
                "tool_calls",
                "citations",
                "usage",
                "metadata",
                "error_message",
                "completed_at",
                "updated_at",
            ]
        )
        return run

    def _record_response_artifacts(self, run: DocumentResearchRun, response: Any) -> None:
        metadata = dict(run.metadata or {})
        usage_by_response_id = metadata.get("usage_by_response_id") or {}
        response_id = (getattr(response, "id", "") or "").strip()
        if response_id and response_id not in usage_by_response_id:
            usage_dict = _usage_to_dict(getattr(response, "usage", None))
            if usage_dict:
                usage_by_response_id[response_id] = usage_dict
        metadata["usage_by_response_id"] = usage_by_response_id
        run.metadata = metadata
        run.usage = _sum_usage_by_response(usage_by_response_id)
        run.tool_calls = _merge_unique_records(run.tool_calls or [], _extract_hosted_tool_calls(response))
        run.citations = _merge_unique_records(run.citations or [], _extract_citations(response))
        self._refresh_run_evidence_pack(run=run)

    def _update_run_state(self, run: DocumentResearchRun, *, status: str, stage: str) -> DocumentResearchRun:
        run.status = status
        self._set_run_phase(run=run, phase=_stage_phase(stage), stage=stage)
        run.save(update_fields=["status", "stage", "tool_calls", "citations", "usage", "metadata", "updated_at"])
        return run

    def _mark_run_failed(self, run: DocumentResearchRun, message: str) -> DocumentResearchRun:
        run.status = "failed"
        self._set_run_phase(run=run, phase=_RUN_PHASE_FAILED, stage="failed")
        run.error_message = (message or "The agent run failed.").strip()
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "status",
                "stage",
                "error_message",
                "completed_at",
                "tool_calls",
                "citations",
                "usage",
                "metadata",
                "updated_at",
            ]
        )
        return run

    def _mark_run_cancelled(self, run: DocumentResearchRun, message: str) -> DocumentResearchRun:
        run.status = "cancelled"
        self._set_run_phase(run=run, phase=_RUN_PHASE_CANCELLED, stage="cancelled")
        run.error_message = (message or "The agent run was cancelled.").strip()
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "status",
                "stage",
                "error_message",
                "completed_at",
                "tool_calls",
                "citations",
                "usage",
                "metadata",
                "updated_at",
            ]
        )
        return run

    def _mark_run_completed(self, run: DocumentResearchRun, *, result_payload: dict[str, Any], response: Any) -> DocumentResearchRun:
        run.status = "completed"
        self._set_run_phase(run=run, phase=_RUN_PHASE_COMPLETED, stage="completed")
        run.error_message = ""
        run.completed_at = timezone.now()
        run.response_id = (getattr(response, "id", "") or run.response_id or "").strip()
        run.result_payload = result_payload
        run.save(
            update_fields=[
                "status",
                "stage",
                "error_message",
                "completed_at",
                "response_id",
                "result_payload",
                "tool_calls",
                "citations",
                "usage",
                "metadata",
                "updated_at",
            ]
        )
        return run

    def _failed_status_message(self, *, run: DocumentResearchRun, response: Any) -> str:
        message = _response_error_message(response)
        if not _looks_like_generic_failed_status(message):
            return message

        tool_call_count = len(run.tool_calls or [])
        if tool_call_count >= AGENT_MAX_TOOL_CALLS:
            return (
                "The agent exhausted its OpenAI tool-call budget before it could finish the answer. "
                "Try again now that the tool budget has been increased."
            )
        if tool_call_count:
            return (
                "The agent gathered research but failed before it produced the final answer. "
                "Retrying should now be more reliable."
            )
        return message

    def _budget_error(self, run: DocumentResearchRun) -> str:
        if bool((run.metadata or {}).get("allow_over_budget_finalization")) and run.stage in {
            "forcing_final",
            "recovering_failure",
            "repairing_json",
        }:
            return ""
        elapsed_seconds = max(0, int((timezone.now() - run.created_at).total_seconds()))
        if elapsed_seconds > AGENT_MAX_RUN_SECONDS:
            return f"The agent run exceeded the {AGENT_MAX_RUN_SECONDS}-second budget."
        if int(run.local_function_rounds or 0) > AGENT_MAX_LOCAL_FUNCTION_ROUNDS:
            if bool((run.metadata or {}).get("allow_over_budget_finalization")):
                return ""
            return (
                "The agent run exceeded the local tool continuation budget of "
                f"{AGENT_MAX_LOCAL_FUNCTION_ROUNDS} rounds."
            )
        usage = run.usage or {}
        if int(usage.get("total_tokens") or 0) > AGENT_MAX_TOTAL_TOKENS:
            return f"The agent run exceeded the total token budget of {AGENT_MAX_TOTAL_TOKENS}."
        return ""

    def _queue_compact_finalization(
        self,
        *,
        run: DocumentResearchRun,
        response: Any,
        stage: str,
        note: str,
        allow_over_budget: bool = False,
        finalization_source: str = "",
    ) -> DocumentResearchRun:
        metadata = dict(run.metadata or {})
        if allow_over_budget:
            metadata["allow_over_budget_finalization"] = True
        if finalization_source:
            metadata["finalization_source"] = finalization_source
        run.metadata = metadata
        self._refresh_run_evidence_pack(run=run)
        compact_input = self._finalization_input_from_run(run=run)
        try:
            follow_up = self._create_background_response(
                instructions=self._run_instructions(run) + "\n\nFinalization note:\n" + note.strip(),
                input_payload=compact_input,
                tools=[],
                previous_response_id=None,
                tool_choice="none",
                max_output_tokens=AGENT_FINALIZATION_MAX_OUTPUT_TOKENS,
                mode=run.mode,
                reasoning_effort=AGENT_FINALIZATION_REASONING_EFFORT,
            )
        except AgentExecutionError as exc:
            return self._mark_run_failed(run, str(exc))
        run.previous_response_id = (getattr(response, "id", "") or "").strip()
        return self._attach_started_response(
            run=run,
            response=follow_up,
            stage=stage,
            phase=_RUN_PHASE_FINALIZE,
        )

    def _recover_budget_overrun(
        self,
        *,
        run: DocumentResearchRun,
        reason: str,
    ) -> DocumentResearchRun | None:
        normalized_reason = (reason or "").strip().lower()
        if not normalized_reason:
            return None
        if "second budget" in normalized_reason:
            return None
        metadata = dict(run.metadata or {})
        if metadata.get("budget_recovery_attempted"):
            return None
        if not (run.tool_calls or []):
            return None

        metadata["budget_recovery_attempted"] = True
        metadata["allow_over_budget_finalization"] = True
        metadata["budget_recovery_reason"] = reason
        run.metadata = metadata
        return self._queue_compact_finalization(
            run=run,
            response=type("BudgetResponse", (), {"id": run.response_id})(),
            stage="forcing_final",
            note=(
                "The run has reached its budget threshold. "
                "Using only the verified research already gathered, provide the final answer now. "
                "Do not call any more tools."
            ),
            allow_over_budget=True,
            finalization_source="budget_recovery",
        )

    def _recover_failed_response(self, *, run: DocumentResearchRun, response: Any) -> DocumentResearchRun | None:
        metadata = dict(run.metadata or {})
        if metadata.get("failed_recovery_attempted"):
            return None
        if not (run.tool_calls or []):
            return None

        metadata["failed_recovery_attempted"] = True
        run.metadata = metadata
        return self._queue_compact_finalization(
            run=run,
            response=response,
            stage="recovering_failure",
            note=(
                "Your previous response ended after gathering research. "
                "Using only the verified research already gathered, provide the final answer now. "
                "Do not call any more tools."
            ),
            finalization_source="failed_recovery",
        )

    def _continue_incomplete_response(self, *, run: DocumentResearchRun, response: Any) -> DocumentResearchRun:
        attempts = int((run.metadata or {}).get("continuation_attempts") or 0)
        has_answer_text = bool(_extract_output_text(response))
        if not has_answer_text:
            if run.tool_calls or attempts >= 1:
                return self._queue_force_final_response(run=run, response=response)
        else:
            self._append_answer_fragment(run=run, response=response)
        if attempts >= 2:
            return self._mark_run_failed(
                run,
                _response_error_message(response) or "The agent response remained incomplete after continuation attempts.",
            )

        metadata = dict(run.metadata or {})
        metadata["continuation_attempts"] = attempts + 1
        run.metadata = metadata
        try:
            follow_up = self._create_background_response(
                instructions=self._run_instructions(run),
                input_payload="Continue exactly where you left off and finish the response. Do not restart the answer.",
                tools=self._build_tools(mode=run.mode, include_mcp=self._run_include_mcp(run)),
                previous_response_id=(getattr(response, "id", "") or "").strip() or None,
                tool_choice="auto",
                max_output_tokens=_CONTINUE_RESPONSE_MAX_OUTPUT_TOKENS,
                mode=run.mode,
            )
        except AgentExecutionError as exc:
            return self._mark_run_failed(run, str(exc))
        run.previous_response_id = (getattr(response, "id", "") or "").strip()
        return self._attach_started_response(
            run=run,
            response=follow_up,
            stage="continuing",
            phase=_RUN_PHASE_FINALIZE,
        )

    def _continue_after_function_calls(
        self,
        *,
        run: DocumentResearchRun,
        response: Any,
        function_calls: list[Any],
    ) -> DocumentResearchRun:
        outputs = []
        local_tool_calls: list[dict[str, Any]] = []

        try:
            for call in function_calls:
                raw_arguments = getattr(call, "arguments", "") or ""
                parsed_arguments = _safe_json_loads(raw_arguments, default={}) or {}
                result = self._call_local_tool(
                    name=getattr(call, "name", "") or "",
                    arguments=parsed_arguments,
                )
                tool_name = getattr(call, "name", "") or ""
                tool_source = "knowledge"
                if tool_name in {"search_client_documents", "get_client_document"}:
                    tool_source = "client_docs"
                record = {
                    "source": tool_source,
                    "type": "function_call",
                    "name": tool_name,
                    "status": "completed",
                    "arguments": parsed_arguments,
                }
                output_excerpt = _compact_tool_output(result)
                if output_excerpt:
                    record["output_excerpt"] = output_excerpt
                local_tool_calls.append(
                    record
                )
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": getattr(call, "call_id", "") or "",
                        "output": json.dumps(result),
                    }
                )
        except Exception as exc:
            logger.exception(
                "Document research agent local tool execution failed",
                extra={
                    "document_id": str(self.document.id),
                    "user_id": getattr(self.user, "id", None),
                    "run_id": str(run.public_id),
                },
            )
            return self._mark_run_failed(run, f"Local tool execution failed: {exc}")

        run.local_function_rounds = int(run.local_function_rounds or 0) + 1
        run.tool_calls = _merge_unique_records(run.tool_calls or [], local_tool_calls)
        self._refresh_run_evidence_pack(run=run)
        budget_error = self._budget_error(run)
        if budget_error:
            if "local tool continuation budget" in budget_error:
                return self._queue_compact_finalization(
                    run=run,
                    response=response,
                    stage="forcing_final",
                    note=(
                        "You now have the local tool results needed for this turn. "
                        "Provide the final answer now using only the verified research already gathered. "
                        "Do not call any more tools."
                    ),
                    allow_over_budget=True,
                    finalization_source="local_tool_budget",
                )
            return self._mark_run_failed(run, budget_error)

        try:
            follow_up = self._create_background_response(
                instructions=self._run_instructions(run),
                input_payload=outputs,
                tools=self._build_tools(mode=run.mode, include_mcp=self._run_include_mcp(run)),
                previous_response_id=(getattr(response, "id", "") or "").strip() or None,
                tool_choice="auto",
                max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
                mode=run.mode,
            )
        except AgentExecutionError as exc:
            return self._mark_run_failed(run, str(exc))
        run.previous_response_id = (getattr(response, "id", "") or "").strip()
        return self._attach_started_response(
            run=run,
            response=follow_up,
            stage="running_tools",
            phase=_RUN_PHASE_RESEARCH,
        )

    def _queue_force_final_response(self, *, run: DocumentResearchRun, response: Any) -> DocumentResearchRun:
        metadata = dict(run.metadata or {})
        if metadata.get("forced_final_attempted"):
            return self._mark_run_failed(run, "The agent returned an empty response.")

        metadata["forced_final_attempted"] = True
        run.metadata = metadata
        return self._queue_compact_finalization(
            run=run,
            response=response,
            stage="forcing_final",
            note=(
                "You have already received the relevant tool outputs for this turn. "
                "Provide the final answer now using only the verified research already gathered. "
                "Do not call any more tools."
            ),
            finalization_source="empty_response",
        )

    def _queue_json_repair(self, *, run: DocumentResearchRun, response: Any) -> DocumentResearchRun:
        metadata = dict(run.metadata or {})
        if metadata.get("json_repair_attempted"):
            if run.mode == "edit":
                fallback = _fallback_edit_result_from_text(
                    _extract_output_text(response),
                    request_payload=run.request_payload or {},
                )
                if fallback:
                    return self._mark_run_completed(
                        run,
                        result_payload=self._build_edit_result_payload(
                            run=run,
                            response=response,
                            normalized=fallback,
                            answer=_extract_output_text(response),
                        ),
                        response=response,
                    )
            return self._mark_run_failed(run, _structured_result_failure_message(run.mode))

        metadata["json_repair_attempted"] = True
        metadata["finalization_source"] = "json_repair"
        run.metadata = metadata
        self._refresh_run_evidence_pack(run=run)
        if run.mode == "edit":
            repair_instructions = (
                "You are repairing a structured edit proposal. "
                "Return valid JSON only. Do not perform more research. "
                "Use this exact shape: "
                '{"edit_summary":"...","rationale":"...","operation":"replace_selection | insert_before_selection | insert_after_selection | append_to_document | delete_selection","target_text":"...","proposed_text":"...","notes":"..."}'
            )
            repair_input = (
                "Reformat your previous answer as valid JSON only.\n\n"
                "If the previous answer already contains drafted language, put that language in proposed_text.\n"
                "If the request targeted an existing paragraph or heading, keep that exact existing document text in target_text.\n"
                "Do not include markdown fences or any prose outside the JSON object."
            )
        else:
            repair_instructions = (
                "You are repairing a structured-output response. "
                "Return valid JSON only, matching the previously requested schema. "
                "Do not perform more research."
            )
            repair_input = (
                "Reformat your previous answer as valid JSON only. "
                "Do not include markdown fences or any prose outside the JSON object."
            )
        try:
            repair_response = self._create_background_response(
                instructions=repair_instructions,
                input_payload=repair_input,
                tools=[],
                previous_response_id=(getattr(response, "id", "") or "").strip() or None,
                tool_choice="none",
                max_output_tokens=_JSON_REPAIR_MAX_OUTPUT_TOKENS,
                mode=run.mode,
                reasoning_effort=AGENT_JSON_REPAIR_REASONING_EFFORT,
            )
        except AgentExecutionError as exc:
            return self._mark_run_failed(run, str(exc))
        run.previous_response_id = (getattr(response, "id", "") or "").strip()
        return self._attach_started_response(
            run=run,
            response=repair_response,
            stage="repairing_json",
            phase=_RUN_PHASE_FINALIZE,
        )

    def _finalize_chat_run(self, *, run: DocumentResearchRun, response: Any, answer: str) -> DocumentResearchRun:
        metadata = dict(run.metadata or {})
        if not metadata.get("finalization_source"):
            metadata["finalization_source"] = "normal"
            run.metadata = metadata
            self._refresh_run_evidence_pack(run=run)
        response_id = (getattr(response, "id", "") or "").strip()
        session = run.session
        if response_id and session.last_response_id != response_id:
            session.last_response_id = response_id
            session.save(update_fields=["last_response_id", "updated_at"])
        result_payload = {
            "answer": answer,
            "response_id": response_id,
            "tool_calls": _public_tool_calls(run.tool_calls or []),
            "citations": run.citations or [],
            "used_tools": _used_tools(run.tool_calls or []),
            "metadata": {
                "model": metadata.get("model", AGENT_MODEL),
                "reasoning_effort": metadata.get("reasoning_effort", AGENT_REASONING_EFFORT),
                "mcp_fallback": bool(metadata.get("mcp_fallback")),
            },
        }
        return self._mark_run_completed(run, result_payload=result_payload, response=response)

    def _finalize_suggest_run(self, *, run: DocumentResearchRun, response: Any, answer: str) -> DocumentResearchRun:
        metadata = dict(run.metadata or {})
        if not metadata.get("finalization_source"):
            metadata["finalization_source"] = "normal"
            run.metadata = metadata
            self._refresh_run_evidence_pack(run=run)
        parsed = _extract_json_object(answer)
        if parsed is None:
            return self._queue_json_repair(run=run, response=response)

        if not any(call.get("source") == "biaedge" for call in run.tool_calls or []):
            return self._mark_run_failed(run, "The suggestion run completed without using BIA Edge tools.")

        result_payload = {
            "selection_summary": str(parsed.get("selection_summary") or "").strip(),
            "draft_gap": str(parsed.get("draft_gap") or "").strip(),
            "authorities": _normalize_authorities(parsed.get("authorities")),
            "search_notes": str(parsed.get("search_notes") or "").strip(),
            "next_questions": [
                str(item).strip()
                for item in (parsed.get("next_questions") or [])
                if str(item).strip()
            ],
            "response_id": (getattr(response, "id", "") or "").strip(),
            "tool_calls": _public_tool_calls(run.tool_calls or []),
            "citations": run.citations or [],
            "raw_answer": answer,
        }
        return self._mark_run_completed(run, result_payload=result_payload, response=response)

    def _finalize_edit_run(self, *, run: DocumentResearchRun, response: Any, answer: str) -> DocumentResearchRun:
        metadata = dict(run.metadata or {})
        if not metadata.get("finalization_source"):
            metadata["finalization_source"] = "normal"
            run.metadata = metadata
            self._refresh_run_evidence_pack(run=run)
        parsed = _extract_json_object(answer)
        if parsed is None:
            return self._queue_json_repair(run=run, response=response)

        normalized = _normalize_edit_result(parsed, request_payload=run.request_payload or {})
        if not normalized:
            return self._mark_run_failed(run, _structured_result_failure_message(run.mode))
        if normalized["operation"] != "delete_selection" and not normalized["proposed_text"]:
            return self._queue_json_repair(run=run, response=response)
        return self._mark_run_completed(
            run,
            result_payload=self._build_edit_result_payload(
                run=run,
                response=response,
                normalized=normalized,
                answer=answer,
            ),
            response=response,
        )

    def _build_edit_result_payload(
        self,
        *,
        run: DocumentResearchRun,
        response: Any,
        normalized: dict[str, Any],
        answer: str,
    ) -> dict[str, Any]:
        return {
            **normalized,
            "selection_required": bool(normalized.get("selected_text")) and normalized.get("operation") != "append_to_document",
            "target_review_required": normalized.get("operation") != "append_to_document",
            "response_id": (getattr(response, "id", "") or "").strip(),
            "tool_calls": _public_tool_calls(run.tool_calls or []),
            "citations": run.citations or [],
            "used_tools": _used_tools(run.tool_calls or []),
            "raw_answer": answer,
            "metadata": {
                "model": (run.metadata or {}).get("model", AGENT_MODEL),
                "reasoning_effort": (run.metadata or {}).get("reasoning_effort", AGENT_REASONING_EFFORT),
            },
        }

    def _force_final_response(
        self,
        *,
        instructions: str,
        current_input: Any,
        current_previous_id: str | None,
        response: Any,
    ) -> dict[str, Any] | None:
        previous_id = (current_previous_id or getattr(response, "id", "") or "").strip() or None
        if not previous_id:
            return None

        if isinstance(current_input, list) and current_input and all(
            isinstance(item, dict) and item.get("type") == "function_call_output"
            for item in current_input
        ):
            follow_up_input = list(current_input)
        else:
            follow_up_input = []

        follow_up_input.append(
            {
                "role": "user",
                "content": (
                    "Provide the final answer to the attorney now. "
                    "Do not call any more tools."
                ),
            }
        )

        follow_up = self._create_response(
            instructions=(
                instructions
                + "\n\nYou have already received the relevant tool outputs for this turn. "
                + "Provide the final answer now and do not call any more tools."
            ),
            input_payload=follow_up_input,
            tools=[],
            previous_response_id=previous_id,
            tool_choice="none",
            max_output_tokens=AGENT_FINALIZATION_MAX_OUTPUT_TOKENS,
            reasoning_effort=AGENT_FINALIZATION_REASONING_EFFORT,
        )

        answer = _extract_output_text(follow_up)
        if not answer:
            return None

        return {
            "answer": answer,
            "response_id": getattr(follow_up, "id", "") or "",
            "tool_calls": _extract_hosted_tool_calls(follow_up),
            "citations": _extract_citations(follow_up),
        }

    def _has_mcp_tools(self, *, tools: list[dict[str, Any]]) -> bool:
        return any((tool.get("type") or "").strip() == "mcp" for tool in tools)

    def _chat_fallback_instructions(self) -> str:
        return (
            _CHAT_SYSTEM_PROMPT
            + "\n\nRuntime note:\n"
            + "BIA Edge database access is unavailable for this turn. "
            + "Do not claim you searched the database. "
            + "Use only the remaining tools, and say explicitly if database verification would materially matter."
        )

    def _finalization_input_from_run(self, *, run: DocumentResearchRun) -> str:
        request_payload = run.request_payload or {}
        selected_text = str(request_payload.get("selected_text") or "").strip()
        focus_note = str(request_payload.get("focus_note") or "").strip()
        attorney_message = str(
            request_payload.get("message")
            or request_payload.get("instruction")
            or ""
        ).strip()
        request_requirements = _turn_requirements_block(self._requirements_for_run(run=run))
        document_type = self.document.document_type.name if self.document.document_type else "Unknown"
        document_slug = self.document.document_type.slug if self.document.document_type else ""
        metadata = dict(run.metadata or {})
        evidence_pack = metadata.get("evidence_pack") if isinstance(metadata.get("evidence_pack"), dict) else {}
        if not evidence_pack:
            evidence_pack = _build_evidence_pack(run.tool_calls or [])
            metadata["evidence_pack"] = evidence_pack
            run.metadata = metadata
        evidence_text = _evidence_pack_text(evidence_pack)

        parts = [
            "You are finalizing a research answer for the attorney using verified tool results already gathered.",
            f"Document title: {self.document.title}",
            f"Document type: {document_type}",
        ]
        if document_slug:
            parts.append(f"Document type slug: {document_slug}")
        if request_requirements:
            parts.extend(["", request_requirements])
        if attorney_message:
            parts.extend(["", "Attorney request:", attorney_message[:4000]])
        if selected_text:
            parts.extend(["", "Selected text:", selected_text[:4000]])
        if focus_note:
            parts.extend(["", "User focus note:", focus_note[:2000]])
        if evidence_text:
            parts.extend(["", "Evidence pack:", evidence_text])

        if run.mode == "suggest":
            parts.extend(
                [
                    "",
                    "Task:",
                    "Return the final structured authority suggestion now using only the verified research above. Do not call any tools.",
                ]
            )
        elif run.mode == "edit":
            parts.extend(
                [
                    "",
                    "Task:",
                    "Return the final structured edit proposal now using only the verified research above. "
                    "Do not call any tools. Return valid JSON only.",
                ]
            )
        else:
            parts.extend(
                [
                    "",
                    "Task:",
                    "Answer the attorney directly using only the verified research above. Do not call any tools. "
                    "If the gathered research is insufficient for any requested quote or proposition, say so plainly.",
                ]
            )

        return "\n".join(parts).strip()

    def _document_context_block(self, *, mode: str, selected_text: str = "", focus_note: str = "") -> str:
        doc_type = self.document.document_type.name if self.document.document_type else "Unknown"
        doc_slug = self.document.document_type.slug if self.document.document_type else ""
        plain_text = extract_plain_text(self.document.content, max_chars=40000)
        if mode == "suggest":
            max_chars = _SUGGEST_DOCUMENT_MAX_CHARS
            tail_chars = _SUGGEST_DOCUMENT_TAIL_CHARS
        elif mode == "edit":
            max_chars = _EDIT_DOCUMENT_MAX_CHARS
            tail_chars = _EDIT_DOCUMENT_TAIL_CHARS
        else:
            max_chars = _CHAT_DOCUMENT_MAX_CHARS
            tail_chars = _CHAT_DOCUMENT_TAIL_CHARS
        clipped_text = clip_document_text(plain_text, max_chars=max_chars, tail_chars=tail_chars)

        lines = [
            "Current document context:",
            f"- Title: {self.document.title}",
            f"- Document type: {doc_type}",
        ]
        if doc_slug:
            lines.append(f"- Document type slug: {doc_slug}")
        client_file_count = self.document.client_files.count()
        if client_file_count:
            lines.append(f"- Uploaded client documents available: {client_file_count}")
        if selected_text:
            lines.extend(
                [
                    "",
                    "Selected text:",
                    selected_text.strip()[:4000],
                ]
            )
        if focus_note:
            lines.extend(
                [
                    "",
                    "User focus note:",
                    focus_note.strip()[:2000],
                ]
            )
        lines.extend(
            [
                "",
                "Draft excerpt:",
                clipped_text or "[The document is currently empty.]",
            ]
        )
        return "\n".join(lines).strip()

    def _document_outline_block(self) -> str:
        content = self.document.content.get("content", []) if isinstance(self.document.content, dict) else []
        if not isinstance(content, list):
            return ""

        outline_lines = []
        for node in content:
            if not isinstance(node, dict) or node.get("type") != "heading":
                continue
            level = node.get("attrs", {}).get("level") or 1
            text = extract_plain_text(node, max_chars=240).replace("\n", " ").strip()
            if not text:
                continue
            outline_lines.append(f"- H{level}: {text}")
            if len(outline_lines) >= 18:
                break

        if not outline_lines:
            return ""
        return "Document outline:\n" + "\n".join(outline_lines)

    def _chat_input(self, *, message: str, selected_text: str = "", transcript_messages: list[Any]) -> str:
        transcript = self._transcript_block(transcript_messages)
        blocks = []
        if transcript:
            blocks.append("Conversation so far:\n" + transcript)
        blocks.append(self._document_context_block(mode="chat", selected_text=selected_text))
        blocks.append(
            _turn_requirements_block(
                _make_turn_requirements(
                    mode="chat",
                    request_text=message,
                    selected_text=selected_text,
                    has_client_files=self.has_client_files,
                    has_active_exemplars=self.has_active_exemplars,
                )
            )
        )
        blocks.append("User message:\n" + message.strip())
        return "\n\n".join(block for block in blocks if block).strip()

    def _suggest_input(self, *, selected_text: str, focus_note: str = "") -> str:
        blocks = [
            self._document_context_block(mode="suggest", selected_text=selected_text, focus_note=focus_note),
        ]
        blocks.append(
            _turn_requirements_block(
                _make_turn_requirements(
                    mode="suggest",
                    request_text=focus_note,
                    selected_text=selected_text,
                    has_client_files=self.has_client_files,
                    has_active_exemplars=self.has_active_exemplars,
                )
            )
        )
        blocks.append("Task:\nSuggest the best authorities for the selected passage in this document.")
        return "\n\n".join(block for block in blocks if block).strip()

    def _edit_input(self, *, instruction: str, selected_text: str = "") -> str:
        blocks = [
            self._document_context_block(mode="edit", selected_text=selected_text),
        ]
        outline = self._document_outline_block()
        if outline:
            blocks.append(outline)
        blocks.append(
            _turn_requirements_block(
                _make_turn_requirements(
                    mode="edit",
                    request_text=instruction,
                    selected_text=selected_text,
                    has_client_files=self.has_client_files,
                    has_active_exemplars=self.has_active_exemplars,
                )
            )
        )
        if selected_text:
            blocks.append(
                "Edit target:\nUse the selected text as the exact target for any replace, insert-before, insert-after, or delete operation."
            )
        else:
            blocks.append(
                "Edit target:\nNo text is currently selected. If the user identified a place in the draft, anchor the edit to an exact existing heading or paragraph from the document. Use append_to_document only if there is no reliable in-document target."
            )
        blocks.append("User edit instruction:\n" + instruction.strip())
        return "\n\n".join(block for block in blocks if block).strip()

    def _transcript_block(self, transcript_messages: list[Any]) -> str:
        if not transcript_messages:
            return ""

        trimmed = transcript_messages[-_CHAT_TRANSCRIPT_LIMIT:]
        lines = []
        for message in trimmed:
            role = getattr(message, "role", "") or ""
            content = (getattr(message, "content", "") or "").strip()
            if not role or not content:
                continue
            lines.append(f"{role.title()}: {content}")
        return "\n".join(lines).strip()

    def _call_local_tool(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "search_client_documents":
            return _search_client_files_for_agent(
                document=self.document,
                query=str(arguments.get("query") or ""),
                limit=arguments.get("limit") or 5,
            )
        if name == "get_client_document":
            return _get_client_file_for_agent(
                document=self.document,
                file_id=_coerce_int(arguments.get("file_id")) or 0,
            )
        if name == "search_exemplars":
            return _search_exemplars_for_agent(
                user=self.user,
                query=str(arguments.get("query") or ""),
                limit=arguments.get("limit") or 5,
                document_type_slug=str(arguments.get("document_type_slug") or ""),
            )
        if name == "get_exemplar":
            return _get_exemplar_for_agent(
                user=self.user,
                exemplar_id=_coerce_int(arguments.get("exemplar_id")) or 0,
            )
        return {"error": f"Unknown tool: {name}"}

    def _create_response(
        self,
        *,
        instructions: str,
        input_payload: Any,
        tools: list[dict[str, Any]],
        previous_response_id: str | None,
        tool_choice: str,
        max_output_tokens: int,
        reasoning_effort: str | None = None,
    ):
        request = {
            "model": AGENT_MODEL,
            "instructions": instructions,
            "input": input_payload,
            "tools": tools,
            "tool_choice": tool_choice,
            "parallel_tool_calls": True,
            "max_tool_calls": AGENT_MAX_TOOL_CALLS,
            "max_output_tokens": max_output_tokens,
            "reasoning": {"effort": _normalize_reasoning_effort(reasoning_effort or AGENT_REASONING_EFFORT)},
            "store": True,
            "include": list(_TOOL_INCLUDE_FIELDS),
            "truncation": "auto",
        }
        if previous_response_id:
            request["previous_response_id"] = previous_response_id

        try:
            return self.client.responses.create(**request)
        except Exception as exc:
            if previous_response_id and _stale_previous_response(exc):
                raise StaleResponseChainError(str(exc)) from exc
            logger.exception(
                "Document research agent response creation failed",
                extra={
                    "document_id": str(self.document.id),
                    "user_id": getattr(self.user, "id", None),
                    "model": AGENT_MODEL,
                    "tool_choice": tool_choice,
                    "has_previous_response_id": bool(previous_response_id),
                    "tool_types": [tool.get("type") for tool in tools if isinstance(tool, dict)],
                },
            )
            raise AgentExecutionError(_openai_exception_message(exc)) from exc

    def _run_response_loop(
        self,
        *,
        instructions: str,
        input_payload: Any,
        tools: list[dict[str, Any]],
        previous_response_id: str | None,
        initial_tool_choice: str,
    ) -> dict[str, Any]:
        tool_calls: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        answer_fragments: list[str] = []
        response = None
        current_input = input_payload
        current_previous_id = previous_response_id
        current_tool_choice = initial_tool_choice
        continuation_budget = 2

        for _ in range(_MAX_FUNCTION_ROUNDS):
            response = self._create_response(
                instructions=instructions,
                input_payload=current_input,
                tools=tools,
                previous_response_id=current_previous_id,
                tool_choice=current_tool_choice,
                max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
            )
            tool_calls.extend(_extract_hosted_tool_calls(response))
            citations.extend(_extract_citations(response))

            function_calls = _pending_function_calls(response)
            if function_calls:
                outputs = []
                for call in function_calls:
                    raw_arguments = getattr(call, "arguments", "") or ""
                    parsed_arguments = _safe_json_loads(raw_arguments, default={}) or {}
                    result = self._call_local_tool(
                        name=getattr(call, "name", "") or "",
                        arguments=parsed_arguments,
                    )
                    tool_calls.append(
                        {
                            "source": "knowledge",
                            "type": "function_call",
                            "name": getattr(call, "name", "") or "",
                            "status": "completed",
                            "arguments": parsed_arguments,
                        }
                    )
                    outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": getattr(call, "call_id", "") or "",
                            "output": json.dumps(result),
                        }
                    )

                current_input = outputs
                current_previous_id = getattr(response, "id", "") or ""
                current_tool_choice = "auto"
                continue

            status = (getattr(response, "status", "") or "").strip().lower()
            if status == "incomplete" and continuation_budget > 0:
                partial_answer = _extract_output_text(response)
                if partial_answer:
                    answer_fragments.append(partial_answer)
                continuation_budget -= 1
                current_input = (
                    "Continue exactly where you left off and finish the response. "
                    "Do not restart the answer."
                )
                current_previous_id = getattr(response, "id", "") or ""
                current_tool_choice = "auto"
                continue

            if status not in {"completed", ""}:
                error_message = _extract_error_text(getattr(response, "error", None))
                raise AgentExecutionError(
                    error_message or f"OpenAI response returned status={status or 'unknown'}."
                )
            break

        if response is None:
            raise AgentExecutionError("No response was generated.")

        answer = _join_text_fragments(answer_fragments + [_extract_output_text(response)])
        if not answer:
            logger.warning(
                "Document research agent produced no assistant text; attempting finalization.",
                extra={
                    "document_id": str(self.document.id),
                    "user_id": getattr(self.user, "id", None),
                    "response_id": getattr(response, "id", "") or "",
                    "output_types": [getattr(item, "type", None) for item in getattr(response, "output", []) or []],
                },
            )
            forced = self._force_final_response(
                instructions=instructions,
                current_input=current_input,
                current_previous_id=current_previous_id,
                response=response,
            )
            if forced:
                tool_calls.extend(forced["tool_calls"])
                citations.extend(forced["citations"])
                forced_answer = _join_text_fragments(answer_fragments + [forced["answer"]])
                return {
                    "answer": forced_answer,
                    "response_id": forced["response_id"],
                    "tool_calls": tool_calls,
                    "citations": citations,
                }
            raise AgentExecutionError("The agent returned an empty response.")

        return {
            "answer": answer,
            "response_id": getattr(response, "id", "") or "",
            "tool_calls": tool_calls,
            "citations": citations,
        }

    def _repair_suggest_json(self, *, previous_response_id: str) -> dict[str, Any] | None:
        if not previous_response_id:
            return None

        repair_response = self._create_response(
            instructions=(
                "You are repairing a structured-output response. "
                "Return valid JSON only, matching the previously requested schema. "
                "Do not perform more research."
            ),
            input_payload=(
                "Reformat your previous answer as valid JSON only. "
                "Do not include markdown fences or any prose outside the JSON object."
            ),
            tools=[],
            previous_response_id=previous_response_id,
            tool_choice="auto",
            max_output_tokens=_JSON_REPAIR_MAX_OUTPUT_TOKENS,
        )
        return _extract_json_object(_extract_output_text(repair_response))

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from .document_text import clip_document_text, extract_plain_text
from .exemplar_service import rank_exemplars
from .models import Exemplar

logger = logging.getLogger(__name__)

AGENT_MODEL = os.environ.get("OPENAI_AGENT_MODEL", "gpt-5.4")
AGENT_REASONING_EFFORT = os.environ.get("OPENAI_AGENT_REASONING_EFFORT", "high").strip().lower() or "high"
AGENT_MAX_TOOL_CALLS = int(os.environ.get("OPENAI_AGENT_MAX_TOOL_CALLS", "12"))
AGENT_MAX_OUTPUT_TOKENS = int(os.environ.get("OPENAI_AGENT_MAX_OUTPUT_TOKENS", "1800"))
AGENT_TIMEOUT_SECONDS = int(os.environ.get("OPENAI_AGENT_TIMEOUT_SECONDS", "120"))
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
_JSON_REPAIR_MAX_OUTPUT_TOKENS = 1200

_CHAT_SYSTEM_PROMPT = """You are Hammond Law's document-side immigration research agent.
You work alongside the attorney inside a live drafting session.

Research operating rules:
1. Use the BIA Edge database tools first for legal authorities, holdings, statutes, regulations, policy sections, and validity checks.
2. Use the knowledge-base tools for firm exemplars, prior briefs, and internal language only when the user is asking about internal style, phrasing, formatting, or prior work product.
3. Use web search only when freshness matters, when the user explicitly asks for it, or when database tools do not answer the question.
4. Do not invent citations, case names, statutes, regulations, policy sections, or quoted passages.
5. Prefer precedential authorities in your legal analysis. If you mention unpublished decisions or non-precedential material, label that clearly.
6. If the user asks you to search the database, actually use the database tools instead of answering from memory.
7. If controlling authority is thin or uncertain, say so directly.

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
3. Use web search or knowledge tools only for context; do not include web-only authorities in the final authority list.
4. Do not invent any citation, holding, or proposition.
5. If the selected text is vague or under-supported, explain the gap instead of bluffing.
6. Return valid JSON only. No markdown fences, no prose outside the JSON object.

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

    return OpenAI(api_key=api_key, timeout=AGENT_TIMEOUT_SECONDS)


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


def _build_web_search_tool() -> dict[str, Any]:
    return {
        "type": "web_search_preview",
        "search_context_size": "medium",
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
        "text": (exemplar.extracted_text or "")[:8000],
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
    return "\n".join(chunks).strip()


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
            tool_calls.append(
                {
                    "source": "biaedge",
                    "type": "mcp_call",
                    "name": getattr(item, "name", "") or "",
                    "status": getattr(item, "status", "") or "",
                    "arguments": _safe_json_loads(getattr(item, "arguments", ""), default={}) or {},
                }
            )
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


class DocumentResearchAgent:
    def __init__(self, *, document, user):
        self.document = document
        self.user = user
        self.client = _new_openai_client()
        self.has_active_exemplars = Exemplar.objects.filter(
            created_by=user,
            is_active=True,
        ).exists()

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
        allowed_tools = _BIAEDGE_CHAT_TOOLS if mode == "chat" else _BIAEDGE_SUGGEST_TOOLS
        tools = [_build_web_search_tool()]
        if self.has_active_exemplars:
            tools.extend(_knowledge_function_tools())
        if include_mcp:
            tools.insert(0, _build_biaedge_mcp_tool(allowed_tools=allowed_tools))
        file_search = _build_file_search_tool()
        if file_search:
            tools.append(file_search)
        return tools

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
            tool_choice="auto",
            max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
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

    def _document_context_block(self, *, selected_text: str = "", focus_note: str = "") -> str:
        doc_type = self.document.document_type.name if self.document.document_type else "Unknown"
        doc_slug = self.document.document_type.slug if self.document.document_type else ""
        plain_text = extract_plain_text(self.document.content, max_chars=40000)
        clipped_text = clip_document_text(plain_text, max_chars=18000, tail_chars=5000)

        lines = [
            "Current document context:",
            f"- Title: {self.document.title}",
            f"- Document type: {doc_type}",
        ]
        if doc_slug:
            lines.append(f"- Document type slug: {doc_slug}")
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

    def _chat_input(self, *, message: str, selected_text: str = "", transcript_messages: list[Any]) -> str:
        transcript = self._transcript_block(transcript_messages)
        blocks = []
        if transcript:
            blocks.append("Conversation so far:\n" + transcript)
        blocks.append(self._document_context_block(selected_text=selected_text))
        blocks.append("User message:\n" + message.strip())
        return "\n\n".join(block for block in blocks if block).strip()

    def _suggest_input(self, *, selected_text: str, focus_note: str = "") -> str:
        return (
            self._document_context_block(selected_text=selected_text, focus_note=focus_note)
            + "\n\nTask:\n"
            + "Suggest the best authorities for the selected passage in this document."
        )

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
            "reasoning": {"effort": _normalize_reasoning_effort(AGENT_REASONING_EFFORT)},
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

        answer = _extract_output_text(response)
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
                return {
                    "answer": forced["answer"],
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

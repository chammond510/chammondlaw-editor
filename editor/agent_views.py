import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from .agent_service import AgentConfigurationError, AgentExecutionError, DocumentResearchAgent
from .models import Document, DocumentResearchMessage, DocumentResearchSession

logger = logging.getLogger(__name__)


def _serialize_message(message):
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "selection_text": message.selection_text,
        "response_id": message.response_id,
        "tool_calls": message.tool_calls or [],
        "citations": message.citations or [],
        "metadata": message.metadata or {},
        "created_at": message.created_at.isoformat(),
    }


def _get_session_for_document(*, user, document):
    session, _ = DocumentResearchSession.objects.get_or_create(
        document=document,
        user=user,
    )
    return session


@login_required
@require_GET
def agent_session(request, doc_id):
    document = get_object_or_404(Document, id=doc_id, created_by=request.user)
    session = _get_session_for_document(user=request.user, document=document)
    messages = [_serialize_message(message) for message in session.messages.order_by("created_at")]
    return JsonResponse(
        {
            "session": {
                "id": session.id,
                "last_response_id": session.last_response_id,
                "updated_at": session.updated_at.isoformat(),
            },
            "messages": messages,
        }
    )


@login_required
@require_POST
def agent_chat(request, doc_id):
    document = get_object_or_404(Document, id=doc_id, created_by=request.user)
    session = _get_session_for_document(user=request.user, document=document)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    message = (data.get("message") or "").strip()
    selected_text = (data.get("selected_text") or "").strip()
    if not message:
        return JsonResponse({"error": "message is required"}, status=400)

    transcript_messages = list(session.messages.order_by("created_at"))
    agent = DocumentResearchAgent(document=document, user=request.user)
    try:
        result = agent.chat(
            message=message,
            selected_text=selected_text,
            previous_response_id=session.last_response_id,
            transcript_messages=transcript_messages,
        )
    except AgentConfigurationError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    except AgentExecutionError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    except Exception:
        logger.exception(
            "Unexpected document agent chat failure",
            extra={"document_id": str(document.id), "user_id": request.user.id},
        )
        return JsonResponse(
            {"error": "The agent failed unexpectedly. The issue has been logged."},
            status=500,
        )

    user_message = DocumentResearchMessage.objects.create(
        session=session,
        role="user",
        content=message,
        selection_text=selected_text,
        metadata={"used_selection": bool(selected_text)},
    )
    assistant_message = DocumentResearchMessage.objects.create(
        session=session,
        role="assistant",
        content=result.answer,
        selection_text=selected_text,
        response_id=result.response_id,
        tool_calls=result.tool_calls,
        citations=result.citations,
        metadata=result.metadata,
    )
    session.last_response_id = result.response_id
    session.save(update_fields=["last_response_id", "updated_at"])

    return JsonResponse(
        {
            "user_message": _serialize_message(user_message),
            "assistant_message": _serialize_message(assistant_message),
            "used_tools": result.used_tools,
        }
    )


@login_required
@require_POST
def agent_reset(request, doc_id):
    document = get_object_or_404(Document, id=doc_id, created_by=request.user)
    session = _get_session_for_document(user=request.user, document=document)
    session.messages.all().delete()
    session.last_response_id = ""
    session.save(update_fields=["last_response_id", "updated_at"])
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def agent_suggest(request, doc_id):
    document = get_object_or_404(Document, id=doc_id, created_by=request.user)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    selected_text = (data.get("selected_text") or "").strip()
    focus_note = (data.get("focus_note") or "").strip()
    if not selected_text:
        return JsonResponse({"error": "selected_text is required"}, status=400)

    agent = DocumentResearchAgent(document=document, user=request.user)
    try:
        result = agent.suggest(selected_text=selected_text, focus_note=focus_note)
    except AgentConfigurationError as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    except AgentExecutionError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    except Exception:
        logger.exception(
            "Unexpected document agent suggest failure",
            extra={"document_id": str(document.id), "user_id": request.user.id},
        )
        return JsonResponse(
            {"error": "The agent failed unexpectedly. The issue has been logged."},
            status=500,
        )

    return JsonResponse(
        {
            "selection_summary": result.selection_summary,
            "draft_gap": result.draft_gap,
            "authorities": result.authorities,
            "search_notes": result.search_notes,
            "next_questions": result.next_questions,
            "tool_calls": result.tool_calls,
            "citations": result.citations,
            "response_id": result.response_id,
        }
    )

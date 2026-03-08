import json
import logging

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .agent_service import AgentConfigurationError, AgentExecutionError, DocumentResearchAgent
from .models import Document, DocumentResearchMessage, DocumentResearchRun, DocumentResearchSession, DocumentVersion

logger = logging.getLogger(__name__)

_ACTIVE_RUN_STATUSES = {"queued", "in_progress"}


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


def _serialize_run(run, *, include_result=False):
    payload = {
        "id": str(run.public_id),
        "mode": run.mode,
        "status": run.status,
        "stage": run.stage,
        "phase": (run.metadata or {}).get("phase") or run.stage,
        "error_message": run.error_message,
        "response_id": run.response_id,
        "response_count": run.response_count,
        "local_function_rounds": run.local_function_rounds,
        "usage": run.usage or {},
        "metrics": (run.metadata or {}).get("metrics") or {},
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }
    if include_result and run.result_payload:
        payload["result"] = run.result_payload
    return payload


def _fallback_chat_message_from_run(run):
    if run.mode != "chat" or run.status != "completed":
        return None

    result = run.result_payload or {}
    answer = str(result.get("answer") or "").strip()
    if not answer:
        return None

    return {
        "id": f"run-{run.public_id}",
        "role": "assistant",
        "content": answer,
        "selection_text": run.user_message.selection_text if run.user_message else "",
        "response_id": str(result.get("response_id") or run.response_id or "").strip(),
        "tool_calls": result.get("tool_calls") or [],
        "citations": result.get("citations") or [],
        "metadata": result.get("metadata") or {},
        "created_at": (run.completed_at or run.updated_at or timezone.now()).isoformat(),
    }


def _get_session_for_document(*, user, document):
    session, _ = DocumentResearchSession.objects.get_or_create(
        document=document,
        user=user,
    )
    return session


def _get_active_run(session):
    return (
        session.runs.filter(status__in=_ACTIVE_RUN_STATUSES)
        .order_by("-created_at")
        .first()
    )


def _mark_run_start_failure(run, message):
    run.status = "failed"
    run.stage = "failed"
    metadata = dict(run.metadata or {})
    metadata["phase"] = "failed"
    run.metadata = metadata
    run.error_message = (message or "The agent run failed to start.").strip()
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "stage", "metadata", "error_message", "completed_at", "updated_at"])
    return run


def _cancel_run_without_agent(run, reason):
    run.status = "cancelled"
    run.stage = "cancelled"
    metadata = dict(run.metadata or {})
    metadata["phase"] = "cancelled"
    run.metadata = metadata
    run.error_message = (reason or "The agent run was cancelled.").strip()
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "stage", "metadata", "error_message", "completed_at", "updated_at"])
    return run


def _persist_chat_completion(run):
    if run.mode != "chat" or run.status != "completed":
        return None
    if run.assistant_message_id:
        return run.assistant_message

    result = run.result_payload or {}
    answer = str(result.get("answer") or "").strip()
    if not answer:
        return None

    session = run.session
    assistant_message = DocumentResearchMessage.objects.create(
        session=session,
        role="assistant",
        content=answer,
        selection_text=run.user_message.selection_text if run.user_message else "",
        response_id=str(result.get("response_id") or "").strip(),
        tool_calls=result.get("tool_calls") or [],
        citations=result.get("citations") or [],
        metadata=result.get("metadata") or {},
    )
    run.assistant_message = assistant_message
    run.save(update_fields=["assistant_message", "updated_at"])
    session.last_response_id = str(result.get("response_id") or "").strip()
    session.save(update_fields=["last_response_id", "updated_at"])
    return assistant_message


def _mark_assistant_persist_failure(run, exc):
    metadata = dict(run.metadata or {})
    metadata["assistant_persist_failed"] = True
    metadata["assistant_persist_error"] = str(exc)[:500]
    run.metadata = metadata
    run.save(update_fields=["metadata", "updated_at"])
    return run


@login_required
@require_GET
def agent_session(request, doc_id):
    document = get_object_or_404(Document, id=doc_id, created_by=request.user)
    session = _get_session_for_document(user=request.user, document=document)
    messages = [_serialize_message(message) for message in session.messages.order_by("created_at")]
    active_run = _get_active_run(session)
    latest_suggest_run = (
        session.runs.filter(mode="suggest", status="completed")
        .order_by("-completed_at", "-created_at")
        .first()
    )
    latest_edit_run = (
        session.runs.filter(mode="edit", status="completed")
        .order_by("-completed_at", "-created_at")
        .first()
    )
    return JsonResponse(
        {
            "session": {
                "id": session.id,
                "last_response_id": session.last_response_id,
                "updated_at": session.updated_at.isoformat(),
            },
            "messages": messages,
            "active_run": _serialize_run(active_run) if active_run else None,
            "latest_suggest_run": _serialize_run(latest_suggest_run, include_result=True) if latest_suggest_run else None,
            "latest_edit_run": _serialize_run(latest_edit_run, include_result=True) if latest_edit_run else None,
        }
    )


@login_required
@require_POST
def agent_chat(request, doc_id):
    document = get_object_or_404(Document, id=doc_id, created_by=request.user)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    message = (data.get("message") or "").strip()
    selected_text = (data.get("selected_text") or "").strip()
    if not message:
        return JsonResponse({"error": "message is required"}, status=400)

    with transaction.atomic():
        session = _get_session_for_document(user=request.user, document=document)
        session = DocumentResearchSession.objects.select_for_update().get(id=session.id)
        active_run = _get_active_run(session)
        if active_run:
            return JsonResponse(
                {
                    "error": "Another research agent task is already running for this document.",
                    "run": _serialize_run(active_run),
                },
                status=409,
            )
        transcript_messages = list(session.messages.order_by("created_at"))
        user_message = DocumentResearchMessage.objects.create(
            session=session,
            role="user",
            content=message,
            selection_text=selected_text,
            metadata={"used_selection": bool(selected_text)},
        )
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="chat",
            status="queued",
            stage="queued",
            user_message=user_message,
        )

    try:
        agent = DocumentResearchAgent(document=document, user=request.user)
        run = agent.start_chat_run(
            run=run,
            message=message,
            selected_text=selected_text,
            previous_response_id=session.last_response_id,
            transcript_messages=transcript_messages,
        )
    except AgentConfigurationError as exc:
        _mark_run_start_failure(run, str(exc))
        return JsonResponse({"error": str(exc), "run": _serialize_run(run)}, status=503)
    except AgentExecutionError as exc:
        _mark_run_start_failure(run, str(exc))
        return JsonResponse({"error": str(exc), "run": _serialize_run(run)}, status=502)
    except Exception:
        logger.exception(
            "Unexpected document agent chat start failure",
            extra={"document_id": str(document.id), "user_id": request.user.id, "run_id": str(run.public_id)},
        )
        _mark_run_start_failure(run, "The agent failed unexpectedly while starting.")
        return JsonResponse(
            {"error": "The agent failed unexpectedly while starting.", "run": _serialize_run(run)},
            status=500,
        )

    return JsonResponse(
        {
            "run": _serialize_run(run),
            "user_message": _serialize_message(user_message),
        },
        status=202,
    )


@login_required
@require_POST
def agent_reset(request, doc_id):
    document = get_object_or_404(Document, id=doc_id, created_by=request.user)
    session = _get_session_for_document(user=request.user, document=document)
    active_runs = list(session.runs.filter(status__in=_ACTIVE_RUN_STATUSES))

    agent = None
    if active_runs:
        try:
            agent = DocumentResearchAgent(document=document, user=request.user)
        except AgentConfigurationError:
            agent = None

    for run in active_runs:
        if agent:
            agent.cancel_run(run=run, reason="The attorney cleared the chat thread.")
        else:
            _cancel_run_without_agent(run, "The attorney cleared the chat thread.")

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

    with transaction.atomic():
        session = _get_session_for_document(user=request.user, document=document)
        session = DocumentResearchSession.objects.select_for_update().get(id=session.id)
        active_run = _get_active_run(session)
        if active_run:
            return JsonResponse(
                {
                    "error": "Another research agent task is already running for this document.",
                    "run": _serialize_run(active_run),
                },
                status=409,
            )
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="suggest",
            status="queued",
            stage="queued",
        )

    try:
        agent = DocumentResearchAgent(document=document, user=request.user)
        run = agent.start_suggest_run(
            run=run,
            selected_text=selected_text,
            focus_note=focus_note,
        )
    except AgentConfigurationError as exc:
        _mark_run_start_failure(run, str(exc))
        return JsonResponse({"error": str(exc), "run": _serialize_run(run)}, status=503)
    except AgentExecutionError as exc:
        _mark_run_start_failure(run, str(exc))
        return JsonResponse({"error": str(exc), "run": _serialize_run(run)}, status=502)
    except Exception:
        logger.exception(
            "Unexpected document agent suggest start failure",
            extra={"document_id": str(document.id), "user_id": request.user.id, "run_id": str(run.public_id)},
        )
        _mark_run_start_failure(run, "The agent failed unexpectedly while starting.")
        return JsonResponse(
            {"error": "The agent failed unexpectedly while starting.", "run": _serialize_run(run)},
            status=500,
        )

    return JsonResponse({"run": _serialize_run(run)}, status=202)


@login_required
@require_POST
def agent_edit(request, doc_id):
    document = get_object_or_404(Document, id=doc_id, created_by=request.user)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    instruction = (data.get("instruction") or "").strip()
    selected_text = (data.get("selected_text") or "").strip()
    selection_from = data.get("selection_from")
    selection_to = data.get("selection_to")
    if not instruction:
        return JsonResponse({"error": "instruction is required"}, status=400)

    with transaction.atomic():
        session = _get_session_for_document(user=request.user, document=document)
        session = DocumentResearchSession.objects.select_for_update().get(id=session.id)
        active_run = _get_active_run(session)
        if active_run:
            return JsonResponse(
                {
                    "error": "Another research agent task is already running for this document.",
                    "run": _serialize_run(active_run),
                },
                status=409,
            )
        run = DocumentResearchRun.objects.create(
            session=session,
            mode="edit",
            status="queued",
            stage="queued",
        )

    try:
        agent = DocumentResearchAgent(document=document, user=request.user)
        run = agent.start_edit_run(
            run=run,
            instruction=instruction,
            selected_text=selected_text,
            selection_from=selection_from,
            selection_to=selection_to,
        )
    except AgentConfigurationError as exc:
        _mark_run_start_failure(run, str(exc))
        return JsonResponse({"error": str(exc), "run": _serialize_run(run)}, status=503)
    except AgentExecutionError as exc:
        _mark_run_start_failure(run, str(exc))
        return JsonResponse({"error": str(exc), "run": _serialize_run(run)}, status=502)
    except Exception:
        logger.exception(
            "Unexpected document agent edit start failure",
            extra={"document_id": str(document.id), "user_id": request.user.id, "run_id": str(run.public_id)},
        )
        _mark_run_start_failure(run, "The agent failed unexpectedly while starting.")
        return JsonResponse(
            {"error": "The agent failed unexpectedly while starting.", "run": _serialize_run(run)},
            status=500,
        )

    return JsonResponse({"run": _serialize_run(run)}, status=202)


@login_required
@require_POST
def agent_apply_edit(request, doc_id):
    document = get_object_or_404(Document, id=doc_id, created_by=request.user)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    run_id = str(data.get("run_id") or "").strip()
    current_content = data.get("current_content")
    new_content = data.get("new_content")
    if not run_id:
        return JsonResponse({"error": "run_id is required"}, status=400)
    if not isinstance(current_content, dict) or not isinstance(new_content, dict):
        return JsonResponse({"error": "current_content and new_content must be Tiptap JSON objects."}, status=400)
    if current_content == new_content:
        return JsonResponse({"error": "No document changes were provided."}, status=400)

    with transaction.atomic():
        run = get_object_or_404(
            DocumentResearchRun.objects.select_for_update().select_related("session", "session__document"),
            public_id=run_id,
            session__user=request.user,
            session__document=document,
            session__document__created_by=request.user,
        )
        if run.mode != "edit":
            return JsonResponse({"error": "That run is not an edit proposal."}, status=400)
        if run.status != "completed":
            return JsonResponse({"error": "The edit proposal is not ready to apply yet."}, status=409)

        metadata = dict(run.metadata or {})
        if metadata.get("applied_at"):
            return JsonResponse({"error": "This edit proposal has already been applied."}, status=409)

        result_payload = run.result_payload or {}
        summary = str(result_payload.get("edit_summary") or "Agent edit").strip()
        label = f"Before agent edit - {summary}"[:100]
        version = DocumentVersion.objects.create(
            document=document,
            content=current_content,
            label=label,
        )

        document.content = new_content
        document.save(update_fields=["content", "updated_at"])

        metadata["applied_at"] = timezone.now().isoformat()
        metadata["applied_snapshot_id"] = version.id
        run.metadata = metadata
        result_payload = dict(run.result_payload or {})
        result_payload["applied_at"] = metadata["applied_at"]
        run.result_payload = result_payload
        run.save(update_fields=["metadata", "result_payload", "updated_at"])

    return JsonResponse(
        {
            "status": "ok",
            "updated_at": document.updated_at.isoformat(),
            "version": {
                "id": version.id,
                "label": version.label,
                "created_at": version.created_at.isoformat(),
            },
            "edit_result": {
                **(run.result_payload or {}),
                "run_id": str(run.public_id),
            },
        }
    )


@login_required
@require_GET
def agent_run_status(request, run_id):
    run = get_object_or_404(
        DocumentResearchRun.objects.select_related(
            "session",
            "session__document",
            "assistant_message",
            "user_message",
        ),
        public_id=run_id,
        session__user=request.user,
        session__document__created_by=request.user,
    )

    try:
        if run.status in _ACTIVE_RUN_STATUSES:
            try:
                agent = DocumentResearchAgent(document=run.session.document, user=request.user)
                run = agent.advance_run(run=run)
            except AgentConfigurationError as exc:
                run = _mark_run_start_failure(run, str(exc))
            except Exception:
                logger.exception(
                    "Unexpected document agent polling failure",
                    extra={
                        "document_id": str(run.session.document.id),
                        "user_id": request.user.id,
                        "run_id": str(run.public_id),
                    },
                )
                run = _mark_run_start_failure(run, "The agent failed unexpectedly while polling.")

        assistant_message = None
        fallback_assistant_message = _fallback_chat_message_from_run(run)
        if run.mode == "chat" and run.status == "completed":
            if run.assistant_message_id:
                assistant_message = run.assistant_message
            elif not (run.metadata or {}).get("assistant_persist_failed"):
                try:
                    with transaction.atomic():
                        locked_run = DocumentResearchRun.objects.select_for_update().select_related(
                            "session",
                            "assistant_message",
                            "user_message",
                        ).get(id=run.id)
                        assistant_message = _persist_chat_completion(locked_run)
                        run = locked_run
                except Exception as exc:
                    logger.exception(
                        "Unable to persist completed document agent chat message",
                        extra={
                            "document_id": str(run.session.document.id),
                            "user_id": request.user.id,
                            "run_id": str(run.public_id),
                        },
                    )
                    run = _mark_assistant_persist_failure(run, exc)

        return JsonResponse(
            {
                "run": _serialize_run(run, include_result=run.mode in {"suggest", "edit"}),
                "assistant_message": (
                    _serialize_message(assistant_message)
                    if assistant_message
                    else (_serialize_message(run.assistant_message) if run.assistant_message_id else fallback_assistant_message)
                ),
                "suggest_result": run.result_payload if run.mode == "suggest" and run.status == "completed" else None,
                "edit_result": run.result_payload if run.mode == "edit" and run.status == "completed" else None,
            }
        )
    except Exception:
        logger.exception(
            "Unexpected document agent run status failure",
            extra={
                "document_id": str(run.session.document.id),
                "user_id": request.user.id,
                "run_id": str(run.public_id),
            },
        )
        fallback_assistant_message = _fallback_chat_message_from_run(run)
        status = 200 if fallback_assistant_message else 500
        return JsonResponse(
            {
                "error": "The agent run status could not be fully loaded.",
                "run": _serialize_run(run, include_result=run.mode in {"suggest", "edit"}),
                "assistant_message": fallback_assistant_message,
                "suggest_result": run.result_payload if run.mode == "suggest" and run.status == "completed" else None,
                "edit_result": run.result_payload if run.mode == "edit" and run.status == "completed" else None,
            },
            status=status,
        )

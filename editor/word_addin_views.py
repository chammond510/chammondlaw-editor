import json
import os
import uuid
from urllib.parse import urlsplit

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.templatetags.static import static
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.http import require_GET, require_POST

from .models import (
    DocumentType,
    WritingWorkspace,
    WorkspaceResearchMessage,
    WorkspaceResearchRun,
    WorkspaceResearchSession,
)


WORD_ADDIN_DEFAULT_BRIDGE_URL = os.environ.get("WORD_ADDIN_DEFAULT_BRIDGE_URL", "https://localhost:8765")


def _absolute_root(url):
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _workspace_payload(workspace):
    return {
        "id": str(workspace.id),
        "kind": workspace.kind,
        "title": workspace.title,
        "document_type": (
            {
                "id": workspace.document_type_id,
                "name": workspace.document_type.name,
                "slug": workspace.document_type.slug,
                "category": workspace.document_type.category,
                "export_format": workspace.document_type.export_format,
            }
            if workspace.document_type_id
            else None
        ),
        "metadata": workspace.metadata or {},
        "updated_at": workspace.updated_at.isoformat(),
    }


def _message_payload(message):
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "selection_text": message.selection_text,
        "citations": message.citations or [],
        "metadata": message.metadata or {},
        "created_at": message.created_at.isoformat(),
    }


def _run_payload(run):
    return {
        "id": str(run.public_id),
        "mode": run.mode,
        "status": run.status,
        "request": run.request_payload or {},
        "result": run.result_payload or {},
        "error_message": run.error_message,
        "bridge_job_id": run.bridge_job_id,
        "metadata": run.metadata or {},
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


def _json_body(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return None


def _resolve_document_type(slug):
    normalized = str(slug or "").strip()
    if not normalized:
        return None
    return DocumentType.objects.filter(slug=normalized).first()


def _status_code_for_run(status):
    if status == "failed":
        return 502
    return 200


def _citation_text(authority):
    citation = str(authority.get("citation") or "").strip()
    title = str(authority.get("title") or "").strip()
    return citation or title or "Authority"


@require_GET
def word_addin_manifest(request):
    taskpane_url = request.build_absolute_uri("/word-addin/taskpane/")
    commands_url = request.build_absolute_uri("/word-addin/commands/")
    icon_url = request.build_absolute_uri(static("word-addin/icon.png"))
    return render(
        request,
        "editor/word_addin_manifest.xml",
        {
            "app_domain": _absolute_root(taskpane_url),
            "commands_url": commands_url,
            "taskpane_url": taskpane_url,
            "icon_url": icon_url,
        },
        content_type="text/xml",
    )


@require_GET
def word_addin_commands(request):
    return render(request, "editor/word_addin_commands.html")


@login_required
@require_GET
def word_addin_taskpane(request):
    return render(
        request,
        "editor/word_addin_taskpane.html",
        {
            "bridge_url": WORD_ADDIN_DEFAULT_BRIDGE_URL,
        },
    )


@login_required
@require_GET
def word_addin_document_types(request):
    types = DocumentType.objects.all()
    return JsonResponse(
        {
            "document_types": [
                {
                    "id": doc_type.id,
                    "name": doc_type.name,
                    "slug": doc_type.slug,
                    "category": doc_type.category,
                    "export_format": doc_type.export_format,
                }
                for doc_type in types
            ]
        }
    )


@login_required
@require_POST
def word_addin_workspace_bootstrap(request):
    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    requested_id = str(data.get("workspace_id") or "").strip()
    title = str(data.get("document_title") or "").strip()[:500] or "Untitled Word Document"
    persistent = bool(data.get("persistent", True))
    document_type = _resolve_document_type(data.get("document_type_slug"))
    external_document_key = str(data.get("external_document_key") or "").strip()[:200]

    workspace = None
    if requested_id:
        try:
            workspace_uuid = uuid.UUID(requested_id)
        except ValueError:
            return JsonResponse({"error": "workspace_id must be a valid UUID."}, status=400)
        workspace = WritingWorkspace.objects.filter(id=workspace_uuid, user=request.user).first()

    if workspace is None:
        workspace = WritingWorkspace.objects.create(
            user=request.user,
            kind="word_addin",
            title=title,
            document_type=document_type,
            external_document_key=external_document_key,
            metadata={
                "persistent": persistent,
            },
        )
    else:
        workspace.title = title
        workspace.document_type = document_type
        if external_document_key:
            workspace.external_document_key = external_document_key
        metadata = dict(workspace.metadata or {})
        metadata["persistent"] = persistent
        workspace.metadata = metadata
        workspace.save(update_fields=["title", "document_type", "external_document_key", "metadata", "updated_at"])

    session, _ = WorkspaceResearchSession.objects.get_or_create(
        workspace=workspace,
        user=request.user,
    )
    return JsonResponse(
        {
            "workspace": _workspace_payload(workspace),
            "persistent": bool((workspace.metadata or {}).get("persistent", True)),
            "session": {
                "id": session.id,
                "updated_at": session.updated_at.isoformat(),
            },
        }
    )


@login_required
@require_GET
def word_addin_workspace_session(request, workspace_id):
    workspace = get_object_or_404(WritingWorkspace, id=workspace_id, user=request.user, kind="word_addin")
    session, _ = WorkspaceResearchSession.objects.get_or_create(workspace=workspace, user=request.user)
    messages = [_message_payload(message) for message in session.messages.order_by("created_at")]
    latest_suggest = (
        session.runs.filter(mode="suggest", status="completed")
        .order_by("-completed_at", "-created_at")
        .first()
    )
    latest_chat = (
        session.runs.filter(mode="chat", status="completed")
        .order_by("-completed_at", "-created_at")
        .first()
    )
    return JsonResponse(
        {
            "workspace": _workspace_payload(workspace),
            "session": {
                "id": session.id,
                "updated_at": session.updated_at.isoformat(),
            },
            "messages": messages,
            "latest_chat_run": _run_payload(latest_chat) if latest_chat else None,
            "latest_suggest_run": _run_payload(latest_suggest) if latest_suggest else None,
        }
    )


@login_required
@require_GET
def word_addin_run_detail(request, run_id):
    run = get_object_or_404(
        WorkspaceResearchRun.objects.select_related("session__workspace"),
        public_id=run_id,
        session__workspace__user=request.user,
    )
    return JsonResponse({"run": _run_payload(run)}, status=_status_code_for_run(run.status))


@login_required
@require_POST
def word_addin_chat_persist(request, workspace_id):
    workspace = get_object_or_404(WritingWorkspace, id=workspace_id, user=request.user, kind="word_addin")
    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    user_message_text = str(data.get("user_message") or "").strip()
    if not user_message_text:
        return JsonResponse({"error": "user_message is required."}, status=400)

    selected_text = str(data.get("selected_text") or "").strip()
    assistant_text = str(data.get("assistant_message") or "").strip()
    status = str(data.get("status") or "completed").strip().lower()
    if status not in {"completed", "failed", "cancelled"}:
        status = "completed"
    citations = data.get("citations") if isinstance(data.get("citations"), list) else []
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    session, _ = WorkspaceResearchSession.objects.get_or_create(workspace=workspace, user=request.user)

    user_message = WorkspaceResearchMessage.objects.create(
        session=session,
        role="user",
        content=user_message_text,
        selection_text=selected_text,
        metadata={"source": "word_addin"},
    )
    assistant_message = None
    result_payload = {}
    error_message = str(data.get("error_message") or "").strip()
    completed_at = None

    if assistant_text:
        assistant_message = WorkspaceResearchMessage.objects.create(
            session=session,
            role="assistant",
            content=assistant_text,
            selection_text=selected_text,
            citations=citations,
            metadata=metadata,
        )
        result_payload = {
            "answer": assistant_text,
            "citations": citations,
        }
        completed_at = timezone.now()

    run = WorkspaceResearchRun.objects.create(
        session=session,
        mode="chat",
        status=status,
        request_payload={
            "message": user_message_text,
            "selected_text": selected_text,
        },
        result_payload=result_payload,
        error_message=error_message,
        bridge_job_id=str(data.get("bridge_job_id") or "").strip(),
        metadata=metadata,
        user_message=user_message,
        assistant_message=assistant_message,
        completed_at=completed_at,
    )
    return JsonResponse({"run": _run_payload(run)}, status=_status_code_for_run(status))


@login_required
@require_POST
def word_addin_suggest_persist(request, workspace_id):
    workspace = get_object_or_404(WritingWorkspace, id=workspace_id, user=request.user, kind="word_addin")
    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    selected_text = str(data.get("selected_text") or "").strip()
    if not selected_text:
        return JsonResponse({"error": "selected_text is required."}, status=400)

    status = str(data.get("status") or "completed").strip().lower()
    if status not in {"completed", "failed", "cancelled"}:
        status = "completed"
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    error_message = str(data.get("error_message") or "").strip()
    session, _ = WorkspaceResearchSession.objects.get_or_create(workspace=workspace, user=request.user)
    completed_at = timezone.now() if status == "completed" else None

    run = WorkspaceResearchRun.objects.create(
        session=session,
        mode="suggest",
        status=status,
        request_payload={
            "selected_text": selected_text,
            "focus_note": str(data.get("focus_note") or "").strip(),
        },
        result_payload=result,
        error_message=error_message,
        bridge_job_id=str(data.get("bridge_job_id") or "").strip(),
        metadata=metadata,
        completed_at=completed_at,
    )
    return JsonResponse({"run": _run_payload(run)}, status=_status_code_for_run(status))


@login_required
@require_POST
def word_addin_citation_format(request):
    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    style = str(data.get("style") or "bare").strip().lower()
    authority = data.get("authority") if isinstance(data.get("authority"), dict) else {}
    citation_text = _citation_text(authority)
    parenthetical = str(data.get("parenthetical") or authority.get("suggested_use") or authority.get("relevance") or "").strip()
    quote_text = str(data.get("quote_text") or authority.get("pinpoint") or "").strip()

    plain_text = citation_text
    if style == "parenthetical" and parenthetical:
        plain_text = f"{citation_text} ({parenthetical})"
    elif style == "quote" and quote_text:
        plain_text = f"\u201c{quote_text}\u201d {citation_text}"

    html = escape(plain_text).replace("\n", "<br>")
    return JsonResponse(
        {
            "plain_text": plain_text,
            "html": html,
        }
    )

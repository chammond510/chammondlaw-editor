import json
import os
from datetime import timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import Document, DocumentType, DocumentVersion
from .export import tiptap_to_docx, tiptap_to_pdf


AUTO_SNAPSHOT_MINUTES = int(os.environ.get("AUTO_SNAPSHOT_MINUTES", "10"))
MAX_SNAPSHOTS_PER_DOC = int(os.environ.get("MAX_SNAPSHOTS_PER_DOC", "100"))


@login_required
def dashboard(request):
    documents = Document.objects.filter(created_by=request.user)
    drafts = documents.filter(status="draft")
    finals = documents.filter(status="final")
    archived = documents.filter(status="archived")
    return render(request, "editor/dashboard.html", {
        "drafts": drafts,
        "finals": finals,
        "archived": archived,
    })


@login_required
def new_document(request):
    types = DocumentType.objects.all()
    # Group by category
    categories = {}
    for dt in types:
        cat = dt.get_category_display()
        categories.setdefault(cat, []).append(dt)
    return render(request, "editor/new_document.html", {"categories": categories})


@login_required
def create_document(request, type_slug):
    doc_type = get_object_or_404(DocumentType, slug=type_slug)
    doc = Document.objects.create(
        title=f"New {doc_type.name}",
        document_type=doc_type,
        content=doc_type.template_content,
        created_by=request.user,
    )
    return redirect("editor", doc_id=doc.id)


@login_required
def editor(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    versions = doc.versions.order_by("-created_at")[:30]
    document_types = DocumentType.objects.all()
    return render(
        request,
        "editor/editor.html",
        {"document": doc, "versions": versions, "document_types": document_types},
    )


@login_required
def delete_document(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    if request.method == "POST":
        doc.delete()
    return redirect("dashboard")


@login_required
@require_POST
def api_save(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    try:
        data = json.loads(request.body)
        new_content = data.get("content", doc.content)
        force_snapshot = bool(data.get("force_snapshot", False))
        snapshot_label = (data.get("snapshot_label") or "").strip()[:100]

        doc.content = new_content
        doc.save()
        _maybe_create_snapshot(doc, new_content, force=force_snapshot, label=snapshot_label)
        return JsonResponse({"status": "ok", "updated_at": doc.updated_at.isoformat()})
    except (json.JSONDecodeError, Exception) as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)


@login_required
@require_POST
def api_update_title(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    try:
        data = json.loads(request.body)
        doc.title = data.get("title", doc.title)[:500]
        doc.save()
        return JsonResponse({"status": "ok"})
    except (json.JSONDecodeError, Exception) as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)


@login_required
def export_docx(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    export_format = "court_brief"
    if doc.document_type:
        export_format = doc.document_type.export_format

    docx_buffer = tiptap_to_docx(doc.content, doc.title, export_format)

    filename = doc.title.replace(" ", "_")[:50] + ".docx"
    response = HttpResponse(
        docx_buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def export_pdf(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    export_format = "court_brief"
    if doc.document_type:
        export_format = doc.document_type.export_format

    pdf_buffer = tiptap_to_pdf(doc.content, doc.title, export_format)

    filename = doc.title.replace(" ", "_")[:50] + ".pdf"
    response = HttpResponse(pdf_buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
@require_GET
def api_versions(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    versions = doc.versions.order_by("-created_at")[:100]
    return JsonResponse(
        {
            "versions": [_version_payload(v, include_preview=True) for v in versions]
        }
    )


@login_required
@require_GET
def api_version_detail(request, doc_id, version_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    version = get_object_or_404(DocumentVersion, id=version_id, document=doc)
    payload = _version_payload(version, include_preview=True)
    payload["content"] = version.content
    payload["full_text"] = _extract_plain_text(version.content, max_chars=50000)
    payload["current_text"] = _extract_plain_text(doc.content, max_chars=50000)
    return JsonResponse(payload)


@login_required
@require_POST
def api_update_version_label(request, doc_id, version_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    version = get_object_or_404(DocumentVersion, id=version_id, document=doc)
    try:
        data = json.loads(request.body or "{}")
        label = (data.get("label") or "").strip()[:100]
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)

    version.label = label or "Snapshot"
    version.save(update_fields=["label"])
    return JsonResponse({"status": "ok", "version": _version_payload(version, include_preview=True)})


@login_required
@require_POST
def api_delete_version(request, doc_id, version_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    version = get_object_or_404(DocumentVersion, id=version_id, document=doc)
    version.delete()
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def api_create_snapshot(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    try:
        data = json.loads(request.body or "{}")
        label = (data.get("label") or "Manual snapshot").strip()[:100]
    except json.JSONDecodeError:
        label = "Manual snapshot"

    version = DocumentVersion.objects.create(document=doc, content=doc.content, label=label)
    _prune_snapshots(doc)
    return JsonResponse(
        {
            "status": "ok",
            "version": {
                "id": version.id,
                "label": version.label,
                "created_at": version.created_at.isoformat(),
            },
        }
    )


@login_required
@require_POST
def api_restore_version(request, doc_id, version_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    version = get_object_or_404(DocumentVersion, id=version_id, document=doc)

    DocumentVersion.objects.create(
        document=doc,
        content=doc.content,
        label=f"Before restore {timezone.now().strftime('%Y-%m-%d %H:%M')}",
    )
    doc.content = version.content
    doc.save(update_fields=["content", "updated_at"])
    _prune_snapshots(doc)

    return JsonResponse(
        {
            "status": "ok",
            "content": version.content,
            "restored_version_id": version.id,
            "updated_at": doc.updated_at.isoformat(),
        }
    )


def _maybe_create_snapshot(doc, content, force=False, label=""):
    last = doc.versions.order_by("-created_at").first()
    if last and last.content == content and not force:
        return None

    snapshot_due = False
    if force or not last:
        snapshot_due = True
    elif timezone.now() - last.created_at >= timedelta(minutes=AUTO_SNAPSHOT_MINUTES):
        snapshot_due = True

    if not snapshot_due:
        return None

    version = DocumentVersion.objects.create(
        document=doc,
        content=content,
        label=label or f"Autosave {timezone.now().strftime('%Y-%m-%d %H:%M')}",
    )
    _prune_snapshots(doc)
    return version


def _prune_snapshots(doc):
    ids_to_keep = list(
        doc.versions.order_by("-created_at").values_list("id", flat=True)[:MAX_SNAPSHOTS_PER_DOC]
    )
    if ids_to_keep:
        doc.versions.exclude(id__in=ids_to_keep).delete()


def _version_payload(version, include_preview=False):
    label = (version.label or "").strip()
    text = _extract_plain_text(version.content, max_chars=20000)
    payload = {
        "id": version.id,
        "label": label or "Snapshot",
        "created_at": version.created_at.isoformat(),
        "is_auto": label.lower().startswith("autosave"),
        "is_restore_point": label.lower().startswith("before restore"),
        "word_count": len([w for w in text.split() if w.strip()]),
        "char_count": len(text),
    }
    if include_preview:
        payload["preview"] = text[:400]
    return payload


def _extract_plain_text(content, max_chars=None):
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

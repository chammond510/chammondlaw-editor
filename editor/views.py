import json
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Document, DocumentType
from .export import tiptap_to_docx


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
    return render(request, "editor/editor.html", {"document": doc})


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
        doc.content = data.get("content", doc.content)
        doc.save()
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

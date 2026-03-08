import os

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from .document_file_service import rank_client_files, serialize_client_file
from .exemplar_service import extract_text_from_file, generate_embedding
from .models import Document, DocumentClientFile


_ALLOWED_CLIENT_FILE_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".rtf"}


def _serialize_client_file_detail(client_file):
    payload = serialize_client_file(client_file)
    payload["extracted_text"] = client_file.extracted_text or ""
    payload["metadata"] = client_file.metadata or {}
    return payload


@login_required
@require_POST
def client_file_upload(request, doc_id):
    document = get_object_or_404(Document, id=doc_id, created_by=request.user)
    uploaded = request.FILES.get("file")
    if not uploaded:
        return JsonResponse({"error": "file is required"}, status=400)

    filename = (uploaded.name or "").strip()
    extension = os.path.splitext(filename)[1].lower()
    if extension not in _ALLOWED_CLIENT_FILE_EXTENSIONS:
        return JsonResponse(
            {"error": "Supported file types are PDF, DOCX, TXT, MD, and RTF."},
            status=400,
        )

    title = (request.POST.get("title") or os.path.splitext(filename)[0] or filename).strip()[:500]
    client_file = DocumentClientFile.objects.create(
        document=document,
        title=title,
        original_file=uploaded,
        metadata={
            "filename": filename,
            "extension": extension,
        },
        uploaded_by=request.user,
    )

    extracted_text = extract_text_from_file(client_file.original_file.path)
    embedding = generate_embedding(extracted_text[:12000]) if extracted_text else []
    metadata = dict(client_file.metadata or {})
    metadata["char_count"] = len(extracted_text)
    metadata["text_extracted"] = bool(extracted_text.strip())
    if not extracted_text.strip():
        metadata["warning"] = "No extractable text was found in this file."
    client_file.extracted_text = extracted_text
    client_file.embedding = embedding
    client_file.metadata = metadata
    client_file.save(update_fields=["extracted_text", "embedding", "metadata", "updated_at"])

    return JsonResponse({"client_file": _serialize_client_file_detail(client_file)})


@login_required
@require_GET
def client_file_list(request, doc_id):
    document = get_object_or_404(Document, id=doc_id, created_by=request.user)
    query = (request.GET.get("q") or "").strip()
    client_files = []
    for client_file in document.client_files.all()[:200]:
        item = _serialize_client_file_detail(client_file)
        item["embedding"] = client_file.embedding or []
        item["extracted_text"] = client_file.extracted_text or ""
        client_files.append(item)

    ranked = rank_client_files(query, client_files)
    results = []
    for item in ranked[:30]:
        item = dict(item)
        item.pop("embedding", None)
        item["extracted_text"] = item.get("extracted_text", "")[:12000]
        results.append(item)
    return JsonResponse({"results": results})


@login_required
@require_GET
def client_file_detail(request, doc_id, file_id):
    document = get_object_or_404(Document, id=doc_id, created_by=request.user)
    client_file = get_object_or_404(DocumentClientFile, id=file_id, document=document)
    return JsonResponse(_serialize_client_file_detail(client_file))

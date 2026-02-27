import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from .exemplar_service import extract_text_from_file, generate_embedding, rank_exemplars
from .models import Document, DocumentType, Exemplar


def _serialize_exemplar(exemplar):
    text = exemplar.extracted_text or ""
    return {
        "id": exemplar.id,
        "title": exemplar.title,
        "document_type": exemplar.document_type.name if exemplar.document_type else "",
        "document_type_id": exemplar.document_type_id,
        "case_type": exemplar.case_type,
        "outcome": exemplar.outcome,
        "date": exemplar.date.isoformat() if exemplar.date else None,
        "tags": exemplar.tags or [],
        "metadata": exemplar.metadata or {},
        "file_url": exemplar.original_file.url if exemplar.original_file else "",
        "snippet": text[:500],
        "updated_at": exemplar.updated_at.isoformat(),
    }


@login_required
@require_POST
def exemplar_upload(request):
    uploaded = request.FILES.get("file")
    if not uploaded:
        return JsonResponse({"error": "file is required"}, status=400)

    doc_type_id = request.POST.get("document_type_id")
    doc_type = None
    if doc_type_id:
        doc_type = DocumentType.objects.filter(id=doc_type_id).first()

    title = (request.POST.get("title") or uploaded.name).strip()[:500]
    case_type = (request.POST.get("case_type") or "").strip()[:100]
    outcome = request.POST.get("outcome") or "unknown"
    tags = [t.strip() for t in (request.POST.get("tags") or "").split(",") if t.strip()]
    metadata_raw = request.POST.get("metadata") or ""
    metadata = {}
    if metadata_raw:
        try:
            metadata = json.loads(metadata_raw)
        except json.JSONDecodeError:
            metadata = {}

    exemplar = Exemplar.objects.create(
        title=title,
        document_type=doc_type,
        case_type=case_type,
        original_file=uploaded,
        outcome=outcome if outcome in dict(Exemplar.OUTCOME_CHOICES) else "unknown",
        tags=tags,
        metadata=metadata,
        created_by=request.user,
    )

    extracted_text = extract_text_from_file(exemplar.original_file.path)
    embedding = generate_embedding(extracted_text[:12000]) if extracted_text else []

    exemplar.extracted_text = extracted_text
    exemplar.embedding = embedding
    exemplar.save(update_fields=["extracted_text", "embedding", "updated_at"])

    return JsonResponse({"exemplar": _serialize_exemplar(exemplar)})


@login_required
@require_GET
def exemplar_search(request):
    query = (request.GET.get("q") or "").strip()
    document_type_id = request.GET.get("document_type_id")
    case_type = (request.GET.get("case_type") or "").strip()

    qs = Exemplar.objects.filter(created_by=request.user)
    if document_type_id:
        qs = qs.filter(document_type_id=document_type_id)
    if case_type:
        qs = qs.filter(case_type__icontains=case_type)

    exemplars = [_serialize_exemplar(ex) for ex in qs[:200]]
    ranked = rank_exemplars(query, exemplars)
    return JsonResponse({"results": ranked[:30]})


@login_required
@require_GET
def exemplar_detail(request, exemplar_id):
    exemplar = get_object_or_404(Exemplar, id=exemplar_id, created_by=request.user)
    data = _serialize_exemplar(exemplar)
    data["extracted_text"] = exemplar.extracted_text
    return JsonResponse(data)


@login_required
@require_GET
def exemplar_suggest_for_document(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, created_by=request.user)
    qs = Exemplar.objects.filter(created_by=request.user)
    if doc.document_type_id:
        qs = qs.filter(document_type_id=doc.document_type_id)
    exemplars = [_serialize_exemplar(ex) for ex in qs[:200]]
    if not exemplars and doc.document_type_id:
        qs = Exemplar.objects.filter(created_by=request.user)[:200]
        exemplars = [_serialize_exemplar(ex) for ex in qs]

    query_text = f"{doc.title}\n{json.dumps(doc.content)[:2000]}"
    ranked = rank_exemplars(query_text, exemplars)
    return JsonResponse({"results": ranked[:10]})

import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from .exemplar_service import extract_text_from_file, generate_embedding, rank_exemplars
from .models import Document, DocumentType, Exemplar
from .style_anchor_service import extract_style_anchor_structure


def _serialize_exemplar(exemplar):
    text = exemplar.extracted_text or ""
    return {
        "id": exemplar.id,
        "title": exemplar.title,
        "document_type": exemplar.document_type.name if exemplar.document_type else "",
        "document_type_id": exemplar.document_type_id,
        "kind": exemplar.kind,
        "style_family": exemplar.style_family,
        "is_active": exemplar.is_active,
        "is_default": exemplar.is_default,
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
    kind = (request.POST.get("kind") or "matter_exemplar").strip()
    if kind not in dict(Exemplar.KIND_CHOICES):
        kind = "matter_exemplar"
    style_family = (request.POST.get("style_family") or "").strip()[:100]
    is_default = (request.POST.get("is_default") or "").strip().lower() in {"1", "true", "yes", "on"}
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
        kind=kind,
        style_family=style_family,
        is_default=is_default,
        case_type=case_type,
        original_file=uploaded,
        outcome=outcome if outcome in dict(Exemplar.OUTCOME_CHOICES) else "unknown",
        tags=tags,
        metadata=metadata,
        created_by=request.user,
    )
    if exemplar.is_default and style_family:
        Exemplar.objects.filter(
            created_by=request.user,
            kind=kind,
            style_family=style_family,
        ).exclude(id=exemplar.id).update(is_default=False)

    extracted_text = extract_text_from_file(exemplar.original_file.path)
    embedding = generate_embedding(extracted_text[:12000]) if extracted_text else []
    if kind == "style_anchor" and exemplar.original_file.name.lower().endswith(".docx"):
        exemplar.metadata = {
            **(exemplar.metadata or {}),
            "style_anchor_structure": extract_style_anchor_structure(exemplar.original_file.path),
        }

    exemplar.extracted_text = extracted_text
    exemplar.embedding = embedding
    exemplar.save(update_fields=["extracted_text", "embedding", "metadata", "updated_at"])

    return JsonResponse({"exemplar": _serialize_exemplar(exemplar)})


@login_required
@require_GET
def exemplar_search(request):
    query = (request.GET.get("q") or "").strip()
    document_type_id = request.GET.get("document_type_id")
    case_type = (request.GET.get("case_type") or "").strip()
    kind = (request.GET.get("kind") or "").strip()
    style_family = (request.GET.get("style_family") or "").strip()

    qs = Exemplar.objects.filter(created_by=request.user, is_active=True)
    if document_type_id:
        qs = qs.filter(document_type_id=document_type_id)
    if case_type:
        qs = qs.filter(case_type__icontains=case_type)
    if kind and kind in dict(Exemplar.KIND_CHOICES):
        qs = qs.filter(kind=kind)
    if style_family:
        qs = qs.filter(style_family=style_family)

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

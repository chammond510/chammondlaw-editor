import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from .biaedge_models import BiaCategory
from . import research_service


@login_required
@require_POST
def suggest_case_law(request):
    try:
        data = json.loads(request.body)
        text = (data.get("text") or "").strip()
        if not text:
            return JsonResponse({"error": "text is required"}, status=400)
        results = research_service.suggest_case_law(text)
        return JsonResponse({"results": results})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@require_GET
def list_categories(request):
    categories = BiaCategory.objects.using("biaedge").filter(enabled=True).order_by("display_order")
    return JsonResponse(
        {
            "categories": [
                {
                    "id": c.id,
                    "name": c.name,
                    "slug": c.slug,
                    "description": c.description,
                }
                for c in categories
            ]
        }
    )


@login_required
@require_GET
def category_cases(request, category_id):
    page = int(request.GET.get("page", 1))
    results = research_service.category_cases(category_id, page=page)
    return JsonResponse({"results": results, "page": page})


@login_required
@require_GET
def category_cases_by_slug(request, slug):
    category = BiaCategory.objects.using("biaedge").filter(slug=slug, enabled=True).first()
    if not category:
        return JsonResponse({"error": "Category not found"}, status=404)
    page = int(request.GET.get("page", 1))
    results = research_service.category_cases(category.id, page=page)
    return JsonResponse(
        {
            "category": {
                "id": category.id,
                "name": category.name,
                "slug": category.slug,
                "description": category.description,
            },
            "results": results,
            "page": page,
        }
    )


@login_required
@require_GET
def case_detail(request, doc_id):
    detail = research_service.case_detail(doc_id)
    if not detail:
        return JsonResponse({"error": "Case not found"}, status=404)
    return JsonResponse(detail)


@login_required
@require_GET
def similar_cases(request, doc_id):
    results = research_service.similar_cases(doc_id)
    return JsonResponse({"results": results})


@login_required
@require_GET
def immcite_status(request, doc_id):
    status = research_service.immcite_status(doc_id)
    return JsonResponse(status)


@login_required
@require_POST
def ask_question(request):
    try:
        data = json.loads(request.body)
        question = (data.get("question") or "").strip()
        if not question:
            return JsonResponse({"error": "question is required"}, status=400)
        result = research_service.ask_question(question)
        return JsonResponse(result)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

from django.contrib import admin
from .models import (
    DocumentType,
    Document,
    DocumentVersion,
    Exemplar,
    DocumentResearchSession,
    DocumentResearchMessage,
)


@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "export_format", "icon", "order"]
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ["title", "document_type", "created_by", "status", "updated_at"]
    list_filter = ["status", "document_type"]


@admin.register(DocumentVersion)
class DocumentVersionAdmin(admin.ModelAdmin):
    list_display = ["document", "label", "created_at"]


@admin.register(Exemplar)
class ExemplarAdmin(admin.ModelAdmin):
    list_display = ["title", "document_type", "case_type", "outcome", "created_by", "updated_at"]
    list_filter = ["outcome", "document_type", "case_type"]
    search_fields = ["title", "case_type", "extracted_text"]


@admin.register(DocumentResearchSession)
class DocumentResearchSessionAdmin(admin.ModelAdmin):
    list_display = ["document", "user", "last_response_id", "updated_at"]
    list_filter = ["updated_at"]
    search_fields = ["document__title", "user__username", "user__email", "last_response_id"]


@admin.register(DocumentResearchMessage)
class DocumentResearchMessageAdmin(admin.ModelAdmin):
    list_display = ["session", "role", "created_at"]
    list_filter = ["role", "created_at"]
    search_fields = ["session__document__title", "content", "selection_text", "response_id"]

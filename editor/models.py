import uuid
from django.db import models
from django.contrib.auth.models import User


class DocumentType(models.Model):
    CATEGORY_CHOICES = [
        ("cover_letter", "Cover Letter"),
        ("brief", "Brief"),
        ("motion", "Motion"),
        ("declaration", "Declaration"),
        ("other", "Other"),
    ]
    EXPORT_FORMAT_CHOICES = [
        ("court_brief", "Court Brief"),
        ("cover_letter", "Cover Letter"),
        ("declaration", "Declaration"),
    ]

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    template_content = models.JSONField(default=dict, blank=True)
    export_format = models.CharField(
        max_length=20, choices=EXPORT_FORMAT_CHOICES, default="court_brief"
    )
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=10, blank=True, default="📄")
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Document(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("final", "Final"),
        ("archived", "Archived"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=500, default="Untitled Document")
    document_type = models.ForeignKey(
        DocumentType, on_delete=models.SET_NULL, null=True, blank=True
    )
    content = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="draft")

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class DocumentVersion(models.Model):
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="versions"
    )
    content = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    label = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.document.title} - {self.label or self.created_at}"


class Exemplar(models.Model):
    OUTCOME_CHOICES = [
        ("approved", "Approved"),
        ("denied", "Denied"),
        ("pending", "Pending"),
        ("unknown", "Unknown"),
    ]

    title = models.CharField(max_length=500)
    document_type = models.ForeignKey(
        DocumentType, on_delete=models.SET_NULL, null=True, blank=True
    )
    case_type = models.CharField(max_length=100, blank=True)
    original_file = models.FileField(upload_to="exemplars/")
    extracted_text = models.TextField(blank=True)
    embedding = models.JSONField(default=list, blank=True)
    outcome = models.CharField(
        max_length=20, choices=OUTCOME_CHOICES, default="unknown"
    )
    date = models.DateField(null=True, blank=True)
    tags = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title

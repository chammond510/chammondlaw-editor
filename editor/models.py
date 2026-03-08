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
    source_docx = models.FileField(upload_to="document_imports/", blank=True)
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
    KIND_CHOICES = [
        ("matter_exemplar", "Matter Exemplar"),
        ("style_anchor", "Style Anchor"),
        ("section_template", "Section Template"),
    ]
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
    kind = models.CharField(
        max_length=30,
        choices=KIND_CHOICES,
        default="matter_exemplar",
    )
    style_family = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
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


class DocumentClientFile(models.Model):
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="client_files",
    )
    title = models.CharField(max_length=500)
    original_file = models.FileField(upload_to="document_client_files/")
    extracted_text = models.TextField(blank=True)
    embedding = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class DocumentResearchSession(models.Model):
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="research_sessions"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    last_response_id = models.CharField(max_length=200, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "user"],
                name="editor_unique_document_research_session",
            )
        ]

    def __str__(self):
        return f"Research session for {self.document} ({self.user})"


class DocumentResearchMessage(models.Model):
    ROLE_CHOICES = [
        ("user", "User"),
        ("assistant", "Assistant"),
    ]

    session = models.ForeignKey(
        DocumentResearchSession, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    selection_text = models.TextField(blank=True)
    response_id = models.CharField(max_length=200, blank=True, default="")
    tool_calls = models.JSONField(default=list, blank=True)
    citations = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.session.document} {self.role} message @ {self.created_at.isoformat()}"


class DocumentResearchRun(models.Model):
    MODE_CHOICES = [
        ("chat", "Chat"),
        ("suggest", "Suggest"),
        ("edit", "Edit"),
    ]
    STATUS_CHOICES = [
        ("queued", "Queued"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    session = models.ForeignKey(
        DocumentResearchSession,
        on_delete=models.CASCADE,
        related_name="runs",
    )
    mode = models.CharField(max_length=20, choices=MODE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="queued")
    stage = models.CharField(max_length=50, blank=True, default="queued")
    request_payload = models.JSONField(default=dict, blank=True)
    result_payload = models.JSONField(default=dict, blank=True)
    response_id = models.CharField(max_length=200, blank=True, default="")
    previous_response_id = models.CharField(max_length=200, blank=True, default="")
    error_message = models.TextField(blank=True)
    response_count = models.PositiveIntegerField(default=0)
    local_function_rounds = models.PositiveIntegerField(default=0)
    tool_calls = models.JSONField(default=list, blank=True)
    citations = models.JSONField(default=list, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    user_message = models.ForeignKey(
        DocumentResearchMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="initiated_runs",
    )
    assistant_message = models.ForeignKey(
        DocumentResearchMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_runs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.session.document} {self.mode} run "
            f"{self.public_id} ({self.status})"
        )

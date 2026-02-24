from django.db import models


class BiaDocument(models.Model):
    source_id = models.IntegerField()
    source_doc_id = models.CharField(max_length=100)
    case_name = models.CharField(max_length=500)
    citation = models.CharField(max_length=200)
    decision_date = models.DateField(null=True)
    deciding_body = models.CharField(max_length=100)
    summary = models.TextField(blank=True)
    case_type = models.CharField(max_length=50)
    court = models.CharField(max_length=20)
    outcome = models.CharField(max_length=50)
    precedential_status = models.CharField(max_length=50)
    cited_by_count = models.IntegerField(default=0)

    class Meta:
        managed = False
        db_table = "documents"


class BiaDocumentText(models.Model):
    document_id = models.IntegerField()
    full_text = models.TextField()
    word_count = models.IntegerField(null=True)

    class Meta:
        managed = False
        db_table = "document_texts"


class BiaHolding(models.Model):
    document_id = models.IntegerField()
    legal_issue = models.CharField(max_length=500)
    rule = models.TextField()
    statutory_basis = models.JSONField(null=True)
    confidence = models.FloatField(null=True)
    sequence = models.IntegerField(default=0)
    is_primary = models.BooleanField(default=False)

    class Meta:
        managed = False
        db_table = "holdings"


class BiaHoldingEmbedding(models.Model):
    holding_id = models.IntegerField()
    model = models.CharField(max_length=100)

    class Meta:
        managed = False
        db_table = "holding_embeddings"


class BiaCategory(models.Model):
    name = models.CharField(max_length=100)
    slug = models.CharField(max_length=50)
    description = models.TextField(blank=True)
    display_order = models.IntegerField(default=0)
    enabled = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "categories"


class BiaDocumentCategory(models.Model):
    document_id = models.IntegerField()
    category_id = models.IntegerField()
    confidence = models.FloatField(null=True)

    class Meta:
        managed = False
        db_table = "document_categories"


class BiaCitationValidity(models.Model):
    document_id = models.IntegerField()
    status = models.CharField(max_length=20)
    status_reason = models.TextField(blank=True)
    positive_citations = models.IntegerField(default=0)
    negative_citations = models.IntegerField(default=0)
    overruling_citations = models.IntegerField(default=0)

    class Meta:
        managed = False
        db_table = "citation_validity"


class BiaHeadnote(models.Model):
    document_id = models.IntegerField()
    sequence = models.IntegerField()
    title = models.CharField(max_length=300)
    text = models.TextField()
    topic_code = models.CharField(max_length=20)
    is_primary = models.BooleanField(default=False)

    class Meta:
        managed = False
        db_table = "headnotes"

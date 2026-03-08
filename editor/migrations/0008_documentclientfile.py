from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("editor", "0007_document_source_docx"),
        migrations.swappable_dependency("auth.User"),
    ]

    operations = [
        migrations.CreateModel(
            name="DocumentClientFile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=500)),
                ("original_file", models.FileField(upload_to="document_client_files/")),
                ("extracted_text", models.TextField(blank=True)),
                ("embedding", models.JSONField(blank=True, default=list)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="client_files", to="editor.document")),
                ("uploaded_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="auth.user")),
            ],
            options={
                "ordering": ["-updated_at"],
            },
        ),
    ]

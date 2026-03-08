from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("editor", "0006_documentresearchrun"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="source_docx",
            field=models.FileField(blank=True, upload_to="document_imports/"),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("editor", "0004_documentresearchsession_documentresearchmessage"),
    ]

    operations = [
        migrations.AddField(
            model_name="exemplar",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="exemplar",
            name="is_default",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="exemplar",
            name="kind",
            field=models.CharField(
                choices=[
                    ("matter_exemplar", "Matter Exemplar"),
                    ("style_anchor", "Style Anchor"),
                    ("section_template", "Section Template"),
                ],
                default="matter_exemplar",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="exemplar",
            name="style_family",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]


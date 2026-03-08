from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("editor", "0008_documentclientfile"),
    ]

    operations = [
        migrations.AlterField(
            model_name="documentresearchrun",
            name="mode",
            field=models.CharField(
                choices=[("chat", "Chat"), ("suggest", "Suggest"), ("edit", "Edit")],
                max_length=20,
            ),
        ),
    ]

from pathlib import Path

from django.contrib.auth.models import User
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from editor.exemplar_service import extract_text_from_file, generate_embedding
from editor.models import Exemplar
from editor.style_anchor_service import (
    USCIS_COVER_LETTER_STYLE_FAMILY,
    extract_style_anchor_structure,
)


class Command(BaseCommand):
    help = "Import exemplar files from the local Exemplars folder into the exemplar bank"

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True, help="Username that should own the imported exemplars")
        parser.add_argument(
            "--source-dir",
            default="Exemplars",
            help="Directory to scan for exemplar files (default: Exemplars)",
        )
        parser.add_argument(
            "--kind",
            default="style_anchor",
            choices=[choice for choice, _ in Exemplar.KIND_CHOICES],
            help="Exemplar kind to assign during import",
        )
        parser.add_argument(
            "--style-family",
            default=USCIS_COVER_LETTER_STYLE_FAMILY,
            help="Style family to assign during import",
        )
        parser.add_argument(
            "--default",
            action="store_true",
            help="Mark imported exemplars as the default for their style family",
        )

    def handle(self, *args, **options):
        username = options["username"].strip()
        source_dir = Path(options["source_dir"]).expanduser()
        kind = options["kind"]
        style_family = options["style_family"].strip()
        mark_default = bool(options["default"])

        user = User.objects.filter(username=username).first()
        if not user:
            raise CommandError(f"User '{username}' does not exist.")
        if not source_dir.exists():
            raise CommandError(f"Source directory not found: {source_dir}")

        files = sorted(
            path
            for path in source_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".pdf", ".docx", ".txt", ".md", ".rtf"}
        )
        if not files:
            raise CommandError(f"No supported exemplar files found in {source_dir}")

        imported = 0
        for file_path in files:
            exemplar, created = Exemplar.objects.get_or_create(
                created_by=user,
                title=file_path.stem,
                kind=kind,
                style_family=style_family,
                defaults={"is_default": mark_default},
            )
            if not created and exemplar.original_file:
                self.stdout.write(f"Skipping existing exemplar: {file_path.name}")
                continue

            with file_path.open("rb") as handle:
                exemplar.original_file.save(file_path.name, File(handle), save=False)

            exemplar.is_active = True
            exemplar.is_default = mark_default
            if exemplar.is_default and style_family:
                Exemplar.objects.filter(
                    created_by=user,
                    kind=kind,
                    style_family=style_family,
                ).exclude(id=exemplar.id).update(is_default=False)
            exemplar.metadata = exemplar.metadata or {}
            if kind == "style_anchor" and file_path.suffix.lower() == ".docx":
                exemplar.metadata["style_anchor_structure"] = extract_style_anchor_structure(str(file_path))

            exemplar.save()

            extracted_text = extract_text_from_file(exemplar.original_file.path)
            exemplar.extracted_text = extracted_text
            exemplar.embedding = generate_embedding(extracted_text[:12000]) if extracted_text else []
            exemplar.save(update_fields=["extracted_text", "embedding", "metadata", "updated_at"])
            imported += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {imported} exemplar(s) from {source_dir} for user '{username}'."
            )
        )

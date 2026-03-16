from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys

from django.conf import settings
from django.utils import timezone
from pypdf import PdfReader

from .export import tiptap_to_docx_with_style_anchor, tiptap_to_docx_with_template, tiptap_to_pdf
from .style_anchor_service import resolve_style_anchor_for_document

try:
    from docx2pdf import convert as docx2pdf_convert
except Exception:  # pragma: no cover - dependency and platform dependent
    docx2pdf_convert = None


PROOF_ROOT = "proof_previews"


@dataclass
class DocumentDocxArtifact:
    filename: str
    export_format: str
    docx_bytes: bytes
    source_kind: str
    source_label: str
    style_anchor_id: int | None = None


class ProofRenderError(RuntimeError):
    pass


class WordRenderBackend:
    name = "word_mac"
    app_env_vars = ("CHLF_WORD_APP_PATH", "MICROSOFT_WORD_APP_PATH")
    app_candidates = (
        "/Applications/Microsoft Word.app",
        "~/Applications/Microsoft Word.app",
    )

    def __init__(self) -> None:
        self.app_path = _first_existing_path(
            _candidate_paths(self.app_env_vars, self.app_candidates)
        )

    def is_available(self) -> bool:
        return bool(docx2pdf_convert) and bool(self.app_path)

    def diagnostic(self) -> dict:
        if self.is_available():
            return {
                "name": self.name,
                "available": True,
                "path": self.app_path,
                "detail": f"Using {self.app_path}.",
            }

        detail_parts: list[str] = []
        if not docx2pdf_convert:
            detail_parts.append("docx2pdf is not installed in the active Python environment.")
        if not self.app_path:
            detail_parts.append(
                "Microsoft Word.app was not found in the standard macOS locations or WORD app env vars."
            )
        return {
            "name": self.name,
            "available": False,
            "path": self.app_path,
            "detail": " ".join(detail_parts) or "Microsoft Word rendering is unavailable.",
        }

    def render_docx_to_pdf(self, input_path: Path, output_path: Path) -> None:
        if not self.is_available():
            raise ProofRenderError("Microsoft Word rendering is unavailable.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-c",
            (
                "from docx2pdf import convert; "
                f"convert({str(input_path)!r}, {str(output_path)!r})"
            ),
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=45,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProofRenderError("Microsoft Word rendering timed out.") from exc
        if result.returncode != 0 or not output_path.exists():
            raise ProofRenderError((result.stderr or result.stdout or "Microsoft Word failed to render the DOCX.").strip())


class SofficeRenderBackend:
    name = "soffice"
    binary_env_vars = ("CHLF_SOFFICE_BINARY", "SOFFICE_BINARY", "LIBREOFFICE_BINARY")
    binary_candidates = (
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/Applications/OpenOffice.app/Contents/MacOS/soffice",
        "/opt/homebrew/bin/soffice",
        "/usr/local/bin/soffice",
        "/usr/bin/soffice",
        "/opt/homebrew/bin/libreoffice",
        "/usr/local/bin/libreoffice",
        "/usr/bin/libreoffice",
    )

    def __init__(self) -> None:
        self.checked_locations = _candidate_paths(
            self.binary_env_vars,
            self.binary_candidates,
            command_names=("soffice", "libreoffice"),
        )
        self.binary = _first_existing_executable(self.checked_locations)

    def is_available(self) -> bool:
        return bool(self.binary)

    def diagnostic(self) -> dict:
        if self.binary:
            return {
                "name": self.name,
                "available": True,
                "path": self.binary,
                "detail": f"Using {self.binary}.",
            }
        checked = ", ".join(self.checked_locations) if self.checked_locations else "no locations"
        return {
            "name": self.name,
            "available": False,
            "path": "",
            "detail": f"LibreOffice/soffice was not found. Checked: {checked}.",
        }

    def render_docx_to_pdf(self, input_path: Path, output_path: Path) -> None:
        if not self.binary:
            raise ProofRenderError("LibreOffice rendering is unavailable.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.binary,
            f"-env:UserInstallation=file:///tmp/lo_profile_{timezone.now().timestamp()}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_path.parent),
            str(input_path),
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        expected_path = output_path.parent / f"{input_path.stem}.pdf"
        if result.returncode != 0 or not expected_path.exists():
            raise ProofRenderError(
                (result.stderr or result.stdout or "LibreOffice failed to render the DOCX.").strip()
            )
        if expected_path != output_path:
            shutil.move(str(expected_path), str(output_path))


class WordRenderService:
    def __init__(self) -> None:
        self.backends = [WordRenderBackend(), SofficeRenderBackend()]

    def backend_status(self) -> list[dict]:
        return [backend.diagnostic() for backend in self.backends]

    def render_docx_to_pdf(self, input_path: Path, output_path: Path) -> str:
        errors: list[str] = []
        for backend in self.backends:
            diagnostic = backend.diagnostic()
            if not diagnostic["available"]:
                errors.append(f'{backend.name}: unavailable ({diagnostic["detail"]})')
                continue
            try:
                backend.render_docx_to_pdf(input_path, output_path)
                return backend.name
            except Exception as exc:  # pragma: no cover - platform dependent
                errors.append(f"{backend.name}: {exc}")
        raise ProofRenderError("; ".join(errors) or "No proof render backend is available.")


def build_document_docx_artifact(document, *, user) -> DocumentDocxArtifact:
    export_format = "court_brief"
    if document.document_type:
        export_format = document.document_type.export_format

    if document.source_docx and document.source_docx.name.lower().endswith(".docx"):
        docx_buffer = tiptap_to_docx_with_template(
            document.content,
            document.title,
            export_format,
            template_path=document.source_docx.path,
            document_metadata=document.metadata,
        )
        source_kind = "source_docx"
        source_label = Path(document.source_docx.name).name
        return DocumentDocxArtifact(
            filename=_safe_docx_filename(document.title),
            export_format=export_format,
            docx_bytes=docx_buffer.getvalue(),
            source_kind=source_kind,
            source_label=source_label,
        )

    style_anchor = resolve_style_anchor_for_document(
        user=user,
        document=document,
        export_format=export_format,
    )
    docx_buffer = tiptap_to_docx_with_style_anchor(
        document.content,
        document.title,
        export_format,
        style_anchor=style_anchor,
        document_metadata=document.metadata,
    )
    return DocumentDocxArtifact(
        filename=_safe_docx_filename(document.title),
        export_format=export_format,
        docx_bytes=docx_buffer.getvalue(),
        source_kind="style_anchor" if style_anchor else "generated",
        source_label=style_anchor.title if style_anchor else "Generated",
        style_anchor_id=style_anchor.exemplar_id if style_anchor else None,
    )


def render_document_proof(document, *, user, force: bool = False) -> dict:
    artifact = build_document_docx_artifact(document, user=user)
    metadata = dict(document.metadata or {})
    metadata.pop("preview_state", None)
    content_hash = hashlib.sha256(
        json.dumps(
            {
                "title": document.title,
                "content": document.content,
                "metadata": metadata,
                "source_docx": document.source_docx.name or "",
                "artifact_source": artifact.source_label,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:16]

    output_dir = Path(settings.MEDIA_ROOT) / PROOF_ROOT / "documents" / str(document.id) / content_hash
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists() and not force:
        return json.loads(manifest_path.read_text())

    output_dir.mkdir(parents=True, exist_ok=True)
    docx_path = output_dir / artifact.filename
    pdf_path = output_dir / f"{Path(artifact.filename).stem}.pdf"
    docx_path.write_bytes(artifact.docx_bytes)

    renderer = WordRenderService()
    backend_name = ""
    manifest_extra = {}
    try:
        backend_name = renderer.render_docx_to_pdf(docx_path, pdf_path)
        manifest_extra["exact_render"] = True
    except ProofRenderError as exc:
        try:
            pdf_buffer = tiptap_to_pdf(
                document.content,
                title=document.title,
                export_format=artifact.export_format,
            )
        except Exception as fallback_exc:
            raise ProofRenderError(f"{exc}; internal_pdf: {fallback_exc}") from fallback_exc
        pdf_path.write_bytes(pdf_buffer.getvalue())
        backend_name = "internal_pdf"
        manifest_extra.update(
            {
                "exact_render": False,
                "notice": "Exact Word rendering was unavailable, so this preview is showing the internal PDF export instead.",
            }
        )
    page_count, page_images = _build_pdf_preview_assets(pdf_path, output_dir / "page")
    manifest = _build_manifest(
        kind="document",
        identifier=str(document.id),
        output_dir=output_dir,
        pdf_path=pdf_path,
        page_count=page_count,
        page_images=page_images,
        backend_name=backend_name,
        source_kind=artifact.source_kind,
        source_label=artifact.source_label,
        content_hash=content_hash,
        filename=artifact.filename,
        style_anchor_id=artifact.style_anchor_id,
        extra=manifest_extra,
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def render_exemplar_preview(exemplar, *, force: bool = False) -> dict:
    source_path = Path(exemplar.original_file.path)
    content_hash = hashlib.sha256(
        json.dumps(
            {
                "path": str(source_path),
                "updated_at": exemplar.updated_at.isoformat(),
                "size": source_path.stat().st_size if source_path.exists() else 0,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]

    output_dir = Path(settings.MEDIA_ROOT) / PROOF_ROOT / "exemplars" / str(exemplar.id) / content_hash
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists() and not force:
        return json.loads(manifest_path.read_text())

    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = source_path.suffix.lower()
    pdf_path = output_dir / f"{source_path.stem}.pdf"
    backend_name = "none"

    if suffix == ".pdf":
        shutil.copy2(source_path, pdf_path)
        backend_name = "native_pdf"
    elif suffix == ".docx":
        backend_name = WordRenderService().render_docx_to_pdf(source_path, pdf_path)
    elif suffix == ".rtf":
        backend_name = SofficeRenderBackend().name
        SofficeRenderBackend().render_docx_to_pdf(source_path, pdf_path)
    else:
        manifest = {
            "kind": "exemplar",
            "id": exemplar.id,
            "title": exemplar.title,
            "preview_available": False,
            "message": f"Preview is not available for {suffix or 'this file type'}.",
            "file_url": _media_url_for(source_path),
            "style_family": exemplar.style_family,
            "updated_at": exemplar.updated_at.isoformat(),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        return manifest

    page_count, page_images = _build_pdf_preview_assets(pdf_path, output_dir / "page")
    manifest = _build_manifest(
        kind="exemplar",
        identifier=str(exemplar.id),
        output_dir=output_dir,
        pdf_path=pdf_path,
        page_count=page_count,
        page_images=page_images,
        backend_name=backend_name,
        source_kind=exemplar.kind,
        source_label=source_path.name,
        content_hash=content_hash,
        filename=source_path.name,
        style_anchor_id=exemplar.id if exemplar.kind == "style_anchor" else None,
        extra={
            "title": exemplar.title,
            "style_family": exemplar.style_family,
            "file_url": exemplar.original_file.url if exemplar.original_file else "",
        },
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def _build_manifest(
    *,
    kind: str,
    identifier: str,
    output_dir: Path,
    pdf_path: Path,
    page_count: int,
    page_images: list[Path],
    backend_name: str,
    source_kind: str,
    source_label: str,
    content_hash: str,
    filename: str,
    style_anchor_id: int | None,
    extra: dict | None = None,
) -> dict:
    manifest = {
        "kind": kind,
        "id": identifier,
        "preview_available": True,
        "hash": content_hash,
        "pdf_url": _media_url_for(pdf_path),
        "page_count": page_count,
        "pages": [
            {
                "index": index + 1,
                "image_url": _media_url_for(path),
            }
            for index, path in enumerate(page_images)
        ],
        "backend": backend_name,
        "generated_at": timezone.now().isoformat(),
        "source_kind": source_kind,
        "source_label": source_label,
        "filename": filename,
        "style_anchor_id": style_anchor_id,
    }
    if extra:
        manifest.update(extra)
    return manifest


def _build_pdf_preview_assets(pdf_path: Path, output_prefix: Path) -> tuple[int, list[Path]]:
    page_count = _pdf_page_count(pdf_path)
    try:
        page_images = _render_pdf_pages(pdf_path, output_prefix)
    except ProofRenderError:
        page_images = []
    if not page_count:
        page_count = len(page_images)
    return page_count, page_images


def _render_pdf_pages(pdf_path: Path, output_prefix: Path) -> list[Path]:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise ProofRenderError("pdftoppm is required to build proof preview thumbnails.")
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    command = [
        pdftoppm,
        "-png",
        str(pdf_path),
        str(output_prefix),
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ProofRenderError((result.stderr or result.stdout or "Unable to render preview images.").strip())
    return sorted(output_prefix.parent.glob(f"{output_prefix.name}-*.png"))


def _media_url_for(path: Path) -> str:
    relative = path.relative_to(settings.MEDIA_ROOT).as_posix()
    return f"{settings.MEDIA_URL}{relative}"


def _safe_docx_filename(title: str) -> str:
    filename = (title or "Document").replace("/", " ").replace("\\", " ").strip()
    filename = "_".join(filename.split())[:80] or "Document"
    return f"{filename}.docx"


def _pdf_page_count(pdf_path: Path) -> int:
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return 0


def _candidate_paths(env_vars: tuple[str, ...], fallback_paths: tuple[str, ...], *, command_names: tuple[str, ...] = ()) -> list[str]:
    candidates: list[str] = []
    for env_var in env_vars:
        value = os.environ.get(env_var)
        if value:
            candidates.append(value)
    for command_name in command_names:
        resolved = shutil.which(command_name)
        if resolved:
            candidates.append(resolved)
    candidates.extend(fallback_paths)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        expanded = str(Path(candidate).expanduser())
        if expanded not in seen:
            deduped.append(expanded)
            seen.add(expanded)
    return deduped


def _first_existing_path(candidates: list[str]) -> str:
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
    return ""


def _first_existing_executable(candidates: list[str]) -> str:
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.exists() and path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return ""

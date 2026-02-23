# Legal Editor

A web-based legal document editor for immigration law practice, built with Django and Tiptap.

## Features

- **Rich text editor** powered by Tiptap (ProseMirror) with toolbar formatting
- **8 document templates** for immigration practice (briefs, cover letters, motions, declarations)
- **Autosave** — content saves automatically every 3 seconds
- **Word export** — download any document as a properly formatted .docx file
- **Format presets** — court briefs (double-spaced, Times New Roman), cover letters (single-spaced), declarations (numbered paragraphs)

## Templates Included

| Template | Type | Description |
|----------|------|-------------|
| I-130 Cover Letter | Cover Letter | Family-based petition |
| I-751 Cover Letter | Cover Letter | Removal of conditions |
| RFE Response | Cover Letter | Response to Request for Evidence |
| Defensive Asylum Brief | Brief | Full merits brief for defensive asylum |
| Bond Brief | Brief | Bond/custody redetermination |
| Hardship Waiver Brief | Brief | I-601/I-601A extreme hardship |
| Motion to Reopen | Motion | MTR for Immigration Court / BIA |
| Client Declaration | Declaration | Sworn client statement |

## Quick Start

```bash
# Clone and setup
cd chammondlaw-editor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Database
python manage.py migrate
python manage.py seed_templates
python manage.py createsuperuser

# Run
python manage.py runserver
```

Visit http://127.0.0.1:8000 and login.

## Deployment (Render)

The included `render.yaml` blueprint will deploy:
- Web service (Django + Gunicorn)
- PostgreSQL database (free tier)

Environment variables needed:
- `SECRET_KEY` (auto-generated)
- `DATABASE_URL` (auto-linked from DB)
- `ALLOWED_HOSTS` (set to your domain)

## Architecture

- **Backend:** Django 4.2 LTS
- **Editor:** Tiptap v2 (loaded via CDN/ESM, no build step)
- **Export:** python-docx for Word, WeasyPrint for PDF (future)
- **Styling:** Tailwind CSS (CDN)
- **Static files:** WhiteNoise

## Roadmap

- [ ] Phase 2: Research database integration (BIA Edge sidebar)
- [ ] Phase 3: Exemplar bank (past briefs searchable by topic)
- [ ] Phase 4: PDF export, document versioning, CRM integration

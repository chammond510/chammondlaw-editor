# Legal Editor

A web-based legal document editor for immigration law practice, built with Django and Tiptap.

## Features

- **Rich text editor** powered by Tiptap (ProseMirror) with toolbar formatting
- **4 starter templates** for Phase 1 (I-130 Cover Letter, Defensive Asylum Brief, Bond Brief, RFE Response)
- **Autosave** — content saves automatically every 3 seconds
- **Word export** — download any document as a properly formatted .docx file
- **Format presets** — court briefs (double-spaced, Times New Roman), cover letters (single-spaced), declarations (numbered paragraphs)

## Templates Included

| Template | Type | Description |
|----------|------|-------------|
| I-130 Cover Letter | Cover Letter | Family-based petition |
| Defensive Asylum Brief | Brief | Full merits brief for defensive asylum |
| Bond Brief | Brief | Bond/custody redetermination |
| RFE Response | Cover Letter | Response to Request for Evidence |

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

- **Backend:** Django 5.2
- **Editor:** Tiptap v2 (loaded via CDN/ESM, no build step)
- **Export:** python-docx for Word, WeasyPrint for PDF (future)
- **Styling:** Tailwind CSS (CDN)
- **Static files:** WhiteNoise

## Roadmap

- [ ] Phase 2: Research database integration (BIA Edge sidebar)
- [ ] Phase 3: Exemplar bank (past briefs searchable by topic)
- [ ] Phase 4: PDF export, document versioning, CRM integration

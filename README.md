# Legal Editor

A web-based legal document editor for immigration law practice, built with Django and Tiptap.

## Features

- **Rich text editor** powered by Tiptap (ProseMirror) with toolbar formatting
- **Template library** seeded from the full legal template set (19 templates; Phase 1-only optional via env flag)
- **Autosave** — content saves automatically every 3 seconds
- **Version snapshots** — autosnapshots + manual restore points
- **Word export** — download any document as a properly formatted .docx file
- **PDF export** — server-side render from Tiptap JSON to print-ready PDF
- **Format presets** — court briefs (double-spaced, Times New Roman), cover letters (single-spaced), declarations (numbered paragraphs)
- **Research sidebar** — suggest case law, ask legal questions with citations, category browser, full case detail, ImmCite status
- **Exemplar bank** — upload/search past work (PDF/DOCX/TXT), semantic ranking, insert excerpts while drafting

## Starter Templates

| Template | Type | Description |
|----------|------|-------------|
| I-130 Cover Letter | Cover Letter | Family-based petition |
| Defensive Asylum Brief | Brief | Full merits brief for defensive asylum |
| Bond Brief | Brief | Bond/custody redetermination |
| RFE Response | Cover Letter | Response to Request for Evidence |

Additional templates (e.g., Motion to Reopen, Client Declaration, I-751, hardship waiver) are included in the seed command when `SEED_PHASE1_ONLY` is not enabled.

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
- `BIAEDGE_DATABASE_URL` (set manually in Render; do not commit)
- `OPENAI_API_KEY` (optional for embeddings/ask; app still works with keyword fallback)

Optional environment variables:
- `SEED_PHASE1_ONLY=true` — seed only the original Phase 1 template subset
- `AUTO_SNAPSHOT_MINUTES=10` — autosnapshot cadence
- `MAX_SNAPSHOTS_PER_DOC=100` — per-document snapshot retention cap

## Architecture

- **Backend:** Django 5.2
- **Editor:** Tiptap v2 (loaded via CDN/ESM, no build step)
- **Export:** python-docx for Word, WeasyPrint for PDF
- **Styling:** Tailwind CSS (CDN)
- **Static files:** WhiteNoise

## Roadmap

- [ ] Improve citation drafting quality and answer formatting controls in `/api/research/ask/`
- [ ] Add richer exemplar metadata workflows (bulk ingest, editable tags/issues/outcomes)
- [ ] CRM matter linkage and single-click open-from-matter flow

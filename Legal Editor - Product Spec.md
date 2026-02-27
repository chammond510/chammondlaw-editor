# Legal Editor — Product Spec

**Status:** Draft for Chris's review
**Date:** 2026-02-22
**Author:** Clawd

---

## Vision

A web-based legal document editor purpose-built for immigration law practice. You open it, choose a document type (e.g., "I-130 Cover Letter" or "Defensive Asylum Brief"), get a structured template, and write — with one-click access to your research database of 200k+ immigration decisions and your own exemplar bank of past briefs and letters.

Think of it as your personal legal writing workbench: templates + AI research + your own past work, all in one place.

---

## Core Features

### 1. Document Editor (Tiptap)

**What:** A rich text editor built on [Tiptap](https://tiptap.dev/) (open source, MIT license, built on ProseMirror).

**Capabilities:**
- Standard formatting: bold, italic, underline, headings, lists, blockquotes
- Legal-specific: footnotes (via [tiptap-footnotes](https://github.com/buttondown/tiptap-footnotes)), numbered paragraphs, page breaks
- Tables (for exhibit lists, evidence charts)
- Slash commands (`/` menu for quick actions)
- Autosave to database (every few seconds, like Google Docs)
- Document history / version snapshots

**Why Tiptap:**
- Fully open source core (MIT) — no licensing costs
- Headless (no opinionated UI) — we design the interface exactly how we want
- Extensible — custom toolbar buttons, slash commands, sidebar panels
- Stores content as JSON — easy to transform for export, search, AI processing
- Large ecosystem of extensions (100+ official, many community)
- Framework-agnostic — works with any frontend

### 2. Template System

**What:** When creating a new document, choose from a library of document types. Each loads a structured template with placeholder sections.

**Initial document types** (based on Chris's practice areas + CRM case types):

#### Cover Letters
| Template | Form | Description |
|----------|------|-------------|
| I-130 Cover Letter | I-130 | Family-based petition |
| I-485 Cover Letter | I-485 | Adjustment of status |
| I-612 Cover Letter | I-612 | J-1 waiver |
| I-601 Cover Letter | I-601 | Unlawful presence waiver |
| I-601A Cover Letter | I-601A | Provisional unlawful presence waiver |
| I-751 Cover Letter | I-751 | Removal of conditions |
| N-400 Cover Letter | N-400 | Naturalization |
| B-2 Cover Letter | B-2 | Visitor visa / consular |

#### Briefs & Legal Arguments
| Template | Context | Description |
|----------|---------|-------------|
| Defensive Asylum Brief | Immigration Court | Full merits brief for defensive asylum |
| Affirmative Asylum Brief | USCIS | I-589 supporting brief |
| Cancellation of Removal Brief | Immigration Court | Non-LPR or LPR cancellation |
| Bond Brief | Immigration Court | Bond hearing brief |
| Motion to Reopen | BIA / Immigration Court | MTR with supporting argument |
| Appeal Brief (BIA) | BIA | Brief on appeal |
| Hardship Waiver Brief | USCIS | Extreme hardship argument |

#### Other
| Template | Description |
|----------|-------------|
| Client Declaration | Template for client's sworn statement |
| Expert Witness Letter | Request template for expert declarations |
| RFE Response | Response to Request for Evidence |
| NOID Response | Response to Notice of Intent to Deny |

**Template structure:**
Each template is stored as Tiptap JSON in the database with:
- Section headers (e.g., "I. STATEMENT OF FACTS", "II. LEGAL STANDARD", "III. ARGUMENT")
- Placeholder text with guidance (e.g., "[Describe the respondent's entry to the United States and immigration history]")
- Pre-filled boilerplate where appropriate (e.g., jurisdictional statements, standard legal frameworks)
- Linked research categories — each template maps to relevant BIA Edge categories for contextual suggestions

### 3. Research Database Integration ("Suggest Case Law")

**What:** A sidebar panel and toolbar button that searches the BIA Edge database and returns relevant case law based on what you're writing.

**How it works:**

1. **Highlight text** in your document (e.g., a paragraph arguing that your client's family constitutes a particular social group)
2. **Click "Suggest Case Law"** (or use slash command `/research`)
3. The highlighted text is sent to the backend, which runs:
   - `holdings-similar` — vector similarity search against 200k+ holdings
   - `search` — full-text search for key terms
   - `immcite-status` — filters out overruled/invalid cases
4. **Results appear in sidebar** with:
   - Case name and citation
   - Relevant holding/summary
   - ImmCite validity status (✅ good law / ⚠️ questioned / ❌ overruled)
   - "Insert Citation" button
5. **Click "Insert Citation"** — drops formatted citation at cursor position

**Additional research features:**
- **Ask a question:** Free-text legal question → RAG pipeline → AI-generated answer with citations (wraps `idb ask`)
- **Browse by category:** Sidebar shows the 71 topic categories from BIA Edge — click one to see leading cases
- **Similar cases:** When viewing a specific case in the sidebar, "Find Similar" button
- **Full text viewer:** Click a case to read the full decision text in a slide-out panel

**API endpoints needed:**

```
POST /api/research/suggest/        — text in, ranked cases out
POST /api/research/ask/            — question in, AI answer + citations out
GET  /api/research/categories/     — list all categories
GET  /api/research/category/{slug}/ — cases in a category
GET  /api/research/case/{id}/      — full case details + text
GET  /api/research/similar/{id}/   — similar cases
GET  /api/research/immcite/{id}/   — validity status
```

### 4. Exemplar Bank

**What:** A searchable library of Chris's past briefs and cover letters, indexed with embeddings for semantic search.

**How it works:**

1. **Ingest:** Upload Word docs / PDFs of past work. System extracts text, generates embeddings, tags by document type and case category.
2. **Search:** When writing a new document, search exemplars by topic or let the system auto-suggest based on current document type + content.
3. **Reference:** View past documents side-by-side while writing. Copy sections, adapt language.
4. **Learn:** Over time, the system learns Chris's writing patterns and preferred phrasings.

**Exemplar metadata:**
- Document type (cover letter, brief, motion, etc.)
- Case type (asylum, cancellation, family-based, etc.)
- Outcome (approved, denied, pending)
- Date
- Key legal issues addressed
- Tags

**Where exemplars come from:**
- Chris uploads a folder of past work (Word docs, PDFs)
- The CRM already has some exemplar templates (`apps/matters/fixtures/exemplars/`)
- Future: documents created in the editor automatically join the exemplar bank (opt-in)

### 5. Export

**What:** Download documents as Word (.docx) or PDF from the editor.

**Implementation:**
- **Word export:** Server-side conversion using `python-docx`. Tiptap stores content as JSON → backend walks the JSON tree → generates .docx with proper formatting (headings, bold/italic, lists, tables, footnotes, page breaks).
- **PDF export:** WeasyPrint (HTML/CSS → PDF). Render the Tiptap content as styled HTML, convert to PDF with proper margins and headers.
- **Template-aware:** Export applies formatting rules based on document type:
  - Court briefs: double-spaced, 12pt Times New Roman, 1-inch margins
  - Cover letters: single-spaced, firm letterhead
  - Declarations: numbered paragraphs

**Why server-side (not Tiptap Pro export):**
- Tiptap's DOCX export is a paid Pro extension
- `python-docx` is free, well-maintained, and gives us full control over formatting
- We can apply legal-specific formatting rules that Tiptap's generic export wouldn't know about

---

## Architecture

### Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Frontend | Tiptap + vanilla JS (or Alpine.js) | Embedded in Django templates |
| Backend | Django 5.x | Same stack as CRM |
| Database | PostgreSQL (Render) | Documents, templates, exemplars |
| Research DB | BIA Edge PostgreSQL | Existing database, read-only access |
| Export | python-docx + WeasyPrint | Server-side document generation |
| Auth | Django auth | Login-protected, same as CRM |
| Hosting | Render | `editor.chammondlaw.com` |
| Search | pgvector + full-text search | Already built in BIA Edge |

### Data Model

```
Document
  - id (UUID)
  - title
  - document_type (FK → DocumentType)
  - matter (FK → Matter, optional — links to CRM)
  - content (JSONField — Tiptap JSON)
  - created_by (FK → User)
  - created_at
  - updated_at
  - status (draft / final / archived)

DocumentType
  - id
  - name ("Defensive Asylum Brief")
  - slug
  - category (cover_letter / brief / motion / declaration / other)
  - template_content (JSONField — Tiptap JSON template)
  - export_format (court_brief / cover_letter / declaration)
  - research_categories (M2M → BIA Edge categories)
  - description

DocumentVersion
  - id
  - document (FK → Document)
  - content (JSONField — snapshot)
  - created_at
  - label (optional — "v1", "before edits", etc.)

Exemplar
  - id
  - title
  - document_type (FK → DocumentType)
  - case_type (asylum / cancellation / family / etc.)
  - original_file (FileField)
  - extracted_text
  - embedding (vector)
  - outcome (approved / denied / pending / unknown)
  - date
  - tags (ArrayField)
  - metadata (JSONField)
```

### System Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    Browser (Chris)                        │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  Tiptap Editor          │  Research Sidebar         │ │
│  │  - Rich text editing    │  - Suggested cases        │ │
│  │  - Slash commands       │  - Ask a question         │ │
│  │  - Template sections    │  - Browse categories      │ │
│  │  - Citation insertion   │  - Exemplar search        │ │
│  │  - Autosave             │  - Full text viewer       │ │
│  └────────────┬────────────┴──────────┬───────────────┘ │
└───────────────┼───────────────────────┼─────────────────┘
                │                       │
                ▼                       ▼
┌───────────────────────┐  ┌────────────────────────────┐
│  Django App (Render)  │  │  Research API              │
│  - Document CRUD      │  │  - /suggest (embeddings)   │
│  - Template mgmt      │  │  - /ask (RAG)              │
│  - Export (docx/pdf)  │  │  - /categories             │
│  - Auth               │  │  - /similar                │
│  - Exemplar mgmt      │  │  - /immcite                │
│  - Autosave API       │  │                            │
│         │             │  │         │                   │
│         ▼             │  │         ▼                   │
│  ┌─────────────┐      │  │  ┌─────────────────┐       │
│  │ Editor DB   │      │  │  │ BIA Edge DB     │       │
│  │ (PostgreSQL)│      │  │  │ (PostgreSQL)    │       │
│  │ docs,       │      │  │  │ 200k+ decisions │       │
│  │ templates,  │      │  │  │ embeddings,     │       │
│  │ exemplars   │      │  │  │ holdings,       │       │
│  └─────────────┘      │  │  │ categories      │       │
└───────────────────────┘  │  └─────────────────┘       │
                           └────────────────────────────┘
```

### Key Decision: Same App or Separate?

**Recommendation: Separate Django app** (`chammondlaw-editor`), separate Render service.

Reasons:
- CRM and editor have different concerns — don't want editor bugs affecting case management
- Can iterate on the editor fast without risking CRM stability
- Connects to BIA Edge DB read-only via `DATABASE_URL` (second database connection)
- Optional: link to CRM matters via API (so you can open the editor from a CRM case page)
- Same auth system (can share Django user table or use SSO later)

---

## Phased Build Plan

### Phase 1: Core Editor + Templates (Week 1-2)
- [ ] Django project scaffold (`chammondlaw-editor`)
- [ ] Tiptap integration (editor page with toolbar)
- [ ] Document model + autosave API
- [ ] 3-4 starter templates (I-130 cover letter, defensive asylum brief, bond brief, RFE response)
- [ ] Document list / dashboard page
- [ ] Basic export to Word (.docx)
- [ ] Deploy to Render
- **Milestone:** You can create a document from a template, write in the editor, save, and download as Word.

### Phase 2: Research Integration (Week 3-4)
- [ ] Research API endpoints (wrapping existing `idb` functionality)
- [ ] Sidebar UI (search results, case viewer)
- [ ] "Suggest Case Law" button — highlight text → get suggestions
- [ ] "Ask a Question" — free-text → RAG answer
- [ ] Citation insertion (click → formatted cite drops into editor)
- [ ] ImmCite validity badges on results
- [ ] Category browser
- **Milestone:** You can write a brief and get AI-powered case law suggestions inline.

### Phase 3: Exemplar Bank (Week 5-6)
- [ ] Exemplar upload + text extraction (Word/PDF → text)
- [ ] Embedding generation for exemplars
- [ ] Exemplar search UI (sidebar tab)
- [ ] Side-by-side exemplar viewer
- [ ] Auto-suggest exemplars based on document type
- [ ] Exemplar metadata tagging (outcome, case type, issues)
- **Milestone:** Your past briefs are searchable and referenceable while writing.

### Phase 4: Polish + Advanced Features (Week 7-8)
- [ ] PDF export with proper legal formatting
- [ ] Remaining templates (full set)
- [ ] Document versioning UI
- [ ] Template-specific formatting presets (court brief vs. cover letter)
- [ ] CRM integration (open editor from a matter, link documents to cases)
- [ ] Slash commands for common legal text (standard objections, legal standards)
- [ ] Keyboard shortcuts
- **Milestone:** Production-ready tool for daily use.

### Future Possibilities (not in initial build)
- AI drafting assistance ("Draft the PSG analysis section based on these facts")
- Collaborative editing (Tiptap supports this via Hocuspocus/Yjs)
- Exhibit list auto-generation (from CRM document uploads)
- Court-specific formatting presets (BIA, 5th Circuit, etc.)
- Integration with court e-filing systems

---

## Technical Details

### Tiptap Setup

The editor will use Tiptap's open-source extensions:

**Included (free/MIT):**
- StarterKit (bold, italic, headings, lists, blockquote, code, etc.)
- Table + TableRow + TableCell + TableHeader
- Underline
- TextAlign
- Placeholder
- CharacterCount
- Typography (smart quotes, em dashes)
- Highlight
- Link
- HorizontalRule
- PageBreak (custom extension)

**Community/custom extensions:**
- Footnotes ([tiptap-footnotes](https://github.com/buttondown/tiptap-footnotes))
- Citation node (custom — renders as formatted legal citation with validity badge)
- Research command (custom — slash command `/research` opens sidebar)
- Numbered paragraphs (custom — for declarations)

### Export Pipeline

```
Tiptap JSON → Python walker → python-docx Document → .docx file

Tiptap JSON → Django template → Styled HTML → WeasyPrint → .pdf file
```

The JSON walker maps Tiptap node types to python-docx elements:
- `heading` → `add_heading(level=n)`
- `paragraph` → `add_paragraph()` with appropriate style
- `bold/italic` → `Run` with `bold=True` / `italic=True`
- `bulletList/orderedList` → `add_paragraph(style='List Bullet/Number')`
- `table` → `add_table()`
- `footnote` → proper Word footnote
- `citation` → formatted text with case name in italics

Format presets:
- **Court Brief:** Times New Roman 12pt, double-spaced, 1" margins, numbered pages
- **Cover Letter:** Times New Roman 12pt, single-spaced, firm letterhead, date block
- **Declaration:** Times New Roman 12pt, double-spaced, numbered paragraphs, signature block

### Research API

The research API is a thin Django REST layer over the existing BIA Edge database. It connects to BIA Edge's PostgreSQL as a second database (read-only).

Key implementation: we reuse the existing query logic from `idb` (the CLI) by importing the Python modules directly or by calling the functions that underlie the CLI commands.

```python
# Example: suggest endpoint
@api_view(['POST'])
def suggest_case_law(request):
    text = request.data['text']
    
    # 1. Semantic search via embeddings
    similar = holdings_similar(query=text, top_k=10, min_sim=0.6)
    
    # 2. Full-text search for key terms
    keywords = extract_keywords(text)  # simple NLP extraction
    text_results = search_documents(query=keywords, limit=10)
    
    # 3. Merge, deduplicate, rank
    results = merge_and_rank(similar, text_results)
    
    # 4. Check ImmCite validity
    for r in results:
        r['validity'] = get_immcite_status(r['id'])
    
    # 5. Filter out overruled cases (flag but still show with warning)
    return Response(results)
```

### Authentication

- Django's built-in auth system
- Login page at `editor.chammondlaw.com/login/`
- Initially just Chris's account
- Future: additional attorney accounts with per-user document access

### Deployment

- Render Web Service (same pattern as CRM)
- `render.yaml` with:
  - Web service (Django + Gunicorn)
  - PostgreSQL database (editor's own DB)
  - Environment variables: `DATABASE_URL`, `BIAEDGE_DATABASE_URL`, `SECRET_KEY`, `DJANGO_SETTINGS_MODULE`
- Static files via WhiteNoise
- Custom domain: `editor.chammondlaw.com`

---

## Cost Estimate

| Item | Cost | Notes |
|------|------|-------|
| Render web service | $7/mo (Starter) | Same tier as CRM |
| Render PostgreSQL | $0 (free tier) | Editor DB is small |
| BIA Edge DB | Already running | Read-only connection |
| Tiptap | $0 | Open source core |
| python-docx | $0 | MIT license |
| WeasyPrint | $0 | BSD license |
| Domain (subdomain) | $0 | Already own chammondlaw.com |
| **Total** | **~$7/mo** | |

---

## Open Questions for Chris

1. **Exemplar bank:** How many past briefs/cover letters do you have? Are they mostly Word docs? Where are they stored?
2. **Template priority:** Which 3-4 document types do you write most often? (Those become Phase 1 templates.)
3. **Formatting requirements:** Any court-specific formatting rules beyond the standard double-spaced/Times New Roman? (e.g., BIA has a 25-page limit for briefs)
4. **CRM link:** Do you want to open the editor directly from a CRM matter, or keep them separate for now?
5. **Repo name:** `chammond510/chammondlaw-editor`? Or something else?

---

## Related
- [[BIA Edge]] — research database
- [[CRM]] — case management system
- [[Website Rebuild]] — firm website

#project #editor #biaedge #legal-tech

# BIA Edge Word Add-in Spec

**Status:** Local pilot implementation
**Date:** 2026-03-24
**Owner:** Chris Hammond

## Summary

Build a Microsoft Word task-pane add-in that brings BIA Edge research, agent chat, authority suggestions, and exemplar lookup directly into Word.

Implementation note: the current pilot uses a Django-served Office.js taskpane plus a localhost Codex bridge so Chris can use the ChatGPT Pro / Codex subscription instead of incurring separate OpenAI API costs for every Word interaction.

This should replace the need to mimic Word inside the browser for drafting workflows. Word becomes the authoring surface. BIA Edge becomes the research and insertion surface.

The add-in should be built as a web-based Office add-in using Office.js, not a Windows-only COM or VSTO plugin.

## Why This Direction

- Word is already the real drafting environment for the final document.
- The current custom editor spends significant effort recreating Word behaviors and fidelity.
- The research layer is already mostly separable from the browser editor.
- The current Django app already exposes reusable JSON endpoints for research, agent chat, case detail, validity, and exemplars.

Relevant existing code:

- Research routes: [editor/urls.py](/Users/chrishammond/chammondlaw-editor/editor/urls.py#L49)
- Research views: [editor/research_views.py](/Users/chrishammond/chammondlaw-editor/editor/research_views.py#L11)
- Research logic: [editor/research_service.py](/Users/chrishammond/chammondlaw-editor/editor/research_service.py#L1)
- Agent routes and lifecycle: [editor/urls.py](/Users/chrishammond/chammondlaw-editor/editor/urls.py#L52), [editor/agent_views.py](/Users/chrishammond/chammondlaw-editor/editor/agent_views.py#L1)
- Exemplar endpoints: [editor/exemplar_views.py](/Users/chrishammond/chammondlaw-editor/editor/exemplar_views.py#L1)
- Current browser editor shell to be avoided for Word-first drafting: [templates/editor/editor.html](/Users/chrishammond/chammondlaw-editor/templates/editor/editor.html#L140)

## Product Goals

- Let Chris research while drafting inside Word without switching to a separate editor.
- Make selection-aware legal research feel immediate.
- Insert useful drafting artifacts back into Word with minimal friction.
- Preserve BIA Edge as the source of truth for cases, validity, statutes, regulations, and policy.
- Reuse as much of the existing Django and BIA Edge backend as possible.

## Non-Goals For V1

- Rebuilding a full document editor inside the add-in.
- Replacing all Word review features such as native track changes, comments, compare, or layout tools.
- Full document management inside the pane.
- Automatic full-brief generation directly into the document body.
- Complex OOXML manipulation as the default insertion path.
- Public AppSource launch in phase 1.

## Primary Users

- Chris drafting briefs, cover letters, motions, declarations, and responses in Word.
- Internal firm users who need BIA Edge research inside the document they are already writing.

## Core User Stories

- As a drafter, I can open a task pane in Word and sign in to BIA Edge.
- As a drafter, when I highlight a paragraph, the pane can suggest the most relevant authorities for that exact passage.
- As a drafter, I can ask a legal question about the current document and get a cited answer.
- As a drafter, I can browse a case, inspect ImmCite status, and read the decision text without leaving Word.
- As a drafter, I can insert a citation, quote, parenthetical, or short drafted snippet at the cursor.
- As a drafter, I can search prior exemplars and insert a selected excerpt into the document.

## V1 User Experience

The add-in appears as a Word task pane named `BIA Edge`.

### Pane Sections

- `Ask`: free-text agent chat about the current document, selected text, or a legal question.
- `Suggest`: structured authority suggestions for the current selection.
- `Authorities`: case/category browsing, case detail, similar cases, ImmCite status.
- `Exemplars`: semantic search over past work and excerpt insertion.

### Primary Actions

- `Use selection`: capture the current highlighted passage as the focal context.
- `Suggest authorities`: run a BIA Edge-first research pass for the selected text.
- `Ask`: send a question plus current selection and document context to the research agent.
- `Insert citation`: insert a formatted citation at the current cursor position.
- `Insert quote`: insert a quoted excerpt plus citation.
- `Insert summary`: insert a one-paragraph synthesis with supporting citations.
- `Insert exemplar excerpt`: insert selected text from a prior exemplar.

### Selection-Aware Behavior

- When the selection changes, the pane updates the displayed focal text.
- The pane should not auto-run expensive research on every cursor move.
- The pane may optionally prefetch lightweight context such as selected text length, citation detection, or last-used authority cards.

## Scope Split

### What We Reuse

- Existing BIA Edge-backed research endpoints and logic.
- Existing agent orchestration patterns where possible.
- Existing exemplar search and detail APIs.
- Existing Django auth and application hosting footprint.

### What We Replace

- The custom browser authoring surface.
- The proof-preview/export loop as the main drafting workflow.
- Editor-specific document autosave and versioning as the default user model.

## Technical Architecture

## Add-in Frontend

- Framework: plain Office.js plus Django-served HTML/JS for the pilot, matching the existing no-build app shape.
- Host: the existing Django app serves the taskpane, manifest, and static add-in assets.
- Runtime: Office.js task pane with Word APIs for selection reads, document settings persistence, and content insertion.
- UI state: Word document settings plus backend workspace/session state for persisted history.

## Backend

- Reuse the existing Django service as the primary application backend.
- Keep BIA Edge database access server-side only.
- For the pilot, rely on the existing Django login/session flow for the taskpane and add-in APIs.
- Run Codex locally through a helper process instead of making hosted OpenAI API calls.

## Local Codex Bridge

- The taskpane calls a localhost bridge process for `Ask` and `Suggest`.
- The bridge shells out to `codex exec`, using the signed-in local Codex session and the configured `biaedge_mcp`.
- Django persists the resulting chat/suggest runs and remains the source of truth for document types, case detail, validity, and exemplars.

## Research Source Order

- First: BIA Edge database authorities and validity data.
- Second: BIA Edge reference materials and policy sources.
- Third: exemplars and local knowledge.
- Fourth: web search only when freshness matters or the user explicitly asks.

## Required New Backend Abstraction

The current agent/session design is document-scoped to a Django `Document` UUID. That does not fit Word documents opened outside this app.

Introduce a backend abstraction called `WritingWorkspace`.

### `WritingWorkspace`

- `id`
- `kind` with values `web_editor` or `word_addin`
- `user`
- `title`
- `document_type`
- `external_document_key`
- `metadata`
- `created_at`
- `updated_at`

### `WorkspaceResearchSession`

- `workspace`
- `user`
- `last_response_id`
- `created_at`
- `updated_at`

### `WorkspaceResearchMessage`

- `session`
- `role`
- `content`
- `selection_text`
- `response_id`
- `tool_calls`
- `citations`
- `metadata`
- `created_at`

This lets the same research runtime support both the current browser editor and Word add-in flows.

## Word Document Identity

For Word documents, the add-in needs a stable per-document key.

Recommended approach:

- On first use, the add-in creates a `workspace_id` on the backend.
- The add-in stores that ID in the Word document as a custom document property or Custom XML Part.
- On reopen, the add-in reads the stored ID and resumes the same workspace/session.
- If the document is read-only or storage fails, the add-in falls back to an ephemeral session.

This is an implementation recommendation, not a current repo feature.

## Document Context Strategy

The add-in should send context to the backend instead of expecting the backend to own the source document.

### Context payload

- document title
- document type when known
- selected text
- nearby paragraph context around the selection
- clipped full-document plain text
- user prompt

### Rules

- Clip aggressively before sending to the backend.
- Prefer selected text over whole-document context for `Suggest`.
- Avoid reading the full document on every selection change.
- Do not persist full document text unless we explicitly decide to add document sync later.

## Authentication

### V1

- Use the current Django-backed BIA Edge login flow in the taskpane itself.
- Keep the Word add-in internal and sideloaded during the pilot.
- Avoid separate token exchange until there is a hosted multi-user deployment need.

### V2

- Add Microsoft 365 / Entra SSO if silent sign-in and firm account linking become important.

Reason for this split:

- It avoids making Microsoft SSO a blocker for the pilot.
- It keeps the first release focused on the legal workflow rather than identity plumbing.

## API Surface

### Reusable endpoints with minor adaptation

- `POST /api/research/suggest/`
- `POST /api/research/ask/`
- `GET /api/research/categories/`
- `GET /api/research/category/{slug}/`
- `GET /api/research/case/{id}/`
- `GET /api/research/similar/{id}/`
- `GET /api/research/immcite/{id}/`
- `GET /api/exemplars/search/`
- `GET /api/exemplars/{id}/`

### New add-in-specific endpoints

- `POST /api/addin/auth/session/`
- `POST /api/addin/workspaces/bootstrap/`
- `GET /api/addin/workspaces/{workspace_id}/session/`
- `POST /api/addin/workspaces/{workspace_id}/chat/`
- `POST /api/addin/workspaces/{workspace_id}/suggest/`
- `POST /api/addin/workspaces/{workspace_id}/insertions/citation-format/`
- `POST /api/addin/workspaces/{workspace_id}/telemetry/`

### Request shape for chat and suggest

- `workspace_id`
- `document_title`
- `document_type`
- `selected_text`
- `selection_context`
- `document_excerpt`
- `message`

## Insertion Strategy

### V1

- Insert plain text or HTML at the current selection/cursor.
- Keep formatting simple and robust.
- Let Word own the final styling.

### Insertable payload types

- bare citation
- citation plus parenthetical
- quoted block plus citation
- one-paragraph authority summary
- exemplar excerpt

### V1.1

- Optionally wrap inserted authorities in content controls tagged with BIA Edge metadata for later refresh or audit.

## Exemplar Behavior

- Reuse the existing exemplar search service.
- Return ranked exemplars plus preview snippets.
- Allow insertion of selected excerpt text, not whole-document clone, from the add-in.
- Keep "open exemplar as full draft" as a web-app workflow, not a Word add-in workflow.

## Security

- The add-in must never connect directly to the BIA Edge database.
- All research requests must go through authenticated backend APIs.
- The backend must authorize by user before exposing exemplars or private data.
- Store only minimal document metadata server-side unless the user explicitly chooses to sync more.
- Log research actions and insertion actions for observability, not document body contents by default.

## Deployment

### Development

- Sideload the add-in locally for Word desktop and Word on the web.
- Use the XML add-in manifest for Mac testing.

### Internal Pilot

- Deploy through the Microsoft 365 admin center integrated apps portal for firm users.
- Keep this as an internal add-in, not a public marketplace listing.

### Later

- Consider AppSource only if there is a real external distribution plan.

## Product Rollout

### Phase 1: MVP

- sign-in
- task pane shell
- selection capture
- suggest authorities
- ask agent
- case detail
- ImmCite status
- citation insertion
- exemplar search and excerpt insertion

### Phase 2: Hardened Pilot

- stable per-document workspace ID
- resumed session history
- category browsing
- similar cases
- better insertion templates
- telemetry and operational dashboards

### Phase 3: Advanced Drafting Support

- content-control tagged insertions
- saved research folders or notebooks
- optional document sync to backend
- matter linkage to CRM
- one-click authority refresh across a document

## Acceptance Criteria For MVP

- A user can open the add-in in Word on Mac and Windows.
- A user can sign in successfully without leaving Word.
- A user can highlight text and receive relevant authority suggestions.
- A user can ask a legal question and receive a cited answer tied to the current document context.
- A user can inspect case details and validity.
- A user can insert a citation or short drafted snippet into the document at the cursor.
- A user can search exemplars and insert an excerpt.
- No direct database credentials are exposed to the client.

## Main Risks

- Authentication inside Office add-in webviews can be finicky.
- Word document identity is straightforward only if we persist a backend workspace ID into the document.
- Rich insertion fidelity can become a sinkhole if we overuse OOXML too early.
- Trying to replicate too much of the current editor would erase the point of the pivot.

## Recommended Product Position

Position this as `BIA Edge for Word`, not `the editor moved into Word`.

That keeps the promise clear:

- draft in real Word
- research in BIA Edge
- insert what matters without switching tools

## References

- Word add-in tutorial: https://learn.microsoft.com/en-gb/office/dev/add-ins/tutorials/word-tutorial
- Dictionary task pane pattern: https://learn.microsoft.com/en-us/office/dev/add-ins/word/dictionary-task-pane-add-ins
- Office add-in platform availability: https://learn.microsoft.com/en-us/javascript/api/requirement-sets?view=excel-js-preview
- Office add-in auth overview: https://learn.microsoft.com/en-us/office/dev/add-ins/develop/overview-authn-authz
- Office add-in requirements: https://learn.microsoft.com/en-us/office/dev/add-ins/concepts/requirements-for-running-office-add-ins
- Office add-in deployment and publishing: https://learn.microsoft.com/en-us/office/dev/add-ins/publish/publish
- Mac sideloading: https://learn.microsoft.com/en-us/office/dev/add-ins/testing/sideload-an-office-add-in-on-mac

# Agent Research Revamp

## Goal

Replace the current heuristic research sidebar with a document-scoped research agent that uses `gpt-5.4` with `reasoning.effort=high` and works directly against BIA Edge, web search, and a firm knowledge layer.

The new agent must support two primary workflows:

1. `Chat`
   The user chats naturally with the agent while drafting. The agent reads the current document, understands the document type, sees user-selected text when available, and can use tools to research, verify, and answer.

2. `Suggest Case Law`
   The agent reads the current draft plus the selected text and returns the most relevant precedential case law, statutes, regulations, and policy for that exact passage.

## Why This Replaces The Old System

The current implementation in `/Users/chrishammond/chammondlaw-editor/editor/research_service.py` is a rules-heavy retrieval pipeline:

- keyword extraction
- phrase extraction
- circuit inference
- separate embedding and PostgreSQL search
- custom reranking rules

That architecture made sense when the model was weaker. With current reasoning models and tool use, the better design is:

- keep tool boundaries deterministic
- let the model decide what to search, in what order, and how to synthesize
- keep source verification and citation discipline explicit in the prompt

## Product Requirements

### Chat

- Lives in the right sidebar beside the editor.
- Reads:
  - document title
  - document type
  - plain-text draft content
  - current user-selected text when available
- Has access to:
  - BIA Edge MCP tools
  - web search
  - knowledge-base search
- Maintains conversation state per user and per document.
- Can answer open-ended drafting and research questions.
- Can be explicitly directed to search the database, web, or knowledge base.
- Must answer with legal citations only when they were verified through tools or are otherwise clearly identified as tentative.

### Suggest Case Law

- Triggered from a dedicated sidebar tab.
- Uses:
  - selected text
  - current draft
  - document type
  - tool access
- Returns structured, ranked suggestions:
  - precedential cases
  - statutes
  - regulations
  - policy / reference materials
- Must explain why each authority matters to the selected passage.
- Must prefer BIA Edge database authorities for final suggested sources.

## Non-Goals

- Rebuilding BIA Edge search inside this repo.
- Recreating the current ranking heuristics with prompt text.
- Streaming UI in phase 1.
- Full citation drafting automation inside the editor body.
- Replacing exemplars as a standalone feature; exemplars become part of the agent's knowledge layer.

## Source Of Truth

### Primary legal database

Use the existing BIA Edge MCP service rather than re-implementing case/statute/reference tools here.

Relevant files in the BIA Edge repo:

- `/Users/chrishammond/biaedge/mcp_server.py`
- `/Users/chrishammond/biaedge/app/pipeline/agent_sdk_qa.py`
- `/Users/chrishammond/biaedge/render.yaml`
- `/Users/chrishammond/biaedge/README.md`

### Tooling already available in BIA Edge

The remote MCP server already exposes read-focused legal research tools including:

- `search_cases`
- `get_document`
- `get_document_text`
- `get_citations`
- `check_validity`
- `get_holdings`
- `search_holdings_by_text`
- `search_statutes`
- `get_statute`
- `search_references`
- `get_reference`
- `get_categories`
- `get_regulatory_updates`

### Current deployment

- Web app: `https://biaedge.onrender.com`
- MCP endpoint: `https://biaedge-mcp.onrender.com/mcp`

## Target Architecture

### 1. Document-scoped research session

Each `(user, document)` pair gets one persisted agent session in Django:

- local message history for UI rendering
- latest OpenAI `response_id` for cheap continuation via `previous_response_id`
- fallback to local transcript rebuild if a prior response cannot be resumed

### 2. Context assembly

For every chat or suggestion request, assemble a context packet containing:

- document title
- document type
- document category / export format when useful
- current draft plain text
- clipped draft excerpt if document is very long
- selected text
- user request

The prompt must tell the agent how to use this context:

- selected text is the immediate focal issue
- draft content defines litigation / filing context
- document type defines style and controlling authorities

### 3. Tool stack

The model will receive a mixed tool stack:

1. `BIA Edge MCP`
   Primary research source for cases, statutes, regulations, policy, and validity.

2. `web_search_preview`
   For current developments, current policy language, or explicit requests to go beyond the database.

3. `Knowledge base`
   Phase 1 knowledge source is the existing exemplar bank via local function tools.
   Optional OpenAI vector-store file search is supported if configured.

### 4. Prompt contracts

The model should not be given vague instructions like "be helpful."
Instead, it needs strict operating rules:

- use BIA Edge first for legal authorities
- prefer precedential authority in main recommendations
- use web search only when freshness matters or the user asks for it
- do not invent citations
- distinguish precedential from unpublished materials
- explain relevance to the exact paragraph or issue
- when results are thin, say so instead of bluffing

### 5. Separate agent modes

#### Chat mode

- conversational output
- tool use allowed
- conversation continuity enabled
- legal citations in normal prose
- broad permissions

#### Suggest mode

- one-shot run
- stricter instructions
- structured JSON output
- BIA Edge-first citation discipline
- returns ranked authority cards with insertion affordances

## Django Data Model Changes

Add two models:

### `DocumentResearchSession`

- `document`
- `user`
- `last_response_id`
- `created_at`
- `updated_at`

Constraints:

- unique `(document, user)`

### `DocumentResearchMessage`

- `session`
- `role` (`user`, `assistant`)
- `content`
- `selection_text`
- `response_id`
- `tool_calls` (JSON)
- `citations` (JSON)
- `metadata` (JSON)
- `created_at`

Purpose:

- render the local sidebar transcript
- preserve continuity even if OpenAI response chaining fails
- keep tool usage inspectable

## Backend Modules

### New module: `editor/document_text.py`

Shared helpers to:

- extract plain text from Tiptap JSON
- clip long documents for prompt use
- compute document context summaries

### New module: `editor/agent_service.py`

Responsibilities:

- OpenAI Responses orchestration
- BIA Edge MCP tool config
- optional knowledge-base tool config
- local function-call loop for exemplars
- chat mode execution
- suggest mode execution
- parsing tool calls and citations
- response fallback when `previous_response_id` is stale

### New module: `editor/agent_views.py`

Responsibilities:

- session bootstrap endpoint
- chat endpoint
- reset endpoint
- suggest endpoint

## API Design

### `GET /api/research/agent/session/<doc_id>/`

Returns:

- session metadata
- existing messages

### `POST /api/research/agent/chat/<doc_id>/`

Request:

- `message`
- `selected_text` (optional)

Response:

- assistant message
- tool call summary
- citations / annotations
- persisted message list if useful

### `POST /api/research/agent/reset/<doc_id>/`

Resets:

- local message history
- cached `last_response_id`

### `POST /api/research/agent/suggest/<doc_id>/`

Request:

- `selected_text`
- `focus_note` (optional)

Response:

- `selection_summary`
- `draft_gap`
- `authorities[]`
- `search_notes`
- `next_questions[]`

## Sidebar UX

Replace the current `Search / Categories / Exemplars` tabs with:

1. `Chat`
   - transcript
   - composer
   - optional "include selection" indicator
   - clear thread button

2. `Suggest`
   - selected text preview
   - optional focus note
   - run button
   - authority cards
   - insert-authority action
   - open case detail action for case results

3. `Exemplars`
   - keep the existing upload/search flow
   - also make exemplars available to the agent as a knowledge tool

## Knowledge Layer

Phase 1 supports two knowledge mechanisms:

1. Existing exemplar bank
   - `search_exemplars`
   - `get_exemplar`

2. Optional OpenAI file search vector store
   - enabled only when a vector store ID is configured

This keeps the implementation useful immediately without blocking on a new ingestion pipeline.

## Environment Variables

### Required for agent research

- `OPENAI_API_KEY`
- `BIAEDGE_MCP_SERVER_URL`

### Optional

- `BIAEDGE_MCP_API_KEY`
- `OPENAI_AGENT_MODEL` default `gpt-5.4`
- `OPENAI_AGENT_REASONING_EFFORT` default `high`
- `OPENAI_AGENT_MAX_TOOL_CALLS`
- `OPENAI_AGENT_MAX_OUTPUT_TOKENS`
- `OPENAI_AGENT_TIMEOUT_SECONDS`
- `OPENAI_AGENT_KNOWLEDGE_VECTOR_STORE_ID`

## Reliability Rules

### Tool routing

- legal authorities: BIA Edge first
- freshness / news / current agency activity: web search
- firm past work / house style / prior arguments: knowledge tools

### Continuity

- use `previous_response_id` for chat turns
- if unavailable, rebuild from local transcript and continue

### Cost / latency control

- cap total tool calls
- clip document context
- do not send the full raw Tiptap JSON
- use structured output only where it buys reliability

## Testing Plan

### Unit tests

- session bootstrap
- chat endpoint persists messages
- stale `previous_response_id` fallback
- suggest endpoint JSON parsing
- exemplar tool execution

### Manual verification

- open document, send a chat message
- ask for a database search
- run suggest on a selected passage
- insert suggested case citation into the editor
- reset thread and confirm clean restart

## Rollout Plan

### Phase 1

- ship chat
- ship suggest
- ship exemplar knowledge tools
- keep existing case detail drawer
- leave legacy research endpoints in place but unused by the new UI

### Phase 2

- streaming responses
- richer authority cards with pin cites and expandable passages
- vector-store knowledge base for firm memos / manuals
- inline "rewrite this paragraph with these authorities" actions

## Acceptance Criteria

The revamp is successful when:

- a user can chat naturally from the document sidebar
- the agent sees the document type, draft, and selected text
- the agent can use BIA Edge MCP directly
- the agent can use web search and knowledge tools
- suggest mode returns structured, practical authority recommendations
- conversation state persists per document
- the old heuristic ranking code is no longer the active path for the sidebar

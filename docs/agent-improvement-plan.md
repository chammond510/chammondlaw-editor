# Agent Improvement Plan

## Purpose

This plan is for stabilizing and simplifying the document-side research/editing agent without removing the capabilities that matter:

- `gpt-5.4` with high reasoning when warranted
- BIA Edge MCP tools
- web search
- exemplar / knowledge retrieval
- client-document retrieval
- chat, suggest, and controlled edit workflows

The goal is not to make the agent smaller. The goal is to make it more reliable, more understandable, and cheaper to operate.

## Current Architecture Summary

The current implementation is centered in [editor/agent_service.py](/Users/chrishammond/chammondlaw-editor/editor/agent_service.py), with request lifecycle in [editor/agent_views.py](/Users/chrishammond/chammondlaw-editor/editor/agent_views.py) and persisted run state in [editor/models.py](/Users/chrishammond/chammondlaw-editor/editor/models.py).

At a high level:

- `agent_chat`, `agent_suggest`, and `agent_edit` create a `DocumentResearchRun`
- the server starts an OpenAI background response
- the client polls `agent_run_status`
- `DocumentResearchAgent.advance_run()` retrieves the current OpenAI response, processes tool calls, tracks budget, handles recovery, and finalizes the run
- chat runs may persist a `DocumentResearchMessage`
- suggest and edit runs persist structured `result_payload`

This works, but too much policy currently lives in one place.

## Main Findings

### 1. Too many concerns are packed into one runtime

`DocumentResearchAgent` currently owns:

- prompt construction
- mode-specific input shaping
- tool registration
- tool dispatch
- background run orchestration
- retry / stale-response recovery
- token-budget enforcement
- quote verification
- JSON repair
- answer stitching
- suggest/edit finalization

This has made the file powerful but difficult to reason about. Fixes have tended to land as local patches because there is not yet a clean separation between orchestration, retrieval, drafting, and finalization.

### 2. The agent is carrying too much raw text through the run

The biggest reliability problem has been context churn, not lack of model capability.

The expensive inputs are:

- document context
- transcript history
- local tool results from client docs and exemplars
- MCP outputs
- previous response chains

Even with the current clipping, the system still relies on several large text surfaces moving through the run. That creates both latency and token-budget risk.

### 3. Prompt control and runtime control are still mixed together

The code is moving in the right direction, but there is still too much behavior split across:

- system prompts
- per-turn requirement blocks
- finalization prompts
- quote-verification prompts
- backend enforcement logic

That makes the behavior harder to predict and easier to accidentally duplicate.

### 4. The tool surface is broader than the runtime discipline around it

The tool stack is valuable, but right now the agent is still too free to:

- search multiple sources before narrowing
- pull more full text than it needs
- perform redundant searches
- arrive at finalization with too much gathered material

This is not a prompt problem alone. It is a tool-policy and state-management problem.

### 5. The run state model is useful but under-structured

`DocumentResearchRun` already stores a lot of good data:

- mode
- stage
- request/result payloads
- tool calls
- citations
- usage
- metadata

But the state machine is still mostly implicit. The runtime advances by inspecting conditions rather than by moving through a small, explicit set of phases with clear invariants.

### 6. Testing coverage is decent for regression, but not yet architecture-proof

The current tests are good at preserving bug fixes. They are less good at proving that the overall design is hard to break.

We need stronger tests around:

- tool-budget exhaustion
- token-budget exhaustion
- repeated polling
- stale-response recovery
- quote-verification flows
- edit runs that touch client docs plus legal research plus drafting

## Design Principles Going Forward

1. Keep capabilities. Simplify execution.
2. Prefer one stable prompt per mode over prompt patches.
3. Prefer structured runtime requirements over prose instructions.
4. Persist compact evidence, not large raw tool payloads.
5. Make finalization a first-class phase, not an emergency fallback.
6. Use deterministic controls for budgets and source-verification rules.
7. Keep the UI explicit: propose, review, apply.

## Target Architecture

### 1. Explicit run phases

Every run should move through a small set of named phases:

- `intake`
- `research`
- `verify`
- `finalize`
- `persist`
- `completed | failed | cancelled`

Each phase should have a narrow contract.

Examples:

- `research` may call tools
- `verify` may only fill missing required source text
- `finalize` may not call tools
- `persist` should not invoke the model at all

This reduces the need for patchy recovery logic because the runtime behavior becomes phase-driven.

### 2. Structured turn requirements

Per-turn control should be a compact structured block that the runtime and prompt both understand.

Example shape:

```json
{
  "requires_exact_quotes": true,
  "required_sources": ["biaedge_case_text", "uscis_policy_manual"],
  "use_client_docs": true,
  "use_exemplars": false,
  "allow_web_search": true,
  "final_output": "edit_json"
}
```

This should replace most ad hoc runtime-note prose.

### 3. Evidence pack instead of raw tool-output accumulation

The runtime should maintain a compact `evidence pack` for each run:

- top authorities with citation and short verified excerpt
- retrieved policy/manual excerpt when needed
- one or two client-document excerpts
- one or two exemplar excerpts
- a short search log

The final drafting step should use the evidence pack, not the full prior response chain.

That means:

- fewer inherited tokens
- fewer continuation failures
- cheaper finalization
- easier debugging

### 4. Tool policy by mode

Each mode should have a clear default tool policy.

`chat`
- broadest tool access
- optimized for quick answer quality
- should stop early once enough support exists

`suggest`
- BIA Edge required
- structured JSON required
- web/exemplar/client-doc usage only for context

`edit`
- first gather only what is needed to support one edit
- then finalize from compact evidence
- do not behave like a full-document drafting agent unless explicitly asked

This should live in code as configuration, not spread across prompt text.

### 5. Separate retrieval from drafting more cleanly

The runtime should treat these as different concerns:

- retrieval: what sources should be consulted
- verification: which source text must be directly retrieved
- drafting: what answer / edit should be produced from verified material

Today those concerns are interleaved. That has produced both token waste and confusing recovery behavior.

### 6. Stronger observability

Each run should record:

- total tokens
- reasoning tokens
- number of OpenAI responses
- number of local function rounds
- MCP tool count
- web-search usage
- client-doc retrieval count
- exemplar retrieval count
- time spent in each phase
- finalization source (`normal`, `budget_recovery`, `failure_recovery`)

This will make future problems diagnosable without log archaeology.

## Recommended Refactor Plan

### Phase 1: Runtime simplification

Goal: reduce complexity without changing visible features.

Changes:

- formalize run phases in `DocumentResearchRun.stage`
- centralize all budget checks in one place
- centralize finalization in one place
- centralize structured turn requirements generation
- remove duplicated prompt-side instructions that are already enforced in code

Acceptance criteria:

- fewer special-case branches in `advance_run()`
- no separate prompt wording for the same rule across multiple recovery paths
- budget failures always converge on one predictable outcome

### Phase 2: Evidence-pack architecture

Goal: stop passing oversized raw content through the model lifecycle.

Changes:

- store compact evidence items as first-class run metadata
- clip and normalize all tool outputs into evidence items
- make finalization consume evidence items only
- stop relying on inherited `previous_response_id` chains for expensive final turns

Acceptance criteria:

- finalization input size is small and predictable
- token usage becomes less sensitive to client-doc or exemplar size
- over-budget failures become rare

### Phase 3: Tool-policy cleanup

Goal: preserve capabilities while reducing tool thrash.

Changes:

- define mode-specific tool budgets and retrieval expectations in code
- tighten when `get_document_text`, `get_reference`, `get_client_document`, and `get_exemplar` are used
- add source-specific excerpt targets where possible
- reduce duplicate searches and duplicate retrievals within a run

Acceptance criteria:

- fewer tool calls per successful run
- fewer repeated searches for the same issue
- better consistency between chat, suggest, and edit behavior

### Phase 4: Editing workflow hardening

Goal: make in-document editing more useful and predictable.

Changes:

- preserve the current propose/apply model
- improve operation selection logic
- optionally support section-level redlines later
- ensure edit runs remain narrow by default

Acceptance criteria:

- edit proposals are consistently one concrete operation
- fewer edit runs spill into broad drafting behavior
- applying edits remains reversible and snapshot-backed

### Phase 5: Test and monitoring hardening

Goal: catch architecture regressions earlier.

Changes:

- add state-machine tests for each phase transition
- add tests for budget-recovery finalization
- add tests for quote-verification plus edit flows
- add production-like telemetry logging for run outcomes and budgets

Acceptance criteria:

- common failure classes are covered by tests
- production failures can be categorized quickly from structured data

## What Not To Do

- Do not remove web search just to make the system simpler.
- Do not keep solving failures by adding more prompt paragraphs.
- Do not let finalization inherit huge raw histories by default.
- Do not turn the agent into a silent auto-editor.
- Do not keep expanding `agent_service.py` without introducing clearer internal boundaries.

## Immediate Next Steps

1. Stabilize the just-deployed budget fix.
2. Refactor the run lifecycle into explicit phases.
3. Introduce a first-class evidence-pack representation.
4. Trim duplicated prompt/runtime rules while keeping structured turn requirements.
5. Add observability for per-run cost and phase timing.

## Success Definition

The agent will be in the right place when:

- chat, suggest, and edit all remain fully capable
- token overruns are uncommon
- failures degrade gracefully instead of collapsing into generic errors
- prompts are shorter and more stable
- the runtime is easier to understand than it is today
- future fixes are mostly architectural improvements, not emergency patches

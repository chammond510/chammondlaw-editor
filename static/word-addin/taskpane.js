import {
    DOCUMENT_STATE_KEY,
    DEFAULT_BRIDGE_URL,
    clipDocumentText,
    coerceWorkspaceState,
    formatBridgeError,
    normalizeBridgeUrl,
} from "./helpers.js";

const state = {
    bridgeUrl: normalizeBridgeUrl(document.body.dataset.bridgeUrl || DEFAULT_BRIDGE_URL),
    workspace: null,
    session: null,
    messages: [],
    latestSuggestRun: null,
    documentTypes: [],
    selectionText: "",
    bridgeHealthy: false,
};

const elements = {
    bridgeUrl: document.getElementById("bridge-url"),
    bridgeSave: document.getElementById("bridge-save"),
    bridgeCheck: document.getElementById("bridge-check"),
    bridgeStatus: document.getElementById("bridge-status"),
    documentTypeSelect: document.getElementById("document-type-select"),
    documentTitle: document.getElementById("document-title"),
    workspaceSync: document.getElementById("workspace-sync"),
    workspaceStatus: document.getElementById("workspace-status"),
    selectionRefresh: document.getElementById("selection-refresh"),
    selectionPreview: document.getElementById("selection-preview"),
    askMessage: document.getElementById("ask-message"),
    askRun: document.getElementById("ask-run"),
    askStatus: document.getElementById("ask-status"),
    chatThread: document.getElementById("chat-thread"),
    suggestNote: document.getElementById("suggest-note"),
    suggestRun: document.getElementById("suggest-run"),
    suggestStatus: document.getElementById("suggest-status"),
    suggestResults: document.getElementById("suggest-results"),
    categoriesLoad: document.getElementById("categories-load"),
    categoryList: document.getElementById("category-list"),
    categoryResults: document.getElementById("category-results"),
    exemplarQuery: document.getElementById("exemplar-query"),
    exemplarSearch: document.getElementById("exemplar-search"),
    exemplarStatus: document.getElementById("exemplar-status"),
    exemplarResults: document.getElementById("exemplar-results"),
    tabs: Array.from(document.querySelectorAll(".tab")),
    panes: Array.from(document.querySelectorAll(".pane")),
};

function getCsrfToken() {
    const match = document.cookie.match(/csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
}

async function apiFetch(url, options = {}) {
    const request = {
        credentials: "same-origin",
        headers: {
            "Content-Type": "application/json",
            ...options.headers,
        },
        ...options,
    };
    if (request.method && request.method.toUpperCase() !== "GET") {
        request.headers["X-CSRFToken"] = getCsrfToken();
    }
    const response = await fetch(url, request);
    const payload = await readJsonResponse(response, url);
    if (!response.ok) {
        throw new Error(payload.error || `Request failed with status ${response.status}.`);
    }
    return payload;
}

async function readJsonResponse(response, url) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
        return response.json().catch(() => ({}));
    }
    const body = await response.text().catch(() => "");
    if (response.redirected || response.url.includes("/login/") || body.includes('name="username"')) {
        throw new Error("Your BIA Edge session expired. Sign in again in the add-in and retry.");
    }
    throw new Error(`Expected JSON from ${url}.`);
}

function setStatus(element, text, variant = "") {
    element.textContent = text;
    element.classList.remove("status-good", "status-bad");
    if (variant === "good") {
        element.classList.add("status-good");
    } else if (variant === "bad") {
        element.classList.add("status-bad");
    }
}

function switchTab(name) {
    elements.tabs.forEach((tab) => {
        tab.classList.toggle("is-active", tab.dataset.tab === name);
    });
    elements.panes.forEach((pane) => {
        pane.classList.toggle("is-active", pane.dataset.pane === name);
    });
}

function renderThread() {
    elements.chatThread.innerHTML = "";
    state.messages.forEach((message) => {
        const item = document.createElement("article");
        item.className = "thread-item";
        item.dataset.role = message.role;
        item.innerHTML = `
            <div class="thread-meta">${message.role === "assistant" ? "BIA Edge" : "You"} • ${new Date(message.created_at).toLocaleString()}</div>
            <div>${escapeHtml(message.content)}</div>
        `;
        elements.chatThread.appendChild(item);
    });
}

function renderSuggestResults(authorities = []) {
    elements.suggestResults.innerHTML = "";
    if (!authorities.length) {
        elements.suggestResults.innerHTML = '<div class="card">No authorities yet.</div>';
        return;
    }
    authorities.forEach((authority) => {
        const card = document.createElement("article");
        card.className = "card";
        card.innerHTML = `
            <div class="card-meta">${escapeHtml(authority.kind || "authority")} • ${escapeHtml(authority.validity_status || "unknown")}</div>
            <div class="card-title">${escapeHtml(authority.title || authority.citation || "Authority")}</div>
            <div>${escapeHtml(authority.citation || "")}</div>
            <p>${escapeHtml(authority.relevance || "")}</p>
            <p>${escapeHtml(authority.pinpoint || "")}</p>
            <div class="card-actions">
                <button type="button" data-action="insert-cite">Insert Cite</button>
                <button type="button" data-action="insert-parenthetical">Insert Cite + Parenthetical</button>
                <button type="button" data-action="insert-quote">Insert Quote</button>
            </div>
        `;
        card.querySelector('[data-action="insert-cite"]').addEventListener("click", () => insertAuthority(authority, "bare"));
        card.querySelector('[data-action="insert-parenthetical"]').addEventListener("click", () => insertAuthority(authority, "parenthetical"));
        card.querySelector('[data-action="insert-quote"]').addEventListener("click", () => insertAuthority(authority, "quote"));
        elements.suggestResults.appendChild(card);
    });
}

function renderCategoryCards(results = []) {
    elements.categoryResults.innerHTML = "";
    if (!results.length) {
        elements.categoryResults.innerHTML = '<div class="card">No category results yet.</div>';
        return;
    }
    results.forEach((item) => {
        const card = document.createElement("article");
        card.className = "card";
        card.innerHTML = `
            <div class="card-title">${escapeHtml(item.case_name || item.title || "Authority")}</div>
            <div class="card-meta">${escapeHtml(item.citation || "")}</div>
            <p>${escapeHtml(item.summary || item.relevance || "")}</p>
            <div class="card-actions">
                <button type="button" data-action="view">View</button>
                <button type="button" data-action="insert">Insert Cite</button>
            </div>
        `;
        card.querySelector('[data-action="view"]').addEventListener("click", () => showAuthorityDetail(item));
        card.querySelector('[data-action="insert"]').addEventListener("click", () => insertAuthority(item, "bare"));
        elements.categoryResults.appendChild(card);
    });
}

function renderExemplars(results = []) {
    elements.exemplarResults.innerHTML = "";
    if (!results.length) {
        elements.exemplarResults.innerHTML = '<div class="card">No exemplar results yet.</div>';
        return;
    }
    results.forEach((item) => {
        const card = document.createElement("article");
        card.className = "card";
        card.innerHTML = `
            <div class="card-title">${escapeHtml(item.title || "Exemplar")}</div>
            <div class="card-meta">${escapeHtml(item.document_type || "")}</div>
            <p>${escapeHtml(item.snippet || "")}</p>
            <div class="card-actions">
                <button type="button" data-action="insert">Insert Excerpt</button>
            </div>
        `;
        card.querySelector('[data-action="insert"]').addEventListener("click", () => insertExemplar(item.id));
        elements.exemplarResults.appendChild(card);
    });
}

function escapeHtml(value) {
    return String(value || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}

async function getDocumentState() {
    if (!window.Office?.context?.document?.settings) {
        return coerceWorkspaceState(JSON.parse(window.localStorage.getItem(DOCUMENT_STATE_KEY) || "{}"));
    }
    const saved = Office.context.document.settings.get(DOCUMENT_STATE_KEY);
    return coerceWorkspaceState(saved);
}

async function saveDocumentState(nextState) {
    const normalized = coerceWorkspaceState(nextState);
    window.localStorage.setItem(DOCUMENT_STATE_KEY, JSON.stringify(normalized));
    if (!window.Office?.context?.document?.settings) {
        return normalized;
    }
    return new Promise((resolve, reject) => {
        Office.context.document.settings.set(DOCUMENT_STATE_KEY, normalized);
        Office.context.document.settings.saveAsync((result) => {
            if (result.status === Office.AsyncResultStatus.Succeeded) {
                resolve(normalized);
            } else {
                reject(new Error(result.error?.message || "Unable to save document settings."));
            }
        });
    });
}

async function detectDocumentTitle() {
    const stateValue = await getDocumentState();
    if (stateValue.documentTitle) {
        return stateValue.documentTitle;
    }
    const url = window.Office?.context?.document?.url || "";
    if (url) {
        const filename = decodeURIComponent(url.split("/").pop() || "").replace(/\.docx$/i, "");
        if (filename) {
            return filename;
        }
    }
    return "Untitled Word Document";
}

async function loadDocumentTypes() {
    const payload = await apiFetch("/api/word-addin/document-types/");
    state.documentTypes = payload.document_types || [];
    if (!state.documentTypes.length) {
        elements.documentTypeSelect.innerHTML = '<option value="">No document types configured</option>';
        return;
    }
    elements.documentTypeSelect.innerHTML = state.documentTypes
        .map((item) => `<option value="${escapeHtml(item.slug)}">${escapeHtml(item.name)}</option>`)
        .join("");
    const documentState = await getDocumentState();
    if (documentState.documentTypeSlug) {
        elements.documentTypeSelect.value = documentState.documentTypeSlug;
    }
}

async function bootstrapWorkspace() {
    const documentState = await getDocumentState();
    const payload = await apiFetch("/api/word-addin/workspaces/bootstrap/", {
        method: "POST",
        body: JSON.stringify({
            workspace_id: documentState.workspaceId || "",
            document_title: elements.documentTitle.value.trim(),
            document_type_slug: elements.documentTypeSelect.value,
            external_document_key: window.Office?.context?.document?.url || "",
            persistent: true,
        }),
    });
    state.workspace = payload.workspace;
    state.session = payload.session;
    await saveDocumentState({
        workspaceId: payload.workspace.id,
        documentTypeSlug: payload.workspace.document_type?.slug || elements.documentTypeSelect.value,
        documentTitle: payload.workspace.title,
        persistent: payload.persistent,
    });
    setStatus(elements.workspaceStatus, `Workspace synced: ${payload.workspace.title}`, "good");
    await loadWorkspaceSession();
}

async function loadWorkspaceSession() {
    if (!state.workspace?.id) {
        return;
    }
    const payload = await apiFetch(`/api/word-addin/workspaces/${state.workspace.id}/session/`);
    state.workspace = payload.workspace;
    state.session = payload.session;
    state.messages = payload.messages || [];
    state.latestSuggestRun = payload.latest_suggest_run;
    elements.documentTitle.value = payload.workspace.title || elements.documentTitle.value;
    if (payload.workspace.document_type?.slug) {
        elements.documentTypeSelect.value = payload.workspace.document_type.slug;
    }
    renderThread();
    if (state.latestSuggestRun?.result?.authorities) {
        renderSuggestResults(state.latestSuggestRun.result.authorities);
    } else {
        renderSuggestResults([]);
    }
}

async function checkBridge() {
    try {
        const response = await fetch(`${state.bridgeUrl}/health`);
        const payload = await response.json();
        if (!response.ok) {
            throw new Error(payload.error || "Bridge unhealthy.");
        }
        state.bridgeHealthy = Boolean(payload.ok);
        setStatus(
            elements.bridgeStatus,
            payload.ok
                ? `Bridge ready • ${payload.login_status || "unknown login"} • model ${payload.model || "default"}`
                : (payload.error || "Bridge unavailable."),
            payload.ok ? "good" : "bad"
        );
    } catch (error) {
        state.bridgeHealthy = false;
        setStatus(
            elements.bridgeStatus,
            `${formatBridgeError(error)} Run "python scripts/setup_word_addin_local_tls.py" once, then start "python scripts/word_addin_codex_bridge.py" with the generated cert and key.`,
            "bad"
        );
    }
}

async function refreshSelection() {
    try {
        const selectionText = await readSelectionText();
        state.selectionText = selectionText;
        elements.selectionPreview.textContent = selectionText || "No selection loaded.";
    } catch (error) {
        elements.selectionPreview.textContent = formatBridgeError(error);
    }
}

async function readSelectionText() {
    if (!window.Word?.run) {
        return new Promise((resolve, reject) => {
            if (!window.Office?.context?.document) {
                resolve("");
                return;
            }
            Office.context.document.getSelectedDataAsync(Office.CoercionType.Text, (result) => {
                if (result.status === Office.AsyncResultStatus.Succeeded) {
                    resolve(String(result.value || "").trim());
                } else {
                    reject(new Error(result.error?.message || "Unable to read selection."));
                }
            });
        });
    }
    return Word.run(async (context) => {
        const range = context.document.getSelection();
        range.load("text");
        await context.sync();
        return String(range.text || "").trim();
    });
}

async function readBodyText() {
    if (!window.Word?.run) {
        return "";
    }
    return Word.run(async (context) => {
        const body = context.document.body;
        body.load("text");
        await context.sync();
        return String(body.text || "").trim();
    });
}

async function insertHtml(html, fallbackText) {
    if (!window.Office?.context?.document) {
        throw new Error("Office document context is not available.");
    }
    return new Promise((resolve, reject) => {
        Office.context.document.setSelectedDataAsync(
            html,
            { coercionType: Office.CoercionType.Html },
            (result) => {
                if (result.status === Office.AsyncResultStatus.Succeeded) {
                    resolve();
                    return;
                }
                Office.context.document.setSelectedDataAsync(
                    fallbackText,
                    { coercionType: Office.CoercionType.Text },
                    (fallback) => {
                        if (fallback.status === Office.AsyncResultStatus.Succeeded) {
                            resolve();
                        } else {
                            reject(new Error(fallback.error?.message || "Unable to insert text."));
                        }
                    }
                );
            }
        );
    });
}

async function runBridge(mode) {
    if (!state.workspace?.id) {
        await bootstrapWorkspace();
    }
    await refreshSelection();
    const documentExcerpt = clipDocumentText(await readBodyText());
    const payload = {
        document_title: elements.documentTitle.value.trim(),
        document_type_slug: elements.documentTypeSelect.value,
        selected_text: state.selectionText,
        document_excerpt: documentExcerpt,
        transcript: state.messages.slice(-8).map((item) => ({
            role: item.role,
            content: item.content,
        })),
    };
    if (mode === "chat") {
        payload.message = elements.askMessage.value.trim();
        if (!payload.message) {
            throw new Error("Enter a question first.");
        }
    } else {
        if (!payload.selected_text) {
            throw new Error("Select some text in Word first.");
        }
        payload.focus_note = elements.suggestNote.value.trim();
    }

    const response = await fetch(`${state.bridgeUrl}/v1/${mode === "chat" ? "chat" : "suggest"}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(body.error || `Bridge ${mode} request failed.`);
    }
    return pollBridgeJob(body.job_id, mode, payload);
}

async function pollBridgeJob(jobId, mode, originalPayload) {
    const statusTarget = mode === "chat" ? elements.askStatus : elements.suggestStatus;
    setStatus(statusTarget, "Running Codex bridge...");
    for (;;) {
        const response = await fetch(`${state.bridgeUrl}/v1/jobs/${jobId}`);
        const payload = await response.json();
        if (payload.status === "completed") {
            if (mode === "chat") {
                await persistChatResult(jobId, originalPayload, payload.result);
            } else {
                await persistSuggestResult(jobId, originalPayload, payload.result);
            }
            return payload.result;
        }
        if (payload.status === "failed") {
            throw new Error(payload.error || "Codex bridge failed.");
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
    }
}

async function persistChatResult(jobId, originalPayload, result) {
    const response = await apiFetch(`/api/word-addin/workspaces/${state.workspace.id}/chat/`, {
        method: "POST",
        body: JSON.stringify({
            user_message: originalPayload.message,
            assistant_message: result.answer,
            selected_text: originalPayload.selected_text,
            citations: result.citations || [],
            bridge_job_id: jobId,
            metadata: {
                source: "codex_bridge",
                model: result.model || "",
            },
            status: "completed",
        }),
    });
    await loadWorkspaceSession();
    elements.askMessage.value = "";
    setStatus(elements.askStatus, `Saved run ${response.run.id}`, "good");
}

async function persistSuggestResult(jobId, originalPayload, result) {
    const response = await apiFetch(`/api/word-addin/workspaces/${state.workspace.id}/suggest/`, {
        method: "POST",
        body: JSON.stringify({
            selected_text: originalPayload.selected_text,
            focus_note: originalPayload.focus_note || "",
            result,
            bridge_job_id: jobId,
            metadata: {
                source: "codex_bridge",
                model: result.model || "",
            },
            status: "completed",
        }),
    });
    await loadWorkspaceSession();
    elements.suggestNote.value = "";
    setStatus(elements.suggestStatus, `Saved run ${response.run.id}`, "good");
}

async function insertAuthority(authority, style) {
    const payload = await apiFetch("/api/word-addin/citation-format/", {
        method: "POST",
        body: JSON.stringify({
            style,
            authority,
        }),
    });
    await insertHtml(payload.html, payload.plain_text);
}

async function insertExemplar(exemplarId) {
    const payload = await apiFetch(`/api/exemplars/${exemplarId}/`);
    const excerpt = String(payload.extracted_text || payload.snippet || "").trim();
    if (!excerpt) {
        throw new Error("The exemplar has no extracted text.");
    }
    await insertHtml(escapeHtml(excerpt).replace(/\n/g, "<br>"), excerpt);
    setStatus(elements.exemplarStatus, `Inserted excerpt from ${payload.title}`, "good");
}

async function showAuthorityDetail(item) {
    const docId = item.document_id || item.id;
    if (!docId) {
        return;
    }
    const detail = await apiFetch(`/api/research/case/${docId}/`);
    const status = await apiFetch(`/api/research/immcite/${docId}/`);
    const authority = {
        title: detail.case_name || detail.title || item.case_name || item.title,
        citation: detail.citation || item.citation || "",
        relevance: detail.summary || item.summary || "",
        validity_status: status.status || item.validity_status || "unknown",
    };
    renderCategoryCards([authority]);
}

async function loadCategories() {
    const payload = await apiFetch("/api/research/categories/");
    elements.categoryList.innerHTML = "";
    (payload.categories || []).forEach((category) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "pill";
        button.textContent = category.name;
        button.addEventListener("click", async () => {
            const cases = await apiFetch(`/api/research/category/${category.slug}/`);
            renderCategoryCards(cases.results || []);
        });
        elements.categoryList.appendChild(button);
    });
}

async function searchExemplars() {
    const query = elements.exemplarQuery.value.trim();
    const type = state.documentTypes.find((item) => item.slug === elements.documentTypeSelect.value);
    const params = new URLSearchParams();
    if (query) {
        params.set("q", query);
    }
    if (type?.id) {
        params.set("document_type_id", String(type.id));
    }
    const payload = await apiFetch(`/api/exemplars/search/?${params.toString()}`);
    renderExemplars(payload.results || []);
    setStatus(elements.exemplarStatus, `Loaded ${payload.results?.length || 0} exemplars.`, "good");
}

function bindEvents() {
    elements.tabs.forEach((tab) => {
        tab.addEventListener("click", () => switchTab(tab.dataset.tab));
    });
    elements.bridgeSave.addEventListener("click", async () => {
        state.bridgeUrl = normalizeBridgeUrl(elements.bridgeUrl.value);
        window.localStorage.setItem("biaedge.wordAddinBridgeUrl", state.bridgeUrl);
        elements.bridgeUrl.value = state.bridgeUrl;
        await checkBridge();
    });
    elements.bridgeCheck.addEventListener("click", checkBridge);
    elements.workspaceSync.addEventListener("click", bootstrapWorkspace);
    elements.selectionRefresh.addEventListener("click", refreshSelection);
    elements.askRun.addEventListener("click", async () => {
        try {
            await runBridge("chat");
        } catch (error) {
            setStatus(elements.askStatus, formatBridgeError(error), "bad");
        }
    });
    elements.suggestRun.addEventListener("click", async () => {
        try {
            await runBridge("suggest");
        } catch (error) {
            setStatus(elements.suggestStatus, formatBridgeError(error), "bad");
        }
    });
    elements.categoriesLoad.addEventListener("click", async () => {
        try {
            await loadCategories();
        } catch (error) {
            renderCategoryCards([{ title: "Category load failed", summary: formatBridgeError(error) }]);
        }
    });
    elements.exemplarSearch.addEventListener("click", async () => {
        try {
            await searchExemplars();
        } catch (error) {
            setStatus(elements.exemplarStatus, formatBridgeError(error), "bad");
        }
    });
    elements.documentTypeSelect.addEventListener("change", async () => {
        const current = await getDocumentState();
        await saveDocumentState({
            ...current,
            documentTypeSlug: elements.documentTypeSelect.value,
            documentTitle: elements.documentTitle.value.trim(),
        });
        if (state.workspace?.id) {
            await bootstrapWorkspace();
        }
    });
    elements.documentTitle.addEventListener("change", async () => {
        const current = await getDocumentState();
        await saveDocumentState({
            ...current,
            documentTypeSlug: elements.documentTypeSelect.value,
            documentTitle: elements.documentTitle.value.trim(),
        });
    });
}

async function initialize() {
    const savedBridge = window.localStorage.getItem("biaedge.wordAddinBridgeUrl");
    if (savedBridge) {
        state.bridgeUrl = normalizeBridgeUrl(savedBridge);
    }
    elements.bridgeUrl.value = state.bridgeUrl;
    elements.documentTitle.value = await detectDocumentTitle();
    await loadDocumentTypes();
    await checkBridge();
    await refreshSelection();
    await bootstrapWorkspace();
}

bindEvents();

if (window.Office?.onReady) {
    Office.onReady(() => {
        initialize().catch((error) => {
            setStatus(elements.workspaceStatus, formatBridgeError(error), "bad");
        });
    });
} else {
    initialize().catch((error) => {
        setStatus(elements.workspaceStatus, formatBridgeError(error), "bad");
    });
}

export const DOCUMENT_STATE_KEY = "biaedge.wordAddinState";
export const DEFAULT_BRIDGE_URL = "https://localhost:8765";

export function normalizeBridgeUrl(raw) {
    const value = String(raw || "").trim();
    return value.replace(/\/+$/, "") || DEFAULT_BRIDGE_URL;
}

export function clipDocumentText(text, maxChars = 12000, tailChars = 2500) {
    const normalized = String(text || "").trim();
    if (normalized.length <= maxChars) {
        return normalized;
    }
    const safeTail = Math.min(tailChars, maxChars - 500);
    const headChars = Math.max(500, maxChars - safeTail);
    const head = normalized.slice(0, headChars).trimEnd();
    const tail = normalized.slice(-safeTail).trimStart();
    const omitted = Math.max(0, normalized.length - head.length - tail.length);
    return `${head}\n\n[Document excerpt clipped. ${omitted} characters omitted.]\n\n${tail}`;
}

export function coerceWorkspaceState(raw) {
    if (!raw || typeof raw !== "object") {
        return {
            workspaceId: "",
            documentTypeSlug: "",
            documentTitle: "",
            persistent: true,
        };
    }
    return {
        workspaceId: String(raw.workspaceId || "").trim(),
        documentTypeSlug: String(raw.documentTypeSlug || "").trim(),
        documentTitle: String(raw.documentTitle || "").trim(),
        persistent: raw.persistent !== false,
    };
}

export function authorityCitationLabel(authority) {
    if (!authority || typeof authority !== "object") {
        return "Authority";
    }
    return String(authority.citation || authority.title || "Authority").trim() || "Authority";
}

export function formatBridgeError(error) {
    if (!error) {
        return "Unknown bridge error.";
    }
    if (typeof error === "string") {
        return error;
    }
    if (error.message) {
        return String(error.message);
    }
    return "Bridge request failed.";
}

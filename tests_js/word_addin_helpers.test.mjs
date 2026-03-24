import test from "node:test";
import assert from "node:assert/strict";

import {
    DEFAULT_BRIDGE_URL,
    authorityCitationLabel,
    clipDocumentText,
    coerceWorkspaceState,
    formatBridgeError,
    normalizeBridgeUrl,
} from "../static/word-addin/helpers.js";

test("normalizeBridgeUrl trims whitespace and trailing slashes", () => {
    assert.equal(normalizeBridgeUrl(" https://localhost:8765/// "), DEFAULT_BRIDGE_URL);
    assert.equal(normalizeBridgeUrl(""), DEFAULT_BRIDGE_URL);
});

test("clipDocumentText keeps short documents intact", () => {
    const text = "Short selection.";
    assert.equal(clipDocumentText(text, 100, 20), text);
});

test("clipDocumentText clips long documents with an omission marker", () => {
    const text = `${"A".repeat(7000)}${"B".repeat(7000)}`;
    const clipped = clipDocumentText(text, 4000, 1000);

    assert.ok(clipped.startsWith("A".repeat(3000)));
    assert.ok(clipped.includes("[Document excerpt clipped."));
    assert.ok(clipped.endsWith("B".repeat(1000)));
    assert.ok(clipped.length < text.length);
});

test("coerceWorkspaceState normalizes missing values", () => {
    assert.deepEqual(coerceWorkspaceState(null), {
        workspaceId: "",
        documentTypeSlug: "",
        documentTitle: "",
        persistent: true,
    });
    assert.deepEqual(coerceWorkspaceState({
        workspaceId: " 123 ",
        documentTypeSlug: " brief ",
        documentTitle: " Draft ",
        persistent: false,
    }), {
        workspaceId: "123",
        documentTypeSlug: "brief",
        documentTitle: "Draft",
        persistent: false,
    });
});

test("authorityCitationLabel prefers citation over title", () => {
    assert.equal(
        authorityCitationLabel({ citation: "25 I&N Dec. 341 (BIA 2010)", title: "Matter of C-T-L-" }),
        "25 I&N Dec. 341 (BIA 2010)",
    );
    assert.equal(authorityCitationLabel({ title: "Matter of Acosta" }), "Matter of Acosta");
    assert.equal(authorityCitationLabel({}), "Authority");
});

test("formatBridgeError returns a readable message", () => {
    assert.equal(formatBridgeError("boom"), "boom");
    assert.equal(formatBridgeError(new Error("bridge down")), "bridge down");
    assert.equal(formatBridgeError({}), "Bridge request failed.");
});

/**
 * API Client — Verification & shared fetch wrapper
 *
 * Uses the fetch API.  Error responses are parsed for `error` and `detail`
 * fields and thrown as Error objects with those fields attached.
 */

const API_BASE = "/api";

/**
 * Header the server demands on every /api/* request.
 *
 * Carrying a custom header is what makes a cross-origin request
 * non-simple, so the browser must preflight it — and this server answers
 * no preflight. A page from another origin therefore cannot reach the
 * API at all, which is the point: the port is loopback-only but the
 * browser sitting on 127.0.0.1 is not the UI. Values are not secrets.
 */
const REQUEST_GUARD_HEADERS = { "X-PDT-Request": "1" };

async function apiRequest(path, options = {}) {
    const { headers, ...rest } = options;
    const res = await fetch(`${API_BASE}${path}`, {
        ...rest,
        headers: {
            ...REQUEST_GUARD_HEADERS,
            "Content-Type": "application/json",
            ...(headers || {}),
        },
    });
    if (!res.ok) {
        let err = {};
        try {
            err = await res.json();
        } catch {
            err = { detail: res.statusText, error: res.statusText };
        }
        let detail = err.detail || err.error || res.statusText;
        if (Array.isArray(detail) && detail.length) detail = JSON.stringify(detail);
        const error = new Error(detail || `HTTP ${res.status}`);
        error.detail = err.detail;
        error.error = err.error;
        throw error;
    }
    return res.json();
}

// ------------------------------------------------------------------
// Verification API
// ------------------------------------------------------------------

async function getVerificationStatus(planId) {
    return apiRequest(`/verification/${planId}/status`);
}

async function startVerification(planId, options = {}) {
    const body = {};
    if (options.max_rounds !== undefined) body.max_rounds = options.max_rounds;
    if (options.auto_fix !== undefined) body.auto_fix = options.auto_fix;
    return apiRequest(`/verification/${planId}/start`, {
        method: "POST",
        body: JSON.stringify(body),
    });
}

async function getRepairTasks(planId) {
    return apiRequest(`/verification/${planId}/repair_tasks`);
}

async function stopVerification(planId) {
    return apiRequest(`/verification/${planId}/stop`, {
        method: "POST",
    });
}

// Expose globally for non-module script usage and for test imports
if (typeof window !== "undefined") {
    window.getVerificationStatus = getVerificationStatus;
    window.startVerification = startVerification;
    window.getRepairTasks = getRepairTasks;
    window.stopVerification = stopVerification;
}

if (typeof module !== "undefined" && module.exports) {
    module.exports = {
        apiRequest,
        getVerificationStatus,
        startVerification,
        getRepairTasks,
        stopVerification,
    };
}

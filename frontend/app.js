/**
 * Spec-Driven Dev System — Frontend Application v2
 * Supports: Interview → PRD → PRD Review Loop → [Arch → Arch Review Loop] →
 *           [Test → Test Review Loop] → Tasks
 */

const API = "/api";

// --- State ---
let currentPlanId = null;
let currentPlanState = null;
let interviewStarted = false;
window.verificationState = null;

// --- Sidebar ---

function phaseLabel(status) {
    return { new: "新建", interviewed: "需求收集", prd: "PRD已生成", reviewed: "审阅中", queued: "排队中" }[status] || status;
}

function phaseClass(status) {
    const known = ["new", "interviewed", "prd", "reviewed", "queued"];
    return known.includes(status) ? `phase-${status}` : "phase-default";
}

async function loadSidebar() {
    try {
        const data = await api("/plans");
        const plans = Array.isArray(data) ? data : (data.plans || []);
    renderSidebar(plans);
    } catch {
        // ignore sidebar errors silently
    }
}

function renderSidebar(plans) {
    const container = $("#sidebar-plans");
    if (!plans || !plans.length) {
        container.innerHTML = '<div class="sidebar-empty">暂无计划<br>点击 + 新建</div>';
        return;
    }
    container.innerHTML = plans.map((p) => `
        <div class="sidebar-plan-item${escapeHtml(currentPlanId === p.id ? " active" : "")}" data-plan="${escapeHtml(p.id)}">
            <div class="sidebar-plan-name">${escapeHtml(p.id)}</div>
            <div class="sidebar-plan-phase ${escapeHtml(phaseClass(p.status))}">${escapeHtml(phaseLabel(p.status))}</div>
            <div class="sidebar-plan-dots">
                <span class="sidebar-plan-dot${escapeHtml(p.steps.interview ? " done" : "")}"></span>
                <span class="sidebar-plan-dot${escapeHtml(p.steps.prd ? " done" : "")}"></span>
                <span class="sidebar-plan-dot${escapeHtml(p.steps.review ? " done" : "")}"></span>
                <span class="sidebar-plan-dot${escapeHtml(p.steps.arch ? " done" : "")}"></span>
                <span class="sidebar-plan-dot${escapeHtml(p.steps.test ? " done" : "")}"></span>
                <span class="sidebar-plan-dot${escapeHtml(p.steps.tasks ? " done" : "")}"></span>
            </div>
        </div>
    `).join("");

    container.querySelectorAll(".sidebar-plan-item").forEach((item) => {
        item.addEventListener("click", () => {
            resumePlan(item.dataset.plan);
        });
    });
}

function updateSidebarActiveItem(planId) {
    $$(".sidebar-plan-item").forEach((item) => {
        item.classList.toggle("active", item.dataset.plan === planId);
    });
}

// --- Helpers ---

// See frontend/api.js — the same guard header, for the same reason:
// a custom header forces a CORS preflight that this server never grants,
// so a foreign page cannot drive the loopback API from the browser.
const REQUEST_GUARD_HEADERS = { "X-PDT-Request": "1" };

async function api(path, options = {}) {
    const { headers, ...rest } = options;
    const res = await fetch(`${API}${path}`, {
        ...rest,
        headers: {
            ...REQUEST_GUARD_HEADERS,
            "Content-Type": "application/json",
            ...(headers || {}),
        },
    });
    if (!res.ok) {
        let err = await res.json().catch(() => ({ detail: res.statusText }));
        let detail = err.detail;
        if (Array.isArray(detail) && detail.length) detail = JSON.stringify(detail);
        throw new Error(detail || `HTTP ${res.status}`);
    }
    return res.json();
}

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }
// The legacy escaping helper — the renderers that predate escapeHtml()
// still call this one.  It must never throw: a task id in tasks.json is
// allowed to be a **number**, and `s.replace` on a Number is a TypeError
// that used to blank the whole task list (loadTasks catches it and paints
// 「任务尚未生成」 over a plan that *has* tasks — silently, with a clean
// console).  Coerce first, escape second.
//
// The escaped set is deliberately unchanged: escapeHtml() also escapes the
// apostrophe, and adding it here would change what the page renders.
function esc(s) { return String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

// HTML-escape for innerHTML template interpolation. Used by every plan/task
// view renderer so server-supplied fields (plan id, status, title, etc.) cannot
// reach innerHTML verbatim. Null / undefined / numbers are coerced to strings
// rather than throwing — render-time data can be missing without bringing the
// view down.
function escapeHtml(value) {
    return String(value == null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function show(id) {
    $$(".view").forEach((v) => v.classList.add("hidden"));
    const el = $(`#${id}`);
    if (el) el.classList.remove("hidden");
}

function safeBind(id, event, handler) {
    const el = $(id);
    if (el) el.addEventListener(event, handler);
    else console.warn(`Element ${id} not found — event not bound.`);
}

function showLoading(text = "处理中...") {
    $("#loading-text").textContent = text;
    $("#loading").classList.remove("hidden");
}

function hideLoading() {
    $("#loading").classList.add("hidden");
}

// --- Dynamic Steps ---

function renderSteps(archEnabled = false, testEnabled = false) {
    const nav = $("#steps-nav");
    const steps = [
        { key: "interview", label: "需求收集" },
        { key: "prd", label: "PRD" },
        { key: "review", label: "PRD审阅" },
    ];
    if (archEnabled) {
        steps.push({ key: "arch-review", label: "架构审阅" });
    }
    if (testEnabled) {
        steps.push({ key: "test-review", label: "测试审阅" });
    }
    steps.push({ key: "tasks", label: "任务" });

    let html = "";
    steps.forEach((step, i) => {
        html += `<div class="step" data-step="${escapeHtml(step.key)}"><span class="step-num">${escapeHtml(i + 1)}</span><span class="step-label">${escapeHtml(step.label)}</span></div>`;
        if (i < steps.length - 1) {
            html += `<div class="step-line"></div>`;
        }
    });
    nav.innerHTML = html;

    // Re-bind step clicks
    $$(".step").forEach((s) => {
        s.addEventListener("click", () => {
            if (!currentPlanId) return;
            handleStepClick(s.dataset.step);
        });
    });
}

function setStep(stepKey) {
    $$(".step").forEach((s) => s.classList.remove("active", "done"));
    const steps = Array.from($$(".step")).map((s) => s.dataset.step);
    const idx = steps.indexOf(stepKey);
    $$(".step").forEach((s, i) => {
        if (i < idx) s.classList.add("done");
        else if (i === idx) s.classList.add("active");
    });
}

async function handleStepClick(step) {
    if (!currentPlanId) return;
    if (step === "interview") {
        try {
            const interview = await api(`/interview/${currentPlanId}`);
            showInterview(currentPlanId, interview);
        } catch {
            showInterview(currentPlanId);
        }
    } else if (step === "prd") {
        showLoading("加载 PRD...");
        try {
            await api(`/prd/${currentPlanId}`);
            showPrdView(currentPlanId);
        } catch {
            hideLoading();
        }
    } else if (step === "review") {
        showReviewView(currentPlanId);
    } else if (step === "arch-review") {
        showArchReviewView(currentPlanId);
    } else if (step === "test-review") {
        showTestReviewView(currentPlanId);
    } else if (step === "tasks") {
        showLoading("加载任务...");
        api(`/tasks/${currentPlanId}`)
            .then(() => showTasksView(currentPlanId))
            .catch(() => hideLoading());
    }
}

// --- Plan State ---

async function loadPlanState(planId) {
    try {
        const state = await api(`/plan/${planId}/state`);
        currentPlanState = state;
        renderSteps(state.flags.arch_enabled, state.flags.test_enabled);
        return state;
    } catch {
        currentPlanState = null;
        renderSteps(false, false);
        return null;
    }
}

async function checkAllReviewsComplete() {
    const state = currentPlanState || await loadPlanState(currentPlanId);
    const problems = [];

    try {
        const d = await api(`/review/${currentPlanId}/items`);
        if (d.items.some((i) => i.status === "pending")) problems.push("PRD 审阅");
    } catch { /* not yet generated — skip */ }

    if (state && state.flags.arch_enabled) {
        try {
            const d = await api(`/arch/${currentPlanId}/review/items`);
            if (d.items.some((i) => i.status === "pending")) problems.push("架构审阅");
        } catch { /* not yet generated — skip */ }
    }

    if (state && state.flags.test_enabled) {
        try {
            const d = await api(`/test/${currentPlanId}/review/items`);
            if (d.items.some((i) => i.status === "pending")) problems.push("测试设计审阅");
        } catch { /* not yet generated — skip */ }
    }

    return problems;
}

function pollPlanState(planId, checkFn, onComplete, interval = 2000, maxAttempts = 30) {
    let attempts = 0;
    const timer = setInterval(async () => {
        attempts++;
        try {
            const state = await api(`/plan/${planId}/state`);
            currentPlanState = state;
            if (checkFn(state)) {
                clearInterval(timer);
                onComplete(state);
            }
        } catch {
            // ignore polling errors
        }
        if (attempts >= maxAttempts) {
            clearInterval(timer);
            onComplete(null);
        }
    }, interval);
    return timer;
}

// --- Review Controller ---

class ReviewController {
    constructor(planId, phase, apiPrefix, containerId, progressId, onAllAccepted) {
        this.planId = planId;
        this.phase = phase;
        this.apiPrefix = apiPrefix;
        this.containerId = containerId;
        this.progressId = progressId;
        this.onAllAccepted = onAllAccepted;
    }

    async loadItems() {
        showLoading("加载审阅项...");
        try {
            const data = await api(`${this.apiPrefix}/${this.planId}/review/items`);
            this.renderItems(data);
        } finally {
            hideLoading();
        }
    }

    renderItems(data) {
        const container = $(this.containerId);
        const total = data.total;
        const reviewed = data.items.filter((i) => i.status !== "pending").length;

        const progressHtml = `
            <span>${reviewed}/${total} 已审阅</span>
            <div class="progress-bar">
                <div class="progress-fill" style="width: ${total ? (reviewed / total) * 100 : 0}%"></div>
            </div>
        `;
        $(this.progressId).innerHTML = progressHtml;

        container.innerHTML =
            data.items
                .map(
                    (item) => `
            <div class="review-item ${item.status}" data-index="${item.index}">
                <div class="review-item-header">
                    <span class="review-item-title">决策点 ${item.index + 1}: ${item.title}</span>
                    ${item.status !== "pending"
                        ? `<span class="review-item-badge" style="background:${
                            item.status === "accepted" ? "var(--success)" : "var(--text-dim)"
                          };color:${item.status === "skipped" ? "var(--text)" : "#fff"}">${
                            { accepted: "已接受", skipped: "已跳过" }[item.status] || item.status
                          }</span>`
                        : ""}
                </div>
                <div class="review-item-content">${esc(item.content)}</div>
                ${item.status === "pending"
                    ? `
                    <div class="review-actions">
                        <button class="btn btn-success btn-accept" data-index="${item.index}">接受</button>
                        <button class="btn btn-revise" data-index="${item.index}" style="background:var(--info);border-color:var(--info);color:#fff">修订</button>
                        <button class="btn btn-skip" data-index="${item.index}">跳过</button>
                    </div>
                    <input class="review-revise-input" data-index="${item.index}" placeholder="输入修订意见，按 Enter 提交..." />
                `
                    : `
                    <div class="review-actions">
                        <button class="btn btn-reset" data-index="${item.index}">撤销</button>
                    </div>
                `}
            </div>`
                )
                .join("") +
            this._renderAddFooter();

        this.bindEvents(container);
    }

    _renderAddFooter() {
        return `
            <div class="review-add-footer">
                <div class="review-add-title">+ 追加新的决策点</div>
                <div class="review-add-hint">
                    描述你发现的缺口（例如「缺少监控告警策略」），LLM 会结合已有决策点
                    生成 CPEA 补齐。如果其实已被覆盖，会返回 NO_GAP 不写盘。
                </div>
                <textarea class="review-add-input" rows="2"
                    placeholder="输入缺口描述..."></textarea>
                <div class="review-add-actions">
                    <button class="btn btn-primary review-add-submit">追加</button>
                    <span class="review-add-feedback"></span>
                </div>
            </div>
        `;
    }

    bindEvents(container) {
        container.querySelectorAll(".btn-accept").forEach((btn) => {
            btn.addEventListener("click", () => this.submitAction(btn.dataset.index, "accept"));
        });
        container.querySelectorAll(".btn-revise").forEach((btn) => {
            btn.addEventListener("click", () => {
                const input = container.querySelector(`.review-revise-input[data-index="${btn.dataset.index}"]`);
                input.classList.toggle("show");
                if (input.classList.contains("show")) input.focus();
            });
        });
        container.querySelectorAll(".review-revise-input").forEach((input) => {
            input.addEventListener("keydown", async (e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    const q = input.value.trim();
                    if (q) {
                        input.value = "";
                        await this.submitAction(input.dataset.index, "revise", "", q);
                    }
                }
            });
        });
        container.querySelectorAll(".btn-skip").forEach((btn) => {
            btn.addEventListener("click", () => this.submitAction(btn.dataset.index, "skip"));
        });
        container.querySelectorAll(".btn-reset").forEach((btn) => {
            btn.addEventListener("click", () => this.submitAction(btn.dataset.index, "reset"));
        });
        const addBtn = container.querySelector(".review-add-submit");
        const addInput = container.querySelector(".review-add-input");
        if (addBtn && addInput) {
            addBtn.addEventListener("click", () => this.submitAdd(addInput));
            addInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    this.submitAdd(addInput);
                }
            });
        }
    }

    async submitAdd(inputEl) {
        const requirement = (inputEl.value || "").trim();
        if (!requirement) {
            this._showAddFeedback("请先描述缺口", "warn");
            return;
        }
        this._showAddFeedback("提交中…", "info");
        inputEl.disabled = true;
        try {
            const resp = await api(
                `${this.apiPrefix}/${this.planId}/decision_point/add`,
                {
                    method: "POST",
                    body: JSON.stringify({ requirement, count: 1 }),
                }
            );
            inputEl.value = "";
            if (resp.no_gap_reason) {
                this._showAddFeedback(`未追加：${resp.no_gap_reason}`, "warn");
            } else if ((resp.added || []).length > 0) {
                const titles = resp.added.map((dp) => dp.title).join("、");
                this._showAddFeedback(`已追加：${titles}`, "success");
                // Reload the list so the new pending card shows up with
                // its accept/revise/skip buttons.
                await this.loadItems();
                await this.checkComplete();
                return;
            } else {
                this._showAddFeedback("LLM 未返回内容，请重试", "warn");
            }
        } catch (err) {
            this._showAddFeedback(`追加失败：${err.message || err}`, "error");
        } finally {
            inputEl.disabled = false;
        }
    }

    _showAddFeedback(msg, level) {
        const el = document.querySelector(`#${this.containerId} .review-add-feedback`);
        if (!el) return;
        const colors = {
            info: "var(--text-dim)",
            success: "var(--success)",
            warn: "var(--warn, #d97706)",
            error: "var(--error, #dc2626)",
        };
        el.textContent = msg;
        el.style.color = colors[level] || colors.info;
    }

    async submitAction(index, action, note = "", question = "") {
        showLoading("提交审阅...");
        try {
            await api(`${this.apiPrefix}/${this.planId}/review/item/${index}`, {
                method: "POST",
                body: JSON.stringify({ action, note, question }),
            });
            await this.loadItems();
            await this.checkComplete();
        } finally {
            hideLoading();
        }
    }

    async checkComplete() {
        try {
            const data = await api(`${this.apiPrefix}/${this.planId}/review/items`);
            const allReviewed = data.items.every((i) => i.status !== "pending");

            if (allReviewed && data.items.length > 0) {
                if (this.onAllAccepted) this.onAllAccepted();
            }
        } catch {
            // ignore
        }
    }

    async acceptAll() {
        showLoading("正在接受全部...");
        try {
            const data = await api(`${this.apiPrefix}/${this.planId}/review/items`);
            const pending = data.items.filter((i) => i.status === "pending");
            if (pending.length === 0) return;
            for (const item of pending) {
                await api(`${this.apiPrefix}/${this.planId}/review/item/${item.index}`, {
                    method: "POST",
                    body: JSON.stringify({ action: "accept", note: "", question: "" }),
                });
            }
            await this.loadItems();
            await this.checkComplete();
        } finally {
            hideLoading();
        }
    }
}

// --- Plans View ---

async function loadPlans() {
    const data = await api("/plans");
    const container = $("#plans-list");
    const plans = Array.isArray(data) ? data : (data.plans || []);

    if (!plans.length) {
        container.innerHTML = `
            <div class="empty-state">
                <p>暂无计划</p>
                <p>点击「新建计划」开始</p>
            </div>`;
        return;
    }

    container.innerHTML = plans
        .map(
            (p) => `
        <div class="plan-card" data-plan="${escapeHtml(p.id)}">
            <div class="plan-card-header">
                <span class="plan-id">${escapeHtml(p.id)}</span>
                <span class="plan-status ${escapeHtml(p.status)}">${escapeHtml(
                { new: "新建", interviewed: "已访谈", prd: "已生成PRD", reviewed: "已审阅", queued: "排队中" }[p.status] || p.status
            )}</span>
            </div>
            <div class="plan-requirement">${escapeHtml(p.requirement || "无需求描述")}</div>
            <div class="plan-steps">
                <span class="plan-step-dot ${escapeHtml(p.steps.interview ? "done" : "")}" title="需求收集"></span>
                <span class="plan-step-dot ${escapeHtml(p.steps.prd ? "done" : "")}" title="PRD"></span>
                <span class="plan-step-dot ${escapeHtml(p.steps.review ? "done" : "")}" title="审阅"></span>
                <span class="plan-step-dot ${escapeHtml(p.steps.arch ? "done" : "")}" title="架构"></span>
                <span class="plan-step-dot ${escapeHtml(p.steps.test ? "done" : "")}" title="测试"></span>
                <span class="plan-step-dot ${escapeHtml(p.steps.tasks ? "done" : "")}" title="任务"></span>
            </div>
        </div>`
        )
        .join("");

    container.querySelectorAll(".plan-card").forEach((card) => {
        card.addEventListener("click", () => {
            const planId = card.dataset.plan;
            resumePlan(planId);
        });
    });
}

async function resumePlan(planId) {
    currentPlanId = planId;
    interviewStarted = true;
    updateSidebarActiveItem(planId);

    const state = await loadPlanState(planId);
    const phase = state ? state.current_phase : "interview";

    // Route to correct view based on phase
    if (phase.startsWith("interview")) {
        try {
            const interview = await api(`/interview/${planId}`);
            showInterview(planId, interview);
        } catch {
            showInterview(planId);
        }
    } else if (phase === "prd_generation") {
        showPrdView(planId);
    } else if (phase === "prd_review" || phase === "prd_refining") {
        if (phase === "prd_refining") {
            show("prd-refine-view");
            pollForRefinement(planId, "prd");
        } else {
            showReviewView(planId);
        }
    } else if (phase === "prd_approved") {
        showArchOptionView(planId);
    } else if (phase === "arch_generation") {
        showArchView(planId);
    } else if (phase === "arch_review" || phase === "arch_refining") {
        if (phase === "arch_refining") {
            show("arch-refine-view");
            pollForRefinement(planId, "arch");
        } else {
            showArchReviewView(planId);
        }
    } else if (phase === "arch_approved") {
        showTestOptionView(planId);
    } else if (phase === "test_generation") {
        showTestView(planId);
    } else if (phase === "test_review" || phase === "test_refining") {
        if (phase === "test_refining") {
            show("test-refine-view");
            pollForRefinement(planId, "test");
        } else {
            showTestReviewView(planId);
        }
    } else if (phase === "test_approved" || phase === "tasks_generation" || phase === "queued" || phase === "ready" || phase === "executing" || phase.startsWith("verification")) {
        showTasksView(planId);
    } else {
        showInterview(planId);
    }
}

function pollForRefinement(planId, type) {
    const prefix = type === "prd" ? "prd" : type === "arch" ? "arch" : "test";
    const viewMap = { prd: "review-view", arch: "arch-review-view", test: "test-review-view" };
    pollPlanState(
        planId,
        (state) => state.current_phase === `${type}_review`,
        (state) => {
            if (state) {
                show(viewMap[type]);
                if (type === "prd") showReviewView(planId);
                else if (type === "arch") showArchReviewView(planId);
                else showTestReviewView(planId);
            }
        }
    );
}

// --- Interview ---

function addChatMsg(container, role, content) {
    const div = document.createElement("div");
    div.className = `chat-msg ${role}`;
    let html = "";
    if (role === "system") {
        html += `<div class="msg-label">Interviewer</div>`;
    } else {
        html += `<div class="msg-label">You</div>`;
    }
    if (Array.isArray(content)) {
        html += "<ul>" + content.map((q) => `<li>${escapeHtml(q)}</li>`).join("") + "</ul>";
    } else {
        html += `<div>${escapeHtml(content)}</div>`;
    }
    div.innerHTML = html;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function showInterview(planId, interviewData) {
    currentPlanId = planId;
    setStep("interview");
    show("interview-view");

    const chat = $("#interview-chat");
    chat.innerHTML = "";

    const isComplete = interviewData && interviewData.status === "complete";
    $("#interview-complete-bar").classList.toggle("hidden", !isComplete);
    $(".input-area").classList.toggle("hidden", isComplete);

    if (interviewData && interviewData.chat_history) {
        for (const msg of interviewData.chat_history) {
            addChatMsg(chat, msg.role === "user" ? "user" : "system", msg.content);
        }
    }

    if (interviewData && interviewData.dimensions) {
        const dims = interviewData.dimensions;
        const summary = [];
        if (dims.background) summary.push(`背景: ${dims.background}`);
        if (dims.goals) summary.push(`目标: ${dims.goals}`);
        if (dims.acceptance) summary.push(`验收: ${dims.acceptance}`);
        if (summary.length) {
            addChatMsg(chat, "system", `已收集信息:\n${summary.join("\n")}`);
        }
    }
}

async function startInterview(requirement) {
    showLoading("开始访谈...");
    try {
        const data = await api("/interview/start", {
            method: "POST",
            body: JSON.stringify({ requirement }),
        });
        currentPlanId = data.plan_id;
        await loadPlanState(currentPlanId);
        await loadSidebar();
        updateSidebarActiveItem(currentPlanId);

        const chat = $("#interview-chat");
        addChatMsg(chat, "user", requirement);

        if (data.questions && data.questions.length) {
            addChatMsg(chat, "system", data.questions);
        }
        if (data.complete) {
            addChatMsg(chat, "system", "需求收集完成！点击下方「生成 PRD」继续。");
            $("#interview-complete-bar").classList.remove("hidden");
            $(".input-area").classList.add("hidden");
        }
    } finally {
        hideLoading();
    }
}

async function continueInterview(reply) {
    showLoading("分析回答中...");
    try {
        const data = await api(`/interview/${currentPlanId}/continue`, {
            method: "POST",
            body: JSON.stringify({ reply }),
        });

        const chat = $("#interview-chat");
        addChatMsg(chat, "user", reply);

        if (data.questions && data.questions.length) {
            addChatMsg(chat, "system", data.questions);
        }
        if (data.complete) {
            addChatMsg(chat, "system", "需求收集完成！点击下方「生成 PRD」继续。");
            $("#interview-complete-bar").classList.remove("hidden");
            $(".input-area").classList.add("hidden");
        }
    } finally {
        hideLoading();
    }
}

// --- PRD View ---

async function showPrdView(planId) {
    currentPlanId = planId;
    setStep("prd");
    show("prd-view");

    showLoading("加载 PRD...");
    try {
        const data = await api(`/prd/${planId}`);
        $("#prd-content").textContent = data.prd;
    } finally {
        hideLoading();
    }
}

async function generatePrd() {
    showLoading("正在生成 PRD（AI 生成，可能需要 30 秒）...");
    try {
        const data = await api(`/prd/${currentPlanId}/generate`, { method: "POST" });
        $("#prd-content").textContent = data.prd;
        setStep("prd");
        show("prd-view");
        loadSidebar();
    } finally {
        hideLoading();
    }
}

async function regenPrd() {
    if (!confirm("重新生成将覆盖当前 PRD 和审阅记录，确定继续？")) return;
    showLoading("正在重新生成 PRD...");
    try {
        const data = await api(`/prd/${currentPlanId}/regenerate`, { method: "POST" });
        await loadPlanState(currentPlanId);
        $("#prd-content").textContent = data.prd;
        setStep("prd");
        show("prd-view");
        loadSidebar();
    } finally {
        hideLoading();
    }
}

// --- PRD Review ---

let prdReviewController = null;

async function showReviewView(planId) {
    currentPlanId = planId;
    setStep("review");
    show("review-view");

    prdReviewController = new ReviewController(
        planId,
        "prd",
        "/review",
        "#review-items",
        "#review-progress",
        () => showArchOptionView(planId)
    );
    await prdReviewController.loadItems();
}

async function refinePRD() {
    showLoading("正在修订 PRD...");
    try {
        await api(`/prd/${currentPlanId}/refine`, { method: "POST" });
        await loadPlanState(currentPlanId);
        showReviewView(currentPlanId);
    } finally {
        hideLoading();
    }
}

// --- Architecture Option ---

async function showArchOptionView(planId) {
    currentPlanId = planId;
    setStep("review");
    show("arch-option-view");
}

// --- Architecture View ---

async function showArchView(planId) {
    currentPlanId = planId;
    setStep("arch-review");
    show("arch-view");

    showLoading("加载架构设计...");
    try {
        const data = await api(`/arch/${planId}`);
        $("#arch-content").textContent = data.arch;
    } finally {
        hideLoading();
    }
}

async function generateArch() {
    showLoading("正在生成架构设计（AI 生成，可能需要 30 秒）...");
    try {
        const data = await api(`/arch/${currentPlanId}/generate`, { method: "POST" });
        await loadPlanState(currentPlanId);
        $("#arch-content").textContent = data.arch;
        setStep("arch-review");
        show("arch-view");
    } finally {
        hideLoading();
    }
}

async function regenArch() {
    if (!confirm("重新生成将覆盖当前架构设计和审阅记录，确定继续？")) return;
    showLoading("正在重新生成架构设计...");
    try {
        const data = await api(`/arch/${currentPlanId}/regenerate`, { method: "POST" });
        await loadPlanState(currentPlanId);
        $("#arch-content").textContent = data.arch;
        setStep("arch-review");
        show("arch-view");
        loadSidebar();
    } finally {
        hideLoading();
    }
}

// --- Architecture Review ---

let archReviewController = null;

async function showArchReviewView(planId) {
    currentPlanId = planId;
    setStep("arch-review");
    show("arch-review-view");

    archReviewController = new ReviewController(
        planId,
        "arch",
        "/arch",
        "#arch-review-items",
        "#arch-review-progress",
        () => showTestOptionView(planId)
    );
    await archReviewController.loadItems();
}

async function refineArch() {
    showLoading("正在修订架构设计...");
    try {
        await api(`/arch/${currentPlanId}/refine`, { method: "POST" });
        await loadPlanState(currentPlanId);
        showArchReviewView(currentPlanId);
    } finally {
        hideLoading();
    }
}

// --- Test Design Option ---

async function showTestOptionView(planId) {
    currentPlanId = planId;
    setStep("arch-review");
    show("test-option-view");
}

// --- Test Design View ---

async function showTestView(planId) {
    currentPlanId = planId;
    setStep("test-review");
    show("test-view");

    showLoading("加载测试设计...");
    try {
        const data = await api(`/test/${planId}`);
        $("#test-content").textContent = data.test_design;
    } finally {
        hideLoading();
    }
}

async function generateTestDesign() {
    showLoading("正在生成测试设计（AI 生成，可能需要 30 秒）...");
    try {
        const data = await api(`/test/${currentPlanId}/generate`, { method: "POST" });
        await loadPlanState(currentPlanId);
        $("#test-content").textContent = data.test_design;
        setStep("test-review");
        show("test-view");
    } finally {
        hideLoading();
    }
}

async function regenTestDesign() {
    if (!confirm("重新生成将覆盖当前测试设计和审阅记录，确定继续？")) return;
    showLoading("正在重新生成测试设计...");
    try {
        const data = await api(`/test/${currentPlanId}/regenerate`, { method: "POST" });
        await loadPlanState(currentPlanId);
        $("#test-content").textContent = data.test_design;
        setStep("test-review");
        show("test-view");
        loadSidebar();
    } finally {
        hideLoading();
    }
}

// --- Test Design Review ---

let testReviewController = null;

async function showTestReviewView(planId) {
    currentPlanId = planId;
    setStep("test-review");
    show("test-review-view");

    testReviewController = new ReviewController(
        planId,
        "test",
        "/test",
        "#test-review-items",
        "#test-review-progress",
        () => showTasksView(planId)
    );
    await testReviewController.loadItems();
}

async function refineTestDesign() {
    showLoading("正在修订测试设计...");
    try {
        await api(`/test/${currentPlanId}/refine`, { method: "POST" });
        await loadPlanState(currentPlanId);
        showTestReviewView(currentPlanId);
    } finally {
        hideLoading();
    }
}

// --- Execution ---

let _executionPoller = null;

function showExecutionPanel() {
    $("#execution-panel").classList.remove("hidden");
    $("#execution-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
    $("#project-dir-input").focus();
}

function setExecBadge(status) {
    const badge = $("#execution-status-badge");
    const labels = { running: "执行中", completed: "已完成", failed: "失败", stopped: "已停止", paused: "已暂停", not_started: "未开始" };
    badge.textContent = labels[status] || status;
    badge.className = `exec-badge ${status}`;
}

// What the badge should show for one status payload.
//
// `paused` outranks `status` because the two do not agree. A plan that
// was stopped and left alone reports `status: "stopped"` — the same
// value a plan that finished gets — and one that outlived a backend
// restart is not in the execution state at all, so it reports
// `not_started`. Only the checkpoint says whether the work is waiting
// to be continued, so it gets the last word. The poller and
// `resumeExecutionPanel` both go through here; letting them pick their
// own rule is how the badge ends up flickering between 已暂停 and
// 已停止 every two seconds.
function execBadgeState(data) {
    return data.paused ? "paused" : data.status;
}

function appendExecLogs(lines) {
    const el = $("#execution-logs");
    const wasAtBottom = el.scrollHeight - el.scrollTop <= el.clientHeight + 40;
    el.textContent = lines.join("\n");
    if (wasAtBottom) el.scrollTop = el.scrollHeight;
}

async function startExecution() {
    const projectDir = $("#project-dir-input").value.trim();
    if (!currentPlanId) {
        alert("尚未选择计划");
        return;
    }
    // An empty directory is not an error here. The backend reads
    // `project_dir` off the resume point first and only falls back to
    // this body, so continuing a paused plan must not make the
    // operator retype a path the checkpoint already carries. A start
    // with neither — no box filled in and no resume point — is answered
    // with a 400, which the catch below already puts in an alert.
    showLoading("正在启动执行...");
    try {
        await api(`/execution/${currentPlanId}/start`, {
            method: "POST",
            body: JSON.stringify(projectDir ? { project_dir: projectDir } : {}),
        });
        $("#execution-setup").classList.add("hidden");
        $("#execution-controls").classList.remove("hidden");
        $("#execution-logs").classList.remove("hidden");
        setExecBadge("running");
        startExecutionPolling();
    } catch (err) {
        alert(`启动执行失败：${err.message || "未知错误"}`);
        console.error(err);
    } finally {
        hideLoading();
    }
}

async function stopExecution() {
    await api(`/execution/${currentPlanId}/stop`, { method: "POST" });
    stopExecutionPolling();
    setExecBadge("stopped");
}

function startExecutionPolling() {
    stopExecutionPolling();
    _executionPoller = setInterval(async () => {
        try {
            const data = await api(`/execution/${currentPlanId}/status`);
            appendExecLogs(data.logs);
            setExecBadge(execBadgeState(data));
            if (data.paused) {
                // Nothing moves until the operator continues the plan,
                // so polling it only re-reads the same checkpoint. It
                // starts again from startExecution() once the resume
                // succeeds.
                stopExecutionPolling();
                return;
            }
            if (data.status !== "running") {
                stopExecutionPolling();
                if (data.status === "completed") {
                    loadSidebar();
                }
            }
        } catch { /* ignore */ }
    }, 2000);
}

function stopExecutionPolling() {
    if (_executionPoller) {
        clearInterval(_executionPoller);
        _executionPoller = null;
    }
}

async function resumeExecutionPanel(planId) {
    try {
        const data = await api(`/execution/${planId}/status`);
        // `not_started` is where a paused plan lands once the backend
        // has restarted — the in-memory execution state is gone, the
        // checkpoint is not — so the early return has to let a paused
        // plan through or the pause is invisible until the next start.
        if (data.status === "not_started" && !data.paused) return;
        showExecutionPanel();
        setExecBadge(execBadgeState(data));
        appendExecLogs(data.logs);
        $("#project-dir-input").value = data.project_dir || "";
        // A paused plan keeps the setup row: that row's button is the
        // existing 开始执行, which is the continue affordance — no new
        // control is introduced for pausing.
        $("#execution-setup").classList.toggle("hidden", !data.paused);
        $("#execution-controls").classList.remove("hidden");
        $("#execution-logs").classList.remove("hidden");
        if (data.status === "running") startExecutionPolling();
    } catch { /* ignore */ }
}

// --- Verification ---

let _verificationPoller = null;

function setVerificationBadge(status) {
    const badge = $("#verification-status-badge");
    const labels = {
        pending: "未开始",
        running: "验证中",
        passed: "已通过",
        failed: "验证失败",
        loop_stopped: "循环停止",
        not_started: "未开始",
        verification_failed: "验证失败",
    };
    badge.textContent = labels[status] || status;
    let cssStatus = status.replace(/^verification_/, "");
    if (cssStatus === "not_started") cssStatus = "pending";
    badge.className = `verification-badge ${cssStatus}`;
}

function renderVerificationPoints(points) {
    const container = $("#verification-points-list");
    if (!points || !points.length) {
        container.innerHTML = '<div style="color:var(--text-dim);font-size:13px;">暂无验证点</div>';
        return;
    }

    const icons = {
        passed: "✅",
        failed: "❌",
        running: "🔄",
        pending: "⏳",
        skipped: "⏭️",
        partial: "⚠️",
    };

    container.innerHTML = points
        .map(
            (p) => `
        <div class="verification-point" data-vp-id="${escapeHtml(p.id)}">
            <div class="verification-point-icon">${escapeHtml(icons[p.status] || "•")}</div>
            <div class="verification-point-body">
                <div class="verification-point-title">${escapeHtml(p.title || "")}</div>
                <div class="verification-point-id">${escapeHtml(p.id)}</div>
            </div>
            <span class="verification-method-tag">${escapeHtml(p.verification_method || "unknown")}</span>
            <div class="verification-point-progress">
                <div class="verification-point-progress-bar" style="width:${escapeHtml(p.progress || 0)}%"></div>
            </div>
        </div>`
        )
        .join("");
}

function updateVerificationPointsProgress(points) {
    if (!points || !points.length) return;
    const icons = {
        passed: "✅",
        failed: "❌",
        running: "🔄",
        pending: "⏳",
        skipped: "⏭️",
        partial: "⚠️",
    };
    points.forEach((p) => {
        const el = $(`.verification-point[data-vp-id="${p.id}"]`);
        if (!el) return;
        const iconEl = el.querySelector(".verification-point-icon");
        const bar = el.querySelector(".verification-point-progress-bar");
        if (iconEl) iconEl.textContent = icons[p.status] || "•";
        if (bar) bar.style.width = `${p.progress || 0}%`;
    });
}

function renderVerificationResult(data) {
    const overallEl = $("#verification-overall-result");
    const statsEl = $("#verification-stats");
    const deviationsEl = $("#verification-deviations");

    const status = data.overall_status || data.verification_status || "FAILED";
    const passed = data.passed_count || 0;
    const failed = data.failed_count || 0;
    const skipped = data.skipped_count || 0;

    const statusClass = status.toLowerCase().replace(/^verification_/, "");
    overallEl.className = `verification-overall ${statusClass}`;
    overallEl.textContent =
        status === "PASSED" || status === "passed"
            ? "✅ 验证通过"
            : status === "FAILED" || status === "failed"
              ? "❌ 验证失败"
              : status === "LOOP_STOPPED" || status === "loop_stopped"
                ? "⚠️ 循环停止"
                : `⚠️ ${status}`;

    statsEl.innerHTML = `
        <div class="verification-stat">
            <span>通过:</span>
            <span class="verification-stat-value passed">${escapeHtml(passed)}</span>
        </div>
        <div class="verification-stat">
            <span>失败:</span>
            <span class="verification-stat-value failed">${escapeHtml(failed)}</span>
        </div>
        ${skipped > 0 ? `
        <div class="verification-stat">
            <span>跳过:</span>
            <span class="verification-stat-value">${escapeHtml(skipped)}</span>
        </div>` : ""}
    `;

    const deviations = data.requirement_deviations || [];
    if (deviations.length > 0) {
        deviationsEl.innerHTML = deviations
            .map(
                (d) => `
            <div class="verification-deviation">
                <div class="verification-deviation-title">需求偏离 — ${escapeHtml(d.type || "未知")} (${escapeHtml(d.severity || "medium")})</div>
                <div class="verification-deviation-desc">${escapeHtml(d.description || "")}</div>
            </div>`
            )
            .join("");
    } else {
        deviationsEl.innerHTML = '<div style="color:var(--text-dim);font-size:13px;">无需求偏离记录</div>';
    }
}

function renderRepairTasks(tasks) {
    const container = $("#verification-repair-tasks");
    if (!tasks || !tasks.length) {
        container.innerHTML = '<div style="color:var(--text-dim);font-size:13px;">暂无修复任务</div>';
        return;
    }

    container.innerHTML = tasks
        .map(
            (t) => `
        <div class="verification-repair-task" data-repair-id="${escapeHtml(t.id)}">
            <span class="verification-repair-task-id">${escapeHtml(t.id)}</span>
            <div class="verification-repair-task-body">
                <div class="verification-repair-task-title">${escapeHtml(t.title || "")}</div>
                <div class="verification-repair-task-desc">${escapeHtml(t.deviation_description || t.description || "")}</div>
            </div>
            <button class="btn btn-primary btn-execute-repair" data-repair-id="${escapeHtml(t.id)}" style="padding:6px 14px;font-size:12px;">执行</button>
        </div>`
        )
        .join("");

    container.querySelectorAll(".btn-execute-repair").forEach((btn) => {
        btn.addEventListener("click", () => executeSingleRepair(btn.dataset.repairId));
    });
}

function updateVerificationActions(status) {
    const startBtn = $("#btn-start-verification");
    const stopBtn = $("#btn-stop-verification");

    startBtn.classList.add("hidden");
    stopBtn.classList.add("hidden");

    if (status === "pending" || status === "not_started") {
        startBtn.classList.remove("hidden");
    } else if (status === "running") {
        stopBtn.classList.remove("hidden");
    }
    // No action button for a finished round. There used to be a
    // "确认修复并重新验证" button here, but the auto-loop confirms and
    // chains rounds itself, so it was a no-op that only flipped
    // in-memory flags ("already running" forever after). Recovery from a
    // failed/stopped round is a restart: POST /verification/{id}/reset
    // then /start.
}

function updateVerificationVisibility(status) {
    const progressArea = $("#verification-progress-area");
    const resultArea = $("#verification-result-area");
    const repairArea = $("#verification-repair-area");

    progressArea.classList.add("hidden");
    resultArea.classList.add("hidden");
    repairArea.classList.add("hidden");

    if (status === "running") {
        progressArea.classList.remove("hidden");
    } else if (status === "passed" || status === "failed" || status === "loop_stopped" || status === "verification_failed") {
        resultArea.classList.remove("hidden");
    }

    if (status === "failed" || status === "verification_failed" || status === "loop_stopped") {
        repairArea.classList.remove("hidden");
    }
}

function renderVerificationPanel(data) {
    if (!data) return;

    // The verification progress carries the plan's outstanding human
    // decisions; the plan page shows them as a read-only count.
    if (data.pending_decisions) renderPendingDecisions(data.pending_decisions);

    const status = data.verification_status || data.status || "pending";
    const round = data.current_round ?? data.verification_round ?? 0;
    const maxRounds = data.max_rounds ?? data.verification_max_rounds ?? 4;

    setVerificationBadge(status);
    $("#verification-round").textContent = `Round ${round}/${maxRounds}`;

    updateVerificationVisibility(status);
    updateVerificationActions(status);

    if (status === "running" && data.verification_points) {
        const container = $("#verification-points-list");
        const existingIds = new Set(
            Array.from(container.querySelectorAll(".verification-point")).map((el) => el.dataset.vpId)
        );
        const newIds = new Set(data.verification_points.map((p) => p.id));
        const idsChanged =
            existingIds.size !== newIds.size ||
            !Array.from(newIds).every((id) => existingIds.has(id));

        // 2026-08-25: surface failed/skipped VPs as a dedicated
        // detail panel above the points list. The list itself
        // already shows per-VP icons, but the user explicitly
        // wanted a structured view of "exactly which VP failed
        // and what was it" — mirrors the task panel treatment.
        const failedVps = (data.vps || []).filter((v) => v.status === "failed");
        const skippedVps = (data.vps || []).filter((v) => v.status === "skipped");
        const vpCountsContainer = $("#verification-counts-detail");
        if (vpCountsContainer) {
            vpCountsContainer.innerHTML = `
                ${renderTaskCountsStrip(data.counts)}
                ${renderStatusDetailPanel("failed", "失败的验证点", failedVps)}
                ${renderStatusDetailPanel("skipped", "跳过的验证点", skippedVps)}
            `;
        }

        if (container.children.length === 0 || !window.verificationState || idsChanged) {
            renderVerificationPoints(data.verification_points);
        } else {
            updateVerificationPointsProgress(data.verification_points);
        }
    }

    if (status === "passed" || status === "failed" || status === "loop_stopped" || status === "verification_failed") {
        renderVerificationResult(data);
    }

    if (
        (status === "failed" || status === "verification_failed" || status === "loop_stopped") &&
        data.repair_tasks
    ) {
        const container = $("#verification-repair-tasks");
        const existingIds = new Set(
            Array.from(container.querySelectorAll(".verification-repair-task")).map((el) => el.dataset.repairId)
        );
        const newIds = new Set(data.repair_tasks.map((t) => t.id));
        const idsChanged =
            existingIds.size !== newIds.size ||
            !Array.from(newIds).every((id) => existingIds.has(id));
        if (container.children.length === 0 || !window.verificationState || idsChanged) {
            renderRepairTasks(data.repair_tasks);
        }
    }
}

async function fetchVerificationStatus() {
    if (!currentPlanId) return;
    try {
        // 2026-08-25: merge /status (plan + summary) with /progress
        // (runtime per-VP status + counts). /status alone returns
        // the static plan definition with no per-VP status, so
        // the failed/skipped detail panel would always be empty.
        const [statusResp, progressResp] = await Promise.all([
            api(`/verification/${currentPlanId}/status`),
            api(`/verification/${currentPlanId}/progress`).catch(() => null),
        ]);
        // Runtime overlays (preferred over /status's static fields):
        const data = Object.assign({}, statusResp);
        if (progressResp) {
            data.vps = progressResp.vps || [];
            data.counts = progressResp.counts || data.counts;
            data.completed_vps = progressResp.completed_vps || [];
            data.failed_vps = progressResp.failed_vps || [];
            data.skipped_vps = progressResp.skipped_vps || [];
            data.current_vp = progressResp.current_vp;
            // The progress projection carries the plan's outstanding
            // human decisions. Carried over here so the panel that
            // renders this payload can show them.
            data.pending_decisions = progressResp.pending_decisions;
        }
        window.verificationState = data;
        renderVerificationPanel(data);
    } catch (e) {
        console.warn("Verification status fetch failed:", e);
    }
}

function startVerificationPolling() {
    stopVerificationPolling();
    fetchVerificationStatus();
    _verificationPoller = setInterval(fetchVerificationStatus, 5000);
}

function stopVerificationPolling() {
    if (_verificationPoller) {
        clearInterval(_verificationPoller);
        _verificationPoller = null;
    }
}

async function startVerification() {
    if (!currentPlanId) return;
    showLoading("正在启动验证...");
    try {
        await api(`/verification/${currentPlanId}/start`, { method: "POST" });
        $("#verification-panel").classList.remove("hidden");
        startVerificationPolling();
    } catch (err) {
        alert(`启动验证失败：${err.message || "未知错误"}`);
    } finally {
        hideLoading();
    }
}

async function stopVerification() {
    if (!currentPlanId) return;
    showLoading("正在停止验证...");
    try {
        await api(`/verification/${currentPlanId}/stop`, { method: "POST" });
        stopVerificationPolling();
        await fetchVerificationStatus();
    } catch (err) {
        alert(`停止验证失败：${err.message || "未知错误"}`);
    } finally {
        hideLoading();
    }
}

async function executeAllRepairs() {
    if (!currentPlanId) return;
    showLoading("正在执行所有修复任务...");
    try {
        await api(`/verification/${currentPlanId}/execute-repairs`, {
            method: "POST",
            body: JSON.stringify({ mode: "all" }),
        });
        await fetchVerificationStatus();
    } catch (err) {
        alert(`执行修复任务失败：${err.message || "未知错误"}`);
    } finally {
        hideLoading();
    }
}

async function executeSingleRepair(repairId) {
    if (!currentPlanId || !repairId) return;
    showLoading("正在执行修复任务...");
    try {
        await api(`/verification/${currentPlanId}/execute-repairs`, {
            method: "POST",
            body: JSON.stringify({ mode: "single", repair_id: repairId }),
        });
        await fetchVerificationStatus();
    } catch (err) {
        alert(`执行修复任务失败：${err.message || "未知错误"}`);
    } finally {
        hideLoading();
    }
}

async function customExecuteRepairs() {
    const repairTasks = window.verificationState?.repair_tasks || [];
    if (!repairTasks.length) {
        alert("暂无修复任务");
        return;
    }
    const selected = prompt(
        `输入要执行的修复任务ID，用逗号分隔：\n可用任务: ${repairTasks.map((t) => t.id).join(", ")}`
    );
    if (!selected) return;
    const ids = selected
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    if (!ids.length) return;

    showLoading("正在执行选定修复任务...");
    try {
        await api(`/verification/${currentPlanId}/execute-repairs`, {
            method: "POST",
            body: JSON.stringify({ mode: "custom", repair_ids: ids }),
        });
        await fetchVerificationStatus();
    } catch (err) {
        alert(`执行修复任务失败：${err.message || "未知错误"}`);
    } finally {
        hideLoading();
    }
}

async function resumeVerificationPanel(planId) {
    const planPhase = currentPlanState?.current_phase || "";
    const isVerificationPhase = planPhase.startsWith("verification");
    const canStartVerification = ["queued", "executing"].includes(planPhase);
    const shouldShowPanel = isVerificationPhase || canStartVerification;

    try {
        const data = await api(`/verification/${planId}/status`);
        const status = data.verification_status || data.status || "not_started";

        // KEY FIX: Don't hide panel when canStartVerification is true
        // Panel should be visible in ready/executing phases to allow starting verification
        if (!canStartVerification && !isVerificationPhase && status === "not_started" && !data.verification_points) {
            $("#verification-panel").classList.add("hidden");
            return;
        }

        $("#verification-panel").classList.remove("hidden");
        window.verificationState = data;
        renderVerificationPanel(data);
        if (status === "running") {
            startVerificationPolling();
        }
    } catch (e) {
        if (shouldShowPanel) {
            $("#verification-panel").classList.remove("hidden");
            renderVerificationPanel({ verification_status: "pending", current_round: 0, max_rounds: 4 });
        } else {
            $("#verification-panel").classList.add("hidden");
        }
    }
}

// --- Tasks View ---

// --- Pending decisions (read-only) ---
//
// The plan's outstanding human decisions.
//
// The backend answers "how is this plan doing" with three
// projections — the plan summary, the execution progress and the
// verification progress — and all three carry the same
// `pending_decisions` block: a count plus the items. This page reads
// that block and shows it, because a plan that waits for a person
// looks exactly like a plan that is simply not moving.
//
// Read-only on purpose. Answering a decision is an HTTP exchange the
// operator performs outside this page — the PRD keeps the interactive
// card and the buttons out of it — so there is no button here and this
// file never asks the decision service for anything.
//
// Every string reaches the DOM through `textContent`. `question` and
// `decision_id` are free text written by whoever raised the decision:
// a decision that says `<img src=x onerror=...>` has to render as
// those characters, not as markup.

//: What the block reads when nothing has told it otherwise. The
//: projection degrades to an empty block every time the decision table
//: cannot be read, so "0 条" is the honest answer for a plan whose
//: decisions nobody could read.
const PENDING_DECISIONS_EMPTY = "待决策 0 条";

// The block's host on the plan page. Created on demand so the page
// carries no empty placeholder for it, and placed under the plan
// page's heading — the count of the decisions that are waiting is the
// first thing the page has to say.
function pendingDecisionsHost() {
    const view = $("#tasks-view");
    if (!view) return null;
    let host = view.querySelector(".pending-decisions");
    if (!host) {
        host = document.createElement("div");
        host.className = "pending-decisions";
        host.textContent = PENDING_DECISIONS_EMPTY;
        const heading = view.querySelector("h2");
        view.insertBefore(host, heading ? heading.nextSibling : view.firstChild);
    }
    return host;
}

// Render one `pending_decisions` block.
//
// `block` is the projection's own shape — `{count, items}` — and it
// may be missing entirely: a plan page that throws on a missing block
// is a plan page that cannot be opened. A missing block and an empty
// item list both render "待决策 0 条".
function renderPendingDecisions(block) {
    const host = pendingDecisionsHost();
    if (!host) return;
    const items = block && Array.isArray(block.items) ? block.items : [];
    const declared = block && Number.isFinite(block.count) ? block.count : 0;
    const count = items.length || declared;
    const detail = items
        .map((item) => {
            const fields = item || {};
            return [fields.decision_id, fields.question].filter(Boolean).join(" ");
        })
        .filter(Boolean)
        .join("；");
    host.textContent = detail ? `待决策 ${count} 条：${detail}` : `待决策 ${count} 条`;
}

// --- Usage panel (per-plan LLM accounting, 2026-09-21) ---
//
// Numbers come from GET /api/plan/{id}/usage, which aggregates CC
// Switch's own ledger for the sessions this plan used (proxy-observed
// calls + direct calls CC Switch learned from the session JSONL scan).

const USAGE_CROSS_CHECK = {
    consistent: ["已对账", "usage-badge passed"],
    partial: ["部分未对账", "usage-badge partial"],
    mismatch: ["对账异常", "usage-badge failed"],
    ledger_unavailable: ["账本不可用", "usage-badge unknown"],
    no_data: ["暂无数据", "usage-badge pending"],
};

function formatTokens(value) {
    const n = Number(value) || 0;
    return n.toLocaleString("en-US");
}

function formatUsd(value) {
    return `$${(Number(value) || 0).toFixed(4)}`;
}

async function loadUsagePanel(planId, { refresh = false } = {}) {
    const panel = $("#usage-panel");
    if (!panel) return;
    panel.classList.remove("hidden");
    const btn = $("#btn-refresh-usage");
    if (btn && !btn.dataset.bound) {
        btn.dataset.bound = "1";
        btn.addEventListener("click", () => loadUsagePanel(currentPlanId, { refresh: true }));
    }
    try {
        const report = await api(`/plan/${planId}/usage${refresh ? "?refresh=true" : ""}`);
        renderUsagePanel(report);
    } catch {
        $("#usage-body").classList.add("hidden");
        $("#usage-empty").classList.remove("hidden");
    }
}

function usageBreakdownTable(title, rows) {
    if (!rows || rows.length === 0) return "";
    const top = rows.slice(0, 6);
    const body = top
        .map(
            (r) => `<tr>
                <td>${r.key}</td>
                <td class="num">${formatTokens(r.requests)}</td>
                <td class="num">${formatTokens(r.new_input_tokens)}</td>
                <td class="num">${formatTokens(r.output_tokens)}</td>
                <td class="num">${formatTokens(r.cache_read_tokens)}</td>
                <td class="num">${formatUsd(r.cost_usd)}</td>
            </tr>`
        )
        .join("");
    return `<div class="usage-breakdown">
        <div class="usage-breakdown-title">${title}</div>
        <table class="usage-table">
            <thead><tr>
                <th></th><th class="num">请求</th><th class="num">新增输入</th>
                <th class="num">输出</th><th class="num">缓存命中</th><th class="num">成本</th>
            </tr></thead>
            <tbody>${body}</tbody>
        </table>
    </div>`;
}

function renderUsagePanel(report) {
    if (!report) return;
    $("#usage-empty").classList.add("hidden");
    $("#usage-body").classList.remove("hidden");

    const totals = report.totals || {};
    const sessions = report.sessions || {};
    const crossCheck = report.cross_check || {};
    const [label, cls] = USAGE_CROSS_CHECK[crossCheck.verdict] || ["未知", "usage-badge unknown"];
    const badge = $("#usage-cross-check-badge");
    badge.textContent = label;
    badge.className = cls;

    const untracked = sessions.untracked || 0;
    $("#usage-meta").innerHTML = `
        <span>会话 ${escapeHtml(formatTokens(sessions.distinct_sessions))}</span>
        <span>已记账 ${escapeHtml(formatTokens(sessions.tracked))}</span>
        <span class="${escapeHtml(untracked ? "usage-warn" : "")}">未记账 ${escapeHtml(formatTokens(untracked))}</span>
        <span>请求 ${escapeHtml(formatTokens(totals.requests))}</span>`;

    const hitRate = Math.round((Number(totals.cache_hit_rate) || 0) * 100);
    // Cost is CC Switch's number, always — that is what actually bills.
    // The re-priced figure is the same tokens valued at CC Switch's
    // current rates, shown only when it disagrees with the ledger.
    const repriced = totals.repriced_cost_usd;
    const repricedNote =
        repriced != null && Math.abs(repriced - (Number(totals.cost_usd) || 0)) > 1e-6
            ? `<div class="usage-repriced">按 CC Switch 现行单价重算：${escapeHtml(formatUsd(repriced))}</div>`
            : "";
    $("#usage-totals").innerHTML = `
        <div class="usage-stat"><div class="usage-stat-label">新增输入</div>
            <div class="usage-stat-value">${escapeHtml(formatTokens(totals.new_input_tokens))}</div></div>
        <div class="usage-stat"><div class="usage-stat-label">输出</div>
            <div class="usage-stat-value">${escapeHtml(formatTokens(totals.output_tokens))}</div></div>
        <div class="usage-stat"><div class="usage-stat-label">缓存命中 (${escapeHtml(hitRate)}%)</div>
            <div class="usage-stat-value">${escapeHtml(formatTokens(totals.cache_read_tokens))}</div></div>
        <div class="usage-stat"><div class="usage-stat-label">缓存创建</div>
            <div class="usage-stat-value">${escapeHtml(formatTokens(totals.cache_creation_tokens))}</div></div>
        <div class="usage-stat"><div class="usage-stat-label">计费成本 (CC Switch)</div>
            <div class="usage-stat-value">${escapeHtml(formatUsd(totals.cost_usd))}</div>
            ${escapeHtml(repricedNote)}</div>`;

    $("#usage-breakdowns").innerHTML =
        usageBreakdownTable("按 Provider", report.by_provider) +
        usageBreakdownTable("按场景", report.by_scene) +
        usageBreakdownTable("按来源", report.by_data_source);

    const notes = (crossCheck.notes || []).map((n) => `<li>${escapeHtml(n)}</li>`).join("");
    $("#usage-notes").innerHTML = notes ? `<ul>${escapeHtml(notes)}</ul>` : "";
    $("#usage-updated").textContent = report.generated_at
        ? `更新于 ${new Date(report.generated_at).toLocaleString()}`
        : "";
}

async function showTasksView(planId) {
    currentPlanId = planId;
    setStep("tasks");
    show("tasks-view");
    await loadTasks(planId);
    await resumeExecutionPanel(planId);
    await resumeVerificationPanel(planId);
    await loadUsagePanel(planId);
}

async function loadTasks(planId) {
    showLoading("加载任务...");
    try {
        // The plan summary is read next to the task list because it
        // carries the outstanding human decisions, which the plan page
        // shows as a read-only count. The summary is fetched with its
        // own catch: a summary that cannot be read must not take the
        // task list down with it.
        const [taskData, summary] = await Promise.all([
            api("/tasks/" + planId),
            api("/plan/" + planId + "/summary").catch(() => null),
        ]);
        renderTasks(Object.assign({}, taskData, {
            pending_decisions: summary ? summary.pending_decisions : undefined,
        }));
    } catch {
        $("#tasks-list").innerHTML = `
            <div class="empty-state">
                <p>任务尚未生成</p>
                <button class="btn btn-primary" id="btn-gen-tasks-inline" style="margin-top:12px">生成任务</button>
            </div>`;
        const btn = $("#btn-gen-tasks-inline");
        if (btn) btn.addEventListener("click", generateTasks);
    } finally {
        hideLoading();
    }
}

async function generateTasks() {
    const problems = await checkAllReviewsComplete();
    if (problems.length > 0) {
        alert(`以下审阅还有未完成的项，请先完成审阅再生成任务：\n\n${problems.join("\n")}`);
        return;
    }
    showLoading("正在生成任务列表（AI 生成，可能需要 30 秒）...");
    try {
        const data = await api(`/tasks/${currentPlanId}/generate`, { method: "POST" });
        renderTasks(data.tasks);
        setStep("tasks");
        show("tasks-view");
        loadSidebar();
    } finally {
        hideLoading();
    }
}

function renderTasks(data) {
    const container = $("#tasks-list");
    const tasks = data.tasks || [];

    // The plan summary carries the outstanding human decisions; the
    // plan page shows them as a read-only count above the task list.
    // The guard keeps a payload that does not carry the block — the
    // task list on its own — from overwriting a count that a fuller
    // payload has already put there.
    if (data.pending_decisions) renderPendingDecisions(data.pending_decisions);

    const counts = data.counts || {
        total: tasks.length,
        completed: 0,
        failed: 0,
        skipped: 0,
        in_progress: 0,
        pending: 0,
    };
    if (!tasks.length) {
        container.innerHTML = `
            <div class="empty-state"><p>暂无任务</p></div>
        `;
        return;
    }

    const failedTasks = tasks.filter((t) => (t.status || "pending") === "failed");
    const skippedTasks = tasks.filter((t) => (t.status || "pending") === "skipped");

    container.innerHTML = [
        renderTaskCountsStrip(counts),
        renderStatusDetailPanel("failed", "失败的任务", failedTasks),
        renderStatusDetailPanel("skipped", "跳过的任务", skippedTasks),
        `<div class="tasks-card-list">${tasks.map((t) => renderTaskCard(t)).join("")}</div>`,
    ].join("");
}

// Counts strip — pill row above the task list. Same visual
// language as the verification-points-list strip so the user
// recognises it across views.
function renderTaskCountsStrip(counts) {
    const c = counts || {};
    const pills = [
        { key: "total", icon: "📊", label: "总数", color: "pending" },
        { key: "completed", icon: "✅", label: "通过", color: "completed" },
        { key: "failed", icon: "❌", label: "失败", color: "failed" },
        { key: "skipped", icon: "⏭️", label: "跳过", color: "skipped" },
        { key: "in_progress", icon: "🔄", label: "进行中", color: "in_progress" },
        { key: "pending", icon: "⏳", label: "待执行", color: "pending" },
    ];
    return `
        <div class="task-counts-strip">
            ${pills
                .map(
                    (p) => `
                <span class="exec-badge ${p.color}">
                    ${p.icon} ${p.label}: <strong>${c[p.key] ?? 0}</strong>
                </span>`,
                )
                .join("")}
        </div>
    `;
}

// Status detail panel — surfaces every failed/skipped item with
// its id, title, and the runtime reason. Empty state keeps the
// panel visible (instead of disappearing) so the user always sees
// confirmation that nothing went wrong.
function renderStatusDetailPanel(kind, label, items) {
    const n = items.length;
    const icon = kind === "failed" ? "❌" : "⏭️";
    const emptyMsg =
        kind === "failed" ? "无失败任务 ✓" : "无跳过任务";
    return `
        <div class="task-status-detail">
            <div class="task-status-detail-header ${kind}">
                <span>${icon}</span>
                <span>${label} (${n})</span>
            </div>
            <div class="task-status-detail-body">
                ${n === 0
                    ? `<div class="task-status-detail-empty">${emptyMsg}</div>`
                    : items
                          .map(
                              (it) => `
                    <div class="task-status-detail-item">
                        <span class="task-status-detail-id">${esc(it.id || "?")}</span>
                        <div class="task-status-detail-body-wrap">
                            <div class="task-status-detail-title">${esc(it.title || "(无标题)")}</div>
                            ${renderStatusDetailReason(kind, it)}
                        </div>
                    </div>`,
                          )
                          .join("")}
            </div>
        </div>
    `;
}

function renderStatusDetailReason(kind, item) {
    // For tasks: failure_reason comes from state_machine.plan_task_repository.
    // For VPs (reused below): actual_result + reasons[0] from the verdict.
    if (item.failure_reason) {
        return `<div class="task-status-detail-reason">失败原因: ${esc(item.failure_reason)}</div>`;
    }
    if (kind === "skipped" && item.actual_result) {
        return `<div class="task-status-detail-reason">${esc(item.actual_result)}</div>`;
    }
    if (item.actual_result) {
        return `<div class="task-status-detail-reason">${esc(item.actual_result)}</div>`;
    }
    return "";
}

const _TASK_STATUS_LABELS = {
    completed: "已完成",
    failed: "失败",
    skipped: "已跳过",
    in_progress: "进行中",
    pending: "待执行",
    running: "进行中",
    stopped: "已停止",
};

function renderTaskCard(t) {
    const status = t.status || "pending";
    const label = _TASK_STATUS_LABELS[status] || status;
    // 2026-08-26: surface failure details inside the card itself so
    // the user sees *which* task is broken (by id + reason) without
    // having to scroll back up to the summary panel. The panel above
    // the list still mirrors the same info, but a failed card that
    // sits inline with 27 completed cards is otherwise invisible
    // unless the user notices the 3px red bar.
    const reasonText = (status === "failed" || status === "skipped")
        ? (t.failure_reason || t.actual_result || "")
        : "";
    const reasonBlock = reasonText
        ? `<div class="task-card-reason ${status}">${
              status === "failed" ? "失败原因" : "跳过原因"
          }: ${esc(reasonText)}</div>`
        : "";
    return `
        <div class="task-card ${status}">
            <span class="task-id ${status}">${esc(t.id || "?")}</span>
            <div class="task-body">
                <div class="task-title">${esc(t.title || "")}</div>
                <div class="task-desc">${esc(t.description || "")}</div>
                ${reasonBlock}
                <div class="task-test">$ ${esc(t.test_command || "无测试命令")}</div>
            </div>
            <span class="task-status-badge exec-badge ${status}">${label}</span>
        </div>`;
}

// --- Scheduler View ---
//
// The scheduler's read model comes from ONE endpoint
// (GET /api/scheduler/state) and the panel renders it the way it
// arrives: the order of the queue and the words on the reasons are
// the server's, not ours.  The panel never sorts the queue and never
// composes a reason of its own — a second order and a second
// vocabulary would be two answers to "who runs next" and "why not",
// and the operator would have no way of telling which of the two the
// scheduler is using.
//
// The four actions are the four the API has (promote / pause / resume
// / start).  Each one is a command: the answer says what the command
// did, not that it was accepted.  There is no global on/off switch
// here on purpose — the operator's rollback lever is a configuration
// key and it stays in the configuration, because a button that stops
// every plan at once is one mis-click away from an outage.

/** The reason vocabulary is closed on the server side
    (plan_scheduler.policy.REASONS); this maps each of the server's
    words to the text the panel draws.  A word that is not in the map
    is drawn as the word itself — never as an empty cell, and never as
    a guess. */
const SCHEDULER_REASON_TEXT = {
    running: "运行中",
    waiting_for_slot: "等待名额",
    blocked_by_capacity: "名额已满",
    workspace_busy: "工作区占用",
    due_later: "尚未到点",
    hitl_pending: "等待人工回答",
    paused: "已暂停",
    over_capacity: "超出名额",
};

/** The label each action's button carries. */
const SCHEDULER_ACTION_LABELS = {
    promote: "提升",
    pause: "暂停",
    resume: "恢复",
    start: "启动",
    "rerun-verification": "重跑验证",
};

/** Some phases need the button to say more than the action's own name.
    On a failed plan 「启动」 reads as "run the whole thing again", but
    the button only puts the plan back in the queue — the verification
    is a separate command with a separate button.  The phase-specific
    label says which one the operator is about to press. */
const SCHEDULER_ACTION_LABEL_BY_PHASE = {
    failed: { start: "重新排队执行" },
};

/** Which actions may be offered for a plan in a given phase.  This is
    the action matrix from the design: the panel offers what the server
    accepts, so a button that is on screen is a button the command will
    carry out.  A phase that is not in the table gets no buttons. */
const SCHEDULER_ACTIONS_BY_PHASE = {
    queued: ["promote", "pause", "start"],
    executing: ["pause"],
    stopped: ["resume", "start"],
    failed: ["start", "rerun-verification"],
    waiting_for_quota: ["start"],
    verification_loop_stopped: ["rerun-verification"],
    verification_failed: ["rerun-verification"],
};

/** The text one action's button carries.  The phase-specific label wins
    when there is one; otherwise the action's own label; and an action
    nobody labelled falls back to its own name, so a button never reads
    "undefined".  A missing phase is not an error — the queue row and
    the running row pass no phase at all. */
function schedulerActionLabel(action, phase) {
    return (
        SCHEDULER_ACTION_LABEL_BY_PHASE[phase]?.[action] ??
        SCHEDULER_ACTION_LABELS[action] ??
        action
    );
}

/** The reason the server gave, as the text the panel draws. */
function schedulerReasonText(reason) {
    if (reason === null || reason === undefined) return "";
    const word = String(reason);
    return SCHEDULER_REASON_TEXT[word] || word;
}

function schedulerSetText(selector, text) {
    const el = $(selector);
    if (el) el.textContent = text;
}

function schedulerToggle(el, visible) {
    if (el) el.classList.toggle("hidden", !visible);
}

/** Render the capacity widget — used / total and the queue length.
    The home widget and the scheduler view carry the same numbers: they
    are read out of one read model, so they cannot disagree.

    The control that changes the capacity follows the server as well:
    the number in force is the server's, never the operator's
    unfinished pick, and the word beside it says whether that number
    is the operator's own override or the configured one. */
function renderSchedulerCapacity(snapshot) {
    const capacity = (snapshot && snapshot.capacity) || {};
    const total = Number(capacity.total) || 0;
    const used = Number(capacity.used) || 0;
    const queued = Number(snapshot && snapshot.queue_length) || 0;
    const over = Boolean(snapshot && snapshot.over_capacity);

    const numbers = [
        ["#scheduler-capacity-used", used],
        ["#scheduler-view-capacity-used", used],
        ["#scheduler-capacity-total", total],
        ["#scheduler-view-capacity-total", total],
        ["#scheduler-capacity-queue", queued],
        ["#scheduler-view-capacity-queue", queued],
    ];
    numbers.forEach(([selector, value]) => schedulerSetText(selector, String(value)));

    const flags = [
        ["#scheduler-capacity-over", over],
        ["#scheduler-view-capacity-over", over],
    ];
    flags.forEach(([selector, flagged]) => {
        const el = $(selector);
        if (!el) return;
        el.classList.toggle("hidden", !flagged);
        el.textContent = flagged ? "over-capacity" : "";
    });

    schedulerSetText("#scheduler-view-generated-at", (snapshot && snapshot.generated_at) || "—");

    // Where the number in force came from.  The operator's own number
    // is marked, so a configured capacity is never passed off as an
    // override — and the override is never passed off as the
    // configured one.
    const isOverride = capacity.source === "override";
    schedulerSetText("#scheduler-capacity-source", isOverride ? "(覆盖)" : "");

    // The select shows the capacity in force.  It carries the numbers
    // an operator may pick (1..12); a number outside them is added, so
    // the control never shows a number the scheduler is not running
    // with.
    const select = $("#scheduler-capacity-select");
    if (select) {
        const value = String(total);
        let option = select.querySelector(`option[value="${value}"]`);
        if (!option) {
            option = document.createElement("option");
            option.value = value;
            option.textContent = value;
            select.appendChild(option);
        }
        select.value = value;
    }
}

/** Write the capacity that was picked — or put the configured one back.

    ``total`` is the number to run with; ``null`` (or no number at all)
    asks for the configured capacity back, and the body the wire
    carries says so — {"reset": true} — instead of carrying a number.

    The number in force afterwards is the **server's**, so the panel
    reads the state again instead of trusting the pick.  A failed write
    says what went wrong — the error's own text, in the action-result
    box — and puts the control back to the number that is really in
    force, so the operator's rejected pick is never mistaken for the
    capacity the scheduler is running with. */
async function schedulerApplyCapacity(total) {
    const reset = total === null || total === undefined;
    const body = reset ? {"reset": true} : {total: total};
    try {
        const result = await api("/scheduler/capacity", {method: "POST", body: JSON.stringify(body)});
        await loadSchedulerState();
        return result;
    } catch (err) {
        // The pick failed: read the state again so the control is
        // filled from the server's number, and then say what went
        // wrong.
        await loadSchedulerState();
        renderSchedulerError(err);
        throw err;
    }
}

/** The action buttons of one row.  A row that names no plan gets no
    button: a button that acts on nothing is worse than no button.
    The phase is optional — a row that does not know its phase gets the
    action's own label. */
function schedulerActionButtons(planId, actions, phase) {
    if (!planId || !actions || !actions.length) {
        return '<span class="scheduler-action-none">—</span>';
    }
    return actions
        .map((action) =>
            `<button class="btn btn-outline scheduler-action" data-action="${escapeHtml(action)}" data-plan="${escapeHtml(planId)}">${escapeHtml(schedulerActionLabel(action, phase))}</button>`
        )
        .join(" ");
}

/** The queue, in the order the server gave it.  An empty queue is an
    empty state, not an error: "nothing is waiting" is a normal thing to
    look at. */
function renderSchedulerQueue(snapshot) {
    const rows = snapshot && Array.isArray(snapshot.queue) ? snapshot.queue : [];
    const body = $("#scheduler-queue-list");
    if (!body) return;

    if (!rows.length) {
        body.innerHTML = "";
        schedulerToggle($("#scheduler-queue-table"), false);
        schedulerToggle($("#scheduler-queue-empty"), true);
        return;
    }

    body.innerHTML = rows
        .map((row) => {
            const item = row || {};
            const planId = item.plan_id ? String(item.plan_id) : "";
            const promoted = item.promote_order === null || item.promote_order === undefined ? "—" : item.promote_order;
            return `<tr>
                <td>${escapeHtml(item.position)}</td>
                <td>${escapeHtml(planId || "—")}</td>
                <td>${escapeHtml(item.seq)}</td>
                <td>${escapeHtml(item.priority)}</td>
                <td>${escapeHtml(promoted)}</td>
                <td>${escapeHtml(schedulerReasonText(item.reason))}</td>
                <td class="scheduler-row-actions">${schedulerActionButtons(planId, ["promote", "start"])}</td>
            </tr>`;
        })
        .join("");

    schedulerToggle($("#scheduler-queue-table"), true);
    schedulerToggle($("#scheduler-queue-empty"), false);
}

/** How long a plan has been running, in the words an operator reads.

    Seconds become a duration: hours and minutes, minutes and seconds,
    or plain seconds.  A number that was not given is a dash and not a
    zero — a plan whose start time is unknown has not run for zero
    seconds, it has run for an unknown length. */
function schedulerDurationText(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const text = String(seconds).trim();
    if (!text) return "—";
    const total = Number(text);
    if (!Number.isFinite(total)) return "—";
    const value = Math.max(0, Math.floor(total));
    if (value >= 3600) {
        const hours = Math.floor(value / 3600);
        const minutes = Math.floor((value % 3600) / 60);
        return `${hours}h ${minutes}m`;
    }
    if (value >= 60) {
        const minutes = Math.floor(value / 60);
        return `${minutes}m ${value % 60}s`;
    }
    return `${value}s`;
}

/** The plans that hold a slot, with the time they have been running and
    whether they may be interrupted.

    The duration is the plan's own clock — how long the plan has been
    running — and it falls back to the slot clock, the time since the
    slot was acquired, for a listing that does not carry the plan
    clock. */
function renderSchedulerRunning(snapshot) {
    const rows = snapshot && Array.isArray(snapshot.running) ? snapshot.running : [];
    const body = $("#scheduler-running-list");
    if (!body) return;

    if (!rows.length) {
        body.innerHTML = "";
        schedulerToggle($("#scheduler-running-table"), false);
        schedulerToggle($("#scheduler-running-empty"), true);
        return;
    }

    body.innerHTML = rows
        .map((row) => {
            const item = row || {};
            const planId = item.plan_id ? String(item.plan_id) : "";
            const duration = schedulerDurationText(item.running_sec ?? item.elapsed_sec);
            return `<tr>
                <td>${escapeHtml(planId || "—")}</td>
                <td>${escapeHtml(duration)}</td>
                <td>${escapeHtml(item.evictable ? "是" : "否")}</td>
                <td>${escapeHtml(item.workspace || "—")}</td>
                <td class="scheduler-row-actions">${schedulerActionButtons(planId, ["pause"])}</td>
            </tr>`;
        })
        .join("");

    schedulerToggle($("#scheduler-running-table"), true);
    schedulerToggle($("#scheduler-running-empty"), false);
}

/** The phase a plan is in: the routing phase when the listing carries
    one, and the listing's own bucket otherwise. */
function schedulerPlanPhase(plan) {
    if (!plan) return "";
    return String(plan.current_phase || plan.status || "");
}

/** The plans an operator can act on, with the actions their phase
    allows.  The order is the listing's own — the panel does not sort
    the plans either.

    This block is the one a **failed** plan is drawn in: a failed plan
    is in neither the queue nor the running list, so the two blocks
    above cannot show it. */
function renderSchedulerTerminalPlans(plans) {
    const rows = Array.isArray(plans) ? plans : [];
    const body = $("#scheduler-terminal-list");
    if (!body) return;

    const actionable = rows.filter((plan) => {
        const phase = schedulerPlanPhase(plan);
        return (SCHEDULER_ACTIONS_BY_PHASE[phase] || []).length > 0;
    });

    if (!actionable.length) {
        body.innerHTML = "";
        schedulerToggle($("#scheduler-terminal-table"), false);
        schedulerToggle($("#scheduler-terminal-empty"), true);
        return;
    }

    body.innerHTML = actionable
        .map((plan) => {
            const planId = plan && plan.id ? String(plan.id) : "";
            const phase = schedulerPlanPhase(plan);
            const actions = SCHEDULER_ACTIONS_BY_PHASE[phase] || [];
            return `<tr>
                <td>${escapeHtml(planId)}</td>
                <td>${escapeHtml(phase || "—")}</td>
                <td class="scheduler-row-actions">${schedulerActionButtons(planId, actions, phase)}</td>
            </tr>`;
        })
        .join("");

    schedulerToggle($("#scheduler-terminal-table"), true);
    schedulerToggle($("#scheduler-terminal-empty"), false);
}
/** Render the scheduler's read model.  The panel draws what it is
    given: the order of the queue, the numbers of the capacity and the
    words on the reasons are all the server's. */
function renderScheduler(snapshot) {
    const data = snapshot && typeof snapshot === "object" ? snapshot : {};
    renderSchedulerCapacity(data);
    renderSchedulerQueue(data);
    renderSchedulerRunning(data);
}

/** The failure each read last had, filed under the read that had it.

    Two failures are the same failure when they come from the same read
    and carry the same message.  The poll runs every five seconds, so a
    backend that is down would otherwise write the same line every five
    seconds for as long as the operator sat there watching.

    The read is part of the key, and that is deliberate.  Every read
    goes through the same api() wrapper, so a backend that is down
    makes them all fail with the very same message — and they are
    still separate failures, because separate reads are broken.  A
    marker shared by every read cannot tell them apart: the second
    read that fails with the same message is swallowed by the first
    one's marker, and the console shows one line for two broken reads.

    Because the key is the read, a read that succeeds again forgets
    only its own failure.  Another read may still be failing with the
    same message, and that failure must not be written a second time
    on the next tick.

    The marker is only about the console.  The last good frame on
    screen is kept by the quiet reads themselves: they simply do not
    paint over it. */
const _schedulerPollErrors = {};

/** The key a failure is filed under when it does not say which read it
    came from.  It is a key of the same map, so a failure that nobody
    attributes to a read never shares a memory with one that is. */
const _schedulerPollUnscoped = "unscoped";

/** Write a failed quiet read to the console — once per failure.

    The same read failing the same way is written once.  A different
    message is a different kind of failure and is written as well: the
    marker is a memory of the last failure of *this* read, not a mute
    that silences the console for ever.

    The marker is written before the line, so the tick that follows
    finds it and stays quiet. */
function schedulerLogPollError(err, source) {
    const message = err && err.message ? String(err.message) : String(err || "");
    if (!message) return;
    const key = source ? String(source) : _schedulerPollUnscoped;
    if (_schedulerPollErrors[key] === message) return;
    _schedulerPollErrors[key] = message;
    console.warn("scheduler refresh failed:", message);
}

/** The failure is gone: the read that failed succeeded again.

    Only that read's failure is forgotten.  A read that did not fail
    must not clear another read's failure, or the very next tick would
    write that failure a second time. */
function schedulerClearPollError(source) {
    const key = source ? String(source) : _schedulerPollUnscoped;
    delete _schedulerPollErrors[key];
}

/** Read the read model.  A scheduler that is not running answers an
    empty document, so there is nothing to catch here but a transport
    failure — and that one is shown rather than thrown away.

    A quiet read is the poll's read.  It keeps the last good frame
    instead of painting over it, and says what went wrong in the
    console rather than on the operator's screen. */
async function loadSchedulerState(options = {}) {
    try {
        const snapshot = await api("/scheduler/state");
        renderScheduler(snapshot);
        if (options.quiet) schedulerClearPollError("state");
        // The parallel-agent count is a second, optional read: it rides
        // along with this refresh instead of owning a timer of its own.
        loadSchedulerAgentCount();
        // The terminal plans are a third read: they are the plans the
        // queue and the running list cannot show.
        await loadSchedulerTerminalPlans();
        return snapshot;
    } catch (err) {
        if (options.quiet) {
            schedulerLogPollError(err, "state");
            return null;
        }
        renderSchedulerError(err);
        return null;
    }
}

/** Read the plans the operator can act on.  The terminal plans are
    included on purpose: a stopped plan is the one that can be
    resumed, and a failed plan is the one that carries the two
    buttons. */
async function loadSchedulerTerminalPlans() {
    try {
        const data = await api("/plans?include_terminal=true");
        const plans = Array.isArray(data) ? data : (data.plans || []);
        renderSchedulerTerminalPlans(plans);
        return plans;
    } catch {
        renderSchedulerTerminalPlans([]);
        return [];
    }
}

/** How many agents are running right now.

    ``GET /api/system/active`` is the workflow's own answer: how many
    execution subprocesses and verification cycles are in flight, and
    how many agents that is altogether.  It is not the scheduler's
    capacity number — six plans holding six slots can be driving eight
    agents, because one plan may be executing *and* verifying at the
    same time.

    The count is optional, and a failure must stay optional: this
    paints a dash and stops.  It must never call renderSchedulerError()
    — that paints an error banner over a panel that is otherwise
    working, and an unknown agent count is not a broken scheduler. */
async function loadSchedulerAgentCount() {
    try {
        const data = await api("/system/active");
        const raw = data && data.total_active;
        const total = Number(raw);
        const text = (raw === null || raw === undefined || raw === "" || !Number.isFinite(total))
            ? "—"
            : String(total);
        schedulerSetText("#scheduler-view-agent-count", text);
    } catch (err) {
        schedulerSetText("#scheduler-view-agent-count", "—");
    }
}

/** How often the scheduler view re-reads the read model.  Five seconds
    is short enough that a queued plan shows up while the operator is
    still looking at the panel, and long enough that the panel does not
    hammer the API. */
const SCHEDULER_POLL_MS = 5000;

/** The one timer that drives the panel's refreshes.  A single handle is
    what keeps a second visit from stacking a second timer on top of the
    first. */
let _schedulerPoller = null;

/** Is the scheduler view the one on screen?  A poll that runs while the
    operator is on another view refreshes a panel nobody is looking at,
    so it draws nothing. */
function schedulerViewVisible() {
    const view = $("#scheduler-view");
    return Boolean(view) && !view.classList.contains("hidden");
}

/** Re-read the read model and redraw the panel.  This is a redraw and
    nothing else: the page is not reloaded, so the operator keeps the
    view they are in, and the loader that already exists is the one
    that does the reading.

    A hidden page reads nothing.  The guard is here rather than in the
    timer so that every caller is covered — the interval, the manual
    refresh and the refresh that happens when the tab comes back.

    The poll reads quietly.  It is the one reader the operator did not
    ask a question, so a failure it hits is a note in the console
    rather than an error painted over a panel that is otherwise fine —
    and the panel keeps the frame it last drew. */
async function refreshScheduler() {
    if (document.hidden || !schedulerViewVisible()) return;
    await loadSchedulerState({ quiet: true });
}

/** Start the panel's refresh timer.  The previous timer is cleared
    first, so entering the view twice cannot leave two of them running
    and double the request rate. */
function startSchedulerPolling() {
    stopSchedulerPolling();
    if (!schedulerViewVisible()) return;
    _schedulerPoller = setInterval(refreshScheduler, SCHEDULER_POLL_MS);
}

/** Stop the panel's refresh timer. */
function stopSchedulerPolling() {
    if (_schedulerPoller) {
        clearInterval(_schedulerPoller);
        _schedulerPoller = null;
    }
}

/** React to the page being hidden or shown.

    A hidden tab stops polling: nobody is looking at the panel, so the
    requests are pure load.  A tab that comes back refreshes at once —
    the operator should not have to wait out an interval to see the
    state they came back for — and then resumes the cadence.

    The scheduler view is checked as well as the page: coming back to
    the tab while another view is on screen starts nothing, because
    there is no panel on screen to keep current. */
function handleSchedulerVisibilityChange() {
    if (document.hidden) {
        stopSchedulerPolling();
        return;
    }
    if (!schedulerViewVisible()) return;
    refreshScheduler();
    startSchedulerPolling();
}

/** What a command did, in the command's own words. */
function renderSchedulerOutcome(result) {
    const outcome = result && result.outcome ? String(result.outcome) : "";
    const detail = result && result.detail !== undefined && result.detail !== null
        ? schedulerDetailText(result.detail)
        : "";
    schedulerSetText("#scheduler-action-outcome", outcome);
    schedulerSetText("#scheduler-action-detail", detail);
    const box = $("#scheduler-action-result");
    if (box) box.classList.remove("error");
    schedulerToggle($("#scheduler-action-result"), true);
}

function schedulerDetailText(detail) {
    if (typeof detail === "object") {
        try {
            return JSON.stringify(detail);
        } catch {
            return "";
        }
    }
    return String(detail);
}

function renderSchedulerError(err) {
    const message = err && err.message ? String(err.message) : "调度请求失败";
    schedulerSetText("#scheduler-action-outcome", "");
    schedulerSetText("#scheduler-action-detail", message);
    const box = $("#scheduler-action-result");
    if (box) box.classList.add("error");
    schedulerToggle($("#scheduler-action-result"), true);
}

/** An action has been carried out: show what it did and read the read
    model again, so the panel shows the state the command produced. */
async function schedulerActionDone(result) {
    renderSchedulerOutcome(result);
    await loadSchedulerState();
}

/** The four actions.  Each one is a command, and each one goes through
    api() — which is what carries the request guard's X-PDT-Request
    header. */
async function schedulerPromote(planId) {
    try {
        const result = await api(`/plan/${encodeURIComponent(planId)}/scheduler/promote`, { method: "POST" });
        await schedulerActionDone(result);
        return result;
    } catch (err) {
        renderSchedulerError(err);
        throw err;
    }
}

async function schedulerPause(planId) {
    try {
        const result = await api(`/plan/${encodeURIComponent(planId)}/scheduler/pause`, { method: "POST" });
        await schedulerActionDone(result);
        return result;
    } catch (err) {
        renderSchedulerError(err);
        throw err;
    }
}

async function schedulerResume(planId) {
    try {
        const result = await api(`/plan/${encodeURIComponent(planId)}/scheduler/resume`, { method: "POST" });
        await schedulerActionDone(result);
        return result;
    } catch (err) {
        renderSchedulerError(err);
        throw err;
    }
}

async function schedulerStart(planId) {
    try {
        const result = await api(`/plan/${encodeURIComponent(planId)}/scheduler/start`, { method: "POST" });
        await schedulerActionDone(result);
        return result;
    } catch (err) {
        renderSchedulerError(err);
        throw err;
    }
}

/** Run the verification round of a plan again.

    Two commands, in this order.  The first rewinds the round counter
    (the plan's budget is untouched — only the counter moves back), so
    the batch may run again; the second begins the round.  Without the
    first there are no rounds left to run, and the second would be a
    no-op.

    These are *verification* commands, not scheduler commands: a re-run
    does not put the plan back in the queue.  That is the other button.

    The body is JSON.stringify({}) and not {} because api() spreads
    its options straight into fetch() and serialises nothing itself —
    a plain object would reach the server as "[object Object]" and be
    rejected. */
async function schedulerRerunVerification(planId) {
    try {
        await api(`/verification/${encodeURIComponent(planId)}/reset_rounds`, {
            method: "POST",
            body: JSON.stringify({}),
        });
        await api(`/verification/${encodeURIComponent(planId)}/start`, {
            method: "POST",
            body: JSON.stringify({}),
        });
        await schedulerActionDone({ outcome: "applied", detail: "verification_rerun" });
    } catch (err) {
        renderSchedulerError(err);
        throw err;
    }
}

/** Carry out the action a button asks for. */
async function schedulerRunAction(action, planId) {
    try {
        if (action === "promote") {
            await schedulerPromote(planId);
        } else if (action === "pause") {
            await schedulerPause(planId);
        } else if (action === "resume") {
            await schedulerResume(planId);
        } else if (action === "start") {
            await schedulerStart(planId);
        } else if (action === "rerun-verification") {
            await schedulerRerunVerification(planId);
        }
    } catch {
        // The failure is already on screen in the action-result box.
    }
}

/** One listener for every action button: the buttons are drawn by the
    renderers, so they are bound by delegation rather than one by one. */
function bindSchedulerActions() {
    const root = $("#scheduler-view");
    if (!root) return;
    root.addEventListener("click", (event) => {
        const target = event.target;
        const button = target && target.closest ? target.closest(".scheduler-action") : null;
        if (!button) return;
        event.preventDefault();
        schedulerRunAction(button.dataset.action, button.dataset.plan);
    });
}

// --- Event Bindings ---

function goHome() {
    currentPlanId = null;
    currentPlanState = null;
    interviewStarted = false;
    renderSteps(false, false);
    setStep("interview");
    show("plans-view");
    loadPlans();
    loadSchedulerState();
    updateSidebarActiveItem(null);
    stopSchedulerPolling();
}

function init() {
    // Sidebar home button
    $("#btn-sidebar-home").addEventListener("click", goHome);

    // New plan button (now in sidebar)
    $("#btn-new-plan").addEventListener("click", () => {
        currentPlanId = null;
        interviewStarted = false;
        renderSteps(false, false);
        setStep("interview");
        show("interview-view");
        updateSidebarActiveItem(null);
        $("#interview-chat").innerHTML = `
            <div class="chat-msg system">
                <div class="msg-label">Interviewer</div>
                请描述你的需求（一句话即可）
            </div>`;
        $("#interview-complete-bar").classList.add("hidden");
        $(".input-area").classList.remove("hidden");
        $("#interview-input").focus();
    });

    // Send interview message
    $("#btn-send-interview").addEventListener("click", async () => {
        const input = $("#interview-input");
        const text = input.value.trim();
        if (!text) return;
        input.value = "";

        if (!interviewStarted && !currentPlanId) {
            interviewStarted = true;
            await startInterview(text);
        } else if (currentPlanId) {
            await continueInterview(text);
        }
    });

    $("#interview-input").addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            $("#btn-send-interview").click();
        }
    });

    // Interview complete: Generate PRD
    $("#btn-generate-prd").addEventListener("click", async () => {
        await generatePrd();
    });

    // Interview complete: back to plans
    $("#btn-to-home-from-interview").addEventListener("click", goHome);

    // Navigation: PRD
    $("#btn-back-interview").addEventListener("click", async () => {
        try {
            const interview = await api(`/interview/${currentPlanId}`);
            showInterview(currentPlanId, interview);
        } catch {
            showInterview(currentPlanId);
        }
    });
    $("#btn-to-review").addEventListener("click", () => {
        showReviewView(currentPlanId);
    });
    $("#btn-regen-prd").addEventListener("click", regenPrd);

    // Navigation: PRD Review
    $("#btn-back-prd").addEventListener("click", () => {
        showPrdView(currentPlanId);
    });
    $("#btn-regen-prd-in-review").addEventListener("click", regenPrd);
    $("#btn-accept-all-prd").addEventListener("click", () => {
        if (prdReviewController) prdReviewController.acceptAll();
    });
    $("#btn-to-arch-option").addEventListener("click", async () => {
        try {
            const reviewData = await api(`/review/${currentPlanId}/items`);
            const allReviewed = reviewData.items.every((i) => i.status !== "pending");
            const hasRejected = reviewData.items.some((i) => i.status === "rejected");
            if (!allReviewed) {
                alert("请先完成所有决策点的审阅");
                return;
            }
            if (hasRejected) {
                $("#prd-rejected-count").textContent = reviewData.items.filter((i) => i.status === "rejected").length;
                show("prd-refine-view");
                await refinePRD();
            } else {
                showArchOptionView(currentPlanId);
            }
        } catch {
            showArchOptionView(currentPlanId);
        }
    });

    // PRD Refinement done
    $("#btn-prd-refine-done").addEventListener("click", () => {
        showReviewView(currentPlanId);
    });

    // Architecture Option
    $("#btn-skip-arch").addEventListener("click", async () => {
        await api(`/plan/${currentPlanId}/state`, {
            method: "POST",
            body: JSON.stringify({ arch_enabled: false }),
        });
        await loadPlanState(currentPlanId);
        const problems = await checkAllReviewsComplete();
        if (problems.length > 0) {
            alert(`以下审阅还有未完成的项，请先完成审阅再生成任务：\n\n${problems.join("\n")}`);
            return;
        }
        showTasksView(currentPlanId);
    });
    $("#btn-enable-arch").addEventListener("click", async () => {
        await generateArch();
    });

    // Navigation: Architecture
    $("#btn-back-prd-from-arch").addEventListener("click", () => {
        showPrdView(currentPlanId);
    });
    $("#btn-to-arch-review").addEventListener("click", () => {
        showArchReviewView(currentPlanId);
    });
    $("#btn-regen-arch").addEventListener("click", regenArch);

    // Navigation: Architecture Review
    $("#btn-back-arch").addEventListener("click", () => {
        showArchView(currentPlanId);
    });
    $("#btn-regen-arch-in-review").addEventListener("click", regenArch);
    $("#btn-accept-all-arch").addEventListener("click", () => {
        if (archReviewController) archReviewController.acceptAll();
    });
    $("#btn-to-test-option").addEventListener("click", async () => {
        try {
            const reviewData = await api(`/arch/${currentPlanId}/review/items`);
            const allReviewed = reviewData.items.every((i) => i.status !== "pending");
            const hasRejected = reviewData.items.some((i) => i.status === "rejected");
            if (!allReviewed) {
                alert("请先完成所有架构决策点的审阅");
                return;
            }
            if (hasRejected) {
                $("#arch-rejected-count").textContent = reviewData.items.filter((i) => i.status === "rejected").length;
                show("arch-refine-view");
                await refineArch();
            } else {
                showTestOptionView(currentPlanId);
            }
        } catch {
            showTestOptionView(currentPlanId);
        }
    });

    // Architecture Refinement done
    $("#btn-arch-refine-done").addEventListener("click", () => {
        showArchReviewView(currentPlanId);
    });

    // Test Design Option
    $("#btn-skip-test").addEventListener("click", async () => {
        await api(`/plan/${currentPlanId}/state`, {
            method: "POST",
            body: JSON.stringify({ test_enabled: false }),
        });
        await loadPlanState(currentPlanId);
        const problems = await checkAllReviewsComplete();
        if (problems.length > 0) {
            alert(`以下审阅还有未完成的项，请先完成审阅再生成任务：\n\n${problems.join("\n")}`);
            return;
        }
        showTasksView(currentPlanId);
    });
    $("#btn-enable-test").addEventListener("click", async () => {
        await generateTestDesign();
    });

    // Navigation: Test Design
    $("#btn-back-arch-from-test").addEventListener("click", () => {
        showArchView(currentPlanId);
    });
    $("#btn-to-test-review").addEventListener("click", () => {
        showTestReviewView(currentPlanId);
    });
    $("#btn-regen-test").addEventListener("click", regenTestDesign);

    // Navigation: Test Review
    $("#btn-back-test").addEventListener("click", () => {
        showTestView(currentPlanId);
    });
    $("#btn-regen-test-in-review").addEventListener("click", regenTestDesign);
    $("#btn-accept-all-test").addEventListener("click", () => {
        if (testReviewController) testReviewController.acceptAll();
    });
    $("#btn-to-tasks-from-test").addEventListener("click", async () => {
        try {
            const reviewData = await api(`/test/${currentPlanId}/review/items`);
            const allReviewed = reviewData.items.every((i) => i.status !== "pending");
            const hasRejected = reviewData.items.some((i) => i.status === "rejected");
            if (!allReviewed) {
                alert("请先完成所有测试决策点的审阅");
                return;
            }
            if (hasRejected) {
                $("#test-rejected-count").textContent = reviewData.items.filter((i) => i.status === "rejected").length;
                show("test-refine-view");
                await refineTestDesign();
            } else {
                await generateTasks();
            }
        } catch {
            await generateTasks();
        }
    });

    // Test Refinement done
    $("#btn-test-refine-done").addEventListener("click", () => {
        showTestReviewView(currentPlanId);
    });

    // Execution
    safeBind("#btn-start-execution", "click", showExecutionPanel);
    safeBind("#btn-confirm-execute", "click", startExecution);
    safeBind("#project-dir-input", "keydown", (e) => {
        if (e.key === "Enter") startExecution();
    });
    safeBind("#btn-stop-execution", "click", stopExecution);
    safeBind("#btn-restart-execution", "click", () => {
        $("#execution-setup").classList.remove("hidden");
        $("#execution-controls").classList.add("hidden");
        $("#execution-logs").classList.add("hidden");
        $("#execution-logs").textContent = "";
        setExecBadge("not_started");
    });

    // Verification
    safeBind("#btn-start-verification", "click", startVerification);
    safeBind("#btn-stop-verification", "click", stopVerification);
    safeBind("#btn-execute-all-repairs", "click", executeAllRepairs);
    safeBind("#btn-custom-execute-repairs", "click", customExecuteRepairs);

    // Navigation: Tasks
    $("#btn-back-tasks").addEventListener("click", () => {
        if (currentPlanState && currentPlanState.flags.test_enabled) {
            showTestReviewView(currentPlanId);
        } else if (currentPlanState && currentPlanState.flags.arch_enabled) {
            showArchReviewView(currentPlanId);
        } else {
            showReviewView(currentPlanId);
        }
    });
    $("#btn-to-home").addEventListener("click", goHome);

    // Scheduler view
    safeBind("#btn-open-scheduler", "click", () => {
        show("scheduler-view");
        loadSchedulerState();
        startSchedulerPolling();
    });
    safeBind("#btn-refresh-scheduler", "click", () => {
        loadSchedulerState();
    });
    safeBind("#btn-scheduler-back-home", "click", goHome);
    // The capacity knob: the picked number is written to the server,
    // and the reset puts the configured capacity back.
    safeBind("#scheduler-capacity-apply", "click", async () => {
        const select = $("#scheduler-capacity-select");
        const total = select ? Number(select.value) : NaN;
        if (!Number.isFinite(total)) return;
        try {
            await schedulerApplyCapacity(total);
        } catch {
            // The failure is already on screen in the action-result box.
        }
    });
    safeBind("#scheduler-capacity-reset", "click", async () => {
        try {
            await schedulerApplyCapacity(null);
        } catch {
            // The failure is already on screen in the action-result box.
        }
    });
    bindSchedulerActions();

    // Initial load
    renderSteps(false, false);
    loadPlans();
    loadSidebar();
    loadSchedulerState();
}

document.addEventListener("DOMContentLoaded", init);

// Registered here, at the top level of the module, and not inside
// init(): init() is re-callable, and a listener registered per call
// would run the handler once per registration — another refresh and
// another timer every time the tab is switched.
document.addEventListener("visibilitychange", handleSchedulerVisibilityChange);

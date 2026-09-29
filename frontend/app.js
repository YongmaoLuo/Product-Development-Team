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
    return { new: "新建", interviewed: "需求收集", prd: "PRD已生成", reviewed: "审阅中", ready: "就绪" }[status] || status;
}

function phaseClass(status) {
    const known = ["new", "interviewed", "prd", "reviewed", "ready"];
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
function esc(s) { return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

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
                { new: "新建", interviewed: "已访谈", prd: "已生成PRD", reviewed: "已审阅", ready: "就绪" }[p.status] || p.status
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
    } else if (phase === "test_approved" || phase === "tasks_generation" || phase === "ready" || phase === "executing" || phase.startsWith("verification")) {
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
    const labels = { running: "执行中", completed: "已完成", failed: "失败", stopped: "已停止", not_started: "未开始" };
    badge.textContent = labels[status] || status;
    badge.className = `exec-badge ${status}`;
}

function appendExecLogs(lines) {
    const el = $("#execution-logs");
    const wasAtBottom = el.scrollHeight - el.scrollTop <= el.clientHeight + 40;
    el.textContent = lines.join("\n");
    if (wasAtBottom) el.scrollTop = el.scrollHeight;
}

async function startExecution() {
    const projectDir = $("#project-dir-input").value.trim();
    if (!projectDir) {
        alert("请输入目标项目目录路径");
        return;
    }
    if (!currentPlanId) {
        alert("尚未选择计划");
        return;
    }
    showLoading("正在启动执行...");
    try {
        await api(`/execution/${currentPlanId}/start`, {
            method: "POST",
            body: JSON.stringify({ project_dir: projectDir }),
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
            setExecBadge(data.status);
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
        if (data.status === "not_started") return;
        showExecutionPanel();
        setExecBadge(data.status);
        appendExecLogs(data.logs);
        $("#project-dir-input").value = data.project_dir || "";
        $("#execution-setup").classList.add("hidden");
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
    const canStartVerification = ["ready", "executing"].includes(planPhase);
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
        const data = await api("/tasks/" + planId);
        renderTasks(data);
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

// --- Event Bindings ---

function goHome() {
    currentPlanId = null;
    currentPlanState = null;
    interviewStarted = false;
    renderSteps(false, false);
    setStep("interview");
    show("plans-view");
    loadPlans();
    updateSidebarActiveItem(null);
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

    // Initial load
    renderSteps(false, false);
    loadPlans();
    loadSidebar();
}

document.addEventListener("DOMContentLoaded", init);

"""The framework-fix lane needs its own column, its own badge, and a
question before a dismissal is written.

Why this gate exists
--------------------
The scheduler's read model grew a partition: ``GET /api/scheduler/state``
answers with a ``framework_fixes`` list — the lane's own repair plans —
and the four ordinary lists (``queue`` / ``running`` / ``waiting`` /
``parked``) no longer name them.  A repair plan is in the framework-fix
column or in the ordinary one, never in both, because a panel that
showed the same plan twice would show the operator two different
things.

Three things in ``frontend/`` therefore have to be true, and none of them
fails loudly when it is not:

1. **The column is drawn.**  A panel that ignores ``framework_fixes``
   shows the operator a scheduler with no repair plans on it at all,
   and the operator cannot tell "there are none" from "the panel does
   not draw them".
2. **The badge says which state the entry is in.**  The five states are
   a **closed** set — ``framework_fix.read_model.DISPLAY_STATES`` — and
   a badge that renders an unlabelled state token tells the operator
   nothing.  The words are the backend's; the frontend only says which
   of them a row is in, so the two cannot drift.
3. **A dismissal is confirmed first.**  The endpoint refuses a
   dismissal nobody can explain (``missing_reason``) and refuses one
   while the defect is still being repaired (409).  The frontend's half
   of that contract is the question: the operator says why, and
   cancelling writes nothing at all.

The scan target
---------------
``frontend/app.js``, ``frontend/index.html`` and ``frontend/style.css``,
read as source.  These are shape gates, like the rest of
``static_gates/``: they run on a plain Python environment with no JS
toolchain, so they pin the contract in the source rather than executing
the page.  A badge class with no CSS rule renders as an unstyled pill,
which is why the stylesheet is read too.

One test runs the page's own functions instead of reading them
----------------------------------------------------------------
A shape gate can say that the ordinary lists are *filtered*; it cannot
say that they come back filtered — a function that filters and then
throws the result away reads exactly the same.  So the fourth test
loads ``frontend/app.js`` into a sandbox with a minimal DOM and asks
the page's own functions what they do: an empty partition hides the
column, a cancelled question writes nothing, a refusal leaves the
entry alone, and the ordinary lists come back without the lane's
plans.  A machine without node skips it, the way the sibling gate that
runs ``node --check`` does.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"
_STYLE_CSS = _REPO_ROOT / "frontend" / "style.css"

#: ``backend/`` on the import path, so the closed vocabulary is read
#: from the module that owns it rather than copied into this file.
_BACKEND_DIR = _REPO_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from framework_fix.read_model import DISPLAY_STATES  # noqa: E402

#: The name of the map that gives a display state its badge word.
_BADGE_MAP = "FRAMEWORK_FIX_BADGE_LABELS"

#: The endpoint a dismissal is written to.
_DISMISS_PATH = "/framework-fix/dismiss"

#: The confirmation the dismissal goes through.
_CONFIRM_FUNCTION = "confirmFrameworkFixDismiss"

#: The helper that takes a dismissed entry off the panel.
_REMOVE_FUNCTION = "frameworkFixRemoveEntry"

#: The ordinary lists the lane's own plans must not appear in.
_ORDINARY_LISTS = ("queue", "running", "waiting", "parked")

#: The cache-busting floors for the two static assets.  The numbers are
#: the ones the page carried **before** the framework-fix column
#: existed, so the assertion is "the version moved", not "the version
#: is this exact number" — a later bump does not make the gate red.
_ASSET_VERSION_FLOORS = {
    "app.js": 21,
    "style.css": 18,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(path: Path) -> str:
    """Return the text of ``path`` as UTF-8.

    ``errors="replace"`` keeps a stray byte from turning a shape gate
    into an unhandled ``UnicodeDecodeError`` — a gate that dies on a
    malformed byte reports nothing useful about the contract it was
    written to protect.
    """
    return path.read_text(encoding="utf-8", errors="replace")


#: A top-level function declaration.  ``app.js`` declares all of its
#: functions at column zero, so the anchor is the line start.
_FUNCTION_DECL = re.compile(
    r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{",
    re.MULTILINE,
)


def _function_body(source: str, name: str) -> str:
    """Return the body text of the top-level ``function name(...)``.

    Raises :class:`AssertionError` when the declaration is absent, so a
    rename that nobody updates the gate with fails loudly instead of
    matching nothing and passing.

    Brace counting is naive — it does not know about strings or
    comments — which is adequate for the handful of short, uniformly
    indented functions this file inspects.  Every function this gate
    reads carries a marker that must appear in its body, and the
    callers check for that marker, so a mis-counted body fails loudly
    instead of quietly answering the wrong question.
    """
    matches = [
        candidate
        for candidate in _FUNCTION_DECL.finditer(source)
        if candidate.group(1) == name
    ]
    assert matches, f"function {name}() not found in the source"
    match = matches[0]
    body_start = match.end() - 1  # index of the opening '{'
    depth = 0
    for index in range(body_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[body_start + 1 : index]
    raise AssertionError(f"unbalanced braces in function {name}()")


def _object_literal(source: str, name: str) -> str:
    """Return the body of ``const name = { ... };``.

    The badge vocabulary is a table, not a function, so it needs its
    own reader.  The table is written without braces inside its
    strings, so the same naive counting is enough.
    """
    match = re.search(
        r"(?:const|let|var)\s+" + re.escape(name) + r"\s*=\s*\{",
        source,
    )
    assert match is not None, f"{name} is not declared in the source"
    body_start = match.end() - 1
    depth = 0
    for index in range(body_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[body_start + 1 : index]
    raise AssertionError(f"unbalanced braces in {name}")


def _enclosing_functions(source: str, needle: str) -> list[str]:
    """The names of the functions whose body holds ``needle``.

    The search is by *position*: every occurrence of ``needle`` is
    attributed to the function declaration that precedes it.  That is
    robust against a mis-counted body elsewhere in the file, which a
    whole-file scan would not be.
    """
    names: list[str] = []
    declarations = list(_FUNCTION_DECL.finditer(source))
    for occurrence in re.finditer(re.escape(needle), source):
        owner = None
        for declaration in declarations:
            if declaration.start() > occurrence.start():
                break
            owner = declaration.group(1)
        if owner is not None and owner not in names:
            names.append(owner)
    return names


# ---------------------------------------------------------------------------
# 1. The partition, the badge, and the closed vocabulary
# ---------------------------------------------------------------------------


def test_partition_hooks() -> None:
    """The panel has its own column, and the badge speaks the backend's
    five-state vocabulary.

    The column is a partition, not a filter: the lane's plans are drawn
    in their own section, and the ordinary lists are drawn without
    them, so the same plan is never shown twice.
    """
    js = _source(_APP_JS)
    html = _source(_INDEX_HTML)
    css = _source(_STYLE_CSS)

    # -- the column exists on the page ------------------------------
    assert 'id="framework-fix-panel"' in html, (
        "index.html has no #framework-fix-panel: the lane's own plans "
        "have nowhere to be drawn."
    )
    # It is a block, not a <section>: the scheduler view is read by
    # five sibling gates as "everything up to its first </section>",
    # and a nested <section> would cut the view in half.
    panel_tag = re.search(
        r"<(\w+)[^>]*\bid=\"framework-fix-panel\"[^>]*>", html
    )
    assert panel_tag is not None, (
        "#framework-fix-panel is not an element of the page."
    )
    assert panel_tag.group(1) != "section", (
        "#framework-fix-panel must not be a <section>: the scheduler "
        "view holds no nested <section>, because five sibling gates "
        "read it as everything up to its first </section>."
    )

    # -- and it is drawn from the read model's partition -------------
    assert "function renderFrameworkFixes(" in js, (
        "app.js has no renderFrameworkFixes(): the partition in the "
        "read model is never painted."
    )
    render = _function_body(js, "renderFrameworkFixes")
    assert "framework_fixes" in render, (
        "renderFrameworkFixes() does not read the read model's "
        "framework_fixes partition."
    )
    assert "framework-fix-panel" in render, (
        "renderFrameworkFixes() does not draw into #framework-fix-panel."
    )
    assert "display_state" in render, (
        "renderFrameworkFixes() does not read the row's display_state, "
        "so the badge cannot say which state the entry is in."
    )

    # -- the badge's vocabulary is the backend's closed set -----------
    mapping = _object_literal(js, _BADGE_MAP)
    keys = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", mapping))
    assert keys == set(DISPLAY_STATES), (
        f"{_BADGE_MAP} must carry exactly the backend's closed set of "
        f"display states. missing={sorted(set(DISPLAY_STATES) - keys)} "
        f"extra={sorted(keys - set(DISPLAY_STATES))}"
    )
    for state in sorted(DISPLAY_STATES):
        labelled = re.search(
            re.escape(state) + r'\s*:\s*"([^"]*)"', mapping
        )
        assert labelled is not None, (
            f"{state} has no badge word in {_BADGE_MAP}: a badge that "
            "renders a raw state token tells the operator nothing."
        )
        assert labelled.group(1).strip(), (
            f"{state} has an empty badge word in {_BADGE_MAP}."
        )

    # -- the badge and the dismiss button are on the entry -----------
    assert "ff-badge" in js, "app.js never draws a .ff-badge."
    assert "ff-dismiss" in js, "app.js never draws a .ff-dismiss button."

    # -- and the stylesheet styles both ------------------------------
    assert ".ff-badge" in css, (
        "style.css has no .ff-badge rule: the badge renders as an "
        "unstyled pill."
    )
    for state in sorted(DISPLAY_STATES):
        assert f".ff-badge.{state}" in css, (
            f"style.css has no rule for .ff-badge.{state}: that state's "
            "badge renders in the default colour, and the operator "
            "cannot tell it from any other."
        )
    assert ".ff-dismiss" in css, "style.css has no .ff-dismiss rule."

    # -- the ordinary lists do not carry the lane's plans ------------
    assert "function withoutFrameworkFixes(" in js, (
        "app.js has no withoutFrameworkFixes(): nothing keeps the "
        "lane's plans out of the ordinary lists."
    )
    ordinary = _function_body(js, "withoutFrameworkFixes")
    for key in _ORDINARY_LISTS:
        assert key in ordinary, (
            f"withoutFrameworkFixes() does not filter the {key} list, so "
            "a repair plan can be drawn in the ordinary column as well."
        )
    scheduler = _function_body(js, "renderScheduler")
    assert "withoutFrameworkFixes(" in scheduler, (
        "renderScheduler() does not filter the ordinary lists."
    )
    assert "renderFrameworkFixes(" in scheduler, (
        "renderScheduler() does not draw the framework-fix column, so "
        "the partition is never painted."
    )


# ---------------------------------------------------------------------------
# 2. The dismissal goes through a question
# ---------------------------------------------------------------------------


def test_dismiss_confirm() -> None:
    """A dismissal is written only after the operator confirmed it.

    Asking and writing are two different functions: the writer is the
    only place the endpoint is called, and it is reachable only from
    the asker, which asks first and stops when the answer is empty.
    """
    js = _source(_APP_JS)

    # -- the question exists -----------------------------------------
    assert f"function {_CONFIRM_FUNCTION}(" in js, (
        f"app.js has no {_CONFIRM_FUNCTION}(): a dismissal would be "
        "written without the operator being asked."
    )
    confirm = _function_body(js, _CONFIRM_FUNCTION)
    assert re.search(r"resolve\(\s*null\s*\)", confirm), (
        f"{_CONFIRM_FUNCTION}() never resolves null, so a cancelled "
        "confirmation cannot be told from an accepted one."
    )

    # -- exactly one function writes the dismissal ------------------
    writers = _enclosing_functions(js, _DISMISS_PATH)
    assert len(writers) == 1, (
        f"the dismissal endpoint must be called from exactly one "
        f"function; it is called from {writers}"
    )
    writer = writers[0]
    writer_body = _function_body(js, writer)
    assert _DISMISS_PATH in writer_body, (
        f"the body of {writer}() does not hold the dismissal endpoint; "
        "the attribution is wrong."
    )

    # -- the writer does not ask: asking and writing are separate ---
    assert _CONFIRM_FUNCTION not in writer_body, (
        f"{writer}() both asks and writes: one function that can be "
        "reached without the question is a dismissal nobody confirmed."
    )

    # -- the asker asks first, and stops when the answer is empty ----
    askers = [
        name
        for name in _enclosing_functions(js, f"{_CONFIRM_FUNCTION}(")
        if name != _CONFIRM_FUNCTION
    ]
    assert len(askers) == 1, (
        f"{_CONFIRM_FUNCTION}() must be called from exactly one place; "
        f"it is called from {askers}"
    )
    asker = askers[0]
    asker_body = _function_body(js, asker)
    answer = re.search(
        r"(?:const|let|var)\s+(\w+)\s*=\s*await\s+"
        + re.escape(_CONFIRM_FUNCTION)
        + r"\s*\(",
        asker_body,
    )
    assert answer is not None, (
        f"{asker}() does not take the answer of {_CONFIRM_FUNCTION}() "
        "as a value, so it cannot tell a cancellation from an "
        "acceptance."
    )
    variable = answer.group(1)
    guard = re.search(
        r"if\s*\(\s*!\s*" + re.escape(variable) + r"\s*\)\s*return\b",
        asker_body,
    )
    assert guard is not None, (
        f"{asker}() does not stop when the confirmation came back empty, "
        "so a cancelled confirmation would still write a dismissal."
    )
    assert guard.start() < asker_body.index(f"{writer}("), (
        f"{asker}() writes the dismissal before it checks the answer."
    )

    # -- a refusal leaves the entry alone ----------------------------
    conflict = writer_body.find("409")
    assert conflict != -1, (
        f"{writer}() cannot tell a 409 from any other failure, so the "
        "refusal would not be shown to the operator."
    )
    removal = writer_body.find(f"{_REMOVE_FUNCTION}(")
    assert removal != -1, (
        f"{writer}() never calls {_REMOVE_FUNCTION}(): a dismissed entry "
        "would stay on the panel."
    )
    assert conflict < removal, (
        f"{writer}() removes the entry before it looks at the 409, so a "
        "refused dismissal would take the entry off the panel anyway."
    )
    assert re.search(r"\breturn\b", writer_body[conflict:removal]), (
        f"{writer}() does not return on the 409, so a refused dismissal "
        "would fall through to the removal."
    )

    # -- and the button is wired to the question ---------------------
    assert "function bindFrameworkFixActions(" in js, (
        "app.js has no bindFrameworkFixActions(): the 忽略 button is "
        "drawn but nothing listens to it."
    )
    binding = _function_body(js, "bindFrameworkFixActions")
    assert ".ff-dismiss" in binding, (
        "bindFrameworkFixActions() does not bind the .ff-dismiss button."
    )
    init = _function_body(js, "init")
    assert "bindFrameworkFixActions(" in init, (
        "init() never binds the framework-fix actions, so the 忽略 button "
        "does nothing when it is pressed."
    )


# ---------------------------------------------------------------------------
# 3. The static assets are versioned
# ---------------------------------------------------------------------------


def test_cache_version() -> None:
    """The page asks for the two static assets with a version number.

    The assets changed with the framework-fix column, and a browser
    that has the old ones cached would show the operator a panel
    without the column — a silent failure that looks like "there are
    no repair plans".  The version number is what tells the browser
    that what it has is stale.
    """
    html = _source(_INDEX_HTML)

    for filename, floor in sorted(_ASSET_VERSION_FLOORS.items()):
        match = re.search(re.escape(filename) + r"\?v=(\d+)", html)
        assert match is not None, (
            f"index.html does not reference {filename} with a ?v= version "
            "number: the browser would keep serving the stale asset."
        )
        assert int(match.group(1)) >= floor, (
            f"{filename} is still at version {match.group(1)}; the "
            f"framework-fix column changed it, so the version must be at "
            f"least {floor}."
        )

    # Every local asset is versioned — a version on one file and not on
    # the next one is the same staleness with fewer steps.
    for reference in re.findall(r'(?:src|href)="([^"]+)"', html):
        if reference.startswith(("http://", "https://", "data:", "#", "//")):
            continue
        assert "?v=" in reference, (
            f"{reference} is referenced without a version number."
        )


# ---------------------------------------------------------------------------
# 4. The page's own functions, run
# ---------------------------------------------------------------------------

#: The harness that loads ``frontend/app.js`` into a sandbox with a
#: minimal DOM and reports what the page's own functions do.  It is
#: written out here rather than shipped as its own file so that the
#: gate and the page it checks cannot drift apart.
_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const appPath = process.argv[2];

function makeClassList() {
    const set = new Set();
    return {
        add: (...n) => n.forEach((x) => set.add(x)),
        remove: (...n) => n.forEach((x) => set.delete(x)),
        contains: (n) => set.has(n),
        toggle: (n, force) => {
            const on = force === undefined ? !set.has(n) : Boolean(force);
            if (on) set.add(n); else set.delete(n);
            return on;
        },
    };
}

function makeElement() {
    const element = {
        dataset: {}, children: [], listeners: {}, textContent: "", value: "",
        addEventListener(t, h) { (this.listeners[t] = this.listeners[t] || []).push(h); },
        removeEventListener(t, h) {
            const list = this.listeners[t] || [];
            const at = list.indexOf(h);
            if (at >= 0) list.splice(at, 1);
        },
        remove() {
            if (element.parent) {
                element.parent.children = element.parent.children.filter((c) => c !== element);
                element.parent._sync();
            }
        },
        focus() {}, closest() { return null; },
        querySelector() { return makeElement(); },
        querySelectorAll(sel) { return sel === ".ff-entry" ? element.children.slice() : []; },
        _sync() {
            element._html = element.children.map((c) => c._markup).join("");
        },
    };
    element.classList = makeClassList();
    element._html = "";
    Object.defineProperty(element, "innerHTML", {
        get() { return element._html; },
        set(value) {
            element._html = String(value);
            element.children = [];
            const pattern = /data-plan-id="([^"]*)"/g;
            let match;
            while ((match = pattern.exec(element._html)) !== null) {
                const child = makeElement();
                child.dataset.planId = match[1];
                child.parent = element;
                const start = element._html.lastIndexOf("<div", match.index);
                const end = element._html.indexOf("</div>", match.index);
                child._markup = element._html.slice(start, end + 6);
                element.children.push(child);
            }
        },
    });
    return element;
}

const registry = new Map();
function byId(id) {
    if (!registry.has(id)) registry.set(id, makeElement());
    return registry.get(id);
}

const calls = [];
let responder = () => ({ ok: true, status: 200, body: { dismissed: true } });

const sandbox = {
    console,
    document: {
        querySelector: (sel) => byId(String(sel || "").replace(/^#/, "")),
        querySelectorAll: () => [],
        addEventListener() {}, removeEventListener() {},
        createElement: () => makeElement(),
        body: makeElement(),
    },
    fetch: async (url, options) => {
        calls.push({ url, body: options && options.body });
        const answer = responder();
        return {
            ok: answer.ok,
            status: answer.status,
            statusText: answer.statusText || "",
            json: async () => answer.body,
        };
    },
    setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
    location: { hash: "", href: "http://127.0.0.1:8000/" },
    history: { replaceState() {}, pushState() {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    navigator: { userAgent: "node" },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
sandbox.addEventListener = () => {};
sandbox.removeEventListener = () => {};

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(appPath, "utf8"), sandbox, { filename: appPath });

const panel = byId("framework-fix-panel");
const list = byId("framework-fix-list");
const confirm = byId("framework-fix-confirm");
const input = byId("framework-fix-reason");
const ok = byId("framework-fix-confirm-ok");
const cancel = byId("framework-fix-confirm-cancel");
const notice = byId("framework-fix-notice");

const out = {};

sandbox.renderFrameworkFixes({
    framework_fixes: [{
        plan_id: "fx1",
        display_state: "waiting_on_user_merge",
        signature_summary: "unit test / assert",
        source: { plan_id: "20261123-foo", round: 2 },
        attach_blocked_count: 3,
        pr_url: "https://github.com/acme/fix/pull/42",
    }],
});
out.panelHiddenWithEntry = panel.classList.contains("hidden");
out.badgeClass = (list.innerHTML.match(/class="ff-badge[^"]*"/) || [""])[0];
out.badgeText = (list.innerHTML.match(/ff-badge[^"]*">([^<]*)</) || ["", ""])[1];
out.hasDismissButton = list.innerHTML.indexOf("ff-dismiss") !== -1;
out.entryHasPlanId = list.innerHTML.indexOf("fx1") !== -1;
// The entry as the page draws it — the round the failure came from and
// the pull request that carries the fix are part of the same entry.
out.entryHtml = list.innerHTML;

// The negative control: an entry that names neither a round nor a pull
// request draws neither — and neither does an entry whose round is
// zero.  A field that is missing must not reach the panel as
// "undefined" or as a link to nowhere.
sandbox.renderFrameworkFixes({
    framework_fixes: [
        {
            plan_id: "fx-plain",
            display_state: "waiting_on_user_merge",
            signature_summary: "unit test / assert",
            source: { plan_id: "20261123-bar" },
            attach_blocked_count: 1,
        },
        {
            plan_id: "fx-zero",
            display_state: "waiting_on_user_merge",
            signature_summary: "unit test / assert",
            source: { plan_id: "20261123-zero", round: 0 },
            attach_blocked_count: 1,
        },
    ],
});
out.plainEntryHtml = list.innerHTML;

sandbox.renderFrameworkFixes({ framework_fixes: [] });
out.panelHiddenWhenEmpty = panel.classList.contains("hidden");
out.emptyLeavesNothing = list.innerHTML === "";

const snapshot = {
    framework_fixes: [{ plan_id: "fx1" }],
    queue: [{ plan_id: "fx1" }, { plan_id: "p2" }],
    running: [{ plan_id: "fx1" }, { plan_id: "p3" }],
    waiting: [{ plan_id: "fx1" }, { plan_id: "p4" }],
    parked: [{ plan_id: "fx1" }, { plan_id: "p5" }],
};
const filtered = sandbox.withoutFrameworkFixes(snapshot);
out.ordinaryPlanIds = {
    queue: filtered.queue.map((r) => r.plan_id),
    running: filtered.running.map((r) => r.plan_id),
    waiting: filtered.waiting.map((r) => r.plan_id),
    parked: filtered.parked.map((r) => r.plan_id),
};
out.lanePartitionKept = filtered.framework_fixes.length === 1;

(async () => {
    // A cancelled question writes nothing at all.
    calls.length = 0;
    const pendingCancel = sandbox.frameworkFixDismissClick("fx1");
    cancel.listeners.click[0]();
    await pendingCancel;
    out.cancelCalls = calls.length;

    // An answered question writes the dismissal, with the reason.
    calls.length = 0;
    const pending = sandbox.frameworkFixDismissClick("fx1");
    input.value = " 这条缺陷不再阻塞任何计划 ";
    ok.listeners.click[0]();
    await pending;
    out.confirmCalls = calls.length;
    out.confirmUrl = calls.length ? calls[0].url : "";
    out.confirmBody = calls.length ? calls[0].body : "";

    // A refusal shows the reason and leaves the entry alone.  This
    // scenario has a plan of its own: "fx1" was dismissed above, and a
    // dismissed entry is not drawn again.
    sandbox.renderFrameworkFixes({
        framework_fixes: [{ plan_id: "fx2", display_state: "reopened" }],
    });
    const before = list.innerHTML;
    calls.length = 0;
    responder = () => ({
        ok: false, status: 409, statusText: "Conflict",
        body: { error: "framework_fix_active", fingerprint: "abc" },
    });
    out.refusalReported = (await sandbox.dismissFrameworkFix("fx2", "试试")) === false;
    out.refusalNotice = String(notice.textContent);
    out.entryAfterRefusal = list.innerHTML === before && list.innerHTML.indexOf("fx2") !== -1;

    // A written dismissal takes the entry off the panel.
    responder = () => ({ ok: true, status: 200, body: { dismissed: true } });
    out.dismissalReported = (await sandbox.dismissFrameworkFix("fx2", "试试")) === true;
    out.entryAfterDismissal = list.innerHTML.indexOf("fx2") !== -1;

    // The next poll reads the server again.  The server's read model
    // still lists a dismissed entry (it records the dismissal in the
    // plan's own block, but the partition does not leave it out), so
    // the panel has to keep its own answer — otherwise the entry the
    // operator just dismissed is drawn straight back.
    sandbox.renderFrameworkFixes({
        framework_fixes: [{ plan_id: "fx2", display_state: "reopened" }],
    });
    out.dismissedEntryRedrawn = list.innerHTML.indexOf("fx2") !== -1;
    out.panelHiddenAfterDismissal = panel.classList.contains("hidden");

    process.stdout.write(JSON.stringify(out));
})();
"""


def _run_harness(tmp_path: Path) -> dict:
    """Run the harness against ``frontend/app.js`` and read its answer.

    A machine without node skips the check rather than failing it —
    the same rule the sibling gate that runs ``node --check`` follows.
    """
    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("没有 node，跳过行为检查")

    script = tmp_path / "framework_fix_harness.js"
    script.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(script), str(_APP_JS)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"the harness failed against frontend/app.js:\n{proc.stdout}\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


def test_partition_behaviour(tmp_path: Path) -> None:
    """The page's own functions do what the boundary conditions say.

    The shape gate above can only say that the ordinary lists are
    *filtered*; this one asks the page what the lists come back as.
    """
    out = _run_harness(tmp_path)

    # -- the column is drawn, and hidden while it is empty ------------
    assert out["panelHiddenWithEntry"] is False, (
        "the column stays hidden while it has an entry."
    )
    assert out["panelHiddenWhenEmpty"] is True, (
        "an empty partition does not hide the column, so it takes room "
        "on the panel for nothing."
    )
    assert out["emptyLeavesNothing"] is True, (
        "an empty partition leaves its markup behind."
    )

    # -- the badge says which state the entry is in --------------------
    assert "ff-badge waiting_on_user_merge" in out["badgeClass"], (
        f"the badge does not carry the state token: {out['badgeClass']}"
    )
    assert out["badgeText"], "the badge carries no word."
    assert out["hasDismissButton"] is True, "the entry has no 忽略 button."
    assert out["entryHasPlanId"] is True, "the entry does not carry its plan id."

    # -- the ordinary lists never carry the lane's plans --------------
    assert out["ordinaryPlanIds"] == {
        "queue": ["p2"],
        "running": ["p3"],
        "waiting": ["p4"],
        "parked": ["p5"],
    }, (
        "the ordinary lists still carry the lane's plans: "
        f"{out['ordinaryPlanIds']}"
    )
    assert out["lanePartitionKept"] is True, (
        "the lane's own partition was filtered away with the ordinary "
        "lists."
    )

    # -- a cancelled question writes nothing --------------------------
    assert out["cancelCalls"] == 0, (
        f"a cancelled confirmation still called the endpoint "
        f"{out['cancelCalls']} time(s)."
    )

    # -- an answered question writes the dismissal, with the reason ---
    assert out["confirmCalls"] == 1, (
        f"the confirmed dismissal was written {out['confirmCalls']} time(s)."
    )
    assert out["confirmUrl"] == "/api/plan/fx1/framework-fix/dismiss", (
        f"the dismissal went to {out['confirmUrl']}"
    )
    assert json.loads(out["confirmBody"])["reason"] == "这条缺陷不再阻塞任何计划", (
        f"the dismissal carries {out['confirmBody']}"
    )

    # -- a refusal shows the reason and leaves the entry alone --------
    assert out["refusalReported"] is True, "the refusal was not reported."
    assert out["refusalNotice"], "the refusal was shown as an empty notice."
    assert out["entryAfterRefusal"] is True, (
        "a refused dismissal took the entry off the panel anyway."
    )

    # -- a written dismissal takes the entry off the panel ------------
    assert out["dismissalReported"] is True, "the dismissal was not reported."
    assert out["entryAfterDismissal"] is False, (
        "a dismissed entry is still on the panel."
    )


# ---------------------------------------------------------------------------
# 5. A dismissal stays dismissed
# ---------------------------------------------------------------------------


def test_dismissed_entry_stays_gone(tmp_path: Path) -> None:
    """A dismissed entry is not drawn again by the next poll.

    The panel polls the read model every five seconds, and the read
    model still names a dismissed entry: the dismissal is recorded in
    the plan's own block and in the append-only audit chain, but the
    partition does not leave the entry out.  A panel that draws what it
    is given therefore puts the entry straight back — and the operator
    watches the 忽略 button undo itself, once every five seconds, for
    as long as they sit there.

    So the panel keeps its own answer for the session it is in: an
    entry the operator dismissed is not drawn again.  The server's
    answer still governs everything else — the entry is only hidden
    from the operator who dismissed it.
    """
    out = _run_harness(tmp_path)

    assert out["dismissedEntryRedrawn"] is False, (
        "the next poll drew the dismissed entry again: the dismissal "
        "does not survive a refresh."
    )
    assert out["panelHiddenAfterDismissal"] is True, (
        "the column stayed open with nothing in it: an empty column "
        "takes room on the panel for nothing."
    )


# ---------------------------------------------------------------------------
# 6. The entry says where it came from
# ---------------------------------------------------------------------------


def test_entry_shows_source_round(tmp_path: Path) -> None:
    """The entry says **which round** the failure came from.

    A repair plan is spawned out of a failed round of another plan, and
    the operator has to be able to read that off the panel: "来自
    20261123-foo" alone says which plan failed, not which round of it
    failed — and a plan that fails three times produces three repair
    plans that look identical on the panel.

    The read model carries the round (``source.round``); the entry has
    to draw it.  A round that is missing — the field is not in the
    payload at all, or it is zero — must not reach the operator as
    ``undefined`` or ``NaN``: the entry then says the plan it came from
    and nothing more.
    """
    out = _run_harness(tmp_path)

    entry = out["entryHtml"]
    assert "20261123-foo" in entry, (
        f"the entry does not carry the plan it came from: {entry}"
    )
    assert "轮" in entry, (
        f"the entry does not say which round it came from: {entry}"
    )
    assert re.search(r"第\s*2\s*轮", entry), (
        f"the entry does not carry the round the failure came from (2): "
        f"{entry}"
    )

    # -- the negative control: no round, no round on the panel -------
    # Two rows are drawn: one whose round is missing, one whose round
    # is zero.  Neither may draw a round, and neither may draw
    # "undefined" or "NaN".
    plain = out["plainEntryHtml"]
    assert "第" not in plain and "轮" not in plain, (
        f"an entry without a round still draws one: {plain}"
    )
    assert "undefined" not in plain, (
        f"a missing round reached the panel as 'undefined': {plain}"
    )
    assert "NaN" not in plain, (
        f"a missing round reached the panel as 'NaN': {plain}"
    )


def test_entry_shows_pr_link(tmp_path: Path) -> None:
    """The entry links the pull request that carries the fix.

    The fix is not on the branch the operator is looking at — it is in a
    pull request, and the pull request's address is the only place the
    operator can read the fix itself.  So the entry draws it as a link.

    An entry whose repair has no pull request yet draws no link at all:
    a link to an empty address is a link the operator can click and
    nothing happens, and a missing field must not reach the panel as
    ``undefined``.
    """
    out = _run_harness(tmp_path)

    entry = out["entryHtml"]
    assert "<a" in entry, f"the entry draws no link at all: {entry}"
    assert 'href="https://github.com/acme/fix/pull/42"' in entry, (
        f"the entry does not link the pull request: {entry}"
    )
    assert ">PR</a>" in entry, (
        f"the link carries no readable word: {entry}"
    )
    assert "undefined" not in entry, (
        f"a missing field reached the panel as 'undefined': {entry}"
    )

    # -- the negative control: no pull request, no link ---------------
    plain = out["plainEntryHtml"]
    assert "<a" not in plain, (
        f"an entry without a pull request still draws a link: {plain}"
    )
    assert "undefined" not in plain, (
        f"a missing pull request reached the panel as 'undefined': {plain}"
    )

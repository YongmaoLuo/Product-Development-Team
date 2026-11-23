"""The scheduler panel must show **what** the operator is waiting for.

What this file witnesses
------------------------
Until the "等待中" block existed, the whole of the human-in-the-loop
story on the scheduler page was four characters in the queue's reason
column: 「等待人工回答」.  The question itself was nowhere, the time
left on it was nowhere, and there was no way to act on it from the
page — an operator could only watch a deadline run out.

This is a **shape gate**: it reads the three static assets the browser
loads (``index.html``, ``app.js``, ``style.css``) as source and pins
the contract in them.  The repo has no JS toolchain in its test lanes,
so the contract is pinned in the source rather than executed in a
browser — the real-browser check is the end-to-end task that comes
after this one (test decision point 9).

The contract, in five parts
----------------------------
1. **The block exists.**  ``#scheduler-view`` carries the waiting
   table, its empty state and its body — three elements, because an
   empty waiting list and a waiting list are two different things to
   look at, and one of them has to be hidden for the other to be read.
2. **The renderer exists and is called.**  ``renderScheduler()`` is
   the one place the read model is drawn; a waiting block that is not
   drawn by it is a block that never updates.
3. **Every row carries three operations.**  延长 / 终止 / 回答 are
   three *commands* against three different server paths: two of them
   are the scheduler's own wait endpoints, and the third reuses the
   existing HITL answer path rather than inventing an approval
   channel of its own.
4. **The question keeps its full text.**  The cell may be shortened
   for the eye, but the ``title`` attribute carries the whole original
   question — a truncated question is a question nobody can answer.
5. **The requirement sheet editor is gone.**  The spec editor was
   removed from the panel on 2026-11-23 (the sheet itself is written
   by the pipeline, not by the operator): the markup, the functions and
   the styles are gone together.  The **backend** spec endpoint is
   untouched — the capability stays, the box on the page does not.

Why the row shape is checked on the source
------------------------------------------
The read model's waiting row is a **closed eight-key shape** (see
``plan_scheduler/snapshot.py``'s ``WAITING_KEYS``): ``plan_id``,
``item_id``, ``wait_type``, ``question``, ``deadline``,
``remaining_seconds``, ``extendable``, ``extensions_count``.  The
panel draws those and invents nothing — so a check that looked for a
ninth key would be checking a drift the server cannot produce.

Non-vacuity
------------
Every assertion below reads the *current* asset and compares it with a
literal that is written out here.  A renderer that was renamed, a
block that was deleted, or a save path that was pointed at the wrong
URL all fail loudly: the helper that extracts a function body raises
when the declaration is absent, so a rename cannot pass by matching
nothing.
"""

from __future__ import annotations

import re
from pathlib import Path


#: The repository root.  ``backend/tests/unit/postprocess/`` → up five
#: levels is the checkout, and the frontend assets live at its top.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_FRONTEND_DIR = _REPO_ROOT / "frontend"
_INDEX_HTML = _FRONTEND_DIR / "index.html"
_APP_JS = _FRONTEND_DIR / "app.js"
_STYLE_CSS = _FRONTEND_DIR / "style.css"


#: The three elements the waiting block is made of.  Written out one by
#: one: a set derived from the page cannot witness that the page did
#: not lose one of them.
WAITING_ELEMENT_IDS = (
    "scheduler-waiting-table",
    "scheduler-waiting-empty",
    "scheduler-waiting-body",
)

#: The three operations one waiting row offers, and the action each one
#: carries.  The vocabulary is the server's — the panel does not
#: invent a fourth operation.
WAITING_ACTIONS = ("extend", "terminate", "answer")

#: The CSS classes the waiting block and the spec editor are drawn
#: with.  A class the stylesheet does not know is a class the browser
#: ignores, so the markup and the stylesheet have to name each other.
WAITING_STYLE_CLASSES = (
    ".scheduler-waiting-table",
    ".scheduler-waiting-question",
    ".scheduler-waiting-answer",
)

#: The requirement sheet editor's own classes.  They were removed
#: together with the editor (2026-11-23): a class the page no longer
#: uses is a rule the stylesheet has no reason to keep.
REMOVED_SPEC_STYLE_CLASSES = (
    ".scheduler-spec-editor",
    ".scheduler-spec-text",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source(path: Path) -> str:
    """Return the text of ``path`` as UTF-8.

    ``errors="replace"`` keeps a stray byte from turning a shape gate
    into an unhandled ``UnicodeDecodeError``.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _function_body(source: str, name: str) -> str:
    """Return the body text of the top-level ``function name(...)``.

    Raises :class:`AssertionError` when the declaration is absent, so a
    rename that nobody updates this gate with fails loudly instead of
    matching nothing and passing.

    Brace counting is naive — it does not know about strings or
    comments — which is adequate for the handful of short, uniformly
    indented functions this file inspects.  The functions it reads are
    written without unbalanced braces inside string literals for
    exactly this reason.
    """
    marker = f"function {name}("
    start = source.find(marker)
    assert start != -1, f"前端没有定义 {name}()"
    brace = source.find("{", start + len(marker))
    assert brace != -1, f"{name}() 没有函数体"
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[brace + 1 : index]
    raise AssertionError(f"{name}() 的函数体没有闭合")


def _scheduler_view(html: str) -> str:
    """Return the ``#scheduler-view`` section of the page.

    The section carries no nested ``<section>``, so the first closing
    tag after the opening one ends it.
    """
    start = html.find('<section id="scheduler-view"')
    assert start != -1, "页面没有 #scheduler-view 区块"
    end = html.find("</section>", start)
    assert end != -1, "#scheduler-view 区块没有闭合"
    return html[start:end]


def _asset_version(html: str, filename: str) -> int:
    """Return the ``?v=`` cache-busting number of one static asset."""
    match = re.search(rf'{re.escape(filename)}\?v=(\d+)', html)
    assert match, f"{filename} 没有带版本号"
    return int(match.group(1))


# ---------------------------------------------------------------------------
# 1. The block exists
# ---------------------------------------------------------------------------


def test_index_html_has_waiting_section() -> None:
    """The scheduler view carries the waiting table, its empty state
    and its body.

    All three, and all three **inside** ``#scheduler-view``: a
    waiting table that lives on another view is a waiting table the
    operator never sees while looking at the scheduler.
    """
    html = _source(_INDEX_HTML)
    view = _scheduler_view(html)
    for element_id in WAITING_ELEMENT_IDS:
        assert f'id="{element_id}"' in html, f"页面缺少 {element_id}"
        assert f'id="{element_id}"' in view, f"{element_id} 不在 #scheduler-view 里"


# ---------------------------------------------------------------------------
# 2. The renderer exists and is called
# ---------------------------------------------------------------------------


def test_app_js_defines_render_scheduler_waiting() -> None:
    """``renderSchedulerWaiting()`` is defined.

    The read model's waiting list has exactly one renderer, so the
    panel cannot grow a second answer to "what is the system waiting
    for".
    """
    js = _source(_APP_JS)
    assert "function renderSchedulerWaiting(" in js, (
        "前端没有定义 renderSchedulerWaiting()"
    )


def test_render_scheduler_calls_waiting_renderer() -> None:
    """``renderScheduler()`` draws the waiting list.

    ``renderScheduler()`` is the one place the read model is painted,
    so a waiting block drawn by anything else would not follow the
    five-second poll.
    """
    body = _function_body(_source(_APP_JS), "renderScheduler")
    assert "renderSchedulerWaiting(" in body, (
        "renderScheduler() 没有调用 renderSchedulerWaiting()"
    )


def test_waiting_renderer_reads_the_snapshot_waiting_list() -> None:
    """The renderer reads ``snapshot.waiting`` — the list the read
    model carries — and hides the table when there is nothing in it.

    An empty waiting list is an empty state, not an error: "nothing
    is waiting" is a normal thing to look at.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerWaiting")
    assert "waiting" in body, "renderSchedulerWaiting() 没有读 snapshot.waiting"
    assert "#scheduler-waiting-table" in body, (
        "renderSchedulerWaiting() 没有切换等待表格的显隐"
    )
    assert "#scheduler-waiting-empty" in body, (
        "renderSchedulerWaiting() 没有切换等待空状态的显隐"
    )


# ---------------------------------------------------------------------------
# 3. Every row carries three operations
# ---------------------------------------------------------------------------


def test_waiting_row_offers_three_actions() -> None:
    """Every waiting row carries 延长 / 终止 / 回答.

    The three operations are the three the architecture decided on
    (arch decision point 6): two of them are the scheduler's own wait
    endpoints, and the third is the existing HITL answer path — a
    panel that grew its own approval channel would be a second answer
    to the same question.

    The markup lives in ``schedulerWaitingActions()`` since
    2026-11-23: the waiting table and the finishing block draw the
    same three operations, and a second copy of this markup would be
    a second answer to "what can I do about this question".
    """
    helper = _function_body(_source(_APP_JS), "schedulerWaitingActions")
    for action in WAITING_ACTIONS:
        assert f'data-action="{action}"' in helper, (
            f"等待行没有 {action} 操作"
        )


def test_waiting_row_uses_the_shared_actions() -> None:
    """The waiting table draws its rows through the shared helper.

    A row that built its own buttons would be the second copy the
    helper was extracted to prevent.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerWaiting")
    assert "schedulerWaitingActions(" in body, (
        "renderSchedulerWaiting() 没有复用 schedulerWaitingActions()"
    )


def test_wait_actions_reach_the_server_paths() -> None:
    """The three operations reach the three paths they must reach.

    * 延长 → ``POST /scheduler/{plan_id}/waits/{item_id}/extend``
    * 终止 → ``POST /scheduler/{plan_id}/waits/{item_id}/terminate``
    * 回答 → ``POST /hitl/{plan_id}/decisions/{decision_id}/answer``

    The answer path is the **existing** HITL one: a new approval
    channel would be a second answer to the same question, and the
    operator would have no way of telling which one the server is
    using.
    """
    js = _source(_APP_JS)
    assert "/waits/" in js and "/extend" in js, "没有等待延长的请求路径"
    assert "/waits/" in js and "/terminate" in js, "没有等待终止的请求路径"
    assert re.search(r"/hitl/\$\{[^}]+\}/decisions/\$\{[^}]+\}/answer", js), (
        "回答没有走既有 HITL 回答路径"
    )


def test_answer_button_resolves_the_hitl_decision() -> None:
    """The answer operation finds the HITL decision it answers.

    The waiting row carries the wait's ``item_id``; the HITL decision
    row carries that same id in its ``task_id``.  So the panel looks
    the decision up by that id and answers it — it does not guess a
    decision id, and it does not answer a decision of another plan.
    """
    js = _source(_APP_JS)
    assert "task_id" in js, "面板没有用 item_id 去找对应的 HITL 决策"
    assert "decision_id" in js, "面板没有用决策 id 回答"


# ---------------------------------------------------------------------------
# 4. The question keeps its full text
# ---------------------------------------------------------------------------


def test_question_cell_keeps_the_full_question_in_title() -> None:
    """The question's cell carries the whole question in ``title``.

    The cell may show a shortened question for the eye, but the
    ``title`` attribute holds the original text — a truncated question
    is a question nobody can answer.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerWaiting")
    assert 'title="${escapeHtml(' in body or "title=" in body, (
        "等待行的问题列没有 title 属性"
    )


def test_waiting_row_keeps_the_server_row_shape() -> None:
    """The row is drawn from the read model's own fields.

    The waiting row is a closed eight-key shape; the panel draws
    those and invents nothing.
    """
    body = _function_body(_source(_APP_JS), "renderSchedulerWaiting")
    for field in ("plan_id", "item_id", "wait_type", "question", "remaining_seconds"):
        assert field in body, f"等待行没有画 {field}"


# ---------------------------------------------------------------------------
# 5. The requirement sheet can be read and written
# ---------------------------------------------------------------------------


def test_spec_edit_entry_is_gone() -> None:
    """The requirement sheet editor is gone from the panel.

    Removed on 2026-11-23: the sheet is written by the pipeline, and a
    box that lets an operator retype it only invites the two of them
    to disagree.  The backend spec endpoint stays — only the editor
    on the page is gone.
    """
    html = _source(_INDEX_HTML)
    js = _source(_APP_JS)

    assert 'id="scheduler-spec-editor"' not in html, "调度页还留着要求书编辑区"
    assert "scheduler-spec-editor" not in html, "调度页还留着要求书编辑区的引用"
    assert "function savePostprocessSpec(" not in js, (
        "前端还留着 savePostprocessSpec()"
    )
    assert "function loadPostprocessSpec(" not in js, (
        "前端还留着 loadPostprocessSpec()"
    )
    assert "#btn-scheduler-spec-load" not in js, "前端还绑着要求书读取按钮"
    assert "#btn-scheduler-spec-save" not in js, "前端还绑着要求书保存按钮"


# ---------------------------------------------------------------------------
# The styles
# ---------------------------------------------------------------------------


def test_style_has_waiting_table_rules() -> None:
    """The stylesheet knows the waiting block.

    A class the stylesheet does not know is a class the browser
    ignores, so the markup and the stylesheet have to name each other.
    """
    css = _source(_STYLE_CSS)
    for selector in WAITING_STYLE_CLASSES:
        assert selector in css, f"样式表缺少 {selector} 的规则"


def test_spec_editor_rules_are_gone() -> None:
    """The spec editor's rules went with the editor.

    A rule for a box that is no longer on the page is a rule nobody
    can see take effect.
    """
    css = _source(_STYLE_CSS)
    for selector in REMOVED_SPEC_STYLE_CLASSES:
        assert selector not in css, f"样式表还留着 {selector} 的规则"


def test_asset_versions_are_incremented() -> None:
    """The two static assets carry a cache-busting version, and it is
    past the version the page carried before this block was added.

    A browser that already has ``app.js`` cached would otherwise keep
    running the old renderer — and the old renderer knows nothing
    about the waiting list, so the block would simply never appear.
    """
    html = _source(_INDEX_HTML)
    assert _asset_version(html, "app.js") >= 19, "app.js 的版本号没有递增"
    assert _asset_version(html, "style.css") >= 16, "style.css 的版本号没有递增"

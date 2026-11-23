"""The scheduler panel has a **finishing** section, and the requirement
sheet editor is gone from it.

What this file witnesses
------------------------
A plan that is being finished is in no queue and in no running line:
it holds no slot and waits for no capacity, so the read model the
panel polls does not carry it at all.  Before this change the
operator could not see such a plan on the panel — it was simply not
there.  The panel now has a section of its own for it, and the section
answers the two things an operator asks about a finishing plan:

* **where is it** — 收尾中 or 收尾失败;
* **what is it waiting on** — the questions, with the same three
  operations (延长 / 终止 / 回答) the waiting table offers.

The second half of the file witnesses the other half of the change:
the requirement sheet editor is removed.  The sheet itself is written
by the pipeline; a box that lets an operator retype it only invites
the two of them to disagree.  The editor's markup, its functions and
its styles go together — and the **backend** spec endpoint stays
untouched: the capability is kept, the box on the page is not.

Why this is a shape gate
------------------------
The repository has no JS toolchain in its test lanes, so the contract
is pinned in the static assets the browser loads (``index.html``,
``app.js``, ``style.css``) as source.  Every assertion reads the
current asset and compares it with a literal written out here, so a
renamed renderer or a deleted section fails loudly instead of matching
nothing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


#: The repository root.  ``backend/tests/static_gates/`` → up three
#: levels is the checkout, and the frontend assets live at its top.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FRONTEND_DIR = _REPO_ROOT / "frontend"
_INDEX_HTML = _FRONTEND_DIR / "index.html"
_APP_JS = _FRONTEND_DIR / "app.js"
_STYLE_CSS = _FRONTEND_DIR / "style.css"


#: The two elements the finishing section is made of.  An empty
#: finishing list and a finishing list are two different things to
#: look at, and one of them has to be hidden for the other to be read.
FINISHING_ELEMENT_IDS = (
    "scheduler-finishing-empty",
    "scheduler-finishing-cards",
)

#: The functions the finishing section is drawn and read by.
FINISHING_FUNCTIONS = (
    "renderSchedulerFinishing",
    "loadSchedulerFinishing",
)

#: The functions that carry the panel's own address.
URL_FUNCTIONS = (
    "schedulerSyncUrl",
    "handleSchedulerHashChange",
)

#: The hash the scheduler view is addressed by.
SCHEDULER_HASH = "#/scheduler"


def _source(path: Path) -> str:
    """Return the text of ``path`` as UTF-8."""
    return path.read_text(encoding="utf-8", errors="replace")


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


# ---------------------------------------------------------------------------
# The finishing section
# ---------------------------------------------------------------------------


def test_index_html_has_the_finishing_section() -> None:
    """The panel carries the finishing section, inside the scheduler view.

    A finishing section that lives on another view is a finishing
    section the operator never sees while looking at the panel.
    """
    html = _source(_INDEX_HTML)
    view = _scheduler_view(html)
    for element_id in FINISHING_ELEMENT_IDS:
        assert f'id="{element_id}"' in html, f"页面缺少 {element_id}"
        assert f'id="{element_id}"' in view, f"{element_id} 不在 #scheduler-view 里"


def test_index_html_labels_the_finishing_section() -> None:
    """The section says what it is: 「收尾」.

    A section without a title of its own is a list of cards with no
    name, and the operator has to guess what they are looking at.
    """
    view = _scheduler_view(_source(_INDEX_HTML))
    assert "收尾</h3>" in view, "收尾分区没有自己的标题"


def test_finishing_section_sits_between_the_queue_and_the_waiting() -> None:
    """The section is drawn after 「队列」 and before 「等待中」.

    The order is the reading order: what is coming, what is being
    finished, what is being waited on.
    """
    view = _scheduler_view(_source(_INDEX_HTML))
    queue = view.find("scheduler-queue")
    finishing = view.find('id="scheduler-finishing-cards"')
    waiting = view.find('id="scheduler-waiting-table"')
    assert -1 not in (queue, finishing, waiting), "调度视图里缺了一块"
    assert queue < finishing < waiting, "收尾分区不在队列与等待中之间"


def test_app_js_defines_the_finishing_renderer() -> None:
    """The finishing section is drawn by its own renderer.

    A section that were drawn by the read model's renderer would be a
    section the read model cannot carry — and the read model does not
    carry finishing plans.
    """
    js = _source(_APP_JS)
    for name in FINISHING_FUNCTIONS:
        assert f"function {name}(" in js, f"前端没有定义 {name}()"


def test_finishing_renderer_draws_both_containers() -> None:
    """The renderer draws the cards and hides the empty state.

    Both, and in both directions: a list of cards with the empty state
    still on screen is a panel that says two things at once.
    """
    js = _source(_APP_JS)
    body = js[js.index("function renderSchedulerFinishing("):]
    body = body[: body.index("\nfunction ")]
    assert "#scheduler-finishing-cards" in body, (
        "renderSchedulerFinishing() 没有画收尾卡片"
    )
    assert "#scheduler-finishing-empty" in body, (
        "renderSchedulerFinishing() 没有切换收尾空状态"
    )


def test_finishing_read_rides_along_with_the_state_read() -> None:
    """The finishing read is fired from the panel's own read.

    The panel polls one read model every five seconds; the finishing
    section has to be refreshed by that same poll, or it would show
    what was true when the panel was opened.
    """
    js = _source(_APP_JS)
    body = js[js.index("async function loadSchedulerState("):]
    body = body[: body.index("\nfunction ")]
    assert "loadSchedulerFinishing(" in body, (
        "loadSchedulerState() 没有带出收尾分区的读取"
    )


def test_finishing_read_failure_does_not_paint_over_the_panel() -> None:
    """A failed finishing read never calls ``renderSchedulerError()``.

    The finishing section is an addition to the panel; a failure of an
    addition is not a failure of the panel.
    """
    js = _source(_APP_JS)
    body = js[js.index("async function loadSchedulerFinishing("):]
    body = body[: body.index("\nfunction ")]
    assert "catch" in body, "loadSchedulerFinishing() 没有处理失败"
    assert "renderSchedulerError" not in body, (
        "loadSchedulerFinishing() 的失败会盖掉整个面板"
    )


def test_style_knows_the_finishing_section() -> None:
    """The stylesheet knows the finishing section.

    A class the stylesheet does not know is a class the browser
    ignores, so the markup and the stylesheet have to name each other.
    """
    css = _source(_STYLE_CSS)
    assert ".scheduler-finishing-card" in css, "样式表缺少 .scheduler-finishing-card 的规则"
    assert ".scheduler-finishing-badge" in css, "样式表缺少 .scheduler-finishing-badge 的规则"


# ---------------------------------------------------------------------------
# The requirement sheet editor is gone
# ---------------------------------------------------------------------------


def test_the_spec_editor_is_gone_from_the_page() -> None:
    """The page no longer carries the requirement sheet editor.

    Its markup, its buttons and its own styles went together — a half
    removed editor is a broken box, and a box nobody uses is a lie the
    page tells.
    """
    html = _source(_INDEX_HTML)
    js = _source(_APP_JS)
    css = _source(_STYLE_CSS)

    assert "scheduler-spec" not in html, "页面还留着要求书编辑区"
    assert "scheduler-spec" not in js, "前端还留着要求书的代码"
    assert "scheduler-spec" not in css, "样式表还留着要求书的样式"
    assert "要求书" not in _scheduler_view(html), "调度视图里还留着要求书"


def test_the_spec_editor_saved_nothing_behind_the_panel() -> None:
    """Nothing in the panel's own code writes the spec any more.

    The backend endpoint stays (the capability is kept); the panel's
    own write path is gone with the box.
    """
    js = _source(_APP_JS)
    assert "postprocess-spec" not in js, "前端还留着要求书的写入路径"


# ---------------------------------------------------------------------------
# The panel has an address of its own
# ---------------------------------------------------------------------------


def test_app_js_defines_the_url_helpers() -> None:
    """The URL helpers exist."""
    js = _source(_APP_JS)
    for name in URL_FUNCTIONS:
        assert f"function {name}(" in js, f"前端没有定义 {name}()"


def test_the_scheduler_view_is_addressed_by_its_hash() -> None:
    """The scheduler view is addressed by ``#/scheduler``.

    A view that cannot be addressed cannot be come back to by an
    address — and a refresh of a panel that has no address is a
    refresh that loses the panel.
    """
    js = _source(_APP_JS)
    assert SCHEDULER_HASH in js, "前端没有把调度视图写成 #/scheduler"


def test_the_address_is_written_with_replace_state() -> None:
    """The address is written with ``replaceState``, never ``pushState``.

    The browser's back button must keep meaning "leave the page" and
    not "walk through the panel's own history".
    """
    js = _source(_APP_JS)
    body = js[js.index("function schedulerSyncUrl("):]
    body = body[: body.index("\nfunction ")]
    assert "replaceState(" in body, "schedulerSyncUrl() 没有用 replaceState"
    assert "pushState(" not in body, "schedulerSyncUrl() 用了 pushState"


def test_the_address_change_does_not_loop() -> None:
    """Writing the address does not start a loop.

    ``replaceState`` fires neither ``hashchange`` nor ``popstate``,
    and the listener is on ``hashchange`` alone: the two cannot call
    each other back and forth.
    """
    js = _source(_APP_JS)
    assert "addEventListener(\"hashchange\"" in js, "前端没有监听 hashchange"
    assert "addEventListener(\"popstate\"" not in js, "前端还监听了 popstate"


def test_the_panel_is_restored_from_the_address_on_start_up() -> None:
    """A page opened at ``#/scheduler`` shows the panel.

    This is the whole point of the address: a refresh of the panel
    must not lose the panel.
    """
    js = _source(_APP_JS)
    assert "window.location.hash === SCHEDULER_HASH" in js, (
        "启动时没有按地址恢复调度视图"
    )


# ---------------------------------------------------------------------------
# The script still parses
# ---------------------------------------------------------------------------


def test_app_js_parses() -> None:
    """``node --check`` accepts the script.

    The browser's parser is the one that matters; ``node --check`` is
    the closest thing this lane has to it.  A machine without node
    skips the check rather than failing it.
    """
    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("没有 node，跳过语法检查")
    proc = subprocess.run(
        [node, "--check", str(_APP_JS)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"node --check frontend/app.js 失败：\n{proc.stdout}\n{proc.stderr}"
    )

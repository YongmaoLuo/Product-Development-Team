"""The scheduler view must keep itself current while it is open.

Why this gate exists
--------------------
The scheduler panel answers the question "why is this plan not
running", and that question is answered by a read model the backend
changes under the operator's feet: a plan is queued, then it is
running, then it fails.  The panel used to read that model exactly
once — when the view was opened — and the only way to read it again
was the refresh button.  A backend that is working perfectly therefore
looks like a backend that has stopped: the panel keeps showing the
state from the moment it was opened, and the operator has to know to
press the button to find out.

The fix is a timer, and a timer has three ways to go wrong, each of
them invisible in the browser:

1. **No named interval.**  ``setInterval(refresh, 5000)`` hides the
   cadence in a literal, so the number cannot be found, cannot be
   changed, and cannot be asserted on.  The gate pins the interval to
   a named constant instead.
2. **Two timers.**  A timer that is started on every visit and never
   stopped doubles, then triples: the second visit to the panel issues
   two requests per tick, the third issues three.  The panel looks
   fine and the API quietly takes three times the load.  The gate
   requires the start function to clear the previous timer *before*
   it sets a new one.
3. **A reload instead of a redraw.**  The cheapest way to make a
   stale page current is ``location.reload()`` — and it throws away
   the operator's place, their scroll position, and the panel they
   were reading.  The gate requires the refresh to reuse the two
   loaders that already exist, and requires the file to carry no
   ``location.reload`` at all.

The rules
---------
* ``SCHEDULER_POLL_MS`` is a named constant and the timer is created
  with it.
* ``startSchedulerPolling()`` stops the previous timer before it sets
  a new one.
* ``refreshScheduler()`` re-reads the read model through the loader
  that already exists — ``loadSchedulerState()`` — and the file carries
  no ``location.reload``.
* The panel's 「可操作的计划」 block is gone from the markup (it was a
  superset of the two blocks that remain), so the file names neither
  ``loadSchedulerPlans()`` nor ``renderSchedulerPlans()``: a loader for
  a table that is not there any more finds nothing and throws on the
  next line.
* Opening the scheduler view starts the timer; leaving it stops the
  timer; the manual refresh button keeps working exactly as before.

Scan target
-----------
``frontend/app.js``, read as source.  These are shape gates like the
rest of ``static_gates/``: they run on a plain Python environment with
no JS toolchain, so they pin the contract in the source rather than
executing the page.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ``static_gates/`` is on sys.path for this directory's modules, so the
# body-slicing helpers of the sibling scheduler gate are imported
# instead of re-implemented: two slicers would be two judgements about
# the same file, and they would drift.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_scheduler_view_present import _function_body, _source  # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"
_INDEX_HTML = _REPO_ROOT / "frontend" / "index.html"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _binding_body(source: str, selector: str) -> str:
    """Return the text of the ``safeBind("<selector>", ...)`` block.

    The block starts at the ``safeBind("`` that names *selector* and
    runs until the next ``safeBind("`` — the bindings are written one
    after another, so the next one is the end of this one.

    Raises :class:`AssertionError` when the binding is absent, so a
    renamed button fails loudly instead of matching nothing and
    passing.
    """
    marker = f'safeBind("{selector}"'
    start = source.find(marker)
    assert start != -1, f'safeBind("{selector}") not found in the source'
    end = source.find('safeBind("', start + len(marker))
    if end == -1:
        end = len(source)
    return source[start:end]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_poll_interval_is_a_named_constant() -> None:
    """The cadence must be a named constant, and the timer must use it.

    A literal in the ``setInterval`` call is a number nobody can find,
    change, or assert on: the only way to learn how often the panel
    refreshes is to read the line and count.  The constant makes it a
    property of the panel rather than a detail of one call.
    """
    source = _source(_APP_JS)

    assert "const SCHEDULER_POLL_MS = 5000;" in source, (
        "frontend/app.js has no `const SCHEDULER_POLL_MS = 5000;`. The "
        "refresh cadence must be a named constant: the panel's poll "
        "interval is a property of the panel, and a literal inside "
        "the setInterval call cannot be found or changed."
    )

    body = _function_body(source, "startSchedulerPolling")
    assert "setInterval(refreshScheduler, SCHEDULER_POLL_MS)" in body, (
        "startSchedulerPolling() does not create the timer with "
        "setInterval(refreshScheduler, SCHEDULER_POLL_MS). Either the "
        "timer polls something other than refreshScheduler(), or the "
        "interval is a literal instead of the named constant."
    )


def test_one_timer_replaces_the_previous_one() -> None:
    """Starting the timer must clear the previous one first.

    A timer that is started on every visit and never stopped doubles
    on the second visit and triples on the third: the panel still
    looks right, and the API quietly takes three times the load. The
    order matters — the previous timer has to be stopped *before* the
    new one is set, or the two overlap for an interval.
    """
    body = _function_body(_source(_APP_JS), "startSchedulerPolling")

    stop_index = body.find("stopSchedulerPolling()")
    interval_index = body.find("setInterval(")

    assert stop_index != -1, (
        "startSchedulerPolling() never calls stopSchedulerPolling(). "
        "Each visit to the scheduler view would leave another timer "
        "running, and the requests per tick would grow with every "
        "visit."
    )
    assert interval_index != -1, (
        "startSchedulerPolling() sets no timer at all — the scheduler "
        "view would still be read only once, when it is opened."
    )
    assert stop_index < interval_index, (
        "startSchedulerPolling() sets the new timer before stopping the "
        "old one. The two overlap, so the panel issues two requests per "
        "tick for as long as both are alive."
    )


def test_refresh_redraws_without_reloading_and_reuses_the_endpoints() -> None:
    """The refresh must redraw through the existing loaders, and the
    file must not reload the page.

    ``location.reload()`` is the tempting shortcut: it makes a stale
    page current in one line. It also throws away the operator's
    place — the view they were in, their scroll position, whatever they
    had typed. The panel already has a loader that reads the read
    model and redraws it; the refresh must go through that one.

    The endpoint is checked as well, because a refresh that quietly
    reads a different URL is a refresh that shows the wrong thing:
    ``/scheduler/state`` is the capacity and the queue, and it is the
    read model the whole panel is built on.
    """
    source = _source(_APP_JS)

    body = _function_body(source, "refreshScheduler")
    assert "loadSchedulerState(" in body, (
        "refreshScheduler() does not call loadSchedulerState(). The "
        "capacity and the queue would keep showing the state from the "
        "moment the view was opened."
    )

    state_body = _function_body(source, "loadSchedulerState")
    assert 'api("/scheduler/state")' in state_body, (
        "loadSchedulerState() does not read /scheduler/state through "
        "api(). The refresh would read a different URL than the one "
        "the panel is built on — and a request outside the wrapper "
        "would be answered 403 by the request guard."
    )

    assert "location.reload" not in source, (
        "frontend/app.js contains location.reload. A reload throws away "
        "the operator's place in the page — the view they were in, "
        "their scroll position, whatever they had typed — so the "
        "refresh must redraw the panel instead."
    )


def test_the_deleted_plans_block_is_not_read_again() -> None:
    """The 「可操作的计划」 block is gone; nothing may read it.

    The block was a superset of the two blocks that remain: every plan
    it showed was already named in the queue or in the running list.
    It is gone from the markup, and the loader that filled it is gone
    with it.

    A loader left behind is not harmless.  ``#scheduler-plans-list``
    resolves to nothing, so the panel throws on the next line — and the
    blocks that are still wanted go blank with it.  The refresh is the
    place where this would bite hardest: it runs every five seconds, so
    the panel would go blank and come back every five seconds.
    """
    source = _source(_APP_JS)

    for name in ("loadSchedulerPlans", "renderSchedulerPlans"):
        assert name not in source, (
            f"frontend/app.js still names {name}. The 「可操作的计划」 "
            "block is gone from the markup, so the loader finds nothing "
            "and the panel goes blank around it."
        )

    # 2026-10-31（合入 #71）：终态计划区块回到面板上 —— failed 计划既不在队列里、
    # 也不在「在跑」里，它们的两个按钮（「启动」/「重跑验证」）画在
    # `#scheduler-terminal-list` 里，数据来自 `/plans?include_terminal=true`。
    # **被删掉的是另一个区块**（「可操作的计划」，`#scheduler-plans-list`），它的
    # 两个 loader 仍然不许回来（上面已经断言）。所以这里不再整体禁掉那个 URL，
    # 而是钉住这条用例真正防的事：**读它的人只有一个，且它渲染的目标在 markup 里
    # 真实存在** —— 一个读者找不到自己的目标，才是「面板每五秒空一次」的来源。
    assert "loadSchedulerTerminalPlans" in source, (
        "frontend/app.js has no terminal plans loader: the failed plans' two "
        "buttons (「启动」 / 「重跑验证」) would never be drawn."
    )
    assert source.count("/plans?include_terminal=true") == 1, (
        "frontend/app.js reads /plans?include_terminal=true "
        f"{source.count('/plans?include_terminal=true')} times. The "
        "terminal plans block is the only reader left; a second read is the "
        "「可操作的计划」 block coming back with it."
    )
    assert 'id="scheduler-terminal-list"' in _source(_INDEX_HTML), (
        "the terminal plans loader renders into #scheduler-terminal-list, "
        "which is not in frontend/index.html: the loader would find nothing "
        "and the panel would go blank around it every tick."
    )


def test_view_entry_starts_polling_and_manual_refresh_stays() -> None:
    """Opening the view starts the timer, leaving it stops the timer,
    and the manual refresh button keeps working.

    The timer has to be tied to the view: started when the panel is
    opened, stopped when the panel is left. A timer that is never
    stopped keeps asking the API about a panel nobody is looking at,
    and a timer that is never started leaves the panel stale.

    The manual refresh button is not removed by auto-refresh — it is
    the one that answers immediately. An operator who has just started
    a plan should not have to wait for the next tick.
    """
    source = _source(_APP_JS)

    open_body = _binding_body(source, "#btn-open-scheduler")
    assert "startSchedulerPolling()" in open_body, (
        'the "#btn-open-scheduler" binding does not call '
        "startSchedulerPolling(). Opening the scheduler view would read "
        "the read model once and then never again."
    )

    manual_body = _binding_body(source, "#btn-refresh-scheduler")
    assert "loadSchedulerState(" in manual_body, (
        'the "#btn-refresh-scheduler" binding no longer reads the read '
        "model. The manual refresh is the button that answers "
        "immediately; the timer does not replace it."
    )
    assert "loadSchedulerPlans(" not in manual_body, (
        'the "#btn-refresh-scheduler" binding reads the plans. The '
        "「可操作的计划」 block is gone from the markup, so the read "
        "finds nothing and the panel throws on the next line."
    )

    home_body = _function_body(source, "goHome")
    assert "stopSchedulerPolling()" in home_body, (
        "goHome() does not call stopSchedulerPolling(). Leaving the "
        "scheduler view would leave its timer running, and the panel "
        "would keep being refreshed while the operator is on another "
        "view."
    )

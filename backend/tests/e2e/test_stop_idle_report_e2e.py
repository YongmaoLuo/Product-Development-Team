"""E2E: 页面上的空闲停止不凭空造出一份 stop_report。

背景
----
``POST /api/execution/{plan_id}/stop`` 回答 200。四个键是每个读者都已经
知道的契约：``status`` / ``paused`` / ``paused_at`` /
``interrupted_task_count``。第五个键 ``stop_report`` 回答的是「这一次停止
对进程树做了什么」——它**只在真的停了什么的时候**才有意义。

空闲停止（计划没有在跑）就是「什么都没停」的那一种：没有执行器可杀，
没有子进程可停。这条分支过去会**凭空合成**一份报告出来——形状和真报告
一模一样（执行器 ``already_gone``、没有 pid、没有子进程、没有残留），
于是「没找到可报告的」和「没找到可停的」在读者眼里是同一句话。

任务 1 已经在 API 层把这条契约钉住了
（``backend/tests/integration/api/test_stop_report_only_when_real.py``：
空闲停止的响应体就是那四个键，磁盘上也不会凭空多出一个
``pause_state.json``）。但那份文件是**在进程内**问的 TestClient。

这份测试问的是**页面**那条路
-------------------------
操作员看到的是页面，页面消费的是那个响应体。所以这里用真实的浏览器
（Playwright）驱动真实的服务：

    页面（真实浏览器）→ 点计划卡片 → 进任务视图 → 页面自己的 api() 停止
        → 断言响应体就是那四个键
        → 断言没有 stop_report、没有 phase_transition_error
        → 断言磁盘上没有凭空多出 pause_state.json

空闲的计划在页面上**没有「停止执行」按钮**——那个按钮只在执行跑着的时候
出现。所以停止是在页面上下文里调页面自己的全局 ``api()`` 完成的，走的是
同一个 request guard（``X-PDT-Request: 1``），和按钮走的是同一条路。

被证明的东西
------------
* **四键契约**：响应体的键**恰好**是那四个，多一个都没有。
* **四个值**：``status == "stopped"``、``paused is False``、
  ``paused_at is None``、``interrupted_task_count == 0``。
* **没有报告**：``stop_report`` 不在响应体里。
* **没有半途而废**：``phase_transition_error`` 也不在响应体里——停止确实
  把计划停住了，不是「杀了但没停住」。
* **磁盘上没有凭空多出来的东西**：``pause_state.json`` 不存在。一个没打断
  任何东西的停止，不该给后来的读者留一个会被误认成真打断的记录。
* **截图**：走完这一步的页面留在磁盘上。

隔离
----
每个测试自己起一个后端进程（自己的 state.db、自己的 plans 树），``finally``
里拆干净，所以漏掉一个服务器不会影响下一个测试，断言失败也不会留下一个
监听端口。端口由内核给（``_free_port``），不写死 8000/8001。

浏览器从哪来
------------
浏览器解释器、浏览器环境、起服务器 / 播种计划 / 跑驱动 / 拆干净这六件事，
全部复用 ``test_stop_tree_ui_e2e.py``（按文件路径加载，不改动那个文件）——
它已经把这些跑通过了，这里只需要换一条路径和一套断言。

这个仓库不自带 Playwright 依赖，也不允许测试去装一个——那会把浏览器下载
到开发者的机器上。所以**没有驱动器的机器上这个测试会 skip**：页面上的事实
（截图、页面上下文里的响应体）没有浏览器就看不到，而断言它们等于断言一个
不存在的东西。服务器起不来、断言失败，一律是红。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = [pytest.mark.e2e]


#: The plan this file drives.  The date prefix keeps it out of the
#: archived-plan guard (``classify_plan`` reads a ``YYYYMMDD-*`` dirname as
#: the plan's creation date, and anything on or before the 2026-08-05 cutoff
#: is archived and read-only).
PLAN_ID = "20261028-stop-idle-report-e2e"

#: The four keys the stop response has always carried — and, on an idle
#: stop, the **only** keys it carries.
FOUR_KEYS = {"status", "paused", "paused_at", "interrupted_task_count"}


# ---------------------------------------------------------------------------
# Read-only reuse of the sibling E2E file
# ---------------------------------------------------------------------------


def _load_sibling_helpers():
    """Load ``test_stop_tree_ui_e2e.py`` **without** modifying it.

    Six helpers are reused as they are: ``_boot_server`` (a real
    ``backend.server`` subprocess on its own port), ``_seed_plan`` (the
    plan directory and the state-machine rows), ``_wait_for_plan_listed``
    (the listing cache settles before the page reads it once),
    ``_make_checkout`` (a real git checkout), ``_run_driver`` (the browser
    pass, in its own process) and ``_teardown`` (nothing left behind).
    ``BrowserUnavailable`` and ``_playwright_interpreter`` come along with
    them: they are what turns "this machine cannot drive a page" into a
    skip instead of a red.

    The file is loaded by **path**: a plain cross-module import would
    depend on pytest's rootdir-based ``sys.path`` juggling.
    """
    path = Path(__file__).with_name("test_stop_tree_ui_e2e.py")
    spec = importlib.util.spec_from_file_location(
        "pdt_reused_stop_tree_ui_e2e", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the reused helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SIBLING = _load_sibling_helpers()

_boot_server = _SIBLING._boot_server
_seed_plan = _SIBLING._seed_plan
_wait_for_plan_listed = _SIBLING._wait_for_plan_listed
_make_checkout = _SIBLING._make_checkout
_run_driver = _SIBLING._run_driver
_teardown = _SIBLING._teardown
_playwright_interpreter = _SIBLING._playwright_interpreter
BrowserUnavailable = _SIBLING.BrowserUnavailable


# ---------------------------------------------------------------------------
# The server's start-up bill
# ---------------------------------------------------------------------------


def _install_without_feishu_sdk(work: Path) -> Path:
    """Give the throwaway server an installation **without** the Feishu SDK.

    为什么要有这一步
    ----------------
    服务起来的时候会构造 Feishu notifier，而那个构造器会 ``import
    lark_oapi`` —— 生成出来的 Feishu SDK，**10,700 个模块**，在一台需要
    现编译字节码的机器上是**几十秒**的 import。关键是：import 发生在
    notifier 去问「Feishu 到底配了没有」**之前**，所以一个压根没有
    Feishu 凭据、也不打算推任何卡片的一次性测试服务器，照样要付这笔钱。

    而服务是**按没有这个 SDK 也能起来**设计的：``FeishuClient`` 在
    import 失败时抛 ``FeishuUnavailable``，notifier 就此保持惰性。所以
    这里给测试服务器的就是那种安装：一个 ``lark_oapi`` 不可 import 的
    安装。它是通过 ``PYTHONPATH`` 送进去的——启动助手把 ``PYTHONPATH``
    接在自己的后端目录之后，所以这个影子模块比 ``site-packages`` 里那个
    真的先被找到。

    没有这一步，启动会在 ``FeishuNotifier.start()`` 里花掉的时间超过
    启动助手愿意等的那 60 秒：一个完全健康的服务就变成了一个红的测试。
    """
    shadow = work / "no_feishu_sdk"
    shadow.mkdir(parents=True, exist_ok=True)
    (shadow / "lark_oapi.py").write_text(
        '"""Stand-in for the Feishu SDK: it is not installed here."""\n'
        'raise ImportError("lark-oapi is not installed")\n',
        encoding="utf-8",
    )
    return shadow


def _python_path_without_feishu_sdk(work: Path) -> str:
    """The ``PYTHONPATH`` the test server boots under.

    The shadow comes first (it has to be found before the real package
    in ``site-packages``); whatever the ambient ``PYTHONPATH`` carried is
    kept behind it, because the backend's own directory is prepended to
    this value by the boot helper.
    """
    shadow = _install_without_feishu_sdk(work)
    ambient = os.environ.get("PYTHONPATH", "")
    return f"{shadow}{os.pathsep}{ambient}" if ambient else str(shadow)


# ---------------------------------------------------------------------------
# The browser pass
# ---------------------------------------------------------------------------
#
# The operator's path is the one the page offers: pick the plan, and — for
# a plan that is not running — there is nothing to click, so the stop goes
# through the page's own ``api()``.  The browser runs in its own process
# because the interpreter that runs pytest is not necessarily the one that
# has Playwright installed; the driver script is handed its parameters
# through ``sys.argv`` and reports what it saw as one JSON line on stdout.

_DRIVER = r'''
"""Drive the served page: open an idle plan, stop it, report what came back.

Run as::

    python drive_stop_idle_report.py <url> <plan_id> <checkout> <work> <shots>

Every step is a step on the page, so the frontend's own handlers run.  The
driver reports what the page saw; the assertions live in the test that runs
it.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot launch
    anything.  The binaries that *are* installed are found here instead --
    nothing is downloaded, and nothing is hard-coded: the cache lives under
    the home the run was launched with.
    """
    home = Path.home()
    roots = [
        home / "Library" / "Caches" / "ms-playwright",
        home / ".cache" / "ms-playwright",
    ]
    patterns = (
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-linux*/chrome",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern)):
                if candidate.exists():
                    return str(candidate)
    return None


url, plan_id, checkout, work_dir, shots_dir = sys.argv[1:6]

work = Path(work_dir)
shots = Path(shots_dir)
shots.mkdir(parents=True, exist_ok=True)

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

report = {"page_errors": [], "console_errors": [], "dialogs": []}

with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    def on_page_error(exc):
        report["page_errors"].append(str(exc))

    def on_console(message):
        if message.type == "error":
            report["console_errors"].append(message.text)

    def on_dialog(dialog):
        report["dialogs"].append(dialog.message)
        dialog.dismiss()

    page.on("pageerror", on_page_error)
    page.on("console", on_console)
    page.on("dialog", on_dialog)

    card = '.plan-card[data-plan="%s"]' % plan_id

    # --- 1: the plan list ---------------------------------------------
    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)
    page.wait_for_selector(card, timeout=30000)
    report["plan_card_visible"] = page.is_visible(card)

    # --- 2: into the task view ----------------------------------------
    page.click(card)
    page.wait_for_selector("#tasks-view:not(.hidden)", timeout=30000)
    report["tasks_view_visible"] = page.is_visible("#tasks-view")

    # An idle plan is not running, so the page offers no 停止执行 button:
    # there is nothing for the operator to click.  That is exactly why the
    # stop below goes through the page's own ``api()``.
    report["stop_button_visible"] = page.is_visible("#btn-stop-execution")

    # --- 3: stop it, from the page's own context -----------------------
    # The page's global ``api()`` — the same function the 停止执行 button
    # calls, carrying the same request guard header.  The response body it
    # returns IS what the page would have consumed.
    body = page.evaluate(
        "async () => await api('/execution/%s/stop', {method:'POST'})" % plan_id
    )
    report["stop_body"] = body

    page.screenshot(path=str(shots / "1-idle-stop.png"), full_page=True)

    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_idle_stop_from_the_page_omits_the_report(tmp_path, monkeypatch):
    """The operator stops an idle plan from the page; no report appears.

    The chain is real from end to end: a real ``backend.server``
    subprocess, the real page, a real browser.  The plan is idle — it was
    never started — so the stop has nothing to stop.

    What is asserted:

    * the operator got to the plan (the card is on the page, clicking it
      opens the task view);
    * the page offers no 停止执行 button for an idle plan, which is why
      the stop goes through the page's own ``api()``;
    * the response body carries **exactly** the four keys every reader
      already knows — the report is not one of them;
    * the four values say what an idle stop means: stopped, not paused,
      no pause point, nothing interrupted;
    * there is no ``phase_transition_error``: the stop held;
    * no ``pause_state.json`` appeared on disk — a stop that interrupted
      nothing leaves no resume point behind for a later reader to
      mistake for a real interruption;
    * the screenshot of the walk is on disk and is not empty.
    """
    work = tmp_path
    # No fake ``claude`` here: an idle plan never dispatches anything.
    fake_bin = work / "fake_bin"
    fake_bin.mkdir(parents=True, exist_ok=True)

    # The server boots without the Feishu SDK, so its start-up does not
    # pay for importing it (see ``_install_without_feishu_sdk``).
    monkeypatch.setenv("PYTHONPATH", _python_path_without_feishu_sdk(work))

    base_url, proc = _boot_server(work, fake_bin)
    try:
        _seed_plan(work, PLAN_ID)
        _wait_for_plan_listed(base_url, PLAN_ID)
        checkout = _make_checkout(work)

        interpreter = _playwright_interpreter()
        if interpreter is None:
            pytest.skip(
                "no Playwright driver on this machine; the page's response "
                "body and the screenshot are facts only a browser can show, "
                "and this repository does not install a browser driver for "
                "its tests"
            )

        script = work / "drive_stop_idle_report.py"
        script.write_text(_DRIVER, encoding="utf-8")
        shots = work / "shots"
        try:
            report = _run_driver(
                interpreter, script, base_url, PLAN_ID, checkout, work, shots,
            )
        except BrowserUnavailable as exc:
            pytest.skip(
                f"the browser pass is unavailable on this machine ({exc})"
            )

        # --- the operator got to the plan ------------------------------
        assert report.get("plan_card_visible") is True, (
            "the plan card never appeared on the page; the operator could "
            f"not even pick the plan. report={report!r}"
        )
        assert report.get("tasks_view_visible") is True, (
            "clicking the plan card did not open the task view. "
            f"report={report!r}"
        )
        assert report.get("stop_button_visible") is False, (
            "the page offered a 停止执行 button for an idle plan; the stop "
            "in this test is supposed to be the one an idle plan offers "
            f"none of. report={report!r}"
        )

        # --- the answer is the four keys, and only the four keys -------
        body = report.get("stop_body")
        assert isinstance(body, dict), (
            f"the stop did not answer with an object: {body!r} "
            f"(report={report!r})"
        )
        assert set(body) == FOUR_KEYS, (
            "the stop answer does not carry exactly the four keys every "
            f"reader knows: {body!r}"
        )

        # --- and the four keys say what an idle stop means --------------
        assert body["status"] == "stopped", body
        assert body["paused"] is False, (
            f"an idle stop paused the plan: {body!r}"
        )
        assert body["paused_at"] is None, (
            f"an idle stop reported a pause point: {body!r}"
        )
        assert body["interrupted_task_count"] == 0, (
            f"an idle stop reported interrupted tasks: {body!r}"
        )

        # --- no report, and no half-held stop --------------------------
        assert "stop_report" not in body, (
            "the stop invented a report about a plan that was never "
            f"running: {body!r}"
        )
        assert "phase_transition_error" not in body, (
            "the stop did not hold — the plan could not be parked in the "
            f"stopped phase: {body!r}"
        )

        # --- and nothing appeared on disk ------------------------------
        plan_dir = work / "plans" / PLAN_ID
        checkpoint = plan_dir / "pause_state.json"
        assert not checkpoint.exists(), (
            "a stop that interrupted nothing left a resume point behind: "
            f"{sorted(p.name for p in plan_dir.iterdir())}"
        )

        # --- the page did not blow up on the way ------------------------
        assert report.get("dialogs") == [], (
            f"the page showed dialogs during the walk: {report['dialogs']!r}"
        )

        # --- the screenshot of the walk is on disk ----------------------
        shot = shots / "1-idle-stop.png"
        assert shot.exists() and shot.stat().st_size > 0, (
            f"the screenshot was not written to {shots}"
        )

        # The evidence, in one line: what the page got back from the stop.
        # An end-to-end walk is worth reading in the log.
        print(
            "[stop-idle-report-e2e] stop_body={0!r} page_errors={1!r}".format(
                body, report.get("page_errors"),
            )
        )
    finally:
        _teardown(base_url, proc, PLAN_ID, work)

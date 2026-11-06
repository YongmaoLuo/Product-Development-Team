"""端到端验收：轮次作用域与根因链预检在真浏览器里看得见，且不推进轮次。

背景
----
前 11 个任务改的都是**后端**：账本带 ``reused_from_round``（任务 6）、
根因链的预检（任务 10）、预检接线到自动循环与手动 start（任务 11）。
没有一条断言证明这些改动在**页面**上是成立的 —— 「第二轮没有重跑、
复用看得出来、链预检没有把轮次推上去」这三件事，操作员只能在真浏览器里
看到。

这一份补上那一段：真后端（``backend.server`` 子进程）、真 Chromium
（Playwright）、真点击（``.sidebar-plan-item[data-plan=...]`` 与
``#btn-start-verification``）。断言的是操作员看到的东西：验证面板里
**三个**验证点、轮次写着「2」、点完「启动验证」两秒之后轮次**还是** 2、
库里 ``plan_verification.round`` 还是 2、状态接口答 200 且带着验证点。

种子
----
计划 ``e2e-round-scope`` 被种成**一个被中断掉的第 2 轮**的样子：

* ``plan_routing`` 停在 ``verification_running``、``plan_verification`` 的
  状态也还是 ``running``、轮次是 2 —— 上一轮跑到一半，进程没了。
  ``runtime_state`` 非空，所以后端启动时把它**恢复**成一条「被恢复的」
  记录（``verification_loop._recover_verification_states``）：这一轮可以
  被重新进入（``routes.verification`` 的 CAS 只有在恢复态下才接受
  ``verification_running`` 作为来源阶段），而且进入的是**第 2 轮**，
  不是第 3 轮（``_resolve_start_round``：行还是 running 且轮次大于 0 ⇒
  中断过 ⇒ 续跑同一轮）。
* 账本 ``execution_results`` 里**恰有一项**带 ``reused_from_round=1`` ——
  第 1 轮的结论被复用，不是本轮真跑（任务 6 的标记）。
* ``verification_round_scope.json``：第 2 轮的 HEAD 与变更集
  （``verification_round_scope.record_round_scope`` 写的那个文件）。
* 根因链 ``verification_root_cause_chain.json``：顶层 ``stage`` 是
  ``implementation``、``head_sha`` 是**当前 HEAD**（链不陈旧），两个验证点
  各站在链的一个阶段上 —— 一个已经做到实现（机器的活做完了，它不再欠
  机器的活），一个的根因报告**已经交付**（它欠一次实现）。

链预检读到的正是这个形状：链**欠活** → 预检把活派给执行器，**这一轮
不跑**。轮次因此停在 2 —— 这就是「链预检不推进轮次」在页面上的样子。

（为什么链不能只有一个站在 ``implementation`` 上的点：那样的点会被**降级**
成「交给人」，链于是**不欠活**，预检把这一轮**还给**轮次自己跑 —— 那条
路不是本文件要钉的。链欠活的那一头必须有一个站在 ``root_cause``、而且
根因报告已经交付的点。）

边界条件
--------
* 端口占用 → ``_free_port()`` 取空闲端口，经 ``PDT_PORT`` 传进去；
* server 60s 没就绪 → 失败，并把服务端自己的输出打出来；
* 浏览器缺失 → **失败**，不跳过：这一份验收没有浏览器就没有验收；
* 读库用只读连接 ``file:<db>?mode=ro``，不与服务进程的写连接争锁；
* 清理：teardown 杀**进程组**（服务端派出去的执行器替身也在里面）；
* 截图落在 ``backend/tests/e2e/artifacts/e2e/round_scope_verification.png``
  —— 相对这份文件是 ``artifacts/e2e/round_scope_verification.png``。截图是
  **运行期产物**：仓库的「截图不入库」约定把它钉在
  ``backend/tests/e2e/artifacts/`` 底下（``.gitignore`` 一条规则 + 一个门禁
  守着）。放在仓库根的 ``artifacts/`` 两头落空 —— 没被 ignore（``git add -A``
  会收进索引），而一旦被提交，``artifacts`` 就成了一个不在 ``SCAN_ROOTS``
  里的「被跟踪顶层目录」，静态门禁必红；
* 「启动验证」是**真点击**：浏览器把鼠标移过去按下去，页面自己的捕获监听器
  记下 ``isTrusted``（只有浏览器自己产生的点击才是 true）。轮次在跑的时候
  前端把这个按钮藏起来（停止按钮顶替它），而验证点又只在轮次跑的时候渲染
  —— 两者在页面上从不同时出现。验收要验的是后端拿到启动请求之后做什么，
  所以测试先把按钮变成可点的，再真点它；
* **浅克隆**（``actions/checkout`` 默认 depth=1，PR 门禁的 e2e lane 就是
  这么跑的）没有 ``HEAD~1`` → 上一轮的 commit 记成「不知道」，变更集也就是
  「不知道」—— 与产品自己的口径一致（``verification_round_scope``：两个 sha
  有一个读不出来时，变更集未知，一律全量重跑）。链的 ``head_sha`` 仍然取
  ``HEAD``：那个读不出来，链就陈旧了，预检不接手，这一轮会真跑 —— 那是
  应当失败的情形，不是应当绕过去的情形。

跑法::

    cd backend && python3 -m pytest \\
        tests/e2e/test_round_scope_verification_ui_e2e.py -v
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

#: 这份文件是端到端验收：它起服务、开浏览器、走完用户的那条路。
#: **不带** ``time_sensitive`` —— 那条 marker 会被 addopts 的
#: ``-m "not time_sensitive"`` 取消选择，测试会被静默地不收集。
pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent
#: 仓库根 —— 截图归档的地方（``artifacts/e2e/…``）。
REPO_ROOT = PROJECT_ROOT

# ``state_machine`` / ``config_paths`` 这些包住在 ``backend/`` 底下，两份
# pytest 配置都把它放进了 ``sys.path``；这里再补一刀，让这份文件在两种
# 配置下都能 import 到它。
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: 这个 plan 的 id。计划目录、``plan_routing`` 行、``plan_verification``
#: 行、``plan_execution`` 行都以它为键。
PLAN_ID = "e2e-round-scope"

#: 种子里写进 ``plan_verification`` 的那一轮 —— 这一次 start 续跑的就是它。
SEEDED_ROUND = 2
SEEDED_MAX_ROUNDS = 4

#: 页面上必须出现的验证点：**三个**，不多不少。
EXPECTED_POINT_COUNT = 3
EXPECTED_POINT_IDS = ("VP-001", "VP-002", "VP-003")

#: 账本里那**一项**复用：第 1 轮的结论，第 2 轮拿来用。
REUSED_VP_ID = "VP-001"
REUSED_FROM_ROUND = 1

#: 链上还欠一次实现的那个验证点 —— 预检派出去的就是它的活。
CHAIN_WORK_VP_ID = "VP-003"

#: 轮次标签（``Round 2/4``）里的那个数字。
EXPECTED_ROUND = SEEDED_ROUND

#: 浏览器在等各个页面元素时的预算（毫秒）。没有固定 sleep：一个等不到的
#: 等待就是一次带原因的失败。
BROWSER_TIMEOUT_MS = 30000

#: 整段浏览器过程的预算（秒）。
DRIVER_TIMEOUT_SECONDS = 180

#: 后端起来的预算（秒）。起不来就失败，并把服务端自己的输出打出来。
READY_TIMEOUT_SECONDS = 60.0

#: 点完「启动验证」之后给轮次的时间（秒）—— 轮次**不该**在这段时间里
#: 往前走，所以这段时间必须短到能证伪它，又长到能证伪一个偷偷开第 3 轮
#: 的实现。
SETTLE_SECONDS = 2.0

#: 预检的脚印（它写下的那条修复任务行）最多等多久（秒）。预检不发任何
#: 请求，它是本地的一笔数据库写入，所以这个等待几乎总是立刻就够。
PREFLIGHT_FOOTPRINT_TIMEOUT_SECONDS = 30.0

#: 截图的归档位置 —— **相对这份测试文件所在的目录**：
#: ``artifacts/e2e/round_scope_verification.png``。
#:
#: 为什么落在 ``backend/tests/e2e/`` 底下而不是仓库根：截图是**运行期产物**
#: —— 每次运行都不一样，仓库的约定是「截图不入库」。那个约定在仓库里是
#: **有形状的**：``.gitignore`` 有一条 ``backend/tests/e2e/artifacts/``，
#: 还有一个门禁（``static_gates/test_e2e_artifacts_are_not_tracked.py``）
#: 断言那个目录下没有一个文件被 git 跟踪；同一个目录也正是仓库里另一个
#: 真浏览器 e2e 测试（``test_execution_status_logs_ui_e2e.py``）放截图的地方。
#:
#: 落在仓库根的 ``artifacts/`` 则两头落空：它没有被 ignore（``git add -A``
#: 会把它收进索引），而一旦被提交，``artifacts`` 就成了一个「被跟踪的顶层
#: 目录」—— ``SCAN_ROOTS`` 要求每个被跟踪的顶层目录都被门禁扫到
#: （``static_gates/test_every_tracked_directory_is_scanned.py``），于是门禁
#: 必红。截图是证据，不是源码：它不进版本库。
SCREENSHOT_RELPATH = Path("artifacts") / "e2e" / "round_scope_verification.png"
SCREENSHOT_PATH = Path(__file__).resolve().parent / SCREENSHOT_RELPATH

#: ``install_hermetic_env()`` 把 ``$HOME`` 指向了一个临时目录；Playwright
#: 的浏览器二进制装在**跑起来时那个 home** 底下。这个变量记着那个 home。
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"


# ---------------------------------------------------------------------------
# Read-only reuse of the sibling E2E files
# ---------------------------------------------------------------------------


def _load_sibling(module_name: str, filename: str):
    """Load a sibling E2E module **by file path**, without modifying it.

    ``backend/tests/e2e/`` 的包结构会变，跨模块 import 依赖 pytest 的
    rootdir gymnastics；按文件路径加载避开那一份脆弱性，也不复制一行被
    借走的代码。
    """
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the borrowed helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    # The module is registered **before** it is executed: ``dataclasses``
    # looks the defining module up in ``sys.modules`` by name, and a module
    # that was never registered makes every ``@dataclass`` in it raise.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


#: 任务 4 的助手：真后端进程、种子、空闲端口、请求。
_RESTART = _load_sibling(
    "pdt_execution_status_restart_helpers",
    "test_execution_status_restart_e2e.py",
)
#: 浏览器助手：能 import playwright 的解释器，以及 Chromium 的发现逻辑。
_TOKEN = _load_sibling(
    "pdt_token_convergence_helpers",
    "test_token_convergence_ui_acceptance.py",
)

_free_port = _RESTART._free_port
_boot = _RESTART._boot
_http_json = _RESTART._http_json
_server_output = _RESTART._server_output
_playwright_interpreter = _TOKEN._playwright_interpreter


# ---------------------------------------------------------------------------
# The Chromium discovery (borrowed from the upstream acceptance file)
# ---------------------------------------------------------------------------


def _upstream_browser_executable() -> Optional[str]:
    """``browser_executable()`` from the upstream acceptance file.

    它住在 ``test_token_convergence_ui_acceptance.py`` 的**驱动源码字符串**
    里面（那里它不是模块级的名字），所以这里用 ``ast`` 把它从那段源码里
    取出来执行 —— 借的是上游那一份实现，本文件不另写一份。

    ``$HOME`` 在 pytest 树里被重定向到了临时目录，而 Chromium 装在**跑起来
    那个 home** 底下；所以调用期间把 ``$HOME`` 临时换回去。
    """
    tree = ast.parse(_TOKEN._DRIVER)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "browser_executable":
            namespace: Dict[str, Any] = {"Path": Path}
            exec(  # noqa: S102  # nosec B102 - the upstream helper, executed verbatim
                compile(
                    ast.Module(body=[node], type_ignores=[]),
                    "<upstream-browser-driver>",
                    "exec",
                ),
                namespace,
            )
            return _with_real_home(namespace["browser_executable"])
    raise AssertionError(
        "the upstream acceptance file no longer carries a "
        "browser_executable() helper -- the browser discovery has to be "
        "re-pointed at whoever owns it now"
    )


def _with_real_home(func):
    """Call ``func()`` with ``$HOME`` pointing at the launching home."""
    real_home = os.environ.get(REAL_HOME_ENV)
    if not real_home:
        return func()
    previous = os.environ.get("HOME")
    os.environ["HOME"] = real_home
    try:
        return func()
    finally:
        if previous is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

_BROWSER_DRIVER = r'''
"""Drive the served page the way an operator does, and report what it shows.

argv: <base_url> <plan_id> <screenshot_path> <chromium_executable> <timeout_ms> <settle_seconds>

Every step is a click on the page, so the frontend's own handlers run:
``resumePlan`` -> ``showTasksView`` -> ``resumeVerificationPanel``.  Nothing
here reaches into the application's internals to make a panel show up.

The start button is **clicked for real**: the browser moves the mouse and
hits the button, so the page's own handler runs off a trusted event.  While
a round is running the frontend hides that button (the stop button takes
its place), and the verification points are rendered only while it is
running -- so on this page the two never coexist.  The test makes the
button clickable and then clicks it; the page's own capturing listener
records ``isTrusted``, which is ``true`` only for a click the browser
itself generated.

The two budgets come from the caller — the module's own constants are the
single source of truth for them, so a value cannot drift between the
constant and the place it is used.

The report is one JSON object on stdout.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


(
    base_url,
    plan_id,
    screenshot_path,
    chromium,
    timeout_ms,
    settle_seconds,
) = sys.argv[1:7]

#: How long the browser waits for each of the things it needs.
timeout_ms = int(timeout_ms)

#: How long the page is given to advance the round after the start click.
settle_seconds = float(settle_seconds)

report = {"page_errors": [], "console_errors": [], "dialogs": []}

with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path=chromium)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))

    def _on_console(message):
        if message.type != "error":
            return
        text = message.text or ""
        # A resource that 404s is reported by the browser as a console
        # error; that is not a page error.
        if "Failed to load resource" in text:
            return
        report["console_errors"].append(text)

    page.on("console", _on_console)

    # A dialog (the frontend alerts when a start fails) must not block the
    # driver: it is recorded and dismissed.
    def _on_dialog(dialog):
        report["dialogs"].append(dialog.message or "")
        try:
            dialog.dismiss()
        except Exception:
            pass

    page.on("dialog", _on_dialog)

    page.goto(base_url, wait_until="load")
    page.wait_for_selector("#app", timeout=timeout_ms)

    # --- 1: the user clicks the plan in the sidebar --------------------
    item = '.sidebar-plan-item[data-plan="%s"]' % plan_id
    page.wait_for_selector(item, timeout=timeout_ms)
    report["sidebar_item_visible"] = page.is_visible(item)
    page.click(item)

    # --- 2: the verification panel opens ------------------------------
    page.wait_for_selector(
        "#verification-panel", state="visible", timeout=timeout_ms
    )
    report["verification_panel_visible"] = page.is_visible("#verification-panel")

    # The points are rendered only while the round is running, so waiting
    # for one of them is waiting for the panel to have painted the round.
    page.wait_for_selector(".verification-point", timeout=timeout_ms)
    report["point_count_before"] = page.locator(".verification-point").count()
    report["point_ids"] = [
        (
            page.locator(".verification-point")
            .nth(index)
            .get_attribute("data-vp-id")
            or ""
        )
        for index in range(report["point_count_before"])
    ]
    report["round_before"] = (page.inner_text("#verification-round") or "").strip()

    # --- 3: the user clicks "start verification" ----------------------
    #
    # A **real** click, not a synthetic DOM event: the browser moves the
    # mouse onto the button and presses, so the page's own handler runs
    # off a trusted event.
    #
    # The product hides the start button while a round is running
    # (``updateVerificationActions``: "running" puts the *stop* button in
    # its place), and the verification points are rendered only while it
    # is running -- so on this page the two never coexist.  What is under
    # test is what the backend does with the start request, so the test
    # makes the button clickable and clicks it.
    #
    # The proof that the click was real is recorded by the page itself: a
    # capturing listener sees ``isTrusted``, which is ``true`` only for a
    # click the browser generated.  A synthetic ``dispatchEvent`` would
    # report ``false``.
    page.evaluate(
        """() => {
            window.__roundScopeClicks = [];
            document.addEventListener("click", (event) => {
                const target = event.target;
                if (target && target.id === "btn-start-verification") {
                    window.__roundScopeClicks.push({
                        is_trusted: event.isTrusted,
                        detail: event.detail,
                    });
                }
            }, true);
        }"""
    )
    report["start_button_was_hidden"] = not page.is_visible(
        "#btn-start-verification"
    )
    page.eval_on_selector(
        "#btn-start-verification", "el => el.classList.remove('hidden')"
    )
    report["start_button_is_visible_now"] = page.is_visible(
        "#btn-start-verification"
    )
    page.click("#btn-start-verification")
    report["start_clicks"] = page.evaluate(
        "() => window.__roundScopeClicks || []"
    )

    # --- 4: give the round its chance to advance ----------------------
    time.sleep(settle_seconds)
    report["round_after"] = (page.inner_text("#verification-round") or "").strip()
    report["point_count_after"] = page.locator(".verification-point").count()

    # --- 5: the screenshot ---------------------------------------------
    shot = Path(screenshot_path)
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot), full_page=True)
    report["screenshot"] = str(shot)
    report["screenshot_size"] = shot.stat().st_size if shot.exists() else 0

    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


# ---------------------------------------------------------------------------
# Running the driver
# ---------------------------------------------------------------------------


def _current_interpreter_has_playwright() -> bool:
    """Can the interpreter running pytest import ``playwright.sync_api``?"""
    probe = subprocess.run(
        [sys.executable, "-c", "import playwright.sync_api"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return probe.returncode == 0


def _driver_env() -> Dict[str, str]:
    """The environment the browser driver runs under.

    ``$HOME`` points back at the home the process was launched with (see
    :data:`REAL_HOME_ENV`), so Playwright finds the browser binary it
    installed there.
    """
    env = dict(os.environ)
    real_home = os.environ.get(REAL_HOME_ENV)
    if real_home:
        env["HOME"] = real_home
    return env


def _parse_report(stdout: str) -> Dict[str, Any]:
    """The report the driver printed — the last non-empty line, as JSON."""
    lines = [line for line in (stdout or "").strip().splitlines() if line.strip()]
    if not lines:
        raise AssertionError("the browser driver printed nothing")
    return json.loads(lines[-1])


def _drive_in_process(argv: List[str]) -> Dict[str, Any]:
    """Run the driver inside this interpreter (it has playwright)."""
    buffer = io.StringIO()
    previous_argv = list(sys.argv)
    # The driver reads its arguments the way a script does -- ``argv[1:]``
    # -- so the program name has to be there too.
    sys.argv = ["<round-scope-browser-driver>", *argv]
    try:
        with redirect_stdout(buffer):
            exec(  # noqa: S102  # nosec B102 - the driver, run verbatim
                compile(_BROWSER_DRIVER, "<round-scope-browser-driver>", "exec"),
                {"__name__": "__main__"},
            )
    finally:
        sys.argv = previous_argv
    return _parse_report(buffer.getvalue())


def _drive_in_subprocess(
    interpreter: str, argv: List[str], work: Path
) -> Dict[str, Any]:
    """Run the driver as its own process, with an interpreter that has playwright."""
    script = work / "drive_round_scope.py"
    script.write_text(_BROWSER_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [interpreter, str(script), *argv],
        capture_output=True,
        text=True,
        timeout=DRIVER_TIMEOUT_SECONDS,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return _parse_report(result.stdout)


def _drive(base_url: str, screenshot_path: Path, work: Path) -> Dict[str, Any]:
    """Drive the page and return what it showed.

    **没有浏览器就是失败，不是跳过。** 这一份验收的全部内容就是「操作员
    在真浏览器里看到什么」；一台没有 Chromium 的机器上它跳过，等于这份
    验收从来没有发生过 —— 而它恰恰是最该被看见的那一份。
    """
    chromium = _upstream_browser_executable()
    if not chromium:
        raise AssertionError(
            "no Chromium is installed for Playwright: this end-to-end "
            "acceptance cannot be verified without a real browser"
        )

    argv = [
        base_url,
        PLAN_ID,
        str(screenshot_path),
        chromium,
        str(BROWSER_TIMEOUT_MS),
        str(SETTLE_SECONDS),
    ]

    if _current_interpreter_has_playwright():
        return _drive_in_process(argv)

    interpreter = _playwright_interpreter()
    if not interpreter:
        raise AssertionError(
            "no interpreter with playwright is installed in this environment: "
            "this end-to-end acceptance cannot be verified without a real "
            "browser"
        )
    return _drive_in_subprocess(interpreter, argv, work)


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------

#: 计划里的三个任务 —— 验证点挂在它们身上（``plan_item_id``）。
SEEDED_TASKS = (
    {
        "id": "T1",
        "title": "记下轮次作用域",
        "description": "d",
        "files_to_modify": ["backend/verification_round_scope.py"],
        "depends_on": [],
        "test_command": "true",
    },
    {
        "id": "T2",
        "title": "根因链走到实现阶段",
        "description": "d",
        "files_to_modify": ["backend/verification/orchestrator.py"],
        "depends_on": [],
        "test_command": "true",
    },
    {
        "id": "T3",
        "title": "根因链欠一次实现",
        "description": "d",
        "files_to_modify": ["backend/verification/orchestrator.py"],
        "depends_on": [],
        "test_command": "true",
    },
)

#: 验证计划里的三个验证点。
SEEDED_POINTS = (
    {
        "id": "VP-001",
        "plan_item_id": "T1",
        "title": "第 2 轮复用的结论",
        "verification_method": "unit",
        "status": "passed",
        "progress": 100,
        "evidence_command": "true",
    },
    {
        "id": "VP-002",
        "plan_item_id": "T2",
        "title": "链上已经做到实现的验证点",
        "verification_method": "unit",
        "status": "failed",
        "progress": 40,
        "evidence_command": "true",
    },
    {
        "id": "VP-003",
        "plan_item_id": "T3",
        "title": "根因报告已交付的验证点",
        "verification_method": "review",
        "status": "failed",
        "progress": 60,
        "evidence_command": "true",
    },
)

#: 复用的那一项声明的范围。复用判据（``may_reuse``）要看的四样东西里，
#: 这一项是其中一样：范围非空、且第 2 轮的变更集碰不到它。
REUSED_COVERS_PATHS = ("backend/verification_agent.py",)


def _git(repo_root: Path, *args: str) -> str:
    """One git answer, or a failure that says which question failed."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr!r}"
        )
    return result.stdout.strip()


def _git_optional(repo_root: Path, *args: str) -> Optional[str]:
    """One git answer, or ``None`` when git cannot answer.

    Some questions have **no answer** in some trees.  ``HEAD~1`` does not
    exist in a **shallow** checkout — and ``actions/checkout`` fetches
    depth 1 by default, so the PR gate's e2e lane runs in exactly such a
    tree (``git rev-parse HEAD~1`` exits 128 there).  A repository with a
    single commit has no parent either.

    A question without an answer must not take the whole acceptance down:
    the production helper already says what an unknown sha means — the
    change set is **unknown**, not empty (``verification_round_scope``'s
    "变更集的两种空") — so the seed records the unknown and lets the
    product read it as the unknown it is.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _is_gitignored(path: Path) -> bool:
    """Is ``path`` kept out of git's index by a rule?

    ``git check-ignore`` answers exactly that question: exit 0 means a rule
    matches the path, so ``git add -A`` will not stage it.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.returncode == 0


def _write_json(path: Path, payload: Any) -> None:
    """Write one JSON document, creating the parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _seed_plan(work: Path) -> None:
    """Write the plan the operator is going to open.

    Four surfaces, each on the one the server reads it from:

    * the plan directory — ``tasks.json``（任务）、``verification_plan.json``
      （三个验证点）、``reports/root-cause-VP-003.md``（**已交付**的根因
      报告）、``verification_root_cause_chain.json``（链）、
      ``verification_round_scope.json``（第 2 轮的轮次作用域）；
    * ``plan_routing`` 行 —— 计划停在哪一步（``verification_running``）；
    * ``plan_execution`` 行 —— 计划的**工作区**。链的 ``head_sha`` 要和
      工作区的 HEAD 对得上（链不陈旧），所以这一行指向仓库本身；
    * ``plan_verification`` 行 —— 第 2 轮、四个点的预算、状态 ``running``，
      非空的 ``runtime_state``（这一行让后端把它**恢复**成一条被恢复的
      记录，于是 ``verification_running`` 是一个可以被重新进入的来源阶段），
      以及账本里那**一项** ``reused_from_round=1``。

    ``plan_execution`` 这一行**不写** ``exec_status``：它要是终局
    （``completed`` / ``failed`` / ``stopped``），列表会把这个计划从侧边栏
    里滤掉（``routes.plans._plan_is_terminal``），页面上就没有那张卡可点。
    """
    plan_dir = work / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    _write_json(plan_dir / "tasks.json", {"tasks": list(SEEDED_TASKS)})
    _write_json(
        plan_dir / "verification_plan.json",
        {"verification_points": [dict(point) for point in SEEDED_POINTS]},
    )

    # The delivered root-cause report of VP-003.  The scope a report
    # declares is what the chain reads back — the report is JSON (or
    # carries a json block) and names the files to change under ``paths``.
    _write_json(
        plan_dir / "reports" / "root-cause-VP-003.md",
        {
            "vp_id": "VP-003",
            "root_cause": "执行器把上一轮的结论当本轮的用了",
            "paths": ["backend/verification/orchestrator.py"],
        },
    )

    head_sha = _git(REPO_ROOT, "rev-parse", "HEAD")
    _write_json(
        plan_dir / "verification_root_cause_chain.json",
        {
            "version": 1,
            # 链在 implementation：它已经派过一次实现。
            "stage": "implementation",
            # 链描述的是**当前**这个 commit 的失败 —— 链因此不陈旧
            # （``orchestrator._chain_is_stale``）。
            "head_sha": head_sha,
            "vps": {
                # 已经做到实现：机器的活做完了，它不再欠机器的活。
                "VP-002": {
                    "stage": "implementation",
                    "round": SEEDED_ROUND - 1,
                    "report_path": f"plans/{PLAN_ID}/reports/root-cause-VP-002.md",
                },
                # 根因报告已经交付：它欠一次实现 —— 链因此**欠活**，
                # 预检接管的正是这一份活。
                "VP-003": {
                    "stage": "root_cause",
                    "round": SEEDED_ROUND,
                    "report_path": f"plans/{PLAN_ID}/reports/root-cause-VP-003.md",
                },
            },
        },
    )

    # The round scope of round 2 — written by the production helper, so the
    # file has the shape the reuse judgement reads.
    from verification_round_scope import changed_files, record_round_scope

    # The previous commit is **not always there**: a shallow checkout
    # (``actions/checkout`` fetches depth 1 by default) has no ``HEAD~1``,
    # and ``git rev-parse HEAD~1`` exits 128 in such a tree.  The
    # production helper says what an unknown sha means — the change set
    # is **unknown**, not empty — so the seed records the unknown instead
    # of failing the whole acceptance over a commit that is not there.
    prev_head = _git_optional(REPO_ROOT, "rev-parse", "HEAD~1")
    record_round_scope(
        plan_dir,
        SEEDED_ROUND,
        head_sha=head_sha,
        prev_head_sha=prev_head,
        changed=changed_files(REPO_ROOT, prev_head, head_sha),
    )

    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = db_open(work / "state.db")
    try:
        migrate(conn)
        # The plan stopped **inside** a verification round: that is what
        # makes the panel render its points, and it is the state the
        # operator is coming back to.
        RoutingRepository(conn).insert(PLAN_ID, "verification_running")
        # The workspace the round runs against — the chain's head_sha has
        # to be this tree's HEAD.
        ExecutionRepository(conn).insert(PLAN_ID, project_dir=str(REPO_ROOT))
        verification = VerificationRepository(conn)
        # Round 2, still running, with the runtime state of an interrupted
        # round: the backend recovers it at startup, so this start
        # **resumes** it instead of opening round 3.
        verification.insert(
            PLAN_ID,
            "running",
            round=SEEDED_ROUND,
            max_rounds=SEEDED_MAX_ROUNDS,
            runtime_state={"pending_vps": ["VP-002", "VP-003"]},
        )
        verification.init_round(PLAN_ID, SEEDED_ROUND, SEEDED_MAX_ROUNDS)
        verification.save_execution_results(PLAN_ID, _seed_ledger())
    finally:
        conn.close()


def _seed_ledger() -> Dict[str, Any]:
    """The round-2 ledger: one reused conclusion, two verdicts of this round.

    Exactly **one** entry carries ``reused_from_round`` — the marker task 6
    put there.  A missing marker means "this round ran it"; that
    distinction is the whole point of the ledger, so the seed has to make
    it: one reused, two fresh.
    """
    results = []
    for point in SEEDED_POINTS:
        reused = REUSED_FROM_ROUND if point["id"] == REUSED_VP_ID else None
        entry: Dict[str, Any] = {
            "id": point["id"],
            "status": "PASSED" if point["status"] == "passed" else "FAILED",
            "round": SEEDED_ROUND,
            "reasons": ["第 1 轮的结论，本轮复用" if reused else "本轮真跑"],
            "evidence": {},
            "reused_from_round": reused,
        }
        if reused is not None:
            entry["covers_paths"] = list(REUSED_COVERS_PATHS)
        results.append(entry)
    return {
        "executed_at": "2026-11-05T00:00:00",
        "verification_points": [dict(point) for point in SEEDED_POINTS],
        "execution_results": results,
    }


# ---------------------------------------------------------------------------
# The backend process
# ---------------------------------------------------------------------------


def _script_dir(work: Path) -> Path:
    """A script directory for the **scripted** LLM double.

    The double answers every query from a fixture file.  This directory
    holds an empty registry, so a query finds no answer and fails fast —
    which is the point: the round under test must never reach a provider.
    """
    script_dir = work / "llm-scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    (script_dir / "registry.json").write_text(
        json.dumps({"prompts": {}}, ensure_ascii=False), encoding="utf-8"
    )
    return script_dir


def _boot_server(work: Path, port: int, script_dir: Path) -> subprocess.Popen:
    """Start the real backend, with the LLM double switched on.

    The sibling E2E file owns the boot contract (plans tree, state
    database, port, provider mode, orphan reaper, executor stand-in).  The
    one thing this file adds is the **scripted LLM double**: the round
    under test must not be able to reach a provider, and the double is
    switched on through the environment, so it is set around the borrowed
    boot rather than duplicated.
    """
    previous = {
        key: os.environ.get(key)
        for key in ("PDT_LLM_MODE", "PDT_LLM_SCRIPT_DIR")
    }
    os.environ["PDT_LLM_MODE"] = "scripted"
    os.environ["PDT_LLM_SCRIPT_DIR"] = str(script_dir)
    try:
        return _boot(work, port)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _wait_ready(base_url: str, proc: subprocess.Popen) -> None:
    """Wait until the backend answers its readiness probe.

    The backend is up when the API answers, not when the process exists:
    the process exists for a moment before uvicorn is listening.  A
    backend that died on the way up says so in its own output, so that
    output is attached to the failure.
    """
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"the backend exited with {proc.returncode} before it was "
                f"ready:\n{_server_output(proc)}"
            )
        try:
            status, body = _http_json("GET", f"{base_url}/api/health")
            if status == 200:
                return
            last = (status, body)
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last = exc
        time.sleep(0.2)
    raise AssertionError(
        f"{base_url}/api/health was not ready within {READY_TIMEOUT_SECONDS}s; "
        f"last={last!r}\n{_server_output(proc)}"
    )


def _teardown(proc: Optional[subprocess.Popen]) -> None:
    """Stop the backend **and everything it started**.

    The server runs the executor in its own process group (the boot uses
    ``start_new_session=True``), so the group is killed as a whole: the
    backend, the executor stand-in the preflight dispatched, and anything
    else the server spawned.  Killing only the server would leave the
    executor behind, holding the plan's project directory open.
    """
    if proc is None:
        return
    handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(proc.pid), sig)
                except (ProcessLookupError, PermissionError):
                    break
                try:
                    proc.wait(timeout=20)
                    break
                except subprocess.TimeoutExpired:
                    continue
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover - the handle is ours
                pass


# ---------------------------------------------------------------------------
# Reading the state back
# ---------------------------------------------------------------------------


def _decode_json(raw: Any) -> Any:
    """A JSON column, decoded. A NULL column is ``None``."""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _read_state_readonly(db_path: Path) -> Dict[str, Any]:
    """Read the plan's state over a **read-only** connection.

    The server holds a write connection to the same database; a read-only
    connection (``file:<db>?mode=ro``) does not take a write lock, so the
    two never fight over it.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT round, execution_results, verdicts "
            "FROM plan_verification WHERE plan_id = ?",
            (PLAN_ID,),
        ).fetchone()
        phase_row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (PLAN_ID,),
        ).fetchone()
        task_rows = conn.execute(
            "SELECT task_id, description, failed_vp_id, files_to_modify "
            "FROM plan_tasks WHERE plan_id = ?",
            (PLAN_ID,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "round": None if row is None else int(row[0]),
        "ledger": _decode_json(row[1] if row is not None else None),
        "verdicts": _decode_json(row[2] if row is not None else None),
        "phase": None if phase_row is None else phase_row[0],
        "tasks": [
            {
                "task_id": task_row[0],
                "description": task_row[1] or "",
                "failed_vp_id": task_row[2] or "",
                "files_to_modify": _decode_json(task_row[3]) or [],
            }
            for task_row in task_rows
        ],
    }


def _ledger_entries(ledger: Any) -> List[Dict[str, Any]]:
    """The ledger's per-point entries, whichever shape it carries."""
    if not isinstance(ledger, dict):
        return []
    entries = ledger.get("execution_results")
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, dict)]
    return []


def _wait_for_preflight_footprint(
    db_path: Path, timeout: float = PREFLIGHT_FOOTPRINT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Wait for the preflight's own footprint, and read the state back.

    The footprint is the repair task the preflight wrote: its description
    names the root-cause report it continues (``root-cause-<vp>.md``) — the
    same thing the product's own ``_is_root_cause_task`` recognises a
    chain task by.  A round that ran for itself would write repair rows of
    its own, but not ones naming a root-cause report.

    The wait returns whatever it found when the budget runs out — the
    caller asserts on it, so a preflight that never happened fails the
    test instead of passing it.
    """
    deadline = time.monotonic() + timeout
    state: Dict[str, Any] = {}
    while True:
        state = _read_state_readonly(db_path)
        if any(
            "root-cause-" in task["description"] for task in state["tasks"]
        ):
            return state
        if time.monotonic() >= deadline:
            return state
        time.sleep(0.2)


def _round_number(label: str) -> Optional[int]:
    """The round a label names — ``"Round 2/4"`` → ``2``."""
    match = re.search(r"(\d+)\s*/\s*(\d+)", label or "")
    return None if match is None else int(match.group(1))


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def round_scope(tmp_path_factory) -> Dict[str, Any]:
    """A real backend, a real browser, and what the page showed.

    The backend is a real ``backend.server`` process against a plans tree
    and a state database that live in pytest's temporary directory.  The
    browser is a real Chromium driven through the page's own events.
    """
    work = tmp_path_factory.mktemp("round-scope-verification-e2e")
    _seed_plan(work)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    proc = _boot_server(work, port, _script_dir(work))
    try:
        _wait_ready(base_url, proc)
        try:
            report = _drive(base_url, SCREENSHOT_PATH, work)
        except Exception as exc:  # noqa: BLE001 - the server log is context
            # The backend's own output is attached to the failure: a boot
            # that hung says where it stopped, and a round that crashed
            # says why.
            raise AssertionError(
                f"{exc}\n--- tail of the backend's own output ---\n"
                f"{_server_output(proc)}"
            ) from exc
        # The state, read back over a read-only connection while the
        # server is still up.
        state = _wait_for_preflight_footprint(work / "state.db")
        status_code, status_body = _http_json(
            "GET", f"{base_url}/api/verification/{PLAN_ID}/status"
        )
    finally:
        _teardown(proc)

    points = []
    if isinstance(status_body, dict):
        points = status_body.get("verification_points") or []
    return {
        "base_url": base_url,
        "work": work,
        "report": report,
        "state": state,
        "status_code": status_code,
        "status_body": status_body,
        "status_points": points,
    }


# ---------------------------------------------------------------------------
# The acceptance
# ---------------------------------------------------------------------------


def test_round_scope_ui_e2e(round_scope):
    """第二轮不重跑、复用在页面上看得见、链预检不推进轮次。

    操作员走的是同一条路：真后端、真页面、真点击。断言的是**他看到的**：

    * 侧边栏点开计划之后，验证面板上有**三个**验证点（多一个少一个都
      是渲染错了）；
    * 面板上的轮次是**第 2 轮**；
    * 点「启动验证」之后**两秒**里，轮次**没有**往前走 —— 链的预检接管了
      这一轮，它不推进轮次；
    * 库里 ``plan_verification.round`` 也还是 2（页面上说的和库里存的
      是同一件事）；
    * 状态接口答 200 且带着验证点（页面上的那些点就是它给的）。

    后面还有两条，它们是上面那条**不空转**的保证：

    * 这一轮**没有跑** —— 账本里还是种子那三条（其中一条带着
      ``reused_from_round=1``），没有新的裁决被写下来。一轮真跑过的
      验证会把账本改写成它自己的结果；
    * 预检**接走了**这一轮 —— 它写下了一条点名根因报告的修复任务行。
      「轮次没前进」如果只是因为 start 被拒绝了，这一条就会空。
    """
    report = round_scope["report"]
    state = round_scope["state"]

    assert report.get("verification_panel_visible") is True, (
        f"the verification panel did not open: {report!r}"
    )
    assert int(report.get("point_count_before") or 0) == EXPECTED_POINT_COUNT, (
        f"the panel shows {report.get('point_count_before')!r} verification "
        f"points, not {EXPECTED_POINT_COUNT}: {report!r}"
    )
    assert tuple(report.get("point_ids") or ()) == EXPECTED_POINT_IDS, (
        f"the verification points are {report.get('point_ids')!r}, not "
        f"{list(EXPECTED_POINT_IDS)!r}: {report!r}"
    )

    round_before = str(report.get("round_before") or "")
    assert str(EXPECTED_ROUND) in round_before, (
        f"the round reads {round_before!r}, which does not name round "
        f"{EXPECTED_ROUND}: {report!r}"
    )
    assert _round_number(round_before) == EXPECTED_ROUND, (
        f"the round reads {round_before!r}, which is not round "
        f"{EXPECTED_ROUND}: {report!r}"
    )

    round_after = str(report.get("round_after") or "")
    assert _round_number(round_after) == EXPECTED_ROUND, (
        f"after the start click the round reads {round_after!r} — the round "
        f"moved on: {report!r}"
    )

    assert state["round"] == SEEDED_ROUND, (
        f"plan_verification.round is {state['round']!r}, not {SEEDED_ROUND}: "
        f"{report!r}"
    )

    assert round_scope["status_code"] == 200, (
        f"the status endpoint answered {round_scope['status_code']!r}: "
        f"{round_scope['status_body']!r}"
    )
    assert round_scope["status_points"], (
        "the status endpoint answered without any verification point: "
        f"{round_scope['status_body']!r}"
    )

    assert SCREENSHOT_PATH.is_file(), (
        f"the screenshot was not archived: {SCREENSHOT_PATH}"
    )
    assert SCREENSHOT_PATH.stat().st_size > 0, (
        f"the screenshot is empty: {SCREENSHOT_PATH}"
    )
    # The screenshot is a runtime artefact, and the repository's convention
    # is that it never enters the index.  Asserted here so that a future
    # change of the path cannot quietly start staging it.
    assert _is_gitignored(SCREENSHOT_PATH), (
        f"the screenshot is not gitignored — it would enter the index on "
        f"the next `git add -A`: {SCREENSHOT_PATH}"
    )

    assert report.get("page_errors") == [], (
        f"the page raised uncaught JavaScript exceptions: "
        f"{report.get('page_errors')!r}"
    )
    assert not [
        dialog for dialog in (report.get("dialogs") or [])
        if "启动验证失败" in dialog
    ], (
        f"the start click was refused by the backend: "
        f"{report.get('dialogs')!r}"
    )

    # --- the start click was a real browser click -----------------------
    #
    # The page's own capturing listener records ``isTrusted``: a click the
    # browser generated is trusted, a synthetic ``dispatchEvent`` is not.
    # So this assertion is what separates "the operator clicked the button"
    # from "the test pushed an event at the button".
    clicks = report.get("start_clicks") or []
    assert clicks, (
        f"the start button was never clicked: {report!r}"
    )
    assert all(
        click.get("is_trusted") is True for click in clicks
    ), (
        f"the start click was not a real browser click: {clicks!r}"
    )

    # --- the round did not run -----------------------------------------
    entries = _ledger_entries(state["ledger"])
    assert [entry.get("id") for entry in entries] == list(EXPECTED_POINT_IDS), (
        f"the ledger no longer holds the seeded round: {entries!r}"
    )
    reused = {
        entry.get("id"): entry.get("reused_from_round") for entry in entries
    }
    assert reused.get(REUSED_VP_ID) == REUSED_FROM_ROUND, (
        f"the reused conclusion lost its marker: {reused!r}"
    )
    assert reused.get("VP-002") is None and reused.get("VP-003") is None, (
        f"only one entry may be reused, but the markers say: {reused!r}"
    )
    assert not state["verdicts"], (
        f"the round was judged — a round that ran leaves verdicts behind: "
        f"{state['verdicts']!r}"
    )

    # --- the preflight took the round over -----------------------------
    chain_tasks = [
        task for task in state["tasks"] if "root-cause-" in task["description"]
    ]
    assert chain_tasks, (
        "the preflight dispatched nothing: the round was not taken over by "
        f"the chain, so 'the round did not advance' proves nothing: "
        f"{state['tasks']!r}"
    )
    assert any(
        task["failed_vp_id"] == CHAIN_WORK_VP_ID for task in chain_tasks
    ), (
        f"the preflight continued the wrong point: {chain_tasks!r}"
    )

"""端到端验收：真浏览器点开计划卡，任务视图与验证面板都在。

背景
----
前 12 个任务改的是守护进程、门禁汇总、判定状态与门禁容器 —— 全部落在
**后端脚本层**。没有一条断言证明：这些改动之后，**dashboard 还是可以用的**。

这一份补上那一段。真后端（不 mock）、真 Chromium（Playwright）、真点击：
用户点开计划卡，任务列表与验证面板都必须出现，且带着正确的结论。

被钉住的东西
------------
* ``.task-card`` **恰好两张**，标题与种子内容逐字相等（等于，不是大于等于）；
* ``#verification-status-badge`` 是「已通过」；
* ``#verification-round`` 是「Round 2/3」；
* 页面全程没有未捕获异常，也没有 console error（资源 404 不算）；
* 截图落在 pytest 的临时目录里，**不落进仓库工作区**；
* 跑完之后仓库工作区没有多出文件。

真实的东西
----------
* 后端由测试自己起（``backend.server`` 子进程），计划树与状态库都在 pytest
  的临时目录里 —— 不是被 mock 出来的；
* 浏览器是 Playwright 驱动的真 Chromium；
* 页面上的每一步都是**点击**，走的是前端自己的事件与自己的 fetch：面板
  出现是因为**页面决定**让它出现，不是因为测试把内容塞了进去。

复用
----
``tests/e2e/test_execution_status_restart_e2e.py`` 的 ``_free_port`` /
``_seed`` / ``_boot`` / ``_wait_ready`` / ``_teardown``；浏览器发现用
``tests/e2e/test_token_convergence_ui_acceptance.py`` 的
``browser_executable()`` 与 ``_playwright_interpreter()``。

边界条件
--------
* 没装 Chromium → 整份文件跳过（``chromium is not installed``）；
* 当前解释器 import 不到 ``playwright.sync_api`` → 用
  ``_playwright_interpreter()`` 找到的解释器以子进程跑同一段驱动；
* 后端起不来 → 失败，并把服务端自己的输出打出来；
* 失败路径也必须把后端停掉（``_teardown``）。

跑法::

    cd backend && .venv/bin/python3 -m pytest \\
        tests/e2e/test_dashboard_plan_and_verification_e2e.py -v
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import subprocess
import sys
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
#: 仓库根 —— ``git status`` 的基准面。
REPO_ROOT = PROJECT_ROOT

# ``state_machine`` / ``config_paths`` 这些包住在 ``backend/`` 底下，两份
# pytest 配置都把它放进了 ``sys.path``；这里再补一刀，让这份文件在
# 两种配置下都能 import 到它。
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: 这个 plan 的 id。计划目录、``plan_routing`` 行、``plan_verification``
#: 行都以它为键。
PLAN_ID = "e2e-dashboard"

#: 种子里的两条任务 —— 断言的是**逐字相等**，不是「至少两条」。
SEEDED_TASKS = (
    {"id": "T1", "title": "实现导出按钮", "description": "d",
     "files_to_modify": [], "depends_on": []},
    {"id": "T2", "title": "补导出测试", "description": "d",
     "files_to_modify": [], "depends_on": []},
)
EXPECTED_TASK_TITLES = ("实现导出按钮", "补导出测试")

#: 种子里写进 ``plan_verification`` 的那一轮。
SEEDED_ROUND = 2
SEEDED_MAX_ROUNDS = 3

#: 页面上必须出现的两个词。
EXPECTED_VERIFICATION_STATUS = "已通过"
EXPECTED_VERIFICATION_ROUND = "Round 2/3"

# --- 「循环停止」现场 ------------------------------------------------------
#
# 这是**另一个**计划，与 :data:`PLAN_ID` 分开：那个计划的验证结论是
# 「已通过」，这个计划的结论是「循环停止」—— 循环在第 2 轮因为基线漂移
# 冲突停下来了。两者的断言互不干扰。

#: 「循环停止」那个计划的 id。
LOOP_STOPPED_PLAN_ID = "e2e-dashboard-loop-stopped"

#: 种子里写进 ``plan_verification`` 的那一轮：第 2 轮，上限 4 轮。
LOOP_STOPPED_ROUND = 2
LOOP_STOPPED_MAX_ROUNDS = 4

#: 停止原因 —— 基线漂移冲突。
LOOP_STOPPED_REASON = "base_drift_conflicts"

#: 页面上必须出现的两个词（这一条场景的）。
EXPECTED_LOOP_STOPPED_STATUS = "循环停止"
EXPECTED_LOOP_STOPPED_ROUND = "Round 2/4"

#: 这个计划的任务：一条，够任务视图渲染出任务卡就行。
LOOP_STOPPED_TASKS = (
    {"id": "T1", "title": "收尾那条验证结论", "description": "d",
     "files_to_modify": [], "depends_on": []},
)

#: 浏览器在等各个页面元素时的预算（毫秒）。没有固定 sleep：一个等不到的
#: 等待就是一次带原因的失败。
BROWSER_TIMEOUT_MS = 30000

#: 整段浏览器过程的预算（秒）。
DRIVER_TIMEOUT_SECONDS = 240

#: 截图的文件名。它落在 pytest 的临时目录里 —— **不在**仓库工作区里。
SCREENSHOT_NAME = "dashboard.png"

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


#: 任务 4 的助手：真后端进程、种子、空闲端口。
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
_seed = _RESTART._seed
_boot = _RESTART._boot
_wait_ready = _RESTART._wait_ready
_teardown = _RESTART._teardown
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

argv: <base_url> <plan_id> <screenshot_path> <chromium_executable>

Every step is a click on the page, so the frontend's own handlers run:
``resumePlan`` → ``showTasksView`` → ``resumeVerificationPanel``.  Nothing
here reaches into the application's internals to make a panel show up.

The report is one JSON object on stdout.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


base_url, plan_id, screenshot_path, chromium = sys.argv[1:5]

#: How long the browser waits for each of the things it needs.
timeout_ms = 30000

report = {"page_errors": [], "console_errors": []}

with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path=chromium)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))

    def _on_console(message):
        if message.type != "error":
            return
        text = message.text or ""
        # A resource that 404s is reported by the browser as a console
        # error; the dashboard asks for a handful of optional assets.  That
        # is not a page error.  Everything else is.
        if "Failed to load resource" in text:
            return
        report["console_errors"].append(text)

    page.on("console", _on_console)

    page.goto(base_url, wait_until="load")
    page.wait_for_selector("#app", timeout=timeout_ms)

    # --- 1: the plan card ---------------------------------------------
    card = '.plan-card[data-plan="%s"]' % plan_id
    page.wait_for_selector(card, timeout=timeout_ms)
    report["plan_card_visible"] = page.is_visible(card)

    # --- 2: the user clicks it -----------------------------------------
    page.click(card)

    # The task cards are the point of the click.
    page.wait_for_selector(".task-card", timeout=timeout_ms)

    # --- 3: the verification panel opens -------------------------------
    page.wait_for_selector(
        "#verification-panel", state="visible", timeout=timeout_ms
    )
    report["verification_panel_visible"] = page.is_visible(
        "#verification-panel"
    )

    # The panel is un-hidden one step BEFORE the badge is written, so the
    # panel being visible is not yet "the badge has been painted".  Wait
    # for the badge to leave its static initial value -- the assertion
    # below then still has teeth: it must be exactly 「已通过」.
    page.wait_for_function(
        """() => {
            const badge = document.querySelector('#verification-status-badge');
            if (!badge) return false;
            const text = (badge.textContent || '').trim();
            return text !== '' && text !== '未开始';
        }""",
        timeout=timeout_ms,
    )

    report["task_card_count"] = page.locator(".task-card").count()
    report["task_titles"] = [
        (page.locator(".task-title").nth(index).inner_text() or "").strip()
        for index in range(report["task_card_count"])
    ]
    report["verification_badge"] = (
        page.inner_text("#verification-status-badge") or ""
    ).strip()
    report["verification_round"] = (
        page.inner_text("#verification-round") or ""
    ).strip()

    # --- 4: the screenshot ---------------------------------------------
    shot = Path(screenshot_path)
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot), full_page=True)
    report["screenshot"] = str(shot)
    report["screenshot_size"] = shot.stat().st_size if shot.exists() else 0

    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


# ---------------------------------------------------------------------------
# The browser driver for the 「循环停止」 scene
# ---------------------------------------------------------------------------
#
# Its own driver, not a flag on the one above: the two scenes assert
# different things about the same panel, and the assertions that belong
# to one of them must not be able to satisfy — or break — the other.

_LOOP_STOPPED_BROWSER_DRIVER = r'''
"""Drive the served page and read the 「循环停止」 verification panel.

argv: <base_url> <plan_id> <screenshot_path> <chromium_executable>

Same road as the driver above — load, click the plan card, read the
panel — with one difference: what it reads is the **loop-stopped**
verdict.  The stop reason and the round have to be on the page, not
only in the database: 「停止原因与结果必须能在面板上被看到」.

Every step is a click, so the frontend's own handlers run
(``resumePlan`` → ``showTasksView`` → ``resumeVerificationPanel``).
Nothing here reaches into the application's internals.

The report is one JSON object on stdout.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


base_url, plan_id, screenshot_path, chromium = sys.argv[1:5]

#: How long the browser waits for each of the things it needs.
timeout_ms = 30000

report = {"page_errors": [], "console_errors": []}

with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path=chromium)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))

    def _on_console(message):
        if message.type != "error":
            return
        text = message.text or ""
        # A resource that 404s is reported by the browser as a console
        # error; the dashboard asks for a handful of optional assets.
        # That is not a page error.  Everything else is.
        if "Failed to load resource" in text:
            return
        report["console_errors"].append(text)

    page.on("console", _on_console)

    page.goto(base_url, wait_until="load")
    page.wait_for_selector("#app", timeout=timeout_ms)

    # --- 1: the plan card ---------------------------------------------
    card = '.plan-card[data-plan="%s"]' % plan_id
    page.wait_for_selector(card, timeout=timeout_ms)
    report["plan_card_visible"] = page.is_visible(card)

    # --- 2: the user clicks it -----------------------------------------
    page.click(card)

    # --- 3: the verification panel opens -------------------------------
    page.wait_for_selector(
        "#verification-panel", state="visible", timeout=timeout_ms
    )
    report["verification_panel_visible"] = page.is_visible(
        "#verification-panel"
    )

    # The panel is un-hidden one step BEFORE the badge is written, so the
    # panel being visible is not yet "the badge has been painted".  Wait
    # for the badge to leave its static initial value -- the assertion
    # in the test then still has teeth: it must be exactly 「循环停止」.
    page.wait_for_function(
        """() => {
            const badge = document.querySelector('#verification-status-badge');
            if (!badge) return false;
            const text = (badge.textContent || '').trim();
            return text !== '' && text !== '未开始';
        }""",
        timeout=timeout_ms,
    )

    # --- 4: what the panel says ----------------------------------------
    report["verification_badge"] = (
        page.inner_text("#verification-status-badge") or ""
    ).strip()
    report["verification_round"] = (
        page.inner_text("#verification-round") or ""
    ).strip()
    report["verification_result_area_visible"] = page.is_visible(
        "#verification-result-area"
    )
    report["verification_overall_result"] = (
        page.inner_text("#verification-overall-result") or ""
    ).strip()
    report["verification_repair_area_visible"] = page.is_visible(
        "#verification-repair-area"
    )

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

    ``$HOME`` 指向回跑起来时的那个 home（见 :data:`REAL_HOME_ENV`），这样
    Playwright 找得到它装在那儿的浏览器二进制。
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


def _drive_in_process(
    argv: List[str], driver_source: str = _BROWSER_DRIVER
) -> Dict[str, Any]:
    """Run the driver inside this interpreter (it has playwright)."""
    buffer = io.StringIO()
    previous_argv = list(sys.argv)
    # The driver reads its arguments the way a script does -- ``argv[1:]``
    # -- so the program name has to be there too.
    sys.argv = ["<dashboard-browser-driver>", *argv]
    try:
        with redirect_stdout(buffer):
            exec(  # noqa: S102  # nosec B102 - the driver, run verbatim
                compile(driver_source, "<dashboard-browser-driver>", "exec"),
                {"__name__": "__main__"},
            )
    finally:
        sys.argv = previous_argv
    return _parse_report(buffer.getvalue())


def _drive_in_subprocess(
    interpreter: str,
    argv: List[str],
    work: Path,
    driver_source: str = _BROWSER_DRIVER,
    script_name: str = "drive_dashboard.py",
) -> Dict[str, Any]:
    """Run the driver as its own process, with an interpreter that has playwright."""
    script = work / script_name
    script.write_text(driver_source, encoding="utf-8")
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


def _drive(
    base_url: str, screenshot_path: Path, work: Path
) -> Dict[str, Any]:
    """Drive the page and return what it showed."""
    chromium = _upstream_browser_executable()
    if not chromium:
        pytest.skip("chromium is not installed")

    argv = [base_url, PLAN_ID, str(screenshot_path), chromium]

    if _current_interpreter_has_playwright():
        return _drive_in_process(argv)

    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no interpreter with playwright is installed in this environment"
        )
    return _drive_in_subprocess(interpreter, argv, work)


def _drive_loop_stopped(
    base_url: str, screenshot_path: Path, work: Path
) -> Dict[str, Any]:
    """Drive the page of the 「循环停止」 plan and return what it showed."""
    chromium = _upstream_browser_executable()
    if not chromium:
        pytest.skip("chromium is not installed")

    argv = [base_url, LOOP_STOPPED_PLAN_ID, str(screenshot_path), chromium]

    if _current_interpreter_has_playwright():
        return _drive_in_process(argv, _LOOP_STOPPED_BROWSER_DRIVER)

    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no interpreter with playwright is installed in this environment"
        )
    return _drive_in_subprocess(
        interpreter,
        argv,
        work,
        _LOOP_STOPPED_BROWSER_DRIVER,
        "drive_dashboard_loop_stopped.py",
    )


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------


def _seed_plan(work: Path) -> None:
    """Write the plan the dashboard is going to show.

    Three pieces, each on the surface the server reads it from:

    * ``plans/<id>/tasks.json`` —— 任务列表（前端的任务卡就是它渲染的）；
    * ``plan_routing`` 行 —— 计划在哪一步；没有它，列表里没有这张卡，
      点开也不知道该去哪个视图；
    * ``plan_verification`` 行 —— 验证到第几轮、结论是什么。

    The verification row goes in through the repository, exactly the way
    the product writes it: ``init_round`` marks the round as started,
    ``complete_round`` records its verdict.  ``publish_closed=False``
    because the round is over but the **plan** is not: the plan is still
    on the task view, and a terminal announce would say otherwise.
    """
    plan_dir = work / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": list(SEEDED_TASKS)}, ensure_ascii=False),
        encoding="utf-8",
    )

    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = db_open(work / "state.db")
    try:
        migrate(conn)
        repository = VerificationRepository(conn)
        repository.init_round(PLAN_ID, SEEDED_ROUND, SEEDED_MAX_ROUNDS)
        repository.complete_round(
            PLAN_ID,
            {
                "pytest_summary": "2 passed",
                "llm_findings": "",
                "performance_metrics": {},
            },
            status="passed",
            publish_closed=False,
        )
    finally:
        conn.close()


def _seed_loop_stopped_plan(work: Path) -> None:
    """Write the plan whose verification loop **stopped**.

    Four pieces, each on the surface the server reads it from:

    * ``plans/<id>/tasks.json`` —— 任务列表。计划目录必须存在：页面读的
      ``GET /api/plan/{id}/state`` 与 ``GET /api/verification/{id}/status``
      都拒绝一个没有目录的计划；
    * ``plan_routing`` 行 —— 相位写成 ``verification``。前端
      ``resumePlan`` 靠 ``phase.startsWith("verification")`` 进任务视图，
      验证面板才会在那里渲染；
    * ``plan_verification`` 行 —— 第 2 轮、上限 4 轮，结论
      ``loop_stopped``，停止原因 ``base_drift_conflicts``。这一行走的是
      生产路径：``init_round`` 开一轮，``mark_stopped`` 写下结论与原因；
    * ``plan_execution`` 行 —— ``running`` 且**没有 pid**。这是现场本身：
      后端启动时的恢复扫描看到「说是运行中、却没有进程」，会走崩溃裁决。
      裁决要给这个计划写下终局相位 —— 而这条计划**还有一条开着的验证
      循环**，它的结论（循环停止）必须在面板上被看到。少了这一行，这
      条用例就绕开了缺陷，等于没测。

    The row is written through the repositories, exactly the way the
    product writes it.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    plan_dir = work / "plans" / LOOP_STOPPED_PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": list(LOOP_STOPPED_TASKS)}, ensure_ascii=False),
        encoding="utf-8",
    )

    conn = db_open(work / "state.db")
    try:
        migrate(conn)

        # The phase: the plan is in its verification stage.
        RoutingRepository(conn).insert(LOOP_STOPPED_PLAN_ID, "verification")

        # The verification row: round 2 of 4, and the loop stopped on a
        # base-drift conflict.
        verification = VerificationRepository(conn)
        verification.init_round(
            LOOP_STOPPED_PLAN_ID, LOOP_STOPPED_ROUND, LOOP_STOPPED_MAX_ROUNDS
        )
        verification.mark_stopped(LOOP_STOPPED_PLAN_ID, LOOP_STOPPED_REASON)

        # The execution row: running, with no pid.  The startup recovery
        # scan judges it dead — the scene the panel has to survive.
        ExecutionRepository(conn).update_fields(
            LOOP_STOPPED_PLAN_ID,
            create_if_missing=True,
            exec_status="running",
            exec_pid=None,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The repository's cleanliness
# ---------------------------------------------------------------------------


def _git_porcelain() -> List[str]:
    """``git status --porcelain`` at the repository root, one entry per line."""
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git status failed (exit {result.returncode}): {result.stderr!r}"
        )
    return [line for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def repo_status_before() -> List[str]:
    """The repository's status **before** the run touches anything."""
    return _git_porcelain()


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory, repo_status_before) -> Dict[str, Any]:
    """A real backend, a real browser, and what the page showed.

    The backend is a real ``backend.server`` process against a plans tree
    and a state database that live in pytest's temporary directory.  The
    browser is a real Chromium driven through the page's own events.
    """
    work = tmp_path_factory.mktemp("dashboard-plan-verification-e2e")
    _seed(work, PLAN_ID)
    _seed_plan(work)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    screenshot_path = work / SCREENSHOT_NAME

    proc = _boot(work, port)
    try:
        _wait_ready(base_url, proc)
        try:
            report = _drive(base_url, screenshot_path, work)
        except Exception as exc:  # noqa: BLE001 - the server log is context
            # ``Exception``, not ``BaseException``: pytest's ``Skipped``
            # inherits from ``BaseException`` (``Skipped -> OutcomeException
            # -> BaseException``), so a wider net here rewrote
            # ``pytest.skip("chromium is not installed")`` into an
            # ``AssertionError`` -- a machine without a browser reported a
            # product failure instead of skipping.  ``AssertionError`` /
            # ``TimeoutExpired`` / a failed subprocess are all ``Exception``
            # subclasses, so they still arrive here with the backend's own
            # output attached.
            raise AssertionError(
                f"{exc}\n--- tail of the backend's own output ---\n"
                f"{_server_output(proc)}"
            ) from exc
    finally:
        # The failure path stops the backend too.
        _teardown(proc)

    return {
        "base_url": base_url,
        "work": work,
        "report": report,
        "screenshot": Path(str(report.get("screenshot") or screenshot_path)),
        "repo_status_before": repo_status_before,
    }


@pytest.fixture(scope="module")
def loop_stopped_dashboard(tmp_path_factory, repo_status_before) -> Dict[str, Any]:
    """A real backend, a real browser, and the 「循环停止」 panel.

    Its own backend: this scene needs a plan tree whose only plan is the
    stopped one, so that what the page shows cannot be another plan's
    card.  Everything else is the same road as :func:`dashboard` — a
    real ``backend.server`` process, a real Chromium, real clicks.
    """
    work = tmp_path_factory.mktemp("dashboard-loop-stopped-e2e")
    _seed_loop_stopped_plan(work)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    screenshot_path = work / SCREENSHOT_NAME

    proc = _boot(work, port)
    try:
        _wait_ready(base_url, proc)
        try:
            report = _drive_loop_stopped(base_url, screenshot_path, work)
        except Exception as exc:  # noqa: BLE001 - the server log is context
            # ``Exception``, not ``BaseException`` — see the fixture above
            # for why: a ``pytest.skip`` must stay a skip.
            raise AssertionError(
                f"{exc}\n--- tail of the backend's own output ---\n"
                f"{_server_output(proc)}"
            ) from exc
    finally:
        # The failure path stops the backend too.
        _teardown(proc)

    return {
        "base_url": base_url,
        "work": work,
        "report": report,
        "screenshot": Path(str(report.get("screenshot") or screenshot_path)),
        "repo_status_before": repo_status_before,
    }


# ---------------------------------------------------------------------------
# The assertions
# ---------------------------------------------------------------------------


def test_a_user_opens_a_plan_and_sees_its_tasks_and_verification(dashboard):
    """点开计划卡之后，任务列表与验证面板都是种子里的那一份。

    用户走的是同一条路：真后端、真页面、真点击。任务卡的**数量**与
    **标题**必须与种子逐字相等 —— 少了是丢了一张，多了是渲染错了，
    「至少两条」会把两者都放过。

    验证面板同理：徽标说的是这一轮的结论，轮次说的是这一轮是第几轮。
    """
    report = dashboard["report"]

    assert report.get("plan_card_visible") is True, (
        f"the plan card never appeared on the page: {report!r}"
    )
    assert report.get("verification_panel_visible") is True, (
        f"the verification panel did not open: {report!r}"
    )

    assert int(report.get("task_card_count") or 0) == len(EXPECTED_TASK_TITLES), (
        f"the task list shows {report.get('task_card_count')!r} cards, not "
        f"{len(EXPECTED_TASK_TITLES)}: {report!r}"
    )
    assert list(report.get("task_titles") or []) == list(EXPECTED_TASK_TITLES), (
        f"the task titles are {report.get('task_titles')!r}, not "
        f"{list(EXPECTED_TASK_TITLES)!r}: {report!r}"
    )

    assert report.get("verification_badge") == EXPECTED_VERIFICATION_STATUS, (
        f"the verification badge reads {report.get('verification_badge')!r}, "
        f"not {EXPECTED_VERIFICATION_STATUS!r}: {report!r}"
    )
    assert report.get("verification_round") == EXPECTED_VERIFICATION_ROUND, (
        f"the verification round reads {report.get('verification_round')!r}, "
        f"not {EXPECTED_VERIFICATION_ROUND!r}: {report!r}"
    )


def test_the_page_raised_no_errors(dashboard):
    """页面全程没有未捕获异常，也没有 console error。

    资源 404 不算：浏览器把「一个资源没找到」也报成 console error，而
    仪表盘会去问几个可有可无的静态资源。除此之外的 error 一律算失败 ——
    一个「有错误但不影响断言」的页面不是可用的仪表盘。
    """
    report = dashboard["report"]

    assert report.get("page_errors") == [], (
        f"the page raised uncaught JavaScript exceptions: "
        f"{report.get('page_errors')!r}"
    )
    assert report.get("console_errors") == [], (
        f"the page logged console errors: {report.get('console_errors')!r}"
    )


def test_a_screenshot_is_archived(dashboard, tmp_path_factory):
    """截图存在、非空，而且**不在仓库工作区里**。

    证据落在 pytest 的临时目录里 —— 仓库工作区里多一张 PNG、一个日志、
    一个 artifact，都是要跟着提交走的东西。
    """
    screenshot = dashboard["screenshot"]

    assert screenshot.is_file(), f"the screenshot was not written: {screenshot}"
    assert screenshot.stat().st_size > 0, f"the screenshot is empty: {screenshot}"

    resolved = screenshot.resolve()
    repo_root = REPO_ROOT.resolve()
    assert not resolved.is_relative_to(repo_root), (
        f"the screenshot landed inside the repository workspace: {resolved}"
    )
    base_temp = Path(str(tmp_path_factory.getbasetemp())).resolve()
    assert resolved.is_relative_to(base_temp), (
        f"the screenshot is outside pytest's temporary directory: {resolved}"
    )


def test_the_run_leaves_the_repository_clean(dashboard):
    """跑完之后，仓库工作区没有多出文件。

    「相对测试开始」—— 前后各取一次 ``git status --porcelain``，两次的差集
    必须为空。已有的改动（这份测试文件本身）不算新增。
    """
    before = set(dashboard["repo_status_before"])
    after = set(_git_porcelain())

    new_entries = sorted(after - before)
    assert new_entries == [], (
        f"the run left {len(new_entries)} new entries in the repository "
        f"workspace: {new_entries!r}"
    )


# ---------------------------------------------------------------------------
# The 「循环停止」 scene: the loop's verdict has to be visible
# ---------------------------------------------------------------------------
#
# §3 的用户可见面：**停止原因与结果必须能在面板上被看到，而不是只留在
# 数据库里。**
#
# 场景：验证循环在第 2 轮因为基线漂移冲突停下来了；同时后端重启时的
# 恢复扫描看到一条「running 却没有 pid」的执行行，给这个计划下了崩溃
# 裁决。这条计划**还有一条开着的验证循环**，所以崩溃这个结论不替它
# 说话 —— 面板上必须仍然是「循环停止」，而不是别的什么。


def test_a_loop_stopped_verification_is_visible_on_the_panel(loop_stopped_dashboard):
    """点开那个计划，面板上写的是「循环停止」，第 2 轮，共 4 轮。

    走的是用户的那条路：真后端、真 Chromium、真点击。每一句断言读的都是
    **页面自己的 DOM** —— 徽标、轮次、结论区、修复区。数据库里的
    ``loop_stopped`` 不算数：这一条钉的就是「结论在面板上被看到」。
    """
    report = loop_stopped_dashboard["report"]

    assert report.get("plan_card_visible") is True, (
        f"the plan card never appeared on the page: {report!r}"
    )
    assert report.get("verification_panel_visible") is True, (
        f"the verification panel did not open: {report!r}"
    )

    assert report.get("verification_badge") == EXPECTED_LOOP_STOPPED_STATUS, (
        f"the verification badge reads {report.get('verification_badge')!r}, "
        f"not {EXPECTED_LOOP_STOPPED_STATUS!r}: {report!r}"
    )
    assert report.get("verification_round") == EXPECTED_LOOP_STOPPED_ROUND, (
        f"the verification round reads {report.get('verification_round')!r}, "
        f"not {EXPECTED_LOOP_STOPPED_ROUND!r}: {report!r}"
    )

    assert report.get("verification_result_area_visible") is True, (
        f"the verification result area is not visible: {report!r}"
    )
    assert EXPECTED_LOOP_STOPPED_STATUS in (
        report.get("verification_overall_result") or ""
    ), (
        f"the overall result reads "
        f"{report.get('verification_overall_result')!r}, which does not "
        f"say {EXPECTED_LOOP_STOPPED_STATUS!r}: {report!r}"
    )
    assert report.get("verification_repair_area_visible") is True, (
        f"the verification repair area is not visible: {report!r}"
    )


def test_the_loop_stopped_page_raised_no_errors(loop_stopped_dashboard):
    """这条场景的页面也没有未捕获异常，也没有 console error。

    资源 404 不算 —— 与本文件既有的那条断言同一把尺子。
    """
    report = loop_stopped_dashboard["report"]

    assert report.get("page_errors") == [], (
        f"the page raised uncaught JavaScript exceptions: "
        f"{report.get('page_errors')!r}"
    )
    assert report.get("console_errors") == [], (
        f"the page logged console errors: {report.get('console_errors')!r}"
    )
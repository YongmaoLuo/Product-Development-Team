"""端到端：一个从未验证过的计划不再刷 ERROR 记录（真后端 + 真浏览器）。

背景
----
任务 1 的修复停在 **API 层**：``backend/routes/plans.py`` 的
``_plan_status_payload`` 认出了 ``HTTPException(404, {"error":
"verification not started"})`` 这张**正常的答案**，把它降级成
``verification = None``，并且只记一行 DEBUG
（``[plan_status] verification not started plan=<id>``）——不再记 ERROR。

API 层答对了，不等于**用户看得见**、**日志干净**。真实链路是这样的：

* 计划页 ``frontend/app.js`` 的 ``resumeVerificationPanel()`` 读
  ``/api/verification/{id}/status``；一个还在执行、验证从未开始的计划在
  这里落到 catch 分支，用 ``{verification_status: "pending"}`` 渲染，
  徽标显示「未开始」；
* 计划页上那张卡片的快照走 ``GET /api/plan/{id}/status``
  （``backend/routes/plans.py``），它在服务端组装 summary / execution /
  verification 三段——**那一行 DEBUG 就是从这里写出来的**；
* 整条链路上，前端是由**同一个后端**在 ``/`` 上静态托管的：不需要另外
  起一个前端进程，浏览器打开的就是这个后端自己送出来的那份页面。

所以这一份是**真的端到端**：真的起一个 ``backend.server``，真的用真的
Chromium 打开它自己托管的页面，真的**点那张计划卡片**，真的在同一个浏览器
上下文里发一次和前端 ``api()`` 一模一样的 ``fetch``，最后读后端**自己的
日志全文**。

三个测试，三个断面
------------------
* ``test_plan_page_shows_verification_not_started`` —— 页面上徽标是
  「未开始」、面板是**打开的**（打开面板这件事只有前端自己会做：初始
  HTML 里 ``#verification-panel`` 是 ``hidden`` 的）；
* ``test_plan_status_endpoint_is_clean_in_browser_context`` —— 浏览器
  上下文里那个 ``fetch`` 答 200、``verification`` 是 ``None``、阶段还是
  ``executing``、``summary`` 不是 ``None``；
* ``test_server_log_has_no_plan_status_error`` —— 日志全文里没有
  ``[plan_status] verification section failed``，也没有属于这条计划的
  ``[plan_status]`` ERROR 行；而**正控**那行 DEBUG 在场。

这个计划为什么是**真的在执行**
------------------------------
「执行中」不是一个测试可以随手写进 routing 行的值：
``plan_status.resolve_phase`` 会**反驳**一个身后没有任何执行记录的运行
相位——一个 routing 行说 ``executing`` 而身后没有进程的计划，读出来是
「歇着」，不是「在跑」（这正是那条规则要消灭的「一条计划两个答案」）。

所以这条计划的执行是**真的**：测试把 routing 行种在 ``queued``
（``server._EXECUTION_START_SOURCE_PHASES`` 里可以发起执行的一个相位），
然后通过 ``POST /api/execution/{id}/start`` **发起**它。于是

* 执行器是**后端自己** spawn 的（替身执行器 ``PDT_EXECUTOR_CMD``），pid
  是真的，台账里那条「这个 pid 属于这条计划」的记录也是后端自己写的；
* ``plan_execution`` 行是**后端自己**写的——测试从不手写这一行：手写的
  行会带着一个后端不认识的 pid，活不过启动时的对账（那行会被停成
  ``stopped``，计划也就成了终态，从侧边栏里消失）；
* 相位是**产品自己的 CAS** 从 ``queued`` 推到 ``executing`` 的。

于是这条计划正是本文件要讲的那种计划：**在执行、验证从未开始**。

复用说明
--------
``backend/tests/e2e/`` 没有 ``__init__.py``，跨模块 import 要靠 pytest 的
rootdir gymnastics，太脆 —— 所以两个兄弟文件都按**文件路径** importlib
进来，一个字都不改：

* ``test_execution_status_restart_e2e.py``：``_free_port`` / ``_boot`` /
  ``_wait_ready`` / ``_teardown`` / ``_http_json`` / ``_start_run``（真后端
  进程的那一套；它的 ``VENV_PYTHON`` 在没有 ``backend/.venv`` 的 worktree
  里会回退到 ``sys.executable``，正好是跑 pytest 的那个解释器）；
* ``test_fastfail_no_ghost_state_ui.py``：``_playwright_interpreter`` /
  ``_seed_plan_dir`` / ``_seed_routing_row``。

浏览器驱动
----------
Playwright 可能不在跑 pytest 的那个解释器里（``_playwright_interpreter``
会去问 ``PATH`` 上的 ``playwright`` 命令的 shebang），所以浏览器那一段是
**一个子进程**：脚本写到临时目录，用那个解释器跑，报告以一行 JSON 回到
stdout。浏览器**起不来**是**失败**，不是 skip —— 只有「压根没有可用的
Playwright 解释器」才 skip（那种环境下 API 层的验收
``backend/tests/unit/test_plan_status_verification_not_started.py`` 仍在）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent

#: The plan this file drives.  A leaf name, nothing exotic: the endpoints
#: validate it (``server._validated_plan_id``) before they resolve a path.
PLAN_ID = "plan-never-verified"

#: Where the screenshot lands: ``<repo>/tmp/plan-status-never-verified.png``.
SCREENSHOT_PATH = PROJECT_ROOT / "tmp" / "plan-status-never-verified.png"

#: The phase the routing row is seeded in.  ``queued`` is one of the
#: phases a run may be started from (``server._EXECUTION_START_SOURCE_PHASES``);
#: the start route's own CAS is what moves the plan to ``executing``.
SEED_PHASE = "queued"

#: The phase the plan is in while its run is under way -- and the phase
#: the snapshot has to report.
RUNNING_PHASE = "executing"

#: The label the frontend shows for a plan whose verification has not
#: started.  It comes from the table in ``frontend/app.js``
#: (``setVerificationBadge``): ``pending`` and ``not_started`` both map to
#: 未开始.
NOT_STARTED_LABEL = "未开始"

#: The CSS class the same function puts on the badge for that label.
NOT_STARTED_CLASS = "pending"

#: The home this test run was *launched* with.  ``install_hermetic_env()``
#: redirects ``$HOME`` to a scratch directory, and Playwright resolves the
#: browser binaries it launches under the home it is handed -- so the
#: driver subprocess is given the real home.
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"

#: How long the browser waits for the plan card, for the verification
#: panel to open, and for the badge to say 未开始.  No fixed sleeps: a
#: wait that times out is a failure with a reason.
BROWSER_TIMEOUT_MS = 20000

#: The whole browser pass, start to finish.
DRIVER_TIMEOUT_SECONDS = 300

#: How long the plans listing has to answer after the backend is up.
#: The listing is what the sidebar reads, so a backend that cannot answer
#: it cannot show the plan card either.
PLANS_READY_TIMEOUT_SECONDS = 60.0

#: How long the snapshot has to report the plan as executing after the
#: run was started.
RUNNING_PHASE_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Read-only reuse of the sibling E2E files
# ---------------------------------------------------------------------------


def _load_sibling(module_name: str, filename: str):
    """Load a sibling E2E module **by file path**, without modifying it.

    ``backend/tests/e2e/`` has no ``__init__.py``, so a plain cross-module
    import would depend on pytest's rootdir-based ``sys.path`` juggling
    (and on collection order).  Loading by file path avoids that without
    copying a single line of the borrowed code.
    """
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the borrowed helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The real backend process: the port, the boot, the readiness probe, the
#: teardown, the HTTP client, and the start of a run.
_RESTART = _load_sibling(
    "pdt_execution_status_restart_helpers",
    "test_execution_status_restart_e2e.py",
)
#: The browser driver helpers and the seed helpers.
_UI = _load_sibling(
    "pdt_fastfail_ui_helpers",
    "test_fastfail_no_ghost_state_ui.py",
)

_free_port = _RESTART._free_port
_boot = _RESTART._boot
_wait_ready = _RESTART._wait_ready
_teardown = _RESTART._teardown
_server_output = _RESTART._server_output
_http_json = _RESTART._http_json
_start_run = _RESTART._start_run
_playwright_interpreter = _UI._playwright_interpreter
_seed_plan_dir = _UI._seed_plan_dir
_seed_routing_row = _UI._seed_routing_row


def _driver_env() -> Dict[str, str]:
    """The environment the browser driver runs under.

    ``$HOME`` is pointed back at the home the run was launched with (see
    :data:`REAL_HOME_ENV`) so that Playwright can find the browser
    binaries it installed there.
    """
    env = dict(os.environ)
    real_home = os.environ.get(REAL_HOME_ENV)
    if real_home:
        env["HOME"] = real_home
    return env


def _wait_plans_endpoint(base_url: str, proc: subprocess.Popen) -> None:
    """Wait until ``GET /api/plans`` answers.

    The readiness probe the backend answers is ``/api/health``; the
    *listing* is what the sidebar reads, and the sidebar is where the
    plan card comes from.  So the listing is probed too — a backend that
    is up but cannot list its plans would fail the browser pass with a
    far less obvious message.

    A backend that dies on the way up says so in its own output, so that
    output is attached to the failure.
    """
    deadline = time.monotonic() + PLANS_READY_TIMEOUT_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"the backend exited with {proc.returncode} before it could "
                f"list its plans:\n{_server_output(proc)}"
            )
        try:
            status, body = _http_json("GET", f"{base_url}/api/plans")
            if status == 200:
                return
            last = (status, body)
        except OSError as exc:
            last = exc
        time.sleep(0.2)
    raise AssertionError(
        f"{base_url}/api/plans did not answer within "
        f"{PLANS_READY_TIMEOUT_SECONDS}s; last={last!r}\n"
        f"{_server_output(proc)}"
    )


def _wait_running_phase(
    base_url: str, plan_id: str, proc: subprocess.Popen
) -> None:
    """Wait until the snapshot reports the plan as ``executing``.

    The start route's CAS writes the phase synchronously, so this is
    normally true on the first read; the loop is there so that a race
    between the start and the read is a short wait and not a flake.
    """
    deadline = time.monotonic() + RUNNING_PHASE_TIMEOUT_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        try:
            status, body = _http_json(
                "GET", f"{base_url}/api/plan/{plan_id}/status"
            )
        except OSError as exc:
            last = exc
            time.sleep(0.2)
            continue
        if status == 200 and isinstance(body, dict):
            last = body.get("current_phase")
            if last == RUNNING_PHASE:
                return
        time.sleep(0.2)
    raise AssertionError(
        f"the plan never read as {RUNNING_PHASE!r} within "
        f"{RUNNING_PHASE_TIMEOUT_SECONDS}s; last={last!r}\n"
        f"{_server_output(proc)}"
    )


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

_BROWSER_DRIVER = r'''
"""Drive the served page the way an operator does, and report what it shows.

The page is driven through its own events: the plan card in the sidebar
is *clicked*, so the frontend's own ``resumePlan`` runs, and with it
``showTasksView`` → ``resumeVerificationPanel``.  Nothing here reaches
into the application's internals to make the panel show up -- the panel
shows up because the page decided to show it.

The report is one JSON object on stdout: the badge, the panel, the answer
of the very request the frontend's ``api()`` makes, and every page error
the run produced.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot launch
    anything.  The binaries that *are* installed are found here instead
    -- nothing is downloaded, and nothing is hard-coded: the cache lives
    under the home the run was launched with.
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


url, plan_id, shot_path = sys.argv[1], sys.argv[2], sys.argv[3]
timeout_ms = 20000
not_started_label = "未开始"

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

    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=timeout_ms)

    # The card is the operator's way in.  Waiting for it is a wait for
    # the sidebar's own listing to arrive, not for a fixed delay.
    card = '.sidebar-plan-item[data-plan="%s"]' % plan_id
    page.wait_for_selector(card, timeout=timeout_ms)
    report["card_visible"] = page.is_visible(card)

    # The click is the operator's: the page's own handler decides what to
    # do with it.
    page.click(card)

    # The panel starts out ``hidden`` in the served HTML -- only the page
    # can open it, so its being open is the page's own decision.
    page.wait_for_selector(
        "#verification-panel:not(.hidden)", state="attached", timeout=timeout_ms
    )
    page.wait_for_function(
        """(label) => {
            const el = document.querySelector('#verification-status-badge');
            return !!el && el.textContent.trim() === label;
        }""",
        arg=not_started_label,
        timeout=timeout_ms,
    )

    badge = page.query_selector("#verification-status-badge")
    report["badge_text"] = (badge.text_content() or "").strip()
    report["badge_class"] = badge.get_attribute("class") or ""
    panel = page.query_selector("#verification-panel")
    report["panel_class"] = panel.get_attribute("class") or ""
    report["panel_visible"] = page.is_visible("#verification-panel")
    report["round_text"] = page.text_content("#verification-round")

    # The very request the frontend's api() makes -- same path, same
    # guard header, same browser context.
    report["fetch"] = page.evaluate(
        """async (id) => {
            const res = await fetch(`/api/plan/${id}/status`, {
                headers: { "X-PDT-Request": "1" },
            });
            return { status: res.status, body: await res.json() };
        }""",
        plan_id,
    )

    shot = Path(shot_path)
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot), full_page=True)
    report["screenshot"] = str(shot)

    browser.close()

print(json.dumps(report))
'''


# ---------------------------------------------------------------------------
# The fixture: one real backend, one real browser, one report
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser_session(tmp_path_factory) -> Dict[str, Any]:
    """Boot a real backend, drive the real page, and report what happened.

    The fixture does the whole pass **once**: boot, start the plan's run,
    click the plan card, read the badge, fetch the snapshot in the
    browser's own context, take the screenshot, and read the backend's
    log.  The three tests below only assert -- so the backend is booted
    once, the browser is launched once, and the assertions all read the
    same report.
    """
    work = tmp_path_factory.mktemp("plan-status-never-verified-e2e")

    # The plan: a directory on disk (so the routes can resolve it) and a
    # routing row saying where it is in the workflow.  The row starts at
    # ``queued`` -- the phase a run may be started from -- because
    # ``executing`` is not a value a test may simply write down: the
    # phase rule rebuts a runtime phase that has nothing behind it.
    _seed_plan_dir(work, PLAN_ID)
    _seed_routing_row(work / "state.db", PLAN_ID, phase=SEED_PHASE)
    target = work / "target"
    target.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    # The DEBUG line is the positive control of the log assertion, so the
    # backend has to log at DEBUG level.  The child inherits the
    # environment of the test process, so this has to be set before the
    # boot.
    env_patch = pytest.MonkeyPatch()
    env_patch.setenv("PDT_LOG_LEVEL", "DEBUG")

    proc = _boot(work, port)
    try:
        _wait_ready(base_url, proc)
        _wait_plans_endpoint(base_url, proc)

        # The run is started through the API, so the **server** owns it:
        # it spawns the executor, writes the ``plan_execution`` row and
        # writes the ledger record that says which pid belongs to this
        # plan.  The run is what makes the plan read as ``executing`` --
        # and what lets the execution section answer instead of logging a
        # failure of its own.
        _start_run(base_url, PLAN_ID, target)
        _wait_running_phase(base_url, PLAN_ID, proc)

        interpreter = _playwright_interpreter()
        if interpreter is None:
            pytest.skip("playwright unavailable")

        script = work / "drive_page.py"
        script.write_text(_BROWSER_DRIVER, encoding="utf-8")

        result = subprocess.run(
            [
                interpreter,
                str(script),
                f"{base_url}/",
                PLAN_ID,
                str(SCREENSHOT_PATH),
            ],
            capture_output=True,
            text=True,
            timeout=DRIVER_TIMEOUT_SECONDS,
            check=False,
            env=_driver_env(),
        )
        if result.returncode != 0:
            # A browser that cannot be launched is a FAILURE, not a skip:
            # the whole point of this file is the browser.
            raise AssertionError(
                f"the browser driver failed (exit {result.returncode}); "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )

        payload: Dict[str, Any] = json.loads(
            result.stdout.strip().splitlines()[-1]
        )

        # The log is read in FULL: the DEBUG line sits in a file full of
        # DEBUG noise, and the 2000-character tail ``_server_output``
        # hands out would push it out of the window.
        log_path = Path(getattr(proc, "_pdt_log_path"))
        payload["log_text"] = log_path.read_text(
            encoding="utf-8", errors="replace"
        )
        payload["base_url"] = base_url
        yield payload
    finally:
        env_patch.undo()
        _teardown(proc)


# ---------------------------------------------------------------------------
# The three assertions
# ---------------------------------------------------------------------------


def test_plan_page_shows_verification_not_started(
    browser_session: Dict[str, Any],
):
    """The plan page says the verification has not started -- and says it.

    Two things are on the screen: the badge (「未开始」) and the panel
    itself.  The panel is the half that matters: in the served HTML it
    carries the ``hidden`` class, and only the page's own
    ``resumeVerificationPanel`` takes it off.  So a visible panel is
    proof that the page ran the frontend's code, not that the markup
    happened to say 未开始.
    """
    assert browser_session.get("card_visible") is True, (
        f"the plan card never appeared in the sidebar: {browser_session!r}"
    )
    assert browser_session.get("badge_text") == NOT_STARTED_LABEL, (
        f"the verification badge does not say {NOT_STARTED_LABEL!r}: "
        f"{browser_session!r}"
    )
    assert NOT_STARTED_CLASS in (browser_session.get("badge_class") or ""), (
        f"the verification badge is not marked {NOT_STARTED_CLASS!r}: "
        f"{browser_session!r}"
    )
    assert "hidden" not in (browser_session.get("panel_class") or ""), (
        f"the verification panel is still hidden: {browser_session!r}"
    )
    assert browser_session.get("panel_visible") is True, (
        f"the verification panel is not visible on the plan page: "
        f"{browser_session!r}"
    )
    assert not browser_session.get("page_errors"), (
        f"the page raised errors while the plan page was opened: "
        f"{browser_session!r}"
    )

    shot = Path(browser_session.get("screenshot") or "")
    assert shot.is_file(), f"the screenshot was not written: {shot}"
    assert shot.stat().st_size > 0, f"the screenshot is empty: {shot}"


def test_plan_status_endpoint_is_clean_in_browser_context(
    browser_session: Dict[str, Any],
):
    """The snapshot answers 200, and its verification section is ``None``.

    The request is the one the frontend's ``api()`` makes -- same path,
    same guard header, same browser context -- so the answer is the one
    the page would have got.

    ``verification is None`` is the contract of the fix: "there is
    nothing to show yet" is an answer, and it is answered *inside* a
    200.  The phase is still ``executing`` and the summary is still
    there: only the verification section was affected.
    """
    answer = browser_session.get("fetch") or {}
    assert answer.get("status") == 200, (
        f"the snapshot did not answer 200: {browser_session!r}"
    )

    body = answer.get("body") or {}
    assert body.get("verification") is None, (
        f"the verification section is not None: {browser_session!r}"
    )
    assert body.get("current_phase") == RUNNING_PHASE, (
        f"the phase is not {RUNNING_PHASE!r}: {browser_session!r}"
    )
    assert body.get("summary") is not None, (
        f"the summary section is missing: {browser_session!r}"
    )


def test_server_log_has_no_plan_status_error(
    browser_session: Dict[str, Any],
):
    """The backend's own log: no error about this plan's status sections.

    The 404 that means "verification not started" is a normal answer.
    While it was handled as a fault, every running plan produced a full
    traceback at ERROR level -- measured at 2393 of them in two hours.

    So the log must not carry the failure line, and must not carry an
    ERROR line about **this plan** in the ``[plan_status]`` section at
    all: the plan's run is real, so the execution section answers too --
    nothing about this plan is broken.

    The positive control is in the same file: the DEBUG line naming the
    plan is what the fix says instead of the traceback.  Without it the
    absence of the ERROR line would prove nothing -- the section might
    simply never have been read.
    """
    log_text = browser_session.get("log_text") or ""
    lines = log_text.splitlines()

    forbidden = "[plan_status] verification section failed plan=" + PLAN_ID
    assert forbidden not in log_text, (
        f"the backend still logs the verification section as failed: "
        f"{forbidden!r}\n{log_text}"
    )

    plan_status_errors = [
        line for line in lines
        if " ERROR " in line
        and "[plan_status]" in line
        and PLAN_ID in line
    ]
    assert plan_status_errors == [], (
        f"the backend logged ERROR lines about this plan's status "
        f"sections: {plan_status_errors!r}"
    )

    expected = "[plan_status] verification not started plan=" + PLAN_ID
    assert expected in log_text, (
        f"the positive control is missing: the backend never said "
        f"{expected!r}, so the silence above proves nothing\n{log_text}"
    )

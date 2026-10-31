"""重启之后，计划页上的执行日志面板仍然有内容 —— 端到端 UI 验收。

前面四个任务修的是同一件事的四个断面
------------------------------------

* 任务 1：``backend/execution_log_tail.py`` —— 从 ``execution.log`` 的尾部
  读出若干行，并把每一行归一化成 ``[<带时区的 ISO 戳>] <正文>``。
* 任务 2：``GET /api/execution/{id}/status`` —— 进程内存里没有日志时，从
  磁盘上那份日志回填。
* 任务 3：``started_at`` —— 写的时候带时区，读的时候也带时区。
* 任务 4：真进程 + 真重启 —— 证明 ``logs`` 在重启之后仍然非空。

这四个任务都停在 **API** 上。API 答对了，不等于**用户看得见** ——
用户看的是计划页右下角那块 ``<pre id="execution-logs">``
（``frontend/index.html``），它的内容由 ``frontend/app.js`` 的
``appendExecLogs`` 写进去，而 ``appendExecLogs`` 写的正是 ``data.logs``：
``logs`` 空，面板就是一块空白。任务 4 钉住了"接口答得对"，这一份把最后
一段路走完：**真浏览器，真点一次计划卡片**，然后看那块面板里有没有字。

为什么非要浏览器
----------------

前端这一段没法用 HTTP 复现：``resumeExecutionPanel()`` 在
``status === "not_started"`` 时会提前 return（面板根本不打开），而
``appendExecLogs`` 是在页面自己的事件里被调用的。用 HTTP 只能证明"接口
答了什么"，证明不了"面板上有没有字"。

所以这一份用 Playwright 驱动真 Chromium：打开真页面，点真卡片，等真
DOM。页面被驱动的方式是它自己的事件（``page.click``），所以走的是前端
自己的代码路径 —— 面板出现是因为**页面决定**让它出现，不是因为测试把
内容塞了进去。

手工复跑（不跑 pytest 的时候）
--------------------------------

用 ``mcp__playwright__`` 按同样的顺序走一遍，每一步的结果都在页面上
看得见：

1. ``mcp__playwright__browser_navigate`` 打开 ``http://127.0.0.1:<port>/``；
2. ``mcp__playwright__browser_wait_for`` 等
   ``.sidebar-plan-item[data-plan="<plan_id>"]`` 出现；
3. ``mcp__playwright__browser_click`` 点那张卡片；
4. ``mcp__playwright__browser_wait_for`` 等 ``#execution-logs`` 可见，
   再等它的 ``textContent`` 不为空 —— 面板上要有字，不能只是一块空白；
5. ``mcp__playwright__browser_take_screenshot`` 存成
   ``execution-logs-fresh.png``。

复用说明
--------

任务 4 的 ``_free_port`` / ``_seed`` / ``_boot`` / ``_wait_ready`` /
``_teardown`` / ``_http_json`` 按**文件路径** importlib 进来（``backend/
tests/e2e/`` 没有 ``__init__.py``，跨模块 import 要靠 pytest 的 rootdir
gymnastics，太脆）；``_playwright_interpreter()`` 同样按文件路径从
``test_fastfail_no_ghost_state_ui.py`` 取。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent

#: Where the screenshots go: ``<repo>/backend/tests/e2e/artifacts/<plan_id>/``.
ARTIFACTS_ROOT = Path(__file__).resolve().parent / "artifacts"

#: The plan this file seeds.  A leaf name, nothing exotic: the endpoint
#: validates it (``server._validated_plan_id``) before it resolves a path.
PLAN_ID = "p1"

#: The message the seeded ``execution.log`` carries.  It is the marker of
#: "the panel shows the log from disk": the text can only be there if the
#: lines really came back from the file.
SEED_MESSAGE = "e2e seed"

#: The home this test run was *launched* with.  ``install_hermetic_env()``
#: redirects ``$HOME`` to a scratch directory, and Playwright resolves the
#: browser binaries it launches under the home it is handed -- so the driver
#: subprocess is given the real home.
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"

#: How long the browser waits for the plan card, for the log panel to
#: become visible, and for the panel to carry text.  No fixed sleeps: a
#: wait that times out is a failure with a reason.
BROWSER_TIMEOUT_MS = 20000

#: The whole browser pass, start to finish.
DRIVER_TIMEOUT_SECONDS = 300


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


#: Task 4's helpers: the real backend process, the seed, the port.
_RESTART = _load_sibling(
    "pdt_execution_status_restart_helpers",
    "test_execution_status_restart_e2e.py",
)
#: The browser driver helpers: the interpreter that can import playwright.
_UI = _load_sibling(
    "pdt_fastfail_ui_helpers",
    "test_fastfail_no_ghost_state_ui.py",
)

_free_port = _RESTART._free_port
_seed = _RESTART._seed
_boot = _RESTART._boot
_wait_ready = _RESTART._wait_ready
_teardown = _RESTART._teardown
_crash = _RESTART._crash
_start_run = _RESTART._start_run
_http_json = _RESTART._http_json
_playwright_interpreter = _UI._playwright_interpreter


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


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

_BROWSER_DRIVER = r'''
"""Drive the served page the way an operator does, and report the panel.

The page is driven through its own events: the plan card in the sidebar
is *clicked*, so the frontend's own ``resumePlan`` runs, and with it
``showTasksView`` → ``resumeExecutionPanel`` → ``appendExecLogs``.
Nothing here reaches into the application's internals to make the panel
show up -- the panel shows up because the page decided to show it.

The report is one JSON object on stdout: the text the log panel carries,
whether it is visible, and every page error the run produced.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot
    launch anything.  The binaries that *are* installed are found here
    instead -- nothing is downloaded, and nothing is hard-coded: the
    cache lives under the home the run was launched with.
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


url, plan_id, artifacts_dir = sys.argv[1:4]
timeout_ms = 20000

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
    report["card_text"] = page.inner_text(card)

    # The click is the operator's: the page's own handler decides what
    # to do with it.
    page.click(card)

    # The log panel is only opened by the page itself, and only when the
    # status answer says the run is not "not started".
    page.wait_for_selector("#execution-logs", state="visible", timeout=timeout_ms)
    report["logs_visible"] = page.is_visible("#execution-logs")

    # And the panel has to carry text -- visible but empty is the very
    # failure this file is about.
    page.wait_for_function(
        """() => {
            const el = document.querySelector('#execution-logs');
            return !!el && el.textContent.length > 0;
        }""",
        timeout=timeout_ms,
    )
    report["logs_text"] = page.text_content("#execution-logs")
    report["logs_length"] = len(report["logs_text"] or "")

    shot = Path(artifacts_dir) / "execution-logs-fresh.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shot), full_page=True)
    report["screenshot"] = str(shot)

    browser.close()

print(json.dumps(report))
'''


# ---------------------------------------------------------------------------
# The fixture: a backend that has been restarted
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def restarted_backend(tmp_path_factory) -> str:
    """The base URL of a backend that has been restarted.

    The sequence is the point: a first boot (which recovers the run
    into its memory), a kill (which takes that memory with it), and a
    second boot on the same plans tree and the same state database.
    The second process knows only what is on disk -- which is exactly
    the state the plan page has to render.
    """
    work = tmp_path_factory.mktemp("execution-logs-ui-e2e")
    _seed(work, PLAN_ID)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    # The first life: a run that is really running, started through the
    # API so the **server** owns the executor (and the process ledger
    # backs its pid).  Then the process is killed outright, so what it
    # held in memory is gone.
    first = _boot(work, port)
    try:
        _wait_ready(base_url, first)
        _start_run(base_url, PLAN_ID, work / "target")
    finally:
        _crash(first)

    # The second life: a new process on the same plans tree.  Its run is
    # started again, so the panel it renders is about a run that is going
    # — and the log lines it shows are not in its memory, they are in the
    # file on disk.
    second = _boot(work, port)
    try:
        _wait_ready(base_url, second)
        _start_run(base_url, PLAN_ID, work / "target")
        yield base_url
    finally:
        _teardown(second)


# ---------------------------------------------------------------------------
# The assertion
# ---------------------------------------------------------------------------


def test_plan_page_shows_logs_after_restart(restarted_backend: str, tmp_path):
    """The plan page shows the run's log lines after a restart.

    The panel is what the operator reads.  A restart took the
    in-process log buffer with it, so the lines the panel shows have to
    come from the file on disk -- and the marker in them ("e2e seed") is
    only there if they did.
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; the "
            "API-level acceptance (test_execution_status_restart_e2e.py) "
            "carries the contract"
        )

    artifacts_dir = ARTIFACTS_ROOT / PLAN_ID
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    script = tmp_path / "drive_page.py"
    script.write_text(_BROWSER_DRIVER, encoding="utf-8")

    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{restarted_backend}/",
            PLAN_ID,
            str(artifacts_dir),
        ],
        capture_output=True,
        text=True,
        timeout=DRIVER_TIMEOUT_SECONDS,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        if "Executable doesn't exist" in combined or (
            "playwright install" in combined
        ):
            pytest.skip(
                "the Playwright driver is installed but its browser "
                "binaries are not; the API-level acceptance carries the "
                "contract"
            )
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    payload: Dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])

    assert payload.get("card_visible") is True, (
        f"the plan card never appeared in the sidebar: {payload!r}"
    )
    assert payload.get("logs_visible") is True, (
        f"the log panel is not visible on the plan page: {payload!r}"
    )
    assert payload.get("logs_length", 0) > 0, (
        f"the log panel is visible but carries no text: {payload!r}"
    )
    assert SEED_MESSAGE in (payload.get("logs_text") or ""), (
        f"the log panel does not carry the seeded log line "
        f"{SEED_MESSAGE!r}: {payload!r}"
    )
    assert not payload.get("page_errors"), (
        f"the page raised errors while the plan page was opened: "
        f"{payload!r}"
    )

    shot = artifacts_dir / "execution-logs-fresh.png"
    assert shot.is_file(), f"the screenshot was not written: {shot}"
    assert shot.stat().st_size > 0, f"the screenshot is empty: {shot}"
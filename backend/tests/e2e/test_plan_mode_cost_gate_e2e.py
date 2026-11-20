"""计划级 mode 的全链路端到端验收。

上游每一段都有自己的单元测试，但那些测试各自只看自己那一段：mode 落库
（任务 3）、成本门分流（任务 4）、子进程注入（任务 5）、scoped tool 注入
（任务 6）、wrapper 强制 test 模式（任务 7）、报告的两份产物（任务 8/9）。
它们全都跑在**同一个进程**里，用的是**直接调用**的方式 —— 传一个 dict
进去，拿到一个 dict 出来。

本文件问的是另一个问题：**这些段在一条真的链路上还接不接得上**。于是这里
的东西全是真的：

* server 不是一个 ``TestClient``，是一个真的 ``backend.server`` 进程 ——
  而且是由 ``scripts/pdt_test_runner.sh`` 拉起的。wrapper 是「test 模式的
  进程只能这样启动」的唯一入口，所以夹具必须按生产的方式用它：用例自己
  启动的东西��自己就是被 wrapper 约束过的东西。
* mode 不是一个塞进 dict 的字符串，是 ``POST /api/plan/import`` 的一个
  字段，落进计划自己的状态行（``plan_routing`` 的 flags），并且从
  ``GET /api/plan/{id}/state`` 读得回来。
* 成本门不是一个被 monkeypatch 的函数，是**另一个进程**里的
  ``_cost_gate_verdict``，读的是磁盘上的 mode —— 它看不见本进程的内存。
* 浏览器不是一个 mock，是一个真的 Chromium：真的点
  ``#btn-start-execution`` 把执行面板展开，真的往 ``#project-dir-input``
  里填字，真的截一张图。
* 报告不是一份内存里的 dict，是 ``update_report()`` 落在磁盘上的两份产物
  （``verification_report.json`` 与 ``verification-report.md``）。

三段证据
--------

**一、落库（HTTP）。** 两个计划从同一个入口进来，一个带 ``mode: "test"``，
一个不带。两次响应都回显自己的 mode；状态接口也读得回来。这条链的起点是
用户输入，不是测试代码。

**二、注入（另一个进程）。** 探针是一个**独立的进程**，由 wrapper 拉起。它
看见的东西和自己的进程无关：``PDT_PLAN_MODE`` 来自 wrapper 的环境，
``read_plan_mode()`` 来自磁盘上的状态行，``_scoped_plan_mode()`` 来自同一
个状态行。三者必须一致 —— 不一致就说明「谁在跑」这件事没有落到进程边界
上，那正是要钉住的东西。

**三、产物（磁盘）。** 报告的两份产物必须说同一件事：JSON 说「test 模式
下被成本门跳过的是 VP-1」，Markdown 说同一件事。计数与小节对不上，就是
两份真相。

边界
----

* **不点 ``#btn-confirm-execute``。** 它绑的是 ``startExecution()``，点了
  就真的会拉起执行器 —— 那会让这条用例变成「我真的跑了一个计划」，而且
  会把断言 5 的语义搅乱（执行器会把 mode 注入成 plan 的 mode）。这里只
  点 ``#btn-start-execution``（``showExecutionPanel()``：展开面板、聚焦输入
  框，没有别的副作用），用来把 ``#project-dir-input`` 从 ``hidden`` 里
  拿出来。
* **wrapper 的调用方不能是 production。** ``pdt_test_runner.sh`` 拒绝为
  「调用方说自己是 production」的进程启动 test 模式的工作。E2E 自己可能
  继承 ``PDT_PLAN_MODE=production``，所以两处 wrapper 调用（起 server、
  跑探针）都显式把这个变量从**调用方**的环境里去掉 —— 这正是
  ``env -u PDT_PLAN_MODE`` 做的事。去掉只影响调用方：探针看到的 ``test``
  是 wrapper 自己注入的。
* **探针的状态库必须和 server 的逐字一致。** ``read_plan_mode()`` 读的是
  ``PDT_STATE_DB_PATH`` 指向的那个库。不一致就读到别的库，两个计划都解析
  成 ``production``，断言 5 必假 —— 而且是**假得很有说服力**（production
  计划的裁决恰好是「不拦」），所以这条一致性是被断言守着的，不是被祈祷
  守着的。
* **playwright 不可用就直接失败，不许 skip。** 本文件的产出就是 UI 链路
  的证据；一个 skip 掉的证据等于没有证据，而「没有证据」和「链路是好的」
  在报告里长得一模一样。
* **单条用例必须在全局 ``timeout = 300`` 秒内跑完。** 所以整个流程
  （起 server ≤60s、浏览器段 ≤120s、探针 ≤60s）是一次性的，不是每个
  断言重做一遍。

复用
----

``_free_port`` / ``_http_json`` / ``_wait_ready`` / ``_teardown`` 是按
**文件路径**从 ``backend/tests/e2e/test_execution_status_restart_e2e.py``
抄过来的：``backend/tests/e2e/`` 是一个包，跨模块 import 要靠 pytest 的
rootdir gymnastics，太脆 —— 这个目录里的既有惯例就是抄，不是 import。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent

#: The backend subprocess has to run on the interpreter this suite was
#: installed into.  A checkout with no ``backend/.venv`` (fresh worktree,
#: container) falls back to the interpreter already running pytest — never
#: to a system python, which would not have the dependencies installed.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: The wrapper: the only sanctioned way to start a PDT process in test
#: mode.  It is resolved from this file's own location, so the path is
#: the one in *this* checkout — not one hard-coded on some machine.
TEST_RUNNER = PROJECT_ROOT / "scripts" / "pdt_test_runner.sh"

#: Every ``/api/*`` request must carry this header (``backend/request_guard.py``).
REQUEST_HEADERS = {
    "X-PDT-Request": "1",
    "Content-Type": "application/json",
}

#: How long the backend may take to answer its readiness probe.  A boot
#: that has not answered within this budget is a failure, and the failure
#: carries the tail of the server's own output.
READY_TIMEOUT_SECONDS = 60.0

#: How long an import may take.  The import asks a model for a task list;
#: here the model is the scripted double, so this is a ceiling, not a wait.
IMPORT_TIMEOUT_SECONDS = 120.0

#: The browser budget.  The whole browser segment has to fit in this.
BROWSER_TIMEOUT_MS = 120_000

#: How long the probe may take.
PROBE_TIMEOUT_SECONDS = 60.0

#: The line the server prints once it knows where it listens.  The server
#: walks forward from ``PDT_PORT`` when the preferred port is taken, so the
#: port it actually bound is not necessarily the one that was asked for —
#: and the URL has to name the one it actually bound.
_BOUND_PORT_RE = re.compile(r"Starting uvicorn on (?P<host>[^:\s]+):(?P<port>\d+)")

#: The import material.  Two different titles, because the plan id is
#: derived from the title: the same title on the same day derives the same
#: id, and the second import would land next to the first one.
IMPORT_TITLE_TEST = "mode cost gate e2e test plan"
IMPORT_TITLE_PRODUCTION = "mode cost gate e2e production plan"
IMPORT_CONTENT = "把报表页的折线图换成按天分组的柱状图"

#: The task list the scripted model answers the import with.  It names
#: files that exist under the checkout, because the import validates every
#: task against the project's own directory before it parks the plan.
_TASKS_PAYLOAD = {
    "requirement": "add a daily grouped bar chart to the reporting view",
    "tasks": [
        {
            "id": "T1",
            "title": "add the chart renderer",
            "description": "## 背景\nthe reporting view has no chart",
            "files_to_modify": ["backend/chart.py"],
            "test_command": "pytest -q backend/tests/test_chart.py",
        },
        {
            "id": "T2",
            "title": "wire the renderer into the view",
            "description": "## 目标\nmount the renderer",
            "files_to_modify": ["backend/reporting_view.py"],
            "test_command": "pytest -q",
            "depends_on": ["T1"],
        },
    ],
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind an ephemeral loopback port and hand the number back.

    The kernel picks the port, so the number is free at the moment it is
    returned.  It is the same trick the sibling E2E files use.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_json(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Tuple[int, Any]:
    """One HTTP request against the backend; ``(status, decoded body)``.

    The guard header goes with every call: ``/api/*`` is guarded, and a
    request without the header is refused with 403 before it reaches a
    route.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in REQUEST_HEADERS.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, raw.decode("utf-8", "replace")


def _server_output(proc: subprocess.Popen, limit: int = 4000) -> str:
    """The tail of the backend's own output (stdout **and** stderr).

    A boot that died says why in there, and a boot that hung says where it
    stopped.  Both streams are captured into one file, so this is the
    whole story the server told.
    """
    path = getattr(proc, "_pdt_log_path", None)
    if path is None:
        return "(no server output was captured)"
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - the file is ours
        return f"(could not read {path}: {exc})"
    return text[-limit:]


def _bound_port(proc: subprocess.Popen) -> Optional[int]:
    """The port the server says it is listening on, if it has said yet.

    The server walks forward from ``PDT_PORT`` when the preferred port is
    taken, so the port that was asked for is not necessarily the port
    that answers.  The server prints the one it bound; that print is the
    authority, and the URL is built from it.
    """
    path = getattr(proc, "_pdt_log_path", None)
    if path is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - the file is ours
        return None
    match = None
    for match in _BOUND_PORT_RE.finditer(text):
        pass
    if match is None:
        return None
    return int(match.group("port"))


def _wait_ready(proc: subprocess.Popen) -> str:
    """Wait until the backend answers; return the base URL it answers on.

    The backend is up when the API answers, not when the process exists:
    the process exists for a moment before uvicorn is listening.  A
    backend that dies on the way up says so in its own output, so that
    output is attached to the failure.
    """
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last: Any = None
    port: Optional[int] = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"the backend exited with {proc.returncode} before it was "
                f"ready:\n{_server_output(proc)}"
            )
        if port is None:
            port = _bound_port(proc)
        if port is not None:
            base_url = f"http://127.0.0.1:{port}"
            try:
                status, body = _http_json("GET", f"{base_url}/api/plans")
                if status == 200:
                    return base_url
                last = (status, body)
            except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
                last = exc
        time.sleep(0.2)
    raise AssertionError(
        f"the backend was not ready within {READY_TIMEOUT_SECONDS}s; "
        f"last={last!r}\n{_server_output(proc)}"
    )


def _teardown(proc: Optional[subprocess.Popen]) -> None:
    """Stop the backend and let go of its port.

    ``terminate`` first — uvicorn shuts down gracefully on SIGTERM — and
    ``kill`` only when that did not finish in time.  The port has to be
    free again before the next boot binds it.
    """
    if proc is None:
        return
    handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover - the handle is ours
                pass


# ---------------------------------------------------------------------------
# The scripted model: the import asks for a task list
# ---------------------------------------------------------------------------


def _write_scripted_llm(script_dir: Path) -> None:
    """Write the scripted answers the server will read instead of a model.

    The registry maps a **matcher** (a substring of the prompt) to a
    response file.  The rules the double itself enforces: an exact match
    wins, otherwise the most specific fragment wins, and a prompt nothing
    matches is an error (there is no silent fallback -- that is the point
    of the double).

    * ``import.json`` -- the import path's task-generation prompt names the
      file it is supposed to read.
    * ``资深任务列表审校员 / 第二轮生成器`` -- the mandatory self-review of
      the task list.
    * ``e`` -- the catch-all for any other turn.  It is one character long,
      so every other (longer, more specific) matcher beats it; it exists
      so that a turn nobody scripted is answered with *something* instead
      of raising.
    """
    script_dir.mkdir(parents=True, exist_ok=True)
    registry = {
        "prompts": {
            "tasks_generation": "import.json",
            "tasks_self_review": "资深任务列表审校员 / 第二轮生成器",
            "agent_turn": "e",
        }
    }
    (script_dir / "registry.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (script_dir / "tasks_generation.txt").write_text(
        json.dumps(_TASKS_PAYLOAD, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (script_dir / "tasks_self_review.txt").write_text(
        json.dumps(
            {"findings": [], "fixed_content": _TASKS_PAYLOAD},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (script_dir / "agent_turn.txt").write_text(
        json.dumps({"ok": True}, ensure_ascii=False), encoding="utf-8"
    )


def _make_checkout(project_dir: Path) -> Path:
    """A real directory for the imported plan's tasks to work on.

    The import validates every task against the project's own directory
    (``files_to_modify`` has to sit under it), so the files the task list
    names have to exist.  The CI entry script is the project's own
    business, not the framework's -- it is installed because a real
    project looks like that, not because a gate demands it.
    """
    (project_dir / "backend").mkdir(parents=True, exist_ok=True)
    for name in ("chart.py", "reporting_view.py"):
        (project_dir / "backend" / name).write_text("# placeholder\n", encoding="utf-8")
    scripts_dir = project_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    adapter = scripts_dir / "ci_gate.sh"
    adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    adapter.chmod(0o755)
    return project_dir


# ---------------------------------------------------------------------------
# The backend process
# ---------------------------------------------------------------------------


def _boot(work: Path, port: int, script_dir: Path) -> subprocess.Popen:
    """Start a real ``backend.server`` process against ``work``.

    The launch goes through ``scripts/pdt_test_runner.sh`` -- the wrapper
    is what makes a test-mode launch a test-mode launch, and a fixture
    that bypassed it would be testing a launch path nobody uses.

    The environment variables are the whole contract between the server
    and the probe:

    * ``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH`` -- the plans tree and the
      state database.  The probe is given **the same two strings**,
      because ``read_plan_mode()`` reads the database the variable names.
    * ``PDT_PORT`` -- the port to try first.  The server walks forward if
      it is taken, and prints the one it bound.
    * ``PDT_PROVIDER_MODE=inherit`` -- the boot asks CC Switch for the
      provider chain and **aborts the startup** when it cannot be asked.
      This server exists to answer one question about modes; it must not
      need a provider fleet to come up.
    * ``PDT_LLM_MODE=scripted`` / ``PDT_LLM_SCRIPT_DIR`` -- the model is
      the one thing that is replaced.  A real model call would be neither
      reproducible nor free, and the import needs a task list.
    * ``PDT_ORPHAN_REAPER=0`` -- the boot otherwise walks **every process
      on the machine** to find orphans left behind by a previous life.
      A throwaway server has no orphans to reap: the reaper is off.
    * ``PYTHONPATH`` -- two directories, and both are needed.
      ``backend/`` because the server's own modules import each other by
      their flat names (``from coding_tool import ...``), and the
      repository root because some of them reach the framework through
      the package path (``from backend.framework.prompts import ...``).
    * ``PYTEST_CURRENT_TEST`` -- the product's own answer to "may this
      process touch the machine?".  This child is a test process.

    The child's ``PDT_PLAN_MODE`` is **removed from the caller's
    environment**: the wrapper refuses to run when the caller says it is
    a production caller, and an E2E may inherit such an environment.
    Removing the key is what ``env -u PDT_PLAN_MODE`` does.  It only
    affects the caller -- the wrapper injects ``test`` for the command it
    runs.
    """
    env = dict(os.environ)
    env.update(
        {
            "PDT_PLANS_DIR": str(work / "plans"),
            "PDT_STATE_DB_PATH": str(work / "state.db"),
            "PDT_PORT": str(port),
            "PDT_HOST": "127.0.0.1",
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_ORPHAN_REAPER": "0",
            "PDT_LLM_MODE": "scripted",
            "PDT_LLM_SCRIPT_DIR": str(script_dir),
            "PDT_DISABLE_FALSIFIABILITY_PROBE": "1",
            "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
            "PYTEST_CURRENT_TEST": (
                "backend/tests/e2e/test_plan_mode_cost_gate_e2e.py (setup)"
            ),
            "PYTHONPATH": os.pathsep.join(
                [str(BACKEND_DIR), str(PROJECT_ROOT), env.get("PYTHONPATH", "")]
            ),
        }
    )
    # The wrapper's caller must not claim to be a production caller.
    env.pop("PDT_PLAN_MODE", None)
    # A stale secret descriptor must never be closed by a process that
    # never owned it.
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)
    env.pop("ANTHROPIC_BASE_URL", None)

    log_path = work / f"server-{port}.log"
    handle = log_path.open("wb")
    proc = subprocess.Popen(
        [str(TEST_RUNNER), "--", str(VENV_PYTHON), "server.py"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._pdt_log_path = log_path  # type: ignore[attr-defined]
    proc._pdt_log_handle = handle  # type: ignore[attr-defined]
    return proc


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------


def _browser_executable() -> Optional[str]:
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot launch
    anything.  The binaries that *are* installed are found here instead --
    nothing is downloaded, and nothing is hard-coded.

    The cache lives under ``$HOME``, and a pytest run redirects ``$HOME``
    to a scratch directory -- so the **real** home is read instead (the
    hermetic environment publishes it as ``PDT_TEST_REAL_HOME``).  Looking
    under the redirected home would find nothing and the browser would
    never launch.
    """
    real_home = os.environ.get("PDT_TEST_REAL_HOME")
    home = Path(real_home) if real_home else Path.home()
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


def _wait_for_sidebar_item(page: Any, plan_id: str, timeout_ms: int) -> bool:
    """Wait for a plan to show up in the sidebar.

    The listing is served from a short-lived cache, so a page that loads
    right after a plan appeared shows a list without it.  Reloading is
    what a user does (the sidebar polls), and it is the only way the cache
    gets out of the way.
    """
    selector = f'.sidebar-plan-item[data-plan="{plan_id}"]'
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            page.wait_for_selector(selector, timeout=3000)
            return True
        except Exception:  # noqa: BLE001 - playwright's own timeout error
            page.reload(wait_until="load")
            page.wait_for_timeout(500)
    return False


def _drive_browser(
    base_url: str,
    test_plan_id: str,
    prod_plan_id: str,
    project_dir: Path,
    screenshot_path: Path,
) -> Dict[str, Any]:
    """Walk the page the way an operator does; report what it shows.

    The path is: the served page → the plan in the sidebar → the tasks
    view → the execution panel → the working directory.  ``#btn-confirm-
    execute`` is **never** clicked: it would really start the executor,
    and this file is about the mode, not about a run.
    """
    from playwright.sync_api import sync_playwright

    executable = _browser_executable()
    launch_kwargs: Dict[str, Any] = {"executable_path": executable} if executable else {}

    observations: Dict[str, Any] = {
        "page_errors": [],
        "console_errors": [],
    }

    with sync_playwright() as driver:
        browser = driver.chromium.launch(**launch_kwargs)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on(
                "pageerror",
                lambda exc: observations["page_errors"].append(str(exc)),
            )
            page.on(
                "console",
                lambda message: (
                    observations["console_errors"].append(message.text)
                    if message.type == "error"
                    else None
                ),
            )

            page.goto(f"{base_url}/", wait_until="load", timeout=BROWSER_TIMEOUT_MS)
            page.wait_for_selector("#app", timeout=BROWSER_TIMEOUT_MS)

            observations["test_plan_visible"] = _wait_for_sidebar_item(
                page, test_plan_id, BROWSER_TIMEOUT_MS
            )
            observations["prod_plan_visible"] = _wait_for_sidebar_item(
                page, prod_plan_id, BROWSER_TIMEOUT_MS
            )
            observations["sidebar_plan_ids"] = page.evaluate(
                """() => Array.from(document.querySelectorAll('.sidebar-plan-item'))
                         .map(item => item.dataset.plan)"""
            )

            # The plan opens in its tasks view; from there the execution
            # panel is one click away.
            page.click(f'.sidebar-plan-item[data-plan="{test_plan_id}"]')
            page.wait_for_selector("#btn-start-execution", timeout=BROWSER_TIMEOUT_MS)
            observations["start_button_visible"] = page.is_visible(
                "#btn-start-execution"
            )

            # ``showExecutionPanel()`` -- expands the panel and focuses the
            # input.  Nothing else: the executor is not started.
            page.click("#btn-start-execution")
            page.wait_for_selector(
                "#project-dir-input", timeout=BROWSER_TIMEOUT_MS
            )
            observations["project_dir_visible"] = page.is_visible(
                "#project-dir-input"
            )

            page.fill("#project-dir-input", str(project_dir))
            observations["project_dir_value"] = page.input_value(
                "#project-dir-input"
            )

            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path))
        finally:
            browser.close()

    return observations


# ---------------------------------------------------------------------------
# The probe: another process, reading the mode off the disk
# ---------------------------------------------------------------------------

#: The probe source, verbatim.  It reads the mode from the plan's own
#: state (``read_plan_mode``) rather than being told what it is -- a probe
#: that hard-codes the mode would prove nothing about the mode.
_PROBE_SOURCE = '''import json, os, sys
from plan_state import read_plan_mode
from verification_executor import _cost_gate_verdict
from verification_subagent import _scoped_plan_mode
test_dir, prod_dir = sys.argv[1], sys.argv[2]
vp = {'id': 'VP-1', 'cost': 'llm_quota'}
print(json.dumps({
    'env_mode': os.environ.get('PDT_PLAN_MODE'),
    'test_mode': read_plan_mode(test_dir),
    'scoped_test': _scoped_plan_mode(test_dir),
    'test_verdict': _cost_gate_verdict(vp, plan_mode=read_plan_mode(test_dir)),
    'prod_verdict': _cost_gate_verdict(vp, plan_mode=read_plan_mode(prod_dir)),
}))
'''


def _run_probe(work: Path, test_plan_dir: Path, prod_plan_dir: Path) -> Dict[str, Any]:
    """Run the probe through the wrapper; return what it answered.

    The wrapper is used here for the same reason the server is launched
    through it: it is the only sanctioned way to start a PDT process in
    test mode, and the point of the probe is that the mode reached a
    **process boundary**.

    The two state variables are the ones the server was given, **verbatim**
    -- a probe that read a different database would read a different
    (or no) plan, and every answer it gave would be about something else.
    """
    probe_path = work / "probe.py"
    probe_path.write_text(_PROBE_SOURCE, encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "PDT_STATE_DB_PATH": str(work / "state.db"),
            "PDT_PLANS_DIR": str(work / "plans"),
            "PYTHONPATH": "backend",
        }
    )
    # The wrapper's caller must not claim to be a production caller.
    env.pop("PDT_PLAN_MODE", None)
    # The cost gate's operator override must be **off** here: this probe
    # measures what the cost gate does in test mode, and an inherited
    # ``PDT_VERIFY_ALLOW_LLM_QUOTA=1`` would turn the gate off and make
    # the answer "nothing was deferred" for a reason that has nothing to
    # do with the mode.  The name is read from the product module, so it
    # cannot drift away from the switch the gate actually reads.
    from verification_executor import ALLOW_LLM_QUOTA_ENV

    env.pop(ALLOW_LLM_QUOTA_ENV, None)

    completed = subprocess.run(
        [
            str(TEST_RUNNER),
            "--",
            str(VENV_PYTHON),
            str(probe_path),
            str(test_plan_dir),
            str(prod_plan_dir),
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0, (
        f"the probe exited with {completed.returncode}\n"
        f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
    )

    answer: Optional[Dict[str, Any]] = None
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            answer = json.loads(line)
        except ValueError:
            continue
    assert answer is not None, (
        f"the probe answered no JSON object\nstdout={completed.stdout!r}\n"
        f"stderr={completed.stderr!r}"
    )
    return answer


# ---------------------------------------------------------------------------
# The report: two artefacts on the disk, saying the same thing
# ---------------------------------------------------------------------------


def _write_report(plan_dir: Path) -> Dict[str, Any]:
    """Write a report that records one test-mode cost-gate skip.

    The report is written through the **real** writer
    (``VerificationPersistenceManager.update_report``) -- the same one
    the verification round uses -- because the artefacts under test are
    the ones that writer produces: the JSON carries the derived list and
    count, and the Markdown is the view of it.
    """
    from verification_persistence import VerificationPersistenceManager

    manager = VerificationPersistenceManager(plan_dir)
    manager.update_report(
        {
            "final_verdict": "deferred",
            "verification_results": [
                {
                    "id": "VP-1",
                    "status": "DEFERRED",
                    "cost": "llm_quota",
                    "reasons": ["cost: llm_quota"],
                    "evidence": {"cost": "llm_quota", "plan_mode": "test"},
                }
            ],
        }
    )

    json_path = plan_dir / "verification_report.json"
    md_path = plan_dir / "verification-report.md"
    return {
        "json": json.loads(json_path.read_text(encoding="utf-8")),
        "markdown": md_path.read_text(encoding="utf-8"),
        "json_path": str(json_path),
        "md_path": str(md_path),
    }


# ---------------------------------------------------------------------------
# The fixture: the whole chain, once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mode_chain(tmp_path_factory) -> Dict[str, Any]:
    """Walk the whole chain once and hand the observations to the tests.

    One walk, many assertions: the story has a beginning (two imports),
    a middle (a browser that opens the plan and fills in a directory) and
    an end (a probe in another process and a report on the disk).
    Re-running it per assertion would make the suite slow without making
    it stricter.
    """
    work = tmp_path_factory.mktemp("plan-mode-cost-gate-e2e")
    plans_dir = work / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    project_dir = _make_checkout(work / "proj")
    script_dir = work / "llm-script"
    _write_scripted_llm(script_dir)
    screenshots_dir = work / "screenshots"
    screenshot_path = screenshots_dir / "e2e.png"

    observations: Dict[str, Any] = {"work": str(work)}
    proc = _boot(work, _free_port(), script_dir)
    try:
        base_url = _wait_ready(proc)
        observations["base_url"] = base_url

        # --- the two imports: one that says ``test``, one that says nothing
        test_status, test_body = _http_json(
            "POST",
            f"{base_url}/api/plan/import",
            {
                "project_dir": str(project_dir),
                "content": IMPORT_CONTENT,
                "title": IMPORT_TITLE_TEST,
                "mode": "test",
            },
            timeout=IMPORT_TIMEOUT_SECONDS,
        )
        assert test_status == 200, (
            f"the test-mode import was refused: {test_status} {test_body!r}\n"
            f"{_server_output(proc)}"
        )
        prod_status, prod_body = _http_json(
            "POST",
            f"{base_url}/api/plan/import",
            {
                "project_dir": str(project_dir),
                "content": IMPORT_CONTENT,
                # 同一份材料、同一个项目，这里**故意**要第二个计划来对比
                # test / production 两种用途，所以显式声明允许重复导入。
                "allow_duplicate": True,
                "title": IMPORT_TITLE_PRODUCTION,
            },
            timeout=IMPORT_TIMEOUT_SECONDS,
        )
        assert prod_status == 200, (
            f"the default-mode import was refused: {prod_status} {prod_body!r}\n"
            f"{_server_output(proc)}"
        )

        observations["test_import_status"] = test_status
        observations["test_import_body"] = test_body
        observations["prod_import_status"] = prod_status
        observations["prod_import_body"] = prod_body

        test_plan_id = str(test_body.get("plan_id") or "")
        prod_plan_id = str(prod_body.get("plan_id") or "")
        assert test_plan_id, f"the test-mode import produced no plan id: {test_body!r}"
        assert prod_plan_id, (
            f"the default-mode import produced no plan id: {prod_body!r}"
        )
        assert test_plan_id != prod_plan_id, (
            f"the two imports landed on the same plan: {test_plan_id!r}"
        )
        observations["test_plan_id"] = test_plan_id
        observations["prod_plan_id"] = prod_plan_id

        # --- the mode is readable through the state endpoint
        test_state_status, test_state = _http_json(
            "GET", f"{base_url}/api/plan/{test_plan_id}/state"
        )
        prod_state_status, prod_state = _http_json(
            "GET", f"{base_url}/api/plan/{prod_plan_id}/state"
        )
        observations["test_state_status"] = test_state_status
        observations["test_state"] = test_state
        observations["prod_state_status"] = prod_state_status
        observations["prod_state"] = prod_state

        # --- the browser: the plan is visible, the panel opens, the input
        #     takes the value
        observations.update(
            _drive_browser(
                base_url,
                test_plan_id,
                prod_plan_id,
                project_dir,
                screenshot_path,
            )
        )
        observations["screenshot_path"] = str(screenshot_path)
        observations["screenshot_exists"] = screenshot_path.exists()
        observations["screenshot_size"] = (
            screenshot_path.stat().st_size if screenshot_path.exists() else 0
        )

        # --- the executor was never started (the confirm button is never
        #     clicked, so the plan has to still be waiting)
        _, after_browser = _http_json(
            "GET", f"{base_url}/api/plan/{test_plan_id}/state"
        )
        observations["phase_after_browser"] = (
            after_browser.get("current_phase") if isinstance(after_browser, dict) else None
        )
        _, exec_status = _http_json(
            "GET", f"{base_url}/api/execution/{test_plan_id}/status"
        )
        observations["execution_status_after_browser"] = (
            exec_status.get("status") if isinstance(exec_status, dict) else None
        )

        # --- the probe: another process, reading the mode off the disk
        observations["probe"] = _run_probe(
            work,
            plans_dir / test_plan_id,
            plans_dir / prod_plan_id,
        )

        # --- the report: two artefacts on the disk
        observations["report"] = _write_report(plans_dir / test_plan_id)
    finally:
        _teardown(proc)

    return observations


# ---------------------------------------------------------------------------
# 1 -- the mode is declared, echoed and persisted
# ---------------------------------------------------------------------------


def test_the_import_declares_and_echoes_the_mode(mode_chain: Dict[str, Any]):
    """The import's answer names the mode the plan actually carries.

    One request says ``test``; the other says nothing.  The second one
    is the important half: a request that says nothing must come back as
    ``production`` -- the mode that is safe to assume -- rather than
    silently becoming a rehearsal.
    """
    test_body = mode_chain["test_import_body"]
    prod_body = mode_chain["prod_import_body"]

    assert isinstance(test_body, dict), f"the import answered {test_body!r}"
    assert isinstance(prod_body, dict), f"the import answered {prod_body!r}"
    assert test_body["mode"] == "test"
    assert prod_body["mode"] == "production"


def test_the_mode_is_readable_from_the_plan_state(mode_chain: Dict[str, Any]):
    """The mode is in the plan's flags, not just in the response.

    The response is only half of the claim.  The mode has to be in the
    plan's **flags**, because that is where the cost gate, the executor
    and the verification sub-agent read it from -- three processes that
    never see the HTTP response.  A mode that lived only in the response
    would be a mode nobody downstream could act on.
    """
    test_state = mode_chain["test_state"]
    prod_state = mode_chain["prod_state"]

    assert mode_chain["test_state_status"] == 200
    assert mode_chain["prod_state_status"] == 200
    assert test_state["flags"]["mode"] == "test"
    assert prod_state["flags"]["mode"] == "production"


# ---------------------------------------------------------------------------
# 2 -- the plans are visible in the UI
# ---------------------------------------------------------------------------


def test_both_plans_are_visible_in_the_sidebar(mode_chain: Dict[str, Any]):
    """The two plans are both on the page the operator sees.

    One of them is the plan this file is about; the other is the control
    -- the production plan that must keep behaving the way it always did.
    A sidebar that showed only one of them would mean one of the two
    imports never landed.
    """
    assert mode_chain["test_plan_visible"] is True, (
        f"the test-mode plan is not in the sidebar: "
        f"{mode_chain.get('sidebar_plan_ids')!r}"
    )
    assert mode_chain["prod_plan_visible"] is True, (
        f"the production plan is not in the sidebar: "
        f"{mode_chain.get('sidebar_plan_ids')!r}"
    )
    sidebar_ids = mode_chain["sidebar_plan_ids"]
    assert mode_chain["test_plan_id"] in sidebar_ids
    assert mode_chain["prod_plan_id"] in sidebar_ids


# ---------------------------------------------------------------------------
# 3 -- the operator's input reached the form
# ---------------------------------------------------------------------------


def test_the_typed_directory_is_the_one_the_form_holds(mode_chain: Dict[str, Any]):
    """What was typed into ``#project-dir-input`` is what the form holds.

    The panel is inside ``#execution-panel``, which starts out hidden --
    so the input is only reachable after ``#btn-start-execution`` has
    opened the panel.  That is the whole point of the click: it is what
    a user does, and it has no side effect beyond the panel.
    """
    assert mode_chain["start_button_visible"] is True
    assert mode_chain["project_dir_visible"] is True
    expected = str(Path(mode_chain["work"]) / "proj")
    assert mode_chain["project_dir_value"] == expected


def test_the_executor_was_never_started(mode_chain: Dict[str, Any]):
    """``#btn-confirm-execute`` was never clicked, so nothing ran.

    The button is bound to ``startExecution()``: clicking it really
    spawns the executor.  This file is about the mode, not about a run,
    and a run would change what the plan's state says -- so the plan has
    to still be waiting, and the execution status has to still be
    ``not_started``.
    """
    assert mode_chain["phase_after_browser"] == "queued", (
        f"the plan moved past 'queued': {mode_chain['phase_after_browser']!r}"
    )
    assert mode_chain["execution_status_after_browser"] == "not_started", (
        f"an execution was started: "
        f"{mode_chain['execution_status_after_browser']!r}"
    )


# ---------------------------------------------------------------------------
# 4 -- the mode reached the process boundary
# ---------------------------------------------------------------------------


def test_the_wrapper_injected_the_test_mode(mode_chain: Dict[str, Any]):
    """The probe's own environment says ``test``.

    The probe was launched by the wrapper, and the wrapper is what
    injects the mode.  The probe cannot see this process's memory and
    cannot see the HTTP response -- it can only see its environment, so
    this answer came from the wrapper and from nowhere else.
    """
    probe = mode_chain["probe"]
    assert probe["env_mode"] == "test", f"the probe saw {probe['env_mode']!r}"


def test_the_scoped_tool_reads_the_mode_off_the_disk(mode_chain: Dict[str, Any]):
    """The scoped tool reads the *test* plan's mode as ``test``.

    ``_scoped_plan_mode`` is the reader the verification sub-agent's
    scoped tool uses.  It reads the plan's state, in another process,
    from the database the server wrote -- so this answer is about the
    disk, not about this test.
    """
    probe = mode_chain["probe"]
    assert probe["scoped_test"] == "test", f"the scoped tool saw {probe['scoped_test']!r}"
    assert probe["test_mode"] == "test", f"the plan's mode read as {probe['test_mode']!r}"


def test_the_wrapper_refuses_a_production_caller():
    """The wrapper will not start test-mode work for a production caller.

    This is the other half of the wrapper's contract, and it is the half
    this file itself leans on: an E2E that inherited
    ``PDT_PLAN_MODE=production`` would be **refused**, and every case in
    this file would go red for a reason that has nothing to do with the
    mode.  So the refusal is pinned here.

    A refusal that still ran the command would be a refusal in name
    only, so the command's own output is checked too: it must not have
    run.
    """
    env = dict(os.environ)
    env["PDT_PLAN_MODE"] = "production"
    completed = subprocess.run(
        [str(TEST_RUNNER), "--", str(VENV_PYTHON), "-c", "print('the command ran')"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 2, (
        f"the wrapper answered {completed.returncode} instead of refusing\n"
        f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
    )
    assert "the command ran" not in completed.stdout, (
        f"the wrapper refused but ran the command anyway: {completed.stdout!r}"
    )
    assert "refusing to run" in completed.stderr, (
        f"the refusal did not say why: {completed.stderr!r}"
    )


# ---------------------------------------------------------------------------
# 5 -- the cost gate splits the two modes
# ---------------------------------------------------------------------------


def test_the_cost_gate_defers_in_test_mode_and_not_in_production(
    mode_chain: Dict[str, Any]
):
    """A ``llm_quota`` verification point is deferred in test mode only.

    Same verification point, same cost declaration, two plans: the test
    plan's point is deferred (this round must not spend real quota), and
    the production plan's point runs as usual (a production verification
    is supposed to spend quota).  The evidence names the mode that
    decided it -- a verdict that cannot say why it fired is a verdict
    nobody can audit.
    """
    probe = mode_chain["probe"]

    test_verdict = probe["test_verdict"]
    assert test_verdict is not None, (
        "the cost gate did not defer anything in test mode: "
        f"{probe!r}"
    )
    assert test_verdict["status"] == "DEFERRED"
    assert test_verdict["evidence"]["plan_mode"] == "test"

    assert probe["prod_verdict"] is None, (
        f"the cost gate fired in production mode: {probe['prod_verdict']!r}"
    )


# ---------------------------------------------------------------------------
# 6 -- the report's two artefacts
# ---------------------------------------------------------------------------


def test_the_report_json_counts_the_test_mode_skip(mode_chain: Dict[str, Any]):
    """The JSON carries the list and the count of the test-mode skips.

    The count is derived by the writer from the report's own entries, so
    a count of one means the writer actually saw one test-mode
    ``DEFERRED`` entry -- not that somebody typed the number in.
    """
    report = mode_chain["report"]
    data = report["json"]
    assert data["cost_gate_skipped_test_mode_count"] == 1, (
        f"the count says {data.get('cost_gate_skipped_test_mode_count')!r}"
    )
    assert data["cost_gate_skipped_test_mode"] == ["VP-1"]


def test_the_report_markdown_has_the_test_mode_section(mode_chain: Dict[str, Any]):
    """The Markdown has its own section for the test-mode skips.

    A ``DEFERRED`` point that skipped because the run was a rehearsal is
    not the same thing as one that skipped for any other reason, and a
    reader has to be able to tell them apart.  So the Markdown names the
    section, and the JSON's count and the section's rows are the same
    number.
    """
    markdown = mode_chain["report"]["markdown"]
    assert "test 模式下因成本门跳过" in markdown, (
        "the Markdown has no section for the test-mode cost skips:\n"
        f"{markdown[:2000]}"
    )
    section_title = "## test 模式下因成本门跳过"
    body = markdown.split(section_title, 1)[1].split("\n## ", 1)[0]
    rows = [line for line in body.splitlines() if line.startswith("| VP-")]
    assert len(rows) == 1, f"the test-mode section has {len(rows)} rows: {rows!r}"
    assert "VP-1" in rows[0]


# ---------------------------------------------------------------------------
# 7 -- the screenshot is on the disk
# ---------------------------------------------------------------------------


def test_the_screenshot_was_written(mode_chain: Dict[str, Any]):
    """The screenshot exists and is not empty.

    It is the archive of the UI segment: the assertion above it says the
    form held the value, this one says there is a picture of the page
    that held it.
    """
    path = Path(mode_chain["screenshot_path"])
    assert path.exists(), f"no screenshot at {path}"
    assert path.stat().st_size > 0, f"the screenshot at {path} is empty"


# ---------------------------------------------------------------------------
# The page must not have thrown
# ---------------------------------------------------------------------------


def test_the_page_raised_no_javascript_error(mode_chain: Dict[str, Any]):
    """The served page ran without a JavaScript error.

    A page that threw while opening the plan would still show the plan in
    the sidebar -- the list is rendered before the click -- so the other
    assertions could pass on a page that is broken half way through.
    """
    assert mode_chain["page_errors"] == [], (
        f"the page raised: {mode_chain['page_errors']!r}"
    )

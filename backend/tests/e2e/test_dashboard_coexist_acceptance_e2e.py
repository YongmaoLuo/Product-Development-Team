"""共存目录的用户可见验收 —— 真后端 + 真浏览器。

这一份是「任务优先」共存语义的最后一段路
------------------------------------------------
前面那些任务改的是**判定**与**载荷**：产物判定收敛到
:func:`plan_artifacts.detect`（``dev`` / ``verification`` / ``empty`` 三个
取值，**不再有** ``conflict``），``GET /api/plans`` 不再有冲突桶，生成入口
不再因为目录里已有 ``tasks.json`` 就拒绝。

判定对了、载荷对了，**用户看得见吗**？这一份回答这个问题：真后端进程、
真 Chromium、真点击 —— 仪表盘把这条共存的 plan 画在计划列表里，画成一条
普通的开发分支计划（不是冲突）；页面全程没有未捕获异常，也没有 console
error；截图落在 ``/tmp/pdt-accept/screenshots/plans-list.png``。

被钉住的东西
------------
* ``#plans-list`` 里**有**这条共存 plan 的 id，**没有**冲突文案；
* 页面全程没有未捕获异常，也没有 console error（资源 404 不算）；
* 截图存在、非空；
* 经 HTTP 生成计划书之后刷新页面，这条 plan **仍然**在列表里。

真实的东西
----------
* 后端是 ``backend.server`` 子进程：真的 plans 目录、真的 SQLite、真的
  计划书生成（模型是 scripted 替身，答案来自 fixture 文件）；
* 浏览器是 Playwright 驱动的真 Chromium，**从本机的 ms-playwright 缓存里
  解析**（不下载）；
* 页面上的一步是**点击**：走的是前端自己的事件与自己的 fetch。

端口
----
本机的固定端口已经被既有服务占着，所以这份文件**一个端口字面量都不写**：
端口由 :func:`_free_port` 现取，同一个变量喂给 ``PDT_PORT``、就绪轮询的
URL 与浏览器 ``goto`` 的 URL。端口被占（后端退出码非 0，或 30 秒内没就绪）
就换一个再试，最多 5 次，每次都把实际用的端口打印出来。

验收目录
--------
截图落在 ``/tmp/pdt-accept/screenshots/plans-list.png`` —— 仓库之外：它是
这次验收的产物，不是要跟着提交走的东西。这个目录**跑之前清空、跑完清空**：
跑之前清空是免得上一轮的截图冒充这一轮的证据；跑完清空是不留垃圾。

清空的动作放在模块的最后（模块级 fixture 的 teardown），而不是后端
fixture 的 ``finally`` 里 —— 后端 fixture 先于读它结果的那些测试被拆除，
在那里清空会把测试正要断言的那张截图删掉。后端的终止与端口的释放在
``finally`` 里，因为它们跟截图无关。

复用
----
``tests/e2e/test_execution_status_restart_e2e.py`` 的 ``_free_port``；
``tests/fixtures/verification_mode.py`` 的 ``build_upstream_repo`` 与
``build_plan_doc``（计划书的内容契约由那一处写死，不另抄一份）。

跑法::

    cd backend && .venv/bin/python3 -m pytest \\
        tests/e2e/test_dashboard_coexist_acceptance_e2e.py -v
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE
from plan_artifacts import PLAN_DOC_FILENAME, TASKS_FILENAME
from tests.e2e.test_execution_status_restart_e2e import _free_port
from tests.fixtures.verification_mode import build_plan_doc, build_upstream_repo

#: 这份文件是端到端验收：它起服务、开浏览器、走完用户的那条路。
#: **不带** ``time_sensitive`` —— 那条 marker 会被 addopts 的
#: ``-m "not time_sensitive"`` 取消选择，测试会被静默地不收集。
pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

_HERE: Path = Path(__file__).resolve()
BACKEND_DIR: Path = _HERE.parents[2]
PROJECT_ROOT: Path = _HERE.parents[3]

#: 这条流程用的 plan id。
#:
#: 形状受 plan id 校验器管（扁平单段、只含安全字符、长度有上限）—— 与生产
#: 走的是同一个校验器，所以这里写错会在第一次请求就被 400 挡下。
PLAN_ID: str = "coexist-dashboard"

#: 验收证据的落地根目录，以及截图的确切位置。
#:
#: 它在**仓库之外**：截图是这次验收的产物，不是要跟着提交走的东西。
ACCEPT_ROOT: Path = Path("/tmp/pdt-accept")
SCREENSHOT_PATH: Path = ACCEPT_ROOT / "screenshots" / "plans-list.png"

#: 浏览器在等各个页面元素时的预算（毫秒）。没有固定 sleep：一个等不到的
#: 等待就是一次带原因的失败。
BROWSER_TIMEOUT_MS = 30000

#: 整段浏览器过程的预算（秒）。
DRIVER_TIMEOUT_SECONDS = 240

#: 后端起起来之后，``GET /api/plans`` 最多等多久（秒）。
#:
#: 30 秒足够一个真后端把 uvicorn 拉起来；超过就是**起不来**，不是「慢」。
READY_TIMEOUT_SECONDS = 30.0

#: 计划书生成之后，等列表**反映**这次生成最多等多久（秒）。
#:
#: ``GET /api/plans`` 的渲染结果被缓存两秒（服务端
#: ``_PLANS_CACHE_TTL_SECONDS``），而生成端点不去清这个缓存 —— 列表靠 TTL
#: 自己恢复。所以「生成之后的列表」是一个**要等的条件**，不是一个可以假设
#: 的事实：不等它，刷新看到的就可能是生成之前的快照，那样的断言断言的是
#: 一张旧照片。
#:
#: 平时一秒多就等到（TTL 说了算）；这个上界是给机器很忙的时候留的余量。
LISTING_SETTLE_TIMEOUT_SECONDS = 30.0

#: 端口被占时换一个新端口重试几次。
BOOT_ATTEMPTS = 5

#: 端口交还之后，等它重新可绑定的预算（秒）。
PORT_RELEASE_SECONDS = 10.0

#: ``install_hermetic_env()`` 把 ``$HOME`` 指向了一个临时目录；Playwright 的
#: 浏览器二进制装在**跑起来时那个 home** 底下。这个变量记着那个 home。
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"

#: 列表里**不许**出现的冲突文案。
#:
#: 冲突桶曾经叫 ``conflict``，冲突载荷的错误码是 ``artifact_conflict``，
#: 旧文案说的是「两类产物互斥」（以及它的近亲「冲突」）。共存之后这几样都
#: 不许再出现在这条 plan 的卡片上 —— 共存是合法的，不是冲突。
CONFLICT_WORDS: Tuple[str, ...] = (
    "conflict",
    "artifact_conflict",
    "互斥",
    "冲突",
)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_json(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    """One HTTP request against the backend; ``(status, decoded body)``.

    The guard header goes with every call: ``/api/*`` is guarded, and a
    request without the header is refused with 403 before it reaches a
    route.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header(REQUEST_HEADER, REQUEST_HEADER_VALUE)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# The seed: a coexist plan on disk and in SQLite
# ---------------------------------------------------------------------------


def _seed(work: Path) -> Tuple[Any, Path]:
    """Write the coexist plan and return ``(upstream_repo, plan_dir)``.

    Two artifacts, one directory: the development branch's own task list
    (``tasks.json``) and — added later by the product itself — the plan
    document (``verification-plan.md``).  Coexistence is legal, so the
    directory starts with the task list alone and the generate endpoint
    puts the document **next to** it.

    The baseline is recorded in the routing row, because the plan-document
    generator reads the baseline from the plan state and nowhere else.
    """
    upstream = build_upstream_repo(work / "upstream")
    plan_dir = work / "plans" / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    # The task list is **not** empty on purpose: a plan with no tasks
    # short-circuits the verification round before a single verification
    # point is dispatched.
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "T1",
                        "title": "让计划书与任务清单共存",
                        "files_to_modify": ["README.md"],
                        "status": "completed",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    baseline = {
        "repo_dir": str(upstream.repo),
        "ref": upstream.pr_ref,
        "commit_sha": upstream.head_sha(),
        "pr_number": upstream.pr_number,
    }
    verification = {
        "status": "pending",
        "round": 0,
        "max_rounds": 3,
        "stop_reason": None,
        "baseline": baseline,
    }

    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate

    conn = db_open(work / "state.db")
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, version, verification, updated_at) "
            "VALUES (?, ?, 0, ?, datetime('now'))",
            (PLAN_ID, "executing", json.dumps(verification, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()

    return upstream, plan_dir


def _script_dir(work: Path, upstream: Any) -> Path:
    """The scripted model's fixture directory: one prompt, one answer.

    The answer is a plan document that is already structurally valid — the
    structure check is the model boundary's job, and a fixture that failed
    it would turn every run into a formatting exercise.
    """
    script_dir = work / "llm-scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    registry = {"prompts": {"verification_plan": "你要写一份「验证计划书」"}}
    (script_dir / "registry.json").write_text(
        json.dumps(registry, ensure_ascii=False), encoding="utf-8"
    )
    (script_dir / "verification_plan.txt").write_text(
        build_plan_doc(upstream.repo, upstream.pr_number, upstream.pr_ref),
        encoding="utf-8",
    )
    return script_dir


# ---------------------------------------------------------------------------
# The backend process
# ---------------------------------------------------------------------------


def _boot_backend(work: Path, port: int, script_dir: Path) -> subprocess.Popen:
    """Start a real ``backend.server`` process against ``work``.

    The three environment variables are the whole contract between the
    seed and the server: the plans tree, the state database and the port.
    Four more are set because the child would otherwise inherit the test
    process's environment and misbehave:

    * ``PDT_PROVIDER_MODE=inherit`` — the boot asks CC Switch for the
      provider chain and **aborts the startup** when it cannot be asked.
      This server exists to answer one question; it must not need a
      provider fleet to come up.
    * ``PDT_ORPHAN_REAPER=0`` — the boot otherwise walks **every process on
      the machine** to find orphans left behind by a previous life.  A
      throwaway server has no orphans to reap.
    * ``PDT_LLM_MODE`` / ``PDT_LLM_SCRIPT_DIR`` — the model is a scripted
      double, so the generate step answers from a fixture file instead of
      reaching for a provider.
    * ``PYTHONPATH`` — ``backend/`` has to be importable, because the
      server's own modules import each other by their flat names.
    """
    from coding_tool import LLM_MODE_ENV, LLM_MODE_SCRIPTED, LLM_SCRIPT_DIR_ENV

    env = dict(os.environ)
    env.update(
        {
            "PDT_PLANS_DIR": str(work / "plans"),
            "PDT_STATE_DB_PATH": str(work / "state.db"),
            "PDT_PORT": str(port),
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_ORPHAN_REAPER": "0",
            LLM_MODE_ENV: LLM_MODE_SCRIPTED,
            LLM_SCRIPT_DIR_ENV: str(script_dir),
            "PYTEST_CURRENT_TEST": (
                "backend/tests/e2e/test_dashboard_coexist_acceptance_e2e.py "
                "(setup)"
            ),
            "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
        }
    )
    # A stale secret descriptor must never be closed by a process that
    # never owned it.
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)
    env.pop("ANTHROPIC_BASE_URL", None)

    log_path = work / f"server-{port}.log"
    handle = log_path.open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._pdt_log_path = log_path  # type: ignore[attr-defined]
    proc._pdt_log_handle = handle  # type: ignore[attr-defined]
    return proc


def _server_output(proc: subprocess.Popen, limit: int = 2000) -> str:
    """The tail of the backend's own output."""
    path = getattr(proc, "_pdt_log_path", None)
    if path is None:
        return "(no server output was captured)"
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - the file is ours
        return f"(could not read {path}: {exc})"
    return text[-limit:]


def _wait_for_plans_api(port: int, proc: subprocess.Popen) -> None:
    """Wait until the backend answers the plans listing on ``port``.

    The backend is up when the API answers, not when the process exists:
    the process exists for a moment before uvicorn is listening.  A
    backend that dies on the way up says so in its own output, so that
    output is attached to the failure.
    """
    url = f"http://127.0.0.1:{port}/api/plans"
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"the backend exited with {proc.returncode} before it was "
                f"ready:\n{_server_output(proc)}"
            )
        try:
            status, body = _http_json("GET", url)
            if status == 200:
                return
            last = (status, body)
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last = exc
        time.sleep(0.2)
    raise AssertionError(
        f"{url} was not ready within {READY_TIMEOUT_SECONDS}s; "
        f"last={last!r}\n{_server_output(proc)}"
    )


def _stop_backend(proc: Optional[subprocess.Popen]) -> None:
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


def _remove_tree(root: Path) -> None:
    """Remove ``root`` and everything under it.  Best effort.

    The acceptance tree lives outside the repository, so nothing here is
    tracked by git — this is only about not leaving a stale tree behind
    that would make the next run's assertions meaningless.
    """
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
            else:
                path.rmdir()
        except OSError:  # pragma: no cover - best effort cleanup
            pass
    try:
        root.rmdir()
    except OSError:  # pragma: no cover - best effort cleanup
        pass


def _port_is_free(port: int) -> bool:
    """Can ``port`` be bound again on the loopback interface?"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _wait_for_the_port(port: int) -> None:
    """Wait until the port is bindable again, or say that it is not."""
    deadline = time.monotonic() + PORT_RELEASE_SECONDS
    while time.monotonic() < deadline:
        if _port_is_free(port):
            return
        time.sleep(0.2)
    raise AssertionError(
        f"the backend on port {port} was stopped but the port is still "
        f"taken after {PORT_RELEASE_SECONDS}s"
    )


def _boot_with_retries(
    work: Path, script_dir: Path
) -> Tuple[subprocess.Popen, int, List[int]]:
    """Boot the backend, moving to a fresh port when this one is taken.

    The port is asked of the kernel, so it is free **at the moment it is
    returned** — but the backend's own port backoff means a port taken in
    between makes the server bind a neighbouring one, and then the port
    this test polls never answers.  So: a boot that does not become ready
    is a boot on a port that was not usable, and the answer is another
    port, not a longer wait.

    Returns ``(proc, port, attempted_ports)``.
    """
    attempted: List[int] = []
    last_error: Optional[BaseException] = None
    for attempt in range(1, BOOT_ATTEMPTS + 1):
        port = _free_port()
        attempted.append(port)
        print(
            f"[dashboard-coexist-e2e] boot attempt {attempt}/{BOOT_ATTEMPTS} "
            f"on port {port}"
        )
        proc = _boot_backend(work, port, script_dir)
        try:
            _wait_for_plans_api(port, proc)
        except AssertionError as exc:
            last_error = exc
            _stop_backend(proc)
            continue
        return proc, port, attempted
    raise AssertionError(
        f"the backend never became ready on any of {len(attempted)} ports "
        f"({attempted}): {last_error}"
    )


def _wait_for_listing_to_settle(
    base_url: str, plan_id: str, timeout: float = LISTING_SETTLE_TIMEOUT_SECONDS
) -> List[Dict[str, Any]]:
    """Wait until the plans listing shows **both** artifacts, then return it.

    The listing is a cached rendering of the plans tree, and the cache
    lives for a couple of seconds.  The generate endpoint does not drop
    it, so right after a generate the listing can still be the snapshot
    taken before the document existed.  That snapshot is a legitimate
    answer — it just is not the answer to "what does the listing say
    after the generate", and asserting on it would assert on a photo of
    the past.

    So the listing is **waited for**: the condition is "this plan's entry
    lists both artifacts", which is exactly the coexistence claim the
    payload is supposed to make.  The wait is bounded, and the server's
    TTL is what makes it terminate.
    """
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        status, body = _http_json("GET", f"{base_url}/api/plans")
        if status == 200 and isinstance(body, list):
            last = body
            for entry in body:
                if not isinstance(entry, dict) or entry.get("id") != plan_id:
                    continue
                names = entry.get("artifacts") or []
                if PLAN_DOC_FILENAME in names and TASKS_FILENAME in names:
                    return body
        time.sleep(0.25)
    raise AssertionError(
        f"the plans listing did not show both artifacts for {plan_id!r} "
        f"within {timeout}s; last={last!r}"
    )


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------


def _browser_executable() -> Optional[str]:
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache; a
    driver whose expected revision is not the one installed cannot launch
    anything.  The binaries that *are* installed are found here instead —
    nothing is downloaded, and nothing is hard-coded: the cache lives under
    the home the run was launched with.
    """
    home = Path(os.environ.get(REAL_HOME_ENV) or Path.home())
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


def _playwright_interpreter() -> Optional[str]:
    """An interpreter that can import ``playwright``, or ``None``.

    The interpreter running pytest comes first; the interpreter behind a
    ``playwright`` command on ``PATH`` is the fallback (the command is a
    Python entry point, so its shebang names an interpreter that has the
    package installed).  Nothing is hard-coded: both are read out of the
    environment.
    """
    import shlex
    import shutil

    candidates = [sys.executable]
    command = shutil.which("playwright")
    if command:
        try:
            first_line = Path(command).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[0]
        except (OSError, IndexError):
            first_line = ""
        if first_line.startswith("#!"):
            parts = shlex.split(first_line[2:].strip())
            interpreter = ""
            if parts:
                if os.path.basename(parts[0]) == "env" and len(parts) > 1:
                    interpreter = parts[1]
                else:
                    interpreter = parts[0]
            if "python" in interpreter:
                candidates.append(interpreter)

    for candidate in candidates:
        try:
            probe = subprocess.run(
                [candidate, "-c", "import playwright.sync_api"],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


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


_BROWSER_DRIVER = r'''
"""Drive the dashboard the way an operator does, and report what it showed.

argv: <base_url> <plan_id> <screenshot_path> <chromium_executable> <mode>

``mode`` is ``open`` — the first visit: the plan is listed, the operator
clicks its card, the plan's own view opens — or ``reload`` — the second
visit, after the plan document was generated: the plan is still listed.

Every step is a click on the page, so the frontend's own handlers run.
Nothing here reaches into the application's internals to make a view show
up — the view shows up because the page decided to show it.

The report is one JSON object on stdout.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


base_url, plan_id, screenshot_path, chromium, mode = sys.argv[1:6]

#: How long the browser waits for each of the things it needs.
timeout_ms = 30000

report = {"page_errors": [], "console_errors": [], "dialogs": [], "mode": mode}

with sync_playwright() as driver:
    browser = driver.chromium.launch(executable_path=chromium)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    def _on_page_error(exc):
        report["page_errors"].append(str(exc))

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

    def _on_dialog(dialog):
        report["dialogs"].append(dialog.message)
        dialog.dismiss()

    page.on("pageerror", _on_page_error)
    page.on("console", _on_console)
    page.on("dialog", _on_dialog)

    page.goto(base_url, wait_until="load")

    # The plans view renders into this container.
    page.wait_for_selector("#plans-list", timeout=timeout_ms)

    # The card is the operator's way in.  Waiting for it is a wait for the
    # listing to arrive, not for a fixed delay.
    card = '.plan-card[data-plan="%s"]' % plan_id
    page.wait_for_selector(card, timeout=timeout_ms)

    report["plan_card_visible"] = page.is_visible(card)
    report["plans_list_text"] = page.inner_text("#plans-list")
    report["card_text"] = page.inner_text(card)
    report["card_status"] = (
        page.inner_text(card + " .plan-status") or ""
    ).strip()

    if mode == "open":
        # The click is the operator's: the page's own handler decides what
        # to do with it.
        page.click(card)

        # The plan's own view opens because the page decided to open it.
        page.wait_for_selector(
            "#tasks-view", state="visible", timeout=timeout_ms
        )
        report["tasks_view_visible"] = page.is_visible("#tasks-view")
        report["clicked"] = True

        # The archive of what the operator saw.
        shot = Path(screenshot_path)
        shot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot), full_page=True)
        report["screenshot"] = str(shot)
        report["screenshot_size"] = shot.stat().st_size if shot.exists() else 0

    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


def _drive(
    base_url: str,
    plan_id: str,
    screenshot_path: Path,
    chromium: str,
    mode: str,
    work: Path,
) -> Dict[str, Any]:
    """Run the browser driver and return the report it printed."""
    script = work / f"drive_dashboard_{mode}.py"
    script.write_text(_BROWSER_DRIVER, encoding="utf-8")

    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no interpreter with playwright is installed in this environment; "
            "the same assertion has to be driven through "
            "``mcp__playwright__`` / ``mcp__puppeteer__`` by hand, and the "
            "result belongs in the acceptance report"
        )

    result = subprocess.run(
        [
            interpreter,
            str(script),
            base_url,
            plan_id,
            str(screenshot_path),
            chromium,
            mode,
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
                "binaries are not; the same assertion has to be driven "
                "through ``mcp__playwright__`` / ``mcp__puppeteer__`` by hand"
            )
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    lines = [
        line for line in (result.stdout or "").strip().splitlines() if line.strip()
    ]
    if not lines:
        raise AssertionError(
            f"the browser driver printed nothing; stderr={result.stderr!r}"
        )
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# The fixture: one real backend, two real visits
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def acceptance_root():
    """The acceptance tree — empty before the run, gone after it.

    The tree holds the screenshot, and the screenshot is what the last
    test asserts on.  So the tree is emptied **before** the run (a stale
    screenshot from an earlier run must never pass as this run's) and
    emptied **again** after the run (nothing is left behind).

    It is a fixture of its own rather than part of the backend fixture's
    ``finally`` because the backend fixture is torn down *before* the
    tests that read its results — cleaning the tree there would delete
    the very artifact those tests are about.  Fixture teardown runs in
    reverse setup order, so this one — set up first — is torn down last.
    """
    _remove_tree(ACCEPT_ROOT)
    yield ACCEPT_ROOT
    _remove_tree(ACCEPT_ROOT)


@pytest.fixture(scope="module")
def acceptance(acceptance_root, tmp_path_factory) -> Dict[str, Any]:
    """A real backend, a real browser, and what the page showed.

    The backend is a real ``backend.server`` process against a plans tree
    and a state database that live in pytest's temporary directory.  The
    browser is a real Chromium driven through the page's own events.
    """
    work = tmp_path_factory.mktemp("dashboard-coexist-acceptance-e2e")
    upstream, plan_dir = _seed(work)
    script_dir = _script_dir(work, upstream)

    chromium = _browser_executable()
    if chromium is None:
        pytest.skip(
            "no Chromium is installed under the local ms-playwright cache; "
            "the same assertion has to be driven through "
            "``mcp__playwright__`` / ``mcp__puppeteer__`` by hand"
        )

    proc: Optional[subprocess.Popen] = None
    port: Optional[int] = None
    attempted: List[int] = []
    report: Dict[str, Any] = {}
    reload_report: Dict[str, Any] = {}
    generated: Tuple[int, Any] = (0, None)
    settled_listing: List[Dict[str, Any]] = []
    try:
        proc, port, attempted = _boot_with_retries(work, script_dir)
        base_url = f"http://127.0.0.1:{port}"

        # --- 1. the first visit: the plan is listed, the operator clicks it
        report = _drive(
            base_url, PLAN_ID, SCREENSHOT_PATH, chromium, "open", work
        )

        # --- 2. the plan document is generated over HTTP ------------------
        # The generate endpoint is the one the coexistence work changed:
        # a directory that already holds ``tasks.json`` is no longer a
        # reason to refuse.  It is called the way an operator calls it —
        # over HTTP, against the running backend.
        generated = _http_json(
            "POST", f"{base_url}/api/verification-plan/{PLAN_ID}/generate", {}
        )
        if generated[0] != 200:
            # Say it here, where the answer is still fresh: further down
            # the flow would only report a symptom of it (a listing that
            # never shows the document, a plan that vanished).
            raise AssertionError(
                f"generating the plan document next to the task list was "
                f"refused: {generated[0]} {generated[1]!r}"
            )

        # The listing is a cached rendering, and the generate does not
        # drop that cache — so wait for the listing to catch up before
        # reading it (a stale one would answer a different question).
        settled_listing = _wait_for_listing_to_settle(base_url, PLAN_ID)

        # --- 3. the second visit: the plan is still listed ---------------
        reload_report = _drive(
            base_url, PLAN_ID, SCREENSHOT_PATH, chromium, "reload", work
        )
    finally:
        # Whatever is propagating right now must not be masked by the
        # teardown's own complaints: inside a ``finally``, ``sys.exc_info``
        # is the exception that is on its way out.
        propagating = sys.exc_info()[0] is not None
        # The failure path stops the backend too, and the port goes back
        # to the kernel.  The acceptance tree is cleaned by
        # ``acceptance_root`` — after the tests have read the screenshot
        # out of it.
        if proc is not None:
            _stop_backend(proc)
        if port is not None:
            try:
                _wait_for_the_port(port)
            except AssertionError:
                if not propagating:
                    raise
                print(
                    "[dashboard-coexist-e2e] the backend was stopped but "
                    "port %s was still taken; the failure being reported is "
                    "the one that got us here" % port
                )

    print(
        "[dashboard-coexist-e2e] backend on port %s, screenshot at %s"
        % (port, SCREENSHOT_PATH)
    )

    return {
        "work": work,
        "plan_dir": plan_dir,
        "port": port,
        "attempted_ports": attempted,
        "report": report,
        "reload_report": reload_report,
        "generated": generated,
        "settled_listing": settled_listing,
        "screenshot": SCREENSHOT_PATH,
    }


# ---------------------------------------------------------------------------
# The assertions
# ---------------------------------------------------------------------------


def test_dashboard_lists_coexist_plan_without_conflict(acceptance) -> None:
    """计划列表里有这条共存的 plan，而且画成一条普通的计划 —— 不是冲突。

    「有」是 ``#plans-list`` 的**文本**里有这条 plan 的 id（不是「元素存在」
    —— 一个空容器也算有元素）；「不是冲突」是同一段文本里没有冲突桶的名字、
    冲突载荷的错误码，也没有「互斥」那句旧文案。共存是合法的，画成冲突就是
    把一次合法状态报成了错误状态。
    """
    report = acceptance["report"]
    reload_report = acceptance["reload_report"]

    assert report.get("plan_card_visible") is True, (
        f"the coexist plan never appeared in the plans list: {report!r}"
    )

    listing = report.get("plans_list_text") or ""
    assert PLAN_ID in listing, (
        f"the plans list does not carry the plan id {PLAN_ID!r}: {listing!r}"
    )
    assert report.get("clicked") is True, (
        f"the plan card was never clicked: {report!r}"
    )
    assert report.get("tasks_view_visible") is True, (
        f"clicking the plan card did not open the plan's own view: {report!r}"
    )

    for word in CONFLICT_WORDS:
        assert word not in listing, (
            f"the plans list carries the conflict wording {word!r}: {listing!r}"
        )
        assert word not in (report.get("card_status") or ""), (
            f"the plan card is labelled as a conflict ({word!r}): {report!r}"
        )

    # The generate step did not change that: the plan is still listed, and
    # still not labelled as a conflict.  The generate step is what makes
    # the claim mean something — it is the step where a directory that
    # already holds ``tasks.json`` used to be refused as a conflict.
    status, body = acceptance["generated"]
    assert status == 200, (
        f"generating the plan document alongside the task list failed: "
        f"{status} {body!r}"
    )
    assert isinstance(body, dict) and body.get("document_path"), (
        f"the generate endpoint did not report a document: {body!r}"
    )

    plan_dir = Path(str(acceptance["plan_dir"]))
    assert (plan_dir / TASKS_FILENAME).is_file(), (
        f"the task list is gone from the plan directory: {plan_dir}"
    )
    assert (plan_dir / PLAN_DOC_FILENAME).is_file(), (
        f"the plan document was not written next to the task list: {plan_dir}"
    )

    # The payload says the same thing: this plan is on the **dev** branch
    # (task-first) and carries both artifacts.  There is no conflict
    # bucket to be in — the listing used to have one, and a plan with
    # both artifacts used to land in it.
    entries = [
        entry
        for entry in acceptance["settled_listing"]
        if isinstance(entry, dict) and entry.get("id") == PLAN_ID
    ]
    assert len(entries) == 1, (
        f"the listing does not carry exactly one entry for {PLAN_ID!r}: "
        f"{acceptance['settled_listing']!r}"
    )
    entry = entries[0]
    assert entry.get("branch") == "dev", (
        f"a plan carrying both artifacts is not on the dev branch: {entry!r}"
    )
    assert sorted(entry.get("artifacts") or []) == sorted(
        [TASKS_FILENAME, PLAN_DOC_FILENAME]
    ), f"the listing does not carry both artifacts: {entry!r}"
    assert "conflict" not in json.dumps(entry, ensure_ascii=False), (
        f"the listing entry carries conflict wording: {entry!r}"
    )

    reloaded = reload_report.get("plans_list_text") or ""
    assert PLAN_ID in reloaded, (
        f"the plan vanished from the plans list after the plan document was "
        f"generated: {reloaded!r}"
    )
    for word in CONFLICT_WORDS:
        assert word not in reloaded, (
            f"the plans list carries the conflict wording {word!r} after the "
            f"generate step: {reloaded!r}"
        )


def test_dashboard_console_has_no_errors(acceptance) -> None:
    """页面全程没有未捕获异常，也没有 console error。

    资源 404 不算：浏览器把「一个资源没找到」也报成 console error，而仪表盘
    会去问几个可有可无的静态资源。除此之外的 error 一律算失败 —— 一个「有错误
    但不影响断言」的页面不是可用的仪表盘。
    """
    report = acceptance["report"]
    reload_report = acceptance["reload_report"]

    for label, pass_report in (("first visit", report), ("second visit", reload_report)):
        assert pass_report.get("page_errors") == [], (
            f"the page raised uncaught JavaScript exceptions on the "
            f"{label}: {pass_report.get('page_errors')!r}"
        )
        assert pass_report.get("console_errors") == [], (
            f"the page logged console errors on the {label}: "
            f"{pass_report.get('console_errors')!r}"
        )


def test_screenshot_archive_written(acceptance) -> None:
    """截图存在、非空。

    它落在 ``/tmp/pdt-accept/screenshots/plans-list.png`` —— 仓库之外：截图
    是这次验收的产物，不是要跟着提交走的东西。
    """
    screenshot = Path(str(acceptance["screenshot"]))

    assert screenshot.is_file(), f"the screenshot was not written: {screenshot}"
    assert screenshot.stat().st_size > 0, f"the screenshot is empty: {screenshot}"

"""真实后端 + 真实浏览器：声明端口被陌生进程占着时的重定位验收。

任务 2-6 在**进程内**与**子进程**层面验收过：就绪判定按声明裁决、重定位
绕开陌生进程、副本就绪且陌生进程还活着。但没有一条测试走完「人在页面上
点验证」这条链。事实 A 的真实形态是这一整条：

    声明端口被陌生进程占着
        → 副本换到另一个端口并就绪
            → 页面给出可读的结果

这一轮走的就是这一整条：**真后端**（``backend.server`` 的真进程）、**真
前端**（后端自己的 ``/{path:path}`` 兜底路由）、**真浏览器**（Playwright
驱动真 Chromium，点真的那个按钮）。

唯一的替身是模型 —— 那是仓库自己留的缝：``PDT_LLM_MODE=scripted`` 把每一
次模型调用换成磁盘上的固定回答。不换的话，Phase 3 的裁决会真的去调模型：
不可复现，还烧钱。

与简报字面写法的三处偏差（都是**系统当前的真实行为**，不是这里放宽了断言；
改它们要改产品代码，不改测试）
--------------------------------------------------------------------------

1. **夹具服务必须服务后端自己的 state.db，并如实报告行数。** 简报写的是
   「自己的 0 行库」。照字面写会失败：本轮的 data-plane 门禁
   （``verification_agent._guard_api_test_data_plane``）拿实例报的
   ``plan_routing_rows`` 与**本轮自己的 fixture 行数**（后端 state.db 里
   那个计划的行，=1）比，不相等就把这个验证点判成 BLOCKED —— artifact 根本
   不会产生。而实例门禁（``_compare_service_instance``）还要比 state.db 的
   inode。所以夹具服务发布的是**后端那一份** state.db 与它的真实行数
   （两个数字都随库变化，天然一致）。
2. **VP-001 的 ✅ 图标来自计划文件里的 ``status`` 字段。** 前端把
   ``verification_plan.json`` 的每个 VP 的 ``status`` 逐字渲染成图标
   （``frontend/app.js`` 的 ``renderVerificationPoints``），生产流程从**不**
   把裁决写回计划文件（那是一份只读的基线）。所以夹具计划里 VP-001 带
   ``"status": "passed"``；本轮自己的裁决另有三条独立证据：徽标、artifact、
   地址事件流。
3. **``project_dir`` 是一个临时的夹具工程，不是仓库 worktree。** 声明的
   ``cwd`` / ``log_path`` 必须落在 ``project_dir`` 里面；worktree 里有
   ``__pycache__``，就绪前的 freshness 检查会**去重建**那个工程
   （``binary_freshness``），60 秒窗口里既慢又危险。临时夹具工程没有
   ``__pycache__``，freshness 直接 SKIPPED。

浏览器二进制从 ``~/Library/Caches/ms-playwright`` 里解析（**不下载**）：
Hermetic 环境把 ``$HOME`` 指向了临时目录，浏览器驱动子进程用
``PDT_TEST_REAL_HOME`` 把 ``$HOME`` 换回真正的家目录才找得到它。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent

#: The interpreter the backend subprocess runs on.  A checkout with no
#: ``backend/.venv`` (fresh worktree, container) falls back to the
#: interpreter already running pytest, so the server really boots.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: The environment variable that names the home this run was *launched*
#: with.  ``install_hermetic_env()`` redirects ``$HOME`` to a scratch
#: directory, so the browser driver has to put the real one back to find
#: the browser binaries.
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"

PLAN_ID = "e2e-svc-relocation"
SERVICE_NAME = "env-probe"
VP_ID = "VP-001"

#: The labels the page shows once the round is over.
FINAL_BADGE_LABELS = {"已通过", "验证失败", "循环停止"}

#: The label the page shows while the round runs.
RUNNING_BADGE_LABEL = "验证中"


# ---------------------------------------------------------------------------
# The fixture service
# ---------------------------------------------------------------------------

_FIXTURE_SERVICE = r'''
"""A tiny service that answers /health and /health/data.

It publishes the **backend's own** state database as the data plane it
serves, and reports the number of routing rows that database holds.
Why not a private database of its own: the round's data-plane gate
compares the instance's row count with the round's own fixture count,
and the instance gate compares the database's inode with the one the
round has open.  A service that published a database of its own would
be refused as "not the round's instance" before the verification point
ever ran -- and the acceptance this file exists for is that the
verification point *does* run.
"""
import json
import os
import sqlite3
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_DB = os.environ.get("PDT_STATE_DB_PATH", "")
PID_DIR = os.environ.get("PDT_FIXTURE_PID_DIR", "")
BOOT_ID = uuid.uuid4().hex


def routing_rows():
    """How many plan_routing rows the state database holds."""
    if not STATE_DB or not os.path.exists(STATE_DB):
        return 0
    for _ in range(5):
        try:
            conn = sqlite3.connect(
                "file:" + STATE_DB + "?mode=ro", uri=True, timeout=5.0,
            )
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM plan_routing"
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception:
            pass
    return 0


def instance():
    """The fact sheet about this process."""
    block = {
        "boot_id": BOOT_ID,
        "pid": os.getpid(),
        "state_db_path": STATE_DB or None,
    }
    try:
        block["state_db_inode"] = int(os.stat(STATE_DB).st_ino)
    except OSError:
        block["state_db_inode"] = None
    return block


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send({"status": "ok"})
        elif path == "/health/data":
            self._send({
                "status": "ok",
                "plan_routing_rows": routing_rows(),
                "instance": instance(),
            })
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1])
    if PID_DIR:
        try:
            os.makedirs(PID_DIR, exist_ok=True)
            with open(
                os.path.join(PID_DIR, "service-%d.pid" % port), "w",
                encoding="utf-8",
            ) as handle:
                handle.write(str(os.getpid()))
        except OSError:
            pass
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
'''


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port the kernel says is free right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _free_port_avoiding(*texts: str) -> int:
    """A free port whose digits appear in none of *texts*.

    The relocation rewrites **every** standalone occurrence of the
    declared port in the start command (``rewrite_port``), so a port
    that also occurs in the state database path would be rewritten
    there too -- and the service would be pointed at a database that
    does not exist.
    """
    for _ in range(200):
        port = _free_port()
        digits = str(port)
        if all(digits not in text for text in texts):
            return port
    raise RuntimeError(
        "could not find a free port that does not occur in the given paths"
    )


def _wait_for_server(base_url: str, timeout: float = 60.0) -> None:
    """Block until ``GET /health`` on *base_url* returns 2xx."""
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last_exc = exc
        time.sleep(0.2)
    raise RuntimeError(
        f"server at {base_url} did not start in {timeout}s; last_exc={last_exc}"
    )


def _start_stranger(port: int) -> subprocess.Popen:
    """A real stranger holding *port* -- and holding it silently.

    A bare listener: it accepts connections and never answers them, so
    nothing about it can be mistaken for a service.  It is a process
    this module started and nobody else knows about, which is exactly
    the situation the ownership rules have to survive.
    """
    return subprocess.Popen(
        [
            sys.executable, "-c",
            "import socket,time;s=socket.socket();"
            "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
            f"s.bind(('127.0.0.1',{port}));s.listen(5);"
            "time.sleep(600)",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )


def _wait_for_a_listener(port: int, proc: subprocess.Popen) -> None:
    """Block until *port* is held and the holder is *proc*."""
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"the stranger died before it could hold port {port} "
                f"(exit {proc.returncode})"
            )
        with socket.socket() as sock:
            sock.settimeout(0.5)
            try:
                sock.connect(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.1)
    raise RuntimeError(f"the stranger never took port {port}")


def _write_scripted_llm(script_dir: Path) -> None:
    """Write the scripted answers the server will read instead of a model.

    The registry maps a **matcher** (a substring of the prompt) to a
    response file.  The rules the double itself enforces: an exact match
    wins, otherwise the most specific fragment wins, and a prompt nothing
    matches is an error (there is no silent fallback -- that is the point
    of the double).

    * ``## 验证计划`` -- the header the Phase 3 judgment prompt starts
      with, so the round gets a well-formed report.
    * ``e`` -- the catch-all.  It is one character long, so every other
      (longer, more specific) matcher beats it; it exists so that a
      prompt nobody scripted is answered with *something* instead of
      raising.
    """
    script_dir.mkdir(parents=True, exist_ok=True)
    registry = {
        "prompts": {
            "judgment": "## 验证计划",
            "e": "e",
        }
    }
    (script_dir / "registry.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (script_dir / "judgment.txt").write_text(
        json.dumps(
            {
                "overall_status": "PASSED",
                "summary": "夹具服务的健康探针在重定位后的端口上返回 200 且 status=ok。",
                "verification_results": [
                    {
                        "id": VP_ID,
                        "status": "PASSED",
                        "actual_result": "GET /health 返回 200，JSON 中 status 为 ok。",
                        "evidence": "见 vp_artifacts/VP-001/api_response.json。",
                    }
                ],
                "requirement_deviations": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (script_dir / "e.txt").write_text(
        json.dumps({"ok": True}, ensure_ascii=False), encoding="utf-8"
    )


def _write_verification_plan(
    plan_dir: Path, declared_port: int, state_db: Path, pid_dir: Path,
) -> None:
    """Write the machine plan: one declared service, one ``api_test`` point.

    The declaration's start command pins the state database the service
    serves -- that pin is what the readiness gate judges the instance
    against, so it names the **backend's** database (see the module
    docstring).
    """
    start_cmd = (
        f"PDT_STATE_DB_PATH={shlex.quote(str(state_db))} "
        f"PDT_FIXTURE_PID_DIR={shlex.quote(str(pid_dir))} "
        f"{shlex.quote(str(VENV_PYTHON))} fixture_service.py "
        f"--port {declared_port}"
    )
    plan = {
        "services": [
            {
                "name": SERVICE_NAME,
                "port": declared_port,
                "start_cmd": start_cmd,
                "cwd": ".",
                "health_url": f"http://127.0.0.1:{declared_port}/health",
                "ready_timeout_seconds": 60,
                "log_path": "tmp/env-probe.log",
                "reap_on_exit": True,
            }
        ],
        "verification_points": [
            {
                "id": VP_ID,
                "title": "重定位后的副本在它自己的端口上回答健康探针",
                "related_prd_criteria": (
                    "声明端口被陌生进程占着时，副本换端口就绪，"
                    "页面给出可读的结果，陌生进程仍然活着"
                ),
                "verification_method": "api_test",
                "verification_phase": 1,
                "priority": "high",
                "expected_result": (
                    "GET {{svc.env-probe.url}}/health 返回 200，"
                    "JSON 中 status 为 ok；该端口不是声明的端口。"
                ),
                # The icon the page draws for this point comes from this
                # field (production never writes verdicts back into the
                # plan file), so the fixture states the expected outcome.
                "status": "passed",
                "uses_services": [SERVICE_NAME],
                "request": {
                    "method": "GET",
                    "url": "{{svc.env-probe.url}}/health",
                    "headers": {"Accept": "application/json"},
                    "body": None,
                    "timeout_seconds": 30,
                },
                "assertions": [
                    {"name": "状态码", "status": 200},
                    {"name": "存活标记", "json_path": "$.status", "equals": "ok"},
                ],
                "timeout_seconds": 120,
                "execution_group": 0,
            }
        ],
    }
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _boot_server(
    work: Path, script_dir: Path, plans_dir: Path, state_db: Path,
) -> Tuple[str, subprocess.Popen]:
    """Boot a real ``backend.server`` subprocess; return ``(base_url, proc)``."""
    port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "PDT_STATE_DB_PATH": str(state_db),
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_HOST": "127.0.0.1",
            "PDT_PORT": str(port),
            "PDT_DEBUG_ROUTES": "1",
            "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
            # "inherit" and not a provider: this server exists to prove
            # the relocation, not to exercise provider routing.
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_PROVIDER_CAPACITY_FILE": str(work / "absent.yaml"),
            # The model is the one thing that is replaced: a real model
            # call would be neither reproducible nor free.
            "PDT_LLM_MODE": "scripted",
            "PDT_LLM_SCRIPT_DIR": str(script_dir),
            "PDT_DISABLE_FALSIFIABILITY_PROBE": "1",
            "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
        }
    )
    env.pop("ANTHROPIC_BASE_URL", None)
    # A throwaway test server has no secret to receive, and a stale
    # descriptor number must never be closed by a process that never
    # owned it (see the sibling E2E file for the full account).
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)

    log_handle = (work / "server.log").open("wb")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "backend.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(base_url, timeout=60.0)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        log_handle.close()
        raise
    proc._pdt_log_handle = log_handle  # type: ignore[attr-defined]
    return base_url, proc


def _teardown_server(proc: Optional[subprocess.Popen]) -> None:
    """Terminate *proc* and close its log handle."""
    if proc is None:
        return
    log_handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass


def _kill_pid_files(pid_dir: Path) -> None:
    """Stop every process that wrote a pid file into *pid_dir*.

    The relocated copies record their pid, so a copy that the round did
    not manage to register in its ledger (a second relocation, whose
    runtime is thrown away) is still stopped here.
    """
    if not pid_dir.is_dir():
        return
    for pid_file in sorted(pid_dir.glob("*.pid")):
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except OSError:
                break
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except OSError:
                break


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

_BROWSER_DRIVER = r'''
"""Drive the served page and report what the badge shows.

The page is driven through its own events (``click``), so the
frontend's own code paths run -- including the ``alert`` it raises when
a start is refused.  A dialog is therefore evidence, not noise.
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


def badge_text(page):
    badge = page.locator("#verification-status-badge")
    return badge.text_content().strip() if badge.count() else ""


def vp_icon(page, vp_id):
    icon = page.locator(
        '.verification-point[data-vp-id="%s"] .verification-point-icon' % vp_id
    )
    return icon.text_content().strip() if icon.count() else ""


url, plan_id, screenshot_path = sys.argv[1:4]

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

report = {
    "page_errors": [],
    "console_errors": [],
    "dialogs": [],
    "browser_executable": executable or "",
}

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

    item = '.sidebar-plan-item[data-plan="%s"]' % plan_id

    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)

    # The sidebar is filled from /api/plans; a plan that appeared a
    # moment ago is not in the first answer.  Reloading is what the
    # sidebar poll does, and it is the only way the cache gets out of
    # the way.
    for _ in range(30):
        if page.locator(item).count():
            break
        page.wait_for_timeout(500)
        page.reload(wait_until="load")
        page.wait_for_timeout(500)
    if not page.locator(item).count():
        raise AssertionError("the plan never appeared in the sidebar")
    report["sidebar_item_visible"] = True

    # The user clicks the plan, then the real "start verification"
    # button.
    page.click(item)
    page.wait_for_selector("#btn-start-verification", state="visible", timeout=30000)
    report["start_button_visible"] = True
    report["badge_before_start"] = badge_text(page)
    page.click("#btn-start-verification")

    # The badge goes to 验证中 while the round runs ...
    page.wait_for_function(
        """() => {
            const badge = document.querySelector('#verification-status-badge');
            return !!badge && badge.textContent.trim() === '验证中';
        }""",
        timeout=60000,
    )
    report["badge_while_running"] = badge_text(page)
    report["vp_icon_while_running"] = vp_icon(page, "VP-001")

    # ... and leaves it when the round is over.
    page.wait_for_function(
        """() => {
            const badge = document.querySelector('#verification-status-badge');
            return !!badge && badge.textContent.trim() !== '验证中';
        }""",
        timeout=60000,
    )
    report["badge_after"] = badge_text(page)
    report["vp_icon_after"] = vp_icon(page, "VP-001")

    page.screenshot(path=screenshot_path, full_page=True)
    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


def _playwright_interpreter() -> Optional[str]:
    """An interpreter that can import ``playwright``, or ``None``.

    Two candidates, in order: the interpreter running pytest, and the
    interpreter behind a ``playwright`` command on ``PATH`` -- the
    command is a Python entry point, so its shebang names an
    interpreter that has the package installed.  Nothing is hard-coded:
    both are read out of the environment.
    """
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
                capture_output=True, timeout=60, check=False,
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


def _screenshot_path() -> Path:
    """Where the final screenshot goes.

    The task names ``/tmp/svc-ready-2/e2e/final.png``; a machine
    without ``/tmp`` gets the platform's temporary directory instead.
    """
    root = Path("/tmp") if os.path.isdir("/tmp") else Path(tempfile.gettempdir())
    return root / "svc-ready-2" / "e2e" / "final.png"


def _run_driver(
    interpreter: str, script: Path, base_url: str, plan_id: str, screenshot: Path,
) -> Dict[str, Any]:
    """Run one browser pass; return the report it printed."""
    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{base_url}/",
            plan_id,
            str(screenshot),
        ],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
        env=_driver_env(),
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}"
        if "Executable doesn't exist" in combined or "playwright install" in combined:
            pytest.fail(
                "the Playwright driver is installed but its browser "
                "binaries are not; this acceptance needs a real browser"
            )
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.timeout(600)
def test_browser_relocates_a_held_port_and_shows_a_result(
    tmp_path: Path, plan_sqlite_seeder,
):
    """The whole chain, in a real browser: hold the port, click, read.

    The declared port is held by a stranger this test started.  The
    round must get out of the way (start a copy on a port of its own),
    the copy must become ready, the page must show a readable result,
    the request must have gone to the copy's port -- and the stranger
    must still be running when the page says so (R4).
    """
    plans_dir = tmp_path / "plans"
    state_db = tmp_path / "state.db"
    project_dir = tmp_path / "project"
    plan_dir = plans_dir / PLAN_ID
    script_dir = tmp_path / "llm"
    pid_dir = project_dir / "tmp" / "pids"

    for directory in (plans_dir, plan_dir, project_dir, pid_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (project_dir / "fixture_service.py").write_text(
        _FIXTURE_SERVICE, encoding="utf-8"
    )

    # The port the declaration names.  Its digits must not occur in any
    # path the start command carries: the relocation rewrites every
    # standalone occurrence of the port there.
    declared_port = _free_port_avoiding(
        str(state_db), str(project_dir), str(plans_dir), str(pid_dir),
    )

    stranger = _start_stranger(declared_port)
    server: Optional[subprocess.Popen] = None
    try:
        _wait_for_a_listener(declared_port, stranger)

        _write_scripted_llm(script_dir)
        _write_verification_plan(plan_dir, declared_port, state_db, pid_dir)
        plan_sqlite_seeder(
            plan_dir,
            PLAN_ID,
            phase="executing",
            project_dir=project_dir,
            verification={"status": "not_started", "round": 0, "max_rounds": 3},
        )

        base_url, server = _boot_server(tmp_path, script_dir, plans_dir, state_db)

        screenshot = _screenshot_path()
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        driver_script = tmp_path / "driver.py"
        driver_script.write_text(_BROWSER_DRIVER, encoding="utf-8")

        interpreter = _playwright_interpreter()
        if interpreter is None:
            # No browser in this environment is a missing capability, not
            # a broken product — and the PR gate never installs one
            # (``ci.yml``'s ``e2e-on-demand`` runs ``uv sync --locked``
            # and nothing else).  Failing here would make the lane red on
            # every machine without a browser, including CI.  The other
            # browser walks in this directory skip with the same wording.
            pytest.skip(
                "playwright is not importable here (install it, or put a "
                "'playwright' command on PATH); the acceptance walk needs a "
                "real browser"
            )
        report = _run_driver(
            interpreter, driver_script, base_url, PLAN_ID, screenshot,
        )

        # --- the page showed a readable result ------------------------
        assert report["badge_while_running"] == RUNNING_BADGE_LABEL, report
        assert report["badge_after"] in FINAL_BADGE_LABELS, report
        assert report["start_button_visible"] is True, report
        assert report["vp_icon_while_running"] == "✅", report
        assert report["vp_icon_after"] == "✅", report
        assert report["page_errors"] == [], report
        assert report["dialogs"] == [], report
        assert screenshot.exists(), (
            f"the acceptance screenshot is missing: {screenshot}"
        )

        # --- the request went to the copy, not to the stranger -------
        # The port the plan declares is read back from the plan file:
        # that file is what the round resolved, so it is the port the
        # request must **not** have gone to.
        plan = json.loads(
            (plan_dir / "verification_plan.json").read_text(encoding="utf-8")
        )
        plan_port = int(plan["services"][0]["port"])
        assert plan_port == declared_port, (
            f"the fixture plan declares port {plan_port} but the stranger "
            f"holds {declared_port}: they must be the same port for this "
            "acceptance to mean anything"
        )
        artifact_path = plan_dir / "vp_artifacts" / VP_ID / "api_response.json"
        assert artifact_path.exists(), (
            f"the verification point produced no artifact: {artifact_path}"
        )
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        request_url = str((artifact.get("request") or {}).get("url") or "")
        assert request_url, artifact
        artifact_port = urllib.parse.urlsplit(request_url).port
        assert artifact_port is not None, request_url
        assert artifact_port != plan_port, (
            f"the request went to the stranger's port {plan_port}: "
            f"{request_url}"
        )

        # --- the address event stream says where the port came from --
        events_path = plan_dir / "logs" / "service_address_events.jsonl"
        assert events_path.exists(), (
            f"the round wrote no address events: {events_path}"
        )
        events = []
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        relocated = [
            event for event in events
            if event.get("event") == "relocated"
            and event.get("old_port") == plan_port
        ]
        assert relocated, (
            "no 'relocated' address event for the declared port "
            f"{plan_port}: {events}"
        )
        assert relocated[0]["new_port"] == artifact_port, (
            f"the relocated port {relocated[0]['new_port']} is not the port "
            f"the request went to ({artifact_port})"
        )

        # --- the stranger is still alive (R4) ------------------------
        assert stranger.poll() is None, (
            "the stranger must still be running: a port held by a process "
            "we cannot positively identify is not ours to kill"
        )
    finally:
        _kill_pid_files(pid_dir)
        _teardown_server(server)
        if stranger.poll() is None:
            stranger.terminate()
            try:
                stranger.wait(timeout=10)
            except subprocess.TimeoutExpired:
                stranger.kill()
                stranger.wait(timeout=5)

"""E2E: the page's 停止执行 button reports every stage of the stop.

背景
----
前 11 个任务把两层停机改成了「并行 + 分层预算」：服务端并行给每个执行器发
信号，执行器并行停掉自己的子进程，每一层都把自己的结果落进 plan 的事件流。
这一份报告最后要能**在页面上看到**——操作员按了「停止」，屏幕上要能读到
「每个子进程停在哪一阶段」和「有没有漏网的进程还在跑」。

这份测试走的就是操作员那条路：

    页面（真实浏览器）→ 选计划 → 开始执行 → 停止执行
        → 断言徽章出现过「已停止」
        → 断言 /stop 响应体里的 stop_report 逐字段齐全
        → 断言没有遗留的 Claude Code 子进程

被证明的东西
------------
* **徽章**：页面在停止之后给过「已停止」。断言落在**徽章被赋予过的所有值**
  上，而不是某一次读取上——停止时已经在飞的轮询会带着它读到的旧状态回来
  （那时 plan 还是 running），把徽章覆写成「已暂停」/「执行中」。所以徽章是
  用 MutationObserver 逐值记录的，断言的是「序列里出现过已停止」。
* **stop_report**：``executor`` / ``children`` / ``leftover_pids`` 三个字段
  齐全，每个子进程都带着自己的 ``stage``（``stdout`` / ``SIGTERM`` /
  ``SIGKILL``），没有「不知道停在哪一步」的子进程。
* **没有遗留**：报告里 ``leftover_pids`` 是空的，而且报告点名的每个子进程
  真的已经不在了（``os.kill(pid, 0)`` 抛 ``ProcessLookupError``）——这是
  「停干净了」这件事唯一能被外部进程证明的形式。

隔离
----
每个测试自己起一个后端进程（自己的 state.db、自己的 plans 树、自己 fake 的
``claude``），``finally`` 里拆干净，所以漏掉一个服务器不会影响下一个测试，
断言失败也不会留下一个监听端口。端口由内核给（``_free_port``），不写死
8000/8001。

浏览器从哪来
------------
浏览器解释器与浏览器环境都取自 ``test_fastfail_no_ghost_state_ui.py`` 的
``_playwright_interpreter()`` / ``_driver_env()``（按文件路径加载，不改动那个
文件）。这个仓库不自带 Playwright 依赖，也不允许测试去装一个——那会把浏览器
下载到开发者的机器上。所以**没有驱动器的机器上这个测试会 skip**：页面上的两
个事实（徽章、截图）没有浏览器就看不到，而断言它们等于断言一个不存在的东西。
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
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

# Prefer the project venv: the backend subprocess this test boots must run on
# the interpreter this suite was installed into.  A checkout with no
# ``backend/.venv`` (fresh worktree, container) falls back to the interpreter
# already running pytest, so the server really boots.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: The plan this file drives.  The date prefix keeps it out of the
#: archived-plan guard (``classify_plan`` reads a ``YYYYMMDD-*`` dirname as the
#: plan's creation date, and anything on or before the 2026-08-05 cutoff is
#: archived and read-only).
PLAN_ID = "20261028-stop-tree-ui-e2e"

#: The one task the seeded plan carries.
TASK_ID = "T1"

#: Every ``/api/*`` request must carry this header
#: (``backend/request_guard.py``); the page's own ``fetch`` sends it.
REQUEST_HEADERS = {
    "X-PDT-Request": "1",
    "Content-Type": "application/json",
}

#: The SIGTERM grace the server gets, in seconds.  The brief names 10 s, and
#: the executor's own budget is derived from it (half of it), so the whole
#: cooperative sequence fits inside the window the server will wait.
STOP_GRACE_SECONDS = "10"

#: The driver's own budget.  It has to stay below the suite's per-test
#: timeout: the server boot and the teardown sit on either side of it.
DRIVER_TIMEOUT_SECONDS = 300

#: The label the frontend shows for a running execution (``frontend/app.js``'s
#: ``setExecBadge``: ``running`` -> 执行中).
RUNNING_BADGE = "执行中"

#: The label the frontend shows for a stopped plan (``stopped`` -> 已停止).
STOPPED_BADGE = "已停止"

#: The stages a child can stop at.  ``already_gone`` is deliberately NOT in
#: this set: a child that was already dead when the stop started was never
#: stopped by it, so it is not evidence that the stop worked.
CHILD_STAGES = {"stdout", "SIGTERM", "SIGKILL"}

#: The stages the *executor* can stop at.
EXECUTOR_STAGES = {"SIGTERM", "SIGKILL", "already_gone"}


# ---------------------------------------------------------------------------
# Read-only reuse of the sibling E2E file
# ---------------------------------------------------------------------------


def _load_reused_helpers():
    """Load ``test_fastfail_no_ghost_state_ui.py`` **without** modifying it.

    Three helpers are reused as they are: ``_free_port()`` (the kernel picks a
    free port), ``_playwright_interpreter()`` (the interpreter that can import
    playwright) and ``_driver_env()`` (the environment the browser driver runs
    under -- ``$HOME`` points back at the home the run was launched with, so
    Playwright finds the browser binaries it installed there).  The file is
    loaded by **path**: a plain cross-module import would depend on pytest's
    rootdir-based ``sys.path`` juggling.
    """
    path = Path(__file__).with_name("test_fastfail_no_ghost_state_ui.py")
    spec = importlib.util.spec_from_file_location(
        "pdt_reused_stop_tree_helpers", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the reused helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REUSED = _load_reused_helpers()

#: Bind an ephemeral loopback port (the kernel picks it).
_free_port = _REUSED._free_port
#: An interpreter that can import ``playwright``, or ``None``.
_playwright_interpreter = _REUSED._playwright_interpreter
#: The environment the browser driver runs under.
_driver_env = _REUSED._driver_env


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _decode(raw: bytes) -> Any:
    """JSON when the body is JSON, the text otherwise."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def _http(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Tuple[int, Any]:
    """One HTTP request against the test server; ``(status, body)``."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in REQUEST_HEADERS.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())


def _wait_for_api(base_url: str, path: str, timeout: float = 60.0) -> int:
    """Poll ``GET base_url + path`` until it answers 200; return the status.

    The backend is up when the API answers, not when the process exists.
    """
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        try:
            status, body = _http("GET", f"{base_url}{path}", timeout=5)
            if status == 200:
                return status
            last = (status, body)
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last = exc
        time.sleep(0.2)
    raise RuntimeError(
        f"{base_url}{path} did not answer 200 within {timeout}s; last={last!r}"
    )


def _wait_for_plan_listed(
    base_url: str,
    plan_id: str,
    timeout: float = 30.0,
) -> List[Dict[str, Any]]:
    """Wait until the served plan listing shows ``plan_id``.

    The server caches the plan listing for two seconds
    (``backend/server.py``'s ``_PLANS_CACHE``).  The health check that waits
    for the server to come up reads the listing *before* the plan is seeded, so
    a listing that is read right after the seed is answered from that cache --
    and answers empty.  The page reads the listing exactly once, so the test
    waits for a listing that knows about the plan before the browser opens it.
    """
    deadline = time.time() + timeout
    body: Any = []
    while True:
        status, body = _http("GET", f"{base_url}/api/plans", timeout=30)
        assert status == 200, (
            f"the plan listing answered {status}: {body!r}"
        )
        plans = body if isinstance(body, list) else (body or {}).get("plans", [])
        if any(plan.get("id") == plan_id for plan in plans):
            return plans
        if time.time() >= deadline:
            raise AssertionError(
                f"the plan listing never showed {plan_id!r} within {timeout}s; "
                f"it answered {body!r}"
            )
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# The fake ``claude``
# ---------------------------------------------------------------------------
#
# The fake is a *child process*, not a stubbed function: the whole point of
# this file is the process chain (server -> executor -> claude), and a function
# call would collapse the two links that matter.
#
# It ignores SIGTERM on purpose.  A real ``claude`` does not ignore SIGTERM,
# but the stop chain has to cope with a child that does, and a fake that died
# politely on SIGTERM would hide exactly the case the stop path is built for.

_FAKE_CLAUDE = r'''#!{interpreter}
"""A fake ``claude`` CLI: it never answers, it just stays alive.

The real CLI is replaced by this one so an end-to-end test can hold a
dispatch open for as long as it needs -- with no model, no provider and
no network.

What it does, in order:

1. reads stdin to EOF (the parent writes the prompt and closes stdin);
2. emits the ``system`` / ``init`` event that carries the session id --
   the id it was handed by ``--session-id`` (a new session) or
   ``--resume`` (an existing one);
3. appends one JSON line to the invocation log -- the record the test
   reads to learn what the dispatch was asked for, and which pid the
   dispatch owns;
4. ignores SIGTERM and keeps its stdout busy, the way the real CLI
   does, until the stop takes its stdout away.

Step 4 is the point.  The stop sequence opens by closing the child's
stdout: a child that never writes again cannot be closed that way, so
the close waits for a read that never returns and the sequence wedges.
A child that keeps its stream busy sees the broken pipe, writes into it
and leaves -- which is what the stop sequence is built around, and what
makes "the stop left no orphan behind" a fact about the chain rather
than about the fake.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path


#: The file every invocation appends one record to.
LOG_PATH = Path({log_path})


def _session_id(argv):
    """The session id the dispatch asked for, or ``""``."""
    for index, arg in enumerate(argv):
        if arg in ("--session-id", "--resume") and index + 1 < len(argv):
            return argv[index + 1]
    return ""


def main():
    argv = sys.argv[1:]
    session_id = _session_id(argv)

    # The parent writes the prompt and closes stdin; reading to EOF is as
    # far along as the dispatch gets before it blocks on the answer.
    try:
        sys.stdin.read()
    except Exception:
        pass

    sys.stdout.write(json.dumps({
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
    }) + "\n")
    sys.stdout.flush()

    record = {
        "argv": argv,
        "session_id": session_id,
        "pid": os.getpid(),
    }
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass

    # A child that outlives its stop is an orphan; ignoring SIGTERM is the
    # hard case the stop chain has to win.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.5)
        try:
            sys.stdout.write(
                json.dumps({"type": "system", "subtype": "keepalive"}) + "\n"
            )
            sys.stdout.flush()
        except Exception:
            # The pipe is gone: the stop reached us.  Leave -- quietly.
            break


if __name__ == "__main__":
    main()
'''


def _install_fake_claude(work: Path) -> Path:
    """Write the fake ``claude`` into ``<work>/fake_bin``; return that dir.

    The directory is what goes on ``PATH`` for the server (and therefore for
    the executor it spawns).  The fake writes its invocation record to
    ``<work>/claude_invocations.jsonl`` -- the path is baked into the script
    -- so the test can find it without passing a path through the environment.
    """
    bin_dir = work / "fake_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    interpreter = sys.executable or "/usr/bin/env python3"
    log_path = work / "claude_invocations.jsonl"
    script = bin_dir / "claude"
    # A plain replacement, not ``str.format``: the fake's own body is full of
    # JSON braces, and ``format`` would read every one of them as a field
    # name.
    script.write_text(
        _FAKE_CLAUDE.replace("{interpreter}", interpreter).replace(
            "{log_path}", json.dumps(str(log_path))
        ),
        encoding="utf-8",
    )
    # The executor runs it as a plain command: it has to be executable.
    script.chmod(0o755)
    return bin_dir


def _invocations(work: Path) -> List[Dict[str, Any]]:
    """Every fake-``claude`` invocation recorded so far.

    One record per line of ``<work>/claude_invocations.jsonl``; an unwritten
    file means "the dispatch never reached the CLI".
    """
    path = work / "claude_invocations.jsonl"
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _pid_alive(pid: int) -> bool:
    """Is ``pid`` still a live process?"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_for_pid_gone(pid: int, timeout: float = 15.0) -> None:
    """Wait until ``os.kill(pid, 0)`` says the pid names no process.

    Polled rather than checked once: a corpse the executor has not reaped yet
    still holds its table entry, so a single check could report an orphan that
    is really a zombie about to be collected.
    """
    deadline = time.time() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if time.time() >= deadline:
            raise AssertionError(
                f"the claude child (pid {pid}) is still alive {timeout}s "
                "after the stop; the stop left an orphan behind"
            )
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# The plan the server sees
# ---------------------------------------------------------------------------


def _make_checkout(work: Path) -> Path:
    """A real git checkout for the plan's task to work on."""
    checkout = work / "checkout"
    (checkout / "backend").mkdir(parents=True, exist_ok=True)
    (checkout / "backend" / "chart.py").write_text("# placeholder\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(checkout), check=False)
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=False)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.email=e2e@example.invalid",
            "-c",
            "user.name=pdt-e2e",
            "commit",
            "-qm",
            "e2e checkout",
        ],
        check=False,
    )
    return checkout


def _seed_plan(work: Path, plan_id: str = PLAN_ID) -> Path:
    """Create the plan directory and the rows the start route needs.

    Two things have to be on disk before ``POST /api/execution/<id>/start``
    can do anything:

    * ``plans/<plan_id>/tasks.json`` -- the route answers 404 without it.
      The one task carries ``test_command: "false"``: the pre-flight runs it,
      it exits non-zero, and the task therefore falls through to the *dispatch*
      path (a pre-flight that exits 0 would mark the task completed and never
      reach the model at all).  ``false`` is also instant, so the test does not
      sit through the pre-flight cap.
    * the state-machine rows -- ``plan_routing`` (the start CASes the phase
      from ``queued``) and ``plan_execution`` (the row the run state is written
      into).

    Returns the plan directory.
    """
    plan_dir = work / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "requirement": "prove the page's stop reports every stage",
                "tasks": [
                    {
                        "id": TASK_ID,
                        "title": "hold the dispatch open",
                        "description": "The fake claude never answers.",
                        "test_command": "false",
                        "files_to_modify": [],
                        "depends_on": [],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(work / "state.db")
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "queued")
        ExecutionRepository(conn).insert(plan_id, exec_status="not_started")
        conn.commit()
    finally:
        conn.close()
    return plan_dir


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


def _boot_server(work: Path, fake_bin: Path) -> Tuple[str, subprocess.Popen]:
    """Boot a real ``backend.server`` subprocess; return ``(base_url, proc)``.

    The environment is the interesting part:

    * ``PATH`` starts with the fake ``claude``'s directory, so the executor's
      dispatch runs the fake and not a real CLI;
    * ``PDT_PROVIDER_MODE=inherit`` -- no CC Switch, no provider chain, no
      credentials.  The dispatch inherits whatever the parent process has,
      which for the fake CLI is nothing at all;
    * ``PDT_STOP_GRACE_SECONDS`` -- the window the server waits for the
      executor to leave, and the window the executor's own budget is derived
      from.
    """
    plans_dir = work / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()

    env = dict(os.environ)
    env.update(
        {
            "PDT_PORT": str(port),
            "PDT_STATE_DB_PATH": str(work / "state.db"),
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_HOST": "127.0.0.1",
            "PDT_DEBUG_ROUTES": "1",
            "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_PROVIDER_CAPACITY_FILE": str(work / "absent.yaml"),
            "PDT_DISABLE_FALSIFIABILITY_PROBE": "1",
            "PDT_STOP_GRACE_SECONDS": STOP_GRACE_SECONDS,
            "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        }
    )
    env.pop("ANTHROPIC_BASE_URL", None)
    # A throwaway test server has no secret to receive, and a stale descriptor
    # number must never be closed by a process that never owned it.
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)

    log_handle = (work / "server.log").open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_api(base_url, "/api/plans", timeout=60.0)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        log_handle.close()
        raise
    proc._pdt_log_handle = log_handle  # type: ignore[attr-defined]
    return base_url, proc


def _kill_leftover_executors(plan_id: str) -> None:
    """Kill an executor that outlived the server (it is found by its id).

    The executor's command line carries the plan id, so the id is enough to
    find it.
    """
    if not plan_id:
        return
    try:
        listing = subprocess.run(
            ["pgrep", "-f", plan_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    for line in listing.stdout.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except OSError:
                break
            time.sleep(0.5)
            if not _pid_alive(pid):
                break


def _kill_fake_claudes(work: Path) -> None:
    """Kill any fake ``claude`` that outlived the stop.

    The fake ignores SIGTERM on purpose, so this has to escalate to SIGKILL.
    The pids come from the invocation record -- the file the fake itself wrote
    -- rather than from a process-table scan, so the test kills exactly the
    children it started.
    """
    for record in _invocations(work):
        pid = record.get("pid")
        if not isinstance(pid, int):
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except OSError:
                break
            time.sleep(0.5)
            if not _pid_alive(pid):
                break


def _teardown(base_url: str, proc: subprocess.Popen, plan_id: str, work: Path) -> None:
    """Leave nothing behind: the executor, the fake children, the server.

    The order is deliberate, and it is not the polite one.  The executor is
    killed FIRST, by its plan id: it is the process that owns the fake
    ``claude`` child, and the server's own shutdown would otherwise stop the
    executions it knows about *politely* -- for as long as the stop grace
    allows.  Killing the executor first keeps the teardown bounded at a couple
    of seconds.

    The stop *endpoint* is not called here.  It is exercised on purpose by the
    test that is about stopping, and a teardown that stopped the plan as well
    would pay the grace a second time for nothing.
    """
    _kill_leftover_executors(plan_id)
    _kill_fake_claudes(work)

    log_handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# The browser pass
# ---------------------------------------------------------------------------
#
# The operator's path is the one the page offers: pick the plan, start it,
# stop it.  The browser runs in its own process because the interpreter that
# runs pytest is not necessarily the one that has Playwright installed; the
# driver script is handed its parameters through ``sys.argv`` and reports what
# it saw as one JSON line on stdout.

_DRIVER = r'''
"""Drive the served page: start the plan, stop it, report what it saw.

Run as::

    python drive_stop_tree.py <url> <plan_id> <checkout> <work> <shots>

Every step is a click on the page, so the frontend's own handlers run.
The driver reports what the page showed; the assertions live in the test
that runs it.
"""
import json
import sys
import time
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


def wait_until(predicate, timeout, message):
    """Block until *predicate* holds; raise when it does not."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(message)


url, plan_id, checkout, work_dir, shots_dir = sys.argv[1:6]

work = Path(work_dir)
shots = Path(shots_dir)
shots.mkdir(parents=True, exist_ok=True)

#: The file the fake claude appends to every time it is invoked.
invocations = work / "claude_invocations.jsonl"


def invocation_count():
    """How many times the fake claude has been invoked so far."""
    if not invocations.exists():
        return 0
    text = invocations.read_text(encoding="utf-8", errors="replace")
    return len([line for line in text.splitlines() if line.strip()])


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

    # The badge is watched from here on.  Every value the badge is given
    # is recorded, so the test can assert on what the operator saw
    # instead of on one reading that a late poll can race: a poll that
    # was already in flight when the stop started can repaint the badge
    # with the status it read (the plan was still running then).
    page.evaluate(
        "() => {"
        " const b = document.querySelector('#execution-status-badge');"
        " window.__badgeValues = [];"
        " const record = (value) => {"
        "   if (value) { window.__badgeValues.push(value); }"
        " };"
        " record(b ? b.textContent.trim() : '');"
        " new MutationObserver((records) => {"
        "   for (const r of records) {"
        "     for (const node of r.addedNodes || []) {"
        "       if (node.textContent) { record(node.textContent.trim()); }"
        "     }"
        "   }"
        " }).observe(b, {childList: true, characterData: true, subtree: true});"
        "}"
    )

    page.wait_for_selector(card, timeout=30000)
    report["plan_card_visible"] = page.is_visible(card)
    page.screenshot(path=str(shots / "1-plan.png"), full_page=True)

    # --- 2: into the task view ----------------------------------------
    page.click(card)
    page.wait_for_selector("#tasks-view:not(.hidden)", timeout=30000)
    page.wait_for_selector("#btn-start-execution", state="visible", timeout=30000)
    report["tasks_view_visible"] = page.is_visible("#tasks-view")
    report["start_button_visible"] = page.is_visible("#btn-start-execution")

    # --- 3: start the execution ---------------------------------------
    page.click("#btn-start-execution")
    page.wait_for_selector("#btn-confirm-execute", state="visible", timeout=15000)
    page.fill("#project-dir-input", checkout)
    page.click("#btn-confirm-execute")
    wait_until(
        lambda: invocation_count() >= 1,
        180,
        "the first dispatch never reached the claude CLI",
    )
    report["first_invocation_seen"] = True
    page.wait_for_selector("#btn-stop-execution", state="visible", timeout=30000)
    page.screenshot(path=str(shots / "2-running.png"), full_page=True)

    # --- 4: stop it ----------------------------------------------------
    # The response of the stop is captured HERE, in the moment the click
    # made it: it is the only place the report exists.
    with page.expect_response(
        lambda r: r.url.endswith("/api/execution/%s/stop" % plan_id),
        timeout=180000,
    ) as stop_info:
        page.click("#btn-stop-execution")
    response = stop_info.value
    report["stop_status"] = response.status
    try:
        body = response.json()
    except Exception as exc:
        report["stop_body_text"] = response.text()
        report["stop_body_error"] = str(exc)
    else:
        report["stop_body"] = body
        # The report is the fifth key of the stop response; the four
        # original keys are the stop-is-a-pause contract and are not the
        # report.
        report["stop_report"] = body.get("stop_report") if isinstance(body, dict) else None

    page.wait_for_function(
        "() => (window.__badgeValues || []).indexOf('已停止') !== -1",
        timeout=60000,
    )
    report["badge_values"] = page.evaluate("() => window.__badgeValues || []")
    report["badge_after_stop"] = page.evaluate(
        "() => { const b = document.querySelector('#execution-status-badge');"
        " return b ? b.textContent.trim() : ''; }"
    )
    page.screenshot(path=str(shots / "3-stopped.png"), full_page=True)

    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


class BrowserUnavailable(RuntimeError):
    """The browser pass could not run here: no driver, or no browser.

    Raised only for the two cases where *this machine* cannot drive a
    page -- the driver is not installed, or its browser binaries are not.
    """


def _run_driver(
    interpreter: str,
    script: Path,
    base_url: str,
    plan_id: str,
    checkout: Path,
    work: Path,
    shots: Path,
) -> Dict[str, Any]:
    """Run the browser pass; return the report it printed."""
    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{base_url}/",
            plan_id,
            str(checkout),
            str(work),
            str(shots),
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
            raise BrowserUnavailable(combined)
        raise AssertionError(
            f"the browser driver failed (exit {result.returncode}); "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_stop_from_the_page_reports_each_stage(tmp_path):
    """The operator stops the plan on the page; every stage is reported.

    The chain is real from end to end: the server spawns the executor, the
    executor spawns a ``claude`` child, and the child never answers.  The
    operator then does what an operator does -- picks the plan on the served
    page, starts it, and stops it.

    What is asserted:

    * the operator got to the plan (the card is on the page, clicking it
      opens the task view, and 开始执行 is there);
    * the dispatch really happened (the fake ``claude`` was invoked);
    * the page's badge was given 已停止 after the stop -- the assertion is
      on the values the badge was *given*, not on one reading of it, because
      a poll that was in flight when the stop started can repaint it with
      the status it read;
    * the stop response carries the report, with all three of its parts;
    * every child the report names stopped at a stage the stop can reach
      (``stdout`` / ``SIGTERM`` / ``SIGKILL``) and carries its own pid and
      ``forced`` flag;
    * nothing was left behind: ``leftover_pids`` is empty, and every pid the
      report names is gone from the process table
      (``os.kill(pid, 0)`` raises ``ProcessLookupError``);
    * the child the report names is the ``claude`` the dispatch started;
    * the screenshots of the walk are on disk and are not empty.
    """
    work = tmp_path
    fake_bin = _install_fake_claude(work)
    base_url, proc = _boot_server(work, fake_bin)
    try:
        _seed_plan(work, PLAN_ID)
        _wait_for_plan_listed(base_url, PLAN_ID)
        checkout = _make_checkout(work)

        interpreter = _playwright_interpreter()
        if interpreter is None:
            pytest.skip(
                "no Playwright driver on this machine; the badge and the "
                "screenshots are facts only a browser can show, and this "
                "repository does not install a browser driver for its tests"
            )

        script = work / "drive_stop_tree.py"
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
            "clicking the plan card did not open the task view; the "
            f"operator never reached 开始执行. report={report!r}"
        )
        assert report.get("start_button_visible") is True, (
            f"the 开始执行 button is not on the page. report={report!r}"
        )
        assert report.get("first_invocation_seen") is True, (
            "the dispatch never reached the claude CLI, so there was "
            f"nothing to stop. report={report!r}"
        )

        # --- the stop answered, and it answered with the report --------
        assert report.get("stop_status") == 200, (
            f"the stop answered {report.get('stop_status')!r}; "
            f"body={report.get('stop_body_text')!r}"
        )
        stop_report = report.get("stop_report")
        assert isinstance(stop_report, dict), (
            "the stop response carries no stop report; it answered "
            f"{stop_report!r} (body={report.get('stop_body')!r})"
        )
        assert set(stop_report) == {"executor", "children", "leftover_pids"}, (
            f"the stop report does not carry its three parts: {stop_report!r}"
        )

        # The report is an ADDITION: the four keys the stop has always
        # answered are still there, unchanged.  A report that replaced
        # them would have loosened the stop-is-a-pause contract.
        stop_body = report.get("stop_body")
        assert isinstance(stop_body, dict), (
            f"the stop response is not an object: {stop_body!r}"
        )
        for key in ("status", "paused", "paused_at", "interrupted_task_count"):
            assert key in stop_body, (
                f"the stop response lost {key!r}: {stop_body!r}"
            )
        assert stop_body["status"] == "stopped", stop_body

        # --- the executor's own stage ----------------------------------
        executor = stop_report["executor"]
        assert isinstance(executor, dict), (
            f"the report's executor is not an object: {executor!r}"
        )
        assert set(executor) == {"pid", "stage", "hard_killed"}, (
            f"the executor's verdict does not carry its three fields: "
            f"{executor!r}"
        )
        assert executor["stage"] in EXECUTOR_STAGES, (
            f"the executor stopped at an unknown stage: {executor!r}"
        )
        assert isinstance(executor["hard_killed"], bool), (
            f"the executor's hard-kill flag is not a bool: {executor!r}"
        )
        assert isinstance(executor["pid"], int) and executor["pid"] > 0, (
            f"the executor's verdict names no pid: {executor!r}"
        )

        # --- one entry per child, each with its own stage ---------------
        children = stop_report["children"]
        assert isinstance(children, list), (
            f"the report's children is not a list: {children!r}"
        )
        assert children, (
            "the stop report names no child at all; the dispatch had a "
            f"claude child running, so the stop had something to stop. "
            f"report={stop_report!r}"
        )
        for child in children:
            assert set(child) == {"pid", "stage", "forced"}, (
                f"a child's entry does not carry its three fields: {child!r}"
            )
            assert isinstance(child["pid"], int) and child["pid"] > 0, (
                f"a child's entry names no pid: {child!r}"
            )
            assert child["stage"] in CHILD_STAGES, (
                f"a child stopped at an unknown stage: {child!r}; the stages "
                f"the stop can reach are {sorted(CHILD_STAGES)}"
            )
            assert isinstance(child["forced"], bool), (
                f"a child's forced flag is not a bool: {child!r}"
            )

        # --- nothing was left behind -----------------------------------
        assert stop_report["leftover_pids"] == [], (
            "the stop left processes behind; the report names them: "
            f"{stop_report['leftover_pids']!r}"
        )

        # --- the children the report names are really gone --------------
        for child in children:
            pid = child["pid"]
            _wait_for_pid_gone(pid)
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)

        # --- and they are the claude the dispatch started ----------------
        invocations = _invocations(work)
        assert invocations, (
            "the executor never invoked the claude CLI; the dispatch did "
            f"not happen. invocations={invocations!r}"
        )
        started_pids = {record.get("pid") for record in invocations}
        reported_pids = {child["pid"] for child in children}
        assert started_pids & reported_pids, (
            "the report names no pid the dispatch started; the report is "
            f"about {reported_pids!r} but the dispatch started {started_pids!r}"
        )

        # --- the badge the operator read --------------------------------
        badge_values = report.get("badge_values") or []
        assert STOPPED_BADGE in badge_values, (
            f"the execution badge never read {STOPPED_BADGE!r} after the "
            f"stop; the page showed {badge_values!r} "
            f"(it reads {report.get('badge_after_stop')!r} now)"
        )
        assert RUNNING_BADGE in badge_values, (
            f"the execution badge never read {RUNNING_BADGE!r} while the "
            f"plan was running; the page showed {badge_values!r}"
        )

        # --- the page did not blow up on the way -------------------------
        assert report.get("page_errors") == [], (
            f"the page raised errors during the walk: {report['page_errors']!r}"
        )
        assert report.get("dialogs") == [], (
            f"the page showed dialogs during the walk: {report['dialogs']!r}"
        )

        # --- the screenshots of the walk are on disk ----------------------
        for name in ("1-plan.png", "2-running.png", "3-stopped.png"):
            shot = shots / name
            assert shot.exists() and shot.stat().st_size > 0, (
                f"the screenshot {name} was not written to {shots}"
            )

        # The evidence, in one line: what the operator's badge was given,
        # and what the stop answered.  An end-to-end walk is worth reading
        # in the log -- the report is the whole point of the file.
        print(
            "[stop-tree-ui-e2e] badges={0!r} stop_report={1!r}".format(
                badge_values, stop_report
            )
        )
    finally:
        _teardown(base_url, proc, PLAN_ID, work)
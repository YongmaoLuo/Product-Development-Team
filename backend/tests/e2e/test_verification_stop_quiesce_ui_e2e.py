"""The stop chain, end to end, through the page the operator drives.

Why this file exists
--------------------
Everything the stop chain does was, until now, proven one layer at a
time.  The backend answers ``stopping`` and parks the plan once the
plan is quiet (the backend's own tests say so), and the frontend has a
label for every status it knows (the frontend's own gates say so).
Neither of those is the thing an operator actually sees.

The operator clicks 「停止执行」 and then *watches the badge*.  The
badge used to say 「已停止」 the moment the request came back — while the
plan was still being taken down, and while the backend was still
answering ``status: "stopping"``.  A badge that says 「已停止」 while
the work is still running is the same lie the endpoint used to tell,
one layer up: the operator stops looking.

So this file drives the whole chain in one browser:

* the plan is started **through the page** (the confirm button, the
  project directory box) — not through the API, because the panel only
  exists after a real start;
* the stop is **clicked on the page**, and the badge is read while the
  plan is stopping;
* the badge is read again once the plan is really down.

Three facts are pinned:

1. the badge says 「正在停止」 while the backend says ``stopping`` —
   and it says it *fast*: the operator clicked a second ago;
2. the backend's answer carries the ``survivors`` while the plan is
   stopping;
3. the badge says 「已停止」 only once the plan is really down.

The page is driven by a real Chromium against a real backend
subprocess, so nothing here is a stub: the badge the test reads is the
badge the operator reads.

How the run is made slow on purpose
-----------------------------------
A stop that ends instantly proves nothing about the window in which
the plan is stopping.  So the executor is a stand-in
(``PDT_EXECUTOR_CMD``) that writes its pid down, ignores stdin and
sleeps: the plan really has a running process, and the stop really has
something to take down.  The quiet window the supervisor waits for is
widened (``PDT_STOP_QUIET_SECONDS``) so that the checks inside the
window — the HTTP status, the re-opened plan — land inside it instead
of racing it.

The executor's command line is an override, so nothing in the backend
knows this file exists: the chain under test is the production chain.

The two static cases live here too
----------------------------------
The last two cases in this file do not drive a browser.  They read the
frontend's source and the static gate that pins it, because those two
facts are what the browser case *rests on*: a label table that has no
``stopping`` entry, and a frozen hash that no longer matches the file,
would make the browser case pass for the wrong reason (the badge would
show the raw English word, and the gate would be red for a reason that
has nothing to do with the stop chain).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest


pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent

#: Prefer the project venv: the backend subprocess this file boots must run
#: on the interpreter this suite was installed into.  A checkout with no
#: ``backend/.venv`` falls back to the interpreter already running pytest.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: Every ``/api/*`` request must carry this header
#: (``backend/request_guard.py``).
REQUEST_HEADERS = {
    "X-PDT-Request": "1",
    "Content-Type": "application/json",
}

#: The plan this file drives.  The date prefix keeps it out of the
#: archived-plan guard (``classify_plan`` reads a ``YYYYMMDD-*`` dirname
#: as the plan's creation date, and anything on or before the 2026-08-05
#: cutoff is archived and read-only).  The pid and the random suffix
#: keep two copies of this file apart.
PLAN_ID = f"20261121-stop-quiesce-{os.getpid()}-{uuid.uuid4().hex[:8]}"

#: The badge the page shows while the plan is being taken down.
STOPPING_BADGE = "正在停止"

#: The badge the page shows once the plan is really down.
STOPPED_BADGE = "已停止"

#: The badge the page shows while the plan is running.
RUNNING_BADGE = "执行中"

#: The quiet window the stop's supervisor waits for, in seconds.
#:
#: The window is the whole point of the chain: the phase may only be
#: flipped after the plan has really fallen silent.  The default (5 s)
#: is wide enough for the chain but too narrow for the checks this file
#: makes *inside* the window — the HTTP status read and the re-opened
#: plan — to land in it on a loaded machine.  12 s buys that room
#: without making the case slow: the phase flips ~12 s after the stop,
#: and the frontend polls every 2 s.
QUIET_SECONDS = "12"

#: The SIGTERM grace the server gets, in seconds.
STOP_GRACE_SECONDS = "20"

#: The frontend file this file reads, and the gate that pins it.
APP_JS = PROJECT_ROOT / "frontend" / "app.js"
FROZEN_GATE = (
    PROJECT_ROOT
    / "backend"
    / "tests"
    / "static_gates"
    / "test_verification_mode_scope_gates.py"
)

#: Where the screenshots of the walk are written.  A runtime output, not
#: a fixture: nothing imports it, and the plan id in its name changes
#: on every run (which is why the directory is gitignored).
ARTIFACTS_ROOT = PROJECT_ROOT / "backend" / "tests" / "e2e" / "artifacts"


# ---------------------------------------------------------------------------
# Read-only reuse of the sibling E2E file
# ---------------------------------------------------------------------------


def _load_reused_helpers():
    """Load ``test_fastfail_no_ghost_state_ui.py`` **without** modifying it.

    Two helpers are reused as they are: ``_free_port()`` (the kernel
    picks a free port) and ``_playwright_interpreter()`` (the
    interpreter that can import playwright).  ``backend/tests/e2e/`` has
    no ``__init__.py``, so a plain cross-module import would depend on
    pytest's rootdir-based ``sys.path`` juggling; loading by **file
    path** avoids that without copying a single line of it.
    """
    path = Path(__file__).with_name("test_fastfail_no_ghost_state_ui.py")
    spec = importlib.util.spec_from_file_location(
        "pdt_reused_stop_quiesce_helpers", path,
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
#: The environment the browser driver runs under (``$HOME`` points back at
#: the home the run was launched with, so Playwright finds its browsers).
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
    """Poll ``GET base_url + path`` until it answers 200; return the status."""
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
    (``backend/server.py``'s ``_PLANS_CACHE``), and the page reads the
    listing exactly once — so the browser must not open the page before
    a listing that knows about the plan exists.
    """
    deadline = time.time() + timeout
    body: Any = []
    while True:
        status, body = _http("GET", f"{base_url}/api/plans", timeout=30)
        assert status == 200, f"the plan listing answered {status}: {body!r}"
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
# The stand-in executor
# ---------------------------------------------------------------------------

_SLOW_EXECUTOR = '''#!/usr/bin/env python3
"""A stand-in for the executor: it stays alive and does nothing.

The stop chain this file drives needs a run that is *really* running,
so that stopping it is a process and not an instant.  This script is
that run: it writes its pid down (so the test can prove the process
existed), ignores stdin, and sleeps long enough for the stop to be the
thing that ends it.
"""
import os
import sys
import time
from pathlib import Path

PID_FILE = Path(__file__).with_name("slow_executor.pid")


def main() -> int:
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    # stdin is never read: the stand-in has nothing to say to it.
    time.sleep(60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _write_slow_executor(work: Path) -> Path:
    """Write the stand-in executor into ``work``; return its path."""
    script = work / "slow_executor.py"
    script.write_text(_SLOW_EXECUTOR, encoding="utf-8")
    script.chmod(0o755)
    return script


# ---------------------------------------------------------------------------
# The plan and the server
# ---------------------------------------------------------------------------


def _seed_plan(work: Path, plan_id: str = PLAN_ID) -> Path:
    """Create the plan directory and the rows the start route needs.

    Two things have to be on disk before ``POST /api/execution/<id>/start``
    can do anything:

    * ``plans/<plan_id>/tasks.json`` -- the route answers 404 without it;
    * the state-machine rows -- ``plan_routing`` (the start CASes the
      phase from ``queued``) and ``plan_execution`` (the row the run
      state is written into).

    Returns the plan directory.
    """
    plan_dir = work / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "requirement": "prove the badge follows the stop chain",
                "tasks": [
                    {
                        "id": "T1",
                        "title": "hold the run open",
                        "description": "The stand-in executor never answers.",
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


def _boot_server(work: Path, executor_cmd: str) -> Tuple[str, subprocess.Popen]:
    """Boot a real ``backend.server`` subprocess; return ``(base_url, proc)``.

    The environment is the interesting part:

    * ``PDT_EXECUTOR_CMD`` names the *whole* executor command line, and
      it is used verbatim -- so the run this test stops is the
      stand-in, and the backend needs no knowledge of this file;
    * ``PDT_STOP_QUIET_SECONDS`` widens the quiet window the stop's
      supervisor waits for, so the checks this file makes *inside* the
      window land inside it;
    * ``PDT_ORPHAN_REAPER=0`` keeps the reaper out of the way: this
      file cleans up after itself;
    * ``PDT_PROVIDER_MODE=inherit`` -- no CC Switch, no provider chain,
      no credentials: the dispatch inherits whatever the parent process
      has, which for the stand-in is nothing at all.
    """
    plans_dir = work / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()

    env = dict(os.environ)
    env.update(
        {
            "PDT_STATE_DB_PATH": str(work / "state.db"),
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_HOST": "127.0.0.1",
            "PDT_PORT": str(port),
            "PDT_DEBUG_ROUTES": "1",
            "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_PROVIDER_CAPACITY_FILE": str(work / "absent.yaml"),
            "PDT_DISABLE_FALSIFIABILITY_PROBE": "1",
            "PDT_EXECUTOR_CMD": executor_cmd,
            "PDT_ORPHAN_REAPER": "0",
            "PDT_STOP_QUIET_SECONDS": QUIET_SECONDS,
            "PDT_STOP_GRACE_SECONDS": STOP_GRACE_SECONDS,
            "PYTHONPATH": (
                f"{BACKEND_DIR}{os.pathsep}{PROJECT_ROOT}"
                f"{os.pathsep}{env.get('PYTHONPATH', '')}"
            ),
        }
    )
    env.pop("ANTHROPIC_BASE_URL", None)
    # A throwaway test server has no secret to receive, and a stale
    # descriptor number must never be closed by a process that never
    # owned it.
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)

    log_handle = (work / "server.log").open("wb")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "server.py"],
        cwd=str(BACKEND_DIR),
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


def _kill_stand_in_executor(work: Path) -> None:
    """Kill the stand-in executor, if it outlived the server.

    The pid comes from the file the stand-in wrote down — a pid is not
    an identity, so the file is read and the process is checked before
    anything is signalled.
    """
    pid_file = work / "slow_executor.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return
    if pid <= 1:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _teardown(proc: subprocess.Popen, work: Path) -> None:
    """Leave nothing behind: the stand-in executor, then the server."""
    _kill_stand_in_executor(work)

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
# The browser driver
# ---------------------------------------------------------------------------

#: The driver's own budget.  It has to stay below the suite's per-test
#: timeout, because the server boot and the teardown sit on either side
#: of it.
_DRIVER_TIMEOUT_SECONDS = 240

_STOP_QUIESCE_DRIVER = r'''
"""Drive the served page through the whole stop chain.

Run as::

    python drive_stop_quiesce.py <url> <plan_id> <project_dir> <artifacts>

Every step is a click on the page, so the frontend's own handlers run.
The driver reports what the page showed; the assertions live in the
test that runs it.
"""
import json
import sys
import time
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


url, plan_id, project_dir, artifacts_dir = sys.argv[1:5]
artifacts = Path(artifacts_dir)
artifacts.mkdir(parents=True, exist_ok=True)

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

report = {
    "page_errors": [],
    "console_errors": [],
    "dialogs": [],
    "badge_values": [],
}

with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page(viewport={"width": 1280, "height": 900})

    # An unhandled promise rejection is a page-level failure, and the
    # acceptance criteria count it as one.  The listener is installed
    # before the first script of the page runs, so nothing is missed.
    page.add_init_script(
        "window.__unhandledRejections = [];"
        "window.addEventListener('unhandledrejection', function (event) {"
        "  var reason = event.reason;"
        "  var text = (reason && reason.stack) ? reason.stack : String(reason);"
        "  window.__unhandledRejections.push(text);"
        "});"
    )

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
    sidebar_item = '.sidebar-plan-item[data-plan="%s"]' % plan_id

    # --- 1: the plan list ---------------------------------------------
    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)
    page.wait_for_selector(card, timeout=30000)
    report["plan_card_visible"] = page.is_visible(card)

    # The operator names the plan in the scheduler's plan box.  That box
    # lives in the scheduler view, so the view is opened first and left
    # again through the button that goes back -- the same two clicks an
    # operator makes.
    page.click("#btn-open-scheduler")
    page.wait_for_selector("#scheduler-spec-plan", state="visible", timeout=15000)
    page.fill("#scheduler-spec-plan", plan_id)
    report["typed_plan_id"] = page.input_value("#scheduler-spec-plan")
    page.click("#btn-scheduler-back-home")
    page.wait_for_selector("#plans-view:not(.hidden)", timeout=15000)
    page.wait_for_selector(card, timeout=15000)

    # --- 2: into the task view ----------------------------------------
    page.click(card)
    page.wait_for_selector("#tasks-view:not(.hidden)", timeout=30000)
    report["tasks_view_visible"] = page.is_visible("#tasks-view")

    # --- 3: really start the execution --------------------------------
    # The execution panel only exists after a real start: the setup row
    # carries the project directory box and the confirm button, and the
    # controls row (with the stop button) is hidden until the run is
    # under way.
    page.click("#btn-start-execution")
    page.wait_for_selector("#btn-confirm-execute", state="visible", timeout=15000)
    page.fill("#project-dir-input", project_dir)
    page.click("#btn-confirm-execute")
    page.wait_for_selector("#execution-controls:not(.hidden)", timeout=30000)
    page.wait_for_function(
        "() => document.querySelector('#execution-status-badge')"
        ".textContent.includes('执行中')",
        timeout=30000,
    )
    report["running_badge"] = page.inner_text("#execution-status-badge")

    # --- 4: watch the badge from here on ------------------------------
    # Every value the badge is given is recorded, so the test can assert
    # on what the operator saw -- and on the ORDER they saw it in.  A
    # poll that was already in flight when the operator clicked stop can
    # repaint the badge afterwards, and only the history shows that.
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

    # --- 5: stop it ----------------------------------------------------
    clicked_at = time.time()
    page.click("#btn-stop-execution")
    # The badge has to say 「正在停止」 FAST: the operator clicked a
    # second ago, and a badge that says 「已停止」 now is the lie this
    # whole chain is about.
    try:
        page.wait_for_function(
            "() => document.querySelector('#execution-status-badge')"
            ".textContent.includes('正在停止')",
            timeout=2000,
        )
    except Exception:
        # The badge never said it.  Report what it DID say, and every
        # value it took, so the failure names the lie instead of only
        # the timeout.
        report["badge_when_stopping_never_appeared"] = page.inner_text(
            "#execution-status-badge"
        )
        report["badge_values"] = page.evaluate("() => window.__badgeValues || []")
        print(json.dumps(report, ensure_ascii=False))
        raise
    report["stopping_entered_seconds"] = time.time() - clicked_at
    report["badge_while_stopping"] = page.inner_text("#execution-status-badge")
    page.screenshot(path=str(artifacts / "01-stopping.png"), full_page=True)

    # --- 6: what the backend says while the plan is stopping ----------
    report["stopping_status"] = page.evaluate(
        "async (pid) => {"
        "  const res = await fetch('/api/execution/' + encodeURIComponent(pid) + '/status',"
        "    {headers: {'X-PDT-Request': '1', 'Content-Type': 'application/json'}});"
        "  return await res.json();"
        "}",
        plan_id,
    )

    # --- 7: a stopping plan still opens its task view -----------------
    # The plan is mid-stop: the phase says so, and the page must still
    # be able to open the plan rather than falling back to the
    # interview screen.
    page.wait_for_selector(sidebar_item, timeout=15000)
    page.click(sidebar_item)
    page.wait_for_timeout(1500)
    report["tasks_view_visible_while_stopping"] = page.is_visible("#tasks-view")
    report["interview_view_visible_while_stopping"] = page.is_visible("#interview-view")
    report["badge_after_reopen"] = page.inner_text("#execution-status-badge")

    # --- 8: the terminal state ----------------------------------------
    page.wait_for_function(
        "() => document.querySelector('#execution-status-badge')"
        ".textContent.includes('已停止')",
        timeout=90000,
    )
    report["terminal_seconds"] = time.time() - clicked_at
    report["final_badge"] = page.inner_text("#execution-status-badge")
    report["badge_values"] = page.evaluate("() => window.__badgeValues || []")
    page.screenshot(path=str(artifacts / "02-stopped.png"), full_page=True)

    report["unhandled_rejections"] = page.evaluate(
        "() => window.__unhandledRejections || []"
    )

    browser.close()

print(json.dumps(report, ensure_ascii=False))
'''


class BrowserUnavailable(RuntimeError):
    """The browser pass could not run here: no driver, or no browser.

    Raised only for the two cases where *this machine* cannot drive a
    page -- the driver is not installed, or its browser binaries are
    not.  It is not a failure of the chain under test.
    """


def _run_driver(
    interpreter: str,
    script: Path,
    base_url: str,
    plan_id: str,
    project_dir: Path,
    artifacts: Path,
) -> Dict[str, Any]:
    """Run the browser pass; return the report it printed."""
    result = subprocess.run(
        [
            interpreter,
            str(script),
            f"{base_url}/",
            plan_id,
            str(project_dir),
            str(artifacts),
        ],
        capture_output=True,
        text=True,
        timeout=_DRIVER_TIMEOUT_SECONDS,
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
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise AssertionError(
            f"the browser driver printed no report: {exc!r}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        ) from exc


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stop_chain(tmp_path_factory) -> Dict[str, Any]:
    """Boot a server, drive the page through the stop chain, tear it down.

    The walk is done ONCE and its report is shared: the three cases
    below are three readings of the same chain (the badge, the backend's
    answer, the page's health), and driving the chain three times would
    prove the same thing three times over.
    """
    work = tmp_path_factory.mktemp("stop-quiesce")
    artifacts = ARTIFACTS_ROOT / PLAN_ID
    artifacts.mkdir(parents=True, exist_ok=True)

    executor_script = _write_slow_executor(work)
    project_dir = work / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    _seed_plan(work)
    executor_cmd = f"{VENV_PYTHON} {executor_script}"
    base_url, proc = _boot_server(work, executor_cmd)

    interpreter = _playwright_interpreter()
    if interpreter is None:
        _teardown(proc, work)
        pytest.skip("no interpreter can import playwright on this machine")

    script = work / "drive_stop_quiesce.py"
    script.write_text(_STOP_QUIESCE_DRIVER, encoding="utf-8")

    try:
        _wait_for_plan_listed(base_url, PLAN_ID, timeout=30.0)
        report = _run_driver(
            interpreter, script, base_url, PLAN_ID, project_dir, artifacts,
        )
    except BrowserUnavailable as exc:
        pytest.skip(f"no browser on this machine: {exc}")
    finally:
        _teardown(proc, work)

    return {
        "report": report,
        "artifacts": artifacts,
        "work": work,
    }


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_stop_shows_stopping_then_stopped_e2e(stop_chain) -> None:
    """The badge says 「正在停止」 first and 「已停止」 only at the end.

    The operator's screen is the only place where the two can be told
    apart, and the order matters as much as the words: a badge that
    said 「已停止」 first and 「正在停止」 afterwards would be just as
    wrong as one that never said 「正在停止」 at all.
    """
    report = stop_chain["report"]
    values = list(report.get("badge_values") or [])

    # The run was real before the stop: the badge said 执行中, so the
    # stop had something to stop.
    assert report.get("running_badge") == RUNNING_BADGE, (
        "the plan never showed 「执行中」 before the stop; it showed "
        f"{report.get('running_badge')!r}"
    )

    assert STOPPING_BADGE in values, (
        "the badge never said 「正在停止」; the operator watched a stop "
        f"that was still in progress and saw {values!r} instead"
    )
    assert STOPPED_BADGE in values, (
        f"the badge never said 「已停止」; it ended at {values!r}"
    )

    stopping_at = values.index(STOPPING_BADGE)
    stopped_at = values.index(STOPPED_BADGE)
    assert stopped_at > stopping_at, (
        "the badge said 「已停止」 before it said 「正在停止」 — the "
        f"order the operator saw was {values!r}"
    )

    # The badge has to say 「正在停止」 while the plan is stopping, not
    # merely at some point during the walk.
    assert report.get("badge_while_stopping") == STOPPING_BADGE, (
        "the badge did not read 「正在停止」 while the plan was stopping; "
        f"it read {report.get('badge_while_stopping')!r}"
    )

    # And it has to say it fast: the operator clicked a second ago.
    entered = float(report.get("stopping_entered_seconds") or 0.0)
    assert entered <= 2.0, (
        f"the badge took {entered:.3f}s to say 「正在停止」; the operator "
        "clicked the stop button a moment ago"
    )

    # The terminal state has to arrive -- and within the window the
    # chain allows for it.
    terminal = float(report.get("terminal_seconds") or 0.0)
    assert terminal <= 90.0, (
        f"the plan took {terminal:.1f}s to really stop; the chain allows 90s"
    )
    assert report.get("final_badge") == STOPPED_BADGE, (
        "the badge did not end at 「已停止」; it ended at "
        f"{report.get('final_badge')!r}"
    )

    # The screenshots of what the operator saw are on disk.
    for name in ("01-stopping.png", "02-stopped.png"):
        shot = stop_chain["artifacts"] / name
        assert shot.exists(), f"the screenshot {name} was not written to {shot}"
        assert shot.stat().st_size > 0, f"the screenshot {name} is empty"


def test_stopping_window_reports_survivors(stop_chain) -> None:
    """While the plan is stopping, the backend says so and names the work.

    Two things have to be true at once: the answer says ``stopping``
    (not ``stopped``), and it carries the ``survivors`` — the processes
    that were still running when the reader asked.  A plan that is
    stopping but cannot say what is still alive is a plan the operator
    cannot act on.
    """
    payload = stop_chain["report"].get("stopping_status")
    assert isinstance(payload, dict), (
        f"the status answer during the stopping window was not an object: "
        f"{payload!r}"
    )
    assert payload.get("status") == "stopping", (
        "the status answer during the stopping window did not say "
        f"'stopping'; it said {payload.get('status')!r}"
    )
    assert "survivors" in payload, (
        "the status answer during the stopping window carried no "
        f"'survivors'; it carried {sorted(payload)!r}"
    )
    assert isinstance(payload["survivors"], list), (
        "the 'survivors' of the stopping answer was not a list; it was "
        f"{type(payload['survivors']).__name__}"
    )


def test_no_js_errors_during_stop(stop_chain) -> None:
    """The page raised nothing while the plan was being stopped.

    An uncaught exception or an unhandled promise rejection on the page
    is a page-level failure: the operator sees a page that half works.
    """
    report = stop_chain["report"]
    assert report.get("page_errors") == [], (
        "the page raised an uncaught exception during the stop: "
        f"{report.get('page_errors')!r}"
    )
    assert report.get("unhandled_rejections") == [], (
        "the page left an unhandled promise rejection during the stop: "
        f"{report.get('unhandled_rejections')!r}"
    )


def test_badge_labels_cover_stopping() -> None:
    """Both badge label tables carry a Chinese 「正在停止」.

    Without the entry the page renders the raw English word
    ``stopping`` — the operator is left to guess what it means, and the
    badge reads as a bug rather than as a state.
    """
    source = APP_JS.read_text(encoding="utf-8", errors="replace")
    for function_name in ("setExecBadge", "setVerificationBadge"):
        labels = _labels_table(source, function_name)
        assert "stopping" in labels, (
            f"{function_name}() has no 'stopping' label; its table carries "
            f"{sorted(labels)!r}"
        )
        value = labels["stopping"]
        assert re.search(r"[一-鿿]", value), (
            f"{function_name}() renders 'stopping' as {value!r}, which is "
            "not a Chinese label"
        )


def test_app_js_pin_matches_disk() -> None:
    """The static gate's frozen hash is a measurement of the file.

    The gate pins ``frontend/app.js`` byte for byte.  The pin has to be
    the hash of the file that is on disk now — a hash typed in by hand
    is a claim about a file rather than a measurement of one, and the
    first later edit of that file slips through a gate that never sees
    a byte.
    """
    pinned = _pinned_app_js_sha256()
    actual = hashlib.sha256(APP_JS.read_bytes()).hexdigest()
    assert pinned == actual, (
        "the frozen hash of frontend/app.js in "
        f"{FROZEN_GATE.name} does not match the file on disk: "
        f"pinned {pinned}, actual {actual}"
    )


# ---------------------------------------------------------------------------
# Source readers
# ---------------------------------------------------------------------------


def _labels_table(source: str, function_name: str) -> Dict[str, str]:
    """The label table of ``function_name``, as ``{status: label}``.

    The table is read out of the source rather than imported: the
    frontend is a classic script, and a gate that imported it would
    need a browser to say anything about it.
    """
    pattern = re.compile(
        r"function\s+" + re.escape(function_name) + r"\s*\([^)]*\)\s*\{.*?"
        r"labels\s*=\s*\{(?P<body>[^}]*)\}",
        re.DOTALL,
    )
    match = pattern.search(source)
    assert match is not None, (
        f"{function_name}() has no label table in {APP_JS.name}"
    )
    return dict(re.findall(r"(\w+)\s*:\s*\"([^\"]*)\"", match.group("body")))


def _pinned_app_js_sha256() -> str:
    """The ``frontend/app.js`` hash the static gate writes down.

    Read out of the gate's own source text, so the value compared with
    the disk is the value that is actually written down — and not
    whatever an import of the module would hand over.
    """
    source = FROZEN_GATE.read_text(encoding="utf-8", errors="replace")
    match = re.search(
        r'"frontend/app\.js"\s*:\s*\(\s*"([0-9a-f]{64})"\s*\)',
        source,
    )
    assert match is not None, (
        f"{FROZEN_GATE.name} pins no sha256 for frontend/app.js"
    )
    return match.group(1)

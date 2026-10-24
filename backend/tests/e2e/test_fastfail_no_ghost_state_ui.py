"""End-to-end: a refused start never puts 执行中 on the screen.

Background
----------
``POST /api/execution/{plan_id}/start`` refuses a ``project_dir`` that
does not exist — ``backend/project_dir_guard.validate_project_dir`` is
the single check, and it runs *before* the routing CAS and before the
spawn. A refused start therefore leaves the plan in the phase it was
in and never starts a process.

The backend half of that story is pinned by
``backend/tests/e2e/test_fastfail_no_ghost_state_e2e.py``. This file
pins the half an operator actually looks at: the page.

What the page reads
-------------------
Three reads, all of them public HTTP:

* ``GET /api/plan/{id}/state`` — ``loadPlanState()`` in
  ``frontend/app.js``; it drives the step indicator and tells the
  frontend which phase the plan is in.
* ``GET /api/plan/{id}/status`` — the plan card's single snapshot
  (``backend/routes/plans.py``); it carries the ``execution`` section
  the card renders.
* ``GET /api/execution/{id}/status`` — what the badge polls
  (``startExecutionPolling()`` / ``resumeExecutionPanel()``).

The badge label itself comes from the table in ``frontend/app.js``:
``running`` maps to 执行中, and no other status does. So 执行中 can only
appear when one of those endpoints reports ``running`` — which is
exactly what this file shows never happens after a refusal.

How the user flow is replayed
-----------------------------
Without a browser, the flow is replayed over HTTP in the order the
frontend issues it: the start is POSTed with the operator's (mistyped)
working directory, the two status endpoints are then read back, and the
served bundle is read to translate the status that came back into the
label the badge would show. Every assertion is on a response body or a
status code — never on the server process's memory.

Why there is no browser in the default path
-------------------------------------------
This repository ships no Playwright dependency and no Puppeteer
dependency, and this file does not add one: installing a browser
driver (or downloading browser binaries) is not something a test may
do on a developer's machine. The default path therefore drives the
same endpoints the page drives, and the DOM is checked by an
*optional* test that runs only when a driver is already present in
the environment. The optional test is additive: when no driver is
installed it skips, and the assertions above still hold.

Isolation
---------
Every test boots its own server against its own ``tmp_path`` (its own
state DB, its own plans tree) and tears it down in a ``finally``, so a
leaked server cannot influence the next test and a failed assertion
cannot leave a listener behind. The port is picked by the kernel
(``_free_port``) — never a hard-coded 8000/8001.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + bootstrap
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent

# Prefer the project venv: the backend subprocess these tests boot must
# run on the interpreter this suite was installed into.  A checkout with
# no ``backend/.venv`` (fresh worktree, container, CI before the venv
# bootstrap step) must not turn the whole suite into a
# ``FileNotFoundError`` raised *before* the server is ever started -- a
# red suite that asserts nothing.  Fall back to the interpreter already
# running pytest, so the server really boots.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

REQUEST_HEADER = "X-PDT-Request"
REQUEST_HEADER_VALUE = "1"

#: The plan every test in this file drives. It is not a date-prefixed
#: directory name, so ``classify_plan`` reads it as "new" and the
#: archived-plan guard (HTTP 410) stays out of the way.
PLAN_ID = "p-guard"

#: The label the frontend shows for a running execution. It is the one
#: string this file is about: a refused start must never produce it.
RUNNING_LABEL = "执行中"

#: The home this test run was *launched* with.  ``install_hermetic_env()``
#: redirects ``$HOME`` to a temp dir, and Playwright resolves the browser
#: binaries it launches under the home it is handed -- so the driver
#: subprocess is given the real home, the same way the provider e2e
#: resolves the real CC Switch database.
REAL_HOME_ENV = "PDT_TEST_REAL_HOME"


# ---------------------------------------------------------------------------
# Server boot — copied from ``test_fastfail_no_ghost_state_e2e.py``.
#
# The helpers are duplicated rather than imported: ``backend/tests/e2e/``
# has no ``__init__.py``, so a cross-module import from here would depend
# on pytest's rootdir-based sys.path juggling (and on collection order).
# A copy is boring, and boring is what a test harness should be.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind to an ephemeral loopback port and return its number.

    The kernel hands us a free port, then releasing the socket leaves
    a small race window during which another process could grab it.
    That race is acceptable for an isolated test process; the
    ``_wait_for_server`` polling below tolerates it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _utcnow_iso() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _wait_for_server(base_url: str, timeout: float = 30.0) -> None:
    """Block until ``GET /health`` on ``base_url`` returns 2xx."""
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"{base_url}/health", timeout=2
            ) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last_exc = exc
        time.sleep(0.2)
    raise RuntimeError(
        f"server at {base_url} did not start in {timeout}s; last_exc={last_exc}"
    )


def boot_real_server(tmp_path: Path) -> Tuple[str, subprocess.Popen, Path]:
    """Boot a real ``backend.server`` subprocess against ``tmp_path``.

    Returns ``(base_url, proc, workdir)`` — ``workdir`` is the directory
    the server's state DB and plans tree live under; tests MUST address
    the server's files via ``workdir`` because the subprocess has its own
    env.

    If the server does not come up within 30 s, the process is killed and
    reaped here and the failure is re-raised — a server that never
    started must not leave a listener (or a zombie) behind.
    """
    state_db = tmp_path / "state.db"
    plans_dir = tmp_path / "plans"
    backups_dir = tmp_path / "backups"
    logs_dir = tmp_path / "logs"
    for d in (plans_dir, backups_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    port = _free_port()

    env = dict(os.environ)
    env.update({
        "PDT_STATE_DB_PATH": str(state_db),
        "PDT_PLANS_DIR": str(plans_dir),
        "PDT_HOST": "127.0.0.1",
        "PDT_PORT": str(port),
        "PDT_DEBUG_ROUTES": "1",
        "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
        # Pinned rather than "dry-run": an unrecognised value falls
        # through to ``cc_switch.probe()``, and the suite's hermetic env
        # seeds an empty CC Switch database, so the probe reports an
        # installed fleet that is not there and the server's lifespan
        # then asks ``127.0.0.1:4000`` for a chain nobody serves. This
        # server exists to prove a refusal, not to exercise provider
        # routing, so the absence of a fleet is the honest setting.
        "PDT_PROVIDER_MODE": "inherit",
        # A capacity file that does not exist = "no rules configured".
        "PDT_PROVIDER_CAPACITY_FILE": str(tmp_path / "absent.yaml"),
        "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
    })
    env.pop("ANTHROPIC_BASE_URL", None)

    # A throwaway test server has no secret to receive, and a stale
    # descriptor number must never be closed by a process that never
    # owned it: ``credentials.read_secret_fd`` closes the descriptor it
    # is handed on every path (the descriptor is "spent either way"),
    # so an inherited ``PDT_SECRET_FD_*=N`` whose pipe is long gone
    # makes the server close whatever now sits at fd N.  On macOS that
    # is asyncio's kqueue descriptor, and the event loop dies with
    # ``OSError: [Errno 9] Bad file descriptor`` before the server ever
    # answers ``/health``.  Drop the whole family for the child.
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)

    log_path = logs_dir / "server.log"
    log_handle = log_path.open("wb")
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
        _wait_for_server(base_url, timeout=30.0)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        log_handle.close()
        raise

    proc._pdt_log_handle = log_handle  # type: ignore[attr-defined]
    return base_url, proc, tmp_path


def teardown_server(proc: subprocess.Popen) -> None:
    """Terminate ``proc`` and close its log handle."""
    log_handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Fixture helpers — what the plan looks like when the server sees it
# ---------------------------------------------------------------------------


def _seed_plan_dir(workdir: Path, plan_id: str) -> Path:
    """Create the on-disk plan directory the start route expects.

    ``tasks.json`` is the file the route checks first; without it the
    route answers 404 and the guard below is never reached.
    """
    plan_dir = workdir / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    return plan_dir


def _seed_routing_row(
    state_db: Path, plan_id: str, phase: str = "ready"
) -> None:
    """Insert the ``plan_routing`` row a plan sitting in ``ready`` has.

    The row is what the "phase never reached ``executing``" assertion
    reads.  Seeding it explicitly is what makes that assertion mean
    something: without a row the column would be ``NULL`` both before
    and after the refusal.
    """
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(state_db)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, substage, version, updated_at) "
            "VALUES (?, ?, NULL, 0, ?)",
            (plan_id, phase, _utcnow_iso()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_get(
    base_url: str,
    path: str,
    *,
    api: bool = True,
    timeout: float = 20.0,
) -> Tuple[int, bytes]:
    """GET ``base_url + path``; return ``(status, body)``.

    ``api=True`` adds the ``X-PDT-Request`` header — the server's
    request guard refuses every ``/api/*`` request that arrives without
    it.  The static assets are fetched *without* it (``api=False``),
    because a browser navigation cannot attach a custom header: that is
    exactly how the UI loads them, and the guard leaves them open.
    """
    headers = {"Host": "127.0.0.1"}
    if api:
        headers[REQUEST_HEADER] = REQUEST_HEADER_VALUE
    req = urllib.request.Request(f"{base_url}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""


def _post_start(
    base_url: str, plan_id: str, project_dir: str
) -> Tuple[int, bytes]:
    """POST a start request; return ``(status, body)``.

    This is the request the frontend's ``startExecution()`` sends: the
    same path, the same body, the same guard header.
    """
    body = json.dumps({"project_dir": project_dir}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/execution/{plan_id}/start",
        data=body,
        method="POST",
        headers={
            REQUEST_HEADER: REQUEST_HEADER_VALUE,
            "Host": "127.0.0.1",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""


def _json(payload: bytes) -> Any:
    """Decode a JSON response body."""
    return json.loads(payload.decode("utf-8"))


# ---------------------------------------------------------------------------
# The label table the badge renders from
# ---------------------------------------------------------------------------

_LABELS_DECL = re.compile(r"const\s+labels\s*=\s*\{(?P<body>[^}]*)\}")
_LABEL_PAIR = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*\"(?P<value>[^\"]*)\""
)


def _labels_from_app_js(source: str) -> Dict[str, str]:
    """The ``status -> label`` table the served bundle declares.

    Read out of the bytes the server actually served, not out of the
    repository file: the served bundle is what the page runs.
    """
    match = _LABELS_DECL.search(source)
    assert match is not None, (
        "the served /app.js no longer declares `const labels = {...}`; "
        "the table the execution badge renders its text from is gone"
    )
    return {
        pair.group("key"): pair.group("value")
        for pair in _LABEL_PAIR.finditer(match.group("body"))
    }


# ---------------------------------------------------------------------------
# 1. The bundle still carries the 执行中 label — and only for `running`
# ---------------------------------------------------------------------------


def test_app_js_renders_execution_label_from_status(tmp_path):
    """``/app.js`` maps the ``running`` status to 执行中, and only that.

    The mapping is what the badge renders (``setExecBadge`` in
    ``frontend/app.js``).  Pinning it from the *served* bundle — not
    from the file on disk — is what makes the rest of this file's
    argument hold: 执行中 is reachable only through a status of
    ``running``.
    """
    base_url, proc, workdir = boot_real_server(tmp_path)
    try:
        status, body = _http_get(base_url, "/app.js", api=False)
        assert status == 200, (
            f"GET /app.js returned HTTP {status}, expected 200"
        )
        source = body.decode("utf-8")

        assert f'running: "{RUNNING_LABEL}"' in source, (
            f"the served bundle must map the running status to "
            f"{RUNNING_LABEL!r}; the served /app.js does not contain "
            f"'running: \"{RUNNING_LABEL}\"'"
        )

        labels = _labels_from_app_js(source)
        assert labels.get("running") == RUNNING_LABEL, (
            f"the label table must render {RUNNING_LABEL!r} for the "
            f"running status; got {labels.get('running')!r}"
        )

        producing = [
            key for key, value in labels.items() if value == RUNNING_LABEL
        ]
        assert producing == ["running"], (
            f"{RUNNING_LABEL!r} must be reachable only through the "
            f"running status; these statuses render it: {producing!r}"
        )
    finally:
        teardown_server(proc)


# ---------------------------------------------------------------------------
# 2. The replayed flow never reports `running`
# ---------------------------------------------------------------------------


def test_replayed_user_flow_never_reports_running(tmp_path):
    """A refused start leaves every endpoint the page reads non-running.

    The flow is the one the frontend performs, in the order it performs
    it: POST the start with a working directory that is gone, then read
    the plan state and the two status endpoints back, and finally read
    the served bundle to see which label the badge would render for the
    status that came back.  Every answer must say the plan is not
    running — otherwise the badge would render 执行中 for a run that
    never started.
    """
    base_url, proc, workdir = boot_real_server(tmp_path)
    try:
        _seed_plan_dir(workdir, PLAN_ID)
        _seed_routing_row(workdir / "state.db", PLAN_ID, phase="ready")

        missing = tmp_path / "deleted-worktree"
        assert not missing.exists(), (
            f"the fixture path {missing} must not exist for this test"
        )

        # -- the operator submits the mistyped working directory -------
        start_status, start_body = _post_start(
            base_url, PLAN_ID, str(missing)
        )
        assert start_status == 400, (
            f"the start endpoint answered HTTP {start_status}, expected "
            f"400; body={start_body!r}"
        )

        # -- loadPlanState(): which phase is the plan in? --------------
        state_status, state_body = _http_get(
            base_url, f"/api/plan/{PLAN_ID}/state"
        )
        assert state_status == 200, (
            f"GET /api/plan/{PLAN_ID}/state answered HTTP {state_status}"
        )
        state = _json(state_body)
        assert state.get("current_phase") == "ready", (
            f"a refused start must leave the plan in the phase it was in; "
            f"current_phase={state.get('current_phase')!r}"
        )

        # -- the plan card's snapshot ----------------------------------
        card_status, card_body = _http_get(
            base_url, f"/api/plan/{PLAN_ID}/status"
        )
        assert card_status == 200, (
            f"GET /api/plan/{PLAN_ID}/status answered HTTP {card_status}"
        )
        card = _json(card_body)

        execution = card.get("execution")
        assert isinstance(execution, dict), (
            f"the plan card payload must carry an execution section; "
            f"got {execution!r}"
        )
        assert "execution_status" in execution, (
            f"the plan card's execution section must carry the status the "
            f"card renders; got {sorted(execution)!r}"
        )
        assert execution["execution_status"] != "running", (
            f"a refused start must not report a running execution; "
            f"execution_status={execution['execution_status']!r}"
        )
        assert "pid" in execution, (
            f"the plan card's execution section must carry the pid; "
            f"got {sorted(execution)!r}"
        )
        assert execution["pid"] is None, (
            f"a refused start spawns nothing, so there is no pid to "
            f"report; pid={execution['pid']!r}"
        )

        summary_execution = card.get("summary", {}).get("execution")
        assert isinstance(summary_execution, dict), (
            f"the plan card's summary must carry an execution section; "
            f"got {summary_execution!r}"
        )
        assert summary_execution.get("status") != "running", (
            f"the summary's execution status must not be running; "
            f"status={summary_execution.get('status')!r}"
        )
        assert summary_execution.get("pid") is None, (
            f"the summary's execution pid must be empty; "
            f"pid={summary_execution.get('pid')!r}"
        )

        # -- the badge's own poll ---------------------------------------
        badge_status, badge_body = _http_get(
            base_url, f"/api/execution/{PLAN_ID}/status"
        )
        assert badge_status == 200, (
            f"GET /api/execution/{PLAN_ID}/status answered HTTP "
            f"{badge_status}"
        )
        badge = _json(badge_body)
        assert badge.get("status") != "running", (
            f"the badge polls this endpoint; a refused start must not "
            f"report running; status={badge.get('status')!r}"
        )
        assert badge.get("pid") is None, (
            f"the badge's endpoint must report no pid; "
            f"pid={badge.get('pid')!r}"
        )

        # -- and therefore the badge renders something else ------------
        app_js_status, app_js_body = _http_get(
            base_url, "/app.js", api=False
        )
        assert app_js_status == 200, (
            f"GET /app.js answered HTTP {app_js_status}"
        )
        labels = _labels_from_app_js(app_js_body.decode("utf-8"))
        rendered = labels.get(badge.get("status"))
        assert rendered != RUNNING_LABEL, (
            f"the badge would render {RUNNING_LABEL!r} for status "
            f"{badge.get('status')!r}; a refused start must not produce "
            f"that label"
        )
    finally:
        teardown_server(proc)


# ---------------------------------------------------------------------------
# 3. The shell the page is served from carries the plan-card markup
# ---------------------------------------------------------------------------


def test_app_shell_serves_plan_card_markup(tmp_path):
    """``/`` serves the app shell and ``/app.js`` the plan-card markup.

    The shell is the element the page is mounted into (``id="app"``),
    and ``data-plan`` is the attribute every plan row carries — the
    hook the frontend binds its plan selection to.  If either is gone,
    the page this file is about has no plan to render.
    """
    base_url, proc, workdir = boot_real_server(tmp_path)
    try:
        index_status, index_body = _http_get(base_url, "/", api=False)
        assert index_status == 200, (
            f"GET / returned HTTP {index_status}, expected 200"
        )
        index = index_body.decode("utf-8")
        assert 'id="app"' in index, (
            f"the served index.html must carry the app shell element "
            f'id="app"; the served page does not'
        )

        app_js_status, app_js_body = _http_get(base_url, "/app.js", api=False)
        assert app_js_status == 200, (
            f"GET /app.js returned HTTP {app_js_status}, expected 200"
        )
        assert "data-plan" in app_js_body.decode("utf-8"), (
            f"the served bundle must carry the data-plan attribute the "
            f"plan rows are rendered with"
        )
    finally:
        teardown_server(proc)


# ---------------------------------------------------------------------------
# Optional: the same assertions against a real DOM
# ---------------------------------------------------------------------------
#
# The tests above are the contract. The one below is *extra* evidence,
# and it only runs where a browser driver already exists — this
# repository ships no Playwright or Puppeteer dependency, and a test is
# not allowed to install one (that would download a browser onto a
# developer's machine). Where no driver is installed the test skips,
# and the assertions above still stand on their own.


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


def _playwright_interpreter() -> Optional[str]:
    """An interpreter that can import ``playwright``, or ``None``.

    Two candidates, in order: the interpreter running pytest, and the
    interpreter behind a ``playwright`` command on ``PATH`` — the
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
            # ``#!/usr/bin/env python3`` names the interpreter in the
            # argument after ``env``; ``#!/path/to/python3`` names it
            # in the shebang itself.
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


_BROWSER_DRIVER = r'''
"""Drive the served page and report what the badge shows."""
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


url, plan_id, missing_dir = sys.argv[1], sys.argv[2], sys.argv[3]

executable = browser_executable()
launch_kwargs = {"executable_path": executable} if executable else {}

with sync_playwright() as driver:
    browser = driver.chromium.launch(**launch_kwargs)
    page = browser.new_page()
    page.goto(url, wait_until="load")
    page.wait_for_selector("#app", timeout=30000)
    page.wait_for_timeout(1000)

    # Select the plan, then let the frontend's own start button send the
    # mistyped working directory.  The page's own code path runs: the
    # request is refused with 400 and the catch branch alerts.
    page.evaluate(
        """([planId, projectDir]) => {
            currentPlanId = planId;
            document.querySelector('#project-dir-input').value = projectDir;
        }""",
        [plan_id, missing_dir],
    )
    page.evaluate("() => startExecution()")

    page.wait_for_timeout(1500)
    badge = page.inner_text("#execution-status-badge")
    # ``text_content`` reads the DOM, ``inner_text`` only what is
    # rendered -- the badge lives in a panel that is not open, so only
    # the DOM read can see the label it carries.
    dom_text = page.text_content("body")
    print(json.dumps({
        "badge": badge,
        "dom_has_running_label": "执行中" in dom_text,
        "badge_class": page.get_attribute(
            "#execution-status-badge", "class"
        ),
    }))
    browser.close()
'''


def test_browser_replays_rejected_start_without_running_badge(tmp_path):
    """The page itself never shows 执行中 for a refused start.

    Optional by construction: it needs a Playwright driver that is
    already installed, and it skips when there is none.  It drives the
    frontend's own ``startExecution()`` — the code path the operator
    takes — with a working directory that does not exist, and then reads
    the badge the page rendered.
    """
    interpreter = _playwright_interpreter()
    if interpreter is None:
        pytest.skip(
            "no Playwright driver is installed in this environment; "
            "the default (browserless) path carries the assertions"
        )

    base_url, proc, workdir = boot_real_server(tmp_path)
    try:
        _seed_plan_dir(workdir, PLAN_ID)
        _seed_routing_row(workdir / "state.db", PLAN_ID, phase="ready")

        missing = tmp_path / "deleted-worktree"
        assert not missing.exists()

        driver_script = tmp_path / "drive_page.py"
        driver_script.write_text(_BROWSER_DRIVER, encoding="utf-8")

        result = subprocess.run(
            [
                interpreter,
                str(driver_script),
                f"{base_url}/",
                PLAN_ID,
                str(missing),
            ],
            capture_output=True,
            text=True,
            timeout=180,
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
                    "binaries are not; the default path carries the "
                    "assertions"
                )
            raise AssertionError(
                f"the browser driver failed (exit {result.returncode}); "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )

        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["badge"] != RUNNING_LABEL, (
            f"the page rendered {RUNNING_LABEL!r} for a refused start; "
            f"badge={payload['badge']!r}"
        )
        assert payload["dom_has_running_label"] is False, (
            f"the page carries {RUNNING_LABEL!r} somewhere in its DOM after "
            f"a refused start"
        )
    finally:
        teardown_server(proc)

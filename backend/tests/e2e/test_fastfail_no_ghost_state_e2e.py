"""End-to-end: a refused start never leaves a ghost ``executing`` state.

Background
----------
``POST /api/execution/{plan_id}/start`` used to answer a mistyped (or
stale) ``project_dir`` with ``mkdir(parents=True, exist_ok=True)`` — it
silently *created* the directory and let the run proceed inside an empty
tree. The real error only surfaced much later, as a confusing "no such
file" from the executor subprocess, with nothing pointing back at the
typo that caused it.

``project_dir_guard.validate_project_dir`` is the single check (see
``backend/project_dir_guard.py``): the directory must already exist and
must really be a directory. The route calls it *before* the routing CAS,
so a refused start never moves the plan into ``executing`` and never
reaches the spawn.

Why a *real* server
--------------------
The unit-level guard tests (``backend/tests/api/
test_start_execution_project_dir_guard.py``) drive the route through
``fastapi.testclient.TestClient``. That harness does not cross a process
boundary: it never goes through the request guard, it never really
spawns, and — most importantly — it cannot observe what a *server
process* writes to disk. The rejection path is the one place where the
operator's evidence has to be on disk (the ``plan_execution`` row and
``<plan_dir>/logs/execution_start.log``), so it has to be exercised
through a real ``backend.server`` subprocess that talks to a real
SQLite file.

What these tests pin
--------------------1. a missing ``project_dir`` is refused with HTTP 400 and the 400 body
     names the offending path;
  2. the refusal leaves the plan exactly where it was — ``plan_routing``
     still says ``ready`` (never ``executing``) and ``plan_execution``
     carries no pid;
  3. the refusal leaves a diagnosable trace: the ``plan_execution`` row
     says ``failed`` / ``stop_reason='project_dir_invalid'``, and
     ``<plan_dir>/logs/execution_start.log`` names the offending path.

Only the public HTTP API and the SQLite rows are asserted — never the
server process's memory.

Isolation
---------
Every test boots its own server against its own ``tmp_path`` (its own
state DB, its own plans tree) and tears it down in a ``finally``, so a
leaked server cannot influence the next test and a failed assertion
cannot leave a listener behind. The port is picked by the kernel
(``_free_port``) — never a hard-coded 8000/8001 — so the suite can run
alongside a developer's own server.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple

import pytest

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Paths + bootstrap
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tests import _readiness  # noqa: E402

# Prefer the project venv: the backend subprocess these tests boot must
# run on the interpreter this suite was installed into.  A checkout with
# no ``backend/.venv`` (fresh worktree, container, CI before the venv
# bootstrap step) must not turn the whole suite into a
# ``FileNotFoundError`` raised *before* the server is ever started -- a
# red suite that asserts nothing.  Fall back to the interpreter already
# running pytest, so the server really boots.  Deliberately not
# ``pytest.skip``: skipping would swap fake reds for fake greens.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

REQUEST_HEADER = "X-PDT-Request"
REQUEST_HEADER_VALUE = "1"

#: The plan every test in this file drives. It is not a date-prefixed
#: directory name, so ``classify_plan`` reads it as "new" and the
#: archived-plan guard (HTTP 410) stays out of the way.
PLAN_ID = "p-guard"


# ---------------------------------------------------------------------------
# Server boot — copied from ``test_onboarding_and_frontend_e2e.py``.
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


#: How long the spawned server may take to answer ``/health``.
#:
#: The ceiling catches a **hung** server, not a slow one.  It used to be
#: 30 s, calibrated on an idle machine: on a loaded host the real server
#: boots slower than that, and the 2026-10-31 whole-repo gate reported
#: ``server at http://127.0.0.1:55739 did not start in 30.0s`` — a red
#: that says the machine was busy, not that the code was broken.  120 s
#: still catches a hang and no longer blames the delivery for the load.
#:
#: The number itself has ONE home — ``tests/_readiness.py`` — because
#: every suite that waits for a backend to answer asks the same
#: question of the same machine and must not carry a second copy of the
#: answer.  The value is that module's ``READY_TIMEOUT_SECONDS``.
_SERVER_READY_TIMEOUT = _readiness.READY_TIMEOUT_SECONDS


def test_the_server_ready_timeout_is_sized_for_a_busy_machine() -> None:
    """The ceiling must outlast a loaded host's server boot.

    A red that only means "the machine was busy" is a red that produces
    repair tasks against a delivery that did not fail.
    """
    assert _SERVER_READY_TIMEOUT >= 120.0


def test_the_loopback_opener_does_not_go_through_a_proxy() -> None:
    """打本机服务的请求必须直连，不能经过环境里的代理。

    代理会用**它自己的 502**（空 body）回答一个指向本机端口的请求 ——
    就绪等待读到的是代理的 502，不是服务的响应，于是「服务没起来」被写进了
    error，而服务其实好好的。再怎么放宽上限也修不好一个根本没送达的请求。

    断言用**行为**而不是结构：起一个真的本机小服务，用这个 opener 去打，
    读到的必须是那个服务自己的响应。开着代理时读到的是代理的 502。
    """
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: Any) -> None:
            """Keep the test output clean."""

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = "http://127.0.0.1:{0}/probe".format(httpd.server_port)
        with _loopback_opener().open(url, timeout=10) as resp:
            assert resp.status == 200, resp.status
            assert resp.read() == b"ok"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_loopback_opener_carries_no_proxy_handler() -> None:
    """结构上的同一件事：opener 里不许有会发代理的 handler。

    ``ProxyHandler({})`` 不会进 ``opener.handlers``（它没有 ``*_open``
    方法可挂），所以「没有 ProxyHandler」正是「不走代理」的结构形状。
    """
    opener = _loopback_opener()
    assert not [
        handler
        for handler in opener.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ], "opener 里出现 ProxyHandler 就意味着本机请求会被发给代理"


def _loopback_opener() -> urllib.request.OpenerDirector:
    """An opener that talks to the spawned server **directly**.

    ``urllib`` builds its default opener from ``getproxies()`` — and on
    macOS that reads the *system* proxy configuration, so a machine
    with a proxy client installed routes *these* loopback calls through
    it.  The proxy answers a request naming a local port with **its own
    502** (empty body), not with the server's response: the readiness
    wait then reads "the server never started" for a server that is
    answering perfectly well — and it keeps reading it for as long as
    the ceiling allows, because the request never arrives at all.

    ``ProxyHandler({})`` is urllib's way of saying "no proxy" — the
    same idiom as ``backend/hooks/dispatcher.py``.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _wait_for_server(base_url: str, timeout: float = _SERVER_READY_TIMEOUT) -> None:
    """Block until ``GET /health`` on ``base_url`` returns 2xx."""
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    opener = _loopback_opener()
    while time.time() < deadline:
        try:
            with opener.open(f"{base_url}/health", timeout=2) as resp:
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
        _wait_for_server(base_url, timeout=_SERVER_READY_TIMEOUT)
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
# HTTP helper
# ---------------------------------------------------------------------------


def post_start(
    base_url: str, plan_id: str, project_dir: str
) -> Tuple[int, bytes]:
    """POST a start request; return ``(status, body)``.

    The ``X-PDT-Request`` header is mandatory: the server's request
    guard refuses any ``/api/*`` request that arrives without it, and
    the explicit ``Host`` keeps the Origin/Host check green.
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
        with _loopback_opener().open(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""


def _detail_of(body: bytes) -> str:
    """The ``detail`` field of a JSON error body (empty when absent)."""
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if isinstance(payload, dict):
        detail = payload.get("detail", "")
        return detail if isinstance(detail, str) else json.dumps(detail)
    return ""


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


def _read_rows(
    state_db: Path, plan_id: str
) -> Tuple[dict | None, dict | None]:
    """Read the ``plan_routing`` and ``plan_execution`` rows for a plan.

    Returns ``(routing_row, execution_row)`` — either may be ``None``
    when the row does not exist.  Only the committed SQLite state is
    read; the server process's memory is never inspected.
    """
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from state_machine.db.connection import open as open_db

    conn = open_db(state_db)
    conn.row_factory = sqlite3.Row
    try:
        routing = conn.execute(
            "SELECT current_phase, substage, version FROM plan_routing "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        execution = conn.execute(
            "SELECT exec_status, exec_pid, stop_reason, project_dir "
            "FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return (
        dict(routing) if routing is not None else None,
        dict(execution) if execution is not None else None,
    )


def _expected_path(raw: str) -> str:
    """The path the server will report for ``raw``.

    The route resolves the requested directory before validating it
    (``Path(...).expanduser().resolve()``), so the path that reaches the
    400 body, the log line and the ``plan_execution`` row is the
    *resolved* one.  On macOS ``/var`` is a symlink to ``/private/var``,
    so the two strings genuinely differ — comparing against the resolved
    form is what makes the assertion correct on every platform.
    """
    return str(Path(raw).expanduser().resolve())


# ---------------------------------------------------------------------------
# The scenario, shared by the three assertions
# ---------------------------------------------------------------------------


class _RejectedStart:
    """What the server did with an unusable ``project_dir``."""

    def __init__(self, status: int, body: bytes, workdir: Path) -> None:
        self.status = status
        self.body = body
        self.detail = _detail_of(body)
        self.state_db = workdir / "state.db"
        self.plan_dir = workdir / "plans" / PLAN_ID
        routing, execution = _read_rows(self.state_db, PLAN_ID)
        self.routing = routing
        self.execution = execution

    @property
    def log_path(self) -> Path:
        return self.plan_dir / "logs" / "execution_start.log"

    def log_text(self) -> str:
        return self.log_path.read_text(encoding="utf-8")


def _reject_with_missing_project_dir(tmp_path: Path) -> _RejectedStart:
    """Boot a server, ask it to start a plan in a directory that is gone.

    The whole scenario lives here so the three tests assert on the *same*
    refusal — one server, one request, three different questions about
    the outcome (the answer, the state, the trace).
    """
    base_url, proc, workdir = boot_real_server(tmp_path)
    try:
        plan_dir = _seed_plan_dir(workdir, PLAN_ID)
        _seed_routing_row(workdir / "state.db", PLAN_ID, phase="ready")

        missing = tmp_path / "deleted-worktree"
        assert not missing.exists(), (
            f"the fixture path {missing} must not exist for this test"
        )

        status, body = post_start(base_url, PLAN_ID, str(missing))
        return _RejectedStart(status, body, workdir)
    finally:
        teardown_server(proc)


# ---------------------------------------------------------------------------
# 1. The refusal answers 400 and names the offending path
# ---------------------------------------------------------------------------


def test_invalid_project_dir_is_rejected_end_to_end(tmp_path):
    """A ``project_dir`` that does not exist is refused with HTTP 400.

    The refusal happens before the routing CAS and before the spawn, so
    the answer is a 400 naming the offending path — not a run that dies
    somewhere inside the executor subprocess.
    """
    outcome = _reject_with_missing_project_dir(tmp_path)

    assert outcome.status == 400, (
        f"start endpoint returned HTTP {outcome.status}, expected 400; "
        f"body={outcome.body!r}"
    )
    expected = _expected_path(str(tmp_path / "deleted-worktree"))
    assert expected in outcome.detail, (
        f"the 400 detail must name the offending path {expected!r}; "
        f"got detail={outcome.detail!r}"
    )
    assert "does not exist" in outcome.detail, (
        f"the 400 detail must say why the path was refused; "
        f"got detail={outcome.detail!r}"
    )


# ---------------------------------------------------------------------------
# 2. The refusal leaves no ghost 'executing' state
# ---------------------------------------------------------------------------


def test_plan_phase_never_reaches_executing(tmp_path):
    """A refused start leaves the plan in the phase it was in.

    This is the ghost-state bug in one assertion: the plan's routing
    phase must still say ``ready`` (never ``executing``) and the
    execution row must carry no pid — nothing was started, so there is
    no process to point at.
    """
    outcome = _reject_with_missing_project_dir(tmp_path)

    assert outcome.routing is not None, (
        f"the routing row for {PLAN_ID} must still exist after a refusal"
    )
    assert outcome.routing["current_phase"] == "ready", (
        f"a refused start must not move the plan out of 'ready'; "
        f"current_phase={outcome.routing['current_phase']!r}"
    )
    assert outcome.execution is not None, (
        f"the refusal must leave a plan_execution row for {PLAN_ID}"
    )
    assert outcome.execution["exec_pid"] is None, (
        f"exec_pid must stay NULL — nothing was spawned; "
        f"got {outcome.execution['exec_pid']!r}"
    )
    assert outcome.execution["exec_status"] == "failed", (
        f"exec_status must record the refusal; "
        f"got {outcome.execution['exec_status']!r}"
    )
    assert "project_dir_invalid" in (outcome.execution["stop_reason"] or ""), (
        f"stop_reason must say why; got {outcome.execution['stop_reason']!r}"
    )


# ---------------------------------------------------------------------------
# 3. The refusal leaves a diagnosable trace on disk
# ---------------------------------------------------------------------------


def test_rejection_is_written_to_execution_log(tmp_path):
    """The refusal is written to the plan's own execution log.

    Without this, a rejected start looks like a plan that was never
    started — no status, no reason, nothing to grep.
    """
    outcome = _reject_with_missing_project_dir(tmp_path)

    assert outcome.log_path.exists(), (
        f"the refusal must leave a log at {outcome.log_path}"
    )
    log_text = outcome.log_text()
    expected = _expected_path(str(tmp_path / "deleted-worktree"))
    assert expected in log_text, (
        f"the log must name the offending path {expected!r} verbatim; "
        f"got log_text={log_text!r}"
    )
    assert "project_dir_invalid" in log_text, (
        f"the log must say why the start was refused; "
        f"got log_text={log_text!r}"
    )
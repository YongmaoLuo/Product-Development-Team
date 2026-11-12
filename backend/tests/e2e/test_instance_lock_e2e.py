"""End-to-end: one PDT server per data root.

Background
----------
A PDT server owns a *data root*: the plans tree it writes artifacts into
and the SQLite state database it records progress in.  Two servers on
one root do not merely duplicate each other — they interleave writes
into the same plan directories, fight over the same state rows, and
each one's shutdown sweep reaps the other's children.

``backend/instance_lock.py`` is the guard, and ``server._lifespan``
takes the lock before it does anything else.  These tests drive the
guard through **real server processes**, because the thing under test
is a refusal that happens at start-up: the second server must never
bind a port, never answer ``/health``, and must say *who* holds the
root.

Why a *real* server
--------------------
A unit test can hold the lock in-process and assert that a second
acquire raises.  That proves the lock works; it does not prove the
server *takes* it.  The wiring is the half that can rot silently — a
lifespan that simply forgot the lock would leave every unit test
green.  So the refusal here is asserted through a real
``backend.server`` subprocess, and the negative control is the test
itself: remove the wiring and the first case goes red.

What these tests pin
--------------------
1. A second server on the same root exits non-zero, within seconds,
   and its output names the *first* server's pid.
2. The first server is unharmed by the refusal — it keeps answering
   ``/health``.
3. Two servers on *different* roots both come up.  A guard that
   refused everything would pass case 1 and break the platform.

Isolation
---------
Every server boots against its own ``tmp_path`` root (its own state
DB, its own plans tree) and is torn down in a ``finally``, so a
leaked server cannot influence the next test.  The port is picked by
the kernel (``_free_port``) — never a hard-coded 8000/8001 — so the
suite can run alongside a developer's own server.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

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
# ``FileNotFoundError`` raised *before* the server is ever started.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: How long the spawned server may take to answer ``/health``.
#:
#: The ceiling catches a **hung** server, not a slow one.  The number
#: has ONE home — ``tests/_readiness.py`` — because every suite that
#: waits for a backend to answer asks the same question of the same
#: machine and must not carry a second copy of the answer.
_SERVER_READY_TIMEOUT = _readiness.READY_TIMEOUT_SECONDS

#: How long a refused server may take to give up.
#:
#: The refused server waits ``PDT_INSTANCE_LOCK_WAIT_SEC`` (3 s, set
#: below) for the root to free up and then exits.  The ceiling covers
#: that wait *plus* the interpreter's start-up (importing the server
#: module is not free) — and it is deliberately generous, because a
#: red that only means "the machine was busy" is a red that says
#: nothing about the code.  The test also records how long the refusal
#: actually took.
_REFUSAL_CEILING_SEC = 30.0


# ---------------------------------------------------------------------------
# Server boot — copied from ``test_fastfail_no_ghost_state_e2e.py``.
#
# The helpers are duplicated rather than imported: ``backend/tests/e2e/``
# has no ``__init__.py``, so a cross-module import from here would depend
# on pytest's rootdir-based sys.path juggling (and on collection order).
# A copy is boring, and boring is what a test harness should be.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind to an ephemeral loopback port and return its number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _loopback_opener() -> urllib.request.OpenerDirector:
    """An opener that talks to the spawned server **directly**.

    ``urllib`` builds its default opener from ``getproxies()`` — and on
    macOS that reads the *system* proxy configuration, so a machine
    with a proxy client installed routes **these** loopback calls
    through it.  ``ProxyHandler({})`` is urllib's way of saying "no
    proxy".
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _wait_for_server(base_url: str, timeout: float = _SERVER_READY_TIMEOUT) -> None:
    """Block until ``GET /health`` on ``base_url`` returns 2xx."""
    deadline = time.time() + timeout
    last_exc: Optional[Exception] = None
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


def _health_status(base_url: str) -> int:
    """The status ``GET /health`` answers with (0 when unreachable)."""
    try:
        with _loopback_opener().open(f"{base_url}/health", timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, ConnectionResetError, OSError):
        return 0


class _Server:
    """A spawned server process and the files it owns."""

    def __init__(
        self,
        base_url: str,
        proc: subprocess.Popen,
        root: Path,
        log_path: Path,
    ) -> None:
        self.base_url = base_url
        self.proc = proc
        self.root = root
        self.log_path = log_path

    def log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - defensive
            return ""

    def wait_for_exit(self, timeout: float) -> Optional[int]:
        """Wait for the process to exit; ``None`` if it is still alive."""
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None


def _boot_server(
    root: Path,
    log_name: str,
    *,
    wait_ready: bool = True,
) -> _Server:
    """Boot a real ``backend.server`` subprocess against ``root``.

    ``root`` holds the server's state DB and plans tree.  With
    ``wait_ready=False`` the caller expects the server to *refuse* to
    start, so the readiness wait is skipped and the caller waits for
    the exit instead.
    """
    state_db = root / "state.db"
    plans_dir = root / "plans"
    backups_dir = root / "backups"
    logs_dir = root / "logs"
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
        # The instance lock gives up after this many seconds.  It is the
        # environment seam the lock reads (``PDT_INSTANCE_LOCK_WAIT_SEC``)
        # and it keeps the refused server's exit inside the test's
        # ceiling.
        "PDT_INSTANCE_LOCK_WAIT_SEC": "3",
        # Pinned rather than "dry-run": an unrecognised value falls
        # through to ``cc_switch.probe()``, and the suite's hermetic env
        # seeds an empty CC Switch database, so the probe reports an
        # installed fleet that is not there and the server's lifespan
        # then asks ``127.0.0.1:4000`` for a chain nobody serves.
        "PDT_PROVIDER_MODE": "inherit",
        # A capacity file that does not exist = "no rules configured".
        "PDT_PROVIDER_CAPACITY_FILE": str(root / "absent.yaml"),
        "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
    })
    env.pop("ANTHROPIC_BASE_URL", None)

    # A throwaway test server has no secret to receive, and a stale
    # descriptor number must never be closed by a process that never
    # owned it — drop the whole family for the child.
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)

    log_path = logs_dir / log_name
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
    server = _Server(base_url, proc, root, log_path)
    if wait_ready:
        try:
            _wait_for_server(base_url, timeout=_SERVER_READY_TIMEOUT)
        except Exception:
            proc.kill()
            proc.wait(timeout=5)
            log_handle.close()
            raise

    proc._pdt_log_handle = log_handle  # type: ignore[attr-defined]
    return server


def _teardown(server: Optional[_Server]) -> None:
    """Terminate a spawned server and close its log handle."""
    if server is None:
        return
    log_handle = getattr(server.proc, "_pdt_log_handle", None)
    try:
        if server.proc.poll() is None:
            server.proc.terminate()
            try:
                server.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.proc.kill()
                server.proc.wait(timeout=5)
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# 1. A second server on the same root is refused
# ---------------------------------------------------------------------------


def test_a_second_server_on_the_same_root_is_refused(tmp_path):
    """The second server must not come up, and must name the holder.

    The negative control is this test itself: with the instance lock
    removed from the lifespan, the second server boots normally and
    this case goes red.
    """
    root = tmp_path / "shared-root"
    root.mkdir(parents=True, exist_ok=True)

    first = _boot_server(root, "server-a.log")
    try:
        assert _health_status(first.base_url) == 200, (
            "the first server must be healthy before the second one starts"
        )

        started = time.monotonic()
        second = _boot_server(root, "server-b.log", wait_ready=False)
        try:
            returncode = second.wait_for_exit(timeout=_REFUSAL_CEILING_SEC)
            elapsed = time.monotonic() - started
            log_text = second.log_text()
        finally:
            _teardown(second)

        assert returncode is not None, (
            f"the second server did not exit within {_REFUSAL_CEILING_SEC}s; "
            f"its log was:\n{log_text}"
        )
        assert returncode != 0, (
            f"the refused server must exit non-zero; it exited {returncode}; "
            f"its log was:\n{log_text}"
        )
        assert f"holder_pid={first.proc.pid}" in log_text, (
            f"the refusal must name the holder's pid ({first.proc.pid}); "
            f"the log was:\n{log_text}"
        )
        assert "PDT_PLANS_DIR" in log_text, (
            f"the refusal must tell the operator what to do; "
            f"the log was:\n{log_text}"
        )
        assert _health_status(first.base_url) == 200, (
            "the first server must be unharmed by the refusal"
        )
    finally:
        _teardown(first)


# ---------------------------------------------------------------------------
# 2. Two servers on different roots both come up
# ---------------------------------------------------------------------------


def test_two_servers_on_different_roots_both_come_up(tmp_path):
    """The guard must not refuse a root that nobody holds.

    A guard that refused everything would satisfy the case above and
    make the platform unusable — and the two servers here really are
    independent, so both must answer ``/health``.
    """
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir(parents=True, exist_ok=True)
    root_b.mkdir(parents=True, exist_ok=True)

    first = _boot_server(root_a, "server-a.log")
    second = None
    try:
        second = _boot_server(root_b, "server-b.log")
        assert _health_status(first.base_url) == 200, (
            f"the first server must be healthy; its log was:\n{first.log_text()}"
        )
        assert _health_status(second.base_url) == 200, (
            f"the second server must be healthy; its log was:\n{second.log_text()}"
        )
    finally:
        _teardown(second)
        _teardown(first)

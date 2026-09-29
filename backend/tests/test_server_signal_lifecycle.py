"""SIGTERM/SIGINT lifecycle regression tests (2026-09-14).

The backend server must actually die when it is signalled — a supervisor or
a watchdog that cannot stop the process is not a watchdog. Two
independent defects made ``kill`` a no-op on a live server:

1. **Duplicate module execution clobbers uvicorn's handler.**
   ``python -m backend.server`` runs the body as ``__main__``, and
   ``provider_order._default_order_file()`` used to do a lazy ``from
   server import PROVIDER_ORDER_FILE`` from *inside the lifespan
   startup*. That executed the whole body a second time, under the name
   ``server``, about a second after ``uvicorn.run()`` had installed
   ``Server.handle_exit`` — so the module-level ``signal.signal(SIGTERM,
   _signal_handler)`` replaced it. uvicorn never learned about the
   signal and the event loop kept serving (main thread parked in
   ``kevent`` under ``sample(1)``).
2. **The handler swallowed the signal.** ``_signal_handler`` ended with
   ``signal.signal(signum, signal.SIG_DFL)``. Restoring the default
   disposition only affects *future* deliveries of that signal; the one
   already in flight is consumed by the handler. So even when our
   handler was the active one, the process stayed alive and a *second*
   SIGTERM is what finally killed it.

The observable symptom is flat: ``kill <pid>`` writes the ``received
SIGTERM`` line to ``server.log`` and changes nothing else — no
``shutdown begin``, no ``shutdown complete``, no exit — so the lifespan
``finally`` (heartbeat monitor stop, feishu notifier stop,
``_shutdown_all_executions``) never runs.
"""

from __future__ import annotations

import ast
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Same idiom as tests/test_sub_agent_watchdog.py — pytest's rootdir
# config puts ``backend/`` on sys.path, so the flat module name works.
import server  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent

_SIGNALLED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


@pytest.fixture
def signal_state():
    """Snapshot / restore the process-wide signal-handler state.

    ``_install_signal_handlers`` mutates three process-global things:
    the real signal dispositions, the ``sys`` install sentinel, and
    ``server._DISPLACED_SIGNAL_HANDLERS``. Without this restore one
    test's install would leak into every later test in the session.
    """
    saved_handlers = {sig: signal.getsignal(sig) for sig in _SIGNALLED}
    saved_displaced = dict(server._DISPLACED_SIGNAL_HANDLERS)
    had_sentinel = hasattr(sys, "_ac_signal_handlers_installed")
    saved_sentinel = getattr(sys, "_ac_signal_handlers_installed", None)
    try:
        yield
    finally:
        for sig, handler in saved_handlers.items():
            signal.signal(sig, handler)
        server._DISPLACED_SIGNAL_HANDLERS.clear()
        server._DISPLACED_SIGNAL_HANDLERS.update(saved_displaced)
        if had_sentinel:
            sys._ac_signal_handlers_installed = saved_sentinel
        elif hasattr(sys, "_ac_signal_handlers_installed"):
            del sys._ac_signal_handlers_installed


# ---------------------------------------------------------------------------
# Installation is once per process
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInstallIsOncePerProcess:
    def test_second_install_does_not_clobber_a_later_handler(
        self, signal_state, monkeypatch
    ):
        """The regression: a duplicate body execution must not replace
        the handler uvicorn installed *after* the first one."""
        monkeypatch.delattr(sys, "_ac_signal_handlers_installed", raising=False)
        server._DISPLACED_SIGNAL_HANDLERS.clear()

        server._install_signal_handlers()          # body execution #1
        assert signal.getsignal(signal.SIGTERM) is server._signal_handler

        def uvicorn_handle_exit(signum, frame):    # uvicorn.run → capture_signals
            pass

        signal.signal(signal.SIGTERM, uvicorn_handle_exit)

        server._install_signal_handlers()          # body execution #2 (the bug)

        assert signal.getsignal(signal.SIGTERM) is uvicorn_handle_exit, (
            "a duplicate execution of server.py replaced uvicorn's SIGTERM "
            "handler — that is what made kill/pkill a no-op"
        )

    def test_install_is_skipped_when_the_process_sentinel_is_set(
        self, signal_state, monkeypatch
    ):
        monkeypatch.setattr(
            sys, "_ac_signal_handlers_installed", True, raising=False
        )
        monkeypatch.setattr(
            server, "_DISPLACED_SIGNAL_HANDLERS", {signal.SIGTERM: "untouched"}
        )

        server._install_signal_handlers()

        assert server._DISPLACED_SIGNAL_HANDLERS[signal.SIGTERM] == "untouched"

    def test_install_records_the_handler_it_displaced(
        self, signal_state, monkeypatch
    ):
        monkeypatch.delattr(sys, "_ac_signal_handlers_installed", raising=False)
        server._DISPLACED_SIGNAL_HANDLERS.clear()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

        server._install_signal_handlers()

        assert signal.getsignal(signal.SIGTERM) is server._signal_handler
        assert server._DISPLACED_SIGNAL_HANDLERS[signal.SIGTERM] == signal.SIG_IGN


# ---------------------------------------------------------------------------
# The handler is transparent — it traces, it does not swallow
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerIsTransparent:
    def test_handler_defers_to_the_handler_it_displaced(self, monkeypatch):
        """When uvicorn owns shutdown, our handler must hand control
        back to it instead of deciding the process's fate itself."""
        seen = []

        def uvicorn_handle_exit(signum, frame):
            seen.append(signum)

        monkeypatch.setattr(
            server, "_DISPLACED_SIGNAL_HANDLERS",
            {signal.SIGTERM: uvicorn_handle_exit},
        )
        killed = []
        monkeypatch.setattr(
            server._os, "kill", lambda pid, sig: killed.append((pid, sig))
        )

        server._signal_handler(signal.SIGTERM, None)

        assert seen == [signal.SIGTERM]
        assert killed == [], (
            "deferring to uvicorn's handle_exit is the graceful path; "
            "killing here would skip the lifespan teardown"
        )

    def test_handler_redelivers_the_signal_when_it_has_nobody_to_defer_to(
        self, monkeypatch
    ):
        """Restoring SIG_DFL is not enough — the in-flight signal was
        already consumed by this handler, so the process would survive.
        It has to be re-delivered."""
        monkeypatch.setattr(
            server, "_DISPLACED_SIGNAL_HANDLERS",
            {signal.SIGTERM: signal.SIG_DFL},
        )
        dispositions = []
        monkeypatch.setattr(
            server.signal, "signal",
            lambda sig, handler: dispositions.append((sig, handler)),
        )
        killed = []
        monkeypatch.setattr(
            server._os, "kill", lambda pid, sig: killed.append((pid, sig))
        )

        server._signal_handler(signal.SIGTERM, None)

        assert dispositions == [(signal.SIGTERM, signal.SIG_DFL)]
        assert killed == [(server._os.getpid(), signal.SIGTERM)], (
            "without re-delivery the handler returns and the server keeps "
            "serving — the 2026-09-14 swallowed-SIGTERM bug"
        )

    def test_handler_ignores_a_displaced_reference_to_itself(self, monkeypatch):
        """Self-reference must not recurse; fall through to termination."""
        monkeypatch.setattr(
            server, "_DISPLACED_SIGNAL_HANDLERS",
            {signal.SIGTERM: server._signal_handler},
        )
        killed = []
        monkeypatch.setattr(
            server._os, "kill", lambda pid, sig: killed.append((pid, sig))
        )

        server._signal_handler(signal.SIGTERM, None)

        assert killed == [(server._os.getpid(), signal.SIGTERM)]


# ---------------------------------------------------------------------------
# The trace line reports what is *in flight*, not what merely has a record
# (2026-09-18)
# ---------------------------------------------------------------------------


def _signal_trace(caplog) -> str:
    """Return the rendered ``received SIGTERM ...`` trace line."""
    for record in caplog.records:
        message = record.getMessage()
        if "in_flight_executions=" in message:
            return message
    raise AssertionError(
        f"no in_flight_executions trace line in {[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.unit
class TestInFlightTraceIsFiltered:
    """``_execution_state`` is a record cache, not an in-flight set.

    ``_recover_execution_states`` re-seeds it from ``plan_execution`` for
    *any* status on every boot and nothing ever evicts an entry, so
    dumping ``list(_execution_state.keys())`` under an ``in_flight_executions``
    label reported three long-finished plans while every filtered consumer
    (``/api/system/active``, the scheduler) correctly reported zero.
    """

    @staticmethod
    def _defer(monkeypatch):
        """Stop ``_signal_handler`` from killing the test process."""
        monkeypatch.setattr(
            server, "_DISPLACED_SIGNAL_HANDLERS",
            {signal.SIGTERM: lambda signum, frame: None},
        )

    def test_terminal_records_are_not_reported_as_in_flight(
        self, monkeypatch, caplog
    ):
        self._defer(monkeypatch)
        monkeypatch.setattr(server, "_execution_state", {
            "plan-done": {
                "status": "completed", "ended_at": "2026-09-18T00:00:00", "pid": 90005,
            },
            "plan-failed": {
                "status": "failed", "ended_at": "2026-09-18T00:00:00", "pid": None,
            },
            "plan-live": {"status": "running", "ended_at": None},
        })
        monkeypatch.setattr(server, "_verification_state", {
            "plan-past": {"verification_status": "passed"},
            # ``repairing`` is active work — the old inline filter only
            # recognised ``running`` and under-reported this one.
            "plan-repair": {"verification_status": "repairing"},
        })

        with caplog.at_level(logging.WARNING):
            server._signal_handler(signal.SIGTERM, None)

        trace = _signal_trace(caplog)
        assert "'plan-live'" in trace
        assert "'plan-repair'" in trace
        assert "plan-done" not in trace
        assert "plan-failed" not in trace
        assert "plan-past" not in trace

    def test_trace_reports_empty_when_nothing_is_running(
        self, monkeypatch, caplog
    ):
        """The terminal-records-only shape: nothing is running."""
        self._defer(monkeypatch)
        monkeypatch.setattr(server, "_execution_state", {
            f"plan-{i}": {"status": s, "ended_at": "2026-09-18T00:00:00", "pid": 1000 + i}
            for i, s in enumerate(("completed", "failed", "failed"))
        })
        monkeypatch.setattr(server, "_verification_state", {})

        with caplog.at_level(logging.WARNING):
            server._signal_handler(signal.SIGTERM, None)

        trace = _signal_trace(caplog)
        assert "in_flight_executions=[]" in trace
        assert "verifying_plans=[]" in trace

    def test_every_in_flight_trace_site_uses_the_predicates(self):
        """Source guard for the two sites.

        The SIGTERM site is exercised behaviourally above; the lifespan
        ``shutdown begin`` site sits inside an async generator and is not
        directly callable, so it is pinned structurally — the same idiom
        ``tests/test_inner_timeout_removed.py`` uses for
        ``_run_single_vp_async``.
        """
        tree = ast.parse(
            (BACKEND_DIR / "server.py").read_text(encoding="utf-8")
        )
        sites = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "warning"
            and "in_flight_executions=" in ast.unparse(node)
        ]

        assert len(sites) >= 2, (
            f"expected at least the shutdown-begin and SIGTERM trace "
            f"sites, found {len(sites)}"
        )
        for text in sites:
            assert "_is_execution_in_flight" in text, text
            assert "_is_verification_in_flight" in text, text
            assert "list(_execution_state.keys())" not in text, (
                f"raw key dump reintroduced under an in-flight label: {text}"
            )


# ---------------------------------------------------------------------------
# End-to-end: a signalled server process must exit
# ---------------------------------------------------------------------------

_BOOT_AND_SIGNAL_SNIPPET = """
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

import server  # installs the tracing handler (body execution #1)


@asynccontextmanager
async def lifespan(app):
    # Simulate the duplicate execution of server.py's module body that
    # the lazy ``from server import PROVIDER_ORDER_FILE`` used to
    # trigger from inside the lifespan startup. On the pre-fix code this
    # replaced uvicorn's SIGTERM handler and the process never exited.
    server._install_signal_handlers()
    yield


app = FastAPI(lifespan=lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]))
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


@pytest.mark.unit
class TestSigtermTerminatesTheServerProcess:
    def test_a_signalled_server_process_exits(self, tmp_path):
        """``kill`` must stop the server. Before the fix the process
        logged the signal and kept serving until a second SIGTERM."""
        port = _free_port()
        script = tmp_path / "boot_and_signal.py"
        script.write_text(_BOOT_AND_SIGNAL_SNIPPET, encoding="utf-8")

        # The script lives in tmp_path, so its own directory (not the
        # repo) is sys.path[0]. Mirror what ``python -m backend.server``
        # from the repo root gives the real process: the repo root for
        # the ``backend.*`` imports and ``backend/`` for the flat ones
        # (``import server``, ``from prompts import ...``).
        repo_root = BACKEND_DIR.parent
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = os.pathsep.join(
            [str(BACKEND_DIR), str(repo_root), child_env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)

        proc = subprocess.Popen(
            [sys.executable, str(script), str(port)],
            cwd=str(BACKEND_DIR),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            assert _wait_for_port(port, timeout=90), (
                "server never came up on port "
                f"{port}: {proc.stdout.read() if proc.stdout else ''}"
            )

            proc.send_signal(signal.SIGTERM)
            try:
                returncode = proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "SIGTERM did not terminate the server within 20s — the "
                    "signal is being swallowed (see module docstring)"
                )

            assert returncode in (0, -signal.SIGTERM), (
                f"unexpected exit code {returncode} after SIGTERM"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

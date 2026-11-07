"""VP-008's acceptance closure: the **mcode-side** service, after a relaunch.

The failure this pins
---------------------
The plan under repair declares two services: the default ``api`` (the
claude side) and ``api-mcode`` (the mcode side).  ``api-mcode``'s
``start_cmd`` is not the claude one with a different port — it carries
``export PDT_HARNESS=mcode`` and points at the mcode side's own
``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH``.  Those three lines are the
*whole* difference between "the mcode backend" and "a server that
happens to answer on that port".

In round 3 the mcode service (pid 38714) was SIGTERM'd at 01:25:26,
fifteen seconds after ``services_ensured`` had put it on 63651 (its
declared port 8231 was held by a stranger, so the round relocated it).
The api_test for VP-008 ran at 01:32:56 against
``http://127.0.0.1:63651/health`` and got ``Connection refused``.  The
only surviving evidence for that endpoint was VP-011's *side
observation*: it restarted 63651 by hand with the plan's own verbatim
``start_cmd`` — the one carrying ``export PDT_HARNESS=mcode`` — and saw
``200 {"status": "ok"}``.  That is not the framework's relaunch: it was
a process the framework did not own, started by a hand, so this
verification point's own assertion never ran.

Why "the service answers" is not enough
---------------------------------------
A restart that drops ``export PDT_HARNESS=mcode`` produces a process
that still answers ``/health`` with ``200 {"status": "ok"}``.  The
criterion would go green while the mcode backend was *not* running —
which is exactly the "configured but not actually in effect" case the
hard constraints call an acceptance failure, and exactly the silent
downgrade ("it runs, that will do") that PRD decision point 1 forbids.

So this test does not accept "the port answers".  It accepts "the port
answers **and** the process that answers it is the mcode backend",
which is read out of the operating system: the relaunched process's
own environment must still carry ``PDT_HARNESS=mcode``.

What this test does
-------------------
It reproduces that timeline against the **real** backend server and
asserts the criterion where it was never asserted:

  1. the round declares both services — the claude one and the mcode
     one — exactly as the plan does, and both come up;
  2. the mcode process's own environment carries ``PDT_HARNESS=mcode``
     and the mcode-side paths.  This is the *control*: it proves the
     declaration says what the rest of the test assumes, and it proves
     the environment reader works at all;
  3. the criterion is observable before anything breaks: ``GET /health``
     answers 200 with ``{"status": "ok"}``;
  4. the mcode service is killed with SIGTERM, exactly as the
     cross-plan sweep killed it, and the port is confirmed free;
  5. the api_test VP runs anyway and must be graded **PASSED** — the
     framework's own relaunch puts the service back **on the same port**
     before the request;
  6. the relaunched process is the mcode backend: its environment still
     carries ``PDT_HARNESS=mcode`` and the mcode-side paths, and the
     claude service was neither restarted nor reused;
  7. and the negative control: a relaunch that comes up **without**
     ``PDT_HARNESS=mcode`` must be graded **FAILED**, not PASSED.  The
     service answers ``/health`` in that case too — that is the point.
     A framework that graded it PASSED would be grading the endpoint
     and ignoring the backend.

The declaration is copied from the plan's own ``services`` block rather
than invented here, including the ``unset PDT_SECRET_FD_*`` step: those
environment variables name descriptors belonging to the *parent* process,
and a child that inherits the names without the descriptors reads
whatever now occupies that number.  In this repository that took
uvicorn's listening socket down with it (``OSError: [Errno 9] Bad file
descriptor`` inside the event loop) — the plan's declaration drops the
names for exactly that reason.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import service_manager  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402
from service_freshness import probe_port  # noqa: E402
from service_manager import ServiceRuntimeMap, ensure_services  # noqa: E402


#: The backend the mcode-side declaration pins.  Written as a literal
#: rather than imported: the test asserts what the *declaration* says,
#: and a typo in the framework's constant must not become the
#: expectation.
MCODE_HARNESS = "mcode"

#: The variable that selects the backend.  Named by the criterion, so it
#: is spelled out here too.
HARNESS_ENV_VAR = "PDT_HARNESS"

#: The mcode side's own paths.  A relaunch that used the claude
#: declaration would come up with the claude side's paths, and the
#: service answering the port would be reading the wrong state.
PLANS_ENV_VAR = "PDT_PLANS_DIR"
STATE_DB_ENV_VAR = "PDT_STATE_DB_PATH"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_cmd(
    port: int, plans_dir: Path, db_path: Path, harness: Optional[str] = None,
) -> str:
    """The plan's own ``start_cmd`` shape, pointed at this test's paths.

    Copied from the plan's ``services`` block so the thing under test is
    the same thing the round started: the real backend server, told which
    port to bind, which plans tree to use, which SQLite database is its
    data plane and — for the mcode side — **which backend it runs under**.

    ``harness`` is the only difference between the two declarations, and
    it is the difference the whole test is about.
    """
    harness_export = (
        f"export {HARNESS_ENV_VAR}={harness}; " if harness else ""
    )
    return (
        # The parent's secret descriptors must not leak into the child:
        # the names survive `exec`, the descriptors do not.
        "for v in $(env | grep '^PDT_SECRET_FD_' | cut -d= -f1); do "
        'unset "$v"; done; '
        # The provider mode is pinned here rather than trusted from the
        # machine the suite happens to run on: the child inherits this
        # process's environment, and the hermetic env pins
        # ``PDT_PROVIDER_MODE=cc-switch`` for the pytest process.  CC
        # Switch is a plugin of one operator's machine — on a clean
        # runner nothing is listening on its port, so the child's
        # startup aborts ("Application startup failed") and the service
        # never comes up.  The service under test must not depend on
        # whether the machine it runs on happens to have CC Switch.
        "export PDT_PROVIDER_MODE=inherit; "
        f"{harness_export}"
        f"export PDT_PORT={port}; "
        f"export {PLANS_ENV_VAR}={plans_dir}; "
        f"export {STATE_DB_ENV_VAR}={db_path}; "
        "export PYTHONPATH=$PWD/..; "
        f"mkdir -p {plans_dir}; "
        "source .venv/bin/activate; exec python server.py"
    )


def _get_json(url: str) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as response:
        assert response.status == 200, (url, response.status)
        return json.loads(response.read().decode("utf-8"))


def _kill_service(pid: Optional[int]) -> None:
    """SIGTERM the service's process group, the way the sweep did.

    The service is started detached (``start_new_session=True``), so it
    is a process-group leader and the group is the unit that can be
    signalled.
    """
    if not pid:
        return
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _wait_until_port_free(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not probe_port(port).listening:
            return
        time.sleep(0.2)
    raise AssertionError(f"port {port} is still held {timeout}s after SIGTERM")


def _read_process_env(pid: int) -> Dict[str, str]:
    """The environment of *pid*, read from the operating system.

    Deliberately written here rather than imported from the framework:
    the test must not grade the framework with the framework's own
    reader — a reader that answers "no environment" would make the
    framework's check and the test's check agree on nothing.

    Linux has ``/proc/<pid>/environ``; macOS/BSD append the environment
    to the command line in ``ps -E``.  The command is stripped off
    first, so a ``KEY=value`` that belongs to the *command line* is not
    mistaken for an environment entry.
    """
    environ = Path("/proc") / str(int(pid)) / "environ"
    if environ.exists():
        try:
            raw = environ.read_bytes()
        except OSError:
            return {}
        env: Dict[str, str] = {}
        for chunk in raw.split(b"\0"):
            text = chunk.decode("utf-8", errors="replace")
            key, sep, value = text.partition("=")
            if sep and key:
                env[key] = value
        return env

    def _ps(argv: list) -> str:
        try:
            out = subprocess.run(
                argv, capture_output=True, text=True, timeout=10,
            )
            return out.stdout
        except Exception:  # noqa: BLE001 - the reader is best-effort
            return ""

    listing = _ps(["ps", "-ww", "-E", "-p", str(int(pid))])
    line = None
    for candidate in listing.splitlines():
        tokens = candidate.split()
        if tokens and tokens[0] == str(int(pid)):
            line = candidate
            break
    if line is None:
        return {}

    command = _ps(["ps", "-ww", "-p", str(int(pid)), "-o", "command="]).strip()
    env_part = line
    if command and command in line:
        env_part = line.split(command, 1)[1]

    env = {}
    for token in env_part.split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            continue
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        env[key] = value
    return env


def _vp_008(port: int) -> Dict[str, Any]:
    """VP-008 as the plan declares it — the criterion is not ours to move.

    The criterion is the mcode side's ``GET /health``: 200, and a body
    whose ``status`` is ``ok``.  Nothing about the backend the service
    runs under is in here — that is the framework's job to establish,
    and this test is what holds it to that.
    """
    return {
        "id": "VP-008",
        "title": "mcode 侧服务健康探针可用",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": f"http://127.0.0.1:{port}/health",
            "headers": {"Accept": "application/json"},
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            {"name": "健康状态为 ok", "json_path": "$.status", "equals": "ok"},
        ],
    }


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_relaunch_preserves_mcode_harness_env(tmp_path, monkeypatch):
    """The relaunched mcode service must be the mcode backend, or FAILED."""
    from verification_agent import VerificationAgent

    # Nothing in this process may leak a harness into the children: the
    # declarations are the only thing that is allowed to decide which
    # backend a service runs under.
    monkeypatch.delenv(HARNESS_ENV_VAR, raising=False)

    work = tmp_path / "pdt-verify-mcode"

    # The two sides of the round, each with its own tree and its own
    # state database — exactly as the plan declares them.
    claude_plans = work / "claude-plans"
    claude_plans.mkdir(parents=True)
    mcode_plans = work / "mcode-plans"
    mcode_plans.mkdir(parents=True)
    claude_db = work / "claude-state.db"
    mcode_db = work / "mcode-state.db"

    # The plan directory lives under the plans tree the mcode service is
    # told to use, exactly as in the round that failed — the boot sweep
    # scans that tree, and the ledger the relaunch writes lives in it.
    # The name is deliberately neutral: a real plan id is a private
    # artefact and this test needs nothing from it but a directory.
    plan_dir = mcode_plans / "plan-under-repair"
    plan_dir.mkdir()

    claude_port = _free_port()
    mcode_port = _free_port()
    claude_declaration = ServiceDeclaration(
        name="api",
        port=claude_port,
        start_cmd=_start_cmd(claude_port, claude_plans, claude_db),
        cwd="backend",
        health_url=f"http://127.0.0.1:{claude_port}/health",
        ready_timeout_seconds=60,
        log_path=str(work / "api_server.log"),
        reap_on_exit=True,
    )
    mcode_declaration = ServiceDeclaration(
        name="api-mcode",
        port=mcode_port,
        start_cmd=_start_cmd(
            mcode_port, mcode_plans, mcode_db, harness=MCODE_HARNESS,
        ),
        cwd="backend",
        health_url=f"http://127.0.0.1:{mcode_port}/health",
        ready_timeout_seconds=60,
        log_path=str(work / "api_mcode_server.log"),
        reap_on_exit=True,
    )

    # --- 1. both sides come up ------------------------------------------
    runtime_map = ensure_services(
        REPO_ROOT, {"api": claude_declaration, "api-mcode": mcode_declaration},
    )
    claude_runtime = runtime_map.runtimes["api"]
    mcode_runtime = runtime_map.runtimes["api-mcode"]
    assert claude_runtime.ready, claude_runtime.detail
    assert mcode_runtime.ready, mcode_runtime.detail
    claude_pid = claude_runtime.pid
    mcode_pid = mcode_runtime.pid
    assert claude_pid
    assert mcode_pid

    agent = None
    try:
        # --- 2. the control: the mcode process really is the mcode ------
        # If this does not hold, every later assertion about the
        # relaunched process would be vacuous — and the test would be
        # asserting nothing about the harness at all.
        mcode_env = _read_process_env(mcode_pid)
        assert mcode_env.get(HARNESS_ENV_VAR) == MCODE_HARNESS, mcode_env
        assert mcode_env.get(PLANS_ENV_VAR) == str(mcode_plans), mcode_env
        assert mcode_env.get(STATE_DB_ENV_VAR) == str(mcode_db), mcode_env

        # The claude side pins no harness, and says so.
        claude_env = _read_process_env(claude_pid)
        assert HARNESS_ENV_VAR not in claude_env, claude_env

        # --- 3. the criterion is observable before anything breaks -------
        payload = _get_json(f"http://127.0.0.1:{mcode_port}/health")
        assert payload == {"status": "ok"}, payload

        # --- 4. the mcode service dies, the way the sweep killed it -----
        _kill_service(mcode_pid)
        _wait_until_port_free(mcode_port)

        # The round's runtime map still points at the dead process: that
        # is exactly the state the api_test path used to meet.
        agent = VerificationAgent(
            plan_dir=plan_dir, project_dir=REPO_ROOT, coding_tool=None,
        )
        agent._service_declarations = {
            "api": claude_declaration,
            "api-mcode": mcode_declaration,
        }
        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={
                "api": claude_runtime,
                "api-mcode": mcode_runtime,
            }
        )

        events: list = []
        monkeypatch.setattr(
            agent,
            "_safe_log_freshness_event",
            lambda vp_id, event_type, data: events.append(
                (vp_id, event_type, data)
            ),
        )

        # --- 5. the verification point runs, and must pass --------------
        verdict = agent._run_api_test_vp(_vp_008(mcode_port))

        assert verdict["status"] == "PASSED", verdict

        relaunched = [
            event for event in events
            if event[1] == "service_relaunched_before_vp"
        ]
        assert len(relaunched) == 1, events
        vp_id, event_type, data = relaunched[0]
        assert vp_id == "VP-008"
        assert event_type == "service_relaunched_before_vp"
        assert data["service"] == "api-mcode", data
        assert data["port"] == mcode_port
        assert data["old_pid"] == mcode_pid
        assert data["new_pid"] not in (None, mcode_pid)

        # --- 6. the relaunched process is the mcode backend -------------
        new_pid = data["new_pid"]
        new_env = _read_process_env(new_pid)
        assert new_env.get(HARNESS_ENV_VAR) == MCODE_HARNESS, new_env
        # The mcode side's own paths: a relaunch that had used the
        # claude declaration would be reading the other side's state.
        assert new_env.get(PLANS_ENV_VAR) == str(mcode_plans), new_env
        assert new_env.get(STATE_DB_ENV_VAR) == str(mcode_db), new_env

        # The claude service was neither restarted nor reused: its pid is
        # the one the round started, and it still answers.
        assert claude_runtime.pid == claude_pid, claude_runtime
        claude_probe = probe_port(claude_port)
        assert claude_probe.listening
        assert claude_pid in claude_probe.pids, (claude_pid, claude_probe.pids)

        # The process answering the mcode port is the *new* one.
        mcode_probe = probe_port(mcode_port)
        assert mcode_probe.listening
        assert new_pid in mcode_probe.pids, (new_pid, mcode_probe.pids)

        # The criterion itself, read out of the audit artifact the
        # framework wrote.
        artifact_path = (
            plan_dir / "vp_artifacts" / "VP-008" / "api_response.json"
        )
        assert artifact_path.exists(), "the runner wrote no audit artifact"
        artifact = json.loads(artifact_path.read_text())
        assert artifact["status_code"] == 200, artifact
        assert not artifact["assertions_failed"], artifact["assertions_failed"]
        assert json.loads(artifact["response_body"]) == {"status": "ok"}

        # --- 7. the negative control ------------------------------------
        # A relaunch that comes up **without** the mcode harness still
        # answers /health with 200 {"status": "ok"}.  Grading that
        # PASSED would be grading the endpoint and ignoring the backend
        # — the silent downgrade the hard constraints forbid.  It has to
        # be FAILED.
        _kill_service(new_pid)
        _wait_until_port_free(mcode_port)

        real_ensure = service_manager.ensure_services

        def _ensure_without_harness(project_dir, declarations, **kwargs):
            """Start the declared service *without* the mcode harness."""
            stripped = {}
            for name, decl in declarations.items():
                stripped[name] = dataclasses.replace(
                    decl,
                    start_cmd=decl.start_cmd.replace(
                        f"export {HARNESS_ENV_VAR}={MCODE_HARNESS}; ", "",
                    ),
                )
            return real_ensure(project_dir, stripped, **kwargs)

        monkeypatch.setattr(
            service_manager, "ensure_services", _ensure_without_harness,
        )

        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={
                "api": claude_runtime,
                "api-mcode": mcode_runtime,
            }
        )

        degraded = agent._run_api_test_vp(_vp_008(mcode_port))

        assert degraded["status"] == "FAILED", degraded
        reasons = " ".join(str(r) for r in (degraded.get("reasons") or []))
        assert HARNESS_ENV_VAR in reasons, degraded
        assert MCODE_HARNESS in reasons, degraded
    finally:
        if agent is not None:
            current = agent._service_runtime_map
            if current is not None:
                for entry in (current.runtimes or {}).values():
                    _kill_service(getattr(entry, "pid", None))
        _kill_service(mcode_pid)
        _kill_service(claude_pid)

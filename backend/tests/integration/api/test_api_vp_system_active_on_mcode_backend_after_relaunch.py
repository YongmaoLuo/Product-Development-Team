"""VP-009's acceptance closure: the five-field contract on the **mcode** side.

The failure this pins
---------------------
VP-009's criterion is the **mcode side's** system overview:
``GET /api/system/active`` (with the ``X-PDT-Request`` guard header)
answers 200 with a JSON body carrying five fields —
``execution_running``, ``verification_running``, ``total_active``,
``details`` and ``paused_executions``.

In round 3 the api_test for this VP ran at 01:32:56 against
``http://127.0.0.1:63651/api/system/active`` and got
``Connection refused``: no status code, no body, and not one of the five
fields observed.  The mcode service (pid 38714) had been SIGTERM'd at
01:25:26 — fifteen seconds after ``services_ensured`` had put it on
63651 — and nothing brought it back before the request.  VP-011's
recovery checked ``/health`` and ``/api/plans`` on the restored port;
this endpoint was never requested at all.

Why "the port answers" is not enough
------------------------------------
``/api/system/active`` is served by the *same* server code on both
sides of the round.  A claude backend answers it just as well as an
mcode one — with the same five fields, and, on a freshly started
server, with the same "nothing is running" counts.  So the five-field
contract on its own cannot tell the two apart: a VP satisfied by a
claude backend process would look exactly like a satisfied VP.

PRD decision point 1 says the mcode side must deliver the *same
execution contract* as the claude side, and that a missing capability
must fail loudly rather than be papered over.  Hard constraint 3 says
the same from the other side: the mcode path must not silently fall
back to the claude backend.  A criterion that a claude process could
satisfy would therefore be worth nothing.

What this test does
-------------------
It reproduces that timeline against the **real** backend server and
asserts the criterion where it was never asserted:

  1. the round declares both sides — the claude one (``api``) and the
     mcode one (``api-mcode``, whose ``start_cmd`` carries
     ``export PDT_HARNESS=mcode`` and the mcode side's own
     ``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH``) — and both come up;
  2. the control: the mcode process really is the mcode backend (read
     out of the operating system), the claude process pins no harness,
     and the claude service answers the *same* endpoint with the *same*
     five fields but with nothing in flight — so the five-field answer
     this VP grades on cannot have come from the claude side;
  3. the mcode service is killed with SIGTERM, exactly as the
     cross-plan sweep killed it, and the port is confirmed free;
  4. the api_test VP runs anyway and must be graded **PASSED** — the
     framework's own relaunch (Leg B of the VP-001 fix) puts the
     mcode service back **on the same port** before the request;
  5. the relaunched process is the mcode backend: its environment still
     carries ``PDT_HARNESS=mcode`` and the mcode-side paths, and the
     claude service was neither restarted nor reused;
  6. the five fields are read out of the audit artifact the runner
     wrote, one by one, and each one's *structure* is checked — not
     merely that the body mentions the five names;
  7. and the negative control: a relaunch that comes up **without**
     ``PDT_HARNESS=mcode`` must be graded **FAILED**, not PASSED —
     even though that service answers the endpoint with 200 and all
     five fields.  A framework that graded it PASSED would be grading
     the endpoint and ignoring the backend, which is exactly the
     silent downgrade the hard constraints forbid;
  8. and the same refusal for a service that was **already alive** and
     is not the mcode backend: nothing was relaunched there, so the
     confirmation has to be about the process that is answering the
     port rather than about the process the framework just started.
     The endpoint answers with 200 and the five fields from that
     process too — the response is indistinguishable from the mcode
     one's, so only the process can tell the two apart.

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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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

#: The variable that selects the backend.  Named by the criterion, so
#: it is spelled out here too.
HARNESS_ENV_VAR = "PDT_HARNESS"

#: The mcode side's own paths.  A relaunch that used the claude
#: declaration would come up with the claude side's paths, and the
#: service answering the port would be reading the wrong state.
PLANS_ENV_VAR = "PDT_PLANS_DIR"
STATE_DB_ENV_VAR = "PDT_STATE_DB_PATH"


#: The plan whose verification round the mcode server recovers at boot.
#: Its name is deliberately neutral: a real plan id is a private
#: artefact and this test needs nothing from it but a directory.
SEEDED_PLAN_ID = "vp009-seed"

#: The plan carrying a pause checkpoint, so ``paused_executions`` has
#: something real to report.
PAUSED_PLAN_ID = "vp009-paused"

#: The round number the recovered verification reports.  Two, so an
#: implementation that hard-codes "round 0" cannot pass by accident.
SEEDED_ROUND = 2

#: The five fields the criterion names.  A response missing any one of
#: them has broken the contract, whatever else it carries.
CONTRACT_FIELDS = (
    "execution_running",
    "verification_running",
    "total_active",
    "details",
    "paused_executions",
)

#: The keys a ``details`` entry carries, per the endpoint's contract.
DETAIL_KEYS = frozenset({
    "plan_id", "type", "status", "round", "started_at", "updated_at",
})

#: The keys a ``paused_executions`` entry carries, per the endpoint's
#: contract.
PAUSED_KEYS = frozenset({
    "plan_id", "kind", "paused_at", "interrupted_task_count",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nothing is listening on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed_state_db(db_path: Path, plans_dir: Path) -> None:
    """Give the server a state database with one verification in flight.

    The row is written through :class:`VerificationRepository` — the same
    path the framework writes it on — and the plan gets a directory under
    the plans tree, because the server's boot-time recovery walks the
    plan *directories* and asks the database about each one.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        repo.insert(
            SEEDED_PLAN_ID,
            "running",
            round=SEEDED_ROUND,
            max_rounds=3,
            runtime_state={"round": SEEDED_ROUND},
            started_at="2026-10-25T00:00:00Z",
        )
        conn.commit()
    finally:
        conn.close()

    (plans_dir / SEEDED_PLAN_ID).mkdir(parents=True, exist_ok=True)


def _seed_pause_checkpoint(plans_dir: Path) -> None:
    """Park one plan with a pause checkpoint on disk.

    ``paused_executions`` is read from the plan directories rather than
    from memory — it is the half of the overview that survives a
    restart — so the checkpoint is what makes that field real.
    """
    from pause_state import write_pause_checkpoint

    write_pause_checkpoint(
        plans_dir / PAUSED_PLAN_ID,
        kind="execution",
        project_dir=None,
        reason="user_requested",
        interrupted_tasks=[{"task_id": "T-001", "status": "in_progress"}],
        unfinished_tasks=[],
    )


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


def _get_json(url: str) -> Tuple[int, Dict[str, Any]]:
    """GET ``url`` with the guard header and return ``(status, body)``."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "X-PDT-Request": "1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status), json.loads(
                response.read().decode("utf-8")
            )
    except urllib.error.HTTPError as exc:
        return int(exc.code), {}


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


def _vp_009(port: int) -> Dict[str, Any]:
    """VP-009 as the criterion states it — the criterion is not ours to move.

    Each of the five fields is asserted **by path**, so a body that
    merely mentions the name is not what passes this.  The counts and
    the two lists' shapes are asserted too: the endpoint is the system
    overview, and an overview that reports the wrong number is broken
    even though it carries all five keys.
    """
    url = f"http://127.0.0.1:{port}/api/system/active"
    return {
        "id": "VP-009",
        "title": "mcode 侧系统总览接口在服务重启后仍满足五字段契约",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": url,
            "headers": {
                "Accept": "application/json",
                # The request guard answers 403 without this header, so
                # the criterion names it: the VP must carry it.
                "X-PDT-Request": "1",
            },
            "body": None,
            "timeout_seconds": 30,
        },
        "assertions": [
            {"name": "状态码 200", "status": 200},
            # --- the five fields, one by one ---
            {
                "name": "execution_running 存在",
                "json_path": "$.execution_running", "exists": True,
            },
            {
                "name": "verification_running 存在",
                "json_path": "$.verification_running", "exists": True,
            },
            {
                "name": "total_active 存在",
                "json_path": "$.total_active", "exists": True,
            },
            {
                "name": "details 存在",
                "json_path": "$.details", "exists": True,
            },
            {
                "name": "paused_executions 存在",
                "json_path": "$.paused_executions", "exists": True,
            },
            # --- and their structure ---
            {
                "name": "没有执行中的执行轮次",
                "json_path": "$.execution_running", "equals": 0,
            },
            {
                "name": "有一个进行中的验证轮次",
                "json_path": "$.verification_running", "equals": 1,
            },
            {
                "name": "活跃总数与明细条数一致",
                "json_path": "$.total_active", "equals": 1,
            },
            {
                "name": "details 是一条明细",
                "json_path": "$.details", "length_equals": 1,
            },
            {
                "name": "details[0] 是验证类型",
                "json_path": "$.details[0].type", "equals": "verification",
            },
            {
                "name": "details[0] 是被恢复的 plan",
                "json_path": "$.details[0].plan_id", "equals": SEEDED_PLAN_ID,
            },
            {
                "name": "details[0] 状态是 running",
                "json_path": "$.details[0].status", "equals": "running",
            },
            {
                "name": "details[0] 轮次号正确",
                "json_path": "$.details[0].round", "equals": SEEDED_ROUND,
            },
            {
                "name": "paused_executions 是一条暂停记录",
                "json_path": "$.paused_executions", "length_equals": 1,
            },
            {
                "name": "paused_executions[0] 是被暂停的 plan",
                "json_path": "$.paused_executions[0].plan_id",
                "equals": PAUSED_PLAN_ID,
            },
            {
                "name": "paused_executions[0] 类型正确",
                "json_path": "$.paused_executions[0].kind", "equals": "execution",
            },
            {
                "name": "paused_executions[0] 统计了被中断的任务",
                "json_path": "$.paused_executions[0].interrupted_task_count",
                "equals": 1,
            },
        ],
    }


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_api_vp_system_active_on_mcode_backend_after_relaunch(
    tmp_path, monkeypatch,
):
    """The five-field contract must be observed on a confirmed mcode backend."""
    from verification_agent import VerificationAgent

    # Nothing in this process may leak a harness into the children: the
    # declarations are the only thing that is allowed to decide which
    # backend a service runs under.
    monkeypatch.delenv(HARNESS_ENV_VAR, raising=False)

    work = tmp_path / "pdt-verify-vp009"

    # The two sides of the round, each with its own tree and its own
    # state database — exactly as the plan declares them.
    claude_plans = work / "claude-plans"
    claude_plans.mkdir(parents=True)
    mcode_plans = work / "mcode-plans"
    mcode_plans.mkdir(parents=True)
    claude_db = work / "claude-state.db"
    mcode_db = work / "mcode-state.db"

    # Only the mcode side carries state: one verification round in flight
    # (recovered from the database at boot) and one plan parked with a
    # pause checkpoint (read from disk).  The claude side stays empty, so
    # the two answers are distinguishable — see step 2.
    _seed_state_db(mcode_db, mcode_plans)
    _seed_pause_checkpoint(mcode_plans)

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
    new_pid = None
    degraded_runtimes: list = []
    try:
        # --- 2. the control: which process is which ---------------------
        # If this does not hold, every later assertion about the
        # relaunched process would be vacuous — and the test would be
        # asserting nothing about the backend at all.
        mcode_env = _read_process_env(mcode_pid)
        assert mcode_env.get(HARNESS_ENV_VAR) == MCODE_HARNESS, mcode_env
        assert mcode_env.get(PLANS_ENV_VAR) == str(mcode_plans), mcode_env
        assert mcode_env.get(STATE_DB_ENV_VAR) == str(mcode_db), mcode_env

        # The claude side pins no harness, and says so.
        claude_env = _read_process_env(claude_pid)
        assert HARNESS_ENV_VAR not in claude_env, claude_env

        # The criterion is observable before anything breaks: the mcode
        # side answers the endpoint with the five fields and with the
        # state that was seeded into its own database.
        status, before = _get_json(
            f"http://127.0.0.1:{mcode_port}/api/system/active"
        )
        assert status == 200, status
        assert set(CONTRACT_FIELDS) <= set(before), before
        assert before["verification_running"] == 1, before
        assert before["total_active"] == 1, before
        assert len(before["details"]) == 1, before
        assert len(before["paused_executions"]) == 1, before

        # And the claude side answers the *same* endpoint with the *same*
        # five fields — the contract does not identify the backend.  What
        # distinguishes the two answers is the state: the claude side has
        # nothing in flight, so a VP graded on the claude side could
        # never report the seeded verification or the seeded pause.  That
        # is what makes the five-field assertion below a statement about
        # the mcode instance rather than about the endpoint.
        claude_status, claude_body = _get_json(
            f"http://127.0.0.1:{claude_port}/api/system/active"
        )
        assert claude_status == 200, claude_status
        assert set(CONTRACT_FIELDS) <= set(claude_body), claude_body
        assert claude_body["verification_running"] == 0, claude_body
        assert claude_body["total_active"] == 0, claude_body
        assert claude_body["details"] == [], claude_body
        assert claude_body["paused_executions"] == [], claude_body

        # --- 3. the mcode service dies, the way the sweep killed it ----
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

        # --- 4. the verification point runs, and must pass --------------
        verdict = agent._run_api_test_vp(_vp_009(mcode_port))

        assert verdict["status"] == "PASSED", verdict

        relaunched = [
            event for event in events
            if event[1] == "service_relaunched_before_vp"
        ]
        assert len(relaunched) == 1, events
        vp_id, event_type, data = relaunched[0]
        assert vp_id == "VP-009"
        assert event_type == "service_relaunched_before_vp"
        assert data["service"] == "api-mcode", data
        assert data["port"] == mcode_port
        assert data["old_pid"] == mcode_pid
        assert data["new_pid"] not in (None, mcode_pid)

        # --- 5. the relaunched process is the mcode backend -------------
        new_pid = data["new_pid"]
        new_env = _read_process_env(new_pid)
        assert new_env.get(HARNESS_ENV_VAR) == MCODE_HARNESS, new_env
        # The mcode side's own paths: a relaunch that had used the
        # claude declaration would be reading the other side's state.
        assert new_env.get(PLANS_ENV_VAR) == str(mcode_plans), new_env
        assert new_env.get(STATE_DB_ENV_VAR) == str(mcode_db), new_env

        # The process answering the mcode port is the *new* one.
        mcode_probe = probe_port(mcode_port)
        assert mcode_probe.listening
        assert new_pid in mcode_probe.pids, (new_pid, mcode_probe.pids)

        # The claude service was neither restarted nor reused: its pid is
        # the one the round started, and it still answers.  The default
        # claude path is unchanged by any of this.
        assert claude_runtime.pid == claude_pid, claude_runtime
        claude_probe = probe_port(claude_port)
        assert claude_probe.listening
        assert claude_pid in claude_probe.pids, (claude_pid, claude_probe.pids)

        # --- 6. the five fields, on that confirmed mcode instance -------
        # Read out of the audit artifact the runner wrote for this very
        # request — the request that went to the mcode port, after the
        # framework had established that the process answering that port
        # is the mcode backend.
        artifact_path = (
            plan_dir / "vp_artifacts" / "VP-009" / "api_response.json"
        )
        assert artifact_path.exists(), "the runner wrote no audit artifact"
        artifact = json.loads(artifact_path.read_text())
        assert artifact["status_code"] == 200, artifact
        assert not artifact["assertions_failed"], artifact["assertions_failed"]

        payload = json.loads(artifact["response_body"])

        # The five fields, one by one — the criterion, not a summary of it.
        for field in CONTRACT_FIELDS:
            assert field in payload, (field, payload)
        assert payload["execution_running"] == 0, payload
        assert payload["verification_running"] == 1, payload
        assert payload["total_active"] == 1, payload

        # The structure of the two lists, checked on the parsed body —
        # a body that merely mentions the five names cannot pass this.
        assert isinstance(payload["details"], list), payload
        assert len(payload["details"]) == 1, payload
        detail = payload["details"][0]
        assert isinstance(detail, dict), payload
        assert set(detail) == DETAIL_KEYS, detail
        assert detail["plan_id"] == SEEDED_PLAN_ID, detail
        assert detail["type"] == "verification", detail
        assert detail["status"] == "running", detail
        assert detail["round"] == SEEDED_ROUND, detail
        assert detail["started_at"], detail
        assert detail["updated_at"], detail

        assert isinstance(payload["paused_executions"], list), payload
        assert len(payload["paused_executions"]) == 1, payload
        paused = payload["paused_executions"][0]
        assert isinstance(paused, dict), payload
        assert set(paused) == PAUSED_KEYS, paused
        assert paused["plan_id"] == PAUSED_PLAN_ID, paused
        assert paused["kind"] == "execution", paused
        assert paused["interrupted_task_count"] == 1, paused
        assert paused["paused_at"], paused

        # The counters and the list agree with each other: the overview
        # is one answer, not five independent ones.
        assert payload["total_active"] == len(payload["details"]), payload
        assert payload["verification_running"] == sum(
            1 for entry in payload["details"] if entry["type"] == "verification"
        ), payload
        assert payload["execution_running"] == sum(
            1 for entry in payload["details"] if entry["type"] == "execution"
        ), payload

        # The same five fields, read live from the mcode port — and the
        # process answering that port is the mcode one, so the answer
        # cannot have come from the claude backend.
        live_status, live = _get_json(
            f"http://127.0.0.1:{mcode_port}/api/system/active"
        )
        assert live_status == 200, live_status
        for field in CONTRACT_FIELDS:
            assert field in live, (field, live)
        assert live["verification_running"] == 1, live
        assert live["total_active"] == 1, live
        assert len(live["details"]) == 1, live
        assert len(live["paused_executions"]) == 1, live

        # --- 7. the negative control ------------------------------------
        # A relaunch that comes up **without** the mcode harness still
        # answers /api/system/active with 200 and all five fields — the
        # endpoint is served by the same code on both sides.  Grading that
        # PASSED would be grading the endpoint and ignoring the backend —
        # the silent downgrade the hard constraints forbid.  It has to be
        # FAILED.
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
            result = real_ensure(project_dir, stripped, **kwargs)
            degraded_runtimes.append(result)
            return result

        monkeypatch.setattr(
            service_manager, "ensure_services", _ensure_without_harness,
        )

        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={
                "api": claude_runtime,
                "api-mcode": mcode_runtime,
            }
        )

        degraded = agent._run_api_test_vp(_vp_009(mcode_port))

        assert degraded["status"] == "FAILED", degraded
        reasons = " ".join(str(r) for r in (degraded.get("reasons") or []))
        assert HARNESS_ENV_VAR in reasons, degraded
        assert MCODE_HARNESS in reasons, degraded

        # --- 8. the alive-but-wrong-backend case -----------------------
        # The port answers, but the process answering it is a claude
        # backend: a service started from the declaration *without* the
        # mcode harness and left running.  Nothing was relaunched here,
        # so the framework has to confirm the backend of the process it
        # is about to grade on — the check the relaunch path does for
        # the process it starts.
        #
        # The endpoint answers with 200 and the five fields either way:
        # the contract cannot tell the two apart.  That is precisely why
        # the confirmation has to be about the *process*.  Grading this
        # PASSED would let a claude backend satisfy a criterion that is
        # about the mcode one — the "configured but mcode not actually
        # in effect" case the hard constraints call an acceptance
        # failure.
        _wait_until_port_free(mcode_port)

        degraded_map = service_manager.ensure_services(
            REPO_ROOT,
            {
                "api-mcode": dataclasses.replace(
                    mcode_declaration,
                    start_cmd=mcode_declaration.start_cmd.replace(
                        f"export {HARNESS_ENV_VAR}={MCODE_HARNESS}; ", "",
                    ),
                ),
            },
        )
        degraded_runtime = degraded_map.runtimes["api-mcode"]
        assert degraded_runtime.ready, degraded_runtime.detail
        assert degraded_runtime.port == mcode_port, degraded_runtime
        degraded_runtimes.append(degraded_map)

        # The endpoint really does answer with the five fields from this
        # process: the response alone would have satisfied the
        # criterion, which is what makes the framework's refusal the
        # only thing standing between the criterion and a claude
        # backend.
        alive_status, alive_body = _get_json(
            f"http://127.0.0.1:{mcode_port}/api/system/active"
        )
        assert alive_status == 200, alive_status
        assert set(CONTRACT_FIELDS) <= set(alive_body), alive_body

        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={
                "api": claude_runtime,
                "api-mcode": mcode_runtime,
            }
        )

        alive_verdict = agent._run_api_test_vp(_vp_009(mcode_port))

        assert alive_verdict["status"] == "FAILED", alive_verdict
        alive_reasons = " ".join(
            str(r) for r in (alive_verdict.get("reasons") or [])
        )
        assert HARNESS_ENV_VAR in alive_reasons, alive_verdict
        assert MCODE_HARNESS in alive_reasons, alive_verdict
    finally:
        if agent is not None:
            current = agent._service_runtime_map
            if current is not None:
                for entry in (current.runtimes or {}).values():
                    _kill_service(getattr(entry, "pid", None))
        for runtime_map_entry in degraded_runtimes:
            for entry in (runtime_map_entry.runtimes or {}).values():
                _kill_service(getattr(entry, "pid", None))
        _kill_service(new_pid)
        _kill_service(mcode_pid)
        _kill_service(claude_pid)

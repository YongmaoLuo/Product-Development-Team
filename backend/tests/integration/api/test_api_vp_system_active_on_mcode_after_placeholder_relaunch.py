"""VP-009's closure on the **mcode** side, through a **placeholder** URL.

The gap this pins
-----------------
VP-009 grades the mcode side's system overview::

    "url": "{{svc.api-mcode.url}}/api/system/active"

with the ``X-PDT-Request`` guard header: 200, a JSON content-type, and a
body carrying ``execution_running``, ``verification_running``,
``total_active`` and ``details``.

Round 3 ran that request against ``http://127.0.0.1:57494/api/system/active``
and got ``Connection refused``: no status code, no body, and not one of the
four fields observed.  The mcode service on that port (pid 739) had been
SIGTERM'd at 06:34:59 — twenty-eight seconds after the preflight had put it
there — and nothing brought it back before the request went out.  The
round's surviving evidence for this endpoint was a *neighbour's*
(``/health``, ``/api/plans``): a 200 on another endpoint says nothing
about the system overview.

Two framework defects had to be closed for the criterion to be observable
at all, and both are about the **placeholder**:

  1. ``_vp_target_port`` could not parse a port out of a placeholder, so
     ``_relaunch_service_for_vp`` gave up before even probing the port —
     a service killed between the preflight and the request stayed dead.
  2. even with the relaunch triggered, the *request* still carried the
     raw placeholder, and ``{{svc.api-mcode.url}}`` is not a URL, so it
     could never have reached the endpoint that had just been brought
     back up.

Why the four fields alone are worth nothing here
-------------------------------------------------
``/api/system/active`` is served by the **same** server code on both
sides of the round.  A claude backend answers it just as well as an
mcode one — with the same four field names, and, on a freshly started
server, with the same "nothing is running" counts.  So the four-field
contract on its own cannot tell the two apart: a VP satisfied by a
claude backend process would look exactly like a satisfied VP.

PRD decision point 1 says the mcode side must deliver the *same
execution contract* as the claude side, and that a missing capability
must fail loudly rather than be papered over.  Hard constraint 3 says
the same from the other side: the mcode path must not silently fall
back to the claude backend.  A criterion that a claude process could
satisfy would therefore be worth nothing — so this test does not accept
"the endpoint answered with the four fields", it accepts "the endpoint
answered with the four fields **on a process that is the mcode backend**".

What this test does
-------------------
Against the **real** backend server (the same ``python server.py`` the
plan's declaration starts):

  1. the round declares both sides — the claude one (``api``) and the
     mcode one (``api-mcode``, whose ``start_cmd`` carries
     ``export PDT_HARNESS=mcode`` and the mcode side's own
     ``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH``) — and both come up;
  2. the control: the mcode process really is the mcode backend (read
     out of the operating system), the claude process pins no harness,
     and the claude service answers the *same* endpoint with the *same*
     four fields but with nothing in flight — so the four-field answer
     this VP grades on cannot have come from the claude side;
  3. the mcode service is killed with SIGTERM, exactly as the cross-plan
     sweep killed it, and the port is confirmed free;
  4. the api_test VP — written the way the plan writes it, with the
     ``{{svc.api-mcode.url}}`` placeholder — runs anyway and must be
     graded **PASSED**: the framework's own relaunch puts the mcode
     service back **on the same port** before the request;
  5. the relaunched process is the mcode backend: its environment still
     carries ``PDT_HARNESS=mcode`` and the mcode-side paths, the event
     the round logged says **which command** was re-run, and the claude
     service was neither restarted nor reused;
  6. the criterion, read out of the audit artifact the runner wrote for
     this very request: 200, a JSON content-type, and the four fields —
     carrying the **mcode side's own state**, which is what makes the
     answer attributable to the mcode instance;
  7. and the negative control: a relaunch that comes up **without**
     ``PDT_HARNESS=mcode`` answers the endpoint with 200, a JSON
     content-type and the four fields — and with the *same* state, because
     it reads the same database — so the response is indistinguishable
     from the mcode one.  That relaunch must be graded **FAILED**, not
     PASSED.  A framework that graded it PASSED would be grading the
     endpoint and ignoring the backend, which is exactly the silent
     downgrade the hard constraints forbid;
  8. and the same refusal for the service that was **already alive** and
     is not the mcode backend: nothing was relaunched there, so the
     confirmation has to be about the process that is answering the port
     rather than about the process the framework just started.  That
     verdict has to name the condition the hard constraint names — the
     backend was *configured* but did not actually take effect — because
     a verdict that only says "that is not the declared backend" leaves
     the reader without the thing that has to be fixed.

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
from typing import Any, Dict, List, Optional, Tuple

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


#: The plan whose verification round the mcode server recovers at boot.
#: Its name is deliberately neutral: a real plan id is a private
#: artefact and this test needs nothing from it but a directory.
SEEDED_PLAN_ID = "vp009-mcode-placeholder-seed"

#: The round number the recovered verification reports.  Two, so an
#: implementation that hard-codes "round 0" cannot pass by accident.
SEEDED_ROUND = 2

#: The four fields the criterion names.  A response missing any one of
#: them has broken the contract, whatever else it carries.
CONTRACT_FIELDS = (
    "execution_running",
    "verification_running",
    "total_active",
    "details",
)

#: The keys a ``details`` entry carries, per the endpoint's contract.
DETAIL_KEYS = frozenset({
    "plan_id", "type", "status", "round", "started_at", "updated_at",
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


def _request(url: str) -> Tuple[int, Dict[str, str], Dict[str, Any]]:
    """GET ``url`` with the guard header; return ``(status, headers, body)``."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "X-PDT-Request": "1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
            headers = {k.lower(): v for k, v in response.headers.items()}
            return int(response.status), headers, json.loads(raw)
    except urllib.error.HTTPError as exc:
        return int(exc.code), {}, {}


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


def _vp_009() -> Dict[str, Any]:
    """VP-009 as the criterion states it — the criterion is not ours to move.

    The URL is the **placeholder** the plan writes, not a literal
    address: that is the whole point of this test, because the
    placeholder is what made the endpoint unobservable in the first
    place.  The assertions are the plan's own, verbatim — 200, a JSON
    content-type, and the four fields present.
    """
    return {
        "id": "VP-009",
        "title": "mcode 侧系统总览接口在服务重启后仍满足契约",
        "verification_method": "api_test",
        "request": {
            "method": "GET",
            "url": "{{svc.api-mcode.url}}/api/system/active",
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
            {
                "name": "响应为 JSON",
                "header": "content-type",
                "contains": "application/json",
            },
            {
                "name": "含 execution_running",
                "json_path": "$.execution_running", "exists": True,
            },
            {
                "name": "含 verification_running",
                "json_path": "$.verification_running", "exists": True,
            },
            {
                "name": "含 total_active",
                "json_path": "$.total_active", "exists": True,
            },
            {
                "name": "含 details",
                "json_path": "$.details", "exists": True,
            },
        ],
    }


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_system_active_on_mcode_after_placeholder_relaunch(
    tmp_path, monkeypatch,
):
    """The four-field contract must be observed on a confirmed mcode backend."""
    from verification_agent import VerificationAgent

    # Nothing in this process may leak a harness into the children: the
    # declarations are the only thing that is allowed to decide which
    # backend a service runs under.
    monkeypatch.delenv(HARNESS_ENV_VAR, raising=False)

    work = tmp_path / "pdt-verify-vp009-mcode"

    # The two sides of the round, each with its own tree and its own
    # state database — exactly as the plan declares them.
    claude_plans = work / "claude-plans"
    claude_plans.mkdir(parents=True)
    mcode_plans = work / "mcode-plans"
    mcode_plans.mkdir(parents=True)
    claude_db = work / "claude-state.db"
    mcode_db = work / "mcode-state.db"

    # Only the mcode side carries state: one verification round in
    # flight, recovered from the database at boot.  The claude side
    # stays empty, so the two answers are distinguishable — see step 2.
    _seed_state_db(mcode_db, mcode_plans)

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
    degraded_runtimes: List[Any] = []
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
        # side answers the endpoint with the four fields and with the
        # state that was seeded into its own database.
        status, headers, before = _request(
            f"http://127.0.0.1:{mcode_port}/api/system/active"
        )
        assert status == 200, status
        assert "application/json" in headers.get("content-type", ""), headers
        assert set(CONTRACT_FIELDS) <= set(before), before
        assert before["verification_running"] == 1, before
        assert before["total_active"] == 1, before
        assert len(before["details"]) == 1, before

        # And the claude side answers the *same* endpoint with the *same*
        # four fields — the contract does not identify the backend.  What
        # distinguishes the two answers is the state: the claude side has
        # nothing in flight, so a VP graded on the claude side could
        # never report the seeded verification.  That is what makes the
        # four-field assertion below a statement about the mcode instance
        # rather than about the endpoint.
        claude_status, claude_headers, claude_body = _request(
            f"http://127.0.0.1:{claude_port}/api/system/active"
        )
        assert claude_status == 200, claude_status
        assert "application/json" in claude_headers.get("content-type", ""), (
            claude_headers
        )
        assert set(CONTRACT_FIELDS) <= set(claude_body), claude_body
        assert claude_body["verification_running"] == 0, claude_body
        assert claude_body["total_active"] == 0, claude_body
        assert claude_body["details"] == [], claude_body

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
        verdict = agent._run_api_test_vp(_vp_009())

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

        # The event has to say **which command** was re-run.  The whole
        # difference between the mcode side and the claude side lives in
        # that command, so a round log that says "service relaunched" and
        # nothing about *what* was started cannot be checked afterwards.
        summary = str(data.get("start_cmd") or "")
        assert summary, data
        assert f"{HARNESS_ENV_VAR}={MCODE_HARNESS}" in summary, summary
        assert str(mcode_plans) in summary, summary
        assert str(mcode_db) in summary, summary

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

        # --- 6. the criterion, on that confirmed mcode instance ---------
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

        # The request that produced the artifact must be the *resolved*
        # one — the raw placeholder is not a URL and could not have been
        # requested at all — and it must be the mcode side's address.
        request_url = artifact["request"]["url"]
        assert "{{" not in request_url, artifact["request"]
        assert f":{mcode_port}/" in request_url, request_url

        # The content-type is part of the criterion, not a detail: the
        # consumer of this endpoint parses it as JSON.
        response_headers = artifact["response_headers"]
        assert "content-type" in response_headers, response_headers
        assert "application/json" in response_headers["content-type"], (
            response_headers
        )

        # ... and it was checked by the VP itself, not only by this
        # test's post-hoc reading of the artifact.
        passed_lines = artifact["assertions_passed"]
        assert any("content-type" in line for line in passed_lines), passed_lines

        payload = json.loads(artifact["response_body"])

        # The four fields, one by one — the criterion, not a summary of it.
        for field in CONTRACT_FIELDS:
            assert field in payload, (field, payload)

        # ... and the state they report is the **mcode side's** state.
        # This is what makes the answer attributable: the claude side
        # answers the same endpoint with the same four field names and
        # with nothing in flight, so a payload carrying the seeded
        # verification can only have come from the mcode instance.
        assert payload["verification_running"] == 1, payload
        assert payload["total_active"] == 1, payload
        assert isinstance(payload["details"], list), payload
        assert len(payload["details"]) == 1, payload
        detail = payload["details"][0]
        assert isinstance(detail, dict), payload
        assert set(detail) == DETAIL_KEYS, detail
        assert detail["plan_id"] == SEEDED_PLAN_ID, detail
        assert detail["type"] == "verification", detail
        assert detail["status"] == "running", detail
        assert detail["round"] == SEEDED_ROUND, detail

        # The counters and the list agree with each other: the overview
        # is one answer, not four independent ones.
        assert payload["total_active"] == len(payload["details"]), payload
        assert payload["verification_running"] == sum(
            1 for entry in payload["details"] if entry["type"] == "verification"
        ), payload
        assert payload["execution_running"] == sum(
            1 for entry in payload["details"] if entry["type"] == "execution"
        ), payload

        # --- 7. the negative control ------------------------------------
        # A relaunch that comes up **without** PDT_HARNESS=mcode still
        # answers /api/system/active with 200, a JSON content-type and
        # the four fields — the endpoint is served by the same code on
        # both sides, and the degraded service reads the same database,
        # so the response is indistinguishable from the mcode one.  Grading
        # that PASSED would be grading the endpoint and ignoring the
        # backend — the silent downgrade the hard constraints forbid.
        # It has to be FAILED.
        _kill_service(new_pid)
        _wait_until_port_free(mcode_port)

        real_ensure = service_manager.ensure_services
        degraded_answers: List[Tuple[int, Dict[str, str], Dict[str, Any]]] = []

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
            # The service is up here: read what it answers *before* the
            # framework stops it.  That answer is the whole point of the
            # control — the criterion is satisfied by the response and
            # the verdict still has to be FAILED.
            degraded_answers.append(
                _request(f"http://127.0.0.1:{mcode_port}/api/system/active")
            )
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

        degraded = agent._run_api_test_vp(_vp_009())

        assert degraded["status"] == "FAILED", degraded
        reasons = " ".join(str(r) for r in (degraded.get("reasons") or []))
        assert HARNESS_ENV_VAR in reasons, degraded
        assert MCODE_HARNESS in reasons, degraded
        # The reason has to say what is actually wrong: the backend was
        # configured but did not take effect.
        assert "not actually in effect" in reasons.lower(), degraded

        # The degraded service really did answer the criterion — 200, a
        # JSON content-type, the four fields, and the same seeded state
        # the mcode instance reported.  Only the process tells the two
        # apart, and the framework graded on the process.
        assert len(degraded_answers) == 1, degraded_answers
        degraded_status, degraded_headers, degraded_body = degraded_answers[0]
        assert degraded_status == 200, degraded_status
        assert "application/json" in degraded_headers.get("content-type", ""), (
            degraded_headers
        )
        assert set(CONTRACT_FIELDS) <= set(degraded_body), degraded_body
        assert degraded_body["verification_running"] == 1, degraded_body
        assert degraded_body["total_active"] == 1, degraded_body
        assert len(degraded_body["details"]) == 1, degraded_body
        assert degraded_body["details"][0]["plan_id"] == SEEDED_PLAN_ID, (
            degraded_body
        )

        # --- 8. the already-alive-but-wrong-backend case ---------------
        # Nothing was relaunched here: the port answers, but the process
        # answering it is a claude backend — a service started from the
        # declaration *without* the mcode harness and left running.  The
        # endpoint answers with 200 and the four fields either way, and
        # with the same state, because it reads the same database: the
        # response is indistinguishable from the mcode one, so only the
        # process can tell the two apart.  Grading this PASSED would let
        # a claude backend satisfy a criterion that is about the mcode
        # one — the "configured but mcode not actually in effect" case
        # the hard constraints call an acceptance failure.
        _wait_until_port_free(mcode_port)

        # The **real** ensure, not the patched one: this service is
        # started here, not by the framework's relaunch path.
        alive_map = real_ensure(
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
        alive_runtime = alive_map.runtimes["api-mcode"]
        assert alive_runtime.ready, alive_runtime.detail
        assert alive_runtime.port == mcode_port, alive_runtime
        degraded_runtimes.append(alive_map)

        alive_status, alive_headers, alive_body = _request(
            f"http://127.0.0.1:{mcode_port}/api/system/active"
        )
        assert alive_status == 200, alive_status
        assert "application/json" in alive_headers.get("content-type", ""), (
            alive_headers
        )
        assert set(CONTRACT_FIELDS) <= set(alive_body), alive_body
        assert alive_body["verification_running"] == 1, alive_body
        assert alive_body["total_active"] == 1, alive_body
        assert len(alive_body["details"]) == 1, alive_body

        agent._service_runtime_map = ServiceRuntimeMap(
            runtimes={
                "api": claude_runtime,
                "api-mcode": mcode_runtime,
            }
        )

        alive_verdict = agent._run_api_test_vp(_vp_009())

        assert alive_verdict["status"] == "FAILED", alive_verdict
        alive_reasons = " ".join(
            str(r) for r in (alive_verdict.get("reasons") or [])
        )
        assert HARNESS_ENV_VAR in alive_reasons, alive_verdict
        assert MCODE_HARNESS in alive_reasons, alive_verdict
        # The verdict has to name the condition the hard constraint names:
        # the backend was configured but did not actually take effect.
        assert "not actually in effect" in alive_reasons.lower(), alive_verdict

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

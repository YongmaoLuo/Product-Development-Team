"""HTTP contract: the request guard still covers the quota-wait read path.

The criterion this file pins
----------------------------
The capacity-wait architecture decision reads:

    *the loopback API's request guard must not be relaxed because a new
    state was added.*

Two rounds of probing produced no evidence about it.  The first probe hit
a recycled backend::

    GET /api/execution/20261024-vp-quota-parked/status  ->  connection refused

A refused *connection* is a fact about the service's lifetime, not about
the guard: the backend was not running, so the answer says nothing about
whether the guard would have refused the request.  The second probe did
get a body, but that body was read by eye — neither the 403 nor the
``forbidden`` envelope was ever asserted, and the guard has *two*
independent reasons to refuse a request:

  * the required ``X-PDT-Request`` header is missing, and
  * the ``Host`` is not one this instance serves.

A probe that only looks at the status code cannot tell those two apart,
so it cannot tell "the guard refused" from "the route is broken" — and
neither can it tell "the guard is still in force" from "the guard was
quietly relaxed by the capacity-wait work".

What this file fixes
--------------------
Three facts, asserted at the HTTP boundary, with the *reason* separated
from the *verdict*:

* **the guard still covers the quota-wait read path** — the status route
  of a parked plan, asked for without the header, answers 403 with
  ``error == "forbidden"`` and a detail that names the *missing
  ``X-PDT-Request`` header* (and does not carry the host refusal);
* **the route itself is alive** — the very same request *with* the
  header answers 200 and reports the parked verdict, so the 403 above is
  the guard refusing a working route rather than a route that 500s;
* **the static assets stay outside the guard** — browser navigation
  cannot attach a header, so ``GET /`` must keep answering 200 for a
  request that carries none.

Hermeticity
------------
The plans tree, the state database and the in-memory execution state all
live inside this test's own sandbox: the autouse ``isolated_plans_dir``
fixture points ``server.PLANS_DIR`` / ``server._state_db_path`` and the
``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH`` env vars at ``tmp_path``, and
the rows are written through the real repositories, so the endpoint
reads back the shapes production writes.  ``clean_execution_state``
clears the in-memory map afterwards, so the parked plan does not outlive
the test.

The header is taken off by hand
-------------------------------
``backend/tests/conftest.py`` wraps ``TestClient.__init__`` so that every
client in this suite carries ``X-PDT-Request: 1`` (see
``_pytest_hermetic_env.install_request_guard_header``).  That wrapper
keeps the suite testing routes instead of plumbing, but it also means a
client built in this file is never a *bare* client — and a test that
believes it sent a header-less request while the wrapper quietly added
one would pass for the wrong reason: it would never have exercised the
refusal path at all.  So the "no header" case takes the injected value
back out (:func:`_bare_client`), and the "with header" case spells the
header out on the request itself rather than relying on the wrapper to
add it.

Nothing here weakens the guard: the header is removed, not the check.

``testserver`` is put into ``PDT_ALLOWED_HOSTS`` for the same reason.
The guard refuses for two independent reasons, and this file is about
the header one; leaving the host unset would let a 403 arrive for the
wrong reason, and the assertion on the detail text would then be
asserting on the wrong string.

The instance that is actually serving
-------------------------------------
Everything above asks the guard a question through a client that lives
in the same process as the application.  That answers "is the guard's
code in force in this process?" — it does not answer "does the
instance an operator reaches on port N refuse a header-less request?".

The two questions came apart in the round this file belongs to: the
in-process check passed, and the instance that was actually serving
did not carry the guard.  So the instance now checks itself, and the
last section of this file asks the questions of a **real uvicorn on a
real port**:

  * the self-check passes there — a header-less request is refused
    with 403 and a ``forbidden`` body, and the same request with the
    header is served;
  * an instance whose guard refuses nothing **does not come up at
    all** — the check runs before uvicorn binds the socket, so the
    port is never opened, and the refusal is written to the log;
  * ``/health/data`` publishes ``request_guard: "enforced"``, which is
    what a reader compares when the answer came from a port they did
    not start themselves.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

import request_guard
import server


#: The plan id the failed probes used.  Reusing it keeps the criterion
#: and its evidence about the same plan.
PLAN_ID = "20261024-vp-quota-parked"

#: The route under test — the endpoint an operator (or a card) reads to
#: answer "where is this plan, and what is still left to do?".  It is a
#: read path the capacity-wait work touched, which is exactly why the
#: guard has to be re-checked on it.
STATUS_ROUTE = f"/api/execution/{PLAN_ID}/status"

#: The three tasks of the parked plan.  ``VP-002`` is the one that ran
#: into the provider's wall; ``VP-003`` is downstream of it and must
#: therefore still be ``pending``.
TASK_IDS = ("VP-001", "VP-002", "VP-003")

#: The one task that finished before the park.
COMPLETED_TASK_ID = "VP-001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bare_client() -> TestClient:
    """A client whose requests carry NO guard header.

    The suite-wide wrapper (``_pytest_hermetic_env.install_request_guard_header``)
    adds ``X-PDT-Request: 1`` to every ``TestClient``; the value is popped
    back out here so the request that reaches the middleware is genuinely
    header-less.  See the module docstring for why the "no header" case is
    unreachable without doing this by hand.
    """
    client = TestClient(server.app)
    client.headers.pop(request_guard.REQUEST_HEADER, None)
    return client


def _write_tasks_file(plan_dir: Path) -> None:
    """Write the plan's task list *and* its quota-wait marker.

    The marker is written the way the executor writes it (see
    ``agent._stop_reason_for_quota_wait``): ``stop_reason`` plus a
    JSON-encoded ``reason_detail`` at the top level of ``tasks.json``,
    next to the task list.  A parked plan that does not carry the marker
    is a plan whose "why did this stop" answer is missing — and the read
    path under test is the one that reads it.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    tasks = [
        {"id": tid, "title": f"task {tid}", "description": f"do {tid}"}
        for tid in TASK_IDS
    ]
    marker = {
        "providers": ["vendor-a"],
        "needs_manual_topup": True,
        "waited_sec": 42.5,
        "at": "2026-10-24T15:00:00",
    }
    payload = {
        "tasks": tasks,
        "stop_reason": "waiting_for_quota",
        "reason_detail": json.dumps(marker),
    }
    (plan_dir / "tasks.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )


def _seed_durable_rows() -> None:
    """Write the parked plan's rows into this test's own state database.

    A thin wrapper over :func:`_seed_parked_rows`, so the in-process
    tests and the detached instance below write the very same rows.
    """
    _seed_parked_rows(server._state_db_path())


def _seed_parked_rows(state_db: Path) -> None:
    """Write the routing row, the execution row and the ``plan_tasks`` rows.

    Every row goes through the repository production writes through, so the
    endpoint reads back exactly the shapes production produces:

      * ``plan_routing.current_phase`` = ``waiting_for_quota`` — the
        workflow phase the plan is parked in;
      * ``plan_execution.exec_status`` = ``waiting_for_quota`` — the
        execution record's own verdict.  Nothing in the row says
        ``failed``: a wait is a pause, not a crash;
      * ``plan_tasks`` — one row per task, with only ``VP-001`` completed.
        ``VP-002`` is the task that hit the provider wall and ``VP-003``
        is downstream of it; both must still read as ``pending``.

    *state_db* is named rather than derived, so a detached instance can
    be given a database of its own.
    """
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = db_open(state_db)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, "waiting_for_quota")
        ExecutionRepository(conn).update_fields(
            PLAN_ID,
            create_if_missing=True,
            exec_status="waiting_for_quota",
            exec_pid=None,
            started_at="2026-10-24T15:00:00",
        )
        task_repo = PlanTaskRepository(conn)
        for tid in TASK_IDS:
            task_repo.add_task(
                PLAN_ID,
                {"id": tid, "title": f"task {tid}", "description": f"do {tid}"},
            )
        # Only the first task finished.  The second one is the one that
        # hit the provider wall; the third one is downstream of it.
        task_repo.update_task(
            PLAN_ID, COMPLETED_TASK_ID, {"status": "completed"},
        )
    finally:
        conn.close()


@pytest.fixture
def parked_plan(tmp_path: Path) -> Path:
    """A plan parked on quota, in this test's own sandbox.

    Three things make it parked, and all three are what production
    leaves behind when a run runs out of provider capacity:

      * the marker in ``tasks.json`` (the executor's own write);
      * the durable rows (the routing phase and the execution verdict);
      * the in-memory execution state (what the status endpoint reads
        first — ``ended_at`` stays ``None`` because the run is parked,
        not over).

    The fixture returns the plan directory so a test can point at the
    marker file if it needs to.
    """
    plan_dir = server.PLANS_DIR / PLAN_ID
    _write_tasks_file(plan_dir)
    _seed_durable_rows()

    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    server._execution_state[PLAN_ID] = {
        "status": "waiting_for_quota",
        "logs": [],
        "project_dir": str(project_dir),
        "process": None,
        "pid": None,
        "started_at": "2026-10-24T15:00:00",
        "ended_at": None,
        "stop_reason": "waiting_for_quota",
    }
    return plan_dir


@pytest.fixture(autouse=True)
def _allowed_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let ``testserver`` through the Host check.

    The guard refuses a request for two independent reasons, and this
    file is about the header one.  Without ``testserver`` in the allowed
    list, the 403 would come from the Host check instead, and the
    assertion on the detail text would be asserting on the wrong string.
    """
    monkeypatch.setenv(request_guard.ENV_ALLOWED_HOSTS, "testserver")


# ---------------------------------------------------------------------------
# 1) No header — the quota-wait read path is still guarded
# ---------------------------------------------------------------------------


def test_missing_header_is_forbidden_on_waiting_status(
    parked_plan: Path,
) -> None:
    """A parked plan's status, asked for without the header, is refused.

    The refusal has to be *the guard's* refusal, not some other 403: the
    body says ``forbidden`` and the detail names the missing
    ``X-PDT-Request`` header.  The detail is what separates "the header is
    missing" from "this host is not served by this instance" — the guard
    has both reasons, and a probe that only reads the status code cannot
    tell them apart.
    """
    client = _bare_client()
    resp = client.get(STATUS_ROUTE)

    assert resp.status_code == 403, (
        f"GET {STATUS_ROUTE} without the guard header answered "
        f"{resp.status_code}; the guard must refuse a header-less request "
        f"on the quota-wait read path too — a status code other than 403 "
        f"means the request was let through"
    )

    body = resp.json()
    assert body.get("error") == "forbidden", (
        f"the refusal must carry the 'forbidden' envelope; got {body!r}"
    )

    detail = body.get("detail") or ""
    assert request_guard.REQUEST_HEADER in detail, (
        f"the detail must name the missing header "
        f"{request_guard.REQUEST_HEADER!r} so an operator can see what to "
        f"send; got {detail!r}"
    )
    assert "missing" in detail.lower(), (
        f"the detail must say the header is missing; got {detail!r}"
    )
    assert "is not this machine" not in detail, (
        f"the refusal came from the Host check, not from the missing "
        f"header: {detail!r}. The two reasons have to stay apart — this "
        f"file is about the header"
    )


# ---------------------------------------------------------------------------
# 2) With the header — the same request is served
# ---------------------------------------------------------------------------


def test_header_makes_the_same_request_succeed(parked_plan: Path) -> None:
    """The very same request with the header answers 200 — and the parked verdict.

    Without this, the 403 above would prove nothing: a route that is
    broken answers 403 too (and so does a route that does not exist).
    The parked verdict is what shows the request reached the quota-wait
    read path and was answered from it — ``waiting_for_quota``, not
    ``failed``: a wait is a pause, not a crash.
    """
    client = _bare_client()
    resp = client.get(
        STATUS_ROUTE,
        headers={
            # The value is spelled out the way the bundled UI sends it
            # (``frontend/api.js`` sends exactly ``"X-PDT-Request": "1"``)
            # rather than read back from the module that checks it: a
            # server whose expected value drifted would otherwise be
            # asked for the *new* value here and would answer 200, and
            # the drift would stay invisible.
            request_guard.REQUEST_HEADER: "1",
        },
    )

    assert resp.status_code == 200, (
        f"GET {STATUS_ROUTE} with the guard header answered "
        f"{resp.status_code}: {resp.text!r}. The header must be enough "
        f"to reach the route — a 403 here means the guard rejected a "
        f"request that carries the header"
    )

    body = resp.json()
    assert body.get("plan_id") == PLAN_ID, (
        f"the answer must be about the parked plan; got {body!r}"
    )
    assert body.get("status") == "waiting_for_quota", (
        f"the parked plan must report its parked verdict; got "
        f"{body.get('status')!r}. A quota wait is a pause — a plan that "
        f"reads as 'failed' here is the regression this criterion is "
        f"about"
    )
    assert body.get("stop_reason") == "waiting_for_quota", (
        f"the stop reason must say why the run stopped; got "
        f"{body.get('stop_reason')!r}"
    )
    assert body.get("ended_at") is None, (
        f"a parked run has not ended; got ended_at={body.get('ended_at')!r}"
    )


# ---------------------------------------------------------------------------
# 3) Static assets stay outside the guard
# ---------------------------------------------------------------------------


def test_static_assets_stay_unguarded() -> None:
    """The UI must stay loadable without the header.

    Browser *navigation* cannot attach a custom header — that is the
    whole reason the guard covers ``/api/`` and not the static assets.
    If the guard were widened to cover them, the bundled UI could not
    load at all, and the failure would look like a broken server rather
    than a broken guard.
    """
    client = _bare_client()
    resp = client.get("/")

    assert resp.status_code != 403, (
        f"GET / answered {resp.status_code} for a request without the "
        f"guard header; the static assets must stay outside the guard — "
        f"the bundled UI cannot attach a header"
    )
    assert resp.status_code == 200, (
        f"GET / answered {resp.status_code}: {resp.text!r}; the UI's "
        f"index page must be served"
    )


# ---------------------------------------------------------------------------
# 4) The persisted-row read path is guarded too
# ---------------------------------------------------------------------------
#
# The status route does not always answer from memory.  When the
# in-memory map holds nothing for the plan — a fresh boot, a restarted
# backend, a second process answering for a plan the first one runs —
# the answer is made of the **persisted row** instead.  That is a
# different branch of the same route, and the row carries the same
# information the map does: where the plan is parked, and why.
#
# The sibling above answers from the in-memory branch, so the guard is
# asserted here on the other one: a header-less request is refused
# there too, and the route is shown to be alive on that path (the same
# request with the header answers 200 and reports the verdict the row
# carries).


def test_headerless_request_is_forbidden_on_the_persisted_row_path(
    parked_plan: Path,
) -> None:
    """The row-fallback read path is behind the guard as well.

    The refusal must be *the guard's* refusal, and for the same reason
    as on the in-memory path: the missing ``X-PDT-Request`` header.
    The host check must not be what refuses it — the two reasons have
    to stay apart, or the assertion on the detail would be asserting on
    the wrong string.
    """
    # The map forgets the plan: what is left is the durable row, and
    # the route has to answer from it.
    server._execution_state.pop(PLAN_ID, None)

    client = _bare_client()
    resp = client.get(STATUS_ROUTE)

    assert resp.status_code == 403, (
        f"GET {STATUS_ROUTE} without the guard header answered "
        f"{resp.status_code} on the persisted-row path; the guard must "
        f"refuse a header-less request there too — a status code other "
        f"than 403 means the request was let through"
    )

    body = resp.json()
    assert body.get("error") == "forbidden", (
        f"the refusal must carry the 'forbidden' envelope; got {body!r}"
    )

    detail = body.get("detail") or ""
    assert request_guard.REQUEST_HEADER in detail, (
        f"the detail must name the missing header "
        f"{request_guard.REQUEST_HEADER!r} so an operator can see what to "
        f"send; got {detail!r}"
    )
    assert "missing" in detail.lower(), (
        f"the detail must say the header is missing; got {detail!r}"
    )
    assert "is not this machine" not in detail, (
        f"the refusal came from the Host check, not from the missing "
        f"header: {detail!r}. The two reasons have to stay apart — this "
        f"file is about the header"
    )

    # The route itself is alive on this path: the very same request
    # with the header answers 200 and reports the verdict the row
    # carries.  Without this the 403 above would prove nothing — a
    # route that fails on the row path refuses the same way.
    served = client.get(
        STATUS_ROUTE,
        headers={request_guard.REQUEST_HEADER: "1"},
    )

    assert served.status_code == 200, (
        f"GET {STATUS_ROUTE} with the guard header answered "
        f"{served.status_code} on the persisted-row path: {served.text!r}. "
        f"The row path must serve the request the guard lets through — "
        f"the 403 above is the guard's doing only if this one is served"
    )

    served_body = served.json()
    assert served_body.get("status") == "waiting_for_quota", (
        f"the parked plan must report its parked verdict when the answer "
        f"comes from the row; got {served_body.get('status')!r}. A quota "
        f"wait is a pause — a plan that reads as 'failed' here is the "
        f"regression this criterion is about"
    )


# ---------------------------------------------------------------------------
# 5) The instance that is actually serving
# ---------------------------------------------------------------------------
#
# The four checks below are made against a real uvicorn listening on a
# real loopback port, not against a test client inside the application's
# own process.  That is the layer the round's failure was in: the
# in-process check passed while the instance that was serving did not
# carry the guard.


def _free_port() -> int:
    """A loopback port the kernel says is free.

    ``bind(("127.0.0.1", 0))`` is the only allocation that cannot
    collide with a port some other process already holds.  The socket
    is closed before the number is returned — the caller has to win the
    race against its own bind, and holding the socket open would make
    losing it certain.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Answer:
    """What one HTTP request over a real socket answered."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body

    def json(self) -> Dict[str, Any]:
        return json.loads(self.body)


def _http_get(
    port: int, path: str, headers: Optional[Dict[str, str]] = None,
) -> _Answer:
    """One GET over a real socket, carrying exactly the headers given.

    The headers are spelled out rather than inherited: the whole point
    of the "without the header" case is that the request really does
    arrive without it.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers=dict(headers or {}))
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        return _Answer(int(resp.status), body)
    finally:
        conn.close()


class _Instance:
    """A uvicorn server and the thread it runs in."""

    def __init__(self, server_obj: Any, thread: threading.Thread):
        self.server = server_obj
        self.thread = thread

    @property
    def started(self) -> bool:
        return bool(self.server.started)


def _start_instance(port: int, lifespan: str = "off") -> _Instance:
    """Start a real uvicorn serving ``server.app`` on *port*.

    ``log_config=None`` keeps uvicorn from replacing the root logger's
    handlers: its default logging configuration would take pytest's
    capture with it, and the checks below need to read what the server
    logged.
    """
    import uvicorn

    config = uvicorn.Config(
        server.app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan=lifespan,
        log_config=None,
    )
    server_obj = uvicorn.Server(config=config)
    thread = threading.Thread(
        target=server_obj.run, name="pdt-guard-instance", daemon=True,
    )
    thread.start()
    return _Instance(server_obj, thread)


def _wait_started(instance: _Instance, timeout: float = 20.0) -> bool:
    """Wait until the instance is listening; False if it never is."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if instance.started:
            return True
        if not instance.thread.is_alive():
            return False
        time.sleep(0.05)
    return False


@pytest.fixture
def live_instance():
    """A real uvicorn serving ``server.app`` on an ephemeral port.

    The lifespan is switched off on purpose: what is under test here
    is the guard on the wire.  A boot that sweeps plans, recovers
    execution state and resolves the provider chain would make this
    fixture depend on the machine it runs on, and the guard has nothing
    to do with any of that.  The startup path has its own check below,
    which runs with the lifespan on.
    """
    for _attempt in range(3):
        port = _free_port()
        instance = _start_instance(port)
        if _wait_started(instance):
            try:
                yield port
            finally:
                instance.server.should_exit = True
                instance.thread.join(timeout=20)
            return
        # The port was taken between the probe and the bind; take another.
        instance.server.should_exit = True
        instance.thread.join(timeout=5)
    raise AssertionError(
        "the live instance never started listening on any of the three "
        "ports it was given"
    )


def test_guard_selfcheck_passes_on_real_port(live_instance: int) -> None:
    """The instance refuses a header-less request on the port it serves.

    This is the check the round was missing: the guard was verified in
    the application's own process, and the instance that was actually
    serving was never asked.  Here the instance is asked over a real
    socket, and the two answers are read separately — a refusal proves
    the guard is in force, and the served request proves the route
    under the guard is alive (a route that refuses everything would
    look exactly like a working guard).
    """
    ok, detail = server._request_guard_selfcheck_on_port(
        "127.0.0.1", live_instance,
    )
    assert ok, (
        f"the guard self-check failed on the instance that is serving on "
        f"port {live_instance}: {detail}"
    )

    bare = _http_get(live_instance, server.REQUEST_GUARD_SELFCHECK_PATH)
    assert bare.status == 403, (
        f"a request without {request_guard.REQUEST_HEADER} answered "
        f"{bare.status} on port {live_instance}; the instance that is "
        f"serving must refuse it"
    )
    assert "forbidden" in bare.body.lower(), (
        f"the refusal must carry 'forbidden'; got {bare.body!r}"
    )

    headed = _http_get(
        live_instance,
        server.REQUEST_GUARD_SELFCHECK_PATH,
        {request_guard.REQUEST_HEADER: "1"},
    )
    assert headed.status != 403, (
        f"a request WITH {request_guard.REQUEST_HEADER} was refused too "
        f"({headed.status}); the route under the guard is not being served"
    )


def test_guard_selfcheck_blocks_startup_when_header_not_enforced(
    monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    """An instance whose guard refuses nothing must not come up.

    The guard is bypassed here the way a careless change would bypass
    it: the middleware still runs, but the check it makes no longer
    refuses anything.  The instance must then refuse to serve — the
    check runs before uvicorn binds the socket, so the port is never
    opened — and it must say why, in the one line an operator reads
    when the API is not guarded.
    """
    monkeypatch.setattr(
        request_guard, "rejection_reason", lambda request: None,
    )

    port = _free_port()
    # ``__main__`` records the address it is about to serve on before
    # the server starts; the refusal names that port.
    monkeypatch.setattr(server, "_SERVER_BOUND_PORT", port, raising=False)

    with caplog.at_level(logging.ERROR):
        instance = _start_instance(port, lifespan="on")
        instance.thread.join(timeout=30)

    assert not instance.started, (
        "the instance came up although its guard refuses nothing — an API "
        "without a guard is exactly what must not reach the network"
    )
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1.0).close()

    assert "request_guard_selfcheck_failed" in caplog.text, (
        f"the refusal must be written to the log; got {caplog.text!r}"
    )
    assert f"port={port}" in caplog.text, (
        f"the refusal must name the port the instance was about to serve "
        f"on; got {caplog.text!r}"
    )


def test_health_data_reports_request_guard_enforced(
    live_instance: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/health/data`` says the guard is in force on this instance.

    The field is what a reader compares when the answer came from a
    port they did not start themselves: an instance that publishes
    ``request_guard: "enforced"`` is one that checked itself, and one
    that does not publish the field is somebody else.
    """
    # Start from "nothing has been checked in this process", so the
    # answer below can only come from the check this test runs.
    monkeypatch.setitem(
        server._REQUEST_GUARD_SELFCHECK, "status", "unchecked",
    )

    ok, detail = server._request_guard_selfcheck_on_port(
        "127.0.0.1", live_instance,
    )
    assert ok, detail

    resp = _http_get(live_instance, "/health/data")
    assert resp.status == 200, resp.body
    body = resp.json()
    assert body.get("request_guard") == "enforced", (
        f"/health/data must publish the guard's own verdict; got {body!r}"
    )


def test_waiting_status_route_still_forbidden_without_header(
    parked_plan: Path, live_instance: int,
) -> None:
    """The waiting-status route is guarded on the instance that serves it.

    The criterion itself, asked of the instance on the port it actually
    listens on: a request without the guard header is refused with 403
    and a ``forbidden`` body, and the very same request with the header
    is served (200) with the parked verdict.  A quota wait is a pause —
    a plan that reads as ``failed`` here is the regression this
    criterion is about.
    """
    bare = _http_get(live_instance, STATUS_ROUTE)
    assert bare.status == 403, (
        f"GET {STATUS_ROUTE} without the guard header answered "
        f"{bare.status} on the instance that is serving; the guard must "
        f"refuse it"
    )
    body = bare.json()
    assert body.get("error") == "forbidden", (
        f"the refusal must carry the 'forbidden' envelope; got {body!r}"
    )

    headed = _http_get(
        live_instance, STATUS_ROUTE, {request_guard.REQUEST_HEADER: "1"},
    )
    assert headed.status == 200, (
        f"GET {STATUS_ROUTE} with the guard header answered "
        f"{headed.status}: {headed.body!r}. The header must be enough to "
        f"reach the route"
    )
    served = headed.json()
    assert served.get("status") == "waiting_for_quota", (
        f"the parked plan must report its parked verdict; got "
        f"{served.get('status')!r}"
    )


# ---------------------------------------------------------------------------
# 6) The instance the round actually reaches — and the self-check it hands
#    to the round
# ---------------------------------------------------------------------------
#
# Everything above asks a question of an instance this test started.  The
# round does not: it reaches a service that **outlives the backend that
# started it**, and by the time it asks, that service may already be gone.
#
# The 2026-10-26 round learned this the hard way.  Its verification point
# was answered ``connection refused`` on port 64469, and the round read
# that as a product defect — while the very same log showed that the
# instance on that port had checked **itself** at startup and found the
# guard refusing (a header-less request answered 403 with a ``forbidden``
# body; the same request with the header was served).
#
# So the guard was working and the answer was still useless, for two
# separate reasons:
#
#   * the **port** was gone by the time the point was dispatched, and
#   * the **self-check result** — the one piece of evidence that says
#     "this instance was up and its guard was in force" — was never
#     handed to the round, so the round could not tell a dead port from a
#     broken product.
#
# This section pins both halves:
#
#   * the criterion is asked of a **detached** instance — one whose
#     starter is already gone — so the answer comes from the instance the
#     round would really be talking to; and
#   * the instance publishes its self-check (``request_guard_detail`` on
#     ``/health/data``), so the round can be **told** what the instance
#     found instead of having to re-derive it on a port that may already
#     be dead.

#: The starter's source.  It spawns the service into a new session, writes
#: its pid down and exits — so the service that answers the port has no
#: starter left, which is the shape the round's own service has.
_STARTER_SOURCE = """
import os
import subprocess
import sys

env = dict(os.environ)
env.update({env!r})
log = open({log!r}, "wb")
proc = subprocess.Popen(
    {cmd!r},
    cwd={cwd!r},
    env=env,
    stdout=log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
open({pid_file!r}, "w").write(str(proc.pid))
"""


def _wait_listening(port: int, timeout: float = 120.0) -> bool:
    """Wait until *port* accepts a connection."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _start_service_that_outlives_its_starter(
    port: int, plans_dir: Path, state_db: Path, root: Path,
) -> int:
    """Start the round's service through a starter that exits at once.

    The service is started detached, so it keeps answering after the
    backend that started it is gone — the shape the round's own service
    has, and the shape the failed probe ran into.  Returns the service's
    pid.
    """
    pid_file = root / "service.pid"
    log_file = root / "service.log"
    source = _STARTER_SOURCE.format(
        env={
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_STATE_DB_PATH": str(state_db),
            "PDT_PORT": str(port),
            "PDT_HOST": "127.0.0.1",
            # The child inherits this process's environment, and the
            # hermetic env pins ``PDT_PROVIDER_MODE=cc-switch`` for the
            # pytest process.  The child must not inherit that mode:
            # CC Switch is a plugin of one operator's machine, and on a
            # clean runner nothing is listening on its port, so the
            # child's startup aborts ("Application startup failed") and
            # the service never comes up.  Pin the mode here rather
            # than trust the machine the suite happens to run on.
            "PDT_PROVIDER_MODE": "inherit",
        },
        log=str(log_file),
        cmd=[sys.executable, "-m", "backend.server"],
        cwd=str(Path(__file__).resolve().parents[3]),
        pid_file=str(pid_file),
    )
    starter = subprocess.Popen([sys.executable, "-c", source])
    starter.wait(timeout=120)
    assert starter.returncode == 0, (
        f"the starter failed to hand the service over; it exited with "
        f"{starter.returncode}"
    )
    return int(pid_file.read_text().strip())


def _stop_service(pid: int) -> None:
    """Stop the service, and everything it started with it."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def test_the_request_guard_answer_stays_reachable_after_the_round_service_outlives_its_starter(
    tmp_path: Path,
) -> None:
    """The guard's answer is still reachable on the instance the round reaches.

    Two facts, asked of a **detached** instance — one whose starter has
    already exited, which is the instance the round really talks to:

    * **the criterion still holds there** — a request for the parked
      plan's status without ``X-PDT-Request`` is refused with 403 and a
      ``forbidden`` body, and the very same request with the header is
      served (200) with the parked verdict; and
    * **the instance hands its self-check to the round** — the
      ``request_guard_detail`` block on ``/health/data`` says when the
      check ran, which paths it asked, and what came back with and
      without the header.

    The second fact is the one that was missing.  The instance *did* check
    itself at startup — its log says so — but the result never left the
    process, so the round could not tell "the guard is broken" from "the
    port is gone", and the round read a refused connection as a product
    defect.  Publishing the check is what lets the round be told.
    """
    plans_dir = tmp_path / "plans"
    state_db = tmp_path / "state.db"
    _write_tasks_file(plans_dir / PLAN_ID)
    _seed_parked_rows(state_db)

    port = _free_port()
    pid = _start_service_that_outlives_its_starter(
        port, plans_dir, state_db, tmp_path,
    )
    try:
        assert _wait_listening(port), (
            f"the round's service never started listening on port {port}; "
            f"its log says:\n"
            f"{(tmp_path / 'service.log').read_text(errors='replace')}"
        )

        # -- the criterion, asked of the instance that is serving ------
        bare = _http_get(port, STATUS_ROUTE)
        assert bare.status == 403, (
            f"GET {STATUS_ROUTE} without {request_guard.REQUEST_HEADER} "
            f"answered {bare.status} on the instance the round reaches; the "
            f"guard must refuse it"
        )
        assert "forbidden" in bare.body.lower(), (
            f"the refusal must carry 'forbidden'; got {bare.body!r}"
        )

        headed = _http_get(
            port, STATUS_ROUTE, {request_guard.REQUEST_HEADER: "1"},
        )
        assert headed.status == 200, (
            f"GET {STATUS_ROUTE} with {request_guard.REQUEST_HEADER} "
            f"answered {headed.status}: {headed.body!r}. The header must be "
            f"enough to reach the route"
        )
        served = headed.json()
        assert served.get("status") == "waiting_for_quota", (
            f"the parked plan must report its parked verdict; got "
            f"{served.get('status')!r}"
        )

        # -- the self-check, handed to the round ------------------------
        health = _http_get(port, "/health/data")
        assert health.status == 200, (
            f"/health/data answered {health.status}: {health.body!r}"
        )
        body = health.json()
        detail = body.get("request_guard_detail")
        assert isinstance(detail, dict), (
            f"/health/data must publish the instance's own guard self-check "
            f"as 'request_guard_detail'; got {body!r}"
        )

        assert detail.get("status") == "enforced", (
            f"the instance checked itself and the guard was in force; got "
            f"{detail.get('status')!r} ({detail.get('detail')!r})"
        )
        assert detail.get("checked_at"), (
            f"the self-check must say when it ran; got {detail!r}"
        )
        assert detail.get("status_without_header") == 403, (
            f"the self-check must record what a request without the header "
            f"answered; got {detail.get('status_without_header')!r}"
        )
        assert detail.get("status_with_header") == 200, (
            f"the self-check must record what the same request with the "
            f"header answered; got {detail.get('status_with_header')!r}"
        )

        paths = detail.get("paths")
        assert isinstance(paths, list) and paths, (
            f"the self-check must name the paths it asked; got {paths!r}"
        )
        asked = {
            str(entry.get("path")): entry for entry in paths
            if isinstance(entry, dict)
        }
        assert server.REQUEST_GUARD_SELFCHECK_PATH in asked, (
            f"the self-check must have asked the guarded path "
            f"{server.REQUEST_GUARD_SELFCHECK_PATH!r}; got {paths!r}"
        )
        guarded = asked[server.REQUEST_GUARD_SELFCHECK_PATH]
        assert guarded.get("status_without_header") == 403, (
            f"the guarded path must have been refused without the header; "
            f"got {guarded!r}"
        )
        assert guarded.get("status_with_header") == 200, (
            f"the guarded path must have been served with the header; got "
            f"{guarded!r}"
        )
    finally:
        _stop_service(pid)

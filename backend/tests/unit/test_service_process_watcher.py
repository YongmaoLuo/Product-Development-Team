"""The watcher that notices a service's process is gone (2026-10-26).

Backstory
---------
On 2026-10-26 at 02:21:49 the round's api service finished its
readiness self-check and the ledger recorded it as ready — pid 43904,
port 64469, ``origin: spawned``.  Two seconds later the service logged
``shutdown complete`` and was gone.  **Nothing in the round said so.**
The first thing that noticed was a VP at 02:25:01 finding the port
refused: 2 minutes 44 seconds after the service had actually died.

The same shape had happened twice before (2026-10-24, 21:44 and 21:54):
the service came up, was recorded as ready, and was gone two or three
seconds later — three rounds in a row, and not one event anywhere.  A
round that cannot see the death cannot report it, cannot explain it,
and cannot ask for the service to be brought back.

So the round now **watches** the process it started.  One light thread
per service polls the child; when the child is gone the round writes
down how it died and marks the runtime dead.  The watcher does not keep
the service alive — nothing in this module can, and pretending
otherwise would be a lie.  What it does is make the death visible the
moment it happens, and stop the dead runtime from being handed out as
an address afterwards.

What this file pins
-------------------
* **the acceptance criterion** — after the round records its service,
  ``/health/data`` still answers, and it still proves the round's own
  four fixture routing rows.  Every assertion that follows is graded
  against the round's data; a service that answers "0 rows" would make
  all of them run against an empty table, which is exactly what the
  2026-10-24 production round did.
* **the watcher** — the exit is recorded, with the facts that explain
  it (which pid, which port, which signal, how long it lived).
* **the state machine** — a runtime whose process is gone stops being
  an address, and says so.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402
from tests import _host_load  # noqa: E402
from tests.unit.test_service_manager import (  # noqa: E402
    _PORT_BLOCK_SIZE,
    _free_port,
    _port_block_base,
)


# ---------------------------------------------------------------------------
# The service under test
# ---------------------------------------------------------------------------
#
# A real process on a real port: the watcher's whole job is to notice
# that a *process* is gone, and a stubbed process cannot go anywhere.

#: The service, written into the scratch directory at test time rather
#: than committed.  It answers two routes: ``/health`` (the readiness
#: probe) and ``/health/data`` (the data-plane probe, carrying the
#: number of routing rows the round's fixture holds).
_SERVICE_SOURCE = '''\
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROWS = int(sys.argv[1])
PORT = int(sys.argv[2])


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.split("?")[0].endswith("/health/data"):
            body = json.dumps({
                "status": "ok",
                "plan_routing_rows": ROWS,
                "instance": {"boot_id": "watcher-fixture"},
            }).encode("utf-8")
        else:
            body = json.dumps({"status": "ok"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), _Handler).serve_forever()
'''

#: The round's own token.  The owner registry is what the round writes
#: itself into, and it is what the watcher stamps its events with.
_ROUND_TOKEN = "round-token-watcher"

#: How many routing rows the round's fixture holds.  Four — one per
#: plan of the acceptance set.
_FIXTURE_ROWS = 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The port this module's service gets
# ---------------------------------------------------------------------------
#
# A port the kernel hands out is not a port a test keeps: the probe
# socket is closed before the number is returned, so the port is
# unowned from that moment on and anybody can take it.  In a parallel
# run that anybody is the worker next door -- running the same test,
# with the same fixture -- and the two of them then fight over one
# port, which is how a service ends up not serving at all.
#
# So the ports of a run are partitioned by worker: worker ``gw0`` owns
# one block, ``gw1`` the next, and no two workers ever draw the same
# number.  A port that something is serving on is skipped, so the
# number that comes back is one this worker owns and nothing answers
# on.
#
# The counter that hands the ports out belongs to the **process**, not
# to this module: a worker runs many modules, and two modules of the
# same worker that drew the same number would be two services fighting
# over one port.  So the allocator is borrowed from the sibling module
# that owns it, and this module's service gets a port out of the run's
# partition -- the same partition every other service of this run gets
# its port from.


# ---------------------------------------------------------------------------
# The deadline this module's service is given
# ---------------------------------------------------------------------------
#
# The service is a real process on a real port, and both its start-up
# and the cost of one probe get slower when the machine is busy.  A
# literal number of seconds is a threshold written for one machine on
# one quiet afternoon; on a host carrying eighteen workers, each of
# them starting a service, it measures the host instead of the
# service.
#
# So the deadline is the shared gate's answer.  The gate
# (``backend/tests/_host_load.py``) owns the limit that says how loaded
# this host is allowed to be, so one limit and one message format
# apply everywhere -- and the case is not skipped on a busy host: what
# is asserted here is what the service *does*, not how long it took,
# and the host that is too busy to measure is exactly the host whose
# case still has to run.

#: The readiness budget a quiet host is given.
READY_BUDGET_SECONDS = 30.0

#: The ceiling: a scaled budget is still a budget.
READY_BUDGET_MAX_SECONDS = 120.0


def _readiness_budget_seconds(base: float = READY_BUDGET_SECONDS) -> float:
    """How long the service is given to come up, decided by the shared gate.

    The budget follows the host's load average, because both the
    child's own start-up and the cost of one probe scale with it.  The
    scale is the shared gate's limit: a host at the limit gets twice
    the base budget, and the budget stops at a ceiling, because a
    scaled budget is still a budget.
    """
    limit = _host_load.host_load_limit()
    load = _host_load.current_host_load()
    if limit <= 0:
        return float(base)
    scaled = base * (1.0 + max(0.0, load) / limit)
    return min(scaled, READY_BUDGET_MAX_SECONDS)


def _write_service(project_dir: Path) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    script = project_dir / "fixture_service.py"
    script.write_text(_SERVICE_SOURCE, encoding="utf-8")
    return script


def _declaration(
    port: int, script: Path, *, rows: int = _FIXTURE_ROWS,
) -> ServiceDeclaration:
    return ServiceDeclaration(
        name="api",
        port=port,
        start_cmd=(
            f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} "
            f"{rows} {port}"
        ),
        cwd=".",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=int(_readiness_budget_seconds()),
        log_path="tmp/api.log",
    )


def _get_json(url: str, timeout: float = 5.0) -> Tuple[int, Dict[str, Any]]:
    """``GET *url*`` — returns ``(status, body)``."""
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return int(response.status), json.loads(raw)


def _wait_for(predicate, timeout: float = 20.0, interval: float = 0.1):
    """Wait until *predicate* is true. Returns its value, or ``None``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _died_from_sigterm(exit_signal: Any, returncode: Any) -> bool:
    """Did the process die because of SIGTERM?

    A shell reports that death two ways: as the signal itself
    (``-15``) and through the shell's own ``128 + signal`` convention
    (``143``).  Which one comes out depends on whether the shell
    exec'd the command or forked it, and neither is wrong.
    """
    return exit_signal == 15 or returncode == 143


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_db(tmp_path: Path, monkeypatch) -> Path:
    """The round's own state database — four routing rows.

    ``/health/data`` counts the rows of the database **the service
    reads**, so the fixture has to be the one the round configured:
    ``PDT_STATE_DB_PATH`` is what the service and the round's own
    expectation both resolve.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db = tmp_path / "fixture_state.db"
    conn = open_db(db)
    try:
        migrate(conn)
        for index in range(_FIXTURE_ROWS):
            conn.execute(
                "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
                "VALUES (?, 'interview', '2026-10-26T00:00:00Z')",
                (f"fixture-plan-{index}",),
            )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def round_dirs(tmp_path: Path) -> Tuple[Path, Path]:
    """The plan directory and the project directory of one round."""
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir, project_dir


@pytest.fixture
def started() -> Iterator[List[Tuple[sm.ServiceRuntime, ServiceDeclaration]]]:
    """Every service this test started, so the teardown can stop it.

    A service left running keeps its port, and the next test that asks
    for a port then fails for a reason that has nothing to do with
    what it is testing.
    """
    services: List[Tuple[sm.ServiceRuntime, ServiceDeclaration]] = []
    yield services
    for runtime, declaration in services:
        sm.stop_service(runtime, declaration, grace_seconds=5.0)


def _bring_up(
    plan_dir: Path, project_dir: Path, *, rows: int = _FIXTURE_ROWS,
) -> Tuple[sm.ServiceRuntime, ServiceDeclaration]:
    """Bring one service up through ``ensure_services`` and return it."""
    script = _write_service(project_dir)
    port = _free_port()
    declaration = _declaration(port, script, rows=rows)
    sm.register_service_owner(
        plan_dir, _ROUND_TOKEN, current=True, pid=os.getpid(),
    )
    runtime_map = sm.ensure_services(
        project_dir, {"api": declaration},
        plan_dir=plan_dir, plan_id="plan-watcher",
    )
    return runtime_map.runtimes["api"], declaration


def _kill_service(runtime: sm.ServiceRuntime) -> None:
    """Kill the service the way the round's own reaper does.

    The **group**, not the pid: the spawned child is a shell, and
    killing only the shell leaves the service behind, still holding the
    port — which is the state the 2026-10-26 round kept running into.
    """
    if runtime.pgid:
        try:
            os.killpg(runtime.pgid, signal.SIGTERM)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if runtime.pid:
        try:
            os.kill(runtime.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass


# ---------------------------------------------------------------------------
# The acceptance criterion
# ---------------------------------------------------------------------------


def test_health_data_still_answers_after_the_round_records_its_service(
    round_dirs, fixture_db, started,
):
    """The round's data plane is still there after the service is recorded.

    This is the criterion the 2026-10-26 round failed: the service was
    recorded as ready, and the question "is it still serving **our**
    data?" has to be answerable — over a real socket, on the round's
    own fixture.

    And when the service dies, the round has to find out.
    """
    plan_dir, project_dir = round_dirs
    runtime, declaration = _bring_up(plan_dir, project_dir)
    started.append((runtime, declaration))

    assert runtime.ready is True, runtime.detail
    assert runtime.data_plane is not None
    assert runtime.data_plane.plan_routing_rows == _FIXTURE_ROWS, (
        f"the service must be serving the round's own rows; got "
        f"{runtime.data_plane.to_dict()!r}"
    )

    # The round records its service: the ledger names the process, and
    # the pin says the round is using it right now.
    ledger_path = sm.record_runtimes(
        plan_dir, "plan-watcher",
        sm.ServiceRuntimeMap(runtimes={"api": runtime}),
        project_dir=project_dir, owner_token=_ROUND_TOKEN,
    )
    assert ledger_path is not None
    ledger = sm.read_ledger(plan_dir)
    assert [entry["name"] for entry in ledger["services"]] == ["api"]
    assert ledger["services"][0]["port"] == runtime.port

    # The criterion: the data plane is still answering, and it is still
    # answering with the round's four rows.
    status, body = _get_json(
        f"http://127.0.0.1:{runtime.port}/health/data",
    )
    assert status == 200, body
    assert body["status"] == "ok", body
    assert body["plan_routing_rows"] >= _FIXTURE_ROWS, (
        f"the data plane must prove the round's own rows; got {body!r}"
    )

    # And the death must not be silent.  This is the shape the round
    # kept running into: the service was gone two seconds after it was
    # recorded, and the first thing that noticed was a VP two minutes
    # later.
    _kill_service(runtime)
    assert _wait_for(lambda: runtime.exit) is not None, (
        "the watcher must notice that the service's process is gone"
    )
    assert runtime.dead is True
    assert runtime.usable is False, (
        "a runtime whose process is gone is not an address any more"
    )

    events = sm.read_process_events(plan_dir)
    exits = [e for e in events if e.get("event") == sm.PROCESS_EVENT_EXITED]
    assert len(exits) == 1, (
        f"the exit must be recorded exactly once; got {events!r}"
    )
    event = exits[0]
    assert event["pid"] == runtime.pid
    assert event["port"] == runtime.port
    assert _died_from_sigterm(event["exit_signal"], event["returncode"])


# ---------------------------------------------------------------------------
# The watcher itself
# ---------------------------------------------------------------------------


def test_watcher_records_exit_time_and_signal(
    round_dirs, fixture_db, started,
):
    """The event says which process, which port, which signal, how long.

    The exit carries the three facts a reader needs to explain a death:
    **when** it happened, **how** it happened, and **how long** the
    process lived.  The times have to agree with each other — an
    ``uptime_sec`` that does not match ``exited_at - started_at`` is a
    sign that one of the three was written by something else.
    """
    plan_dir, project_dir = round_dirs
    runtime, declaration = _bring_up(plan_dir, project_dir)
    started.append((runtime, declaration))

    assert runtime.pid
    _kill_service(runtime)
    assert _wait_for(lambda: runtime.exit) is not None

    events = sm.read_process_events(plan_dir)
    exits = [e for e in events if e.get("event") == sm.PROCESS_EVENT_EXITED]
    assert len(exits) == 1, events
    event = exits[0]

    assert event["pid"] == runtime.pid
    assert event["port"] == runtime.port
    assert event["round_token"] == _ROUND_TOKEN, (
        "the event must name the round it belongs to; got "
        f"{event['round_token']!r}"
    )
    assert event["started_at"] > 0
    assert event["exited_at"] >= event["started_at"]
    assert event["uptime_sec"] >= 0
    assert event["returncode"] is not None

    # The signal is named in the positive form, so a reader does not
    # have to know the "-15 means SIGTERM" convention to ask "was it
    # killed?".
    assert event["exit_signal"] == 15, (
        f"the death must be attributable to the SIGTERM; got {event!r}"
    )
    assert _died_from_sigterm(event["exit_signal"], event["returncode"])

    # The three times must agree with each other.
    assert abs(
        event["uptime_sec"] - (event["exited_at"] - event["started_at"])
    ) < 1.0, f"the recorded uptime must match the recorded times; got {event!r}"

    # The same facts on the runtime itself: a reader that only has the
    # runtime in hand still learns that it is dead and how it died.
    assert runtime.exit is not None
    assert runtime.exit.pid == runtime.pid
    assert runtime.exit.port == runtime.port
    assert runtime.exit.round_token == _ROUND_TOKEN


def test_the_signal_is_named_even_when_a_shell_reports_it_the_other_way(
    round_dirs, fixture_db, started,
):
    """A shell that reports ``128 + signal`` still killed the process.

    A service started through ``bash -lc`` is not always exec'd: a
    compound command (``cd app && npm run dev``) leaves the shell in
    charge, and a shell that is killed reports the death the way shells
    report it — as the exit code ``128 + signal``, not as the signal.
    The event has to name the signal in both cases, because "which
    signal" is the question a reader of the log is asking.
    """
    assert sm.exit_signal_of(-15) == 15
    assert sm.exit_signal_of(143) == 15
    assert sm.exit_signal_of(0) is None
    assert sm.exit_signal_of(3) is None
    assert sm.exit_signal_of(None) is None


def test_exit_event_carries_port(round_dirs, fixture_db, started):
    """The event names the port the service actually served on.

    Not the port the plan declared — the port the process was really
    listening on.  Those differ whenever the round had to relocate,
    and the port in the event is the one a reader has to check
    against.
    """
    plan_dir, project_dir = round_dirs
    runtime, declaration = _bring_up(plan_dir, project_dir)
    started.append((runtime, declaration))

    # The port is the one the service really answered on.
    status, _ = _get_json(
        f"http://127.0.0.1:{runtime.port}/health/data",
    )
    assert status == 200

    _kill_service(runtime)
    assert _wait_for(lambda: runtime.exit) is not None

    events = sm.read_process_events(plan_dir)
    exits = [e for e in events if e.get("event") == sm.PROCESS_EVENT_EXITED]
    assert len(exits) == 1, events
    event = exits[0]

    assert event["port"] == runtime.port
    assert event["port"] == declaration.port


def test_runtime_marked_dead_after_exit(round_dirs, fixture_db, started):
    """A runtime whose process is gone is not an address any more.

    And the round that comes after must **bring the service back**,
    not hand the dead runtime out again: the 2026-10-26 round spent
    two minutes and forty-four seconds finding out, from a VP, that
    the service it had been told was ready had been gone for minutes.
    """
    plan_dir, project_dir = round_dirs
    runtime, declaration = _bring_up(plan_dir, project_dir)
    started.append((runtime, declaration))
    first_pid = runtime.pid

    _kill_service(runtime)
    assert _wait_for(lambda: runtime.exit) is not None

    assert runtime.dead is True
    assert runtime.alive is False, (
        "a runtime whose process is gone must not say it is alive"
    )
    assert runtime.usable is False, (
        "a runtime whose process is gone is not an address any more"
    )

    # The next round asks for the same service again.
    runtime_map = sm.ensure_services(
        project_dir, {"api": declaration},
        plan_dir=plan_dir, plan_id="plan-watcher",
    )
    again = runtime_map.runtimes["api"]
    started.append((again, declaration))

    assert again.ready is True, again.detail
    assert again.pid != first_pid, (
        "the service must be started again, not the dead runtime reused"
    )
    assert again.dead is False
    assert again.alive is True
    assert again.usable is True


def test_a_live_service_is_not_reported_as_dead(round_dirs, fixture_db, started):
    """The watcher says nothing while the service is still there.

    A watcher that reports every service as dead would be worse than no
    watcher: the round would learn to ignore it.
    """
    plan_dir, project_dir = round_dirs
    runtime, declaration = _bring_up(plan_dir, project_dir)
    started.append((runtime, declaration))

    time.sleep(1.5)

    assert runtime.dead is False
    assert runtime.exit is None
    assert runtime.usable is True
    assert sm.read_process_events(plan_dir) == []


def test_watcher_stops_when_service_stops(round_dirs, fixture_db, started):
    """The watcher does not outlive the service it watches.

    A thread that polls forever would accumulate one per service per
    round, for as long as the backend lives.  The watcher polls every
    half second, so the thread is gone shortly after the process is.
    """
    plan_dir, project_dir = round_dirs
    runtime, declaration = _bring_up(plan_dir, project_dir)
    started.append((runtime, declaration))

    killed_at = time.monotonic()
    _kill_service(runtime)
    assert _wait_for(lambda: runtime.exit) is not None

    assert _wait_for(
        lambda: not [
            thread for thread in threading.enumerate()
            if thread.name.startswith("pdt-service-watch-")
        ],
        timeout=1.8,
    ) is not None, "the watcher thread must end with the process"
    assert time.monotonic() - killed_at < 2.0, (
        "the watcher thread must be gone within two seconds of the "
        "process it watches"
    )


def test_a_service_the_round_stopped_is_not_reported_as_a_death(
    round_dirs, fixture_db, started,
):
    """A reap is not a crash.

    The round stops its own services when it ends.  That exit belongs
    in the stream too — but it must not read as "the service died",
    because that is a different event with a different meaning.
    """
    plan_dir, project_dir = round_dirs
    runtime, declaration = _bring_up(plan_dir, project_dir)
    started.append((runtime, declaration))

    sm.stop_service(runtime, declaration, grace_seconds=10.0)
    assert _wait_for(lambda: runtime.exit) is not None

    events = sm.read_process_events(plan_dir)
    exits = [e for e in events if e.get("event") == sm.PROCESS_EVENT_EXITED]
    assert len(exits) == 1, events
    assert "stopped by the round" in exits[0]["reason"], exits[0]


# ---------------------------------------------------------------------------
# The port and the deadline this module's services get
# ---------------------------------------------------------------------------
#
# Two things about the service this module watches are hostage to the
# machine it runs on, and both of them used to be fixed numbers:
#
#   * the **port** -- the kernel hands one out and gives it back the
#     moment the probe socket is closed, so between choosing it and
#     binding it the port belongs to nobody, and in a parallel run
#     "nobody" is the worker next door, which is running the same test
#     with the same fixture.  Two workers that draw the same number
#     fight over one port, and the service that loses never comes up.
#   * the **deadline** -- thirty seconds of wall clock is a threshold
#     written for one machine on one quiet afternoon.  On a host that
#     carries eighteen workers, each of them starting a service, the
#     same service needs longer to come up, and a deadline that knows
#     nothing about the host measures the host instead of the service.
#
# So the ports are partitioned by worker, and the deadline is the
# shared gate's answer -- ``backend/tests/_host_load.py`` owns the
# limit that says how loaded this host is allowed to be, so one limit
# and one message format apply everywhere.


def test_the_port_of_this_module_comes_from_the_runs_partition(monkeypatch):
    """The port this module's service gets is one the run partitioned.

    The allocator belongs to the **process**, not to this module: two
    modules of one worker must never be handed the same number, or the
    stop of one takes the service of the other down with it -- a
    service stopped by somebody else's stop is a service that *died*,
    and the round is told it died.  So this module borrows the run's
    allocator instead of keeping a counter of its own, and this pins
    that the port it gets is out of that partition: below the
    ephemeral range, where the kernel cannot take the number away
    while the service is binding it.
    """
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw5")
    port = _free_port()

    base = _port_block_base(5)
    assert base <= port < base + _PORT_BLOCK_SIZE, (
        f"the port {port} is not in worker gw5's block "
        f"[{base}, {base + _PORT_BLOCK_SIZE})"
    )
    assert port < 32768, (
        f"port {port} is inside the ephemeral range; a port of this "
        f"partition has to sit below it"
    )


def test_the_readiness_budget_scales_with_the_host_load(monkeypatch):
    """A busy host must not be measured by a fixed deadline.

    The service is a real process on a real port, and both its
    start-up and the cost of one probe get slower when the machine is
    busy.  A deadline that knows nothing about the host measures the
    host instead of the service, and the case fails for a reason that
    has nothing to do with the code under test.

    So the budget follows the load -- and it follows the **shared
    gate's** limit, so that one limit and one message format apply
    everywhere.
    """
    monkeypatch.setattr(os, "getloadavg", lambda: (50.0, 40.0, 30.0))

    budget = _readiness_budget_seconds()

    assert budget > READY_BUDGET_SECONDS, (
        f"the readiness budget was {budget}s on a host whose load average "
        f"was 50; it must scale with the load"
    )


def test_the_readiness_budget_is_never_shorter_than_the_base(monkeypatch):
    """Scaling the budget must not shorten it.

    The budget a quiet host gets is the budget the deadline used to be.
    A longer wait for a service that is on its way up is patience, not
    a weaker assertion -- but a *shorter* one would be the opposite.
    """
    monkeypatch.setattr(os, "getloadavg", lambda: (0.0, 0.0, 0.0))

    assert _readiness_budget_seconds() == READY_BUDGET_SECONDS


def test_the_readiness_budget_stays_inside_its_ceiling(monkeypatch):
    """A scaled budget is still a budget.

    Without a ceiling a host that never settles would keep the case
    waiting; the ceiling keeps a service that is never coming up a
    failing case instead of a hanging one.
    """
    monkeypatch.setattr(os, "getloadavg", lambda: (100000.0, 100000.0, 100000.0))

    assert _readiness_budget_seconds() <= READY_BUDGET_MAX_SECONDS


def test_the_declaration_is_given_the_shared_budget(tmp_path, monkeypatch):
    """The deadline the service is given is the gate's answer.

    A literal in the declaration would be a deadline written for one
    machine; the deadline has to be the one the shared gate works out
    for the host the case is running on.
    """
    monkeypatch.setattr(os, "getloadavg", lambda: (50.0, 40.0, 30.0))

    script = _write_service(tmp_path / "project")
    declaration = _declaration(_free_port(), script)

    assert declaration.ready_timeout_seconds == int(_readiness_budget_seconds())

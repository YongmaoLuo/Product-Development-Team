"""The scheduler's HTTP surface: the guard, the input, the capability.

Why this file exists
--------------------
The scheduler added **five** endpoints to a server that binds loopback.
Loopback is not a boundary a browser respects: any page the operator
opens can issue requests to ``127.0.0.1:<port>`` from inside the
browser, and the server cannot tell those apart from its own UI — both
arrive from 127.0.0.1 with no credentials to check.  The request guard
(``backend/request_guard.py``) is what closes that half, and it closes
it by **middleware**, over every ``/api/`` path.

That is exactly why the new endpoints need their own tests.  Middleware
covers a *prefix*, and a new endpoint under that prefix is covered by
construction — but "by construction" is a claim about the code, not
evidence.  A route registered outside the guarded prefix, a router
mounted after the guard, a second app instance serving the same API:
each of those would leave the endpoints reachable from a forged page
while every functional test stayed green.  The matrix below is the
evidence.

The four properties
-------------------
1. **The guard matrix** — five endpoints × four header shapes
   (no header / wrong ``Origin`` / wrong ``Host`` / correct) must answer
   ``403 / 403 / 403 / 2xx``.  A guard that covers four of the five
   endpoints is a guard the panel cannot rely on.
2. **The input** — a ``plan_id`` shaped like an injection, a traversal,
   an empty string, an absurd length or a Unicode control character must
   be refused with ``400`` or ``404``, and must never reach a query as
   anything but a **bound parameter**.
3. **The capability surface** — of the four actions, only ``start`` may
   interrupt a running plan.  ``promote`` and ``resume`` must not stop
   anybody, *even when there is a victim the rules would allow them to
   stop*.  The test carries a **negative control**: the same world,
   driven through ``start``, does stop somebody — so a probe that can
   never see a stop cannot pass by being blind.
4. **No CORS** — the preflight (``OPTIONS``) must not be granted CORS
   headers.  A server that answers a preflight with
   ``Access-Control-Allow-Origin`` is a server that lets a foreign page
   make the call in the first place.

How the world is built
----------------------
The endpoints are exercised through the **mounted app** with a **real
engine** behind it: the route reads ``app.state.scheduler_engine``, and
what is mounted there is a worker thread with a real mailbox, a real
store, a real ledger and the real lifecycle facade.  A route that never
reaches the engine, or one that reaches a stand-in that agrees with it,
would pass a matrix that says nothing about production.

The plans are made by hand for the same reason the action matrix makes
them by hand: three facts have to agree for a plan to be "working" — its
phase, its execution row with a **live** pid, and the slot row that
says when it took the slot.  The slot's clock is what the eviction
rule reads, so a case cannot reach "running for 700 seconds" by waiting;
it writes the slot's ``acquired_at`` itself.

Why this directory is a package
-------------------------------
The name ``plan_scheduler`` is taken twice in this repository: by the
application's own package (``backend/plan_scheduler/``) and by this
directory.  pytest decides which by walking up from the test module to
the first directory that is *not* a package and putting that directory
on ``sys.path``.  ``tests/security/`` is not a package, so without the
defence below this directory would be imported as the top-level
``plan_scheduler`` and the application's package would be left
unreachable — every test that imports ``from plan_scheduler.store
import ...`` would fail to collect with ``ModuleNotFoundError``.

:func:`pkgutil.extend_path` is the standard library's answer to exactly
this: a package whose ``__path__`` covers more than one directory.  It
appends every other ``plan_scheduler/`` directory found on ``sys.path``,
so the application's modules stay importable under the name they own
while this package keeps the name pytest gave it.  The sibling
``tests/e2e/plan_scheduler/__init__.py`` carries the same defence for
the same reason.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE  # noqa: E402


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------

#: The read endpoint: the whole scheduler state in one document.
STATE_ENDPOINT = "/api/scheduler/state"

#: The four actions.  The vocabulary is **closed** — the same four words
#: the command executor knows.
ACTIONS: Tuple[str, ...] = ("promote", "pause", "resume", "start")

#: The five endpoints, with the plan each one needs to answer ``2xx``.
#:
#: ``resume`` is asked about a *stopped* plan (a queued one is a
#: conflict), the other three about a queued one.
ENDPOINTS: Tuple[Tuple[str, str, str], ...] = (
    ("state", "GET", STATE_ENDPOINT),
    ("promote", "POST", "/api/plan/p-queued/scheduler/promote"),
    ("pause", "POST", "/api/plan/p-queued/scheduler/pause"),
    ("resume", "POST", "/api/plan/p-stopped/scheduler/resume"),
    ("start", "POST", "/api/plan/p-queued/scheduler/start"),
)

#: The four header shapes the matrix crosses the endpoints with.
#:
#: ``correct`` is the guard's own header on a loopback host; the other
#: three are the ways a forged page (or a rebound host) shows up.
HEADER_COMBOS: Tuple[Tuple[str, Dict[str, str]], ...] = (
    ("no_header", {}),
    ("wrong_origin", {"Origin": "http://evil.example"}),
    ("wrong_host", {"Host": "evil.example"}),
    ("correct", {}),
)

#: The five shapes a hostile ``plan_id`` takes.
#:
#: The bidi case is spelled as an escape on purpose.  The character is
#: part of what the request carries, but a source file that contains it
#: literally is a Trojan-Source file (bandit B613) that no reviewer can
#: audit -- the rendered source reads as something else entirely.  The
#: string the code builds is the same either way.
MALICIOUS_PLAN_IDS: Tuple[Tuple[str, str], ...] = (
    ("sql_injection", "' OR 1=1 --"),
    ("path_traversal", "../../etc/passwd"),
    ("empty", ""),
    ("oversized", "a" * 10000),
    ("unicode_control", "plan\u202eid"),
)

#: A fixed moment, so that every case reads the same clock.
BASE_EPOCH = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc).timestamp()

#: The runtime a plan is protected for — the ``M`` of the eviction rule.
MIN_RUNTIME_SECONDS = 600.0

#: How long a working plan has held its slot: long enough to be
#: interrupted, and long enough not to be.
ELAPSED_EVICTABLE = 700.0
ELAPSED_PROTECTED = 100.0


# ---------------------------------------------------------------------------
# The clients
# ---------------------------------------------------------------------------


@pytest.fixture()
def guarded_client() -> TestClient:
    """A client that carries the guard header.

    The suite's hermetic environment injects ``X-PDT-Request: 1`` into
    every ``TestClient`` and allows the ``testserver`` host, so this
    client is the shape the bundled UI and a local script use.
    """
    return TestClient(server.app)


@pytest.fixture()
def bare_client() -> TestClient:
    """A client that sends **no** guard header.

    The header is removed from the client's own headers rather than
    "not passing it", because the hermetic environment adds it to every
    client it builds.
    """
    client = TestClient(server.app)
    client.headers.pop(REQUEST_HEADER, None)
    return client


def _call(
    client: TestClient,
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    """Issue one request with ``method`` and ``url``."""
    return client.request(method, url, headers=headers or {})


# ---------------------------------------------------------------------------
# The world a case drives
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock the case moves by hand.

    It is a callable, because that is what the ledger, the facts reader
    and the executor ask for: ``clock()`` is "what time is it now".
    """

    def __init__(self, now: float = BASE_EPOCH) -> None:
        self._now = float(now)

    def __call__(self) -> float:
        """Return the current moment, in epoch seconds."""
        return self._now

    def advance(self, seconds: float) -> float:
        """Move the clock forward and return the new moment."""
        self._now += float(seconds)
        return self._now


class _StaticConfig:
    """A configuration loader that always answers the same snapshot."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def current(self) -> Any:
        """Return the snapshot."""
        return self._config


def _iso(epoch: float) -> str:
    """Return the moment as the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _plans_root() -> Path:
    """The plans tree the facade and the facts reader resolve plans in."""
    return Path(server.PLANS_DIR)


def _open_state_db() -> sqlite3.Connection:
    """Open the case's state database with the production PRAGMA contract."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    migrate(conn)
    return conn


def _make_plan(plan_id: str, phase: str) -> Path:
    """Create a plan whose phase is ``phase``.

    The phase is written as the legacy ``plan_state.json`` snapshot and
    then read once, which is what migrates it into ``plan_routing`` —
    the same path a plan that was bootstrapped by the API takes.  The
    migration is not optional here: the scheduler reads the phase from
    ``plan_routing``, and a plan whose row was never written is a plan
    the scheduler does not know.
    """
    from plan_state import PlanState

    plan_dir = _plans_root() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": plan_id, "current_phase": phase}),
        encoding="utf-8",
    )
    # The first read migrates the legacy file into the routing row.
    PlanState(plan_dir)
    return plan_dir


def _mark_execution_alive(plan_id: str) -> None:
    """Mark the execution round as in flight (``status=running``).

    The facade asks the **process** whether a plan is running, so this
    is what makes ``stop_plan`` dispatch to the execution side.
    """
    with server._execution_state_lock:
        server._execution_state[plan_id] = {"status": "running", "ended_at": None}


def _wait_for_tick(engine: Any, timeout: float = 10.0) -> None:
    """Wait until the engine has run its first tick.

    The tick reconciles the queue and the slots against the plan facts.
    A case that wrote its plans **before** that tick would have the tick
    re-stamp the slot it wrote — and a slot stamped with "now" is a plan
    that has just started, not one that has been working for 700
    seconds.  So the case waits for the tick, and only then writes.
    """
    deadline = time.monotonic() + timeout
    while engine.ticks < 1:
        if time.monotonic() > deadline:
            raise AssertionError(
                "the scheduler engine did not run its first tick in time"
            )
        time.sleep(0.005)


@pytest.fixture
def live_pid(register_child_process):
    """The pid of a process that is running right now.

    The facts reader decides "is this plan working" by asking whether
    the process is **alive**, so a case that wants a working plan needs
    a process that really is running.
    """
    process = register_child_process(
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
    )
    return int(process.pid)


@pytest.fixture
def stop_handlers(monkeypatch):
    """Replace the two stop endpoints with recorders.

    The real stop handler kills the process and writes the checkpoint;
    what the facade's contract is about is *which* handler it dispatched
    and that the plan ends up in ``stopped``.  The recorder does both —
    it marks the execution stopped, exactly like the real handler's
    persistence step, and parks the phase — so the assertions are about
    the facade, not about the handler.

    The recorder writes through **its own connection**, the way the real
    handler does.  The executor must not hold a write transaction while
    it calls the facade; if it does, these writes block and the case
    says so.
    """
    from routes import execution as routes_execution
    from routes import verification as routes_verification

    calls: List[Any] = []

    def _mark_stopped_in_db(plan_id: str) -> None:
        conn = _open_state_db()
        try:
            conn.execute(
                "UPDATE plan_execution SET exec_status = ?, exec_pid = NULL "
                "WHERE plan_id = ?",
                ("stopped", plan_id),
            )
        finally:
            conn.close()

    def _park_as_stopped(plan_id: str) -> None:
        from plan_state import PlanState

        plan_dir = _plans_root() / plan_id
        state = PlanState(plan_dir)
        state.reload()
        state.transition_to("stopped")

    def fake_stop_execution(plan_id, discard=False):
        calls.append(("execution", plan_id, discard))
        _mark_stopped_in_db(plan_id)
        with server._execution_state_lock:
            server._execution_state.pop(plan_id, None)
        _park_as_stopped(plan_id)

    def fake_stop_verification(plan_id):
        calls.append(("verification", plan_id))
        _mark_stopped_in_db(plan_id)
        _park_as_stopped(plan_id)

    monkeypatch.setattr(routes_execution, "stop_execution", fake_stop_execution)
    monkeypatch.setattr(
        routes_verification, "stop_verification", fake_stop_verification,
    )
    return calls


@pytest.fixture
def scheduler(monkeypatch):
    """A scheduler the HTTP layer can talk to — a real engine, really running.

    The factory takes the capacity, the protection window and the
    command's timeout, and hands back the world.  The engine is mounted
    on ``app.state.scheduler_engine`` — the handle the route reads — and
    the first tick is waited for, so that the case writes its plans
    into a world the tick has already reconciled.
    """
    from plan_lifecycle_service import PlanLifecycleService
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.commands import CommandExecutor
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.dryrun import ExecuteSink
    from plan_scheduler.engine import PlanSchedulerEngine
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore

    started: List[Tuple[Any, sqlite3.Connection, sqlite3.Connection]] = []

    def _make(
        capacity: int = 1,
        min_runtime_seconds: float = MIN_RUNTIME_SECONDS,
        command_timeout_seconds: float = 10.0,
        lifecycle: Any = None,
    ) -> Dict[str, Any]:
        engine_conn = _open_state_db()
        case_conn = _open_state_db()
        store = SqliteSchedulerStore()
        clock = FakeClock()
        facts = PlanFactsReader(engine_conn, _plans_root(), clock=clock)
        ledger = CapacityLedger(store, facts, clock, conn=engine_conn)
        facade = lifecycle if lifecycle is not None else PlanLifecycleService()
        commands = CommandExecutor(
            store,
            ledger,
            facade,
            facts=facts,
            conn=engine_conn,
            capacity=capacity,
            min_runtime_seconds=min_runtime_seconds,
            clock=clock,
        )
        engine = PlanSchedulerEngine(
            _StaticConfig(
                SchedulerConfig(
                    enabled=True,
                    dry_run=False,
                    capacity=capacity,
                    min_runtime_seconds=min_runtime_seconds,
                    # One tick, and then a very long wait: the cases
                    # drive the scheduler through the **mailbox**, and a
                    # tick that kept running would decide things behind
                    # the case's back.
                    tick_seconds=3600.0,
                    command_timeout_seconds=command_timeout_seconds,
                )
            ),
            store=store,
            facts=facts,
            ledger=ledger,
            commands=commands,
            # The tick's exit: the decisions are carried out by nobody.
            # The cases drive the actions through the mailbox, and a
            # tick that started plans behind the case's back would make
            # every assertion about "who is running" a race.
            sink=ExecuteSink(lambda decision: decision),
            clock=clock,
            conn=engine_conn,
        )
        engine.start()
        _wait_for_tick(engine)
        monkeypatch.setattr(
            server.app.state, "scheduler_engine", engine, raising=False
        )
        started.append((engine, engine_conn, case_conn))
        return {
            "engine": engine,
            "store": store,
            "clock": clock,
            "conn": case_conn,
            "engine_conn": engine_conn,
            "ledger": ledger,
            "commands": commands,
            "lifecycle": facade,
        }

    yield _make

    for engine, engine_conn, case_conn in started:
        engine.stop()
        for conn in (engine_conn, case_conn):
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass


def _make_working_plan(world: Dict[str, Any], plan_id: str, elapsed: float,
                       pid: int) -> None:
    """Make ``plan_id`` a plan that is working and has held its slot.

    Three facts have to agree for a plan to be "working": the phase, the
    execution row (with a live pid), and the process state the facade
    reads.  The slot row says **when** it took the slot, which is what
    the eviction rule reads — so it is written with the elapsed time the
    case wants, not with the moment the case happened to run.
    """
    _make_plan(plan_id, "executing")
    world["conn"].execute(
        "INSERT OR REPLACE INTO plan_execution "
        "(plan_id, project_dir, next_run_at, exec_pid, exec_status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            plan_id,
            str(_plans_root() / plan_id / "project"),
            None,
            pid,
            "running",
            _iso(BASE_EPOCH),
        ),
    )
    _mark_execution_alive(plan_id)
    world["store"].upsert_slot(
        world["conn"], plan_id, now=_iso(BASE_EPOCH - elapsed)
    )


# ---------------------------------------------------------------------------
# 1 — the guard matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint_name,method,url", ENDPOINTS, ids=[e[0] for e in ENDPOINTS]
)
@pytest.mark.parametrize(
    "combo,extra_headers", HEADER_COMBOS, ids=[c[0] for c in HEADER_COMBOS]
)
def test_guard_matrix_5x4(
    endpoint_name: str,
    method: str,
    url: str,
    combo: str,
    extra_headers: Dict[str, str],
    scheduler: Any,
    guarded_client: TestClient,
    bare_client: TestClient,
) -> None:
    """Five endpoints × four header shapes: 403 / 403 / 403 / 2xx.

    The matrix is the evidence: a rule that holds for four of the five
    endpoints and quietly does not hold for the fifth is a rule the
    panel would get wrong on exactly the endpoint the operator was
    looking at.

    The three refusals are the guard's own answers, and they are
    different failures: a request with **no** header is a page that
    could not set one, a request with a foreign ``Origin`` is a page
    from another site, and a request with a foreign ``Host`` is a
    rebound name.  All three must be refused before the handler runs.
    """
    _make_plan("p-queued", "queued")
    _make_plan("p-stopped", "stopped")
    scheduler()

    if combo == "no_header":
        client = bare_client
        headers: Dict[str, str] = {}
    else:
        client = guarded_client
        headers = dict(extra_headers)

    response = _call(client, method, url, headers)

    if combo == "correct":
        assert 200 <= response.status_code < 300, (
            "{0} {1} with the guard header answered {2}: {3}".format(
                method, url, response.status_code, response.text
            )
        )
        return

    assert response.status_code == 403, (
        "{0} {1} with {2} answered {3}, not 403: {4}".format(
            method, url, combo, response.status_code, response.text
        )
    )
    body = response.json()
    assert "detail" in body, body


def test_guard_matrix_covers_every_cell() -> None:
    """The matrix is the whole grid, not a sample of it.

    A matrix that quietly lost a cell would still pass every case in it:
    the coverage is the claim, and it is checked here rather than
    counted by eye.
    """
    assert len(ENDPOINTS) == 5
    assert len(HEADER_COMBOS) == 4
    assert len({e[0] for e in ENDPOINTS}) == 5
    assert len({c[0] for c in HEADER_COMBOS}) == 4


#: The paths under the scheduler's prefix that the four fixed endpoints
#: **cannot** match — the malformed ones.  They are still scheduler
#: paths, and the contract says *all* of them are guarded.
MALFORMED_PATHS: Tuple[Tuple[str, str], ...] = (
    ("traversal", "/api/plan/{0}/scheduler/promote".format(
        quote("../../etc/passwd", safe=""))),
    ("empty_id", "/api/plan//scheduler/promote"),
    ("illegal_action", "/api/plan/p-queued/scheduler/destroy"),
)


def test_the_guard_covers_the_malformed_paths_too(
    scheduler: Any,
    guarded_client: TestClient,
    bare_client: TestClient,
) -> None:
    """The guard covers the prefix, not the five canonical routes.

    The matrix above walks the paths the scheduler **advertises**.  It
    cannot walk the ones it does not: an empty plan id, a plan id that
    carries a separator, an action outside the closed four — none of
    them matches a fixed route, and a guard that only covered the
    routes would leave them outside.

    The contract is about the paths, not the routes: every
    ``/api/scheduler`` and ``/api/plan/{id}/scheduler`` path must be
    refused without the guard header.  So each malformed path is asked
    twice — bare, and with the header.  Bare is 403 (the guard ran
    before the router ever saw the path); with the header the path
    reaches the scheduler and is refused on its own terms, 400 or 404.

    The last one is the sharper half: an illegal action must be a 404
    from the scheduler itself, and not the router's generic answer.
    """
    scheduler()

    for shape, path in MALFORMED_PATHS:
        refused = bare_client.post(path)
        assert refused.status_code == 403, (
            "{0} ({1}) without the guard header answered {2}, not 403: "
            "{3}".format(path, shape, refused.status_code, refused.text)
        )

        answered = guarded_client.post(path)
        assert answered.status_code in (400, 404), (
            "{0} ({1}) with the guard header answered {2}, not 400/404: "
            "{3}".format(path, shape, answered.status_code, answered.text)
        )

    # The illegal action says so in its own words: the scheduler
    # refuses it, and it is not the router's "Not found".
    illegal = guarded_client.post("/api/plan/p-queued/scheduler/destroy")
    assert illegal.status_code == 404, illegal.text
    assert illegal.json().get("error") == "no_such_action", illegal.text


# ---------------------------------------------------------------------------
# 2 — the input
# ---------------------------------------------------------------------------


def _segment(plan_id: str) -> str:
    """The path segment a client sends for ``plan_id``.

    Percent-encoded, because a raw ``/`` would split the path before
    the router ever saw it — and a request that never reaches the
    endpoint cannot be refused *by* it.
    """
    return quote(plan_id, safe="")


@pytest.mark.parametrize(
    "shape,plan_id", MALICIOUS_PLAN_IDS, ids=[m[0] for m in MALICIOUS_PLAN_IDS]
)
@pytest.mark.parametrize("action", ACTIONS)
def test_injection_inputs_all_400_or_404(
    shape: str,
    plan_id: str,
    action: str,
    scheduler: Any,
    guarded_client: TestClient,
) -> None:
    """Every hostile ``plan_id`` is refused with 400 or 404.

    The property under test is "the request is **refused**": never a
    2xx that answers a plan that does not exist, never a 500 that turns
    a bad request into a server error, and never a query in which the id
    was pasted into the statement.

    The band is 400-or-404 on purpose, and both ends are right: 400 is
    the shared guard's own answer ("this is not a plan id"), 404 is
    "there is no such plan".  What would not be right is a 200 — the
    app treating ``' OR 1=1 --`` as a plan it knows.
    """
    scheduler()

    url = "/api/plan/{0}/scheduler/{1}".format(_segment(plan_id), action)
    response = guarded_client.post(url)

    assert response.status_code in (400, 404), (
        "POST {0} (a {1} plan id) answered {2}, not 400/404: {3}".format(
            url, shape, response.status_code, response.text
        )
    )
    assert response.status_code != 500, response.text


def test_injection_inputs_never_reach_a_query_as_text(
    scheduler: Any,
    guarded_client: TestClient,
) -> None:
    """A hostile plan id is carried into the database as a **value**.

    The id is put into the store **directly**, because the route would
    (rightly) refuse it long before the database; the store is the last
    place a plan id is turned into a query, so that is where the
    property is proved.

    The proof is an **effect**, not a spelling.  ``enqueue`` looks the
    plan up before it writes it::

        SELECT seq FROM scheduler_queue WHERE plan_id = ?

    If the id were pasted into that statement instead of bound to it,
    the lookup for ``' OR 1=1 --`` would match **every** row — the
    already-queued benign plan included — and the store would believe
    the hostile id was already in the queue and would write nothing.
    So: the hostile id must produce a **second** row, carrying the id
    verbatim, and the queue must still hold exactly the two rows that
    were written.

    The trace is the mechanism, quoted from the same run: SQLite
    doubles the quote inside a value, so the id reaches the statement
    as one quoted literal (``''' OR 1=1 --'``) and not as code.
    """
    from plan_scheduler.store import SqliteSchedulerStore

    world = scheduler()
    hostile = "' OR 1=1 --"
    benign = "p-benign"

    traced: List[str] = []
    world["conn"].set_trace_callback(traced.append)

    store = SqliteSchedulerStore()
    benign_seq = store.enqueue(world["conn"], benign)
    hostile_seq = store.enqueue(world["conn"], hostile)

    statements = [s for s in traced if "scheduler_queue" in s]
    assert statements, (
        "the trace callback recorded no statement about the queue — the "
        "write never reached the database, so this test would pass "
        "without proving anything"
    )
    hostile_literal = "'" + hostile.replace("'", "''") + "'"
    assert any(hostile_literal in s for s in statements), (
        "the hostile id did not reach the statement as a quoted value: "
        "{0!r}".format(statements)
    )

    # The effect: a second row, carrying the id verbatim.
    rows = world["conn"].execute(
        "SELECT plan_id FROM scheduler_queue ORDER BY seq"
    ).fetchall()
    assert rows == [(benign,), (hostile,)], (
        "the queue holds {0!r} — a hostile id that was pasted into the "
        "lookup would have matched the benign row and written "
        "nothing".format(rows)
    )
    assert hostile_seq != benign_seq, (
        "the hostile id was given the benign plan's seq ({0}) — the "
        "lookup matched a row it should not have".format(hostile_seq)
    )
    assert world["conn"].execute(
        "SELECT COUNT(*) FROM scheduler_queue"
    ).fetchone()[0] == 2, (
        "the hostile id changed the statement's meaning — the queue does "
        "not hold exactly the rows that were written"
    )


def test_sql_statements_are_parameterized_not_concatenated() -> None:
    """Every statement the scheduler runs is a **literal** with bound parameters.

    The trace above shows a value arriving as data; this shows the
    *statement* is never assembled at runtime.  A statement built by an
    f-string, a ``+`` or a ``%`` is a statement whose shape depends on
    the input, and that is the whole of the injection risk — so the
    check is over the **syntax**, not over a grep: the first argument of
    every ``execute`` must be a constant string, and a statement with a
    ``?`` in it must be given its parameters separately.
    """
    import ast
    import inspect

    from plan_scheduler import capacity, commands, facts, store

    modules = (store, facts, commands, capacity)
    assert modules, "no scheduler module to check"

    checked = 0
    for module in modules:
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in ("execute", "executemany")):
                continue
            checked += 1
            assert node.args, (
                "{0}.{1} was called with no statement".format(
                    module.__name__, func.attr
                )
            )
            statement = node.args[0]
            assert isinstance(statement, ast.Constant) and isinstance(
                statement.value, str
            ), (
                "the statement in {0}.{1} is not a constant string: {2!r} — "
                "a statement whose text is assembled at runtime is a "
                "statement whose shape depends on the input".format(
                    module.__name__, func.attr, ast.dump(statement)
                )
            )
            if "?" in statement.value:
                assert len(node.args) >= 2, (
                    "the statement in {0}.{1} carries a placeholder but no "
                    "parameters: {2!r}".format(
                        module.__name__, func.attr, statement.value
                    )
                )

    assert checked >= 10, (
        "only {0} statement(s) were checked — the enumeration is probably "
        "broken and the assertions above would be vacuous".format(checked)
    )


def test_illegal_action_is_not_reachable(scheduler: Any,
                                         guarded_client: TestClient) -> None:
    """The action vocabulary is closed, and nothing outside it runs.

    The four endpoints are fixed paths, so an action outside the
    vocabulary must not reach the command mailbox.  It is refused with a
    404 — "there is no such endpoint" — and the command log stays empty:
    the refusal happens before the engine is asked to do anything.
    """
    world = scheduler()
    _make_plan("p-queued", "queued")

    response = guarded_client.post("/api/plan/p-queued/scheduler/destroy")

    assert response.status_code == 404, (
        "an action outside the closed vocabulary answered {0}: {1}".format(
            response.status_code, response.text
        )
    )
    rows = world["conn"].execute(
        "SELECT COUNT(*) FROM scheduler_command"
    ).fetchone()[0]
    assert rows == 0, (
        "an illegal action created {0} command row(s); the vocabulary is "
        "closed and nothing outside it may run".format(rows)
    )


# ---------------------------------------------------------------------------
# 3 — the capability surface
# ---------------------------------------------------------------------------


def test_promote_resume_never_stop(
    scheduler: Any,
    live_pid: int,
    stop_handlers: Any,
    guarded_client: TestClient,
) -> None:
    """Only ``start`` may interrupt a running plan.

    ``promote`` is a change of order and ``resume`` is a **wait**:
    neither takes a slot, neither looks at the capacity, and neither
    interrupts anybody.  The world here is deliberately the most
    dangerous one for that claim — the capacity is full and the plan
    holding it has been running long enough for the eviction rule to
    allow stopping it — so a promote or a resume that stopped somebody
    would be stopping a victim the rules *were* willing to sacrifice.

    The negative control is the other half of the claim: the same
    world, driven through ``start``, **does** stop the victim.  Without
    it a probe that could never see a stop would pass by being blind.
    """
    world = scheduler(capacity=1)
    _make_working_plan(world, "p-victim", ELAPSED_EVICTABLE, live_pid)
    _make_plan("p-queued", "queued")
    _make_plan("p-stopped", "stopped")

    promoted = guarded_client.post("/api/plan/p-queued/scheduler/promote")
    assert promoted.status_code == 200, promoted.text
    assert stop_handlers == [], (
        "promote stopped {0!r}; a promotion is a change of order and "
        "interrupts nobody".format(stop_handlers)
    )

    resumed = guarded_client.post("/api/plan/p-stopped/scheduler/resume")
    assert resumed.status_code == 200, resumed.text
    assert stop_handlers == [], (
        "resume stopped {0!r}; a resume is a wait and interrupts "
        "nobody".format(stop_handlers)
    )

    # The negative control: the same world, through the one action that
    # may interrupt.  If this does not stop the victim, the probe above
    # proves nothing.
    started = guarded_client.post("/api/plan/p-queued/scheduler/start")
    assert started.status_code == 200, started.text
    assert stop_handlers, (
        "start did not stop the victim either — the writer probe is "
        "blind, so the two assertions above would pass vacuously"
    )
    assert ("execution", "p-victim", False) in stop_handlers, stop_handlers


# ---------------------------------------------------------------------------
# 4 — no CORS
# ---------------------------------------------------------------------------


def test_options_grants_no_cors(
    guarded_client: TestClient,
    bare_client: TestClient,
    scheduler: Any,
) -> None:
    """A preflight is never granted CORS.

    The guard's first half is the header: a cross-origin request that
    carries a custom header is no longer a CORS "simple request", so the
    browser must send a preflight first — and a preflight only succeeds
    if the server answers it with CORS headers, which this one
    deliberately never does.  That is the whole mechanism, and a server
    that answered the preflight with ``Access-Control-Allow-Origin``
    would have thrown it away.

    So the preflight is asked twice: bare (as a browser sends it — a
    preflight carries no custom header) and with the guard header (as a
    client that has it would send it).  Neither may come back with a
    CORS header.
    """
    scheduler()

    for url in (STATE_ENDPOINT, "/api/plan/p-queued/scheduler/promote"):
        for client in (bare_client, guarded_client):
            response = client.request(
                "OPTIONS",
                url,
                headers={
                    "Origin": "http://evil.example",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": REQUEST_HEADER,
                },
            )
            for header in (
                "access-control-allow-origin",
                "access-control-allow-methods",
                "access-control-allow-headers",
                "access-control-allow-credentials",
            ):
                assert header not in response.headers, (
                    "OPTIONS {0} answered with {1}: {2}; a preflight that "
                    "is granted CORS is what lets a foreign page make the "
                    "call in the first place".format(
                        url, header, dict(response.headers)
                    )
                )


# ---------------------------------------------------------------------------
# 5 — the routes themselves
# ---------------------------------------------------------------------------


def test_every_scheduler_route_admits_the_plan_id_through_the_guard() -> None:
    """Every route that takes a plan id admits it through the shared guard.

    ``tests/security/test_plan_id_containment.py`` enumerates the routes
    whose path carries the literal ``{plan_id}``.  A route that widens
    the plan id to a path segment (``{plan_id:path}``) is invisible to
    that enumeration, so the property is checked here as well: the guard
    call is **in the body** of every route of this router that takes a
    plan id, so a future edit cannot drop it without failing.
    """
    import inspect

    from routes import scheduler as routes_scheduler

    routes = [
        route
        for route in routes_scheduler.router.routes
        if "plan_id" in getattr(route, "path", "")
    ]
    assert routes, (
        "the scheduler router has no route carrying a plan id — the "
        "enumeration below would be vacuous"
    )
    for route in routes:
        source = inspect.getsource(route.endpoint)
        assert (
            "_plan_dir(plan_id)" in source
            or "_validated_plan_id(plan_id)" in source
        ), (
            "{0} takes a plan id without admitting it through the shared "
            "guard; every path this route could build from that id "
            "inherits the hole".format(route.path)
        )
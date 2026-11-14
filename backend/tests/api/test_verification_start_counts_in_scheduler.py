"""The capacity count follows a verification that was started by hand.

What is pinned here
-------------------
``capacity.used`` is the number of plans that hold compute.  The claim
under test is that this number is not a count of *scheduler slots*: a
plan whose verification round was started directly — through
``POST /api/verification/{id}/start``, with no scheduler involved, no
queue row and no slot row — holds compute all the same, and the panel
has to say so.

The negative control
--------------------
The scheduler never sees this plan.  ``POST /start`` is called straight
against the API, the plan is not in the scheduler's queue, and the
``scheduler_slot`` table has no row for it.  If ``used`` were a count of
slots, it would not move.  It moves, so the count is a count of *plans
that are working*.

``used`` is the union of two sources — the database's rows and the live
set (:func:`server._live_plan_ids`) — and the round that ``/start``
begins is in **both**: its own ``plan_verification`` row says
``running``, and the in-memory record says ``running`` too.  To see the
live set's own contribution, the row has to be quiet — and a row that
has not been refreshed for fifteen minutes is exactly what a round that
is alive but busy looks like to the reader
(:data:`plan_scheduler.facts.VERIFICATION_STALE_SECONDS`; the reader's
clock is a parameter precisely so that the freshness rule can be
exercised without waiting for it).  The reader is therefore given a
clock past that window, and the cases below assert that the rows alone
say nothing: what moves the count is the live set.

The number is read through the **mounted app**: ``GET
/api/scheduler/state`` answers ``capacity.used``, and the engine behind
it is a real one — the real facts reader, the real ledger, the real
snapshot builder — wired exactly the way production wires it, with the
live set injected (:func:`server._live_plan_ids`).  A test that called
the ledger directly would pin the ledger; this one pins the number the
operator's panel shows.

The thread
----------
``/start`` spawns the verification worker.  Here the worker is a shell
that never runs its target (the ``no_worker_thread`` fixture, borrowed
from the CAS suite next door): the round must not really run, and the
round's *record* is what is under test.

``auto_fix=False`` is what puts that record in place.  The default
(``auto_fix=True``) defers the initialisation of the in-memory record
to the auto-loop — that is, to the worker thread — and a worker that
never runs leaves no running record behind.  The single-round path
initialises the record **before** the worker is spawned, so the record
says ``running`` even though the worker is a shell.

Going down again
----------------
``used`` has to come back down when the round is over.  The round ends
the way production ends it: ``POST /api/verification/{id}/stop`` marks
the in-memory record ``loop_stopped`` and parks the persistent rows.
``loop_stopped`` is not in ``_VERIFICATION_IN_FLIGHT_STATUSES``, so the
live set no longer names the plan — the record itself stays, it is the
predicate that leaves it out — and the number the panel shows returns to
where it was before the start.

Every case here ends its round through that route.  Nothing reaches
into the process's own tables to make the count come down: a case that
popped the record by hand would go green while the production release
path was broken, and the number under test is the number the operator's
panel shows.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterator, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

from plan_scheduler.facts import VERIFICATION_STALE_SECONDS  # noqa: E402

# The fixtures are borrowed from the CAS suite next door: the plan it
# builds is a real verification-branch plan (a real git baseline, a real
# machine plan on disk, a real review on disk), and that is what
# ``/start`` needs to be let through its CAS.  Importing the fixtures
# rather than copying them keeps the two suites describing one plan.
from backend.tests.api.test_start_verification_cas import (  # noqa: E402
    git_repo,
    hermetic_env,
    make_verification_plan,
    no_worker_thread,
)

client = TestClient(server.app)

#: The endpoint the operator's panel reads.
STATE_PATH = "/api/scheduler/state"

#: The capacity the engine is built with.  It only has to be a whole
#: number: the number under test is ``used``, not ``total``.
CONFIGURED_CAPACITY = 4


class _StaticConfig:
    """A configuration loader that always answers the same snapshot."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def current(self) -> Any:
        """Return the snapshot."""
        return self._config


def _reader_clock() -> float:
    """The facts reader's "now" — past the window a quiet row lives in.

    The reader treats a ``plan_verification`` row that has not been
    refreshed for fifteen minutes as belonging to a thread that died
    (:data:`plan_scheduler.facts.VERIFICATION_STALE_SECONDS`).  A round
    that is alive but busy writes its row once and lets its heartbeat
    refresh it — and a round whose heartbeat has been quiet for a
    quarter of an hour looks exactly like that to the reader.

    The reader's clock is a parameter so that this rule can be
    exercised without waiting for it, and it is exercised here: the
    clock is a minute past the window, so the row ``/start`` writes is
    quiet in the reader's eyes.  What is left to hold the count is the
    live set.
    """
    return time.time() + VERIFICATION_STALE_SECONDS + 60.0


def _open_state_db() -> sqlite3.Connection:
    """Open the case's state database with the production PRAGMA contract."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    migrate(conn)
    return conn


@pytest.fixture
def scheduler_engine(
    hermetic_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    """A real engine behind the mounted app, wired the way production wires it.

    The worker is **not** started — the panel only reads the state, and
    a tick would write to the very tables these cases measure.

    The facts reader is handed :func:`server._live_plan_ids`, which is
    the wiring under test: without it the reader answers from the
    database's rows alone, and the union the live set exists for never
    happens.
    """
    from plan_lifecycle_service import PlanLifecycleService
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.commands import CommandExecutor
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.engine import PlanSchedulerEngine
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore

    conn = _open_state_db()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(
        conn,
        Path(server.PLANS_DIR),
        clock=_reader_clock,
        live_plan_ids=server._live_plan_ids,
    )
    ledger = CapacityLedger(store, facts, time.time, conn=conn)
    commands = CommandExecutor(
        store,
        ledger,
        PlanLifecycleService(),
        facts=facts,
        conn=conn,
        capacity=CONFIGURED_CAPACITY,
        clock=time.time,
    )
    engine = PlanSchedulerEngine(
        _StaticConfig(
            SchedulerConfig(enabled=True, capacity=CONFIGURED_CAPACITY)
        ),
        store=store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=object(),
        clock=time.time,
        conn=conn,
    )
    # The engine keeps the configuration snapshot it was built with; the
    # reader takes it from the loader, so the snapshot is refreshed here
    # the way the first tick refreshes it.
    engine._config = engine._read_config()

    monkeypatch.setattr(
        server.app.state, "scheduler_engine", engine, raising=False
    )
    try:
        yield engine
    finally:
        conn.close()


def _used(client: TestClient) -> int:
    """The number of plans holding compute, as the panel reads it."""
    response = client.get(STATE_PATH)
    assert response.status_code == 200, response.text
    return int(response.json()["capacity"]["used"])


def _slot_rows(plan_id: str) -> List[Any]:
    """The scheduler's own slots for ``plan_id`` — the thing being falsified."""
    conn = _open_state_db()
    try:
        return conn.execute(
            "SELECT plan_id FROM scheduler_slot WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()


def _active_plans_without_the_live_set() -> List[Any]:
    """The database's own answer — the live set not consulted.

    A reader with no callback is the reader as it was before the live
    set existed.  What it says is what the count would be if the count
    came from the rows alone.
    """
    from plan_scheduler.facts import PlanFactsReader

    conn = _open_state_db()
    try:
        reader = PlanFactsReader(
            conn, Path(server.PLANS_DIR), clock=_reader_clock
        )
        return reader.active_plans()
    finally:
        conn.close()


def _stop_through_the_route(plan_id: str) -> dict:
    """Stop the round the way production stops it — through the route.

    ``POST /api/verification/{id}/stop`` takes no request body.  The
    ``200`` is itself part of the assertion: the route answers ``409``
    when its compare-and-set on the routing row does not match, so a
    ``200`` says the release sequence really ran.
    """
    response = client.post(f"/api/verification/{plan_id}/stop")
    assert response.status_code == 200, response.text
    return response.json()


def _verification_row(plan_id: str) -> dict:
    """The persistent ``plan_verification`` row, read through its repository."""
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _open_state_db()
    try:
        row = VerificationRepository(conn).current(plan_id)
        assert row is not None, f"no plan_verification row for {plan_id}"
        return dict(row)
    finally:
        conn.close()


def _routing_row(plan_id: str) -> dict:
    """The persistent ``plan_routing`` row, read through its repository."""
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = _open_state_db()
    try:
        row = RoutingRepository(conn).current(plan_id)
        assert row is not None, f"no plan_routing row for {plan_id}"
        return dict(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1 — a verification started by hand holds compute
# ---------------------------------------------------------------------------


def test_start_verification_increments_used(
    scheduler_engine,
    make_verification_plan,
    no_worker_thread,
) -> None:
    """``POST /start`` → ``used`` is one higher, without a scheduler slot.

    The plan never went through the scheduler: it was not queued, it was
    not admitted, and it holds no ``scheduler_slot`` row.  If ``used``
    were a count of slots, it would not move — so the count is a count
    of plans that are working.
    """
    plan = make_verification_plan()

    before = _used(client)
    assert before == 0, (
        f"the case starts with something working already: {before}"
    )

    response = client.post(
        f"/api/verification/{plan.plan_id}/start",
        json={"max_rounds": 1, "auto_fix": False},
    )
    assert response.status_code == 200, response.text

    assert _used(client) == before + 1

    # The negative control's teeth: the scheduler gave this plan no
    # slot, so the number that moved is not a count of slots.
    assert _slot_rows(plan.plan_id) == [], (
        "the scheduler gave the plan a slot — the count is not the "
        "negative control it was meant to be"
    )
    # The rows alone hold nothing: the round's own row has gone quiet,
    # and what is left is the in-memory record.
    assert _active_plans_without_the_live_set() == []
    # And that record says the round is running.
    assert plan.plan_id in server._live_plan_ids()
    assert server._verification_state[plan.plan_id]["verification_status"] == (
        "running"
    )

    # The round is ended the way production ends it, so this case's own
    # teardown cannot be mistaken for the release path being exercised.
    _stop_through_the_route(plan.plan_id)


# ---------------------------------------------------------------------------
# 2 — and it lets go again when the round is over
# ---------------------------------------------------------------------------


def test_stopping_verification_decrements_used(
    scheduler_engine,
    make_verification_plan,
    no_worker_thread,
) -> None:
    """A started verification → ``used`` goes up; a stopped one → back down.

    The round is stopped the way production stops it — through
    ``POST /api/verification/{id}/stop``.  That route marks the
    in-memory record ``loop_stopped`` and parks the persistent rows;
    ``loop_stopped`` is not in ``_VERIFICATION_IN_FLIGHT_STATUSES``, so
    the live set no longer names the plan, and the number the panel
    shows returns to where it was before the start.

    Every step of that release is asserted here, because each of them is
    a place where the count could stay up: the answer the operator got,
    the record the process kept, the persistent verification row, the
    routing row, and the panel's own reading.
    """
    plan = make_verification_plan()

    before = _used(client)

    started = client.post(
        f"/api/verification/{plan.plan_id}/start",
        json={"max_rounds": 1, "auto_fix": False},
    )
    assert started.status_code == 200, started.text
    assert _used(client) == before + 1
    assert plan.plan_id in server._live_plan_ids()

    body = _stop_through_the_route(plan.plan_id)

    # a) The answer the operator got.
    assert body["status"] == "stopped"
    assert body["paused"] is True
    assert body["reason"] == "user_stopped"

    # b) The record the process kept: the route demotes it, it does not
    #    delete it.
    assert plan.plan_id in server._verification_state
    state = server._verification_state[plan.plan_id]
    assert state["verification_status"] == "loop_stopped"
    assert state["stop_reason"] == "user_stopped"

    # c) The persistent row says the same thing.
    assert _verification_row(plan.plan_id)["verification_status"] == (
        "loop_stopped"
    )

    # d) The routing row is parked by the stop's compare-and-set.
    assert _routing_row(plan.plan_id)["current_phase"] == "verification"

    # e) And the panel's own reading has come back down.
    assert plan.plan_id not in server._live_plan_ids()
    assert _used(client) == before


# ---------------------------------------------------------------------------
# 3 — a start that is refused changes nothing
# ---------------------------------------------------------------------------


def test_refused_start_leaves_used_unchanged(
    scheduler_engine,
    no_worker_thread,
) -> None:
    """A plan that is not there → the start is refused, ``used`` does not move."""
    before = _used(client)

    response = client.post(
        "/api/verification/verify-plan-that-does-not-exist/start",
        json={"max_rounds": 1},
    )

    assert response.status_code != 200, response.text
    assert _used(client) == before
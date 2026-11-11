"""Integration: a resume names the plan it cannot resume.

The defect
----------
``_do_resume`` answered ``no_such_plan`` for every plan the scheduler
does not know, and the answer never said **why** it did not know it.
Two situations that are nothing alike were answered the same way:

* the plan whose **import failed** — its directory is on disk with the
  material that was imported, it is registered as ``import_failed``, and
  its execution row carries the words the import refused it with;
* the **orphan** — a directory that is on disk with a plan's own files
  in it and no row behind it anywhere.  A directory like that is left
  over from before the import registered its failures, and it is the
  material an operator needs in order to find out what happened.

Both were answered ``no_such_plan``: the operator was told to go and
look for a plan that was right there on the disk, with the reason it
was refused nowhere to be found.

What this file holds down
--------------------------
1. An ``import_failed`` plan is a **conflict** that names the reason —
   the words the import wrote, unchanged.
2. A directory with no row behind it is a **not found** that says the
   directory is there.
3. A plan that is not there at all is still ``no_such_plan``.
4. A directory that holds no plan's files is still ``no_such_plan``.
5. A registered plan is still resumed the way it was — the original
   branch, unchanged.

The answers are read at the **HTTP boundary**
---------------------------------------------
The status is part of the answer, not a decoration on it: a conflict is
409 and a plan that is not there is 404.  So the cases below go
through the route — the route's own translation of the executor's
outcome into a status, and the body it writes.

The engine behind the route is the production one with the worker's
mailbox hop left out: the command the route carries out is the
executor's own ``CommandResult``, and the stand-in hands that answer
straight back.  Everything these cases are about — the facts reader,
the executor, the route, the status and the body — is the production
one.

The two refusals do not touch the world
----------------------------------------
A plan that cannot be resumed is not resumed: the lifecycle is not
asked to move it and no queue row is written for it.  A refusal that
wrote a queue row would put a plan that was refused at the door into
the line, and the tick would start it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient


#: The words the import wrote on the plan when it refused the material.
IMPORT_REASON = "task 1: files_to_modify is empty"

#: The detail the import-failed refusal carries.
IMPORT_FAILED_DETAIL = "plan is in import_failed: {0}".format(IMPORT_REASON)

#: The detail the orphan refusal carries.
ORPHAN_DETAIL = "plan directory exists but is not registered"

#: The detail a plan that is not there at all carries.
NO_SUCH_PLAN_DETAIL = "no_such_plan"


# ---------------------------------------------------------------------------
# The world the cases drive
# ---------------------------------------------------------------------------


def _plans_root() -> Path:
    """The plans tree the facade resolves plans in.

    The suite redirects ``server.PLANS_DIR`` at ``tmp_path`` — the facade
    resolves a plan through ``server._plan_dir``, so the case has to
    write where the facade looks.
    """
    import server

    return Path(server.PLANS_DIR)


def _open_state_db() -> sqlite3.Connection:
    """Open the case's state database with the production PRAGMA contract."""
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    migrate(conn)
    return conn


def _write_plan_files(plan_dir: Path, *names: str) -> None:
    """Write the named plan files into ``plan_dir``.

    A directory that holds one of the plan's own files is a plan's
    directory; a directory that holds none of them is not a plan.
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        if name == "tasks.json":
            payload: Any = {
                "requirement": "a plan that could not be imported",
                "tasks": [
                    {
                        "id": "task-1",
                        "title": "The task the import refused",
                        "test_command": "true",
                    }
                ],
            }
        elif name == "import.json":
            payload = {"material": "the material that was imported"}
        else:
            payload = {"title": f"Plan {plan_dir.name}"}
        (plan_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def _register_import_failed(plan_id: str, reason: str) -> None:
    """Register the plan as ``import_failed``, the way the import does.

    The two writes are the import's own: the phase goes into
    ``plan_routing`` and the reason goes onto the plan's execution row,
    word for word.  A case that wrote them any other way would be
    testing a world the import never produces.
    """
    import server
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    server._seed_plan_routing_phase(plan_id, phase="import_failed")

    conn = _open_state_db()
    try:
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            stop_reason=reason,
        )
    finally:
        conn.close()


def _phase_in_db(conn: sqlite3.Connection, plan_id: str) -> Optional[str]:
    """The phase the plan carries **in the database**."""
    row = conn.execute(
        "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def _stop_reason_in_db(
    conn: sqlite3.Connection, plan_id: str
) -> Optional[str]:
    """The reason the plan's execution row carries."""
    row = conn.execute(
        "SELECT stop_reason FROM plan_execution WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    return None if row is None else row[0]


def _queue_order(conn: sqlite3.Connection) -> List[str]:
    """The plan ids the scheduler queue holds, in the queue's order."""
    from plan_scheduler.store import SqliteSchedulerStore

    return [
        str(row["plan_id"])
        for row in SqliteSchedulerStore().list_queue(conn)
    ]


# ---------------------------------------------------------------------------
# The engine the route talks to
# ---------------------------------------------------------------------------


class _RecordingLifecycle:
    """The real facade, with the calls written down.

    A refusal that asked the lifecycle to move the plan would have
    moved it — and the case has to be able to see that it did not.
    """

    def __init__(self) -> None:
        from plan_lifecycle_service import PlanLifecycleService

        self._facade = PlanLifecycleService()
        self.calls: List[Tuple[str, str]] = []

    def requeue_plan(self, plan_id: str) -> Any:
        """Answer the requeue, and write the call down."""
        self.calls.append(("requeue_plan", plan_id))
        return self._facade.requeue_plan(plan_id)

    def pause_plan(self, plan_id: str) -> Any:
        """Answer the pause, and write the call down."""
        self.calls.append(("pause_plan", plan_id))
        return self._facade.pause_plan(plan_id)

    def __getattr__(self, name: str) -> Any:
        """Everything else is the production facade's."""
        return getattr(self._facade, name)


class _EngineThatCarriesTheCommandOut:
    """The engine the route reads, with the worker's mailbox hop left out.

    The real engine puts the command in a mailbox and a worker thread
    takes it from there; the **command** is the same one either way —
    the executor's own ``CommandResult``.  So the stand-in hands the
    executor's answer straight back to the route, and the route's own
    translation of that answer into a status is the production one.
    """

    #: The route asks the engine whether it is running at all.
    enabled = True

    def __init__(self, executor: Any) -> None:
        self._executor = executor
        self.commands: List[Tuple[str, str]] = []
        self.results: List[Any] = []

    def submit_command(self, action: str, plan_id: str, **_: Any) -> Any:
        """Carry the command out and hand the answer back."""
        self.commands.append((action, plan_id))
        result = self._executor.resume(plan_id)
        self.results.append(result)
        return result


class World:
    """What a case drives: the route, the executor and the database.

    Attributes:
        client: the client the route is reached through.
        executor: the production command executor.
        engine: the stand-in engine behind the route.
        lifecycle: the recording facade.
        conn: the case's connection to the state database.
    """

    def __init__(
        self,
        client: TestClient,
        executor: Any,
        engine: _EngineThatCarriesTheCommandOut,
        lifecycle: _RecordingLifecycle,
        conn: sqlite3.Connection,
    ) -> None:
        """Hold the four things the case drives."""
        self.client = client
        self.executor = executor
        self.engine = engine
        self.lifecycle = lifecycle
        self.conn = conn

    def resume(self, plan_id: str) -> Tuple[int, Dict[str, Any]]:
        """Ask for a resume, and return the status and the body."""
        response = self.client.post(
            "/api/plan/{0}/scheduler/resume".format(plan_id)
        )
        return response.status_code, response.json()


@pytest.fixture
def world(monkeypatch) -> Any:
    """The route, the executor and the database they share."""
    import server
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.commands import CommandExecutor
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore

    conn = _open_state_db()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, _plans_root())
    ledger = CapacityLedger(store, facts)
    lifecycle = _RecordingLifecycle()
    executor = CommandExecutor(
        store,
        ledger,
        lifecycle,
        facts=facts,
        conn=conn,
    )
    engine = _EngineThatCarriesTheCommandOut(executor)
    monkeypatch.setattr(
        server.app.state, "scheduler_engine", engine, raising=False
    )

    try:
        yield World(
            client=TestClient(server.app),
            executor=executor,
            engine=engine,
            lifecycle=lifecycle,
            conn=conn,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. The import-failed plan names the reason
# ---------------------------------------------------------------------------


def test_resume_import_failed_names_reason(world: Any) -> None:
    """A plan whose import failed is refused, and the reason is in the answer.

    The import wrote the reason on the plan — that is where it lives
    once the HTTP response has been thrown away.  The refusal has to
    carry it: "this plan cannot be resumed" is not an answer an
    operator can act on, and "this plan cannot be resumed **because the
    import was refused with these words**" is.

    The words are the import's own, unchanged.

    And the refusal moves nothing: the lifecycle is not asked to requeue
    the plan, and no queue row is written for it.  A plan that was
    refused at the door must not end up in the line.
    """
    plan_id = "import-failed-p1"
    plan_dir = _plans_root() / plan_id
    _write_plan_files(plan_dir, "tasks.json", "import.json")
    _register_import_failed(plan_id, IMPORT_REASON)

    assert _phase_in_db(world.conn, plan_id) == "import_failed"

    status, body = world.resume(plan_id)

    assert status == 409, (
        f"the refusal of an import-failed plan is not a conflict: {body!r}"
    )
    assert body["detail"] == IMPORT_FAILED_DETAIL, (
        f"the refusal does not name the reason the import gave: {body!r}"
    )
    # Nothing happened: the lifecycle was not asked, and the plan is not
    # in the queue.
    assert world.lifecycle.calls == [], (
        "the refusal asked the lifecycle to move the plan: {0!r}".format(
            world.lifecycle.calls
        )
    )
    assert _queue_order(world.conn) == [], (
        f"the refused plan is in the queue: {_queue_order(world.conn)!r}"
    )
    assert _phase_in_db(world.conn, plan_id) == "import_failed", (
        "the refusal moved the plan's phase"
    )


# ---------------------------------------------------------------------------
# 2. The orphan directory is named
# ---------------------------------------------------------------------------


def test_resume_orphan_directory_is_named(world: Any) -> None:
    """A directory with no row behind it is named, not called "no such plan".

    The directory is there, with a plan's own files in it — the material
    an operator reads in order to find out what happened.  Saying "no
    such plan" sends them looking for a plan that is right there on the
    disk; saying "the directory exists but is not registered" points at
    the thing that is actually wrong.
    """
    plan_id = "orphan-p1"
    plan_dir = _plans_root() / plan_id
    _write_plan_files(plan_dir, "tasks.json")

    # No row behind it: the directory is an orphan.
    assert _phase_in_db(world.conn, plan_id) is None

    status, body = world.resume(plan_id)

    assert status == 404, (
        f"the refusal of an orphan directory is not a not-found: {body!r}"
    )
    assert body["detail"] == ORPHAN_DETAIL, (
        f"the refusal does not name the directory: {body!r}"
    )
    assert world.lifecycle.calls == [], (
        "the refusal asked the lifecycle to move the plan: {0!r}".format(
            world.lifecycle.calls
        )
    )


# ---------------------------------------------------------------------------
# 3. A plan that is not there is still "no such plan"
# ---------------------------------------------------------------------------


def test_missing_plan_still_no_such_plan(world: Any) -> None:
    """A plan with neither a row nor a directory is still "no such plan".

    The two refusals above are about a plan that is *somehow* there.
    A plan that is not there at all is the plain case, and its plain
    answer has to survive: it is the answer every other caller relies
    on, and a new wording would break them for nothing.
    """
    plan_id = "missing-p1"
    assert not (_plans_root() / plan_id).exists()

    status, body = world.resume(plan_id)

    assert status == 404, (
        f"the refusal of a plan that is not there is not a not-found: {body!r}"
    )
    assert body["detail"] == NO_SUCH_PLAN_DETAIL, (
        f"the refusal of a plan that is not there changed its wording: {body!r}"
    )


# ---------------------------------------------------------------------------
# 4. A directory that holds no plan is not a plan
# ---------------------------------------------------------------------------


def test_empty_directory_is_not_a_plan(world: Any) -> None:
    """A directory with no plan's files in it is not a plan.

    The directory exists, but there is nothing in it that belongs to a
    plan — no task list, no PRD, no imported material, no interview.
    A directory like that is a directory that happens to be there, and
    it must not be answered about as if a plan were in it: the orphan
    wording would send the operator looking for a plan's material that
    is not there.
    """
    plan_id = "empty-p1"
    plan_dir = _plans_root() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    # A log file is not a plan's file.
    (plan_dir / "execution.log").write_text("nothing to see\n", encoding="utf-8")

    status, body = world.resume(plan_id)

    assert status == 404, (
        f"the refusal of an empty directory is not a not-found: {body!r}"
    )
    assert body["detail"] == NO_SUCH_PLAN_DETAIL, (
        f"an empty directory was answered about as a plan: {body!r}"
    )


# ---------------------------------------------------------------------------
# 5. A registered plan is still resumed the way it was
# ---------------------------------------------------------------------------


def test_registered_plan_unaffected(world: Any) -> None:
    """A registered plan goes through the original branch, unchanged.

    A ``stopped`` plan is resumed: the phase moves to ``queued``, the
    queue row is written, and the answer is ``applied``.  A plan that
    is already waiting is refused by the lifecycle — the phase it is in
    has no edge back into the queue — and that refusal is the
    lifecycle's own, not one of the two new wordings.

    Neither of them may be caught by the new refusals: they are
    registered plans, and the registered plans are the ones the operator
    came for.
    """
    from plan_state import PlanState

    stopped_id = "registered-stopped-p1"
    stopped_dir = _plans_root() / stopped_id
    _write_plan_files(stopped_dir, "tasks.json", "prd.json")
    (stopped_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": stopped_id, "current_phase": "stopped"}),
        encoding="utf-8",
    )
    # Constructing a ``PlanState`` writes nothing (2026-11-11), so the
    # routing row is created explicitly rather than as a side effect.
    PlanState(stopped_dir).ensure_row()

    status, body = world.resume(stopped_id)

    assert status == 200, (
        f"the resume of a registered plan did not go through: {body!r}"
    )
    assert body["outcome"] == "applied", (
        f"the resume of a registered plan was not applied: {body!r}"
    )
    assert body["detail"] == "resumed", (
        f"the applied resume does not say what it did: {body!r}"
    )
    assert _phase_in_db(world.conn, stopped_id) == "queued", (
        "the resumed plan is not waiting: {0!r}".format(
            _phase_in_db(world.conn, stopped_id)
        )
    )
    assert _queue_order(world.conn) == [stopped_id], (
        f"the resumed plan is not in the queue: {_queue_order(world.conn)!r}"
    )

    # A plan that is already waiting: the lifecycle's own refusal.
    queued_id = "registered-queued-p1"
    queued_dir = _plans_root() / queued_id
    _write_plan_files(queued_dir, "tasks.json", "prd.json")
    (queued_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": queued_id, "current_phase": "queued"}),
        encoding="utf-8",
    )
    PlanState(queued_dir).ensure_row()

    status, body = world.resume(queued_id)

    assert status == 409, (
        f"the refusal of a waiting plan is not a conflict: {body!r}"
    )
    assert body["detail"] not in (IMPORT_FAILED_DETAIL, ORPHAN_DETAIL), (
        f"a registered plan was caught by one of the new refusals: {body!r}"
    )


# ---------------------------------------------------------------------------
# 6. The two facts the refusals are made of
# ---------------------------------------------------------------------------


def test_facts_read_the_reason_and_the_directory(world: Any) -> None:
    """The facts reader answers the two questions the refusals are made of.

    ``import_failed_reason`` reads the reason off the plan's execution
    row — the place the import wrote it — and ``plan_directory_exists``
    answers whether a plan's directory is on disk.  The second is the
    stricter question: a directory that holds none of a plan's own files
    is not a plan's directory, however much of a directory it is.
    """
    from plan_scheduler.facts import PlanFactsReader

    facts = PlanFactsReader(world.conn, _plans_root())

    plan_id = "import-failed-p1"
    _write_plan_files(_plans_root() / plan_id, "tasks.json", "import.json")
    _register_import_failed(plan_id, IMPORT_REASON)

    assert facts._phase(plan_id) == "import_failed"
    assert facts.import_failed_reason(plan_id) == IMPORT_REASON, (
        "the reason is not the one the import wrote: {0!r}".format(
            facts.import_failed_reason(plan_id)
        )
    )

    orphan_id = "orphan-facts-p1"
    _write_plan_files(_plans_root() / orphan_id, "tasks.json")
    assert facts._phase(orphan_id) is None
    assert facts.plan_directory_exists(orphan_id) is True, (
        "the directory of a plan with no row behind it was not found"
    )

    empty_id = "empty-facts-p1"
    (_plans_root() / empty_id).mkdir(parents=True, exist_ok=True)
    assert facts.plan_directory_exists(empty_id) is False, (
        "a directory with no plan's files in it was called a plan"
    )
    assert facts.plan_directory_exists("no-such-plan-at-all") is False, (
        "a plan that is not there was called a plan"
    )

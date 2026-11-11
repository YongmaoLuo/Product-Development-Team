"""Integration: a resume is **applied** only when the plan really came back.

The defect
----------
``_do_resume`` answered ``applied`` unconditionally.  It asked the
lifecycle to requeue the plan, wrote the queue row, and said
``"resumed"`` — without ever asking whether the plan had actually come
back.  Two things can go wrong, and both of them end the same way: the
plan is gone and the operator was told it was fine.

* The lifecycle says ``ok`` but the **phase never moved** — the plan is
  still ``failed``, the queue says it is waiting, and nothing will ever
  start it.
* The phase moved but the **queue row is not there** — the tick that
  pulls the head of the queue ran in between, and the plan was dropped
  on the floor.

The truth is read **after** the fact, not taken from the request: the
phase the plan is really in, the queue row that really exists, and
whether the plan is really working.  A resume is applied when the plan
is waiting again — or when the engine has already pulled it up, which
is the same thing seen one moment later.

What this file holds down
-------------------------
1. ``applied`` requires the phase to be ``queued`` — otherwise the
   answer is a conflict that names the phase the plan is really in.
2. ``applied`` requires a queue row — a phase of ``queued`` with no row
   in the queue is a conflict too, and it says so.
3. A plan the **engine pulled up** counts as applied: it is working, so
   it was not lost.
4. A plan with **no phase** is still ``not_found`` — before the resume
   and after it.
5. The **happy path** is still applied: ``stopped → queued`` puts the
   plan at the head of the queue.
6. The **negative control**: a plan with failed tasks is never answered
   ``applied`` unless it is really in the queue or really working.

The doubles are narrow on purpose
----------------------------------
The phase and the queue row are two facts in **one** SQLite file, and
the executor reads both of them there.  So the database is real, the
store is real, and only the two things that have to go wrong go wrong:

* the **lifecycle** is a stand-in that answers ``ok`` and does what the
  case says — because "the facade said yes and the phase did not move"
  is not something the real facade can be asked to do;
* the **store** is the production one, with the queue row taken away
  again — because "the row is not there any more" is what the tick
  does, and it happens between the write and the check.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

import pytest


#: A fixed moment, so that every case writes the same timestamps.
BASE_EPOCH = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(epoch: float = BASE_EPOCH) -> str:
    """Return the moment as the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _plans_root() -> Path:
    """The plans tree the facade resolves plans in.

    The suite redirects ``server.PLANS_DIR`` at ``tmp_path`` — the facade
    resolves a plan through ``server._plan_dir``, so the case has to
    write where the facade looks.
    """
    import server

    return Path(server.PLANS_DIR)


def _make_plan(plan_id: str, phase: str) -> Path:
    """Create a plan directory whose phase is ``phase``.

    The phase is written as the legacy ``plan_state.json`` snapshot and
    then materialised into ``plan_routing`` — the same path a plan
    that was bootstrapped by the API takes.  The row is not optional
    here: the scheduler reads the phase from ``plan_routing``, and a
    plan whose row was never written is a plan the scheduler does not
    know.  Since 2026-11-11 constructing a ``PlanState`` writes
    nothing, so the row is created explicitly via ``ensure_row()``.
    """
    from plan_state import PlanState

    plan_dir = _plans_root() / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({"plan_id": plan_id, "current_phase": phase}),
        encoding="utf-8",
    )
    # Constructing a ``PlanState`` writes nothing (2026-11-11), so the
    # routing row is created explicitly rather than as a side effect.
    PlanState(plan_dir).ensure_row()
    return plan_dir


def _write_tasks(plan_dir: Path, statuses: List[str]) -> None:
    """Write a task list whose tasks carry ``statuses``, in order."""
    tasks = [
        {
            "id": f"task-{index + 1}",
            "title": f"Task {index + 1}",
            "status": status,
        }
        for index, status in enumerate(statuses)
    ]
    (plan_dir / "tasks.json").write_text(
        json.dumps({"requirement": "a resumed plan", "tasks": tasks}),
        encoding="utf-8",
    )


def _phase_in_db(conn: sqlite3.Connection, plan_id: str) -> Optional[str]:
    """The phase the plan carries **in the database**."""
    row = conn.execute(
        "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def _queue_order(conn: sqlite3.Connection) -> List[str]:
    """The plan ids the scheduler queue holds, in the queue's order."""
    from plan_scheduler.store import SqliteSchedulerStore

    return [
        str(row["plan_id"])
        for row in SqliteSchedulerStore().list_queue(conn)
    ]


def _set_phase(conn: sqlite3.Connection, plan_id: str, phase: str) -> None:
    """Move the plan's phase in the database.

    The phase belongs to the state machine; the **case** may write it
    (the case is the world, not the code under test), and the executor
    may not.
    """
    conn.execute(
        "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
        (phase, plan_id),
    )


def _write_running_execution_row(
    conn: sqlite3.Connection, plan_id: str, pid: int
) -> None:
    """Give the plan the execution row of a run that is working.

    The facts reader says a plan is working when its execution row says
    ``running`` **and** the pid it names is still alive — the row alone
    is not enough, and neither half is.
    """
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, project_dir, next_run_at, exec_pid, exec_status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            plan_id,
            str(_plans_root() / plan_id),
            None,
            pid,
            "running",
            _iso(),
        ),
    )


class _LifecycleResult:
    """What a lifecycle answers with.

    The executor reads ``ok`` and ``detail`` off the answer; the rest is
    here so the stand-in has the same shape as the real result.
    """

    def __init__(
        self,
        *,
        ok: bool = True,
        detail: str = "requeue",
        final_phase: str = "queued",
    ) -> None:
        self.ok = ok
        self.detail = detail
        self.final_phase = final_phase


class _LifecycleSaysYes:
    """A lifecycle that says yes, and does what the case tells it to.

    The real facade moves the phase through the state machine; a case
    that has to show what happens when the phase does **not** move needs
    a facade that answers ``ok`` and leaves the phase alone.  The
    ``on_requeue`` callback is where the case says what the world did.
    """

    def __init__(
        self, on_requeue: Optional[Callable[[str], None]] = None
    ) -> None:
        self._on_requeue = on_requeue
        self.calls: List[str] = []

    def requeue_plan(self, plan_id: str) -> _LifecycleResult:
        """Answer the requeue, and let the case change the world."""
        self.calls.append(plan_id)
        if self._on_requeue is not None:
            self._on_requeue(plan_id)
        return _LifecycleResult(ok=True, detail="requeue")


class _StoreThatLosesTheQueueRow:
    """A store whose promoted plan does not stay in the queue.

    It is the production store with one thing changed: the queue row is
    written and then taken away again — the tick that pulls the head of
    the queue ran in between — and the plan never came back.  The
    subclass keeps every other behaviour (the command log, the slots)
    exactly as it is.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        """Everything but ``promote`` is the production store's."""
        return getattr(self._store, name)

    def promote(
        self,
        conn: sqlite3.Connection,
        plan_id: str,
        now: Optional[str] = None,
    ) -> Optional[int]:
        """Promote the plan — and then lose its queue row."""
        order = self._store.promote(conn, plan_id, now)
        conn.execute(
            "DELETE FROM scheduler_queue WHERE plan_id = ?", (plan_id,)
        )
        return order


def _executor(
    conn: sqlite3.Connection,
    lifecycle: Any = None,
    store: Any = None,
) -> Any:
    """A command executor over the case's database.

    The collaborators are the production ones: the store, the capacity
    ledger, the facts reader and — unless the case says otherwise — the
    lifecycle facade.  Only the connection is the case's own.
    """
    from plan_lifecycle_service import PlanLifecycleService
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.commands import CommandExecutor
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore

    store = SqliteSchedulerStore() if store is None else store
    facts = PlanFactsReader(conn, _plans_root())
    ledger = CapacityLedger(store, facts)
    return CommandExecutor(
        store,
        ledger,
        PlanLifecycleService() if lifecycle is None else lifecycle,
        facts=facts,
        conn=conn,
    )


def _live_pid(case_registry: Any) -> int:
    """The pid of a process that is running right now."""
    process = case_registry.spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    return int(process.pid)


# ---------------------------------------------------------------------------
# 1. The phase has to be ``queued`` for the answer to be ``applied``
# ---------------------------------------------------------------------------


def test_applied_requires_queued_phase(db_factory: Any) -> None:
    """A facade that says "ok" without moving the phase is a conflict.

    The facade's answer is a **request**, not a fact: what counts is
    where the plan really is afterwards.  A plan that is still
    ``failed`` has not come back, and answering ``applied`` would say
    it had — the plan would sit in the queue with a phase nothing
    starts, and the operator would be told it was fine.

    The detail names the phase the plan is really in, so the caller can
    see what happened without going and looking.
    """
    db = db_factory()
    conn = db.connect()
    try:
        _make_plan("p1", "failed")
        # The facade says yes and moves nothing.
        executor = _executor(conn, lifecycle=_LifecycleSaysYes())

        result = executor.resume("p1")

        assert result.outcome == "conflict", (
            f"a resume that did not move the phase was applied: {result!r}"
        )
        assert result.detail == "resume_not_queued:failed", (
            f"the conflict does not name the phase the plan is in: {result!r}"
        )
        # And the plan really is still where it was.
        assert _phase_in_db(conn, "p1") == "failed", (
            "the resume moved the phase itself: {0!r}".format(
                _phase_in_db(conn, "p1")
            )
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. The queue row has to be there for the answer to be ``applied``
# ---------------------------------------------------------------------------


def test_applied_requires_queue_row(db_factory: Any) -> None:
    """A phase of ``queued`` with no queue row is a conflict.

    The phase says the plan is waiting; the queue row is where it
    waits.  A plan that has one without the other is a plan nobody will
    start — the queue is what the engine reads, and the row is not
    there.

    The row is written and then taken away again, which is what the
    tick that pulls the head of the queue does to a plan it starts.
    Here the plan is not started: it is dropped, and the answer has to
    say so.
    """
    db = db_factory()
    conn = db.connect()
    try:
        _make_plan("p1", "stopped")

        def _move_to_queued(plan_id: str) -> None:
            _set_phase(conn, plan_id, "queued")

        from plan_scheduler.store import SqliteSchedulerStore

        store = _StoreThatLosesTheQueueRow(SqliteSchedulerStore())
        executor = _executor(
            conn,
            lifecycle=_LifecycleSaysYes(on_requeue=_move_to_queued),
            store=store,
        )

        result = executor.resume("p1")

        assert result.outcome == "conflict", (
            f"a resume whose queue row vanished was applied: {result!r}"
        )
        assert result.detail == "resume_not_queued:queued", (
            f"the conflict does not name the missing queue row: {result!r}"
        )
        # The row really is gone, and the plan is not working either.
        assert "p1" not in _queue_order(conn), (
            f"the plan is in the queue after all: {_queue_order(conn)!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. A plan the engine pulled up counts as applied
# ---------------------------------------------------------------------------


def test_running_counts_as_applied(
    db_factory: Any, case_registry: Any
) -> None:
    """A plan the engine picked up in the meantime is applied.

    The queue row is gone — that is what the engine does when it takes
    the head of the queue — but the plan is **working**, so it was not
    lost.  Answering ``conflict`` here would be wrong: the operator
    would be told the resume failed while the plan runs.

    The plan is made to work by the facade's own callback, so the plan
    is *not* working when the resume starts — the resume is what
    brought it back, and the engine took it from there.
    """
    db = db_factory()
    conn = db.connect()
    try:
        _make_plan("p1", "stopped")
        pid = _live_pid(case_registry)

        def _pulled_up(plan_id: str) -> None:
            _set_phase(conn, plan_id, "queued")
            _write_running_execution_row(conn, plan_id, pid)

        from plan_scheduler.store import SqliteSchedulerStore

        store = _StoreThatLosesTheQueueRow(SqliteSchedulerStore())
        executor = _executor(
            conn,
            lifecycle=_LifecycleSaysYes(on_requeue=_pulled_up),
            store=store,
        )

        result = executor.resume("p1")

        assert result.outcome == "applied", (
            f"a plan the engine pulled up was not applied: {result!r}"
        )
        assert result.detail == "resumed", (
            f"the applied resume does not say what it did: {result!r}"
        )
        # It really is working — that is why it is applied.
        assert "p1" not in _queue_order(conn), (
            f"the plan is still in the queue: {_queue_order(conn)!r}"
        )
        row = conn.execute(
            "SELECT exec_status FROM plan_execution WHERE plan_id = ?",
            ("p1",),
        ).fetchone()
        assert row is not None and str(row[0]) == "running", (
            f"the plan is not working: {row!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. A plan that is not there is still ``not_found``
# ---------------------------------------------------------------------------


def test_missing_phase_keeps_not_found(db_factory: Any) -> None:
    """A plan with no phase is answered ``not_found``, before and after.

    Two moments, one answer.  The plan that was never there is refused
    at the door — the branch that was already there, unchanged.  The
    plan that vanished **while** the resume was running is refused by
    the postcondition, for the same reason and with the same word: the
    scheduler does not know this plan, so there is nothing to resume.

    The wording of the refusal is task 6's business; what is pinned
    here is that the branch is still the not-found one.
    """
    db = db_factory()
    conn = db.connect()
    try:
        # A plan nobody ever wrote: the branch that was already there.
        executor = _executor(conn, lifecycle=_LifecycleSaysYes())

        missing = executor.resume("no-such-plan")

        assert missing.outcome == "not_found", (
            f"a plan that is not there was not refused: {missing!r}"
        )

        # A plan that was there and is not any more: the postcondition
        # reads the phase and finds no plan.
        _make_plan("p1", "stopped")

        def _vanished(plan_id: str) -> None:
            conn.execute(
                "DELETE FROM plan_routing WHERE plan_id = ?", (plan_id,)
            )

        executor = _executor(
            conn, lifecycle=_LifecycleSaysYes(on_requeue=_vanished),
        )

        vanished = executor.resume("p1")

        assert vanished.outcome == "not_found", (
            f"a plan that vanished was not refused: {vanished!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5. The happy path is still applied
# ---------------------------------------------------------------------------


def test_happy_path_still_applied(db_factory: Any) -> None:
    """A stopped plan that is really resumed is applied.

    The real facade and the real store: ``stopped → queued`` moves the
    phase through the state machine, the queue row is written, and the
    plan waits at the head of the line.  This is the case the
    postcondition must not break.
    """
    db = db_factory()
    conn = db.connect()
    try:
        _make_plan("p1", "stopped")
        executor = _executor(conn)

        result = executor.resume("p1")

        assert result.outcome == "applied", (
            f"the resume did not go through: {result!r}"
        )
        assert result.detail == "resumed", (
            f"the applied resume does not say what it did: {result!r}"
        )
        assert _phase_in_db(conn, "p1") == "queued", (
            "the resumed plan is not waiting: {0!r}".format(
                _phase_in_db(conn, "p1")
            )
        )
        assert _queue_order(conn) == ["p1"], (
            f"the resumed plan is not in the queue: {_queue_order(conn)!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. The negative control
# ---------------------------------------------------------------------------


def test_failed_plan_never_applied_without_queue(db_factory: Any) -> None:
    """A plan with failed tasks is never answered "applied" and lost.

    This is the control on the whole file: a plan whose task failed is
    the shape the defect was reported against, and the answer it gets
    has to be backed by a fact.

    * an ``applied`` answer has to be backed by the plan really being
      in the queue, or really working;
    * an answer that is not ``applied`` has to be an **explicit**
      refusal — never a quiet success and never an exception the caller
      cannot read.
    """
    db = db_factory()
    conn = db.connect()
    try:
        plan_id = "p-failed"
        plan_dir = _make_plan(plan_id, "failed")
        _write_tasks(plan_dir, ["failed", "pending"])

        executor = _executor(conn)
        result = executor.resume(plan_id)

        if result.outcome == "applied":
            # Applied means the plan really came back: it waits in the
            # queue, or it is already working.
            assert (
                plan_id in _queue_order(conn)
                or _is_working(conn, plan_id)
            ), (
                "the resume was applied but the plan is nowhere: "
                f"queue={_queue_order(conn)!r} result={result!r}"
            )
        else:
            # Not applied means the refusal is explicit: a word the
            # caller can read, not a silence.
            assert result.outcome in ("conflict", "not_found"), (
                f"the resume failed in a way the caller cannot read: {result!r}"
            )
            assert result.detail, (
                f"the refusal does not say why: {result!r}"
            )
    finally:
        conn.close()


def _is_working(conn: sqlite3.Connection, plan_id: str) -> bool:
    """Whether the plan holds compute right now."""
    from plan_scheduler.facts import PlanFactsReader

    facts = PlanFactsReader(conn, _plans_root())
    return any(
        str(row.get("plan_id")) == plan_id for row in facts.active_plans()
    )
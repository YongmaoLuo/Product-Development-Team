"""Integration tests: the answer hook — an answer puts the plan back to work.

Background
----------
A human answers a question the plan is blocked on, and the answer is
written down.  That is the end of the *decision* and the beginning of
the *schedule*: the plan has to get back to work.

There are two cases, and one fact decides between them — **is the
plan's execution process still alive?**

* **It is alive.**  The process was blocked waiting for the answer, and
  the answer woke it up.  It is working, it holds its slot, and there
  is nothing to put back in the queue: a resume here would requeue a
  plan that is running, and a start would run it a second time.  The
  hook therefore does nothing, and says so — ``already_running``.
* **It is not alive.**  The process died while the question was open —
  that is what ``kill -9`` does to a plan that waits for a human.  The
  plan is parked, nobody is working on it, and the answer is the signal
  that somebody is ready for it again: the plan goes back to the head of
  the queue (``stopped → queued`` and a promotion), so it is the next
  one to run.

The hook belongs to the **answer path**, not to the answer.  A question
that is answered is answered whatever the schedule does with it
afterwards, so the two ways the hook can fail are both non-events for
the caller:

* there is no scheduler — it is not enabled, or it is not started: the
  answer still succeeds and the skipped scheduling action is written
  down in the log;
* the submission itself raises: the answer still succeeds.  A 500 here
  would invite a retry, and a retry is a second answer.

What is pinned here
-------------------
1. ``test_answer_submits_resume`` — the answer submits one ``resume``
   command, and a parked plan comes back to the head of the queue.
2. ``test_live_process_no_reschedule`` — a plan that is still working is
   not requeued, not promoted and not started again.
3. ``test_engine_absent_answer_still_ok`` — no scheduler: the answer is
   still a 200, over HTTP and in the service, and the skip is logged.
4. ``test_answer_failure_sends_nothing`` — an answer that was refused
   submits nothing at all.

Plus the failure the four above share: a scheduler that raises must not
turn an answer into a 500 (``test_engine_failure_keeps_the_answer``).

The real engine is used
-----------------------
The first two cases drive the **production** engine — the store, the
ledger, the facts reader, the command executor and the real lifecycle
facade — on a real SQLite file with the production schema.  A double
would answer the question "was the hook called?" but not the question
that matters: "did the plan come back to the head of the line?".  Only
the real thing can answer that one.

The engine's tick is left running, because it is part of the world the
hook acts in: a tick that reconciles the queue must not undo what the
resume did.  The sink is the dry-run exit — it writes the tick's
decisions down and starts nothing — so a case can watch what the
scheduler decided without the world changing under it.

Test command::

    cd backend && ./.venv/bin/python3 -m pytest \
        tests/integration/plan_scheduler/test_hitl_resume_hook.py -v
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from hitl.service import answer_decision
from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import CommandExecutor
from plan_scheduler.config import SchedulerConfig
from plan_scheduler.engine import PlanSchedulerEngine
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore


#: The moment every case starts at.
BASE_EPOCH = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc).timestamp()

#: How long the caller waits for the command's answer.  The command is
#: carried out by the worker thread, and the wait is bounded — the
#: bound is generous here because the case is about what the command
#: did, not about how long it took.
COMMAND_TIMEOUT = 10.0

#: The tick's period.  Short, so that the reconciliation the tick does
#: has happened by the time the case reads the world.
TICK_SECONDS = 0.02


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock the case moves by hand.

    It is callable, because that is what the ledger, the facts reader
    and the executor ask for: ``clock()`` is "what time is it now".
    """

    def __init__(self, now: float = BASE_EPOCH) -> None:
        self._now = float(now)

    def __call__(self) -> float:
        """Return the current moment, in epoch seconds."""
        return self._now


class StaticConfigLoader:
    """A configuration loader that always answers the same snapshot."""

    def __init__(self, **overrides: Any) -> None:
        self.config = SchedulerConfig(**overrides)

    def current(self) -> SchedulerConfig:
        """Return the snapshot."""
        return self.config


class RecordingSink:
    """A sink that writes down every decision it is handed.

    The dry-run exit: it applies nothing and starts no plan, so a case
    can watch what the tick decided without the world changing.
    """

    def __init__(self) -> None:
        self.batches: List[List[Dict[str, Any]]] = []

    def apply(self, decisions: Any) -> List[Dict[str, Any]]:
        """Remember the decisions and hand back what they produced."""
        batch = [dict(decision) for decision in decisions]
        self.batches.append(batch)
        return batch

    def actions_for(self, plan_id: str) -> List[str]:
        """The actions the tick decided for one plan, in order."""
        return [
            str(decision.get("action"))
            for batch in self.batches
            for decision in batch
            if str(decision.get("plan_id")) == plan_id
        ]


class RecordingEngine:
    """An engine that only remembers what it was asked to do.

    It is the right double for the cases where the question is "did the
    hook ask the scheduler anything?" — a refused answer must not even
    get that far.
    """

    def __init__(self, outcome: str = "applied", detail: str = "resumed") -> None:
        self.calls: List[Any] = []
        self.outcome = outcome
        self.detail = detail

    def submit_command(self, action: str, plan_id: str, **kwargs: Any) -> Any:
        """Write the call down and answer like the engine would."""
        self.calls.append((action, plan_id))
        from plan_scheduler.commands import CommandResult

        return CommandResult(
            outcome=self.outcome,
            detail=self.detail,
            plan_id=plan_id,
            action=action,
        )


class ExplodingEngine:
    """An engine whose submission raises."""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def submit_command(self, action: str, plan_id: str, **kwargs: Any) -> Any:
        """Write the call down and fail."""
        self.calls.append((action, plan_id))
        raise RuntimeError("the scheduler could not be reached")


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    """Return the moment the way the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _plans_root() -> Path:
    """The plans tree the facts reader and the lifecycle read.

    The suite redirects ``server.PLANS_DIR`` at ``tmp_path`` — the
    facade resolves a plan through ``server._plan_dir``, so the case has
    to write where the facade looks.
    """
    import server

    return Path(server.PLANS_DIR)


def _open_db(path: Path) -> sqlite3.Connection:
    """Open a connection to the case's database with the production contract."""
    from state_machine.db.connection import open as open_db

    return open_db(path)


def _read_rows(db_path: Path, sql: str) -> List[Dict[str, Any]]:
    """Read rows through a connection of the case's own.

    A connection of its own, so that reading never joins the worker's
    transaction and never changes the row factory the production code
    expects.
    """
    conn = _open_db(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _make_plan(plan_id: str, phase: str) -> Path:
    """Create a plan whose phase is ``phase``.

    The phase is written as the legacy ``plan_state.json`` snapshot and
    then read once, which migrates it into ``plan_routing`` — the same
    path a plan that was bootstrapped by the API takes.  The migration
    is not optional here: the scheduler reads the phase from
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


def _write_execution_row(
    conn: sqlite3.Connection,
    plan_id: str,
    exec_pid: Optional[int] = None,
    exec_status: Optional[str] = None,
    project_dir: Optional[str] = None,
) -> None:
    """Write the plan's execution fact: the subprocess and the directory."""
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, project_dir, next_run_at, exec_pid, exec_status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (plan_id, project_dir, None, exec_pid, exec_status, _iso(BASE_EPOCH)),
    )


def _live_pid(case_registry: Any) -> int:
    """The pid of a process that is running right now."""
    process = case_registry.spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    return int(process.pid)


def _pending_decision(plan_id: str, decision_id: str, status: str = "pending") -> str:
    """Write a decision of the case's plan straight into the store.

    The decision is written where the answer path will find it, and the
    plan's directory exists, so the audit mirror has somewhere to go.
    """
    from config_paths import resolve_state_db_path
    from hitl.models import Decision, DecisionRequest
    from hitl.store import HitlStore

    request = DecisionRequest(
        plan_id=plan_id,
        source="test",
        question="Ship the change as written?",
        options=["accept", "revise"],
        default_option="accept",
    )
    decision = Decision.from_request(request, decision_id=decision_id)
    decision.status = status
    with HitlStore(resolve_state_db_path()) as store:
        store.insert(decision)
    return decision_id


def _decision_status(decision_id: str) -> Optional[str]:
    """The status the stored decision carries."""
    from config_paths import resolve_state_db_path
    from hitl.store import HitlStore

    with HitlStore(resolve_state_db_path()) as store:
        decision = store.get(decision_id)
    return None if decision is None else str(decision.status)


def _command_rows(db_path: Path) -> List[Dict[str, Any]]:
    """Every command the scheduler was asked for, oldest first."""
    return _read_rows(
        db_path,
        "SELECT command_id, plan_id, action, outcome, detail "
        "FROM scheduler_command ORDER BY command_id",
    )


def _queue_rows(db_path: Path) -> List[Dict[str, Any]]:
    """The queue, in the order the production comparator gives."""
    conn = _open_db(db_path)
    try:
        return [dict(row) for row in SqliteSchedulerStore().list_queue(conn)]
    finally:
        conn.close()


def _phase_of(db_path: Path, plan_id: str) -> Optional[str]:
    """The phase the plan carries in the routing table."""
    conn = _open_db(db_path)
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else str(row[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


class World:
    """What a case drives: the engine and the world around it.

    Attributes:
        engine: the engine under test.
        sink: the sink the tick's decisions are written to.
        db_path: the case's database file.
        conn: the engine's connection.
    """

    def __init__(
        self,
        engine: Any,
        sink: RecordingSink,
        db_path: Path,
        conn: sqlite3.Connection,
    ) -> None:
        self.engine = engine
        self.sink = sink
        self.db_path = db_path
        self.conn = conn

    def close(self) -> None:
        """Stop the engine and give the connection back."""
        try:
            self.engine.stop()
        finally:
            try:
                self.conn.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass


def _world(db_factory: Any, plan_factory: Any) -> World:
    """Build the world a case drives.

    The collaborators are the production ones: the store, the capacity
    ledger, the facts reader, the command executor and the real
    lifecycle facade.  Only the clock, the sink and the capacity are the
    case's own.
    """
    from plan_lifecycle_service import PlanLifecycleService

    database = db_factory()
    plan_factory(plan_id="p1")

    conn = _open_db(database.path)
    clock = FakeClock()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, _plans_root(), clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    commands = CommandExecutor(
        store,
        ledger,
        PlanLifecycleService(),
        facts=facts,
        conn=conn,
        capacity=2,
        min_runtime_seconds=0.0,
        clock=clock,
    )
    sink = RecordingSink()
    engine = PlanSchedulerEngine(
        config_loader=StaticConfigLoader(
            capacity=2,
            min_runtime_seconds=0.0,
            tick_seconds=TICK_SECONDS,
            command_timeout_seconds=COMMAND_TIMEOUT,
        ),
        store=store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=sink,
        clock=clock,
    )
    return World(engine, sink, Path(database.path), conn)


# ---------------------------------------------------------------------------
# 1. An answer puts a parked plan back at the head of the queue
# ---------------------------------------------------------------------------


def test_answer_submits_resume(
    db_factory: Any, plan_factory: Any
) -> None:
    """Answering a parked plan's question submits one ``resume``.

    The plan's process is gone — it died while the question was open —
    so the plan is parked in ``stopped`` and nobody is working on it.
    The answer is the signal that somebody is ready for it again: the
    hook submits a ``resume``, and the plan comes back **waiting** and
    **first** — ``stopped → queued``, a promotion, and the head of the
    line.
    """
    world = _world(db_factory, plan_factory)
    try:
        _make_plan("p1", "stopped")
        _make_plan("p2", "queued")
        decision_id = _pending_decision("p1", "d1")

        world.engine.start()
        status, code = answer_decision(
            decision_id,
            "accept",
            "alice",
            "",
            plan_id="p1",
            engine=world.engine,
        )

        assert (status, code) == ("answered", 200), (
            f"the answer was not taken: {(status, code)!r}"
        )
        assert _decision_status(decision_id) == "answered"

        # Exactly one resume command, and it is about this plan.
        resumes = [
            row for row in _command_rows(world.db_path)
            if row["action"] == "resume"
        ]
        assert len(resumes) == 1, (
            f"the answer submitted {len(resumes)} resume command(s): "
            f"{resumes!r}"
        )
        assert resumes[0]["plan_id"] == "p1"
        assert "applied" in (resumes[0]["detail"] or ""), (
            "the resume did not go through: {0!r}".format(resumes[0])
        )

        # The plan is waiting again.
        assert _phase_of(world.db_path, "p1") == "queued", (
            "the plan is not waiting: {0!r}".format(
                _phase_of(world.db_path, "p1")
            )
        )

        # And it is first in the line: the promotion is the smallest one.
        queue = _queue_rows(world.db_path)
        assert queue, "the resumed plan is not in the queue"
        assert queue[0]["plan_id"] == "p1", (
            "the resumed plan is not at the head: {0!r}".format(queue)
        )
        promoted = [
            row["promote_order"] for row in queue
            if row["promote_order"] is not None
        ]
        assert queue[0]["promote_order"] == min(promoted), (
            "the resumed plan does not carry the smallest promotion: "
            "{0!r}".format(queue)
        )
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 2. A plan that is still working is not rescheduled
# ---------------------------------------------------------------------------


def test_live_process_no_reschedule(
    db_factory: Any, plan_factory: Any, case_registry: Any, caplog: Any
) -> None:
    """A plan whose process is still alive is not requeued.

    The process was blocked waiting for the answer and the answer woke
    it up: it is working, and it holds its slot.  Putting it back in
    the queue would be a lie — it is not waiting — and starting it again
    would run it twice.  So the hook asks, the engine says the plan is
    running, and **nothing is rescheduled**: the phase does not move,
    the plan does not join the line, and nothing is started.
    """
    world = _world(db_factory, plan_factory)
    try:
        _make_plan("p1", "executing")
        alive = _live_pid(case_registry)
        _write_execution_row(
            world.conn,
            "p1",
            exec_pid=alive,
            exec_status="running",
            project_dir=str(_plans_root() / "p1"),
        )
        decision_id = _pending_decision("p1", "d1")

        world.engine.start()
        with caplog.at_level(logging.INFO, logger="hitl.service"):
            status, code = answer_decision(
                decision_id,
                "accept",
                "alice",
                "",
                plan_id="p1",
                engine=world.engine,
            )

        assert (status, code) == ("answered", 200), (
            f"the answer was not taken: {(status, code)!r}"
        )

        # The resume was submitted — and the engine said the plan is
        # working, so the command did nothing.
        resumes = [
            row for row in _command_rows(world.db_path)
            if row["action"] == "resume"
        ]
        assert len(resumes) == 1, (
            f"the answer submitted {len(resumes)} resume command(s): "
            f"{resumes!r}"
        )
        assert "running" in (resumes[0]["detail"] or ""), (
            "the resume did not find the plan working: {0!r}".format(
                resumes[0]
            )
        )

        # Zero requeue: the phase did not move and the plan did not
        # join the line.
        assert _phase_of(world.db_path, "p1") == "executing", (
            "the plan was moved: {0!r}".format(_phase_of(world.db_path, "p1"))
        )
        assert "p1" not in [row["plan_id"] for row in _queue_rows(world.db_path)], (
            "the working plan was put back in the queue: {0!r}".format(
                _queue_rows(world.db_path)
            )
        )

        # And zero start: the tick never decided to admit it again.
        assert "admit" not in world.sink.actions_for("p1"), (
            "the plan was started again: {0!r}".format(
                world.sink.actions_for("p1")
            )
        )

        # The case says so out loud — ``already_running``.
        assert "already_running" in caplog.text, (
            "the hook did not say the plan was already running: {0!r}".format(
                caplog.text
            )
        )
    finally:
        world.close()


# ---------------------------------------------------------------------------
# 3. No scheduler: the answer is still an answer
# ---------------------------------------------------------------------------


def test_engine_absent_answer_still_ok(
    db_factory: Any, plan_factory: Any, caplog: Any, monkeypatch: Any
) -> None:
    """A scheduler that is not running does not make the answer fail.

    The scheduler can be switched off (``enabled = false``) or simply
    not started.  Then there is nobody to submit a ``resume`` to — and
    the answer is still an answer: it is taken, it is stored, and the
    HTTP answer is a 200.  The skipped scheduling action is written
    down, so an operator can tell "nobody was scheduled" from "the
    answer never arrived".
    """
    from fastapi.testclient import TestClient

    import server

    db = db_factory()
    _make_plan("p1", "stopped")
    over_http = _pending_decision("p1", "d-http")
    in_service = _pending_decision("p1", "d-service")

    # No engine on the app at all: the scheduler is not running.
    monkeypatch.setattr(
        server.app.state, "scheduler_engine", None, raising=False
    )
    client = TestClient(server.app)

    with caplog.at_level(logging.INFO, logger="hitl.service"):
        response = client.post(
            f"/api/hitl/p1/decisions/{over_http}/answer",
            json={"option": "accept", "answered_by": "alice", "note": ""},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "answered", response.text
    assert _decision_status(over_http) == "answered"

    # The same through the service: no engine, no scheduling action, and
    # the answer is still taken.
    with caplog.at_level(logging.INFO, logger="hitl.service"):
        status, code = answer_decision(
            in_service,
            "accept",
            "alice",
            "",
            plan_id="p1",
        )

    assert (status, code) == ("answered", 200), (
        f"the answer was not taken: {(status, code)!r}"
        )
    assert _decision_status(in_service) == "answered"

    # Nothing was scheduled, and the skip was written down.
    assert _command_rows(Path(db.path)) == [], (
        "a command was submitted without a scheduler: {0!r}".format(
            _command_rows(Path(db.path))
        )
    )
    assert "no_engine" in caplog.text, (
        "the skipped scheduling action was not written down: {0!r}".format(
            caplog.text
        )
    )


# ---------------------------------------------------------------------------
# 4. A refused answer submits nothing
# ---------------------------------------------------------------------------


def test_answer_failure_sends_nothing(
    db_factory: Any, plan_factory: Any
) -> None:
    """An answer that was refused never reaches the scheduler.

    A decision that does not exist is not found, and a decision that is
    already settled is a conflict.  Neither is an answer, so neither may
    start a plan: the hook is only ever reached by an answer that was
    actually taken.
    """
    db = db_factory()
    _make_plan("p1", "stopped")
    engine = RecordingEngine()

    missing = answer_decision(
        "d-does-not-exist",
        "accept",
        "alice",
        "",
        plan_id="p1",
        engine=engine,
    )
    assert missing == ("not_found", 404), (
        f"answering an unknown decision was not a 404: {missing!r}"
    )

    settled = _pending_decision("p1", "d-settled", status="answered")
    conflict = answer_decision(
        settled,
        "accept",
        "alice",
        "",
        plan_id="p1",
        engine=engine,
    )
    assert conflict == ("answered", 409), (
        f"answering a settled decision was not a 409: {conflict!r}"
    )

    assert engine.calls == [], (
        "a refused answer reached the scheduler: {0!r}".format(engine.calls)
    )
    assert _command_rows(Path(db.path)) == [], (
        "a refused answer submitted a command: {0!r}".format(
            _command_rows(Path(db.path))
        )
    )


# ---------------------------------------------------------------------------
# 5. A scheduler that raises does not turn the answer into a 500
# ---------------------------------------------------------------------------


def test_engine_failure_keeps_the_answer(
    db_factory: Any, plan_factory: Any, monkeypatch: Any
) -> None:
    """A submission that raises is logged, and the answer still stands.

    A 500 here would invite a retry, and a retry is a second answer —
    which the conflict rule would then refuse.  The caller must get the
    answer it earned.
    """
    from fastapi.testclient import TestClient

    import server

    db = db_factory()
    _make_plan("p1", "stopped")
    decision_id = _pending_decision("p1", "d1")

    exploding = ExplodingEngine()
    status, code = answer_decision(
        decision_id,
        "accept",
        "alice",
        "",
        plan_id="p1",
        engine=exploding,
    )
    assert (status, code) == ("answered", 200), (
        f"the answer was not taken: {(status, code)!r}"
    )
    assert exploding.calls == [("resume", "p1")]

    # And over HTTP: the answer is a 200 even though the scheduler is
    # unreachable.
    monkeypatch.setattr(
        server.app.state, "scheduler_engine", exploding, raising=False
    )
    client = TestClient(server.app)
    over_http = _pending_decision("p1", "d-http")
    response = client.post(
        f"/api/hitl/p1/decisions/{over_http}/answer",
        json={"option": "accept", "answered_by": "alice", "note": ""},
    )
    assert response.status_code == 200, response.text
    assert _decision_status(over_http) == "answered"

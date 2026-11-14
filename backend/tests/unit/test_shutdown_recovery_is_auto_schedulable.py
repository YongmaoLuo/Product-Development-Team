"""A plan the shutdown interrupted is a plan the scheduler picks up again.

Background
----------
The scheduler does not decide what to run from the execution side.  It
reads *plan facts* — ``plan_scheduler.facts.PlanFactsReader`` — and the
fact that matters for "may this plan start now?" is the plan's own
phase: ``queued_plans()`` returns the plans whose ``plan_routing``
phase is ``queued``, and the scheduler enqueues exactly those.  Every
other phase — ``executing``, ``failed``, ``stopped`` — is somebody
else's business.

So "the server went down and the work was interrupted" is only a
recovery if it lands the plan in a phase somebody reads.  The recovery
scan (``routes.execution._recover_execution_states``) parks such a plan
in ``queued`` (``routes.execution._park_shutdown_interrupted``), and
this file is the pin that says so — from the *consumer's* side.

Why the consumer's side and not the writer's
--------------------------------------------
The writer already has its own tests
(``backend/tests/api/test_shutdown_recovery_parks_queued.py``): they
assert the phase is ``queued``.  A phase assertion is a promise about
the database; what the scheduler needs is a promise about
*behaviour* — "this plan is in the queue now".  A future change could
keep the phase at ``queued`` and still empty the queue (a reader that
filters something else, a join that stops matching, a queue table
that stops being consulted).  Only the consumer's own question
(``queued_plans()``) rules that out.

The three worlds these tests set up
----------------------------------
* ``p-shutdown`` — the server went down under a live run.  The
  checkpoint says ``server_shutdown`` and it is fresh, so the run was
  interrupted rather than broken, and the plan comes back as
  ``queued``.
* ``p-crash`` — the same dead executor, but no checkpoint: nothing
  proves the server went down, so the run is parked as ``stopped``
  (the startup reconcile's verdict) and a parked plan is nobody's
  business.
* ``p-stopped`` — somebody pressed stop.  That is a third thing
  again: the run was cancelled on purpose, its phase is ``stopped``,
  and neither the recovery scan nor the scheduler may put it back in
  line.

The stamps here are *computed* rather than hard-coded: the
checkpoint's ``paused_at`` is an aware UTC stamp and the row's
``started_at`` a naive local one, exactly as the two writers spell
them, so a test means the same instant whatever timezone the machine
running it is in.

Each test owns its ``tmp_path`` and its own ``state.db`` — the shared
database is never touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import pytest
from pause_state import PAUSE_CHECKPOINT_FILENAME, write_pause_checkpoint
from plan_scheduler.facts import PlanFactsReader
from plan_state import PlanState
from server import _execution_state, _recover_execution_states

# ``server`` must be imported first: it seeds ``sys.modules['server']``
# and then wires the ``routes`` package, and ``routes.execution``
# late-binds back into it.  So the import below comes *after* the one
# above on purpose -- do not sort these two.
from routes import execution as routes_execution
from shutdown_checkpoint import SHUTDOWN_STOP_REASON
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository


#: A pid that cannot belong to a live process.  The recovery scan probes
#: it with ``os.kill(pid, 0)`` and reads the ``ProcessLookupError`` as
#: "the executor is gone".
_DEAD_PID = 999999

#: The reason a *person* stopping a run writes into the checkpoint.
#: The codebase has no constant for it — the stop path writes the
#: literal (``routes/execution.py``) — so the test names it once here
#: instead of scattering the word around.
_USER_STOP_REASON = "user_requested"


# ----------------------------------------------------------------------
# The test's own world
# ----------------------------------------------------------------------


def _state_db(tmp_path, monkeypatch):
    """This test's own state database.

    The suite's hermetic fixture already points ``PDT_STATE_DB_PATH``
    *and* ``server._state_db_path`` at ``tmp_path / "state.db"``.  The
    two have to name the same file: a split would hand the recovery
    scan and the scheduler reader two different worlds, and the plan
    would be "queued" in one of them and invisible in the other.  So
    the test uses that path and says so out loud rather than inventing
    a second one.
    """
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    return db_path


def _seed_plan(sample_plan_factory, db_path, plan_id, phase):
    """A plan directory plus the routing row the scheduler reads.

    The routing row is written explicitly because a plan that is not
    in ``plan_routing`` at all is trivially absent from the queue —
    that would make every negative control below vacuous.  These plans
    are all *in* the scheduler's world; they differ in the phase they
    stand in.
    """
    plan_dir = sample_plan_factory(plan_id=plan_id, phase=phase)
    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, phase)
    finally:
        conn.close()
    return plan_dir


def _seed_execution(db_path, plan_id, **fields):
    """Write one ``plan_execution`` row — the run state the scan reads."""
    conn = open_db(db_path)
    try:
        migrate(conn)
        ExecutionRepository(conn).update_fields(
            plan_id, create_if_missing=True, **fields
        )
    finally:
        conn.close()


def _write_checkpoint(plan_dir, *, kind, reason, paused_at):
    """Write a checkpoint whose ``paused_at`` the test controls.

    ``write_pause_checkpoint`` stamps ``paused_at`` itself, and the
    freshness judgement is exactly about that field — so the helper
    goes through the public writer (which owns the schema version) and
    then rewrites the one field the test needs to pin.
    """
    document = dict(
        write_pause_checkpoint(plan_dir, kind=kind, reason=reason)
    )
    document["paused_at"] = paused_at
    (plan_dir / PAUSE_CHECKPOINT_FILENAME).write_text(
        json.dumps(document), encoding="utf-8"
    )
    return document


def _naive_local(moment):
    """``moment`` (aware) spelled the way the execution row spells it.

    ``routes.execution`` writes ``started_at`` as ``datetime.now()
    .isoformat()`` — a naive *local* timestamp.  The helper makes the
    same conversion, so a test can hand the predicate a naive stamp
    that denotes a known instant.
    """
    return moment.astimezone().replace(tzinfo=None).isoformat()


# ----------------------------------------------------------------------
# The three worlds
# ----------------------------------------------------------------------


def _interrupted_by_shutdown(sample_plan_factory, db_path, plan_id="p-shutdown"):
    """A plan whose executor the server going down took with it.

    The seed is the real one: a row that still says ``running`` with a
    pid that is gone, and a *fresh* shutdown checkpoint beside it.
    """
    plan_dir = _seed_plan(sample_plan_factory, db_path, plan_id, "executing")
    paused = datetime.now(timezone.utc)
    _seed_execution(
        db_path,
        plan_id,
        exec_status="running",
        exec_pid=_DEAD_PID,
        started_at=_naive_local(paused - timedelta(minutes=1)),
        project_dir=str(plan_dir / "project"),
    )
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason=SHUTDOWN_STOP_REASON,
        paused_at=paused.isoformat(),
    )
    return plan_dir


def _crashed(sample_plan_factory, db_path, plan_id="p-crash"):
    """The same dead executor, but no checkpoint: the work died.

    Without a checkpoint there is no evidence that the *server* went
    away — only that the executor is gone — so this run is parked as
    ``stopped`` rather than queued for another attempt.
    """
    plan_dir = _seed_plan(sample_plan_factory, db_path, plan_id, "executing")
    _seed_execution(
        db_path,
        plan_id,
        exec_status="running",
        exec_pid=_DEAD_PID,
        started_at=_naive_local(datetime.now(timezone.utc)),
        project_dir=str(plan_dir / "project"),
    )
    return plan_dir


def _stopped_by_the_user(sample_plan_factory, db_path, plan_id="p-stopped"):
    """Somebody pressed stop: the run was cancelled on purpose.

    The row says ``stopped``, the checkpoint says ``user_requested``
    and the plan stands in ``stopped``.  There is no dead executor here
    to judge: the recovery scan has nothing to say about this plan, and
    what it must not do is put a cancelled run back in line.
    """
    plan_dir = _seed_plan(sample_plan_factory, db_path, plan_id, "stopped")
    _seed_execution(
        db_path,
        plan_id,
        exec_status="stopped",
        stop_reason=_USER_STOP_REASON,
        project_dir=str(plan_dir / "project"),
    )
    _write_checkpoint(
        plan_dir,
        kind="execution",
        reason=_USER_STOP_REASON,
        paused_at=datetime.now(timezone.utc).isoformat(),
    )
    return plan_dir


# ----------------------------------------------------------------------
# What the scheduler sees
# ----------------------------------------------------------------------


def _phase_of(db_path, plan_id):
    """The phase the scheduler reads for this plan."""
    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row is not None else None


def _queued_plan_ids(db_path, plans_root):
    """The plan ids the scheduler's own reader calls waiting."""
    conn = open_db(db_path)
    try:
        migrate(conn)
        reader = PlanFactsReader(conn, plans_root)
        return [entry["plan_id"] for entry in reader.queued_plans()]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# The pins
# ----------------------------------------------------------------------


def test_a_shutdown_interrupted_plan_is_in_the_queue(
    tmp_path, monkeypatch, sample_plan_factory
):
    """The plan the shutdown interrupted is one the scheduler will run again.

    All three plans live in the same tree, because the question is not
    "is p-shutdown queued?" but "is *only* p-shutdown queued?" — the
    crash and the user stop must not ride along on the same phase
    change.
    """
    db_path = _state_db(tmp_path, monkeypatch)
    plans_root = tmp_path / "plans"

    _interrupted_by_shutdown(sample_plan_factory, db_path)
    _crashed(sample_plan_factory, db_path)
    _stopped_by_the_user(sample_plan_factory, db_path)

    _execution_state.clear()
    _recover_execution_states(plans_root)

    assert _phase_of(db_path, "p-shutdown") == "queued", (
        "the interrupted plan waits in the only phase the scheduler reads"
    )
    assert _queued_plan_ids(db_path, plans_root) == ["p-shutdown"], (
        "the scheduler sees the interrupted plan, and only it"
    )


def test_a_crash_is_not_in_the_queue(
    tmp_path, monkeypatch, sample_plan_factory
):
    """Negative control: a run that died is nobody's business."""
    db_path = _state_db(tmp_path, monkeypatch)
    plans_root = tmp_path / "plans"

    _crashed(sample_plan_factory, db_path)

    _execution_state.clear()
    _recover_execution_states(plans_root)

    assert _phase_of(db_path, "p-crash") == "stopped", (
        "without a checkpoint there is no evidence the server went down, "
        "so the run is parked — not queued, and not claimed to have "
        "died unexpectedly"
    )
    assert _queued_plan_ids(db_path, plans_root) == [], (
        "a crashed plan is not waiting for anybody"
    )


def test_a_user_stopped_plan_is_not_in_the_queue(
    tmp_path, monkeypatch, sample_plan_factory
):
    """Negative control: somebody pressed stop, and that is not an accident."""
    db_path = _state_db(tmp_path, monkeypatch)
    plans_root = tmp_path / "plans"

    _stopped_by_the_user(sample_plan_factory, db_path)

    _execution_state.clear()
    _recover_execution_states(plans_root)

    assert _phase_of(db_path, "p-stopped") == "stopped", (
        "the recovery scan leaves a plan somebody stopped where it stood"
    )
    assert "p-stopped" not in _queued_plan_ids(db_path, plans_root), (
        "a cancelled run must not be queued for another attempt"
    )


# ----------------------------------------------------------------------
# The boot repair: the phase is moved, the verdict is not judged again
# ----------------------------------------------------------------------


class _PhaseSpy(NamedTuple):
    """What the boot repair did to a plan's phase.

    * ``opened`` -- the plans whose state was opened at all.  A repair
      that has nothing to repair opens nothing;
    * ``reads`` -- the phases the repair read, and in what order;
    * ``writes`` -- the phases the repair wrote.
    """

    opened: list
    reads: list
    writes: list


def _phase_writer_spy(monkeypatch):
    """Record what the boot repair opens, reads and writes.

    ``routes.execution._restore_parked_phase`` repairs a parked plan's
    phase through ``PlanState.force_set_phase`` -- the one writer of a
    plan's phase.  The spy stands in front of that writer, in front of
    the read that precedes it, and in front of the state holder's own
    constructor, so a test can say **when** the repair writes and what
    it writes, and not merely that the phase ended up somewhere.  Every
    call goes through: the repair's own write still lands, and the row
    it writes is the row the test reads back.
    """
    opened = []
    reads = []
    writes = []

    original_open = PlanState.__init__
    original_read = PlanState.get_current_phase
    original_write = PlanState.force_set_phase

    def _open_spy(self, plan_dir):
        opened.append(Path(plan_dir).name)
        return original_open(self, plan_dir)

    def _read_spy(self):
        phase = original_read(self)
        reads.append((self.plan_dir.name, phase))
        return phase

    def _write_spy(self, phase):
        writes.append((self.plan_dir.name, phase))
        return original_write(self, phase)

    monkeypatch.setattr(PlanState, "__init__", _open_spy)
    monkeypatch.setattr(PlanState, "get_current_phase", _read_spy)
    monkeypatch.setattr(PlanState, "force_set_phase", _write_spy)
    return _PhaseSpy(opened, reads, writes)


def _execution_row(db_path, plan_id):
    """The run state row, as the recovery path reads it."""
    conn = open_db(db_path)
    try:
        return ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()


def test_shutdown_restore_pins_phase_repair_for_a_parked_plan(
    tmp_path, monkeypatch, sample_plan_factory
):
    """The phase a dying server failed to write is written by the repair.

    The repair is ``backend/routes/execution.py:2511``'s
    ``_restore_parked_phase`` (the ``startup_reconcile`` path).  A plan
    whose run the server parked under a shutdown but whose phase was
    never moved still wears the phase of a live run -- and the
    scheduler never picks a plan up from there.  So the repair moves the
    phase, and it moves it through the phase writer: the read and the
    write are both pinned here, so a repair that guessed the phase, or
    wrote it by hand, would not pass.
    """
    db_path = _state_db(tmp_path, monkeypatch)
    plan_dir = _seed_plan(
        sample_plan_factory, db_path, "p-parked", "executing"
    )
    spy = _phase_writer_spy(monkeypatch)

    routes_execution._restore_parked_phase("p-parked", plan_dir)

    assert spy.opened == ["p-parked"], "the repair opened the plan"
    assert spy.reads == [("p-parked", "executing")], (
        "the repair decides on the phase the plan actually stands in"
    )
    assert spy.writes == [("p-parked", "queued")], (
        "the repair moves the phase to the one the scheduler reads"
    )
    assert _phase_of(db_path, "p-parked") == "queued", (
        "the parked plan is back in the waiting set"
    )


def test_shutdown_restore_pins_phase_repair_for_a_queued_plan(
    tmp_path, monkeypatch, sample_plan_factory
):
    """A plan that is already queued is not written again.

    The repair writes the phase the plan already stands in.  Writing it
    a second time is not free -- the write moves the plan's completed
    phases along with it -- and it is not needed: the plan is already
    where the scheduler will find it.
    """
    db_path = _state_db(tmp_path, monkeypatch)
    plan_dir = _seed_plan(sample_plan_factory, db_path, "p-queued", "queued")
    spy = _phase_writer_spy(monkeypatch)

    routes_execution._restore_parked_phase("p-queued", plan_dir)

    assert spy.reads == [("p-queued", "queued")], (
        "the repair looked at the phase before deciding to leave it"
    )
    assert spy.writes == [], "a plan that is already queued is not written again"
    assert _phase_of(db_path, "p-queued") == "queued"


@pytest.mark.parametrize(
    "terminal", ["completed", "failed", "stopped", "archived"]
)
def test_shutdown_restore_pins_phase_repair_for_a_terminal_plan(
    tmp_path, monkeypatch, sample_plan_factory, terminal
):
    """A plan that has moved on is not put back in line.

    The terminal phases are the four the repair knows about
    (``server._TERMINAL_PHASES``).  A plan that reached one of them
    after the shutdown was parked has moved on since -- its row is an
    old verdict rather than the current state -- so re-queueing it
    would resurrect work that is already done.
    """
    db_path = _state_db(tmp_path, monkeypatch)
    plan_dir = _seed_plan(sample_plan_factory, db_path, "p-done", terminal)
    spy = _phase_writer_spy(monkeypatch)

    routes_execution._restore_parked_phase("p-done", plan_dir)

    assert spy.reads == [("p-done", terminal)], (
        "the repair looked at the phase before deciding to leave it"
    )
    assert spy.writes == [], (
        f"a plan in {terminal} is not work that is waiting any more"
    )
    assert _phase_of(db_path, "p-done") == terminal


def test_shutdown_restore_pins_phase_repair_without_a_plan_directory(
    tmp_path, monkeypatch, sample_plan_factory
):
    """No plan directory means no phase to repair.

    The repair writes a phase, not a state of its own: with no
    directory there is nothing to read the phase from, and inventing
    one would put a plan in the queue that nobody has ever run.
    """
    db_path = _state_db(tmp_path, monkeypatch)
    _seed_plan(sample_plan_factory, db_path, "p-elsewhere", "executing")
    missing = tmp_path / "plans" / "p-missing"
    spy = _phase_writer_spy(monkeypatch)

    routes_execution._restore_parked_phase("p-missing", missing)

    assert spy.opened == [], (
        "there was no plan to open: the repair did not even look"
    )
    assert spy.reads == [], "there was no phase to read"
    assert spy.writes == [], "there was no phase to write"
    assert _phase_of(db_path, "p-missing") is None, (
        "the repair invented a phase for a plan that has no directory"
    )


def test_shutdown_restore_pins_phase_repair_does_not_judge_the_row(
    tmp_path, monkeypatch, sample_plan_factory
):
    """The repair moves the phase; the row's verdict stands as it is.

    The row says the server went down under the run, and that verdict
    was written once.  The repair is not a second judgement -- it takes
    the verdict as it is and moves the phase to agree with it.  So the
    row must come out of the repair exactly as it went in.
    """
    db_path = _state_db(tmp_path, monkeypatch)
    plan_dir = _seed_plan(
        sample_plan_factory, db_path, "p-parked", "executing"
    )
    _seed_execution(
        db_path,
        "p-parked",
        exec_status="stopped",
        stop_reason=SHUTDOWN_STOP_REASON,
        exec_pid=_DEAD_PID,
        started_at=_naive_local(datetime.now(timezone.utc)),
    )
    spy = _phase_writer_spy(monkeypatch)

    routes_execution._restore_parked_phase("p-parked", plan_dir)

    assert spy.writes == [("p-parked", "queued")], "the phase is moved"
    row = _execution_row(db_path, "p-parked")
    assert row is not None, "the row is gone"
    assert row["exec_status"] == "stopped", (
        "the repair re-judged the run: a verdict is judged once, not twice"
    )
    assert row["stop_reason"] == SHUTDOWN_STOP_REASON, (
        "the repair re-judged the run: a verdict is judged once, not twice"
    )

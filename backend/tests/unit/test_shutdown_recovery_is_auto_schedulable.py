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

from pause_state import PAUSE_CHECKPOINT_FILENAME, write_pause_checkpoint
from plan_scheduler.facts import PlanFactsReader
from server import _execution_state, _recover_execution_states
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
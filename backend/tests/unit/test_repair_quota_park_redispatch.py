"""2026-11-03: a quota-parked repair round must be able to RE-DISPATCH.

Background
----------
A repair round that ran out of provider capacity is PARKED, not failed:
the executor writes ``stop_reason="waiting_for_quota"`` into the top
level of ``tasks.json`` and exits 0, the repair path records the same
word in ``_execution_state``, and the post-repair callback parks the
plan without writing a terminal verdict.

Parking is only half of the contract.  The plan is parked *waiting for
capacity to come back*, and when capacity comes back the repair work
has to be dispatchable again.  Two things make that possible and both
are pinned here:

* the parked phase is an ACCEPTED start source
  (``server._EXECUTION_START_SOURCE_PHASES``) and it has a legal edge
  back into ``executing`` (``plan_state.PHASE_TRANSITIONS``) — a parked
  plan must not be a plan nobody is allowed to start again;
* the repair runner itself can be dispatched again over the same plan:
  the pending rows are still there, the undrained-repair guard was
  cleared rather than bumped, and a second dispatch really spawns a
  second executor.

Contract pinned here
--------------------
* ``waiting_for_quota`` is in ``_EXECUTION_START_SOURCE_PHASES``,
  ``waiting_for_quota → executing`` is a legal transition, and
  ``executing → waiting_for_quota`` is a legal transition (the park
  itself has to be reachable).
* After the park, a second dispatch of the repair runner works: the
  callback fires again with rc=0, the executor was spawned twice, and
  the undrained-repair guard reads 0 — a park CLEARS the counter, it
  never bumps it.
* A second dispatch that still finds pending work parks AGAIN: no
  terminal verdict, no guard bump.
* The park is idempotent: firing the same callback twice writes the
  ``waiting_for_quota`` phase ONCE and writes no terminal verdict.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import plan_state
import server
import verification_loop as vl
from plan_status import QUOTA_WAIT_PHASE


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _mock_subprocess(returncode: int = 0, pid: int = 424242):
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = pid
    proc.wait = MagicMock(return_value=returncode)
    return proc


def _seed_plan(
    tmp_path: Path,
    plan_id: str,
    db_path: Path,
    *,
    disk_tasks: list,
    task_rows: list,
    quota_marker: bool = True,
) -> Path:
    """Write ``tasks.json`` plus the state.db rows the park needs.

    The seed needs BOTH ``plan_routing`` and ``plan_execution``:

    * ``plan_routing`` is the plan's phase row — the repair dispatch
      stamps ``executing`` on it before it spawns the executor.
    * ``plan_execution`` is the row the park writes ``exec_status`` to.
      Without it ``ExecutionRepository.update_status`` raises
      ``PlanNotFoundError``, which the park swallows — the park would
      then live only in memory.
    """
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    payload: dict = {"tasks": disk_tasks}
    if quota_marker:
        payload["stop_reason"] = QUOTA_WAIT_PHASE
        payload["reason_detail"] = json.dumps(
            {
                "providers": ["kimi"],
                "needs_manual_topup": True,
                "waited_sec": 12.0,
                "at": "2026-11-03T00:00:00",
            }
        )
    (plan_dir / "tasks.json").write_text(json.dumps(payload), encoding="utf-8")

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            (plan_id, "executing", "2026-11-03T00:00:00"),
        )
        conn.execute(
            "INSERT INTO plan_execution (plan_id, attempt_count, project_dir, "
            "exec_status, updated_at) VALUES (?, ?, ?, ?, ?)",
            (plan_id, 1, str(tmp_path), "running", "2026-11-03T00:00:00"),
        )
        for row in task_rows:
            conn.execute(
                "INSERT INTO plan_tasks (plan_id, task_id, status, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    plan_id,
                    row["task_id"],
                    row["status"],
                    "2026-11-03T00:00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return plan_dir


def _complete_tasks(plan_id: str, db_path: Path, task_ids) -> None:
    """The runtime overlay says the pending repair work is done.

    This is what "capacity came back" looks like on disk: the repair
    rows that were left ``pending`` by the park now read ``completed``,
    so the next dispatch finds nothing left to explain.
    """
    from state_machine.db.connection import open as open_db

    conn = open_db(db_path)
    try:
        for task_id in task_ids:
            conn.execute(
                "UPDATE plan_tasks SET status = 'completed', updated_at = ? "
                "WHERE plan_id = ? AND task_id = ?",
                ("2026-11-03T01:00:00", plan_id, task_id),
            )
        conn.commit()
    finally:
        conn.close()


def _read_task_statuses(plan_id: str, db_path: Path) -> dict:
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(db_path)
    try:
        return {
            task_id: row.get("status")
            for task_id, row in (PlanTaskRepository(conn).load_all(plan_id) or {}).items()
        }
    finally:
        conn.close()


class _RecordingPlanState:
    """A ``PlanState`` stand-in that RECORDS every phase write.

    ``transition_to`` never raises here, on purpose: the real
    ``PlanState`` rejects a second ``waiting_for_quota`` write (that
    edge is not in the transition table) and would turn the second
    write into a forced one — the count would still be right, but the
    count would be of the wrong thing.  The point of this stand-in is
    to see HOW MANY times the park reached for the phase.

    ``get_current_phase`` is the READ side of the same contract: the
    park's idempotence check reads the PERSISTED phase, so the stand-in
    has to remember what it was told to write.  The real ``PlanState``
    persists a phase and a later instance reads it back; here the
    class-level ``writes`` list plays that role, so a second park over
    an already-parked plan is skipped exactly as it is on disk.  The
    writes list IS the disk here — without this the stand-in would
    claim that nothing was ever persisted, and the second park would
    look like a first park.
    """

    writes: list = []

    def __init__(self, *_a, **_kw):
        pass

    def get_current_phase(self):
        return _RecordingPlanState.writes[-1] if _RecordingPlanState.writes \
            else "executing"

    def transition_to(self, phase):
        _RecordingPlanState.writes.append(phase)

    def force_set_phase(self, phase):
        _RecordingPlanState.writes.append(phase)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state.db"


@pytest.fixture(autouse=True)
def _shared_state_is_restored():
    """Leave the two MODULE-LEVEL maps this file touches as we found them.

    ``server._execution_state`` and ``vl._repair_not_drained_guard``
    are shared by the whole process, so a test that leaves a plan
    behind can decide the outcome of a test that runs later.
    """
    with server._execution_state_lock:
        server._execution_state.clear()
    saved_guard = dict(vl._repair_not_drained_guard)
    yield
    with server._execution_state_lock:
        server._execution_state.clear()
    vl._repair_not_drained_guard.clear()
    vl._repair_not_drained_guard.update(saved_guard)


def _patch_plan_paths(monkeypatch, tmp_path, db_path):
    """Point the repair path at this test's plan directory and database."""
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(
        server, "_state_db_path", lambda *_a, **_kw: Path(db_path),
    )
    monkeypatch.setattr(
        server, "_plan_dir",
        lambda pid: tmp_path / "plans" / pid,
    )


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_parked_phase_is_an_accepted_start_source():
    """A parked plan must be startable again when capacity comes back.

    Parking is a wait, not a verdict.  If the parked phase were not an
    accepted start source, or if it had no legal edge back into
    ``executing``, the plan could be parked forever — the operator
    would have no way to put the work back in motion.
    """
    assert "waiting_for_quota" in server._EXECUTION_START_SOURCE_PHASES, (
        "a plan parked for quota is not an accepted start source — it "
        "cannot be started again when the capacity comes back"
    )
    assert "executing" in plan_state.PHASE_TRANSITIONS["waiting_for_quota"], (
        "there is no legal edge out of waiting_for_quota — a parked plan "
        "cannot go back to executing"
    )
    assert "waiting_for_quota" in plan_state.PHASE_TRANSITIONS["executing"], (
        "there is no legal edge into waiting_for_quota — the park itself "
        "is not reachable"
    )


def test_quota_recovery_redispatches_repair(monkeypatch, tmp_path, db_path):
    """After the park, the repair round can be dispatched again.

    The first round ran out of capacity and parked.  Capacity came
    back, the pending repair row is done, and the runner is dispatched
    over the same plan once more: the callback fires again, a second
    executor is spawned, and the undrained-repair guard reads 0.
    """
    plan_id = "p-repair-redispatch"
    task_id = "repair-r1-01"
    _seed_plan(
        tmp_path,
        plan_id,
        db_path,
        disk_tasks=[{"id": task_id}],
        task_rows=[{"task_id": task_id, "status": "pending"}],
        quota_marker=True,
    )
    _patch_plan_paths(monkeypatch, tmp_path, db_path)

    # ONE spawn mock for BOTH rounds — the assertion that the second
    # round really dispatched a second executor depends on it.
    spawn = MagicMock(
        side_effect=lambda **kw: (_mock_subprocess(), tmp_path / "log"),
    )
    monkeypatch.setattr(server, "_spawn_executor_subprocess", spawn)

    with server._execution_state_lock:
        server._execution_state.clear()

    # --- round 1: every provider ran dry → the round parks ------------
    parked = threading.Event()
    park_callback = vl._make_repair_on_complete(plan_id, tmp_path)

    def _first_cb(rc):
        try:
            park_callback(rc)
        finally:
            parked.set()

    server._run_repair_execution_async(plan_id, tmp_path, on_complete=_first_cb)
    assert parked.wait(timeout=5.0), "the first round's callback never fired"

    with server._execution_state_lock:
        state = dict(server._execution_state[plan_id])
    assert state["status"] == QUOTA_WAIT_PHASE, (
        f"the first round was recorded as {state['status']!r} — a "
        "capacity-starved round is parked, not failed"
    )
    assert state["stop_reason"] == QUOTA_WAIT_PHASE
    assert state["ended_at"] is None, "a parked round is not over"
    assert spawn.call_count == 1

    # The park left the pending row alone — a park is not a verdict on
    # the tasks, so the work is still there to be re-dispatched.
    assert _read_task_statuses(plan_id, db_path) == {task_id: "pending"}

    # --- capacity comes back: the pending work is done ----------------
    _complete_tasks(plan_id, db_path, [task_id])

    # --- round 2: the same runner, dispatched again -------------------
    seen: dict = {}
    second_done = threading.Event()
    server._run_repair_execution_async(
        plan_id,
        tmp_path,
        on_complete=lambda rc: (seen.__setitem__("rc", rc), second_done.set()),
    )
    assert second_done.wait(timeout=5.0), "the second round's callback never fired"

    assert seen["rc"] == 0, (
        f"the re-dispatched round exited {seen['rc']!r} — a round whose "
        "work is done must exit cleanly"
    )
    assert spawn.call_count == 2, (
        f"the executor was spawned {spawn.call_count} time(s) — the "
        "re-dispatch did not actually run a second round"
    )
    assert vl._repair_not_drained_guard.get(plan_id, 0) == 0, (
        "the undrained-repair guard was not cleared by the park"
    )

    with server._execution_state_lock:
        state = dict(server._execution_state[plan_id])
    assert state["status"] == "completed", (
        f"the re-dispatched round was recorded as {state['status']!r}"
    )
    assert state["stop_reason"] is None
    assert state["ended_at"] is not None


def test_wakeup_with_pending_work_parks_again(monkeypatch, tmp_path, db_path):
    """A wake-up that still finds pending work parks AGAIN.

    The capacity marker is still on disk and the repair row is still
    pending, so the re-dispatched round has nothing to show for itself.
    It parks again — no terminal verdict, and the undrained-repair
    guard is cleared rather than bumped (a bump would take it to the
    ``repair_never_drained`` threshold and kill the chain).
    """
    plan_id = "p-repair-redispatch-pending"
    task_id = "repair-r1-01"
    _seed_plan(
        tmp_path,
        plan_id,
        db_path,
        disk_tasks=[{"id": task_id}],
        task_rows=[{"task_id": task_id, "status": "pending"}],
        quota_marker=True,
    )
    _patch_plan_paths(monkeypatch, tmp_path, db_path)

    spawn = MagicMock(
        side_effect=lambda **kw: (_mock_subprocess(), tmp_path / "log"),
    )
    monkeypatch.setattr(server, "_spawn_executor_subprocess", spawn)

    record_terminal = MagicMock()
    monkeypatch.setattr(
        server, "_record_verification_terminal", record_terminal,
    )

    with server._execution_state_lock:
        server._execution_state.clear()

    try:
        for _round in (1, 2):
            # The guard is seeded NON-ZERO on purpose: a park must
            # CLEAR the counter, not merely decline to bump it.
            vl._repair_not_drained_guard[plan_id] = 2

            done = threading.Event()
            callback = vl._make_repair_on_complete(plan_id, tmp_path)

            def _cb(rc, _callback=callback, _done=done):
                try:
                    _callback(rc)
                finally:
                    _done.set()

            server._run_repair_execution_async(
                plan_id, tmp_path, on_complete=_cb,
            )
            assert done.wait(timeout=5.0), "the callback never fired"

            assert vl._repair_not_drained_guard.get(plan_id, 0) == 0, (
                "the park did not clear the undrained-repair guard"
            )
    finally:
        vl._repair_not_drained_guard.pop(plan_id, None)

    assert record_terminal.call_count == 0, (
        "a quota-parked round was killed with a terminal verdict "
        f"({record_terminal.call_args_list!r})"
    )
    assert spawn.call_count == 2

    with server._execution_state_lock:
        state = dict(server._execution_state[plan_id])
    assert state["status"] == QUOTA_WAIT_PHASE, (
        f"the re-dispatched round was recorded as {state['status']!r} — a "
        "round that still has pending work is parked again"
    )
    assert state["ended_at"] is None
    assert _read_task_statuses(plan_id, db_path) == {task_id: "pending"}


def test_repeat_callback_does_not_write_a_second_terminal(monkeypatch, tmp_path, db_path):
    """The same callback firing twice writes the phase ONCE.

    The park is idempotent: the second callback finds the plan already
    parked and does not reach for the phase again, and it writes no
    terminal verdict on either call.
    """
    plan_id = "p-repair-redispatch-repeat"
    _seed_plan(
        tmp_path,
        plan_id,
        db_path,
        disk_tasks=[{"id": "repair-r1-01"}],
        task_rows=[{"task_id": "repair-r1-01", "status": "pending"}],
        quota_marker=True,
    )
    _patch_plan_paths(monkeypatch, tmp_path, db_path)

    # The state the callback reads: the repair round stopped for quota.
    with server._execution_state_lock:
        server._execution_state.clear()
        server._execution_state[plan_id] = {
            "_source": "repair_execution",
            "stop_reason": QUOTA_WAIT_PHASE,
        }

    _RecordingPlanState.writes.clear()
    monkeypatch.setattr(server, "PlanState", _RecordingPlanState)

    record_terminal = MagicMock()
    monkeypatch.setattr(
        server, "_record_verification_terminal", record_terminal,
    )

    try:
        callback = vl._make_repair_on_complete(plan_id, tmp_path)
        callback(0)
        callback(0)
    finally:
        vl._repair_not_drained_guard.pop(plan_id, None)

    assert record_terminal.call_count == 0, (
        "a quota-parked round was killed with a terminal verdict "
        f"({record_terminal.call_args_list!r}) — the plan must be parked, "
        "not reported as broken"
    )
    assert _RecordingPlanState.writes.count(QUOTA_WAIT_PHASE) == 1, (
        "the parked phase was written "
        f"{_RecordingPlanState.writes.count(QUOTA_WAIT_PHASE)} time(s) — "
        f"the park must be idempotent (writes: {_RecordingPlanState.writes!r})"
    )

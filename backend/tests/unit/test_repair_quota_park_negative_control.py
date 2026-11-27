"""2026-11-03: NEGATIVE CONTROL — the non-quota repair path keeps its shape.

Background
----------
The 2026-11-02 quota-park fix taught the repair path a new word:
``waiting_for_quota``.  When the executor exits 0 because every
provider ran out of capacity, the round is now PARKED instead of
FAILED, and the post-repair callback parks the plan instead of writing
a terminal verdict.

Every one of those changes hangs on "the executor left a quota marker
on disk".  The OTHER branch — no marker, work still pending — must keep
behaving exactly as it did before the fix:

* the repair runner records ``status='failed'`` and
  ``stop_reason='repair_batch_not_drained:<n>_pending'`` — the batch
  did not drain, so work was LEFT behind;
* the post-repair callback does NOT park the plan: an undrained batch
  is work left behind, not a verdict about the code, so the callback
  RE-DISPATCHES the repair executor and writes NO terminal at all.
  Only when the bounded guard trips does the chain stop, and then with
  a reason of its own — ``repair_never_drained:<reason>:<n>x``.

This file is that negative control.  It pins the shape of the non-quota
path so that a future change cannot "helpfully" park every undrained
round, rename either reason, or let the FIRST callback trip the
``repair_never_drained`` threshold.

The wording in this file's NAME is historical
---------------------------------------------
``repair_exited_with_unfinished_tasks`` is how the repair path USED to
spell "the batch did not drain".  It now classifies its own ending (see
``verification.repair_outcome.classify_repair_outcome``) and writes
``repair_batch_not_drained:<n>_pending`` instead.  Only the STRING
changed: the behaviour pinned here — the bounded re-dispatch, and the
single terminal that only the bound may write — is the contract.

The chain it covers
-------------------
``_run_repair_execution_async`` (rc=0) → ``state['stop_reason']`` →
``_make_repair_on_complete`` → ``_record_verification_terminal``.

Why the helpers are rebuilt here
--------------------------------
``_seed_plan`` / ``_repair_row`` / ``_run_repair`` are written out in
this file rather than imported from
``tests/unit/test_repair_exit_accounting.py`` (which this task reads
but does not modify).  A negative control that SHARES its scaffolding
with the behaviour it guards is a negative control that moves when the
behaviour moves.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import server
import verification_loop
from plan_status import QUOTA_WAIT_PHASE


# ---------------------------------------------------------------------------
# Helpers — rebuilt from tests/unit/test_repair_exit_accounting.py
# ---------------------------------------------------------------------------


def _mock_subprocess(returncode: int = 0, pid: int = 424242):
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = pid
    proc.wait = MagicMock(return_value=returncode)
    return proc


def _repair_row(
    task_id: str,
    status: str,
    failed_vp_id: str,
    round_n: int,
) -> dict:
    """One ``plan_tasks`` row for a repair task."""
    return {
        "task_id": task_id,
        "status": status,
        "failed_vp_id": failed_vp_id,
        "round": round_n,
    }


def _seed_plan(
    tmp_path: Path,
    plan_id: str,
    db_path: Path,
    *,
    disk: dict,
    runtime: list,
) -> Path:
    """Write ``tasks.json`` (the ``disk`` payload) plus the state.db rows.

    ``disk`` is written to ``tasks.json`` VERBATIM.  The negative
    control's case has no ``stop_reason`` key in it — that absence is
    exactly what "no quota marker" means on disk.

    The seed needs BOTH ``plan_routing`` and ``plan_execution``:

    * ``plan_routing`` is the plan's phase row.  The repair path
      stamps ``executing`` on it before it spawns the executor, and it
      is the surface the negative control reads to prove the plan was
      NOT parked.
    * ``plan_execution`` is the row the repair path writes the
      terminal status to.  Without it ``ExecutionRepository
      .update_status`` raises ``PlanNotFoundError``, which the repair
      path swallows — the status would then live only in memory.
    """
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(json.dumps(disk), encoding="utf-8")

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
        for row in runtime:
            conn.execute(
                "INSERT INTO plan_tasks (plan_id, task_id, status, "
                "task_group, failed_vp_id, round, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    plan_id,
                    row["task_id"],
                    row["status"],
                    f"repair-round-{row['round']}",
                    row["failed_vp_id"],
                    row["round"],
                    "2026-11-03T00:00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return plan_dir


def _run_repair(monkeypatch, tmp_path, plan_id, db_path, returncode=0):
    """Drive ``_run_repair_execution_async`` to completion; return state."""
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(
        server, "_state_db_path", lambda *_a, **_kw: Path(db_path),
    )
    monkeypatch.setattr(
        server, "_plan_dir",
        lambda pid: tmp_path / "plans" / pid,
    )
    monkeypatch.setattr(
        server, "_spawn_executor_subprocess",
        lambda **kw: (_mock_subprocess(returncode=returncode), tmp_path / "log"),
    )
    with server._execution_state_lock:
        server._execution_state.clear()

    done = threading.Event()
    seen = {}

    def _cb(rc):
        seen["rc"] = rc
        done.set()

    server._run_repair_execution_async(
        plan_id, tmp_path, on_complete=_cb,
    )
    assert done.wait(timeout=5.0), "on_complete never fired"
    with server._execution_state_lock:
        return dict(server._execution_state[plan_id])


def _read_exec_status(plan_id: str, db_path: Path):
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(db_path)
    try:
        return ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()


def _read_routing_phase(plan_id: str, db_path: Path):
    from state_machine.db.connection import open as open_db

    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state.db"


@pytest.fixture(autouse=True)
def _clean_repair_guard():
    """Clear the module-level undrained-repair guard around every test.

    ``verification_loop._repair_not_drained_guard`` is process-wide
    state shared by every plan.  A hit left behind by one test would
    push the NEXT test's first callback onto the
    ``repair_never_drained`` threshold, and the failure would land on
    the wrong test.  Clearing it before and after every test here is
    what makes "guard hit == 1" a meaningful assertion.
    """
    verification_loop._repair_not_drained_guard.clear()
    try:
        yield
    finally:
        verification_loop._repair_not_drained_guard.clear()
        with server._execution_state_lock:
            server._execution_state.pop("p-nc", None)


# ---------------------------------------------------------------------------
# The negative control
# ---------------------------------------------------------------------------


def test_no_marker_keeps_repair_exited_with_unfinished_tasks(
    monkeypatch, tmp_path, db_path,
):
    """Without a quota marker, an undrained repair round still FAILS.

    The executor exited 0 and two repair tasks are still pending.  There
    is no quota marker on disk, so nothing about this round says "the
    providers ran dry" — the round genuinely did not drain, and the
    wording for that is ``repair_batch_not_drained:<n>_pending``.

    (This test's NAME still says ``repair_exited_with_unfinished_tasks``
    — that is the historical spelling of the same fact.  The name is
    kept so the node id stays selectable; the string it names is not
    what the runner writes any more.)

    The quota-park fix must not swallow this branch: if it did, every
    undrained repair round would be reported as parked and the operator
    would never learn that their repair did not run.
    """
    plan_id = "p-nc"
    _seed_plan(
        tmp_path,
        plan_id,
        db_path,
        # No ``stop_reason`` key: there is no quota marker.
        disk={"tasks": [{"id": "repair-r1-01"}, {"id": "repair-r1-02"}]},
        runtime=[
            _repair_row("repair-r1-01", "pending", "VP-005", 1),
            _repair_row("repair-r1-02", "pending", "VP-006", 1),
        ],
    )

    state = _run_repair(monkeypatch, tmp_path, plan_id, db_path, returncode=0)

    assert state["stop_reason"] == "repair_batch_not_drained:2_pending"
    assert state["status"] == "failed"
    assert state["repair_unfinished_tasks"] == 2
    assert state["executor_finished_cleanly"] is True

    # A failed round ENDED; a parked one does not.  ``ended_at`` is the
    # discriminator the readers ask "did this run end?".
    assert state["ended_at"] is not None

    # The round was not parked — neither in memory nor on disk.
    assert state["status"] != QUOTA_WAIT_PHASE
    assert state["stop_reason"] != QUOTA_WAIT_PHASE

    summary = _read_exec_status(plan_id, db_path)
    assert summary is not None, "no plan_execution row was written"
    assert summary["exec_status"] == "failed"
    assert summary["exec_status"] != QUOTA_WAIT_PHASE

    assert _read_routing_phase(plan_id, db_path) != QUOTA_WAIT_PHASE


def test_callback_records_loop_stopped_with_undrained_reason(
    monkeypatch, tmp_path, db_path,
):
    """An undrained batch is re-dispatched; only the BOUND stops the chain.

    The repair path recorded ``repair_batch_not_drained:2_pending`` and
    the repair queue still holds two pending tasks.  That reason says
    "there is still work to run" — it is NOT a verdict about the code,
    so the callback must NOT write a terminal: it re-dispatches the
    repair executor and lets the chain continue.

    The re-dispatch is BOUNDED (``_MAX_REPAIR_NOT_DRAINED_GUARDS``).
    The FIRST callback may not trip the threshold, and the chain stops
    only on the last permitted hit — and then with a reason of its own
    (``repair_never_drained:<reason>:<n>x``), so the batch's own reason
    is never mistaken for a verdict about the code.

    It must NOT park the plan either: ``waiting_for_quota`` is the word
    for "the providers ran dry", and nobody said that here.
    """
    plan_id = "p-nc"
    _seed_plan(
        tmp_path,
        plan_id,
        db_path,
        # No ``stop_reason`` key: there is no quota marker.
        disk={"tasks": [{"id": "repair-r1-01"}, {"id": "repair-r1-02"}]},
        runtime=[
            _repair_row("repair-r1-01", "pending", "VP-005", 1),
            _repair_row("repair-r1-02", "pending", "VP-006", 1),
        ],
    )

    # The callback reads the pending repair queue from the state db
    # through ``server._state_db_path`` — point it at the seed db.
    monkeypatch.setattr(
        server, "_state_db_path", lambda *_a, **_kw: Path(db_path),
    )

    # The in-memory record the repair path leaves behind, seeded
    # DIRECTLY: this test does not run the executor, it pins what the
    # callback does with the record the runner wrote.
    with server._execution_state_lock:
        server._execution_state[plan_id] = {
            "_source": "repair_execution",
            "stop_reason": "repair_batch_not_drained:2_pending",
        }

    # The re-dispatch is a stand-in: this test pins what the callback
    # DOES, and the real executor would spawn a subprocess.
    redispatch = MagicMock(return_value={"status": "started"})
    monkeypatch.setattr(server, "_run_repair_execution_async", redispatch)

    with patch("server._record_verification_terminal") as terminal:
        callback = verification_loop._make_repair_on_complete(plan_id, tmp_path)

        # 1st undrained stop: work is LEFT behind → run it again.  No
        # terminal may be written: the chain is still running.
        callback(0)
        assert terminal.call_count == 0, (
            "an undrained batch is work left behind, not a verdict — "
            f"no terminal may be written yet ({terminal.call_args_list!r})"
        )
        assert redispatch.call_count == 1, (
            "the undrained batch must be re-dispatched — the queued work "
            "still has to run"
        )
        assert verification_loop._repair_not_drained_guard[plan_id] == 1

        # 2nd undrained stop: still below the bound → same answer.
        callback(0)
        assert terminal.call_count == 0, (
            "the re-dispatch is bounded, but the bound is not reached yet "
            f"({terminal.call_args_list!r})"
        )
        assert redispatch.call_count == 2
        assert verification_loop._repair_not_drained_guard[plan_id] == 2

        # 3rd undrained stop: the bound trips.  Now the chain stops —
        # exactly once, and with a reason of its own.
        callback(0)
        assert redispatch.call_count == 2, (
            "the re-dispatch is bounded — the executor is not dispatched a "
            "fourth time"
        )
        assert terminal.call_count == 1, (
            "expected exactly one terminal verdict, got "
            f"{terminal.call_args_list!r}"
        )
        assert terminal.call_args.args == (
            plan_id,
            "loop_stopped",
            "repair_never_drained:repair_batch_not_drained:2_pending:3x",
        )
        assert verification_loop._repair_not_drained_guard[plan_id] == 3

    # The terminal reason is the NEVER-DRAINED one, not the batch's own
    # reason: ``repair_batch_not_drained:`` is the word for "there is
    # still work to run", and it must never be used as a verdict.
    assert terminal.call_args.args[2].startswith("repair_never_drained:")

    # The plan was NOT parked.
    assert _read_routing_phase(plan_id, db_path) != QUOTA_WAIT_PHASE
    summary = _read_exec_status(plan_id, db_path)
    assert summary is not None, "no plan_execution row was written"
    assert summary["exec_status"] != QUOTA_WAIT_PHASE
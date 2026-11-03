"""2026-11-02: a quota-parked repair round must be PARKED, not FAILED.

Background
----------
When every provider runs out of capacity the executor parks the plan
instead of failing it: it writes ``stop_reason="waiting_for_quota"`` into
the top level of ``tasks.json`` and exits with rc=0.

The repair path read that marker nowhere.  Its rc==0 branch asked one
question — "are there still unfinished tasks?" — and a "yes" was written
down as ``status='failed'`` /
``stop_reason='repair_exited_with_unfinished_tasks:N'``.  A repair round
that was parked for capacity was therefore indistinguishable from a
repair round that genuinely failed to drain: the plan was reported as
broken when the only thing that had happened was that the providers ran
dry.

Contract pinned here
--------------------
* rc=0 + a quota marker + unfinished tasks → the round is PARKED:
  ``status == 'waiting_for_quota'``, ``stop_reason == 'waiting_for_quota'``,
  ``ended_at is None`` (the run is parked, not over) and
  ``repair_unfinished_tasks == N``.
* The park is PERSISTED: ``plan_execution.exec_status`` reads
  ``'waiting_for_quota'``, so the operator-facing surfaces say the same
  thing the in-memory state says.
* The unfinished rows are left exactly as they were.  A park is not a
  verdict on the tasks, and ``skipped`` would be a lie that unlocks
  downstream work under ``agent.is_dependency_ready``.
* rc=0 + a quota marker + everything completed → still ``completed``.
  The marker is only consulted when there is leftover work to explain.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import server
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
    quota_marker: bool = False,
) -> Path:
    """Write ``tasks.json`` and the matching state.db rows.

    The seed needs BOTH ``plan_routing`` and ``plan_execution``:

    * ``plan_execution`` is the row the repair path writes the terminal
      status to.  Without it ``ExecutionRepository.update_status`` raises
      ``PlanNotFoundError``, which the repair path swallows — the park
      would then live only in memory and ``exec_status`` would never
      say ``waiting_for_quota``.
    * ``plan_routing`` is the plan's phase row; the repair path stamps
      ``executing`` on it before it spawns the executor.
    """
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    payload = {"tasks": disk_tasks}
    if quota_marker:
        payload["stop_reason"] = "waiting_for_quota"
        payload["reason_detail"] = json.dumps(
            {
                "providers": ["kimi"],
                "needs_manual_topup": True,
                "waited_sec": 12.0,
                "at": "2026-11-02T00:00:00",
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
            (plan_id, "executing", "2026-11-02T00:00:00"),
        )
        conn.execute(
            "INSERT INTO plan_execution (plan_id, attempt_count, project_dir, "
            "exec_status, updated_at) VALUES (?, ?, ?, ?, ?)",
            (plan_id, 1, str(tmp_path), "running", "2026-11-02T00:00:00"),
        )
        for row in task_rows:
            conn.execute(
                "INSERT INTO plan_tasks (plan_id, task_id, status, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (plan_id, row["task_id"], row["status"], "2026-11-02T00:00:00"),
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


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state.db"


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_quota_marker_parks_instead_of_failing(monkeypatch, tmp_path, db_path):
    """A quota-parked repair round is parked, not failed.

    The executor exited 0 because every provider ran out of quota.  The
    repair tasks are still pending — nobody could run them — and the
    plan must say "parked", not "failed".
    """
    plan_id = "p-repair-quota-park"
    _seed_plan(
        tmp_path,
        plan_id,
        db_path,
        disk_tasks=[{"id": "repair-r1-01"}, {"id": "repair-r1-02"}],
        task_rows=[
            {"task_id": "repair-r1-01", "status": "pending"},
            {"task_id": "repair-r1-02", "status": "pending"},
        ],
        quota_marker=True,
    )

    state = _run_repair(monkeypatch, tmp_path, plan_id, db_path, returncode=0)

    assert state["status"] == "waiting_for_quota", (
        "a quota-parked repair round was recorded as "
        f"{state['status']!r} — the plan is reported as broken when the "
        "only thing that happened was that the providers ran dry"
    )
    assert state["status"] == QUOTA_WAIT_PHASE
    assert state["stop_reason"] == QUOTA_WAIT_PHASE
    assert state["ended_at"] is None, (
        "a parked round is not over — ended_at must stay None"
    )
    assert state["repair_unfinished_tasks"] == 2
    assert state["executor_finished_cleanly"] is True

    # The park reaches the database, so the operator-facing surfaces
    # read the same thing the in-memory state says.
    summary = _read_exec_status(plan_id, db_path)
    assert summary is not None, "no plan_execution row was written"
    assert summary["exec_status"] == "waiting_for_quota"

    # The unfinished rows are left exactly as they were: a park is not a
    # verdict on the tasks.
    assert _read_task_statuses(plan_id, db_path) == {
        "repair-r1-01": "pending",
        "repair-r1-02": "pending",
    }


def test_completed_tasks_still_complete_despite_marker(
    monkeypatch, tmp_path, db_path,
):
    """A marker does not park a round that has nothing left to do.

    The marker is only consulted to EXPLAIN leftover work.  With no
    leftover work the round is completed, exactly as before.
    """
    plan_id = "p-repair-quota-done"
    _seed_plan(
        tmp_path,
        plan_id,
        db_path,
        disk_tasks=[{"id": "repair-r1-01"}, {"id": "repair-r1-02"}],
        task_rows=[
            {"task_id": "repair-r1-01", "status": "completed"},
            {"task_id": "repair-r1-02", "status": "completed"},
        ],
        quota_marker=True,
    )

    state = _run_repair(monkeypatch, tmp_path, plan_id, db_path, returncode=0)

    assert state["status"] == "completed"
    assert state["stop_reason"] is None
    assert state["executor_finished_cleanly"] is True

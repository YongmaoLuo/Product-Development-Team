import os

import pytest

from server import (
    _execution_state,
    _recover_execution_states,
)
from plan_state import PlanState

# 2026-09-13 port (SQLite decision): ``_recover_execution_states``
# restores ``_execution_state`` from the ``plan_execution`` SQLite row
# (``ExecutionRepository.summary``) — the retired ``execution.json`` is
# neither read nor written by production. These tests seed the row via
# the hermetic per-test ``PDT_STATE_DB_PATH`` database (same DB the
# server resolves through ``server._state_db_path()``), never the live
# ``state.db``.


def _seed_execution_row(plan_id: str, **fields) -> None:
    """Insert/update a ``plan_execution`` row in the hermetic test DB."""
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(_state_db_path())
    try:
        migrate(conn)
        ExecutionRepository(conn).update_phase(
            plan_id,
            current_phase=fields.pop("current_phase", "executing"),
            create_if_missing=True,
            **fields,
        )
    finally:
        conn.close()


class TestRecoverExecutionStates:
    def test_recover_completed_no_liveness_check(self, sample_plan_factory, monkeypatch):
        plan_dir = sample_plan_factory(phase="executing")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="completed",
            exec_pid=99999,
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)

        assert _execution_state[plan_id]["status"] == "completed"
        # os.kill should NOT be called for non-running status

    def test_recover_running_alive(self, sample_plan_factory, monkeypatch):
        plan_dir = sample_plan_factory(phase="executing")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=os.getpid(),  # current process is alive
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)

        assert _execution_state[plan_id]["status"] == "running"

    def test_recover_running_dead(self, sample_plan_factory, monkeypatch):
        plan_dir = sample_plan_factory(phase="executing")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            exec_pid=99999,  # non-existent PID
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)

        assert _execution_state[plan_id]["status"] == "failed"
        assert _execution_state[plan_id]["stop_reason"] == "process_died_unexpectedly"
        ps = PlanState(plan_dir)
        assert ps.get_current_phase() == "failed"

    def test_recover_missing_pid(self, sample_plan_factory):
        plan_dir = sample_plan_factory(phase="executing")
        plan_id = plan_dir.name
        _seed_execution_row(
            plan_id,
            exec_status="running",
            started_at="2024-01-01T00:00:00",
            project_dir=str(plan_dir / "project"),
        )

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)

        assert _execution_state[plan_id]["status"] == "failed"
        assert _execution_state[plan_id]["stop_reason"] == "process_died_unexpectedly"

    def test_recover_missing_execution_row_is_skipped(self, sample_plan_factory, caplog):
        """A plan dir with NO plan_execution row is skipped silently."""
        plan_dir = sample_plan_factory(phase="executing")
        plan_id = plan_dir.name
        # Deliberately NOT seeding any execution row.

        _execution_state.clear()
        _recover_execution_states(plan_dir.parent)

        assert plan_id not in _execution_state

    def test_recover_empty_plans_dir(self, tmp_path):
        _execution_state.clear()
        _recover_execution_states(tmp_path / "nonexistent")
        assert len(_execution_state) == 0

"""Tests for the _get_pending_repair_tasks helper.

2026-09-12: this helper is the missing link in the iteration
loop. Regardless of whether the current round produced new repair
tasks, every RP-* task generated in an earlier round is still pending
and must be handed to the executor.

The helper reads state.db.plan_tasks directly (bypassing
``check_cycle_conditions``) so the auto-loop can pick up RP-* tasks
that the orchestrator's ``same_failure_repeated`` branch orphaned.

These tests exercise the helper in isolation — they're cheap to run
and don't require spinning up the full server.

The conftest's autouse ``isolated_plans_dir`` fixture monkeypatches
``server._state_db_path`` to return ``tmp_path / state.db`` per test,
so we use that exact path for our test setup writes — pointing at a
different file would make the helper see empty data even when our
test wrote rows.
"""
from __future__ import annotations

import unittest

import server as _server_mod
from server import _get_pending_repair_tasks
from state_machine.db.connection import open as _open_db
from state_machine.db.schema import migrate
from state_machine.repositories.plan_task_repository import (
    PlanTaskRepository,
)


def _db_path():
    """Resolve the DB path through ``server._state_db_path`` so the
    conftest's monkeypatched version (used by the autouse
    ``isolated_plans_dir`` fixture) is honoured. Importing
    ``_state_db_path`` at module level captures the original
    function reference before the autouse fixture applies, so we
    must always go through the module attribute instead.
    """
    return _server_mod._state_db_path()


class TestGetPendingRepairTasks(unittest.TestCase):
    """Exercise the helper against the conftest-isolated state.db."""

    def _make_pending_rp(self, plan_id: str, rp_id: str, title: str,
                        failed_vp_id: str = None, round_no: int = None,
                        task_group: str = "repair") -> None:
        """Add a pending RP-* task using the helper's own DB path."""
        conn = _open_db(_db_path())
        try:
            migrate(conn)
            repo = PlanTaskRepository(conn)
            payload = {
                "id": rp_id,
                "title": title,
                "task_group": task_group,
            }
            if failed_vp_id is not None:
                payload["failed_vp_id"] = failed_vp_id
            if round_no is not None:
                payload["round"] = round_no
            repo.add_task(plan_id, payload)
            repo.update_task(plan_id, rp_id, {"status": "pending"})
            conn.commit()
        finally:
            conn.close()

    def test_returns_empty_when_no_tasks(self) -> None:
        # Reset plan_tasks table so prior tests don't leak in
        conn = _open_db(_db_path())
        try:
            migrate(conn)
            conn.execute("DELETE FROM plan_tasks")
            conn.commit()
        finally:
            conn.close()

        assert _get_pending_repair_tasks("any-plan") == []

    def test_returns_pending_rp_tasks_only(self) -> None:
        """Only RP-* tasks in 'pending' status are returned.
        RP-* in completed/failed status are excluded; non-RP tasks
        (e.g. ``1-1``, ``9-1-2``) are excluded regardless of status.
        """
        # Reset first
        conn = _open_db(_db_path())
        try:
            migrate(conn)
            conn.execute("DELETE FROM plan_tasks")
            conn.commit()
        finally:
            conn.close()

        # Pending RP-1 / RP-2 — should be returned
        self._make_pending_rp("plan-x", "RP-1", "fix VP-006",
                              failed_vp_id="VP-006", round_no=2)
        self._make_pending_rp("plan-x", "RP-2", "fix VP-023")

        # Completed RP-3 — should NOT be returned
        self._make_pending_rp("plan-x", "RP-3", "fix VP-027")
        conn = _open_db(_db_path())
        try:
            repo = PlanTaskRepository(conn)
            repo.update_task("plan-x", "RP-3", {"status": "completed"})
            conn.commit()
        finally:
            conn.close()

        # Pending non-RP — should NOT be returned
        self._make_pending_rp("plan-x", "9-1-2", "regular task",
                              task_group="implementation")

        result = _get_pending_repair_tasks("plan-x")
        ids = sorted(r["id"] for r in result)
        assert ids == ["RP-1", "RP-2"], (
            f"Expected only pending RP-1 and RP-2; got {ids}"
        )
        # Title field is propagated from DB
        titles = {r["id"]: r["title"] for r in result}
        assert titles["RP-1"] == "fix VP-006"
        assert titles["RP-2"] == "fix VP-023"

    def test_filters_by_plan_id(self) -> None:
        """Tasks for OTHER plans are excluded — the helper is
        plan-scoped via the WHERE plan_id = ? filter.
        """
        # Reset
        conn = _open_db(_db_path())
        try:
            migrate(conn)
            conn.execute("DELETE FROM plan_tasks")
            conn.commit()
        finally:
            conn.close()

        self._make_pending_rp("plan-x", "RP-1", "x")
        self._make_pending_rp("plan-y", "RP-9", "y")

        x = _get_pending_repair_tasks("plan-x")
        y = _get_pending_repair_tasks("plan-y")
        assert [r["id"] for r in x] == ["RP-1"]
        assert [r["id"] for r in y] == ["RP-9"]

    def test_propagates_failed_vp_id_and_round(self) -> None:
        """The failed_vp_id and round fields are useful for the
        executor and Feishu card. Verify they're forwarded.
        """
        # Reset
        conn = _open_db(_db_path())
        try:
            migrate(conn)
            conn.execute("DELETE FROM plan_tasks")
            conn.commit()
        finally:
            conn.close()

        self._make_pending_rp("plan-x", "RP-1", "fix VP-006",
                              failed_vp_id="VP-006", round_no=2)

        result = _get_pending_repair_tasks("plan-x")
        assert len(result) == 1
        assert result[0]["failed_vp_id"] == "VP-006"
        assert result[0]["round"] == 2

    def test_handles_db_error_gracefully(self) -> None:
        """When the DB is unreachable, the helper returns []
        instead of raising — callers fall back to the
        orchestrator's ``result["repair_tasks"]`` payload.
        """
        # Point at a non-existent DB path by monkeypatching
        # _state_db_path within the test
        import server as _server_mod

        original = _server_mod._state_db_path
        try:
            _server_mod._state_db_path = lambda request=None: "/tmp/__nope__.db"
            assert _get_pending_repair_tasks("any-plan") == []
        finally:
            _server_mod._state_db_path = original


if __name__ == "__main__":
    unittest.main()
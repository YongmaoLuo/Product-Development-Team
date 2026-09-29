"""Test the 2026-09-12 fix for _get_pending_repair_tasks R{n}-* filter.

Bug
---
``_get_pending_repair_tasks`` filtered on ``tid.startswith("RP-")`` only,
silently dropping the post-v9 ``R<round>-<i>`` ids that the orchestrator
generates. The auto-loop then ran ``_run_auto_verification_loop``,
saw ``_pending_db == []``, and recorded terminal
``no_repair_tasks`` even when state.db had genuinely-pending R6-1 / R6-2
entries — breaking the closed-loop iteration.

Fix
----
Match either ``RP-*`` (legacy) OR ``R<digits>-<digit>`` (post-v9) AND
fall back to ``task_group.startswith("repair")`` as the authoritative
marker.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fresh_state_db(tmp_path, monkeypatch):
    """Build a real state.db with the v4 schema and inject both id shapes."""
    db_path = tmp_path / "state.db"
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    conn = open_db(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    # Patch server._state_db_path to point at our tmp DB.
    import server
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)

    from state_machine.db.connection import open as open_db
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    plan_id = "test-repair-plan"
    conn = open_db(db_path)
    try:
        repo = PlanTaskRepository(conn)
        # Legacy RP-1 (pending) — pre-v9 format
        repo.add_task(plan_id, {
            "id": "RP-1",
            "title": "fix VP-006 (legacy)",
            "task_group": "repair",
            "failed_vp_id": "VP-006",
            "round": 1,
        })
        # Post-v9 R6-2 (pending) — new format
        repo.add_task(plan_id, {
            "id": "R6-2",
            "title": "fix VP-027 (post-v9)",
            "task_group": "repair-round-6",
            "failed_vp_id": "VP-027",
            "round": 6,
        })
        # Post-v9 R6-1 (completed) — should NOT appear
        repo.add_task(plan_id, {
            "id": "R6-1",
            "title": "fix VP-006 (completed)",
            "task_group": "repair-round-6",
            "failed_vp_id": "VP-006",
            "round": 6,
        })
        # Use update_task to mark R6-1 as completed
        repo.update_task(plan_id, "R6-1", {"status": "completed"})
        # Normal task (no task_group) — should NOT appear
        repo.add_task(plan_id, {
            "id": "11-2",
            "title": "normal task",
            "task_group": None,
        })
    finally:
        conn.close()

    return plan_id, db_path


def test_legacy_RP_id_surfaces(fresh_state_db):
    plan_id, _ = fresh_state_db
    import server
    pending = server._get_pending_repair_tasks(plan_id)
    ids = {t["id"] for t in pending}
    assert "RP-1" in ids, f"legacy RP-1 missing; got {ids}"


def test_post_v9_R_id_surfaces(fresh_state_db):
    plan_id, _ = fresh_state_db
    import server
    pending = server._get_pending_repair_tasks(plan_id)
    ids = {t["id"] for t in pending}
    assert "R6-2" in ids, f"post-v9 R6-2 missing; got {ids}"


def test_completed_R_id_filtered_out(fresh_state_db):
    plan_id, _ = fresh_state_db
    import server
    pending = server._get_pending_repair_tasks(plan_id)
    ids = {t["id"] for t in pending}
    assert "R6-1" not in ids, f"completed R6-1 should NOT appear; got {ids}"


def test_normal_task_filtered_out(fresh_state_db):
    plan_id, _ = fresh_state_db
    import server
    pending = server._get_pending_repair_tasks(plan_id)
    ids = {t["id"] for t in pending}
    assert "11-2" not in ids, f"normal 11-2 should NOT appear; got {ids}"


def test_pending_set_exactly_R6_2_and_RP_1(fresh_state_db):
    """Combined assertion: the function returns ONLY the two pending
    repair tasks (one legacy, one post-v9)."""
    plan_id, _ = fresh_state_db
    import server
    pending = server._get_pending_repair_tasks(plan_id)
    ids = {t["id"] for t in pending}
    assert ids == {"RP-1", "R6-2"}, (
        f"expected exactly {{RP-1, R6-2}}; got {ids}"
    )

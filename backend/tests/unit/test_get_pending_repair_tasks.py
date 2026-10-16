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


# ===========================================================================
# 2026-10-16 — the phantom split parent. A STANDING defect, pinned.
# ===========================================================================


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known defect, documented rather than fixed. ``_split_parent_ids`` "
        "parses ``rpartition('-') + suffix.isdigit()`` and so cannot tell a "
        "breakdown ordinal from any other bare integer. ``repair-r1-01`` "
        "therefore manufactures the phantom parent ``repair-r1``; if a task "
        "id ``repair-r1`` exists, it is classified as 'already split' and "
        "dropped from revival by ``repair_task_skipped_split_into_children``, "
        "so a failed repair task whose VP is still failing is never offered "
        "again. Today's live ids do not collide (a read-only audit of "
        "state.db finds every phantom name that is also a real id is a "
        "genuine breakdown parent), so nothing is broken in production "
        "today — this test exists so that if the collision ever appears, or "
        "if someone fixes the parser, one of the two is loud. "
        "strict=True is load-bearing: a FIX makes this XPASS, which fails "
        "the suite and forces this marker to be removed on purpose rather "
        "than the test rotting green."
    ),
)
def test_repair_r1_with_breakdown_child_is_silently_dropped(tmp_path, monkeypatch):
    """A ``failed`` repair task must be revived, not mistaken for a parent.

    ``repair-r1`` is failed, its ``failed_vp_id`` is in this round's
    failure set and its attempt counter is 0 — every revival criterion
    :func:`_get_pending_repair_tasks` documents is met. It must come back
    in the queue.

    It does not, because ``repair-r1-01`` exists and
    :func:`_split_parent_ids` reads that suffix ``01`` as a breakdown
    ordinal. The repair round's bare number is mistaken for a task id.

    The fixture is deliberately minimal — two rows — because the whole
    claim is that ONE extra id is enough to silently remove a task that
    should have run. ``control_repair_r1_alone_revives`` is the same
    fixture without that one row and passes today, which is what proves
    the drop is caused by the id and not by anything else in the setup.
    """
    db_path = tmp_path / "state.db"
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    conn = open_db(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    import server
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    plan_id = "phantom-parent-plan"
    conn = open_db(db_path)
    try:
        repo = PlanTaskRepository(conn)
        repo.add_task(plan_id, {
            "id": "repair-r1",
            "title": "repair round 1",
            "task_group": "repair",
            "failed_vp_id": "VP-006",
            "round": 1,
        })
        repo.update_task(plan_id, "repair-r1",
                         {"status": "failed", "attempt": 0})
        # The single row that causes the drop. In a real database this is
        # a breakdown child; nothing records that relationship, so the
        # reader infers it from the spelling.
        repo.add_task(plan_id, {
            "id": "repair-r1-01",
            "title": "breakdown child",
            "task_group": "repair",
            "failed_vp_id": "VP-006",
            "round": 1,
        })
    finally:
        conn.close()

    pending = server._get_pending_repair_tasks(plan_id, failed_vp_ids={"VP-006"})
    ids = {t["id"] for t in pending}
    assert "repair-r1" in ids, (
        "repair-r1 meets every revival criterion (failed, its VP is still "
        "in this round's failure set, attempt 0 of 3) and must be "
        "re-queued; it was dropped by repair_task_skipped_split_into_"
        f"children because 'repair-r1-01' made a phantom parent out of "
        f"the bare round number. pending={sorted(ids)}"
    )


def test_control_repair_r1_alone_revives(tmp_path, monkeypatch):
    """The control for the xfail above: identical, minus ``repair-r1-01``.

    Without that one row the phantom parent does not exist and
    ``repair-r1`` is revived normally. If this ever goes red, the xfail
    above stopped being about the phantom parent and is about something
    else entirely — which is exactly when you would want to know.
    """
    db_path = tmp_path / "state.db"
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    conn = open_db(db_path)
    try:
        migrate(conn)
    finally:
        conn.close()

    import server
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    plan_id = "control-plan"
    conn = open_db(db_path)
    try:
        repo = PlanTaskRepository(conn)
        repo.add_task(plan_id, {
            "id": "repair-r1",
            "title": "repair round 1",
            "task_group": "repair",
            "failed_vp_id": "VP-006",
            "round": 1,
        })
        repo.update_task(plan_id, "repair-r1",
                         {"status": "failed", "attempt": 0})
    finally:
        conn.close()

    pending = server._get_pending_repair_tasks(plan_id, failed_vp_ids={"VP-006"})
    ids = {t["id"] for t in pending}
    assert "repair-r1" in ids, (
        "without a breakdown child there is no phantom parent and the "
        f"revival criterion must be met; pending={sorted(ids)}"
    )


"""Startup reconciliation of orphaned verification stages (2026-09-13).

Pins the durable fix for the split-brain: a plan whose
verification thread died across a server restart keeps
``plan_routing.stage = 'verification_running'`` on disk while
``plan_verification.verification_status`` is already terminal.  The
in-memory watchdog (``_lazy_check_verification``) only inspects plans
present in ``_verification_state``, so after a restart the stranded
row is invisible forever — and every notifier startup sweep re-pushes
the plan's failed card (the "卡片一直处于失败状态" report).

``server._reconcile_orphaned_verification_stages`` (invoked from the
lifespan right after ``_recover_verification_states``) sweeps routing
rows at ``verification_running`` / ``verification_rerunning`` whose
verification row is terminal and re-emits
``_persist_verification_terminal(..., chain_ending=True)``, which
CASes the routing stage to ``completed`` / ``failed`` and
mirrors ``current_phase``.

Contract pins:

  * stranded ``verification_running`` + terminal ``failed`` →
    ``failed``;
  * stranded ``verification_rerunning`` + terminal ``passed`` →
    ``completed``;
  * ``verification_repairing`` (user-gated pause) is NOT touched even
    with a terminal verification status from the previous round;
  * a genuinely-running row (``verification_status = 'running'``) is
    left for the watchdog;
  * a recovered in-memory ``_verification_state`` entry for a
    reconciled plan is dropped so /status cannot report "running";
  * no state.db on disk → no-op (fresh install).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


@pytest.fixture
def seeded_stalled_plan(monkeypatch, tmp_path):
    """Seed a hermetic state.db with a stalled verification plan.

    Returns ``(plan_id, routing_repo, verification_repo, conn)`` where
    ``conn`` is the open connection the caller must close.
    """
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    db_path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    conn = open_db(db_path)
    migrate(conn)
    plan_id = "20260101-stalled-reconcile"
    RoutingRepository(conn).insert(plan_id, "verification_running")
    VerificationRepository(conn).insert(plan_id, verification_status="failed")
    repo = VerificationRepository(conn)
    repo._update(
        plan_id,
        verification_status="failed",
        verification_stop_reason="verification_log_stale",
    )
    return plan_id, RoutingRepository(conn), repo, conn


def test_stranded_verification_running_cas_to_failed(
    seeded_stalled_plan, monkeypatch
):
    """routing stage 'verification_running' + terminal verification row
    → CAS to 'failed' and no in-memory entry survives."""
    import server

    plan_id, routing, _verif, conn = seeded_stalled_plan
    server._verification_state[plan_id] = {
        "verification_status": "running",
        "orchestrator": None,
        "thread": None,
    }
    try:
        server._reconcile_orphaned_verification_stages()

        row = routing.find(plan_id)
        assert row is not None, "routing row must survive reconciliation"
        assert row["current_phase"] == "failed", (
            f"stranded verification_running row must CAS to "
            f"failed, got {row['stage']!r}"
        )
        assert plan_id not in server._verification_state, (
            "reconciled plan must be evicted from the in-memory "
            "verification state so /status cannot report 'running'"
        )
    finally:
        server._verification_state.pop(plan_id, None)
        conn.close()


def test_stranded_verification_rerunning_passed_cas_to_completed(
    seeded_stalled_plan, monkeypatch
):
    """routing stage 'verification_rerunning' + terminal 'passed' →
    'completed'."""
    import server
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_id, _routing, verif, conn = seeded_stalled_plan
    # Re-stage: rerunning + passed.
    RoutingRepository(conn).try_mark_phase(
        plan_id, ("verification_running",), "verification_rerunning"
    )
    verif._update(plan_id, verification_status="passed")
    try:
        server._reconcile_orphaned_verification_stages()

        row = RoutingRepository(conn).find(plan_id)
        assert row is not None
        assert row["current_phase"] == "completed", (
            f"stranded verification_rerunning row with passed verdict "
            f"must CAS to completed, got {row['stage']!r}"
        )
    finally:
        conn.close()


def test_verification_repairing_not_touched(seeded_stalled_plan, monkeypatch):
    """'verification_repairing' is a user-gated pause — a terminal
    verification_status from the PREVIOUS round is expected there and
    must NOT be reconciled."""
    import server
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_id, _routing, _verif, conn = seeded_stalled_plan
    RoutingRepository(conn).try_mark_phase(
        plan_id, ("verification_running",), "verification_repairing"
    )
    try:
        server._reconcile_orphaned_verification_stages()

        row = RoutingRepository(conn).find(plan_id)
        assert row is not None
        assert row["current_phase"] == "verification_repairing", (
            f"verification_repairing must be left alone by the "
            f"reconciler, got {row['stage']!r}"
        )
    finally:
        conn.close()


def test_genuinely_running_row_left_for_watchdog(seeded_stalled_plan, monkeypatch):
    """routing 'verification_running' + verification_status 'running'
    is a live run — the reconciler must leave it to the watchdog."""
    import server
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_id, _routing, verif, conn = seeded_stalled_plan
    verif._update(
        plan_id,
        verification_status="running",
        verification_stop_reason=None,
    )
    try:
        server._reconcile_orphaned_verification_stages()

        row = RoutingRepository(conn).find(plan_id)
        assert row is not None
        assert row["current_phase"] == "verification_running", (
            f"a genuinely-running verification must not be CAS'd by "
            f"the startup reconciler, got {row['stage']!r}"
        )
    finally:
        conn.close()


def test_no_state_db_is_noop(monkeypatch, tmp_path):
    """Fresh install (no state.db) → the reconciler no-ops silently."""
    import server

    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "missing" / "state.db"))
    server._reconcile_orphaned_verification_stages()  # must not raise


# ---------------------------------------------------------------------------
# 2026-09-14: dead-ended verification_repairing rows
# ---------------------------------------------------------------------------
#
# Operator decision (an earlier plan parked at
# verification_repairing with an empty repair list): a
# ``no_repair_tasks`` stop reason means the user gate has nothing to
# confirm.  Unfinished execution work → roll back to ``ready``
# (operator resumes explicitly); nothing left → chain-ending terminal.


def _seed_dead_end_repairing(seeded_stalled_plan):
    """Re-stage the fixture as a dead-ended repairing row."""
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_id, _routing, verif, conn = seeded_stalled_plan
    RoutingRepository(conn).try_mark_phase(
        plan_id, ("verification_running",), "verification_repairing"
    )
    verif._update(
        plan_id,
        verification_status="failed",
        verification_stop_reason="no_repair_tasks",
    )
    return plan_id, conn


def _insert_task(conn, plan_id, task_id, status):
    conn.execute(
        "INSERT INTO plan_tasks (plan_id, task_id, status, _repo_version, "
        "updated_at) VALUES (?, ?, ?, 0, '2026-09-14T00:00:00Z')",
        (plan_id, task_id, status),
    )
    conn.commit()


def test_dead_end_repairing_no_unfinished_tasks_goes_terminal(
    seeded_stalled_plan,
):
    """repairing + no_repair_tasks + zero unfinished tasks → the chain
    closes: routing stage CAS'd to failed (never strands on
    the user gate with an empty repair list)."""
    import server
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_id, conn = _seed_dead_end_repairing(seeded_stalled_plan)
    try:
        server._reconcile_orphaned_verification_stages()

        row = RoutingRepository(conn).find(plan_id)
        assert row is not None
        assert row["current_phase"] == "failed", (
            f"a dead-ended verification_repairing row with no unfinished "
            f"work must close the chain (failed), got {row['stage']!r}"
        )
    finally:
        conn.close()


def test_dead_end_repairing_with_unfinished_tasks_rolls_back_to_ready(
    seeded_stalled_plan,
):
    """repairing + no_repair_tasks + pending execution tasks → routing
    rolls back to ready and current_phase mirrors 'ready', so the
    operator can resume execution explicitly (Ready != Start)."""
    import server
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_id, conn = _seed_dead_end_repairing(seeded_stalled_plan)
    _insert_task(conn, plan_id, "9-9", "pending")
    try:
        server._reconcile_orphaned_verification_stages()

        row = RoutingRepository(conn).find(plan_id)
        assert row is not None
        assert row["current_phase"] == "ready", (
            f"a dead-ended verification_repairing row with unfinished "
            f"work must roll back to ready, got {row['stage']!r}"
        )
        phase = conn.execute(
            "SELECT current_phase FROM plan_execution WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        assert phase is not None and phase[0] == "ready", (
            f"current_phase must mirror the rollback (ready), got {phase!r}"
        )
    finally:
        conn.close()


def test_count_unfinished_execution_tasks(seeded_stalled_plan):
    """_count_unfinished_execution_tasks counts pending + in_progress
    rows only — completed / failed tasks are terminal and must not
    count as resumable work."""
    import server

    plan_id, _routing, _verif, conn = seeded_stalled_plan
    try:
        assert server._count_unfinished_execution_tasks(plan_id) == 0
        _insert_task(conn, plan_id, "1", "pending")
        _insert_task(conn, plan_id, "2", "in_progress")
        _insert_task(conn, plan_id, "3", "completed")
        _insert_task(conn, plan_id, "4", "failed")
        assert server._count_unfinished_execution_tasks(plan_id) == 2
    finally:
        conn.close()


def test_dead_end_reason_in_results_json_only(seeded_stalled_plan):
    """The live shape: ``verification_stop_reason`` is NULL and the
    reason lives in the ``results`` JSON payload (what an earlier plan row actually looks like — verified against state.db on
    2026-09-14).  The sweep must still recognise the dead end."""
    import json as _json

    import server
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_id, conn = _seed_dead_end_repairing(seeded_stalled_plan)
    # Revert the column back to NULL and carry the reason in ``results``.
    conn.execute(
        "UPDATE plan_verification SET verification_stop_reason = NULL, "
        "results = ? WHERE plan_id = ?",
        (
            _json.dumps(
                {
                    "status": "failed",
                    "stop_reason": "no_repair_tasks",
                    "recorded_by": "_persist_verification_terminal",
                }
            ),
            plan_id,
        ),
    )
    conn.commit()
    try:
        server._reconcile_orphaned_verification_stages()

        row = RoutingRepository(conn).find(plan_id)
        assert row is not None
        assert row["current_phase"] == "failed", (
            f"a dead end whose stop_reason is only in the results JSON "
            f"must still be reconciled, got {row['stage']!r}"
        )
    finally:
        conn.close()


def test_repairing_with_pending_repair_task_left_alone(seeded_stalled_plan):
    """A repairing row with a pending RP-* task is a LIVE user gate —
    the sweep must not touch it, even with the no_repair_tasks reason
    present in the results payload (defence against a stale reason
    from the previous round)."""
    import json as _json

    import server
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_id, conn = _seed_dead_end_repairing(seeded_stalled_plan)
    conn.execute(
        "UPDATE plan_verification SET verification_stop_reason = NULL, "
        "results = ? WHERE plan_id = ?",
        (_json.dumps({"status": "failed", "stop_reason": "no_repair_tasks"}), plan_id),
    )
    _insert_task(conn, plan_id, "R1-1", "pending")
    try:
        server._reconcile_orphaned_verification_stages()

        row = RoutingRepository(conn).find(plan_id)
        assert row is not None
        assert row["current_phase"] == "verification_repairing", (
            f"a repairing row with a pending RP-* task must be left for "
            f"the user to confirm, got {row['stage']!r}"
        )
    finally:
        conn.close()

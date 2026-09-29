"""Tests for ``verification_status`` atomicity contract.

Background (audit 2026-09-09 plan ``2026-09-04 plan``):
the plan's state.db ended up with ``results.recorded_by ==
"_persist_verification_terminal"`` but ``verification_status ==
"pending"`` (the initial INSERT value). The two columns should have
been updated together by ``complete_round``'s SQL UPDATE under a
single transaction, so the divergence indicates a drift we couldn't
root-cause without server.log retention.

These tests pin the contract:

  1. ``complete_round`` raises ``VerificationStatusInconsistent`` when
     a post-write read shows ``verification_status`` did NOT match
     what we just wrote (simulated by injecting a writer between the
     UPDATE and the read).
  2. ``repair_stale_terminal_state`` heals the audit scenario
     (``results.recorded_by`` set, ``verification_status == pending``)
     by deriving ``verification_status`` from ``results.status``.
  3. ``repair_stale_terminal_state`` is a no-op when the row is
     already consistent.
  4. ``repair_stale_terminal_state`` mirrors the repaired
     ``verification_status`` to ``plan_routing.verification`` JSON
     column, so the ``/api/verification/{id}/progress`` endpoint
     (which reads via PlanState) sees the same verdict.
  5. ``complete_round`` succeeds and ``verification_status`` ends up
     matching ``status`` (the happy-path regression).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _open_state_machine_db(tmp_path: Path):
    """Open a fresh state-machine SQLite file with the schema applied."""
    from state_machine.db.connection import open
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open(str(db_path))
    migrate(conn)
    return conn


def _seed_plan_row(
    conn: sqlite3.Connection,
    plan_id: str,
    *,
    verification_status: str = "pending",
    results: dict | None = None,
    routing_verification: dict | None = None,
    routing_stage: str = "verification_running",
    routing_current_phase: str = "verification_running",
) -> None:
    """Seed ``plan_verification`` + ``plan_routing`` rows for ``plan_id``."""
    conn.execute(
        "INSERT INTO plan_verification "
        "(plan_id, verification_status, round, max_rounds, "
        " verification_stop_reason, results, started_at, updated_at) "
        "VALUES (?, ?, 0, 3, NULL, ?, '2026-09-09T07:00:00Z', "
        "        '2026-09-09T07:00:00Z')",
        (
            plan_id,
            verification_status,
            json.dumps(results) if results is not None else None,
        ),
    )
    if routing_verification is None:
        routing_verification = {"status": verification_status, "round": 0,
                                "max_rounds": 3, "stop_reason": None}
    conn.execute(
        # 2026-09-17 (schema v5): one workflow-state column.
        "INSERT INTO plan_routing "
        "(plan_id, current_phase, verification, completed_phases, "
        " review_rounds, flags, updated_at) "
        "VALUES (?, ?, ?, '{}', '{}', '{}', '2026-09-09T07:00:00Z')",
        (
            plan_id,
            routing_current_phase,
            json.dumps(routing_verification),
        ),
    )
    conn.commit()


@pytest.fixture
def repo(tmp_path):
    """A fresh VerificationRepository backed by an in-memory SQLite file."""
    conn = _open_state_machine_db(tmp_path)
    yield VerificationRepository(conn)
    conn.close()


def test_complete_round_writes_both_columns_atomically(tmp_path):
    """Happy path: ``complete_round`` writes both columns in one SQL
    UPDATE, post-write invariant check passes, no exception raised.

    Regression for the pre-2026-09-09 audit path: previously
    ``complete_round`` ran the UPDATE then committed silently; this
    test confirms the new post-write assertion does NOT spuriously
    raise on a clean write.
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-happy"
    _seed_plan_row(conn, plan_id, verification_status="running")
    repo.complete_round(
        plan_id,
        results={
            "status": "passed",
            "stop_reason": None,
            "recorded_by": "_persist_verification_terminal",
        },
        status="passed",
    )
    row = repo.current(plan_id)
    assert row["verification_status"] == "passed", row
    assert row["results"]["recorded_by"] == "_persist_verification_terminal", row


def test_complete_round_raises_on_post_write_status_drift(tmp_path):
    """If something resets ``verification_status`` between the UPDATE
    and the post-write read, ``complete_round`` raises
    ``VerificationStatusInconsistent`` instead of silently committing
    a drifted state.

    Simulated by monkey-patching ``VerificationRepository.current``
    to return the drifted value for one call (the post-write read).
    The actual write still succeeds at the SQL level — the assertion
    is what catches it.
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    from state_machine.repositories.verification_status_inconsistent import (
        VerificationStatusInconsistent,
    )

    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-drift"
    _seed_plan_row(conn, plan_id, verification_status="running")

    original_current = repo.current
    call_count = {"n": 0}

    def fake_current(pid):
        call_count["n"] += 1
        # First call is from inside the assertion; the second would be
        # the test's own read. Either way, return drifted state.
        result = original_current(pid)
        if result is not None:
            result = dict(result)
            result["verification_status"] = "pending"
        return result

    repo.current = fake_current
    try:
        with pytest.raises(VerificationStatusInconsistent) as exc_info:
            repo.complete_round(
                plan_id,
                results={
                    "status": "failed",
                    "stop_reason": "test",
                    "recorded_by": "_persist_verification_terminal",
                },
                status="failed",
            )
        assert exc_info.value.plan_id == plan_id
        assert exc_info.value.expected_status == "failed"
        assert exc_info.value.actual_status == "pending"
    finally:
        repo.current = original_current


def test_repair_heals_audit_scenario(tmp_path):
    """Reproduce the audit-2026-09-09 scenario:

      * ``results.recorded_by = "_persist_verification_terminal"``
      * ``results.status = "passed"`` (the verdict step 1 stamped)
      * ``verification_status = "pending"`` (the initial INSERT value
        that step 1's UPDATE should have replaced)

    ``repair_stale_terminal_state`` should derive
    ``verification_status = "passed"`` from ``results.status``.
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-audit-scenario"
    _seed_plan_row(
        conn,
        plan_id,
        verification_status="pending",
        results={
            "status": "passed",
            "stop_reason": None,
            "recorded_by": "_persist_verification_terminal",
        },
    )

    previous = repo.repair_stale_terminal_state(plan_id)

    assert previous == "pending", f"expected previous='pending', got {previous!r}"
    row = repo.current(plan_id)
    assert row["verification_status"] == "passed", (
        f"repair did not heal: status={row['verification_status']!r}"
    )


def test_repair_is_noop_when_consistent(tmp_path):
    """If ``verification_status`` already matches a terminal value,
    ``repair_stale_terminal_state`` returns ``None`` and leaves the
    row untouched.
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-noop"
    _seed_plan_row(
        conn,
        plan_id,
        verification_status="passed",
        results={
            "status": "passed",
            "stop_reason": None,
            "recorded_by": "_persist_verification_terminal",
        },
    )

    result = repo.repair_stale_terminal_state(plan_id)
    assert result is None, f"expected None, got {result!r}"

    row = repo.current(plan_id)
    assert row["verification_status"] == "passed"
    assert row["updated_at"] == "2026-09-09T07:00:00Z", (
        "repair should not touch updated_at when no-op"
    )


def test_repair_mirrors_to_plan_routing_verification_column(tmp_path):
    """``/api/verification/{id}/progress`` reads from
    ``plan_routing.verification`` JSON column via ``PlanState``. The
    repair must mirror the verdict there so the card layer sees the
    same status as ``plan_verification.verification_status``.
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-mirror"
    _seed_plan_row(
        conn,
        plan_id,
        verification_status="pending",
        routing_verification={"status": "pending", "round": 0,
                              "max_rounds": 3, "stop_reason": None},
        results={
            "status": "failed",
            "stop_reason": "vp_034_timeout",
            "recorded_by": "_persist_verification_terminal",
        },
    )

    repo.repair_stale_terminal_state(plan_id)

    rrow = conn.execute(
        "SELECT verification FROM plan_routing WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    routing_v = json.loads(rrow[0])
    assert routing_v["status"] == "failed", (
        f"plan_routing.verification.status not mirrored: {routing_v!r}"
    )
    assert routing_v.get("stop_reason") == "vp_034_timeout", (
        f"plan_routing.verification.stop_reason not mirrored: {routing_v!r}"
    )


def test_repair_skips_non_persist_terminal_records(tmp_path):
    """``results.recorded_by`` not equal to
    ``_persist_verification_terminal`` means the terminal write came
    from some other path (``mark_stopped`` for example). The repair
    helper must not touch it.
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-other-recorded-by"
    _seed_plan_row(
        conn,
        plan_id,
        verification_status="loop_stopped",
        results={"status": "loop_stopped", "stop_reason": "user_stopped",
                "recorded_by": "mark_stopped_helper"},
    )

    result = repo.repair_stale_terminal_state(plan_id)
    assert result is None
    row = repo.current(plan_id)
    assert row["verification_status"] == "loop_stopped"


def test_repair_handles_missing_results(tmp_path):
    """If ``results`` is ``None`` (the row was just inserted but no
    terminal write happened), repair must not crash.
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-no-results"
    _seed_plan_row(conn, plan_id, verification_status="pending", results=None)

    result = repo.repair_stale_terminal_state(plan_id)
    assert result is None
    row = repo.current(plan_id)
    assert row["verification_status"] == "pending"


def test_mark_stopped_post_write_assertion_passes(tmp_path):
    """``mark_stopped`` also runs the post-write invariant. The
    happy path (write matches read) must NOT raise.
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-mark-stopped"
    _seed_plan_row(conn, plan_id, verification_status="running")

    repo.mark_stopped(plan_id, reason="user_stopped", status="loop_stopped")

    row = repo.current(plan_id)
    assert row["verification_status"] == "loop_stopped"
    assert row["verification_stop_reason"] == "user_stopped"

def test_repair_never_restamps_a_live_round(tmp_path):
    """2026-09-15 a production plan live incident: round 3 was flipped to
    ``failed`` 20 s after it started, because ``repair_stale_terminal_state``
    (fired by every /progress read) saw ``results.recorded_by=
    "_persist_verification_terminal"`` from the PREVIOUS round and
    "healed" the live row backwards. A ``running`` status means a live
    round owns the row — the repair must refuse to touch it.
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-repair-skips-live-round"
    _seed_plan_row(
        conn,
        plan_id,
        verification_status="running",
        results={
            "status": "failed",
            "stop_reason": "Illegal transition from 'failed' to "
                           "'verification_failed'",
            "recorded_by": "_persist_verification_terminal",
        },
    )

    previous = repo.repair_stale_terminal_state(plan_id)

    assert previous is None, f"a live round must not be repaired, got {previous!r}"
    row = repo.current(plan_id)
    assert row["verification_status"] == "running", (
        f"repair restamped a live round: {row['verification_status']!r}"
    )
    assert row["verification_stop_reason"] is None, (
        f"repair poisoned a live round's stop_reason: "
        f"{row['verification_stop_reason']!r}"
    )


def test_init_round_clears_stale_results_but_keeps_verdicts(tmp_path):
    """Round N's ``results`` envelope must not survive into round N+1:
    a fresh round has no results until ``complete_round`` writes them.
    The resume path's ``verdicts`` cache, however, MUST survive
    (init_round's documented contract).
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-init-clears-results"
    _seed_plan_row(
        conn,
        plan_id,
        verification_status="failed",
        results={
            "status": "failed",
            "stop_reason": "verification_log_stale",
            "recorded_by": "_persist_verification_terminal",
        },
    )
    conn.execute(
        "UPDATE plan_verification SET verdicts = ? WHERE plan_id = ?",
        (json.dumps([{"vp_id": "VP-001", "status": "PASSED"}]), plan_id),
    )
    conn.commit()

    repo.init_round(plan_id, round_n=3, max_rounds=3)

    row = repo.current(plan_id)
    assert row["verification_status"] == "running"
    assert row["round"] == 3
    assert row["results"] is None, (
        f"stale terminal envelope must be cleared at round start, "
        f"got {row['results']!r}"
    )
    assert row["verification_stop_reason"] is None
    assert row["verdicts"] == [{"vp_id": "VP-001", "status": "PASSED"}], (
        "verdicts cache must survive init_round (resume contract)"
    )


def test_reset_round_counter_clears_envelope_and_mirror_and_reset_sticks(tmp_path):
    """The 2026-09-15 wedge: ``reset_rounds`` wrote
    ``verification_status='pending'`` but left the old terminal
    ``results`` envelope in place — so the next /progress read's
    auto-repair re-stamped the row to ``failed`` and the reset was
    undone within seconds. The reset must clear the envelope AND
    re-sync the ``plan_routing.verification`` mirror, after which a
    repair pass must be a no-op ("the reset sticks").
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-reset-sticks"
    _seed_plan_row(
        conn,
        plan_id,
        verification_status="failed",
        results={
            "status": "failed",
            "stop_reason": "Illegal transition from 'failed' to "
                           "'verification_failed'",
            "recorded_by": "_persist_verification_terminal",
        },
        routing_verification={"status": "failed", "round": 3, "max_rounds": 3,
                              "stop_reason": "verification_log_stale"},
    )

    repo.reset_round_counter(plan_id, round_n=0, max_rounds=3)

    row = repo.current(plan_id)
    assert row["verification_status"] == "pending", row
    assert row["verification_stop_reason"] is None, row
    assert row["results"] is None, (
        f"reset must clear the stale terminal envelope, got {row['results']!r}"
    )
    assert row["round"] == 0
    assert row["max_rounds"] == 3

    mirror = json.loads(
        conn.execute(
            "SELECT verification FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0]
    )
    assert mirror["status"] == "pending", f"routing mirror not re-synced: {mirror}"
    assert mirror["stop_reason"] is None, mirror
    assert mirror["round"] == 0

    # The exact /progress read path that used to un-reset the row.
    assert repo.repair_stale_terminal_state(plan_id) is None, (
        "repair must be a no-op after a reset — the reset must stick"
    )
    assert repo.current(plan_id)["verification_status"] == "pending"

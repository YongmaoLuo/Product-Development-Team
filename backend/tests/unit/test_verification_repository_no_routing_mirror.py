"""Tests pinning that ``VerificationRepository`` never touches the
``plan_routing.verification`` mirror column.

Background (arch decision point 3 — "delete the mirror write paths"):
``plan_verification`` is the single authoritative home of the
verification lifecycle state.  ``plan_routing.verification`` used to
hold a *mirror* of that state, kept in sync by several writers so a
reader could get the status out of the routing row alone.  Since the
``PlanState`` getter now reads the authoritative ``plan_verification``
row directly, the mirror has no reader left — and every mirror write
is another chance for the two copies to disagree.

``VerificationRepository`` was one of the mirror writers:
``reset_round_counter`` and ``repair_stale_terminal_state`` both read
and then wrote ``plan_routing.verification``.  These tests pin that
both of them now leave the routing row alone, byte for byte, while
keeping their authoritative-row behaviour intact.

What is pinned:

  1. ``reset_round_counter`` leaves the ``plan_routing`` row untouched.
  2. ``repair_stale_terminal_state`` leaves the ``plan_routing`` row
     untouched (and still heals the authoritative row).
  3. ``reset_round_counter`` still clears the stale terminal envelope on
     the ``plan_verification`` row — the 2026-09-15 "the reset does not
     stick" defect stays fixed.
  4. A missing ``plan_routing`` row is not an error for either method.
  5. The repository module does not mention ``plan_routing`` at all.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

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
    with_routing_row: bool = True,
) -> None:
    """Seed the ``plan_verification`` row (and optionally the routing row)."""
    conn.execute(
        "INSERT INTO plan_verification "
        "(plan_id, verification_status, round, max_rounds, "
        " verification_stop_reason, results, started_at, updated_at) "
        "VALUES (?, ?, 0, 3, NULL, ?, '2026-11-04T07:00:00Z', "
        "        '2026-11-04T07:00:00Z')",
        (
            plan_id,
            verification_status,
            json.dumps(results) if results is not None else None,
        ),
    )
    if not with_routing_row:
        conn.commit()
        return
    if routing_verification is None:
        routing_verification = {
            "status": verification_status,
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
        }
    conn.execute(
        "INSERT INTO plan_routing "
        "(plan_id, current_phase, verification, completed_phases, "
        " review_rounds, flags, updated_at) "
        "VALUES (?, 'verification_running', ?, '{}', '{}', '{}', "
        "        '2026-11-04T07:00:00Z')",
        (plan_id, json.dumps(routing_verification)),
    )
    conn.commit()


def _routing_blob_bytes(conn: sqlite3.Connection, plan_id: str) -> bytes | None:
    """The raw ``plan_routing.verification`` column, as bytes.

    The column is TEXT, so "byte-for-byte unchanged" is checked on the
    UTF-8 encoding of the stored value: an equal-length but differently
    encoded blob would still show up as a difference here.
    """
    row = conn.execute(
        "SELECT verification FROM plan_routing WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    if row is None:
        return None
    raw = row[0]
    if raw is None:
        return None
    return raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)


def _routing_row(conn: sqlite3.Connection, plan_id: str) -> tuple | None:
    """The whole ``plan_routing`` row, so ``updated_at`` is covered too."""
    return conn.execute(
        "SELECT plan_id, current_phase, substage, version, completed_phases, "
        "       review_rounds, flags, verification, last_updated, updated_at "
        "FROM plan_routing WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()


def test_reset_round_counter_leaves_routing_column_alone(tmp_path):
    """``reset_round_counter`` must not read or write the routing mirror.

    The mirror write used to overwrite ``plan_routing.verification``
    with a fresh ``pending`` envelope — and bump ``updated_at`` on the
    routing row — so a caller that had deliberately written its own
    routing payload lost it.  The authoritative row is all that
    matters now: the routing row must come out of the call untouched.
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-reset-no-mirror"
    _seed_plan_row(
        conn,
        plan_id,
        verification_status="failed",
        results={
            "status": "failed",
            "stop_reason": "vp_034_timeout",
            "recorded_by": "_persist_verification_terminal",
        },
        routing_verification={
            "status": "failed",
            "round": 3,
            "max_rounds": 3,
            "stop_reason": "vp_034_timeout",
            "custom": "operator payload",
        },
    )

    before_blob = _routing_blob_bytes(conn, plan_id)
    before_row = _routing_row(conn, plan_id)
    assert before_blob is not None
    assert before_row is not None

    repo.reset_round_counter(plan_id, round_n=0, max_rounds=3)

    assert _routing_blob_bytes(conn, plan_id) == before_blob, (
        "reset_round_counter wrote the plan_routing.verification mirror"
    )
    assert _routing_row(conn, plan_id) == before_row, (
        "reset_round_counter modified the plan_routing row"
    )


def test_repair_leaves_routing_column_alone(tmp_path):
    """``repair_stale_terminal_state`` must not write the routing mirror.

    The repair still heals the authoritative row — that is its whole
    job — but the routing row is not its business any more.
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-repair-no-mirror"
    _seed_plan_row(
        conn,
        plan_id,
        verification_status="pending",
        routing_verification={
            "status": "pending",
            "round": 0,
            "max_rounds": 3,
            "stop_reason": None,
            "custom": "operator payload",
        },
        results={
            "status": "failed",
            "stop_reason": "vp_034_timeout",
            "recorded_by": "_persist_verification_terminal",
        },
    )

    before_blob = _routing_blob_bytes(conn, plan_id)
    before_row = _routing_row(conn, plan_id)

    repo.repair_stale_terminal_state(plan_id)

    assert _routing_blob_bytes(conn, plan_id) == before_blob, (
        "repair_stale_terminal_state wrote the plan_routing.verification mirror"
    )
    assert _routing_row(conn, plan_id) == before_row, (
        "repair_stale_terminal_state modified the plan_routing row"
    )

    # The authoritative repair itself still happens.
    row = repo.current(plan_id)
    assert row["verification_status"] == "failed", row
    assert row["verification_stop_reason"] == "vp_034_timeout", row


def test_reset_still_clears_repo_envelope(tmp_path):
    """The 2026-09-15 wedge stays fixed without the mirror.

    ``reset_round_counter`` used to leave the previous round's terminal
    envelope in place, so the next ``/progress`` read re-derived the
    old verdict and un-did the reset within seconds.  Clearing the
    envelope on the authoritative row is what makes the reset stick —
    the routing mirror played no part in that.
    """
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-reset-clears-envelope"
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
        routing_verification={
            "status": "failed",
            "round": 3,
            "max_rounds": 3,
            "stop_reason": "verification_log_stale",
        },
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

    # And the reset sticks: the repair pass finds no envelope to re-derive.
    assert repo.repair_stale_terminal_state(plan_id) is None, (
        "repair must be a no-op after a reset — the reset must stick"
    )
    assert repo.current(plan_id)["verification_status"] == "pending"


def test_reset_without_routing_row_does_not_raise(tmp_path):
    """No ``plan_routing`` row is not an error (and nothing is created)."""
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-reset-no-routing-row"
    _seed_plan_row(
        conn,
        plan_id,
        verification_status="failed",
        results={
            "status": "failed",
            "stop_reason": "vp_034_timeout",
            "recorded_by": "_persist_verification_terminal",
        },
        with_routing_row=False,
    )

    repo.reset_round_counter(plan_id, round_n=0, max_rounds=3)

    assert _routing_row(conn, plan_id) is None
    row = repo.current(plan_id)
    assert row["verification_status"] == "pending", row
    assert row["results"] is None, row


def test_repair_without_routing_row_does_not_raise(tmp_path):
    """No ``plan_routing`` row is not an error (and nothing is created)."""
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = _open_state_machine_db(tmp_path)
    repo = VerificationRepository(conn)
    plan_id = "test-repair-no-routing-row"
    _seed_plan_row(
        conn,
        plan_id,
        verification_status="pending",
        results={
            "status": "failed",
            "stop_reason": "vp_034_timeout",
            "recorded_by": "_persist_verification_terminal",
        },
        with_routing_row=False,
    )

    previous = repo.repair_stale_terminal_state(plan_id)

    assert previous == "pending"
    assert _routing_row(conn, plan_id) is None
    assert repo.current(plan_id)["verification_status"] == "failed"


def test_repository_module_has_no_plan_routing_reference():
    """The repository no longer knows about the mirror table at all.

    This is the interface contract the downstream gate asserts on: as
    long as ``VerificationRepository`` mentions ``plan_routing`` in its
    source, a future edit can reintroduce a mirror write.
    """
    import state_machine.repositories.verification_repository as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "plan_routing" not in source, (
        "verification_repository still references plan_routing"
    )

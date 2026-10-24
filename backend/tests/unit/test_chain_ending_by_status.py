"""Chain-ending is decided by the verification STATUS, not by a reason whitelist.

Background
----------
``_persist_verification_terminal`` decides whether a terminal verification
write really ENDS the chain — and only then does it CAS
``plan_routing.current_phase`` out of the verification family and mirror
the verdict into the phase column.  The decision used to be a whitelist
of stop reasons (``server.VERIFICATION_TERMINAL_STOP_REASONS``) plus
``status == "passed"``.

A whitelist is a losing game: every new stop reason has to be added to
it, and the one that gets forgotten strands the plan.  Two stop reasons
of the repair path were forgotten:

  * ``repair_non_zero_exit`` — the repair executor exited non-zero.
  * ``repair_batch_not_drained:<reason>:<n>_pending`` — the repair
    batch never drained.

Both terminate the round with a terminal STATUS, but neither is in the
whitelist — so ``_chain_ending`` came out False, the routing CAS and the
``current_phase`` write were skipped, and the plan stayed parked at
``current_phase='executing'`` forever.  That is the ghost state: the
repair is over, the plan still looks like it is running.

The 2026-09-20 post-mortem already reached the conclusion for the
convergence reasons: what ends the chain is the verdict (the STATUS —
``passed`` / ``failed`` / ``loop_stopped``); the reason string only
DESCRIBES the verdict, it does not classify it.  This module applies
that conclusion to every terminal status.

The reason set survives for compatibility only: a caller that already
passes ``chain_ending`` explicitly is unaffected, and a non-terminal
status carrying a terminal reason still ends the chain.

Contract pinned here
--------------------
* ``status='failed'`` + ``reason='repair_non_zero_exit'`` → the chain
  ends and ``current_phase`` moves ``executing → failed``.
* ``status='failed'`` + a segmented reason
  (``repair_batch_not_drained:...``) → the same.
* ``chain_ending=False`` passed explicitly → the chain does NOT end, even
  though the status is terminal.  The mid-loop protection is now
  expressed by that explicit flag, not by the absence of a reason.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def hermetic_server(tmp_path, monkeypatch):
    """Point ``server`` at a hermetic plans root + ``state.db``."""
    import server

    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    monkeypatch.setattr(server, "PLANS_DIR", plans_root)
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    return server, plans_root, db_path


def _seed_executing_plan(db_path: Path, plan_id: str) -> None:
    """Seed a plan parked at ``current_phase='executing'``.

    ``executing`` is the phase the repair path leaves behind when it
    dies: the repair executor took the plan out of the verification
    family, then the chain ended without ever writing the terminal
    phase back.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, substage, current_phase, version, completed_phases, "
            " review_rounds, flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, None, "executing", 0, "[]", "{}", "{}",
                json.dumps(
                    {"status": "running", "round": 1, "max_rounds": 3,
                     "stop_reason": None}
                ),
                "2026-10-23T00:00:00", "2026-10-23T00:00:00Z",
            ),
        )
        conn.commit()
        VerificationRepository(conn).insert(plan_id, "running")
    finally:
        conn.close()


def _phase(db_path: Path, plan_id: str) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?", (plan_id,)
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# 1. A terminal status ends the chain — whatever the reason says
# ---------------------------------------------------------------------------


def test_repair_non_zero_exit_ends_chain(hermetic_server):
    """``repair_non_zero_exit`` is not in the reason whitelist.

    Before the fix this verdict left ``current_phase='executing'`` on
    disk: the repair round was over, but the plan still looked like it
    was running.
    """
    server, _plans_root, db_path = hermetic_server
    plan_id = "p1"
    _seed_executing_plan(db_path, plan_id)

    server._persist_verification_terminal(
        plan_id, "failed", "repair_non_zero_exit"
    )

    assert _phase(db_path, plan_id) == "failed", (
        "a terminal 'failed' verdict must end the chain and move "
        "current_phase out of 'executing' — the repair is over, the "
        "plan must not keep looking like it is running"
    )


def test_repair_batch_not_drained_ends_chain(hermetic_server):
    """The undrained-batch reason is a SEGMENTED string.

    It is built as ``repair_batch_not_drained:<reason>:<n>_pending`` and
    can therefore never be a member of a fixed reason set — it is
    exactly the kind of reason a whitelist loses.
    """
    server, _plans_root, db_path = hermetic_server
    plan_id = "p2"
    _seed_executing_plan(db_path, plan_id)

    server._persist_verification_terminal(
        plan_id, "failed",
        "repair_batch_not_drained:repair_non_zero_exit:2_pending",
    )

    assert _phase(db_path, plan_id) == "failed", (
        "a segmented repair stop reason must not be able to strand the "
        "plan at current_phase='executing'"
    )


# ---------------------------------------------------------------------------
# 2. The mid-loop protection is the explicit flag
# ---------------------------------------------------------------------------


def test_explicit_mid_loop_flag_still_skips_phase_write(hermetic_server):
    """``chain_ending=False`` still wins over the status.

    A caller that knows the chain is NOT over (the executor subprocess
    is still mid-flight) says so explicitly; the phase write is then
    skipped even though the status is terminal.
    """
    server, _plans_root, db_path = hermetic_server
    plan_id = "p3"
    _seed_executing_plan(db_path, plan_id)

    server._persist_verification_terminal(
        plan_id, "failed", "repair_non_zero_exit", chain_ending=False,
    )

    assert _phase(db_path, plan_id) == "executing", (
        "chain_ending=False is the mid-loop guard — the phase must be "
        "left alone so the executor subprocess can still transition it"
    )

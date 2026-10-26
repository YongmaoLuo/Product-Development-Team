"""2026-10-25 (R10): the failure rollbacks write ``queued``, not ``ready``.

Three fallback paths used to park a plan on the ``ready`` phase:

* ``backend/agent.py`` — a hard-timeout task that auto-splits moves the
  plan back to the resumable-execution phase, so the orchestrator re-runs
  the new task list on the next dispatch.
* ``backend/verification_loop.py`` — the dead-end rollback CASes the
  routing row back to the resumable phase when verification stops with
  ``no_repair_tasks`` while execution work still remains.
* ``backend/verification_loop.py`` — the verification-start CAS names
  ``executing`` / ``ready`` as its source phases.

``ready`` is now an INPUT ALIAS of ``queued``: the write path must not
produce it any more (see ``backend/phase_vocabulary.py``).  A rollback
that still writes ``ready`` puts the retired word back into the routing
row, which is exactly what the vocabulary merge removed.

Contract pinned here
--------------------
* the agent's auto-split rollback targets ``queued``;
* the dead-end rollback writes ``queued`` into ``plan_routing`` and
  reports success;
* the verification-start CAS accepts ``queued`` (and no longer ``ready``)
  as a source phase.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

#: ``backend/tests/unit/test_*.py`` → repo root is three levels up.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(relative: str) -> str:
    return (_REPO_ROOT / relative).read_text(encoding="utf-8")


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


def _seed_routing_row(db_path: Path, plan_id: str, phase: str) -> None:
    """Seed a ``plan_routing`` row parked at ``phase``."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, substage, current_phase, version, completed_phases, "
            " review_rounds, flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, None, phase, 0, "[]", "{}", "{}",
                json.dumps(
                    {"status": "failed", "round": 1, "max_rounds": 3,
                     "stop_reason": "no_repair_tasks"}
                ),
                "2026-10-25T00:00:00", "2026-10-25T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _read_phase(db_path: Path, plan_id: str):
    from state_machine.db.connection import open as open_db

    conn = open_db(db_path)
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. agent.py — the auto-split rollback
# ---------------------------------------------------------------------------


def test_agent_rollback_targets_queued():
    """The hard-timeout auto-split rollback must park the plan on ``queued``.

    ``transition_to("ready")`` would still land on ``queued`` (the entry
    normalises the alias), but it would do so through the DEPRECATED
    word — and the deprecation log would fire on every auto-split.  The
    write path is supposed to speak the current vocabulary.
    """
    src = _read("backend/agent.py")

    assert 'transition_to("queued")' in src, (
        "the auto-split rollback must transition to \"queued\""
    )
    assert 'transition_to("ready")' not in src, (
        "the auto-split rollback must not write the retired \"ready\" phase"
    )


# ---------------------------------------------------------------------------
# 2. verification_loop.py — the dead-end rollback
# ---------------------------------------------------------------------------


def test_dead_end_rollback_writes_queued(hermetic_server):
    """``_rollback_dead_end_to_queued`` writes ``queued`` into the routing row.

    The plan is parked at ``verification_repairing`` (the user-gated
    phase the dead-end branch must not strand it on) and the rollback
    moves it to the resumable-execution phase — which is ``queued``
    now.
    """
    server, _plans_root, db_path = hermetic_server
    import verification_loop

    _seed_routing_row(db_path, "p1", "verification_repairing")

    assert verification_loop._rollback_dead_end_to_queued(
        "p1", 2, "no_repair_tasks",
    ) is True, "the rollback CAS should have been applied"

    assert _read_phase(db_path, "p1") == "queued"


def test_dead_end_rollback_keeps_the_old_name_gone(hermetic_server):
    """The old ``_rollback_dead_end_to_ready`` name must not survive.

    A name that says ``ready`` while the body writes ``queued`` is the
    exact drift the vocabulary merge is meant to remove.
    """
    server, _plans_root, db_path = hermetic_server
    import verification_loop

    assert not hasattr(verification_loop, "_rollback_dead_end_to_ready"), (
        "the old _rollback_dead_end_to_ready name must be gone"
    )
    assert hasattr(verification_loop, "_rollback_dead_end_to_queued"), (
        "the rollback must be reachable under its new name"
    )
    assert hasattr(server, "_rollback_dead_end_to_queued"), (
        "server.py must re-export the rollback under the same name"
    )


def test_dead_end_rollback_conflict_returns_false(hermetic_server):
    """A CAS conflict is logged and reported as ``False`` — never raised."""
    server, _plans_root, db_path = hermetic_server
    import verification_loop

    # The row sits outside the CAS source set (``verification_running`` /
    # ``verification_repairing``), so the predicate cannot match.
    _seed_routing_row(db_path, "p1", "executing")

    assert verification_loop._rollback_dead_end_to_queued(
        "p1", 2, "no_repair_tasks",
    ) is False

    assert _read_phase(db_path, "p1") == "executing", (
        "a conflicting CAS must not touch the row"
    )


# ---------------------------------------------------------------------------
# 3. verification_loop.py — the verification-start CAS source set
# ---------------------------------------------------------------------------


def _start_cas_source_phases() -> set:
    """The ``expected_phases`` of the verification-start routing CAS."""
    tree = ast.parse(_read("backend/verification_loop.py"))
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_run_auto_verification_loop_inner"
    )
    sources = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "try_mark_phase"
            and len(node.args) >= 2
        ):
            sources.append(set(ast.literal_eval(node.args[1])))
    assert sources, (
        "no try_mark_phase call found inside "
        "_run_auto_verification_loop_inner"
    )
    return set().union(*sources)


def test_start_cas_source_includes_queued():
    """The verification-start CAS must accept ``queued`` as a source phase.

    A plan rolled back by the dead-end branch now sits on ``queued``; if
    the verification-start CAS did not name it, the plan would be unable
    to enter verification and would strand there.
    """
    sources = _start_cas_source_phases()

    assert "queued" in sources, (
        f"the verification-start CAS must accept \"queued\"; got {sources}"
    )
    assert "ready" not in sources, (
        f"the verification-start CAS must not accept the retired "
        f"\"ready\"; got {sources}"
    )

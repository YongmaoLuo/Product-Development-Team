"""The death mark must accept a memory that already reached a verdict.

``_mark_verification_failed_dead`` refused to write the death terminal
state unless the in-memory ``verification_status`` said ``running``.
A round whose own verdict was ALREADY recorded in memory was therefore
never marked:

  * ``verification_failed`` — the orchestrator's own spelling for a
    failed round;
  * ``failed`` — the death mark's own spelling.

Both mean the same thing: the round is over.  Refusing them left the
plan's own row at ``running`` for ever (the divergence task 2
reconciles on the watchdog tick), left the side-channel
``.verification_runtime.json`` unwritten, and said nothing at all about
why the plan was stuck.

What this file pins:

  * a divergent memory (``verification_failed`` / ``failed``) is
    ACCEPTED — the death terminal state is written;
  * a running memory is still accepted (unchanged behaviour);
  * ``paused`` / ``passed`` / ``loop_stopped`` / an empty state are
    still refused: a live round and a settled one must not be marked
    dead by the death mark;
  * the mark never calls ``_persist_verification_terminal`` — the
    caller owns that write, and doing it here would double-write the
    row and double-fire ``plan_closed``;
  * a memory that already carries the death mark's own spelling
    (``failed``) is not written a second time: the reason on record
    stays the reason on record.

Hermeticity
------------
Everything lives inside ``tmp_path``: the plan directory, the state
database (whose ``plan_verification`` row says ``running``, so the
divergence is real rather than staged) and the runtime snapshot.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server as server_mod  # noqa: E402


#: The plan whose memory and whose row disagree.
PLAN_ID = "20261111-mark-failed-dead-divergent"

#: The reason the watchdog detected.
LOG_STALE_REASON = "verification_log_stale"

#: The reason a caller that names none gets.
THREAD_DEATH_REASON = "verification_thread_died_unexpectedly"

#: The statuses the death mark must refuse: a live round and a
#: settled one are not dead rounds.
REFUSED_STATUSES = ("paused", "passed", "loop_stopped")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_running_verification(db_path: Path, plan_id: str) -> None:
    """Seed ``plan_routing`` + ``plan_verification`` mid-verification.

    ``init_round`` inserts the verification row with
    ``verification_status = "running"`` — the value the divergence is
    measured against.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(str(db_path))
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "verification_running")
        VerificationRepository(conn).init_round(
            plan_id, round_n=1, max_rounds=3,
        )
    finally:
        conn.close()


def _db_status(db_path: Path, plan_id: str) -> Any:
    """The plan's own verification row, as the database sees it."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT verification_status FROM plan_verification "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _memory_state(
    status: str, stop_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """An in-memory verification record in the given state."""
    return {
        "plan_id": PLAN_ID,
        "verification_status": status,
        "verification_round": 2,
        "stop_reason": stop_reason,
    }


def _runtime_snapshot(plan_dir: Path) -> Dict[str, Any]:
    """The side-channel snapshot the death mark writes."""
    path = plan_dir / ".verification_runtime.json"
    assert path.exists(), "the side-channel snapshot must be written"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    """A plan whose memory and whose row disagree.

    The plan directory and the state database both live inside the
    test's own tree: the row says ``running`` (the divergence) and the
    memory is written by the test that uses this fixture.
    """
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    db_path = tmp_path / "state.db"
    plan_dir = plans_root / PLAN_ID
    plan_dir.mkdir()
    monkeypatch.setattr(server_mod, "PLANS_DIR", plans_root)
    monkeypatch.setattr(
        server_mod, "_state_db_path", lambda *a, **k: str(db_path),
    )
    _seed_running_verification(db_path, PLAN_ID)
    server_mod._verification_state.pop(PLAN_ID, None)

    yield {"plan_dir": plan_dir, "db_path": db_path}

    server_mod._verification_state.pop(PLAN_ID, None)


# ---------------------------------------------------------------------------
# 1. A divergent memory is accepted
# ---------------------------------------------------------------------------


def test_divergent_memory_state_is_accepted(plan_env):
    """A memory that already carries a verdict is still marked.

    The round's verdict is in memory; the plan's own row still says
    ``running``.  The death mark must write the death terminal state
    instead of refusing it — otherwise the row stays at ``running``
    for ever and nothing says why.
    """
    plan_dir = plan_env["plan_dir"]
    server_mod._verification_state[PLAN_ID] = _memory_state(
        "verification_failed", LOG_STALE_REASON,
    )

    server_mod._mark_verification_failed_dead(PLAN_ID, LOG_STALE_REASON)

    state = server_mod._verification_state[PLAN_ID]
    assert state["verification_status"] == "failed", (
        "a divergent memory must be marked, not refused — the row that "
        "still says 'running' is what hangs the plan"
    )
    assert state["stop_reason"] == LOG_STALE_REASON
    persisted = _runtime_snapshot(plan_dir)
    assert persisted["verification_status"] == "failed"
    assert persisted["stop_reason"] == LOG_STALE_REASON


def test_failed_memory_state_is_accepted(plan_env):
    """A memory that already says ``failed`` is still marked.

    ``failed`` is the death mark's OWN spelling: the verdict it wrote
    is on record already, so the record must not be rewritten — the
    reason that was detected is the reason on record.  The
    side-channel snapshot is still written: it is what a restarted
    server reads back, and it is what keeps a plan whose row still
    says ``running`` from hanging at "verifying" for ever.
    """
    plan_dir = plan_env["plan_dir"]
    server_mod._verification_state[PLAN_ID] = _memory_state(
        "failed", "same_failure_repeated_after_max_attempts",
    )

    server_mod._mark_verification_failed_dead(PLAN_ID, LOG_STALE_REASON)

    state = server_mod._verification_state[PLAN_ID]
    assert state["verification_status"] == "failed"
    assert state["stop_reason"] == "same_failure_repeated_after_max_attempts", (
        "an already-terminal state must not be rewritten by a later reason"
    )
    persisted = _runtime_snapshot(plan_dir)
    assert persisted["verification_status"] == "failed"
    assert persisted["stop_reason"] == "same_failure_repeated_after_max_attempts"


# ---------------------------------------------------------------------------
# 2. The running memory is still accepted
# ---------------------------------------------------------------------------


def test_running_is_still_accepted(plan_env):
    """The thread-death path is unchanged: a running memory is marked."""
    plan_dir = plan_env["plan_dir"]
    server_mod._verification_state[PLAN_ID] = _memory_state("running")

    server_mod._mark_verification_failed_dead(PLAN_ID, LOG_STALE_REASON)

    state = server_mod._verification_state[PLAN_ID]
    assert state["verification_status"] == "failed"
    assert state["stop_reason"] == LOG_STALE_REASON
    assert _runtime_snapshot(plan_dir)["verification_status"] == "failed"


def test_no_reason_falls_back_to_the_thread_death_reason(plan_env):
    """A caller that names no reason keeps the old fallback."""
    server_mod._verification_state[PLAN_ID] = _memory_state("running")

    server_mod._mark_verification_failed_dead(PLAN_ID)

    state = server_mod._verification_state[PLAN_ID]
    assert state["verification_status"] == "failed"
    assert state["stop_reason"] == THREAD_DEATH_REASON


# ---------------------------------------------------------------------------
# 3. The boundary conditions
# ---------------------------------------------------------------------------


def test_paused_and_passed_are_still_refused(plan_env):
    """A live round and a settled one are not marked dead.

    ``paused`` is a round waiting for a human, ``passed`` and
    ``loop_stopped`` are verdicts the round reached on its own.  None
    of them is a dead round, so the death mark must leave them — and
    their reasons — exactly as they are.  An empty state is not a
    round at all.
    """
    plan_dir = plan_env["plan_dir"]
    runtime_path = plan_dir / ".verification_runtime.json"

    for status in REFUSED_STATUSES:
        server_mod._verification_state[PLAN_ID] = _memory_state(
            status, "a_reason_of_its_own",
        )
        server_mod._mark_verification_failed_dead(PLAN_ID, LOG_STALE_REASON)

        state = server_mod._verification_state[PLAN_ID]
        assert state["verification_status"] == status, (
            f"a {status!r} round must not be marked dead"
        )
        assert state["stop_reason"] == "a_reason_of_its_own"
        assert not runtime_path.exists(), (
            f"a {status!r} round must not get a death snapshot"
        )

    # An empty state is not a round at all.
    server_mod._verification_state.pop(PLAN_ID, None)
    server_mod._mark_verification_failed_dead(PLAN_ID, LOG_STALE_REASON)
    assert PLAN_ID not in server_mod._verification_state
    assert not runtime_path.exists()


# ---------------------------------------------------------------------------
# 4. The function owns no terminal write
# ---------------------------------------------------------------------------


def test_no_terminal_persist_call(plan_env):
    """The mark never writes the terminal row itself.

    ``_lazy_check_verification`` — the caller — invokes
    ``_persist_verification_terminal`` on the line after this function
    returns.  Calling it here would double-write the row and
    double-fire ``plan_closed`` on the state bus.
    """
    server_mod._verification_state[PLAN_ID] = _memory_state(
        "verification_failed", LOG_STALE_REASON,
    )

    with patch.object(server_mod, "_persist_verification_terminal") as persist:
        server_mod._mark_verification_failed_dead(PLAN_ID, LOG_STALE_REASON)

    persist.assert_not_called()


# ---------------------------------------------------------------------------
# 5. The divergence is real: the row still says "running"
# ---------------------------------------------------------------------------


def test_the_row_still_says_running_while_the_memory_is_marked(plan_env):
    """The scenario these tests stand for: memory marked, row not.

    The death mark does not touch the row — that is the caller's
    write.  This test pins the premise the whole file rests on: the
    plan's own ``plan_verification`` row says ``running`` while the
    memory carries a verdict, and the mark does not change that.
    """
    db_path = plan_env["db_path"]
    server_mod._verification_state[PLAN_ID] = _memory_state(
        "verification_failed", LOG_STALE_REASON,
    )
    assert _db_status(db_path, PLAN_ID) == "running"

    server_mod._mark_verification_failed_dead(PLAN_ID, LOG_STALE_REASON)

    assert _db_status(db_path, PLAN_ID) == "running", (
        "the row belongs to the caller's terminal write, not to the mark"
    )
    assert server_mod._verification_state[PLAN_ID]["verification_status"] == (
        "failed"
    )

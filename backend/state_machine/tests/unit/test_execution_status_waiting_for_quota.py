"""Unit tests for the ``waiting_for_quota`` execution status.

Background
----------
``ExecutionRepository.update_status`` validates the status string it
is given against a fixed set of recognised execution statuses.  Until
2026-10-24 that set was::

    {"not_started", "running", "completed", "failed", "stopped"}

There was no way to persist "this execution is parked because the
provider has no quota right now".  A caller that wanted to record that
had two bad options: lie and write ``running`` (so a restart would
probe the PID, find it dead, and mark the plan ``failed`` even though
nothing had actually failed), or write ``stopped`` (which reads as
"the operator cancelled this").  Neither is the truth, and the first
one actively destroys the distinction between "crashed" and "waiting".

``waiting_for_quota`` is that missing status.  It says the execution
is alive-but-parked: nothing failed, nothing was cancelled, the run
is waiting on the provider.

The contract pinned by these tests:

  * ``update_status(plan_id, "waiting_for_quota")`` is accepted and
    persisted to the row's ``exec_status`` column, so
    ``summary(plan_id)["exec_status"]`` reads it back.
  * An unknown status still raises ``ValueError`` — the validator is
    widened by exactly one value, not opened up.
  * Writing the same status twice is idempotent.
  * ``update_status`` writes ONLY ``plan_execution.exec_status``.  It
    does not stamp a workflow phase; the phase belongs to
    ``plan_routing`` and this method has not touched it since
    2026-09-19.
  * ``_recover_execution_states`` restores the status verbatim.  That
    function only probes the PID for ``running`` entries, so a
    ``waiting_for_quota`` row comes back as ``waiting_for_quota``
    rather than being mistaken for a dead run.

The last one is the whole point: a server restart must not turn a
quota-parked execution into a failed one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a fresh, migrated SQLite connection (autocommit mode)."""
    db_path = tmp_path / "state.db"
    connection = open_db(db_path)
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> ExecutionRepository:
    """Yield an ``ExecutionRepository`` bound to the test connection."""
    return ExecutionRepository(conn)


# ---------------------------------------------------------------------------
# The status itself
# ---------------------------------------------------------------------------


def test_waiting_for_quota_is_accepted(
    conn: sqlite3.Connection,
    repo: ExecutionRepository,
) -> None:
    """``waiting_for_quota`` persists to ``exec_status`` and reads back.

    The write must also stay inside ``plan_execution``: this method
    is a status writer, not a phase writer.  A ``plan_routing`` row
    for the same plan must not appear (or change) as a side effect.
    """
    repo.insert(plan_id="p1")

    repo.update_status("p1", "waiting_for_quota")

    assert repo.summary("p1")["exec_status"] == "waiting_for_quota"

    # The write did not stamp a workflow phase anywhere else.
    routing_row = conn.execute(  # noqa: F821 - fixture-provided connection
        "SELECT current_phase FROM plan_routing WHERE plan_id = ?", ("p1",)
    ).fetchone()
    assert routing_row is None


def test_unknown_status_still_rejected(repo: ExecutionRepository) -> None:
    """A status outside the recognised set still raises ``ValueError``.

    Widening the set by one value must not turn the validator into a
    no-op: the row is left untouched when the write is refused.
    """
    repo.insert(plan_id="p1")

    with pytest.raises(ValueError) as excinfo:
        repo.update_status("p1", "definitely_not_a_status")

    assert "definitely_not_a_status" in str(excinfo.value)
    assert repo.summary("p1")["exec_status"] is None


def test_idempotent_rewrite(
    conn: sqlite3.Connection,
    repo: ExecutionRepository,
) -> None:
    """Writing ``waiting_for_quota`` twice leaves one row with one value."""
    repo.insert(plan_id="p1")

    repo.update_status("p1", "waiting_for_quota")
    first = repo.summary("p1")
    repo.update_status("p1", "waiting_for_quota")
    second = repo.summary("p1")

    assert first["exec_status"] == "waiting_for_quota"
    assert second["exec_status"] == "waiting_for_quota"
    assert first["exec_status"] == second["exec_status"]

    # Still exactly one row — the second write updated it, it did not
    # insert a sibling.
    row_count = conn.execute(  # noqa: F821 - fixture-provided connection
        "SELECT COUNT(*) FROM plan_execution WHERE plan_id = ?", ("p1",)
    ).fetchone()[0]
    assert row_count == 1


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


def test_recovery_keeps_the_status(tmp_path: Path) -> None:
    """A restart restores ``waiting_for_quota`` verbatim.

    ``_recover_execution_states`` only probes the PID for ``running``
    rows; every other status is copied into the in-memory state as
    it stands.  That is the property that keeps a quota-parked
    execution from being reported as failed after a restart.
    """
    import server  # imported lazily: the 8000 socket guard is installed
    from server import _recover_execution_states
    from server import _state_db_path

    plan_id = "p-waiting-for-quota"
    plans_dir = tmp_path / "plans"
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True)

    # Seed the row in the SAME database the server resolves to, so the
    # recovery scan reads what this test wrote.
    db_conn = open_db(_state_db_path())
    try:
        migrate(db_conn)
        db_repo = ExecutionRepository(db_conn)
        db_repo.update_fields(
            plan_id,
            create_if_missing=True,
            exec_status="running",
            started_at="2026-10-24T00:00:00",
        )
        db_repo.update_status(plan_id, "waiting_for_quota")
    finally:
        db_conn.close()

    server._execution_state.clear()
    try:
        _recover_execution_states(plans_dir)

        assert server._execution_state[plan_id]["status"] == "waiting_for_quota"
    finally:
        server._execution_state.pop(plan_id, None)

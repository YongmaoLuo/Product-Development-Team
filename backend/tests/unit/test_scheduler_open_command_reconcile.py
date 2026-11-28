"""Unit tests: the startup reconciliation of the commands left open.

Background
----------
A command the operator issued is written down **the moment the worker
takes it out of the mailbox**, with the outcome it has *so far* --
``accepted`` -- and it is turned into ``done`` or ``failed`` only when
the worker has answered.  The read model counts an ``accepted`` command
as *"stopping"*: the plan is in the gap between "asked to stop" and
"stopped".

A process that is killed between the two moments leaves the row behind
for ever.  The command is not in the mailbox any more -- it was taken --
and no worker is left to answer it, so nothing ever writes the final
outcome.  The panel then shows a plan that finished days ago as still
"stopping", which is a thing that cannot be true.

So the engine reconciles the command log at startup: every command the
dead process left open is **failed**, with a detail that says why, and a
warning is logged for each of them.

What this file asserts
----------------------
1. **An open command is failed at startup** -- and the detail says the
   scheduler restarted before carrying it out.
2. **A finished command is left alone** -- the reconciliation only
   touches the rows that are still pending.
3. **The reconciliation is idempotent** -- a second recovery changes
   nothing, and nothing is left open.
4. **The reconciliation is loud** -- each command it failed is logged as
   a warning that names the plan, the action and the command.

The commands are written through the **store's own** writing interface
and read back through its reading one, against a real SQLite database
with the production schema: what is asserted here is what is in the
table, not what a double was told to expect.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from plan_scheduler.engine import RESTARTED_COMMAND_DETAIL
from plan_scheduler.store import PENDING_OUTCOME, SqliteSchedulerStore


#: The plan whose command was left open by the process that died.
STUCK_PLAN = "plan-stuck"

#: The plan whose command was answered before the process died.
ANSWERED_PLAN = "plan-answered"


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


class _EmptyFacts:
    """A facts reader that says no plan is working.

    The recovery asks the facts who is working, so that the slots can
    be reconciled with them.  This file is about the command log, and
    the plans it mentions are not working -- they finished long ago.
    """

    def active_plans(self) -> List[Dict[str, Any]]:
        """No plan is working."""
        return []


class _ConfigLoader:
    """A loader that hands out one immutable snapshot."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def current(self) -> Any:
        """The configuration snapshot, unchanged."""
        return self._config


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection, closed when the test is over."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    connection = open_db(tmp_path / "state.db")
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


def _store() -> SqliteSchedulerStore:
    """The scheduler's own store."""
    return SqliteSchedulerStore()


def _engine(conn: sqlite3.Connection) -> Any:
    """Build an engine on a real store and a real connection.

    The engine is the production one: the recovery it runs at startup
    is the recovery under test.
    """
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.engine import PlanSchedulerEngine

    engine = PlanSchedulerEngine(
        config_loader=_ConfigLoader(SchedulerConfig()),
        store=_store(),
        facts=_EmptyFacts(),
        ledger=object(),
        commands=object(),
        sink=object(),
        conn=conn,
    )
    engine._config = engine._read_config()
    return engine


def _write_command(
    conn: sqlite3.Connection,
    plan_id: str,
    action: str,
    outcome: Optional[str] = None,
    created_at: str = "2026-11-23T06:55:56Z",
) -> int:
    """Write a command into the log, the way the worker writes it.

    ``outcome`` of ``None`` means the command is still **accepted** --
    the row the dead process left behind.

    Args:
        conn: the connection to write on.
        plan_id: the plan the command is about.
        action: what was asked for.
        outcome: the outcome to record; ``accepted`` when omitted.
        created_at: when the command was taken.

    Returns:
        The id of the command that was written.
    """
    command_id = _store().record_command(
        conn,
        plan_id,
        action,
        outcome=outcome or PENDING_OUTCOME,
        now=created_at,
    )
    if outcome is not None:
        _store().finish_command(conn, command_id, outcome, detail="done")
    return command_id


def _read_command(conn: sqlite3.Connection, command_id: int) -> Dict[str, Any]:
    """Read one command back out of the log."""
    row = _store().get_command(conn, command_id)
    assert row is not None
    return dict(row)


# ---------------------------------------------------------------------------
# 1. An open command is failed at startup
# ---------------------------------------------------------------------------


def test_startup_fails_the_command_the_dead_process_left_open(
    conn: sqlite3.Connection,
) -> None:
    """The command the previous process left open is failed.

    The row was written when the worker took the command out of the
    mailbox, and the process died before the worker answered it.  The
    restart is the moment the scheduler can say so: the command is
    failed, and the detail says why.
    """
    stuck_id = _write_command(conn, STUCK_PLAN, "start")

    engine = _engine(conn)
    engine._recover()

    row = _read_command(conn, stuck_id)
    assert row["outcome"] == "failed"
    assert RESTARTED_COMMAND_DETAIL in (row["detail"] or "")
    assert row["detail"] == (
        "the scheduler restarted before carrying this command out"
    )


def test_the_startup_reconciliation_leaves_no_open_command(
    conn: sqlite3.Connection,
) -> None:
    """Nothing is left open after the startup.

    The open commands are what the panel shows as "stopping", so a
    command that is still open after the startup is still stopping.
    The reconciliation is what empties the list.
    """
    _write_command(conn, STUCK_PLAN, "start")

    engine = _engine(conn)
    engine._recover()

    assert _store().list_open_commands(conn) == []


# ---------------------------------------------------------------------------
# 2. A finished command is left alone
# ---------------------------------------------------------------------------


def test_a_finished_command_is_not_touched_by_the_startup(
    conn: sqlite3.Connection,
) -> None:
    """A command that was answered is still answered.

    The reconciliation finishes what is **open**; a command that was
    carried out before the process died is a fact about a plan that is
    already done, and rewriting it would rewrite a fact that was never
    in doubt.
    """
    answered_id = _write_command(conn, ANSWERED_PLAN, "pause", outcome="done")
    before = _read_command(conn, answered_id)

    engine = _engine(conn)
    engine._recover()

    after = _read_command(conn, answered_id)
    assert after == before
    assert after["outcome"] == "done"


# ---------------------------------------------------------------------------
# 3. The reconciliation is idempotent
# ---------------------------------------------------------------------------


def test_the_startup_reconciliation_can_run_twice(
    conn: sqlite3.Connection,
) -> None:
    """A second recovery changes nothing.

    The recovery runs more than once over the life of a process -- the
    worker recovers when it starts, and again when it binds itself --
    so it has to be harmless the second time.
    """
    stuck_id = _write_command(conn, STUCK_PLAN, "start")

    engine = _engine(conn)
    engine._recover()
    first = _read_command(conn, stuck_id)

    engine._recover()
    second = _read_command(conn, stuck_id)

    assert first == second
    assert second["outcome"] == "failed"


# ---------------------------------------------------------------------------
# 4. The reconciliation is loud
# ---------------------------------------------------------------------------


def test_the_startup_logs_a_warning_for_each_command_it_failed(
    conn: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each failed command is logged as a warning.

    A command that was lost is a thing the operator has to be able to
    find in the log -- the plan, the action and the command's id are
    all named, so the log entry can be looked up.
    """
    stuck_id = _write_command(conn, STUCK_PLAN, "start")

    engine = _engine(conn)
    with caplog.at_level(logging.WARNING, logger="plan_scheduler.engine"):
        engine._recover()

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "restarted" in record.getMessage()
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert STUCK_PLAN in message
    assert "start" in message
    assert str(stuck_id) in message

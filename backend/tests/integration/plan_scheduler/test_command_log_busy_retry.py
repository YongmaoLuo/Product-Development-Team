"""Integration tests: the command log survives a transient lock.

Background
----------
``scheduler_command`` is the scheduler's own record of what the operator
asked for.  A command that is missing from it is a command that never
happened: the caller is answered with the id of a row that is not
there, the panel shows nothing, and the operator's "pause" leaves no
trace it could be checked against.

The write goes through ``BEGIN IMMEDIATE``
(:meth:`plan_scheduler.store.SqliteSchedulerStore.transaction`), which
takes the write lock *up front*.  When another writer holds that lock
for longer than SQLite's ``busy_timeout``, the write fails with
``OperationalError: database is locked`` — and the engine's
``_record_command`` swallows that error on purpose: the command is still
carried out, it simply has no id.  So the *store* is the last place
where the lock can be waited out, and if it does not wait, the command
is lost for good.

What these cases pin
--------------------
**A transient lock is waited out.**  One lock on the write does not lose
the command: the store retries, the row is there, and the id the caller
was given names that row.

**The retry is a retry of the *whole transaction*.**  A retry that only
repeated the INSERT would leave the first transaction open and half
written.  Each attempt opens its own ``BEGIN IMMEDIATE``, and a failed
attempt leaves nothing behind — so the table ends up with exactly one
row, not two.

**Only locks are retried.**  A missing table is not going to appear
because the store waited; that error is raised at once, after one
attempt, so the caller learns about it immediately.

**A lock that never clears is raised.**  The store does not swallow it
and does not change its meaning: the engine's own handling of a write
that cannot be made stays where it is.

How the lock is produced
------------------------
The lock is a *scripted* one: a connection wrapper that raises
``OperationalError: database is locked`` on the statement the case
names, and behaves like the connection it wraps afterwards.  Nothing is
timed, no second process is started and no ``busy_timeout`` is waited
out — what is under test is what the store does with the error, and
that has to be the same on every machine.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, List, Optional

import pytest

from plan_scheduler.store import SqliteSchedulerStore

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the production PRAGMA contract."""
    from state_machine.db.connection import open as open_db

    return open_db(db_path)


def _open_migrated(db_path: Path) -> sqlite3.Connection:
    """Open a connection to a database that has the scheduler tables."""
    from state_machine.db.schema import migrate

    conn = _connect(db_path)
    migrate(conn)
    return conn


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """The path of the throwaway state database the tests write to."""
    return tmp_path / "state.db"


@pytest.fixture()
def conn(db_path: Path):
    """A migrated connection, closed when the test is over."""
    connection = _open_migrated(db_path)
    try:
        yield connection
    finally:
        connection.close()


class _ScriptedConnection:
    """A connection that fails a chosen statement, then behaves normally.

    The failure is a *connection-level* event, not a store-level one:
    the store sees an :class:`sqlite3.OperationalError` come out of
    ``execute`` and has to decide what to do with it.  Every statement
    is recorded, so a case can assert how many transactions were opened
    — which is how "the retry was a retry of the whole transaction" is
    told apart from "the statement was repeated inside one transaction".
    """

    def __init__(
        self,
        inner: sqlite3.Connection,
        *,
        fail_on: Optional[str] = None,
        failures: int = 0,
        error: Optional[BaseException] = None,
    ) -> None:
        self._inner = inner
        self._fail_on = fail_on
        self._failures = failures
        self._error = error
        self.statements: List[str] = []

    @property
    def begins(self) -> int:
        """How many transactions were opened on this connection."""
        return sum(
            1
            for sql in self.statements
            if sql.strip().upper().startswith("BEGIN")
        )

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        """Run a statement, failing it while the script says so."""
        self.statements.append(sql)
        if (
            self._failures > 0
            and self._fail_on is not None
            and self._fail_on in sql
        ):
            self._failures -= 1
            if self._error is not None:
                raise self._error
            raise sqlite3.OperationalError("database is locked")
        return self._inner.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Everything else is the connection this one wraps."""
        return getattr(self._inner, name)


def _command_rows(conn: sqlite3.Connection) -> List[tuple]:
    """Every row of the command log, in the order it was written."""
    return conn.execute(
        "SELECT command_id, plan_id, action, outcome, detail "
        "FROM scheduler_command ORDER BY command_id"
    ).fetchall()


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_record_command_survives_one_lock(conn: sqlite3.Connection) -> None:
    """One lock on the write does not lose the command.

    The lock arrives on the INSERT, inside the transaction.  The store
    waits it out and tries the whole transaction again, so the caller is
    given an id and the row is really there — exactly once, because the
    failed attempt was rolled back and left nothing behind.
    """
    store = SqliteSchedulerStore()
    locked = _ScriptedConnection(conn, fail_on="INSERT INTO scheduler_command", failures=1)

    command_id = store.record_command(
        locked, "p1", "pause", outcome="accepted", detail="paused"
    )

    assert isinstance(command_id, int)
    # Two transactions: the one that hit the lock and the one that
    # worked.  A single BEGIN with a repeated INSERT would mean the
    # retry happened *inside* the transaction, which is not the
    # contract.
    assert locked.begins == 2

    rows = _command_rows(conn)
    assert len(rows) == 1, "the failed attempt left a row behind"
    row = rows[0]
    assert int(row[0]) == command_id
    assert row[1] == "p1"
    assert row[2] == "pause"
    assert row[3] == "accepted"
    assert row[4] == "paused"

    # The id the caller was given names a row that is really there.
    recorded = store.get_command(conn, command_id)
    assert recorded is not None
    assert recorded["plan_id"] == "p1"
    assert recorded["action"] == "pause"


def test_finish_command_survives_one_lock(conn: sqlite3.Connection) -> None:
    """One lock on the outcome write does not lose the outcome.

    The outcome is written the same way the command was: inside one
    ``BEGIN IMMEDIATE``.  A lock there is waited out the same way, and
    the command ends up finished exactly once.
    """
    store = SqliteSchedulerStore()
    command_id = store.record_command(conn, "p1", "pause")

    locked = _ScriptedConnection(conn, fail_on="UPDATE scheduler_command", failures=1)
    finished = store.finish_command(locked, command_id, outcome="done", detail="paused")

    assert finished is True
    assert locked.begins == 2

    rows = _command_rows(conn)
    assert len(rows) == 1
    assert rows[0][3] == "done"
    assert rows[0][4] == "paused"


def test_non_locked_error_not_retried(db_path: Path) -> None:
    """A missing table is raised at once — waiting cannot fix it.

    The database here never had the scheduler tables created, so the
    write fails with "no such table".  That is not a lock, and the
    store must not turn it into one: the error is raised after a single
    attempt, so the caller hears about the real problem immediately
    instead of after four rounds of waiting.
    """
    store = SqliteSchedulerStore()
    connection = _connect(db_path)
    try:
        counted = _ScriptedConnection(connection)

        with pytest.raises(sqlite3.OperationalError) as caught:
            store.record_command(counted, "p1", "pause")

        assert "no such table" in str(caught.value)
        assert counted.begins == 1, "a non-lock error was retried"
    finally:
        connection.close()


def test_lock_that_never_clears_is_raised(conn: sqlite3.Connection) -> None:
    """A lock that outlives the retries is raised, not swallowed.

    The store does not decide what a write that could not be made
    means — that is the engine's business, and the engine already
    handles it.  So the error travels up with its meaning intact.
    """
    store = SqliteSchedulerStore()
    locked = _ScriptedConnection(
        conn, fail_on="INSERT INTO scheduler_command", failures=99
    )

    with pytest.raises(sqlite3.OperationalError) as caught:
        store.record_command(locked, "p1", "pause")

    assert "database is locked" in str(caught.value)
    assert _command_rows(conn) == []


def test_two_commands_with_the_same_action_are_two_rows(
    conn: sqlite3.Connection,
) -> None:
    """The retry does not make the command log idempotent.

    Two commands about the same plan and the same action are two
    commands — the log records what was asked for, every time it was
    asked for.  A retry that collapsed them would change what the log
    means.
    """
    store = SqliteSchedulerStore()

    first = store.record_command(conn, "p1", "pause")
    second = store.record_command(conn, "p1", "pause")

    assert first != second
    rows = _command_rows(conn)
    assert len(rows) == 2
    assert [row[0] for row in rows] == [first, second]
    assert [row[2] for row in rows] == ["pause", "pause"]

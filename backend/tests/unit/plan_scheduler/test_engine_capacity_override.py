"""The engine reads the capacity override on every call.

Background
----------
The scheduler's capacity is configured in ``backend/config.yaml`` — a
file that is tracked by git and carries the comments that explain it,
so it may not be rewritten to change a knob.  An operator who wants
the scheduler to run three plans at a time instead of six needs that
number to change *while the scheduler runs*, so the override is a
**row** in the state database (see ``test_scheduler_setting_store.py``)
and the engine reads that row every time it needs a capacity.

The word *every* is the whole contract.  A value cached in the
configuration snapshot would only take effect after a restart, and the
point of the override is that it takes effect now.

These cases pin:

* an override that is set wins over the configured capacity;
* deleting it puts the configured capacity back;
* a value that is not a usable capacity is ignored, and the configured
  capacity stays in charge — a negative number included, while zero is
  a legitimate answer (it pauses the scheduling);
* the row is re-read on every call, so a change lands at once;
* a store that cannot answer is recorded and does not take the tick
  down with it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ConfigLoader:
    """A loader that hands out one immutable snapshot."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def current(self) -> Any:
        """The configuration snapshot, unchanged."""
        return self._config


class _BrokenStore:
    """A store whose settings cannot be read."""

    def get_setting(
        self, conn: sqlite3.Connection, key: str
    ) -> Optional[str]:
        """Fail the way a missing table would."""
        raise sqlite3.OperationalError("no such table: scheduler_setting")


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


def _engine(
    conn: sqlite3.Connection,
    store: Any,
    *,
    capacity: Any = 6,
) -> Any:
    """Build an engine whose configured capacity is ``capacity``.

    The collaborators the capacity does not read — the facts, the
    ledger, the command executor, the sink — are placeholders: this
    file is about one number and where it comes from.
    """
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.engine import PlanSchedulerEngine

    engine = PlanSchedulerEngine(
        config_loader=_ConfigLoader(SchedulerConfig(capacity=capacity)),
        store=store,
        facts=object(),
        ledger=object(),
        commands=object(),
        sink=object(),
        conn=conn,
    )
    engine._config = engine._read_config()
    return engine


def _store() -> Any:
    """The scheduler's own store."""
    from plan_scheduler.store import SqliteSchedulerStore

    return SqliteSchedulerStore()


def _write(conn: sqlite3.Connection, value: str) -> None:
    """Write the capacity override."""
    from plan_scheduler.store import CAPACITY_OVERRIDE_KEY

    _store().set_setting(
        conn, CAPACITY_OVERRIDE_KEY, value, stamp="2026-10-29T21:00:00"
    )


def _delete(conn: sqlite3.Connection) -> None:
    """Remove the capacity override."""
    from plan_scheduler.store import CAPACITY_OVERRIDE_KEY

    _store().delete_setting(conn, CAPACITY_OVERRIDE_KEY)


# ---------------------------------------------------------------------------
# 1. An override that is set wins over the configured capacity
# ---------------------------------------------------------------------------


def test_capacity_uses_override(conn: sqlite3.Connection) -> None:
    """The override is the capacity while it is there.

    The configured capacity is six; the operator writes three; three
    is what the scheduler must run with — not six, and not the
    snapshot the engine loaded when it started.
    """
    engine = _engine(conn, _store())

    assert engine._config_value("capacity", 6) == 6, (
        "the configured capacity is six"
    )

    _write(conn, "3")

    assert engine._capacity() == 3


def test_capacity_override_is_none_when_unset(conn: sqlite3.Connection) -> None:
    """No override says ``None``, not zero.

    ``None`` and ``0`` are different answers: zero is a capacity the
    operator chose, and ``None`` means there is no override at all.
    The caller that wants to show *where* the number came from reads
    this answer.
    """
    engine = _engine(conn, _store())

    assert engine._capacity_override() is None


def test_capacity_override_is_the_number_that_was_written(
    conn: sqlite3.Connection,
) -> None:
    """The override answers with the number stored in the row."""
    engine = _engine(conn, _store())

    _write(conn, "3")

    assert engine._capacity_override() == 3


# ---------------------------------------------------------------------------
# 2. Deleting the override puts the configured capacity back
# ---------------------------------------------------------------------------


def test_capacity_falls_back_after_delete(conn: sqlite3.Connection) -> None:
    """Removing the row hands the job back to the configuration file.

    There is no "the capacity is three" state left after the delete —
    the row is gone, so the configured capacity is the only answer
    there is.
    """
    engine = _engine(conn, _store())
    _write(conn, "3")
    assert engine._capacity() == 3

    _delete(conn)

    assert engine._capacity() == 6


# ---------------------------------------------------------------------------
# 3. A value that is not a usable capacity is ignored
# ---------------------------------------------------------------------------


def test_capacity_ignores_invalid_override(conn: sqlite3.Connection) -> None:
    """Junk in the row leaves the configured capacity in charge.

    A row that says ``abc`` is not a capacity the scheduler may run
    with, and a row that says ``-1`` is not a number of plans either.
    Neither is an error the operator has to clear by hand: the
    override is ignored and the configured capacity stands.
    """
    engine = _engine(conn, _store())

    for junk in ("abc", "-1"):
        _write(conn, junk)

        assert engine._capacity() == 6, f"{junk!r} must not become the capacity"


def test_capacity_zero_is_a_valid_override(conn: sqlite3.Connection) -> None:
    """Zero is a capacity value, not junk.

    Zero is how the operator pauses the scheduling: the plans stay in
    the line and none of them starts.  So the row is honoured as it
    is, and the configured capacity does not creep back in.
    """
    engine = _engine(conn, _store())

    _write(conn, "0")

    assert engine._capacity() == 0


def test_capacity_falls_back_to_default_when_config_is_junk(
    conn: sqlite3.Connection,
) -> None:
    """A configuration that cannot be used falls back to the default.

    The override may be absent here, so the answer comes from the
    configuration — and a configuration that says something that is
    not a number has no capacity to give.  The default stands.
    """
    from plan_scheduler.config import DEFAULT_CAPACITY

    engine = _engine(conn, _store(), capacity="not a number")

    assert engine._capacity() == DEFAULT_CAPACITY


# ---------------------------------------------------------------------------
# 4. The row is read again on every call
# ---------------------------------------------------------------------------


def test_capacity_rereads_each_call(conn: sqlite3.Connection) -> None:
    """A change lands at once, without a restart.

    The engine holds a configuration snapshot, and it keeps it — that
    snapshot is what the file said when it was read.  The override is
    not part of that snapshot: it is a row that an operator may write
    at any moment, so it is read again every time a capacity is
    needed.  A value that was cached would only take effect after the
    next restart, which is exactly what the override is not for.
    """
    engine = _engine(conn, _store())

    _write(conn, "3")
    assert engine._capacity() == 3

    _write(conn, "8")
    assert engine._capacity() == 8


# ---------------------------------------------------------------------------
# 5. A store that cannot answer does not take the tick down
# ---------------------------------------------------------------------------


def test_capacity_survives_a_failing_store(conn: sqlite3.Connection) -> None:
    """A store that raises is recorded, and the capacity still answers.

    The tick reads the override on its way through, and a failure
    there — a table that is not there, a database that is locked —
    must not stop the tick from running.  The failure is recorded
    under the phase it happened in, and the configured capacity stands.
    """
    engine = _engine(conn, _BrokenStore())

    assert engine._capacity() == 6

    phases = [phase for phase, _ in engine.errors]
    assert phases == ["capacity"], (
        f"the failure belongs to the capacity phase: {engine.errors!r}"
    )

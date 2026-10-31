"""The active plans carry the moment the plan really started.

Background
----------
The scheduler's "已运行(秒)" is a slot clock, not a clock:
``_recover_slots()`` deletes and re-inserts every slot on each
restart, so the number the operator sees starts again from zero
whenever the scheduler does — it says how long the *scheduler* has
held the slot, not how long the *plan* has been running.

The plan's real start time is written down already:
``plan_execution.started_at`` — the naive local-time ISO stamp written
when the execution started.  What was missing was a way to *read* it:
``PlanFactsReader.active_plans()`` selected the pid, the status, the
directory and the next run time, but not the start time, so every
consumer of the active plans had to guess how long the plan had been
at it.

These cases pin the reading:

* an active plan carries the ``started_at`` its execution row holds,
  unchanged — the stamp is passed through, never parsed here;
* an execution row without a start time says ``None`` rather than
  raising — the reader has no opinion about a plan it cannot date;
* a plan that is only active on the verification side has no execution
  row, and therefore no start time either.

Why a real database
-------------------
The claim being pinned is a claim about a *column*: the value that
sits in ``plan_execution.started_at`` has to arrive in the active
row.  A stand-in connection could hold the same tuples and still not
pin the SQL — the column the derivation actually reads.  The database
here is the production schema, created by the production migration.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reader(conn: sqlite3.Connection, project_root: Path) -> Any:
    """A facts reader over ``conn``.

    The import happens inside the helper so that a missing module is
    reported as the failure of the case that needed it, rather than as
    a collection error that hides every other case in this file.
    """
    from plan_scheduler.facts import PlanFactsReader

    return PlanFactsReader(conn, project_root)


def _now() -> str:
    """The current UTC time, as the state database writes it."""
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """A state database on the production schema."""
    from state_machine.db.schema import migrate

    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        migrate(connection)
        yield connection
    finally:
        connection.close()


def _execution_row(
    conn: sqlite3.Connection,
    plan_id: str,
    started_at: Optional[str] = None,
    exec_pid: Optional[int] = None,
    exec_status: str = "running",
) -> None:
    """Write the plan's execution row: the subprocess and its start time."""
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, exec_pid, exec_status, started_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (plan_id, exec_pid, exec_status, started_at, _now()),
    )


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_active_plan_carries_started_at(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """An active plan carries the start time its execution row holds.

    The stamp is passed through unchanged — this reader does not parse
    it and does not round it: what the row says is what the plan was
    told, microseconds and all.
    """
    started_at = "2026-10-29T20:04:26.016813"
    _execution_row(
        conn, "p-1", started_at=started_at, exec_pid=os.getpid()
    )

    active = _reader(conn, tmp_path / "plans").active_plans()

    assert active == [
        {
            "plan_id": "p-1",
            "side": "execution",
            "workspace": None,
            "started_at": started_at,
        }
    ]


def test_active_plan_without_started_at_is_none(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """An execution row without a start time says ``None``.

    A row that has never been dated is not an error — the reader
    answers "I don't know", and the caller decides what to do with
    that.  Raising here would take the whole scheduler down over a
    column that is allowed to be empty.
    """
    _execution_row(conn, "p-1", started_at=None, exec_pid=os.getpid())

    active = _reader(conn, tmp_path / "plans").active_plans()

    assert [entry["plan_id"] for entry in active] == ["p-1"]
    assert active[0]["started_at"] is None


def test_active_plan_with_empty_started_at_is_none(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """An execution row whose start time is blank says ``None``.

    Blank is not a timestamp: there is nothing to pass through, so the
    answer is the same one a missing value gets.
    """
    _execution_row(conn, "p-1", started_at="", exec_pid=os.getpid())

    active = _reader(conn, tmp_path / "plans").active_plans()

    assert [entry["plan_id"] for entry in active] == ["p-1"]
    assert active[0]["started_at"] is None


def test_verification_side_plan_has_no_started_at(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A plan that is only verifying has no execution row, and no start.

    A verification round runs in a thread, not in a subprocess: there
    is no ``plan_execution`` row to date, so there is no start time to
    report.  The rest of the row — the plan id, the side it is active
    on — is unchanged.
    """
    conn.execute(
        "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
        "VALUES (?, ?, ?)",
        ("p-ver", "verification_running", _now()),
    )
    conn.execute(
        "INSERT INTO plan_verification "
        "(plan_id, verification_status, round, max_rounds, updated_at) "
        "VALUES (?, 'running', 0, 3, ?)",
        ("p-ver", _now()),
    )

    active = _reader(conn, tmp_path / "plans").active_plans()

    assert [entry["plan_id"] for entry in active] == ["p-ver"]
    assert active[0]["side"] == "verification"
    assert active[0]["started_at"] is None
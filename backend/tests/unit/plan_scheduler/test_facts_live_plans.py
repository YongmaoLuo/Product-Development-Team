"""The active plans are the union of the database's facts and the live set.

Background
----------
``PlanFactsReader.active_plans()`` answers "which plans are holding
compute?" from the rows in the state database: a running execution
subprocess, or a running verification round.

The verification side has a freshness rule: a ``plan_verification`` row
that has not been updated for fifteen minutes belongs to a thread that
died, and a dead thread holds nothing.  That rule is right for a row
that is written by a beat — but a verification round started directly
by ``start_all`` may run longer than that without touching
``updated_at``, and when it does, the plan *vanishes* from the active
set.  The scheduler then believes a slot is free that is not free, and
admits another plan on top of it.

The fix is not a longer timeout.  A timeout is still a guess about how
long a live round may be quiet, and the guess has to be wrong in the
safe direction every time it is wrong.  The fix is to ask the thing
that actually knows — the process and the loop that are alive — and to
take the **union** of that answer with the database's, so the two
sources never subtract from each other.

The live set is *injected* rather than derived here: the reader is
given a callback that answers "which plan ids are alive right now?",
and it asks the callback on every call.  The reader therefore keeps
its promise of holding no state about the plans it reports — the
callback is asked every time, exactly like the rows are read every
time.

These cases pin the union:

* a plan the callback names and the database does not know about is
  active, with the verification side;
* a reader without a callback answers exactly what it answered before
  the callback existed;
* a callback that raises does not take the scheduler down — the
  database's facts are the answer, and the failure is a warning;
* a plan that both sources name appears **once**.

Why a real database
-------------------
The claim being pinned is a claim about what the reader does with the
rows it reads, so the rows have to be real ones — the production
schema, created by the production migration.  A stand-in connection
could hold the same tuples and still not pin the SQL the derivation
reads.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reader(conn: sqlite3.Connection, project_root: Path, **kwargs: Any) -> Any:
    """A facts reader over ``conn``.

    The import happens inside the helper so that a missing module is
    reported as the failure of the case that needed it, rather than as
    a collection error that hides every other case in this file.
    """
    from plan_scheduler.facts import PlanFactsReader

    return PlanFactsReader(conn, project_root, **kwargs)


def _now() -> str:
    """The current UTC time, as the state database writes it."""
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _clock() -> float:
    """A fixed "now", so the freshness rule cannot depend on the wall."""
    return datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()


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


def test_live_plan_union(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A plan the callback names is active even when the database forgets it.

    The database knows about one working plan; the callback names
    another one — a verification round that is alive but has not
    refreshed its row.  Both are working, so both are reported, and the
    plan that only the callback knows about is reported on the
    verification side: it holds compute, but no subprocess holds it.
    """
    _execution_row(
        conn,
        "p-db",
        started_at="2026-11-01T10:00:00.000000",
        exec_pid=os.getpid(),
    )

    reader = _reader(
        conn,
        tmp_path / "plans",
        clock=_clock,
        live_plan_ids=lambda: {"p-live"},
    )

    active = reader.active_plans()

    assert [entry["plan_id"] for entry in active] == ["p-db", "p-live"]
    assert active[1] == {
        "plan_id": "p-live",
        "side": "verification",
        "workspace": None,
        "phase": None,
        "started_at": None,
    }


def test_no_callback_is_unchanged(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A reader with no callback answers exactly what it answered before.

    The callback is an addition, not a change of behaviour: a reader
    that is not given one must answer the database's facts and nothing
    else.  Passing ``live_plan_ids=None`` explicitly is the same
    reader as not passing it at all.
    """
    _execution_row(
        conn,
        "p-1",
        started_at="2026-11-01T10:00:00.000000",
        exec_pid=os.getpid(),
    )
    plans_dir = tmp_path / "plans"

    plain = _reader(conn, plans_dir, clock=_clock).active_plans()
    explicit_none = _reader(
        conn, plans_dir, clock=_clock, live_plan_ids=None
    ).active_plans()

    assert explicit_none == plain
    assert [entry["plan_id"] for entry in plain] == ["p-1"]
    assert plain[0]["side"] == "execution"
    assert plain[0]["started_at"] == "2026-11-01T10:00:00.000000"


def test_callback_error_degrades(
    conn: sqlite3.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A callback that raises does not take the scheduler down.

    The callback is somebody else's code, and somebody else's code
    raises.  A scheduler that dies because a helper threw would lose
    every plan it was watching, not just the one the callback was
    supposed to describe — so the failure degrades to the database's
    facts, and is reported as a warning rather than swallowed.
    """
    _execution_row(
        conn,
        "p-1",
        started_at="2026-11-01T10:00:00.000000",
        exec_pid=os.getpid(),
    )

    def _boom() -> set[str]:
        raise RuntimeError("the live set is unavailable")

    reader = _reader(
        conn, tmp_path / "plans", clock=_clock, live_plan_ids=_boom
    )

    with caplog.at_level(logging.WARNING, logger="plan_scheduler.facts"):
        active = reader.active_plans()

    assert [entry["plan_id"] for entry in active] == ["p-1"]
    assert active[0]["side"] == "execution"
    assert any(
        record.levelno == logging.WARNING
        and "live" in record.getMessage().lower()
        for record in caplog.records
    )


def test_same_plan_id_appears_once(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A plan both sources name is one plan, not two.

    A plan holds one slot, not one per source that can see it.  The
    database's own side wins the tie: a running subprocess is the
    stronger evidence of compute being held, and the start time comes
    with it.
    """
    _execution_row(
        conn,
        "p-1",
        started_at="2026-11-01T10:00:00.000000",
        exec_pid=os.getpid(),
    )

    reader = _reader(
        conn, tmp_path / "plans", clock=_clock, live_plan_ids=lambda: {"p-1"}
    )

    active = reader.active_plans()

    assert [entry["plan_id"] for entry in active] == ["p-1"]
    assert active[0]["side"] == "execution"
    assert active[0]["started_at"] == "2026-11-01T10:00:00.000000"

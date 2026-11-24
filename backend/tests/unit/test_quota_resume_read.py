"""Reading the parked plans out of the state database.

The criterion this file pins
----------------------------
Requirement R3 of the quota-park work says, in one line: *only revive
what was parked because of quota*.  Task 1 delivered the decision
(:func:`quota_resume.select_parked_plans`) as a pure function over rows
that somebody else hands it.  This file pins the *other* half: where the
rows come from.

Two readers need that half, and they need it in two different shapes:

* the heartbeat sweeps the whole table every tick and asks "who is
  parked?" — it needs every parked plan, not one;
* the death latch asks "is THIS plan parked?" for a single plan it is
  about to declare dead — it needs one answer, and it needs that answer
  to be *false* for anything it cannot prove.

The read therefore has one property that matters more than the rest:
**it never raises**.  A heartbeat thread that dies on one malformed row
stops the whole resumption loop for every plan, and a death latch that
raises on a row it cannot read declares the plan dead when it is only
unreadable.  So every failure mode — no tables, a missing column, a
corrupt file — answers the empty answer: no plan is parked, and no plan
is proven to be parked.

What these tests are for
------------------------
They pin the *wiring* between the durable tables and the decision layer:
which columns carry the park, which of them are read, and what happens
when the read cannot be done at all.  The decision itself — which rows
count as parked — is pinned in ``test_quota_resume_select.py``; these
cases must not grow a second copy of it.
"""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from quota_resume import (  # noqa: E402
    ParkedPlan,
    plan_parked,
    read_parked_plans,
)
from state_machine.db import schema as _schema  # noqa: E402
from state_machine.db import connection as _connection  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

#: The stamp the seeded rows carry.  It is a constant because nothing in
#: this criterion reads a timestamp, and a constant keeps the seed honest:
#: a test that grows a timestamp dependency should have to say so.
_STAMP = "2026-11-15T00:00:00Z"


def _state_db() -> sqlite3.Connection:
    """An in-memory state database carrying the *real* schema.

    The real schema is applied on purpose.  A hand-written
    ``CREATE TABLE plan_execution (plan_id, exec_status, stop_reason)``
    would let the reader drift away from the columns the product
    actually writes, and the drift would only show up in production —
    where the column that carries the park is named
    ``verification_stop_reason`` and not ``stop_reason``.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    _schema.migrate(conn)
    return conn


def _seed_execution(
    conn: sqlite3.Connection,
    plan_id: str,
    exec_status: str = "waiting_for_quota",
    stop_reason: str = "waiting_for_quota",
    project_dir: str = None,
) -> None:
    """Write one ``plan_execution`` row."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_execution "
        "(plan_id, exec_status, stop_reason, project_dir, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (plan_id, exec_status, stop_reason, project_dir, _STAMP),
    )


def _seed_verification(
    conn: sqlite3.Connection,
    plan_id: str,
    verification_status: str = "waiting_for_quota",
    verification_stop_reason: str = "waiting_for_quota",
    round: int = 2,
) -> None:
    """Write one ``plan_verification`` row."""
    conn.execute(
        "INSERT OR REPLACE INTO plan_verification "
        "(plan_id, verification_status, round, verification_stop_reason, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (plan_id, verification_status, round, verification_stop_reason, _STAMP),
    )


# ---------------------------------------------------------------------------
# The two tables the reader reads
# ---------------------------------------------------------------------------


def test_read_parked_plans_reads_execution_row():
    """A parked execution is read out of ``plan_execution``.

    The execution table is where a run that stopped for capacity leaves
    its marker: ``exec_status`` says the run is waiting for quota and
    ``stop_reason`` says why.  The reader hands the row to the shared
    decision layer, which is what decides that the row counts as parked
    — this file only proves the row *reaches* it, and that the answer
    it comes back with names the execution.

    The kind matters downstream: the resumer starts an execution and a
    verification through different paths, so a row that arrives with the
    wrong kind would be revived by the wrong one.
    """
    conn = _state_db()
    _seed_execution(conn, "p9")

    assert read_parked_plans(conn) == [
        ParkedPlan(
            plan_id="p9",
            kind="execution",
            round=0,
            reason="waiting_for_quota",
        )
    ]


def test_read_parked_plans_reads_verification_row():
    """A parked verification round is read out of ``plan_verification``.

    The verification table carries its park in its own vocabulary:
    ``verification_status`` says the round is waiting for quota and
    ``verification_stop_reason`` says why.  The round number travels
    with it, because the resumed round *is* the round that stopped —
    starting a new round would throw away the verdicts the stopped one
    had already reached.
    """
    conn = _state_db()
    _seed_verification(conn, "p9", round=2)

    assert read_parked_plans(conn) == [
        ParkedPlan(
            plan_id="p9",
            kind="verification",
            round=2,
            reason="waiting_for_quota",
        )
    ]


def test_read_parked_plans_dedupes_a_plan_parked_in_both_tables():
    """A plan parked in both tables is read once.

    The decision layer owns the de-duplication — a plan parked as an
    execution *and* as a verification is one plan, and reviving it
    twice would break the single-instance rule the whole plan rests on.
    This case pins that the reader does not add a second copy of that
    rule on top: it hands both tables over and takes one answer back.
    """
    conn = _state_db()
    _seed_execution(conn, "p9")
    _seed_verification(conn, "p9", round=2)

    assert read_parked_plans(conn) == [
        ParkedPlan(
            plan_id="p9",
            kind="execution",
            round=0,
            reason="waiting_for_quota",
        )
    ]


# ---------------------------------------------------------------------------
# The rows that are not parked
# ---------------------------------------------------------------------------


def test_plan_parked_false_for_user_stop():
    """A plan a human stopped is not parked, and must not be revived.

    ``/stop`` is a human decision and it is final for that run: the
    plan is not running, but it is not waiting for capacity either.
    Reviving it would undo the human, and it would do so silently — the
    operator asked for the plan to stop and the plan starts itself again
    a few minutes later.

    This is the case the single-plan reader exists for: the death latch
    asks "is this plan parked?" before it declares a plan dead, and a
    plan the human stopped is exactly the plan it must not touch.
    """
    conn = _state_db()
    _seed_execution(conn, "p9", exec_status="stopped", stop_reason="user_stopped")

    assert plan_parked(conn, "p9") is False


def test_plan_parked_false_for_stopped_execution_even_with_the_quota_marker():
    """A terminal status wins over the quota marker.

    The marker says "this run stopped for capacity"; the status says
    "this run is over".  When both are on the row, the status is the
    later word: a run that was parked and *then* stopped has an ending,
    and reviving it would re-run work whose outcome was already
    written down.

    So the marker alone must not be enough.  This case exists because
    the two halves are read from different columns, and a reader that
    only looked at the marker column would revive stopped plans.
    """
    conn = _state_db()
    _seed_execution(conn, "p9", exec_status="stopped", stop_reason="waiting_for_quota")

    assert plan_parked(conn, "p9") is False


def test_read_parked_plans_ignores_a_failed_verification_round():
    """A failed verification round is not parked.

    ``failed`` says the round ended for good and its verdict stands.
    The round may still carry a quota marker from the moment it parked,
    but a round that ended has nothing left to resume — and reviving it
    would re-run a judgement that has already been written down.
    """
    conn = _state_db()
    _seed_verification(
        conn,
        "p9",
        verification_status="failed",
        verification_stop_reason="waiting_for_quota",
    )

    assert read_parked_plans(conn) == []
    assert plan_parked(conn, "p9") is False


def test_plan_parked_true_for_a_quota_park():
    """The positive answer, so the negative ones are not vacuous.

    A test that only ever sees ``False`` would also pass against a
    reader that never reads anything.  The latch needs to be able to
    say *yes*: a plan parked for capacity is not dead, it is waiting,
    and the latch must be able to tell those two apart.
    """
    conn = _state_db()
    _seed_execution(conn, "p9")

    assert plan_parked(conn, "p9") is True


def test_plan_parked_false_for_an_unknown_plan():
    """A plan with no rows at all is not parked."""
    conn = _state_db()
    _seed_execution(conn, "p9")

    assert plan_parked(conn, "p-other") is False


# ---------------------------------------------------------------------------
# The read that cannot be done
# ---------------------------------------------------------------------------


def test_read_parked_plans_swallows_broken_db():
    """A database without the tables answers "nothing is parked".

    The reader is called from a heartbeat thread and from a death latch.
    Both run inside a process that has other work to do, so a read that
    raises is not a failed read — it is a dead thread, and the dead
    thread takes the whole resumption loop with it.

    So the answer to "I could not read the table" is the empty answer.
    The cost of that choice is a park that is not noticed until the
    next tick; the cost of the other choice is a heartbeat that never
    runs again.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)

    assert read_parked_plans(conn) == []
    assert plan_parked(conn, "p9") is False


def test_read_parked_plans_swallows_a_missing_column():
    """A table that lost a column answers "nothing is parked".

    The columns that carry the park are named by the schema, and a
    database written by an older build may not have them.  Reading
    around the missing column would be worse than not reading: the
    reader would answer "this plan is parked" from a half-understood
    row, and would revive it on the strength of a column it could not
    read.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(
        "CREATE TABLE plan_execution (plan_id TEXT, exec_status TEXT, stop_reason TEXT)"
    )
    conn.execute(
        "CREATE TABLE plan_verification (plan_id TEXT, verification_status TEXT, round INTEGER)"
    )
    conn.execute(
        "INSERT INTO plan_execution VALUES ('p9', 'waiting_for_quota', 'waiting_for_quota')"
    )

    assert read_parked_plans(conn) == []


def test_read_parked_plans_swallows_a_corrupt_file(tmp_path):
    """A corrupt database file answers "nothing is parked".

    SQLite does not refuse to open a file that is not a database — it
    refuses at the first statement.  So the corrupt case reaches the
    reader as an exception from the query, and the reader has to absorb
    it exactly like the missing-table case.
    """
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"this is not a sqlite database at all")

    conn = sqlite3.connect(str(db_path))
    try:
        assert read_parked_plans(conn) == []
        assert plan_parked(conn, "p9") is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The connection the reader opens for itself
# ---------------------------------------------------------------------------


def test_read_parked_plans_opens_its_own_connection(tmp_path, monkeypatch):
    """With no connection handed in, the reader opens its own.

    The heartbeat has no connection to lend: it is a background thread,
    and the connection it would borrow belongs to a request that is
    already over.  So the reader resolves the state database's path
    itself and closes the connection it opened — a connection left open
    by a thread that runs every few seconds is a file descriptor leak
    that only shows up as "too many open files" hours later.

    The path comes from ``server._state_db_path()`` — the one place that
    knows how the state database's location is resolved — so the reader
    follows the same override the rest of the product follows.

    The connection the reader opened is closed again before it returns.
    That is the part of the contract that is easy to lose: a reader
    that opens its own connection and forgets to close it leaks one
    file descriptor per tick, and the symptom of that leak arrives
    hours later, in a process nobody suspects.
    """
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    _schema.migrate(conn)
    _seed_execution(conn, "p9")
    conn.close()

    stub = types.ModuleType("server")
    stub._state_db_path = lambda: db_path
    monkeypatch.setitem(sys.modules, "server", stub)

    opened: list = []
    real_open = _connection.open

    def _recording_open(path):
        opened_conn = real_open(path)
        opened.append(opened_conn)
        return opened_conn

    monkeypatch.setattr(_connection, "open", _recording_open)

    assert read_parked_plans() == [
        ParkedPlan(
            plan_id="p9",
            kind="execution",
            round=0,
            reason="waiting_for_quota",
        )
    ]
    assert plan_parked(None, "p9") is True

    # Every connection the reader opened was closed again.
    assert opened, "the reader did not open a connection of its own"
    for leaked in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            leaked.execute("SELECT 1")


# ---------------------------------------------------------------------------
# The working directory the row records
# ---------------------------------------------------------------------------


def test_read_parked_plans_reads_project_dir():
    """The reader brings the recorded working directory out with it.

    The park lives in ``exec_status`` / ``stop_reason``, but the
    directory the run was in lives in a third column — and it is the
    column the start path needs.  A run that stopped for capacity has
    no pause checkpoint to carry the directory, so the durable row is
    the only place it can come from; a reader that does not bring it
    out leaves the resumer with nothing to hand over, and the start
    path refuses the plan (400) on every tick, forever.

    The directory is carried as it is recorded: no resolution, no
    normalisation, no default.  The start path resolves it itself, and
    a reader that resolved it too would be a second opinion about
    which directory the run was in.
    """
    conn = _state_db()
    _seed_execution(conn, "p9", project_dir="/tmp/worktrees/p9")

    parked = read_parked_plans(conn)

    assert [entry.plan_id for entry in parked] == ["p9"]
    assert [entry.project_dir for entry in parked] == ["/tmp/worktrees/p9"]


def test_read_parked_plans_without_project_dir_still_reads_the_row():
    """A row that records no directory is still a parked plan.

    The column is optional in the schema and empty in every row written
    before the run had a directory, so the reader has to cope with its
    absence: the plan is parked either way.  What it cannot be is
    *revived* — which is a fact the resumer reports, not a reason for
    the reader to pretend the plan is not parked.

    This is the backwards-compatible half of the change: an old row
    reads as before, with no directory attached.
    """
    conn = _state_db()
    _seed_execution(conn, "p9")

    parked = read_parked_plans(conn)

    assert parked == [
        ParkedPlan(
            plan_id="p9",
            kind="execution",
            round=0,
            reason="waiting_for_quota",
        )
    ]
    assert parked[0].project_dir is None

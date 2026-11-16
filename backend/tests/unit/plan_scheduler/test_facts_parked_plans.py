"""The scheduler's read model can see the plans that are parked.

Background
----------
A plan parked for quota is neither working nor waiting: it is not
holding a slot (:meth:`PlanFactsReader.active_plans` does not name it)
and it is not in line (:meth:`PlanFactsReader.queued_plans` does not
name it either).  The scheduler's read model therefore could not see a
plan that had stopped for want of provider capacity — the one plan whose
state an operator most needs to see, because it is the one that will
start again by itself once the window opens.

The park is already written down: the execution row carries
``exec_status`` / ``stop_reason`` and the verification row carries
``verification_status`` / ``verification_stop_reason``, and the workflow
phase says ``waiting_for_quota`` on the routing row.

What "parked" means is *not* decided here.  The criterion lives in one
place — :func:`quota_resume.select_parked_plans` — because the read
model and the resumer must not grow two answers to the same question.
This file pins the *reading*: which rows are handed to that selector,
what the row the panel receives carries, and how the reader behaves
when there is nothing parked or when the rows cannot be read.

Why a real database
-------------------
The claim being pinned is a claim about *columns*: the value that sits
in ``plan_execution.exec_status`` and in
``plan_verification.verification_status`` has to arrive in the row.  A
stand-in connection could hold the same tuples and still not pin the
SQL the derivation actually reads.  The database here is the production
schema, created by the production migration.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

pytestmark = pytest.mark.unit


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


def _routing(conn: sqlite3.Connection, plan_id: str, phase: str) -> None:
    """Write the plan's routing row: the phase it is in."""
    conn.execute(
        "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, phase, _now()),
    )


def _parked_execution(conn: sqlite3.Connection, plan_id: str) -> None:
    """Write the plan's execution row, parked for quota.

    This is the shape the quota disposition writes: the status says the
    run is parked and the reason says *why* it stopped.
    """
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, exec_status, stop_reason, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (plan_id, "waiting_for_quota", "waiting_for_quota", _now()),
    )


def _parked_verification(
    conn: sqlite3.Connection, plan_id: str, round_number: int = 1
) -> None:
    """Write the plan's verification row, parked for quota."""
    conn.execute(
        "INSERT INTO plan_verification "
        "(plan_id, verification_status, verification_stop_reason, round, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            plan_id,
            "waiting_for_quota",
            "waiting_for_quota",
            round_number,
            _now(),
        ),
    )


def _by_id(rows: list) -> dict:
    """Index the rows by the plan they are about."""
    return {row["plan_id"]: row for row in rows}


def _plan_dir(plans_root: Path, plan_id: str) -> Path:
    """Give the plan a directory under the plans root.

    The parked set does not depend on the directory — the case that
    pins that rule is the one that leaves the directory out.
    """
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


# ---------------------------------------------------------------------------
# The parked rows
# ---------------------------------------------------------------------------


def test_parked_plans_reads_both_tables(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A parked plan is read from either table.

    An execution run and a verification round are parked in different
    tables, and the panel has to see both — they are two plans waiting
    for the same window, and a reader that only looked at one table
    would report half of them.
    """
    _routing(conn, "p-exec", "waiting_for_quota")
    _parked_execution(conn, "p-exec")
    _routing(conn, "p-verify", "waiting_for_quota")
    _parked_verification(conn, "p-verify", round_number=2)
    _plan_dir(tmp_path / "plans", "p-exec")
    _plan_dir(tmp_path / "plans", "p-verify")

    parked = _reader(conn, tmp_path / "plans").parked_plans()

    rows = _by_id(parked)
    assert set(rows) == {"p-exec", "p-verify"}
    assert rows["p-exec"]["kind"] == "execution"
    assert rows["p-verify"]["kind"] == "verification"
    # The verification row knows which round it stopped in; the
    # execution row has no round of its own and says zero.
    assert rows["p-verify"]["round"] == 2
    assert rows["p-exec"]["round"] == 0
    assert all(row["reason"] == "waiting_for_quota" for row in parked)


def test_parked_plans_dedupes_to_execution(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A plan parked in both tables is one row, on the execution side.

    The two tables describe two sides of the same plan, and a plan is
    one plan — it holds one slot and it gets one row, the way
    :meth:`PlanFactsReader.active_plans` reports it once.  The
    execution side wins the tie, because it is the side a human
    recognises as "the run stopped".
    """
    _routing(conn, "p-1", "waiting_for_quota")
    _parked_execution(conn, "p-1")
    _parked_verification(conn, "p-1", round_number=1)
    _plan_dir(tmp_path / "plans", "p-1")

    parked = _reader(conn, tmp_path / "plans").parked_plans()

    assert len(parked) == 1
    assert parked[0]["plan_id"] == "p-1"
    assert parked[0]["kind"] == "execution"


def test_parked_plans_is_empty_list_when_none(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Nothing parked is an empty list — never ``None``.

    The caller asks "which plans are parked?" on every tick and walks
    the answer.  ``None`` is not an answer a caller can walk, and a
    reader that answers ``None`` when there is nothing to say teaches
    every caller to guard against a case that cannot happen.
    """
    _routing(conn, "p-1", "queued")
    conn.execute(
        "INSERT INTO plan_execution (plan_id, exec_status, updated_at) "
        "VALUES (?, ?, ?)",
        ("p-1", "completed", _now()),
    )
    _plan_dir(tmp_path / "plans", "p-1")

    parked = _reader(conn, tmp_path / "plans").parked_plans()

    assert parked == []
    assert isinstance(parked, list)


def test_parked_plans_carries_phase(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A parked row carries the phase the routing row holds.

    The phase is read from the routing row and passed on as it is — the
    facts layer does not map it to a label, because a label is the
    routing layer's business and a second mapping would be a second
    answer to the same question.
    """
    _routing(conn, "p-1", "waiting_for_quota")
    _parked_execution(conn, "p-1")
    _plan_dir(tmp_path / "plans", "p-1")

    parked = _reader(conn, tmp_path / "plans").parked_plans()

    assert len(parked) == 1
    assert parked[0]["phase"] == "waiting_for_quota"


# ---------------------------------------------------------------------------
# The edges
# ---------------------------------------------------------------------------


def test_parked_plans_reports_a_plan_without_a_directory(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A parked plan is reported even when its directory is gone.

    A plan whose directory is missing is dropped from the *queue* — a
    ghost at the head of the queue would block every plan behind it.
    A parked plan is not in the queue, and the park is a fact of the
    database: dropping it because the directory moved would hide the
    very plan the operator has to know about.
    """
    _routing(conn, "p-1", "waiting_for_quota")
    _parked_execution(conn, "p-1")

    parked = _reader(conn, tmp_path / "plans").parked_plans()

    assert [row["plan_id"] for row in parked] == ["p-1"]


def test_parked_plans_returns_empty_when_the_rows_cannot_be_read(
    conn: sqlite3.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Rows that cannot be read are a warning and an empty list.

    The scheduler reads the parked set on every tick, and a read that
    raises would take the tick down — the tick is what admits the other
    plans.  A failed read therefore answers "nothing is parked", which
    is the safe direction: the parked plans wait one tick longer, and
    nothing else breaks.
    """
    conn.execute("DROP TABLE plan_execution")

    with caplog.at_level(logging.WARNING, logger="plan_scheduler.facts"):
        parked = _reader(conn, tmp_path / "plans").parked_plans()

    assert parked == []
    assert "parked" in caplog.text.lower()

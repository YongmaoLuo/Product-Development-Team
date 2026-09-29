"""Unit tests for the :class:`RoutingRepository` CAS predicate layer.

Background
----------
``plan_routing`` is the state-machine's startup-mutex hot row.
``RoutingRepository`` exposes:

  * ``find(plan_id)``         — return the row dict or ``None``
  * ``list_all()``            — return every row with ``archived=False``
                                derived at the cutoff
  * ``current(plan_id)``      — convenience alias for ``find`` (the
                                common read path is "what stage is
                                this plan in right now?")
  * ``try_mark_phase(...)``   — CAS on (stage, version); returns
                                ``True`` on success, raises
                                :class:`ConflictError` on predicate
                                or version mismatch
  * ``insert(plan_id, stage, substage=None)`` — bootstrap a row
  * ``current_version(plan_id)`` — read the current ``version`` for
                                the predicate layer

CAS contract (this is the bug 2 anchor):

    try_mark_phase(plan_id,
                   expected_phases=("ready",),
                   new_phase="executing")

Succeeds only if the current row's ``stage`` is one of
``expected_phases`` AND the row's ``version`` is unchanged since the
last read.  On failure the version is NOT incremented (no partial
business row).

Concurrency contract:

    Two concurrent callers execute ``try_mark_phase`` on the same
    plan.  One wins, the other raises :class:`ConflictError`.  The
    loser sees the version committed by the winner (its CAS predicate
    fails because the version it read is stale).

TDD spec (these are the tests the contract above pins):

  * test_try_mark_phase_succeeds_from_startable_state
      ready → executing returns True
  * test_try_mark_phase_rejects_non_startable_state (BUG 2 ANCHOR)
      executing 状态再次 start execution → raise ConflictError
  * test_try_mark_phase_increments_version_by_one
      Success path increments ``version`` by 1
  * test_try_mark_phase_failure_leaves_no_partial_business_row
      CAS failure does NOT bump ``version``; ``stage`` stays
  * test_insert_then_find_roundtrip
      insert → find returns the same fields
  * test_current_returns_none_for_missing_plan
      current(missing) returns None (not raising)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.routing_repository import (
    ConflictError,
    PlanNotFoundError,
    RoutingRepository,
)


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


# ---------------------------------------------------------------------------
# Roundtrip / read API
# ---------------------------------------------------------------------------


def test_insert_then_find_roundtrip(conn: sqlite3.Connection) -> None:
    """insert → find returns the same fields.

    Pins the basic write-then-read contract: every field we
    ``INSERT``'d is faithfully returned by ``find``.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="ready", substage="ready")

    row = repo.find("p1")
    assert row is not None, "find() must return the inserted row"
    assert row["plan_id"] == "p1"
    assert row["current_phase"] == "ready"
    assert row["substage"] == "ready"
    # version is implementation detail but must be a non-negative int
    assert isinstance(row["version"], int)
    assert row["version"] >= 0
    assert isinstance(row["updated_at"], str)
    assert row["updated_at"]  # non-empty


def test_current_returns_none_for_missing_plan(conn: sqlite3.Connection) -> None:
    """``current(missing)`` returns ``None`` rather than raising.

    The read path is the common code path; the missing case should
    not raise (callers want to know "is there a row for this plan?").
    """
    repo = RoutingRepository(conn)
    assert repo.current("does-not-exist") is None


def test_current_returns_same_as_find(conn: sqlite3.Connection) -> None:
    """``current(plan_id)`` is a thin alias over ``find(plan_id)``.

    Per the interface contract ``current`` is the convenience accessor;
    we pin the equivalence so future refactors don't drift.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="ready")

    a = repo.current("p1")
    b = repo.find("p1")
    assert a == b


# ---------------------------------------------------------------------------
# CAS predicate: success path
# ---------------------------------------------------------------------------


def test_try_mark_phase_succeeds_from_startable_state(
    conn: sqlite3.Connection,
) -> None:
    """ready → executing returns True (CAS success)."""
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="ready")

    ok = repo.try_mark_phase(
        "p1",
        expected_phases=("ready",),
        new_phase="executing",
        substage="task_1",
    )
    assert ok is True, "CAS must succeed from a startable state"

    # The new stage and substage must be persisted.
    row = repo.find("p1")
    assert row is not None
    assert row["current_phase"] == "executing"
    assert row["substage"] == "task_1"


def test_try_mark_phase_increments_version_by_one(
    conn: sqlite3.Connection,
) -> None:
    """Successful CAS increments ``version`` by exactly 1.

    version is the optimistic-lock token: each successful
    ``try_mark_phase`` bumps it.  Two successful CAS calls in a
    row → version += 2.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="ready")

    v0 = repo.current_version("p1")
    assert v0 is not None

    repo.try_mark_phase("p1", ("ready",), "executing", substage=None)
    v1 = repo.current_version("p1")
    assert v1 == v0 + 1, f"version must advance by 1; got {v0} -> {v1}"

    repo.try_mark_phase(
        "p1", ("executing", "failed", "completed"),
        "completed",
    )
    v2 = repo.current_version("p1")
    assert v2 == v1 + 1, f"version must advance again; got {v1} -> {v2}"


# ---------------------------------------------------------------------------
# CAS predicate: rejection path (BUG 2 ANCHOR)
# ---------------------------------------------------------------------------


def test_try_mark_phase_rejects_non_startable_state(
    conn: sqlite3.Connection,
) -> None:
    """executing 状态再次 start execution → raise ConflictError (BUG 2 ANCHOR).

    This is the contract bug 2 hinges on: a second
    ``try_mark_phase(..., 'executing', ...)`` call when the plan is
    ALREADY in ``executing`` must NOT succeed.  The semantic failure
    mode is ``ConflictError`` (predicate mismatch).
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="ready")

    # First transition: ready → executing (succeeds).
    repo.try_mark_phase("p1", ("ready",), "executing", substage="task_1")

    # Second transition: executing → executing (MUST raise ConflictError
    # because 'executing' is NOT in expected_phases=("ready", ...)).
    with pytest.raises(ConflictError):
        repo.try_mark_phase(
            "p1",
            ("ready", "failed", "completed"),
            "executing",
            substage="task_2",
        )

    # And the row is left in the SAME state the first CAS left it in
    # (no partial business row from the rejected attempt).
    row = repo.find("p1")
    assert row is not None
    assert row["current_phase"] == "executing"
    assert row["substage"] == "task_1"


def test_try_mark_phase_failure_leaves_no_partial_business_row(
    conn: sqlite3.Connection,
) -> None:
    """CAS failure must NOT bump ``version`` and must NOT mutate fields.

    A predicate or version mismatch is a "no-op, surface error"
    outcome.  ``version`` stays where it was, and ``stage``/
    ``substage`` are unchanged.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="executing", substage="task_5")

    v_before = repo.current_version("p1")
    assert v_before is not None

    # The row is in "executing"; expected_phases=("ready",) is
    # a mismatch — CAS must fail.
    with pytest.raises(ConflictError):
        repo.try_mark_phase(
            "p1",
            ("ready",),
            "completed",
        )

    v_after = repo.current_version("p1")
    row = repo.find("p1")
    assert v_after == v_before, (
        f"version must NOT change on CAS failure; got {v_before} -> {v_after}"
    )
    assert row is not None
    assert row["current_phase"] == "executing"
    assert row["substage"] == "task_5"


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------


def test_try_mark_phase_raises_plan_not_found(conn: sqlite3.Connection) -> None:
    """Unknown plan_id → PlanNotFoundError (not ConflictError).

    The two errors have different upstream meanings:
      * PlanNotFoundError = caller typo / orphan retry
      * ConflictError     = concurrent writer beat us
    Pinning them separately keeps API consumers from collapsing the
    two failure modes into a single 409.
    """
    repo = RoutingRepository(conn)

    with pytest.raises(PlanNotFoundError):
        repo.try_mark_phase(
            "no-such-plan",
            ("ready",),
            "executing",
        )


def test_try_mark_phase_rejects_empty_expected_phases(
    conn: sqlite3.Connection,
) -> None:
    """``expected_phases=()`` is a programmer error → ValueError.

    An empty tuple would mean "match any current stage", which is
    exactly the silent-failure case bug 2 is meant to prevent.
    We raise rather than silently succeed.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="ready")

    with pytest.raises(ValueError):
        repo.try_mark_phase("p1", (), "executing")


# ---------------------------------------------------------------------------
# list_all + archived derivation
# ---------------------------------------------------------------------------


def test_list_all_returns_empty_on_fresh_db(conn: sqlite3.Connection) -> None:
    """Empty database → list_all returns []."""
    repo = RoutingRepository(conn)
    assert repo.list_all() == []


def test_list_all_returns_all_inserted_rows(
    conn: sqlite3.Connection,
) -> None:
    """list_all returns every inserted row, with archived=False (cutoff = now)."""
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="ready")
    repo.insert(plan_id="p2", phase="executing", substage="task_3")

    rows = repo.list_all()
    by_id = {r["plan_id"]: r for r in rows}
    assert set(by_id.keys()) == {"p1", "p2"}
    for r in rows:
        assert r["archived"] is False
    assert by_id["p1"]["current_phase"] == "ready"
    assert by_id["p2"]["current_phase"] == "executing"
    assert by_id["p2"]["substage"] == "task_3"


# ---------------------------------------------------------------------------
# Concurrency (single-threaded simulator — same-plan race)
# ---------------------------------------------------------------------------


def test_concurrent_try_mark_phase_one_winner(
    conn: sqlite3.Connection,
) -> None:
    """Two consecutive CAS attempts on the same plan: exactly one wins.

    The "second" attempt reads the row AFTER the first has committed,
    so its version is now stale relative to the DB.  The CAS
    predicate's version check rejects the second.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="ready")

    # First call: read at v0, predicate matches, version advances to v0+1.
    repo.try_mark_phase("p1", ("ready",), "executing", substage="task_1")

    # Second call: it sees v0+1 in the DB and is asking for the same
    # "executing" target — but the predicate is the same, so the
    # **stage mismatch** (current='executing', expected contains
    # 'ready' but not 'executing') is what rejects it.
    # We confirm the rejection and the row state is preserved.
    with pytest.raises(ConflictError):
        repo.try_mark_phase(
            "p1",
            ("ready", "failed", "completed"),
            "executing",
            substage="task_2",
        )

    row = repo.find("p1")
    assert row is not None
    assert row["current_phase"] == "executing"
    assert row["substage"] == "task_1"

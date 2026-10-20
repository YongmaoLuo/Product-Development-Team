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

completed_phases contract (the phase history must not drift away
from the phase):

  * test_try_mark_phase_records_completed_phases_executing_to_verification
      executing → verification ⇒ ["executing", "execution"]
  * test_try_mark_phase_records_completed_phases_executing_to_completed
      executing → completed ⇒ ["executing", "execution"]
  * test_try_mark_phase_records_completed_phases_ready_to_executing
      ready → executing ⇒ ["ready"]
  * test_try_mark_phase_conflict_leaves_completed_phases_untouched
      a rejected CAS leaves the column byte-for-byte identical
  * test_try_mark_phase_completed_phases_dedup
      a phase already recorded is not recorded twice
  * test_try_mark_phase_version_still_increments_with_completed_phases
      the version CAS is unchanged by the history bookkeeping
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# completed_phases: the phase history travels with the phase CAS
# ---------------------------------------------------------------------------


def _read_completed_phases(
    repo: RoutingRepository,
    plan_id: str,
) -> list:
    """Read ``completed_phases`` off the row, decoded from its JSON text."""
    row = repo.find(plan_id)
    assert row is not None, f"row for {plan_id!r} must exist"
    raw = row["completed_phases"]
    if not raw:
        return []
    return json.loads(raw)


def _read_completed_phases_raw(
    repo: RoutingRepository,
    plan_id: str,
) -> str:
    """Read the ``completed_phases`` column verbatim (no decoding)."""
    row = repo.find(plan_id)
    assert row is not None, f"row for {plan_id!r} must exist"
    return row["completed_phases"]


def test_try_mark_phase_records_completed_phases_executing_to_verification(
    conn: sqlite3.Connection,
) -> None:
    """executing → verification records ["executing", "execution"].

    The phase history is maintained by the SAME statement that moves
    the phase, so a plan that leaves ``executing`` for a verification
    phase carries both the phase it left and the ``execution`` marker
    ``PlanState.transition_to`` would have appended (plan_state.py:608).
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="executing")

    ok = repo.try_mark_phase(
        "p1",
        expected_phases=("executing",),
        new_phase="verification",
    )
    assert ok is True

    assert _read_completed_phases(repo, "p1") == ["executing", "execution"]


def test_try_mark_phase_records_completed_phases_executing_to_completed(
    conn: sqlite3.Connection,
) -> None:
    """executing → completed records ["executing", "execution"].

    A terminal phase completes ``execution`` too (plan_state.py:612).
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="executing")

    ok = repo.try_mark_phase(
        "p1",
        expected_phases=("executing",),
        new_phase="completed",
    )
    assert ok is True

    assert _read_completed_phases(repo, "p1") == ["executing", "execution"]


def test_try_mark_phase_records_completed_phases_ready_to_executing(
    conn: sqlite3.Connection,
) -> None:
    """ready → executing records ["ready"] and nothing else.

    ``executing`` is neither a terminal phase nor a verification phase,
    so only the phase we left is completed.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="ready")

    ok = repo.try_mark_phase(
        "p1",
        expected_phases=("ready",),
        new_phase="executing",
    )
    assert ok is True

    assert _read_completed_phases(repo, "p1") == ["ready"]


def test_try_mark_phase_conflict_leaves_completed_phases_untouched(
    conn: sqlite3.Connection,
) -> None:
    """A rejected CAS must not write ``completed_phases`` at all.

    The seed is written as NON-canonical JSON text on purpose: a
    re-serialization of the same list would produce different bytes, so
    this assertion can tell "nothing was written" apart from "the
    column was rewritten with the same content".
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="executing")
    conn.execute(
        "UPDATE plan_routing SET completed_phases = ? WHERE plan_id = ?",
        ('["ready","executing"]', "p1"),
    )

    before = _read_completed_phases_raw(repo, "p1")
    assert before == '["ready","executing"]'

    with pytest.raises(ConflictError):
        repo.try_mark_phase(
            "p1",
            expected_phases=("ready",),
            new_phase="completed",
        )

    after = _read_completed_phases_raw(repo, "p1")
    assert after == before, (
        f"a failed CAS must not touch completed_phases; "
        f"got {before!r} -> {after!r}"
    )

    # The next successful CAS builds on exactly the seeded value — the
    # rejected attempt neither wrote nor corrupted it.
    repo.try_mark_phase(
        "p1",
        expected_phases=("executing",),
        new_phase="verification",
    )
    assert _read_completed_phases(repo, "p1") == [
        "ready",
        "executing",
        "execution",
    ]


def test_try_mark_phase_completed_phases_dedup(
    conn: sqlite3.Connection,
) -> None:
    """A phase already recorded is not recorded a second time."""
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="executing")
    repo.write_plan_state(
        "p1",
        phase=None,
        completed_phases=["executing"],
        review_rounds={},
        flags={},
        verification={},
        last_updated=None,
    )

    ok = repo.try_mark_phase(
        "p1",
        expected_phases=("executing",),
        new_phase="verification",
    )
    assert ok is True

    completed = _read_completed_phases(repo, "p1")
    assert completed == ["executing", "execution"]
    assert completed.count("executing") == 1, (
        f"completed_phases must not contain duplicates; got {completed!r}"
    )


def test_try_mark_phase_version_still_increments_with_completed_phases(
    conn: sqlite3.Connection,
) -> None:
    """The version CAS is untouched by the history bookkeeping."""
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="executing")

    v0 = repo.current_version("p1")
    assert v0 is not None

    repo.try_mark_phase(
        "p1",
        expected_phases=("executing",),
        new_phase="verification",
    )

    v1 = repo.current_version("p1")
    assert v1 == v0 + 1, f"version must advance by 1; got {v0} -> {v1}"
    assert _read_completed_phases(repo, "p1") == ["executing", "execution"]


# ---------------------------------------------------------------------------
# completed_phases: the PlanState write path MERGES the history
# ---------------------------------------------------------------------------
#
# ``write_plan_state`` is the write path of ``PlanState``, whose
# ``completed_phases`` is a CACHED SNAPSHOT of the row.  A snapshot
# that predates an append somebody else made (the phase CAS in
# :meth:`RoutingRepository.try_mark_phase` maintains the very same
# ledger) may only ADD the phases it knows about — it must never
# delete history somebody else recorded.  Hence ``"merge"`` is the
# default and ``"replace"`` is reserved for the explicit rewind
# (``PlanState.reset_to``).


def _seed_completed_phases(
    conn: sqlite3.Connection,
    plan_id: str,
    phases: list,
) -> None:
    """Put ``phases`` into the row's ``completed_phases`` column.

    Seeded with a direct UPDATE — NOT through ``write_plan_state`` —
    so the test does not depend on the code path it is testing.
    """
    conn.execute(
        "UPDATE plan_routing SET completed_phases = ? WHERE plan_id = ?",
        (json.dumps(phases), plan_id),
    )


def _write_metadata(
    repo: RoutingRepository,
    plan_id: str,
    completed_phases: list,
    mode: str = "merge",
) -> None:
    """A metadata-only ``write_plan_state`` (no phase argument)."""
    repo.write_plan_state(
        plan_id,
        phase=None,
        completed_phases=completed_phases,
        review_rounds={},
        flags={},
        verification={},
        last_updated=None,
        completed_phases_mode=mode,
    )


def test_write_plan_state_merge_keeps_the_recorded_history(
    conn: sqlite3.Connection,
) -> None:
    """A metadata write that knows nothing must not erase the history.

    This is the exact shape of the bug the merge fixes: a
    ``PlanState`` instance holding a stale snapshot (its
    ``completed_phases`` is empty) writes it back and, under the old
    whole-column overwrite, the entries the CAS had just appended were
    gone.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="executing")
    _seed_completed_phases(conn, "p1", ["executing", "execution"])

    _write_metadata(repo, "p1", [])

    assert _read_completed_phases(repo, "p1") == ["executing", "execution"], (
        "the merge must keep the history already recorded on the row"
    )


def test_write_plan_state_merge_appends_in_order_without_duplicates(
    conn: sqlite3.Connection,
) -> None:
    """merge keeps the existing order and appends the new entries once.

    ``["a", "b"]`` + ``["b", "c"]`` → ``["a", "b", "c"]``: the existing
    entries keep their positions, the new one is appended, and the
    entry both sides know about is not recorded twice.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="executing")
    _seed_completed_phases(conn, "p1", ["a", "b"])

    _write_metadata(repo, "p1", ["b", "c"])

    assert _read_completed_phases(repo, "p1") == ["a", "b", "c"]


def test_write_plan_state_replace_overwrites_the_history(
    conn: sqlite3.Connection,
) -> None:
    """``replace`` keeps the old whole-column-overwrite semantics.

    This is the mode ``PlanState.reset_to`` uses: rewinding a plan
    means forgetting the phases after the target.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="executing")
    _seed_completed_phases(conn, "p1", ["a", "b", "c"])

    _write_metadata(repo, "p1", ["a"], mode="replace")

    assert _read_completed_phases(repo, "p1") == ["a"], (
        "replace must overwrite the column verbatim"
    )


def test_write_plan_state_rejects_an_unknown_mode(
    conn: sqlite3.Connection,
) -> None:
    """An unknown mode is a programmer error → ValueError.

    Silently falling back to one of the two modes would hide the
    mistake: the difference between "append-only history" and
    "overwrite the history" is exactly the kind of thing that must
    never be guessed.
    """
    repo = RoutingRepository(conn)
    repo.insert(plan_id="p1", phase="executing")

    with pytest.raises(ValueError):
        _write_metadata(repo, "p1", ["a"], mode="append")

"""Integration tests: the capacity ledger — the slots, the used count, the candidates.

Background
----------
A slot is a promise about compute: "this plan is working, and it took
the slot at this moment".  The scheduler does not keep the *plan* side
of that promise in memory — who is working is a plan fact, derived on
every tick from ``plan_routing``, ``plan_execution`` and
``plan_verification``.

What the scheduler keeps is the other half: ``scheduler_slot``, one row
per plan, holding the moment the slot was taken.  That moment is the
only thing the eviction rule is allowed to use — "a plan that has been
running for at least M seconds may be interrupted" — so it has to be
right, and it has to survive a restart.

:class:`CapacityLedger` is where the two halves are put back together on
every tick:

* ``reconcile()`` makes ``scheduler_slot`` say what the plan facts say.
  A working plan with no slot row gets one — it was started without the
  scheduler, or the scheduler was restarted while it ran — and a slot
  row for a plan that is not working any more is deleted, because the
  plan stopped and the slot is free.
* ``used()`` answers "how many plans hold compute".
* ``evictable(now, M)`` answers "which of them may be interrupted".
* ``workspace_busy(key)`` answers "is this directory taken".

The four claims being pinned
----------------------------
**The reconciliation is the authority.**  A slot row is a fact about a
plan that the plan facts contradict, and the reconciliation is what
makes them agree again: the row is written when it is missing and
removed when it is stale.  A ledger that only ever added rows would
count plans that finished long ago and would report a capacity that is
full while nothing is running.

**The timer is not refreshed.**  A plan that moves from one
verification phase to the next is still the same plan holding the same
slot.  Refreshing ``acquired_at`` on every phase change would let a
plan reset its own protection window and escape eviction by changing
phase — the eviction rule would be satisfied by a plan that had been
running for an hour.

**M is a threshold, not a hint.**  A plan that has been running for
``M - 1`` seconds is protected; a plan that has been running for ``M``
seconds is not.  A rule that is off by one either voids the protection
or stalls the scheduler for good.

**The queue is not the workspace.**  ``workspace_busy`` looks at the
plans that are *working*, not at the plans that are waiting: a plan in
the queue does not hold a directory, and only the working plan does.

Why these cases run against a real SQLite file
----------------------------------------------
The claims are claims about *rows*: a phase in ``plan_routing``, a pid
in ``plan_execution``, a heartbeat in ``plan_verification`` and a row
in ``scheduler_slot``.  A stand-in connection could hold the same tuples
and still not pin the SQL, nor the transaction the reconciliation runs
in.  The database here is the production schema on a real file, opened
the way production opens it.

Why the clock is injected
-------------------------
The eviction threshold is 600 seconds and the verification staleness
rule is 15 minutes.  Waiting for either of them would make the suite
take longer than the thing it measures.  So the clock is a parameter:
the cases below drive :class:`FakeClock` by hand, and the ledger and
the facts reader both read it — one clock, one "now", and no case ever
depends on how fast the machine it runs on is.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from plan_scheduler.store import SqliteSchedulerStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: A fixed moment, so that every case reads the same clock.
BASE_EPOCH = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc).timestamp()


class FakeClock:
    """A clock the case moves by hand.

    It is a callable, because that is what the ledger and the facts
    reader ask for: ``clock()`` is "what time is it now".
    """

    def __init__(self, now: float = BASE_EPOCH) -> None:
        self._now = float(now)

    def __call__(self) -> float:
        """Return the current moment, in epoch seconds."""
        return self._now

    def advance(self, seconds: float) -> float:
        """Move the clock forward and return the new moment."""
        self._now += float(seconds)
        return self._now


def _iso(epoch: float) -> str:
    """Return the moment as the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _ledger(
    conn: sqlite3.Connection, project_root: Path, clock: FakeClock
) -> Any:
    """A capacity ledger over ``conn``.

    The import happens inside the helper so that a missing module is
    reported as the failure of the case that needed it, rather than as a
    collection error that hides every other case in this file.
    """
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.facts import PlanFactsReader

    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, project_root, clock=clock)
    return CapacityLedger(store, facts, clock)


def _facts(conn: sqlite3.Connection, project_root: Path, clock: FakeClock) -> Any:
    """A facts reader over ``conn``, on the case's clock."""
    from plan_scheduler.facts import PlanFactsReader

    return PlanFactsReader(conn, project_root, clock=clock)


def _routing(conn: sqlite3.Connection, plan_id: str, phase: str) -> None:
    """Write the plan's routing fact: the phase it is in."""
    conn.execute(
        "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, phase, _iso(BASE_EPOCH)),
    )


def _execution(
    conn: sqlite3.Connection,
    plan_id: str,
    exec_pid: Optional[int] = None,
    exec_status: Optional[str] = None,
    project_dir: Optional[str] = None,
) -> None:
    """Write the plan's execution fact: the subprocess and the directory."""
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, project_dir, next_run_at, exec_pid, exec_status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (plan_id, project_dir, None, exec_pid, exec_status, _iso(BASE_EPOCH)),
    )


def _verification(
    conn: sqlite3.Connection,
    plan_id: str,
    status: str = "running",
    updated_at: Optional[str] = None,
) -> None:
    """Write the plan's verification fact: the round and its heartbeat."""
    conn.execute(
        "INSERT INTO plan_verification "
        "(plan_id, verification_status, round, max_rounds, updated_at) "
        "VALUES (?, ?, 0, 3, ?)",
        (plan_id, status, updated_at if updated_at is not None else _iso(BASE_EPOCH)),
    )


def _slot(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    plan_id: str,
    acquired_at: str,
) -> None:
    """Give the plan a slot, taken at ``acquired_at``."""
    store.upsert_slot(conn, plan_id, now=acquired_at)


def _slots(
    store: SqliteSchedulerStore, conn: sqlite3.Connection
) -> Dict[str, Dict[str, Any]]:
    """The held slots, by plan id."""
    return {slot["plan_id"]: slot for slot in store.list_slots(conn)}


def _live_pid(case_registry: Any) -> int:
    """The pid of a process that is running right now."""
    process = case_registry.spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    return int(process.pid)


# ---------------------------------------------------------------------------
# The reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_adds_missing_slot(
    db_factory: Any, tmp_path: Path, case_registry: Any
) -> None:
    """A working plan with no slot row gets one.

    A plan can be working without the scheduler having given it a slot:
    the operator started it through the lifecycle API, or the scheduler
    was restarted while it ran.  The reconciliation is what notices, and
    the slot it writes says the plan took the slot *now* — the moment it
    was observed to be working — so the eviction rule protects it for a
    full M rather than evicting it on the spot.
    """
    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()
        alive = _live_pid(case_registry)

        _routing(conn, "p1", "executing")
        _execution(
            conn,
            "p1",
            exec_pid=alive,
            exec_status="running",
            project_dir=str(tmp_path / "p1"),
        )
        _routing(conn, "p2", "executing")
        _execution(
            conn,
            "p2",
            exec_pid=alive,
            exec_status="running",
            project_dir=str(tmp_path / "p2"),
        )
        # The third plan is verifying: no subprocess, but it holds compute.
        _routing(conn, "p3", "verification_running")
        _verification(conn, "p3")

        # Two of the three plans have a slot; the third one has none.
        _slot(store, conn, "p1", _iso(BASE_EPOCH - 100))
        _slot(store, conn, "p2", _iso(BASE_EPOCH - 200))

        ledger = _ledger(conn, tmp_path / "plans", clock)
        ledger.reconcile()

        slots = _slots(store, conn)
        assert set(slots) == {"p1", "p2", "p3"}, (
            f"the working plans are not the plans holding a slot: {slots!r}"
        )
        assert slots["p3"]["acquired_at"] == _iso(BASE_EPOCH), (
            "the missing slot was not taken at the moment the plan was "
            f"observed to be working: {slots['p3']!r}"
        )
        assert slots["p1"]["acquired_at"] == _iso(BASE_EPOCH - 100), (
            "an existing slot was re-stamped: {0!r}".format(slots["p1"])
        )
        assert ledger.used() == 3
    finally:
        conn.close()


def test_reconcile_removes_stale_slot(
    db_factory: Any, tmp_path: Path, case_registry: Any
) -> None:
    """A slot for a plan that is not working is given back.

    The plan finished — or was stopped, or was never started at all — and
    the row that says "this plan holds a slot" is now a lie.  A ledger
    that only ever added rows would keep counting it, and the operator
    would watch a scheduler that says it is full while nothing runs.
    """
    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()
        alive = _live_pid(case_registry)

        _routing(conn, "p1", "executing")
        _execution(
            conn,
            "p1",
            exec_pid=alive,
            exec_status="running",
            project_dir=str(tmp_path / "p1"),
        )

        _slot(store, conn, "p1", _iso(BASE_EPOCH - 100))
        # A plan that is not working at all, and one that finished.
        _slot(store, conn, "ghost", _iso(BASE_EPOCH - 300))
        _slot(store, conn, "finished", _iso(BASE_EPOCH - 200))

        ledger = _ledger(conn, tmp_path / "plans", clock)
        ledger.reconcile()

        slots = _slots(store, conn)
        assert set(slots) == {"p1"}, (
            f"a slot row survived for a plan that is not working: {slots!r}"
        )
        assert slots["p1"]["acquired_at"] == _iso(BASE_EPOCH - 100)
        assert ledger.used() == 1
    finally:
        conn.close()


def test_acquired_at_not_refreshed_across_phases(
    db_factory: Any, tmp_path: Path
) -> None:
    """A phase change does not restart a plan's clock.

    A plan walks through the verification phases — ``verify_first_pass``
    to ``verify_recheck`` to ``verification_repairing`` to
    ``verification_rerunning`` — and it is the same plan, holding the
    same slot, for the whole walk.  If the timer were refreshed on every
    phase change, a plan that had been running for an hour could make
    itself look brand new and walk straight past the eviction threshold.
    """
    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()

        _routing(conn, "p1", "verify_first_pass")
        _verification(conn, "p1", updated_at=_iso(BASE_EPOCH))
        _slot(store, conn, "p1", _iso(BASE_EPOCH - 900))

        ledger = _ledger(conn, tmp_path / "plans", clock)
        ledger.reconcile()

        assert _slots(store, conn)["p1"]["acquired_at"] == _iso(BASE_EPOCH - 900)

        # The plan moves to the next phase and the clock moves on.
        conn.execute(
            "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
            ("verify_recheck", "p1"),
        )
        clock.advance(300)
        conn.execute(
            "UPDATE plan_verification SET updated_at = ? WHERE plan_id = ?",
            (_iso(BASE_EPOCH + 300), "p1"),
        )

        ledger.reconcile()

        slots = _slots(store, conn)
        assert set(slots) == {"p1"}, f"the plan lost its slot: {slots!r}"
        assert slots["p1"]["acquired_at"] == _iso(BASE_EPOCH - 900), (
            "the phase change refreshed the timer: {0!r}".format(slots["p1"])
        )
        assert ledger.used() == 1
    finally:
        conn.close()


def test_empty_active_set_reports_nothing(
    db_factory: Any, tmp_path: Path
) -> None:
    """Nothing working means no slot, no used count and no candidate."""
    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()

        _slot(store, conn, "ghost", _iso(BASE_EPOCH - 900))

        ledger = _ledger(conn, tmp_path / "plans", clock)
        ledger.reconcile()

        assert store.list_slots(conn) == []
        assert ledger.used() == 0
        assert ledger.evictable(clock(), 600) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The eviction candidates
# ---------------------------------------------------------------------------


def test_evictable_respects_m_threshold(
    db_factory: Any, tmp_path: Path, case_registry: Any
) -> None:
    """A plan is a candidate once it has run for M seconds — not before.

    The threshold is ``>=``, not ``>``: the default protection of 600
    seconds is exactly that long, and a rule that is off by one either
    voids the protection or stalls the scheduler for good.  A plan that
    has been running for ``M - 1`` seconds is still protected.
    """
    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()
        alive = _live_pid(case_registry)

        for plan_id, elapsed in (("young", 599), ("exact", 600), ("older", 601)):
            _routing(conn, plan_id, "executing")
            _execution(
                conn,
                plan_id,
                exec_pid=alive,
                exec_status="running",
                project_dir=str(tmp_path / plan_id),
            )
            _slot(store, conn, plan_id, _iso(BASE_EPOCH - elapsed))

        ledger = _ledger(conn, tmp_path / "plans", clock)
        candidates = ledger.evictable(clock(), 600)
        plan_ids = [candidate["plan_id"] for candidate in candidates]

        assert "young" not in plan_ids, (
            f"a plan that ran for M-1 seconds was a candidate: {candidates!r}"
        )
        assert "exact" in plan_ids, (
            f"a plan that ran for M seconds was not a candidate: {candidates!r}"
        )
        assert "older" in plan_ids, (
            f"a plan that ran for M+1 seconds was not a candidate: {candidates!r}"
        )
        assert plan_ids == ["older", "exact"], (
            "the candidates are not in a fixed order: {0!r}".format(candidates)
        )
    finally:
        conn.close()


def test_evictable_ignores_a_plan_that_is_not_working(
    db_factory: Any, tmp_path: Path
) -> None:
    """A slot row alone does not make a plan a candidate.

    The candidate set is "working **and** old enough".  A row left over
    from a plan that has since stopped is not working, and interrupting
    it would interrupt nothing.
    """
    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()

        _slot(store, conn, "finished", _iso(BASE_EPOCH - 900))

        ledger = _ledger(conn, tmp_path / "plans", clock)

        assert ledger.evictable(clock(), 600) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The workspace
# ---------------------------------------------------------------------------


def test_workspace_busy_blocks_second_plan(
    db_factory: Any, tmp_path: Path, case_registry: Any, workspace_factory: Any
) -> None:
    """Two plans may not work in one directory.

    The first plan is working in the workspace; the second one wants the
    same directory and is held back by it.  The directory is named three
    ways in the world — itself, a symlink to it and a symlink to that
    symlink — and all three are one key, so a plan that spells the
    directory differently is held back too.
    """
    db = db_factory()
    conn = db.connect()
    try:
        store = SqliteSchedulerStore()
        clock = FakeClock()
        alive = _live_pid(case_registry)
        workspace = workspace_factory("ws")
        other = workspace_factory("other")

        _routing(conn, "p1", "executing")
        _execution(
            conn,
            "p1",
            exec_pid=alive,
            exec_status="running",
            project_dir=str(workspace.project),
        )
        # The second plan is waiting for its turn — it wants the same
        # directory, but it is not working in it yet.
        _routing(conn, "p2", "queued")
        _execution(conn, "p2", project_dir=str(workspace.project))

        ledger = _ledger(conn, tmp_path / "plans", clock)
        facts = _facts(conn, tmp_path / "plans", clock)
        key = facts.workspace_key("p1")

        assert key is not None
        assert ledger.workspace_busy(key) is True, (
            "the working plan did not make its directory busy"
        )
        assert ledger.workspace_busy(str(workspace.link)) is True, (
            "the same directory under another name was not busy"
        )
        assert ledger.workspace_busy(str(other.project)) is False, (
            "a directory nobody works in was busy"
        )
    finally:
        conn.close()


def test_workspace_busy_ignores_the_queue(
    db_factory: Any, tmp_path: Path
) -> None:
    """A plan that is waiting does not hold a directory.

    The rule is about compute, not about order: a plan in the queue is
    not working, so it cannot make the directory busy for the plan behind
    it.  Were it the other way round, the first plan in the queue would
    block every other plan in the same workspace before a single one of
    them had run.
    """
    db = db_factory()
    conn = db.connect()
    try:
        clock = FakeClock()

        _routing(conn, "p1", "queued")
        _execution(conn, "p1", project_dir=str(tmp_path / "ws"))

        ledger = _ledger(conn, tmp_path / "plans", clock)
        facts = _facts(conn, tmp_path / "plans", clock)
        key = facts.workspace_key("p1")

        assert key is not None
        assert ledger.workspace_busy(key) is False
    finally:
        conn.close()
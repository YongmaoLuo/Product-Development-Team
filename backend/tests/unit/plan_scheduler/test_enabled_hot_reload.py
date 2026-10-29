"""Unit tests: the scheduler's ``enabled`` switch, read on every tick.

What this file holds down
-------------------------
The switch that says whether the scheduler may run is **read on every
tick**, not once at boot.  That is what makes it a switch at all: a
switch that is read can be turned without a restart, and the rollback
the runbook describes is a switch somebody can pull.

The contracts:

1. **A disabled tick does nothing at all.**  It starts no plan, it
   moves no phase, it writes no row — and it leaves the plans that are
   running exactly as it found them.  A rollback stops the writing; it
   does not undo the history, and it does not interrupt anybody.
2. **The transition is seen, once.**  The tick says that the switch
   moved — once per move, not once per tick.
3. **The other settings keep their hot reload.**  ``capacity`` is read
   on every tick, and it was before the switch became one.
4. **Nothing is opened while the switch is off.**  A scheduler that may
   not run does not open the database, because opening it is something
   that happens and "may not run" has to mean "nothing happens".

The cases drive the **production** engine on a **production**
configuration file: the loader is the real one, the file is a real file
on disk, and the change is a rewrite of that file.  Only the
collaborators are the case's own — they write down what the tick did,
which is what the case asks about.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from plan_scheduler.config import SchedulerConfigLoader
from plan_scheduler.engine import PlanSchedulerEngine


#: The moment every case starts at.
BASE_EPOCH = 1_700_000_000.0

#: The name of the worker thread the engine starts.
THREAD_NAME = "PlanSchedulerEngine"


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock the case moves by hand."""

    def __init__(self, now: float = BASE_EPOCH) -> None:
        self._now = float(now)

    def __call__(self) -> float:
        """Return the current moment, in epoch seconds."""
        return self._now


class FakeFacts:
    """The plan facts, as a case writes them down.

    Attributes:
        waiting: the plans that are waiting for their turn.
        running: the plans that are working right now.
    """

    def __init__(
        self,
        waiting: Optional[List[Dict[str, Any]]] = None,
        running: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.waiting = list(waiting or [])
        self.running = list(running or [])

    def queued_plans(self) -> List[Dict[str, Any]]:
        """The plans that are waiting."""
        return list(self.waiting)

    def active_plans(self) -> List[Dict[str, Any]]:
        """The plans that are working."""
        return list(self.running)

    def has_pending_decision(self, plan_id: str) -> bool:
        """No plan of the case's is parked in front of a human."""
        return False

    def workspace_key(self, plan_id: str) -> str:
        """The directory a plan works in."""
        return "workspace-{0}".format(plan_id)

    def start_spec(self, plan_id: str) -> Dict[str, Any]:
        """What the lifecycle needs to start a plan."""
        return {"project_dir": None}


class FakeStore:
    """The scheduler's own store, writing down what it was asked to do.

    Attributes:
        writes: every write the tick asked for, in order.
        queue: the line, as the store holds it.
        slots: the slots, as the store holds them.
    """

    def __init__(self) -> None:
        self.writes: List[str] = []
        self.queue: List[Dict[str, Any]] = []
        self.slots: List[Dict[str, Any]] = []

    # -- reads ------------------------------------------------------------

    def list_queue(self, conn: Any) -> List[Dict[str, Any]]:
        """The line."""
        return list(self.queue)

    def list_slots(self, conn: Any) -> List[Dict[str, Any]]:
        """The slots that are taken."""
        return list(self.slots)

    # -- writes -----------------------------------------------------------

    def enqueue(self, conn: Any, plan_id: str, priority: int = 0) -> None:
        """Put a plan in the line."""
        self.writes.append("enqueue:{0}".format(plan_id))
        self.queue.append({"plan_id": plan_id, "priority": priority, "seq": 1})

    def remove(self, conn: Any, plan_id: str) -> None:
        """Take a plan out of the line."""
        self.writes.append("remove:{0}".format(plan_id))
        self.queue = [row for row in self.queue if row["plan_id"] != plan_id]

    def upsert_slot(self, conn: Any, plan_id: str, stamp: str) -> None:
        """Give a plan its slot."""
        self.writes.append("slot:{0}".format(plan_id))
        self.slots.append({"plan_id": plan_id, "acquired_at": stamp})

    def transaction(self, conn: Any) -> Any:
        """A transaction that commits nothing: the case's writes are the record."""
        from contextlib import nullcontext

        return nullcontext()


class FakeLedger:
    """The capacity ledger, as a case writes it down."""

    def __init__(self, used: int = 0) -> None:
        self._used = int(used)
        self.reconciled = 0

    def used(self) -> int:
        """How many slots are taken."""
        return self._used

    def reconcile(self) -> None:
        """Bring the slots in line with the facts."""
        self.reconciled += 1

    def workspace_busy(self, key: Any) -> bool:
        """No workspace of the case's is taken."""
        return False


class RecordingSink:
    """The exit the tick's decisions go through.

    It applies nothing and starts no plan: the case watches what the
    tick decided without the world changing.
    """

    def __init__(self) -> None:
        self.batches: List[List[Dict[str, Any]]] = []

    def apply(self, decisions: Any) -> List[Dict[str, Any]]:
        """Remember the decisions and hand back what they produced."""
        batch = [dict(decision) for decision in decisions]
        self.batches.append(batch)
        return batch

    def decisions(self) -> List[Dict[str, Any]]:
        """Every decision the sink was handed, in order."""
        return [decision for batch in self.batches for decision in batch]


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _write_settings(path: Path, **scheduler: Any) -> None:
    """Write the settings file and make the change visible in its mtime.

    The loader watches the file's ``(mtime_ns, size)`` fingerprint, so
    the mtime is moved forward explicitly: a rewrite that does not move
    the fingerprint is not a change the loader can see.
    """
    path.write_text(
        yaml.safe_dump({"scheduler": scheduler}), encoding="utf-8"
    )
    stamp = path.stat().st_mtime + 2
    os.utime(path, (stamp, stamp))


def _engine(
    config_path: Path,
    facts: FakeFacts,
    store: FakeStore,
    ledger: FakeLedger,
    sink: RecordingSink,
) -> PlanSchedulerEngine:
    """Build the engine the case drives.

    The engine is built **once** and ticks many times: a switch that
    needs a new engine is a switch that needs a restart.
    """
    return PlanSchedulerEngine(
        SchedulerConfigLoader(config_path),
        store,
        facts,
        ledger,
        object(),
        sink,
        FakeClock(),
        conn=object(),
        thread_name=THREAD_NAME,
    )


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_the_same_engine_sees_the_switch_turned_on_and_off(tmp_path):
    """``enabled`` is read on every tick — the same engine, the same file.

    The case is the whole point of the change: the settings file is
    rewritten while the engine keeps running, and the tick after the
    rewrite sees the new value.  Nothing is restarted, and no second
    engine is built.
    """
    config_path = tmp_path / "scheduler.yaml"
    _write_settings(config_path, enabled=False, capacity=1, tick_seconds=0.01)

    facts = FakeFacts(waiting=[{"plan_id": "p1", "priority": 0, "due": True}])
    store = FakeStore()
    ledger = FakeLedger()
    sink = RecordingSink()
    engine = _engine(config_path, facts, store, ledger, sink)

    # -- off: the tick does nothing at all --------------------------------
    engine._run_tick()
    assert store.writes == [], (
        "a disabled tick wrote to the scheduler's own facts: "
        f"{store.writes!r}"
    )
    assert sink.decisions() == [], (
        "a disabled tick decided something: "
        f"{sink.decisions()!r}"
    )
    assert engine.ticks == 1, "the tick was not counted"

    # -- on: the tick does its work --------------------------------------
    _write_settings(config_path, enabled=True, capacity=1, tick_seconds=0.01)
    engine._run_tick()

    assert "slot:p1" in store.writes, (
        "the tick did not admit the plan that was waiting after the "
        f"switch was turned on: {store.writes!r}"
    )
    assert [d.get("action") for d in sink.decisions()] == ["admit"], (
        "the tick did not decide what it was supposed to decide: "
        f"{sink.decisions()!r}"
    )

    # -- off again: the tick does nothing at all --------------------------
    store.writes.clear()
    sink.batches.clear()
    _write_settings(config_path, enabled=False, capacity=1, tick_seconds=0.01)
    engine._run_tick()

    assert store.writes == [], (
        "a tick after the switch was turned off wrote to the scheduler's "
        f"own facts: {store.writes!r}"
    )
    assert sink.decisions() == [], (
        "a tick after the switch was turned off decided something: "
        f"{sink.decisions()!r}"
    )
    assert engine.ticks == 3, "the engine was not the same one throughout"


def test_a_disabled_tick_leaves_the_running_plans_alone(tmp_path):
    """A disabled scheduler does not touch the plans that are running.

    The rollback stops the scheduler from doing new work; it does not
    interrupt anybody, and it does not give anybody's slot back.
    """
    config_path = tmp_path / "scheduler.yaml"
    _write_settings(config_path, enabled=False, capacity=1, tick_seconds=0.01)

    running = [{"plan_id": "p-running", "workspace": "workspace-p-running"}]
    facts = FakeFacts(
        waiting=[{"plan_id": "p1", "priority": 0, "due": True}], running=running
    )
    store = FakeStore()
    store.slots.append({"plan_id": "p-running", "acquired_at": "2026-01-01T00:00:00Z"})
    ledger = FakeLedger(used=1)
    sink = RecordingSink()
    engine = _engine(config_path, facts, store, ledger, sink)

    engine._run_tick()

    assert store.writes == [], (
        "a disabled tick wrote to the scheduler's own facts: "
        f"{store.writes!r}"
    )
    assert [row["plan_id"] for row in store.slots] == ["p-running"], (
        "a disabled tick changed the slots of the plans that are "
        f"running: {store.slots!r}"
    )
    assert [row["plan_id"] for row in facts.running] == ["p-running"], (
        "a disabled tick changed the running plans: "
        f"{facts.running!r}"
    )
    assert sink.decisions() == [], (
        "a disabled tick decided something about the running plans: "
        f"{sink.decisions()!r}"
    )


def test_the_switch_is_said_once_per_move(tmp_path, caplog):
    """The tick says that the switch moved — once per move, not per tick.

    A line per tick would be a flood nobody reads, and a switch that is
    read is a switch whose change has to be visible.
    """
    config_path = tmp_path / "scheduler.yaml"
    _write_settings(config_path, enabled=False, capacity=1, tick_seconds=0.01)

    facts = FakeFacts(waiting=[{"plan_id": "p1", "priority": 0, "due": True}])
    store = FakeStore()
    ledger = FakeLedger()
    sink = RecordingSink()
    engine = _engine(config_path, facts, store, ledger, sink)

    with caplog.at_level("WARNING", logger="plan_scheduler.engine"):
        for _ in range(3):
            engine._run_tick()
        disabled = [
            record for record in caplog.records
            if "disabled" in record.getMessage()
        ]
        assert len(disabled) == 1, (
            "the switch was said "
            f"{len(disabled)} times for three ticks that did nothing: "
            f"{[r.getMessage() for r in caplog.records]!r}"
        )

        _write_settings(
            config_path, enabled=True, capacity=1, tick_seconds=0.01
        )
        engine._run_tick()
        enabled = [
            record for record in caplog.records
            if "enabled" in record.getMessage() and "disabled" not in record.getMessage()
        ]
        assert len(enabled) == 1, (
            "the switch was turned on and nobody said so: "
            f"{[r.getMessage() for r in caplog.records]!r}"
        )

        _write_settings(
            config_path, enabled=False, capacity=1, tick_seconds=0.01
        )
        engine._run_tick()
        disabled_again = [
            record for record in caplog.records
            if "disabled" in record.getMessage()
        ]
        assert len(disabled_again) == 2, (
            "the switch was turned off again and nobody said so: "
            f"{[r.getMessage() for r in caplog.records]!r}"
        )


def test_the_worker_notices_the_switch_without_a_restart(tmp_path):
    """The worker reads the file while it runs — no restart, no second engine.

    This is the case an operator runs: the file is edited, the process
    keeps running, and the scheduler starts scheduling.
    """
    config_path = tmp_path / "scheduler.yaml"
    _write_settings(config_path, enabled=False, capacity=1, tick_seconds=0.02)

    facts = FakeFacts(waiting=[{"plan_id": "p1", "priority": 0, "due": True}])
    store = FakeStore()
    ledger = FakeLedger()
    sink = RecordingSink()
    engine = _engine(config_path, facts, store, ledger, sink)
    engine.start()
    try:
        deadline = time.monotonic() + 5.0
        while engine.ticks < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert engine.ticks >= 2, (
            "the worker never ticked while the switch was off"
        )
        assert store.writes == [], (
            "the worker wrote while the switch was off: "
            f"{store.writes!r}"
        )

        _write_settings(
            config_path, enabled=True, capacity=1, tick_seconds=0.02
        )
        deadline = time.monotonic() + 5.0
        while not store.writes and time.monotonic() < deadline:
            time.sleep(0.005)
        assert store.writes, (
            "the worker never noticed that the switch was turned on — the "
            "settings file is rewritten and the process keeps running"
        )
    finally:
        engine.stop(timeout=5.0)


def test_capacity_is_still_reloaded_on_every_tick(tmp_path):
    """The other settings keep the hot reload they had.

    ``capacity`` was read on every tick before the switch became one,
    and it is read on every tick now.
    """
    config_path = tmp_path / "scheduler.yaml"
    _write_settings(config_path, enabled=True, capacity=1, tick_seconds=0.01)

    facts = FakeFacts(waiting=[{"plan_id": "p1", "priority": 0, "due": True}])
    store = FakeStore()
    ledger = FakeLedger()
    sink = RecordingSink()
    engine = _engine(config_path, facts, store, ledger, sink)

    engine._run_tick()
    assert "slot:p1" in store.writes, (
        f"the tick admitted nothing: {store.writes!r}"
    )

    # A capacity of zero is a coherent answer: "run nothing".
    store.writes.clear()
    sink.batches.clear()
    _write_settings(config_path, enabled=True, capacity=0, tick_seconds=0.01)
    engine._run_tick()

    assert store.writes == [], (
        "the tick admitted a plan although the capacity was zero: "
        f"{store.writes!r}"
    )
    assert sink.decisions() == [], (
        "the tick decided to start a plan although the capacity was "
        f"zero: {sink.decisions()!r}"
    )


def test_a_disabled_scheduler_does_not_open_the_database(tmp_path):
    """Nothing is opened while the switch is off.

    The engine binds itself to the database when the configuration says
    it may run — and not before.  "May not run" has to mean "nothing
    happens", and opening the database is something that happens.
    """
    config_path = tmp_path / "scheduler.yaml"
    _write_settings(config_path, enabled=False, capacity=1, tick_seconds=0.01)

    bound = []

    def _binder() -> Any:
        bound.append(True)
        raise AssertionError("the binder must not be called while off")

    facts = FakeFacts(waiting=[{"plan_id": "p1", "priority": 0, "due": True}])
    store = FakeStore()
    ledger = FakeLedger()
    sink = RecordingSink()
    engine = PlanSchedulerEngine(
        SchedulerConfigLoader(config_path),
        binder=_binder,
        clock=FakeClock(),
        thread_name=THREAD_NAME,
    )

    engine._run_tick()

    assert bound == [], (
        "the engine bound itself to the database while the scheduler was "
        "switched off"
    )
    assert engine.bound is False, (
        "the engine is bound although the scheduler may not run"
    )

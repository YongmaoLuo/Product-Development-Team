"""Integration tests: the dry run's artifacts, and what it must not touch.

Background
----------
The native scheduler replaces the old cron scheduler, but only after a
24 hour dry run proves the two behave the same.  The evidence of that
run is three files: ``ticks.jsonl`` (the decisions the new engine
recorded), ``windows.json`` (the half-hour windows the comparison is
made in) and ``diff.md`` (the report over those windows).

The dry run is *read-only about the plans* and *write-everything about
itself*, and these cases hold both halves of that down:

1. **The three artifacts exist.**  ``ticks.jsonl``, ``windows.json``
   and ``diff.md``, in the day's directory under the dry-run root.
2. **The tick lines are readable.**  One JSON object per line, with
   the four canonical fields.
3. **The windows are on the clock.**  A record at every half-hour
   boundary the run crossed — ``:00`` and ``:30``.
4. **Nothing about the plans moved.**  The phase and the version of
   every plan are what they were, and no process was started.
5. **The scheduler's own facts were still written.**  The queue keeps
   the plan's priority and its place in the line, and the slots are
   reconciled with the ledger — a dry run that dropped the queue would
   lose the priority, and a scheduler that lost its priority would come
   back after a restart with a different line than the one that was
   compared.

The cases drive the **production** engine — the real store, the real
ledger, the real facts reader and the real command executor, on a real
SQLite file with the production schema.  Only three things are the
case's own: the clock (moved by hand, so the window boundaries are
exact), the tick loop (driven one tick at a time, so a hundred ticks
are a hundred ticks) and the lifecycle (which writes down what it was
asked to do, and is never asked to do anything).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest

from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import CommandExecutor
from plan_scheduler.config import SchedulerConfig
from plan_scheduler.dryrun import (
    RECORD_FIELDS,
    DryRunWriter,
    ReferenceWindows,
    evaluate_windows,
)
from plan_scheduler.engine import PlanSchedulerEngine
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# The values the cases use
# ---------------------------------------------------------------------------

#: The moment every case starts at: a UTC midnight, and therefore a
#: window boundary.
BASE_EPOCH = datetime(2026, 10, 28, 0, 0, 0, tzinfo=timezone.utc).timestamp()

#: How far the clock moves between two ticks — half a minute, so a
#: hundred ticks cover fifty minutes and cross the ``:00`` boundary and
#: the ``:30`` boundary, and no other.
TICK_STEP = 30.0

#: How many ticks the dry run runs.
TICKS = 100

#: The plan the dry run admits.
PLAN_ID = "p1"

#: The priority the plan stands in the queue with.  A number the dry
#: run has to keep: a scheduler that forgot it would come back after a
#: restart with a different line than the one that was compared.
PRIORITY = 7

#: The version the plan's routing row carries before the dry run.  A
#: non-zero number, so a write that reset it would be visible.
VERSION = 11

#: The three artifacts a dry run leaves behind.
ARTIFACTS = ("ticks.jsonl", "windows.json", "diff.md")


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock the case moves by hand.

    It is callable, because that is what the ledger, the facts reader,
    the engine and the writer all ask for: ``clock()`` is "what time is
    it now".
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


class StaticConfigLoader:
    """A configuration loader that always answers the same snapshot."""

    def __init__(self, **overrides: Any) -> None:
        self.config = SchedulerConfig(**overrides)

    def current(self) -> SchedulerConfig:
        """Return the snapshot."""
        return self.config


class LifecycleResult:
    """What the lifecycle answers: the work was done."""

    def __init__(self, plan_id: str) -> None:
        self.plan_id = plan_id
        self.ok = True
        self.detail = "ok"


class RecordingLifecycle:
    """A lifecycle that writes down what it was asked to do.

    A dry run never asks it to do anything — that is the point of the
    dry run — so the list of calls is the evidence that no plan was
    started, stopped or moved.
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    def pause_plan(self, plan_id: str) -> LifecycleResult:
        """Record the pause and answer that it was done."""
        self.calls.append(("pause_plan", plan_id))
        return LifecycleResult(plan_id)

    def requeue_plan(self, plan_id: str) -> LifecycleResult:
        """Record the requeue and answer that it was done."""
        self.calls.append(("requeue_plan", plan_id))
        return LifecycleResult(plan_id)

    def stop_plan(self, plan_id: str, requeue: bool = False) -> LifecycleResult:
        """Record the stop and answer that it was done."""
        self.calls.append(("stop_plan", plan_id))
        return LifecycleResult(plan_id)


class DrivenEngine(PlanSchedulerEngine):
    """The engine, driven one tick at a time by the case.

    The worker thread ticks on real time, which cannot say where a
    window boundary falls.  The case needs the boundaries to be exact,
    so it drives the ticks itself — the same tick, the same phases, the
    same code path, with the clock under the case's hand.
    """

    def tick(self) -> None:
        """Run one tick, on this thread."""
        self._run_tick()


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    """Return the moment the way the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _open_db(path: Path) -> sqlite3.Connection:
    """Open a connection to the case's database with the production contract."""
    from state_machine.db.connection import open as open_db

    return open_db(path)


def _day_dir(out_dir: Path, moment: float = BASE_EPOCH) -> Path:
    """The directory the day's artifacts live in, as the contract says.

    The expectation is spelled out here rather than read back from the
    writer: a test that asks the code where it put its files proves
    nothing about where the files are supposed to go.
    """
    day = datetime.fromtimestamp(moment, tz=timezone.utc).strftime("%Y%m%d")
    return out_dir / day


def _child_pids() -> Optional[Set[int]]:
    """The pids of this process's children, or ``None`` when unreadable.

    The count is read from the process table: ``/proc`` where it
    exists, and the process list elsewhere.  The process list names the
    process that reads it, so that one is left out — otherwise the
    measurement would find the measuring instrument.
    """
    mine = os.getpid()
    children: Set[int] = set()

    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8")
            except OSError:
                continue
            # After the command name (which may itself hold a ")") the
            # fields are the state and then the parent's pid.
            fields = stat.rsplit(")", 1)[-1].split()
            if len(fields) >= 2 and fields[1].isdigit() and int(fields[1]) == mine:
                children.add(int(entry.name))
        return children

    try:
        reader = subprocess.Popen(
            ["ps", "-A", "-o", "pid=,ppid="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        output, _ = reader.communicate()
    except OSError:
        return None

    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[1].isdigit():
            continue
        if int(fields[1]) == mine and int(fields[0]) != reader.pid:
            children.add(int(fields[0]))
    return children


class World:
    """The world a dry run happens in.

    Attributes:
        engine: the engine under test.
        writer: the dry-run writer — the engine's sink.
        store: the scheduler store, the only writer of the scheduler's
            own facts.
        ledger: the capacity ledger — the authority on who holds
            compute.
        conn: the case's connection, on the same file the engine
            writes on.
        clock: the clock the case moves by hand.
        lifecycle: the lifecycle double — it records what it was asked
            to do, and a dry run never asks.
    """

    def __init__(
        self,
        engine: Any,
        writer: DryRunWriter,
        store: SqliteSchedulerStore,
        ledger: CapacityLedger,
        conn: sqlite3.Connection,
        clock: FakeClock,
        lifecycle: RecordingLifecycle,
    ) -> None:
        self.engine = engine
        self.writer = writer
        self.store = store
        self.ledger = ledger
        self.conn = conn
        self.clock = clock
        self.lifecycle = lifecycle

    def run(self, ticks: int = TICKS) -> "World":
        """Run the dry run: ``ticks`` ticks, half a minute apart."""
        for _ in range(ticks):
            self.engine.tick()
            self.clock.advance(TICK_STEP)
        return self

    def routing_rows(self) -> List[Tuple[Any, ...]]:
        """The plan routing rows, as they are right now.

        The phase and the version are the two facts a dry run must not
        move: a plan's phase is the lifecycle's to write, and its
        version is how a reader knows the row was rewritten.
        """
        rows = self.conn.execute(
            "SELECT plan_id, current_phase, version FROM plan_routing "
            "ORDER BY plan_id"
        ).fetchall()
        return [tuple(row) for row in rows]


def _world(
    db_factory: Any,
    plan_factory: Any,
    tmp_path: Path,
    out_dir: Path,
    start: float = BASE_EPOCH,
    reference: Any = None,
) -> World:
    """Build the world the dry run happens in.

    The plan is real and it is **queued** in the database — the phase
    lives in ``plan_routing`` — and it already stands in the queue with
    a priority, the way a plan that an earlier run enqueued would.

    ``reference`` is the comparison side: the old engine's recorded
    windows, read only.  Without it the dry run has nobody to compare
    itself with, and every window it records says so.
    """
    database = db_factory()
    plan_factory(plan_id=PLAN_ID, priority=PRIORITY)

    conn = _open_db(database.path)
    conn.execute(
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, version, updated_at) VALUES (?, ?, ?, ?)",
        (PLAN_ID, "queued", VERSION, _iso(BASE_EPOCH)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO scheduler_queue "
        "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (PLAN_ID, 1, PRIORITY, None, _iso(BASE_EPOCH), _iso(BASE_EPOCH)),
    )

    clock = FakeClock(start)
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, tmp_path / "plans", clock=clock)
    ledger = CapacityLedger(store, facts, clock)
    lifecycle = RecordingLifecycle()
    commands = CommandExecutor(
        store,
        ledger,
        lifecycle,
        facts=facts,
        conn=conn,
        capacity=1,
        min_runtime_seconds=0.0,
        clock=clock,
    )
    writer = DryRunWriter(
        out_dir,
        clock=clock,
        store=store,
        conn=conn,
        reference=reference,
        workspace_of=facts.workspace_key,
    )
    engine = DrivenEngine(
        config_loader=StaticConfigLoader(
            enabled=True,
            dry_run=True,
            capacity=1,
            min_runtime_seconds=0.0,
            tick_seconds=0.01,
            command_timeout_seconds=10.0,
        ),
        store=store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=writer,
        clock=clock,
        conn=conn,
    )
    return World(engine, writer, store, ledger, conn, clock, lifecycle)


def _dry_run(
    db_factory: Any,
    plan_factory: Any,
    tmp_path: Path,
    out_dir: Path,
    ticks: int = TICKS,
    start: float = BASE_EPOCH,
) -> World:
    """Build the world and run the dry run in it."""
    return _world(db_factory, plan_factory, tmp_path, out_dir, start).run(ticks)


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_three_artifacts_written(db_factory, plan_factory, tmp_path) -> None:
    """The dry run leaves the three artifacts behind in the day's directory."""
    out_dir = tmp_path / "dryrun"
    _dry_run(db_factory, plan_factory, tmp_path, out_dir)

    day = _day_dir(out_dir)
    assert day.is_dir(), "the day's directory was not created: {0}".format(day)
    for name in ARTIFACTS:
        path = day / name
        assert path.is_file(), "{0} was not written".format(path)
        assert path.stat().st_size > 0, "{0} is empty".format(path)

    # The report is about the day's artifacts, so it has to agree with
    # them: a report that is a window behind the files next to it is
    # worse than no report.
    ticks = (day / "ticks.jsonl").read_text(encoding="utf-8").splitlines()
    report = (day / "diff.md").read_text(encoding="utf-8")
    assert str(len(ticks)) in report, (
        "the report does not mention the {0} decisions the tick file "
        "holds".format(len(ticks))
    )


def test_tick_lines_parseable(db_factory, plan_factory, tmp_path) -> None:
    """Every tick line is one JSON object carrying the four canonical fields."""
    out_dir = tmp_path / "dryrun"
    world = _dry_run(db_factory, plan_factory, tmp_path, out_dir)

    path = _day_dir(out_dir) / "ticks.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines, "the dry run recorded no decision at all"

    for number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise AssertionError(
                "line {0} of {1} is not JSON: {2}".format(number, path, exc)
            ) from exc
        assert isinstance(record, dict), (
            "line {0} of {1} is not a JSON object".format(number, path)
        )
        missing = [field for field in RECORD_FIELDS if field not in record]
        assert not missing, (
            "line {0} of {1} is missing {2}".format(number, path, missing)
        )

    # One line per decision the tick made — the dry run decided on every
    # tick, and every decision was written down.
    assert len(lines) == TICKS, (
        "the dry run recorded {0} decisions in {1} ticks".format(
            len(lines), TICKS
        )
    )
    actions = {json.loads(line)["action"] for line in lines}
    assert actions == {"admit"}, (
        "the dry run decided something other than an admission: {0}".format(
            actions
        )
    )
    assert world.lifecycle.calls == [], (
        "the dry run asked the lifecycle to do something: {0}".format(
            world.lifecycle.calls
        )
    )


def test_window_records_at_half_hours(db_factory, plan_factory, tmp_path) -> None:
    """A window record sits on every half-hour boundary the run crossed."""
    out_dir = tmp_path / "dryrun"
    _dry_run(db_factory, plan_factory, tmp_path, out_dir)

    path = _day_dir(out_dir) / "windows.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, list), "the windows are not a list"
    assert payload, "the dry run recorded no window at all"

    starts = []
    for window in payload:
        assert isinstance(window, dict), "a window is not an object"
        for key in ("start", "end", "running", "queue"):
            assert key in window, "a window carries no {0}".format(key)
        starts.append(window["start"])

    minutes = {
        datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").minute for start in starts
    }
    assert minutes == {0, 30}, (
        "the windows are not on the half-hour boundaries: {0}".format(sorted(starts))
    )
    assert len(payload) == 2, (
        "the dry run crossed two boundaries and recorded {0} windows".format(
            len(payload)
        )
    )

    # The window carries the running set and the queue snapshot.
    first = payload[0]
    assert [row["plan_id"] for row in first["running"]] == [PLAN_ID], (
        "the window does not carry the running set: {0}".format(first["running"])
    )
    assert [row["plan_id"] for row in first["queue"]] == [PLAN_ID], (
        "the window does not carry the queue snapshot: {0}".format(first["queue"])
    )


def test_no_phase_or_process_change(db_factory, plan_factory, tmp_path) -> None:
    """The dry run moved no plan's phase, its version, and started no process."""
    out_dir = tmp_path / "dryrun"
    world = _world(db_factory, plan_factory, tmp_path, out_dir)

    before = world.routing_rows()
    children_before = _child_pids()

    world.run()

    after = world.routing_rows()
    assert after == before, (
        "the dry run rewrote the plan routing rows: {0} -> {1}".format(
            before, after
        )
    )
    assert world.lifecycle.calls == [], (
        "the dry run asked the lifecycle to do something: {0}".format(
            world.lifecycle.calls
        )
    )

    children_after = _child_pids()
    if children_before is not None and children_after is not None:
        started = children_after - children_before
        assert not started, (
            "the dry run started {0}".format(sorted(started))
        )


def test_scheduler_tables_still_written(db_factory, plan_factory, tmp_path) -> None:
    """The dry run wrote the scheduler's own facts, and they agree with the ledger."""
    out_dir = tmp_path / "dryrun"
    world = _dry_run(db_factory, plan_factory, tmp_path, out_dir)

    queue = world.store.list_queue(world.conn)
    slots = world.store.list_slots(world.conn)

    # The queue kept the plan's priority and its place in the line: the
    # dry run did not renumber the queue and did not drop the priority.
    assert [row["plan_id"] for row in queue] == [PLAN_ID], (
        "the dry run did not keep the plan in the queue: {0}".format(queue)
    )
    assert queue[0]["priority"] == PRIORITY, (
        "the dry run lost the plan's priority: {0}".format(queue[0])
    )
    assert queue[0]["seq"] == 1, (
        "the dry run renumbered the queue: {0}".format(queue[0])
    )

    # The slot was written too: the tick writes the slot before it
    # decides, so the decision is backed by a record.
    assert [row["plan_id"] for row in slots] == [PLAN_ID], (
        "the dry run wrote no slot: {0}".format(slots)
    )

    # And the tables agree with the ledger once it reconciles: nothing
    # holds compute in a dry run, so no slot row may survive.
    world.ledger.reconcile()
    assert world.ledger.used() == len(world.store.list_slots(world.conn)), (
        "the slot table and the ledger disagree"
    )
    assert world.store.list_slots(world.conn) == [], (
        "the dry run left a slot behind for a plan that holds nothing"
    )


def test_same_day_rerun_appends(db_factory, plan_factory, tmp_path) -> None:
    """A second run on the same day adds to the day's artifacts."""
    out_dir = tmp_path / "dryrun"

    _dry_run(db_factory, plan_factory, tmp_path, out_dir, ticks=2)
    day = _day_dir(out_dir)
    first_ticks = (day / "ticks.jsonl").read_text(encoding="utf-8")
    first_windows = json.loads((day / "windows.json").read_text(encoding="utf-8"))
    assert first_ticks.strip(), "the first run wrote no tick line"
    assert len(first_windows) == 1, (
        "the first run recorded {0} windows".format(len(first_windows))
    )

    # The second run is later on the same day, so its window is a
    # different one: the day's file has to hold both runs' windows, and
    # the first run's window has to be the one that is on record.
    _dry_run(
        db_factory,
        plan_factory,
        tmp_path,
        out_dir,
        ticks=2,
        start=BASE_EPOCH + 3600.0,
    )

    second_ticks = (day / "ticks.jsonl").read_text(encoding="utf-8")
    assert second_ticks.startswith(first_ticks), (
        "the second run overwrote the first run's ticks"
    )
    assert len(second_ticks.splitlines()) > len(first_ticks.splitlines()), (
        "the second run added no tick line"
    )

    second_windows = json.loads((day / "windows.json").read_text(encoding="utf-8"))
    assert second_windows[: len(first_windows)] == first_windows, (
        "the second run rewrote the first run's windows"
    )
    assert len(second_windows) == len(first_windows) + 1, (
        "the second run's window was not added: {0}".format(
            [window["start"] for window in second_windows]
        )
    )


# ---------------------------------------------------------------------------
# 6. The comparison: what a window says when there is nobody to compare it with
# ---------------------------------------------------------------------------


def test_a_window_without_a_comparison_side_says_so(
    db_factory, plan_factory, tmp_path
) -> None:
    """A window nobody compared is recorded as *not compared*.

    The writer holds one side of the comparison — the new engine's.
    A window whose other side is missing carries no verdict, and it
    says so: a window that claimed to be consistent without a
    comparison would let a run that compared nothing through the gate.
    """
    out_dir = tmp_path / "dryrun"
    _dry_run(db_factory, plan_factory, tmp_path, out_dir)

    windows = json.loads(
        (_day_dir(out_dir) / "windows.json").read_text(encoding="utf-8")
    )
    assert windows, "the dry run recorded no window at all"

    for window in windows:
        assert window["compared"] is False, (
            "a window without a comparison side claims to be compared: "
            "{0}".format(window)
        )
        assert window["differences"] == [], (
            "a window without a comparison side carries differences: "
            "{0}".format(window)
        )
        assert window.get("consistent") is not True, (
            "a window without a comparison side claims to be consistent: "
            "{0}".format(window)
        )

    verdict = evaluate_windows(windows)
    assert verdict.consistent == 0, (
        "a run that compared nothing was counted as consistent: {0}".format(verdict)
    )
    assert verdict.uncompared == len(windows)
    assert verdict.passed is False


def test_a_window_compared_with_the_old_engine_carries_the_difference(
    db_factory, plan_factory, tmp_path
) -> None:
    """A window that was compared carries the difference the comparison found.

    The old engine's window for the same period is the comparison
    side.  Here it holds no running plan, while the new engine had
    already admitted one — so the comparison found a new start, and
    the window says so, in the vocabulary the judge understands.
    """
    out_dir = tmp_path / "dryrun"
    reference = ReferenceWindows(
        [
            {
                "start": "2026-10-28T00:00:00Z",
                "running": [],
                "queue": [{"plan_id": PLAN_ID}],
            }
        ]
    )

    world = _world(
        db_factory,
        plan_factory,
        tmp_path,
        out_dir,
        reference=reference,
    )
    # Two windows: the clock has to cross the half-hour boundary for
    # the second one to be recorded.
    world.run(ticks=61)

    windows = json.loads(
        (_day_dir(out_dir) / "windows.json").read_text(encoding="utf-8")
    )
    first = windows[0]
    assert first["compared"] is True, (
        "the window with a comparison side is not marked as compared: "
        "{0}".format(first)
    )

    kinds = [difference["kind"] for difference in first["differences"]]
    assert "new_start" in kinds, (
        "the comparison did not find the new start: {0}".format(first["differences"])
    )
    for difference in first["differences"]:
        assert difference.get("explanation"), (
            "a difference without a written explanation: {0}".format(difference)
        )

    # The window the reference has nothing for is still recorded as
    # *not compared* — the comparison side is not made up.
    later = [window for window in windows if window["start"] != first["start"]]
    assert later, "the dry run recorded only one window"
    for window in later:
        assert window["compared"] is False


def test_a_window_that_matches_the_old_engine_carries_no_difference(
    db_factory, plan_factory, tmp_path
) -> None:
    """A window the comparison found nothing in is consistent.

    This is the shape a healthy dry run has: the two engines agree on
    the window, and the window says it was compared.
    """
    out_dir = tmp_path / "dryrun"
    reference = ReferenceWindows(
        [
            {
                "start": "2026-10-28T00:00:00Z",
                "running": [{"plan_id": PLAN_ID}],
                "queue": [{"plan_id": PLAN_ID}],
            }
        ]
    )

    world = _world(
        db_factory,
        plan_factory,
        tmp_path,
        out_dir,
        reference=reference,
    )
    world.run(ticks=2)

    windows = json.loads(
        (_day_dir(out_dir) / "windows.json").read_text(encoding="utf-8")
    )
    first = windows[0]
    assert first["compared"] is True
    assert first["differences"] == [], (
        "the comparison found a difference in two identical windows: "
        "{0}".format(first["differences"])
    )
    assert first["consistent"] is True


# ---------------------------------------------------------------------------
# 7. The production wiring: the dry run the backend really runs
# ---------------------------------------------------------------------------


def test_the_production_dry_run_leaves_the_three_artifacts(
    db_factory, plan_factory, tmp_path, monkeypatch
) -> None:
    """The dry run the *backend* runs leaves the three artifacts behind.

    The gate in the cutover runbook is replayed against what the
    production path produced, so the production path has to produce
    it.  A dry run that only wrote a decision log would make the gate
    exit 2 — "the input file is missing" — on every single run.
    """
    import server
    from plan_scheduler import dryrun as dryrun_module

    database = db_factory()
    plan = plan_factory(plan_id=PLAN_ID, priority=PRIORITY)
    conn = _open_db(database.path)
    conn.execute(
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, version, updated_at) VALUES (?, ?, ?, ?)",
        (PLAN_ID, "queued", VERSION, _iso(BASE_EPOCH)),
    )
    conn.commit()
    conn.close()

    out_dir = tmp_path / "dryrun"
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "scheduler:\n"
        "  enabled: true\n"
        "  dry_run: true\n"
        "  capacity: 1\n"
        "  min_runtime_seconds: 600\n"
        "  tick_seconds: 0.05\n"
        "  command_timeout_seconds: 30\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(server, "PLANS_DIR", plan.plan_dir.parent)
    monkeypatch.setattr(server, "_scheduler_settings_path", lambda: settings)
    monkeypatch.setattr(dryrun_module, "DEFAULT_DRYRUN_ROOT", str(out_dir))

    runtime = server._build_scheduler_engine()
    engine = runtime.engine
    assert engine is not None, "the production wiring did not build the engine"
    try:
        time.sleep(0.5)
    finally:
        engine.stop()
        if runtime.conn is not None:
            runtime.conn.close()

    day = _day_dir(out_dir, time.time())
    assert day.is_dir(), "the day's directory was not created: {0}".format(day)
    for name in ARTIFACTS:
        path = day / name
        assert path.is_file(), "{0} was not written".format(path)
        assert path.stat().st_size > 0, "{0} is empty".format(path)

    # The tick file holds the decisions the dry run made, and the
    # windows file holds the windows it covered.
    ticks = [
        line
        for line in (day / "ticks.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert ticks, "the production dry run recorded no decision at all"

    windows = json.loads((day / "windows.json").read_text(encoding="utf-8"))
    assert windows, "the production dry run recorded no window at all"
    for window in windows:
        for key in ("start", "end", "running", "queue"):
            assert key in window, "a window carries no {0}".format(key)

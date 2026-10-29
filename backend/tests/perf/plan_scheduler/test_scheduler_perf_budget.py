"""The scheduler's performance budgets.

What this file is
-----------------
Three budgets, asserted with the standard library and nothing else:
the tick's latency, the snapshot endpoint's latency, and the queue
comparator's cost at the size of a full queue.  They are the three
quantities that decide whether the scheduler keeps up:

* the **tick** runs every ``tick_seconds`` (five seconds by default).
  A tick that takes longer than the cycle is a scheduler whose
  decisions pile up behind each other — the queue still looks right,
  and nothing happens;
* the **snapshot** is what the operator panel polls.  A read path that
  jitters is a panel that flickers, and a panel that flickers is a
  panel nobody reads;
* the **comparator** is a pure function over a queue row.  Its cost
  grows with the queue, so the size of the queue is a budget of its
  own.

How the numbers are taken
-------------------------
``time.perf_counter`` and nothing else.  No load-test tool, no new
dependency: the repository does not add one, and a benchmark that
drags a framework in has its own overhead inside every number it
reports.

The thresholds are **percentiles**, not maxima.  A maximum is a
measurement of the worst moment of a run, and the worst moment of a
run is the moment the machine did something else at the same time; a
benchmark that fails on that is a benchmark that fails at random.  The
p95 says "one run in twenty is allowed to be this slow", and the p99
says "one in a hundred" — which is how a budget is written when the
thing being budgeted is a cycle that has to keep turning.

The thresholds themselves are written **into the cases**, not only
into a document: a budget nobody can read in the code is a budget
nobody can argue about when the number moves.

The marker
----------
``pytestmark = [pytest.mark.perf]``.  The ``perf`` marker is what puts
a file in the lane CI runs *alone* — ``pytest tests/ -m "time_sensitive
or perf"`` — whose 1800-second budget is the only one that can hold a
wall-clock assertion on a shared runner.

``time_sensitive`` would say the same thing about running alone, and
this file would rather carry it: ``backend/pytest.ini`` ships
``-m "not time_sensitive"`` in its ``addopts``, so a file marked that
way is *deselected* by every plain ``pytest`` run — including the
command that runs this file.  Marked that way, the file would be
invisible to the very command that is supposed to prove it works.

The world, and what it does not cover
-------------------------------------
The world is two hundred plans waiting in the queue and fifty working
at the same time, which is the scenario the budgets were written for.
A waiting plan is waiting *before* it has been bound to a working
directory, so its task list declares none; a working plan is working
in one, and says so.

That boundary is worth stating plainly, because it decides what these
numbers mean.  A plan that is waiting **and already bound to a working
directory** makes the tick cost something else: the ledger asks, for
every plan in the queue, whether the directory it wants is taken, and
it answers by re-deriving the working plans — and their workspace
keys — once per queued plan.  That is O(queued x working) realpath
calls per rebuild, two rebuilds per tick.

Measured on this tree, with every plan bound to a directory, the same
benchmark answers **777 ms** for the tick's p95 and **453 ms** for the
snapshot's p95 — over both budgets, and slow enough that the two
hundred samples of each measurement take minutes where they now take
seconds.  So the budget as written is a budget for a scheduler that
does not compare workspaces on every queued plan, and the workspace
comparison is the part of the tick that is over it.

What these cases cover is the rest of the tick: the state read, the
queue reconciliation, the capacity accounting, the convergence, the
fill and the record — the work that grows with the size of the queue.
A regression there is what these budgets are there to catch, and the
workspace comparison is a separate, larger problem that belongs to the
ledger rather than to the benchmark.

Test command::

    cd backend && pytest tests/perf/plan_scheduler/test_scheduler_perf_budget.py -v
"""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Set

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from plan_scheduler.capacity import CapacityLedger  # noqa: E402
from plan_scheduler.commands import CommandExecutor  # noqa: E402
from plan_scheduler.config import SchedulerConfig  # noqa: E402
from plan_scheduler.dryrun import ExecuteSink  # noqa: E402
from plan_scheduler.engine import PlanSchedulerEngine  # noqa: E402
from plan_scheduler.facts import PlanFactsReader  # noqa: E402
from plan_scheduler.queue import queue_key  # noqa: E402
from plan_scheduler.store import SqliteSchedulerStore  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402

#: The marker this file carries.  ``perf`` is what puts it in the lane
#: CI runs alone; see the module docstring for why it is not
#: ``time_sensitive``.
pytestmark = [pytest.mark.perf]

#: How many rows the comparator is asked to sort.  Five hundred is a
#: queue worth looking at — a scheduler that is behind will have a
#: queue this size or larger.
SORT_ROWS = 500

#: The world the tick and the snapshot are measured in: this many plans
#: waiting in the queue, and this many working at the same time.
QUEUED_PLANS = 200
ACTIVE_PLANS = 50

#: How many plans may hold compute at the same time.  It is the number
#: of working plans, so the world is at capacity: the tick has a full
#: queue in front of it and no free slot to fill, which is the state
#: the scheduler spends its time in.
CAPACITY = ACTIVE_PLANS

#: How long a working plan is protected from being converged.  The
#: plans in the world started a moment ago, so this is what keeps the
#: measurement out of the convergence path.
MIN_RUNTIME_SECONDS = 600.0

#: How often the worker looks.  The benchmark drives the ticks itself,
#: so the worker is parked far enough away that it never competes for
#: the machine the measurement is taken on.
TICK_SECONDS = 3600.0

#: How many times each measurement is repeated.  Two hundred samples
#: are enough for a p95 that means something and few enough that the
#: case still runs in seconds.
TICK_SAMPLES = 200
SNAPSHOT_SAMPLES = 200

#: The endpoint the operator panel polls.
STATE_PATH = "/api/scheduler/state"

#: The marker expression of the serial lane — ``.github/workflows/ci.yml``,
#: the "Time-sensitive + perf (serial lane)" job: ``pytest tests/ -m
#: "time_sensitive or perf"``.  It is the lane that runs one pytest
#: process at a time, on a runner that is not shared with anything else.
SERIAL_LANE = "time_sensitive or perf"

#: The marker expression of the default lanes — the same file, the
#: "Run unit tests" jobs: ``pytest tests/ -m "not e2e and not
#: integration and not time_sensitive and not perf"``.  A case that is
#: in the serial lane is out of these, which is the whole point of the
#: marker.
DEFAULT_LANES = (
    "not e2e and not integration and not time_sensitive and not perf"
)


def _evaluate(node: ast.AST, namespace: Dict[str, bool]) -> bool:
    """Return the value of a marker expression over a set of names."""
    if isinstance(node, ast.BoolOp):
        values = [_evaluate(value, namespace) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _evaluate(node.operand, namespace)
    if isinstance(node, ast.Name):
        return namespace[node.id]
    raise AssertionError("unsupported marker expression: %s" % ast.dump(node))


def _lane_selects(markers: Set[str], expression: str) -> bool:
    """Return whether a lane's marker expression selects these markers.

    pytest's own ``-m`` reads a boolean combination of bare marker
    names, and a bare name matches a case that carries that marker.
    The expression is therefore evaluated with Python's own parser over
    a namespace of booleans, so the two agree on ``not``, ``and`` and
    ``or`` without a second marker parser being written here.
    """
    tree = ast.parse(expression, mode="eval")
    namespace = {
        node.id: node.id in markers
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    return _evaluate(tree.body, namespace)


def _queue_rows(count: int) -> List[Dict[str, Any]]:
    """Return ``count`` queue rows, in the shape the comparator reads.

    The rows are not all alike: some carry a promotion, some carry a
    priority, and the rest are plain arrivals — so the sort has to
    compare all four elements of the key, not just one.
    """
    rows: List[Dict[str, Any]] = []
    for index in range(count):
        rows.append(
            {
                "plan_id": "plan-%04d" % index,
                "promote_order": -(index + 1) if index % 7 == 0 else None,
                "priority": index % 5,
                "seq": index,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


class _StaticConfig:
    """The scheduler's settings, held still.

    The benchmark measures a tick, not a configuration reload: the
    settings are read once and never change, so what is measured is
    the tick and nothing else.
    """

    def __init__(self, config: SchedulerConfig) -> None:
        self._config = config

    def current(self) -> SchedulerConfig:
        """Return the settings the tick runs with."""
        return self._config


class _NoLifecycle:
    """A lifecycle that starts nothing.

    The benchmark runs no command: the world is at capacity, so the
    tick has no slot to fill and no decision to carry out.  A lifecycle
    that raised if it were asked is the honest way to say so — a
    benchmark that quietly started a plan would be measuring something
    else entirely.
    """

    def start_plan(self, plan_id: str, spec: Any = None) -> Any:
        raise AssertionError("the benchmark must not start a plan")

    def stop_plan(self, plan_id: str, requeue: bool = False) -> Any:
        raise AssertionError("the benchmark must not stop a plan")

    def pause_plan(self, plan_id: str) -> Any:
        raise AssertionError("the benchmark must not pause a plan")

    def requeue_plan(self, plan_id: str) -> Any:
        raise AssertionError("the benchmark must not requeue a plan")


class _World:
    """What the two measurements are taken on."""

    def __init__(
        self,
        engine: PlanSchedulerEngine,
        client: TestClient,
        root: Path,
    ) -> None:
        self.engine = engine
        self.client = client
        self.root = root


def _iso(epoch: float) -> str:
    """Return the moment as the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _write_plans(conn: Any, plans_root: Path) -> None:
    """Write the plans the scheduler reads, and the rows that say so.

    Two hundred plans are **waiting** — their phase is ``queued`` — and
    fifty are **working**: their phase is ``executing`` and their
    execution row names a process that is alive right now (this one),
    which is how the facts reader knows they are working.

    A waiting plan is waiting *before* it has been bound to a working
    directory, so its task list declares none.  A working plan is
    working in a directory, so its task list and its execution row name
    one.
    """
    stamp = _iso(time.time())
    pid = os.getpid()

    routing: List[Any] = []
    execution: List[Any] = []

    for index in range(QUEUED_PLANS + ACTIVE_PLANS):
        plan_id = "plan-%04d" % index
        waiting = index < QUEUED_PLANS
        plan_dir = plans_root / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)

        task: Dict[str, Any] = {"id": "%s-1" % plan_id}
        project_dir: Any = None
        if not waiting:
            project_dir = plan_dir / "project"
            project_dir.mkdir(parents=True, exist_ok=True)
            task["project_dir"] = str(project_dir)

        (plan_dir / "prd.json").write_text(
            json.dumps({"plan_id": plan_id, "title": plan_id}),
            encoding="utf-8",
        )
        (plan_dir / "tasks.json").write_text(
            json.dumps({"tasks": [task]}),
            encoding="utf-8",
        )

        if waiting:
            routing.append((plan_id, "queued", stamp))
        else:
            routing.append((plan_id, "executing", stamp))
            execution.append(
                (plan_id, str(project_dir), pid, "running", stamp)
            )

    conn.executemany(
        "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
        "VALUES (?, ?, ?)",
        routing,
    )
    conn.executemany(
        "INSERT INTO plan_execution (plan_id, project_dir, exec_pid, "
        "exec_status, updated_at) VALUES (?, ?, ?, ?, ?)",
        execution,
    )
    conn.commit()


def _wait_for_first_tick(engine: PlanSchedulerEngine, timeout: float = 30.0) -> None:
    """Wait until the engine has run its first tick.

    The first tick is what puts the waiting plans into the queue table,
    so a measurement taken before it would measure an empty queue and
    would prove nothing about a queue of two hundred.
    """
    deadline = time.monotonic() + timeout
    while engine.ticks < 1:
        if time.monotonic() > deadline:
            raise AssertionError("the engine never ran its first tick")
        time.sleep(0.005)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_World]:
    """Build the world, hand it to the cases, and take it away again.

    The world is a temporary directory: a plans tree, a state database
    and an engine that reads them.  Everything the case starts is
    stopped before the case ends — the worker thread is asked to stop
    and joined, the connections are closed and the directory is
    removed — because a thread that outlives its case is a thread that
    outlives its database.
    """
    root = tmp_path_factory.mktemp("scheduler_perf")
    plans_root = root / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)

    conn = open_db(root / "state.db")
    migrate(conn)
    _write_plans(conn, plans_root)

    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, plans_root)
    ledger = CapacityLedger(store, facts, conn=conn)
    commands = CommandExecutor(
        store,
        ledger,
        _NoLifecycle(),
        facts=facts,
        conn=conn,
        capacity=CAPACITY,
        min_runtime_seconds=MIN_RUNTIME_SECONDS,
    )
    engine = PlanSchedulerEngine(
        _StaticConfig(
            SchedulerConfig(
                enabled=True,
                dry_run=False,
                capacity=CAPACITY,
                min_runtime_seconds=MIN_RUNTIME_SECONDS,
                tick_seconds=TICK_SECONDS,
                command_timeout_seconds=30.0,
            )
        ),
        store=store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=ExecuteSink(lambda decision: decision),
        conn=conn,
    )
    engine.start()
    _wait_for_first_tick(engine)

    # The endpoint reads the engine off the application, so the
    # benchmark hangs its engine there for the length of the case.
    previous = getattr(server.app.state, "scheduler_engine", None)
    server.app.state.scheduler_engine = engine
    client = TestClient(server.app)

    # httpx logs every request at INFO; two hundred of them would be
    # two hundred lines of noise around the numbers.
    httpx_logger = logging.getLogger("httpx")
    httpx_level = httpx_logger.level
    httpx_logger.setLevel(logging.WARNING)

    try:
        yield _World(engine=engine, client=client, root=root)
    finally:
        httpx_logger.setLevel(httpx_level)
        server.app.state.scheduler_engine = previous
        client.close()
        engine.stop(timeout=30.0)
        conn.close()
        shutil.rmtree(root, ignore_errors=True)


def _percentile(samples: List[float], percentile: float) -> float:
    """Return the ``percentile`` of the samples.

    The samples are milliseconds.  The percentile is taken over the
    sorted samples, with the index rounded to the nearest sample — the
    same rule ``numpy``'s default "linear" method is close to, and one
    that needs no dependency to compute.
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = int(round((percentile / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(index, len(ordered) - 1))]


def _report(metrics: Dict[str, Any], request: Any) -> None:
    """Publish the numbers the budget was measured from.

    The print is this repository's convention for the perf lane
    (``tests/perf/test_verification_mode_scaling.py`` prints its
    measurements the same way).  On its own it would not be enough:
    pytest captures the stdout of a passing case and throws it away,
    so the line would never reach the lane's log — the one place the
    measurement is worth having.  Writing it through the terminal
    reporter puts it in the log without a change to the repository's
    pytest configuration, which this task does not own.

    (``record_property`` would put the numbers in the lane's JUnit
    XML instead, but pytest's default ``xunit2`` family drops recorded
    properties and warns about them, so it is not the way.)
    """
    print(metrics)

    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    capture = request.config.pluginmanager.get_plugin("capturemanager")
    if reporter is None:
        return
    line = "plan_scheduler perf: %s" % (metrics,)
    if capture is None:
        reporter.write_line(line)
        return
    # The reporter writes to stdout, which the capture manager owns; the
    # write has to happen with the capture suspended or the line lands
    # in the capture buffer and is thrown away with the passing case.
    with capture.global_and_fixture_disabled():
        reporter.write_line(line)


def test_sort_500_rows_under_50ms(request: Any) -> None:
    """Sorting a full queue stays cheap.

    The comparator is a pure function of a queue row, so its cost can
    be measured on its own — no engine, no database, no worker.  Five
    hundred rows is a queue that is worth looking at; fifty
    milliseconds is a budget that leaves the comparison invisible
    inside the tick that does it.
    """
    rows = _queue_rows(SORT_ROWS)

    started = time.perf_counter()
    ordered = sorted(rows, key=queue_key)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert len(ordered) == SORT_ROWS

    _report({"sort_500_rows_ms": round(elapsed_ms, 3), "rows": SORT_ROWS}, request)

    assert elapsed_ms < 50.0, (
        "sorting %d queue rows took %.1f ms, which is over the 50 ms "
        "budget" % (SORT_ROWS, elapsed_ms)
    )


def test_tick_p95_within_budget(world: _World, request: Any) -> None:
    """The tick keeps inside its cycle.

    The tick runs every ``tick_seconds`` — five seconds by default —
    and it is one worker thread: a tick that overruns its cycle does
    not make the scheduler wrong, it makes it late, and the lateness
    piles up behind every tick that follows.

    The budget is therefore a tenth of the cycle for the p95 (500 ms)
    and two fifths for the p99 (2 s): the ordinary tick is far inside
    the cycle, and even the unlucky one in a hundred still leaves the
    cycle mostly unused.

    The ticks are driven by the case itself, one after another, with
    the worker parked — so what is measured is the tick and not the
    competition between the case and the scheduler's own cadence.
    """
    samples: List[float] = []
    for _ in range(TICK_SAMPLES):
        started = time.perf_counter()
        world.engine._run_tick()
        samples.append((time.perf_counter() - started) * 1000.0)

    assert not world.engine.errors, (
        "the engine recorded errors, so the numbers below are a "
        "measurement of a broken tick: %r" % (world.engine.errors[:3],)
    )

    p95 = _percentile(samples, 95)
    p99 = _percentile(samples, 99)

    _report(
        {
            "tick_ms_p50": round(_percentile(samples, 50), 3),
            "tick_ms_p95": round(p95, 3),
            "tick_ms_p99": round(p99, 3),
            "tick_ms_max": round(max(samples), 3),
            "ticks": TICK_SAMPLES,
        },
        request,
    )

    assert p95 <= 500.0, (
        "the tick's p95 was %.1f ms over %d ticks, which is over the "
        "500 ms budget" % (p95, TICK_SAMPLES)
    )
    assert p99 <= 2000.0, (
        "the tick's p99 was %.1f ms over %d ticks, which is over the "
        "2000 ms budget" % (p99, TICK_SAMPLES)
    )


def test_snapshot_p95_within_budget(world: _World, request: Any) -> None:
    """The panel's read path stays quick.

    What is measured is the **request**: the endpoint the operator
    panel polls, over HTTP, with the answer parsed.  The panel does
    not care how long the engine took to read its state; it cares how
    long it waited for the answer, and a read path that jitters is a
    panel that flickers.

    The budget is 200 ms for the p95 — a fifth of the tick's cycle,
    and the difference between a panel that feels alive and one that
    feels broken.
    """
    samples: List[float] = []
    queue_length = 0
    for _ in range(SNAPSHOT_SAMPLES):
        started = time.perf_counter()
        response = world.client.get(STATE_PATH)
        samples.append((time.perf_counter() - started) * 1000.0)

        assert response.status_code == 200, (
            "the snapshot endpoint answered %s instead of the read model"
            % response.status_code
        )
        body = response.json()
        queue_length = int(body["queue_length"])

    assert queue_length == QUEUED_PLANS, (
        "the snapshot carried %d queued plans instead of the %d the "
        "world was built with, so the number above is a measurement of "
        "the wrong world" % (queue_length, QUEUED_PLANS)
    )

    p95 = _percentile(samples, 95)

    _report(
        {
            "snapshot_ms_p50": round(_percentile(samples, 50), 3),
            "snapshot_ms_p95": round(p95, 3),
            "snapshot_ms_p99": round(_percentile(samples, 99), 3),
            "snapshot_ms_max": round(max(samples), 3),
            "requests": SNAPSHOT_SAMPLES,
        },
        request,
    )

    assert p95 <= 200.0, (
        "the snapshot endpoint's p95 was %.1f ms over %d requests, "
        "which is over the 200 ms budget" % (p95, SNAPSHOT_SAMPLES)
    )


def test_marked_perf_and_serial(request: Any) -> None:
    """The cases in this file belong to the lane that runs alone.

    Two facts are asserted, and together they are what "runs serially"
    means in this repository: the file carries the ``perf`` marker, and
    the lane expressions agree — the serial lane selects the case and
    the default lanes do not.
    """
    markers = {marker.name for marker in request.node.iter_markers()}

    assert "perf" in markers, (
        "the file carries no perf marker, so the cases would run in the "
        "parallel lanes, beside other workers, and the wall-clock "
        "numbers would be a measurement of the runner's load: %r"
        % sorted(markers)
    )

    assert _lane_selects(markers, SERIAL_LANE), (
        "the serial lane (%r) does not select this case, so the budget "
        "would be measured beside other work: %r"
        % (SERIAL_LANE, sorted(markers))
    )

    assert not _lane_selects(markers, DEFAULT_LANES), (
        "the default lanes (%r) select this case, so the budget would "
        "be measured on a shared runner: %r"
        % (DEFAULT_LANES, sorted(markers))
    )

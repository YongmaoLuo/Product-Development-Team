"""The four negative controls: the old behaviour, put back on purpose.

Why this file exists
--------------------
R1 to R4 each changed one thing, and each change has a **seam** — the
one place where the old behaviour can be put back:

===========  ====================================  ===========================
Fix          The seam                              The old behaviour
===========  ====================================  ===========================
R1a          ``read_conn=None`` on the engine      the read model read on the
                                                   write connection
R2a          ``ledger._writes_needed``             the ledger took SQLite's
                                                   write lock on every tick
R3           ``commands._is_locked_error``         a locked queue write was a
                                                   fault instead of a 2xx
R4b          ``engine._synthesized_queued_rows``   a waiting plan without a
                                                   queue row was invisible
===========  ====================================  ===========================

Each case here **forces** the old behaviour through its seam and asserts
that the old behaviour is *observable*: a duration, a number of write
transactions, an exception, a row that is not in the line.  A seam that
changes nothing is a seam that does not matter, and a fix whose seam
changes nothing cannot be shown to have been a fix.

Each case then **restores** the seam and asserts that the new behaviour
is there: the read model reads the connection of its own, the steady
state takes no write transaction, the locked write degrades, the waiting
plan without a row is in the line.

So every case is **discriminating**: it says which behaviour the code
has, and it says so twice — once for the old behaviour and once for the
new one — on the same world.  The seam is restored by the case itself
(``monkeypatch`` puts every patch back when the case ends), and no
production default is touched: the old behaviour is reached by narrowing
the seam, never by changing what the product does.

How the four cases are bounded
------------------------------
* **NC1** waits for SQLite's write lock, and the wait is bounded by the
  write connection's ``busy_timeout``: 300 milliseconds.  The case
  asserts a **floor** on the elapsed time, not a ceiling: a floor gets
  *more* comfortable when the machine is loaded, so the case does not
  measure the runner's load the way an upper bound would.
* **NC2** counts write transactions; it opens one.
* **NC3** raises an exception; it does not wait.
* **NC4** reads a document; it does not write.

Only NC1 touches the clock, and only through a floor.

One connection, one caller at a time
------------------------------------
NC1 needs **one connection and two threads**: the worker waits for the
write lock on it while the panel's read goes to the same connection.
SQLite's own rule is that a connection must not be used by two threads
at the same time, and CPython's ``sqlite3`` module does not enforce it
— ``check_same_thread=False`` lifts only the *thread identity* check —
and SQLite's busy handler lets the second thread into the connection
while the first one is asleep waiting for a lock.  Two threads inside
one connection is undefined behaviour, and the answer comes out
differently each time: the waiting statement comes back with SQLite's
own lock error, or with an error of another kind, or with no error at
all while no transaction was ever opened.

So the case makes the rule explicit and the answer reproducible: the
connection is handed to **one caller at a time** (see
:class:`_OneCallerConnection`).  The wait that this produces is the
wait the panel really suffers — the connection is busy with the worker,
so the panel's read on it comes back after the worker's call is over —
and the worker's own call is now alone inside SQLite, so the lock
failure it comes back with is SQLite's own, every time.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from plan_scheduler import commands as commands_module
from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import APPLIED, CommandExecutor
from plan_scheduler.config import SchedulerConfig
from plan_scheduler.engine import PlanSchedulerEngine
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.store import SqliteSchedulerStore
from state_machine.db.schema import migrate

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The bounds the cases work within
# ---------------------------------------------------------------------------

#: A fixed moment, so that no case depends on the wall.
BASE_EPOCH = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc).timestamp()

#: How long the write connection waits for SQLite's write lock before it
#: gives up.  The wait is bounded, so NC1 cannot hang: the worker comes
#: back with SQLite's own answer, and the read that was waiting for it
#: comes back too.
WRITER_BUSY_TIMEOUT_MS = 300

#: The floor the blocked read has to reach.  The worker's wait is longer
#: than this — SQLite goes round the lock more than once before it gives
#: up — and the floor is well below it, so a read that came back in a
#: millisecond did not wait for anything.
BLOCKED_READ_FLOOR_SECONDS = 0.15

#: How much earlier than the worker the read is allowed to come back.
#: The read is released by the very call the worker is inside, so it may
#: come back a hair before the worker has recorded that it is over; the
#: tolerance is for that hair and for nothing else.
WORKER_WAIT_TOLERANCE_SECONDS = 0.05

#: The detail a degraded answer carries: the plan is queued, the queue
#: row is still missing, the reconciler will put it there.
PENDING_RECONCILE = "queued_pending_reconcile"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso(epoch: float = BASE_EPOCH) -> str:
    """Return the moment as the state database writes timestamps."""
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _queue_ids(document: Dict[str, Any]) -> List[str]:
    """The plan ids of a document's line, in the order it shows them."""
    return [str(row.get("plan_id") or "") for row in document.get("queue", [])]


class _ConfigLoader:
    """A loader that hands out one immutable snapshot."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def current(self) -> Any:
        """The configuration snapshot, unchanged."""
        return self._config


class _Clock:
    """A clock the case reads; it never moves."""

    def __init__(self, now: float = BASE_EPOCH) -> None:
        self._now = float(now)

    def __call__(self) -> float:
        """Return the current moment, in epoch seconds."""
        return self._now


class _OneCallerConnection(sqlite3.Connection):
    """A connection that serves one caller at a time.

    SQLite's own rule: **a connection must not be used by two threads
    at the same time**.  CPython's ``sqlite3`` module does not enforce
    it — ``check_same_thread=False`` lifts only the *thread identity*
    check — and SQLite's busy handler lets a second thread into the
    connection while the first one is asleep inside the busy wait.

    Two threads inside one connection is undefined behaviour, and the
    answer comes out differently each time.  Measured on this case,
    before the rule was made explicit: the case failed about half the
    time, and the waiting statement came back with SQLite's own
    ``database is locked`` about half the time, with ``no more rows
    available`` (SQLite's ``SQLITE_DONE`` reported as an error) some
    times, and with **no error at all** the rest of the time — the
    statement said it had begun a transaction that was never begun, the
    connection not being in a transaction afterwards.

    The rule is made explicit here so that the case measures the
    connection and not the undefined behaviour: the connection is
    handed to one caller at a time.  The wait this produces is the wait
    the panel really suffers — the connection is busy with the worker,
    so the panel's read on it comes back after the worker's call is
    over — and the worker's call is alone inside SQLite while it
    waits, so the failure it comes back with is SQLite's own, every
    time.

    Only :meth:`execute` is gated, because that is the call the case
    makes.  The cursor it hands back is stepped by the caller that got
    it, and that caller already held the connection when it asked for
    the cursor: the other thread is through by then.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Open the connection and give it its gate."""
        super().__init__(*args, **kwargs)
        self._gate = threading.RLock()

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        """Run one statement with the connection to this caller alone."""
        with self._gate:
            return super().execute(*args, **kwargs)


def _open_one_caller(path: Path) -> sqlite3.Connection:
    """Open a database with the same contract as the product's open().

    The contract is the one
    :func:`state_machine.db.connection.open` pins down — WAL, a busy
    timeout, ``synchronous = NORMAL`` and autocommit — with
    :class:`_OneCallerConnection` in place of the plain connection.
    The only difference is the gate, and the paths are always files
    this case made under ``tmp_path``, so the operator's live database
    is not in reach.
    """
    conn = sqlite3.connect(
        str(path),
        isolation_level=None,
        timeout=30.0,
        check_same_thread=False,
        factory=_OneCallerConnection,
    )
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# ---------------------------------------------------------------------------
# NC1 — the read model and the write model share one connection
# ---------------------------------------------------------------------------


class _ReadPathWorld:
    """Two databases, a worker that waits for the write lock, and the lines.

    The write database and the read database are **two files**, so the
    only way a plan can be in the document is for the read model to have
    read the connection that holds it.  One database would answer the
    same either way, and the case would prove nothing.

    The write database carries three connections: the one the engine
    writes on, the one that **holds** the write lock, and — while the
    case runs — the worker that waits for that lock on the engine's own
    connection.  A connection that is inside a call that waits cannot be
    used by anybody else, so the read model that shares it waits too.

    All three are :class:`_OneCallerConnection`: the case needs one
    connection and two threads, and SQLite's rule — one caller at a
    time on a connection — is what makes the answer the same every
    time.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.plans_root = tmp_path / "plans"
        (self.plans_root / "p-read").mkdir(parents=True)
        (self.plans_root / "p-write").mkdir(parents=True)

        # The connection the worker writes on.  Its busy timeout is
        # shortened, so the worker's wait is bounded and the case cannot
        # hang.
        self.write_conn = _open_one_caller(tmp_path / "write.db")
        migrate(self.write_conn)
        self.write_conn.execute(
            "PRAGMA busy_timeout = {0}".format(WRITER_BUSY_TIMEOUT_MS)
        )

        # The connection the read model reads on: the other file.
        self.read_conn = _open_one_caller(tmp_path / "read.db")
        migrate(self.read_conn)

        # The connection that holds the write lock.
        self.blocker = _open_one_caller(tmp_path / "write.db")

        self.store = SqliteSchedulerStore()
        self.facts = PlanFactsReader(self.write_conn, self.plans_root)
        self.ledger = CapacityLedger(
            self.store, self.facts, conn=self.write_conn
        )

        self._queue_plan(self.write_conn, "p-write")
        self._queue_plan(self.read_conn, "p-read")

        self._worker: Optional[threading.Thread] = None
        self._statement_started = threading.Event()
        self._worker_fault: Optional[BaseException] = None
        self._worker_entered_at: Optional[float] = None
        self._worker_left_at: Optional[float] = None

    # -- the setup ---------------------------------------------------------

    def _queue_plan(self, conn: sqlite3.Connection, plan_id: str) -> None:
        """Put a plan in the line: the routing row and the queue row.

        Both rows are needed: the phase lives in ``plan_routing`` and
        the place in the line lives in ``scheduler_queue``, and the read
        model is the two of them together.
        """
        conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            (plan_id, "queued", _utc_iso()),
        )
        SqliteSchedulerStore().enqueue(
            conn, plan_id, priority=0, now=_utc_iso()
        )

    def engine(
        self, read_conn: Optional[sqlite3.Connection]
    ) -> PlanSchedulerEngine:
        """An engine over this world, with or without a read connection."""
        engine = PlanSchedulerEngine(
            config_loader=_ConfigLoader(SchedulerConfig(capacity=4)),
            store=self.store,
            facts=self.facts,
            ledger=self.ledger,
            commands=object(),
            sink=object(),
            conn=self.write_conn,
            read_conn=read_conn,
        )
        engine._config = engine._read_config()
        return engine

    # -- the contention ----------------------------------------------------

    def hold_the_write_lock(self) -> None:
        """Take SQLite's write lock and hold it."""
        self.blocker.execute("BEGIN IMMEDIATE")
        assert self.blocker.in_transaction, (
            "the blocker did not take the write lock"
        )

    def start_worker(self) -> None:
        """Start the thread that waits for the write lock."""
        self.write_conn.set_trace_callback(self._trace)
        self._worker = threading.Thread(
            target=self._wait_for_the_lock,
            name="scheduler-writer",
        )
        self._worker.daemon = True
        self._worker.start()

    def _trace(self, statement: str) -> None:
        """Notice the moment the waiting statement starts."""
        if "BEGIN IMMEDIATE" in statement.upper():
            self._statement_started.set()

    def _wait_for_the_lock(self) -> None:
        """Ask for the write lock, and wait for it.

        The blocker holds that lock, so the call cannot come back before
        the blocker gives it up.  When it does come back it comes back
        with SQLite's own answer — ``database is locked``.

        The worker is alone inside the connection while it waits (see
        :class:`_OneCallerConnection`), so the answer is SQLite's own
        and not the wreckage of two threads inside one connection.  The
        **kind** of the failure is what the case is about; the message
        is SQLite's wording and is not asserted.

        Whatever comes back is recorded, whatever it is: a failure that
        was not recorded would only say "no failure at all", and the
        assertion below is the one that says what the failure has to
        be.
        """
        self._worker_entered_at = time.perf_counter()
        try:
            self.write_conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:  # noqa: BLE001 — recorded, judged below
            self._worker_fault = exc
        finally:
            self._worker_left_at = time.perf_counter()

    def wait_for_the_worker(self, timeout: float = 5.0) -> None:
        """Wait until the worker is inside the call that waits.

        Two facts together are the proof: the trace callback says the
        statement has started, and the thread being alive says it has
        not come back.  Between them the worker is inside SQLite's busy
        wait.
        """
        assert self._statement_started.wait(timeout), (
            "the worker never reached the write lock"
        )
        assert self.worker_alive(), (
            "the worker came back before the lock was contended"
        )

    def worker_alive(self) -> bool:
        """Whether the worker is still waiting."""
        return self._worker is not None and self._worker.is_alive()

    def join_worker(self, timeout: float = 5.0) -> float:
        """Wait for the worker to be over, and say how long it waited.

        The worker's statement is over by the time the read that was
        waiting for it comes back — the read is released by that very
        call — so the worker only has to record that it is over.
        """
        if self._worker is not None:
            self._worker.join(timeout=timeout)
        if self._worker_entered_at is None or self._worker_left_at is None:
            return 0.0
        return self._worker_left_at - self._worker_entered_at

    @property
    def worker_fault(self) -> Optional[BaseException]:
        """The failure the waiting statement came back with."""
        return self._worker_fault

    # -- the teardown ------------------------------------------------------

    def close(self) -> None:
        """Give the lock back, stop the worker, close the connections.

        The order matters: the blocker gives the write lock back first,
        so the waiting thread can come back and be joined, and the trace
        callback is taken off **after** the join — taking it off while
        the thread waits would block on the very mutex the waiting
        thread holds.
        """
        try:
            if self.blocker.in_transaction:
                self.blocker.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - the teardown never raises
            pass
        if self._worker is not None:
            self._worker.join(timeout=5.0)
        try:
            self.write_conn.set_trace_callback(None)
        except sqlite3.Error:  # pragma: no cover - the teardown never raises
            pass
        for conn in (self.write_conn, self.read_conn, self.blocker):
            try:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()
            except sqlite3.Error:  # pragma: no cover - the teardown never raises
                pass


@pytest.fixture()
def read_path_world(tmp_path: Path) -> Iterator[_ReadPathWorld]:
    """Two databases, a worker, and the two lines they hold."""
    the_world = _ReadPathWorld(tmp_path)
    try:
        yield the_world
    finally:
        the_world.close()


def test_nc1_shared_conn_blocks_read(
    read_path_world: _ReadPathWorld,
) -> None:
    """The shared connection makes the panel's read wait for the worker.

    The engine has one connection and the worker writes on it.  A worker
    that sits in ``BEGIN IMMEDIATE`` holds that connection for as long
    as the call waits, and a read model that shares the connection waits
    with it — so the document the panel renders arrives after the
    worker's transaction, not with it.

    The connection serves one caller at a time (see
    :class:`_OneCallerConnection`), so the wait the panel suffers is
    the wait for the worker's call on that connection to be over, and
    the worker's call is alone inside SQLite while it waits: the lock
    failure it comes back with is SQLite's own, every time.

    With the seam restored — the engine has a read connection of its own
    — the document is that connection's document: the plan the read
    connection knows about is in the line, and the plan the write
    connection knows about is not.

    The floor is a floor and not a ceiling: the read cannot come back
    before the worker does, and the worker's wait is bounded by the
    write connection's ``busy_timeout``.
    """
    world = read_path_world

    # The seam restored: the read model has a connection of its own.
    restored = world.engine(read_conn=world.read_conn)
    document = restored.snapshot()

    assert _queue_ids(document) == ["p-read"], (
        "the read model did not read the connection it was given: "
        "{0!r}".format(_queue_ids(document))
    )

    # The seam reverted: the engine has no read connection, so the read
    # model reads on the write connection — the one the worker holds.
    world.hold_the_write_lock()
    world.start_worker()
    world.wait_for_the_worker()

    shared = world.engine(read_conn=None)
    started = time.perf_counter()
    document = shared.snapshot()
    elapsed = time.perf_counter() - started

    waited = world.join_worker()
    fault = world.worker_fault

    assert elapsed >= BLOCKED_READ_FLOOR_SECONDS, (
        "the read on the shared connection came back in {0:.3f}s: it did "
        "not wait for the worker at all".format(elapsed)
    )
    assert waited >= BLOCKED_READ_FLOOR_SECONDS, (
        "the worker was not inside the lock wait for long enough to "
        "prove anything: {0:.3f}s".format(waited)
    )
    assert elapsed >= waited - WORKER_WAIT_TOLERANCE_SECONDS, (
        "the read came back before the worker had finished waiting: the "
        "read took {0:.3f}s and the worker waited {1:.3f}s".format(
            elapsed, waited
        )
    )
    assert isinstance(fault, sqlite3.OperationalError), (
        "the worker did not fail the way SQLite fails when the write "
        "lock cannot be taken: {0!r}".format(fault)
    )
    assert _queue_ids(document) == ["p-write"], (
        "the shared read model did not read the write connection: "
        "{0!r}".format(_queue_ids(document))
    )


# ---------------------------------------------------------------------------
# NC2 — the capacity ledger takes the write lock it does not need
# ---------------------------------------------------------------------------


class _CountingStore(SqliteSchedulerStore):
    """A store that counts the write transactions it opens.

    ``SqliteSchedulerStore.transaction`` opens a ``BEGIN IMMEDIATE`` when
    the connection is not in a transaction yet, and joins the caller's
    transaction when it is.  Only the first kind takes SQLite's write
    lock, so only the first kind is counted here: the number is how many
    times the ledger made the other writers of the database wait.
    """

    def __init__(self) -> None:
        super().__init__()
        #: How many times the ledger took SQLite's write lock.
        self.write_transactions = 0

    @contextlib.contextmanager
    def transaction(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """Count the write transaction, then run the real one."""
        if not conn.in_transaction:
            self.write_transactions += 1
        with super().transaction(conn):
            yield conn

    def reset(self) -> None:
        """Forget the transactions counted so far."""
        self.write_transactions = 0


class _SteadyWorld:
    """A database whose slots already say what the plan facts say.

    The plan is on the verification side — a round that is running — and
    it holds a slot, so the two sides agree.  This is the steady state:
    every tick but the ones where a plan started or finished.
    """

    def __init__(self, conn: sqlite3.Connection, root: Path) -> None:
        self.conn = conn
        self.root = root
        self.clock = _Clock()
        self.store = _CountingStore()
        self.facts = PlanFactsReader(conn, root / "plans", clock=self.clock)
        self.ledger = CapacityLedger(
            self.store, self.facts, self.clock, conn=conn
        )
        self._working("a")
        self.store.upsert_slot(conn, "a", now=_utc_iso())
        self.store.reset()

    def _working(self, plan_id: str) -> None:
        """Make the plan hold compute."""
        self.conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            (plan_id, "verify_first_pass", _utc_iso(self.clock())),
        )
        self.conn.execute(
            "INSERT INTO plan_execution (plan_id, attempt_count, project_dir, "
            "stop_reason, task_progress, next_run_at, card_state, flags, "
            "exec_pid, exec_status, started_at, updated_at) "
            "VALUES (?, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
            (plan_id, _utc_iso(self.clock()), _utc_iso(self.clock())),
        )
        self.conn.execute(
            "INSERT INTO plan_verification (plan_id, verification_status, "
            "round, max_rounds, updated_at) VALUES (?, ?, 0, 3, ?)",
            (plan_id, "running", _utc_iso(self.clock())),
        )


@pytest.fixture()
def steady_world(tmp_path: Path) -> Iterator[_SteadyWorld]:
    """A ledger over an in-memory database in the steady state."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    migrate(conn)
    try:
        yield _SteadyWorld(conn, tmp_path)
    finally:
        conn.close()


def test_nc2_stable_state_opens_txn(
    steady_world: _SteadyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The steady state takes no write transaction — unless it is told to.

    The slots already say what the facts say, so a reconciliation has
    nothing to write, and the ledger must not take SQLite's write lock:
    a tick like this is every tick but the ones where a plan started or
    finished, and a lock taken for nothing is a lock the other writers
    of the database wait for.

    With the seam reverted — the ledger always believes there is
    something to write — the write transaction is opened anyway.  That
    is the old behaviour, and it is countable.
    """
    world = steady_world
    real_writes_needed = world.ledger._writes_needed

    # The seam reverted: there is always something to write.
    monkeypatch.setattr(
        world.ledger, "_writes_needed", lambda active, slots: True
    )
    world.store.reset()

    summary = world.ledger.reconcile()

    assert world.store.write_transactions == 1, (
        "the steady state did not take the write transaction the seam "
        "asked for: {0!r}".format(world.store.write_transactions)
    )
    assert summary["preserved"] == ["a"], (
        "the reconciliation did not preserve the slot it found: "
        "{0!r}".format(summary)
    )

    # The seam restored: the steady state takes no write transaction.
    monkeypatch.setattr(world.ledger, "_writes_needed", real_writes_needed)
    world.store.reset()

    world.ledger.reconcile()

    assert world.store.write_transactions == 0, (
        "the steady state took SQLite's write lock for nothing: "
        "{0!r}".format(world.store.write_transactions)
    )


# ---------------------------------------------------------------------------
# NC3 — a locked queue write
# ---------------------------------------------------------------------------


class _LifecycleResult:
    """What the lifecycle answers with."""

    def __init__(self, ok: bool = True, detail: str = "requeue") -> None:
        self.ok = ok
        self.detail = detail


class _LifecycleThatQueues:
    """The lifecycle, moving the phase to ``queued`` in its own transaction.

    The phase belongs to the state machine, and the lifecycle writes it
    on a connection of its own — so the phase is committed **before** the
    executor writes the queue row.  That is the order the case is about:
    the phase already says ``queued`` when the queue write hits the lock.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.calls: List[str] = []

    def requeue_plan(self, plan_id: str) -> _LifecycleResult:
        """Move the plan to ``queued``, commit it, and answer."""
        self.calls.append(plan_id)
        self._conn.execute(
            "UPDATE plan_routing SET current_phase = ? WHERE plan_id = ?",
            ("queued", plan_id),
        )
        self._conn.commit()
        return _LifecycleResult()


class _CommandWorld:
    """The world a command runs in: a database and an executor."""

    def __init__(self, conn: sqlite3.Connection, root: Path) -> None:
        self.conn = conn
        self.clock = _Clock()
        self.store = SqliteSchedulerStore()
        self.facts = PlanFactsReader(conn, root / "plans", clock=self.clock)
        self.ledger = CapacityLedger(self.store, self.facts, self.clock)
        self.lifecycle = _LifecycleThatQueues(conn)
        self.executor = CommandExecutor(
            self.store,
            self.ledger,
            self.lifecycle,
            facts=self.facts,
            conn=self.conn,
            capacity=1,
            clock=self.clock,
        )

    def plan(self, plan_id: str, phase: str) -> None:
        """Register a plan that is in the phase the case needs."""
        self.conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            (plan_id, phase, _utc_iso(self.clock())),
        )
        self.conn.commit()


@pytest.fixture()
def command_world(tmp_path: Path) -> Iterator[_CommandWorld]:
    """An executor over an in-memory database that carries the real schema."""
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    try:
        yield _CommandWorld(conn, tmp_path)
    finally:
        conn.close()


def _raise_locked(conn: sqlite3.Connection, plan_id: str, now: Any = None) -> Any:
    """Fail the way SQLite fails when the write lock cannot be taken."""
    raise sqlite3.OperationalError("database is locked")


def test_nc3_lock_error_propagates(
    command_world: _CommandWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked queue write is a fault — unless the lock is recognised.

    The queue write lost the race for SQLite's write lock, and the
    write **did not happen**.  The phase was moved in the lifecycle's own
    transaction and committed before the queue write was even attempted,
    so the plan is waiting and only the row is missing — the row the
    reconciler puts right.

    With the seam reverted — the executor does not recognise the lock —
    the fault stays a fault and comes out as an exception.  With the
    seam restored, the answer is ``applied`` and it names the row that
    is still missing.
    """
    world = command_world
    world.plan("20261123-nc3", "stopped")
    monkeypatch.setattr(world.store, "promote", _raise_locked)
    real_is_locked_error = commands_module._is_locked_error

    # The seam reverted: the lock is not recognised, so it is not a lock.
    monkeypatch.setattr(
        commands_module, "_is_locked_error", lambda exc: False
    )

    with pytest.raises(sqlite3.OperationalError) as caught:
        world.executor.resume("20261123-nc3")

    assert "locked" in str(caught.value), (
        "the fault that was raised is not the one that happened: "
        "{0!r}".format(caught.value)
    )

    # The seam restored: the locked write degrades, and the answer says
    # what is still missing.
    monkeypatch.setattr(
        commands_module, "_is_locked_error", real_is_locked_error
    )

    result = world.executor.resume("20261123-nc3")

    assert result.outcome == APPLIED, (
        "a resume that hit the write lock was not applied: {0!r}".format(
            result
        )
    )
    assert result.detail == PENDING_RECONCILE, (
        "the degraded answer does not name what is still missing: "
        "{0!r}".format(result)
    )


# ---------------------------------------------------------------------------
# NC4 — a plan that is waiting without a queue row
# ---------------------------------------------------------------------------


class _QueueWorld:
    """A plan that is waiting without a queue row, and the engine over it."""

    def __init__(self, tmp_path: Path) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        migrate(self.conn)
        self.plans_root = tmp_path / "plans"
        (self.plans_root / "p1").mkdir(parents=True)
        self.conn.execute(
            "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            ("p1", "queued", _utc_iso()),
        )
        self.store = SqliteSchedulerStore()
        self.facts = PlanFactsReader(self.conn, self.plans_root)
        self.ledger = CapacityLedger(
            self.store, self.facts, conn=self.conn
        )
        self.engine = PlanSchedulerEngine(
            config_loader=_ConfigLoader(SchedulerConfig(capacity=4)),
            store=self.store,
            facts=self.facts,
            ledger=self.ledger,
            commands=object(),
            sink=object(),
            conn=self.conn,
            plans_dir=self.plans_root,
        )

    def queue_ids(self) -> List[str]:
        """The plans that have a queue row, in the production order."""
        return [
            str(row.get("plan_id") or "")
            for row in self.store.list_queue(self.conn)
        ]

    def close(self) -> None:
        """Give back the case's connection."""
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover - the teardown never raises
            pass


@pytest.fixture()
def queue_world(tmp_path: Path) -> Iterator[_QueueWorld]:
    """One plan that is waiting and has no queue row."""
    the_world = _QueueWorld(tmp_path)
    try:
        yield the_world
    finally:
        the_world.close()


def test_nc4_without_synthesis_plan_invisible(
    queue_world: _QueueWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiting plan without a row is in the line — because it is synthesized.

    The facts say the plan is waiting; the queue table has never heard
    of it.  The read model gives it a row of its own — a **synthesized**
    one, which lives in the read model and nowhere else — so the line
    the operator sees is the line the scheduler is about to run.

    With the seam reverted — the read model synthesizes nothing — the
    plan is invisible: the facts say it is waiting and the line does not
    show it, which is the defect this file holds down.
    """
    world = queue_world
    real_synthesis = PlanSchedulerEngine._synthesized_queued_rows

    # The seam reverted: nothing is synthesized, so the waiting plan
    # without a row is nowhere.
    monkeypatch.setattr(
        PlanSchedulerEngine,
        "_synthesized_queued_rows",
        lambda *args, **kwargs: [],
    )

    document = world.engine.snapshot()

    assert "p1" not in _queue_ids(document), (
        "the plan that is waiting without a row is in the line even "
        "though nothing was synthesized: {0!r}".format(_queue_ids(document))
    )
    assert world.queue_ids() == [], (
        "the queue table grew a row of its own: {0!r}".format(
            world.queue_ids()
        )
    )

    # The seam restored: the plan joins the tail of the line.
    monkeypatch.setattr(
        PlanSchedulerEngine, "_synthesized_queued_rows", real_synthesis
    )

    document = world.engine.snapshot()

    assert _queue_ids(document) == ["p1"], (
        "the plan that is waiting without a row is not in the line: "
        "{0!r}".format(_queue_ids(document))
    )
    assert world.queue_ids() == [], (
        "the synthesized row was written to the table: {0!r}".format(
            world.queue_ids()
        )
    )

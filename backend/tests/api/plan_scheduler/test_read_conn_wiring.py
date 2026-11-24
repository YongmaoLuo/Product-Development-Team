"""The production scheduler opens a **second** connection for the read model.

Background
----------
The scheduler's read model — the document the operator's panel reads — used
to read on the **same** connection the worker writes on.  A read on the
writer's own connection waits for the writer's transaction, so the panel
showed nothing while the worker was busy: the very moment the operator
most wants to see the queue is the moment the queue is least readable.

The engine therefore knows about a **read connection**
(:class:`plan_scheduler.engine.EngineParts`): a second connection to the
same database, which the read model reads through.  What this file pins
is the **production wiring** — the engine may carry a read connection, but
the server's own binder is what decides whether the running server has
one.  An engine that supports the split and a server that does not wire
it is the failure this file is about.

What these cases pin
--------------------
1. **The production binder opens a second connection.**  The parts the
   binder hands back carry a ``read_conn``, and it is a *different*
   object from the write connection: a read model that reads on the
   write connection is the split that was not made.
2. **The read connection reaches the same database.**  A row written
   through the write connection is seen through the read connection —
   the second connection is a second *handle* on the same facts, not a
   second database with its own facts.
3. **A read connection that cannot be opened leaves nothing behind.**
   The connection that was opened before it is closed, and the failure
   reaches the caller as it came — a teardown that swallows the
   caller's error, or that leaks the write connection on the way out, is
   a failure of its own.
4. **The schema is migrated once.**  The write connection migrates it;
   the read connection is a reader of a schema that is already there, and
   a second migrator is a second writer.

The world the cases drive
-------------------------
The **production** binder — the one :func:`server._build_scheduler_engine`
builds and hands to the engine — and not a hand-built one.  A binder of
the case's own would answer the question about the case's own wiring.

The engine itself is a **stub**: it records the binder the production
builder hands it and starts **no thread**, so nothing ticks over the very
rows these cases are about.  The binder is called by hand, which is what
the worker's first tick does.  The cases close both connections
themselves — the stub owns neither of them.

Why this directory has no ``__init__.py``
-----------------------------------------
The production package is called ``plan_scheduler``, and so is this
directory.  A package marker here would make pytest import this
directory as the **top-level** package ``plan_scheduler``, and the name
would then mean two things at once.  This directory deliberately has
none — the sibling files say the whole of it.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import plan_scheduler.engine  # noqa: E402
import server  # noqa: E402


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


class _StubEngine:
    """The engine, replaced by a stub that starts no thread.

    The stub records the binder the production builder hands it, so a
    case can call the binder by hand — which is what the worker's first
    tick does, and what the builder itself does not do.

    It holds no connection of its own: the two connections the binder
    opens belong to the cases, and the cases close them.
    """

    def __init__(
        self,
        config_loader: Any,
        *,
        binder: Any = None,
        plans_dir: Any = None,
        **kwargs: Any,
    ) -> None:
        self.config_loader = config_loader
        self.binder = binder
        self.plans_dir = plans_dir
        self.started = False
        self.stopped = False

    def start(self) -> "_StubEngine":
        """Pretend to start the worker.  No thread is started."""
        self.started = True
        return self

    def stop(self, timeout: float = 30.0) -> None:
        """Pretend to stop the worker.  There is nothing to join."""
        self.stopped = True


def _install_stub_engine(monkeypatch: Any) -> Dict[str, Any]:
    """Replace the engine with the stub, and return what it recorded."""
    captured: Dict[str, Any] = {}

    def _engine(
        config_loader: Any,
        *,
        binder: Any = None,
        plans_dir: Any = None,
        **kwargs: Any,
    ) -> _StubEngine:
        captured["binder"] = binder
        stub = _StubEngine(
            config_loader, binder=binder, plans_dir=plans_dir, **kwargs
        )
        captured["engine"] = stub
        return stub

    monkeypatch.setattr(
        plan_scheduler.engine, "PlanSchedulerEngine", _engine
    )
    return captured


def _point_at_throwaway_files(
    monkeypatch: Any, tmp_path: Path
) -> "tuple[Path, Path]":
    """Point the production builder at this case's own database and tree.

    The builder resolves the database through ``server._state_db_path``
    and the plans tree through ``server.PLANS_DIR``; both are module
    globals, so both are pointed at the case's own files here.
    """
    db_path = tmp_path / "state.db"
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    monkeypatch.setattr(
        server, "_state_db_path", lambda *args, **kwargs: db_path
    )
    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    return db_path, plans_dir


def _production_binder(monkeypatch: Any, tmp_path: Path) -> Any:
    """The production binder, with the engine stubbed out.

    The binder under test is the one :func:`server._build_scheduler_engine`
    builds: the database it opens, the facts reader it wires and the
    parts it returns are the production ones.  A binder of the case's own
    would answer the question about the case's own wiring.

    The engine is a stub (see :class:`_StubEngine`), so the worker never
    starts and the binder is called by hand — which is what the worker's
    first tick does.
    """
    _point_at_throwaway_files(monkeypatch, tmp_path)
    captured = _install_stub_engine(monkeypatch)

    runtime = server._build_scheduler_engine()
    assert runtime.engine is not None, (
        "the production wiring did not build the scheduler engine"
    )
    binder = captured.get("binder")
    assert binder is not None, (
        "the production builder did not hand the engine a binder"
    )
    return binder


@pytest.fixture
def production_parts(monkeypatch, tmp_path) -> Iterator[Any]:
    """The parts the **production** binder hands back.

    Both connections the binder opened are closed when the case is over —
    they are the case's, not the stub's.
    """
    binder = _production_binder(monkeypatch, tmp_path)
    parts = binder()
    try:
        yield parts
    finally:
        for conn in (getattr(parts, "read_conn", None), parts.conn):
            if conn is not None:
                conn.close()


def _queue_row(conn: sqlite3.Connection, plan_id: str) -> None:
    """Put one plan in the queue, the way the scheduler's writer does."""
    from plan_scheduler.store import SqliteSchedulerStore

    SqliteSchedulerStore().enqueue(conn, plan_id)


# ---------------------------------------------------------------------------
# 1 — the production binder opens a second connection
# ---------------------------------------------------------------------------


def test_engine_parts_carry_separate_read_conn(production_parts: Any) -> None:
    """The production parts carry a read connection of their own.

    The engine can read on a connection that is not the write
    connection — but only if somebody hands it one.  The production
    binder is that somebody: it opens the write connection, and it
    opens a **second** one for the read model.

    A ``read_conn`` that is the write connection would leave the panel
    waiting for the worker's transaction, which is the failure the
    whole split exists to remove.
    """
    parts = production_parts

    assert parts.read_conn is not None, (
        "the production binder opened no read connection — the read "
        "model reads on the write connection and waits for the worker"
    )
    assert parts.read_conn is not parts.conn, (
        "the read connection is the write connection — the panel's "
        "document waits for the worker's transaction again"
    )


# ---------------------------------------------------------------------------
# 2 — the second connection reaches the same database
# ---------------------------------------------------------------------------


def test_read_conn_reaches_the_same_database(production_parts: Any) -> None:
    """The read connection reads the **same** facts the writer writes.

    Two connections to one database are two handles on one truth.  A
    second connection to a second database would answer the panel's
    question about a world nobody else can see, and the panel would
    show an empty queue next to a working scheduler.

    So: a row written through the write connection is seen through the
    read connection.
    """
    parts = production_parts

    read_conn: sqlite3.Connection = parts.read_conn
    assert read_conn is not None, "the production binder opened no read connection"

    _queue_row(parts.conn, "p-wired")

    count = read_conn.execute(
        "SELECT count(*) FROM scheduler_queue"
    ).fetchone()[0]
    assert count > 0, (
        "the read connection sees an empty queue while the write "
        "connection just wrote a row into it — the two connections are "
        "not on the same database"
    )


# ---------------------------------------------------------------------------
# 3 — a read connection that cannot be opened leaves nothing behind
# ---------------------------------------------------------------------------


def test_a_read_connection_that_cannot_be_opened_leaves_nothing_behind(
    monkeypatch, tmp_path
) -> None:
    """A failed read connection closes the write one and re-raises.

    Two rules, and both of them are about the failure path:

    * what was opened before the failure is given back — a descriptor
      that is never returned is a descriptor that is never given back;
    * the caller's own error is the one that reaches the caller — a
      teardown that replaces it with one of its own hides the reason
      the scheduler could not start.
    """
    import state_machine.db.connection as connection_module

    opened: List[sqlite3.Connection] = []
    real_open = connection_module.open
    boom = RuntimeError("the read connection cannot be opened")

    def _open(path: Any) -> sqlite3.Connection:
        """Open the first connection; refuse the second one."""
        if len(opened) == 1:
            raise boom
        conn = real_open(path)
        opened.append(conn)
        return conn

    # The builder takes its factory **by name** when it is called, so
    # the spy has to be in place before the builder runs.
    monkeypatch.setattr(connection_module, "open", _open)

    binder = _production_binder(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError) as excinfo:
        binder()

    assert excinfo.value is boom, (
        "the binder replaced the failure with one of its own — the "
        "caller has to see the reason the read connection could not be "
        "opened"
    )
    assert len(opened) == 1, "the write connection was never opened"
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


# ---------------------------------------------------------------------------
# 4 — the schema is migrated once
# ---------------------------------------------------------------------------


def test_the_read_connection_does_not_migrate_the_schema(
    monkeypatch, tmp_path
) -> None:
    """The write connection migrates; the read connection only reads.

    The schema is a fact about the **database**, not about one handle on
    it.  A second migrator would be a second writer, and the read
    connection is a reader — its whole purpose is to read while the
    worker writes, so it must not take the write lock to do it.
    """
    import state_machine.db.schema as schema_module

    migrated: List[sqlite3.Connection] = []
    real_migrate = schema_module.migrate

    def _migrate(conn: sqlite3.Connection) -> Any:
        migrated.append(conn)
        return real_migrate(conn)

    # The builder takes the migrator **by name** when it is called, so
    # the spy has to be in place before the builder runs.
    monkeypatch.setattr(schema_module, "migrate", _migrate)

    binder = _production_binder(monkeypatch, tmp_path)

    parts = binder()
    try:
        assert len(migrated) == 1, (
            f"the schema was migrated {len(migrated)} times — the read "
            "connection migrates it a second time"
        )
        assert migrated[0] is parts.conn, (
            "the schema was migrated on a connection other than the "
            "write one"
        )
    finally:
        for conn in (getattr(parts, "read_conn", None), parts.conn):
            if conn is not None:
                conn.close()

"""The scheduler's integration fixtures — one case, one world.

Background
----------
The scheduler's integration cases drive a **worker**: a thread that
ticks, reads the queue and writes to the state database.  A worker that
outlives its case does not fail its own case — it fails a *later* one,
because the paths it needs (the database, the plans tree) are resolved
**on every call**.  A worker still running when the next case starts
resolves them against the next case's world and writes there.

That is the 2026-09-14 ``database disk image is malformed`` failure, and
it is why this directory carries its own conftest.  The four fixtures
below are the four things a scheduler case needs, and every one of them
is bound to *this* case's ``tmp_path`` — never to a shared directory,
never to the operator's tree.

The four fixtures
-----------------
``plan_factory``
    A minimal, legal plan directory: ``prd.json``, ``tasks.json`` and
    the project directory the plan works in.
``workspace_factory``
    A workspace reachable under three names — the directory itself, a
    symlink to it, and a symlink to that symlink.  All three resolve to
    one real path, which is what the scheduler's workspace bookkeeping
    has to survive.
``db_factory``
    A **real** SQLite file (never ``:memory:``) carrying the production
    schema, with ``PDT_STATE_DB_PATH`` pointing at it.
``engine_factory``
    The engine a case drives: a worker thread that ticks the store.

The cleanup contract
--------------------
Every case ends the same way, in this order:

1. **stop the engine** — the worker is told to stop before anything is
   torn down, so it cannot write into a world that is being removed;
2. **join the threads** — stopping is a request, joining is the
   guarantee: the thread has *finished* before the case is over;
3. **kill the subprocess tree** — a child that outlives its parent keeps
   the port, the lock and the file handle;
4. **remove the scratch directories** — last, because a worker that was
   still running would recreate what was just removed.

Then the case asserts three things: no engine thread is alive, no child
process is alive, and no socket the case listened on is still open.  A
leak is not a warning here — it is the failure this conftest exists to
prevent, so it fails the case that caused it.

The order is not decoration.  Reversing (1) and (2) leaves a thread
that is *asked* to stop but not yet finished, writing into a database
whose file has just been deleted; reversing (3) and (4) leaves a child
holding a lock on a file that no longer exists.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest


# ---------------------------------------------------------------------------
# Binding the fixtures to every node pytest builds for this directory
# ---------------------------------------------------------------------------

#: This directory, resolved.
_THIS_DIRECTORY = Path(__file__).resolve().parent


def _same_directory(left: Path, right: Path) -> bool:
    """Whether two paths name the same directory.

    ``collector.path`` is built from the rootdir and is not resolved, while
    ``Path(__file__).resolve()`` is — so on a machine where the checkout sits
    behind a symlink the two spell the same directory differently.  The
    comparison is therefore made on the resolved paths.
    """
    try:
        return left.resolve() == right.resolve()
    except OSError:  # pragma: no cover - an unreadable path
        return left == right


def _bind_fixtures_to(collector: Any) -> None:
    """Bind this conftest's fixtures to ``collector``.

    pytest 9 binds a conftest's fixtures to the **node object** of the
    directory the conftest lives in, and it parses the conftest exactly
    once — the first time that directory is collected.  A directory, though,
    can be collected **twice**: a file that is named on the command line
    directly inside a package makes pytest re-collect that package
    (``Session._collect_one_node`` with ``handle_dupes=False``), and the
    re-collect rebuilds the whole subtree below it with *new* node objects.

    The second tree then holds a conftest whose fixtures are bound to a node
    that is not one of its ancestors, so pytest does not find them
    (``FixtureManager._matchfactories``) and the case fails with
    ``fixture 'db_factory' not found``.  The autouse containment fixture is
    hit the same way and fails *silently*: it stops being applied, so a case
    would no longer hand back the engine thread, the child process and the
    socket it started.

    Re-binding the fixtures to every node pytest builds for this directory
    restores both halves of the contract — the same fixtures, the same
    scopes, the same autouse behaviour — on the node that is actually in
    the tree being collected.  Nothing about the contract itself changes:
    the fixtures are still the ones below, still bound to this case's
    ``tmp_path``, and still one case one world.
    """
    fixturemanager = collector.session._fixturemanager

    # Take the pending registration, so pytest does not parse this conftest
    # a second time for the very same node.
    pending = getattr(fixturemanager, "_pending_conftests", None)
    if pending is not None:
        pending.pop(collector.path, None)

    # Already bound to this very node?  Then there is nothing left to do.
    for fixturedefs in fixturemanager._arg2fixturedefs.values():
        for fixturedef in fixturedefs:
            if getattr(fixturedef, "node", None) is collector:
                return

    fixturemanager.parsefactories(holder=sys.modules[__name__], node=collector)


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    """Bind this conftest's fixtures to every node built for this directory.

    The hook is a wrapper, so it runs around the report pytest builds for a
    collector.  pytest parses a conftest in the very same hook, and it does
    so *after* the collector's own children have been built — which is early
    enough, because pytest collects depth first: the children of this
    directory are collected only after this directory's report is made.
    Binding here therefore reaches the cases in the tree pytest is about to
    collect, whether this is the first node it builds for the directory or
    the second.
    """
    result = yield
    path = getattr(collector, "path", None)
    if path is not None and _same_directory(Path(path), _THIS_DIRECTORY):
        _bind_fixtures_to(collector)
    return result


# ---------------------------------------------------------------------------
# The paths
# ---------------------------------------------------------------------------


def db_path_for(root: Path, name: str = "state.db") -> Path:
    """The state database of a case rooted at ``root``.

    A pure function of the root, and that is the whole point: the
    database a case writes to is decided by *where the case runs*, not
    by anything the case says.  Two cases have two roots, so two cases
    have two databases — a case can never read another case's queue.

    Args:
        root: the case's root directory (pytest's ``tmp_path``).
        name: the file name inside ``root``.

    Returns:
        The path of the state database inside ``root``.
    """
    return Path(root) / name


# ---------------------------------------------------------------------------
# The values the fixtures hand out
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanFixture:
    """A minimal plan, and where each of its parts lives.

    Attributes:
        plan_id: the plan's id — the name of its directory under the
            plans root, and the key it is queued under.
        plan_dir: the plan's directory: ``prd.json``, ``tasks.json``.
        project_dir: the directory the plan works in.  It lives inside
            the plan directory unless the case names another location.
        priority: the priority the plan is queued with.
    """

    plan_id: str
    plan_dir: Path
    project_dir: Path
    priority: int


@dataclass(frozen=True)
class Workspace:
    """A workspace reachable under three different names.

    Attributes:
        root: the workspace root — the directory that holds the three
            names.
        project: the workspace itself, the real directory.
        link: a symlink to ``project``: the second name for the same
            directory.
        alias: a symlink to ``link``: the third name, reached through a
            chain of two links.

    The three names are three *strings* for one directory.  A scheduler
    that resolves a workspace by string comparison sees three different
    plans; one that resolves it by real path sees one.  The fixture
    exists so a case can pin which of the two the code does.
    """

    root: Path
    project: Path
    link: Path
    alias: Path

    @property
    def realpath(self) -> Path:
        """The one real path all three names resolve to."""
        return self.project.resolve()


@dataclass(frozen=True)
class SchedulerDb:
    """This case's state database.

    Attributes:
        path: the database file — a real file on disk, never
            ``:memory:``.  Two connections cannot race over an
            in-memory database, and the scheduler's own concurrency
            contract is exactly that race.
    """

    path: Path

    def connect(self) -> sqlite3.Connection:
        """Open a connection with the production PRAGMA contract.

        The production contract is the point: WAL, ``busy_timeout``,
        autocommit.  A connection opened any other way would not be the
        one the scheduler opens in production.
        """
        from state_machine.db.connection import open as open_db

        return open_db(self.path)

    def table_names(self) -> set:
        """The user table names in the database."""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        finally:
            conn.close()
        return {row[0] for row in rows}


@dataclass
class CaseRegistry:
    """What this case started, and what the teardown has to give back.

    The registry is what makes the cleanup contract *enforceable*: a
    case that starts an engine, a child process or a listening socket
    records it here, and the teardown reclaims it and then says so.

    Attributes:
        engines: the engines this case started.
        children: the child processes this case started.
        scratch: the directories this case created and has to remove.
        sockets: the sockets this case listened on.
        order: the cleanup steps that were run, in the order they ran.
    """

    engines: List["SchedulerEngine"] = field(default_factory=list)
    children: List[Any] = field(default_factory=list)
    scratch: List[Path] = field(default_factory=list)
    sockets: List[Any] = field(default_factory=list)
    order: List[str] = field(default_factory=list)

    def reclaim(self) -> List[str]:
        """Give back everything this case started — in the contract's order.

        The order is the contract, and it is not interchangeable:

        1. **stop the engine** — a worker that is still told to stop
           cannot write into a world that is being taken apart;
        2. **join the threads** — stopping is a request, joining is the
           guarantee that the thread has finished;
        3. **kill the subprocess trees** — a child keeps the port, the
           lock and the file handle long after its parent is gone;
        4. **remove the scratch directories** — last, because a worker
           that was still running would recreate what was just removed.

        Calling this twice is harmless: stopping an engine that has
        stopped, joining a thread that has finished, killing a child
        that is gone and removing a directory that is not there are all
        no-ops.  That is what lets a case reclaim *during* the case and
        still let the teardown reclaim again.

        Returns:
            The steps that ran, in the order they ran.
        """
        order: List[str] = []

        for engine in self.engines:
            engine.stop()
        order.append("stop_engine")

        for engine in self.engines:
            engine.join(timeout=5.0)
        order.append("join_threads")

        for process in self.children:
            _kill_process_tree(process)
        order.append("kill_subprocesses")

        for path in self.scratch:
            shutil.rmtree(path, ignore_errors=True)
        order.append("remove_tmp")

        self.order = order
        return order

    def spawn(self, argv: List[str], cwd: Optional[Path] = None) -> Any:
        """Start a child process and hand it to the teardown.

        The child is started in its **own** session, so the teardown can
        kill the whole tree it grew without touching the test runner's
        own process group.

        Args:
            argv: the command line.
            cwd: the working directory; the case's own by default.

        Returns:
            The started :class:`subprocess.Popen`.
        """
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            start_new_session=True,
        )
        self.children.append(process)
        return process


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class SchedulerEngine:
    """The engine a scheduler case drives.

    A worker thread that ticks: it opens a connection to *this case's*
    database, reads the queue and the slots through
    :class:`~plan_scheduler.store.SqliteSchedulerStore`, and asks the
    policy why the head of the queue may — or may not — start.

    The pieces around the loop are the production ones (the store, the
    schema, the policy); the loop itself is the test's, because what a
    case needs is a worker it can *stop* and *join*, and a worker that
    can be stopped and joined is the whole cleanup contract.

    Attributes:
        db_path: the database the engine reads and writes.
        plans_dir: the plans tree the engine looks at.
        store: the scheduler store the engine writes through.
        capacity: how many plans may hold a slot at once.
        tick_seconds: how long the worker waits between two ticks.
        ticks: how many ticks the engine has run.
        last_verdict: the admission verdict of the most recent tick.
        errors: the exceptions a tick raised, if any.
    """

    #: The prefix every engine thread's name carries.
    #:
    #: The teardown has to tell "our" thread from a library's — the
    #: suite runs with threads from ``anyio``, ``httpx`` and pytest
    #: plugins that live for the whole session, and reclaiming those
    #: would be a bug, not a cleanup.
    THREAD_PREFIX = "scheduler-engine"

    def __init__(
        self,
        db_path: Path,
        plans_dir: Path,
        *,
        store: Optional[Any] = None,
        capacity: int = 1,
        tick_seconds: float = 0.01,
        thread_name: Optional[str] = None,
    ) -> None:
        from plan_scheduler.store import SqliteSchedulerStore

        self.db_path = Path(db_path)
        self.plans_dir = Path(plans_dir)
        self.store = store if store is not None else SqliteSchedulerStore()
        self.capacity = capacity
        self.tick_seconds = tick_seconds
        self.ticks = 0
        self.last_verdict: Optional[str] = None
        self.errors: List[BaseException] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=thread_name or f"{self.THREAD_PREFIX}-{id(self):x}",
            daemon=True,
        )

    # -- the worker --------------------------------------------------------

    @property
    def thread(self) -> threading.Thread:
        """The worker thread."""
        return self._thread

    def start(self) -> "SchedulerEngine":
        """Start the worker."""
        self._thread.start()
        return self

    def _run(self) -> None:
        """Tick until the engine is stopped."""
        while not self._stop.wait(self.tick_seconds):
            try:
                self.tick()
            except BaseException as exc:  # pragma: no cover - defensive
                # A tick that raises must not kill the worker silently:
                # the error is kept, and the case reads it.
                self.errors.append(exc)
                return

    def tick(self) -> str:
        """Run one tick and return the admission verdict it reached.

        The tick reads the queue and the slots through the store and
        hands the policy a snapshot — the same plain data the live
        engine hands it.
        """
        from plan_scheduler.policy import admission_verdict

        conn = self.connect()
        try:
            queue = self.store.list_queue(conn)
            slots = self.store.list_slots(conn)
        finally:
            conn.close()

        snapshot: Dict[str, Any] = {
            "queued": [
                {
                    "plan_id": row["plan_id"],
                    "due": True,
                    "workspace_busy": False,
                    "paused": False,
                    "hitl_pending": False,
                }
                for row in queue
            ],
            "used": len(slots),
            "capacity": self.capacity,
            "running": [
                {
                    "plan_id": row["plan_id"],
                    "elapsed": 0.0,
                    "acquired_at": 0.0,
                }
                for row in slots
            ],
            "allow_evict": False,
        }
        verdict = admission_verdict(snapshot)
        self.last_verdict = verdict
        self.ticks += 1
        return verdict

    def connect(self) -> sqlite3.Connection:
        """Open a connection to the engine's database."""
        from state_machine.db.connection import open as open_db

        return open_db(self.db_path)

    # -- stopping ----------------------------------------------------------

    def stop(self) -> None:
        """Ask the worker to stop.

        Stopping is a *request*: the thread may be in the middle of a
        tick.  :meth:`join` is what turns the request into a guarantee.
        """
        self._stop.set()

    def join(self, timeout: float = 5.0) -> None:
        """Wait for the worker to finish.

        A thread that was never started is not joined — joining it would
        raise, and there is nothing to wait for.
        """
        if self._thread.is_alive():
            self._thread.join(timeout)

    def close(self) -> None:
        """Stop the worker and wait for it to finish."""
        self.stop()
        self.join()


# ---------------------------------------------------------------------------
# Reclaiming
# ---------------------------------------------------------------------------


def _kill_process_tree(process: Any) -> None:
    """Stop a child process and everything it started.

    The process group is killed only when the child has one of its own.
    A child that shares the test runner's group would take the runner
    with it, and a test suite that kills itself is worse than one that
    leaks a process.

    Args:
        process: the child to stop.
    """
    pid = getattr(process, "pid", None)
    if pid is None:
        return

    own_group = False
    try:
        own_group = os.getpgid(pid) != os.getpgid(0)
    except (OSError, ProcessLookupError):
        own_group = False

    try:
        if own_group:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):  # pragma: no cover - already gone
        pass

    try:
        process.wait(timeout=5)
        return
    except Exception:  # noqa: BLE001 - the child refused to go
        pass

    try:
        if own_group:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):  # pragma: no cover - already gone
        pass
    try:
        process.wait(timeout=5)
    except Exception:  # noqa: BLE001 - nothing more can be done
        pass


def _engine_threads() -> List[threading.Thread]:
    """The engine threads that are still alive."""
    return [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(SchedulerEngine.THREAD_PREFIX) and thread.is_alive()
    ]


def _running_children(processes: List[Any]) -> List[Any]:
    """The children that are still running."""
    running = []
    for process in processes:
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is None:
            running.append(process)
    return running


def _open_sockets(sockets: List[Any]) -> List[Any]:
    """The sockets that are still open.

    A closed socket reports ``-1`` for its file descriptor; a socket
    that is still listening reports a real one.
    """
    open_sockets = []
    for sock in sockets:
        try:
            fileno = sock.fileno()
        except OSError:  # pragma: no cover - already closed under us
            continue
        if fileno != -1:
            open_sockets.append(sock)
    return open_sockets


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def case_registry() -> CaseRegistry:
    """What this case started — the list the teardown reclaims."""
    return CaseRegistry()


@pytest.fixture(autouse=True)
def scheduler_case_containment(case_registry, monkeypatch):
    """Give back everything this case started, in the contract's order.

    Autouse, because the contract is not a rule the cases opt into: a
    case that forgets to stop its engine is exactly the case that
    breaks the next one.

    The listening sockets are recorded by wrapping ``socket.listen`` —
    the moment a socket starts listening is the moment it becomes a port
    somebody else could bind, and it is the only moment at which the
    teardown can still name it.
    """
    original_listen = socket.socket.listen
    listening: List[Any] = []

    def _listen(self, *args, **kwargs):
        listening.append(self)
        return original_listen(self, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "listen", _listen)
    case_registry.sockets = listening

    yield

    # The four steps, in the order the contract states — see
    # :meth:`CaseRegistry.reclaim` for why the order is not
    # interchangeable.
    order = case_registry.reclaim()
    assert order == [
        "stop_engine",
        "join_threads",
        "kill_subprocesses",
        "remove_tmp",
    ], f"the cleanup ran out of order: {order!r}"

    # --- the verdicts ----------------------------------------------------
    leaked_threads = _engine_threads()
    assert not leaked_threads, (
        f"{len(leaked_threads)} engine thread(s) outlived their case: "
        f"{[t.name for t in leaked_threads]!r}. A worker that outlives its "
        f"case resolves the database and the plans tree against the *next* "
        f"case and writes there — every case must hand back what it starts."
    )

    alive_children = _running_children(case_registry.children)
    assert not alive_children, (
        f"{len(alive_children)} child process(es) outlived their case: "
        f"{[getattr(p, 'pid', None) for p in alive_children]!r}."
    )

    leftover_sockets = _open_sockets(case_registry.sockets)
    assert not leftover_sockets, (
        f"{len(leftover_sockets)} socket(s) the case listened on are still "
        f"open: {leftover_sockets!r}. A port left listening is a port the "
        f"next case cannot bind."
    )


@pytest.fixture
def db_factory(tmp_path, monkeypatch) -> Any:
    """A factory for this case's state database.

    The database is a **real file** with the production schema, and
    ``PDT_STATE_DB_PATH`` points at it — every component that resolves
    the database resolves it *on every call*, so the environment
    variable is the only thing that decides which database a case
    writes to.  A case that created its own database and left the
    variable alone would have two databases: the one it writes and the
    one the code under test reads.

    The default name is ``state.db``, the same file the suite-wide
    ``isolated_plans_dir`` fixture points at, so the fixture and the
    suite agree on one database instead of quietly splitting the case's
    state in two.

    Returns:
        A factory: ``db_factory(name="state.db", root=None,
        set_env=True)``.  ``root`` is the case root the database lives
        in; ``set_env`` says whether ``PDT_STATE_DB_PATH`` follows the
        database that was just created.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    def _make(
        name: str = "state.db",
        root: Optional[Path] = None,
        set_env: bool = True,
    ) -> SchedulerDb:
        base = Path(root) if root is not None else tmp_path
        path = db_path_for(base, name)
        path.parent.mkdir(parents=True, exist_ok=True)

        conn = open_db(path)
        try:
            migrate(conn)
        finally:
            conn.close()

        if set_env:
            monkeypatch.setenv("PDT_STATE_DB_PATH", str(path))

        return SchedulerDb(path=path)

    return _make


@pytest.fixture
def plan_factory(tmp_path, case_registry) -> Any:
    """A factory for a minimal, legal plan directory.

    Three things make a plan directory legal for the scheduler: the
    PRD it was built from, the task list it has to run, and the project
    directory it runs in.  The fixture writes the first two and creates
    the third, so a case never has to hand-write a plan to get one
    queued.

    The plan directory lives under the case's own ``plans`` root — the
    same one ``PDT_PLANS_DIR`` points at — and the project directory
    inside it unless the case names another location.

    Returns:
        A factory: ``plan_factory(plan_id="p1", priority=0,
        project_dir=None, plans_dir=None)``.
    """
    plans_root = tmp_path / "plans"
    # The plans tree this fixture writes into is scratch: the teardown
    # removes it, because a tree that outlives its case is a tree the
    # next case can read.
    case_registry.scratch.append(plans_root)

    def _make(
        plan_id: str = "p1",
        priority: int = 0,
        project_dir: Optional[Path] = None,
        plans_dir: Optional[Path] = None,
    ) -> PlanFixture:
        root = Path(plans_dir) if plans_dir is not None else plans_root
        plan_dir = root / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)

        project = Path(project_dir) if project_dir is not None else plan_dir / "project"
        project.mkdir(parents=True, exist_ok=True)

        (plan_dir / "prd.json").write_text(
            json.dumps(
                {
                    "title": f"Plan {plan_id}",
                    "overview": "A minimal plan written by the scheduler fixtures.",
                    "constraints": "",
                    "acceptance": "",
                    "decision_points": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        (plan_dir / "tasks.json").write_text(
            json.dumps(
                {
                    "requirement": "A minimal plan written by the scheduler fixtures.",
                    "tasks": [
                        {
                            "id": f"{plan_id}-1",
                            "title": "The one task this minimal plan has",
                            "description": "The fixture writes a plan that is legal, not one that is busy.",
                            "test_command": "true",
                            "project_dir": str(project),
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        return PlanFixture(
            plan_id=plan_id,
            plan_dir=plan_dir,
            project_dir=project,
            priority=priority,
        )

    return _make


@pytest.fixture
def workspace_factory(tmp_path, case_registry) -> Any:
    """A factory for a workspace reachable under three names.

    The workspace is a real directory, and the fixture adds two links to
    it: one pointing at the directory, one pointing at that link.  All
    three names resolve to one real path, and all three are different
    strings — which is the whole point.  A case that pins the realpath
    pins the behaviour the scheduler has to have: two plans may not
    work in "the same" workspace just because they spelled it
    differently.

    Returns:
        A factory: ``workspace_factory(name="ws")``.
    """
    def _make(name: str = "ws") -> Workspace:
        root = tmp_path / name
        project = root / "project"
        project.mkdir(parents=True, exist_ok=True)

        link = root / "project-link"
        if os.path.lexists(link):
            link.unlink()
        link.symlink_to(project, target_is_directory=True)

        alias = root / "project-alias"
        if os.path.lexists(alias):
            alias.unlink()
        alias.symlink_to(link, target_is_directory=True)

        if root not in case_registry.scratch:
            case_registry.scratch.append(root)

        return Workspace(root=root, project=project, link=link, alias=alias)

    return _make


@pytest.fixture
def engine_factory(db_factory, plan_factory, case_registry) -> Any:
    """A factory for the engine a case drives.

    The engine is bound to *this case's* database and plans tree — the
    two things a worker resolves on every call, and the two things that
    make a leaked worker write into the next case.  The engine is
    registered with the teardown, so a case that forgets to stop it
    still cannot leak it.

    Returns:
        A factory: ``engine_factory(db=None, plans_dir=None,
        capacity=1, tick_seconds=0.01, start=True)``.  ``db`` is a
        :class:`SchedulerDb`; without one the case's own database is
        created and ``PDT_STATE_DB_PATH`` is pointed at it.
    """
    def _make(
        db: Optional[SchedulerDb] = None,
        plans_dir: Optional[Path] = None,
        capacity: int = 1,
        tick_seconds: float = 0.01,
        start: bool = True,
    ) -> SchedulerEngine:
        database = db if db is not None else db_factory()
        # The plans tree is a sibling of the database: one case, one
        # root, one world.
        root = (
            Path(plans_dir)
            if plans_dir is not None
            else Path(database.path).parent / "plans"
        )

        engine = SchedulerEngine(
            database.path,
            root,
            capacity=capacity,
            tick_seconds=tick_seconds,
        )
        case_registry.engines.append(engine)
        if start:
            engine.start()
        return engine

    return _make

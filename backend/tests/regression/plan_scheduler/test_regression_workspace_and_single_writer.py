"""Regression nails for the two remaining historical scheduler defects.

Background
----------
The native scheduler replaced a hand-rolled "one plan at a time" runner
that had four defects worth a nail each.  The first two are nailed in
``test_regression_admission_adoption.py`` (a plan that enters ``queued``
never enters the line, and the adoption is lost across a restart).  The
two nailed here are the other two:

**Defect 3 — the workspace block was lost.**

Two plans may not work in the same directory at the same time.  The
rule is about compute, not about order: only one plan at a time may
hold a directory.  The old implementation enforced the rule and said
nothing about it — the second plan simply sat in the queue, with no
reason, no word on the panel and nothing in the API.  An operator
looking at a panel that says "waiting" goes and looks at the slots;
the plan was not waiting for a slot, it was waiting for a *directory*,
and the two are fixed in different places.

The report is therefore the defect, not the block: the block itself
(``plan_scheduler.capacity.CapacityLedger.workspace_busy``) was
always right.  What was lost is the *reason* — the word that says
which of the two things the plan is waiting for.  So this case drives
the production scheduler with two plans that point at the same
``project_dir`` and reads the answer back out of the production read
model: the engine's own snapshot, the ``GET /api/scheduler/state``
endpoint the panel polls, and the panel's own vocabulary.

**Defect 4 — two writers of the phase.**

The old scheduler kept a shadow of the plan state next to the real
one, and the two were written independently.  Two writers of one
phase is not a slow path and not a race that shows up in the logs: it
is a state in which the panel and the state machine can disagree, and
the disagreement is silent.  This defect is the hardest of the four
to nail, because **it is an absence** — "two writers never happened"
is not a thing a green suite can see.  A suite that only asserted the
*result* ("the phase ended up right") would pass just as well with
two writers as with one: the result is the same, the disagreement is
what differs, and the disagreement only shows up later, somewhere
else, in a different defect.

So this case proves the absence with an **observable write path**:
a SQLite trace over the tick's own connections records every statement
the tick runs, and every write to ``plan_routing`` is recorded with
the **Python call stack** that made it.  A write whose stack does not
contain :class:`plan_lifecycle_service.PlanLifecycleService` is a
second writer, and the case fails on it.  The runtime probe is backed
by a static one: the scheduler package may not import ``requests`` or
``httpx`` at all, so it cannot call its own HTTP endpoint and become
a second writer that way either.

What these cases drive
----------------------
Both cases drive the **production** scheduler — the same engine,
store, facts, ledger, command executor and lifecycle the server
assembles in ``server._build_scheduler_engine`` — and the production
write paths:

* a plan is put in ``queued`` by ``routes.fast_lane._mark_queued``,
  the same write the import makes;
* a plan is started by the lifecycle, which is the only thing allowed
  to move a plan's phase;
* the state is read back through the repositories, through the
  scheduler's own store, and through the mounted HTTP app.

No case writes a scheduling row into ``state.db`` by hand, and the
single-writer case writes **no** phase row at all — it only watches.

The negative controls
---------------------
Both defects are absences, so a green suite cannot tell them apart
from a scheduler that never had the problem.  Each case therefore
names the control that turns it red, and the control is the defect
itself, with the offending step switched off.

``test_regression_workspace_block_is_reported`` — **defect 3**
    Control: ``CapacityLedger.workspace_busy`` stops answering (the
    ledger with that rule deleted).  The second plan is then admitted
    into the directory the first one is working in, and the queue
    reports a reason that does not say ``workspace_busy`` — the case
    fails on it, because the operator is told nothing about the
    directory, which is the defect.

    The second flavour of the same control leaves the block in place
    and only drops the **report**: the queue view stops carrying the
    reason.  The plan still waits, and the queue still says
    ``waiting_for_slot`` — a plan waiting for a directory, reported as
    a plan waiting for a slot.  The case fails on that too.

``test_regression_single_writer`` — **defect 4**
    Control: the scheduler writes the phase itself, next to the
    lifecycle's write (the engine with the write-right boundary
    deleted).  The probe records that second write with a stack that
    does not contain the lifecycle, and the case fails on it.

The method of the control is the same as the sibling file's: a
scratch pytest plugin (outside the repository, never committed)
replaces the one method that implements the step, so the engine is in
exactly the state it would be in with that code deleted — without the
production file being edited, and without a tree left dirty behind
it.

``test_scheduler_module_does_not_import_http_clients`` — the static
    half of **defect 4**.  Its control is inside the case: the scanner
    is pointed at a synthetic source that *does* import a client, and
    it has to find it.  A scanner that finds nothing everywhere is
    indistinguishable from a scanner that finds nothing at all.

    The static gate was exercised against the real package as well: a
    **copy** of the scheduler package with one module tampered with
    was scanned instead of the production one, and the case went red
    on the tampered copy.  The production tree was never edited.

The **workspace rule** itself is nailed by two more cases:
``test_workspace_block_is_recomputed_every_tick`` and
``test_workspace_is_derived_and_never_stored``.  Defect 3 nailed that
the block is *reported*; these two nail the two halves of the rule the
report rests on: that the block is **recomputed** on every tick (the
plan that holds the directory lets go of it the moment its process is
gone — the next tick, without a restart) and that the workspace is
**nowhere but derived** (not a column of the scheduler's tables, not a
cache in the engine's memory).  The first one's control is a workspace
rule that answers "taken" for ever; the second one's control is a row
that carries the workspace, put in by hand.
"""

from __future__ import annotations

import ast
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

import server
from plan_lifecycle_service import PlanLifecycleService
from plan_scheduler.capacity import CapacityLedger
from plan_scheduler.commands import CommandExecutor
from plan_scheduler.config import SchedulerConfigLoader
from plan_scheduler.engine import PlanSchedulerEngine
from plan_scheduler.facts import PlanFactsReader
from plan_scheduler.policy import REASONS
from plan_scheduler.store import SqliteSchedulerStore
from routes import fast_lane
from state_machine.db import connection as db_connection
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository

# ---------------------------------------------------------------------------
# The constants
# ---------------------------------------------------------------------------

#: The stand-in executor.  Resolved through this file's own location, so
#: the path is right on every checkout and no absolute path is written
#: down anywhere.
EXECUTOR = (
    Path(__file__).resolve().parents[2]
    / "e2e"
    / "fixtures"
    / "deterministic_executor.py"
)

#: The backend package — the scheduler's own code.
BACKEND_DIR = Path(__file__).resolve().parents[3]

#: The scheduler package: the engine, the policy, the store, the facts,
#: the commands.  This is what must not reach for an HTTP client, and
#: this is what must not write a plan's phase.
SCHEDULER_PACKAGE_DIR = BACKEND_DIR / "plan_scheduler"

#: The panel's own source.  The panel is the operator's window onto the
#: read model, so "the reason is on the panel" is a question about this
#: file.
FRONTEND_APP_JS = BACKEND_DIR.parent / "frontend" / "app.js"

#: The endpoint the panel polls.
STATE_PATH = "/api/scheduler/state"

#: The moment the scheduler starts at: 2026-01-01T00:00:00Z.  The clock
#: is injected, so the case moves time by hand instead of waiting for
#: it.
BASE_EPOCH = 1767225600.0

#: How long a young run is protected for — the scheduler's M.
MIN_RUNTIME_SECONDS = 600.0

#: How long the scheduler waits between two ticks.  The cases drive the
#: tick by hand, so this only matters to the engine's own bookkeeping.
TICK_SECONDS = 0.05

#: The file the phase's only writer lives in.  A write to
#: ``plan_routing`` whose stack does not pass through this file is a
#: second writer.
LIFECYCLE_MODULE = "plan_lifecycle_service.py"

#: The HTTP clients the scheduler package may not import.  A scheduler
#: that called its own endpoint over HTTP would be a second writer with
#: a network hop in the middle of it.
HTTP_CLIENTS = ("requests", "httpx")


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


class _Clock:
    """A clock the case moves by hand."""

    def __init__(self, now: float) -> None:
        self._now = float(now)

    def __call__(self) -> float:
        """Return the current moment, in epoch seconds."""
        return self._now

    def set(self, now: float) -> None:
        """Move the clock to ``now``."""
        self._now = float(now)


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@dataclass
class _World:
    """One scheduler, over one database, with one plans tree."""

    conn: sqlite3.Connection
    store: SqliteSchedulerStore
    facts: PlanFactsReader
    ledger: CapacityLedger
    engine: PlanSchedulerEngine
    db_path: Path
    sink: "_RecordingSink"


class _RecordingSink:
    """The tick's exit, with the decisions it was handed remembered.

    The production exit — the one ``server._scheduler_sink`` builds — is
    what carries a decision out; this wraps it and remembers what it was
    handed, so that a case can say *the tick decided to admit this plan*
    without reading the decisions out of the engine's own log.

    The wrapper changes nothing: the decisions are carried out by the
    production exit underneath, in the same order and with the same
    dictionaries.  What it adds is the *question* the decisions answer —
    which plan was admitted — in a form a case can read without
    re-deriving it from the world.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._decisions: List[Dict[str, Any]] = []

    def apply(self, decisions: Any) -> Any:
        """Carry the decisions out and remember them."""
        self._decisions.extend(dict(decision) for decision in decisions)
        return self._inner.apply(decisions)

    def decisions(self) -> List[Dict[str, Any]]:
        """Every decision the exit was handed, in order."""
        return list(self._decisions)

    def admits(self) -> List[str]:
        """The plans the tick decided to admit, in order."""
        return [
            str(decision.get("plan_id"))
            for decision in self._decisions
            if decision.get("action") == "admit"
        ]


def _world(tmp_path: Path, clock: _Clock, capacity: int = 1) -> _World:
    """Assemble the scheduler the way the server assembles it.

    The objects are the production ones — the engine, the store, the
    facts reader, the ledger, the command executor and the lifecycle —
    wired the way ``server._build_scheduler_engine`` wires them, with
    the same clock injected into each of them.  The only thing that is
    not production is the configuration file: it is written into the
    case's own directory so that the capacity and the tick cadence are
    the case's to choose.
    """
    config_path = tmp_path / "scheduler.yaml"
    config_path.write_text(
        "scheduler:\n"
        "  enabled: true\n"
        "  dry_run: false\n"
        f"  capacity: {capacity}\n"
        f"  min_runtime_seconds: {MIN_RUNTIME_SECONDS}\n"
        f"  tick_seconds: {TICK_SECONDS}\n"
        "  command_timeout_seconds: 30\n",
        encoding="utf-8",
    )

    loader = SchedulerConfigLoader(config_path)
    db_path = Path(server._state_db_path())
    conn = open_db(db_path)
    migrate(conn)
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, server.PLANS_DIR, clock=clock)
    ledger = CapacityLedger(store, facts, clock=clock, conn=conn)
    lifecycle = PlanLifecycleService()
    commands = CommandExecutor(
        store,
        ledger,
        lifecycle,
        facts=facts,
        conn=conn,
        capacity=capacity,
        min_runtime_seconds=MIN_RUNTIME_SECONDS,
        clock=clock,
    )
    sink = _RecordingSink(
        server._scheduler_sink(loader.current(), lifecycle, facts)
    )
    engine = PlanSchedulerEngine(
        loader,
        store,
        facts,
        ledger,
        commands,
        sink,
        clock=clock,
        conn=conn,
    )
    return _World(
        conn=conn,
        store=store,
        facts=facts,
        ledger=ledger,
        engine=engine,
        db_path=db_path,
        sink=sink,
    )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def _write_plan(plan_id: str, project_dir: Path) -> Path:
    """Write a minimal, legal plan directory for ``plan_id``.

    Three things make a plan directory legal for the scheduler: the task
    list it has to run, the project directory it runs in, and a phase
    the routing table can carry.  The task list is written here; the
    phase is written by :func:`_mark_queued`, the way the import writes
    it.

    The project directory is a real directory of its own, outside
    ``backend/`` — the execution side refuses to work inside the
    service's own tree.
    """
    plan_dir = server._plan_dir(plan_id)
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)

    (plan_dir / "tasks.json").write_text(
        json.dumps(
            {
                "requirement": (
                    "A regression nail: the plan that is blocked must say "
                    "so, and the phase must have exactly one writer."
                ),
                "project_dir": str(project_dir),
                "tasks": [
                    {
                        "id": f"{plan_id}-1",
                        "title": "The one task this minimal plan has",
                        "description": (
                            "The plan exists so that the scheduler has "
                            "something to admit."
                        ),
                        "test_command": "true",
                        "project_dir": str(project_dir),
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return plan_dir


def _mark_queued(plan_id: str, plan_dir: Path) -> None:
    """Put the plan in ``queued`` the way the import path does.

    ``routes.fast_lane._mark_queued`` is the production write path: it
    seeds the routing row and moves the phase through the state store,
    and it is called here directly so that the case drives the same
    writes the import drives.
    """
    fast_lane._mark_queued(plan_id, plan_dir)


# ---------------------------------------------------------------------------
# Reading the world back
# ---------------------------------------------------------------------------


def _execution_row(
    conn: sqlite3.Connection, plan_id: str
) -> Optional[Dict[str, Any]]:
    """Return the plan's execution row, or ``None`` when it has none."""
    return ExecutionRepository(conn).summary(plan_id)


def _phase(conn: sqlite3.Connection, plan_id: str) -> Optional[str]:
    """Return the plan's phase, or ``None`` when the routing row is gone."""
    row = RoutingRepository(conn).current(plan_id)
    if row is None:
        return None
    return row.get("current_phase")


def _pid_is_alive(pid: Optional[int]) -> bool:
    """Return whether the process ``pid`` is still there."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        # It is there, and it belongs to somebody else.
        return True
    return True


def _executor_pids() -> List[int]:
    """The pids of the live processes running the stand-in executor.

    Read from the process table rather than from anything the executor
    said about itself: the count is taken from where the operating
    system keeps it.  Only the children of this process are counted —
    the stand-in is spawned by the scheduler, which runs in this
    process, so a stand-in that belongs to another test is not this
    test's business.

    The two ``w`` options are load-bearing.  ``ps`` cuts the command
    column down to the width of the screen it believes it is writing
    to — eighty columns when it cannot find one — and the stand-in's
    command line is longer than that, because the file name sits at
    the end of a path of a hundred characters.  A cut line does not
    contain the file name, so the filter below drops the very process
    it went looking for.  Here that would be quiet: this helper is
    only ever asked for the process table so the teardown can hand the
    stand-ins back, and a reading that is empty is a teardown that
    signals nobody and leaves the processes behind.  ``-ww`` turns the
    cutting off, so what is read is the whole command line.
    """
    listing = subprocess.run(
        ["ps", "-ww", "-A", "-o", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pids: List[int] = []
    for line in listing.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        try:
            pid, parent = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if parent != os.getpid():
            continue
        if EXECUTOR.name not in fields[2]:
            continue
        pids.append(pid)
    return sorted(pids)


# ---------------------------------------------------------------------------
# The writer probe
# ---------------------------------------------------------------------------


def _is_plan_routing_write(statement: str) -> bool:
    """Return whether ``statement`` writes to the ``plan_routing`` table.

    The word is matched as a whole, so ``plan_routing_v5`` — the
    migration's own table — is not a write to ``plan_routing``.
    """
    text = " ".join(str(statement).split()).upper()
    if not text.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
        return False
    return re.search(r"\bPLAN_ROUTING\b", text) is not None


#: A statement that writes to the phase table, anywhere in a source.
#:
#: The keyword is looked for anywhere in the text, not at the start of a
#: line: a write that is wrapped over three lines is still a write, and
#: a scanner that only reads the first line of it would miss exactly
#: the statement it was written for.
_PLAN_ROUTING_WRITE_RE = re.compile(
    r"(?is)\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM"
    r"|REPLACE\s+INTO)\s*[\"'`\[]?\s*plan_routing\b"
)


def _plan_routing_writes(source: str) -> List[str]:
    """Return the statements in ``source`` that write to ``plan_routing``.

    The statement is read out of the text, so a write is found whether
    it is spelled out in one line or wrapped over several.  A ``SELECT``
    is not a write and is not returned.
    """
    return [
        " ".join(match.split())
        for match in _PLAN_ROUTING_WRITE_RE.findall(source)
    ]


def _stack_frames() -> Tuple[str, ...]:
    """Return the Python call stack that is running right now.

    Each frame is ``"<file>:<function>"``.  The file is the part that
    matters: the question is *which module* made the write, and a
    function name alone would not answer it (``write_plan_state`` is
    called from more than one place).
    """
    return tuple(
        f"{Path(frame.filename).name}:{frame.name}"
        for frame in traceback.extract_stack()
    )


class _WriterProbe:
    """Watch the writes to ``plan_routing`` and who made them.

    The probe is a SQLite trace: SQLite itself reports every statement
    a connection runs, so the record is the database's own account of
    what was written, not the case's guess at it.

    **The trace is scoped to the tick's own connections.**  The tick
    decides and writes on one connection — the one the engine was given
    — and the lifecycle's write goes through a connection of its own,
    opened by the same factory every state-database connection in this
    process goes through.  Both are the tick's: they are the same
    database, and they are opened while the tick is running.  A
    connection to any other database is left alone, so the record
    cannot pick up a write that had nothing to do with this tick.

    The probe keeps two records:

    * ``tick_statements`` — every statement the **tick's own decision
      connection** ran.  This is the scheduler's own write path, and it
      is what says whether the scheduler writes a phase itself.
    * ``writes`` — every write to ``plan_routing``, with the stack that
      made it.  This is the phase's write path, and it is what says
      whether the phase has one writer or two.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path.resolve()
        self.tick_statements: List[str] = []
        self.writes: List[Tuple[str, Tuple[str, ...]]] = []
        self._tick_connection: Optional[sqlite3.Connection] = None
        self._armed = False

    # -- arming ---------------------------------------------------------

    def arm(self, tick_connection: sqlite3.Connection) -> None:
        """Watch the tick's decision connection from now on."""
        self._tick_connection = tick_connection
        self._armed = True
        tick_connection.set_trace_callback(self._on_tick_statement)

    def disarm(self) -> None:
        """Stop watching."""
        if self._tick_connection is not None:
            self._tick_connection.set_trace_callback(None)
        self._armed = False

    def install_on(self, monkeypatch: Any) -> None:
        """Attach the probe to every connection opened for this database.

        The factory is the one chokepoint every state-database
        connection goes through, so attaching here is what makes the
        lifecycle's own connection visible to the probe.  Connections to
        any other database are handed back untouched.
        """
        real_open = db_connection.open
        db_path = self._db_path

        def _open_and_watch(path: Any, *args: Any, **kwargs: Any) -> Any:
            conn = real_open(path, *args, **kwargs)
            try:
                if Path(str(path)).resolve() == db_path:
                    conn.set_trace_callback(self._on_statement)
            except Exception:  # noqa: BLE001 — the probe never breaks a write
                pass
            return conn

        monkeypatch.setattr(db_connection, "open", _open_and_watch)

    # -- the callbacks --------------------------------------------------

    def _on_tick_statement(self, statement: str) -> None:
        """Record a statement the tick's own connection ran."""
        self.tick_statements.append(statement)
        self._on_statement(statement)

    def _on_statement(self, statement: str) -> None:
        """Record a write to ``plan_routing``, with the stack that made it."""
        if not self._armed:
            return
        if _is_plan_routing_write(statement):
            self.writes.append((statement, _stack_frames()))

    # -- what the probe saw ---------------------------------------------

    def scheduler_writes(self) -> List[str]:
        """The phase writes the **scheduler's own connection** made."""
        return [
            statement
            for statement in self.tick_statements
            if _is_plan_routing_write(statement)
        ]

    def writes_outside_the_lifecycle(self) -> List[Tuple[str, Tuple[str, ...]]]:
        """The phase writes that did not pass through the lifecycle."""
        return [
            (statement, frames)
            for statement, frames in self.writes
            if not any(
                frame.startswith(LIFECYCLE_MODULE + ":") for frame in frames
            )
        ]


# ---------------------------------------------------------------------------
# The panel's vocabulary
# ---------------------------------------------------------------------------


def _panel_reason_text() -> Dict[str, str]:
    """Return the reason vocabulary the panel draws.

    Read out of ``frontend/app.js`` — the panel maps the server's words
    to the text it shows, and the mapping is the panel's own.  A reason
    the panel has never heard of is drawn as the bare word, so the
    mapping is what "the panel says it" means.
    """
    source = FRONTEND_APP_JS.read_text(encoding="utf-8", errors="replace")
    match = re.search(
        r"const\s+SCHEDULER_REASON_TEXT\s*=\s*\{(.*?)\}",
        source,
        re.DOTALL,
    )
    assert match is not None, (
        "frontend/app.js has no SCHEDULER_REASON_TEXT: the panel draws no "
        "reason at all, so a reason the API reports is a word nobody "
        "sees"
    )
    body = match.group(1)
    entries = re.findall(
        r"""([A-Za-z_][A-Za-z0-9_]*)\s*:\s*"([^"]*)\"""", body
    )
    return {name: text for name, text in entries}


# ---------------------------------------------------------------------------
# The static scanners
# ---------------------------------------------------------------------------


def _imported_modules(source: str) -> List[str]:
    """Return the top-level modules ``source`` imports.

    Read out of the syntax tree rather than out of the text: a module
    name in a comment or in a docstring is not an import, and a
    scanner that cannot tell the two apart is a scanner that cries wolf
    until somebody stops reading it.
    """
    names: List[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import: it cannot name a third-party
                # package by accident.
                continue
            if node.module:
                names.append(node.module.split(".")[0])
    return names


def _import_lines(source: str) -> List[str]:
    """Return the import statements ``source`` spells out."""
    return [
        line.strip()
        for line in source.splitlines()
        if re.match(r"^\s*(import|from)\s+", line)
    ]


def _quoted_module_names(source: str) -> List[str]:
    """Return the module names ``source`` names in a string literal.

    The dynamic import — ``importlib.import_module("requests")`` — is
    invisible to the syntax tree and to the import lines, and it is
    exactly the shape a caller reaches for when it wants an HTTP
    client without an import statement.  A quoted name is precise: a
    word in prose is not a quoted name.
    """
    return re.findall(r"""["']([A-Za-z_][A-Za-z0-9_.]*)["']""", source)


def _http_client_hits(source: str) -> List[str]:
    """Return every way ``source`` reaches for an HTTP client."""
    hits: List[str] = []
    for name in _imported_modules(source):
        if name in HTTP_CLIENTS:
            hits.append(f"import {name}")
    for line in _import_lines(source):
        for name in HTTP_CLIENTS:
            if re.search(r"\b" + re.escape(name) + r"\b", line):
                hits.append(f"import line: {line}")
    for name in _quoted_module_names(source):
        if name.split(".")[0] in HTTP_CLIENTS:
            hits.append(f"quoted name: {name}")
    return hits


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _executor_override(monkeypatch):
    """Swap the executor for the deterministic stand-in.

    ``PDT_EXECUTOR_CMD`` is the production seam that names the whole
    command the execution side spawns, and the stand-in is a real
    process: a real pid, a real process group, and an exit that
    happens when the case says so.  The stand-in stays alive until it
    is signalled, so the plan is running for as long as the case needs
    it to be.

    The ``PDT_E2E_*`` variables are cleared first: a value that
    happened to be set in the shell the test runs under must not decide
    how long the stand-in lives.
    """
    for name in [key for key in os.environ if key.startswith("PDT_E2E_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "PDT_EXECUTOR_CMD", f'"{sys.executable}" -u "{EXECUTOR}"'
    )


@pytest.fixture(autouse=True)
def _reclaim_executors():
    """Hand back the stand-in processes this case started.

    A stand-in that outlives its case is a stand-in the next case
    counts: the process count these cases assert on is the count of
    the processes this process started, so a leak from the previous case
    would be read as a second process here.
    """
    yield
    for pid in _executor_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _executor_pids():
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# The shared scenario
# ---------------------------------------------------------------------------


def _plan_running_in(tmp_path: Path, clock: _Clock, plan_id: str,
                     project_dir: Path, capacity: int = 2) -> _World:
    """Bring a plan up under a scheduler and return the world running it.

    The plan is waiting before the scheduler looks, so the tick's own
    reconciliation puts it in the line and the same tick admits it.
    The plan is then running with a real process, and the scheduler
    holds its slot.

    The capacity is a parameter because the cases that need room for a
    second plan in the same world have to be able to ask for it: a
    blocked plan and a control for that block are two more plans, and a
    capacity of two would let the capacity — and not the directory —
    decide the control's outcome.
    """
    world = _world(tmp_path, clock, capacity=capacity)
    plan_dir = _write_plan(plan_id, project_dir)
    _mark_queued(plan_id, plan_dir)

    world.engine._run_tick()

    row = _execution_row(world.conn, plan_id)
    assert row is not None, (
        f"the plan {plan_id} was not admitted by the tick: the scheduler "
        f"reported {world.engine.errors!r}"
    )
    assert row["exec_status"] == "running", row
    assert row["exec_pid"] and _pid_is_alive(row["exec_pid"]), row
    assert _phase(world.conn, plan_id) == "executing"
    return world


# ---------------------------------------------------------------------------
# Defect 3 — the workspace block is lost
# ---------------------------------------------------------------------------


def test_regression_workspace_block_is_reported(tmp_path, monkeypatch):
    """Defect 3: a plan blocked by the workspace says so — everywhere.

    Reproduction condition
    ----------------------
    * two plans point at the **same** ``project_dir``; and
    * the first one is working in it, and the second one is waiting.

    The second plan may not start — only one plan at a time may work in
    a directory — and it must **say** so.  The reason travels the whole
    way the operator can see it: the engine's snapshot, the endpoint
    the panel polls, and the panel's own vocabulary.  A reason that
    stops at the engine is a reason the operator never reads.

    Negative control
    ----------------
    Switch the workspace rule off (``CapacityLedger.workspace_busy``
    stops answering, which is the ledger with that rule deleted): the
    second plan is admitted into the directory the first one is working
    in, and the queue reports a reason that does not say
    ``workspace_busy`` — this case fails on it, because the operator is
    told nothing about the directory, which is the defect.

    The other flavour of the same control leaves the block in place and
    drops only the **report**: the queue view stops carrying the reason.
    The plan still waits, and the queue says ``waiting_for_slot`` — a
    plan waiting for a directory, reported as a plan waiting for a
    slot.  This case fails on that as well.
    """
    clock = _Clock(BASE_EPOCH)
    shared = tmp_path / "workspaces" / "shared"

    running_id = "20261028-regression-workspace-p1"
    blocked_id = "20261028-regression-workspace-p2"

    # The first plan takes the directory and starts working in it.
    world = _plan_running_in(tmp_path, clock, running_id, shared)

    # The second plan points at the very same directory.
    blocked_dir = _write_plan(blocked_id, shared)
    _mark_queued(blocked_id, blocked_dir)

    # The next tick sees the directory is taken.
    world.engine._run_tick()

    # The reason the engine reports.  This is the defect, so it is the
    # first thing asserted.
    snapshot = world.engine.snapshot()
    reasons = [str(row["reason"]) for row in snapshot["queue"]]
    assert reasons == ["workspace_busy"], (
        "the blocked plan is not reported as blocked: {0!r}. A plan "
        "waiting for a directory that says nothing is the defect".format(
            reasons
        )
    )

    # A slot was free the whole time: the capacity was two and one
    # plan was working.  So the second plan was not stopped by the
    # lack of a slot — it was stopped by the directory, and the reason
    # above has to say so.
    # The snapshot's capacity carries a source field (the configured
    # capacity or a runtime override), so the comparison takes that
    # field into account -- the shape is still compared for exact
    # equality, so one key too many or too few turns the gate red.
    assert snapshot["capacity"] == {"total": 2, "used": 1, "source": "config"}, snapshot

    # The blocked plan is the one that is waiting — it was never
    # started, and it is still waiting.
    queued = [str(row["plan_id"]) for row in world.store.list_queue(world.conn)]
    assert queued == [blocked_id], queued
    assert _execution_row(world.conn, blocked_id) is None, (
        "the blocked plan was started in a directory that was held"
    )
    assert _phase(world.conn, blocked_id) == "queued"

    # The reason on the endpoint the panel polls.  The engine is the
    # real one, hung on the real handle the route reads.
    monkeypatch.setattr(
        server.app.state, "scheduler_engine", world.engine, raising=False
    )
    client = TestClient(server.app)
    response = client.get(STATE_PATH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "capacity",
        "queue_length",
        "over_capacity",
        "queue",
        "running",
        "generated_at",
    }, body
    assert body["queue_length"] == 1, body
    assert [str(row["reason"]) for row in body["queue"]] == [
        "workspace_busy"
    ], body

    # The reason on the panel.  The panel draws the server's words, so
    # the word the API reported has to be a word the panel knows.
    panel = _panel_reason_text()
    for row in body["queue"]:
        reason = str(row["reason"])
        assert reason in panel, (
            "the panel has no text for {0!r}: the reason the API "
            "reported is a word nobody sees".format(reason)
        )
        assert panel[reason].strip(), (
            "the panel draws {0!r} as an empty cell".format(reason)
        )

    # And the panel's vocabulary is the server's: a reason the server
    # can produce and the panel cannot draw is a reason that never
    # reaches the operator.
    assert set(REASONS) <= set(panel), (
        "the panel cannot draw {0!r}".format(
            sorted(set(REASONS) - set(panel))
        )
    )


# ---------------------------------------------------------------------------
# Defect 4 — two writers of the phase
# ---------------------------------------------------------------------------


def test_regression_single_writer(tmp_path, monkeypatch):
    """Defect 4: the phase has exactly one writer, and it is the lifecycle.

    Reproduction condition
    ----------------------
    * a tick runs, and the tick **starts** a plan — which moves the
      plan's phase from ``queued`` to ``executing``; and
    * every write to ``plan_routing`` is watched while the tick runs.

    A phase with two writers does not look different from a phase with
    one: both end up at ``executing``.  What differs is the *path*, so
    this case watches the path.  Every write the tick's connections
    make to ``plan_routing`` is recorded with the Python stack that
    made it, and a stack that does not pass through
    :class:`plan_lifecycle_service.PlanLifecycleService` is a second
    writer.

    Negative control
    ----------------
    Let the scheduler write the phase itself, next to the lifecycle's
    write (the engine with the write-right boundary deleted): the
    probe records that second write with a stack that does not contain
    the lifecycle, and this case fails on it.
    """
    clock = _Clock(BASE_EPOCH)
    project_dir = tmp_path / "workspaces" / "solo"
    plan_id = "20261028-regression-single-writer-p1"

    world = _world(tmp_path, clock, capacity=2)

    # The plan is waiting before the scheduler looks at it.
    plan_dir = _write_plan(plan_id, project_dir)
    _mark_queued(plan_id, plan_dir)

    # The probe is armed **after** the import: the import's own write
    # of the phase is the import's, not the tick's, and a trace that
    # started earlier would be measuring the wrong thing.
    probe = _WriterProbe(world.db_path)
    probe.install_on(monkeypatch)
    probe.arm(world.conn)
    try:
        world.engine._run_tick()
    finally:
        probe.disarm()

    # The tick moved the phase: the plan is running.
    assert _phase(world.conn, plan_id) == "executing", (
        "the tick did not start the plan, so it never moved the phase "
        "and this case would prove nothing"
    )

    # The probe saw the write.  A probe that never fires proves
    # nothing: the silence above would have been the probe's.
    assert probe.writes, (
        "the probe recorded no write to plan_routing during the tick — "
        "the write never reached the database, so this case would pass "
        "without proving anything"
    )

    # Every one of those writes came from the lifecycle.  This is the
    # defect itself: a write whose stack does not pass through the
    # lifecycle is a second writer, and the phase is then written by
    # two things that do not know about each other.
    offenders = probe.writes_outside_the_lifecycle()
    assert offenders == [], (
        "the phase was written without the lifecycle: {0!r}".format(
            [
                (statement, [f for f in frames if "plan_" in f])
                for statement, frames in offenders
            ]
        )
    )

    # And the scheduler itself wrote nothing: on the connection the
    # tick decides on, not one statement touches the phase.  The
    # scheduler decides *who* runs and *when*; it does not move phases.
    assert probe.scheduler_writes() == [], (
        "the scheduler wrote the phase itself: {0!r}".format(
            probe.scheduler_writes()
        )
    )

    # The same boundary, read out of the scheduler's own source: no
    # module of the scheduler package writes the phase table.  The
    # runtime probe says the writes that happened were the lifecycle's;
    # this says there is no second write path to take.
    #
    # The scanner is not vacuous — a write is found wherever in the
    # file it sits, wrapped over three lines as often as not, and a
    # read is not mistaken for a write.
    for sample in (
        'conn.execute("UPDATE plan_routing SET current_phase = ?", (a,))',
        'conn.execute(\n    "INSERT INTO plan_routing (plan_id) VALUES (?)"\n)',
        'conn.execute("DELETE FROM plan_routing WHERE plan_id = ?", (a,))',
    ):
        assert _plan_routing_writes(sample), (
            "the scanner did not find the write in {0!r}: a scan that "
            "finds nothing proves nothing".format(sample)
        )
    assert not _plan_routing_writes(
        'conn.execute("SELECT current_phase FROM plan_routing")'
    ), "the scanner mistook a read for a write"

    offenders = []
    for path in sorted(SCHEDULER_PACKAGE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        for statement in _plan_routing_writes(source):
            offenders.append(f"{path.name}: {statement}")
    assert offenders == [], (
        "the scheduler package writes the phase table itself: {0!r}".format(
            offenders
        )
    )


# ---------------------------------------------------------------------------
# Defect 4 — the static half: the scheduler may not call itself over HTTP
# ---------------------------------------------------------------------------


def test_scheduler_module_does_not_import_http_clients():
    """The scheduler package may not import an HTTP client.

    A scheduler that called its own endpoint over HTTP would be a
    second writer with a network hop in the middle of it: the phase
    would be written by the process that received the request rather
    than by the one that decided.  The runtime probe above cannot see
    that — the write would come from the lifecycle's stack and look
    perfectly innocent — so the boundary is also stated in the source.

    The control is inside the case: the scanner is pointed at sources
    that *do* reach for a client, and it has to find every one of
    them.  A scanner that finds nothing everywhere is
    indistinguishable from a scanner that finds nothing at all.
    """
    # The control first: the scanner has to be able to see.
    for source, expected in (
        ("import requests\n", "import requests"),
        ("from httpx import AsyncClient\n", "import httpx"),
        ("import requests.adapters\n", "import requests"),
        ("if True:\n    import httpx\n", "import httpx"),
        ('x = importlib.import_module("requests")\n', "quoted name: requests"),
        ('x = importlib.import_module("httpx")\n', "quoted name: httpx"),
    ):
        hits = _http_client_hits(source)
        assert expected in hits, (
            "the scanner did not find {0!r} in {1!r}: it found {2!r}, so "
            "a clean scan proves nothing".format(expected, source, hits)
        )

    # And now the scan itself: the scheduler package, every module of
    # it.
    modules = sorted(SCHEDULER_PACKAGE_DIR.glob("*.py"))
    assert modules, (
        "the scheduler package was not found at {0!r} — a scan of "
        "nothing proves nothing".format(str(SCHEDULER_PACKAGE_DIR))
    )

    offenders: Dict[str, List[str]] = {}
    for path in modules:
        source = path.read_text(encoding="utf-8", errors="replace")
        hits = _http_client_hits(source)
        if hits:
            offenders[path.name] = hits

    assert offenders == {}, (
        "the scheduler package reaches for an HTTP client: {0!r}. A "
        "scheduler that calls its own endpoint is a second writer".format(
            offenders
        )
    )


# ---------------------------------------------------------------------------
# Defect 5 — the scheduler's own three tables have one writer as well
# ---------------------------------------------------------------------------

#: The store methods that write the scheduler's three tables.
#:
#: These are the store's own names: every write to ``scheduler_queue``,
#: ``scheduler_slot`` and ``scheduler_command`` goes through one of them,
#: so wrapping them is what makes the write path observable.
SCHEDULER_TABLE_WRITES = (
    "enqueue",
    "remove",
    "promote",
    "consume_promotion",
    "upsert_slot",
    "delete_slot",
    "record_command",
    "finish_command",
)

#: The routes' directory — the HTTP layer the scheduler is reached from.
ROUTES_DIR = BACKEND_DIR / "routes"


def _tap_scheduler_writes(monkeypatch: Any, writes: List[str]) -> None:
    """Record the thread every write to the three tables is made on.

    The wrapper is put back by ``monkeypatch`` when the case ends, so
    the rest of the suite sees the store as it is.
    """

    def _wrap(name: str, original: Any) -> Any:
        def _traced(self: Any, *args: Any, **kwargs: Any) -> Any:
            writes.append(threading.current_thread().name)
            return original(self, *args, **kwargs)

        _traced.__name__ = name
        return _traced

    for name in SCHEDULER_TABLE_WRITES:
        monkeypatch.setattr(
            SqliteSchedulerStore,
            name,
            _wrap(name, getattr(SqliteSchedulerStore, name)),
        )


def _iso_now() -> str:
    """Return the current moment, the way the state database writes it."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _wait_until(predicate: Any, timeout: float = 10.0) -> bool:
    """Wait for ``predicate`` to become true, and say whether it did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _count(conn: sqlite3.Connection, table: str, plan_id: str) -> int:
    """Return how many rows ``table`` holds for ``plan_id``."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM {0} WHERE plan_id = ?".format(table),
            (plan_id,),
        ).fetchone()[0]
    )


def test_regression_scheduler_tables_have_one_writer(tmp_path, monkeypatch):
    """The three scheduler tables are written by the worker, and by nobody else.

    The scheduler owns three facts of its own: the order in which the
    plans wait, which plans hold a slot, and the commands the operator
    issued.  They are written from four places today — the start-up
    reconciliation, the tick, the command executor and the HTTP layer
    that asked for the command — and only one of them is the worker's.

    The rule this nails is the architecture's invariant 2: *a scheduler
    fact has exactly one writer, and it is the engine's worker thread*.
    The case proves it with an **observable write path**: every write the
    store makes is recorded with the thread that made it, and every
    writer but the worker's is a second writer.

    The case drives the **production** wiring — ``server._build_scheduler_engine``
    — over a real SQLite file, so what is watched is the wiring the
    server ships, not a hand-built world.
    """
    from plan_scheduler.engine import THREAD_NAME

    writes: List[str] = []
    _tap_scheduler_writes(monkeypatch, writes)

    settings = tmp_path / "scheduler.yaml"
    settings.write_text(
        "scheduler:\n"
        "  enabled: true\n"
        "  dry_run: true\n"
        "  capacity: 1\n"
        "  min_runtime_seconds: 600\n"
        "  tick_seconds: 3600\n"
        "  command_timeout_seconds: 30\n",
        encoding="utf-8",
    )
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "_scheduler_settings_path", lambda: settings)
    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))

    import plan_scheduler.dryrun as dryrun

    monkeypatch.setattr(
        dryrun, "DEFAULT_DRYRUN_ROOT", str(tmp_path / "dryrun")
    )

    # The world the start-up reconciliation has to reconcile: a plan
    # that is working (it is adopted, so it gets a slot) and a plan that
    # is waiting without a queue row (it joins the tail, so it gets one).
    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)
        stamp = _iso_now()
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, updated_at) VALUES ('q1', 'queued', ?)",
            (stamp,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, updated_at) VALUES ('r1', 'executing', ?)",
            (stamp,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution (plan_id, attempt_count, "
            "project_dir, exec_pid, exec_status, updated_at) "
            "VALUES ('r1', 1, ?, ?, 'running', ?)",
            (str(tmp_path), os.getpid(), stamp),
        )
        conn.commit()
    finally:
        conn.close()

    runtime = server._build_scheduler_engine()
    engine = runtime.engine
    assert engine is not None, (
        "the production wiring did not build the scheduler engine"
    )

    read_conn = open_db(tmp_path / "state.db")
    try:
        # The start-up reconciliation writes a slot and a queue row — on
        # the worker, which is where every write of the three tables has
        # to happen.
        assert _wait_until(
            lambda: _count(read_conn, "scheduler_slot", "r1") == 1
        ), "the start-up reconciliation never adopted r1"
        assert _wait_until(
            lambda: _count(read_conn, "scheduler_queue", "q1") == 1
        ), "the start-up reconciliation never backfilled q1"

        # A command submitted from the caller's own thread is recorded by
        # the worker — the caller only puts it in the mailbox.
        engine.submit_command("promote", "q1", timeout=10.0)
        assert _wait_until(
            lambda: _count(read_conn, "scheduler_command", "q1") >= 1
        ), "the promote command left no scheduler_command row"

        writers = sorted(set(writes))
        assert writes, (
            "the scheduler never wrote any of its three tables: the "
            "start-up reconciliation and the command log both have to land "
            "somewhere"
        )
        assert writers == [THREAD_NAME], (
            "scheduler_queue/scheduler_slot/scheduler_command were written "
            "by more than the engine's worker thread — the writers were "
            "{0!r}, and the worker is {1!r}".format(writers, THREAD_NAME)
        )
    finally:
        try:
            engine.stop()
        except Exception:  # noqa: BLE001 — the teardown must not raise
            pass
        read_conn.close()


def test_scheduler_routes_do_not_reach_the_scheduler_storage():
    """A route may not open the scheduler's own tables.

    The invariant the case above proves at runtime is stated in the
    source as well: a route that opened the state database and wrote
    ``scheduler_queue`` itself would be a second writer that the runtime
    probe could only catch by accident.

    The scan has a control inside the case: the scanner is pointed at a
    source that *does* reach for the store, and it has to find it — a
    scanner that finds nothing everywhere is indistinguishable from a
    scanner that finds nothing at all.
    """
    forbidden = (
        "from plan_scheduler.store import",
        "from plan_scheduler.queue import",
        "import plan_scheduler.store",
        "import plan_scheduler.queue",
    )

    def _hits(source: str) -> List[str]:
        return [needle for needle in forbidden if needle in source]

    assert _hits("from plan_scheduler.store import SqliteSchedulerStore\n"), (
        "the scanner did not find the store import: a scan that finds "
        "nothing proves nothing"
    )

    modules = sorted(ROUTES_DIR.glob("*.py"))
    assert modules, (
        "the routes directory was not found at {0!r} — a scan of nothing "
        "proves nothing".format(str(ROUTES_DIR))
    )

    offenders: Dict[str, List[str]] = {}
    for path in modules:
        source = path.read_text(encoding="utf-8", errors="replace")
        hits = _hits(source)
        if hits:
            offenders[path.name] = hits

    assert offenders == {}, (
        "the HTTP layer reaches the scheduler's own storage directly: "
        "{0!r}. A route that writes a scheduler fact is a second "
        "writer".format(offenders)
    )


# ---------------------------------------------------------------------------
# VP-015 — the workspace: the block is recomputed, and it is never stored
# ---------------------------------------------------------------------------

#: The scheduler's own four tables.  They are the only tables the
#: scheduler writes, and the question the second case below asks is
#: whether any of them carries a workspace.
SCHEDULER_TABLES = (
    "scheduler_command", "scheduler_queue", "scheduler_slot",
    "scheduler_setting",
)


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _workspace_needles(directory: Path) -> Tuple[str, ...]:
    """The strings that would betray a workspace that was written down.

    Three of them are the same directory: the path as the case wrote it,
    the path as the facts derive it (the real path) and that path
    normalised.  The fourth is the directory's **name** — a workspace
    that was written down under a short name would not carry the long
    one, and the name is what a row would look like if somebody kept
    the workspace instead of the path.
    """
    raw = str(directory)
    real = os.path.realpath(raw)
    return tuple(
        sorted({raw, real, os.path.normpath(real), directory.name})
    )


def _scheduler_table_names(conn: sqlite3.Connection) -> List[str]:
    """The scheduler's own tables, as the database says they are."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name LIKE 'scheduler_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _workspace_leaks_in_tables(
    conn: sqlite3.Connection, needles: Tuple[str, ...]
) -> List[str]:
    """Return every place the scheduler's tables carry a workspace.

    Both halves of a table are read: the **column names** and every
    **value** in every row.  A path in a value is a leak even when no
    column is named after it, and a column that is *named* after the
    workspace is a leak even when no row carries a path yet — a column
    is where the path would go.
    """
    leaks: List[str] = []
    for table in _scheduler_table_names(conn):
        columns = [
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        for column in columns:
            if "workspace" in column.lower():
                leaks.append(
                    "{0}.{1}: the scheduler's own tables hold no "
                    "workspace".format(table, column)
                )
            for needle in needles:
                if needle in column:
                    leaks.append(
                        "{0}.{1}: the column is named after the "
                        "workspace".format(table, column)
                    )
        for row in conn.execute(f"SELECT * FROM {table}").fetchall():
            for index, value in enumerate(row):
                text = "" if value is None else str(value)
                for needle in needles:
                    if needle in text:
                        name = (
                            columns[index]
                            if index < len(columns)
                            else str(index)
                        )
                        leaks.append(
                            "{0}.{1} = {2!r}".format(table, name, text)
                        )
    return leaks


def _mentions_a_workspace(value: Any, needles: Tuple[str, ...]) -> bool:
    """Return whether ``value`` carries one of the workspace needles."""
    if isinstance(value, str):
        return any(needle in value for needle in needles)
    if isinstance(value, dict):
        return any(
            _mentions_a_workspace(key, needles)
            or _mentions_a_workspace(item, needles)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_mentions_a_workspace(item, needles) for item in value)
    return False


def _workspace_caches_in_engine(
    engine: Any, plan_ids: Any, needles: Tuple[str, ...]
) -> List[str]:
    """Return every place the engine keeps a plan id next to a workspace.

    The scan does not look for a **name** — a cache called
    ``_workspace_cache`` and a cache called ``_held`` are the same
    defect — it looks for a **pairing**: a mapping whose key is one of
    the plans and whose value carries the workspace.

    The tick's own state is not such a pairing: it carries the plan id
    and the workspace in two *fields* of one record, and it is rebuilt
    from the database on every tick.  A cache is a plan id with a
    workspace *underneath* it — the answer kept, so that the question
    does not have to be asked again.

    The walk is bounded in depth and remembers what it has already
    seen, so that the engine's own graph — the facts, the ledger, the
    command executor, the exit — can be read without looping.
    """
    offenders: List[str] = []
    seen = set()

    def _is_plan_id(value: Any) -> bool:
        return isinstance(value, str) and value in plan_ids

    def _walk(value: Any, trail: List[str], depth: int) -> None:
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if _is_plan_id(key) and _mentions_a_workspace(item, needles):
                    offenders.append(
                        "{0}[{1!r}] = {2!r}".format(
                            ".".join(trail), key, item
                        )
                    )
                _walk(item, trail + [repr(key)], depth + 1)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for index, item in enumerate(value):
                if (
                    isinstance(item, (list, tuple))
                    and len(item) == 2
                    and _is_plan_id(item[0])
                    and _mentions_a_workspace(item[1], needles)
                ):
                    offenders.append(
                        "{0}[{1}] = {2!r}".format(
                            ".".join(trail), index, item
                        )
                    )
                _walk(item, trail + ["[{0}]".format(index)], depth + 1)
            return
        if isinstance(value, (str, bytes, int, float, bool, type(None))):
            return
        attributes = getattr(value, "__dict__", None)
        if attributes is None:
            return
        marker = id(value)
        if marker in seen:
            return
        seen.add(marker)
        for name, item in attributes.items():
            _walk(item, trail + [str(name)], depth + 1)

    for name, item in vars(engine).items():
        _walk(item, [str(name)], 1)
    return offenders


def _stop_the_process(pid: Any) -> None:
    """Make a process really stop, and wait until it has.

    The signal is the case's way of saying "this run is over"; the wait
    is what makes the observation afterwards mean something.  A case
    that signalled and moved on would be racing the process it is
    talking about.
    """
    os.kill(int(pid), signal.SIGTERM)
    assert _wait_until(lambda: not _pid_is_alive(pid)), (
        "the process {0!r} did not stop".format(pid)
    )


def _workspace_blocked_scenario(
    tmp_path: Path,
    clock: _Clock,
    running_id: str,
    blocked_id: str,
    shared: Path,
    capacity: int = 3,
) -> _World:
    """Bring a plan up in a directory and block a second plan in it.

    The first plan is working in the directory; the second one points at
    the very same directory and is waiting.  The tick that follows sees
    the directory taken, so the second plan is blocked — and it says so.

    The world is returned as it is: the holder is running with a real
    process, and the blocked plan is waiting with a reason.

    Args:
        tmp_path: the case's own directory.
        clock: the clock the world runs on.
        running_id: the id of the plan that takes the directory.
        blocked_id: the id of the plan that is kept out of it.
        shared: the directory both plans point at.
        capacity: how many plans may work at the same time.  It has to
            leave room for the plans a case adds later, or the capacity
            — and not the directory — would decide their outcome.
    """
    world = _plan_running_in(
        tmp_path, clock, running_id, shared, capacity=capacity
    )
    blocked_dir = _write_plan(blocked_id, shared)
    _mark_queued(blocked_id, blocked_dir)
    world.engine._run_tick()
    return world


# ---------------------------------------------------------------------------
# Judgment 2 — the block is recomputed on every tick
# ---------------------------------------------------------------------------


def test_workspace_block_is_recomputed_every_tick(tmp_path, monkeypatch):
    """VP-015 judgment 2: the block is recomputed on every tick.

    The three judgments this case holds together
    ----------------------------------------------
    1. The workspace key is **derived**, in this order: the execution
       row's ``project_dir``, then the ``project_dir`` the task list
       declares, then the workspace a ``prd.json`` / ``interview.json``
       declares as a constraint — and it is normalised with
       ``realpath`` + ``normpath``, so that the directory, a symlink to
       it and a symlink to that symlink are one key.

       Anchors: ``backend/plan_scheduler/facts.py:536`` —
       :meth:`PlanFactsReader.workspace_key` (the rule is written out at
       :539-544, the implementation at :560), and the three-step
       fallback at :562 (:meth:`_declared_workspace`), :603
       (:meth:`_tasks_project_dir`) and :623 (:meth:`_prd_workspace`).

    2. The block is **recomputed on every tick**: the tick reads the
       state out of the database every time it runs — "Nothing is
       cached between the ticks" — and the workspace question is asked
       again every time, from the facts and not from a memory of the
       last answer.

       Anchors: ``backend/plan_scheduler/engine.py:1439``
       (:meth:`_read_state`), the sentence itself at :1443-1444,
       :1479 (:meth:`_rebuild_line`) with the per-plan
       ``workspace_busy`` at :1509-1513, the head of the line that is
       stopped at :1241-1250, and the reason that leaves through the
       panel at :1570-1571 (:meth:`_reason_behind`).

    3. The plan that is blocked **says so**: ``workspace_busy``, with a
       word the panel draws.

    What this case holds down
    --------------------------
    That the second plan is released **by the next tick**, without a
    restart and without a rebuild of the world: the holder stops, the
    tick runs again on the same engine and the same connection, and the
    plan that was waiting is admitted.

    The release is caused by **one** fact and one fact only: the
    holder's process is gone.  There is no pending decision anywhere in
    this case — and the case says so, because a release that came from a
    pending decision would be a different rule, and one this
    architecture does not have: a plan that is still running holds its
    slot whether or not anybody is waiting to hear about it.

    Negative control
    ----------------
    The workspace rule is asked the question it always answers — "is the
    directory taken?" — and it is made to answer "yes" for ever, by
    replacing :meth:`plan_scheduler.capacity.CapacityLedger.workspace_busy`
    with one that always says the directory is taken.  That is the
    scheduler with a **remembered** block: the answer is kept, so the
    question is never asked again, and the blocked plan is never let go.

    The control is given its own pair of plans, in its own directory, and
    it is put in place **after** the holder is up — so that the control
    changes the release and nothing else.  The case then says that the
    plan was *not* admitted, and that the capacity was not the reason:
    a control that was explained by a full capacity would prove nothing
    about the workspace at all.
    """
    clock = _Clock(BASE_EPOCH)
    shared = tmp_path / "workspaces" / "ws-shared-4d2f"

    running_id = "20261028-regression-ws-recompute-p1"
    blocked_id = "20261028-regression-ws-recompute-p2"

    world = _workspace_blocked_scenario(
        tmp_path, clock, running_id, blocked_id, shared
    )

    # -- the block -----------------------------------------------------
    # The reason the engine reports, and the reason the panel would
    # draw: the directory is taken, and the plan says so.
    reasons = [
        str(row["reason"]) for row in world.engine.snapshot()["queue"]
    ]
    assert reasons == ["workspace_busy"], (
        "the blocked plan is not reported as blocked: {0!r}".format(reasons)
    )

    # The blocked plan was never started: it is waiting, and it is still
    # waiting.
    assert _execution_row(world.conn, blocked_id) is None, (
        "the blocked plan was started in a directory that was held"
    )
    assert _phase(world.conn, blocked_id) == "queued"
    assert world.sink.admits() == [running_id], (
        "the tick admitted more than the plan that holds the directory: "
        "{0!r}".format(world.sink.admits())
    )

    # The capacity was not the reason either: there was room the whole
    # time, so the only thing that could stop the second plan was the
    # directory.
    snapshot = world.engine.snapshot()
    assert snapshot["capacity"]["used"] < snapshot["capacity"]["total"], (
        "the capacity was full, so the block was not the workspace's to "
        "give: {0!r}".format(snapshot["capacity"])
    )

    # -- the holder stops ----------------------------------------------
    # The persistent fact that changes is the process: it is gone.  The
    # row may still say "running" — the status says what was intended
    # and the pid says what is still there, and the pid is the truth.
    holder = _execution_row(world.conn, running_id)
    assert holder is not None and holder["exec_pid"], holder
    holder_pid = int(holder["exec_pid"])
    assert _pid_is_alive(holder_pid), holder
    _stop_the_process(holder_pid)
    assert not _pid_is_alive(holder_pid), (
        "the holder's process is still there: the release below would be "
        "a race and not an observation"
    )

    # And nothing else changed: no human was asked about either plan,
    # so the release below cannot be a release that waited for an
    # answer.
    assert not world.facts.has_pending_decision(running_id), (
        "the holder has a pending decision: the release below would not "
        "say what it was caused by"
    )
    assert not world.facts.has_pending_decision(blocked_id), (
        "the blocked plan has a pending decision: the release below would "
        "not say what it was caused by"
    )

    # -- the next tick --------------------------------------------------
    # The same engine, the same connection, no restart and no rebuild of
    # the world: the tick decides again, from what the database says.
    ticks_before = world.engine.ticks
    connection_before = world.engine._conn
    world.engine._run_tick()
    assert world.engine.ticks == ticks_before + 1
    assert world.engine._conn is connection_before, (
        "the engine was given another connection: the second tick would "
        "not be a continuation of the first"
    )

    # The plan that was waiting is admitted: the tick decided to admit
    # it, the slot says it holds compute, and the plan is running with a
    # real process.
    assert blocked_id in world.sink.admits(), (
        "the blocked plan was not admitted on the next tick: the block "
        "was not recomputed, so it was remembered. The admits were "
        "{0!r}".format(world.sink.admits())
    )
    slots = [
        str(row["plan_id"]) for row in world.store.list_slots(world.conn)
    ]
    assert blocked_id in slots, (
        "the blocked plan holds no slot: it was not admitted after all. "
        "The slots were {0!r}".format(slots)
    )
    row = _execution_row(world.conn, blocked_id)
    assert row is not None, (
        "the blocked plan was admitted but never started"
    )
    assert row["exec_status"] == "running", row
    assert row["exec_pid"] and _pid_is_alive(row["exec_pid"]), row
    assert _phase(world.conn, blocked_id) == "executing"

    # And the panel no longer says the directory is taken.
    reasons = [
        str(row["reason"]) for row in world.engine.snapshot()["queue"]
    ]
    assert "workspace_busy" not in reasons, (
        "the snapshot still reports the workspace as busy: {0!r}".format(
            reasons
        )
    )

    # -- the control ----------------------------------------------------
    # The same shape, one directory over: a holder, a plan that is kept
    # out of the directory, and then a workspace rule that never lets go.
    control_running_id = "20261028-regression-ws-recompute-p3"
    control_blocked_id = "20261028-regression-ws-recompute-p4"
    control_dir = tmp_path / "workspaces" / "ws-control-8b1c"

    control_holder_dir = _write_plan(control_running_id, control_dir)
    _mark_queued(control_running_id, control_holder_dir)
    world.engine._run_tick()

    control_blocked_dir = _write_plan(control_blocked_id, control_dir)
    _mark_queued(control_blocked_id, control_blocked_dir)
    world.engine._run_tick()

    # The control itself: the workspace rule answers "taken" for ever.
    monkeypatch.setattr(
        CapacityLedger,
        "workspace_busy",
        lambda self, key: True,
    )

    control_holder = _execution_row(world.conn, control_running_id)
    assert control_holder is not None and control_holder["exec_pid"], (
        "the control's holder was never started, so the control would "
        "prove nothing"
    )
    _stop_the_process(int(control_holder["exec_pid"]))
    world.engine._run_tick()

    # The control's answer: the plan that was waiting is *not* let go.
    # This is what the case above would see if the block were
    # remembered instead of recomputed.
    assert control_blocked_id not in world.sink.admits(), (
        "the control did not work: the blocked plan was admitted anyway, "
        "so the case above cannot tell a recomputed block from a "
        "remembered one"
    )
    assert _execution_row(world.conn, control_blocked_id) is None, (
        "the control did not work: the blocked plan was started anyway"
    )

    # And the control is about the workspace: the capacity was not the
    # reason, and the reason the panel would show is the directory.
    control_snapshot = world.engine.snapshot()
    assert (
        control_snapshot["capacity"]["used"]
        < control_snapshot["capacity"]["total"]
    ), (
        "the control was not attributable: the capacity was full, so the "
        "plan was not admitted for a reason the control did not touch: "
        "{0!r}".format(control_snapshot["capacity"])
    )
    control_reasons = [
        str(row["reason"]) for row in control_snapshot["queue"]
    ]
    assert control_reasons == ["workspace_busy"], (
        "the control was not about the workspace: the plan was held back "
        "for {0!r}".format(control_reasons)
    )


# ---------------------------------------------------------------------------
# Judgment 2 — the workspace is derived, and it is never stored
# ---------------------------------------------------------------------------


def test_workspace_is_derived_and_never_stored(tmp_path):
    """VP-015 judgment 2: the workspace is derived, and never stored.

    The judgment
    ------------
    The workspace of a plan is **derived**, on every tick, from the
    facts — and it is nowhere else.  It is not a column of the
    scheduler's tables, and it is not a cache in the engine's memory:
    a scheduler that kept it would have two answers to the question
    "which directory is this plan working in", and the two would drift.

    The anchors are the ones the case above names: the derivation is
    ``backend/plan_scheduler/facts.py:536``
    (:meth:`PlanFactsReader.workspace_key`) with its three-step fallback
    at :562, :603 and :623; the tick reads the state out of the database
    every time — ``backend/plan_scheduler/engine.py:1439``
    (:meth:`_read_state`), "Nothing is cached between the ticks" at
    :1443-1444 — and the line is rebuilt from the facts at :1479
    (:meth:`_rebuild_line`) with the per-plan ``workspace_busy`` at
    :1509-1513.

    What this case holds down
    --------------------------
    That after the whole scenario — the holder up, the second plan
    blocked, the holder gone, the second plan admitted — **nothing** in
    the scheduler's own tables carries the workspace: not in a column
    name, not in a value.  And that the engine's own memory carries no
    pairing of a plan and a workspace either.

    The tables are the three the scheduler owns
    (``backend/state_machine/db/schema.py:429``, :450 and :465), and
    the case reads them the way the schema writes them: the columns
    from ``PRAGMA table_info`` and the values from every row.

    The engine is read by **value**, not by name: a cache called
    ``_workspace_cache`` and a cache called ``_held`` are the same
    defect, and a case that only knew one of the names would pass the
    moment somebody renamed the other.  What the case looks for is a
    *pairing* — a plan id with a workspace underneath it.

    Negative control
    ----------------
    A row is put into ``scheduler_queue`` **by hand**, carrying the
    workspace, and the case then says that the scan finds it.  A scan
    that finds nothing everywhere is indistinguishable from a scan that
    finds nothing at all.
    """
    clock = _Clock(BASE_EPOCH)
    shared = tmp_path / "workspaces" / "ws-shared-4d2f"

    running_id = "20261028-regression-ws-derived-p1"
    blocked_id = "20261028-regression-ws-derived-p2"

    world = _workspace_blocked_scenario(
        tmp_path, clock, running_id, blocked_id, shared
    )

    # The scenario runs to its end, so that the tables hold everything a
    # full scenario leaves behind: a queue row, a slot row and the
    # admission that came out of them.
    holder = _execution_row(world.conn, running_id)
    assert holder is not None and holder["exec_pid"], holder
    _stop_the_process(int(holder["exec_pid"]))
    world.engine._run_tick()
    assert blocked_id in world.sink.admits(), (
        "the scenario did not run to its end: the blocked plan was never "
        "admitted, so the tables were never given the chance to carry "
        "the workspace"
    )

    needles = _workspace_needles(shared)

    # -- the tables ----------------------------------------------------
    tables = _scheduler_table_names(world.conn)
    assert set(tables) == set(SCHEDULER_TABLES), (
        "the scheduler's own tables are {0!r} and not {1!r}: a scan of "
        "the wrong tables proves nothing".format(tables, SCHEDULER_TABLES)
    )

    leaks = _workspace_leaks_in_tables(world.conn, needles)
    assert leaks == [], (
        "the scheduler's own tables carry the workspace: {0!r}. The "
        "workspace is derived on every tick and written down nowhere".format(
            leaks
        )
    )

    # -- the engine's memory --------------------------------------------
    caches = _workspace_caches_in_engine(
        world.engine, {running_id, blocked_id}, needles
    )
    assert caches == [], (
        "the engine keeps a plan next to a workspace: {0!r}. The "
        "workspace is derived on every tick and kept nowhere".format(
            caches
        )
    )

    # -- the control ----------------------------------------------------
    # A row that carries the workspace, put in by hand.  The scan has to
    # find it: a scan that finds nothing proves nothing.
    leak_id = "20261028-regression-ws-leak-{0}".format(shared)
    stamp = _iso_now()
    world.conn.execute(
        "INSERT INTO scheduler_queue (plan_id, seq, priority, "
        "promote_order, enqueued_at, updated_at) "
        "VALUES (?, 987654, 0, NULL, ?, ?)",
        (leak_id, stamp, stamp),
    )
    world.conn.commit()

    leaks = _workspace_leaks_in_tables(world.conn, needles)
    assert leaks, (
        "the scan did not find the row that was put in by hand: a scan "
        "that finds nothing proves nothing"
    )
    assert any("scheduler_queue" in leak for leak in leaks), leaks

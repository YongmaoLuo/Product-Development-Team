"""The cutover and the rollback switch — one scheduler at any moment.

Background
----------
The PDT-native scheduler replaces a cron that used to do the real
scheduling every thirty minutes.  The change between the two is a
single act, and it has two halves:

* the **cutover** — the old cron is stopped and the new engine is
  turned on.  The two never run at the same time, because two
  schedulers are two writers, and the whole point of the change is
  that there is one;
* the **rollback** — ``scheduler.enabled: false`` in the configuration.
  It stops everything the worker does; it is not a pause.  And it lives
  in the configuration *only*: a button that stops every plan in the
  system is one misclick away from an outage, so there is no such
  button, and there is no endpoint that does what it would do.

What is pinned here
-------------------
1. **The rollback writes nothing.**  ``enabled: false`` makes the tick
   do nothing at all and writes no row: not to the queue, not to the
   slots, not to the commands, and not to a plan's phase.  The rows
   that are already there stay exactly as they were — a rollback stops
   the writing, it does not undo the history.
2. **The cutover leaves one scheduler.**  While the new engine runs,
   exactly one worker exists, at every moment the case looks, and
   asking for a second one does not produce a second scheduler.
3. **The switch is not in the panel and not in the API.**  The route
   table has no global switch, the frontend has no control for one,
   and nothing under ``backend/routes/`` writes the configuration file
   the switch lives in.
4. **The retired scheduler is really gone.**  The port it served
   answers nothing, and its state file is not there.

Every case here asserts the *check* as well as the *state*: the
scanners and the probes are handed a violation on purpose and have to
report it.  A case that passes is a case that looked.

Provenance
----------
The cases answer the upstream decision points the cutover was decided
by:

* **architecture decision point 7** — the switch that turns the
  scheduler on and off is a configuration value, and the configuration
  is the only place it lives.  The panel and the API are checked for a
  write of it, so the decision cannot be undone by a request;
* **test decision point 7** — the cutover is proven by *running* the
  server's own lifespan, not by inspecting it.  The engine that is
  built, the worker thread that is alive and the rows that are written
  are the evidence;
* **test decision point 9** — the retired scheduler is gone when its
  port answers nothing and its state file is not there.  The check is
  bounded and the check itself is shown to be able to fail.

What is not here
----------------
The decommission of the old cron is an operator's act — a real cron on
a real machine.  These cases do not stop it, do not touch a
supervisor and do not reach into the retired process: they assert the
check the release gate runs, and they prove that check can fail.
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import socket
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from unittest.mock import MagicMock

import pytest
import yaml


pytestmark = pytest.mark.integration


_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

#: The repository root.  The frontend bundle and the configuration file
#: are found relative to it, never through an absolute path.
_REPO_ROOT = _BACKEND_DIR.parent

#: The name the scheduler's worker thread carries.  It is the engine's
#: own name, spelled out here so that a change to it shows up as a
#: failure in this file rather than as a silent leak.
THREAD_NAME = "PlanSchedulerEngine"

#: The port the retired scheduler served its read-only page on.
LEGACY_PORT = 8004

#: How long a port probe may take before the case gives up on it.  A
#: probe without a bound is a probe that hangs the whole lane.
PORT_TIMEOUT = 2.0

#: The three tables the scheduler owns, and the one it must never move.
PROBED_TABLES = (
    "scheduler_queue",
    "scheduler_slot",
    "scheduler_command",
    "plan_routing",
)

#: The tables the write probe watches, and the column that names a row
#: in each of them.
PROBED = (
    ("scheduler_queue", "plan_id"),
    ("scheduler_slot", "plan_id"),
    ("scheduler_command", "command_id"),
    ("plan_routing", "plan_id"),
)

#: The timestamp every seeded row carries.  It is in the format the
#: state database writes, because the scheduler compares these strings.
STAMP = "2026-01-01T00:00:00Z"

#: A moment far enough in the future that a plan due at it is not due
#: now: the scheduler leaves it in the line and does not start it.
FUTURE = "2099-01-01T00:00:00Z"

#: The words a "turn it on / turn it off" control is spelled with.
#:
#: The scheduler's four actions — promote, pause, resume, start — are
#: not among them: they act on one plan and cannot stop the world.
SWITCH_WORDS = (
    "enable",
    "disable",
    "toggle",
    "switch",
    "turn_on",
    "turn_off",
    "turnon",
    "turnoff",
    "on_off",
    "off_on",
)

#: A switch word as a whole word.  ``schedulerToggle`` is a helper that
#: shows and hides a table, not a switch: the word is part of a longer
#: name, and a name is not a control.
SWITCH_WORD = re.compile(r"\b(?:%s)\b" % "|".join(SWITCH_WORDS), re.IGNORECASE)

#: A write of the scheduler's own on/off setting.
SETTING_WRITE = re.compile(
    r"scheduler\s*[.\[:]\s*[\"']?enabled\b", re.IGNORECASE
)

#: A string literal in a JavaScript file — single quoted, double quoted
#: or a template.
LITERAL = re.compile(
    r"""(?:"([^"\\]*(?:\\.[^"\\]*)*)"|'([^'\\]*(?:\\.[^'\\]*)*)'|`([^`\\]*(?:\\.[^`\\]*)*)`)"""
)

#: What a statement has to name to be about the configuration file.
CONFIG_MARKERS = (
    "config.yaml",
    "config_paths",
    "config_dir",
    "schedulerconfig",
    "scheduler_config",
    "plan_scheduler",
)

#: What a statement has to do to change a file.
WRITE_MARKERS = (
    "write_text",
    "write_bytes",
    "safe_dump",
    "yaml.dump",
    "json.dump",
    "os.replace",
    "shutil.move",
    "shutil.copy",
    "unlink",
    "remove(",
)

#: Where the retired scheduler's state file used to be, when nobody
#: says otherwise.  The operator who knows better points the case at
#: the real one.
LEGACY_STATE_ENV = "PDT_LEGACY_SCHEDULER_STATE_FILE"

#: The names a retired scheduler's state file is recognised by.
LEGACY_STATE_PATTERNS = (
    "*scheduler*state*",
    "*ac_scheduler*",
    "*scheduler_state*",
)


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def _engine_threads() -> List[threading.Thread]:
    """The scheduler worker threads that are alive right now."""
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == THREAD_NAME and thread.is_alive()
    ]


def _recovery_helpers(server_module: Any) -> List[str]:
    """The server's recovery helpers, by whatever they are called today.

    They are found by the shape of their names rather than pinned: a
    helper that has been renamed away is a fact the case reports, not
    an ``AttributeError`` it raises while setting up.
    """
    return [
        name
        for name in dir(server_module)
        if name.startswith("_")
        and any(marker in name for marker in ("recover", "reconcile"))
        and callable(getattr(server_module, name, None))
    ]


class _FakeState:
    """A real attribute bag.

    A ``MagicMock`` would answer every attribute with another mock, so
    "the engine is ``None``" could not be told apart from "the engine
    is something".
    """


class _FakeApp:
    """The app the lifespan is handed."""

    def __init__(self) -> None:
        self.state = _FakeState()


def _drive(server_module: Any, app: Any, inside: Optional[Callable[[], None]] = None) -> None:
    """Enter the lifespan and run ``inside`` while the process is up."""
    async def _main() -> None:
        async with server_module._lifespan(app):
            if inside is not None:
                inside()

    asyncio.run(_main())


# ---------------------------------------------------------------------------
# The write probe
# ---------------------------------------------------------------------------


def _install_write_probe(conn: sqlite3.Connection) -> None:
    """Record every write to the tables the scheduler may touch.

    The probe is a set of SQLite triggers, so it fires inside the
    writing transaction: a write that is rolled back leaves no trace.
    What the probe records is therefore what was *committed* — which is
    exactly what "the scheduler wrote" has to mean.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS probe_write ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " table_name TEXT NOT NULL,"
        " operation TEXT NOT NULL,"
        " row_key TEXT)"
    )
    for table, key in PROBED:
        for operation, ref in (("insert", "NEW"), ("update", "NEW"), ("delete", "OLD")):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS probe_{table}_{operation} "
                f"AFTER {operation.upper()} ON {table} "
                f"BEGIN INSERT INTO probe_write(table_name, operation, row_key) "
                f"VALUES ('{table}', '{operation}', {ref}.{key}); END"
            )
    conn.commit()


def _probe_writes(db_path: Path) -> List[Tuple[str, str, Optional[str]]]:
    """The writes the probe recorded, in the order they happened."""
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT table_name, operation, row_key FROM probe_write ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()


def _table_rows(db_path: Path, table: str) -> List[Tuple]:
    """Every row of a table, in a stable order."""
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            tuple(row)
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The seeding
# ---------------------------------------------------------------------------


def _seed_plan(
    db_path: Path,
    plan_id: str,
    phase: str,
    version: int = 3,
    next_run_at: Optional[str] = None,
) -> None:
    """Put a plan into a phase, the way the router does.

    ``next_run_at`` is the moment the plan becomes due.  A plan whose
    moment is in the future stays in the line: the scheduler writes its
    own bookkeeping about it and does not start it, which is what makes
    it safe to run a live engine in a case.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, version, updated_at) VALUES (?, ?, ?, ?)",
            (plan_id, phase, version, STAMP),
        )
        if next_run_at is not None:
            conn.execute(
                "INSERT OR REPLACE INTO plan_execution "
                "(plan_id, next_run_at, updated_at) VALUES (?, ?, ?)",
                (plan_id, next_run_at, STAMP),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_scheduler_rows(db_path: Path) -> None:
    """Give the scheduler's own tables the rows a running scheduler leaves.

    The rows belong to a plan that is not waiting and not working — a
    plan a running scheduler would reconcile away.  They are here so
    that "the rollback changed nothing" is a statement about rows that
    exist, not about an empty table.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO scheduler_queue "
            "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
            "VALUES ('p-other', 1, 0, NULL, ?, ?)",
            (STAMP, STAMP),
        )
        conn.execute(
            "INSERT OR REPLACE INTO scheduler_slot "
            "(plan_id, acquired_at, updated_at) VALUES ('p-other', ?, ?)",
            (STAMP, STAMP),
        )
        conn.execute(
            "INSERT OR REPLACE INTO scheduler_command "
            "(plan_id, action, outcome, detail, created_at) "
            "VALUES ('p-other', 'promote', 'ok', 'seeded', ?)",
            (STAMP,),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The port probe
# ---------------------------------------------------------------------------


def _port_is_reachable(port: int, timeout: float = PORT_TIMEOUT) -> bool:
    """Whether something is listening on ``port`` right now.

    The connect is bounded by ``timeout``: a port that never answers
    must not hang the case that asks about it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _legacy_state_files(roots: Sequence[Path]) -> List[Path]:
    """The retired scheduler's state files, if any are there.

    The scan is bounded: the roots it is given, one level deep.  A
    retired scheduler's state file is a file whose *name* says what it
    is, so the name is what is looked for.
    """
    found: List[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in LEGACY_STATE_PATTERNS:
            found.extend(sorted(root.glob(pattern)))
    return found


# ---------------------------------------------------------------------------
# The scanners
# ---------------------------------------------------------------------------


def _is_global_switch_path(path: str) -> bool:
    """Whether a route path is a global on/off switch for the scheduler.

    The scheduler's own paths — the read model and the four per-plan
    actions — are not switches: they act on one plan, and they cannot
    stop the world.
    """
    low = path.lower()
    return "scheduler" in low and any(word in low for word in SWITCH_WORDS)


def _strip_comments(text: str) -> str:
    """The source without its comments.

    Prose about a switch is not a control for one, and a comment that
    says "the operator can turn the scheduler off" must not be read as
    a way to turn it off.  The string literals are kept — they are the
    names and the paths the scan is about — and a comment marker
    inside a string is not a comment.
    """
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "/" and text[index : index + 2] == "//":
            end = text.find("\n", index)
            index = length if end == -1 else end
            continue
        if char == "/" and text[index : index + 2] == "/*":
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        if char == "<" and text[index : index + 4] == "<!--":
            end = text.find("-->", index + 4)
            index = length if end == -1 else end + 3
            continue
        if char in "\"'`":
            end = index + 1
            while end < length:
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == char:
                    break
                end += 1
            out.append(text[index : end + 1])
            index = end + 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _frontend_switch_lines(text: str) -> List[str]:
    """The places in a frontend file that could switch the scheduler.

    Three shapes are a global on/off control:

    * a **setting write** — ``scheduler.enabled = false``;
    * a **request** — a path that carries a switch word
      (``/api/scheduler/enabled``);
    * a **control** — an element whose name carries one
      (``id="btn-scheduler-enable"``).

    The panel's own vocabulary is not a violation: a helper that shows
    and hides a table (``schedulerToggle(el, visible)``) is a rendering
    detail, not a switch.  So the scan reads the file's *literals* —
    the names and the paths — and leaves the comments out.
    """
    findings: List[str] = []
    source = _strip_comments(text)

    for match in LITERAL.finditer(source):
        literal = next(group for group in match.groups() if group is not None)
        low = literal.lower()
        if "scheduler" not in low:
            continue
        if SWITCH_WORD.search(low):
            line = source.count("\n", 0, match.start()) + 1
            findings.append(f"{line}: control {literal.strip()!r}")

    for match in SETTING_WRITE.finditer(source):
        line = source.count("\n", 0, match.start()) + 1
        findings.append(f"{line}: setting write {match.group(0)!r}")

    return findings


#: The end of a line, as the Python parser reads it: a carriage
#: return and a newline are one ending, and a form feed is not an
#: ending at all.  ``str.splitlines`` would break on a form feed and
#: on a handful of other characters the parser treats as ordinary
#: whitespace, which would put every line number after one out of
#: step with the tree.
_LINE_ENDING = re.compile(r"\r\n|\r|\n")


def _source_lines(source: str) -> List[str]:
    """The source split into lines, each keeping its own ending.

    The split the parser makes, done once for the whole file.  The
    lines keep their endings so that a statement running over
    several of them can be cut out of them without a second pass.
    """
    lines: List[str] = []
    start = 0
    for match in _LINE_ENDING.finditer(source):
        end = match.end()
        lines.append(source[start:end])
        start = end
    if start < len(source):
        lines.append(source[start:])
    return lines


def _segment(lines: List[bytes], node: ast.stmt) -> Optional[str]:
    """The text of a statement, cut out of the lines already split.

    This is the text ``ast.get_source_segment`` would have returned,
    without its cost: that call re-read the whole file for every
    statement, which made a scan of the route modules quadratic —
    slow enough to push the release gate over its time budget.

    The columns the tree carries are *byte* offsets, so the cut is
    made on the encoded line and decoded afterwards; a line that
    carries non-ASCII text would otherwise be cut in the wrong
    place.
    """
    if node.end_lineno is None or node.end_col_offset is None:
        return None
    lineno = node.lineno - 1
    end_lineno = node.end_lineno - 1
    if end_lineno == lineno:
        chunk = lines[lineno][node.col_offset : node.end_col_offset]
    else:
        chunk = (
            lines[lineno][node.col_offset :]
            + b"".join(lines[lineno + 1 : end_lineno])
            + lines[end_lineno][: node.end_col_offset]
        )
    return chunk.decode()


def _config_write_statements(source: str) -> List[str]:
    """The statements in a module that could write the configuration file.

    The switch lives in a file, so the only way an API could flip it is
    by writing that file.  A statement is read as a whole — a write
    whose path is named on the next line is still a write — and a
    docstring is prose, not code, so it is not a statement to judge.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - a module that does not parse
        return []

    lines = [line.encode() for line in _source_lines(source)]

    hits: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue  # a docstring is prose, not code
        text = _segment(lines, node) or ""
        low = text.lower()
        if any(marker in low for marker in CONFIG_MARKERS) and any(
            marker in low for marker in WRITE_MARKERS
        ):
            hits.append(text.strip().splitlines()[0])
    return hits


# ---------------------------------------------------------------------------
# The fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def server_module():
    """The ``server`` module — the flat one, the way production imports it.

    The flat name is the one the suite's own fixtures patch (they set
    ``server.PLANS_DIR`` and ``server._state_db_path``), so a case that
    imported ``backend.server`` would be looking at a second module
    object with a second copy of everything.
    """
    import importlib

    return importlib.import_module("server")


@pytest.fixture
def startup(server_module, monkeypatch):
    """The lifespan, with the parts that are not under test stubbed out.

    The recovery helpers are replaced by no-ops: they recover the plans
    that were running, which is not what these cases are about, and
    they would touch the case's world.  The heartbeat monitor and the
    Feishu notifier are replaced by mocks so that no second thread is
    started behind the case's back — a case that counts threads has to
    be sure the threads it counts are the ones it started.
    """
    for name in _recovery_helpers(server_module):
        monkeypatch.setattr(server_module, name, lambda *a, **k: None, raising=False)

    monkeypatch.setattr(server_module, "load_providers", lambda: [], raising=False)
    monkeypatch.setattr(server_module, "heartbeat_monitor", MagicMock(), raising=False)
    monkeypatch.setattr(
        "notifications.feishu_notifier.FeishuNotifier", MagicMock(), raising=False
    )
    return server_module


@pytest.fixture
def settings(tmp_path, monkeypatch, server_module):
    """The scheduler's settings file — owned by this case.

    The server reads the ``scheduler:`` section of its own
    ``config.yaml``; the path it reads is a seam, so a case can say
    what the scheduler is told without touching the repository's file.
    """
    path = tmp_path / "config.yaml"

    def _write(**scheduler: Any) -> Path:
        path.write_text(
            yaml.safe_dump({"scheduler": scheduler}), encoding="utf-8"
        )
        return path

    monkeypatch.setattr(
        server_module, "_scheduler_settings_path", lambda: path, raising=False
    )
    return _write


@dataclass
class _World:
    """The case's world: one database, one plans tree.

    Attributes:
        db_path: the state database the run reads and writes.
        plans_dir: the plans tree the run looks at.
    """

    db_path: Path
    plans_dir: Path


@pytest.fixture
def world(db_factory, case_registry, tmp_path, monkeypatch, server_module) -> _World:
    """The database and the plans tree this case's run sees.

    The database is the case's own (``db_factory`` points
    ``PDT_STATE_DB_PATH`` at it, and the server resolves the database
    on every call), and the plans tree is the case's own too.  A case
    that ran against the operator's world would be a case whose
    assertions mean nothing.

    The plans tree is registered with the case registry, so the
    directory's cleanup contract gives it back: a tree that outlives
    its case is a tree the next case can read.
    """
    db = db_factory()
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    case_registry.scratch.append(plans_dir)
    monkeypatch.setattr(server_module, "PLANS_DIR", str(plans_dir), raising=False)
    return _World(db_path=Path(db.path), plans_dir=plans_dir)


@pytest.fixture
def engine_probe(monkeypatch) -> List[Any]:
    """The engines this case's run builds.

    The engine class is wrapped rather than replaced: the case needs
    to know *how many* schedulers were built, and it needs the real
    engine — the real worker thread, the real collaborators.
    """
    from plan_scheduler import engine as engine_module

    built: List[Any] = []
    real_engine = engine_module.PlanSchedulerEngine

    class _ProbedEngine(real_engine):  # type: ignore[misc, valid-type]
        """The engine, counted."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built.append(self)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(engine_module, "PlanSchedulerEngine", _ProbedEngine)
    return built


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def test_disabled_rollback_stops_all_writes(
    server_module, startup, settings, world
) -> None:
    """``enabled: false`` — the worker ticks and nothing is written.

    The rollback switch is a switch, not a pause.  A scheduler that is
    switched off must write no row of its own and must not move a
    plan's phase.  Its worker is still there — the switch is read on
    every tick, so it can be turned on without a restart — and the tick
    it runs is a tick that does nothing at all.

    The rows that are already there stay exactly as they were.  The
    queue row of a plan that is not waiting is a row a *running*
    scheduler would reconcile away — the rollback has to leave it alone,
    because a rollback stops the writing, it does not undo the
    history.

    The last half of the case is the negative control: the switch is
    turned on and the very same probe has to record a write.  A probe
    that never fires would have made every assertion above true.
    """
    # A plan that is waiting but not due: the scheduler writes its own
    # bookkeeping about it and leaves it in the line.
    _seed_plan(
        world.db_path,
        "p-waiting",
        "queued",
        version=3,
        next_run_at=FUTURE,
    )
    _seed_scheduler_rows(world.db_path)

    conn = sqlite3.connect(str(world.db_path))
    try:
        _install_write_probe(conn)
    finally:
        conn.close()

    before = {table: _table_rows(world.db_path, table) for table in PROBED_TABLES}

    settings(
        enabled=False,
        dry_run=False,
        capacity=4,
        min_runtime_seconds=600,
        tick_seconds=0.05,
        command_timeout_seconds=1,
    )

    app = _FakeApp()
    observed: Dict[str, Any] = {}

    def _inside() -> None:
        engine = app.state.scheduler_engine
        observed["engine"] = engine
        observed["threads"] = [t.name for t in _engine_threads()]
        # The worker has to tick: a worker that does not tick cannot
        # notice that the switch was turned on.
        deadline = time.monotonic() + 5.0
        while (
            engine is not None
            and engine.ticks < 2
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        observed["ticks"] = engine.ticks if engine is not None else 0

    _drive(server_module, app, _inside)

    # -- the worker -------------------------------------------------------
    assert observed["engine"] is not None, (
        "the scheduler's worker was not built — the switch is read on "
        "every tick, so it can be turned on without a restart"
    )
    assert observed["threads"] == [THREAD_NAME], (
        "the scheduler's worker thread is not the one it should be: "
        f"{observed['threads']!r}"
    )
    assert observed["ticks"] >= 1, (
        "the worker never ticked — a worker that does not tick cannot "
        "notice that the switch was turned on"
    )

    # -- the writes -------------------------------------------------------
    writes = _probe_writes(world.db_path)
    assert writes == [], (
        "a disabled scheduler wrote to the state database: "
        f"{writes!r}"
    )
    for table in PROBED_TABLES:
        assert _table_rows(world.db_path, table) == before[table], (
            f"the rollback changed the rows of {table!r}: "
            f"{before[table]!r} -> {_table_rows(world.db_path, table)!r}. "
            "A rollback stops the writing; it does not undo the history."
        )

    # -- the negative control ---------------------------------------------
    settings(
        enabled=True,
        dry_run=False,
        capacity=4,
        min_runtime_seconds=600,
        tick_seconds=0.05,
        command_timeout_seconds=1,
    )

    _drive(server_module, _FakeApp())

    writes = _probe_writes(world.db_path)
    assert any(
        write[0] == "scheduler_queue" and write[1] == "insert" for write in writes
    ), (
        "the probe recorded no write after the scheduler was switched on: "
        f"{writes!r}. A probe that never fires proves nothing — the "
        "silence above would have been the probe's, not the switch's."
    )
    assert _table_rows(world.db_path, "plan_routing") == before["plan_routing"], (
        "the scheduler moved a plan's phase: "
        f"{before['plan_routing']!r} -> "
        f"{_table_rows(world.db_path, 'plan_routing')!r}"
    )


def test_single_scheduler_after_cutover(
    server_module, startup, settings, world, engine_probe
) -> None:
    """After the cutover there is exactly one scheduler — at any moment.

    The cutover is "stop the old cron, then turn the new one on"; the
    two never run at the same time, because two schedulers are two
    writers.  What the case proves is the invariant the switch has to
    keep: while the new engine runs, exactly one worker exists, and
    asking for a second one does not produce a second scheduler.

    The writer probe is the case's own: it counts the engines that were
    built, it counts the worker threads that are alive, and it records
    every write the scheduler committed.  One engine and one thread
    mean there was one scheduler, so the writes are its.
    """
    # A plan that is waiting but not due: the engine has bookkeeping to
    # do about it, and nothing to start.
    _seed_plan(
        world.db_path,
        "p-waiting",
        "queued",
        version=3,
        next_run_at=FUTURE,
    )

    conn = sqlite3.connect(str(world.db_path))
    try:
        _install_write_probe(conn)
    finally:
        conn.close()

    # -- before the cutover -----------------------------------------------
    settings(
        enabled=False,
        dry_run=False,
        capacity=4,
        min_runtime_seconds=600,
        tick_seconds=0.05,
        command_timeout_seconds=1,
    )

    before_app = _FakeApp()
    before_threads: List[threading.Thread] = []

    def _before_inside() -> None:
        before_threads.extend(_engine_threads())

    _drive(server_module, before_app, _before_inside)

    assert len(engine_probe) == 1, (
        "the scheduler's worker was not built before the cutover: "
        f"{[type(e).__name__ for e in engine_probe]!r}"
    )
    assert len(before_threads) == 1, (
        "the scheduler's worker was not running before the cutover: "
        f"{[t.name for t in before_threads]!r}"
    )
    assert _probe_writes(world.db_path) == [], (
        "something wrote before the cutover: "
        f"{_probe_writes(world.db_path)!r}"
    )

    # -- the cutover ------------------------------------------------------
    settings(
        enabled=True,
        dry_run=False,
        capacity=4,
        min_runtime_seconds=600,
        tick_seconds=0.05,
        command_timeout_seconds=1,
    )

    observed: Dict[str, Any] = {}

    def _inside() -> None:
        engine = app.state.scheduler_engine

        # The engine is really running: it has ticked at least once.
        deadline = time.monotonic() + 5.0
        while engine.ticks < 1 and time.monotonic() < deadline:
            time.sleep(0.01)

        observed["ticks"] = engine.ticks

        # The invariant, sampled: at every moment the case looks, there
        # is exactly one scheduler running.
        samples = []
        for _ in range(20):
            samples.append(len(_engine_threads()))
            time.sleep(0.01)
        observed["samples"] = samples

        # And asking for a second one does not produce a second one.
        again = server_module._start_scheduler(app)
        observed["again_is_same"] = again.engine is engine
        observed["after_again"] = len(_engine_threads())

    app = _FakeApp()
    built_before_cutover = len(engine_probe)
    _drive(server_module, app, _inside)

    assert len(engine_probe) == built_before_cutover + 1, (
        f"the cutover built {len(engine_probe) - built_before_cutover} "
        f"schedulers: "
        f"{[type(e).__name__ for e in engine_probe]!r}. Two schedulers are "
        "two writers."
    )
    assert observed["ticks"] >= 1, (
        "the engine was built but never ticked — the case cannot claim "
        "the scheduler was running"
    )
    assert set(observed["samples"]) == {1}, (
        "the number of running schedulers changed while the engine ran: "
        f"{observed['samples']!r}. At any moment there is exactly one."
    )
    assert observed["again_is_same"], (
        "a second start handed back a different engine — the second "
        "scheduler would have been a second writer"
    )
    assert observed["after_again"] == 1, (
        "a second start produced a second worker: "
        f"{observed['after_again']!r} schedulers are running"
    )

    writes = _probe_writes(world.db_path)
    assert writes, (
        "the scheduler ran but wrote nothing — the case cannot claim the "
        "one scheduler that ran was the writer"
    )

    # -- after the shutdown -----------------------------------------------
    assert _engine_threads() == [], (
        "the worker outlived the process: "
        f"{[t.name for t in _engine_threads()]!r}"
    )


def test_no_global_toggle_in_ui_or_api(server_module) -> None:
    """The rollback switch is in the configuration and nowhere else.

    The switch stops every plan in the system.  A button for it is one
    misclick away from an outage, so the panel does not have one, and
    the API does not have an endpoint that does what it would do.

    Three surfaces are checked, and each check is checked against a
    violation on purpose: the route table, the frontend bundle and the
    route modules' source.
    """
    # -- the route table --------------------------------------------------
    paths = [route.path for route in server_module.iter_app_routes()]
    assert paths, (
        "the app serves no routes at all — the route table cannot be "
        "read, so the check below would pass on nothing"
    )

    toggles = sorted(path for path in paths if _is_global_switch_path(path))
    assert toggles == [], (
        "the API exposes a global switch for the scheduler: "
        f"{toggles!r}. The rollback switch lives in the configuration."
    )

    scheduler_paths = sorted(path for path in paths if "scheduler" in path.lower())
    assert "/api/scheduler/state" in scheduler_paths, (
        f"the scheduler's read model is gone: {scheduler_paths!r}"
    )
    for action in ("promote", "pause", "resume", "start"):
        assert f"/api/plan/{{plan_id}}/scheduler/{action}" in scheduler_paths, (
            f"the scheduler's {action} action is gone: {scheduler_paths!r}"
        )
    for path in scheduler_paths:
        if path == "/api/scheduler/state":
            continue
        assert path.startswith("/api/plan/"), (
            f"a scheduler route that is not about one plan: {path!r}. The "
            "read model and the per-plan actions are the whole of it."
        )

    # The predicate has to be able to fail.
    assert _is_global_switch_path("/api/scheduler/enabled"), (
        "the route predicate does not recognise a global switch"
    )
    assert not _is_global_switch_path("/api/scheduler/state"), (
        "the route predicate mistakes the read model for a switch"
    )

    # -- the frontend -----------------------------------------------------
    frontend = _REPO_ROOT / "frontend"
    frontend_files = sorted(
        path for path in frontend.rglob("*") if path.is_file()
    )
    assert frontend_files, (
        f"the frontend bundle at {frontend} is empty — the scan below "
        "would pass on nothing"
    )

    hits: List[str] = []
    for path in frontend_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in _frontend_switch_lines(text):
            hits.append(f"{path.relative_to(_REPO_ROOT)}:{line}")
    assert hits == [], (
        "the frontend offers a control for the scheduler's switch: "
        f"{hits!r}. The rollback switch lives in the configuration."
    )

    # The scanner has to be able to fail.
    planted = (
        "await fetch('/api/scheduler/enabled', { method: 'POST' });\n"
        "scheduler.enabled = false;\n"
    )
    assert _frontend_switch_lines(planted), (
        "the frontend scanner does not recognise a control for the switch"
    )
    assert not _frontend_switch_lines(
        "const state = await fetch('/api/scheduler/state').then(r => r.json());\n"
        "schedulerToggle($('#scheduler-queue-table'), false);\n"
    ), (
        "the frontend scanner mistakes the read model — or the panel's own "
        "show/hide helper — for a control for the switch"
    )

    # -- the route modules ------------------------------------------------
    route_modules = sorted((_BACKEND_DIR / "routes").glob("*.py"))
    assert route_modules, "there are no route modules to scan"

    writes: List[str] = []
    for path in route_modules:
        source = path.read_text(encoding="utf-8")
        for statement in _config_write_statements(source):
            writes.append(f"{path.relative_to(_REPO_ROOT)}: {statement}")
    assert writes == [], (
        "a route module writes the configuration file the switch lives "
        f"in: {writes!r}. The rollback switch lives in the configuration, "
        "and only in the configuration."
    )

    # The scanner has to be able to fail.
    planted_source = (
        "def set_enabled(flag: bool) -> None:\n"
        "    path = config_paths.CONFIG_DIR / 'config.yaml'\n"
        "    path.write_text(yaml.safe_dump({'scheduler': {'enabled': flag}}))\n"
    )
    assert _config_write_statements(planted_source), (
        "the configuration scanner does not recognise a write of the "
        "scheduler's switch"
    )


def test_config_write_scanner_reads_the_text_the_parser_locates() -> None:
    """The scanner cuts a statement out exactly where the parser says.

    The scanner used to ask the parser for the text of every
    statement, one statement at a time, and the parser re-read the
    whole file for each of them — a quadratic cost that pushed the
    release gate over its time budget.  It now cuts the text out of
    the lines it has already split, and the cut has to land in the
    same place: a statement read as the wrong text would change
    which ones look like a write of the configuration file, and a
    scanner that reads the wrong bytes is a scanner that can be
    fooled.
    """
    sources = [
        # A plain statement.
        "path.write_text('x')\n",
        # Text before the statement on the same line, and text that
        # is not ASCII: the columns the tree carries are *byte*
        # offsets, so a cut that counts characters reads the wrong
        # place.
        "if True:\n    变量 = 1; path.write_text('x')\n",
        # A statement that runs over several lines.
        "result = (\n    path\n    .write_text(\n        'x'\n    )\n)\n",
        # A docstring: prose, not a statement to judge.
        '"""A module."""\nimport os\n',
        # A form feed: the parser does not take it for the end of a
        # line — ``str.splitlines`` does, and would put every line
        # number after it out of step.
        "x = 1\n\fy = 2\n",
        # A carriage return, and a carriage return and a newline.
        "x = 1\ry = 2\r\nz = 3\n",
        # A comment between two statements.
        "x = 1  # a note\ny = 2\n",
        # A tab, and a string that carries a quote of its own.
        "if True:\n\tvalue = 'it\\'s'\n",
        # A file with no ending at all.
        "x = 1",
    ]

    for source in sources:
        tree = ast.parse(source)
        lines = _source_lines(source)

        parser_lines = getattr(ast, "_splitlines_no_ff", None)
        if parser_lines is not None:
            assert lines == parser_lines(source), (
                "the scan splits the source differently from the parser: "
                f"{lines!r} != {parser_lines(source)!r}"
            )

        encoded = [line.encode() for line in lines]
        for node in ast.walk(tree):
            if not isinstance(node, ast.stmt):
                continue
            assert _segment(encoded, node) == ast.get_source_segment(source, node), (
                "the scan reads a statement differently from the parser: "
                f"{_segment(encoded, node)!r} != "
                f"{ast.get_source_segment(source, node)!r}"
            )

    # And on the real thing: a route module, statement by statement.
    module = _BACKEND_DIR / "routes" / "debug.py"
    source = module.read_text(encoding="utf-8")
    tree = ast.parse(source)
    encoded = [line.encode() for line in _source_lines(source)]
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        assert _segment(encoded, node) == ast.get_source_segment(source, node), (
            f"the scan reads {module.name} differently from the parser"
        )


def test_port_8004_closed_after_decommission(tmp_path) -> None:
    """The retired scheduler's port answers nothing, and its state file is gone.

    The decommission of the old cron is an operator's act — a real cron
    on a real machine.  This case does not stop it, does not touch a
    supervisor and does not reach into the retired process: it asserts
    the check the release gate runs, and it proves that the check can
    fail.

    The port probe is bounded: a port that never answers must not hang
    the case that asks about it.
    """
    # -- the check can fail: a port that is listening is reported --------
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        listening_port = listener.getsockname()[1]
        assert _port_is_reachable(listening_port), (
            "the port probe does not notice a port that is listening — "
            "the check below would pass on a broken probe"
        )
    finally:
        listener.close()

    assert not _port_is_reachable(listening_port), (
        "the port probe still reports a port that has been closed"
    )

    # -- the check can fail: a state file that is there is reported ------
    planted = tmp_path / "ac_scheduler_state.json"
    planted.write_text("{}", encoding="utf-8")
    assert _legacy_state_files([tmp_path]), (
        "the state-file scan does not notice a retired scheduler's state "
        "file — the check below would pass on nothing"
    )

    # -- and today neither of them is there ------------------------------
    reachable = _port_is_reachable(LEGACY_PORT)
    assert not reachable, (
        f"port {LEGACY_PORT} is still reachable — the retired scheduler "
        "is not decommissioned"
    )

    override = os.environ.get(LEGACY_STATE_ENV)
    if override:
        state_file = Path(override).expanduser()
        assert not state_file.exists(), (
            f"the retired scheduler's state file is still there: {state_file}"
        )
    else:
        found = _legacy_state_files(
            [_REPO_ROOT / ".pdt", _REPO_ROOT]
        )
        assert found == [], (
            "the retired scheduler's state file is still there: "
            f"{[str(path) for path in found]!r}"
        )
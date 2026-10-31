"""The executor's spawn and exit are written to the process ledger.

Why this exists
---------------
The in-memory handle to the executor dies with the backend process.
After a restart, nothing knows which pid the plan started, and every one
of them is an orphan nobody will ever reap. The ledger
(``backend/process_ledger.py``) is the durable answer to that question
-- but it is only an answer if something writes to it, and the one place
in the backend that starts an executor is
``routes/execution.py::_spawn_executor_subprocess``.

So this pins the two moments that make the ledger worth having:

* **the spawn** — the moment the process exists, a record names it:
  its pid, its process group, the identity the kernel gave it, and who
  started it. It is written *after* the process exists, so a record can
  never name a pid that was never used;
* **the exit** — the moment the process is gone, an exit event says so,
  so the ledger's live set does not keep a pid that no longer runs.

Two properties make the instrumentation safe to trust:

* **a ledger failure never blocks the work.** The ledger is evidence,
  not a gate: a read-only plan directory or a full disk must not stop
  the execution that was requested. The failure is logged and the
  run continues;
* **the identity is read, not guessed.** pids are recycled, so the
  number alone does not identify a process — the start time does.
  ``process_identity.probe`` supplies it.

What is pinned here
-------------------
* the spawn writes an ``executor`` record whose pgid is the child's own
  pid (the child leads its own session);
* a ledger write that raises does not abort the spawn;
* the exit event is written after ``process.wait()`` and before the
  run's status is persisted to the database;
* ``start_new_session=True`` survives — the child is its own process
  group leader, so a kill aimed at the group cannot reach the backend;
* two spawns of one plan each get their own record;
* the ``role`` the caller asks for is the role the record carries — the
  reaper decides what to do with a process by its role, so the caller
  has to be able to say which kind of process it started.

And the paths that make the spawn mean anything
------------------------------------------------
A record says *which* process was started; it does not say whether that
process could find the plan it was sent to run. That is the second half
of the contract, and it is a path contract:

* the executor runs with **its own** working directory (``backend/``),
  so a relative path handed to it is resolved against *that* — not
  against the backend's, and not against the plan's;
* the plans root and the state database are configured through the
  environment, and a relative value there used to be taken literally,
  so the same configuration named a different directory depending on
  who was asking;
* a plan whose ``tasks.json`` is missing must be **refused**, not read
  as "a plan with no tasks": the executor's recovery path skips the
  load, so a silent empty list made the executor exit successfully
  having done nothing — and the backend, seeing an executor that had
  ended, had no tree to kill.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` has to be imported before ``routes.execution``: the server
# module wires the router at import time and ``routes.execution``
# late-binds back into it, so the other order is a circular import.
import server  # noqa: E402
from routes import execution as execution_route  # noqa: E402

import process_ledger as ledger  # noqa: E402


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "process groups and sessions are POSIX-only; the ledger records a "
        "pgid and the spawn puts the child in a session of its own"
    ),
)

PLAN_ID = "20261030-executor-ledger"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_state_db(tmp_path: Path) -> Path:
    """Allocate a hermetic ``state.db`` and return its path."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()
    return db_path


def _seed_plan(db_path: Path, plan_id: str, project_dir: Path) -> None:
    """Insert the ``plan_routing`` / ``plan_execution`` rows a start needs.

    The routing row is parked at ``queued`` — the waiting phase, and the
    one the start route accepts (``server._EXECUTION_START_SOURCE_PHASES``).
    ``ready`` is a retired input alias: a row parked there is refused
    with ``409 stage_mismatch`` before the executor is ever spawned, so
    the case would wait for an exit event that never comes.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    try:
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, substage, current_phase, version, completed_phases, "
            " review_rounds, flags, verification, last_updated, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, None, "queued", 0, "[]",
                "{}", "{}",
                json.dumps(
                    {
                        "status": "pending",
                        "round": 0,
                        "max_rounds": 3,
                        "stop_reason": None,
                    }
                ),
                "2026-10-30T00:00:00",
                "2026-10-30T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution (plan_id, attempt_count, "
            "project_dir, stop_reason, task_progress, next_run_at, "
            "card_state, flags, exec_pid, exec_status, started_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, 0, str(project_dir), None, json.dumps({"tasks": {}}),
                None, "{}", "{}", None, "not_started", None,
                "2026-10-30T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _fake_backend(tmp_path: Path) -> Path:
    """A ``backend/`` directory whose ``cli.py`` is a harmless sleeper.

    The real spawn helper builds ``<backend_dir>/cli.py`` onto the
    command line, so pointing it at this directory gives the test a
    real child process without launching the real executor.
    """
    backend_dir = tmp_path / "fake_backend"
    backend_dir.mkdir()
    (backend_dir / "cli.py").write_text(
        "import sys\n"
        "import time\n"
        "sys.stdout.write('fake executor up\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    return backend_dir


def _terminate(proc) -> None:
    """Stop a spawned child without ever touching our own process group."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    if pgid is not None and pgid != os.getpgid(0):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001 - the child is disposable
        pass


def _spawn(
    plan_id: str,
    backend_dir: Path,
    project_dir: Path,
    tasks_file: Path,
    log_path: Path,
    **kwargs: Any,
):
    """Call the real spawn helper."""
    return execution_route._spawn_executor_subprocess(
        plan_id=plan_id,
        backend_dir=backend_dir,
        project_dir=project_dir,
        tasks_file=tasks_file,
        tool=None,
        log_path=log_path,
        **kwargs,
    )


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    """A plan directory wired into the server's plans root."""
    plans_dir = tmp_path / "plans"
    pdir = plans_dir / PLAN_ID
    pdir.mkdir(parents=True)
    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    return PLAN_ID, pdir


@pytest.fixture
def project_dir(tmp_path):
    """The working directory the executor is pointed at."""
    pdir = tmp_path / "project"
    pdir.mkdir()
    return pdir


def _tasks_file(pdir: Path) -> Path:
    tasks_file = pdir / "tasks.json"
    tasks_file.write_text(
        json.dumps({"tasks": [{"id": "1", "title": "t", "depends_on": []}]}),
        encoding="utf-8",
    )
    return tasks_file


class _FakeProcess:
    """Minimal stand-in for the executor ``subprocess.Popen`` handle."""

    def __init__(self, pid: int = 424242) -> None:
        self.pid = pid
        self.returncode = 0
        self.waited = False

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        self.waited = True
        return self.returncode


def _await(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.02)
    assert predicate(), "the expected call never happened within the timeout"


# ---------------------------------------------------------------------------
# 1. The spawn writes the record.
# ---------------------------------------------------------------------------
def test_spawn_writes_executor_record_with_pgid(
    plan_dir, project_dir, tmp_path, caplog
):
    """The spawned executor is on record, in its own process group."""
    pid_, pdir = plan_dir
    backend_dir = _fake_backend(tmp_path)
    tasks_file = _tasks_file(pdir)

    with caplog.at_level(logging.INFO, logger="server"):
        proc, _log_path = _spawn(
            pid_, backend_dir, project_dir, tasks_file, tmp_path / "executor.log"
        )
    try:
        live = ledger.live_records(pdir)
        assert proc.pid in live, (
            "the spawned executor is not on the ledger — a restart would "
            "have no idea which pid this plan owns"
        )
        record = live[proc.pid]
        assert record.role == "executor"
        # ``start_new_session=True`` makes the child its own process
        # group leader, so the group it must be killed through is the
        # group it leads.
        assert record.pgid == proc.pid
        assert record.plan_id == pid_
        assert record.owner_pid == os.getpid()
        assert record.owner_role == "backend"
        assert record.create_time > 0
        assert record.cmdline

        # The ledger is a file on disk nobody watches — the log line
        # is what an operator actually sees.
        spawned = [
            r.getMessage()
            for r in caplog.records
            if "executor_spawned" in r.getMessage()
        ]
        assert spawned, "the spawn was not logged"
        assert f"pid={proc.pid}" in spawned[-1]
        assert f"pgid={proc.pid}" in spawned[-1]
    finally:
        _terminate(proc)


# ---------------------------------------------------------------------------
# 2. A ledger failure never blocks the spawn.
# ---------------------------------------------------------------------------
def test_spawn_failure_does_not_abort_execution(
    plan_dir, project_dir, tmp_path, monkeypatch, caplog
):
    """The ledger is evidence, not a gate — a failed write is survivable."""
    pid_, pdir = plan_dir
    backend_dir = _fake_backend(tmp_path)
    tasks_file = _tasks_file(pdir)

    def _boom(*args, **kwargs):
        raise OSError("the plan directory is read-only")

    monkeypatch.setattr(execution_route, "ledger_record_spawn", _boom)

    with caplog.at_level(logging.WARNING, logger="server"):
        proc, _log_path = _spawn(
            pid_,
            backend_dir,
            project_dir,
            tasks_file,
            tmp_path / "executor.log",
        )
        try:
            # The spawn happened, and the process is running: a ledger
            # that could not be written did not cost the run that was
            # requested.
            assert proc.pid > 0
            assert proc.poll() is None
            assert ledger.live_records(pdir) == {}
        finally:
            _terminate(proc)

    warnings = [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert warnings, "a failed ledger write must leave a warning behind"


# ---------------------------------------------------------------------------
# 3. The exit is written after the process is reaped.
# ---------------------------------------------------------------------------
@pytest.fixture
def running_plan(tmp_path, monkeypatch):
    """A plan whose executor is a fake process, wired into the server."""
    plans_dir = tmp_path / "plans"
    pdir = plans_dir / PLAN_ID
    pdir.mkdir(parents=True)
    _tasks_file(pdir)
    pdir_project = tmp_path / "project"
    pdir_project.mkdir()

    db_path = _make_state_db(tmp_path)
    _seed_plan(db_path, PLAN_ID, pdir_project)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    monkeypatch.setattr(
        server, "_reject_if_placeholder_prd", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "state_machine.db.archive_scan.classify_plan",
        lambda *a, **k: "active",
    )
    monkeypatch.setattr(
        server,
        "_count_unfinished_tasks",
        lambda *a, **k: {"total": 1, "pending_or_unstarted": 0, "failed": 0},
    )

    fake = _FakeProcess()

    def _fake_spawn(**kwargs):
        # The real spawn writes the ledger; this stand-in writes it too,
        # so the exit event has a live record to close.
        ledger.record_spawn(
            pdir,
            ledger.ProcessRecord(
                plan_id=PLAN_ID,
                role="executor",
                pid=fake.pid,
                pgid=fake.pid,
                create_time=time.time(),
                cmdline=("fake-executor",),
                cwd=str(pdir_project),
                owner_pid=os.getpid(),
                owner_role="backend",
            ),
        )
        return fake, tmp_path / "executor.log"

    monkeypatch.setattr(server, "_spawn_executor_subprocess", _fake_spawn)
    monkeypatch.setattr(
        server, "_run_auto_verification_loop", lambda *a, **k: None
    )

    server._execution_state.pop(PLAN_ID, None)
    yield server, PLAN_ID, pdir, pdir_project, fake
    server._execution_state.pop(PLAN_ID, None)


def test_run_writes_exit_event_after_process_wait(running_plan, monkeypatch):
    """``_run`` records the exit — after the wait, before the persist."""
    srv, plan_id, pdir, work_dir, fake = running_plan

    exit_calls: List[Dict[str, Any]] = []
    real_exit = execution_route.ledger_record_exit

    def _spy_exit(plan_dir_arg, pid, event="exit", **kwargs):
        # The real write happens *first* and the call is recorded after
        # it: the record must not appear before the ledger has it, or
        # the assertions below would race the write.
        result = real_exit(plan_dir_arg, pid, event, **kwargs)
        exit_calls.append(
            {"pid": pid, "event": event, "waited": fake.waited, **kwargs}
        )
        return result

    monkeypatch.setattr(execution_route, "ledger_record_exit", _spy_exit)

    # What the database looked like at the moment each status was
    # persisted — the exit event has to be on disk by then.
    persisted_after: List[List[str]] = []
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    real_update_status = ExecutionRepository.update_status

    def _spy_update(self, plan_id_arg, status):
        persisted_after.append(
            [e.get("event") for e in ledger.read_events(pdir)]
        )
        return real_update_status(self, plan_id_arg, status)

    monkeypatch.setattr(ExecutionRepository, "update_status", _spy_update)

    srv.start_execution(
        plan_id, srv.StartExecutionRequest(project_dir=str(work_dir))
    )

    _await(lambda: bool(exit_calls) and bool(persisted_after))

    assert len(exit_calls) == 1
    assert exit_calls[0]["pid"] == fake.pid
    assert exit_calls[0]["event"] == "exit"
    assert exit_calls[0]["exit_code"] == 0
    # ``waited`` is flipped inside ``wait()`` — the exit event was
    # written after the process was reaped, never before.
    assert exit_calls[0]["waited"] is True

    exits = [e for e in ledger.read_events(pdir) if e["event"] == "exit"]
    assert len(exits) == 1
    assert exits[0]["pid"] == fake.pid
    assert exits[0]["exit_code"] == 0

    # The live set no longer holds the pid that is gone.
    assert ledger.live_records(pdir) == {}

    # And the exit was on disk before the run's status was persisted.
    assert persisted_after, "the run never persisted its status"
    for events_at_persist in persisted_after:
        assert "exit" in events_at_persist, (
            "the status was persisted before the exit event reached the "
            "ledger — a crash in between would leave a pid that looks live"
        )


# ---------------------------------------------------------------------------
# 4. ``start_new_session=True`` survives.
# ---------------------------------------------------------------------------
def test_spawn_keeps_start_new_session(plan_dir, project_dir, tmp_path):
    """The child leads its own session — a group kill cannot reach us."""
    pid_, pdir = plan_dir
    backend_dir = _fake_backend(tmp_path)
    tasks_file = _tasks_file(pdir)

    proc, _log_path = _spawn(
        pid_, backend_dir, project_dir, tasks_file, tmp_path / "executor.log"
    )
    try:
        assert os.getpgid(proc.pid) == proc.pid
        assert os.getsid(proc.pid) == proc.pid
        # The backend is in a different group — that is the whole point
        # of the separate session.
        assert os.getpgid(proc.pid) != os.getpgid(0)
    finally:
        _terminate(proc)


# ---------------------------------------------------------------------------
# 5. Two spawns of one plan each get a record.
# ---------------------------------------------------------------------------
def test_repeat_spawn_of_one_plan_writes_one_record_each(
    plan_dir, project_dir, tmp_path
):
    """Each spawn is its own record — the ledger is append-only."""
    pid_, pdir = plan_dir
    backend_dir = _fake_backend(tmp_path)
    tasks_file = _tasks_file(pdir)

    first, _ = _spawn(
        pid_, backend_dir, project_dir, tasks_file, tmp_path / "first.log"
    )
    second, _ = _spawn(
        pid_, backend_dir, project_dir, tasks_file, tmp_path / "second.log"
    )
    try:
        spawns = [
            e for e in ledger.read_events(pdir) if e["event"] == "spawn"
        ]
        assert len(spawns) == 2
        assert {s["pid"] for s in spawns} == {first.pid, second.pid}
        live = ledger.live_records(pdir)
        assert set(live) == {first.pid, second.pid}
    finally:
        _terminate(first)
        _terminate(second)


# ---------------------------------------------------------------------------
# 6. The role is the caller's to choose.
# ---------------------------------------------------------------------------
def test_spawn_records_the_requested_role(plan_dir, project_dir, tmp_path):
    """The ``role`` the caller asks for is the role on the record.

    The reaper decides what to do with a process by its role, so a
    caller that starts a different kind of process has to be able to
    say so. This task does no whitelist validation — whatever role the
    caller passes is the role the record carries, and the default stays
    ``"executor"`` for the caller that says nothing.
    """
    pid_, pdir = plan_dir
    backend_dir = _fake_backend(tmp_path)
    tasks_file = _tasks_file(pdir)

    proc, _log_path = _spawn(
        pid_,
        backend_dir,
        project_dir,
        tasks_file,
        tmp_path / "executor.log",
        role="repair_executor",
    )
    try:
        live = ledger.live_records(pdir)
        assert proc.pid in live, "the spawned process is not on the ledger"
        assert live[proc.pid].role == "repair_executor", (
            "the record ignored the role the caller asked for — the reaper "
            "would treat this process as the wrong kind"
        )
    finally:
        _terminate(proc)


# ---------------------------------------------------------------------------
# 7. The roots the environment configures are anchored.
# ---------------------------------------------------------------------------
def test_relative_env_roots_resolve_against_the_project_root(monkeypatch):
    """A relative plans root / state db is anchored on the repository.

    ``PDT_PLANS_DIR`` and ``PDT_STATE_DB_PATH`` are the two seams every
    harness uses to point the backend somewhere else. A relative value
    used to be taken literally, so the *same* configuration named a
    different directory for every process that read it — the backend
    (whose working directory is the repository root) and the executor
    (whose working directory is ``backend/``) disagreed about where the
    plans live, and the executor silently found nothing.
    """
    from config_paths import (
        PROJECT_ROOT,
        resolve_plans_dir,
        resolve_state_db_path,
    )

    monkeypatch.setenv("PDT_PLANS_DIR", "relative-plans")
    monkeypatch.setenv("PDT_STATE_DB_PATH", "relative-state.db")

    plans = resolve_plans_dir()
    db = resolve_state_db_path()

    assert plans.is_absolute(), (
        "a relative PDT_PLANS_DIR is not a plans root — it is a path "
        "relative to whoever reads it"
    )
    assert plans == (PROJECT_ROOT / "relative-plans").resolve()
    assert db.is_absolute()
    assert db == (PROJECT_ROOT / "relative-state.db").resolve()


def test_absolute_env_roots_are_left_alone(monkeypatch, tmp_path):
    """An absolute override is still the override — anchoring is a fallback."""
    from config_paths import resolve_plans_dir, resolve_state_db_path

    monkeypatch.setenv("PDT_PLANS_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))

    assert resolve_plans_dir() == tmp_path / "plans"
    assert resolve_state_db_path() == tmp_path / "state.db"


def test_the_server_reads_its_plans_root_through_the_resolver(tmp_path):
    """``server.PLANS_DIR`` is the resolver's answer, not a literal.

    The server used to read ``PDT_PLANS_DIR`` on its own and keep a
    relative value as it was. The backend then handed that root to
    children whose working directory is ``backend/`` — so the root named
    one directory for the backend and another for everything it started.
    """
    import shutil

    from config_paths import PROJECT_ROOT

    # A relative value that resolves inside the repository's gitignored
    # runtime-state directory, so the import-time ``mkdir`` the server
    # performs cannot pollute the working tree.
    relative_root = ".pdt/vp-relative-plans"
    script = (
        "import os, sys\n"
        "os.environ['PDT_PLANS_DIR'] = {root!r}\n"
        "sys.path.insert(0, {backend!r})\n"
        "sys.path.insert(0, {repo!r})\n"
        "import server\n"
        "print(server.PLANS_DIR)\n"
    ).format(
        root=relative_root,
        backend=str(_BACKEND_DIR),
        repo=str(_BACKEND_DIR.parent),
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            # A working directory that is *not* the repository: a
            # cwd-anchored resolution would land here instead.
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        printed = Path(result.stdout.strip().splitlines()[-1])
        assert printed.is_absolute(), (
            "the server kept a relative plans root — it names a different "
            "directory for every process that reads it"
        )
        assert printed == (PROJECT_ROOT / relative_root).resolve()
    finally:
        shutil.rmtree(PROJECT_ROOT / relative_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8. The child is handed absolute paths.
# ---------------------------------------------------------------------------
def _fake_backend_reporting(tmp_path: Path, report_path: Path) -> Path:
    """A ``backend/`` whose ``cli.py`` reports what it was handed."""
    backend_dir = tmp_path / "fake_backend_reporting"
    backend_dir.mkdir()
    (backend_dir / "cli.py").write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "\n"
        "args = sys.argv[1:]\n"
        "tasks_file = None\n"
        "for index, arg in enumerate(args):\n"
        "    if arg == '--tasks-file' and index + 1 < len(args):\n"
        "        tasks_file = args[index + 1]\n"
        "resolved = Path(tasks_file).resolve() if tasks_file else None\n"
        "Path('__REPORT__').write_text(json.dumps({\n"
        "    'tasks_file': tasks_file,\n"
        "    'cwd': os.getcwd(),\n"
        "    'resolved': str(resolved) if resolved else None,\n"
        "    'found': bool(resolved and resolved.exists()),\n"
        "}), encoding='utf-8')\n"
        "time.sleep(30)\n".replace("__REPORT__", str(report_path)),
        encoding="utf-8",
    )
    return backend_dir


def test_the_executor_finds_the_tasks_file_behind_a_relative_path(
    plan_dir, project_dir, tmp_path, monkeypatch
):
    """The child resolves the plan's tasks file, whatever its cwd is.

    The executor's working directory is ``backend/`` — the plan's tree
    is not under it. A relative ``--tasks-file`` therefore names a
    *different* file for the child than for the backend, and the child
    found nothing: an executor that starts, finds no tasks, and exits
    successfully, which the backend reads as "the run finished".
    """
    pid_, pdir = plan_dir
    report_path = tmp_path / "child_report.json"
    backend_dir = _fake_backend_reporting(tmp_path, report_path)
    tasks_file = _tasks_file(pdir)

    # The backend's own view of the plan is relative; the child's is not.
    monkeypatch.chdir(tmp_path)
    relative_tasks = os.path.relpath(tasks_file, tmp_path)

    proc, _log_path = _spawn(
        pid_,
        backend_dir,
        project_dir,
        Path(relative_tasks),
        tmp_path / "executor.log",
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not report_path.exists():
            time.sleep(0.02)
        assert report_path.exists(), (
            "the executor never reported what it was handed"
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))

        assert report["found"], (
            "the executor could not find the plan's tasks file — it was "
            "handed %r and looked in %s" % (
                report["tasks_file"], report["resolved"],
            )
        )
        assert report["resolved"] == str(tasks_file.resolve())
    finally:
        _terminate(proc)


def test_the_spawn_command_carries_absolute_paths(
    plan_dir, project_dir, tmp_path, monkeypatch
):
    """``--tasks-file`` and ``-w`` are absolute in the spawned command."""
    pid_, pdir = plan_dir
    backend_dir = _fake_backend(tmp_path)
    tasks_file = _tasks_file(pdir)

    monkeypatch.chdir(tmp_path)
    relative_tasks = os.path.relpath(tasks_file, tmp_path)

    proc, _log_path = _spawn(
        pid_,
        backend_dir,
        project_dir,
        Path(relative_tasks),
        tmp_path / "executor.log",
    )
    try:
        record = ledger.live_records(pdir)[proc.pid]
        cmdline = list(record.cmdline)
        tasks_arg = cmdline[cmdline.index("--tasks-file") + 1]
        workdir_arg = cmdline[cmdline.index("-w") + 1]
        assert os.path.isabs(tasks_arg), (
            "the executor was handed a relative tasks file (%s) — it "
            "resolves it against its own working directory" % tasks_arg
        )
        assert os.path.isabs(workdir_arg), (
            "the executor was handed a relative working directory (%s)"
            % workdir_arg
        )
        assert Path(tasks_arg) == tasks_file.resolve()
    finally:
        _terminate(proc)


# ---------------------------------------------------------------------------
# 9. A missing tasks file is refused, not read as an empty plan.
# ---------------------------------------------------------------------------
def test_a_missing_tasks_file_is_refused_not_read_as_an_empty_plan(tmp_path):
    """Reading the task list of a plan whose file is missing is an error.

    The refusal waits for the read on purpose: a manager is built both
    to read a plan and to write one, and only the *caller* knows which
    one it is doing. A caller that takes over the task list clears the
    refusal; a caller that reads a plan that is not there gets the
    error.
    """
    import task_manager
    from task_manager import TaskManager

    assert hasattr(task_manager, "TasksFileMissingError"), (
        "the refusal has no name of its own — a caller cannot tell it "
        "apart from any other failure"
    )

    missing = tmp_path / "plans" / "20261031-a-plan" / "tasks.json"
    manager = TaskManager(project_dir=missing.parent, tasks_file=missing)
    with pytest.raises(task_manager.TasksFileMissingError) as caught:
        manager.tasks
    assert str(missing) in str(caught.value)


def test_a_manager_for_a_plan_that_is_about_to_be_written_is_not_refused(
    tmp_path,
):
    """The plan-writing path keeps working: it hands the manager its rows.

    The fast-lane import builds a manager for a plan whose
    ``tasks.json`` does not exist yet and writes it. Refusing that
    would break the import; the refusal is cleared by the assignment.
    """
    from task import SubTask
    from task_manager import TaskManager

    plan_dir = tmp_path / "plans" / "20261031-a-plan"
    plan_dir.mkdir(parents=True)
    tasks_file = plan_dir / "tasks.json"

    manager = TaskManager(project_dir=plan_dir, tasks_file=tasks_file)
    manager.tasks = [
        SubTask(id="1", title="t", description="d", depends_on=[]),
    ]
    manager.save_tasks()

    assert tasks_file.exists()
    assert [t.id for t in manager.tasks] == ["1"]


def test_a_missing_tasks_file_ends_the_process_with_a_non_zero_code(tmp_path):
    """The refusal reaches the process as a non-zero exit code.

    This is what the backend sees: an executor that ended. It has to be
    able to tell "the run finished" from "the run could not start". The
    line below is the one the executor's recovery path runs — it counts
    the tasks it is about to execute.
    """
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, {backend!r})\n"
        "from task_manager import TaskManager\n"
        "manager = TaskManager(project_dir=Path({project!r}), "
        "tasks_file=Path({tasks!r}))\n"
        "print(len(manager.tasks))\n"
    ).format(
        backend=str(_BACKEND_DIR),
        project=str(tmp_path / "plan"),
        tasks=str(tmp_path / "plan" / "tasks.json"),
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0, (
        "a missing tasks file must end the process with a non-zero code — "
        "the executor's recovery path skips the load, so an empty task "
        "list made it exit successfully having done nothing"
    )
    assert "tasks file not found" in (result.stdout + result.stderr), (
        "the process failed for some other reason — the refusal is what "
        "has to end it"
    )


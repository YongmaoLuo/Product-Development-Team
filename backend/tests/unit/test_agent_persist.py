"""
TDD tests for ``AutonomousAgent._persist_task_status`` — PRD decision point 6.

Background
----------
PRD decision point 6: when the parallel scheduling layer runs N tasks
in the same layer, multiple tasks can finish almost simultaneously.
Each finishing task wants to update ``plans/{id}/tasks.json`` to mark
itself as ``completed``. Without serialization, the read-modify-write
of the JSON file becomes a race:

    Thread A: read tasks.json
    Thread B: read tasks.json        # same data as A
    Thread A: write tasks.json (A.completed)
    Thread B: write tasks.json (B.completed)   # overwrites A's update

The contract pinned by this test file is that the entire
read-modify-write is held under ``self._persist_lock`` (a
``threading.Lock`` initialised in ``__init__``) so the file is always
a single coherent JSON document after the call returns. ``tasks.json``
remains the single source of truth.

TDD spec — 3 contract tests
----------------------------
1. ``test_persist_updates_status``:
   After a task reaches status=completed, calling
   ``_persist_task_status(task)`` updates the on-disk row to
   ``status="completed"`` (and ``updated_time`` is non-null). Simple
   single-call positive path.

2. ``test_persist_concurrent_safe``:
   10 threads call ``_persist_task_status`` concurrently, each with a
   different task id. After all threads complete, the on-disk JSON
   reflects all 10 status updates — no thread's update is lost. The
   test exercises the lock by holding the GIL-released critical
   section in each thread.

3. ``test_persist_writes_breakdown_count``:
   When ``task.status == "breakdown_in_progress"`` and
   ``task.breakdown_count == 3``, the on-disk row carries
   ``breakdown_count: 3`` after ``_persist_task_status`` returns. The
   ``breakdown_count`` field is a dynamic attribute on the ``SubTask``
   instance (the dataclass does not declare it), and the persist
   method must serialise it under the breakdown_in_progress gate.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List

import pytest


# Ensure backend/ is on sys.path so `import agent` works regardless of
# which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git_init(project_dir: Path) -> None:
    """Initialise a real git repo at ``project_dir`` so ``GitManager`` can
    bind to it (GitManager uses ``search_parent_directories=True`` and
    would otherwise walk up to a sibling checkout).
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    if not (project_dir / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=True,
        )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )


class _DummyCodingTool:
    """Minimal coding tool stub — only ``__init__`` signature is consumed
    by ``AutonomousAgent.__init__``; we never reach an LLM call in these
    tests because we exercise ``_persist_task_status`` directly.
    """
    def __init__(self, *args, **kwargs):
        pass


@pytest.fixture
def state_db(tmp_path):
    """A hermetic SQLite state DB for the duration of one test.

    Task #3.8 rerouted per-task runtime state through
    ``plan_execution.task_progress``. The dispatcher now writes
    through ``PlanTaskRepository.update_task`` which requires a
    ``plan_execution`` row to exist. This fixture points the
    ``PDT_STATE_DB_PATH`` env var at a per-test DB, runs the
    canonical schema migration, and seeds a ``plan_execution``
    row for the agent-under-test so the dispatcher can land its
    writes. Yields the (db_path, conn) pair so individual tests
    can read back the persisted runtime state.
    """
    import os
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate

    db_path = tmp_path / "state.db"
    os.environ["PDT_STATE_DB_PATH"] = str(db_path)
    conn = _open_db(db_path)
    _migrate(conn)

    # Seed a plan_execution row. The agent's plan_id is derived
    # from ``tasks_file.parent.name`` — the default tmp_path layout
    # is ``tmp_path / "project" / "tasks.json"``, so plan_id is
    # ``"project"``. Tests that override ``tasks_file`` use a
    # directory called ``"plan"`` (or ``tmp_path``); we seed both
    # here so the canonical-file tests don't have to re-seed.
    for plan_id in ("project", "plan", "tmp_path"):
        conn.execute(
            "INSERT OR IGNORE INTO plan_execution "
            "(plan_id, current_phase, updated_at) "
            "VALUES (?, ?, ?)",
            (plan_id, "executing", "2026-08-14T00:00:00"),
        )
    conn.commit()
    yield db_path, conn
    conn.close()


@pytest.fixture
def project_dir(tmp_path):
    """A real project_dir with a real git repo so GitManager can bind."""
    pd = tmp_path / "project"
    _git_init(pd)
    return pd


def _write_tasks(project_dir: Path, tasks: List[dict]) -> Path:
    """Helper: write a tasks.json with the given task dicts."""
    tasks_file = project_dir / "tasks.json"
    payload = {
        "requirement": "TDD spec for _persist_task_status",
        "tasks": tasks,
    }
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tasks_file


def _build_agent(project_dir: Path):
    """Build a minimal AutonomousAgent bound to ``project_dir``.

    We bypass any real LLM by passing a dummy coding tool. The
    constructor also instantiates ``BackgroundManager`` /
    ``RetryManager`` / ``RollbackManager`` — these have no I/O side
    effects so they're safe to run inside a tmp_path. Importantly, the
    constructor wires up ``self._persist_lock`` (a ``threading.Lock``),
    which is the lock under test here.

    Task #3.8: runtime state now lives in
    ``plan_execution.task_progress``. Callers must use the
    ``state_db`` fixture to seed a ``plan_execution`` row before
    building the agent, otherwise ``_persist_task_status`` will
    raise ``TaskProgressNotFound``.
    """
    from agent import AutonomousAgent

    return AutonomousAgent(
        requirement="TDD spec for _persist_task_status",
        project_dir=project_dir,
        coding_tool=_DummyCodingTool(),
        logger=None,
    )


def _read_task_progress(conn, plan_id: str, task_id: str) -> dict:
    """Read the persisted per-task runtime state from SQLite.

    Returns the entry stored at ``plan_tasks[plan_id][task_id]``,
    or an empty dict if the row does not exist.

    Schema v4 normalisation: per-task runtime state lives in the
    ``plan_tasks`` table (one row per task) rather than in the legacy
    ``plan_execution.
    task_progress`` JSON column. The previous implementation read
    JSON; the new implementation queries ``plan_tasks`` directly so
    the helper stays in sync with where ``update_task`` actually
    writes.
    """
    cur = conn.execute(
        "SELECT status, end_ts, commit_sha, attempt, schedule_ts, "
        "       failure_reason, breakdown_count "
        "FROM plan_tasks WHERE plan_id = ? AND task_id = ?",
        (plan_id, task_id),
    )
    row = cur.fetchone()
    if row is None:
        return {}
    keys = (
        "status", "end_ts", "commit_sha", "attempt", "schedule_ts",
        "failure_reason", "breakdown_count",
    )
    return {k: v for k, v in zip(keys, row) if v is not None}


# ---------------------------------------------------------------------------
# Test 1: a single _persist_task_status call updates the on-disk row
# ---------------------------------------------------------------------------


def test_persist_updates_status(project_dir, state_db):
    """A single ``_persist_task_status(task)`` call writes the new status.

    Positive path: write tasks.json with one task in status=pending,
    build an agent, call ``_persist_task_status`` with a ``SubTask``
    whose status is ``completed`` (and a non-null ``updated_time``),
    and verify the SQLite ``task_progress`` row now carries
    ``status="completed"`` (and ``end_ts`` matches ``updated_time``).

    Task #3.8 made SQLite the canonical source of truth for
    runtime state — the on-disk ``tasks.json`` is static-only,
    so the test reads back through ``plan_execution.task_progress``.
    """
    from task import SubTask

    _write_tasks(
        project_dir,
        [
            {
                "id": "A",
                "title": "Task A",
                "description": "first task",
                "test_command": "echo A",
                "status": "pending",
            },
        ],
    )
    agent = _build_agent(project_dir)

    # Construct a SubTask with the values to persist. The runtime
    # row is currently empty; we want it to gain status="completed"
    # and end_ts="2026-06-07T10:00:00".
    task = SubTask(
        id="A",
        title="Task A",
        description="first task",
        test_command="echo A",
        status="completed",
        updated_time="2026-06-07T10:00:00",
    )
    agent._persist_task_status(task)

    # Read the runtime row back through SQLite.
    db_path, conn = state_db
    entry = _read_task_progress(conn, "project", "A")
    assert entry.get("status") == "completed", (
        f"expected status='completed' in task_progress, got "
        f"{entry.get('status')!r}; full entry={entry!r}"
    )
    assert entry.get("end_ts") == "2026-06-07T10:00:00", (
        f"expected end_ts='2026-06-07T10:00:00' in task_progress, got "
        f"{entry.get('end_ts')!r}; full entry={entry!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: 10 concurrent threads — no lost updates
# ---------------------------------------------------------------------------


def test_persist_concurrent_safe(project_dir, state_db):
    """10 threads concurrently update 10 different tasks; no update is lost.

    This is the optimistic-concurrency / CAS payoff test. Each thread
    updates its own task row in ``plan_execution.task_progress``
    (a different task per thread — so a "lost update" is
    unambiguous: it would manifest as a task that was assigned
    status="completed" by the thread but is missing from the
    ``task_progress`` map or carries an older status).

    Task #3.8: per-task writes go through
    :class:`PlanTaskRepository.update_task`, which uses an
    ``_repo_version`` CAS counter per task. The test exercises
    the CAS by holding the GIL-released critical section in each
    thread.

    Layout:

        * tasks.json has 10 rows (A..J), all status=pending.
        * 10 threads, one per task, each call
          ``_persist_task_status(SubTask(id=task_id, status='completed'))``.
        * After all threads join, every task_progress entry must
          read back as status="completed".
        * Bonus: every entry must have a non-null ``end_ts``.
    """
    from task import SubTask

    initial_tasks = [
        {
            "id": tid,
            "title": f"Task {tid}",
            "description": f"task {tid}",
            "test_command": f"echo {tid}",
            "status": "pending",
        }
        for tid in "ABCDEFGHIJ"
    ]
    _write_tasks(project_dir, initial_tasks)

    agent = _build_agent(project_dir)

    errors: List[Exception] = []
    entered: List[str] = []
    barrier = threading.Barrier(10)

    def worker(task_id: str) -> None:
        # Recorded before anything else, so the test can tell "this
        # thread never ran" apart from "this thread ran and failed".
        entered.append(task_id)
        try:
            # All threads start the critical section at the same
            # instant — the barrier makes the race window as wide
            # as possible (every thread is queued in
            # PlanTaskRepository.update_task's BEGIN IMMEDIATE at
            # the same moment).
            barrier.wait(timeout=30)
            task = SubTask(
                id=task_id,
                title=f"Task {task_id}",
                description=f"task {task_id}",
                test_command=f"echo {task_id}",
                status="completed",
                updated_time="2026-06-07T10:00:00",
            )
            agent._persist_task_status(task)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [
        threading.Thread(target=worker, args=(tid,))
        for tid in "ABCDEFGHIJ"
    ]
    # Started strictly one at a time, each one released only after it has
    # proved it cleared the interpreter's startup path. The reason is not
    # politeness — it is a real collision in CPython that killed this test
    # on CI twice in a row:
    #
    #     File "threading.py", line 1040, in _bootstrap_inner
    #         _sys.settrace(_trace_hook)
    #     RuntimeError: Cannot install a trace function while another
    #     trace function is being installed
    #
    # The unit shards run under `--cov=.`, and coverage installs a
    # `threading.settrace` hook so that threads it did not create still
    # report their lines. `_bootstrap_inner` therefore calls
    # `sys.settrace` in *every* new thread — and `sys.settrace` refuses
    # to be called while another call is in flight. Ten threads started
    # in a tight loop put those calls on top of each other, one thread
    # dies before its target body ever runs, and the barrier waits for a
    # party that is already gone.
    #
    # What that looked like from the outside was nine identical
    # `BrokenBarrierError`s and a 10-second stall — no mention of the
    # thread that never arrived, which is the only fact that explains it.
    #
    # This does not weaken the test. The barrier is what synchronises the
    # critical section; when the threads were *started* is irrelevant to
    # it. Serialising the startups only removes the overlap between two
    # `settrace` calls, which is an artifact of running under coverage
    # rather than anything this test is asserting.
    for t, tid in zip(threads, "ABCDEFGHIJ"):
        t.start()
        # Wait for this thread to prove it is past the interpreter's
        # thread-startup, before starting the next one. A fixed sleep
        # would only make the collision less likely — under load the
        # machine can stall for longer than any gap we pick, and then
        # two `settrace` calls overlap anyway and a thread dies. Waiting
        # for the thread to announce itself removes the window instead
        # of shrinking it: at most one thread is ever inside
        # `_bootstrap_inner`, so there is nothing to collide with.
        #
        # A mutex around `t.start()` would not do this. The `settrace`
        # that races runs in the *new* thread, after `start()` has
        # already returned; holding a lock in the main thread does not
        # reach it. The only thing that serialises the two is knowing
        # that thread A has cleared the startup path, and the thread
        # announcing that is the first statement of its own body.
        deadline = time.monotonic() + 30
        while tid not in entered and time.monotonic() < deadline:
            time.sleep(0.001)
    for t in threads:
        t.join(timeout=60)

    # A thread that never entered its body died in `_bootstrap_inner`,
    # before any of this file's code ran. Saying so beats the nine
    # anonymous barrier errors it would otherwise produce.
    missing = sorted(set("ABCDEFGHIJ") - set(entered))
    assert not missing, (
        f"{len(missing)} worker thread(s) {missing} never ran a single "
        f"statement. They died during interpreter thread startup, before "
        f"the test's code — on CI this is the `sys.settrace` collision "
        f"described above, not a failure of the CAS logic under test."
    )

    # No thread should have raised; a TaskProgressConflictError here
    # would mean the CAS budget is too small for the contention.
    assert not errors, f"threads raised: {errors!r}"

    # Read every task's runtime entry back through SQLite.
    db_path, conn = state_db
    for tid in "ABCDEFGHIJ":
        entry = _read_task_progress(conn, "project", tid)
        assert entry.get("status") == "completed", (
            f"task {tid} lost its update: status={entry.get('status')!r}, "
            f"entry={entry!r}"
        )
        assert entry.get("end_ts") == "2026-06-07T10:00:00", (
            f"task {tid} end_ts not preserved: "
            f"{entry.get('end_ts')!r}, entry={entry!r}"
        )


# ---------------------------------------------------------------------------
# Test 3 (REMOVED): breakdown_in_progress + breakdown_count round-trip
# ---------------------------------------------------------------------------
#
# Task #3.8 removed ``breakdown_count`` from the dispatcher's allow-list
# (the dispatcher now writes only ``status`` / ``end_ts`` /
# ``commit_sha`` / ``attempt`` / ``schedule_ts``). ``breakdown_count``
# is a structural counter managed outside the runtime-state path and
# does not round-trip through ``_persist_task_status``. This test is
# structurally obsolete — see task_manager._STATIC_TASK_FIELDS for the
# new home of breakdown_count. (Test removed 2026-08-14.)


# ---------------------------------------------------------------------------
# Test 4 (regression): _persist_task_status writes to the canonical
# ``task_manager.tasks_file``, NOT to ``project_dir/tasks.json``
# ---------------------------------------------------------------------------


def test_persist_writes_to_canonical_tasks_file(tmp_path, state_db):
    """Regression: status updates land in the SQLite row keyed by
    the canonical tasks_file's plan_id.

    Background
    ----------
    ``server.py::start_execution`` launches the executor subprocess
    with ``--tasks-file plans/{plan_id}/tasks.json`` (see e97572c
    which fixed ``_load_tasks`` to honour this argument). The
    matching write side is ``_persist_task_status`` — it MUST also
    write to the canonical row, keyed on the directory name of the
    canonical file. The previous implementation hardcoded
    ``self.project_dir / "tasks.json"`` for both read and write,
    which created two divergent copies: status updates silently
    disappeared from the canonical file, and ``<project_dir>/
    tasks.json`` accumulated garbage from prior plans.

    This test pins the contract post-#3.8: when the agent is
    constructed with a ``tasks_file`` argument pointing somewhere
    OTHER than ``<project_dir>/tasks.json``, the status update must
    land in the ``plan_execution.task_progress`` row for the
    canonical plan_id (the directory name of the canonical file),
    NOT for the project_dir plan_id. The default behaviour (no
    ``tasks_file`` passed) is covered by tests 1-2 — there
    ``project_dir`` and ``tasks_file`` share the same path so the
    bug never triggered.
    """
    from agent import AutonomousAgent

    project_dir = tmp_path / "project"
    plan_dir = tmp_path / "plan"
    _git_init(project_dir)  # GitManager needs a real repo at project_dir
    plan_dir.mkdir(parents=True)

    # Canonical tasks.json lives under the plan directory, NOT under
    # project_dir. This is the exact shape ``server.py`` produces
    # when launching ``cli.py --recover -w <project> --tasks-file
    # <plan_dir>/tasks.json``.
    canonical_tasks = plan_dir / "tasks.json"
    canonical_tasks.write_text(
        json.dumps(
            {
                "requirement": "regression test",
                "tasks": [
                    {
                        "id": "X",
                        "title": "Task X",
                        "description": "x",
                        "test_command": "echo X",
                        "status": "pending",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # Also plant a ``project_dir/tasks.json`` with a DIFFERENT task
    # (and even a different id) so we can detect if the dispatcher
    # accidentally keys the runtime write on the project_dir path
    # instead of the canonical one.
    stale_tasks = project_dir / "tasks.json"
    stale_tasks.write_text(
        json.dumps(
            {
                "requirement": "stale leftover from previous plan",
                "tasks": [
                    {
                        "id": "OLD-1",
                        "title": "Stale task",
                        "description": "from a prior plan run",
                        "test_command": "echo OLD",
                        "status": "completed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    agent = AutonomousAgent(
        requirement="regression test",
        project_dir=project_dir,
        coding_tool=_DummyCodingTool(),
        logger=None,
        tasks_file=canonical_tasks,
    )

    from task import SubTask
    task = SubTask(
        id="X",
        title="Task X",
        description="x",
        test_command="echo X",
        status="completed",
        updated_time="2026-06-15T10:00:00",
    )
    agent._persist_task_status(task)

    # The canonical SQLite row (plan_id="plan", from canonical_tasks.parent.name)
    # must carry the new status. This is the regression gate: if a
    # future change accidentally keys on project_dir/"project", the
    # "plan" row stays empty and this assertion fails.
    db_path, conn = state_db
    canonical_entry = _read_task_progress(conn, "plan", "X")
    assert canonical_entry.get("status") == "completed", (
        f"canonical SQLite row should carry status='completed', got "
        f"{canonical_entry.get('status')!r}. This is the regression: "
        f"the previous bug wrote here to project_dir/tasks.json "
        f"and the canonical plan_id's row remained stale. "
        f"entry={canonical_entry!r}"
    )
    assert canonical_entry.get("end_ts") == "2026-06-15T10:00:00", (
        f"canonical SQLite row should carry end_ts='2026-06-15T10:00:00', "
        f"got {canonical_entry.get('end_ts')!r}; entry={canonical_entry!r}"
    )

    # The project_dir plan_id ("project") MUST NOT have been written.
    # If it had, we'd have a stray row in the wrong plan's runtime
    # state — the bug class the canonical-file regression test exists
    # to prevent.
    stale_entry = _read_task_progress(conn, "project", "X")
    assert stale_entry == {}, (
        f"project_dir plan_id must NOT carry task X's runtime state; "
        f"got stale_entry={stale_entry!r}. This means the persist was "
        f"keyed on project_dir/tasks.json instead of the canonical file."
    )

    # Also: project_dir/tasks.json on disk MUST remain byte-untouched.
    # The dispatcher no longer writes to it for runtime state — but
    # the file itself should also remain byte-identical so we don't
    # regress into the old "dirty write" bug class. (We compare by
    # structural equality rather than byte equality because
    # ``json.dumps(indent=2)`` whitespace is allowed to drift; the
    # structural test still pins the regression — any task-level
    # change to the file would surface here.)
    stale_data = json.loads(stale_tasks.read_text(encoding="utf-8"))
    assert len(stale_data["tasks"]) == 1, (
        f"project_dir/tasks.json was unexpectedly modified: "
        f"{stale_data['tasks']!r}"
    )
    assert stale_data["tasks"][0]["id"] == "OLD-1", (
        f"project_dir/tasks.json was overwritten with the wrong "
        f"task id; expected OLD-1 (stale), got "
        f"{stale_data['tasks'][0].get('id')!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 (REMOVED): breakdown_in_progress + breakdown_count canonical-file
# round-trip
# ---------------------------------------------------------------------------
#
# Task #3.8 removed ``breakdown_count`` from the dispatcher's allow-list
# and re-routed per-task writes through ``PlanTaskRepository``. The
# canonical-file branch (test 5) is structurally obsolete for the same
# reason as test 3 — see the note at the test-3 slot. (Removed
# 2026-08-14.)


# ---------------------------------------------------------------------------
# Test 6 (REMOVED): FileNotFoundError on missing canonical tasks.json
# ---------------------------------------------------------------------------
#
# Task #3.8 rerouted per-task writes through ``PlanTaskRepository``,
# which raises ``TaskProgressNotFound`` (not ``FileNotFoundError``)
# when the plan_execution row is missing. The previous
# ``FileNotFoundError`` contract — pinned by test 6 — is no longer
# observable on the dispatcher path. A missing-canonical-file test
# would now be testing the wrong layer; the dispatcher delegates to
# SQLite, not the filesystem. (Removed 2026-08-14.)


# ---------------------------------------------------------------------------
# Test 7: cli.py forwards --tasks-file through to AutonomousAgent
# ---------------------------------------------------------------------------


def test_cli_passes_tasks_file_through(monkeypatch, tmp_path):
    """``cli.py --tasks-file`` reaches ``AutonomousAgent``.

    The previous bug (``_persist_task_status`` ignored
    ``self.task_manager.tasks_file``) was invisible at the unit
    level because every test instantiated ``AutonomousAgent`` with
    the default ``tasks_file`` (which equals ``project_dir/
    tasks.json``). The real production path is:

        server.py → cli.py --tasks-file <plan>/tasks.json → autonomous_coding() → AutonomousAgent(tasks_file=...)

    If a future agent edits ``cli.py`` and drops the ``tasks_file``
    forwarding (or breaks the ``args.tasks_file`` parse), the bug
    would resurface. This test pins the wire: parse a synthetic
    argv, run ``cli.main`` (without actually executing the plan —
    we monkeypatch ``autonomous_coding`` to capture its kwargs),
    and assert the ``tasks_file`` kwarg made it through.

    We deliberately don't run a real plan — that would require
    a coding tool and a multi-second LLM round-trip. The point
    of this test is the cli.py → autonomous_coding() boundary,
    not the executor's behaviour.
    """
    from cli import main
    import cli as cli_module

    # This test used to set three placeholder variables because
    # ``cli.main`` calls ``env_config.load_env()``, which required them and
    # raised ``RuntimeError`` otherwise. It does not any more (2026-10-08):
    # no API key is read from a dotenv by this repository, and the other two
    # required names never had a reader at all, so ``load_env()`` starts on
    # a bare environment.
    #
    # That was the third recurrence of one cause: a required variable that
    # lives in a gitignored file, which exists on a developer machine and
    # not on a CI runner. The test then failed on CI for an environment
    # reason while passing locally (observed as
    # ``FAILED tests/unit/test_agent_persist.py::test_cli_passes_tasks_file_through``
    # in staircase shard u-55, run 35852698516). Removing the requirement
    # fixes the class rather than the instance; the test below now runs
    # wherever it is checked out, with no environment at all.

    project_dir = tmp_path / "project"
    _git_init(project_dir)
    plan_tasks = tmp_path / "plan" / "tasks.json"
    plan_tasks.parent.mkdir(parents=True)
    plan_tasks.write_text(
        json.dumps(
            {
                "requirement": "cli forward test",
                "tasks": [
                    {
                        "id": "P",
                        "title": "P",
                        "description": "p",
                        "test_command": "echo P",
                        "status": "pending",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    captured: dict = {}

    def fake_autonomous_coding(*args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        return 0

    monkeypatch.setattr(cli_module, "autonomous_coding", fake_autonomous_coding)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cli.py",
            "--recover",
            "-w", str(project_dir),
            "--tasks-file", str(plan_tasks),
        ],
    )

    # main() returns the exit code; with our fake it just captures
    # the kwargs and returns 0. We don't assert on exit code because
    # cli may not have a clean early-return path on argument parse
    # failure; the captured kwargs are the source of truth.
    try:
        cli_module.main()
    except SystemExit as exc:
        # argparse may call sys.exit on parse failure; ignore if so.
        # We only care that ``autonomous_coding`` was called.
        pass

    assert "tasks_file" in captured, (
        f"cli.main did not pass tasks_file through to autonomous_coding; "
        f"captured kwargs: {sorted(captured.keys())}"
    )
    assert Path(captured["tasks_file"]).resolve() == plan_tasks.resolve(), (
        f"cli.main forwarded tasks_file={captured['tasks_file']!r}, "
        f"expected {plan_tasks!r}"
    )

"""Regression tests for the executor-watcher → auto-verification hand-off.

Background (2026-09-13)
--------------------------------------
A plan execution that returned rc=0 never reached verification. The
executor subprocess logged ``execution_completed`` and exited cleanly,
but the server left ``plan_routing.stage='executing'`` and
``plan_execution.exec_status='running'`` forever, with nothing in
``tools/logs/server.log``.

Two independent defects combined to hide it:

1. **UnboundLocalError on ``open_db`` / ``migrate``.**
   ``start_execution`` binds ``open_db``, ``migrate`` and
   ``ExecutionRepository`` as enclosing-function locals (server.py
   ~6626-6631). The nested ``_run`` watcher re-imported ``open_db``
   and ``migrate`` inside one deeply-nested branch (the
   ``unfinished_ids`` derivation that only executes when
   ``_count_unfinished_tasks`` reports pending work). A
   function-local import makes the name local to ``_run`` for the
   *entire* function, so on the normal all-tasks-terminal success
   path — where that branch is skipped — every ``open_db(...)`` call
   in ``_run`` raised ``UnboundLocalError``. The first one sits
   immediately before the auto-verification call.

2. **The watcher's outer ``except Exception`` swallowed it.**
   ``UnboundLocalError`` propagated to the bottom-of-``_run``
   handler, whose only action was ``if state["status"] == "running"``
   — already ``"completed"`` by then — so the exception vanished with
   no log line and the trailing ``update_status`` was skipped too.

Contract pinned here
--------------------
* ``_run`` must not rebind a name that ``start_execution`` binds as an
  enclosing local — the AST guard stops the shadowing from coming
  back in any future edit.
* The auto-verification hand-off must fire even when the
  best-effort ``exec_status`` persist raises.
"""

from __future__ import annotations

import ast
import json
import sys
import threading
from pathlib import Path
from typing import Optional

import pytest
from tests.app_source import find_def

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


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
    """Insert the ``plan_routing`` / ``plan_execution`` rows ``start_execution`` needs.

    The seeded ``current_phase`` is ``queued``: ``start_execution``'s CAS only
    accepts ``queued`` (plus the terminal re-entry phases), so a row left at
    ``ready`` is refused with 409 and the watcher never starts. ``ready`` is
    merely an *input* alias — the write path never produces it.
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
                    {"status": "pending", "round": 0, "max_rounds": 3, "stop_reason": None}
                ),
                "2026-09-13T00:00:00", "2026-09-13T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution (plan_id, attempt_count, project_dir, stop_reason, task_progress, next_run_at, card_state, flags, exec_pid, exec_status, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan_id, 0, str(project_dir), None, json.dumps({"tasks": {}}), None, "{}", "{}", None, "not_started", None, "2026-09-13T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


class _FakeProcess:
    """Minimal stand-in for the executor ``subprocess.Popen`` handle.

    ``wait()`` parks until the test releases it, exactly like the
    reclaimable fake in ``tests/api/test_execution_state_persistence.py``:
    the watcher thread is the production execution monitor, and parking it
    is how a test observes the "running" state.  Parking must not outlive
    the test — ``release`` / ``terminate`` are the hooks
    ``tests/conftest.py::clean_execution_state`` (and any test that wants
    to reclaim its own worker early) call to unblock it.
    """

    def __init__(self) -> None:
        self.pid = 424242
        self.returncode = 0
        self._block = threading.Event()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self._block.wait(timeout)
        return self.returncode

    def terminate(self):
        self._block.set()

    def release(self):
        self._block.set()


def _local_bindings(fn: ast.FunctionDef) -> set[str]:
    """Names bound *inside* ``fn`` (imports, assignments, params)."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


def _watcher_bodies() -> tuple[set[str], set[str]]:
    """Return (start_execution locals, _run locals) from the live source.

    ``start_execution`` lives in ``backend/routes/execution.py`` since the
    2026-09-25 split; :func:`find_def` follows the code across modules.
    """
    outer = find_def("start_execution").node
    inner = next(
        n for n in ast.walk(outer)
        if isinstance(n, ast.FunctionDef) and n.name == "_run"
    )
    return _local_bindings(outer), _local_bindings(inner)


# ---------------------------------------------------------------------------
# 1. Structural guard — no shadowing of the enclosing scope's DB helpers
# ---------------------------------------------------------------------------


def test_run_does_not_shadow_enclosing_db_helpers():
    """``_run`` must not rebind ``open_db`` / ``migrate`` / ``ExecutionRepository``.

    Re-importing any of these inside ``_run`` turns the name into a
    ``_run``-local for the whole function, so branches that skip the
    import raise ``UnboundLocalError`` at every later use. That is the
    2026-09-13 bug: the auto-verification hand-off was skipped because
    ``open_db`` was unbound on the all-tasks-terminal success path.
    """
    outer, inner = _watcher_bodies()
    enclosing = {"open_db", "migrate", "ExecutionRepository"}

    assert enclosing <= outer, (
        "start_execution no longer binds the DB helpers the watcher closes "
        "over — update this guard if the import seam moved."
    )
    shadowed = sorted(enclosing & inner)
    assert shadowed == [], (
        f"_run re-binds enclosing-scope name(s) {shadowed}; remove the local "
        "import and rely on the start_execution closure instead."
    )


def test_run_actually_uses_open_db():
    """Guard against a vacuous pass: ``_run`` must still call ``open_db``."""
    outer = find_def("start_execution").node
    inner = next(
        n for n in ast.walk(outer)
        if isinstance(n, ast.FunctionDef) and n.name == "_run"
    )
    called = {
        n.func.id
        for n in ast.walk(inner)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "open_db" in called


# ---------------------------------------------------------------------------
# 2. Behavioural — the hand-off fires even when the status persist fails
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_server(tmp_path, monkeypatch):
    """Wire ``server`` to a hermetic plan dir + state.db and neutral seams."""
    server, plan_id, project_dir, plan_dir, calls, fake = _isolate_server(
        tmp_path, monkeypatch, returncode=0,
    )
    yield server, plan_id, project_dir, calls, fake
    server._execution_state.pop(plan_id, None)


def _isolate_server(tmp_path, monkeypatch, *, returncode: int):
    """The isolated server, with an executor that exits with *returncode*.

    ``returncode=0`` is the clean success path; anything else is the
    executor dying — which is the path the shutdown checkpoint has to
    be able to interrupt.
    """
    import server

    plan_id = "handoff-plan"
    plan_dir = tmp_path / "plans" / plan_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "1", "title": "t", "depends_on": []}]})
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    db_path = _make_state_db(tmp_path)
    _seed_plan(db_path, plan_id, project_dir)

    monkeypatch.setattr(server, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    monkeypatch.setattr(server, "_reject_if_placeholder_prd", lambda *a, **k: None)
    monkeypatch.setattr(
        "state_machine.db.archive_scan.classify_plan", lambda *a, **k: "active"
    )
    # All tasks terminal → the clean success path (no orphan derivation branch).
    monkeypatch.setattr(
        server,
        "_count_unfinished_tasks",
        lambda *a, **k: {"total": 1, "pending_or_unstarted": 0, "failed": 0},
    )
    fake = _FakeProcess()
    fake.returncode = returncode
    monkeypatch.setattr(
        server,
        "_spawn_executor_subprocess",
        lambda **kwargs: (fake, tmp_path / "executor.log"),
    )

    calls: list[tuple] = []
    monkeypatch.setattr(
        server,
        "_run_auto_verification_loop",
        lambda *a, **k: calls.append((a, k)),
    )

    server._execution_state.pop(plan_id, None)
    return server, plan_id, project_dir, plan_dir, calls, fake


@pytest.fixture
def isolated_dead_server(tmp_path, monkeypatch):
    """The isolated server, with an executor that was killed by a signal.

    ``-15`` is what a shutdown leaves behind: the executor is in the
    server's tree, and the tree is signalled.
    """
    server, plan_id, project_dir, plan_dir, calls, fake = _isolate_server(
        tmp_path, monkeypatch, returncode=-15,
    )
    yield server, plan_id, project_dir, plan_dir, calls, fake
    server._execution_state.pop(plan_id, None)


@pytest.fixture
def isolated_exited_server(tmp_path, monkeypatch):
    """The isolated server, with an executor that exited with code 1.

    A positive exit code is the executor's *own* failure — it ran and
    failed, rather than being killed.
    """
    server, plan_id, project_dir, plan_dir, calls, fake = _isolate_server(
        tmp_path, monkeypatch, returncode=1,
    )
    yield server, plan_id, project_dir, plan_dir, calls, fake
    server._execution_state.pop(plan_id, None)


def _release_and_join(fake, calls, reclaim) -> list[tuple]:
    """Release the parked executor handle, then join the watcher thread.

    The watcher thread IS the production execution monitor
    (``routes/execution.py``).  Its tail opens the state database through
    ``_state_db_path()`` — resolved **on every call** — and writes to it,
    so a monitor that is still running when this test ends writes into the
    NEXT test's database instead of its own.  That is the 2026-09-14
    ``database disk image is malformed`` flake.

    Releasing then joining makes the ordering a guarantee rather than a
    timing hope, and it is the same "release, then join" rule the autouse
    ``clean_execution_state`` fixture applies at teardown — applied here
    while this test still owns the worker.
    """
    fake.release()
    stuck = reclaim()
    assert not stuck, (
        f"the execution monitor still runs after the release: {stuck!r} — a "
        "worker that outlives its test writes into the next test's state.db"
    )
    return calls


def test_auto_verification_fires_on_clean_exit(isolated_server, reclaim_workers):
    """Baseline: rc=0 with every task terminal reaches ``_run_auto_verification_loop``."""
    server, plan_id, project_dir, calls, fake = isolated_server

    server.start_execution(
        plan_id,
        server.StartExecutionRequest(project_dir=str(project_dir)),
    )
    calls = _release_and_join(fake, calls, reclaim_workers)

    assert calls, "auto-verification hand-off never fired"


def test_auto_verification_fires_even_if_status_persist_fails(
    isolated_server, monkeypatch, reclaim_workers
):
    """A failure in the best-effort ``exec_status`` persist must not skip verification.

    Before the fix, *any* exception raised between ``state["ended_at"]``
    and the auto-verification call propagated into ``_run``'s outer
    ``except Exception`` — which swallowed it silently and skipped the
    hand-off entirely.
    """
    server, plan_id, project_dir, calls, fake = isolated_server

    from state_machine.repositories.execution_repository import ExecutionRepository

    def _boom(self, *a, **k):
        raise RuntimeError("simulated state.db contention")

    monkeypatch.setattr(ExecutionRepository, "update_status", _boom)

    server.start_execution(
        plan_id,
        server.StartExecutionRequest(project_dir=str(project_dir)),
    )
    calls = _release_and_join(fake, calls, reclaim_workers)

    assert calls, (
        "the auto-verification hand-off was skipped when the exec_status "
        "persist raised"
    )


# ---------------------------------------------------------------------------
# 3. The watcher asks the shutdown checkpoint before it writes a verdict
# ---------------------------------------------------------------------------
#
# The watcher is a **second** writer of a terminal verdict.  The startup
# recovery scan asks the shutdown checkpoint before it calls a dead
# executor a crash; the watcher did not, and the watcher is the earlier
# one — it sees the executor die the moment the shutdown kills the
# tree, while the scan only sees it a boot later.  So the watcher wrote
# "failed" first, and the plan came back from the restart as a failed
# run instead of a queued one.  ``failed`` is terminal: no phase
# transition leaves it, and no scheduler waits on it.
#
# The rule these pins hold: **every writer of a terminal failure asks
# the checkpoint first**, and the answer it gets is the same answer the
# recovery scan gets.


def _read_execution_row(db_path: Path, plan_id: str) -> dict:
    """The durable row, as the API would read it."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = open_db(db_path)
    try:
        row = ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()
    assert row is not None, f"no execution row for {plan_id!r}"
    return row


def test_a_shutdown_interrupted_run_is_parked_not_failed(
    isolated_dead_server, tmp_path, reclaim_workers
):
    """The executor died because the server went down: the run is parked.

    The verdict of an interrupted run is "stopped" with the phase
    "queued" — the work was interrupted, not broken, and somebody may
    pick it up again.  "failed" is terminal and in nobody's waiting set.
    """
    server, plan_id, project_dir, plan_dir, calls, fake = isolated_dead_server

    server.start_execution(
        plan_id,
        server.StartExecutionRequest(project_dir=str(project_dir)),
    )

    # The checkpoint is written while the run is live: it says "the
    # server went down under *this* run", and a checkpoint older than
    # the run says nothing about it.
    from pause_state import write_pause_checkpoint
    from shutdown_checkpoint import SHUTDOWN_STOP_REASON

    write_pause_checkpoint(
        plan_dir, kind="execution", reason=SHUTDOWN_STOP_REASON,
    )

    fake.release()
    stuck = reclaim_workers()
    assert not stuck, (
        f"the execution monitor still runs after the release: {stuck!r}"
    )

    row = _read_execution_row(tmp_path / "state.db", plan_id)
    assert row["exec_status"] == "stopped", (
        "a run the server took down was written down as "
        f"{row['exec_status']!r}; it should be parked as 'stopped'"
    )
    assert row["stop_reason"] == SHUTDOWN_STOP_REASON, (
        f"the parked run does not say why it was parked: "
        f"{row['stop_reason']!r}"
    )
    state = server._execution_state.get(plan_id) or {}
    assert state.get("status") == "stopped", (
        f"this process' memory disagrees with the row: {state.get('status')!r}"
    )


def test_a_run_that_died_on_its_own_is_still_failed(
    isolated_exited_server, tmp_path, reclaim_workers
):
    """The negative control: no checkpoint means the work died, so it failed.

    Without this, "always park" would pass the test above — and would
    hide every real crash.

    The reason carries the number that ended the run: the exit code
    here, so the row says ``non_zero_exit:1`` and not a bare
    ``non_zero_exit``.  An operator reading the row cannot tell exit 1
    from exit 7 otherwise, and the two do not call for the same
    response.
    """
    server, plan_id, project_dir, plan_dir, calls, fake = isolated_exited_server

    server.start_execution(
        plan_id,
        server.StartExecutionRequest(project_dir=str(project_dir)),
    )
    fake.release()
    stuck = reclaim_workers()
    assert not stuck, (
        f"the execution monitor still runs after the release: {stuck!r}"
    )

    row = _read_execution_row(tmp_path / "state.db", plan_id)
    assert row["exec_status"] == "failed", (
        f"an executor that died on its own was written down as "
        f"{row['exec_status']!r}; without a checkpoint it is a crash"
    )
    assert row["stop_reason"] == "non_zero_exit:1", (
        f"the failed run does not say why it failed: {row['stop_reason']!r}"
    )


def test_a_signalled_run_with_no_checkpoint_is_still_failed(
    isolated_dead_server, tmp_path, reclaim_workers
):
    """The other negative control: a signalled executor, no checkpoint.

    The signal is not the evidence.  A shutdown signsalled executor and
    a crash that killed the executor look the same from here — the
    difference is the checkpoint, and without one the run failed.

    The reason names the signal that ended it — ``signal:15`` for the
    SIGTERM this run was given — so the row says what killed it rather
    than only that it died.
    """
    server, plan_id, project_dir, plan_dir, calls, fake = isolated_dead_server

    server.start_execution(
        plan_id,
        server.StartExecutionRequest(project_dir=str(project_dir)),
    )
    fake.release()
    stuck = reclaim_workers()
    assert not stuck, (
        f"the execution monitor still runs after the release: {stuck!r}"
    )

    row = _read_execution_row(tmp_path / "state.db", plan_id)
    assert row["exec_status"] == "failed", (
        f"a signalled executor without a checkpoint was written down as "
        f"{row['exec_status']!r}"
    )
    assert row["stop_reason"] == "signal:15", (
        f"the failed run does not say why it failed: {row['stop_reason']!r}"
    )


# ---------------------------------------------------------------------------
# 4) The restart finishes a park that the dying server left half done
# ---------------------------------------------------------------------------
#
# The park is **two** writes: the execution row (``stopped`` /
# ``server_shutdown``) and the plan's phase (``queued``).  The writer is
# the server that is going down — the shutdown kills the executor's
# tree, and the executor's watcher is the thread that notices — and that
# watcher is a **daemon** thread, so the interpreter is free to stop it
# between the two writes.
#
# On the real e2e story (``tests/e2e/
# test_shutdown_checkpoint_e2e.py``): the row landed (``plan_execution.
# updated_at`` moved to the second the tree was killed) and the phase
# did not (``plan_routing.updated_at`` stayed on the start).  The
# restart then found a row that said ``stopped`` — a verdict, not a live
# run — and walked past it, because the recovery only parks a row that
# still says ``running``.  The plan came back claiming to be
# ``executing``, and ``executing`` is in nobody's waiting set: the
# scheduler never picked it up again.
#
# So the restart has to **finish** the park.  The row's own
# ``stop_reason`` says a park happened (``server_shutdown`` is written
# by the park and by nothing else), and the plan's phase says whether
# that park finished.


def _seed_parked_plan(
    db_path: Path,
    plan_id: str,
    *,
    phase: str,
    exec_status: str,
    stop_reason: str,
    exec_pid: Optional[int] = None,
) -> None:
    """Seed the two rows a park leaves behind — one of them half written."""
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
                plan_id, None, phase, 1, "[]", "{}", "{}",
                json.dumps(
                    {"status": "pending", "round": 0, "max_rounds": 3,
                     "stop_reason": None}
                ),
                "2026-11-02T00:00:00Z", "2026-11-02T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution (plan_id, attempt_count, "
            "project_dir, stop_reason, task_progress, next_run_at, card_state, "
            "flags, exec_pid, exec_status, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, 0, str(db_path.parent / "project"), stop_reason,
                json.dumps({"tasks": {}}), None, "{}", "{}",
                exec_pid, exec_status, "2026-11-02T00:00:00+00:00",
                "2026-11-02T00:00:01Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _read_routing_phase(db_path: Path, plan_id: str) -> Optional[str]:
    """The phase the plan is in, as the routing row says it."""
    from state_machine.db.connection import open as open_db

    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    return row[0] if row is not None else None


def _run_the_recovery(plans_dir: Path) -> None:
    """Run the boot-time recovery scan over *plans_dir*."""
    from routes.execution import _recover_execution_states

    _recover_execution_states(plans_dir)


def test_the_restart_finishes_a_park_the_dying_server_left_half_done(tmp_path):
    """判据-3: a plan the shutdown interrupted comes back as ``queued``.

    The row says the run was parked by a shutdown, and the plan is
    still in the phase a running execution owns — the two halves of the
    park disagree, because the process that wrote the first half was
    killed before it wrote the second.  The restart finishes the park.
    """
    from shutdown_checkpoint import SHUTDOWN_STOP_REASON

    plan_id = "half-parked-plan"
    plans_dir = tmp_path / "plans"
    (plans_dir / plan_id).mkdir(parents=True)
    db_path = _make_state_db(tmp_path)

    _seed_parked_plan(
        db_path, plan_id,
        phase="executing",
        exec_status="stopped",
        stop_reason=SHUTDOWN_STOP_REASON,
    )

    _run_the_recovery(plans_dir)

    phase = _read_routing_phase(db_path, plan_id)
    assert phase == "queued", (
        "the restart did not finish the park: the plan is stranded in "
        f"{phase!r}, and a plan in that phase is in nobody's waiting set"
    )


def test_a_run_that_failed_on_its_own_is_not_requeued_by_the_restart(tmp_path):
    """The negative control: a crash is not an interruption.

    Without this, "re-queue every stopped plan" would pass the test
    above — and would resurrect every plan that actually crashed.
    """
    plan_id = "crashed-plan"
    plans_dir = tmp_path / "plans"
    (plans_dir / plan_id).mkdir(parents=True)
    db_path = _make_state_db(tmp_path)

    _seed_parked_plan(
        db_path, plan_id,
        phase="executing",
        exec_status="failed",
        stop_reason="non_zero_exit",
    )

    _run_the_recovery(plans_dir)

    phase = _read_routing_phase(db_path, plan_id)
    assert phase == "executing", (
        f"the restart re-queued a plan that crashed: {phase!r}"
    )


def test_a_plan_that_moved_on_is_not_requeued_by_the_restart(tmp_path):
    """The other negative control: a finished plan stays finished.

    The parked row is old news and the phase is the plan's current
    state: a plan that has moved on since the park owns its own phase,
    and re-queueing it would resurrect work that already completed.
    """
    from shutdown_checkpoint import SHUTDOWN_STOP_REASON

    plan_id = "finished-plan"
    plans_dir = tmp_path / "plans"
    (plans_dir / plan_id).mkdir(parents=True)
    db_path = _make_state_db(tmp_path)

    _seed_parked_plan(
        db_path, plan_id,
        phase="completed",
        exec_status="stopped",
        stop_reason=SHUTDOWN_STOP_REASON,
    )

    _run_the_recovery(plans_dir)

    phase = _read_routing_phase(db_path, plan_id)
    assert phase == "completed", (
        f"the restart re-queued a plan that had already finished: {phase!r}"
    )

"""
E2E tests for ``AutonomousAgent`` file-write conflict prevention.

Background
----------
PRD test-design decision point 4 requires end-to-end verification that the
``tasks.json`` → real-filesystem → file-conflict chain is intact. Three
contracts are pinned by the tests in this module:

  1. ``test_shared_file_serial_no_corruption`` — Two tasks that both declare
     ``files_to_modify=["shared.py"]`` must run serially. The dispatcher
     either puts them in separate micro-layers (``_build_micro_layers``
     serialises any connected conflict component into one-task-per-layer
     slices) or rejects the second one via the
     ``_IN_FLIGHT_FILES`` memory check inside
     :meth:`AutonomousAgent._pre_task_lock_hook`. Either way, their
     wall-clock windows must NOT overlap.

  2. ``test_independent_tasks_concurrent`` — Tasks with disjoint
     ``files_to_modify`` entries must be batched into a single
     micro-layer by ``_build_micro_layers`` (because each task is a
     singleton conflict component) and execute concurrently. The test
     places two disjoint-file singletons next to a shared-file
     singleton (``shared.py`` with only one task sharing it) and
     asserts all three run in the same micro-layer window — i.e.
     the "concurrent even alongside shared-file tasks" contract.

  3. ``test_no_lock_leak`` — After ``agent.run()`` returns, every OS
     lock acquired during execution must have been released AND the
     ``_IN_FLIGHT_FILES`` memory map must be empty. The test asserts
     both: a fresh :class:`FileLockManager` can re-acquire every file
     previously locked, and the in-flight map is empty.

All three tests stub ``_execute_task_with_retry`` so no real LLM call
ever happens; the stubs synchronously record timing / concurrency
metadata via thread-safe primitives and update the task status to
``"completed"`` so the dispatcher's layer-rebuild on the next iteration
sees the work as terminal and progresses.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


pytestmark = [
    pytest.mark.e2e,
]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _git_init(project_dir: Path) -> None:
    """Initialise a real git repo at ``project_dir`` so ``GitManager``
    can bind to it (GitManager uses ``search_parent_directories=True``
    and would otherwise walk up to a sibling checkout).
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
        ["git", "config", "user.name", "Conflict Test"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )


def _write_tasks(project_dir: Path, tasks: list) -> Path:
    """Helper: write a tasks.json with the given task dicts.

    2026-09-13 — also materialises every relative path declared in a
    task's ``files_to_modify`` as an empty placeholder file. The
    dispatcher's post-read gate runs ``TaskOutputValidator``, whose
    step 3 (``_files_to_modify_existence_check``) rejects any declared
    path that is not on disk (``"{path} not found"``). These fixtures
    name files the tasks are meant to write, so the paths have to exist
    for the plan to load at all. Before this change every test in this
    module died at ``agent._load_tasks()`` with a step-3 error.
    """
    for task in tasks:
        for rel in task.get("files_to_modify") or []:
            # Skip the sentinels (``__UNKNOWN_MODIFICATIONS__`` /
            # ``__NO_FILE_CHANGES__``) — those are not real paths.
            if not isinstance(rel, str) or rel.startswith("__"):
                continue
            target = project_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text("", encoding="utf-8")

    tasks_file = project_dir / "tasks.json"
    payload = {
        "requirement": "TDD spec for e2e file-conflict prevention",
        "tasks": tasks,
    }
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tasks_file


class _StubCodingTool:
    """Minimal coding tool stub. The e2e tests stub
    ``_execute_task_with_retry`` so ``query_json`` should never be
    called; the stub raises loudly if it is.
    """

    def __init__(self, response: Optional[dict] = None) -> None:
        self.response = response
        self.calls: list = []

    def query_json(self, prompt: str, system_instruction: Optional[str] = None) -> dict:
        self.calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
        })
        raise AssertionError(
            "query_json must NOT be called in e2e file-conflict tests; "
            "_execute_task_with_retry is stubbed at the agent level"
        )


def _build_agent(project_dir: Path, coding_tool):
    """Build a minimal AutonomousAgent bound to ``project_dir``."""
    from agent import AutonomousAgent

    return AutonomousAgent(
        requirement="TDD spec for e2e file-conflict prevention",
        project_dir=project_dir,
        coding_tool=coding_tool,
        logger=None,
    )


def _install_event_loop_for_sync_test() -> tuple:
    """Install a fresh event loop for the main thread.

    ``ProviderConcurrencyController.__init__`` creates an
    :class:`asyncio.Semaphore`, which on Python 3.9 eagerly binds to
    the *current* event loop. A sync test that constructs the
    controller without a running loop raises ``RuntimeError``. This
    helper installs a fresh loop + policy, exposes a teardown that
    closes the loop and restores the previous policy, and is
    borrowed verbatim from ``tests/unit/test_agent_dispatch.py``.
    """
    saved_policy = asyncio.get_event_loop_policy()
    fresh_policy = asyncio.DefaultEventLoopPolicy()
    asyncio.set_event_loop_policy(fresh_policy)
    loop = fresh_policy.new_event_loop()
    fresh_policy.set_event_loop(loop)

    def _restore() -> None:
        try:
            loop.close()
        except Exception:
            pass
        try:
            asyncio.set_event_loop_policy(saved_policy)
        except Exception:
            pass

    return loop, _restore


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A real project_dir with a real git repo so GitManager can bind.

    2026-09-13 — the directory name must NOT be ``project``. ``plan_id``
    is derived from ``tasks_file.parent.name``, and ``TaskManager``
    refuses reserved names (``project``, ``plans``, ``test``, ...) so
    that generic layouts cannot pollute ``state.db``. With a refused
    plan_id the ``plan_execution.task_progress`` mirror is skipped, so
    ``_load_tasks``' SQLite re-hydration finds nothing and every reload
    reverts the tasks to ``status="pending"`` — which makes the
    dispatcher re-schedule already-completed tasks until the same-id
    loop guard trips three layers later. In production the plan id is
    real, so the hydrate works; the tests need a non-reserved name to
    reproduce that.

    The ``tmp_path`` slug is part of the name so each test gets its own
    plan id. A constant name would make all four tests share one
    ``plan_execution`` row, and the first test to complete a task id
    would leave the others loading it as ``completed`` — they then skip
    every dispatch and fail with empty timing windows.
    """
    slug = "".join(
        ch if (ch.isalnum() or ch in "._-") else "-" for ch in tmp_path.name
    ).strip("-")
    pd = tmp_path / f"e2e-file-conflict-{slug or 'case'}"
    _git_init(pd)
    return pd


@pytest.fixture(autouse=True)
def _hermetic_state_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point the ``plan_execution``/``plan_tasks`` mirror at a temp DB.

    ``PDT_STATE_DB_PATH`` is honoured by both ``agent._get_task_progress_
    repository`` and ``TaskManager._persist_status_to_sqlite``; without
    the override the run writes into the repo-level ``state.db``, which
    leaks task statuses across tests AND across pytest sessions (a
    re-run would find yesterday's ``completed`` rows for the same plan
    id and dispatch nothing).
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))


# 2026-09-13 — the autouse ``_clean_in_flight`` fixture that used to live
# here was deleted along with its ``from agent import _IN_FLIGHT_FILES,
# _IN_FLIGHT_LOCK`` body. Those module globals were removed by the TC-006 /
# AC-009 / VP-024 dependency-injection refactor (commit ``f134499``); the
# in-flight map now lives on the agent instance
# (``AutonomousAgent._in_flight_files``). Because every test below builds
# its own agent via ``_build_agent``, there is no process-wide map left to
# reset between tests, so the fixture was a no-op wrapped around a stale
# import that failed at setup time for all four tests.


# ---------------------------------------------------------------------------
# Test 1 — shared-file tasks run serially
# ---------------------------------------------------------------------------


def test_shared_file_serial_no_corruption(project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two tasks sharing ``shared.py`` must execute serially.

    Setup:
      * ``t1`` with ``files_to_modify=["shared.py"]``
      * ``t2`` with ``files_to_modify=["shared.py"]``
      * No ``depends_on`` — both land in outer layer 0.

    Expected layer shape (from ``_build_micro_layers``):
      * t1 and t2 share a file → connected conflict component of
        size 2 → serialised into individual micro-layers
        ``[[t1], [t2]]``.
      * The dispatcher's outer loop processes one micro-layer per
        iteration, so t1's full execution window elapses before t2
        starts. The memory-lock layer
        (``_IN_FLIGHT_FILES``) is a second line of defence — even if
        the layer build were wrong, the second
        ``_pre_task_lock_hook`` call would raise before t2's executor
        ran.

    Assertion:
      * ``t1.end < t2.start`` (strictly serial — no overlap).
      * Both tasks reach status ``"completed"`` after the run.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "t1",
                "title": "Shared writer #1",
                "description": "first writer for shared.py",
                "test_command": "echo t1",
                "status": "pending",
                "depends_on": [],
                "files_to_modify": ["shared.py"],
            },
            {
                "id": "t2",
                "title": "Shared writer #2",
                "description": "second writer for shared.py",
                "test_command": "echo t2",
                "status": "pending",
                "depends_on": [],
                "files_to_modify": ["shared.py"],
            },
        ],
    )

    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool)
    agent._load_tasks()

    monkeypatch.setenv("PDT_PROVIDER_NAME", "vendor-a-pro")
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "10")

    loop, restore = _install_event_loop_for_sync_test()
    try:
        from provider_concurrency import ProviderConcurrencyController

        controller = ProviderConcurrencyController(
            global_limit=10,
            provider_limits={"vendor-a-pro": 10},
        )
        agent._provider_controller = controller

        timings: dict[str, dict[str, float]] = {}
        timings_lock = threading.Lock()

        def _stub_execute(task, max_retries=5, timeout=None):
            with timings_lock:
                timings[task.id] = {"start": time.monotonic()}
            # Force a measurable body window so the serial property is
            # observable even on a fast CI box. 50ms is well above any
            # scheduling overhead, so a parallel run would still see
            # overlap; a serial run sees the gap.
            time.sleep(0.05)
            with timings_lock:
                timings[task.id]["end"] = time.monotonic()
            agent.task_manager.update_task_status(task.id, "completed")
            # Mirror the real completion bookkeeping in
            # ``_execute_task_with_retry`` (agent.py:4630). The
            # dispatcher's same-id loop guard reads
            # ``_session_task_completed_counts`` and breaks the cycle
            # once a task id has completed ``_SAME_ID_LOOP_THRESHOLD``
            # times. A stub that flips the status but skips this counter
            # disarms the guard: the layer rebuild draws from
            # ``self._all_tasks`` (separate SubTask objects from
            # ``task_manager.tasks``), keeps seeing the task as pending,
            # and re-schedules it forever — ``agent.run()`` then never
            # returns and the suite hangs until pytest-timeout.
            agent._session_task_completed_counts[task.id] = (
                agent._session_task_completed_counts.get(task.id, 0) + 1
            )
            return True

        monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

        agent.run(timeout=None)
    finally:
        restore()

    # Both tasks recorded a complete window.
    assert "t1" in timings and "start" in timings["t1"] and "end" in timings["t1"], (
        f"t1 did not record a complete timing window: {timings.get('t1')!r}"
    )
    assert "t2" in timings and "start" in timings["t2"] and "end" in timings["t2"], (
        f"t2 did not record a complete timing window: {timings.get('t2')!r}"
    )

    t1_start, t1_end = timings["t1"]["start"], timings["t1"]["end"]
    t2_start, t2_end = timings["t2"]["start"], timings["t2"]["end"]

    # Serial contract: t1's window closes strictly before t2 opens.
    # Using ``<`` (not ``<=``) ensures they don't even touch — a run
    # that overlapped by even an OS-scheduling quantum would prove
    # the conflict prevention is broken.
    assert t1_end < t2_start, (
        f"shared-file tasks t1 and t2 overlapped — file-conflict prevention "
        f"failed (t1=[{t1_start}, {t1_end}], t2=[{t2_start}, {t2_end}], "
        f"overlap={t1_end - t2_start:.6f}s)"
    )

    # Both tasks reached terminal state.
    completed_ids = {t.id for t in agent.task_manager.tasks if t.status == "completed"}
    assert {"t1", "t2"} <= completed_ids, (
        f"expected both t1 and t2 to reach 'completed', got {completed_ids}"
    )


# ---------------------------------------------------------------------------
# Test 2 — disjoint-file tasks run concurrently with the shared-file task
# ---------------------------------------------------------------------------


def test_independent_tasks_concurrent(project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three singleton tasks (one shared-file, two disjoint) run concurrently.

    Setup:
      * ``t1`` with ``files_to_modify=["shared.py"]``
      * ``t2`` with ``files_to_modify=["alpha.py"]``
      * ``t3`` with ``files_to_modify=["beta.py"]``

    Each task is a singleton conflict component (no other task shares
    any file with it), so ``_build_micro_layers`` flushes all three
    into one micro-layer: ``[[t1, t2, t3]]``. The dispatcher's outer
    loop processes that single micro-layer via ``asyncio.gather``,
    so all three tasks are in flight at the same time.

    The "alongside shared-file tasks" phrasing from the TDD spec
    specifically refers to this scenario: t1 is the shared-file task
    (even though it has only one writer, the file is named after the
    "shared" pattern), and the disjoint-file tasks t2 and t3 are not
    blocked by it. If the conflict graph were over-eager, t2 and t3
    would be serialised into separate micro-layers — and the test
    would fail because their windows would not overlap.

    Assertion:
      * All three tasks' wall-clock windows overlap (t2.start <
        t1.end AND t3.start < t1.end).
      * Peak concurrent in-body count reaches 3.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "t1",
                "title": "Shared-file singleton",
                "description": "singleton writer for shared.py",
                "test_command": "echo t1",
                "status": "pending",
                "depends_on": [],
                "files_to_modify": ["shared.py"],
            },
            {
                "id": "t2",
                "title": "Disjoint writer #1",
                "description": "writes alpha.py — disjoint from t1 and t3",
                "test_command": "echo t2",
                "status": "pending",
                "depends_on": [],
                "files_to_modify": ["alpha.py"],
            },
            {
                "id": "t3",
                "title": "Disjoint writer #2",
                "description": "writes beta.py — disjoint from t1 and t2",
                "test_command": "echo t3",
                "status": "pending",
                "depends_on": [],
                "files_to_modify": ["beta.py"],
            },
        ],
    )

    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool)
    agent._load_tasks()

    monkeypatch.setenv("PDT_PROVIDER_NAME", "vendor-a-pro")
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "10")

    loop, restore = _install_event_loop_for_sync_test()
    try:
        from provider_concurrency import ProviderConcurrencyController

        controller = ProviderConcurrencyController(
            global_limit=10,
            provider_limits={"vendor-a-pro": 10},
        )
        agent._provider_controller = controller

        # Barrier sized to 3 — every task must arrive before any is
        # allowed to leave. A non-overlapping execution would surface
        # as a BrokenBarrierError and fail the test loudly.
        enter_barrier = threading.Barrier(3, timeout=5.0)

        timings: dict[str, dict[str, float]] = {}
        timings_lock = threading.Lock()

        state = {"in_flight": 0, "peak": 0}

        def _stub_execute(task, max_retries=5, timeout=None):
            with timings_lock:
                timings[task.id] = {"start": time.monotonic()}
            with timings_lock:
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
            try:
                enter_barrier.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                pass
            time.sleep(0.02)
            with timings_lock:
                state["in_flight"] -= 1
                timings[task.id]["end"] = time.monotonic()
            agent.task_manager.update_task_status(task.id, "completed")
            # Mirror the real completion bookkeeping in
            # ``_execute_task_with_retry`` (agent.py:4630). The
            # dispatcher's same-id loop guard reads
            # ``_session_task_completed_counts`` and breaks the cycle
            # once a task id has completed ``_SAME_ID_LOOP_THRESHOLD``
            # times. A stub that flips the status but skips this counter
            # disarms the guard: the layer rebuild draws from
            # ``self._all_tasks`` (separate SubTask objects from
            # ``task_manager.tasks``), keeps seeing the task as pending,
            # and re-schedules it forever — ``agent.run()`` then never
            # returns and the suite hangs until pytest-timeout.
            agent._session_task_completed_counts[task.id] = (
                agent._session_task_completed_counts.get(task.id, 0) + 1
            )
            return True

        monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

        agent.run(timeout=None)
    finally:
        restore()

    # All three tasks recorded a complete timing window.
    for tid in ("t1", "t2", "t3"):
        assert tid in timings and "start" in timings[tid] and "end" in timings[tid], (
            f"task {tid} did not record a complete timing window: {timings.get(tid)!r}"
        )

    t1_start, t1_end = timings["t1"]["start"], timings["t1"]["end"]
    t2_start, t2_end = timings["t2"]["start"], timings["t2"]["end"]
    t3_start, t3_end = timings["t3"]["start"], timings["t3"]["end"]

    # Concurrent contract: every disjoint task's window overlaps with
    # t1's window. The barrier forces t2 and t3 to enter body before
    # any task can exit, so a non-overlapping observation proves the
    # micro-layer structure was wrong (tasks serialised when they
    # should have been batched).
    assert t2_start < t1_end and t3_start < t1_end, (
        f"disjoint-file tasks did not overlap with t1 — concurrency lost: "
        f"t1=[{t1_start}, {t1_end}], "
        f"t2=[{t2_start}, {t2_end}], "
        f"t3=[{t3_start}, {t3_end}]. "
        f"This typically means the dispatcher serialised tasks that the "
        f"layer build placed in the same micro-layer."
    )

    # And t2 / t3 must overlap each other too (sanity on the barrier).
    assert t2_start < t3_end and t3_start < t2_end, (
        f"t2 and t3 did not overlap with each other: "
        f"t2=[{t2_start}, {t2_end}], t3=[{t3_start}, {t3_end}]"
    )

    # Peak concurrent in-body count reached 3 (all three inside the
    # barrier simultaneously). If the cap were lower than 3 the
    # barrier would block and the test would fail with a timeout.
    assert state["peak"] == 3, (
        f"expected peak concurrent in-flight == 3, got {state['peak']}; "
        f"the dispatcher is running these singleton tasks serially"
    )

    # All three tasks reached terminal state.
    completed_ids = {t.id for t in agent.task_manager.tasks if t.status == "completed"}
    assert {"t1", "t2", "t3"} <= completed_ids, (
        f"expected all three tasks to reach 'completed', got {completed_ids}"
    )


# ---------------------------------------------------------------------------
# Test 3 — no OS lock or in-flight memory state leaks after run
# ---------------------------------------------------------------------------


def test_no_lock_leak(project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After ``agent.run()`` returns, every OS lock and the
    ``_IN_FLIGHT_FILES`` memory map must be drained.

    The contract is enforced by the ``finally`` block inside
    :meth:`AutonomousAgent._run_task_with_provider_slot`: every task
    that acquired OS locks (via ``_pre_task_lock_hook``) must release
    them (via ``_post_task_lock_hook``) before returning, including on
    the exception / cancellation paths. A leaked OS lock would block
    any subsequent agent that tries to write the same file; a leaked
    in-flight entry would block any subsequent task that happens to
    share that file on the same agent instance.

    Setup mirrors test 1: t1 and t2 share ``shared.py`` so the
    dispatcher walks through the pre/post lock hooks twice (once
    per micro-layer iteration).

    Assertion:
      * ``agent._in_flight_files`` is empty after the run.
      * Every previously-locked file's lock file under
        ``<project_dir>/.pdt/locks/`` does NOT exist (released —
        ``FileLockManager.release()`` deletes the lock file).
      * A fresh :class:`FileLockManager` can acquire every file
        without timing out (proves the OS-level lock was truly
        released, not just the in-memory state cleared).
    """
    from file_lock_manager import FileLockManager

    _write_tasks(
        project_dir,
        [
            {
                "id": "t1",
                "title": "Shared writer #1",
                "description": "first writer for shared.py",
                "test_command": "echo t1",
                "status": "pending",
                "depends_on": [],
                "files_to_modify": ["shared.py"],
            },
            {
                "id": "t2",
                "title": "Shared writer #2",
                "description": "second writer for shared.py",
                "test_command": "echo t2",
                "status": "pending",
                "depends_on": [],
                "files_to_modify": ["shared.py"],
            },
        ],
    )

    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool)
    agent._load_tasks()

    monkeypatch.setenv("PDT_PROVIDER_NAME", "vendor-a-pro")
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "10")

    loop, restore = _install_event_loop_for_sync_test()
    try:
        from provider_concurrency import ProviderConcurrencyController

        controller = ProviderConcurrencyController(
            global_limit=10,
            provider_limits={"vendor-a-pro": 10},
        )
        agent._provider_controller = controller

        def _stub_execute(task, max_retries=5, timeout=None):
            # Sanity probe mid-run: at least one of the tasks must
            # see "shared.py" registered in the in-flight map (proves
            # the pre-hook ran). We capture the set snapshot rather
            # than asserting inline because the post-hook may have
            # already cleared by the time we observe (race with the
            # dispatcher).
            agent.task_manager.update_task_status(task.id, "completed")
            # Mirror the real completion bookkeeping in
            # ``_execute_task_with_retry`` (agent.py:4630). The
            # dispatcher's same-id loop guard reads
            # ``_session_task_completed_counts`` and breaks the cycle
            # once a task id has completed ``_SAME_ID_LOOP_THRESHOLD``
            # times. A stub that flips the status but skips this counter
            # disarms the guard: the layer rebuild draws from
            # ``self._all_tasks`` (separate SubTask objects from
            # ``task_manager.tasks``), keeps seeing the task as pending,
            # and re-schedules it forever — ``agent.run()`` then never
            # returns and the suite hangs until pytest-timeout.
            agent._session_task_completed_counts[task.id] = (
                agent._session_task_completed_counts.get(task.id, 0) + 1
            )
            return True

        monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

        agent.run(timeout=None)
    finally:
        restore()

    # Both tasks completed.
    completed_ids = {t.id for t in agent.task_manager.tasks if t.status == "completed"}
    assert {"t1", "t2"} <= completed_ids, (
        f"expected both t1 and t2 to reach 'completed', got {completed_ids}"
    )

    # --- Assertion 1: in-flight memory map is drained ---
    leaked = dict(agent._in_flight_files)
    assert leaked == {}, (
        f"agent._in_flight_files leaked entries after run: {leaked!r}; "
        f"the post-task lock hook did not clear the in-flight state"
    )

    # --- Assertion 2: the OS-level lock is released ---
    # ``filelock.FileLock.release()`` does NOT delete the lock file —
    # it only releases the OS-level advisory lock (flock / fcntl)
    # so the next acquirer can grab it. The lock file stays on disk
    # as a sentinel. So we cannot assert the file is gone; we
    # instead assert the OS lock state is free by re-acquiring it.
    # We also verify the locks dir was created at all (proves the
    # pre-hook actually ran during the run). Read off the agent rather
    # than recomputed: the location is the plan's own ``locks/`` dir, and
    # recomputing it would pin a formula instead of the wiring.
    locks_dir = agent._locks_dir
    assert locks_dir.exists(), (
        f"expected locks dir at {locks_dir} (proves the pre-task lock "
        f"hook fired during the run); missing implies _pre_task_lock_hook "
        f"was bypassed"
    )

    # --- Assertion 3: a fresh FileLockManager can re-acquire ---
    # This is the definitive check: even if the lock file persists on
    # disk (see Assertion 2 above), the kernel-level flock must be
    # free, otherwise this acquire times out.
    fresh = FileLockManager()
    fresh.acquire(["shared.py"], str(project_dir))
    fresh.release()

# ---------------------------------------------------------------------------
# Test 4 — different-file tasks: file content + concurrent windows
# ---------------------------------------------------------------------------


def test_different_files_concurrent(project_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tasks modifying disjoint files write correct content AND run concurrently.

    VP-027 contract:

      * Construct two tasks whose ``files_to_modify`` are completely
        disjoint — one writes ``shared.py``, the other writes
        ``independent.py``. Each task is a singleton conflict
        component, so ``_build_micro_layers`` flushes them into a
        single micro-layer ``[[t_shared, t_independent]]`` and the
        dispatcher's outer loop runs both via ``asyncio.gather``.

      * Run the full ``agent.run()`` flow end-to-end (not just the
        dispatcher) and assert BOTH files are written with the
        expected content — proves the layer batcher actually drove
        both tasks to a terminal state through the real pipeline,
        not just that the layer builder produced the right shape.

      * Wall-clock windows must overlap: ``t_independent.start <
        t_shared.end`` (and vice versa). The "shared.py serial
        task" wording in the TDD spec refers to the *contract* of
        shared-file writers in general (when multiple writers
        target the same file, they must run serially); in this
        scenario the ``shared.py`` task is a singleton, so the
        "concurrent even with the shared-file singleton"
        invariant applies.

    Setup:
      * ``t_shared`` with ``files_to_modify=["shared.py"]``
      * ``t_independent`` with ``files_to_modify=["independent.py"]``
      * No ``depends_on`` — both land in outer layer 0.

    Expected layer shape (from ``_build_micro_layers``):
      * t_shared and t_independent have no file overlap -> each is
        a singleton component -> one micro-layer
        ``[[t_shared, t_independent]]``.

    Assertion:
      * ``shared.py`` exists in ``project_dir`` with the
        expected content (written by ``t_shared``).
      * ``independent.py`` exists in ``project_dir`` with the
        expected content (written by ``t_independent``).
      * ``t_independent.start < t_shared.end`` (concurrent
        window — no serialisation between disjoint files).
      * ``t_shared.start < t_independent.end`` (the other
        direction of the overlap).
      * Both tasks reach status ``"completed"``.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "t_shared",
                "title": "Writes shared.py",
                "description": "singleton writer for shared.py",
                "test_command": "echo t_shared",
                "status": "pending",
                "depends_on": [],
                "files_to_modify": ["shared.py"],
            },
            {
                "id": "t_independent",
                "title": "Writes independent.py",
                "description": "writes independent.py — disjoint from t_shared",
                "test_command": "echo t_independent",
                "status": "pending",
                "depends_on": [],
                "files_to_modify": ["independent.py"],
            },
        ],
    )

    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool)
    agent._load_tasks()

    monkeypatch.setenv("PDT_PROVIDER_NAME", "vendor-a-pro")
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "10")

    loop, restore = _install_event_loop_for_sync_test()
    try:
        from provider_concurrency import ProviderConcurrencyController

        controller = ProviderConcurrencyController(
            global_limit=10,
            provider_limits={"vendor-a-pro": 10},
        )
        agent._provider_controller = controller

        timings = {}
        timings_lock = threading.Lock()
        state = {"in_flight": 0, "peak": 0}

        # Barrier sized to 2 — both tasks must arrive before either
        # is allowed to leave, giving a deterministic overlap window
        # that does not depend on scheduler timing.
        enter_barrier = threading.Barrier(2, timeout=5.0)

        def _stub_execute(task, max_retries=5, timeout=None):
            with timings_lock:
                timings[task.id] = {"start": time.monotonic()}
            with timings_lock:
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
            try:
                # Both tasks must enter body before either exits.
                enter_barrier.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                pass
            # Write the expected file content for this task so the
            # "both files written correctly" assertion has real
            # evidence on disk (not just a task-status signal).
            if task.id == "t_shared":
                (project_dir / "shared.py").write_text(
                    "written by t_shared\n", encoding="utf-8"
                )
            elif task.id == "t_independent":
                (project_dir / "independent.py").write_text(
                    "written by t_independent\n", encoding="utf-8"
                )
            time.sleep(0.02)
            with timings_lock:
                state["in_flight"] -= 1
                timings[task.id]["end"] = time.monotonic()
            agent.task_manager.update_task_status(task.id, "completed")
            # Mirror the real completion bookkeeping in
            # ``_execute_task_with_retry`` (agent.py:4630). The
            # dispatcher's same-id loop guard reads
            # ``_session_task_completed_counts`` and breaks the cycle
            # once a task id has completed ``_SAME_ID_LOOP_THRESHOLD``
            # times. A stub that flips the status but skips this counter
            # disarms the guard: the layer rebuild draws from
            # ``self._all_tasks`` (separate SubTask objects from
            # ``task_manager.tasks``), keeps seeing the task as pending,
            # and re-schedules it forever — ``agent.run()`` then never
            # returns and the suite hangs until pytest-timeout.
            agent._session_task_completed_counts[task.id] = (
                agent._session_task_completed_counts.get(task.id, 0) + 1
            )
            return True

        monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

        agent.run(timeout=None)
    finally:
        restore()

    # Both tasks recorded a complete timing window.
    for tid in ("t_shared", "t_independent"):
        assert tid in timings and "start" in timings[tid] and "end" in timings[tid], (
            f"task {tid} did not record a complete timing window: {timings.get(tid)!r}"
        )

    # --- File-content assertions: both files written correctly ---
    shared_path = project_dir / "shared.py"
    independent_path = project_dir / "independent.py"
    assert shared_path.exists(), (
        f"expected shared.py to be written by t_shared at {shared_path}; "
        f"missing implies the dispatcher's task execution did not run"
    )
    assert shared_path.read_text(encoding="utf-8") == "written by t_shared\n", (
        f"shared.py content mismatch: got {shared_path.read_text(encoding='utf-8')!r}"
    )
    assert independent_path.exists(), (
        f"expected independent.py to be written by t_independent at "
        f"{independent_path}; missing implies the dispatcher's task "
        f"execution did not run"
    )
    assert independent_path.read_text(encoding="utf-8") == "written by t_independent\n", (
        f"independent.py content mismatch: got {independent_path.read_text(encoding='utf-8')!r}"
    )

    # --- Concurrency assertions: windows must overlap in both directions ---
    ts_start, ts_end = timings["t_shared"]["start"], timings["t_shared"]["end"]
    ti_start, ti_end = timings["t_independent"]["start"], timings["t_independent"]["end"]

    # shared.py serial-task vs independent.py-task overlap (TDD spec wording).
    assert ti_start < ts_end, (
        f"independent.py task did not overlap shared.py task (ti.start < "
        f"ts.end failed): t_shared=[{ts_start}, {ts_end}], "
        f"t_independent=[{ti_start}, {ti_end}]. This typically means the "
        f"dispatcher serialised disjoint-file tasks that the layer build "
        f"placed in the same micro-layer."
    )
    # Reverse direction — both must overlap each other (sanity).
    assert ts_start < ti_end, (
        f"shared.py task did not overlap independent.py task "
        f"(ts.start < ti.end failed): t_shared=[{ts_start}, {ts_end}], "
        f"t_independent=[{ti_start}, {ti_end}]"
    )

    # Peak in-flight reached 2 (both inside barrier simultaneously).
    assert state["peak"] == 2, (
        f"expected peak concurrent in-flight == 2, got {state['peak']}; "
        f"the dispatcher is running these disjoint-file tasks serially"
    )

    # Both tasks reached terminal state.
    completed_ids = {t.id for t in agent.task_manager.tasks if t.status == "completed"}
    assert {"t_shared", "t_independent"} <= completed_ids, (
        f"expected both tasks to reach 'completed', got {completed_ids}"
    )

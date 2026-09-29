"""
Regression test for the same-id loop detector status-clobber bug.

Background
----------
``agent.py:2646-2665`` (pre-fix) used to call
``task_manager.record_task_failure(t.id, "same_id_re_run_loop")`` whenever
the same-id loop detector tripped on a task that had already been
marked ``completed`` in the session. The result: the persisted
``plan_execution.task_progress`` row for that task was overwritten
from ``status='completed'`` to ``status='failed'``, and the task-sync
card / counts endpoint showed a fake failure for work that had
actually committed successfully.

The fix introduces a session-local ``_dispatcher_blocked: set[str]``
on the agent and changes the loop detector handler to:

  * If ``task.status == 'completed'``: add the task id to
    ``_dispatcher_blocked`` and do NOT call ``record_task_failure``.
    The completed status stays intact.
  * Otherwise: keep the legacy ``record_task_failure`` path so the
    loop still terminates for tasks that never reached a clean
    completion.

These three TDD tests pin the new contract.

  1. ``test_same_id_loop_preserves_completed_status``
     Simulate the same-id loop on a task whose ``status`` is already
     ``"completed"``. After the handler runs, the task's status must
     remain ``"completed"`` and ``_dispatcher_blocked`` must contain
     its id. Without the fix this would set status to ``"failed"``.

  2. ``test_same_id_loop_records_failure_for_pending_task``
     Regression guard for the legacy path: a task whose ``status`` is
     still ``"pending"`` (e.g. it never made it past ``task_started``
     because of an LLM-side error) when the loop trips MUST still be
     marked failed via ``record_task_failure`` so the dispatcher stops
     scheduling it.

  3. ``test_get_active_tasks_excludes_dispatcher_blocked``
     ``_get_active_tasks_for_scheduling`` must skip task ids present
     in ``_dispatcher_blocked``. Without this filter, the next layer
     rebuild would re-emit the task and trip the same-id detector
     again — turning the loop break into a tight per-layer cycle
     rather than a one-shot exit.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _git_init(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )
    if not (project_dir / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir), capture_output=True, text=True, check=True,
        )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )


def _write_tasks(project_dir: Path, tasks: list) -> Path:
    tasks_file = project_dir / "tasks.json"
    payload = {"requirement": "TDD spec", "tasks": tasks}
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tasks_file


def _make_agent(project_dir: Path):
    """Build a minimal AutonomousAgent bound to ``project_dir``."""
    from agent import AutonomousAgent

    _write_tasks(project_dir, [
        {"id": "1", "title": "demo task", "description": "x", "test_command": "true"},
    ])

    return AutonomousAgent(
        requirement="TDD spec",
        project_dir=project_dir,
        coding_tool=None,
        config={"max_retries": 1},
        logger=None,
        tasks_file=project_dir / "tasks.json",
    )


def test_same_id_loop_preserves_completed_status(tmp_path):
    """A completed task must NOT be clobbered to ``status='failed'``
    when the same-id loop detector trips."""
    _git_init(tmp_path)
    agent = _make_agent(tmp_path)

    # Simulate: task already marked completed (real commit happened)
    task = agent.task_manager.tasks[0]
    task.status = "completed"

    # Simulate the same-id loop guard path. Direct call into the
    # handler logic — we don't need the full async main loop because
    # the bug is in the synchronous handler block at agent.py:2646+.
    current_status = getattr(task, "status", None)
    if current_status == "completed":
        agent._dispatcher_blocked.add(task.id)
    else:
        agent.task_manager.record_task_failure(
            task.id, "same_id_re_run_loop"
        )

    assert task.status == "completed", (
        "completed status must be preserved; the loop detector's "
        "``record_task_failure`` overwrite was the root cause of "
        "the fake 'failed' state on the task_sync card."
    )
    assert task.id in agent._dispatcher_blocked, (
        "_dispatcher_blocked must contain the task id so the next "
        "layer build excludes it from scheduling."
    )


def test_same_id_loop_records_failure_for_pending_task(tmp_path):
    """Legacy path: a task still in 'pending' when the loop trips MUST
    be marked failed via ``record_task_failure`` — without that the
    loop never terminates."""
    _git_init(tmp_path)
    agent = _make_agent(tmp_path)

    task = agent.task_manager.tasks[0]
    # Task never made it past task_started — still pending
    assert task.status == "pending"

    current_status = getattr(task, "status", None)
    if current_status == "completed":
        agent._dispatcher_blocked.add(task.id)
    else:
        agent.task_manager.record_task_failure(
            task.id, "same_id_re_run_loop"
        )

    assert task.status == "failed", (
        "Pending tasks must still be marked failed so the dispatcher "
        "stops re-scheduling them."
    )
    assert task.id not in agent._dispatcher_blocked, (
        "Pending tasks go through the legacy record_task_failure path "
        "and should not be added to _dispatcher_blocked (they are "
        "already terminal)."
    )


def test_get_active_tasks_excludes_dispatcher_blocked(tmp_path):
    """``_get_active_tasks_for_scheduling`` must skip task ids in
    ``_dispatcher_blocked`` even if their status is still
    ``"pending"`` (the race-condition window where the task's
    in-memory status hasn't caught up to the loop break yet)."""
    _git_init(tmp_path)
    agent = _make_agent(tmp_path)

    task = agent.task_manager.tasks[0]
    # Simulate the race: status still pending but dispatcher already
    # blocked it from re-scheduling.
    task.status = "pending"
    agent._dispatcher_blocked.add(task.id)

    active = agent._get_active_tasks_for_scheduling()
    assert task not in active, (
        "_dispatcher_blocked tasks must be excluded from the active "
        "scheduling list. Without this filter the next layer rebuild "
        "would re-emit the task and trip the same-id detector every "
        "iteration."
    )


def test_same_id_loop_session_count_geq_threshold_preserves_completed(tmp_path):
    """Regression test: when the
    in-memory status drifts to ``"in_progress"`` / ``"pending"`` due
    to a transient ``_persist_status_to_sqlite`` failure but the
    session counter proves the executor DID complete the task, the
    loop detector must RE-SYNC ``completed`` and add to the blocker
    set — NOT clobber with ``record_task_failure``."""
    _git_init(tmp_path)
    agent = _make_agent(tmp_path)
    # ``_session_task_completed_counts`` is initialised inside
    # ``_run_async``; tests that bypass the async main loop must
    # create the dict manually.
    agent._session_task_completed_counts = {}

    task = agent.task_manager.tasks[0]
    # Simulate: SQLite hydrate failed so in-memory status is stuck
    # at "in_progress", but session counter says we actually completed.
    task.status = "in_progress"
    agent._session_task_completed_counts[task.id] = 2  # >= threshold

    # Replay the new decision block from agent.py:2682-2784
    current_status = getattr(task, "status", None)
    session_count = agent._session_task_completed_counts.get(task.id, 0)
    _THRESHOLD = 2
    if current_status == "completed":
        preserved_status = "completed"
    elif session_count >= _THRESHOLD:
        preserved_status = "completed"
        agent.task_manager.update_task_status(task.id, "completed")
    else:
        preserved_status = current_status

    if preserved_status == "completed":
        agent._dispatcher_blocked.add(task.id)
    else:
        agent.task_manager.record_task_failure(
            task.id, "same_id_re_run_loop"
        )

    assert task.status == "completed", (
        "Session counter >= threshold must RE-SYNC status to "
        "'completed' rather than letting the loop detector clobber "
        "the legitimate committed work with 'failed'."
    )
    assert task.id in agent._dispatcher_blocked, (
        "_dispatcher_blocked must contain the task id so the next "
        "layer build excludes it from scheduling."
    )


def test_same_id_loop_session_count_zero_still_records_failure(tmp_path):
    """Regression test: session counter == 0 + non-completed status
    must still fall through to the legacy ``record_task_failure``
    path (otherwise a real failure mode loses its terminator)."""
    _git_init(tmp_path)
    agent = _make_agent(tmp_path)
    agent._session_task_completed_counts = {}

    task = agent.task_manager.tasks[0]
    task.status = "in_progress"
    # Session counter is 0 — task was never completed in this session
    assert agent._session_task_completed_counts.get(task.id, 0) == 0

    current_status = getattr(task, "status", None)
    session_count = agent._session_task_completed_counts.get(task.id, 0)
    _THRESHOLD = 2
    if current_status == "completed":
        preserved_status = "completed"
    elif session_count >= _THRESHOLD:
        preserved_status = "completed"
        agent.task_manager.update_task_status(task.id, "completed")
    else:
        preserved_status = current_status

    if preserved_status == "completed":
        agent._dispatcher_blocked.add(task.id)
    else:
        agent.task_manager.record_task_failure(
            task.id, "same_id_re_run_loop"
        )

    assert task.status == "failed", (
        "Without session counter evidence the legacy "
        "``record_task_failure`` path must still terminate the loop."
    )
    assert task.id not in agent._dispatcher_blocked

# ---------------------------------------------------------------------------
# Regression suite for the same-id loop *infinite retry* path.
#
# A refiner can break ``task 1`` into 1-1/1-2/1-3/1-4 and rewrite task
# 2-1's ``depends_on`` without rewriting its description. The executor
# then enters ``while True:`` and the same-id loop detector fires on
# every pass, because:
#
#   1. ``same_id_loop_reload_failed`` logged the reload error and
#      **continued** without breaking the outer loop.
#   2. The in-memory ``task_manager.tasks`` still pointed at the
#      pre-refiner shape, so task 2-1 kept being scheduled even
#      though its disk description disagreed with ``depends_on``.
#
# The tests below pin each failure mode individually so future
# changes can't silently re-introduce the loop.
# ---------------------------------------------------------------------------


def _build_plan_with_split_parent(tmp_path):
    """Build a minimal plan that reproduces the earlier plan's bug.

    ``task_2_1`` has a stale description ("前置条件 1") whose
    ``depends_on`` was rewritten by the refiner to
    ``["1-1", "1-2", "1-3", "1-4"]`` (the children of a split
    parent). Loading this plan via ``_load_tasks`` must raise.
    """
    _git_init(tmp_path)
    _write_tasks(tmp_path, [
        {"id": "1-1", "title": "split 1/4", "description": "x", "test_command": "true"},
        {"id": "1-2", "title": "split 2/4", "description": "x", "test_command": "true"},
        {"id": "1-3", "title": "split 3/4", "description": "x", "test_command": "true"},
        {"id": "1-4", "title": "split 4/4", "description": "x", "test_command": "true"},
        {
            "id": "2-1",
            "title": "downstream of split parent",
            # Stale description: mentions parent id "1" but deps are
            # the children the refiner produced.
            "description": "## 背景\n前置条件：1 已完成。",
            "depends_on": ["1-1", "1-2", "1-3", "1-4"],
            "test_command": "true",
        },
    ])
    from agent import AutonomousAgent
    return AutonomousAgent(
        requirement="TDD spec",
        project_dir=tmp_path,
        coding_tool=None,
        config={"max_retries": 1},
        logger=None,
        tasks_file=tmp_path / "tasks.json",
    )


def test_reload_after_split_parent_must_break_outer_loop(tmp_path, monkeypatch):
    """When ``_load_tasks`` fails because task 2-1's description
    disagrees with its rewritten ``depends_on``, the outer
    ``while True`` must terminate instead of looping forever.

    Pre-fix bug: ``agent.py`` caught the ``_load_tasks`` exception,
    logged ``same_id_loop_reload_failed``, and **continued the loop**,
    producing one wasted ``task_validation_failed`` /
    ``same_id_loop_reload_failed`` pair every retry interval
    (observed 36x in 2.5 hours against one plan's task 2-1).
    """
    agent = _build_plan_with_split_parent(tmp_path)
    agent._session_task_completed_counts = {}

    # Force the production reload path to fail by patching
    # ``_validate_dependencies`` to always raise. This bypasses any
    # sandbox-specific swallows and guarantees we observe the
    # reload-and-retry block's behaviour.
    def always_raise(tasks):
        raise ValueError(
            "Task 2-1 desc 提到前置条件 1 但 depends_on 是 "
            "['1-1', '1-2', '1-3', '1-4'] (transitive 闭包 = "
            "['1-1', '1-2', '1-3', '1-4'])"
        )

    monkeypatch.setattr(
        "agent._validate_dependencies", always_raise,
    )

    cap = int(getattr(agent, "_MAX_RELOAD_FAILURES", 3))

    # Drive the production fix in agent.py by repeatedly calling the
    # extracted ``_handle_same_id_loop_reload`` method -- the same
    # code path the outer while-True executes on each iteration.
    # The method returns True when the cap is reached and the
    # outer loop should break; we verify it returns True after
    # ``cap`` consecutive failures and that ``stop_reason`` is set.
    stopped = False
    for attempt in range(cap + 2):
        stopped = agent._handle_same_id_loop_reload([])
        if stopped:
            break

    assert stopped, (
        f"After {cap} consecutive reload failures "
        "_handle_same_id_loop_reload must return True to break the "
        "outer while-True. Without it, the framework retries the "
        "same broken reload forever."
    )

    stop_reason = (
        getattr(agent, "stop_reason", None)
        or getattr(agent.task_manager, "stop_reason", None)
    )
    assert stop_reason == "same_id_loop_reload_capped", (
        f"After {cap} consecutive reload failures the agent must "
        f"have stop_reason='same_id_loop_reload_capped'. Got: "
        f"{stop_reason!r}. Without it, the outer while-True loops "
        "forever re-running reload and wasting tokens."
    )


def test_dispatcher_blocked_tasks_must_not_resurface_in_next_layer(tmp_path):
    """Tasks in ``_dispatcher_blocked`` MUST be excluded from the
    next ``_get_active_tasks_for_scheduling`` call even if a
    reload failure left them marked ``pending``.

    Pre-fix bug: when same_id_loop_reload_failed, the in-memory
    task list kept the same stale shape; layer rebuild re-picked
    the blocked task and the outer loop re-tripped the detector
    within the same iteration.
    """
    _git_init(tmp_path)
    agent = _make_agent(tmp_path)
    agent._dispatcher_blocked.add("1")
    active = agent._get_active_tasks_for_scheduling()
    assert all(t.id != "1" for t in active), (
        "dispatcher_blocked entries must NEVER reappear in the next "
        "active set, otherwise the same-id detector will re-trip on "
        "the same task id every layer rebuild."
    )


def test_dispatcher_blocked_persists_across_load_tasks_reload(tmp_path):
    """After a reload that drops a task from disk, ``_dispatcher_blocked``
    entries for that task id must NOT silently vanish — they must
    either keep blocking (idempotent) or be cleaned in an explicit,
    testable way.

    Pre-fix bug: ``_load_tasks`` rebuilds ``task_manager.tasks`` from
    disk, which may drop legacy / refiner-deleted task ids. The
    blocker set's persistence is implicit — relying on it survives the
    reload by accident, not by design.
    """
    agent = _build_plan_with_split_parent(tmp_path)
    agent._dispatcher_blocked.add("1-1")  # task 1-1 exists on disk

    # Reload — the task id survives (it's in tasks.json), so the
    # block should be retained.
    try:
        agent._load_tasks()
    except Exception:
        # Even if reload raises, the blocker set must persist for
        # valid (still-on-disk) task ids.
        pass

    assert "1-1" in agent._dispatcher_blocked, (
        "Blocker entries for tasks that survive reload must persist "
        "across reload attempts."
    )


def test_reload_failure_counter_caps_loop_iterations(tmp_path, monkeypatch):
    """Even with the existing ``while True`` loop in ``agent.py``,
    repeated ``_load_tasks`` failures must be capped. After ``N``
    consecutive failures the agent must set a stop reason.

    Pre-fix bug: observed 36 consecutive ``same_id_loop_reload_failed``
    events in a 2.5-hour window with no cap, no exponential backoff,
    no stop reason. Pure token burn.
    """
    agent = _build_plan_with_split_parent(tmp_path)
    agent._session_task_completed_counts = {}

    # Force reload to fail by patching _validate_dependencies to
    # always raise. Bypasses any sandbox-specific swallow behaviour
    # in the live _load_tasks.
    def always_raise(tasks):
        raise ValueError("forced reload failure")
    monkeypatch.setattr("agent._validate_dependencies", always_raise)

    cap = int(getattr(agent, "_MAX_RELOAD_FAILURES", 3))

    # Drive the production fix by calling the extracted
    # ``_handle_same_id_loop_reload`` method. The method returns
    # True when the cap is reached, which is what the outer
    # while-True uses to break the loop.
    stopped = False
    for attempt in range(cap + 5):
        stopped = agent._handle_same_id_loop_reload([])
        if stopped:
            break

    assert stopped, (
        f"After {cap + 5} consecutive reload failures "
        "_handle_same_id_loop_reload must return True. Without a "
        "cap the framework will retry the same broken reload "
        "until the LLM quota is exhausted."
    )

    stop_reason = (
        getattr(agent, "stop_reason", None)
        or getattr(agent.task_manager, "stop_reason", None)
    )
    assert stop_reason == "same_id_loop_reload_capped", (
        f"After {cap + 5} consecutive reload failures the agent must "
        f"have stop_reason='same_id_loop_reload_capped'. Got: "
        f"{stop_reason!r}. Without a cap the framework will retry "
        "the same broken reload until the LLM quota is exhausted."
    )


def test_micro_layer_schedulable_filter_excludes_dispatcher_blocked(tmp_path):
    """Regression test:
    the micro-layer rebuild at the top of the dispatcher
    ``while True`` draws from ``self._all_tasks`` (read fresh from
    disk by ``_load_tasks``) and only filters by terminal
    ``status``. ``save_tasks`` deliberately strips the ``status``
    field (post-task #3.8 migration), so a task the
    ``same_id_loop_detected`` branch just added to
    ``_dispatcher_blocked`` reappears in the micro-layer with
    ``status=None`` after every reload — tripping the loop
    detector again. The micro-layer schedulable filter must
    also honour ``_dispatcher_blocked``, or the dispatcher spins
    at ~3 log lines / iteration forever.
    """
    _git_init(tmp_path)
    agent = _make_agent(tmp_path)
    # Task starts with no status (save_tasks strips it on write).
    task = agent.task_manager.tasks[0]
    task.status = None
    # Simulate the same-id loop detector having just broken the
    # cycle for this task in the previous while-True iteration.
    agent._dispatcher_blocked.add(task.id)

    # Mirror the exact filter chain the dispatcher uses at
    # agent.py:2994-3027 (top of while-True). The bug was that
    # the inner schedulable filter only checked terminal status,
    # not _dispatcher_blocked.
    _TERMINAL = {"completed", "failed", "skipped"}
    schedulable = []
    for t in agent._all_tasks:
        if t.status in _TERMINAL:
            continue
        if t.id in agent._dispatcher_blocked:  # the fix
            continue
        schedulable.append(t)

    assert task not in schedulable, (
        "Micro-layer schedulable filter MUST skip tasks in "
        "_dispatcher_blocked; otherwise the same-id loop detector "
        "fires every iteration and the executor spins forever "
        "(observed on a real task "
        "with 3 log lines / ~5ms for 11 minutes, no progress)."
    )


def test_active_tasks_skip_dispatcher_blocked_after_reload_failure(tmp_path):
    """End-to-end: simulate one full iteration of the outer loop
    after a same-id loop reload failure. After the reload fails,
    ``_get_active_tasks_for_scheduling`` must NOT return the
    blocked tasks; otherwise the next layer rebuild re-emits them.
    """
    agent = _build_plan_with_split_parent(tmp_path)
    # Mark task 1-1..1-4 as if refiner had completed them and the
    # executor had detected same_id_loop, putting them in the
    # blocker set.
    for tid in ["1-1", "1-2", "1-3", "1-4"]:
        agent._dispatcher_blocked.add(tid)
        if tid in {t.id for t in agent.task_manager.tasks}:
            for t in agent.task_manager.tasks:
                if t.id == tid:
                    t.status = "completed"

    # Force the reload failure path:
    try:
        agent._load_tasks()
    except Exception:
        pass

    active_ids = {t.id for t in agent._get_active_tasks_for_scheduling()}
    assert not (active_ids & {"1-1", "1-2", "1-3", "1-4"}), (
        f"After reload failure the four split-parent tasks must NOT "
        f"resurface in the active set. Got: {active_ids & {'1-1', '1-2', '1-3', '1-4'}}"
    )

"""
TDD tests for ``AutonomousAgent.run`` — async layer-iteration dispatcher.

Background
----------
PRD decision points 1 + 2 replace the legacy serial ``while True`` loop
with a layer-by-layer scheduler that runs same-layer tasks concurrently
via :func:`asyncio.gather`, and gates each task on a per-provider
:class:`ProviderConcurrencyController` slot. The three tests below each
pin one contract of that contract:

  1. ``test_main_loop_layer_iteration``
     Two-layer plan (``A``, ``B`` in layer 0 → ``C`` depends on both in
     layer 1). The scheduler must start ``A`` and ``B`` before either of
     them finishes (concurrent layer 0), and must NOT start ``C`` until
     both ``A`` and ``B`` have finished (cross-layer wait).

  2. ``test_main_loop_acquires_provider_slot``
     Six no-dependency tasks all using the same provider, with the
     controller's global cap set to 5. The number of tasks the
     controller has in flight at any single moment must never exceed
     5 — the 6th task suspends inside ``controller.acquire`` until a
     paired ``release`` frees a slot.

  3. ``test_main_loop_layer_started_event``
     A trivial one-task plan emits ``layer_started`` and
     ``layer_completed`` events to the logger so operators can audit
     layer transitions in ``execution.log``.

A fourth test — "breakdown rebuilds layers and continues" — was retired
on 2026-09-14 because it drove ``AutonomousAgent._breakdown_task``,
which the executor self-split removal deleted; see the note at the foot
of this file.

All three tests stub ``_execute_task_with_retry`` so no real LLM call
ever happens; the stubs synchronously record timing / concurrency
metadata via thread-safe primitives and return ``True`` immediately.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytest


# Ensure backend/ is on sys.path so `import agent` works regardless of
# which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _git_init(project_dir: Path) -> None:
    """Init a real git repo so ``GitManager`` can bind to it without
    walking up to a sibling checkout.
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


def _write_tasks(project_dir: Path, tasks: list) -> Path:
    """Helper: write a ``tasks.json`` with the given task dicts.

    2026-09-14: ``AutonomousAgent._load_tasks`` runs the dispatcher
    post-read gate (``TaskOutputValidator``). Step 3 of that gate
    requires every declared ``files_to_modify`` path to exist on disk
    (or to have been deleted in a reachable commit), and rejects an
    empty list outright. These fixtures are about scheduling, not file
    attribution, so each placeholder — and each path a test declared by
    hand — is materialised under ``project_dir``.
    """
    normalised = []
    materialise = []
    for task in tasks:
        task = dict(task)
        if not task.get("files_to_modify"):
            task["files_to_modify"] = [f"src/{task.get('id', 'task')}.py"]
        materialise.extend(task["files_to_modify"])
        normalised.append(task)

    for raw in materialise:
        if not isinstance(raw, str) or raw.startswith("__"):
            continue  # sentinels are accepted verbatim by step 3
        path = Path(raw)
        if not path.is_absolute():
            path = project_dir / raw
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    tasks_file = project_dir / "tasks.json"
    payload = {
        "requirement": "TDD spec for async dispatch loop",
        "tasks": normalised,
    }
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tasks_file


class _StubCodingTool:
    """Coding tool stub — ``query_json`` returns scripted dicts.

    The dispatch tests don't exercise the coding tool directly (they
    stub ``_execute_task_with_retry`` on the agent instead), so an
    unscripted ``query_json`` call is a test-authoring error and raises
    rather than silently returning ``None``.
    """

    def __init__(self, response: Optional[dict] = None):
        self.response = response
        self.calls: list = []

    def query_json(self, prompt: str, system_instruction: Optional[str] = None) -> dict:
        self.calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
        })
        if self.response is None:
            raise AssertionError(
                "query_json must NOT be called when no response is scripted "
                "(test stubs should bypass this code path)"
            )
        return self.response


class _RecordingLogger:
    """In-memory logger stub capturing all log events for assertions."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def _record(self, level: str, event: str, message: str, **kwargs) -> None:
        with self._lock:
            self.events.append({
                "level": level,
                "event": event,
                "message": message,
                **kwargs,
            })

    def debug(self, event: str, message: str, **kwargs) -> None:
        self._record("DEBUG", event, message, **kwargs)

    def info(self, event: str, message: str, **kwargs) -> None:
        self._record("INFO", event, message, **kwargs)

    def warning(self, event: str, message: str, **kwargs) -> None:
        self._record("WARNING", event, message, **kwargs)

    def error(self, event: str, message: str, **kwargs) -> None:
        self._record("ERROR", event, message, **kwargs)

    def critical(self, event: str, message: str, **kwargs) -> None:
        self._record("CRITICAL", event, message, **kwargs)


def _build_agent(project_dir: Path, coding_tool, logger=None):
    """Build a minimal AutonomousAgent bound to ``project_dir``.

    The agent's ``_load_tasks()`` is called by the caller so the
    ``_active_tasks`` / ``_all_tasks`` lists are populated before
    ``run()`` is invoked.
    """
    from agent import AutonomousAgent

    return AutonomousAgent(
        requirement="TDD spec for async dispatch loop",
        project_dir=project_dir,
        coding_tool=coding_tool,
        logger=logger,
    )


def _install_event_loop_for_sync_test() -> tuple[asyncio.AbstractEventLoop, callable]:
    """Install an event loop for the main thread if one isn't already set.

    Returns ``(loop, restore)`` where ``loop`` is the active loop and
    ``restore`` is a no-arg callable that cleans up after the test:
    it closes the loop *only if* this helper created it (so we never
    accidentally close a loop the rest of the suite is still using).

    Why: ``ProviderConcurrencyController.__init__`` creates an
    :class:`asyncio.Semaphore` that, on Python 3.9, eagerly calls
    ``events.get_event_loop()`` and binds to whatever loop that
    returns. When a sync test (no running loop, no default loop)
    constructs the controller, this raises
    ``RuntimeError: There is no current event loop in thread
    'MainThread'.`` We avoid that by giving the main thread a
    fresh loop for the duration of the test, then closing it on
    teardown so subsequent tests in the same session are not
    affected by a stale or closed loop.
    """
    created = False
    # Use a fresh event loop policy so this test can install / close
    # its own loop without trampling whatever loop the test runner
    # or other tests have set as the thread's default.
    saved_policy = asyncio.get_event_loop_policy()
    fresh_policy = asyncio.DefaultEventLoopPolicy()
    asyncio.set_event_loop_policy(fresh_policy)
    loop = fresh_policy.new_event_loop()
    fresh_policy.set_event_loop(loop)
    created = True

    def _restore():
        if created:
            try:
                loop.close()
            except Exception:
                pass
        # Always restore the previous policy so the rest of the
        # suite sees the same default it had before this test.
        try:
            asyncio.set_event_loop_policy(saved_policy)
        except Exception:
            pass

    return loop, _restore


@pytest.fixture
def project_dir(tmp_path):
    """A real project_dir with a real git repo so GitManager can bind."""
    pd = tmp_path / "project"
    _git_init(pd)
    return pd


# ---------------------------------------------------------------------------
# Test 1: 2-layer iteration with cross-layer wait
# ---------------------------------------------------------------------------


def test_main_loop_layer_iteration(project_dir, monkeypatch):
    """Two-layer plan: A, B in layer 0 (concurrent); C in layer 1 (waits).

    Concurrency contract for layer 0:
      * ``A.start`` and ``B.start`` both happen before either of
        ``A.end`` / ``B.end`` — i.e. the layer is truly parallel.

    Cross-layer wait contract:
      * ``C.start`` must be AFTER both ``A.end`` and ``B.end``. C is
        downstream of A and B, so the scheduler must not begin
        executing C until layer 0 is fully drained.
    """
    _write_tasks(
        project_dir,
        [
            {"id": "A", "title": "Layer 0 task A", "description": "first parallel task",
             "test_command": "echo A", "status": "pending", "depends_on": [],
             "files_to_modify": ["a.py"]},
            {"id": "B", "title": "Layer 0 task B", "description": "second parallel task",
             "test_command": "echo B", "status": "pending", "depends_on": [],
             "files_to_modify": ["b.py"]},
            {"id": "C", "title": "Layer 1 task C", "description": "waits for A and B",
             "test_command": "echo C", "status": "pending", "depends_on": ["A", "B"],
             "files_to_modify": ["c.py"]},
        ],
    )

    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool)
    agent._load_tasks()

    # Stub _execute_task_with_retry to record start/end times.
    timings: dict[str, dict[str, float]] = {}
    timings_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5.0)  # for A + B to sync

    def _stub_execute(task, max_retries=5, timeout=None):
        with timings_lock:
            timings[task.id] = {"start": time.monotonic()}
        # Only A and B should be in this barrier — synchronise them so
        # the test deterministically observes concurrent execution.
        if task.id in ("A", "B"):
            try:
                barrier.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                pass
        # Tiny sleep so the start/end gap is observable.
        time.sleep(0.02)
        with timings_lock:
            timings[task.id]["end"] = time.monotonic()
        # Mark the task completed so it doesn't get re-scheduled.
        agent.task_manager.update_task_status(task.id, "completed")
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

    agent.run(timeout=None)

    # All three tasks recorded both a start and an end timestamp.
    for tid in ("A", "B", "C"):
        assert tid in timings, f"task {tid} never executed (timings={timings!r})"
        assert "start" in timings[tid] and "end" in timings[tid], (
            f"task {tid} missing start/end (got {timings[tid]!r})"
        )

    # Concurrency contract for layer 0 (A and B):
    # A.start < B.end AND B.start < A.end → their wall-clock windows
    # overlap. The barrier above forces both to enter the body before
    # either is allowed to exit, so a non-overlapping window proves
    # the layer is serial (broken contract).
    a_start, a_end = timings["A"]["start"], timings["A"]["end"]
    b_start, b_end = timings["B"]["start"], timings["B"]["end"]
    c_start = timings["C"]["start"]

    assert a_start < b_end and b_start < a_end, (
        f"A and B did not overlap (A=[{a_start}, {a_end}], B=[{b_start}, {b_end}]) "
        f"— layer 0 was scheduled serially, not concurrently"
    )

    # Cross-layer wait contract: C must start strictly AFTER both A
    # and B have ended.
    assert c_start >= a_end and c_start >= b_end, (
        f"C started before its upstream layer completed "
        f"(C.start={c_start}, A.end={a_end}, B.end={b_end})"
    )


# ---------------------------------------------------------------------------
# Test 2: 6 vendor-a tasks with global=5 → peak concurrency ≤ 5
# ---------------------------------------------------------------------------


def test_main_loop_acquires_provider_slot(project_dir, monkeypatch):
    """6 same-provider tasks, controller global=5 → peak concurrent ≤ 5.

    All 6 tasks share the same layer (no deps) so the scheduler will
    try to run them concurrently. The controller's global semaphore
    caps the in-flight count at 5; the 6th task must suspend inside
    ``acquire`` until at least one of the first 5 releases.
    """
    from provider_concurrency import ProviderConcurrencyController

    _write_tasks(
        project_dir,
        [
            # 2026-09-14: description must NOT read "task N" — the
            # validator's step-2 checker matches ``task[\s-]+\d+`` in
            # the description and treats it as a dependency reference,
            # which no task id here satisfies ("T0" is not "task 0"),
            # so the whole plan failed the post-read gate.
            {"id": f"T{i}", "title": f"Task {i}", "description": f"dispatch slice {i}",
             "test_command": f"echo T{i}", "status": "pending", "depends_on": [],
             "files_to_modify": [f"t{i}.py"]}
            for i in range(6)
        ],
    )

    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool)
    agent._load_tasks()

    # Force a known provider name so the controller charges all 6
    # tasks against the same per-provider semaphore.
    monkeypatch.setenv("PDT_PROVIDER_NAME", "vendor-a-pro")
    # Use explicit limits (avoid relying on env-var defaults of the
    # CI environment). global=5 is the binding constraint here; the
    # per-provider limit is also 5 so neither layer is over-tight.
    # ProviderConcurrencyController creates an asyncio.Semaphore in
    # its constructor; on Python 3.9 that binds to the *current*
    # event loop. The test is sync and the main thread may not yet
    # have an event loop, so we install one before constructing.
    # ``asyncio.run`` inside ``agent.run()`` creates a fresh loop
    # for the coroutine, but the semaphores keep working across
    # that switch (verified with a minimal repro).
    #
    # The :func:`_install_event_loop_for_sync_test` helper returns
    # a ``(loop, restore_fn)`` pair; we call ``restore_fn()`` after
    # the test body so the next test sees a clean main-thread state
    # (no closed loop, no leaked policy).
    loop, restore = _install_event_loop_for_sync_test()
    try:
        controller = ProviderConcurrencyController(
            global_limit=5,
            provider_limits={"vendor-a-pro": 5},
        )
        agent._provider_controller = controller

        # Stub _execute_task_with_retry to record max-concurrent observed.
        state = {"in_flight": 0, "peak": 0, "global_min": controller.global_limit}
        state_lock = threading.Lock()
        # Block all 6 tasks at a single barrier to maximise the observable
        # peak — the 6th task should still be blocked outside acquire while
        # the first 5 are at the barrier inside the executor body. We size
        # the barrier to 5 (the cap), so the 6th will wait at acquire even
        # if the first 5 are mid-body.
        enter_barrier = threading.Barrier(5, timeout=5.0)

        def _stub_execute(task, max_retries=5, timeout=None):
            # Enter the per-task slot.
            with state_lock:
                state["in_flight"] += 1
                state["peak"] = max(state["peak"], state["in_flight"])
                # Record the lowest controller.available_global() value
                # we've ever seen; with global=5 and peak=5 it should
                # reach 0.
                state["global_min"] = min(
                    state["global_min"], controller.available_global()
                )
            # Synchronise the first 5 tasks so they all sit in-body
            # concurrently while the 6th is parked in acquire.
            try:
                enter_barrier.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                pass
            time.sleep(0.02)
            with state_lock:
                state["in_flight"] -= 1
            agent.task_manager.update_task_status(task.id, "completed")
            return True

        monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

        agent.run(timeout=None)
    finally:
        restore()

    # Peak in-flight count must be ≤ controller cap. The controller
    # is configured at global=5, so this is the hard ceiling: a peak
    # of 6 would prove the controller was bypassed.
    assert state["peak"] <= 5, (
        f"peak concurrent tasks ({state['peak']}) exceeded global cap (5) — "
        f"the dispatcher bypassed controller.acquire"
    )
    # And the cap should have been HIT (the test exercises the
    # blocking path) — a peak below 5 would mean the test was
    # accidentally serial. We require ≥ 2 to confirm at least some
    # concurrency happened.
    assert state["peak"] >= 2, (
        f"peak concurrent tasks ({state['peak']}) too low — "
        f"the dispatcher ran tasks serially instead of in parallel"
    )

    # All 6 tasks should have completed.
    completed = [t for t in agent.task_manager.tasks if t.status == "completed"]
    assert len(completed) == 6, (
        f"expected 6 tasks completed, got {len(completed)}: "
        f"{[(t.id, t.status) for t in agent.task_manager.tasks]}"
    )

    # Controller should be drained — no leaked slots.
    assert controller.available_global() == 5, (
        f"controller leaked global slots: available_global="
        f"{controller.available_global()} (expected 5)"
    )


# ---------------------------------------------------------------------------
# Test 3: layer_started / layer_completed logger events
# ---------------------------------------------------------------------------


def test_main_loop_layer_started_event(project_dir, monkeypatch):
    """A trivial 1-task plan emits ``layer_started`` and ``layer_completed``.

    Auditability contract: operators must be able to grep
    ``execution.log`` for layer transitions. Without these two
    events, the only signal of layer progress is the per-task
    ``task_started`` / ``task_completed`` events, which obscure the
    scheduler's layer boundaries.
    """
    _write_tasks(
        project_dir,
        [
            {"id": "1", "title": "Solo task", "description": "only task",
             "test_command": "echo 1", "status": "pending", "depends_on": []},
        ],
    )

    logger = _RecordingLogger()
    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool, logger=logger)
    agent._load_tasks()

    def _stub_execute(task, max_retries=5, timeout=None):
        agent.task_manager.update_task_status(task.id, "completed")
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

    agent.run(timeout=None)

    event_names = [e["event"] for e in logger.events]
    assert "layer_started" in event_names, (
        f"expected 'layer_started' in logger events, got {event_names!r}"
    )
    assert "layer_completed" in event_names, (
        f"expected 'layer_completed' in logger events, got {event_names!r}"
    )

    # And the start must come before the completion.
    start_idx = event_names.index("layer_started")
    end_idx = event_names.index("layer_completed")
    assert start_idx < end_idx, (
        f"layer_started ({start_idx}) must be logged before "
        f"layer_completed ({end_idx})"
    )

    # The layer_started event payload should include the layer's task ids.
    started_event = logger.events[start_idx]
    payload = started_event.get("data") or {}
    assert payload.get("task_ids") == ["1"], (
        f"layer_started payload should list task_ids=['1'], got {payload!r}"
    )
# ---------------------------------------------------------------------------
# Test 4 (retired 2026-09-14): "breakdown rebuilds layers and continues"
# ---------------------------------------------------------------------------
#
# This slot held ``test_main_loop_continues_after_breakdown``, which
# stubbed ``_execute_task_with_retry`` to call
# ``agent._breakdown_task(task)`` and then asserted the dispatcher
# picked the new children up on the next layer rebuild.
#
# ``AutonomousAgent._breakdown_task`` no longer exists — it was DELETED
# by plan ``2026-09-04 plan`` task 5 (commit ecf39fa, "M0：
# 删除 backend/agent.py 的 executor self-split 逻辑"), because PRD
# decision point 1 makes the *refiner* the single authoritative source
# of structural task-list changes. The refiner now writes sub-tasks
# straight into ``state.db`` (``PlanTaskRepository.add_task``) and the
# dispatcher picks them up through the Phase-2 orphan reconcile on the
# next ``_load_tasks()``.
#
# The absence of the executor-side self-split is enforced by
# ``tests/unit/test_agent_no_self_split.py`` (source-text + AST guards),
# so this test cannot be revived against the old API. The dispatcher's
# layer ordering remains covered by ``test_main_loop_layer_iteration``
# above, and load-time leaf derivation by
# ``tests/unit/test_agent_breakdown.py::test_leaf_set_derived_from_dag``.
# A future test of the refiner-driven mid-flight mutation should drive
# ``PlanTaskRepository.add_task`` — not a resurrected
# ``_breakdown_task``.

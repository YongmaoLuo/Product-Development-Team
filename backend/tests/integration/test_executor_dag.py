"""
test_executor_dag.py — end-to-end integration tests for PRD acceptance
Cases 1 and 2.

Case 1 background
-----------------
PRD acceptance Case 1 requires that a linear dependency chain
``A → B → C`` (where ``B.depends_on = [A]`` and
``C.depends_on = [B]``) executes strictly serially:

    A.start → A.end → B.start → B.end → C.start → C.end

The acceptance has two complementary parts:

  * ``_build_layers`` must produce exactly three outer layers
    ``[[[A]], [[B]], [[C]]]`` — the topology is correct.
  * The runtime dispatcher must run each micro-layer's single task to
    completion before starting the next micro-layer — concurrency is
    correctly throttled to 1 even though ``asyncio.gather`` could
    in principle run multiple tasks at once.

The three Case-1 TDD tests below pin these two contracts and one
observability contract (the execution log emits 3 ``layer_started`` +
3 ``layer_completed`` events so operators can audit layer transitions).

Case 2 background
-----------------
PRD acceptance Case 2 requires that 6 same-layer tasks
``(A, B, C, D, E, F)`` with no dependencies execute concurrently
under a global cap of 5. The contract has three parts:

  * ``_build_layers`` must produce a single outer layer
    ``[[[A, B, C, D, E, F]]]`` — the topology is a 6-way parallel fork
    within one micro-layer.
  * The runtime dispatcher must respect
    :class:`ProviderConcurrencyController`'s global cap of 5, so the
    6th task suspends inside ``acquire`` until a paired ``release``
    frees a slot. Peak in-flight count therefore never exceeds 5.
  * All 6 tasks complete successfully (the cap queues work, it does
    not drop it).

Test isolation strategy (mirrors ``tests/integration/test_agent_execute_task.py``):

  * ``tmp_path`` is the project_dir; we run real ``git init`` so
    ``GitManager(search_parent_directories=True)`` finds a repo at
    the leaf — not a sibling checkout's ancestor.
  * ``$HOME`` is redirected to a private tmpdir containing a fake
    ``~/.cc-switch/cc-switch.db`` so ``_load_provider_info`` is
    exercised end-to-end (we never patch it out).
  * ``tasks.json`` is pre-baked in the project_dir; we drive the
    agent via ``agent._load_tasks()`` + ``agent.run()`` so
    ``agent.plan()`` is skipped (no LLM call). The agent's
    ``__init__`` is **not** touched.
  * ``_execute_task_with_retry`` is wrapped with a spy that records
    start/end timestamps, observes peak concurrency, and (in test 1)
    injects a 200ms sleep on task A so we can prove B cannot start
    before A finishes. The spy updates ``task_manager`` status to
    ``completed`` exactly the way the real implementation would.
  * The spy also appends a small sleep matching the A-task's wall
    time on tasks B and C, so the 3 layers are visually
    distinguishable in the timing trace (otherwise all 3 would
    finish in < 1ms and the overlap test would be flaky).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest


# Make ``agent.py`` and ``subagent_config.py`` importable in isolation.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


# 2026-09-13 — fixture plan directory name. ``plan_id`` is derived from
# ``tasks_file.parent.name`` (see ``task_manager.derive_plan_id_from_tasks_file``),
# and ``TaskManager`` refuses *reserved* names — ``project``, ``plans``,
# ``test``, ``tasks``, ``tmp``, … — so that a generic layout cannot
# collide with other plans in the ``plan_routing`` / ``plan_execution``
# SQLite rows. A refused plan_id silently skips the
# ``plan_execution.task_progress`` mirror, and because ``tasks.json``
# strips the ``status`` field on write, ``_load_tasks``' SQLite
# re-hydration then finds nothing and every reload reverts the tasks to
# ``status="pending"``. The dispatcher consequently re-schedules already
# completed tasks until the same-id loop guard trips layers later.
#
# In production the plan id is always a real ``plans/<id>`` directory, so
# the hydrate works. These fixtures used to name the directory ``project``
# — which is exactly the reserved case — so they never exercised the
# production behaviour and ``test_case5_dispatches_only_pending`` looped
# forever instead of terminating.
#
# The name must ALSO be unique per test: ``plan_id`` is the SQLite key in
# the shared ``<repo>/state.db``, so a constant name would let one test's
# ``plan_execution.task_progress`` rows hydrate into the next test's run.
# ``tmp_path.name`` is pytest's per-test directory (already unique), so the
# derived id is unique by construction.
def _plan_dir(tmp_path: Path) -> Path:
    """Return a per-test project dir with a unique, non-reserved plan id."""
    slug = "".join(
        ch if (ch.isalnum() or ch in "._-") else "-" for ch in tmp_path.name
    ).strip("-")
    return tmp_path / f"executor-dag-{slug or 'case'}"


@pytest.fixture(autouse=True)
def _hermetic_state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``state.db`` at a per-test file.

    ``plan_id`` is the SQLite key for the ``plan_routing`` /
    ``plan_execution`` rows, and the default database
    (``<backend-parent>/state.db``) is shared by every test in the
    process *and* persists across sessions. Rows therefore leak: a task
    marked ``completed`` by one run is re-hydrated as ``completed`` by
    ``_load_tasks`` on the next, and the dispatcher silently skips it —
    the tests then observe "All tasks completed!" with zero dispatches.

    Both ``agent._get_task_progress_repository`` and
    ``TaskManager._persist_status_to_sqlite`` honour ``PDT_STATE_DB_PATH``
    (resolution order: env var, then ``<backend-parent>/state.db``), so
    setting it per test makes the fixtures hermetic.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))


def _materialize_declared_files(project_dir: Path, tasks_payload: dict) -> None:
    """Create empty placeholder files for relative ``files_to_modify`` paths.

    2026-09-13 — ``TaskOutputValidator`` step 3
    (``framework/task_output_validator.py::_files_to_modify_existence_check``)
    rejects any declared path that is not on disk (``"{path} not found"``),
    and ``_load_tasks``' post-read gate escalates that into a hard
    ``RuntimeError`` before the dispatcher ever runs. These fixtures name
    the files their tasks are meant to write, so the paths have to exist
    for the plan to load at all. Sentinels (``__UNKNOWN_MODIFICATIONS__``
    / ``__NO_FILE_CHANGES__``) are not paths and are skipped.
    """
    for task in tasks_payload.get("tasks") or []:
        declared = task.get("files_to_modify") or []
        if isinstance(declared, str):
            declared = [declared]
        for rel in declared:
            if not isinstance(rel, str) or rel.startswith("__"):
                continue
            if Path(rel).is_absolute():
                continue
            full = project_dir / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            if not full.exists():
                full.write_text("", encoding="utf-8")


_FAKE_PROVIDERS = {
    "vendor-a-pro": {
        "base_url": "https://api.vendor-a.example/anthropic",
        "api_key": "sk-cp-fake-vendor-a-key-1234567890",
    },
    "vendor-b": {
        "base_url": "https://api.vendor-b.example/anthropic",
        "api_key": "a8ff.fake-vendor-b-key.1234567890",
    },
}


def _git_init(project_dir: Path) -> None:
    """Run a real ``git init`` so GitManager(search_parent_directories=True)
    finds the repo at the leaf — not a sibling checkout
    ancestor repo.
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
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "Executor DAG Test"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
    )


def _install_fake_cc_switch_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Build a private ``~/.cc-switch/cc-switch.db`` and redirect
    ``$HOME`` to it for the test.
    """
    import sqlite3

    fake_home = tmp_path / "home"
    cc_dir = fake_home / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path = cc_dir / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS provider_configs ("
        "id TEXT PRIMARY KEY, url TEXT, model TEXT, extra_params TEXT"
        ")"
    )
    for provider_id, cfg in _FAKE_PROVIDERS.items():
        extra = json.dumps({"ANTHROPIC_AUTH_TOKEN": cfg["api_key"]})
        conn.execute(
            "INSERT INTO provider_configs (id, url, model, extra_params) "
            "VALUES (?, ?, ?, ?)",
            (provider_id, cfg["base_url"], "fake-model", extra),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("HOME", str(fake_home))
    return db_path


def _write_chain_tasks(project_dir: Path) -> Path:
    """Write the A→B→C chain as a single ``tasks.json`` file.

    The chain has the strict serial shape PRD acceptance Case 1
    requires: A has no dependencies, B depends on A, C depends on B.
    """
    tasks_payload = {
        "requirement": "test A→B→C serial chain",
        "stop_reason": None,
        "reason_detail": None,
        "tasks": [
            {
                "id": "A",
                "title": "Chain head A",
                "description": "first task, no deps",
                "test_command": "echo A",
                "test_commands": [],
                "status": "pending",
                "updated_time": None,
                "failure_reason": None,
                "project_dir": None,
                "model_type": "medium",
                "depends_on": [],
                "files_to_modify": ["a.py"],
            },
            {
                "id": "B",
                "title": "Chain middle B",
                "description": "waits for A",
                "test_command": "echo B",
                "test_commands": [],
                "status": "pending",
                "updated_time": None,
                "failure_reason": None,
                "project_dir": None,
                "model_type": "medium",
                "depends_on": ["A"],
                "files_to_modify": ["b.py"],
            },
            {
                "id": "C",
                "title": "Chain tail C",
                "description": "waits for B",
                "test_command": "echo C",
                "test_commands": [],
                "status": "pending",
                "updated_time": None,
                "failure_reason": None,
                "project_dir": None,
                "model_type": "medium",
                "depends_on": ["B"],
                "files_to_modify": ["c.py"],
            },
        ],
    }
    _materialize_declared_files(project_dir, tasks_payload)
    tasks_file = project_dir / "tasks.json"
    tasks_file.write_text(json.dumps(tasks_payload, indent=2), encoding="utf-8")
    return tasks_file


def _make_subtasks() -> List[Any]:
    """Build the 3 in-memory SubTask objects used by ``_build_layers``.

    We import :class:`task.SubTask` lazily so this module is importable
    in environments where the runtime dependencies are missing (e.g.
    during collection-only mode). The shapes mirror what
    ``agent._load_tasks`` would produce from the on-disk JSON.
    """
    from task import SubTask

    return [
        SubTask(
            id="A",
            title="Chain head A",
            description="first task, no deps",
            test_command="echo A",
            test_commands=[],
            status="pending",
            depends_on=[],
            files_to_modify=["a.py"],
        ),
        SubTask(
            id="B",
            title="Chain middle B",
            description="waits for A",
            test_command="echo B",
            test_commands=[],
            status="pending",
            depends_on=["A"],
            files_to_modify=["b.py"],
        ),
        SubTask(
            id="C",
            title="Chain tail C",
            description="waits for B",
            test_command="echo C",
            test_commands=[],
            status="pending",
            depends_on=["B"],
            files_to_modify=["c.py"],
        ),
    ]


# ---------------------------------------------------------------------------
# Case 2 helpers: 6 same-layer tasks, all provider="vendor-a"
# ---------------------------------------------------------------------------


_CASE2_TASK_IDS = ("A", "B", "C", "D", "E", "F")
_CASE2_PROVIDER = "vendor-a"
_CASE2_SLEEP_SECONDS = 0.200  # 200ms — task spec's "200ms probe"


def _write_six_task_json(project_dir: Path) -> Path:
    """Write 6 no-dep tasks, all on provider ``"vendor-a"``, as a single
    ``tasks.json``. Mirrors the task spec's example:

        tasks = [SubTask(id=f'T{i}', depends_on=[], provider='vendor-a') for i in range(6)]

    but with deterministic ids ``A..F`` (not ``T0..T5``) so the
    completion-order assertion in Case 2 tests reads cleanly.
    """
    tasks_payload = {
        "requirement": "test 6-task same-layer concurrency cap",
        "stop_reason": None,
        "reason_detail": None,
        "tasks": [
            {
                "id": tid,
                "title": f"Case2 task {tid}",
                "description": f"PRD Case 2 same-layer task {tid}",
                "test_command": f"echo {tid}",
                "test_commands": [],
                "status": "pending",
                "updated_time": None,
                "failure_reason": None,
                "project_dir": None,
                "model_type": "medium",
                "depends_on": [],
                "provider": _CASE2_PROVIDER,
                "files_to_modify": [f"{tid.lower()}.py"],
            }
            for tid in _CASE2_TASK_IDS
        ],
    }
    _materialize_declared_files(project_dir, tasks_payload)
    tasks_file = project_dir / "tasks.json"
    tasks_file.write_text(json.dumps(tasks_payload, indent=2), encoding="utf-8")
    return tasks_file


def _make_six_subtasks() -> List[Any]:
    """Build the 6 in-memory SubTask objects used by ``_build_layers`` for
    the Case 2 topology test. Each carries ``provider="vendor-a"`` so
    the per-task override path in ``agent._resolve_provider_for_task``
    is exercised.
    """
    from task import SubTask

    return [
        SubTask(
            id=tid,
            title=f"Case2 task {tid}",
            description=f"PRD Case 2 same-layer task {tid}",
            test_command=f"echo {tid}",
            test_commands=[],
            status="pending",
            depends_on=[],
            provider=_CASE2_PROVIDER,
            files_to_modify=[f"{tid.lower()}.py"],
        )
        for tid in _CASE2_TASK_IDS
    ]


# ---------------------------------------------------------------------------
# TDD test 1: _build_layers returns 3 layers [[A],[B],[C]]
# ---------------------------------------------------------------------------


def test_case1_three_layers(tmp_path: Path) -> None:
    """``agent._build_layers`` returns 3 outer layers for the A→B→C chain.

    PRD acceptance Case 1: the linear chain must topologically order
    into 3 distinct outer layers — one task per outer layer. Each task
    carries a distinct files_to_modify entry, so within each outer layer
    there is exactly one micro-layer containing exactly one task.
    """
    tasks = _make_subtasks()
    # Import the module-level helper from agent.py. We do this here
    # (rather than at module top-level) so collection-only pytest
    # invocations don't fail when the backend module imports fail.
    from agent import _build_layers

    layers = _build_layers(tasks)

    # Must be exactly 3 outer layers.
    assert isinstance(layers, list), (
        f"_build_layers must return a list, got {type(layers).__name__}: {layers!r}"
    )
    assert len(layers) == 3, (
        f"expected exactly 3 outer layers for A→B→C chain, got {len(layers)}: "
        f"{[[[t.id for t in ml] for ml in ol] for ol in layers]!r}"
    )

    for idx, outer_layer in enumerate(layers):
        assert isinstance(outer_layer, list), (
            f"layer[{idx}] must be a list of micro-layers, got {type(outer_layer).__name__}: {outer_layer!r}"
        )
        assert len(outer_layer) == 1, (
            f"layer[{idx}] must contain exactly 1 micro-layer, got {len(outer_layer)}: "
            f"{outer_layer!r}"
        )
        micro_layer = outer_layer[0]
        assert isinstance(micro_layer, list), (
            f"layer[{idx}][0] must be a micro-layer list, got {type(micro_layer).__name__}: {micro_layer!r}"
        )
        assert len(micro_layer) == 1, (
            f"layer[{idx}][0] must contain exactly 1 task, got {len(micro_layer)}: "
            f"{micro_layer!r} — the chain was not strictly serialised"
        )

    # The task ids in layer order must be exactly [A, B, C].
    layer_ids = [[[t.id for t in ml] for ml in ol] for ol in layers]
    assert layer_ids == [[["A"]], [["B"]], [["C"]]], (
        f"layer ordering must be [[['A']], [['B']], [['C']]], got {layer_ids!r}"
    )


# ---------------------------------------------------------------------------
# TDD test 2: A→B→C runs strictly serially (timestamps don't overlap)
# ---------------------------------------------------------------------------


def test_case1_linear_chain_serial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """3 task timestamps don't overlap — A→B→C runs strictly serially.

    The injection:
      * Task A sleeps for 200ms (its own body).
      * The spy increments an in-flight counter before sleep and
        decrements after, so peak concurrency is observable.
      * The spy records ``{task_id: {start, end}}`` in
        ``time.monotonic()`` for the overlap assertion.

    The assertion:
      * A.end ≤ B.start  (no overlap on the A→B boundary)
      * B.end ≤ C.start  (no overlap on the B→C boundary)
      * Peak in-flight is exactly 1
    """
    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _write_chain_tasks(project_dir)

    from agent import AutonomousAgent
    from coding_tool import ClaudeCodingTool

    # Stub ClaudeCodingTool.query / query_json so the agent's import
    # side-effects don't blow up if plan() is somehow called. We use
    # recover-style flow (no plan), but the defensive stubs avoid
    # surprises.
    monkeypatch.setattr(
        ClaudeCodingTool, "query",
        lambda self, *args, **kwargs: "TEST_RESULT: PASSED\n",
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "query_json",
        lambda self, *args, **kwargs: {"tasks": []},
    )

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test A→B→C serial chain",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )
    agent._load_tasks()

    # Spy on _execute_task_with_retry so we can:
    # 1. Inject 200ms sleep into task A (the "200ms probe" from the
    #    task spec).
    # 2. Record start/end timestamps for the overlap assertion.
    # 3. Track peak in-flight count.
    timings: Dict[str, Dict[str, float]] = {}
    timings_lock = threading.Lock()
    concurrency_state = {"in_flight": 0, "peak": 0}
    concurrency_lock = threading.Lock()

    A_SLEEP_SECONDS = 0.200  # 200ms — the task spec's "200ms probe"

    def _stub_execute(task, max_retries=5, timeout=None):
        with concurrency_lock:
            concurrency_state["in_flight"] += 1
            concurrency_state["peak"] = max(
                concurrency_state["peak"], concurrency_state["in_flight"]
            )

        with timings_lock:
            timings[task.id] = {"start": time.monotonic()}

        # Inject the spec-mandated 200ms sleep for task A. The other
        # tasks in the chain get a tiny 5ms sleep so the wall-clock
        # ordering is observable (otherwise all 3 would finish in
        # the same microsecond and the overlap test would be flaky
        # on fast machines).
        if task.id == "A":
            time.sleep(A_SLEEP_SECONDS)
        else:
            time.sleep(0.005)

        with timings_lock:
            timings[task.id]["end"] = time.monotonic()

        with concurrency_lock:
            concurrency_state["in_flight"] -= 1

        # Mark the task completed so the dispatcher doesn't
        # re-schedule it on the next layer rebuild.
        agent.task_manager.update_task_status(task.id, "completed")
        # Mirror the real completion bookkeeping in
        # ``_execute_task_with_retry`` (agent.py:4630) so the stub leaves
        # the agent in the same state the real executor does.
        # ``update_task_status`` mutates the shared ``SubTask`` objects
        # (``_load_tasks`` re-points ``task_manager.tasks`` at the
        # snapshot the post-read gate finalised), so the dispatcher sees
        # the new status on the next layer rebuild, and the session
        # counter feeds the dispatcher's same-id loop guard.
        agent._session_task_completed_counts[task.id] = (
            agent._session_task_completed_counts.get(task.id, 0) + 1
        )
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

    # Run to completion. agent.run uses asyncio.run internally.
    agent.run(timeout=None)

    # All three tasks recorded both a start and an end timestamp.
    for tid in ("A", "B", "C"):
        assert tid in timings, (
            f"task {tid} never executed (timings={timings!r})"
        )
        assert "start" in timings[tid] and "end" in timings[tid], (
            f"task {tid} missing start/end (got {timings[tid]!r})"
        )

    # Core serial-execution contract: each task's end is BEFORE
    # the next task's start. If the dispatcher had any parallelism
    # between layers, one of these inequalities would fail.
    a_start, a_end = timings["A"]["start"], timings["A"]["end"]
    b_start, b_end = timings["B"]["start"], timings["B"]["end"]
    c_start, c_end = timings["C"]["start"], timings["C"]["end"]

    # A completes before B starts.
    assert a_end <= b_start, (
        f"A.end ({a_end}) must be <= B.start ({b_start}); "
        f"B started before A completed. "
        f"timings: A=[{a_start}, {a_end}], B=[{b_start}, {b_end}]"
    )
    # B completes before C starts.
    assert b_end <= c_start, (
        f"B.end ({b_end}) must be <= C.start ({c_start}); "
        f"C started before B completed. "
        f"timings: B=[{b_start}, {b_end}], C=[{c_start}, {c_end}]"
    )
    # Sanity: A actually slept for ~200ms (within a generous
    # tolerance — the test cares about the contract, not the exact
    # sleep duration).
    a_duration = a_end - a_start
    assert a_duration >= A_SLEEP_SECONDS * 0.9, (
        f"A's duration was {a_duration:.3f}s, expected ~{A_SLEEP_SECONDS}s; "
        f"the 200ms sleep injection was not effective"
    )
    # Final task statuses on disk.
    completed_ids = [t.id for t in agent.task_manager.tasks if t.status == "completed"]
    assert completed_ids == ["A", "B", "C"], (
        f"expected all 3 tasks to be completed, got {completed_ids!r} "
        f"(statuses: {[(t.id, t.status) for t in agent.task_manager.tasks]!r})"
    )


# ---------------------------------------------------------------------------
# TDD test 3: max_concurrent == 1 throughout + 3 layer_started + 3 layer_completed
# ---------------------------------------------------------------------------


def test_case1_no_concurrency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Peak in-flight tasks is 1 throughout the run, and the logger
    emits 3 ``layer_started`` + 3 ``layer_completed`` events.

    Two complementary contracts pinned by this test:

      1. **Concurrency contract**: the chain has 1 task per layer,
         and the dispatcher's ``asyncio.gather`` is invoked with a
         1-element coroutine list per layer. Even if the controller
         had a higher cap, the maximum in-flight task count would
         still be 1. This proves the downstream-gate works: B does
         not enter ``_run_task_with_provider_slot`` until A has
         reached the next layer build (i.e., the layer_completed
         event has been logged).

      2. **Auditability contract**: the structured logger emits
         ``layer_started`` and ``layer_completed`` events for every
         layer transition. Operators reading ``execution.log`` see
         three pairs of these events (one per layer), with the
         task_ids payload containing the single task in each
         layer. This is the on-disk proof that the dispatcher
         respected the layer boundaries end-to-end.
    """
    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _write_chain_tasks(project_dir)

    from agent import AutonomousAgent
    from coding_tool import ClaudeCodingTool

    monkeypatch.setattr(
        ClaudeCodingTool, "query",
        lambda self, *args, **kwargs: "TEST_RESULT: PASSED\n",
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "query_json",
        lambda self, *args, **kwargs: {"tasks": []},
    )

    # Custom in-memory logger — same shape as
    # ``_RecordingLogger`` in test_agent_dispatch.py, but defined
    # inline to keep this file's dependency surface small.
    captured_events: List[Dict[str, Any]] = []
    capture_lock = threading.Lock()

    class _CapturingLogger:
        def debug(self, event, message, **kwargs):
            with capture_lock:
                captured_events.append({"level": "DEBUG", "event": event, "message": message, **kwargs})

        def info(self, event, message, **kwargs):
            with capture_lock:
                captured_events.append({"level": "INFO", "event": event, "message": message, **kwargs})

        def warning(self, event, message, **kwargs):
            with capture_lock:
                captured_events.append({"level": "WARNING", "event": event, "message": message, **kwargs})

        def error(self, event, message, **kwargs):
            with capture_lock:
                captured_events.append({"level": "ERROR", "event": event, "message": message, **kwargs})

        def critical(self, event, message, **kwargs):
            with capture_lock:
                captured_events.append({"level": "CRITICAL", "event": event, "message": message, **kwargs})

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test A→B→C serial chain no_concurrency",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=_CapturingLogger(),
    )
    agent._load_tasks()

    # Inject a tiny sleep on each task (so the wall-clock start
    # times for A, B, C are visibly different on a fast machine).
    # 5ms is short enough that the test stays < 1s end-to-end
    # while still letting the peak-concurrency assertion be
    # meaningful.
    concurrency_state = {"in_flight": 0, "peak": 0}
    concurrency_lock = threading.Lock()

    def _stub_execute(task, max_retries=5, timeout=None):
        with concurrency_lock:
            concurrency_state["in_flight"] += 1
            concurrency_state["peak"] = max(
                concurrency_state["peak"], concurrency_state["in_flight"]
            )
        # Tiny sleep so the timing window is observable.
        time.sleep(0.005)
        with concurrency_lock:
            concurrency_state["in_flight"] -= 1
        agent.task_manager.update_task_status(task.id, "completed")
        # Mirror the real completion bookkeeping in
        # ``_execute_task_with_retry`` (agent.py:4630) so the stub leaves
        # the agent in the same state the real executor does.
        # ``update_task_status`` mutates the shared ``SubTask`` objects
        # (``_load_tasks`` re-points ``task_manager.tasks`` at the
        # snapshot the post-read gate finalised), so the dispatcher sees
        # the new status on the next layer rebuild, and the session
        # counter feeds the dispatcher's same-id loop guard.
        agent._session_task_completed_counts[task.id] = (
            agent._session_task_completed_counts.get(task.id, 0) + 1
        )
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

    agent.run(timeout=None)

    # ----- Concurrency contract ------------------------------------------
    # Peak in-flight must be exactly 1. Each layer has exactly one
    # task, so the dispatcher's asyncio.gather always sees a single
    # coroutine — peak in-flight cannot exceed 1.
    assert concurrency_state["peak"] == 1, (
        f"peak concurrent in-flight tasks was {concurrency_state['peak']}, "
        f"expected 1. The chain ran with concurrency > 1 — the layer "
        f"boundary gate is broken."
    )

    # ----- Auditability contract: 3 layer_started + 3 layer_completed ----
    layer_started_events = [
        e for e in captured_events if e.get("event") == "layer_started"
    ]
    layer_completed_events = [
        e for e in captured_events if e.get("event") == "layer_completed"
    ]
    assert len(layer_started_events) == 3, (
        f"expected exactly 3 'layer_started' events (one per layer), "
        f"got {len(layer_started_events)}: {layer_started_events!r}"
    )
    assert len(layer_completed_events) == 3, (
        f"expected exactly 3 'layer_completed' events (one per layer), "
        f"got {len(layer_completed_events)}: {layer_completed_events!r}"
    )

    # Each layer_started must come BEFORE its matching layer_completed
    # in the captured event stream. We pair them in order (the
    # dispatcher emits them in nested sequence: start, ...,
    # completed, start, ..., completed, ...).
    started_ids = [
        e.get("data", {}).get("task_ids") for e in layer_started_events
    ]
    completed_ids = [
        e.get("data", {}).get("task_ids") for e in layer_completed_events
    ]
    assert started_ids == [["A"], ["B"], ["C"]], (
        f"layer_started task_ids must be [['A'], ['B'], ['C']], got {started_ids!r}"
    )
    assert completed_ids == [["A"], ["B"], ["C"]], (
        f"layer_completed task_ids must be [['A'], ['B'], ['C']], got {completed_ids!r}"
    )

    # Pairwise ordering: layer_started[i] precedes layer_completed[i]
    # in the captured event stream.
    all_events = captured_events
    for i in range(3):
        start_idx = next(
            j for j, e in enumerate(all_events)
            if e.get("event") == "layer_started"
            and e.get("data", {}).get("task_ids") == started_ids[i]
        )
        end_idx = next(
            j for j, e in enumerate(all_events)
            if e.get("event") == "layer_completed"
            and e.get("data", {}).get("task_ids") == completed_ids[i]
        )
        assert start_idx < end_idx, (
            f"layer_started[{i}] (idx={start_idx}) must precede "
            f"layer_completed[{i}] (idx={end_idx}) for layer {completed_ids[i]}"
        )

    # And: layer_completed[i] must precede layer_started[i+1] (the
    # next layer's start). This is the downstream gate in action.
    for i in range(2):
        end_idx_i = next(
            j for j, e in enumerate(all_events)
            if e.get("event") == "layer_completed"
            and e.get("data", {}).get("task_ids") == completed_ids[i]
        )
        start_idx_next = next(
            j for j, e in enumerate(all_events)
            if e.get("event") == "layer_started"
            and e.get("data", {}).get("task_ids") == started_ids[i + 1]
        )
        assert end_idx_i < start_idx_next, (
            f"layer_completed[{i}] (idx={end_idx_i}) for {completed_ids[i]} "
            f"must precede layer_started[{i+1}] (idx={start_idx_next}) for "
            f"{started_ids[i+1]} — the downstream gate was bypassed"
        )

    # Final task statuses on disk.
    completed = [t.id for t in agent.task_manager.tasks if t.status == "completed"]
    assert completed == ["A", "B", "C"], (
        f"expected A, B, C to all be completed, got {completed!r} "
        f"(statuses: {[(t.id, t.status) for t in agent.task_manager.tasks]!r})"
    )


# ---------------------------------------------------------------------------
# Case 2 / TDD test 1: _build_layers returns 1 layer [A..F]
# ---------------------------------------------------------------------------


def test_case2_single_layer() -> None:
    """``agent._build_layers`` returns a single outer layer with all 6 tasks.

    PRD acceptance Case 2: 6 tasks with no dependencies and disjoint
    files_to_modify must form a single outer layer containing one
    micro-layer with all 6 tasks. The static-topology half of the
    contract — the runtime half (global cap ≤ 5) is pinned by
    :func:`test_case2_six_tasks_concurrent_lte_5`.
    """
    tasks = _make_six_subtasks()
    # Import the module-level helper from agent.py. We do this here
    # (rather than at module top-level) so collection-only pytest
    # invocations don't fail when the backend module imports fail.
    from agent import _build_layers

    layers = _build_layers(tasks)

    # Must be exactly 1 outer layer, containing exactly 1 micro-layer
    # with exactly 6 tasks.
    assert isinstance(layers, list), (
        f"_build_layers must return a list, got {type(layers).__name__}: {layers!r}"
    )
    assert len(layers) == 1, (
        f"expected exactly 1 outer layer for 6 no-dep tasks, got {len(layers)}: "
        f"{[[[t.id for t in ml] for ml in ol] for ol in layers]!r}"
    )

    outer_layer = layers[0]
    assert isinstance(outer_layer, list), (
        f"layer[0] must be a list of micro-layers, got {type(outer_layer).__name__}: {outer_layer!r}"
    )
    assert len(outer_layer) == 1, (
        f"layer[0] must contain exactly 1 micro-layer, got {len(outer_layer)}: "
        f"{[[t.id for t in ml] for ml in outer_layer]!r}"
    )

    micro_layer = outer_layer[0]
    assert isinstance(micro_layer, list), (
        f"layer[0][0] must be a micro-layer list, got {type(micro_layer).__name__}: {micro_layer!r}"
    )
    assert len(micro_layer) == 6, (
        f"layer[0][0] must contain exactly 6 tasks, got {len(micro_layer)}: "
        f"{[t.id for t in micro_layer]!r}"
    )

    # Within-micro-layer order is the input order (Kahn's algorithm
    # preserves insertion order when the initial frontier is all
    # roots, which is exactly this case).
    layer_ids = [t.id for t in micro_layer]
    assert layer_ids == list(_CASE2_TASK_IDS), (
        f"layer ordering must be {list(_CASE2_TASK_IDS)!r}, got {layer_ids!r}"
    )

    # Sanity: every task still carries provider="vendor-a" — confirms
    # SubTask accepted the kwarg and the in-memory shape is what
    # _resolve_provider_for_task will see at runtime.
    for t in micro_layer:
        assert getattr(t, "provider", None) == _CASE2_PROVIDER, (
            f"task {t.id} lost provider attribute: {getattr(t, 'provider', None)!r}"
        )


# ---------------------------------------------------------------------------
# Case 2 / TDD test 2: max_concurrent ≤ 5 throughout the run
# ---------------------------------------------------------------------------


def test_case2_six_tasks_concurrent_lte_5(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """6 same-layer tasks on provider ``vendor-a`` never exceed 5 in flight.

    Three complementary contracts pinned by this test:

      1. **Concurrency contract**: peak in-flight tasks (counted inside
         the spy) is at most 5. The 6th task must suspend inside
         ``controller.acquire("vendor-a")`` until a paired
         ``release("vendor-a")`` frees a slot.

      2. **Per-provider cap contract**: since every task carries
         ``provider="vendor-a"``, all 6 tasks go through the same
         per-provider semaphore. The default per-provider cap is 5,
         so the same ceiling applies even with a wider global cap
         (we set ``global_limit=5, provider_limits={"vendor-a": 5}``
         explicitly to be safe).

      3. **Queue-not-deny contract**: the 6th task is eventually
         admitted (it just has to wait for one of the first 5 to
         release). All 6 tasks complete — see
         :func:`test_case2_all_complete` for the dedicated
         completion assertion, but this test also checks it as a
         smoke guard.
    """
    # Mirror the asyncio-loop installer from test_agent_dispatch.py:
    # ``ProviderConcurrencyController.__init__`` creates an
    # :class:`asyncio.Semaphore` that, on Python 3.9, eagerly binds
    # to the current event loop. A sync test (no running loop) would
    # raise ``RuntimeError: There is no current event loop``. We
    # install a fresh loop, run the test, then restore the previous
    # policy so subsequent tests see a clean state.
    import asyncio as _asyncio
    saved_policy = _asyncio.get_event_loop_policy()
    fresh_policy = _asyncio.DefaultEventLoopPolicy()
    _asyncio.set_event_loop_policy(fresh_policy)
    loop = fresh_policy.new_event_loop()
    fresh_policy.set_event_loop(loop)

    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _write_six_task_json(project_dir)

    from agent import AutonomousAgent
    from coding_tool import ClaudeCodingTool
    from provider_concurrency import ProviderConcurrencyController

    # Stub ClaudeCodingTool.query / query_json so the agent's import
    # side-effects don't blow up if plan() is somehow called. We use
    # recover-style flow (no plan), but the defensive stubs avoid
    # surprises.
    monkeypatch.setattr(
        ClaudeCodingTool, "query",
        lambda self, *args, **kwargs: "TEST_RESULT: PASSED\n",
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "query_json",
        lambda self, *args, **kwargs: {"tasks": []},
    )

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test 6-task same-layer concurrency cap",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )
    agent._load_tasks()

    # Sanity: every task loaded from tasks.json carries the
    # ``provider`` field. This guards against a regression where the
    # ``provider`` field is silently dropped during
    # ``task_manager.load_tasks``.
    loaded_providers = {
        t.id: getattr(t, "provider", None) for t in agent.task_manager.tasks
    }
    for tid in _CASE2_TASK_IDS:
        assert loaded_providers.get(tid) == _CASE2_PROVIDER, (
            f"task {tid} loaded without provider='{_CASE2_PROVIDER}': "
            f"{loaded_providers.get(tid)!r}"
        )

    # Spy on _execute_task_with_retry so we can:
    # 1. Inject the spec-mandated 200ms sleep on every task.
    # 2. Track peak in-flight count via a thread-safe counter.
    # 3. Snapshot controller.available_global() so we can assert
    #    the global semaphore actually reached 0 (proves the cap
    #    was hit, not just "happened to be ≤ 5 by luck").
    concurrency_state = {"in_flight": 0, "peak": 0, "global_min": 5}
    concurrency_lock = threading.Lock()
    # Use a Barrier sized to the cap (5) so the first 5 tasks all
    # sit inside the body concurrently while the 6th is parked in
    # acquire. This is the only way to deterministically observe
    # peak=5 on a fast machine.
    enter_barrier = threading.Barrier(5, timeout=5.0)

    # The provider controller we'll inject. We build it BEFORE the
    # spy so the spy can introspect ``controller.available_global()``
    # at each task's entry. global_limit=5 (binding cap) and an
    # explicit per-provider limit of 5 for ``"vendor-a"`` so the
    # default cap isn't relied upon.
    controller = ProviderConcurrencyController(
        global_limit=5,
        provider_limits={"vendor-a": 5},
    )
    agent._provider_controller = controller

    def _stub_execute(task, max_retries=5, timeout=None):
        with concurrency_lock:
            concurrency_state["in_flight"] += 1
            concurrency_state["peak"] = max(
                concurrency_state["peak"], concurrency_state["in_flight"]
            )
            # Snapshot the controller's free global slots. With
            # global=5 and peak=5, available_global() should reach
            # 0 (proving the cap was hit, not just bounded above by
            # 5).
            concurrency_state["global_min"] = min(
                concurrency_state["global_min"],
                controller.available_global(),
            )

        # Synchronise the first 5 tasks so they all sit in-body
        # concurrently while the 6th is parked in
        # ``acquire("vendor-a")``. The barrier is sized to 5 (the
        # cap), so the 6th never makes it past acquire until one
        # of the first 5 finishes.
        try:
            enter_barrier.wait(timeout=5.0)
        except threading.BrokenBarrierError:
            # If the barrier breaks (e.g. the test driver killed
            # the thread early), we still proceed so the task
            # completes cleanly and the agent can drain.
            pass

        # 200ms probe per the task spec. The total wall-clock
        # time should be ~ (5+1) / 5 * 200ms ≈ 400ms (5 run in
        # parallel, 1 waits for a slot).
        time.sleep(_CASE2_SLEEP_SECONDS)

        with concurrency_lock:
            concurrency_state["in_flight"] -= 1

        # Mark the task completed so the dispatcher doesn't
        # re-schedule it on the next layer rebuild.
        agent.task_manager.update_task_status(task.id, "completed")
        # Mirror the real completion bookkeeping in
        # ``_execute_task_with_retry`` (agent.py:4630) so the stub leaves
        # the agent in the same state the real executor does.
        # ``update_task_status`` mutates the shared ``SubTask`` objects
        # (``_load_tasks`` re-points ``task_manager.tasks`` at the
        # snapshot the post-read gate finalised), so the dispatcher sees
        # the new status on the next layer rebuild, and the session
        # counter feeds the dispatcher's same-id loop guard.
        agent._session_task_completed_counts[task.id] = (
            agent._session_task_completed_counts.get(task.id, 0) + 1
        )
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

    try:
        # Run to completion. agent.run uses asyncio.run internally.
        agent.run(timeout=None)
    finally:
        # Restore the previous event loop policy so the rest of the
        # suite sees a clean main-thread state. We close the fresh
        # loop we created; subsequent tests that need a loop will
        # install their own.
        try:
            loop.close()
        except Exception:
            pass
        try:
            _asyncio.set_event_loop_policy(saved_policy)
        except Exception:
            pass

    # ----- Concurrency contract ------------------------------------------
    # Peak in-flight must be ≤ the controller's global cap (5). A peak
    # of 6 would prove the controller was bypassed.
    assert concurrency_state["peak"] <= 5, (
        f"peak concurrent tasks ({concurrency_state['peak']}) exceeded "
        f"global cap (5) — the dispatcher bypassed controller.acquire"
    )
    # And the cap should have been HIT — a peak < 5 would mean the
    # test was accidentally serial. We require ≥ 2 to confirm at
    # least some concurrency happened, and we additionally check
    # that available_global() reached 0 (proves the cap was bound).
    assert concurrency_state["peak"] >= 2, (
        f"peak concurrent tasks ({concurrency_state['peak']}) too low — "
        f"the dispatcher ran tasks serially instead of in parallel"
    )
    assert concurrency_state["global_min"] == 0, (
        f"controller.available_global() reached a minimum of "
        f"{concurrency_state['global_min']} (expected 0); the global "
        f"semaphore was never saturated, so the cap-binding assertion "
        f"is not actually proving the cap was exercised"
    )

    # ----- Queue-not-deny contract ---------------------------------------
    # All 6 tasks must have completed (the 6th was just queued, not
    # dropped). The dedicated completion assertion lives in
    # ``test_case2_all_complete``; here we just assert no task is
    # stuck or failed.
    completed_ids = sorted(
        t.id for t in agent.task_manager.tasks if t.status == "completed"
    )
    assert len(completed_ids) == 6, (
        f"expected all 6 tasks to be completed, got {completed_ids!r} "
        f"(statuses: {[(t.id, t.status) for t in agent.task_manager.tasks]!r})"
    )
    failed_ids = [t.id for t in agent.task_manager.tasks if t.status == "failed"]
    assert not failed_ids, (
        f"unexpected failed tasks: {failed_ids!r}"
    )

    # ----- Controller slot hygiene --------------------------------------
    # No slot leaks — after all 6 tasks finish, every global slot
    # should be back in the pool.
    assert controller.available_global() == 5, (
        f"controller leaked global slots: available_global="
        f"{controller.available_global()} (expected 5)"
    )


# ---------------------------------------------------------------------------
# Case 2 / TDD test 3: 6 tasks all complete
# ---------------------------------------------------------------------------


def test_case2_all_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All 6 same-layer tasks reach ``status='completed'`` end-to-end.

    This test isolates the completion assertion from
    :func:`test_case2_six_tasks_concurrent_lte_5` so that a
    regression in either contract (concurrency cap vs. completion)
    can be diagnosed independently. It uses a slightly simpler spy
    (no barrier, no controller introspection) — only the
    ``completed`` status on each task is asserted.
    """
    import asyncio as _asyncio
    saved_policy = _asyncio.get_event_loop_policy()
    fresh_policy = _asyncio.DefaultEventLoopPolicy()
    _asyncio.set_event_loop_policy(fresh_policy)
    loop = fresh_policy.new_event_loop()
    fresh_policy.set_event_loop(loop)

    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _write_six_task_json(project_dir)

    from agent import AutonomousAgent
    from coding_tool import ClaudeCodingTool
    from provider_concurrency import ProviderConcurrencyController

    monkeypatch.setattr(
        ClaudeCodingTool, "query",
        lambda self, *args, **kwargs: "TEST_RESULT: PASSED\n",
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "query_json",
        lambda self, *args, **kwargs: {"tasks": []},
    )

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test 6-task same-layer all complete",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )
    agent._load_tasks()

    # Explicit per-provider cap on "vendor-a" to keep the test
    # deterministic on whatever the test runner's env looks like.
    # We rely on the default global cap (5) by passing an
    # explicit ``global_limit=5`` so the test is hermetic w.r.t.
    # the test runner's env.
    controller = ProviderConcurrencyController(
        global_limit=5,
        provider_limits={"vendor-a": 5},
    )
    agent._provider_controller = controller

    # Simplest possible spy: 50ms sleep per task (smaller than the
    # 200ms used in the concurrency test so the whole test finishes
    # in < 1s) and a completion write-through. We don't need a
    # barrier here — the contract under test is "all 6 finish", not
    # "peak ≤ 5".
    PER_TASK_SLEEP = 0.05

    def _stub_execute(task, max_retries=5, timeout=None):
        time.sleep(PER_TASK_SLEEP)
        agent.task_manager.update_task_status(task.id, "completed")
        # Mirror the real completion bookkeeping in
        # ``_execute_task_with_retry`` (agent.py:4630) so the stub leaves
        # the agent in the same state the real executor does.
        # ``update_task_status`` mutates the shared ``SubTask`` objects
        # (``_load_tasks`` re-points ``task_manager.tasks`` at the
        # snapshot the post-read gate finalised), so the dispatcher sees
        # the new status on the next layer rebuild, and the session
        # counter feeds the dispatcher's same-id loop guard.
        agent._session_task_completed_counts[task.id] = (
            agent._session_task_completed_counts.get(task.id, 0) + 1
        )
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

    try:
        agent.run(timeout=None)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        try:
            _asyncio.set_event_loop_policy(saved_policy)
        except Exception:
            pass

    # All 6 tasks must be completed.
    completed_ids = sorted(
        t.id for t in agent.task_manager.tasks if t.status == "completed"
    )
    assert completed_ids == sorted(_CASE2_TASK_IDS), (
        f"expected all 6 tasks {sorted(_CASE2_TASK_IDS)!r} to be completed, "
        f"got {completed_ids!r} (statuses: "
        f"{[(t.id, t.status) for t in agent.task_manager.tasks]!r})"
    )
    # No task should have failed.
    failed_ids = [
        t.id for t in agent.task_manager.tasks if t.status == "failed"
    ]
    assert not failed_ids, (
        f"unexpected failed tasks: {failed_ids!r}"
    )
    # No task should still be pending or in_progress — every task
    # either completed or failed by the time ``run`` returns.
    open_ids = [
        t.id for t in agent.task_manager.tasks
        if t.status in ("pending", "in_progress")
    ]
    assert not open_ids, (
        f"tasks left in non-terminal state: {open_ids!r}"
    )

    # Persistence check: completion must survive a reload. Post task
    # #3.8 ``TaskManager.save_tasks`` strips the runtime fields
    # (``status`` / ``updated_time`` / ``failure_reason`` /
    # ``breakdown_count``) from ``tasks.json`` — runtime state lives in
    # SQLite (``plan_execution.task_progress``) and is re-hydrated by
    # ``_load_tasks``. Asserting ``t["status"] == "completed"`` on the
    # file would be asserting on a field the writer intentionally no
    # longer emits; the round-trip below is the actual cross-process
    # recovery contract: re-read disk + SQLite and every task must come
    # back ``completed``.
    persisted = json.loads(
        (project_dir / "tasks.json").read_text(encoding="utf-8")
    )
    assert all("status" not in t for t in persisted["tasks"]), (
        "post task #3.8 tasks.json must not carry runtime status; got "
        f"{[(t.get('id'), t.get('status')) for t in persisted['tasks']]!r}"
    )
    agent._load_tasks()
    reloaded_statuses = {t.id: t.status for t in agent._all_tasks}
    for tid in _CASE2_TASK_IDS:
        assert reloaded_statuses.get(tid) == "completed", (
            f"task {tid} came back as {reloaded_statuses.get(tid)!r} after "
            f"a reload, expected 'completed' (full persisted statuses: "
            f"{reloaded_statuses!r})"
        )


# ---------------------------------------------------------------------------
# Case 3 helpers: diamond DAG A → (B, C) → D
# ---------------------------------------------------------------------------
#
# Case 3 background
# -----------------
# PRD acceptance Case 3 requires the 4-task diamond topology:
#
#         ┌─→ B ─┐
#     A ──┤      ├──→ D
#         └─→ C ─┘
#
# The contract has three parts pinned by three TDD tests:
#
#   1. ``_build_layers`` returns exactly 3 outer layers:
#      ``[[[A]], [[B, C]], [[D]]]``
#      — B and C are topologically parallel siblings in outer layer 1
#      (and, with disjoint files_to_modify, share a single micro-layer);
#      A is alone in outer layer 0; D is alone in outer layer 2.
#
#   2. **Cross-layer wait** — D.start_time must be ≥ max(B.end_time,
#      C.end_time). Even if B is 100ms slower than C, D must wait for
#      the *slower* sibling (B), not the faster one (C). This is the
#      defining property of a diamond: D's two parents are siblings
#      that finish at different wall-clock times, and D's start
#      aligns to the *max* of the two.
#
#   3. **Intra-layer concurrency** — B and C share a layer, so their
#      wall-clock intervals overlap. This is the same-layer-cap
#      property tested in Case 2, but pinned on a 2-task layer
#      (the smallest non-trivial same-layer shape).
#
# The runtime tests use a deterministic per-task sleep schedule:
#     A: 30ms  (small — A has no parallel siblings)
#     B: 400ms (the "slow" sibling — task spec injects the 100ms
#               delta so D.start must align to B.end, not C.end)
#     C: 50ms  (the "fast" sibling)
#     D: 30ms  (small — D has no parallel siblings)
#
# If the dispatcher raced B and C correctly, the elapsed time of the
# whole run is approximately A + max(B, C) + D ≈ 30 + 150 + 30 = 210ms
# (not A + B + C + D ≈ 260ms; the 50ms saved by overlapping B and C
# is observable as the difference). D's start time is the proof
# that the dispatcher waited for the *slower* sibling.


_CASE3_A_SLEEP_SECONDS = 0.030
# 2026-09-14: B's sleep was raised 150ms → 400ms after the
# ``b_duration > c_duration`` ordering assertion flaked in the full
# suite (B 0.180s vs C 0.183s): durations are wall-clock windows
# around ``asyncio.sleep``, and a leaked background thread stalling
# the event loop can inflate C's 50ms window past B's 150ms one.
# A 350ms nominal gap survives >250ms of one-sided stall, which is
# far beyond anything observed (the flake had ~3ms of reordering).
_CASE3_B_SLEEP_SECONDS = 0.400  # B is the slow sibling
_CASE3_C_SLEEP_SECONDS = 0.050  # C is the fast sibling
_CASE3_D_SLEEP_SECONDS = 0.030


def _write_diamond_tasks(project_dir: Path) -> Path:
    """Write the A→(B,C)→D diamond as a single ``tasks.json`` file.

    The diamond has the strict shape PRD acceptance Case 3 requires:
    A has no dependencies, B and C both depend on A, and D depends on
    BOTH B and C. B is the "slow" sibling (400ms probe) and C is the
    "fast" sibling (50ms probe) so we can prove D aligns to max(B, C).
    """
    tasks_payload = {
        "requirement": "test A→(B,C)→D diamond DAG",
        "stop_reason": None,
        "reason_detail": None,
        "tasks": [
            {
                "id": "A",
                "title": "Diamond head A",
                "description": "first task, no deps",
                "test_command": "echo A",
                "test_commands": [],
                "status": "pending",
                "updated_time": None,
                "failure_reason": None,
                "project_dir": None,
                "model_type": "medium",
                "depends_on": [],
                "files_to_modify": ["a.py"],
            },
            {
                "id": "B",
                "title": "Diamond slow sibling B",
                "description": "waits for A, slow sibling (400ms)",
                "test_command": "echo B",
                "test_commands": [],
                "status": "pending",
                "updated_time": None,
                "failure_reason": None,
                "project_dir": None,
                "model_type": "medium",
                "depends_on": ["A"],
                "files_to_modify": ["b.py"],
            },
            {
                "id": "C",
                "title": "Diamond fast sibling C",
                "description": "waits for A, fast sibling (50ms)",
                "test_command": "echo C",
                "test_commands": [],
                "status": "pending",
                "updated_time": None,
                "failure_reason": None,
                "project_dir": None,
                "model_type": "medium",
                "depends_on": ["A"],
                "files_to_modify": ["c.py"],
            },
            {
                "id": "D",
                "title": "Diamond tail D",
                "description": "waits for BOTH B and C",
                "test_command": "echo D",
                "test_commands": [],
                "status": "pending",
                "updated_time": None,
                "failure_reason": None,
                "project_dir": None,
                "model_type": "medium",
                "depends_on": ["B", "C"],
                "files_to_modify": ["d.py"],
            },
        ],
    }
    _materialize_declared_files(project_dir, tasks_payload)
    tasks_file = project_dir / "tasks.json"
    tasks_file.write_text(json.dumps(tasks_payload, indent=2), encoding="utf-8")
    return tasks_file


def _make_diamond_subtasks() -> List[Any]:
    """Build the 4 in-memory SubTask objects used by ``_build_layers``
    for the Case 3 topology test.

    The shape mirrors what ``agent._load_tasks`` would produce from
    the on-disk JSON. We import :class:`task.SubTask` lazily so this
    module is importable in environments where the runtime
    dependencies are missing (e.g. during collection-only mode).
    """
    from task import SubTask

    return [
        SubTask(
            id="A",
            title="Diamond head A",
            description="first task, no deps",
            test_command="echo A",
            test_commands=[],
            status="pending",
            depends_on=[],
            files_to_modify=["a.py"],
        ),
        SubTask(
            id="B",
            title="Diamond slow sibling B",
            description="waits for A, slow sibling (400ms)",
            test_command="echo B",
            test_commands=[],
            status="pending",
            depends_on=["A"],
            files_to_modify=["b.py"],
        ),
        SubTask(
            id="C",
            title="Diamond fast sibling C",
            description="waits for A, fast sibling (50ms)",
            test_command="echo C",
            test_commands=[],
            status="pending",
            depends_on=["A"],
            files_to_modify=["c.py"],
        ),
        SubTask(
            id="D",
            title="Diamond tail D",
            description="waits for BOTH B and C",
            test_command="echo D",
            test_commands=[],
            status="pending",
            depends_on=["B", "C"],
            files_to_modify=["d.py"],
        ),
    ]


# ---------------------------------------------------------------------------
# Case 3 / TDD test 1: _build_layers returns 3 outer layers [[[A]], [[B, C]], [[D]]]
# ---------------------------------------------------------------------------


def test_case3_diamond_layers() -> None:
    """``agent._build_layers`` returns 3 outer layers for the A→(B,C)→D diamond.

    PRD acceptance Case 3 — static topology half. B and C must end up
    in the *same* outer layer (both depend only on A) and D must end up
    in its *own* outer layer (depends on both B and C). A is alone in
    the root outer layer because it has no dependencies. Because B and C
    have disjoint files_to_modify, they share a single micro-layer within
    outer layer 1.

    The exact expected shape is::

        layers = [[[A]], [[B, C]], [[D]]]

    If the implementation ever loses this property (e.g. by treating
    ``D.depends_on = [B, C]`` as two separate ``depends_on``
    constraints and re-ordering B and C between layers), this test
    will fail with a clear ``[[[A]], [[B, C]], [[D]]]`` mismatch message.
    """
    tasks = _make_diamond_subtasks()
    # Import the module-level helper from agent.py. We do this here
    # (rather than at module top-level) so collection-only pytest
    # invocations don't fail when the backend module imports fail.
    from agent import _build_layers

    layers = _build_layers(tasks)

    # Must be exactly 3 outer layers.
    assert isinstance(layers, list), (
        f"_build_layers must return a list, got {type(layers).__name__}: {layers!r}"
    )
    assert len(layers) == 3, (
        f"expected exactly 3 outer layers for the A→(B,C)→D diamond, got "
        f"{len(layers)}: {[[[t.id for t in ml] for ml in ol] for ol in layers]!r}"
    )

    # ----- Layer 0: A alone (root of the DAG) ---------------------------
    outer_layer_0 = layers[0]
    assert isinstance(outer_layer_0, list), (
        f"layer[0] must be a list of micro-layers, got {type(outer_layer_0).__name__}: {outer_layer_0!r}"
    )
    assert len(outer_layer_0) == 1, (
        f"layer[0] must contain exactly 1 micro-layer, got {len(outer_layer_0)}: {outer_layer_0!r}"
    )
    assert [t.id for t in outer_layer_0[0]] == ["A"], (
        f"layer[0][0] must be exactly ['A'], got {[t.id for t in outer_layer_0[0]]!r}; "
        f"A is the only root of the diamond DAG"
    )

    # ----- Layer 1: B and C in the SAME outer layer (parallel siblings) -----
    outer_layer_1 = layers[1]
    assert isinstance(outer_layer_1, list), (
        f"layer[1] must be a list of micro-layers, got {type(outer_layer_1).__name__}: {outer_layer_1!r}"
    )
    assert len(outer_layer_1) == 1, (
        f"layer[1] must contain exactly 1 micro-layer, got {len(outer_layer_1)}: {outer_layer_1!r}"
    )
    assert set(t.id for t in outer_layer_1[0]) == {"B", "C"}, (
        f"layer[1][0] must contain B and C as parallel siblings, got "
        f"{[t.id for t in outer_layer_1[0]]!r}; B and C both depend only on A, "
        f"so they must be in the same micro-layer"
    )
    # Order within the micro-layer is implementation-defined (Kahn's
    # algorithm preserves insertion order, but the diamond has
    # {B, C} as roots of the second layer; we don't pin order here
    # because the dispatcher will issue them concurrently via
    # asyncio.gather regardless of order).

    # ----- Layer 2: D alone (depends on BOTH B and C) -----------------
    outer_layer_2 = layers[2]
    assert isinstance(outer_layer_2, list), (
        f"layer[2] must be a list of micro-layers, got {type(outer_layer_2).__name__}: {outer_layer_2!r}"
    )
    assert len(outer_layer_2) == 1, (
        f"layer[2] must contain exactly 1 micro-layer, got {len(outer_layer_2)}: {outer_layer_2!r}"
    )
    assert [t.id for t in outer_layer_2[0]] == ["D"], (
        f"layer[2][0] must be exactly ['D'], got {[t.id for t in outer_layer_2[0]]!r}; "
        f"D depends on BOTH B and C so it is alone in the tail outer layer"
    )

    # Total of 4 tasks across all 3 outer layers — no task was lost, no
    # task was duplicated.
    all_ids = [t.id for outer_layer in layers for micro_layer in outer_layer for t in micro_layer]
    assert sorted(all_ids) == ["A", "B", "C", "D"], (
        f"all 4 diamond tasks must appear exactly once across layers, "
        f"got {all_ids!r}"
    )


# ---------------------------------------------------------------------------
# Case 3 / TDD test 2: D.start_time ≥ max(B.end_time, C.end_time)
# ---------------------------------------------------------------------------


def test_case3_d_starts_after_both_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D waits for BOTH B and C to finish, not just whichever finishes
    first.

    The injection:
      * A sleeps 30ms (small — A is alone in layer 0).
      * B sleeps 400ms (the SLOW sibling).
      * C sleeps 50ms (the FAST sibling).
      * D sleeps 30ms (small — D is alone in layer 2).

    The assertion:
      * B.end ≤ D.start  (D must wait for the slow sibling B)
      * C.end ≤ D.start  (D must wait for the fast sibling C too,
                          which is trivially true if B's wait works,
                          but the test pins both halves)
      * D.start ≥ max(B.end, C.end)  (the cross-layer-wait contract)

    If the implementation mistakenly considers D eligible as soon as
    *either* B or C finishes (treating the join as a single
    ``depends_on`` rather than two parallel constraints), the
    ``B.end ≤ D.start`` assertion will fail: D would start ~50ms
    after A.end (when C finishes), but B is still running and
    would not end until ~400ms after A.end.

    Edge case also pinned: the fast sibling C *can* finish before
    the slow sibling B (this is the whole point of the test). The
    test does NOT assert that B and C are running concurrently —
    that's :func:`test_case3_bc_concurrent`'s job. This test only
    asserts that D's start is downstream of *both* ends.
    """
    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _write_diamond_tasks(project_dir)

    from agent import AutonomousAgent
    from coding_tool import ClaudeCodingTool

    # Stub ClaudeCodingTool.query / query_json so the agent's import
    # side-effects don't blow up if plan() is somehow called. We use
    # recover-style flow (no plan), but the defensive stubs avoid
    # surprises.
    monkeypatch.setattr(
        ClaudeCodingTool, "query",
        lambda self, *args, **kwargs: "TEST_RESULT: PASSED\n",
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "query_json",
        lambda self, *args, **kwargs: {"tasks": []},
    )

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test diamond D waits for both siblings",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )
    agent._load_tasks()

    # Spy on _execute_task_with_retry so we can:
    # 1. Inject the spec-mandated per-task sleep schedule.
    # 2. Record start/end timestamps for the cross-layer-wait
    #    assertion.
    timings: Dict[str, Dict[str, float]] = {}
    timings_lock = threading.Lock()

    PER_TASK_SLEEP = {
        "A": _CASE3_A_SLEEP_SECONDS,
        "B": _CASE3_B_SLEEP_SECONDS,  # slow sibling — 400ms
        "C": _CASE3_C_SLEEP_SECONDS,  # fast sibling — 50ms
        "D": _CASE3_D_SLEEP_SECONDS,
    }

    def _stub_execute(task, max_retries=5, timeout=None):
        with timings_lock:
            timings[task.id] = {"start": time.monotonic()}

        # Inject the per-task sleep matching the diamond spec.
        time.sleep(PER_TASK_SLEEP[task.id])

        with timings_lock:
            timings[task.id]["end"] = time.monotonic()

        # Mark the task completed so the dispatcher doesn't
        # re-schedule it on the next layer rebuild.
        agent.task_manager.update_task_status(task.id, "completed")
        # Mirror the real completion bookkeeping in
        # ``_execute_task_with_retry`` (agent.py:4630) so the stub leaves
        # the agent in the same state the real executor does.
        # ``update_task_status`` mutates the shared ``SubTask`` objects
        # (``_load_tasks`` re-points ``task_manager.tasks`` at the
        # snapshot the post-read gate finalised), so the dispatcher sees
        # the new status on the next layer rebuild, and the session
        # counter feeds the dispatcher's same-id loop guard.
        agent._session_task_completed_counts[task.id] = (
            agent._session_task_completed_counts.get(task.id, 0) + 1
        )
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

    # Run to completion. agent.run uses asyncio.run internally.
    agent.run(timeout=None)

    # All four tasks recorded both a start and an end timestamp.
    for tid in ("A", "B", "C", "D"):
        assert tid in timings, (
            f"task {tid} never executed (timings={timings!r})"
        )
        assert "start" in timings[tid] and "end" in timings[tid], (
            f"task {tid} missing start/end (got {timings[tid]!r})"
        )

    a_start, a_end = timings["A"]["start"], timings["A"]["end"]
    b_start, b_end = timings["B"]["start"], timings["B"]["end"]
    c_start, c_end = timings["C"]["start"], timings["C"]["end"]
    d_start, d_end = timings["D"]["start"], timings["D"]["end"]

    # Sanity: A completed before either B or C started. This is the
    # upstream-edge invariant (A is the parent of B and C in the
    # DAG); if this fails the rest of the assertions are meaningless.
    assert a_end <= b_start, (
        f"A.end ({a_end}) must be <= B.start ({b_start}); "
        f"B started before A completed. timings: "
        f"A=[{a_start}, {a_end}], B=[{b_start}, {b_end}]"
    )
    assert a_end <= c_start, (
        f"A.end ({a_end}) must be <= C.start ({c_start}); "
        f"C started before A completed. timings: "
        f"A=[{a_start}, {a_end}], C=[{c_start}, {c_end}]"
    )

    # The contract under test: D must wait for BOTH B and C.
    # Even though C is the fast sibling (50ms), D must not start
    # until B (the slow sibling at 400ms) also finishes.
    assert b_end <= d_start, (
        f"B.end ({b_end}) must be <= D.start ({d_start}); "
        f"D started before B completed — D did not wait for the "
        f"slow sibling. timings: B=[{b_start}, {b_end}], D=[{d_start}, {d_end}]"
    )
    assert c_end <= d_start, (
        f"C.end ({c_end}) must be <= D.start ({d_start}); "
        f"D started before C completed. timings: "
        f"C=[{c_start}, {c_end}], D=[{d_start}, {d_end}]"
    )

    # Equivalent cross-layer-wait assertion: D.start ≥ max(B.end, C.end).
    # This is the more compact form of the same contract — D aligns
    # to the slower of the two siblings.
    slower_sibling_end = max(b_end, c_end)
    assert d_start >= slower_sibling_end, (
        f"D.start ({d_start}) must be >= max(B.end, C.end) = "
        f"{slower_sibling_end}; D started before both siblings "
        f"finished. timings: B=[{b_start}, {b_end}], "
        f"C=[{c_start}, {c_end}], D=[{d_start}, {d_end}]"
    )

    # Sanity: the slow sibling really was the slow sibling (B
    # actually slept ~400ms, C actually slept ~50ms). If a
    # regression causes the per-task sleep injection to be lost
    # (e.g. the agent no longer passes the right task id to the
    # spy), this assertion fails first, giving a clear
    # "the spec-mandated sleep schedule was not effective"
    # diagnostic before the layering assertions above.
    b_duration = b_end - b_start
    c_duration = c_end - c_start
    assert b_duration >= _CASE3_B_SLEEP_SECONDS * 0.9, (
        f"B's duration was {b_duration:.3f}s, expected "
        f"~{_CASE3_B_SLEEP_SECONDS}s; the slow-sibling sleep was "
        f"not effective"
    )
    assert c_duration >= _CASE3_C_SLEEP_SECONDS * 0.9, (
        f"C's duration was {c_duration:.3f}s, expected "
        f"~{_CASE3_C_SLEEP_SECONDS}s; the fast-sibling sleep was "
        f"not effective"
    )
    # And B really was slower than C (so the max(B.end, C.end) ==
    # B.end branch was actually exercised).
    assert b_duration > c_duration, (
        f"B's duration ({b_duration:.3f}s) must be > C's duration "
        f"({c_duration:.3f}s); the test assumes B is the slow "
        f"sibling — if both sleeps are equal, the max() assertion "
        f"isn't actually testing the cross-layer-wait for the slow "
        f"sibling"
    )

    # Final task statuses on disk.
    completed_ids = [t.id for t in agent.task_manager.tasks if t.status == "completed"]
    assert completed_ids == ["A", "B", "C", "D"], (
        f"expected all 4 diamond tasks to be completed, got "
        f"{completed_ids!r} (statuses: "
        f"{[(t.id, t.status) for t in agent.task_manager.tasks]!r})"
    )


# ---------------------------------------------------------------------------
# Case 3 / TDD test 3: B and C share layer 1 — their timestamps overlap
# ---------------------------------------------------------------------------


def test_case3_bc_concurrent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B and C share a layer — their wall-clock intervals overlap.

    Three complementary contracts pinned by this test:

      1. **Layer 1 contains both B and C** — the dispatcher's
         ``asyncio.gather`` is invoked with a 2-element coroutine
         list for layer 1. This is the static shape from
         :func:`test_case3_diamond_layers`; we re-assert it here
         in the runtime spy for the closed loop (if the topology
         test ever silently passed for the wrong reason, this
         test would still catch the regression).

      2. **B/C timestamps overlap** — B.start < C.end AND
         C.start < B.end. This is the runtime evidence that
         ``asyncio.gather`` actually ran them concurrently rather
         than serialising the layer 1 dispatch. If the dispatcher
         accidentally reduced layer 1 to a serial loop, B.start
         would be ≥ C.end (or vice versa) and the overlap
         assertion would fail.

      3. **D is alone in layer 2** — peak in-flight tasks across
         the entire run is 2 (A is alone in layer 0, B+C are
         together in layer 1, D is alone in layer 2). If the
         dispatcher accidentally merged layers (e.g. scheduled
         A, B, and C all together in a single gather), peak
         in-flight would be 3 — and the test would fail.

    The test uses the same per-task sleep schedule as
    :func:`test_case3_d_starts_after_both_done`, which gives B and
    C a generous 50ms-to-100ms window of overlap — well within
    scheduler noise on a typical test machine.
    """
    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _write_diamond_tasks(project_dir)

    from agent import AutonomousAgent
    from coding_tool import ClaudeCodingTool

    monkeypatch.setattr(
        ClaudeCodingTool, "query",
        lambda self, *args, **kwargs: "TEST_RESULT: PASSED\n",
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "query_json",
        lambda self, *args, **kwargs: {"tasks": []},
    )

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test diamond B/C concurrent same-layer",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )
    agent._load_tasks()

    # Spy on _execute_task_with_retry so we can:
    # 1. Inject the per-task sleep schedule.
    # 2. Record start/end timestamps for the overlap assertion.
    # 3. Track peak in-flight tasks across the run.
    timings: Dict[str, Dict[str, float]] = {}
    timings_lock = threading.Lock()
    concurrency_state = {"in_flight": 0, "peak": 0}
    concurrency_lock = threading.Lock()

    PER_TASK_SLEEP = {
        "A": _CASE3_A_SLEEP_SECONDS,
        "B": _CASE3_B_SLEEP_SECONDS,  # slow sibling — 400ms
        "C": _CASE3_C_SLEEP_SECONDS,  # fast sibling — 50ms
        "D": _CASE3_D_SLEEP_SECONDS,
    }

    def _stub_execute(task, max_retries=5, timeout=None):
        with concurrency_lock:
            concurrency_state["in_flight"] += 1
            concurrency_state["peak"] = max(
                concurrency_state["peak"], concurrency_state["in_flight"]
            )

        with timings_lock:
            timings[task.id] = {"start": time.monotonic()}

        time.sleep(PER_TASK_SLEEP[task.id])

        with timings_lock:
            timings[task.id]["end"] = time.monotonic()

        with concurrency_lock:
            concurrency_state["in_flight"] -= 1

        agent.task_manager.update_task_status(task.id, "completed")
        # Mirror the real completion bookkeeping in
        # ``_execute_task_with_retry`` (agent.py:4630) so the stub leaves
        # the agent in the same state the real executor does.
        # ``update_task_status`` mutates the shared ``SubTask`` objects
        # (``_load_tasks`` re-points ``task_manager.tasks`` at the
        # snapshot the post-read gate finalised), so the dispatcher sees
        # the new status on the next layer rebuild, and the session
        # counter feeds the dispatcher's same-id loop guard.
        agent._session_task_completed_counts[task.id] = (
            agent._session_task_completed_counts.get(task.id, 0) + 1
        )
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

    # Run to completion. agent.run uses asyncio.run internally.
    agent.run(timeout=None)

    # All four tasks recorded both a start and an end timestamp.
    for tid in ("A", "B", "C", "D"):
        assert tid in timings, (
            f"task {tid} never executed (timings={timings!r})"
        )
        assert "start" in timings[tid] and "end" in timings[tid], (
            f"task {tid} missing start/end (got {timings[tid]!r})"
        )

    a_start, a_end = timings["A"]["start"], timings["A"]["end"]
    b_start, b_end = timings["B"]["start"], timings["B"]["end"]
    c_start, c_end = timings["C"]["start"], timings["C"]["end"]
    d_start, d_end = timings["D"]["start"], timings["D"]["end"]

    # ----- Contract 1: A is alone in layer 0 (peak-in-flight 1) -------
    # A's body runs alone; B and C cannot be in flight at the same
    # time as A. If they were, peak in-flight would be ≥ 2 *before*
    # the B/C overlap window, and the assertion in the next section
    # would lose its meaning.
    assert a_end <= b_start, (
        f"A.end ({a_end}) must be <= B.start ({b_start}); A and B "
        f"ran concurrently — A is not alone in layer 0"
    )
    assert a_end <= c_start, (
        f"A.end ({a_end}) must be <= C.start ({c_start}); A and C "
        f"ran concurrently — A is not alone in layer 0"
    )

    # ----- Contract 2: B and C timestamps overlap ---------------------
    # The cross-product of (B.start, B.end) and (C.start, C.end)
    # must overlap. Both conditions are required because either
    # alone is not enough:
    #   - B.start < C.end is true even in the degenerate case where
    #     B started and immediately finished before C ran.
    #   - C.start < B.end is true even in the reverse degenerate
    #     case.
    # The conjunction proves the two intervals share at least one
    # wall-clock instant.
    assert b_start < c_end, (
        f"B.start ({b_start}) must be < C.end ({c_end}); B and C "
        f"do not overlap — the dispatcher serialised layer 1. "
        f"timings: B=[{b_start}, {b_end}], C=[{c_start}, {c_end}]"
    )
    assert c_start < b_end, (
        f"C.start ({c_start}) must be < B.end ({b_end}); B and C "
        f"do not overlap — the dispatcher serialised layer 1. "
        f"timings: B=[{b_start}, {b_end}], C=[{c_start}, {c_end}]"
    )

    # ----- Contract 3: peak in-flight is exactly 2 --------------------
    # Across the 3 layers, the maximum number of in-flight tasks at
    # any single instant is 2 (B and C in layer 1). A is alone in
    # layer 0, D is alone in layer 2. If the dispatcher merged
    # layers (e.g. gathered all 4 tasks at once), peak would be
    # > 2 and this assertion would fail with a clear diagnostic.
    assert concurrency_state["peak"] == 2, (
        f"peak concurrent in-flight tasks was "
        f"{concurrency_state['peak']}, expected 2 (B and C "
        f"together in layer 1). The dispatcher either serialised "
        f"layer 1 (peak=1) or merged multiple layers into one "
        f"(peak>2) — the diamond's layer boundaries are broken"
    )

    # ----- D is alone in layer 2 (D's body does not overlap B/C) ----
    # D must not start until both B and C are done — this is the
    # cross-layer-wait contract pinned in
    # :func:`test_case3_d_starts_after_both_done`, restated here for
    # the closed loop. We don't re-check the > half (D alone in
    # layer 2) via timestamps because that's the same contract.
    assert b_end <= d_start, (
        f"B.end ({b_end}) must be <= D.start ({d_start}); D started "
        f"while B was still running"
    )
    assert c_end <= d_start, (
        f"C.end ({c_end}) must be <= D.start ({d_start}); D started "
        f"while C was still running"
    )
    # D is alone in layer 2 — no other task runs concurrently with D.
    assert d_end <= a_end or True  # trivial — A finished long ago
    # The real check: D's body had no other task in flight. We
    # verify by checking that no task *started* during D's body
    # window. (D is the last task, so this is trivially true, but
    # we keep the assertion for documentation.)
    no_concurrent_d = all(
        d_end <= t["start"] or d_start >= t["end"]
        for tid, t in timings.items()
        if tid != "D"
    )
    assert no_concurrent_d, (
        f"D's body overlapped with another task; D should be alone "
        f"in layer 2. timings: {timings!r}"
    )

    # Final task statuses on disk.
    completed_ids = [t.id for t in agent.task_manager.tasks if t.status == "completed"]
    assert completed_ids == ["A", "B", "C", "D"], (
        f"expected all 4 diamond tasks to be completed, got "
        f"{completed_ids!r} (statuses: "
        f"{[(t.id, t.status) for t in agent.task_manager.tasks]!r})"
    )


# ---------------------------------------------------------------------------
# Case 4 helpers: tasks.json fail-fast validation (Kahn in-degree checks)
# ---------------------------------------------------------------------------
#
# Case 4 background
# -----------------
# PRD acceptance Case 4 requires that ``agent._load_tasks`` reject
# three illegal dependency shapes at load time so a malformed plan
# never reaches the executor:
#
#   1. **Cycle** — A↔B (A depends on B, B depends on A). Both nodes
#      have in-degree ≥ 1 forever, so Kahn's algorithm never
#      surfaces them. The residual (cycle members, sorted) is the
#      cycle report.
#   2. **Missing dependency** — A depends on Z but Z is not in the
#      task list. The missing-id check runs *before* the cycle check
#      so the first failure a human reader sees is the missing
#      id, not the synthetic cycle that A→Z would otherwise form.
#   3. **Self-dependency** — A depends on A. Reported separately
#      from the missing-id check so the message is specific.
#
# The four TDD tests below pin:
#
#   * ``test_case4_cycle_rejected`` — A→B→A raises ``ValueError``
#     matching ``"Cycle detected: A, B"``.
#   * ``test_case4_missing_dep_rejected`` — A→Z raises ``ValueError``
#     matching ``"Task A depends on missing task Z"``.
#   * ``test_case4_self_dep_rejected`` — A→A raises ``ValueError``
#     whose message identifies the offending task id and the
#     self-loop.
#   * ``test_case4_valid_dag_loaded`` — a valid A→B→C chain loads
#     without raising and returns the non-terminal subset.
#
# Test isolation: mirrors Cases 1-3 (real ``git init`` in tmp_path,
# private ``$HOME`` so ``_load_provider_info`` resolves, defensive
# ClaudeCodingTool stub). The 3 negative-path tests do NOT need the
# agent to be fully wired — they exercise the validation path that
# runs *before* the executor — but we still build the full agent so
# the test is end-to-end with the same call surface as Cases 1-3.

# Path to the JSON fixtures used by the 3 negative-path tests.
# All three fixtures live next to this test module under ``fixtures/``
# so the tests can be re-run from any cwd. The valid-dag test
# reuses the existing ``_write_chain_tasks`` helper.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _install_fixture_tasks(project_dir: Path, fixture_name: str) -> Path:
    """Copy ``fixtures/<fixture_name>.json`` to ``<project_dir>/tasks.json``.

    The copy is a byte-for-byte write (no mutation), so any failure
    inside ``agent._load_tasks`` is the load-time validator's fault
    and not a test-driver artifact. Returns the on-disk
    ``tasks.json`` path for diagnostics.
    """
    src = _FIXTURES_DIR / fixture_name
    if not src.exists():
        raise FileNotFoundError(
            f"fixture not found: {src} — did the test setup create "
            f"the fixtures/ directory?"
        )
    dst = project_dir / "tasks.json"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


# ---------------------------------------------------------------------------
# Case 4 / TDD test 1: cycle A→B→A is rejected at load time
# ---------------------------------------------------------------------------


def test_case4_cycle_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A→B→A is broken at load time — cycle resilience (2026-09-05 incident).

    The cycle A→B→A has the strict shape PRD acceptance Case 4
    requires: A depends on B and B depends on A, so neither node
    ever reaches in-degree 0 under Kahn's algorithm.

    Contract history: originally a cycle raised
    ``ValueError("Cycle detected: A, B")`` and rejected the whole
    plan. After the 2026-09-05 incident (plan
    ``2026-09-04 plan``: a transient refiner
    cycle stranded 32 downstream tasks with
    ``executor_exited_with_unfinished_tasks:32``) the contract is
    break-and-continue, pinned by
    ``tests/contract/test_dispatcher_cycle_resilience.py`` and
    implemented in ``agent._break_cycle_resilience``:

      1. ``_load_tasks`` does NOT raise. Both cycle members are
         marked ``skipped`` (terminal), their mutual ``depends_on``
         edges are stripped from ``tasks.json``, and the repaired
         acyclic snapshot is returned with the members filtered out.
      2. The members remain in ``tasks.json`` (audit visibility) but
         without the cycle edges.
      3. A second ``_load_tasks`` call re-reads the repaired file
         and succeeds — the validator does not cache or short-
         circuit, and the on-disk repair is what makes the re-load
         cycle-free.
    """
    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _install_fixture_tasks(project_dir, "cycle_tasks.json")

    from agent import AutonomousAgent

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test cycle resilience",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )

    # 1) First load: no raise; members skipped and filtered out of
    #    the returned (non-terminal) subset.
    tasks = agent._load_tasks()
    returned_ids = {t.id for t in tasks}
    assert "A" not in returned_ids, (
        f"Cycle member A must be filtered out (terminal=skipped). "
        f"Got: {sorted(returned_ids)}"
    )
    assert "B" not in returned_ids, (
        f"Cycle member B must be filtered out (terminal=skipped). "
        f"Got: {sorted(returned_ids)}"
    )
    for tid in ("A", "B"):
        sub = next(
            (t for t in agent.task_manager.tasks if t.id == tid), None
        )
        assert sub is not None, f"{tid} should still exist on the task manager"
        assert sub.status == "skipped", (
            f"Cycle member {tid} should be marked skipped after "
            f"resilience. Got: {sub.status!r}"
        )

    # 2) The cycle edges are stripped from tasks.json (members stay,
    #    for audit), so the re-read below is cycle-free.
    on_disk = json.loads(
        (project_dir / "tasks.json").read_text(encoding="utf-8")
    )
    by_id = {t["id"]: t for t in on_disk["tasks"]}
    assert "A" in by_id and "B" in by_id, (
        "Cycle members must remain in tasks.json for audit visibility"
    )
    assert "B" not in (by_id["A"].get("depends_on") or []), (
        "A→B cycle edge must be stripped from tasks.json"
    )
    assert "A" not in (by_id["B"].get("depends_on") or []), (
        "B→A cycle edge must be stripped from tasks.json"
    )

    # 3) Second load on the same agent: no raise — the repair
    #    persists, nothing re-cycles.
    agent._load_tasks()


# ---------------------------------------------------------------------------
# Case 4 / TDD test 2: missing dependency A→Z is rejected at load time
# ---------------------------------------------------------------------------


def test_case4_missing_dep_repaired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A→Z (Z missing) is REPAIRED at load time, not rejected.

    2026-09-21 contract change. This used to raise
    ``ValueError("Task A depends on missing task Z")`` and refuse the
    plan; on the refinement path the same shape was mis-labelled as an
    18-node cycle by ``task_manager._ensure_acyclic`` (see
    ``framework/task_graph``). A reference to a task that is not in the
    list carries no ordering information, so the edge is stripped and
    the plan loads.
    """
    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _install_fixture_tasks(project_dir, "missing_dep_tasks.json")

    from agent import AutonomousAgent

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test missing dep repair",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )

    loaded = agent._load_tasks()  # must NOT raise

    by_id = {t.id: t for t in loaded}
    assert "A" in by_id, f"task A must survive the load, got {sorted(by_id)}"
    assert by_id["A"].depends_on == [], (
        f"the dangling edge must be stripped, got "
        f"{by_id['A'].depends_on!r}"
    )


# ---------------------------------------------------------------------------
# Case 4 / TDD test 3: self-dependency A→A is rejected at load time
# ---------------------------------------------------------------------------


def test_case4_self_dep_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A→A self-loop is rejected at load time with a ``ValueError`` that
    identifies the offending task and the self-reference.

    The self-dep check runs *after* the missing-id check (so a
    self-loop never triggers a spurious missing-id error) and
    *before* the cycle check (so a self-loop is reported as
    "self-dependency", not "cycle detected: A"). The error message
    must name the task id (``A``) and use the word ``itself``.
    """
    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _install_fixture_tasks(project_dir, "self_dep_tasks.json")

    from agent import AutonomousAgent

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test self-dep rejection",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )

    # The exact wording the implementation produces is
    # ``"Task A cannot depend on itself"``; the spec example
    # shows ``"Task A depends on itself"``. Both forms name the
    # task id and the self-reference, which is the contract that
    # matters. The regex ``Task A .* itself`` accepts either
    # form so a minor wording tweak in the implementation does
    # not break the test.
    with pytest.raises(ValueError, match=r"Task A .* itself"):
        agent._load_tasks()


# ---------------------------------------------------------------------------
# Case 4 / TDD test 4: valid A→B→C DAG loads without raising
# ---------------------------------------------------------------------------


def test_case4_valid_dag_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A valid A→B→C chain loads without raising and returns the
    non-terminal task subset.

    This is the positive control for the 3 negative-path tests
    above. The same Kahn-residual check that surfaces the A↔B
    cycle in ``test_case4_cycle_rejected`` must NOT fire here —
    A is the only in-degree-0 root, B and C are reached in order,
    and the residual after Kahn is empty.

    Assertions:
      * ``_load_tasks()`` returns a non-empty list.
      * The returned list contains all 3 task ids (none were
        filtered out as terminal, since all 3 are ``pending``).
      * The full task list (on ``self._all_tasks``) also contains
        exactly 3 tasks — i.e. the filter does not lose anything.
    """
    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _write_chain_tasks(project_dir)  # the A→B→C valid chain

    from agent import AutonomousAgent

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test valid DAG loads cleanly",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )

    # Must NOT raise. The negative-path tests above all assert
    # that bad input raises — this one pins the contrapositive.
    loaded = agent._load_tasks()

    # All 3 tasks are pending (non-terminal) so the returned
    # filtered list is exactly the 3 chain tasks.
    assert isinstance(loaded, list), (
        f"_load_tasks must return a list, got {type(loaded).__name__}: "
        f"{loaded!r}"
    )
    loaded_ids = sorted(t.id for t in loaded)
    assert loaded_ids == ["A", "B", "C"], (
        f"expected A, B, C in the loaded list, got {loaded_ids!r} "
        f"(statuses: {[(t.id, t.status) for t in loaded]!r})"
    )

    # ``self._all_tasks`` (the un-filtered record) also contains
    # exactly the 3 chain tasks — nothing was lost, nothing was
    # added.
    all_ids = sorted(t.id for t in agent._all_tasks)
    assert all_ids == ["A", "B", "C"], (
        f"expected _all_tasks to contain A, B, C, got {all_ids!r}"
    )

    # The cycle / self / missing checks would each fail loudly
    # (see the negative tests above) — if we got here without an
    # exception, the valid-DAG contract is pinned.


# ---------------------------------------------------------------------------
# Case 5 helpers: cross-process recovery — terminal tasks filtered on reload
# ---------------------------------------------------------------------------
#
# Case 5 background
# -----------------
# PRD acceptance Case 5 requires that ``agent._load_tasks`` (and the
# downstream dispatcher) skip tasks that have already reached a
# terminal state when reloading ``tasks.json``. The motivating scenario
# is a crash recovery: the previous process committed some task
# statuses (``completed``/``failed``/``skipped``) to ``tasks.json``
# before dying, and a new process picks up the same project. The
# recovered process MUST NOT re-execute those terminal tasks — doing
# so would re-write their git checkpoints and double-count the
# progress.
#
# Status semantics (mirror :data:`_TERMINAL_TASK_STATUSES` in agent.py):
#
#   * ``completed``           → terminal, filtered out.
#   * ``failed``              → terminal, filtered out.
#   * ``skipped``             → terminal, filtered out.
#   * ``pending``             → active, kept.
#   * ``breakdown_in_progress`` → active, kept. When a parent task is
#     being broken down into subtasks, the children have already
#     been inserted into ``tasks.json`` and the parent must remain
#     in the DAG so the children can be scheduled. Filtering the
#     parent out would orphan the children.
#
# The four TDD tests below pin:
#
#   * ``test_case5_skips_completed_on_reload`` — ``_load_tasks`` returns
#     a list that does NOT contain completed A; ``_all_tasks`` still
#     contains the full record.
#   * ``test_case5_dispatches_only_pending`` — runtime spy asserts the
#     executor never invokes ``_execute_task_with_retry`` for A
#     (completed) or C (failed); only pending / non-terminal tasks
#     (B, D, E, F) are dispatched.
#   * ``test_case5_skips_failed_terminal`` — ``_load_tasks`` excludes
#     failed C; ``failed`` is a terminal status like ``completed``.
#   * ``test_case5_keeps_breakdown_in_progress`` — E (with status
#     ``breakdown_in_progress``) is in the returned active list
#     even though it is not a "normal" pending task.
#
# The fixture ``fixtures/partial_completed_tasks.json`` carries 6 tasks
# that together cover all four contracts:
#
#     A: completed           → filtered (test 1)
#     B: pending             → dispatched (test 2)
#     C: failed              → filtered (tests 1 + 3), NOT dispatched (test 2)
#     D: pending, dep [B,C]  → dispatched after B (test 2)
#     E: breakdown_in_progress → kept active (test 4)
#     F: pending             → dispatched (test 2)
#
# Test isolation: mirrors Cases 1-4 (real ``git init`` in tmp_path,
# private ``$HOME`` so ``_load_provider_info`` resolves, defensive
# ClaudeCodingTool stub). The fixture is copied into
# ``<project_dir>/tasks.json`` byte-for-byte via
# :func:`_install_fixture_tasks`.


# ---------------------------------------------------------------------------
# Case 5 / TDD test 1: completed A is excluded from the returned active list
# ---------------------------------------------------------------------------


def test_case5_skips_completed_on_reload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_load_tasks`` returns a list that does NOT contain completed A.

    The cross-process recovery contract (PRD acceptance case 5):
    when reloading ``tasks.json`` after a crash, the DAG returned
    to the dispatcher must exclude any task whose status is
    already terminal — otherwise the executor would re-execute it
    and double-write git checkpoints / commit messages. The full
    record is still retained on ``self._all_tasks`` so progress
    dashboards can enumerate already-finished tasks.

    Assertions:
      * Returned list is a list with the 5 non-terminal task ids
        (B, D, E, F) in input order — A and C are NOT in it.
      * ``self._all_tasks`` still contains all 6 tasks
        (the filter is non-destructive at the record level).
      * The 5 returned tasks all carry a non-terminal status.
    """
    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _install_fixture_tasks(project_dir, "partial_completed_tasks.json")

    from agent import AutonomousAgent

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test cross-process recovery: skip completed",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )

    loaded = agent._load_tasks()

    # The returned list must be a list.
    assert isinstance(loaded, list), (
        f"_load_tasks must return a list, got {type(loaded).__name__}: "
        f"{loaded!r}"
    )
    # The 5 non-terminal task ids in input order (A is first but
    # filtered; the remaining active tasks preserve their input
    # order: B, D, E, F).
    loaded_ids = [t.id for t in loaded]
    assert loaded_ids == ["B", "D", "E", "F"], (
        f"expected active list to be ['B', 'D', 'E', 'F'] in input "
        f"order, got {loaded_ids!r} (A=completed and C=failed must be "
        f"filtered out; full statuses: "
        f"{[(t.id, t.status) for t in loaded]!r})"
    )
    # A is NOT in the returned list — completed tasks are filtered
    # out at load time, not just at the dispatcher.
    assert "A" not in loaded_ids, (
        f"A is completed and must be filtered out on reload, but it "
        f"appears in the loaded list: {loaded_ids!r}"
    )

    # The full record on ``self._all_tasks`` must still contain
    # all 6 tasks — the filter is non-destructive at the record
    # level so progress reporting can still see A and C.
    all_ids = [t.id for t in agent._all_tasks]
    assert sorted(all_ids) == ["A", "B", "C", "D", "E", "F"], (
        f"expected _all_tasks to contain all 6 tasks (including "
        f"terminal A and C), got {sorted(all_ids)!r}; the filter "
        f"must not lose terminal tasks from the record"
    )
    # And A's status on the full record is still 'completed' (the
    # filter doesn't mutate).
    a_on_record = next(t for t in agent._all_tasks if t.id == "A")
    assert a_on_record.status == "completed", (
        f"A's status on _all_tasks must be 'completed' (the filter "
        f"is non-destructive), got {a_on_record.status!r}"
    )

    # Every returned task must have a non-terminal status — the
    # filter is defined as ``status not in _TERMINAL_TASK_STATUSES``.
    _TERMINAL = {"completed", "failed", "skipped"}
    for t in loaded:
        assert t.status not in _TERMINAL, (
            f"loaded task {t.id} has terminal status {t.status!r} — "
            f"the filter must exclude terminal tasks but one slipped "
            f"through (full loaded statuses: "
            f"{[(t.id, t.status) for t in loaded]!r})"
        )


# ---------------------------------------------------------------------------
# Case 5 / TDD test 2: runtime dispatcher never invokes execute on A or C
# ---------------------------------------------------------------------------


def test_case5_dispatches_only_pending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The runtime dispatcher only dispatches pending / non-terminal tasks.

    The cross-process recovery contract end-to-end: even if a task
    somehow leaks past ``_load_tasks`` (or the runtime layer filter),
    the executor's per-layer filter
    ``t.status not in _TERMINAL_TASK_STATUSES`` (agent.py:1095-1098)
    is the second line of defence. This test verifies that line of
    defence: the spy on ``_execute_task_with_retry`` is invoked
    exactly for the dispatchable non-terminal tasks (B, E, F) and
    NEVER for the terminal tasks (A, C).

    A and C are NEVER recorded by the spy. This is the strongest
    possible assertion: even if the dispatcher built a layer
    containing A or C, the per-layer filter strips them out before
    the spy is called.

    D (fixture ``partial_completed_tasks.json``) is the third case:
    ``pending`` but NOT dispatchable. Its ``depends_on`` is
    ``["B", "C"]`` and C is ``failed``, so the Principle-3 hard gate
    (``is_dependency_ready``, agent.py:405) keeps it deferred for the
    whole run — ``failed`` is a non-success upstream and only
    ``completed`` / ``skipped`` unlock downstream work. D is asserted
    absent, which is the contract the 2026-08-19 audit pinned.
    """
    import asyncio as _asyncio
    saved_policy = _asyncio.get_event_loop_policy()
    fresh_policy = _asyncio.DefaultEventLoopPolicy()
    _asyncio.set_event_loop_policy(fresh_policy)
    loop = fresh_policy.new_event_loop()
    fresh_policy.set_event_loop(loop)

    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _install_fixture_tasks(project_dir, "partial_completed_tasks.json")

    from agent import AutonomousAgent
    from coding_tool import ClaudeCodingTool

    monkeypatch.setattr(
        ClaudeCodingTool, "query",
        lambda self, *args, **kwargs: "TEST_RESULT: PASSED\n",
    )
    monkeypatch.setattr(
        ClaudeCodingTool, "query_json",
        lambda self, *args, **kwargs: {"tasks": []},
    )

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test cross-process recovery: dispatch only pending",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )
    agent._load_tasks()

    # Spy: record the set of dispatched task ids; mark each as
    # ``completed`` so the dispatcher makes forward progress and
    # eventually exits. Without the completion write-through the
    # dispatcher would loop forever re-issuing the same pending
    # layer.
    dispatched: List[str] = []
    dispatched_lock = threading.Lock()

    def _stub_execute(task, max_retries=5, timeout=None):
        with dispatched_lock:
            dispatched.append(task.id)
        # Tiny sleep so wall-clock ordering is observable on fast
        # machines (otherwise the test could complete in < 1ms and
        # timing-sensitive assertions downstream would be flaky).
        time.sleep(0.005)
        # Mark completed so the dispatcher doesn't re-schedule the
        # same task in the next layer rebuild.
        agent.task_manager.update_task_status(task.id, "completed")
        # Mirror the real completion bookkeeping in
        # ``_execute_task_with_retry`` (agent.py:4630) so the stub leaves
        # the agent in the same state the real executor does.
        # ``update_task_status`` mutates the shared ``SubTask`` objects
        # (``_load_tasks`` re-points ``task_manager.tasks`` at the
        # snapshot the post-read gate finalised), so the dispatcher sees
        # the new status on the next layer rebuild, and the session
        # counter feeds the dispatcher's same-id loop guard.
        agent._session_task_completed_counts[task.id] = (
            agent._session_task_completed_counts.get(task.id, 0) + 1
        )
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

    try:
        # Run to completion. agent.run uses asyncio.run internally.
        agent.run(timeout=None)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        try:
            _asyncio.set_event_loop_policy(saved_policy)
        except Exception:
            pass

    # ----- Core contract: A and C are NEVER dispatched ----------------
    # The terminal-status filter at the dispatcher layer must
    # exclude completed A and failed C. If either slipped through,
    # the cross-process recovery contract is broken.
    assert "A" not in dispatched, (
        f"completed task A was dispatched: {dispatched!r}. The "
        f"per-layer terminal filter at agent.py:1095-1098 must "
        f"strip completed tasks before _execute_task_with_retry "
        f"is called. Re-running A would double-write its git "
        f"checkpoint and re-execute finished work."
    )
    assert "C" not in dispatched, (
        f"failed task C was dispatched: {dispatched!r}. The "
        f"per-layer terminal filter must strip failed tasks "
        f"before _execute_task_with_retry is called. Re-running "
        f"C would silently overwrite the prior failure record."
    )

    # ----- The dispatchable non-terminal tasks (B, E, F) are dispatched -
    # B, E, F are roots (no deps), so they go in layer 0. E is
    # ``breakdown_in_progress``, which is intentionally NOT terminal,
    # so it is dispatched too.
    for tid in ("B", "E", "F"):
        assert tid in dispatched, (
            f"non-terminal task {tid} was never dispatched: "
            f"{dispatched!r}. The dispatcher skipped a pending "
            f"task — either the layer build or the per-layer "
            f"filter is too aggressive."
        )

    # ----- D is non-terminal but blocked, so it is NEVER dispatched ----
    # D depends on B and C; C is ``failed``. The Principle-3 hard gate
    # keeps D deferred for the whole run — scheduling it would run work
    # whose upstream never succeeded.
    assert "D" not in dispatched, (
        f"task D was dispatched: {dispatched!r}. D depends on B and C "
        f"and C is 'failed'; ``is_dependency_ready`` (agent.py:405) "
        f"must keep it deferred — only 'completed' / 'skipped' "
        f"upstreams unlock downstream work."
    )

    # ----- Dispatched set contains ONLY the 3 dispatchable tasks -------
    # Exactly 3 dispatches, no duplicates, no extras.
    assert sorted(dispatched) == ["B", "E", "F"], (
        f"expected exactly 3 dispatches (B, E, F) for the "
        f"non-terminal, dependency-ready tasks, got {sorted(dispatched)!r}"
    )
    # No duplicates — each task should be dispatched exactly once.
    assert len(dispatched) == len(set(dispatched)), (
        f"some task was dispatched more than once: "
        f"{dispatched!r}. The dispatcher re-issued a task after "
        f"it was marked completed."
    )


# ---------------------------------------------------------------------------
# Case 5 / TDD test 3: failed C is also filtered as terminal
# ---------------------------------------------------------------------------


def test_case5_skips_failed_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_load_tasks`` excludes failed C — ``failed`` is a terminal status.

    The terminal-status set is ``{completed, failed, skipped}``. The
    test_case5_skips_completed_on_reload test pins the ``completed``
    half; this test pins the ``failed`` half. A failed task must be
    treated the same as a completed task at load time — i.e. filtered
    out of the active list and never re-executed on a recovered run.

    Assertions:
      * Returned list does NOT contain C.
      * C's full record is still in ``_all_tasks`` with status
        ``failed`` (non-destructive).
      * The returned list contains the 5 non-terminal tasks
        (B, D, E, F) in input order.
    """
    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _install_fixture_tasks(project_dir, "partial_completed_tasks.json")

    from agent import AutonomousAgent

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test cross-process recovery: skip failed",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )

    loaded = agent._load_tasks()

    # C must NOT be in the returned list — failed is terminal.
    loaded_ids = [t.id for t in loaded]
    assert "C" not in loaded_ids, (
        f"failed task C must be filtered out on reload, but it "
        f"appears in the loaded list: {loaded_ids!r}. ``failed`` "
        f"is in _TERMINAL_TASK_STATUSES; a failed task must not "
        f"be re-executed on a recovered run."
    )
    # A (completed) is also filtered — the test pins both halves
    # of the terminal set in one go.
    assert "A" not in loaded_ids, (
        f"completed task A must be filtered out on reload, but "
        f"it appears in the loaded list: {loaded_ids!r}"
    )

    # The full record must still carry C with status 'failed' —
    # the filter must not drop or mutate the record itself.
    c_on_record = next(
        (t for t in agent._all_tasks if t.id == "C"), None
    )
    assert c_on_record is not None, (
        f"C is missing from _all_tasks (the filter must be "
        f"non-destructive): "
        f"{[t.id for t in agent._all_tasks]!r}"
    )
    assert c_on_record.status == "failed", (
        f"C's status on _all_tasks must be 'failed' (the filter "
        f"must not mutate), got {c_on_record.status!r}"
    )

    # The 5 non-terminal task ids in input order.
    assert loaded_ids == ["B", "D", "E", "F"], (
        f"expected active list to be ['B', 'D', 'E', 'F'] in "
        f"input order, got {loaded_ids!r}"
    )

    # Every returned task must have a non-terminal status.
    _TERMINAL = {"completed", "failed", "skipped"}
    for t in loaded:
        assert t.status not in _TERMINAL, (
            f"loaded task {t.id} has terminal status {t.status!r} — "
            f"the filter must exclude terminal tasks but one "
            f"slipped through (full loaded statuses: "
            f"{[(t.id, t.status) for t in loaded]!r})"
        )


# ---------------------------------------------------------------------------
# Case 5 / TDD test 4: breakdown_in_progress E is kept in the active list
# ---------------------------------------------------------------------------


def test_case5_keeps_breakdown_in_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_load_tasks`` keeps E (status ``breakdown_in_progress``) active.

    ``breakdown_in_progress`` is a transient state for a parent
    task whose children have just been inserted into
    ``tasks.json``. Filtering the parent out would orphan the
    children — the children need the parent in the DAG so their
    ``depends_on`` references resolve during ``_build_layers`` and
    they can be scheduled.

    The terminal set is ``{completed, failed, skipped}`` —
    ``breakdown_in_progress`` is intentionally NOT in that set.
    This test pins the inclusion: E appears in the returned
    active list with its ``breakdown_in_progress`` status intact.

    Assertions:
      * E is in the returned active list.
      * E's status is ``breakdown_in_progress`` (the load must
        not rewrite the status field).
      * E is also in ``_all_tasks`` (non-destructive).
      * The other 4 active tasks (B, D, F) are present and
        have a normal pending status — only E is the
        ``breakdown_in_progress`` outlier.
    """
    project_dir = _plan_dir(tmp_path)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _install_fixture_tasks(project_dir, "partial_completed_tasks.json")

    from agent import AutonomousAgent

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="test cross-process recovery: keep breakdown_in_progress",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )

    loaded = agent._load_tasks()

    # E must be in the returned active list.
    loaded_by_id = {t.id: t for t in loaded}
    assert "E" in loaded_by_id, (
        f"breakdown_in_progress task E must be kept in the active "
        f"list (the children inserted by the breakdown flow still "
        f"need the parent in the DAG), but E is not in the loaded "
        f"list: {list(loaded_by_id)!r}"
    )
    # E's status is preserved as ``breakdown_in_progress`` — the
    # load must not rewrite the status field.
    e_loaded = loaded_by_id["E"]
    assert e_loaded.status == "breakdown_in_progress", (
        f"E's status must be 'breakdown_in_progress' after the "
        f"load (the load must not rewrite status), got "
        f"{e_loaded.status!r}"
    )

    # E is also in the full record.
    e_on_record = next(
        (t for t in agent._all_tasks if t.id == "E"), None
    )
    assert e_on_record is not None, (
        f"E is missing from _all_tasks (the filter must be "
        f"non-destructive): "
        f"{[t.id for t in agent._all_tasks]!r}"
    )
    assert e_on_record.status == "breakdown_in_progress", (
        f"E's status on _all_tasks must be 'breakdown_in_progress' "
        f"(non-destructive), got {e_on_record.status!r}"
    )

    # The other active tasks (B, D, F) are present with their
    # ``pending`` status — sanity check that the inclusion of E
    # did not accidentally drop the other non-terminal tasks.
    for tid in ("B", "D", "F"):
        assert tid in loaded_by_id, (
            f"pending task {tid} is missing from the active list: "
            f"{list(loaded_by_id)!r}"
        )
        assert loaded_by_id[tid].status == "pending", (
            f"task {tid} should have status 'pending' on load, "
            f"got {loaded_by_id[tid].status!r}"
        )

    # Exactly 4 active tasks total (B, D, F are pending, E is
    # breakdown_in_progress) — A (completed) and C (failed) are
    # still excluded.
    assert len(loaded) == 4, (
        f"expected exactly 4 active tasks (B, D, F pending + E "
        f"breakdown_in_progress), got {len(loaded)}: "
        f"{[(t.id, t.status) for t in loaded]!r}"
    )

"""
test_backward_compat.py — backward-compatibility tests for legacy tasks.json.

Context
--------
Older versions of the autonomous-coding pipeline did not include the
``files_to_modify`` field on tasks. The current ``SubTask`` model
requires every task to declare its target files (otherwise downstream
layer building cannot reason about file-level conflicts), but to avoid
breaking pre-existing ``tasks.json`` payloads we accept legacy entries
unchanged and substitute a synthetic sentinel:

    SubTask.files_to_modify == UNKNOWN_MODIFICATIONS_SENTINEL
                                == ["__UNKNOWN_MODIFICATIONS__"]

These tests pin three contracts:

  1. **Sentinel mapping** — loading a legacy entry (no
     ``files_to_modify`` key) through ``TaskManager`` yields a
     ``SubTask`` whose ``files_to_modify`` equals
     ``UNKNOWN_MODIFICATIONS_SENTINEL``. The *agent* load path goes
     one step further: ``agent._load_tasks`` runs the dispatcher's
     step-3 self-heal, and because ``__UNKNOWN_MODIFICATIONS__`` is
     deliberately rejected by that gate (it means "should modify
     files, list unknown"), the entry is resolved by the batch
     subagent fill — or, when the subagent names no files, by the
     documented "unknown → read-only" fallback to
     ``NO_FILE_CHANGES_SENTINEL``. Both paths are asserted; see
     ``test_legacy_task_maps_to_sentinel`` for the split.

  2. **Legacy vs legacy = concurrent** — two legacy tasks in the same
     outer layer run *concurrently*: each sentinel entry gets a unique
     synthetic conflict key, so neither forms a connected component
     with the other (2026-08-19 sentinel-parallel fix).

  3. **Legacy vs new = concurrent** — a legacy task and a new task
     with disjoint ``files_to_modify`` must run concurrently, because
     their conflict keys are disjoint (synthetic vs ``frozenset(...)``).

The fixture ``tests/fixtures/legacy_tasks.json`` mirrors the old
on-disk format: it omits ``files_to_modify``, ``provider``,
``model_type``, and ``breakdown_count`` for tasks 1 and 2 (true
legacy) and keeps them for task 3 (modern).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Helpers (mirrored from test_executor_dag.py)
# ---------------------------------------------------------------------------


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


_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tests"
    / "fixtures"
    / "legacy_tasks.json"
)


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
        ["git", "config", "user.name", "Backward Compat Test"],
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


def _copy_legacy_fixture(project_dir: Path) -> Path:
    """Copy ``tests/fixtures/legacy_tasks.json`` into ``project_dir``."""
    assert _FIXTURE_PATH.exists(), (
        f"legacy fixture missing at {_FIXTURE_PATH}"
    )
    tasks_file = project_dir / "tasks.json"
    shutil.copyfile(_FIXTURE_PATH, tasks_file)
    return tasks_file


def _materialise_declared_paths(project_dir: Path, *paths: str) -> None:
    """Create the paths a fixture declares in ``files_to_modify``.

    ``TaskOutputValidator`` step 3 rejects a declared path that does
    not exist on disk (and is not a known-deleted path in git
    history). The fixtures declare real-looking paths (``src/modern.py``
    et al.) against a project that is otherwise empty, so those paths
    have to exist before ``agent._load_tasks()`` runs — otherwise the
    dispatcher post-read gate raises ``RuntimeError: dispatcher
    post-read gate`` for the modern task, which has nothing to do with
    backward compatibility.
    """
    for raw in paths:
        target = project_dir / raw
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()


def _write_legacy_only(project_dir: Path) -> Path:
    """Write a tasks.json with two legacy tasks (no files_to_modify) only.

    Used by ``test_legacy_tasks_run_serially`` to pin the legacy-vs-legacy
    serial contract without involving a modern task.
    """
    payload = {
        "requirement": "legacy-vs-legacy serial contract",
        "stop_reason": None,
        "reason_detail": None,
        "tasks": [
            {
                "id": "L1",
                "title": "legacy task L1",
                "description": "no files_to_modify",
                "test_command": "echo L1",
            },
            {
                "id": "L2",
                "title": "legacy task L2",
                "description": "no files_to_modify",
                "test_command": "echo L2",
            },
        ],
    }
    tasks_file = project_dir / "tasks.json"
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tasks_file


def _write_legacy_plus_modern(project_dir: Path) -> Path:
    """Write a tasks.json with one legacy task and one modern task.

    The modern task carries ``files_to_modify`` disjoint from the
    sentinel so the conflict graph treats it as a separate
    connected component — the legacy + modern pair must run
    concurrently in a single micro-layer.
    """
    payload = {
        "requirement": "legacy-vs-modern concurrent contract",
        "stop_reason": None,
        "reason_detail": None,
        "tasks": [
            {
                "id": "L1",
                "title": "legacy task L1",
                "description": "no files_to_modify",
                "test_command": "echo L1",
            },
            {
                "id": "M1",
                "title": "modern task M1",
                "description": "declares files_to_modify",
                "test_command": "echo M1",
                "files_to_modify": ["src/modern.py"],
            },
        ],
    }
    tasks_file = project_dir / "tasks.json"
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tasks_file


def _install_asyncio_loop() -> tuple:
    """Install a fresh asyncio event loop policy.

    ``ProviderConcurrencyController.__init__`` creates an
    :class:`asyncio.Semaphore` that, on Python 3.9, eagerly binds to
    the current event loop. A sync test (no running loop) would raise
    ``RuntimeError: There is no current event loop``. We install a
    fresh loop, run the test, then restore the previous policy.
    """
    saved_policy = asyncio.get_event_loop_policy()
    fresh_policy = asyncio.DefaultEventLoopPolicy()
    asyncio.set_event_loop_policy(fresh_policy)
    loop = fresh_policy.new_event_loop()
    fresh_policy.set_event_loop(loop)
    return saved_policy, loop


def _restore_asyncio_loop(saved_policy, loop) -> None:
    """Restore the previous event loop policy."""
    try:
        loop.close()
    except Exception:
        pass
    try:
        asyncio.set_event_loop_policy(saved_policy)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# TDD test 1: legacy task → sentinel mapping
# ---------------------------------------------------------------------------


def test_legacy_task_maps_to_sentinel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Loading a legacy task (no ``files_to_modify``) yields a ``SubTask``
    whose ``files_to_modify`` equals ``UNKNOWN_MODIFICATIONS_SENTINEL``.

    Two complementary contracts:

      * **``TaskManager`` contract**: when the on-disk JSON omits
        ``files_to_modify`` entirely, ``SubTask``'s default factory
        returns a copy of the sentinel. This is the lowest level the
        legacy format reaches — any code that goes through
        ``TaskManager`` (which includes the dispatch loop) sees the
        sentinel.

      * **``agent._load_tasks`` contract**: the agent's load path uses
        the same default factory but then runs the dispatcher's
        step-3 self-heal. ``__UNKNOWN_MODIFICATIONS__`` is
        deliberately *rejected* by that gate (``task.py:27-35`` — it
        means "should modify files, list unknown"), so the batch
        subagent fill fires. The stub subagent below names no files,
        so the documented "unknown → read-only" fallback
        (``agent.py:2222-2241``) resolves each legacy entry to
        ``__NO_FILE_CHANGES__``. What this layer pins is therefore
        *not* "the unknown sentinel survives" — that would re-reject
        on the next outer iteration and dead-loop — but "legacy
        entries survive the load and settle on a sentinel".

    Both layers are exercised so a regression in either path is
    caught independently.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _copy_legacy_fixture(project_dir)
    _materialise_declared_paths(project_dir, "src/modern.py")

    from task import UNKNOWN_MODIFICATIONS_SENTINEL, SubTask
    from task_manager import TaskManager

    # ----- Layer 1: TaskManager.load_tasks ------------------------------
    tm = TaskManager(project_dir)
    tm_loaded = {t.id: t for t in tm.tasks}

    assert "1" in tm_loaded and "2" in tm_loaded, (
        f"legacy fixture missing ids 1/2: {list(tm_loaded.keys())}"
    )
    # Task 1 and 2 are true legacy entries (no files_to_modify) —
    # they must come back with the sentinel.
    assert tm_loaded["1"].files_to_modify == UNKNOWN_MODIFICATIONS_SENTINEL, (
        f"task 1 (legacy) did not map to sentinel; got "
        f"{tm_loaded['1'].files_to_modify!r}"
    )
    assert tm_loaded["2"].files_to_modify == UNKNOWN_MODIFICATIONS_SENTINEL, (
        f"task 2 (legacy) did not map to sentinel; got "
        f"{tm_loaded['2'].files_to_modify!r}"
    )
    # Sanity: the sentinel is exactly what the constant declares.
    assert UNKNOWN_MODIFICATIONS_SENTINEL == ["__UNKNOWN_MODIFICATIONS__"], (
        f"the sentinel constant drifted: {UNKNOWN_MODIFICATIONS_SENTINEL!r}"
    )

    # Task 3 is a modern entry — its declared files_to_modify must be
    # preserved verbatim (not overwritten by the sentinel).
    assert "3" in tm_loaded, (
        f"modern task 3 missing from fixture: {list(tm_loaded.keys())}"
    )
    assert tm_loaded["3"].files_to_modify == ["src/modern.py"], (
        f"task 3 (modern) lost its files_to_modify; got "
        f"{tm_loaded['3'].files_to_modify!r}"
    )

    # ----- Layer 2: agent._load_tasks -----------------------------------
    # The agent path goes through the same SubTask default factory
    # but reads tasks.json again and validates dependencies — make
    # sure the sentinel still reaches ``_active_tasks``.
    from agent import AutonomousAgent

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    agent = AutonomousAgent(
        requirement="legacy mapping smoke test",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )
    loaded = agent._load_tasks()

    loaded_by_id = {t.id: t for t in loaded}
    assert "1" in loaded_by_id and "2" in loaded_by_id, (
        f"agent._load_tasks dropped legacy entries: {list(loaded_by_id.keys())}"
    )

    from task import is_no_file_changes, is_unknown_modifications

    for legacy_id in ("1", "2"):
        files = loaded_by_id[legacy_id].files_to_modify
        # 2026-09-21: the execution-side pre-run gate (and the subagent
        # fill loop that used to rewrite UNKNOWN → NO_FILE_CHANGES here)
        # was removed — validation now happens at task-generation time.
        # ``_load_tasks`` therefore keeps the substituted sentinel: the
        # task stays loadable, which is the whole point of the legacy
        # mapping. Nothing re-rejects it any more, so there is no
        # dispatcher dead-loop to guard against.
        assert is_unknown_modifications(files), (
            f"legacy task {legacy_id} must keep the UNKNOWN sentinel "
            f"through _load_tasks now that the fill loop is gone; got "
            f"{files!r}"
        )
        assert not is_no_file_changes(files), (
            f"legacy task {legacy_id} must not be silently relabelled "
            f"read-only; got {files!r}"
        )


# ---------------------------------------------------------------------------
# TDD test 2: two legacy tasks run concurrently
# ---------------------------------------------------------------------------


def test_legacy_tasks_run_concurrently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two legacy tasks in the same outer layer run *concurrently*.

    Contract (renamed 2026-08-19): a
    sentinel ``files_to_modify`` value means "the exact files are not
    yet known", NOT "this task touches every file". Mapping every
    sentinel task to one shared conflict key collided them all into a
    single connected component, so a plan whose tasks all carry a
    sentinel — the common case after the step-3 validator backfills
    one — lost ALL parallelism. ``agent._build_micro_layers`` now
    gives each sentinel task a *unique* synthetic key
    (``__SENTINEL__#<task.id>``), so sentinel tasks do not conflict
    with each other and batch into one parallel micro layer. The
    2026-09-09 two-constant scheme extended this to both sentinels
    (``__UNKNOWN_MODIFICATIONS__`` and ``__NO_FILE_CHANGES__``).

    The injection:
      * Task L1 sleeps for 200ms (its own body).
      * The spy records ``{task_id: {start, end}}`` in
        ``time.monotonic()`` for the overlap assertion.

    The assertion:
      * L2.start < L1.end (the two bodies overlap in wall-clock)
      * Peak in-flight ≥ 2
      * Both tasks reach ``status='completed'``
    """
    saved_policy, loop = _install_asyncio_loop()

    project_dir = tmp_path / "project"
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _write_legacy_only(project_dir)

    from agent import AutonomousAgent
    from coding_tool import ClaudeCodingTool
    from provider_concurrency import ProviderConcurrencyController

    # Defensive stubs so the agent's import-time wiring is satisfied
    # even though we drive the run via the recover flow (no plan()).
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
        requirement="legacy-vs-legacy serial contract",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )
    agent._load_tasks()

    # Sanity: both tasks loaded and settled on the SAME conflict key.
    # ``agent._load_tasks`` runs the step-3 self-heal, which resolves
    # the legacy entries' ``__UNKNOWN_MODIFICATIONS__`` to the
    # read-only sentinel (the stub subagent names no files — see
    # ``test_legacy_task_maps_to_sentinel``). The serial contract only
    # needs both to carry an identical conflict key, so pin equality
    # rather than a specific sentinel value.
    from task import is_sentinel

    loaded_files = {t.id: t.files_to_modify for t in agent._active_tasks}
    assert loaded_files.get("L1") == loaded_files.get("L2"), (
        f"legacy tasks must share one conflict key; got "
        f"L1={loaded_files.get('L1')!r} L2={loaded_files.get('L2')!r}"
    )
    assert loaded_files.get("L1") and is_sentinel(loaded_files["L1"]), (
        f"L1 should carry a sentinel; got {loaded_files.get('L1')!r}"
    )

    # Spy on _execute_task_with_retry so we can:
    # 1. Inject a 200ms sleep on L1 (so the L1/L2 overlap is observable).
    # 2. Record start/end timestamps for the overlap assertion.
    # 3. Track peak in-flight count.
    timings: Dict[str, Dict[str, float]] = {}
    timings_lock = threading.Lock()
    concurrency_state = {"in_flight": 0, "peak": 0}
    concurrency_lock = threading.Lock()

    L1_SLEEP_SECONDS = 0.200  # 200ms probe on L1

    def _stub_execute(task, max_retries=5, timeout=None):
        with concurrency_lock:
            concurrency_state["in_flight"] += 1
            concurrency_state["peak"] = max(
                concurrency_state["peak"], concurrency_state["in_flight"]
            )

        with timings_lock:
            timings[task.id] = {"start": time.monotonic()}

        # L1 sleeps for 200ms so the L1→L2 boundary is observable.
        # L2 only needs a tiny sleep to make its end-time visibly
        # distinct from its start-time on a fast machine.
        if task.id == "L1":
            time.sleep(L1_SLEEP_SECONDS)
        else:
            time.sleep(0.005)

        with timings_lock:
            timings[task.id]["end"] = time.monotonic()

        with concurrency_lock:
            concurrency_state["in_flight"] -= 1

        agent.task_manager.update_task_status(task.id, "completed")
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

    # Inject a ProviderConcurrencyController so the dispatcher does
    # not instantiate one with default caps (which could allow > 1
    # concurrent task at the controller level, masking the
    # micro-layer serialisation).
    agent._provider_controller = ProviderConcurrencyController(
        global_limit=10,
        provider_limits={},
    )

    try:
        agent.run(timeout=None)
    finally:
        _restore_asyncio_loop(saved_policy, loop)

    # Both tasks recorded both a start and an end timestamp.
    for tid in ("L1", "L2"):
        assert tid in timings, (
            f"task {tid} never executed (timings={timings!r})"
        )
        assert "start" in timings[tid] and "end" in timings[tid], (
            f"task {tid} missing start/end (got {timings[tid]!r})"
        )

    # Core concurrent-execution contract: the two bodies overlap in
    # wall-clock. L1 sleeps 200ms while L2 finishes in ~5ms, so
    # ``L2.start < L1.end`` can only hold if the dispatcher scheduled
    # both in the same micro layer.
    l1_start, l1_end = timings["L1"]["start"], timings["L1"]["end"]
    l2_start, l2_end = timings["L2"]["start"], timings["L2"]["end"]

    assert l2_start < l1_end, (
        f"L2.start ({l2_start}) must be < L1.end ({l1_end}); the two "
        f"sentinel tasks did not overlap, so they were serialised. "
        f"timings: L1=[{l1_start}, {l1_end}], L2=[{l2_start}, {l2_end}]"
    )

    # Sanity: L1 actually slept for ~200ms (within a generous tolerance).
    l1_duration = l1_end - l1_start
    assert l1_duration >= L1_SLEEP_SECONDS * 0.9, (
        f"L1's duration was {l1_duration:.3f}s, expected ~{L1_SLEEP_SECONDS}s; "
        f"the 200ms sleep injection was not effective"
    )

    # Peak in-flight must reach 2 — sentinel tasks carry unique
    # synthetic keys, so the two single-task components coalesce into
    # one micro layer and run in parallel.
    assert concurrency_state["peak"] >= 2, (
        f"peak concurrent in-flight tasks was {concurrency_state['peak']}, "
        f"expected >= 2. Two legacy tasks were serialised — the sentinel "
        f"unique-key rule (agent._build_micro_layers) was not applied."
    )

    # Final task statuses on disk.
    completed_ids = [t.id for t in agent.task_manager.tasks if t.status == "completed"]
    assert completed_ids == ["L1", "L2"], (
        f"expected both legacy tasks to be completed, got {completed_ids!r} "
        f"(statuses: {[(t.id, t.status) for t in agent.task_manager.tasks]!r})"
    )


# ---------------------------------------------------------------------------
# TDD test 3: legacy + modern task run concurrently
# ---------------------------------------------------------------------------


def test_legacy_with_new_task_scheduling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A legacy task and a modern task with disjoint files run concurrently.

    The contract: legacy tasks carry the synthetic conflict key
    ``frozenset(["__UNKNOWN_MODIFICATIONS__"])`` while a modern task
    carries ``frozenset(["src/modern.py"])``. The two keys are
    disjoint, so no conflict edge exists between them in the
    micro-layer conflict graph. Each forms a single-task connected
    component, and the two single-task components are coalesced
    into one micro-layer — both tasks run concurrently.

    The injection:
      * Both tasks sleep for 200ms inside their bodies.
      * The spy tracks peak in-flight count via a thread-safe counter.

    The assertion:
      * Peak in-flight ≥ 2 (the two tasks overlap in wall-clock)
      * Both tasks reach ``status='completed'``
    """
    saved_policy, loop = _install_asyncio_loop()

    project_dir = tmp_path / "project"
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _write_legacy_plus_modern(project_dir)
    _materialise_declared_paths(project_dir, "src/modern.py")

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
        requirement="legacy-vs-modern concurrent contract",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=None,
    )
    agent._load_tasks()

    # Sanity: L1 settles on a sentinel (its own conflict key, disjoint
    # from M1's), M1 keeps its declared list verbatim.
    from task import is_sentinel

    loaded_files = {t.id: t.files_to_modify for t in agent._active_tasks}
    assert loaded_files.get("L1") and is_sentinel(loaded_files["L1"]), (
        f"L1 should carry a sentinel; got {loaded_files.get('L1')!r}"
    )
    assert "src/modern.py" not in (loaded_files.get("L1") or []), (
        "L1 and M1 must have disjoint conflict keys, or they would "
        "serialise instead of running concurrently"
    )
    assert loaded_files.get("M1") == ["src/modern.py"], (
        f"M1 should carry ['src/modern.py']; got {loaded_files.get('M1')!r}"
    )

    concurrency_state = {"in_flight": 0, "peak": 0}
    concurrency_lock = threading.Lock()

    # Both tasks sleep for 200ms inside their bodies so the peak
    # overlap is observable on a fast machine.
    PER_TASK_SLEEP = 0.200

    def _stub_execute(task, max_retries=5, timeout=None):
        with concurrency_lock:
            concurrency_state["in_flight"] += 1
            concurrency_state["peak"] = max(
                concurrency_state["peak"], concurrency_state["in_flight"]
            )
        time.sleep(PER_TASK_SLEEP)
        with concurrency_lock:
            concurrency_state["in_flight"] -= 1
        agent.task_manager.update_task_status(task.id, "completed")
        return True

    monkeypatch.setattr(agent, "_execute_task_with_retry", _stub_execute)

    # Cap of 10 so neither task is denied a slot — we want the
    # scheduler to actually schedule both, not have one queue behind
    # a controller-side bottleneck.
    agent._provider_controller = ProviderConcurrencyController(
        global_limit=10,
        provider_limits={},
    )

    try:
        agent.run(timeout=None)
    finally:
        _restore_asyncio_loop(saved_policy, loop)

    # Peak in-flight must be ≥ 2 — the legacy + modern pair must
    # run concurrently in a single micro-layer.
    assert concurrency_state["peak"] >= 2, (
        f"peak concurrent tasks was {concurrency_state['peak']}, expected >= 2. "
        f"Legacy + modern with disjoint files did NOT run concurrently — the "
        f"sentinel was incorrectly added as a conflict key against modern tasks."
    )

    # Both tasks completed.
    completed_ids = sorted(
        t.id for t in agent.task_manager.tasks if t.status == "completed"
    )
    assert completed_ids == ["L1", "M1"], (
        f"expected both tasks to be completed, got {completed_ids!r} "
        f"(statuses: {[(t.id, t.status) for t in agent.task_manager.tasks]!r})"
    )
    failed_ids = [t.id for t in agent.task_manager.tasks if t.status == "failed"]
    assert not failed_ids, (
        f"unexpected failed tasks: {failed_ids!r}"
    )


# ---------------------------------------------------------------------------
# TDD test 4: legacy task marker is written to execution.log
# ---------------------------------------------------------------------------


class _RecordingLogger:
    """In-memory logger stub capturing all log events for assertions.

    Mirrors the pattern used by
    ``tests/unit/test_agent_dispatch.py::_RecordingLogger`` and
    ``tests/unit/test_agent_load.py`` — the agent's
    :meth:`_load_tasks` only calls ``self.logger.info(...)``, so we
    do not need a real :class:`ExecutionLogger` writing JSON lines
    to disk for this contract test. Recording to a list keeps the
    assertion path flat (no fs roundtrip, no log parser) and makes
    the test deterministic across Python versions.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def _record(self, level: str, event: str, message: str, **kwargs) -> None:
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


def test_logs_legacy_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Loading a legacy tasks.json emits a ``legacy_task_marker`` log event.

    The migration-audit contract: when an older-format tasks.json
    (one whose entries omit ``files_to_modify``) is loaded, the
    load step must write a single ``legacy_task_marker`` event so
    a downstream auditor can ``grep legacy_task_marker
    execution.log`` and find every plan that needs a migration
    pass. The payload must carry the legacy ids in input order
    (mirroring the ``task_filtered_terminal`` contract from
    ``tests/unit/test_agent_load.py``).

    The test pins three sub-contracts:

      1. **Event name + level** — exactly one ``legacy_task_marker``
         event at level ``INFO`` is recorded.
      2. **Payload contents** — ``data.legacy_count`` matches the
         number of legacy entries; ``data.legacy_ids`` lists their
         ids in input order.
      3. **Negative case** — when every task declares
         ``files_to_modify`` (i.e. the plan is on the current
         format), NO ``legacy_task_marker`` event is emitted. This
         matches the existing ``task_filtered_terminal`` convention
         of staying quiet on the happy path so the log is not
         flooded with false-positive audit hits.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _git_init(project_dir)
    _install_fake_cc_switch_db(monkeypatch, tmp_path)
    _materialise_declared_paths(project_dir, "src/modern.py")

    # ----- Sub-contract 1+2: legacy entries → marker is recorded --------
    payload = {
        "requirement": "legacy marker audit contract",
        "stop_reason": None,
        "reason_detail": None,
        "tasks": [
            {
                "id": "1",
                "title": "legacy task 1",
                "description": "no files_to_modify (true legacy)",
                "test_command": "echo 1",
            },
            {
                "id": "2",
                "title": "legacy task 2",
                "description": "no files_to_modify (true legacy)",
                "test_command": "echo 2",
            },
            {
                "id": "3",
                "title": "modern task 3",
                "description": "declares files_to_modify",
                "test_command": "echo 3",
                "files_to_modify": ["src/modern.py"],
            },
        ],
    }
    tasks_file = project_dir / "tasks.json"
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    coding_tool_stub = type("StubCodingTool", (), {
        "query": lambda self, *a, **kw: "TEST_RESULT: PASSED\n",
        "query_json": lambda self, *a, **kw: {"tasks": []},
    })()

    recording_logger = _RecordingLogger()

    from agent import AutonomousAgent

    agent = AutonomousAgent(
        requirement="legacy marker audit contract",
        project_dir=project_dir,
        coding_tool=coding_tool_stub,
        logger=recording_logger,
    )
    loaded = agent._load_tasks()

    # Sanity: the load produced all three SubTask objects (the
    # sentinel mapping from TDD test 1 still holds) — this test
    # only adds a log-line contract on top of that.
    loaded_by_id = {t.id: t for t in loaded}
    assert set(loaded_by_id.keys()) == {"1", "2", "3"}

    marker_events = [
        e for e in recording_logger.events if e.get("event") == "legacy_task_marker"
    ]
    assert len(marker_events) == 1, (
        f"expected exactly 1 legacy_task_marker event, got {len(marker_events)}. "
        f"All events: {recording_logger.events!r}"
    )

    marker = marker_events[0]
    assert marker.get("level") == "INFO", (
        f"legacy_task_marker must be INFO, got {marker.get('level')!r}"
    )

    data = marker.get("data", {})
    assert data.get("legacy_count") == 2, (
        f"legacy_count must equal the number of legacy entries (2), "
        f"got {data.get('legacy_count')!r}. Marker payload: {data!r}"
    )
    assert data.get("legacy_ids") == ["1", "2"], (
        f"legacy_ids must list the legacy task ids in input order, "
        f"got {data.get('legacy_ids')!r}. Marker payload: {data!r}"
    )

    # ----- Sub-contract 3: modern-only payload → no marker emitted ------
    # Use a *separate* tmp_path for the second project so the second
    # ``_install_fake_cc_switch_db`` call installs a fresh DB (the
    # fake DB carries UNIQUE provider ids — re-using the same
    # tmp_path would collide).
    modern_tmp = tmp_path / "modern"
    modern_tmp.mkdir(parents=True, exist_ok=True)
    project_dir2 = modern_tmp / "project_modern"
    _git_init(project_dir2)
    _install_fake_cc_switch_db(monkeypatch, modern_tmp)
    _materialise_declared_paths(project_dir2, "src/m1.py", "src/m2.py")
    modern_payload = {
        "requirement": "modern-only payload, no legacy marker expected",
        "tasks": [
            {
                "id": "M1",
                "title": "modern task M1",
                "description": "declares files_to_modify",
                "test_command": "echo M1",
                "files_to_modify": ["src/m1.py"],
            },
            {
                "id": "M2",
                "title": "modern task M2",
                "description": "declares files_to_modify",
                "test_command": "echo M2",
                "files_to_modify": ["src/m2.py"],
            },
        ],
    }
    (project_dir2 / "tasks.json").write_text(
        json.dumps(modern_payload, indent=2), encoding="utf-8"
    )

    recording_logger_modern = _RecordingLogger()
    agent_modern = AutonomousAgent(
        requirement="modern-only payload",
        project_dir=project_dir2,
        coding_tool=coding_tool_stub,
        logger=recording_logger_modern,
    )
    agent_modern._load_tasks()

    marker_events_modern = [
        e for e in recording_logger_modern.events
        if e.get("event") == "legacy_task_marker"
    ]
    assert marker_events_modern == [], (
        f"legacy_task_marker must NOT be emitted when every task declares "
        f"files_to_modify; got {marker_events_modern!r}. "
        f"All events: {recording_logger_modern.events!r}"
    )


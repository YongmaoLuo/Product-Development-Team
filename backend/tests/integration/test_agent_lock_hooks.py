"""
Integration tests for the pre-task / post-task file-lock hooks in
``AutonomousAgent._run_task_with_provider_slot``.

These tests verify PRD decision point 4: in-memory conflict detection
combined with OS-level advisory locks (``FileLockManager``) around task
execution.

2026-09-13 — migrated off the removed module-level API. The TC-006 /
AC-009 / VP-024 dependency-injection refactor (commit ``f134499``)
deleted the process-wide ``agent._IN_FLIGHT_FILES`` / ``agent._IN_FLIGHT_LOCK``
module globals and moved the in-flight map onto the agent instance
(``AutonomousAgent._in_flight_files`` / ``._in_flight_lock``). This file
kept importing the module globals, so it raised ``ImportError`` at
collection time and aborted the entire ``backend/tests/`` run with exit
code 2. Every assertion below now reads the *owning agent's* map; there
is no process-wide map to reset between tests, and each test builds its
own agent, so the previous autouse ``_clean_in_flight`` fixture is gone.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent import AutonomousAgent
from file_lock_manager import FileLockManager
from provider_concurrency import ProviderConcurrencyController
from task import SubTask


def _git_init(project_dir: Path) -> None:
    """Initialize a git repo so ``GitManager`` can be constructed."""
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
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
        ["git", "config", "user.name", "Lock Hook Test"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
    )


def _make_agent(project_dir: Path) -> AutonomousAgent:
    """Build an ``AutonomousAgent`` with a mocked coding tool."""
    mock_coding_tool = Mock()
    return AutonomousAgent(
        requirement=None,
        project_dir=project_dir,
        coding_tool=mock_coding_tool,
    )


def _lock_file_path(agent, file_path: str) -> Path:
    """The lock file this agent uses for ``file_path``.

    Read off the agent rather than recomputed: the location is
    ``plans/<plan_id>/locks/`` (or a workspace-derived fallback when the
    agent has no plan directory), and a test that rebuilt the formula
    would keep passing against a location production had moved away from.
    """
    from file_lock_protocol import lock_file_path

    return lock_file_path(agent._locks_dir, agent.project_dir, file_path)


def test_pre_task_acquires_locks(tmp_path: Path) -> None:
    """Pre-task hook acquires OS-level locks for all target files."""
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _make_agent(project_dir)

    task = SubTask(
        id="1",
        title="lock test",
        description="Modify backend/agent.py and backend/file_lock_manager.py\nFILE: backend/agent.py\nFILE: backend/file_lock_manager.py",
        test_command="echo ok",
    )

    files, manager = agent._pre_task_lock_hook(task)

    assert sorted(files) == ["backend/agent.py", "backend/file_lock_manager.py"]
    for file_path in files:
        lock_path = _lock_file_path(agent, file_path)
        assert lock_path.exists(), f"lock file missing for {file_path}"

    agent._post_task_lock_hook(task.id, files, manager)


def test_post_task_releases_locks(tmp_path: Path) -> None:
    """Post-task hook releases OS-level locks and clears in-flight state."""
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _make_agent(project_dir)

    task = SubTask(
        id="1",
        title="lock test",
        description="FILE: backend/agent.py",
        test_command="echo ok",
    )

    files, manager = agent._pre_task_lock_hook(task)
    assert files == ["backend/agent.py"]
    assert _lock_file_path(agent, "backend/agent.py").exists()

    agent._post_task_lock_hook(task.id, files, manager)

    # In-flight memory state must be cleared.
    assert "backend/agent.py" not in agent._in_flight_files

    # The OS lock must be released: a fresh manager can acquire it.
    fresh = FileLockManager()
    fresh.acquire(["backend/agent.py"], str(project_dir))
    fresh.release()


def test_in_memory_conflict_check(tmp_path: Path) -> None:
    """In-memory conflict detection (PRE-OS lock) raises RuntimeError."""
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _make_agent(project_dir)

    task_a = SubTask(
        id="A",
        title="first task",
        description="FILE: backend/agent.py",
        test_command="echo A",
    )
    task_b = SubTask(
        id="B",
        title="second task",
        description="FILE: backend/agent.py",
        test_command="echo B",
    )

    files_a, manager_a = agent._pre_task_lock_hook(task_a)
    assert files_a == ["backend/agent.py"]

    with pytest.raises(RuntimeError, match="File conflict detected"):
        agent._pre_task_lock_hook(task_b)

    # Task A's locks remain intact; only the second attempt is rejected.
    assert agent._in_flight_files.get("backend/agent.py") == "A"
    assert _lock_file_path(agent, "backend/agent.py").exists()

    agent._post_task_lock_hook(task_a.id, files_a, manager_a)


def test_in_flight_files_cleared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After task ends (normal OR exception), the in-flight map is cleared.

    A subsequent task using the same ``files_to_modify`` must be able to
    acquire its locks without hitting the in-memory conflict check.
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _make_agent(project_dir)

    task_a = SubTask(
        id="A",
        title="first task",
        description="FILE: backend/agent.py\nFILE: backend/file_lock_manager.py",
        test_command="echo A",
    )
    task_b = SubTask(
        id="B",
        title="second task reusing same files",
        description="FILE: backend/agent.py\nFILE: backend/file_lock_manager.py",
        test_command="echo B",
    )

    files_a, manager_a = agent._pre_task_lock_hook(task_a)
    assert sorted(files_a) == ["backend/agent.py", "backend/file_lock_manager.py"]
    assert agent._in_flight_files.get("backend/agent.py") == "A"
    assert agent._in_flight_files.get("backend/file_lock_manager.py") == "A"

    # --- Scenario 1: normal completion clears the in-flight map. ---
    agent._post_task_lock_hook(task_a.id, files_a, manager_a)
    assert "backend/agent.py" not in agent._in_flight_files
    assert "backend/file_lock_manager.py" not in agent._in_flight_files

    # --- Scenario 2: a new task can reuse the same files_to_modify. ---
    files_b, manager_b = agent._pre_task_lock_hook(task_b)
    assert sorted(files_b) == ["backend/agent.py", "backend/file_lock_manager.py"]
    assert agent._in_flight_files.get("backend/agent.py") == "B"
    assert agent._in_flight_files.get("backend/file_lock_manager.py") == "B"

    agent._post_task_lock_hook(task_b.id, files_b, manager_b)
    assert "backend/agent.py" not in agent._in_flight_files
    assert "backend/file_lock_manager.py" not in agent._in_flight_files

    # --- Scenario 3: exception path also clears the in-flight map. ---
    task_c = SubTask(
        id="C",
        title="exploding task",
        description="FILE: backend/agent.py",
        test_command="echo C",
    )
    controller = ProviderConcurrencyController(global_limit=5)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated task failure")

    monkeypatch.setattr(agent, "_execute_task_with_retry", _boom)

    with pytest.raises(RuntimeError, match="simulated task failure"):
        asyncio.run(agent._run_task_with_provider_slot(task_c, controller, timeout=1))

    assert "backend/agent.py" not in agent._in_flight_files

    # --- Scenario 4: another task can reuse the same file after exception. ---
    task_d = SubTask(
        id="D",
        title="task after explosion",
        description="FILE: backend/agent.py",
        test_command="echo D",
    )
    files_d, manager_d = agent._pre_task_lock_hook(task_d)
    assert files_d == ["backend/agent.py"]
    assert agent._in_flight_files.get("backend/agent.py") == "D"
    agent._post_task_lock_hook(task_d.id, files_d, manager_d)
    assert "backend/agent.py" not in agent._in_flight_files


@pytest.mark.asyncio
async def test_releases_lock_on_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If task execution raises, the finally block still releases locks."""
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _make_agent(project_dir)

    task = SubTask(
        id="1",
        title="boom",
        description="FILE: backend/agent.py",
        test_command="echo boom",
    )

    def _boom(*args, **kwargs) -> bool:
        raise RuntimeError("simulated task failure")

    monkeypatch.setattr(agent, "_execute_task_with_retry", _boom)

    controller = ProviderConcurrencyController(global_limit=5)

    with pytest.raises(RuntimeError, match="simulated task failure"):
        await agent._run_task_with_provider_slot(task, controller, timeout=1)

    # In-flight state must be cleared even though execution raised.
    assert "backend/agent.py" not in agent._in_flight_files

    # OS lock must be released.
    fresh = FileLockManager()
    fresh.acquire(["backend/agent.py"], str(project_dir))
    fresh.release()


def test_env_var_disables_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PDT_DISABLE_IN_FLIGHT_CHECK=1 suppresses the in-memory RuntimeError.

    Scenario: the agent already has ``backend/agent.py`` recorded as held
    by another task. We then ask the *same* agent to acquire that file.

    Without the env var, ``_pre_task_lock_hook`` raises
    ``RuntimeError("File conflict detected ...")``. With
    ``PDT_DISABLE_IN_FLIGHT_CHECK=1`` set, that specific in-memory
    RuntimeError is suppressed; the OS-level ``FileLockManager`` is
    the only remaining protection (and it operates on a different lock
    file path for this agent's ``tmp_path``).
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)

    # Simulate an in-flight task A holding the file. The map is owned by
    # the agent instance (TC-006 DI refactor), so the conflict has to be
    # recorded on the same agent instance we then exercise — a freshly
    # constructed agent would start with an empty map and never conflict.
    agent = _make_agent(project_dir)
    with agent._in_flight_lock:
        agent._in_flight_files["backend/agent.py"] = "A"

    task = SubTask(
        id="B",
        title="conflicting task",
        description="FILE: backend/agent.py",
        test_command="echo B",
    )

    # --- Control: env var NOT set -> in-memory RuntimeError raised. ----
    monkeypatch.delenv("PDT_DISABLE_IN_FLIGHT_CHECK", raising=False)
    with pytest.raises(RuntimeError, match="File conflict detected"):
        agent._pre_task_lock_hook(task)

    # --- With env var SET -> in-memory RuntimeError is suppressed. -----
    monkeypatch.setenv("PDT_DISABLE_IN_FLIGHT_CHECK", "1")
    # This must NOT raise RuntimeError("File conflict detected"). It
    # may succeed (returning files + manager) or fail at the OS-lock
    # layer for an unrelated reason; the assertion is specifically
    # that the in-memory conflict check is disabled.
    files, manager = agent._pre_task_lock_hook(task)
    assert files == ["backend/agent.py"]
    agent._post_task_lock_hook(task.id, files, manager)


# ── 2026-09-23: conflict keys come from ``files_to_modify`` ────────────
#
# A plan's first micro layer ran tasks 1 and 2 in parallel — which
# was correct, their declared ``files_to_modify`` do not overlap — and
# then the runtime guard failed task 2 with
#
#     File conflict detected: native_ext/tests/lifecycle.rs
#     is already being modified by task 1
#
# Task 1 never claimed that file. Its description only *cites* it:
#
#     「僵尸符号 → 已随函数删除」这一项的行为断言由既有
#     `native_ext/tests/lifecycle.rs` 承担，本任务只核对
#     文档结论与该守卫一致，不另写第二份源码扫描
#
# The guard read the citation as a write claim because it re-derived the
# claim set by regex-sweeping the *whole* description, while the planner
# (:func:`agent._build_micro_layers`) keyed on the structured field. The
# two now share :func:`agent._declared_modification_files`; these tests
# pin them together.


def test_declared_files_win_over_description_citation(tmp_path: Path) -> None:
    """A file the description merely cites must not be claimed."""
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _make_agent(project_dir)

    task = SubTask(
        id="1",
        title="audit closure guard",
        description=(
            "## 修改位置\n"
            "- `native_ext/docs/audit-0f0f0f0f.md`\n"
            "- `tests/regression/test_audit_0f0f0f0f_closure.py`\n"
            "\n"
            "## 边界条件\n"
            "「僵尸符号」这一项的行为断言由既有 "
            "`native_ext/tests/lifecycle.rs` 承担，"
            "本任务只核对文档结论与该守卫一致，不另写第二份源码扫描。\n"
        ),
        files_to_modify=[
            "native_ext/docs/audit-0f0f0f0f.md",
            "tests/regression/test_audit_0f0f0f0f_closure.py",
        ],
        test_command="echo ok",
    )

    files, manager = agent._pre_task_lock_hook(task)
    try:
        assert files == [
            "native_ext/docs/audit-0f0f0f0f.md",
            "tests/regression/test_audit_0f0f0f0f_closure.py",
        ], "declared files must be the claim set, verbatim and complete"
        assert "native_ext/tests/lifecycle.rs" not in files
    finally:
        agent._post_task_lock_hook(task.id, files, manager)


def test_parallel_tasks_with_disjoint_declarations_do_not_conflict(
    tmp_path: Path,
) -> None:
    """The regression: the planner's parallel pair must survive the guard.

    Reproduces that plan's L0 — task 1 (audit, cites task 2's file) and
    task 2 (owns that file) in one micro layer.
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _make_agent(project_dir)

    audit = SubTask(
        id="1",
        title="audit closure guard",
        description=(
            "## 修改位置\n- `native_ext/docs/audit-0f0f0f0f.md`\n"
            "\n## 边界条件\n"
            "行为断言由既有 `native_ext/tests/lifecycle.rs` "
            "承担，本任务不写它。\n"
        ),
        files_to_modify=["native_ext/docs/audit-0f0f0f0f.md"],
        test_command="echo ok",
    )
    zombie = SubTask(
        id="2",
        title="zombie symbol guard",
        description=(
            "## 修改位置\n"
            "- `native_ext/src/core.rs`\n"
            "- `native_ext/tests/lifecycle.rs`\n"
        ),
        files_to_modify=[
            "native_ext/src/core.rs",
            "native_ext/tests/lifecycle.rs",
        ],
        test_command="echo ok",
    )

    files_a, mgr_a = agent._pre_task_lock_hook(audit)
    try:
        # Before the fix this raised
        # RuntimeError("File conflict detected: ... by task 1").
        files_b, mgr_b = agent._pre_task_lock_hook(zombie)
        try:
            assert files_b == [
                "native_ext/src/core.rs",
                "native_ext/tests/lifecycle.rs",
            ]
        finally:
            agent._post_task_lock_hook(zombie.id, files_b, mgr_b)
    finally:
        agent._post_task_lock_hook(audit.id, files_a, mgr_a)


def test_shared_declared_file_still_conflicts(tmp_path: Path) -> None:
    """Defence in depth preserved: a real overlap still raises."""
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _make_agent(project_dir)

    first = SubTask(
        id="A",
        title="holder",
        description="FILE: backend/agent.py",
        files_to_modify=["backend/agent.py"],
        test_command="echo ok",
    )
    second = SubTask(
        id="B",
        title="conflicting task",
        description="FILE: backend/agent.py",
        files_to_modify=["backend/agent.py"],
        test_command="echo ok",
    )

    files, manager = agent._pre_task_lock_hook(first)
    try:
        with pytest.raises(RuntimeError, match="File conflict detected"):
            agent._pre_task_lock_hook(second)
    finally:
        agent._post_task_lock_hook(first.id, files, manager)


def test_sentinel_task_still_falls_back_to_the_modification_section(
    tmp_path: Path,
) -> None:
    """Unknown-file tasks keep the prose fallback — but scoped.

    A task with no usable declaration has only its prose, so the scan
    stays. It must read the ``## 修改位置`` section, not the whole
    description, or a citation in 边界条件 becomes a write claim again.
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _make_agent(project_dir)

    task = SubTask(
        id="3",
        title="unknown files",
        description=(
            "## 修改位置\n- `backend/file_lock_manager.py`\n"
            "\n## 边界条件\n"
            "参照既有 `backend/agent.py` 的实现，本任务不改它。\n"
        ),
        files_to_modify=["__UNKNOWN_MODIFICATIONS__"],
        test_command="echo ok",
    )

    files, manager = agent._pre_task_lock_hook(task)
    try:
        assert files == ["backend/file_lock_manager.py"]
        assert "backend/agent.py" not in files
    finally:
        agent._post_task_lock_hook(task.id, files, manager)


# ---------------------------------------------------------------------------
# broker ownership (2026-09-26)
#
# The broker is an acceptor *thread* holding OS locks. It is started by
# ``run`` and stopped by ``run``'s ``finally``, so its lifetime is exactly
# the run's. An earlier cut started it lazily from ``_pre_task_lock_hook``
# instead, which meant any caller using the hook without ``run`` — every
# test in this file, and any embedded use — spawned a thread nothing
# reclaimed. The conftest's "no worker outlives its test" guard is what
# surfaced it.
# ---------------------------------------------------------------------------
def test_task_with_no_declared_files_still_releases_hook_acquired_locks(tmp_path: Path) -> None:
    """An empty declaration must not strand the locks taken mid-task.

    ``files_to_modify: []`` is the normal shape for an audit-style task,
    and such a task still takes locks: the Edit/Write hook acquires every
    file the sub-agent decides to touch, keyed by task id. This used to
    return a bare ``FileLockManager`` — which holds nothing and has never
    heard of the broker — so the only release point never reached those
    locks and they outlived the task.

    A task declaring ``files_to_modify: []`` acquired locks through the
    broker and released none of them, and its breakdown child then sat in
    the broker's queue behind one of those locks for the rest of the run.
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _make_agent(project_dir)
    broker = agent._ensure_lock_broker()
    assert broker is not None, "the broker must start on a POSIX host"
    try:
        task = SubTask(
            id="audit-1",
            title="audit with no declared files",
            description="## 修改位置\n（本任务不新增文件）\n",
            files_to_modify=[],
            test_command="echo ok",
        )
        files, handle = agent._pre_task_lock_hook(task)
        assert files == [], "this task must declare nothing for the test to mean anything"

        # Exactly what the Edit/Write hook does mid-task.
        assert broker.acquire(task.id, "backend/whatever.py", 5) in (
            "acquired", "already",
        )
        assert broker.held(task.id) == ["backend/whatever.py"]

        agent._post_task_lock_hook(task.id, files, handle)

        assert broker.held(task.id) == [], (
            "a lock taken mid-task survived the task: the release point "
            "never reached the broker, so it stays held until the broker "
            "dies and every later acquirer of that file queues behind it"
        )
    finally:
        agent._stop_lock_broker()


def test_agent_broker_binds_a_socket_and_stops_on_demand(tmp_path: Path) -> None:
    """The two halves of the broker's lifetime, explicitly."""
    from file_lock_protocol import socket_path

    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _make_agent(project_dir)

    broker = agent._ensure_lock_broker()
    try:
        assert broker is not None, "the broker must start on a POSIX host"
        assert socket_path(project_dir).exists(), "no socket for the hook to reach"
        assert agent._active_lock_broker() is broker
    finally:
        agent._stop_lock_broker()

    assert not socket_path(project_dir).exists(), (
        "a finished run must not leave a socket for the next one to adopt"
    )


def test_active_broker_never_starts_one(tmp_path: Path) -> None:
    """Reading the active broker must not create it.

    This is the leak, pinned: the lock path asks whether a broker is
    running, and the answer for a hook called outside ``run`` is "no" —
    not "here is one I just started".
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _make_agent(project_dir)

    assert agent._active_lock_broker() is None
    assert agent._lock_broker is None, "asking started a broker"

    # ...and the hook still works, on the single-process fallback.
    task = SubTask(
        id="1",
        title="lock test",
        description="FILE: backend/agent.py",
        test_command="echo ok",
    )
    files, manager = agent._pre_task_lock_hook(task)
    try:
        assert files == ["backend/agent.py"]
        assert isinstance(manager, FileLockManager)
    finally:
        agent._post_task_lock_hook(task.id, files, manager)

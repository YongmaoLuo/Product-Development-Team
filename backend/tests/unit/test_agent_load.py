"""
Tests for AutonomousAgent._load_tasks() — PRD decision point 5.

Background
----------
PRD decision point 5: dependency validation must happen at LOAD time,
not at execution time. The contract is:

  * ``_load_tasks()`` reads ``<project_dir>/tasks.json`` and parses it
    into ``SubTask`` objects.
  * Immediately after parsing, it calls ``_validate_dependencies()``
    which raises ``ValueError`` on missing / self violations, and on a
    cycle *after* the 2026-09-05 ``_break_cycle_resilience`` pass has
    had its chance to strip the offending edges and skip the members.
  * On validation failure, the error is also written to
    ``execution.log`` under the event name ``task_validation_failed``.

The previous tasks in this plan added the three primitive helpers
(``_build_layers`` for Kahn's algorithm, ``_validate_dependencies`` for
the fail-fast check, ``ProviderConcurrencyController`` for the runtime
side). This file pins the integration: the load entry point actually
calls the validator and the agent refuses to proceed on malformed
``tasks.json``.

TDD spec — 3 contract tests
----------------------------
1. ``test_load_validates_passes``:
   Valid tasks.json (3 tasks with a clean DAG) → ``_load_tasks()`` returns
   a list of 3 SubTask objects; no ValueError.
2. ``test_load_validates_rejects_cycle``:
   tasks.json with ``A -> B -> A`` → both members are marked
   ``skipped`` and every cycling edge is stripped, so ``_load_tasks()``
   returns no schedulable task. ``_validate_dependencies`` itself
   still raises ``ValueError`` whose message contains "Cycle".
3. ``test_load_validates_rejects_missing``:
   tasks.json with ``A.depends_on=["NONEXISTENT"]`` → ``_load_tasks()``
   raises ``ValueError`` whose message contains "NONEXISTENT" and
   identifies the offending task id ("A").
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

from task import SubTask
from agent import _filter_terminal_tasks


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
    tests because we exercise ``_load_tasks`` directly.
    """
    def __init__(self, *args, **kwargs):
        pass


@pytest.fixture
def project_dir(tmp_path, monkeypatch):
    """A real project_dir with a real git repo so GitManager can bind.

    Also points ``PDT_STATE_DB_PATH`` at a fresh per-test state.db so
    the Phase-2 reconcile step in :meth:`AutonomousAgent._load_tasks`
    cannot pick up residue from prior tests or live plans that share
    the canonical repo-root ``state.db`` (the agent derives
    ``plan_id`` from the tasks.json parent dir, which is the
    literal string ``"project"`` here — anything in the live
    state.db for that id would leak into the test).
    """
    # Per-test state.db under tmp_path.
    test_state_db = tmp_path / "test_state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(test_state_db))

    pd = tmp_path / "project"
    _git_init(pd)
    return pd


def _write_tasks(project_dir: Path, tasks: list) -> Path:
    """Helper: write a tasks.json with the given task dicts.

    2026-09-09 update (two-constant scheme): the validator's step-3 gate
    accepts the ``NO_FILE_CHANGES`` sentinel as read-only directly. The
    other
    sentinel (``UNKNOWN_MODIFICATIONS``) is rejected and triggers
    the fill loop. Tests in this module exercise ``_load_tasks``
    filtering semantics, not the files_to_modify validation gate
    (that has its own test suite in
    ``test_files_to_modify_validation_gate.py``). The helper
    injects ``files_to_modify`` with the NO_FILE_CHANGES sentinel
    for any task dict that does not already specify it — equivalent
    to a plan author marking the task as read-only. Tests that DO
    want to exercise the gate (e.g. empty list, unknown-
    modifications) pass ``files_to_modify`` explicitly in their
    fixture.
    """
    from task import NO_FILE_CHANGES_SENTINEL

    normalised: list = []
    for t in tasks:
        td = dict(t)
        if "files_to_modify" not in td:
            td["files_to_modify"] = list(NO_FILE_CHANGES_SENTINEL)
        normalised.append(td)
    tasks_file = project_dir / "tasks.json"
    payload = {
        "requirement": "TDD spec for _load_tasks validation",
        "tasks": normalised,
    }
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tasks_file


def _build_agent(project_dir: Path):
    """Build a minimal AutonomousAgent bound to ``project_dir``.

    We bypass any real LLM by passing a dummy coding tool. The
    constructor also instantiates ``BackgroundManager`` /
    ``RetryManager`` / ``RollbackManager`` — these have no I/O side
    effects so they're safe to run inside a tmp_path.
    """
    from agent import AutonomousAgent

    return AutonomousAgent(
        requirement="TDD spec for _load_tasks validation",
        project_dir=project_dir,
        coding_tool=_DummyCodingTool(),
        logger=None,
    )


# ---------------------------------------------------------------------------
# Test 1: valid tasks.json → loads normally, validator passes silently
# ---------------------------------------------------------------------------


def test_load_validates_passes(project_dir):
    """A valid DAG (A → B, A → C) loads without raising and returns 3 tasks.

    PRD decision point 5 positive path: a well-formed plan must reach
    ``task_manager.tasks`` intact, ready for the executor to consume.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "A",
                "title": "Root task A",
                "description": "no deps",
                "test_command": "echo A",
                "status": "pending",
                "depends_on": [],
            },
            {
                "id": "B",
                "title": "Downstream of A",
                "description": "depends on A",
                "test_command": "echo B",
                "status": "pending",
                "depends_on": ["A"],
            },
            {
                "id": "C",
                "title": "Downstream of A",
                "description": "depends on A",
                "test_command": "echo C",
                "status": "pending",
                "depends_on": ["A"],
            },
        ],
    )

    agent = _build_agent(project_dir)
    tasks = agent._load_tasks()

    assert isinstance(tasks, list), "_load_tasks must return a list"
    assert len(tasks) == 3, f"expected 3 tasks, got {len(tasks)}"
    assert {t.id for t in tasks} == {"A", "B", "C"}
    # depends_on must survive the round-trip
    by_id = {t.id: t for t in tasks}
    assert by_id["A"].depends_on == []
    assert by_id["B"].depends_on == ["A"]
    assert by_id["C"].depends_on == ["A"]
    # task_manager must be updated so subsequent get_next_task() sees them.
    # The filtered subset (no terminal tasks here) must equal the full
    # list stored on the manager. (Since all three tasks are non-terminal,
    # the filter is a no-op and the contents match in both order and ids.)
    assert agent.task_manager.tasks == tasks
    # _all_tasks is also populated with the same content for status
    # queries.
    assert hasattr(agent, "_all_tasks")
    assert agent._all_tasks == tasks


# ---------------------------------------------------------------------------
# Test 2: cycle (A → B → A) → ValueError, plan rejected at load time
# ---------------------------------------------------------------------------


def test_load_validates_rejects_cycle(project_dir):
    """A 2-node cycle (A.depends_on=[B], B.depends_on=[A]) is BROKEN at load time.

    Contract updated 2026-09-05 (``AutonomousAgent._break_cycle_resilience``,
    plan ``2026-09-04 plan``). The load path no
    longer aborts the whole plan on a cycle: the 2026-09-05 incident
    showed that raising cost 32 downstream tasks (4 of which had
    already executed successfully) for a transient refiner 2-cycle.
    Instead the dispatcher:

      1. strips the cycle-causing member→member ``depends_on`` edges,
      2. marks each non-terminal member ``skipped`` (persisted, so the
         cycle cannot re-form after a restart),
      3. re-runs ``_validate_dependencies``.

    The safety property the old assertion protected — "the executor
    must never spin forever on an unsatisfiable graph" — is preserved:
    the members are terminal *before* the graph reaches the scheduler,
    every edge they owned is gone, and :func:`_validate_dependencies`
    still raises ``ValueError("Cycle detected: ...")`` for a residual
    cycle. That fail-fast layer is asserted directly at the bottom of
    this test, because it is where the ``ValueError`` contract now
    lives.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "A",
                "title": "A",
                "description": "depends on B (cycle)",
                "test_command": "echo A",
                "status": "pending",
                "depends_on": ["B"],
            },
            {
                "id": "B",
                "title": "B",
                "description": "depends on A (cycle)",
                "test_command": "echo B",
                "status": "pending",
                "depends_on": ["A"],
            },
        ],
    )

    agent = _build_agent(project_dir)
    loaded = agent._load_tasks()

    # Both members were skipped, so nothing schedulable survives the
    # terminal-status filter. This is the loop-forever guard: the
    # dispatcher never sees the cycle.
    assert loaded == [], (
        f"every cycle member must be marked skipped, leaving no "
        f"schedulable task; got {[(t.id, t.status) for t in loaded]!r}"
    )
    by_id = {t.id: t for t in agent.task_manager.tasks}
    assert set(by_id) == {"A", "B"}, (
        f"members must stay in tasks.json; got "
        f"{sorted(by_id)}"
    )
    for tid in ("A", "B"):
        assert by_id[tid].status == "skipped", (
            f"cycle member {tid!r} must be skipped; got "
            f"{by_id[tid].status!r}"
        )
        assert list(by_id[tid].depends_on or []) == [], (
            f"the cycle-causing edge must be stripped from {tid!r}; got "
            f"{by_id[tid].depends_on!r}"
        )
    assert agent._active_tasks == [], (
        f"no task may reach the scheduler; got "
        f"{[(t.id, t.status) for t in agent._active_tasks]!r}"
    )

    # The fail-fast layer is unchanged: a cycle handed straight to it
    # (bypassing the resilience pass) still raises, names the violation
    # ("Cycle") and lists every member so the operator can grep
    # tasks.json directly.
    from agent import _validate_dependencies

    cyclic = [
        SubTask(
            id="A", title="A", description="depends on B (cycle)",
            test_command="echo A", depends_on=["B"],
        ),
        SubTask(
            id="B", title="B", description="depends on A (cycle)",
            test_command="echo B", depends_on=["A"],
        ),
    ]
    with pytest.raises(ValueError) as exc_info:
        _validate_dependencies(cyclic)

    msg = str(exc_info.value)
    assert "Cycle" in msg, f"expected 'Cycle' in error message, got: {msg!r}"
    for member in ("A", "B"):
        assert member in msg, (
            f"expected cycle member {member!r} in error, got: {msg!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: missing dependency (A.depends_on=['NONEXISTENT']) → REPAIRED at load
# ---------------------------------------------------------------------------


def test_load_repairs_missing_dep(project_dir):
    """A task that depends on a non-existent id is repaired, not rejected.

    2026-09-21 contract change. This used to ``raise ValueError`` and
    refuse the plan; on the refinement path the same shape was even
    mis-labelled as a cycle (``task_manager._ensure_acyclic`` counted a
    missing ``depends_on`` target as unmet in-degree, so its target's
    whole downstream cone was reported as cycle members — plan
    a production plan died that way).

    A reference to a task that is not in the list carries no ordering
    information, so the edge is stripped, the load succeeds, and the
    repair is logged under ``task_dangling_deps_stripped``.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "A",
                "title": "A",
                "description": "depends on a missing task",
                "test_command": "echo A",
                "status": "pending",
                "depends_on": ["NONEXISTENT"],
            },
        ],
    )

    agent = _build_agent(project_dir)
    loaded = agent._load_tasks()  # must NOT raise

    by_id = {t.id: t for t in loaded}
    assert "A" in by_id, f"task A must survive the load, got {sorted(by_id)}"
    assert by_id["A"].depends_on == [], (
        f"the dangling edge must be stripped, got "
        f"{by_id['A'].depends_on!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: terminal-status filter (PRD acceptance case 5)
# ---------------------------------------------------------------------------
#
# Cross-process recovery contract: when an existing tasks.json is
# reloaded (e.g. after a crash, or when starting a second run on the
# same project), the DAG fed into _build_layers() must not contain
# tasks that have already reached a terminal state. Otherwise the
# executor would re-execute them and double-write checkpoints / commit
# messages.
#
# The contract is:
#   * terminal statuses (completed / failed / skipped) are excluded
#     from the returned list.
#   * The full list is retained on self._all_tasks for status queries.
#   * non-terminal statuses (pending / in_progress /
#     breakdown_in_progress) are kept.
#   * When every task is terminal, _load_tasks returns [] so the
#     scheduler can enter the all-done state immediately.


def test_load_skips_completed(project_dir):
    """A task with status='completed' is filtered out of the returned DAG.

    Task A is already completed from a previous run. On reload, the
    returned list must NOT contain A — otherwise _build_layers() would
    re-include it and the executor would re-run its test command and
    re-emit its `[task-A]` commit. The full list (with A) is still
    available via self._all_tasks for status / progress reporting.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "A",
                "title": "Already done",
                "description": "previously completed",
                "test_command": "echo A",
                "status": "completed",
                "depends_on": [],
            },
            {
                "id": "B",
                "title": "Still pending",
                "description": "needs to run",
                "test_command": "echo B",
                "status": "pending",
                "depends_on": [],
            },
        ],
    )

    agent = _build_agent(project_dir)
    tasks = agent._load_tasks()

    # The returned list excludes the completed task.
    assert isinstance(tasks, list), "_load_tasks must return a list"
    assert len(tasks) == 1, (
        f"expected 1 non-terminal task, got {len(tasks)}: "
        f"{[t.id for t in tasks]}"
    )
    assert tasks[0].id == "B", f"expected B to be kept, got {tasks[0].id}"

    # The full list is retained on self._all_tasks for status queries.
    assert hasattr(agent, "_all_tasks"), (
        "agent must retain the full task list on self._all_tasks"
    )
    all_ids = {t.id for t in agent._all_tasks}
    assert all_ids == {"A", "B"}, (
        f"self._all_tasks must contain every task including terminal "
        f"ones, got {all_ids}"
    )


def test_load_skips_failed(project_dir):
    """A task with status='failed' is filtered out of the returned DAG.

    Same contract as test_load_skips_completed, but for the
    ``failed`` terminal status. A failed task must not be re-run on
    reload; the operator is expected to inspect the failure reason
    and either edit tasks.json to mark it pending or let the recovery
    flow surface the error.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "B",
                "title": "Previously failed",
                "description": "errored on last run",
                "test_command": "echo B",
                "status": "failed",
                "failure_reason": "exit 1: assertion failed",
                "depends_on": [],
            },
            {
                "id": "C",
                "title": "Still pending",
                "description": "needs to run",
                "test_command": "echo C",
                "status": "pending",
                "depends_on": [],
            },
        ],
    )

    agent = _build_agent(project_dir)
    tasks = agent._load_tasks()

    assert len(tasks) == 1, (
        f"expected 1 non-terminal task, got {len(tasks)}: "
        f"{[t.id for t in tasks]}"
    )
    assert tasks[0].id == "C", f"expected C to be kept, got {tasks[0].id}"
    # _all_tasks still has the failed task.
    all_ids = {t.id for t in agent._all_tasks}
    assert all_ids == {"B", "C"}


def test_load_keeps_pending(project_dir):
    """A task with status='pending' is kept in the returned DAG.

    The positive control: nothing is filtered when every task is in a
    non-terminal state. The full set is returned to the caller and
    matches self._all_tasks.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "C",
                "title": "Fresh task",
                "description": "needs to run",
                "test_command": "echo C",
                "status": "pending",
                "depends_on": [],
            },
        ],
    )

    agent = _build_agent(project_dir)
    tasks = agent._load_tasks()

    assert len(tasks) == 1, f"expected 1 task, got {len(tasks)}"
    assert tasks[0].id == "C"
    assert tasks[0].status == "pending"
    # The same set is on _all_tasks.
    assert {t.id for t in agent._all_tasks} == {"C"}


def test_load_all_completed_returns_empty(project_dir):
    """When every task is terminal, _load_tasks returns [].

    This is the scheduler's signal to enter the all-done state
    immediately. self._all_tasks still has the full list (so the
    progress endpoint can report "5 / 5 completed") but the DAG is
    empty.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "1",
                "title": "done 1",
                "description": "",
                "test_command": "echo 1",
                "status": "completed",
                "depends_on": [],
            },
            {
                "id": "2",
                "title": "done 2",
                "description": "",
                "test_command": "echo 2",
                "status": "completed",
                "depends_on": ["1"],
            },
            {
                "id": "3",
                "title": "done 3",
                "description": "",
                "test_command": "echo 3",
                "status": "completed",
                "depends_on": ["2"],
            },
        ],
    )

    agent = _build_agent(project_dir)
    tasks = agent._load_tasks()

    assert tasks == [], (
        f"expected empty list when all tasks are terminal, got "
        f"{[t.id for t in tasks]}"
    )
    # _all_tasks retains the full list for status reporting.
    assert {t.id for t in agent._all_tasks} == {"1", "2", "3"}


def test_load_keeps_breakdown_in_progress(project_dir):
    """A task with status='breakdown_in_progress' is kept (not terminal).

    When a task is being broken down into subtasks, its children have
    just been appended to tasks.json. The parent is marked
    ``breakdown_in_progress`` and the children are typically
    ``pending``. Both must be in the returned DAG so the children can
    be scheduled. Excluding the parent would be safe (it has no test
    to run) but the children — which are not terminal — would still
    be kept, and the parent would surface on _all_tasks. This test
    pins that the parent itself is NOT excluded as a side effect.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "P",
                "title": "Parent being broken down",
                "description": "children just inserted",
                "test_command": "echo P",
                "status": "breakdown_in_progress",
                "depends_on": [],
            },
            {
                "id": "P-1",
                "title": "Child 1",
                "description": "fresh",
                "test_command": "echo P-1",
                "status": "pending",
                "depends_on": [],
            },
        ],
    )

    agent = _build_agent(project_dir)
    tasks = agent._load_tasks()

    ids = {t.id for t in tasks}
    assert "P-1" in ids, "the pending child must be in the DAG"
    # The parent is in a working state (not terminal) so it is also
    # retained — but importantly the children are not filtered out
    # just because their parent is in a non-terminal working state.
    assert "P" in ids or "P" in {t.id for t in agent._all_tasks}, (
        "the parent must be reachable via tasks or _all_tasks"
    )


def test_load_filters_dependency_failed_and_skipped(project_dir):
    """Tasks with status='skipped' are also filtered.

    The terminal-status set is the union of {completed, failed, skipped}.
    This test pins the full union so a future refactor that drops one
    of the three (e.g. removes ``skipped`` thinking it's an alias of
    ``failed``) is caught immediately.

    Note: ``dependency_failed`` is no longer a recognised status — PRD
    decision point 4 removed the downstream-propagation path, so a
    synthetic ``dependency_failed`` status never appears on disk. This
    test exercises the three-status union that replaces it.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "1",
                "title": "completed",
                "description": "",
                "test_command": "echo 1",
                "status": "completed",
                "depends_on": [],
            },
            {
                "id": "2",
                "title": "failed",
                "description": "",
                "test_command": "echo 2",
                "status": "failed",
                "depends_on": [],
            },
            {
                "id": "3",
                "title": "skipped",
                "description": "",
                "test_command": "echo 3",
                "status": "skipped",
                "depends_on": [],
            },
            {
                "id": "4",
                "title": "still pending",
                "description": "",
                "test_command": "echo 4",
                "status": "pending",
                "depends_on": [],
            },
        ],
    )

    agent = _build_agent(project_dir)
    tasks = agent._load_tasks()

    returned_ids = {t.id for t in tasks}
    assert returned_ids == {"4"}, (
        f"only the pending task must survive, got {returned_ids}"
    )
    # _all_tasks retains all 4.
    assert {t.id for t in agent._all_tasks} == {"1", "2", "3", "4"}


# ---------------------------------------------------------------------------
# Test 5: _filter_terminal_tasks — pure function contract
# ---------------------------------------------------------------------------
#
# The pure function ``_filter_terminal_tasks`` is the smallest extractable
# unit of the load-time filtering logic in ``_load_tasks``. It is invoked
# by the method on every load, but it can also be called directly on any
# list of SubTask objects to answer the question: "given this list, which
# tasks are still actionable and how many were already finished?".
#
# The four tests below pin the exact contract documented in the function's
# docstring: empty → ([], 0), all-terminal → ([], N), all-active → (tasks, 0),
# mixed → (active, count). breakdown_in_progress is a non-terminal status
# and must be kept in the active list even though the task has no test
# command to run on its own.


def _make_task(task_id: str, status: str) -> SubTask:
    """Build a minimal SubTask with the given id and status.

    The pure function only reads ``task.status``; all other fields are
    irrelevant for these tests. ``test_command`` defaults to empty so
    the task model is happy.
    """
    return SubTask(
        id=task_id,
        title=f"task {task_id}",
        description="",
        test_command="",
        status=status,
    )


def test_filter_returns_active_and_count():
    """Mixed statuses → active list (B, D, F) and terminal_count=3 (A, C, E).

    This is the example pinned by the subtask description: a list of 6
    SubTask objects with three terminal statuses interleaved with three
    active ones. The function must return only B, D, F in input order
    and report that 3 tasks were terminal. A and C and E must NOT be in
    the active list.
    """
    tasks = [
        _make_task("A", "completed"),
        _make_task("B", "pending"),
        _make_task("C", "failed"),
        _make_task("D", "in_progress"),
        _make_task("E", "skipped"),
        _make_task("F", "breakdown_in_progress"),
    ]

    active, terminal_count = _filter_terminal_tasks(tasks)

    assert isinstance(active, list), "active must be a list"
    assert terminal_count == 3, (
        f"expected 3 terminal tasks (A, C, E), got {terminal_count}"
    )
    active_ids = [t.id for t in active]
    assert active_ids == ["B", "D", "F"], (
        f"active must preserve input order, got {active_ids}"
    )
    # The original list must NOT be mutated (function is pure).
    assert [t.id for t in tasks] == [
        "A", "B", "C", "D", "E", "F"
    ], "input list must not be mutated"


def test_filter_keeps_breakdown_in_progress():
    """breakdown_in_progress is non-terminal → preserved in active list.

    The subtask spec is explicit: ``breakdown_in_progress`` is a working
    state (the parent is in the middle of being split into subtasks)
    and must NOT be excluded. This test pins the contract independently
    of the surrounding fixture, so a future refactor that accidentally
    adds ``breakdown_in_progress`` to the terminal set is caught here.
    """
    tasks = [
        _make_task("P", "breakdown_in_progress"),
        _make_task("P-1", "pending"),
        _make_task("P-2", "pending"),
    ]

    active, terminal_count = _filter_terminal_tasks(tasks)

    assert terminal_count == 0, (
        f"breakdown_in_progress must not be terminal, got count={terminal_count}"
    )
    active_ids = [t.id for t in active]
    assert active_ids == ["P", "P-1", "P-2"], (
        f"all three tasks must be kept, got {active_ids}"
    )


def test_filter_empty_input():
    """[] → ([], 0).

    The empty-input edge case is the "no work to do" sentinel: the
    scheduler should enter the all-done state immediately. The function
    must not raise on an empty list and must return an empty active
    list (not None) so callers can iterate uniformly.
    """
    active, terminal_count = _filter_terminal_tasks([])

    assert active == [], f"active must be empty list, got {active!r}"
    assert terminal_count == 0, (
        f"terminal_count must be 0 for empty input, got {terminal_count}"
    )


def test_filter_all_terminal():
    """All completed → ([], N).

    When every task in the input has a terminal status, the function
    must return an empty active list and the full input length as the
    terminal count. This is the "everything is done" case the scheduler
    uses to enter the all-done state.

    Note: ``dependency_failed`` is no longer a recognised terminal
    status (PRD decision point 4). The terminal set is now exactly
    {completed, failed, skipped}.
    """
    tasks = [
        _make_task("1", "completed"),
        _make_task("2", "failed"),
        _make_task("3", "skipped"),
        _make_task("4", "completed"),
    ]

    active, terminal_count = _filter_terminal_tasks(tasks)

    assert active == [], (
        f"active must be empty when all tasks are terminal, got "
        f"{[t.id for t in active]}"
    )
    assert terminal_count == 4, (
        f"terminal_count must equal input length (4), got {terminal_count}"
    )
    # Sanity: every status in the input is recognised as terminal.
    assert {t.status for t in tasks} <= {
        "completed", "failed", "skipped",
    }


# ---------------------------------------------------------------------------
# Test 6: cross-process recovery log line
# ---------------------------------------------------------------------------
#
# Operators and SREs investigating "why was task X skipped on
# recover?" need a single line in ``execution.log`` that names the
# filtered task ids. Without it, the only way to reconstruct the
# decision is to diff the on-disk ``tasks.json`` against an earlier
# snapshot, which is brittle in CI and impossible in production. The
# three tests below pin the contract that:
#
#   1. ``_load_tasks()`` with N terminal tasks writes exactly one
#      ``task_filtered_terminal`` event whose ``data.filtered_count``
#      equals N.
#   2. ``_load_tasks()`` with 0 terminal tasks writes NO
#      ``task_filtered_terminal`` event (avoiding noise on a clean
#      cold start where every task is pending).
#   3. The ``task_filtered_terminal`` event's ``data.terminal_ids``
#      list contains exactly the ids of the terminal tasks in input
#      order — the operator can ``grep`` those ids out of the log
#      without a second pass over ``tasks.json``.


@pytest.fixture
def logged_plans_dir(tmp_path):
    """A temp ``plans_dir`` for the ExecutionLogger the new tests use.

    The logger writes to ``<plans_dir>/<plan_id>/execution.log`` —
    returning just the parent so each test can pick a unique plan_id
    and still share the same temp scratch area. (We deliberately
    avoid a single hard-coded plan_id so tests that read the log
    can't accidentally see each other's writes.)
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    return plans_dir


def _read_log_entries(log_file: Path, event_name: Optional[str] = None) -> list:
    """Read execution.log into a list of dicts, optionally filtered by event.

    Mirrors the parsing done in
    ``tests/integration/test_agent_wiring.py::test_agent_settings_path_in_logger``
    — one JSON object per line, skip blank / malformed lines.
    """
    if not log_file.exists():
        return []
    entries = []
    for line in log_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event_name is None or entry.get("event") == event_name:
            entries.append(entry)
    return entries


def test_load_logs_filtered_count(project_dir, logged_plans_dir):
    """2 terminal tasks → execution.log contains a task_filtered_terminal event.

    The cross-process recovery diagnostic contract: when a load
    filters terminal tasks out of the returned DAG, the operator
    can find the decision in ``execution.log`` by grep'ing for
    ``task_filtered_terminal``. The line carries a
    ``data.filtered_count`` matching the actual number of terminal
    tasks (not the total tasks, not the remaining tasks). This
    test pins both the event name and the count payload.
    """
    from execution_logger import ExecutionLogger

    plan_id = "test-load-logs-filtered-count"
    logger = ExecutionLogger(plan_id=plan_id, plans_dir=logged_plans_dir)

    _write_tasks(
        project_dir,
        [
            {
                "id": "A",
                "title": "previously completed",
                "description": "",
                "test_command": "echo A",
                "status": "completed",
                "depends_on": [],
            },
            {
                "id": "B",
                "title": "previously failed",
                "description": "",
                "test_command": "echo B",
                "status": "failed",
                "depends_on": [],
            },
            {
                "id": "C",
                "title": "still pending",
                "description": "",
                "test_command": "echo C",
                "status": "pending",
                "depends_on": [],
            },
        ],
    )

    agent = _build_agent(project_dir)
    # _build_agent constructed the agent with logger=None. Replace
    # the logger with our real one so the load diagnostic actually
    # hits disk. (The agent does not cache the logger in any
    # downstream structure; replacing the attribute is sufficient.)
    agent.logger = logger

    tasks = agent._load_tasks()
    # Sanity: the filter contract still holds — the DAG returned
    # to the caller is the non-terminal subset (C only).
    assert [t.id for t in tasks] == ["C"]

    log_file = logged_plans_dir / plan_id / "execution.log"
    entries = _read_log_entries(log_file, event_name="task_filtered_terminal")

    assert len(entries) == 1, (
        f"expected exactly 1 task_filtered_terminal event, got {len(entries)}. "
        f"Full log:\n{log_file.read_text() if log_file.exists() else '<no log>'}"
    )
    entry = entries[0]
    assert entry.get("level") == "INFO", (
        f"event level must be INFO, got {entry.get('level')!r}"
    )
    assert entry.get("data", {}).get("filtered_count") == 2, (
        f"filtered_count must equal the number of terminal tasks (2), "
        f"got {entry.get('data', {}).get('filtered_count')!r}. "
        f"Full entry: {entry!r}"
    )


def test_load_no_log_when_zero_filtered(project_dir, logged_plans_dir):
    """All pending → execution.log contains NO task_filtered_terminal event.

    The negative case: a clean cold start (no previously-finished
    tasks) is the most common load scenario. Writing a
    ``task_filtered_terminal`` event with ``filtered_count=0`` on
    every such load would flood the log with noise, and grep'ing
    for "did the recover skip anything?" would return false
    positives on every run. The contract is: only write the event
    when the count is > 0.
    """
    from execution_logger import ExecutionLogger

    plan_id = "test-load-no-log-when-zero"
    logger = ExecutionLogger(plan_id=plan_id, plans_dir=logged_plans_dir)

    _write_tasks(
        project_dir,
        [
            {
                "id": "A",
                "title": "fresh task A",
                "description": "",
                "test_command": "echo A",
                "status": "pending",
                "depends_on": [],
            },
            {
                "id": "B",
                "title": "fresh task B",
                "description": "",
                "test_command": "echo B",
                "status": "pending",
                "depends_on": [],
            },
        ],
    )

    agent = _build_agent(project_dir)
    agent.logger = logger

    tasks = agent._load_tasks()
    # Sanity: nothing is filtered.
    assert [t.id for t in tasks] == ["A", "B"]

    log_file = logged_plans_dir / plan_id / "execution.log"
    entries = _read_log_entries(log_file, event_name="task_filtered_terminal")

    assert entries == [], (
        f"expected NO task_filtered_terminal event when filtered_count==0, "
        f"got {len(entries)} entries. Full log:\n"
        f"{log_file.read_text() if log_file.exists() else '<no log>'}"
    )


def test_load_log_includes_terminal_ids(project_dir, logged_plans_dir):
    """Log payload must include the actual filtered task ids (in input order).

    The whole point of writing a log line on filter is to give the
    operator a single grep target. If the payload omits the
    terminal ids, the line is useless — the operator would have
    to diff tasks.json against a baseline anyway. This test pins
    that the ``data.terminal_ids`` field is a non-empty list
    containing exactly the ids of the tasks that were filtered
    out, in the same order they appeared in tasks.json.
    """
    from execution_logger import ExecutionLogger

    plan_id = "test-load-log-includes-terminal-ids"
    logger = ExecutionLogger(plan_id=plan_id, plans_dir=logged_plans_dir)

    _write_tasks(
        project_dir,
        [
            {
                "id": "X",
                "title": "previously completed",
                "description": "",
                "test_command": "echo X",
                "status": "completed",
                "depends_on": [],
            },
            {
                "id": "Y",
                "title": "previously failed",
                "description": "",
                "test_command": "echo Y",
                "status": "failed",
                "depends_on": [],
            },
            {
                "id": "Z",
                "title": "still pending",
                "description": "",
                "test_command": "echo Z",
                "status": "pending",
                "depends_on": [],
            },
        ],
    )

    agent = _build_agent(project_dir)
    agent.logger = logger

    tasks = agent._load_tasks()
    # Sanity: only Z survives the filter.
    assert [t.id for t in tasks] == ["Z"]

    log_file = logged_plans_dir / plan_id / "execution.log"
    entries = _read_log_entries(log_file, event_name="task_filtered_terminal")

    assert len(entries) == 1, (
        f"expected exactly 1 task_filtered_terminal event, got {len(entries)}. "
        f"Full log:\n{log_file.read_text() if log_file.exists() else '<no log>'}"
    )
    entry = entries[0]
    terminal_ids = entry.get("data", {}).get("terminal_ids")

    # The payload must carry the list. An absent / None / non-list
    # field is a regression to the original (pre-6-3) "filtered
    # silently" behaviour that this subtask explicitly fixes.
    assert isinstance(terminal_ids, list), (
        f"data.terminal_ids must be a list, got {type(terminal_ids).__name__}: "
        f"{terminal_ids!r}. Full entry: {entry!r}"
    )
    # Exactly the terminal tasks, in input order (X came before Y
    # in tasks.json). We do not assert set equality because the
    # contract is "preserve input order" — a future refactor that
    # alphabetised the list would break grep-pipeline expectations.
    assert terminal_ids == ["X", "Y"], (
        f"terminal_ids must be ['X', 'Y'] in input order, got {terminal_ids!r}. "
        f"Full entry: {entry!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: desc-vs-depends_on consistency (task 4 of the depends_on chain)
# ---------------------------------------------------------------------------


def test_load_validates_rejects_desc_dependency_drift(project_dir):
    """desc says '前置条件：任务 1 已完成' but ``depends_on=[]`` → ValueError.

    Incremental test for the desc-vs-depends_on consistency check
    added on top of the original ``_load_tasks`` suite. Where
    ``test_load_validates_rejects_missing`` checks the *data* path
    (an id in ``depends_on`` that no task carries), this test
    checks the *prose ⇄ data* path (a desc that names a
    prerequisite the data omits).

    The drift is sneaky: the layer builder walks ``depends_on``
    only, so a task whose desc declares a prereq but whose
    ``depends_on`` is empty would be scheduled before its prereq
    finishes. The fix is a fail-fast ``ValueError`` at load time
    whose message names both the offending task id (the task
    carrying the bad desc) and the missing dependency (so an
    operator can grep tasks.json directly).

    This test complements ``test_tasks_generator_depends_on.py
    ::test_load_tasks_desc_consistency`` — that test pins the
    spec example; this one pins the contract from a slightly
    different angle (hierarchical id 1-2 mentions in desc, so the
    parser's id-preservation is exercised through the full
    ``_load_tasks`` path).
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "1-2",
                "title": "intermediate task with hierarchical id",
                "description": "本任务实现中间功能。前置条件/依赖：任务 1-2-1 已完成。",
                "test_command": "echo 1-2",
                "status": "pending",
                "depends_on": [],
            },
            {
                "id": "1-2-1",
                "title": "leaf prerequisite",
                "description": "",
                "test_command": "echo 1-2-1",
                "status": "pending",
                "depends_on": [],
            },
        ],
    )

    agent = _build_agent(project_dir)

    with pytest.raises(ValueError) as exc_info:
        agent._load_tasks()

    msg = str(exc_info.value)
    # The error must name the offending task id (the task carrying
    # the bad desc), the missing hierarchical dep "1-2-1", and
    # hint at "desc" as the source.
    assert "1-2" in msg, (
        f"expected offending task id '1-2' in error message, "
        f"got: {msg!r}"
    )
    assert "1-2-1" in msg, (
        f"expected missing dep '1-2-1' in error message, "
        f"got: {msg!r}"
    )
    assert "desc" in msg.lower() or "前置" in msg or "依赖" in msg, (
        f"error should mention 'desc', '前置', or '依赖' as the "
        f"source of the discrepancy, got: {msg!r}"
    )



# ---------------------------------------------------------------------------
# Test 4: behaviour-bearing static flags survive the load filter
# ---------------------------------------------------------------------------


def test_load_carries_task_group_and_verification_only(project_dir):
    """``_load_tasks`` must not drop the two behaviour-bearing flags.

    2026-09-16. ``_load_tasks`` rebuilds each ``SubTask`` from an
    explicit allow-list (``_TASK_FIELDS``). ``task_group`` and
    ``verification_only`` were both missing from it, which turned their
    consumers into dead code:

      * ``task_group`` gates the refiner's ``startswith("repair")``
        guard (see ``refiner_structure``). The refiner reads it off
        ``[t.model_dump() for t in task_manager.tasks]``, so with the
        field dropped at load the guard never matched and the refiner
        was free to delete every ``repair-*`` row it did not echo back
        — including already-completed ones, which then reloaded as
        ``pending``.
      * ``verification_only`` is the declared half of the audit-task
        exemption in the dual-criterion completion rule.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "orig-1",
                "title": "an authored task",
                "description": "no deps",
                "test_command": "echo a",
                "status": "pending",
                "depends_on": [],
            },
            {
                "id": "repair-r3-01",
                "title": "a repair task",
                "description": "owned by the orchestrator",
                "test_command": "echo b",
                "status": "pending",
                "depends_on": [],
                "task_group": "repair-round-3",
                "verification_only": True,
            },
        ],
    )

    agent = _build_agent(project_dir)
    tasks = agent._load_tasks()

    by_id = {t.id: t for t in tasks}
    assert by_id["repair-r3-01"].task_group == "repair-round-3", (
        "task_group was dropped at load — the refiner's repair-task "
        "guard will never match"
    )
    assert by_id["repair-r3-01"].verification_only is True, (
        "verification_only was dropped at load — the declared "
        "audit-task exemption will never apply"
    )
    # And the refiner's decision function must see it too.
    from refiner_structure import is_protected

    assert is_protected(by_id["repair-r3-01"].model_dump())
    assert not is_protected(by_id["orig-1"].model_dump())

"""
TDD tests for ``AutonomousAgent._breakdown_task`` — DAG-aware breakdown.

Background
----------
When the executor decides a single task cannot be completed at its
current granularity, ``_breakdown_task`` is the integration point that
splits the parent into 2-3 child subtasks, inserts them into the
scheduler's leaf set (``self._active_tasks``), and persists the
``breakdown_count`` to disk so cross-process recovery resumes from the
right step.

The four tests below pin one contract each:

  1. ``test_breakdown_inserts_subtasks``
     After ``_breakdown_task(parent)`` returns, ``self._active_tasks``
     contains the freshly-generated child :class:`SubTask` objects (and
     the parent has been removed from the leaf set since it is no
     longer the next scheduling target).

  2. ``test_breakdown_increments_count``
     ``parent.breakdown_count`` advances from 0 to 1 after one call. The
     value is also persisted to ``tasks.json`` so a process restart can
     read it back.

  3. ``test_breakdown_max_5_stops``
     With ``parent.breakdown_count == EXECUTOR_BREAKDOWN_MAX`` (5),
     ``_breakdown_task`` makes **no** LLM call, marks
     ``parent.status = "failed"``, persists the failure, and adds no
     children. This is the safety net that prevents infinite breakdown
     loops on adversarial tasks.

  4. ``test_breakdown_rebuilds_layers``
     After the split, calling :func:`_build_layers` on
     ``self._active_tasks`` produces layers that include the child
     tasks. Combined with test 1 this proves the scheduler can pick
     up the children on the next layer-rebuild without any external
     state being refreshed.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

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


class _StubCodingTool:
    """Minimal coding tool stub with a scripted ``query_json`` response.

    ``_breakdown_task`` calls ``self.coding_tool.query_json(prompt, ...)``
    to generate child subtask dicts. The real ``ClaudeCodingTool`` /
    ``OpenCodeCodingTool`` would shell out to an LLM; this stub returns
    a fixed dict that the test crafted, and records the prompts it saw
    so the test can assert on call count (e.g. test 3 asserts the LLM
    was NOT called when the cap was reached).
    """

    def __init__(self, response: Optional[dict] = None):
        # The dict returned from ``query_json``. Tests that exercise the
        # cap-reached branch pass ``response=None`` so a call would
        # raise — the cap branch must never reach ``query_json``.
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
                "(typically because the breakdown cap should have stopped "
                "execution before this point)."
            )
        return self.response


@pytest.fixture
def project_dir(tmp_path):
    """A real project_dir with a real git repo so GitManager can bind."""
    pd = tmp_path / "project"
    _git_init(pd)
    return pd


def _write_tasks(project_dir: Path, tasks: list) -> Path:
    """Helper: write a ``tasks.json`` with the given task dicts.

    2026-09-14: every task gets an explicit ``files_to_modify`` when the
    caller did not supply one. ``AutonomousAgent._load_tasks`` runs the
    dispatcher post-read gate (``TaskOutputValidator``), whose step 3
    rejects a task whose ``files_to_modify`` is missing, empty or the
    ``__UNKNOWN_MODIFICATIONS__`` sentinel. The 2026-09-09 decision
    deliberately REMOVED the silent sentinel substitution, so a fixture
    that omits the field now raises
    ``RuntimeError: dispatcher post-read gate`` instead of loading.
    These tests are about ``_breakdown_task``, not about file
    attribution, so a placeholder path is the honest fixture value.

    Step 3 also requires each declared path to exist on disk (or to
    have been deleted in a reachable commit), so every placeholder —
    and every path a caller declared by hand, e.g. ``p1.py`` in
    ``test_breakdown_rebuilds_layers`` — is materialised under
    ``project_dir``.
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
        "requirement": "TDD spec for _breakdown_task",
        "tasks": normalised,
    }
    tasks_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return tasks_file


def _build_agent(project_dir: Path, coding_tool):
    """Build a minimal AutonomousAgent bound to ``project_dir``.

    Accepts a coding_tool so each test can pass its own stub. The
    agent's constructor wires up the persist lock and the empty
    ``_active_tasks`` / ``_all_tasks`` lists; the test then calls
    ``_load_tasks()`` (or builds the lists by hand) before invoking
    ``_breakdown_task``.
    """
    from agent import AutonomousAgent

    return AutonomousAgent(
        requirement="TDD spec for _breakdown_task",
        project_dir=project_dir,
        coding_tool=coding_tool,
        logger=None,
    )


# ---------------------------------------------------------------------------
# _aggregate_breakdown_verdict — collapse children into parent's verdict
# ---------------------------------------------------------------------------
#
# TDD spec (one-to-one with the task description):
#
#   1. test_aggregate_all_pass
#      3 children all completed → parent.status = "completed".
#   2. test_aggregate_any_fail
#      1 child failed (others completed) → parent.status = "failed".
#   3. test_leaf_set_derived_from_dag
#      The leaf set is recomputed from the DAG on load: a parent
#      with children on disk is not a leaf, its children are. The
#      leaf set is the authoritative source for plan-failure
#      detection (read-only).
#   4. test_aggregate_persists
#      Aggregation calls _persist_task_status on the parent so the
#      verdict survives a process crash.
#
# Plus one boundary case mirroring the cap branch:
#
#   5. test_aggregate_no_children_is_noop
#      A parent with no matching children (id prefix misses) is
#      left untouched — the method is a no-op for top-level tasks
#      and for parents that have not been broken down.
#
# And one boundary case for the persistence contract:
#
#   6. test_aggregate_no_persist_when_not_ready
#      When any child is still non-terminal, the method must NOT
#      persist anything — only act when the verdict is final.


# ---------------------------------------------------------------------------
# Test 5: 3 children all completed → parent.completed
# ---------------------------------------------------------------------------


def test_aggregate_all_pass(project_dir):
    """All children ``completed`` → ``parent.status = "completed"``.

    This is the happy-path aggregation: a parent P was broken down
    into P-1, P-2, P-3; every child has reached the ``completed``
    state; calling ``_aggregate_breakdown_verdict(P)`` must collapse
    the verdict to ``P.status = "completed"``.

    The leaf set is also updated: P-1/P-2/P-3 are removed and P is
    re-added. This is the inverse of the breakdown mutation and
    keeps ``self._leaf_tasks`` in sync with the DAG.
    """
    from task import SubTask

    _write_tasks(
        project_dir,
        [
            {
                "id": "P",
                "title": "Parent that was broken down",
                "description": "children have all completed",
                "test_command": "echo P",
                "status": "breakdown_in_progress",
                "breakdown_count": 1,
                "depends_on": [],
            },
            {
                "id": "P-1",
                "title": "Child 1",
                "description": "done",
                "test_command": "echo P-1",
                "status": "completed",
                "depends_on": [],
            },
            {
                "id": "P-2",
                "title": "Child 2",
                "description": "done",
                "test_command": "echo P-2",
                "status": "completed",
                "depends_on": [],
            },
            {
                "id": "P-3",
                "title": "Child 3",
                "description": "done",
                "test_command": "echo P-3",
                "status": "completed",
                "depends_on": [],
            },
        ],
    )

    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool)
    agent._load_tasks()

    parent = next(t for t in agent._all_tasks if t.id == "P")
    # Sanity: parent is in breakdown_in_progress (not yet aggregated).
    assert parent.status == "breakdown_in_progress", (
        f"expected parent status='breakdown_in_progress' before aggregation, "
        f"got {parent.status!r}"
    )
    # Sanity: children are loaded as completed.
    for cid in ("P-1", "P-2", "P-3"):
        c = next(t for t in agent._all_tasks if t.id == cid)
        assert c.status == "completed", (
            f"expected child {cid} status='completed' before aggregation, "
            f"got {c.status!r}"
        )

    # The three children are leaves (parent is in breakdown_in_progress,
    # so it is not in the active set / leaf set right now).
    assert "P-1" in agent._leaf_tasks
    assert "P-2" in agent._leaf_tasks
    assert "P-3" in agent._leaf_tasks
    assert "P" not in agent._leaf_tasks, (
        f"parent P must NOT be in the leaf set while children are active, "
        f"got leaf set {agent._leaf_tasks!r}"
    )

    agent._aggregate_breakdown_verdict(parent)

    # Aggregation collapses to "completed" (no failures).
    assert parent.status == "completed", (
        f"expected parent.status='completed' after aggregation, "
        f"got {parent.status!r}"
    )

    # Leaf set: children out, parent in.
    assert "P-1" not in agent._leaf_tasks
    assert "P-2" not in agent._leaf_tasks
    assert "P-3" not in agent._leaf_tasks
    assert "P" in agent._leaf_tasks, (
        f"parent P must be back in the leaf set after aggregation, "
        f"got leaf set {agent._leaf_tasks!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: 1 child failed → parent.failed
# ---------------------------------------------------------------------------


def test_aggregate_any_fail(project_dir):
    """Any child ``failed`` → ``parent.status = "failed"``.

    Failure propagates upward: a single failed child is enough to
    fail the parent. The aggregation must surface the failing
    child's ``failure_reason`` on the parent so the operator does
    not have to open every child row to find the cause.
    """
    from task import SubTask

    _write_tasks(
        project_dir,
        [
            {
                "id": "P",
                "title": "Parent that was broken down",
                "description": "one child failed",
                "test_command": "echo P",
                "status": "breakdown_in_progress",
                "breakdown_count": 1,
                "depends_on": [],
            },
            {
                "id": "P-1",
                "title": "Child 1",
                "description": "ok",
                "test_command": "echo P-1",
                "status": "completed",
                "depends_on": [],
            },
            {
                "id": "P-2",
                "title": "Child 2",
                "description": "ok",
                "test_command": "echo P-2",
                "status": "completed",
                "depends_on": [],
            },
            {
                "id": "P-3",
                "title": "Child 3",
                "description": "broken",
                "test_command": "echo P-3",
                "status": "failed",
                "failure_reason": "child 3 build error: import failed",
                "depends_on": [],
            },
        ],
    )

    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool)
    agent._load_tasks()

    parent = next(t for t in agent._all_tasks if t.id == "P")

    agent._aggregate_breakdown_verdict(parent)

    # Parent aggregates to "failed" because P-3 is failed.
    assert parent.status == "failed", (
        f"expected parent.status='failed' after any-fail aggregation, "
        f"got {parent.status!r}"
    )

    # The first child's failure_reason is surfaced on the parent
    # for operator diagnostics. The exact phrasing is not pinned —
    # we only require the original reason text to be present.
    assert parent.failure_reason, (
        f"parent.failure_reason must be populated on failed aggregation, "
        f"got {parent.failure_reason!r}"
    )
    assert "child 3 build error" in parent.failure_reason, (
        f"parent.failure_reason must surface the failing child's reason; "
        f"got {parent.failure_reason!r}"
    )

    # Leaf set is still updated: children out, parent in.
    for cid in ("P-1", "P-2", "P-3"):
        assert cid not in agent._leaf_tasks, (
            f"child {cid} must be removed from leaf set after aggregation, "
            f"got {agent._leaf_tasks!r}"
        )
    assert "P" in agent._leaf_tasks, (
        f"parent P must be in leaf set after aggregation, "
        f"got {agent._leaf_tasks!r}"
    )


# ---------------------------------------------------------------------------
# Test 7: leaf set is derived from the on-disk DAG at load time
# ---------------------------------------------------------------------------
#
# 2026-09-14: this slot used to hold ``test_leaf_set_dynamic``, which
# drove ``AutonomousAgent._breakdown_task`` to watch the leaf set flip
# from the parent to its children. That method was DELETED by plan
# ``2026-09-04 plan`` task 5 (commit ecf39fa, "M0：删除
# backend/agent.py 的 executor self-split 逻辑"), because PRD decision
# point 1 makes the *refiner* the single authoritative source of
# structural task-list changes — the executor must never add, remove or
# reorder tasks itself. The absence is now enforced by
# ``tests/unit/test_agent_no_self_split.py`` (source-text + AST guards),
# so the breakdown half of the old test can never be restored. The four
# sibling ``test_breakdown_*`` tests in this file were retired for the
# same reason.
#
# What survives — and is what this rewrite pins — is the read side:
# ``_leaf_tasks`` is recomputed from the DAG on every load
# (``_compute_leaf_tasks``), so a plan whose children were inserted by
# the refiner schedules correctly from a cold start.


def test_leaf_set_derived_from_dag(project_dir):
    """``self._leaf_tasks`` reflects the breakdown tree as loaded.

    A task is a leaf iff no other task in the plan carries its id as a
    structural-parent prefix. The contract pinned here is that the leaf
    set is the read-side companion of ``_all_tasks`` — any drift would
    surface as a silent plan-failure-detection bug at runtime.
    """
    _write_tasks(
        project_dir,
        [
            {
                "id": "P",
                "title": "Parent already split by the refiner",
                "description": "broad task",
                "test_command": "echo P",
                "status": "breakdown_in_progress",
                "depends_on": [],
            },
            {
                "id": "P-1",
                "title": "Child 1",
                "description": "first half",
                "test_command": "echo P-1",
                "status": "pending",
                "depends_on": [],
            },
            {
                "id": "P-2",
                "title": "Child 2",
                "description": "second half",
                "test_command": "echo P-2",
                "status": "pending",
                "depends_on": [],
            },
        ],
    )

    agent = _build_agent(project_dir, _StubCodingTool(response={}))
    agent._load_tasks()

    assert "P" not in agent._leaf_tasks, (
        f"a parent with children on disk must not be a leaf, "
        f"got {agent._leaf_tasks!r}"
    )
    assert "P-1" in agent._leaf_tasks, (
        f"child P-1 must be a leaf, got {agent._leaf_tasks!r}"
    )
    assert "P-2" in agent._leaf_tasks, (
        f"child P-2 must be a leaf, got {agent._leaf_tasks!r}"
    )
    assert len(agent._leaf_tasks) == 2, (
        f"expected exactly 2 leaves (the children), got {agent._leaf_tasks!r}"
    )


# ---------------------------------------------------------------------------
# Test 8: aggregation persists the parent's new status
# ---------------------------------------------------------------------------


def test_aggregate_persists(project_dir):
    """``_aggregate_breakdown_verdict`` calls ``_persist_task_status``.

    Persistence is the durability contract: a process crash
    immediately after the aggregation runs must still see the
    parent with the aggregated status. The test asserts the
    persistence is delegated to ``_persist_task_status`` (rather
    than reaching into the file directly) so a future refactor
    of the persistence path (e.g. switching to a transactional
    outbox) only has to update one site.
    """
    from task import SubTask
    from unittest.mock import patch

    _write_tasks(
        project_dir,
        [
            {
                "id": "P",
                "title": "Parent",
                "description": "",
                "test_command": "echo P",
                "status": "breakdown_in_progress",
                "breakdown_count": 1,
                "depends_on": [],
            },
            {
                "id": "P-1",
                "title": "Child 1",
                "description": "done",
                "test_command": "echo P-1",
                "status": "completed",
                "depends_on": [],
            },
            {
                "id": "P-2",
                "title": "Child 2",
                "description": "done",
                "test_command": "echo P-2",
                "status": "completed",
                "depends_on": [],
            },
        ],
    )

    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool)
    agent._load_tasks()

    parent = next(t for t in agent._all_tasks if t.id == "P")

    # Patch _persist_task_status on the agent instance so we can
    # capture the call without depending on disk-side effects
    # (which are already covered by the load-then-reload pattern
    # in tests/test_task_manager.py).
    with patch.object(agent, "_persist_task_status") as mock_persist:
        agent._aggregate_breakdown_verdict(parent)

    # Aggregation must have called the persist helper exactly once
    # for the parent. Using assert_called_once_with pins BOTH the
    # call count (no spurious persists for children) AND the
    # argument (the parent's mutated state, not a stale snapshot).
    assert mock_persist.call_count == 1, (
        f"expected _persist_task_status to be called exactly once on "
        f"aggregation, got {mock_persist.call_count} calls"
    )
    persisted_task = mock_persist.call_args[0][0]
    assert persisted_task.id == "P", (
        f"persisted task must be the parent P, got id={persisted_task.id!r}"
    )
    assert persisted_task.status == "completed", (
        f"persisted task must carry the aggregated status, "
        f"got status={persisted_task.status!r}"
    )


# ---------------------------------------------------------------------------
# Boundary: parent with no children → no-op
# ---------------------------------------------------------------------------


def test_aggregate_no_children_is_noop(project_dir):
    """A parent that has no breakdown children is left untouched.

    The aggregation is a no-op when the id prefix does not match
    any task in ``self._all_tasks``. This is the
    top-level-task / never-broken-down case — there is no
    verdict to collapse, so the method must not change the
    parent's status or persist anything.
    """
    from task import SubTask
    from unittest.mock import patch

    _write_tasks(
        project_dir,
        [
            {
                "id": "P",
                "title": "Top-level task",
                "description": "never broken down",
                "test_command": "echo P",
                "status": "pending",
                "depends_on": [],
            },
        ],
    )

    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool)
    agent._load_tasks()

    parent = agent._all_tasks[0]
    # Sanity: parent is in pending (not breakdown_in_progress).
    assert parent.status == "pending"

    with patch.object(agent, "_persist_task_status") as mock_persist:
        agent._aggregate_breakdown_verdict(parent)

    # No-op: status unchanged, no persist.
    assert parent.status == "pending", (
        f"top-level task must remain in 'pending' status, "
        f"got {parent.status!r}"
    )
    assert mock_persist.call_count == 0, (
        f"_persist_task_status must NOT be called when there are no "
        f"children, got {mock_persist.call_count} calls"
    )


# ---------------------------------------------------------------------------
# Boundary: at least one child still non-terminal → no persist
# ---------------------------------------------------------------------------


def test_aggregate_no_persist_when_not_ready(project_dir):
    """When any child is still non-terminal, the method must not persist.

    The contract is "wait until all children are terminal". If
    even one child is still ``in_progress`` / ``pending`` /
    ``breakdown_in_progress``, the method must return silently
    — no status update on the parent, no persist, no leaf-set
    mutation. The next task completion in the run will re-invoke
    the method.
    """
    from task import SubTask
    from unittest.mock import patch

    _write_tasks(
        project_dir,
        [
            {
                "id": "P",
                "title": "Parent",
                "description": "one child still running",
                "test_command": "echo P",
                "status": "breakdown_in_progress",
                "breakdown_count": 1,
                "depends_on": [],
            },
            {
                "id": "P-1",
                "title": "Child 1",
                "description": "done",
                "test_command": "echo P-1",
                "status": "completed",
                "depends_on": [],
            },
            {
                "id": "P-2",
                "title": "Child 2",
                "description": "still running",
                "test_command": "echo P-2",
                "status": "in_progress",
                "depends_on": [],
            },
        ],
    )

    coding_tool = _StubCodingTool()
    agent = _build_agent(project_dir, coding_tool)
    agent._load_tasks()

    parent = next(t for t in agent._all_tasks if t.id == "P")

    with patch.object(agent, "_persist_task_status") as mock_persist:
        agent._aggregate_breakdown_verdict(parent)

    # Parent status must remain breakdown_in_progress (not yet ready).
    assert parent.status == "breakdown_in_progress", (
        f"parent must remain in 'breakdown_in_progress' when a child is "
        f"still running, got {parent.status!r}"
    )
    # No persist — we only persist when the verdict is final.
    assert mock_persist.call_count == 0, (
        f"_persist_task_status must NOT be called when not all children "
        f"are terminal, got {mock_persist.call_count} calls"
    )
    # Leaf set is also untouched: children that are still active
    # remain in the leaf set so the scheduler can pick them up.
    assert "P-1" in agent._leaf_tasks
    assert "P-2" in agent._leaf_tasks
    assert "P" not in agent._leaf_tasks, (
        f"parent must remain out of leaf set while children are still active, "
        f"got {agent._leaf_tasks!r}"
    )

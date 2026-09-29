"""
Tests for AutonomousAgent plan-failure detection — PRD decision point 4.

Background
----------
PRD decision point 4 explicitly removes the "dependency_failed" status
from the state machine and the API surface. The contract is:

  * Failures do NOT propagate downstream. A failed leaf task stays
    ``status="failed"`` and is reported as a plan failure by
    :func:`_is_plan_failed`; its downstream tasks keep their own
    lifecycle (``pending`` → ``in_progress`` → terminal).
  * The plan as a whole is FAILED iff at least one **leaf** task has
    ``status="failed"``. Non-leaf ``failed`` rows are transient —
    :meth:`AutonomousAgent._aggregate_breakdown_verdict` rolls them up
    into the leaf set, so a leaf-only scan is the correct lens.
  * There is no ``_propagate_dependency_failure`` helper. The earlier
    synthesised "downstream failed because upstream failed" status has
    been removed entirely; ``_is_plan_failed`` is the entire
    plan-failure surface.

TDD spec — 4 contract tests
----------------------------
1. ``test_is_plan_failed_any_leaf_failed``:
   One leaf ``status="failed"`` in a 3-leaf list → ``_is_plan_failed``
   returns True.

2. ``test_is_plan_failed_all_completed``:
   Three leaves, all ``status="completed"`` → ``_is_plan_failed``
   returns False (plan PASSED).

3. ``test_status_enum_no_dependency_failed``:
   ``_TERMINAL_TASK_STATUSES`` does NOT contain ``"dependency_failed"``
   and the SubTask state-machine vocabulary the operator-facing
   surface depends on (the literal ``frozenset``) has no entry for
   it. This pins the "removed from state machine" half of PRD
   decision point 4.

4. ``test_no_propagate_call_in_main_loop``:
   The ``_run_async`` method body of :class:`AutonomousAgent` does
   NOT contain a call to ``_propagate_dependency_failure`` (the
   removed propagation helper). This pins the "removed from main
   loop" half of PRD decision point 4.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

from task import SubTask
from agent import _is_plan_failed, _TERMINAL_TASK_STATUSES, AutonomousAgent


# Ensure backend/ is on sys.path so `import agent` works regardless of
# which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_task(task_id: str, status: str, parent_id: str = "") -> SubTask:
    """Build a minimal SubTask with the given id and status.

    The ``_is_plan_failed`` helper only reads ``.id`` and ``.status``
    for the verdict — the leaf-set computation is a function of
    ``.id`` (the prefix check). All other fields are irrelevant for
    these tests.
    """
    return SubTask(
        id=task_id,
        title=f"task {task_id}",
        description="",
        test_command="",
        status=status,
    )


# ---------------------------------------------------------------------------
# Test 1: any leaf failed → plan failed
# ---------------------------------------------------------------------------


def test_is_plan_failed_any_leaf_failed():
    """One leaf ``failed`` in a 3-leaf list → True.

    Mirrors the spec example: a leaf set of three tasks where exactly
    one has ``status="failed"`` must report the plan as failed. This
    is the central "leaf-driven failure" contract — there is no
    aggregate scoring or threshold; one failed leaf is enough.
    """
    leaf_tasks = [
        _make_task("A", "completed"),
        _make_task("B", "failed"),
        _make_task("C", "completed"),
    ]

    assert _is_plan_failed(leaf_tasks) is True, (
        "a single failed leaf must mark the plan as failed"
    )


# ---------------------------------------------------------------------------
# Test 2: all leaves completed → plan passed
# ---------------------------------------------------------------------------


def test_is_plan_failed_all_completed():
    """Three leaves, all completed → False.

    The mirror of test 1: when every leaf has ``status="completed"``
    the plan has passed. ``_is_plan_failed`` returning False is the
    signal the dispatcher uses to set the plan-wide ``success`` stop
    reason.
    """
    leaf_tasks = [
        _make_task("A", "completed"),
        _make_task("B", "completed"),
        _make_task("C", "completed"),
    ]

    assert _is_plan_failed(leaf_tasks) is False, (
        "all-completed leaves must NOT mark the plan as failed"
    )


# ---------------------------------------------------------------------------
# Test 3: status vocabulary — no ``dependency_failed``
# ---------------------------------------------------------------------------


def test_status_enum_no_dependency_failed():
    """``_TERMINAL_TASK_STATUSES`` has no ``"dependency_failed"`` entry.

    PRD decision point 4: the synthesised ``dependency_failed`` status
    is removed from the state machine. The terminal-status set is
    the operator-facing surface of the task vocabulary (the
    ``frozenset`` is what ``_filter_terminal_tasks`` and
    :meth:`AutonomousAgent._get_active_tasks_for_scheduling` consult
    to decide what is "done"). A regression that re-introduces the
    string here would re-enable the propagation path on the read
    side, so we pin its absence directly.
    """
    assert "dependency_failed" not in _TERMINAL_TASK_STATUSES, (
        f"terminal statuses must not include 'dependency_failed' "
        f"(PRD decision point 4); got {_TERMINAL_TASK_STATUSES!r}"
    )

    # Belt-and-braces: the SubTask constructor itself does not define
    # an Enum (status is a free-form str), so the canonical place to
    # pin the vocabulary is the terminal set. We also confirm the
    # exact union is the contract — adding a status (e.g. a new
    # "dependency_failed" alias) would also break PRD decision point
    # 4 even if it slipped past the previous assertion, so we pin the
    # full set.
    #
    # ``superseded`` is a legitimate fourth member (2026-09-08 plan):
    # refiner-orphan residue in state.db that is NOT in ``tasks.json``
    # and must not be re-executed. It is not a ``dependency_failed``
    # reintroduction — see ``agent._TERMINAL_TASK_STATUSES``.
    assert _TERMINAL_TASK_STATUSES == frozenset({
        "completed",
        "failed",
        "skipped",
        "superseded",
    }), (
        f"terminal statuses must be exactly the 4-status union; "
        f"got {_TERMINAL_TASK_STATUSES!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: main loop never calls the removed helper
# ---------------------------------------------------------------------------


def test_no_propagate_call_in_main_loop():
    """``AutonomousAgent._run_async`` does not call ``_propagate_dependency_failure``.

    The removed helper was the "downstream propagation" path: when an
    upstream task failed, it marked every downstream task
    ``status="dependency_failed"``. PRD decision point 4 deletes that
    propagation step. The dispatcher must therefore not invoke the
    removed helper. We pin this directly by reading the AST of the
    method body and asserting the call name is absent.
    """
    # Locate the method source via static analysis — no need to run
    # the agent. The function body is large enough that grepping the
    # raw text is brittle, so we go through ``ast`` and check the
    # ``ast.Call`` node names.
    agent_path = Path(_BACKEND_DIR) / "agent.py"
    source = agent_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Find the ``_run_async`` function definition.
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_run_async":
                target = node
                break
    assert target is not None, (
        "AutonomousAgent._run_async must exist in agent.py"
    )

    # Walk every ``ast.Call`` inside the method body looking for a
    # call to ``_propagate_dependency_failure``. We only care about
    # the local name — ``self._propagate_dependency_failure(...)`` is
    # also caught by the same check (the attribute access is a
    # ``Call`` whose ``func`` is an ``Attribute`` whose ``attr`` is
    # the name we want).
    forbidden_names = {"_propagate_dependency_failure"}
    found_calls: list[str] = []
    for sub in ast.walk(target):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name) and func.id in forbidden_names:
                found_calls.append(func.id)
            elif isinstance(func, ast.Attribute) and func.attr in forbidden_names:
                found_calls.append(func.attr)

    assert not found_calls, (
        f"AutonomousAgent._run_async must not call any of "
        f"{forbidden_names!r} (PRD decision point 4 removed the "
        f"propagation path); found calls: {found_calls}"
    )

    # Belt-and-braces: also assert the helper is not defined on the
    # class. If a future refactor re-introduces the helper but does
    # not call it from _run_async, the test above would still pass —
    # so we add a second pin that catches "helper exists again" at
    # all. The class should be a stranger to that name.
    class_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AutonomousAgent":
            class_node = node
            break
    assert class_node is not None, "AutonomousAgent class must exist"

    class_method_names = {
        n.name for n in class_node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_propagate_dependency_failure" not in class_method_names, (
        "AutonomousAgent must not define _propagate_dependency_failure "
        "(PRD decision point 4 removed the helper); the class body "
        f"contains: {sorted(m for m in class_method_names if 'propagate' in m)}"
    )

    # Final check: the module-level free function (if any) should also
    # not be named that. We pin the absence of the symbol at the
    # module level too.
    module_func_names = {
        n.name for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_propagate_dependency_failure" not in module_func_names, (
        "module-level _propagate_dependency_failure must not exist "
        "(PRD decision point 4 removed the helper)"
    )


# ---------------------------------------------------------------------------
# Test extras: edge cases (kept on the file so a regression on the
# "is it a leaf?" predicate is caught here, not in the breakdown tests)
# ---------------------------------------------------------------------------


def test_is_plan_failed_empty_input():
    """Empty task list → False (no work, no failure)."""
    assert _is_plan_failed([]) is False


def test_is_plan_failed_non_leaf_failed_not_counted():
    """A non-leaf ``failed`` is NOT counted by the leaf-only scan.

    When a parent has children, the parent is a non-leaf in the
    breakdown tree. The leaf-only scan must ignore it — the verdict
    lives on the children. The breakdown/aggregation flow is
    responsible for rolling the children's statuses up to the
    parent; ``_is_plan_failed`` is the read-side query and trusts the
    caller to invoke it on a terminal snapshot.
    """
    leaf_tasks = [
        _make_task("P", "failed"),  # non-leaf (has children P-1, P-2)
        _make_task("P-1", "completed"),
        _make_task("P-2", "completed"),
    ]

    assert _is_plan_failed(leaf_tasks) is False, (
        "non-leaf 'failed' must not mark the plan as failed — the "
        "breakdown/aggregation flow rolls the children's verdict up "
        "to the parent, and the leaf-only scan intentionally trusts "
        "the caller to invoke it on a terminal snapshot"
    )


def test_is_plan_failed_skipped_leaves_only():
    """All leaves ``skipped`` → False (no failure, just no work)."""
    leaf_tasks = [
        _make_task("A", "skipped"),
        _make_task("B", "skipped"),
    ]
    assert _is_plan_failed(leaf_tasks) is False, (
        "all-skipped leaves must NOT mark the plan as failed"
    )

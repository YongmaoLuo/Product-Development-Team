"""Contract: dangling references are NOT cycles (2026-09-21).

Regression for a production plan, which died with:

    Unexpected error for task [3]: Cycle detected: 10, 11, 12, 13, 14,
    15, 16, 17, 3-1, 3-2, 3-3, 3-4, 4, 5, 6, 7, 8, 9

There was no cycle. The refiner had split parent ``3`` into
``3-1..3-4``; one stale ``depends_on`` edge pointing at the removed
parent was enough for ``task_manager._ensure_acyclic`` to report the
target's ENTIRE downstream cone as cycle members, because its Kahn
residual counted a missing ``depends_on`` target as unmet in-degree
(``len(deps)`` counts it; the reverse adjacency is only built for ids
that exist, so the count can never reach zero).

Three questions are now answered separately:

* :func:`framework.task_graph.find_dangling_references` — the actionable
  cause;
* :func:`framework.task_graph.find_cycle_groups` — genuine cycle members
  (Tarjan SCC of size ≥ 2, plus self-loops);
* everything else is merely *blocked*, and is reported as neither.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from framework.task_graph import (  # noqa: E402
    DanglingTaskDependency,
    find_cycle_groups,
    find_dangling_references,
    find_self_loops,
)
from task import SubTask  # noqa: E402
from task_manager import CycleInTaskGraph, _ensure_acyclic  # noqa: E402


def _task(task_id: str, depends_on=None) -> SubTask:
    return SubTask(
        id=task_id,
        title=task_id,
        description="",
        test_command="echo noop",
        files_to_modify=["__NO_FILE_CHANGES__"],
        depends_on=list(depends_on or []),
    )


def _chain_of(n: int) -> list:
    """``root`` plus ``n`` tasks, each depending on ``root``.

    Mirrors the 0921 shape: one node everything else hangs off.
    """
    tasks = [_task("root")]
    for i in range(n):
        tasks.append(_task(f"t{i}", depends_on=["root"]))
    return tasks


# ---------------------------------------------------------------------------
# Dangling references
# ---------------------------------------------------------------------------


class TestDanglingReferences:
    def test_detects_the_pair_in_input_order(self):
        tasks = [_task("root"), _task("a", ["root"]), _task("b", ["gone"])]
        assert find_dangling_references(tasks) == [("b", "gone")]

    def test_clean_graph_has_none(self):
        assert find_dangling_references(_chain_of(3)) == []

    def test_message_names_both_ends(self):
        exc = DanglingTaskDependency([("3-1", "3")])
        assert "3-1" in str(exc)
        assert "3" in str(exc)
        assert "Cycle" not in str(exc)

    def test_is_a_valueerror_for_legacy_callers(self):
        assert issubclass(DanglingTaskDependency, ValueError)


# ---------------------------------------------------------------------------
# The 0921 regression
# ---------------------------------------------------------------------------


class TestTheNegativeCentOneEighteenTaskSignature:
    """One dangling edge must not become an 18-node "cycle"."""

    def _broken_cone(self):
        tasks = _chain_of(17)
        # The root itself references a task that is not in the list.
        tasks[0].depends_on = ["removed-parent"]
        return tasks

    def test_reports_a_dangling_reference_not_a_cycle(self):
        tasks = self._broken_cone()
        with pytest.raises(DanglingTaskDependency) as excinfo:
            _ensure_acyclic(tasks)
        assert "root" in str(excinfo.value)
        assert "removed-parent" in str(excinfo.value)
        assert "Cycle" not in str(excinfo.value)

    def test_does_not_name_the_downstream_cone(self):
        """The old bug reported every downstream task as a cycle member."""
        tasks = self._broken_cone()
        with pytest.raises(DanglingTaskDependency):
            _ensure_acyclic(tasks)
        # Nothing in the cone is a cycle member — there is no cycle.
        assert find_cycle_groups(tasks) == []


# ---------------------------------------------------------------------------
# Real cycles are still caught, and only the real members are named
# ---------------------------------------------------------------------------


class TestCycles:
    def test_two_node_cycle_names_only_its_members(self):
        tasks = [
            _task("a", ["b"]),
            _task("b", ["a"]),
            _task("downstream", ["a"]),
        ]
        assert find_cycle_groups(tasks) == [["a", "b"]]
        with pytest.raises(CycleInTaskGraph) as excinfo:
            _ensure_acyclic(tasks)
        members = excinfo.value.members
        assert members == ["a", "b"], (
            f"only the real cycle members may be named, got {members}"
        )
        assert "downstream" not in str(excinfo.value)

    def test_self_loop(self):
        assert find_self_loops([_task("self", ["self"])]) == ["self"]
        with pytest.raises(CycleInTaskGraph) as excinfo:
            _ensure_acyclic([_task("self", ["self"])])
        assert excinfo.value.members == ["self"]

    def test_clean_dag_passes(self):
        _ensure_acyclic(_chain_of(5))  # must not raise


# ---------------------------------------------------------------------------
# The dispatcher-side validator agrees
# ---------------------------------------------------------------------------


def test_agent_validator_raises_dangling_not_cycle():
    """``agent._validate_dependencies`` must use the same vocabulary."""
    from agent import _validate_dependencies

    tasks = _chain_of(3)
    tasks[0].depends_on = ["gone"]
    with pytest.raises(DanglingTaskDependency) as excinfo:
        _validate_dependencies(tasks)
    assert "Cycle" not in str(excinfo.value)


def test_agent_validator_cycle_names_only_members():
    from agent import _validate_dependencies

    tasks = [_task("a", ["b"]), _task("b", ["a"]), _task("c", ["a"])]
    with pytest.raises(ValueError) as excinfo:
        _validate_dependencies(tasks)
    message = str(excinfo.value)
    assert message == "Cycle detected: a, b", message

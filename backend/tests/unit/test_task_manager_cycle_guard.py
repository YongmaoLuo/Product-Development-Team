"""
Tests for the ``TaskManager`` cycle guard.

Background (audit 2026-09-04 plan):
The LLM-generated tasks.json for that plan contained cycles:
  * 4-2-2 →4-2-3 →4-2-2 (mutual)
  * 8-1 ↔8-2 ↔8-3 ↔8-4 (4-way mutual)
Neither the dispatcher gate nor the validator caught the cycle at
write time — both run on ``_load_tasks`` AFTER the bad state is
already persisted, so the only recovery was to manually edit
``tasks.json`` on disk. The cycle guard here is the prevention
mechanism: ``set_tasks`` and ``save_tasks`` refuse to persist a
task list whose ``depends_on`` graph contains a cycle.

These tests assert the contract:
  1. acyclic graphs pass through ``_ensure_acyclic``
  2. ``CycleInTaskGraph`` is raised for multi-node cycles
  3. self-loops are reported as a single-member cycle
  4. the dispatcher-cycle case (the 2026-09-05 audit) is caught
  5. ``TaskManager.set_tasks`` raises ``CycleInTaskGraph`` BEFORE
     swapping ``self.tasks`` (so the previous good state survives)
  6. ``TaskManager.save_tasks`` raises ``CycleInTaskGraph`` BEFORE
     writing to disk
"""

import os
import sys
from pathlib import Path

import pytest

# Resolve the backend directory relative to THIS test file rather than
# hard-coding ``a sibling checkout's backend`` (an absolute path that
# breaks on any other dev machine or repo location).
# Layout: <repo>/backend/tests/unit/test_task_manager_cycle_guard.py
# BACKEND_DIR resolves to ``<repo>/backend``.
BACKEND_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_DIR))

from task import SubTask  # noqa: E402
from task_manager import (  # noqa: E402
    CycleInTaskGraph,
    TaskManager,
    _ensure_acyclic,
)


def _make(id_: str, *, deps=()):
    """Build a SubTask with the minimum required fields."""
    return SubTask(id=id_, title=f"task {id_}", description="", depends_on=list(deps))


def test_ensure_acyclic_accepts_linear_dag():
    """A →B →C is a valid DAG — the guard must NOT raise."""
    tasks = [
        _make("1"),
        _make("2", deps=("1",)),
        _make("3", deps=("2",)),
    ]
    _ensure_acyclic(tasks)  # must not raise


def test_ensure_acyclic_rejects_two_node_cycle():
    """A →B →A must raise ``CycleInTaskGraph`` with both members."""
    tasks = [
        _make("A", deps=("B",)),
        _make("B", deps=("A",)),
    ]
    with pytest.raises(CycleInTaskGraph) as exc_info:
        _ensure_acyclic(tasks)
    assert set(exc_info.value.members) == {"A", "B"}
    # CycleInTaskGraph must remain catchable as ValueError for
    # backward-compat with ``except ValueError`` callers.
    assert isinstance(exc_info.value, ValueError)


def test_ensure_acyclic_rejects_self_loop():
    """A →A is a cycle — the guard catches it via the pre-pass."""
    tasks = [_make("A", deps=("A",))]
    with pytest.raises(CycleInTaskGraph) as exc_info:
        _ensure_acyclic(tasks)
    assert exc_info.value.members == ["A"]


def test_ensure_acyclic_rejects_plan_20260101_audit_cycle():
    """Regression: plan 2026-09-04 cycle graph.

    4-2-2 ↔4-2-3 (mutual) AND 8-1 ↔8-2 ↔8-3 ↔8-4 (4-way mutual) +
    53 → ... → 9 → 8-4 → ... closes the macro-cycle. The audit
    cycle caught all six node ids — the guard must surface ALL of
    them at once so the LLM regeneration can see the full scope of
    the problem.
    """
    tasks = [
        _make("4-2-1"),
        _make("4-2-2", deps=("4-2-1", "4-2-3")),
        _make("4-2-3", deps=("4-2-2",)),
        _make("8-1", deps=("8-2", "8-3", "8-4")),
        _make("8-2", deps=("8-1", "8-3", "8-4")),
        _make("8-3", deps=("8-2",)),
        _make("8-4", deps=("8-3", "8-1", "8-2")),
    ]
    with pytest.raises(CycleInTaskGraph) as exc_info:
        _ensure_acyclic(tasks)
    # The cycle members include at least the 6 nodes that form the
    # strongly-connected component — exact set depends on Kahn's
    # residual frontier but should always include all six.
    assert {"4-2-2", "4-2-3", "8-1", "8-2", "8-3", "8-4"}.issubset(
        set(exc_info.value.members)
    )


def test_set_tasks_rejects_cycle_before_swapping_state(tmp_path):
    """``TaskManager.set_tasks`` must reject cycles BEFORE replacing
    ``self.tasks`` — the previous good state must survive so the
    executor can keep running with the last-known-good plan."""
    tasks_file = tmp_path / "tasks.json"
    # Initial good state.
    initial = [
        {"id": "1", "title": "first", "description": "", "depends_on": []},
        {"id": "2", "title": "second", "description": "", "depends_on": ["1"]},
    ]
    import json as _json
    tasks_file.write_text(_json.dumps({
        "requirement": "",
        "stop_reason": None,
        "reason_detail": None,
        "tasks": initial,
    }))

    tm = TaskManager(project_dir=tmp_path, tasks_file=tasks_file)
    assert [t.id for t in tm.tasks] == ["1", "2"]

    # New payload contains a cycle.
    bad_payload = [
        {"id": "3", "title": "third", "description": "", "depends_on": ["4"]},
        {"id": "4", "title": "fourth", "description": "", "depends_on": ["3"]},
    ]
    with pytest.raises(CycleInTaskGraph):
        tm.set_tasks(bad_payload)

    # Previous state must be intact (NOT swapped to the cycle).
    assert [t.id for t in tm.tasks] == ["1", "2"]
    # And the on-disk file must be untouched (set_tasks aborted
    # before save_tasks was called).
    on_disk = _json.loads(tasks_file.read_text())
    assert [t["id"] for t in on_disk["tasks"]] == ["1", "2"]


def test_save_tasks_rejects_cycle_before_writing(tmp_path):
    """``TaskManager.save_tasks`` must reject cycles BEFORE writing
    to disk — directly mutating ``tm.tasks`` to introduce a cycle
    must not silently persist broken state."""
    tasks_file = tmp_path / "tasks.json"
    import json as _json
    initial = [
        {"id": "1", "title": "first", "description": "", "depends_on": []},
    ]
    tasks_file.write_text(_json.dumps({
        "requirement": "",
        "stop_reason": None,
        "reason_detail": None,
        "tasks": initial,
    }))

    tm = TaskManager(project_dir=tmp_path, tasks_file=tasks_file)
    # In-memory mutate to introduce a cycle.
    tm.tasks.append(SubTask(id="2", title="loop", description="", depends_on=["2"]))

    with pytest.raises(CycleInTaskGraph):
        tm.save_tasks()

    # On-disk file must NOT have been overwritten with the cycle.
    on_disk = _json.loads(tasks_file.read_text())
    assert [t["id"] for t in on_disk["tasks"]] == ["1"]


def test_cycle_in_task_graph_is_value_error_subclass():
    """``CycleInTaskGraph`` must inherit from ``ValueError`` so
    existing ``except ValueError`` blocks (e.g. the dispatcher's
    cycle detector at ``agent._validate_dependencies``) continue
    to catch it without code changes."""
    assert issubclass(CycleInTaskGraph, ValueError)
    err = CycleInTaskGraph(["A", "B"])
    assert str(err) == "Cycle detected: A, B"
"""``auto_fix`` must never inject a self-dependency.

Background
----------
``TaskOutputValidator.auto_fix`` walks each task's ``description`` for
``task-N`` references and appends any that are missing from
``depends_on``. A task whose description mentions **its own** id
("task 2-1 rewrites the config loader") therefore used to gain
``depends_on=["2-1"]``.

The runtime gate ``is_dependency_ready`` (agent.py) can never satisfy a
self-edge — a task is not its own upstream — so the task is deferred on
every dispatcher tick and never runs. Plan
``2026-08-07 plan`` logged it verbatim:

    same_id_loop_reload_failed: Task 2-1 cannot depend on itself

The blast radius grew on 2026-09-13: ``_load_tasks`` now re-points
``task_manager.tasks`` at the post-read-gate snapshot, so an injected
self-edge also reaches ``TaskManager.save_tasks`` → ``_ensure_acyclic``
(which raises ``CycleInTaskGraph``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from framework.task_output_validator import TaskOutputValidator  # noqa: E402
from task import SubTask, NO_FILE_CHANGES_SENTINEL  # noqa: E402


def _task(**overrides) -> SubTask:
    payload = {
        "id": "2-1",
        "title": "Rewrite the config loader",
        "description": "Implement the loader.",
        "test_command": "pytest tests/unit/test_task_output_validator.py -v",
        "files_to_modify": list(NO_FILE_CHANGES_SENTINEL),
        "depends_on": [],
        "status": "pending",
    }
    payload.update(overrides)
    return SubTask(**payload)


def test_auto_fix_does_not_inject_self_dependency(tmp_path: Path) -> None:
    """A description naming its own id must leave depends_on empty."""
    validator = TaskOutputValidator(tmp_path)
    snapshot = [
        _task(description="Task 2-1 rewrites the config loader end to end."),
    ]

    fixed = validator.auto_fix(snapshot)

    assert fixed[0].depends_on == [], (
        f"auto_fix injected a self-edge: {fixed[0].depends_on!r}. "
        f"``is_dependency_ready`` can never satisfy a task depending on "
        f"itself, so the task would be deferred forever."
    )
    # The caller's snapshot must stay untouched (contract 3).
    assert snapshot[0].depends_on == []


def test_auto_fix_still_injects_genuine_cross_task_dependency(
    tmp_path: Path,
) -> None:
    """The self-edge guard must not suppress real cross-task references.

    Note the normalisation: ``_normalize_dep_token`` reduces ``task-3``
    and ``3`` to the bare ``3`` (the runtime's id namespace), which is
    also why the self-edge case above compares normalised ids rather
    than raw ones.
    """
    validator = TaskOutputValidator(tmp_path)
    snapshot = [
        _task(id="2-1", description="Depends on task 3 before starting."),
        _task(id="3", description="Standalone work."),
    ]

    fixed = validator.auto_fix(snapshot)

    assert fixed[0].depends_on == ["3"], (
        f"expected the cross-task reference to survive, got "
        f"{fixed[0].depends_on!r}"
    )
    assert fixed[1].depends_on == []


def test_auto_fix_self_edge_does_not_trip_the_cycle_guard(
    tmp_path: Path,
) -> None:
    """The fixed snapshot must be writable (no ``CycleInTaskGraph``).

    ``TaskManager.save_tasks`` runs ``_ensure_acyclic`` before writing;
    a self-edge raises there and aborts the completion bookkeeping.
    """
    from task_manager import TaskManager

    project_dir = tmp_path / "self-edge-plan"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "tasks.json").write_text(
        '{"requirement": "r", "tasks": []}', encoding="utf-8"
    )

    validator = TaskOutputValidator(project_dir)
    fixed = validator.auto_fix([
        _task(description="Task 2-1 rewrites the config loader."),
    ])

    manager = TaskManager(project_dir=project_dir)
    manager.tasks = fixed
    manager.save_tasks()  # must not raise

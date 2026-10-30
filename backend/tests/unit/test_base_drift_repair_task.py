"""The repair task a base-drift conflict turns into.

A verification round that stands on a branch the base has moved past
does not need *more* analysis — it needs the conflict resolved.  The
``repair`` mode turns the conflict report into exactly one executable
task, and that task has to survive two gates:

* the **id shape** the executor recognises (``repair-r<round>-<seq>``).
  A task whose id falls outside that shape is a task the executor
  never picks up: the round reports "the repair batch drained" while
  the repair is still sitting in the queue.
* the **field allow-list** of ``state.db``.  ``add_task`` *rejects* a
  payload carrying a field it does not know — it does not filter it out
  — so a task with one stray key is a task that cannot be written at
  all.

The builder is a **pure function**: it turns a report into a dict and
stops there.  Writing the task to the database belongs to the repair
channel that already exists, not to this module — a module that both
builds the task and writes it would have two owners for one row.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import base_drift  # noqa: E402
from base_drift import (  # noqa: E402
    STATUS_CONFLICTS,
    BaseDriftReport,
    build_conflict_repair_task,
)
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    ALLOWED_STATIC_TASK_FIELDS,
)

PLAN_ID = "p1"
PROJECT_DIR = Path("/tmp/wt")
BASE_SHA = "1a2b3c4d5e6f"


def _report(**over):
    """A conflicting base-drift report, with the given fields replaced."""
    payload = {
        "status": STATUS_CONFLICTS,
        "base_ref": "origin/main",
        "base_sha": BASE_SHA,
        "behind": 2,
        "conflict_files": [{"path": "app.py", "conflict_blocks": 1}],
        "mode": "repair",
    }
    payload.update(over)
    return BaseDriftReport(**payload)


def _task(report=None, **over):
    """Build one repair task from ``report`` (a conflicting one by default)."""
    kwargs = {
        "plan_id": PLAN_ID,
        "project_dir": PROJECT_DIR,
        "round_number": 3,
        "seq": 1,
    }
    kwargs.update(over)
    return build_conflict_repair_task(
        report if report is not None else _report(), **kwargs
    )


def test_task_id_group_and_round():
    """The id is the shape the executor picks up; the group says why.

    ``repair-r3-01`` is the id the repair round writes and the id
    ``verification_loop._is_repair_task_id`` recognises.  The group
    starts with ``repair`` for the same reason — it is the fallback
    marker for rows whose id shape is unusual.
    """
    task = _task()

    assert task["id"] == "repair-r3-01"
    assert re.fullmatch(r"repair-r\d+-\d{2}", task["id"]), task["id"]
    assert task["task_group"] == "repair"
    assert task["round"] == 3
    assert task["failed_vp_id"] == "__base_drift__"
    assert task["priority"] == "P0"


def test_files_to_modify_are_conflict_paths():
    """The change scope is exactly the paths the merge left unmerged.

    A task whose scope names a file nobody has to touch sends the
    executor off to change code that was never in conflict; a task that
    omits a conflicting file leaves the conflict in place and the next
    round reports the very same drift again.
    """
    report = _report(
        conflict_files=[
            {"path": "app.py", "conflict_blocks": 1},
            {"path": "other.py", "conflict_blocks": 2},
        ]
    )

    task = _task(report=report)

    assert task["files_to_modify"] == ["app.py", "other.py"]


def test_description_carries_evidence():
    """The description carries everything the fixer needs, and no more.

    The base ref and SHA say *what* is being merged in, ``behind`` says
    how far the branch drifted, every conflicting file is named with the
    number of conflict blocks in it, and the steps say what a correct
    resolution looks like — merge the base, resolve block by block, keep
    both sides' intent, run the affected tests.

    A description that leaves out the SHA leaves the fixer guessing
    which commit to merge; one that leaves out the block counts leaves
    it guessing how much conflict there is.
    """
    report = _report(
        conflict_files=[
            {"path": "app.py", "conflict_blocks": 1},
            {"path": "other.py", "conflict_blocks": 3},
        ]
    )

    description = _task(report=report)["description"]

    assert "base_ref: origin/main" in description
    assert f"base_sha: {BASE_SHA}" in description
    assert "behind: 2" in description
    assert "app.py 冲突块 1" in description
    assert "other.py 冲突块 3" in description
    # The steps: merge the base, resolve block by block, keep both
    # sides' intent, run the affected tests.
    assert f"git merge {BASE_SHA}" in description
    assert "逐块解决" in description
    assert "保留双方意图" in description
    assert "跑受影响测试" in description


def test_description_names_plan_id():
    """The plan id lands on the first line of the description, and only there.

    The repair task is written into a plan's task queue; a fixer that
    does not know which plan the conflict belongs to cannot report the
    outcome anywhere.  The description is the only place the plan id
    may appear — the task row has no column for it.
    """
    description = _task()["description"]

    lines = description.splitlines()
    assert lines[0] == f"plan={PLAN_ID}", description
    assert description.count(PLAN_ID) == 1, description


def test_only_allowlisted_fields():
    """Every key the builder emits is a key the task table accepts.

    ``add_task`` raises on a payload carrying an unknown field, so a
    single extra key (``status`` is the classic one — it is a runtime
    mirror field owned by ``update_task``) makes the task unwritable.
    """
    task = _task()

    assert set(task).issubset(ALLOWED_STATIC_TASK_FIELDS), sorted(
        set(task) - ALLOWED_STATIC_TASK_FIELDS
    )
    assert "status" not in task


def test_no_conflict_files_returns_none():
    """A report without conflicting files produces no task.

    There is nothing to repair: a task without a change scope sends the
    executor off to "fix" files that were never in conflict.
    """
    assert _task(report=_report(conflict_files=[])) is None


def test_the_builder_never_touches_the_database():
    """This module builds the task; the repair channel writes it.

    The persistence layer has one owner.  A builder that also opened a
    connection would be a second one, and the row it wrote would not
    go through the same validation the repair channel applies.
    """
    source = (BACKEND_DIR / "base_drift.py").read_text(encoding="utf-8")

    for forbidden in ("PlanTaskRepository", "sqlite3", "connection.open"):
        assert forbidden not in source, forbidden

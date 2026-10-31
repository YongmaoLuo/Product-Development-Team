"""A base-drift merge task has to survive the orphan reconciliation.

``base_drift.build_conflict_repair_task`` produces a *dict* and
``verification_agent.persist_conflict_repair_task`` stores it in
``plan_tasks``.  That is where the story used to end — and where the
task used to die.

The executor does not read the repair channel's rows directly.  It
reconciles the ``plan_tasks`` table against the on-disk ``tasks.json``:
every row whose id is *not* in the file is an **orphan**, and for each
orphan the loader decides between

  * **merging** the row's real content into a runnable ``SubTask``,
  * **substituting** a content-free placeholder.

The decision is :func:`orphan_rules.has_mergeable_content`, and it
requires a ``title`` *and* a ``description``.  A conflict task that
carried only a description was therefore always downgraded to a
placeholder: the merge never ran, and the round reported a drained
repair batch while the conflict was still sitting in the working tree.

So this file closes the loop end to end — build the task, persist it,
read it back as the orphan the executor would see, and assert that
what comes back is **mergeable**, not a placeholder:

* the persisted row carries a title and a description, so
  ``has_mergeable_content`` says yes;
* the merged ``SubTask`` kwargs carry the title, the description and
  the merge-probe command;
* the row keeps the markers the repair chain needs (``task_group``,
  ``failed_vp_id``) and the id the executor recognises.

The two negative controls matter as much as the positives: a row with
only a description, and a row with only a title, must BOTH come back
unmergeable.  Without them, a future edit to ``MERGE_REQUIRED_FIELDS``
that loosened the gate — or a builder that quietly stopped emitting a
title — would still leave the positive tests green.

Every write goes to ``tmp_path``.  The operator's live ``state.db`` is
never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import server  # noqa: F401  — imported first: server wires verification_loop
import verification_agent
import verification_loop
from base_drift import (  # noqa: E402
    BASE_DRIFT_VP_ID,
    REPAIR_TASK_GROUP,
    STATUS_CONFLICTS,
    BaseDriftReport,
    build_conflict_repair_task,
)
from orphan_rules import has_mergeable_content, merged_subtask_kwargs  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)

PLAN_ID = "p1"
BASE_SHA = "abc123"


def _report():
    """A conflicting base-drift report — two files in conflict."""
    return BaseDriftReport(
        status=STATUS_CONFLICTS,
        base_ref="origin/main",
        base_sha=BASE_SHA,
        behind=2,
        conflict_files=[
            {"path": "a.py", "conflict_blocks": 1},
            {"path": "b.py", "conflict_blocks": 2},
        ],
        mode="repair",
    )


def _task(project_dir, **over):
    """Build one repair task from the conflicting report."""
    kwargs = {
        "plan_id": PLAN_ID,
        "project_dir": project_dir,
        "round_number": 3,
        "seq": 1,
    }
    kwargs.update(over)
    return build_conflict_repair_task(_report(), **kwargs)


def _persist(task, db_path):
    """Write ``task`` into ``db_path`` through the repair channel."""
    assert verification_agent.persist_conflict_repair_task(
        PLAN_ID, task, db_path=db_path
    ) is True


def _orphans(db_path, plan_id=PLAN_ID):
    """Every row of ``plan_id``, read back as the executor's orphans.

    ``disk_ids`` is empty on purpose: nothing is on disk, so *every*
    row in the table is an orphan — exactly the situation the base
    drift repair channel creates, since it writes straight to
    ``plan_tasks`` without touching ``tasks.json``.
    """
    conn = open_db(db_path)
    try:
        return list(
            PlanTaskRepository(conn).iter_orphan_tasks(plan_id, disk_ids=set())
        )
    finally:
        conn.close()


def test_persisted_row_is_mergeable(tmp_path):
    """The row the executor reads back is merged, not replaced.

    This is the whole point of the file.  The task is built, stored,
    and read back through the very iterator the loader uses; the row
    that comes out has to satisfy the merge gate.  A row that fails the
    gate is handed to the subagent as a placeholder — real content
    destroyed, the merge never runs, and the round reports success
    while the conflict is still there.
    """
    db_path = tmp_path / "state.db"
    task = _task(tmp_path)
    _persist(task, db_path)

    rows = _orphans(db_path)

    assert len(rows) == 1, rows
    row = rows[0]
    assert row["id"] == "repair-r3-01"
    assert has_mergeable_content(row) is True


def test_merged_kwargs_carry_title_description_and_test_command(tmp_path):
    """What the loader would build from the row is the real task.

    ``merged_subtask_kwargs`` is the payload the orphan merge hands to
    the ``SubTask`` constructor.  Three things have to survive the
    round trip through the database:

    * the **title** — what the queue shows and what the gate needs;
    * the **description** — what tells the agent this is a base merge
      rather than a product-code fix;
    * the **test command** — the probe that asks whether the merge is
      actually done.

    The row's own markers come through as well: ``task_group`` decides
    whether the refiner may delete the task at all, and ``failed_vp_id``
    is what the verification round reports the task against.
    """
    db_path = tmp_path / "state.db"
    task = _task(tmp_path)
    _persist(task, db_path)

    row = next(iter(_orphans(db_path)))
    kwargs = merged_subtask_kwargs(row)

    assert kwargs["title"] == task["title"]
    assert kwargs["description"] == task["description"]
    assert kwargs["test_command"].startswith("git merge-base --is-ancestor")
    assert kwargs["task_group"] == REPAIR_TASK_GROUP

    # The markers the repair chain keys off are carried through the
    # database untouched — they are not derived from the row.
    assert row["task_group"] == REPAIR_TASK_GROUP
    assert row["failed_vp_id"] == BASE_DRIFT_VP_ID


def test_row_without_title_is_not_mergeable(tmp_path):
    """Negative control: a description alone is not a task.

    This is the exact shape the conflict task used to have — a full
    description and nothing else the gate could see.  Such a row is
    NOT mergeable: the loader substitutes a placeholder and the merge
    never runs.  If this control ever came back ``True``, the positive
    test above would be proving nothing.
    """
    db_path = tmp_path / "state.db"
    task = _task(tmp_path)
    task.pop("title")
    _persist(task, db_path)

    row = next(iter(_orphans(db_path)))

    assert row["title"] is None
    assert row["description"], "the description must still be there"
    assert has_mergeable_content(row) is False


def test_row_without_description_is_not_mergeable(tmp_path):
    """Negative control: a title alone is not a task either.

    A row that names the merge but does not say what to do is just as
    unusable as one that says what to do but names nothing — and the
    gate has to refuse it, or the subagent is handed a heading and no
    instructions.
    """
    db_path = tmp_path / "state.db"
    task = _task(tmp_path)
    task.pop("description")
    _persist(task, db_path)

    row = next(iter(_orphans(db_path)))

    assert row["description"] is None
    assert row["title"], "the title must still be there"
    assert has_mergeable_content(row) is False


def test_task_id_is_recognized_as_a_repair_task(tmp_path):
    """The id the row is stored under is one the executor picks up.

    ``verification_loop._is_repair_task_id`` is what the repair channel
    asks before it counts a task towards "the repair batch drained".
    A row whose id the predicate does not recognise is a task the
    executor never runs: the round reports a drained batch while the
    conflict is still queued.

    The id has to survive the database round trip, so the assertion is
    made against the *persisted* row and not only against a literal.
    """
    assert verification_loop._is_repair_task_id("repair-r3-01", "repair") is True

    db_path = tmp_path / "state.db"
    _persist(_task(tmp_path), db_path)

    row = next(iter(_orphans(db_path)))

    assert verification_loop._is_repair_task_id(
        row["id"], row["task_group"]
    ) is True
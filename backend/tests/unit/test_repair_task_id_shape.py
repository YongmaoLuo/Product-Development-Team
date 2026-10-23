"""2026-10-23: the repair-task id predicate must know the REAL id shape.

``RepairTaskAssembler`` produces LOWERCASE ids of the shape
``repair-r<round>-<seq>`` (``repair-r1-01``) and its breakdown children
(``repair-r1-06-1``, ``repair-r3-03-1-1-2``). Those rows carry NO
``task_group`` on the breakdown children.

The predicate in ``verification_loop._get_pending_repair_tasks`` matched
neither branch for them:

    if tid.startswith("RP-") or tid.startswith("R") and "-" in tid:

``"repair-r3-01-1"`` starts with a lowercase ``r`` — so neither
``"RP-"`` nor ``"R"`` matched — and its ``task_group`` is NULL, so the
fallback did not match either. The consequence: the "has the repair
batch drained?" guard always saw an EMPTY list, so it could never
notice a queued repair task and never stopped the chain.

Two layers are pinned here:

  1. the predicate itself, against the real id shapes;
  2. the LIVE reader — ``_get_pending_repair_tasks`` — against a
     ``plan_tasks`` row that carries the real shape and no
     ``task_group``. The predicate alone would not prove that the
     guard sees the row.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import pytest

import server  # noqa: F401  — imported first: server wires verification_loop
import verification_loop
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.plan_task_repository import PlanTaskRepository

PLAN_ID = "20261023-repair-task-id"


# ---------------------------------------------------------------------------
# 1) the predicate — the real shapes
# ---------------------------------------------------------------------------


def test_the_real_repair_id_shape_is_recognised():
    """``repair-r3-01-1`` is a repair task even without a task_group.

    This is the id the assembler actually writes, and the breakdown
    children carry no ``task_group`` — so the id shape is the only
    thing that can identify them.
    """
    assert verification_loop._is_repair_task_id("repair-r3-01-1", None) is True


def test_a_verification_point_is_not_a_repair_task():
    """``VP-013`` is not a repair task — the predicate stays narrow."""
    assert verification_loop._is_repair_task_id("VP-013", None) is False


@pytest.mark.parametrize(
    "tid",
    [
        "repair-r1-01",       # the assembler's own shape
        "repair-r3-01-1",     # a breakdown child
        "repair-r3-03-1-1-2", # a third-level child
        "RP-12",              # the legacy pre-v9 shape
        "R6-1",               # the legacy post-v9 shape
    ],
)
def test_every_known_repair_id_shape_is_recognised(tid):
    """All the shapes the repair round has ever written are repair tasks."""
    assert verification_loop._is_repair_task_id(tid, None) is True, tid


@pytest.mark.parametrize(
    "tid",
    ["VP-013", "TASK-1", "1", "repairtask-1", "arepair-1"],
)
def test_a_non_repair_id_is_not_matched_by_the_prefix_rule(tid):
    """The predicate is narrow: a known repair PREFIX, not the word.

    ``repairtask-1`` and ``arepair-1`` contain the word "repair" but do
    not carry a repair prefix — they are not repair tasks, and
    widening the rule to "contains repair" would sweep ordinary task
    ids into the repair queue.
    """
    assert verification_loop._is_repair_task_id(tid, None) is False, tid


def test_the_legacy_uppercase_R_rule_is_unchanged():
    """The pre-existing ``R<round>-<i>`` rule keeps its old breadth.

    ``tid.startswith("R") and "-" in tid`` also matches ids like
    ``REPO-WORK-2``. That breadth is PRE-EXISTING (it is what the
    2026-09-12 change added so ``R6-1`` / ``R6-2`` would not be
    dropped from the executor queue) and this fix does not change it
    — the fix only ADDS the lowercase ``repair-`` shape. Narrowing it
    is a separate decision with its own blast radius.
    """
    assert verification_loop._is_repair_task_id("R6-1", None) is True
    assert verification_loop._is_repair_task_id("REPO-WORK-2", None) is True


def test_the_task_group_fallback_still_works():
    """A stamped ``task_group`` remains a valid repair marker."""
    assert verification_loop._is_repair_task_id("XYZ-1", "repair-round-3") is True
    assert verification_loop._is_repair_task_id("XYZ-1", "repair") is True
    assert verification_loop._is_repair_task_id("XYZ-1", "feature") is False
    assert verification_loop._is_repair_task_id("XYZ-1", None) is False


def test_a_non_string_id_is_not_a_repair_task():
    """The predicate never raises on a malformed id."""
    assert verification_loop._is_repair_task_id("", None) is False
    assert verification_loop._is_repair_task_id(None, None) is False
    assert verification_loop._is_repair_task_id(123, None) is False


# ---------------------------------------------------------------------------
# 2) the live reader — the guard must SEE the queued repair task
# ---------------------------------------------------------------------------


@pytest.fixture
def db_env(monkeypatch, tmp_path):
    """A hermetic state.db with the plan's ``plan_tasks`` rows."""
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()

    import server
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    yield db_path
    os.environ.pop("PDT_STATE_DB_PATH", None)


def _task(tid: str, **over: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": tid,
        "title": f"task {tid}",
        "description": "d",
        "test_command": "pytest -q",
        "files_to_modify": ["__NO_FILE_CHANGES__"],
        "depends_on": [],
        "model_type": "medium",
        "project_dir": None,
    }
    out.update(over)
    return out


def test_the_pending_repair_reader_sees_the_real_id_shape(db_env):
    """The LIVE guard sees a queued repair task written with the real id.

    ``_get_pending_repair_tasks`` is the reader behind the "the repair
    batch did not drain" guard. Before the fix it returned an EMPTY
    list for a plan whose only queued task was ``repair-r3-01-1`` —
    which is how the chain kept re-entering verification over a repair
    round that had never been executed.
    """
    db_path = db_env
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = PlanTaskRepository(conn)
        # The real shape, and NO task_group — exactly what the
        # breakdown children are written with.
        repo.add_task(PLAN_ID, _task("repair-r3-01-1"))
        repo.add_task(PLAN_ID, _task("VP-013"))
    finally:
        conn.close()

    pending = verification_loop._get_pending_repair_tasks(PLAN_ID)

    ids = [t["id"] for t in pending]
    assert "repair-r3-01-1" in ids, (
        f"the pending-repair reader must see a queued repair task with "
        f"the real id shape; it returned {ids!r}"
    )
    assert "VP-013" not in ids, (
        f"a verification point is not a repair task; got {ids!r}"
    )

"""2026-10-13 — a breakdown child inherits the repair parent's ``failed_vp_id``.

The gap this closes
-------------------
``9a2e8d4`` (the 2026-10-13 revival change) re-queues a ``failed`` repair
task while the VP it was written for is still failing this round. It
carved out one exception: *rows whose ``failed_vp_id`` is NULL are never
revived*, because there is no VP to test them against.

Every breakdown child was in that exception. ``failed_vp_id`` is stamped
by the orchestrator when it *generates* a repair task
(``repair_generator.RepairTaskAssembler``); the refiner *splits* one and
authors only content, so all 24 breakdown children in the live database
read NULL — including ``repair-r1-01-1/-2/-3`` in
a 2026-10-12 production plan, whose own titles and
descriptions name the VP they are working on ("原 repair-r1-01 的判定被宿主
进程状态左右…"). A child that failed was therefore stranded exactly the way
the parent used to be, one level down.

Where the work happens
----------------------
``refiner_structure.plan_refiner_structure`` is the split point: it drops
the protected parent (``effective_by_id.pop(tid)``) and keeps the children.
It decides; ``agent._apply_refiner_structure`` writes the decision to both
truth sources.

The column is read in ``agent.py`` — not here — because ``failed_vp_id``
is not on :class:`task.SubTask` and never was, so ``current_tasks_dict``
(``[t.model_dump() for t in task_manager.tasks]``) cannot carry it. See
``AutonomousAgent._repair_failed_vp_map``.

Every test below drives a production entry point: the real
``_apply_refiner_structure`` (with a real ``TaskManager``, a real migrated
``state.db`` and a real ``tasks.json``), then the real
``server._get_pending_repair_tasks`` — the executor's actual queue.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402
from task import SubTask  # noqa: E402
from task_manager import TaskManager  # noqa: E402

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)


PLAN_ID = "test-breakdown-inherits-failed-vp"
PARENT = "repair-r1-01"
VP = "VP-009"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _StubAgent:
    """Host for the real ``AutonomousAgent`` writer methods.

    ``_apply_refiner_structure`` only needs a ``TaskManager``, a
    repository, a ``plan_id`` and a logger — no executor, no coding tool,
    no LLM. The methods under test are the unbound production ones.
    """

    _TERMINAL_REPAIR_TASK_GROUP_PREFIX = (
        AutonomousAgent._TERMINAL_REPAIR_TASK_GROUP_PREFIX
    )
    _apply_refiner_structure = AutonomousAgent._apply_refiner_structure
    _is_refiner_protected = AutonomousAgent._is_refiner_protected
    _repair_failed_vp_map = AutonomousAgent._repair_failed_vp_map

    def __init__(self, task_manager, repo, logger=None, plan_id=PLAN_ID):
        self.task_manager = task_manager
        self._repo = repo
        self.plan_id = plan_id
        self.logger = logger

    def _get_task_progress_repository(self):
        return self._repo


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """A migrated throwaway ``state.db``, wired in as the live one."""
    db_path = tmp_path / "state.db"
    os.environ["PDT_STATE_DB_PATH"] = str(db_path)
    conn = open_db(db_path)
    migrate(conn)

    import server
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    try:
        yield {"db_path": db_path, "conn": conn, "tmp_path": tmp_path}
    finally:
        conn.close()
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


def _seed_failed_repair_parent(env, repo, *, task_id=PARENT, vp=VP,
                                round_n=1, group="repair-round-1"):
    """The pre-refinement world: one failed repair row + its disk twin."""
    repo.add_task(PLAN_ID, _task(
        task_id, task_group=group, failed_vp_id=vp, round=round_n,
    ))
    repo.update_task(PLAN_ID, task_id, {
        "status": "failed", "failure_reason": "empty_diff_no_changes",
    })
    disk = _task(task_id, task_group=group)
    tasks_file = env["tmp_path"] / "tasks.json"
    tasks_file.write_text(
        json.dumps({"tasks": [disk]}, ensure_ascii=False), encoding="utf-8",
    )
    tm = TaskManager(project_dir=env["tmp_path"], tasks_file=tasks_file)
    agent = _StubAgent(tm, repo)
    return agent, tm, tasks_file, disk


def _split(agent, tm, disk, children: List[Dict[str, Any]]):
    """Run the production writer: current list in, refiner list in."""
    current = [t.model_dump() for t in tm.tasks]
    return agent._apply_refiner_structure(
        current, children, SubTask(id=disk["id"], title=disk["title"],
                                   description=disk["description"]),
    )


def _column(env, task_id, column):
    conn = sqlite3.connect(f"file:{env['db_path']}?mode=ro", uri=True)
    try:
        row = conn.execute(
            f"SELECT {column} FROM plan_tasks "
            f"WHERE plan_id = ? AND task_id = ?", (PLAN_ID, task_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _row_exists(env, task_id) -> bool:
    conn = sqlite3.connect(f"file:{env['db_path']}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT 1 FROM plan_tasks WHERE plan_id = ? AND task_id = ?",
            (PLAN_ID, task_id),
        ).fetchone() is not None
    finally:
        conn.close()


def _disk(tasks_file: Path) -> List[Dict[str, Any]]:
    return json.loads(tasks_file.read_text(encoding="utf-8"))["tasks"]


def _children(*ids: str, group: str = "repair-round-1") -> List[Dict[str, Any]]:
    return [
        _task(cid, task_group=group, description=f"child {cid}")
        for cid in ids
    ]


# ---------------------------------------------------------------------------
# 1. the child takes over the parent's VP
# ---------------------------------------------------------------------------


def test_every_child_gets_the_parents_failed_vp(env):
    """Acceptance 1 — through ``_apply_refiner_structure``, both stores.

    ``plan_tasks`` is the store that owns ``failed_vp_id``; the parent row
    is gone (the split deletes it) so this is the only copy left.
    """
    repo = PlanTaskRepository(env["conn"])
    agent, tm, tasks_file, disk = _seed_failed_repair_parent(env, repo)

    assert _split(agent, tm, disk, _children("repair-r1-01-1", "repair-r1-01-2")) is True

    assert _column(env, "repair-r1-01-1", "failed_vp_id") == VP
    assert _column(env, "repair-r1-01-2", "failed_vp_id") == VP
    assert not _row_exists(env, PARENT), (
        "the split parent is still deleted — inheriting is not keeping it"
    )
    # The disk store must agree about the two children existing, even
    # though ``SubTask`` has no field for the VP itself.
    assert sorted(t["id"] for t in _disk(tasks_file)) == [
        "repair-r1-01-1", "repair-r1-01-2",
    ]


def test_inheritance_is_transitive(env):
    """``repair-r4-01`` → ``-5`` → ``-5-1..4`` in the live database.

    The second split reads the first split's write, because the fact now
    lives in ``plan_tasks``. Without this a third-level child is stranded
    even though its parent carries a VP.
    """
    repo = PlanTaskRepository(env["conn"])
    agent, tm, _tasks_file, disk = _seed_failed_repair_parent(
        env, repo, task_id="repair-r4-01", vp="VP-007", round_n=4,
        group="repair-round-4",
    )
    assert _split(agent, tm, disk, _children(
        "repair-r4-01-5", group="repair-round-4",
    )) is True
    assert _column(env, "repair-r4-01-5", "failed_vp_id") == "VP-007"

    # The next executor run reloads from disk + SQLite; the child is then
    # split again, this time with its own row carrying the VP.
    tm.load_tasks()
    disk5 = _task("repair-r4-01-5", task_group="repair-round-4")
    assert _split(agent, tm, disk5, _children(
        "repair-r4-01-5-1", "repair-r4-01-5-2", group="repair-round-4",
    )) is True
    assert _column(env, "repair-r4-01-5-1", "failed_vp_id") == "VP-007"
    assert _column(env, "repair-r4-01-5-2", "failed_vp_id") == "VP-007"


def test_parent_without_a_vp_leaves_the_children_alone(env):
    """The 24 NULL rows stay NULL — there is nothing to inherit."""
    repo = PlanTaskRepository(env["conn"])
    agent, tm, _tasks_file, disk = _seed_failed_repair_parent(
        env, repo, vp=None,
    )

    assert _split(agent, tm, disk, _children("repair-r1-01-1")) is True
    assert _column(env, "repair-r1-01-1", "failed_vp_id") is None


def test_a_refiner_supplied_vp_is_not_clobbered(env):
    repo = PlanTaskRepository(env["conn"])
    agent, tm, _tasks_file, disk = _seed_failed_repair_parent(env, repo)

    children = _children("repair-r1-01-1")
    children[0]["failed_vp_id"] = "VP-042"
    assert _split(agent, tm, disk, children) is True

    assert _column(env, "repair-r1-01-1", "failed_vp_id") == "VP-042"


# ---------------------------------------------------------------------------
# 2. round stays NULL — the hard constraint
# ---------------------------------------------------------------------------


def test_child_round_stays_null(env):
    """Acceptance 2.

    ``verification_loop._supersede_prior_round_repairs`` retires rows for
    the same VP whose round is *strictly earlier* than the current one::

        if row_round is None or int(row_round) >= int(round_number):
            continue          # do not retire

    Copy the parent's ``round`` (1) onto the child and the next round
    (round_number=2) retires it the moment it is revived — the inheritance
    would cancel itself.
    """
    repo = PlanTaskRepository(env["conn"])
    agent, tm, _tasks_file, disk = _seed_failed_repair_parent(
        env, repo, round_n=1,
    )
    assert _column(env, PARENT, "round") == 1, "precondition: parent is round 1"

    assert _split(agent, tm, disk, _children("repair-r1-01-1", "repair-r1-01-2")) is True

    assert _column(env, "repair-r1-01-1", "round") is None
    assert _column(env, "repair-r1-01-2", "round") is None


def test_a_null_round_child_is_not_retired_as_a_prior_round(env):
    """The consequence, on the production retire path itself."""
    from verification_loop import _supersede_prior_round_repairs

    repo = PlanTaskRepository(env["conn"])
    agent, tm, _tasks_file, disk = _seed_failed_repair_parent(env, repo)
    assert _split(agent, tm, disk, _children("repair-r1-01-1")) is True

    assert _supersede_prior_round_repairs(PLAN_ID, 2, [VP]) == []
    assert _column(env, "repair-r1-01-1", "status") == "pending", (
        "the row the round-N+1 repair for the same VP would have "
        "superseded is still live — that is what lets the revival stand"
    )


# ---------------------------------------------------------------------------
# 3. end to end: the failed child is re-queued
# ---------------------------------------------------------------------------


def test_failed_child_is_requeued_while_its_vp_still_fails(env):
    """Acceptance 3 — split, fail the child, run the real queue."""
    import server

    repo = PlanTaskRepository(env["conn"])
    agent, tm, _tasks_file, disk = _seed_failed_repair_parent(env, repo)
    assert _split(agent, tm, disk, _children("repair-r1-01-1", "repair-r1-01-2")) is True

    # The executor runs both children: one passes, one fails.
    repo.update_task(PLAN_ID, "repair-r1-01-2", {"status": "completed"})
    repo.update_task(PLAN_ID, "repair-r1-01-1", {
        "status": "failed", "failure_reason": "empty_diff_no_changes",
    })

    # Next round: VP-009 still fails, so the child is offered again.
    queued = server._get_pending_repair_tasks(PLAN_ID, {VP})
    assert [t["id"] for t in queued] == ["repair-r1-01-1"]
    assert queued[0]["failed_vp_id"] == VP
    assert _column(env, "repair-r1-01-1", "status") == "pending"
    assert _column(env, "repair-r1-01-1", "attempt") == 1

    # And the VP passing ends it again.
    repo.update_task(PLAN_ID, "repair-r1-01-1", {
        "status": "failed", "failure_reason": "empty_diff_no_changes",
    })
    assert server._get_pending_repair_tasks(PLAN_ID, {"VP-011"}) == []
    assert _column(env, "repair-r1-01-1", "status") == "failed"


def test_a_failed_child_is_not_revived_without_a_round_verdict(env):
    """The read-only probes pass ``failed_vp_ids=None``; stay inert."""
    import server

    repo = PlanTaskRepository(env["conn"])
    agent, tm, _tasks_file, disk = _seed_failed_repair_parent(env, repo)
    assert _split(agent, tm, disk, _children("repair-r1-01-1")) is True
    repo.update_task(PLAN_ID, "repair-r1-01-1", {"status": "failed"})

    assert server._get_pending_repair_tasks(PLAN_ID) == []
    assert _column(env, "repair-r1-01-1", "status") == "failed"


# ---------------------------------------------------------------------------
# 4. the ordinary, unprotected split does not change
# ---------------------------------------------------------------------------


def test_unprotected_split_is_unchanged(env):
    """Acceptance 4.

    The map is seeded with an entry for the unprotected parent on purpose:
    the gate that limits inheritance to repair tasks is ``is_protected``,
    not the absence of the id, and this pins which one it is.
    """
    repo = PlanTaskRepository(env["conn"])
    repo.add_task(PLAN_ID, _task("40"))
    repo.update_task(PLAN_ID, "40", {"status": "failed"})
    disk = _task("40")
    tasks_file = env["tmp_path"] / "tasks.json"
    tasks_file.write_text(
        json.dumps({"tasks": [disk]}, ensure_ascii=False), encoding="utf-8",
    )
    tm = TaskManager(project_dir=env["tmp_path"], tasks_file=tasks_file)
    agent = _StubAgent(tm, repo)
    agent.plan_id = PLAN_ID

    assert agent._repair_failed_vp_map() == {}, (
        "precondition: the map is built from repair-group rows only"
    )
    assert _split(agent, tm, disk, [_task("40-1"), _task("40-2")]) is True

    assert not _row_exists(env, "40")
    assert _column(env, "40-1", "failed_vp_id") is None
    assert _column(env, "40-2", "failed_vp_id") is None
    assert sorted(t["id"] for t in _disk(tasks_file)) == ["40-1", "40-2"]


def test_only_a_protected_parent_can_pass_a_vp_on():
    """The gate itself, pinned where it lives.

    ``_repair_failed_vp_map`` reads the repair task group out of
    ``plan_tasks``, so an unprotected row is never a candidate in
    practice — which is exactly why ``test_unprotected_split_is_unchanged``
    cannot go red when the gate is widened. The rule that survives is the
    one in :func:`refiner_structure.plan_refiner_structure`: protection,
    not the caller's bookkeeping, is what admits a parent to inheritance.
    """
    from refiner_structure import plan_refiner_structure

    unprotected = plan_refiner_structure(
        [_task("40")], [_task("40-1"), _task("40-2")], "repair",
        {"40": VP},
    )
    assert all(
        "failed_vp_id" not in t for t in unprotected.effective
    ), "an ordinary task has no VP; nothing about its split may change"

    protected = plan_refiner_structure(
        [_task(PARENT, task_group="repair-round-1")],
        [_task("repair-r1-01-1", task_group="repair-round-1")],
        "repair", {PARENT: VP},
    )
    assert protected.effective[0]["failed_vp_id"] == VP


# ---------------------------------------------------------------------------
# Reported side effects of the change (2026-10-13 review)
# ---------------------------------------------------------------------------


def test_load_repair_outcomes_reports_the_child_after_the_split(env):
    """``load_repair_outcomes`` groups by ``failed_vp_id``, newest round wins.

    ``refiner_structure`` deletes the split parent, so after this change
    the child's row is the *only* row carrying the VP — the prompt's
    "last round" block for that VP goes from absent to present. That is
    the answer to "does the repair prompt get worse": it gets the child's
    own outcome where it previously had nothing at all.
    """
    from verification.failure_history import load_repair_outcomes

    repo = PlanTaskRepository(env["conn"])
    agent, tm, _tasks_file, disk = _seed_failed_repair_parent(env, repo)
    assert _split(agent, tm, disk, _children("repair-r1-01-1")) is True
    repo.update_task(PLAN_ID, "repair-r1-01-1", {
        "status": "failed", "failure_reason": "child failed too",
    })

    outcomes = load_repair_outcomes(env["db_path"], PLAN_ID)
    assert set(outcomes) == {VP}
    assert outcomes[VP]["repair_task_id"] == "repair-r1-01-1"
    assert outcomes[VP]["repair_failure_reason"] == "child failed too"


def test_a_null_round_child_never_outranks_a_surviving_parent(env):
    """The legacy shape, where the parent row outlived its split.

    ``9a2e8d4``'s own survey found six such parents still sitting at
    ``failed`` next to their children (rows written before the 2026-10-05
    delete rule). ``load_repair_outcomes`` reads a NULL ``round`` as 0, so
    the parent's round-1 row keeps winning and the prompt keeps seeing
    the parent's history — the child does not displace it.
    """
    from verification.failure_history import load_repair_outcomes

    repo = PlanTaskRepository(env["conn"])
    agent, tm, _tasks_file, disk = _seed_failed_repair_parent(env, repo)
    assert _split(agent, tm, disk, _children("repair-r1-01-1")) is True
    repo.update_task(PLAN_ID, "repair-r1-01-1", {
        "status": "failed", "failure_reason": "child failed too",
    })
    # Re-seed the parent row: the shape the pre-2026-10-05 splitter left.
    repo.add_task(PLAN_ID, _task(PARENT, task_group="repair-round-1",
                                 failed_vp_id=VP, round=1))
    repo.update_task(PLAN_ID, PARENT, {
        "status": "failed", "failure_reason": "empty_diff_no_changes",
    })

    outcomes = load_repair_outcomes(env["db_path"], PLAN_ID)
    assert outcomes[VP]["repair_task_id"] == PARENT
    assert outcomes[VP]["repair_failure_reason"] == "empty_diff_no_changes"


def test_a_revived_child_and_a_new_repair_row_both_queue_for_one_vp(env):
    """KNOWN GAP (not fixed here) — the same VP can be fixed twice.

    ``_supersede_prior_round_repairs`` only retires rows from a strictly
    earlier round, and a breakdown child has ``round = NULL``, so the
    round-N repair task the orchestrator assembles for a still-failing VP
    does not retire the child. ``_get_pending_repair_tasks`` then returns
    both. This is pre-existing for any failed repair task since
    ``9a2e8d4``; the inheritance widens it to children. Pinned so the
    duplication is visible rather than silent.
    """
    import server

    repo = PlanTaskRepository(env["conn"])
    agent, tm, _tasks_file, disk = _seed_failed_repair_parent(env, repo)
    assert _split(agent, tm, disk, _children("repair-r1-01-1")) is True
    repo.update_task(PLAN_ID, "repair-r1-01-1", {"status": "failed"})

    # Round 2: the orchestrator assembles a fresh repair for VP-009.
    repo.add_task(PLAN_ID, _task(
        "repair-r2-01", task_group="repair-round-2",
        failed_vp_id=VP, round=2,
    ))

    queued = sorted(t["id"] for t in server._get_pending_repair_tasks(
        PLAN_ID, {VP},
    ))
    assert queued == ["repair-r1-01-1", "repair-r2-01"], (
        "both the revived child and the new row are queued for one VP — "
        "the same repair runs twice in one round"
    )

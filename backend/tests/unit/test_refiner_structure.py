"""The refiner's structural mutation must reach both truth sources the same way.

2026-09-16 (D1). ``agent._refine_after_failure`` used to derive the new
task list twice — once for ``tasks.json`` (the raw refiner output, via
``set_tasks``) and once for ``plan_tasks`` in ``state.db`` (a separately
computed id diff). The two disagreed in exactly the two places that
matter:

* **repair tasks** — protected on the database side only, so a refiner
  that dropped one deleted it from disk while the row stayed in SQLite
  and came back through orphan-reconcile on the next load;
* **split parents** — removed from disk by the whole-file rewrite and
  from SQLite by a separate delete, so any drift left the parent on disk
  with no row to hydrate a terminal status from: it reloaded as
  ``pending`` and the dispatcher re-executed it forever.

``refiner_structure.plan_refiner_structure`` now decides once.
``agent._apply_refiner_structure`` is the thin writer. These tests pin
the decision, the writer, and the two ``TaskManager`` helpers the writer
depends on.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import task_manager as task_manager_mod  # noqa: E402
from agent import AutonomousAgent  # noqa: E402
from refiner_structure import (  # noqa: E402
    is_protected,
    plan_refiner_structure,
)
from task import SubTask  # noqa: E402
from task_manager import TaskManager  # noqa: E402

from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    PlanTaskRepository,
)


PLAN_ID = "test-plan-refiner-structure"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    tid: str,
    *,
    title: str | None = None,
    description: str = "d",
    test_command: str = "pytest -q",
    task_group: str | None = None,
    extra: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": tid,
        "title": title or f"task {tid}",
        "description": description,
        "test_command": test_command,
        "files_to_modify": ["__NO_FILE_CHANGES__"],
        "depends_on": [],
        "model_type": "medium",
        "project_dir": None,
    }
    if task_group is not None:
        out["task_group"] = task_group
    if extra:
        out.update(extra)
    return out


class _LogRecorder:
    """Minimal stand-in for the agent's logger."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def _record(self, level):
        def _fn(event, message, **kwargs):
            self.events.append({
                "level": level,
                "event": event,
                "message": message,
                "data": kwargs.get("data"),
                "task_id": kwargs.get("task_id"),
            })
        return _fn

    def __getattr__(self, name):  # pragma: no cover - trivial dispatch
        if name in ("info", "warning", "error", "debug"):
            return self._record(name)
        raise AttributeError(name)

    def events_named(self, event: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]


class _StubAgent:
    """Host for ``AutonomousAgent._apply_refiner_structure``.

    The method only needs a ``TaskManager``, a repository, a ``plan_id``
    and a logger — no executor, no coding tool, no LLM.
    """

    _TERMINAL_REPAIR_TASK_GROUP_PREFIX = (
        AutonomousAgent._TERMINAL_REPAIR_TASK_GROUP_PREFIX
    )
    _apply_refiner_structure = AutonomousAgent._apply_refiner_structure
    _is_refiner_protected = AutonomousAgent._is_refiner_protected

    def __init__(self, task_manager, repo, logger=None, plan_id=PLAN_ID):
        self.task_manager = task_manager
        self._repo = repo
        self.plan_id = plan_id
        self.logger = logger

    def _get_task_progress_repository(self):
        return self._repo


class _BrokenRepo:
    """A repository whose writes always fail (for the rollback test)."""

    def delete_task(self, plan_id, task_id):
        raise sqlite3.OperationalError("disk I/O error")

    def add_task(self, plan_id, task_dict, expected_version=0):
        raise sqlite3.OperationalError("disk I/O error")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    os.environ["PDT_STATE_DB_PATH"] = str(tmp_path / "state.db")
    c = open_db(tmp_path / "state.db")
    migrate(c)
    try:
        yield c
    finally:
        c.close()
        os.environ.pop("PDT_STATE_DB_PATH", None)


def _write_tasks_file(tmp_path: Path, tasks: List[Dict[str, Any]]) -> Path:
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8",
    )
    return tasks_file


def _read_tasks_file(tasks_file: Path) -> List[Dict[str, Any]]:
    return json.loads(tasks_file.read_text(encoding="utf-8"))["tasks"]


def _harness(tmp_path: Path, disk_tasks, repo):
    tasks_file = _write_tasks_file(tmp_path, disk_tasks)
    tm = TaskManager(project_dir=tmp_path, tasks_file=tasks_file)
    return _StubAgent(tm, repo), tm, tasks_file


# ---------------------------------------------------------------------------
# The decision (pure)
# ---------------------------------------------------------------------------


def test_unchanged_list_is_a_noop():
    tasks = [_task("1"), _task("2")]
    plan = plan_refiner_structure(tasks, [dict(t) for t in tasks])
    assert plan.is_noop
    assert plan.effective == tasks
    assert plan.removed_ids == ()
    assert plan.added_ids == ()


def test_split_parent_is_removed_and_children_added():
    parent = _task("40")
    children = [_task("40-1"), _task("40-2"), _task("40-3")]
    plan = plan_refiner_structure([_task("1"), parent], [_task("1")] + children)

    assert plan.removed_ids == ("40",)
    assert plan.added_ids == ("40-1", "40-2", "40-3")
    assert {t["id"] for t in plan.effective} == {"1", "40-1", "40-2", "40-3"}


def test_dropped_repair_task_is_reinstated():
    repair = _task("repair-r3-01", task_group="repair-round-3")
    plan = plan_refiner_structure(
        [_task("1"), repair], [_task("1")],
    )

    assert plan.reinstated_ids == ("repair-r3-01",)
    assert plan.removed_ids == (), (
        "a dropped repair task must never be treated as removed — the "
        "orchestrator owns it and the refiner does not know it exists"
    )
    assert {t["id"] for t in plan.effective} == {"1", "repair-r3-01"}


def test_edited_repair_task_is_reverted():
    repair = _task("repair-r3-01", task_group="repair-round-3",
                   test_command="pytest tests/a.py")
    tampered = dict(repair, test_command="pytest tests/b.py")
    plan = plan_refiner_structure([repair], [tampered])

    assert plan.reverted_ids == ("repair-r3-01",)
    assert plan.reinstated_ids == ()
    kept = next(t for t in plan.effective if t["id"] == "repair-r3-01")
    assert kept["test_command"] == "pytest tests/a.py", (
        "the pre-refinement content must win for a protected task"
    )


def test_untouched_repair_task_is_not_reported():
    repair = _task("RP-1", task_group="repair-round-1")
    plan = plan_refiner_structure([repair], [dict(repair)])
    assert plan.reinstated_ids == ()
    assert plan.reverted_ids == ()


def test_runtime_fields_do_not_count_as_an_edit():
    """The LLM echoes stale ``status``/``failure_reason`` — not an edit."""
    repair = _task("RP-1", task_group="repair-round-1")
    echoed = dict(repair, status="completed", failure_reason="old", attempt=3)
    plan = plan_refiner_structure([repair], [echoed])
    assert plan.reverted_ids == ()
    assert plan.is_noop


def test_non_protected_dropped_task_is_removed_not_reinstated():
    plan = plan_refiner_structure([_task("1"), _task("2")], [_task("1")])
    assert plan.removed_ids == ("2",)
    assert plan.reinstated_ids == ()


def test_duplicate_ids_in_the_refiner_output_are_collapsed():
    """Two entries with the same id would be written to both stores twice."""
    first = _task("40-1", title="first")
    second = _task("40-1", title="second")
    plan = plan_refiner_structure([_task("40")], [first, second])

    ids = [t["id"] for t in plan.effective]
    assert ids == ["40-1"]
    assert plan.effective[0]["title"] == "first", "first occurrence wins"


def test_refiner_order_is_preserved_and_reinstated_appended():
    repair = _task("RP-1", task_group="repair-round-1")
    plan = plan_refiner_structure(
        [_task("1"), repair], [_task("2"), _task("1")],
    )
    assert [t["id"] for t in plan.effective] == ["2", "1", "RP-1"]


def test_is_protected_matches_both_repair_id_schemas():
    assert is_protected({"task_group": "repair-round-3"})
    assert is_protected({"task_group": "repair"})
    assert not is_protected({"task_group": "original"})
    assert not is_protected({"task_group": None})
    assert not is_protected({})
    assert not is_protected(None)


def test_a_custom_prefix_is_honoured():
    """The prefix is a parameter so the rule can be pinned independently."""
    plan = plan_refiner_structure(
        [_task("x-1", task_group="orchestrator")],
        [],
        protected_prefix="orchestrator",
    )
    assert plan.reinstated_ids == ("x-1",)


# ---------------------------------------------------------------------------
# ``task_group`` has to survive the whole passthrough chain
# ---------------------------------------------------------------------------
#
# The refiner decides protection from
# ``[t.model_dump() for t in task_manager.tasks]``. ``SubTask`` overrides
# ``model_dump`` with an opt-in field list and ``model_config`` is
# ``extra="ignore"``, so a pydantic field alone changes nothing — every
# hop has to carry the key or the guard silently stops matching again.
# It did: ``task_group`` was on no hop, the ``startswith("repair")``
# guard never matched, and the refiner deleted every ``repair-*`` row it
# did not echo back.


def test_task_group_survives_the_subtask_dump():
    t = SubTask(
        id="RP-1", title="t", description="d", task_group="repair-round-1",
    )
    assert t.model_dump()["task_group"] == "repair-round-1"
    assert is_protected(t.model_dump()), (
        "the refiner reads protection off model_dump() output"
    )


def test_task_group_stays_out_of_the_dump_when_unset():
    """Round-trip identity for tasks that never carried the field."""
    t = SubTask(id="1", title="t", description="d")
    assert "task_group" not in t.model_dump()


def test_task_group_survives_a_task_manager_load(tmp_path: Path):
    tasks_file = _write_tasks_file(
        tmp_path, [_task("RP-1", task_group="repair-round-1")],
    )
    tm = TaskManager(project_dir=tmp_path, tasks_file=tasks_file)
    dumped = tm.tasks[0].model_dump()
    assert dumped.get("task_group") == "repair-round-1"
    assert is_protected(dumped)


def test_task_group_survives_an_orphan_merge():
    """A DB-only orphan keeps its group, so it stays undeletable."""
    from orphan_rules import merged_subtask_kwargs

    kwargs = merged_subtask_kwargs({
        "id": "RP-9", "title": "t", "description": "d",
        "task_group": "repair-round-9", "verification_only": True,
    })
    assert kwargs["task_group"] == "repair-round-9"
    assert kwargs["verification_only"] is True
    assert is_protected(SubTask(**kwargs).model_dump())


def test_verification_only_survives_an_orphan_merge_and_the_dump():
    """The declared half of the audit-task exemption."""
    from orphan_rules import merged_subtask_kwargs

    kwargs = merged_subtask_kwargs({
        "id": "a-1", "title": "t", "description": "d",
        "verification_only": True,
    })
    t = SubTask(**kwargs)
    assert t.verification_only is True
    assert t.model_dump().get("verification_only") is True


# ---------------------------------------------------------------------------
# TaskManager helpers the writer relies on
# ---------------------------------------------------------------------------


def test_set_tasks_preserves_the_db_orphan_origin_tag(tmp_path: Path):
    """A placeholder echoed back by the refiner must stay off disk.

    ``model_dump()`` drops ``_origin`` (it is not a model field), so the
    tag has to be re-sourced from the live object. Without this, the
    placeholder's ``"[db-orphan:...]"`` title would be written to
    ``tasks.json`` and read back as an authored task.
    """
    tasks_file = _write_tasks_file(tmp_path, [_task("disk-1")])
    tm = TaskManager(project_dir=tmp_path, tasks_file=tasks_file)

    placeholder = SubTask(
        id="ghost-1",
        title="[db-orphan:ghost-1]",
        description="recovered from plan_tasks DB (no static fields on disk)",
        test_command="",
    )
    placeholder._origin = "db_orphan"  # type: ignore[attr-defined]
    tm.tasks.append(placeholder)

    # What the refiner path feeds in: model_dump() dicts, no ``_origin``.
    payload = [t.model_dump() for t in tm.tasks]
    assert all("_origin" not in p for p in payload)

    tm.set_tasks(payload)

    written = _read_tasks_file(tasks_file)
    assert [t["id"] for t in written] == ["disk-1"], (
        "the db_orphan placeholder leaked into tasks.json"
    )


def test_save_tasks_does_not_truncate_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """``tasks.json`` is the dispatcher's startup input — no partial writes."""
    tasks_file = _write_tasks_file(tmp_path, [_task("disk-1")])
    original = tasks_file.read_text(encoding="utf-8")
    tm = TaskManager(project_dir=tmp_path, tasks_file=tasks_file)

    def _boom(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(task_manager_mod.json, "dump", _boom)
    with pytest.raises(OSError):
        tm.save_tasks()

    assert tasks_file.read_text(encoding="utf-8") == original, (
        "save_tasks truncated tasks.json before the write failed"
    )


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def test_apply_removes_the_parent_from_both_stores(tmp_path, conn):
    repo = PlanTaskRepository(conn)
    for t in (_task("1"), _task("40")):
        repo.add_task(PLAN_ID, t)

    agent, tm, tasks_file = _harness(tmp_path, [_task("1"), _task("40")], repo)
    current = [t.model_dump() for t in tm.tasks]

    agent._apply_refiner_structure(
        current, [_task("1"), _task("40-1"), _task("40-2")], SubTask(id="40", title="t", description="d"),
    )

    disk_ids = [t["id"] for t in _read_tasks_file(tasks_file)]
    assert "40" not in disk_ids, "split parent still on disk"
    assert {"1", "40-1", "40-2"} <= set(disk_ids)

    db_ids = set(repo.load_all(PLAN_ID))
    assert "40" not in db_ids, "split parent still in state.db"
    assert {"40-1", "40-2"} <= db_ids


def test_apply_reinstates_a_dropped_repair_task_in_both_stores(tmp_path, conn):
    repo = PlanTaskRepository(conn)
    repair = _task("repair-r3-01", task_group="repair-round-3")
    for t in (_task("1"), repair):
        repo.add_task(PLAN_ID, t)

    agent, tm, tasks_file = _harness(
        tmp_path, [_task("1"), repair], repo,
    )
    current = [t.model_dump() for t in tm.tasks]

    agent._apply_refiner_structure(
        current, [_task("1")], SubTask(id="1", title="t", description="d"),
    )

    disk_ids = [t["id"] for t in _read_tasks_file(tasks_file)]
    assert "repair-r3-01" in disk_ids, (
        "the refiner dropped a repair task and it did not come back"
    )
    assert "repair-r3-01" in set(repo.load_all(PLAN_ID))


def test_apply_logs_the_structure_delta(tmp_path, conn):
    repo = PlanTaskRepository(conn)
    repair = _task("repair-r3-01", task_group="repair-round-3")
    for t in (_task("1"), _task("40"), repair):
        repo.add_task(PLAN_ID, t)

    agent, tm, _ = _harness(tmp_path, [_task("1"), _task("40"), repair], repo)
    agent.logger = _LogRecorder()
    current = [t.model_dump() for t in tm.tasks]

    agent._apply_refiner_structure(
        current, [_task("1"), _task("40-1")], SubTask(id="40", title="t", description="d"),
    )

    applied = agent.logger.events_named("refine_structure_applied")
    assert len(applied) == 1
    data = applied[0]["data"]
    assert data["added_ids"] == ["40-1"]
    assert data["removed_ids"] == ["40"]
    assert data["reinstated_ids"] == ["repair-r3-01"]

    touched = agent.logger.events_named("refine_touched_protected_tasks")
    assert len(touched) == 1
    assert touched[0]["data"]["reinstated_ids"] == ["repair-r3-01"]


def test_apply_is_a_noop_when_nothing_changed(tmp_path, conn):
    repo = PlanTaskRepository(conn)
    repo.add_task(PLAN_ID, _task("1"))
    agent, tm, tasks_file = _harness(tmp_path, [_task("1")], repo)
    agent.logger = _LogRecorder()
    before = tasks_file.read_text(encoding="utf-8")

    current = [t.model_dump() for t in tm.tasks]
    agent._apply_refiner_structure(
        current, [dict(t) for t in current], SubTask(id="1", title="t", description="d"),
    )

    assert tasks_file.read_text(encoding="utf-8") == before
    assert agent.logger.events == []


def test_apply_rolls_the_disk_back_when_the_db_write_fails(tmp_path, conn):
    """Disk-first ordering + rollback: no drifted state left behind.

    Both orders leave a window (a file and a SQLite table cannot commit
    together). Disk-first is chosen because the alternative — a removed
    parent present on disk with its row already gone — reloads as
    ``pending`` and is the dead-lock this whole change is about. If the
    database write fails outright we undo the disk side rather than sit
    out of step.
    """
    repair = _task("RP-1", task_group="repair-round-1")
    agent, tm, tasks_file = _harness(
        tmp_path, [_task("1"), _task("40"), repair], _BrokenRepo(),
    )
    agent.logger = _LogRecorder()
    current = [t.model_dump() for t in tm.tasks]

    agent._apply_refiner_structure(
        current, [_task("1"), _task("40-1")], SubTask(id="40", title="t", description="d"),
    )

    disk_ids = [t["id"] for t in _read_tasks_file(tasks_file)]
    assert sorted(disk_ids) == sorted(["1", "40", "RP-1"]), (
        "the disk write was not rolled back after the state.db failure"
    )
    assert agent.logger.events_named("refine_structure_rolled_back")


def test_apply_does_not_reset_a_pre_existing_tasks_runtime_state(tmp_path, conn):
    """``add_task`` sets ``status='pending'`` — never re-create a live task."""
    repo = PlanTaskRepository(conn)
    repo.add_task(PLAN_ID, _task("1"))
    repo.update_task(
        plan_id=PLAN_ID, task_id="1",
        fields={"status": "completed"}, expected_version=0,
    )
    agent, tm, _ = _harness(tmp_path, [_task("1")], repo)
    current = [t.model_dump() for t in tm.tasks]

    agent._apply_refiner_structure(
        current, [_task("1"), _task("2")], SubTask(id="1", title="t", description="d"),
    )

    assert repo.load_all(PLAN_ID)["1"]["status"] == "completed", (
        "a pre-existing task was re-created and lost its runtime status"
    )
    assert repo.load_all(PLAN_ID)["2"]["status"] == "pending"


# ---------------------------------------------------------------------------
# Untrusted input: a refinement that cannot be constructed is rejected
# ---------------------------------------------------------------------------


def test_unconstructable_entries_flags_an_absolute_files_to_modify():
    """The exact failure that strands a plan.

    A repair task comes back from the refiner with
    ``files_to_modify=['/Users/.../plans/<id>/verification_plan.json']``.
    ``SubTask`` requires relative paths, so ``set_tasks`` raised
    ``ValidationError``, the exception escaped ``_refine_after_failure``,
    and the run ended as ``task_error`` + ``execution_stopped | No
    schedulable micro-layer found``.
    """
    from refiner_structure import unconstructable_entries

    problems = unconstructable_entries([
        _task("fine"),
        dict(_task("bad"), files_to_modify=["/abs/verification_plan.json"]),
    ])
    assert [tid for tid, _ in problems] == ["bad"]
    assert "must be relative" in problems[0][1]


def test_unconstructable_entries_accepts_a_clean_list():
    from refiner_structure import unconstructable_entries

    assert unconstructable_entries([_task("1"), _task("2")]) == []


def test_unconstructable_entries_flags_a_missing_id_and_non_dicts():
    from refiner_structure import unconstructable_entries

    problems = unconstructable_entries([
        {"title": "t", "description": "d"},
        "not a mapping",
    ])
    assert [tid for tid, _ in problems] == ["<missing id>", "<non-dict>"]


def test_apply_rejects_an_unconstructable_refinement(
    tmp_path, conn,
):
    """Rejected *before* either store is touched — the old list survives."""
    repo = PlanTaskRepository(conn)
    for t in (_task("1"), _task("40")):
        repo.add_task(PLAN_ID, t)

    agent, tm, tasks_file = _harness(tmp_path, [_task("1"), _task("40")], repo)
    agent.logger = _LogRecorder()
    before_disk = tasks_file.read_text(encoding="utf-8")
    current = [t.model_dump() for t in tm.tasks]

    applied = agent._apply_refiner_structure(
        current,
        [
            _task("1"),
            dict(_task("40-1"), files_to_modify=["/abs/plan/verification_plan.json"]),
        ],
        SubTask(id="40", title="t", description="d"),
    )

    assert applied is False
    assert tasks_file.read_text(encoding="utf-8") == before_disk, (
        "a rejected refinement must not rewrite tasks.json"
    )
    assert sorted(repo.load_all(PLAN_ID)) == ["1", "40"], (
        "a rejected refinement must not delete the parent row"
    )
    rejected = agent.logger.events_named("refine_rejected")
    assert len(rejected) == 1
    assert rejected[0]["data"]["unconstructable"][0]["task_id"] == "40-1"


def test_apply_returns_true_when_the_refinement_is_applied(tmp_path, conn):
    repo = PlanTaskRepository(conn)
    repo.add_task(PLAN_ID, _task("40"))
    agent, tm, _ = _harness(tmp_path, [_task("40")], repo)
    current = [t.model_dump() for t in tm.tasks]

    assert agent._apply_refiner_structure(
        current, [_task("40-1")], SubTask(id="40", title="t", description="d"),
    ) is True


# ---------------------------------------------------------------------------
# A protected task that was split must still be removed
# ---------------------------------------------------------------------------
#
# 2026-10-05. Protection covers a repair task's *content*: the refiner
# is an LLM and must not rewrite what a repair round decided. It was
# applied to a task's *existence* as well, and the two collided the
# moment a repair task failed and got split — the exact thing the
# refiner is called to do.
#
#   protected parent + refiner emits {parent}-1/-2/-3
#     -> parent reinstated unconditionally
#     -> removed_ids stays empty
#     -> agent._apply_refiner_structure never deletes the row
#     -> record_task_failure pins it at "failed"
#     -> the parent sits there forever beside children that all
#        completed, and the plan reports failed tasks of which several
#        have no unfinished work under them at all.
#
# Four repair parents were stranded this way in one plan
# (repair-r1-01, repair-r1-01-2, repair-r2-01, repair-r2-03).


def test_split_protected_parent_is_removed():
    parent = _task("repair-r2-03", task_group="repair-round-2")
    updated = [
        parent,
        _task("repair-r2-03-1", task_group="repair-round-2"),
        _task("repair-r2-03-2", task_group="repair-round-2"),
        _task("repair-r2-03-3", task_group="repair-round-2"),
    ]

    plan = plan_refiner_structure([parent], updated)

    assert plan.removed_ids == ("repair-r2-03",), (
        "a split parent must be removed even though it is protected"
    )
    assert plan.added_ids == (
        "repair-r2-03-1", "repair-r2-03-2", "repair-r2-03-3",
    )
    assert "repair-r2-03" not in [t["id"] for t in plan.effective]


def test_split_protected_parent_is_removed_even_when_the_refiner_keeps_it():
    """The refiner often returns the parent alongside its own children.

    Reinstatement keyed on "the refiner still mentioned it" would keep
    the parent alive; the split is the signal, not the mention.
    """
    parent = _task("repair-r1-01", task_group="repair-round-1")
    updated = [parent, _task("repair-r1-01-1", task_group="repair-round-1")]

    plan = plan_refiner_structure([parent], updated)

    assert plan.removed_ids == ("repair-r1-01",)
    assert plan.reinstated_ids == ()


def test_unprotected_split_parent_is_still_removed():
    """Regression: the ordinary, unprotected path must not change."""
    parent = _task("40")
    updated = [_task("40-1"), _task("40-2")]

    plan = plan_refiner_structure([parent], updated)

    assert plan.removed_ids == ("40",)
    assert plan.added_ids == ("40-1", "40-2")


def test_protected_task_dropped_without_being_split_is_still_reinstated():
    """Protection still holds when there is no split to justify removal."""
    protected = _task("repair-r1-01", task_group="repair-round-1")
    other = _task("9")

    plan = plan_refiner_structure([protected, other], [other])

    assert plan.reinstated_ids == ("repair-r1-01",)
    assert plan.removed_ids == ()


def test_protected_task_edited_without_being_split_is_still_reverted():
    """Content protection is untouched by the split exemption."""
    original = _task("repair-r1-01", title="原文", task_group="repair-round-1")
    edited = _task("repair-r1-01", title="被改写", task_group="repair-round-1")

    plan = plan_refiner_structure([original], [edited])

    assert plan.reverted_ids == ("repair-r1-01",)
    assert [t["title"] for t in plan.effective] == ["原文"]


def test_sibling_prefix_is_not_mistaken_for_a_split():
    """``repair-r2-03`` must not read ``repair-r2-030`` as a child.

    Ids are hierarchical, so a child carries a separator after the
    parent id; a bare ``startswith`` on the id would let an unrelated
    task suppress a protected parent. Here the refiner drops the parent
    while keeping the longer id, so the split exemption must not
    fire and the parent must be reinstated as usual.
    """
    parent = _task("repair-r2-03", task_group="repair-round-2")
    unrelated = _task("repair-r2-030", task_group="repair-round-2")

    plan = plan_refiner_structure([parent, unrelated], [unrelated])

    assert plan.removed_ids == ()
    assert plan.reinstated_ids == ("repair-r2-03",)
    assert "repair-r2-03" in [t["id"] for t in plan.effective]



# ---------------------------------------------------------------------------
# The judging command is write-once, not never-written (2026-10-11).
#
# Someone has to author a test_command the first time — the generator, or
# the refiner when it splits a failed task into children. A child is a
# *different task* with a smaller scope, so reusing the parent's command
# would grade it against work it was never asked to do.
#
# What must not happen is the SECOND write. The
# 20261010-CC-Switch-Remote-Aut plan grew 31 → 55 tasks over eleven
# refinements, every one logged as ``+3 added, -1 removed``, and 25
# commands ended up carrying a ``PATH="$HOME/.cargo/bin:$PATH"`` prefix
# the refiner invented to "fix" an environment defect it could not see.
# Nothing stopped it: the protection above only covers
# ``task_group.startswith("repair")`` and every task in that plan had
# ``task_group = None``.
#
# The quality of a *first* write is a separate question, and it lives in
# the agent (``_enforce_new_subtask_test_command``) because judging it
# means running the command.
# ---------------------------------------------------------------------------


def test_non_protected_task_cannot_rewrite_its_own_judge():
    """The core rule: a task outside the repair group gets no exemption.

    The refiner may still refine everything that *describes* the work.
    Only the command that decides whether the work is done is frozen.
    """
    original = _task("13", title="\u539f\u9898", test_command="cargo test --lib remote::e2e::x")
    tampered = dict(
        original,
        title="\u66f4\u51c6\u786e\u7684\u9898",
        test_command='PATH="$HOME/.cargo/bin:$PATH" cargo test --lib remote::e2e::x',
    )

    plan = plan_refiner_structure([original], [tampered])

    assert plan.judge_rewritten_ids == ("13",)
    kept = next(t for t in plan.effective if t["id"] == "13")
    assert kept["test_command"] == "cargo test --lib remote::e2e::x", (
        "the pre-refinement command must win for every task, not just "
        "the repair group"
    )
    assert kept["title"] == "\u66f4\u51c6\u786e\u7684\u9898", (
        "a legitimate descriptive refinement must survive — only the "
        "judge fields are frozen"
    )
    assert plan.reverted_ids == (), (
        "reverted_ids means 'the LLM tried to edit a REPAIR task'; a "
        "judge rewrite is a different event and must not be folded in"
    )


def test_judge_rewrite_is_reported_even_when_nothing_else_changed():
    """It must not be swallowed by the ``is_noop`` short-circuit.

    If ``is_noop`` were true here the corrected list would never be
    written to either store, and the rewritten command would survive on
    disk — the exact outcome this rule exists to prevent.
    """
    original = _task("7", test_command="cargo test --lib remote::auth::secret")
    tampered = dict(original, test_command="cargo test --lib no_such_module")

    plan = plan_refiner_structure([original], [tampered])

    assert not plan.is_noop
    assert plan.added_ids == () and plan.removed_ids == ()
    assert next(
        t for t in plan.effective if t["id"] == "7"
    )["test_command"] == "cargo test --lib remote::auth::secret"


def test_switching_between_the_two_command_shapes_is_not_a_rewrite():
    """``test_commands: []`` and an absent ``test_commands`` mean the same
    thing; an LLM that echoes the other shape has not changed the judge."""
    original = _task("3", test_command="pytest -q")
    echoed = dict(original, test_commands=[])

    plan = plan_refiner_structure([original], [echoed])

    assert plan.judge_rewritten_ids == ()
    assert plan.is_noop


def test_a_split_child_authors_its_own_judge():
    """The half that must NOT be frozen.

    A split child is a different task with a smaller scope; its command
    is the refiner's to write, and must come through untouched.
    """
    parent = _task("1", test_command="cargo test --test gate")
    children = [
        _task("1-1", test_command="cargo test --lib remote::auth::secret"),
        _task("1-2", test_command="cargo test --test auth_token_gate"),
        _task("1-3"),  # the refiner chose to write no command at all
    ]

    plan = plan_refiner_structure([parent], children)

    assert plan.judge_rewritten_ids == ()
    assert plan.removed_ids == ("1",)
    by_id = {t["id"]: t for t in plan.effective}
    assert by_id["1-1"]["test_command"] == "cargo test --lib remote::auth::secret"
    assert by_id["1-2"]["test_command"] == "cargo test --test auth_token_gate"
    assert by_id["1-3"]["test_command"] == "pytest -q"  # _task default, untouched


def test_a_brand_new_task_authors_its_own_judge():
    """A gap the refiner spotted is a new task, not a rewrite."""
    plan = plan_refiner_structure(
        [_task("9")], [_task("9"), _task("99", test_command="pytest -k gap")]
    )

    assert plan.judge_rewritten_ids == ()
    assert plan.added_ids == ("99",)
    assert next(
        t for t in plan.effective if t["id"] == "99"
    )["test_command"] == "pytest -k gap"


def test_a_pre_existing_child_shaped_id_is_still_frozen():
    """``1-2`` may be an authored id rather than a child of ``1``.

    Whatever its name looks like, it existed before the refinement, so
    the write-once rule governs it — not the new-task exemption.
    """
    parent = _task("1", test_command="cargo test --test a")
    sibling = _task("1-2", test_command="cargo test --test b")

    plan = plan_refiner_structure(
        [parent, sibling],
        [
            _task("1", test_command="cargo test --test a"),
            _task("1-2", test_command="cargo test --test CHANGED"),
        ],
    )

    assert plan.judge_rewritten_ids == ("1-2",)
    assert next(
        t for t in plan.effective if t["id"] == "1-2"
    )["test_command"] == "cargo test --test b"

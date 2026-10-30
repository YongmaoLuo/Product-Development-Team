"""The channel that puts a base-drift conflict task into ``state.db``.

``base_drift.build_conflict_repair_task`` produces a *dict* and stops
there — the persistence layer has exactly one owner.  This file pins
the other half: the write path that turns that dict into a row the
executor will actually pick up.

The properties that matter:

* the row goes in through the **repair channel's own entry point**
  (``PlanTaskRepository.add_task``) — the place where the field
  allow-list is enforced, where the upsert lives, and where the row's
  status is decided.  A channel that wrote the row itself would own a
  second copy of those rules, and the two copies would drift.
* the payload is **narrowed** to the table's static allow-list before
  the call: ``add_task`` *rejects* a payload carrying a field it does
  not know, it does not filter it out.
* the write is **idempotent** — the same task written twice is one row,
  not two.
* a write that fails **does not raise**: a task that cannot be stored
  must not take the whole verification round down with it.
* the builder stays **free of the database**: ``base_drift`` builds,
  this module stores.
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
    STATUS_CONFLICTS,
    BaseDriftReport,
    build_conflict_repair_task,
)
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.repositories.plan_task_repository import (  # noqa: E402
    ALLOWED_STATIC_TASK_FIELDS,
    PlanTaskRepository,
)

PLAN_ID = "p1"
PROJECT_DIR = Path("/tmp/wt")
BASE_SHA = "1a2b3c4d5e6f"


def _report():
    """A conflicting base-drift report."""
    return BaseDriftReport(
        status=STATUS_CONFLICTS,
        base_ref="origin/main",
        base_sha=BASE_SHA,
        behind=2,
        conflict_files=[{"path": "app.py", "conflict_blocks": 1}],
        mode="repair",
    )


def _task(**over):
    """Build one repair task from a conflicting report."""
    kwargs = {
        "plan_id": PLAN_ID,
        "project_dir": PROJECT_DIR,
        "round_number": 3,
        "seq": 1,
    }
    kwargs.update(over)
    return build_conflict_repair_task(_report(), **kwargs)


def _rows(db_path, plan_id=PLAN_ID):
    """Every task row of ``plan_id``, read back through the repository."""
    conn = open_db(db_path)
    try:
        return PlanTaskRepository(conn).load_all(plan_id)
    finally:
        conn.close()


def test_persist_uses_repository_add_task(monkeypatch, tmp_path):
    """The row goes in through ``add_task`` — the repair channel's door.

    The repository instance is built the way the existing repair path
    builds it (same connection, same migration, same repository class),
    and the task dict is handed over as-is except for the narrowing to
    the table's static allow-list.  ``add_task`` is where the field
    validation, the upsert and the initial ``pending`` status live; a
    second writer would have to re-implement all three.
    """
    calls = []

    def fake_add_task(self, plan_id, task_dict, expected_version=0):
        calls.append((plan_id, task_dict))

    monkeypatch.setattr(PlanTaskRepository, "add_task", fake_add_task)

    task = _task()
    ok = verification_agent.persist_conflict_repair_task(
        PLAN_ID, task, db_path=tmp_path / "state.db"
    )

    assert ok is True
    assert len(calls) == 1, calls
    written_plan_id, written_task = calls[0]
    assert written_plan_id == PLAN_ID
    assert written_task["id"] == task["id"] == "repair-r3-01"
    # The payload is narrowed to the table's static allow-list — no
    # ``status`` (that column belongs to ``update_task``'s runtime
    # mirror), no stray key the table would reject.
    assert "status" not in written_task
    assert set(written_task).issubset(ALLOWED_STATIC_TASK_FIELDS)


def test_persist_is_idempotent(tmp_path):
    """The same task written twice is ONE row, not two.

    ``add_task`` upserts on ``(plan_id, task_id)``.  A second write of
    the same task therefore refreshes the static columns of the row
    that is already there — it must never append a duplicate, or the
    executor would run the same repair twice.
    """
    db_path = tmp_path / "state.db"
    task = _task()

    assert verification_agent.persist_conflict_repair_task(
        PLAN_ID, task, db_path=db_path
    ) is True
    assert verification_agent.persist_conflict_repair_task(
        PLAN_ID, task, db_path=db_path
    ) is True

    rows = _rows(db_path)
    assert list(rows) == ["repair-r3-01"], rows
    assert rows["repair-r3-01"]["task_group"] == "repair"
    assert rows["repair-r3-01"]["status"] == "pending"


def test_persist_returns_false_on_error(monkeypatch, tmp_path):
    """A write that blows up returns ``False`` — it does not raise.

    The verification round has more to do than this one row: a
    database that is momentarily unwritable must not take the round
    down.  The caller gets a boolean and decides what to do with it.
    """
    def boom(self, *args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(PlanTaskRepository, "add_task", boom)

    ok = verification_agent.persist_conflict_repair_task(
        PLAN_ID, _task(), db_path=tmp_path / "state.db"
    )

    assert ok is False


def test_base_drift_has_no_db_dependency():
    """The builder does not touch the database; this channel does.

    One owner per concern: ``base_drift`` turns a conflict report into
    a task dict, and the repair channel puts that dict into
    ``state.db``.  A builder that also opened a connection would be a
    second owner of the write, and its row would not go through the
    validation the repair channel applies.
    """
    source = (BACKEND_DIR / "base_drift.py").read_text(encoding="utf-8")

    for forbidden in ("PlanTaskRepository", "sqlite3", "connection.open"):
        assert forbidden not in source, forbidden


def test_row_is_visible_to_pending_repair_query(tmp_path):
    """The stored row is one the executor's repair query recognises.

    A row the executor cannot see is a row that never runs: the round
    reports "the repair batch drained" while the repair is still
    queued.  ``verification_loop._is_repair_task_id`` is the predicate
    that decides visibility — the id shape ``repair-r3-01`` and the
    ``repair`` group are what it matches on.
    """
    db_path = tmp_path / "state.db"
    task = _task()

    assert verification_agent.persist_conflict_repair_task(
        PLAN_ID, task, db_path=db_path
    ) is True

    rows = _rows(db_path)
    assert list(rows) == ["repair-r3-01"], rows
    row = rows["repair-r3-01"]
    assert verification_loop._is_repair_task_id(
        "repair-r3-01", row.get("task_group")
    ) is True
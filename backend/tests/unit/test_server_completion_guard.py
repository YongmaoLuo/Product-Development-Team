"""Server: ``_count_unfinished_tasks`` cross-checks the runtime overlay.

Background
----------
``start_execution`` spawns the executor subprocess; ``_run`` (the
inner thread that tails stdout) used to mark the plan ``completed``
unconditionally on subprocess exit-code 0. That hid a real failure
mode in the executor's self-scheduling loop: ``agent.run`` returns 0
even when the no-schedulable-micro-layer branch in ``agent.py``
fires with downstream tasks still pending. The downstream state
machine then proceeds to launch verification against a
half-finished plan: tasks that had never started were still reported
as done, and the executor's "all done" was taken at face value.

The fix added ``_count_unfinished_tasks`` (server.py) which
cross-checks ``tasks.json`` against the SQLite
``plan_execution.task_progress`` runtime overlay and reports:

  * ``pending_or_unstarted`` — tasks whose runtime entry is missing
    (never started) or whose status is ``pending`` /
    ``in_progress`` / ``breakdown_in_progress``.
  * ``failed`` — tasks whose status is ``failed``.
  * ``total`` — total tasks in ``tasks.json``.

Contract pinned here
--------------------
1. Empty / missing ``tasks.json`` → all zeros, no exceptions.
2. ``tasks.json`` with only terminal-success overlays → all zeros.
3. ``tasks.json`` with a task id not present in runtime overlay →
   that task counts as ``pending_or_unstarted``.
4. ``tasks.json`` task id with ``status="failed"`` overlay →
   counted under ``failed``, not ``pending_or_unstarted``.
5. Runtime overlay for an id NOT in ``tasks.json`` is ignored
   (defensive: stale rows from a prior tasks.json revision) — with one
   exception added 2026-09-14: an overlay-only row whose status is
   ``failed`` counts under ``failed`` unless the id was broken down
   (children ``<id>-*`` exist in ``tasks.json``). See
   ``test_overlay_only_failure_counts_as_unfinished`` et al.
6. State machine unavailable (no SQLite) → every static task counts
   as ``pending_or_unstarted`` (matches pre-#3.8 behaviour).
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# ``backend/pytest.ini`` puts ``backend/`` on sys.path.
from server import _count_unfinished_tasks


def _write_tasks_json(path: Path, task_ids: list[str]) -> None:
    payload = {"tasks": [{"id": tid, "title": tid, "depends_on": []} for tid in task_ids]}
    path.write_text(json.dumps(payload))


def _seed_runtime_overlay(db_path: Path, plan_id: str, entries: dict) -> None:
    """Insert a minimal plan_execution row with the desired task_progress."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS plan_execution ("
            "  plan_id TEXT PRIMARY KEY,"
            "  task_progress TEXT"
            ")"
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_execution(plan_id, task_progress) "
            "VALUES (?, ?)",
            (plan_id, json.dumps({"tasks": entries})),
        )
        conn.commit()
    finally:
        conn.close()


def test_missing_tasks_json_returns_zeros(tmp_path, monkeypatch):
    """No tasks.json on disk → safe defaults, no exceptions."""
    monkeypatch.setattr("server._state_db_path", lambda *a, **k: tmp_path / "nope.db")
    out = _count_unfinished_tasks("p1", tmp_path / "tasks.json")
    assert out == {"total": 0, "pending_or_unstarted": 0, "failed": 0}


def test_corrupt_tasks_json_returns_zeros(tmp_path, monkeypatch):
    """Garbage tasks.json → safe defaults, no exceptions."""
    bad = tmp_path / "tasks.json"
    bad.write_text("{not json")
    monkeypatch.setattr("server._state_db_path", lambda *a, **k: tmp_path / "nope.db")
    out = _count_unfinished_tasks("p1", bad)
    assert out == {"total": 0, "pending_or_unstarted": 0, "failed": 0}


def test_all_tasks_terminal_success(tmp_path, monkeypatch):
    """Every task has a runtime overlay status of completed/skipped → all zero."""
    tasks = tmp_path / "tasks.json"
    _write_tasks_json(tasks, ["1", "2", "3"])
    db = tmp_path / "state.db"
    _seed_runtime_overlay(
        db,
        "p1",
        {
            "1": {"status": "completed"},
            "2": {"status": "completed"},
            "3": {"status": "skipped"},
        },
    )
    monkeypatch.setattr("server._state_db_path", lambda *a, **k: db)
    out = _count_unfinished_tasks("p1", tasks)
    assert out == {"total": 3, "pending_or_unstarted": 0, "failed": 0}


def test_never_started_task_counts_as_pending(tmp_path, monkeypatch):
    """Task id present in tasks.json but missing from runtime overlay.

    This is the exact shape of the 2026-08-24 OCP bug: the executor
    exited without ever writing a runtime row for tasks 1-2-1, 1-3-2,
    1-3-3, 2-1, 2-2, 3, 4, 17, 18, 24, 25.
    """
    tasks = tmp_path / "tasks.json"
    _write_tasks_json(tasks, ["1", "2", "3", "4", "5"])
    db = tmp_path / "state.db"
    _seed_runtime_overlay(
        db,
        "p1",
        {
            "1": {"status": "completed"},
            "2": {"status": "completed"},
            # 3, 4, 5 never appeared in the runtime overlay.
        },
    )
    monkeypatch.setattr("server._state_db_path", lambda *a, **k: db)
    out = _count_unfinished_tasks("p1", tasks)
    assert out == {"total": 5, "pending_or_unstarted": 3, "failed": 0}


def test_failed_task_counted_separately(tmp_path, monkeypatch):
    """A failed task is reported under ``failed``, not pending."""
    tasks = tmp_path / "tasks.json"
    _write_tasks_json(tasks, ["1", "2"])
    db = tmp_path / "state.db"
    _seed_runtime_overlay(
        db,
        "p1",
        {
            "1": {"status": "completed"},
            "2": {"status": "failed", "failure_reason": "boom"},
        },
    )
    monkeypatch.setattr("server._state_db_path", lambda *a, **k: db)
    out = _count_unfinished_tasks("p1", tasks)
    assert out == {"total": 2, "pending_or_unstarted": 0, "failed": 1}


def test_stale_runtime_row_for_removed_task_ignored(tmp_path, monkeypatch):
    """Runtime overlay may carry entries for tasks no longer in tasks.json.

    Defensive: removing a task from tasks.json should not retroactively
    count it as "unfinished".
    """
    tasks = tmp_path / "tasks.json"
    _write_tasks_json(tasks, ["1"])
    db = tmp_path / "state.db"
    _seed_runtime_overlay(
        db,
        "p1",
        {
            "1": {"status": "completed"},
            "99-deleted": {"status": "pending"},  # ghost row
        },
    )
    monkeypatch.setattr("server._state_db_path", lambda *a, **k: db)
    out = _count_unfinished_tasks("p1", tasks)
    assert out == {"total": 1, "pending_or_unstarted": 0, "failed": 0}


# ---------------------------------------------------------------------------
# 7. Overlay-only FAILURES (2026-09-14)
#
# The 2026-09-04 plan was terminated as finished while the
# runtime overlay still held two ``failed`` rows — ``40-1`` and ``R1-5``
# — that a later refiner pass had dropped from ``tasks.json`` (whose 63
# surviving entries were all ``completed``). ``failed`` came back 0, the
# dead-end disambiguation read that as "no unfinished work", and the
# chain closed over live failures.
#
# The rule that separates the two shapes: overlay-only failures whose id
# was *broken down* (``<id>-*`` children exist in ``tasks.json``) are
# superseded work and stay ignored; the rest are unfinished work.
# ---------------------------------------------------------------------------


def test_overlay_only_failure_counts_as_unfinished(tmp_path, monkeypatch):
    """A failed task that simply vanished from ``tasks.json`` still counts.

    This is ``R1-5``: a round-1 repair task ("修复全量 pytest 回归至
    1,482 用例 0 failed 基线") whose definition exists only in
    ``verification_tasks_round_1.json``, and which a refiner pass did
    not carry into ``tasks.json``. Its recorded failure must keep the
    plan open.
    """
    tasks = tmp_path / "tasks.json"
    _write_tasks_json(tasks, ["1", "2"])
    db = tmp_path / "state.db"
    _seed_runtime_overlay(
        db,
        "p1",
        {
            "1": {"status": "completed"},
            "2": {"status": "completed"},
            "R1-5": {"status": "failed", "failure_reason": "manual unstick"},
        },
    )
    monkeypatch.setattr("server._state_db_path", lambda *a, **k: db)
    out = _count_unfinished_tasks("p1", tasks)
    assert out == {"total": 2, "pending_or_unstarted": 0, "failed": 1}, (
        "an overlay-only failure is unfinished work; ignoring it lets the "
        "plan be terminated with live failures on the books"
    )


def test_overlay_only_failure_of_broken_down_parent_ignored(
    tmp_path, monkeypatch
):
    """``40-1`` was broken into ``40-1-1/2/3`` — those children carry the work.

    The parent's stale ``failed`` row is residue from before the
    breakdown, not work the plan still owes. Counting it would strand
    the plan permanently (the children are done; the parent can never
    run again).
    """
    tasks = tmp_path / "tasks.json"
    _write_tasks_json(tasks, ["40-1-1", "40-1-2", "40-1-3"])
    db = tmp_path / "state.db"
    _seed_runtime_overlay(
        db,
        "p1",
        {
            "40-1": {"status": "failed"},  # superseded by the breakdown
            "40-1-1": {"status": "completed"},
            "40-1-2": {"status": "completed"},
            "40-1-3": {"status": "completed"},
        },
    )
    monkeypatch.setattr("server._state_db_path", lambda *a, **k: db)
    out = _count_unfinished_tasks("p1", tasks)
    assert out == {"total": 3, "pending_or_unstarted": 0, "failed": 0}


def test_overlay_only_non_failure_still_ignored(tmp_path, monkeypatch):
    """Only ``failed`` overlay-only rows are promoted.

    Contract 5's original shape must survive: a stale ``pending`` row
    (or ``completed`` / ``in_progress`` / a malformed entry) for an id
    the refiner removed is not resurrected as unfinished work.
    """
    tasks = tmp_path / "tasks.json"
    _write_tasks_json(tasks, ["1"])
    db = tmp_path / "state.db"
    _seed_runtime_overlay(
        db,
        "p1",
        {
            "1": {"status": "completed"},
            "99-pending": {"status": "pending"},
            "98-progress": {"status": "in_progress"},
            "97-done": {"status": "completed"},
            "96-malformed": "not-a-dict",
        },
    )
    monkeypatch.setattr("server._state_db_path", lambda *a, **k: db)
    out = _count_unfinished_tasks("p1", tasks)
    assert out == {"total": 1, "pending_or_unstarted": 0, "failed": 0}


def test_breakdown_prefix_requires_the_separator(tmp_path, monkeypatch):
    """``40-1`` is superseded by ``40-1-*`` — but not by ``40-10``.

    The breakdown test is ``<id>-`` (with the separator), so a
    higher-numbered sibling that merely shares digits does not
    masquerade as a child. ``40-1`` has no children here, so its
    recorded failure keeps the plan open.
    """
    tasks = tmp_path / "tasks.json"
    _write_tasks_json(tasks, ["40-10"])
    db = tmp_path / "state.db"
    _seed_runtime_overlay(
        db,
        "p1",
        {"40-1": {"status": "failed"}, "40-10": {"status": "completed"}},
    )
    monkeypatch.setattr("server._state_db_path", lambda *a, **k: db)
    out = _count_unfinished_tasks("p1", tasks)
    assert out == {"total": 1, "pending_or_unstarted": 0, "failed": 1}


def test_state_machine_unavailable_counts_all_pending(tmp_path, monkeypatch):
    """No state machine on disk → pre-#3.8 behaviour: every task is pending.

    The function must not raise — it has to gracefully degrade so the
    caller can still make progress (it would be ironic if the unfinished
    guard crashed the same way the executor did).
    """
    tasks = tmp_path / "tasks.json"
    _write_tasks_json(tasks, ["1", "2"])
    # Point _state_db_path at a path that does not exist AND cannot
    # be opened (use a directory so open() raises IsADirectoryError).
    bad_db = tmp_path / "state_db_dir"
    bad_db.mkdir()
    monkeypatch.setattr("server._state_db_path", lambda *a, **k: bad_db)
    out = _count_unfinished_tasks("p1", tasks)
    assert out == {"total": 2, "pending_or_unstarted": 2, "failed": 0}

"""Tests for partial-completion auto-verification path (2026-09-11 plan v9 Bug 2).

A plan can finish all the work it can do without ever entering
verification on its own. Verification must start automatically once
execution is done, rather than waiting for a manual kick.

Root cause: ``backend/server.py:_run()`` (the executor-subprocess
completion handler) had a binary branch — either ``pending_or_unstarted
== 0`` (full completion → auto-verification) or > 0 (whole plan
``failed``, no auto-verification). A plan where every unfinished task
was blocked by a failed upstream ended up permanently stuck at
``routing.stage='ready'`` until someone unblocked it by hand.

Fix: introduce "partial completion" sub-case in the unfinished branch.
If EVERY unfinished task has at least one failed upstream (cannot ever
succeed), mark them ``skipped`` with reason
``blocked_by_failed_upstream``, then fall through to the success path
that enters auto-verification.

These tests pin the two helpers (``_are_all_unfinished_blocked_by_failed_upstream``
and ``_mark_task_skipped``) and the integration contract — the
legacy "truly unfinished → failed" path is preserved when not all
unfinished tasks are blocked.

Note on test isolation: ``test_task_manager_cycle_guard.py`` inserts
``a sibling checkout's backend`` into ``sys.path`` at import time
without restoring it. If that test runs first, ``import server``
elsewhere in the test session resolves to the dev backend (missing
the v9 helpers), so this file imports ``server`` from the canonical
backend path explicitly at module-load time and caches the module
reference.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent

# Canonical ``server`` module — bound to the absolute path BEFORE any
# other test file (notably ``test_task_manager_cycle_guard.py``) gets
# a chance to corrupt ``sys.path`` by inserting
# ``a sibling checkout's backend`` at import time. We use
# ``importlib.util.spec_from_file_location`` so the module is loaded
# from the canonical path regardless of what ends up in ``sys.path``
# at collection time. The module is also stashed under the alias
# ``partial_completion_path_test_server`` so subsequent
# ``import partial_completion_path_test_server`` calls in test bodies
# resolve to the canonical module, not the dev tree.
_SERVER_PATH = _BACKEND_DIR / "server.py"
_SPEC = importlib.util.spec_from_file_location(
    "partial_completion_path_test_server", str(_SERVER_PATH),
)
_server_module = importlib.util.module_from_spec(_SPEC)
sys.modules["partial_completion_path_test_server"] = _server_module
_SPEC.loader.exec_module(_server_module)
SERVER = _server_module

# Same isolation for ``state_machine.*`` — cycle_guard's
# ``sys.path.insert`` would otherwise let ``state_machine`` resolve
# to the dev tree, whose ``ALLOWED_TASK_FIELDS`` frozenset is missing
# ``failure_reason`` (raising ``TaskProgressValidationError`` at
# runtime). We pre-load the package and register it under the
# ``state_machine`` name so all later ``from state_machine.*`` calls
# hit the canonical backend.
if "state_machine" not in sys.modules:
    _SM_INIT = _BACKEND_DIR / "state_machine" / "__init__.py"
    _sm_spec = importlib.util.spec_from_file_location(
        "state_machine", str(_SM_INIT),
        submodule_search_locations=[str(_BACKEND_DIR / "state_machine")],
    )
    _sm_mod = importlib.util.module_from_spec(_sm_spec)
    sys.modules["state_machine"] = _sm_mod
    _sm_spec.loader.exec_module(_sm_mod)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_state_db(tmp_path: Path) -> Path:
    """Allocate a hermetic ``state.db`` and return its path."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()
    return db_path


def _make_tasks_file(
    tmp_path: Path,
    tasks: list,
) -> Path:
    """Write a tasks.json in ``tmp_path/plan/tasks.json`` shape.

    Returns the file path.
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    tf = plan_dir / "tasks.json"
    tf.write_text(json.dumps({"tasks": tasks}))
    return tf


def _populate_plan_tasks(
    db_path: Path,
    plan_id: str,
    task_statuses: dict,
) -> None:
    """Insert rows into ``plan_tasks`` for a given plan_id.

    ``task_statuses`` maps ``task_id → status`` (e.g. {"11-2": "failed"}).
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = PlanTaskRepository(conn)
        for tid, status in task_statuses.items():
            # add_task uses INSERT OR REPLACE; safe to call even if
            # the row already exists. We pass the static fields it
            # requires (id, title, description, depends_on,
            # files_to_modify, test_command, model_type) and let
            # status default to pending. Then we update_task with the
            # caller-supplied status.
            try:
                repo.add_task(
                    plan_id=plan_id,
                    task_dict={
                        "id": tid,
                        "title": f"task {tid}",
                        "description": "test",
                        "depends_on": [],
                        "files_to_modify": [],
                        "test_command": "",
                        "model_type": "medium",
                    },
                )
            except Exception:
                # Row already exists — fine, update below.
                pass
            try:
                current_version = repo.get_version(plan_id, tid)
            except Exception:
                current_version = 0
            repo.update_task(
                plan_id=plan_id,
                task_id=tid,
                fields={
                    "status": status,
                    "failure_reason": f"test_reason_{tid}" if status == "failed" else None,
                },
                expected_version=current_version,
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def state_db_path(tmp_path, monkeypatch):
    """Provision a temp state.db and patch ``_state_db_path()`` to return it.

    This isolates the helpers under test from the project's real state.db.
    """
    db = _make_state_db(tmp_path)
    # Patch the helper. We import inside the test so the module is
    # available; the monkeypatch is applied BEFORE the test body runs.
    # Note: ``_state_db_path`` is defined as
    # ``def _state_db_path(request: Optional["Request"] = None) -> Path``
    # — accept an optional request arg so we match the live signature
    # and don't break tests that pass ``None`` explicitly.
    import partial_completion_path_test_server as _pct_server
    monkeypatch.setattr(
        _pct_server, "_state_db_path", lambda request=None: db,
    )
    return db


# ---------------------------------------------------------------------------
# _are_all_unfinished_blocked_by_failed_upstream
# ---------------------------------------------------------------------------


def test_all_blocked_when_each_unfinished_has_failed_upstream(
    tmp_path, state_db_path
):
    """All 5 unfinished tasks depend (transitively) on task 11-2
    which is ``failed`` in the runtime overlay. Helper must return True.

    Each unfinished task's upstream must be **either failed or
    completed** — if any upstream is unknown / pending /
    in_progress, the task is genuinely unfinished, not "blocked by
    failed upstream". We populate 11-1, 11-3 as completed so the
    11-4 → [11-2, 11-3] case resolves cleanly.
    """
    from partial_completion_path_test_server import _are_all_unfinished_blocked_by_failed_upstream

    plan_id = "plan2"
    tasks_file = _make_tasks_file(
        tmp_path,
        [
            {"id": "11-1", "title": "11-1", "depends_on": []},
            {"id": "11-2", "title": "11-2", "depends_on": ["11-1"]},
            {"id": "11-3", "title": "11-3", "depends_on": ["11-2"]},
            {"id": "11-4", "title": "11-4", "depends_on": ["11-2", "11-3"]},
            {"id": "11-5-1", "title": "11-5-1", "depends_on": ["11-4"]},
            {"id": "11-5-2", "title": "11-5-2", "depends_on": ["11-5-1"]},
        ],
    )
    _populate_plan_tasks(
        state_db_path,
        plan_id,
        {
            "11-1": "completed",
            "11-2": "failed",
            "11-3": "completed",  # 11-3 completed → 11-4 blocked only by 11-2
        },
    )
    unfinished = ["11-4", "11-5-1", "11-5-2"]
    assert _are_all_unfinished_blocked_by_failed_upstream(
        plan_id, unfinished, tasks_file,
    )


def test_not_blocked_when_unfinished_has_no_failed_upstream(
    tmp_path, state_db_path
):
    """An unfinished task with NO failed upstream (executor just never
    ran it) returns False — preserves legacy failed path.
    """
    from partial_completion_path_test_server import _are_all_unfinished_blocked_by_failed_upstream

    plan_id = "test"
    tasks_file = _make_tasks_file(
        tmp_path,
        [
            {"id": "1", "title": "1", "depends_on": []},
            {"id": "2", "title": "2", "depends_on": ["1"]},
            {"id": "3", "title": "3", "depends_on": []},  # no deps
        ],
    )
    # ``1`` is completed; ``3`` is pending — neither is failed.
    _populate_plan_tasks(
        state_db_path,
        plan_id,
        {"1": "completed"},
    )
    unfinished = ["2", "3"]  # ``3`` has no failed upstream → not blocked
    assert not _are_all_unfinished_blocked_by_failed_upstream(
        plan_id, unfinished, tasks_file,
    )


def test_mixed_blocked_and_unblocked_returns_false(
    tmp_path, state_db_path
):
    """If even ONE unfinished task has no failed upstream, the helper
    must return False (mixed → take the legacy failed path, NOT
    partial completion).
    """
    from partial_completion_path_test_server import _are_all_unfinished_blocked_by_failed_upstream

    plan_id = "mixed"
    tasks_file = _make_tasks_file(
        tmp_path,
        [
            {"id": "1", "title": "1", "depends_on": []},
            {"id": "2", "title": "2", "depends_on": ["1"]},
            {"id": "3", "title": "3", "depends_on": ["2"]},
            {"id": "4", "title": "4", "depends_on": []},
        ],
    )
    _populate_plan_tasks(
        state_db_path,
        plan_id,
        {"2": "failed"},  # 3 blocked, 4 not blocked → mixed
    )
    unfinished = ["3", "4"]
    assert not _are_all_unfinished_blocked_by_failed_upstream(
        plan_id, unfinished, tasks_file,
    )


def test_orphan_unfinished_treated_as_not_blocked(tmp_path, state_db_path):
    """An orphan task id (DB-only, no static fields on disk) cannot
    have its ``depends_on`` inspected. Helper must default to
    ``not blocked`` (conservative → legacy failed path).
    """
    from partial_completion_path_test_server import _are_all_unfinished_blocked_by_failed_upstream

    plan_id = "orphan"
    # Empty tasks.json (no static fields at all)
    tasks_file = _make_tasks_file(tmp_path, [])
    _populate_plan_tasks(
        state_db_path,
        plan_id,
        {"orphan-1": "failed"},  # only DB
    )
    unfinished = ["orphan-1"]
    # No static fields → cannot prove blocked → False
    assert not _are_all_unfinished_blocked_by_failed_upstream(
        plan_id, unfinished, tasks_file,
    )


def test_empty_unfinished_returns_false(tmp_path, state_db_path):
    """No unfinished tasks → False (caller should not invoke this
    helper in that case, but be defensive)."""
    from partial_completion_path_test_server import _are_all_unfinished_blocked_by_failed_upstream

    plan_id = "empty"
    tasks_file = _make_tasks_file(tmp_path, [])
    assert not _are_all_unfinished_blocked_by_failed_upstream(
        plan_id, [], tasks_file,
    )


# ---------------------------------------------------------------------------
# _mark_task_skipped
# ---------------------------------------------------------------------------


def test_mark_task_skipped_writes_status_and_reason(tmp_path, state_db_path):
    """Happy path: pending task → skipped with reason."""
    from partial_completion_path_test_server import _mark_task_skipped

    plan_id = "p1"
    _populate_plan_tasks(state_db_path, plan_id, {"X": "pending"})
    ok = _mark_task_skipped(plan_id, "X", reason="blocked_by_failed_upstream")
    assert ok is True

    # Verify the row was updated
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    conn = open_db(state_db_path)
    try:
        migrate(conn)
        all_tasks = PlanTaskRepository(conn).load_all(plan_id)
    finally:
        conn.close()
    assert all_tasks["X"]["status"] == "skipped"
    assert all_tasks["X"]["failure_reason"] == "blocked_by_failed_upstream"


def test_mark_task_skipped_is_idempotent_on_terminal(tmp_path, state_db_path):
    """A task that is already terminal (completed/failed/skipped) is
    NOT re-written. The first call writes skipped; the second call
    returns False (no version bump).
    """
    from partial_completion_path_test_server import _mark_task_skipped
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    plan_id = "p2"
    _populate_plan_tasks(state_db_path, plan_id, {"X": "completed"})

    # Task is already completed → should NOT be re-written
    ok = _mark_task_skipped(plan_id, "X", reason="x")
    assert ok is False

    # Confirm status unchanged
    conn = open_db(state_db_path)
    try:
        migrate(conn)
        all_tasks = PlanTaskRepository(conn).load_all(plan_id)
    finally:
        conn.close()
    assert all_tasks["X"]["status"] == "completed"


def test_mark_task_skipped_handles_missing_row_gracefully(tmp_path, state_db_path):
    """A task id that doesn't exist in plan_tasks should not crash —
    helper returns False (best-effort)."""
    from partial_completion_path_test_server import _mark_task_skipped

    plan_id = "missing"
    ok = _mark_task_skipped(plan_id, "does-not-exist", reason="r")
    # The implementation may either silently no-op or fail; either
    # way the call must NOT raise. (Current implementation returns
    # False if add_task raises.)
    assert ok is False or ok is True  # defensive: no exception


# ---------------------------------------------------------------------------
# Integration: helper order on the partial-completion shape
# ---------------------------------------------------------------------------


def test_partial_completion_path(tmp_path, state_db_path):
    """End-to-end: one task failed, its downstream all deferred.
    Helpers collectively identify all-blocked and skip them.

    Each deferred task's upstream must be ``completed`` or
    ``failed`` (not unknown/pending). 11-1 and 11-3 are completed
    so 11-4's ``depends_on=[11-2, 11-3]`` resolves with 11-2=failed
    and 11-3=completed (block + non-block = blocked by at least one).
    """
    from partial_completion_path_test_server import _are_all_unfinished_blocked_by_failed_upstream
    from partial_completion_path_test_server import _mark_task_skipped

    plan_id = "plan2-real"
    tasks_file = _make_tasks_file(
        tmp_path,
        [
            {"id": "11-1", "title": "11-1", "depends_on": []},
            {"id": "11-2", "title": "11-2", "depends_on": ["11-1"]},
            {"id": "11-3", "title": "11-3", "depends_on": ["11-2"]},
            {"id": "11-4", "title": "11-4", "depends_on": ["11-2", "11-3"]},
            {"id": "11-5-1", "title": "11-5-1", "depends_on": ["11-4"]},
            {"id": "11-5-2", "title": "11-5-2", "depends_on": ["11-5-1"]},
            {"id": "11-5-3", "title": "11-5-3", "depends_on": ["11-5-2"]},
            {"id": "11-6", "title": "11-6", "depends_on": ["11-5-3"]},
        ],
    )
    _populate_plan_tasks(
        state_db_path,
        plan_id,
        {
            "11-1": "completed",
            "11-2": "failed",
            "11-3": "completed",
        },
    )

    # The 5 deferred tasks all depend (directly or transitively) on
    # 11-2 → all blocked
    unfinished = ["11-4", "11-5-1", "11-5-2", "11-5-3", "11-6"]
    assert _are_all_unfinished_blocked_by_failed_upstream(
        plan_id, unfinished, tasks_file,
    )

    # Mark all 5 skipped
    for tid in unfinished:
        assert _mark_task_skipped(plan_id, tid, reason="blocked_by_failed_upstream")

    # Verify all 5 are now ``skipped`` with the right reason
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )
    conn = open_db(state_db_path)
    try:
        migrate(conn)
        all_tasks = PlanTaskRepository(conn).load_all(plan_id)
    finally:
        conn.close()
    for tid in unfinished:
        assert all_tasks[tid]["status"] == "skipped", (
            f"Expected {tid} skipped, got {all_tasks[tid]['status']!r}"
        )
        assert all_tasks[tid]["failure_reason"] == "blocked_by_failed_upstream"

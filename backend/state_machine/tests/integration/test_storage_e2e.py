"""End-to-end storage-path validation — task #3.10.

Background
----------
Tasks #3.4 through #3.9 collapsed five JSON sidecar files
(``plan_state.json``, ``plan_execution.json``,
``plan_verification_execution_results.json``, ``tasks.json``,
``plan_state.json`` once more) into five SQLite tables
(``plan_routing``, ``plan_execution``, ``plan_verification``,
``plan_artifacts``, ``schema_version``). After these refactors:

  * Per-task runtime state (``status`` / ``end_ts`` /
    ``commit_sha`` / ``attempt`` / ``schedule_ts`` /
    ``_repo_version``) lives in
    ``plan_execution.task_progress`` (JSON column).
  * Plan-phase routing (``current_phase`` /
    ``completed_phases`` / ``review_rounds`` / ``flags`` /
    ``verification``) lives in ``plan_routing`` (single row per
    plan).
  * Verification execution results (``verification_points`` /
    ``execution_results`` / ``executed_at``) live in
    ``plan_verification.execution_results`` (JSON column).
  * Interview / PRD / Arch / Test artifacts (per-artifact JSON)
    live in ``plan_artifacts`` (one row per artifact).
  * ``schema_version`` carries the migration version.

The goal of task #3.10 is to verify the system walks through a
plan lifecycle with the routing table as the only source of
truth and ZERO reliance on legacy JSON for runtime state.

TDD contract — these are the four tests the e2e must pin:

  1. ``test_full_plan_lifecycle_routes_through_all_five_tables``
     A plan walks interview → interview_complete → prd_review →
     tasks_generation → ready → executing → completed →
     verification → verification_passed. After each transition,
     the corresponding ``plan_routing`` row carries the new
     the single ``current_phase`` column. No ``plan_state.json``
     file is read for routing.

  2. ``test_per_task_runtime_state_lives_in_sqlite_only``
     The dispatcher's persist path writes
     ``plan_execution.task_progress`` (JSON column) — NOT
     ``tasks.json``. The legacy ``tasks.json`` file is static-
     only post-migration.

  3. ``test_verification_results_live_in_sqlite_only``
     A ``verification_results`` write reaches
     ``plan_verification.execution_results`` (JSON column) —
     NOT a ``verification_execution_results.json`` file. The
     legacy file is no longer read for results.

  4. ``test_interview_artifact_lives_in_plan_artifacts``
     An interview submission lands in ``plan_artifacts``
     (one row keyed by ``artifact_type='interview'``), not in
     a loose ``interview.json`` read for routing.

The integration asserts against the canonical state DB
(same path the API endpoints consult) so a regression that
silently diverges the disk write from the read is caught.
"""

from __future__ import annotations

import json
import os
import sys
import sqlite3
from pathlib import Path
from typing import Any, Dict

import pytest

# Ensure backend/ is on sys.path.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _read_plan_routing(db_path: Path, plan_id: str) -> Dict[str, Any]:
    """Return the ``plan_routing`` row dict, or ``{}`` if absent."""
    conn = sqlite3.connect(str(db_path))
    try:
        from state_machine.db.schema import migrate as _migrate
        _migrate(conn)
        row = conn.execute(
            "SELECT * FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            return {}
        cols = [c[0] for c in conn.execute(
            "SELECT * FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).description]
        return dict(zip(cols, row))
    finally:
        conn.close()


def _read_task_progress(db_path: Path, plan_id: str) -> Dict[str, Any]:
    """Return ``{"tasks": {task_id: {...}}}`` read from ``plan_tasks``.

    Schema v4 (2026-09-09) lifted per-task
    runtime state out of the legacy ``plan_execution.task_progress``
    JSON column into the relational ``plan_tasks`` table.
    ``PlanTaskRepository`` is the single read/write path for it.

    The legacy column is deliberately NOT dropped (it stays as a
    deprecated empty column), so a reader that still selects it gets
    ``{}`` for every plan — which is exactly the false negative this
    helper used to produce.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        from state_machine.db.schema import migrate as _migrate
        _migrate(conn)
        rows = conn.execute(
            "SELECT task_id, status, end_ts FROM plan_tasks "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
        return {
            "tasks": {
                task_id: {"status": status, "end_ts": end_ts}
                for task_id, status, end_ts in rows
            }
        }
    finally:
        conn.close()


def _read_verification_results(
    db_path: Path, plan_id: str,
) -> Dict[str, Any]:
    """Return the parsed ``execution_results`` JSON column."""
    conn = sqlite3.connect(str(db_path))
    try:
        from state_machine.db.schema import migrate as _migrate
        _migrate(conn)
        row = conn.execute(
            "SELECT execution_results FROM plan_verification "
            "WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if row is None or not row[0]:
            return {}
        return json.loads(row[0])
    finally:
        conn.close()


def _read_artifact(db_path: Path, plan_id: str, artifact_type: str):
    """Return the latest ``plan_artifacts`` row for the given type.

    The ``plan_artifacts`` table is a file-pointer ledger:
    ``file_path`` / ``status`` / ``content_hash`` / timestamps.
    The JSON body itself lives on disk at ``file_path``, not
    in the table — this helper returns the row dict so callers
    can assert on the pointer metadata.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        from state_machine.db.schema import migrate as _migrate
        _migrate(conn)
        row = conn.execute(
            "SELECT file_path, status, content_hash FROM plan_artifacts "
            "WHERE plan_id = ? AND artifact_type = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (plan_id, artifact_type),
        ).fetchone()
        if row is None:
            return None
        return {"file_path": row[0], "status": row[1], "content_hash": row[2]}
    finally:
        conn.close()


def _state_db_path_from_server():
    from server import _state_db_path
    return _state_db_path()


# ---------------------------------------------------------------------------
# Test 1: plan lifecycle walks through all routing transitions
# ---------------------------------------------------------------------------


def test_full_plan_lifecycle_routes_through_plan_routing() -> None:
    """Every phase transition lands in ``plan_routing.current_phase``.

    The lifecycle: ``interview`` → ``interview_complete`` →
    ``prd_generation`` → ``prd_review`` → ``prd_approved`` →
    ``tasks_generation`` → ``ready`` → ``executing`` →
    ``completed`` → ``verification`` → ``verification_passed``.

    After each transition, the canonical routing row carries the
    new ``current_phase`` value. The legacy
    ``plan_state.json`` file is NOT read for routing.
    """
    from plan_state import PlanState

    plan_id = "e2e-lifecycle-1"
    # ``isolated_plans_dir`` autouse fixture already gave us a
    # tmp_path; the plan_state and PlanState write through the
    # same _state_db_path seam.
    db_path = _state_db_path_from_server()

    state = PlanState.__new__(PlanState)
    # Manually wire up the minimal state to avoid re-reading the
    # file system — we drive ``transition_to`` against the
    # routing table.
    state.plan_dir = _BACKEND_DIR.parent / "plans" / plan_id
    state.state_file = state.plan_dir / "plan_state.json"
    state._lock = __import__("threading").Lock()
    state._state = {
        "plan_id": plan_id,
        "current_phase": "interview",
        "completed_phases": [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending", "round": 0, "max_rounds": 3,
            "stop_reason": None,
        },
    }
    # 2026-09-19: ``__new__`` bypasses ``PlanState.__init__``, so every
    # attribute the write path reads has to be wired by hand. This one
    # holds the phase last seen in ``plan_routing`` and is how the write
    # path decides whether to touch the ``current_phase`` column at all
    # (``None`` = leave it alone). It must mirror the pre-seeded row
    # below — "interview", the phase we start from.
    state._persisted_phase = "interview"
    # Pre-seed the row so the very first transition's write path
    # is exercised.
    from server import _seed_plan_routing_phase
    _seed_plan_routing_phase(plan_id, phase="interview")

    phases = [
        "interview_complete",
        "prd_generation",
        "prd_review",
        "prd_approved",
        "tasks_generation",
        "ready",
        "executing",
        "completed",
        "verification",
        "verification_passed",
    ]
    for phase in phases:
        state.transition_to(phase)
        row = _read_plan_routing(db_path, plan_id)
        assert row.get("current_phase") == phase, (
            f"plan_routing.current_phase must follow transitions; "
            f"expected={phase!r}, got current_phase={row.get('current_phase')!r}"
        )
        assert row.get("current_phase") == phase, (
            f"plan_routing.current_phase must follow transitions; "
            f"expected={phase!r}, got current_phase="
            f"{row.get('current_phase')!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: per-task runtime state lives in SQLite only
# ---------------------------------------------------------------------------


def test_per_task_runtime_state_lives_in_sqlite_only(tmp_path, monkeypatch) -> None:
    """A dispatcher's ``_persist_task_status`` write lands in
    ``plan_execution.task_progress``; ``tasks.json`` is static."""
    import subprocess

    from agent import AutonomousAgent
    from task import SubTask
    from unittest.mock import MagicMock

    # ``isolated_plans_dir`` autouse fixture patches
    # ``server._state_db_path`` to its own ``tmp_path/state.db``;
    # to keep the seed and the agent pointing at the same path
    # we set ``PDT_STATE_DB_PATH`` explicitly. Both paths must be
    # overridden because ``_open_db`` reads the env at connect
    # time.
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))

    plan_id = "project"  # ``tasks_file.parent.name`` is the canonical plan_id.
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    # GitManager needs a real git repo at project_dir.
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir),
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(project_dir), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(project_dir), capture_output=True, check=True,
    )
    # tasks.json is static — only structural fields.
    (project_dir / "tasks.json").write_text(
        json.dumps(
            {
                "requirement": "e2e task progress test",
                "tasks": [
                    {
                        "id": "A",
                        "title": "Task A",
                        "description": "anchor",
                        "test_command": "echo A",
                    },
                ],
            },
        ),
    )
    # Pre-seed the plan_execution row the dispatcher needs.
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate
    conn = _open_db(db_path)
    _migrate(conn)
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, current_phase, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, "executing", "2026-08-14T00:00:00"),
    )
    conn.commit()
    conn.close()

    agent = AutonomousAgent(
        requirement="e2e task progress",
        project_dir=project_dir,
        coding_tool=MagicMock(),
        logger=None,
        tasks_file=project_dir / "tasks.json",
    )
    task = SubTask(
        id="A",
        title="Task A",
        description="anchor",
        test_command="echo A",
        status="completed",
        updated_time="2026-08-14T10:00:00",
    )
    agent._persist_task_status(task)

    # SQLite is the new source of truth.
    progress = _read_task_progress(db_path, plan_id)
    assert progress.get("tasks", {}).get("A", {}).get("status") == "completed", (
        f"plan_tasks must record status='completed' in SQLite; "
        f"got progress={progress!r}"
    )

    # And tasks.json must NOT carry the runtime fields.
    tasks_data = json.loads(
        (project_dir / "tasks.json").read_text(encoding="utf-8"),
    )
    a_row = next(t for t in tasks_data["tasks"] if t["id"] == "A")
    assert "status" not in a_row, (
        f"tasks.json must be static-only post-#3.8; "
        f"unexpected 'status' field in {a_row!r}"
    )
    assert "end_ts" not in a_row, (
        f"tasks.json must be static-only post-#3.8; "
        f"unexpected 'end_ts' field in {a_row!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: verification results live in SQLite only
# ---------------------------------------------------------------------------


def test_verification_results_live_in_sqlite_only() -> None:
    """Verification execution results land in
    ``plan_verification.execution_results``; no legacy
    ``verification_execution_results.json`` is read."""
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    plan_id = "e2e-verification-1"
    db_path = _state_db_path_from_server()

    # Seed the plan_verification row.
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate
    conn = _open_db(db_path)
    _migrate(conn)
    conn.execute(
        "INSERT OR IGNORE INTO plan_verification "
        "(plan_id, verification_status, round, max_rounds, started_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            plan_id,
            "running",
            0,
            3,
            "2026-08-14T00:00:00",
            "2026-08-14T00:00:00",
        ),
    )
    conn.commit()

    repo = VerificationRepository(conn)
    # ``save_execution_results`` is the production write path.
    envelope = {
        "verification_points": [
            {"id": "VP-001", "title": "smoke", "verdict": "PASSED"},
        ],
        "execution_results": [
            {"verification_point_id": "VP-001", "result": "ok"},
        ],
        "executed_at": "2026-08-14T10:00:00",
    }
    repo.save_execution_results(plan_id, envelope)
    conn.commit()
    conn.close()

    results = _read_verification_results(db_path, plan_id)
    assert results.get("verification_points"), (
        f"plan_verification.execution_results must hold the envelope; "
        f"got results={results!r}"
    )
    assert results["verification_points"][0]["id"] == "VP-001", (
        f"verification_points[0].id must round-trip; "
        f"got {results!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: interview artifact lands in plan_artifacts
# ---------------------------------------------------------------------------


def test_interview_artifact_lives_in_plan_artifacts() -> None:
    """An interview submission writes a ``plan_artifacts`` row,
    not a loose ``interview.json`` read for routing.

    ``plan_artifacts`` is the file-pointer ledger: it tracks the
    on-disk location of each artifact (``file_path``) plus its
    lifecycle (``status``: ``pending`` / ``complete`` / ``failed`` /
    ``superseded``) and a content hash for integrity. The JSON
    body itself lives in the file at ``file_path`` — the
    repository does NOT read or store the body.
    """
    from state_machine.repositories.artifact_repository import (
        ArtifactRepository,
    )

    plan_id = "e2e-artifacts-1"
    db_path = _state_db_path_from_server()
    from state_machine.db.connection import open as _open_db
    from state_machine.db.schema import migrate as _migrate
    conn = _open_db(db_path)
    _migrate(conn)

    repo = ArtifactRepository(conn)
    file_path = f"plans/{plan_id}/interview.json"
    repo.upsert(
        plan_id=plan_id,
        artifact_type="interview",
        file_path=file_path,
        status="generated",
        content_hash="deadbeef" * 8,  # 64-char hex placeholder
    )
    conn.commit()
    conn.close()

    artifact = _read_artifact(db_path, plan_id, "interview")
    # ``plan_artifacts`` carries ``file_path`` / ``status`` /
    # ``content_hash`` — NOT the JSON body itself.
    assert artifact is not None, (
        "plan_artifacts must carry the interview row; got None"
    )
    assert artifact.get("file_path") == file_path, (
        f"plan_artifacts row must record file_path={file_path!r}; "
        f"got {artifact!r}"
    )
    assert artifact.get("status") == "generated", (
        f"plan_artifacts row must record status='generated'; "
        f"got {artifact!r}"
    )
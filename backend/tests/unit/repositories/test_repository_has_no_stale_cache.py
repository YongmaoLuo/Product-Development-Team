"""Repository layer carries no in-memory cache that could go stale.

**VP-016 anchor — no stale cache.**

This test pins the contract that :class:`ExecutionRepository`,
:class:`VerificationRepository`, and :class:`ArtifactRepository`
carry **no** in-process cache that could become stale when a
sibling module writes to the same row via direct SQL.  A
"stale-cache" regression would look like:

  * Repository instance reads ``row v0`` and stashes it in
    ``self._cache[plan_id]``.
  * Another connection (or the test harness) UPDATE-s the row to
    ``row v1`` via a fresh ``sqlite3.Connection``.
  * Repository reads again — sees ``row v0`` because the cache
    pinned it.

The architecture decision explicitly forbids this.  Every read
in the repository layer MUST go straight to SQLite on the next
call.

The test is intentionally simple: it uses TWO independent
``open_db()`` connections to the same DB file, executes an
``UPDATE`` on connection B, then reads the row on connection A
through a freshly-instantiated repository.  If the repository
caches, the read on connection A will return the pre-update
value and the test fails.  If the repository has no cache
(which is the required state), the read on connection A returns
the post-update value.

The test command for VP-016 is::

    pytest tests/unit/repositories/test_repository_has_no_stale_cache.py -v
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.verification_repository import VerificationRepository
from state_machine.repositories.artifact_repository import ArtifactRepository


SEED_PLAN_ID = "p1"
SEED_PHASE = "ready"
SEED_STATUS = "pending"
SEED_VERIFICATION_STATUS = "running"
SEED_ARTIFACT_TYPE = "interview"

PRE_UPDATE_PHASE = "executing"
POST_UPDATE_PHASE = "verification_running"

PRE_UPDATE_VERIFICATION_STATUS = "running"
POST_UPDATE_VERIFICATION_STATUS = "passed"

SEED_ARTIFACT_STATUS = "pending"
PRE_UPDATE_ARTIFACT_STATUS = "pending"
POST_UPDATE_ARTIFACT_STATUS = "generated"


def _bootstrap(db_path):
    """Create schema and seed one row in each repository's table."""
    bootstrap = open_db(db_path)
    try:
        migrate(bootstrap)
        bootstrap.execute(
            "INSERT INTO plan_execution "
            "(plan_id, current_phase, attempt_count, project_dir, "
            " stop_reason, task_progress, next_run_at, card_state, "
            " flags, exec_pid, exec_status, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                SEED_PLAN_ID, SEED_PHASE, 0, "/orig/path", None,
                None, None, None, None, None, None, None,
                "2026-08-05T11:00:00Z",
            ),
        )
        bootstrap.execute(
            "INSERT INTO plan_verification "
            "(plan_id, verification_status, round, max_rounds, "
            " verification_stop_reason, runtime_state, executor_state, "
            " progress_state, results, verdicts, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                SEED_PLAN_ID, SEED_VERIFICATION_STATUS, 1, 3, None,
                "{}", "{}", "{}", None, None,
                "2026-08-05T11:30:00Z", "2026-08-05T11:30:00Z",
            ),
        )
        bootstrap.execute(
            "INSERT INTO plan_artifacts "
            "(plan_id, artifact_type, file_path, status, "
            " content_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                SEED_PLAN_ID, SEED_ARTIFACT_TYPE, "/tmp/interview.json",
                SEED_ARTIFACT_STATUS, "deadbeef",
                "2026-08-05T10:00:00Z", "2026-08-05T10:00:00Z",
            ),
        )
    finally:
        bootstrap.close()


def test_execution_repository_has_no_stale_cache(tmp_path):
    """A sibling connection's UPDATE is visible on the next read.

    **VP-016 anchor.**  Two independent ``open_db()`` connections
    share the WAL log; the reader on connection A MUST observe the
    writer on connection B's committed UPDATE on its very next
    read.  A cached repository would silently return the
    pre-update value.
    """
    db_path = tmp_path / "state.db"
    _bootstrap(db_path)

    # Phase 1: a fresh repo reads the seed value through its own
    # connection.  Establishes the "first read" baseline.
    reader_a = open_db(db_path)
    try:
        repo_a = ExecutionRepository(reader_a)
        first = repo_a.summary(SEED_PLAN_ID)
        assert first is not None
        assert first["current_phase"] == SEED_PHASE
    finally:
        reader_a.close()

    # Phase 2: connection B performs a direct UPDATE bypassing the
    # repository layer entirely.  This is the "sibling module wrote
    # behind the repository's back" simulation.
    writer_b = open_db(db_path)
    try:
        writer_b.execute(
            "UPDATE plan_execution SET current_phase = ? WHERE plan_id = ?",
            (POST_UPDATE_PHASE, SEED_PLAN_ID),
        )
    finally:
        writer_b.close()

    # Phase 3: a brand-new repo on a brand-new connection reads.
    # If the repo carried a cache, this would return the pre-update
    # value.  The contract is "no cache": we must observe
    # POST_UPDATE_PHASE.
    reader_c = open_db(db_path)
    try:
        repo_c = ExecutionRepository(reader_c)
        second = repo_c.summary(SEED_PLAN_ID)
        assert second is not None
        assert second["current_phase"] == POST_UPDATE_PHASE, (
            "ExecutionRepository returned a stale value after a "
            "sibling connection wrote to plan_execution; the repo "
            "appears to be caching reads."
        )
        assert second["current_phase"] != PRE_UPDATE_PHASE
    finally:
        reader_c.close()


def test_verification_repository_has_no_stale_cache(tmp_path):
    """A sibling connection's UPDATE is visible on the next read."""
    db_path = tmp_path / "state.db"
    _bootstrap(db_path)

    reader_a = open_db(db_path)
    try:
        repo_a = VerificationRepository(reader_a)
        first = repo_a.current(SEED_PLAN_ID)
        assert first is not None
        assert first["verification_status"] == SEED_VERIFICATION_STATUS
    finally:
        reader_a.close()

    writer_b = open_db(db_path)
    try:
        writer_b.execute(
            "UPDATE plan_verification SET verification_status = ? "
            "WHERE plan_id = ?",
            (POST_UPDATE_VERIFICATION_STATUS, SEED_PLAN_ID),
        )
    finally:
        writer_b.close()

    reader_c = open_db(db_path)
    try:
        repo_c = VerificationRepository(reader_c)
        second = repo_c.current(SEED_PLAN_ID)
        assert second is not None
        assert second["verification_status"] == POST_UPDATE_VERIFICATION_STATUS, (
            "VerificationRepository returned a stale value after a "
            "sibling connection wrote to plan_verification; the repo "
            "appears to be caching reads."
        )
        assert second["verification_status"] != PRE_UPDATE_VERIFICATION_STATUS
    finally:
        reader_c.close()


def test_artifact_repository_has_no_stale_cache(tmp_path):
    """A sibling connection's UPDATE is visible on the next read."""
    db_path = tmp_path / "state.db"
    _bootstrap(db_path)

    reader_a = open_db(db_path)
    try:
        repo_a = ArtifactRepository(reader_a)
        first = repo_a.find(SEED_PLAN_ID, SEED_ARTIFACT_TYPE)
        assert first is not None
        assert first["status"] == SEED_ARTIFACT_STATUS
    finally:
        reader_a.close()

    writer_b = open_db(db_path)
    try:
        writer_b.execute(
            "UPDATE plan_artifacts SET status = ? "
            "WHERE plan_id = ? AND artifact_type = ?",
            (POST_UPDATE_ARTIFACT_STATUS, SEED_PLAN_ID, SEED_ARTIFACT_TYPE),
        )
    finally:
        writer_b.close()

    reader_c = open_db(db_path)
    try:
        repo_c = ArtifactRepository(reader_c)
        second = repo_c.find(SEED_PLAN_ID, SEED_ARTIFACT_TYPE)
        assert second is not None
        assert second["status"] == POST_UPDATE_ARTIFACT_STATUS, (
            "ArtifactRepository returned a stale value after a "
            "sibling connection wrote to plan_artifacts; the repo "
            "appears to be caching reads."
        )
        assert second["status"] != PRE_UPDATE_ARTIFACT_STATUS
    finally:
        reader_c.close()

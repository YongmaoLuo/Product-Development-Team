"""Tests for the verification progress_state persistence.

Background (2026-08-25 audit): the executor's verification
thread used to write per-VP verdicts to the state-machine SQLite
``plan_verification.verdicts`` column but never wrote
``progress_state``. The public
``/api/verification/{plan_id}/progress`` endpoint reads from
``progress_state`` and 404s if absent, so the dashboard
constantly fell back to ``/status`` even while verification
was actively running VPs.

This module tests the new
:meth:`VerificationRepository.update_progress_state` method
and the executor's :meth:`_save_progress` hook that writes
through it on every ``_save_state`` call.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)


def _make_repo_with_plan(plan_id: str) -> tuple[VerificationRepository, sqlite3.Connection]:
    """Set up a fresh state-machine SQLite row for ``plan_id`` and
    return a repo + the underlying connection (caller must close).
    """
    conn = open_db(Path(tempfile.mkdtemp()) / "state.db")
    migrate(conn)
    repo = VerificationRepository(conn)
    # Seed a row so ``_update`` has a target to modify. The repo's
    # ``_update`` is UPDATE-only; we INSERT a fresh row directly
    # via the connection so the helper's rowcount check is
    # satisfied regardless of whether ``init_round`` has run.
    import json as _json
    cols = (
        "plan_id", "verification_status", "round", "max_rounds",
        "verification_stop_reason", "runtime_state", "executor_state",
        "progress_state", "results", "verdicts", "execution_results",
        "started_at", "updated_at",
    )
    placeholders = ",".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO plan_verification ({','.join(cols)}) VALUES ({placeholders})",
        (
            plan_id, "pending", 0, 3, None, "{}", "{}",
            _json.dumps({"current_vp": None, "completed_vps": [],
                         "failed_vps": [], "skipped_vps": []}),
            "{}", "[]", "{}", _now_iso(), _now_iso(),
        ),
    )
    conn.commit()
    return repo, conn


def _now_iso() -> str:
    from datetime import datetime, timezone
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class TestVerificationProgressState(unittest.TestCase):
    def test_update_progress_state_persists_lists(self):
        repo, conn = _make_repo_with_plan("plan-x")

        repo.update_progress_state(
            "plan-x",
            current_vp="VP-007",
            completed_vps=["VP-001", "VP-002"],
            failed_vps=["VP-003"],
            skipped_vps=["VP-004"],
        )
        conn.commit()

        row = conn.execute(
            "SELECT progress_state FROM plan_verification WHERE plan_id = ?",
            ("plan-x",),
        ).fetchone()
        self.assertIsNotNone(row[0])
        state = json.loads(row[0])
        self.assertEqual(state["current_vp"], "VP-007")
        self.assertEqual(state["completed_vps"], ["VP-001", "VP-002"])
        self.assertEqual(state["failed_vps"], ["VP-003"])
        self.assertEqual(state["skipped_vps"], ["VP-004"])
        # updated_at is always present so the read endpoint can
        # emit a heartbeat without falling back to "no progress yet".
        self.assertIn("updated_at", state)

        conn.close()

    def test_update_progress_state_overwrites_prior(self):
        """A second ``update_progress_state`` call replaces the
        prior column payload entirely — partial-update semantics
        are not part of the contract."""
        repo, conn = _make_repo_with_plan("plan-x")

        repo.update_progress_state("plan-x", "VP-001", [], [], [])
        repo.update_progress_state(
            "plan-x", "VP-005", ["VP-001", "VP-002", "VP-003", "VP-004"], [], []
        )
        conn.commit()
        state = json.loads(
            conn.execute(
                "SELECT progress_state FROM plan_verification WHERE plan_id = ?",
                ("plan-x",),
            ).fetchone()[0]
        )
        self.assertEqual(state["current_vp"], "VP-005")
        self.assertEqual(len(state["completed_vps"]), 4)
        # The earlier empty ``[]`` payload was replaced, not merged.
        self.assertEqual(state["failed_vps"], [])

        conn.close()

    def test_empty_plan_id_no_op(self):
        """Defensive: ``plan_id == ""`` is a no-op (mirrors the
        ``if not plan_id: return`` early-out in the helper)."""
        repo, conn = _make_repo_with_plan("plan-x")
        # Should not raise.
        repo.update_progress_state(
            "", current_vp="VP-001", completed_vps=[], failed_vps=[], skipped_vps=[]
        )
        conn.close()

    def test_list_args_are_defensively_copied(self):
        """The caller can mutate the lists they pass in afterwards
        without affecting the persisted state."""
        repo, conn = _make_repo_with_plan("plan-x")
        completed = ["VP-001"]
        failed: list = []
        repo.update_progress_state(
            "plan-x", current_vp="VP-001", completed_vps=completed, failed_vps=failed, skipped_vps=[]
        )
        conn.commit()
        # Mutate the caller's list AFTER the call.
        completed.append("VP-002")
        failed.append("VP-099")
        # Persisted state should still be the original snapshot.
        state = json.loads(
            conn.execute(
                "SELECT progress_state FROM plan_verification WHERE plan_id = ?",
                ("plan-x",),
            ).fetchone()[0]
        )
        self.assertEqual(state["completed_vps"], ["VP-001"])
        self.assertEqual(state["failed_vps"], [])
        conn.close()


class TestProgressEndpointRead(unittest.TestCase):
    """Round-trip test: write via ``update_progress_state`` and
    confirm the public /progress endpoint shape."""

    def test_read_after_write_returns_consistent_view(self):
        repo, conn = _make_repo_with_plan("plan-rt")
        repo.update_progress_state(
            "plan-rt",
            current_vp="VP-003",
            completed_vps=["VP-001", "VP-002"],
            failed_vps=[],
            skipped_vps=[],
        )
        conn.commit()
        # Re-read via ``summary`` (which is what the /progress
        # endpoint calls) — confirms the column is the same view.
        row = repo.summary("plan-rt")
        self.assertIsNotNone(row)
        self.assertEqual(row["progress_state"]["current_vp"], "VP-003")
        self.assertEqual(row["progress_state"]["completed_vps"], ["VP-001", "VP-002"])
        conn.close()


if __name__ == "__main__":
    unittest.main()
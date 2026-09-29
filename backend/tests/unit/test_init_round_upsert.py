"""Tests for the UPSERT behaviour of ``init_round``.

Background (2026-08-25 audit): ``init_round`` was UPDATE-only.
On a plan with no pre-existing ``plan_verification`` row
(common for plans created after the state-machine migration),
the UPDATE affected 0 rows, no error, no row created. Every
subsequent ``append_verdict`` / ``update_progress_state`` call
then failed with a swallowed ``KeyError``, so the executor
silently believed it had written progress state and the
``/api/verification/{plan_id}/progress`` endpoint returned
``404 verification not started``.

Fix: probe ``current(plan_id)`` first and insert the row if
absent, before the UPDATE. Idempotent.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)


def _make_repo() -> tuple[VerificationRepository, sqlite3.Connection]:
    conn = open_db(Path(tempfile.mkdtemp()) / "state.db")
    migrate(conn)
    return VerificationRepository(conn), conn


class TestInitRoundUpsert(unittest.TestCase):
    def test_init_round_inserts_when_row_missing(self):
        """No row initially; ``init_round`` must create one."""
        repo, conn = _make_repo()
        plan_id = "plan-fresh"

        # Sanity: row absent before the call.
        self.assertIsNone(repo.current(plan_id))

        repo.init_round(plan_id, round_n=1, max_rounds=3)

        row = repo.current(plan_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["round"], 1)
        self.assertEqual(row["max_rounds"], 3)
        self.assertEqual(row["verification_status"], "running")

        conn.close()

    def test_init_round_updates_when_row_present(self):
        """Existing row is updated in place; not duplicated."""
        repo, conn = _make_repo()
        plan_id = "plan-exists"

        # Seed via insert() so the upsert path takes the UPDATE branch.
        repo.insert(plan_id, "pending", round=0, max_rounds=3)
        # Now init_round should hit the UPDATE branch.
        repo.init_round(plan_id, round_n=2, max_rounds=5)

        row = repo.current(plan_id)
        self.assertEqual(row["round"], 2)
        self.assertEqual(row["max_rounds"], 5)
        self.assertEqual(row["verification_status"], "running")
        conn.close()

    def test_init_round_then_update_progress_state(self):
        """End-to-end: insert via init_round, then ``update_progress_state``
        succeeds (the original bug swallowed the KeyError)."""
        repo, conn = _make_repo()
        plan_id = "plan-roundtrip"

        repo.init_round(plan_id, round_n=1, max_rounds=3)
        # Before the fix this raised KeyError because ``_update``
        # on a row inserted mid-call returned 0 rowcount.
        repo.update_progress_state(
            plan_id, current_vp="VP-001", completed_vps=[], failed_vps=[], skipped_vps=[]
        )
        conn.commit()

        state = repo.current(plan_id)
        self.assertIsNotNone(state)
        self.assertIn("progress_state", state)
        self.assertEqual(state["progress_state"]["current_vp"], "VP-001")
        conn.close()


if __name__ == "__main__":
    unittest.main()
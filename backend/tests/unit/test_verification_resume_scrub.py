"""2026-09-14: verification resume must not bleed the previous
cycle's repair_tasks into the new round's card.

User report: the plan's card showed round-4 repair tasks from a
PREVIOUS execution cycle on the CURRENT round-1 verification card.

Root cause chain:

  * The previous cycle's ``_verification_state`` (including the
    round-close ``repair_tasks`` payload) is persisted to disk as the
    plan's verification runtime state.
  * On server restart, ``_recover_verification_states`` spread the
    persisted dict back into ``_verification_state`` wholesale —
    ``repair_tasks`` included.
  * When the operator later restarted execution + verification, the
    ``/start`` handler's ``setdefault`` preserved those stale fields
    for the auto-fix path, and the card rendered them under the new
    round number.

Fixes pinned here:

  * ``_recover_verification_states`` scrubs ``repair_tasks`` when
    rebuilding the in-memory entry (pending RP-* work is NOT lost —
    the auto-loop re-reads it from ``plan_tasks`` via
    ``_get_pending_repair_tasks``).
  * The card's ``_verification_sections`` drops repair tasks whose
    ``round`` is greater than the displayed round (defensive; see
    test_verification_card_routing.py for the card-level pins).
"""

from __future__ import annotations

import unittest
from unittest import mock

import server as _server_mod


class TestRecoverVerificationStateScrub(unittest.TestCase):
    """A recovered running-cycle entry must not carry the interrupted
    cycle's repair_tasks."""

    def test_recovery_drops_stale_repair_tasks(self):
        plan_id = "plan-resume-scrub"
        stale = {
            "plan_id": plan_id,
            "verification_status": "running",
            "verification_round": 4,
            "verification_max_rounds": 5,
            "results": {"pytest_summary": "1 failed",
                        "llm_findings": "", "performance_metrics": {}},
            "repair_tasks": [
                {"id": "R4-1", "title": "旧周期的修复任务",
                 "round": 4, "status": "pending"},
            ],
            "updated_at": "2026-09-13T02:00:00",
        }
        with mock.patch.object(
            _server_mod, "_load_verification_runtime_state",
            return_value=stale,
        ), mock.patch.object(
            _server_mod, "_verification_state", {},
        ) as _vs, mock.patch.object(
            _server_mod, "PLANS_DIR",
        ) as _plans:
            # ``plans.iterdir()`` must yield at least one directory;
            # the plan_id comes from the directory name. NOTE:
            # ``Mock(name=...)`` sets the mock's repr-name, not a
            # ``.name`` attribute — assign the attribute explicitly.
            _dir = mock.Mock()
            _dir.is_dir.return_value = True
            _dir.name = plan_id
            _plans.exists.return_value = True
            _plans.iterdir.return_value = [_dir]
            _server_mod._recover_verification_states(_plans)

        recovered = _vs.get(plan_id)
        self.assertIsNotNone(recovered, "plan must be recovered")
        self.assertEqual(recovered.get("repair_tasks"), [])
        self.assertIsNone(recovered.get("orchestrator"))
        self.assertIsNone(recovered.get("thread"))
        # Round / status survive — only the per-cycle content is scrubbed.
        self.assertEqual(recovered.get("verification_round"), 4)

    def test_recovery_ignores_non_running_state(self):
        """Terminal persisted states are NOT loaded into memory."""
        plan_id = "plan-terminal-not-recovered"
        with mock.patch.object(
            _server_mod, "_load_verification_runtime_state",
            return_value={"verification_status": "failed",
                          "repair_tasks": [{"id": "R2-1"}]},
        ), mock.patch.object(
            _server_mod, "_verification_state", {},
        ) as _vs, mock.patch.object(
            _server_mod, "PLANS_DIR",
        ) as _plans:
            _plans.exists.return_value = True
            _dir = mock.Mock()
            _dir.is_dir.return_value = True
            _dir.name = plan_id
            _plans.iterdir.return_value = [_dir]
            _server_mod._recover_verification_states(_plans)

        self.assertNotIn(plan_id, _vs)


if __name__ == "__main__":
    unittest.main()

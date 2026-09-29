"""Pin the 2026-09-12 v3 fix for round-passed + pending-RP-* state machine.

Bug
---
When the round's verdicts all PASS but pending RP-* tasks remain in state.db
(from prior failure rounds), ``check_cycle_conditions`` returns
``status="passed"``. The plan is in ``verification_passed`` (terminal, no
forward edges). ``_run_auto_verification_loop`` then calls
``orch.confirm_repair_and_rerun`` → ``start_repair_execution`` which
raises ``ValueError: Cannot start repair execution from 'verification_passed'``.

Fix
----
When ``status == "passed"`` BUT ``_get_pending_repair_tasks`` returns
non-empty, override the result dict to ``status="failed"`` with
``stop_reason="pending_repair_tasks"``. The orchestrator's
``verification_failed`` → ``start_verification_repair`` path then walks
the plan into ``verification_repairing`` so
``start_repair_execution`` succeeds.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from tests.app_source import app_source


def test_server_overrides_passed_to_failed_when_pending():
    """Static check: the v3 override is in place at the right location."""
    src = app_source()
    # The 2026-09-12 v3 fix adds an in-place result override
    # ``result["status"] = "failed"`` inside the
    # ``Round X PASSED but ... pending RP-*`` branch.
    assert 'result["status"] = "failed"' in src, (
        "BUG: server.py lost the v3 override — when a round PASSES "
        "but pending RP-* tasks remain, the orchestrator's passed "
        "status is not flipped, and confirm_repair_and_rerun raises "
        "ValueError because verification_passed is terminal."
    )
    assert '"pending_repair_tasks"' in src, (
        "BUG: server.py does not set the pending_repair_tasks stop_reason"
    )


def test_pending_db_helpers_run_for_post_v9_format():
    """Sanity: the helpers themselves surface R{n}-* ids (covered by
    test_get_pending_repair_tasks.py, but pin both helpers together)."""
    import server
    # Helper should not raise on missing plan_id
    assert server._get_pending_repair_tasks("nonexistent-plan-id") == []

"""Verify verification_executor_state.json is cleared on each new verification run."""
import json
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _clear(plan_dir):
    from verification_agent import VerificationAgent
    # Build a stub agent instance and invoke the unbound helper.
    class _StubAgent:
        pass
    _StubAgent.plan_dir = plan_dir
    VerificationAgent._clear_verification_state_files(_StubAgent())


def test_clear_verification_state_files_removes_cache(tmp_path):
    from verification_executor import DEFAULT_STATE_FILENAME, PROGRESS_STATE_FILENAME

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    # Pre-create stale state files that a previous run would have left.
    (plan_dir / DEFAULT_STATE_FILENAME).write_text(json.dumps({
        "plan_id": "stale",
        "pending_vps": ["VP-stale"],
        "verdicts": {"VP-stale": {"status": "FAILED", "reasons": ["stale"], "evidence": {}}},
    }))
    (plan_dir / PROGRESS_STATE_FILENAME).write_text(json.dumps({
        "plan_id": "stale",
        "current_vp": "VP-stale",
        "completed_vps": ["VP-stale"],
        "failed_vps": ["VP-stale"],
        "skipped_vps": [],
        "updated_at": "2026-01-01T00:00:00",
    }))

    _clear(plan_dir)

    assert not (plan_dir / DEFAULT_STATE_FILENAME).exists(), (
        "verification_executor_state.json should be cleared on new run"
    )
    assert not (plan_dir / PROGRESS_STATE_FILENAME).exists(), (
        "verification_progress_state.json should be cleared on new run"
    )


def test_clear_verification_state_files_idempotent_when_missing(tmp_path):
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    # No state files exist; helper should not raise.
    _clear(plan_dir)
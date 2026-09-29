"""Regression: _convert_plan_to_executor_schema must IGNORE the plan's
timeout_seconds.

2026-09-13: the per-VP timeout interface was
deleted. A legacy plan value must never reach the executor again —
VP-023's ``timeout_seconds: 120`` used to shrink the inner watcher AND
the outer cap, stalling round 4 indefinitely. The executor schema now
pins the field at the flat 1-hour cap (3600) for event observability.
"""
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_agent import VerificationAgent


def test_plan_timeout_seconds_is_ignored():
    plan = {
        "verification_points": [
            {
                "id": "VP-034",
                "verification_method": "automated_test",
                "title": "full pytest",
                "expected_result": "all green",
                "test_command": "pytest backend/tests/ -q",
                "priority": "high",
                # Legacy plan value that used to be forwarded — must be
                # dropped and replaced with the flat 3600.
                "timeout_seconds": 1800,
            },
            {
                "id": "VP-002",
                "verification_method": "automated_test",
                "title": "quick check",
                "test_command": "true",
            },
            {
                "id": "VP-023",
                "verification_method": "automated_test",
                "title": "nightly CI",
                "test_command": "docker compose up -d && pytest tests/ -v",
                # The exact value that stalled round 4 on 2026-09-13.
                "timeout_seconds": 120,
            },
        ]
    }
    out = VerificationAgent._convert_plan_to_executor_schema(plan)
    by_id = {v["id"]: v for v in out["vps"]}
    for vp_id, vp in by_id.items():
        assert vp["timeout_seconds"] == 3600, (
            f"{vp_id}: the plan's timeout_seconds ({vp.get('timeout_seconds')}) "
            "must NOT survive the schema conversion — the per-VP timeout "
            "interface was deleted 2026-09-13 and the field is pinned at "
            "the flat 1-hour cap (3600)."
        )

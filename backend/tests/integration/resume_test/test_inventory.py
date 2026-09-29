"""TDD tests for ``tests.integration.resume_test.inventory``.

These tests pin the four contracts documented in the resume test spec:

  1. ``test_is_active_true_for_executing`` — phase ``executing`` is ACTIVE.
  2. ``test_is_active_false_for_completed`` — phase ``completed`` is NOT ACTIVE.
  3. ``test_safe_when_only_except_id_active`` — when the only ACTIVE plan is
     in ``except_ids``, the inventory reports ``safe=True``.
  4. ``test_unsafe_when_other_plan_active`` — when a non-excepted plan is
     ACTIVE, the inventory reports ``safe=False`` and lists that plan ID.

All tests use ``unittest.mock.MagicMock`` for the client so they do NOT
need a running the backend.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

# ``resume_test`` is a package under ``backend/tests/integration``. To keep
# imports robust across project-root and backend-level pytest invocations,
# add ``backend/tests/integration`` to sys.path just like the api_client
# tests do.
_INTEGRATION_DIR = Path(__file__).resolve().parent.parent
if str(_INTEGRATION_DIR) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION_DIR))

from resume_test.inventory import (
    ACTIVE_PHASES,
    is_active,
    inventory_safe_to_restart,
    list_plans,
)


def _summary(phase: str) -> dict:
    """Build a minimal plan summary dict with the given current_phase."""
    return {"state": {"current_phase": phase}}


def test_is_active_true_for_executing():
    """phase=executing is in ACTIVE_PHASES and returns True."""
    assert "executing" in ACTIVE_PHASES
    assert is_active(_summary("executing")) is True


def test_is_active_false_for_completed():
    """phase=completed is not ACTIVE and returns False."""
    assert "completed" not in ACTIVE_PHASES
    assert is_active(_summary("completed")) is False


def test_safe_when_only_except_id_active():
    """Only the excepted plan is ACTIVE → safe=True, active_plans empty."""
    client = mock.MagicMock()
    client.get.return_value = {"plans": [{"id": "my-test-plan"}]}
    client.get_plan_summary.return_value = _summary("executing")

    result = inventory_safe_to_restart(client, except_ids={"my-test-plan"})

    assert result["safe"] is True
    assert result["active_plans"] == []
    assert len(result["all_plans"]) == 1
    assert result["all_plans"][0] == {
        "plan_id": "my-test-plan",
        "phase": "executing",
        "active": True,
    }


def test_unsafe_when_other_plan_active():
    """A non-excepted plan is ACTIVE → safe=False, active_plans lists it."""
    client = mock.MagicMock()
    client.get.return_value = {"plans": [{"id": "plan-a"}, {"id": "plan-b"}]}
    client.get_plan_summary.side_effect = [
        _summary("executing"),   # plan-a is active but excepted
        _summary("executing"),   # plan-b is active and NOT excepted
    ]

    result = inventory_safe_to_restart(client, except_ids={"plan-a"})

    assert result["safe"] is False
    assert result["active_plans"] == ["plan-b"]
    assert len(result["all_plans"]) == 2


def test_list_plans_returns_plans_array():
    """``list_plans`` extracts the ``plans`` array from the backend response."""
    client = mock.MagicMock()
    client.get.return_value = {
        "plans": [
            {"id": "plan-1"},
            {"id": "plan-2"},
        ]
    }

    plans = list_plans(client)

    assert plans == [{"id": "plan-1"}, {"id": "plan-2"}]
    client.get.assert_called_once_with("/api/plans")


def test_inventory_empty_plan_list_is_safe():
    """No plans → safe=True with empty all_plans / active_plans."""
    client = mock.MagicMock()
    client.get.return_value = {"plans": []}

    result = inventory_safe_to_restart(client)

    assert result["safe"] is True
    assert result["active_plans"] == []
    assert result["all_plans"] == []


def test_inventory_summary_failure_marks_unknown():
    """A single plan summary failure is marked unknown and does not block."""
    client = mock.MagicMock()
    client.get.return_value = {"plans": [{"id": "fragile-plan"}]}
    client.get_plan_summary.side_effect = RuntimeError("backend blip")

    result = inventory_safe_to_restart(client)

    assert result["safe"] is True
    assert result["active_plans"] == []
    assert result["all_plans"] == [
        {"plan_id": "fragile-plan", "phase": "unknown", "active": False}
    ]

"""Fold three legacy fixture sections into one status payload.

2026-09-23 — the notifier's card rebuild used to make **three** HTTP
reads (``/summary`` + ``/execution/{id}/progress`` +
``/verification/{id}/progress``), taken at three instants, and every
suite in this directory patched the three fetch helpers. ``_rebuild_card``
now makes **one** read (``GET /api/plan/{id}/status``) whose response
carries the same three sections plus the ``PlanStatus`` fields the header
renders from.

This module keeps the suites' existing three-section style working: the
status fields are derived the same way ``server._build_plan_status``
derives them, so a test that sets
``summary["state"]["verification"]["status"]`` still controls the header.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def build_payload(
    plan_id: str,
    summary: Optional[Dict[str, Any]] = None,
    execution: Optional[Dict[str, Any]] = None,
    verification: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """A ``GET /api/plan/{id}/status`` response built from fixtures.

    ``status`` overrides individual ``PlanStatus`` fields — a test that
    cares about liveness passes ``{"execution_in_flight": True}``.
    """
    state = (summary or {}).get("state") or {}
    verif = state.get("verification") or {}
    progress = verification or {}

    payload: Dict[str, Any] = {
        "plan_id": plan_id,
        "phase": state.get("current_phase") or "",
        "verification_status": (
            progress.get("verification_status") or verif.get("status") or ""
        ),
        "verification_round": (
            progress.get("verification_round") or verif.get("round") or 0
        ),
        "verification_max_rounds": (
            progress.get("max_rounds") or verif.get("max_rounds") or 0
        ),
        "verification_stop_reason": (
            progress.get("stop_reason") or verif.get("stop_reason")
        ),
        "tasks": dict((summary or {}).get("tasks") or {}),
        # These suites are about rendering, not liveness; the real values
        # come from the server. Default to "nothing running", which is
        # the conservative direction.
        "execution_in_flight": False,
        "verification_in_flight": False,
        "current_task": None,
        "next_task": None,
        "divergences": [],
    }
    payload.update(status or {})
    payload["summary"] = summary
    payload["execution"] = execution
    payload["verification"] = verification
    return payload


def status_from(
    summary: Optional[Dict[str, Any]] = None,
    execution: Optional[Dict[str, Any]] = None,
    verification: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    plan_id: str = "test-plan",
):
    """The ``PlanStatus`` the server would have sent for these fixtures.

    Goes through :func:`build_payload` and ``plan_status.from_payload``
    — the real wire path — so a test that renders a card from this is
    exercising the same rehydration the notifier does.
    """
    from plan_status import from_payload

    return from_payload(
        build_payload(plan_id, summary, execution, verification, status)
    )

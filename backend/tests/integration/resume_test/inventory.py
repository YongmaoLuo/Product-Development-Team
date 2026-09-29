"""Plan inventory helpers for the backend server restart safety gate.

These helpers implement the pre-flight inventory from the global rule
**"backend server restart safety — never assume a single plan owns the backend"**:

> Before ANY destructive the backend operation (restart, kill, stop, redeploy,
> port flip), do a full plan inventory first.

The module exposes three functions:

  * ``list_plans(client)`` — fetch the list of plans from the backend.
  * ``is_active(summary)`` — decide whether a plan summary describes an
    ACTIVE phase.
  * ``inventory_safe_to_restart(client, except_ids)`` — run the full
    inventory and report whether a restart is safe.

Only the ``ApiClient`` boundary is required; no FastAPI server needs to
be running in-process.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# ACTIVE phase set from the global rule in ~/.claude/CLAUDE.md and
# autonomous-coding/CLAUDE.md. A plan in any of these phases has
# in-flight LLM calls, active subprocesses, or un-persisted state that
# would be wiped by an backend server restart.
ACTIVE_PHASES = frozenset(
    (
        "executing",
        "verification_running",
        "interview",
        "prd_generation",
        "prd_review",
        "arch_generation",
        "arch_review",
        "test_generation",
        "test_review",
        "task_generation",
        "verification_repairing",
    )
)


def list_plans(client: Any) -> List[Dict[str, Any]]:
    """Return the list of plans known to the backend.

    Calls ``GET /api/plans`` through the supplied ``client`` and returns
    the ``plans`` array. The backend contract returns::

        {"plans": [{"id": "...", "requirement": "...", ...}]}

    If the response lacks a ``plans`` key, an empty list is returned so
    callers always get a list.
    """
    response = client.get("/api/plans")
    if not isinstance(response, dict):
        return []
    plans = response.get("plans", [])
    if not isinstance(plans, list):
        return []
    return [p for p in plans if isinstance(p, dict)]


def is_active(summary: Dict[str, Any]) -> bool:
    """Return ``True`` when ``summary`` describes an ACTIVE plan phase.

    The phase is read from ``summary["state"]["current_phase"]``. Missing
    or malformed state is treated as non-active (``False``) — the safety
    rule only trips when we have positive evidence of an ACTIVE phase.
    """
    if not isinstance(summary, dict):
        return False
    state = summary.get("state")
    if not isinstance(state, dict):
        return False
    phase = state.get("current_phase", "?")
    return phase in ACTIVE_PHASES


def inventory_safe_to_restart(
    client: Any,
    except_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Run the full plan inventory and report whether a restart is safe.

    Parameters
    ----------
    client:
        An ``ApiClient``-like object with ``get(path)`` and
        ``get_plan_summary(plan_id)`` methods.
    except_ids:
        Plan IDs that are allowed to be ACTIVE without making the
        inventory unsafe. These are typically the plan currently under
        test / about to be restarted.

    Returns
    -------
    A dict with three keys:

      * ``safe`` — ``True`` iff no non-excepted plan is ACTIVE.
      * ``active_plans`` — list of plan IDs that are ACTIVE and NOT in
        ``except_ids``.
      * ``all_plans`` — list of inventory rows for every plan returned
        by the backend. Each row has ``plan_id``, ``phase``, and
        ``active`` keys. A plan whose summary could not be fetched has
        ``phase == "unknown"`` and ``active == False``.

    Boundary handling:

      * An empty plan list → ``safe`` is ``True`` (nothing is running).
      * A single plan summary failure → that plan is marked ``unknown``
        and does not block the overall result.
      * ``except_ids`` may be any iterable; it is normalised to a ``set``.
    """
    except_set = set(except_ids) if except_ids else set()
    plans = list_plans(client)

    all_plans: List[Dict[str, Any]] = []
    active_plans: List[str] = []

    for plan in plans:
        plan_id = plan.get("id", "?")
        try:
            summary = client.get_plan_summary(plan_id)
            active = is_active(summary)
            phase = (
                summary.get("state", {}).get("current_phase", "unknown")
                if isinstance(summary, dict)
                else "unknown"
            )
        except Exception:
            # Single plan summary failure → mark unknown, don't block overall.
            active = False
            phase = "unknown"

        all_plans.append(
            {
                "plan_id": plan_id,
                "phase": phase,
                "active": active,
            }
        )
        if active and plan_id not in except_set:
            active_plans.append(plan_id)

    return {
        "safe": len(active_plans) == 0,
        "active_plans": active_plans,
        "all_plans": all_plans,
    }

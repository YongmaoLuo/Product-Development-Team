"""The three progress projections carry the plan's pending decisions.

Background
----------
A human-in-the-loop decision is asked by one actor and answered by
another.  The question that answers "somebody has to look at this" is
"what is still outstanding?" — and until now the frontend had no way
to ask it: neither the plan summary nor the two progress projections
carried a count of the decisions waiting for a human.

So three projections were extended with the same block:

    "pending_decisions": {
        "count": <int>,
        "items": [
            {
                "decision_id": str,
                "question": str,
                "source": str,
                "created_at": str,
            },
            ...
        ],
    }

The three rules the block obeys:

1. **Only pending decisions are listed.**  A decision that has been
   answered, defaulted or refused is settled — it is history, not
   work.  A projection that listed them would tell the operator to go
   and answer a question that has already been answered.
2. **The field is always present.**  A plan with no decisions reports
   ``{"count": 0, "items": []}`` rather than omitting the key, so a
   consumer never has to distinguish "there are no decisions" from
   "this endpoint does not know about decisions".
3. **A decision table that cannot be read degrades to empty.**  These
   projections answer "how is this plan doing"; a broken decision
   table must not turn that into a 500.

What this file pins:

1. ``test_summary_projection`` — the plan summary reports the pending
   decisions, in the documented shape.
2. ``test_progress_projection`` — the execution and verification
   progress projections carry the same block, with the same values.
3. ``test_empty_shape`` — a plan with no decisions still reports the
   block, empty.
4. ``test_only_pending_decisions_are_listed`` — settled decisions are
   not listed.
5. ``test_unreadable_decision_table_degrades_to_empty`` — a decision
   table that cannot be read is reported as "no decisions", not as a
   failed request.

Test command::

    pytest backend/tests/api/test_pending_decisions_projection.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from config_paths import resolve_state_db_path  # noqa: E402
from hitl.models import Decision, DecisionRequest, DecisionStatus  # noqa: E402
from hitl.store import HitlStore  # noqa: E402


client = TestClient(server.app)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _plan_dir(plan_id: str) -> Path:
    """Create a minimal plan directory and return it.

    The projections all read the plan's own directory first, so it has
    to exist; ``tasks.json`` and ``plan_state.json`` are the two files
    they expect to find.
    """
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": "ready",
            "completed_phases": [],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {},
        }),
        encoding="utf-8",
    )
    return plan_dir


def _insert_decision(
    plan_id: str,
    *,
    decision_id: str,
    status: str = DecisionStatus.PENDING.value,
    source: str = "execution",
    question: str = "Retry the failing task?",
) -> Decision:
    """Store one decision for ``plan_id`` in the given status.

    The row is written through the HITL store — the same table the
    projections read — so the test exercises the real read path rather
    than a stub of it.
    """
    request = DecisionRequest(
        plan_id=plan_id,
        source=source,
        task_id="T-1",
        question=question,
        options=["accept", "reject"],
        default_option="accept",
        mode="auto",
        timeout_sec=1800,
    )
    decision = Decision.from_request(request, decision_id=decision_id)
    decision.status = status
    store = HitlStore(resolve_state_db_path())
    try:
        store.insert(decision)
    finally:
        store.close()
    return decision


def _seed_execution(plan_id: str, plan_dir: Path) -> Path:
    """Give the plan a project directory, so ``/progress`` can be served.

    ``/api/execution/{plan_id}/progress`` refuses a plan whose
    ``project_dir`` is unknown (404) — the directory is resolved from
    the in-memory execution record first, then from the
    ``plan_execution`` row.
    """
    project_dir = plan_dir / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    server._execution_state[plan_id] = {
        "status": "running",
        "project_dir": str(project_dir),
    }
    return project_dir


def _seed_verification(plan_id: str) -> None:
    """Write the progress state the verification projection reads.

    ``/api/verification/{plan_id}/progress`` answers 404 ("verification
    not started") until the executor has written a progress state, so
    a plan that never started verification has no progress payload to
    carry the decisions block.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    db_path = server._state_db_path(None)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        migrate(conn)
        VerificationRepository(conn).insert(
            plan_id,
            verification_status="running",
            progress_state={
                "plan_id": plan_id,
                "current_vp": None,
                "completed_vps": [],
                "failed_vps": [],
                "skipped_vps": [],
                "updated_at": "2026-10-22T10:00:00.000000Z",
            },
        )
    finally:
        conn.close()


def _block(body: Dict[str, Any]) -> Dict[str, Any]:
    """The ``pending_decisions`` block of a response body."""
    assert "pending_decisions" in body, (
        "every projection must carry the block, even when it is empty; "
        f"got keys {sorted(body)!r}"
    )
    return body["pending_decisions"]


# ---------------------------------------------------------------------------
# 1. The plan summary
# ---------------------------------------------------------------------------


def test_summary_projection(isolated_plans_dir) -> None:
    """The plan summary reports the decisions that wait for a human.

    The block is the answer to "does this plan need me?", so it carries
    the count *and* the questions themselves — the summary is what an
    agent reads to decide whether it may continue.
    """
    plan_id = "20261022-pending-summary"
    _plan_dir(plan_id)
    decision = _insert_decision(
        plan_id,
        decision_id="d1",
        source="execution",
        question="Retry the failing task?",
    )

    resp = client.get(f"/api/plan/{plan_id}/summary")
    assert resp.status_code == 200, resp.text

    block = _block(resp.json())
    assert block["count"] == 1, f"expected one pending decision; got {block!r}"
    assert block["items"] == [{
        "decision_id": "d1",
        "question": "Retry the failing task?",
        "source": "execution",
        "created_at": decision.created_at,
    }], f"the item must carry the question as asked; got {block!r}"


# ---------------------------------------------------------------------------
# 2. The two progress projections
# ---------------------------------------------------------------------------


def test_progress_projection(isolated_plans_dir) -> None:
    """Both progress projections carry the same block as the summary.

    The frontend polls the execution and the verification progress
    endpoints; a plan waiting for a human has to look the same in both
    of them and in the summary, or the operator has to know which
    endpoint to poll to find out that they are the bottleneck.
    """
    plan_id = "20261022-pending-progress"
    plan_dir = _plan_dir(plan_id)
    _seed_execution(plan_id, plan_dir)
    _seed_verification(plan_id)
    decision = _insert_decision(
        plan_id,
        decision_id="d1",
        source="verification",
        question="Retry the failing task?",
    )

    exec_resp = client.get(f"/api/execution/{plan_id}/progress")
    assert exec_resp.status_code == 200, exec_resp.text
    verif_resp = client.get(f"/api/verification/{plan_id}/progress")
    assert verif_resp.status_code == 200, verif_resp.text

    expected = {
        "count": 1,
        "items": [{
            "decision_id": "d1",
            "question": "Retry the failing task?",
            "source": "verification",
            "created_at": decision.created_at,
        }],
    }
    for name, body in (
        ("execution", exec_resp.json()),
        ("verification", verif_resp.json()),
    ):
        block = _block(body)
        assert block == expected, (
            f"the {name} progress projection must carry the same block as "
            f"the summary; got {block!r}"
        )


# ---------------------------------------------------------------------------
# 3. The empty shape
# ---------------------------------------------------------------------------


def test_empty_shape(isolated_plans_dir) -> None:
    """A plan with no decisions still reports the block.

    The field must be there even when it is empty: a consumer that has
    to special-case a missing key is a consumer that will eventually
    forget to.
    """
    plan_id = "20261022-pending-empty"
    plan_dir = _plan_dir(plan_id)
    _seed_execution(plan_id, plan_dir)
    _seed_verification(plan_id)

    for url in (
        f"/api/plan/{plan_id}/summary",
        f"/api/execution/{plan_id}/progress",
        f"/api/verification/{plan_id}/progress",
    ):
        resp = client.get(url)
        assert resp.status_code == 200, resp.text
        block = _block(resp.json())
        assert block == {"count": 0, "items": []}, (
            f"{url} must report an empty block for a plan with no "
            f"decisions; got {block!r}"
        )


# ---------------------------------------------------------------------------
# 4. Only the pending ones
# ---------------------------------------------------------------------------


def test_only_pending_decisions_are_listed(isolated_plans_dir) -> None:
    """A settled decision is history, not work.

    Answered, defaulted and unroutable decisions have all reached a
    final state.  Listing them would tell the operator to go and answer
    a question nobody is waiting for any more.
    """
    plan_id = "20261022-pending-only"
    _plan_dir(plan_id)
    _insert_decision(plan_id, decision_id="d-pending")
    _insert_decision(plan_id, decision_id="d-answered", status="answered")
    _insert_decision(plan_id, decision_id="d-defaulted", status="defaulted")

    resp = client.get(f"/api/plan/{plan_id}/summary")
    assert resp.status_code == 200, resp.text

    block = _block(resp.json())
    assert block["count"] == 1, (
        f"only the pending decision may be counted; got {block!r}"
    )
    assert [item["decision_id"] for item in block["items"]] == ["d-pending"], (
        f"the settled decisions must not be listed; got {block!r}"
    )


# ---------------------------------------------------------------------------
# 5. A decision table that cannot be read
# ---------------------------------------------------------------------------


def test_unreadable_decision_table_degrades_to_empty(
    isolated_plans_dir, monkeypatch
) -> None:
    """A decision table that cannot be read is not a failed request.

    The projection answers "how is this plan doing".  A decision table
    that is locked, corrupt or missing must not turn that question
    into a 500 — the honest answer is "no decisions are outstanding",
    and the log says what actually happened.
    """
    import hitl.service

    def _boom(*args: Any, **kwargs: Any):
        raise RuntimeError("the decision table cannot be read")

    monkeypatch.setattr(hitl.service, "list_decisions", _boom)

    plan_id = "20261022-pending-unreadable"
    _plan_dir(plan_id)

    resp = client.get(f"/api/plan/{plan_id}/summary")
    assert resp.status_code == 200, resp.text
    assert _block(resp.json()) == {"count": 0, "items": []}

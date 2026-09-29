"""
TDD tests for the full-validation fix in POST /api/review/{plan_id}/review/item/{idx}.

Bug fixed: ``review_item`` transitioned the plan to ``prd_approved`` as soon as
``pending == 0`` in ``review.json`` — but ``total_items`` was read from
``review.json`` (the count of *reviewed* items, not the count of *decision
points* in ``prd.json``).  So a partial review (2 of 8 decision points
accepted) silently advanced the plan to ``prd_approved`` and skipped the rest
of the review loop.

The corrected behaviour (synced with ``plan_state._is_prd_review_complete``):

- 8/8 accept → current_phase == "prd_approved" (full accept, transition)
- 2/8 accept → response carries ``status="warning"`` and
  current_phase stays at "prd_review" (no transition)
- action=revise → returns early (no full validation runs)
- action=skip → no transition is triggered (skip is recorded, but the
  decision point is not "accepted", so ``prd_approved`` does not fire
  unless every other decision point has been explicitly accepted)
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app


client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_plans_dir(monkeypatch, tmp_path):
    """Redirect server.PLANS_DIR to a tmp dir for isolation."""
    plans_root = tmp_path / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("server.PLANS_DIR", plans_root)
    yield plans_root


def _make_decision_points(n: int) -> list:
    """Build n synthetic decision points with sequential indices."""
    return [
        {
            "index": i,
            "title": f"决策点 {i}",
            "context": "context",
            "problem": "problem",
            "evidence": "evidence",
            "action": "action",
            "impact": "impact",
            "alternatives": [],
        }
        for i in range(n)
    ]


def _setup_plan_with_prd(
    plans_root: Path,
    plan_id: str,
    n_decision_points: int,
    current_phase: str = "prd_review",
) -> Path:
    """Materialise a plan with prd.json containing N decision points and
    a plan_state.json pinned to ``current_phase``.
    """
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    dps = _make_decision_points(n_decision_points)
    (plan_dir / "prd.json").write_text(
        json.dumps(
            {
                "title": "test plan",
                "overview": "test",
                "decision_points": dps,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": current_phase,
                "completed_phases": [],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {"arch_enabled": False, "test_enabled": False},
                "verification": {
                    "status": "pending",
                    "round": 0,
                    "max_rounds": 3,
                    "stop_reason": None,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_dir


def _accept_all_n(plans_root: Path, plan_id: str, n: int) -> None:
    """Drive the endpoint for all N decision points with action=accept.

    Done by sending individual POSTs so the server-side counter actually
    runs through the real transition code path.
    """
    for i in range(n):
        resp = client.post(
            f"/api/review/{plan_id}/review/item/{i}",
            json={"action": "accept"},
        )
        assert resp.status_code == 200, (
            f"accept {i} returned {resp.status_code} body={resp.text!r}"
        )


def _read_current_phase(plans_root: Path, plan_id: str) -> str:
    """Read current_phase via PlanState (plan_routing SQLite row is canonical).

    2026-09-13 port: the plan_state.json mirror write was removed; the
    file no longer reflects transitions, so raw file reads would go stale.
    """
    from plan_state import PlanState

    return PlanState(plans_root / plan_id).get_current_phase()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReviewItemFullAcceptedTransitions:
    """8/8 accept → current_phase=prd_approved."""

    def test_review_item_full_accepted_transitions(self, isolated_plans_dir):
        plan_id = "test-full-accept"
        _setup_plan_with_prd(isolated_plans_dir, plan_id, n_decision_points=8)

        _accept_all_n(isolated_plans_dir, plan_id, 8)

        current = _read_current_phase(isolated_plans_dir, plan_id)
        assert current == "prd_approved", (
            f"after 8/8 accept, current_phase must be prd_approved, got {current!r}"
        )


class TestReviewItemPartialReturnsWarning:
    """2/8 accept → 200 + status=warning + current_phase=prd_review."""

    def test_review_item_partial_returns_warning(self, isolated_plans_dir):
        plan_id = "test-partial-accept"
        _setup_plan_with_prd(isolated_plans_dir, plan_id, n_decision_points=8)

        # Accept only the first 2 of 8
        for i in range(2):
            resp = client.post(
                f"/api/review/{plan_id}/review/item/{i}",
                json={"action": "accept"},
            )
            assert resp.status_code == 200
            # Each individual accept on a still-partial review must
            # already surface a warning (so the client can render the
            # partial state) — the LAST accept (i=1) is the
            # deterministic 2/8 point.
            if i == 1:
                data = resp.json()
                assert data.get("status") == "warning", (
                    f"2/8 accept: response must include status=warning, "
                    f"got {data!r}"
                )
                assert data.get("current_phase") == "prd_review", (
                    f"2/8 accept: current_phase must stay at prd_review, "
                    f"got {data.get('current_phase')!r}"
                )

        # Final state check — plan must NOT have transitioned
        current = _read_current_phase(isolated_plans_dir, plan_id)
        assert current == "prd_review", (
            f"after 2/8 accept, current_phase must remain prd_review, got {current!r}"
        )


class TestReviewItemReviseNoValidation:
    """action=revise → no full validation, returns early from revise branch."""

    def test_review_item_revise_no_validation(self, isolated_plans_dir, monkeypatch):
        plan_id = "test-revise"
        _setup_plan_with_prd(isolated_plans_dir, plan_id, n_decision_points=8)

        # Stub PRDReviewer.submit_action to assert it is invoked and to
        # return early without persisting review state.  This way, if
        # the endpoint somehow ALSO ran the full-validation block after
        # revise, the test would catch the side-effect on plan_state.
        from prd_review import PRDReviewer

        def _fake_submit_action(self, item_index, action, question=""):
            return {"status": "revised", "index": item_index, "question": question}

        monkeypatch.setattr(PRDReviewer, "submit_action", _fake_submit_action)

        # Track how many times the transition path is reached.  We
        # instrument by counting plan_state.json writes via mtime.
        state_file = isolated_plans_dir / plan_id / "plan_state.json"
        mtime_before = state_file.stat().st_mtime_ns

        resp = client.post(
            f"/api/review/{plan_id}/review/item/0",
            json={"action": "revise", "question": "simplify"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # revise branch returns the reviewer's result; status key is
        # whatever the stub returns ("revised").
        assert data.get("status") == "revised"

        # plan_state.json must not have been touched by the full-validation
        # block (i.e. no transition_to was attempted).
        mtime_after = state_file.stat().st_mtime_ns
        assert mtime_after == mtime_before, (
            "action=revise must not write plan_state.json (full validation skipped)"
        )

        # And current_phase is unchanged.
        current = _read_current_phase(isolated_plans_dir, plan_id)
        assert current == "prd_review"


class TestReviewItemSkipNoTransition:
    """action=skip (1/8 skip) → no transition triggered."""

    def test_review_item_skip_no_transition(self, isolated_plans_dir):
        plan_id = "test-skip"
        _setup_plan_with_prd(isolated_plans_dir, plan_id, n_decision_points=8)

        # Skip only the first of 8 decision points
        resp = client.post(
            f"/api/review/{plan_id}/review/item/0",
            json={"action": "skip"},
        )
        assert resp.status_code == 200

        # The skip itself was recorded in review.json — the per-item
        # status must reflect "skipped" regardless of any plan-level
        # validation message that might also be returned.
        review_file = isolated_plans_dir / plan_id / "review.json"
        review = json.loads(review_file.read_text(encoding="utf-8"))
        items_by_index = {i["index"]: i for i in review.get("items", [])}
        assert items_by_index.get(0, {}).get("status") == "skipped", (
            f"action=skip: review.json item 0 must be 'skipped', "
            f"got {items_by_index.get(0)!r}"
        )

        # The plan must NOT have transitioned — only 1 of 8 has even been
        # touched, and skipped ≠ accepted.
        current = _read_current_phase(isolated_plans_dir, plan_id)
        assert current == "prd_review", (
            f"action=skip (1/8): current_phase must stay at prd_review, "
            f"got {current!r}"
        )

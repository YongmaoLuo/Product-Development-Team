"""The guarded dismissal of a framework-fix entry.

What this file pins
-------------------
* ``POST /api/plan/{plan_id}/framework-fix/dismiss`` dismisses an entry
  nobody is working on any more — 200, and the plan's framework-fix
  block says so;
* it refuses while the defect is still open — 409, and the entry stays
  exactly as it was (nothing is deleted);
* it refuses while a merge lease is still held for the entry — 409;
* an unknown plan is 404, and a dismissal without a reason is 400
  (with or without a body at all);
* dismissing twice is idempotent: 200 both times, and the audit chain
  carries **one** event, not two;
* a plan that exists but has no framework-fix entry can still be
  dismissed — the audit chain is the record;
* the dismissal is additive: the other writers' facts in the block
  survive it.

Why the guard exists
--------------------
The framework-fix partition of the scheduler page offers a delete /
ignore action.  Dismissing an entry that still has an open repair plan
— or a repair PR sitting in a repository's merge slot — would hide
work that is in flight, and every plan waiting on that repair would
stay blocked with nobody looking after them.  So the refusal is the
default and the dismissal is the exception.

The data is real
----------------
The registry rows, the merge lease and the plan's routing row live in
the same migrated SQLite file the endpoint reads (the ``state_db``
fixture), and the block is written through the production writer
(``PlanState.enter_block``).  Nothing here is a double: a guard that
only holds against a fake database is not a guard.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from server import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def plans_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A plans tree of this test's own."""
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("server.PLANS_DIR", root)
    return root


def _seed_plan(plans_root: Path, plan_id: str) -> Path:
    """A plan directory in the tree."""
    plan_dir = plans_root / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text("{}", encoding="utf-8")
    return plan_dir


def _registry():
    """The registry, pointed at the test's own state database."""
    from framework_fix.registry import FrameworkFixRegistry

    return FrameworkFixRegistry()


def _register_defect(registry) -> str:
    """Register a defect the way the lane does — status ``open``."""
    return registry.register_hit(
        {
            "plan_id": "biz-plan",
            "vp_id": "VP-1",
            "framework_files": ["backend/framework_fix/registry.py"],
            "error_class": "AssertionError",
            "first_error_line": "AssertionError: expected 1, got 2",
            "repo": "fixture-repo",
        }
    )


def _mark_fixed(fingerprint: str) -> None:
    """The defect was repaired and merged — the registry row says so."""
    from config_paths import resolve_state_db_path

    conn = sqlite3.connect(str(resolve_state_db_path()))
    try:
        conn.execute(
            "UPDATE framework_fix_registry SET status = 'fixed' "
            "WHERE fingerprint = ?",
            (fingerprint,),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_block(plan_dir: Path, plan_id: str, *, fingerprint: str) -> None:
    """Give the plan a framework-fix block, through the production writer."""
    from plan_state import PlanState

    PlanState(plan_dir).enter_block(
        plan_id, fingerprint=fingerprint, fix_plan_id="fx-fix-0001"
    )


def _framework_fix_block(plan_id: str) -> Dict[str, Any]:
    """The plan's framework-fix block, as stored."""
    from config_paths import resolve_state_db_path

    conn = sqlite3.connect(str(resolve_state_db_path()))
    try:
        row = conn.execute(
            "SELECT framework_fix FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row[0] is None:
        return {}
    block = json.loads(row[0])
    return block if isinstance(block, dict) else {}


def _events(fingerprint: str) -> List[Dict[str, Any]]:
    """The audit chain, oldest first."""
    from config_paths import resolve_state_db_path

    conn = sqlite3.connect(str(resolve_state_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_type, payload_json, fix_plan_id FROM framework_fix_event "
            "WHERE fingerprint = ? ORDER BY created_at, event_id",
            (fingerprint,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _dismissal_events() -> List[Dict[str, Any]]:
    """Every dismissal on the audit chain, oldest first."""
    from config_paths import resolve_state_db_path

    conn = sqlite3.connect(str(resolve_state_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_type, payload_json, fingerprint FROM framework_fix_event "
            "WHERE event_type = 'framework_fix_dismissed' "
            "ORDER BY created_at, event_id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _seed_extra_block_key(plan_id: str, key: str, value: Any) -> None:
    """Another writer's fact in the plan's block.

    The block is shared: the read model reads ``status`` /
    ``paused_by`` / ``merge_ready_at`` out of it, and the classifier
    keeps ``unclassified_rounds`` in it.  Writing one of those keys
    directly is the only way to seed them — the production writers do
    not offer a way to plant somebody else's fact.
    """
    from config_paths import resolve_state_db_path

    conn = sqlite3.connect(str(resolve_state_db_path()))
    try:
        row = conn.execute(
            "SELECT framework_fix FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        block = json.loads(row[0]) if row and row[0] else {}
        block[key] = value
        conn.execute(
            "UPDATE plan_routing SET framework_fix = ? WHERE plan_id = ?",
            (json.dumps(block), plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _dismiss(plan_id: str, reason: Any = "noise"):
    return client.post(
        f"/api/plan/{plan_id}/framework-fix/dismiss",
        json={"reason": reason},
    )


# ---------------------------------------------------------------------------
# 1. The dismissal itself
# ---------------------------------------------------------------------------
def test_dismiss_ok(plans_root: Path, state_db: Path) -> None:
    """No live repair any more → 200, and the entry records the dismissal."""
    registry = _registry()
    fingerprint = _register_defect(registry)
    _mark_fixed(fingerprint)

    plan_id = "fx-fix-0001"
    plan_dir = _seed_plan(plans_root, plan_id)
    _seed_block(plan_dir, plan_id, fingerprint=fingerprint)

    response = _dismiss(plan_id)

    assert response.status_code == 200
    assert response.json() == {"dismissed": True}

    block = _framework_fix_block(plan_id)
    assert block["dismissed"] is True
    assert block["dismiss_reason"] == "noise"
    # The hold is released: the plan is not waiting on this defect any more.
    assert block["hold"] is False
    # The block keeps the facts the read model needs.
    assert block["fingerprint"] == fingerprint

    # The audit chain carries the dismissal.
    dismissals = [
        event
        for event in _events(fingerprint)
        if event["event_type"] == "framework_fix_dismissed"
    ]
    assert len(dismissals) == 1
    payload = json.loads(dismissals[0]["payload_json"])
    assert payload["reason"] == "noise"
    assert payload["plan_id"] == plan_id


# ---------------------------------------------------------------------------
# 2. The guard: an open defect
# ---------------------------------------------------------------------------
def test_dismiss_active_409(plans_root: Path, state_db: Path) -> None:
    """The defect is still being repaired → 409, and nothing is dismissed."""
    registry = _registry()
    fingerprint = _register_defect(registry)  # status stays ``open``

    plan_id = "fx-fix-0002"
    plan_dir = _seed_plan(plans_root, plan_id)
    _seed_block(plan_dir, plan_id, fingerprint=fingerprint)

    response = _dismiss(plan_id)

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "framework_fix_active"
    assert body["fingerprint"] == fingerprint

    # Nothing was dismissed: the block is untouched.
    block = _framework_fix_block(plan_id)
    assert "dismissed" not in block
    assert block["hold"] is True
    assert not [
        event
        for event in _events(fingerprint)
        if event["event_type"] == "framework_fix_dismissed"
    ]


# ---------------------------------------------------------------------------
# 3. The guard: an active merge lease
# ---------------------------------------------------------------------------
def test_dismiss_active_lease_409(plans_root: Path, state_db: Path) -> None:
    """A repair PR still holds the repository's merge slot → 409."""
    registry = _registry()
    fingerprint = _register_defect(registry)
    _mark_fixed(fingerprint)
    assert registry.acquire_merge_lease("fixture-repo", "fx-fix-0003") is True

    plan_id = "fx-fix-0003"
    plan_dir = _seed_plan(plans_root, plan_id)
    _seed_block(plan_dir, plan_id, fingerprint=fingerprint)

    response = _dismiss(plan_id)

    assert response.status_code == 409
    assert response.json()["error"] == "framework_fix_active"
    assert "dismissed" not in _framework_fix_block(plan_id)


# ---------------------------------------------------------------------------
# 4. Unknown plan
# ---------------------------------------------------------------------------
def test_dismiss_404(plans_root: Path, state_db: Path) -> None:
    """A plan nobody knows about → 404."""
    response = _dismiss("no-such-plan")

    assert response.status_code == 404
    assert response.json()["error"] == "plan_not_found"


# ---------------------------------------------------------------------------
# 5. Idempotence
# ---------------------------------------------------------------------------
def test_dismiss_idempotent(plans_root: Path, state_db: Path) -> None:
    """Dismissing twice is the same as dismissing once."""
    registry = _registry()
    fingerprint = _register_defect(registry)
    _mark_fixed(fingerprint)

    plan_id = "fx-fix-0004"
    plan_dir = _seed_plan(plans_root, plan_id)
    _seed_block(plan_dir, plan_id, fingerprint=fingerprint)

    first = _dismiss(plan_id)
    second = _dismiss(plan_id)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json() == {"dismissed": True}

    dismissals = [
        event
        for event in _events(fingerprint)
        if event["event_type"] == "framework_fix_dismissed"
    ]
    assert len(dismissals) == 1


# ---------------------------------------------------------------------------
# 6. The reason is mandatory
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("reason", [None, "", "   ", 7])
def test_dismiss_missing_reason_400(
    plans_root: Path, state_db: Path, reason: Any
) -> None:
    """No reason → 400: a dismissal nobody can explain is not a dismissal."""
    plan_id = "fx-fix-0005"
    plan_dir = _seed_plan(plans_root, plan_id)

    if reason is None:
        response = client.post(
            f"/api/plan/{plan_id}/framework-fix/dismiss", json={}
        )
    else:
        response = _dismiss(plan_id, reason)

    assert response.status_code == 400
    assert response.json()["error"] == "missing_reason"


# ---------------------------------------------------------------------------
# 7. No body at all
# ---------------------------------------------------------------------------
def test_dismiss_without_body_400(
    plans_root: Path, state_db: Path
) -> None:
    """A request with no body → 400, not a server error."""
    plan_id = "fx-fix-0006"
    _seed_plan(plans_root, plan_id)

    response = client.post(f"/api/plan/{plan_id}/framework-fix/dismiss")

    assert response.status_code == 400
    assert response.json()["error"] == "missing_reason"


# ---------------------------------------------------------------------------
# 8. A plan with no framework-fix entry
# ---------------------------------------------------------------------------
def test_dismiss_plan_without_entry_200(
    plans_root: Path, state_db: Path
) -> None:
    """The plan exists but has no entry → 200, and the chain records it."""
    plan_id = "fx-fix-0007"
    _seed_plan(plans_root, plan_id)

    response = _dismiss(plan_id)

    assert response.status_code == 200
    assert response.json() == {"dismissed": True}

    # The audit chain is the record here: this plan has no routing row,
    # so there is no block to write the dismissal into.
    events = _dismissal_events()
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["plan_id"] == plan_id
    assert payload["reason"] == "noise"


# ---------------------------------------------------------------------------
# 9. The block keeps the other writers' facts
# ---------------------------------------------------------------------------
def test_dismiss_preserves_other_block_keys(
    plans_root: Path, state_db: Path
) -> None:
    """The dismissal adds its facts to the block; it does not wipe it."""
    registry = _registry()
    fingerprint = _register_defect(registry)
    _mark_fixed(fingerprint)

    plan_id = "fx-fix-0008"
    plan_dir = _seed_plan(plans_root, plan_id)
    _seed_block(plan_dir, plan_id, fingerprint=fingerprint)
    _seed_extra_block_key(plan_id, "unclassified_rounds", 3)

    response = _dismiss(plan_id)

    assert response.status_code == 200
    block = _framework_fix_block(plan_id)
    # Somebody else's fact survives.
    assert block["unclassified_rounds"] == 3
    # And the entry's own facts survive.
    assert block["fingerprint"] == fingerprint
    assert block["dismissed"] is True
    assert block["hold"] is False

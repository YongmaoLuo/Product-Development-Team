"""The plan listing must treat ``archived`` as a terminal phase.

What this pins
--------------
``GET /api/plans`` drops terminal plans by default — they are
historical artefacts that no longer need a sidebar entry — and shows
them when the caller asks for them (``?include_terminal=true``).

``archived`` is one of those terminal phases: a plan we archived on
purpose, which is never going to run again.  So:

1. the default listing does **not** contain it;
2. ``?include_terminal=true`` does contain it, and it says
   ``current_phase == "archived"`` — the phase is the answer, not a
   guess from which documents happen to be on disk.

The list is read through the production route, not through the
repository, because the filter lives in the route.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server import app, invalidate_plans_cache

pytestmark = pytest.mark.unit

client = TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_listing(monkeypatch, tmp_path):
    """Point the listing at this test's own plans tree."""
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    (tmp_path / "plans").mkdir(parents=True, exist_ok=True)
    invalidate_plans_cache()
    yield
    invalidate_plans_cache()


def _seed_archived_plan(plan_id: str) -> None:
    """Materialise a plan whose routing row says ``archived``.

    The plan directory carries no execution record — an archived plan
    never ran — so the listing's phase comes from the routing row alone.
    """
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": "archived",
            "completed_phases": ["interview", "stopped"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {},
        }),
        encoding="utf-8",
    )


def _set_routing_phase(plan_id: str, phase: str) -> None:
    """Write the routing row the state layer would have written."""
    from server import _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(_state_db_path(None))
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        if repo.find(plan_id) is None:
            repo.insert(plan_id, phase)
        else:
            repo.write_plan_state(
                plan_id,
                phase=phase,
                completed_phases=[],
                review_rounds={},
                flags={},
                verification={},
                last_updated=None,
            )
    finally:
        conn.close()


def _list(include_terminal: bool):
    invalidate_plans_cache()
    if include_terminal:
        response = client.get("/api/plans?include_terminal=true")
    else:
        response = client.get("/api/plans")
    assert response.status_code == 200, response.text
    return {entry["id"]: entry for entry in response.json()}


def test_archived_plan_is_not_in_the_default_listing():
    plan_id = "archive-listing-hidden"
    _seed_archived_plan(plan_id)
    _set_routing_phase(plan_id, "archived")

    entries = _list(include_terminal=False)

    assert plan_id not in entries, (
        "an archived plan is terminal — the default listing must not "
        f"show it, but it did: {entries.get(plan_id)!r}"
    )


def test_archived_plan_is_visible_with_include_terminal():
    plan_id = "archive-listing-visible"
    _seed_archived_plan(plan_id)
    _set_routing_phase(plan_id, "archived")

    entries = _list(include_terminal=True)

    assert plan_id in entries, (
        f"{plan_id} is missing from the full listing: {entries!r}"
    )
    assert entries[plan_id]["current_phase"] == "archived", entries[plan_id]

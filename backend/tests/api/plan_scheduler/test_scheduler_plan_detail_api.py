"""``GET /api/scheduler/plan/{plan_id}/detail`` — one plan's own progress.

What is pinned here
-------------------
``GET /api/scheduler/state`` says **which** plans are queued and which
are running.  When the operator opens one of them, the panel asks a
second question — "what is this plan actually doing right now?" — and
that answer already exists: it is the Feishu progress card's body, the
same three sections (where the plan is, how the tasks are counted, what
is running this moment), worded by the same builders.

So the detail endpoint does not grow a second phrasing of the card: it
asks the card's builder for the lines and hands them on.  Three things
follow, and they are what is pinned here:

1. **The lines are the card's.**  The endpoint's ``lines`` are the ones
   ``cards.build_progress_lines`` produces for the same plan status — the
   same builder, so the panel and the card cannot disagree about what a
   plan is doing.  A second phrasing written here would be a second
   answer to the same question.
2. **An unknown plan is a 404.**  The plan is not there, and the answer
   says so in the shape the panel expects: ``{"detail": "plan not
   found"}``.  An empty detail would be a panel that renders an empty
   box for a plan that never existed.
3. **Reading is reading.**  The endpoint writes nothing: the routing
   row's ``current_phase`` is the same before the call and after it.
   Opening a plan must not move the plan.

The request carries ``X-PDT-Request: 1`` — the guard every ``/api/*``
request has to satisfy, and the header the bundled UI sends.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: it is the application, and it is what puts the
# routers on the app.  ``routes.plans`` is imported **after** it — the
# two modules import each other, and the application's own import order
# is the one that resolves the cycle.
import server  # noqa: E402
from notifications import cards  # noqa: E402
from routes import plans as routes_plans  # noqa: E402

client = TestClient(server.app)

#: The endpoint under test.
DETAIL_PATH = "/api/scheduler/plan/{plan_id}/detail"

#: The header every ``/api/*`` request carries (``request_guard``).
GUARD_HEADERS = {"X-PDT-Request": "1"}


@pytest.fixture(autouse=True)
def _isolated_plans_dir(monkeypatch, tmp_path):
    """Point ``PLANS_DIR`` at a tmp tree, and keep the listing cache cold."""
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    (tmp_path / "plans").mkdir(parents=True, exist_ok=True)
    server.invalidate_plans_cache()
    yield
    server.invalidate_plans_cache()


@pytest.fixture
def seed_plan():
    """Seed a plan directory (and its routing row) — and clean it up after.

    The routing table lives in the session-level hermetic ``state.db``,
    so a row written here outlives the test unless it is deleted again.
    The database path is captured at **setup** time: a test that seeds
    no routing row never migrates that database, and the teardown must
    not open a file that is not there.
    """
    from server import _state_db_path

    db_path = _state_db_path(None)
    seeded: List[str] = []

    def _seed(
        plan_id: str,
        *,
        files: Optional[Dict[str, str]] = None,
        phase: Optional[str] = None,
    ) -> str:
        from server import PLANS_DIR

        plan_dir = PLANS_DIR / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)
        for name, content in (files or {}).items():
            (plan_dir / name).write_text(content, encoding="utf-8")
        if phase:
            from state_machine.db.connection import open as open_db
            from state_machine.db.schema import migrate
            from state_machine.repositories.routing_repository import (
                RoutingRepository,
            )

            conn = open_db(db_path)
            try:
                migrate(conn)
                repo = RoutingRepository(conn)
                if repo.find(plan_id) is None:
                    repo.insert(plan_id, phase)
            finally:
                conn.close()
        seeded.append(plan_id)
        return plan_id

    yield _seed

    if seeded:
        from state_machine.db.connection import open as open_db
        from state_machine.db.schema import migrate

        conn = open_db(db_path)
        try:
            # The schema may not exist yet: a test that seeds no routing
            # row never migrated this database.
            migrate(conn)
            for plan_id in seeded:
                conn.execute(
                    "DELETE FROM plan_routing WHERE plan_id = ?", (plan_id,)
                )
            conn.commit()
        finally:
            conn.close()


def _detail(plan_id: str):
    """Ask for one plan's detail."""
    return client.get(
        DETAIL_PATH.format(plan_id=plan_id), headers=dict(GUARD_HEADERS)
    )


def _routing_phase(plan_id: str) -> Optional[str]:
    """The phase the routing row holds right now — read straight from it."""
    from server import _state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(_state_db_path(None))
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row is not None else None


# ---------------------------------------------------------------------------
# 1 — the lines are the card's
# ---------------------------------------------------------------------------


def test_detail_returns_progress_lines(seed_plan):
    """A registered plan answers with the card's own progress lines.

    The lines are the ones ``cards.build_progress_lines`` builds from the
    very same plan status — the same builder the Feishu card renders,
    so the panel and the card cannot say two different things about the
    same plan.
    """
    plan_id = seed_plan(
        "plan-detail-lines",
        files={"tasks.json": "[]"},
        phase="verification",
    )

    response = _detail(plan_id)

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["plan_id"] == plan_id, body
    assert body["phase"] == "verification", body
    # The label is the one the shared helper produces for that phase —
    # the same one the scheduler's read model puts on its rows.
    assert body["phase_label"] == routes_plans._phase_label("verification"), body
    assert body["phase_label"] == "verification", body

    assert body["lines"], body
    for line in body["lines"]:
        assert set(line) == {"kind", "text"}, line

    # The lines are the card's: the same builder, fed the same status,
    # produces exactly these lines.
    status_payload = routes_plans.get_plan_status(plan_id)
    assert isinstance(status_payload, dict), status_payload
    expected = cards.build_progress_lines(status_payload)["lines"]
    assert body["lines"] == expected, body


def test_detail_phase_label_comes_from_the_shared_table(seed_plan, monkeypatch):
    """The label is the shared table's — the read model's label too.

    The helper in ``routes.plans`` is replaced with one that says
    something else, and the endpoint's answer changes with it.  A label
    computed here — a second copy of the table, the card's own emoji
    vocabulary — would not move when the table moves, and the same
    phase would then be labelled two ways in two endpoints: the read
    model's rows and this detail.
    """
    plan_id = seed_plan(
        "plan-detail-label",
        files={"tasks.json": "[]"},
        phase="verification",
    )

    def _fake_label(phase: Optional[str]) -> str:
        return f"label-of-{phase}"

    monkeypatch.setattr(routes_plans, "_phase_label", _fake_label)

    response = _detail(plan_id)

    assert response.status_code == 200, response.text
    assert response.json()["phase_label"] == "label-of-verification", response.text


# ---------------------------------------------------------------------------
# 2 — an unknown plan
# ---------------------------------------------------------------------------


def test_detail_unknown_plan_404(seed_plan):
    """A plan that is not registered is a 404, not an empty detail.

    The panel asks about a plan that is not there; the answer says so
    instead of handing back a detail with no lines, which the panel
    would render as an empty box.
    """
    # One registered plan, so the tree this instance serves is searchable
    # and the state database holds rows of its own — the situation in
    # which "not found" is a fact this instance may state.
    seed_plan(
        "plan-detail-known",
        files={"tasks.json": "[]"},
        phase="verification",
    )

    response = _detail("plan-detail-unknown")

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "plan not found"}


# ---------------------------------------------------------------------------
# 3 — reading writes nothing
# ---------------------------------------------------------------------------


def test_detail_is_read_only(seed_plan):
    """Opening a plan does not move it.

    The endpoint is a **read**: it asks the plan's status for where the
    plan is and hands the card's wording on.  The routing row's phase is
    the same before the call and after it — a detail endpoint that
    stamped a phase would be a second writer of the plan's state.
    """
    plan_id = seed_plan(
        "plan-detail-read-only",
        files={"tasks.json": "[]"},
        phase="verification",
    )

    before = _routing_phase(plan_id)

    response = _detail(plan_id)
    assert response.status_code == 200, response.text

    after = _routing_phase(plan_id)

    assert before == "verification", before
    assert after == before, (before, after)

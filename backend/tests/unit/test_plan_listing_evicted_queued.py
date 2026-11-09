"""The plans list keeps an evicted plan that is back in the queue.

What this pins
--------------
A plan the scheduler **evicted** gave way to another plan that needed the
capacity.  The eviction writes a verdict on the victim's execution row:
``exec_status='stopped'`` and ``stop_reason='evicted:<requester>'`` — the
run is over, and the reason names the plan that took its place.  The
victim is put **back in the queue** (``queued``) to wait for its next
turn.

The plan listing's terminal verdict (``routes/plans.py``'s
``_plan_is_terminal``) reads two verdicts: the routing phase and the
persisted execution verdict.  For the evicted plan they contradict each
other — the phase says "queued", the execution row says "stopped" — and
the second verdict wins (``stopped`` is in ``TERMINAL_EXEC_STATUSES``).
So the plan was filtered out of the list even though it was waiting for
its next turn: it vanished from the operator's sidebar.

The fix is a third input: the stop reason.  An evicted plan that is
queued again is **not** terminal — its run is over, but the plan is not.
The exemption is deliberately narrow:

  * it needs the routing phase to be ``queued`` (a plan whose phase is
    ``stopped`` — the requeue that failed — is terminal whatever the
    reason says);
  * it needs the stop reason to say ``evicted:`` (a run the operator
    stopped is terminal);
  * a two-argument call — every pre-existing caller — keeps the old rule
    exactly.

What the test does
------------------
1. Pins the four verdicts of ``_plan_is_terminal`` directly (the pure
   judgement, with the reason as a third input).
2. Pins ``_persisted_stop_reason`` — the reader of the reason off an
   execution row.
3. Reads the **production route** (``GET /api/plans``) with the rows an
   eviction really leaves behind, and asserts the evicted plan is in the
   listing while a user-stopped queued plan is not.  The unit-level
   assertions alone would not prove the route passes the reason in.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from server import app, invalidate_plans_cache

pytestmark = pytest.mark.unit

client = TestClient(app)


# ---------------------------------------------------------------------------
# The pure judgement
# ---------------------------------------------------------------------------


def _plans_module():
    """``routes.plans``, imported the only way that works.

    ``routes.plans`` imports ``server`` at module level and ``server``
    imports ``routes.plans`` back (for a handful of helpers), so the
    first of the two to be imported has to be ``server``.
    """
    import server  # noqa: F401  — imported first, on purpose
    from routes import plans

    return plans


def test_queued_evicted_plan_is_not_terminal() -> None:
    """An evicted plan that is back in the queue is waiting, not over.

    The run is over (``stopped``), but the plan is not: the eviction put
    it back in the queue to wait for its next turn.  A listing that
    filters it out hides a plan that is still going to run.
    """
    plans = _plans_module()

    assert plans._plan_is_terminal("queued", "stopped", "evicted:u") is False


def test_queued_plan_with_other_stop_reason_is_terminal() -> None:
    """The exemption belongs to the eviction, not to every stopped run.

    A run the operator stopped (``stop_reason='user_requested'``) leaves
    the plan where it was; nothing put it back in the queue, so the old
    rule stands and the plan is terminal.
    """
    plans = _plans_module()

    assert plans._plan_is_terminal("queued", "stopped", "user_requested") is True


def test_stopped_evicted_plan_is_terminal() -> None:
    """A plan whose phase is ``stopped`` is terminal, reason or not.

    The phase says the plan itself is over — the requeue that should
    have put it back in the queue did not happen.  The reason on the
    execution row cannot overrule the phase.
    """
    plans = _plans_module()

    assert plans._plan_is_terminal("stopped", "stopped", "evicted:u") is True


def test_two_argument_calls_keep_the_old_rule() -> None:
    """Two arguments are the old rule, unchanged.

    Every caller that predates the stop reason — the requeue path, the
    resume path, the existing tests — passes two arguments.  They must
    keep the answer they had: no reason, no exemption.
    """
    plans = _plans_module()

    assert plans._plan_is_terminal("queued", "stopped") is True


def test_persisted_stop_reason_reads_the_reason_off_the_row() -> None:
    """The reason is read off the execution row, verbatim."""
    plans = _plans_module()

    assert plans._persisted_stop_reason(
        {"stop_reason": "evicted:pdt-e2e-9"}
    ) == "evicted:pdt-e2e-9"


def test_persisted_stop_reason_is_none_without_a_reason() -> None:
    """No row, no reason, an empty reason — all of them read as ``None``.

    ``None`` means "there is no reason to read", never "the run stopped
    for no reason": an empty reason must not be mistaken for one, and it
    must not exempt anything.
    """
    plans = _plans_module()

    assert plans._persisted_stop_reason(None) is None
    assert plans._persisted_stop_reason({}) is None
    assert plans._persisted_stop_reason({"stop_reason": None}) is None
    assert plans._persisted_stop_reason({"stop_reason": "   "}) is None


# ---------------------------------------------------------------------------
# The production route
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_listing(monkeypatch, tmp_path):
    """Point the listing at this test's own plans tree."""
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    (tmp_path / "plans").mkdir(parents=True, exist_ok=True)
    invalidate_plans_cache()
    yield
    invalidate_plans_cache()


def _seed_plan(plan_id: str) -> None:
    """Materialise the plan directory an eviction victim has.

    ``tasks.json`` is what makes the listing's artifact heuristic answer
    ``queued``; the directory is also what makes the tree and the state
    database describe each other (the listing refuses to answer when
    they do not).
    """
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "tasks": [{"id": "1", "title": "wait for the next turn"}],
        }),
        encoding="utf-8",
    )


def _seed_execution_row(
    plan_id: str,
    *,
    exec_status: str,
    stop_reason: Optional[str],
) -> None:
    """Write the execution row the eviction (or the operator) leaves.

    The row is written through the repository production writes through,
    so the listing reads back exactly the shape production produces.
    """
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = db_open(_state_db_path(None))
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "queued")
        ExecutionRepository(conn).insert(
            plan_id,
            exec_status=exec_status,
            stop_reason=stop_reason,
            project_dir=str(_state_db_path(None).parent / "project" / plan_id),
        )
    finally:
        conn.close()


def _execution_row(plan_id: str) -> Dict[str, Any]:
    """The execution row of one plan, read back from the database.

    The row is the fact the listing filters on, so it is read back from
    the database rather than taken from the values the test just wrote:
    what has to hold is that the *persisted* row says "evicted" and that
    the listing keeps the plan anyway.
    """
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(_state_db_path(None))
    try:
        return ExecutionRepository(conn).summary(plan_id)
    finally:
        conn.close()


def _listing() -> Dict[str, Any]:
    """The default listing, read through the production route."""
    invalidate_plans_cache()
    response = client.get("/api/plans")
    assert response.status_code == 200, response.text
    return {entry["id"]: entry for entry in response.json()}


def test_the_listing_keeps_the_evicted_plan_that_is_queued_again() -> None:
    """The evicted plan stays in the list; the stopped one does not.

    Two plans, one listing, one difference: the stop reason.  The
    evicted one was put back in the queue by the eviction and is waiting
    for its next turn — it must be visible.  The other one was stopped
    by the operator and is over — it must be filtered out, which is the
    negative control that keeps the exemption from being "every stopped
    run is fine".
    """
    evicted = "20261109-evicted-queued"
    stopped = "20261109-user-stopped-queued"

    for plan_id in (evicted, stopped):
        _seed_plan(plan_id)

    _seed_execution_row(
        evicted,
        exec_status="stopped",
        stop_reason="evicted:pdt-e2e-9",
    )
    _seed_execution_row(
        stopped,
        exec_status="stopped",
        stop_reason="user_requested",
    )

    entries = _listing()

    # The persisted row says exactly what an eviction writes: the run is
    # over, and the reason names the plan that took the place.
    row = _execution_row(evicted)
    assert row["exec_status"] == "stopped", row
    assert row["stop_reason"] == "evicted:pdt-e2e-9", row

    assert evicted in entries, (
        f"the evicted plan is back in the queue but missing from the "
        f"listing: {sorted(entries)!r}"
    )
    assert entries[evicted]["current_phase"] == "queued", entries[evicted]
    assert stopped not in entries, (
        f"a plan the operator stopped is not over and must stay out of "
        f"the listing: {entries[stopped]!r}"
    )

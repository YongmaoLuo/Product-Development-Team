"""The plan listing must not report a runtime phase for a plan that never ran.

What this pins
--------------
``GET /api/plans`` answers two fields per plan:

* ``status`` — a heuristic read off the plan directory (``tasks.json``
  present means ``ready``), overridden to ``running`` when the execution
  store holds a live run (a status of ``running`` with a positive pid);
* ``current_phase`` — the authority, ``plan_routing.current_phase``.

Those two must agree, and for one phase they cannot be read
independently.  ``executing`` does not describe a plan's ARTEFACTS, it
describes a PROCESS: "the executor is running this plan right now".  So
the execution store can refute it, and the listing has to check it
against the store before repeating it.

The shape that made them disagree is a plan directory carrying a legacy
``plan_state.json``: ``plan_state.PlanState._load_state`` imports that
file's ``current_phase`` into ``plan_routing`` verbatim, once, and never
reconciles it.  A fixture left behind by a test run therefore carries
``executing`` forever, and the listing answered::

    {"id": "resume-test-1790215922380", "status": "ready",
     "pid": null, "current_phase": "executing"}

— ``ready`` (no pid, no execution row) and ``executing`` in the same
entry.  Anything that counts ``current_phase`` to answer "how many plans
are running?" over-counts by every one of those.

What the test does
------------------
1. Materialise a plan in the exact shape of those leftovers: a directory
   with ``plan_state.json`` (phase ``executing``), ``tasks.json`` and
   ``execution.json``, a ``plan_routing`` row stuck at ``executing``, and
   **no** ``plan_execution`` row.
2. Read the listing through the production route (``GET /api/plans``).
3. Assert the entry says ``status=ready`` and does NOT say
   ``current_phase=executing``.
4. Assert the rule is not "executing is never reported": a plan that
   DOES have an execution record keeps its ``executing`` phase.
5. Assert the two readers agree: ``GET /api/plan/{id}/status`` — the
   single-plan snapshot — must report the same phase as the listing.
   It used to repeat the routing row instead, so the same plan read
   ``ready`` in one answer and ``executing`` in the other.

Scope, stated honestly
----------------------
This reads the readers — the listing and the single-plan status endpoint.
It does not repair the stale routing row — the routing table is the phase
authority, and rewriting it is a data migration, not a reader's job.  What
it pins is that no reader repeats a phase the execution store can refute,
and that the two readers do not answer the same question differently.
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


def _seed_plan(plan_id: str, *, phase: str = "executing") -> None:
    """Materialise a plan directory in the shape the leaked fixtures had.

    ``plan_state.json`` carries the legacy phase, ``tasks.json`` is what
    makes the listing's artifact heuristic answer ``ready``, and
    ``execution.json`` is the on-disk twin of the (absent) execution
    record.
    """
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": phase,
            "completed_phases": ["interview", "prd_generation", "task_generation"],
            "review_rounds": {"prd": 1, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8",
    )
    (plan_dir / "execution.json").write_text(
        json.dumps({
            "status": "running", "started_at": "2026-09-24T02:12:02Z",
            "ended_at": None, "pid": None, "stop_reason": None,
            "project_dir": "/tmp/nonexistent",
        }),
        encoding="utf-8",
    )


def _set_routing_phase(plan_id: str, phase: str) -> None:
    """Write the routing row the legacy migration would have written."""
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


def _add_execution_record(plan_id: str) -> None:
    """Give the plan the execution row a real run would have left."""
    from server import _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import ExecutionRepository

    conn = open_db(_state_db_path(None))
    try:
        migrate(conn)
        ExecutionRepository(conn).insert(
            plan_id, exec_status="running", exec_pid=4242,
        )
    finally:
        conn.close()


def _entry(plan_id: str) -> dict:
    """The listing entry for *plan_id*, read through the production route.

    The listing is cached by design (same ``PLANS_DIR``, same
    ``include_terminal``), so the cache is dropped before each read —
    otherwise a second plan seeded into the same tree would never appear.
    """
    invalidate_plans_cache()
    response = client.get("/api/plans")
    assert response.status_code == 200, response.text
    entries = {e["id"]: e for e in response.json()}
    assert plan_id in entries, (
        f"{plan_id} is missing from the listing: {entries!r}"
    )
    return entries[plan_id]


def test_a_plan_with_no_execution_record_is_not_reported_as_executing():
    """The contract: ``executing`` needs an execution record behind it."""
    never_ran = "resume-test-1790215922380"
    _seed_plan(never_ran)
    _set_routing_phase(never_ran, "executing")

    entry = _entry(never_ran)

    # The plan has tasks and no execution: the listing says ``queued``
    # (the single waiting phase; ``ready`` is only its legacy input alias
    # and must not be emitted).
    assert entry["status"] == "queued", entry
    assert entry["pid"] is None, entry
    # And it must not ALSO claim the executor is running it.
    assert entry["current_phase"] != "executing", (
        f"{never_ran} has no execution record, so ``executing`` is not a "
        f"phase it can be in — the listing answered {entry!r}"
    )
    # The two answers have to agree with each other.
    assert entry["current_phase"] == entry["status"], entry

    # The rule is not "``executing`` is never reported": a plan that DOES
    # have an execution record keeps it.
    running = "resume-test-1790215922382"
    _seed_plan(running)
    _set_routing_phase(running, "executing")
    _add_execution_record(running)

    running_entry = _entry(running)
    assert running_entry["current_phase"] == "executing", running_entry


def test_the_status_endpoint_agrees_with_the_listing_about_the_phase():
    """One plan, one phase — whichever endpoint you ask.

    The listing learned the rule above; ``GET /api/plan/{id}/status`` did
    not, and it kept repeating the routing row.  So the same plan read
    ``ready`` in one answer and ``executing`` in the other: one plan, two
    answers, and no way for a consumer to tell which one is the lie.

    Both readers now call the same judgement
    (``plan_status.resolve_phase``) with the same fallback, so they
    cannot disagree.  This pins that they do not.
    """
    never_ran = "resume-test-1790215922384"
    _seed_plan(never_ran)
    _set_routing_phase(never_ran, "executing")

    entry = _entry(never_ran)

    response = client.get(f"/api/plan/{never_ran}/status")
    assert response.status_code == 200, response.text
    payload = response.json()

    # The two endpoints must answer the same question the same way.
    assert payload["phase"] == entry["current_phase"], (
        f"the listing says current_phase={entry['current_phase']!r} and the "
        f"status endpoint says phase={payload['phase']!r} for the same plan "
        f"(no execution record, routing row says 'executing')"
    )
    # And the shared answer is not the refuted runtime claim.
    assert payload["phase"] != "executing", payload

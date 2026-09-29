"""TDD tests for /api/plans response cache.

Pins the contract for the in-memory cache that backs ``GET /api/plans``:

* Cache hits return the same payload without re-reading the plans
  directory.
* ``invalidate_plans_cache()`` forces a fresh re-read on the next call.
* The ``include_terminal`` query parameter is part of the cache key, so
  filtered and unfiltered requests do not poison each other.

The cache is exposed (rather than purely internal) so that mutation
endpoints (``start_execution``, ``stop_execution``, ``start_verification``,
``stop_verification``, ``interview/answer``, ``prd/generate``, …) can call
``invalidate_plans_cache()`` to drop the cached snapshot when they know
the listing is stale, and so tests can deterministically force a fresh
read instead of waiting for the TTL to expire.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from server import app, _PLANS_CACHE, invalidate_plans_cache


client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_plans_dir(monkeypatch, tmp_path):
    """Redirect ``PLANS_DIR`` to a tmp dir and reset cache state per test."""
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    (tmp_path / "plans").mkdir(parents=True, exist_ok=True)
    invalidate_plans_cache()
    yield
    invalidate_plans_cache()


def _seed_plan(plan_id: str) -> None:
    """Create a minimal on-disk plan directory that ``list_plans`` will pick up.

    Also inserts a ``plan_routing`` row into the hermetic test state.db.
    When state.db exists (any batch run), ``GET /api/plans`` goes through
    ``RoutingRepository.list_all``, which only surfaces plans that have a
    routing row (directory-derived rows are the *archived* section, gated
    on the ``^\\d{8}-`` naming convention + the 2026-08-05 cutoff).  The
    ``plan-alpha`` / ``plan-beta`` ids intentionally do not match the
    naming convention, so without a routing row they are invisible in
    batch runs while the legacy no-state.db fallback masked that in
    isolation — the 2026-09-14 order-dependence fix is to seed the row.
    """
    from server import PLANS_DIR, _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    # ``list_plans`` checks ``interview.json``/``tasks.json`` etc.  Writing
    # tasks.json is the minimum signal that the plan has reached at least
    # the "ready" stage.
    (plan_dir / "tasks.json").write_text('{"tasks": []}', encoding="utf-8")

    conn = open_db(_state_db_path(None))
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        if repo.find(plan_id) is None:
            repo.insert(plan_id, "ready")
    finally:
        conn.close()


def test_invalidate_plans_cache_is_exposed():
    """``invalidate_plans_cache`` must be importable so mutation endpoints can call it."""
    assert callable(invalidate_plans_cache)


def test_second_call_within_ttl_returns_cached_payload(monkeypatch):
    """Two ``GET /api/plans`` calls within the TTL return identical payloads.

    The test mutates the plans directory between the calls; if the cache
    is honoured, the second call returns the original (pre-mutation)
    payload.  After invalidation, the third call sees the new plan.
    """
    _seed_plan("plan-alpha")

    # First call — populates the cache.
    resp1 = client.get("/api/plans")
    assert resp1.status_code == 200
    plans1 = resp1.json()
    plan_ids_1 = {p.get("id") for p in plans1 if isinstance(p, dict)}
    assert "plan-alpha" in plan_ids_1

    # Mutate the plans directory under the cache's nose.  A real disk
    # scan on the second call would now include ``plan-beta``; a cached
    # call must NOT.
    _seed_plan("plan-beta")

    resp2 = client.get("/api/plans")
    assert resp2.status_code == 200
    plans2 = resp2.json()
    plan_ids_2 = {p.get("id") for p in plans2 if isinstance(p, dict)}
    assert plan_ids_2 == plan_ids_1, "second call must return the cached payload"
    assert "plan-beta" not in plan_ids_2

    # After explicit invalidation, the next call sees the new plan.
    invalidate_plans_cache()
    resp3 = client.get("/api/plans")
    plans3 = resp3.json()
    plan_ids_3 = {p.get("id") for p in plans3 if isinstance(p, dict)}
    assert "plan-beta" in plan_ids_3


def test_cache_key_separates_include_terminal_variants(monkeypatch):
    """Filtered and unfiltered lists are cached independently.

    Calling with ``include_terminal=false`` first and then
    ``include_terminal=true`` must produce different payloads — they must
    NOT share the same cached entry.
    """
    _seed_plan("plan-filtered")

    resp_default = client.get("/api/plans")
    assert resp_default.status_code == 200
    default_payload = resp_default.json()

    resp_terminal = client.get("/api/plans?include_terminal=true")
    assert resp_terminal.status_code == 200
    terminal_payload = resp_terminal.json()

    # The cache key must be the tuple (include_terminal,); different
    # values of include_terminal must produce different cache entries,
    # so the cached result for one variant must not bleed into the other.
    assert default_payload is not terminal_payload


def test_invalidate_clears_module_level_cache():
    """``invalidate_plans_cache()`` empties ``_PLANS_CACHE``."""
    _PLANS_CACHE["False"] = (time.monotonic(), [{"id": "stale"}])
    invalidate_plans_cache()
    assert _PLANS_CACHE == {}


def test_poisoned_routing_row_is_skipped_not_raised():
    """A legacy ``plan_routing`` row with an id the validator rejects
    must not take down the whole listing.

    Regression guard for the 2026-09-25 incident: a real ``state.db``
    carried a ``'..'`` row written during the traversal era, and the
    first ``GET /api/plans`` after the plan-id containment fix went 400
    on every sidebar poll — the per-row ``_plan_dir`` guard raised on
    the first bad row and the exception escaped the full-table
    rendering. The listing skips such rows (with a warning) and still
    serves every renderable plan.
    """
    from server import _state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    _seed_plan("plan-alpha")  # a real, renderable plan

    conn = open_db(_state_db_path(None))
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        if repo.find("..") is None:
            repo.insert("..", "interview")
    finally:
        conn.close()
    try:
        resp = client.get("/api/plans")
        assert resp.status_code == 200
        plan_ids = {p.get("id") for p in resp.json() if isinstance(p, dict)}
        assert "plan-alpha" in plan_ids
        assert ".." not in plan_ids
    finally:
        # The hermetic session DB is shared across tests; leave no
        # poisoned row behind for the next list_all consumer.
        conn = open_db(_state_db_path(None))
        try:
            conn.execute("DELETE FROM plan_routing WHERE plan_id = ?", ("..",))
            conn.commit()
        finally:
            conn.close()
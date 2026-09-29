"""Integration tests for GET /api/plan/{plan_id}/state-from-db.

This endpoint is dedicated to integration tests that need to read the
``state.db.plan_routing`` table directly — it bypasses the
``/state`` route's JSON-on-disk + migration logic and is independent
of any plan-directory file.  The endpoint contract is intentionally
narrow: only the three fields the brief pins
(``plan_id``, ``current_phase``, ``completed_phases``).

Schema used by the endpoint (per the task brief):

    CREATE TABLE plan_routing (
        plan_id          TEXT,
        current_phase    TEXT NULL,
        completed_phases TEXT JSON
    );

The endpoint is read-only against the SQLite file at
``server._state_db_path()``.  The integration tests below monkeypatch
that helper to a per-test ``tmp_path / state.db`` so the test is
fully isolated from the developer's real ``state.db``.

TDD spec (pinned by the task brief):

  1. ``test_returns_plan_state_for_existing_plan``
       Happy path: a row exists; the response carries the three
       fields, ``current_phase`` and ``completed_phases`` are
       correctly decoded (``completed_phases`` is a list, not the
       raw JSON string).

  2. ``test_returns_404_when_plan_not_in_routing``
       Empty routing table; response is HTTP 404 + error_code
       ``PLAN_NOT_FOUND``.

  3. ``test_returns_503_when_state_db_missing``
       ``state.db`` does not exist on disk; response is HTTP 503 +
       error_code ``STATE_DB_MISSING``.

  4. ``test_completed_phases_json_decode``
       ``completed_phases='["a","b"]'`` -> response carries the
       decoded Python list (NOT the raw string).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterator

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Schema bootstrap helpers
# ---------------------------------------------------------------------------


# The schema the brief pins.  We deliberately do NOT touch the
# production ``state.db`` schema (which uses ``stage`` / ``substage``
# / ``version`` / ``updated_at``); this endpoint is a *separate*
# contract that talks to plan_routing with the column names the
# brief specifies.
#
# 2026-09-14: the bootstrap DROPs first instead of relying on
# ``CREATE TABLE IF NOT EXISTS``.  A leaked background verification
# thread from an earlier test in the same process can still call
# ``_persist_verification_terminal`` (which ``open_db``s +
# ``migrate``s whichever ``PDT_STATE_DB_PATH`` is current) between this
# helper's unlink and its INSERT, recreating ``plan_routing`` with the
# PRODUCTION schema — the brief's reduced-column INSERT then dies with
# ``IntegrityError: NOT NULL constraint failed:
# plan_routing.current_phase`` (red in the 2026-09-14 full-lane run; the file
# passes in isolation).  Dropping and recreating makes the bootstrap
# deterministic regardless of what a concurrent writer did to the file.
PLAN_ROUTING_SCHEMA_SQL = """
DROP TABLE IF EXISTS plan_routing;
CREATE TABLE plan_routing (
    plan_id          TEXT PRIMARY KEY,
    current_phase    TEXT,
    completed_phases TEXT
)
"""


def _create_state_db(db_path, rows=None):
    """Create a fresh ``state.db`` file with the brief's schema.

    Parameters
    ----------
    db_path:
        Path where the SQLite file will be created.  Any existing
        file at this location is overwritten.
    rows:
        Optional iterable of ``(plan_id, current_phase,
        completed_phases_json_str)`` tuples to insert after the
        schema is created.  ``completed_phases_json_str`` is the
        raw JSON string the helper will store verbatim (use
        ``None`` to insert SQL NULL).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Remove first so a leftover file from a previous test never
    # bleeds schema state into this run.
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(PLAN_ROUTING_SCHEMA_SQL)
        if rows:
            for plan_id, current_phase, completed_phases in rows:
                conn.execute(
                    "INSERT INTO plan_routing "
                    "(plan_id, current_phase, completed_phases) "
                    "VALUES (?, ?, ?)",
                    (plan_id, current_phase, completed_phases),
                )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db_path(tmp_path):
    """Return the ``tmp_path / state.db`` the autouse conftest fixture points at.

    The autouse ``isolated_plans_dir`` fixture in ``tests/conftest.py``
    monkeypatches ``server._state_db_path`` to return
    ``tmp_path / state.db`` (no request arg).  We expose that path
    here so individual tests can pre-populate it with the brief's
    schema before exercising the endpoint.
    """
    return tmp_path / "state.db"


@pytest.fixture
def client(state_db_path, fake_cc_switch_home) -> Iterator[TestClient]:
    """Build a FastAPI TestClient and pre-create an empty state.db.

    Most tests that need a non-empty routing table call
    ``_create_state_db(state_db_path, rows=...)`` BEFORE issuing
    the HTTP request; the test that asserts "db missing" simply
    removes the file before the request.

    ``fake_cc_switch_home`` is not about this endpoint: entering the
    TestClient runs the app's lifespan, which resolves the provider
    fallback chain, which needs both the optimiser's
    ``provider-order.json`` (supplied session-wide by
    ``_provider_order_contract_file``) and ``~/.cc-switch/cc-switch.db``.
    Neither ships in a git checkout, so without it the app fails to
    start here for an environment reason and every assertion below
    would be reporting that instead of the endpoint's behaviour.
    """
    # Pre-create an empty state.db so the autouse fixture's path
    # actually exists on disk for tests that don't want to seed
    # rows but DO want the file present.
    _create_state_db(state_db_path)
    from server import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_returns_plan_state_for_existing_plan(client, state_db_path):
    """Happy path: row exists -> 200 + three fields + decoded list."""
    _create_state_db(
        state_db_path,
        rows=[
            (
                "20260101-example-plan",
                "prd_review",
                json.dumps(["interview", "prd_generation"]),
            )
        ],
    )

    resp = client.get(
        "/api/plan/20260101-example-plan/state-from-db"
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "plan_id": "20260101-example-plan",
        "current_phase": "prd_review",
        "completed_phases": ["interview", "prd_generation"],
    }


def test_returns_404_when_plan_not_in_routing(client):
    """Empty routing table -> 404 + error_code PLAN_NOT_FOUND."""
    resp = client.get(
        "/api/plan/20260814-no-such-plan/state-from-db"
    )

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body == {
        "error": "plan not found in routing table",
        "error_code": "PLAN_NOT_FOUND",
    }


def test_returns_503_when_state_db_missing(state_db_path, fake_cc_switch_home):
    """state.db file does not exist -> 503 + error_code STATE_DB_MISSING."""
    # Make sure the file is gone (the ``client`` fixture may have
    # pre-created an empty one for unrelated tests).
    if state_db_path.exists():
        state_db_path.unlink()

    from server import app

    # ``fake_cc_switch_home`` for the same reason as in ``client``: the
    # lifespan needs the provider chain to resolve before this endpoint
    # is ever reached.
    with TestClient(app) as c:
        resp = c.get(
            "/api/plan/20260101-example-plan/state-from-db"
        )

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body == {
        "error": "state db unavailable",
        "error_code": "STATE_DB_MISSING",
    }


def test_completed_phases_json_decode(client, state_db_path):
    """completed_phases='["a","b"]' -> decoded list, not raw string."""
    _create_state_db(
        state_db_path,
        rows=[
            ("20260814-json-decode", "prd_review", '["a","b"]'),
        ],
    )

    resp = client.get("/api/plan/20260814-json-decode/state-from-db")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["completed_phases"] == ["a", "b"]
    # Specifically guard against "decoded" meaning "wrapped in a
    # string" — the contract is a real Python list, JSON-serialised
    # as a JSON array on the wire.
    assert isinstance(body["completed_phases"], list)
    assert body["completed_phases"] != '["a","b"]'
"""``GET /api/scheduler/state`` — the scheduler's one read model, over HTTP.

What is pinned here
-------------------
The endpoint is driven through the **mounted app** — a real request,
never a direct call into ``routes.scheduler`` — because the failure
this guards against is a route that was never registered, or one
registered *after* the frontend catch-all, where a GET is answered
with a 404 (``{"detail": "Not found"}``) instead of the snapshot.

1. **The route answers, and it answers with JSON.**  A request without
   the guard header is refused by the middleware before it reaches a
   handler, so a 403 here would mean the route sits outside the
   guarded surface; a 404 would mean the catch-all swallowed it.
2. **The key set is closed.**  The document has exactly six keys.  A
   field that appears without being declared is a field the panel
   cannot render and the LUI cannot rely on.
3. **The queue is in the production order.**  The rows come back
   sorted by ``plan_scheduler.queue.queue_key`` — the comparator the
   admission check uses — and the position of each row counts from
   one.  A second comparator here would be a second answer to "who
   runs next".
4. **An idle scheduler is an empty document, not an error.**  No
   engine, no facts: a 200 with an empty queue, no running plans and
   ``used = 0``.  A 500 here would leave the operator's panel broken
   exactly when there is nothing to show.
5. **Every way of not having a read model gives the same answer.**  An
   engine that raises while reading its state, and an engine that
   answers with something that is not a document, are both answered
   with the idle read model — each one logged, none of them a reason
   to leave the panel without an answer.

Two further pins ride along: the route is registered *ahead* of the
catch-all (the registration order is what the 404 above is made of),
and the route module does not open a database of its own — it reads
the engine's snapshot and nothing else.

The guard header
----------------
Every request here carries ``X-PDT-Request: 1``: the request guard is
middleware over every ``/api/`` path, so a request without the header
never reaches a handler at all.  The suite's hermetic environment
injects the header into every ``TestClient``, which is why the calls
below do not spell it out — and why a 403 in this file would mean the
route sits outside the guarded surface rather than that the caller
forgot a header.

Why this directory has no ``__init__.py``
-----------------------------------------
The production package is called ``plan_scheduler``, and so is this
directory.  A ``__init__.py`` here would make pytest import this
directory as the **top-level** package ``plan_scheduler`` — and the
name would then mean two things at once, with the winner decided by
the order the files happen to be imported in:

* imported first, this package shadows the production one, and every
  other test that asks for ``plan_scheduler.snapshot`` fails with
  ``ModuleNotFoundError: No module named 'plan_scheduler.policy'``;
* imported second, pytest cannot find this module inside the
  production package at all — ``ModuleNotFoundError: No module named
  'plan_scheduler.test_scheduler_state_api'`` — and the file is not
  even collected.

Which order happens is not a matter of luck: the CI resolves each
shard's files and then **shuffles the order they run in**, seeded from
the run, precisely so that a contract which only holds in one order
cannot pass unnoticed.  Both orders therefore occur in practice, and a
package marker here would make the suite's import order load-bearing.

The marker that settles it belongs one level up.  ``backend/tests/`` is
a package, and so are ``backend/tests/unit/``,
``backend/tests/integration/`` and ``backend/tests/e2e/`` — each of
which owns a ``plan_scheduler/`` subdirectory with a marker of its own.
``backend/tests/api/`` is the one that has no marker yet, so a marker
*here* would bind the top-level name to this directory instead of
naming this directory inside the ``backend.tests.api`` package.  With
the marker at the parent, this directory's test module is imported as
``backend.tests.api.plan_scheduler.test_scheduler_state_api`` — a name
nothing else claims — and the top-level ``plan_scheduler`` keeps
meaning the production package in every order.

Until that parent marker exists, this directory deliberately has none:
a directory without a package marker, rather than a package marker that
makes the suite's import order load-bearing.
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

import server  # noqa: E402
from plan_scheduler.queue import queue_key  # noqa: E402
from plan_scheduler.snapshot import build  # noqa: E402


client = TestClient(server.app)

#: The endpoint under test.
STATE_PATH = "/api/scheduler/state"

#: The frontend catch-all.  Every ``GET`` it would answer is a request
#: for a file in the frontend bundle; an ``/api/`` path that reaches it
#: is a route that was registered too late.
CATCH_ALL_PATH = "/{path:path}"

#: The six keys the read model carries — and no others.
EXPECTED_KEYS = {
    "capacity",
    "queue_length",
    "over_capacity",
    "queue",
    "running",
    "generated_at",
}


class _FakeEngine:
    """The engine's read side, with a state to read.

    The production engine answers ``snapshot()`` with the read model
    assembled by :func:`plan_scheduler.snapshot.build` — the same
    function, the same comparator.  This one does the same thing with
    a state the test chooses, so the endpoint is exercised against the
    very ordering the scheduler itself uses.
    """

    def __init__(self, state: Optional[Dict[str, Any]] = None) -> None:
        self._state = state if state is not None else {}

    def snapshot(self) -> Dict[str, Any]:
        return build(self._state)


@pytest.fixture
def install_engine(monkeypatch):
    """Put an engine on the app — and take it away afterwards.

    The handle is ``app.state.scheduler_engine``: the same one the
    lifespan hangs the engine on.
    """

    def _install(state: Optional[Dict[str, Any]] = None) -> _FakeEngine:
        engine = _FakeEngine(state)
        monkeypatch.setattr(
            server.app.state, "scheduler_engine", engine, raising=False
        )
        return engine

    return _install


def _queue_rows() -> List[Dict[str, Any]]:
    """Four queued plans, deliberately out of order.

    The order they are written in is arrival order; the order they
    must come back in is the comparator's: the promoted plan first,
    then by priority, then by arrival.
    """
    return [
        {
            "plan_id": "p-arrived-third",
            "seq": 3,
            "priority": 0,
            "promote_order": None,
            "reason": "waiting_for_slot",
        },
        {
            "plan_id": "p-promoted",
            "seq": 9,
            "priority": 0,
            "promote_order": 1,
            "reason": "waiting_for_slot",
        },
        {
            "plan_id": "p-arrived-first",
            "seq": 1,
            "priority": 0,
            "promote_order": None,
            "reason": "waiting_for_slot",
        },
        {
            "plan_id": "p-high-priority",
            "seq": 2,
            "priority": 5,
            "promote_order": None,
            "reason": "workspace_busy",
        },
    ]


# ---------------------------------------------------------------------------
# 1 — the route answers, with the snapshot
# ---------------------------------------------------------------------------


def test_state_returns_200_not_catchall(install_engine):
    """The endpoint answers 200 with the snapshot — not the catch-all.

    The catch-all is a GET, and route matching is first-match-wins, so
    a route registered after it answers 404 for a file that does not
    exist.  That 404 reads like a missing asset rather than a missing
    route, which is why the body is checked as well as the status.
    """
    install_engine({"capacity": 4, "used": 1, "queue": [], "running": []})

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json"), (
        f"the endpoint answered with {response.headers['content-type']!r} — "
        "the frontend catch-all swallowed the request"
    )
    body = response.json()
    assert "capacity" in body, body
    assert body != {"detail": "Not found"}, body
    assert "<!doctype html" not in response.text.lower()


# ---------------------------------------------------------------------------
# 2 — the key set is closed
# ---------------------------------------------------------------------------


def test_state_key_set_closed(install_engine):
    """The document has exactly six keys.

    A field that appears without being declared is a field the panel
    cannot render and the LUI cannot rely on, so the set is asserted
    exactly — in both directions.
    """
    install_engine(
        {
            "capacity": 4,
            "used": 1,
            "now": 1000.0,
            "min_runtime_seconds": 600,
            "queue": _queue_rows(),
            "running": [
                {
                    "plan_id": "p-running",
                    "acquired_at": 400.0,
                    "workspace": "/ws",
                }
            ],
        }
    )

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == EXPECTED_KEYS, sorted(body)
    assert set(body["queue"][0]) == {
        "position",
        "seq",
        "priority",
        "promote_order",
        "reason",
    }, sorted(body["queue"][0])
    assert set(body["running"][0]) == {
        "plan_id",
        "acquired_at",
        "elapsed_sec",
        "evictable",
        "workspace",
    }, sorted(body["running"][0])


# ---------------------------------------------------------------------------
# 3 — the queue is in the production order
# ---------------------------------------------------------------------------


def test_state_queue_matches_production_key(install_engine):
    """The queue comes back in ``queue_key`` order, numbered from one.

    The comparator is the production one — the same function the
    admission check sorts by.  A second comparator in the read path
    would be a second answer to "who runs next", and the only way of
    finding out which one the scheduler was using would be to watch a
    plan start in the wrong order.
    """
    rows = _queue_rows()
    install_engine({"capacity": 4, "used": 0, "queue": rows, "running": []})

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    expected = sorted(rows, key=queue_key)

    assert [row["seq"] for row in body["queue"]] == [
        row["seq"] for row in expected
    ], body["queue"]
    assert [row["position"] for row in body["queue"]] == [1, 2, 3, 4]
    assert body["queue_length"] == len(rows)


# ---------------------------------------------------------------------------
# 4 — an idle scheduler is an empty document
# ---------------------------------------------------------------------------


def test_state_empty_engine_ok(monkeypatch):
    """No engine, no facts: an empty document, not a 500.

    The endpoint is what the panel polls.  A scheduler that has not
    been wired up yet — or one with nothing to schedule — must still
    answer, with an empty queue, nothing running and ``used = 0``; a
    500 would leave the operator with a broken panel and no way to
    tell "nothing is running" from "the scheduler is broken".
    """
    monkeypatch.delattr(
        server.app.state, "scheduler_engine", raising=False
    )

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == EXPECTED_KEYS, sorted(body)
    assert body["queue"] == []
    assert body["running"] == []
    assert body["queue_length"] == 0
    assert body["capacity"]["used"] == 0
    assert body["over_capacity"] is False


def test_state_empty_engine_with_engine_installed(install_engine):
    """An engine with nothing to schedule answers the same empty way."""
    install_engine({"capacity": 4, "used": 0, "queue": [], "running": []})

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queue"] == []
    assert body["running"] == []
    assert body["capacity"]["used"] == 0


def test_state_broken_engine_answers_empty_not_500(monkeypatch):
    """An engine that cannot answer still leaves the panel an answer.

    A snapshot that cannot be read is "nothing is running" as far as the
    panel is concerned, and the panel must keep answering: a 500 would
    leave the operator with a broken panel and no way to tell an idle
    scheduler from a broken one.
    """

    class _BrokenEngine:
        def snapshot(self) -> Dict[str, Any]:
            raise RuntimeError("the scheduler's state cannot be read")

    monkeypatch.setattr(
        server.app.state, "scheduler_engine", _BrokenEngine(), raising=False
    )

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == EXPECTED_KEYS, sorted(body)
    assert body["queue"] == []
    assert body["running"] == []
    assert body["capacity"]["used"] == 0


def test_state_non_document_snapshot_answers_empty_not_500(monkeypatch):
    """An engine that answers with something that is not a document.

    The read model is a document with six keys, and the panel reads
    those keys by name.  An engine that hands back something else —
    ``None`` today, a list tomorrow — has not answered the question,
    and a document that is not the read model would leave the panel
    reading keys that are not there.  The route treats that the same
    way it treats an engine that raises: the panel still gets an
    answer, and the answer is still the read model.
    """

    class _NotADocumentEngine:
        def snapshot(self) -> None:
            return None

    monkeypatch.setattr(
        server.app.state,
        "scheduler_engine",
        _NotADocumentEngine(),
        raising=False,
    )

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == EXPECTED_KEYS, sorted(body)
    assert body["queue"] == []
    assert body["running"] == []
    assert body["capacity"]["used"] == 0


# ---------------------------------------------------------------------------
# The two pins that make the first one possible
# ---------------------------------------------------------------------------


def test_state_route_registered_before_catch_all():
    """The endpoint is registered ahead of the frontend catch-all.

    The registration order is what the 404 of the first test is made
    of, so it is asserted directly rather than only through its
    symptom.
    """
    paths = [route.path for route in server.iter_app_routes()]

    assert STATE_PATH in paths, (
        "the scheduler state endpoint is not registered on the app; a "
        "module that is never mounted answers nothing"
    )
    assert CATCH_ALL_PATH in paths, (
        "the frontend catch-all is gone — this test would pass vacuously"
    )
    assert paths.index(STATE_PATH) < paths.index(CATCH_ALL_PATH), (
        "the scheduler state endpoint is registered after the frontend "
        "catch-all, so a GET on it is answered with a file instead of "
        "the snapshot"
    )


def test_route_module_does_not_open_a_database():
    """The route reads the engine's snapshot and nothing else.

    The read model is the engine's to assemble: a route that opened
    the database itself would be a second reader, and the two would
    eventually disagree about who is running.
    """
    source = (_BACKEND_DIR / "routes" / "scheduler.py").read_text(
        encoding="utf-8"
    )

    assert "import sqlite3" not in source
    assert "sqlite3." not in source

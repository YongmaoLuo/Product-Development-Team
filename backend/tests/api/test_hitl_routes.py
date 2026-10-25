"""The HITL decision endpoints: ask, list, answer.

What is pinned here
-------------------
The three endpoints of the human-in-the-loop contract, driven through
the mounted app — a real request, never a direct call into
``routes.hitl`` — because the failure this guards against is a route
that was never registered, or one registered *after* the frontend
catch-all, where a GET is answered with ``index.html`` instead of
JSON.

1. **The loop closes.**  A decision asked for is listed, and answering
   it moves it from ``pending`` to ``answered`` — visible in the list
   under the same filter the UI polls with.
2. **The route is ahead of the catch-all.**  Two assertions, because
   they fail differently: the registration order (the flat route list)
   and the response itself (JSON, not HTML).  A route that is
   registered too late answers 404 for a *file* that does not exist,
   which reads like a missing asset rather than a missing route.
3. **A filter outside the vocabulary is refused.**  ``status`` is a
   closed vocabulary; a value nobody can interpret is a 400, not a
   silently wrong set of decisions.

The remaining boundary conditions the contract promises are pinned
here too: the request guard, the 409 on a second answer, the empty
list for a plan nobody asked anything about, the 404 for a decision
that does not exist, and the refusal of a question that could not
become a decision.

Every error body on this surface is **flat** — ``error`` names what
went wrong, ``detail`` says it in words, and a 409 adds the ``status``
the decision is actually in.  The flatness is the application's own
convention (``server.custom_http_exception_handler``), and it is
pinned here because a client that reads ``body["error"]`` breaks the
moment the shape changes.

Isolation is the autouse ``isolated_plans_dir`` fixture: the plans
tree and the state database are both redirected at ``tmp_path``, so
this file never reads a live plan or a live database.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from config_paths import resolve_plans_dir, resolve_state_db_path  # noqa: E402
from hitl.models import Decision, DecisionRequest  # noqa: E402
from hitl.store import HitlStore  # noqa: E402
from hooks import config_path  # noqa: E402
from request_guard import REQUEST_HEADER  # noqa: E402
from utils.atomic_io import atomic_write_json  # noqa: E402


client = TestClient(server.app)

#: A plan id that is valid under the shared validator (``[A-Za-z0-9._-]``)
#: and that no other test in the suite uses.
PLAN_ID = "p1"

#: The frontend catch-all.  Every ``GET`` it would answer is a request
#: for a file in the frontend bundle; an ``/api/`` path that reaches it
#: is a route that was registered too late.
CATCH_ALL_PATH = "/{path:path}"


def _create_body(**overrides):
    """A question that can become a decision, with ``overrides`` applied."""
    payload = {
        "question": "Ship the change as written?",
        "options": ["accept", "revise"],
        "default_option": "accept",
    }
    payload.update(overrides)
    return payload


def _create(plan_id: str = PLAN_ID, **overrides):
    """``POST /api/hitl/{plan_id}/decisions`` with a valid body."""
    return client.post(
        f"/api/hitl/{plan_id}/decisions", json=_create_body(**overrides)
    )


def _list(plan_id: str = PLAN_ID, **params):
    """``GET /api/hitl/{plan_id}/decisions`` with optional query params."""
    return client.get(f"/api/hitl/{plan_id}/decisions", params=params)


def _answer(decision_id: str, plan_id: str = PLAN_ID, **body):
    """``POST /api/hitl/{plan_id}/decisions/{decision_id}/answer``."""
    payload = {"option": "accept", "answered_by": "alice", "note": ""}
    payload.update(body)
    return client.post(
        f"/api/hitl/{plan_id}/decisions/{decision_id}/answer", json=payload
    )


def _stored_decision(decision_id: str, status: str, plan_id: str = PLAN_ID) -> str:
    """A decision that already sits in ``status``, written to the store.

    The HTTP contract has to answer for every state a decision can be
    in, and three of those states are not reachable by asking a
    question: they are reached by a deadline passing.  Writing the row
    directly is how the precondition is set up — the route under test
    still only ever sees it over HTTP.
    """
    request = DecisionRequest(
        plan_id=plan_id,
        source="test",
        question="Ship the change as written?",
        options=["accept", "revise"],
        default_option="accept",
    )
    decision = Decision.from_request(request, decision_id=decision_id)
    decision.status = status
    with HitlStore(resolve_state_db_path()) as store:
        store.insert(decision)
    return decision_id


def _audit_lines(plan_id: str = PLAN_ID):
    """The plan's audit mirror, one parsed record per line."""
    path = resolve_plans_dir() / plan_id / "hitl_decisions.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _the_plan_has_an_outlet(isolated_plans_dir):
    """Give the plan a destination, so its questions are asked of somebody.

    A question nobody can be asked is ``unroutable`` from the start: it
    is never ``pending``, it can never be answered, and a second answer
    to it is a conflict rather than a second refusal.  The ``cmd``
    destination here always succeeds — the delivery runs on its own
    thread, so it never holds the route.
    """
    plan_dir = resolve_plans_dir() / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        config_path(plan_dir),
        {
            "version": 1,
            "mode": "auto",
            "timeout_sec": 1800,
            "hooks": [{"kind": "cmd", "command": "true"}],
        },
        reraise=True,
    )


# ---------------------------------------------------------------------------
# 1 — the loop closes
# ---------------------------------------------------------------------------


def test_create_list_answer():
    """Ask, find, answer — and see the answer in the list.

    The three endpoints are one contract, so this walks them in the
    order a human-facing UI would: the question is asked, the plan's
    outstanding list shows it, the human answers it, and the list no
    longer shows it as outstanding.
    """
    created = _create()

    assert created.status_code == 200, created.text
    decision = created.json()
    assert decision["status"] == "pending"
    assert decision["decision_id"]
    assert decision["deadline_at"], (
        "a decision with a timeout carries a deadline; without one a "
        "reader cannot tell how long it is still waiting"
    )
    assert decision["plan_id"] == PLAN_ID

    listed = _list()
    assert listed.status_code == 200
    assert [d["decision_id"] for d in listed.json()] == [decision["decision_id"]]

    outstanding = _list(status="pending")
    assert [d["decision_id"] for d in outstanding.json()] == [decision["decision_id"]]

    answered = _answer(decision["decision_id"])
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["status"] == "answered"
    assert body["answer"] == "accept"
    assert body["answered_by"] == "alice"
    assert body["decision_id"] == decision["decision_id"]

    # The answer is visible in the list, and the decision is no longer
    # outstanding — the two facts a polling UI reads.
    assert _list(status="pending").json() == []
    settled = _list(status="answered")
    assert [d["decision_id"] for d in settled.json()] == [decision["decision_id"]]
    assert settled.json()[0]["answer"] == "accept"


# ---------------------------------------------------------------------------
# 2 — the route is ahead of the frontend catch-all
# ---------------------------------------------------------------------------


def test_route_before_catchall():
    """The list endpoint is JSON, and it is registered before the catch-all.

    The catch-all is a ``GET /{path:path}`` that answers with a file
    from the frontend bundle.  A HITL route registered after it would
    never be reached: the request would match the catch-all first and
    be answered with ``index.html`` (or a 404 for a file that does not
    exist) instead of the decision list.
    """
    paths = [route.path for route in server.iter_app_routes()]

    assert "/api/hitl/{plan_id}/decisions" in paths, (
        "the HITL routes are not registered on the app; a module that is "
        "never mounted answers nothing"
    )
    assert CATCH_ALL_PATH in paths, (
        "the frontend catch-all is gone — this test would pass vacuously"
    )
    assert paths.index("/api/hitl/{plan_id}/decisions") < paths.index(
        CATCH_ALL_PATH
    ), (
        "the HITL routes are registered after the frontend catch-all, so a "
        "GET on them is answered with a file instead of JSON"
    )

    response = _list()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json"), (
        f"the list endpoint answered with {response.headers['content-type']!r} "
        "— the frontend catch-all swallowed the request"
    )
    assert isinstance(response.json(), list)
    assert "<!doctype html" not in response.text.lower()


# ---------------------------------------------------------------------------
# 3 — a filter outside the vocabulary is refused
# ---------------------------------------------------------------------------


def test_invalid_status_400():
    """``status`` is a closed vocabulary; anything outside it is a 400.

    A filter nobody can interpret would silently return the wrong set
    of decisions — a UI asking for ``"PENDING"`` would be told "nothing
    is outstanding" and would stop showing the question.  The refusal
    is 400 rather than an empty list, so the caller learns that its
    filter was wrong.
    """
    response = _list(status="bogus")

    assert response.status_code == 400, response.text
    assert "status" in response.text.lower()
    assert response.json()["error"] == "invalid_status", (
        "the error body is flat — ``error`` names the rule that refused "
        "the request, so a client can tell a bad filter from a bad plan"
    )


@pytest.mark.parametrize(
    "status", ["PENDING", "answered ", "done", "pending,answered"]
)
def test_status_outside_the_vocabulary_is_400(status):
    """Every spelling outside the five statuses is refused, not ignored."""
    assert _list(status=status).status_code == 400


# ---------------------------------------------------------------------------
# 4 — the rest of the boundary conditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", f"/api/hitl/{PLAN_ID}/decisions", _create_body()),
        ("GET", f"/api/hitl/{PLAN_ID}/decisions", None),
        (
            "POST",
            f"/api/hitl/{PLAN_ID}/decisions/d-1/answer",
            {"option": "accept", "answered_by": "alice", "note": ""},
        ),
    ],
    ids=["create", "list", "answer"],
)
def test_missing_guard_header_is_403(method, path, body):
    """No ``X-PDT-Request``, no decision — the middleware, not the handler.

    The header is checked by middleware for every ``/api/`` path, so a
    route registered outside the middleware's reach would answer 200 to
    the one caller that should not have reached it.  A *new* route is
    exactly where such a bypass would be added, so all three endpoints
    are checked — one of them answering 200 would mean the guard does
    not cover this path.
    """
    raw = TestClient(server.app)
    raw.headers.pop(REQUEST_HEADER, None)

    response = raw.request(method, path, json=body)

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "forbidden"
    # Nothing was created behind the guard.
    assert _list().json() == []


def test_unknown_plan_lists_nothing():
    """A plan nobody asked anything about has an empty list, not an error.

    The question a list answers is "what is outstanding", not "does the
    plan exist" — a plan with no decisions and a plan that does not
    exist are the same answer.
    """
    response = _list("no-such-plan")

    assert response.status_code == 200
    assert response.json() == []


def test_duplicate_answer_is_409():
    """The second answer to a decision is a conflict, and changes nothing.

    Two operators clicking "accept" at the same second must produce one
    stored answer; the loser is told the decision is already settled
    rather than overwriting the first answer.  "Changes nothing" is
    asserted three ways: the stored answer, the stored ``answered_by``
    and ``answered_at``, and the audit mirror — a rejected answer must
    not leave a line behind either.
    """
    created = _create()
    decision_id = created.json()["decision_id"]

    first = _answer(decision_id)
    settled = _list().json()[0]
    lines_before = len(_audit_lines())

    second = _answer(decision_id, option="revise", answered_by="bob")

    assert first.status_code == 200
    assert second.status_code == 409, second.text
    # The conflict carries the state the decision is actually in, so the
    # losing operator is told what the decision looks like now instead
    # of only being told that it is not their turn.
    assert second.json()["error"] == "conflict"
    assert second.json()["status"] == "answered"
    stored = _list().json()[0]
    assert stored["answer"] == "accept", (
        "the second answer must not overwrite the first one"
    )
    assert stored["answered_by"] == "alice"
    assert stored["answered_at"] == settled["answered_at"]
    assert len(_audit_lines()) == lines_before, (
        "a refused answer must not be recorded as a transition"
    )


@pytest.mark.parametrize(
    "status,expected_code",
    [
        ("pending", 200),
        ("paused", 200),
        ("answered", 409),
        ("defaulted", 409),
        ("unroutable", 409),
    ],
)
def test_answer_status_matrix(status, expected_code):
    """The answering matrix, one row at a time.

    ``pending`` and ``paused`` are open — a paused decision is one
    whose strict deadline passed, and it stays answerable — so
    answering them succeeds.  ``answered`` / ``defaulted`` /
    ``unroutable`` are settled: a second answer to them is a conflict,
    and it must not change what the first one stored.
    """
    decision_id = _stored_decision(f"d-{status}", status)
    lines_before = len(_audit_lines())

    response = _answer(decision_id)

    assert response.status_code == expected_code, response.text
    stored = _list().json()[0]
    if expected_code == 200:
        assert stored["status"] == "answered"
        assert stored["answer"] == "accept"
        assert len(_audit_lines()) == lines_before + 1, (
            "a transition must be recorded in the audit mirror exactly once"
        )
    else:
        assert stored["status"] == status, (
            f"a refused answer must leave the {status!r} decision as it was"
        )
        assert len(_audit_lines()) == lines_before, (
            "a refused answer must not be recorded as a transition"
        )


def test_answer_to_an_unknown_decision_is_404():
    """There is nothing here — 404, not a 409 about a state.

    A stale card pointing at a decision that was never created must not
    be told that the decision is in a state it cannot be in.
    """
    response = _answer("d-does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_answer_to_another_plans_decision_is_404():
    """A decision of another plan is not answerable through this plan.

    The path carries the plan, and the answer is scoped to it: a stale
    card for one plan must not answer somebody else's question.
    """
    created = _create()
    decision_id = created.json()["decision_id"]

    response = _answer(decision_id, plan_id="p2")

    assert response.status_code == 404
    assert _list().json()[0]["status"] == "pending", (
        "the decision must be left exactly as it was"
    )


def test_a_question_that_cannot_become_a_decision_is_refused():
    """An ``auto`` decision with no default answer is refused, not stored.

    ``auto`` means "when the deadline passes, use the default answer";
    a decision with no default has no way to end.  The refusal is a
    400, and it leaves no row behind.
    """
    response = _create(default_option=None)

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "auto_requires_default", (
        "the slug names the rule that refused the question, so a caller "
        "can tell it from a different refusal"
    )
    assert _list().json() == [], "a refused question must not leave a row behind"


def test_a_question_with_no_options_is_refused():
    """A question nobody can answer is refused at creation time."""
    response = _create(options=[])

    assert response.status_code == 400
    assert _list().json() == []


def test_a_traversal_plan_id_is_refused():
    """``..`` is a bad plan id, not a plan.

    Every path this route could build from that id inherits the hole,
    so the id is admitted through the shared validator before anything
    else happens.
    """
    response = _list("%2e%2e")

    assert response.status_code in (400, 404), (
        f"a plan id of '..' must be refused, not answered as a plan; "
        f"got {response.status_code}"
    )

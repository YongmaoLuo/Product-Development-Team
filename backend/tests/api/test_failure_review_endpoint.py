"""The failure-review read-only endpoint: ``GET /api/plan/{plan_id}/failure-review``.

What is pinned here
-------------------
1. **The route is on the app, ahead of the frontend catch-all.**  Route
   matching is first-match-wins and the catch-all is a GET, so a route
   registered after it is answered with a file (or a 404 for a file that
   is not there) instead of with the review.
2. **Four fields, and only four.**  The endpoint hands on what the
   sidecar recorded — ``decision_id``, ``problem``, ``suggestion``,
   ``created_at`` — and nothing else.  The sidecar carries more (the
   plan id, the round, the question), and the endpoint is a read of the
   announcement, not a second description of it.
3. **No record is a 404.**  A plan that never failed has nothing to
   show, and the answer says so.
4. **A malformed id is a 400.**  The id is admitted through the shared
   guard, so a traversal is refused the way every other plan route
   refuses it.
5. **The route is read-only.**  It writes no file and changes no state.

Isolation is the autouse ``isolated_plans_dir`` fixture: the plans tree
is redirected at ``tmp_path``, so this file never reads a live plan.

Test command::

    pytest backend/tests/api/test_failure_review_endpoint.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from routes.failure_review import get_failure_review  # noqa: E402


client = TestClient(server.app)

#: A plan id that is valid under the shared validator (``[A-Za-z0-9._-]``)
#: and that no other test in the suite uses.
PLAN_ID = "p1"

#: The endpoint under test.
REVIEW_PATH = "/api/plan/{plan_id}/failure-review"

#: The frontend catch-all.  Every ``GET`` it would answer is a request
#: for a file in the frontend bundle; an ``/api/`` path that reaches it
#: is a route that was registered too late.
CATCH_ALL_PATH = "/{path:path}"

#: The four fields the endpoint hands on — and no others.
EXPECTED_FIELDS = {"decision_id", "problem", "suggestion", "created_at"}

#: What the sidecar recorded.  The extra keys are the ones the
#: announcement writes for its own bookkeeping; the endpoint does not
#: hand them on.
SIDECAR = {
    "decision_id": "ab12",
    "problem": "验证失败：登录接口 500",
    "suggestion": "建议修复：修复登录接口",
    "created_at": "2026-11-19T12:00:00+00:00",
    "plan_id": PLAN_ID,
    "round": 2,
    "question": "[failure_review] 验证失败：登录接口 500",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_dir(plan_id: str = PLAN_ID) -> Path:
    """Where the plan's artefacts live — the isolated plans tree."""
    return Path(server.PLANS_DIR) / plan_id


def _seed_review(plan_id: str = PLAN_ID, payload: dict = None) -> Path:
    """Write a failure-review sidecar for the plan, and return its directory."""
    directory = _plan_dir(plan_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "failure_review.json").write_text(
        json.dumps(SIDECAR if payload is None else payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return directory


# ---------------------------------------------------------------------------
# 1. The route is on the app, ahead of the catch-all
# ---------------------------------------------------------------------------


def test_route_registered_before_catch_all() -> None:
    """The endpoint is registered ahead of the frontend catch-all.

    The registration order is what a swallowed request is made of, so it
    is asserted directly rather than only through its symptom.
    """
    paths = [route.path for route in server.iter_app_routes()]

    assert REVIEW_PATH in paths, (
        "the failure-review endpoint is not registered on the app; a "
        "module that is never mounted answers nothing"
    )
    assert CATCH_ALL_PATH in paths, (
        "the frontend catch-all is gone — this test would pass vacuously"
    )
    assert paths.index(REVIEW_PATH) < paths.index(CATCH_ALL_PATH), (
        "the failure-review endpoint is registered after the frontend "
        "catch-all, so a GET on it is answered with a file instead of "
        "the review"
    )


# ---------------------------------------------------------------------------
# 2. The four fields
# ---------------------------------------------------------------------------


def test_returns_four_fields() -> None:
    """A plan with a record answers with the four fields — and only them."""
    _seed_review()

    payload = get_failure_review(PLAN_ID)

    assert set(payload) == EXPECTED_FIELDS, (
        "the endpoint must hand on exactly the four recorded fields; the "
        "sidecar's bookkeeping keys are not part of the answer: {0!r}".format(
            sorted(payload)
        )
    )
    assert payload["decision_id"] == "ab12"
    assert payload["problem"] == "验证失败：登录接口 500"
    assert payload["suggestion"] == "建议修复：修复登录接口"
    assert payload["created_at"] == "2026-11-19T12:00:00+00:00"


# ---------------------------------------------------------------------------
# 3. No record
# ---------------------------------------------------------------------------


def test_404_when_no_record() -> None:
    """A plan directory with no record answers 404, and says why."""
    _plan_dir().mkdir(parents=True, exist_ok=True)

    with pytest.raises(HTTPException) as caught:
        get_failure_review(PLAN_ID)

    assert caught.value.status_code == 404
    assert caught.value.detail == "failure review not found"


# ---------------------------------------------------------------------------
# 4. The id is admitted through the shared guard
# ---------------------------------------------------------------------------


def test_traversal_id_is_refused_with_400() -> None:
    """A traversal id is a bad request, not a missing plan."""
    with pytest.raises(HTTPException) as caught:
        get_failure_review("..")

    assert caught.value.status_code == 400


# ---------------------------------------------------------------------------
# 5. The endpoint over HTTP, and it writes nothing
# ---------------------------------------------------------------------------


def test_endpoint_answers_over_http_and_writes_nothing() -> None:
    """The endpoint serves the four fields over HTTP and touches nothing.

    The route is read-only: the plan's directory is the same before the
    call and after it, and no file is added to it.
    """
    directory = _seed_review()
    before = sorted(path.name for path in directory.iterdir())

    response = client.get("/api/plan/{0}/failure-review".format(PLAN_ID))

    assert response.status_code == 200, (
        "the endpoint answered {0}: {1}".format(
            response.status_code, response.text
        )
    )
    body = response.json()
    assert set(body) == EXPECTED_FIELDS
    assert body["decision_id"] == "ab12"
    assert sorted(path.name for path in directory.iterdir()) == before, (
        "the endpoint is read-only: it must not write into the plan's "
        "directory"
    )


def test_endpoint_answers_404_over_http_without_a_record() -> None:
    """A plan without a record answers 404 over HTTP too."""
    _plan_dir().mkdir(parents=True, exist_ok=True)

    response = client.get("/api/plan/{0}/failure-review".format(PLAN_ID))

    assert response.status_code == 404
    assert response.json()["detail"] == "failure review not found"

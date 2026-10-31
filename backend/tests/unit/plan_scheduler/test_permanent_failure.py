"""Unit tests: which failures will not get better by trying again.

What this file holds down
-------------------------
The scheduler retries.  A retry is right for a fault that may go away
by itself — the database was busy, the service was restarting — and
wrong for a failure that is an *answer*: the plan is not there, the
plan is archived, the request conflicts with the plan's own state.
Retrying an answer a thousand times changes nothing, so the failure
has to be recognised as permanent *before* it is retried.

The predicate is therefore a pure function of the exception: given the
exception, it says whether retrying is pointless.  It reads the
exception and nothing else — no clock, no database, no engine state —
so it can be asked about a failure that happened long ago.

The contracts this file pins:

1. **A missing plan is permanent.**  ``PlanNotFoundError`` is the state
   machine's own word for "there is no such plan", and no retry will
   create one.
2. **409 and 410 are permanent.**  They are refusals with a status of
   their own, not outages.
3. **503 and an ordinary fault are not.**  They may go away by
   themselves, so the retry is still worth making.
4. **An archived plan is permanent, recognised by name.**  The error
   class lives in the HTTP layer, and importing that layer from the
   scheduler would be a circular import; the name is the whole of the
   contract that crosses the boundary.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from plan_scheduler.engine import is_permanent_failure
from state_machine.repositories.routing_repository import PlanNotFoundError

pytestmark = pytest.mark.unit


def test_plan_not_found_is_permanent() -> None:
    """A plan that is not there will not be there on the next try."""
    assert is_permanent_failure(PlanNotFoundError("p1")) is True


def test_http_409_410_are_permanent() -> None:
    """409 and 410 are answers the caller gets, not outages."""
    assert is_permanent_failure(HTTPException(status_code=409)) is True
    assert is_permanent_failure(HTTPException(status_code=410)) is True


def test_http_503_and_runtime_are_transient() -> None:
    """503 and an ordinary fault may go away by themselves."""
    assert is_permanent_failure(HTTPException(status_code=503)) is False
    assert is_permanent_failure(RuntimeError("db busy")) is False


def test_archived_plan_error_by_name() -> None:
    """An archived plan is recognised by its name, not by its class.

    The class is made here rather than imported: the error lives in the
    HTTP layer, and the scheduler must not import that layer to learn
    its name.  The name is the contract.
    """
    archived = type("ArchivedPlanError", (Exception,), {})

    assert is_permanent_failure(archived("plan is archived")) is True


def test_none_is_not_permanent() -> None:
    """No exception at all is not a permanent failure."""
    assert is_permanent_failure(None) is False

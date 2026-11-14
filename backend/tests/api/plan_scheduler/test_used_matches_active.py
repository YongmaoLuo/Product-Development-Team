"""``capacity.used`` and ``total_active`` are the same number.

Background
----------
Two endpoints answer the same question — *how many plans are working
right now* — and they used to answer it two different ways:

* ``GET /api/scheduler/state`` → ``capacity.used``: the number of
  plans that hold a slot.  This is the number the operator's panel
  shows beside the capacity;
* ``GET /api/system/active`` → ``total_active``: the number of agents
  the workflow is running right now.

The scheduler's facts reader asked the **database** which plans were
working; the active endpoint asked the two **in-memory tables**.  The
rules drifted apart: a verification round that stops refreshing its
rows goes quiet in the database while the round is very much still
running, and the in-memory tables keep resting records forever.  So
the capacity panel said a slot was free while the workflow was working
on it — *used* and *active* did not agree, and neither was wrong on
its own terms.

Both readers now ask one helper — :func:`server._live_plan_ids` — so
the two answers are the same answer and cannot disagree.

What these cases pin
--------------------
* with one live verification, the two endpoints give the same number,
  and that number is **1**;
* with nothing live, the two endpoints give the same number, and that
  number is **0** — an empty answer is an answer, not an error.

The world the cases drive
-------------------------
A **real engine** behind the **mounted app**.  The engine is built by
the production builder (:func:`server._build_scheduler_engine`), so the
facts reader the snapshot reads through is the production one — the
one that was handed :func:`server._live_plan_ids`.  A fake engine
would answer the question about the fake.

The worker is **not** started: these cases read the engine's snapshot,
and a worker would tick over the very rows the cases are about.  The
engine is bound by hand instead, which is what the worker's first tick
would have done.

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
directory.  A package marker here would make pytest import this
directory as the **top-level** package ``plan_scheduler``, and the name
would then mean two things at once.  This directory deliberately has
none — the sibling files say the whole of it.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402


client = TestClient(server.app)

#: The scheduler's read model — ``capacity.used`` lives here.
STATE_PATH = "/api/scheduler/state"

#: The active endpoint — ``total_active`` lives here.
ACTIVE_PATH = "/api/system/active"

#: The plan the cases make live.  A plan id that no directory and no
#: database row knows: everything the two endpoints say about it comes
#: from the process's own tables.
LIVE_PLAN_ID = "p-a"


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


@contextmanager
def _live_verification(plan_id: str = LIVE_PLAN_ID) -> Iterator[None]:
    """Make one plan live for the duration of the block.

    The record is the shape the auto-loop writes: a status and the
    thread that drives the round.  A round with no thread is still a
    round — the thread is bookkeeping, the status is the claim.

    Both tables are cleaned up afterwards.  They are process globals,
    and a record left behind would decide the next case's answer.
    """
    server._verification_state[plan_id] = {
        "verification_status": "running",
        "thread": None,
    }
    try:
        yield
    finally:
        server._verification_state.pop(plan_id, None)
        server._execution_state.pop(plan_id, None)


@pytest.fixture
def scheduler(monkeypatch) -> Iterator[Any]:
    """The production scheduler engine, bound and hung on the app.

    The engine is the production one: it is built by the production
    builder, so the facts reader behind ``capacity.used`` is the
    production reader — the one that was handed
    :func:`server._live_plan_ids`.  A reader that was not handed it
    would answer from the database's rows alone, and the two
    endpoints would disagree — which is exactly what these cases are
    about, so a fake would answer the question about the fake.

    The worker is not started (see the module docstring), so the
    engine is bound by hand: that is the binder the worker's first
    tick would have called.
    """
    from plan_scheduler.engine import PlanSchedulerEngine

    monkeypatch.setattr(PlanSchedulerEngine, "start", lambda self: self)

    runtime = server._build_scheduler_engine()
    engine = runtime.engine
    assert engine is not None, "the scheduler engine could not be built"
    assert engine._bind(), "the scheduler engine could not be bound"

    monkeypatch.setattr(
        server.app.state, "scheduler_engine", engine, raising=False
    )
    try:
        yield engine
    finally:
        # The engine opened the database itself, so stopping it gives
        # the connection back.
        engine.stop()


def _used() -> int:
    """The number of plans that hold a slot, as the panel reads it."""
    response = client.get(STATE_PATH)
    assert response.status_code == 200, response.text
    return int(response.json()["capacity"]["used"])


def _active() -> int:
    """The number of agents the workflow is running, as the API says."""
    response = client.get(ACTIVE_PATH)
    assert response.status_code == 200, response.text
    return int(response.json()["total_active"])


# ---------------------------------------------------------------------------
# 1 — one live plan, one number
# ---------------------------------------------------------------------------


def test_used_matches_active_under_live_verification(scheduler: Any) -> None:
    """One live verification → the two endpoints say **1** and **1**.

    The panel's "used" and the API's "total active" are the same
    question asked twice.  When a round is running, the capacity panel
    must not show a free slot: the number that says how much of the
    machine is in use and the number that says how many plans are
    running have to be one number, not two that happen to agree today.
    """
    with _live_verification():
        used = _used()
        active = _active()

    assert used == active == 1, (
        f"the scheduler says {used} plan(s) hold a slot while the active "
        f"endpoint says {active} agent(s) are running — the two answers "
        "are computed from different facts and must not be allowed to "
        "drift apart"
    )


# ---------------------------------------------------------------------------
# 2 — nothing live, no error
# ---------------------------------------------------------------------------


def test_used_matches_active_when_idle(scheduler: Any) -> None:
    """Nothing live → the two endpoints say **0** and **0**.

    An empty answer is an answer.  The idle case is the one that
    breaks a count written as "the first element of something": an
    empty collection has no first element, and a count that raises on
    an empty set is a count that takes the panel down exactly when
    there is nothing to show.
    """
    used = _used()
    active = _active()

    assert used == active == 0, (
        f"an idle scheduler says {used} plan(s) hold a slot while the "
        f"active endpoint says {active} agent(s) are running — with "
        "nothing running both numbers must be zero"
    )
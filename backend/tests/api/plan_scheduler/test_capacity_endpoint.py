"""``POST /api/scheduler/capacity`` — the operator's capacity knob.

What is pinned here
-------------------
The scheduler's capacity is configured in ``backend/config.yaml`` — a
file that is tracked by git, so it may not be rewritten to change a
knob.  The number that is in force *right now* is a **row in the state
database**, and this endpoint is the operator's way of writing that row
— and of putting the configured number back.

The contract, in three answers:

* **200** — the capacity was set, or the configured one was put back.
  The body says which number is in force **and where it came from**:
  ``{"capacity": {"total": 5, "source": "override"}}`` for a number
  that was written, and ``{"capacity": {"total": 6, "source":
  "config"}}`` for the configured one.  The word matters as much as the
  number: a panel that shows "5" without saying where it came from
  cannot tell the operator's number from the configured one.
* **400** — the request says something the scheduler cannot run with:
  a capacity that is not a number, a number outside the range, and a
  request that says nothing at all.  The refusal says which of them it
  was.
* **503** — there is no scheduler to write on: the engine is not
  running, or its configuration says the scheduler is switched off.

Two requests that are not the same request
------------------------------------------
``{"total": null}`` and ``{}`` are not the same thing: the first one
says "put the configured capacity back", the second one says nothing
at all.  The request model knows the difference — ``model_fields_set``
holds the fields the caller actually sent — and the endpoint uses it.

The world the cases drive
-------------------------
A **real engine** behind the **mounted app**: the route reads
``app.state.scheduler_engine``, and what is mounted there is a real
store on a real (migrated) state database.  The capacity override *is*
a row in that database, so a case that faked the store would be
testing a mock's memory rather than the scheduler's fact.

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
directory.  The sibling files say the whole of it: a package marker
here would make pytest import this directory as the **top-level**
package ``plan_scheduler``, and the name would then mean two things at
once.  This directory deliberately has none.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402


client = TestClient(server.app)

#: The endpoint under test.
CAPACITY_PATH = "/api/scheduler/capacity"

#: The capacity the configuration holds in the world the cases drive.
#: The number is deliberately **not** the default one, so that an
#: answer that read the default by accident would be caught.
CONFIGURED_CAPACITY = 6

#: The lowest and the highest capacity the endpoint accepts.
CAPACITY_MIN = 1
CAPACITY_MAX = 64


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


class _StaticConfig:
    """A configuration loader that always answers the same snapshot."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def current(self) -> Any:
        """Return the snapshot."""
        return self._config


def _plans_root() -> Path:
    """The plans tree the facts reader resolves plans in."""
    return Path(server.PLANS_DIR)


def _open_state_db() -> sqlite3.Connection:
    """Open the case's state database with the production PRAGMA contract."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(server._state_db_path())
    migrate(conn)
    return conn


@dataclass
class World:
    """What a case drives: the engine, its store and its connection.

    Attributes:
        engine: the engine the route reads.
        store: the scheduler's own store — where the override is a row.
        conn: the connection the engine writes on.
    """

    engine: Any
    store: Any
    conn: sqlite3.Connection


@pytest.fixture
def scheduler(monkeypatch) -> Iterator[World]:
    """A real engine behind the mounted app.

    The worker is **not** started: the endpoint writes the override and
    reads the capacity back, and neither of those needs a thread.  What
    it does need is the engine's own store and connection — the row the
    override is written on, and the row the engine reads it from.
    """
    from plan_lifecycle_service import PlanLifecycleService
    from plan_scheduler.capacity import CapacityLedger
    from plan_scheduler.commands import CommandExecutor
    from plan_scheduler.config import SchedulerConfig
    from plan_scheduler.engine import PlanSchedulerEngine
    from plan_scheduler.facts import PlanFactsReader
    from plan_scheduler.store import SqliteSchedulerStore

    conn = _open_state_db()
    store = SqliteSchedulerStore()
    facts = PlanFactsReader(conn, _plans_root(), clock=time.time)
    ledger = CapacityLedger(store, facts, time.time, conn=conn)
    commands = CommandExecutor(
        store,
        ledger,
        PlanLifecycleService(),
        facts=facts,
        conn=conn,
        capacity=CONFIGURED_CAPACITY,
        clock=time.time,
    )
    engine = PlanSchedulerEngine(
        _StaticConfig(
            SchedulerConfig(enabled=True, capacity=CONFIGURED_CAPACITY)
        ),
        store=store,
        facts=facts,
        ledger=ledger,
        commands=commands,
        sink=object(),
        clock=time.time,
        conn=conn,
    )
    # The engine keeps the configuration snapshot it was built with; the
    # reader takes it from the loader, so the snapshot is refreshed here
    # the way the first tick refreshes it.
    engine._config = engine._read_config()

    monkeypatch.setattr(
        server.app.state, "scheduler_engine", engine, raising=False
    )
    try:
        yield World(engine=engine, store=store, conn=conn)
    finally:
        conn.close()


def _override_row(world: World) -> Any:
    """The capacity override, as the row the engine reads."""
    from plan_scheduler.store import CAPACITY_OVERRIDE_KEY

    return world.store.get_setting(world.conn, CAPACITY_OVERRIDE_KEY)


# ---------------------------------------------------------------------------
# 1 — the operator's number is written, and it is in force at once
# ---------------------------------------------------------------------------


def test_capacity_post_sets_override(scheduler: World) -> None:
    """``{"total": 5}`` → 200, the number is the operator's, and it lands.

    The answer says **two** things: the number that is in force, and
    the word that says where it came from.  A panel that shows the
    number without the word cannot tell an operator's override from the
    configured capacity — so both are asserted, and so is the row the
    scheduler itself reads: an endpoint that answered "override" without
    writing anything would be a lie the next tick takes back.
    """
    response = client.post(CAPACITY_PATH, json={"total": 5})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"capacity": {"total": 5, "source": "override"}}, body

    assert _override_row(scheduler) == "5", (
        "the override is a row in the state database, and it was not "
        "written — the scheduler would still run with the configured "
        "capacity"
    )
    assert scheduler.engine._capacity() == 5, (
        "the scheduler does not run with the number that was written"
    )


# ---------------------------------------------------------------------------
# 2 — the configured capacity can be put back
# ---------------------------------------------------------------------------


def test_capacity_post_reset(scheduler: World) -> None:
    """``{"reset": true}`` → the configured capacity is back.

    Resetting is not "setting the number the configuration happens to
    hold" — it is **removing the override**, so that the configured
    capacity is in charge again and the answer says so: ``source`` goes
    back to ``config``.  The row is gone afterwards, which is what the
    engine reads to decide that there is no override.
    """
    client.post(CAPACITY_PATH, json={"total": 5})

    response = client.post(CAPACITY_PATH, json={"reset": True})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "capacity": {"total": CONFIGURED_CAPACITY, "source": "config"}
    }, body

    assert _override_row(scheduler) is None, (
        "the override was not removed — the configured capacity is not "
        "in charge"
    )
    assert scheduler.engine._capacity() == CONFIGURED_CAPACITY


def test_capacity_post_null_total_resets(scheduler: World) -> None:
    """``{"total": null}`` says the same thing as ``{"reset": true}``.

    A caller that sends ``total`` with no value is asking for the
    configured capacity back — that is what "no number" means here.
    The distinction that matters is between **no number** and **no
    request at all**: ``{}`` says nothing, and is refused (see the
    refusal cases below).
    """
    client.post(CAPACITY_PATH, json={"total": 5})

    response = client.post(CAPACITY_PATH, json={"total": None})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "capacity": {"total": CONFIGURED_CAPACITY, "source": "config"}
    }, body
    assert _override_row(scheduler) is None


# ---------------------------------------------------------------------------
# 3 — what the scheduler cannot run with is refused
# ---------------------------------------------------------------------------


def test_capacity_post_rejects_bad_values(scheduler: World) -> None:
    """A capacity that is not a number of plans → 400, and it says why.

    The five refusals are five different mistakes:

    * ``0`` and ``-1`` — numbers outside the range;
    * ``"5"`` — a number that is written as a string, which the
      scheduler would have to guess at;
    * ``true`` — a boolean, which is an ``int`` in Python and would
      silently become the capacity ``1``;
    * ``{}`` — a request that says nothing at all.

    Every one of them is refused with a **400** and a ``detail`` that
    says what was wrong, and none of them leaves a row behind: a
    refused request does not change the capacity.
    """
    for payload in (
        {"total": 0},
        {"total": -1},
        {"total": "5"},
        {"total": True},
        {},
    ):
        response = client.post(CAPACITY_PATH, json=payload)

        assert response.status_code == 400, (payload, response.text)
        body = response.json()
        assert "detail" in body, (payload, body)
        assert body["detail"], (payload, body)

    assert _override_row(scheduler) is None, (
        "a refused request wrote a capacity — a refusal must not change "
        "anything"
    )


def test_capacity_post_rejects_a_number_above_the_range(
    scheduler: World,
) -> None:
    """A number above the range is refused, and the range is in the detail.

    The capacity is a number of plans the machine may run at the same
    time: the range is written down in one place, and the refusal
    quotes it, so the operator is told what the endpoint accepts.
    """
    response = client.post(CAPACITY_PATH, json={"total": CAPACITY_MAX + 1})

    assert response.status_code == 400, response.text
    body = response.json()
    assert str(CAPACITY_MAX) in body["detail"], body

    below = client.post(CAPACITY_PATH, json={"total": CAPACITY_MIN - 1})
    assert below.status_code == 400, below.text


# ---------------------------------------------------------------------------
# 4 — there is no scheduler to write on
# ---------------------------------------------------------------------------


def test_capacity_post_without_engine(monkeypatch) -> None:
    """No engine → 503, and the body says which one it is.

    The capacity is the scheduler's own fact: with no scheduler there
    is nothing to write it on, and no capacity to answer with.  The
    answer is the same one the four actions give when the scheduler is
    not running, so a caller learns the one thing that is true.
    """
    monkeypatch.delattr(server.app.state, "scheduler_engine", raising=False)

    response = client.post(CAPACITY_PATH, json={"total": 5})

    assert response.status_code == 503, response.text
    assert response.json() == {"error": "scheduler_unavailable"}, response.text


def test_capacity_post_with_a_disabled_scheduler(
    scheduler: World, monkeypatch
) -> None:
    """An engine whose configuration says "off" → 503 as well.

    A scheduler that is switched off in the configuration is not running
    and must not be written to: the override it would carry would be a
    number nobody enforces.  The refusal is the same one the missing
    engine gets.
    """
    from plan_scheduler.config import SchedulerConfig

    monkeypatch.setattr(
        scheduler.engine, "_config", SchedulerConfig(enabled=False)
    )

    response = client.post(CAPACITY_PATH, json={"total": 5})

    assert response.status_code == 503, response.text
    assert response.json() == {"error": "scheduler_unavailable"}, response.text
    assert _override_row(scheduler) is None
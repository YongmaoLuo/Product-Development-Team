"""``GET /api/scheduler/state`` — the scheduler's "stopping" view, over HTTP.

What is pinned here
-------------------
A plan that is asked to pause leaves the running line **before** it
comes back: the stop is a request the worker has not answered yet, and
in that gap the plan is in neither the running line nor the queue.  An
operator who cannot see that gap cannot tell "the pause worked" from
"the pause was lost".

The read model therefore carries a **stopping** list — one row per
command the scheduler has accepted and not answered yet — and this
file pins the last hop of it: the engine hands the command log over to
the read model, and the endpoint hands that list to the panel.

1. **The endpoint answers with the stopping rows.**  The rows come
   from the command log — the scheduler's own fact — and a plan that
   is stopping has to be *named* by the response, or the gap is
   invisible.
2. **Every stopping row carries a phase.**  The command log knows what
   was asked for, not what phase a plan is in; the route works the
   phase out from the action, so the row can be placed.  The phase is
   never empty, and the row carries exactly the five keys the panel
   draws.
3. **A store that cannot be read costs the list, not the endpoint.**
   Another writer holding the database's lock is an ordinary event,
   and the answer is a 200 with an empty stopping list — **the rest of
   the read model still there**.  A 500 would leave the operator's
   panel broken over a list it could have drawn without.
4. **The two views may overlap.**  A plan that is working *and*
   stopping is named in both lists: dropping either one of them would
   hide the gap the list exists to show.
5. **An answered command is not stopping any more.**  The list is the
   commands that are still in flight, so the list changes by itself on
   the next read once the worker has answered.

The endpoint is driven through the **mounted app** — a real request,
never a direct call into ``routes.scheduler`` — for the reason the
sibling file gives: the failure this guards against is a route that
was never registered, or one registered after the frontend catch-all,
where a GET is answered with a 404 instead of the read model.

The engine is the **production** one, over the production store and a
real (migrated) database: the contract this file pins is a *storage*
contract — what is listed is what is in the command log, not what a
double was told to expect.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from plan_scheduler.engine import EngineParts, PlanSchedulerEngine  # noqa: E402
from plan_scheduler.store import (  # noqa: E402
    PENDING_OUTCOME,
    SqliteSchedulerStore,
)

client = TestClient(server.app)

#: The endpoint under test.
STATE_PATH = "/api/scheduler/state"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Facts:
    """A facts reader that says what the read model needs to see.

    Nothing is queued — but one plan is **parked**, and that is
    deliberate: the parked list is the one part of the read model the
    idle document does not carry.  A response that names the parked
    plan therefore proves the engine's own state was read, and not the
    route's fallback answer.

    The working plans are given to the reader, and there are none by
    default: the ordinary case of this file is a scheduler with
    nothing running in it.  A case that needs a plan in the running
    line says so.
    """

    def __init__(self, active: Optional[List[Dict[str, Any]]] = None) -> None:
        self._active = list(active or [])

    def queued_plans(self) -> List[Dict[str, Any]]:
        """No plan is waiting for its turn."""
        return []

    def active_plans(self) -> List[Dict[str, Any]]:
        """The plans that are working — none unless the case says so."""
        return list(self._active)

    def parked_plans(self) -> List[Dict[str, Any]]:
        """One plan is parked for quota."""
        return [{"plan_id": "p-parked", "reason": "waiting_for_quota"}]


class _Ledger:
    """A capacity ledger that says nobody holds compute."""

    def used(self) -> int:
        """No plan holds a slot."""
        return 0


def _open_migrated(db_path: Path) -> sqlite3.Connection:
    """Open a connection to a database that has the scheduler's tables."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(db_path)
    migrate(conn)
    return conn


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection, closed when the test is over."""
    connection = _open_migrated(tmp_path / "state.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def install_engine(
    monkeypatch, conn
) -> Callable[..., PlanSchedulerEngine]:
    """Put a real engine on the app — and take it away afterwards.

    The handle is ``app.state.scheduler_engine``: the same one the
    lifespan hangs the engine on.

    The plans that are working are given to the case: ``install_engine()``
    says nothing is working, which is the ordinary case of this file.
    """

    def _install(
        active: Optional[List[Dict[str, Any]]] = None,
    ) -> PlanSchedulerEngine:
        parts = EngineParts(
            store=SqliteSchedulerStore(),
            facts=_Facts(active),
            ledger=_Ledger(),
            commands=object(),
            sink=object(),
            conn=conn,
        )
        engine = PlanSchedulerEngine(
            config_loader=object(),
            binder=lambda: parts,
        )
        assert engine._bind(), "the engine could not be bound"
        monkeypatch.setattr(
            server.app.state, "scheduler_engine", engine, raising=False
        )
        return engine

    return _install


def _seed_open_command(
    conn: sqlite3.Connection,
    plan_id: str = "p1",
    action: str = "pause",
) -> None:
    """Write a command that was accepted and has not been answered yet."""
    SqliteSchedulerStore().record_command(
        conn, plan_id, action, outcome=PENDING_OUTCOME
    )


# ---------------------------------------------------------------------------
# 1 — the endpoint answers with the stopping rows
# ---------------------------------------------------------------------------


def test_state_includes_stopping_rows(install_engine, conn) -> None:
    """A command that is in flight names its plan in the response.

    The command log is the scheduler's own fact: the row is written the
    moment the worker takes the command, with the outcome it has *so
    far* (``accepted``), and it is turned into ``applied`` / ``failed``
    only once the worker has answered.  A row that is still
    ``accepted`` is a command that is still in flight — and a plan in
    that gap is in neither the running line nor the queue.

    So the plan has to be named: without the stopping list the
    operator cannot tell "the pause worked" from "the pause was lost".
    """
    _seed_open_command(conn, "p1", "pause")
    install_engine()

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert "stopping" in body, sorted(body)
    rows = [row for row in body["stopping"] if row["plan_id"] == "p1"]
    assert len(rows) == 1, body["stopping"]
    assert rows[0]["action"] == "pause", rows[0]


# ---------------------------------------------------------------------------
# 2 — every stopping row carries a phase
# ---------------------------------------------------------------------------


def test_stopping_row_has_phase_label(install_engine, conn) -> None:
    """A stopping row carries the phase the plan is in.

    The command log knows what was **asked for**, not what phase a plan
    is in — so the row the panel draws would have no phase at all, and
    a row the panel cannot place is a row it cannot draw.  The phase is
    worked out from the action: a plan that is asked to pause comes to
    rest in ``stopped``, and a plan that is asked to come back waits
    for its turn in ``queued``.

    The shape is closed as well: the five keys the panel draws, no
    more.  The keys are asserted exactly — a subset assertion would
    still pass when a field was added, and adding a field is
    precisely the thing that has to be a decision somebody made on
    purpose.
    """
    _seed_open_command(conn, "p1", "pause")
    install_engine()

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    rows = [row for row in body["stopping"] if row["plan_id"] == "p1"]
    assert len(rows) == 1, body["stopping"]
    row = rows[0]
    assert row.get("phase"), row
    assert row["phase"] == "stopped", row
    assert set(row) == {
        "plan_id",
        "action",
        "phase",
        "since",
        "age_seconds",
    }, sorted(row)


# ---------------------------------------------------------------------------
# 3 — a store that cannot be read costs the list, not the endpoint
# ---------------------------------------------------------------------------


def test_state_ok_when_store_busy(install_engine, conn, monkeypatch) -> None:
    """A locked database costs the stopping list and nothing else.

    Another writer holding the lock is an ordinary event, and the read
    model has to survive it: the answer is a 200 whose stopping list is
    empty, and **the rest of the read model is still there**.

    The parked plan is what proves the second half.  The idle document
    — the route's own fallback, used when the engine cannot answer at
    all — carries no parked plan, so a response that names one is a
    response built from the engine's own state: the engine read the
    world, the command log was the one part it could not read, and
    every other key was answered as usual.
    """
    _seed_open_command(conn, "p1", "pause")
    engine = install_engine()

    calls: List[Any] = []

    def _locked(*args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        calls.append(args)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(engine._store, "list_open_commands", _locked)

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    assert calls, "the engine did not ask the store for the open commands"
    body = response.json()
    assert body["stopping"] == [], body["stopping"]
    # The rest of the read model is still the engine's own answer.
    assert [row["plan_id"] for row in body["parked"]] == ["p-parked"], body


# ---------------------------------------------------------------------------
# 4 — the two views may overlap
# ---------------------------------------------------------------------------


def test_plan_in_the_running_line_is_also_listed_as_stopping(
    install_engine, conn
) -> None:
    """A plan that is working and stopping is named in **both** lists.

    The stop is a request: the plan is still working while the command
    is still in flight, and in that gap the plan is in the running
    line *and* is stopping.  Dropping either of the two would hide
    the gap — dropping the running row would say the plan is not
    working, and dropping the stopping row would say the stop was
    never asked for.

    So both lists carry the plan, and the panel — which prefers the
    stopping row — has both to choose from.
    """
    _seed_open_command(conn, "p1", "pause")
    install_engine(active=[{"plan_id": "p1", "phase": "running"}])

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["plan_id"] for row in body["running"]] == ["p1"], body["running"]
    assert [row["plan_id"] for row in body["stopping"]] == ["p1"], body["stopping"]


# ---------------------------------------------------------------------------
# 5 — an answered command is not stopping any more
# ---------------------------------------------------------------------------


def test_answered_command_leaves_the_stopping_list(install_engine, conn) -> None:
    """A command that has been answered drops out of the stopping list.

    The list is the commands that are **still in flight**: once the
    worker has answered, the command is over, and the plan is not
    stopping any more — it is stopped, or back in the queue, and the
    read model says so in its own place.  The list therefore changes
    by itself on the next read; nothing has to take the row out.
    """
    store = SqliteSchedulerStore()
    command_id = store.record_command(
        conn, "p1", "pause", outcome=PENDING_OUTCOME
    )
    install_engine()

    before = client.get(STATE_PATH)
    assert before.status_code == 200, before.text
    assert [
        row["plan_id"] for row in before.json()["stopping"]
    ] == ["p1"], before.json()["stopping"]

    assert store.finish_command(conn, command_id, "applied") is True

    after = client.get(STATE_PATH)
    assert after.status_code == 200, after.text
    assert after.json()["stopping"] == [], after.json()["stopping"]
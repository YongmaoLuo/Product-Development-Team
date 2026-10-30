"""``started_at`` names an instant — and says which one.

The defect this pins
--------------------
``POST /api/execution/{plan_id}/start`` wrote its start time with
``datetime.now().isoformat()``: a naive string that means *this
machine's* local time and carries no offset.  A reader on another
timezone — the AC Scheduler — cannot tell which instant such a string
names, so it reads one as UTC and computes an age that is wrong by the
writer's offset.  For an Asia/Shanghai writer that is eight hours: a
plan that started the previous evening reads as ~2386 minutes old the
moment it starts, and every tick reports it STUCK.

Two rules close the hole, and neither of them changes the shape of the
answer:

* **New runs write a stamp that carries its offset.**  ``_utc_now_iso()``
  already exists in this module and already answers "this instant, with
  a timezone"; the start path simply was not using it.
* **Historical stamps are interpreted, not assumed.**  A row written
  before the fix holds a naive string whose meaning is the *writer's*
  local time.  It is read as local time and answered with that offset
  attached — never as UTC, which would silently move the instant.

What is pinned
--------------
* A naive start time comes back with an offset, and it names the same
  instant it always named.
* A start time that already carries an offset keeps it, and keeps its
  instant.
* ``None`` stays ``None`` — a plan that never ran has no start time.
* A start writes an offset from now on: the persisted row's stamp is
  UTC-aware, so a reader on any timezone reads the same instant.
* The answer's shape is unchanged: the same keys, the same names.  The
  scheduler reads this endpoint and must need no change.

Hermeticity
-----------
The ``plan_execution`` row is seeded through the per-test
``PDT_STATE_DB_PATH`` database that ``server._state_db_path()``
resolves — never the operator's live ``state.db``.  The in-process
registry is cleared before and after every case: it is a module-level
dict, and a sibling test's entry would otherwise be answered as this
test's run.  The locks go with it: a lock held for a plan this case
never started would block the route.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server

# The start path and its plan scaffolding are borrowed from the
# persistence suite rather than re-invented here: a second copy of
# ``subprocess.Popen``'s stub would drift from the one CI already
# keeps green, and the contract under test is "what the start path
# persists", not "how a fake process is built".
from tests.api.test_execution_state_persistence import (  # noqa: F401
    _setup_plan,
    stub_subprocess,
)


PLAN_ID = "20261028-started-at-timezone"

#: The keys the status answer has always carried.  A rename or a
#: deletion here breaks the scheduler, which reads this endpoint and
#: was written against this shape — so the shape is pinned, not just
#: the four keys the timezone fix touches.
CONTRACT_KEYS = {
    "plan_id",
    "status",
    "logs",
    "project_dir",
    "pid",
    "started_at",
    "ended_at",
    "stop_reason",
    "sync_targets",
    "paused",
    "paused_at",
    "interrupted_task_count",
    "data_source",
    "data_source_detail",
}


@pytest.fixture(autouse=True)
def clean_registry():
    """Empty the in-process registry around every case.

    ``server._execution_state`` is a module-level dict that outlives a
    test, so an entry left behind by a sibling case would be answered
    as this case's run.  The locks go with it: a lock held for a plan
    this case never started would block the route.
    """
    server._execution_state.clear()
    server._execution_locks.clear()
    yield
    server._execution_state.clear()
    server._execution_locks.clear()


@pytest.fixture
def client() -> TestClient:
    """A client on the real application."""
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_dir(plan_id: str) -> Path:
    """The plan's directory under the (redirected) plans root."""
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


def _seed_execution_row(plan_id: str, **fields) -> None:
    """Insert/update a ``plan_execution`` row in the hermetic test DB.

    Written through the production repository so the row shape matches
    what the status route reads back via ``ExecutionRepository.summary``.
    """
    from server import _state_db_path
    from state_machine.db.connection import open as db_open
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )

    conn = db_open(_state_db_path())
    try:
        migrate(conn)
        ExecutionRepository(conn).update_fields(
            plan_id,
            create_if_missing=True,
            **fields,
        )
    finally:
        conn.close()


def _parse(value: str) -> datetime:
    """Parse an answer's stamp, tolerating a trailing ``Z``."""
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text)


# ---------------------------------------------------------------------------
# The stamps
# ---------------------------------------------------------------------------


def test_naive_started_at_gains_local_offset(client):
    """A naive start time comes back with an offset, naming the same instant.

    This is the historical row: written before the fix, it is a local
    wall-clock string with no offset.  The answer must say which
    instant it names — and it must be the instant the writer meant, so
    the offset is the *local* one, not UTC's.
    """
    plan_id = f"{PLAN_ID}-naive"
    _plan_dir(plan_id)
    naive = datetime.now().isoformat()
    _seed_execution_row(plan_id, exec_status="completed", started_at=naive)

    resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    got = resp.json()["started_at"]
    assert got, "the start time was dropped from the answer"

    parsed = _parse(got)
    assert parsed.utcoffset() is not None, (
        f"the start time still carries no offset: {got!r} — a reader on "
        "another timezone cannot tell which instant it names"
    )
    expected = datetime.fromisoformat(naive).astimezone()
    assert parsed.timestamp() == expected.timestamp(), (
        f"the start time moved: {got!r} names a different instant than "
        f"the naive local stamp {naive!r} did"
    )


def test_aware_started_at_preserved(client):
    """A start time that already carries an offset keeps it."""
    plan_id = f"{PLAN_ID}-aware"
    _plan_dir(plan_id)
    aware = "2026-10-28T08:35:13+00:00"
    _seed_execution_row(plan_id, exec_status="completed", started_at=aware)

    resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    got = resp.json()["started_at"]
    assert got, "the start time was dropped from the answer"

    parsed = _parse(got)
    assert parsed.utcoffset() == timedelta(0), (
        f"the offset of an aware start time was rewritten: {got!r}"
    )
    assert parsed.timestamp() == datetime.fromisoformat(aware).timestamp(), (
        f"the aware start time names a different instant than it did: "
        f"{got!r}"
    )


def test_none_started_at_stays_none(client):
    """A plan with no start time answers ``None`` — not a stamp."""
    plan_id = f"{PLAN_ID}-none"
    _plan_dir(plan_id)
    _seed_execution_row(plan_id, exec_status="completed", started_at=None)

    resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    assert resp.json()["started_at"] is None, (
        f"a missing start time was answered with a stamp: "
        f"{resp.json()['started_at']!r}"
    )


def test_new_start_writes_offset(
    client, stub_subprocess, tmp_path, state_db_reader
):
    """A start writes a stamp that carries its offset.

    The persisted row is what every later reader sees, so this is the
    write that has to be right: UTC-aware, so a reader on any timezone
    reads the same instant.
    """
    plan_id = f"{PLAN_ID}-new-start"
    _setup_plan(plan_id)
    project_dir = tmp_path / "target-started-at-timezone"
    project_dir.mkdir(parents=True, exist_ok=True)

    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(project_dir)},
    )
    assert resp.status_code == 200, resp.text

    row = state_db_reader.execution(plan_id)
    assert row is not None, f"plan_execution row missing for {plan_id}"

    raw = row.get("started_at")
    assert raw, f"the row carries no start time: {row!r}"

    parsed = _parse(raw)
    assert parsed.utcoffset() == timedelta(0), (
        f"the persisted start time carries no UTC offset: {raw!r} — a "
        "reader that reads a naive stamp as UTC computes a wrong age"
    )


def test_z_suffix_start_time_is_understood(client):
    """A ``Z``-suffixed stamp is UTC, and says so on the way out.

    ``Z`` is another spelling of ``+00:00``; a reader that does not
    understand it is a reader that reads the stamp as local time.
    """
    plan_id = f"{PLAN_ID}-zulu"
    _plan_dir(plan_id)
    _seed_execution_row(
        plan_id, exec_status="completed", started_at="2026-10-28T08:35:13Z"
    )

    resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    got = resp.json()["started_at"]
    parsed = _parse(got)
    assert parsed.utcoffset() == timedelta(0), (
        f"a ``Z``-suffixed start time came back without a UTC offset: {got!r}"
    )
    assert parsed.timestamp() == datetime.fromisoformat(
        "2026-10-28T08:35:13+00:00"
    ).timestamp(), f"the ``Z``-suffixed start time moved: {got!r}"


def test_unreadable_start_time_is_answered_unchanged(client):
    """A start time no parser accepts is answered as it was found.

    The read path must never turn an unreadable field into an error,
    and it must never invent a time the run did not have.
    """
    plan_id = f"{PLAN_ID}-unreadable"
    _plan_dir(plan_id)
    _seed_execution_row(
        plan_id, exec_status="completed", started_at="not-a-timestamp"
    )

    resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    assert resp.json()["started_at"] == "not-a-timestamp", (
        f"an unreadable start time was rewritten: "
        f"{resp.json()['started_at']!r}"
    )


def test_memory_branch_start_time_is_normalized(client):
    """The in-process view is normalised the same way the row is.

    This is the second branch of the answer: a plan this process is
    running but whose row has not been written yet.  A start time read
    from here names an instant exactly as much as one read from the
    row does, so it carries an offset too.
    """
    plan_id = f"{PLAN_ID}-memory"
    _plan_dir(plan_id)
    naive = datetime.now().isoformat()
    server._execution_state[plan_id] = {
        "status": "completed",
        "logs": deque(maxlen=200),
        "project_dir": "",
        "process": None,
        "started_at": naive,
        "ended_at": None,
        "pid": None,
        "stop_reason": None,
    }

    resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    got = resp.json()["started_at"]
    assert got, "the start time was dropped from the answer"
    parsed = _parse(got)
    assert parsed.utcoffset() is not None, (
        f"the in-process start time still carries no offset: {got!r}"
    )
    assert parsed.timestamp() == datetime.fromisoformat(naive).astimezone(
    ).timestamp(), f"the in-process start time moved: {got!r}"


def test_status_contract_shape_unchanged(client):
    """The answer's shape is unchanged: same keys, same names.

    The scheduler reads this endpoint and was written against this
    shape.  The timezone fix changes what a value *means*, never what
    the answer is called.
    """
    plan_id = f"{PLAN_ID}-shape"
    _plan_dir(plan_id)
    _seed_execution_row(
        plan_id,
        exec_status="completed",
        started_at=datetime.now().isoformat(),
    )

    resp = client.get(f"/api/execution/{plan_id}/status")

    assert resp.status_code == 200, resp.text
    data = resp.json()

    for key in ("status", "logs", "started_at", "pid"):
        assert key in data, f"the status answer no longer carries {key!r}"

    missing = CONTRACT_KEYS - set(data)
    assert not missing, (
        f"the status answer lost these keys: {sorted(missing)} — the "
        "scheduler reads this endpoint and was written against this shape"
    )

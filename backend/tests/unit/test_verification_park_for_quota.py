"""A verification round parked for quota is RECORDED as parked.

Background
----------
When every provider runs out of capacity mid-round, the repair path
parks the EXECUTION side: the repair-async exit writes
``state["status"] = QUOTA_WAIT_PHASE`` and the plan's phase moves to
``waiting_for_quota``.

The VERIFICATION side was left saying nothing.  The
``plan_verification`` row kept ``running`` and the side-channel
snapshot ``plans/{id}/.verification_runtime.json`` was not written at
all, so two things were unknowable: *which* round the plan stopped at,
and *that* it had stopped.  The resume had no round to pick up and the
operator had no status to read.

``_park_verification_round_for_quota`` writes that side:

  * ``plan_verification.verification_status`` becomes
    ``waiting_for_quota`` and ``verification_stop_reason`` says why;
  * the round number is left AS IT IS — a parked round resumes in place
    under the same number (arch decision R1 — 停放态原样保留);
  * the side-channel snapshot carries the same status, in the same
    format the death mark already writes (same file, same field names);
  * nothing terminal is written: no ``plan_closed``, no verdict, no
    ``ended_at`` — a parked round is not over.

What it pins
------------
1. the row says parked (status + stop reason) and the round is intact;
2. the side-channel snapshot exists and carries the parked status;
3. parking twice is idempotent — the second call writes nothing again
   and changes nothing;
4. a caller that does not know the round does not invent one;
5. a database that cannot be written does not break the repair exit —
   the failure is logged and the snapshot is still written;
6. the parked plan is not closed: its routing phase stays where it was.

Hermeticity
------------
Everything lives inside ``tmp_path``: the plan directory, the state
database and the runtime snapshot.  Nothing here touches the operator's
own ``state.db``.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server as server_mod  # noqa: E402
from verification_loop import _park_verification_round_for_quota  # noqa: E402


#: The plan this file parks.
PLAN_ID = "20261115-verification-park-for-quota"

#: The status a parked verification round carries.  It is the same word
#: the execution side parks under (``plan_status.QUOTA_WAIT_PHASE``) and
#: the same word the repository's ``mark_parked`` writes.
PARKED_STATUS = "waiting_for_quota"

#: The round the seeded plan is on.
ROUND_N = 2

#: The verdict the seeded round had already collected — the park must
#: not touch it (a parked round resumes with its verdicts intact).
SEEDED_VERDICTS = [{"vp_id": "VP-001", "verdict": "fail"}]

#: The results envelope the seeded round had already written.
SEEDED_RESULTS = {"pytest_summary": "1 failed"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_plan(db_path: Path, plan_id: str) -> None:
    """Seed a plan that is mid-verification: a running round, a verdict.

    ``plan_verification`` carries the round the plan is on; the row is
    what the park has to update, and the verdict on it is what the park
    has to leave alone.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(str(db_path))
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "verification_running")
        VerificationRepository(conn).insert(
            plan_id,
            "running",
            round=ROUND_N,
            max_rounds=3,
            verdicts=SEEDED_VERDICTS,
            results=SEEDED_RESULTS,
        )
    finally:
        conn.close()


def _verification_row(db_path: Path, plan_id: str) -> Dict[str, Any]:
    """The plan's own verification row, as the database sees it."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(str(db_path))
    try:
        row = VerificationRepository(conn).current(plan_id)
    finally:
        conn.close()
    assert row is not None, "the plan's verification row must still be there"
    return row


def _routing_row(db_path: Path, plan_id: str) -> Dict[str, Any]:
    """The plan's routing row — the phase the plan is parked at."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(str(db_path))
    try:
        row = RoutingRepository(conn).find(plan_id)
    finally:
        conn.close()
    assert row is not None, "the plan's routing row must still be there"
    return row


def _runtime_snapshot(plan_dir: Path) -> Dict[str, Any]:
    """The side-channel snapshot the park writes."""
    path = plan_dir / ".verification_runtime.json"
    assert path.exists(), "the side-channel snapshot must be written"
    return json.loads(path.read_text(encoding="utf-8"))


def _park_log_lines(caplog: Any) -> list:
    """The log lines the park recorded."""
    return [
        record.getMessage()
        for record in caplog.records
        if "verification_park" in record.getMessage()
    ]


@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    """A plan that is mid-verification, inside the test's own tree."""
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    db_path = tmp_path / "state.db"
    plan_dir = plans_root / PLAN_ID
    plan_dir.mkdir()
    monkeypatch.setattr(server_mod, "PLANS_DIR", plans_root)
    monkeypatch.setattr(
        server_mod, "_state_db_path", lambda *a, **k: str(db_path),
    )
    _seed_plan(db_path, PLAN_ID)
    server_mod._verification_state.pop(PLAN_ID, None)
    server_mod._verification_locks.pop(PLAN_ID, None)

    yield {"plan_dir": plan_dir, "db_path": db_path}

    server_mod._verification_state.pop(PLAN_ID, None)
    server_mod._verification_locks.pop(PLAN_ID, None)


# ---------------------------------------------------------------------------
# 1. The row says parked
# ---------------------------------------------------------------------------


def test_park_writes_verification_row(plan_env):
    """The plan's own row says PARKED — and stays on its round.

    The row is what the resume reads to decide which round to pick up.
    A parked row that said ``running`` would look like a live round, and
    a parked row whose round moved would relabel the round's work under
    a number that never ran.
    """
    _park_verification_round_for_quota(PLAN_ID, ROUND_N)

    row = _verification_row(plan_env["db_path"], PLAN_ID)
    assert row["verification_status"] == PARKED_STATUS, (
        "a parked round must read as parked, not as running"
    )
    assert row["verification_stop_reason"] == PARKED_STATUS, (
        "the row must say WHY the round stopped"
    )
    assert row["round"] == ROUND_N, (
        "the park must not move the round — it resumes in place"
    )


# ---------------------------------------------------------------------------
# 2. The side-channel snapshot carries the park
# ---------------------------------------------------------------------------


def test_park_writes_runtime_sidecar(plan_env):
    """The side-channel snapshot carries the parked status.

    This is the file the death mark already writes, in the same format
    and under the same field names: a restarted server reads it to learn
    what the round did.  A park that leaves no snapshot behind is a
    park nobody can see.
    """
    _park_verification_round_for_quota(PLAN_ID, ROUND_N)

    snapshot = _runtime_snapshot(plan_env["plan_dir"])
    assert snapshot["verification_status"] == PARKED_STATUS
    assert snapshot["stop_reason"] == PARKED_STATUS
    assert snapshot["verification_round"] == ROUND_N, (
        "the snapshot has to name the round the plan stopped at"
    )
    assert snapshot["ended_at"] is None, (
        "a parked round is not over — it must not carry an end time"
    )


# ---------------------------------------------------------------------------
# 3. Parking twice changes nothing
# ---------------------------------------------------------------------------


def test_park_is_idempotent(plan_env):
    """Parking twice raises nothing and writes nothing a second time.

    The park can be reached from more than one place (the repair exit
    and the auto-loop's own park), so the second call has to be a
    no-op: the same values, the same round, no exception.
    """
    _park_verification_round_for_quota(PLAN_ID, ROUND_N)
    first_snapshot = _runtime_snapshot(plan_env["plan_dir"])

    _park_verification_round_for_quota(PLAN_ID, ROUND_N)

    row = _verification_row(plan_env["db_path"], PLAN_ID)
    assert row["verification_status"] == PARKED_STATUS
    assert row["verification_stop_reason"] == PARKED_STATUS
    assert row["round"] == ROUND_N, (
        "a second park must not move the round"
    )
    assert _runtime_snapshot(plan_env["plan_dir"]) == first_snapshot, (
        "a second park must not rewrite the snapshot"
    )


# ---------------------------------------------------------------------------
# 4. The round is left intact
# ---------------------------------------------------------------------------


def test_park_keeps_the_round_intact(plan_env):
    """The verdicts and the results of the parked round are left alone.

    The round resumes in place, so the work it already did has to still
    be there when it resumes.  A park that cleared the verdicts would
    re-measure the whole round from scratch under the same number.
    """
    _park_verification_round_for_quota(PLAN_ID, ROUND_N)

    row = _verification_row(plan_env["db_path"], PLAN_ID)
    assert row["verdicts"] == SEEDED_VERDICTS
    assert row["results"] == SEEDED_RESULTS


def test_park_does_not_close_the_plan(plan_env):
    """A parked plan is not a closed plan.

    Parking is not a verdict: the routing phase stays where it was, so
    the plan is still the plan's own business and can be started again
    once the capacity is back.
    """
    _park_verification_round_for_quota(PLAN_ID, ROUND_N)

    row = _routing_row(plan_env["db_path"], PLAN_ID)
    assert row["current_phase"] == "verification_running", (
        "the park must not move the plan out of verification — the round "
        "resumes there"
    )


# ---------------------------------------------------------------------------
# 5. A caller that does not know the round
# ---------------------------------------------------------------------------


def test_park_without_a_known_round_invents_nothing(plan_env, caplog):
    """A caller that does not know the round does not make one up.

    The repair exit knows the round only sometimes.  When it does not,
    the park still happens — but the round number is left alone rather
    than invented, and the log says the round was 0 (unknown) so the
    record does not claim a round nobody handed over.
    """
    caplog.set_level(logging.INFO, logger="server")

    _park_verification_round_for_quota(PLAN_ID, 0)

    row = _verification_row(plan_env["db_path"], PLAN_ID)
    assert row["verification_status"] == PARKED_STATUS
    assert row["round"] == ROUND_N, (
        "an unknown round must not overwrite the round the plan is on"
    )
    lines = _park_log_lines(caplog)
    assert lines, "the park must be recorded in the log"
    assert any("round=0" in line for line in lines), lines


# ---------------------------------------------------------------------------
# 6. A database that cannot be written
# ---------------------------------------------------------------------------


def test_park_survives_an_unwritable_database(plan_env, monkeypatch, caplog):
    """A row that cannot be written does not break the repair exit.

    The park runs on the repair path's exit, in the middle of the
    repair bookkeeping.  A database that cannot be opened must be
    logged and stepped over — not raised into the exit, which would
    turn "the providers ran dry" into "the repair crashed".
    """
    caplog.set_level(logging.INFO, logger="server")
    not_a_database = plan_env["db_path"].parent / "not-a-database"
    not_a_database.mkdir()
    monkeypatch.setattr(
        server_mod, "_state_db_path", lambda *a, **k: str(not_a_database),
    )

    _park_verification_round_for_quota(PLAN_ID, ROUND_N)

    snapshot = _runtime_snapshot(plan_env["plan_dir"])
    assert snapshot["verification_status"] == PARKED_STATUS, (
        "the snapshot is the record of the park — it is written even when "
        "the row could not be"
    )
    assert any(
        "verification_park" in line for line in _park_log_lines(caplog)
    ), "the failed write must be logged"

"""A live verification round refuses ``/reset`` and ``/start`` (2026-11-25).

What this pins
--------------
The round LEASE — ``plans/<id>/verification_round.lock`` — is the durable
record of "a round owns this plan".  The in-memory state
(``server._verification_state``) is not: a backend restart empties it,
and a round that is still running was invisible to the guard that used
to read it.  The 2026-11-25 incident is exactly that — a ``reset``
answered ``cancelled_live_round=true`` while the plan's lease was
still beating and its VP child ran for another half hour.

So:

1. a **live** lease (fresh heartbeat, holder alive) refuses ``/reset``
   with 409 — ``force=true`` included, because ending a round is
   ``/stop``'s job and not the reset's;
2. a **live** lease refuses ``/start`` with 409 too — never a
   ``resumed: true``, never a silent no-op that answers 200;
3. a **stale** lease (old heartbeat, dead holder, no live verification
   child) does NOT block recovery: the reset removes it, with a
   WARNING that says it was stale, and the plan starts again.

The lease's staleness threshold is the holder's own heartbeat interval
times three (``verification_loop._VERIFICATION_ROUND_LEASE_HEARTBEAT_SECONDS``),
so the tests read that constant instead of hard-coding a number.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)
from verification_loop import (
    _VERIFICATION_ROUND_LEASE_HEARTBEAT_SECONDS,
    _verification_round_lock_path,
)

PLAN_ID = "20261125-round-lease-guard"

#: The staleness threshold the code uses: three missed heartbeats.
STALE_AFTER_SECONDS = _VERIFICATION_ROUND_LEASE_HEARTBEAT_SECONDS * 3


class _DormantThread:
    """``threading.Thread`` stub that never actually starts a thread."""

    def __init__(self, *args, **kwargs):
        self._alive = False

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return self._alive


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()
    server._verification_state.clear()
    monkeypatch.setattr(server.threading, "Thread", _DormantThread)

    yield plans_dir, conn, db_path

    server._execution_state.clear()
    server._verification_state.clear()
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


def _seed_plan(plans_dir: Path, db_path: Path, *, max_rounds: int = 3) -> Path:
    """A plan that sits on the verification branch, ready to be started."""
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)

    conn = open_db(db_path)
    migrate(conn)
    RoutingRepository(conn).insert(PLAN_ID, "verification")
    ExecutionRepository(conn).insert(
        plan_id=PLAN_ID,
        project_dir=str(plan_dir / "project"),
    )
    VerificationRepository(conn).insert(
        PLAN_ID, verification_status="pending", max_rounds=max_rounds,
    )
    conn.close()
    return plan_dir


def _write_lease(plan_dir: Path, *, pid: int, heartbeat: str) -> Path:
    """Write the plan's round lease, exactly as the holder writes it."""
    path = _verification_round_lock_path(plan_dir)
    path.write_text(
        json.dumps(
            {
                "plan_id": plan_dir.name,
                "pid": pid,
                "started_at": heartbeat,
                "heartbeat": heartbeat,
                "token": "test-token",
            }
        ),
        encoding="utf-8",
    )
    return path


def _dead_pid() -> int:
    """A pid that names no process."""
    for candidate in range(999999, 999899, -1):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            continue
    raise AssertionError("no free pid found")


def _other_live_pid() -> int:
    """A pid of ANOTHER process that is alive (never this test process).

    The lease of a round is held by the backend process itself, so the
    lease gate only speaks about **other** processes: a lease whose
    holder is this very process is not evidence (this process knows
    better than a file does), and the in-memory guard decides instead.
    """
    pid = os.getppid()
    assert pid > 0 and pid != os.getpid(), (
        f"the parent pid {pid} cannot stand in for another holder"
    )
    os.kill(pid, 0)  # raises ProcessLookupError when it is not alive
    return pid


def _row(db_path: Path, table: str, plan_id: str = PLAN_ID) -> dict:
    conn = open_db(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            f"SELECT * FROM {table} WHERE plan_id = ?", (plan_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. A live round refuses the reset — force included
# ---------------------------------------------------------------------------


def test_reset_refuses_a_round_with_a_fresh_lease_and_a_live_holder(
    env, client,
):
    """A fresh lease + a live holder = a running round.  409, always.

    The holder is ANOTHER live process, so the lease speaks about a
    round this process cannot see (a round that outlived a backend
    restart, or one another backend owns).  ``force=true`` must not
    cancel it: only ``/stop`` ends a round, and a reset that cancels
    one behind the operator's back is the bug this guard exists to
    close.
    """
    plans_dir, _conn, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path)
    _write_lease(
        plan_dir,
        pid=_other_live_pid(),
        heartbeat=datetime.now().isoformat(),
    )

    resp = client.post(f"/api/verification/{PLAN_ID}/reset", json={})
    assert resp.status_code == 409, resp.text
    # The app's custom HTTPException handler UNWRAPS a dict detail, so
    # the body is the dict itself.
    assert resp.json()["error"] == "Already running"

    resp = client.post(
        f"/api/verification/{PLAN_ID}/reset", json={"force": True},
    )
    assert resp.status_code == 409, (
        f"force must not cancel a live round: {resp.status_code} {resp.text}"
    )
    assert resp.json()["error"] == "Already running"

    # The refusal must be clean — nothing cleared.
    row = _row(db_path, "plan_verification")
    assert row["verification_status"] == "pending"
    assert plan_dir.joinpath("verification_round.lock").exists(), (
        "the live lease must survive the refusal"
    )


# ---------------------------------------------------------------------------
# 2. A live round refuses the start
# ---------------------------------------------------------------------------


def test_start_refuses_a_round_with_a_fresh_lease(env, client):
    """A start beside a running round is a second round on one plan.

    Before the lease was read, the start was admitted and the round
    that lost the race recorded ``round_already_running — this run
    dispatched no VP`` while the endpoint answered 200 "started".

    The holder is ANOTHER live process — a lease of this process is
    not evidence (see
    ``test_same_process_ghost_lease_does_not_refuse_reset_or_start``).
    """
    plans_dir, _conn, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path)
    _write_lease(
        plan_dir,
        pid=_other_live_pid(),
        heartbeat=datetime.now().isoformat(),
    )

    resp = client.post(f"/api/verification/{PLAN_ID}/start")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"] == "Already running"
    assert resp.json().get("resumed") is not True


# ---------------------------------------------------------------------------
# 2b. A lease of THIS process is not evidence
# ---------------------------------------------------------------------------


def test_same_process_ghost_lease_does_not_refuse_reset_or_start(
    env, client, monkeypatch,
):
    """The lease of this very process must not block anything.

    The holder pid of a round IS the backend process
    (``_acquire_verification_round_lease`` uses ``os.getpid()``), and
    its heartbeat is refreshed by an in-process daemon thread.  So
    right after a round of ours is judged dead, the lease file can
    still be on disk with a FRESH heartbeat while the thread that owns
    it walks to its ``finally``.  That instant is not a live round —
    this process knows it — and refusing there would block the very
    recovery the death sequence opens (the E2E
    ``test_e2e_second_start_is_accepted_after_death`` is that path).

    So the lease gate only speaks about OTHER processes; for a round of
    ours the in-memory guard is the authority — and here the memory
    says there is no live round, so neither endpoint refuses.
    """
    plans_dir, _conn, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path)
    lease_path = _write_lease(
        plan_dir,
        pid=os.getpid(),
        heartbeat=datetime.now().isoformat(),
    )

    # The in-memory state knows of no live round — that is the point.
    assert PLAN_ID not in server._verification_state

    resp = client.post(f"/api/verification/{PLAN_ID}/reset", json={})
    assert resp.status_code == 200, (
        f"a lease of this process refused the reset: {resp.status_code} "
        f"{resp.text}"
    )
    assert lease_path.exists(), (
        "the lease of this process must not be deleted by the reset — "
        "the thread that owns it releases it in its own finally"
    )

    def _fake_loop(plan_id, plan_dir, project_dir, **kwargs):
        raise AssertionError("the auto-loop must not run in this test")

    monkeypatch.setattr(server, "_run_auto_verification_loop", _fake_loop)
    monkeypatch.setattr(
        server, "_recover_verification_states", lambda *a, **k: None,
    )

    resp = client.post(f"/api/verification/{PLAN_ID}/start")
    assert resp.status_code == 200, (
        f"a lease of this process refused the start: {resp.status_code} "
        f"{resp.text}"
    )


# ---------------------------------------------------------------------------
# 3. A stale lease must not block recovery
# ---------------------------------------------------------------------------


def test_reset_clears_a_stale_lease_and_then_succeeds(env, client, monkeypatch):
    """A dead round's leftover lock must not lock the plan out.

    The heartbeat is older than the threshold, the holder's pid names no
    process, and the plan has no live verification child: the lease is
    stale.  The reset removes it (with a WARNING that says so) and
    proceeds; the plan starts again afterwards.
    """
    plans_dir, _conn, db_path = env
    plan_dir = _seed_plan(plans_dir, db_path)
    stale_heartbeat = (
        datetime.now() - timedelta(seconds=STALE_AFTER_SECONDS * 10)
    ).isoformat()
    lease_path = _write_lease(
        plan_dir, pid=_dead_pid(), heartbeat=stale_heartbeat,
    )
    assert lease_path.exists()

    resp = client.post(f"/api/verification/{PLAN_ID}/reset", json={})
    assert resp.status_code == 200, resp.text
    assert not lease_path.exists(), (
        "a stale lease must be removed by the reset — it is what was "
        "blocking the recovery"
    )

    # And the plan is startable again.
    def _fake_loop(plan_id, plan_dir, project_dir, **kwargs):
        raise AssertionError("the auto-loop must not run in this test")

    monkeypatch.setattr(server, "_run_auto_verification_loop", _fake_loop)
    monkeypatch.setattr(
        server, "_recover_verification_states", lambda *a, **k: None,
    )

    resp = client.post(f"/api/verification/{PLAN_ID}/start")
    assert resp.status_code == 200, (
        f"the stale lease blocked the recovery: {resp.status_code} {resp.text}"
    )

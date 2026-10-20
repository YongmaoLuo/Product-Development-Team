"""A pause has to be visible from the two endpoints an operator polls.

A stop now leaves a checkpoint on disk, which is the only durable record
that the work was interrupted rather than finished. Both read endpoints
that answer "what is this plan doing right now" — ``GET
/api/execution/{id}/status`` and ``GET /api/system/active`` — used to
report only in-memory state. That state is exactly what a restart
destroys, so immediately after a restart (the moment an operator most
needs the answer) both endpoints said the plan had never run.

``/api/system/active`` makes this worse by design: it short-circuits to
``0 / 0 / 0 / []`` when both state dicts are empty, which is precisely
the state of a freshly-restarted server. The checkpoint has to be read
before that early return, not after it.

What is pinned here:

1. **The status endpoint carries the resume point** — ``paused``,
   ``paused_at`` and ``interrupted_task_count``, on both the
   ``not_started`` branch and the live-state branch.
2. **No checkpoint means no pause**, and the three fields say so
   explicitly rather than being absent — a reader must be able to tell
   "not paused" from "this build does not report it".
3. **The active-tasks listing survives the empty-state short-circuit** —
   with both state dicts empty it still lists the paused plans.
4. **A torn checkpoint reads as no checkpoint**, not as a 500.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import pause_state
import server
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate

PLAN_ID = "20261020-pause-visible-exec"
OTHER_PLAN_ID = "20261020-pause-visible-verif"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation, both state dicts emptied."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()
    server._verification_state.clear()

    yield plans_dir, db_path

    server._execution_state.clear()
    server._verification_state.clear()
    conn.close()


@pytest.fixture
def client():
    return TestClient(app)


def _write_checkpoint(
    plans_dir: Path,
    plan_id: str,
    *,
    kind: str = "execution",
    interrupted: int = 2,
) -> Path:
    """A paused plan directory: the tasks, then the checkpoint over them."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    pause_state.write_pause_checkpoint(
        plan_dir,
        kind=kind,
        project_dir=str(plan_dir / "project"),
        reason="user_requested",
        interrupted_tasks=[
            {
                "task_id": str(index),
                "status": "in_progress",
                "phase": "execution" if kind == "execution" else "verification",
                "session_id": f"sess-{index}",
                "paused_at": None,
                "strategy": "session_resume",
                "reason": "session_id_available",
            }
            for index in range(1, interrupted + 1)
        ],
        unfinished_tasks=[{"task_id": str(index), "status": "pending"}
                          for index in range(interrupted + 1, interrupted + 3)],
    )
    return plan_dir


def _checkpoint_on_disk(plan_dir: Path) -> Dict[str, Any]:
    return json.loads(
        (plan_dir / pause_state.PAUSE_CHECKPOINT_FILENAME).read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# 1) the status endpoint carries the resume point
# ---------------------------------------------------------------------------


def test_status_exposes_checkpoint(env, client):
    """A paused plan reports when it paused and how much was in flight.

    ``paused_at`` has to be the checkpoint's own stamp rather than
    "now": the interruption happened when the stop happened, and a
    resume has to be able to reason about how long the plan has been
    sitting there.
    """
    plans_dir, _db_path = env
    plan_dir = _write_checkpoint(plans_dir, PLAN_ID, interrupted=2)
    checkpoint = _checkpoint_on_disk(plan_dir)
    server._execution_state[PLAN_ID] = {
        "status": "stopped",
        "logs": [],
        "project_dir": str(plan_dir / "project"),
        "process": None,
        "started_at": "2026-10-20T11:00:00",
        "ended_at": checkpoint["paused_at"],
        "pid": None,
        "stop_reason": "user_requested",
    }

    resp = client.get(f"/api/execution/{PLAN_ID}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["paused"] is True, body
    assert body["paused_at"] == checkpoint["paused_at"], body
    assert body["interrupted_task_count"] == 2, body


# ---------------------------------------------------------------------------
# 2) no checkpoint — the fields are present and say "not paused"
# ---------------------------------------------------------------------------


def test_status_without_checkpoint(env, client):
    """Both branches report a pause that did not happen, in full.

    The ``not_started`` branch is the one that matters most: after a
    restart it is the branch a paused-but-unresumed plan still lands
    in, because its in-memory state is gone. Reporting the fields
    there — rather than omitting them — is what makes "no pause"
    distinguishable from "this build cannot tell".
    """
    plans_dir, _db_path = env

    # (a) a plan the server has no memory of at all
    not_started = client.get(f"/api/execution/{PLAN_ID}/status").json()
    assert not_started["status"] == "not_started", not_started
    assert not_started["paused"] is False, not_started
    assert not_started["paused_at"] is None, not_started
    assert not_started["interrupted_task_count"] == 0, not_started

    # (b) a plan it remembers as stopped, but with no checkpoint on disk
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    server._execution_state[PLAN_ID] = {
        "status": "stopped",
        "logs": [],
        "project_dir": str(plan_dir / "project"),
        "process": None,
        "started_at": "2026-10-20T11:00:00",
        "ended_at": "2026-10-20T12:00:00",
        "pid": None,
        "stop_reason": "user_requested",
    }

    stopped = client.get(f"/api/execution/{PLAN_ID}/status").json()
    assert stopped["status"] == "stopped", stopped
    assert stopped["paused"] is False, stopped
    assert stopped["paused_at"] is None, stopped
    assert stopped["interrupted_task_count"] == 0, stopped


def test_corrupt_checkpoint_reads_as_no_pause(env, client):
    """A checkpoint torn by a crash must not 500 the endpoint that reads it.

    ``checkpoint_summary`` already degrades to ``None`` for an
    unreadable file; this pins that the endpoint inherits that
    behaviour rather than being the one caller that lets the error
    through.
    """
    plans_dir, _db_path = env
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / pause_state.PAUSE_CHECKPOINT_FILENAME).write_text(
        '{"version": 1, "kind": "exec', encoding="utf-8",
    )

    resp = client.get(f"/api/execution/{PLAN_ID}/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paused"] is False, body
    assert body["paused_at"] is None, body
    assert body["interrupted_task_count"] == 0, body


# ---------------------------------------------------------------------------
# 3) the active-tasks listing survives the empty-state short-circuit
# ---------------------------------------------------------------------------


def test_active_lists_paused_plans(env, client):
    """A freshly-restarted server still lists the plans it can resume.

    Both state dicts are emptied explicitly, because that is the whole
    point: the in-memory registries are the thing a restart takes with
    it, so if the listing only works while they are populated it does
    not survive the restart it was added for. The empty ``details``
    list is the short-circuit's own answer — the pauses have to arrive
    beside it, not instead of it.
    """
    plans_dir, _db_path = env
    _write_checkpoint(plans_dir, PLAN_ID, kind="execution", interrupted=2)
    _write_checkpoint(plans_dir, OTHER_PLAN_ID, kind="verification", interrupted=1)
    server._execution_state.clear()
    server._verification_state.clear()

    resp = client.get("/api/system/active")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["execution_running"] == 0, body
    assert body["verification_running"] == 0, body
    assert body["total_active"] == 0, body
    assert body["details"] == [], body

    entries = {entry["plan_id"]: entry for entry in body["paused_executions"]}
    assert set(entries) == {PLAN_ID, OTHER_PLAN_ID}, body["paused_executions"]

    for entry in body["paused_executions"]:
        assert set(entry) == {
            "plan_id", "kind", "paused_at", "interrupted_task_count",
        }, entry
        assert entry["paused_at"], entry
        assert isinstance(entry["interrupted_task_count"], int), entry

    assert entries[PLAN_ID]["kind"] == "execution", entries[PLAN_ID]
    assert entries[PLAN_ID]["interrupted_task_count"] == 2, entries[PLAN_ID]
    assert entries[OTHER_PLAN_ID]["kind"] == "verification", entries[OTHER_PLAN_ID]
    assert entries[OTHER_PLAN_ID]["interrupted_task_count"] == 1, entries[OTHER_PLAN_ID]

"""A stop says what it left behind — in the plan's own execution log.

What this pins
--------------
``stop`` used to leave two records and no more: the HTTP response (gone
when the request ends) and ``pause_state.json`` (consumed by the next
resume). Neither is an audit trail — once the plan has been resumed,
nothing is left that says "this plan was stopped here, and this is what
was in flight when it was".

``backend/resume_audit_log.py`` is the third record: one
``execution_stop_checkpoint`` event in the plan's execution log. This
file pins the *stop path* that produces it, not the writer itself (the
writer has its own tests in ``test_resume_audit_log_stop_event.py``).

Four properties, one per boundary:

* **a stop records the resume point it actually left** -- exactly one
  event, and the task lists in it are the ones in ``pause_state.json``,
  not a re-derivation of them;
* **a checkpoint that is not on disk records nothing** -- the event is
  written from what was *read back*, so a write that silently failed
  cannot leave an event claiming a resume point that does not exist;
* **``discard=true`` records nothing** -- the operator threw the resume
  point away, so there is nothing to audit;
* **a failing audit writer does not fail the stop** -- the log is
  evidence, not control flow: the stop still answers 200 and the
  checkpoint still survives.

The event is written *after* the checkpoint landed (architecture
decision point 3), and every failure around it is isolated
(architecture decision point 4).
"""

from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

import pause_state
import resume_audit_log
import server
import verification_cancel
from resume_audit_log import STOP_EVENT
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.routing_repository import RoutingRepository

PLAN_ID = "20261023-stop-audit-event"

#: The task the stop catches mid-flight. Its id is zero-padded because
#: that is how the executor names tasks, and the checkpoint echoes it
#: back verbatim.
TASK_ID = "08"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeProc:
    """A live process handle — the route only ever asks it ``poll()``."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.alive = True
        self.returncode = None

    def poll(self) -> Optional[int]:
        return None if self.alive else (self.returncode or 0)

    def wait(self, timeout=None) -> int:
        threading.Event().wait(timeout=30)
        return 0


@pytest.fixture(autouse=True)
def _clean_cancel_flags():
    verification_cancel._reset_for_tests()
    yield
    verification_cancel._reset_for_tests()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Per-test PLANS_DIR + state.db isolation."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    server._execution_state.clear()
    yield plans_dir, db_path
    server._execution_state.clear()


@pytest.fixture
def client():
    return TestClient(server.app)


def _seed_plan(
    plans_dir: Path,
    db_path: Path,
    *,
    plan_id: str = PLAN_ID,
    phase: str = "executing",
    tasks: Optional[List[Dict[str, Any]]] = None,
):
    """A plan with a routing row, a phase, one task in flight."""
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    if tasks is None:
        tasks = [
            {"id": TASK_ID, "title": "eighth task", "status": "in_progress"},
        ]
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": tasks}), encoding="utf-8",
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": phase,
            "completed_phases": ["execution"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, phase)
    finally:
        conn.close()

    return plan_dir, project_dir


def _mark_running(plan_id: str, project_dir: Path) -> None:
    """Put the plan into the state the stop endpoint sees as in-flight."""
    server._execution_state[plan_id] = {
        "status": "running",
        "logs": deque(maxlen=100),
        "project_dir": str(project_dir),
        "process": FakeProc(),
        "started_at": "2026-10-23T04:20:00",
        "ended_at": None,
        "pid": 4242,
        "stop_reason": None,
    }


def _log_entries(plan_dir: Path) -> List[Dict[str, Any]]:
    """Every JSON line of the plan's execution log, in write order."""
    log_path = plan_dir / "execution.log"
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    return entries


def _stop_events(plan_dir: Path) -> List[Dict[str, Any]]:
    """The stop events in the plan's execution log."""
    return [
        entry for entry in _log_entries(plan_dir)
        if entry.get("event") == STOP_EVENT
    ]


def _checkpoint(plan_dir: Path) -> Dict[str, Any]:
    """The checkpoint the stop left on disk."""
    return json.loads(
        (plan_dir / "pause_state.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stop_records_checkpoint_event(env, client, monkeypatch):
    """A stop writes one event, carrying the checkpoint it left behind."""
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    _mark_running(PLAN_ID, project_dir)
    # A cooperative exit: the tree went down on SIGTERM, so the
    # interruption was not a hard kill.
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: False)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "stopped"
    assert body["paused"] is True
    assert body["interrupted_task_count"] == 1

    events = _stop_events(plan_dir)
    assert len(events) == 1, (
        "a stop must leave exactly one audit event, so a reader can count "
        f"stops without de-duplicating; got {len(events)}"
    )

    data = events[0]["data"]
    stored = _checkpoint(plan_dir)
    assert data["checkpoint_kind"] == "execution"
    assert data["hard_killed"] is False
    assert data["task_count"] == 1
    assert data["interrupted"] == stored["interrupted_tasks"], (
        "the event must carry the checkpoint's own task list, not a "
        "re-derivation of it"
    )
    assert data["unfinished"] == stored["unfinished_tasks"]
    assert data["interrupted"][0]["task_id"] == TASK_ID
    assert data["interrupted"][0]["status"] == "in_progress"


def test_stop_skips_event_when_checkpoint_not_on_disk(
    env, client, monkeypatch
):
    """A checkpoint that is not on disk records nothing.

    The write is atomic but it is also *swallowed* when it fails: a
    checkpoint is a resume optimisation, and losing it must not take the
    stop down with it. So a write that silently failed leaves no file —
    and an event written from the in-memory value alone would claim a
    resume point that does not exist.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    _mark_running(PLAN_ID, project_dir)
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: False)
    monkeypatch.setattr(pause_state, "atomic_write_json", lambda *a, **k: None)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")

    assert resp.status_code == 200, resp.text
    assert not (plan_dir / "pause_state.json").exists()
    assert _stop_events(plan_dir) == [], (
        "an event must not claim a resume point that was never written"
    )


def test_stop_skips_event_when_a_stale_checkpoint_is_on_disk(
    env, client, monkeypatch
):
    """A leftover checkpoint from an earlier stop is not this stop's record.

    ``paused_at`` is the checkpoint's identity. When the write of this
    stop's checkpoint fails, the file left on disk is the *previous*
    stop's — and writing an event from it would say "these tasks were
    in flight" about a plan that has moved on.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    _mark_running(PLAN_ID, project_dir)
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: False)

    stale = {
        "version": 1,
        "kind": "execution",
        "paused_at": "2020-01-01T00:00:00+00:00",
        "interrupted_tasks": [],
        "unfinished_tasks": [],
    }
    (plan_dir / "pause_state.json").write_text(
        json.dumps(stale), encoding="utf-8",
    )
    # The write of this stop's checkpoint fails; the stale file survives.
    monkeypatch.setattr(pause_state, "atomic_write_json", lambda *a, **k: None)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")

    assert resp.status_code == 200, resp.text
    assert _checkpoint(plan_dir)["paused_at"] == stale["paused_at"]
    assert _stop_events(plan_dir) == [], (
        "an event must not be written from a checkpoint this stop did not "
        "leave behind"
    )


def test_stop_discard_writes_no_event(env, client, monkeypatch):
    """``discard=true`` throws the resume point away — nothing to audit."""
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    _mark_running(PLAN_ID, project_dir)
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: False)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop?discard=true")

    assert resp.status_code == 200, resp.text
    assert resp.json()["paused"] is False
    assert not (plan_dir / "pause_state.json").exists()
    assert _stop_events(plan_dir) == []


def test_stop_survives_audit_log_failure(env, client, monkeypatch):
    """The audit record is evidence, not control flow.

    The writer is allowed to fail — a full disk, a rotated log, a
    malformed checkpoint. None of that may turn a stop that worked into a
    stop that failed: the checkpoint is still on disk, and the operator
    still gets a 200.
    """
    plans_dir, db_path = env
    plan_dir, project_dir = _seed_plan(plans_dir, db_path)
    _mark_running(PLAN_ID, project_dir)
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: False)

    def _boom(*args, **kwargs):
        raise RuntimeError("the audit writer is having a bad day")

    monkeypatch.setattr(resume_audit_log, "log_stop_checkpoint", _boom)

    resp = client.post(f"/api/execution/{PLAN_ID}/stop")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paused"] is True
    assert body["interrupted_task_count"] == 1
    assert (plan_dir / "pause_state.json").exists(), (
        "the checkpoint must survive a failing audit writer"
    )
    assert _stop_events(plan_dir) == []

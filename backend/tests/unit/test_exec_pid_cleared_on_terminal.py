"""A terminal execution status must clear ``plan_execution.exec_pid``.

Accident B (2026-10-23): a plan was terminated, and afterwards its
``plan_execution.exec_pid`` still held 58353 — the pid of a process
that was already dead.  Every reader that asks "is this execution
alive?" (the plan summary, the planner UI card, the startup recovery
path) then saw a pid and concluded the plan was still running: a
ghost.

``exec_pid`` means "the executor subprocess that is running right now".
The moment the execution reaches a terminal status there IS no such
process, so the column has to be NULL.  What is pinned here:

  1. :meth:`ExecutionRepository.update_status` clears the pid for the
     three terminal statuses (``completed`` / ``failed`` / ``stopped``).
  2. ``update_status`` leaves the pid alone for a non-terminal status
     (``running``) — the pid is the whole point of a running execution.
  3. The stop path (``POST /api/execution/{id}/stop``) persists
     ``exec_status='stopped'`` through ``update_fields`` — NOT through
     ``update_status`` — so it has to clear the pid itself.
  4. ``update_fields`` itself enforces the rule: a terminal status
     always lands with ``exec_pid`` NULL, whoever writes it.  That is
     the only way a write point that bypasses ``update_status``
     (the spawn-failure branch, the ``project_dir`` rejection) cannot
     leave a dead pid behind.
  5. The start route's spawn-failure branch (``except OSError``) writes
     ``exec_status='failed'`` — and must leave no stale pid behind.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

import server
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import (
    ExecutionRepository,
    PlanNotFoundError,
)
from state_machine.repositories.routing_repository import RoutingRepository


PLAN_ID = "20261023-exec-pid-cleared"
DEAD_PID = 58353


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_exec_row(conn, plan_id: str = "p1") -> Dict[str, Any]:
    """The ``plan_execution`` row for ``plan_id`` as a plain dict."""
    cur = conn.execute(
        "SELECT exec_status, exec_pid FROM plan_execution WHERE plan_id = ?",
        (plan_id,),
    )
    row = cur.fetchone()
    assert row is not None, f"no plan_execution row for {plan_id!r}"
    return {"exec_status": row[0], "exec_pid": row[1]}


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "state.db"


class _FakeProc:
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


# ---------------------------------------------------------------------------
# The repository contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "stopped"])
def test_terminal_status_clears_exec_pid(db_path, terminal_status):
    """A terminal status means there is no executor process any more.

    The pid left behind is a dead pid, and every "is this execution
    alive?" reader believes a pid means alive.
    """
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = ExecutionRepository(conn)
        repo.update_fields(
            "p1", create_if_missing=True,
            exec_pid=DEAD_PID, exec_status="running",
        )

        repo.update_status("p1", terminal_status)

        row = _read_exec_row(conn)
    finally:
        conn.close()

    assert row["exec_status"] == terminal_status
    assert row["exec_pid"] is None, (
        f"exec_status={terminal_status!r} left exec_pid={row['exec_pid']!r} "
        "behind — a dead pid reads as a live execution"
    )


def test_running_status_keeps_exec_pid(db_path):
    """A running execution HAS an executor process — keep the pid.

    Clearing it here would break every "is it alive?" check that is
    supposed to say yes.
    """
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = ExecutionRepository(conn)
        repo.update_fields(
            "p1", create_if_missing=True,
            exec_pid=DEAD_PID, exec_status="running",
        )

        repo.update_status("p1", "running")

        row = _read_exec_row(conn)
    finally:
        conn.close()

    assert row["exec_status"] == "running"
    assert row["exec_pid"] == DEAD_PID, (
        "a non-terminal status must not clear the pid"
    )


def test_not_started_status_keeps_exec_pid(db_path):
    """``not_started`` is not a terminal status either."""
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = ExecutionRepository(conn)
        repo.update_fields(
            "p1", create_if_missing=True,
            exec_pid=DEAD_PID, exec_status="running",
        )

        repo.update_status("p1", "not_started")

        row = _read_exec_row(conn)
    finally:
        conn.close()

    assert row["exec_status"] == "not_started"
    assert row["exec_pid"] == DEAD_PID


def test_unknown_status_does_not_clear_exec_pid(db_path):
    """A status outside the terminal set never clears the pid.

    ``paused`` is not a recognised execution status, so the call is
    refused outright — and the refusal must not have wiped the pid on
    its way out.
    """
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = ExecutionRepository(conn)
        repo.update_fields(
            "p1", create_if_missing=True,
            exec_pid=DEAD_PID, exec_status="running",
        )

        with pytest.raises(ValueError):
            repo.update_status("p1", "paused")

        row = _read_exec_row(conn)
    finally:
        conn.close()

    assert row["exec_pid"] == DEAD_PID


def test_missing_plan_still_raises_plan_not_found(db_path):
    """The "no such plan" semantics are unchanged."""
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = ExecutionRepository(conn)

        with pytest.raises(PlanNotFoundError):
            repo.update_status("no-such-plan", "failed")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The stop path
# ---------------------------------------------------------------------------


def _seed_running_plan(plans_dir: Path, db_path: Path) -> Path:
    """A plan with a routing row, a phase and a running execution."""
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(exist_ok=True)

    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "1", "title": "first task"}]}),
        encoding="utf-8",
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": PLAN_ID,
            "current_phase": "executing",
            "completed_phases": ["execution"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, "executing")
        ExecutionRepository(conn).update_fields(
            PLAN_ID, create_if_missing=True,
            exec_pid=DEAD_PID, exec_status="running",
        )
    finally:
        conn.close()
    return plan_dir


def test_stop_path_clears_the_pid(monkeypatch, tmp_path, db_path):
    """``POST /api/execution/{id}/stop`` must not leave a dead pid.

    The stop path writes ``exec_status='stopped'`` through
    ``update_fields`` — not through ``update_status`` — so the
    repository's terminal-status rule does not fire for it.  It has to
    clear the pid itself.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = _seed_running_plan(plans_dir, db_path)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)
    monkeypatch.setattr(server, "_kill_process_tree", lambda *a, **k: True)

    server._execution_state[PLAN_ID] = {
        "status": "running",
        "logs": deque(maxlen=100),
        "project_dir": str(plan_dir / "project"),
        "process": _FakeProc(),
        "started_at": "2026-10-23T04:20:00",
        "ended_at": None,
        "pid": DEAD_PID,
        "stop_reason": None,
    }
    try:
        client = TestClient(server.app)
        resp = client.post(f"/api/execution/{PLAN_ID}/stop")
        assert resp.status_code == 200, resp.text
    finally:
        server._execution_state.pop(PLAN_ID, None)

    conn = open_db(db_path)
    try:
        row = _read_exec_row(conn, PLAN_ID)
    finally:
        conn.close()

    assert row["exec_status"] == "stopped"
    assert row["exec_pid"] is None, (
        f"the stop left exec_pid={row['exec_pid']!r} behind — the killed "
        "executor is gone, and the pid says otherwise"
    )


# ---------------------------------------------------------------------------
# The write point's own invariant
#
# `update_fields` is where every column of `plan_execution` is written.
# Several callers persist a terminal `exec_status` through it directly —
# the stop path, the `project_dir` rejection, the spawn-failure branch —
# so the "terminal status ⇒ no pid" rule has to live HERE, not only in
# `update_status`.  Otherwise the next caller that bypasses
# `update_status` re-opens the ghost-pid hole.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "stopped"])
def test_update_fields_clears_a_stale_pid_on_a_terminal_status(
    db_path, terminal_status
):
    """A terminal status lands with ``exec_pid`` NULL — always.

    Even when the caller hands in a pid: the pid belongs to a process
    that is gone the moment the status is terminal.
    """
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = ExecutionRepository(conn)
        repo.update_fields(
            "p1", create_if_missing=True,
            exec_pid=DEAD_PID, exec_status="running",
        )

        repo.update_fields(
            "p1", exec_status=terminal_status, exec_pid=DEAD_PID
        )

        row = _read_exec_row(conn)
    finally:
        conn.close()

    assert row["exec_status"] == terminal_status
    assert row["exec_pid"] is None, (
        f"update_fields wrote exec_status={terminal_status!r} but kept "
        f"exec_pid={row['exec_pid']!r} — the row reads as a live execution"
    )


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "stopped"])
def test_update_fields_clears_the_pid_when_the_caller_omits_it(
    db_path, terminal_status
):
    """A caller that writes a terminal status without a pid clears it too.

    The pid is already on the row from the run that just ended; not
    mentioning it in the write must not mean "keep it".
    """
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = ExecutionRepository(conn)
        repo.update_fields(
            "p1", create_if_missing=True,
            exec_pid=DEAD_PID, exec_status="running",
        )

        repo.update_fields("p1", exec_status=terminal_status)

        row = _read_exec_row(conn)
    finally:
        conn.close()

    assert row["exec_status"] == terminal_status
    assert row["exec_pid"] is None, (
        f"update_fields wrote exec_status={terminal_status!r} without "
        f"mentioning the pid, and left exec_pid={row['exec_pid']!r} behind"
    )


@pytest.mark.parametrize("live_status", ["running", "not_started"])
def test_update_fields_keeps_the_pid_for_a_non_terminal_status(
    db_path, live_status
):
    """A live execution keeps its pid — that is what the column is for."""
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = ExecutionRepository(conn)
        repo.update_fields(
            "p1", create_if_missing=True,
            exec_pid=DEAD_PID, exec_status="running",
        )

        repo.update_fields("p1", exec_status=live_status, exec_pid=DEAD_PID)

        row = _read_exec_row(conn)
    finally:
        conn.close()

    assert row["exec_status"] == live_status
    assert row["exec_pid"] == DEAD_PID, (
        f"exec_status={live_status!r} must keep the pid, got "
        f"{row['exec_pid']!r}"
    )


# ---------------------------------------------------------------------------
# The spawn-failure branch of the start route
# ---------------------------------------------------------------------------


def test_spawn_failure_clears_the_pid(monkeypatch, tmp_path, state_db_reader):
    """A failed spawn writes a terminal status — so the pid must go.

    The start route's ``except OSError`` branch persists
    ``exec_status='failed'`` (and ``stop_reason``) through
    ``update_fields``.  It used to leave the previous run's pid on the
    row: the HTTP answer said ``{"status": "failed", "pid": null}``
    while the row still held the dead pid — and every "is this
    execution alive?" reader believed the pid.
    """
    plan_id = "20261023-spawn-failure"
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "1", "title": "first task"}]}),
        encoding="utf-8",
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": "ready",
            "completed_phases": [],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    conn = open_db(tmp_path / "state.db")
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "queued")
        ExecutionRepository(conn).update_fields(
            plan_id, create_if_missing=True,
            exec_pid=DEAD_PID, exec_status="running",
        )
    finally:
        conn.close()

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(
        server, "_state_db_path", lambda *a, **k: tmp_path / "state.db"
    )

    def _boom(**kwargs):
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(server, "_spawn_executor_subprocess", _boom)
    server._execution_state.clear()
    try:
        client = TestClient(server.app)
        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "failed", resp.text
        assert resp.json()["pid"] is None, resp.text
    finally:
        server._execution_state.pop(plan_id, None)

    row = state_db_reader.execution(plan_id)
    assert row is not None, (
        f"the failed spawn must leave a plan_execution row for {plan_id}"
    )
    assert row["exec_status"] == "failed", (
        f"exec_status must record the failed spawn; got {row['exec_status']!r}"
    )
    assert row["exec_pid"] is None, (
        f"the failed spawn left exec_pid={row['exec_pid']!r} behind — the "
        "executor never started, and the pid says otherwise"
    )

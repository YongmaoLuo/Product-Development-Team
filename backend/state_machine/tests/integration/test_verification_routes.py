"""Integration coverage for SQLite-backed verification routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import VerificationRepository


@pytest.fixture
def verification_env(monkeypatch, tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._verification_state.clear()

    class DormantThread:
        def __init__(self, *args, **kwargs):
            self._alive = False

        def start(self):
            return None

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(server.threading, "Thread", DormantThread)

    yield plans_dir, conn

    server._verification_state.clear()
    conn.close()


def _seed_plan(
    plans_dir: Path, conn, plan_id: str, stage: str,
    max_rounds: int | None = None,
) -> None:
    """Seed a plan across the tables the verification routes read.

    2026-09-19: ``project_dir`` is seeded into ``plan_execution`` instead
    of a ``plans/<id>/execution.json`` file. ``/start`` resolves the
    project directory via ``_get_project_dir`` → ``ExecutionRepository``;
    the legacy ``execution.json`` read was deleted when SQLite became the
    single source of truth. Writing the file therefore bought nothing —
    it made ``/start`` answer ``400 Missing project directory`` and it
    tripped the conftest gate that fails any test leaving a deprecated
    state JSON behind in ``tmp_path``.

    ``max_rounds`` is written explicitly (2026-09-20) so the seeded plan
    carries a real budget instead of silently inheriting the schema's
    ``DEFAULT 3``. ``/start`` now reads that column as the plan's own
    budget, so a fixture relying on the column default would be asserting
    against a schema artifact rather than the endpoint's contract.
    """
    plan_dir = plans_dir / plan_id
    project_dir = plan_dir / "project"
    project_dir.mkdir(parents=True)
    ExecutionRepository(conn).insert(plan_id, stage, project_dir=str(project_dir))
    RoutingRepository(conn).insert(plan_id, stage)
    VerificationRepository(conn).insert(
        plan_id, "not_started",
        max_rounds=(
            server.DEFAULT_MAX_VERIFICATION_ROUNDS
            if max_rounds is None else max_rounds
        ),
    )


def test_start_verification_succeeds_from_executing(verification_env):
    plans_dir, conn = verification_env
    plan_id = "20260806-start-verification"
    _seed_plan(plans_dir, conn, plan_id, "executing")

    response = TestClient(app).post(f"/api/verification/{plan_id}/start")

    assert response.status_code == 200
    assert response.json() == {"plan_id": plan_id, "status": "started"}
    assert RoutingRepository(conn).current(plan_id)["current_phase"] == "verification_running"
    row = VerificationRepository(conn).current(plan_id)
    assert row["verification_status"] == "running"
    assert row["round"] == 1
    assert row["max_rounds"] == server.DEFAULT_MAX_VERIFICATION_ROUNDS


def test_start_without_max_rounds_keeps_the_plans_own_budget(verification_env):
    """省略 ``max_rounds`` = 不动这个计划的预算（2026-09-20）。

    显式给值才是"给这个任务换一个轮次预算"；不带就该沿用计划已有的。
    在这之前请求模型自带默认值（先是 3，后是 4），于是每一次无 body 的
    ``/start`` 都把这个全局默认值盖到计划头上 —— 一个预算 5 的计划会被
    压到 3，而把常量改成 4 之后，一个预算 3 的计划又会被抬到 4。
    默认值只能决定**从未跑过**的计划拿多少钱。
    """
    plans_dir, conn = verification_env
    plan_id = "20260806-keep-own-budget"
    _seed_plan(plans_dir, conn, plan_id, "executing", max_rounds=5)

    response = TestClient(app).post(f"/api/verification/{plan_id}/start")

    assert response.status_code == 200
    assert VerificationRepository(conn).current(plan_id)["max_rounds"] == 5


def test_start_with_an_explicit_max_rounds_sets_the_budget(verification_env):
    """显式传 ``max_rounds`` = 给这个任务一个自己的轮次预算。"""
    plans_dir, conn = verification_env
    plan_id = "20260806-explicit-budget"
    _seed_plan(plans_dir, conn, plan_id, "executing", max_rounds=5)

    response = TestClient(app).post(
        f"/api/verification/{plan_id}/start", json={"max_rounds": 2},
    )

    assert response.status_code == 200
    assert VerificationRepository(conn).current(plan_id)["max_rounds"] == 2


@pytest.mark.bug_2
def test_start_verification_409_when_already_running(verification_env):
    plans_dir, conn = verification_env
    plan_id = "20260806-start-twice"
    _seed_plan(plans_dir, conn, plan_id, "executing")
    client = TestClient(app)

    assert client.post(f"/api/verification/{plan_id}/start").status_code == 200
    response = client.post(f"/api/verification/{plan_id}/start")

    assert response.status_code == 409
    assert response.json() == {"error": "conflict", "reason": "stage_mismatch"}


def test_start_verification_409_when_stage_mismatch(verification_env):
    plans_dir, conn = verification_env
    plan_id = "20260806-interview-plan"
    _seed_plan(plans_dir, conn, plan_id, "interview")

    response = TestClient(app).post(f"/api/verification/{plan_id}/start")

    assert response.status_code == 409
    assert response.json() == {"error": "conflict", "reason": "stage_mismatch"}


def test_start_verification_410_for_archived_plan(verification_env):
    plans_dir, conn = verification_env
    plan_id = "20260801-archived-plan"
    _seed_plan(plans_dir, conn, plan_id, "executing")

    response = TestClient(app).post(f"/api/verification/{plan_id}/start")

    assert response.status_code == 410
    assert response.json() == {"error": "archived"}


def test_stop_verification_409_when_not_running(verification_env):
    plans_dir, conn = verification_env
    plan_id = "20260806-idle-verification"
    _seed_plan(plans_dir, conn, plan_id, "executing")

    response = TestClient(app).post(f"/api/verification/{plan_id}/stop")

    assert response.status_code == 409
    assert response.json() == {"error": "conflict", "reason": "stage_mismatch"}


def test_status_reflects_db_state(verification_env):
    plans_dir, conn = verification_env
    plan_id = "20260806-db-status"
    _seed_plan(plans_dir, conn, plan_id, "executing")
    server._verification_state[plan_id] = {
        "verification_status": "passed",
        "verification_round": 99,
    }

    VerificationRepository(conn).init_round(plan_id, round_n=2, max_rounds=5)
    RoutingRepository(conn).try_mark_phase(
        plan_id, ("executing",), "verification_running"
    )
    response = TestClient(app).get(f"/api/verification/{plan_id}/status")

    assert response.status_code == 200
    data = response.json()
    assert data["verification_status"] == "running"
    assert data["verification_round"] == 2
    assert data["verification_max_rounds"] == 5
    assert data["current_phase"] == "verification_running"

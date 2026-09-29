"""``POST /api/verification/{plan_id}/reset`` — the manual restart (2026-09-17).

A restart sets the verification state directly instead of walking the
state machine from one point to the next. The transition machinery is
what lets the machine drift into states nobody asked for, and it makes
every restart depend on the transitions the current stage happens to
allow.

Two properties carry the design, and both are pinned here:

1. **Direct writes, no transitions.** A reset is not a workflow step.
   Routing it through ``transition_to`` means every restart must satisfy
   whatever the current stage's transition table happens to allow — which
   is exactly where the drift comes from. The endpoint writes the
   post-reset state instead.
2. **A fixed procedure.** "Clear what should be cleared" is a specific,
   enumerable list: in-memory state, the verdict/runtime columns, the
   routing stage, the repair tasks generated from the discarded verdicts,
   and the per-round artifacts on disk. A reset that misses one of them
   is not a reset — the 2026-09-15 note on ``reset_round_counter`` records
   the incident where a surviving ``results`` envelope let
   ``repair_stale_terminal_state`` re-derive the OLD terminal verdict on
   the very next ``/progress`` read.

   **One thing is deliberately NOT cleared: ``verification_plan.json``**
   (2026-09-19). The VP list is positionally keyed (``VP-0NN``);
   regenerating recycles those ids onto different verification points,
   which re-attributes every VP-id-keyed cross-batch artefact to the wrong
   VP. A reset now rewinds the round counter and leaves the VP list alone;
   regenerating is opt-in via ``clear_verification_plan``.

The end-to-end assertion that matters most is the last one: after a reset,
``/start`` admits the plan again.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.plan_task_repository import PlanTaskRepository
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)

PLAN_ID = "20260915-reset-target"


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


def _seed_mid_flight_plan(plans_dir: Path, db_path: Path, *, max_rounds: int = 3):
    """A plan mid-verification with everything a reset is supposed to clear.

    Returns ``(plan_dir, conn)``. A SECOND connection is used (not the
    fixture's) so the rows the endpoint writes are re-read fresh — the
    fixture connection would serve a stale snapshot.
    """
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    (plan_dir / "screenshots").mkdir(exist_ok=True)

    conn = open_db(db_path)
    migrate(conn)
    RoutingRepository(conn).insert(PLAN_ID, "verification_running")
    ExecutionRepository(conn).insert(
        plan_id=PLAN_ID, current_phase="verification",
        project_dir=str(plan_dir / "project"),
    )
    verif = VerificationRepository(conn)
    verif.insert(PLAN_ID, verification_status="failed", max_rounds=max_rounds)
    verif.init_round(PLAN_ID, round_n=2, max_rounds=max_rounds)
    verif._update(
        PLAN_ID,
        verification_status="failed",
        verification_stop_reason="no_repair_tasks",
        results=json.dumps({"status": "failed", "stop_reason": "no_repair_tasks"}),
        verdicts=json.dumps([{"id": "VP-001", "status": "FAILED"}]),
        runtime_state=json.dumps({"current_vp": "VP-003"}),
        progress_state=json.dumps({"current_vp": "VP-003"}),
        execution_results=json.dumps({"verification_points": []}),
    )

    tasks = PlanTaskRepository(conn)
    tasks.add_task(PLAN_ID, {"id": "RP-1", "title": "repair one",
                             "task_group": "repair"})
    tasks.add_task(PLAN_ID, {"id": "1-1", "title": "normal task"})
    with conn:
        conn.execute(
            "UPDATE plan_tasks SET status = 'pending' WHERE plan_id = ?",
            (PLAN_ID,),
        )
    conn.close()

    for rel in (
        "verification_plan.json",
        "verification_report.json",
        "verification_execution_results.json",
        "verification_repair_tasks.json",
        # 2026-09-19: the cross-round same-failure tracking. A reset must
        # drop it — it counts repeats of the verdict set being discarded,
        # so inheriting it would stop the next round on a comparison
        # against a plan generation that no longer exists.
        "verification_loop_tracking.json",
    ):
        (plan_dir / rel).write_text("{}", encoding="utf-8")
    (plan_dir / "logs" / "verification_1_20260915.log").write_text("{}", encoding="utf-8")
    (plan_dir / "screenshots" / "verification_1_VP-001.png").write_bytes(b"x")
    return plan_dir


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


def _task_status(db_path: Path, task_id: str) -> str | None:
    conn = open_db(db_path)
    try:
        cur = conn.execute(
            "SELECT status FROM plan_tasks WHERE plan_id = ? AND task_id = ?",
            (PLAN_ID, task_id),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The happy path — "clear what should be cleared"
# ---------------------------------------------------------------------------


def test_reset_clears_the_verification_row(env, client):
    plans_dir, _conn, db_path = env
    _seed_mid_flight_plan(plans_dir, db_path)

    resp = client.post(f"/api/verification/{PLAN_ID}/reset", json={})
    assert resp.status_code == 200, resp.text

    row = _row(db_path, "plan_verification")
    assert row["verification_status"] == "pending"
    assert row["round"] == 0, "restart_at_round defaults to 1 → counter 0"
    assert row["verification_stop_reason"] is None
    for column in ("results", "verdicts", "runtime_state",
                   "progress_state", "execution_results"):
        assert row[column] in (None, ""), (
            f"{column} survived the reset — the 2026-09-15 incident was a "
            f"stale `results` envelope letting repair_stale_terminal_state "
            f"re-derive the old terminal verdict"
        )


def test_reset_leaves_the_routing_row_where_start_can_take_it(env, client):
    """``verification`` is in /start's CAS set — that is the point.

    A reset that landed the row anywhere else would make the operator's
    next ``/start`` 409 on a stage nothing can leave.
    """
    plans_dir, _conn, db_path = env
    _seed_mid_flight_plan(plans_dir, db_path)
    before = _row(db_path, "plan_routing")

    resp = client.post(f"/api/verification/{PLAN_ID}/reset", json={})
    assert resp.status_code == 200, resp.text

    after = _row(db_path, "plan_routing")
    assert after["current_phase"] == "verification"
    assert after["version"] > before["version"], (
        "the version must advance so CAS readers that snapshotted it "
        "cannot still match"
    )


def test_reset_supersedes_repair_tasks_but_never_deletes_them(env, client):
    """Repairs generated from the discarded verdicts must not run.

    Task-level data may only be voided or created, never rewritten — so
    the row is marked ``superseded`` and kept, not removed.
    """
    plans_dir, _conn, db_path = env
    _seed_mid_flight_plan(plans_dir, db_path)
    assert _task_status(db_path, "RP-1") == "pending"

    resp = client.post(f"/api/verification/{PLAN_ID}/reset", json={})
    assert resp.status_code == 200, resp.text

    assert _task_status(db_path, "RP-1") == "superseded"
    assert _task_status(db_path, "1-1") == "pending", (
        "a non-repair task is the executor's work, not verification's — "
        "the reset must not void it"
    )
    assert resp.json()["cleared"]["superseded_repair_tasks"] == ["RP-1"]


def test_reset_removes_the_per_round_artifacts(env, client):
    plans_dir, _conn, db_path = env
    plan_dir = _seed_mid_flight_plan(plans_dir, db_path)

    resp = client.post(f"/api/verification/{PLAN_ID}/reset", json={})
    assert resp.status_code == 200, resp.text

    for rel in ("verification_report.json",
                "verification_execution_results.json",
                "verification_repair_tasks.json",
                "verification_loop_tracking.json"):
        assert not (plan_dir / rel).exists(), f"{rel} survived the reset"
    assert list((plan_dir / "logs").glob("verification_*")) == []
    assert list((plan_dir / "screenshots").glob("verification_*")) == []
    # The VP list is NOT among them any more — see
    # ``test_reset_keeps_the_vp_list_and_only_rewinds_the_counter``.
    assert (plan_dir / "verification_plan.json").exists()
    assert len(resp.json()["cleared"]["disk_artifacts"]) == 6


def test_reset_keeps_the_vp_list_and_only_rewinds_the_counter(env, client):
    """重置 = 计数器归零，VP 清单一动不动（2026-09-19）。

    VP 编号是位置式的（``VP-0NN``）。重新生成会把编号回收给**完全不同**的
    验证点（一次实测：20 条 → 23 条，VP-004 从"已发出的底部信号仍
    正常发出"变成"底部信号正常发出（第 3 个信号，item_index=161）"），于是
    所有按 VP id 索引的跨批次产物都把上一批的裁决挂到了这一批的同名 VP 上
    —— 首当其冲就是修复 prompt 读的 ``verification_failure_history.json``。

    这个契约锁两件事，缺一不可：文件**逐字节**没动，且计数器真的归零了。
    """
    plans_dir, _conn, db_path = env
    plan_dir = _seed_mid_flight_plan(plans_dir, db_path)
    vp_list = json.dumps(
        {"services": [{"name": "api", "port": 8652}],
         "verification_points": [
             {"id": "VP-004", "title": "9/2 13:41 底部信号正常发出",
              "verification_method": "api_test"}]},
        ensure_ascii=False,
    )
    (plan_dir / "verification_plan.json").write_text(vp_list, encoding="utf-8")

    resp = client.post(f"/api/verification/{PLAN_ID}/reset", json={})
    assert resp.status_code == 200, resp.text

    assert (plan_dir / "verification_plan.json").read_text(
        encoding="utf-8"
    ) == vp_list, "reset 不该碰 VP 清单"
    assert "verification_plan.json" not in resp.json()["cleared"]["disk_artifacts"]
    assert _row(db_path, "plan_verification")["round"] == 0, (
        "默认 restart_at_round=1 存成 completed-rounds=0 —— 这才是 reset 该做的"
    )


def test_clear_verification_plan_opts_back_into_regeneration(env, client):
    """想从零重生成 VP 仍然可以，但要显式开口。

    改这一条之前，"删 VP 清单"是 reset 的隐式副作用：操作者按一次 reset，
    下一轮 Phase-1 就悄悄换一套 VP。现在它是一个要主动写出来的选择 ——
    计划本身重生成过（PRD 改了、任务重排了）时才需要。
    """
    plans_dir, _conn, db_path = env
    plan_dir = _seed_mid_flight_plan(plans_dir, db_path)

    resp = client.post(
        f"/api/verification/{PLAN_ID}/reset",
        json={"clear_verification_plan": True},
    )
    assert resp.status_code == 200, resp.text
    assert not (plan_dir / "verification_plan.json").exists()
    assert "verification_plan.json" in resp.json()["cleared"]["disk_artifacts"]


def test_reset_drops_the_in_memory_state(env, client):
    plans_dir, _conn, db_path = env
    _seed_mid_flight_plan(plans_dir, db_path)
    server._verification_state[PLAN_ID] = {
        "verification_status": "failed",
        "verification_round": 2,
        "stop_reason": "no_repair_tasks",
        "thread": None,
    }

    resp = client.post(f"/api/verification/{PLAN_ID}/reset", json={})
    assert resp.status_code == 200, resp.text

    assert PLAN_ID not in server._verification_state, (
        "a surviving in-memory entry is how a reset silently un-resets: "
        "/status keeps reporting the old verdict from it"
    )
    assert resp.json()["cleared"]["in_memory_state"] is True


# ---------------------------------------------------------------------------
# The end-to-end claim: the plan is restartable
# ---------------------------------------------------------------------------


def test_start_is_admitted_after_a_reset(env, client, monkeypatch):
    """The whole point. Before the reset the routing stage blocks /start.

    The 200 is the assertion that matters: ``/start``'s stage CAS is what
    refused the plan before, and ``verification`` is the stage that
    lets it through. (The auto-loop itself is not asserted on here — the
    ``env`` fixture stubs ``threading.Thread`` to a dormant double, so the
    dispatch thread never runs, and that is deliberate: a real one would
    spawn verification subprocesses.)
    """
    plans_dir, _conn, db_path = env
    _seed_mid_flight_plan(plans_dir, db_path)

    def _fake_loop(plan_id, plan_dir, project_dir, **kwargs):
        raise AssertionError("the auto-loop must not run in this test")

    monkeypatch.setattr(server, "_run_auto_verification_loop", _fake_loop)
    monkeypatch.setattr(server, "_recover_verification_states", lambda *a, **k: None)

    resp = client.post(f"/api/verification/{PLAN_ID}/reset", json={})
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/api/verification/{PLAN_ID}/start")
    assert resp.status_code == 200, (
        f"/start was refused after a reset: {resp.status_code} {resp.text}"
    )


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_a_live_round_is_refused_unless_forced(env, client):
    plans_dir, _conn, db_path = env
    _seed_mid_flight_plan(plans_dir, db_path)
    server._verification_state[PLAN_ID] = {
        "verification_status": "running",
        "thread": None,
    }

    resp = client.post(f"/api/verification/{PLAN_ID}/reset", json={})
    assert resp.status_code == 409
    # The app's custom HTTPException handler UNWRAPS a dict detail, so the
    # body is the dict itself (server.py:923 ``content=exc.detail``).
    assert resp.json()["error"] == "Already running"
    # The refusal must be clean — nothing cleared.
    assert _row(db_path, "plan_verification")["verification_status"] == "failed"


def test_force_cancels_the_live_round_and_resets(env, client):
    plans_dir, _conn, db_path = env
    _seed_mid_flight_plan(plans_dir, db_path)
    server._verification_state[PLAN_ID] = {
        "verification_status": "running",
        "thread": None,
    }

    resp = client.post(
        f"/api/verification/{PLAN_ID}/reset", json={"force": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cancelled_live_round"] is True
    assert _row(db_path, "plan_verification")["verification_status"] == "pending"


def test_restart_round_above_the_immutable_cap_is_refused(env, client):
    """``max_rounds`` is the plan's budget — a reset does not rewrite it."""
    plans_dir, _conn, db_path = env
    _seed_mid_flight_plan(plans_dir, db_path, max_rounds=3)

    resp = client.post(
        f"/api/verification/{PLAN_ID}/reset", json={"restart_at_round": 4},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "restart_round_above_cap"
    assert _row(db_path, "plan_verification")["round"] == 2, "nothing changed"


def test_restart_at_round_selects_where_the_next_start_begins(env, client):
    plans_dir, _conn, db_path = env
    _seed_mid_flight_plan(plans_dir, db_path, max_rounds=3)

    resp = client.post(
        f"/api/verification/{PLAN_ID}/reset", json={"restart_at_round": 2},
    )
    assert resp.status_code == 200, resp.text
    # Stored as completed-rounds so the next /start begins AT round 2.
    assert _row(db_path, "plan_verification")["round"] == 1
    assert resp.json()["restart_at_round"] == 2


def test_clear_artifacts_can_be_turned_off(env, client):
    plans_dir, _conn, db_path = env
    plan_dir = _seed_mid_flight_plan(plans_dir, db_path)

    resp = client.post(
        f"/api/verification/{PLAN_ID}/reset", json={"clear_artifacts": False},
    )
    assert resp.status_code == 200, resp.text
    assert (plan_dir / "verification_plan.json").exists()
    assert resp.json()["cleared"]["disk_artifacts"] == []


def test_supersede_can_be_turned_off(env, client):
    plans_dir, _conn, db_path = env
    _seed_mid_flight_plan(plans_dir, db_path)

    resp = client.post(
        f"/api/verification/{PLAN_ID}/reset",
        json={"supersede_repair_tasks": False},
    )
    assert resp.status_code == 200, resp.text
    assert _task_status(db_path, "RP-1") == "pending"


def test_unknown_plan_is_a_404(env, client):
    resp = client.post("/api/verification/no-such-plan/reset", json={})
    assert resp.status_code == 404

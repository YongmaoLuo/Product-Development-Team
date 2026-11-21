"""事故回归：``POST /stop`` 不得改写终态验证行已经写下的停止原因。

事故
----
某个 plan 的第 2 轮早就以
``base_drift_conflicts`` 结束了 —— ``plan_verification`` 行上是
``verification_status='loop_stopped'`` +
``verification_stop_reason='base_drift_conflicts'``。随后操作员按了一次
``POST /api/verification/{plan_id}/stop``，那一刀把原因改写成了
``user_stopped``：真正的死因从盘上消失了，事后只看得到"用户按了停止"。

一条链，三层
------------
一次 ``/stop`` 走过三步，三步各有各的职责，混在一起就会互相吃掉：

  1. **路由 CAS** —— ``plan_routing.current_phase`` 从
     ``verification_running`` 挪到计划级的 ``user_stopping``；计划真的
     安静下来之后，后台层才把它停到 ``verification``。停的是**轮次**，
     不是计划；计划还在，只是这一轮暂停了。
  2. **仓库层** —— ``mark_stopped()`` 对**已经是终态**的行只写一次：
     第一次的原因留下，后来的覆盖只被记进日志，不落盘。这一步现在由
     后台层做 —— 所以断言行的那几条用例要等停止收尾之后再读盘。
  3. **检查点** —— ``pause_state.json`` 的 ``reason`` 来自**调用方**：
     ``/stop`` 自己说 ``user_stopped``，检查点就写 ``user_stopped``。

所以一次 ``/stop`` 之后，盘上是**两个不同的真相**，而且都是真的：

  * 行里的**死因**（``base_drift_conflicts``）—— 这一轮为什么结束的，
    不许被后来的按键改写；
  * 检查点里的**这次暂停的原因**（``user_stopped``）—— 这一次暂停是
    因为什么。

把两者混成一个（行里也写成 ``user_stopped``）就是事故本身。

``plan_closed`` 只能发一次
--------------------------
这一轮已经关过了。再发一次 ``plan_closed`` 就是对同一个计划宣告两次
"结束"，卡片会被推到终态之外。

种子的写法（计数的前提）
------------------------
终态行用 ``VerificationRepository.insert()`` **直写**，不用
``mark_stopped()`` —— 后者自己就会发一次 ``plan_closed``，把计数搅浑，
于是"这次 stop 到底发了几次"这个问题就再也答不清了。

上游对应：arch 决策点 1（终态原因只写一次）、arch 决策点 3（检查点原因
来自调用方）、test 决策点 2（``plan_closed`` 不重复）、test 决策点 4
（暂停检查点不写死）、test 决策点 5（相关链路全绿）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

import server
import stop_state
import stop_supervisor
import verification_cancel
from pause_state import checkpoint_path
from server import app
from state_machine.db.connection import open as open_db
from state_machine.db.schema import migrate
from state_machine.repositories.execution_repository import ExecutionRepository
from state_machine.repositories.routing_repository import RoutingRepository
from state_machine.repositories.verification_repository import (
    VerificationRepository,
)


PLAN_ID = "20261103-stop-reason-kept"

#: The quiet window the background layer waits for before it may park
#: the plan.  Short here on purpose: the window is a policy of the
#: deployment, not a contract of the stop.
QUIET_SECONDS = 0.2

#: 这一轮**已经**结束时的原因 —— 事故里被覆盖掉的那一个。
TERMINAL_REASON = "base_drift_conflicts"

#: ``/stop`` 自己的原因 —— 操作员按了停止，这是调用方的事实。
STOP_REASON = "user_stopped"

#: 这一轮的轮次号。停的是第 2 轮。
ROUND_N = 2

#: 这一轮已经判完的 VP。``VP-002`` 没有判词 —— 它就是被这次 stop 打断的
#: 那个 VP，也是 resume 还欠着的那个。
VP_IDS = ["VP-001", "VP-002"]

#: 这一轮已经写下的判词（executor 的形状：``vp_id`` / ``status`` /
#: ``reasons`` / ``evidence``）。
ROUND_VERDICTS = [
    {
        "vp_id": "VP-001",
        "status": "PASSED",
        "worker_id": "executor",
        "seq": 1,
        "reasons": [],
        "evidence": {},
    },
]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_stop_state(monkeypatch):
    """stop 的进程级状态是全局的 —— 不许漏到下一个用例。

    ``stop_state``（停止记录）、``stop_supervisor``（监督线程）和
    ``verification_cancel``（取消标志）都是进程级的、按 plan_id 索引的，
    而这些用例复用同一个 ``PLAN_ID`` —— 上一个用例留下的记录或监督线程
    会决定下一个用例的 stop 行为。

    静默窗口是部署的策略、不是 stop 的契约，所以这里把它调短：用例不该
    为每次 stop 付五秒钟。
    """
    real_start = stop_supervisor.start

    def _start(plan_id, **kwargs):
        kwargs["quiet_seconds"] = QUIET_SECONDS
        return real_start(plan_id, **kwargs)

    monkeypatch.setattr(stop_supervisor, "start", _start)

    stop_state._reset_for_tests()
    stop_supervisor._reset_for_tests()
    verification_cancel._reset_for_tests()
    yield
    stop_supervisor._reset_for_tests()
    stop_state._reset_for_tests()
    verification_cancel._reset_for_tests()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """每个用例自己的 plans 目录 + state.db。"""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"
    conn = open_db(db_path)
    migrate(conn)
    conn.close()

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(server, "_state_db_path", lambda request=None: db_path)
    server._execution_state.clear()
    server._verification_state.clear()

    yield plans_dir, db_path

    server._execution_state.clear()
    server._verification_state.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def published(monkeypatch):
    """记录这次请求期间发出的每一个状态事件。

    ``publish_safe`` 是仓库层唯一的出口（``VerificationRepository``
    在调用时才 import 它），所以在这里替换它就能数到 ``plan_closed``。
    """
    import notifications.state_events as state_events

    events: List[tuple] = []

    def _record(kind, plan_id, **payload):
        events.append((kind, plan_id, payload))

    monkeypatch.setattr(state_events, "publish_safe", _record)
    return events


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed(
    plans_dir: Path,
    db_path: Path,
    *,
    phase: str = "verification_running",
) -> Path:
    """种一个"轮次已结束、但路由行还停在 running"的计划。

    ``plan_verification`` 行是**直写**的终态行（``insert``），理由已经
    在那儿了 —— 这正是事故发生时的盘上状态。用 ``mark_stopped()`` 种子
    会先发一次 ``plan_closed``，把后面要数的计数搅浑。

    行的 ``results`` 是空的：``results.recorded_by`` 这个标记只有
    ``_persist_verification_terminal`` 才会盖，而 ``mark_stopped()``
    故意不写 ``results``。终态行缺这个标记是合法的 —— 它不许让 stop
    抛出一致性异常。

    返回 ``plan_dir``。
    """
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "logs").mkdir(exist_ok=True)
    project_dir = plan_dir / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    conn = open_db(db_path)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, phase)
        ExecutionRepository(conn).insert(
            plan_id=PLAN_ID, project_dir=str(project_dir),
        )
        VerificationRepository(conn).insert(
            PLAN_ID,
            verification_status="loop_stopped",
            round=ROUND_N,
            max_rounds=3,
            verification_stop_reason=TERMINAL_REASON,
            verdicts=[dict(v) for v in ROUND_VERDICTS],
        )
    finally:
        conn.close()

    (plan_dir / "verification_plan.json").write_text(
        json.dumps(
            {
                "verification_points": [
                    {"id": vp_id, "title": f"check {vp_id}"} for vp_id in VP_IDS
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_dir


def _read_row(db_path: Path) -> Dict[str, Any]:
    """在自己的连接上把 ``plan_verification`` 行读回来。"""
    conn = open_db(db_path)
    try:
        return VerificationRepository(conn).current(PLAN_ID) or {}
    finally:
        conn.close()


def _read_phase(db_path: Path) -> str:
    """路由行的 ``current_phase`` —— 在自己的连接上读回来。"""
    conn = open_db(db_path)
    try:
        row = RoutingRepository(conn).current(PLAN_ID)
    finally:
        conn.close()
    return (row or {}).get("current_phase")


def _wait_for_stop_to_settle(db_path: Path, timeout: float = 60.0) -> str:
    """等后台层给出裁决，再把相位读回来。

    停止是一个**过程**：请求只关闸门、回答 ``stopping``，把相位停到
    计划级的 ``user_stopping``；真正把相位停到别处的是后台层 —— 所以
    相位是轮询出来的，不是假定的。

    停止记录也一起等：``/start`` 在停止还在进行时会拒它，而记录正是在
    后台层收尾时才清掉的。
    """
    deadline = time.monotonic() + timeout
    phase = _read_phase(db_path)
    while time.monotonic() < deadline:
        if phase != "user_stopping" and stop_state.get(PLAN_ID) is None:
            return phase
        time.sleep(0.05)
        phase = _read_phase(db_path)
    raise AssertionError(
        f"停止始终没有收尾 —— {timeout:.0f}s 之后计划还停在 {phase!r}"
    )


def _read_checkpoint(plan_dir: Path) -> Dict[str, Any]:
    """检查点读回来 —— 断言的是**盘上的东西**，不是返回值。"""
    return json.loads(checkpoint_path(plan_dir).read_text(encoding="utf-8"))


def _plan_closed(events) -> list:
    return [e for e in events if e[0] == "plan_closed"]


# ---------------------------------------------------------------------------
# 1) 行里的死因不被改写
# ---------------------------------------------------------------------------


def test_stop_does_not_overwrite_terminal_reason(env, client, published):
    """``/stop`` 之后，行里的原因仍然是 ``base_drift_conflicts``。

    事故就在这一行上：轮次早就以 ``base_drift_conflicts`` 结束了，
    ``/stop`` 把它改写成 ``user_stopped``，死因消失。

    断言的是**盘上的行**（自己的连接上读回来），不是接口返回体 ——
    返回体从来不承诺这件事，只有行才是权威。
    """
    plans_dir, db_path = env
    _seed(plans_dir, db_path)

    resp = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    # 终态写发生在**后台层**了：请求只关闸门。所以行是在停止收尾之后
    # 读的 —— 否则这条断言跑在它要断言的那次写之前。
    _wait_for_stop_to_settle(db_path)

    row = _read_row(db_path)
    assert row["verification_stop_reason"] == TERMINAL_REASON, (
        "the stop rewrote the terminal round's stop reason — the real "
        f"cause of the termination ({TERMINAL_REASON!r}) is gone from the "
        f"row and only the later keypress ({STOP_REASON!r}) is left: {row}"
    )
    assert row["verification_status"] == "loop_stopped", row


def test_stop_on_terminal_row_without_recorded_by_does_not_raise(
    env, client, published,
):
    """终态行缺 ``results.recorded_by`` → 不得触发一致性异常。

    ``results.recorded_by`` 只有 ``_persist_verification_terminal`` 会盖，
    而 ``mark_stopped()`` 故意不写 ``results``。所以一个"已经结束、
    但不是从终态持久化那条路结束的"轮次，行上就是没有这个标记 ——
    这是合法的状态，不许让 ``/stop`` 抛异常。

    一致性异常会一路冒到路由层变成 500，所以这里 200 本身就是断言。
    """
    plans_dir, db_path = env
    _seed(plans_dir, db_path)

    row_before = _read_row(db_path)
    assert row_before.get("results") is None, (
        "the seed must not carry results.recorded_by — that missing "
        "marker is exactly the boundary this case pins"
    )

    resp = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert resp.status_code == 200, (
        "the stop raised on a terminal row without results.recorded_by — "
        f"a legitimate state (mark_stopped deliberately writes no "
        f"results) must not take the endpoint down: {resp.status_code} "
        f"{resp.text}"
    )

    # The background layer's verdict must be reached too — a stop that
    # could not park the plan is a stop that did not hold.
    assert _wait_for_stop_to_settle(db_path) == "verification"


# ---------------------------------------------------------------------------
# 2) 检查点记的是这次暂停自己的原因
# ---------------------------------------------------------------------------


def test_stop_checkpoint_records_its_own_reason(env, client, published):
    """检查点的 ``reason`` 是 ``user_stopped`` —— 调用方自己说的。

    检查点和行说的是**两件事**：行说"这一轮为什么结束"，检查点说"这次
    暂停是因为什么"。所以行是 ``base_drift_conflicts``、检查点是
    ``user_stopped``，两个同时为真，不矛盾。

    检查点的原因是**调用方给的**，不是函数替调用方编的 —— 所以这里断言
    的正是 ``/stop`` 传下来的那一个。
    """
    plans_dir, db_path = env
    plan_dir = _seed(plans_dir, db_path)

    resp = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    checkpoint = _read_checkpoint(plan_dir)
    assert checkpoint["reason"] == STOP_REASON, (
        "the pause checkpoint does not carry the caller's reason — a "
        f"checkpoint that says {checkpoint.get('reason')!r} misreports "
        "why the round was interrupted: " + json.dumps(
            checkpoint, ensure_ascii=False,
        )
    )
    # 任务级的 reason 与检查点级的是同一个事实，两者不能互相矛盾。
    interrupted = checkpoint.get("interrupted_tasks") or []
    assert interrupted, (
        "the checkpoint records no interrupted task — the VP the stop cut "
        "short (VP-002) is what a resume still owes, and it must be on "
        "the checkpoint"
    )
    assert {t.get("reason") for t in interrupted} == {STOP_REASON}, interrupted


# ---------------------------------------------------------------------------
# 3) 终态行上不再发第二次 plan_closed
# ---------------------------------------------------------------------------


def test_stop_on_terminal_row_publishes_no_second_plan_closed(
    env, client, published,
):
    """这一轮已经关过了 —— 这次 stop 不许再发一次 ``plan_closed``。

    种子用 ``insert()`` 直写终态行，所以 ``published`` 里出现的每一次
    ``plan_closed`` 都来自**这次 stop**。一条都不该有。
    """
    plans_dir, db_path = env
    _seed(plans_dir, db_path)

    resp = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text

    # The terminal write is the background layer's now — so the count
    # is taken after the stop has settled, or it would count nothing
    # at all.
    _wait_for_stop_to_settle(db_path)

    closed = _plan_closed(published)
    assert closed == [], (
        "the stop announced the plan closed again — this round was "
        "already closed, and a second plan_closed tells every subscriber "
        f"the plan ended twice: {closed}"
    )


# ---------------------------------------------------------------------------
# 4) 边界：这条链的其余几格
# ---------------------------------------------------------------------------


def test_stop_reads_db_row_when_in_memory_state_is_empty(env, client, published):
    """``_verification_state`` 为空 → 走 DB 行，仍返回 stopping/paused。

    停的依据是**路由行的 CAS**，不是内存里的 ``_verification_state`` ——
    后端重启之后内存是空的，但行还在，stop 必须照样工作。
    """
    plans_dir, db_path = env
    _seed(plans_dir, db_path)

    assert server._verification_state.get(PLAN_ID) is None, (
        "the seed must not leave an in-memory verification state — the "
        "empty-state path is exactly what this case pins"
    )

    resp = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The stop is a process: the request closes the gate and answers
    # "stopping" — the park is the background layer's verdict.
    assert body["status"] == "stopping", body
    assert body["paused"] is True, body
    assert body["round"] == ROUND_N, body

    # And the background layer does park it — the plan leaves the
    # stopping phase at the phase it was interrupted in.
    assert _wait_for_stop_to_settle(db_path) == "verification"


def test_stop_on_non_verification_phase_still_409(env, client, published):
    """routing 不在 ``verification_running``/``repairing`` → 409，行为不变。

    这次修复动的是"写什么原因"，不是"什么时候能停"。一个停在
    ``executing`` 的计划依然不能被 ``/stop`` 停 —— 那是另一套语义，
    这次一个字都没动。

    409 之后行里的原因也不能变：那一次 stop 压根没发生过。
    """
    plans_dir, db_path = env
    _seed(plans_dir, db_path, phase="executing")

    resp = client.post(f"/api/verification/{PLAN_ID}/stop")
    assert resp.status_code == 409, (
        "a plan that is not in a verification phase was stopped anyway — "
        f"the stop's source set changed: {resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert body["error"] == "conflict", body

    row = _read_row(db_path)
    assert row["verification_stop_reason"] == TERMINAL_REASON, (
        "the refused stop still touched the verification row — a 409 must "
        f"leave every row exactly as it was: {row}"
    )
    assert _plan_closed(published) == [], (
        "the refused stop announced a close — a 409 must not publish "
        "anything"
    )
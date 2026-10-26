"""统一 stop/start 门面与既有端点的等价性（R11 · 任务 21）。

背景
----
门面（``POST /api/plan/{id}/lifecycle/{stop,start}``）与既有端点
（``POST /api/execution/{id}/{stop,start}``、``POST /api/verification/{id}/{stop,start}``）
是**同一个动作的两条门**。调度器只认门面，既有调用方只认旧端点 —— 两条
路径上的行为一旦分叉，同一个计划就会在两个地方显示出两种状态，而没有人
会立刻发现。

这个文件钉的就是那条分叉线。

怎么比
------
每个场景播**两份完全相同的计划**（同 ``tasks.json``、同检查点、同 routing
相位），只差 ``plan_id``：一份给旧端点，一份给门面。然后比**三元组**：

1. **HTTP 状态码** —— 调用方直接看见的那一个；
2. **routing 相位**（``plan_routing.current_phase``）—— 相位的唯一真相，
   调度器读的就是这一列；
3. **``_execution_state`` 里有没有这个 plan** —— 门面说「停了」而执行记录
   还在，就是「停了但没停干净」。

再比一样**产物**：两条路各自留下的 ``pause_state.json`` 的 ``kind``（没有
就是没有）—— 恢复从哪儿继续由它说了算，两条门必须指向同一边。

为什么是这几样：状态码是调用方的第一手观察；相位是所有读盘的人共享的
那一列；``_execution_state`` 是进程内「执行记录还在不在」的唯一答案；
检查点的 kind 是恢复路径的唯一输入。少一样，就会漏掉一种分叉。

六个场景
--------
``execution_in_flight_stop`` / ``verification_in_flight_stop`` / ``idle_stop``
（三个 stop）与 ``execution_checkpoint_start`` /
``verification_checkpoint_start`` / ``no_checkpoint_start``（三个 start）。
前三个问「谁在跑就停谁」（``idle_stop`` 问的是「没人在跑的时候停会怎样」——
一个**已经停住**的计划再被停一次，两条门都必须原样留着 ``stopped``），
后三个问「按检查点的 kind 回到被打断的那一侧」。

每个场景的**期望答案都是两条路共同的那一个** —— 这个文件里没有「这条门
例外」的分支。调度器在两条路径上看到两种结果，正是这层门面要消灭的分裂；
哪条门的行为变了，这个文件就先红。

被测的是**两条路的产物**，不是门面内部怎么分派（那是任务 18 / 19 的单元
测试钉的东西）。这里钉的是：调用方从两条门看到的**同一个答案**。

不变量（每个用例自己负责）
--------------------------
本文件会起假进程（执行器）与假线程（验证轮次）。每一个用例都必须把
自己起过的东西放掉 —— 否则下一个用例会看到一个「还在跑」的计划，而那
是上一个用例的残留，不是它自己播的。``process_state_guard`` 这个
autouse fixture 负责收尾：放掉假进程、把 ``_execution_state`` /
``_verification_state`` 恢复成用例开始前的样子。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient

import server
from server import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# 假执行器进程
# ---------------------------------------------------------------------------


class FakeProcess:
    """一个「活着、被 terminate 就退出」的假执行器进程。

    真 spawn 会拉起一个真的 ``cli.py`` 子进程（会去做 LLM 调用）。这条
    等价性关心的是「两条路对同一个计划给出的答案一样」，执行器是不是真的
    跑起来与那条等价性无关 —— 但假进程必须由用例自己放掉。

    ``wait()`` 立刻返回：``stop`` 的宽限期默认 10 秒，一个会真的阻塞的
    假进程会让每个用例慢 10 秒。这里模拟的是「执行器响应了 SIGTERM」
    （真实执行器里最常见的那种退出）。
    """

    #: 取一个**不可能存在**的 pid：``stop`` 会拿 ``proc.pid`` 去问
    #: ``os.getpgid``，问一个真实存在的 pid 会在别的进程头上动刀。
    PID = 4_194_303

    def __init__(self) -> None:
        self.pid = self.PID
        self._alive = True
        self._lock = threading.Lock()

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        with self._lock:
            self._alive = False
        return 0

    def terminate(self):
        with self._lock:
            self._alive = False

    def kill(self):
        self.terminate()

    @property
    def returncode(self):
        return 0 if not self._alive else None


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def process_state_guard():
    """收尾：放掉假进程，把进程内的执行 / 验证状态恢复原样。

    恢复而不是清空：别的用例（以及应用自己的启动恢复）可能在这些字典里
    放过东西，清空会毁掉不属于本用例的状态。
    """
    execution_before = dict(server._execution_state)
    verification_before = dict(server._verification_state)
    spawned: list = []
    yield spawned
    for proc in spawned:
        proc.terminate()
    server._execution_state.clear()
    server._execution_state.update(execution_before)
    server._verification_state.clear()
    server._verification_state.update(verification_before)


@pytest.fixture
def fake_executor(monkeypatch, tmp_path, process_state_guard):
    """把执行器的 spawn 换成假进程。

    假进程登记在 ``process_state_guard`` 交出来的清单里，用例结束统一
    放掉 —— 「每个用例释放自己启的假进程」这条规矩由它兜底。

    同时把后台线程的入口换成空操作：执行 / 验证的看门线程跑的是
    ``server._run_in_plan_ctx``，那里面有自动验证接续和 LLM 调用。换成
    空操作之后线程照常起、照常立刻退出，但不会在测试里做别的。
    """
    log_path = tmp_path / "executor.log"

    def _spawn(**kwargs):
        proc = FakeProcess()
        process_state_guard.append(proc)
        return proc, log_path

    monkeypatch.setattr(server, "_spawn_executor_subprocess", _spawn)
    monkeypatch.setattr(server, "_run_in_plan_ctx", lambda *args, **kwargs: None)
    return process_state_guard


# ---------------------------------------------------------------------------
# 播种
# ---------------------------------------------------------------------------


def _seed_plan(
    plan_id: str,
    *,
    phase: str,
    project_dir: Path,
    checkpoint_kind: Optional[str] = None,
) -> Path:
    """播一份计划：目录 + ``tasks.json`` + 三张状态库行（+ 可选的检查点）。

    三张行都要有：routing 行放相位，execution 行放工作目录，verification
    行放验证轮次预算。少任何一张，走的分支就不是被测的那条了。
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.execution_repository import (
        ExecutionRepository,
    )
    from state_machine.repositories.routing_repository import RoutingRepository
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    plan_dir = Path(server.PLANS_DIR) / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )

    conn = open_db(server._state_db_path())
    try:
        migrate(conn)
        ExecutionRepository(conn).insert(
            plan_id, project_dir=str(project_dir),
        )
        RoutingRepository(conn).insert(plan_id, phase)
        VerificationRepository(conn).insert(
            plan_id, "not_started", round=0, max_rounds=3,
        )
    finally:
        conn.close()

    if checkpoint_kind is not None:
        from pause_state import write_pause_checkpoint

        write_pause_checkpoint(
            plan_dir,
            kind=checkpoint_kind,
            project_dir=str(project_dir),
            reason="user_requested",
        )
    return plan_dir


def _mark_execution_in_flight(plan_id: str, project_dir: Path) -> FakeProcess:
    """把执行记录标成「在飞」（``status=running`` 且没有 ``ended_at``）。

    这与 :func:`server._is_execution_in_flight` 认的那两种形态之一
    （后台线程跑的轮次没有 ``proc`` 可 poll）一致。
    """
    proc = FakeProcess()
    server._execution_state[plan_id] = {
        "status": "running",
        "ended_at": None,
        "process": proc,
        "pid": proc.pid,
        "project_dir": str(project_dir),
        "stop_reason": None,
    }
    return proc


def _mark_verification_in_flight(plan_id: str) -> None:
    """把验证记录标成「在飞」（``verification_status='running'``）。"""
    server._verification_state[plan_id] = {"verification_status": "running"}


# ---------------------------------------------------------------------------
# 读回
# ---------------------------------------------------------------------------


def _routing_phase(plan_id: str) -> Optional[str]:
    """读 routing 行里的相位 —— 相位的唯一真相。"""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(server._state_db_path())
    try:
        row = RoutingRepository(conn).current(plan_id)
    finally:
        conn.close()
    return None if row is None else row.get("current_phase")


def _execution_recorded(plan_id: str) -> bool:
    """``_execution_state`` 里有没有这个 plan 的执行记录。"""
    return plan_id in server._execution_state


def _checkpoint_kind(plan_dir: Path) -> Optional[str]:
    """这个计划的检查点 kind；没有检查点是 ``None``。

    恢复从哪儿继续由检查点的 ``kind`` 说了算（``execution`` 还是
    ``verification``），所以它是两条路必须给出同一个答案的**产物**之一。
    """
    from pause_state import read_pause_checkpoint

    checkpoint = read_pause_checkpoint(plan_dir)
    return None if checkpoint is None else checkpoint.get("kind")


def _install_stop_effect_counter() -> None:
    """在测试库里装一个计数器：数「相位被真正移进 ``stopped``」的次数。

    为什么不在 Python 里数：两个线程各自的 ``PlanState`` 都缓存着自己读到
    的那一行，所以「我看到的相位变了」在两个线程眼里可能**都是**真的 ——
    那是各自缓存里的旧值，不是库里发生过的变化。触发器在**写事务内部**比对
    ``OLD``/``NEW``，而写的先后由 SQLite 的写锁排好，所以它数出来的
    「相位真的变成了 ``stopped``」就是库里发生过的次数，与竞态无关。
    """
    from state_machine.db.connection import open as open_db

    conn = open_db(server._state_db_path())
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stop_effects (plan_id TEXT, phase TEXT)"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS count_stop_effects "
            "AFTER UPDATE OF current_phase ON plan_routing "
            "WHEN NEW.current_phase = 'stopped' "
            "AND OLD.current_phase <> 'stopped' "
            "BEGIN "
            "  INSERT INTO stop_effects (plan_id, phase) "
            "  VALUES (NEW.plan_id, NEW.current_phase); "
            "END;"
        )
        conn.commit()
    finally:
        conn.close()


def _stop_effects(plan_id: str) -> list:
    """这个 plan 的相位被真正移进 ``stopped`` 过几次（见上面的计数器）。"""
    from state_machine.db.connection import open as open_db

    conn = open_db(server._state_db_path())
    try:
        rows = conn.execute(
            "SELECT plan_id, phase FROM stop_effects WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return [tuple(row) for row in rows]


# ---------------------------------------------------------------------------
# 六个场景
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """一个等价性场景。

    * ``phase`` —— 播进 routing 行的相位；
    * ``in_flight`` —— 谁在飞（``execution`` / ``verification`` / 没有）；
    * ``checkpoint_kind`` —— 检查点的 kind（``execution`` / ``verification``
      / 没有检查点）；
    * ``legacy`` / ``unified`` —— 旧端点与门面的 URL 模板；
    * ``wants_project_dir`` —— 请求体要不要带 ``project_dir``；
    * ``expected_phase`` —— 两条路**都该**落在哪个相位（钉住绝对值，
      不只是「两边一样」）。
    """

    name: str
    phase: str
    legacy: str
    unified: str
    in_flight: Optional[str] = None
    checkpoint_kind: Optional[str] = None
    wants_project_dir: bool = False
    expected_phase: str = ""


SCENARIOS = (
    Scenario(
        name="execution_in_flight_stop",
        phase="executing",
        in_flight="execution",
        legacy="/api/execution/{plan_id}/stop",
        unified="/api/plan/{plan_id}/lifecycle/stop",
        expected_phase="stopped",
    ),
    Scenario(
        name="verification_in_flight_stop",
        phase="verification_running",
        in_flight="verification",
        legacy="/api/verification/{plan_id}/stop",
        unified="/api/plan/{plan_id}/lifecycle/stop",
        expected_phase="verification",
    ),
    Scenario(
        name="idle_stop",
        # idle：没有在飞的东西，停是**幂等**的 —— 一个已经停住的计划再被
        # 停一次，两条门都必须原样留着 'stopped'（既有端点的 idle 路径对
        # 终态计划不重写相位：它没有工作被打断，也没有东西可停）。
        phase="stopped",
        legacy="/api/execution/{plan_id}/stop",
        unified="/api/plan/{plan_id}/lifecycle/stop",
        expected_phase="stopped",
    ),
    Scenario(
        name="execution_checkpoint_start",
        phase="stopped",
        checkpoint_kind="execution",
        legacy="/api/execution/{plan_id}/start",
        unified="/api/plan/{plan_id}/lifecycle/start",
        wants_project_dir=True,
        expected_phase="executing",
    ),
    Scenario(
        name="verification_checkpoint_start",
        phase="verification",
        checkpoint_kind="verification",
        legacy="/api/verification/{plan_id}/start",
        unified="/api/plan/{plan_id}/lifecycle/start",
        expected_phase="verification_running",
    ),
    Scenario(
        name="no_checkpoint_start",
        phase="queued",
        legacy="/api/execution/{plan_id}/start",
        unified="/api/plan/{plan_id}/lifecycle/start",
        wants_project_dir=True,
        expected_phase="executing",
    ),
)

SCENARIOS_BY_NAME = {s.name: s for s in SCENARIOS}


# ---------------------------------------------------------------------------
# 等价性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario", SCENARIOS, ids=[s.name for s in SCENARIOS],
)
def test_stop_equivalence_in_six_scenarios(
    scenario, tmp_path, fake_executor,
):
    """门面与既有端点在六个场景下给出同一个答案。

    两份计划播得一模一样（``tasks.json`` 逐字节相同、检查点相同、routing
    相位相同），只差 ``plan_id``：一份走旧端点，一份走门面。然后比三元组。

    断言的是**相等**（两条路一致）**加上绝对值**（两条路都落在同一个
    已知相位上）。只断言相等的话，两条路一起坏掉也看不出来。
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    legacy_id = f"legacy-{scenario.name}"
    unified_id = f"unified-{scenario.name}"

    legacy_dir = _seed_plan(
        legacy_id,
        phase=scenario.phase,
        project_dir=project_dir,
        checkpoint_kind=scenario.checkpoint_kind,
    )
    unified_dir = _seed_plan(
        unified_id,
        phase=scenario.phase,
        project_dir=project_dir,
        checkpoint_kind=scenario.checkpoint_kind,
    )

    # 两份计划确实只差 plan_id —— 这是「等价」的前提，先钉住它。
    assert (legacy_dir / "tasks.json").read_bytes() == (
        unified_dir / "tasks.json"
    ).read_bytes()
    assert _routing_phase(legacy_id) == _routing_phase(unified_id)

    if scenario.checkpoint_kind is not None:
        from pause_state import read_pause_checkpoint

        legacy_checkpoint = read_pause_checkpoint(legacy_dir)
        unified_checkpoint = read_pause_checkpoint(unified_dir)
        assert legacy_checkpoint["kind"] == unified_checkpoint["kind"], (
            f"{scenario.name}: 两边的检查点 kind 不一样："
            f"{legacy_checkpoint.get('kind')!r} vs "
            f"{unified_checkpoint.get('kind')!r}"
        )
        assert legacy_checkpoint["kind"] == scenario.checkpoint_kind

    if scenario.in_flight == "execution":
        _mark_execution_in_flight(legacy_id, project_dir)
        _mark_execution_in_flight(unified_id, project_dir)
    elif scenario.in_flight == "verification":
        _mark_verification_in_flight(legacy_id)
        _mark_verification_in_flight(unified_id)

    body = {"project_dir": str(project_dir)} if scenario.wants_project_dir else {}

    legacy = client.post(scenario.legacy.format(plan_id=legacy_id), json=body)
    unified = client.post(scenario.unified.format(plan_id=unified_id), json=body)

    # --- 三元组 -------------------------------------------------------
    assert legacy.status_code == unified.status_code, (
        f"{scenario.name}: 旧端点 {legacy.status_code} vs "
        f"门面 {unified.status_code}"
    )
    assert _routing_phase(legacy_id) == _routing_phase(unified_id), (
        f"{scenario.name}: 旧端点把相位留在 "
        f"{_routing_phase(legacy_id)!r}，门面留在 "
        f"{_routing_phase(unified_id)!r}"
    )
    assert _execution_recorded(legacy_id) == _execution_recorded(unified_id), (
        f"{scenario.name}: 旧端点留下执行记录 "
        f"{_execution_recorded(legacy_id)}，门面留下 "
        f"{_execution_recorded(unified_id)}"
    )

    # 绝对值：两条路都落在同一个已知相位上。只断言相等的话，两条路一起
    # 坏掉也看不出来。
    assert _routing_phase(legacy_id) == scenario.expected_phase, (
        f"{scenario.name}: 相位落在 {_routing_phase(legacy_id)!r}，"
        f"应该是 {scenario.expected_phase!r}"
    )

    # 产物：两条路各自留下的检查点必须指向同一边。
    assert _checkpoint_kind(legacy_dir) == _checkpoint_kind(unified_dir), (
        f"{scenario.name}: 旧端点留下检查点 kind "
        f"{_checkpoint_kind(legacy_dir)!r}，门面留下 "
        f"{_checkpoint_kind(unified_dir)!r}"
    )


def test_start_equivalence_for_both_checkpoint_kinds(
    tmp_path, fake_executor,
):
    """两种检查点的 start：门面回到被打断的那一侧，旧端点也是。

    ``kind='execution'`` 的检查点 → 两条路都启动执行；
    ``kind='verification'`` 的检查点 → 两条路都启动验证。

    这一条比上一条多钉一件事：**门面派给了谁**。检查点的 kind 是门面的
    分派依据，kind 写错就是「验证的续跑被当成全新执行」——那种 bug 在
    状态码和相位上都可能看不出来（两边都 200、都落在某个相位），只有
    门面自己说的 ``dispatched`` 说得清。
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    for kind, legacy_path, unified_path, expected_dispatch, expected_phase in (
        (
            "execution",
            "/api/execution/{plan_id}/start",
            "/api/plan/{plan_id}/lifecycle/start",
            "execution",
            "executing",
        ),
        (
            "verification",
            "/api/verification/{plan_id}/start",
            "/api/plan/{plan_id}/lifecycle/start",
            "verification",
            "verification_running",
        ),
    ):
        legacy_id = f"legacy-checkpoint-{kind}"
        unified_id = f"unified-checkpoint-{kind}"
        phase = "stopped" if kind == "execution" else "verification"

        _seed_plan(
            legacy_id,
            phase=phase,
            project_dir=project_dir,
            checkpoint_kind=kind,
        )
        _seed_plan(
            unified_id,
            phase=phase,
            project_dir=project_dir,
            checkpoint_kind=kind,
        )

        body = {"project_dir": str(project_dir)} if kind == "execution" else {}

        legacy = client.post(legacy_path.format(plan_id=legacy_id), json=body)
        unified = client.post(unified_path.format(plan_id=unified_id), json=body)

        assert legacy.status_code == unified.status_code == 200, (
            f"kind={kind}: 旧端点 {legacy.status_code}，"
            f"门面 {unified.status_code}"
        )
        assert _routing_phase(legacy_id) == _routing_phase(unified_id), (
            f"kind={kind}: 旧端点 {_routing_phase(legacy_id)!r} vs "
            f"门面 {_routing_phase(unified_id)!r}"
        )
        assert _routing_phase(legacy_id) == expected_phase, (
            f"kind={kind}: 相位落在 {_routing_phase(legacy_id)!r}，"
            f"应该是 {expected_phase!r}"
        )
        assert _execution_recorded(legacy_id) == _execution_recorded(unified_id), (
            f"kind={kind}: 执行记录的留存两边不一致"
        )

        # 门面派给了谁 —— 由检查点的 kind 决定。
        payload = unified.json()
        assert payload["dispatched"] == expected_dispatch, (
            f"kind={kind}: 门面派给了 {payload.get('dispatched')!r}，"
            f"应该是 {expected_dispatch!r}"
        )
        assert payload["plan_id"] == unified_id


def test_concurrent_stop_converges_to_stopped(
    tmp_path, fake_executor,
):
    """两个线程同时 POST stop → 相位收敛到 ``stopped``，且只停了一次。

    「同时」是真的同时：``ThreadPoolExecutor`` 起两个线程，各发一个
    stop，请求在服务端并发处理。

    收敛要钉三样：

    * **最终相位是 ``stopped``** —— 两条路都停到那儿，不存在「一个线程
      停了、另一个线程把相位改回去」；
    * **只留下一份检查点** —— 重复 stop 写两份检查点的话，恢复会读到
      哪一份就成了运气；
    * **最多一次生效的停** —— 两个线程都进了处理器不代表停了两次：相位
      迁移是幂等的，第二次是 no-op。这里数的是**生效的**相位迁移
      （相位真的从别的相位变成 ``stopped``），而且是在**库里**数的
      （SQLite 触发器在写事务内部比对 OLD/NEW）—— 不是在 Python 里数
      「我看到的相位变了」：两个线程各自缓存着自己读到的那一行，那种数法
      在真并发下会把两个线程都算成「生效」，数到的是缓存而不是事实。
      处理器被调用几次是竞态的结果，不是契约；相位被移进去一次才是。

    末尾还有一条**负控**：手动把相位推回 ``executing`` 再推回 ``stopped``，
    计数器必须从 1 变成 2。没有它，「计数器其实是死的」和「真的只停了一次」
    这两种情况在断言里长得一模一样。
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    plan_id = "concurrent-stop"
    _seed_plan(plan_id, phase="executing", project_dir=project_dir)
    _mark_execution_in_flight(plan_id, project_dir)
    _install_stop_effect_counter()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                client.post, f"/api/plan/{plan_id}/lifecycle/stop", json={},
            )
            for _ in range(2)
        ]
        responses = [future.result() for future in futures]

    # 两个请求都要有答案。
    assert [r.status_code for r in responses] == [200, 200], (
        f"并发 stop 的状态码：{[r.status_code for r in responses]}"
    )

    # 收敛：相位停在 stopped。
    assert _routing_phase(plan_id) == "stopped", (
        f"并发 stop 之后相位是 {_routing_phase(plan_id)!r}"
    )

    # 只留一份检查点。
    from pause_state import PAUSE_CHECKPOINT_FILENAME

    checkpoints = sorted(
        p.name
        for p in Path(server.PLANS_DIR).glob(f"{plan_id}/{PAUSE_CHECKPOINT_FILENAME}")
    )
    assert len(checkpoints) == 1, (
        f"并发 stop 之后检查点有 {len(checkpoints)} 份：{checkpoints}"
    )

    # 最多一次生效的停（库里数的）。
    effective_stops = _stop_effects(plan_id)
    assert len(effective_stops) == 1, (
        f"并发 stop 里相位被真正移进 stopped 的次数是 {len(effective_stops)}："
        f"{effective_stops}"
    )

    # --- 负控：计数器不是死的 ------------------------------------------------
    from state_machine.db.connection import open as open_db

    conn = open_db(server._state_db_path())
    try:
        conn.execute(
            "UPDATE plan_routing SET current_phase = 'executing' "
            "WHERE plan_id = ?", (plan_id,),
        )
        conn.execute(
            "UPDATE plan_routing SET current_phase = 'stopped' "
            "WHERE plan_id = ?", (plan_id,),
        )
        conn.commit()
    finally:
        conn.close()
    assert len(_stop_effects(plan_id)) == 2, (
        f"负控失败：计数器读不到第二次迁移：{_stop_effects(plan_id)}"
    )

    # 计划是真的停了（不是「相位写着 stopped，进程还在跑」）。
    record = server._execution_state.get(plan_id)
    assert record is not None, "执行记录不见了"
    assert record.get("status") == "stopped", (
        f"执行记录的状态是 {record.get('status')!r}，应该是 'stopped'"
    )

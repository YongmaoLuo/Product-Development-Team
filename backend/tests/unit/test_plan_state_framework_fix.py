"""``PlanState`` 读写真实框架修复块 —— ``plan_kind`` 与 ``framework_fix``。

这个文件钉住什么
----------------
架构决策点 4：五种可见状态里**只有** ``blocked_on_framework_fix`` 落在相位上
（且只在「全部剩余工作被阻塞」时），其余四种（``waiting_on_user_merge`` /
``framework_fix_exhausted`` / ``scheduler_deadlock_suspected`` / ``reopened``）
住在计划状态的 ``framework_fix`` JSON 块里，由读模型
（``backend/framework_fix/read_model.py``）投影成展示状态。

于是 ``plan_routing`` 多了两列（任务 1 加的）：

* ``plan_kind`` —— 普通计划（``normal``）还是框架修复计划（``framework_fix``）；
* ``framework_fix`` —— 那块 JSON（``{}`` 表示「这个计划没有框架修复状态」）。

``PlanState`` 认识这两列：``get_plan_kind()`` / ``get_framework_fix()`` 读，
``enter_block()`` / ``exit_block()`` 写。

**相位词表多了一个相位**（2026-11-25）—— 架构决策点 4 明确要求
``plan_state`` 新增 ``blocked_on_framework_fix`` 相位常量与合法迁移：只有
「全部剩余工作被阻塞」的计划才进这个相位；进边来自执行与验证系列，出边是
``queued``（让出槽位后回调度队列）与 ``stopped``（留给操作者）。
``test_blocked_phase_is_in_the_vocabulary`` 钉住这份契约，词表指纹
（``test_vocabulary_fingerprint_is_pinned``）跟那次改动在同一个提交里重钉。

TDD contract — 4 cases
-----------------------
1. ``test_default_kind`` —— 旧行（没有这两列）读成普通计划 + 空块。
2. ``test_enter_exit_block`` —— 进块 / 退块往返。
3. ``test_block_idempotent`` —— 重复进块保留计数。
4. ``test_blocked_phase_is_in_the_vocabulary`` —— 阻塞相位在词表里，且有进边
   与出边。

另外三个用例钉住边界条件：

5. ``test_exit_block_without_block_is_noop`` —— 无块退块是**无操作**（不写库）。
6. ``test_exit_block_without_row_creates_nothing`` —— 没有行的计划退块不建行。
7. ``test_write_failure_raises_and_is_retryable`` —— 写库失败抛错，修好后可重试。

还有一个用例钉住「块是共享的」：

8. ``test_enter_block_preserves_other_block_keys`` —— 进块保留别的写者写下的键。

边界条件
--------
* 旧计划缺列 → ``plan_kind='normal'``、``framework_fix={}``；
* ``enter_block`` 幂等：重复调用不重置 ``unclassified_rounds``；
* ``exit_block`` 无块 → 无操作；
* 写库失败 → 抛错且可重试（不静默吞掉 —— 静默吞掉的挂起看起来成功而实际没落库）。

测试数据
--------
真实的 ``plan_routing`` 行，写库走生产路径（``RoutingRepository``），读列用
裸 SQL —— 断言要看的正是「行里那列原封不动是什么」。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import plan_state as plan_state_module  # noqa: E402
from plan_state import PlanState  # noqa: E402


#: 当前相位词表的指纹。
#:
#: 词表由三部分组成：``VALID_PHASES`` 的顺序、``PHASE_TRANSITIONS`` 的键、
#: ``PHASE_LABELS`` 的键。三者一起哈希 —— 任何一项增删改都会让指纹变。
#:
#: 将来若有人**有意**改词表，这个指纹要跟那次改动在同一个提交里重新钉上，
#: 并在提交信息里写明改了什么。
#:
#: 2026-11-25: 按架构决策点 4 重钉 —— 词表多了 ``blocked_on_framework_fix``
#: 这个相位（``VALID_PHASES`` 多一项、``PHASE_TRANSITIONS`` 多一行、
#: ``PHASE_LABELS`` 多一项）。
_VOCABULARY_SHA256 = (
    "a364a317b9f93037545189252efbacea6993d46283c4b612c6fb7f89f775d411"
)


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每个用例一份自己的状态库。"""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _plan_dir(tmp_path: Path, plan_id: str) -> Path:
    plan_dir = tmp_path / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


def _seed_routing_row(plan_id: str, *, phase: str = "executing") -> None:
    """按生产写路径建一行 ``plan_routing``。"""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import RoutingRepository

    conn = open_db(resolve_state_db_path())
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, phase)
    finally:
        conn.close()


def _seed_legacy_routing_row(plan_id: str) -> None:
    """建一张**没有** ``plan_kind`` / ``framework_fix`` 两列的旧 ``plan_routing``。

    2026-11-23 之前写下的状态库就是这个样子。任务 1 的迁移给旧库补上这两列
    （列的 DEFAULT 是 ``'normal'`` 与 ``'{}'``），但读路径必须**自己**也能给出
    同样的答案 —— 补列没跑成时（迁移失败、库是只读的、读的是另一个副本），
    读出来的仍然是「普通计划 + 空块」，而不是一个异常。
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS plan_routing ("
            " plan_id TEXT PRIMARY KEY,"
            " current_phase TEXT NOT NULL DEFAULT 'interview',"
            " substage TEXT,"
            " version INTEGER NOT NULL DEFAULT 0,"
            " completed_phases TEXT,"
            " review_rounds TEXT,"
            " flags TEXT,"
            " verification TEXT,"
            " last_updated TEXT,"
            " updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO plan_routing "
            "(plan_id, current_phase, version, updated_at) "
            "VALUES (?, ?, 0, ?)",
            (plan_id, "executing", "2026-11-23T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def _framework_fix_column(plan_id: str) -> Optional[str]:
    """读 ``plan_routing.framework_fix`` 列的原文。"""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        row = conn.execute(
            "SELECT framework_fix FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _write_framework_fix_column(plan_id: str, value: str) -> None:
    """直接写 ``plan_routing.framework_fix`` 列 —— 模拟另一个写者。

    ``unclassified_rounds`` 是**别的**写者维护的计数（分类器判不出这一轮属于
    哪个缺陷时递增）。挂起动作不该把它清零，所以测试要能造出「计数已经不是
    0」的块 —— 走 ``PlanState`` 的公开接口造不出来，只能直接写列。
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        conn.execute(
            "UPDATE plan_routing SET framework_fix = ? WHERE plan_id = ?",
            (value, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _updated_at(plan_id: str) -> Optional[str]:
    """读 ``plan_routing.updated_at`` —— 用来判断「有没有写过这一行」。"""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        row = conn.execute(
            "SELECT updated_at FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _set_updated_at(plan_id: str, value: str) -> None:
    """把 ``updated_at`` 写成一个哨兵值。

    写路径动这一列，所以「这一列有没有被改成别的东西」就是「有没有发生过
    一次写」的判据。哨兵值让判据不依赖时间戳的精度（写路径的时间戳只到秒，
    同一秒内的两次写看起来一模一样）。
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        conn.execute(
            "UPDATE plan_routing SET updated_at = ? WHERE plan_id = ?",
            (value, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _routing_row_count(plan_id: str) -> int:
    """``plan_routing`` 里这个 plan 的行数。"""
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _vocabulary_fingerprint() -> str:
    """相位词表的指纹（顺序敏感的 ``VALID_PHASES`` + 两个映射的键）。"""
    payload = json.dumps(
        {
            "valid_phases": list(plan_state_module.VALID_PHASES),
            "transitions": sorted(plan_state_module.PHASE_TRANSITIONS.keys()),
            "labels": sorted(plan_state_module.PHASE_LABELS.keys()),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 1. 旧行读成普通计划
# ---------------------------------------------------------------------------
def test_default_kind(state_db: Path, tmp_path: Path) -> None:
    """没有框架修复列的旧行 → ``plan_kind='normal'``、``framework_fix={}``。

    两种旧行都算：列**不在**表里（迁移没跑成 / 读的是老副本），以及列在但
    行是迁移前写的（列的 DEFAULT 兜住）。两种都读成普通计划 + 空块 ——
    一个框架修复特性上线之前写下的计划，本来就不该有框架修复状态。
    """
    plan_dir = _plan_dir(tmp_path, "p1")

    # (a) 列不在表里。
    _seed_legacy_routing_row("p1")
    ps = PlanState(plan_dir)
    assert ps.get_plan_kind() == "normal"
    assert ps.get_framework_fix() == {}

    # (b) 行在，但这两列是迁移补上去的默认值（另一个 plan，走正常写路径）。
    other_dir = _plan_dir(tmp_path, "p2")
    _seed_routing_row("p2")
    ps2 = PlanState(other_dir)
    assert ps2.get_plan_kind() == "normal"
    assert ps2.get_framework_fix() == {}


# ---------------------------------------------------------------------------
# 2. 进块 / 退块往返
# ---------------------------------------------------------------------------
def test_enter_exit_block(state_db: Path, tmp_path: Path) -> None:
    """进块 → 块有四个键；退块 → 块清空。"""
    plan_dir = _plan_dir(tmp_path, "p1")
    _seed_routing_row("p1")
    ps = PlanState(plan_dir)

    ps.enter_block("p1", fingerprint="sha256:a", fix_plan_id="fx1")

    expected = {
        "hold": True,
        "fingerprint": "sha256:a",
        "fix_plan_id": "fx1",
        "unclassified_rounds": 0,
    }
    assert ps.get_framework_fix() == expected
    # 列里存的就是这一块 —— 不是内存里的另一份。
    assert json.loads(_framework_fix_column("p1") or "{}") == expected
    # 挂起的是**业务**计划：它的 plan_kind 不变。修复计划是另一个 plan，
    # 它自己那一行才是 plan_kind='framework_fix'。
    assert ps.get_plan_kind() == "normal"

    ps.exit_block("p1")

    assert ps.get_framework_fix() == {}
    assert _framework_fix_column("p1") == "{}"


# ---------------------------------------------------------------------------
# 3. 进块幂等
# ---------------------------------------------------------------------------
def test_block_idempotent(state_db: Path, tmp_path: Path) -> None:
    """重复进块是幂等的 —— 计数不被重置。"""
    plan_dir = _plan_dir(tmp_path, "p1")
    _seed_routing_row("p1")
    ps = PlanState(plan_dir)

    ps.enter_block("p1", fingerprint="sha256:a", fix_plan_id="fx1")
    ps.enter_block("p1", fingerprint="sha256:a", fix_plan_id="fx1")

    assert ps.get_framework_fix() == {
        "hold": True,
        "fingerprint": "sha256:a",
        "fix_plan_id": "fx1",
        "unclassified_rounds": 0,
    }

    # 计数由别的写者维护：分类器判不出这一轮属于哪个缺陷时递增。
    block = json.loads(_framework_fix_column("p1") or "{}")
    block["unclassified_rounds"] = 3
    _write_framework_fix_column("p1", json.dumps(block))

    ps.enter_block("p1", fingerprint="sha256:a", fix_plan_id="fx1")

    assert ps.get_framework_fix()["unclassified_rounds"] == 3


# ---------------------------------------------------------------------------
# 4. 阻塞相位在词表里
# ---------------------------------------------------------------------------
def test_blocked_phase_is_in_the_vocabulary() -> None:
    """``blocked_on_framework_fix`` 是相位词表里的一个相位（架构决策点 4）。

    五种可见状态里**只有**它落在相位上，且只在「全部剩余工作被阻塞」时。
    所以词表必须认它 —— 否则「写这个相位」会抛 ``ValueError``，一条全部
    阻塞的计划永远进不了这个状态。

    进边来自执行与验证系列（被框架缺陷挡住的计划正是在跑或在验证时撞上的），
    出边是 ``queued``（让出槽位后回调度队列）与 ``stopped``（留给操作者）。

    它**不是**终态：它说的是「等着」，不是「结束了」—— 终态的计划会被从
    列表里滤掉，而一条等着框架修复的计划恰恰是操作者最该看见的。
    """
    assert "blocked_on_framework_fix" in plan_state_module.VALID_PHASES
    assert plan_state_module.PHASE_LABELS["blocked_on_framework_fix"] == (
        "被框架缺陷阻塞"
    )

    # 进边：执行与验证系列都能进这个相位。
    for phase in (
        "executing",
        "verification",
        "verification_running",
        "verify_first_pass",
        "verify_recheck",
        "verification_failed",
        "verification_repairing",
    ):
        allowed = set(plan_state_module.PHASE_TRANSITIONS.get(phase, []))
        allowed.update(
            plan_state_module.VERIFICATION_PHASE_TRANSITIONS.get(phase, [])
        )
        assert "blocked_on_framework_fix" in allowed, (
            f"{phase!r} must be able to enter the blocked phase"
        )

    # 出边：回调度队列（让出槽位后重新排队）+ 操作者叫停。
    assert plan_state_module.PHASE_TRANSITIONS["blocked_on_framework_fix"] == [
        "queued",
        "stopped",
    ]

    # 不是终态。
    assert "blocked_on_framework_fix" not in plan_state_module.TERMINAL_PHASES


def test_vocabulary_fingerprint_is_pinned() -> None:
    """相位词表与钉住的那一版逐字相同。

    词表是**契约**：任何一项增删改都要在这个提交里重钉指纹，并写明改了什么。
    """
    assert _vocabulary_fingerprint() == _VOCABULARY_SHA256


# ---------------------------------------------------------------------------
# 5. 无块退块 = 无操作
# ---------------------------------------------------------------------------
def test_exit_block_without_block_is_noop(state_db: Path, tmp_path: Path) -> None:
    """没有块时退块不写库 —— 行里的 ``updated_at`` 不动。

    判据用哨兵值而不是「前后两个时间戳相等」：写路径的时间戳只到秒，同一秒里
    的一次写看起来和没写一样。哨兵值是任何写路径都写不出来的字符串，所以
    「它还在」就等于「没写过」。
    """
    plan_dir = _plan_dir(tmp_path, "p1")
    _seed_routing_row("p1")
    ps = PlanState(plan_dir)

    _set_updated_at("p1", "sentinel-unchanged")
    ps.exit_block("p1")

    assert _updated_at("p1") == "sentinel-unchanged"
    assert ps.get_framework_fix() == {}


# ---------------------------------------------------------------------------
# 6. 没有行的计划退块不建行
# ---------------------------------------------------------------------------
def test_exit_block_without_row_creates_nothing(
    state_db: Path, tmp_path: Path
) -> None:
    """没有 ``plan_routing`` 行的计划退块 —— 连行都不建。

    建行是一次写。退块在没有块时是**无操作**，所以它不许顺手把一个不存在的
    计划写进状态库。
    """
    plan_dir = _plan_dir(tmp_path, "p9")
    ps = PlanState(plan_dir)
    assert _routing_row_count("p9") == 0

    ps.exit_block("p9")

    assert _routing_row_count("p9") == 0


# ---------------------------------------------------------------------------
# 7. 写库失败抛错，且可重试
# ---------------------------------------------------------------------------
def test_write_failure_raises_and_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写库失败抛错；路径修好后同一件事重试成功。

    挂起写不进去却报告成功，比挂起写失败更糟：读模型会按「没挂起」渲染，
    而计划其实停在被阻塞的状态上。所以写路径**不吞**异常。
    """
    broken = tmp_path / "broken.db"
    broken.mkdir()  # 目录，不是数据库文件 —— 打开就会失败
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(broken))

    plan_dir = _plan_dir(tmp_path, "p1")
    ps = PlanState(plan_dir)
    with pytest.raises(Exception):
        ps.enter_block("p1", fingerprint="sha256:a", fix_plan_id="fx1")

    # 路径修好 → 重试成功。
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    retry = PlanState(plan_dir)
    retry.enter_block("p1", fingerprint="sha256:a", fix_plan_id="fx1")

    assert retry.get_framework_fix() == {
        "hold": True,
        "fingerprint": "sha256:a",
        "fix_plan_id": "fx1",
        "unclassified_rounds": 0,
    }


# ---------------------------------------------------------------------------
# 8. 进块保留块里的其它键
# ---------------------------------------------------------------------------
def test_enter_block_preserves_other_block_keys(
    state_db: Path, tmp_path: Path
) -> None:
    """进块只写它自己的四个键 —— 块里的其它键原样留着。

    块是**共享**的：读模型（``backend/framework_fix/read_model.py``）从里面读
    ``status`` / ``paused_by`` / ``merge_ready_at`` 这些键。挂起动作如果整块
    覆盖掉，读模型投影出来的状态就变了 —— 挂起一个人不该顺手改掉
    「等合并」或「已重开」这些别的写者记下的事实。
    """
    plan_dir = _plan_dir(tmp_path, "p1")
    _seed_routing_row("p1")
    ps = PlanState(plan_dir)

    # 别的写者先写好的块。
    _write_framework_fix_column(
        "p1",
        json.dumps(
            {
                "status": "waiting_on_user_merge",
                "merge_ready_at": "2026-11-23T10:00:00Z",
            }
        ),
    )

    ps.enter_block("p1", fingerprint="sha256:a", fix_plan_id="fx1")

    block = ps.get_framework_fix()
    assert block["status"] == "waiting_on_user_merge"
    assert block["merge_ready_at"] == "2026-11-23T10:00:00Z"
    assert block["hold"] is True
    assert block["fingerprint"] == "sha256:a"
    assert block["fix_plan_id"] == "fx1"
    assert block["unclassified_rounds"] == 0

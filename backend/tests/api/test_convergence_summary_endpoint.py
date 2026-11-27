"""只读收敛汇总端点：``GET /api/verification/convergence-summary``。

背景
----
任务 9 落地了分类与汇总（``verification/convergence_metrics.py``），但那是一个纯
模块 —— 没有人能调用它。运维要回答的问题只有一个：「现在收敛率多少？」，这个
问题必须有一个**能调用的入口**。本文件是那个入口的契约。

端点做什么
----------
读 ``plan_verification`` 表，把每一行交给任务 9 的 :func:`summarize`，把结果端
出去，外加 ``window_days`` —— 回显调用方传进来的窗口，让答案自带单位：「rate
是 0.5」在 7 天窗口和 90 天窗口下是两件事。

端点不做什么
------------
* 不写 ``plan_verification``。这条端点是给运维看的，不是给人叫停计划的。
* 不触发执行、不调用 LLM。它是「读现在」，不是「做点什么」。
* **不重写分类口径**。判定表在任务 9 的纯模块里，端点只负责取数与转交。
  两份口径 = 两份会漂的数字。

路由的位置
----------
``backend/server.py`` 在文件末尾挂了 frontend 的 catch-all
（``@app.get("/{path:path}")``），路由匹配是**先注册先得**：注册在 catch-all
之后的 GET 路由永远轮不到自己。因此验证路由必须挂在 catch-all 之前 ——
``server.py`` 把整个 ``routes.verification`` 路由器 include 在 catch-all 之前，
而本端点是这个路由器的一员。第一条用例顺带断言答案是 **JSON 而不是
HTML**：被 catch-all 接管的话，答案会是前端的 ``index.html``。

测试数据
--------
播种五行，五种处境，与任务 9 的单元测试同源（因此手算的分母/分子可以与那份
用例对读）::

    conv-pass     PASSED            —— 收敛了：进分母，也进分子
    conv-user     loop_stopped/...  —— 人叫停：出结论，但**不进分母**
    conv-env      FAILED/preflight  —— 环境挡住：出结论，但**不进分母**
    conv-defect   FAILED/gate_red   —— 代码不对：进分母，没进分子
    conv-running  running           —— 还在跑：进了验证，没出结论

手算：分母 = 2（pass + defect），分子 = 1（pass），rate = 0.5。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402


#: 守卫要求每个 ``/api/*`` 请求都带这个头（见 ``backend/request_guard.py``）。
#: 测试套件其实会在 ``TestClient`` 构造时自动注入，但这里显式写出来 ——
#: 契约本身是端点的一部分，写出来才读得懂「这个请求是怎么发的」。
REQUEST_HEADERS = {"X-PDT-Request": "1"}


#: 播种用的五行：``(plan_id, verification_status, verification_stop_reason,
#: 距今几天)``。``updated_at`` 刻意各不相同 —— 「未收敛清单」按更新时间倒序
#: 排，时间相同的话那条顺序就成了偶然。
SEED_ROWS: Tuple[Tuple[str, str, Any, int], ...] = (
    ("conv-pass", "PASSED", None, 1),
    ("conv-user", "loop_stopped", "user_stopped", 2),
    ("conv-env", "FAILED", "service_preflight_failed", 3),
    ("conv-defect", "FAILED", "repair_gate_red", 4),
    ("conv-running", "running", None, 5),
)


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把状态库指到本次测试专属的临时文件。

    ``server._state_db_path()`` **每次调用都重新解析**环境变量（不缓存），
    所以 ``monkeypatch.setenv`` 之后端点读到的就是这一个文件。专属文件是
    必需的：``summarize()`` 数的是整张 ``plan_verification`` 表，共享库里
    别的用例留下的行会混进分母，手算就不成立了。
    """
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    return db_path


def _seed(state_db: Path) -> None:
    """把 :data:`SEED_ROWS` 写进 ``plan_verification``。"""
    conn = open_db(state_db)
    try:
        migrate(conn)
        now = datetime.now(timezone.utc)
        for plan_id, status, reason, days_ago in SEED_ROWS:
            updated_at = (now - timedelta(days=days_ago)).isoformat()
            conn.execute(
                "INSERT INTO plan_verification ("
                "  plan_id, verification_status, verification_stop_reason, updated_at"
                ") VALUES (?, ?, ?, ?)",
                (plan_id, status, reason, updated_at),
            )
    finally:
        conn.close()


def _snapshot(state_db: Path) -> Tuple[int, Dict[str, str]]:
    """读出 ``plan_verification`` 的行数与每行的 ``updated_at``。

    这两样就是「只读」这条契约的尺子：调用前后它们必须一模一样。
    """
    conn = open_db(state_db)
    try:
        migrate(conn)
        rows = conn.execute(
            "SELECT plan_id, updated_at FROM plan_verification ORDER BY plan_id"
        ).fetchall()
    finally:
        conn.close()
    return len(rows), {r[0]: r[1] for r in rows}


def _row_counts(state_db: Path) -> Dict[str, int]:
    """读出几张 ``plan_*`` 表的行数。

    ``plan_routing`` / ``plan_execution`` 是「有没有东西被调度起来」的尺子：
    这条端点是「读现在」，它一旦顺手排了路由、起了执行，运维看到的数字就
    掺进了它自己制造的动静。
    """
    conn = open_db(state_db)
    try:
        migrate(conn)
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("plan_verification", "plan_routing", "plan_execution")
        }
    finally:
        conn.close()


def _client() -> TestClient:
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# 1) 端点给出什么
# ---------------------------------------------------------------------------


def test_summary_endpoint_returns_metrics(state_db: Path) -> None:
    """播种五行 → 分母、分子、分类与未收敛清单都符合手算。

    手算写在 :data:`SEED_ROWS` 的说明里：分母 2（pass + defect）、分子 1
    （pass）、rate 0.5。人叫停与环境挡住都出了结论，但**都不进分母** ——
    它们说的不是「产品收不收敛」。
    """
    _seed(state_db)
    client = _client()

    resp = client.get(
        "/api/verification/convergence-summary",
        params={"window_days": 30, "recent_n": 5},
        headers=REQUEST_HEADERS,
    )

    assert resp.status_code == 200, resp.text
    # 答案必须是 JSON 而不是前端的 index.html：catch-all 抢走路由的话，
    # content-type 会是 text/html。
    assert resp.headers["content-type"].startswith("application/json")

    body = resp.json()

    # 端点回显窗口：rate 是 0.5，说的必须是「这 30 天里的 0.5」。
    assert body["window_days"] == 30

    # --- 分母与分子 -------------------------------------------------------
    assert body["entered"] == 5
    assert body["concluded"] == 4
    assert body["running"] == 1
    assert body["denominator"] == 2
    assert body["numerator"] == 1
    assert body["rate"] == 0.5

    # --- 分类直方图 --------------------------------------------------------
    # by_category 覆盖每一个出结论的行（人叫停 / 环境挡住都算），
    # 但 passed 不是一种失败分类，不进这张表。
    assert body["by_category"] == {
        "user_stopped": 1,
        "env_blocked": 1,
        "code_defect": 1,
    }

    # --- 未收敛清单：最近 N 条，按更新时间倒序 ------------------------------
    # conv-env 比 conv-defect 新，所以 env_blocked 在前。
    assert body["unconverged"] == [
        {"plan_id": "conv-env", "category": "env_blocked"},
        {"plan_id": "conv-defect", "category": "code_defect"},
    ]


# ---------------------------------------------------------------------------
# 2) 参数边界
# ---------------------------------------------------------------------------


def test_summary_endpoint_rejects_bad_window(state_db: Path) -> None:
    """``window_days <= 0`` → 400。

    窗口是 0 天或负数天：「这段时间里收敛了几个」这个问题本身没有答案 ——
    0 天的窗口里唯一可能是「刚刚发生的那一瞬间」。与其给一个看起来像答案的
    数字，不如说清楚这个参数不成立。
    """
    client = _client()

    for bad_window in (0, -1):
        resp = client.get(
            "/api/verification/convergence-summary",
            params={"window_days": bad_window},
            headers=REQUEST_HEADERS,
        )
        assert resp.status_code == 400, (
            f"window_days={bad_window} 应当被拒，但拿到 {resp.status_code}"
        )


def test_summary_endpoint_rejects_oversized_recent_n(state_db: Path) -> None:
    """``recent_n > 100`` → 400。

    未收敛清单是给人看的：超过 100 条的清单没人看，而它会实实在在地把
    响应体撑大。上限是这条端点唯一的容量约束。
    """
    client = _client()

    resp = client.get(
        "/api/verification/convergence-summary",
        params={"window_days": 30, "recent_n": 101},
        headers=REQUEST_HEADERS,
    )
    assert resp.status_code == 400, resp.text


def test_summary_endpoint_on_empty_table(state_db: Path) -> None:
    """库里没有行 → 200，且 ``rate`` 是 ``null``。

    「没有可测量的对象」（``None``）与「全都失败了」（``0.0``）是两件不同的
    事。库里空着的时候说 rate = 0.0，等于把「还没开始量」伪装成「量过了，
    全灭」。
    """
    client = _client()

    resp = client.get(
        "/api/verification/convergence-summary",
        headers=REQUEST_HEADERS,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["denominator"] == 0
    assert body["numerator"] == 0
    assert body["rate"] is None
    assert body["by_category"] == {}
    assert body["unconverged"] == []


# ---------------------------------------------------------------------------
# 3) 只读
# ---------------------------------------------------------------------------


def test_summary_endpoint_is_read_only(state_db: Path) -> None:
    """调用一次端点之后，行数与每行的 ``updated_at`` 一字未改。

    这条端点是给运维看的「现在怎么样」。它一旦顺手改了点什么（比如把
    ``updated_at`` 刷新成当前时刻），那么下一次再量的时候窗口的起点就被
    自己挪走了 —— 数字会随「看」的次数而变。所以尺子必须只读。

    同一把尺子还量另一件事：**没有东西被调度起来**。``plan_routing`` /
    ``plan_execution`` 仍然是空的 —— 这条端点不排路由、不起执行、不调 LLM。
    它掺进自己制造的动静，量出来的就不是「现在」了。
    """
    _seed(state_db)
    before = _snapshot(state_db)
    assert before[0] == 5, "夹具没有播种成功，接下来什么也证明不了"

    client = _client()
    resp = client.get(
        "/api/verification/convergence-summary",
        params={"window_days": 30, "recent_n": 5},
        headers=REQUEST_HEADERS,
    )
    assert resp.status_code == 200, resp.text

    after = _snapshot(state_db)
    assert after == before, (
        f"只读端点改了库：调用前 {before}，调用后 {after}"
    )

    # 没有路由、没有执行：端点只读不写，也不调度任何东西。
    assert _row_counts(state_db) == {
        "plan_verification": 5,
        "plan_routing": 0,
        "plan_execution": 0,
    }

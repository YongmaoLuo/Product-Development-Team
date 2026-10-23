"""``_record_result`` / ``_mark_layer_skipped`` 之后索引桶必须互斥。

背景
----
``VerificationExecutor`` 有三个写入点会动四个索引桶：

  * ``record_verdict``（任务 2 已修）—— 只往目标桶追加，从不在别的桶里移除；
  * ``_record_result``（``BaseExecutor.run()`` 循环走的那条）—— 同样的形状；
  * ``_mark_layer_skipped``（整层被门禁跳过）—— 同样只追加。

于是一个 VP 可以同时躺在 ``failed_vps`` 与 ``completed_vps`` 里：仪表盘报
「1 失败 1 通过」，而裁决表里只有一条结论。2026-10-22 那次现场就是这个形状 ——
报告里 VP-013/14/15 是 SKIPPED，``failed_vps`` 里却还留着它们。

本文件钉住写侧的互斥契约（任务书 TDD 规格）：

  1. ``test_record_result_failed_then_passed_only_completed``；
  2. ``test_record_result_passed_then_failed_only_failed``；
  3. ``test_record_result_deferred_not_terminal``；
  4. ``test_mark_layer_skipped_removes_from_failed``。

外加三条边界条件：BLOCKED 落 failed（历史 else 分支的合约）、整层跳过要同时
清掉旧失败**与**旧通过、以及落库顺序（repo append / ``_save_state()``）不变。

桶的归属规则来自 :mod:`verification_buckets`（任务 1 的纯函数模块），本文件
不重新定义它 —— 只钉住写侧的**互斥性**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_executor import (  # noqa: E402
    DEFAULT_STATE_FILENAME,
    VerificationExecutor,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _verdict(status: str) -> Dict[str, Any]:
    """一条最小合法的裁决（schema: status / reasons / evidence）。"""
    return {"status": status, "reasons": [], "evidence": {}}


def _plan(vps: List[Dict[str, Any]] = None) -> Dict[str, Any]:
    """一份最小的验证计划（``vps`` 是执行器读的形状）。"""
    return {
        "vps": vps
        if vps is not None
        else [{"id": "VP-001", "test_command": "pytest tests/test_x.py"}]
    }


class _FakeRepo:
    """只实现 ``append_verdict`` 的仓库替身（``summary`` 返回空行）。"""

    def __init__(self) -> None:
        self.appended: List[tuple] = []

    def summary(self, plan_id: str) -> Optional[Dict[str, Any]]:
        return None

    def append_verdict(self, plan_id: str, payload: Dict[str, Any]) -> None:
        self.appended.append((plan_id, payload))


def _make_executor(
    tmp_path: Path,
    plan: Dict[str, Any] = None,
    plan_id: str = "vp-bucket-record-result",
    verif_repo: Any = None,
) -> VerificationExecutor:
    """构造一个 ``VerificationExecutor``（不绑 verif_repo → 走 JSON 落库路径）。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return VerificationExecutor(
        verification_plan=plan if plan is not None else _plan(),
        plan_id=plan_id,
        plan_dir=plan_dir,
        sub_agent_runner=lambda vp: _verdict("PASSED"),
        verif_repo=verif_repo,
    )


def _buckets_of(ex: VerificationExecutor) -> Dict[str, List[str]]:
    """执行器当前四个桶的快照（键名与 ``verification_buckets`` 的常量一致）。"""
    return {
        "completed_vps": ex.completed_vps,
        "failed_vps": ex.failed_vps,
        "skipped_vps": ex.skipped_vps,
        "deferred_vps": list(ex._deferred_vps),
    }


def _buckets_holding(ex: VerificationExecutor, vp_id: str) -> List[str]:
    """这个 vp_id 落在哪些桶里（互斥时恰好一个）。"""
    return [
        name
        for name, members in _buckets_of(ex).items()
        if vp_id in members
    ]


# ---------------------------------------------------------------------------
# 1. 先失败后通过 → 只在 completed
# ---------------------------------------------------------------------------


def test_record_result_failed_then_passed_only_completed(tmp_path: Path) -> None:
    """先 FAILED 后 PASSED：VP-001 只留在 completed 桶里。

    旧实现只往目标桶追加，于是 FAILED 那份永远留在 ``failed_vps`` 里 ——
    仪表盘于是报「1 失败 1 通过」，而裁决表里只有一条 PASSED。
    """
    ex = _make_executor(tmp_path)

    ex._record_result("VP-001", _verdict("FAILED"))
    ex._record_result("VP-001", _verdict("PASSED"))

    assert ex.failed_vps == []
    assert ex.completed_vps == ["VP-001"]
    assert ex.skipped_vps == []
    assert list(ex._deferred_vps) == []
    assert _buckets_holding(ex, "VP-001") == ["completed_vps"]


# ---------------------------------------------------------------------------
# 2. 先通过后失败 → 只在 failed
# ---------------------------------------------------------------------------


def test_record_result_passed_then_failed_only_failed(tmp_path: Path) -> None:
    """先 PASSED 后 FAILED：VP-001 只留在 failed 桶里。

    反方向同样要成立：先通过不能变成「永远算通过」。
    """
    ex = _make_executor(tmp_path)

    ex._record_result("VP-001", _verdict("PASSED"))
    ex._record_result("VP-001", _verdict("FAILED"))

    assert ex.completed_vps == []
    assert ex.failed_vps == ["VP-001"]
    assert ex.skipped_vps == []
    assert list(ex._deferred_vps) == []
    assert _buckets_holding(ex, "VP-001") == ["failed_vps"]


# ---------------------------------------------------------------------------
# 3. DEFERRED 不是终态 → 只在 deferred
# ---------------------------------------------------------------------------


def test_record_result_deferred_not_terminal(tmp_path: Path) -> None:
    """DEFERRED 只进 deferred 桶，三个终态桶都不留它。

    DEFERRED 的语义是「本轮延后，下轮必须真跑」—— 它既不是终态成功
    （不能进 completed/skipped），也不是失败（不该生成修复任务）。
    """
    ex = _make_executor(tmp_path)

    ex._record_result("VP-001", _verdict("DEFERRED"))

    assert ex.failed_vps == []
    assert ex.completed_vps == []
    assert ex.skipped_vps == []
    assert list(ex._deferred_vps) == ["VP-001"]
    assert _buckets_holding(ex, "VP-001") == ["deferred_vps"]


def test_record_result_deferred_after_failed_leaves_terminal(
    tmp_path: Path,
) -> None:
    """失败之后改成 DEFERRED：旧失败必须消失（否则又是两个桶）。"""
    ex = _make_executor(tmp_path)

    ex._record_result("VP-001", _verdict("FAILED"))
    ex._record_result("VP-001", _verdict("DEFERRED"))

    assert ex.failed_vps == []
    assert ex.completed_vps == []
    assert ex.skipped_vps == []
    assert list(ex._deferred_vps) == ["VP-001"]
    assert _buckets_holding(ex, "VP-001") == ["deferred_vps"]


# ---------------------------------------------------------------------------
# 4. 整层跳过 → 清掉旧失败
# ---------------------------------------------------------------------------


def test_mark_layer_skipped_removes_from_failed(tmp_path: Path) -> None:
    """整层被跳过时，之前那笔失败必须从失败桶里消失。

    旧实现只往 ``skipped_vps`` 追加，于是「上一轮失败、这一轮整层被门禁
    跳过」的 VP 同时是失败和跳过 —— 而裁决表里只有一条 SKIPPED。
    """
    ex = _make_executor(tmp_path)

    ex._record_result("VP-001", _verdict("FAILED"))
    assert ex.failed_vps == ["VP-001"]

    ex._mark_layer_skipped([{"id": "VP-001"}], "上游门禁拦下")

    assert ex.failed_vps == []
    assert ex.skipped_vps == ["VP-001"]
    assert ex.completed_vps == []
    assert list(ex._deferred_vps) == []
    assert _buckets_holding(ex, "VP-001") == ["skipped_vps"]


def test_mark_layer_skipped_removes_from_completed(tmp_path: Path) -> None:
    """整层被跳过时，之前那笔通过也不能留在 completed 桶里。"""
    ex = _make_executor(tmp_path)

    ex._record_result("VP-001", _verdict("PASSED"))
    assert ex.completed_vps == ["VP-001"]

    ex._mark_layer_skipped([{"id": "VP-001"}], "上游门禁拦下")

    assert ex.completed_vps == []
    assert ex.skipped_vps == ["VP-001"]
    assert ex.failed_vps == []
    assert _buckets_holding(ex, "VP-001") == ["skipped_vps"]


def test_mark_layer_skipped_verdict_replaces_stale_failure(
    tmp_path: Path,
) -> None:
    """裁决表与桶必须说同一件事：跳过之后那条裁决就是 SKIPPED。

    桶说「跳过」而裁决表还写着「失败」是最难查的一种漂移 —— 读侧
    （报告 / 卡片 / API）是从裁决表派生的，两边不一致时用户看到的仍然是
    旧结论。
    """
    ex = _make_executor(tmp_path)

    ex._record_result("VP-001", _verdict("FAILED"))
    ex._mark_layer_skipped([{"id": "VP-001"}], "上游门禁拦下")

    collected = ex.collect_verdicts()
    assert [v["vp_id"] for v in collected] == ["VP-001"]
    assert collected[0]["status"] == "SKIPPED"


# ---------------------------------------------------------------------------
# 5. BLOCKED 落 failed（历史 else 分支的合约）
# ---------------------------------------------------------------------------


def test_record_result_blocked_lands_in_failed(tmp_path: Path) -> None:
    """BLOCKED 必须以失败的身份出现 —— 否则没人会去修它。

    见 ``test_verification_rebuild_offloop`` 的注释：「``completed_vps`` 是
    PASSED-only 的合约（BLOCKED 裁决落进 ``failed_vps``）」。
    """
    ex = _make_executor(tmp_path)

    ex._record_result("VP-001", _verdict("BLOCKED"))

    assert ex.failed_vps == ["VP-001"]
    assert ex.completed_vps == []
    assert ex.skipped_vps == []
    assert _buckets_holding(ex, "VP-001") == ["failed_vps"]


def test_record_result_unknown_status_lands_in_failed(tmp_path: Path) -> None:
    """看不懂的 status 仍按历史 else 分支归入失败桶。

    写侧刚刚亲手写下这条裁决，它必须落进某个终态桶 —— 落进「哪儿都不是」
    等于这个 VP 永远没人修。
    """
    ex = _make_executor(tmp_path)

    ex._record_result("VP-001", {"status": "WAT", "reasons": [], "evidence": {}})

    assert ex.failed_vps == ["VP-001"]
    assert _buckets_holding(ex, "VP-001") == ["failed_vps"]


# ---------------------------------------------------------------------------
# 6. 落库顺序不变：repo append 与 _save_state()
# ---------------------------------------------------------------------------


def test_record_result_repo_append_order_unchanged(tmp_path: Path) -> None:
    """绑了 verif_repo：只走 ``append_verdict``，不写 JSON 状态文件。"""
    repo = _FakeRepo()
    ex = _make_executor(tmp_path, verif_repo=repo)

    ex._record_result("VP-001", _verdict("PASSED"))

    assert len(repo.appended) == 1
    plan_id, payload = repo.appended[0]
    assert plan_id == "vp-bucket-record-result"
    assert payload["vp_id"] == "VP-001"
    assert payload["status"] == "PASSED"
    assert not list((tmp_path / "plan").glob("verification_*_state.json")), (
        "绑了 verif_repo 时不得再写 JSON 状态文件"
    )


def test_record_result_saves_state_without_repo(tmp_path: Path) -> None:
    """没绑 verif_repo：照旧落 JSON 状态文件，裁决内容不变。"""
    ex = _make_executor(tmp_path)

    ex._record_result("VP-001", _verdict("FAILED"))

    state = json.loads((tmp_path / "plan" / DEFAULT_STATE_FILENAME).read_text())
    assert state["plan_id"] == "vp-bucket-record-result"
    assert state["verdicts"]["VP-001"]["status"] == "FAILED"


def test_mark_layer_skipped_saves_state_without_repo(tmp_path: Path) -> None:
    """整层跳过照旧落 JSON 状态文件（落库路径不变）。"""
    ex = _make_executor(tmp_path)

    ex._record_result("VP-001", _verdict("FAILED"))
    ex._mark_layer_skipped([{"id": "VP-001"}], "上游门禁拦下")

    state = json.loads((tmp_path / "plan" / DEFAULT_STATE_FILENAME).read_text())
    assert state["verdicts"]["VP-001"]["status"] == "SKIPPED"


# ---------------------------------------------------------------------------
# 7. 互斥性：任意顺序的状态序列，VP 都只落在一个桶里
# ---------------------------------------------------------------------------


def test_record_result_buckets_stay_mutually_exclusive(tmp_path: Path) -> None:
    """无论状态怎么变，同一个 VP 永远只落在一个桶里。"""
    ex = _make_executor(tmp_path)

    for status in ("FAILED", "PASSED", "SKIPPED", "DEFERRED", "FAILED"):
        ex._record_result("VP-001", _verdict(status))

    holders = _buckets_holding(ex, "VP-001")
    assert len(holders) == 1, f"VP-001 同时落在 {holders!r}"
    assert holders == ["failed_vps"]

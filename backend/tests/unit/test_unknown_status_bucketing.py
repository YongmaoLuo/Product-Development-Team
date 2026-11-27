"""UNKNOWN 裁决：放行，且**不进任何桶**。

背景
----
子 agent 的答案解析不出来、重试预算也耗尽时，产生的是一条 ``UNKNOWN``
裁决（见 :mod:`verification_subagent` 的 ``VERDICT_UNKNOWN``）。它对交付物
**什么都没说** —— 把它折算成失败，等于给一个没人验过的判据凭空生产一个
修复任务；把它折算成成功，等于把没验过的东西记成「验证通过」。

arch 决策点 1：**UNKNOWN 是独立状态，不得折算成失败。**

本文件钉住三条契约（任务书 TDD 规格）：

  1. ``test_unknown_is_allowed_and_bucketless`` —— UNKNOWN 能通过 schema
     校验，且不进任何桶（尤其不在 ``failed_items`` 里 —— 那是修复任务的输入）；
  2. ``test_unknown_moves_out_of_completed_bucket`` —— 先 PASSED 后 UNKNOWN，
     这个 VP 从 completed 桶里移出，且不进任何桶；
  3. ``test_unknown_string_still_fails_closed`` —— 不认识的状态字符串
     （``"GARBAGE"``）仍然落失败桶（回归不变：写侧不能悄悄吞掉一条看不懂的
     裁决）。

外加三条边界：

  * UNKNOWN 走生产写入口径（``_record_result``）同样不进任何桶；
  * 「UNKNOWN 不得折算成失败」这条豁免写在**改写点本身**，不依赖
    ``ALLOWED_STATUSES`` 的成员关系 —— 白名单以后增删状态时它不会静默失效；
  * UNKNOWN 的裁决本身被原样保留（审计痕迹不能丢）。

桶的归属规则来自 :mod:`verification_buckets`（唯一的那份派生规则），本文件
不重新定义它 —— 只钉住 UNKNOWN 这条状态在**写侧**的表现。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_executor  # noqa: E402
from verification_buckets import (  # noqa: E402
    BUCKET_COMPLETED,
    BUCKET_DEFERRED,
    BUCKET_FAILED,
    BUCKET_SKIPPED,
    bucket_for_status,
)
from verification_executor import (  # noqa: E402
    ALLOWED_STATUSES,
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
        else [{"id": "VP-017", "test_command": "pytest tests/test_x.py"}]
    }


def _make_executor(
    tmp_path: Path,
    plan: Dict[str, Any] = None,
    plan_id: str = "vp-unknown-bucketing",
) -> VerificationExecutor:
    """构造一个 ``VerificationExecutor``（不绑 verif_repo → 走 JSON 落库路径）。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return VerificationExecutor(
        verification_plan=plan if plan is not None else _plan(),
        plan_id=plan_id,
        plan_dir=plan_dir,
        sub_agent_runner=lambda vp: _verdict("PASSED"),
    )


def _buckets_of(ex: VerificationExecutor) -> Dict[str, List[str]]:
    """执行器当前四个桶的快照（键名与 ``verification_buckets`` 的常量一致）。"""
    return {
        BUCKET_COMPLETED: ex.completed_vps,
        BUCKET_FAILED: ex.failed_vps,
        BUCKET_SKIPPED: ex.skipped_vps,
        BUCKET_DEFERRED: list(ex._deferred_vps),
    }


def _buckets_holding(ex: VerificationExecutor, vp_id: str) -> List[str]:
    """这个 vp_id 落在哪些桶里（不进任何桶时返回空 list）。"""
    return [
        name
        for name, members in _buckets_of(ex).items()
        if vp_id in members
    ]


# ---------------------------------------------------------------------------
# 1. UNKNOWN：放行，且不进任何桶
# ---------------------------------------------------------------------------


def test_unknown_is_allowed_and_bucketless(tmp_path: Path) -> None:
    """UNKNOWN 能通过 schema 校验，落盘后不进任何桶、也不在 failed_items。

    ``failed_items`` 是修复环的输入 —— 一个 UNKNOWN 落进去就等于凭空生产
    一个修复任务（为一个谁也没验过的判据）。
    """
    assert "UNKNOWN" in ALLOWED_STATUSES, (
        "UNKNOWN 是独立状态，必须是合法的裁决状态 —— 否则 schema 校验会"
        "把它拒掉，写侧只能拿 FAILED 顶替"
    )

    # 任务书给的输入 → 输出示例：UNKNOWN 不进任何桶。
    assert bucket_for_status("UNKNOWN", vp_id="VP-017", retired=False) is None

    ex = _make_executor(tmp_path)

    # record_verdict 不抛异常 —— schema 放行 UNKNOWN。
    ex.record_verdict("VP-017", _verdict("UNKNOWN"))

    assert _buckets_holding(ex, "VP-017") == [], (
        "UNKNOWN 必须不进任何桶（completed / failed / skipped / deferred）"
    )
    assert ex.failed_vps == []
    assert ex.failed_items == [], (
        "UNKNOWN 落进 failed_items 就是给修复环喂了一个修复任务"
    )
    assert ex.completed_vps == []
    assert ex.skipped_vps == []
    assert list(ex._deferred_vps) == []


# ---------------------------------------------------------------------------
# 2. 先 PASSED 后 UNKNOWN → 移出 completed 桶，且不进任何桶
# ---------------------------------------------------------------------------


def test_unknown_moves_out_of_completed_bucket(tmp_path: Path) -> None:
    """先 PASSED 再记 UNKNOWN：这个 VP 从 completed 桶里移出，且哪桶都不进。

    留在 completed 桶里等于「验证通过」—— 而 UNKNOWN 恰恰说的是「没验出
    结论」；留在 failed 桶里等于「验了没过」—— 同样是折算。
    """
    ex = _make_executor(tmp_path)

    ex.record_verdict("VP-017", _verdict("PASSED"))
    assert ex.completed_vps == ["VP-017"]

    ex.record_verdict("VP-017", _verdict("UNKNOWN"))

    assert ex.completed_vps == [], (
        "UNKNOWN 必须把 completed 桶里的旧记录清掉（桶是互斥的）"
    )
    assert ex.failed_vps == []
    assert ex.skipped_vps == []
    assert list(ex._deferred_vps) == []
    assert _buckets_holding(ex, "VP-017") == []


# ---------------------------------------------------------------------------
# 3. 不认识的状态字符串仍然 fail closed（回归不变）
# ---------------------------------------------------------------------------


def test_unknown_string_still_fails_closed(tmp_path: Path) -> None:
    """``"GARBAGE"`` 仍然落失败桶 —— 写侧不能悄悄吞掉一条看不懂的裁决。

    走 ``_record_result``（生产写入口径）：``record_verdict`` 会先做 schema
    校验，压根不会让一个非法状态写进来；「看不懂的状态怎么办」这条规则发生
    在写入口径上。
    """
    ex = _make_executor(tmp_path)

    ex._record_result("VP-017", _verdict("GARBAGE"))

    assert ex.failed_vps == ["VP-017"]
    assert _buckets_holding(ex, "VP-017") == [BUCKET_FAILED]


# ---------------------------------------------------------------------------
# 4. 边界：生产写入口径（_record_result）也不折算 UNKNOWN
# ---------------------------------------------------------------------------


def test_record_result_does_not_fold_unknown_into_failed(tmp_path: Path) -> None:
    """``_record_result`` 写下 UNKNOWN → 同样不进任何桶。

    ``_record_result`` 里有一条「状态不认识就归入失败桶」的历史兜底分支
    （写侧刚亲手写下这条裁决，落进「哪儿都不是」等于这个 VP 永远没人修）。
    UNKNOWN 必须**豁免**于这条兜底。
    """
    ex = _make_executor(tmp_path)

    ex._record_result("VP-017", _verdict("UNKNOWN"))

    assert _buckets_holding(ex, "VP-017") == []
    assert ex.failed_vps == []
    assert ex.failed_items == []


def test_unknown_exemption_does_not_depend_on_the_status_whitelist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「UNKNOWN 不折算成失败」这条豁免写在改写点本身，不靠白名单的成员关系。

    白名单（``ALLOWED_STATUSES``）以后增删状态时，这条豁免不能跟着静默
    失效 —— 否则 UNKNOWN 会被悄悄折算回失败，正是这条契约要防的事。
    """
    monkeypatch.setattr(
        verification_executor,
        "ALLOWED_STATUSES",
        ("PASSED", "FAILED", "SKIPPED", "BLOCKED", "DEFERRED"),
    )
    ex = _make_executor(tmp_path)

    ex._record_result("VP-017", _verdict("UNKNOWN"))

    assert ex.failed_vps == [], (
        "UNKNOWN 不得被 _record_result 改写成 BUCKET_FAILED"
    )
    assert _buckets_holding(ex, "VP-017") == []


def test_unknown_verdict_is_kept_as_is(tmp_path: Path) -> None:
    """UNKNOWN 的裁决原样保留 —— 审计痕迹不能因为「不进任何桶」而丢。"""
    ex = _make_executor(tmp_path)

    ex.record_verdict("VP-017", _verdict("UNKNOWN"))

    collected = ex.collect_verdicts()
    assert len(collected) == 1
    assert collected[0]["vp_id"] == "VP-017"
    assert collected[0]["status"] == "UNKNOWN"

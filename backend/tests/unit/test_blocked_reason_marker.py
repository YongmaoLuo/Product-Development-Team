"""BLOCKED 产出点的 reason 必须带 ``BLOCKED — `` 前缀（缺陷 4 要求 2）。

背景
----
用户已决定**不单列 BLOCKED 桶**（BLOCKED 继续留在失败桶里，因为「验不下去」
也得有人去修），但要求「一眼看出验不下去」而不是「验了没过」。同一个失败桶里
两种语义混在一起，读报告的人分不出「这条根本没验成」和「这条验了没过」。

四个 BLOCKED 产出点
------------------
  1. ``verification_agent.Orchestrator._build_binary_freshness_blocked_verdict``
     —— 二进制过期（build 比 source 旧）。
  2. ``verification_agent.Orchestrator._build_retired_method_blocked_verdict``
     —— ``verification_method`` 已退役 / 不存在。
  3. ``verification_agent.Orchestrator._build_service_freshness_blocked_verdict``
     —— 服务端口在轮次开始时起不来。
  4. ``verification_subagent.VerificationSubAgent.run`` 的
     ``except BlockedPathError`` —— 安全网拦下了子 agent 的动作。

统一由 :func:`verification_buckets.blocked_reason` 加前缀，所以四个产出点
不可能各写各的（前缀写法一改就漂移）。

契约
----
  * 普通 detail → ``"BLOCKED — " + detail``（前缀**恰好一次**）。
  * 已经带前缀的 detail → **不再叠加**（幂等）。
  * 空 detail → ``"BLOCKED — (no detail)"``（不得只剩一个前缀）。
  * **桶规则不动**：``BLOCKED → failed_vps``。
  * **只改 ``reasons``**：``actual_result`` 文案与 ``parse_verdict`` 的
    零容忍语义都不动。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_buckets import (  # noqa: E402
    BUCKET_FAILED,
    blocked_reason,
    bucket_for_status,
)

#: 前缀本身。单独抽出来是为了断言「前缀恰好一次」而不是「以某某开头」。
BLOCKED_PREFIX = "BLOCKED — "


# ---------------------------------------------------------------------------
# 1. blocked_reason 本身
# ---------------------------------------------------------------------------


def test_blocked_reason_prefixes_once() -> None:
    """普通 detail 加一次前缀；已有前缀不叠加；空 detail 有兜底文案。"""
    # 任务书给的输入 → 输出必须逐字对得上。
    assert blocked_reason("binary stale: build older than source") == (
        "BLOCKED — binary stale: build older than source"
    )

    # 前缀恰好一次。
    marked = blocked_reason("binary stale: build older than source")
    assert marked.count(BLOCKED_PREFIX) == 1
    assert marked.startswith(BLOCKED_PREFIX)

    # 已经带前缀的 detail 不得叠加两次。
    already = blocked_reason(f"{BLOCKED_PREFIX}binary stale")
    assert already == f"{BLOCKED_PREFIX}binary stale"
    assert already.count(BLOCKED_PREFIX) == 1

    # 空 detail 不得只剩前缀（那等于没说发生了什么）。
    assert blocked_reason("") == "BLOCKED — (no detail)"
    assert blocked_reason("   ") == "BLOCKED — (no detail)"
    assert blocked_reason("") != BLOCKED_PREFIX


# ---------------------------------------------------------------------------
# 2. agent 侧三个 BLOCKED 产出点
# ---------------------------------------------------------------------------


def test_agent_binary_freshness_reason_marked() -> None:
    """二进制过期 → reasons[0] 带 ``BLOCKED — `` 前缀。"""
    from binary_freshness import FreshnessReport
    from verification_agent import Orchestrator

    orchestrator = Orchestrator.__new__(Orchestrator)
    freshness = FreshnessReport(
        kind="pure_python",
        status="FAILED",
        detail="build older than source",
        evidence="binary mtime < newest source mtime",
        rebuild_command="pip install -e .",
    )
    verdict = orchestrator._build_binary_freshness_blocked_verdict(
        "VP-001", freshness,
    )

    assert verdict["status"] == "BLOCKED"
    reasons = verdict["reasons"]
    assert reasons[0].startswith(BLOCKED_PREFIX), reasons
    assert reasons[0].count(BLOCKED_PREFIX) == 1, reasons
    # 前缀之外的原始信息必须还在（不能只剩前缀）。
    assert "binary stale" in reasons[0], reasons


def test_agent_service_freshness_reason_marked() -> None:
    """服务端口起不来 → reasons[0] 带 ``BLOCKED — `` 前缀。"""
    from verification_agent import Orchestrator

    orchestrator = Orchestrator.__new__(Orchestrator)
    # 构造器会读这一条；没有轮次报告时 detail 走兜底文案。
    orchestrator._last_service_freshness_report = None
    verdict = orchestrator._build_service_freshness_blocked_verdict(
        "VP-001", [8080],
    )

    assert verdict["status"] == "BLOCKED"
    reasons = verdict["reasons"]
    assert reasons[0].startswith(BLOCKED_PREFIX), reasons
    assert reasons[0].count(BLOCKED_PREFIX) == 1, reasons
    assert "8080" in reasons[0], reasons


def test_agent_retired_method_reason_marked() -> None:
    """已退役 / 不存在的 method → reasons[0] 带 ``BLOCKED — `` 前缀。"""
    from verification_agent import Orchestrator

    orchestrator = Orchestrator.__new__(Orchestrator)
    verdict = orchestrator._build_retired_method_blocked_verdict(
        "VP-001", "automated_test",
    )

    assert verdict["status"] == "BLOCKED"
    reasons = verdict["reasons"]
    assert reasons[0].startswith(BLOCKED_PREFIX), reasons
    assert reasons[0].count(BLOCKED_PREFIX) == 1, reasons
    assert "automated_test" in reasons[0], reasons


# ---------------------------------------------------------------------------
# 3. 子 agent 侧的安全网
# ---------------------------------------------------------------------------


def test_subagent_security_net_reason_marked(monkeypatch, tmp_path) -> None:
    """安全网拦下子 agent 的动作 → BLOCKED verdict 的 reason 带前缀。"""
    import verification_subagent
    from verification_subagent import (
        BlockedPathError,
        VerificationSubAgent,
    )

    class _FakeSettingsBuilder:
        """只提供 run() 用到的 build() / cleanup()。"""

        def __init__(self, vp_id: str = "VP-001", plan_id: str = "default"):
            self.vp_id = vp_id
            self.plan_id = plan_id

        def build(self) -> str:
            return str(tmp_path / "verif_settings_test.json")

        def cleanup(self) -> None:
            return None

    async def _blocked_self_heal_loop(self, **kwargs):
        raise BlockedPathError(
            reason="Edit/Write outside the workspace blocked",
            evidence="hook exit=2 at call 0",
        )

    monkeypatch.setattr(
        verification_subagent, "SettingsJsonBuilder", _FakeSettingsBuilder,
    )
    monkeypatch.setattr(
        verification_subagent.VerificationSubAgent,
        "_self_heal_loop",
        _blocked_self_heal_loop,
    )

    agent = VerificationSubAgent(method="code_review")
    verdict = asyncio.run(
        agent.run({"id": "VP-001"}, log_dir=tmp_path / "logs")
    )

    assert verdict.verdict == "BLOCKED"
    reasons = verdict.reasons
    assert reasons[0].startswith(BLOCKED_PREFIX), reasons
    assert reasons[0].count(BLOCKED_PREFIX) == 1, reasons
    assert "Security net blocked" in reasons[0], reasons


# ---------------------------------------------------------------------------
# 4. 桶规则不变
# ---------------------------------------------------------------------------


def test_blocked_still_in_failed_bucket() -> None:
    """本任务只加前缀，**不动桶规则**：BLOCKED 仍然进失败桶。"""
    assert bucket_for_status("BLOCKED") == BUCKET_FAILED
    assert bucket_for_status("BLOCKED") == "failed_vps"
    # 纯函数视角：不带 vp_id、不带 retired 时也一样。
    assert bucket_for_status("BLOCKED", vp_id="VP-001") == "failed_vps"

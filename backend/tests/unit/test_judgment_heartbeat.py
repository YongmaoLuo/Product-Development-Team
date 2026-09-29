"""2026-09-14 裁决阶段心跳 —— 让"在工作"这件事对看门狗可见。

现象（round 3）: 最后一个 VP 报完
``vp_complete`` 之后，round 进入 Phase 3 裁决（一次报告判定 LLM 调用 +
逐 VP 的补充 spec/code review），**整个阶段不写 plan 目录任何日志**。
于是 3600s 阈值的 stale 检查会把一个健康、正在干活的 round 判成
``verification_log_stale`` 并终结。看门狗的陈旧信号只和它读的日志一样
可靠 —— 所以裁决阶段现在自己保持那份日志新鲜。

覆盖:
  * 心跳事件真的写进 round log（event_type=judgment_heartbeat）；
  * 写入失败（persistence 未 start_round）绝不抛，不拖垮裁决；
  * 报告判定重试循环每次尝试前都打心跳；
  * 逐 VP 的补充审查每个 VP 都打心跳。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from verification_agent import VerificationAgent


# ---------------------------------------------------------------------------
# helper contract
# ---------------------------------------------------------------------------

def _agent(persistence=None) -> VerificationAgent:
    agent = VerificationAgent.__new__(VerificationAgent)
    agent.persistence = persistence if persistence is not None else Mock()
    return agent


def test_heartbeat_writes_round_log_event():
    persistence = Mock()
    _agent(persistence)._judgment_heartbeat("report_judgment", vp_id="VP-005")
    persistence.write_verification_point_log.assert_called_once()
    args = persistence.write_verification_point_log.call_args[0]
    assert args[0] == "VP-005"
    assert args[1] == "judgment_heartbeat"
    # NOTE: this payload key is the verification-agent's own VP stage
    # (``verification_agent._judgment_heartbeat``), NOT the plan_routing
    # workflow state. It keeps the name ``stage``.
    assert args[2]["stage"] == "report_judgment"


def test_heartbeat_uses_placeholder_vp_when_absent():
    persistence = Mock()
    _agent(persistence)._judgment_heartbeat("report_judgment")
    assert persistence.write_verification_point_log.call_args[0][0] == "-"


def test_heartbeat_never_raises():
    """A plan whose persistence has no active round log must not break
    the judgment phase (``write_verification_point_log`` raises
    RuntimeError in that state)."""
    persistence = Mock()
    persistence.write_verification_point_log.side_effect = RuntimeError(
        "No log file active. Call start_round() first."
    )
    _agent(persistence)._judgment_heartbeat("supplement_review", vp_id="VP-1")


# ---------------------------------------------------------------------------
# call sites
# ---------------------------------------------------------------------------

def _make_real_agent(tmp_path: Path) -> VerificationAgent:
    """A real agent wired to a tmp state.db (the row must exist)."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    plan_dir = tmp_path / "plan-heartbeat"
    project_dir = tmp_path / "proj"
    plan_dir.mkdir()
    project_dir.mkdir()
    conn = open_db(tmp_path / "state.db")
    migrate(conn)
    repo = VerificationRepository(conn)
    repo.insert(plan_dir.name, verification_status="not_started")
    conn.commit()

    tool = Mock()
    return VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=tool,
        verif_repo=repo,
    )


def test_report_judgment_heartbeats_every_attempt(tmp_path):
    """The judgment LLM call can take 180s per attempt, with retries —
    every attempt must refresh the round log first."""
    agent = _make_real_agent(tmp_path)
    beats: list = []
    original = agent._judgment_heartbeat

    def _spy(stage, vp_id=""):
        beats.append(stage)
        return original(stage, vp_id)

    agent._judgment_heartbeat = _spy  # type: ignore[method-assign]
    agent.coding_tool.query_json.side_effect = RuntimeError("provider down")
    agent.logger = Mock()

    try:
        agent.generate_verification_report(
            execution_results={"verification_points": [
                {"id": "VP-001", "status": "PASSED", "actual_result": "",
                 "title": "t"},
            ]},
            retry_llm=2,
        )
    except Exception:
        # The fallback path varies; the heartbeat contract is what we pin.
        pass

    assert beats.count("report_judgment") >= 2, (
        f"one heartbeat per judgment attempt expected; got {beats}"
    )


def test_supplement_review_heartbeats_per_vp(tmp_path):
    agent = _make_real_agent(tmp_path)
    beats: list = []
    original = agent._judgment_heartbeat

    def _spy(stage, vp_id=""):
        beats.append((stage, vp_id))
        return original(stage, vp_id)

    agent._judgment_heartbeat = _spy  # type: ignore[method-assign]
    agent.coding_tool.query.side_effect = RuntimeError("provider down")
    agent.logger = Mock()

    agent._supplement_spec_code_review({
        "verification_points": [
            {"id": "VP-001", "title": "a"},
            {"id": "VP-002", "title": "b"},
        ],
    })

    stages = [s for s, _ in beats]
    vps = [v for _, v in beats]
    assert stages.count("supplement_review") == 2
    assert vps == ["VP-001", "VP-002"]

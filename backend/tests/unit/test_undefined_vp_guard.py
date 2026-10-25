"""分派前的**定义完整性**门禁（2026-10-24，VP-031）。

背景
====

PRD《20261021-PDT-HITL-1-infra-cri》验收 (f)：``criterion_defect`` 的验证点
作废并替换，**不生成修复任务**；VP 的声明字段不可变 —— 判据写错或缺失时
由操作者重新表达，**执行器不得自行发明判据**。

事故
====

第 3 轮的增量分配了一个 id（``VP-031``），却没有对应的 VP 定义：它不在
``verification_plan.json`` 里，也没有任何判据。执行器对"有 id、无定义"的
验证点**没有分派前校验** —— 计划转执行器 schema 时给缺 method 的条目套上
缺省方法 ``code_review``，于是这条空壳被派给了 coding tool。整轮随后在
分派飞行中被取消，那一次尝试被记成 FAILED：

* 污染了失败统计（本应是 ``criterion_defect``，且不生成修复任务）；
* 而且根本无法判定 —— 没有判据可核。

修法
====

分派**之前**先问一句"这条验证点有判据吗"：id 非空、title 非空、
``verification_method`` 在 ``SUPPORTED_METHODS`` 里、且这个 id 在
``verification_plan.json`` 的 ``verification_points`` 里真的存在。缺任一项
就返回 BLOCKED 判定（``evidence.signal = "vp_definition_missing"``），
**一次都不调用 coding tool**。

本文件钉住两件事：

* 缺定义的验证点**不派发**（coding tool 一次都不被调用）；
* 这个 signal 在四分类里命中**显式规则** → ``criterion_defect``（不是默认
  分支兜出来的 ``deliverable_defect``）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


class _RecordingCodingTool:
    """A coding tool that records every call and answers with a stub.

    The guard's whole promise is "the coding tool is never called for a
    verification point that has no definition".  Recording every call is
    what turns that promise into an observation instead of a hope.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def _record(self, kind: str, kwargs: Dict[str, Any]) -> None:
        self.calls.append({"kind": kind, **kwargs})

    def query(self, *args: Any, **kwargs: Any) -> str:
        self._record("query", kwargs)
        return "stub"

    def query_json(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        self._record("query_json", kwargs)
        return {}


def _make_agent(tmp_path: Path):
    """An agent whose plan directory holds a plan file we control."""
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)

    coding_tool = _RecordingCodingTool()
    agent = VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=coding_tool,
    )
    return agent, coding_tool


def _write_plan(agent, points: List[Dict[str, Any]]) -> None:
    """Write ``verification_plan.json`` — the plan's authority on disk."""
    agent.verification_plan_file.write_text(
        json.dumps(
            {"plan_id": agent.plan_dir.name, "verification_points": points},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _patch_dispatch(monkeypatch, agent, coding_tool) -> List[Dict[str, Any]]:
    """Record every VP that reaches the per-VP runner.

    The real runner reaches the coding tool; the fake does the same, so
    "the VP was dispatched" and "the coding tool was called" are the same
    observation from two sides.
    """
    dispatched: List[Dict[str, Any]] = []

    async def _fake_run_single_vp(vp: Dict[str, Any]) -> Dict[str, Any]:
        dispatched.append(dict(vp))
        coding_tool.query(prompt="should never happen")
        return {"status": "PASSED", "reasons": [], "evidence": {}}

    monkeypatch.setattr(agent, "_run_single_vp_async", _fake_run_single_vp)
    return dispatched


# ---------------------------------------------------------------------------
# 主用例：生产事故的形状
# ---------------------------------------------------------------------------

def test_undefined_vp_is_criterion_defect_and_not_dispatched(
    tmp_path: Path, monkeypatch,
) -> None:
    """有 id、无定义的 VP 不得被派发；它的失败类别是 criterion_defect。

    生产形状：``VP-031`` 拿到了 id，却不在 ``verification_plan.json`` 里。
    执行器曾经按缺省方法把它派给 coding tool，随后整轮取消把它记成
    FAILED —— 那是"判据坏了"被记成"交付物坏了"。
    """
    agent, coding_tool = _make_agent(tmp_path)
    _write_plan(agent, [
        {"id": "VP-001", "title": "计划里真实存在的验证点",
         "verification_method": "code_review", "expected_result": "ok"},
    ])
    dispatched = _patch_dispatch(monkeypatch, agent, coding_tool)

    executor = agent._build_verification_executor(
        plan_data={"verification_points": [
            {"id": "VP-001", "title": "计划里真实存在的验证点",
             "verification_method": "code_review"},
            {"id": "VP-031", "title": "", "verification_method": "code_review"},
        ]},
    )

    verdict = asyncio.run(executor.sub_agent_runner(
        {"id": "VP-031", "method": "code_review", "title": ""},
    ))

    # 1) 没派发：VP 运行器和 coding tool 都没被碰到。
    assert dispatched == [], (
        f"an undefined VP must not be dispatched; got {dispatched!r}"
    )
    assert coding_tool.calls == [], (
        f"the coding tool must not be called; got {coding_tool.calls!r}"
    )

    # 2) 判定是 BLOCKED，且证据里点名"定义缺失"。
    assert verdict["status"] == "BLOCKED", f"got {verdict!r}"
    assert verdict["evidence"]["signal"] == "vp_definition_missing", (
        f"the blocked verdict must name the missing definition; "
        f"got evidence={verdict['evidence']!r}"
    )

    # 3) 四分类：命中**显式规则** → criterion_defect。
    #    不是 None（那会落回语义层），也不是默认分支的 deliverable_defect。
    from verification.failure_triage import (
        CRITERION_DEFECT,
        DELIVERABLE_DEFECT,
        classify_deterministic,
    )

    assert classify_deterministic("vp_definition_missing", "") == CRITERION_DEFECT, (
        "vp_definition_missing must hit an explicit tier-A rule"
    )
    assert classify_deterministic("vp_definition_missing", "") != DELIVERABLE_DEFECT
    assert verdict["failure_class"] == CRITERION_DEFECT, (
        f"the verdict must carry the class the rule produced; "
        f"got {verdict.get('failure_class')!r}"
    )


# ---------------------------------------------------------------------------
# 门禁的另外几种缺口
# ---------------------------------------------------------------------------

def test_a_vp_without_a_title_is_not_dispatched(
    tmp_path: Path, monkeypatch,
) -> None:
    """计划里有这条 VP，但标题是空的 —— 没有标题就没有判据。"""
    agent, coding_tool = _make_agent(tmp_path)
    _write_plan(agent, [
        {"id": "VP-002", "title": "", "verification_method": "code_review"},
    ])
    dispatched = _patch_dispatch(monkeypatch, agent, coding_tool)

    executor = agent._build_verification_executor(
        plan_data={"verification_points": [
            {"id": "VP-002", "title": "", "verification_method": "code_review"},
        ]},
    )
    verdict = asyncio.run(executor.sub_agent_runner(
        {"id": "VP-002", "method": "code_review", "title": ""},
    ))

    assert verdict["status"] == "BLOCKED", f"got {verdict!r}"
    assert verdict["evidence"]["signal"] == "vp_definition_missing"
    assert dispatched == []
    assert coding_tool.calls == []


def test_a_vp_with_an_unsupported_method_is_not_dispatched(
    tmp_path: Path, monkeypatch,
) -> None:
    """方法不在 ``SUPPORTED_METHODS`` 里 —— 判据没法跑，更不能被发明。"""
    agent, coding_tool = _make_agent(tmp_path)
    _write_plan(agent, [
        {"id": "VP-003", "title": "方法已退休",
         "verification_method": "automated_test"},
    ])
    dispatched = _patch_dispatch(monkeypatch, agent, coding_tool)

    executor = agent._build_verification_executor(
        plan_data={"verification_points": [
            {"id": "VP-003", "title": "方法已退休",
             "verification_method": "automated_test"},
        ]},
    )
    verdict = asyncio.run(executor.sub_agent_runner(
        {"id": "VP-003", "method": "automated_test", "title": "方法已退休"},
    ))

    assert verdict["status"] == "BLOCKED", f"got {verdict!r}"
    assert verdict["evidence"]["signal"] == "vp_definition_missing"
    assert dispatched == []
    assert coding_tool.calls == []


def test_a_vp_without_an_id_is_not_dispatched(
    tmp_path: Path, monkeypatch,
) -> None:
    """连 id 都没有的条目不是验证点。"""
    agent, coding_tool = _make_agent(tmp_path)
    _write_plan(agent, [
        {"id": "VP-004", "title": "有定义", "verification_method": "code_review"},
    ])
    dispatched = _patch_dispatch(monkeypatch, agent, coding_tool)

    executor = agent._build_verification_executor(
        plan_data={"verification_points": [
            {"id": "VP-004", "title": "有定义",
             "verification_method": "code_review"},
        ]},
    )
    verdict = asyncio.run(executor.sub_agent_runner(
        {"method": "code_review", "title": "没有 id"},
    ))

    assert verdict["status"] == "BLOCKED", f"got {verdict!r}"
    assert verdict["evidence"]["signal"] == "vp_definition_missing"
    assert dispatched == []
    assert coding_tool.calls == []


# ---------------------------------------------------------------------------
# 负控：门禁不误伤
# ---------------------------------------------------------------------------

def test_a_well_defined_vp_is_still_dispatched(
    tmp_path: Path, monkeypatch,
) -> None:
    """计划里定义完整的 VP 照常派发 —— 门禁只拦"没有判据"的。"""
    agent, coding_tool = _make_agent(tmp_path)
    _write_plan(agent, [
        {"id": "VP-005", "title": "定义完整的验证点",
         "verification_method": "code_review", "expected_result": "ok"},
    ])
    dispatched = _patch_dispatch(monkeypatch, agent, coding_tool)

    executor = agent._build_verification_executor(
        plan_data={"verification_points": [
            {"id": "VP-005", "title": "定义完整的验证点",
             "verification_method": "code_review"},
        ]},
    )
    verdict = asyncio.run(executor.sub_agent_runner(
        {"id": "VP-005", "method": "code_review", "title": "定义完整的验证点"},
    ))

    assert [vp["id"] for vp in dispatched] == ["VP-005"], (
        f"a well-defined VP must still be dispatched; got {dispatched!r}"
    )
    assert verdict.get("evidence", {}).get("signal") != "vp_definition_missing", (
        f"a well-defined VP must not be blocked by the definition guard; "
        f"got {verdict!r}"
    )

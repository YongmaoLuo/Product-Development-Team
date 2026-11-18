"""不变量：缺了 repair 任务的失败 VP，必须**同时**有一行 HITL 问题。

背景（2026-11-17，plan 20261117-repair-coverage-hitl-2 的最终验收）
--------------------------------------------------------------
2026-11-14 的一轮验证里有两条 ``deliverable_defect`` 失败 —— VP-013 与
VP-023。repair 内容是**一次**生成的：生成器拿到本轮所有候选 VP，产出
若干条内容，``RepairTaskAssembler.assemble`` 再把它们变成任务行。首批
只覆盖了其中一条，于是另一条（VP-023）既没有 repair 任务，也没有
HITL：没有人知道它失败过，直到下一轮它再次失败。

这个文件钉住的是那个「另一条不再静默消失」：

* **缺任务的 VP 必须有一行问题** —— 不是二选一。VP-013 拿到 repair
  任务的那一轮里，VP-023 的问题必须同时写进 ``plan_decisions``；
* **任务与问题并存** —— 覆盖校验不能因为要提问就把已有的任务丢掉；
* **原因要说得对** —— 「生成器压根没提这条」（``generator_returned_no_entry``）
  与「提到了，但被装配器丢掉，因为它的 ``files_to_modify`` 是空的」
  （``files_to_modify_was_empty``）是两回事，要修的地方也不同，问题里
  必须写得出是哪一种；
* **只开一次** —— 一次覆盖校验里，一个 VP 只问一次。

这一段是端到端跑的：真的 ``RepairTaskAssembler``（id 分配、空
``files_to_modify`` 丢弃都是它做的）、真的 ``hitl.service``（问题真的
落进 ``plan_decisions``，读回也用真的 ``list_decisions``）、真的
``VerificationOrchestrator._ensure_repair_coverage``。只有 repair 生成器
是替身 —— 它是这一段里唯一要花钱的东西。

plan 的 ``hitl_config.json`` 配了**空的 hooks**：问题仍然落库（这正是
要钉住的一半），而投递不依赖任何网络出口 —— 一条没有收件人的问题不该
把「问题写没写下去」这件事也变成不确定的。

测试命令::

    pytest backend/tests/unit/test_repair_coverage_invariant_e2e.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from hitl.service import list_decisions  # noqa: E402
from repair_generator import RepairTaskAssembler  # noqa: E402
from verification.failure_triage import DELIVERABLE_DEFECT  # noqa: E402
from verification.orchestrator import VerificationOrchestrator  # noqa: E402
from verification.repair_coverage import MISSED_VP_OPTIONS  # noqa: E402


# ---------------------------------------------------------------------------
# 替身：只有 repair 生成器
# ---------------------------------------------------------------------------


class _FakeGenerator:
    """``self.repair_generator`` 的替身。

    按脚本回内容 —— 第 N 次调用回脚本里的第 N 条（脚本用完了就重复回最后
    一条）。记下每一次调用收到了哪些 VP，好让测试能断言「补生成只收到了
    缺的那一条」。
    """

    def __init__(self, scripts=None):
        self._scripts = list(scripts or [])
        self.calls = []

    def generate_repair_contents(self, failed_vps, round_number, **kwargs):
        self.calls.append({
            "vp_ids": [
                str(vp.get("id") or "") if isinstance(vp, dict) else str(vp)
                for vp in (failed_vps or [])
            ],
            "round_number": round_number,
        })
        if not self._scripts:
            return []
        index = min(len(self.calls) - 1, len(self._scripts) - 1)
        return [dict(row) for row in (self._scripts[index] or [])]


#: 一条**能被装配成任务**的 repair 内容 —— 它有 ``files_to_modify``。
_CONTENT_VP013 = {
    "failed_vp_id": "VP-013",
    "title": "repair VP-013",
    "description": "VP-013 的 deliverable 是坏的，把它修好。",
    "files_to_modify": ["backend/verify_vp013.py"],
    "acceptance_criteria": "pytest -q backend/tests/unit/test_vp013.py 通过",
}

#: 一条**装不成任务**的 repair 内容 —— ``files_to_modify`` 是空的，装配器
#: 会把它丢掉（``repair_task_dropped_empty_files_to_modify``）。
_CONTENT_VP023_DROPPED = {
    "failed_vp_id": "VP-023",
    "title": "repair VP-023",
    "description": "VP-023 的 deliverable 是坏的，把它修好。",
    "files_to_modify": [],
    "acceptance_criteria": "pytest -q backend/tests/unit/test_vp023.py 通过",
}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    """一个真的 orchestrator，它的 plan 目录里只多一份 hitl_config.json。

    ``hooks`` 是空的：问题仍然要落进 ``plan_decisions``，而这一段不依赖
    任何网络出口。
    """
    plan_dir = tmp_path / "20260101-repair-coverage-fixture"
    plan_dir.mkdir()
    (plan_dir / "hitl_config.json").write_text(
        json.dumps({
            "version": 1,
            "mode": "strict",
            "timeout_sec": 3600,
            "hooks": [],
        }),
        encoding="utf-8",
    )
    # 重依赖换成替身：这两个对象是构造出来的，跟本文件要测的东西无关。
    monkeypatch.setattr(
        "verification.orchestrator.VerificationAgent",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "verification.orchestrator.RepairTaskGenerator",
        lambda *args, **kwargs: Mock(),
    )
    return VerificationOrchestrator(plan_dir=plan_dir, project_dir=tmp_path)


def _assemble(round_number, contents, failed_vps, vp_test_commands, plan_file):
    """用**真的**装配器把内容变成任务行。"""
    return RepairTaskAssembler(
        round_number=round_number,
        failed_vps=failed_vps,
        vp_test_commands=vp_test_commands,
        plan_path=str(plan_file),
        seq_base=1,
    ).assemble(list(contents))


# ---------------------------------------------------------------------------
# 场景一：生成器压根没提 VP-023
# ---------------------------------------------------------------------------


def test_missing_vp_gets_question_alongside_repair(orchestrator):
    """首批只覆盖 VP-013 → VP-023 拿到任务？拿不到，但它拿到一个问题。

    这是 2026-11-14 那一轮的样子：两条 ``deliverable_defect`` 失败，首批
    只覆盖了一条。缺的那条必须留下一行问题 —— **和**已有任务并存，不是
    二选一。
    """
    plan_id = orchestrator.plan_dir.name
    plan_file = orchestrator.plan_dir / "verification_plan.json"

    candidates = [{"id": "VP-013"}, {"id": "VP-023", "test_command": "pytest -q"}]
    failed_vps = [
        {"id": "VP-013", "title": "VP-013", "priority": "high"},
        {"id": "VP-023", "title": "VP-023", "priority": "medium"},
    ]
    vp_test_commands = {"VP-023": "pytest -q"}

    # 生成器只产出 VP-013 的内容 —— 首批一次，补生成一次（脚本只有一条，
    # 替身会重复回最后一条，所以补生成拿到的也是 VP-013 的内容）。
    orchestrator.repair_generator = _FakeGenerator([
        [_CONTENT_VP013],
    ])

    # 首批：生成 → 装配（真的装配器）。
    first_contents = orchestrator.repair_generator.generate_repair_contents(
        failed_vps, 3,
    )
    first_tasks = _assemble(
        3, first_contents, failed_vps, vp_test_commands, plan_file,
    )
    assert [task["failed_vp_id"] for task in first_tasks] == ["VP-013"]

    repair_tasks = orchestrator._ensure_repair_coverage(
        first_tasks,
        candidates,
        first_contents,
        3,
        failed_vps=failed_vps,
        vp_test_commands=vp_test_commands,
        plan_file=plan_file,
        existing_repair_ids=set(),
        generator_kwargs={},
    )

    # 补生成只收到了缺的那一条 VP（别让它重新替 VP-013 干活）。
    assert len(orchestrator.repair_generator.calls) == 2
    assert orchestrator.repair_generator.calls[1]["vp_ids"] == ["VP-023"]

    # 任务表：VP-013 的任务还在 —— 提问没有把任务挤掉。
    assert [task["failed_vp_id"] for task in repair_tasks] == ["VP-013"]

    # 问题行真的落进了 ``plan_decisions``（用真的 ``list_decisions`` 读回，
    # 不是问一个替身「你打算问什么」）。
    decisions = list_decisions(plan_id)
    assert [d.vp_id for d in decisions] == ["VP-023"]
    decision = decisions[0]

    # 三个出路都在：人来修 / 改判据 / 暂时搁置。
    assert set(decision.options) == {"manual_fix", "revise_criterion", "defer"}
    assert set(decision.options) == set(MISSED_VP_OPTIONS)

    # 问题点名了是哪个点、哪一轮、失败分类是什么。
    assert "VP-023" in decision.question
    assert "round 3" in decision.question
    assert decision.failure_class == DELIVERABLE_DEFECT
    assert decision.round == 3

    # 原因是「生成器压根没提这一条」。
    assert "no entry" in decision.question.lower()


# ---------------------------------------------------------------------------
# 场景二：生成器提了，但装配器把那一行丢了
# ---------------------------------------------------------------------------


def test_question_reason_comes_from_dropped_content(orchestrator):
    """补生成提到了 VP-023，但 ``files_to_modify`` 为空 → 原因文案要说得对。

    「生成器漏了」和「被装配器丢了」要修的地方完全不同：前者要改生成器
    的输入，后者要改生成器的输出契约。一个只说「VP-023 没有修复任务」
    的问题会把人引到错的地方去。
    """
    plan_id = orchestrator.plan_dir.name
    plan_file = orchestrator.plan_dir / "verification_plan.json"

    candidates = [{"id": "VP-013"}, {"id": "VP-023", "test_command": "pytest -q"}]
    failed_vps = [
        {"id": "VP-013", "title": "VP-013", "priority": "high"},
        {"id": "VP-023", "title": "VP-023", "priority": "medium"},
    ]
    vp_test_commands = {"VP-023": "pytest -q"}

    # 首批只覆盖 VP-013；补生成提到了 VP-023，但那一行装不成任务。
    orchestrator.repair_generator = _FakeGenerator([
        [_CONTENT_VP013],
        [_CONTENT_VP023_DROPPED],
    ])

    first_contents = orchestrator.repair_generator.generate_repair_contents(
        failed_vps, 3,
    )
    first_tasks = _assemble(
        3, first_contents, failed_vps, vp_test_commands, plan_file,
    )

    repair_tasks = orchestrator._ensure_repair_coverage(
        first_tasks,
        candidates,
        first_contents,
        3,
        failed_vps=failed_vps,
        vp_test_commands=vp_test_commands,
        plan_file=plan_file,
        existing_repair_ids=set(),
        generator_kwargs={},
    )

    # 被丢掉的那一行没有变成任务 —— 它只有 HITL 这条出路。
    assert [task["failed_vp_id"] for task in repair_tasks] == ["VP-013"]

    decisions = list_decisions(plan_id)
    assert [d.vp_id for d in decisions] == ["VP-023"]
    decision = decisions[0]

    # 原因说的是「提到了，但 files_to_modify 是空的」——不是「生成器漏了」。
    assert "files_to_modify was empty" in decision.question.lower()
    assert "no entry" not in decision.question.lower()
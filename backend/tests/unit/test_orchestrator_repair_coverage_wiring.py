"""R1/R2/R7 —— orchestrator 的覆盖校验接线。

背景（2026-11-17，plan 20261117-repair-coverage-hitl-2）
------------------------------------------------------
repair 内容是**一次**生成的：生成器拿到本轮所有候选 VP，产出若干条内容，
``RepairTaskAssembler.assemble`` 再把它们变成任务行。两条路可以让一个
候选 VP 悄悄消失 —— 生成器压根没提它，或者 assemble 把它那行
``files_to_modify`` 为空的行丢掉了。

消失的那条 VP 既没有 repair 任务，也没有 HITL：没有人知道它失败过，
直到下一轮它再次失败（2026-11-17 丢过一个 VP-023）。

本任务测的是**接线**：``_ensure_repair_coverage`` 在 assemble 之后、
持久化之前做一次覆盖校验，缺的那几条补生成一次（最多一次），补不上
的逐个开 HITL。

钉住的六件事
------------
1. **部分覆盖 → 补生成一次** —— 第二次生成只收到缺的那一个 VP；
2. **新 id 不复用** —— 补出来的是 ``repair-30``，不是 ``repair-29``；
3. **全覆盖 → 不补生成** —— 生成器一次都不被调用；
4. **补生成抛错 → 仍然开 HITL** —— 生成器坏了不等于这个 VP 有出路；
5. **R7 三条日志** —— ``repair_coverage_checked`` /
   ``repair_coverage_regenerated`` / ``repair_coverage_hitl_raised``；
6. **生成失败的一轮不参与校验**（2026-09-14 park 语义）、**别的异常不
   让本轮搁浅**、**取消不被吞掉**。

装配用的是**真的** ``RepairTaskAssembler``（id 分配、空
``files_to_modify`` 丢弃都是它做的），HITL 用的是**真的**
``hitl.service.create_decision``（被替身记录），只有 repair 生成器是
替身 —— 它是这一层唯一要花钱的东西。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from repair_generator import RepairGenerationError  # noqa: E402
from verification.orchestrator import VerificationOrchestrator  # noqa: E402
from verification.repair_coverage import (  # noqa: E402
    REASON_EMPTY_FILES_TO_MODIFY,
    REASON_GENERATOR_MISSING,
)
from verification_cancel import VerificationCancelled  # noqa: E402


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


class _FakeGenerator:
    """``self.repair_generator`` 的替身。

    记下每一次调用（收到哪些 VP、第几轮、带了什么 kwargs），并按脚本
    回内容 —— 脚本里放一个异常实例就抛它。
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
            "kwargs": dict(kwargs),
        })
        index = min(len(self.calls) - 1, len(self._scripts) - 1)
        script = self._scripts[index] if self._scripts else []
        if isinstance(script, BaseException):
            raise script
        return [dict(row) for row in (script or [])]


class _DecisionSpy:
    """``hitl.service.create_decision`` 的替身。"""

    def __init__(self, decision_id="DEC-R3"):
        self.requests = []
        self._decision_id = decision_id

    def __call__(self, request, plan_dir, *args, **kwargs):
        self.requests.append(request)
        return SimpleNamespace(decision_id=self._decision_id)


@pytest.fixture
def orchestrator(tmp_path, monkeypatch):
    """一个重依赖被替换掉的 verification orchestrator。"""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    monkeypatch.setattr(
        "verification.orchestrator.VerificationAgent",
        lambda *args, **kwargs: Mock(),
    )
    monkeypatch.setattr(
        "verification.orchestrator.RepairTaskGenerator",
        lambda *args, **kwargs: Mock(),
    )
    return VerificationOrchestrator(plan_dir=plan_dir, project_dir=tmp_path)


@pytest.fixture
def spy(monkeypatch):
    """装上 ``create_decision`` 的替身。"""
    _spy = _DecisionSpy()
    monkeypatch.setattr("hitl.service.create_decision", _spy)
    return _spy


def _run(orchestrator, **overrides):
    """按「首批只覆盖 VP-013」的形状跑一次覆盖校验。"""
    kwargs = {
        "failed_vps": [{"id": "VP-013"}, {"id": "VP-023"}],
        "vp_test_commands": {"VP-023": "pytest -q"},
        "plan_file": orchestrator.plan_dir / "verification_plan.json",
        "existing_repair_ids": {"repair-29"},
        "generator_kwargs": {},
    }
    kwargs.update(overrides)
    return orchestrator._ensure_repair_coverage(
        [{"id": "repair-29", "failed_vp_id": "VP-013"}],
        [{"id": "VP-013"}, {"id": "VP-023"}],
        [{"failed_vp_id": "VP-013"}],
        3,
        **kwargs,
    )


def _run_empty_batch(orchestrator, **overrides):
    """按「首批什么内容都没有」的形状跑一次覆盖校验。"""
    kwargs = {
        "failed_vps": [{"id": "VP-013"}, {"id": "VP-023"}],
        "vp_test_commands": {},
        "plan_file": orchestrator.plan_dir / "verification_plan.json",
        "existing_repair_ids": set(),
        "generator_kwargs": {},
    }
    kwargs.update(overrides)
    return orchestrator._ensure_repair_coverage(
        [],
        [{"id": "VP-013"}, {"id": "VP-023"}],
        [],
        3,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 决策点 1 —— 全覆盖不触发补生成
# ---------------------------------------------------------------------------


def test_full_coverage_no_second_call(orchestrator):
    """每个候选都拿到了任务 → 生成器一次都不被调用，任务表原样返回。"""
    orchestrator.repair_generator = _FakeGenerator([
        [{"failed_vp_id": "VP-023", "files_to_modify": ["backend/b.py"]}],
    ])

    tasks = orchestrator._ensure_repair_coverage(
        [
            {"id": "repair-29", "failed_vp_id": "VP-013"},
            {"id": "repair-30", "failed_vp_id": "VP-023"},
        ],
        [{"id": "VP-013"}, {"id": "VP-023"}],
        [{"failed_vp_id": "VP-013"}, {"failed_vp_id": "VP-023"}],
        3,
        failed_vps=[{"id": "VP-013"}, {"id": "VP-023"}],
        vp_test_commands={},
        plan_file=orchestrator.plan_dir / "verification_plan.json",
        existing_repair_ids={"repair-29", "repair-30"},
        generator_kwargs={},
    )

    assert orchestrator.repair_generator.calls == []
    assert [task["id"] for task in tasks] == ["repair-29", "repair-30"]


# ---------------------------------------------------------------------------
# 决策点 2 —— 部分覆盖 → 补生成一次并补齐
# ---------------------------------------------------------------------------


def test_partial_coverage_regenerates_and_logs(orchestrator, capsys):
    """缺 1 个 → 补生成只收到那一个 VP，新 id 不复用，日志齐全。"""
    orchestrator.repair_generator = _FakeGenerator([
        [{"failed_vp_id": "VP-023", "files_to_modify": ["backend/b.py"]}],
    ])

    tasks = _run(orchestrator)

    # 补生成只发生一次，且只收到缺的那一个 VP。
    assert len(orchestrator.repair_generator.calls) == 1
    assert orchestrator.repair_generator.calls[0]["vp_ids"] == ["VP-023"]
    assert orchestrator.repair_generator.calls[0]["round_number"] == 3

    # 补出来的任务接在原批次后面，id 从本批 id ∪ 已有 id 续号。
    assert [task["id"] for task in tasks] == ["repair-29", "repair-30"]
    assert [task["failed_vp_id"] for task in tasks] == ["VP-013", "VP-023"]

    # R7 三条日志都在。
    out = capsys.readouterr().out
    assert "repair_coverage_checked missing=['VP-023']" in out
    assert "repair_coverage_regenerated added=['repair-30']" in out
    assert "repair_coverage_hitl_raised=[]" in out


def test_regenerated_id_does_not_reuse_the_batch_id(orchestrator):
    """本批已有的 id 不进池子 → 补出来的那条不会顶掉 ``repair-29``。"""
    orchestrator.repair_generator = _FakeGenerator([
        [{"failed_vp_id": "VP-023", "files_to_modify": ["backend/b.py"]}],
    ])

    tasks = _run(orchestrator, existing_repair_ids=set())

    assert [task["id"] for task in tasks] == ["repair-29", "repair-30"]


def test_second_assembler_gets_the_whole_id_pool(orchestrator, monkeypatch):
    """补装配的池子 = 本批 id ∪ 已有 id；同名的那一行被丢掉。"""
    orchestrator.repair_generator = _FakeGenerator([
        [{"failed_vp_id": "VP-023", "files_to_modify": ["backend/b.py"]}],
    ])
    seen = {}

    class _AssemblerSpy:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def assemble(self, contents):
            # 一个不守规矩的装配器：把本批已经用过的 id 又发了一遍。
            return [{"id": "repair-29", "failed_vp_id": "VP-023"}]

    monkeypatch.setattr("repair_generator.RepairTaskAssembler", _AssemblerSpy)

    tasks = _run(orchestrator)

    # 池子：本批的 id ∪ 计划已有的 id。
    assert seen["existing_task_ids"] == {"repair-29"}
    assert seen["seq_base"] == 30
    # 第二次装配只看缺的那一个 VP。
    assert [str(vp.get("id")) for vp in seen["failed_vps"]] == ["VP-023"]
    # 同名的行不能进任务表 —— 两行共用一个 id 会在 plan_tasks 里互相覆盖。
    assert [task["id"] for task in tasks] == ["repair-29"]


# ---------------------------------------------------------------------------
# 决策点 3 —— 补生成后仍缺 → 开 HITL
# ---------------------------------------------------------------------------


def test_regeneration_error_falls_through_to_hitl(orchestrator, monkeypatch):
    """补生成抛错 → 视为无内容继续，仍然开 HITL。"""
    orchestrator.repair_generator = _FakeGenerator([
        RepairGenerationError("the model is down"),
    ])
    asked = []

    def _ask(vp, reason, round_number):
        asked.append((vp, reason, round_number))
        return "DEC-R3"

    monkeypatch.setattr(
        orchestrator, "_ask_human_about_missing_repair", _ask,
    )

    tasks = _run(orchestrator)

    assert len(asked) == 1
    vp, reason, round_number = asked[0]
    assert str(vp.get("id")) == "VP-023"
    assert reason == REASON_GENERATOR_MISSING
    assert round_number == 3

    # 补不上 → 任务表里没有 VP-023 的任务（它只有一条出路：HITL）。
    assert [task["failed_vp_id"] for task in tasks] == ["VP-013"]


def test_hitl_raised_logged(orchestrator, capsys, spy):
    """补生成的内容被 assemble 丢掉 → HITL 真的开出去，且日志点名它。"""
    orchestrator.repair_generator = _FakeGenerator([
        # ``files_to_modify`` 为空 → 真的 assemble 会把这一行丢掉。
        [{"failed_vp_id": "VP-023", "files_to_modify": []}],
    ])

    tasks = _run(
        orchestrator,
        failed_vps=[
            {"id": "VP-013"},
            {"id": "VP-023", "recommendation": "defer"},
        ],
    )

    out = capsys.readouterr().out
    assert "repair_coverage_hitl_raised=['VP-023']" in out

    # 问题真的写出去了，而且点名这个 VP（``plan_decisions`` 里查得到）。
    assert len(spy.requests) == 1
    request = spy.requests[0]
    assert request.vp_id == "VP-023"
    assert "VP-023" in request.question
    assert "files_to_modify" in request.question
    # 默认跟随这条 VP 自己的 recommendation。
    assert request.default_option == "defer"

    # 丢掉的那一行没有变成任务 —— 它只有 HITL 这条出路。
    assert [task["failed_vp_id"] for task in tasks] == ["VP-013"]


# ---------------------------------------------------------------------------
# 边界 —— 生成失败、别的异常、取消
# ---------------------------------------------------------------------------


def test_generation_error_skips_the_check(orchestrator, capsys):
    """首批生成就失败的一轮 → 不校验、不提问（2026-09-14 park 语义）。"""
    orchestrator.repair_generator = _FakeGenerator([
        [{"failed_vp_id": "VP-023", "files_to_modify": ["backend/b.py"]}],
    ])

    tasks = _run(orchestrator, repair_generation_error="the model is down")

    assert orchestrator.repair_generator.calls == []
    assert [task["failed_vp_id"] for task in tasks] == ["VP-013"]

    out = capsys.readouterr().out
    assert "repair_coverage_checked skipped=generation_error" in out


def test_unexpected_error_keeps_the_round_alive(orchestrator, capsys):
    """别的异常 → 记 ``repair_coverage_check_failed``，原任务表返回。"""
    orchestrator.repair_generator = _FakeGenerator([
        RuntimeError("something else broke"),
    ])

    tasks = _run(orchestrator)

    assert [task["id"] for task in tasks] == ["repair-29"]
    out = capsys.readouterr().out
    assert "repair_coverage_check_failed" in out


def test_cancellation_is_not_swallowed(orchestrator):
    """取消原样抛出 —— 它不是「生成失败」，是操作员叫停。"""
    orchestrator.repair_generator = _FakeGenerator([
        VerificationCancelled("plan-x"),
    ])

    with pytest.raises(VerificationCancelled):
        _run(orchestrator)


def test_empty_scope_is_dropped_so_the_reason_is_known(orchestrator, monkeypatch):
    """空 ``files_to_modify`` 被丢掉 → 原因是「被丢掉」，不是「生成器漏了」。"""
    orchestrator.repair_generator = _FakeGenerator([
        [{"failed_vp_id": "VP-023", "files_to_modify": []}],
    ])
    asked = []

    def _ask(vp, reason, round_number):
        asked.append((vp, reason, round_number))
        return "DEC-R3"

    monkeypatch.setattr(
        orchestrator, "_ask_human_about_missing_repair", _ask,
    )

    _run(orchestrator)

    assert len(asked) == 1
    assert asked[0][1] == REASON_EMPTY_FILES_TO_MODIFY


# ---------------------------------------------------------------------------
# 边界 —— 补出来的那批也要过意图门禁
# ---------------------------------------------------------------------------


def test_regenerated_contents_pass_the_intent_gate(orchestrator, monkeypatch):
    """补出来的那一行若越过计划自己的约束 → 不成为任务，且冲突留痕。

    首批过了门禁、补出来的那批不过，等于门禁被绕开一次：这一轮的约束
    对补生成的那几条不生效，而它们照样会变成修复任务被执行。
    """
    orchestrator.repair_generator = _FakeGenerator([
        [{
            "failed_vp_id": "VP-023",
            "files_to_modify": ["engine_rs/src/signals.rs"],
            "intent_check": {
                "violates": True,
                "constraint": "不碰 Rust",
                "reason": "要改 engine_rs，违反计划约束",
            },
        }],
    ])
    asked = []

    def _ask(vp, reason, round_number):
        asked.append((vp, reason, round_number))
        return "DEC-R3"

    monkeypatch.setattr(
        orchestrator, "_ask_human_about_missing_repair", _ask,
    )

    tasks = _run(orchestrator, plan_intent=["不碰 Rust"])

    # 那一行没有变成任务 —— 它只有 HITL 这条出路。
    assert [task["id"] for task in tasks] == ["repair-29"]
    assert len(asked) == 1
    assert str(asked[0][0].get("id")) == "VP-023"


def test_intent_conflict_of_the_regenerated_batch_is_logged(
    orchestrator, capsys,
):
    """补出来的那批的冲突要看得见 —— 和首批一样是 WARNING。"""
    orchestrator.repair_generator = _FakeGenerator([
        [{
            "failed_vp_id": "VP-023",
            "files_to_modify": ["engine_rs/src/signals.rs"],
            "intent_check": {
                "violates": True,
                "constraint": "不碰 Rust",
                "reason": "要改 engine_rs，违反计划约束",
            },
        }],
    ])

    _run(orchestrator, plan_intent=["不碰 Rust"])

    out = capsys.readouterr().out
    assert "repair_intent_conflict" in out
    assert "VP-023" in out


# ---------------------------------------------------------------------------
# 边界 —— 没有批次可校验的那两种形状
# ---------------------------------------------------------------------------


def test_empty_first_batch_skips_the_check(orchestrator, capsys):
    """首批什么内容都没有 → 这一轮没有批次，覆盖无从谈起。

    生产里这等于「生成失败」：``generate_repair_contents`` 解析不出可用
    内容时会抛 ``RepairGenerationError``（park 语义已经接住它）。一个
    成功却空手的生成器是同一场失败的另一种穿法。
    """
    orchestrator.repair_generator = _FakeGenerator([
        [{"failed_vp_id": "VP-023", "files_to_modify": ["backend/b.py"]}],
    ])

    tasks = _run_empty_batch(orchestrator)

    assert orchestrator.repair_generator.calls == []
    assert tasks == []

    out = capsys.readouterr().out
    assert "repair_coverage_checked skipped=no_content" in out
    assert "repair_coverage_hitl_raised=[]" in out


def test_unattributable_batch_skips_the_check(orchestrator, capsys):
    """装配出来的行没有一行说得出它修的是哪个点 → 覆盖判定的输入不完整。

    生产里不会走到这里（``RepairTaskAssembler`` 发出的每一行都带着它修
    的那个点），这条是给「行不完整」的批次兜底的：遇到就说一声，不猜。
    """
    orchestrator.repair_generator = _FakeGenerator([
        [{"failed_vp_id": "VP-023", "files_to_modify": ["backend/b.py"]}],
    ])

    tasks = orchestrator._ensure_repair_coverage(
        [{"id": "repair-29", "title": "修 VP-013"}],
        [{"id": "VP-013"}, {"id": "VP-023"}],
        [{"failed_vp_id": "VP-013"}],
        3,
        failed_vps=[{"id": "VP-013"}, {"id": "VP-023"}],
        vp_test_commands={},
        plan_file=orchestrator.plan_dir / "verification_plan.json",
        existing_repair_ids={"repair-29"},
        generator_kwargs={},
    )

    assert orchestrator.repair_generator.calls == []
    assert [task["id"] for task in tasks] == ["repair-29"]

    out = capsys.readouterr().out
    assert "repair_coverage_checked skipped=unattributable_batch" in out
    assert "repair_coverage_hitl_raised=[]" in out


def test_no_candidates_is_nothing_to_check(orchestrator, capsys):
    """本轮没有需要 repair 任务的 VP → 没有覆盖目标，缺的是零个。"""
    orchestrator.repair_generator = _FakeGenerator([
        [{"failed_vp_id": "VP-023", "files_to_modify": ["backend/b.py"]}],
    ])

    tasks = orchestrator._ensure_repair_coverage(
        [],
        [],
        [],
        3,
        failed_vps=[],
        vp_test_commands={},
        plan_file=orchestrator.plan_dir / "verification_plan.json",
        existing_repair_ids=set(),
        generator_kwargs={},
    )

    assert orchestrator.repair_generator.calls == []
    assert tasks == []

    out = capsys.readouterr().out
    assert "repair_coverage_checked missing=[]" in out
    assert "repair_coverage_hitl_raised=[]" in out

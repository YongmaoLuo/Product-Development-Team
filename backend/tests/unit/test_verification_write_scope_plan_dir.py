"""生产路径把 plan 目录与轮次交给 SettingsJsonBuilder。

背景
----
任务 3 给 ``SettingsJsonBuilder`` 加了 ``plan_dir`` / ``round_no`` 两个字段 ——
可写范围由它们算出来，写进 settings.json 的 ``env``。但**没人把它们喂进去**：
``VerificationSubAgent.run`` 只收 ``plan_id``，构造 builder 时也就没传这两个值。
于是护栏在真实运行时拿不到可写根，等于没接上。

本文件钉三件事
--------------
  1. ``run(..., plan_dir=..., round_no=...)`` → builder 实例真的拿到了这两个值；
  2. 不传 ``plan_dir`` → builder 只收到两个 kwarg（``vp_id`` / ``plan_id``），
     窄签名的测试替身不会炸；
  3. 生产入口 ``Orchestrator._delegate_to_sub_agent`` → 把 ``self.plan_dir`` 与
     本轮轮次交给子 agent；轮次缺失时按 0 算。

断言方式
--------
不直接摸 builder 的 dataclass 字段就完事 —— 走**生产入口**：
``VerificationSubAgent.run`` 构造 builder 时用的正是那几个 kwarg。替身把构造
参数原样记下来，断言就落在"生产路径真的把这���值交出去了"这件事上。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_subagent  # noqa: E402
from verification_subagent import (  # noqa: E402
    BlockedPathError,
    VerificationSubAgent,
    Verdict,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stop_the_self_heal_loop(monkeypatch) -> None:
    """把自愈回路拦下来 —— 断言只看 builder，不看 LLM 会话。

    抛 :class:`BlockedPathError` 是有意的：它是 ``run()`` 自己认识的一种
    结束方式（安全网拦截），所以 ``run()`` 会正常返回一个 BLOCKED verdict，
    而不是把异常甩给测试。
    """

    async def _blocked_self_heal_loop(self, **kwargs):
        raise BlockedPathError(reason="stop before the LLM session", evidence="")

    monkeypatch.setattr(
        verification_subagent.VerificationSubAgent,
        "_self_heal_loop",
        _blocked_self_heal_loop,
    )


# ---------------------------------------------------------------------------
# 1. run() 把 plan_dir / round_no 交给 builder
# ---------------------------------------------------------------------------


def test_run_forwards_plan_dir_and_round(monkeypatch, tmp_path):
    """run(plan_dir=..., round_no=...) → builder 实例上的两个字段都对。"""
    captured: List[Any] = []

    class _RecordingBuilder:
        """把构造时收到的 kwarg 原样记下来。"""

        def __init__(self, vp_id, plan_id, **kwargs):
            self.vp_id = vp_id
            self.plan_id = plan_id
            self.plan_dir = kwargs.get("plan_dir")
            self.round_no = kwargs.get("round_no")
            captured.append(self)

        def build(self) -> str:
            return str(tmp_path / "verif_settings_test.json")

        def cleanup(self) -> None:
            return None

    monkeypatch.setattr(
        verification_subagent, "SettingsJsonBuilder", _RecordingBuilder,
    )
    _stop_the_self_heal_loop(monkeypatch)

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()

    agent = VerificationSubAgent(method="code_review")
    verdict = asyncio.run(
        agent.run(
            {"id": "VP-001"},
            coding_tool=None,
            log_dir=tmp_path / "logs",
            plan_dir=plan_dir,
            round_no=3,
        )
    )

    assert verdict.verdict == "BLOCKED"
    assert len(captured) == 1, captured
    builder = captured[0]
    assert builder.plan_dir == plan_dir, (
        f"builder 没拿到 plan_dir：{builder.plan_dir!r}"
    )
    assert builder.round_no == 3, (
        f"builder 没拿到 round_no：{builder.round_no!r}"
    )


# ---------------------------------------------------------------------------
# 2. 不传 plan_dir → 只构造两个 kwarg
# ---------------------------------------------------------------------------


def test_run_without_plan_dir_constructs_builder_with_two_kwargs(
    monkeypatch, tmp_path,
):
    """不带 plan_dir → builder 只收到 vp_id / plan_id 两个 kwarg。

    替身的签名是**故意窄**的：只接受 ``vp_id`` / ``plan_id``。多传一个
    kwarg 就是 TypeError —— 这正是"旧调用方（窄签名测试替身）不炸"这条边界
    条件的可执行版本：没有 plan_dir 就没有可写范围要声明，也就没什么理由
    多传参数。
    """
    captured: List[Any] = []

    class _NarrowSettingsBuilder:
        """窄签名替身：只接受 vp_id / plan_id。"""

        def __init__(self, vp_id, plan_id):
            self.vp_id = vp_id
            self.plan_id = plan_id
            captured.append(self)

        def build(self) -> str:
            return str(tmp_path / "verif_settings_test.json")

        def cleanup(self) -> None:
            return None

    monkeypatch.setattr(
        verification_subagent, "SettingsJsonBuilder", _NarrowSettingsBuilder,
    )
    _stop_the_self_heal_loop(monkeypatch)

    agent = VerificationSubAgent(method="code_review")
    verdict = asyncio.run(
        agent.run(
            {"id": "VP-001"},
            coding_tool=None,
            log_dir=tmp_path / "logs",
        )
    )

    assert verdict.verdict == "BLOCKED"
    assert len(captured) == 1, captured
    assert captured[0].vp_id == "VP-001"
    assert captured[0].plan_id == "default"


# ---------------------------------------------------------------------------
# 3. 生产入口：Orchestrator 把 plan 目录与轮次交给子 agent
# ---------------------------------------------------------------------------


class _FakeSubAgent:
    """子 agent 替身：只记下 run() 收到的 kwargs。"""

    def __init__(self, **kwargs) -> None:
        self.init_kwargs = kwargs
        self.run_kwargs: Dict[str, Any] = {}

    async def run(self, **kwargs):
        self.run_kwargs = dict(kwargs)
        return Verdict(verdict="PASSED", reasons=["stub"], evidence=[])


def _make_orchestrator(monkeypatch, tmp_path, round_no=None):
    """一个够用的 Orchestrator —— 只带 ``_delegate_to_sub_agent`` 用到的字段。"""
    import verification_agent
    from verification_agent import Orchestrator

    captured: List[_FakeSubAgent] = []

    def _factory(**kwargs):
        fake = _FakeSubAgent(**kwargs)
        captured.append(fake)
        return fake

    monkeypatch.setattr(verification_agent, "VerificationSubAgent", _factory)

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.plan_dir = tmp_path / "plan"
    orchestrator.project_dir = tmp_path / "project"
    orchestrator.coding_tool = None
    orchestrator.persistence = None
    if round_no is not None:
        orchestrator._current_round = round_no
    return orchestrator, captured


def test_delegate_to_sub_agent_forwards_plan_dir_and_round(monkeypatch, tmp_path):
    """生产入口把 plan 目录与本轮轮次交给子 agent。"""
    orchestrator, captured = _make_orchestrator(monkeypatch, tmp_path, round_no=3)

    asyncio.run(
        orchestrator._delegate_to_sub_agent(
            {"id": "VP-001"}, "code_review", "VP-001",
        )
    )

    assert len(captured) == 1, captured
    run_kwargs = captured[0].run_kwargs
    assert run_kwargs.get("plan_dir") == tmp_path / "plan", run_kwargs
    assert run_kwargs.get("round_no") == 3, run_kwargs


def test_delegate_to_sub_agent_defaults_round_no_to_zero(monkeypatch, tmp_path):
    """没有轮次（``_current_round`` 缺失）→ 按 0 算，不炸。"""
    orchestrator, captured = _make_orchestrator(monkeypatch, tmp_path)

    asyncio.run(
        orchestrator._delegate_to_sub_agent(
            {"id": "VP-001"}, "code_review", "VP-001",
        )
    )

    assert len(captured) == 1, captured
    run_kwargs = captured[0].run_kwargs
    assert run_kwargs.get("plan_dir") == tmp_path / "plan", run_kwargs
    assert run_kwargs.get("round_no") == 0, run_kwargs

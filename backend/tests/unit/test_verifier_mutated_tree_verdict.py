"""一个越权写过被验证仓库的 VP，不许拿着 PASSED 回轮次。

背景
----
2026-11-02（写入范围落地）：验证子 agent 本该**只读**被验证仓库。写护栏
（``backend/verification_write_guard.py``）把越界的 Edit / Write 机械拒绝，
并在 plan 目录里留下证据::

    <plan>/vp_write_scope/refusals/round-<n>/<vp>.jsonl

事故里整轮失真，就是因为这条证据只有轮次收尾才读得到（任务 6：轮次结束
按拒绝证据隔离并还原工作区），而 VP 自己仍然拿着 PASSED 回了轮次 —— 那一
轮在账面上是绿的，而事实上有一个验证点越权改了树。

本文件钉四件事
--------------
  1. 有拒绝记录的 VP → 改判 FAILED，原因前缀 ``verifier_mutated_tree``，
     失败分类 ``infra`` / 来源 ``deterministic``；
  2. 没有拒绝记录 → 原 verdict 原样返回；
  3. 别的**轮次**的记录不算进这一轮；
  4. 别的 **VP** 的记录不算在这个 VP 头上。

断言方式
--------
走**生产入口** ``Orchestrator._delegate_to_sub_agent``：子 agent 被打桩成
"总是回 PASSED"，拒绝证据由护栏自己写（``record_refusal`` —— marker 的
格式是护栏与读回方之间的契约，自己手写的 marker 只能证明读得懂自己写的
东西）。断言落在"生产路径真的把 PASSED 改判成 FAILED"这件事上。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_write_guard  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402
from verification_subagent import Verdict  # noqa: E402

#: The event the rewritten verdict is recorded under.
MUTATION_EVENT = "verifier_mutated_tree"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakePersistence:
    """Stands in for the persistence manager the orchestrator writes through."""

    def __init__(self) -> None:
        self.events: List[Tuple[str, str, Dict[str, Any]]] = []

    def write_verification_point_log(
        self, vp_id: str, event_type: str, data: Dict[str, Any],
    ) -> None:
        self.events.append((vp_id, event_type, data))


def _passed_verdict() -> Verdict:
    """The verdict a sub-agent hands back when everything went fine."""
    return Verdict(
        verdict="PASSED",
        reasons=["stub"],
        evidence=["stub evidence"],
        provider="vendor-a-pro",
        chosen_model="Vendor A-M3",
        model_complexity="simple",
    )


def _orchestrator(
    monkeypatch, tmp_path: Path, round_no: int = 2,
) -> Tuple[Orchestrator, Verdict, List[tuple]]:
    """An ``Orchestrator`` with only the attributes this path reads.

    ``__init__`` is skipped on purpose: it wires a coding tool, a
    persistence manager and a venv ``PATH`` prefix, none of which these
    cases need.
    """
    import verification_agent

    original = _passed_verdict()
    events: List[tuple] = []

    class _FakeSubAgent:
        """A sub-agent that always says PASSED."""

        def __init__(self, **kwargs) -> None:
            self.init_kwargs = kwargs
            self.run_kwargs: Dict[str, Any] = {}

        async def run(self, **kwargs):
            self.run_kwargs = dict(kwargs)
            return original

    monkeypatch.setattr(verification_agent, "VerificationSubAgent", _FakeSubAgent)

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.plan_dir = tmp_path / "plan"
    orchestrator.plan_dir.mkdir(parents=True, exist_ok=True)
    orchestrator.project_dir = tmp_path / "project"
    orchestrator.coding_tool = None
    orchestrator.persistence = _FakePersistence()
    orchestrator._current_round = round_no
    orchestrator._safe_log_freshness_event = (
        lambda vp_id, event_type, data: events.append((vp_id, event_type, data))
    )
    return orchestrator, original, events


def _record_refusal(
    monkeypatch,
    plan_dir: Path,
    round_no: int,
    vp_id: str,
    path: Path,
) -> Optional[Path]:
    """Write a refusal marker the way the guard itself writes it."""
    monkeypatch.setenv(verification_write_guard.SCOPE_ENV, str(plan_dir))
    monkeypatch.setenv(verification_write_guard.VP_ID_ENV, vp_id)
    monkeypatch.setenv(verification_write_guard.ROUND_ENV, str(round_no))
    return verification_write_guard.record_refusal(
        "Edit", str(path), verification_write_guard.REASON_OUTSIDE_SCOPE,
    )


def _mutation_events(events: List[tuple]) -> List[tuple]:
    """The mutation events out of the recorded ones."""
    return [event for event in events if event[1] == MUTATION_EVENT]


def _delegate(orchestrator: Orchestrator, vp_id: str) -> Verdict:
    """Run the production entry point for one VP."""
    return asyncio.run(
        orchestrator._delegate_to_sub_agent({"id": vp_id}, "code_review", vp_id),
    )


# ---------------------------------------------------------------------------
# 1. 有拒绝记录 → PASSED 改判 FAILED
# ---------------------------------------------------------------------------
def test_refusal_turns_passed_into_verifier_mutated_tree(monkeypatch, tmp_path):
    """VP 试图写被验证仓库 → PASSED 改判 FAILED（infra / deterministic）。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=2)
    target = tmp_path / "project" / "a.py"
    _record_refusal(
        monkeypatch, orchestrator.plan_dir, round_no=2, vp_id="VP-055", path=target,
    )

    verdict = _delegate(orchestrator, "VP-055")

    # the verdict is no longer the sub-agent's
    assert verdict.verdict == "FAILED", verdict
    assert verdict.failure_class == "infra", verdict
    assert verdict.failure_class_source == "deterministic", verdict

    # and it says why, in the reason prefix the downstream readers key on
    assert verdict.reasons, "a rewritten verdict has to say why"
    assert verdict.reasons[0].startswith("verifier_mutated_tree"), verdict.reasons
    assert str(target) in verdict.reasons[0], verdict.reasons

    # the routing facts are still the facts of this attempt
    assert verdict.provider == original.provider, verdict
    assert verdict.chosen_model == original.chosen_model, verdict
    assert verdict.model_complexity == original.model_complexity, verdict

    # the round is told what happened
    recorded = _mutation_events(events)
    assert len(recorded) == 1, events
    vp_id, _, data = recorded[0]
    assert vp_id == "VP-055", recorded
    assert data["paths"] == [str(target)], data
    assert data["count"] == 1, data


# ---------------------------------------------------------------------------
# 2. 没有拒绝记录 → 原 verdict 原样返回
# ---------------------------------------------------------------------------
def test_no_refusal_keeps_the_verdict(monkeypatch, tmp_path):
    """没有拒绝记录 → verdict 不动，也不记事件。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=2)

    verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "PASSED", verdict
    assert verdict is original, "没有拒绝证据时原样返回，不重建"
    assert verdict.failure_class == "", verdict
    assert _mutation_events(events) == [], events


# ---------------------------------------------------------------------------
# 3. 别的轮次的记录不算进这一轮
# ---------------------------------------------------------------------------
def test_refusal_from_another_round_is_ignored(monkeypatch, tmp_path):
    """上一轮的拒绝证据 → 这一轮不受影响。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=2)
    _record_refusal(
        monkeypatch, orchestrator.plan_dir, round_no=1, vp_id="VP-055",
        path=tmp_path / "project" / "a.py",
    )

    verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "PASSED", verdict
    assert verdict is original, verdict
    assert _mutation_events(events) == [], events


# ---------------------------------------------------------------------------
# 4. 别的 VP 的记录不算在这个 VP 头上
# ---------------------------------------------------------------------------
def test_refusal_from_another_vp_is_ignored(monkeypatch, tmp_path):
    """别的 VP 越权写 → 这个 VP 的 verdict 不受影响。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=2)
    _record_refusal(
        monkeypatch, orchestrator.plan_dir, round_no=2, vp_id="VP-999",
        path=tmp_path / "project" / "a.py",
    )

    verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "PASSED", verdict
    assert verdict is original, verdict
    assert _mutation_events(events) == [], events

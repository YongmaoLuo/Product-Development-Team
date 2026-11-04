"""负控：树外拒绝不再改判之后，写进被验证树的拒绝**仍然**要改判。

背景
----
2026-11-04（树外拒绝不再改判）：``_apply_refusal_evidence`` 改成按
``project_dir`` 把拒绝记录分成「树内 / 树外」—— 树外的（如写
``~/.claude/projects/<project>/memory/…``）只记事件、不改判 verdict。

这个放宽是**有意的**，但它的方向必须**两边都证明**：

  * 树外 → 不改判（由 ``test_verifier_refused_outside_scope.py`` 钉住）；
  * 树内 → **仍然改判**（由本文件钉住）。

只钉一边是不够的：一次「把所有拒绝都当树外」的改写，能让树外那条用例继续
绿，却把真正的越权写入放过去了 —— 那正是 2026-11-02 那次事故要防的东西。
所以本文件是那次放宽的**负控**：它必须证明 ``verifier_mutated_tree`` 这条
改判没有被削弱。

本文件钉三件事
--------------
  1. 树内路径被拒 → 仍然改判 FAILED（infra / deterministic），理由点名那条路径；
  2. 混合记录（1 条树内 + 1 条树外）→ 改判，理由**只列树内路径**，两类事件
     **各记一条**、互不吞并；
  3. 同一路径被拒两次 → ``paths`` 去重为 1，但 ``count`` 记 2（尝试数）。

断言方式
--------
走**生产入口** ``Orchestrator._delegate_to_sub_agent``：子 agent 被打桩成
「总是回 PASSED」，拒绝证据由护栏自己写（``record_refusal`` —— marker 的格式
是护栏与读回方之间的契约）。断言落在「生产路径真的按树内 / 树外分组、真的
仍然改判」这件事上。
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

#: The event an in-tree refusal is recorded under.
MUTATION_EVENT = "verifier_mutated_tree"

#: The event an out-of-tree refusal is recorded under.
OUTSIDE_EVENT = "verifier_refused_outside_scope"


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
    path: Any,
) -> Optional[Path]:
    """Write a refusal marker the way the guard itself writes it."""
    monkeypatch.setenv(verification_write_guard.SCOPE_ENV, str(plan_dir))
    monkeypatch.setenv(verification_write_guard.VP_ID_ENV, vp_id)
    monkeypatch.setenv(verification_write_guard.ROUND_ENV, str(round_no))
    return verification_write_guard.record_refusal(
        "Edit", str(path), verification_write_guard.REASON_OUTSIDE_SCOPE,
    )


def _events_of(events: List[tuple], event_type: str) -> List[tuple]:
    """The recorded events of one type."""
    return [event for event in events if event[1] == event_type]


def _delegate(orchestrator: Orchestrator, vp_id: str) -> Verdict:
    """Run the production entry point for one VP."""
    return asyncio.run(
        orchestrator._delegate_to_sub_agent({"id": vp_id}, "code_review", vp_id),
    )


# ---------------------------------------------------------------------------
# 1. 树内的拒绝 → 仍然改判
# ---------------------------------------------------------------------------
def test_in_tree_refusal_still_fails_the_vp(monkeypatch, tmp_path):
    """写进被验证树的拒绝 → 仍然 FAILED（infra / deterministic），理由点名路径。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=2)
    project_dir = tmp_path / "project"
    in_tree = project_dir / "backend" / "app.py"
    _record_refusal(
        monkeypatch, orchestrator.plan_dir, round_no=2, vp_id="VP-055",
        path=in_tree,
    )

    verdict = _delegate(orchestrator, "VP-055")

    # the sub-agent's PASSED does not survive a write into the tree
    assert verdict is not original, "树内越权写必须改判，不许原样返回"
    assert verdict.verdict == "FAILED", verdict
    assert verdict.failure_class == "infra", verdict
    assert verdict.failure_class_source == "deterministic", verdict

    # and it says why, naming the path that was written
    assert verdict.reasons, "改判后的 verdict 必须说清为什么"
    assert verdict.reasons[0] == (
        f"verifier_mutated_tree: 尝试写 {in_tree}"
    ), verdict.reasons

    # the round is told what happened
    recorded = _events_of(events, MUTATION_EVENT)
    assert len(recorded) == 1, events
    vp_id, _, data = recorded[0]
    assert vp_id == "VP-055", recorded
    assert data["paths"] == [str(in_tree)], data
    assert data["count"] == 1, data

    # a write into the tree is not an out-of-tree refusal
    assert _events_of(events, OUTSIDE_EVENT) == [], events


# ---------------------------------------------------------------------------
# 2. 混合记录 → 改判，理由只列树内，两类事件各一条
# ---------------------------------------------------------------------------
def test_mixed_records_fail_and_report_both_events(monkeypatch, tmp_path):
    """一条树内 + 一条树外 → 改判；两类事件各记一条，互不吞并。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=2)
    project_dir = tmp_path / "project"
    in_tree = project_dir / "backend" / "app.py"
    outside = "~/.claude/projects/p/memory/x.md"
    _record_refusal(
        monkeypatch, orchestrator.plan_dir, round_no=2, vp_id="VP-055",
        path=in_tree,
    )
    _record_refusal(
        monkeypatch, orchestrator.plan_dir, round_no=2, vp_id="VP-055",
        path=outside,
    )

    verdict = _delegate(orchestrator, "VP-055")

    # the in-tree write still sinks the verdict
    assert verdict.verdict == "FAILED", verdict
    assert verdict.failure_class == "infra", verdict
    assert verdict.failure_class_source == "deterministic", verdict

    # the reason names the tree write — and only the tree write
    assert verdict.reasons == [
        f"verifier_mutated_tree: 尝试写 {in_tree}",
    ], verdict.reasons
    assert outside not in " ".join(verdict.reasons), verdict.reasons

    # the mutation event counts only the tree write
    mutation = _events_of(events, MUTATION_EVENT)
    assert len(mutation) == 1, events
    _, _, data = mutation[0]
    assert data["paths"] == [str(in_tree)], data
    assert data["count"] == 1, data

    # and the out-of-tree refusal is still on record, under its own event
    outside_events = _events_of(events, OUTSIDE_EVENT)
    assert len(outside_events) == 1, events
    vp_id, _, outside_data = outside_events[0]
    assert vp_id == "VP-055", outside_events
    assert outside_data["paths"] == [outside], outside_data
    assert outside_data["count"] == 1, outside_data


# ---------------------------------------------------------------------------
# 3. 同一路径被拒两次 → paths 去重，count 记尝试数
# ---------------------------------------------------------------------------
def test_duplicate_path_counts_attempts(monkeypatch, tmp_path):
    """同一路径被拒两次 → ``paths`` 一条，``count`` 两次。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=2)
    project_dir = tmp_path / "project"
    in_tree = project_dir / "backend" / "app.py"
    _record_refusal(
        monkeypatch, orchestrator.plan_dir, round_no=2, vp_id="VP-055",
        path=in_tree,
    )
    _record_refusal(
        monkeypatch, orchestrator.plan_dir, round_no=2, vp_id="VP-055",
        path=in_tree,
    )

    verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "FAILED", verdict
    assert verdict.failure_class == "infra", verdict

    # one path, one reason
    recorded = _events_of(events, MUTATION_EVENT)
    assert len(recorded) == 1, events
    _, _, data = recorded[0]
    assert data["paths"] == [str(in_tree)], data
    assert len(data["paths"]) == 1, data
    assert verdict.reasons == [
        f"verifier_mutated_tree: 尝试写 {in_tree}",
    ], verdict.reasons

    # but two attempts were refused — the operator wants both numbers
    assert data["count"] == 2, data

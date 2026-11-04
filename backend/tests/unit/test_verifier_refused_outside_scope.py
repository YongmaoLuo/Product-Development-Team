"""树外的拒绝不改判 —— 只有写进被验证树的拒绝才算「改了树」。

背景
----
2026-11-04（写护栏假阳性之二）：``_apply_refusal_evidence`` 只按
``read_refusals`` 的条数改判。VP-024 有一条拒绝写的是
``~/.claude/projects/<project>/memory/…`` —— 写 memory 目录不可能改动被验证
仓库，却照样把 PASSED 改判成 FAILED（``failure_class=infra``），一条与被验证
仓库毫无关系的记录把整轮验证拖垮。

本文件钉四件事
--------------
  1. 树外的拒绝 → verdict 原样返回，另记 ``verifier_refused_outside_scope``；
  2. ``is_inside_tree`` 展开 ``~``、解析 ``..``，只有落在树内才算 True；
  3. 没有 ``project_dir`` → fail-open，不改判；
  4. 空 ``path`` → 仍算树内（保留「路径未记录」的改判语义）。

断言方式
--------
走**生产入口** ``Orchestrator._delegate_to_sub_agent``：子 agent 被打桩成
「总是回 PASSED」，拒绝证据由护栏自己写（``record_refusal`` —— marker 的
格式是护栏与读回方之间的契约）。断言落在「生产路径真的按树内 / 树外分组」
这件事上。
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

#: The event an out-of-tree refusal is recorded under.
OUTSIDE_EVENT = "verifier_refused_outside_scope"

#: The event an in-tree refusal is recorded under.
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
    monkeypatch,
    tmp_path: Path,
    round_no: int = 1,
    with_project_dir: bool = True,
) -> Tuple[Orchestrator, Verdict, List[tuple]]:
    """An ``Orchestrator`` with only the attributes this path reads.

    ``__init__`` is skipped on purpose: it wires a coding tool, a persistence
    manager and a venv ``PATH`` prefix, none of which these cases need.
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
    orchestrator.project_dir = tmp_path / "project" if with_project_dir else None
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
# 1. 树外的拒绝 → 不改判
# ---------------------------------------------------------------------------
def test_out_of_scope_refusal_keeps_the_verdict(monkeypatch, tmp_path):
    """树外的 memory 写入 → verdict 原样返回，另记树外事件。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)
    outside_path = "~/.claude/projects/p/memory/x.md"
    _record_refusal(
        monkeypatch, orchestrator.plan_dir, round_no=1, vp_id="VP-024",
        path=outside_path,
    )

    verdict = _delegate(orchestrator, "VP-024")

    # the sub-agent's verdict stands
    assert verdict is original, "树外的拒绝不改判"
    assert verdict.verdict == "PASSED", verdict
    assert verdict.failure_class == "", verdict

    # and no mutation was claimed
    assert _events_of(events, MUTATION_EVENT) == [], events

    # but the refusal is on record, under its own event
    outside = _events_of(events, OUTSIDE_EVENT)
    assert len(outside) == 1, events
    vp_id, _, data = outside[0]
    assert vp_id == "VP-024", outside
    assert data["count"] == 1, data
    assert data["round"] == 1, data
    assert data["paths"] == [outside_path], data


# ---------------------------------------------------------------------------
# 2. is_inside_tree：展开 ~、解析 ..、按分隔符收尾
# ---------------------------------------------------------------------------
def test_is_inside_tree_expands_and_resolves(tmp_path):
    """``is_inside_tree`` 只认落在树内的路径。"""
    root = tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)

    # inside
    assert verification_write_guard.is_inside_tree(str(root), root) is True
    assert verification_write_guard.is_inside_tree(str(root / "a.py"), root) is True
    assert (
        verification_write_guard.is_inside_tree(str(root / "sub" / "b.py"), root)
        is True
    )

    # outside
    assert (
        verification_write_guard.is_inside_tree(str(tmp_path / "other" / "a.py"), root)
        is False
    )
    # ``~`` is expanded before the comparison
    assert verification_write_guard.is_inside_tree("~/x", root) is False
    # a relative path that climbs out of the tree
    assert verification_write_guard.is_inside_tree("a/../..", root) is False
    # a sibling with the tree's name as a prefix is not inside it
    assert verification_write_guard.is_inside_tree(str(root) + "-evil", root) is False

    # nothing to compare → not inside
    assert verification_write_guard.is_inside_tree("", root) is False
    assert verification_write_guard.is_inside_tree(str(root / "a.py"), "") is False


# ---------------------------------------------------------------------------
# 3. 没有 project_dir → fail-open
# ---------------------------------------------------------------------------
def test_missing_project_dir_keeps_the_verdict(monkeypatch, tmp_path):
    """没有 ``project_dir`` → 原样返回，不改判。"""
    orchestrator, original, events = _orchestrator(
        monkeypatch, tmp_path, round_no=1, with_project_dir=False,
    )
    _record_refusal(
        monkeypatch, orchestrator.plan_dir, round_no=1, vp_id="VP-024",
        path=str(tmp_path / "project" / "a.py"),
    )

    verdict = _delegate(orchestrator, "VP-024")

    assert verdict is original, "没有 project_dir 就不改判"
    assert verdict.verdict == "PASSED", verdict
    assert _events_of(events, MUTATION_EVENT) == [], events

    # the attribute being absent entirely is the same case
    del orchestrator.project_dir
    direct = orchestrator._apply_refusal_evidence(_passed_verdict(), "VP-024")
    assert direct.verdict == "PASSED", direct
    assert _events_of(events, MUTATION_EVENT) == [], events


# ---------------------------------------------------------------------------
# 4. 空 path → 仍算树内（路径未记录的语义不能丢）
# ---------------------------------------------------------------------------
def test_empty_path_still_counts_as_mutation(monkeypatch, tmp_path):
    """空 path → 仍改判 FAILED（infra / deterministic）。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)
    _record_refusal(
        monkeypatch, orchestrator.plan_dir, round_no=1, vp_id="VP-024", path="",
    )

    verdict = _delegate(orchestrator, "VP-024")

    assert verdict.verdict == "FAILED", verdict
    assert verdict.failure_class == "infra", verdict
    assert verdict.failure_class_source == "deterministic", verdict
    assert verdict.reasons, "改判后的 verdict 必须说清为什么"
    assert verdict.reasons[0].startswith("verifier_mutated_tree"), verdict.reasons

    recorded = _events_of(events, MUTATION_EVENT)
    assert len(recorded) == 1, events
    assert recorded[0][2]["count"] == 1, recorded

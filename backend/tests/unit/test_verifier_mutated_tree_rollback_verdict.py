"""改过被验证树的 VP，不许拿着 PASSED 回轮次。

背景
----
缺陷 A 第 3 条后半：写护栏**拒绝**的写能改判（``refusals`` 账本，见
``test_verifier_mutated_tree_verdict.py``），但护栏**测量到**的写没有进
判定层 —— 树护栏在 post 阶段量到「被验证的树被动过」，于是存证、回滚、
记一笔账（``mutations`` 账本），而判定层压根没读过这份账本：VP 改了
自己验证的树，它照样交回一个 PASSED。

那条 PASSED 是在一棵**它自己动过手脚**的树上给出的 —— 结论的依据已经
不可采信（arch 决策点 3）。所以有 mutation 记录的 VP 一律改判 FAILED，
失败分类 ``infra``（出 malfunction 的是这次尝试本身，不是交付物），来源
``deterministic``（按规则推出来的，不是模型说的）。

本文件钉四件事
--------------
  1. ``test_mutation_evidence_fails_the_vp`` —— 有记录 → 改判 FAILED，
     ``failure_class=infra`` / ``failure_class_source=deterministic``，
     原因前缀 ``verifier_mutated_tree``；
  2. ``test_event_carries_all_fields`` —— 事件 ``verifier_mutated_tree``
     带全字段（created / modified / deleted / patch / tool_use_id /
     command）；
  3. ``test_no_record_leaves_verdict_untouched`` —— 没有记录 → 原 verdict
     原样返回（连对象都不重建）；
  4. ``test_other_vp_records_do_not_affect`` —— 别的 VP 的记录不算在这个
     VP 头上。

外加两条边界条件：别的**轮次**的记录不算进这一轮；账本**读不出来** →
原样返回（账本坏了不等于 VP 越权 —— 拿一个读不出来的账本去否证一个 VP，
比放过它更坏）。

断言方式
--------
走**生产入口** ``Orchestrator._delegate_to_sub_agent``：子 agent 被打桩成
"总是回 PASSED"，mutation 记录由树护栏自己写（``record_mutation`` ——
账本的格式是写入方与读回方之间的契约，自己手写的行只能证明读得懂自己
写的东西）。断言落在"生产路径真的把 PASSED 改判成 FAILED"这件事上。

全部跑在 ``tmp_path`` 下的临时 git 仓库里 —— 被测的仓库一个字节都不碰。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_agent  # noqa: E402
import verification_tree_guard as guard  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402
from verification_subagent import Verdict  # noqa: E402
from tree_snapshot import snapshot  # noqa: E402

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
    monkeypatch, tmp_path: Path, round_no: int = 1,
) -> Tuple[Orchestrator, Verdict, List[tuple]]:
    """An ``Orchestrator`` with only the attributes this path reads.

    ``__init__`` is skipped on purpose: it wires a coding tool, a
    persistence manager and a venv ``PATH`` prefix, none of which these
    cases need.
    """
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


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` inside ``repo`` and return its stdout."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


def _init_repo(repo: Path) -> Path:
    """Create a throw-away git repository with one tracked file."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


def _mutate(repo: Path) -> Dict[str, Any]:
    """Do what a verifier's command did: rewrite a tracked file, add a new one.

    Returns the payload the post phase hands to :func:`record_mutation`.
    """
    before = snapshot(repo)
    (repo / "tracked.txt").write_text("tracked\nx\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    return {
        "root": str(repo),
        "command": "echo x >> tracked.txt",
        "tool_use_id": "tu_1",
        "created": ["new.txt"],
        "modified": ["tracked.txt"],
        "deleted": [],
        "before": guard.snapshot_to_dict(before),
    }


def _record_mutation(
    plan_dir: Path, round_no: int, vp_id: str, repo: Path,
) -> Path:
    """Mutate ``repo`` and let the tree guard store the evidence + record it.

    The mutation happens through the production writer
    (:func:`verification_tree_guard.record_mutation`), so the ledger line
    is the one the guard really writes.
    """
    payload = _mutate(repo)
    return guard.record_mutation(plan_dir, round_no, vp_id, payload)


def _mutation_events(events: List[tuple]) -> List[tuple]:
    """The mutation events out of the recorded ones."""
    return [event for event in events if event[1] == MUTATION_EVENT]


def _delegate(orchestrator: Orchestrator, vp_id: str) -> Verdict:
    """Run the production entry point for one VP."""
    return asyncio.run(
        orchestrator._delegate_to_sub_agent({"id": vp_id}, "code_review", vp_id),
    )


# ---------------------------------------------------------------------------
# 1. 有 mutation 记录 → PASSED 改判 FAILED
# ---------------------------------------------------------------------------
def test_mutation_evidence_fails_the_vp(monkeypatch, tmp_path):
    """VP 改过被验证树 → PASSED 改判 FAILED（infra / deterministic）。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)
    repo = _init_repo(tmp_path / "project")
    _record_mutation(orchestrator.plan_dir, 1, "VP-055", repo)

    verdict = _delegate(orchestrator, "VP-055")

    # the verdict is no longer the sub-agent's
    assert verdict.verdict == "FAILED", verdict
    assert verdict.failure_class == "infra", verdict
    assert verdict.failure_class_source == "deterministic", verdict

    # and it says why, in the reason prefix the downstream readers key on
    assert verdict.reasons, "a rewritten verdict has to say why"
    assert verdict.reasons[0] == "verifier_mutated_tree: 改动 new.txt", verdict.reasons
    assert "verifier_mutated_tree: 改动 tracked.txt" in verdict.reasons, verdict.reasons

    # the routing facts are still the facts of this attempt
    assert verdict.provider == original.provider, verdict
    assert verdict.chosen_model == original.chosen_model, verdict
    assert verdict.model_complexity == original.model_complexity, verdict


# ---------------------------------------------------------------------------
# 2. 事件带全字段
# ---------------------------------------------------------------------------
def test_event_carries_all_fields(monkeypatch, tmp_path):
    """事件 ``verifier_mutated_tree`` 带 created/modified/deleted/patch/
    tool_use_id/command —— 运营拿事件就能对上账本那一行。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)
    repo = _init_repo(tmp_path / "project")
    _record_mutation(orchestrator.plan_dir, 1, "VP-055", repo)

    _delegate(orchestrator, "VP-055")

    recorded = _mutation_events(events)
    assert len(recorded) == 1, events
    vp_id, _, data = recorded[0]
    assert vp_id == "VP-055", recorded

    assert data["created"] == ["new.txt"], data
    assert data["modified"] == ["tracked.txt"], data
    assert data["deleted"] == [], data
    assert data["tool_use_id"] == "tu_1", data
    assert data["command"] == "echo x >> tracked.txt", data

    # the patch is where the evidence was stored — it has to be a real file
    assert data["patch"], data
    assert Path(str(data["patch"])).exists(), data


# ---------------------------------------------------------------------------
# 3. 没有记录 → 原 verdict 原样返回
# ---------------------------------------------------------------------------
def test_no_record_leaves_verdict_untouched(monkeypatch, tmp_path):
    """没有 mutation 记录 → verdict 不动，也不记事件。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)

    verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "PASSED", verdict
    assert verdict is original, "没有 mutation 证据时原样返回，不重建"
    assert verdict.failure_class == "", verdict
    assert _mutation_events(events) == [], events


# ---------------------------------------------------------------------------
# 4. 别的 VP 的记录不算在这个 VP 头上
# ---------------------------------------------------------------------------
def test_other_vp_records_do_not_affect(monkeypatch, tmp_path):
    """别的 VP 改过树 → 这个 VP 的 verdict 不受影响。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)
    repo = _init_repo(tmp_path / "project")
    _record_mutation(orchestrator.plan_dir, 1, "VP-999", repo)

    verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "PASSED", verdict
    assert verdict is original, verdict
    assert _mutation_events(events) == [], events


# ---------------------------------------------------------------------------
# 边界条件：别的轮次不算进这一轮
# ---------------------------------------------------------------------------
def test_another_round_is_ignored(monkeypatch, tmp_path):
    """上一轮改过树 → 这一轮不受影响。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=2)
    repo = _init_repo(tmp_path / "project")
    _record_mutation(orchestrator.plan_dir, 1, "VP-055", repo)

    verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "PASSED", verdict
    assert verdict is original, verdict
    assert _mutation_events(events) == [], events


# ---------------------------------------------------------------------------
# 边界条件：账本读不出来 → 原样返回
# ---------------------------------------------------------------------------
def test_unreadable_ledger_leaves_verdict_untouched(monkeypatch, tmp_path):
    """账本读不出来 → verdict 不动（账本坏了不等于 VP 越权）。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)

    def _boom(*args, **kwargs):
        raise OSError("ledger is on fire")

    monkeypatch.setattr(verification_agent, "read_mutations", _boom)

    verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "PASSED", verdict
    assert verdict is original, verdict
    assert _mutation_events(events) == [], events

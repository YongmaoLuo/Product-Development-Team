"""改过被验证树的 VP：记一条 warning，**不改判**。

背景
----
缺陷 A 第 3 条后半：写护栏**拒绝**的写能改判（``refusals`` 账本，见
``test_verifier_mutated_tree_verdict.py``），但护栏**测量到**的写没有进
判定层 —— 树护栏在 post 阶段量到「被验证的树被动过」，于是存证、回滚、
记一笔账（``mutations`` 账本）。最初判定层把有 mutation 记录的 VP **一律
改判 FAILED**（arch 决策点 3），理由是「那条 PASSED 是在一棵被自己动过
手脚的树上给出的」。

2026-11-07（用户裁定）
----------------------
**回滚已经发生了，就没有必要再因为「改了树」去失败这个 VP、更不能因此停
掉迭代。** 所以 mutation 这条路径降级成**一条 warning**：

  - 记一条 WARNING 日志（vp_id / round / created / modified / deleted /
    patch）；
  - 继续写 freshness 事件 ``verifier_mutated_tree``（运营靠它对账本），
    事件里带 ``"severity": "warning"`` —— 让读事件的人知道它**不再是否决**；
  - 把「本 VP 改过树」这条事实附到 verdict 的 **evidence** 里（保留可追
    溯性），**不改 status**、不改 reasons、不写失败分类。

实测的代价（这条降级的由来）：pdt-user 那一轮 12 个 VP 被改判 FAILED，其中
VP-015 的账本记录的命令**全是只读命令**（``python3 -I -c`` 读 JSON +
``grep``），却被记成 created ``backend/tests/unit/test_vp002_origin_test.py``；
Phase 2 关卡据此把 VP-023/024 延后，整轮直接停在第 1 轮。

**不改的那条路径**：护栏**当场拒绝**的写（``verifier_mutated_tree: 尝试写
…``）没有发生、树是干净的，那一条**仍然改判**（见
``test_verifier_mutated_tree_verdict.py``）—— 本文件不碰它。

本文件钉四件事
--------------
  1. ``test_mutation_evidence_warns_without_rewriting_the_vp`` —— 有记录 →
     verdict 的 status **不变**（PASSED 仍是 PASSED）、WARNING 被记录、
     evidence 里出现被改路径、**不出现** FAILED / ``infra`` /
     ``verifier_mutated_tree:`` 改判原因；
  2. ``test_event_carries_all_fields`` —— 事件 ``verifier_mutated_tree``
     带全字段（created / modified / deleted / patch / tool_use_id /
     command）外加 ``severity=warning``；
  3. ``test_no_record_leaves_verdict_untouched`` —— 没有记录 → 原 verdict
     原样返回（连对象都不重建）、没有 warning；
  4. ``test_other_vp_records_do_not_affect`` —— 别的 VP 的记录不算在这个
     VP 头上。

外加三条边界条件：别的**轮次**的记录不算进这一轮；账本**读不出来** →
原样返回（账本坏了不等于 VP 越权 —— 拿一个读不出来的账本去否证一个 VP，
比放过它更坏），只留一条 warning（措辞是「路径未记录」）；账本记了有这么
一笔却**没记路径** → 仍然是 warning，仍然不改判（措辞保留「路径未记录」）。

断言方式
--------
走**生产入口** ``Orchestrator._delegate_to_sub_agent``：子 agent 被打桩成
"总是回 PASSED"，mutation 记录由树护栏自己写（``record_mutation`` ——
账本的格式是写入方与读回方之间的契约，自己手写的行只能证明读得懂自己
写的东西）。断言落在"生产路径真的没有把 PASSED 改判、而是在 evidence 里
留下了痕迹"这件事上。

全部跑在 ``tmp_path`` 下的临时 git 仓库里 —— 被测的仓库一个字节都不碰。
"""

from __future__ import annotations

import asyncio
import json
import logging
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

#: The event the mutation is recorded under.
MUTATION_EVENT = "verifier_mutated_tree"

#: The logger the warning goes to.
LOGGER_NAME = "verification_agent"


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


def _record_without_paths(
    plan_dir: Path, round_no: int, vp_id: str,
) -> Path:
    """Write a ledger line that records a mutation but no paths.

    The production writer refuses to record a mutation without paths (the
    ledger only holds real overruns), so this line is written by hand: it
    is the "the ledger says something happened, but not what" case the
    verdict layer has to survive.
    """
    ledger = guard.ledger_path(plan_dir, round_no, vp_id)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "vp_id": vp_id,
        "round": round_no,
        "created": [],
        "modified": [],
        "deleted": [],
        "patch": None,
        "command": "echo x >> tracked.txt",
        "tool_use_id": "tu_2",
    }
    ledger.write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    return ledger


def _mutation_events(events: List[tuple]) -> List[tuple]:
    """The mutation events out of the recorded ones."""
    return [event for event in events if event[1] == MUTATION_EVENT]


def _warnings(caplog) -> List[str]:
    """The warning lines this path logged."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME
        and record.levelno == logging.WARNING
    ]


def _delegate(orchestrator: Orchestrator, vp_id: str) -> Verdict:
    """Run the production entry point for one VP."""
    return asyncio.run(
        orchestrator._delegate_to_sub_agent({"id": vp_id}, "code_review", vp_id),
    )


# ---------------------------------------------------------------------------
# 1. 有 mutation 记录 → 记 warning，status 不变
# ---------------------------------------------------------------------------
def test_mutation_evidence_warns_without_rewriting_the_vp(
    monkeypatch, tmp_path, caplog,
):
    """VP 改过被验证树 → 记一条 WARNING，但 verdict 仍然是 PASSED。

    2026-11-07（用户裁定）：**回滚已经发生了**，就没有必要再因为「改了
    树」去失败这个 VP、更不能因此停掉迭代。
    """
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)
    repo = _init_repo(tmp_path / "project")
    _record_mutation(orchestrator.plan_dir, 1, "VP-055", repo)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        verdict = _delegate(orchestrator, "VP-055")

    # the verdict is still the sub-agent's
    assert verdict.verdict == "PASSED", verdict
    assert verdict.failure_class == "", verdict
    assert verdict.failure_class_source != "infra", verdict

    # and it says nothing about failing
    assert not any(
        str(reason).startswith("verifier_mutated_tree:")
        for reason in verdict.reasons
    ), verdict.reasons
    assert verdict.reasons == list(original.reasons), verdict.reasons

    # the fact is still on the record — in the evidence
    joined = " ".join(verdict.evidence)
    assert "new.txt" in joined, verdict.evidence
    assert "tracked.txt" in joined, verdict.evidence

    # and it was logged as a warning
    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    warning = warnings[0]
    assert "VP-055" in warning, warning
    assert "new.txt" in warning, warning
    assert "tracked.txt" in warning, warning

    # the routing facts are still the facts of this attempt
    assert verdict.provider == original.provider, verdict
    assert verdict.chosen_model == original.chosen_model, verdict
    assert verdict.model_complexity == original.model_complexity, verdict


# ---------------------------------------------------------------------------
# 2. 事件带全字段 + severity
# ---------------------------------------------------------------------------
def test_event_carries_all_fields(monkeypatch, tmp_path):
    """事件 ``verifier_mutated_tree`` 带 created/modified/deleted/patch/
    tool_use_id/command —— 运营拿事件就能对上账本那一行。

    事件照旧写（运营靠它对账本），但它**不再是否决**：``severity`` 字段
    说清这一点。
    """
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

    # the event is a warning, not a veto
    assert data["severity"] == "warning", data

    # the patch is where the evidence was stored — it has to be a real file
    assert data["patch"], data
    assert Path(str(data["patch"])).exists(), data


# ---------------------------------------------------------------------------
# 3. 没有记录 → 原 verdict 原样返回
# ---------------------------------------------------------------------------
def test_no_record_leaves_verdict_untouched(monkeypatch, tmp_path, caplog):
    """没有 mutation 记录 → verdict 不动，也不记 warning、不记事件。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "PASSED", verdict
    assert verdict is original, "没有 mutation 证据时原样返回，不重建"
    assert verdict.failure_class == "", verdict
    assert _mutation_events(events) == [], events
    assert _warnings(caplog) == [], caplog.records


# ---------------------------------------------------------------------------
# 4. 别的 VP 的记录不算在这个 VP 头上
# ---------------------------------------------------------------------------
def test_other_vp_records_do_not_affect(monkeypatch, tmp_path, caplog):
    """别的 VP 改过树 → 这个 VP 的 verdict 不受影响，也没有 warning。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)
    repo = _init_repo(tmp_path / "project")
    _record_mutation(orchestrator.plan_dir, 1, "VP-999", repo)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "PASSED", verdict
    assert verdict is original, verdict
    assert _mutation_events(events) == [], events
    assert _warnings(caplog) == [], caplog.records


# ---------------------------------------------------------------------------
# 4b. 一个 VP 有记录、另一个没有 → 只有有记录的那个收到 warning
# ---------------------------------------------------------------------------
def test_only_the_mutated_vp_gets_the_warning(monkeypatch, tmp_path, caplog):
    """两个 VP 走同一轮：动了树的那个收到 warning，另一个原样。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)
    repo = _init_repo(tmp_path / "project")
    _record_mutation(orchestrator.plan_dir, 1, "VP-055", repo)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        mutated = _delegate(orchestrator, "VP-055")
        untouched = _delegate(orchestrator, "VP-056")

    assert mutated.verdict == "PASSED", mutated
    assert untouched.verdict == "PASSED", untouched
    assert untouched is original, untouched

    recorded = _mutation_events(events)
    assert [event[0] for event in recorded] == ["VP-055"], recorded

    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "VP-055" in warnings[0], warnings[0]
    assert "VP-056" not in warnings[0], warnings[0]


# ---------------------------------------------------------------------------
# 边界条件：别的轮次不算进这一轮
# ---------------------------------------------------------------------------
def test_another_round_is_ignored(monkeypatch, tmp_path, caplog):
    """上一轮改过树 → 这一轮不受影响。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=2)
    repo = _init_repo(tmp_path / "project")
    _record_mutation(orchestrator.plan_dir, 1, "VP-055", repo)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "PASSED", verdict
    assert verdict is original, verdict
    assert _mutation_events(events) == [], events
    assert _warnings(caplog) == [], caplog.records


# ---------------------------------------------------------------------------
# 边界条件：账本读不出来 → 原样返回
# ---------------------------------------------------------------------------
def test_unreadable_ledger_leaves_verdict_untouched(monkeypatch, tmp_path, caplog):
    """账本读不出来 → verdict 不动（账本坏了不等于 VP 越权）。

    它仍然只是一条 warning，措辞是「路径未记录」—— 它更不能变成一次改判。
    """
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)

    def _boom(*args, **kwargs):
        raise OSError("ledger is on fire")

    monkeypatch.setattr(verification_agent, "read_mutations", _boom)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "PASSED", verdict
    assert verdict is original, verdict
    assert _mutation_events(events) == [], events

    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "VP-055" in warnings[0], warnings[0]
    assert "路径未记录" in warnings[0], warnings[0]


# ---------------------------------------------------------------------------
# 边界条件：账本记了但没记路径 → 仍然是 warning，仍然不改判
# ---------------------------------------------------------------------------
def test_record_without_paths_still_only_warns(monkeypatch, tmp_path, caplog):
    """账本记了有这么一笔却没记路径 → 措辞保留「路径未记录」，仍不改判。"""
    orchestrator, original, events = _orchestrator(monkeypatch, tmp_path, round_no=1)
    _record_without_paths(orchestrator.plan_dir, 1, "VP-055")

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        verdict = _delegate(orchestrator, "VP-055")

    assert verdict.verdict == "PASSED", verdict
    assert verdict.failure_class == "", verdict
    assert not any(
        str(reason).startswith("verifier_mutated_tree:")
        for reason in verdict.reasons
    ), verdict.reasons

    joined = " ".join(verdict.evidence)
    assert "路径未记录" in joined, verdict.evidence

    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "VP-055" in warnings[0], warnings[0]

    recorded = _mutation_events(events)
    assert len(recorded) == 1, events
    assert recorded[0][2]["severity"] == "warning", recorded

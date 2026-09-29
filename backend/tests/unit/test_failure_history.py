"""2026-09-14 跨轮失败历史 —— 修复 prompt 的"上次方案无效"反馈。

问题：把上一次失败的原因放回 prompt 再重试，这条链路原本是不通的。

断点在于：历史原本只活在 orchestrator 实例内存里，而自动循环的
修复→执行→再验证链每轮都新建 orchestrator（`server.py`
`_on_repair_complete`），所以 `previous_failure_feedback` 恒为空 ——
那段"上次修复尝试未生效"的提示从未真正进入修复 prompt。

本文件钉住修复后的行为：历史落盘 + 跨实例读回 + 既有计划用 verdicts
播种 + prompt 真正渲染出该区块。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from verification import failure_history as fh
from verification.orchestrator import VerificationOrchestrator


# ---------------------------------------------------------------------------
# module-level behaviour
# ---------------------------------------------------------------------------

def test_load_missing_returns_empty(tmp_path):
    assert fh.load(tmp_path) == {}


def test_load_corrupt_returns_empty(tmp_path):
    (tmp_path / fh.HISTORY_FILENAME).write_text("{ not json", encoding="utf-8")
    assert fh.load(tmp_path) == {}


def test_save_load_roundtrip(tmp_path):
    history = {"VP-023": [{"round": 2, "actual_result": "124 failed",
                           "evidence": "pytest tail"}]}
    fh.save(tmp_path, history)
    assert fh.load(tmp_path) == history


def test_save_is_atomic_and_creates_dir(tmp_path):
    target = tmp_path / "plan-x"
    fh.save(target, {"VP-1": [{"round": 1, "actual_result": "x",
                               "evidence": ""}]})
    assert (target / fh.HISTORY_FILENAME).exists()
    # No temp files left behind.
    assert not [p for p in target.iterdir() if p.name.startswith(".failure_history")]


def test_merge_round_is_idempotent(tmp_path):
    history: Dict[str, List[Dict[str, Any]]] = {}
    failed = {"VP-023": {"actual_result": "boom", "evidence": "log"}}
    fh.merge_round(history, failed, 3)
    fh.merge_round(history, failed, 3)   # same round again → no duplicate
    assert len(history["VP-023"]) == 1
    fh.merge_round(history, failed, 4)   # new round → appended
    assert [e["round"] for e in history["VP-023"]] == [3, 4]


def test_merge_round_lets_a_rerun_replace_that_rounds_verdict():
    """同一个 ``(vp_id, round)`` 的第二次写入要**覆盖**，不是被丢掉。

    2026-09-19：reset 把轮次计数器打回 0，于是新一批的第 1 轮和上一批的
    第 1 轮撞在同一个 key 上。旧行为是 ``continue`` —— 新一批真正的失败
    理由被静默丢弃，修复 prompt 里留下的还是上一批的理由。一次实测中：VP-004 / VP-013 的条目逐字节没变，而它们引用的是 2026-09-18
    就退休的 ``cross-verify`` / ``test_command`` 话术 —— 当前代码根本产
    不出那种句子，只可能是上一批的化石。

    幂等性没有丢：一轮仍然只有一条。
    """
    history: Dict[str, List[Dict[str, Any]]] = {}
    fh.merge_round(history, {"VP-004": {
        "actual_result": "上一批的原因", "evidence": "old"}}, 1)
    fh.merge_round(history, {"VP-004": {
        "actual_result": "这一批的原因", "evidence": "new"}}, 1)

    assert len(history["VP-004"]) == 1, "一轮一条，不能出现重复条目"
    entry = history["VP-004"][0]
    assert entry["actual_result"] == "这一批的原因"
    assert entry["evidence"] == "new"


def test_merge_round_carries_repair_outcome_fields():
    history: Dict[str, List[Dict[str, Any]]] = {}
    fh.merge_round(history, {"VP-023": {
        "actual_result": "still failing",
        "evidence": "",
        "repair_task_id": "repair-r3-01",
        "repair_status": "failed",
        "repair_failure_reason": "pytest exit 1",
    }}, 3)
    entry = history["VP-023"][0]
    assert entry["repair_task_id"] == "repair-r3-01"
    assert entry["repair_status"] == "failed"


def test_prune_drops_passed_vps():
    history = {
        "VP-006": [{"round": 1, "actual_result": "a", "evidence": ""}],
        "VP-023": [{"round": 1, "actual_result": "b", "evidence": ""}],
    }
    fh.prune(history, {"VP-023"})
    assert list(history) == ["VP-023"]


def test_save_caps_entries_per_vp(tmp_path):
    history = {"VP-1": [
        {"round": i, "actual_result": f"r{i}", "evidence": ""}
        for i in range(10)
    ]}
    fh.save(tmp_path, history)
    stored = fh.load(tmp_path)["VP-1"]
    assert len(stored) == fh.MAX_ENTRIES_PER_VP
    assert stored[-1]["round"] == 9, "the newest entries must survive the cap"


def test_seed_from_verdicts_groups_and_labels():
    verdicts = [
        {"vp_id": "VP-006", "status": "PASSED", "reasons": "ok"},
        {"vp_id": "VP-023", "status": "FAILED", "reasons": ["a"], "evidence": "e1"},
        {"vp_id": "VP-023", "status": "BLOCKED", "reasons": ["binary stale"]},
        {"vp_id": "VP-006", "status": "FAILED", "reasons": ["函数体 diff 非空"]},
    ]
    seeded = fh.seed_from_verdicts(verdicts)
    assert set(seeded) == {"VP-023", "VP-006"}
    assert "PASSED" not in json.dumps(seeded), "successes are noise here"
    vp23 = seeded["VP-023"]
    assert [e["label"] for e in vp23] == ["既往失败 #1", "既往失败 #2"]
    assert vp23[0]["round"] is None, "verdict rows carry no round — never invent one"
    assert "a" in vp23[0]["actual_result"]


def test_seed_from_verdicts_handles_junk():
    assert fh.seed_from_verdicts(None) == {}
    assert fh.seed_from_verdicts(["nope", 3]) == {}


# ---------------------------------------------------------------------------
# orchestrator integration
# ---------------------------------------------------------------------------

class _Repo:
    def __init__(self, verdicts=None):
        self._verdicts = verdicts or []

    def summary(self, plan_id):
        return {"verdicts": self._verdicts}


def _orch(tmp_path, repo=None):
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir(exist_ok=True)
    return VerificationOrchestrator(
        plan_dir, tmp_path / "proj", coding_tool=object(), verif_repo=repo,
    )


def test_orchestrator_loads_persisted_history(tmp_path):
    plan_dir = tmp_path / "plan-x"
    plan_dir.mkdir()
    fh.save(plan_dir, {"VP-023": [
        {"round": 2, "actual_result": "124 failed", "evidence": "tail"}
    ]})
    orch = _orch(tmp_path)
    assert orch._failure_history["VP-023"][0]["round"] == 2


def test_orchestrator_seeds_from_verdicts_when_no_file(tmp_path):
    """A plan that was already mid-flight when this feature shipped still
    gets feedback on its very next repair generation."""
    repo = _Repo([
        {"vp_id": "VP-023", "status": "FAILED", "reasons": ["max retries exhausted"]},
    ])
    orch = _orch(tmp_path, repo=repo)
    assert "VP-023" in orch._failure_history
    assert orch._failure_history["VP-023"][0]["label"] == "既往失败 #1"


def test_orchestrator_without_repo_or_file_is_empty(tmp_path):
    assert _orch(tmp_path)._failure_history == {}


# ---------------------------------------------------------------------------
# the actual payoff: the repair prompt renders the feedback block
# ---------------------------------------------------------------------------

def test_repair_prompt_renders_previous_failure_feedback():
    from repair_generator import _build_repair_contents_prompt

    prompt = _build_repair_contents_prompt(
        failed_vps=[{"id": "VP-023", "title": "Nightly CI", "priority": "high"}],
        round_number=4,
        plan_dir=Path("/tmp/plan"),
        project_dir=Path("/tmp/proj"),
        previous_failure_feedback={
            "VP-023": [
                {"round": 3, "actual_result": "124 failed in 1338s",
                 "evidence": "pytest tail"},
                {"round": None, "label": "既往失败 #1",
                 "actual_result": "max retries exhausted", "evidence": ""},
            ],
        },
    )
    assert "上次修复尝试未生效" in prompt
    assert "不要重复相同的方案" in prompt
    assert "Round 3" in prompt
    assert "既往失败 #1" in prompt, "seeded entries must render their ordinal label"
    assert "124 failed" in prompt

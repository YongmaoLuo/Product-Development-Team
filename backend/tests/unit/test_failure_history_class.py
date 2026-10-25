"""失败历史里的失败性质 —— 分诊要区分的是"性质漂移"，不是"又失败了"。

背景
====

``verification/failure_history.py`` 记录的是**事实**：哪个 VP、哪一轮、
实际结果是什么、证据是什么。它回答的是"又失败了"，不回答"怎么失败的"。

分诊要的恰恰是后者。同一个 VP 连着三轮失败，可能是第三轮才第一次变成
``criterion_defect``（判据本身有问题，repair 再修一万次也没用），也可能
一直是 ``deliverable_defect``（产物确实错了，repair 值得再试）。这两种
情况在旧的历史记录里长得一模一样 —— 都是"VP-007 第 4 轮失败"。分诊因此
读不出性质漂移，只能按"又失败了"处理。

本文件钉住的行为：

* 每条失败记录携带 ``failure_class`` 与 ``failure_class_source``；
* 缺字段 → 写 ``deliverable_defect`` 与 ``defaulted``（保守的那一档：
  它计数、它会被重做、它是人该看到的那一档）；
* 旧的历史文件（没有这两个键）读回来不崩，且按 ``deliverable_defect``
  解读 —— 老记录不许被当成"不是失败"。

不改变既有键（``round`` / ``actual_result`` / ``evidence`` / ``repair_*``）
与截断规则（``MAX_ENTRIES_PER_VP`` / ``MAX_HISTORY_TEXT_CHARS``）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest


# Ensure backend/ is on sys.path so ``import verification`` works
# regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _fh():
    """The module under test.

    Imported inside the test bodies on purpose: a module that does not
    exist (or does not export the contract) then shows up as a test
    FAILURE carrying the real reason, instead of a collection ERROR
    that hides which contract broke.
    """
    try:
        from verification import failure_history
    except ImportError as exc:  # pragma: no cover — the RED phase
        pytest.fail(f"verification.failure_history is not importable: {exc}")
    return failure_history


# ---------------------------------------------------------------------------
# the contract
# ---------------------------------------------------------------------------

def test_records_failure_class():
    """写入并读回：分类与来源跟着这一轮失败落盘，读回来还在。

    The list form is the shape the verdict records arrive in
    (``{"id": ..., "failure_class": ..., "failure_class_source": ...}``).
    """
    fh = _fh()
    history: Dict[str, List[Dict[str, Any]]] = {}
    fh.merge_round(
        history,
        round_number=4,
        failures=[{
            "id": "VP-007",
            "failure_class": "criterion_defect",
            "failure_class_source": "llm",
        }],
    )

    assert history["VP-007"][-1]["failure_class"] == "criterion_defect"
    assert history["VP-007"][-1]["failure_class_source"] == "llm"

    # 读回：落盘再读出来，两个键都还在。
    with tempfile.TemporaryDirectory() as tmp:
        plan_dir = Path(tmp)
        fh.save(plan_dir, history)
        reloaded = fh.load(plan_dir)
    assert reloaded["VP-007"][-1]["failure_class"] == "criterion_defect"
    assert reloaded["VP-007"][-1]["failure_class_source"] == "llm"


def test_legacy_record_defaults():
    """旧记录按 deliverable_defect 读：缺字段的老历史不崩，也不被当成"不是失败"。

    A history file written before the class existed carries no
    ``failure_class`` / ``failure_class_source`` at all. Reading it must
    not raise, and the record must read as ``deliverable_defect`` — the
    class that counts, so a legacy history is triaged as conservatively
    as a fresh one.
    """
    fh = _fh()
    with tempfile.TemporaryDirectory() as tmp:
        plan_dir = Path(tmp)
        # A legacy file, written by hand: no class, no source.
        (plan_dir / fh.HISTORY_FILENAME).write_text(
            json.dumps({
                "version": 1,
                "plan_id": plan_dir.name,
                "vp_history": {
                    "VP-007": [{
                        "round": 2,
                        "actual_result": "124 failed",
                        "evidence": "pytest tail",
                    }],
                },
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        history = fh.load(plan_dir)      # must not raise
        entry = history["VP-007"][-1]

    assert "failure_class" not in entry
    assert "failure_class_source" not in entry
    assert fh.failure_class_of(entry) == "deliverable_defect"
    assert fh.failure_class_source_of(entry) == "defaulted"


# ---------------------------------------------------------------------------
# 缺字段 → 保守的那一档
# ---------------------------------------------------------------------------

def test_missing_class_defaults_to_deliverable_defect():
    """调用方没给分类 → 写 deliverable_defect / defaulted。"""
    fh = _fh()
    history: Dict[str, List[Dict[str, Any]]] = {}
    fh.merge_round(
        history,
        round_number=1,
        failures=[{"id": "VP-001"}],
    )
    entry = history["VP-001"][-1]
    assert entry["failure_class"] == "deliverable_defect"
    assert entry["failure_class_source"] == "defaulted"


def test_missing_class_in_dict_form_defaults_too():
    """dict 形式（orchestrator 一直用的那个）同样吃默认值。"""
    fh = _fh()
    history: Dict[str, List[Dict[str, Any]]] = {}
    fh.merge_round(
        history,
        {"VP-002": {"actual_result": "boom", "evidence": "log"}},
        2,
    )
    entry = history["VP-002"][-1]
    assert entry["failure_class"] == "deliverable_defect"
    assert entry["failure_class_source"] == "defaulted"


def test_unknown_class_reads_as_deliverable_defect():
    """词汇表之外的分类不留在历史里 —— 分诊只认那四类。"""
    fh = _fh()
    history: Dict[str, List[Dict[str, Any]]] = {}
    fh.merge_round(
        history,
        round_number=1,
        failures=[{"id": "VP-003", "failure_class": "cosmic_rays"}],
    )
    assert history["VP-003"][-1]["failure_class"] == "deliverable_defect"


# ---------------------------------------------------------------------------
# 不改变既有键与截断规则
# ---------------------------------------------------------------------------

def test_existing_keys_and_truncation_are_unchanged():
    """既有键一个不少，截断规则一条没动。"""
    fh = _fh()
    history: Dict[str, List[Dict[str, Any]]] = {}
    fh.merge_round(
        history,
        {"VP-023": {
            "actual_result": "boom",
            "evidence": "log",
            "repair_task_id": "repair-r3-01",
            "repair_test_command": "pytest tests/ -v",
            "repair_status": "failed",
            "repair_failure_reason": "still red",
        }},
        3,
    )
    entry = history["VP-023"][-1]
    assert entry["round"] == 3
    assert entry["actual_result"] == "boom"
    assert entry["evidence"] == "log"
    assert entry["repair_task_id"] == "repair-r3-01"
    assert entry["repair_test_command"] == "pytest tests/ -v"
    assert entry["repair_status"] == "failed"
    assert entry["repair_failure_reason"] == "still red"

    # 每 VP 最多留 MAX_ENTRIES_PER_VP 条 —— 加了分类也不变。
    for round_number in range(1, fh.MAX_ENTRIES_PER_VP + 3):
        fh.merge_round(
            history,
            {"VP-023": {"actual_result": "x", "evidence": "y"}},
            round_number,
        )
    with tempfile.TemporaryDirectory() as tmp:
        plan_dir = Path(tmp)
        fh.save(plan_dir, history)
        stored = fh.load(plan_dir)["VP-023"]
    assert len(stored) == fh.MAX_ENTRIES_PER_VP


def test_same_round_replaces_the_entry_and_keeps_the_class():
    """同轮重跑覆盖旧记录：分类也跟着换成新的那一版。"""
    fh = _fh()
    history: Dict[str, List[Dict[str, Any]]] = {}
    fh.merge_round(
        history,
        round_number=2,
        failures=[{
            "id": "VP-004",
            "failure_class": "deliverable_defect",
            "failure_class_source": "llm",
        }],
    )
    fh.merge_round(
        history,
        round_number=2,
        failures=[{
            "id": "VP-004",
            "failure_class": "criterion_defect",
            "failure_class_source": "llm",
        }],
    )
    assert len(history["VP-004"]) == 1
    assert history["VP-004"][-1]["failure_class"] == "criterion_defect"


# ---------------------------------------------------------------------------
# 字面量不许漂移
# ---------------------------------------------------------------------------

def test_default_literals_match_the_triage_vocabulary():
    """历史里的默认值必须和分诊模块说的是同一件事。"""
    fh = _fh()
    from verification import failure_triage

    assert fh.DEFAULT_FAILURE_CLASS == failure_triage.DELIVERABLE_DEFECT
    assert fh.DEFAULT_FAILURE_CLASS_SOURCE == failure_triage.SOURCE_DEFAULTED
    assert fh.DEFAULT_FAILURE_CLASS == "deliverable_defect"
    assert fh.DEFAULT_FAILURE_CLASS_SOURCE == "defaulted"

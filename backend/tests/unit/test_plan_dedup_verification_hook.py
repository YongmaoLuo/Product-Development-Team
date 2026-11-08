"""验证计划落盘前的去重检测钩子（2026-11-09, R3）。

R3 要求：验证计划**生成后、落盘前**跑一次重复检测 —— 有重复组就把报告写到
验证计划旁边，并打一条 WARNING；提供默认开启的开关；**不改变**已有验证点的
编号与内容（检测是只读的）。本文件钉住钩子的五道缝：

* **计划照旧落盘** —— 计划文件的字节与改动前**逐字节一致**（钩子不能改
  计划的内容、编号或格式）。
* **报告落在计划旁边** —— ``verification_plan_dedup.json`` 与计划同目录，
  里面能看到重复组 ``["VP-022", "VP-025"]``。
* **检测只读** —— 调用前后 ``plan_data`` 深比较不变（R3：「检测是只读的，
  是否删除由人或后续任务决定」）。
* **开关关闭时行为与今天完全一致** —— 只写计划文件，目录里**不出现任何
  报告文件**，且计划字节与开启时相同。
* **目录不存在就建出来** —— 钩子自己负责 ``mkdir(parents=True)``。
* **报告写盘异常不吞掉** —— 写盘失败要让调用方看见，不许悄悄吞掉。

风格说明：``sys.path`` 先补上 backend 目录再 import（与本目录其它用例同一套
做法，见 ``test_api_test_plan_gate.py``）。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


import verification_agent  # noqa: E402
from verification_agent import VerificationAgent  # noqa: E402


# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------

#: VP-022 与 VP-025 跑的是同一条整仓门禁命令 —— 判重成立。
VP_022 = {
    "id": "VP-022",
    "title": "【全量】Nightly CI",
    "ci_entry": "bash scripts/ci_gate.sh",
    "expected_result": "Nightly CI 整仓门禁退出码 0",
}

VP_025 = {
    "id": "VP-025",
    "title": "【云端】整仓门禁",
    "ci_entry": "bash scripts/ci_gate.sh",
    "expected_result": "云端整仓门禁退出码 0",
}


def _agent(plan_file: Path) -> "VerificationAgent":
    """造一个只带 ``verification_plan_file`` 的 agent。

    钩子只碰 ``verification_plan_file``（计划目录从它推出来），因此不需要
    走 ``__init__`` —— ``__init__`` 要 plan_dir / project_dir，而钩子一个
    都不用。
    """
    agent = object.__new__(VerificationAgent)
    agent.verification_plan_file = plan_file
    return agent


def _plan() -> dict:
    return {"verification_points": [copy.deepcopy(VP_022), copy.deepcopy(VP_025)]}


def _plan_bytes(plan_data: dict) -> bytes:
    """计划文件应有的字节 —— 与改动前的写法完全一致。"""
    return json.dumps(plan_data, indent=2, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# 计划落盘 + 报告落盘
# ---------------------------------------------------------------------------


def test_persist_writes_plan_and_report(tmp_path):
    """计划照旧落盘（逐字节一致），报告落在计划旁边。"""
    agent = _agent(tmp_path / "verification_plan.json")
    plan_data = _plan()

    agent._persist_verification_plan(plan_data)

    plan_path = tmp_path / "verification_plan.json"
    assert plan_path.exists()
    assert plan_path.read_bytes() == _plan_bytes(plan_data)

    report_path = tmp_path / "verification_plan_dedup.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    groups = [group["member_ids"] for group in report["duplicate_groups"]]
    assert ["VP-022", "VP-025"] in groups


def test_persist_does_not_mutate_plan(tmp_path):
    """检测是只读的：调用前后 ``plan_data`` 深比较不变。"""
    agent = _agent(tmp_path / "verification_plan.json")
    plan_data = _plan()
    before = copy.deepcopy(plan_data)

    agent._persist_verification_plan(plan_data)

    assert plan_data == before


def test_disabled_switch_writes_plan_only(tmp_path, monkeypatch):
    """开关关闭：只写计划文件，目录里不出现任何报告文件。"""
    monkeypatch.setenv("PDT_PLAN_DEDUP", "0")
    agent = _agent(tmp_path / "verification_plan.json")
    plan_data = _plan()

    agent._persist_verification_plan(plan_data)

    assert (tmp_path / "verification_plan.json").exists()
    assert not (tmp_path / "verification_plan_dedup.json").exists()
    assert not (tmp_path / "verification_plan_dedup.txt").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["verification_plan.json"]


# ---------------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------------


def test_disabled_switch_preserves_plan_bytes(tmp_path, monkeypatch):
    """开关关闭时，计划字节与开启时**逐字节相同**（PRD 验收第 5 条）。"""
    plan_data = _plan()

    on_dir = tmp_path / "on"
    on_dir.mkdir()
    _agent(on_dir / "verification_plan.json")._persist_verification_plan(plan_data)

    monkeypatch.setenv("PDT_PLAN_DEDUP", "0")
    off_dir = tmp_path / "off"
    off_dir.mkdir()
    _agent(off_dir / "verification_plan.json")._persist_verification_plan(plan_data)

    assert (off_dir / "verification_plan.json").read_bytes() == (
        on_dir / "verification_plan.json"
    ).read_bytes()


def test_persist_creates_missing_plan_directory(tmp_path):
    """计划目录不存在 → 钩子自己建出来（``parents=True``）。"""
    plan_file = tmp_path / "plans" / "20261109" / "verification_plan.json"
    agent = _agent(plan_file)

    agent._persist_verification_plan(_plan())

    assert plan_file.exists()
    assert (plan_file.parent / "verification_plan_dedup.json").exists()


def test_report_write_failure_is_not_swallowed(tmp_path, monkeypatch):
    """报告写盘异常不许吞掉 —— 让调用方看见。"""

    def _boom(plan_data, plan_dir):
        raise OSError("disk full")

    monkeypatch.setattr(
        verification_agent, "write_verification_dedup_report", _boom
    )
    agent = _agent(tmp_path / "verification_plan.json")

    with pytest.raises(OSError, match="disk full"):
        agent._persist_verification_plan(_plan())

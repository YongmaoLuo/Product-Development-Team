"""2026-09-14 修复任务执行结果回灌 prompt。

修复反馈的后半段：上一轮**为该 VP 生成的**那个修复任务，执行后是什么结果。
上一轮验证报告只能说明"这个 VP 又失败了"; 说不清"上轮为它生成的那个
修复任务**执行后**是什么结果"。本文件钉住这条链：

  * ``failure_history.load_repair_outcomes`` 从 state.db ``plan_tasks``
    读修复任务结果 (修复任务不在 tasks.json 里 —— 由执行器的 Phase-2
    reconcile 注入 DAG), 同一 VP 取最新一轮;
  * ``_build_repair_contents_prompt`` 把结果渲染到该 VP 的证据块下方,
    并明确要求换一个思路;
  * ``generate_repair_contents`` 把 ``repair_outcomes`` 透传到 prompt。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from repair_generator import (
    RepairTaskGenerator,
    _build_repair_contents_prompt,
)
from verification import failure_history as fh


# ---------------------------------------------------------------------------
# load_repair_outcomes
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> Path:
    """Minimal state.db with a ``plan_tasks`` table matching the real schema."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE plan_tasks (
            plan_id TEXT, task_id TEXT, status TEXT, end_ts TEXT,
            schedule_ts TEXT, attempt INTEGER, commit_sha TEXT,
            failure_reason TEXT, breakdown_count INTEGER,
            _repo_version INTEGER, title TEXT, description TEXT,
            test_command TEXT, files_to_modify TEXT, depends_on TEXT,
            model_type TEXT, project_dir TEXT, provider TEXT,
            task_group TEXT, execution_group INTEGER, priority TEXT,
            acceptance_criteria TEXT, failed_vp_id TEXT, round INTEGER,
            updated_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return db


def _insert(db: Path, **row):
    conn = sqlite3.connect(db)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO plan_tasks ({cols}) VALUES ({marks})",
        tuple(row.values()),
    )
    conn.commit()
    conn.close()


def test_load_repair_outcomes_reads_latest_round(tmp_path):
    db = _make_db(tmp_path)
    _insert(db, plan_id="plan-x", task_id="repair-r3-01", status="failed",
            title="老修复", test_command="pytest a", failure_reason="still red",
            attempt=2, round=3, failed_vp_id="VP-023")
    _insert(db, plan_id="plan-x", task_id="repair-r4-01", status="completed",
            title="新修复", test_command="pytest b", failure_reason="",
            attempt=1, round=4, failed_vp_id="VP-023")
    _insert(db, plan_id="plan-x", task_id="1-1", status="completed",
            title="普通任务", failure_reason="", attempt=1, round=1,
            failed_vp_id=None)

    outcomes = fh.load_repair_outcomes(db, "plan-x")
    assert set(outcomes) == {"VP-023"}, "only repair tasks (failed_vp_id set)"
    assert outcomes["VP-023"]["repair_task_id"] == "repair-r4-01", (
        "the newest round's repair task wins"
    )
    assert outcomes["VP-023"]["repair_status"] == "completed"
    assert outcomes["VP-023"]["repair_test_command"] == "pytest b"


def test_load_repair_outcomes_scopes_to_plan(tmp_path):
    db = _make_db(tmp_path)
    _insert(db, plan_id="other-plan", task_id="repair-r1-01",
            status="failed", title="t", attempt=1, round=1,
            failed_vp_id="VP-001")
    assert fh.load_repair_outcomes(db, "plan-x") == {}


def test_load_repair_outcomes_missing_db_is_empty(tmp_path):
    assert fh.load_repair_outcomes(tmp_path / "nope.db", "plan-x") == {}


# ---------------------------------------------------------------------------
# prompt rendering
# ---------------------------------------------------------------------------

def test_prompt_renders_repair_outcome_block():
    prompt = _build_repair_contents_prompt(
        failed_vps=[{"id": "VP-023", "title": "Nightly CI", "priority": "high"}],
        round_number=4,
        plan_dir=Path("/tmp/plan"),
        project_dir=Path("/tmp/proj"),
        repair_outcomes={
            "VP-023": {
                "repair_task_id": "repair-r3-01",
                "repair_task_title": "修 VP-023 的 docker 依赖",
                "repair_test_command": "pytest tests/ -v",
                "repair_status": "failed",
                "repair_failure_reason": "pytest exit 1: 124 failed",
                "repair_attempt": 2,
            },
        },
    )
    assert "上轮为该 VP 生成的修复任务" in prompt
    assert "`repair-r3-01`" in prompt
    assert "执行结果: failed" in prompt
    assert "pytest exit 1: 124 failed" in prompt
    assert "执行尝试次数: 2" in prompt
    assert "请给出**不同的**修复思路" in prompt


def test_prompt_omits_block_for_vps_without_outcome():
    prompt = _build_repair_contents_prompt(
        failed_vps=[{"id": "VP-006", "title": "diff", "priority": "high"}],
        round_number=2,
        plan_dir=Path("/tmp/plan"),
        project_dir=Path("/tmp/proj"),
        repair_outcomes={"VP-999": {"repair_task_id": "repair-r1-01"}},
    )
    assert "上轮为该 VP 生成的修复任务" not in prompt


def test_prompt_tolerates_malformed_outcome():
    prompt = _build_repair_contents_prompt(
        failed_vps=[{"id": "VP-006", "title": "diff", "priority": "high"}],
        round_number=2,
        plan_dir=Path("/tmp/plan"),
        project_dir=Path("/tmp/proj"),
        repair_outcomes={"VP-006": "not-a-dict"},
    )
    assert "上轮为该 VP 生成的修复任务" not in prompt


# ---------------------------------------------------------------------------
# pass-through
# ---------------------------------------------------------------------------

def test_generate_repair_contents_forwards_outcomes(tmp_path, monkeypatch):
    """The orchestrator passes ``repair_outcomes``; it must reach the prompt."""
    seen = {}

    class _Tool:
        def query_json(self, prompt, system_instruction, timeout=None):
            seen["prompt"] = prompt
            return {"tasks": [{
                "failed_vp_id": "VP-023",
                "title": "T", "description": "D", "acceptance_criteria": "A",
            }]}

    gen = RepairTaskGenerator(_Tool(), tmp_path, tmp_path / "proj")
    gen.generate_repair_contents(
        [{"id": "VP-023", "title": "Nightly CI"}],
        round_number=3,
        repair_outcomes={"VP-023": {
            "repair_task_id": "repair-r2-01", "repair_status": "failed",
        }},
    )
    assert "repair-r2-01" in seen["prompt"]
    assert "执行结果: failed" in seen["prompt"]

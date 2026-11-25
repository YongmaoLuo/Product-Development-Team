"""TDD spec —— 同一失败指纹修满 2 次之后不再派修复实现任务。

背景
----
VP-004 被修了 8 次、VP-008/VP-009 各 7 次 —— 每一轮失败都换一个修复任务，
同一个失败指纹被反复修，修的不是根因，换的是运气。直到
``same_failure_repeated_after_max_attempts`` 才停。

任务 4 已经把「同一个失败指纹连续几轮」这个数持久化下来
(``orch._same_fingerprint_runs``)，所以现在**看得见**同一个点是不是还在以
同一个指纹失败。本文件钉住这个数的用途：**同一个指纹已经修满 2 次之后，
不再把这个点交给修复模型**，而是把它记成 blocked，交给人决定（修 / 作废）。

这个文件钉住四件事
------------------
1. run == 3（已修 2 次）→ 不进 ``repairable``，进 ``deferred``，并且那条
   记录带 ``blocked`` / ``reason`` / ``same_fingerprint_runs`` /
   ``operator_action``；
2. run == 2（已修 1 次）→ 照旧进 ``repairable``（这条边界不能动：修过一轮
   还没修好是正常的，还能再修一轮）；
3. run 缺失 / 0 → 照旧进 ``repairable``（老行为不能因为新字段缺了就被拦；
   指纹缺失 = 判不了 = 永不拦截）；
4. 只有 ``deliverable_defect``（``RETRY_COUNTED``）被拦 —— infra /
   criterion_defect / out_of_scope 走各自的原路径，不得被追加 ``blocked``。

blocked 记录走既有 ``deferred_vps`` 通道（round result 里本来就有这个键），
所以报告自然带上「同一指纹已修 N 次」，round result 的顶层键集合不变。

测试命令::

    pytest backend/tests/unit/test_same_fingerprint_repair_gate.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


# Ensure backend/ is on sys.path so ``import verification.orchestrator``
# works regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.orchestrator import VerificationOrchestrator  # noqa: E402


#: The point the rounds below fail on.
VP_ID = "VP-1"

#: The failure class that buys a repair task — the only class this gate
#: touches.
DELIVERABLE_DEFECT = "deliverable_defect"

#: The class that buys a question for a human instead of a repair task.
OUT_OF_SCOPE = "out_of_scope"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_plan_dir(tmp_path):
    plan_dir = tmp_path / "plans" / "same-fingerprint-repair-gate"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    project_dir = tmp_path / "projects" / "same-fingerprint-repair-gate"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_coding_tool():
    return Mock()


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file.

    Without this the orchestrator would write the test plan's row into
    the repo's real ``state.db``.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


def _make_orchestrator(plan_dir, project_dir, coding_tool):
    """Build an orchestrator with the model-facing halves mocked out."""
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        return VerificationOrchestrator(plan_dir, project_dir, coding_tool)


def _broken_deliverable():
    """A failed point whose class buys a repair task."""
    return {"id": VP_ID, "title": "the broken deliverable"}


# ---------------------------------------------------------------------------
# 1. The third same-fingerprint failure is not repaired any more
# ---------------------------------------------------------------------------


def test_third_same_fingerprint_failure_is_not_repaired(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """run == 3（已修 2 次）→ 不派修复任务，记成 blocked。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    orch._same_fingerprint_runs = {VP_ID: 3}
    vp = _broken_deliverable()

    repairable, deferred = orch._route_failed_vps([vp], 3)

    assert repairable == [], (
        f"同一指纹修满 2 次之后不得再派修复任务，实际是 {repairable!r}"
    )
    assert len(deferred) == 1, (
        f"被拦下的点必须被记下来（走 deferred_vps 通道），实际是 {deferred!r}"
    )
    record = deferred[0]
    assert record.get("blocked") is True, (
        f"被拦下的记录必须带 blocked，实际是 {record!r}"
    )
    assert "同一指纹已修 2 次" in str(record.get("reason") or ""), (
        f"reason 必须说明已经修了多少次，实际是 {record!r}"
    )
    assert record.get("same_fingerprint_runs") == 3, (
        f"记录必须带着连续计数，实际是 {record!r}"
    )
    assert record.get("operator_action") == "decide_repair_or_void", (
        f"记录必须说清楚人要做什么，实际是 {record!r}"
    )
    assert record.get("id") == VP_ID, (
        f"记录必须说清楚是哪个点，实际是 {record!r}"
    )
    assert record.get("failure_class") == DELIVERABLE_DEFECT, (
        f"记录必须带着失败类别，实际是 {record!r}"
    )
    assert record.get("disposition") == "escalate_counted", (
        f"被拦下的失败按『升级』记账，实际是 {record!r}"
    )


# ---------------------------------------------------------------------------
# 2. The second same-fingerprint failure is still repaired
# ---------------------------------------------------------------------------


def test_second_same_fingerprint_failure_is_still_repaired(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """run == 2（已修 1 次）→ 照旧派修复任务。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    orch._same_fingerprint_runs = {VP_ID: 2}
    vp = _broken_deliverable()

    repairable, deferred = orch._route_failed_vps([vp], 2)

    assert repairable == [vp], (
        f"只修过一轮还得再修一轮，实际是 {repairable!r}"
    )
    assert deferred == [], (
        f"还没到上限的点不得进 deferred，实际是 {deferred!r}"
    )


# ---------------------------------------------------------------------------
# 3. A missing counter falls back to the old behaviour
# ---------------------------------------------------------------------------


def test_missing_run_falls_back_to_old_behaviour(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """run 缺失 / 0 → 照旧派修复任务（判不了就不拦）。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    vp = _broken_deliverable()

    repairable, deferred = orch._route_failed_vps([vp], 1)

    assert repairable == [vp], (
        f"没有连续计数时必须走老路（进 repairable），实际是 {repairable!r}"
    )
    assert deferred == [], (
        f"没有连续计数时不得进 deferred，实际是 {deferred!r}"
    )

    orch._same_fingerprint_runs = {VP_ID: 0}
    repairable, deferred = orch._route_failed_vps([vp], 1)

    assert repairable == [vp], (
        f"连续计数为 0 时必须走老路（进 repairable），实际是 {repairable!r}"
    )
    assert deferred == [], (
        f"连续计数为 0 时不得进 deferred，实际是 {deferred!r}"
    )


# ---------------------------------------------------------------------------
# 4. Only the repair-buying class is blocked
# ---------------------------------------------------------------------------


def test_blocking_only_applies_to_retry_counted(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """out_of_scope 不受这个门影响：它走的是问人，不是修复。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    orch._same_fingerprint_runs = {VP_ID: 3}
    monkeypatch.setattr(orch, "_ask_a_human_about", lambda *a, **k: "")
    vp = {
        "id": VP_ID,
        "title": "the out-of-scope check",
        "failure_class": OUT_OF_SCOPE,
    }

    repairable, deferred = orch._route_failed_vps([vp], 3)

    assert repairable == [], (
        f"范围外的检查本来就不该被修复，实际是 {repairable!r}"
    )
    assert len(deferred) == 1, (
        f"范围外的检查必须被记下来，实际是 {deferred!r}"
    )
    record = deferred[0]
    assert "blocked" not in record, (
        f"只有交付物缺陷才带 blocked，实际是 {record!r}"
    )
    assert record.get("disposition") == "skip_not_counted", (
        f"范围外的检查仍然按『跳过』记账，实际是 {record!r}"
    )
    assert record.get("failure_class") == OUT_OF_SCOPE, (
        f"记录必须带着失败类别，实际是 {record!r}"
    )

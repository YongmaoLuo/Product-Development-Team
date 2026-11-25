"""TDD spec —— 同一失败指纹的连续轮次计数。

背景
----
``_failure_fingerprints`` 只存 ``[上一轮, 本轮]`` 两个值。它回答得了「这一轮跟
上一轮是不是同一个指纹」，回答不了「同一个指纹已经连续几轮了」。

import.json 第 3 条要求的是：**连续 2 次实现修复后仍然是同一指纹**，就早停、
早升级。这个判断需要「连续几轮」这一个数，而每轮循环都会**新建**一个
orchestrator（``server.py`` 的 ``_on_repair_complete``），所以这个数必须能
跨进程存活 —— 写在实例内存里等于每轮从 0 开始，条件永远不成立。

这个文件钉住四件事
------------------
1. ``advance_same_fingerprint_run`` 的纯函数契约：同指纹 +1、异指纹归 1、
   指纹缺失归 0。
2. orchestrator 连调两轮之后，同一个点、同一个指纹 → run == 2。
3. 这个 run 能写进 ``verification_loop_tracking.json``，**新建**一个
   orchestrator 之后还是 2（跨进程存活）。
4. 边界：旧 tracking 文件没有这个键不报错；map 为空时不写这个键（不改旧
   payload 的形状）；点本轮不再失败就从 map 里消失。

测试命令::

    pytest backend/tests/unit/test_same_fingerprint_runs.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


# Ensure backend/ is on sys.path so ``import verification.orchestrator``
# works regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.failure_fingerprint import (  # noqa: E402
    advance_same_fingerprint_run,
)
from verification.orchestrator import (  # noqa: E402
    VerificationOrchestrator,
    _parse_same_fingerprint_runs,
)


#: The tracking file the run counter is persisted in.
TRACKING_FILENAME = "verification_loop_tracking.json"

#: The point the rounds below fail on.
VP_ID = "VP-1"

#: The fingerprint every round below produces — the same one, so the
#: counter advances.
FINGERPRINT = "FP-A"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_plan_dir(tmp_path):
    plan_dir = tmp_path / "plans" / "same-fingerprint-runs"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    project_dir = tmp_path / "projects" / "same-fingerprint-runs"
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


def _stub_fingerprints(orch, monkeypatch, fingerprint=FINGERPRINT,
                       rule="identity-v1"):
    """Make every round report the same point failing the same way."""
    monkeypatch.setattr(
        orch,
        "_round_fingerprint_records",
        lambda report: {VP_ID: {"fingerprint": fingerprint, "rule": rule}},
    )
    return orch


# ---------------------------------------------------------------------------
# 1. The pure function
# ---------------------------------------------------------------------------


def test_same_fingerprint_advances():
    """同一指纹连续出现 → 计数 +1。"""
    assert advance_same_fingerprint_run(2, "A", "A") == 3, (
        f"同一指纹连续第二轮必须是 3，实际是 "
        f"{advance_same_fingerprint_run(2, 'A', 'A')}"
    )


def test_different_fingerprint_resets():
    """指纹变了 → 归 1；指纹缺失 → 归 0（不判）。"""
    assert advance_same_fingerprint_run(2, "A", "B") == 1, (
        f"指纹换了必须重新计数，实际是 "
        f"{advance_same_fingerprint_run(2, 'A', 'B')}"
    )
    assert advance_same_fingerprint_run(2, "A", None) == 0, (
        f"本轮没有指纹就不能判，实际是 "
        f"{advance_same_fingerprint_run(2, 'A', None)}"
    )


def test_the_first_fingerprint_counts_as_one():
    """第一次失败（没有上一轮指纹）→ 1，不是 0。"""
    assert advance_same_fingerprint_run(0, None, "A") == 1, (
        f"第一次出现必须是 1，实际是 "
        f"{advance_same_fingerprint_run(0, None, 'A')}"
    )
    assert advance_same_fingerprint_run(0, "", "A") == 1, (
        f"空串也算没有上一轮指纹，实际是 "
        f"{advance_same_fingerprint_run(0, '', 'A')}"
    )


def test_a_non_positive_previous_run_never_produces_a_non_positive_step():
    """上一轮的计数是 0 / 负数 / None 时，增量也必须是正的。"""
    for previous_run in (0, -3, None):
        advanced = advance_same_fingerprint_run(previous_run, "A", "A")
        assert advanced >= 2, (
            f"上一轮计数是 {previous_run!r} 时，同指纹必须至少到 2，实际是 "
            f"{advanced}"
        )


def test_an_empty_current_fingerprint_never_advances():
    """本轮指纹为空 → 0（永不触发早停）。"""
    assert advance_same_fingerprint_run(5, "A", "") == 0, (
        f"本轮指纹为空必须是 0，实际是 "
        f"{advance_same_fingerprint_run(5, 'A', '')}"
    )


# ---------------------------------------------------------------------------
# 2. The orchestrator records the run
# ---------------------------------------------------------------------------


def test_orchestrator_records_two_consecutive_same_fingerprints(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """连调两轮 → 同一个点、同一个指纹 → run == 2。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    _stub_fingerprints(orch, monkeypatch)

    orch._record_round_fingerprints({}, set())
    orch._record_round_fingerprints({}, set())

    assert orch._same_fingerprint_runs[VP_ID] == 2, (
        f"同一指纹连续两轮必须是 2，实际是 "
        f"{orch._same_fingerprint_runs!r}"
    )


def test_a_changed_fingerprint_resets_the_run(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """指纹变了 → run 归 1。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    _stub_fingerprints(orch, monkeypatch)
    orch._record_round_fingerprints({}, set())
    orch._record_round_fingerprints({}, set())

    monkeypatch.setattr(
        orch,
        "_round_fingerprint_records",
        lambda report: {VP_ID: {"fingerprint": "FP-B", "rule": "identity-v1"}},
    )
    orch._record_round_fingerprints({}, set())

    assert orch._same_fingerprint_runs[VP_ID] == 1, (
        f"指纹换了必须重新计数，实际是 {orch._same_fingerprint_runs!r}"
    )


def test_a_point_that_stops_failing_leaves_the_map(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """本轮不再失败的点从 map 里消失（与 fingerprints 的语义一致）。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    _stub_fingerprints(orch, monkeypatch)
    orch._record_round_fingerprints({}, set())
    orch._record_round_fingerprints({}, set())
    assert orch._same_fingerprint_runs == {VP_ID: 2}, (
        f"两轮同指纹之后 map 必须是 {{{VP_ID!r}: 2}}，实际是 "
        f"{orch._same_fingerprint_runs!r}"
    )

    monkeypatch.setattr(orch, "_round_fingerprint_records", lambda report: {})
    orch._record_round_fingerprints({}, set())

    assert orch._same_fingerprint_runs == {}, (
        f"不再失败的点不得留在 map 里，实际是 "
        f"{orch._same_fingerprint_runs!r}"
    )


def test_a_voided_point_leaves_the_map(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """本轮被作废的点不进 map —— 契约只算**非 void** 的 VP。

    一个作废的点不是交付物的失败：它没有根因，所以既不该有指纹，
    也不该有连续轮次。
    """
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    _stub_fingerprints(orch, monkeypatch)
    orch._record_round_fingerprints({}, set())
    orch._record_round_fingerprints({}, set())
    assert orch._same_fingerprint_runs == {VP_ID: 2}, (
        f"作废之前必须是 {{{VP_ID!r}: 2}}，实际是 "
        f"{orch._same_fingerprint_runs!r}"
    )

    orch._record_round_fingerprints({}, {VP_ID})

    assert orch._same_fingerprint_runs == {}, (
        f"被作废的点不得留在 map 里，实际是 "
        f"{orch._same_fingerprint_runs!r}"
    )


def test_the_return_value_is_still_the_fingerprint_pairs(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """返回值仍然是 pairs —— 新计数不能改掉旧契约。

    ``_record_round_fingerprints`` 的调用方读的是这个返回值；新增连续
    计数如果改了它，老的调用方就跟着坏掉。
    """
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    _stub_fingerprints(orch, monkeypatch)
    orch._record_round_fingerprints({}, set())

    pairs = orch._record_round_fingerprints({}, set())

    assert pairs == {VP_ID: [FINGERPRINT, FINGERPRINT]}, (
        f"返回值必须还是 [上一轮, 本轮] 的指纹对，实际是 {pairs!r}"
    )


# ---------------------------------------------------------------------------
# 3. The run survives a fresh orchestrator
# ---------------------------------------------------------------------------


def test_the_run_survives_a_fresh_orchestrator(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """写盘 → 新建 orchestrator → run 仍然是 2。

    每轮循环都新建 orchestrator，所以这个数不落盘就等于每轮从 0 开始。
    """
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    _stub_fingerprints(orch, monkeypatch)
    orch._record_round_fingerprints({}, set())
    orch._record_round_fingerprints({}, set())
    orch._save_loop_tracking()

    fresh = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )

    assert fresh._same_fingerprint_runs == {VP_ID: 2}, (
        f"新建的 orchestrator 必须接着上一个的连续计数，实际是 "
        f"{fresh._same_fingerprint_runs!r}"
    )


def test_the_tracking_file_carries_the_key(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """落盘的 payload 里必须有 ``same_fingerprint_runs``。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    _stub_fingerprints(orch, monkeypatch)
    orch._record_round_fingerprints({}, set())
    orch._record_round_fingerprints({}, set())
    orch._save_loop_tracking()

    payload = json.loads(
        (temp_plan_dir / TRACKING_FILENAME).read_text(encoding="utf-8")
    )
    assert payload.get("same_fingerprint_runs") == {VP_ID: 2}, (
        f"落盘的 payload 必须带着连续计数，实际是 {payload!r}"
    )


def test_the_streak_grows_across_a_fresh_orchestrator(
    temp_plan_dir, temp_project_dir, mock_coding_tool, monkeypatch,
):
    """跨进程**继续**往上走，不是从头开始。

    这才是 import.json 第 3 条要的那个数：「连续 2 次实现修复后仍然是同一
    指纹」——第三轮必须**看见** 3，只看见 2 是不够的（2 只说明「修了一轮
    没修好」，3 才说明「修了两轮没修好」）。而第三轮跑在一个**新**的
    orchestrator 里，所以这个数必须从盘上接着往上加，不能每轮从 1 重来。
    """
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    _stub_fingerprints(orch, monkeypatch)
    orch._record_round_fingerprints({}, set())
    orch._record_round_fingerprints({}, set())
    orch._save_loop_tracking()
    assert orch._same_fingerprint_runs[VP_ID] == 2, (
        f"同一进程里两轮必须是 2，实际是 {orch._same_fingerprint_runs!r}"
    )

    fresh = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    _stub_fingerprints(fresh, monkeypatch)
    fresh._record_round_fingerprints({}, set())

    assert fresh._same_fingerprint_runs[VP_ID] == 3, (
        f"新的 orchestrator 必须把计数接着往上加，实际是 "
        f"{fresh._same_fingerprint_runs!r}"
    )


def test_an_empty_map_does_not_add_the_key(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """map 为空时不写这个键 —— 旧 payload 的形状不能被改掉。"""
    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )
    orch._save_loop_tracking()

    payload = json.loads(
        (temp_plan_dir / TRACKING_FILENAME).read_text(encoding="utf-8")
    )
    assert "same_fingerprint_runs" not in payload, (
        f"空 map 不得往 payload 里加键，实际是 {payload!r}"
    )


def test_a_legacy_tracking_file_loads_without_the_key(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """旧 tracking 文件没有这个键 → 载入 ``{}``，不报错。"""
    (temp_plan_dir / TRACKING_FILENAME).write_text(
        json.dumps({
            "version": 2,
            "previous_failed_keys": ["VP-1|deliverable_defect"],
            "same_failure_counts": {"VP-1|deliverable_defect": 1},
            "last_round": 3,
        }),
        encoding="utf-8",
    )

    orch = _make_orchestrator(
        temp_plan_dir, temp_project_dir, mock_coding_tool,
    )

    assert orch._same_fingerprint_runs == {}, (
        f"旧 payload 必须读成空 map，实际是 {orch._same_fingerprint_runs!r}"
    )


# ---------------------------------------------------------------------------
# 4. The parser
# ---------------------------------------------------------------------------


def test_the_parser_keeps_only_readable_entries():
    """只收 str 键与非 bool 的非负 int，其余丢弃。"""
    parsed = _parse_same_fingerprint_runs({
        "VP-1": 2,
        "VP-2": True,        # bool 不是 int
        "VP-3": -1,          # 负数
        "VP-4": "3",         # 字符串不是 int
        "VP-5": 0,           # 0 是合法的非负 int
        7: 4,               # 键不是 str
    })

    assert parsed == {"VP-1": 2, "VP-5": 0}, (
        f"只有可读的条目能留下，实际是 {parsed!r}"
    )


def test_the_parser_of_a_missing_payload_is_empty():
    """没有这个键 → 空 map，不报错。"""
    assert _parse_same_fingerprint_runs(None) == {}
    assert _parse_same_fingerprint_runs("not a dict") == {}
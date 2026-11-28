"""TDD spec — backend/framework_fix/notify.py 的八类通知事件与 30 分钟合并提醒。

这个模块要解决什么
------------------
PRD 要求「命中框架缺陷必须通知到人」：八类事件各自有一个确定的触发时机，
合并审批在就绪满 30 分钟仍未批复时重发，夜间 22:00–08:00 静默。此前没有
任何地方表达过这件事 —— 命中框架缺陷不会通知任何人。

本模块把三件事收成一处：

* :data:`EVENTS` —— 八类事件的**闭集**。集合外的词抛 ``ValueError``：拼错的
  事件名在**发出之前**就被挡住，而不是投出去之后没人认得。
* :func:`emit` —— 投递一条事件。静默窗口内**不投递但记录**；投递失败
  **记录错误而不抛出** —— 通知不是主流程，通知失败不能把修复流程带崩。
* :func:`due_reminders` —— 哪些合并租约该发 30 分钟提醒了。纯函数：给定
  时刻与租约列表，答案只有一个。

提醒的三个边界
--------------
* **无有效期不提醒** —— 租约没有 ``merge_ready_at``（「就绪」这个起点不存在），
  就没有「满了 30 分钟」这回事。
* **已提醒过的不重复** —— 同一个租约的 ``merge_reminder_30m`` 只发一次。
* **静默窗口内不投递** —— 但仍然记录：静默是「现在不打扰」，不是「没发生」。

加急合并走 HITL
---------------
``merge_ready`` 不是一条「贴在墙上的消息」，它是一条**等人批准**的问题：
PR 号、CI 结论与「回复 approve 合入」的问句，落在 HITL 的决定表里，用户答了
才算数（PRD 决策点 7：没有用户批准就不合入）。

TDD contract — 4 cases
-----------------------
1. ``test_events_closed_set`` —— 八类事件齐全；集合外的词抛 ``ValueError``。
2. ``test_reminder_due`` —— 就绪满 30 分钟 -> 返回租约的 plan id。
3. ``test_quiet_hours`` —— 静默窗口 -> 不投递（但记录）。
4. ``test_reminder_dedup`` —— 已提醒过 -> 不重复。

同文件覆盖的边界条件：没有 ``merge_ready_at`` -> 不提醒；投递失败 -> 记录错误
而不抛出；就绪事件 -> 落一条等人批准的决定；``emit`` 把它的记录还回来，
调用方据此分辨「送到了 / 被静默吞了 / 出错了」。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from framework_fix.notify import (  # noqa: E402
    EVENTS,
    clear_records,
    due_reminders,
    emit,
    records,
)

#: 事件词表 —— PRD 与架构决策点 6 批准的八类，一类不多一类不少。
EXPECTED_EVENTS = frozenset(
    {
        "discovered",
        "merge_ready",
        "merge_reminder_30m",
        "waiting_on_user_merge",
        "merged",
        "reopened",
        "exhausted",
        "scheduler_deadlock_suspected",
    }
)

#: 一个白天时刻（不在静默窗口内）。
DAY = datetime(2026, 11, 23, 12, 0)

#: 一个夜里时刻（落在 22:00–08:00 的静默窗口内）。
NIGHT = datetime(2026, 11, 23, 23, 30)


# ---------------------------------------------------------------------------
# 夹具与助手
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_records():
    """每个用例前后都清空投递记录 —— 记录是进程内的状态。"""
    clear_records()
    yield
    clear_records()


def _write_plan(plan_dir: Path, out: Path, *, mode: str = "auto") -> Path:
    """写一个带 ``cmd`` 出口的计划目录。

    出口把收到的 body 原样追加到 ``out`` —— 于是「投递了没有」不是一个
    断言，而是磁盘上一个文件在不在。
    """
    plan_dir.mkdir(parents=True, exist_ok=True)
    script = plan_dir / "record.py"
    script.write_text(
        "import sys\n"
        "with open(sys.argv[1], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(sys.stdin.read())\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}" "{out}"'
    (plan_dir / "hitl_config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mode": mode,
                "timeout_sec": 1800,
                "hooks": [{"kind": "cmd", "command": command, "timeout_sec": 10}],
            }
        ),
        encoding="utf-8",
    )
    return plan_dir


def _bodies(out: Path) -> List[Dict[str, Any]]:
    """出口收到的那几份 body。"""
    if not out.exists():
        return []
    return [
        json.loads(line)
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _wait_for_bodies(out: Path, timeout: float = 10.0) -> List[Dict[str, Any]]:
    """等出口把 body 送过来。

    加急合并的决定是在工作线程上投递的（挂起的出口不能占住请求路径），
    所以「送到了没有」要等一下 —— 等不到就是没送到。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        bodies = _bodies(out)
        if bodies:
            return bodies
        time.sleep(0.05)
    return _bodies(out)


# ---------------------------------------------------------------------------
# 1. 事件闭集
# ---------------------------------------------------------------------------
def test_events_closed_set():
    """八类事件齐全；集合外的词在发出之前就被挡住。"""
    assert EVENTS == EXPECTED_EVENTS
    assert isinstance(EVENTS, frozenset)
    assert len(EVENTS) == 8

    with pytest.raises(ValueError):
        emit("not_an_event", {"plan_id": "fx1"})

    with pytest.raises(ValueError):
        emit("framework_fix_discovered", {"plan_id": "fx1"})


# ---------------------------------------------------------------------------
# 2. 提醒到期
# ---------------------------------------------------------------------------
def test_reminder_due():
    """就绪满 30 分钟 -> 该提醒了；没满 -> 不该。"""
    lease = {"plan_id": "fx1", "merge_ready_at": DAY - timedelta(minutes=30)}

    assert due_reminders(DAY, [lease]) == ["fx1"]

    # 还差一分钟 —— 不到阈值就不提醒
    soon = {"plan_id": "fx2", "merge_ready_at": DAY - timedelta(minutes=29)}
    assert due_reminders(DAY, [soon]) == []

    # 阈值是参数，不是写死的 30
    assert due_reminders(DAY, [lease], threshold_minutes=60) == []


def test_reminder_due_multiple_leases():
    """多个租约 —— 只有到期的那些被点名，顺序跟租约一致。"""
    leases = [
        {"plan_id": "fx1", "merge_ready_at": DAY - timedelta(minutes=5)},
        {"plan_id": "fx2", "merge_ready_at": DAY - timedelta(minutes=45)},
        {"plan_id": "fx3", "merge_ready_at": DAY - timedelta(minutes=31)},
    ]

    assert due_reminders(DAY, leases) == ["fx2", "fx3"]


def test_reminder_due_accepts_other_timestamp_spellings():
    """时刻的写法不改变提醒的算法 —— ISO 字符串与 epoch 秒都认。"""
    lease = {
        "plan_id": "fx1",
        "merge_ready_at": (DAY - timedelta(minutes=30)).isoformat(),
    }
    assert due_reminders(DAY, [lease]) == ["fx1"]

    epoch = 1_700_000_000.0
    assert due_reminders(epoch, [{"plan_id": "fx2", "merge_ready_at": epoch - 1800}]) == [
        "fx2"
    ]


def test_reminder_accepts_the_registry_lease_spelling():
    """租约行写的是 ``fix_plan_id`` —— 注册表那一张表就是这么命名的。

    ``framework_fix_merge_lease`` 的主键是 ``fix_plan_id``，不是 ``plan_id``。
    两种写法说的是同一件事，提醒的计算不能只认其中一种 —— 否则租约到了
    该提醒的时候算不出来。
    """
    lease = {
        "fix_plan_id": "fx1",
        "merge_ready_at": DAY - timedelta(minutes=30),
    }
    assert due_reminders(DAY, [lease]) == ["fx1"]


# ---------------------------------------------------------------------------
# 3. 静默窗口
# ---------------------------------------------------------------------------
def test_quiet_hours(tmp_path):
    """静默窗口内不投递但记录；窗口外照常投递。"""
    plan_dir = _write_plan(tmp_path / "plan", tmp_path / "delivered.jsonl")

    emit(
        "discovered",
        {"plan_id": "fx1", "plan_dir": str(plan_dir)},
        now=NIGHT,
    )

    assert not (tmp_path / "delivered.jsonl").exists()

    recorded = records()
    assert len(recorded) == 1
    assert recorded[0]["event"] == "discovered"
    assert recorded[0]["delivered"] is False
    assert recorded[0]["suppressed"] == "quiet_hours"
    assert recorded[0]["error"] is None

    # 窗口外 —— 同样的事件照常投递
    emit(
        "discovered",
        {"plan_id": "fx1", "plan_dir": str(plan_dir)},
        now=DAY,
    )

    bodies = _bodies(tmp_path / "delivered.jsonl")
    assert len(bodies) == 1
    assert bodies[0]["event"] == "framework_fix_discovered"
    assert bodies[0]["plan_id"] == "fx1"


def test_quiet_hours_boundaries():
    """窗口的两端：22:00 起静默，08:00 止静默。"""
    from framework_fix.notify import in_quiet_hours

    assert in_quiet_hours(datetime(2026, 11, 23, 22, 0)) is True
    assert in_quiet_hours(datetime(2026, 11, 23, 7, 59)) is True
    assert in_quiet_hours(datetime(2026, 11, 23, 8, 0)) is False
    assert in_quiet_hours(datetime(2026, 11, 23, 21, 59)) is False


# ---------------------------------------------------------------------------
# 4. 提醒去重
# ---------------------------------------------------------------------------
def test_reminder_dedup():
    """已提醒过的租约不再提醒；没提醒过的照常提醒。"""
    reminded = {
        "plan_id": "fx1",
        "merge_ready_at": DAY - timedelta(minutes=30),
        "notified_events": ["merge_reminder_30m"],
    }
    assert due_reminders(DAY, [reminded]) == []

    marked = {
        "plan_id": "fx2",
        "merge_ready_at": DAY - timedelta(minutes=30),
        "merge_reminder_30m": True,
    }
    assert due_reminders(DAY, [marked]) == []

    fresh = {"plan_id": "fx3", "merge_ready_at": DAY - timedelta(minutes=30)}
    assert due_reminders(DAY, [fresh]) == ["fx3"]


# ---------------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------------
def test_emit_returns_its_record(tmp_path):
    """emit 把记录还回来 —— 调用方要能分辨三种结局。

    「送到了」「被静默窗口吞了」「出错了」是三件不同的事：提醒的标记
    只在真的送到时才写（静默吞掉的要等窗口过去再发），而这一层只有
    返回值能说清是哪一种。
    """
    plan_dir = _write_plan(tmp_path / "plan", tmp_path / "delivered.jsonl")

    record = emit(
        "discovered",
        {"plan_id": "fx1", "plan_dir": str(plan_dir)},
        now=DAY,
    )

    assert isinstance(record, dict), "emit 必须把它的记录还回来"
    assert record["event"] == "discovered"
    assert record["delivered"] is True
    assert record["suppressed"] is None
    assert record["error"] is None

    swallowed = emit(
        "discovered",
        {"plan_id": "fx2", "plan_dir": str(plan_dir)},
        now=NIGHT,
    )
    assert swallowed["delivered"] is False
    assert swallowed["suppressed"] == "quiet_hours"
    assert swallowed["error"] is None


def test_reminder_without_merge_ready_at():
    """没有有效期（没有 ``merge_ready_at``）-> 不提醒。"""
    assert due_reminders(DAY, [{"plan_id": "fx1"}]) == []
    assert due_reminders(DAY, [{"plan_id": "fx1", "merge_ready_at": None}]) == []


def test_reminder_stops_when_the_lease_expired():
    """租约过了有效期 -> 不再提醒（租约没了，提醒给谁？）。"""
    expired = {
        "plan_id": "fx1",
        "merge_ready_at": DAY - timedelta(minutes=30),
        "expires_at": DAY - timedelta(minutes=1),
    }
    assert due_reminders(DAY, [expired]) == []

    still_valid = {
        "plan_id": "fx2",
        "merge_ready_at": DAY - timedelta(minutes=30),
        "expires_at": DAY + timedelta(minutes=30),
    }
    assert due_reminders(DAY, [still_valid]) == ["fx2"]


def test_emit_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    """投递失败 -> 记录错误，不抛 —— 通知不是主流程。"""
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("outlet is down")

    monkeypatch.setattr("hooks.dispatcher.dispatch", _boom)

    emit(
        "discovered",
        {"plan_id": "fx1", "plan_dir": str(tmp_path)},
        now=DAY,
    )

    recorded = records()
    assert len(recorded) == 1
    assert recorded[0]["delivered"] is False
    assert recorded[0]["error"] is not None
    assert "outlet is down" in recorded[0]["error"]


def test_merge_ready_asks_through_hitl(tmp_path, state_db):
    """就绪事件走 HITL —— 落一条等人批准的决定。"""
    from hitl.service import list_decisions

    plan_dir = _write_plan(tmp_path / "plan", tmp_path / "delivered.jsonl")

    emit(
        "merge_ready",
        {
            "plan_id": "fx1",
            "plan_dir": str(plan_dir),
            "pr_url": "https://example.invalid/pr/1",
            "ci_conclusion": "passed",
        },
        now=DAY,
    )

    decisions = list_decisions("fx1")
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.source == "framework_fix"
    assert decision.options == ["approve", "reject"]
    assert "approve" in decision.question
    assert "https://example.invalid/pr/1" in decision.question

    # 问句真的送出去了 —— 出口收到的那一份里带着同一个 PR 号
    bodies = _wait_for_bodies(tmp_path / "delivered.jsonl")
    assert len(bodies) == 1
    assert bodies[0]["event"] == "hitl_decision_created"
    assert bodies[0]["plan_id"] == "fx1"
    assert "https://example.invalid/pr/1" in bodies[0]["question"]


def test_merge_ready_in_quiet_hours_asks_nobody(tmp_path, state_db):
    """静默窗口内的就绪事件 —— 不问人（连决定都不落）。"""
    from hitl.service import list_decisions

    plan_dir = _write_plan(tmp_path / "plan", tmp_path / "delivered.jsonl")

    emit(
        "merge_ready",
        {
            "plan_id": "fx1",
            "plan_dir": str(plan_dir),
            "pr_url": "https://example.invalid/pr/1",
            "ci_conclusion": "passed",
        },
        now=NIGHT,
    )

    assert list_decisions("fx1") == []
    assert records()[0]["suppressed"] == "quiet_hours"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))

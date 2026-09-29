"""搁浅卡片的识别（2026-09-16）。

现象：卡片上任务的"最后更新"停在上一次推送，标题却至今显示"执行中"——
实际早就没在跑了。这是一类同步 bug，而且有确切的复现路径。

成因：修复执行被 server 重启打断 —— shutdown 顺手终止了它的子进程，DB
正常写成 ``failed``，但**没有任何事件去推卡片**，于是卡片永远停在最后
一次推送的那一版「执行中」。

修法的难点在于**不能扫太宽**：早先一版只按"推过卡片"来扫，会把大量早已
结束的历史计划一起捞出来，正是"过期卡片不应该再弹消息"这条要求要避免的；
收紧成三条全中之后，命中面才收得住。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from notifications import plan_dir_resolver as pdr


def _iso(epoch: float) -> str:
    """``plan_routing.updated_at`` 的落盘格式（UTC + Z）。"""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """把 plans 目录与 state.db 都指到 tmp；返回一个造数据的 helper。"""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    db_path = tmp_path / "state.db"

    monkeypatch.setattr(pdr, "_plans_dir", lambda: plans_dir)
    monkeypatch.setattr(pdr, "_state_db", lambda: db_path)

    def _seed(plan_id, stage, updated_epoch, *, last_push_epoch=None,
              message_id="om_x", touch_card=True):
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS plan_routing "
                "(plan_id TEXT PRIMARY KEY, current_phase TEXT, "
                " updated_at TEXT)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO plan_routing "
                "(plan_id, current_phase, updated_at) VALUES (?, ?, ?)",
                (plan_id, stage, _iso(updated_epoch)),
            )
        if touch_card:
            card_dir = plans_dir / plan_id / "notifications"
            card_dir.mkdir(parents=True)
            (card_dir / "feishu_card.json").write_text(
                json.dumps({
                    "plan_id": plan_id,
                    "message_id": message_id,
                    "last_push_ts": last_push_epoch,
                }),
                encoding="utf-8",
            )

    return plans_dir, _seed


NOW = 1_800_000_000.0


def test_terminal_plan_whose_card_predates_the_transition_is_stranded(env) -> None:
    """用户报的那张：卡片推于 1000，计划在 2000 才转终态 —— 中间没人推卡。"""
    _, seed = env
    seed("plan-stranded", "failed",
         updated_epoch=NOW, last_push_epoch=NOW - 3600)

    assert pdr.list_stranded_card_plan_ids() == ["plan-stranded"]


def test_a_normally_finished_plan_is_not_stranded(env) -> None:
    """正常终了的计划：终态转换自己会推一次卡，于是 push 晚于 updated_at。"""
    _, seed = env
    seed("plan-ok", "completed",
         updated_epoch=NOW - 3600, last_push_epoch=NOW)

    assert pdr.list_stranded_card_plan_ids() == []


def test_a_running_plan_is_not_stranded(env) -> None:
    """非终态不归这里管 —— 那是 ``list_active_plan_ids`` 的活。

    这条同时锁住"不能扫太宽"：把非终态也纳进来，就等于在每次重启时给一大
    批历史计划推卡片。
    """
    _, seed = env
    seed("plan-running", "executing",
         updated_epoch=NOW, last_push_epoch=NOW - 3600)

    assert pdr.list_stranded_card_plan_ids() == []


def test_a_card_that_never_pushed_successfully_is_ignored(env) -> None:
    """没有 message_id = 当时没推成。此时再推等于**凭空造一张新卡片**。"""
    _, seed = env
    seed("plan-never-pushed", "failed",
         updated_epoch=NOW, last_push_epoch=NOW - 3600, message_id=None)

    assert pdr.list_stranded_card_plan_ids() == []


def test_telegram_only_card_counts(env) -> None:
    """只推成了 Telegram 也算"我们拥有这张卡"。"""
    plans_dir, seed = env
    seed("plan-tg", "failed",
         updated_epoch=NOW, last_push_epoch=None, message_id=None)
    card = plans_dir / "plan-tg" / "notifications" / "feishu_card.json"
    data = json.loads(card.read_text(encoding="utf-8"))
    data["telegram_message_id"] = 1402
    data["last_push_ts"] = NOW - 3600
    card.write_text(json.dumps(data), encoding="utf-8")

    assert pdr.list_stranded_card_plan_ids() == ["plan-tg"]


def test_a_missing_card_file_is_ignored(env) -> None:
    _, seed = env
    seed("plan-no-card", "failed",
         updated_epoch=NOW, last_push_epoch=None, touch_card=False)

    assert pdr.list_stranded_card_plan_ids() == []


def test_a_card_without_a_push_timestamp_is_ignored(env) -> None:
    """``last_push_ts`` 缺失时无法证明"推于状态变更之前" —— 不猜，放过。"""
    _, seed = env
    seed("plan-no-ts", "failed",
         updated_epoch=NOW, last_push_epoch=None)

    assert pdr.list_stranded_card_plan_ids() == []


def test_unparseable_updated_at_is_ignored(env, monkeypatch) -> None:
    """时间戳解析不了就跳过，不要让整个 sweep 崩掉。"""
    plans_dir, seed = env
    seed("plan-bad-ts", "failed",
         updated_epoch=NOW, last_push_epoch=NOW - 3600)
    import sqlite3
    with sqlite3.connect(pdr._state_db()) as conn:
        conn.execute(
            "UPDATE plan_routing SET updated_at = 'not-a-timestamp' "
            "WHERE plan_id = 'plan-bad-ts'"
        )

    assert pdr.list_stranded_card_plan_ids() == []


def test_missing_plans_dir_or_db_yields_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pdr, "_plans_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(pdr, "_state_db", lambda: tmp_path / "nope.db")

    assert pdr.list_stranded_card_plan_ids() == []


def test_results_are_sorted_for_determinism(env) -> None:
    _, seed = env
    for name in ("plan-c", "plan-a", "plan-b"):
        seed(name, "failed", updated_epoch=NOW,
             last_push_epoch=NOW - 3600)

    assert pdr.list_stranded_card_plan_ids() == [
        "plan-a", "plan-b", "plan-c",
    ]


def test_the_sweep_actually_consumes_the_stranded_set() -> None:
    """结构性回归锁：``start()`` 必须把这个集合并进 sweep。

    这条不是走行为，而是锁住"接线" —— 函数写对了但没接上，等于没修。
    """
    import inspect

    from notifications import feishu_notifier

    src = inspect.getsource(feishu_notifier.FeishuNotifier.start)
    assert "list_stranded_card_plan_ids()" in src
    assert "list_active_plan_ids()" in src, (
        "原 sweep 不能被替换掉，只能是并集"
    )


# ---------------------------------------------------------------------------
# 夹具 plan_id 过滤（2026-09-16）
# ---------------------------------------------------------------------------
#
# 统计出来的 9 张里只有 **2 张是真实计划**，另外 7 张是 pytest 夹具漏写进真实
# ``plans/`` 目录的产物。它们有 plan_state.json、有 tasks.json、还真的推成过
# 飞书卡片 —— 从数据上看和真实计划无法区分，只能按 id 形状认。


def test_fixture_plan_ids_are_not_considered_stranded(env) -> None:
    """不滤的后果：每次重启都给这 7 张死卡片推一条消息。"""
    _, seed = env
    for pid in (
        "vp-003-explicit",
        "vp001-pid-in-payload",
        "resume-test-1789390385741",
        "project_vp016-sync-passed",
    ):
        seed(pid, "completed", updated_epoch=NOW, last_push_epoch=NOW - 3600)

    assert pdr.list_stranded_card_plan_ids() == []


def test_fixture_plan_ids_are_not_swept_while_active(env) -> None:
    """同一条过滤也必须管住原 sweep。

    2026-09-13 那次事故就是它复活的空卡片（见 ``_plans_dir`` 的 docstring：
    "the running server's startup sweep then kept refreshing cards for those
    junk fixture plans"）—— vp-* 的卡片当初就是这么推出去的。
    """
    _, seed = env
    seed("vp-003-explicit", "executing",
         updated_epoch=NOW, last_push_epoch=None, touch_card=False)

    assert pdr.list_active_plan_ids() == []


def test_a_real_plan_whose_name_contains_test_is_kept(env) -> None:
    """锚定前缀 vs 宽子串 —— 这条锁的是**设计选择**。

    scheduler 侧用的是 ``"test" in plan_id or "vp" in plan_id``；实测那套规则
    在本仓 229 个计划里命中 **102** 个。通知器侧的误判代价是"真实卡片永远
    不刷新"，方向比 scheduler 侧危险，所以只认锚定前缀。
    """
    _, seed = env
    seed("20260621-ac-resume-smoke-test", "executing",
         updated_epoch=NOW, last_push_epoch=None, touch_card=False)

    assert pdr.list_active_plan_ids() == ["20260621-ac-resume-smoke-test"]


@pytest.mark.parametrize("plan_id,expected", [
    ("vp-003-explicit", True),
    ("vp001-pid-in-payload", True),
    ("VP-002-PID-PAYLOAD", True),                    # 大小写不敏感
    ("resume-test-1789390385741", True),
    ("test-agent-dispatch-20f8bbd3", True),
    ("test_plan", True),
    ("project_vp016-sync-passed", True),
    # --- 以下都是真实计划形状，必须放过 ---
    ("20260101-production-plan(consolidat", False),
    ("20260101-支付网关重构-架构评审", False),
    ("20260621-resume-smoke-test", False),        # 含 "test" 但不在开头
    ("20260604-test-fast", False),
    ("", False),
    (None, False),
])
def test_fixture_id_shape(plan_id, expected) -> None:
    assert pdr._looks_like_fixture_plan(plan_id) is expected

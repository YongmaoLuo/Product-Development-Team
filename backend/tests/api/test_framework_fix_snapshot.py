"""``GET /api/scheduler/state`` —— 框架修复分区与 ``kind`` 过滤。

这个文件钉住什么
----------------
1. **分区存在，且普通分区不混它。** 一个修复计划（``plan_kind='framework_fix'``）
   在 ``framework_fixes`` 分区里，并且**不在**任何别的分区里：普通队列、运行
   线、停放列表只说普通计划。一个计划只属于一个分区 —— 面板把同一个计划显示
   两次，显示给操作者的是两件不同的事。
2. **``kind`` 查询参数挑分区。** ``kind=plans`` 答普通分区、框架修复分区是
   空列表；``kind=framework_fix`` 答框架修复分区、普通分区是空的；``kind=all``
   两个都答。**未知的词是 422** —— 词表是闭的，闭集外的词是一个答不了的问题，
   不是一个可以悄悄按「all」回答的问题。
3. **每个分区条目都带 ``display_state``** —— 走同一个投影
   （:func:`framework_fix.read_model.project_display_state`），所以面板、通知与
   API 不会对同一个计划说出三个状态。
4. **没有修复计划的调度器答一个空分区**，普通分区一个字都不变。
5. **挂接数是挂接表的实时计数**，不是生成器写进路由块的那份快照。快照是
   生成当时的数：挂接被耗尽（``exhausted``）之后表里已经没有活着的挂接，
   快照却还停在生成那一刻 —— 读模型要是回落那份快照，答出来的数与表矛盾。

数据是真的
----------
行是框架修复 lane 自己的表：``plan_routing``（``plan_kind`` 与
``framework_fix`` 块）、``framework_fix_registry``、``framework_fix_attach``、
``framework_fix_hit``、``framework_fix_merge_lease``，都在读模型读的那份已迁移
SQLite 文件里（``state_db`` fixture）。读模型由生产构建器
（``plan_scheduler.snapshot.build``）装配、请求走挂载的应用，所以这里断言的是
面板真正拿到的那份答案。

守卫请求头
----------
这里的每个请求都带 ``X-PDT-Request: 1``：请求守卫是每个 ``/api/`` 路径上的
中间件，没有这个头的请求根本到不了 handler。套件的 hermetic 环境把它注入了
每个 ``TestClient``，所以下面的调用没有写它 —— 而这里出现 403 意味着这个
路由在受守卫的表面之外，而不是调用方忘了带头。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from server import app

client = TestClient(app)


#: The endpoint under test.
STATE_PATH = "/api/scheduler/state"

#: The repair plan that waits in the queue.
QUEUED_REPAIR_PLAN_ID = "fx1"

#: The repair plan that holds a slot.
RUNNING_REPAIR_PLAN_ID = "fx2"

#: The ordinary plan that waits in the queue.
ORDINARY_PLAN_ID = "p-normal"

#: The defect the queued repair plan repairs.
QUEUED_FINGERPRINT = "sha256:" + "a" * 64

#: The defect the running repair plan repairs.
RUNNING_FINGERPRINT = "sha256:" + "b" * 64

#: The defect whose two attachments have been exhausted.
EXHAUSTED_FINGERPRINT = "sha256:" + "c" * 64

#: The repair plan of the exhausted defect.
EXHAUSTED_REPAIR_PLAN_ID = "fx3"

#: The defect whose attachments are still live.
LIVE_FINGERPRINT = "sha256:" + "d" * 64

#: The repair plan of the live defect.
LIVE_REPAIR_PLAN_ID = "fx4"

#: The signature the registry row carries.
SIGNATURE = {
    "framework_files": ["backend/framework_fix/registry.py"],
    "error_class": "AssertionError",
    "first_error_line": "assert 0 == 1",
}

#: The stamp the rows carry.
STAMP = "2026-11-24T00:00:00Z"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
def _insert(db_path: Path, statement: str, parameters: tuple) -> None:
    """Run one statement against the state database."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(statement, parameters)
        conn.commit()
    finally:
        conn.close()


def _routing_row(
    plan_id: str,
    *,
    kind: str,
    phase: str,
    block: Dict[str, Any],
) -> tuple:
    """One ``plan_routing`` row — the kind and the framework-fix block."""
    return (
        "INSERT INTO plan_routing "
        "(plan_id, current_phase, updated_at, plan_kind, framework_fix) "
        "VALUES (?, ?, ?, ?, ?)",
        (plan_id, phase, STAMP, kind, json.dumps(block)),
    )


@pytest.fixture
def repair_plans(state_db: Path) -> List[str]:
    """Two repair plans in the state database — the lane's own rows.

    One waits in the queue, one holds a slot.  The ordinary plan is in
    the database too, so the ordinary partition has something to keep.
    """
    _insert(
        state_db,
        *_routing_row(
            QUEUED_REPAIR_PLAN_ID,
            kind="framework_fix",
            phase="queued",
            block={
                "fingerprint": QUEUED_FINGERPRINT,
                "status": "waiting_on_user_merge",
                "source_plan_id": "p-source",
                "source_round": 3,
                "attach_blocked_count": 0,
                "attach_registered_count": 0,
                "repo": "app",
                "branch": "fix/fx1",
                "created_at": STAMP,
            },
        ),
    )
    _insert(
        state_db,
        *_routing_row(
            RUNNING_REPAIR_PLAN_ID,
            kind="framework_fix",
            phase="executing",
            block={
                "fingerprint": RUNNING_FINGERPRINT,
                "status": "active",
                "source_plan_id": "p-source",
                "source_round": 1,
                "attach_blocked_count": 0,
                "attach_registered_count": 0,
                "repo": "app",
                "branch": "fix/fx2",
                "created_at": STAMP,
            },
        ),
    )
    _insert(
        state_db,
        *_routing_row(
            ORDINARY_PLAN_ID, kind="normal", phase="queued", block={}
        ),
    )

    for fingerprint, fix_plan_id in (
        (QUEUED_FINGERPRINT, QUEUED_REPAIR_PLAN_ID),
        (RUNNING_FINGERPRINT, RUNNING_REPAIR_PLAN_ID),
    ):
        _insert(
            state_db,
            "INSERT INTO framework_fix_registry "
            "(fingerprint, fix_plan_id, repo, status, key_json, "
            "framework_files_json, evidence_summary_json, "
            "first_seen_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fingerprint,
                fix_plan_id,
                "app",
                "active",
                json.dumps(SIGNATURE),
                json.dumps(SIGNATURE["framework_files"]),
                json.dumps({}),
                STAMP,
                STAMP,
            ),
        )

    # One blocked attachment and one registered one — the counts the
    # partition entry carries.
    _insert(
        state_db,
        "INSERT INTO framework_fix_attach "
        "(attach_id, fingerprint, plan_id, vp_id, round, source, status, "
        "held_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "attach-1",
            QUEUED_FINGERPRINT,
            "p-blocked",
            "VP-1",
            3,
            "hit",
            "blocked",
            STAMP,
        ),
    )
    _insert(
        state_db,
        "INSERT INTO framework_fix_attach "
        "(attach_id, fingerprint, plan_id, vp_id, round, source, status, "
        "held_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "attach-2",
            QUEUED_FINGERPRINT,
            "p-registered",
            "VP-2",
            3,
            "hit",
            "registered",
            STAMP,
        ),
    )

    # The hit that started it all — the source of the defect.
    _insert(
        state_db,
        "INSERT INTO framework_fix_hit "
        "(hit_id, fingerprint, plan_id, vp_id, task_id, round, "
        "classification, blocking, disposition, first_seen_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "hit-1",
            QUEUED_FINGERPRINT,
            "p-source",
            "VP-1",
            "t-1",
            3,
            "framework_deterministic",
            1,
            "blocked",
            STAMP,
            STAMP,
        ),
    )

    # The merge slot of the repository — held by the queued repair plan.
    _insert(
        state_db,
        "INSERT INTO framework_fix_merge_lease "
        "(repo, fix_plan_id, pr_url, acquired_at) VALUES (?, ?, ?, ?)",
        ("app", QUEUED_REPAIR_PLAN_ID, "https://example.invalid/pr/1", STAMP),
    )

    return [QUEUED_REPAIR_PLAN_ID, RUNNING_REPAIR_PLAN_ID]


@pytest.fixture
def attach_count_plans(state_db: Path) -> List[str]:
    """Two repair plans whose attach counts only the live table can answer.

    The first one's two attachments have been **exhausted**: the live
    count of the attach table for its fingerprint is zero, but the
    routing block still carries the generator's snapshot of the moment
    the plan was created — two blocked and two registered.  A read model
    that falls back to that snapshot answers a number the table does not
    support.

    The second one has one blocked and two registered attachments, and
    its routing block claims five and six.  The live table is the one
    that answers.
    """
    for plan_id, fingerprint, snapshot in (
        (EXHAUSTED_REPAIR_PLAN_ID, EXHAUSTED_FINGERPRINT, (2, 2)),
        (LIVE_REPAIR_PLAN_ID, LIVE_FINGERPRINT, (5, 6)),
    ):
        _insert(
            state_db,
            *_routing_row(
                plan_id,
                kind="framework_fix",
                phase="executing",
                block={
                    "fingerprint": fingerprint,
                    "status": "active",
                    "source_plan_id": "p-source",
                    "source_round": 1,
                    # 生成器写下的快照 —— 读模型不读它。
                    "attach_blocked_count": snapshot[0],
                    "attach_registered_count": snapshot[1],
                    "repo": "app",
                    "branch": f"fix/{plan_id}",
                    "created_at": STAMP,
                },
            ),
        )
        _insert(
            state_db,
            "INSERT INTO framework_fix_registry "
            "(fingerprint, fix_plan_id, repo, status, key_json, "
            "framework_files_json, evidence_summary_json, "
            "first_seen_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fingerprint,
                plan_id,
                "app",
                "active",
                json.dumps(SIGNATURE),
                json.dumps(SIGNATURE["framework_files"]),
                json.dumps({}),
                STAMP,
                STAMP,
            ),
        )

    for index in (1, 2):
        _insert(
            state_db,
            "INSERT INTO framework_fix_attach "
            "(attach_id, fingerprint, plan_id, vp_id, round, source, status, "
            "held_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"attach-exhausted-{index}",
                EXHAUSTED_FINGERPRINT,
                f"p-exhausted-{index}",
                f"VP-{index}",
                1,
                "hit",
                "exhausted",
                STAMP,
            ),
        )

    _insert(
        state_db,
        "INSERT INTO framework_fix_attach "
        "(attach_id, fingerprint, plan_id, vp_id, round, source, status, "
        "held_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "attach-live-blocked",
            LIVE_FINGERPRINT,
            "p-live-blocked",
            "VP-3",
            1,
            "hit",
            "blocked",
            STAMP,
        ),
    )
    for index in (1, 2):
        _insert(
            state_db,
            "INSERT INTO framework_fix_attach "
            "(attach_id, fingerprint, plan_id, vp_id, round, source, status, "
            "held_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"attach-live-registered-{index}",
                LIVE_FINGERPRINT,
                f"p-live-registered-{index}",
                f"VP-{3 + index}",
                1,
                "hit",
                "registered",
                STAMP,
            ),
        )

    return [EXHAUSTED_REPAIR_PLAN_ID, LIVE_REPAIR_PLAN_ID]


class _FakeEngine:
    """The engine's read side, with a state to read.

    The production engine answers ``snapshot()`` with the read model
    assembled by :func:`plan_scheduler.snapshot.build` — the same
    function.  This one does the same thing with a state the test
    chooses, so the endpoint is exercised against the very read model
    the scheduler itself uses.
    """

    def __init__(self, state: Optional[Dict[str, Any]] = None) -> None:
        self._state = state if state is not None else {}

    def snapshot(self) -> Dict[str, Any]:
        from plan_scheduler.snapshot import build

        return build(self._state)


@pytest.fixture
def install_engine(monkeypatch):
    """Put an engine on the app — and take it away afterwards."""

    def _install(state: Optional[Dict[str, Any]] = None) -> _FakeEngine:
        engine = _FakeEngine(state)
        monkeypatch.setattr(
            app.state, "scheduler_engine", engine, raising=False
        )
        return engine

    return _install


def _state() -> Dict[str, Any]:
    """The scheduler's state: an ordinary plan and two repair plans.

    The repair plans are in the ordinary lists on purpose — the state
    is what the scheduler read, and the read model is what has to take
    them out.
    """
    return {
        "capacity": 4,
        "used": 1,
        "now": 1000.0,
        "min_runtime_seconds": 600,
        "queue": [
            {
                "plan_id": ORDINARY_PLAN_ID,
                "seq": 1,
                "priority": 0,
                "promote_order": None,
                "due": True,
                "reason": "waiting_for_slot",
            },
            {
                "plan_id": QUEUED_REPAIR_PLAN_ID,
                "seq": 2,
                "priority": 0,
                "promote_order": None,
                "due": True,
                "reason": "waiting_for_slot",
            },
        ],
        "running": [
            {
                "plan_id": RUNNING_REPAIR_PLAN_ID,
                "acquired_at": 900.0,
                "workspace": "/ws",
            },
        ],
    }


def _plan_ids(document: Dict[str, Any], name: str) -> List[str]:
    """The plans an ordinary partition list names."""
    return [str(row.get("plan_id") or "") for row in document.get(name) or []]


# ---------------------------------------------------------------------------
# TDD 规格
# ---------------------------------------------------------------------------
def test_partition_exclusive(install_engine, repair_plans) -> None:
    """修复计划只出现在框架修复分区里，普通分区一个字都不提它。"""
    install_engine(_state())

    response = client.get(STATE_PATH)

    assert response.status_code == 200, response.text
    body = response.json()

    partition = [str(row.get("plan_id") or "") for row in body["framework_fixes"]]
    assert partition == [QUEUED_REPAIR_PLAN_ID, RUNNING_REPAIR_PLAN_ID], partition

    ordinary = [
        plan_id
        for name in ("queue", "running", "waiting", "parked")
        for plan_id in _plan_ids(body, name)
    ]
    assert QUEUED_REPAIR_PLAN_ID not in ordinary, ordinary
    assert RUNNING_REPAIR_PLAN_ID not in ordinary, ordinary

    # 普通计划留在普通分区里 —— 过滤掉的只有修复计划。
    assert ORDINARY_PLAN_ID in ordinary, ordinary
    assert ORDINARY_PLAN_ID not in partition, partition


def test_kind_filter(install_engine, repair_plans) -> None:
    """``kind`` 挑分区：plans 只给普通分区，framework_fix 只给框架分区。"""
    install_engine(_state())

    plans_only = client.get(f"{STATE_PATH}?kind=plans")
    assert plans_only.status_code == 200, plans_only.text
    body = plans_only.json()
    assert body["framework_fixes"] == [], body["framework_fixes"]
    assert _plan_ids(body, "queue") == [ORDINARY_PLAN_ID], body["queue"]

    fixes_only = client.get(f"{STATE_PATH}?kind=framework_fix")
    assert fixes_only.status_code == 200, fixes_only.text
    body = fixes_only.json()
    assert [row["plan_id"] for row in body["framework_fixes"]] == [
        QUEUED_REPAIR_PLAN_ID,
        RUNNING_REPAIR_PLAN_ID,
    ], body["framework_fixes"]
    assert body["queue"] == [], body["queue"]
    assert body["running"] == [], body["running"]
    assert body["parked"] == [], body["parked"]
    assert body["queue_length"] == 0, body["queue_length"]

    both = client.get(f"{STATE_PATH}?kind=all")
    assert both.status_code == 200, both.text
    body = both.json()
    assert len(body["framework_fixes"]) == 2, body["framework_fixes"]
    assert _plan_ids(body, "queue") == [ORDINARY_PLAN_ID], body["queue"]


def test_unknown_kind_422(install_engine, repair_plans) -> None:
    """未知的 kind 是 422 —— 词表是闭的。"""
    install_engine(_state())

    response = client.get(f"{STATE_PATH}?kind=bogus")

    assert response.status_code == 422, response.text


def test_display_state_present(install_engine, repair_plans) -> None:
    """每个分区条目都带 ``display_state``，值来自读模型的那一个投影。"""
    install_engine(_state())

    response = client.get(f"{STATE_PATH}?kind=framework_fix")

    assert response.status_code == 200, response.text
    body = response.json()
    entries = {row["plan_id"]: row for row in body["framework_fixes"]}

    queued = entries[QUEUED_REPAIR_PLAN_ID]
    assert queued["display_state"] == "waiting_on_user_merge", queued

    # 没有特殊状态的修复计划回退到它自己的相位（架构决策点 6）。
    running = entries[RUNNING_REPAIR_PLAN_ID]
    assert running["display_state"] == "executing", running

    for row in body["framework_fixes"]:
        assert row["display_state"], row
        assert row["kind"] == "framework_fix", row
        assert row["plan_id"], row


def test_partition_entry_carries_the_defect_facts(
    install_engine, repair_plans
) -> None:
    """分区条目带着这条缺陷的事实：来源、签名摘要、挂接数、PR 链接。"""
    install_engine(_state())

    response = client.get(f"{STATE_PATH}?kind=framework_fix")

    assert response.status_code == 200, response.text
    body = response.json()
    entry = {
        row["plan_id"]: row for row in body["framework_fixes"]
    }[QUEUED_REPAIR_PLAN_ID]

    # 契约里的那十个字段 —— 架构决策点 6 列的就是它们，一个不多一个不少。
    assert set(entry) == {
        "plan_id",
        "title",
        "kind",
        "source",
        "signature_summary",
        "display_state",
        "attach_blocked_count",
        "attach_registered_count",
        "pr_url",
        "created_at",
    }, sorted(entry)

    assert entry["source"] == {"plan_id": "p-source", "round": 3}, entry
    assert "AssertionError" in entry["signature_summary"], entry
    assert entry["attach_blocked_count"] == 1, entry
    assert entry["attach_registered_count"] == 1, entry
    assert entry["pr_url"] == "https://example.invalid/pr/1", entry
    assert entry["created_at"], entry


def test_attach_counts_are_the_live_ones_not_the_generator_snapshot(
    install_engine, attach_count_plans
) -> None:
    """挂接数是挂接表的实时计数 —— 生成器写下的快照不参与读数。

    两条挂接被耗尽（``exhausted``）之后，表里已经没有活着的挂接：
    计数就是零。路由块里那份「生成当时的数」（两个 blocked、两个
    registered）留在那儿，读模型要是回落它，答出来的数与表矛盾。
    """
    install_engine(_state())

    response = client.get(f"{STATE_PATH}?kind=framework_fix")

    assert response.status_code == 200, response.text
    entries = {
        row["plan_id"]: row for row in response.json()["framework_fixes"]
    }

    exhausted = entries[EXHAUSTED_REPAIR_PLAN_ID]
    assert exhausted["attach_blocked_count"] == 0, exhausted
    assert exhausted["attach_registered_count"] == 0, exhausted


def test_attach_counts_follow_the_live_table_not_the_routing_block(
    install_engine, attach_count_plans
) -> None:
    """对照：还有活着的挂接时，计数等于表里的行数。

    路由块里写的 ``attach_blocked_count`` / ``attach_registered_count``
    是另一个数（这里写成 5 和 6），改它不影响读数。
    """
    install_engine(_state())

    response = client.get(f"{STATE_PATH}?kind=framework_fix")

    assert response.status_code == 200, response.text
    entries = {
        row["plan_id"]: row for row in response.json()["framework_fixes"]
    }

    live = entries[LIVE_REPAIR_PLAN_ID]
    assert live["attach_blocked_count"] == 1, live
    assert live["attach_registered_count"] == 2, live


def test_no_framework_plans_leaves_the_ordinary_partition_alone(
    install_engine,
) -> None:
    """没有修复计划 → 框架分区是空的，普通分区一个字都不变。"""
    install_engine(_state())

    response = client.get(f"{STATE_PATH}?kind=all")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["framework_fixes"] == [], body["framework_fixes"]
    assert _plan_ids(body, "queue") == [
        ORDINARY_PLAN_ID,
        QUEUED_REPAIR_PLAN_ID,
    ], body["queue"]
    assert _plan_ids(body, "running") == [RUNNING_REPAIR_PLAN_ID], body["running"]


def test_framework_fix_view_reads_the_partition() -> None:
    """``framework_fix_view`` 是分区的读法本身：没有修复计划就是空的。"""
    from plan_scheduler.snapshot import framework_fix_view

    assert framework_fix_view({}) == []


def test_the_route_and_the_read_model_share_one_vocabulary() -> None:
    """路由接受的词就是读模型知道的那三个 —— 两处说法必须一致。"""
    from plan_scheduler.snapshot import (
        KIND_ALL,
        KIND_FRAMEWORK_FIX,
        KIND_PLANS,
    )

    schema = app.openapi()
    parameters = schema["paths"][STATE_PATH]["get"]["parameters"]
    kind = [item for item in parameters if item["name"] == "kind"]

    assert kind, f"路由没有声明 kind 参数；实际 {parameters!r}"
    assert kind[0]["schema"]["enum"] == [
        KIND_ALL,
        KIND_PLANS,
        KIND_FRAMEWORK_FIX,
    ], kind[0]

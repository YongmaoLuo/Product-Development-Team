"""``user_stopping`` 的展示词表与忙碌语义（后端三处映射）。

背景
----
``user_stopping``（plan 级「正在停止」）进了相位词表（任务 1），但**词表里有
名字 ≠ 每条读路径都认得这个名字**。三处读路径各自漏掉它，后果各不相同：

* ``plan_state.PHASE_LABELS`` —— 没有这一行，展示面就回退成相位名本身，
  于是卡片与页面显示裸英文 ``user_stopping``；
* ``cards._PHASE_LABEL`` —— 卡片没有这一行，卡片标题退回裸英文；
* ``plan_scheduler.facts.VERIFICATION_PHASES`` —— 调度器不认这个相位，一个
  **正在停止**的计划就被当成空闲，槽位当场释放 —— 这正是事故里
  「显示已停、后台仍在派发」的那一半。

钉死的行为
----------
* ``PHASE_LABELS['user_stopping']`` 是中文标签「正在停止」；
* 卡片的 ``_PHASE_LABEL['user_stopping']`` 解析成「🛑 正在停止」，渲染出来
  的一行里不出现裸英文相位名；
* 调度器把 ``user_stopping`` 当作**忙碌**相位：处于该相位、验证行在跑的
  计划出现在活跃集合里（而不是被当成空闲）。

边界
----
* 三处映射缺一处，对应用例就红；
* 卡片文案必须是中文，不得出现裸英文相位名；
* 只新增映射，不改其它相位文案。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    """The current UTC time, as the state database writes it."""
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _has_ascii_letters(text: str) -> bool:
    """Whether ``text`` carries a bare English word.

    A Chinese label with an emoji in front of it is a Chinese label; a
    label that keeps the phase's own English name is a leak of the raw
    vocabulary into the display surface.
    """
    return any(("a" <= char.lower() <= "z") for char in text)


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """A state database on the production schema."""
    from state_machine.db.schema import migrate

    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        migrate(connection)
        yield connection
    finally:
        connection.close()


def _reader(conn: sqlite3.Connection, project_root: Path, **kwargs: Any) -> Any:
    """A facts reader over ``conn``."""
    from plan_scheduler.facts import PlanFactsReader

    return PlanFactsReader(conn, project_root, **kwargs)


def _routing(conn: sqlite3.Connection, plan_id: str, phase: str) -> None:
    """Write the plan's routing row: the phase it is in."""
    conn.execute(
        "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, phase, _now()),
    )


def _running_verification(conn: sqlite3.Connection, plan_id: str) -> None:
    """Write the plan's verification row: a round that is running now."""
    conn.execute(
        "INSERT INTO plan_verification "
        "(plan_id, verification_status, round, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (plan_id, "running", 1, _now()),
    )


# ---------------------------------------------------------------------------
# 1) plan_state 的展示标签
# ---------------------------------------------------------------------------


def test_plan_state_label_is_chinese() -> None:
    """``PHASE_LABELS['user_stopping']`` 是中文标签「正在停止」。

    The label is what the list interface buckets by, and what a page
    falls back to when it has no other name to show.  A missing entry
    does not raise there — it falls back to the phase name itself, and
    the operator reads a bare ``user_stopping``.
    """
    from plan_state import PHASE_LABELS

    assert "user_stopping" in PHASE_LABELS, (
        "PHASE_LABELS 里必须有 user_stopping；"
        f"实际有 {sorted(PHASE_LABELS)!r}"
    )

    label = PHASE_LABELS["user_stopping"]

    assert label == "正在停止", (
        f"user_stopping 的标签必须是「正在停止」；实际 {label!r}"
    )
    assert not _has_ascii_letters(label), (
        f"标签里不能出现裸英文；实际 {label!r}"
    )


# ---------------------------------------------------------------------------
# 2) 卡片的相位文案
# ---------------------------------------------------------------------------


def test_card_label_resolves() -> None:
    """卡片的 ``_PHASE_LABEL`` 解析出中文文案，不回退成裸英文。

    The card's header line is built from this table, so a missing entry
    is not an exception but a raw phase name in the operator's face.
    """
    from notifications import cards

    assert "user_stopping" in cards._PHASE_LABEL, (
        "cards._PHASE_LABEL 里必须有 user_stopping；"
        f"实际有 {sorted(cards._PHASE_LABEL)!r}"
    )

    label = cards._PHASE_LABEL["user_stopping"]

    assert label == "🛑 正在停止", (
        f"卡片的 user_stopping 文案必须是「🛑 正在停止」；实际 {label!r}"
    )
    assert "user_stopping" not in label, (
        f"卡片文案里不能出现裸英文相位名；实际 {label!r}"
    )

    # The label has to survive the renderer, not just the table: the
    # line the operator reads is built from this lookup.
    section = cards._unified_phase_section("user_stopping", None)
    line = section[0]["text"]["content"]

    assert "🛑 正在停止" in line, (
        f"渲染出来的一行里必须有中文文案；实际 {line!r}"
    )
    assert "user_stopping" not in line, (
        f"渲染出来的一行里不能出现裸英文相位名；实际 {line!r}"
    )


# ---------------------------------------------------------------------------
# 3) 调度器把 user_stopping 当作忙碌
# ---------------------------------------------------------------------------


def test_scheduler_treats_as_busy(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """处于 ``user_stopping`` 的计划在调度器眼里是**忙碌**的。

    The scheduler releases a slot as soon as a plan stops looking busy.
    A plan that is *being stopped* still holds compute — the stop is
    in flight — so treating it as idle hands its slot to somebody else
    while its own worker is still running.

    The phase is the plan's own (``plan_routing.current_phase``), the
    round is running, and the heartbeat is fresh: with the phase
    recognised, the plan is reported as active.
    """
    from plan_scheduler.facts import VERIFICATION_PHASES

    assert "user_stopping" in VERIFICATION_PHASES, (
        "调度器的忙碌相位词表里必须有 user_stopping；"
        f"实际有 {sorted(VERIFICATION_PHASES)!r}"
    )

    plans_root = tmp_path / "plans"
    (plans_root / "p-1").mkdir(parents=True, exist_ok=True)
    _routing(conn, "p-1", "user_stopping")
    _running_verification(conn, "p-1")

    active = _reader(conn, plans_root).active_plans()

    assert [entry["plan_id"] for entry in active] == ["p-1"], (
        "正在停止的计划必须算忙碌（占着槽位）；"
        f"实际活跃集合 {[entry['plan_id'] for entry in active]!r}"
    )
    assert active[0]["side"] == "verification", (
        f"它占的是验证那一侧的槽位；实际 {active[0]['side']!r}"
    )
    assert active[0]["phase"] == "user_stopping", (
        f"活跃行要带上相位；实际 {active[0]['phase']!r}"
    )

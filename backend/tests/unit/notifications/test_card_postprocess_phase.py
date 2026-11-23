"""卡片认识收尾相位：收尾中 / 收尾失败说得出一句话。

一次事故：某个计划验证通过、收尾第一项失败，卡片从此再没更新过 ——
一直显示「🔄 验证中」。（事故细节不入库：仓库的规矩是「缺陷公开、
事故私有」，计划编号与时间戳不进源码。）

两个原因，各钉一条：

* **收尾期间**（``post_processing``）``verification_status`` 一直是
  ``running``（终态要等收尾阶段自己写），所以卡片按
  ``verification_status == "running"`` 渲染成「🔄 验证中」，
  一直说到收尾结束 —— 甚至验证状态已经是 ``passed`` 时也一样。
* **收尾失败**（``postprocess_failed``）相位不在任何扫描集合里，
  终态又带 ``publish_closed=False``，卡片永久冻结。

这个文件钉住的是**头部**（``_resolve_header``）—— 卡片的标题行：

* 收尾中 → 标题必须说「收尾中」，且不能被验证状态盖掉；
* 收尾失败 → 标题必须说「收尾失败」，且不能说「验证失败」
  （验证其实过了，是收尾没过）；
* 没有要求书的 ``passed`` 路径**一字不改**（回归）。

关于第 2 条用例的措辞：收尾中的标题是「✅ 验证通过 · 🧹 收尾中」——
它**含有**「验证通过」三个字。所以这里断言的不是「标题里没有出现过
『验证通过』这几个字」，而是「**验证通过**那条分支没有**接管**标题」：
标题不是裸的「✅ 验证通过 · <plan_id>」，而是带上了收尾中这一段。
这正是需求里「后者会盖住收尾」的意思。
"""

from __future__ import annotations

import pytest

from notifications.cards import _resolve_header
from plan_status import PlanStatus


PLAN_ID = "p-card-postprocess-phase"

#: A finished task list — a plan that really ran its tasks.
TASKS = {"total": 12, "completed": 12, "failed": 0, "skipped": 0}


def _header(
    *,
    phase: str,
    verification_status: str,
    **kwargs,
) -> str:
    """The card title for one snapshot."""
    status = PlanStatus(
        plan_id=PLAN_ID,
        phase=phase,
        verification_status=verification_status,
        tasks=dict(TASKS),
        **kwargs,
    )
    return _resolve_header(status, dict(TASKS))["title"]


# ---------------------------------------------------------------------------
# 收尾中
# ---------------------------------------------------------------------------


def test_post_processing_says_collecting_not_verifying():
    """收尾期间 ``verification_status`` 是 ``running`` —— 标题必须说收尾中。

    旧行为：标题是「🔄 验证中」，收尾跑多久就说多久。
    """
    title = _header(phase="post_processing", verification_status="running")

    assert "收尾中" in title, title
    assert "验证中" not in title, title


def test_post_processing_is_not_covered_by_the_passed_verdict():
    """收尾期间 ``verification_status`` 也可能是 ``passed``。

    断言的是「验证通过那条分支没有接管标题」：标题不是裸的
    「✅ 验证通过 · <plan_id>」，收尾中这一段还在。
    """
    title = _header(phase="post_processing", verification_status="passed")

    assert "收尾中" in title, title
    assert title != f"✅ 验证通过 · {PLAN_ID}", title
    assert title.startswith("✅ 验证通过 · "), title
    assert title.endswith(f"· {PLAN_ID}"), title


def test_post_processing_ignores_a_stale_in_flight_execution():
    """收尾相位就是收尾相位 —— 即使有残留的执行在飞。

    收尾阶段开始时执行已经归零，但相位是权威的：相位说收尾中，
    标题就说收尾中。
    """
    title = _header(
        phase="post_processing",
        verification_status="running",
        execution_in_flight=True,
    )

    assert "收尾中" in title, title


# ---------------------------------------------------------------------------
# 收尾失败
# ---------------------------------------------------------------------------


def test_postprocess_failed_says_postprocess_failed_not_verification_failed():
    """收尾失败 ≠ 验证失败。

    验证过了，是收尾没过 —— 标题说「❌ 验证失败」是假的。
    """
    title = _header(
        phase="postprocess_failed", verification_status="failed",
    )

    assert "收尾失败" in title, title
    assert "验证失败" not in title, title


def test_postprocess_failed_says_collecting_failed_not_collecting():
    """收尾失败不能被读成「还在收尾」。"""
    title = _header(
        phase="postprocess_failed", verification_status="failed",
    )

    assert "收尾中" not in title, title


def test_postprocess_failed_wins_even_when_the_verdict_reads_passed():
    """相位是权威的：即使验证状态列写着 ``passed``，收尾失败也要说出来。"""
    title = _header(
        phase="postprocess_failed", verification_status="passed",
    )

    assert "收尾失败" in title, title


# ---------------------------------------------------------------------------
# 回归：没有要求书的 passed 路径一字不改
# ---------------------------------------------------------------------------


def test_the_plain_passed_header_is_unchanged():
    """没有要求书 → 验证通过 → 标题还是原来那句。"""
    title = _header(phase="verification_passed", verification_status="passed")

    assert title == f"✅ 验证通过 · {PLAN_ID}", title


def test_the_plain_failed_header_is_unchanged():
    """验证真的失败了 → 标题还是「❌ 验证失败」。"""
    title = _header(phase="verification_failed", verification_status="failed")

    assert title == f"❌ 验证失败 · {PLAN_ID}", title


def test_the_plain_running_header_is_unchanged():
    """验证还在跑 → 标题还是「🔄 验证中」。"""
    title = _header(phase="verification_running", verification_status="running")

    assert title.startswith("🔄 验证中 · "), title


# ---------------------------------------------------------------------------
# 相位标签表
# ---------------------------------------------------------------------------


def test_the_two_new_phases_have_labels():
    """两个相位在标签表里各有话说 —— 没有标签的相位卡片上只能显示裸英文。"""
    from notifications.cards import _PHASE_LABEL

    assert _PHASE_LABEL["post_processing"] == "🧹 收尾中"
    assert _PHASE_LABEL["postprocess_failed"] == "⚠️ 收尾失败"


@pytest.mark.parametrize(
    "phase,expect",
    [
        ("post_processing", "🧹 收尾中"),
        ("postprocess_failed", "⚠️ 收尾失败"),
    ],
)
def test_the_unified_line_names_the_finishing_phase(phase, expect):
    """「📍 当前状态」那一行也要说得出收尾相位。"""
    from notifications.cards import _unified_phase_section

    out = _unified_phase_section(phase, verification_status="running")
    assert out, phase
    content = out[0]["text"]["content"]
    assert expect in content, content


# ---------------------------------------------------------------------------
# 启动扫描集合
# ---------------------------------------------------------------------------


def test_the_startup_sweep_covers_the_finishing_window():
    """收尾要跑 CI / 性能，可能几十分钟 —— 服务器重启后要能把卡片补上。"""
    from notifications.plan_dir_resolver import _ACTIVE_PHASES

    assert "post_processing" in _ACTIVE_PHASES, _ACTIVE_PHASES


def test_the_startup_sweep_does_not_cover_the_parked_phase():
    """收尾失败是 parked —— 靠一条事件推一次就够，反复刷新是噪音。"""
    from notifications.plan_dir_resolver import _ACTIVE_PHASES

    assert "postprocess_failed" not in _ACTIVE_PHASES, _ACTIVE_PHASES

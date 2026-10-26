"""等待相位词汇的唯一归一化实现（R10：queued 成为唯一等待相位）。

背景
----
``plan_state`` 里已经有一个 :func:`plan_state.normalize_phase`（返回 ``str``，
处理历史别名）。等待相位是**另一套语义**：它要回答「这个相位是不是在等人」
，并且在人等的这件事上，历史值 ``ready`` 与现在的 ``queued`` 是同一个意思。

两套归一化如果同名，读代码的人会把它们当成同一个函数，而它们的返回值形状
（``str`` vs ``(str, bool)``）根本不同。因此本模块的函数**不叫**
``normalize_phase``，叫 :func:`normalize_waiting_phase`——名字里必须带
"waiting"，否则迟早有人 import 错。

钉死的行为
----------
* ``'ready'`` → ``('queued', True)``：归一化 + 标记为「这是历史别名」。
* 其它相位（``'executing'``、``'verification_running'`` 等）→ 原值 +
  ``aliased=False``：本模块不是校验器，来路不明的值不吞、不改。
* ``None`` / 空串 → 原值 + ``aliased=False``，不抛异常：读路径上拿到的可能是
  缺字段的行，抛异常会把「读旧数据」变成「整个接口 500」。
* 只精确匹配小写 ``'ready'``：``'Ready'`` 不是任何地方写出来的值，归一化它
  只会掩盖真实的数据问题。
* 弃用要**留痕**：读到历史值时打一条 warning，带上 plan_id 与来源，否则
  旧数据永远清不掉而没人知道它还在。
"""

from __future__ import annotations

import logging

from phase_vocabulary import (
    LEGACY_READY,
    QUEUED,
    is_waiting_phase,
    log_deprecated_ready,
    normalize_waiting_phase,
)


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_normalize_ready_returns_queued() -> None:
    """``ready`` 是 ``queued`` 的历史别名，归一化并标记 aliased。"""
    assert normalize_waiting_phase("ready") == (QUEUED, True)


def test_normalize_other_phase_unchanged() -> None:
    """非等待相位原值返回，不标记为别名。"""
    assert normalize_waiting_phase("executing") == ("executing", False)


def test_normalize_unknown_phase_unchanged() -> None:
    """未知相位原值返回——本模块不是校验器，不吞也不改来路不明的值。"""
    assert normalize_waiting_phase("verification_running") == (
        "verification_running",
        False,
    )


def test_normalize_none_returns_none_without_raising() -> None:
    """``None`` 原值返回，不抛异常：缺字段的行不该把读路径变成 500。"""
    assert normalize_waiting_phase(None) == (None, False)


def test_normalize_empty_string_returns_empty_without_raising() -> None:
    """空串原值返回，不抛异常。"""
    assert normalize_waiting_phase("") == ("", False)


def test_normalize_is_case_sensitive() -> None:
    """只精确匹配小写 ``ready``；``Ready`` 不归一化。"""
    assert normalize_waiting_phase("Ready") == ("Ready", False)
    assert normalize_waiting_phase("READY") == ("READY", False)


def test_normalize_queued_is_not_aliased() -> None:
    """``queued`` 本身是现行词汇，不是别名。"""
    assert normalize_waiting_phase(QUEUED) == (QUEUED, False)


# ---------------------------------------------------------------------------
# 等待相位判定
# ---------------------------------------------------------------------------


def test_is_waiting_phase_recognizes_current_and_legacy() -> None:
    """现行值与历史别名都算「在等人」——否则旧行会被当成不在等。"""
    assert is_waiting_phase(QUEUED) is True
    assert is_waiting_phase(LEGACY_READY) is True


def test_is_waiting_phase_rejects_other_phases() -> None:
    """其它相位不是等待相位；``None`` / 空串也不是。"""
    assert is_waiting_phase("executing") is False
    assert is_waiting_phase("verification_running") is False
    assert is_waiting_phase(None) is False
    assert is_waiting_phase("") is False


# ---------------------------------------------------------------------------
# 弃用日志
# ---------------------------------------------------------------------------


def test_log_deprecated_ready_emits_warning(caplog) -> None:
    """读到历史值要留痕：warning 里必须带 plan_id 与来源。"""
    logger = logging.getLogger("phase_vocabulary.test")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        log_deprecated_ready(logger, "plan-20261025-abc", "scheduler")

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "弃用没有留痕：历史值会被无声地吞掉"

    text = "\n".join(r.getMessage() for r in warnings)
    assert "plan-20261025-abc" in text
    assert "scheduler" in text
"""pipeline 的任务生成落点写的是 ``queued``，不是 ``ready``。

背景
----
「任务已生成、等人（或调度器）决定何时开始」这件事，历史上 pipeline 写成
``ready``。R10 把 ``queued`` 定为**唯一**的等待相位，``ready`` 降级为**输入
别名** —— 读路径仍然接受它，但写路径不再产生它。

pipeline 是**写路径**。它继续写 ``ready`` 会有两个后果：任务 16 的门禁把它
拦下来；更糟的是，读者会以为 ``ready`` 仍是一个合法的落点。

钉死的行为
----------
* ``pipeline.py`` 里写的是字面量 ``transition_to("queued")``；
* 源码里**不再**出现 ``transition_to("ready")``；
* ``tasks.json`` 的落盘早于那次相位翻转 —— 顺序反了的话，调度器会看到一个
  「已经排队、但还没有任务可跑」的计划。

边界
----
* 写点用**字面量** ``"queued"``，不引入新常量：任务 12 的既有源码断言按
  字面量匹配，常量化会把那条断言打空。
* 生成失败路径的相位不变（仍走 ``failed``）—— 本次只改等待相位这一个落点。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

#: ``backend/tests/unit/test_pipeline_queued_phase.py`` → ``backend/pipeline.py``
PIPELINE_SOURCE = _BACKEND_DIR / "pipeline.py"


def _source() -> str:
    assert PIPELINE_SOURCE.is_file(), f"找不到 pipeline 源码: {PIPELINE_SOURCE}"
    return PIPELINE_SOURCE.read_text(encoding="utf-8")


def _line_of(src: str, needle: str) -> int:
    """``needle`` 第一次出现的一行号（1 起）；找不到就失败。"""
    for idx, line in enumerate(src.splitlines(), start=1):
        if needle in line:
            return idx
    raise AssertionError(f"源码里找不到 {needle!r}")


def _phase_targets(src: str) -> list:
    """源码里所有 ``transition_to("<相位>")`` 的相位字面量。"""
    return re.findall(r'transition_to\("([a-z_]+)"\)', src)


def test_pipeline_uses_queued_transition():
    """pipeline 的等待相位写的是字面量 ``queued``。"""
    src = _source()
    assert 'transition_to("queued")' in src


def test_pipeline_has_no_ready_transition():
    """写路径不再产生 ``ready`` —— 它只是输入别名。"""
    src = _source()
    assert 'transition_to("ready")' not in src


def test_tasks_are_written_before_the_phase_flip():
    """``tasks.json`` 落盘早于相位翻转，否则调度器会捡到一个空计划。"""
    src = _source()
    write_line = _line_of(src, "tasks_gen.generate()")
    flip_line = _line_of(src, 'transition_to("queued")')
    assert write_line < flip_line, (
        f"tasks.json 的落盘（第 {write_line} 行）必须早于相位翻转"
        f"（第 {flip_line} 行）"
    )


def test_failed_phase_is_untouched():
    """生成失败路径的相位不变（仍走 ``failed``）。

    本次只换等待相位这一个落点：pipeline 从来不写 ``failed``（生成失败由
    生成器/执行器负责），所以换相位之后它仍然不写 —— 失败路径的落点没被动过。
    """
    src = _source()
    assert "failed" not in _phase_targets(src), "pipeline 不接管失败相位"

    from plan_state import PHASE_TRANSITIONS

    assert any(
        "failed" in targets for targets in PHASE_TRANSITIONS.values()
    ), "failed 仍在相位词汇里 —— 失败路径的落点没有被删掉"

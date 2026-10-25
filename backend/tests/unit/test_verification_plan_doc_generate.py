"""``verification-plan.md`` 的生成入口：非法文档在落盘前被拒绝。

背景
----
计划书由 LLM 生成，但结构非法的文档不得进入人工审核 —— 一份缺章节的计划书
一旦落进 ``plan_dir``，审核者看到的就是一份「看起来完整」的文件，而下游翻译
管道要按十章节契约去解析它。所以生成入口的职责是：**先校验，后落盘**。

判据
----
本文件钉住生成入口的五条行为：

* 合法响应 → 落盘且可解析（十章节齐全、基线行在位）；
* 非法响应 → 抛 :class:`PlanDocError`，磁盘上**没有**这份文档；
* ``context`` 缺 baseline → 生成前就报错，LLM 根本没被问；
* 连续两次非法 → 仍然不落盘，错误带上路由层要的 422；
* 第一次非法、第二次合法 → 落盘，且第二次的提示词里带着第一次的结构错误。

测试数据
--------
两份脚本化响应都在本文件里自建：一份合法文档，一份删掉「风险面」章节的非法
文档。响应按调用次序回放，用尽即报错 —— 生成入口多问一次就会暴露出来。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification_plan_doc import (  # noqa: E402
    REQUIRED_SECTIONS,
    PlanDocError,
    generate,
    parse_plan_doc,
)

#: 一份合法的计划书 —— 脚本化 LLM 的合法响应。
VALID_DOC = """\
# 验证计划书

本计划书是对被验证对象的人工审核对象，也是机器格式翻译的唯一来源。

## 被验证对象

被验证对象为 product-development-team 仓库 PR #7 的 head，含配额等待的重构。
baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7

## 验证范围

- 配额等待按真实重开时刻睡

## 排除范围

- 前端交互（本次未改动前端）

## 验证方法

- 以单元测试为主，必要时做一次手工核验

## 验证点清单

### VP-001 配额按真实重开时刻睡眠

- 范围：backend/verification_loop.py 的配额等待路径
- 验证方法：单元测试
- 通过判据：
  - C1: 配额耗尽时睡到真实重开时刻
- 证据要求：
  - E1: tests/unit/test_quota_wait.py 的 pytest 输出
- 预期结果：判据满足，无超时

## 预期结果

- 全部验证点通过，未发现需要修复的缺陷

## 证据要求

- 每个验证点给出可复跑的命令与原始输出

## 通过判据

- 所有验证点的 C 编号判据全部满足

## 环境与依赖

- Python 3.11 与 pytest 可用

## 风险面

- 上游 PR head 可能移动，导致基线漂移
"""

#: 非法文档：删掉最后一个章节「风险面」，其余字节与合法文档一致。
INVALID_DOC = """\
# 验证计划书

本计划书是对被验证对象的人工审核对象，也是机器格式翻译的唯一来源。

## 被验证对象

被验证对象为 product-development-team 仓库 PR #7 的 head，含配额等待的重构。
baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7

## 验证范围

- 配额等待按真实重开时刻睡

## 排除范围

- 前端交互（本次未改动前端）

## 验证方法

- 以单元测试为主，必要时做一次手工核验

## 验证点清单

### VP-001 配额按真实重开时刻睡眠

- 范围：backend/verification_loop.py 的配额等待路径
- 验证方法：单元测试
- 通过判据：
  - C1: 配额耗尽时睡到真实重开时刻
- 证据要求：
  - E1: tests/unit/test_quota_wait.py 的 pytest 输出
- 预期结果：判据满足，无超时

## 预期结果

- 全部验证点通过，未发现需要修复的缺陷

## 证据要求

- 每个验证点给出可复跑的命令与原始输出

## 通过判据

- 所有验证点的 C 编号判据全部满足

## 环境与依赖

- Python 3.11 与 pytest 可用
"""

#: 生成上下文：计划 id + 基线事实。
CONTEXT = {
    "plan_id": "p1",
    "baseline": {"repo": "/tmp/repo", "ref": "refs/pull/7/head", "pr": 7},
}


class ScriptedTool:
    """按调用次序回放脚本化响应。

    响应按次序弹出，用尽即报错：生成入口多问一次（而不是重试或拒绝）就会
    在测试里暴露，而不是悄悄多花一次模型调用。
    """

    def __init__(self, responses: List[str]) -> None:
        self.responses = list(responses)
        self.prompts: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("脚本化响应已用尽：生成入口多问了一次")
        return self.responses.pop(0)


class ConstantTool:
    """每次都回放同一份响应 —— 模型反复写错同一份文档。"""

    def __init__(self, doc: str) -> None:
        self.doc = doc
        self.prompts: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.doc


class SilentTool:
    """第一次给一份非法文档，之后不再回答。"""

    def __init__(self, doc: str) -> None:
        self.doc = doc
        self.prompts: List[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) > 1:
            raise RuntimeError("模型没有返回")
        return self.doc


# ---------------------------------------------------------------------------
# 1. 合法响应 → 落盘且可解析
# ---------------------------------------------------------------------------


def test_generate_writes_valid_doc(tmp_path: Path) -> None:
    tool = ScriptedTool([VALID_DOC])

    path = generate(tmp_path, CONTEXT, tool=tool)

    assert path == tmp_path / "verification-plan.md"
    assert path.exists()

    text = path.read_text(encoding="utf-8")
    parsed = parse_plan_doc(text)
    assert list(parsed.sections) == list(REQUIRED_SECTIONS)
    assert parsed.baseline.repo_dir == Path("/tmp/repo")
    assert parsed.baseline.ref == "refs/pull/7/head"
    assert parsed.baseline.pr_number == 7
    assert "baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7" in text
    assert [item.id for item in parsed.items] == ["VP-001"]


def test_generate_prompt_carries_the_baseline_line(tmp_path: Path) -> None:
    """提示词必须把基线行原文交给模型 —— 它是机器事实，不是叙述。"""
    tool = ScriptedTool([VALID_DOC])

    generate(tmp_path, CONTEXT, tool=tool)

    assert len(tool.prompts) == 1
    assert "baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7" in tool.prompts[0]


# ---------------------------------------------------------------------------
# 2. 非法响应 → 抛错且不落盘
# ---------------------------------------------------------------------------


def test_generate_rejects_missing_section_before_write(tmp_path: Path) -> None:
    tool = ConstantTool(INVALID_DOC)

    with pytest.raises(PlanDocError) as excinfo:
        generate(tmp_path, CONTEXT, tool=tool)

    assert not (tmp_path / "verification-plan.md").exists()
    codes = [error.code for error in excinfo.value.errors]
    assert "missing_section" in codes


def test_generate_rejects_after_two_invalid_attempts(tmp_path: Path) -> None:
    """连续两次非法 → 仍然不落盘，错误带上路由层要的 422。"""
    tool = ScriptedTool([INVALID_DOC, INVALID_DOC])

    with pytest.raises(PlanDocError) as excinfo:
        generate(tmp_path, CONTEXT, tool=tool)

    assert not (tmp_path / "verification-plan.md").exists()
    assert len(tool.prompts) == 2
    assert excinfo.value.status_code == 422


def test_generate_reports_first_errors_when_the_retry_gets_no_response(
    tmp_path: Path,
) -> None:
    """重试那一次工具没回话 → 报第一次的结构错误，不报工具的异常。"""
    tool = SilentTool(INVALID_DOC)

    with pytest.raises(PlanDocError) as excinfo:
        generate(tmp_path, CONTEXT, tool=tool)

    assert not (tmp_path / "verification-plan.md").exists()
    codes = [error.code for error in excinfo.value.errors]
    assert "missing_section" in codes


def test_generate_writes_after_a_repair_attempt(tmp_path: Path) -> None:
    """第一次非法、第二次合法 → 落盘，且第二次提示词带着第一次的结构错误。"""
    tool = ScriptedTool([INVALID_DOC, VALID_DOC])

    path = generate(tmp_path, CONTEXT, tool=tool)

    assert path.exists()
    assert len(tool.prompts) == 2
    assert "missing_section" in tool.prompts[1]


# ---------------------------------------------------------------------------
# 3. context 缺 baseline → 生成前直接报错
# ---------------------------------------------------------------------------


def test_generate_requires_baseline(tmp_path: Path) -> None:
    tool = ScriptedTool([VALID_DOC])

    with pytest.raises(PlanDocError) as excinfo:
        generate(tmp_path, {"plan_id": "p1"}, tool=tool)

    assert not (tmp_path / "verification-plan.md").exists()
    assert tool.prompts == []
    assert excinfo.value.errors[0].code == "context_missing_baseline"


def test_generate_rejects_incomplete_baseline(tmp_path: Path) -> None:
    """baseline 缺键 → 同样在生成前报错，LLM 不会被问。"""
    tool = ScriptedTool([VALID_DOC])
    context = {"plan_id": "p1", "baseline": {"repo": "/tmp/repo"}}

    with pytest.raises(PlanDocError) as excinfo:
        generate(tmp_path, context, tool=tool)

    assert not (tmp_path / "verification-plan.md").exists()
    assert tool.prompts == []
    assert excinfo.value.errors[0].code == "context_incomplete_baseline"


def test_generate_rejects_relative_baseline_repo(tmp_path: Path) -> None:
    """上下文里的基线与文档里的基线行是同一套语法：相对路径当场拒绝。"""
    tool = ScriptedTool([VALID_DOC])
    context = {
        "plan_id": "p1",
        "baseline": {"repo": "repo", "ref": "refs/pull/7/head", "pr": 7},
    }

    with pytest.raises(PlanDocError) as excinfo:
        generate(tmp_path, context, tool=tool)

    assert not (tmp_path / "verification-plan.md").exists()
    assert tool.prompts == []
    assert excinfo.value.errors[0].code == "baseline_relative_path"

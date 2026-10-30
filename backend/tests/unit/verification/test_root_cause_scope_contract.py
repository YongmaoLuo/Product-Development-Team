"""TDD spec — 根因任务的改动范围契约（作者侧 ↔ 读者侧对齐）。

背景
----
根因链的**读者**（``verification.orchestrator._root_cause_scope_from_text``）
只认两种形态：``## 改动范围`` 等六种标题之一开头的章节，或者 JSON 里的
``paths`` / ``files_to_modify`` / ``fix_plan``。读不出路径就判
``root_cause_report_without_scope``，自动修复停摆。

而**作者**（``root_cause_task.build_root_cause_task``）此前从未被告知这个
契约：description 里没有模板，``ROOT_CAUSE_DELIVERABLE`` 里也没有「改动
范围」这一项。写报告的人不知道要交什么，交出来的就是叙事体。

TDD contract — 4 cases
-----------------------
1. ``test_description_carries_scope_contract`` —— description 带模板：
   ``## 改动范围`` 标题、路径示例、JSON 备选形状。
2. ``test_heading_matches_reader_constant`` —— 模板标题与 reader 的
   ``_ROOT_CAUSE_SCOPE_HEADINGS`` 逐字对齐（读者能前缀匹配上）。
3. ``test_template_round_trips_through_reader`` —— **回归钉子**：从
   description **原文切片**取出 ``## 改动范围`` 那一节，喂给 reader，
   必须解析出 ``backend/example_scope.py``；整段 description 再喂一次，
   也必须解析出至少一条路径。
4. ``test_deliverable_names_scope`` —— deliverable 里有「改动范围」这一项，
   且任务仍然是只读诊断任务（没有 ``files_to_modify``）。

边界条件
--------
* 标题必须与 reader 逐字对齐（``## 改动范围``），不得写成「改动清单」
  「变更文件」等变体 —— 读者只认 ``_ROOT_CAUSE_SCOPE_HEADINGS`` 里的六种。
* 切片必须是**原文**：不许把模板重拼一遍再喂给 reader —— 那样测的是
  测试自己写的字符串，不是任务真正下发给作者的那一段。
* 不改 ``read_only`` 语义，不给诊断任务加 ``files_to_modify``。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from root_cause_task import (  # noqa: E402
    build_root_cause_task,
)
from verification.orchestrator import (  # noqa: E402
    _ROOT_CAUSE_SCOPE_HEADINGS,
    _root_cause_scope_from_text,
)


#: 模板里给的示例路径。读者读出来的必须是它。
EXAMPLE_PATH = "backend/example_scope.py"

#: 模板用的标题 —— 与 reader 的 ``_ROOT_CAUSE_SCOPE_HEADINGS`` 首项一致。
TEMPLATE_HEADING = "改动范围"


def _scope_section(text: str) -> str:
    """从 description **原文**里切出 ``## 改动范围`` 那一节。

    切法：标题行起，到下一个标题行止（没有下一个标题就切到文末）。
    切出来的东西必须是原文的子串 —— 重拼一遍就测不到真正的契约了。
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") and TEMPLATE_HEADING in stripped:
            start = index
            break
    assert start is not None, "description 里没有「改动范围」这一节"

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].strip().startswith("#"):
            end = index
            break

    section = "\n".join(lines[start:end])
    assert section in text, "切出来的必须是原文的子串"
    return section


# ---------------------------------------------------------------------------
# 1. description 带改动范围契约
# ---------------------------------------------------------------------------
def test_description_carries_scope_contract():
    """description 里有标题、路径示例和 JSON 备选形状。"""
    task = build_root_cause_task("p1", "VP-3", 2)
    text = task["description"]

    assert "## 改动范围" in text
    assert f"- `{EXAMPLE_PATH}`" in text
    # JSON 备选：读者也认这个形状（``files_to_modify`` / ``fix_plan``）。
    assert "files_to_modify" in text
    assert "fix_plan" in text


# ---------------------------------------------------------------------------
# 2. 标题与 reader 的常量逐字对齐
# ---------------------------------------------------------------------------
def test_heading_matches_reader_constant():
    """模板标题能被 ``_ROOT_CAUSE_SCOPE_HEADINGS`` 前缀匹配上。"""
    task = build_root_cause_task("p1", "VP-3", 2)
    text = task["description"]

    heading_line = next(
        line for line in text.splitlines()
        if line.strip().startswith("#") and TEMPLATE_HEADING in line
    )
    # reader 的读法：去掉所有前导 ``#``，再按候选前缀匹配。
    heading = heading_line.strip().lstrip("#").strip().lower()
    assert any(
        heading.startswith(candidate.lower())
        for candidate in _ROOT_CAUSE_SCOPE_HEADINGS
    )
    # 而且用的就是首选标题，不是变体。
    assert heading == _ROOT_CAUSE_SCOPE_HEADINGS[0].lower()


# ---------------------------------------------------------------------------
# 3. 作者↔读者回归钉子
# ---------------------------------------------------------------------------
def test_template_round_trips_through_reader():
    """模板原文喂给 reader，必须解析出示例路径。"""
    task = build_root_cause_task("p1", "VP-3", 2)
    text = task["description"]

    section = _scope_section(text)
    assert _root_cause_scope_from_text(section) == [EXAMPLE_PATH]

    # 整段 description 也得能读出至少一条路径。
    whole = _root_cause_scope_from_text(text)
    assert len(whole) >= 1
    assert EXAMPLE_PATH in whole


# ---------------------------------------------------------------------------
# 4. deliverable 点名「改动范围」
# ---------------------------------------------------------------------------
def test_deliverable_names_scope():
    """deliverable 里有「改动范围」这一项，任务仍是只读诊断任务。"""
    task = build_root_cause_task("p1", "VP-3", 2)

    deliverable = task["deliverable"]
    assert len(deliverable) >= 5
    assert "改动范围" in deliverable[4]
    assert any("改动范围" in item for item in deliverable)
    # 前四项是旧契约，一项都不能被这次改动挤掉。
    assert deliverable[:4] == [
        "确定性复现步骤",
        "完整因果链",
        "一次覆盖所有层的修复方案",
        "验收方式（只选中这次失败的用例）",
    ]

    # 只读语义不变：诊断任务不许声明改动范围。
    assert task["read_only"] is True
    assert "files_to_modify" not in task

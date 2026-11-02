"""TDD spec — 根因任务的改动范围契约（作者侧 ↔ 读者侧对齐）。

背景
----
根因链的**读者**（``verification.orchestrator._root_cause_scope_from_text``）
只读**声明**：整份报告是 JSON，或者报告里有一个语言标注为 ``json`` 的围栏
块。散文不是声明 —— 一段话、一张表、一串 bullet 都读不出「哪个路径是这次
真要新建的、哪个只是举例」，所以读者不再按行扫「改动范围」小节。读不出
声明就判 ``root_cause_report_without_scope``，自动修复停摆。

而**作者**（``root_cause_task.build_root_cause_task``）必须被告知这个契约：
description 里带着模板（:data:`_SCOPE_CONTRACT_TEMPLATE`），模板给出声明的
**形状**，不给出任何具体路径。

TDD contract — 4 cases
-----------------------
1. ``test_description_carries_scope_contract`` —— description 带声明模板：
   一个 ``json`` 围栏块、``files_to_modify`` 与 ``fix_plan`` 两个键。
2. ``test_heading_matches_reader_constant`` —— 模板标题与
   ``_ROOT_CAUSE_SCOPE_HEADINGS`` 首项逐字对齐（读者不再扫小节，但两边
   仍然叫同一个名字，读报告的人才找得到那一节）。
3. ``test_template_round_trips_through_reader`` —— **回归钉子**：把模板里
   那个 ``json`` 块从**原文**切出来，把占位路径换成一条真路径，喂给
   reader，必须读出那条路径 —— 模板给的形状就是读者读的形状。
4. ``test_deliverable_names_scope`` —— deliverable 里有「改动范围」这一项，
   且任务仍然是只读诊断任务（没有 ``files_to_modify``）。

边界条件
--------
* 模板里**不许出现任何具体路径** —— 读者会把块里的每一个路径都当成白名单，
  一个占位名进去就是一个假白名单项出去。
* 取块必须是**原文**：不许把模板重拼一遍再喂给 reader —— 那样测的是测试
  自己写的字符串，不是任务真正下发给作者的那一段。
* 不改 ``read_only`` 语义，不给诊断任务加 ``files_to_modify``。
"""

from __future__ import annotations

import re
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


#: 模板用的标题 —— 与 reader 的 ``_ROOT_CAUSE_SCOPE_HEADINGS`` 首项一致。
TEMPLATE_HEADING = "改动范围"

#: 回归钉子里用的那条真路径。它不在模板里 —— 模板只给形状。
DECLARED_PATH = "backend/reader.py"


def _json_block(text: str) -> str:
    """从 description **原文**里切出那个 ```json 围栏块。

    切出来的东西必须是原文的子串 —— 重拼一遍就测不到真正的契约了。
    """
    blocks = re.findall(r"```json[ \t]*\n(.*?)```", text, re.DOTALL)
    assert blocks, "description 里没有 ```json 围栏块"
    return blocks[0]


def _declared_paths(block: str) -> list:
    """块里声明的路径 —— 直接问读者，不重拼 JSON。"""
    return _root_cause_scope_from_text(block)


# ---------------------------------------------------------------------------
# 1. description 带声明契约
# ---------------------------------------------------------------------------
def test_description_carries_scope_contract():
    """description 里有 json 围栏块、``files_to_modify`` 与 ``fix_plan``。"""
    task = build_root_cause_task("p1", "VP-3", 2)
    text = task["description"]

    assert "## 改动范围" in text
    block = _json_block(text)
    # JSON 备选：读者也认这个形状（``files_to_modify`` / ``fix_plan``）。
    assert "files_to_modify" in block
    assert "fix_plan" in block

    # 模板里不许出现任何具体路径 —— 读者会把块里的每一个路径都当成白名单。
    declared = _declared_paths(block)
    assert not [path for path in declared if "/" in path], (
        f"模板里不许有具体路径，读者的白名单会照单全收：{declared!r}"
    )


# ---------------------------------------------------------------------------
# 2. 标题与 reader 的常量逐字对齐
# ---------------------------------------------------------------------------
def test_heading_matches_reader_constant():
    """模板标题用的是常量表的首项，两边叫同一个名字。"""
    task = build_root_cause_task("p1", "VP-3", 2)
    text = task["description"]

    heading_line = next(
        line for line in text.splitlines()
        if line.strip().startswith("#") and TEMPLATE_HEADING in line
    )
    # 读法：去掉所有前导 ``#``，再按候选前缀匹配。
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
    """模板原文里的 json 块喂给 reader，读得出声明的路径。"""
    task = build_root_cause_task("p1", "VP-3", 2)
    text = task["description"]

    block = _json_block(text)
    # 只把占位路径换成一条真路径 —— 块的其余部分（键、嵌套、写法）都是
    # 模板原文。模板给的形状必须就是读者读的形状。
    declared = re.sub(
        r'"files_to_modify":\s*\[[^\]]*\]',
        '"files_to_modify": ["%s"]' % DECLARED_PATH,
        block,
    )
    assert _declared_paths(declared) == [DECLARED_PATH]


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

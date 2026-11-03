"""Unit tests for the root-cause scope **reader** — declarations, not prose.

Why this exists
---------------
The scope a root-cause report delivers is the ``files_to_modify`` of the
one implementation task that follows it, and that list is a **write
allow-list**: the executor physically cannot touch a file outside it
(``coding_tool.current_task_allowed_files``). So a scope read wrong is
not a cosmetic problem — it locks the files the fix needs and unlocks
the ones it must not touch.

The reader used to read the scope out of **prose**: every line under a
``## 改动范围`` heading was mined for path-shaped tokens. That cannot be
done safely. Nothing in a sentence says whether the path it names is one
this task will really create or one it mentions as an example, and every
heuristic that tries to tell them apart (the name, whether the file
exists, the extension) flips on some real report. One did: a report
whose scope section is a paragraph of prose, ten bullets and a
two-column table produced a sixteen-entry allow-list that carried a
placeholder file name, a directory, a table fragment and a stray
``2/3``.

So the reader stopped guessing. It reads **declarations** now: the whole
report is JSON, or the report carries a fenced block whose info string is
``json``. Prose is not a declaration and yields no scope, and the
caller's existing ``root_cause_scope_missing`` path hands that report to
a human.

What is pinned here
-------------------
* prose alone → no scope;
* a table alone → no scope;
* prose plus a table plus a ``json`` block → **only** the block's paths;
* a whole report that is JSON → its paths;
* the nested ``fix_plan.files_to_modify`` shape → its paths;
* a fence in another language is not read — the ``json`` one is;
* an empty ``files_to_modify`` → no scope;
* the incident report, verbatim → no scope, and none of the fragments the
  old reader produced.

No network / database — pure in-process unit tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification.orchestrator import (  # noqa: E402
    _root_cause_scope_from_text,
)


#: The incident report, verbatim. A fixture, so the regression does not
#: depend on a plan directory that is not in the repository.
INCIDENT_REPORT = (
    _BACKEND_DIR
    / "tests"
    / "fixtures"
    / "plans"
    / "20261102-orphan_reaper"
    / "reports"
    / "root-cause-VP-018.md"
)


# ---------------------------------------------------------------------------
# 1. Prose alone declares nothing
# ---------------------------------------------------------------------------
def test_prose_only_report_declares_no_scope():
    """一段散文提到一个路径 —— 那不是声明，读者不读它。"""
    text = (
        "# VP-018 根因报告\n"
        "\n"
        "## 改动范围\n"
        "\n"
        "这次修复会改到 `backend/example_scope.py` —— 那是简报里写的占位名，"
        "仓里其实没有这个文件，真正要动的是服务生命周期那一层。\n"
    )

    assert _root_cause_scope_from_text(text) == []


# ---------------------------------------------------------------------------
# 2. A table alone declares nothing
# ---------------------------------------------------------------------------
def test_table_only_report_declares_no_scope():
    """一张两列表格不是声明。"""
    text = (
        "# VP-018 根因报告\n"
        "\n"
        "## 改动范围\n"
        "\n"
        "| 文件 | 承担 |\n"
        "|---|---|\n"
        "| `backend/service_manager.py` | L1 身份闸门 |\n"
        "| `backend/server.py` | L6 detach_owner 改显式开关 |\n"
    )

    assert _root_cause_scope_from_text(text) == []


# ---------------------------------------------------------------------------
# 3. The declaration wins over the prose around it
# ---------------------------------------------------------------------------
def test_json_block_wins_over_prose_and_table():
    """散文 + 表格 + json 块 → 只取 json 块里的路径。"""
    text = (
        "# VP-018 根因报告\n"
        "\n"
        "## 改动范围\n"
        "\n"
        "先说一件事：简报里写的 `backend/example_scope.py` 在仓里不存在。\n"
        "\n"
        "- `backend/service_manager.py`\n"
        "\n"
        "| 文件 | 承担 |\n"
        "|---|---|\n"
        "| `backend/server.py` | L6 |\n"
        "\n"
        "真正的改动面在这里声明：\n"
        "\n"
        "```json\n"
        "{\n"
        '  "files_to_modify": ["backend/service_manager.py", "backend/server.py"],\n'
        '  "fix_plan": "加身份闸门，把 detach_owner 改成显式开关"\n'
        "}\n"
        "```\n"
    )

    assert _root_cause_scope_from_text(text) == [
        "backend/service_manager.py",
        "backend/server.py",
    ]


# ---------------------------------------------------------------------------
# 4. The whole report is JSON
# ---------------------------------------------------------------------------
def test_whole_report_json_is_read():
    """整份报告是 JSON —— 直接读。"""
    text = (
        "{\n"
        '  "plan_id": "p1",\n'
        '  "vp_id": "VP-018",\n'
        '  "files_to_modify": ["backend/service_manager.py", "backend/server.py"],\n'
        '  "fix_plan": "加身份闸门"\n'
        "}\n"
    )

    assert _root_cause_scope_from_text(text) == [
        "backend/service_manager.py",
        "backend/server.py",
    ]


# ---------------------------------------------------------------------------
# 5. The nested shape
# ---------------------------------------------------------------------------
def test_nested_fix_plan_files_to_modify_is_read():
    """``fix_plan.files_to_modify`` 嵌套形态也读。"""
    text = (
        "```json\n"
        "{\n"
        '  "fix_plan": {\n'
        '    "files_to_modify": ["backend/process_identity.py"],\n'
        '    "summary": "复用唯一的身份判定"\n'
        "  }\n"
        "}\n"
        "```\n"
    )

    assert _root_cause_scope_from_text(text) == ["backend/process_identity.py"]


# ---------------------------------------------------------------------------
# 6. Only a ``json`` fence is a declaration
# ---------------------------------------------------------------------------
def test_only_the_json_fence_is_read():
    """第一个围栏是 python、第二个是 json —— 读 json 那个。"""
    text = (
        "# VP-018 根因报告\n"
        "\n"
        "```python\n"
        "import os\n"
        "print(os.getpid())\n"
        "```\n"
        "\n"
        "```json\n"
        "{\n"
        '  "files_to_modify": ["backend/process_manager.py"]\n'
        "}\n"
        "```\n"
    )

    assert _root_cause_scope_from_text(text) == ["backend/process_manager.py"]


# ---------------------------------------------------------------------------
# 7. An empty declaration is no declaration
# ---------------------------------------------------------------------------
def test_empty_files_to_modify_declares_no_scope():
    """``files_to_modify`` 是空数组 → 没有范围。"""
    text = (
        "```json\n"
        "{\n"
        '  "files_to_modify": [],\n'
        '  "fix_plan": "还没想清楚"\n'
        "}\n"
        "```\n"
    )

    assert _root_cause_scope_from_text(text) == []


# ---------------------------------------------------------------------------
# 8. The incident report, verbatim
# ---------------------------------------------------------------------------
def test_incident_report_declares_no_scope():
    """真事故报告全文喂进去 → 没有范围，也没有旧读者读出来的碎片。

    这份报告的「改动范围」小节是一段散文 + 十条 bullet + 一张两列表格，
    散文里还提到了简报里的占位名。旧读者从里面读出了十六项白名单。
    新读者只读声明 —— 报告里没有 ``json`` 围栏块，所以范围是空的，
    链把这份报告交给人。
    """
    text = INCIDENT_REPORT.read_text(encoding="utf-8")

    scope = _root_cause_scope_from_text(text)

    assert scope == []
    # 旧读者读出来的那几样东西，一个都不许再出现。
    assert "backend/example_scope.py" not in scope
    assert "worktrees/reaper-leak" not in scope
    assert "2/3" not in scope
    assert not [path for path in scope if "（" in path or "|" in path]

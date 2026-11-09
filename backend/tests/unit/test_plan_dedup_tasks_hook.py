"""任务列表落盘时写去重报告（2026-11-09, R4 / 任务 8）。

契约来源：R4 要求**任务列表**也检测并写报告。``tasks.json`` 的每次落盘都经过
``TasksGenerator._write_tasks_file()`` —— 那是唯一收口，所以钩子挂在那一处，
而不是散落在各个调用点上。

本文件钉住四道缝：

* **落盘即出报告** —— 任务列表写下去，重复组报告必须跟着出现在计划目录里；
* **tasks.json 逐字节不变** —— 钩子只**多写一份**报告，不动任务列表本身
  （R4 判据：开关关闭时任务列表逐字节不变）；
* **错误落盘不写报告** —— ``payload["error"]`` 存在说明这次落盘是**失败**，
  不是一份任务列表；拿失败载荷去判重没有意义；
* **开关关闭只写 tasks.json** —— ``PDT_PLAN_DEDUP=0`` 时一切照旧。

风格说明：``tasks_generator`` 依赖 ``coding_tool`` 等一堆模块，import 本身有
成本，所以本文件**按需** import（见 :func:`_generator_module`）。顶层 import 在
模块出问题时会变成**收集报错**（exit 2），看不到是哪一条用例红了。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _generator_module():
    """按需 import ``tasks_generator``。

    异常从**用例体内**抛出 → pytest 报**失败**；顶层 import 则是收集报错
    （error），两者在 CI 里的可见度完全不同。
    """
    return importlib.import_module("tasks_generator")


# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------

#: 两条任务改了同一批文件、跑了同一条命令 → 判重成立。
TASK_1 = {
    "id": "1",
    "files_to_modify": ["backend/plan_dedup.py"],
    "test_commands": ["pytest backend/tests/unit/test_计划去重.py -v"],
}

TASK_2 = {
    "id": "2",
    "files_to_modify": ["backend/plan_dedup.py"],
    "test_commands": ["pytest backend/tests/unit/test_计划去重.py -v"],
}

#: 一条干净的任务：文件与命令都独一无二。
TASK_CLEAN = {
    "id": "3",
    "files_to_modify": ["backend/plan_dedup_report.py"],
    "test_commands": ["pytest backend/tests/unit/test_plan_dedup_report.py -v"],
}

#: 报告文件名（任务侧）。
TASKS_REPORT_JSON = "tasks_dedup.json"


def _make_generator(tmp_path: Path):
    """造一个只带落盘所需属性的 ``TasksGenerator``。

    用 ``object.__new__`` 绕开 ``__init__``：``__init__`` 会要求一整套依赖
    （coding_tool 等），而这里要验的只有落盘这一个动作。
    """
    module = _generator_module()
    gen = object.__new__(module.TasksGenerator)
    gen.tasks_file = tmp_path / "tasks.json"
    gen.plan_dir = tmp_path
    gen._defer_tasks_writes = False
    return gen


def _expected_tasks_json(payload) -> str:
    """``_write_tasks_file`` 落盘时用的**同一套**序列化口径。

    钩子只允许多写一份报告，任务列表本身必须逐字节不变 —— 这条断言就是
    "不变"的定义。
    """
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 落盘即出报告
# ---------------------------------------------------------------------------


def test_write_tasks_file_emits_dedup_report(tmp_path):
    """任务列表落盘 → 计划目录里出现 ``tasks_dedup.json``，组为 ``["1", "2"]``。"""
    gen = _make_generator(tmp_path)
    payload = {"tasks": [TASK_1, TASK_2]}

    gen._write_tasks_file(payload)

    report_file = tmp_path / TASKS_REPORT_JSON
    assert report_file.exists(), "落盘后必须出现 tasks_dedup.json"
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["duplicate_groups"] == [
        {
            "reason": (
                "identical_files_and_command: 1 file(s) + "
                "pytest backend/tests/unit/test_计划去重.py -v"
            ),
            "member_ids": ["1", "2"],
        }
    ]


def test_write_tasks_file_leaves_tasks_json_unchanged(tmp_path):
    """钩子只**多写一份**报告 —— tasks.json 的内容逐字节不变。"""
    gen = _make_generator(tmp_path)
    payload = {"tasks": [TASK_1, TASK_2]}

    gen._write_tasks_file(payload)

    assert (tmp_path / "tasks.json").read_text(encoding="utf-8") == (
        _expected_tasks_json(payload)
    )


def test_clean_tasks_write_no_report(tmp_path):
    """没有重复组 → 不写报告（计划目录里凭空多一份空报告没有意义）。"""
    gen = _make_generator(tmp_path)

    gen._write_tasks_file({"tasks": [TASK_CLEAN]})

    assert not (tmp_path / TASKS_REPORT_JSON).exists()
    assert (tmp_path / "tasks.json").exists()


# ---------------------------------------------------------------------------
# 边界载荷
# ---------------------------------------------------------------------------


def test_error_payload_writes_no_report(tmp_path):
    """``payload["error"]`` 存在 → 这是**失败落盘**，不写报告。

    失败载荷里也可能带着任务（端到端门禁不通过时会把原任务一并写进去），
    拿它判重等于把"生成失败了"误报成"任务重复了"。
    """
    gen = _make_generator(tmp_path)
    payload = {
        "error": "tasks_generator: LLM returned 0 tasks.",
        "error_type": "llm_returned_zero_tasks",
        "tasks": [TASK_1, TASK_2],
    }

    gen._write_tasks_file(payload)

    assert not (tmp_path / TASKS_REPORT_JSON).exists()
    # 失败载荷本身照样落盘 —— 钩子不该改变这件事。
    assert (tmp_path / "tasks.json").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"requirement": "只有需求，没有任务"},
        {"tasks": []},
        {"tasks": "not-a-list"},
    ],
    ids=["missing-tasks", "empty-tasks", "tasks-not-a-list"],
)
def test_unusable_payload_writes_no_report(tmp_path, payload):
    """``tasks`` 缺失 / 为空 / 不是 list → 不写报告，也不抛异常。"""
    gen = _make_generator(tmp_path)

    gen._write_tasks_file(payload)

    assert not (tmp_path / TASKS_REPORT_JSON).exists()
    assert (tmp_path / "tasks.json").exists()


def test_deferred_write_emits_no_report(tmp_path):
    """``_defer_tasks_writes`` 为真 → 原函数提前 return，不写报告。"""
    gen = _make_generator(tmp_path)
    gen._defer_tasks_writes = True

    gen._write_tasks_file({"tasks": [TASK_1, TASK_2]})

    assert not (tmp_path / TASKS_REPORT_JSON).exists()
    assert not (tmp_path / "tasks.json").exists()


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------


def test_disabled_switch_writes_tasks_only(tmp_path, monkeypatch):
    """``PDT_PLAN_DEDUP=0`` → 只写 tasks.json，报告不落盘。"""
    monkeypatch.setenv("PDT_PLAN_DEDUP", "0")
    gen = _make_generator(tmp_path)
    payload = {"tasks": [TASK_1, TASK_2]}

    gen._write_tasks_file(payload)

    assert not (tmp_path / TASKS_REPORT_JSON).exists()
    # 任务列表照旧落盘，且逐字节不变。
    assert (tmp_path / "tasks.json").read_text(encoding="utf-8") == (
        _expected_tasks_json(payload)
    )


def test_switch_is_on_by_default(tmp_path, monkeypatch):
    """默认**开启** —— 没设这个变量时，报告照常落盘。"""
    monkeypatch.delenv("PDT_PLAN_DEDUP", raising=False)
    gen = _make_generator(tmp_path)

    gen._write_tasks_file({"tasks": [TASK_1, TASK_2]})

    assert (tmp_path / TASKS_REPORT_JSON).exists()

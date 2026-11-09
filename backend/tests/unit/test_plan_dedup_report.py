"""计划去重报告的落盘与开关（2026-11-09, R3 / R4）。

契约来源：R3 要求把验证点重复检测的结果写成计划目录里的报告（JSON + 一行
摘要）并 WARNING，且提供**默认开启**的开关；R4 要求任务侧也出一份报告。

本文件钉住四道缝：

* **逐字节可复现** —— 同一份输入两次落盘，文件字节必须完全一样。报告是要被
  diff、被审的产物；里面带一个时间戳就等于每次 diff 都脏。
* **干净的计划不落盘** —— 没有重复、没有空泛判据时**不写文件**、返回
  ``None``：计划目录里凭空多一份空报告，会让人以为"查过了，有问题"。
* **开关关闭时不落盘** —— ``PDT_PLAN_DEDUP=0`` 时一切照旧地不写、不报。
* **两侧同一套序列化** —— 任务侧与验证点侧用同一套
  ``sort_keys / indent=2 / ensure_ascii=False`` 口径，两份报告可以直接 diff。

风格说明：本文件**按需** import ``plan_dedup_report``（见 :func:`_report_module`
）。顶层 ``import plan_dedup_report`` 在模块还不存在时会让 pytest 直接
**收集报错**（exit 2），看不到是哪一条用例红了 —— 与本目录其它用例对
``plan_dedup`` 的处理同一套理由（见 ``test_plan_dedup_vague.py`` 的说明）。
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import plan_dedup  # noqa: E402


def _report_module():
    """按需 import ``plan_dedup_report``。

    模块还不存在时，异常从**用例体内**抛出 → pytest 报**失败**；顶层 import
    则是收集报错（error），两者在 CI 里的可见度完全不同。
    """
    return importlib.import_module("plan_dedup_report")


# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------

#: VP-022 与 VP-025 跑的是同一条整仓门禁命令 —— 它们**没有**声明覆盖面，
#: 所以 R5 的例外不触发（空覆盖面不足以支撑任何判定），判重成立。
VP_022 = {
    "id": "VP-022",
    "title": "【全量】Nightly CI",
    "ci_entry": "bash scripts/ci_gate.sh",
    "expected_result": "Nightly CI 整仓门禁退出码 0",
}

#: VP-024 的验收判据是占位词「验证通过」—— 空泛判据。
VP_024 = {
    "id": "VP-024",
    "title": "登录接口",
    "expected_result": "验证通过",
}

VP_025 = {
    "id": "VP-025",
    "title": "【云端】整仓门禁",
    "ci_entry": "bash scripts/ci_gate.sh",
    "expected_result": "云端整仓门禁退出码 0",
}

#: 一条干净的验证点：命令唯一、判据说清了怎么算通过。
VP_CLEAN = {
    "id": "VP-030",
    "title": "单元测试",
    "ci_entry": "pytest backend/tests/unit -v",
    "expected_result": "单元测试全部通过且退出码为 0",
}

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

TASK_CLEAN = {
    "id": "3",
    "files_to_modify": ["backend/plan_dedup_report.py"],
    "test_commands": ["pytest backend/tests/unit/test_plan_dedup_report.py -v"],
}

EXPECTED_SUMMARY = (
    "1 duplicate group(s): VP-022, VP-025; 1 vague criterion(s): VP-024"
)


# ---------------------------------------------------------------------------
# 逐字节可复现
# ---------------------------------------------------------------------------


def test_report_bytes_are_identical_across_two_runs(tmp_path):
    """同一份输入落盘两次，文件必须**逐字节相同**。

    报告要被 diff、被审。里面只要有一个时间戳或随机序，每次 diff 都脏，
    "这次改动引入了新的重复"这句话就再也说不清了。
    """
    module = _report_module()
    plan = {"verification_points": [VP_022, VP_024, VP_025]}
    first = tmp_path / "run-1"
    second = tmp_path / "run-2"

    module.write_verification_dedup_report(plan, first)
    module.write_verification_dedup_report(plan, second)

    for name in ("verification_plan_dedup.json", "verification_plan_dedup.txt"):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_report_contains_no_timestamp(tmp_path):
    """报告里不掺时间戳 —— 逐字节可复现的前提。"""
    module = _report_module()
    plan = {"verification_points": [VP_022, VP_024, VP_025]}

    module.write_verification_dedup_report(plan, tmp_path)

    text = (tmp_path / "verification_plan_dedup.json").read_text(encoding="utf-8")
    assert "generated_at" not in text
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text)


# ---------------------------------------------------------------------------
# 干净的计划不落盘
# ---------------------------------------------------------------------------


def test_clean_plan_writes_no_files(tmp_path):
    """没有重复、没有空泛判据 → **不写文件**、返回 ``None``。

    计划目录里凭空多一份空报告，会让人以为"查过了，有问题"。
    """
    module = _report_module()
    plan = {"verification_points": [VP_CLEAN]}

    assert module.write_verification_dedup_report(plan, tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_clean_tasks_write_no_files(tmp_path):
    """任务侧同理：没有重复组 → 不写文件、返回 ``None``。"""
    module = _report_module()
    tasks = {"tasks": [TASK_CLEAN]}

    assert module.write_tasks_dedup_report(tasks, tmp_path) is None
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------


def test_disabled_switch_writes_nothing(tmp_path, monkeypatch):
    """``PDT_PLAN_DEDUP=0`` → 两侧都不写文件、都返回 ``None``。"""
    module = _report_module()
    monkeypatch.setenv("PDT_PLAN_DEDUP", "0")
    plan = {"verification_points": [VP_022, VP_024, VP_025]}
    tasks = {"tasks": [TASK_1, TASK_2]}

    assert module.write_verification_dedup_report(plan, tmp_path) is None
    assert module.write_tasks_dedup_report(tasks, tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_switch_is_on_by_default(monkeypatch):
    """默认**开启** —— 没设这个变量时，检查照常跑。"""
    module = _report_module()
    monkeypatch.delenv("PDT_PLAN_DEDUP", raising=False)

    assert module.dedup_enabled() is True
    assert module.dedup_enabled({}) is True


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "OFF", "No", " 0 "])
def test_switch_disables_on_documented_off_values(value):
    """``0 / false / off / no`` → 关闭（大小写与首尾空白不敏感）。"""
    module = _report_module()

    assert module.dedup_enabled({"PDT_PLAN_DEDUP": value}) is False


@pytest.mark.parametrize("value", ["1", "true", "on", "ON", "True"])
def test_switch_enables_on_documented_on_values(value):
    """``1 / true / on`` → 开启（大小写不敏感）。"""
    module = _report_module()

    assert module.dedup_enabled({"PDT_PLAN_DEDUP": value}) is True


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def test_tasks_report_uses_the_same_serialisation(tmp_path):
    """任务侧与验证点侧同一套序列化口径。

    固定 ``sort_keys=True, indent=2, ensure_ascii=False`` + 末尾换行：键序固定
    才能 diff，中文不转义才读得懂。
    """
    module = _report_module()
    tasks = {"tasks": [TASK_1, TASK_2]}

    report = module.write_tasks_dedup_report(tasks, tmp_path)
    raw = (tmp_path / "tasks_dedup.json").read_bytes()

    assert raw == (
        json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    # ensure_ascii=False：中文原样落盘，不写成 ``\\uXXXX``。
    assert "计划去重" in raw.decode("utf-8")


def test_verification_report_uses_the_same_serialisation(tmp_path):
    """验证点侧用的是同一套口径 —— 任务侧的断言在这里同样成立。"""
    module = _report_module()
    plan = {"verification_points": [VP_022, VP_024, VP_025]}

    report = module.write_verification_dedup_report(plan, tmp_path)
    raw = (tmp_path / "verification_plan_dedup.json").read_bytes()

    assert raw == (
        json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# 报告内容
# ---------------------------------------------------------------------------


def test_report_lists_duplicate_groups_and_vague_criteria(tmp_path):
    """报告同时装下重复组与空泛判据 —— 两类发现都不能丢。"""
    module = _report_module()
    plan = {"verification_points": [VP_022, VP_024, VP_025]}

    report = module.write_verification_dedup_report(plan, tmp_path)

    assert report["duplicate_groups"] == [
        {
            "reason": "identical_command: bash scripts/ci_gate.sh",
            "member_ids": ["VP-022", "VP-025"],
        }
    ]
    assert report["vague_criteria"] == [
        {
            "id": "VP-024",
            "reason": "vague_criterion: placeholder",
            "expected_result": "验证通过",
        }
    ]
    assert report["summary"] == EXPECTED_SUMMARY


def test_tasks_report_lists_duplicate_groups(tmp_path):
    """任务侧报告装下重复组，摘要说得清是谁跟谁重复。"""
    module = _report_module()
    tasks = {"tasks": [TASK_1, TASK_2]}

    report = module.write_tasks_dedup_report(tasks, tmp_path)

    assert report["duplicate_groups"] == [
        {
            "reason": (
                "identical_files_and_command: 1 file(s) + "
                "pytest backend/tests/unit/test_计划去重.py -v"
            ),
            "member_ids": ["1", "2"],
        }
    ]
    assert report["summary"] == "1 duplicate group(s): 1, 2"


def test_summary_file_holds_exactly_one_line(tmp_path):
    """``verification_plan_dedup.txt`` 是**一行**摘要 + 一个换行。"""
    module = _report_module()
    plan = {"verification_points": [VP_022, VP_024, VP_025]}

    module.write_verification_dedup_report(plan, tmp_path)

    text = (tmp_path / "verification_plan_dedup.txt").read_text(encoding="utf-8")
    assert text == EXPECTED_SUMMARY + "\n"
    assert text.count("\n") == 1


def test_vague_only_plan_still_writes_a_report(tmp_path):
    """只有空泛判据、零重复 → 照样要出报告（空泛判据是独立的一类发现）。"""
    module = _report_module()
    plan = {"verification_points": [VP_024, VP_CLEAN]}

    report = module.write_verification_dedup_report(plan, tmp_path)

    assert report["duplicate_groups"] == []
    assert len(report["vague_criteria"]) == 1
    assert (tmp_path / "verification_plan_dedup.json").exists()
    assert (tmp_path / "verification_plan_dedup.txt").exists()


# ---------------------------------------------------------------------------
# WARNING
# ---------------------------------------------------------------------------


def test_warning_is_emitted_with_summary_and_member_ids(tmp_path, caplog):
    """有发现时打**一条** WARNING，含摘要与成员 id。

    操作员不一定去翻计划目录 —— 日志是唯一一定被看到的地方。
    """
    module = _report_module()
    plan = {"verification_points": [VP_022, VP_024, VP_025]}

    with caplog.at_level(logging.WARNING, logger=module.__name__):
        module.write_verification_dedup_report(plan, tmp_path)

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == module.__name__
    ]
    assert len(messages) == 1
    assert EXPECTED_SUMMARY in messages[0]
    assert "VP-022" in messages[0]
    assert "VP-025" in messages[0]


def test_no_warning_when_nothing_is_found(tmp_path, caplog):
    """没有发现 → 不打 WARNING（否则日志会被"一切正常"刷屏）。"""
    module = _report_module()
    plan = {"verification_points": [VP_CLEAN]}

    with caplog.at_level(logging.WARNING, logger=module.__name__):
        module.write_verification_dedup_report(plan, tmp_path)

    assert [r for r in caplog.records if r.name == module.__name__] == []


# ---------------------------------------------------------------------------
# 纯函数：报告组装（plan_dedup.build_*）
# ---------------------------------------------------------------------------


def test_build_verification_report_is_pure():
    """组装报告不碰文件系统，也不改输入。"""
    points = [VP_022, VP_024, VP_025]
    snapshot = json.dumps(points, sort_keys=True, ensure_ascii=False)

    report = plan_dedup.build_verification_report(points)

    assert report["summary"] == EXPECTED_SUMMARY
    assert json.dumps(points, sort_keys=True, ensure_ascii=False) == snapshot


def test_build_tasks_report_is_pure():
    tasks = [TASK_1, TASK_2]
    snapshot = json.dumps(tasks, sort_keys=True, ensure_ascii=False)

    report = plan_dedup.build_tasks_report(tasks)

    assert report["summary"] == "1 duplicate group(s): 1, 2"
    assert json.dumps(tasks, sort_keys=True, ensure_ascii=False) == snapshot


def test_build_reports_tolerate_empty_input():
    """空输入 → 空报告，摘要为空串（不抛异常）。"""
    assert plan_dedup.build_verification_report([]) == {
        "duplicate_groups": [],
        "summary": "",
        "vague_criteria": [],
    }
    assert plan_dedup.build_tasks_report([]) == {
        "duplicate_groups": [],
        "summary": "",
    }

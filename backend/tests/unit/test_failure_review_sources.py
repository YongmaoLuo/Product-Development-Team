"""failure_review 的来源加载：齐备 / 缺失 / 损坏 / 轮次 / 导入边界。

本文件钉住 R1 的**输入面**。提炼是确定性的（不新增 LLM 调用），所以
它的每一个输入都必须是「读文件就能拿到」的事实；而失败分析是**旁路**
产物 —— 主流程已经到终态了，读不到东西只该让消息少一段，不该把终态
写入变成一次崩溃。

四个必答问题：

* 四份产物齐备时，字段就是解析出来的 dict；
* 任何一份缺失或 JSON 损坏时，对应字段为空，且**不抛异常**；
* ``round`` 的取值顺序是固定的：报告的 ``_metadata.round`` → 计划状态的
  ``verification.round`` → 0；
* 模块顶层不 import ``server`` / ``routes.*`` / ``verification_loop``，
  否则这条旁路链路会被拉回主流程，形成循环依赖。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from failure_review import FailureSources, load_sources

BACKEND_DIR = Path(__file__).resolve().parents[2]
MODULE_PATH = BACKEND_DIR / "failure_review.py"


def _write_json(path: Path, payload) -> None:
    """Write one JSON artefact into the plan directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def test_loads_report_and_repair_tasks(tmp_path):
    """两文件齐备 → 字段是解析后的 dict，其余字段空着。"""
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()
    _write_json(plan_dir / "verification_report.json", {"summary": "验证失败"})
    _write_json(
        plan_dir / "verification_repair_tasks.json",
        {"rounds": [{"round": 2, "tasks": []}]},
    )

    sources = load_sources(plan_dir, stop_reason=None)

    assert sources.report == {"summary": "验证失败"}
    assert sources.repair_tasks == {"rounds": [{"round": 2, "tasks": []}]}
    assert sources.verification_plan is None
    assert sources.stop_reason is None
    assert sources.failed_tasks == []
    assert sources.round == 0


def test_missing_files_are_empty(tmp_path):
    """空目录 → 全空，且不抛异常。"""
    plan_dir = tmp_path / "empty"
    plan_dir.mkdir()

    sources = load_sources(plan_dir)

    assert sources.report is None
    assert sources.verification_plan is None
    assert sources.repair_tasks is None
    assert sources.stop_reason is None
    assert sources.failed_tasks == []
    assert sources.round == 0


def test_broken_json_is_ignored(tmp_path):
    """截断的 JSON → 该字段 None，其它文件照常读出来。"""
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()
    (plan_dir / "verification_report.json").write_text(
        '{"summary": "验证失败"',
        encoding="utf-8",
    )
    _write_json(plan_dir / "verification_repair_tasks.json", {"rounds": []})

    sources = load_sources(plan_dir)

    assert sources.report is None
    assert sources.repair_tasks == {"rounds": []}


def test_missing_plan_dir_yields_empty_sources(tmp_path):
    """plan_dir 根本不存在 → 全部为空，stop_reason 用入参。"""
    sources = load_sources(
        tmp_path / "no-such-plan",
        stop_reason="verification_failed",
    )

    assert sources.report is None
    assert sources.verification_plan is None
    assert sources.repair_tasks is None
    assert sources.stop_reason == "verification_failed"
    assert sources.failed_tasks == []
    assert sources.round == 0


def test_round_prefers_report_metadata(tmp_path):
    """报告的 ``_metadata.round`` 优先于计划状态里的轮次。"""
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()
    _write_json(
        plan_dir / "verification_report.json",
        {"_metadata": {"round": 3}},
    )
    _write_json(
        plan_dir / "plan_state.json",
        {"verification": {"round": 7}},
    )

    sources = load_sources(plan_dir)

    assert sources.round == 3


def test_round_falls_back_to_plan_state(tmp_path):
    """报告没有轮次 → 用计划状态的 ``verification.round``。"""
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()
    _write_json(plan_dir / "verification_report.json", {"summary": "验证失败"})
    _write_json(
        plan_dir / "plan_state.json",
        {"verification": {"round": 7}},
    )

    sources = load_sources(plan_dir)

    assert sources.round == 7


def test_round_is_zero_when_no_source_has_one(tmp_path):
    """两处都没有轮次 → 0。"""
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()
    _write_json(plan_dir / "verification_report.json", {"summary": "验证失败"})
    _write_json(plan_dir / "plan_state.json", {"verification": {"status": "failed"}})

    sources = load_sources(plan_dir)

    assert sources.round == 0


def test_stop_reason_and_failed_tasks_come_from_the_caller(tmp_path):
    """stop_reason 与 failed_tasks 由调用方给，不由文件推导。"""
    plan_dir = tmp_path / "p1"
    plan_dir.mkdir()
    _write_json(
        plan_dir / "verification_report.json",
        {"summary": "验证失败", "final_verdict": "failed"},
    )

    sources = load_sources(
        plan_dir,
        stop_reason="verification_loop_stopped",
        failed_tasks=[{"id": "T1", "title": "修复失败"}],
    )

    assert sources.stop_reason == "verification_loop_stopped"
    assert sources.failed_tasks == [{"id": "T1", "title": "修复失败"}]


def test_sources_is_a_dataclass_with_the_published_fields():
    """字段名与顺序是给后续任务的接口契约。"""
    assert [f.name for f in FailureSources.__dataclass_fields__.values()] == [
        "report",
        "verification_plan",
        "repair_tasks",
        "stop_reason",
        "failed_tasks",
        "round",
    ]


def test_module_does_not_import_server_or_verification_loop():
    """模块顶层不 import 主流程模块（避免循环依赖）。

    用 AST 解析而不是 import —— 回答的是「源码顶层引了什么」，
    而不是「这次会话里 sys.modules 有什么」。
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    forbidden = {"server", "routes", "verification_loop"}
    offenders = {
        name
        for name in imported
        if name.split(".")[0] in forbidden
    }
    assert not offenders, (
        f"failure_review.py 顶层 import 了 {sorted(offenders)} —— "
        "旁路模块不得把主流程拉回来"
    )

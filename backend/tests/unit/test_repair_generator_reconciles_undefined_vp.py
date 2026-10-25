"""没有定义的 VP 不得每轮再生成一条修复任务（记录层的对账）。

背景
====

``20261021-PDT-HITL-1-infra-cri`` 这个计划的 ``verification_plan.json``
里只有 VP-001..VP-030，**没有 VP-031**。round 3 的 delta 分配过这个 id，
定义却没有落盘；那次尝试 8 秒结束，逐字是::

    coding tool call failed: verification cancelled for plan
    '20261021-PDT-HITL-1-infra-cri' while the dispatch was in flight

于是这条记录同时踩中两条判据：

* 该 id **在验证计划里没有定义** —— 判据本身不存在（criterion_defect）；
* 那一次尝试**在分派飞行中被取消** —— 交付物从未被判过（infra）。

上一轮的修复只管到了**分派时**（发号前校验 + ``vp_definition_missing``
的显式映射）。但这条记录根本到不了分派：它每一轮都是从
``verification_failure_history.json`` / ``verification_report.json``
里**原样带出来**的，而失败记录只有 ``round / label / actual_result /
evidence``，没有 ``failure_class`` —— 缺字段时
:func:`verification.failure_history.failure_class_of` 按默认读成
``deliverable_defect``（"计数的那一档"），于是
:func:`repair_generator.select_repairable_failed_vps` 把它放进可修复集合，
每一轮都生成一条"再修一次"的修复任务 —— 而交付物那边没有任何东西可修。

本文件钉住的是**记录层**的三件事：

* 没有定义的 VP 不生成修复任务，出口信息说的是**人要做的动作**
  （作废，或由计划属主重新表达判据），不是"再修一次"；
* 被取消的尝试判 infra（重试的是验证本身，不是重做产物）；
* 真正的断言失败**仍然可修复**（负控：规则不能把正常失败也一起拦掉）。

外加**对账**：判为 criterion_defect / infra 的历史条目在
``verification_failure_history.json`` 里被标上 ``superseded_by``，
**原记录保留**（审计不丢），但不再进入失败集合 —— 同一个 id 的旧记录
不得每轮重新出现。

纯规则测试：不调模型、不起子进程，唯一副作用是 pytest 自己的
``tmp_path``。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


# Ensure backend/ is on sys.path so ``import repair_generator`` works
# regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: 那次尝试的逐字文本（取自 attempt log）。
CANCELLED_TEXT = (
    "执行失败：coding tool call failed: verification cancelled for plan "
    "'20261021-PDT-HITL-1-infra-cri' while the dispatch was in flight。"
    "details 与 raw 均为空，未产生任何判据证据。"
)


def _write_plan(plan_dir: Path, vp_ids) -> Path:
    """一份只列出 ``vp_ids`` 的验证计划。"""
    plan_path = plan_dir / "verification_plan.json"
    plan_path.write_text(
        json.dumps({
            "verification_points": [
                {
                    "id": vp_id,
                    "title": f"{vp_id} 的判据",
                    "verification_method": "pytest",
                    "expected_result": "PASSED",
                }
                for vp_id in vp_ids
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return plan_path


def _write_history(plan_dir: Path, vp_history) -> Path:
    """一份按历史格式写好的失败记录文件。"""
    from verification.failure_history import HISTORY_FILENAME

    history_path = plan_dir / HISTORY_FILENAME
    history_path.write_text(
        json.dumps({
            "version": 1,
            "plan_id": plan_dir.name,
            "vp_history": vp_history,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return history_path


def _read_history(plan_dir: Path) -> dict:
    """文件里**原样**存着的失败记录（审计视角）。"""
    from verification.failure_history import HISTORY_FILENAME

    data = json.loads((plan_dir / HISTORY_FILENAME).read_text(encoding="utf-8"))
    return data.get("vp_history") or {}


# ---------------------------------------------------------------------------
# 没有定义的 VP：不生成修复任务，出口信息说的是人要做的动作
# ---------------------------------------------------------------------------

def test_undefined_vp_gets_no_repair_task_and_names_the_operator_action(tmp_path):
    """计划里没有定义的 VP 不进可修复集合，出口信息写明操作者要做什么。

    这是本文件的正题：``VP-031`` 不在 ``verification_plan.json`` 里，
    它没有判据可核，重做交付物解决不了任何事 —— 要么作废它，要么由
    计划属主重新表达判据。出口信息必须说的是这件事，而不是"再修一次"。
    """
    from repair_generator import select_repairable_failed_vps

    _write_plan(tmp_path, [f"VP-{i:03d}" for i in range(1, 31)])

    repairable, filtered = select_repairable_failed_vps([
        {
            "id": "VP-031",
            "definition_missing": True,
            "failure_class": "criterion_defect",
            "actual_result_summary": CANCELLED_TEXT,
        },
    ])

    assert repairable == [], (
        "没有定义的 VP 不得生成修复任务，交付物那边没有任何东西可修"
    )
    assert [item["id"] for item in filtered] == ["VP-031"]
    reason = filtered[0]["reason"]
    assert "作废" in reason, f"出口信息必须说得出作废这件事：{reason!r}"
    assert "重新表达" in reason, f"出口信息必须说得出重新表达判据：{reason!r}"
    assert "VP-031" in reason, f"出口信息必须点名是哪个 VP：{reason!r}"


def test_definition_missing_is_refused_even_without_a_class(tmp_path):
    """只有 ``definition_missing`` 标记、没有分类的记录，同样被拒绝。

    分类是比标记新的东西：一条老记录可能只有标记没有分类。判据层不能
    依赖"分类一定在" —— 否则缺分类的老记录又会掉回默认分支，重新变成
    修复任务。
    """
    from repair_generator import select_repairable_failed_vps

    repairable, filtered = select_repairable_failed_vps([
        {"id": "VP-031", "definition_missing": True},
    ])

    assert repairable == []
    assert [item["id"] for item in filtered] == ["VP-031"]
    assert filtered[0]["failure_class"] == "criterion_defect"


# ---------------------------------------------------------------------------
# 被取消的尝试：infra（重试的是验证本身，不是重做产物）
# ---------------------------------------------------------------------------

def test_cancelled_attempt_is_classified_infra(tmp_path):
    """分派在飞行中被取消 → infra，不是 deliverable_defect。

    那次尝试**没有判过交付物**：它被取消了。把它算成"产物有问题"会让
    同一件事每轮重试一次，而重试的永远是同一条被取消的记录。
    """
    from verification.failure_history import failure_class_of

    entry = {
        "id": "VP-031",
        "round": 3,
        "actual_result": CANCELLED_TEXT,
        "evidence": "attempt log: attempt_completed verdict=FAILED",
    }

    assert failure_class_of(entry) == "infra", (
        "被取消的尝试必须判 infra（不计数重试），不是默认的 deliverable_defect"
    )


def test_cancelled_attempt_never_becomes_a_repair_task(tmp_path):
    """被取消的尝试不生成修复任务，出口信息说的是"重试验证本身"。"""
    from repair_generator import select_repairable_failed_vps

    repairable, filtered = select_repairable_failed_vps([
        {"id": "VP-031", "actual_result": CANCELLED_TEXT},
    ])

    assert repairable == []
    assert [item["id"] for item in filtered] == ["VP-031"]
    assert filtered[0]["failure_class"] == "infra"


# ---------------------------------------------------------------------------
# 负控：真正的断言失败仍然可修复
# ---------------------------------------------------------------------------

def test_real_assertion_failure_is_still_repairable(tmp_path):
    """计划里有的 VP、真的断言失败 → 仍然是 deliverable_defect，仍然可修。

    这是规则的负控：把"没有定义"和"被取消"拦掉，不能顺手把正常的失败
    也一起拦掉 —— 那样计划就再也不会修任何东西了。
    """
    from repair_generator import select_repairable_failed_vps
    from verification.failure_history import failure_class_of

    defined_ids = {f"VP-{i:03d}" for i in range(1, 31)}
    entry = {
        "id": "VP-007",
        "round": 2,
        "actual_result": "3 failed, 12 passed",
        "evidence": "E   assert 1 == 2",
    }

    assert failure_class_of(entry, defined_vp_ids=defined_ids) == (
        "deliverable_defect"
    )

    repairable, filtered = select_repairable_failed_vps([
        {"id": "VP-007", "failure_class": "deliverable_defect"},
    ])
    assert [vp["id"] for vp in repairable] == ["VP-007"]
    assert filtered == []


# ---------------------------------------------------------------------------
# 对账：旧记录不再复现，原记录仍在文件里
# ---------------------------------------------------------------------------

def test_reconcile_marks_the_record_and_keeps_it_in_the_file(tmp_path):
    """对账：没有定义的记录被标 ``superseded_by``，但**原记录保留**。

    审计不丢：这条记录确实发生过，它是操作者唯一能看到的"这里有个
    没有定义的验证点"的证据。但它不再进入失败集合 —— 同一个 id 的旧
    记录不得每轮重新出现。
    """
    from verification import failure_history as fh

    _write_plan(tmp_path, [f"VP-{i:03d}" for i in range(1, 31)])
    _write_history(tmp_path, {
        "VP-031": [{
            "round": 3,
            "actual_result": CANCELLED_TEXT,
            "evidence": "attempt log 逐字",
        }],
    })

    summary = fh.reconcile_history(tmp_path)

    assert summary["superseded"], "对账必须报告它标记了哪些记录"
    assert summary["superseded"][0]["id"] == "VP-031"

    # 原记录仍在文件里（审计不丢）。
    stored = _read_history(tmp_path)
    assert "VP-031" in stored, "原记录必须保留在文件里"
    assert stored["VP-031"][0]["actual_result"] == CANCELLED_TEXT
    assert stored["VP-031"][0].get("superseded_by"), "记录必须被标上 superseded_by"

    # 但它不再进入失败集合。
    assert "VP-031" not in fh.load(tmp_path), (
        "被对账掉的记录不得再进入失败集合 —— 否则同一个 id 每轮都回来"
    )
    assert "VP-031" in fh.load(tmp_path, include_superseded=True), (
        "审计视角必须仍然看得到这条记录"
    )


def test_reconcile_is_idempotent_and_does_not_resurrect_the_record(tmp_path):
    """对账跑两遍，结论一样；旧记录不会因为又跑一遍就回来。

    同一轮的重跑、resume、重复回调都会再走一遍对账。这条路径必须幂等：
    跑第二遍既不会多出记录，也不会把标记过的记录放回失败集合。
    """
    from verification import failure_history as fh

    _write_plan(tmp_path, [f"VP-{i:03d}" for i in range(1, 31)])
    _write_history(tmp_path, {
        "VP-031": [{
            "round": 3,
            "actual_result": CANCELLED_TEXT,
            "evidence": "attempt log 逐字",
        }],
    })

    first = fh.reconcile_history(tmp_path)
    second = fh.reconcile_history(tmp_path)

    assert [item["id"] for item in first["superseded"]] == ["VP-031"]
    assert second["superseded"] == [], "已经标记过的记录不得再被报告一次"
    assert "VP-031" not in fh.load(tmp_path)
    assert len(_read_history(tmp_path)["VP-031"]) == 1, "不得凭空多出记录"


def test_reconcile_leaves_a_live_failure_alone(tmp_path):
    """负控：真正的失败记录**不**被对账掉。

    对账只动判为 criterion_defect / infra 的记录。一条正常的断言失败
    必须留在失败集合里 —— 否则计划再也不会修任何东西。
    """
    from verification import failure_history as fh

    _write_plan(tmp_path, [f"VP-{i:03d}" for i in range(1, 31)])
    _write_history(tmp_path, {
        "VP-007": [{
            "round": 2,
            "actual_result": "3 failed, 12 passed",
            "evidence": "E   assert 1 == 2",
        }],
    })

    summary = fh.reconcile_history(tmp_path)

    assert summary["superseded"] == []
    assert "VP-007" in fh.load(tmp_path), "正常的失败必须留在失败集合里"
    assert not _read_history(tmp_path)["VP-007"][0].get("superseded_by")


def test_the_reader_marks_the_undefined_vp_and_keeps_the_evidence(tmp_path):
    """报告里的 FAILED 条目：核对它还有没有定义，**记录不删**。

    证据路径必须留着：操作者要看的正是"这条失败到底说了什么"。
    """
    from verification.verification_report_reader import (
        extract_failed_vps_with_paths,
    )

    _write_plan(tmp_path, [f"VP-{i:03d}" for i in range(1, 31)])
    report_path = tmp_path / "verification_report.json"
    report_path.write_text(
        json.dumps({
            "verification_results": [
                {
                    "id": "VP-031",
                    "status": "FAILED",
                    "actual_result": CANCELLED_TEXT,
                    "evidence": "attempt log 逐字",
                },
                {
                    "id": "VP-007",
                    "status": "FAILED",
                    "actual_result": "3 failed, 12 passed",
                    "evidence": "E   assert 1 == 2",
                },
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    failed = extract_failed_vps_with_paths(
        report_path, tmp_path / "verification_plan.json",
    )
    by_id = {entry["id"]: entry for entry in failed}

    assert by_id["VP-031"]["definition_missing"] is True
    assert by_id["VP-031"]["failure_class"] == "criterion_defect"
    assert by_id["VP-031"]["evidence_paths"], "证据路径必须保留"

    assert by_id["VP-007"].get("definition_missing") is not True
    assert by_id["VP-007"]["failure_class"] == "deliverable_defect"


# ---------------------------------------------------------------------------
# 负控：一份**没有定义任何验证点**的计划不是"定义了零个"
# ---------------------------------------------------------------------------

def test_a_plan_that_defines_nothing_does_not_call_a_failure_undefined(tmp_path):
    """``verification_points: []`` 的计划**说不出**"这个点没有定义"。

    规则的方向性：一份没写下任何验证点的计划，对"某个 id 是不是被定义
    过"这个问题**没有发言权**。把它读成"定义了零个"会让它的**每一条**
    失败都变成 criterion_defect —— 而判据缺失是一个要人去处置的具体
    结论，不是默认分支。

    这正是 ``tests/integration/test_fixture_replay.py`` 里那条
    "读者把分类原样带出去" 钉住的行为：一个不带分类的失败仍然读成
    deliverable_defect / defaulted。
    """
    from verification.verification_report_reader import (
        extract_failed_vps_with_paths,
    )

    plan_path = tmp_path / "verification_plan.json"
    plan_path.write_text(
        json.dumps({"verification_points": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    report_path = tmp_path / "verification_report.json"
    report_path.write_text(
        json.dumps({
            "verification_results": [{
                "id": "VP-BARE",
                "status": "FAILED",
                "actual_result": "expected 1, got 2",
                "evidence": "assertion failed",
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    failed = extract_failed_vps_with_paths(report_path, plan_path)

    assert len(failed) == 1
    assert failed[0].get("definition_missing") is not True, (
        "一份没有写下任何验证点的计划没有发言权，不能说这个点没有定义"
    )
    assert failed[0]["failure_class"] == "deliverable_defect", (
        "没有分类、规则也无话可说 → 读成默认那一档"
    )
    assert failed[0]["failure_class_source"] == "defaulted"


def test_a_plan_without_the_key_does_not_call_a_failure_undefined(tmp_path):
    """计划文件里**没有** ``verification_points`` 这个键 → 同样没有发言权。

    缺键和空列表是同一件事的两种写法：这份计划说不出自己定义了哪些
    验证点。把它读成"定义了零个"会把计划写坏时的一次误判，变成一整批
    假的 criterion_defect。
    """
    from verification.verification_report_reader import (
        extract_failed_vps_with_paths,
    )

    plan_path = tmp_path / "verification_plan.json"
    plan_path.write_text(
        json.dumps({"plan_id": "x", "note": "还没写完"}, ensure_ascii=False),
        encoding="utf-8",
    )
    report_path = tmp_path / "verification_report.json"
    report_path.write_text(
        json.dumps({
            "verification_results": [{
                "id": "VP-BARE",
                "status": "FAILED",
                "actual_result": "expected 1, got 2",
                "evidence": "assertion failed",
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    failed = extract_failed_vps_with_paths(report_path, plan_path)

    assert len(failed) == 1
    assert failed[0].get("definition_missing") is not True
    assert failed[0]["failure_class"] == "deliverable_defect"
    assert failed[0]["failure_class_source"] == "defaulted"


def test_the_history_agrees_with_the_reader_about_a_plan_that_defines_nothing(
    tmp_path,
):
    """``plan_defined_vp_ids`` 与 reader 讲同一件事：未知就是未知。

    规则散在两处就会漂：reader 说"这份计划没有定义任何东西"，对账
    却按"它定义了零个"去把每条历史记录都标成 criterion_defect ——
    于是对账替 reader 造出一批假结论。两处必须一致。
    """
    from verification import failure_history as fh

    for payload in ({"verification_points": []}, {"plan_id": "x"}):
        plan_path = tmp_path / "verification_plan.json"
        plan_path.write_text(json.dumps(payload), encoding="utf-8")
        assert fh.plan_defined_vp_ids(tmp_path) is None, (
            f"这份计划 {payload!r} 说不出自己定义了哪些验证点"
        )

    # 真的写下了验证点的计划，才是权威。
    _write_plan(tmp_path, ["VP-001", "VP-002"])
    assert fh.plan_defined_vp_ids(tmp_path) == {"VP-001", "VP-002"}


def test_a_plan_that_defines_nothing_does_not_invent_a_criterion_defect(tmp_path):
    """对账：计划没写下任何验证点 → **不得**凭空造出 criterion_defect。

    对账把记录标成 criterion_defect 是要人处置的结论（"这个点没有判据，
    请作废或重新表达"）。一份没写下任何验证点的计划没有资格下这个结论。

    但"这次尝试被取消了"是**尝试本身**的事实，与计划无关：它照样判
    infra（重试的是验证本身），照样被对账掉。区别只在于类别——
    必须是 infra，不能是那份空计划造出来的 criterion_defect。
    """
    from verification import failure_history as fh

    plan_path = tmp_path / "verification_plan.json"
    plan_path.write_text(
        json.dumps({"verification_points": []}), encoding="utf-8",
    )
    _write_history(tmp_path, {
        "VP-031": [{
            "round": 3,
            "actual_result": CANCELLED_TEXT,
            "evidence": "attempt log 逐字",
        }],
    })

    summary = fh.reconcile_history(tmp_path)

    assert summary["plan_known"] is False, "这份计划说不出自己定义了哪些点"
    assert [item["failure_class"] for item in summary["superseded"]] == [
        "infra"
    ], "空计划不得造出 criterion_defect；被取消的尝试判 infra"
    stored = _read_history(tmp_path)["VP-031"][0]
    assert stored.get("superseded_by") == "infra"
    assert "VP-031" not in fh.load(tmp_path), "被对账掉的记录不再进失败集合"
    assert "VP-031" in fh.load(tmp_path, include_superseded=True), "审计保留"


def test_a_plan_that_defines_nothing_leaves_a_live_failure_alone(tmp_path):
    """对账：计划没写下任何验证点 → 普通的断言失败**不**被对账掉。

    负控。计划读不出自己定义了哪些点时，判据层对"这个点是不是没定义"
    **无话可说**；而一条普通的断言失败没有任何别的规则命中它，所以它
    必须留在失败集合里 —— 否则计划就再也不会修任何东西了。
    """
    from verification import failure_history as fh

    plan_path = tmp_path / "verification_plan.json"
    plan_path.write_text(
        json.dumps({"verification_points": []}), encoding="utf-8",
    )
    _write_history(tmp_path, {
        "VP-007": [{
            "round": 2,
            "actual_result": "3 failed, 12 passed",
            "evidence": "E   assert 1 == 2",
        }],
    })

    summary = fh.reconcile_history(tmp_path)

    assert summary["superseded"] == []
    assert summary["plan_known"] is False
    assert "VP-007" in fh.load(tmp_path), "正常的失败必须留在失败集合里"
    assert not _read_history(tmp_path)["VP-007"][0].get("superseded_by")


# ---------------------------------------------------------------------------
# 循环侧的对账：生成修复任务前先把旧记录对掉
# ---------------------------------------------------------------------------

def test_the_loop_reconciles_the_history_before_it_generates_repair_tasks(
    tmp_path, monkeypatch,
):
    """验证循环在对账之后才建 orchestrator / 生成修复任务。

    对账点有两个（建 orchestrator 之前一轮、每轮生成修复任务之前一轮），
    这里盯的是**它确实会对账**：调用之后，那条没有定义的记录不再进入
    失败集合，而原记录仍然留在文件里。
    """
    import server
    from verification import failure_history as fh
    import verification_loop

    plan_dir = tmp_path / "plan-with-a-hole"
    plan_dir.mkdir()
    _write_plan(plan_dir, [f"VP-{i:03d}" for i in range(1, 31)])
    _write_history(plan_dir, {
        "VP-031": [{
            "round": 3,
            "actual_result": CANCELLED_TEXT,
            "evidence": "attempt log 逐字",
        }],
    })

    monkeypatch.setattr(server, "PLANS_DIR", tmp_path)

    summary = verification_loop._reconcile_failure_history("plan-with-a-hole")

    assert [item["id"] for item in summary.get("superseded") or []] == [
        "VP-031"
    ], "循环必须报告它对掉了哪条记录"
    assert "VP-031" not in fh.load(plan_dir), "对账后不得再进入失败集合"
    assert "VP-031" in fh.load(plan_dir, include_superseded=True), (
        "审计视角必须仍然看得到这条记录"
    )


def test_a_broken_reconciliation_does_not_break_the_round(tmp_path, monkeypatch):
    """对账失败**不是**一轮失败：记录照旧，循环继续。

    对账是补丁，不是判据：它失败时这一轮必须照常跑完（记录按原样留着，
    最多再生成一条老样子的修复任务），而不是把整轮验证带崩。
    """
    from verification import failure_history as fh
    import verification_loop

    def boom(plan_dir, **kwargs):
        raise RuntimeError("reconciliation exploded")

    monkeypatch.setattr(fh, "reconcile_history", boom)

    assert verification_loop._reconcile_failure_history("any-plan") == {}


if __name__ == "__main__":  # pragma: no cover — manual entry point
    raise SystemExit(pytest.main([__file__, "-v"]))

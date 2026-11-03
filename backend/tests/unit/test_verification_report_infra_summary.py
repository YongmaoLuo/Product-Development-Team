"""The report's infra summary (2026-11-03) — 超限的 VP 落成终态，失败按类分开数。

背景
====

账本（Task 1/6）已经能回答"这个 VP 已经因为 infra 失败过几轮"，标记方法
（Task 3）也已经能给报告条目盖上"基础设施未解决"的终态。缺的是把两者**接
起来**，接上之后报告要说出三件事：

  * **谁超限了** —— 账本里 ``is_exhausted`` 为真的那些 VP；
  * **接上之后报告怎么说** —— 这条 VP 不再是 ``FAILED``（那是关于交付物的
    判决），而是 ``BLOCKED`` + ``failure_terminal = infrastructure_unresolved``；
  * **失败按类分开数** —— ``failure_class_counts`` 把 infra 与
    deliverable_defect 分开数，读者不必自己把 FAILED 拆成两类。

arch 决策点 3 是"**基础设施未解决就停，不进迭代**"。本文件只负责把这件事
落到**报告**上（终态 + 计数）：停不停是另一个决定，它有自己封闭的词汇表
（``plan_state`` / ``server.VERIFICATION_TERMINAL_STOP_REASONS``），本任务
一个字也不往那里面加。

边界
====

  * **没有超限的 VP** → 条目一个都不动，但 ``failure_class_counts`` 照样写；
  * **PASSED 的条目不计入任何类** —— 绿的点没有失败可分类（执行器自己的
    投影也把通过点的 ``failure_class`` 清空）；
  * **四个键恒在**（含 0），键序固定：infra / deliverable_defect /
    criterion_defect / out_of_scope；
  * **条目缺类** → 记 ``DEFAULT_FAILURE_CLASS``（deliverable_defect）；
  * **未知类** → 归入 ``deliverable_defect`` —— 不认识的类不许把一条失败弄丢；
  * **账本坏了** → 视作没有超限，不抛 —— 报告才是交付物，账本只是一本辅助账。

接口契约
========

::

    verification_agent.Orchestrator._exhausted_infra_vp_ids(self) -> list[str]
    verification_agent.Orchestrator._failure_class_counts(report_data) -> dict[str, int]

    report_data["failure_class_counts"]: Dict[str, int]
    超限条目携带 failure_terminal == "infrastructure_unresolved"

注意
====

本文件不按名字 import 被测对象之外的任何东西：缺的常量、缺的方法都在调用
时去取，这样这个模块永远能收集起来，每个用例都按名字报出来。收集期的
``ImportError`` 会把整个文件（以及它里面每一个用例名）从报告里拿走，也会
打断 CI 的 unit lane（``pytest tests/unit``）—— 它要收集这个目录。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_agent  # noqa: E402
from verification_agent import Orchestrator  # noqa: E402


#: The failure class of an attempt that malfunctioned — the environment
#: failed, the deliverable was never asked.
INFRA = "infra"

#: The failure class of a deliverable that is genuinely wrong.
DELIVERABLE_DEFECT = "deliverable_defect"

#: The failure class of a criterion (the verification point) that is wrong.
CRITERION_DEFECT = "criterion_defect"

#: The failure class of a check that asks for something out of scope.
OUT_OF_SCOPE = "out_of_scope"

#: The terminal the contract names.
INFRA_UNRESOLVED_TERMINAL = "infrastructure_unresolved"

#: The key order the counts are always written in.
CLASS_ORDER = ["infra", "deliverable_defect", "criterion_defect", "out_of_scope"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubCodingTool:
    """A coding-tool stand-in that never judges anything.

    The tests that need a judgment hand in :class:`_ReportingTool`
    instead; this one only carries the provider chain.
    """

    def __init__(self, provider_priority: Optional[List[str]] = None) -> None:
        self.provider_priority = list(provider_priority or [])

    def query_json(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise AssertionError(
            "this stand-in must not be asked for a judgment here"
        )


class _ReportingTool(_StubCodingTool):
    """A coding-tool stand-in whose judgment answers with a report."""

    def __init__(self, report: Dict[str, Any]) -> None:
        super().__init__(provider_priority=[])
        self.report = report

    def query_json(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return json.loads(json.dumps(self.report))


class _FakePersistence:
    """A persistence stand-in that keeps the report in memory.

    The real ``VerificationPersistenceManager`` writes the report and its
    Markdown view into the plan directory; what these tests are about is
    what the report *says*, so the stand-in keeps the dict and writes
    nothing.
    """

    def __init__(self) -> None:
        self.reports: List[Dict[str, Any]] = []

    def write_verification_point_log(self, *args: Any, **kwargs: Any) -> None:
        return None

    def update_report(self, report_data: Dict[str, Any]) -> None:
        self.reports.append(report_data)


class _BrokenLedger:
    """A ledger stand-in that cannot be read at all.

    The ledger lives on disk next to the plan; a broken file, a
    read-only directory or a corrupt payload must never take the report
    down with it — so this stand-in raises on **every** operation,
    including the one that hands out the file's path.
    """

    @property
    def path(self) -> Path:
        raise OSError("the ledger file is gone")

    def is_exhausted(self, vp_id: str) -> bool:
        raise OSError("the ledger file is gone")

    def record_attempt(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise OSError("the ledger file is gone")


class _UnreadableThresholdLedger:
    """A ledger whose ids can be listed but whose threshold cannot be read."""

    @property
    def path(self) -> Path:
        return Path("/nonexistent/verification_infra_retries.json")

    def is_exhausted(self, vp_id: str) -> bool:
        raise OSError("the ledger file is gone")

    def record_attempt(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise OSError("the ledger file is gone")


def _make_orchestrator(tmp_path: Path, coding_tool: Any = None) -> Orchestrator:
    """An orchestrator over a throwaway plan directory."""
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    orchestrator = Orchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=coding_tool or _StubCodingTool(),
    )
    orchestrator.persistence = _FakePersistence()
    return orchestrator


def _judgment_report(*entries: Dict[str, Any]) -> Dict[str, Any]:
    """The report the judgment LLM hands back."""
    return {
        "overall_status": "FAILED",
        "summary": "the judgment LLM's own words",
        "verification_results": list(entries),
        "requirement_deviations": [],
    }


def _execution_results(*entries: Dict[str, Any]) -> Dict[str, Any]:
    """The executor's results for the round."""
    return {
        "verification_points": [],
        "execution_results": list(entries),
    }


def _infra_execution(vp_id: str = "VP-006") -> Dict[str, Any]:
    """One executor result that failed for infra reasons."""
    return {
        "id": vp_id,
        "status": "FAILED",
        "actual_result": "the dispatch timed out",
        "evidence": json.dumps({
            "vp_id": vp_id,
            "reason_code": "coding_tool_call_failed",
        }),
        "failure_class": INFRA,
    }


def _book_attempts(
    orchestrator: Orchestrator, vp_id: str, rounds: List[int],
) -> None:
    """Book one infra attempt per round in the retry ledger."""
    for round_number in rounds:
        orchestrator._infra_retry_ledger.record_attempt(
            vp_id, round_number, provider="vendor-a-pro",
            reason_code="coding_tool_call_failed",
        )


# ---------------------------------------------------------------------------
# 1. 超限的 VP 落成终态
# ---------------------------------------------------------------------------


def test_exhausted_vp_marked_blocked_in_report(tmp_path):
    """把 infra 的重试预算用完的 VP，在报告里不再是 FAILED。

    任务书给的输入 / 输出示例，一字不差：账本里 ``VP-006`` 已经记了 3 轮
    infra（这一轮记的是第 3 轮），报告里那条 ``VP-006`` 的条目就是
    ``BLOCKED``，并且带着 ``failure_terminal = infrastructure_unresolved``。

    关键在顺序：这一轮先被记进账本（``_record_infra_attempts``），**然后**
    才问"谁用完了预算"。用完预算的正是这一轮，所以问晚了就晚了一轮。
    """
    orchestrator = _make_orchestrator(
        tmp_path,
        coding_tool=_ReportingTool(_judgment_report({
            "id": "VP-006", "status": "FAILED", "actual_result": "timeout",
        })),
    )
    orchestrator._current_round = 3
    _book_attempts(orchestrator, "VP-006", [1, 2])

    report = orchestrator.generate_verification_report(
        _execution_results(_infra_execution()),
    )

    entry = report["verification_results"][0]
    assert entry["status"] == "BLOCKED", report
    assert entry["failure_terminal"] == INFRA_UNRESOLVED_TERMINAL, report
    assert entry["infrastructure_unresolved"] is True, report
    # The class is stated, not inherited: an infra terminal sitting next
    # to a stale ``deliverable_defect`` is exactly the ambiguity this
    # terminal exists to remove.
    assert entry["failure_class"] == INFRA, report


def test_exhausted_vp_is_counted_as_infra(tmp_path):
    """超限的 VP 落在 ``infra`` 这一栏，不落在 ``deliverable_defect``。

    这就是"把 infra 与 deliverable_defect 分开计数"的意义：一个只看报告
    的读者，不必再自己把 FAILED 拆成两类，才知道这一条是环境的锅还是产品
    的锅。
    """
    orchestrator = _make_orchestrator(
        tmp_path,
        coding_tool=_ReportingTool(_judgment_report({
            "id": "VP-006", "status": "FAILED", "actual_result": "timeout",
        })),
    )
    orchestrator._current_round = 3
    _book_attempts(orchestrator, "VP-006", [1, 2])

    report = orchestrator.generate_verification_report(
        _execution_results(_infra_execution()),
    )

    assert report["failure_class_counts"] == {
        "infra": 1,
        "deliverable_defect": 0,
        "criterion_defect": 0,
        "out_of_scope": 0,
    }, report


# ---------------------------------------------------------------------------
# 2. 没超限的 VP 保持原样
# ---------------------------------------------------------------------------


def test_not_exhausted_vp_stays_failed(tmp_path):
    """预算还没用完的 VP 仍然是 FAILED —— 标记器不许提前收网。

    这是控制组：标记器只能动它被告知的那些点。一个还有重试余量的点如果被
    提前标成终态，产品的问题就会被环境的锅盖住，而下一轮根本不会再跑它。
    """
    orchestrator = _make_orchestrator(
        tmp_path,
        coding_tool=_ReportingTool(_judgment_report({
            "id": "VP-006", "status": "FAILED", "actual_result": "timeout",
        })),
    )
    orchestrator._current_round = 2
    _book_attempts(orchestrator, "VP-006", [1])

    report = orchestrator.generate_verification_report(
        _execution_results(_infra_execution()),
    )

    entry = report["verification_results"][0]
    assert entry["status"] == "FAILED", report
    assert "failure_terminal" not in entry, report
    assert "infrastructure_unresolved" not in entry, report


def test_no_exhausted_vp_leaves_every_entry_untouched(tmp_path):
    """没有超限的 VP → 条目一个不动，但计数照样写。

    计数是**每一轮都要有**的东西：读者读的是"这一轮有几条 infra、几条缺陷"，
    而不是"这一轮有没有超限"。没有超限的一轮照样有它自己的计数。
    """
    orchestrator = _make_orchestrator(
        tmp_path,
        coding_tool=_ReportingTool(_judgment_report(
            {"id": "VP-002", "status": "FAILED", "actual_result": "assert"},
            {"id": "VP-001", "status": "PASSED", "actual_result": "ok"},
        )),
    )
    orchestrator._current_round = 1

    report = orchestrator.generate_verification_report(
        _execution_results({
            "id": "VP-002",
            "status": "FAILED",
            "actual_result": "assertion failed",
            "evidence": json.dumps({"reason_code": "assertion failed"}),
            "failure_class": DELIVERABLE_DEFECT,
        }),
    )

    assert report["verification_results"][0]["status"] == "FAILED", report
    assert "failure_terminal" not in report["verification_results"][0], report
    assert report["failure_class_counts"] == {
        "infra": 0,
        "deliverable_defect": 1,
        "criterion_defect": 0,
        "out_of_scope": 0,
    }, report


# ---------------------------------------------------------------------------
# 3. 失败按类分开数
# ---------------------------------------------------------------------------


def test_failure_class_counts_separates_infra_and_defect():
    """四个类各数各的，infra 不混进 deliverable_defect。

    这是本任务真正要交付的那张表：读者一眼看到"这一轮有 1 条是环境的锅、
    2 条是产品的锅"，而不用自己去猜哪条是哪条。
    """
    report = {
        "verification_results": [
            {"id": "VP-001", "status": "FAILED", "failure_class": INFRA},
            {"id": "VP-002", "status": "FAILED", "failure_class": DELIVERABLE_DEFECT},
            {"id": "VP-003", "status": "FAILED", "failure_class": CRITERION_DEFECT},
            {"id": "VP-004", "status": "FAILED", "failure_class": OUT_OF_SCOPE},
        ],
    }

    counts = Orchestrator._failure_class_counts(report)

    assert counts == {
        "infra": 1,
        "deliverable_defect": 1,
        "criterion_defect": 1,
        "out_of_scope": 1,
    }, counts


def test_passed_entries_are_counted_nowhere():
    """PASSED 的条目不计入任何一栏。

    绿的点没有失败可分类：把它算进任何一栏，都会让那一栏说谎。
    """
    report = {
        "verification_results": [
            {"id": "VP-001", "status": "PASSED", "failure_class": INFRA},
            {"id": "VP-002", "status": "PASSED"},
        ],
    }

    counts = Orchestrator._failure_class_counts(report)

    assert counts == {
        "infra": 0,
        "deliverable_defect": 0,
        "criterion_defect": 0,
        "out_of_scope": 0,
    }, counts


def test_an_entry_without_a_class_is_counted_as_the_default():
    """条目缺类 → 记 ``DEFAULT_FAILURE_CLASS``（deliverable_defect）。

    缺类不是"没有失败"：这一轮没能说出它是哪一类，于是按消费方一贯的默认
    归到交付物缺陷 —— 少算一条比算错一条更危险。
    """
    report = {
        "verification_results": [
            {"id": "VP-001", "status": "FAILED"},
        ],
    }

    counts = Orchestrator._failure_class_counts(report)

    assert counts["deliverable_defect"] == 1, counts


def test_an_unknown_class_is_counted_as_a_deliverable_defect():
    """不认识的类 → 归入 deliverable_defect。

    一条失败不许因为它的类名眼生就凭空消失：不认识就按缺陷算，总比把它
    弄丢了强。
    """
    report = {
        "verification_results": [
            {"id": "VP-001", "status": "FAILED", "failure_class": "cosmic_ray"},
        ],
    }

    counts = Orchestrator._failure_class_counts(report)

    assert counts == {
        "infra": 0,
        "deliverable_defect": 1,
        "criterion_defect": 0,
        "out_of_scope": 0,
    }, counts


# ---------------------------------------------------------------------------
# 4. 四个键恒在
# ---------------------------------------------------------------------------


def test_counts_always_contain_zero_keys():
    """没有条目也要有完整的四个键（含 0），键序固定。

    读者读的是 ``counts["infra"]``，不是一个可能缺键的字典：缺键会让
    "这一轮没有 infra 失败"和"这一轮没写计数"看起来一模一样。
    """
    counts = Orchestrator._failure_class_counts({"verification_results": []})

    assert counts == {
        "infra": 0,
        "deliverable_defect": 0,
        "criterion_defect": 0,
        "out_of_scope": 0,
    }, counts
    assert list(counts.keys()) == CLASS_ORDER, counts


def test_counts_of_a_report_without_results_are_still_complete():
    """报告连 ``verification_results`` 都没有 → 四个零。

    计数不能因为报告的形状不对就缺席：它恒在，是这张表唯一的承诺。
    """
    counts = Orchestrator._failure_class_counts({"overall_status": "PASSED"})

    assert counts == {
        "infra": 0,
        "deliverable_defect": 0,
        "criterion_defect": 0,
        "out_of_scope": 0,
    }, counts
    assert list(counts.keys()) == CLASS_ORDER, counts


# ---------------------------------------------------------------------------
# 5. 账本坏了 → 视作没有超限
# ---------------------------------------------------------------------------


def test_ledger_error_yields_no_marks(tmp_path):
    """账本读不出来 → 一个标记都不盖。

    账本是一本辅助账，报告才是交付物。用一本读不出来的账去盖"基础设施未
    解决"的终态，等于凭一本坏账宣布这个点没被判过 —— 而它明明被判过，
    而且被判成了失败。
    """
    orchestrator = _make_orchestrator(
        tmp_path,
        coding_tool=_ReportingTool(_judgment_report({
            "id": "VP-006", "status": "FAILED", "actual_result": "timeout",
        })),
    )
    orchestrator._current_round = 3
    orchestrator._infra_retry_ledger = _BrokenLedger()

    # Neither call may raise.
    assert orchestrator._exhausted_infra_vp_ids() == []

    report = orchestrator.generate_verification_report(
        _execution_results(_infra_execution()),
    )

    entry = report["verification_results"][0]
    assert entry["status"] == "FAILED", report
    assert "failure_terminal" not in entry, report


def test_an_unreadable_threshold_yields_no_marks(tmp_path):
    """账本能列出 VP，但"超限没有"读不出来 → 同样不盖标记。"""
    orchestrator = _make_orchestrator(tmp_path)
    orchestrator._infra_retry_ledger = _UnreadableThresholdLedger()

    assert orchestrator._exhausted_infra_vp_ids() == []


def test_a_missing_ledger_yields_no_marks(tmp_path):
    """账本文件还不存在 → 没有超限的 VP。

    第一轮就是这样：账本还没写过一个字。没有记录就是没有超限，不是出错。
    """
    orchestrator = _make_orchestrator(tmp_path)

    assert orchestrator._exhausted_infra_vp_ids() == []


# ---------------------------------------------------------------------------
# 6. 账本问出来的名单
# ---------------------------------------------------------------------------


def test_exhausted_vp_ids_lists_only_the_exhausted_ones(tmp_path):
    """名单里只有真的用完预算的 VP。

    预算是 ``1 + MAX_INFRA_CROSS_ROUND_RETRIES``（默认 3 次尝试）：记了 2
    次还有余量，记了 3 次才叫用完。
    """
    orchestrator = _make_orchestrator(tmp_path)
    _book_attempts(orchestrator, "VP-006", [1, 2, 3])
    _book_attempts(orchestrator, "VP-002", [1, 2])

    assert orchestrator._exhausted_infra_vp_ids() == ["VP-006"]


# ---------------------------------------------------------------------------
# 7. 计数写在报告里
# ---------------------------------------------------------------------------


def test_the_report_carries_the_counts(tmp_path):
    """``failure_class_counts`` 写在报告的顶层，读者直接读得到。"""
    orchestrator = _make_orchestrator(
        tmp_path,
        coding_tool=_ReportingTool(_judgment_report(
            {"id": "VP-001", "status": "PASSED", "actual_result": "ok"},
        )),
    )
    orchestrator._current_round = 1

    report = orchestrator.generate_verification_report(
        _execution_results(),
    )

    assert report["failure_class_counts"] == {
        "infra": 0,
        "deliverable_defect": 0,
        "criterion_defect": 0,
        "out_of_scope": 0,
    }, report
    assert list(report["failure_class_counts"].keys()) == CLASS_ORDER, report

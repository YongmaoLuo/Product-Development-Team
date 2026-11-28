"""The spawn gate — only a blocking, reproduced framework defect becomes a
repair plan.

What this file pins
-------------------
* ``spawn`` refuses a non-blocking hit.  A flake, a failure that does not
  reproduce on clean main, an unclassified reproduction and a
  non-acceptance-critical hit all leave the plans directory **empty** — the
  gate is the only thing standing between "a test flaked once" and "a
  repair plan for the framework";
* the refusal is **recorded**, not swallowed: the hit reaches the registry
  with the disposition that says what was done (``register_flake`` /
  ``local_continue`` / ``register_signature``) and with the **real**
  ``blocking`` value rather than a constant;
* a blocking, reproduced defect still creates exactly one plan, and a
  second hit on the same signature attaches to that plan instead of
  creating a second one;
* three consecutive unclassified rounds ask a human instead of guessing.

Why these tests are *integration* tests
---------------------------------------
The gate spans three stores: the plans directory (does a plan exist?), the
state database (what was recorded about the hit?) and the hitl decision
table (was a human asked?).  A double for any of them would test the gate
against a world nobody runs.  Every test gets the shared ``state_db``
fixture — a migrated file inside that test's own ``tmp_path`` — and the
autouse ``isolated_plans_dir`` fixture redirects the plans root the same
way, so a spawned plan never lands in the repository's ``plans/``.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from framework_fix.classifier import FailureHit
from framework_fix.fingerprint import compute_fingerprint
from framework_fix.repro import ReproAttempt, ReproductionResult
from framework_fix.spawner import build_envelope, dispatch_hit, spawn
from repair_generator import RepairTaskGenerator


# ---------------------------------------------------------------------------
# 材料
# ---------------------------------------------------------------------------
def _hit(**overrides: Any) -> FailureHit:
    """一次验收关键的命中 —— 判定公式的输入事实。"""
    hit = FailureHit(
        vp_id="VP-3",
        plan_id="plan-a",
        error_class="AssertionError",
        first_error_line="AssertionError: expected 1, got 2",
        framework_files=["backend/framework_fix/registry.py"],
        acceptance_critical=True,
        out_of_scope_change=False,
    )
    return dataclasses.replace(hit, **overrides) if overrides else hit


def _repro(classification: str) -> ReproductionResult:
    """一份复现结论 —— 分类是它唯一被读的那部分。"""
    attempts = (
        ReproAttempt(
            run=1,
            exit_code=1,
            timed_out=False,
            first_error_line="AssertionError: expected 1, got 2",
            duration_s=1.0,
        ),
        ReproAttempt(
            run=2,
            exit_code=1,
            timed_out=False,
            first_error_line="AssertionError: expected 1, got 2",
            duration_s=1.0,
        ),
    )
    return ReproductionResult(
        classification=classification,
        attempts=attempts,
        clean_commit="a" * 40,
        tree_fingerprint="sha256:" + "b" * 64,
    )


def _envelope(hit: FailureHit, classification: str) -> Dict[str, Any]:
    """信封 —— 指纹由签名材料算出，与信封里的那个一致。"""
    return build_envelope(
        fingerprint=compute_fingerprint(
            framework_files=list(hit.framework_files),
            error_class=hit.error_class,
            first_error_line=hit.first_error_line,
        ),
        hit=hit,
        repro=_repro(classification),
        repo="repo-a",
    )


def _plans_root() -> Path:
    """这次测试写进去的计划根目录。"""
    from config_paths import resolve_plans_dir

    return resolve_plans_dir()


def _plan_dirs() -> List[str]:
    """计划根目录下的计划名 —— 空列表就是「没有计划」。"""
    root = _plans_root()
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir())


def _framework_fix_plan_dirs() -> List[str]:
    """计划根目录下的**框架修复**计划。

    问过人的那条计划也会在计划根目录下留下一个目录（HITL 的审计镜像写在
    计划目录里），所以「有没有建修复计划」不能只看目录在不在 —— 要看它
    是不是一个框架修复计划。
    """
    root = _plans_root()
    if not root.is_dir():
        return []
    found: List[str] = []
    for path in sorted(root.iterdir()):
        envelope = path / "framework_fix" / "envelope.json"
        if not envelope.is_file():
            continue
        try:
            payload = json.loads(envelope.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(payload.get("kind") or "") == "framework_fix":
            found.append(path.name)
    return found


def _hit_rows(state_db: Path) -> List[Dict[str, Any]]:
    """登记表里的命中行，最早的在前。"""
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM framework_fix_hit ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _decisions(state_db: Path) -> List[Dict[str, Any]]:
    """问过人的那些问题。"""
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM plan_decisions ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 闸门
# ---------------------------------------------------------------------------
def test_a_flake_creates_no_repair_plan(state_db: Path) -> None:
    """一次 flake 不是框架缺陷 —— 它不建修复计划。"""
    plan_id = spawn(_envelope(_hit(), "flake"))

    assert plan_id == "", "a flake must not name a repair plan"
    assert _plan_dirs() == [], "a flake must leave the plans directory empty"


def test_a_failure_that_does_not_reproduce_creates_no_repair_plan(
    state_db: Path,
) -> None:
    """干净 main 上跑不出来 —— 只在 plan 分支复现，不是框架缺陷。"""
    plan_id = spawn(_envelope(_hit(), "not_reproducible"))

    assert plan_id == ""
    assert _plan_dirs() == []


def test_an_unclassified_reproduction_creates_no_repair_plan(
    state_db: Path,
) -> None:
    """归不了类 —— 证据不全，本轮不建计划。"""
    plan_id = spawn(_envelope(_hit(), "unclassified"))

    assert plan_id == ""
    assert _plan_dirs() == []


def test_a_non_acceptance_critical_hit_creates_no_repair_plan(
    state_db: Path,
) -> None:
    """不是验收关键的命中压过一切 —— 复现得再稳也不阻塞。"""
    plan_id = spawn(
        _envelope(_hit(acceptance_critical=False), "framework_deterministic")
    )

    assert plan_id == ""
    assert _plan_dirs() == []


def test_a_blocking_reproduced_defect_creates_a_repair_plan(
    state_db: Path,
) -> None:
    """验收关键 + 干净 main 上复现 —— 建计划。"""
    plan_id = spawn(_envelope(_hit(), "framework_deterministic"))

    assert plan_id, "a blocking reproduced defect must name a repair plan"
    assert _plan_dirs() == [plan_id]


# ---------------------------------------------------------------------------
# 登记 —— 写的是判定的事实，不是常量
# ---------------------------------------------------------------------------
def test_a_flake_is_registered_as_a_flake(state_db: Path) -> None:
    """flake 的登记写 ``register_flake``，且 ``blocking`` 是假的。"""
    spawn(_envelope(_hit(), "flake"))

    rows = _hit_rows(state_db)
    assert len(rows) == 1, f"expected exactly one registered hit: {rows!r}"
    assert rows[0]["disposition"] == "register_flake"
    assert rows[0]["blocking"] == 0
    assert rows[0]["classification"] == "flake"


def test_a_failure_that_does_not_reproduce_is_registered_as_local_continue(
    state_db: Path,
) -> None:
    """not_reproducible 的登记写 ``local_continue``。"""
    spawn(_envelope(_hit(), "not_reproducible"))

    rows = _hit_rows(state_db)
    assert len(rows) == 1
    assert rows[0]["disposition"] == "local_continue"
    assert rows[0]["blocking"] == 0


def test_a_blocking_hit_is_registered_as_a_fix_plan(state_db: Path) -> None:
    """建了计划的命中写 ``fix_plan``，且 ``blocking`` 是真的。"""
    spawn(_envelope(_hit(), "framework_deterministic"))

    rows = _hit_rows(state_db)
    assert len(rows) == 1
    assert rows[0]["disposition"] == "fix_plan"
    assert rows[0]["blocking"] == 1


# ---------------------------------------------------------------------------
# 一个签名一个计划
# ---------------------------------------------------------------------------
def test_the_same_signature_creates_one_plan(state_db: Path) -> None:
    """第二次命中走既有计划 —— 不产生第二个。"""
    first = spawn(_envelope(_hit(), "framework_deterministic"))
    second = spawn(_envelope(_hit(), "framework_deterministic"))

    assert first == second
    assert _plan_dirs() == [first]


# ---------------------------------------------------------------------------
# 判定入口
# ---------------------------------------------------------------------------
def test_the_dispatch_registers_a_flake_without_a_plan(state_db: Path) -> None:
    """判定入口对 flake 的答复：登记签名，不建计划。"""
    outcome = dispatch_hit(hit=_hit(), reproduction=_repro("flake"))

    assert outcome["action"] == "not_blocking"
    assert outcome["blocking"] is False
    assert outcome["disposition"] == "register_flake"
    assert outcome["fix_plan_id"] == ""
    assert _plan_dirs() == []


def test_the_dispatch_creates_one_plan_for_a_blocking_defect(
    state_db: Path,
) -> None:
    """判定入口对阻塞命中的答复：建计划。"""
    outcome = dispatch_hit(
        hit=_hit(), reproduction=_repro("framework_deterministic")
    )

    assert outcome["action"] == "spawned"
    assert outcome["blocking"] is True
    assert outcome["disposition"] == "fix_plan"
    assert outcome["fix_plan_id"]
    assert _plan_dirs() == [outcome["fix_plan_id"]]


def test_the_dispatch_attaches_the_second_hit(state_db: Path) -> None:
    """同一签名的第二次命中挂到既有计划上。"""
    first = dispatch_hit(
        hit=_hit(), reproduction=_repro("framework_deterministic")
    )
    second = dispatch_hit(
        hit=_hit(), reproduction=_repro("framework_deterministic")
    )

    assert second["fix_plan_id"] == first["fix_plan_id"]
    assert _plan_dirs() == [first["fix_plan_id"]]


def test_the_dispatch_without_a_reproduction_creates_no_plan(
    state_db: Path,
) -> None:
    """没有复现结论 —— 本轮判不了，不建计划。"""
    outcome = dispatch_hit(hit=_hit(), reproduction=None)

    assert outcome["action"] == "needs_repro"
    assert outcome["blocking"] is False
    assert outcome["fix_plan_id"] == ""
    assert _plan_dirs() == []


def test_the_third_unclassified_round_asks_a_human(state_db: Path) -> None:
    """连续三轮归不了类 —— 升级 HITL，而不是继续猜。"""
    for _ in range(2):
        outcome = dispatch_hit(hit=_hit(), reproduction=_repro("unclassified"))
        assert outcome["action"] == "registered", outcome
        assert outcome["blocking"] is False
    assert _framework_fix_plan_dirs() == []

    outcome = dispatch_hit(hit=_hit(), reproduction=_repro("unclassified"))

    assert outcome["action"] == "hitl"
    assert outcome["blocking"] is False
    assert _framework_fix_plan_dirs() == []
    decisions = _decisions(state_db)
    assert len(decisions) == 1, f"expected exactly one question: {decisions!r}"
    assert decisions[0]["failure_class"] == "framework_fix_unclassified"


# ---------------------------------------------------------------------------
# 生产接线 —— 修复轮把失败交给判定入口
# ---------------------------------------------------------------------------
class _RecordingCodingTool:
    """一个只回答「放行」的 coding tool。

    它回答的是**判定入口放行**的那些失败：每一次调用记一笔，答一行够
    解析器收下的内容。它记下的笔数证明的是「这次失败有没有走到修复模型
    面前」—— 被框架修复车道接管的失败，一次都到不了。
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def query(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the repair path must not ask for plain text here")

    def query_json(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"prompt": str(kwargs.get("prompt") or "")})
        return {
            "tasks": [
                {
                    "failed_vp_id": "VP-3",
                    "title": "repair the deliverable",
                    "description": "the failure was released to the local repair",
                    "acceptance_criteria": "the acceptance command passes",
                    "files_to_modify": ["backend/app.py"],
                }
            ]
        }


def _generator(tmp_path: Path, tool: Any) -> RepairTaskGenerator:
    """一台修复生成器，指向它自己的计划目录。

    计划目录在计划根目录底下 —— 修复计划建出来时也落在那里，所以
    「建没建计划」这件事在同一棵树上看得见。
    """
    plan_dir = tmp_path / "plans" / "plan-a"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return RepairTaskGenerator(tool, plan_dir, tmp_path)


def _failure_record(**overrides: Any) -> Dict[str, Any]:
    """一条带着指纹材料的失败记录。

    指纹的原料是三样：框架文件集、错误类别、首错行。生产里失败记录不带
    这三样时，材料来自注册表里那一行 —— 那个指纹已经登记过了。
    """
    record: Dict[str, Any] = {
        "id": "VP-3",
        "title": "the acceptance command passes",
        "priority": "high",
        "framework_files": ["backend/framework_fix/registry.py"],
        "error_class": "AssertionError",
        "first_error_line": "AssertionError: expected 1, got 2",
    }
    record.update(overrides)
    return record


def _fingerprint_of(record: Dict[str, Any]) -> str:
    """那条失败记录的指纹。"""
    return compute_fingerprint(
        framework_files=list(record["framework_files"]),
        error_class=str(record["error_class"]),
        first_error_line=str(record["first_error_line"]),
    )


def _store_reproduction(
    state_db: Path, fingerprint: str, classification: str,
) -> None:
    """把一份复现结论写进复现队列。

    写的就是复现协议写的那一列（``result_json``），键是那次复现对应的
    缺陷指纹。没有它，判定入口读到的是「还没复现」—— 本轮判不了。
    """
    conn = sqlite3.connect(str(state_db))
    try:
        conn.execute(
            "INSERT INTO framework_fix_repro ("
            "  repro_id, repo, plan_id, failure_ref, base_ref, status,"
            "  request_json, result_json, queued_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"repro-{fingerprint[-8:]}",
                "repo-a",
                "plan-a",
                fingerprint,
                "main",
                "done",
                json.dumps({}),
                json.dumps(_repro(classification).to_dict()),
                "2026-11-27T00:00:00.000000+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_the_repair_round_creates_one_fix_plan_for_a_blocking_defect(
    state_db: Path, tmp_path: Path,
) -> None:
    """修复轮里的阻塞命中 → 恰好一个修复计划，本计划不再自己修。"""
    tool = _RecordingCodingTool()
    generator = _generator(tmp_path, tool)
    record = _failure_record()
    _store_reproduction(
        state_db, _fingerprint_of(record), "framework_deterministic",
    )

    result = generator.generate(
        plan_id="plan-a", failures=[record], round_number=1,
    )

    assert len(_framework_fix_plan_dirs()) == 1, (
        "验收关键且在干净 main 上复现的框架缺陷必须建一个修复计划；"
        f"got {_framework_fix_plan_dirs()!r}"
    )
    assert result["skipped_reason"] == "blocked_on_framework_fix", (
        "缺陷归框架修复车道了，本计划就不该再为它生成本地修复任务"
    )
    assert tool.calls == [], (
        "被框架修复车道接管的失败，一次都到不了修复模型面前"
    )


def test_the_repair_round_creates_no_fix_plan_for_a_flake(
    state_db: Path, tmp_path: Path,
) -> None:
    """修复轮里的 flake → 不建计划，本计划自己修（本地继续）。"""
    tool = _RecordingCodingTool()
    generator = _generator(tmp_path, tool)
    record = _failure_record()
    _store_reproduction(state_db, _fingerprint_of(record), "flake")

    result = generator.generate(
        plan_id="plan-a", failures=[record], round_number=1,
    )

    assert _framework_fix_plan_dirs() == [], (
        "一次 flake 不该换来一个框架修复计划；"
        f"got {_framework_fix_plan_dirs()!r}"
    )
    assert result["skipped_reason"] is None, (
        "flake 不是框架缺陷 —— 本计划继续自己修"
    )
    assert tool.calls, (
        "flake 要走到修复模型面前：它不是框架缺陷，本计划自己修"
    )


def test_the_repair_round_asks_a_human_after_three_unclassified_rounds(
    state_db: Path, tmp_path: Path,
) -> None:
    """修复轮里连续三轮归不了类 —— 升级 HITL，而不是继续猜。"""
    tool = _RecordingCodingTool()
    generator = _generator(tmp_path, tool)
    record = _failure_record()
    _store_reproduction(state_db, _fingerprint_of(record), "unclassified")

    for round_no in (1, 2):
        generator.generate(
            plan_id="plan-a", failures=[record], round_number=round_no,
        )
        assert _decisions(state_db) == [], (
            "还没到第三轮，不该问人"
        )

    generator.generate(
        plan_id="plan-a", failures=[record], round_number=3,
    )

    decisions = _decisions(state_db)
    assert len(decisions) == 1, f"expected exactly one question: {decisions!r}"
    assert decisions[0]["failure_class"] == "framework_fix_unclassified"
    assert _framework_fix_plan_dirs() == [], (
        "归不了类的那几轮不建计划"
    )

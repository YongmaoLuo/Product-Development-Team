"""TDD spec —— 根因升级链路的**任务流**（根因任务 → 唯一实现任务 → 转人工）。

背景
----
失败指纹（任务 13）回答了「根因有没有演进」，根因任务与实现任务的构建器
（任务 14）回答了「演进之后该派什么」。缺的那一环是**按轮次串起来**：

    第 1 轮  失败（指纹 aaa）  →  一个普通修复任务
    第 2 轮  失败（指纹 bbb）  →  根因任务（**不是**第三个修复任务）
    第 3 轮  根因报告已交付    →  一个实现任务（带改动范围）
    第 4 轮  实现任务再失败    →  转人工（不再派发新一轮）

四份契约
--------
1. ``test_root_cause_replaces_third_repair`` —— 根因演进的那一轮，任务集合
   **恰为** ``{"root_cause"}``：没有第三个盲修复。
2. ``test_single_implementation_after_diagnosis`` —— 诊断交付之后，实现任务
   **恰 1 个**，且 ``files_to_modify`` 非空。
3. ``test_impl_failure_blocks_on_user`` —— 实现任务再失败，状态是
   ``BLOCKED_ON_USER``，且**没有新任务**。
4. ``test_max_rounds_semantics_unchanged`` —— 既有的 ``max_rounds`` 判定
   一个字符未改：既有测试本体原样跑过（零修改）。

轮次序列
--------
整条链路由 :func:`run_orchestrator_rounds` 驱动 —— 一串轮次依次跑过去，
每轮一个全新的 orchestrator（生产就是这样跑的：自动循环每修完一次就重建
orchestrator，所以一轮记得的东西必须能从磁盘上回来）::

    rounds = [
        ("fail", "aaa"),          # 第 1 轮：失败 → 一个普通修复任务
        ("fail", "bbb"),          # 第 2 轮：指纹变了 → 根因任务（无第三个 repair）
        ("root_cause_done", "bbb"),  # 第 3 轮：诊断交付 → 1 个 implementation
        ("fail", "bbb"),          # 第 4 轮：实现再失败 → BLOCKED_ON_USER
    ]
    results = run_orchestrator_rounds(rounds, plan_dir, project_dir)

边界条件
--------
* 同一指纹重复仍走 ``_same_failure_counts`` 既有路径（负控）。
* ``max_rounds`` 与 ``loop_stopped`` 判定一个字符不改。
* implementation 的 ``files_to_modify`` 非空。
* 失败后不再自动派发新一轮。

测试命令::

    pytest backend/tests/integration/verification_chain/test_root_cause_task_flow.py -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# ``backend/`` 进 sys.path，``import verification.*`` 才成立 —— 与仓库里
# 其它测试文件同一套做法，从哪个目录起 pytest 都一样。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification.orchestrator import VerificationOrchestrator  # noqa: E402


pytestmark = pytest.mark.integration


#: The point the rounds below fail on.
VP_ID = "VP-3"

#: The failure class the rounds carry. It counts (it is not ``infra`` /
#: ``out_of_scope``), so the old key's counter moves.
FAILURE_CLASS = "deliverable_defect"

#: The decision key the old key counts under.
DECISION_KEY = f"{VP_ID}|{FAILURE_CLASS}"

#: The chain's record — the file that says where the escalation stands.
CHAIN_FILENAME = "verification_root_cause_chain.json"

#: The stage the chain reaches when a human has to take over.
CHAIN_STAGE_BLOCKED_ON_USER = "BLOCKED_ON_USER"

#: The file the root cause report lands in (the report the diagnosis
#: task delivers, and the implementation task reads).
ROOT_CAUSE_REPORT_NAME = f"root-cause-{VP_ID}.md"

#: The scope the root cause report delivers — the implementation task's
#: ``files_to_modify`` comes from exactly these paths.
REPORT_SCOPE = ["backend/app/service.py", "backend/app/handler.py"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path):
    plan = tmp_path / "plans" / "root-cause-task-flow"
    plan.mkdir(parents=True, exist_ok=True)
    return plan


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "projects" / "root-cause-task-flow"
    project.mkdir(parents=True, exist_ok=True)
    return project


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Keep the orchestrator's state machine out of the real ``state.db``."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def repair_mocks():
    """The repair path, mocked so the round's rows are observable.

    The generator **echoes** the candidates it was handed, so what the
    round decided about a row (its title, its description) is what the
    caller can read back. The assembler is the real one — the chain's
    tasks do not go through it (they carry their own ids and their own
    ``kind``), and the plain repair rows must look exactly like the ones
    production builds.
    """
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator") as mock_generator:
        mock_generator.return_value.generate_repair_contents.side_effect = (
            lambda candidates, *args, **kwargs: [
                {
                    "failed_vp_id": str(candidate.get("id")),
                    "title": f"fix {candidate.get('id')}",
                    "description": f"fix {candidate.get('id')}",
                    "files_to_modify": ["src/app.py"],
                    "test_command": "pytest -q",
                }
                for candidate in candidates
            ]
        )
        yield mock_generator


# ---------------------------------------------------------------------------
# The scenario on disk
# ---------------------------------------------------------------------------


def _write_plan_state(plan_dir, phase="executing", verification_round=0,
                      max_rounds=3):
    """Bootstrap the plan state the orchestrator reads on construction."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": None,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state), encoding="utf-8",
    )


def _write_plan(plan_dir):
    """The verification plan — one point, the one the rounds fail on."""
    payload = {
        "plan_id": plan_dir.name,
        "verification_points": [
            {
                "id": VP_ID,
                "title": "the endpoint returns 200",
                "verification_method": "api_test",
                "related_prd_criteria": "AC-1",
                "expected_result": "the endpoint returns 200",
                "verification_phase": 1,
            },
        ],
    }
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _fail_report(fp="aaa"):
    """A round report: the point failed, with the given fingerprint label.

    The fingerprint is a hash of the failure's evidence, so the label is
    carried by the error line — two rounds with two different labels are
    two different failures under the same name, which is exactly the
    case the old decision key cannot see.
    """
    error = f"AssertionError: fingerprint {fp}"
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": VP_ID,
                "status": "FAILED",
                "failure_class": FAILURE_CLASS,
                "actual_result": error,
                "evidence": error,
            },
        ],
        "requirement_deviations": [],
    }


def _write_report(plan_dir, report):
    """The round's report on disk — what the repair path reads."""
    (plan_dir / "verification_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _write_root_cause_report(plan_dir, scope=None):
    """The root cause report the diagnosis task delivers.

    It names the files to change under a ``改动范围`` heading — that
    section is the implementation task's ``files_to_modify``.
    """
    paths = list(scope if scope is not None else REPORT_SCOPE)
    reports_dir = plan_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    body = [
        f"# {VP_ID} 根因报告",
        "",
        "## 根因",
        "断言读到 401 而不是 200 —— 鉴权中间件在校验之前就短路了。",
        "",
        "## 改动范围",
    ]
    body.extend(f"- `{path}`" for path in paths)
    body.extend([
        "",
        "## 验收方式",
        "pytest -q backend/tests/test_service.py",
        "",
    ])
    (reports_dir / ROOT_CAUSE_REPORT_NAME).write_text(
        "\n".join(body), encoding="utf-8",
    )


def _read_chain(plan_dir):
    """The chain's record on disk, parsed."""
    path = plan_dir / CHAIN_FILENAME
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def run_orchestrator_rounds(rounds, plan_dir, project_dir):
    """Run a sequence of rounds; return ``[(orchestrator, result), ...]``.

    ``rounds`` is the brief's sequence — one entry per round, in order:

    * ``("fail", fingerprint)`` —— the point fails, with that fingerprint;
    * ``("root_cause_done", fingerprint)`` —— the diagnosis has landed
      (the root cause report is written) and the point fails again.

    Every round runs through a **fresh** orchestrator: that is how
    production runs it — the auto-loop rebuilds the orchestrator after
    every repair, so anything a round remembers has to come back from
    disk.
    """
    results = []
    for round_number, (kind, fingerprint) in enumerate(rounds, start=1):
        if kind == "root_cause_done":
            _write_root_cause_report(plan_dir)
        report = _fail_report(fingerprint)
        _write_report(plan_dir, report)
        orch = VerificationOrchestrator(plan_dir, project_dir, Mock())
        orch.start_verification_cycle(round_number=round_number)
        result = orch.check_cycle_conditions(
            report, round_number=round_number,
        )
        results.append((orch, result))
    return results


def _kinds(tasks):
    """The kinds of a round's tasks.

    A plain repair row carries no ``kind`` — the repair generator's rows
    never had one — so it reads as ``"repair"`` here.
    """
    return {str(task.get("kind") or "repair") for task in tasks or []}


# ---------------------------------------------------------------------------
# 1. 根因演进的那一轮不生成第三个修复任务
# ---------------------------------------------------------------------------


def test_root_cause_replaces_third_repair(plan_dir, project_dir, repair_mocks):
    """根因演进 → 任务集合恰为 ``{"root_cause"}``，没有第三个盲修复。

    第 1 轮失败，第 2 轮**同一个点**又失败了 —— 但失败指纹变了：根因
    已经演进。上一轮的修复方案打的是旧根因，再生成一个「修复 VP-3」的
    任务只会再打一次同样的地方。

    所以这一轮的任务集合必须**恰为**一个根因任务：一个不多（没有盲修复），
    一个不少（诊断这一轮仍然欠着一个任务）。
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)

    results = run_orchestrator_rounds(
        [("fail", "aaa"), ("fail", "bbb")], plan_dir, project_dir,
    )
    _, r1 = results[0]
    _, r2 = results[1]

    # 第 1 轮：第一次失败 —— 一个普通修复任务。
    assert _kinds(r1["repair_tasks"]) == {"repair"}, (
        f"the first failure gets a plain repair: {r1['repair_tasks']!r}"
    )

    # 第 2 轮：同一个点又失败了，但指纹变了 —— 根因演进。
    assert r2["root_cause_vp_ids"] == [VP_ID], (
        "the changed fingerprint is what makes this a root-cause round"
    )
    # 第 2 轮后任务集合 == {"root_cause"}（无第三个 repair）
    assert _kinds(r2["repair_tasks"]) == {"root_cause"}, (
        f"the third blind repair must not be generated: {r2['repair_tasks']!r}"
    )

    task = r2["repair_tasks"][0]
    assert task["kind"] == "root_cause"
    assert task["read_only"] is True, (
        "the diagnosis changes nothing — its deliverable is the report"
    )
    assert not task.get("files_to_modify"), (
        "a root-cause task declares no blast radius"
    )
    assert task["report_path"].endswith(ROOT_CAUSE_REPORT_NAME), (
        f"the report path is the deliverable: {task['report_path']!r}"
    )


# ---------------------------------------------------------------------------
# 2. 诊断交付之后只派一个实现任务
# ---------------------------------------------------------------------------


def test_single_implementation_after_diagnosis(plan_dir, project_dir, repair_mocks):
    """诊断交付 → 实现任务恰 1 个，且带改动范围。

    根因任务是只读的：它交付一份报告，**不**改代码。所以诊断交付之后的
    那一轮，点还是失败的 —— 但失败该由**一个**实现任务接手，而不是再来
    一个盲修复。

    实现任务的改动范围来自根因报告 —— 没有范围的实现任务等于把「改哪里」
    原样丢回给执行器，而根因任务刚刚回答过它。
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)

    results = run_orchestrator_rounds(
        [("fail", "aaa"), ("fail", "bbb"), ("root_cause_done", "bbb")],
        plan_dir, project_dir,
    )
    _, r3 = results[2]

    # 第 3 轮后 == {1 个 implementation}
    tasks = r3["repair_tasks"]
    assert len(tasks) == 1, f"exactly one implementation task: {tasks!r}"
    task = tasks[0]
    assert task["kind"] == "implementation"
    assert task["files_to_modify"], (
        "the implementation task must declare what it changes"
    )
    assert sorted(task["files_to_modify"]) == sorted(REPORT_SCOPE), (
        f"the scope comes from the report: {task['files_to_modify']!r}"
    )


# ---------------------------------------------------------------------------
# 3. 实现任务再失败 → 转人工
# ---------------------------------------------------------------------------


def test_impl_failure_blocks_on_user(plan_dir, project_dir, repair_mocks):
    """实现任务再失败 → 状态 ``BLOCKED_ON_USER``，且没有新任务。

    根因任务已经定位了根因，实现任务已经按根因改了代码 —— 还是失败。
    这时候再派一轮任务只是把同一个问题再问一遍：机器这一侧的路已经走完
    了（一次根因任务 + 一次实现任务），该人来看。

    所以这一轮**不再派发任何任务**，状态是「转人工」。
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)

    results = run_orchestrator_rounds(
        [
            ("fail", "aaa"),
            ("fail", "bbb"),
            ("root_cause_done", "bbb"),
            ("fail", "bbb"),
        ],
        plan_dir, project_dir,
    )
    _, r4 = results[3]

    # 第 4 轮后状态 == BLOCKED_ON_USER
    assert r4["repair_tasks"] == [], (
        f"no new task may be dispatched: {r4['repair_tasks']!r}"
    )
    assert r4["root_cause_chain"]["stage"] == CHAIN_STAGE_BLOCKED_ON_USER, (
        f"the chain stops at the human: {r4['root_cause_chain']!r}"
    )
    assert r4["blocked_on_user"] is True
    assert r4["waiting_for_user"] is True
    assert r4["status"] == "loop_stopped", (
        "the chain ends here — the auto-loop must not start another round"
    )
    assert r4["should_stop"] is True

    chain = _read_chain(plan_dir)
    assert chain["stage"] == CHAIN_STAGE_BLOCKED_ON_USER, (
        f"the chain's record says where it stopped: {chain!r}"
    )


# ---------------------------------------------------------------------------
# 4. 链路任务真的落进任务库
# ---------------------------------------------------------------------------


def test_chain_task_reaches_the_task_store(
    plan_dir, project_dir, repair_mocks, state_db,
):
    """诊断任务必须真的写进任务库 —— 否则执行器看不到它。

    链路在内存里排好了不算数：执行器是从 ``state.db`` 的
    ``plan_tasks`` 里取待办任务的。写不进去的任务，执行器永远不会跑，
    根因报告也就永远不会交回来 —— 链路会原地卡住。

    任务库没有「根因报告写到哪」这一列，所以那个路径必须在描述里：
    描述是执行器唯一读得到的地方。
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)

    results = run_orchestrator_rounds(
        [("fail", "aaa"), ("fail", "bbb")], plan_dir, project_dir,
    )
    _, r2 = results[1]

    from state_machine.db.connection import open as open_db
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(state_db)
    try:
        rows = PlanTaskRepository(conn).load_all(plan_dir.name)
    finally:
        conn.close()

    task_id = r2["repair_tasks"][0]["id"]
    assert task_id in rows, (
        f"the diagnosis never reached the task store: {rows!r}"
    )
    row = rows[task_id]
    assert row["title"].startswith("【根因演进】"), (
        f"the stored task must say what it is: {row['title']!r}"
    )
    assert ROOT_CAUSE_REPORT_NAME in str(row["description"]), (
        "the report path has to survive the write — the description is "
        f"where it lives: {row['description']!r}"
    )


# ---------------------------------------------------------------------------
# 5. 既有的 max_rounds 判定一个字符不改
# ---------------------------------------------------------------------------


def test_max_rounds_semantics_unchanged(tmp_path, monkeypatch):
    """既有的 ``max_rounds`` 测试**零修改**跑过。

    这条不是重写一遍既有判定，而是把既有测试的**本体**原样跑一次：既有
    文件一个字都不动，它怎么说，这一轮就怎么说。``max_rounds`` 是软上限、
    ``loop_stopped`` 的判定归属同一条分支 —— 根因链路不得碰它们。
    """
    existing = Path(__file__).resolve().parents[2] / "test_verification_orchestrator.py"
    assert existing.exists(), f"the existing test file is gone: {existing}"

    spec = importlib.util.spec_from_file_location(
        "existing_verification_orchestrator_tests", existing,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)

    # The existing test body — called as-is, with the same arguments its
    # own fixtures would have supplied.
    module.TestBoundaryConditions().test_max_rounds_boundary_at_exact_limit(
        plan_dir, project_dir, Mock(),
    )


# ---------------------------------------------------------------------------
# 6. 负控：同一指纹重复仍走既有路径
# ---------------------------------------------------------------------------


def test_same_fingerprint_repeat_keeps_the_existing_path(
    plan_dir, project_dir, repair_mocks,
):
    """同一指纹重复 → 既有路径不变（既不是根因任务，也不转人工）。

    指纹**没变**意味着这还是同一个失败：旧键的计数继续走它的路。这条
    负控钉住「根因链路只接管指纹变了的那一轮」，不动旧键的任何语义。
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)

    results = run_orchestrator_rounds(
        [("fail", "aaa"), ("fail", "aaa")], plan_dir, project_dir,
    )
    _, r1 = results[0]
    _, r2 = results[1]

    assert _kinds(r1["repair_tasks"]) == {"repair"}

    assert r2["root_cause_vp_ids"] == [], (
        "the same failure is the old key's business"
    )
    assert _kinds(r2["repair_tasks"]) == {"repair"}, (
        f"a same-fingerprint repeat is still a plain repair: "
        f"{r2['repair_tasks']!r}"
    )
    assert r2.get("blocked_on_user") is not True, (
        "the same failure is not a human decision"
    )

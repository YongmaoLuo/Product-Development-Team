"""TDD spec —— 终局轮次的**操作员决策**：一条带事实、可决策的问题。

背景
----
根因链路的终局轮次（诊断 → 实现 → 还是失败）此前只把 ``needs_operator_decision``
和 ``waiting_for_user`` 置上，**从不真的问一个问题**：机器这一侧的路走完了，
人却什么也没收到 —— 一张「请人工介入」的牌子，门后没有内容。

这一层把链路的两个出口接起来：``_terminal_decision_question`` 造出**带事实**的
问题（哪一轮停的、为什么停、哪个点挂了、几个修复任务），``_ask_a_human_about``
把这个问题**写进决策表**（``plan_decisions``），人才能答。

三份契约
--------
1. ``test_terminal_round_creates_one_decidable_operator_question`` —— 终局轮次
   **恰一条**决策：问的是这个计划（不是某一个点），问题里带着事实，选项是
   三个固定语义，``default_option`` 在选项之内。
2. ``test_passing_round_creates_no_operator_question`` —— 全 PASSED 的轮次
   **一条决策也不产生**（负控：接线不能过宽）。
3. ``test_terminal_round_keeps_its_existing_outcome`` —— 接线**不改变**终局
   轮次原有的结论：``repair_tasks`` 仍为空、仍 ``blocked_on_user``、链路阶段
   仍是 ``MACHINE_EXHAUSTED``。

再加上「同一个计划只问一次」的三份契约
--------------------------------------
4. ``test_open_terminal_question_is_not_asked_again`` —— 计划已经拿着一个
   **未决**的终局问题时，终局轮次**不再写第二行**，但收尾路径一个字段都不变。
5. ``test_same_round_terminal_question_is_not_asked_again`` —— 同一轮里已经
   问过一次（哪怕那一问没人接），这一轮不再问第二次。
6. ``test_point_question_does_not_silence_the_terminal_question`` —— 挂在
   **某一个点**上的未决问题**不吞掉**终局问题（负控：计划级去重不能误伤
   别人的问题；没有未决终局问题时，终局问题照旧要问出来）。

轮次序列
--------
::

    rounds = [
        ("fail", "aaa"),          # 第 1 轮：失败 → 一个普通修复任务
        ("fail", "bbb"),          # 第 2 轮：指纹变了 → 根因任务
        ("root_cause_done", "bbb"),  # 第 3 轮：诊断交付 → 1 个 implementation
        ("fail", "bbb"),          # 第 4 轮：实现再失败 → 终局，要问人
    ]
    results = run_orchestrator_rounds(rounds, plan_dir, project_dir)

夹具说明
--------
本文件自带最小夹具，**不跨测试文件 import**（夹具是测试的一部分，import 别人
的夹具等于把别人的改动变成自己的失败原因）。

``PDT_STATE_DB_PATH`` 由根 ``backend/tests/conftest.py`` 的 autouse
``isolated_plans_dir`` 夹具重定向到 ``tmp_path/state.db`` —— ``create_decision``
与 ``list_decisions`` 因此读写同一个 per-test 库，测试内**不手动改环境变量**。

根因报告里的改动范围是**声明**的（``json`` 块），不是散文里写的：读者只读
声明 —— 写成 bullet 的范围段不是声明（同一个约定由
``tests/integration/verification_chain/test_root_cause_scope_missing.py``
从另一头钉住）。不声明范围，第 3 轮的点就直接交给人，下面这段轮次序列根本
不会发生。

测试命令::

    pytest backend/tests/integration/verification_chain/test_terminal_operator_decision.py -v
"""

from __future__ import annotations

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

import hitl.service  # noqa: E402
from verification.orchestrator import VerificationOrchestrator  # noqa: E402


pytestmark = pytest.mark.integration


#: The point the rounds below fail on.
VP_ID = "VP-3"

#: The failure class the rounds carry. It counts (it is not ``infra`` /
#: ``out_of_scope``), so the old key's counter moves.
FAILURE_CLASS = "deliverable_defect"

#: The chain's record — the file that says where the escalation stands.
CHAIN_FILENAME = "verification_root_cause_chain.json"

#: The stage the chain reaches when a human has to take over.
CHAIN_STAGE_MACHINE_EXHAUSTED = "MACHINE_EXHAUSTED"

#: The file the root cause report lands in (the report the diagnosis
#: task delivers, and the implementation task reads).
ROOT_CAUSE_REPORT_NAME = f"root-cause-{VP_ID}.md"

#: The scope the root cause report delivers — the implementation task's
#: ``files_to_modify`` comes from exactly these paths.
REPORT_SCOPE = ["backend/app/service.py", "backend/app/handler.py"]

#: The three answers a terminal question offers — the three things that
#: can happen to a plan whose repair path ran out.
TERMINAL_OPTIONS = ["rerun_this_round", "accept_as_failed", "manual_repair_tasks"]

#: The answer a terminal question falls back to when nobody replies.
TERMINAL_DEFAULT_OPTION = "accept_as_failed"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path):
    plan = tmp_path / "plans" / "terminal-operator-decision"
    plan.mkdir(parents=True, exist_ok=True)
    return plan


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "projects" / "terminal-operator-decision"
    project.mkdir(parents=True, exist_ok=True)
    return project


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


@pytest.fixture
def decision_calls(monkeypatch):
    """A **pass-through** spy on ``hitl.service.create_decision``.

    The spy records the request and then calls the real implementation,
    so the decision really lands in ``plan_decisions`` — a spy that only
    records would leave the table empty and prove nothing about what the
    operator is asked.

    Returns:
        The list of requests the real implementation was handed.
    """
    real = hitl.service.create_decision
    calls = []

    def spy(request, *args, **kwargs):
        calls.append(request)
        return real(request, *args, **kwargs)

    monkeypatch.setattr(hitl.service, "create_decision", spy)
    return calls


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


def _pass_report():
    """A round report: the point passed — nothing is broken, nothing to ask."""
    return {
        "overall_status": "PASSED",
        "verification_results": [
            {
                "id": VP_ID,
                "status": "PASSED",
                "actual_result": "200",
                "evidence": "200",
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

    The scope is **declared**: a ``json`` block naming the files to
    change, which is the implementation task's ``files_to_modify``.

    The reader reads declarations, not prose — a scope section written
    as bullets is not a declaration (see
    ``tests/integration/verification_chain/test_root_cause_scope_missing.py``,
    which pins the same contract from the other side).  A report that
    declares no scope says nothing about what to change: the point goes
    to a human in round 3, and the round sequence this file documents
    (round 3 delivers the implementation, round 4 is the terminal one)
    never happens.
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
        "",
        "```json",
        "{",
        '  "files_to_modify": %s,' % json.dumps(
            list(paths), ensure_ascii=False,
        ),
        '  "fix_plan": "在鉴权中间件里补一条身份检查"',
        "}",
        "```",
        "",
        "## 验收方式",
        "pytest -q backend/tests/test_service.py",
        "",
    ]
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
      (the root cause report is written) and the point fails again;
    * ``("pass", anything)`` —— the point passes (the label is unused);
      the negative control's round.

    Every round runs through a **fresh** orchestrator: that is how
    production runs it — the auto-loop rebuilds the orchestrator after
    every repair, so anything a round remembers has to come back from
    disk.
    """
    results = []
    for round_number, (kind, fingerprint) in enumerate(rounds, start=1):
        if kind == "root_cause_done":
            _write_root_cause_report(plan_dir)
        report = _pass_report() if kind == "pass" else _fail_report(fingerprint)
        _write_report(plan_dir, report)
        orch = VerificationOrchestrator(plan_dir, project_dir, Mock())
        orch.start_verification_cycle(round_number=round_number)
        result = orch.check_cycle_conditions(
            report, round_number=round_number,
        )
        results.append((orch, result))
    return results


# ---------------------------------------------------------------------------
# 1. 终局轮次创建一条可决策的问题
# ---------------------------------------------------------------------------


def test_terminal_round_creates_one_decidable_operator_question(
    plan_dir, project_dir, repair_mocks, decision_calls,
):
    """终局轮次 → **恰一条**决策，问题带着事实，选项是三个固定语义。

    机器这一侧的路走完了（一次诊断 + 一次实现，点还是失败），该人来看。
    人要看的不是「请人工介入」四个字，而是**能据此决定的事实**：

    * 停在哪一轮（第 4 轮）；
    * 为什么停（``repair_execution_failed``）；
    * 哪个点挂了、挂成什么（``VP-3=FAILED``）；
    * 这一轮造了几个修复任务（``0 repair task(s)`` —— 终局分支按构造
      不造任务，所以 0 是事实而不是占位符）。

    问题问的是**这个计划**，不是某一个点：终局时该决定「这个计划接下来
    怎么办」的人不是某个点的负责人。
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

    # 终局轮次到了。
    assert r4["stop_reason"] == "repair_execution_failed", (
        f"the fourth round is the terminal one: {r4!r}"
    )

    # 恰好问了一次 —— 前三轮都没有问（前三轮各有任务可派）。
    assert len(decision_calls) == 1, (
        f"the terminal round asks exactly one question: {decision_calls!r}"
    )
    request = decision_calls[0]

    # 问的是这个计划。
    assert request.plan_id == plan_dir.name, (
        f"the question belongs to this plan: {request.plan_id!r}"
    )
    assert request.vp_id is None, (
        "the terminal question is about the plan, not about one point: "
        f"{request.vp_id!r}"
    )
    assert request.source == "verification", (
        f"the question is the verification loop's: {request.source!r}"
    )
    assert request.round == 4, (
        f"the question says which round stopped: {request.round!r}"
    )

    # 问题带着四件事实。
    question = request.question
    assert "round 4" in question, f"which round stopped: {question!r}"
    assert "repair_execution_failed" in question, f"why it stopped: {question!r}"
    assert "VP-3=FAILED" in question, f"which point failed: {question!r}"
    assert "0 repair task(s)" in question, (
        f"how many repair tasks the round produced: {question!r}"
    )

    # 三个固定语义的选项，默认项在选项之内。
    assert request.options == TERMINAL_OPTIONS, (
        f"the three answers on the table: {request.options!r}"
    )
    assert request.default_option in request.options, (
        f"the fallback answer must be one of the options: "
        f"{request.default_option!r} not in {request.options!r}"
    )

    # 问题真的落到决策表里了 —— 人要能答它。
    from hitl.service import list_decisions

    stored = list_decisions(plan_dir.name)
    assert len(stored) == 1, (
        f"exactly one decision is waiting for an answer: {stored!r}"
    )
    assert stored[0].source == "verification"
    assert stored[0].round == 4
    assert stored[0].question == question
    assert list(stored[0].options) == TERMINAL_OPTIONS


# ---------------------------------------------------------------------------
# 2. 全 PASSED 的轮次不产生任何决策（次负控）
# ---------------------------------------------------------------------------


def test_passing_round_creates_no_operator_question(
    plan_dir, project_dir, repair_mocks, decision_calls,
):
    """一轮全 PASSED → 一条决策也不产生。

    接线的**宽度**必须被钉住：如果终局之外的路径也问人，操作员会收到
    「计划通过了，你要不要点一下确认吗」这种问题。一轮通过了，就该没有
    问题 —— 决策表必须是空的。
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)

    results = run_orchestrator_rounds(
        [("pass", "zzz")], plan_dir, project_dir,
    )
    _, r1 = results[0]

    assert r1["status"] == "passed", f"the round passed: {r1!r}"
    assert decision_calls == [], (
        f"a passing round asks nothing: {decision_calls!r}"
    )

    from hitl.service import list_decisions

    assert list_decisions(plan_dir.name) == [], (
        "no decision may exist for a plan whose round passed"
    )


# ---------------------------------------------------------------------------
# 3. 接线不改变终局轮次原有的结论
# ---------------------------------------------------------------------------


def test_terminal_round_keeps_its_existing_outcome(
    plan_dir, project_dir, repair_mocks,
):
    """终局轮次的结论一个字段都没变。

    问人是为了让人**接手**，不是为了改掉机器这一侧已经算清楚的结论：
    终局分支仍然不派新任务（``repair_tasks == []``）、仍然等一个人
    （``waiting_for_user``）、链路仍然停在 ``MACHINE_EXHAUSTED``。

    接线如果动了这些，接线就变成了「多加一个任务」，而不是「多问一句」。
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

    assert r4["repair_tasks"] == [], (
        f"no new task may be dispatched: {r4['repair_tasks']!r}"
    )
    # 2026-11-06: 「机器没路了」（``machine_exhausted``）与「有人在等
    # 回答」（``blocked_on_user``）是两个事实：这个环境里问题被问出来了，
    # 但没有人接得住它（没有出口），所以没有人在等。
    assert r4["machine_exhausted"] is True
    assert r4["blocked_on_user"] is False
    assert r4["waiting_for_user"] is True
    assert r4["needs_operator_decision"] is True
    assert r4["status"] == "loop_stopped"
    assert r4["should_stop"] is True
    assert r4["root_cause_chain"]["stage"] == CHAIN_STAGE_MACHINE_EXHAUSTED, (
        f"the chain stops where the machine has no road left: "
        f"{r4['root_cause_chain']!r}"
    )

    chain = _read_chain(plan_dir)
    assert chain["stage"] == CHAIN_STAGE_MACHINE_EXHAUSTED, (
        f"the chain's record says where it stopped: {chain!r}"
    )


# ---------------------------------------------------------------------------
# 4-6. 同一个计划的终局决策只问一次
# ---------------------------------------------------------------------------


def _seed_terminal_decision(plan_id, round_number, *, status="pending",
                             vp_id=None):
    """Write a terminal question for the plan straight into the store.

    The row is seeded, not asked: this is the state the plan is in when
    somebody is already holding its terminal question, and reaching that
    state through the delivery layer would need an outlet to deliver
    to.  ``vp_id`` names a point when the question is about one.
    """
    from config_paths import resolve_state_db_path
    from hitl.models import Decision, DecisionRequest
    from hitl.store import HitlStore

    request = DecisionRequest(
        plan_id=plan_id,
        source="verification",
        vp_id=vp_id,
        round=round_number,
        question=(
            f"verification round {int(round_number)} stopped with "
            f"repair_execution_failed: 0 repair task(s) were generated; "
            f"failed checks: {VP_ID}=FAILED. Choose how to proceed."
        ),
        options=list(TERMINAL_OPTIONS),
        default_option=TERMINAL_DEFAULT_OPTION,
    )
    decision = Decision.from_request(request)
    decision.status = status
    store = HitlStore(resolve_state_db_path())
    try:
        store.insert(decision)
    finally:
        store.close()
    return decision


def test_open_terminal_question_is_not_asked_again(
    plan_dir, project_dir, repair_mocks, decision_calls,
):
    """计划已经拿着未决的终局问题 → 终局轮次**不再问第二次**。

    终局问题问的是**这个计划**（``vp_id=None``），按点查重的
    ``check_open_question`` 看不见它 —— 每进一次终局分支就多一行一样的
    问题，操作员每轮都要答一遍一模一样的话。

    这里计划已经拿着一个**未决**（``pending``）的终局问题：终局轮次不再写
    新行；但**收尾照旧** —— 结论、停止、链路阶段一个字段都不变。跳过提问
    不是跳过收尾。
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)
    seeded = _seed_terminal_decision(plan_dir.name, round_number=3)

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

    assert decision_calls == [], (
        f"the plan is asked once, not once per round: {decision_calls!r}"
    )

    from hitl.service import list_decisions

    stored = list_decisions(plan_dir.name)
    assert len(stored) == 1, (
        f"the question the plan already holds is the only one: {stored!r}"
    )
    assert stored[0].decision_id == seeded.decision_id

    # 收尾路径一个字段都没变。
    assert r4["stop_reason"] == "repair_execution_failed"
    assert r4["repair_tasks"] == []
    # 这个计划**拿着一个未决的问题**，所以确实有人在等回答。
    assert r4["blocked_on_user"] is True
    assert r4["machine_exhausted"] is True
    assert r4["waiting_for_user"] is True
    assert r4["needs_operator_decision"] is True
    assert r4["status"] == "loop_stopped"
    assert r4["should_stop"] is True
    assert r4["root_cause_chain"]["stage"] == CHAIN_STAGE_MACHINE_EXHAUSTED


def test_same_round_terminal_question_is_not_asked_again(
    plan_dir, project_dir, repair_mocks, decision_calls,
):
    """同一轮里已经问过一次 → 这一轮不再问第二次。

    那一问可能 nobody 接（``unroutable``：没人被问过），但**同一轮**不该
    把同一个问题问两遍 —— 一轮问一次，问的是这一轮的实。
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)
    _seed_terminal_decision(
        plan_dir.name, round_number=4, status="unroutable",
    )

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

    assert r4["stop_reason"] == "repair_execution_failed"
    assert decision_calls == [], (
        f"one round asks the plan once: {decision_calls!r}"
    )

    from hitl.service import list_decisions

    assert len(list_decisions(plan_dir.name)) == 1


def test_point_question_does_not_silence_the_terminal_question(
    plan_dir, project_dir, repair_mocks, decision_calls,
):
    """挂在点上的未决问题**不吞掉**终局问题（负控）。

    计划级去重判的是「问的是这个计划、不是某一个点」：一条挂在点上的
    未决问题（``vp_id="VP-3"``）不是终局问题，不能让它把终局问题一起吞掉
    —— 那样操作员就永远收不到「这个计划接下来怎么办」。

    这条同时是接线本身的负控：计划**没有**未决的终局问题时，终局问题照旧
    要问出来（去重不是把提问关掉）。
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)
    _seed_terminal_decision(plan_dir.name, round_number=2, vp_id=VP_ID)

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

    assert r4["stop_reason"] == "repair_execution_failed"
    assert len(decision_calls) == 1, (
        f"the terminal question is still asked: {decision_calls!r}"
    )
    assert decision_calls[0].vp_id is None
    assert decision_calls[0].round == 4

    from hitl.service import list_decisions

    stored = list_decisions(plan_dir.name)
    assert len(stored) == 2, (
        f"the point's own question is still there: {stored!r}"
    )
    assert {row.vp_id for row in stored} == {VP_ID, None}


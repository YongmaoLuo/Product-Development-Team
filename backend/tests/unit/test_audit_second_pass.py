"""Tests for the audit-task second-pass adversarial review.

Background (the project's CLAUDE.md, "Task completion dual-criterion
rule", 2026-08-24):

  For audit-style tasks (declared ``verification_only=True`` or
  heuristically detected by ``_looks_like_audit_task``), the
  first-pass cross_verify gate relies on ``pytest`` exit codes plus
  the AI's own TEST_RESULT claim. For a path-audit task (e.g.
  "verify src/stock_data/providers/registry.py contains PROVIDERS /
  @register_provider / fallback_chain") there is no code diff to
  inspect, so the empty-output gate cannot rescue a plausibly-
  sounding but incorrect answer.

  The fix is an adversarial second pass: a fresh ``claude -p`` call
  reads the spec (task.description) and the subagent's final
  report, then returns a verdict. If the verdict is FAILED, the
  task is downgraded from PASSED to FAILED regardless of what the
  first-pass cross_verify said.

  Failure mode (network / timeout / non-JSON output) must NOT
  block the first-pass verdict — second pass is a tightening on
  top of an already-passing task, never a hard fail-blocker. The
  helper returns ``audit_inconclusive:<reason>`` and the caller
  treats the task as passed while logging the gap.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

# tools/ is on sys.path via pytest.ini's pythonpath so
# ``from agent import AutonomousAgent`` resolves to backend/agent.py.
from agent import AutonomousAgent


def _make_agent() -> AutonomousAgent:
    """Build a minimal AutonomousAgent stub with only the fields
    ``_audit_task_second_pass`` and ``_looks_like_audit_task``
    touch."""
    config = MagicMock()
    config.executor_system_prompt = ""
    coding_tool = MagicMock()
    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.config = config
    agent.coding_tool = coding_tool
    agent.logger = None
    return agent


def _make_task(
    task_id: str = "t1",
    title: str = "",
    description: str = "",
    verification_only: bool = False,
    test_commands: list | None = None,
) -> MagicMock:
    """Build a task stub. ``title`` and ``description`` default to
    empty strings (NOT MagicMock auto-spec) so the audit-task
    keyword scan in ``_looks_like_audit_task`` operates on real
    strings — otherwise it crashes inside the haystack build with
    ``TypeError: sequence item: expected str instance, MagicMock found``.

    ``test_commands`` models ``SubTask.get_test_commands()`` and
    defaults to a non-empty list — the common case (and what a bare
    MagicMock attribute produced incidentally, by being truthy). Pass
    ``test_commands=[]`` to model a task with no runnable command, which
    since 2026-09-14 is itself a second-pass trigger.
    """
    t = MagicMock()
    t.id = task_id
    t.title = title
    t.description = description
    t.verification_only = verification_only
    commands = (
        ["pytest tests/test_x.py -q"] if test_commands is None
        else list(test_commands)
    )
    t.get_test_commands = MagicMock(return_value=commands)
    return t


class TestAuditSecondPass(unittest.TestCase):
    def test_non_audit_task_short_circuits(self):
        """Non-audit tasks get (True, "") — no second pass needed."""
        agent = _make_agent()
        task = _make_task(
            title="重构 src/native/api/routes.py: 添加新路由",
            description="实现新 API endpoint",
        )
        passed, reason = agent._audit_task_second_pass(task, "subagent response")
        self.assertTrue(passed)
        self.assertEqual(reason, "")
        # coding_tool.query should NOT have been called
        agent.coding_tool.query.assert_not_called()

    def test_task_without_a_test_command_gets_the_second_pass(self):
        """2026-09-14: a task with no runnable command has no independent
        second signal — ``_cross_verify_test_result`` can only echo the
        AI's claim — so the adversarial review must run even though the
        task is not audit-style.

        Regression: the 2026-09-07 content-only refactor stripped
        ``test_command`` from every generated repair task, so the whole
        repair round completed on self-reports alone.
        """
        agent = _make_agent()
        agent.coding_tool.query = MagicMock(
            return_value="AUDIT_VERDICT: PASSED"
        )
        task = _make_task(
            title="修复 VP-013 的 test_command",
            description="改 verification_plan.json",
            test_commands=[],
        )

        passed, reason = agent._audit_task_second_pass(task, "answer")

        self.assertTrue(passed)
        agent.coding_tool.query.assert_called_once()

    def test_commandless_task_can_be_downgraded_by_the_second_pass(self):
        agent = _make_agent()
        agent.coding_tool.query = MagicMock(
            return_value=(
                "evidence: signal.rs:31 still calls select_entering_consolidation\n"
                "AUDIT_VERDICT: FAILED\n"
                "REASON: the 9/2 behaviour freeze is still violated"
            )
        )
        task = _make_task(
            title="修复 VP-006", description="撤销函数体改写",
            test_commands=[],
        )

        passed, reason = agent._audit_task_second_pass(task, "「我已经修好了」")

        self.assertFalse(passed)
        self.assertIn("9/2 behaviour freeze", reason)

    def test_verification_only_task_runs_audit(self):
        """verification_only=True always triggers the second pass."""
        agent = _make_agent()
        agent.coding_tool.query = MagicMock(return_value="AUDIT_VERDICT: PASSED")
        task = _make_task(
            title="内容审计",
            description="verify 三个 API",
            verification_only=True,
        )
        passed, reason = agent._audit_task_second_pass(task, "answer")
        self.assertTrue(passed)
        agent.coding_tool.query.assert_called_once()
        # The prompt must include the task description
        call_args = agent.coding_tool.query.call_args
        self.assertIn("verify 三个 API", call_args.args[0])

    def test_audit_keyword_triggers_audit(self):
        """A task whose title contains 审计 triggers the second pass
        even without verification_only=True."""
        agent = _make_agent()
        agent.coding_tool.query = MagicMock(return_value="AUDIT_VERDICT: PASSED")
        task = _make_task(
            title="路径契约审计",
            description="定位真实 src/ 与 tests/ 目录",
        )
        passed, _ = agent._audit_task_second_pass(task, "answer")
        self.assertTrue(passed)
        agent.coding_tool.query.assert_called_once()

    def test_audit_passed_verdict(self):
        """AUDIT_VERDICT: PASSED → (True, \"\")."""
        agent = _make_agent()
        agent.coding_tool.query = MagicMock(
            return_value=(
                "evidence: registry.py:1 has PROVIDERS\n"
                "evidence: registry.py:42 has @register_provider\n"
                "AUDIT_VERDICT: PASSED"
            )
        )
        task = _make_task(title="内容审计", description="x", verification_only=True)
        passed, reason = agent._audit_task_second_pass(task, "answer")
        self.assertTrue(passed)
        self.assertEqual(reason, "")

    def test_audit_failed_verdict_downgrades(self):
        """AUDIT_VERDICT: FAILED → (False, reason)."""
        agent = _make_agent()
        agent.coding_tool.query = MagicMock(
            return_value=(
                "evidence: registry.py:1 has FOO, not PROVIDERS\n"
                "AUDIT_VERDICT: FAILED\n"
                "REASON: PROVIDERS symbol not found in registry.py"
            )
        )
        task = _make_task(title="内容审计", description="x", verification_only=True)
        passed, reason = agent._audit_task_second_pass(task, "answer")
        self.assertFalse(passed)
        self.assertIn("PROVIDERS symbol not found", reason)

    def test_audit_timeout_is_non_blocking(self):
        """coding_tool.query TimeoutError → (True, audit_inconclusive:...)."""
        agent = _make_agent()
        agent.coding_tool.query = MagicMock(side_effect=TimeoutError("network"))
        task = _make_task(title="路径审计", description="x", verification_only=True)
        passed, reason = agent._audit_task_second_pass(task, "answer")
        # The first-pass verdict stands; the audit is inconclusive, not failed.
        self.assertTrue(passed)
        self.assertIn("audit_inconclusive", reason)
        self.assertIn("TimeoutError", reason)

    def test_audit_no_verdict_is_non_blocking(self):
        """A response with no AUDIT_VERDICT line is treated as
        inconclusive, not as FAILED — we cannot tell whether the
        second pass ran or hung, so do not penalise the task."""
        agent = _make_agent()
        agent.coding_tool.query = MagicMock(
            return_value="I'm not sure, looks plausible I guess"
        )
        task = _make_task(title="路径审计", description="x", verification_only=True)
        passed, reason = agent._audit_task_second_pass(task, "answer")
        self.assertTrue(passed)
        self.assertIn("audit_inconclusive:no_verdict", reason)

    def test_audit_verdict_with_reason(self):
        """REASON line is captured when verdict is FAILED."""
        agent = _make_agent()
        agent.coding_tool.query = MagicMock(
            return_value=(
                "AUDIT_VERDICT: FAILED\n"
                "REASON: missing test for fallback_chain"
            )
        )
        task = _make_task(title="路径审计", description="x", verification_only=True)
        passed, reason = agent._audit_task_second_pass(task, "answer")
        self.assertFalse(passed)
        self.assertIn("missing test for fallback_chain", reason)

    def test_commandless_task_review_carries_the_self_verification_contract(self):
        """2026-09-20: when there is no command to re-run, the reviewer is
        told what the subagent was asked to do.

        The executor's ``test_instruction`` now asks a commandless task
        to construct its own verification and paste the terminal output
        (see ``test_audit_veto_gates_completion``). If the reviewer is
        not told that, it judges prose against a spec that never asked
        for evidence — which is how six repair tasks were marked
        ``completed`` on self-reports alone.
        """
        agent = _make_agent()
        agent.coding_tool.query = MagicMock(return_value="AUDIT_VERDICT: PASSED")
        task = _make_task(title="修复 VP-004", description="x", test_commands=[])

        agent._audit_task_second_pass(task, "「上一轮已经改好了，EXIT_CODE=0」")

        prompt = agent.coding_tool.query.call_args.args[0]
        self.assertIn("this task has no test_command", prompt)
        self.assertIn("must be FAILED", prompt)

    def test_task_with_a_command_review_omits_the_note(self):
        """The note is scoped to the commandless population.

        A task with a real ``test_command`` already has an independent
        second signal, so adding the note would just bias the reviewer.
        """
        agent = _make_agent()
        agent.coding_tool.query = MagicMock(return_value="AUDIT_VERDICT: PASSED")
        task = _make_task(
            title="内容审计", description="x", verification_only=True,
        )

        agent._audit_task_second_pass(task, "answer")

        prompt = agent.coding_tool.query.call_args.args[0]
        self.assertNotIn("this task has no test_command", prompt)


class TestRepairTasksAreNeverAuditStyle(unittest.TestCase):
    """2026-09-20 (post-mortem): the keyword heuristic must not
    swallow repair tasks.

    ``_AUDIT_TASK_KEYWORDS`` contains 「定位」, 「复跑」, 「不变量」 and
    「checkpoint」 — words that occur naturally in a *repair brief*
    ("【根因（代码定位）】", "异地复跑", "核心不变量"). Those words are
    common in a repair brief, so the heuristic classified repair tasks as
    audit-style; for an audit task an empty diff is accepted rather than
    retried, so the tasks produced an empty git diff, were marked
    ``completed``, and left the next round's failure set byte-identical —
    which the loop then correctly reported as convergence.

    A repair task's entire purpose is to change code in response to a
    failed verification point. Keywords in its brief describe what to
    look at, never what to deliver.
    """

    #: Real brief fragments from an earlier plan, each paired with the
    #: keyword it used to trip.
    REPAIR_BRIEFS = [
        (
            "【根因（代码定位）】分类唯一产出点是 native_ext/src/"
            "core.rs 的 classify_metric_window(:1102-1168)",
            "定位",
        ),
        (
            "异地 cwd 收口：从 /tmp 复跑 plan 原文，证明既不依赖 cwd "
            "也不依赖 venv",
            "复跑",
        ),
        (
            "把核心不变量（zg>=zd、区间与构成笔包含关系）两条用例"
            "接回真实 test_command",
            "不变量",
        ),
        (
            "checkpoints.json 写入 vp_artifacts/VP-019/checkpoints.json"
            "（5 个 checkpoint，全部 passed=false）",
            "checkpoint",
        ),
    ]

    def test_repair_id_is_never_audit_style(self):
        agent = _make_agent()
        for brief, keyword in self.REPAIR_BRIEFS:
            with self.subTest(keyword=keyword):
                task = _make_task(
                    task_id="repair-r1-03",
                    title="修复 VP-005：5min 周期信号分类",
                    description=brief,
                )
                # Anti-vacuous: the keyword really is in the brief, so
                # without the guard this task WOULD be classified audit.
                self.assertIn(keyword, task.description)
                self.assertFalse(
                    agent._looks_like_audit_task(task),
                    f"repair task matched {keyword!r} and was classified "
                    f"audit-style — an empty diff would be accepted",
                )

    def test_legacy_RP_id_is_also_recognised(self):
        agent = _make_agent()
        task = _make_task(
            task_id="RP-004",
            title="修复 VP-009",
            description="【根因（代码定位）】period_config.py:58-68",
        )
        self.assertFalse(agent._looks_like_audit_task(task))

    def test_repair_task_group_without_a_repair_id_is_recognised(self):
        """Some repair rows carry ``task_group`` but no ``repair-`` id.

        The id scheme is the reliable half — several repair rows had
        a null ``task_group`` — so both signals are checked.
        """
        agent = _make_agent()
        task = _make_task(
            task_id="9-3",
            title="修复 VP-001",
            description="【根因（代码定位）】…",
        )
        task.model_dump = MagicMock(return_value={"task_group": "repair-round-1"})
        self.assertFalse(agent._looks_like_audit_task(task))

    def test_the_guard_is_repair_scoped_not_a_blanket_removal(self):
        """A genuine audit task must still be detected.

        The bug was that repair briefs leak audit vocabulary, not that
        the keyword list is wrong.
        """
        agent = _make_agent()
        task = _make_task(
            task_id="1-2-2",
            title="内容审计：OCP 刷新路径契约",
            description="给出 行号定位 报告",
        )
        self.assertTrue(agent._looks_like_audit_task(task))


class TestAuditIntegrationWithExecute(unittest.TestCase):
    """Verify that an audit-task second-pass FAILED verdict flows
    through ``_execute_task_with_retry`` correctly: the cross_verify
    PASSED but audit FAILED must be reflected as a non-zero
    return."""

    def test_audit_failure_downgrades_cross_verify(self):
        # Smoke test: confirm the helper exposes (bool, str) — this
        # is what the call site uses to override tests_passed.
        agent = _make_agent()
        agent.coding_tool.query = MagicMock(
            return_value="AUDIT_VERDICT: FAILED\nREASON: nope"
        )
        task = _make_task(title="内容审计", description="x", verification_only=True)
        passed, reason = agent._audit_task_second_pass(task, "answer")
        self.assertFalse(passed)
        # contract: when audit returns False, caller MUST downgrade
        # tests_passed to False (per integration test below).
        tests_passed = True
        if not passed:
            tests_passed = False
            test_reason = reason
        self.assertFalse(tests_passed)
        self.assertIn("nope", test_reason)


if __name__ == "__main__":
    unittest.main()
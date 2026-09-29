"""
TDD tests for the inline spec/code review inserted into
``AutonomousAgent._execute_task_with_retry`` (DP3).

Background
----------
DP3 requires that, after the cross-verify layer confirms the test
command exited 0 but BEFORE the git checkpoint commit, the agent
performs a lightweight adversarial self-review:

  * Pass ``task.description`` + ``git diff --stat`` to an LLM.
  * The LLM grades the implementation along two dimensions —
    spec compliance and code quality — and emits ``should_block``.
  * If the LLM flags either dimension as ``high`` severity, the
    task is reverted to ``pending``, ``failure_reason`` is written,
    and the commit is skipped.
  * If the LLM call fails (network, API error, JSON parse), the
    agent logs a warning and proceeds with the commit — the review
    is a safety net, not a hard gate.
  * If the git diff is empty, the review is skipped entirely.

These tests pin the integration contract between
``_execute_task_with_retry`` and ``_inline_spec_code_review`` /
``_get_git_diff_stat_for_review`` — the actual LLM prompt is
exercised by ``test_agent_refine_timing.py`` and the unit tests
on ``_inline_spec_code_review`` itself. We mock the seams the
caller exposes so the test does not depend on a real LLM round
trip.

TDD spec — 5 contract tests
---------------------------
1. ``test_execute_review_blocks_commit_on_high_spec``:
   ``_inline_spec_code_review`` returns ``spec_compliance="high"``,
   ``should_block=True``. The commit must NOT be invoked; the task
   must be reverted to ``pending``; ``failure_reason`` must be
   recorded.

2. ``test_execute_review_blocks_commit_on_high_quality``:
   ``_inline_spec_code_review`` returns ``code_quality="high"``,
   ``should_block=True``. Same expectations as #1.

3. ``test_execute_review_allows_commit_on_low``:
   ``_inline_spec_code_review`` returns ``spec_compliance="low"``,
   ``code_quality="low"``, ``should_block=False``. The commit MUST
   be invoked; the task status moves to ``completed``.

4. ``test_execute_review_llm_failure_continues_commit``:
   ``_inline_spec_code_review`` raises. The outer try/except in
   ``_execute_task_with_retry`` catches it, logs a warning, and
   proceeds with the commit.

5. ``test_execute_review_skipped_when_diff_empty``:
   ``_get_git_diff_stat_for_review`` returns ``""``. The inline
   review is NOT invoked at all; the commit proceeds normally.
"""

import sys
import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _build_mock_agent(max_retries: int = 1):
    """Construct a bare ``AutonomousAgent`` with all external deps mocked.

    Mirrors the harness in ``test_agent_refine_timing.py``: we bypass
    ``__init__`` because the real constructor instantiates TaskManager
    / GitManager / BackgroundManager / RetryManager / RollbackManager,
    none of which we need for driving the post-test pre-commit
    inline-review branch deterministically.
    """
    from agent import AutonomousAgent

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = Path("/tmp/_inline_review_dummy")
    agent.logger = None

    agent.retry_manager = __import__("retry_manager").RetryManager()

    # coding_tool.query default — never reached on the happy path
    # because ``_inline_spec_code_review`` is mocked at the agent
    # boundary. The default is still useful so a stray call surfaces
    # clearly in test output.
    cheat_response = "Some impl work.\n\nTEST_RESULT: PASSED\nREASON: ok\n"
    agent.coding_tool = MagicMock()
    agent.coding_tool.query = MagicMock(return_value=cheat_response)

    agent.executor = MagicMock()
    agent.executor.had_previous_timeout = MagicMock(return_value=False)

    agent.task_manager = MagicMock()
    agent.task_manager.update_task_status = MagicMock()
    agent.task_manager.record_task_failure = MagicMock()

    agent.config = MagicMock()
    agent.config.executor_system_prompt = ""
    agent.config.max_retries = max_retries

    agent.subagent_cfg = None

    agent.parse_files_from_response = MagicMock(return_value={})

    agent.git_manager = MagicMock()
    agent.git_manager.get_changed_files = MagicMock(return_value=[])

    # commit: spy — the tests assert against call_count
    agent._commit_task_changes = MagicMock()

    # Empty-output gate: tests pin the inline-review contract and must
    # not be blocked by the earlier plan's 2026-08-19 gate. Mark every
    # task's declared deliverable as already on disk so an empty diff
    # still passes the gate (the inline-review branch is exercised
    # independently via the explicit ``changed_files`` mocks below).
    agent._task_declared_files_exist = MagicMock(return_value=True)

    agent._clean_test_command = lambda cmd: cmd

    # cross_verify: default to "tests passed" so we reach the review
    # branch — this is the precondition for DP3.
    agent._cross_verify_test_result = MagicMock(return_value=(True, ""))

    # diff-stat helper — tests override as needed.
    agent._get_git_diff_stat_for_review = MagicMock(return_value="src/x.py | 5 +++")

    # inline review — tests override as needed.
    agent._inline_spec_code_review = MagicMock(
        return_value={
            "spec_compliance": "low",
            "code_quality": "low",
            "should_block": False,
            "reason": "",
        }
    )

    # Session counter required by the post-commit bookkeeping
    # (``_session_task_completed_counts``).
    agent._session_task_completed_counts = {}

    return agent


def _build_task(task_id: str = "1"):
    from task import SubTask

    return SubTask(
        id=task_id,
        title="Test task",
        description="Implement X with password hashing",
        test_command="echo test",
        status="pending",
    )


# ---------------------------------------------------------------------------
# Test 1: high spec deviation -> block commit
# ---------------------------------------------------------------------------


def test_execute_review_blocks_commit_on_high_spec():
    """LLM flags ``spec_compliance='high'`` with ``should_block=True``.

    The commit MUST NOT be invoked. The task must be reverted to
    ``pending`` and ``failure_reason`` recorded so the next run
    picks it up.
    """
    agent = _build_mock_agent()
    agent._inline_spec_code_review = MagicMock(
        return_value={
            "spec_compliance": "high",
            "code_quality": "low",
            "should_block": True,
            "reason": "spec deviation: missing password hashing",
        }
    )
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=1)

    # Task ultimately fails on this run
    assert result is False

    # HEADLINE: commit was NOT called — bad code stays out of git
    assert agent._commit_task_changes.call_count == 0, (
        f"Expected _commit_task_changes NOT to be called when "
        f"spec_compliance='high', but it was called "
        f"{agent._commit_task_changes.call_count} time(s)."
    )

    # Task reverted to pending so the next run re-picks it
    statuses = [c.args[1] for c in agent.task_manager.update_task_status.call_args_list]
    assert "pending" in statuses, (
        f"Expected task to be reverted to 'pending', got statuses: {statuses}"
    )

    # failure_reason captured
    assert agent.task_manager.record_task_failure.call_count == 1
    failure_args = agent.task_manager.record_task_failure.call_args
    failure_reason = failure_args.args[1] if len(failure_args.args) > 1 else failure_args.kwargs.get("error_msg", "")
    assert "spec deviation" in failure_reason or "password" in failure_reason, (
        f"Expected failure_reason to mention spec deviation, got: {failure_reason!r}"
    )

    # Inline review WAS invoked
    assert agent._inline_spec_code_review.call_count == 1


# ---------------------------------------------------------------------------
# Test 2: high quality issue -> block commit
# ---------------------------------------------------------------------------


def test_execute_review_blocks_commit_on_high_quality():
    """LLM flags ``code_quality='high'`` with ``should_block=True``.

    Same expectations as the high-spec case: no commit, task
    reverted to pending, failure_reason recorded.
    """
    agent = _build_mock_agent()
    agent._inline_spec_code_review = MagicMock(
        return_value={
            "spec_compliance": "low",
            "code_quality": "high",
            "should_block": True,
            "reason": "quality: plaintext password in code",
        }
    )
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=1)

    assert result is False

    assert agent._commit_task_changes.call_count == 0, (
        f"Expected _commit_task_changes NOT to be called when "
        f"code_quality='high', but it was called "
        f"{agent._commit_task_changes.call_count} time(s)."
    )

    statuses = [c.args[1] for c in agent.task_manager.update_task_status.call_args_list]
    assert "pending" in statuses, (
        f"Expected task to be reverted to 'pending', got statuses: {statuses}"
    )

    assert agent.task_manager.record_task_failure.call_count == 1
    assert agent._inline_spec_code_review.call_count == 1


# ---------------------------------------------------------------------------
# Test 3: low severity on both dimensions -> commit proceeds
# ---------------------------------------------------------------------------


def test_execute_review_allows_commit_on_low():
    """LLM returns low severity on both spec and code quality.

    ``should_block=False`` -> the commit MUST proceed; the task
    status moves to ``completed``.
    """
    agent = _build_mock_agent()
    agent._inline_spec_code_review = MagicMock(
        return_value={
            "spec_compliance": "low",
            "code_quality": "low",
            "should_block": False,
            "reason": "minor suggestions only",
        }
    )
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=1)

    assert result is True

    # HEADLINE: commit WAS called
    assert agent._commit_task_changes.call_count == 1, (
        f"Expected _commit_task_changes to be called exactly once when "
        f"review allows, got {agent._commit_task_changes.call_count}."
    )

    # Task marked completed
    statuses = [c.args[1] for c in agent.task_manager.update_task_status.call_args_list]
    assert "completed" in statuses, (
        f"Expected task to be marked 'completed', got statuses: {statuses}"
    )

    # No failure recorded on the happy path
    assert agent.task_manager.record_task_failure.call_count == 0

    assert agent._inline_spec_code_review.call_count == 1


# ---------------------------------------------------------------------------
# Test 4: LLM raises -> log warning, continue commit
# ---------------------------------------------------------------------------


def test_execute_review_llm_failure_continues_commit():
    """``_inline_spec_code_review`` raises an exception.

    The outer try/except in ``_execute_task_with_retry`` MUST
    catch it, log a warning, and proceed with the commit — the
    review is a safety net, not a hard gate.
    """
    agent = _build_mock_agent()
    agent._inline_spec_code_review = MagicMock(
        side_effect=RuntimeError("simulated LLM outage")
    )
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=1)

    # Despite the review failure, the task still completes
    assert result is True, (
        "Inline review exception must NOT block commit. The review is "
        "best-effort; outages degrade gracefully."
    )

    # HEADLINE: commit was still called
    assert agent._commit_task_changes.call_count == 1, (
        f"Expected _commit_task_changes to be called even after review "
        f"exception, got {agent._commit_task_changes.call_count}."
    )

    # Task marked completed (not reverted to pending)
    statuses = [c.args[1] for c in agent.task_manager.update_task_status.call_args_list]
    assert "completed" in statuses, (
        f"Expected task to be 'completed' despite review exception, "
        f"got statuses: {statuses}"
    )

    # No failure reason recorded — review outage is not a task failure
    assert agent.task_manager.record_task_failure.call_count == 0


# ---------------------------------------------------------------------------
# Test 5: empty diff -> skip review entirely
# ---------------------------------------------------------------------------


def test_execute_review_skipped_when_diff_empty():
    """``_get_git_diff_stat_for_review`` returns ``""``.

    The inline review MUST be skipped entirely — calling an LLM
    with no diff context is wasteful (and would grade on spec
    alone, which is what the test layer already proved). The
    commit proceeds directly.
    """
    agent = _build_mock_agent()
    agent._get_git_diff_stat_for_review = MagicMock(return_value="")
    # If the review WERE invoked, this would block — proving the
    # skip path.
    agent._inline_spec_code_review = MagicMock(
        return_value={
            "spec_compliance": "high",
            "code_quality": "high",
            "should_block": True,
            "reason": "should never reach here",
        }
    )
    task = _build_task("1")

    result = agent._execute_task_with_retry(task, max_retries=1)

    assert result is True

    # HEADLINE: inline review was NOT called
    assert agent._inline_spec_code_review.call_count == 0, (
        "Expected _inline_spec_code_review to be SKIPPED when the git "
        "diff is empty, but it was called "
        f"{agent._inline_spec_code_review.call_count} time(s)."
    )

    # Commit proceeds normally
    assert agent._commit_task_changes.call_count == 1

    statuses = [c.args[1] for c in agent.task_manager.update_task_status.call_args_list]
    assert "completed" in statuses


# ---------------------------------------------------------------------------
# DP3 (2) — Judgment-phase supplement spec/code review
# ---------------------------------------------------------------------------
#
# Background
# ----------
# DP3 action item (2) extends the inline spec/code review (introduced
# by Task 11) into the verification judgment phase. After the main
# PASSED/FAILED verdict has been settled, the agent re-prompts the
# LLM with the same review template (``INLINE_SPEC_CODE_REVIEW_PROMPT``
# from ``agent.py``) for each verification point, then attaches the
# findings to the report as ``supplement_findings``.
#
# The supplement is purely informational: it MUST NOT change the
# main ``overall_status`` / per-VP statuses. The contract is the
# following:
#
#   * ``supplement_findings`` is always a list (possibly empty).
#   * Each entry is shaped ``{"vp_id": ..., "spec_findings": [...],
#     "quality_findings": [...]}``.
#   * When the LLM fails (any exception during the supplement call)
#     the supplement degrades to ``[]`` — the main verdict is
#     untouched.
#   * When there are zero verification points, ``supplement_findings``
#     is ``[]`` (no LLM call attempted).
#
# These four tests pin those boundaries. They construct a minimal
# ``VerificationAgent`` with a mocked ``coding_tool`` so the LLM
# surface is fully under the test's control.


def _build_verification_agent_with_vps(vps):
    """Build a minimal VerificationAgent wired to a mocked coding tool.

    Returns ``(agent, mock_coding_tool)``. ``mock_coding_tool`` is a
    :class:`unittest.mock.MagicMock` whose ``query_json`` (used by
    the main judgment) and ``query`` (used by the supplement) can be
    primed by the test.

    The agent is constructed against a temporary ``plan_dir`` and
    ``project_dir`` so the persistence layer can write a verification
    report without polluting the repo. The ``start_verification_round``
    call is required because ``generate_verification_report`` uses
    the persistence manager to write the report.
    """
    from verification_agent import VerificationAgent

    tmp = tempfile.mkdtemp()
    plan_dir = Path(tmp) / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir = Path(tmp) / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    mock_coding_tool = MagicMock()
    agent = VerificationAgent(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=mock_coding_tool,
    )
    agent.start_verification_round(1)

    # The VPs under review. We pass them as both the plan payload and
    # the execution_results envelope; the judgment phase only reads
    # ``execution_results["verification_points"]`` and the matching
    # ``execution_results["execution_results"]`` rows.
    vps_payload = list(vps)
    execution_results = {
        "verification_points": vps_payload,
        "execution_results": [
            {
                "id": vp["id"],
                "status": "PASSED",
                "actual_result": "ok",
                "reasons": ["stub"],
                "evidence": "stub",
            }
            for vp in vps_payload
        ],
        "executed_at": "2026-07-07T00:00:00",
    }
    return agent, mock_coding_tool, execution_results


def _main_report_payload(vps, *, overall_status="PASSED"):
    """Build the JSON dict the main judgment LLM is expected to return.

    Mirrors the shape that ``generate_verification_report`` validates:
    ``overall_status``, ``verification_results`` (per-VP), and an
    empty ``requirement_deviations`` list. All VPs are reported as
    PASSED so the post-rewrite re-derivation does not flip the
    overall status to FAILED.
    """
    return {
        "overall_status": overall_status,
        "summary": "all green",
        "verification_results": [
            {
                "id": vp["id"],
                "status": "PASSED",
                "actual_result": "ok",
                "reasons": [],
                "evidence": "",
            }
            for vp in vps
        ],
        "requirement_deviations": [],
    }


def _supplement_json_text(*, severity="low"):
    """Build the JSON text the supplement LLM is expected to return.

    Wrapped in ``{...}`` so the supplement review's JSON parser
    (mirroring ``_inline_spec_code_review`` in ``agent.py``) can
    extract it via ``re.search(r"\\{.*\\}", text, re.DOTALL)``.
    """
    payload = {
        "spec_compliance": severity,
        "code_quality": severity,
        "should_block": severity == "high",
        "reason": f"stub supplement review ({severity})",
    }
    return "```json\n" + json.dumps(payload) + "\n```"


# ---------------------------------------------------------------------------
# Test 6: supplement_findings is populated when LLM returns findings
# ---------------------------------------------------------------------------


def test_judgment_appends_supplement_findings():
    """Mock LLM returns spec+code findings -> ``supplement_findings`` non-empty.

    Two VPs go through the judgment phase. The mocked coding tool
    returns a passing main report on ``query_json`` and a low-severity
    supplement review on ``query`` (one call per VP, with the same
    shape). After ``generate_verification_report`` returns, the
    report dict MUST contain a ``supplement_findings`` list with one
    entry per VP, each entry carrying the spec/quality findings.

    Boundary contract pinned:
      * 2 VPs in plan -> 2 entries in ``supplement_findings``.
      * Each entry has ``vp_id``, ``spec_findings`` (list), and
        ``quality_findings`` (list).
    """
    vps = [
        {"id": "VP-001", "title": "feature X", "priority": "high",
         "verification_method": "automated_test",
         "related_prd_criteria": "X works",
         "expected_result": "X passes"},
        {"id": "VP-002", "title": "feature Y", "priority": "medium",
         "verification_method": "automated_test",
         "related_prd_criteria": "Y works",
         "expected_result": "Y passes"},
    ]
    agent, mock_coding_tool, execution_results = _build_verification_agent_with_vps(vps)

    # Main judgment: PASSED for both VPs.
    mock_coding_tool.query_json.return_value = _main_report_payload(vps)

    # Supplement review: one call per VP, all low severity.
    mock_coding_tool.query.return_value = _supplement_json_text(severity="low")

    report = agent.generate_verification_report(execution_results)

    # -- Headline: supplement_findings present and populated ---------
    assert "supplement_findings" in report, (
        "Expected verification report to carry a 'supplement_findings' "
        "field populated by the post-judgment spec/code review."
    )
    assert isinstance(report["supplement_findings"], list)
    assert len(report["supplement_findings"]) == 2, (
        f"Expected 2 supplement entries (one per VP), got "
        f"{len(report['supplement_findings'])}"
    )

    by_vp = {entry["vp_id"]: entry for entry in report["supplement_findings"]}
    assert "VP-001" in by_vp and "VP-002" in by_vp
    for entry in by_vp.values():
        assert isinstance(entry["spec_findings"], list)
        assert isinstance(entry["quality_findings"], list)


# ---------------------------------------------------------------------------
# Test 7: supplement does NOT change the main verdict
# ---------------------------------------------------------------------------


def test_judgment_supplement_does_not_change_verdict():
    """Main verdict PASSED + supplement 'high' severity -> verdict stays PASSED.

    The supplement review is intentionally lossy and advisory. Even
    when the LLM flags a high-severity issue in the supplement (which
    would block the inline pre-commit review), the judgment-phase
    supplement MUST NOT change the main verdict (``overall_status``)
    or any per-VP status. This is the contract DP3 action item (2)
    pins: "对每个验证点输出 supplement_findings 但不改变 PASSED/FAILED 主判定".

    Boundary contract pinned:
      * Main judgment LLM says PASSED for all VPs.
      * Supplement LLM says "high" severity for one VP.
      * Final report: ``overall_status == "PASSED"``, and per-VP
        statuses in ``verification_results`` are still PASSED.
      * The high-severity signal is captured ONLY in
        ``supplement_findings``.
    """
    vps = [
        {"id": "VP-001", "title": "feature X", "priority": "high",
         "verification_method": "automated_test",
         "related_prd_criteria": "X works",
         "expected_result": "X passes"},
    ]
    agent, mock_coding_tool, execution_results = _build_verification_agent_with_vps(vps)

    # Main judgment: clean PASSED.
    mock_coding_tool.query_json.return_value = _main_report_payload(
        vps, overall_status="PASSED",
    )

    # Supplement: HIGH severity on both spec_compliance and code_quality.
    # should_block=true (per the inline-review contract) but the
    # judgment-phase supplement must NOT honour this — it's advisory.
    mock_coding_tool.query.return_value = _supplement_json_text(severity="high")

    report = agent.generate_verification_report(execution_results)

    # -- Headline: main verdict unchanged -----------------------------
    assert report["overall_status"] == "PASSED", (
        f"Supplement 'high' severity must NOT change overall_status; "
        f"got overall_status={report['overall_status']!r}"
    )
    statuses = [vr.get("status") for vr in report["verification_results"]]
    assert all(s == "PASSED" for s in statuses), (
        f"Per-VP statuses must remain PASSED despite supplement "
        f"'high'; got {statuses!r}"
    )

    # -- And the high signal is captured in the supplement itself -----
    assert "supplement_findings" in report
    assert len(report["supplement_findings"]) == 1
    entry = report["supplement_findings"][0]
    assert entry["vp_id"] == "VP-001"
    # At least one of the two findings must carry the high-severity
    # signal (the contract is "spec_findings" and "quality_findings"
    # both can carry a string "high" or a structured entry whose
    # severity field is "high"). We accept either shape.
    combined = (entry["spec_findings"], entry["quality_findings"])
    flat = []
    for sublist in combined:
        for item in sublist:
            if isinstance(item, str):
                flat.append(item)
            elif isinstance(item, dict):
                flat.append(str(item.get("severity", "")))
    assert any("high" in str(x).lower() for x in flat), (
        f"Expected the supplement entry to surface the 'high' severity "
        f"signal in spec_findings or quality_findings; got "
        f"spec_findings={entry['spec_findings']!r}, "
        f"quality_findings={entry['quality_findings']!r}"
    )


# ---------------------------------------------------------------------------
# Test 8: LLM failure -> supplement_findings = [] (main verdict intact)
# ---------------------------------------------------------------------------


def test_judgment_llm_failure_empty_supplement():
    """Mock LLM raises during supplement -> ``supplement_findings = []``.

    The supplement is best-effort. When the LLM call raises (network
    blip, API error, JSON parse), the judgment phase MUST degrade
    gracefully: the main verdict is still produced, but the
    supplement is empty. The downstream consumer (``generate_verification_report``
    returns a normal report) does not see a partial or malformed
    supplement.

    Boundary contract pinned:
      * Supplement LLM call raises on the first attempt.
      * Final report: ``supplement_findings == []``.
      * Main verdict is still PASSED (the supplement failure must NOT
        downgrade the report).
    """
    from coding_tool import ApiError

    vps = [
        {"id": "VP-001", "title": "feature X", "priority": "high",
         "verification_method": "automated_test",
         "related_prd_criteria": "X works",
         "expected_result": "X passes"},
    ]
    agent, mock_coding_tool, execution_results = _build_verification_agent_with_vps(vps)

    # Main judgment works fine.
    mock_coding_tool.query_json.return_value = _main_report_payload(vps)

    # Supplement LLM raises (the contract: any exception -> []).
    mock_coding_tool.query.side_effect = ApiError(
        "simulated LLM outage", status="overloaded",
    )

    report = agent.generate_verification_report(execution_results)

    # -- Headline: supplement degraded to [] --------------------------
    assert "supplement_findings" in report, (
        "supplement_findings MUST be present (even when empty) so "
        "downstream consumers can rely on the field existing."
    )
    assert report["supplement_findings"] == [], (
        f"Expected supplement_findings == [] when LLM raises; got "
        f"{report['supplement_findings']!r}"
    )

    # Main verdict intact
    assert report["overall_status"] == "PASSED", (
        f"Main verdict must be unaffected by supplement LLM failure; "
        f"got overall_status={report['overall_status']!r}"
    )


# ---------------------------------------------------------------------------
# Test 9: zero VPs -> supplement_findings = [] (no LLM call)
# ---------------------------------------------------------------------------


def test_judgment_no_findings_when_vps_empty():
    """vps=[] -> ``supplement_findings = []`` (no LLM call attempted).

    When the judgment phase is invoked with an empty verification
    plan (no VPs to evaluate), there is nothing to review. The
    supplement LLM call MUST be skipped entirely (no wasted tokens)
    and ``supplement_findings`` MUST be the empty list.

    Boundary contract pinned:
      * ``verification_points`` and ``execution_results`` are both
        empty.
      * ``coding_tool.query`` is NEVER called for the supplement.
      * Final report: ``supplement_findings == []``.
    """
    agent, mock_coding_tool, execution_results = _build_verification_agent_with_vps([])

    # Main judgment: empty plan -> minimal passing report.
    mock_coding_tool.query_json.return_value = {
        "overall_status": "PASSED",
        "summary": "no VPs",
        "verification_results": [],
        "requirement_deviations": [],
    }

    report = agent.generate_verification_report(execution_results)

    # -- Headline: supplement_findings == [] -------------------------
    assert "supplement_findings" in report
    assert report["supplement_findings"] == [], (
        f"Expected supplement_findings == [] for an empty plan; got "
        f"{report['supplement_findings']!r}"
    )

    # Supplement LLM was NOT called at all (zero VPs -> zero LLM
    # round-trips). The main judgment may still have used query_json,
    # but query (the supplement surface) MUST NOT have been touched.
    assert mock_coding_tool.query.call_count == 0, (
        f"Expected coding_tool.query to be unused for an empty plan, "
        f"but it was called {mock_coding_tool.query.call_count} time(s)."
    )

"""TDD tests for ``_inline_spec_code_review`` integration in ``agent.py``.

Background
----------
DP3 requires a lightweight spec+code self-check at the end of
``_execute_task_with_retry``: after the test command exits 0 (proved by
``_cross_verify_test_result``) and before the git checkpoint commit, the
agent runs an inline LLM review. The review grades the implementation
along two dimensions — ``spec_compliance`` and ``code_quality`` — and
emits a ``should_block`` flag. High-severity deviations revert the task
to ``pending`` and write a ``failure_reason`` so the next run picks up
the corrective work. Low-severity or unparseable output commits the
task normally so an LLM outage never blocks the legitimate flow.

This module pins three contracts:

  1. ``test_inline_review_passes_clean_task``
     When the inline LLM returns a low-severity verdict (both
     ``spec_compliance`` and ``code_quality`` non-"high" and
     ``should_block == False``), the task completes normally:
     ``_execute_task_with_retry`` returns ``True``, the task status is
     ``"completed"``, and ``_commit_task_changes`` is called exactly
     once.

  2. ``test_inline_review_blocks_high_severity``
     When the inline LLM returns a high-severity verdict
     (``should_block == True``), the task is reverted to
     ``pending`` with a ``failure_reason`` set to the review's
     reason text: ``_execute_task_with_retry`` returns ``False``,
     ``_commit_task_changes`` is NOT called, ``record_task_failure``
     is called once with the review's reason, and the task status is
     ``"pending"``.

  3. ``test_inline_review_fallback_on_llm_failure``
     When the inline LLM raises any exception (network / API / parse
     error that escapes the inner try/except), the review degrades to
     ``should_block = False`` (graceful fallback) and the task
     commits normally — ``_execute_task_with_retry`` returns
     ``True``, the task status is ``"completed"``, and
     ``_commit_task_changes`` is called once. The reviewer is a
     safety net, not a gate.

TDD spec (3 tests, module-level pytest functions):

  - ``test_inline_review_passes_clean_task``:
        LLM returns ``{"spec_compliance": "low", "code_quality": "low",
        "should_block": False, "reason": "ok"}`` → task commits
        normally (status="completed", _commit_task_changes called
        once).
  - ``test_inline_review_blocks_high_severity``:
        LLM returns ``{"spec_compliance": "high", "code_quality":
        "medium", "should_block": True, "reason": "实现遗漏密码哈希"}``
        → task reverts to pending, _commit_task_changes NOT called,
        record_task_failure called once.
  - ``test_inline_review_fallback_on_llm_failure``:
        LLM raises RuntimeError("simulated LLM outage") → task commits
        normally (graceful fallback), _commit_task_changes called
        once.

Final line emitted by the wrapper:
  - On success: ``TEST_RESULT: PASSED``
  - On failure: ``TEST_RESULT: FAILED`` + ``REASON: ...``

Note on test wiring
-------------------
These tests exercise ``_execute_task_with_retry`` (NOT
``_inline_spec_code_review`` directly) because the spec contract is
about the **integration** — what happens to the task when the review
returns a particular verdict. We:

  * build a real :class:`AutonomousAgent` against a real git
    project_dir (so ``_get_git_diff_stat_for_review`` runs ``git diff
    --stat`` successfully);
  * stub ``coding_tool.query`` with a small stateful helper that
    returns a ``TEST_RESULT: PASSED`` response on the first call
    (the main coding call) and the canned review verdict on the
    second call (the inline review call);
  * patch ``_cross_verify_test_result`` to return ``(True, "")`` so
    we cross the test gate without spawning a real subprocess;
  * patch ``_commit_task_changes`` to record the call so we can
    assert whether it happened.

The ``_session_task_completed_counts`` attribute is initialised by
``_run_async`` in production; since we call ``_execute_task_with_retry``
directly we set it manually (an empty dict) to keep the success path
side-effect-free.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


# ---------------------------------------------------------------------------
# Helpers: project_dir + git init + tasks.json
# ---------------------------------------------------------------------------


def _git_init(project_dir: Path) -> None:
    """Initialise a real git repo inside ``project_dir`` so ``git diff
    --stat`` (called by ``_get_git_diff_stat_for_review``) returns a
    non-empty string."""
    if shutil.which("git") is None:
        pytest.skip("git binary not on PATH; skipping inline review tests")
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main"],
        cwd=str(project_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    # Set a local user identity so commit/email-keyed git commands
    # don't complain. These never appear in history because the tests
    # don't push.
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(project_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(project_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    # Seed a baseline commit so HEAD is valid and ``git diff --stat``
    # has something to compare against.
    (project_dir / "README.md").write_text("# baseline\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(project_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "baseline"],
        cwd=str(project_dir),
        check=True,
        capture_output=True,
        text=True,
    )


def _write_tasks(project_dir: Path, tasks: list) -> None:
    """Write ``tasks.json`` with the given task dicts."""
    payload = {"requirement": "DP3 inline spec+code review TDD fixture", "tasks": tasks}
    (project_dir / "tasks.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8",
    )


@pytest.fixture
def project_dir(tmp_path):
    """A real git project_dir suitable for AutonomousAgent."""
    pd = tmp_path / "project"
    pd.mkdir()
    _git_init(pd)
    return pd


# ---------------------------------------------------------------------------
# Stub coding_tool: returns a main-coder response on the first call and
# a canned review verdict on the second call.
# ---------------------------------------------------------------------------


class _ScriptedCodingTool:
    """Stub for the LLM coding tool.

    ``AutonomousAgent._execute_task_with_retry`` calls
    ``coding_tool.query(...)`` twice on the happy path:

      * call #1 (main coding): inside the per-attempt loop — returns
        ``coder_response`` which carries ``TEST_RESULT: PASSED`` and
        a ``FILE: ... ``` ... ``` `` block so ``parse_files_from_response``
        extracts a writable file.
      * call #2 (inline review): inside ``_inline_spec_code_review`` —
        returns a JSON verdict matching the spec shape.

    The stub also implements ``query_json`` (used by plan/refine
    flows, but those aren't exercised here) by returning a sentinel
    dict — the tests never trigger plan/refine paths.
    """

    def __init__(
        self,
        review_verdict: dict | None = None,
        review_should_raise: Exception | None = None,
    ):
        self.review_verdict = review_verdict
        self.review_should_raise = review_should_raise
        self.calls: list = []
        # 1-based call counter — call #1 is the main coder, calls #2+
        # are inline-review (only call #2 in the happy path).
        self._call_index = 0

    def _main_coder_response(self) -> str:
        """A coder response that yields a writable file (so the agent's
        ``parse_files_from_response`` returns a non-empty dict) and
        contains ``TEST_RESULT: PASSED``."""
        return (
            "I'll implement the requested change.\n\n"
            "FILE: src/example.py\n"
            "```python\n"
            "def hello():\n"
            "    return 'hi'\n"
            "```\n\n"
            "Tested locally — works.\n\n"
            "TEST_RESULT: PASSED\n"
        )

    def query(self, prompt: str, system_instruction=None, timeout=None, **kwargs):
        self._call_index += 1
        self.calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
            "timeout": timeout,
            "kwargs": kwargs,
        })
        if self._call_index == 1:
            # Main coder call. Return a response that passes
            # ``_parse_test_result`` and yields a writable file.
            return self._main_coder_response()
        # Inline review call.
        if self.review_should_raise is not None:
            raise self.review_should_raise
        if self.review_verdict is None:
            # Defensive default — keep the run moving.
            return json.dumps({
                "spec_compliance": "low",
                "code_quality": "low",
                "should_block": False,
                "reason": "(stub default)",
            }, ensure_ascii=False)
        return json.dumps(self.review_verdict, ensure_ascii=False)

    def query_json(self, prompt: str, system_instruction=None):
        """Plan/refine entry point — unused by these tests."""
        self.calls.append({
            "prompt": prompt,
            "system_instruction": system_instruction,
            "kwargs": {"query_json": True},
        })
        # Should never be exercised by ``_execute_task_with_retry`` —
        # assert loudly if it is, so test breakage surfaces immediately.
        raise AssertionError(
            "query_json must NOT be called inside _execute_task_with_retry; "
            "the stub is wired for the happy path only."
        )


# ---------------------------------------------------------------------------
# Agent construction helper
# ---------------------------------------------------------------------------


def _build_agent(project_dir: Path, coding_tool) -> "object":
    """Build a minimal :class:`AutonomousAgent` bound to ``project_dir``.

    We construct the agent against a real (in-memory) project_dir and
    pass our scripted coding_tool. ``_session_task_completed_counts``
    is initialised manually because ``_execute_task_with_retry`` reads
    from it on the success path and it would otherwise be unset
    (production initialises it inside ``_run_async``).
    """
    from agent import AutonomousAgent

    agent = AutonomousAgent(
        requirement="DP3 inline spec+code review TDD fixture",
        project_dir=project_dir,
        coding_tool=coding_tool,
        logger=None,
    )
    agent._session_task_completed_counts = {}
    return agent


# ---------------------------------------------------------------------------
# Test 1: low severity → task commits normally
# ---------------------------------------------------------------------------


def test_inline_review_passes_clean_task(project_dir, tmp_path):
    """Inline LLM returns ``{should_block: False, ...}`` → task commits.

    Contract:

      * The main coder call returns a response with
        ``TEST_RESULT: PASSED`` and a writable file block.
      * The inline review call returns a low-severity verdict
        (``spec_compliance == "low"``, ``code_quality == "low"``,
        ``should_block == False``).
      * ``_cross_verify_test_result`` is patched to return
        ``(True, "")`` so the test gate is crossed without spawning
        a real subprocess.
      * The expected post-conditions hold:
          - ``_execute_task_with_retry`` returns ``True``,
          - the task's on-disk ``status`` is ``"completed"``,
          - ``_commit_task_changes`` is called exactly once.
    """
    review_verdict = {
        "spec_compliance": "low",
        "code_quality": "low",
        "should_block": False,
        "reason": "实现完整，未发现严重偏离",
    }
    coding_tool = _ScriptedCodingTool(review_verdict=review_verdict)
    agent = _build_agent(project_dir, coding_tool)

    from task import SubTask

    task = SubTask(
        id="T1",
        title="实现 example 模块",
        description="实现一个简单的 example 模块",
        # 2026-09-13: ``exit 1`` so the 2026-08-24 pre-flight gate
        # (run test_command before the subagent; skip on rc=0) falls
        # through to the real subagent path these tests exercise.
        test_command="exit 1",
        status="pending",
        project_dir=str(project_dir),
        # 2026-09-13: read-only sentinel — these tests exercise the
        # inline review gate, not the files_to_modify fill loop (an
        # empty list now fails step 3 and drives the subagent fill).
        files_to_modify=["__NO_FILE_CHANGES__"],
    )
    _write_tasks(project_dir, [task.model_dump()])

    # Reload via the real path so the agent's task list is consistent
    # with disk.
    agent._load_tasks()
    loaded_task = next(t for t in agent._active_tasks if t.id == "T1")

    commit_calls: list = []

    def _record_commit(t, files):
        commit_calls.append((t.id, list(files)))

    with patch.object(agent, "_cross_verify_test_result", return_value=(True, "")), \
         patch.object(agent, "_commit_task_changes", side_effect=_record_commit):
        result = agent._execute_task_with_retry(loaded_task, max_retries=1, timeout=60)

    # Verdict: success.
    assert result is True, (
        "expected _execute_task_with_retry to return True for a clean review; "
        f"got {result!r}. Calls={coding_tool.calls!r}"
    )

    # The commit must have happened exactly once.
    assert len(commit_calls) == 1, (
        f"expected _commit_task_changes to be called once for a clean review; "
        f"got {len(commit_calls)} call(s): {commit_calls!r}"
    )
    assert commit_calls[0][0] == "T1", (
        f"expected _commit_task_changes to be called for T1; got {commit_calls[0]!r}"
    )

    # The inline review's coding_tool call must have happened (call #2).
    # call #1 = main coder, call #2 = inline review.
    assert len(coding_tool.calls) >= 2, (
        f"expected at least 2 coding_tool.query() calls (main coder + inline "
        f"review); got {len(coding_tool.calls)}: {coding_tool.calls!r}"
    )
    review_call = coding_tool.calls[1]
    # The inline review's prompt should mention the task description
    # and the spec/quality dimensions so the LLM has the right context.
    assert "spec_compliance" in review_call["prompt"], (
        "inline review prompt must mention the spec_compliance dimension; "
        f"prompt head: {review_call['prompt'][:200]!r}"
    )
    assert "实现一个简单的 example 模块" in review_call["prompt"], (
        "inline review prompt must include the task description; "
        f"prompt head: {review_call['prompt'][:200]!r}"
    )

    # On-disk task status must be "completed".
    agent.task_manager.load_tasks()  # reload from disk
    persisted = next(t for t in agent.task_manager.tasks if t.id == "T1")
    assert persisted.status == "completed", (
        f"expected on-disk task status 'completed' for a clean review; "
        f"got {persisted.status!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: high severity → task reverts to pending
# ---------------------------------------------------------------------------


def test_inline_review_blocks_high_severity(project_dir, tmp_path):
    """Inline LLM returns ``{should_block: True, ...}`` → task reverts to pending.

    Contract:

      * The main coder call still succeeds (so we cross the test
        gate and reach the inline review branch).
      * The inline review call returns a high-severity verdict with
        ``should_block == True`` and a non-empty ``reason``.
      * ``_cross_verify_test_result`` is patched to return
        ``(True, "")`` so the test gate is crossed.
      * The expected post-conditions hold:
          - ``_execute_task_with_retry`` returns ``False``,
          - the task's on-disk ``status`` is ``"pending"`` (the
            caller reverts the task so the next run picks it up),
          - ``_commit_task_changes`` is NOT called,
          - ``record_task_failure`` is called once with the
            review's reason text.
    """
    review_reason = "实现遗漏密码哈希，spec 要求必须哈希存储"
    review_verdict = {
        "spec_compliance": "high",
        "code_quality": "medium",
        "should_block": True,
        "reason": review_reason,
    }
    coding_tool = _ScriptedCodingTool(review_verdict=review_verdict)
    agent = _build_agent(project_dir, coding_tool)

    from task import SubTask

    task = SubTask(
        id="T2",
        title="实现 register 函数",
        description="实现一个 register 函数，要求对密码做哈希",
        # 2026-09-13: ``exit 1`` so the 2026-08-24 pre-flight gate
        # (run test_command before the subagent; skip on rc=0) falls
        # through to the real subagent path these tests exercise.
        test_command="exit 1",
        status="pending",
        project_dir=str(project_dir),
        # 2026-09-13: read-only sentinel — these tests exercise the
        # inline review gate, not the files_to_modify fill loop (an
        # empty list now fails step 3 and drives the subagent fill).
        files_to_modify=["__NO_FILE_CHANGES__"],
    )
    _write_tasks(project_dir, [task.model_dump()])

    agent._load_tasks()
    loaded_task = next(t for t in agent._active_tasks if t.id == "T2")

    commit_calls: list = []
    failure_calls: list = []

    def _record_commit(t, files):
        commit_calls.append((t.id, list(files)))

    original_record_failure = agent.task_manager.record_task_failure

    def _record_failure(task_id: str, error: str):
        failure_calls.append((task_id, error))
        # Delegate to the real implementation so the on-disk row is
        # updated, then we re-read it for assertions.
        return original_record_failure(task_id, error)

    with patch.object(agent, "_cross_verify_test_result", return_value=(True, "")), \
         patch.object(agent, "_commit_task_changes", side_effect=_record_commit), \
         patch.object(agent.task_manager, "record_task_failure", side_effect=_record_failure):
        result = agent._execute_task_with_retry(loaded_task, max_retries=1, timeout=60)

    # Verdict: failure.
    assert result is False, (
        "expected _execute_task_with_retry to return False when the inline "
        f"review says should_block=True; got {result!r}"
    )

    # The commit must NOT have happened.
    assert commit_calls == [], (
        f"expected _commit_task_changes NOT to be called when should_block=True; "
        f"got {commit_calls!r}"
    )

    # record_task_failure must have been called once with the review's reason.
    assert len(failure_calls) == 1, (
        f"expected record_task_failure to be called once for should_block=True; "
        f"got {len(failure_calls)} call(s): {failure_calls!r}"
    )
    assert failure_calls[0][0] == "T2", (
        f"expected record_task_failure to be called for T2; got {failure_calls[0]!r}"
    )
    assert review_reason in failure_calls[0][1], (
        f"expected failure_reason to contain the review reason "
        f"{review_reason!r}; got {failure_calls[0][1]!r}"
    )

    # On-disk task status must be "pending" (caller reverts after record).
    agent.task_manager.load_tasks()
    persisted = next(t for t in agent.task_manager.tasks if t.id == "T2")
    assert persisted.status == "pending", (
        f"expected on-disk task status 'pending' after should_block=True; "
        f"got {persisted.status!r}"
    )

    # failure_reason must contain the review reason.
    assert persisted.failure_reason and review_reason in persisted.failure_reason, (
        f"expected failure_reason to contain the review reason "
        f"{review_reason!r}; got {persisted.failure_reason!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: LLM failure → graceful fallback, task commits normally
# ---------------------------------------------------------------------------


def test_inline_review_fallback_on_llm_failure(project_dir, tmp_path):
    """Inline LLM raises → graceful fallback, task commits normally.

    Contract:

      * The main coder call still succeeds.
      * The inline review call raises a ``RuntimeError("simulated
        LLM outage")`` — this models a network / API / parse error
        that escapes the inner ``try/except`` inside
        ``_inline_spec_code_review``. The outer ``except`` at the
        call site (``_execute_task_with_retry``) must catch it and
        degrade to ``should_block = False`` so the commit happens.
      * ``_cross_verify_test_result`` is patched to return
        ``(True, "")`` so the test gate is crossed.
      * The expected post-conditions hold:
          - ``_execute_task_with_retry`` returns ``True``,
          - the task's on-disk ``status`` is ``"completed"``,
          - ``_commit_task_changes`` is called exactly once.
    """
    coding_tool = _ScriptedCodingTool(
        review_should_raise=RuntimeError("simulated LLM outage"),
    )
    agent = _build_agent(project_dir, coding_tool)

    from task import SubTask

    task = SubTask(
        id="T3",
        title="实现 graceful 模块",
        description="实现 graceful 模块，LLM 评审必须失败降级",
        # 2026-09-13: ``exit 1`` so the 2026-08-24 pre-flight gate
        # (run test_command before the subagent; skip on rc=0) falls
        # through to the real subagent path these tests exercise.
        test_command="exit 1",
        status="pending",
        project_dir=str(project_dir),
        # 2026-09-13: read-only sentinel — these tests exercise the
        # inline review gate, not the files_to_modify fill loop (an
        # empty list now fails step 3 and drives the subagent fill).
        files_to_modify=["__NO_FILE_CHANGES__"],
    )
    _write_tasks(project_dir, [task.model_dump()])

    agent._load_tasks()
    loaded_task = next(t for t in agent._active_tasks if t.id == "T3")

    commit_calls: list = []

    def _record_commit(t, files):
        commit_calls.append((t.id, list(files)))

    with patch.object(agent, "_cross_verify_test_result", return_value=(True, "")), \
         patch.object(agent, "_commit_task_changes", side_effect=_record_commit):
        result = agent._execute_task_with_retry(loaded_task, max_retries=1, timeout=60)

    # Verdict: success (graceful fallback — must NEVER block on an
    # LLM outage).
    assert result is True, (
        "expected _execute_task_with_retry to return True even when the inline "
        f"review LLM raises; got {result!r}. The reviewer is a safety net, not "
        "a gate — an outage must not block the commit."
    )

    # The commit must have happened exactly once.
    assert len(commit_calls) == 1, (
        f"expected _commit_task_changes to be called once on graceful fallback; "
        f"got {len(commit_calls)} call(s): {commit_calls!r}"
    )
    assert commit_calls[0][0] == "T3", (
        f"expected _commit_task_changes to be called for T3; got {commit_calls[0]!r}"
    )

    # The inline review call must have happened and raised.
    assert len(coding_tool.calls) >= 2, (
        f"expected at least 2 coding_tool.query() calls (main coder + inline "
        f"review that raised); got {len(coding_tool.calls)}: {coding_tool.calls!r}"
    )

    # On-disk task status must be "completed".
    agent.task_manager.load_tasks()
    persisted = next(t for t in agent.task_manager.tasks if t.id == "T3")
    assert persisted.status == "completed", (
        f"expected on-disk task status 'completed' on graceful fallback; "
        f"got {persisted.status!r}"
    )
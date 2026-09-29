"""Contract: the framework's report trailer is not a spec violation (2026-09-22).

Background — the 0921 runaway
-----------------------------
Every executor subagent is REQUIRED to end its report with::

    TEST_RESULT: PASSED

(see ``test_instruction`` in ``AutonomousAgent._execute_task_with_retry``
— "CRITICAL: Your response MUST end with a TEST_RESULT line"). The
executor parses that line to cross-check the subagent's claim against the
real ``test_command`` exit code.

The audit second pass hands the spec plus the subagent's answer to a
reviewer LLM and asks whether the spec's acceptance criteria are met.
Before this fix the answer went over verbatim — including the mandatory
trailer. On a production plan the refiner had (in
response to a failure reason that merely *mentioned* the trailer) written
a spec clause "绝对禁止输出任何自称通过字样", the reviewer enforced it
literally, and the outcome was:

  * 22 of 24 task failures attributed to this one shape;
  * the task split 10 levels deep (``10-2-3-4-1-1-1-1-1-1``);
  * the plan grew 20 → 59 tasks and stalled for 5 hours.

Two guarantees are pinned here:

  1. :func:`strip_framework_result_trailer` removes a trailer that is
     genuinely at the end of a report, and leaves everything else alone.
  2. ``_audit_task_second_pass`` never hands the reviewer the trailer.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent import strip_framework_result_trailer  # noqa: E402
from task import SubTask  # noqa: E402


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


class TestStripTrailer:
    def test_removes_a_trailing_passed_line(self):
        text = "did the work\n\nTEST_RESULT: PASSED"
        assert strip_framework_result_trailer(text) == "did the work"

    def test_removes_failed_line_and_reason(self):
        text = (
            "tried but the probe still returns None\n"
            "TEST_RESULT: FAILED\n"
            "REASON: probe returned None"
        )
        assert (
            strip_framework_result_trailer(text)
            == "tried but the probe still returns None"
        )

    def test_removes_markdown_decorated_trailer(self):
        text = "summary\n**TEST_RESULT: PASSED**"
        assert strip_framework_result_trailer(text) == "summary"

    def test_trailing_whitespace_after_trailer_is_tolerated(self):
        text = "summary\nTEST_RESULT: PASSED\n\n   "
        assert strip_framework_result_trailer(text) == "summary"

    def test_text_without_a_trailer_is_unchanged(self):
        text = "EXIT_CODE=0\nall 5 guards green"
        assert strip_framework_result_trailer(text) == text

    def test_a_mid_text_mention_is_preserved(self):
        """Only a genuine end-of-report trailer is stripped — the reviewer
        still needs the body verbatim, and the system instruction tells it
        how to read a stray mention."""
        text = "earlier the worker wrote TEST_RESULT: PASSED by mistake\nnow it does not"
        assert strip_framework_result_trailer(text) == text

    def test_none_and_empty_pass_through(self):
        assert strip_framework_result_trailer(None) is None
        assert strip_framework_result_trailer("") == ""


# ---------------------------------------------------------------------------
# The audit prompt
# ---------------------------------------------------------------------------


class _CapturingCodingTool:
    """Records the prompt the audit second pass builds."""

    def __init__(self, verdict: str):
        self.prompts: list = []
        self._verdict = verdict

    def query(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        return self._verdict

    def query_json(self, *args, **kwargs):  # pragma: no cover - unused
        return {}


def _audit_style_task() -> SubTask:
    """Audit-style + no runnable command → the second pass runs."""
    return SubTask(
        id="10-2-3",
        title="审计：pyo3 扩展陈旧导致 test-report mismatch 的定界",
        description="产出书面定界结论",
        test_command="",
        test_commands=[],
        files_to_modify=["__NO_FILE_CHANGES__"],
        depends_on=[],
    )


def _build_agent(project_dir: Path, coding_tool):
    from agent import AutonomousAgent

    return AutonomousAgent(
        requirement="audit trailer",
        project_dir=project_dir,
        coding_tool=coding_tool,
        logger=None,
    )


def test_audit_prompt_never_contains_the_framework_trailer(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=project, check=True, capture_output=True
    )
    tool = _CapturingCodingTool("AUDIT_VERDICT: PASSED")
    agent = _build_agent(project, tool)

    answer = (
        "扩展陈旧是根因：venv1 重建过、.venv 没重建。\n"
        "PYTEST_EXIT=1 有原文证据。\n"
        "TEST_RESULT: PASSED"
    )
    passed, _reason = agent._audit_task_second_pass(
        _audit_style_task(), answer
    )

    assert passed is True
    assert tool.prompts, "the second pass must have queried the reviewer"
    sent = tool.prompts[0]
    assert "TEST_RESULT" not in sent, (
        "the framework's mandatory trailer must not reach the reviewer — a "
        "spec clause forbidding it would make every answer fail"
    )
    # The body itself must survive intact.
    assert "PYTEST_EXIT=1" in sent

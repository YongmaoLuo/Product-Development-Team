"""Acceptance: a verifier timeout is INCONCLUSIVE — never "the subagent lied".

What this file is
-----------------
The unit tests that shipped with tasks 1-5 each pin one piece of the
verifier-timeout contract: the budget tiers, the new ``verifier_timeout``
kind and its remedy, the inconclusive verdict, the escalation ladder, the
executor's dispatch on the remedy.  Each of them exercises ONE function.

This file is the acceptance pass over the whole thing: it drives the
real production path — ``AutonomousAgent._execute_task_with_retry`` with
the real ``_cross_verify_test_result`` inside it — and asserts the three
properties that only matter when the pieces are assembled:

1. **Negative control A (test decision point 1).**  A verification that
   ran out of its budget produces the ``verifier_timeout`` verdict, the
   ``test_cross_verify_inconclusive`` event, and a failure classified as
   ``verifier_timeout``.  The contrast case is the OLD wording for the
   same facts — ``Subagent test-report mismatch: …`` — which is a report
   rejection and must still classify as ``report_format``.  The new kind
   has to be NARROW: if it swallowed the old wording too, then it would
   be a catch-all and the classification would have stopped meaning
   anything.

2. **The guard is not weakened (test decision point 2).**  A real
   non-zero exit with a PASSED claim is still a lie: the
   ``test_cross_verify_cheat_detected`` event still fires, the reason
   still classifies as ``report_format``, and the verification is NOT
   re-run on the ladder (a real exit code is an answer, not a symptom).
   The fix for "a timeout was called a lie" must not become "a lie was
   called a timeout".

3. **One task, three verifications (test decision point 5).**  End to end
   through the real executor: the task runs ONCE (the subagent is not
   re-dispatched — a second run would spend the very occurrence the
   split gate reads as "this task is too big"), the VERIFICATION runs
   three times (the budget ladder: base → heavy → 2×heavy), the task
   fails, and the refiner is never called (there is no verdict to
   decompose).

The budget is configuration, so it is pinned to a small value
(``PDT_CROSS_VERIFY_TIMEOUT_SEC`` / ``PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC``
= 5s) rather than to the 600s/1800s defaults: the ladder's SHAPE is
what is under test, and a five-second budget keeps the test fast without
changing the shape.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import agent as agent_module  # noqa: E402
from agent import AutonomousAgent  # noqa: E402
from failure_classification import (  # noqa: E402
    REPORT_FORMAT,
    REVERIFY_WITH_MORE_BUDGET,
    VERIFIER_TIMEOUT,
    classify_failure,
)


# ---------------------------------------------------------------------------
# The suite must not inherit the executor's secret descriptors
# ---------------------------------------------------------------------------
#
# The executor hands a secret to the process it runs a task in through a
# pipe: the parent publishes the value on the write end and names the read
# end in the environment (``PDT_SECRET_FD_<NAME>``).  That is the handoff
# ``credentials.read_secret_fd`` reads from.
#
# A pytest run launched by that executor inherits the variable -- and the
# number in it names a descriptor of the PARENT, which in a test process is
# one of that process's own: under xdist, the worker's execnet channel.
#
# Reading it does not fail fast, it BLOCKS.  The pipe never carries the
# payload, so the read waits for a writer that is not coming; the worker is
# killed by the test timeout, xdist replaces it, the replacement picks the
# same test up and blocks the same way -- and the suite never finishes.
# The failure surfaces as a timeout, not as the misconfiguration it is.
#
# The drop happens here, at import, because this file is the only one this
# task may write.  The natural home for it is
# ``backend/_pytest_hermetic_env.py`` -- whose whole job is to keep ambient
# deployment state out of the suite -- and it should be moved there as soon
# as a task is declared to change that file.  What matters now is that the
# suite does not answer to a descriptor number it does not own.

SECRET_FD_ENV_PREFIX = "PDT_SECRET_FD_"


def _drop_inherited_secret_descriptors() -> List[str]:
    """Remove the executor's inherited secret-descriptor variables.

    Returns the names that were dropped, so a caller can report them.
    Only the descriptor variables go: ``PDT_SECRET_READER_PATH`` names a
    binary rather than a descriptor, and a case that wants the inherited
    source sets its own variable anyway.
    """
    dropped = [
        name for name in os.environ if name.startswith(SECRET_FD_ENV_PREFIX)
    ]
    for name in dropped:
        os.environ.pop(name, None)
    return dropped


INHERITED_SECRET_DESCRIPTORS_DROPPED = _drop_inherited_secret_descriptors()


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _RecordingLogger:
    """A logger that keeps every event it is handed.

    The event NAME is the contract, not the level: an operator greps
    the log for ``test_cross_verify_inconclusive`` to find the
    verifications that produced no verdict, and for
    ``test_cross_verify_cheat_detected`` to find the accusations.  A
    test that asserted on the level instead would let a renamed level
    through while the name — the thing that is actually greppable —
    changed underneath it.
    """

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def _record(self, level: str, event: str, message: str, **kwargs: Any) -> None:
        self.events.append(
            {"level": level, "event": event, "message": message, **kwargs}
        )

    def info(self, event: str, message: str, **kwargs: Any) -> None:
        self._record("info", event, message, **kwargs)

    def warning(self, event: str, message: str, **kwargs: Any) -> None:
        self._record("warning", event, message, **kwargs)

    def error(self, event: str, message: str, **kwargs: Any) -> None:
        self._record("error", event, message, **kwargs)

    def event_names(self) -> List[str]:
        return [e["event"] for e in self.events]

    def events_named(self, name: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["event"] == name]


class _ScriptExhausted(BaseException):
    """Raised when the runner is called more often than the script allows.

    Deliberately a :class:`BaseException`: the caller catches
    ``Exception`` around the runner, so an ``Exception`` subclass would
    be swallowed and turned into an ordinary verification failure — the
    test would then pass for the wrong reason.
    """


class _StubTask:
    """The two fields ``_cross_verify_test_result`` reads off a task."""

    def __init__(self, commands: List[str], project_dir: Path) -> None:
        self.id = "task-verifier-timeout-acceptance"
        self.project_dir = str(project_dir)
        self.test_commands = list(commands)
        self.test_command = self.test_commands[0] if self.test_commands else ""

    def get_test_commands(self) -> List[str]:
        return list(self.test_commands)


def _make_agent(logger: Any, project_dir: Path) -> AutonomousAgent:
    """An ``AutonomousAgent`` with only the fields the verdict depends on.

    The full constructor starts a lock broker, opens a task database and
    resolves a coding tool — none of which the cross-verify verdict
    depends on.  Building the instance without ``__init__`` keeps these
    tests about the verdict rather than about the wiring.
    """
    stub = object.__new__(AutonomousAgent)
    stub.logger = logger
    stub.project_dir = Path(project_dir)
    return stub


def _install_runner(
    monkeypatch: pytest.MonkeyPatch, script: List[Any]
) -> List[Dict[str, Any]]:
    """Replace ``agent.run_bounded`` with a scripted one.

    ``script`` holds one entry per runner call: an integer is an exit
    code, and the string ``"timeout"`` raises
    :class:`subprocess.TimeoutExpired`.  The returned list records every
    call, so a test can assert how many times the verification ran and
    which budget each run was given.  A call beyond the end of the
    script raises :class:`_ScriptExhausted` rather than running out of
    entries: the caller catches ``Exception`` around the runner, and an
    exhausted script silently turned into a runner error would read as
    an ordinary verification failure.
    """
    calls: List[Dict[str, Any]] = []

    def _fake_run_bounded(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append({"cmd": cmd, "timeout": kwargs.get("timeout")})
        if len(calls) > len(script):
            raise _ScriptExhausted(
                f"the runner was called {len(calls)} times but the script "
                f"only describes {len(script)} calls"
            )
        outcome = script[len(calls) - 1]
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(
                cmd=cmd, timeout=kwargs.get("timeout")
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=outcome, stdout="1 passed in 0.01s", stderr=""
        )

    monkeypatch.setattr(agent_module, "run_bounded", _fake_run_bounded)
    return calls


def _verify(
    agent: AutonomousAgent,
    task: _StubTask,
    *,
    ai_claimed_passed: bool = False,
    ai_reason: str = "",
    coder_response: str = "",
):
    return agent._cross_verify_test_result(
        task=task,
        ai_claimed_passed=ai_claimed_passed,
        ai_reason=ai_reason,
        coder_response=coder_response,
    )


# ---------------------------------------------------------------------------
# 1. Negative control A — the timeout is inconclusive
# ---------------------------------------------------------------------------


def test_negative_control_timeout_is_inconclusive(monkeypatch, tmp_path):
    """A timeout is INCONCLUSIVE — and the new kind is narrow.

    The subagent here claims PASSED, which is the case the old code got
    wrong: a timeout was folded into the ordinary non-zero branch, that
    branch's only question was "did the AI claim PASSED?", and the
    answer produced a loud accusation of lying.  A timeout says the
    VERIFIER ran out of budget; it says nothing at all about what the
    subagent wrote.
    """
    monkeypatch.setenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", "5")
    monkeypatch.setenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", "5")
    calls = _install_runner(monkeypatch, ["timeout", "timeout", "timeout"])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit/test_x.py -q"], tmp_path)

    passed, reason = _verify(agent, task, ai_claimed_passed=True)

    # The verdict is inconclusive, not PASSED and not a lie.
    assert passed is False, "a timed-out verification produced no verdict"
    assert reason.startswith("verifier_timeout:"), reason

    names = logger.event_names()
    assert "test_cross_verify_inconclusive" in names, names
    assert "test_cross_verify_cheat_detected" not in names, (
        "a timeout is evidence of nothing about what the subagent wrote; "
        "the accusation must not be made"
    )

    # The ladder ran: base → heavy → 2×heavy, with the configured 5s
    # budget (and 2×5s on the last rung).
    assert len(calls) == 3, calls
    assert [c["timeout"] for c in calls] == [5, 5, 10], calls

    # The failure classifies as the timeout kind, with the remedy that
    # says "give it a bigger budget" rather than "split it".
    failure = classify_failure(reason)
    assert failure.kind == VERIFIER_TIMEOUT, (
        f"expected {VERIFIER_TIMEOUT!r}, got {failure.kind!r} for {reason!r}"
    )
    assert failure.kind != REPORT_FORMAT, (
        "a timeout must not be filed as a report rejection"
    )
    assert failure.remedy == REVERIFY_WITH_MORE_BUDGET, failure.remedy

    # THE NEGATIVE CONTROL.  The OLD wording for the very same facts —
    # what the code produced before the timeout had its own branch: the
    # timeout was given the conventional exit code 124, the branch
    # asked whether the subagent claimed PASSED, and it said yes.  That
    # sentence is a report rejection, so it classifies as
    # ``report_format`` — and it must STILL classify that way, because
    # the new kind has to be narrow.  A kind that swallowed the old
    # wording as well would be a catch-all, and the classification would
    # have stopped carrying any information.
    old_reason = (
        "Subagent test-report mismatch: claimed TEST_RESULT: PASSED but "
        "actual pytest exit code was 124. Treating as FAILED. "
        "Tail of test output:\n"
        "[timeout after 5s] pytest tests/unit/test_x.py -q"
    )
    old_failure = classify_failure(old_reason)
    assert old_failure.kind == REPORT_FORMAT, (
        f"the old wording must still classify as {REPORT_FORMAT!r}, "
        f"got {old_failure.kind!r}"
    )
    assert old_failure.kind != VERIFIER_TIMEOUT, (
        "the timeout kind must not swallow the old report-rejection wording"
    )


# ---------------------------------------------------------------------------
# 2. The guard is not weakened
# ---------------------------------------------------------------------------


def test_guard_not_weakened(monkeypatch, tmp_path):
    """A real failure with a PASSED claim is still a lie.

    The fix for "a timeout was called a lie" must not become "a lie was
    called a timeout".  A command that RAN and exited non-zero produced
    an answer; the accusation stands, the reason classifies as a report
    rejection, and — because an answer is not a symptom — the
    verification is NOT re-run on the ladder.
    """
    monkeypatch.setenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", "5")
    monkeypatch.setenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", "5")
    calls = _install_runner(monkeypatch, [1])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit/test_x.py -q"], tmp_path)

    passed, reason = _verify(agent, task, ai_claimed_passed=True)

    assert passed is False
    assert not reason.startswith("verifier_timeout:"), reason

    names = logger.event_names()
    assert "test_cross_verify_cheat_detected" in names, names
    assert "test_cross_verify_inconclusive" not in names, (
        "a command that ran and failed produced an answer; it is not "
        "inconclusive"
    )

    failure = classify_failure(reason)
    assert failure.kind == REPORT_FORMAT, (
        f"expected {REPORT_FORMAT!r}, got {failure.kind!r} for {reason!r}"
    )

    # Only a timeout escalates the budget.  A real exit code is an
    # answer, so the verification ran ONCE.
    assert len(calls) == 1, (
        f"a genuine failure must not be re-verified on the ladder: {calls}"
    )


# ---------------------------------------------------------------------------
# 3. End to end: one task, three verifications, no split
# ---------------------------------------------------------------------------


def _build_executor_agent(project_dir: Path, logger: Any) -> AutonomousAgent:
    """A bare ``AutonomousAgent`` with every external dep mocked.

    Mirrors the builder in ``tests/unit/test_verifier_timeout_disposition.py``
    on purpose: the two contracts are separate, and a shared fixture would
    couple them.  Three attributes are load-bearing rather than decorative:

      * ``_failure_occurrences`` — the occurrence table the split gate
        counts on;
      * ``_refinement_count``    — read by the real ``_refine_after_failure``
        before it will split;
      * ``refiner.refine``       — the observation point: it returns the
        caller's own task list, so the real ``_refine_after_failure`` takes
        its "refiner returned no changes" branch and the test stays pointed
        at the DISPOSITION rather than at the split-application machinery
        downstream of it.

    Unlike that builder, the real ``_cross_verify_test_result`` is NOT
    replaced here — this test drives the production verification path.
    """
    from retry_manager import RetryManager
    from task import SubTask

    agent = AutonomousAgent.__new__(AutonomousAgent)
    # A REAL directory: the cross-verify refuses to run when the project
    # dir does not exist, and a verification that never ran would prove
    # nothing.
    agent.project_dir = Path(project_dir)
    agent.plan_id = "test-plan"
    agent.logger = logger

    agent.retry_manager = RetryManager()
    agent.config = MagicMock()
    agent.config.executor_system_prompt = ""
    agent.config.max_retries = 2

    # The subagent CLAIMS PASSED.  That is the case the old code got
    # wrong (a timeout was recorded as "the subagent lied"), so the
    # acceptance has to run it.
    cheat_response = "Some impl work.\n\nTEST_RESULT: PASSED\nREASON: looks good\n"
    agent.coding_tool = MagicMock()
    agent.coding_tool.query = MagicMock(return_value=cheat_response)

    agent.executor = MagicMock()
    agent.executor.had_previous_timeout = MagicMock(return_value=False)

    task_manager = MagicMock()
    task_manager.requirement = "Build something small."
    agent.task_manager = task_manager

    agent.subagent_cfg = None
    agent.parse_files_from_response = MagicMock(return_value={})
    agent.git_manager = MagicMock()
    agent.git_manager.get_changed_files = MagicMock(return_value=[])
    agent._commit_task_changes = MagicMock()
    agent._task_declared_files_exist = MagicMock(return_value=True)
    agent._get_git_diff_stat_for_review = MagicMock(return_value="")
    agent._clean_test_command = lambda cmd: cmd
    agent._session_task_completed_counts = {}

    # The gate's own state, mirrored from ``__init__``.
    agent._failure_occurrences = {}
    agent._refinement_count = 0

    task = SubTask(
        id="1",
        title="Test task",
        description="Implement X",
        test_command="pytest tests/unit/test_x.py -q",
        status="pending",
        project_dir=str(project_dir),
    )
    agent.refiner = MagicMock()
    agent.refiner.refine = MagicMock(
        side_effect=lambda **kwargs: kwargs["tasks"],
    )
    agent._persist_refiner_output = MagicMock()
    task_manager.tasks = [task]

    # The pre-flight check ("is the work already on disk?") runs the
    # task's command ONCE before the subagent is dispatched.  It is an
    # optimisation that has nothing to do with the verification ladder,
    # and it would consume one runner call — which would make
    # ``run_bounded.call_count == 3`` ambiguous between "the ladder ran
    # three times" and "the ladder ran twice and the pre-flight ran
    # once".  Neutralising it keeps the count about the ladder.  The
    # task's own semantics stay vanilla: it is not audit-style, and its
    # ``verification_only`` flag is left at its default.
    agent._preflight_test_command_skip = MagicMock(return_value=None)

    return agent


def test_e2e_task_once_verification_three_times(monkeypatch, tmp_path):
    """The whole thing, assembled: one task run, three verifications.

    The task's code was never disproven — the verification simply never
    finished.  So the executor must:

      * run the TASK once (a second run would spend the second
        occurrence, which is exactly what the split gate reads as "this
        task is too big for one subagent");
      * run the VERIFICATION three times (the budget ladder: base →
        heavy → 2×heavy — a bigger budget is the one thing that can fix
        a budget that was too small);
      * fail the task, with the reason naming the timeout;
      * NOT call the refiner (there is no verdict to decompose, and the
        children could not be verified either).
    """
    monkeypatch.setenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", "5")
    monkeypatch.setenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", "5")
    calls = _install_runner(monkeypatch, ["timeout", "timeout", "timeout"])

    logger = _RecordingLogger()
    agent = _build_executor_agent(tmp_path, logger)
    task = agent.task_manager.tasks[0]

    result = agent._execute_task_with_retry(task, max_retries=2)

    # The task failed.
    assert result is False, "a task whose verification never completed fails"

    # The task ran ONCE.
    assert agent.coding_tool.query.call_count == 1, (
        "a verifier timeout must not buy another run of the task; "
        f"got {agent.coding_tool.query.call_count} runs"
    )

    # The verification ran THREE times — the ladder.
    assert len(calls) == 3, (
        f"the verification must run once per rung of the ladder: {calls}"
    )
    assert [c["timeout"] for c in calls] == [5, 5, 10], calls

    # The refiner was never called.
    assert agent.refiner.refine.call_count == 0, (
        "a task whose verification produced no verdict must not be split: "
        "the children cannot be verified either"
    )

    # The task was recorded as failed, and the reason says why.
    assert agent.task_manager.record_task_failure.call_count == 1, (
        "the task must be recorded as failed"
    )
    recorded = agent.task_manager.record_task_failure.call_args[0][1]
    assert recorded.startswith("verifier_timeout:"), recorded

    # The events are greppable by name.
    names = logger.event_names()
    assert "test_cross_verify_inconclusive" in names, names
    assert "task_verifier_timeout_unproven" in names, names
    assert "test_cross_verify_cheat_detected" not in names, (
        "a timeout is not an accusation of lying"
    )
    assert "task_retry" not in names, (
        "a failure that was not retried must not be logged as a retry: "
        "the attempt counters are built on that event"
    )


# ---------------------------------------------------------------------------
# The suite must not inherit the executor's secret descriptors
# ---------------------------------------------------------------------------
#
# The executor hands a secret to the process it runs a task in through a
# pipe: the parent publishes the value on the write end and names the read
# end in the environment (``PDT_SECRET_FD_<NAME>``).  That is the handoff
# ``credentials.read_secret_fd`` reads from.
#
# A pytest run launched by that executor inherits the variable -- and the
# number in it names a descriptor of the PARENT, which in a test process is
# one of that process's own: under xdist, the worker's execnet channel.
#
# Reading it does not fail fast, it BLOCKS.  The pipe never carries the
# payload, so the read waits for a writer that is not coming; the worker is
# killed by the test timeout, xdist replaces it, the replacement picks the
# same test up and blocks the same way -- and the suite never finishes.
# The failure surfaces as a timeout, not as the misconfiguration it is.
#
# The drop happens here, at import, because this file is the only one this
# task may write.  The natural home for it is
# ``backend/_pytest_hermetic_env.py`` -- whose whole job is to keep ambient
# deployment state out of the suite -- and it should be moved there as soon
# as a task is declared to change that file.  What matters now is that the
# suite does not answer to a descriptor number it does not own.


def test_the_suite_does_not_inherit_the_executors_secret_descriptors():
    """The suite's own environment carries none of the executor's variables.

    The control for the drop below: the variables arrive with the process,
    so a suite that keeps them is reading a descriptor number that names
    something else entirely -- and blocks on it instead of failing.
    """
    inherited = [name for name in os.environ if name.startswith("PDT_SECRET_FD_")]
    assert not inherited, (
        f"the suite inherited the executor's secret descriptors: {inherited}.  "
        f"A descriptor number is meaningful only in the process that owns the "
        f"descriptor; in a test process it names one of that process's own "
        f"descriptors -- under xdist, the worker's execnet channel -- and "
        f"reading it blocks forever instead of failing, so the worker is "
        f"killed and replaced until the run times out."
    )


def test_the_dropper_removes_the_executors_secret_descriptors(monkeypatch):
    """The drop is what removes them, and it removes exactly those.

    A case that wants the inherited-fd source sets the variable itself, in
    the environment it hands to the code under test; the suite-wide drop
    only takes away what the parent published.
    """
    monkeypatch.setenv("PDT_SECRET_FD_FEISHU_APP_SECRET", "3")
    monkeypatch.setenv("PDT_SECRET_FD_TELEGRAM_BOT_TOKEN", "4")
    monkeypatch.setenv("PDT_SECRET_READER_PATH", "/nowhere/pdt-secret-reader")

    dropped = _drop_inherited_secret_descriptors()

    assert sorted(dropped) == [
        "PDT_SECRET_FD_FEISHU_APP_SECRET",
        "PDT_SECRET_FD_TELEGRAM_BOT_TOKEN",
    ], dropped
    assert not [
        name for name in os.environ if name.startswith("PDT_SECRET_FD_")
    ], os.environ
    # The reader path is a different variable and stays: it names a binary,
    # not a descriptor, and a case that wants it sets it itself.
    assert os.environ["PDT_SECRET_READER_PATH"] == "/nowhere/pdt-secret-reader"

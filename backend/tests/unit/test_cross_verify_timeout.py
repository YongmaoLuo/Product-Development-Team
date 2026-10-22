"""A cross-verify timeout is INCONCLUSIVE, not evidence that the subagent lied.

The defect this pins
--------------------
``_cross_verify_test_result`` re-runs the task's own ``test_command`` and
treats the exit code as the ground truth.  A command that did not finish
has no exit code — and the old code invented one: the
``except subprocess.TimeoutExpired`` branch folded the timeout into
``aggregate_exit = 124`` and let it fall through to the ordinary
non-zero branch.  There, the *only* question asked was "did the AI claim
PASSED?", and the answer drove a loud
``test_cross_verify_cheat_detected`` event whose text says the subagent
claimed PASSED but the tests failed.

That is a false accusation.  A timeout says the verifier ran out of
budget; it says nothing at all about what the subagent wrote.  The
cheat-detection branch is a guard against a subagent lying about its
tests, and a timeout must never reach it — otherwise a slow-but-correct
suite burns the budget, gets recorded as "the subagent lied", and the
whole run is condemned on evidence that does not exist.

The budget is configuration
--------------------------
The budget is no longer a hard-coded ``600``.  A single test file and a
whole test directory do not have the same runtime magnitude, and a
budget that is wrong in either direction is wrong expensively: too
small kills a run that was about to finish (and a killed run reports
itself as a failure, so a slow pass turns red), too large makes a stuck
run cost the whole tree-sized wait.  So the budget comes from
``verify_budget.timeout_seconds``, which reads the tier off the command
and the number off the environment.

What these tests hold in place
------------------------------
1. A timeout yields the ``verifier_timeout`` verdict and the
   ``test_cross_verify_inconclusive`` event — never
   ``test_cross_verify_cheat_detected``.
2. A timeout on a LATER leg does not inherit the earlier leg's exit 0.
3. The budget is the configured one, per lane.
4. The cheat guard is NOT weakened: a real non-zero exit with a PASSED
   claim still fires ``test_cross_verify_cheat_detected``, and a real
   non-zero exit with a FAILED claim still fires ``test_cross_verify_fail``.
5. A missing logger does not turn a timeout into a crash.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import agent as agent_module  # noqa: E402
from agent import AutonomousAgent  # noqa: E402


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _RecordingLogger:
    """A logger that keeps every event it is handed."""

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

    def event(self, name: str) -> Dict[str, Any]:
        for entry in self.events:
            if entry["event"] == name:
                return entry
        raise AssertionError(
            f"event {name!r} was never logged; logged: {self.event_names()}"
        )


class _StubTask:
    """The two fields ``_cross_verify_test_result`` reads off a task."""

    def __init__(self, commands: List[str], project_dir: Path) -> None:
        self.id = "task-cross-verify-timeout"
        self.project_dir = str(project_dir)
        self.test_commands = list(commands)
        self.test_command = self.test_commands[0] if self.test_commands else ""

    def get_test_commands(self) -> List[str]:
        return list(self.test_commands)


def _make_agent(logger: Any, project_dir: Path) -> AutonomousAgent:
    """An ``AutonomousAgent`` with only the fields this method touches.

    The full constructor starts a lock broker, opens a task database and
    resolves a coding tool — none of which the cross-verify verdict
    depends on.  Building the instance without ``__init__`` keeps the
    test about the verdict rather than about the wiring.
    """
    stub = object.__new__(AutonomousAgent)
    stub.logger = logger
    stub.project_dir = Path(project_dir)
    return stub


def _install_runner(
    monkeypatch: pytest.MonkeyPatch, script: List[Any]
) -> List[Dict[str, Any]]:
    """Replace ``agent.run_bounded`` with a scripted one.

    ``script`` holds one entry per leg: an integer is an exit code, and
    the string ``"timeout"`` raises :class:`subprocess.TimeoutExpired`.
    The returned list records every call, so a test can assert on the
    budget that was actually handed to the runner.
    """
    calls: List[Dict[str, Any]] = []

    def _fake_run_bounded(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append({"cmd": cmd, "timeout": kwargs.get("timeout")})
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
# The timeout is inconclusive
# ---------------------------------------------------------------------------


def test_timeout_logs_inconclusive_not_cheat(monkeypatch, tmp_path):
    """A timeout is reported as inconclusive, never as a lie."""
    monkeypatch.delenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", raising=False)
    _install_runner(monkeypatch, ["timeout"])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit/test_x.py -q"], tmp_path)

    passed, reason = _verify(agent, task, ai_claimed_passed=True)

    assert passed is False
    assert reason.startswith("verifier_timeout:"), reason
    # The accusation must not be made: there is no evidence about what
    # the subagent wrote.
    assert "test_cross_verify_inconclusive" in logger.event_names()
    assert "test_cross_verify_cheat_detected" not in logger.event_names()

    inconclusive = logger.event("test_cross_verify_inconclusive")
    assert inconclusive["level"] == "warning"
    assert "验证命令超时" in inconclusive["message"]
    assert "不能据此判定 subagent 说谎" in inconclusive["message"]


def test_timeout_after_passing_leg_is_not_passed(monkeypatch, tmp_path):
    """A timeout on the second leg does not inherit the first leg's exit 0."""
    monkeypatch.delenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", raising=False)
    _install_runner(monkeypatch, [0, "timeout"])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(
        ["pytest tests/unit/test_a.py -q", "pytest tests/unit/test_b.py -q"],
        tmp_path,
    )

    passed, reason = _verify(agent, task, ai_claimed_passed=True)

    assert passed is False, "a timed-out leg must not be reported as PASSED"
    assert reason.startswith("verifier_timeout:"), reason
    assert "test_cross_verify_inconclusive" in logger.event_names()
    assert "test_cross_verify_cheat_detected" not in logger.event_names()


def test_timeout_without_logger_does_not_raise(monkeypatch, tmp_path):
    """No logger is a normal configuration, not a crash."""
    monkeypatch.delenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", raising=False)
    _install_runner(monkeypatch, ["timeout"])

    agent = _make_agent(None, tmp_path)
    task = _StubTask(["pytest tests/unit/test_x.py -q"], tmp_path)

    passed, reason = _verify(agent, task, ai_claimed_passed=True)

    assert passed is False
    assert reason.startswith("verifier_timeout:"), reason


# ---------------------------------------------------------------------------
# The budget is configured, and follows the command's lane
# ---------------------------------------------------------------------------


def test_timeout_budget_comes_from_env(monkeypatch, tmp_path):
    """The light lane's budget comes from the environment."""
    monkeypatch.setenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", "5")
    monkeypatch.delenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", raising=False)
    calls = _install_runner(monkeypatch, ["timeout"])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit/test_x.py -q"], tmp_path)

    _verify(agent, task, ai_claimed_passed=False)

    assert calls, "the runner was never called"
    assert calls[0]["timeout"] == 5


def test_heavy_command_uses_heavy_budget(monkeypatch, tmp_path):
    """A whole test directory gets the heavy lane's budget."""
    monkeypatch.delenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", raising=False)
    calls = _install_runner(monkeypatch, ["timeout"])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit -q"], tmp_path)

    _verify(agent, task, ai_claimed_passed=False)

    assert calls, "the runner was never called"
    assert calls[0]["timeout"] == 1800


# ---------------------------------------------------------------------------
# The guard is not weakened
# ---------------------------------------------------------------------------


def test_cheat_detection_still_fires_on_real_exit_1(monkeypatch, tmp_path):
    """A real failure with a PASSED claim is still a lie."""
    monkeypatch.delenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", raising=False)
    _install_runner(monkeypatch, [1])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit/test_x.py -q"], tmp_path)

    passed, reason = _verify(agent, task, ai_claimed_passed=True)

    assert passed is False
    assert "test_cross_verify_cheat_detected" in logger.event_names()
    assert "test_cross_verify_inconclusive" not in logger.event_names()
    assert not reason.startswith("verifier_timeout:"), reason


def test_real_failure_with_failed_claim_still_fires_fail(monkeypatch, tmp_path):
    """A real failure with a FAILED claim is still an ordinary failure."""
    monkeypatch.delenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", raising=False)
    _install_runner(monkeypatch, [1])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit/test_x.py -q"], tmp_path)

    passed, reason = _verify(
        agent, task, ai_claimed_passed=False, ai_reason="the fix does not work"
    )

    assert passed is False
    assert "test_cross_verify_fail" in logger.event_names()
    assert "test_cross_verify_cheat_detected" not in logger.event_names()
    assert "test_cross_verify_inconclusive" not in logger.event_names()
    assert not reason.startswith("verifier_timeout:"), reason

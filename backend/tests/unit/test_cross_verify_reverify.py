"""A verifier timeout escalates the budget — and only a timeout does.

The defect this pins
--------------------
``_cross_verify_test_result`` re-runs the task's own ``test_command`` and
reads the exit code as the ground truth.  A command that runs out of
budget produces no exit code at all, so the old code reported it as
``verifier_timeout`` and stopped there — one timeout, one inconclusive
verdict, and the task dies on a budget that was simply too small for
the command.

That is the wrong remedy for the wrong cause.  The real incident behind
this work was a whole-suite lane (``pytest tests/unit -q -n 8``) that
needs a tree-sized budget, running against a single-file budget: the
suite was about to finish and was killed instead.  The evidence was
"the budget was too small", and the only thing more budget can fix is
"the budget was too small".

So a timeout gets a ladder of budgets — base → heavy → 2×heavy — and the
*verification* is re-run on each rung.  Three properties make this
safe, and each is a test here:

1. **Only a timeout escalates.**  A real exit code is an answer, not a
   symptom: a suite that failed with exit 1 must be reported as failed
   after ONE run, not re-run three times with three chances to pass.
2. **The task itself is never re-run.**  Re-running the task would hit
   the same wall at the same budget, and would burn the second
   occurrence that turns "the budget was too small" into "the task is
   too big".  Only the verification re-runs here.
3. **The ladder grows and never shrinks.**  Each rung is at least the
   one before it, so an escalation never spends less time than the run
   that already timed out — and a lowered ``heavy`` budget cannot pull
   the ladder below the light one.

What these tests hold in place
------------------------------
* a timeout escalates base → heavy → 2×heavy, three runner calls, and
  ends as ``verifier_timeout:`` with two ``test_cross_verify_reverify``
  events and one ``test_cross_verify_inconclusive``;
* a genuine failure is NOT re-verified — one runner call;
* a timeout that clears on the second rung is a PASSED verdict, in two
  runner calls;
* the heavy lane's ladder is ``[1800, 1800, 3600]`` — the first rung
  is the heavy budget already, because the command's own lane is heavy;
* an environment that lowers ``heavy`` cannot shrink the ladder.
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


class _ScriptExhausted(BaseException):
    """Raised when the runner is called more often than the script allows.

    Deliberately a :class:`BaseException`: the caller catches
    ``Exception`` around the runner, so an ``Exception`` subclass would
    be swallowed and turned into an ordinary verification failure — the
    test would then pass for the wrong reason.
    """


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

    def events_named(self, name: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["event"] == name]


class _StubTask:
    """The two fields ``_cross_verify_test_result`` reads off a task."""

    def __init__(self, commands: List[str], project_dir: Path) -> None:
        self.id = "task-cross-verify-reverify"
        self.project_dir = str(project_dir)
        self.test_commands = list(commands)
        self.test_command = self.test_commands[0] if self.test_commands else ""

    def get_test_commands(self) -> List[str]:
        return list(self.test_commands)


def _make_agent(logger: Any, project_dir: Path) -> AutonomousAgent:
    """An ``AutonomousAgent`` with only the fields this method touches."""
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
    call, so a test can assert both how many times the verification ran
    and which budget each run was given.
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


def _budgets(calls: List[Dict[str, Any]]) -> List[Any]:
    return [c["timeout"] for c in calls]


# ---------------------------------------------------------------------------
# A timeout escalates the budget
# ---------------------------------------------------------------------------


def test_timeout_escalates_three_budgets(monkeypatch, tmp_path):
    """Three timeouts → three budgets → one inconclusive verdict."""
    monkeypatch.delenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", raising=False)
    calls = _install_runner(monkeypatch, ["timeout", "timeout", "timeout"])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit/test_x.py -q"], tmp_path)

    passed, reason = _verify(agent, task, ai_claimed_passed=True)

    assert passed is False
    assert reason.startswith("verifier_timeout:"), reason
    assert _budgets(calls) == [600, 1800, 3600]
    assert len(calls) == 3

    names = logger.event_names()
    assert names.count("test_cross_verify_reverify") == 2, names
    assert names.count("test_cross_verify_inconclusive") == 1, names
    assert "test_cross_verify_cheat_detected" not in names, names


def test_reverify_event_records_the_budgets(monkeypatch, tmp_path):
    """The re-verify event says which budget ran and which comes next."""
    monkeypatch.delenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", raising=False)
    _install_runner(monkeypatch, ["timeout", "timeout", "timeout"])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit/test_x.py -q"], tmp_path)

    _verify(agent, task, ai_claimed_passed=True)

    reverifies = logger.events_named("test_cross_verify_reverify")
    assert len(reverifies) == 2, logger.event_names()
    first, second = reverifies
    assert first["data"]["attempt"] == 1
    assert first["data"]["timeout_used"] == 600
    assert first["data"]["next_timeout"] == 1800
    assert second["data"]["attempt"] == 2
    assert second["data"]["timeout_used"] == 1800
    assert second["data"]["next_timeout"] == 3600


def test_heavy_lane_ladder(monkeypatch, tmp_path):
    """A whole test directory starts at the heavy budget."""
    monkeypatch.delenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", raising=False)
    calls = _install_runner(monkeypatch, ["timeout", "timeout", "timeout"])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit -q"], tmp_path)

    passed, reason = _verify(agent, task, ai_claimed_passed=True)

    assert passed is False
    assert reason.startswith("verifier_timeout:"), reason
    assert _budgets(calls) == [1800, 1800, 3600]
    assert len(calls) == 3


def test_lowered_heavy_budget_cannot_shrink_the_ladder(monkeypatch, tmp_path):
    """A ``heavy`` budget below ``base`` does not pull the ladder down."""
    monkeypatch.setenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", "600")
    monkeypatch.setenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", "100")
    calls = _install_runner(monkeypatch, ["timeout", "timeout", "timeout"])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit/test_x.py -q"], tmp_path)

    _verify(agent, task, ai_claimed_passed=True)

    assert _budgets(calls) == [600, 600, 600]
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Only a timeout escalates
# ---------------------------------------------------------------------------


def test_genuine_failure_is_not_reverified(monkeypatch, tmp_path):
    """A real failure is an answer, not a symptom — one run, no ladder."""
    monkeypatch.delenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", raising=False)
    calls = _install_runner(monkeypatch, [1])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit/test_x.py -q"], tmp_path)

    passed, reason = _verify(
        agent, task, ai_claimed_passed=False, ai_reason="the fix does not work"
    )

    assert passed is False
    assert len(calls) == 1, "a genuine failure must not be re-verified"
    assert reason.startswith("pytest exit 1"), reason
    assert "test_cross_verify_reverify" not in logger.event_names()


def test_reverify_recovers_on_second_budget(monkeypatch, tmp_path):
    """A timeout that clears on the second rung is a PASSED verdict."""
    monkeypatch.delenv("PDT_CROSS_VERIFY_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("PDT_CROSS_VERIFY_HEAVY_TIMEOUT_SEC", raising=False)
    calls = _install_runner(monkeypatch, ["timeout", 0])

    logger = _RecordingLogger()
    agent = _make_agent(logger, tmp_path)
    task = _StubTask(["pytest tests/unit/test_x.py -q"], tmp_path)

    passed, reason = _verify(agent, task, ai_claimed_passed=True)

    assert (passed, reason) == (True, "")
    assert len(calls) == 2
    assert _budgets(calls) == [600, 1800]
    names = logger.event_names()
    assert names.count("test_cross_verify_reverify") == 1, names
    assert "test_cross_verify_inconclusive" not in names, names

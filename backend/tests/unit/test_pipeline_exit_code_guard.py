"""A pipeline's exit status belongs to its LAST stage (2026-09-17).

Why this exists
---------------
A task or VP is judged by the exit code of its ``test_command``. A shell
pipeline exits with the status of its final command, so

    python scripts/ci_local.py --tiers lint,unit,e2e | tee /tmp/progress.log

exits 0 no matter what the CI run did — the tested command's status is
discarded by ``tee``.

This is the mirror image of the ``probe_chain`` rule. That one describes a
command that can never report *success* (guaranteeing a false FAILURE,
which gets investigated). This one describes a command that can never
report *failure* — a false PASS, which nobody investigates. The shape it
exists for: a full-CI task
whose command ended in ``| tee``; the pre-flight saw exit 0, declared the
work "already done", and marked the task completed without a subagent
ever running.

Coverage pinned here:

  * sinks (``tee``/``tail``/``cat``/…) as the final stage →
    ``exit_code_swallowed_by_pipe``;
  * match filters (``grep``/``rg``/…) as the final stage →
    ``upstream_failure_masked_by_filter``;
  * explicit re-propagation (``set -o pipefail``, ``${PIPESTATUS[0]}``)
    clears both — the author handled it;
  * a redirect-based capture with ``exit $rc`` is clean (the canonical
    replacement shape);
  * the VP-level guard (``verification_command_guard``) reports the same
    codes, from the same implementation;
  * the executor's pre-flight refuses to *skip* a task on the strength of
    such a command.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from test_command_quality import (  # noqa: E402
    find_pipeline_exit_issue,
    inspect_command,
)

R3_08_COMMAND = (
    "cd <user>/work/a dev checkout && source venv1/bin/"
    "activate && timeout 5400 python scripts/ci_local.py --tiers "
    "lint,unit,integration,e2e,regression --report /tmp/nightly_ci_report.json "
    "2>&1 | tee /tmp/VP-028_progress.log"
)

#: The shape the plan's own repair tasks use (r3-06-3): two commands, each
#: captured to a log, statuses merged, ``exit $rc``. Must stay clean.
REDIRECT_CAPTURE = (
    "cd /x && timeout 900 cargo test --test signal_classification > "
    "/tmp/VP-016_rust.log 2>&1; rc1=$?; cd /y && source venv1/bin/activate && "
    "timeout 900 python3 tools/verify_vp016_marker.py > /tmp/VP-016_presence.log "
    "2>&1; rc2=$?; rc=$(( rc1 || rc2 )); echo \"EXIT_CODE=$rc\"; exit $rc"
)


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------


def test_the_live_r3_08_shape_is_flagged():
    issue = find_pipeline_exit_issue(R3_08_COMMAND)
    assert issue is not None
    assert issue.code == "exit_code_swallowed_by_pipe"
    assert "tee" in issue.detail


def test_redirect_capture_with_explicit_exit_is_clean():
    assert find_pipeline_exit_issue(REDIRECT_CAPTURE) is None
    assert inspect_command(REDIRECT_CAPTURE) == []


@pytest.mark.parametrize("sink", ["tee /tmp/x.log", "tail -20", "cat",
                                  "head -5", "wc -l", "awk '{print $1}'"])
def test_sinks_as_final_stage_are_flagged(sink):
    issue = find_pipeline_exit_issue(f"pytest -q tests/x.py 2>&1 | {sink}")
    assert issue is not None and issue.code == "exit_code_swallowed_by_pipe"


@pytest.mark.parametrize("flt", ["grep -E 'passed|failed'", "rg FAILED"])
def test_match_filters_as_final_stage_are_flagged(flt):
    issue = find_pipeline_exit_issue(f"pytest -q tests/x.py 2>&1 | {flt}")
    assert issue is not None
    assert issue.code == "upstream_failure_masked_by_filter"


@pytest.mark.parametrize("escape", [
    "set -o pipefail; pytest -q | tee /tmp/x.log",
    "pytest -q | tee /tmp/x.log; exit ${PIPESTATUS[0]}",
    "pytest -q 2>&1 | tee /tmp/x.log; rc=${PIPESTATUS[0]}; exit $rc",
])
def test_explicit_repropagation_clears_the_rule(escape):
    assert find_pipeline_exit_issue(escape) is None


def test_a_pipe_in_an_earlier_link_is_not_the_verdict():
    """Only the LAST link's status is the command's status."""
    cmd = ("pytest -q | tee /tmp/early.log; rc=${PIPESTATUS[0]}; "
           "echo done; exit $rc")
    assert find_pipeline_exit_issue(cmd) is None


def test_quoted_pipe_is_not_a_pipeline():
    assert find_pipeline_exit_issue("python3 -c \"print('a | b')\"") is None


def test_bare_filter_assertion_is_still_legitimate():
    """``grep -q X file`` as the whole command is a contract check."""
    assert find_pipeline_exit_issue("grep -q 'marker' file.txt") is None
    assert inspect_command("grep -q 'marker' file.txt") == []


def test_pipeline_rule_is_independent_of_the_test_runner_exemption():
    """``probe_chain`` bails out when a runner is present; this must not.

    A test runner piped into ``tee`` is precisely the shape the probe-chain
    heuristic was told to ignore, and precisely the one that must be
    caught.
    """
    issues = inspect_command(R3_08_COMMAND)
    assert [i.code for i in issues] == ["exit_code_swallowed_by_pipe"]


# ---------------------------------------------------------------------------
# The VP-level guard reports the same thing, from the same implementation
# ---------------------------------------------------------------------------


def test_verification_guard_reports_the_pipeline_violation():
    from verification_command_guard import find_violations

    codes = [v.code for v in find_violations(R3_08_COMMAND)]
    assert "exit_code_swallowed_by_pipe" in codes


def test_verification_guard_leaves_the_canonical_shape_clean():
    from verification_command_guard import find_violations

    codes = [v.code for v in find_violations(REDIRECT_CAPTURE)]
    assert "exit_code_swallowed_by_pipe" not in codes


def test_generation_rules_mention_the_exit_code_rule():
    """The prompt text and the detector live in the same file by design."""
    from verification_command_guard import GENERATION_RULES

    assert "退出码" in GENERATION_RULES
    assert "tee" in GENERATION_RULES
    assert "pipefail" in GENERATION_RULES


# ---------------------------------------------------------------------------
# Layer 3: the executor must not SKIP a task on such a command
# ---------------------------------------------------------------------------


class _StubAgent:
    """Host for the real pre-flight method."""

    from agent import AutonomousAgent  # noqa: E402
    _preflight_test_command_skip = AutonomousAgent._preflight_test_command_skip
    _clean_test_command = AutonomousAgent._clean_test_command
    _looks_like_audit_task = AutonomousAgent._looks_like_audit_task
    _AUDIT_TASK_KEYWORDS = AutonomousAgent._AUDIT_TASK_KEYWORDS
    _PREFLIGHT_TEST_TIMEOUT_SEC = AutonomousAgent._PREFLIGHT_TEST_TIMEOUT_SEC

    def __init__(self, project_dir, logger=None):
        self.project_dir = project_dir
        self.logger = logger

    def _get_task_progress_repository(self):  # pragma: no cover
        return None


class _LogRecorder:
    def __init__(self):
        self.events = []

    def _record(self, level):
        def _fn(event, message, **kwargs):
            self.events.append((event, kwargs.get("data")))
        return _fn

    def __getattr__(self, name):
        if name in ("info", "warning", "error", "debug"):
            return self._record(name)
        raise AttributeError(name)


def _task(command, **extra):
    from task import SubTask

    fields = {
        "id": "t-1", "title": "t", "description": "d",
        "test_command": command, "verification_only": False,
    }
    fields.update(extra)
    return SubTask(**fields)


def test_preflight_refuses_to_skip_on_a_status_swallowing_command(tmp_path):
    """The r3-08 trap: exit 0 says nothing, so it must not mean "done"."""
    recorder = _LogRecorder()
    agent = _StubAgent(tmp_path, recorder)

    # ``true | tee`` exits 0 whatever ``true`` did — without the guard the
    # pre-flight would return True and the task would never run.
    result = agent._preflight_test_command_skip(
        _task("true 2>&1 | tee /tmp/whatever.log"),
    )

    assert result is None, "a command that cannot report failure must not skip the task"
    assert any(e[0] == "task_preflight_skip_refused_unusable_command"
               for e in recorder.events)


def test_preflight_still_skips_a_well_formed_command(tmp_path):
    """The guard must not disable the skip mechanism itself."""
    agent = _StubAgent(tmp_path, None)
    assert agent._preflight_test_command_skip(_task("true > /tmp/out.log 2>&1; exit $?")) is True


def test_preflight_still_reports_failure_for_a_well_formed_command(tmp_path):
    agent = _StubAgent(tmp_path, None)
    assert agent._preflight_test_command_skip(_task("false > /tmp/out.log 2>&1; exit $?")) is False

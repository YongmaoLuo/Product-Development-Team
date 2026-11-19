"""The test launcher wrapper: it injects the mode, refuses a production
caller, and hands the child's exit code back.

``scripts/pdt_test_runner.sh`` is the only sanctioned way to start a
PDT process in test mode.  The point of it is not convenience -- it is
that "forgot to inject the mode" stops being a thing that can happen:
the wrapper exports ``PDT_PLAN_MODE=test`` before the command starts,
so a test launch cannot silently run as if it were production.

Three properties are pinned here, and each of them is a way the wrapper
could fail to be a wrapper:

* **it injects** -- the child sees ``PDT_PLAN_MODE=test``, and with
  ``--allow-llm-quota`` it also sees ``PDT_VERIFY_ALLOW_LLM_QUOTA=1``;
* **it refuses** -- a caller that already says ``PDT_PLAN_MODE=production``
  is not run at all: the wrapper writes the reason to stderr and exits
  2.  A production plan must not be able to start its work in test
  mode, and "the command ran anyway" is the failure this guards;
* **it passes through** -- the exit code is the command's own, and the
  arguments after ``--`` reach it unchanged.

The wrapper is also asked to launch something real: ``backend/cli.py
--help`` is a genuine PDT process, and running it through the wrapper
proves the injection reaches a real child rather than a stub.

Why the environment is built here instead of inherited
--------------------------------------------------------
Once the executor and the verification sub-agent inject
``PDT_PLAN_MODE`` into their pytest subprocesses, a production plan's
pytest run carries ``PDT_PLAN_MODE=production``.  A test that inherited
that environment would be refused by the wrapper it is testing -- the
positive cases would measure the refusal instead of the injection, and
they would pass or fail depending on which plan was running.  So the
positive cases hand the wrapper a *clean* environment (no
``PDT_PLAN_MODE``), and the refusal case hands it the opposite one
(``production``, explicitly).  Neither case depends on what the
surrounding process happens to export.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: The wrapper under test, and the CLI it has to be able to launch.
#: Both are addressed relative to this file, so the suite runs in any
#: checkout -- no path from one developer's machine is baked in.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_RUNNER = _REPO_ROOT / "scripts" / "pdt_test_runner.sh"
_CLI = _REPO_ROOT / "backend" / "cli.py"


def _clean_env() -> dict:
    """The environment of an ordinary test process: no plan mode.

    See the module docstring -- inheriting the mode would make the
    positive cases depend on the plan that happens to be running.
    """
    return {k: v for k, v in os.environ.items() if k != "PDT_PLAN_MODE"}


def _run(args, env, timeout=180):
    return subprocess.run(
        [str(_RUNNER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_exports_test_mode():
    """The child sees ``PDT_PLAN_MODE=test``.

    This is the whole point of the wrapper: the mode cannot be
    forgotten, because the wrapper puts it there.
    """
    result = _run(
        ["--", sys.executable, "-c", "import os; print(os.environ.get('PDT_PLAN_MODE'))"],
        env=_clean_env(),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "test"


def test_refuses_production_caller(tmp_path):
    """A caller that says ``production`` is refused, not served.

    The command must not run at all -- so the child is asked to leave a
    marker file, and the marker's absence is the evidence.  A refusal
    that still ran the command would be a refusal in name only.
    """
    marker = tmp_path / "ran.txt"
    child = f"open({str(marker)!r}, 'w').write('ran')"
    env = {**os.environ, "PDT_PLAN_MODE": "production"}

    result = _run(["--", sys.executable, "-c", child], env=env)

    assert result.returncode == 2
    assert not marker.exists(), "the refused command ran anyway"
    # The reason has to be readable: a bare exit code tells the caller
    # nothing about which of the two refusals they hit.
    assert "PDT_PLAN_MODE" in result.stderr
    assert "production" in result.stderr
    assert "refus" in result.stderr.lower()


def test_allow_llm_quota_flag():
    """``--allow-llm-quota`` exports the cost gate's override."""
    result = _run(
        [
            "--allow-llm-quota",
            "--",
            sys.executable,
            "-c",
            "import os; print(os.environ.get('PDT_VERIFY_ALLOW_LLM_QUOTA'))",
        ],
        env=_clean_env(),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_without_quota_flag_the_variable_is_not_exported():
    """Without the flag the cost gate's override is absent.

    The override lets ``cost=llm_quota`` verification points run
    instead of being deferred, so it has to be something the caller
    asks for -- not something a launch inherits by accident.
    """
    env = _clean_env()
    env.pop("PDT_VERIFY_ALLOW_LLM_QUOTA", None)

    result = _run(
        ["--", sys.executable, "-c", "import os; print(os.environ.get('PDT_VERIFY_ALLOW_LLM_QUOTA'))"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "None"


def test_exit_code_passthrough():
    """The command's exit code is the wrapper's exit code."""
    result = _run(
        ["--", sys.executable, "-c", "import sys; sys.exit(7)"],
        env=_clean_env(),
    )

    assert result.returncode == 7


def test_arguments_after_the_separator_are_passed_through():
    """Everything after ``--`` reaches the command unchanged."""
    result = _run(
        ["--", sys.executable, "-c", "import sys; print(sys.argv[1:])", "a b", "--flag", "c"],
        env=_clean_env(),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['a b', '--flag', 'c']"


def test_missing_separator_is_a_usage_error():
    """Without ``--`` the command is ambiguous, so nothing runs."""
    env = _clean_env()

    result = _run([sys.executable, "-c", "print('should not run')"], env=env)

    assert result.returncode == 2
    assert "should not run" not in result.stdout
    assert "usage" in result.stderr.lower()


def test_launches_real_pdt_process():
    """The wrapper can start a real PDT process.

    ``backend/cli.py --help`` is the genuine CLI -- importing it pulls
    in the agent, the config registry and the credential plumbing.  If
    the wrapper's injection broke a real process, this is where it
    would show, and a stub would not have shown it.
    """
    result = _run(["--", sys.executable, str(_CLI), "--help"], env=_clean_env(), timeout=300)

    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()
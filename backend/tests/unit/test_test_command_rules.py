"""Tests for ``test_command_rules``.

The three rules in that module answer questions that two callers ask
independently and that neither of them can answer alone: the refiner
(auditing whether a failed task is still worth keeping) and the repair
generator (writing the replacement).  Both need to know *which files
the failed attempt touched*, *whether the target command collects any
test at all*, and *whether a test is a vacuous shell*.  Worked out
separately on each side, the answers drift — so the rules live in one
place and both sides call them.

The rules are **pure**: their inputs arrive as arguments and their
answers leave as return values.  The one impure thing in the
neighbourhood — the process that actually runs ``pytest
--collect-only`` — arrives as an injected ``runner`` callable.  That
keeps the rule exercisable without starting a process, and it leaves
the caller in charge of timeouts, sandboxing and output parsing.

Three boundaries carry the weight:

* **No stack and no diff → no files.**  A guessed path is worse than an
  empty list.  ``files_to_modify`` is a hard gate, so a
  plausible-looking wrong path does not merely fail to help — it sends
  the repair at a file the failure never touched.
* **``collected == 0`` is not an answer.**  Zero collected means the
  runner could not tell, and the caller decides what to do about that.
  The rule reports the zero; it does not turn it into "these tests do
  not exist" or "these tests are vacuous".
* **A shell test asserts nothing.**  ``assert True``, ``pass`` and
  ``pytest.skip(...)`` all pass without checking anything, so a task
  whose only test is one of those verifies nothing and must not be
  counted as a working repair.
"""

import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from test_command_rules import (  # noqa: E402
    collect_only_probe,
    derive_files_to_modify,
    is_empty_shell_test,
)


# ---------------------------------------------------------------------------
# derive_files_to_modify
# ---------------------------------------------------------------------------


def test_derive_from_stack_and_diff():
    """The stack and the diff are two sources of one merged answer."""
    derived = derive_files_to_modify(
        failure={"stack": "tests/a.py:3"},
        diff=["src/x.py"],
    )
    assert derived == ["tests/a.py", "src/x.py"]

    # The same file named by both sources is one file, not two.
    assert derive_files_to_modify(
        failure={"stack": "tests/a.py:3"},
        diff=["tests/a.py", "src/x.py"],
    ) == ["tests/a.py", "src/x.py"]


def test_derive_tolerates_noise_around_a_named_path():
    """A failure record names its file with punctuation around it.

    The prose around a path — quotes, brackets, a trailing colon, the
    ``...`` that marks a path a tool shortened — is not part of the
    path.  Stripping it is not a guess; keeping it would report a
    path that no caller could use.
    """
    assert derive_files_to_modify(failure={"file": '"tests/a.py":'}, diff=[]) == [
        "tests/a.py"
    ]
    assert derive_files_to_modify(failure={"file": "(tests/a.py)"}, diff=[]) == [
        "tests/a.py"
    ]
    # A shortened path has no project root, so it is not a path this
    # rule can report: it is dropped rather than guessed at.
    assert derive_files_to_modify(failure={"file": ".../backend/agent.py"}, diff=[]) == []


def test_derive_empty_no_guess():
    """With nothing to go on, the answer is nothing — never a guess."""
    assert derive_files_to_modify(failure={}, diff=[]) == []
    assert derive_files_to_modify(failure=None, diff=None) == []

    # A stack that names no source file is no source of files either:
    # the prose around it ("assert 1 == 2") must not be mined for
    # something that looks like a path.
    assert derive_files_to_modify(
        failure={"stack": "AssertionError: assert 1 == 2"},
        diff=[],
    ) == []


def test_derive_reads_the_documented_stack_shapes():
    """The shapes a failure record actually carries."""
    # A python traceback frame.
    assert derive_files_to_modify(
        failure={"stack": 'File "backend/foo.py", line 42, in test_x'},
        diff=[],
    ) == ["backend/foo.py"]

    # A pytest node id.
    assert derive_files_to_modify(
        failure={"stack": "FAILED tests/unit/test_q.py::test_q - AssertionError"},
        diff=[],
    ) == ["tests/unit/test_q.py"]

    # A diff body rather than a list of paths.
    assert derive_files_to_modify(
        failure={},
        diff=["diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py"],
    ) == ["x.py"]

    # The record itself is the stack.
    assert derive_files_to_modify(failure="tests/a.py:3", diff=[]) == ["tests/a.py"]


# ---------------------------------------------------------------------------
# is_empty_shell_test
# ---------------------------------------------------------------------------


def test_empty_shell_matrix():
    """The three shapes that pass without asserting anything."""
    assert is_empty_shell_test("def test_x(): assert True") is True
    assert is_empty_shell_test("def test_x(): pass") is True
    assert is_empty_shell_test('def test_x(): pytest.skip("not now")') is True

    # A test that checks something is not a shell, whatever it is written
    # in: a plain assert, an unittest-style call, or a raises-block.
    assert is_empty_shell_test("def test_x(): assert compute() == 2") is False
    assert is_empty_shell_test("def test_x(): self.assertEqual(compute(), 2)") is False
    assert (
        is_empty_shell_test(
            "def test_x():\n"
            "    with pytest.raises(ValueError):\n"
            "        compute()\n"
        )
        is False
    )


def test_empty_shell_source_without_a_test():
    """A source with no test in it is judged on its own body."""
    # A module that asserts nothing asserts nothing.
    assert is_empty_shell_test("import os\n") is True
    assert is_empty_shell_test("assert True\n") is True

    # A module that does assert something is not a shell.
    assert is_empty_shell_test("assert compute() == 2\n") is False

    # A source that does not parse is broken, which is a different
    # finding from being a shell.
    assert is_empty_shell_test("def test_x(:\n") is False


# ---------------------------------------------------------------------------
# collect_only_probe
# ---------------------------------------------------------------------------


def test_collect_probe_injected(monkeypatch):
    """The probe runs through the injected runner, and only that."""
    # Negative control: the rule must not start a process itself.
    def forbidden(*args, **kwargs):
        raise AssertionError("collect_only_probe must not start a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    seen = []

    def runner(command):
        seen.append(command)
        return 0

    assert collect_only_probe("pytest -q tests/a.py", runner=runner) == 0
    assert len(seen) == 1
    assert "--collect-only" in seen[0]
    assert "tests/a.py" in seen[0]

    # A count that is not zero is reported as it was measured.
    assert collect_only_probe("pytest -q tests/a.py", runner=lambda cmd: 3) == 3


def test_collect_probe_rewrites_the_command():
    """The probe command is the collect-only form of the command."""
    seen = []

    def runner(command):
        seen.append(command)
        return 0

    # The flag lands on the pytest invocation, not on a later link.
    collect_only_probe("python -m pytest tests/unit -v", runner=runner)
    assert seen == ["python -m pytest --collect-only tests/unit -v"]

    # A command that is already a probe is left as it is.
    seen.clear()
    collect_only_probe("pytest -q tests/a.py --collect-only", runner=runner)
    assert seen == ["pytest -q tests/a.py --collect-only"]

    # A command with no pytest in it has nothing to make collect-only.
    seen.clear()
    collect_only_probe("echo hi", runner=runner)
    assert seen == ["echo hi"]


def test_rules_handle_their_remaining_shapes():
    """The rest of what the rules promise."""
    # A leading "./" is not part of the path.
    assert derive_files_to_modify(
        failure={"stack": "./tests/a.py:3"},
        diff=[],
    ) == ["tests/a.py"]

    # A record that is neither a mapping nor a string names no file.
    assert derive_files_to_modify(failure=42, diff=[]) == []

    # A comparison between constants is true before the code runs.
    assert is_empty_shell_test("def test_x(): assert 1 == 1") is True

    # There is nothing to probe in an empty command.
    assert collect_only_probe("", runner=lambda cmd: 5) == 0

    # A runner that answered with something unmeasurable measured nothing.
    assert collect_only_probe("pytest -q tests/a.py", runner=lambda cmd: "many") == 0
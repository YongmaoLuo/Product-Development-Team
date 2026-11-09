"""Tests for ``paths_named_by_command`` — the repo paths a command names.

A task carries a ``test_command`` and a ``files_to_modify`` list, and
the two have to agree: a command that names a file in the repository
says which file the task is about, so that file belongs in the
declared list.  The rule that reads the command out lives here, next to
the other command rules, so that the generator side and the audit side
read a command the same way.

The rule is deliberately narrow.  It answers one question — *which
paths inside the repository does this command name?* — and it answers
it with the paths **as the command wrote them**: no ``cd`` is
resolved, no path is checked against the filesystem, and no path is
invented.  A command that names no source file names no repo file, and
the answer is an empty list rather than a guess.

What is *not* a repo path is dropped rather than reported:

* an absolute path (``/abs/x.py``) — it is somewhere else on this
  machine, not in the repository;
* a path reached through ``~`` or ``$`` — ``$HOME/x.py`` is the user's
  home directory, not a file of the project;
* a path carrying ``..`` — it leaves the directory it was written in.

What is a repo path is kept, including a shell script: the incident
this rule was written for named a ``.sh`` file under ``scripts/``, and
a rule that only knew about Python files would have missed it.

The read-only allow-list (entry scripts, runner configuration,
``plans/``, ``tmp/``, virtualenv directories) **is** part of this
rule: a command names the entry script it runs and the configuration
it reads, and neither of those is a file the task has to change.  The
allow-list has a file of its own next door —
``test_test_command_read_only_whitelist.py`` — and nothing here
asserts anything about it.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import test_command_rules  # noqa: E402


# ---------------------------------------------------------------------------
# paths_named_by_command
# ---------------------------------------------------------------------------


def test_repo_relative_paths_are_returned():
    """A source file the command names is returned as the command wrote it."""
    assert test_command_rules.paths_named_by_command(
        "pytest tests/unit/test_b.py"
    ) == ["tests/unit/test_b.py"]

    # The path is reported as written: the ``cd`` is not resolved and the
    # absolute interpreter in the same command is not a repo file.
    assert test_command_rules.paths_named_by_command(
        "cd backend && /abs/.venv/bin/python3 -m pytest tests/unit/test_b.py -v"
    ) == ["tests/unit/test_b.py"]

    # "./" is another way of writing the same path.
    assert test_command_rules.paths_named_by_command(
        "pytest ./tests/unit/test_b.py"
    ) == ["tests/unit/test_b.py"]


def test_shell_scripts_are_returned():
    """A shell script is a source file like any other.

    The incident this rule answers named a ``.sh`` file under
    ``scripts/`` — a rule that only knew about Python files would have
    reported nothing and let the task through unchallenged.

    The script named here is one the task is *about*.  The entry
    script a command merely runs (``run_tests.sh``) is read-only and
    is dropped — see ``test_test_command_read_only_whitelist.py``.
    """
    assert test_command_rules.paths_named_by_command(
        "bash scripts/ci_gate.sh"
    ) == ["scripts/ci_gate.sh"]
    assert test_command_rules.paths_named_by_command(
        "sh scripts/verify.sh --fast"
    ) == ["scripts/verify.sh"]


def test_absolute_and_traversal_paths_are_not_returned():
    """A path outside the repository is not a repo path.

    An absolute path points somewhere else on this machine, and a path
    carrying ``..`` leaves the directory it was written in.  Neither
    names a file of the project, so neither is reported.
    """
    assert test_command_rules.paths_named_by_command("pytest /abs/x.py") == []
    assert test_command_rules.paths_named_by_command("pytest ../x.py") == []
    assert test_command_rules.paths_named_by_command(
        "pytest tests/../../outside.py"
    ) == []

    # A path the command reaches through the home directory or an
    # environment variable is that directory, not a file of the project.
    assert test_command_rules.paths_named_by_command("pytest ~/x.py") == []
    assert test_command_rules.paths_named_by_command("pytest $HOME/x.py") == []


def test_command_without_source_paths_yields_empty():
    """A command that names no source file names no repo file."""
    assert test_command_rules.paths_named_by_command("pytest --version") == []
    assert test_command_rules.paths_named_by_command("") == []
    assert test_command_rules.paths_named_by_command("   ") == []


def test_named_paths_keep_their_order_and_appear_once():
    """The same file named twice is one file, and the order is the
    order the command names them in."""
    assert test_command_rules.paths_named_by_command(
        "pytest tests/unit/test_a.py tests/unit/test_b.py tests/unit/test_a.py"
    ) == ["tests/unit/test_a.py", "tests/unit/test_b.py"]


def test_the_function_is_exported():
    """The rule is part of the module's public surface."""
    assert "paths_named_by_command" in test_command_rules.__all__

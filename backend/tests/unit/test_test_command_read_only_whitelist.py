"""Tests for the read-only allow-list ``paths_named_by_command`` applies.

A ``test_command`` names more than the files it wants changed.  It
names the entry script it runs, the runner configuration it reads, the
plan directory it works in, the scratch directory it writes into.  None
of those is a file the task is expected to modify, and a rule that
reported them would hand the repair a file the task never had to touch
— the same shape of mistake the incident was about, one step earlier.

So the rule drops the read-only paths.  What it must NOT drop is the
script the task really is about: ``scripts/ci_gate.sh`` is the file the
incident named, and an allow-list that swallowed it would have silenced
the very signal the rule was written for.

The allow-list is three groups, and each group is checked against the
path the command wrote:

* **prefixes** — the path starts with one of them (``plans/``,
  ``tmp/``, ``.pdt/``, ``backend/.venv/``, ``.venv/``);
* **basenames** — the file's last segment is one of them
  (``run_tests.sh``, ``pytest.ini``, …);
* **globs** — the file's last segment matches one of them
  (``vitest.config.*``, ``*.lock``, …).

The list is explicit on purpose: an allow-list nobody can recite is an
allow-list nobody can review, so the constants themselves are pinned
below alongside the behaviour they produce.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import test_command_rules  # noqa: E402


# ---------------------------------------------------------------------------
# paths_named_by_command — read-only allow-list
# ---------------------------------------------------------------------------


def test_read_only_entry_scripts_are_not_returned():
    """The entry script a command runs is read-only, not a deliverable.

    ``scripts/run_tests.sh`` is the gate's own entry point: the command
    runs it, it is not what the task is asked to change.
    """
    assert test_command_rules.paths_named_by_command(
        "bash scripts/run_tests.sh"
    ) == []
    assert test_command_rules.paths_named_by_command(
        "sh scripts/run_tests.sh --fast"
    ) == []
    # The same file reached through another directory is the same file.
    assert test_command_rules.paths_named_by_command(
        "bash backend/scripts/run_tests.sh"
    ) == []


def test_plan_and_tmp_paths_are_not_returned():
    """The plan directory and the scratch directory hold runtime artefacts.

    A plan's own bookkeeping and a scratch file are written by the
    runtime, not by the task, so naming one of them says nothing about
    what the task should change.
    """
    assert test_command_rules.paths_named_by_command(
        "pytest plans/example-plan/context/round-1.json"
    ) == []
    assert test_command_rules.paths_named_by_command(
        "python tmp/scratch/notes.py"
    ) == []
    assert test_command_rules.paths_named_by_command(
        "pytest .pdt/state.json"
    ) == []


def test_runner_config_files_are_not_returned():
    """A runner configuration is read-only; a source file beside it is not.

    The command names both — the configuration the runner reads and the
    file under test — and only the second one is a deliverable.
    """
    assert test_command_rules.paths_named_by_command(
        "pytest -c pytest.ini tests/unit/test_b.py"
    ) == ["tests/unit/test_b.py"]

    # Every configuration name on the list is read-only.
    for config in (
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "package.json",
        "Cargo.toml",
        "tsconfig.json",
    ):
        assert test_command_rules.paths_named_by_command(
            f"pytest {config} tests/unit/test_b.py"
        ) == ["tests/unit/test_b.py"], config

    # The glob group: a runner configuration whose name carries a suffix.
    assert test_command_rules.paths_named_by_command(
        "npm test --config vitest.config.ts"
    ) == []
    assert test_command_rules.paths_named_by_command(
        "npx playwright test --config playwright.config.js"
    ) == []


def test_non_whitelisted_shell_scripts_are_still_returned():
    """A script that is not on the list is still a file the task owns.

    ``scripts/ci_gate.sh`` is the script the incident was about: the
    command required it to change, so the rule has to report it.  An
    allow-list that filtered every shell script would have made this
    rule report nothing at all.
    """
    assert test_command_rules.paths_named_by_command(
        "bash scripts/ci_gate.sh"
    ) == ["scripts/ci_gate.sh"]
    assert test_command_rules.paths_named_by_command(
        "sh scripts/verify.sh --fast"
    ) == ["scripts/verify.sh"]


def test_virtualenv_paths_are_not_returned():
    """A file inside a virtualenv belongs to the environment, not the task.

    The interpreter and the installed packages live there; nothing in a
    task asks for them to be changed.
    """
    assert test_command_rules.paths_named_by_command(
        "pytest backend/.venv/lib/python3.11/site-packages/pkg/mod.py"
    ) == []
    assert test_command_rules.paths_named_by_command(
        "pytest .venv/lib/python3.11/site-packages/pkg/mod.py"
    ) == []


def test_the_allow_list_is_the_documented_one():
    """The allow-list is explicit: three groups, each written down.

    Two of the glob entries (``*.lock``, ``*.log``) name files whose
    suffix the source-file filter already rejects, so no command can
    report them either way — they are pinned here because the list
    itself is the contract, not only the paths that happen to reach it.
    """
    assert set(test_command_rules._READ_ONLY_PREFIXES) == {
        "plans/",
        "tmp/",
        ".pdt/",
        "backend/.venv/",
        ".venv/",
    }
    assert set(test_command_rules._READ_ONLY_BASENAMES) == {
        "run_tests.sh",
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "package.json",
        "Cargo.toml",
        "tsconfig.json",
    }
    assert set(test_command_rules._READ_ONLY_GLOBS) == {
        "vitest.config.*",
        "playwright.config.*",
        "*.lock",
        "*.log",
    }


def test_an_empty_command_still_names_nothing():
    """The allow-list does not change what an empty command reports."""
    assert test_command_rules.paths_named_by_command("") == []
    assert test_command_rules.paths_named_by_command("   ") == []
    assert test_command_rules.paths_named_by_command("pytest --version") == []

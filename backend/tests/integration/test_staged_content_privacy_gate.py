"""The pre-commit stage must actually block a commit carrying a leak.

Why an integration test and not just unit tests on the pure functions
---------------------------------------------------------------------
The three rule modules are pure functions with their own tests, and all of
them pass while the gate is useless if nothing calls them at commit time.
That is the failure this file exists to prevent: a rule that is defined,
tested, wired into a table -- and never reached from the entry point the
developer actually runs.

So every test here goes through ``scripts/scan_staged_privacy.sh``, the
shell wrapper the ``pre-commit`` hook invokes, and drives it with a real
index.

Index isolation
---------------
Every test points ``GIT_INDEX_FILE`` at a temporary index built with
``git read-tree``. The developer's real staging area is never read and
never written: a test that ran ``git add`` against the live index would
leave a staged file behind for whoever ran it next, and one that ran
``git reset`` would destroy their work. Blobs are injected with
``git update-index --cacheinfo`` so not even a temporary file has to be
written into the working tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GATES_DIR = REPO_ROOT / "backend" / "tests" / "static_gates"
WRAPPER = REPO_ROOT / "scripts" / "scan_staged_privacy.sh"

sys.path.insert(0, str(GATES_DIR))

# The five strings from the 2026-09-29 publication. Assembled from
# fragments: this file lives under the scan root and the private-name rule
# has no test exemption, so a literal would make the gate flag its own
# negative control.
LEAK_PLAN_ID = "2026" + "1010-" + "CC-Switch-" + "Remote-Aut"
LEAK_PROJECT = "frontend" + "-echarts"
LEAK_QUOTE = "symbol=" + "000001"

CLEAN_SOURCE = 'GREETING = "hello"\nCOUNT = 3\n'


def _git(env: dict[str, str], *args: str, stdin: bytes | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        env=env,
        input=stdin,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace')}"
        )
    return result.stdout.decode("utf-8", "replace")


def _staged_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose ``GIT_INDEX_FILE`` is a throwaway index."""
    index = tmp_path / "index"
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(index)
    _git(env, "read-tree", "HEAD")
    return env


def _stage(env: dict[str, str], path: str, content: str) -> None:
    """Put *content* at *path* in the index, without touching the worktree."""
    sha = _git(env, "hash-object", "-w", "--stdin", stdin=content.encode("utf-8")).strip()
    _git(env, "update-index", "--add", "--cacheinfo", f"100644,{sha},{path}")


def _scan(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the hook's entry point the way the hook does."""
    return subprocess.run(
        ["bash", str(WRAPPER)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        check=False,
    )


def test_a_clean_index_passes(tmp_path) -> None:
    """The ordinary case must not cost a developer anything."""
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", CLEAN_SOURCE)
    got = _scan(env)
    assert got.returncode == 0, f"clean content blocked:\n{got.stdout}\n{got.stderr}"


def test_a_plan_id_in_staged_source_is_blocked(tmp_path) -> None:
    """The 2026-09-29 shape, staged, stops the commit."""
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", f'PLAN = "{LEAK_PLAN_ID}"\n')
    got = _scan(env)
    assert got.returncode == 1, f"leak not blocked (exit {got.returncode}):\n{got.stdout}"
    assert LEAK_PLAN_ID in got.stderr, got.stderr


def test_a_private_name_in_staged_source_is_blocked(tmp_path) -> None:
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", f'REPO = "{LEAK_PROJECT}"\n')
    got = _scan(env)
    assert got.returncode == 1, f"leak not blocked (exit {got.returncode}):\n{got.stdout}"


def test_a_live_quote_symbol_is_blocked(tmp_path) -> None:
    env = _staged_env(tmp_path)
    _stage(env, "backend/agent.py", f'QUOTE = "{LEAK_QUOTE}"\n')
    got = _scan(env)
    assert got.returncode == 1, f"leak not blocked (exit {got.returncode}):\n{got.stdout}"


def test_the_private_name_rule_applies_to_tests_too(tmp_path) -> None:
    """A test file is exempt from the plan-id rule and not from this one.

    The boundary is the point of the pair: the plan-id rule exempts
    ``tests/`` because synthetic fixtures live there, while a private
    project name is as published inside a test as inside a docstring.
    """
    env = _staged_env(tmp_path)
    _stage(env, "backend/tests/unit/test_thing.py", f'NAME = "{LEAK_PROJECT}"\n')
    got = _scan(env)
    assert got.returncode == 1, f"private name in a test file was allowed:\n{got.stdout}"
    assert "private name" in got.stderr, got.stderr


def test_the_index_is_read_not_the_working_tree(tmp_path) -> None:
    """What is staged is what is judged, even if the file was edited after.

    This is the reason the gate reads the index at all. A file can be
    staged with a leak and then tidied on disk; a scan of the working tree
    would report the tidied version and let through a commit carrying the
    staged one.
    """
    env = _staged_env(tmp_path)
    _stage(env, "backend/never_written_to_disk.py", f'PLAN = "{LEAK_PLAN_ID}"\n')
    # No such file exists in the working tree at all, which is the strongest
    # form of the check: anything reading the filesystem finds nothing.
    assert not (REPO_ROOT / "backend" / "never_written_to_disk.py").exists()
    got = _scan(env)
    assert got.returncode == 1, f"the index was not read:\n{got.stdout}\n{got.stderr}"
    assert "never_written_to_disk.py" in got.stderr, got.stderr


def test_an_empty_index_is_an_error_not_a_pass(tmp_path) -> None:
    """Nothing staged must not look like everything staged being clean."""
    env = dict(os.environ)
    # A path git has never seen: no index exists, so nothing can be read.
    empty = tmp_path / "never-created.index"
    empty.unlink(missing_ok=True)
    env["GIT_INDEX_FILE"] = str(empty)
    got = _scan(env)
    assert got.returncode == 2, (
        f"an unread index reported {got.returncode}, not 2:\n{got.stdout}\n{got.stderr}"
    )
    assert "ERROR" in got.stderr, got.stderr


def test_the_hook_is_registered_for_the_pre_commit_stage() -> None:
    """The wrapper above must be what the pre-commit hook actually calls.

    Without this, the six tests above prove a script works while the hook
    keeps calling something else -- and the gate is inert.
    """
    installer = (REPO_ROOT / "scripts" / "install_git_hooks.sh").read_text(encoding="utf-8")
    assert "scripts/pre_commit_gate.sh" in installer, (
        "install_git_hooks.sh no longer points the pre-commit hook at the "
        "dispatcher, so the staged privacy scan would not run at commit time"
    )
    dispatcher = (REPO_ROOT / "scripts" / "pre_commit_gate.sh").read_text(encoding="utf-8")
    assert "scan_staged_privacy.sh" in dispatcher, (
        "the pre-commit dispatcher no longer runs the staged privacy scan"
    )
    config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "scripts/scan_staged_privacy.sh" in config, (
        ".pre-commit-config.yaml no longer declares the staged privacy hook"
    )

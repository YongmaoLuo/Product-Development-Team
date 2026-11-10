"""A change another task made must not be blamed on this task.

The accident
------------

Concurrent tasks share one worktree. Task 1 starts, captures its
baseline (the HEAD at that moment) and works; while it works, task 2
commits its own files into the SAME repository. Task 1's change set is
computed as "everything that differs from my baseline", so task 2's
commit shows up inside task 1's set — and the undeclared-write gate
then fails task 1 for writing files task 1 never touched (and never
declared).

This file pins the attribution filter that removes those paths:

* a commit whose subject is ``[task-<id>]`` attributes its files to
  that task;
* a path locked by another task is attributed to that task;
* everything else — untagged commits, mixed ownership, this task's own
  commits — stays in the change set.

The task ids used here are the real ones (numeric, with refiner
children like ``"3-2"``), because the lineage comparison is
lineage-based: ``_lineage_id("3") == _lineage_id("3-2") == "3"``.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402
from git_manager import GitManager  # noqa: E402
from task import SubTask  # noqa: E402

#: The file this task declares and is allowed to touch.
DECLARED = "own.py"

#: The file that belongs to the concurrent task.
OTHER_TASK_FILE = "other_task.py"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tiny git repository with one committed baseline."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / DECLARED).write_text("original\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "gate@example.com")
    _git(root, "config", "user.name", "undeclared write attribution")
    _git(root, "add", DECLARED)
    _git(root, "commit", "-q", "-m", "base")
    return root


def _commit(repo: Path, rel: str, message: str, content: str = "new\n") -> None:
    """Commit a new file with ``message`` as the commit subject."""
    (repo / rel).write_text(content, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", message)


def _make_task(task_id: str = "1"):
    """A task that declares exactly ``DECLARED``."""
    return SubTask(
        id=task_id,
        title="Do the declared thing",
        description="Touch only the declared file.",
        test_command="",
        files_to_modify=[DECLARED],
    )


def _make_agent(repo: Path, *, broker=None):
    """A minimal ``AutonomousAgent`` skeleton over ``repo``.

    Built with ``__new__`` on purpose: the production constructor is
    heavyweight, and the filter under test needs nothing but the
    project dir, a logger and a lock broker.
    """
    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = repo
    agent.logger = MagicMock()
    agent._fake_broker = broker
    agent._active_lock_broker = lambda: agent._fake_broker
    return agent


class FakeBroker:
    """Stands in for :class:`FileLockBroker` — only ``holders()`` matters."""

    def __init__(self, holders: dict[str, list[str]]):
        self._holders = holders

    def holders(self) -> dict[str, list[str]]:
        return dict(self._holders)


def test_concurrent_task_commit_is_excluded(repo: Path):
    """A file committed by another task is not this task's undeclared write."""
    baseline = _git(repo, "rev-parse", "HEAD")
    _commit(repo, OTHER_TASK_FILE, "[task-2] add the concurrent task's file")

    agent = _make_agent(repo)
    task = _make_task("1")

    excess = agent._undeclared_writes_this_attempt(task, baseline)

    assert OTHER_TASK_FILE not in excess


def test_own_commit_is_kept(repo: Path):
    """This task's own commit stays in its change set."""
    baseline = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "undeclared_by_me.py", "[task-1] my own stray write")

    agent = _make_agent(repo)
    task = _make_task("1")

    excess = agent._undeclared_writes_this_attempt(task, baseline)

    assert "undeclared_by_me.py" in excess


def test_untagged_commit_is_kept(repo: Path):
    """A commit without a ``[task-...]`` subject is not attributed."""
    baseline = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "untagged.py", "just a commit, no task tag")

    agent = _make_agent(repo)
    task = _make_task("1")

    excess = agent._undeclared_writes_this_attempt(task, baseline)

    assert "untagged.py" in excess


def test_mixed_ownership_is_kept(repo: Path):
    """A path both this task and another task touched stays put."""
    baseline = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "mixed.py", "[task-2] touched it too")
    (repo / "mixed.py").write_text("and again\n", encoding="utf-8")
    _git(repo, "add", "mixed.py")
    _git(repo, "commit", "-q", "-m", "[task-1] and me")

    agent = _make_agent(repo)
    task = _make_task("1")

    excess = agent._undeclared_writes_this_attempt(task, baseline)

    assert "mixed.py" in excess


def test_locked_by_other_task_is_excluded(repo: Path):
    """A file another task holds the lock on is not this task's write."""
    baseline = _git(repo, "rev-parse", "HEAD")
    (repo / "locked_by_other.py").write_text("in progress\n", encoding="utf-8")

    broker = FakeBroker({"2": ["locked_by_other.py"]})
    agent = _make_agent(repo, broker=broker)
    task = _make_task("1")

    excess = agent._undeclared_writes_this_attempt(task, baseline)

    assert "locked_by_other.py" not in excess


def test_locked_by_this_task_is_kept(repo: Path):
    """This task's own lock does not excuse an undeclared write."""
    baseline = _git(repo, "rev-parse", "HEAD")
    (repo / "locked_by_me.py").write_text("mine\n", encoding="utf-8")

    broker = FakeBroker({"1": ["locked_by_me.py"]})
    agent = _make_agent(repo, broker=broker)
    task = _make_task("1")

    excess = agent._undeclared_writes_this_attempt(task, baseline)

    assert "locked_by_me.py" in excess


def test_no_broker_still_uses_commits(repo: Path):
    """With no broker running, the commit signal alone decides."""
    baseline = _git(repo, "rev-parse", "HEAD")
    _commit(repo, OTHER_TASK_FILE, "[task-2] add the concurrent task's file")

    agent = _make_agent(repo, broker=None)
    task = _make_task("1")

    excess = agent._undeclared_writes_this_attempt(task, baseline)

    assert OTHER_TASK_FILE not in excess


def test_empty_baseline_filters_nothing(repo: Path):
    """No baseline means no comparison — nothing is filtered."""
    _commit(repo, OTHER_TASK_FILE, "[task-2] add the concurrent task's file")

    agent = _make_agent(repo)
    task = _make_task("1")

    assert agent._paths_owned_by_other_tasks(task, "", [OTHER_TASK_FILE]) == set()
    assert agent._undeclared_writes_this_attempt(task, "") == []


# ---------------------------------------------------------------------------
# The same accident, driven through the executor's own entry point.
# ---------------------------------------------------------------------------

def _make_executor_agent(repo: Path, *, concurrent_commit: str):
    """An agent whose sub-agent makes ANOTHER task's commit mid-run.

    The commit is made while the task runs, which is exactly the
    concurrent case: the concurrent task commits into the shared
    worktree between this task's baseline and the gate's check.
    """
    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.project_dir = repo
    agent.subagent_cfg = None
    agent.logger = MagicMock()
    agent.config = MagicMock()
    agent.config.executor_system_prompt = "fake prompt"
    agent.executor = MagicMock()
    agent.executor.had_previous_timeout.return_value = False
    agent.retry_manager = MagicMock()
    agent.retry_manager.get_retry_prompt_modifier.return_value = ""
    agent.task_manager = MagicMock()
    agent.task_manager.tasks_file = (
        Path(tempfile.mkdtemp(prefix="pdt-undeclared-write-attribution-"))
        / "tasks.json"
    )
    agent.task_manager.record_task_superseded.return_value = False
    agent.plan_id = "pdt-undeclared-write-attribution"
    agent.rollback_manager = MagicMock()
    agent._session_task_completed_counts = {}
    agent._failure_occurrences = {}
    agent._refinement_count = 0
    agent.git_manager = GitManager(str(repo))

    def _fake_query(*_args, **_kwargs) -> str:
        _commit(repo, concurrent_commit, "[task-2] the concurrent task's commit")
        return "Done.\nTEST_RESULT: PASSED"

    agent.coding_tool = MagicMock()
    agent.coding_tool.query.side_effect = _fake_query

    agent._preflight_test_command_skip = MagicMock(return_value=False)
    agent._parse_test_result = MagicMock(return_value=(True, ""))
    agent._cross_verify_test_result = MagicMock(return_value=(True, ""))
    agent._task_declared_files_exist = MagicMock(return_value=True)
    agent._get_git_diff_stat_for_review = MagicMock(return_value="")
    agent._refine_after_failure = MagicMock()
    agent._persist_task_status = MagicMock()
    agent._commit_task_changes = MagicMock()
    agent._audit_task_second_pass = MagicMock(return_value=(True, ""))
    agent._active_lock_broker = lambda: None
    return agent


def test_executor_completes_a_task_that_did_not_write_the_other_files(
    repo: Path,
):
    """The executor's own gate must not fail the task for another task's file.

    This is the production entry point: ``_execute_task_with_retry`` →
    ``_undeclared_writes_this_attempt``. Without the attribution filter
    the concurrent task's committed file lands in this task's change set
    and the task is failed for a write it never made.
    """
    agent = _make_executor_agent(repo, concurrent_commit=OTHER_TASK_FILE)
    task = _make_task(f"1-{uuid.uuid4().hex[:10]}")

    ok = agent._execute_task_with_retry(task, max_retries=1)

    assert ok is True, (
        "the executor failed a task for a file a concurrent task committed "
        "— the change set is not attributed to the task that wrote it"
    )

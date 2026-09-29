"""Regression tests for the empty-output completion gate.

Background
----------
2026-08-19: read-only
investigation tasks with an empty ``test_command``) were marked
``completed`` on an EMPTY git commit. The completion path's only gate
was ``tests_passed``; for a task with ``test_command == ""`` the
cross-verify layer falls back to the AI's self-reported claim, so a
subagent that produced NO changes still reached ``completed`` — then
got re-scheduled and re-ran (burning tokens) until the same-id loop
guard tripped. The same hole let a run whose diff was review-blocked
("inline_review_blocked") re-complete on an empty follow-up diff.

The fix inserts an empty-output gate between ``tests_passed`` and the
git checkpoint commit: when a run produces NO changed files AND the
task's declared deliverables are not already on disk, the task is
failed/retried instead of marked completed. The helper
``_task_declared_files_exist`` distinguishes a legitimate no-op re-run
(deliverable already committed previously) from a genuine empty output.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _git_init(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(project_dir), capture_output=True, check=True)


def _make_agent(project_dir: Path):
    from agent import AutonomousAgent

    (project_dir / "tasks.json").write_text(
        '{"requirement": "x", "tasks": []}', encoding="utf-8"
    )
    return AutonomousAgent(
        requirement="x",
        project_dir=project_dir,
        coding_tool=None,
        config={},
        logger=None,
        tasks_file=project_dir / "tasks.json",
    )


def test_sentinel_only_task_no_prior_commit_is_empty_output(tmp_path):
    """Sentinel task with NO prior ``[task-id]`` commit → False.

    An empty first-run diff on a sentinel task is genuine empty output and
    must be blocked from completing.
    """
    from task import SubTask, UNKNOWN_MODIFICATIONS_SENTINEL

    _git_init(tmp_path)
    agent = _make_agent(tmp_path)
    task = SubTask(
        id="1", title="x", description="d",
        files_to_modify=list(UNKNOWN_MODIFICATIONS_SENTINEL),
    )
    assert agent._task_declared_files_exist(task) is False


def test_sentinel_task_with_prior_commit_is_legit_noop(tmp_path):
    """Sentinel task WITH a prior ``[task-id]`` commit → True.

    Mirrors task #1: ``etf_root_cause_report.json`` was committed in an
    earlier ``[task-1]`` attempt, so a later re-run producing an empty diff
    is a legitimate no-op and may complete.
    """
    from task import SubTask, UNKNOWN_MODIFICATIONS_SENTINEL

    _git_init(tmp_path)
    # Commit a deliverable under the task's checkpoint prefix.
    (tmp_path / "report.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=t",
         "commit", "-m", "[task-1] ETF root-cause investigation"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    agent = _make_agent(tmp_path)
    task = SubTask(
        id="1", title="x", description="d",
        files_to_modify=list(UNKNOWN_MODIFICATIONS_SENTINEL),
    )
    assert agent._task_declared_files_exist(task) is True


def test_declared_file_present_is_legit_noop_rerun(tmp_path):
    """Declared deliverable already on disk → True (legit no-op re-run).

    Mirrors task #1: ``etf_root_cause_report.json`` was committed in an
    earlier attempt, so a later re-run legitimately produces an empty
    diff and may complete.
    """
    from task import SubTask

    _git_init(tmp_path)
    (tmp_path / "report.json").write_text("{}", encoding="utf-8")
    agent = _make_agent(tmp_path)
    task = SubTask(id="2", title="x", description="d", files_to_modify=["report.json"])
    assert agent._task_declared_files_exist(task) is True


def test_declared_file_missing_is_genuine_empty_output(tmp_path):
    """Declared deliverable absent → False (empty output is genuine).

    This is the bug case: empty diff AND no deliverable on disk means the
    subagent produced nothing — must NOT be marked completed.
    """
    from task import SubTask

    _git_init(tmp_path)
    agent = _make_agent(tmp_path)
    task = SubTask(id="3", title="x", description="d", files_to_modify=["missing.py"])
    assert agent._task_declared_files_exist(task) is False


def test_empty_files_to_modify_returns_false(tmp_path):
    """An empty ``files_to_modify`` declares nothing → False."""
    from task import SubTask

    _git_init(tmp_path)
    agent = _make_agent(tmp_path)
    task = SubTask(id="4", title="x", description="d", files_to_modify=[])
    assert agent._task_declared_files_exist(task) is False


def test_all_declared_files_must_exist(tmp_path):
    """ALL concrete declared files must exist — one missing → False."""
    from task import SubTask

    _git_init(tmp_path)
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    agent = _make_agent(tmp_path)
    task = SubTask(
        id="5", title="x", description="d",
        files_to_modify=["a.py", "b.py"],  # b.py does not exist
    )
    assert agent._task_declared_files_exist(task) is False

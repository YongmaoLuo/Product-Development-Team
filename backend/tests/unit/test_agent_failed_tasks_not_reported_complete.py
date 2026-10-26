"""A run with failed tasks must not report "All tasks completed".

2026-10-26 (B10) — the completion claim that was a lie
-------------------------------------------------------
The dispatcher's "nothing left to schedule" branch answered
``"All tasks completed!"`` and wrote a ``success`` marker onto
``tasks.json`` — whatever the tasks actually looked like.

The branch is reached when *nothing is schedulable any more*, and that
happens in two very different worlds:

  1. every task finished successfully, and
  2. what is left is broken — one task failed and the rest depend on
     it, so nothing can run.

Both worlds landed in the same branch, so world 2 was announced as
world 1: the run wrote ``stop_reason="success"``, the log said
"All tasks completed", and the operator reading either believed the
plan was done while part of it was red on the card. The ``success``
marker is worse than the print — it is persisted, and every later
reader (the dashboard, the resume path, the next run) takes it as
"the work is finished".

What these tests pin:

  * one failed task among the finished ones → the run says ``failed``,
    says how many tasks failed, and never says "All tasks completed";
  * every task finished → the run still says ``success`` and still
    says "All tasks completed" (the guard against fixing B10 by
    breaking the happy path).

The dispatcher is driven through the real ``_run_async``, so the
assertions are about the loop, not about a re-implementation of it.
The task statuses are written through the real ``TaskManager`` API
(``update_task_status`` / ``record_task_failure``) so they survive the
reload the dispatcher performs at the start of a run — exactly the way
they survive a real crash-and-resume.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


#: The directory the plan's ``tasks.json`` lives in. A real plan id
#: (not a pytest tmp dir name) keeps the plan-id derivation on its
#: normal path.
_PLAN_DIR_NAME = "plan-under-test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _LogRecorder:
    """Collects the structured events the agent writes."""

    def __init__(self) -> None:
        self.events = []

    def _record(self, level):
        def _fn(event, message, **kwargs):
            self.events.append({
                "level": level.upper(),
                "event": event,
                "message": message,
                "data": kwargs.get("data"),
            })
        return _fn

    def __getattr__(self, name):
        if name in ("info", "warning", "error", "debug", "critical"):
            return self._record(name)
        raise AttributeError(name)

    def names(self):
        return [e["event"] for e in self.events]

    def messages(self):
        return [e["message"] for e in self.events]

    def data_for(self, event):
        for entry in self.events:
            if entry["event"] == event:
                return entry["data"]
        return None

    def count(self, event):
        return self.names().count(event)


def _git_init(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir), capture_output=True, text=True, check=False,
    )
    if not (project_dir / ".git").exists():
        subprocess.run(
            ["git", "init"],
            cwd=str(project_dir), capture_output=True, text=True, check=True,
        )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    )


def _task(task_id: str, depends_on=None) -> dict:
    """A read-only task — it takes no file lock, so the dispatcher
    test never touches the OS lock machinery."""
    return {
        "id": task_id,
        "title": f"task {task_id}",
        "description": "does the work",
        "test_command": "true",
        "depends_on": list(depends_on or []),
        "files_to_modify": [],
    }


def _make_agent(tmp_path, logger, tasks):
    """Build an agent bound to a plan directory under ``tmp_path``."""
    from agent import AutonomousAgent
    from config import AgentConfig

    plan_dir = tmp_path / _PLAN_DIR_NAME
    plan_dir.mkdir(parents=True, exist_ok=True)

    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text(
        json.dumps({"requirement": "b10 spec", "tasks": tasks}, indent=2),
        encoding="utf-8",
    )

    agent = AutonomousAgent(
        requirement="b10 spec",
        project_dir=tmp_path,
        coding_tool=None,
        config=AgentConfig(planner_system_prompt="spec", max_retries=1),
        logger=logger,
        tasks_file=tasks_file,
    )
    return agent, plan_dir


def _tasks_on_disk(plan_dir: Path) -> dict:
    return json.loads((plan_dir / "tasks.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _hermetic_state_db(tmp_path, monkeypatch):
    """Point ``state.db`` at a per-test file.

    ``plan_id`` is the SQLite key for the runtime-status rows, and the
    default database is shared by every test in the process. A row
    leaked from another test would hydrate into this one and change
    what the dispatcher sees.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))


# ---------------------------------------------------------------------------
# A failed task must not be reported as a completed one
# ---------------------------------------------------------------------------


def test_failed_task_is_not_reported_as_all_completed(tmp_path):
    """One completed + one failed → the run says ``failed``.

    The completion claim is the whole bug: an operator who reads
    "All tasks completed" stops looking, and a broken task stays
    broken and unnoticed.
    """
    _git_init(tmp_path)
    logger = _LogRecorder()
    agent, plan_dir = _make_agent(
        tmp_path, logger, [_task("1"), _task("2")],
    )

    # The statuses are written through the real TaskManager API so
    # they survive the reload the dispatcher performs at the start of
    # the run — the same way they survive a crash-and-resume.
    agent.task_manager.update_task_status("1", "completed")
    agent.task_manager.record_task_failure("2", "the build broke")

    asyncio.run(agent._run_async())

    assert agent.task_manager.stop_reason == "failed", (
        "a run that ended with a failed task must not claim success — "
        f"got: {agent.task_manager.stop_reason!r}"
    )
    assert not any(
        "All tasks completed" in (m or "") for m in logger.messages()
    ), (
        "the run must never say 'All tasks completed' when a task "
        f"failed. Log messages: {logger.messages()!r}"
    )
    assert any(
        "Completed with 1 failed task(s)" in (m or "")
        for m in logger.messages()
    ), (
        "the run must say how many tasks failed. Log messages: "
        f"{logger.messages()!r}"
    )

    data = logger.data_for("execution_completed")
    assert data is not None, (
        "the completion event must still be written — it is the one "
        "event an operator's dashboard reads"
    )
    assert data["total"] == 2
    assert data["completed"] == 1
    assert data["failed"] == 1

    # And the marker on disk says the same thing.
    assert _tasks_on_disk(plan_dir)["stop_reason"] == "failed"


def test_failed_task_status_is_not_touched_by_the_completion_branch(tmp_path):
    """The branch reports; it does not re-judge.

    A failed task's status was decided elsewhere
    (``record_task_failure``). This branch only says what the RUN
    concluded, so it must not rewrite a task's status — neither the
    failed one (into "completed") nor the completed one.
    """
    _git_init(tmp_path)
    logger = _LogRecorder()
    agent, _plan_dir = _make_agent(
        tmp_path, logger, [_task("1"), _task("2")],
    )

    agent.task_manager.update_task_status("1", "completed")
    agent.task_manager.record_task_failure("2", "the build broke")

    asyncio.run(agent._run_async())

    statuses = {t.id: t.status for t in agent.task_manager.tasks}
    assert statuses["1"] == "completed", (
        f"the completed task must stay completed. Got: {statuses!r}"
    )
    assert statuses["2"] == "failed", (
        f"the failed task must stay failed. Got: {statuses!r}"
    )


def test_all_completed_still_reports_success(tmp_path):
    """The happy path must be untouched by the B10 fix.

    Without this test, "always say failed" would satisfy the test
    above.
    """
    _git_init(tmp_path)
    logger = _LogRecorder()
    agent, plan_dir = _make_agent(
        tmp_path, logger, [_task("1"), _task("2")],
    )

    agent.task_manager.update_task_status("1", "completed")
    agent.task_manager.update_task_status("2", "completed")

    asyncio.run(agent._run_async())

    assert agent.task_manager.stop_reason == "success", (
        "a run where everything finished says success. Got: "
        f"{agent.task_manager.stop_reason!r}"
    )
    assert any(
        "All tasks completed" in (m or "") for m in logger.messages()
    ), (
        "the happy path still says 'All tasks completed'. Log messages: "
        f"{logger.messages()!r}"
    )
    assert not any(
        "failed task(s)" in (m or "") for m in logger.messages()
    ), (
        "a run with no failed task must not mention failed tasks. Log "
        f"messages: {logger.messages()!r}"
    )

    data = logger.data_for("execution_completed")
    assert data is not None
    assert data["total"] == 2
    assert data["completed"] == 2
    assert "failed" not in data, (
        "the success path's payload is unchanged — it carries no "
        f"'failed' key. Got: {data!r}"
    )
    assert _tasks_on_disk(plan_dir)["stop_reason"] == "success"

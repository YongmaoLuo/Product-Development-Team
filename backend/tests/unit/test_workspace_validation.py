"""
Workspace validation regression tests for sub-task ``project_dir``.

Background
----------
When a task fails repeatedly, ``AutonomousAgent._breakdown_task`` (and
the related ``_refine_after_failure`` path) ask the LLM to split it
into 2-3 smaller sub-tasks. The LLM also gets to set each sub-task's
``project_dir`` — and historically it has hallucinated unrelated paths
that don't exist on the executor's filesystem. The sub-task then fails
with ``file or directory not found`` and the failure → breakdown loop
spins until the plan exhausts retries.

The fix adds a workspace allow-list:
  1. ``_load_tasks`` snapshots every original task's ``project_dir``
     into ``self._valid_workspace_roots`` (plus the agent's own
     ``project_dir``).
  2. ``_is_valid_subtask_workspace(candidate)`` checks if a candidate
     path is one of those roots, or a sub-directory of one.
  3. ``_enforce_subtask_workspace(new_task, parent_task)`` uses that
     check and replaces any LLM-hallucinated path with the parent's
     ``project_dir`` (logging ``breakdown_workspace_corrected``).
  4. ``_breakdown_task`` and ``_refine_after_failure`` call the
     enforcer before persisting the new sub-tasks.

These tests pin the contract at the unit level (the two helper methods)
plus a minimal integration test on ``_breakdown_task`` that exercises
the hook end-to-end with a stubbed LLM.
"""

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _git_init(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    if not (project_dir / ".git").exists():
        subprocess.run(["git", "init"], cwd=str(project_dir),
                       capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=str(project_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "T"],
                   cwd=str(project_dir), capture_output=True, check=True)


class _DummyCodingTool:
    """Stub coding tool — captures the LLM query, returns a fixed JSON.

    For the unit tests we never reach the LLM; for the breakdown
    integration test we configure ``query_json`` to return the
    breakdown response we want to validate against.
    """
    def __init__(self, response=None):
        self._response = response or {"tasks": []}
        self.calls = []

    def query_json(self, prompt, system_instruction=""):
        self.calls.append({"prompt": prompt, "system": system_instruction})
        return self._response


def _build_agent(project_dir: Path, **kwargs):
    from agent import AutonomousAgent
    return AutonomousAgent(
        requirement="test",
        project_dir=project_dir,
        coding_tool=_DummyCodingTool(),
        logger=None,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Test 1: _is_valid_subtask_workspace boundary matrix
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_with_roots(tmp_path):
    """Agent with a fixed allow-list for predictable assertions."""
    project_dir = tmp_path / "project"
    _git_init(project_dir)

    repo_a = tmp_path / "repoA"
    repo_b = tmp_path / "repoB"
    repo_a.mkdir()
    repo_b.mkdir()

    agent = _build_agent(project_dir)
    # Override the allow-list (normally built from tasks.json in
    # _load_tasks). Using resolved Paths matches the real impl.
    agent._valid_workspace_roots = {repo_a.resolve(), repo_b.resolve()}
    return agent, repo_a, repo_b, tmp_path


def test_is_valid_workspace_accepts_root_itself(agent_with_roots):
    agent, repo_a, repo_b, _ = agent_with_roots
    assert agent._is_valid_subtask_workspace(str(repo_a)) is True
    assert agent._is_valid_subtask_workspace(str(repo_b)) is True


def test_is_valid_workspace_accepts_subdirectory(agent_with_roots):
    agent, repo_a, _, _ = agent_with_roots
    sub = repo_a / "src" / "deep" / "nest"
    assert agent._is_valid_subtask_workspace(str(sub)) is True


def test_is_valid_workspace_rejects_unrelated_path(agent_with_roots):
    agent, repo_a, repo_b, tmp_path = agent_with_roots
    # A path that's NOT a parent or child of any allow-listed root.
    elsewhere = tmp_path / "unrelated" / "elsewhere"
    assert agent._is_valid_subtask_workspace(str(elsewhere)) is False


def test_is_valid_workspace_rejects_sibling_prefix_not_ancestor(agent_with_roots):
    """``/foo/bar-x`` must NOT be accepted as a child of ``/foo/bar``.

    String-prefix matching would wrongly accept ``/foo/bar-x`` as a
    child of ``/foo/bar``. The implementation uses
    ``Path.relative_to`` (which is ancestor-aware) to avoid this.
    """
    agent, repo_a, _, tmp_path = agent_with_roots
    sibling = tmp_path / "repoA-suffix"
    sibling.mkdir()
    assert agent._is_valid_subtask_workspace(str(sibling)) is False


def test_is_valid_workspace_accepts_empty_candidate(agent_with_roots):
    agent, _, _, _ = agent_with_roots
    # Empty / None means "unset" — treat as valid so the sub-task
    # inherits the parent's project_dir downstream.
    assert agent._is_valid_subtask_workspace(None) is True
    assert agent._is_valid_subtask_workspace("") is True


def test_is_valid_workspace_accepts_anything_when_allowlist_empty(tmp_path):
    """When the allow-list is empty (no original project_dir anywhere),
    everything is valid — don't block a legitimate flow.
    """
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    agent = _build_agent(project_dir)
    agent._valid_workspace_roots = set()
    assert agent._is_valid_subtask_workspace("/anything/here") is True


def test_is_valid_workspace_rejects_unresolvable_path(agent_with_roots):
    """A path with a null byte (or other OSError on resolve) is rejected."""
    agent, _, _, _ = agent_with_roots
    # Null bytes in path strings cause OSError on .resolve() on POSIX.
    assert agent._is_valid_subtask_workspace("/foo\x00bar") is False


# ---------------------------------------------------------------------------
# Test 2: _enforce_subtask_workspace corrects invalid paths
# ---------------------------------------------------------------------------


class _RecordingLogger:
    """Minimal logger that captures events for assertions."""
    def __init__(self):
        self.events = []
    def info(self, event, message, **kwargs):
        self.events.append({"event": event, "message": message, **kwargs})
    def warning(self, event, message, **kwargs):
        self.events.append({"event": event, "message": message, **kwargs})
    def error(self, event, message, **kwargs):
        self.events.append({"event": event, "message": message, **kwargs})
    def debug(self, event, message, **kwargs):
        self.events.append({"event": event, "message": message, **kwargs})


def test_enforce_workspace_keeps_valid_candidate(agent_with_roots):
    agent, repo_a, _, tmp_path = agent_with_roots
    from task import SubTask
    parent = SubTask(id="1", title="p", description="d",
                     test_command="echo", status="failed",
                     project_dir=str(repo_a))
    sub = {"id": "1-1", "title": "child", "description": "c",
           "test_command": "echo", "status": "pending",
           "project_dir": str(repo_a / "subdir")}
    out = agent._enforce_subtask_workspace(sub, parent)
    assert out["project_dir"] == str(repo_a / "subdir")


def test_enforce_workspace_corrects_hallucinated_path(agent_with_roots):
    """The bug this file is for: an LLM-hallucinated unrelated path
    gets replaced with the parent's workspace, and a warning event is
    logged."""
    agent, repo_a, _, tmp_path = agent_with_roots
    agent.logger = _RecordingLogger()
    from task import SubTask
    parent = SubTask(id="1", title="p", description="d",
                     test_command="echo", status="failed",
                     project_dir=str(repo_a))

    hallucinated = str(tmp_path / "hallucinated")
    sub = {"id": "1-2", "title": "child2", "description": "c",
           "test_command": "echo", "status": "pending",
           "project_dir": hallucinated}
    out = agent._enforce_subtask_workspace(sub, parent)

    assert out["project_dir"] == str(repo_a), (
        f"hallucinated path {hallucinated!r} must be replaced with "
        f"parent workspace; got {out.get('project_dir')!r}"
    )
    # Warning event captured.
    ws_events = [e for e in agent.logger.events
                 if e["event"] == "breakdown_workspace_corrected"]
    assert len(ws_events) == 1, (
        f"expected one breakdown_workspace_corrected event; "
        f"got {[e['event'] for e in agent.logger.events]}"
    )
    assert ws_events[0]["task_id"] == "1"
    assert ws_events[0]["data"]["original_project_dir"] == hallucinated
    assert ws_events[0]["data"]["fallback_project_dir"] == str(repo_a)


def test_enforce_workspace_does_not_mutate_input(agent_with_roots):
    """The enforcer returns a NEW dict — caller's input is untouched.

    Important for the refinement path where the caller holds the full
    updated_tasks list and may want to inspect what the LLM produced
    before our rewrite.
    """
    agent, repo_a, _, tmp_path = agent_with_roots
    from task import SubTask
    parent = SubTask(id="1", title="p", description="d",
                     test_command="echo", status="failed",
                     project_dir=str(repo_a))
    sub = {"id": "1-3", "title": "child3", "description": "c",
           "test_command": "echo", "status": "pending",
           "project_dir": str(tmp_path / "bogus")}
    out = agent._enforce_subtask_workspace(sub, parent)
    # Input dict unchanged.
    assert sub["project_dir"] == str(tmp_path / "bogus")
    # Output dict corrected.
    assert out["project_dir"] == str(repo_a)


# ---------------------------------------------------------------------------
# Test 3: _load_tasks populates _valid_workspace_roots from tasks.json
# ---------------------------------------------------------------------------


def test_load_tasks_populates_workspace_roots(tmp_path):
    """``_load_tasks`` snapshots every original task's ``project_dir``
    into ``_valid_workspace_roots`` (plus ``self.project_dir``).

    This is the bootstrap step that makes the allow-list work. If a
    future refactor breaks the snapshot, every other test in this
    file is moot — the enforcer will accept everything.
    """
    from agent import AutonomousAgent

    project_dir = tmp_path / "project"
    _git_init(project_dir)
    repo_a = tmp_path / "repoA"
    repo_b = tmp_path / "repoB"
    repo_a.mkdir()
    repo_b.mkdir()

    canonical = tmp_path / "plan" / "tasks.json"
    canonical.parent.mkdir()
    # 2026-09-09 two-constant scheme:
    # step 3 accepts ``__NO_FILE_CHANGES__`` ("read-only by author
    # intent") directly, while ``__UNKNOWN_MODIFICATIONS__`` ("should
    # modify files, list unknown") is deliberately REJECTED so the
    # dispatcher's subagent fill loop fires. This test exercises
    # workspace-root snapshotting, not files_to_modify validation, so
    # the read-only sentinel is the one to use — the unknown sentinel
    # would send the load through the fill loop and out the other side
    # with a post-read gate failure.
    canonical.write_text(json.dumps({
        "requirement": "load-roots test",
        "tasks": [
            {"id": "1", "title": "t1", "description": "d",
             "test_command": "echo", "status": "pending",
             "project_dir": str(repo_a),
             "files_to_modify": ["__NO_FILE_CHANGES__"]},
            {"id": "2", "title": "t2", "description": "d",
             "test_command": "echo", "status": "pending",
             "project_dir": str(repo_b),
             "files_to_modify": ["__NO_FILE_CHANGES__"]},
        ],
    }), encoding="utf-8")

    agent = AutonomousAgent(
        requirement="load-roots test",
        project_dir=project_dir,
        coding_tool=_DummyCodingTool(),
        logger=None,
        tasks_file=canonical,
    )
    agent._load_tasks()

    roots = {str(r) for r in agent._valid_workspace_roots}
    assert str(project_dir.resolve()) in roots, (
        "agent's own project_dir must be in the allow-list"
    )
    assert str(repo_a.resolve()) in roots, (
        f"task 1's project_dir must be in the allow-list; got {roots}"
    )
    assert str(repo_b.resolve()) in roots, (
        f"task 2's project_dir must be in the allow-list; got {roots}"
    )


# ---------------------------------------------------------------------------
# Test 4: _breakdown_task applies the enforcer end-to-end
# ---------------------------------------------------------------------------










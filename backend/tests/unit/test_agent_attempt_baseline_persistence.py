"""The attempt baseline is recorded once and reused by every later attempt.

What this pins
--------------
A task's *attempt baseline* is the commit its attempt started from.  Read
it fresh from "whatever HEAD is right now" on every attempt and the second
attempt of a task starts from a HEAD that already contains the first
attempt's committed work: the baseline has swallowed the previous
attempt's work, the change set the attempt is judged by disappears, and a
partial rollback would roll back the wrong thing.

So the baseline has to be *recorded once* — on the first attempt — and
*reused* afterwards.  ``backend/attempt_baselines.py`` is the file layer
that stores it; this file pins the **executor** side of that contract:

* **first attempt** — the HEAD the attempt started on is recorded on disk
  and returned;
* **second attempt** — after the sub-agent committed and HEAD moved on,
  the recorded baseline comes back unchanged;
* **sibling** — two tasks of the same lineage keep separate entries, so
  the sibling that finished first is not folded into the baseline of the
  sibling that runs later;
* **not a repository** — the answer stays ``""`` and *nothing is
  written*, because "no baseline" must always be a reachable answer;
* **state root** — the plan directory (``tasks.json``'s parent) wins over
  the project directory, and the project directory is the fallback;
* **degradation** — a store that cannot be used falls back to the HEAD,
  and the whole path starts no process (it runs on the quota-park path,
  which must spawn nothing).

The repositories here are written by hand rather than by ``git``: the
point of the file is that the answers come from reading files, so the
tests do not need a git binary to build their fixtures.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import agent  # noqa: E402
from agent import AutonomousAgent  # noqa: E402

_SHA = "1" * 40
_NEXT_SHA = "2" * 40


class _Task:
    """The only things the baseline reads off a task are ``id`` and ``project_dir``."""

    def __init__(self, project_dir, task_id: str = "3") -> None:
        self.project_dir = project_dir
        self.id = task_id


class _TaskManager:
    """A stand-in for the real ``TaskManager`` — only ``tasks_file`` is read."""

    def __init__(self, tasks_file) -> None:
        self.tasks_file = tasks_file


def _agent_for(project_dir: Path, tasks_file=None) -> AutonomousAgent:
    """An executor bound to ``project_dir``.

    The real constructor builds a ``GitManager``, and GitManager opens the
    repository with GitPython — which is process work this file is not
    about.  The baseline methods read nothing but the paths, so the
    instance is built without the constructor.
    """
    instance = object.__new__(AutonomousAgent)
    instance.project_dir = project_dir
    instance.task_manager = _TaskManager(tasks_file)
    return instance


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _repo_with_head(root: Path, sha: str) -> Path:
    """A repository whose ``HEAD`` points at a branch holding ``sha``."""
    root.mkdir(parents=True, exist_ok=True)
    _write(root / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(root / ".git" / "refs" / "heads" / "main", f"{sha}\n")
    return root


def _advance_head(root: Path, sha: str) -> None:
    """Move the branch the way a sub-agent's commit would."""
    _write(root / ".git" / "refs" / "heads" / "main", f"{sha}\n")


def _plan_dir(tmp_path: Path) -> Path:
    """The plan directory — ``tasks.json``'s parent."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


def _document(plan_dir: Path) -> dict:
    """The stored baseline document, read straight off disk."""
    path = plan_dir / ".pdt" / "attempt_baselines.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def no_spawn(monkeypatch):
    """Make any process start an immediate, loud failure."""
    started: list = []

    def _forbidden(name):
        def _call(*args, **kwargs):
            started.append((name, args, kwargs))
            raise AssertionError(
                f"{name} was called — the baseline path must stay pure Python"
            )

        return _call

    monkeypatch.setattr(subprocess, "run", _forbidden("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", _forbidden("subprocess.Popen"))
    monkeypatch.setattr(agent.subprocess, "run", _forbidden("agent.subprocess.run"))
    monkeypatch.setattr(agent.subprocess, "Popen", _forbidden("agent.subprocess.Popen"))
    return started


def test_first_attempt_records_head(tmp_path, no_spawn):
    """The first attempt's HEAD is returned *and* lands on disk."""
    repo = _repo_with_head(tmp_path / "repo", _SHA)
    plan_dir = _plan_dir(tmp_path)
    executor = _agent_for(repo, tasks_file=plan_dir / "tasks.json")

    baseline = executor._capture_attempt_baseline(_Task(repo))

    assert baseline == _SHA
    document = _document(plan_dir)
    assert document["version"] == 1
    assert document["lineages"]["3"]["3"] == _SHA
    assert no_spawn == [], f"a process was started: {no_spawn!r}"


def test_second_attempt_reuses_recorded_baseline(tmp_path):
    """After the sub-agent committed and HEAD moved, the old baseline comes back."""
    repo = _repo_with_head(tmp_path / "repo", _SHA)
    plan_dir = _plan_dir(tmp_path)
    executor = _agent_for(repo, tasks_file=plan_dir / "tasks.json")
    task = _Task(repo)

    first = executor._capture_attempt_baseline(task)
    # The sub-agent committed its work — HEAD moved on.
    _advance_head(repo, _NEXT_SHA)
    second = executor._capture_attempt_baseline(task)

    assert first == second == _SHA
    # The document still holds the FIRST baseline, not the newer HEAD.
    assert _document(plan_dir)["lineages"]["3"]["3"] == _SHA


def test_sibling_task_in_same_lineage_is_independent(tmp_path):
    """Two tasks of one lineage keep separate baselines."""
    repo = _repo_with_head(tmp_path / "repo", _SHA)
    plan_dir = _plan_dir(tmp_path)
    executor = _agent_for(repo, tasks_file=plan_dir / "tasks.json")

    first = executor._capture_attempt_baseline(_Task(repo, task_id="3"))
    _advance_head(repo, _NEXT_SHA)
    sibling = executor._capture_attempt_baseline(_Task(repo, task_id="3-2"))

    assert first == _SHA
    assert sibling == _NEXT_SHA
    document = _document(plan_dir)
    assert document["lineages"]["3"]["3"] == _SHA
    assert document["lineages"]["3"]["3-2"] == _NEXT_SHA


def test_non_repository_returns_empty(tmp_path):
    """Outside a repository the answer stays ``""`` — and nothing is written."""
    plain = tmp_path / "plain"
    plain.mkdir()
    plan_dir = _plan_dir(tmp_path)
    executor = _agent_for(plain, tasks_file=plan_dir / "tasks.json")

    assert executor._capture_attempt_baseline(_Task(plain)) == ""
    assert not (plan_dir / ".pdt" / "attempt_baselines.json").exists()


def test_state_root_prefers_tasks_file_parent(tmp_path):
    """The plan directory wins over the project directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_dir = _plan_dir(tmp_path)
    executor = _agent_for(repo, tasks_file=plan_dir / "tasks.json")

    assert executor._pdt_state_root(_Task(repo)) == plan_dir


def test_state_root_falls_back_to_project_dir(tmp_path):
    """Without a ``tasks_file`` the project directory is the state root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    executor = _agent_for(repo, tasks_file=None)

    assert executor._pdt_state_root(_Task(repo)) == repo


def test_state_root_falls_back_to_the_agent_project_dir(tmp_path):
    """A task without its own ``project_dir`` uses the executor's."""
    repo = tmp_path / "repo"
    repo.mkdir()
    executor = _agent_for(repo, tasks_file=None)

    assert executor._pdt_state_root(_Task(None)) == repo


def test_store_lives_under_the_state_root(tmp_path):
    """The store is ``<state root>/.pdt/attempt_baselines.json``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_dir = _plan_dir(tmp_path)
    executor = _agent_for(repo, tasks_file=plan_dir / "tasks.json")

    store = executor._attempt_baseline_store(_Task(repo))

    assert store.path == plan_dir / ".pdt" / "attempt_baselines.json"


def test_store_failure_falls_back_to_head(tmp_path, monkeypatch):
    """A store that cannot be used answers the HEAD and raises nothing."""
    repo = _repo_with_head(tmp_path / "repo", _SHA)
    plan_dir = _plan_dir(tmp_path)
    executor = _agent_for(repo, tasks_file=plan_dir / "tasks.json")

    def _boom(task):
        raise OSError("no store for you")

    monkeypatch.setattr(executor, "_attempt_baseline_store", _boom)

    assert executor._capture_attempt_baseline(_Task(repo)) == _SHA

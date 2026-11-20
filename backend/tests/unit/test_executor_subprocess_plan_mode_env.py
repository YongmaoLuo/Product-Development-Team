"""The executor subprocess is told which mode its plan runs in.

Why this exists
---------------
A plan may declare that it runs in ``test`` mode: the pipeline is
exercised, but the run must not touch anything real.  That declaration
lives in the plan's own state (``flags["mode"]``), and the process that
actually does the work is a **different process** -- the executor
subprocess the backend spawns.  It cannot see the backend's memory, so
the only way it learns the mode is through its environment.

Before this, the spawn injected only ``PYTHONUNBUFFERED`` and
``PDT_PLAN_ID``.  The child had no way to know whether the plan it was
sent to run was a rehearsal or the real thing, so "the plan said test"
and "the executor ran as if it were production" could both be true at
once.

What is pinned here
-------------------
* a plan in ``test`` mode spawns an executor whose environment says
  ``PDT_PLAN_MODE=test``;
* a plan that says nothing at all spawns an executor whose environment
  says ``PDT_PLAN_MODE=production`` -- the mode that must be safe to
  assume, and the one a plan written before modes existed reads as;
* a caller that passes ``extra_env`` may override the value, because
  the caller knows more about the run than the plan's flag does.

The spawn itself is intercepted at the module-level ``Popen`` seam, so
no executor is started: the test asserts on the environment the child
*would* have been given.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` has to be imported before ``routes.execution``: the server
# module wires the router at import time and ``routes.execution``
# late-binds back into it, so the other order is a circular import.
import server  # noqa: E402
from routes import execution as execution_route  # noqa: E402


PLAN_ID = "20261118-executor-plan-mode-env"


class _FakeProcess:
    """What the spawn seam hands back.

    A pid (the ledger record names one) and an iterable stdout (a
    reader of the child's output may ask for it) -- nothing else, because
    the spawn path asks for nothing else.
    """

    def __init__(self) -> None:
        self.pid = 999_000_001
        self.stdout = iter(())

    def poll(self) -> int:
        return 0

    def wait(self, timeout: Any = None) -> int:
        return 0

    def terminate(self) -> None:
        return None


@pytest.fixture
def plan_dir(tmp_path, monkeypatch) -> Path:
    """A plan directory wired into the server's plans root.

    The state database is pointed at a throwaway file first, so the
    plan's routing row -- where the mode lives -- is written to and read
    from somewhere that belongs to this test.
    """
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(tmp_path / "state.db"))
    plans_dir = tmp_path / "plans"
    pdir = plans_dir / PLAN_ID
    pdir.mkdir(parents=True)
    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    return pdir


@pytest.fixture
def spawn_env(tmp_path, monkeypatch) -> Dict[str, Any]:
    """The environment the executor subprocess would have been given."""
    recorded: Dict[str, Any] = {}

    def _fake_popen(cmd: List[str], **kwargs: Any) -> _FakeProcess:
        recorded["cmd"] = list(cmd)
        recorded["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(execution_route, "Popen", _fake_popen)
    return recorded


def _set_mode(plan_dir: Path, mode: str) -> None:
    """Give the plan a mode, the way another writer would."""
    from plan_state import PlanState

    ps = PlanState(plan_dir)
    ps.ensure_row()
    assert ps.set_mode(mode) == mode


def _spawn(plan_dir: Path, tmp_path: Path, **kwargs: Any):
    """Call the real spawn helper against the fake backend."""
    backend_dir = tmp_path / "fake_backend"
    backend_dir.mkdir(exist_ok=True)
    (backend_dir / "cli.py").write_text("# not a real executor\n", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    tasks_file = plan_dir / "tasks.json"
    tasks_file.write_text("[]", encoding="utf-8")

    return execution_route._spawn_executor_subprocess(
        plan_id=PLAN_ID,
        backend_dir=backend_dir,
        project_dir=project,
        tasks_file=tasks_file,
        tool=None,
        log_path=tmp_path / "executor.log",
        **kwargs,
    )


def test_spawn_injects_plan_mode(plan_dir, tmp_path, spawn_env):
    """A plan in test mode spawns an executor that is told it is a test."""
    _set_mode(plan_dir, "test")

    _spawn(plan_dir, tmp_path)

    env = spawn_env["kwargs"]["env"]
    assert env["PDT_PLAN_MODE"] == "test"


def test_spawn_defaults_to_production(plan_dir, tmp_path, spawn_env):
    """A plan that says nothing spawns an executor in production mode.

    Production is the mode that must be safe to assume: a plan written
    before modes existed says nothing, and running it for real is what
    every plan did before.
    """
    _spawn(plan_dir, tmp_path)

    env = spawn_env["kwargs"]["env"]
    assert env["PDT_PLAN_MODE"] == "production"


def test_extra_env_can_override(plan_dir, tmp_path, spawn_env):
    """A caller may override the mode it is handed.

    The plan's flag is the default; the caller knows more about the run
    it is starting than the flag does.  The plan here says ``test``, so
    the override has to win over a value that *is* there -- otherwise
    this test would pass without the override doing anything.
    """
    from plan_state import read_plan_mode

    _set_mode(plan_dir, "test")
    assert read_plan_mode(plan_dir) == "test"

    _spawn(plan_dir, tmp_path, extra_env={"PDT_PLAN_MODE": "production"})

    env = spawn_env["kwargs"]["env"]
    assert env["PDT_PLAN_MODE"] == "production"

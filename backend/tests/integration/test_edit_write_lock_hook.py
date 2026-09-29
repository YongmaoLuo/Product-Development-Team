"""The Edit/Write hook must take the file lock before allowing a write.

The hook is the only place that sees the *real* target of a write at the
moment it happens. Without the lock step, the executor held locks only for
the files a task declared up front, so a sub-agent that decided mid-task to
touch an undeclared file — the normal case when a plan cannot fully
anticipate a change — wrote it holding nothing.

These tests drive the generated hook script as a subprocess, with the same
stdin payload and environment the SDK gives it. That is deliberate: the
contract under test is an **exit code** and a stream, and the only honest
way to check an exit code is to make the process produce one. A test that
called the logic in-process would not have caught the ``python3`` on
``PATH`` / virtualenv split that constrains what the hook may import.

Exit codes (see ``backend/file_lock_cli.py``):
    0 allow, 2 refuse, and no other value is produced by this hook.
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

from file_lock_broker import FileLockBroker  # noqa: E402
from file_lock_protocol import socket_path  # noqa: E402


@pytest.fixture
def workspace(tmp_path):
    """A project dir, a plans dir, and the hook script, ready to invoke."""
    from coding_tool import _ensure_guard_hook_path

    project = tmp_path / "project"
    plans = tmp_path / "plans"
    project.mkdir()
    plans.mkdir()
    return {
        "project": project,
        "plans": plans,
        "script": _ensure_guard_hook_path(),
        "cli": _BACKEND_DIR / "file_lock_cli.py",
    }


@pytest.fixture
def broker(workspace):
    from file_lock_protocol import locks_dir_for_plan

    b = FileLockBroker(workspace["project"], locks_dir_for_plan(workspace["plans"]))
    b.start()
    try:
        yield b
    finally:
        b.stop()


def _run_hook(workspace, target, *, broker_on, task_id="t1", extra_env=None):
    """Run the guard hook for one Edit, returning ``(exit_code, stderr)``."""
    env = {
        "PATH": "/usr/bin:/bin",
        "PDT_PROJECT_DIR": str(workspace["project"]),
        "PDT_PLANS_DIR": str(workspace["plans"]),
        "PDT_FORMAL_REPO_PATH": str(workspace["project"]),
        "PDT_LOCK_CLI": str(workspace["cli"]),
    }
    if broker_on:
        env["PDT_LOCK_BROKER"] = str(socket_path(workspace["project"]))
        env["PDT_LOCK_TASK_ID"] = task_id
        env["PDT_LOCK_TIMEOUT"] = "3"
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        [sys.executable, str(workspace["script"])],
        input=json.dumps({"tool_input": {"file_path": str(target)}}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return proc.returncode, proc.stderr


# ---------------------------------------------------------------------------
# containment (pre-existing contract — must not regress)
# ---------------------------------------------------------------------------
def test_edit_outside_the_project_is_still_refused(workspace, tmp_path):
    outside = tmp_path / "elsewhere" / "notes.md"
    code, stderr = _run_hook(workspace, outside, broker_on=True)

    assert code == 2
    assert "outside the project dir" in stderr


def test_edit_inside_the_project_is_allowed(workspace):
    code, _ = _run_hook(workspace, workspace["project"] / "a.py", broker_on=False)
    assert code == 0


# ---------------------------------------------------------------------------
# the lock step
# ---------------------------------------------------------------------------
def test_edit_takes_the_lock_and_the_executor_can_release_it(workspace, broker):
    """The hook's acquire must land in the table the executor releases.

    Keyed by task id, which is why the hook is given ``PDT_LOCK_TASK_ID``:
    an acquire under any other name is a lock the executor would never
    free, i.e. a leak that blocks the next task on that file.
    """
    target = workspace["project"] / "backend" / "app.py"
    code, stderr = _run_hook(workspace, target, broker_on=True, task_id="7")

    assert code == 0, stderr
    assert broker.held("7") == ["backend/app.py"]
    assert broker.release("7") == 1


def test_a_contended_file_refuses_the_edit(workspace, broker):
    """The behaviour the whole feature exists for.

    Another task holds the file, so this edit must be *refused* — with a
    message the sub-agent can act on — rather than allowed to race.
    """
    target = workspace["project"] / "backend" / "app.py"
    assert broker.acquire("other-task", "backend/app.py", 5) == "acquired"

    code, stderr = _run_hook(workspace, target, broker_on=True, task_id="7")

    assert code == 2, f"contended edit was allowed; stderr={stderr!r}"
    assert "another task is editing this file" in stderr
    assert "backend/app.py" in stderr


def test_the_same_task_editing_twice_is_not_refused(workspace, broker):
    """A sub-agent routinely edits one file several times per task."""
    target = workspace["project"] / "backend" / "app.py"
    first, _ = _run_hook(workspace, target, broker_on=True, task_id="7")
    second, stderr = _run_hook(workspace, target, broker_on=True, task_id="7")

    assert (first, second) == (0, 0), stderr


# ---------------------------------------------------------------------------
# degradation
# ---------------------------------------------------------------------------
def test_no_broker_configured_is_a_silent_pass(workspace):
    """Standalone ``coding_tool`` use must be unchanged.

    The socket is what marks a workspace as brokered, so its absence is
    not an error — and warning here would fire on every edit of every
    test and standalone run until readers learnt to ignore it.
    """
    code, stderr = _run_hook(
        workspace, workspace["project"] / "a.py", broker_on=False
    )
    assert code == 0
    assert stderr == ""


def test_a_dead_broker_allows_the_edit_with_a_warning(workspace):
    """The executor having died is not the sub-agent's fault.

    Refusing here would brick every sub-agent in flight, so the hook
    proceeds — loudly, because the lock guarantee is gone for this edit.
    """
    sock = socket_path(workspace["project"])
    sock.parent.mkdir(parents=True, exist_ok=True)
    sock.write_text("", encoding="utf-8")  # exists, but nothing is listening
    try:
        code, stderr = _run_hook(
            workspace, workspace["project"] / "a.py", broker_on=True
        )
    finally:
        sock.unlink(missing_ok=True)

    assert code == 0, stderr
    assert "broker unavailable" in stderr


def test_plan_state_files_are_not_brokered(workspace, broker):
    """``plans/`` is shared state with its own writers, not project source.

    Locking it here would serialise the plan machinery against sub-agents
    for no gain, and would block a plan write that is not a code edit.
    """
    target = workspace["plans"] / "tasks.json"
    assert broker.acquire("other-task", str(target), 5) in ("acquired", "already")

    code, stderr = _run_hook(workspace, target, broker_on=True, task_id="7")

    assert code == 0, stderr
    assert "another task" not in stderr


def test_the_cli_path_the_tool_publishes_really_exists():
    """``coding_tool`` publishes ``<backend>/file_lock_cli.py`` to the hook.

    If that path drifts, every hook exits 4 and the lock silently becomes
    fail-open — no error anywhere, just no locking. The hook's own tests
    pass a path they construct themselves, so without this the published
    path could rot unnoticed.
    """
    import coding_tool

    published = Path(coding_tool.__file__).resolve().parent / "file_lock_cli.py"
    assert published.exists(), f"hook would be handed a missing CLI: {published}"

    # And the workspace fixture builds it from the same directory.
    assert published.parent == _BACKEND_DIR

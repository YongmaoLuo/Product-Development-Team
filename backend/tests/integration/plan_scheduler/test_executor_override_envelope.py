"""Integration tests: the executor override port and the launch envelope.

Background
----------
:func:`routes.execution._spawn_executor_subprocess` used to build its
command line from a literal list inside the function.  Nothing could
replace the *body* that gets executed: a test that wanted a stand-in
for the executor had no port to reach it through, and the only way to
observe the spawn was to let the real ``cli.py`` run.

The scheduler's end-to-end tests need the opposite — a deterministic
stand-in for the executor, so the test controls what the spawned
process does without touching production code.  The port that makes
that possible is the ``PDT_EXECUTOR_CMD`` environment variable:

* **Unset** — the command line is built exactly as it always was, byte
  for byte.  The default path may not move, because every existing
  run depends on it.
* **Set** — the value is split with :func:`shlex.split` and *that* is
  the command line.  Nothing is appended: no ``--recover``, no ``-w``,
  no ``--tasks-file``, no ``--tool``, no extra args.  The value is the
  whole command.
* **Set to something that cannot run** — the spawn fails loudly.  The
  ``OSError`` reaches the caller, which records the run as ``failed`` /
  ``process_died_unexpectedly``.  It never falls back to the real
  executor: a test that asked for a stand-in and silently got the
  production body would be worse than no test at all.

The envelope around the command line is the other half of the
contract.  Whatever runs, it runs with the same working directory, the
same session, the same log file, the same ``PYTHONUNBUFFERED`` and the
same ``PDT_PLAN_ID``.  The override replaces *what* runs; it never
changes *how* it is launched.

What is observed, and how
--------------------------
The contract under test is "the command line ``Popen`` receives", so
the honest way to observe it is to replace ``Popen`` and read what it
was handed.  The failure case deliberately does the opposite: it lets
the real ``Popen`` run against a path that does not exist, because the
promise being pinned there is a real ``OSError`` and not a mock's
idea of one.

Every test here sets (or unsets) the variable *inside* the test — the
module under test was imported long before — and the spawn still sees
the value.  That is the proof that the variable is read at spawn time
and not frozen at import time.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

# ``server`` is imported BEFORE ``routes.execution`` on purpose: the route
# module late-binds into the application module, and ``server`` seeds
# ``sys.modules['server']`` before importing it.
import server
from routes import execution as routes_execution


client = TestClient(server.app)


#: The environment variable that replaces the executor command line.
EXECUTOR_CMD_ENV = "PDT_EXECUTOR_CMD"

#: The plan id every spawn in this file uses.  It only has to be a legal
#: plan id — the tests never read the plan's directory.
PLAN_ID = "p-exec-override"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeProcess:
    """A process handle for a process that was never started.

    ``Popen`` is replaced by a recorder in the tests that inspect the
    command line; the recorder still has to hand back something that
    looks like a process, because the caller stores its pid.
    """

    pid = 4242
    returncode = 0

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def terminate(self):
        return None


class _RecordedSpawn:
    """One ``Popen`` call: the command line it was given and its keywords."""

    def __init__(self, cmd: List[str], kwargs: Dict[str, Any]) -> None:
        self.cmd = list(cmd)
        self.kwargs = kwargs


@pytest.fixture()
def recorded_spawns(monkeypatch) -> List[_RecordedSpawn]:
    """Replace ``Popen`` with a recorder and collect what it was handed.

    ``Popen`` is looked up on the ``subprocess`` module at call time, so
    replacing it there is what makes the recorder effective for the
    production helper.
    """
    spawns: List[_RecordedSpawn] = []

    def _fake_popen(cmd, **kwargs):
        spawns.append(_RecordedSpawn(cmd, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(routes_execution.subprocess, "Popen", _fake_popen)
    return spawns


def _spawn(
    tmp_path: Path,
    *,
    tool: str = "claude",
    extra_args: Optional[List[str]] = None,
) -> None:
    """Spawn the executor through the production helper.

    The tree is built under ``tmp_path`` so nothing here touches the
    developer's checkout: ``backend/cli.py`` only has to *exist as a
    path* for the command line to be built — it is never run.
    """
    backend_dir = tmp_path / "backend"
    (backend_dir / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "worktree"
    project_dir.mkdir(exist_ok=True)
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")

    routes_execution._spawn_executor_subprocess(
        plan_id=PLAN_ID,
        backend_dir=backend_dir,
        project_dir=project_dir,
        tasks_file=tasks_file,
        tool=tool,
        extra_args=extra_args,
        log_path=tmp_path / "logs" / "executor.log",
    )


def _expected_default_argv(tmp_path: Path) -> List[str]:
    """The command line the default path builds, written out by hand.

    It is written out rather than computed from the production code so
    that the test fails when the production code drifts — a template
    derived from the implementation proves nothing.
    """
    return [
        sys.executable,
        "-u",
        str(tmp_path / "backend" / "cli.py"),
        "--recover",
        "-w",
        str(tmp_path / "worktree"),
        "--tasks-file",
        str(tmp_path / "tasks.json"),
        "--tool",
        "claude",
        "--verification-tasks",
        "v.json",
    ]


# ---------------------------------------------------------------------------
# 1. Unset — the command line does not move
# ---------------------------------------------------------------------------


def test_unset_argv_byte_identical(monkeypatch, recorded_spawns, tmp_path):
    """With the variable unset, the command line is exactly what it was.

    Every existing run depends on this line, so it is pinned element by
    element — the interpreter, the ``-u``, the ``cli.py`` path, and the
    four arguments the executor needs (``--recover``, ``-w``,
    ``--tasks-file``) plus the ``--tool`` and the extra args the caller
    supplied.
    """
    monkeypatch.delenv(EXECUTOR_CMD_ENV, raising=False)

    _spawn(tmp_path, extra_args=["--verification-tasks", "v.json"])

    assert len(recorded_spawns) == 1, "the spawn did not reach Popen"
    assert recorded_spawns[0].cmd == _expected_default_argv(tmp_path), (
        f"the default command line moved: {recorded_spawns[0].cmd!r}"
    )

    # An empty value is the same statement of intent as no value at all:
    # "there is no override".  The two paths must produce the very same
    # command line, byte for byte.
    monkeypatch.setenv(EXECUTOR_CMD_ENV, "")
    _spawn(tmp_path, extra_args=["--verification-tasks", "v.json"])

    assert len(recorded_spawns) == 2, "the second spawn did not reach Popen"
    assert recorded_spawns[1].cmd == recorded_spawns[0].cmd, (
        f"an empty override changed the command line: "
        f"{recorded_spawns[1].cmd!r} != {recorded_spawns[0].cmd!r}"
    )


# ---------------------------------------------------------------------------
# 2. Set — the value is the whole command
# ---------------------------------------------------------------------------


def test_set_argv_verbatim(monkeypatch, recorded_spawns, tmp_path):
    """With the variable set, the command line is the split value.

    Nothing is appended to it.  The value is the *whole* command: a
    stand-in for the executor is given the arguments it needs in the
    value itself, and the production arguments are not appended behind
    its back — an executor that does not understand ``--recover`` must
    not receive one.
    """
    value = "/path/.venv/bin/python3 -u /abs/deterministic_executor.py"
    monkeypatch.setenv(EXECUTOR_CMD_ENV, value)

    _spawn(tmp_path, extra_args=["--verification-tasks", "v.json"])

    assert len(recorded_spawns) == 1, "the spawn did not reach Popen"
    cmd = recorded_spawns[0].cmd

    assert cmd == shlex.split(value), (
        f"the command line is not the split value: {cmd!r}"
    )
    assert cmd == [
        "/path/.venv/bin/python3",
        "-u",
        "/abs/deterministic_executor.py",
    ], f"the split value is not what was asked for: {cmd!r}"

    for forbidden in (
        "--recover",
        "-w",
        "--tasks-file",
        "--tool",
        "--verification-tasks",
        "claude",
    ):
        assert forbidden not in cmd, (
            f"{forbidden!r} was appended to the override: {cmd!r}"
        )


# ---------------------------------------------------------------------------
# 3. The envelope does not move
# ---------------------------------------------------------------------------


def test_envelope_identical_both_paths(monkeypatch, recorded_spawns, tmp_path):
    """Both paths launch the same way; only the command line differs.

    The override replaces *what* runs.  How it is launched is the
    contract the rest of the system depends on: the working directory
    (``cli.py`` resolves its own imports from it), the new session (the
    stop path signals the whole group), the log file (the status route
    reads it), the unbuffered stdout (the log is tailed live) and
    ``PDT_PLAN_ID`` (the executor stamps its rows with it).
    """
    monkeypatch.delenv(EXECUTOR_CMD_ENV, raising=False)
    _spawn(tmp_path)
    monkeypatch.setenv(EXECUTOR_CMD_ENV, "/opt/deterministic_executor.py")
    _spawn(tmp_path)

    assert len(recorded_spawns) == 2, "both spawns did not reach Popen"
    unset_spawn, set_spawn = recorded_spawns
    unset_kwargs, set_kwargs = unset_spawn.kwargs, set_spawn.kwargs

    # The command line is the only thing that differs.
    assert unset_spawn.cmd != set_spawn.cmd, (
        "the override did not change the command line at all"
    )

    expected_log = str(tmp_path / "logs" / "executor.log")
    expected_venv_bin = str(tmp_path / "backend" / ".venv" / "bin")

    for kwargs in (unset_kwargs, set_kwargs):
        assert kwargs["cwd"] == str(tmp_path / "backend"), (
            f"the working directory moved: {kwargs['cwd']!r}"
        )
        assert kwargs["start_new_session"] is True, (
            "the executor must get its own session"
        )
        assert kwargs["stdout"].name == expected_log, (
            f"stdout must land in the log file; got {kwargs['stdout'].name!r}"
        )
        assert kwargs["stderr"] == subprocess.STDOUT, (
            "stderr must go to the same log file"
        )
        assert kwargs["bufsize"] == 0, "the log must not be block-buffered"

        env = kwargs["env"]
        assert env["PYTHONUNBUFFERED"] == "1", (
            f"the executor must run unbuffered; got {env['PYTHONUNBUFFERED']!r}"
        )
        assert env["PDT_PLAN_ID"] == PLAN_ID, (
            f"the executor must be told which plan it runs; "
            f"got {env['PDT_PLAN_ID']!r}"
        )
        assert env["PATH"].split(os.pathsep)[0] == expected_venv_bin, (
            f"the venv must lead the PATH; got {env['PATH']!r}"
        )

    # The two environments agree on everything the envelope names.
    # (``PDT_EXECUTOR_CMD`` itself differs — it is inherited from the
    # process environment, and it is the override, not part of the
    # envelope.)
    assert unset_kwargs["env"]["PATH"] == set_kwargs["env"]["PATH"], (
        "the two paths disagree about the PATH"
    )
    assert unset_kwargs["env"]["PYTHONUNBUFFERED"] == (
        set_kwargs["env"]["PYTHONUNBUFFERED"]
    )
    assert unset_kwargs["env"]["PDT_PLAN_ID"] == set_kwargs["env"]["PDT_PLAN_ID"]


# ---------------------------------------------------------------------------
# 4. A command that cannot run fails loudly
# ---------------------------------------------------------------------------


def test_bad_value_fails_loudly(monkeypatch, tmp_path):
    """An override that cannot run raises — it never falls back.

    The promise is not "the spawn fails"; it is "the spawn fails
    *there*".  A fallback to the real executor would mean a test that
    asked for a stand-in quietly ran the production body instead — the
    worst possible outcome for a test, and the reason the error must
    name the interpreter that was asked for.
    """
    missing = tmp_path / "no-such-interpreter"
    monkeypatch.setenv(
        EXECUTOR_CMD_ENV,
        f"{missing} -u /abs/deterministic_executor.py",
    )

    # No recorder here: the real ``Popen`` runs, so the ``OSError`` is
    # a real one and not a mock's idea of one.
    with pytest.raises(OSError) as excinfo:
        _spawn(tmp_path)

    assert excinfo.value.filename == str(missing), (
        f"the error must name the interpreter that was asked for; "
        f"got filename={getattr(excinfo.value, 'filename', None)!r}"
    )
    assert str(tmp_path / "backend" / "cli.py") not in str(excinfo.value), (
        "the failure must not be about the real executor — "
        f"that would mean it ran instead: {excinfo.value!r}"
    )


def test_malformed_value_fails_loudly(monkeypatch, recorded_spawns, tmp_path):
    """A value that is not a command line fails the way a missing one does.

    The value is split with ``shlex.split``, and a value with an
    unclosed quote makes that raise.  It has to reach the caller as the
    same ``OSError`` a missing interpreter produces: all three spawn
    call sites catch ``OSError`` and record the run as failed, and a
    plan whose executor never came up must not be left half-started.
    """
    monkeypatch.setenv(EXECUTOR_CMD_ENV, '"unclosed quote')

    with pytest.raises(OSError) as excinfo:
        _spawn(tmp_path)

    assert EXECUTOR_CMD_ENV in str(excinfo.value), (
        f"the error must name the variable that is wrong; "
        f"got {excinfo.value!r}"
    )
    assert recorded_spawns == [], (
        f"nothing may be started; Popen was called {len(recorded_spawns)} time(s)"
    )


# ---------------------------------------------------------------------------
# 5. The failed spawn is recorded, not swallowed
# ---------------------------------------------------------------------------


def _setup_plan_dir(plan_id: str) -> Path:
    """A minimal plan directory — tasks and state, nothing else."""
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": "ready",
                "completed_phases": [],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {"arch_enabled": False, "test_enabled": False},
            }
        ),
        encoding="utf-8",
    )
    return plan_dir


def test_bad_value_is_recorded_as_failed(monkeypatch, tmp_path, state_db_reader):
    """A start whose override cannot run is recorded as a failed run.

    The ``OSError`` is not swallowed on the way out: the start route
    catches it, records the run as ``failed`` with
    ``stop_reason='process_died_unexpectedly'`` and answers 200 — the
    run stays pollable, and it says what happened to it.  The failure
    is also the whole truth about the run: no pid is left behind, so
    nothing reads the dead run as a live one.
    """
    plan_id = "p-exec-override-failed"
    plan_dir = _setup_plan_dir(plan_id)
    workdir = plan_dir.parent / "worktree"
    workdir.mkdir(exist_ok=True)

    missing = tmp_path / "no-such-interpreter"
    monkeypatch.setenv(EXECUTOR_CMD_ENV, f"{missing} -u /abs/executor.py")

    server._execution_state.clear()
    server._execution_locks.clear()
    try:
        response = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(workdir)},
        )
    finally:
        server._execution_state.clear()
        server._execution_locks.clear()

    assert response.status_code == 200, (
        f"the start must still answer; got HTTP {response.status_code}; "
        f"body={response.text!r}"
    )
    body = response.json()
    assert body["status"] == "failed", (
        f"the run must be recorded as failed; got {body!r}"
    )
    assert body["pid"] is None, (
        f"nothing was spawned, so there is no pid; got {body!r}"
    )

    row = state_db_reader.execution(plan_id)
    assert row is not None, (
        f"the failed run must leave a plan_execution row for {plan_id}"
    )
    assert row["exec_status"] == "failed", (
        f"exec_status must record the failure; got {row['exec_status']!r}"
    )
    assert row["stop_reason"] == "process_died_unexpectedly", (
        f"stop_reason must say why; got {row['stop_reason']!r}"
    )
    assert row["exec_pid"] is None, (
        f"no process was started, so no pid may be recorded; "
        f"got {row['exec_pid']!r}"
    )

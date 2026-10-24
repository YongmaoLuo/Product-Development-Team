"""The repair dispatch must refuse an invalid ``project_dir`` before it acts.

Background
----------
``_run_repair_execution_async`` (backend/verification_loop.py) used to
``force_set_phase("executing")`` and spawn the executor subprocess
*before* anyone asked whether the working directory was real. A
``project_dir`` pointing at a deleted worktree therefore produced a
plan that had already been stamped ``executing`` while the failure only
surfaced inside the subprocess — a ghost state the operator sees as
"running" and the watchdog later reaps as "dead".

``project_dir_guard.validate_project_dir`` is the single validator (see
``backend/project_dir_guard.py``): the directory must already exist and
must be a directory. The repair dispatch calls it *before* touching the
phase or spawning anything, records the rejection, and reports the
failure to the caller.

These tests pin five contract points:

  1. a missing ``project_dir`` stops the dispatch — neither
     ``force_set_phase("executing")`` nor ``_spawn_executor_subprocess``
     is reached;
  2. the caller gets a machine-readable failure
     (``{"status": "failed", "reason": "project_dir_invalid"}``) and the
     execution state records the same reason;
  3. the offending path is written to the plan's log, verbatim, so the
     operator can see *which* path was refused;
  4. ``on_complete(-1)`` still fires, so the verification loop does not
     wait for a round that will never come;
  5. a directory that exists is untouched — the guard refuses, it does
     not rewrite the original flow.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import server


PLAN_ID = "p-guard"


@pytest.fixture
def plans_dir(tmp_path, monkeypatch):
    """Point ``server.PLANS_DIR`` at a throwaway tree for this test."""
    root = tmp_path / "plans"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "PLANS_DIR", root)
    return root


@pytest.fixture
def plan_dir(plans_dir):
    """The plan's own directory, with an (empty) ``tasks.json``."""
    directory = plans_dir / PLAN_ID
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tasks.json").write_text("[]", encoding="utf-8")
    return directory


@pytest.fixture
def dispatch_spy(monkeypatch):
    """Fail loudly if the dispatch reaches the phase write or the spawn.

    Both are recorded as mocks rather than raising, so the assertions can
    read the call counts instead of relying on an exception that would
    mask the real return value.
    """
    spawn = MagicMock(name="_spawn_executor_subprocess")
    monkeypatch.setattr(server, "_spawn_executor_subprocess", spawn)

    force_set_phase = MagicMock(name="force_set_phase")
    monkeypatch.setattr(
        server, "PlanState",
        lambda *a, **kw: MagicMock(force_set_phase=force_set_phase),
    )
    return spawn, force_set_phase


@pytest.fixture(autouse=True)
def _clean_execution_state():
    """Keep the process-global execution state from leaking across tests."""
    with server._execution_state_lock:
        server._execution_state.clear()
    yield
    with server._execution_state_lock:
        server._execution_state.clear()


def test_repair_dispatch_rejects_missing_project_dir_before_spawn(
    plan_dir, dispatch_spy, tmp_path,
):
    """A missing directory stops the dispatch before anything observable.

    Neither the phase write nor the subprocess spawn may happen: the
    whole point is that the plan must not be left in the ``executing``
    phase for a run that can never start.
    """
    spawn, force_set_phase = dispatch_spy
    missing_dir = tmp_path / "deleted-worktree"

    callback = MagicMock(name="on_complete")
    out = server._run_repair_execution_async(
        PLAN_ID, missing_dir, on_complete=callback,
    )

    assert out == {"status": "failed", "reason": "project_dir_invalid"}
    assert spawn.call_count == 0, "the executor must not be spawned"
    assert force_set_phase.call_count == 0, (
        "the plan must not be stamped 'executing' for a run that cannot start"
    )

    state = server._execution_state[PLAN_ID]
    assert state["status"] == "failed"
    assert state["stop_reason"] == "project_dir_invalid"
    assert state["project_dir"] == str(missing_dir)
    assert callback.call_args.args[0] == -1


def test_repair_dispatch_writes_log_line_with_path(
    plan_dir, dispatch_spy, tmp_path,
):
    """The refused path is written to the plan's log, verbatim.

    The operator needs to know *which* directory was refused — the plan
    id alone does not say. The line therefore carries the path as given.
    """
    missing_dir = tmp_path / "deleted-worktree"

    server._run_repair_execution_async(PLAN_ID, missing_dir, on_complete=None)

    log_file = plan_dir / "logs" / "execution_start.log"
    assert log_file.exists(), f"{log_file} was not written"
    content = log_file.read_text(encoding="utf-8")
    assert str(missing_dir) in content, (
        f"the log must name the refused path; got {content!r}"
    )


def test_missing_project_dir_does_not_spawn(plan_dir, dispatch_spy, tmp_path):
    """The executor subprocess is never reached for a missing directory.

    A separate assertion from the phase one: the spawn is the thing that
    would have failed inside the child process, where the operator
    cannot see it.
    """
    spawn, _force_set_phase = dispatch_spy

    out = server._run_repair_execution_async(
        PLAN_ID, tmp_path / "deleted-worktree", on_complete=None,
    )

    assert spawn.call_count == 0
    assert out["status"] == "failed"


def test_missing_project_dir_calls_on_complete_minus_one(
    plan_dir, dispatch_spy, tmp_path,
):
    """The callback still fires, with -1, so the chain does not hang.

    ``on_complete`` is how the auto-verification loop learns that the
    dispatch ended. A refusal that returned without calling it would
    leave the loop waiting for a round that will never come.
    """
    _spawn, _force_set_phase = dispatch_spy

    callback = MagicMock(name="on_complete")
    server._run_repair_execution_async(
        PLAN_ID, tmp_path / "deleted-worktree", on_complete=callback,
    )

    assert callback.called, "on_complete must fire on a refused dispatch"
    assert callback.call_args.args[0] == -1


def test_existing_project_dir_still_spawns(plan_dir, dispatch_spy, tmp_path):
    """A valid directory takes the original path, unchanged.

    The guard is a refusal, not a rewrite: a directory that exists must
    still be spawned into, with the phase stamped as before.
    """
    spawn, force_set_phase = dispatch_spy
    workdir = tmp_path / "worktree"
    workdir.mkdir()

    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = 0
    proc.wait = MagicMock(return_value=0)
    spawn.side_effect = lambda **kwargs: (proc, tmp_path / "log")

    out = server._run_repair_execution_async(
        PLAN_ID, workdir, on_complete=None,
    )

    assert spawn.call_count == 1, "a valid directory must still be spawned"
    assert force_set_phase.call_args.args[0] == "executing"
    assert out["status"] == "started"
    assert out["pid"] == 4242


def test_rejection_log_appends_and_keeps_earlier_lines(
    plan_dir, dispatch_spy, tmp_path,
):
    """The rejection is APPENDED to the log, not written over it.

    ``execution_start.log`` is the plan's running log of dispatch
    attempts. A second refusal must not erase what the first one
    recorded — an operator reading the file has to see the whole
    sequence, not just the last line.
    """
    missing_dir = tmp_path / "deleted-worktree"

    logs_dir = plan_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "execution_start.log"
    log_file.write_text("earlier dispatch line\n", encoding="utf-8")

    server._run_repair_execution_async(PLAN_ID, missing_dir, on_complete=None)

    content = log_file.read_text(encoding="utf-8")
    assert "earlier dispatch line" in content, (
        f"the earlier log content was lost; got {content!r}"
    )
    assert str(missing_dir) in content, (
        f"the refused path was not recorded; got {content!r}"
    )


def test_rejection_updates_only_the_four_documented_state_keys(
    plan_dir, dispatch_spy, tmp_path,
):
    """An existing execution-state entry keeps everything but the four keys.

    The dispatch refuses, so it must not fabricate a running record —
    but it also must not wipe what the record already says. The keys it
    owns are exactly ``status`` / ``stop_reason`` / ``project_dir`` /
    ``ended_at``; every other key belongs to whoever put it there (the
    watchdog, the previous round) and must survive untouched.
    """
    with server._execution_state_lock:
        server._execution_state[PLAN_ID] = {
            "status": "running",
            "pid": 4242,
            "logs": ["earlier.log"],
            "started_at": "2026-10-23T00:00:00",
            "ended_at": None,
            "stop_reason": None,
            "sync_targets": None,
        }

    missing_dir = tmp_path / "deleted-worktree"
    server._run_repair_execution_async(PLAN_ID, missing_dir, on_complete=None)

    state = server._execution_state[PLAN_ID]
    assert state["status"] == "failed"
    assert state["stop_reason"] == "project_dir_invalid"
    assert state["project_dir"] == str(missing_dir)
    assert state["ended_at"] is not None
    assert state["pid"] == 4242, "the refusal must not fabricate a pid"
    assert state["logs"] == ["earlier.log"], "unrelated keys must survive"
    assert state["started_at"] == "2026-10-23T00:00:00", (
        "the refusal must not rewrite when the round started"
    )

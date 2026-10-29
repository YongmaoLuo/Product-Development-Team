"""The scheduler's fixtures keep one case's world out of the next one's.

Background
----------
A scheduler case drives a worker: a thread that ticks, reads the queue
and writes to the state database.  The worker resolves the paths it
needs — the database, the plans tree — **on every call**, so a worker
that outlives its case does not merely leak: it resolves them against
the *next* case and writes there.

Four promises make that impossible, and this module pins each of them:

1. **One case, one database.**  The database is a real file under the
   case's own ``tmp_path``, and ``PDT_STATE_DB_PATH`` points at it.
2. **One workspace, one real path.**  The workspace is reachable under
   three names — the directory, a symlink to it, and a symlink to that
   symlink — and all three resolve to one path.
3. **A minimal plan is legal.**  The plan directory carries the three
   things the scheduler reads: ``prd.json``, ``tasks.json`` and the
   project directory.
4. **Nothing outlives the case.**  After the teardown there is no engine
   thread, no child process and no listening socket left behind.

The order of the teardown is part of the contract, not an
implementation detail: the engine is stopped, then joined, then the
subprocess tree is killed, and only then is the scratch removed — a
worker that was still running would recreate what had just been
deleted.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# One case, one database
# ---------------------------------------------------------------------------


def test_db_path_is_per_test_tmp(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    db_factory,
) -> None:
    """Each case gets its own state database, and the environment says so.

    The database is a *file* under the case's own root, and
    ``PDT_STATE_DB_PATH`` points at it.  That variable is the only thing
    that decides which database a component writes to, because the path
    is resolved on every call rather than captured once — so a case
    that created a database without pointing the variable at it would
    have two databases and would be reading the wrong one.

    The second half of the test is the same property seen from the other
    side: another case is another root, and another root is another
    database.  pytest gives every case its own root, so no two cases can
    share a database.
    """
    mine = db_factory()

    # This case's own file, under this case's own root.
    assert mine.path == tmp_path / "state.db"
    assert mine.path.is_file()

    # The environment points at it — every reader resolves the database
    # through this variable, on every call.
    assert os.environ["PDT_STATE_DB_PATH"] == str(mine.path)

    # A real database with the scheduler's own tables (never ``:memory:``:
    # two connections cannot race over an in-memory database).
    tables = mine.table_names()
    assert {"scheduler_queue", "scheduler_slot", "scheduler_command"} <= tables

    # Another case is another root, and another root is another database.
    other_root = tmp_path_factory.mktemp("other-case")
    theirs = db_factory(root=other_root, set_env=False)
    assert theirs.path != mine.path
    assert theirs.path.is_relative_to(other_root)
    assert theirs.path.is_file()


# ---------------------------------------------------------------------------
# One workspace, one real path
# ---------------------------------------------------------------------------


def test_workspace_symlink_same_realpath(workspace_factory) -> None:
    """The workspace's three names resolve to one directory.

    The scheduler has to know whether two plans are working in "the
    same" workspace.  Spelling it three ways is how that question gets
    two answers: a plan that works in ``project/`` and a plan that works
    in ``project-link/`` are in the same directory, and only the real
    path says so.

    The alias goes through a *chain* of two links on purpose: a
    resolver that follows one link and stops would answer differently
    for the two names.
    """
    workspace = workspace_factory()

    # Three names, and three different strings.
    assert workspace.project != workspace.link
    assert workspace.link != workspace.alias
    assert workspace.project != workspace.alias

    # The real directory, and the directory reached through a link.
    assert workspace.project.is_dir()
    assert workspace.link.is_dir()
    assert workspace.link.is_symlink()
    assert workspace.alias.is_symlink()

    # One directory.
    real = workspace.project.resolve()
    assert workspace.link.resolve() == real
    assert workspace.alias.resolve() == real
    assert workspace.realpath == real


# ---------------------------------------------------------------------------
# Nothing outlives the case
# ---------------------------------------------------------------------------


def test_teardown_leaves_no_threads(engine_factory, case_registry) -> None:
    """Stopping and joining the engine leaves no worker thread behind.

    A worker thread that outlives its case resolves the database and the
    plans tree against the *next* case and writes there — which is how
    a case that passes on its own fails three cases later.  So the
    engine is stopped *and* joined, and then the thread list is checked:
    nothing with the engine's thread prefix is still running.

    The reclaim's own order is pinned here too, because the order is the
    contract: stop the engine, join the threads, kill the subprocess
    trees, and only then remove the scratch — a worker that was still
    running would recreate what had just been deleted.
    """
    engine = engine_factory()

    assert engine.thread.is_alive()
    assert engine.thread.name.startswith(engine.THREAD_PREFIX)

    # The worker really ran: it ticked at least once against this
    # case's database.
    deadline = time.monotonic() + 10.0
    while engine.ticks < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert engine.ticks >= 1, "the engine never ticked"
    assert not engine.errors, f"the engine's tick raised: {engine.errors!r}"

    # Stop, then join — the order the cleanup contract states.
    engine.stop()
    engine.join(timeout=5.0)

    assert not engine.thread.is_alive()

    leaked = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(engine.THREAD_PREFIX) and thread.is_alive()
    ]
    assert leaked == [], f"engine threads outlived their case: {leaked!r}"

    assert case_registry.reclaim() == [
        "stop_engine",
        "join_threads",
        "kill_subprocesses",
        "remove_tmp",
    ]


# ---------------------------------------------------------------------------
# A minimal plan is legal
# ---------------------------------------------------------------------------


def test_plan_factory_writes_minimal_plan(tmp_path: Path, plan_factory) -> None:
    """The plan directory carries the PRD, the task list and the project.

    Three things, and the scheduler reads all three: the PRD the plan
    was built from, the tasks it has to run, and the directory it runs
    in.  A case that had to hand-write a plan to get one queued would
    stop testing the scheduler and start testing its own JSON.
    """
    plan = plan_factory(plan_id="p1", priority=0, project_dir=tmp_path / "ws")

    assert plan.plan_id == "p1"
    assert plan.priority == 0

    # The three things a legal plan directory has.
    assert (plan.plan_dir / "prd.json").is_file()
    assert (plan.plan_dir / "tasks.json").is_file()
    assert plan.project_dir.is_dir()

    # And they parse as what they claim to be.
    prd = json.loads((plan.plan_dir / "prd.json").read_text(encoding="utf-8"))
    tasks = json.loads((plan.plan_dir / "tasks.json").read_text(encoding="utf-8"))

    assert prd["title"]
    assert prd["decision_points"] == []
    assert len(tasks["tasks"]) == 1
    assert tasks["tasks"][0]["project_dir"] == str(plan.project_dir)

    # The plan lives in this case's own tree, not in a shared one.
    assert plan.plan_dir.is_relative_to(tmp_path)

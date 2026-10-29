"""The plan facts, derived on every tick — never copied.

Background
----------
The scheduler keeps no copy of what a plan is doing.  What a plan is
doing already lives in the state database — ``plan_routing`` (the
phase), ``plan_execution`` (the execution subprocess and the working
directory), ``plan_verification`` (the verification round) and
``plan_decisions`` (the questions a human still has to answer) — and the
scheduler *derives* what it needs from them, every tick, from the
database.

This module is where that derivation happens, and it is the landing
point of the "the scheduler does not copy plan facts" invariant: the
reader holds no state, it answers from what is in the database right
now, and two readers that ask the same question at the same moment get
the same answer.

The five questions
------------------
``queued_plans()``
    Which plans are waiting to run, in the queue's own order.
``active_plans()``
    Which plans are holding compute — the execution side and the
    verification side, one row per plan.
``has_pending_decision(plan_id)``
    Is a human being asked about this plan?
``workspace_key(plan_id)``
    Which directory does this plan work in?  One key per directory, no
    matter how many names that directory has.
``start_spec(plan_id)``
    What starting this plan needs.

Why the derivation is not a copy
--------------------------------
A copy is a second answer to the same question, and two answers drift.
The scheduler would keep saying a plan is queued while the plan itself
had moved on, and the operator's only way to find out which of the two
was right would be to watch the wrong plan start.  So nothing here is
remembered: every call reads the rows again, and every value returned
is a value that is in the database at that moment.

Why these cases run against a real SQLite file
----------------------------------------------
The claims being pinned are claims about *rows*: a phase in
``plan_routing``, a pid in ``plan_execution``, a status in
``plan_decisions``.  A stand-in connection could hold the same tuples
and still not pin the SQL — the join, the left join that must not
swallow a plan, the column the derivation actually reads.  The
database here is the production schema on a real file, opened the way
production opens it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reader(
    conn: sqlite3.Connection, project_root: Path, **kwargs: Any
) -> Any:
    """A facts reader over ``conn``.

    The import happens inside the helper so that a missing module is
    reported as the failure of the case that needed it, rather than as
    a collection error that hides every other case in this file.
    """
    from plan_scheduler.facts import PlanFactsReader

    return PlanFactsReader(conn, project_root, **kwargs)


def _iso(offset_seconds: float = 0.0) -> str:
    """The current UTC time, ``offset_seconds`` away, as the DB writes it."""
    moment = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
        seconds=offset_seconds
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _routing(conn: sqlite3.Connection, plan_id: str, phase: str) -> None:
    """Write the plan's routing fact: the phase it is in."""
    conn.execute(
        "INSERT INTO plan_routing (plan_id, current_phase, updated_at) "
        "VALUES (?, ?, ?)",
        (plan_id, phase, _iso()),
    )


def _execution(
    conn: sqlite3.Connection,
    plan_id: str,
    exec_pid: Optional[int] = None,
    exec_status: Optional[str] = None,
    project_dir: Optional[str] = None,
    next_run_at: Optional[str] = None,
) -> None:
    """Write the plan's execution fact: the subprocess and the directory."""
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, project_dir, next_run_at, exec_pid, exec_status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (plan_id, project_dir, next_run_at, exec_pid, exec_status, _iso()),
    )


def _verification(
    conn: sqlite3.Connection,
    plan_id: str,
    status: str = "running",
    updated_at: Optional[str] = None,
) -> None:
    """Write the plan's verification fact: the round and its heartbeat."""
    conn.execute(
        "INSERT INTO plan_verification "
        "(plan_id, verification_status, round, max_rounds, updated_at) "
        "VALUES (?, ?, 0, 3, ?)",
        (plan_id, status, updated_at if updated_at is not None else _iso()),
    )


def _decision(
    conn: sqlite3.Connection,
    plan_id: str,
    status: str = "pending",
    decision_id: Optional[str] = None,
) -> None:
    """Write one human-in-the-loop decision about the plan."""
    conn.execute(
        "INSERT INTO plan_decisions "
        "(decision_id, plan_id, source, question, options, status, "
        " created_at, updated_at) "
        "VALUES (?, ?, 'test', 'A question the operator has to answer?', "
        "'[]', ?, ?, ?)",
        (
            decision_id or f"d-{plan_id}-{status}",
            plan_id,
            status,
            _iso(),
            _iso(),
        ),
    )


def _queue_row(
    conn: sqlite3.Connection,
    plan_id: str,
    seq: int = 1,
    priority: int = 0,
    promote_order: Optional[int] = None,
) -> None:
    """Write the scheduler's own queue row for the plan."""
    conn.execute(
        "INSERT INTO scheduler_queue "
        "(plan_id, seq, priority, promote_order, enqueued_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (plan_id, seq, priority, promote_order, _iso(), _iso()),
    )


def _live_pid(case_registry: Any) -> int:
    """The pid of a process that is running right now."""
    process = case_registry.spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    return int(process.pid)


def _dead_pid(case_registry: Any) -> int:
    """The pid of a process that has already exited."""
    process = case_registry.spawn([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return int(process.pid)


def _realpath(path: Any) -> str:
    """The one path a directory is known by."""
    return os.path.normpath(os.path.realpath(str(path)))


# ---------------------------------------------------------------------------
# The queued plans
# ---------------------------------------------------------------------------


def test_queued_plans_derived_from_routing(
    db_factory: Any, tmp_path: Path
) -> None:
    """A plan whose routing phase is ``queued`` is a queued plan.

    The set of queued plans comes from ``plan_routing`` — the plan's own
    phase — not from the scheduler's queue table.  A plan that is
    ``queued`` but has no queue row yet (a plan imported before the
    scheduler's first tick) is still a queued plan, and the engine has
    to be able to see it; a plan that still has a queue row but has left
    the ``queued`` phase is not one any more.
    """
    db = db_factory()
    conn = db.connect()
    try:
        _routing(conn, "p1", "queued")
        _routing(conn, "p2", "executing")
        _queue_row(conn, "p1", seq=1, priority=0)
        _queue_row(conn, "p2", seq=2, priority=0)

        queued = _reader(conn, tmp_path / "plans").queued_plans()

        assert [entry["plan_id"] for entry in queued] == ["p1"]
        assert queued[0] == {
            "plan_id": "p1",
            "priority": 0,
            "seq": 1,
            "due": True,
        }
    finally:
        conn.close()


def test_queued_plan_is_held_back_by_next_run_at(
    db_factory: Any, tmp_path: Path
) -> None:
    """A plan scheduled for later is not due yet.

    ``next_run_at`` is the one thing that can hold a queued plan back:
    everything else about it is ready, and a plan whose time has not
    come must not be started early.
    """
    db = db_factory()
    conn = db.connect()
    try:
        _routing(conn, "p1", "queued")
        _execution(conn, "p1", next_run_at=_iso(3600))
        _queue_row(conn, "p1", seq=1)

        queued = _reader(conn, tmp_path / "plans").queued_plans()

        assert queued == [
            {"plan_id": "p1", "priority": 0, "seq": 1, "due": False}
        ]
    finally:
        conn.close()


def test_queued_plans_are_ordered_by_the_queue_comparator(
    db_factory: Any, tmp_path: Path
) -> None:
    """The queued plans come back in the queue's own order.

    One comparator decides who runs next, and the panel, the admission
    check and the restart recovery all read it.  A reader that sorted
    the queue on its own would be a second answer to "who runs next".
    """
    db = db_factory()
    conn = db.connect()
    try:
        _routing(conn, "p-late", "queued")
        _routing(conn, "p-early", "queued")
        _routing(conn, "p-promoted", "queued")
        _queue_row(conn, "p-late", seq=1, priority=0)
        _queue_row(conn, "p-early", seq=2, priority=0)
        _queue_row(conn, "p-promoted", seq=3, priority=0, promote_order=-1)

        queued = _reader(conn, tmp_path / "plans").queued_plans()

        assert [entry["plan_id"] for entry in queued] == [
            "p-promoted",
            "p-late",
            "p-early",
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The active plans
# ---------------------------------------------------------------------------


def test_active_union_dedup(
    db_factory: Any, tmp_path: Path, case_registry: Any
) -> None:
    """A plan that is active on both sides is active once.

    The active set is the union of the execution side and the
    verification side, and a plan that satisfies both is one plan: it
    holds one slot, so it may only be counted once.
    """
    db = db_factory()
    conn = db.connect()
    try:
        alive = _live_pid(case_registry)

        _routing(conn, "p-exec", "executing")
        _execution(
            conn,
            "p-exec",
            exec_pid=alive,
            exec_status="running",
            project_dir=str(tmp_path),
        )

        _routing(conn, "p-ver", "verification_running")
        _verification(conn, "p-ver")

        _routing(conn, "p-both", "verification_running")
        _execution(conn, "p-both", exec_pid=alive, exec_status="running")
        _verification(conn, "p-both")

        active = _reader(conn, tmp_path / "plans").active_plans()
        by_id = {entry["plan_id"]: entry for entry in active}

        assert sorted(by_id) == ["p-both", "p-exec", "p-ver"]
        assert by_id["p-exec"]["side"] == "execution"
        assert by_id["p-ver"]["side"] == "verification"

        both = [entry for entry in active if entry["plan_id"] == "p-both"]
        assert len(both) == 1, f"a plan on both sides is one plan: {active!r}"
    finally:
        conn.close()


def test_verification_side_counts_as_active(
    db_factory: Any, tmp_path: Path
) -> None:
    """A plan that is verifying holds compute even without a subprocess.

    A verification round has no execution subprocess — there is no
    ``exec_pid`` to probe — but it is calling a provider and burning CPU
    and memory, so it holds a slot like any other working plan.  Leaving
    it out would allow "four plans verifying plus four plans executing"
    under a limit of four.
    """
    db = db_factory()
    conn = db.connect()
    try:
        _routing(conn, "p1", "verification_running")
        _execution(conn, "p1", exec_status="running")
        _verification(conn, "p1")

        active = _reader(conn, tmp_path / "plans").active_plans()

        assert [entry["plan_id"] for entry in active] == ["p1"]
        assert active[0]["side"] == "verification"
    finally:
        conn.close()


def test_stale_verification_row_is_not_active(
    db_factory: Any, tmp_path: Path
) -> None:
    """A verification nobody has touched for a quarter of an hour is dead.

    The verification thread beats every 30 seconds.  A row that has not
    been updated for 15 minutes belongs to a thread that died, and the
    plan it names is not holding compute any more — counting it would
    keep a slot that nobody can release.
    """
    db = db_factory()
    conn = db.connect()
    try:
        _routing(conn, "p1", "verification_running")
        _verification(conn, "p1", updated_at=_iso(-20 * 60))

        active = _reader(conn, tmp_path / "plans").active_plans()

        assert active == []
    finally:
        conn.close()


def test_dead_pid_not_active(
    db_factory: Any, tmp_path: Path, case_registry: Any
) -> None:
    """A subprocess that has exited does not hold a slot.

    ``exec_pid`` outlives the process it names: the row is only cleared
    when the execution side writes a terminal status, and a crash never
    gets that far.  Until then the pid is the only evidence there was,
    so the reader probes it — and a pid that is gone means the plan is
    not working any more.
    """
    db = db_factory()
    conn = db.connect()
    try:
        finished = _dead_pid(case_registry)

        _routing(conn, "p1", "executing")
        _execution(conn, "p1", exec_pid=finished, exec_status="running")

        active = _reader(conn, tmp_path / "plans").active_plans()

        assert active == []
    finally:
        conn.close()


def test_live_pid_is_active(
    db_factory: Any, tmp_path: Path, case_registry: Any
) -> None:
    """A subprocess that is running does hold a slot.

    The counterpart of the dead-pid case: without this one, "not active"
    would be trivially satisfiable by a reader that never finds a plan.
    """
    db = db_factory()
    conn = db.connect()
    try:
        alive = _live_pid(case_registry)

        _routing(conn, "p1", "executing")
        _execution(conn, "p1", exec_pid=alive, exec_status="running")

        active = _reader(conn, tmp_path / "plans").active_plans()

        assert [entry["plan_id"] for entry in active] == ["p1"]
        assert active[0]["side"] == "execution"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The workspace key
# ---------------------------------------------------------------------------


def test_workspace_key_realpath(
    db_factory: Any, tmp_path: Path, workspace_factory: Any
) -> None:
    """Three names for one workspace are one key.

    The directory, a symlink to it and a symlink to that symlink are
    three strings for one directory.  The key is the real path, so a
    plan working through the alias is recognised as working in the same
    place as a plan working in the directory itself — which is what
    makes the "one plan at a time per workspace" rule survive a
    restart.
    """
    workspace = workspace_factory()
    db = db_factory()
    conn = db.connect()
    try:
        for plan_id, path in (
            ("p-direct", workspace.project),
            ("p-link", workspace.link),
            ("p-alias", workspace.alias),
        ):
            _routing(conn, plan_id, "executing")
            _execution(
                conn, plan_id, exec_status="running", project_dir=str(path)
            )

        reader = _reader(conn, tmp_path / "plans")
        keys = {
            plan_id: reader.workspace_key(plan_id)
            for plan_id in ("p-direct", "p-link", "p-alias")
        }

        expected = str(workspace.realpath)
        assert set(keys.values()) == {expected}, keys
        assert keys["p-direct"] == expected
    finally:
        conn.close()


def test_workspace_key_falls_back_to_the_task_list(
    db_factory: Any, tmp_path: Path, plan_factory: Any
) -> None:
    """Without a directory on the row, the plan's own files answer.

    The order is the one the architecture pins: the execution row's
    ``project_dir`` first, then the ``project_dir`` the task list
    declares, then the workspace the PRD declares.  The first one that
    says something wins — a plan that has been running always has the
    row, and the files are the fallback for one that has not.
    """
    plan = plan_factory(plan_id="p1")
    db = db_factory()
    conn = db.connect()
    try:
        _routing(conn, "p1", "queued")
        _execution(conn, "p1")

        reader = _reader(conn, tmp_path / "plans")

        assert reader.workspace_key("p1") == _realpath(plan.project_dir)
    finally:
        conn.close()


def test_workspace_key_falls_back_to_the_prd(
    db_factory: Any, tmp_path: Path
) -> None:
    """Without a directory on the row or in the tasks, the PRD answers.

    The PRD declares the workspace as a constraint; that declaration is
    the last word on where the plan works.
    """
    plans_root = tmp_path / "plans"
    plan_dir = plans_root / "p1"
    plan_dir.mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [{"id": "p1-1", "title": "no project_dir"}]}),
        encoding="utf-8",
    )
    (plan_dir / "prd.json").write_text(
        json.dumps({"constraints": {"target_project_dir": str(workspace)}}),
        encoding="utf-8",
    )

    db = db_factory()
    conn = db.connect()
    try:
        _routing(conn, "p1", "queued")

        reader = _reader(conn, plans_root)

        assert reader.workspace_key("p1") == _realpath(workspace)
    finally:
        conn.close()


def test_workspace_key_of_an_unknown_plan_is_none(
    db_factory: Any, tmp_path: Path
) -> None:
    """A plan nobody has heard of has no workspace.

    The answer is ``None`` rather than a guess: a plan with no
    directory is not in any workspace, and inventing one would let it
    share a slot with a plan that has one.
    """
    db = db_factory()
    conn = db.connect()
    try:
        reader = _reader(conn, tmp_path / "plans")

        assert reader.workspace_key("p-nobody") is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The pending decision
# ---------------------------------------------------------------------------


def test_has_pending_decision(db_factory: Any, tmp_path: Path) -> None:
    """Only a pending decision is a pending decision.

    A decision that was answered, that fell back to its default or that
    was abandoned is history: the plan is not waiting for anybody any
    more, and the slot it holds may be released.
    """
    db = db_factory()
    conn = db.connect()
    try:
        _decision(conn, "p-pending", status="pending")
        _decision(conn, "p-answered", status="answered")
        _decision(conn, "p-defaulted", status="defaulted")

        reader = _reader(conn, tmp_path / "plans")

        assert reader.has_pending_decision("p-pending") is True
        assert reader.has_pending_decision("p-answered") is False
        assert reader.has_pending_decision("p-defaulted") is False
        assert reader.has_pending_decision("p-unknown") is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The start spec
# ---------------------------------------------------------------------------


def test_start_spec(
    db_factory: Any, tmp_path: Path, plan_factory: Any
) -> None:
    """The start spec says where the plan runs and whether it may run.

    It is derived, not remembered: the directory comes from the plan's
    own facts, the phase from the routing row, and the "is it due"
    answer from the execution row.  A fresh execution has to be given a
    directory, so the spec is what carries it.
    """
    plan = plan_factory(plan_id="p1")
    db = db_factory()
    conn = db.connect()
    try:
        _routing(conn, "p1", "queued")

        reader = _reader(conn, tmp_path / "plans")
        spec = reader.start_spec("p1")

        assert spec["plan_id"] == "p1"
        assert spec["project_dir"] == _realpath(plan.project_dir)
        assert spec["phase"] == "queued"
        assert spec["due"] is True
    finally:
        conn.close()

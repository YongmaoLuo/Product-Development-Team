"""A failed verification round must not leave the plan's row saying ``running``.

The chain this file pins
------------------------

A verification round ends with a verdict.  When the round FAILED and
produced repair tasks, the auto-loop does not end the plan: it dispatches
a background repair execution and returns -- a **hand-off**, not a
terminal verdict.  Two things have to be true at that moment, and for a
long time they were false at the same time:

  * the plan's own row (``plan_verification``) must carry the round's
    verdict -- ``failed`` -- and not the ``running`` value stamped at
    round start.  That row is what ``/api/plan/{id}/status`` reads, so a
    row left at ``running`` says "still verifying" for the whole of the
    repair round: the plan looks alive while its executor works, and the
    card contradicts the body above it;
  * the hand-off must not be mistaken for a death.  The verification
    thread ends at a hand-off exactly the way it ends at a verdict, and
    the watchdog's detection ladder used to read "the thread is gone" as
    "the round died" -- writing ``verification_thread_died_unexpectedly``
    over a plan whose repair executor was still running (PR #138).

The criteria, one test each:

* a failed round is persisted as ``failed`` -- never as ``running``;
* the round's own ``stop_reason`` survives the write;
* a round that was declared dead (its token invalidated) does not get
  to write a verdict at all -- the row stays ``running``;
* a hand-off to an in-flight repair execution is not written as a thread
  death.

The boundary between the second and the fourth is the empty repair
queue: a round that failed with nothing to repair takes the
``loop_stopped`` exit, and the row says ``loop_stopped`` -- which is
another verdict, and also not ``running``.

Hermeticity
------------

Everything happens inside ``tmp_path``: the plan directory, the project
directory, the state database and the plan tree.  No thread is started
(the "dead thread" the watchdog judges is a handle that was never
started), no subprocess is spawned (the coding tool and the repair
dispatch are faked), and the in-memory maps the watchdog reads are
process-global, so the test's entries are removed again afterwards.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ``server`` first: ``verification_loop`` imports it and ``server`` imports
# back, so importing the inner one first trips the cycle.
import server as srv  # noqa: E402
import verification_loop as vl  # noqa: E402
import zombie_rounds  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.routing_repository import (  # noqa: E402
    RoutingRepository,
)
from state_machine.repositories.verification_repository import (  # noqa: E402
    VerificationRepository,
)


#: The plan under test.
PLAN_ID = "20261111-failed-round"

#: The token of the round that fails.
ROUND_TOKEN = "verification_loop-1-failed-round"

#: The reason the orchestrator gives the failed round.
FAILED_STOP_REASON = "same_failure_repeated_after_max_attempts"

#: The reason the watchdog gives a round whose thread is gone.
THREAD_DEATH_REASON = "verification_thread_died_unexpectedly"

#: The orchestrator's verdict for a round that failed and produced
#: repairs.  The spelling is the orchestrator's own -- ``verification_failed``
#: -- and it is the alias table in ``verification_loop`` that maps it onto
#: the column's vocabulary (``failed``).
FAILED_ROUND_RESULT: Dict[str, Any] = {
    "status": "verification_failed",
    "stop_reason": FAILED_STOP_REASON,
    "repair_tasks": [{"id": "R-1", "title": "fix the failing check"}],
}

#: The orchestrator's verdict for a round that failed with nothing to
#: repair: the loop takes its ``loop_stopped`` exit.
NO_REPAIR_ROUND_RESULT: Dict[str, Any] = {
    "status": "verification_failed",
    "stop_reason": FAILED_STOP_REASON,
    "repair_tasks": [],
}

#: What the orchestrator reports for the round it just ran.
ROUND_REPORT: Dict[str, Any] = {
    "verification_results": [{"status": "FAILED", "vp_id": "VP-1"}],
    "rounds": [],
    "summary": "one verification point failed",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeCodingTool:
    """The orchestrator's coding tool.

    A real one spawns a subprocess; this test never starts one, so the
    tool only has to be a distinguishable object.
    """

    def __init__(self, name: str = "tool") -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - a debugging aid
        return f"<FakeCodingTool {self.name}>"


class _FakePlanState:
    """The plan's state machine, reduced to what the loop calls.

    A real ``PlanState`` writes into the operator's plan tree and keeps
    its own copy of the phase; the loop only asks it for a handful of
    verbs, and none of them is what this file is about.
    """

    def __init__(self, plan_dir: Path) -> None:
        self.plan_dir = plan_dir

    def set_verification_max_rounds(self, max_rounds: int) -> None:
        pass

    def ensure_row(self) -> None:
        """The real one creates the routing row; this file seeds it itself."""

    def reload(self) -> None:
        pass

    def begin_verification(self, round_n: Optional[int] = None) -> None:
        pass

    def transition_to(self, phase: str) -> None:
        pass

    def force_set_phase(self, phase: str) -> None:
        pass

    def get_current_phase(self) -> str:
        return "verification_running"


def _orchestrator_factory(
    result: Dict[str, Any],
    on_judged: Optional[Callable[[], None]] = None,
) -> Any:
    """A fake orchestrator that answers with ``result``.

    ``on_judged`` runs at the moment the round is judged -- between the
    round's work and the verdict being written -- which is where a
    watchdog that declares the round dead would strike.
    """

    class _FakeOrchestrator:
        def __init__(
            self, plan_dir: Path, project_dir: Path,
            coding_tool: Any = None, verif_repo: Any = None,
        ) -> None:
            self.plan_dir = plan_dir
            self.project_dir = project_dir
            self.coding_tool = coding_tool
            self.verif_repo = verif_repo

        def start_verification_cycle(self, *args: Any, **kwargs: Any) -> Dict:
            return dict(ROUND_REPORT)

        def check_cycle_conditions(
            self, report: Dict, round_number: Optional[int] = None,
        ) -> Dict:
            if on_judged is not None:
                on_judged()
            return dict(result)

        def confirm_repair_and_rerun(self) -> None:
            pass

    return _FakeOrchestrator


def _repair_dispatch_factory(
    calls: List[Dict[str, Any]],
    db_path: Path,
) -> Any:
    """A fake repair dispatch that records the hand-off.

    The real ``_run_repair_execution_async`` spawns an executor
    subprocess and returns; this one does the two observable things
    that dispatch does and spawns nothing:

      * it seeds ``_execution_state[plan_id]`` with the in-flight record
        (``status="running"``, ``ended_at=None``, ``process=None``) --
        the ONE answer in the process to "is a background execution
        outstanding for this plan", and the criterion the watchdog's
        detection ladder asks before it calls a thread end a death;
      * it records the hand-off on the loop's outcome, and stamps the
        routing phase ``executing`` the way the executor's own start
        does.
    """

    def _dispatch(
        plan_id: str,
        project_dir: Path,
        tool: Any = None,
        on_complete: Any = None,
        outcome: Any = None,
        scene: Optional[str] = None,
    ) -> Dict[str, Any]:
        calls.append({
            "plan_id": plan_id,
            "project_dir": project_dir,
            "tool": tool,
            "scene": scene,
        })
        with srv._execution_state_lock:
            srv._execution_state[plan_id] = {
                "status": "running",
                "ended_at": None,
                "process": None,
            }
        if outcome is not None:
            outcome.handed_off = True
        _set_phase(db_path, plan_id, "executing")
        return {"status": "started", "pid": 4242}

    return _dispatch


def _set_phase(db_path: Path, plan_id: str, phase: str) -> None:
    """Move the plan's routing phase, the way the state machine does."""
    conn = open_db(str(db_path))
    try:
        migrate(conn)
        RoutingRepository(conn).try_mark_phase(
            plan_id, ("verification_running", "executing"), phase,
        )
    finally:
        conn.close()


def _phase(db_path: Path, plan_id: str) -> Any:
    """The plan's routing phase, as the state machine sees it."""
    conn = open_db(str(db_path))
    try:
        row = conn.execute(
            "SELECT current_phase FROM plan_routing WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _seed_running_verification(db_path: Path, plan_id: str) -> None:
    """Seed ``plan_routing`` + ``plan_verification`` mid-verification.

    The row says ``running`` -- the value the round start stamps -- and
    the plan is in the verification family, which is where the auto-loop
    finds it.
    """
    conn = open_db(str(db_path))
    try:
        migrate(conn)
        RoutingRepository(conn).insert(plan_id, "verification_running")
        repo = VerificationRepository(conn)
        repo.insert(plan_id, "running")
        repo.init_round(plan_id, round_n=1, max_rounds=3)
    finally:
        conn.close()


def _row(db_path: Path, plan_id: str) -> Dict[str, Any]:
    """The plan's verification row, as the status endpoint reads it."""
    conn = open_db(str(db_path))
    try:
        return VerificationRepository(conn).current(plan_id)
    finally:
        conn.close()


def _forget_plan_actions(plan_id: str) -> None:
    """Drop the watchdog's recorded actions for one plan.

    The action log is a bounded deque shared by the whole process, so a
    test that leaves an entry behind would hand it to the next one.
    """
    actions = srv._WATCHDOG_STATS["actions"]
    for action in [
        action for action in actions if action.get("plan_id") == plan_id
    ]:
        actions.remove(action)


def _dead_thread() -> threading.Thread:
    """A thread handle that is not alive -- and was never started.

    The watchdog judges "the thread is gone" by asking ``is_alive()``.
    A handle that was never started answers ``False`` exactly like a
    thread that has exited, and nothing is run: this test starts no
    thread of its own.
    """
    return threading.Thread(target=lambda: None)


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_env(tmp_path: Path, monkeypatch: Any):
    """A plan whose verification round is about to fail.

    The plan directory, the project directory and the state database
    all live inside the test's own tree, and the seams that would reach
    outside it are replaced:

      * the coding tool (a real one spawns a subprocess);
      * the plan state (a real one writes into the operator's plan tree);
      * the HITL sweep (orthogonal to a round's verdict, and it would
        otherwise reach for the state database of the machine the test
        runs on).
    """
    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    plan_dir = plans_root / PLAN_ID
    plan_dir.mkdir()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    db_path = tmp_path / "state.db"

    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db_path))
    monkeypatch.setattr(srv, "PLANS_DIR", plans_root)
    monkeypatch.setattr(srv, "_state_db_path", lambda *a, **k: db_path)
    monkeypatch.setattr(srv, "PlanState", _FakePlanState)
    monkeypatch.setattr(
        srv, "create_coding_tool", lambda *a, **k: _FakeCodingTool(),
    )
    monkeypatch.setattr(vl, "_expire_hitl_decisions", lambda now: 0)
    monkeypatch.setattr(
        vl, "_handle_hitl_strict_timeout", lambda plan_id, now: None,
    )

    # The in-memory maps are process-global. The plan under test is
    # removed from them afterwards so it cannot leak into another test.
    srv._verification_state.pop(PLAN_ID, None)
    srv._execution_state.pop(PLAN_ID, None)
    srv._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN_ID, None)
    _forget_plan_actions(PLAN_ID)

    _seed_running_verification(db_path, PLAN_ID)

    yield {
        "plan_dir": plan_dir,
        "project_dir": project_dir,
        "db_path": db_path,
    }

    srv._verification_state.pop(PLAN_ID, None)
    srv._execution_state.pop(PLAN_ID, None)
    srv._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN_ID, None)
    _forget_plan_actions(PLAN_ID)


def _run_failed_round(
    plan_env: Dict[str, Any],
    monkeypatch: Any,
    result: Optional[Dict[str, Any]] = None,
    on_judged: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """Drive one failed round through the production auto-loop.

    Returns a record of what the round did: the repair dispatches it
    made and the outcome it handed back.
    """
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        srv, "VerificationOrchestrator",
        _orchestrator_factory(result or FAILED_ROUND_RESULT, on_judged),
    )
    monkeypatch.setattr(
        srv, "_run_repair_execution_async",
        _repair_dispatch_factory(calls, plan_env["db_path"]),
    )
    outcome = vl._VerificationLoopOutcome()
    vl._run_auto_verification_loop_inner(
        PLAN_ID,
        plan_env["plan_dir"],
        plan_env["project_dir"],
        max_rounds=3,
        outcome=outcome,
        round_token=ROUND_TOKEN,
    )
    return {"dispatches": calls, "outcome": outcome}


# ---------------------------------------------------------------------------
# 1. The verdict lands on the row
# ---------------------------------------------------------------------------


def test_failed_round_writes_failed_not_running(plan_env, monkeypatch):
    """A failed round is persisted as ``failed``.

    Before the recorder learned the orchestrator's spelling, the write
    was dropped silently: the row kept the ``running`` value stamped at
    round start, and the plan looked like it was still verifying for the
    whole of the repair round.
    """
    _run_failed_round(plan_env, monkeypatch)

    row = _row(plan_env["db_path"], PLAN_ID)
    assert row is not None, "the plan has no verification row at all"
    assert row["verification_status"] == "failed", (
        f"the failed round left the row saying "
        f"{row['verification_status']!r}, not 'failed'"
    )
    assert row["verification_status"] != "running", (
        "the failed round left the plan's row saying 'running'"
    )


# ---------------------------------------------------------------------------
# 2. The reason survives
# ---------------------------------------------------------------------------


def test_failed_round_preserves_stop_reason(plan_env, monkeypatch):
    """The row carries the reason the round gave.

    The status alone says the round failed; the reason says WHY, and it
    is the reason the operator reads on the card.  A write that kept the
    status and dropped the reason would leave the plan failed and
    unexplained.
    """
    _run_failed_round(plan_env, monkeypatch)

    row = _row(plan_env["db_path"], PLAN_ID)
    assert row is not None, "the plan has no verification row at all"
    assert row["verification_stop_reason"] == FAILED_STOP_REASON, (
        f"the row's stop reason is {row['verification_stop_reason']!r}, "
        f"not the round's own {FAILED_STOP_REASON!r}"
    )
    assert (row.get("results") or {}).get("stop_reason") == FAILED_STOP_REASON


# ---------------------------------------------------------------------------
# 3. A dead round does not get to speak
# ---------------------------------------------------------------------------


def test_invalidated_round_still_refused(plan_env, monkeypatch):
    """A round that was declared dead does not write a verdict.

    ``plan_verification`` is the PLAN's column.  A round the watchdog
    wrote off does not get to speak for the plan -- the plan may already
    have a live round writing to the same row, and the dead round's
    opinion would overwrite it.

    The round is declared dead WHILE it is running (the watchdog's own
    moment), so the recorder sees a dead round asking for a verdict.
    """
    plan_dir = plan_env["plan_dir"]

    def _declare_dead() -> None:
        zombie_rounds.invalidate(
            plan_dir, ROUND_TOKEN,
            reason="verification_log_stale",
            actor="verification_watchdog",
        )

    _run_failed_round(plan_env, monkeypatch, on_judged=_declare_dead)

    row = _row(plan_env["db_path"], PLAN_ID)
    assert row is not None, "the plan has no verification row at all"
    assert row["verification_status"] == "running", (
        f"a round that was declared dead wrote {row['verification_status']!r} "
        f"over the plan's row; it must leave it alone"
    )
    assert row["verification_stop_reason"] is None, (
        "a round that was declared dead wrote a stop reason over the "
        "plan's row"
    )


# ---------------------------------------------------------------------------
# 4. A hand-off is not a death
# ---------------------------------------------------------------------------


def test_handoff_round_is_not_written_as_thread_death(plan_env, monkeypatch):
    """A hand-off to an in-flight repair execution is not a thread death.

    The round failed, the loop dispatched a background repair execution
    and returned.  The verification thread ends at a hand-off exactly
    the way it ends at a verdict, and the watchdog's detection ladder
    used to read "the thread is gone" as "the round died" -- writing
    ``verification_thread_died_unexpectedly`` over a plan whose repair
    executor was still running.

    The chain this test drives:

      1. the round fails and produces repair tasks;
      2. the loop records the round's verdict on the plan's row and
         hands off to a background repair execution, which is in flight;
      3. the verification thread ends;
      4. the watchdog ticks.
    """
    record = _run_failed_round(plan_env, monkeypatch)

    # The hand-off really happened: the repairs were dispatched, the
    # outcome says so, and the execution is in flight.
    assert record["dispatches"], "the failed round dispatched no repairs"
    assert record["outcome"].handed_off is True, (
        "the round handed off to a repair execution without recording it"
    )
    assert srv._plan_execution_in_flight(PLAN_ID) is True, (
        "the repair execution is not in flight, so the hand-off criterion "
        "is not exercised"
    )
    assert _phase(plan_env["db_path"], PLAN_ID) == "executing", (
        "the plan is not in the executing phase, so the hand-off is not "
        "the shape the watchdog has to survive"
    )

    # The round's own verdict is on the row.
    row = _row(plan_env["db_path"], PLAN_ID)
    assert row["verification_status"] == "failed"
    assert row["verification_stop_reason"] == FAILED_STOP_REASON

    # The verification thread ends at the hand-off -- and the next round
    # puts the plan back into the shape the detection ladder judges:
    # "running", with a thread handle that is not alive.
    state = srv._verification_state[PLAN_ID]
    state["verification_status"] = "running"
    state["thread"] = _dead_thread()

    vl._lazy_check_verification(PLAN_ID)

    row = _row(plan_env["db_path"], PLAN_ID)
    assert row["verification_stop_reason"] != THREAD_DEATH_REASON, (
        "the hand-off was written as a thread death"
    )
    assert row["verification_stop_reason"] == FAILED_STOP_REASON, (
        f"the row's stop reason is {row['verification_stop_reason']!r} -- "
        f"the round's own verdict must survive the watchdog tick"
    )
    assert row["verification_status"] == "failed"
    assert not [
        action for action in srv._WATCHDOG_STATS["actions"]
        if action.get("plan_id") == PLAN_ID
    ], "the watchdog recorded an action for a plan that was not dead"

    # --- negative control -------------------------------------------
    # The hand-off criterion is the ONLY thing that keeps the ladder
    # from firing here, so the assertion above has to be able to fail.
    # Take the in-flight execution away and the very same tick writes
    # the thread death over the round's verdict.
    srv._execution_state.pop(PLAN_ID, None)
    srv._WATCHDOG_STATS["per_plan_last_action_ts"].pop(PLAN_ID, None)
    monkeypatch.setattr(
        srv, "_cleanup_dead_verification_processes", lambda *a, **k: None,
    )

    vl._lazy_check_verification(PLAN_ID)

    row = _row(plan_env["db_path"], PLAN_ID)
    assert row["verification_stop_reason"] == THREAD_DEATH_REASON, (
        "the negative control did not fire: without the in-flight "
        "execution the same tick should have written the thread death, "
        f"but the row says {row['verification_stop_reason']!r}"
    )


# ---------------------------------------------------------------------------
# 5. The boundary: a round with nothing to repair
# ---------------------------------------------------------------------------


def test_round_without_repair_tasks_is_written_loop_stopped(
    plan_env, monkeypatch,
):
    """A round with nothing to repair takes the ``loop_stopped`` exit.

    The empty repair queue is the other way a failed round ends, and it
    ends the chain rather than handing off: the row says
    ``loop_stopped``, which is a verdict too -- and, like every other
    verdict here, not ``running``.
    """
    _run_failed_round(plan_env, monkeypatch, result=NO_REPAIR_ROUND_RESULT)

    row = _row(plan_env["db_path"], PLAN_ID)
    assert row is not None, "the plan has no verification row at all"
    assert row["verification_status"] == "loop_stopped", (
        f"the round with nothing to repair left the row saying "
        f"{row['verification_status']!r}, not 'loop_stopped'"
    )
    assert row["verification_status"] != "running"

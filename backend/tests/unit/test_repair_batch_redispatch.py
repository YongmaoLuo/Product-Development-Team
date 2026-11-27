"""An undrained repair batch is re-dispatched, not fatal (2026-11-01).

Background
----------
``_on_repair_complete`` used to read ANY stop reason plus a non-empty
repair queue as "the repair never drains" and write
``loop_stopped``.  One pending task out of three therefore killed the
whole verification round: the plan's verdict became "the loop stopped"
instead of "the code is wrong", and the work that was still queued
never ran.

That conflated two facts that need opposite answers:

* **work was LEFT** (``repair_batch_not_drained:...``) — the batch has
  to be run again.  This is the reason to continue, not to stop.
* **the code is wrong** (``repair_gate_red`` / ``repair_task_failed``)
  — a verdict about the CODE.  The next verification round is what
  measures it.

Contract pinned here
--------------------
* ``repair_batch_not_drained:`` + queued work + guard below the cap →
  the repair executor is re-dispatched and NO terminal is written.
* the same, with the guard AT the cap → exactly one terminal,
  ``loop_stopped`` / ``repair_never_drained:<reason>:<n>x``.
* ``repair_gate_red`` (or any other verdict about the code) + an empty
  queue → no re-dispatch, the guard does not move, the chain continues
  with the next verification round.
* an empty queue (the batch drained after all) → the chain continues
  with the next verification round.

The guard counts CONSECUTIVE undrained stops and moves only there: a
gate-red stop is not an undrained stop, so it must not push the plan
closer to the cap.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Recorder:
    """What the post-repair callback did, in the order it did it."""

    def __init__(self) -> None:
        self.terminals: list = []
        self.repair_dispatches: list = []
        self.rounds: list = []

    @property
    def drained_family_terminals(self) -> list:
        """Terminals whose reason belongs to the undrained family.

        Those are the ones that end the chain over a queue that was
        merely not empty.  ``repair_never_drained`` is the ONE terminal
        of that family that is allowed to exist — and only once the
        bounded redispatch has given up.
        """
        return [
            t for t in self.terminals
            if isinstance(t[2], str) and t[2].startswith(
                ("repair_never_drained", "repair_batch_not_drained"),
            )
        ]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Wire the post-repair callback to hermetic stand-ins."""
    import server
    import verification_loop

    rec = _Recorder()
    state = {"stop_reason": None, "pending": []}

    plans_root = tmp_path / "plans"
    plans_root.mkdir()
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(server, "PLANS_DIR", plans_root)
    monkeypatch.setattr(server, "_state_db_path", lambda *a, **k: db_path)

    # The terminal write — what would end the chain.
    def _record_terminal(plan_id, status, stop_reason, **kwargs):
        rec.terminals.append((plan_id, status, stop_reason))

    monkeypatch.setattr(
        server, "_record_verification_terminal", _record_terminal,
    )

    # The repair dispatch — what would re-run the executor.
    def _run_repair(plan_id, project_dir, tool=None, on_complete=None, **kw):
        rec.repair_dispatches.append({
            "plan_id": plan_id,
            "project_dir": project_dir,
            "tool": tool,
            "on_complete": on_complete,
        })
        return {"status": "started", "pid": 4242}

    monkeypatch.setattr(server, "_run_repair_execution_async", _run_repair)

    # The repair path's own verdict, and the queue it left behind.
    monkeypatch.setattr(
        verification_loop, "_repair_path_stop_reason",
        lambda plan_id: state["stop_reason"],
    )
    monkeypatch.setattr(
        verification_loop, "_get_pending_repair_tasks",
        lambda plan_id, *a, **kw: list(state["pending"]),
    )

    # The verification round the callback re-enters.
    def _dispatch(plan_id, plan_dir, orch, round_n, *a, **kw):
        rec.rounds.append({"plan_id": plan_id, "round": round_n})
        return ({"stop_reason": None}, None)

    monkeypatch.setattr(
        verification_loop, "_dispatch_verification_round", _dispatch,
    )

    # The orchestrator the callback builds for the fresh round.
    class _FakeOrch:
        def __init__(self, *a, **kw):
            pass

        def check_cycle_conditions(self, report, round_number=None):
            return {
                "status": "passed",
                "repair_tasks": [],
                "stop_reason": None,
            }

        def _extract_failed_ids(self, report):
            return set()

        def confirm_repair_and_rerun(self):
            return None

        def start_verification_cycle(self, **kwargs):
            return {"report": "fake"}

    monkeypatch.setattr(server, "VerificationOrchestrator", _FakeOrch)
    monkeypatch.setattr(
        server, "create_coding_tool", lambda *a, **kw: object(),
    )

    # The guard is module-level state — give every test its own.
    monkeypatch.setattr(verification_loop, "_repair_not_drained_guard", {})

    return {
        "server": server,
        "verification_loop": verification_loop,
        "state": state,
        "rec": rec,
        "project_dir": tmp_path,
    }


def _invoke(harness, plan_id: str = "p-redispatch") -> None:
    """Run the post-repair callback the way the repair thread does.

    The plan gets its directory first.  Since 2026-11-13 the re-entry
    is a round INSTANCE of its own: it mints a token and CLAIMS the
    plan's service-owner role before it dispatches.  The claim refuses
    a plan directory that is not there — the registry write creates the
    directory it writes into, so a claim aimed at a missing path would
    bring a plan into being.  A plan with no directory therefore stops
    at that refusal and never reaches the next verification round, which
    is exactly the case these tests are about: the directory has to be
    there for the claim to be made, and the claim has to be made for the
    chain to continue.
    """
    plan_dir = harness["server"]._plan_dir(plan_id)
    Path(plan_dir).mkdir(parents=True, exist_ok=True)

    callback = harness["verification_loop"]._make_repair_on_complete(
        plan_id, harness["project_dir"], tool=None,
    )
    callback(0)


# ---------------------------------------------------------------------------
# 1. One pending task is work left behind, not a verdict
# ---------------------------------------------------------------------------


def test_one_pending_redispatches_instead_of_terminating(harness):
    """The queue is not empty → run the executor again, write no verdict."""
    vl = harness["verification_loop"]
    rec = harness["rec"]
    plan_id = "p-redispatch"

    harness["state"]["stop_reason"] = "repair_batch_not_drained:1_pending"
    harness["state"]["pending"] = [{"id": "repair-r1-02"}]
    vl._repair_not_drained_guard[plan_id] = 1

    _invoke(harness, plan_id)

    assert len(rec.repair_dispatches) == 1, (
        "an undrained batch must be re-dispatched — one pending task out "
        "of three is work left behind, not a reason to end the round"
    )
    dispatch = rec.repair_dispatches[0]
    assert dispatch["plan_id"] == plan_id
    assert callable(dispatch["on_complete"]), (
        "the re-dispatch has to hand the callback back, so the next "
        "attempt gets another chance to drain the batch"
    )
    assert rec.terminals == [], (
        "no terminal verdict may be written while the chain is still "
        "running — the guard is below its cap"
    )
    assert vl._repair_not_drained_guard[plan_id] == 2, (
        "the guard counts consecutive undrained stops, and this one was "
        "an undrained stop"
    )


# ---------------------------------------------------------------------------
# 2. The bound on the redispatch
# ---------------------------------------------------------------------------


def test_guard_cap_stops_with_repair_never_drained(harness):
    """At the cap the chain stops — with a reason of its own."""
    vl = harness["verification_loop"]
    rec = harness["rec"]
    plan_id = "p-redispatch"

    harness["state"]["stop_reason"] = "repair_batch_not_drained:1_pending"
    harness["state"]["pending"] = [{"id": "repair-r1-02"}]
    # The third undrained stop in a row.
    vl._repair_not_drained_guard[plan_id] = (
        vl._MAX_REPAIR_NOT_DRAINED_GUARDS - 1
    )

    _invoke(harness, plan_id)

    assert rec.repair_dispatches == [], (
        "the redispatch is bounded — at the cap the executor is not "
        "dispatched a fourth time"
    )
    assert len(rec.terminals) == 1, (
        "the chain stops exactly once, and only here"
    )
    plan_id_arg, status, reason = rec.terminals[0]
    assert plan_id_arg == plan_id
    assert status == "loop_stopped"
    assert reason == (
        "repair_never_drained:repair_batch_not_drained:1_pending:"
        f"{vl._MAX_REPAIR_NOT_DRAINED_GUARDS}x"
    ), (
        "the terminal literal of the undrained family is "
        "repair_never_drained — the batch's own reason is not a verdict"
    )


# ---------------------------------------------------------------------------
# 3. A gate-red stop is a verdict about the CODE
# ---------------------------------------------------------------------------


def test_gate_red_proceeds_without_redispatch(harness):
    """``repair_gate_red`` → no re-dispatch, no guard bump, next round."""
    vl = harness["verification_loop"]
    rec = harness["rec"]
    plan_id = "p-redispatch"

    harness["state"]["stop_reason"] = "repair_gate_red"
    harness["state"]["pending"] = []

    _invoke(harness, plan_id)

    assert rec.repair_dispatches == [], (
        "a gate-red stop says the code is wrong — re-running the repair "
        "executor buys nothing, the next verification round is what "
        "measures it"
    )
    assert rec.drained_family_terminals == [], (
        "no terminal of the undrained family may be written for a "
        "gate-red stop"
    )
    assert len(rec.rounds) == 1, (
        "the chain must continue with the next verification round"
    )
    assert vl._repair_not_drained_guard.get(plan_id, 0) == 0, (
        "the guard counts UNDRAINED stops — a gate-red stop must not "
        "push the plan closer to the cap"
    )


# ---------------------------------------------------------------------------
# 4. An empty queue means the batch drained after all
# ---------------------------------------------------------------------------


def test_no_pending_after_repair_proceeds_to_next_round(harness):
    """The queue is empty → the batch drained → the next round runs."""
    vl = harness["verification_loop"]
    rec = harness["rec"]
    plan_id = "p-redispatch"

    harness["state"]["stop_reason"] = "repair_batch_not_drained:1_pending"
    harness["state"]["pending"] = []

    _invoke(harness, plan_id)

    assert rec.repair_dispatches == [], (
        "nothing is left to run — the batch drained after all"
    )
    assert rec.drained_family_terminals == [], (
        "a drained batch is not a terminal verdict"
    )
    assert len(rec.rounds) == 1, (
        "the chain must continue with the next verification round"
    )
    assert vl._repair_not_drained_guard.get(plan_id, 0) == 0, (
        "the guard is reset once the batch drained"
    )
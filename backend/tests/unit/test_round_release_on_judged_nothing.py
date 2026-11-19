"""A round that judged nothing must not consume a round number.

Background
----------
2026-11-01 (verification convergence, §3): two plans —
``20261031-dispatch-loop-and-lo`` and ``20261029-c7c29faf`` — ran a
round that came out ``overall=SKIPPED`` with nothing measured, and the
round number was eaten anyway.  A plan that spends a round on a turn
which never looked at the product burns its budget on nothing: the
"四轮内收敛" promise is kept by the counter, not by the work.

``_round_release_reason`` is the decision: it reads the orchestrator's
cycle result and the round's report and answers "did this round measure
anything?".  ``None`` means the round keeps its number — a verdict owns
the round it was produced in.  A string is the reason the number goes
back, and the reason is what the operator reads in
``executor_state["released_rounds"]``.

What is pinned here:

  * ``environment_blocked`` → the round is released;
  * 0 VP + ``overall=SKIPPED`` → the round is released;
  * a round with a PASSED VP → the number is kept;
  * a round whose VPs all came back BLOCKED → the round is released;
  * and the auto-loop really hands the number back — the decision is
    wired into the loop, not only spelled out in a helper.

The last one is the wiring: ``_round_release_reason`` decides, the
auto-loop calls ``_release_unverified_round`` (task 7) with the reason,
and the counter ends at ``n - 1``.

Boundary conditions
-------------------
  * a VP that carries a verdict (PASSED / FAILED / PARTIAL) means the
    round measured something — the number stays, whatever the overall
    status says;
  * the release runs AFTER the terminal write, so the terminal verdict
    stands and the counter still ends at ``n - 1``;
  * the release only takes effect for the NEXT ``/start``: the round
    that judged nothing still ends the loop, it is not re-run in place.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ``backend/`` on sys.path so ``import server`` / ``import
# ``verification_loop`` work no matter which entry point runs the suite.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

PLAN_ID = "20260101-round-release-judged-nothing"


# ---------------------------------------------------------------------------
# The four shapes of a round
# ---------------------------------------------------------------------------

#: The environment took the round out before a single VP ran.
ENVIRONMENT_BLOCKED_RESULT = {
    "status": "loop_stopped",
    "environment_blocked": True,
}
ENVIRONMENT_BLOCKED_REPORT = {
    "verification_results": [],
    "rounds": [{"results": []}],
}

#: The round ran and measured nothing: no VP at all, and the report
#: says so.
ZERO_VP_RESULT = {"status": "loop_stopped"}
ZERO_VP_REPORT = {
    "overall_status": "SKIPPED",
    "verification_results": [],
    "rounds": [{"results": []}],
}

#: A round that measured something: one VP came back PASSED.
PASSED_RESULT = {"status": "passed"}
PASSED_REPORT = {
    "overall_status": "PASSED",
    "verification_results": [
        {"id": "VP-001", "status": "PASSED"},
    ],
}

#: A round whose VPs were all blocked — nothing was judged, so the
#: round measured nothing either.
ALL_BLOCKED_RESULT = {"status": "loop_stopped"}
ALL_BLOCKED_REPORT = {
    "overall_status": "SKIPPED",
    "verification_results": [
        {"id": "VP-001", "status": "BLOCKED"},
        {"id": "VP-002", "status": "BLOCKED"},
    ],
}


# ---------------------------------------------------------------------------
# Fixtures — a throwaway state.db + plans tree
# ---------------------------------------------------------------------------


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Point the loop at a throwaway state.db and plans tree.

    Nothing here may touch the operator's live tree: the plan
    directory, the SQLite state store and the ``execution.log`` all land
    under ``tmp_path``.
    """
    import server

    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    # The usage report snapshots a ~90 MB CC Switch store on a daemon
    # thread. A test has no use for it, and it would keep the CPU busy
    # after the test finished.
    monkeypatch.setattr(server, "_refresh_usage_report", lambda *a, **k: None)

    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    db = tmp_path / "state.db"
    conn = open_db(db)
    migrate(conn)
    conn.close()
    monkeypatch.setattr(server, "_state_db_path", lambda: db)

    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir()
    (plan_dir / "tasks.json").write_text("[]", encoding="utf-8")
    return plan_dir


@pytest.fixture(autouse=True)
def _clean_verification_state():
    """The in-memory verification state is process-global; keep it clean."""
    import server

    with server._verification_lock:
        server._verification_state.pop(PLAN_ID, None)
    yield
    with server._verification_lock:
        server._verification_state.pop(PLAN_ID, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _JudgedNothingOrchestrator:
    """An orchestrator whose round came back without a verdict."""

    def __init__(self, report, result):
        self.report = report
        self.result = result
        self.rounds = []

    def start_verification_cycle(
        self, round_number=None, force=False, resume=False,
    ):
        self.rounds.append(round_number)
        return self.report

    def check_cycle_conditions(self, report, round_number=None):
        return dict(self.result)


def _drive_judged_nothing_round(
    plan_dir, monkeypatch, report, result, *, start_round=3, max_rounds=4,
):
    """Run the real auto-loop over one judged-nothing round."""
    import server

    monkeypatch.setattr(
        server, "PlanState", lambda *a, **kw: MagicMock(name="PlanState"),
    )
    monkeypatch.setattr(
        server, "create_coding_tool",
        lambda *a, **kw: MagicMock(name="coding_tool"),
    )
    orch = _JudgedNothingOrchestrator(report, result)
    monkeypatch.setattr(
        server, "VerificationOrchestrator", lambda *a, **kw: orch,
    )

    server._run_auto_verification_loop_inner(
        PLAN_ID, plan_dir, plan_dir,
        max_rounds=max_rounds,
        start_round=start_round,
    )
    return orch


def _verification_row(plan_id: str = PLAN_ID):
    """The plan's ``plan_verification`` row, as the repository reads it."""
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(server._state_db_path())
    try:
        return VerificationRepository(conn).summary(plan_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 1 — the environment took the round out
# ---------------------------------------------------------------------------


def test_environment_blocked_releases():
    """``environment_blocked`` → the round hands its number back.

    The environment took the round out before anything was measured, so
    the number must not be charged to the plan.  The reason says which
    of the two kinds of "nothing measured" this was, because the
    operator reads it in ``released_rounds``.
    """
    import verification_loop

    reason = verification_loop._round_release_reason(
        ENVIRONMENT_BLOCKED_RESULT, ENVIRONMENT_BLOCKED_REPORT,
    )

    assert reason == "environment_blocked", (
        "the environment blocked the round — the reason must say so"
    )


# ---------------------------------------------------------------------------
# Test 2 — a round with no VP at all
# ---------------------------------------------------------------------------


def test_zero_vp_skipped_releases():
    """0 VP + ``overall=SKIPPED`` → the round hands its number back.

    The round ran and produced no verdict at all: the report says
    ``SKIPPED`` and carries no verification point.  Nothing was
    measured, so the number goes back — and the reason is
    ``round_judged_nothing`` rather than ``environment_blocked``,
    because the environment was not what stopped it.
    """
    import verification_loop

    reason = verification_loop._round_release_reason(
        ZERO_VP_RESULT, ZERO_VP_REPORT,
    )

    assert reason == "round_judged_nothing", (
        "the round judged nothing — the reason must say so"
    )


# ---------------------------------------------------------------------------
# Test 3 — the boundary: a round that measured something keeps its number
# ---------------------------------------------------------------------------


def test_passed_round_keeps_number():
    """A round with a PASSED VP keeps its number.

    A verdict owns the round it was produced in: handing that number
    back would give the plan a round for work that was already done.
    """
    import verification_loop

    reason = verification_loop._round_release_reason(
        PASSED_RESULT, PASSED_REPORT,
    )

    assert reason is None, (
        f"a round that measured something must keep its number; got {reason!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — every VP blocked
# ---------------------------------------------------------------------------


def test_all_blocked_releases():
    """A round whose VPs all came back BLOCKED is released too.

    The VPs were dispatched, but none of them produced a verdict: the
    round measured nothing, so it must not cost the plan a round.
    """
    import verification_loop

    reason = verification_loop._round_release_reason(
        ALL_BLOCKED_RESULT, ALL_BLOCKED_REPORT,
    )

    assert reason == "round_judged_nothing", (
        f"a round where every VP was blocked measured nothing; got {reason!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 — the wiring: the auto-loop really gives the number back
# ---------------------------------------------------------------------------


def test_judged_nothing_round_releases_the_number(hermetic, monkeypatch):
    """The loop releases the round it just judged empty.

    The decision alone is not the feature: the number has to go back,
    the terminal verdict has to stand, and the audit trail has to name
    the reason.
    """
    orch = _drive_judged_nothing_round(
        hermetic, monkeypatch, ZERO_VP_REPORT, ZERO_VP_RESULT,
    )

    assert orch.rounds == [3], (
        "the round must have been dispatched (and judged empty) — "
        "otherwise this test proves nothing about the release"
    )

    row = _verification_row()
    assert row is not None, "the round must have written the row"
    assert row["round"] == 2, (
        "the round measured nothing, so the counter must stay at 2; "
        f"got {row['round']!r}"
    )
    assert row["verification_status"] == "loop_stopped", (
        "the terminal verdict must still stand — the release only gives "
        "the NUMBER back, it does not undo the plan's ending"
    )

    released = (row["executor_state"] or {}).get("released_rounds") or []
    assert [(entry["round"], entry["reason"]) for entry in released] == [
        (3, "round_judged_nothing"),
    ], f"the release must be audited; got {released!r}"


# ---------------------------------------------------------------------------
# Test 6 — the invariant is the helper's own, not the caller's promise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vp_status", ["PASSED", "FAILED", "PARTIAL"])
def test_helper_refuses_to_release_a_round_that_measured_something(
    hermetic, vp_status,
):
    """A round that measured something keeps its number — the helper says no.

    "Only a round that measured nothing may be released" used to be a
    sentence in a docstring, which is a promise every call site has to
    keep.  It is now the helper's own invariant: hand it the round's
    report and it checks the report itself.  A report carrying a verdict
    about the deliverable means the round measured something, so the
    release is REFUSED — ``False``, not an exception — and the counter
    does not move.
    """
    import server
    import verification_loop
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(server._state_db_path())
    try:
        VerificationRepository(conn).init_round(PLAN_ID, 3, 4)
    finally:
        conn.close()

    judged_report = {
        "overall_status": vp_status,
        "verification_results": [
            {"id": "VP-001", "status": vp_status},
        ],
    }

    assert verification_loop._release_unverified_round(
        PLAN_ID, 3, "round_judged_nothing", report=judged_report,
    ) is False, (
        "a round that measured something must keep its number — the "
        "helper has to refuse, not the caller"
    )

    row = _verification_row()
    assert row["round"] == 3, (
        "the refused release must not have moved the counter; "
        f"got {row['round']!r}"
    )
    released = (row["executor_state"] or {}).get("released_rounds") or []
    assert released == [], (
        f"a refused release must leave no audit trail; got {released!r}"
    )


def test_helper_releases_a_round_whose_vps_only_carry_no_verdict(hermetic):
    """The other side of the invariant: a round with no verdict is released.

    The guard must not turn into a blanket refusal — a report whose
    verification points are all SKIPPED or BLOCKED carries no verdict
    about the deliverable, so the number still goes back.
    """
    import server
    import verification_loop
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(server._state_db_path())
    try:
        VerificationRepository(conn).init_round(PLAN_ID, 3, 4)
    finally:
        conn.close()

    unjudged_report = {
        "overall_status": "SKIPPED",
        "verification_results": [
            {"id": "VP-001", "status": "BLOCKED"},
            {"id": "VP-002", "status": "SKIPPED"},
        ],
    }

    assert verification_loop._release_unverified_round(
        PLAN_ID, 3, "round_judged_nothing", report=unjudged_report,
    ) is True

    row = _verification_row()
    assert row["round"] == 2, (
        f"the counter must be rewound by one; got {row['round']!r}"
    )

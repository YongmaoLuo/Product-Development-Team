"""A round that dispatched no VP must not consume a round number.

Background
----------
2026-11-01 (verification convergence, §2 / §3): the auto-loop writes the
round number BEFORE it dispatches the round — ``init_round(plan_id,
round_n=next)`` — and ``init_round`` only ever moves the counter
forward.  Two preflights can block a round after that write and before
a single verification point is dispatched:

  * the **base-drift preflight** — the branch drifted away from its base
    and merging the base back left conflicts (§2);
  * the **service preflight** — the round's services were not usable (§3).

Both refusals produce an EMPTY report: the preflight runs before the
verification plan is even generated, so nothing was measured.  The round
number was already spent, so the plan burned one of its rounds on a turn
that never looked at the product.

``_release_unverified_round`` is the way back.  It hands the number to
``VerificationRepository.release_round`` (task 6), which rewinds the
counter by exactly one and records WHY in
``executor_state["released_rounds"]`` — the audit trail survives the
rewind instead of vanishing together with the number.

What is pinned here:

  * the helper passes the round and the reason to the repository, and
    writes the release into the plan's own ``execution.log``;
  * a base-drift-blocked round leaves the counter where it was;
  * a service-preflight-blocked round leaves the counter where it was;
  * the terminal write does NOT cancel the release — the counter is
    still ``n - 1`` after ``_record_terminal`` ran.

Boundary conditions
-------------------
  * a round that DID dispatch a VP is never released — only the two
    blocked reports are, and both of them are pre-dispatch refusals;
  * a release that cannot land (no row) is a warning, never an
    exception: the existing ``_record_terminal`` still stands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ``backend/`` on sys.path so ``import server`` / ``import
# verification_loop`` work no matter which entry point runs the suite.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

PLAN_ID = "20260101-round-release"

#: The round the base-drift preflight blocked: the branch drifted away
#: from its base and merging the base back left conflicts.  The report
#: carries NO verification results — the preflight runs before the
#: verification plan is generated, so no VP was dispatched.
BASE_DRIFT_REPORT = {
    "stop_reason": "base_drift_conflicts",
    "summary": "base drift: 2 conflict(s) after merging base",
    "base_drift_conflicts": {
        "status": "base_drift_conflicts",
        "base_ref": "main",
        "base_commit": "0" * 40,
        "conflicts": ["backend/a.py", "backend/b.py"],
    },
    "verification_results": [],
}

#: The round whose services were not usable: the service preflight
#: refused before the first VP was dispatched.
SERVICE_PREFLIGHT_REPORT = {
    "stop_reason": "service_preflight_failed",
    "summary": "the service 'api' on port 18090 was not usable",
    "service_preflight_failed": {
        "service": "api",
        "port": 18090,
        "pid": None,
        "error_kind": "not_listening",
        "detail": "connection refused",
    },
    "verification_results": [],
}

#: The round whose base-drift conflict became a REPAIR TASK: the plan is
#: not finished, it is handed to the executor to run the merge, and the
#: merge comes back as the next round.  The round still dispatched no
#: verification point.
BASE_DRIFT_REPAIR_REPORT = {
    "stop_reason": "base_drift_conflicts",
    "summary": "base drift: 1 conflict(s) after merging base",
    "base_drift_conflicts": {
        "status": "base_drift_conflicts",
        "base_ref": "main",
        "base_commit": "0" * 40,
        "conflicts": ["backend/a.py"],
    },
    "base_drift_repair_task_id": "RP-1",
    "verification_results": [],
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


class _FakeOrchestrator:
    """An orchestrator whose round is blocked before it dispatches a VP."""

    def __init__(self, report):
        self.report = report
        self.rounds = []

    def start_verification_cycle(
        self, round_number=None, force=False, resume=False,
    ):
        self.rounds.append(round_number)
        return self.report


def _drive_blocked_round(
    plan_dir, monkeypatch, report, *, start_round=3, max_rounds=4,
):
    """Run the real auto-loop over one blocked round; return the fake orch."""
    import server

    monkeypatch.setattr(
        server, "PlanState", lambda *a, **kw: MagicMock(name="PlanState"),
    )
    monkeypatch.setattr(
        server, "create_coding_tool",
        lambda *a, **kw: MagicMock(name="coding_tool"),
    )
    orch = _FakeOrchestrator(report)
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


def _execution_log_entries(plan_dir: Path):
    """Every JSON entry the plan's ``execution.log`` holds."""
    log_file = plan_dir / "execution.log"
    if not log_file.exists():
        return []
    return [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Test 1 — the helper talks to the repository
# ---------------------------------------------------------------------------


def test_release_helper_calls_repository(hermetic, monkeypatch):
    """The helper hands the round and the reason to the repository.

    ``release_round`` is the one way back (task 6): it rewinds the
    counter by exactly one and records the reason.  The helper's whole
    job is to call it with the right arguments — and to leave a trace in
    the plan's own ``execution.log``, which is where an operator reads
    what happened to the round.
    """
    import verification_loop
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    calls = []

    def fake_release(self, plan_id, round_n, reason):
        calls.append((plan_id, round_n, reason))
        return True

    monkeypatch.setattr(VerificationRepository, "release_round", fake_release)

    assert verification_loop._release_unverified_round(
        PLAN_ID, 3, "base_drift_conflicts",
    ) is True

    assert calls == [(PLAN_ID, 3, "base_drift_conflicts")]

    released = [
        entry for entry in _execution_log_entries(hermetic)
        if entry.get("event") == "round_released"
    ]
    assert released, (
        "the release must be written to the plan's execution.log — the "
        "operator reads that file, not the database"
    )
    assert released[0]["message"] == "round 3 released: base_drift_conflicts"


# ---------------------------------------------------------------------------
# Test 2 — a base-drift-blocked round does not advance the counter
# ---------------------------------------------------------------------------


def test_blocked_round_not_counted(hermetic, monkeypatch):
    """A base-drift-blocked round leaves the counter where it was.

    The round number is written before the dispatch, so without a
    release the plan spends one of its rounds on a turn that measured
    nothing — and the base-drift conflict it hit gets a second chance
    only after the plan has burned that round.
    """
    orch = _drive_blocked_round(hermetic, monkeypatch, BASE_DRIFT_REPORT)

    assert orch.rounds == [3], (
        "the round must have been dispatched (and blocked) — otherwise this "
        "test proves nothing about the release"
    )

    row = _verification_row()
    assert row is not None, "the round must have written the row"
    assert row["round"] == 2, (
        "the blocked round 3 dispatched no VP, so the counter must stay "
        f"at 2; got {row['round']!r}"
    )

    released = (row["executor_state"] or {}).get("released_rounds") or []
    assert [entry["round"] for entry in released] == [3], (
        f"the release must be audited; got {released!r}"
    )
    assert released[0]["reason"] == "base_drift_conflicts"


# ---------------------------------------------------------------------------
# Test 3 — the service preflight releases the round too
# ---------------------------------------------------------------------------


def test_service_preflight_releases_round(hermetic, monkeypatch):
    """A round blocked by the service preflight is released as well.

    Same shape as the base-drift block: the preflight refuses before the
    first VP is dispatched, the report is empty, and the round number
    must go back.
    """
    orch = _drive_blocked_round(
        hermetic, monkeypatch, SERVICE_PREFLIGHT_REPORT,
    )

    assert orch.rounds == [3], (
        "the round must have been dispatched (and blocked) — otherwise this "
        "test proves nothing about the release"
    )

    row = _verification_row()
    assert row is not None, "the round must have written the row"
    assert row["round"] == 2, (
        "a round whose services were unusable measured nothing — the "
        f"counter must stay at 2; got {row['round']!r}"
    )

    released = (row["executor_state"] or {}).get("released_rounds") or []
    assert [entry["round"] for entry in released] == [3]
    assert released[0]["reason"] == "service_preflight_failed"


# ---------------------------------------------------------------------------
# Test 4 — the terminal write does not cancel the release
# ---------------------------------------------------------------------------


def test_release_survives_terminal_write(hermetic, monkeypatch):
    """The release runs AFTER the terminal write, and stands.

    The blocked round still ends the plan, so ``_record_terminal`` runs
    and writes the verdict.  That write must not put the counter back:
    the release comes after it, so the counter ends at ``n - 1`` while
    the terminal verdict stands.
    """
    import server
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    order = []
    real_terminal = server._record_verification_terminal
    real_release = VerificationRepository.release_round

    def spy_terminal(plan_id, status, stop_reason, **kwargs):
        order.append(("terminal", status, stop_reason))
        return real_terminal(plan_id, status, stop_reason, **kwargs)

    def spy_release(self, plan_id, round_n, reason):
        order.append(("release", round_n, reason))
        return real_release(self, plan_id, round_n, reason)

    monkeypatch.setattr(server, "_record_verification_terminal", spy_terminal)
    monkeypatch.setattr(VerificationRepository, "release_round", spy_release)

    _drive_blocked_round(hermetic, monkeypatch, BASE_DRIFT_REPORT)

    kinds = [entry[0] for entry in order]
    assert kinds == ["terminal", "release"], (
        f"the release must follow the terminal write; got {order!r}"
    )

    row = _verification_row()
    assert row["round"] == 2, (
        "the terminal write must not put the counter back — the blocked "
        f"round 3 never measured anything; got {row['round']!r}"
    )
    assert row["verification_status"] == "loop_stopped", (
        "the terminal verdict must still stand — the release only gives "
        "the NUMBER back, it does not undo the plan's ending"
    )


# ---------------------------------------------------------------------------
# The boundary: a release that cannot land is a warning, never a crash
# ---------------------------------------------------------------------------


def test_release_without_a_row_is_a_warning_not_a_crash(hermetic, monkeypatch):
    """A plan without a row cannot be released — and must not raise.

    The release is called from the blocked-round path, right after the
    terminal write.  If it raised, the blocked round would blow up
    instead of ending the plan quietly, and the terminal verdict that
    was already written would be lost with the traceback.
    """
    import verification_loop

    assert verification_loop._release_unverified_round(
        "no-such-plan", 3, "base_drift_conflicts",
    ) is False


# ---------------------------------------------------------------------------
# The hand-off path: the conflict became a repair task
# ---------------------------------------------------------------------------


def test_base_drift_repair_handoff_releases_the_round(hermetic, monkeypatch):
    """A base-drift conflict that became a repair task releases the round.

    This path does not end the plan: the conflict became work, the
    executor runs the merge, and the merge comes back as the next round.
    The round still dispatched nothing, so its number must not be
    charged to the plan — the round the merge comes back to is computed
    without this one in it.
    """
    import server

    dispatched = []
    monkeypatch.setattr(
        server, "_run_repair_execution_async",
        lambda *a, **kw: dispatched.append((a, kw)),
    )

    _drive_blocked_round(hermetic, monkeypatch, BASE_DRIFT_REPAIR_REPORT)

    assert dispatched, (
        "the merge must have been handed to the executor — otherwise this "
        "test proves nothing about the release"
    )

    row = _verification_row()
    assert row is not None, "the round must have written the row"
    assert row["round"] == 2, (
        "the blocked round 3 dispatched no VP, so the counter must stay "
        f"at 2; got {row['round']!r}"
    )

    released = (row["executor_state"] or {}).get("released_rounds") or []
    assert [(entry["round"], entry["reason"]) for entry in released] == [
        (3, "base_drift_conflicts"),
    ], f"the release must be audited; got {released!r}"

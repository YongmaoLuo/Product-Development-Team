"""The plan summary's verification block is owned by ``plan_verification``.

Background
----------
``GET /api/plan/{id}/summary`` reported the plan's verification
sub-state by reading ``plan_routing.verification`` — a *mirror* column
that the verification loop happens to write on its way past.  The
authoritative record is ``plan_verification``, the table
``GET /api/verification/{id}/status`` reads.  The two have drifted: two
live plans reported a verdict the authoritative table contradicted, so
the summary and the status endpoint answered the same question two
different ways.

The summary's ``state.verification`` block is therefore built from
``plan_verification`` alone.  The keys do not change — ``status`` /
``round`` / ``max_rounds`` / ``stop_reason`` — so the consumers
(``feishu_notifier`` / ``cards``) need no code change; they just start
reading the authoritative value.

What this file pins:

1. ``test_summary_reports_plan_verification_row`` — a conflicting
   mirror column loses to the authoritative row.
2. ``test_summary_matches_status_endpoint`` — the summary and
   ``/api/verification/{id}/status`` agree on status / round /
   max_rounds.
3. ``test_summary_defaults_without_row`` — a plan that never ran a
   round reports the documented defaults.
4. ``test_summary_preserves_baseline`` — the baseline keys the other
   modules read survive the override.
5. ``test_summary_keeps_planstate_value_without_state_db`` — without a
   state database the summary keeps what ``PlanState`` says, and adds
   no key of its own.
6. ``test_summary_forces_running_during_verification_phase`` — a plan
   whose phase says "verification is running" IS running.
7. ``test_summary_keeps_unknown_status_verbatim`` — an unrecognised
   status is reported as-is.

Test command::

    pytest backend/tests/api/test_summary_verification_authority.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402


client = TestClient(server.app)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _plan_dir(
    plan_id: str,
    verification: Optional[Dict[str, Any]] = None,
) -> Path:
    """Create a minimal plan directory and return it.

    The summary reads the plan's own directory first, so it has to
    exist.  ``verification`` is written into the legacy
    ``plan_state.json`` — the only state a plan has before the
    state-machine database exists.
    """
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    state: Dict[str, Any] = {
        "plan_id": plan_id,
        "current_phase": "ready",
        "completed_phases": [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
    }
    if verification is not None:
        state["verification"] = verification
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    return plan_dir


def _stale_lifecycle(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The four lifecycle keys, as a stale writer left them in the column.

    The mirror column no longer carries the verification state; these
    cases exist to prove the reader does not pick the column up, so the
    keys have to be *in* it — written the way the old writer wrote them.
    """
    return {
        key: value
        for key, value in payload.items()
        if key not in ("baseline", "baseline_doc_sha256")
    }


def _seed_routing(
    plan_id: str,
    *,
    phase: str = "ready",
    verification: Optional[Dict[str, Any]] = None,
) -> None:
    """Write the plan's ``plan_routing`` row.

    The ``verification`` argument is the *mirror* column — the value
    the summary used to report.  Tests that need the mirror and the
    authoritative table to disagree write the wrong value here.

    The column keeps the baseline and nothing else; the four lifecycle
    keys only survive there as what a **stale writer** left behind, and
    the reader must not pick them up.  So they go in through raw SQL
    (the way the old writer wrote them) while the baseline goes through
    the column's only writer,
    ``RoutingRepository.write_verification_baseline``.
    """
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    db_path = server._state_db_path(None)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = RoutingRepository(conn)
        repo.write_plan_state(
            plan_id,
            phase=phase,
            completed_phases=[],
            review_rounds={"prd": 0, "arch": 0, "test": 0},
            flags={},
            last_updated="2026-11-04T00:00:00Z",
        )
        # 过期写者留在镜像列里的形态：生命周期四键不该被读侧采信，
        # 所以照旧写者的手笔用裸 SQL 写进去。基线另走列现在唯一的写者。
        conn.execute(
            "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
            (json.dumps(_stale_lifecycle(verification or {})), plan_id),
        )
        conn.commit()
        baseline = (verification or {}).get("baseline")
        if baseline is not None:
            repo.write_verification_baseline(
                plan_id,
                baseline,
                (verification or {}).get("baseline_doc_sha256", ""),
            )
    finally:
        conn.close()


def _seed_verification(
    plan_id: str,
    *,
    status: str = "passed",
    round_n: int = 2,
    max_rounds: int = 3,
    stop_reason: Optional[str] = None,
) -> None:
    """Write the authoritative ``plan_verification`` row."""
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    db_path = server._state_db_path(None)
    conn = open_db(db_path)
    try:
        migrate(conn)
        VerificationRepository(conn).insert(
            plan_id,
            verification_status=status,
            round=round_n,
            max_rounds=max_rounds,
            verification_stop_reason=stop_reason,
        )
    finally:
        conn.close()


def _summary_verification(plan_id: str) -> Dict[str, Any]:
    """The ``state.verification`` block of a plan summary."""
    resp = client.get(f"/api/plan/{plan_id}/summary")
    assert resp.status_code == 200, resp.text
    return resp.json()["state"]["verification"]


# ---------------------------------------------------------------------------
# 1. The authoritative row wins
# ---------------------------------------------------------------------------


def test_summary_reports_plan_verification_row() -> None:
    """The summary reports the ``plan_verification`` row, not the mirror.

    The mirror column (``plan_routing.verification``) is written on the
    way past by the verification loop; the row is the record.  When the
    two disagree, the row is what the plan actually did.
    """
    plan_id = "20261104-summary-authority"
    _plan_dir(plan_id)
    _seed_routing(
        plan_id,
        phase="ready",
        verification={"status": "failed", "round": 9},
    )
    _seed_verification(
        plan_id, status="passed", round_n=2, max_rounds=3,
    )

    block = _summary_verification(plan_id)

    assert block["status"] == "passed", (
        f"the summary must report the authoritative row; got {block!r}"
    )
    assert block["round"] == 2, (
        f"the summary must report the authoritative round; got {block!r}"
    )
    assert block["max_rounds"] == 3, (
        f"the summary must report the authoritative cap; got {block!r}"
    )
    assert block["stop_reason"] is None, (
        f"a row without a stop reason reports None; got {block!r}"
    )


# ---------------------------------------------------------------------------
# 2. The summary and the status endpoint agree
# ---------------------------------------------------------------------------


def test_summary_matches_status_endpoint() -> None:
    """Two endpoints, one question, one answer.

    ``/api/verification/{id}/status`` is the endpoint the operator
    watches.  If the summary answered differently, whichever one the
    reader happened to poll would be the one they believed — and the
    two would never agree.
    """
    plan_id = "20261104-summary-matches-status"
    _plan_dir(plan_id)
    _seed_routing(plan_id, phase="ready")
    _seed_verification(
        plan_id,
        status="failed",
        round_n=3,
        max_rounds=5,
        stop_reason="budget_exhausted",
    )

    status_resp = client.get(f"/api/verification/{plan_id}/status")
    assert status_resp.status_code == 200, status_resp.text
    status = status_resp.json()

    block = _summary_verification(plan_id)

    assert block["status"] == status["verification_status"], (
        f"status must match /status; summary {block!r} vs {status!r}"
    )
    assert block["round"] == status["verification_round"], (
        f"round must match /status; summary {block!r} vs {status!r}"
    )
    assert block["max_rounds"] == status["verification_max_rounds"], (
        f"max_rounds must match /status; summary {block!r} vs {status!r}"
    )


# ---------------------------------------------------------------------------
# 3. No row at all
# ---------------------------------------------------------------------------


def test_summary_defaults_without_row() -> None:
    """A plan that never ran a round reports the documented defaults.

    "No row" is an answer, not a missing key: a consumer must not have
    to tell "verification never started" apart from "the endpoint does
    not know about verification".
    """
    plan_id = "20261104-summary-no-row"
    _plan_dir(plan_id)
    _seed_routing(plan_id, phase="ready")

    block = _summary_verification(plan_id)

    assert block["status"] == "not_started", f"got {block!r}"
    assert block["round"] == 0, f"got {block!r}"
    assert block["max_rounds"] == server.DEFAULT_MAX_VERIFICATION_ROUNDS, (
        f"got {block!r}"
    )
    assert block["stop_reason"] is None, f"got {block!r}"


# ---------------------------------------------------------------------------
# 4. The baseline keys survive
# ---------------------------------------------------------------------------


def test_summary_preserves_baseline() -> None:
    """The frozen baseline is still in the block.

    ``baseline`` / ``baseline_doc_sha256`` are written by
    ``PlanState.set_verification_baseline`` and read by the modules that
    decide whether a plan may enter verification.  The override
    replaces the four lifecycle keys — it must not drop the baseline
    that was frozen alongside them.
    """
    plan_id = "20261104-summary-baseline"
    baseline = {
        "repo_dir": "/tmp/repo",
        "ref": "feature/x",
        "commit_sha": "abc123",
        "pr_number": 42,
    }
    _plan_dir(plan_id)
    _seed_routing(
        plan_id,
        phase="ready",
        verification={
            "status": "failed",
            "round": 9,
            "baseline": baseline,
            "baseline_doc_sha256": "sha-256-value",
        },
    )
    _seed_verification(plan_id, status="passed", round_n=2, max_rounds=3)

    block = _summary_verification(plan_id)

    assert block.get("baseline") == baseline, (
        f"the frozen baseline must survive; got {block!r}"
    )
    assert block.get("baseline_doc_sha256") == "sha-256-value", (
        f"the baseline's document hash must survive; got {block!r}"
    )
    assert block["status"] == "passed", (
        f"the lifecycle keys are still overridden; got {block!r}"
    )


# ---------------------------------------------------------------------------
# 5. No state database
# ---------------------------------------------------------------------------


def test_summary_keeps_planstate_value_without_state_db() -> None:
    """Without a state database, the summary keeps what ``PlanState`` says.

    The override is sourced from the state machine.  When the state
    machine is not there, there is nothing to override with — and the
    answer must stay the one the plan state already carries, with no
    key added and none dropped.
    """
    plan_id = "20261104-summary-no-state-db"
    _plan_dir(
        plan_id,
        verification={
            "status": "failed",
            "round": 7,
            "max_rounds": 5,
            "stop_reason": "manual",
        },
    )
    # The state database must not exist: that is what makes the
    # state-machine handle None.
    assert not server._state_db_path(None).exists()

    block = _summary_verification(plan_id)

    assert block == {
        "status": "failed",
        "round": 7,
        "max_rounds": 5,
        "stop_reason": "manual",
    }, f"the plan state's own value must survive untouched; got {block!r}"


# ---------------------------------------------------------------------------
# 6. A running round
# ---------------------------------------------------------------------------


def test_summary_forces_running_during_verification_phase() -> None:
    """A plan whose phase says "verification is running" IS running.

    The round row is stamped when the round ends; the *phase* is what
    says a round is in flight.  A plan between the two must read as
    running — the same rule ``/api/verification/{id}/status`` applies.
    """
    plan_id = "20261104-summary-running"
    _plan_dir(plan_id)
    _seed_routing(
        plan_id,
        phase="verification_running",
        verification={"status": "not_started", "round": 0},
    )
    _seed_verification(plan_id, status="not_started", round_n=0, max_rounds=4)

    block = _summary_verification(plan_id)

    assert block["status"] == "running", (
        f"a plan in the verification_running phase must read as running; "
        f"got {block!r}"
    )


# ---------------------------------------------------------------------------
# 7. An unknown status
# ---------------------------------------------------------------------------


def test_summary_keeps_unknown_status_verbatim() -> None:
    """An unrecognised status is reported as-is.

    The normalization table maps the two historical spellings onto the
    public set.  A value that is in neither table is a value this code
    does not understand — reporting it verbatim is what lets a human
    see that, instead of silently claiming the plan never started.
    """
    plan_id = "20261104-summary-unknown-status"
    _plan_dir(plan_id)
    _seed_routing(plan_id, phase="ready")
    _seed_verification(plan_id, status="quantum_flux", round_n=1, max_rounds=4)

    block = _summary_verification(plan_id)

    assert block["status"] == "quantum_flux", (
        f"an unknown status must be reported verbatim; got {block!r}"
    )


def test_summary_normalizes_legacy_status() -> None:
    """The two historical spellings are mapped onto the public set."""
    plan_id = "20261104-summary-legacy-status"
    _plan_dir(plan_id)
    _seed_routing(plan_id, phase="ready")
    _seed_verification(plan_id, status="verification_passed", round_n=1)

    block = _summary_verification(plan_id)

    assert block["status"] == "passed", f"got {block!r}"

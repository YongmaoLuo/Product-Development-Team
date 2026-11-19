"""``/api/verification/{id}/progress`` answers from the authoritative row.

Background
----------
``GET /api/verification/{id}/progress`` reported the round's status /
round / stop reason from ``plan_routing.verification`` — a *mirror*
column the verification loop used to write on its way past — whenever
its in-memory dict did not already carry a value.  The authoritative
record is ``plan_verification``, the table
``GET /api/verification/{id}/status`` reads.  The two have drifted:
the mirror stopped receiving writes (2026-11-04), so the fallback could
only answer with a stale value — a plan whose authoritative row said
``passed / round 2`` reported whatever the mirror last held.

The three lifecycle fields therefore come from ``plan_verification``
alone.  The rest of the payload — the VP lists, the counts, the round
activity, ``current_vp`` — is untouched: those never came from the
mirror in the first place.

What this file pins:

1. ``test_progress_reads_verification_row`` — a conflicting mirror
   column loses to the authoritative row.
2. ``test_progress_ignores_routing_mirror`` — rewriting the mirror
   column with raw SQL changes nothing the endpoint reports.
3. ``test_progress_forces_running_during_verification_phase`` — a plan
   whose phase says "verification is running" IS running, the same
   rule ``/api/verification/{id}/status`` applies.

Test command::

    pytest backend/tests/api/test_verification_progress_authority.py -v
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


def _plan_dir(plan_id: str) -> Path:
    """Create a minimal plan directory and return it.

    The endpoint reads the plan's own directory for the VP list, so it
    has to exist.  The plan id is the directory name — that is how
    ``PlanState`` finds the plan's ``plan_routing`` row.
    """
    plan_dir = server.PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "verification_points": [
                    {
                        "id": "VP-001",
                        "title": "Login API contract",
                        "verification_method": "automated_test",
                    },
                ],
            }
        ),
        encoding="utf-8",
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
    the endpoint used to report.  Tests that need the mirror and the
    authoritative table to disagree write the wrong value here.
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
    """Write the authoritative ``plan_verification`` row.

    ``progress_state`` is what the endpoint reads to decide that
    verification has started at all — without it the request answers
    ``404 {"error": "verification not started"}`` before any of the
    lifecycle fields are assembled.
    """
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
            status,
            round=round_n,
            max_rounds=max_rounds,
            verification_stop_reason=stop_reason,
            progress_state={
                "plan_id": plan_id,
                "current_vp": None,
                "current_layer": None,
                "completed_vps": ["VP-001"],
                "failed_vps": [],
                "skipped_vps": [],
                "layer_summaries": {},
                "updated_at": "2026-11-04T00:00:00.000000Z",
            },
        )
    finally:
        conn.close()


def _write_mirror_column(plan_id: str, mirror: Dict[str, Any]) -> None:
    """Overwrite the ``plan_routing.verification`` mirror with raw SQL.

    Raw SQL on purpose: the repository layer is not the thing under
    test here — the *reader* is.  Writing the column the way the
    verification loop used to (and the way a stale row still carries
    it) is what the endpoint must not react to.
    """
    from state_machine.db.connection import open as open_db

    conn = open_db(server._state_db_path(None))
    try:
        conn.execute(
            "UPDATE plan_routing SET verification = ? WHERE plan_id = ?",
            (json.dumps(mirror), plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def _progress_fields(plan_id: str) -> Dict[str, Any]:
    """The three lifecycle fields of a ``/progress`` response."""
    resp = client.get(f"/api/verification/{plan_id}/progress")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {
        "verification_status": body["verification_status"],
        "verification_round": body["verification_round"],
        "stop_reason": body["stop_reason"],
    }


# ---------------------------------------------------------------------------
# 1. The authoritative row wins
# ---------------------------------------------------------------------------


def test_progress_reads_verification_row() -> None:
    """The endpoint reports the ``plan_verification`` row, not the mirror.

    The mirror column is what the endpoint used to answer with; the row
    is what the plan actually did.  When the two disagree, the row is
    the answer — otherwise the progress endpoint reports a verdict the
    plan's own record contradicts.
    """
    plan_id = "20261104-progress-authority"
    _plan_dir(plan_id)
    _seed_routing(
        plan_id,
        phase="ready",
        verification={"status": "failed", "round": 9, "stop_reason": "stale"},
    )
    _seed_verification(
        plan_id, status="passed", round_n=2, max_rounds=3, stop_reason=None,
    )

    fields = _progress_fields(plan_id)

    assert fields == {
        "verification_status": "passed",
        "verification_round": 2,
        "stop_reason": None,
    }, f"the authoritative row must win; got {fields!r}"


# ---------------------------------------------------------------------------
# 2. The mirror is not a reader's source
# ---------------------------------------------------------------------------


def test_progress_ignores_routing_mirror() -> None:
    """Rewriting the mirror column changes nothing the endpoint reports.

    The mirror is written on the way past by other writers; it is not
    the plan's record.  A mirror that moves — as it does when a stale
    round writes it — must not move the progress endpoint's answer.
    """
    plan_id = "20261104-progress-mirror-ignored"
    _plan_dir(plan_id)
    _seed_routing(
        plan_id,
        phase="ready",
        verification={"status": "failed", "round": 9},
    )
    _seed_verification(
        plan_id, status="passed", round_n=2, max_rounds=3, stop_reason=None,
    )

    before = _progress_fields(plan_id)

    _write_mirror_column(
        plan_id,
        {"status": "failed", "round": 9, "stop_reason": "manual", "max_rounds": 3},
    )

    after = _progress_fields(plan_id)

    assert after == before, (
        f"the mirror column must not be a source; {before!r} -> {after!r}"
    )


# ---------------------------------------------------------------------------
# 3. A running round
# ---------------------------------------------------------------------------


def test_progress_forces_running_during_verification_phase() -> None:
    """A plan whose phase says "verification is running" IS running.

    The round row is stamped when the round *ends*; the phase is what
    says a round is in flight.  A plan between the two must read as
    running — the same rule ``/api/verification/{id}/status`` applies,
    and the same rule the plan summary applies.  Without it the
    progress endpoint would report "not_started" for a plan that is
    demonstrably mid-round.
    """
    plan_id = "20261104-progress-running"
    _plan_dir(plan_id)
    _seed_routing(
        plan_id,
        phase="verification_running",
        verification={"status": "not_started", "round": 0},
    )
    _seed_verification(
        plan_id, status="not_started", round_n=0, max_rounds=4, stop_reason=None,
    )

    fields = _progress_fields(plan_id)

    assert fields["verification_status"] == "running", (
        f"a plan in the verification_running phase must read as running; "
        f"got {fields!r}"
    )

"""
TDD tests for the new ``GET /api/verification/{plan_id}/progress`` endpoint.

Background
----------
The dashboard (and the Feishu bridge in particular) needs
VP-granularity + layer-granularity verification state. The
existing ``/api/verification/{plan_id}/status`` endpoint only
returns the per-cycle summary (``verification_status`` /
``verification_round`` / ``results``), so the bridge has been
falling back to a coarse "is it running?" view.

The new ``/progress`` endpoint reads the executor's on-disk
progress state file (``verification_progress_state.json``, written
on every ``vp_status_changed`` event) and returns:

  * ``current_vp`` — the VP currently in flight, with its title /
    layer / start timestamp (start is recovered from the JSON-lines
    log file).
  * ``completed_vps`` / ``failed_vps`` / ``skipped_vps`` /
    ``pending_vps`` — the four VP index lists.
  * ``current_layer`` + ``layer_summaries`` — the per-layer rollup
    (total / completed / failed / skipped).
  * ``counts`` — the dashboard-friendly rollup: ``in_progress`` is
    exactly 1 if and only if ``current_vp`` is set, the other
    counts are the lengths of the index lists.

Boundary conditions pinned by the three tests below:

  1. ``test_progress_returns_schema`` — the response is the
     documented schema when the executor has written a complete
     progress state file (round 1, mid-flight on VP-002). This is
     the happy path the Feishu bridge will hit on every poll.
  2. ``test_progress_404_when_no_state`` — 404 when the progress
     file is missing. The dashboard uses 404 to decide whether to
     fall back to ``/status``.
  3. ``test_progress_counts_match_lists`` — ``counts`` are derived
     from the index lists (not a stale snapshot), and
     ``in_progress`` is exactly 1 iff ``current_vp`` is set.

The tests are pure unit tests: they use FastAPI's ``TestClient``
against the real ``server.app`` instance and write real
``verification_progress_state.json`` + ``verification_plan.json``
files under the per-test ``tmp_path`` (the
``isolated_plans_dir`` autouse fixture in ``tests/conftest.py``
already redirects ``PLANS_DIR`` to ``tmp_path / "plans"``).
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Ensure ``backend/`` is on ``sys.path`` so ``import server`` works
# regardless of which test runner entry point is used. Mirrors the
# pattern in ``test_verification_executor.py``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from fastapi.testclient import TestClient  # noqa: E402

from server import app  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """A FastAPI TestClient bound to the real ``server.app``.

    The ``isolated_plans_dir`` autouse fixture in
    ``tests/conftest.py`` already redirects ``PLANS_DIR`` to a
    per-test ``tmp_path / "plans"`` so the tests don't leak state
    onto the developer's real plans directory.

    The /progress endpoint reads from the state-machine SQLite row
    (plan_verification.progress_state) via the VerificationRepository.
    The framework resolves the db path via ``_state_db_path`` which
    defaults to ``<PLANS_DIR.parent> / state.db`` (i.e. the
    development ``backend/state.db``). We redirect that to a per-test
    ``tmp_path / state.db`` so the test does not depend on the
    developer's real db.
    """
    import server
    server._state_db_path = lambda request=None: tmp_path / "state.db"  # noqa: F841
    return TestClient(app)


def _write_plan(
    plan_dir: Path,
    *,
    plan_id: str,
    vps: List[Dict[str, Any]],
) -> None:
    """Write a minimal ``verification_plan.json`` to ``plan_dir``.

    Uses the canonical envelope key ``verification_points`` (the
    shape :class:`VerificationAgent` writes on disk). The endpoint
    also accepts ``vps`` for plans from the executor pipeline, but
    the agent-shaped envelope is the more common one in fixtures.
    """
    plan_file = plan_dir / "verification_plan.json"
    plan_file.write_text(
        json.dumps(
            {"plan_id": plan_id, "verification_points": vps},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_progress(
    plan_dir: Path,
    *,
    plan_id: str,
    current_vp: Any = None,
    current_layer: Any = None,
    completed_vps: List[str] = (),
    failed_vps: List[str] = (),
    skipped_vps: List[str] = (),
    layer_summaries: Dict[str, Dict[str, int]] = None,
    updated_at: str = "2026-06-07T10:05:00.000000Z",
) -> Path:
    """Write a complete ``verification_progress_state.json`` to ``plan_dir``.

    Mirrors the on-disk layout :class:`VerificationExecutor`
    produces — every field the endpoint reads is set explicitly so
    the tests pin the full schema.
    """
    progress_payload = {
        "plan_id": plan_id,
        "current_vp": current_vp,
        "current_layer": current_layer,
        "completed_vps": list(completed_vps),
        "failed_vps": list(failed_vps),
        "skipped_vps": list(skipped_vps),
        "layer_summaries": dict(layer_summaries) if layer_summaries is not None else {},
        "updated_at": updated_at,
    }
    # The /progress endpoint reads from the state-machine SQLite row
    # (plan_verification.progress_state) via the VerificationRepository.
    # The fixture (client) redirects ``_state_db_path`` to a per-test
    # db path. We MUST write to that exact path — the endpoint's
    # state-machine helper opens the same db_path when serving the
    # GET request.
    # The helper is idempotent: ``insert_or_replace`` semantics so
    # multiple helpers in the same test don't trip the UNIQUE
    # constraint on plan_id.
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    import server
    db_path = server._state_db_path(None)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    migrate(conn)
    # Idempotent insert: if a row already exists for this plan_id,
    # delete and reinsert so the progress_state reflects the latest
    # call. This mirrors how the live executor overwrites progress
    # state on every status change.
    repo = VerificationRepository(conn)
    existing = repo.summary(plan_id)
    if existing is not None:
        # Use a quick UPDATE on the progress_state column instead of
        # delete+insert so the row's started_at is preserved across
        # helper invocations within the same test.
        repo._conn.execute(
            "UPDATE plan_verification SET progress_state = ? WHERE plan_id = ?",
            (repo._encode_fields({"progress_state": progress_payload})["progress_state"], plan_id),
        )
        repo._conn.commit()
    else:
        repo.insert(
            plan_id,
            verification_status="running",
            progress_state=progress_payload,
        )
    conn.close()
    return db_path


def _write_vp_start_log(
    plan_dir: Path,
    *,
    vp_id: str,
    round_number: int = 1,
    timestamp: str = "2026-06-07T10:04:30.000000Z",
) -> Path:
    """Write a JSON-lines log file containing one ``vp_start`` event.

    Used by the ``started_at`` recovery path: the endpoint scans
    the round log files for the most recent ``vp_start`` event
    whose ``verification_point_id`` matches the in-flight VP.

    2026-09-14: the event-name key is ``event_type``, matching
    ``VerificationPersistenceManager.write_verification_point_log``
    (``verification_persistence.py`` writes
    ``{"verification_point_id", "event_type", "timestamp", "data"}``).
    This fixture used to emit the legacy ``"event"`` key, so
    ``server._read_latest_vp_start`` / ``_read_all_vp_starts`` — which
    read ``event_type`` since the 2026-08-25 audit — matched nothing
    and ``current_vp.started_at`` came back ``None``.
    """
    logs_dir = plan_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"verification_{round_number}_20260607_100430.log"
    entry = {
        "event_type": "vp_start",
        "verification_point_id": vp_id,
        "round": round_number,
        "timestamp": timestamp,
        "data": {"title": "Login API contract", "method": "automated_test"},
    }
    log_file.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
    return log_file


def _append_vp_log_event(
    plan_dir: Path,
    *,
    vp_id: str,
    event_type: str,
    round_number: int = 1,
    timestamp: str = "2026-06-07T10:09:30.000000Z",
) -> Path:
    """Append one event to the round log written by
    :func:`_write_vp_start_log` (same file naming scheme).

    2026-09-14: the progress endpoint treats the NEWEST round log as
    live truth — a ``vp_start`` without a subsequent ``vp_complete``
    means the VP is mid-flight regardless of the persisted verdict
    lists. Tests modelling "VP finished" must therefore append the
    matching ``vp_complete`` event, not just rewrite the progress
    state.
    """
    logs_dir = plan_dir / "logs"
    log_file = logs_dir / f"verification_{round_number}_20260607_100430.log"
    entry = {
        "event_type": event_type,
        "verification_point_id": vp_id,
        "round": round_number,
        "timestamp": timestamp,
        "data": {},
    }
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_file


def _write_plan_state(
    plan_dir: Path,
    *,
    plan_id: str,
    current_phase: str = "verification_running",
    verification_status: str = "running",
    verification_round: int = 1,
) -> None:
    """Write a minimal ``plan_state.json`` to ``plan_dir``.

    The ``/progress`` endpoint falls back to this file for
    ``verification_status`` / ``verification_round`` when the
    in-memory ``_verification_state`` dict is empty (e.g. after a
    server restart). The endpoint should still return the right
    answer in that case.
    """
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": current_phase,
                "completed_phases": [],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {},
                "verification": {
                    "status": verification_status,
                    "round": verification_round,
                    "max_rounds": 3,
                    "stop_reason": None,
                },
                "last_updated": "2026-06-07T10:05:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Test 1: /progress returns the documented schema
# ---------------------------------------------------------------------------


def test_progress_returns_schema(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
    tmp_path: Path,
) -> None:
    """Round 1, mid-flight on ``VP-002``, with one completed VP
    (``VP-001``) and one pending VP (``VP-003``). The endpoint must
    return the full documented schema with the right values for
    every field.

    Specifically:

      * ``plan_id`` / ``verification_status`` / ``verification_round``
        are read from the in-memory ``_verification_state`` dict
        (or the ``plan_state.json`` fallback).
      * ``current_vp`` is a dict with the in-flight VP's id /
        title / layer / started_at (started_at recovered from the
        JSON-lines log).
      * ``completed_vps`` is ``["VP-001"]``; ``pending_vps`` is
        ``["VP-003"]`` (the only VP in the plan that isn't in a
        terminal list and isn't the in-flight VP).
      * ``current_layer`` is the executor-recorded layer of the
        in-flight VP (``"L2"``).
      * ``layer_summaries`` is keyed by ``"L1"`` / ``"L2"`` /
        ``"L3"`` in canonical order, with the executor's counters
        copied verbatim and missing layers populated as zeros.
      * ``counts.in_progress`` is exactly ``1``.
      * ``last_updated_at`` is the executor-written timestamp.
    """
    plan_id = "20260607-progress-schema"
    plan_dir = isolated_plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    # 3-VP plan covering each layer: VP-001 / L1, VP-002 / L2,
    # VP-003 / L3. ``VP-002`` is the in-flight one.
    _write_plan(
        plan_dir,
        plan_id=plan_id,
        vps=[
            {
                "id": "VP-001",
                "title": "Login API contract",
                "verification_method": "automated_test",
                "layer": 1,
            },
            {
                "id": "VP-002",
                "title": "Login form on mobile",
                "verification_method": "ui_validation",
                "layer": 2,
            },
            {
                "id": "VP-003",
                "title": "Login analytics funnel",
                "verification_method": "code_review",
                "layer": 3,
            },
        ],
    )
    _write_progress(
        plan_dir,
        plan_id=plan_id,
        current_vp="VP-002",
        current_layer="L2",
        completed_vps=["VP-001"],
        failed_vps=[],
        skipped_vps=[],
        layer_summaries={
            "L1": {"total": 1, "completed": 1, "failed": 0, "skipped": 0},
            "L2": {"total": 1, "completed": 0, "failed": 0, "skipped": 0},
            "L3": {"total": 1, "completed": 0, "failed": 0, "skipped": 0},
        },
    )
    _write_vp_start_log(
        plan_dir,
        vp_id="VP-002",
        round_number=1,
        timestamp="2026-06-07T10:04:30.000000Z",
    )
    _write_plan_state(
        plan_dir,
        plan_id=plan_id,
        current_phase="verification_running",
        verification_status="running",
        verification_round=1,
    )

    # The /progress endpoint falls back to plan_state.json when the
    # in-memory _verification_state dict is empty (which it is in
    # unit tests — no live orchestrator). Seed the plan_state.json
    # so the response picks up the right status / round.
    resp = client.get(f"/api/verification/{plan_id}/progress")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["plan_id"] == plan_id
    assert body["verification_status"] == "running"
    assert body["verification_round"] == 1

    # current_vp is a dict with the in-flight VP's id / title /
    # started_at. (The ``layer`` field was removed 2026-06-13.)
    cv = body["current_vp"]
    assert cv is not None, "current_vp must be non-null while a VP is in flight"
    assert cv["id"] == "VP-002"
    assert cv["title"] == "Login form on mobile"
    assert "layer" not in cv, (
        "current_vp.layer was removed with the L1/L2/L3 layer concept"
    )
    assert cv["started_at"] == "2026-06-07T10:04:30.000000Z"

    # Index lists: one completed, zero failed / skipped, the
    # remaining plan VP (VP-003) is pending.
    assert body["completed_vps"] == ["VP-001"]
    assert body["failed_vps"] == []
    assert body["skipped_vps"] == []
    assert body["pending_vps"] == ["VP-003"]

    # layer_summaries / current_layer are kept in the response
    # shape for backward compat with older API consumers, but
    # always empty / None (the layer concept was removed
    # 2026-06-13).
    assert body["layer_summaries"] == {}
    assert body["current_layer"] is None

    # counts.in_progress is exactly 1 while a VP is in flight.
    assert body["counts"] == {
        "total": 3,
        "completed": 1,
        "failed": 0,
        "skipped": 0,
        "in_progress": 1,
        "pending": 1,
    }
    assert body["last_updated_at"] == "2026-06-07T10:05:00.000000Z"


# ---------------------------------------------------------------------------
# Test 2: /progress returns 404 when the progress state file is missing
# ---------------------------------------------------------------------------


def test_progress_404_when_no_state(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
    tmp_path: Path,
) -> None:
    """When ``verification_progress_state.json`` is missing under
    the plan directory (verification hasn't started, or the run is
    so early the first ``vp_status_changed`` event hasn't fired),
    the endpoint returns ``404`` with a friendly error body.

    The dashboard uses 404 to decide whether to fall back to
    ``/status`` (which always answers, even when verification
    hasn't started). Returning 200 with a synthetic "no VPs yet"
    payload would obscure the difference between "verification is
    mid-flight on VP-002" and "verification never started", so
    404 is the right status code here.
    """
    plan_id = "20260607-progress-no-state"
    plan_dir = isolated_plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    # The plan directory exists (otherwise the endpoint returns
    # "Plan not found", which is a different 404). The progress
    # state file is intentionally NOT written.
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": "executing",
                "completed_phases": [],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {},
                "verification": {"status": "pending", "round": 0, "max_rounds": 3, "stop_reason": None},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    resp = client.get(f"/api/verification/{plan_id}/progress")
    assert resp.status_code == 404, resp.text

    body = resp.json()
    # FastAPI HTTPException serialises the ``detail`` field as
    # whatever the user passed (here a string). The dashboard
    # keys on the error string being non-empty + status==404.
    err = (body.get("error") or body.get("detail") or "").lower()
    assert "verification" in err or "not started" in err, (
        f"expected 404 body to mention verification / not started, got {body!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: counts is derived from the index lists (in_progress == 1 iff current_vp set)
# ---------------------------------------------------------------------------


def test_progress_counts_match_lists(
    client: TestClient,
    isolated_plans_dir,  # noqa: F841  — autouse fixture; doc reference
    tmp_path: Path,
) -> None:
    """``counts`` must be derived from the index lists, not a stale
    snapshot. Specifically:

      * ``counts.total``     = ``len(plan)``
      * ``counts.completed`` = ``len(completed_vps)``
      * ``counts.failed``    = ``len(failed_vps)``
      * ``counts.skipped``   = ``len(skipped_vps)``
      * ``counts.pending``   = ``len(pending_vps)``
      * ``counts.in_progress = 1 if current_vp else 0``

    This contract is the one the Feishu bridge relies on to
    render the live progress bar — if the counts drift from the
    lists, the bar shows a non-integer percentage.

    The test covers both branches of the in_progress condition:

      * Phase A — one in-flight VP (``current_vp`` is set): the
        endpoint should return ``in_progress=1``, and the in-flight
        VP must NOT appear in ``pending_vps``.
      * Phase B — no in-flight VP (``current_vp`` is None): the
        endpoint should return ``in_progress=0`` and the plan's
        remaining VPs should all appear in ``pending_vps``.
    """
    plan_id = "20260607-progress-counts"
    plan_dir = isolated_plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)

    # 4-VP plan: VP-001..VP-004.  ``VP-003`` is set to in-flight
    # in Phase A and to None in Phase B.
    _write_plan(
        plan_dir,
        plan_id=plan_id,
        vps=[
            {"id": "VP-001", "title": "VP-001", "layer": 1},
            {"id": "VP-002", "title": "VP-002", "layer": 1},
            {"id": "VP-003", "title": "VP-003", "layer": 2},
            {"id": "VP-004", "title": "VP-004", "layer": 2},
        ],
    )
    _write_plan_state(
        plan_dir,
        plan_id=plan_id,
        verification_status="running",
        verification_round=1,
    )

    # ---- Phase A: one in-flight VP (current_vp="VP-003") ----
    _write_progress(
        plan_dir,
        plan_id=plan_id,
        current_vp="VP-003",
        current_layer="L2",
        completed_vps=["VP-001"],
        failed_vps=["VP-002"],
        skipped_vps=[],
        layer_summaries={
            "L1": {"total": 2, "completed": 1, "failed": 1, "skipped": 0},
            "L2": {"total": 2, "completed": 0, "failed": 0, "skipped": 0},
        },
    )
    _write_vp_start_log(plan_dir, vp_id="VP-003", round_number=1)

    resp_a = client.get(f"/api/verification/{plan_id}/progress")
    assert resp_a.status_code == 200, resp_a.text
    body_a = resp_a.json()

    # The in-flight VP is in current_vp, NOT in pending_vps.
    assert body_a["current_vp"] is not None
    assert body_a["current_vp"]["id"] == "VP-003"
    assert "VP-003" not in body_a["pending_vps"]

    # Counts derived from the index lists:
    #   total=4, completed=1, failed=1, skipped=0, pending=1 (VP-004),
    #   in_progress=1 (VP-003 is in flight).
    assert body_a["counts"] == {
        "total": 4,
        "completed": 1,
        "failed": 1,
        "skipped": 0,
        "in_progress": 1,
        "pending": 1,
    }
    # Belt-and-suspenders: each list length matches the count.
    assert len(body_a["completed_vps"]) == body_a["counts"]["completed"]
    assert len(body_a["failed_vps"]) == body_a["counts"]["failed"]
    assert len(body_a["skipped_vps"]) == body_a["counts"]["skipped"]
    assert len(body_a["pending_vps"]) == body_a["counts"]["pending"]
    # And the sum of completed + failed + skipped + pending + in_progress
    # equals total (every VP in the plan is accounted for).
    sum_indices = (
        body_a["counts"]["completed"]
        + body_a["counts"]["failed"]
        + body_a["counts"]["skipped"]
        + body_a["counts"]["pending"]
        + body_a["counts"]["in_progress"]
    )
    assert sum_indices == body_a["counts"]["total"]

    # ---- Phase B: no in-flight VP (current_vp=None) ----
    # Re-write the progress file with no in-flight VP. The
    # executor's ``current_vp`` is None between rounds (or after
    # a layer boundary short-circuit completes), so the bridge
    # needs to render a "0 in_progress" state correctly.
    _write_progress(
        plan_dir,
        plan_id=plan_id,
        current_vp=None,
        current_layer=None,
        completed_vps=["VP-001", "VP-003"],
        failed_vps=["VP-002"],
        skipped_vps=[],
        layer_summaries={
            "L1": {"total": 2, "completed": 1, "failed": 1, "skipped": 0},
            "L2": {"total": 2, "completed": 1, "failed": 0, "skipped": 0},
        },
    )
    # 2026-09-14: the newest round log is live truth — VP-003 finished,
    # so its vp_complete must land in the log. Without it the endpoint
    # (correctly, per the current-VP fix) reports VP-003 as mid-flight.
    _append_vp_log_event(
        plan_dir, vp_id="VP-003", event_type="vp_complete",
    )

    resp_b = client.get(f"/api/verification/{plan_id}/progress")
    assert resp_b.status_code == 200, resp_b.text
    body_b = resp_b.json()

    # current_vp is None → current_layer should also be None.
    assert body_b["current_vp"] is None
    assert body_b["current_layer"] is None

    # All plan VPs are accounted for: 2 completed, 1 failed,
    # 0 skipped, 1 pending (VP-004), 0 in_progress. The pending
    # VP list is the lone plan VP not in a terminal list.
    assert body_b["pending_vps"] == ["VP-004"]
    assert body_b["counts"] == {
        "total": 4,
        "completed": 2,
        "failed": 1,
        "skipped": 0,
        "in_progress": 0,
        "pending": 1,
    }
    # And again, sum of index lengths + in_progress == total.
    sum_indices_b = (
        body_b["counts"]["completed"]
        + body_b["counts"]["failed"]
        + body_b["counts"]["skipped"]
        + body_b["counts"]["pending"]
        + body_b["counts"]["in_progress"]
    )
    assert sum_indices_b == body_b["counts"]["total"]

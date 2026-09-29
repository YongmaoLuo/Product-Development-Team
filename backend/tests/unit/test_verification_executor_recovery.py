"""
TDD tests for ``VerificationExecutor`` crash-recovery semantics.

Background
----------
The executor persists two on-disk artifacts per plan:

  * ``verification_executor_state.json``  — verdict map keyed by VP id.
  * ``verification_progress_state.json`` — scheduler view
    (current_vp / current_layer / completed_vps / layer_summaries).

A process crash (kill -9, OOM, restart of the backend server, etc.) can
leave these two artifacts in an **inconsistent** state:

  * The verdict map says the VP is still pending
    (no entry for it in ``verdicts``).
  * The progress state file says the VP is currently being
    executed (``current_vp = "VP-007"``).
  * The per-VP verdict file
    ``vps/{vp_id}/verdict.json`` (written by the sub-agent) does
    **not** exist on disk.

A naive resume would re-execute the in-flight VP.  But the original
subprocess is gone — re-executing it would silently double-count the
VP (one verdict for the original subprocess that crashed, one for
the new run).  The contract pinned by these four tests is:

  1. **terminal** (PASSED / FAILED / SKIPPED) → skip, do not
     re-execute (the verdict map already says "done").
  2. **in-flight** (``current_vp`` set, no ``verdict.json`` on
     disk) → force mark as FAILED with the marker reason
     ``"recovered_from_crash"``.  The crashed subprocess is
     unrecoverable; the recovered verdict triggers the repair
     loop in the next round.
  3. **pending** (``current_vp`` is None, no verdict) → continue
     as planned; the resume picks up exactly where it left off.
  4. **no state file at all** → fresh init from the plan; all VPs
     start in ``pending_vps`` and ``current_vp`` is None.

The four contract pins
----------------------
  1. ``test_recover_terminal_vp_skipped`` — a verdict map that
     already contains terminal verdicts for 2 VPs is loaded
     verbatim; only the remaining pending VPs appear in
     ``pending_vps``; ``current_vp`` stays None.
  2. ``test_recover_inflight_marked_failed`` — a progress state
     file with ``current_vp = "VP-007"`` and no corresponding
     ``vps/VP-007/verdict.json`` on disk → after ``__init__`` the
     executor's ``failed_vps`` contains "VP-007", the verdict
     map has a FAILED entry for "VP-007" with reason
     ``"recovered_from_crash"``, ``current_vp`` is reset to
     ``None``, and the verdict is also persisted to the
     on-disk state file so a subsequent restart sees the same
     picture.
  3. ``test_recover_pending_continues`` — a progress state file
     with ``current_vp = None`` and a verdict map with 1 of 3 VPs
     already terminal → after ``__init__`` the executor's
     ``pending_vps`` is the remaining 2 ids (the in-flight
     detection does NOT fire); the on-disk state is unchanged
     for the already-terminal VP.
  4. ``test_no_state_file_full_init`` — neither the verdict map
     nor the progress state file exists → after ``__init__`` all
     3 VPs from the plan are in ``pending_vps`` and
     ``current_vp`` is None; nothing has been written to disk
     by the constructor.

These tests do NOT invoke ``run()`` (the recovery decision is
made in the constructor / state-loading path).  They construct a
``VerificationExecutor`` against a pre-populated ``plan_dir`` and
inspect the resulting in-memory state plus the on-disk artifacts
written by the recovery path.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock

import pytest


# Ensure ``backend/`` is on ``sys.path`` so
# ``import verification_executor`` works regardless of the test
# runner entry point.  Mirrors the pattern used by
# ``test_verification_executor.py``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from verification_executor import (  # noqa: E402
    DEFAULT_STATE_FILENAME,
    PROGRESS_STATE_FILENAME,
    VerificationExecutor,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_runner() -> Mock:
    """A mock sub-agent runner (not invoked by recovery tests, but
    the executor's constructor still requires one)."""
    return Mock()


@pytest.fixture
def tmp_plan_dir(tmp_path: Path) -> Path:
    """A fresh, empty plan directory.  Recovery tests pre-populate
    it with the state file and progress state file before
    constructing the executor."""
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def recovery_plan() -> Dict[str, Any]:
    """A 3-VP plan covering 2 layers, used by all four recovery
    tests.  The exact id set is not important beyond being
    distinct and stable — the tests verify id-by-id behavior."""
    return {
        "vps": [
            {
                "id": "VP-001",
                "layer": "L1",
                "method": "automated_test",
                "title": "Login API contract",
            },
            {
                "id": "VP-002",
                "layer": "L1",
                "method": "code_review",
                "title": "Login password hashing",
            },
            {
                "id": "VP-003",
                "layer": "L2",
                "method": "ui_validation",
                "title": "Login form on mobile",
            },
        ]
    }


def _make_executor(
    verification_plan: Dict[str, Any],
    plan_dir: Path,
    mock_runner: Mock,
    plan_id: str = "20260607-recovery",
    db_path: Path = None,
) -> VerificationExecutor:
    """Helper to construct a ``VerificationExecutor`` with the test
    fixtures.  Mirrors the helper in
    ``test_verification_executor.py``.

    Wires the executor to a state-machine ``VerificationRepository``
    pointed at the same SQLite db the helpers write to, so the
    executor's read path and the helpers' write path see the same
    data. Without ``verif_repo`` the executor falls back to the
    legacy ``verification_executor_state.json`` disk file (which
    the helpers no longer write), so the test would see an empty
    executor state and fail.
    """
    import server
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    if db_path is None:
        db_path = server._state_db_path(None)
    conn = open_db(db_path)
    verif_repo = VerificationRepository(conn)
    return VerificationExecutor(
        verification_plan=verification_plan,
        plan_id=plan_id,
        plan_dir=plan_dir,
        sub_agent_runner=mock_runner,
        verif_repo=verif_repo,
    )


def _write_state_file(
    plan_dir: Path,
    *,
    plan_id: str = "20260607-recovery",
    pending_vps: List[str],
    verdicts: Dict[str, Dict[str, Any]],
    db_path: Path = None,
) -> Path:
    """Write the verdict-map state to the state-machine SQLite row
    (via the VerificationRepository). The on-disk layout is the
    plan_verification table's ``runtime_state`` / ``verdicts``
    columns; the executor reads from the same repo at recover
    time.

    ``db_path`` defaults to the path the live executor uses in
    production (``<PLANS_DIR.parent> / state.db``). Tests that
    isolate the executor's state-machine db should pass an
    explicit ``db_path``.
    """
    import server
    if db_path is None:
        db_path = server._state_db_path(None)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        # The legacy layout split between plan_verification.runtime_state
        # (pending_vps) and verdicts (vp_id -> verdict dict). The
        # new framework keeps both as JSON columns on the same row.
        existing = repo.summary(plan_id)
        runtime_state = (existing or {}).get("runtime_state") or {}
        runtime_state = dict(runtime_state)
        runtime_state["pending_vps"] = list(pending_vps)
        existing_verdicts = (existing or {}).get("verdicts") or {}
        existing_verdicts = dict(existing_verdicts)
        for vp_id, verdict in verdicts.items():
            existing_verdicts[vp_id] = dict(verdict)
        if existing is None:
            # ``pending_vps`` lives inside the ``runtime_state`` JSON
            # column (the schema has no top-level pending_vps column).
            # We inject it on top of any other runtime_state content.
            runtime_state = dict(runtime_state)
            runtime_state["pending_vps"] = list(pending_vps)
            repo.insert(
                plan_id,
                verification_status="running",
                verdicts=existing_verdicts,
                runtime_state=runtime_state,
            )
        else:
            # Idempotent update for the existing row.
            runtime_state = dict(runtime_state)
            runtime_state["pending_vps"] = list(pending_vps)
            encoded = repo._encode_fields({
                "verdicts": existing_verdicts,
                "runtime_state": runtime_state,
            })
            for col, val in encoded.items():
                repo._conn.execute(
                    f"UPDATE plan_verification SET {col} = ? WHERE plan_id = ?",
                    (val, plan_id),
                )
            repo._conn.commit()
    finally:
        conn.close()
    return db_path


def _write_progress_file(
    plan_dir: Path,
    *,
    plan_id: str = "20260607-recovery",
    current_vp: Any = None,
    current_layer: Any = None,
    completed_vps: List[str] = (),
    layer_summaries: Dict[str, Dict[str, int]] = None,
    db_path: Path = None,
) -> Path:
    """Write the progress state to the state-machine SQLite row
    (plan_verification.progress_state) via the VerificationRepository.
    The on-disk layout is the ``progress_state`` JSON column.
    """
    payload = {
        "plan_id": plan_id,
        "current_vp": current_vp,
        "current_layer": current_layer,
        "completed_vps": list(completed_vps),
        "failed_vps": [],
        "skipped_vps": [],
        "layer_summaries": dict(layer_summaries) if layer_summaries else {},
        "updated_at": "2026-06-07T00:00:00.000000Z",
    }
    import server
    if db_path is None:
        db_path = server._state_db_path(None)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )
    conn = open_db(db_path)
    try:
        migrate(conn)
        repo = VerificationRepository(conn)
        existing = repo.summary(plan_id)
        if existing is None:
            repo.insert(
                plan_id,
                verification_status="running",
                progress_state=payload,
            )
        else:
            encoded = repo._encode_fields({"progress_state": payload})
            repo._conn.execute(
                "UPDATE plan_verification SET progress_state = ? WHERE plan_id = ?",
                (encoded["progress_state"], plan_id),
            )
            repo._conn.commit()
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Test 1: terminal VPs are skipped (not re-executed)
# ---------------------------------------------------------------------------


def test_recover_terminal_vp_skipped(
    recovery_plan: Dict[str, Any],
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """When the verdict map on disk already contains terminal
    verdicts for 2 VPs, a fresh executor skips them: they are
    NOT in ``pending_vps`` and their verdicts are NOT overwritten.

    This is the **happy path** of cross-process recovery: the
    previous run completed VPs 1 and 2 cleanly (one PASSED, one
    FAILED) and crashed (or was restarted) before reaching VP-003.
    A fresh executor picks up exactly at VP-003.

    Layout pre-populated on disk::

        verification_executor_state.json:
            pending_vps: ["VP-003"]
            verdicts:
                "VP-001": {"status": "PASSED", ...}
                "VP-002": {"status": "FAILED", ...}

        verification_progress_state.json:
            current_vp: null
            completed_vps: ["VP-001", "VP-002"]

    Expectations after ``__init__``:

      * ``pending_vps == ["VP-003"]`` (the one VP still pending).
      * ``collect_verdicts()`` returns 2 entries, one per
        terminal VP, with their original status / reasons /
        evidence preserved verbatim.
      * ``current_vp is None`` (no in-flight signal).
      * ``failed_vps == ["VP-002"]`` (the FAILED index list is
        backfilled from the verdict map).
      * The runner is NOT invoked by the constructor.
    """
    # --- 1. Pre-populate the on-disk artifacts ---------------------
    _write_state_file(
        tmp_plan_dir,
        pending_vps=["VP-003"],
        verdicts={
            "VP-001": {
                "status": "PASSED",
                "reasons": ["pytest exit 0"],
                "evidence": {"logs_path": "logs/VP-001.log"},
            },
            "VP-002": {
                "status": "FAILED",
                "reasons": ["reviewer rejected password hashing"],
                "evidence": {"comments_url": "https://..."},
            },
        },
    )
    _write_progress_file(
        tmp_plan_dir,
        current_vp=None,
        completed_vps=["VP-001", "VP-002"],
    )

    # --- 2. Construct the executor ---------------------------------
    executor = _make_executor(
        recovery_plan, tmp_plan_dir, mock_runner, plan_id="20260607-recovery"
    )

    # --- 3. Pending list is the remaining 1 VP ---------------------
    assert executor.pending_vps == ["VP-003"], (
        f"expected only VP-003 to be pending, got {executor.pending_vps!r}"
    )

    # --- 4. Verdicts are loaded verbatim (no re-computation) -------
    collected = executor.collect_verdicts()
    by_id: Dict[str, Dict[str, Any]] = {e["vp_id"]: e for e in collected}
    assert set(by_id.keys()) == {"VP-001", "VP-002"}, (
        f"expected 2 terminal verdicts, got {sorted(by_id.keys())!r}"
    )
    assert by_id["VP-001"]["status"] == "PASSED"
    assert by_id["VP-001"]["reasons"] == ["pytest exit 0"]
    assert by_id["VP-002"]["status"] == "FAILED"
    assert by_id["VP-002"]["reasons"] == ["reviewer rejected password hashing"]

    # --- 5. No in-flight signal ------------------------------------
    assert executor.current_vp is None
    assert executor.current_vp is None  # legacy: was current_layer

    # --- 6. failed_vps index is backfilled from the verdict map ---
    assert executor.failed_vps == ["VP-002"], (
        f"expected VP-002 to be in failed_vps, got {executor.failed_vps!r}"
    )
    assert executor.skipped_vps == [], (
        f"expected empty skipped_vps, got {executor.skipped_vps!r}"
    )

    # --- 7. The runner is NOT invoked by the constructor -----------
    mock_runner.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: in-flight VP is force-marked FAILED with recovered_from_crash
# ---------------------------------------------------------------------------


def test_recover_inflight_marked_failed(
    recovery_plan: Dict[str, Any],
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """When the progress state file says a VP is in-flight
    (``current_vp`` is set) but no per-VP verdict file exists on
    disk, the executor must synthesize a FAILED verdict with the
    marker reason ``"recovered_from_crash"`` and clear
    ``current_vp`` to ``None``.

    This is the **crash recovery** contract: the subprocess that
    was running VP-007 is gone (OOM, kill -9, server restart).
    Re-executing VP-007 would double-count it; the contract is to
    mark it FAILED so the next round's repair loop sees the
    failure and the dashboard / orchestrator can show a clear
    "recovered from crash" status.

    Layout pre-populated on disk::

        verification_executor_state.json:
            pending_vps: ["VP-007", "VP-008", "VP-009"]
            verdicts: {}    (VP-007 had not finished writing)

        verification_progress_state.json:
            current_vp: "VP-007"
            current_layer: "L1"
            completed_vps: ["VP-001", "VP-002", "VP-003"]

        (no vps/VP-007/verdict.json on disk — the subprocess died)

    Expectations after ``__init__``:

      * VP-007 has a FAILED verdict in the verdict map with
        ``reasons == ["recovered_from_crash"]``.
      * VP-007 is in ``failed_vps``.
      * VP-007 is removed from ``pending_vps`` (only VP-008 and
        VP-009 remain).
      * ``current_vp`` is reset to ``None`` (so ``run()`` does
        not immediately re-pick VP-007).
      * The on-disk verdict-map state file is updated so a
        subsequent restart sees the same FAILED verdict (no
        "phantom in-flight" persistence).
    """
    # --- 1. Pre-populate the on-disk artifacts ---------------------
    # The crashed state: VP-007 was the in-flight VP at the
    # moment of the crash.  The verdict map still has it in
    # pending (the verdict was never recorded) and the progress
    # state file says it was the active VP.
    _write_state_file(
        tmp_plan_dir,
        pending_vps=["VP-007", "VP-008", "VP-009"],
        verdicts={},
    )
    _write_progress_file(
        tmp_plan_dir,
        current_vp="VP-007",
        current_layer="L1",
        completed_vps=["VP-001", "VP-002", "VP-003"],
    )

    # Sanity: the per-VP verdict file does NOT exist (the
    # subprocess that would have written it died before fsync).
    assert not (tmp_plan_dir / "vps" / "VP-007" / "verdict.json").exists()

    # --- 2. Construct the executor ---------------------------------
    executor = _make_executor(
        recovery_plan, tmp_plan_dir, mock_runner, plan_id="20260607-recovery"
    )

    # --- 3. VP-007 has a FAILED verdict with the crash marker ------
    collected = {e["vp_id"]: e for e in executor.collect_verdicts()}
    assert "VP-007" in collected, (
        f"VP-007 should have a synthesized FAILED verdict, got "
        f"{sorted(collected.keys())!r}"
    )
    failed_entry = collected["VP-007"]
    assert failed_entry["status"] == "FAILED", (
        f"in-flight VP should be marked FAILED, got "
        f"{failed_entry['status']!r}"
    )
    assert "recovered_from_crash" in failed_entry["reasons"], (
        f"reasons must include the recovery marker, got "
        f"{failed_entry['reasons']!r}"
    )

    # --- 4. VP-007 is in failed_vps --------------------------------
    assert "VP-007" in executor.failed_vps, (
        f"VP-007 should be in failed_vps, got {executor.failed_vps!r}"
    )

    # --- 5. VP-007 is removed from pending_vps ---------------------
    assert "VP-007" not in executor.pending_vps, (
        f"VP-007 should be removed from pending_vps, got "
        f"{executor.pending_vps!r}"
    )
    # The other pending VPs (VP-008, VP-009) are still pending.
    assert set(executor.pending_vps) == {"VP-008", "VP-009"}, (
        f"expected VP-008 and VP-009 to remain pending, got "
        f"{executor.pending_vps!r}"
    )

    # --- 6. current_vp is reset to None ----------------------------
    assert executor.current_vp is None, (
        f"current_vp must be reset to None after recovery, got "
        f"{executor.current_vp!r}"
    )
    # current_layer is also reset to None so the dashboard sees an
    # idle executor.
    assert executor.current_vp is None  # legacy: was current_layer

    # --- 7. The verdict map on disk is updated so a subsequent
    #       restart sees the same FAILED verdict. --------------------
    state_file = tmp_plan_dir / DEFAULT_STATE_FILENAME
    assert state_file.exists()
    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert "VP-007" in on_disk["verdicts"], (
        f"VP-007 should be persisted as FAILED in the on-disk "
        f"state file, got {sorted(on_disk['verdicts'].keys())!r}"
    )
    assert on_disk["verdicts"]["VP-007"]["status"] == "FAILED"
    assert "recovered_from_crash" in on_disk["verdicts"]["VP-007"]["reasons"]
    assert "VP-007" not in on_disk["pending_vps"], (
        f"VP-007 should be removed from pending_vps on disk, "
        f"got {on_disk['pending_vps']!r}"
    )

    # --- 8. The runner is NOT invoked by the constructor -----------
    mock_runner.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: pending VPs (no in-flight signal) are not touched
# ---------------------------------------------------------------------------


def test_recover_pending_continues(
    recovery_plan: Dict[str, Any],
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """When the progress state file has ``current_vp = None``
    (no in-flight signal) and the verdict map already has 1 of 3
    VPs marked terminal, the executor leaves the remaining 2 VPs
    in ``pending_vps`` and does NOT synthesize any FAILED
    verdict.

    This is the **idle recovery** contract: a previous run
    completed 1 VP and then the orchestrator / dashboard simply
    stopped (no crash — ``current_vp`` was cleanly reset to
    ``None`` at the end of the previous run).  A fresh executor
    picks up the remaining 2 VPs without any FAILED synthesis.

    Layout pre-populated on disk::

        verification_executor_state.json:
            pending_vps: ["VP-002", "VP-003"]
            verdicts:
                "VP-001": {"status": "PASSED", ...}

        verification_progress_state.json:
            current_vp: null
            completed_vps: ["VP-001"]

    Expectations after ``__init__``:

      * ``pending_vps == ["VP-002", "VP-003"]`` (preserved
        verbatim).
      * ``collect_verdicts()`` returns 1 entry for VP-001 with
        the original PASSED verdict (no recovery FAILED verdict
        is synthesized).
      * ``current_vp is None`` (already was — no in-flight).
      * The on-disk verdict map is unchanged.
    """
    # --- 1. Pre-populate the on-disk artifacts ---------------------
    _write_state_file(
        tmp_plan_dir,
        pending_vps=["VP-002", "VP-003"],
        verdicts={
            "VP-001": {
                "status": "PASSED",
                "reasons": ["pytest exit 0"],
                "evidence": {"logs_path": "logs/VP-001.log"},
            },
        },
    )
    _write_progress_file(
        tmp_plan_dir,
        current_vp=None,
        completed_vps=["VP-001"],
    )

    # --- 2. Construct the executor ---------------------------------
    executor = _make_executor(
        recovery_plan, tmp_plan_dir, mock_runner, plan_id="20260607-recovery"
    )

    # --- 3. The remaining 2 VPs are still pending ------------------
    assert executor.pending_vps == ["VP-002", "VP-003"], (
        f"expected VP-002 and VP-003 to be pending, got "
        f"{executor.pending_vps!r}"
    )

    # --- 4. VP-001's PASSED verdict is preserved verbatim ---------
    collected = {e["vp_id"]: e for e in executor.collect_verdicts()}
    assert list(collected.keys()) == ["VP-001"], (
        f"expected only VP-001 in verdicts, got "
        f"{sorted(collected.keys())!r}"
    )
    assert collected["VP-001"]["status"] == "PASSED"
    assert collected["VP-001"]["reasons"] == ["pytest exit 0"]
    assert collected["VP-001"]["evidence"] == {"logs_path": "logs/VP-001.log"}

    # --- 5. No FAILED verdict was synthesized ---------------------
    assert executor.failed_vps == [], (
        f"failed_vps should be empty (no in-flight recovery), "
        f"got {executor.failed_vps!r}"
    )
    assert executor.skipped_vps == [], (
        f"skipped_vps should be empty, got {executor.skipped_vps!r}"
    )

    # --- 6. current_vp stays None ----------------------------------
    assert executor.current_vp is None

    # --- 7. The runner is NOT invoked by the constructor -----------
    mock_runner.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: no state file on disk → fresh init from the plan
# ---------------------------------------------------------------------------


def test_no_state_file_full_init(
    recovery_plan: Dict[str, Any],
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """When neither the verdict-map state file nor the progress
    state file exists on disk, the executor performs a fresh
    init: every VP from the plan appears in ``pending_vps``, the
    verdicts map is empty, and ``current_vp`` is ``None``.

    This is the **first-run** contract: a brand-new plan that
    has never been verified before.  The constructor must not
    crash on missing files and must not write anything to disk
    (it is read-only until the first ``record_verdict`` /
    ``_save_state`` call).

    Layout: no state file, no progress state file.  Only
    ``plan_dir`` exists.

    Expectations after ``__init__``:

      * ``pending_vps == ["VP-001", "VP-002", "VP-003"]`` (all
        VPs from the plan, in plan order).
      * ``collect_verdicts() == []`` (no verdicts yet).
      * ``current_vp is None``.
      * ``failed_vps == []`` and ``skipped_vps == []``.
      * The state file does NOT exist on disk (the constructor
        does not write — it is read-only until
        ``record_verdict``).
    """
    # Sanity: the plan_dir is fresh — neither state file exists.
    assert not (tmp_plan_dir / DEFAULT_STATE_FILENAME).exists()
    assert not (tmp_plan_dir / PROGRESS_STATE_FILENAME).exists()

    # --- 1. Construct the executor ---------------------------------
    executor = _make_executor(
        recovery_plan, tmp_plan_dir, mock_runner, plan_id="20260607-recovery"
    )

    # --- 2. All 3 VPs from the plan are pending -------------------
    assert executor.pending_vps == ["VP-001", "VP-002", "VP-003"], (
        f"expected all 3 VPs in plan order, got "
        f"{executor.pending_vps!r}"
    )

    # --- 3. The verdicts store is empty ---------------------------
    assert executor.collect_verdicts() == [], (
        f"expected empty verdicts on first run, got "
        f"{executor.collect_verdicts()!r}"
    )

    # --- 4. No in-flight / failed / skipped state -----------------
    assert executor.current_vp is None
    assert executor.current_vp is None  # legacy: was current_layer
    assert executor.failed_vps == []
    assert executor.skipped_vps == []
    assert executor.completed_vps == []

    # --- 5. The constructor does not write the state file ---------
    # The constructor is read-only — it only writes on
    # ``record_verdict`` / ``_save_state``.  A first-run
    # ``VerificationExecutor`` therefore leaves the plan_dir
    # untouched.
    assert not (tmp_plan_dir / DEFAULT_STATE_FILENAME).exists(), (
        "first-run constructor must not write the state file"
    )

    # --- 6. The runner is NOT invoked by the constructor -----------
    mock_runner.assert_not_called()

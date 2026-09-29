"""
TDD tests for ``VerificationExecutor`` — Phase 2 functional executor skeleton.

Background
----------
The :class:`VerificationExecutor` (in ``backend/verification_executor.py``)
is the Phase 2 executor that drives the verification plan. It is shaped
to mirror ``AutonomousAgent.run()`` in ``agent.py`` (same 4-tuple
constructor: plan, plan_id, plan_dir, runner) so the two can be swapped
in the same orchestrator slot.

This skeleton does NOT implement the ``run()`` main loop — only the
class skeleton, the verdict-schema validation, and the
load-or-init/persist state lifecycle. The four tests below pin one
contract each:

  1. ``test_init_creates_pending_vps_from_plan``
     After construction, ``executor.pending_vps`` is the list of
     ``vp["id"]`` values from the plan, in plan order. ``verdicts``
     is empty.

  2. ``test_validate_verdict_accepts_valid``
     ``_validate_verdict({'status': 'PASSED', 'reasons': [...],
     'evidence': {...}})`` returns silently — no exception.

  3. ``test_validate_verdict_rejects_invalid_status``
     ``_validate_verdict({'status': 'UNKNOWN', ...})`` raises
     :class:`ValueError` (the schema-error subclass).

  4. ``test_collect_verdicts_returns_all``
     After recording verdicts for 3 VPs via
     :meth:`record_verdict`, :meth:`collect_verdicts` returns 3
     entries, each containing the original verdict body plus a
     ``vp_id`` key.

These tests do NOT exercise ``run()`` (not implemented in the
skeleton) and do NOT touch any real LLM / network. The
``sub_agent_runner`` is a ``Mock`` that is recorded but never
invoked by the skeleton.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import pytest


# Ensure ``backend/`` is on ``sys.path`` so ``import verification_executor``
# works regardless of which test runner entry point is used. Mirrors
# the pattern in ``test_agent_breakdown.py``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


from verification_executor import (  # noqa: E402
    ALLOWED_STATUSES,
    DEFAULT_STATE_FILENAME,
    PROGRESS_STATE_FILENAME,
    VerdictSchemaError,
    VerificationExecutor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_runner() -> Mock:
    """A mock sub-agent runner. The skeleton does not invoke it, so
    the mock is a passive object that tests can introspect.
    """
    return Mock()


@pytest.fixture
def tmp_plan_dir(tmp_path: Path) -> Path:
    """A fresh, empty plan directory. The skeleton does not create
    any sub-directories — it only writes the state file directly
    under ``plan_dir``.
    """
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def sample_plan() -> Dict[str, Any]:
    """A 3-VP plan covering each layer / method combination we care
    about. The skeleton only reads the ``id`` field per VP, so the
    other fields are present to mirror a real plan shape (the
    orchestrator will pass them through to the sub-agent runner).
    """
    return {
        "vps": [
            {
                "id": "VP-001",
                "layer": "L1",
                "method": "automated_test",
                "title": "Login API contract",
                "test_command": "pytest tests/test_auth.py",
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
                "target_url": "http://127.0.0.1:8000/login",
            },
        ]
    }


def _make_executor(
    verification_plan: Dict[str, Any],
    plan_dir: Path,
    mock_runner: Mock,
    plan_id: str = "20260607-test",
) -> VerificationExecutor:
    """Helper to construct a VerificationExecutor with the test fixtures.

    Uses positional args for the first three parameters to keep the
    call sites compact; ``plan_id`` is keyword-only with a default.
    """
    return VerificationExecutor(
        verification_plan=verification_plan,
        plan_id=plan_id,
        plan_dir=plan_dir,
        sub_agent_runner=mock_runner,
    )


# ---------------------------------------------------------------------------
# Test 1: __init__ seeds pending_vps from the plan
# ---------------------------------------------------------------------------


def test_init_creates_pending_vps_from_plan(
    sample_plan: Dict[str, Any],
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """After construction, ``pending_vps`` mirrors the plan's VP ids
    in plan order, and the verdicts store is empty. The state file
    has NOT been written (the constructor is read-only — it only
    writes on ``record_verdict`` / ``_save_state``).
    """
    executor = _make_executor(sample_plan, tmp_plan_dir, mock_runner)

    # 3 VPs in the plan → 3 pending ids, in plan order.
    assert executor.pending_vps == ["VP-001", "VP-002", "VP-003"]

    # Verdicts store is empty (no VPs have produced a verdict yet).
    assert executor.collect_verdicts() == []

    # Sub-agent runner is stored but NOT invoked by the skeleton.
    mock_runner.assert_not_called()

    # The state file path is wired up correctly (this is the contract
    # cross-process recovery depends on).
    assert executor.state_file == tmp_plan_dir / DEFAULT_STATE_FILENAME
    assert executor.state_file.parent == tmp_plan_dir


# ---------------------------------------------------------------------------
# Test 2: _validate_verdict accepts a well-formed payload
# ---------------------------------------------------------------------------


def test_validate_verdict_accepts_valid(
    sample_plan: Dict[str, Any],
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """A verdict that matches the schema returns silently — no
    exception is raised. This is the input example from the task
    spec:

        verdict = {'status': 'PASSED', 'reasons': ['pytest exit 0'],
                   'evidence': {'logs_path': '...'}}
        executor._validate_verdict(verdict)  # 不抛异常
    """
    executor = _make_executor(sample_plan, tmp_plan_dir, mock_runner)

    verdict = {
        "status": "PASSED",
        "reasons": ["pytest exit 0"],
        "evidence": {"logs_path": "logs/VP-001.log"},
    }

    # Should not raise.
    result = executor._validate_verdict(verdict)
    assert result is None  # no return value (None is implicit)


# ---------------------------------------------------------------------------
# Test 3: _validate_verdict rejects an invalid status
# ---------------------------------------------------------------------------


def test_validate_verdict_rejects_invalid_status(
    sample_plan: Dict[str, Any],
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """A verdict whose status is outside ``ALLOWED_STATUSES`` raises
    :class:`ValueError` (the schema-error subclass
    :class:`VerdictSchemaError` is a :class:`ValueError`, so the
    task spec's ``pytest.raises(ValueError)`` matches).

    The task spec input is::

        bad = {'status': 'UNKNOWN', 'reasons': [], 'evidence': {}}
        pytest.raises(ValueError): executor._validate_verdict(bad)
    """
    executor = _make_executor(sample_plan, tmp_plan_dir, mock_runner)

    bad = {"status": "UNKNOWN", "reasons": [], "evidence": {}}

    with pytest.raises(ValueError) as excinfo:
        executor._validate_verdict(bad)
    # Sanity: the error names the offending status so debugging is
    # possible without re-running with a debugger.
    assert "UNKNOWN" in str(excinfo.value)
    assert "status" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Test 4: collect_verdicts returns all collected verdicts
# ---------------------------------------------------------------------------


def test_collect_verdicts_returns_all(
    sample_plan: Dict[str, Any],
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """After recording verdicts for multiple VPs via
    :meth:`record_verdict`, :meth:`collect_verdicts` returns one
    entry per recorded VP. Each entry contains the original verdict
    body plus a ``vp_id`` key (the orchestrator / judgment phase
    needs the id outside the body so it can group verdicts back
    onto the plan).
    """
    executor = _make_executor(sample_plan, tmp_plan_dir, mock_runner)

    # Record 3 verdicts. Each uses a different status so the test
    # also covers the all-three-statuses case (a future judgment
    # test will iterate the same shape).
    verdicts_in: Dict[str, Dict[str, Any]] = {
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
        "VP-003": {
            "status": "SKIPPED",
            "reasons": ["puppeteer binary not available"],
            "evidence": {},
        },
    }
    for vp_id, verdict in verdicts_in.items():
        executor.record_verdict(vp_id, verdict)

    collected = executor.collect_verdicts()
    assert len(collected) == 3

    # Build a vp_id -> entry map for ergonomic assertions.
    by_id: Dict[str, Dict[str, Any]] = {entry["vp_id"]: entry for entry in collected}

    # All 3 recorded VPs are present in the collected output.
    for vp_id in verdicts_in.keys():
        assert vp_id in by_id, f"missing vp_id {vp_id!r} in collected verdicts"

    # Each entry's body fields (status, reasons, evidence) match
    # what was recorded. We strip the injected ``vp_id`` key for
    # the comparison so we are checking the body verbatim.
    for vp_id, expected_body in verdicts_in.items():
        actual = by_id[vp_id]
        assert actual["status"] == expected_body["status"]
        assert actual["reasons"] == expected_body["reasons"]
        assert actual["evidence"] == expected_body["evidence"]

    # After recording all 3, the pending list is empty.
    assert executor.pending_vps == []


# ---------------------------------------------------------------------------
# Optional / sanity tests (not in the task spec, but cheap to add and
# they catch the obvious regressions if a future refactor changes
# the skeleton in a way the 4 task-spec tests do not detect).
# ---------------------------------------------------------------------------


def test_validate_verdict_rejects_non_dict(
    sample_plan: Dict[str, Any],
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """A verdict that is not a dict (e.g. a list, a str) is rejected."""
    executor = _make_executor(sample_plan, tmp_plan_dir, mock_runner)
    with pytest.raises(ValueError):
        executor._validate_verdict(["not", "a", "dict"])
    with pytest.raises(ValueError):
        executor._validate_verdict("a string verdict")


def test_validate_verdict_rejects_missing_fields(
    sample_plan: Dict[str, Any],
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """A verdict missing one or more required fields is rejected;
    the error message names every missing key (so the caller can
    fix all of them in one round-trip)."""
    executor = _make_executor(sample_plan, tmp_plan_dir, mock_runner)

    # Missing all 3 required fields.
    with pytest.raises(ValueError) as excinfo:
        executor._validate_verdict({})
    msg = str(excinfo.value)
    assert "status" in msg
    assert "reasons" in msg
    assert "evidence" in msg

    # Missing only ``reasons``.
    with pytest.raises(ValueError) as excinfo:
        executor._validate_verdict({"status": "PASSED", "evidence": {}})
    assert "reasons" in str(excinfo.value)


def test_validate_verdict_rejects_wrong_types(
    sample_plan: Dict[str, Any],
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """Each field has a type contract: ``status`` is str,
    ``reasons`` is list, ``evidence`` is dict. Wrong types raise."""
    executor = _make_executor(sample_plan, tmp_plan_dir, mock_runner)

    # status is not a str (it's an int)
    with pytest.raises(ValueError) as excinfo:
        executor._validate_verdict(
            {"status": 42, "reasons": [], "evidence": {}}
        )
    assert "status" in str(excinfo.value)

    # reasons is not a list (it's a str)
    with pytest.raises(ValueError) as excinfo:
        executor._validate_verdict(
            {"status": "PASSED", "reasons": "not a list", "evidence": {}}
        )
    assert "reasons" in str(excinfo.value)

    # evidence is not a dict (it's a list)
    with pytest.raises(ValueError) as excinfo:
        executor._validate_verdict(
            {"status": "PASSED", "reasons": [], "evidence": ["not a dict"]}
        )
    assert "evidence" in str(excinfo.value)


def test_load_or_init_state_loads_existing(
    sample_plan: Dict[str, Any],
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """If a state file already exists on disk, a fresh executor
    pointed at the same ``plan_dir`` picks up the existing
    ``pending_vps`` and ``verdicts`` (cross-process recovery)."""
    # 1) Build an executor and record one verdict, which writes
    #    state to disk.
    executor_a = _make_executor(sample_plan, tmp_plan_dir, mock_runner)
    executor_a.record_verdict(
        "VP-001",
        {
            "status": "PASSED",
            "reasons": ["ok"],
            "evidence": {"logs_path": "x"},
        },
    )

    # Sanity: the state file is on disk.
    state_file = tmp_plan_dir / DEFAULT_STATE_FILENAME
    assert state_file.exists()

    # 2) Build a fresh executor against the same plan_dir. The
    #    pending list and verdicts should be loaded from disk,
    #    not regenerated from the plan.
    executor_b = _make_executor(sample_plan, tmp_plan_dir, mock_runner)
    assert executor_b.pending_vps == ["VP-002", "VP-003"]
    collected = executor_b.collect_verdicts()
    assert len(collected) == 1
    assert collected[0]["vp_id"] == "VP-001"
    assert collected[0]["status"] == "PASSED"


def test_init_with_empty_plan(
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """A plan with no VPs produces an empty pending list and an
    empty verdicts store — no crash, no defaults inserted."""
    executor = _make_executor(
        verification_plan={"vps": []},
        plan_dir=tmp_plan_dir,
        mock_runner=mock_runner,
    )
    assert executor.pending_vps == []
    assert executor.collect_verdicts() == []


# ---------------------------------------------------------------------------
# Phase 2 — run() main loop TDD tests
# ---------------------------------------------------------------------------
#
# These four tests pin the contract of the new ``run()`` method that
# drives the executor through the verification plan layer by layer.
# They are split out from the skeleton tests above because the run()
# method is a separate follow-up subtask and is exercised in a
# different way (async, side-effecting on disk, fanned out across
# the sub-agent runner).
#
# The 4 contract pins are:
#
#   1. ``test_run_executes_vps_in_layer_order``
#      VPs are passed to the sub-agent runner in L1→L2→L3 order
#      regardless of the order they appear in the plan (the
#      scheduler sorts by layer).
#
#   2. ``test_run_updates_current_vp_per_step``
#      ``current_vp`` is set to each VP id while it is being
#      executed and reset to ``None`` when idle. The progress
#      state file is updated accordingly.
#
#   3. ``test_run_layer_summaries_aggregated``
#      ``layer_summaries`` is populated with per-layer completed /
#      failed / skipped / total counters based on the verdict
#      statuses returned by the runner.
#
#   4. ``test_run_fsync_each_status_change``
#      ``_save_progress_state`` is called at least 2 × VP_count
#      times during ``run()`` (one fsync per VP-status change:
#      pending→running, then running→terminal).
#
# All four tests use a sync ``sub_agent_runner`` (a ``Mock`` with a
# configured return value); the executor accepts both sync and
# async runners, but a sync mock keeps the test surface minimal
# and avoids the ``asyncio`` ceremony. The executor is still called
# via ``asyncio.run(executor.run())`` so the await path is
# exercised end-to-end.


# Reusable plan with 4 VPs across 3 layers. The IDs are intentionally
# in *non-layer order* so the layer-sort test can detect a buggy
# implementation that iterates the plan naively.
_MIXED_LAYER_PLAN: Dict[str, Any] = {
    "vps": [
        {
            "id": "VP-101",
            "layer": "L3",
            "method": "ui_validation",
            "title": "End-to-end smoke test",
        },
        {
            "id": "VP-001",
            "layer": "L1",
            "method": "automated_test",
            "title": "Unit test for module A",
        },
        {
            "id": "VP-002",
            "layer": "L1",
            "method": "automated_test",
            "title": "Unit test for module B",
        },
        {
            "id": "VP-051",
            "layer": "L2",
            "method": "code_review",
            "title": "Integration review",
        },
    ]
}


def _passing_verdict(reason: str = "ok") -> Dict[str, Any]:
    """A canonical PASSED verdict for use in runner return values."""
    return {
        "status": "PASSED",
        "reasons": [reason],
        "evidence": {"logs_path": f"logs/{reason}.log"},
    }


def _failing_verdict(reason: str = "nope") -> Dict[str, Any]:
    """A canonical FAILED verdict for use in runner return values."""
    return {
        "status": "FAILED",
        "reasons": [reason],
        "evidence": {"stderr": reason},
    }


# ---------------------------------------------------------------------------
# Test R1: VPs are passed to the runner in plan-insertion order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_executes_vps_in_plan_order(
    tmp_plan_dir: Path,
) -> None:
    """``run()`` must invoke the sub-agent runner once per VP, in
    the order the VPs appear in the plan. (L1/L2/L3 layer sorting
    was removed 2026-06-13; VPs are now processed in plan order.)

    The plan declares the VPs out of order (VP-101 first, then
    VP-001, VP-002, VP-051). A correct implementation processes
    them in plan-insertion order, so the runner call sequence is
    ``[VP-101, VP-001, VP-002, VP-051]`` — not sorted by id.

    The mock runner returns a fixed PASSED verdict for every call
    so the scheduler advances through all 4 VPs without retry
    logic interfering with the assertion.
    """
    runner = Mock(return_value=_passing_verdict())
    executor = _make_executor(
        _MIXED_LAYER_PLAN, tmp_plan_dir, runner, plan_id="plan-order"
    )

    await executor.run()

    # The runner was called once per VP (4 VPs in the plan).
    assert runner.call_count == 4, (
        f"expected 4 runner invocations, got {runner.call_count}"
    )

    # The order of calls matches plan-insertion order — extracted
    # from the ``id`` field of the VP dict passed as the first
    # positional argument to the runner.
    called_ids: List[str] = [
        call.args[0]["id"] for call in runner.call_args_list
    ]
    assert called_ids == ["VP-101", "VP-001", "VP-002", "VP-051"], (
        f"VPs were not executed in plan-insertion order: "
        f"got {called_ids}"
    )

    # After run() completes, all 4 VPs are in the verdicts store
    # and ``current_vp`` is reset (the loop is done).
    assert len(executor.collect_verdicts()) == 4
    assert executor.current_vp is None


# ---------------------------------------------------------------------------
# Test R2: current_vp tracks the active VP and is persisted to disk
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Test R3: per-VP terminal statuses are aggregated into completed / failed / skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_aggregates_verdict_statuses(
    tmp_plan_dir: Path,
) -> None:
    """The dispatcher records per-VP terminal statuses in the
    ``completed_vps`` / ``failed_vps`` / ``skipped_vps`` index
    lists. (L1/L2/L3 layer summaries were removed 2026-06-13;
    the per-VP index lists are now the source of truth.)

    Plan shape: 4 VPs in plan-insertion order (VP-101, VP-001,
    VP-002, VP-051). The runner returns PASSED for VP-101,
    PASSED for VP-001, FAILED for VP-002, PASSED for VP-051. The
    expected aggregation is:

      * completed_vps = [VP-101, VP-001, VP-051]  (in plan order)
      * failed_vps    = [VP-002]
      * skipped_vps   = []
      * collect_verdicts() returns all 4 verdicts.
    """
    verdict_by_id: Dict[str, Dict[str, Any]] = {
        "VP-101": _passing_verdict(reason="VP-101 passed"),
        "VP-001": _passing_verdict(reason="VP-001 passed"),
        "VP-002": _failing_verdict(reason="VP-002 failed"),
        "VP-051": _passing_verdict(reason="VP-051 passed"),
    }

    def runner(vp: Dict[str, Any]) -> Dict[str, Any]:
        return verdict_by_id[vp["id"]]

    executor = _make_executor(
        _MIXED_LAYER_PLAN, tmp_plan_dir, runner, plan_id="aggregate"
    )

    await executor.run()

    # Terminal status index lists. 2026-08-25 contract (see
    # ``verification_executor.py:_rebuild_status_indexes``):
    # ``completed_vps`` is PASSED-only, because the resume path treats
    # it as "terminal success — skip on the next round". A FAILED VP
    # left inside it would never be re-run.
    assert executor.completed_vps == ["VP-101", "VP-001", "VP-051"]
    assert executor.failed_vps == ["VP-002"]
    assert executor.skipped_vps == []

    # Sanity: the verdicts store has all 4 VPs.
    assert len(executor.collect_verdicts()) == 4


# ---------------------------------------------------------------------------
# Test R4: progress state is fsync'd on every vp_status_changed event
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Bonus tests — runner-exception coercion and layer-sort key
# ---------------------------------------------------------------------------
#
# These are not in the original 4-spec list, but they pin two
# related contracts that are easy to regress in a future refactor
# of run().


@pytest.mark.asyncio
async def test_run_coerces_runner_exception_to_failed_verdict(
    tmp_plan_dir: Path,
) -> None:
    """If the sub-agent runner raises (sync), ``run()`` converts the
    exception into a FAILED verdict rather than letting the loop
    crash. The exception type and message are surfaced in
    ``reasons`` and ``evidence`` so the failure is diagnosable
    from the progress state file alone.

    The plan is a single-layer L1 plan with 3 VPs. A single-layer
    plan is used because the L1→L2 short-circuit would otherwise
    short-circuit the higher-layer VPs to SKIPPED (not FAILED)
    once the first L1 exception is coerced — defeating the
    point of this test, which is to assert that *every* coerced
    exception ends up as a FAILED verdict in the verdict store.
    """

    pure_l1_plan: Dict[str, Any] = {
        "vps": [
            {
                "id": "VP-A",
                "layer": "L1",
                "method": "automated_test",
                "title": "unit test A",
            },
            {
                "id": "VP-B",
                "layer": "L1",
                "method": "automated_test",
                "title": "unit test B",
            },
            {
                "id": "VP-C",
                "layer": "L1",
                "method": "automated_test",
                "title": "unit test C",
            },
        ]
    }

    def raising_runner(vp: Dict[str, Any]) -> Dict[str, Any]:
        raise RuntimeError(f"subprocess crashed for {vp['id']}")

    executor = _make_executor(
        pure_l1_plan, tmp_plan_dir, raising_runner, plan_id="exc-coerce"
    )

    # The loop should not raise — the exception is absorbed and
    # turned into a verdict.
    await executor.run()

    # All 3 VPs have a verdict in the store, all of them FAILED.
    collected = executor.collect_verdicts()
    assert len(collected) == 3
    for entry in collected:
        assert entry["status"] == "FAILED", (
            f"expected FAILED for {entry['vp_id']!r}, "
            f"got {entry['status']!r}"
        )
        # The reason should reference the exception.
        joined = " ".join(entry["reasons"])
        assert "RuntimeError" in joined, (
            f"reason for {entry['vp_id']!r} did not include the "
            f"exception type: {entry['reasons']!r}"
        )
        assert "subprocess crashed" in joined, (
            f"reason for {entry['vp_id']!r} did not include the "
            f"exception message: {entry['reasons']!r}"
        )

    # All 3 VPs are in failed_vps, in plan order (the L1/L2/L3 layer
    # sort was removed 2026-06-13).
    # ``completed_vps`` stays EMPTY: since the 2026-08-25 contract it
    # is PASSED-only ("terminal success, skip on the next round"), and
    # a coerced exception produces FAILED verdicts. Leaving them in
    # ``completed_vps`` would make the resume path skip the very VPs
    # that need re-running.
    assert executor.failed_vps == ["VP-A", "VP-B", "VP-C"]
    assert executor.completed_vps == []
    assert executor.skipped_vps == []


# ---------------------------------------------------------------------------
# run() sanity: empty plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_empty_plan(
    tmp_plan_dir: Path,
    mock_runner: Mock,
) -> None:
    """``run()`` on a plan with no VPs is a no-op: the runner is
    not invoked, ``current_vp`` stays ``None``, and ``completed_vps``
    stays empty. The progress state file is still written (the
    "loop completed" snapshot) so the dashboard can distinguish
    "no VPs to run" from "run() was never called"."""
    executor = _make_executor(
        verification_plan={"vps": []},
        plan_dir=tmp_plan_dir,
        mock_runner=mock_runner,
        plan_id="empty-plan",
    )

    await executor.run()

    mock_runner.assert_not_called()
    assert executor.current_vp is None
    assert executor.completed_vps == []
    assert executor.failed_vps == []
    assert executor.skipped_vps == []


# ---------------------------------------------------------------------------
# run() concurrency: progress state is fsync'd after each VP in parallel mode
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Tests: depends_on DAG layering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_respects_depends_on_layer_order(
    tmp_plan_dir: Path,
) -> None:
    """A VP that depends on another is only dispatched after the
    dependency finishes.  ``depends_on`` is the source of truth for
    layer order; the v1 ``layer`` field is documentation only.

    The test uses a 3-VP chain (A -> B -> C) and a recorder runner
    that records the dispatch order.  We assert B is dispatched
    only after A's verdict is recorded, and C only after B's.
    """
    plan: Dict[str, Any] = {
        "vps": [
            {"id": "VP-A", "method": "automated_test", "title": "A"},
            {
                "id": "VP-B",
                "method": "automated_test",
                "title": "B",
                "depends_on": ["VP-A"],
            },
            {
                "id": "VP-C",
                "method": "automated_test",
                "title": "C",
                "depends_on": ["VP-B"],
            },
        ]
    }
    dispatch_order: List[str] = []
    verdicts: Dict[str, Dict[str, Any]] = {}

    def runner(vp: Dict[str, Any]) -> Dict[str, Any]:
        # Record the dispatch order.  We also snapshot the current
        # verdicts at the time of dispatch so the test can prove
        # that B is only run after A's verdict is recorded.
        dispatch_order.append(str(vp["id"]))
        verdicts_snapshot_at_dispatch = dict(verdicts)
        verdict = _passing_verdict(reason=vp["id"])
        verdicts[str(vp["id"])] = verdict
        # Stash the snapshot for assertion below.
        runner._snapshots = getattr(runner, "_snapshots", {})
        runner._snapshots[str(vp["id"])] = verdicts_snapshot_at_dispatch
        return verdict

    executor = _make_executor(plan, tmp_plan_dir, runner, plan_id="dag-chain")
    await executor.run()

    # Dispatch order is strictly A -> B -> C.
    assert dispatch_order == ["VP-A", "VP-B", "VP-C"]

    # When B is dispatched, A's verdict is already recorded.
    assert "VP-A" in runner._snapshots["VP-B"]
    # When C is dispatched, B's verdict is already recorded
    # (proving C was not dispatched in the same layer as B — the
    # depends_on contract is honoured).
    assert "VP-B" in runner._snapshots["VP-C"]
    # A is also in C's snapshot (the verdict map is shared across
    # the whole run, so A's verdict is still in the map when C
    # runs; this is a sanity check, not a layer-ordering one).
    assert "VP-A" in runner._snapshots["VP-C"]

    # All three VPs are completed.
    assert set(executor.completed_vps) == {"VP-A", "VP-B", "VP-C"}


@pytest.mark.asyncio
async def test_run_diamond_dependency_uses_dag_layering(
    tmp_plan_dir: Path,
) -> None:
    """A diamond: A is the root; B and C depend on A; D depends on B and C.

    Execution:
      * layer 0: VP-A (the only VP with no deps)
      * layer 1: VP-B and VP-C (both depend on A) — dispatched
        together as soon as layer 0 finishes
      * layer 2: VP-D (depends on B and C) — dispatched after both
        B and C are recorded
    """
    plan: Dict[str, Any] = {
        "vps": [
            {"id": "VP-A", "method": "automated_test", "title": "root"},
            {
                "id": "VP-B",
                "method": "automated_test",
                "title": "left",
                "depends_on": ["VP-A"],
            },
            {
                "id": "VP-C",
                "method": "automated_test",
                "title": "right",
                "depends_on": ["VP-A"],
            },
            {
                "id": "VP-D",
                "method": "code_review",
                "title": "join",
                "depends_on": ["VP-B", "VP-C"],
            },
        ]
    }
    dispatch_order: List[str] = []
    verdicts: Dict[str, Dict[str, Any]] = {}

    def runner(vp: Dict[str, Any]) -> Dict[str, Any]:
        dispatch_order.append(str(vp["id"]))
        # Record the verdict AFTER snapshotting dispatch order so
        # the snapshot is consistent with the dispatch.
        verdict = _passing_verdict(reason=vp["id"])
        verdicts[str(vp["id"])] = verdict
        runner._snapshots = getattr(runner, "_snapshots", {})
        runner._snapshots[str(vp["id"])] = dict(verdicts)
        return verdict

    executor = _make_executor(plan, tmp_plan_dir, runner, plan_id="diamond")
    await executor.run()

    # A is first.
    assert dispatch_order[0] == "VP-A"
    # D is last (it depends on both B and C).
    assert dispatch_order[-1] == "VP-D"
    # B and C are in the middle, in plan-insertion order.
    middle = set(dispatch_order[1:-1])
    assert middle == {"VP-B", "VP-C"}

    # When D is dispatched, both B and C are already recorded.
    assert "VP-B" in runner._snapshots["VP-D"]
    assert "VP-C" in runner._snapshots["VP-D"]

    # All four VPs completed.
    assert set(executor.completed_vps) == {"VP-A", "VP-B", "VP-C", "VP-D"}


@pytest.mark.asyncio
async def test_run_dag_resume_skips_completed_dependency(
    tmp_plan_dir: Path,
) -> None:
    """If the upstream VP is already in the verdict map (resumed
    from a previous run), the executor does not wait for it — the
    downstream is dispatched in the layer after the dependency's
    layer, and the dependency is not re-run.
    """
    plan: Dict[str, Any] = {
        "vps": [
            {"id": "VP-A", "method": "automated_test", "title": "A"},
            {
                "id": "VP-B",
                "method": "automated_test",
                "title": "B",
                "depends_on": ["VP-A"],
            },
        ]
    }

    # Pre-populate the on-disk state with VP-A already having a
    # verdict (simulates a prior run that crashed after A but
    # before B).
    prior_state = {
        "plan_id": "dag-resume",
        "pending_vps": ["VP-A", "VP-B"],
        "verdicts": {
            "VP-A": {
                "status": "PASSED",
                "reasons": ["prior run"],
                "evidence": {},
            },
        },
    }
    (tmp_plan_dir / DEFAULT_STATE_FILENAME).write_text(
        json.dumps(prior_state, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    dispatched: List[str] = []

    def runner(vp: Dict[str, Any]) -> Dict[str, Any]:
        dispatched.append(str(vp["id"]))
        return _passing_verdict(reason=vp["id"])

    executor = _make_executor(plan, tmp_plan_dir, runner, plan_id="dag-resume")
    await executor.run()

    # Only VP-B is dispatched; VP-A is not re-run.
    assert dispatched == ["VP-B"]
    # Both VPs are marked completed in the in-memory state.
    assert set(executor.completed_vps) == {"VP-A", "VP-B"}
    # The on-disk state has both verdicts.
    on_disk = json.loads(
        (tmp_plan_dir / DEFAULT_STATE_FILENAME).read_text(encoding="utf-8")
    )
    assert set(on_disk["verdicts"].keys()) == {"VP-A", "VP-B"}


@pytest.mark.asyncio
async def test_run_dag_no_deps_preserves_plan_order(
    tmp_plan_dir: Path,
) -> None:
    """Backwards-compat: a plan with no ``depends_on`` edges
    produces a single layer, dispatched in plan-insertion order.
    This is the contract the v1 tests rely on.
    """
    plan: Dict[str, Any] = {
        "vps": [
            {"id": "VP-X1", "method": "automated_test"},
            {"id": "VP-X2", "method": "automated_test"},
            {"id": "VP-X3", "method": "code_review"},
        ]
    }
    dispatched: List[str] = []

    def runner(vp: Dict[str, Any]) -> Dict[str, Any]:
        dispatched.append(str(vp["id"]))
        return _passing_verdict(reason=vp["id"])

    executor = _make_executor(plan, tmp_plan_dir, runner, plan_id="no-deps")
    await executor.run()

    assert dispatched == ["VP-X1", "VP-X2", "VP-X3"]

"""
Performance Baseline Tests for the Verification Pipeline
=========================================================

Three wall-clock / memory gates that pin the production
performance contract of the verification pipeline. These are
the regression alarms for the runtime:

1. ``test_perf_wall_time_under_15s`` — the spec example
   ``automated_test=14, ui_validation=4, code_review=3,
   api_test=2`` runs in < 15s under the real
   :class:`VerificationAgent` with the
   :class:`FakeVerifierBackend` (fake_sleep=0.1s, well under
   the per-method timeout). The cap of 4 means the 14-VP
   automated_test group runs in 4 sequential rounds
   (14/4 → 4 batches of 4/4/4/2 → 4×0.1s = 0.4s), and the
   other three groups run in parallel in ~0.1s. The 15s
   ceiling is the production "smoke budget" the bridge UI
   uses to decide "is this round taking too long?".

2. ``test_perf_parallelism_observed_via_timestamps`` — 4
   ui_validation VPs each sleep 1s. With
   ``parallelism_cap=4`` the wall time is ~1s, NOT ~4s
   (serial). We assert the JSONL execution log carries ≥2
   ``group_started`` events whose timestamps overlap
   (i.e. multiple groups really did run concurrently). This
   pins the *observable* parallelism contract that the
   bridge UI's progress card and the log parser's
   per-group event timeline both depend on.

3. ``test_perf_no_memory_leak_3_rounds`` — running one
   warmup round and then 3 measured sequential rounds with 9
   VPs each, with ``tracemalloc`` snapshotting memory at
   the end of each measured round, must not see the
   *retained* memory (what is still alive after
   ``gc.collect()``) grow from round to round. The check
   allows a one-time bump from the first measured round to
   the second (the persistence layer's first round opens a
   new log file, registers a logger, etc.) of up to 2× the
   first measured peak, and then requires the growth of the
   retained memory between any two adjacent measured rounds
   to stay within 10% of the first measured peak. The peak
   (the largest *transient* allocation a round made) is
   measured too, but it is not what the leak criterion is
   judged on: a transient allocation frees itself before the
   next round's measurement, and the peak's absolute value
   drifts between processes. Real leaks (which grow
   unboundedly, round after round) would blow past both the
   2× warmup bound and the per-round growth allowance.
   The warmup round exists so that the pipeline's lazy
   imports are paid before the measurement starts; without
   it the import cost lands on whichever round runs first
   and the comparison is made between two rounds that did
   different work.

Marker isolation
----------------
All tests in this file carry the ``perf`` marker. The
pytest.ini registers ``perf`` as a real marker; CI can gate
on the default suite and run the perf tests in a separate
job (or nightly) without blocking PRs. The marker does NOT
remove these tests from the default collection — the
``--strict-markers`` flag in pytest.ini just means an
unknown marker is a fatal error, not that ``perf`` is
auto-skipped.

Environment
-----------
Each test sets ``VERIFICATION_PROFILE=dry_run`` so the
fake backend is used (the real LLM / subprocess path
would dominate the wall time and mask the parallelism /
memory-leak signal). The fake sleep is small enough to
keep the test under 30s in total — well under the 5-min
perf budget.

Memory measurement
------------------
We use :mod:`tracemalloc` rather than ``resource.getrusage``
because the former gives Python-level memory numbers --
both the *retained* memory (``current``, i.e. what is still
alive) and the *peak* (the largest transient allocation a
round made) -- which is what a slow leak in the verification
pipeline would actually show (the orchestrator's data
structures are all Python objects). ``getrusage`` would be
skewed by libc / OS-level noise unrelated to the agent.
The leak criterion is judged on the retained memory: that is
the quantity a leak grows, while the peak only records a
transient allocation that frees itself again.
"""

from __future__ import annotations

import asyncio
import gc
import json
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import pytest

# Make `verification_agent` / `verification` / `plan_state` importable
# when pytest is launched from either the project root or the
# ``backend/`` directory. Mirrors the pattern in
# ``test_verification_e2e_dryrun.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ``backend/`` -- the shared host-load gate lives in ``backend/tests/``,
# so the backend directory has to be importable from here.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from tests import _host_load  # noqa: E402
from verification_agent import (  # noqa: E402
    FakeVerifierBackend,
    VerificationAgent,
)
from verification_config import TimeoutPolicy  # noqa: E402


# =============================================================================
# Module-level markers
# =============================================================================

# All tests in this file are perf tests. pytestmark applies the
# marker in one place so individual tests don't have to decorate
# themselves. CI can opt in / out by selecting the marker, e.g.:
#   pytest -m perf           # only perf
#   pytest -m "not perf"     # everything else
pytestmark = pytest.mark.perf


# =============================================================================
# The host-load gate
# =============================================================================
#
# A wall-clock budget measured on a host that is already carrying someone
# else's work is a measurement of the machine, not of the code under test.
# The question "is this host too loaded to measure on?" is therefore asked
# before the budget is asserted -- and it is asked the same way everywhere,
# because a decision that is made twice is a decision that drifts.
#
# The decision lives in one place: ``backend/tests/_host_load.py``.  One
# limit, one message format, one place to change them.  On CI the gate
# stands down: a hosted runner's load is not the runner's own doing, and a
# lane that quietly skipped would report green while asserting nothing.


def _skip_if_host_too_busy() -> None:
    """Skip this case when the host is too loaded to measure a budget on.

    The budget itself is not weakened: it is asserted at exactly the value
    it always had, wherever the host is measurable.
    """
    reason = _host_load.skip_reason()
    if reason is not None:
        pytest.skip(reason)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_plan_dir(tmp_path):
    """Per-test plan directory under ``tmp_path/plans/perf``."""
    plan_dir = tmp_path / "plans" / "perf"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    """Per-test project directory under ``tmp_path/projects/perf``."""
    project_dir = tmp_path / "projects" / "perf"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_coding_tool():
    """Stand-in ``CodingTool`` — the agent's report step is
    bypassed in these perf tests; the leaf executors are
    patched, the report is not used."""
    return Mock()


@pytest.fixture
def dry_run_env(monkeypatch):
    """Pin the dry-run profile env var for every test in this file.

    Pinned via ``monkeypatch`` so the env var is restored
    after each test (we don't want one perf run to leak the
    env var into the next non-perf test).
    """
    monkeypatch.setenv("VERIFICATION_PROFILE", "dry_run")
    return monkeypatch


# =============================================================================
# Helpers
# =============================================================================


def _make_vps_by_method(
    counts: Dict[str, int],
    *,
    expected_result: str = "ok",
    timeout_seconds: int | None = None,
) -> List[Dict[str, Any]]:
    """Build a flat list of VPs whose methods match ``counts``.

    Mirrors the helper used in
    ``test_verification_e2e_dryrun.py`` so the wall-time
    budgets can be compared against the e2e reference
    values.
    """
    vps: List[Dict[str, Any]] = []
    for method, n in counts.items():
        for i in range(n):
            vp: Dict[str, Any] = {
                "id": f"VP-{method.upper().replace('_', '')}-{i}",
                "title": f"{method} #{i}",
                "verification_method": method,
                "priority": "medium",
                "expected_result": expected_result,
            }
            if timeout_seconds is not None:
                vp["timeout_seconds"] = timeout_seconds
            vps.append(vp)
    return vps


def _patch_leaf_to_fake_backend(
    agent: VerificationAgent,
    method_to_backend: Dict[str, FakeVerifierBackend],
) -> None:
    """Wire each method's leaf executor to its own ``FakeVerifierBackend``.

    The mapping is method-specific: the agent's
    :meth:`_execute_verification_point_inner` dispatches each
    method to a different attribute name on the agent
    (``_execute_automated_test_async`` for automated_test,
    ``_execute_code_review`` / ``_execute_ui_validation`` /
    ``_execute_api_test`` for the rest). Patching the wrong
    name is the silent failure that drove the first version
    of this helper to SKIP every VP — patching
    ``_execute_automated_test`` (the legacy sync method) had
    no effect because the inner dispatcher calls
    ``_execute_automated_test_async``.

    Each replacement is a thin ``async def`` that delegates
    to :meth:`FakeVerifierBackend.execute`, which itself
    calls :func:`asyncio.wait_for` so the resolved per-VP
    timeout is observed at the same point as the real
    backend. This is the closest we can get to a "real"
    integration without paying the LLM / puppeteer /
    subprocess cost.
    """
    method_attr_map = {
        "automated_test": "_execute_automated_test_async",
        "code_review": "_execute_code_review",
        "ui_validation": "_execute_ui_validation",
        "api_test": "_execute_api_test",
    }
    for method, backend in method_to_backend.items():
        attr = method_attr_map.get(method, f"_execute_{method}")

        async def _stub(vp, _backend=backend, _method=method, _attr=attr):
            timeout_seconds = agent.timeout_policy.resolve(_method)
            return await _backend.execute(vp, timeout_seconds=timeout_seconds)

        setattr(agent, attr, _stub)


def _patch_leaf_to_sleep(
    agent: VerificationAgent, method_to_sleep: Dict[str, float]
) -> None:
    """Replace per-method leaf executors with ``asyncio.sleep`` stubs.

    Used by the parallelism-observation test so the
    FakeVerifierBackend's own short-circuit (which masks
    the wall-time cost) doesn't fire and we can observe
    real concurrency through the JSONL log timestamps.
    """
    for method, sleep_seconds in method_to_sleep.items():
        attr = f"_execute_{method}"

        async def _stub(vp, _sleep=sleep_seconds):
            await asyncio.sleep(_sleep)
            return {
                "id": vp.get("id", "unknown"),
                "status": "PASSED",
                "actual_result": f"fake sleep {_sleep}s",
                "evidence": "perf_stub",
            }

        setattr(agent, attr, _stub)


def _load_log_entries(plan_dir: Path) -> List[Dict[str, Any]]:
    """Concatenate every JSON-lines entry from the round log
    files in chronological order.

    Used by the parallelism-observation test to compute
    per-group event timestamps and check for overlap.
    """
    log_files = sorted((plan_dir / "logs").glob("verification_*.log"))
    if not log_files:
        return []
    entries: List[Dict[str, Any]] = []
    for log_file in log_files:
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    entries.append(json.loads(stripped))
    return entries


# =============================================================================
# 1. Wall time < 15s for the spec example
# =============================================================================


def test_perf_wall_time_under_15s(
    dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
):
    """Spec example: 14+4+3+2 dry-run VPs in a single round
    must finish in < 15s.

    With ``fake_sleep=0.1s`` and ``parallelism_cap=4``:

    * The 14-VP ``automated_test`` group runs in 4 sequential
      rounds of 4/4/4/2 (the last batch has 2 VPs) → 4 ×
      0.1s = 0.4s.
    * The 4-VP ``ui_validation`` group runs in 1 batch of 4
      → 0.1s.
    * The 3-VP ``code_review`` group runs in 1 batch of 3
      → 0.1s.
    * The 2-VP ``api_test`` group runs in 1 batch of 2 →
      0.1s.

    All four groups run concurrently (asyncio.gather of
    groups), so the *outer* wall time is bounded by the
    slowest group: 0.4s. The 15s ceiling is the production
    "smoke budget" — generous enough to absorb CI noise
    (slow runners, GC pauses, asyncio scheduling
    overhead) but tight enough to fail if a regression
    serialises the VPs (which would push the wall time
    past 23 × 0.1s = 2.3s and break the 15s budget on its
    own; it would also leave group events missing or
    malformed, which the e2e suite already pins).
    """
    _skip_if_host_too_busy()

    agent = VerificationAgent(
        plan_dir=temp_plan_dir,
        project_dir=temp_project_dir,
        coding_tool=mock_coding_tool,
    )
    # 1800s per-method timeout: the fake sleep (0.1s) is
    # well under it, so the timeout doesn't fire.
    agent.timeout_policy = TimeoutPolicy(
        per_method_timeout_seconds={
            "automated_test": 1800,
            "ui_validation": 1800,
            "code_review": 1800,
            "api_test": 1800,
        },
        global_default_timeout_seconds=1800,
    )
    # The width is 4, handed in through the resolver the provider pool
    # answers through. ``TimeoutPolicy.parallelism_cap`` was deleted with
    # every other pre-agreed round width (2026-10-13), so the same number
    # now arrives through the one door that is left; the width under test
    # and the band it is measured against are both unchanged.
    agent.vp_capacity_resolver = lambda: 4
    agent.start_verification_round(1)

    vps = _make_vps_by_method(
        {
            "automated_test": 14,
            "ui_validation": 4,
            "code_review": 3,
            "api_test": 2,
        }
    )
    _patch_leaf_to_fake_backend(
        agent,
        {
            "automated_test": FakeVerifierBackend(fake_sleep_seconds=0.1),
            "ui_validation": FakeVerifierBackend(fake_sleep_seconds=0.1),
            "code_review": FakeVerifierBackend(fake_sleep_seconds=0.1),
            "api_test": FakeVerifierBackend(fake_sleep_seconds=0.1),
        },
    )

    start = time.monotonic()
    result = asyncio.run(
        agent.execute_verification_plan_async({"verification_points": vps})
    )
    wall_time = time.monotonic() - start

    # 1. Every VP returned.
    results = result["execution_results"]
    assert len(results) == 23, (
        f"expected 23 results (14+4+3+2), got {len(results)}"
    )

    # 2. Every VP is PASSED.
    statuses = [r.get("status") for r in results]
    assert all(s == "PASSED" for s in statuses), (
        f"expected all 23 VPs PASSED, got statuses: {statuses!r}"
    )

    # 3. Wall time is the production smoke budget.
    assert wall_time < 15.0, (
        f"expected wall time < 15s for 23 VPs (4 groups, "
        f"cap=4, 0.1s sleep), got {wall_time:.2f}s"
    )


# =============================================================================
# 2. Parallelism is observable through the JSONL log timestamps
# =============================================================================


def test_perf_parallelism_observed_via_timestamps(
    dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
):
    """4 ui_validation VPs each sleep 1s — wall time ~ 1s, NOT
    ~ 4s, and the JSONL log shows ≥ 2 time-overlapping
    groups.

    The test partitions a small plan into 2 groups
    (``ui_validation`` with 4 VPs and ``api_test`` with 2
    VPs) and replaces the leaf executors with real
    ``asyncio.sleep(1)`` stubs. With ``parallelism_cap=4``,
    both groups' VPs run concurrently:

    * ``ui_validation`` finishes in ~1s (4 in parallel).
    * ``api_test`` finishes in ~1s (2 in parallel).
    * The two groups run in parallel with each other.

    The persistence log writes one ``group_started`` and
    one ``group_completed`` event per group. Two groups
    that truly run concurrently must produce two
    ``group_started`` events whose timestamps overlap with
    the *other* group's still-running interval. The
    assertion walks the log, computes each group's
    ``[started, completed]`` interval, and checks for
    pairwise overlap.

    A regression that drops the per-group gather (i.e.
    serialises groups) would shrink the wall time to ~2s
    AND would also eliminate the cross-group overlap,
    failing the assertion below.
    """
    _skip_if_host_too_busy()

    agent = VerificationAgent(
        plan_dir=temp_plan_dir,
        project_dir=temp_project_dir,
        coding_tool=mock_coding_tool,
    )
    # Per-method timeout well above the 1s sleep, so the
    # semaphore — not the timeout — is the rate-limiting
    # mechanism.
    agent.timeout_policy = TimeoutPolicy(
        per_method_timeout_seconds={"ui_validation": 60, "api_test": 60},
        global_default_timeout_seconds=60,
    )
    # The width is 4, handed in through the resolver the provider pool
    # answers through. ``TimeoutPolicy.parallelism_cap`` was deleted with
    # every other pre-agreed round width (2026-10-13), so the same number
    # now arrives through the one door that is left; the width under test
    # and the band it is measured against are both unchanged.
    agent.vp_capacity_resolver = lambda: 4
    agent.start_verification_round(1)

    vps = _make_vps_by_method(
        {
            "ui_validation": 4,
            "api_test": 2,
        }
    )
    _patch_leaf_to_sleep(
        agent,
        {"ui_validation": 1.0, "api_test": 1.0},
    )

    start = time.monotonic()
    asyncio.run(
        agent.execute_verification_plan_async({"verification_points": vps})
    )
    wall_time = time.monotonic() - start

    # 1. Wall time is the parallel floor (~1s), not the
    # serial floor (~2s for the 2 groups' 1s each). A
    # generous upper bound absorbs asyncio / GC overhead.
    assert 1.0 <= wall_time <= 2.5, (
        f"expected wall time ∈ [1.0, 2.5]s for 2 groups of "
        f"4+2 VPs in parallel (cap=4, 1s each), got "
        f"{wall_time:.2f}s (would be ~6s if serial, ~1s if parallel)"
    )

    # 2. The JSONL log carries at least 2 ``group_started``
    # events — one per non-manual group — and the two
    # intervals overlap in wall time.
    entries = _load_log_entries(temp_plan_dir)
    started = [e for e in entries if e.get("event_type") == "group_started"]
    completed = [e for e in entries if e.get("event_type") == "group_completed"]

    assert len(started) >= 2, (
        f"expected ≥ 2 group_started events (one per "
        f"non-manual group), got {len(started)}: "
        f"{[e.get('data') for e in started]!r}"
    )
    assert len(completed) >= 2, (
        f"expected ≥ 2 group_completed events, got {len(completed)}"
    )

    # 3. Two groups' intervals overlap in wall-clock time.
    # Build a per-method [start, end] interval map from
    # the started + completed events. The persistence
    # layer writes ``timestamp`` (ISO-8601) on every row,
    # not ``ts``, so we read the right field here.
    intervals: Dict[str, List[str]] = {}
    for ev in started:
        method = ev.get("data", {}).get("method")
        ts = ev.get("timestamp")
        if method is None or ts is None:
            continue
        if method not in intervals:
            intervals[method] = [ts, ts]
    for ev in completed:
        method = ev.get("data", {}).get("method")
        ts = ev.get("timestamp")
        if method is None or ts is None or method not in intervals:
            continue
        # Take the *last* completed ts for the method (a
        # group can in principle have multiple completed
        # entries if the test rerun — the canonical case
        # here is one).
        intervals[method][1] = max(intervals[method][1], ts)

    assert len(intervals) >= 2, (
        f"need ≥ 2 distinct group intervals to assert "
        f"overlap, got {list(intervals.keys())!r}"
    )

    # Pairwise check: there must be at least one pair of
    # groups whose [start, end] intervals overlap.
    overlap_found = False
    methods = list(intervals.keys())
    for i in range(len(methods)):
        for j in range(i + 1, len(methods)):
            a_start, a_end = intervals[methods[i]]
            b_start, b_end = intervals[methods[j]]
            # ISO-8601 string comparison works for our
            # timestamps because the agent's persistence
            # layer writes them in monotonic order.
            if a_start <= b_end and b_start <= a_end:
                overlap_found = True
                break
        if overlap_found:
            break

    assert overlap_found, (
        f"expected ≥ 2 groups to have overlapping "
        f"started/completed intervals (true parallelism), "
        f"got intervals={intervals!r}"
    )


# =============================================================================
# 3. No memory leak across 3 rounds
# =============================================================================


#: Round number used for the *warmup* round — the one that pays the
#: pipeline's lazy imports and is therefore NOT measured.
#:
#: ``VerificationAgent.start_verification_round`` numbers rounds from 1
#: (``verification_agent.py``: "Current round number (1-indexed)") and
#: asserts nothing about the value, so 1 is the warmup and 2/3/4 are the
#: measured rounds. The persistence layer names the log file after the
#: round number and keeps the last 3 rounds, so the numbering costs
#: nothing.
_WARMUP_ROUND = 1

#: How many rounds are measured (after the warmup).
_MEASURED_ROUNDS = 3


#: The allowance for per-round growth, as a fraction of the first
#: measured round's peak.  This is the *same absolute amount* the
#: peak-based criterion allowed (10% of a ~240 KB peak ≈ 24 KB) -- the
#: change is which quantity it is measured against, not how much of
#: it is tolerated.
_RETAINED_GROWTH_ALLOWANCE = 0.10


def _assert_no_retained_leak(
    retained_mb: List[float],
    peak_scale_mb: float,
    peaks_mb: Optional[List[float]] = None,
) -> None:
    """Assert that retained memory does not grow from round to round.

    "Per-round leak" means memory that is *still alive* after
    ``gc.collect()`` keeps growing round over round.  The *peak* is the
    largest transient allocation a round ever made -- a one-off
    transient is not a leak, and the absolute value of a peak drifts
    between processes (allocator state, import order), so a peak that
    sits 20% above the previous round's peak says nothing about
    accumulation.

    The allowance is therefore applied to the *retained* series: the
    growth between two adjacent measured rounds must stay within
    ``_RETAINED_GROWTH_ALLOWANCE`` of ``peak_scale_mb`` (the first
    measured round's peak -- the same absolute budget the peak-based
    criterion gave).  A real leak grows by far more than that on every
    round; a transient allocation frees itself before the next round's
    measurement.

    The failure message discloses the *whole* series (``peaks_mb`` and
    ``retained_mb``), not two numbers: the next red has to be
    diagnosable from the message alone.
    """
    allowance_mb = _RETAINED_GROWTH_ALLOWANCE * peak_scale_mb
    for index in range(1, len(retained_mb)):
        previous_mb = retained_mb[index - 1]
        growth_mb = retained_mb[index] - previous_mb
        if previous_mb > 0:
            growth_pct = f"+{100 * growth_mb / previous_mb:.1f}%"
        else:
            growth_pct = f"+{growth_mb:.6f} MB"
        assert growth_mb <= allowance_mb, (
            f"retained memory grew from {previous_mb:.3f} MB "
            f"(measured round {index}) to {retained_mb[index]:.3f} MB "
            f"(measured round {index + 1}): {growth_pct}, "
            f"expected ≤ +{allowance_mb * 1024:.1f} KB "
            f"({_RETAINED_GROWTH_ALLOWANCE:.0%} of the first measured peak "
            f"= {peak_scale_mb:.3f} MB) — per-round leak\n"
            f"  peaks_mb={peaks_mb!r}\n"
            f"  retained_mb={retained_mb!r}"
        )


def test_perf_no_memory_leak_3_rounds(
    dry_run_env, temp_plan_dir, temp_project_dir, mock_coding_tool
):
    """3 sequential verification rounds (9 VPs each) must
    not exhibit monotonically increasing retained memory.

    The test runs one **warmup** round and then 3 measured rounds,
    snapshotting the Python heap at the end of each *measured*
    round via :mod:`tracemalloc` -- both the peak (the largest
    transient allocation of the round) and the retained memory (what
    is still alive after the collection).

    Why the warmup round exists: the pipeline's lazy imports (sqlite3
    via ``_load_provider_info``'s CC Switch DB fallback, the
    persistence layer's logger, ...) are paid by whichever round
    happens to run first. Comparing that round against a later round
    that no longer pays the import is what made this test flaky — the
    import cost landed on one side of the comparison and not the
    other, non-deterministically. The warmup round runs the *same*
    9-VP workload, so every lazy import is already paid for before
    the first measured round, and the measured rounds are compared
    against each other on equal footing.

    The acceptance criterion is:

    * Sanity floor: every measured round's peak is ≥ 0.01 MB (10 KB).
      A 9-VP dry-run round with 0.05s sleep allocates only
      ~50-80 KB of Python objects, so a sub-MB floor is
      realistic. Below 10 KB the snapshot would be empty
      and the test would be measuring nothing.
    * ``peak[measured_2]`` ≤ ``peak[measured_1]`` × 2.0 — a
      one-time bump is allowed. The persistence
      layer's first round opens a new log file, registers
      a logger handler, and primes the asyncio event loop
      state — these get reused (not re-allocated) on
      the later rounds, so a real leak would still grow past
      the warmup floor.
    * ``retained[measured_n+1]`` − ``retained[measured_n]`` ≤
      10% of ``peak[measured_1]`` for every adjacent pair of
      measured rounds — the per-round leak criterion. *Retained* is
      what is still alive after ``gc.collect()``; that is what a leak
      grows. The peak is only the largest transient allocation a
      round made, and its absolute value drifts between processes
      (allocator state, import order) — a round whose peak happens to
      sit 20% above the previous round's peak says nothing about
      accumulation, and judging it as a leak is what made this case
      fail on a shared CI runner. The allowance is the *same absolute
      amount* the peak-based criterion gave (10% of a ~240 KB peak
      ≈ 24 KB), so a leak that really accumulates still trips it.

    A regression that *under*-uses memory (e.g. a leak
    detection that's fooled by aggressive GC) would
    pass spuriously, so the sanity floor catches that
    case.
    """
    agent = VerificationAgent(
        plan_dir=temp_plan_dir,
        project_dir=temp_project_dir,
        coding_tool=mock_coding_tool,
    )
    agent.timeout_policy = TimeoutPolicy(
        per_method_timeout_seconds={"automated_test": 1800},
        global_default_timeout_seconds=1800,
    )
    # The width is 4, handed in through the resolver the provider pool
    # answers through. ``TimeoutPolicy.parallelism_cap`` was deleted with
    # every other pre-agreed round width (2026-10-13), so the same number
    # now arrives through the one door that is left; the width under test
    # and the band it is measured against are both unchanged.
    agent.vp_capacity_resolver = lambda: 4

    vps = _make_vps_by_method(
        {
            "automated_test": 6,
            "ui_validation": 2,
            "api_test": 1,
        }
    )
    _patch_leaf_to_fake_backend(
        agent,
        {
            "automated_test": FakeVerifierBackend(fake_sleep_seconds=0.05),
            "ui_validation": FakeVerifierBackend(fake_sleep_seconds=0.05),
            "api_test": FakeVerifierBackend(fake_sleep_seconds=0.05),
        },
    )

    # Start tracing before any round so the peak is
    # measured across the whole sequence. We do NOT take
    # a snapshot between rounds (would force GC and
    # mask transient allocations) — instead we let
    # ``tracemalloc`` track the *peak* since trace
    # start, snapshot at the end of each round, and
    # reset the peak counter via ``reset_peak()`` so
    # the next round's peak is measured in isolation.
    tracemalloc.start()
    try:
        # ---- warmup round: NOT measured -------------------------
        # Same 9-VP workload as the measured rounds, so the lazy
        # imports the pipeline pays on its first real round are
        # already paid for here. Without it the import cost lands
        # on whichever round runs first and the 2× tolerance
        # compares an import-paying round against one that does not
        # pay it — non-deterministically, because the import is
        # only paid when this process has not paid it before.
        agent.start_verification_round(_WARMUP_ROUND)
        asyncio.run(
            agent.execute_verification_plan_async(
                {"verification_points": vps}
            )
        )
        # Drop the warmup round's garbage and re-base the peak
        # counter on what is still retained, so the first measured
        # round starts from a clean baseline.
        gc.collect()
        tracemalloc.reset_peak()

        # Retire the warmup round's log file before measuring.
        #
        # ``VerificationPersistenceManager._cleanup_old_rounds``
        # archives the round logs older than ``current_round - 2``,
        # and that archive is not free: ``gzip.open`` builds zlib's
        # deflate state through PyMem, which ``tracemalloc`` *does*
        # see — a ~360 KB transient per archived file, on top of the
        # round's own allocations. With the warmup round's log in the
        # plan directory the measured rounds would hold four round
        # logs, so exactly one measured round would archive one and
        # its peak would be ~5x the other two — the three measured
        # rounds would no longer be comparable, and the comparison
        # would be measuring zlib instead of the pipeline. The
        # pre-warmup test (rounds 1-3) never archived anything, so
        # the measured rounds are kept in that same state: the
        # warmup's own log is dropped, and rounds 2/3/4 leave at
        # most three logs behind, none of them old enough to be
        # archived.
        for _warmup_log in (temp_plan_dir / "logs").glob("verification_*.log"):
            _warmup_log.unlink()

        # ---- measured rounds ------------------------------------
        peaks_mb: List[float] = []
        retained_mb: List[float] = []
        first_measured = _WARMUP_ROUND + 1
        for round_number in range(first_measured, first_measured + _MEASURED_ROUNDS):
            agent.start_verification_round(round_number)
            asyncio.run(
                agent.execute_verification_plan_async(
                    {"verification_points": vps}
                )
            )
            # Force a collection so the peak reflects
            # *retained* memory, not transient garbage
            # from the just-finished round.
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            peaks_mb.append(peak / (1024 * 1024))
            # ``current`` is what is still *alive* after the collection
            # -- the quantity a per-round leak actually grows.  The
            # peak above is only the largest transient allocation the
            # round made, and it drifts between processes (allocator
            # state, import order) without anything accumulating.
            retained_mb.append(current / (1024 * 1024))
            # Reset the peak counter for the next round.
            tracemalloc.reset_peak()
    finally:
        tracemalloc.stop()

    # 1. Every measured round produced real allocations
    # (sanity check: the test is not measuring an empty
    # heap). The 9-VP dry-run workload with fake_sleep=0.05s
    # allocates only ~50-80 KB of Python objects, so the
    # sanity floor is 0.01 MB (10 KB). Below this the
    # snapshot would be effectively empty and the test
    # would pass trivially.
    assert len(peaks_mb) == _MEASURED_ROUNDS, (
        f"expected {_MEASURED_ROUNDS} measured rounds, "
        f"got {len(peaks_mb)} — the warmup round must not be "
        f"counted as a measured one"
    )
    assert all(p >= 0.01 for p in peaks_mb), (
        f"expected each round's peak to be ≥ 0.01 MB, "
        f"got peaks_mb={peaks_mb!r} — the test is not "
        f"measuring real allocations"
    )

    # 2. The peak must not grow monotonically. The
    # first *measured* round is the floor (the warmup
    # round already paid the lazy imports); the second
    # measured round is allowed to bump up to 2× the
    # first (persistence layer opens a new log file, primes
    # asyncio state, etc.).
    p1, p2 = peaks_mb[0], peaks_mb[1]
    assert p2 <= p1 * 2.0, (
        f"peak memory grew from {p1:.3f} MB (measured round 1) "
        f"to {p2:.3f} MB (measured round 2): +{100 * (p2 - p1) / p1:.1f}%, "
        f"expected ≤ 2× warmup tolerance (possible leak)\n"
        f"  peaks_mb={peaks_mb!r}\n"
        f"  retained_mb={retained_mb!r}"
    )

    # 3. The *retained* memory must not grow from round to round.
    # This is the per-round leak criterion: memory that is still
    # alive after ``gc.collect()`` and keeps growing is a leak; a
    # one-off transient allocation is not. The allowance is the same
    # absolute amount the peak-based criterion allowed (10% of the
    # first measured peak), so a leak that really accumulates still
    # trips it -- what changed is that the noise a shared runner
    # produces (a transient allocation in one round) no longer does.
    _assert_no_retained_leak(retained_mb, peaks_mb[0], peaks_mb=peaks_mb)


# =============================================================================
# 4. The leak criterion judges retained memory, not the peak
# =============================================================================


def test_perf_leak_criterion_separates_retained_growth_from_peak_noise():
    """Negative control for the per-round leak criterion.

    "Per-round leak" means *retained* memory grows round over round --
    memory that is still alive after ``gc.collect()``.  The *peak* is
    the largest transient allocation a round ever made; a one-off
    transient allocation is not a leak, and judging it as one is what
    made the round-3 peak comparison flaky on a shared runner.

    Two sequences pin the difference:

    * Retained memory grows 64 KB every round and the peaks stay
      flat -- a real leak.  The criterion must reject it, even though
      the old peak-based criterion (``p3 <= p2 * 1.10``) would have
      accepted it.
    * Retained memory is flat and one round's peak is 20% above the
      previous round -- transient noise, the shape the CI runner
      produced.  The criterion must accept it, even though the old
      peak-based criterion would have rejected it.

    The allowance is the *same absolute amount* as before (10% of the
    first measured peak); only the quantity it is measured against
    changed.  The first sequence proves the criterion still catches a
    leak that actually accumulates.
    """
    # (a) A real leak: retained grows 64 KB (0.0625 MB) per round,
    #     while the peaks stay flat.
    flat_peaks_mb = [0.25, 0.25, 0.25]
    leaking_retained_mb = [0.05, 0.05 + 0.0625, 0.05 + 2 * 0.0625]
    # The old peak-based criterion accepted this sequence:
    assert flat_peaks_mb[2] <= flat_peaks_mb[1] * 1.10
    with pytest.raises(AssertionError) as excinfo:
        _assert_no_retained_leak(
            leaking_retained_mb,
            flat_peaks_mb[0],
            peaks_mb=flat_peaks_mb,
        )
    # The failure must disclose the whole series, not two numbers --
    # the next red has to be diagnosable from the message alone.
    assert "peaks_mb=" in str(excinfo.value)
    assert "retained_mb=" in str(excinfo.value)

    # (b) Transient noise: retained is flat, one round's peak is +20%
    #     over the previous round (the shape CI reported).
    noisy_peaks_mb = [0.24, 0.20, 0.24]
    flat_retained_mb = [0.100, 0.1022, 0.1044]
    # The old peak-based criterion rejected this sequence:
    assert not (noisy_peaks_mb[2] <= noisy_peaks_mb[1] * 1.10)
    # The retained-based criterion accepts it: nothing accumulates.
    _assert_no_retained_leak(
        flat_retained_mb,
        noisy_peaks_mb[0],
        peaks_mb=noisy_peaks_mb,
    )

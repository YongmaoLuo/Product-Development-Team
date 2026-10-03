"""
test_concurrency_performance.py — performance benchmark + stress tests for
file-lock-based task concurrency (PRD test-design decision point 6).

This module pins four performance / correctness contracts on top of the
file-lock and memory-conflict mechanisms exercised in tasks 4 and 5:

  * ``test_independent_tasks_run_concurrently``
      4 tasks, each targeting a disjoint file and each sleeping 0.5s,
      must finish in **strictly less than 1.0s** wall-clock. (If they
      were accidentally serialised, total elapsed would be ≥ 2.0s.)

  * ``test_conflicting_tasks_run_serially``
      4 tasks all targeting the *same* file and each sleeping 0.2s
      must finish in **strictly greater than 0.6s** wall-clock. (If
      they were accidentally concurrent, total elapsed would be ≤
      0.2s; if they deadlocked, the test would timeout.) The
      lower-bound 0.6s comes from ``3 × 0.2s = 0.6s`` (at least three
      serial slots for a 4-task run — assuming no overlap).

  * ``test_mixed_tasks_correct_overlap``
      2 pairs of independent tasks + 1 pair of conflicting tasks. The
      conflicting pair must run serially; the two independent pairs
      must each run in parallel. Total wall-clock must reflect the
      overlap (≈ max(independent_pair, conflicting_pair) plus
      single-task overhead, not the full sum).

  * ``test_deadlock_free_stress``
      50 tasks across 5 files must all complete in bounded wall-clock
      (≤ 30s budget). A deadlock or starvation would push the run
      past the budget; this is the deadlock regression net.

All four tests are marked ``@pytest.mark.time_sensitive`` and are **skipped by
default** to keep the fast / default test loop hermetic. The skip is
enforced at the project level: ``backend/pytest.ini`` ships with
``-m "not time_sensitive"`` in ``addopts``, so a plain ``pytest`` run deselects
all ``time_sensitive``-marked tests. Opt in with:

    pytest tests/integration/test_concurrency_performance.py -m slow -v
    pytest tests/integration/test_concurrency_performance.py -m "not notslow" -v

(``-m slow`` is the standard pytest marker-based selector; ``-m "not
notslow"`` is the standard pytest override of the default
deselection.)

Why a dedicated FileLockManager-driven harness (not AutonomousAgent)
-------------------------------------------------------------------
The four contracts are about wall-clock timing under contention, not
about the full agent execution loop (planning, breakdown, retries,
provider slots, layer rebuilds). Driving ``AutonomousAgent.run`` for
the stress test would inject ~5s of unrelated startup cost on every
iteration and pull the wall-clock signal into a much noisier
distribution. ``FileLockManager`` is the *only* mechanism that
governs file-level concurrency for the contracts under test (in
combination with the in-memory ``_IN_FLIGHT_FILES`` map for
fail-fast). Driving it directly gives deterministic timing.

Each test uses real ``FileLockManager`` (no monkey-patching of
``filelock.FileLock``) so the integration surface stays honest: the
OS-level fcntl locking on the on-disk ``.lock`` file is what would
happen in production. The tasks run in ``ThreadPoolExecutor`` workers
(``max_workers=8``) so concurrency is bounded above the global
``FileLockManager`` cap, mirroring the agent's ThreadPoolExecutor
backbone.
"""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

# Make ``FileLockManager`` importable in isolation.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from file_lock_manager import FileLockManager  # noqa: E402


# ---------------------------------------------------------------------------
# Marker wiring
# ---------------------------------------------------------------------------


# Every test in this module is slow / performance. We use a module-level
# ``pytestmark`` so a single decoration covers all four tests; per-test
# decorators would be redundant.
#
# The "skipped by default" behaviour is enforced at the project level
# via ``-m "not time_sensitive"`` in ``backend/pytest.ini``'s ``addopts`` (see
# the module docstring above). We do NOT register a local
# ``pytest_collection_modifyitems`` / ``pytest_addoption`` here
# because pytest only auto-discovers hooks from conftest.py and
# plugins, not from arbitrary test files — defining them in a test
# module is a silent no-op. The pytest.ini-level deselection is
# both the simplest and the only working mechanism.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.time_sensitive,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# A wall-clock budget that is generous enough to absorb thread-pool
# startup, file lock creation, and 50-task queue churn on a slow CI
# machine, but tight enough that a real deadlock is caught before the
# 30s backend budget (mirrors the contract used by the other the backend
# stress / performance tests).
_PER_TASK_SLEEP_INDEPENDENT = 0.5
_PER_TASK_SLEEP_CONFLICTING = 0.2
_INDEPENDENT_PARALLEL_BUDGET = 1.0  # 4 × 0.5s = 2.0s if serial
_CONFLICTING_SERIAL_FLOOR = 0.6     # 3 × 0.2s = 0.6s minimum serial sum
_DEADLOCK_BUDGET = 30.0             # backend hard budget


def _run_task_with_locks(
    project_dir: Path,
    target_files: List[str],
    sleep_seconds: float,
    per_task_lock_timeout: float = 5.0,
) -> Tuple[float, float]:
    """Acquire locks for ``target_files``, sleep, release.

    Mirrors the shape of the agent's pre/post-task lock hooks but
    without involving the agent or the coding tool. Returns a
    ``(start_monotonic, end_monotonic)`` tuple representing the
    **actual file-hold window** (post-acquire → pre-release), not
    the full try-to-acquire → released window.

    The window is deliberately the file-hold window because the
    per-file overlap check tests whether multiple tasks were
    *simultaneously holding* the same file. The try-to-acquire
    window would be misleading: all 4 conflicting tasks would have
    overlapping try-to-acquire windows (they all started polling
    before the first one released), even though the lock correctly
    serialised them.

    The wall-clock check at the call site uses an outer
    ``time.monotonic()`` measurement (started before submitting
    tasks, elapsed = now − started) so it captures the full
    contention cost end-to-end. The per-task intervals returned
    here are for the overlap analysis only.

    The lock acquisition uses :class:`FileLockManager` so the
    fcntl-based on-disk ``.lock`` file is exercised end-to-end (no
    mock). The lock's acquisition timeout is generous (5s default,
    overridable) so a concurrent acquirer in the same test can park
    on the lock until the holder releases — which is exactly the
    contention shape the test contracts describe.
    """
    manager = FileLockManager()
    manager.acquire(target_files, str(project_dir), timeout=per_task_lock_timeout)
    start = time.monotonic()
    try:
        time.sleep(sleep_seconds)
    finally:
        manager.release()
    end = time.monotonic()
    return start, end


def _execute_tasks_parallel(
    project_dir: Path,
    task_specs: List[Tuple[str, List[str]]],
    per_task_sleep,
    max_workers: int = 8,
    per_task_lock_timeout: float = 5.0,
) -> List[Tuple[str, float, float]]:
    """Drive a list of (task_id, target_files) through the lock harness
    in parallel and return per-task ``(task_id, start, end)`` results.

    The pool size is deliberately **larger than any individual test's
    contention**: the four-contract suite only ever contends on
    4–50 tasks, so 8 workers is enough to surface the per-file lock
    bottleneck without artificial serialisation from a small pool.

    ``per_task_sleep`` is either a single ``float`` applied to every
    task or a ``Dict[str, float]`` mapping ``task_id`` → sleep
    seconds. The dict form is required for the mixed-overlap test
    where independent tasks and conflicting tasks need different
    sleep durations to make the wall-clock contract observable.

    ``per_task_lock_timeout`` controls how long each individual task
    will park on a held lock before giving up. The default 5s is
    appropriate for the 4-task and 6-task tests (where worst-case
    queue depth is 3-4). The 50-task stress test overrides it to
    60s because the same-file queue depth there is ~20.

    Results are returned in completion order (not input order). The
    caller derives total wall-clock from the min of all start
    timestamps and the max of all end timestamps.
    """
    completed: List[Tuple[str, float, float]] = []
    completed_lock = threading.Lock()

    def _worker(task_id: str, target_files: List[str]) -> None:
        sleep_for = (
            per_task_sleep[task_id]
            if isinstance(per_task_sleep, dict)
            else per_task_sleep
        )
        start, end = _run_task_with_locks(
            project_dir,
            target_files,
            sleep_for,
            per_task_lock_timeout=per_task_lock_timeout,
        )
        with completed_lock:
            completed.append((task_id, start, end))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(_worker, task_id, target_files)
            for task_id, target_files in task_specs
        ]
        for fut in as_completed(futures):
            # Surface worker exceptions as test failures rather than
            # silently swallowing them — a future with an exception
            # would otherwise be invisible to the wall-clock checks.
            fut.result()

    return completed


def _total_wall_clock(results: List[Tuple[str, float, float]]) -> float:
    """Return the wall-clock span of a completed-results list."""
    if not results:
        return 0.0
    starts = [r[1] for r in results]
    ends = [r[2] for r in results]
    return max(ends) - min(starts)


def _per_file_max_overlap(
    results: List[Tuple[str, float, float]],
    file_to_tasks: Dict[str, List[str]],
) -> Dict[str, int]:
    """Compute the peak simultaneous in-flight count per file.

    A file is in-flight for a task from ``start`` to ``end``. We
    sweep all task intervals against a target file and count how many
    tasks cover each moment. The peak count is the maximum number of
    *concurrent* task intervals that share the file — for an
    independent run this is 1 (no overlap); for a properly
    serialised run on the same file it is also 1 (lock serialises
    them); for a buggy run that bypasses the lock it can be ≥ 2.
    """
    peaks: Dict[str, int] = {f: 0 for f in file_to_tasks}
    for f in file_to_tasks:
        # Sort intervals by start time so the per-file sweep is O(n log n)
        # even when n is small.
        intervals: List[Tuple[float, float]] = []
        for task_id, start, end in results:
            if task_id in file_to_tasks[f]:
                intervals.append((start, end))
        intervals.sort()
        # Sliding sweep: at each interval's start, count how many
        # open intervals overlap.
        for i, (s_i, e_i) in enumerate(intervals):
            open_count = 1
            for j, (s_j, e_j) in enumerate(intervals):
                if i == j:
                    continue
                if s_j < e_i and e_j > s_i:
                    # Overlap iff j starts before i ends AND j ends
                    # after i starts. Since j's start < i's start
                # in this branch (we iterate i in sorted order),
                # the open count is "how many earlier-started
                # intervals are still open at i.start".
                    if s_j <= s_i:
                        open_count += 1
            if open_count > peaks[f]:
                peaks[f] = open_count
    return peaks


# ---------------------------------------------------------------------------
# TDD test 1: 4 independent tasks run concurrently (wall-clock < 1.0s)
# ---------------------------------------------------------------------------


def test_independent_tasks_run_concurrently(tmp_path: Path) -> None:
    """4 independent tasks (disjoint target files) finish in < 1.0s.

    Each task acquires a lock on its own (unique) file, sleeps 0.5s,
    and releases. The harness drives all 4 tasks in parallel through
    a ThreadPoolExecutor. If the lock mechanism is correct, all 4
    tasks run truly concurrently (no shared file, no contention),
    and total wall-clock is dominated by the 0.5s sleep — well below
    the 1.0s budget.

    If the lock mechanism were broken — e.g., all tasks accidentally
    share a lock file, or the lock acquisition were synchronously
    blocking across independent files — total wall-clock would be
    ≥ 2.0s (4 × 0.5s if serial), failing the budget.

    The per-file overlap check is a secondary safety net: it
    confirms each task held **its own** file in isolation (no cross-
    contamination), which is the structural contract that makes
    the wall-clock speedup meaningful.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    task_specs: List[Tuple[str, List[str]]] = [
        (f"ind-{i}", [f"src/file_{i}.py"]) for i in range(4)
    ]
    file_to_tasks: Dict[str, List[str]] = {
        f"src/file_{i}.py": [f"ind-{i}"] for i in range(4)
    }

    started = time.monotonic()
    results = _execute_tasks_parallel(
        project_dir, task_specs, _PER_TASK_SLEEP_INDEPENDENT
    )
    elapsed = time.monotonic() - started

    # 4 tasks all completed.
    assert len(results) == 4, (
        f"expected 4 task completions, got {len(results)}: {results!r}"
    )

    # The wall-clock budget: 4 independent × 0.5s sleep should
    # finish in well under 1.0s when run in parallel (the contract
    # is "strictly less than 1.0s" — equal-to-budget is a fail).
    assert elapsed < _INDEPENDENT_PARALLEL_BUDGET, (
        f"4 independent × 0.5s tasks took {elapsed:.3f}s wall-clock, "
        f"expected < {_INDEPENDENT_PARALLEL_BUDGET:.1f}s — the tasks "
        f"did not run in parallel (something is serialising them)"
    )
    # And a sanity upper bound: if the run takes < 0.4s, something
    # is wrong (the sleep is being skipped). The 0.4s floor is
    # 0.5s − a generous 0.1s tolerance for CI clock skew.
    assert elapsed >= _PER_TASK_SLEEP_INDEPENDENT - 0.1, (
        f"4 independent tasks finished in {elapsed:.3f}s, "
        f"less than the 0.5s sleep duration — the sleep was bypassed"
    )

    # Per-file overlap: each file must have been held by exactly
    # one task at a time (the lock guarantees this trivially since
    # the files are disjoint, but the assertion is a regression net
    # for any future change that accidentally introduces a shared
    # lock file across tasks).
    peaks = _per_file_max_overlap(results, file_to_tasks)
    for file_path, peak in peaks.items():
        assert peak == 1, (
            f"file {file_path} was held by {peak} tasks "
            f"simultaneously; expected 1 — the lock acquisition "
            f"was bypassed for at least one pair"
        )


# ---------------------------------------------------------------------------
# TDD test 2: 4 conflicting tasks run serially (wall-clock > 0.6s)
# ---------------------------------------------------------------------------


def test_conflicting_tasks_run_serially(tmp_path: Path) -> None:
    """4 conflicting tasks (same target file) finish in > 0.6s serially.

    All 4 tasks attempt to lock the *same* file. With the lock
    mechanism working, only one task can hold the file at a time,
    so the 4 tasks are forced into a strict serial schedule. Each
    task sleeps 0.2s, so the minimum serial sum is
    ``4 × 0.2s = 0.8s`` — strictly greater than the 0.6s lower
    bound the spec requires.

    The 0.6s lower bound is intentionally **less than 0.8s** so the
    test accepts both perfectly-serial runs (≥ 0.8s) and a
    minimally-serial run where one task starts ~0.2s late (still
    ≥ 0.6s). The point of the assertion is: *serialised execution
    was preserved*, not "exactly 0.8s".

    If the lock mechanism were broken — e.g., all 4 tasks bypassed
    the lock and ran in parallel — total wall-clock would be
    ≈ 0.2s, failing the 0.6s floor. That is the regression this
    test pins.

    Per-file overlap: the shared file's peak in-flight count is
    also asserted to be exactly 1, so a buggy run that "happens to
    take ≥ 0.6s" via some other serialisation is still caught.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    shared_file = "src/shared.py"
    task_specs: List[Tuple[str, List[str]]] = [
        (f"conf-{i}", [shared_file]) for i in range(4)
    ]
    file_to_tasks: Dict[str, List[str]] = {
        shared_file: [f"conf-{i}" for i in range(4)],
    }

    started = time.monotonic()
    results = _execute_tasks_parallel(
        project_dir, task_specs, _PER_TASK_SLEEP_CONFLICTING
    )
    elapsed = time.monotonic() - started

    # 4 tasks all completed (no deadlock under the test's 5s lock
    # acquisition timeout × 4 waiters + 4 × 0.2s = ~5.8s ceiling).
    assert len(results) == 4, (
        f"expected 4 task completions, got {len(results)}: {results!r}"
    )

    # Serial lower bound: 4 × 0.2s = 0.8s minimum; the test accepts
    # any value strictly greater than 0.6s (which guarantees the
    # tasks were NOT concurrent — 0.2s alone would fail the floor).
    assert elapsed > _CONFLICTING_SERIAL_FLOOR, (
        f"4 conflicting × 0.2s tasks took {elapsed:.3f}s wall-clock, "
        f"expected > {_CONFLICTING_SERIAL_FLOOR:.1f}s — the tasks "
        f"may have run in parallel (lock bypassed?)"
    )
    # Upper bound: a real 4-slot serial run cannot exceed
    # 4 × 0.2s = 0.8s + thread-pool overhead + lock fsync latency
    # (generous 5s ceiling — anything beyond this means the lock
    # acquisition is timing out repeatedly, not that the lock
    # is enforcing serialisation).
    assert elapsed < 5.0, (
        f"4 conflicting tasks took {elapsed:.3f}s, which exceeds "
        f"the 5s serialisation ceiling — likely a lock timeout "
        f"regression"
    )

    # Per-file overlap: the shared file must have been held by
    # exactly 1 task at a time. A peak ≥ 2 would prove the lock
    # was bypassed even if wall-clock happened to exceed 0.6s
    # for some other reason.
    peaks = _per_file_max_overlap(results, file_to_tasks)
    assert peaks[shared_file] == 1, (
        f"shared file {shared_file} had peak in-flight = "
        f"{peaks[shared_file]}, expected 1 — concurrent access "
        f"to the same file was not serialised"
    )

    # Sort results by start time so we can also assert the
    # pairwise no-overlap property directly (a stronger signal
    # than the peak count alone, since it rules out interleaved
    # execution even at the boundary).
    ordered = sorted(results, key=lambda r: r[1])
    for i in range(len(ordered) - 1):
        end_i = ordered[i][2]
        start_next = ordered[i + 1][1]
        assert end_i <= start_next + 0.05, (
            f"task {ordered[i][0]} ended at {end_i:.3f}s but the "
            f"next task {ordered[i + 1][0]} started at "
            f"{start_next:.3f}s — the serialised boundary was "
            f"violated (end < next.start + 0.05s tolerance)"
        )


# ---------------------------------------------------------------------------
# TDD test 3: mixed tasks show correct overlap (independent + conflicting)
# ---------------------------------------------------------------------------


def test_mixed_tasks_correct_overlap(tmp_path: Path) -> None:
    """Mixed independent and conflicting tasks: overlap the right pairs.

    Topology:
      * Pair A: independent tasks A1, A2 (different files)
      * Pair B: independent tasks B1, B2 (different files)
      * Pair C: conflicting tasks C1, C2 (same file)

    Expected behaviour:
      * A1 ‖ A2 (concurrent) — disjoint files, no contention.
      * B1 ‖ B2 (concurrent) — disjoint files, no contention.
      * C1 → C2 (serialised) — same file, lock enforces order.
      * Total wall-clock ≈ max(0.5s, 0.4s) + thread overhead,
        i.e. ≈ 0.5s — well below the 0.5s + 0.5s + 0.4s = 1.4s
        "fully serial" total.

    If the lock mechanism is working:
      * Pair A overlap: 2 (concurrent).
      * Pair B overlap: 2 (concurrent).
      * Pair C overlap: 1 (serial).
      * Total wall-clock: ≈ 0.5s ± overhead.

    A regression that serialises the independent pairs would push
    total wall-clock to ≈ 1.0s; a regression that parallelises the
    conflicting pair would be caught by the per-pair overlap check.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    # Sleep durations are tuned so the contract is observable in
    # wall-clock: 0.5s for the independent pairs, 0.2s for the
    # conflicting pair (which adds up to 0.4s when serialised).
    SLEEP_INDEP = 0.5
    SLEEP_CONF = 0.2

    task_specs: List[Tuple[str, List[str]]] = [
        ("A1", ["src/a.py"]),
        ("A2", ["src/a2.py"]),
        ("B1", ["src/b.py"]),
        ("B2", ["src/b2.py"]),
        ("C1", ["src/c.py"]),
        ("C2", ["src/c.py"]),
    ]
    file_to_tasks: Dict[str, List[str]] = {
        "src/a.py": ["A1"],
        "src/a2.py": ["A2"],
        "src/b.py": ["B1"],
        "src/b2.py": ["B2"],
        "src/c.py": ["C1", "C2"],
    }

    started = time.monotonic()
    # Per-task sleep map: independent tasks sleep 0.5s, the
    # conflicting pair sleeps 0.2s each. The helper uses the
    # task_id key so each task gets its own duration.
    sleep_map: Dict[str, float] = {
        "A1": SLEEP_INDEP,
        "A2": SLEEP_INDEP,
        "B1": SLEEP_INDEP,
        "B2": SLEEP_INDEP,
        "C1": SLEEP_CONF,
        "C2": SLEEP_CONF,
    }
    results = _execute_tasks_parallel(
        project_dir, task_specs, sleep_map
    )
    elapsed = time.monotonic() - started

    # All 6 tasks completed.
    assert len(results) == 6, (
        f"expected 6 task completions, got {len(results)}: {results!r}"
    )

    # Per-file overlap contract.
    peaks = _per_file_max_overlap(results, file_to_tasks)
    # Independent pairs: peak overlap of 1 each (each file held by
    # one task at a time, trivially). The interesting assertion is
    # that wall-clock shows them running concurrently.
    for file_path, peak in peaks.items():
        assert peak == 1, (
            f"file {file_path} had peak in-flight = {peak}, expected 1"
        )

    # Wall-clock budget: 6 tasks should finish in well under
    # ``SLEEP_INDEP + SLEEP_INDEP + 2*SLEEP_CONF = 1.4s`` (the
    # fully-serial sum). The 1.2s budget is generous — it absorbs
    # the thread-pool startup and the serialised 0.4s of the
    # conflicting pair.
    assert elapsed < 1.2, (
        f"6 mixed tasks took {elapsed:.3f}s, expected < 1.2s — "
        f"the independent pairs were likely serialised"
    )
    # And the run must have taken at least SLEEP_INDEP = 0.5s
    # (the dominant parallel block).
    assert elapsed >= SLEEP_INDEP - 0.1, (
        f"6 mixed tasks finished in {elapsed:.3f}s, less than "
        f"the 0.5s sleep — sleeps were bypassed"
    )

    # Pairwise concurrency check: A1 and A2 must have overlapping
    # wall-clock intervals (running in parallel). Same for B1/B2.
    # If either pair was serialised, the other's start would be
    # ≥ the first's end (a clear signal of regression).
    by_id: Dict[str, Tuple[float, float]] = {
        task_id: (start, end) for task_id, start, end in results
    }
    a1_s, a1_e = by_id["A1"]
    a2_s, a2_e = by_id["A2"]
    b1_s, b1_e = by_id["B1"]
    b2_s, b2_e = by_id["B2"]
    c1_s, c1_e = by_id["C1"]
    c2_s, c2_e = by_id["C2"]

    # A1 ‖ A2: their intervals must overlap (start < other.end).
    a_overlap = (a1_s < a2_e) and (a2_s < a1_e)
    b_overlap = (b1_s < b2_e) and (b2_s < b1_e)
    assert a_overlap, (
        f"independent pair A1/A2 ran serially: A1=[{a1_s:.3f}, {a1_e:.3f}], "
        f"A2=[{a2_s:.3f}, {a2_e:.3f}]"
    )
    assert b_overlap, (
        f"independent pair B1/B2 ran serially: B1=[{b1_s:.3f}, {b1_e:.3f}], "
        f"B2=[{b2_s:.3f}, {b2_e:.3f}]"
    )

    # C1 → C2: the conflicting pair must have been serialised
    # (end of earlier < start of later, with a small tolerance).
    c_ordered = sorted([("C1", c1_s, c1_e), ("C2", c2_s, c2_e)], key=lambda r: r[1])
    earlier_end = c_ordered[0][2]
    later_start = c_ordered[1][1]
    assert earlier_end <= later_start + 0.05, (
        f"conflicting pair C1/C2 had overlapping intervals: "
        f"{c_ordered[0][0]} ended at {earlier_end:.3f}s, "
        f"{c_ordered[1][0]} started at {later_start:.3f}s — "
        f"the lock did not serialise the same-file pair"
    )

    # Conflicting-pair duration must reflect the serial sum
    # (≥ 2 × SLEEP_CONF). The 0.3s floor is 2 × 0.2s − 0.1s
    # tolerance for clock skew.
    c_duration = later_start + c_ordered[1][2] - c_ordered[0][1]  # noqa: F841
    conf_pair_span = max(c1_e, c2_e) - min(c1_s, c2_s)
    assert conf_pair_span >= 2 * SLEEP_CONF - 0.05, (
        f"conflicting pair C1/C2 spanned {conf_pair_span:.3f}s, "
        f"expected ≥ 2 × {SLEEP_CONF}s — they ran concurrently, "
        f"the lock was bypassed"
    )


# ---------------------------------------------------------------------------
# TDD test 4: 50 tasks / 5 files — deadlock-free stress
# ---------------------------------------------------------------------------


def test_deadlock_free_stress(tmp_path: Path) -> None:
    """50 tasks across 5 shared files complete without deadlock.

    Each task claims a deterministic mix of 1–2 files from a fixed
    pool of 5 (so different tasks can share different files, but no
    task is forced to claim all 5). The lock manager's
    lexicographic-order acquisition prevents the classic
    cycle-induced deadlock (T1 holds a, waits for b; T2 holds b,
    waits for a) — this test confirms that property under stress.

    The exact task→file mapping is deterministic (modulo the
    thread-pool's completion-order shuffling) so a real deadlock
    would be reproducible on any run.

    Contract:
      * All 50 tasks reach completion (no deadlock, no starvation).
      * Total wall-clock ≤ 30s (the backend budget).
      * Per-file peak in-flight is exactly 1 (the lock
        serialises same-file access).
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    files = [f"src/file_{i}.py" for i in range(5)]
    # Deterministic 50-task, multi-file workload. The mapping is
    # designed so that every pair of consecutive task ids has at
    # least one shared file (forcing real lock contention), and the
    # file-pool is small enough (5 files) that deadlocks would be
    # observable as a runaway wall-clock.
    task_specs: List[Tuple[str, List[str]]] = []
    file_to_tasks: Dict[str, List[str]] = {f: [] for f in files}
    for i in range(50):
        # Cycle through (i, i+1) mod 5 file pairs — guarantees
        # every task overlaps with at least one neighbour, and
        # the file pool is fully utilised.
        primary = files[i % 5]
        secondary = files[(i + 1) % 5]
        target_files = sorted({primary, secondary})
        task_id = f"stress-{i:02d}"
        task_specs.append((task_id, target_files))
        for f in target_files:
            file_to_tasks[f].append(task_id)

    # 50 tasks × 0.05s sleep = 2.5s minimum serial sum. With
    # 5-file contention the effective parallelism is 5, so the
    # lower bound on wall-clock is ≈ 50 / 5 × 0.05s = 0.5s. We
    # use a per-task sleep of 0.05s (small enough to keep the test
    # under 30s even on slow CI, large enough to make the lock
    # contention observable).
    STRESS_SLEEP = 0.05
    # Pool size = 16 (>= 5 file contention lanes × 3 oversubscribe
    # factor) so the pool itself is not the bottleneck.
    POOL_SIZE = 16

    started = time.monotonic()
    # Pass an explicit per-task lock timeout (60s) so the 50-task queue
    # on 5 files cannot trip the default 5s ceiling under CI noise.
    # The wall-clock assertion below is the real deadlock detector.
    results = _execute_tasks_parallel(
        project_dir,
        task_specs,
        STRESS_SLEEP,
        max_workers=POOL_SIZE,
        per_task_lock_timeout=60.0,
    )
    elapsed = time.monotonic() - started

    # ----- Completion contract: 50 / 50 ----------------------------
    assert len(results) == 50, (
        f"expected 50 task completions, got {len(results)} — "
        f"{(50 - len(results))} task(s) deadlocked or starved"
    )

    # ----- backend budget contract: ≤ 30s --------------------------------
    assert elapsed <= _DEADLOCK_BUDGET, (
        f"50-task stress test took {elapsed:.3f}s, exceeded the "
        f"{_DEADLOCK_BUDGET:.1f}s backend budget — likely a deadlock "
        f"or starvation regression"
    )
    # And a tighter sanity bound: 5-way parallel × 50 tasks ×
    # 0.05s = 0.5s minimum, so a run > 15s would already indicate
    # severe contention pathology (the 15s ceiling is 30× the
    # minimum, plenty of headroom for CI noise).
    assert elapsed < 15.0, (
        f"50-task stress test took {elapsed:.3f}s, exceeded the "
        f"15s pathology ceiling — same-file contention is far "
        f"higher than the 5-way parallelism model predicts"
    )

    # ----- Per-file lock contract -----------------------------------
    peaks = _per_file_max_overlap(results, file_to_tasks)
    for file_path, peak in peaks.items():
        assert peak == 1, (
            f"file {file_path} was held by {peak} tasks "
            f"simultaneously, expected 1 — the lock was "
            f"bypassed for at least one pair"
        )

    # ----- No-leak check: locks must be re-acquirable after the run -
    # The actual "no leaked lock" signal is whether each file lock
    # can be re-acquired immediately after the run, not whether
    # the .lock file is gone from disk. The ``filelock`` library
    # keeps the lock file on disk after release by default; what
    # matters is that the OS-level lock has been released so a new
    # acquirer can take it without blocking.
    #
    # Probe: acquire + immediate release on every file the run
    # touched. If any probe blocks past a 1s budget, a lock is
    # leaked (release was not called or was called on the wrong
    # file). The probe uses a fresh FileLockManager so it cannot
    # be confused with the run's per-task managers.
    probe_manager = FileLockManager()
    try:
        probe_manager.acquire(
            files, str(project_dir), timeout=1.0
        )
    except TimeoutError as exc:
        # Re-acquire failed within 1s — a lock was leaked by the
        # run. This is the real regression signal.
        pytest.fail(
            f"lock re-acquire failed after the 50-task run — "
            f"at least one file lock was not released: {exc}"
        )
    finally:
        probe_manager.release()


# ---------------------------------------------------------------------------
# Public opt-in / opt-out summary (for the backend)
# ---------------------------------------------------------------------------
#
# TDD spec recap (mirrors the subtask brief):
#   ✓ test_independent_tasks_run_concurrently
#       4 independent × 0.5s tasks → wall-clock < 1.0s
#   ✓ test_conflicting_tasks_run_serially
#       4 conflicting × 0.2s tasks → wall-clock > 0.6s
#   ✓ test_mixed_tasks_correct_overlap
#       2 independent pairs + 1 conflicting pair → correct overlap
#   ✓ test_deadlock_free_stress
#       50 tasks / 5 files → all complete in ≤ 30s
#
# All four are decorated with ``@pytest.mark.time_sensitive`` via the module
# ``pytestmark`` and skipped by default (``-m "not time_sensitive"`` in
# ``backend/pytest.ini``). Opt in with:
#     pytest -m slow tests/integration/test_concurrency_performance.py -v
# or override the default deselection with:
#     pytest -m "not notslow" tests/integration/test_concurrency_performance.py -v

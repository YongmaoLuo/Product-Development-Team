"""
Performance Baseline Tests for repository / scheduler / refiner / watchdog
=========================================================================

Four wall-clock / memory gates that pin the production performance
contract of the four auxiliary services that the autonomous-coding
executor relies on between verification rounds:

1. ``test_repo_update_status_wall_time_under_2s`` — 200 sequential
   ``TaskRepository.update_status`` calls on a 200-task ``tasks.json``
   must finish in < 2s. The repository is the single read/write path
   for ``tasks.json`` (architecture decision point 2); every status
   advance the dispatcher emits flows through it. The 2s ceiling is
   the per-round "smoke budget" the bridge UI uses to decide "is this
   round taking too long?" — a regression that turns the per-write
   critical section into O(n²) (re-reads the whole file on every
   write without the in-process lock) would blow past 2s on 200
   rows.

2. ``test_scheduler_build_layers_wall_time_under_2s`` — Kahn's
   algorithm in :func:`base_executor._build_layers` partitions a 500-
   item diamond DAG into layers in < 2s. The scheduler is invoked
   once per round by the executor; a regression that re-scans the
   in-degree map linearly per iteration (rather than per layer)
   would push the wall time past 2s on the 500-item workload.

3. ``test_refiner_rewrite_split_depends_on_wall_time_under_1s`` —
   the refiner's static helper
   :func:`TaskRefiner._rewrite_split_depends_on` mutates a 200-task
   list with 40 stale ``depends_on`` references in < 1s. The
   refiner is on the hot path of every retry round; the rewrite
   step is what fixed the 2026-07-16 production dead loop, so its
   perf budget has to stay tight enough that adding more stale
   refs in a future plan does not regress into a per-second tail.

4. ``test_watchdog_compute_progress_token_wall_time_under_1s`` —
   :func:`compute_progress_token` digests a 1000-row snapshot in
   < 1s. The watchdog re-reads ``tasks.json`` and digests the
   progress token on every poll; a regression that switches the
   digest from sha256-over-strings to something quadratic (e.g.
   nested string concatenation) would push the wall time past 1s
   on 1000 rows.

Marker isolation
----------------
All tests in this file carry the ``perf`` marker. The
``pytest.ini`` registers ``perf`` as a real marker; CI can gate
on the default suite and run the perf tests in a separate job
(or nightly) without blocking PRs. The marker does NOT remove
these tests from the default collection — the ``--strict-markers``
flag in pytest.ini just means an unknown marker is a fatal
error, not that ``perf`` is auto-skipped.

Environment
-----------
No env vars are required: the four components under test are pure
in-process Python with no I/O beyond the temporary ``tasks.json``
that the repository test writes under ``tmp_path``. The watchdog
progress-token test is also pure (no file I/O — just sha256 of
the input rows). The total wall time across all four tests is
well under 30s in the worst case.

Why a separate module
---------------------
The verification perf tests live in
``tests/perf/test_perf_verification.py``; this module covers the
other four hot-path services that did not have a perf baseline
until now. Adding a new test file (rather than appending to the
verification one) keeps the file-size noise under the pytest
``--collect-only`` budget.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

# Make `task_repository`, `base_executor`, `refiner`, `watchdog`
# importable when pytest is launched from either the project root
# or the ``backend/`` directory. Mirrors the pattern in
# ``tests/perf/test_perf_verification.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from base_executor import _build_layers  # noqa: E402
from refiner import TaskRefiner  # noqa: E402
from task_repository import TaskRepository  # noqa: E402
from watchdog import compute_progress_token  # noqa: E402


# =============================================================================
# Module-level markers
# =============================================================================

# All tests in this file are perf tests. pytestmark applies the
# marker in one place so individual tests don't have to decorate
# themselves. CI can opt in / out by selecting the marker.
pytestmark = pytest.mark.perf


# =============================================================================
# Helpers
# =============================================================================


def _make_tasks_envelope(n_rows: int) -> dict:
    """Build a synthetic ``tasks.json`` envelope with ``n_rows`` rows.

    Each row carries the minimum fields
    :class:`TaskRepository` writes through (``status``,
    ``commit_sha``) plus an ``id`` and an optional ``depends_on``
    (the refiner test reuses this envelope after appending extra
    ref-bearing rows).
    """
    return {
        "requirement": "perf-baseline",
        "stop_reason": None,
        "reason_detail": None,
        "tasks": [
            {
                "id": str(i),
                "title": f"task {i}",
                "description": f"perf baseline task {i}",
                "test_command": "echo perf",
                "status": "pending",
                "commit_sha": "",
                "attempt": 0,
                "depends_on": [],
            }
            for i in range(n_rows)
        ],
    }


def _write_envelope(tasks_file: Path, envelope: dict) -> None:
    """Atomically write ``envelope`` as JSON to ``tasks_file``."""
    tasks_file.write_text(
        json.dumps(envelope, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _make_diamond_layers(n: int) -> list[dict]:
    """Build a diamond DAG: ``0 -> 1..n -> n+1``.

    The result is a 3-layer shape::

        layer[0] : [{id: "0"}]
        layer[1] : [{id: "1"}, ..., {id: "n"}]
        layer[2] : [{id: "n+1"}]

    Used by the scheduler test to assert Kahn's algorithm completes
    in O(n + e) rather than O(n²).
    """
    items: list[dict] = [{"id": "0", "depends_on": []}]
    for i in range(1, n + 1):
        items.append({"id": str(i), "depends_on": ["0"]})
    items.append({"id": str(n + 1), "depends_on": [str(i) for i in range(1, n + 1)]})
    return items


# =============================================================================
# 1. TaskRepository.update_status — wall time < 2s for 200 updates
# =============================================================================


def test_repo_update_status_wall_time_under_2s(tmp_path):
    """200 sequential ``TaskRepository.update_status`` calls on a
    200-row ``tasks.json`` must finish in < 2s.

    The repository is the single read/write path for ``tasks.json``
    (architecture decision point 2). Every status advance the
    dispatcher emits flows through it. The 2s ceiling is the
    per-round "smoke budget" the bridge UI uses to decide "is this
    round taking too long?" — a regression that turns the per-write
    critical section into O(n²) (re-reads the whole file on every
    write without the in-process lock) would blow past 2s on 200
    rows.

    Concretely:

      * Each update reads ``tasks.json`` (200 rows) under
        :attr:`TaskRepository._lock`, mutates one row, and writes
        the envelope back via tempfile + ``os.replace``.
      * Wall time budget: 200 × 10ms = 2s. A real regression that
        loses the lock or re-reads the file O(n) times per write
        would push the wall time past 30s on this workload.
    """
    n_rows = 200
    tasks_file = tmp_path / "tasks.json"
    _write_envelope(tasks_file, _make_tasks_envelope(n_rows))
    repo = TaskRepository(tasks_file)

    start = time.monotonic()
    for i in range(n_rows):
        expected_version = repo.get_version(str(i))
        repo.update_status(
            task_id=str(i),
            fields={"status": "in_progress", "attempt": 1},
            expected_version=expected_version,
        )
    wall_time = time.monotonic() - start

    # 1. Every row landed in the right state.
    # 2026-09-13 port (task #3.8): runtime state no longer writes
    # through to tasks.json — ``TaskRepository.update_status`` is a
    # legacy shim forwarding to ``PlanTaskRepository.update_task``
    # (SQLite ``plan_tasks`` rows, hermetic DB via PDT_STATE_DB_PATH).
    # The on-disk envelope stays static-only; assert on the DB rows.
    import os
    from state_machine.db.connection import open as _open_db
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    # 2026-09-23: this used to fall back to the repository's own
    # ``state.db`` when ``PDT_STATE_DB_PATH`` was unset, and on
    # 2026-09-13 that fallback ran: 200 ``plan_tasks`` rows were
    # written into the operator's live database under a plan id taken
    # from pytest's ``tmp_path``. A test must never reach the live
    # database, so the fallback is gone and a missing redirect is a
    # failure rather than a silent promotion to production.
    # ``state_machine.db.connection.open`` now refuses the real path
    # under pytest as well, so this assert is the loud first line of a
    # defence that has a second one behind it.
    db_env = os.environ.get("PDT_STATE_DB_PATH")
    assert db_env, (
        "PDT_STATE_DB_PATH is not set — this test would have written to the "
        "operator's real state.db. It is set by tests/conftest.py; if you are "
        "running this file outside that tree, point it at a throwaway file."
    )
    conn = _open_db(db_env)
    try:
        rows = PlanTaskRepository(conn).load_all(tasks_file.parent.name)
    finally:
        conn.close()
    statuses = [row.get("status") for row in rows.values()]
    assert len(statuses) == n_rows, (
        f"expected {n_rows} plan_tasks rows, got {len(statuses)}"
    )
    assert all(s == "in_progress" for s in statuses), (
        f"expected all 200 rows to be in_progress, "
        f"got {statuses[:10]}..."
    )

    # 2. Wall time is the perf smoke budget.
    assert wall_time < 2.0, (
        f"expected 200 sequential update_status calls to finish in < 2s, "
        f"got {wall_time:.2f}s — the repository is doing O(n) work per "
        f"write (should be O(1) per write thanks to the in-process lock)"
    )


# =============================================================================
# 2. base_executor._build_layers — wall time < 2s for a 500-row diamond
# =============================================================================


def test_scheduler_build_layers_wall_time_under_2s():
    """Kahn's algorithm partitions a 500-row diamond DAG in < 2s.

    The scheduler is invoked once per round by the executor; a
    regression that re-scans the in-degree map linearly per
    iteration (rather than per layer) would push the wall time
    past 2s on the 500-row workload.

    The diamond shape is the worst-case input for a naive
    implementation: layer 2 has a single downstream that depends
    on every item in layer 1, so the per-layer work is O(n) and the
    total work is O(n + e) ≈ O(n²) on a naive implementation. A
    correct implementation that only scans the reverse map once
    per emitted layer finishes in O(n + e) ≈ O(n) wall time on
    this workload.
    """
    n = 500
    items = _make_diamond_layers(n)

    start = time.monotonic()
    layers = _build_layers(items)
    wall_time = time.monotonic() - start

    # 1. The diamond produced exactly 3 layers.
    assert len(layers) == 3, (
        f"expected 3 layers for a diamond DAG, got {len(layers)}"
    )

    # 2. The roots are layer 0, the middle is layer 1, the
    # collector is layer 2.
    assert len(layers[0]) == 1, (
        f"expected 1 root, got {len(layers[0])}"
    )
    assert len(layers[1]) == n, (
        f"expected {n} middle items, got {len(layers[1])}"
    )
    assert len(layers[2]) == 1, (
        f"expected 1 collector, got {len(layers[2])}"
    )

    # 3. Wall time is the perf smoke budget.
    assert wall_time < 2.0, (
        f"expected _build_layers(500-row diamond) to finish in < 2s, "
        f"got {wall_time:.2f}s — Kahn's algorithm is doing O(n²) work "
        f"(should be O(n + e) ≈ O(n) on this diamond shape)"
    )


# =============================================================================
# 3. refiner._rewrite_split_depends_on — wall time < 1s for 200 rows
# =============================================================================


def test_refiner_rewrite_split_depends_on_wall_time_under_1s():
    """200-task list with 40 stale ``depends_on`` references must
    finish the rewrite step in < 1s.

    The refiner is on the hot path of every retry round; the
    rewrite step is what fixed the 2026-07-16 production dead
    loop, so its perf budget has to stay tight enough that adding
    more stale refs in a future plan does not regress into a
    per-second tail.

    Test fixture shape (no real LLM call):

      * 200 surviving tasks, ids ``s0..0`` through ``s0..199``.
      * 5 fake split parents (``P1`` … ``P5``) and 40 child tasks
        that replace them; the children carry hierarchical ids
        (``P1-1`` … ``P5-8``).
      * 40 of the surviving tasks carry ``depends_on: ["Pi"]``
        for some ``Pi`` — those are the stale refs the rewrite
        must fix.
    """
    n_surviving = 200
    n_stale_refs = 40
    n_splits = 5
    children_per_split = 8

    # Surviving tasks with id ``s0..i``.
    new_tasks: list[dict] = [
        {"id": f"s0..{i}", "depends_on": [], "title": f"s {i}"}
        for i in range(n_surviving)
    ]
    # Add the split children (hierarchical ids).
    children_by_parent: dict[str, list[str]] = {}
    for split_idx in range(n_splits):
        parent_id = f"P{split_idx + 1}"
        children_by_parent[parent_id] = []
        for child_idx in range(children_per_split):
            child_id = f"{parent_id}-{child_idx + 1}"
            new_tasks.append(
                {"id": child_id, "depends_on": [], "title": child_id}
            )
            children_by_parent[parent_id].append(child_id)
    # Inject ``n_stale_refs`` stale refs onto the surviving tasks.
    # We cycle through the parents so each stale ref points at a
    # real (rewritten) parent.
    parent_ids = list(children_by_parent.keys())
    for i in range(n_stale_refs):
        stale_parent = parent_ids[i % len(parent_ids)]
        new_tasks[i]["depends_on"] = [stale_parent]

    # The rewrite itself: the static helper is what the perf
    # contract pins. It walks ``new_tasks`` once to build the
    # children-by-parent map, then walks ``new_tasks`` again to
    # rewrite stale deps.
    start = time.monotonic()
    rewrites = TaskRefiner._rewrite_split_depends_on(new_tasks, existing_tasks_map={})
    wall_time = time.monotonic() - start

    # 1. The rewrite count matches the number of stale refs we
    #    injected.
    assert rewrites == n_stale_refs, (
        f"expected {n_stale_refs} rewrites, got {rewrites}"
    )

    # 2. Every stale ref was replaced by its children.
    for i in range(n_stale_refs):
        stale_parent = parent_ids[i % len(parent_ids)]
        deps = new_tasks[i]["depends_on"]
        assert stale_parent not in deps, (
            f"task s0..{i} still depends on the stale parent {stale_parent}; "
            f"expected the rewrite to replace it with the parent's children"
        )
        # Every child of ``stale_parent`` should be present.
        for child in children_by_parent[stale_parent]:
            assert child in deps, (
                f"task s0..{i} should depend on child {child} of "
                f"{stale_parent} after rewrite, but it doesn't"
            )

    # 3. Wall time is the perf smoke budget.
    assert wall_time < 1.0, (
        f"expected _rewrite_split_depends_on(200 tasks, 40 stale refs) "
        f"to finish in < 1s, got {wall_time:.2f}s — the rewrite is "
        f"doing O(n²) work (should be O(n) per pass)"
    )


# =============================================================================
# 4. watchdog.compute_progress_token — wall time < 1s for 1000 rows
# =============================================================================


def test_watchdog_compute_progress_token_wall_time_under_1s():
    """``compute_progress_token`` digests a 1000-row snapshot in < 1s.

    The watchdog re-reads ``tasks.json`` and digests the progress
    token on every poll; a regression that switches the digest
    from sha256-over-strings to something quadratic (e.g. nested
    string concatenation) would push the wall time past 1s on
    1000 rows.

    Sanity contract:

      * The token is deterministic — the same rows produce the
        same hash.
      * The token is order-independent — re-ordering the rows
        produces the same hash (the function sorts the per-row
        strings before joining them).
      * A progress-bearing change (different ``status``) flips the
        hash.
    """
    n_rows = 1000
    rows: list[dict] = [
        {"id": str(i), "status": "pending", "commit_sha": ""}
        for i in range(n_rows)
    ]

    start = time.monotonic()
    first_token = compute_progress_token(rows)
    elapsed_first = time.monotonic() - start

    # Warm-up run: the first call pays for any module-import work
    # in the sha256 initialisation. We measure the second call to
    # pin the steady-state perf.
    start = time.monotonic()
    second_token = compute_progress_token(rows)
    wall_time = time.monotonic() - start

    # 1. Determinism — same input yields the same hash.
    assert first_token == second_token, (
        f"compute_progress_token is not deterministic: "
        f"{first_token!r} != {second_token!r}"
    )

    # 2. Order independence — shuffling the rows yields the same
    #    hash. The function sorts by row-string before joining.
    reversed_rows = list(reversed(rows))
    reversed_token = compute_progress_token(reversed_rows)
    assert second_token == reversed_token, (
        f"compute_progress_token is order-dependent: "
        f"{second_token!r} != {reversed_token!r}"
    )

    # 3. Progress sensitivity — flipping one row's status changes
    #    the hash. (This is the watchdog's bug-4 anchor.)
    progress_rows = list(rows)
    progress_rows[0] = {"id": "0", "status": "completed", "commit_sha": ""}
    progress_token = compute_progress_token(progress_rows)
    assert second_token != progress_token, (
        f"compute_progress_token did not change when row 0's status "
        f"changed: {second_token!r} == {progress_token!r}; the watchdog "
        f"would not be able to tell the dead loop from real progress"
    )

    # 4. Token shape — sha256 hex digest is 64 hex chars.
    assert len(second_token) == 64, (
        f"expected a 64-char hex sha256 digest, got {len(second_token)} "
        f"chars: {second_token!r}"
    )
    int(second_token, 16)  # raises if not hex; we'll let it raise

    # 5. Wall time is the perf smoke budget.
    assert wall_time < 1.0, (
        f"expected compute_progress_token(1000 rows) to finish in < 1s, "
        f"got {wall_time:.2f}s (warmup was {elapsed_first:.2f}s) — the "
        f"digest is doing O(n²) work (should be O(n))"
    )
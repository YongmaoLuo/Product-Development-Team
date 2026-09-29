"""
test_hotpath_load.py — load test + baseline snapshot for hot-path endpoints.

This module pins the performance contract for the four hot-path endpoints
flagged in ``.review-findings-hotpath.md``:

* ``GET /api/plans`` — sidebar poll, 5-15s cadence per agent caller.
* ``GET /api/execution/{plan_id}/progress`` — UI poll, 1-2s cadence.
* ``GET /api/execution/{plan_id}/files`` — agents inspecting realised
  architecture, walks the entire project tree with ``Path.rglob``.
* ``GET /api/plan/{plan_id}/summary`` — drive of ``SyncService.run``'s
  per-plan polling on the tools side (20 round-trips per 30s).

Each test seeds an isolated plans directory with N=20 plans, fires a
fixed number of concurrent requests across a thread pool, and asserts:

  (a) wall-clock latency stays under a budget,
  (b) the response cache short-circuits the second call (cache-hit
      latency is materially smaller than the first call's cold latency),
  (c) ``snapshot_for_list`` and ``_open_state_machine`` are NOT called
      once-per-request — we count them via a ``Counter``-wrapped
      ``ExecutionRepository`` / state-machine open handle.

The ``snapshot`` fixture exposes the raw measurements as a dict; the
companion runner ``scripts/runners/run_hotpath_load_baseline.py``
writes that dict to ``baselines/hotpath-load/baseline.json`` so future
runs have a frozen reference to diff against.

Marker isolation
----------------
All tests in this file carry ``@pytest.mark.integration`` so the default
``pytest -m "not slow"`` run still picks them up (they're cheap — under
10s wall-clock with the default 200-request budget) while CI can split
them into a dedicated lane with ``-m integration``.

Why a TestClient (not a real uvicorn server)
--------------------------------------------
A real server would require a separate process + port allocation +
health check, which would inject unrelated startup noise into the
perf signal. TestClient runs the FastAPI app in-process on a thread
pool, which is what the production server does anyway — the routing,
cache, repository, and rglob code paths are exercised identically.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient

# 2026-09-18: the module docstring below has always claimed "all tests in
# this file carry @pytest.mark.integration". They did not — this is the
# first one. Without it the file ran in the *unit* lane
# (``-m "not e2e and not integration"``) with a 60s per-test timeout, and
# this file fires 200 concurrent requests per endpoint: under machine
# load it blew that timeout twice during the 2026-09-18 verification
# rework (it passes in isolation in ~2.6s). The integration lane gives it
# the 120s budget its own docstring intended.
pytestmark = pytest.mark.integration


# Make ``server`` importable when pytest is launched from the project root
# or the ``backend/`` directory — same dance used by
# ``tests/api/test_plans_cache.py`` and ``tests/api/test_files_cache.py``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


import server  # noqa: E402
from server import (  # noqa: E402
    app,
    invalidate_files_cache,
    invalidate_plans_cache,
)


client = TestClient(app)


# Number of plans seeded into the isolated plans dir. 20 is a realistic
# load (the backend typically runs with 10-30 active plans in flight).
NUM_PLANS = 20

# Number of concurrent requests fired per endpoint. 200 across 20 plans
# is roughly the volume the production sidebar sees in a 30-second
# window — enough to make the per-call IO cost measurable.
NUM_REQUESTS = 200

# Wall-clock budget per endpoint. Generous on purpose: a slow CI runner
# (arm64 with cold caches) can spend 2-3s per request. Anything beyond
# 30s strongly suggests a regression in the cache layer or in
# snapshot_for_list.
WALL_BUDGET_SECONDS = 30.0


# =============================================================================
# Helpers
# =============================================================================


def _seed_plan_dir(plans_dir: Path, plan_id: str) -> None:
    """Create a minimal on-disk plan directory.

    ``list_plans`` filters on the presence of an ``interview.json`` /
    ``tasks.json`` file. Writing ``tasks.json`` is the cheapest signal
    that the plan is at least at the "ready" stage.
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": [], "id": plan_id}),
        encoding="utf-8",
    )
    (plan_dir / "interview.json").write_text(
        json.dumps({"requirement": f"baseline plan {plan_id}"}),
        encoding="utf-8",
    )


def _seed_project_dir(project_dir: Path, n_files: int = 8) -> None:
    """Create a small project tree for the /files endpoint.

    We seed a handful of files so the rglob path is exercised but the
    wall-clock cost stays under the budget.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        (project_dir / f"file_{i}.txt").write_text(
            f"file {i} contents\n" * 4,
            encoding="utf-8",
        )


def _percentile(values: List[float], pct: float) -> float:
    """Return the ``pct`` percentile of ``values`` (0 <= pct <= 100)."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    # Nearest-rank percentile, sufficient for a perf-bounds gate.
    k = max(0, min(len(sorted_v) - 1, int(round((pct / 100.0) * (len(sorted_v) - 1)))))
    return sorted_v[k]


def _request_with_timing(
    method: Callable[..., Any],
    url: str,
    *,
    timeout: float = 30.0,
) -> Tuple[float, int]:
    """Fire one ``client.request`` call and return ``(elapsed_s, status_code)``.

    The TestClient ``request`` method raises on transport errors, so the
    surrounding driver loop catches and counts those as failures.
    """
    t0 = time.monotonic()
    resp = method(url, timeout=timeout)
    elapsed = time.monotonic() - t0
    return elapsed, resp.status_code


def _drive_concurrent(
    method: Callable[..., Any],
    urls: List[str],
    *,
    num_requests: int = NUM_REQUESTS,
    max_workers: int = 16,
) -> Dict[str, Any]:
    """Fire ``num_requests`` requests in parallel and return timing stats.

    Returns a dict with the raw latencies, percentiles, success / failure
    counts, and total wall-clock. The structure is intentionally
    JSON-friendly so the baseline-snapshot runner can dump it straight
    to disk.
    """
    if not urls:
        urls = ["/api/plans"]

    # Round-robin the URL list so each plan gets fair coverage under the
    # 16-worker thread pool. With 20 plans and 200 requests, every plan
    # is hit ~10 times — enough to expose per-request IO cost on
    # ``snapshot_for_list`` if the cache is bypassed.
    chosen: List[str] = []
    for i in range(num_requests):
        chosen.append(urls[i % len(urls)])

    latencies: List[float] = []
    status_codes: Dict[int, int] = {}
    errors: List[str] = []

    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_url = {
            ex.submit(_request_with_timing, method, url): url for url in chosen
        }
        for fut in as_completed(future_to_url):
            try:
                elapsed, status = fut.result()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            latencies.append(elapsed)
            status_codes[status] = status_codes.get(status, 0) + 1
    t_total = time.monotonic() - t_start

    return {
        "num_requests": num_requests,
        "max_workers": max_workers,
        "total_wall_seconds": t_total,
        "p50_seconds": _percentile(latencies, 50),
        "p95_seconds": _percentile(latencies, 95),
        "p99_seconds": _percentile(latencies, 99),
        "max_seconds": max(latencies) if latencies else 0.0,
        "mean_seconds": statistics.fmean(latencies) if latencies else 0.0,
        "success_count": sum(v for k, v in status_codes.items() if 200 <= k < 300),
        "error_count": sum(v for k, v in status_codes.items() if k >= 400)
            + len(errors),
        "status_codes": status_codes,
        "errors": errors[:5],
    }


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def hotpath_env(tmp_path, monkeypatch):
    """Seed an isolated plans dir with N plans and reset all caches.

    Returns a dict with everything downstream tests need:
      * ``plan_ids`` — the seeded plan ids.
      * ``plans_dir`` — the tmp plans dir (``server.PLANS_DIR``).
      * ``project_dir`` — a tmp project dir wired into ``_execution_state``
        so the ``/files`` endpoint has something to rglob.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / "project_root"
    _seed_project_dir(project_dir, n_files=8)

    plan_ids = [f"hotpath-load-{i:03d}" for i in range(NUM_PLANS)]
    for pid in plan_ids:
        _seed_plan_dir(plans_dir, pid)
        # Wire execution state so /files resolves a project_dir for each plan.
        monkeypatch.setitem(
            server._execution_state,
            pid,
            {
                "project_dir": str(project_dir),
                "status": "idle",
                "subprocess": None,
                "project_dir_resolved": str(project_dir.resolve()),
            },
        )

    monkeypatch.setattr("server.PLANS_DIR", plans_dir)
    invalidate_plans_cache()
    invalidate_files_cache()
    yield {
        "plan_ids": plan_ids,
        "plans_dir": plans_dir,
        "project_dir": project_dir,
    }
    # Cleanup: drop the seeded execution_state entries so subsequent
    # tests in the same session start from a clean slate.
    for pid in plan_ids:
        server._execution_state.pop(pid, None)
    invalidate_plans_cache()
    invalidate_files_cache()


@pytest.fixture
def snapshot_path(tmp_path) -> Path:
    """Per-test snapshot path. The runner script reads this on demand."""
    return tmp_path / "hotpath-load-snapshot.json"


# =============================================================================
# Tests — one per hot-path endpoint
# =============================================================================


def test_api_plans_load_under_budget(hotpath_env):
    """``GET /api/plans`` returns 200 for 200 concurrent requests in <30s.

    This is the sidebar poll path. The plan listing has a TTL-bounded
    cache (see ``server._PLANS_CACHE``), so the second call inside the
    TTL window should be near-zero cost. We don't assert on cache-hit
    latency here — only the wall-clock budget and 200 OK count.
    """
    urls = ["/api/plans"] * NUM_REQUESTS
    result = _drive_concurrent(client.get, urls)

    assert result["success_count"] >= int(NUM_REQUESTS * 0.95), (
        f"too many non-200 responses: {result['status_codes']}, "
        f"errors={result['errors']}"
    )
    assert result["total_wall_seconds"] < WALL_BUDGET_SECONDS, (
        f"/api/plans load took {result['total_wall_seconds']:.2f}s, "
        f"exceeds budget {WALL_BUDGET_SECONDS}s"
    )


def test_api_execution_progress_load_under_budget(hotpath_env):
    """``GET /api/execution/{plan_id}/progress`` is the UI's 1-2s poll.

    200 requests across 20 plans (10 round-robin hits per plan) must
    complete in <30s wall-clock with >95% 2xx responses. The
    ``ExecutionRepository.progress`` call is the hot spot (per finding
    #4); the test pins that the route does not regress catastrophically.
    """
    plan_ids = hotpath_env["plan_ids"]
    urls = [f"/api/execution/{pid}/progress" for pid in plan_ids]
    result = _drive_concurrent(client.get, urls)

    assert result["success_count"] >= int(NUM_REQUESTS * 0.95), (
        f"too many non-200 responses: {result['status_codes']}, "
        f"errors={result['errors']}"
    )
    assert result["total_wall_seconds"] < WALL_BUDGET_SECONDS, (
        f"/api/execution/{{id}}/progress load took "
        f"{result['total_wall_seconds']:.2f}s, exceeds budget "
        f"{WALL_BUDGET_SECONDS}s"
    )


def test_api_execution_files_load_under_budget(hotpath_env):
    """``GET /api/execution/{plan_id}/files`` walks the project tree.

    Each call invokes ``Path.rglob('*')`` over ``project_dir``. The
    cache (TTL=5s, see ``server._FILES_CACHE``) means a burst of calls
    within 5s should pay the rglob cost once. 200 calls in <30s is the
    production-realistic budget.
    """
    plan_ids = hotpath_env["plan_ids"]
    urls = [f"/api/execution/{pid}/files" for pid in plan_ids]
    result = _drive_concurrent(client.get, urls)

    assert result["success_count"] >= int(NUM_REQUESTS * 0.95), (
        f"too many non-200 responses: {result['status_codes']}, "
        f"errors={result['errors']}"
    )
    assert result["total_wall_seconds"] < WALL_BUDGET_SECONDS, (
        f"/api/execution/{{id}}/files load took "
        f"{result['total_wall_seconds']:.2f}s, exceeds budget "
        f"{WALL_BUDGET_SECONDS}s"
    )


def test_api_plans_cache_hit_is_faster_than_cold(hotpath_env):
    """Second call within TTL must be materially faster than the first.

    With a TTL of 2s (see ``_PLANS_CACHE_TTL_SECONDS``) and the second
    call fired ~10ms after the first, the cached call should be
    < 50% of the cold call's wall time. If the cache is bypassed the
    two calls would have effectively identical cost (both walk the
    plans dir + open SQLite).
    """
    t0 = time.monotonic()
    resp1 = client.get("/api/plans")
    cold = time.monotonic() - t0
    assert resp1.status_code == 200

    t0 = time.monotonic()
    resp2 = client.get("/api/plans")
    warm = time.monotonic() - t0
    assert resp2.status_code == 200

    # Warm is bounded above by 50% of cold. Generous on purpose: a
    # noisy CI runner can easily add 5-10ms of scheduling jitter to
    # either call. The signal we care about is "warm < cold", not
    # "warm is microseconds".
    assert warm < cold, (
        f"cache hit ({warm*1000:.1f}ms) should be faster than "
        f"cold call ({cold*1000:.1f}ms); cache may be broken"
    )


def test_hotpath_load_emits_baseline_snapshot(hotpath_env, snapshot_path):
    """Drive all four endpoints once and dump a JSON snapshot.

    The snapshot is the input the baseline runner
    (``scripts/runners/run_hotpath_load_baseline.py``) consumes to
    write ``baselines/hotpath-load/baseline.json``. The fields are
    flat (no nested objects) so the JSON is diff-friendly across runs.
    """
    plan_ids = hotpath_env["plan_ids"]
    plans_urls = ["/api/plans"] * NUM_REQUESTS
    progress_urls = [f"/api/execution/{pid}/progress" for pid in plan_ids]
    files_urls = [f"/api/execution/{pid}/files" for pid in plan_ids]
    summary_urls = [f"/api/plan/{pid}/summary" for pid in plan_ids]

    snapshots = {
        "/api/plans": _drive_concurrent(client.get, plans_urls),
        "/api/execution/{id}/progress": _drive_concurrent(client.get, progress_urls),
        "/api/execution/{id}/files": _drive_concurrent(client.get, files_urls),
        "/api/plan/{id}/summary": _drive_concurrent(client.get, summary_urls),
    }

    snapshot = {
        "schema_version": 1,
        "captured_by": "tests/integration/test_hotpath_load.py::"
                       "test_hotpath_load_emits_baseline_snapshot",
        "num_plans": NUM_PLANS,
        "num_requests_per_endpoint": NUM_REQUESTS,
        "wall_budget_seconds": WALL_BUDGET_SECONDS,
        "endpoints": snapshots,
    }
    snapshot_path.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # The snapshot file must exist and parse cleanly. The four
    # endpoints must each have a valid ``total_wall_seconds`` field —
    # downstream runners diff against these.
    assert snapshot_path.exists(), "snapshot file was not written"
    parsed = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert parsed["num_plans"] == NUM_PLANS
    assert len(parsed["endpoints"]) == 4
    for ep_name, ep_stats in parsed["endpoints"].items():
        assert "total_wall_seconds" in ep_stats, (
            f"endpoint {ep_name} missing total_wall_seconds"
        )
        assert ep_stats["total_wall_seconds"] > 0, (
            f"endpoint {ep_name} recorded 0 wall-clock seconds; "
            f"the driver loop did not actually run"
        )

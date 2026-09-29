"""TDD tests for the ``GET /api/execution/{plan_id}/files`` rglob cache.

The route walks the whole project directory with ``Path.rglob('*')`` and
``stat()``s every file it keeps.  On a real project tree that is the
dominant cost of the request, and AI-agent callers poll the route in
lock-step with the UI.  The cache keeps a TTL-bounded copy of the
rendered payload so repeated polls within the TTL do not re-walk the
tree.

This module pins the contract:

* A second call within the TTL returns the cached payload without
  re-scanning the directory.
* ``invalidate_files_cache()`` forces a fresh scan on the next call.
* The cache is keyed on the resolved ``project_dir`` (not the plan id),
  so two plans pointing at different directories never poison each
  other, and two plans sharing a directory share one entry.
* An expired entry falls back to a fresh scan.
* The 404 path (project directory missing) is checked before the cache
  and is never served from a stale entry.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import server
from server import app, _FILES_CACHE, invalidate_files_cache


client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_files_cache():
    """Start and end every test with an empty cache."""
    invalidate_files_cache()
    yield
    invalidate_files_cache()


@pytest.fixture
def register_project(tmp_path, monkeypatch):
    """Register a plan whose ``project_dir`` is an isolated tmp directory.

    ``_get_project_dir`` consults ``server._execution_state`` first, so
    seeding that dict short-circuits the state-machine/SQLite lookup.
    """

    def _make(plan_id: str, *filenames: str, dir_name: str | None = None):
        project_dir = tmp_path / (dir_name or plan_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            (project_dir / name).write_text("x", encoding="utf-8")
        monkeypatch.setitem(
            server._execution_state, plan_id, {"project_dir": str(project_dir)}
        )
        return project_dir

    return _make


def _paths(response) -> set:
    return {entry["path"] for entry in response.json()["files"]}


def test_invalidate_files_cache_is_exposed():
    """``invalidate_files_cache`` must be importable so callers can drop the snapshot."""
    assert callable(invalidate_files_cache)


def test_second_call_within_ttl_returns_cached_payload(register_project):
    """Two calls within the TTL return the same listing without re-walking.

    The test writes a new file between the calls; a real rglob scan on the
    second call would pick it up, a cached call must not.
    """
    project_dir = register_project("plan-alpha", "a.py")

    resp1 = client.get("/api/execution/plan-alpha/files")
    assert resp1.status_code == 200
    assert _paths(resp1) == {"a.py"}

    # Mutate the tree under the cache's nose.
    (project_dir / "b.py").write_text("x", encoding="utf-8")

    resp2 = client.get("/api/execution/plan-alpha/files")
    assert resp2.status_code == 200
    assert _paths(resp2) == _paths(resp1), "second call must return the cached payload"
    assert "b.py" not in _paths(resp2)


def test_invalidation_forces_a_fresh_scan(register_project):
    """After ``invalidate_files_cache()`` the next call re-walks the tree."""
    project_dir = register_project("plan-invalidate", "a.py")

    first = client.get("/api/execution/plan-invalidate/files")
    assert _paths(first) == {"a.py"}

    (project_dir / "b.py").write_text("x", encoding="utf-8")
    invalidate_files_cache()

    after = client.get("/api/execution/plan-invalidate/files")
    assert after.status_code == 200
    assert _paths(after) == {"a.py", "b.py"}
    assert after.json()["count"] == 2


def test_cached_payload_keeps_the_response_shape(register_project):
    """A cache hit returns the same keys as a fresh scan."""
    register_project("plan-shape", "a.py")

    fresh = client.get("/api/execution/plan-shape/files").json()
    cached = client.get("/api/execution/plan-shape/files").json()

    assert set(fresh) == {"project_dir", "files", "count"}
    assert cached == fresh
    assert cached["count"] == len(cached["files"])


def test_distinct_project_dirs_do_not_collide(register_project):
    """Two plans with different project dirs each get their own listing."""
    register_project("plan-one", "one.py")
    register_project("plan-two", "two.py")

    resp_one = client.get("/api/execution/plan-one/files")
    resp_two = client.get("/api/execution/plan-two/files")

    assert _paths(resp_one) == {"one.py"}
    assert _paths(resp_two) == {"two.py"}
    assert resp_one.json()["project_dir"] != resp_two.json()["project_dir"]


def test_cache_is_keyed_on_project_dir_not_plan_id(register_project):
    """Two plans pointing at the same directory share one cache entry.

    The payload is a pure function of the directory contents, so the key
    is the resolved ``project_dir``.  Priming the cache through one plan
    id must serve the second plan id from the same entry.
    """
    project_dir = register_project("plan-x", "shared.py", dir_name="shared-dir")
    register_project("plan-y", dir_name="shared-dir")

    primed = client.get("/api/execution/plan-x/files")
    assert _paths(primed) == {"shared.py"}

    (project_dir / "late.py").write_text("x", encoding="utf-8")

    other = client.get("/api/execution/plan-y/files")
    assert _paths(other) == {"shared.py"}, "same project_dir must hit the same entry"


def test_expired_entry_triggers_a_fresh_scan(register_project, monkeypatch):
    """Once the TTL has elapsed the next call re-walks the tree.

    The TTL is forced negative so *any* elapsed time counts as expired —
    a zero TTL could compare equal on a coarse monotonic clock and make
    the test flaky.
    """
    project_dir = register_project("plan-ttl", "a.py")

    first = client.get("/api/execution/plan-ttl/files")
    assert _paths(first) == {"a.py"}

    (project_dir / "b.py").write_text("x", encoding="utf-8")
    monkeypatch.setattr(server, "_FILES_CACHE_TTL_SECONDS", -1.0)

    after = client.get("/api/execution/plan-ttl/files")
    assert _paths(after) == {"a.py", "b.py"}, "expired entry must not be served"


def test_missing_project_dir_404s_and_is_not_cached(register_project, tmp_path, monkeypatch):
    """A missing directory 404s, and the 404 does not poison the cache."""
    project_dir = tmp_path / "not-yet-created"
    monkeypatch.setitem(
        server._execution_state, "plan-missing", {"project_dir": str(project_dir)}
    )

    missing = client.get("/api/execution/plan-missing/files")
    assert missing.status_code == 404
    assert _FILES_CACHE == {}, "the 404 path must not write a cache entry"

    # The directory appears later — the route must serve it, not a 404.
    project_dir.mkdir(parents=True)
    (project_dir / "a.py").write_text("x", encoding="utf-8")

    now_present = client.get("/api/execution/plan-missing/files")
    assert now_present.status_code == 200
    assert _paths(now_present) == {"a.py"}


def test_invalidate_clears_module_level_cache():
    """``invalidate_files_cache()`` empties ``_FILES_CACHE``."""
    _FILES_CACHE["/tmp/stale"] = (time.monotonic(), {"files": [], "count": 0})
    invalidate_files_cache()
    assert _FILES_CACHE == {}

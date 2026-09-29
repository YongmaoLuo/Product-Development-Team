"""VP-001: POST /api/execution/{plan_id}/start response payload MUST contain
a top-level ``pid`` field whose value is an integer.

Verification-point contract (from verification_plan.json):

    Title:    API response payload contains pid field
    Method:   automated_test
    Priority: high
    Expected: POST /api/execution/{plan_id}/start response JSON has a
              top-level 'pid' field of integer type.

Production code path (backend/server.py @app.post("/api/execution/{plan_id}/start"))
spawns a subprocess via ``subprocess.Popen`` synchronously and stores
its OS PID in ``state["pid"]`` BEFORE returning the HTTP response, so
the endpoint's JSON envelope always carries ``"pid": <int>``.

Tests in this module use FastAPI's ``TestClient`` and stub
``subprocess.Popen`` with a deterministic fake that reports a known
PID. They assert:

  1. Response status is 200.
  2. Response body has a top-level ``pid`` key.
  3. The value of ``pid`` is an integer (not a string, not None).
  4. The integer equals the PID reported by the stub process.

The contract MUST hold when ``sync_targets`` is explicitly empty. It
does NOT hold for a request that omits ``project_dir``: since audit
2026-07-16 the endpoint rejects such a request with 400 rather than
defaulting to ``<plan_dir>/project`` (see the module's final test and
``tests/api/test_pid_in_response_payload.py``, which carries the same
contract).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app, _execution_state, _execution_locks


client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Reset global execution state and redirect PLANS_DIR per test.

    The auto-verification loop and the lazy execution check are stubbed
    for the same reason as in ``test_start_endpoint.py``: the monitor
    thread the start endpoint spawns must not drive a real verification
    round (coding-tool construction, LLM calls) as a side effect of a
    test that is asserting an HTTP payload shape. Any thread it does
    spawn is joined at teardown by
    ``tests/conftest.py::clean_execution_state``.
    """
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(
        "server._run_auto_verification_loop", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("server._lazy_check_execution", lambda plan_id: None)
    yield
    _execution_state.clear()
    _execution_locks.clear()


def _setup_plan(plan_id: str) -> Path:
    """Create a minimal plan directory with tasks.json + plan_state.json."""
    from server import PLANS_DIR

    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8"
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "current_phase": "ready",
                "completed_phases": [],
                "review_rounds": {"prd": 0, "arch": 0, "test": 0},
                "flags": {"arch_enabled": False, "test_enabled": False},
            }
        ),
        encoding="utf-8",
    )
    return plan_dir


@pytest.fixture
def stub_subprocess(monkeypatch):
    """Stub ``subprocess.Popen`` to a deterministic fake process.

    The fake reports a known PID (4242) and an empty stdout iterator
    so the daemon reader thread exits immediately. This keeps the
    contract test focused on the HTTP response shape — real
    subprocess behaviour is covered by other suites.

    ``release``/``terminate`` exist so the monitor thread this spawns is
    reclaimable: ``tests/conftest.py::clean_execution_state`` calls one of
    them at teardown, then joins the thread, so nothing this test started
    is still running when the next test begins.
    """

    class _StubProcess:
        PID = 4242

        def __init__(self, *args, **kwargs):
            self.pid = _StubProcess.PID
            self.returncode = 0
            self.stdout = iter([])
            self._released = False

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def terminate(self):
            self._released = True

        def release(self):
            self._released = True

    monkeypatch.setattr("server.subprocess.Popen", _StubProcess)
    return _StubProcess


# ---------------------------------------------------------------------------
# VP-001: pid field exists in /api/execution/{plan_id}/start response
# ---------------------------------------------------------------------------


class TestStartExecutionPayloadContainsPid:
    """Contract: response payload of /api/execution/{plan_id}/start has 'pid'."""

    def test_start_execution_response_payload_has_pid_field(self, stub_subprocess, tmp_path):
        """Top-level 'pid' key MUST be present in the response JSON."""
        plan_id = "vp001-pid-in-payload"
        _setup_plan(plan_id)
        project_dir = tmp_path / "target-vp001"
        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "pid" in body, (
            f"response missing 'pid' field: keys={list(body.keys())}"
        )

    def test_start_execution_pid_is_integer_type(self, stub_subprocess, tmp_path):
        """'pid' MUST be of type int (not str, not None)."""
        plan_id = "vp001-pid-int-type"
        _setup_plan(plan_id)
        project_dir = tmp_path / "target-vp001-int"
        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body.get("pid"), int), (
            f"pid must be int, got {type(body.get('pid')).__name__} "
            f"with value={body.get('pid')!r}"
        )
        # Specifically reject the string form "4242" which some naive
        # implementations may produce by str(process.pid).
        assert not isinstance(body.get("pid"), str)

    def test_start_execution_pid_matches_subprocess_pid(self, stub_subprocess, tmp_path):
        """'pid' MUST equal the OS PID reported by subprocess.Popen."""
        plan_id = "vp001-pid-matches-subproc"
        _setup_plan(plan_id)
        project_dir = tmp_path / "target-vp001-match"
        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pid"] == stub_subprocess.PID, (
            f"response pid {body['pid']!r} != subprocess pid "
            f"{stub_subprocess.PID!r}"
        )

    def test_start_execution_pid_present_when_project_dir_omitted(
        self, stub_subprocess
    ):
        """An empty ``project_dir`` MUST be rejected with 400.

        Audit 2026-07-16: when ``project_dir`` defaulted to
        ``<plan_dir>/project`` (a subdirectory of the backend's git repo),
        the executor's git_manager committed inside the backend repo while the
        actual work lived in a separate working tree, producing 11
        empty ``[task-N]`` commits on the backend's main main
        (``f7a784e9`` .. ``5f28a5e0``). The fix refuses requests that
        do not name an explicit ``project_dir``.

        This test used to assert 200 + an integer ``pid`` — the
        pre-audit contract. The pid-in-payload shape is still covered
        by the three sibling tests above; what this test now pins is
        the rejection itself, which is the part that matters when
        ``project_dir`` is absent.
        """
        plan_id = "vp001-pid-default-project"
        _setup_plan(plan_id)
        resp = client.post(f"/api/execution/{plan_id}/start", json={})
        assert resp.status_code == 400, (
            f"empty project_dir should be rejected with 400, got "
            f"{resp.status_code}: {resp.text}"
        )
        body = resp.json()
        detail = body.get("detail", "") if isinstance(body, dict) else ""
        assert "project_dir" in detail.lower(), (
            f"error message should mention project_dir, got: {detail!r}"
        )

    def test_start_execution_pid_not_none(self, stub_subprocess, tmp_path):
        """pid MUST be a non-None integer (caught None-vs-missing ambiguity)."""
        plan_id = "vp001-pid-not-none"
        _setup_plan(plan_id)
        project_dir = tmp_path / "target-vp001-non-none"
        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(project_dir)},
        )
        body = resp.json()
        assert body.get("pid") is not None, (
            f"pid should not be None after a successful start; body={body!r}"
        )

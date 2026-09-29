"""VP-004: POST /api/execution/{plan_id}/start MUST refuse (HTTP 400) when
``project_dir`` points at (or under) the running server's own ``backend/``
directory — a path-traversal / self-modification guard.

Verification-point contract (from verification_plan.json):

    Title:    拒绝在 backend/ 目录内执行（路径穿越防护）
    Method:   api_test
    Priority: high
    Expected: when project_dir points to backend/ or a subdirectory of it,
              the endpoint returns HTTPException(400) with a Chinese
              rejection detail containing "拒绝在 backend/ 目录内执行";
              no path-traversal side-effects and no execution.json write.

Production code path (backend/server.py @app.post(
"/api/execution/{plan_id}/start")) resolves the request's ``project_dir``,
then computes ``_self_root = Path(__file__).parent.parent.resolve()`` and
``_backend_dir = _self_root / "backend"``. If ``project_dir == _backend_dir``
or ``_backend_dir in project_dir.parents``, the endpoint raises
``HTTPException(400, "拒绝在 backend/ 目录内执行（…）")`` BEFORE any
``subprocess.Popen`` call and BEFORE any ``execution.json`` write.

Tests in this module use FastAPI's ``TestClient`` and stub
``subprocess.Popen`` with a tracking fake. They assert:
  1. Response status is 400.
  2. Response body's ``detail`` field contains the substring
     "拒绝在 backend/ 目录内执行".
  3. ``subprocess.Popen`` was NOT called (no executor spawned).
  4. ``execution.json`` was NOT written (no state side-effect).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import app, _execution_state, _execution_locks


client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    yield
    _execution_state.clear()
    _execution_locks.clear()


def _setup_plan(plan_id: str) -> Path:
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


class TestBackendDirGuardRejectsWith400:
    """Path-traversal guard: project_dir == backend/ ⇒ HTTP 400 with 中文 detail."""

    def test_start_execution_backend_dir_root_returns_400(self, monkeypatch):
        """project_dir == <root>/backend MUST be refused with HTTP 400."""
        plan_id = "vp004-backend-dir-root"
        plan_dir = _setup_plan(plan_id)

        popen_calls = []

        class _FakeProcess:
            def __init__(self, *args, **kwargs):
                popen_calls.append((args, kwargs))
                self.pid = 9999
                self.returncode = 0
                self.stdout = iter([])

            def wait(self):
                return 0

            def poll(self):
                return 0

        monkeypatch.setattr("server.subprocess.Popen", _FakeProcess)
        monkeypatch.setattr(
            "server._run_auto_verification_loop", lambda *a, **kw: None
        )
        monkeypatch.setattr("server._lazy_check_execution", lambda plan_id: None)

        backend_root = Path(server_module_file()).resolve().parent.parent
        backend_dir = (backend_root / "backend").resolve()

        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={"project_dir": str(backend_dir), "sync_targets": ["telegram"]},
        )

        assert resp.status_code == 400, (
            f"expected HTTP 400 for project_dir pointing at backend/, "
            f"got {resp.status_code}; body={resp.text!r}"
        )
        body = resp.json()
        detail = body.get("detail", "")
        assert "拒绝在 backend/ 目录内执行" in detail, (
            f"400 detail did NOT contain '拒绝在 backend/ 目录内执行'; "
            f"got detail={detail!r}, body={body!r}"
        )
        assert popen_calls == [], (
            f"subprocess.Popen was called even though the backend_dir guard "
            f"fired; calls={popen_calls!r}"
        )
        exec_file = plan_dir / "execution.json"
        assert not exec_file.exists(), (
            f"execution.json was unexpectedly written at {exec_file} even "
            f"though the request was rejected with 400"
        )
        assert plan_id not in _execution_state, (
            f"_execution_state unexpectedly contains plan_id={plan_id!r} "
            f"after the 400 backend_dir refusal"
        )

    def test_start_execution_backend_dir_subdir_returns_400(self, monkeypatch):
        """project_dir pointing UNDER backend/ (e.g. backend/cli.py) ⇒ HTTP 400."""
        plan_id = "vp004-backend-dir-subdir"
        plan_dir = _setup_plan(plan_id)

        popen_calls = []

        class _FakeProcess:
            def __init__(self, *args, **kwargs):
                popen_calls.append((args, kwargs))
                self.pid = 9999
                self.returncode = 0
                self.stdout = iter([])

            def wait(self):
                return 0

            def poll(self):
                return 0

        monkeypatch.setattr("server.subprocess.Popen", _FakeProcess)
        monkeypatch.setattr(
            "server._run_auto_verification_loop", lambda *a, **kw: None
        )
        monkeypatch.setattr("server._lazy_check_execution", lambda plan_id: None)

        backend_root = Path(server_module_file()).resolve().parent.parent
        subdir_under_backend = (backend_root / "backend" / "tests").resolve()

        resp = client.post(
            f"/api/execution/{plan_id}/start",
            json={
                "project_dir": str(subdir_under_backend),
                "sync_targets": ["telegram"],
            },
        )

        assert resp.status_code == 400, (
            f"expected HTTP 400 for project_dir under backend/, "
            f"got {resp.status_code}; body={resp.text!r}"
        )
        body = resp.json()
        detail = body.get("detail", "")
        assert "拒绝在 backend/ 目录内执行" in detail, (
            f"400 detail did NOT contain '拒绝在 backend/ 目录内执行'; "
            f"got detail={detail!r}, body={body!r}"
        )
        assert popen_calls == [], (
            f"subprocess.Popen was called even though the backend_dir guard "
            f"fired; calls={popen_calls!r}"
        )
        exec_file = plan_dir / "execution.json"
        assert not exec_file.exists(), (
            f"execution.json was unexpectedly written at {exec_file} even "
            f"though the request was rejected with 400"
        )


def server_module_file() -> str:
    """Return the filesystem path to the live server module.

    Imported lazily so module-level imports don't pull in the production
    module before monkeypatches are applied.
    """
    import server as _server

    return _server.__file__
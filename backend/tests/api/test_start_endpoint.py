"""VP-001 — ``project_dir`` is REQUIRED for ``POST /api/execution/{plan_id}/start``.

2026-09-13 contract update (two deliberate production changes made this
module's original VP-001/VP-002/VP-005 assertions stale):

1. **project_dir became REQUIRED** (audit 2026-07-16): the endpoint
   rejects a body without ``project_dir`` with HTTP 400 —
   ``"project_dir is required. Provide the absolute path ..."``.
   The old "default to ``<plan_dir>/project``" fallback was revoked
   because a silent default could produce empty commits in the wrong
   git repo. The VP-001 test now PINS that 400 contract (a missing
   ``project_dir`` must be rejected, not defaulted).

2. **execution.json was retired**: run state persists to the
   ``plan_execution`` SQLite row (source of truth). The old
   ``_read_execution_json`` assertions are ported to the
   ``state_db_reader`` fixture, which reads the hermetic per-test
   ``PDT_STATE_DB_PATH`` database — never the live ``state.db``.

The remaining tests pin: synchronous pid in the POST payload
(VP-002), explicit project_dir honored end-to-end (VP-003), SQLite
persistence before the response returns (VP-005), exact 4-key response
shape (VP-008).

Test isolation
--------------
Same pattern as ``test_start_api_backward_compat.py``: ``FakeProcess``
mocks ``subprocess.Popen`` so no real executor is spawned,
``_run_auto_verification_loop`` and ``_lazy_check_execution`` are
stubbed so the heartbeat / auto-verification thread does not perform
real LLM calls, and ``PLANS_DIR`` is redirected to ``tmp_path`` per
test.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import (
    _execution_locks,
    _execution_state,
    app,
)


client = TestClient(app)


# ---------------------------------------------------------------------------
# Test fixtures — same pattern as the rest of the start-endpoint suite
# ---------------------------------------------------------------------------


class FakeProcess:
    """Fake ``subprocess.Popen`` that blocks until released.

    Stores the args it was created with so the test can inspect what
    ``subprocess.Popen`` was invoked with (working directory, command,
    env) — that is the proof that ``process spawns normally`` per the
    VP-001 contract.
    """

    def __init__(self):
        self.pid = 4242
        self._block = threading.Event()
        self.args = None
        self.kwargs = None

    @property
    def returncode(self):
        return 0

    @property
    def stdout(self):
        self._block.wait(timeout=30)
        return iter([])

    def wait(self, timeout=None):
        self._block.wait(timeout=timeout or 30)
        return 0

    def poll(self):
        if self._block.is_set():
            return 0
        return None

    def terminate(self):
        self._block.set()

    def release(self):
        self._block.set()


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Reset global execution state and redirect PLANS_DIR per test."""
    _execution_state.clear()
    _execution_locks.clear()
    monkeypatch.setattr("server.PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(
        "server._run_auto_verification_loop",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("server._lazy_check_execution", lambda plan_id: None)
    yield
    _execution_state.clear()
    _execution_locks.clear()


def _setup_plan_dir(plan_id: str, plans_dir: Path) -> Path:
    """Create a minimal plan directory with tasks.json + plan_state.json."""
    plan_dir = plans_dir / plan_id
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


def _read_execution_row(state_db_reader, plan_id: str) -> dict:
    """Load the plan_execution SQLite row; raise AssertionError if missing."""
    row = state_db_reader.execution(plan_id)
    assert row is not None, (
        f"plan_execution row was not written for {plan_id} after start"
    )
    return row


# ---------------------------------------------------------------------------
# VP-001 — project_dir is REQUIRED (audit 2026-07-16 revoked the default fallback)
# ---------------------------------------------------------------------------


def test_missing_project_dir_returns_400(monkeypatch, state_db_reader):
    """POST without ``project_dir`` MUST be rejected with HTTP 400.

    VP-001 contract (2026-07-16 audit, pinned 2026-09-13):
        * When the body omits ``project_dir``, the endpoint MUST
          short-circuit with HTTP 400 and a ``detail`` message stating
          that ``project_dir is required`` — NOT silently default to
          ``<plan_dir>/project``.
        * ``subprocess.Popen`` MUST NOT be invoked.

    Why the old default was revoked
    -------------------------------
    Defaulting to a relative/implicit path silently produces empty
    commits in the wrong git repo (audit 2026-07-16). Callers must
    state explicitly where the executor should checkout / commit.

    Test design
    -----------
    * Post an empty JSON body (``{}``) — the worst-case old-caller shape.
    * Monkeypatch ``server.subprocess.Popen`` to a sentinel that records
      invocation, proving the endpoint rejected before spawn.
    """
    from server import PLANS_DIR

    plan_id = "vp-001-fallback"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    popen_calls = []
    monkeypatch.setattr(
        "server.subprocess.Popen",
        lambda *args, **kwargs: (popen_calls.append((args, kwargs)) or FakeProcess()),
    )

    # Empty body — omit ``project_dir`` entirely.
    resp = client.post(f"/api/execution/{plan_id}/start", json={})

    assert resp.status_code == 400, (
        f"start endpoint returned HTTP {resp.status_code}, expected 400; "
        f"body={resp.text!r}"
    )
    assert "project_dir is required" in resp.json().get("detail", ""), (
        f"400 detail must explain project_dir is required; "
        f"got {resp.json()!r}"
    )

    assert popen_calls == [], (
        f"subprocess.Popen must NOT be invoked when project_dir is "
        f"missing; was called {len(popen_calls)} time(s)"
    )

    row = state_db_reader.execution(plan_id)
    assert row is None, (
        f"no plan_execution row may be written for a rejected request; "
        f"row={row!r}"
    )


# ---------------------------------------------------------------------------
# VP-002 — synchronous subprocess spawn returns pid in payload (no polling)
# ---------------------------------------------------------------------------


def test_pid_in_response_payload(monkeypatch):
    """POST ``/api/execution/{plan_id}/start`` MUST return a non-empty pid synchronously.

    VP-002 contract (DP-start-2):
        * ``subprocess.Popen`` must execute on the request handler thread
          (not a background thread), so ``process.pid`` is known BEFORE
          the function returns.
        * The HTTP response MUST carry a ``pid`` field that is a positive
          integer (proves the spawn happened and the kernel assigned a pid).
        * Immediately after the POST returns (no sleep, no polling loop),
          ``GET /api/execution/{plan_id}/status`` MUST report the SAME
          ``pid``. This is the no-polling guarantee: the caller does not
          need to retry / wait for the spawn to land.

    Test design
    -----------
    * Patch ``server.subprocess.Popen`` with ``FakeProcess`` so the test
      does not depend on real OS subprocess scheduling, and so the test
      runs even if the underlying ``cli.py`` is not present in the venv.
    * Make ONE POST then ONE GET — no ``time.sleep`` between them. The
      assertion is on ``response == POST pid == GET pid``, not on a
      polling deadline.
    * Assert pid type (int) and value (> 0) so a regression that returns
      ``pid=None`` or ``pid="4242"`` (string) is caught.
    """
    from server import PLANS_DIR

    plan_id = "vp-002-pid-payload"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    # ---- (1) POST /api/execution/{plan_id}/start --------------------------
    # project_dir is REQUIRED since the 2026-07-16 audit (the old
    # <plan_dir>/project default fallback was revoked).
    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str((plan_dir / "project").resolve())},
    )
    assert resp.status_code == 200, (
        f"start endpoint returned HTTP {resp.status_code}, expected 200; "
        f"body={resp.text!r}"
    )
    body = resp.json()

    # VP-002 asserts pid is non-empty in the POST payload — proves spawn
    # happened on the request thread, NOT deferred to a background thread.
    assert "pid" in body, (
        f"POST response missing 'pid' field; got keys={list(body.keys())!r}"
    )
    post_pid = body["pid"]
    assert post_pid is not None, (
        f"POST response.pid is None; subprocess.Popen must run synchronously "
        f"on the request thread so process.pid is available before return"
    )
    assert isinstance(post_pid, int), (
        f"POST response.pid must be int (process.pid type); "
        f"got {type(post_pid).__name__}={post_pid!r}"
    )
    assert post_pid > 0, (
        f"POST response.pid must be a positive OS-assigned pid; got {post_pid!r}"
    )

    # ---- (2) GET /api/execution/{plan_id}/status — no polling ------------
    # Critical: NO time.sleep, NO retry loop. The whole point of VP-002
    # is that the synchronous spawn makes the pid visible immediately.
    status_resp = client.get(f"/api/execution/{plan_id}/status")
    assert status_resp.status_code == 200, (
        f"status endpoint returned HTTP {status_resp.status_code}, expected 200; "
        f"body={status_resp.text!r}"
    )
    status_body = status_resp.json()

    # The status endpoint must surface the pid so the LUI / bridge UIs
    # can show "PID: 4242" without an extra round-trip to /progress.
    assert "pid" in status_body, (
        f"GET /status response missing 'pid' field; got "
        f"keys={list(status_body.keys())!r}"
    )
    get_pid = status_body["pid"]
    assert get_pid is not None, (
        f"GET /status.pid is None even though POST returned pid={post_pid}; "
        f"the synchronous spawn contract is broken — pid should be in "
        f"_execution_state[plan_id]['pid'] immediately after Popen returns"
    )
    assert get_pid == post_pid, (
        f"GET /status.pid={get_pid!r} does not match POST response.pid="
        f"{post_pid!r}; the pid recorded at spawn time must be the same pid "
        f"served by the status endpoint (no async drift)"
    )

    fake.release()


# ---------------------------------------------------------------------------
# VP-003 — explicit project_dir is honored end-to-end (backward compat)
# ---------------------------------------------------------------------------


def test_explicit_project_dir(monkeypatch, tmp_path, state_db_reader):
    """POST ``/api/execution/{plan_id}/start`` with an explicit ``project_dir`` MUST use it.

    VP-003 contract (backward compat):
        * When the request body explicitly passes ``project_dir`` as an
          absolute path, ``project_dir`` returned in the response MUST
          equal that path after ``expanduser`` + ``resolve``.
        * ``execution.json`` MUST persist the same value (canonical
          record consulted by every downstream endpoint).
        * The subprocess MUST be spawned (response carries a positive
          ``pid``) and the spawned command MUST include the
          ``-w <project_dir>`` argument so cli.py's autonomous-coding
          agent runs in the right working directory.
        * The response MUST report ``status == "running"`` immediately
          after spawn.

    Why this matters
    ----------------
    Old / minimal clients explicitly pass ``project_dir`` because they
    pre-date the VP-001 default-fallback. The endpoint MUST still
    honor their explicit choice — silently falling back to
    ``<plan_dir>/project`` would break every existing caller that
    relies on running tasks against a real checkout (e.g. a
    pre-prepared repo from a previous plan).

    Test design
    -----------
    * Create a real on-disk directory (``tmp_path / "explicit_proj"``)
      and use that as the explicit ``project_dir`` — the test then
      asserts the response and ``execution.json`` match this path
      exactly (post-resolve), AND that the directory was actually
      touched by the endpoint (``mkdir(parents=True, exist_ok=True)``
      would have created it if it didn't exist; we pre-create it so
      we can also verify ``cwd``/``-w`` semantics).
    * Patch ``server.subprocess.Popen`` with a recording ``FakeProcess``
      so the test can assert the ``cmd`` and ``cwd`` kwargs the
      endpoint actually used.
    """
    from server import PLANS_DIR

    plan_id = "vp-003-explicit"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    # Pre-create the explicit project directory so we can refer to it
    # as an absolute path AND so the endpoint's mkdir is a no-op
    # (lets us assert no side effects on the directory).
    explicit_project_dir = (tmp_path / "explicit_proj").resolve()
    explicit_project_dir.mkdir(parents=True, exist_ok=True)

    # Capturing FakeProcess: store the args/kwargs Popen was called
    # with so the test can assert cmd and cwd.
    captured = {}

    class RecordingFakeProcess(FakeProcess):
        def __init__(self):
            super().__init__()

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return RecordingFakeProcess()

    monkeypatch.setattr("server.subprocess.Popen", fake_popen)

    # ---- POST with explicit project_dir ---------------------------------
    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str(explicit_project_dir)},
    )
    assert resp.status_code == 200, (
        f"start endpoint returned HTTP {resp.status_code}, expected 200; "
        f"body={resp.text!r}"
    )
    body = resp.json()

    # (a) response.project_dir MUST equal the explicit path after
    # expanduser + resolve. We pass an already-absolute, already-resolved
    # path, so the response should be byte-identical to the input.
    assert body["project_dir"] == str(explicit_project_dir), (
        f"response.project_dir must equal explicit path "
        f"{str(explicit_project_dir)!r}; got {body['project_dir']!r}"
    )

    # (b) the plan_execution SQLite row MUST persist the same value —
    # that is the canonical record consulted by /status, /progress,
    # /files.
    row = _read_execution_row(state_db_reader, plan_id)
    assert row["project_dir"] == str(explicit_project_dir), (
        f"plan_execution.project_dir must equal explicit path "
        f"{str(explicit_project_dir)!r}; got {row.get('project_dir')!r}"
    )

    # (c) process spawns normally — response status=="started" (spawn
    # acknowledgement) + positive pid.
    assert body["status"] == "started", (
        f"start endpoint must report status='started' immediately after "
        f"spawn; got {body.get('status')!r}"
    )
    assert body.get("pid") and int(body["pid"]) > 0, (
        f"start endpoint must return a positive pid proving subprocess "
        f"spawn worked; got pid={body.get('pid')!r}"
    )

    # (d) The spawned command MUST include the -w flag with the
    # explicit project_dir so cli.py's autonomous-coding agent runs
    # against the right working directory.
    assert captured.get("args"), (
        "subprocess.Popen was never called; the endpoint must spawn a "
        "subprocess for explicit project_dir requests too"
    )
    cmd = captured["args"][0]
    assert "-w" in cmd, (
        f"spawned command must include '-w' working-directory flag; "
        f"got cmd={cmd!r}"
    )
    w_idx = cmd.index("-w")
    assert cmd[w_idx + 1] == str(explicit_project_dir), (
        f"spawned command's -w argument must equal explicit "
        f"project_dir={str(explicit_project_dir)!r}; got "
        f"{cmd[w_idx + 1]!r}"
    )

    # Release the FakeProcess so the daemon thread can exit cleanly.
    fake_obj = captured.get("kwargs", {})
    # No-op: the captured FakeProcess is local; just let it block
    # until the test ends (the daemon thread won't block pytest exit
    # for long, and the test cleanup is best-effort).



# ---------------------------------------------------------------------------
# VP-007 — missing tasks.json returns 404 without spawning a subprocess
# ---------------------------------------------------------------------------


def test_missing_tasks_returns_404(monkeypatch):
    """POST ``/api/execution/{plan_id}/start`` MUST return 404 when ``tasks.json`` is absent.

    VP-007 contract:
        * When ``plans/{plan_id}/tasks.json`` does NOT exist (the user
          called ``/start`` before ``/tasks/generate``), the endpoint
          MUST short-circuit BEFORE spawning any subprocess.
        * Response MUST be HTTP 404 with body
          ``{"detail": "Tasks not found — generate tasks first"}``.
        * ``subprocess.Popen`` MUST NOT be invoked.
    """
    from server import PLANS_DIR

    plan_id = "vp-007-missing-tasks"
    plan_dir = PLANS_DIR / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    # NOTE: deliberately NOT creating tasks.json

    class PopenCalled:
        def __init__(self):
            self.called = False

        def __call__(self, *args, **kwargs):
            self.called = True
            return FakeProcess()

    sentinel = PopenCalled()
    monkeypatch.setattr("server.subprocess.Popen", sentinel)

    resp = client.post(f"/api/execution/{plan_id}/start", json={})

    assert resp.status_code == 404, (
        f"start endpoint must return HTTP 404 when tasks.json is missing; "
        f"got HTTP {resp.status_code}; body={resp.text!r}"
    )

    body = resp.json()
    assert body == {"detail": "Tasks not found — generate tasks first"}, (
        f"start endpoint must return exact body "
        f'{{"detail": "Tasks not found — generate tasks first"}}; '
        f"got {body!r}"
    )

    assert sentinel.called is False, (
        "subprocess.Popen must NOT be invoked when tasks.json is missing; "
        "the endpoint must short-circuit with 404 before reaching spawn"
    )

    from server import _execution_state as exec_state
    assert plan_id not in exec_state, (
        f"_execution_state must remain empty after a 404 response; "
        f"found entries: {list(exec_state.keys())!r}"
    )

    assert not (plan_dir / "execution.json").exists(), (
        "execution.json must not be written when start returns 404; "
        "the 404 path must not have any side effects on disk"
    )


# ---------------------------------------------------------------------------
# VP-005 — plan_execution row is persisted BEFORE the response returns
# ---------------------------------------------------------------------------


def test_execution_row_persisted_before_response(monkeypatch, state_db_reader):
    """plan_execution row MUST be visible (exec_pid + running) before POST returns.

    VP-005 contract (DP-start-5), ported 2026-09-13 from the retired
    ``execution.json`` to the SQLite ``plan_execution`` row:
        * After ``POST /api/execution/{plan_id}/start`` returns, reading
          the row immediately MUST show ``exec_pid`` non-null and
          ``exec_status == "running"`` — WITHOUT waiting for the
          background reader thread.
        * This proves the canonical record is written on the request
          handler thread (right after ``subprocess.Popen`` assigns
          ``process.pid``), not deferred to the ``_run`` daemon.

    Test design
    -----------
    * ``FakeProcess.wait()`` blocks for up to 30s, so the ``_run`` daemon
      thread is guaranteed to still be parked inside ``process.wait()``
      when we read the row. Any pid/status observed therefore came from
      the request thread, NOT the daemon.
    * NO ``time.sleep``, NO polling: read the row the instant POST returns.
    """
    from server import PLANS_DIR

    plan_id = "vp-005-persist-before-return"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str((plan_dir / "project").resolve())},
    )
    assert resp.status_code == 200, (
        f"start endpoint returned HTTP {resp.status_code}, expected 200; "
        f"body={resp.text!r}"
    )

    # Read the plan_execution row the instant the POST returns — no
    # sleep, no polling. The daemon thread is blocked in
    # FakeProcess.wait(), so anything present here was written
    # synchronously on the request thread before the response was sent.
    row = _read_execution_row(state_db_reader, plan_id)

    pid = row.get("exec_pid")
    assert pid is not None, (
        f"plan_execution.exec_pid must be non-null immediately after "
        f"POST returns (persisted synchronously on the request thread); "
        f"got exec_pid={pid!r}"
    )
    assert isinstance(pid, int) and pid > 0, (
        f"plan_execution.exec_pid must be a positive OS-assigned pid; "
        f"got {pid!r}"
    )
    assert row.get("exec_status") == "running", (
        f"plan_execution.exec_status must be 'running' immediately after "
        f"POST returns; got {row.get('exec_status')!r}"
    )

    # Cross-check: persisted pid matches the pid in the POST response.
    assert pid == resp.json().get("pid"), (
        f"plan_execution.exec_pid={pid!r} must match the pid in the "
        f"POST response={resp.json().get('pid')!r}"
    )

    fake.release()

# ---------------------------------------------------------------------------
# VP-008 — response field completeness (plan_id / status / pid / project_dir)
# ---------------------------------------------------------------------------


def test_response_field_contract(monkeypatch):
    """POST /api/execution/{plan_id}/start success payload MUST have exactly the 4-key contract.

    VP-008 contract:
        * On a successful start the response body MUST be a dict whose key
          set is EXACTLY {"plan_id", "status", "pid", "project_dir"} — no
          missing keys and no extra internal fields leaking.
        * status MUST be "started" (the spawn acknowledgement), while the
          persisted execution state remains "running".
    """
    from server import PLANS_DIR

    plan_id = "vp-008-field-contract"
    plan_dir = _setup_plan_dir(plan_id, PLANS_DIR)

    fake = FakeProcess()
    monkeypatch.setattr("server.subprocess.Popen", lambda *args, **kwargs: fake)

    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": str((plan_dir / "project").resolve())},
    )
    assert resp.status_code == 200, (
        f"start endpoint returned HTTP {resp.status_code}, expected 200; "
        f"body={resp.text!r}"
    )

    body = resp.json()

    assert isinstance(body, dict), (
        f"start response must be a JSON object (dict); got "
        f"{type(body).__name__}={body!r}"
    )

    expected_keys = {"plan_id", "status", "pid", "project_dir"}
    assert set(body.keys()) == expected_keys, (
        f"start response key set must be exactly {sorted(expected_keys)!r}; "
        f"got {sorted(body.keys())!r}"
    )

    assert body["status"] == "started", (
        f"start response.status must equal 'started' immediately after spawn; "
        f"got {body['status']!r}"
    )

    fake.release()



# ---------------------------------------------------------------------------
# VP-010 — project_dir safety: refuse to execute inside backend/ directory
# ---------------------------------------------------------------------------


def test_refuse_backend_dir(monkeypatch, tmp_path):
    """POST /api/execution/{plan_id}/start MUST refuse when project_dir points to backend.

    VP-010 contract:
        * When the request body passes project_dir as a path that
          resolves to the running service's backend/ directory, the
          endpoint MUST short-circuit BEFORE spawning any subprocess.
        * Response MUST be a 4xx HTTP error (HTTPException 400) - not 2xx.
        * subprocess.Popen MUST NOT be invoked.
        * _execution_state MUST remain empty for that plan_id.
        * execution.json MUST NOT be written.
    """
    from server import PLANS_DIR

    plan_id = "vp-010-backend-refuse"
    _setup_plan_dir(plan_id, PLANS_DIR)

    class PopenCalled:
        def __init__(self):
            self.called = False

        def __call__(self, *args, **kwargs):
            self.called = True
            return FakeProcess()

    sentinel = PopenCalled()
    monkeypatch.setattr("server.subprocess.Popen", sentinel)

    # Build a project_dir that points to backend/. The endpoint computes
    # _backend_dir as Path(__file__).parent.parent / "backend"; reproduce.
    from pathlib import Path as _P
    _server_file = _P(__file__)  # this test file lives in backend/tests/api/
    _backend_dir = (_server_file.parent.parent.parent).resolve() / "backend"
    bad_project_dir = str(_backend_dir)

    resp = client.post(
        f"/api/execution/{plan_id}/start",
        json={"project_dir": bad_project_dir},
    )

    assert 400 <= resp.status_code < 600, (
        f"start endpoint must return HTTP 4xx/5xx when project_dir is "
        f"inside backend/; got HTTP {resp.status_code}; body={resp.text!r}"
    )

    assert sentinel.called is False, (
        "subprocess.Popen must NOT be invoked when project_dir is inside "
        "backend/; the endpoint must short-circuit with 4xx before reaching spawn"
    )

    from server import _execution_state as exec_state
    assert plan_id not in exec_state, (
        f"_execution_state must remain empty after a 4xx response; "
        f"found entries: {list(exec_state.keys())!r}"
    )

    plan_dir = PLANS_DIR / plan_id
    assert not (plan_dir / "execution.json").exists(), (
        "execution.json must not be written when start returns 4xx; "
        "the refused path must not have any side effects on disk"
    )

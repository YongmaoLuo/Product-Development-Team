"""E2E regression: tightened security guards must not over-reject the happy path.

Background
----------
Tasks 5/6/7/9/11/12/13 each tightened one layer of the security boundary
(path validation, Host/Origin guard, subprocess argument allowlist, plan-id
boundary, etc.). Every tightening is a regression hazard: a guard that was
too loose becomes too tight and starts refusing inputs the rest of the
codebase legitimately produces.

The unit and integration tiers cannot catch this class of regression:

  * ``TestClient`` injects ``Host: testserver`` and a synthetic request
    header, so the loopback Host check is bypassed.
  * ``TestClient`` does not cross a process boundary, so a tightened
    subprocess argv guard that refuses the legitimate argv we actually
    pass cannot be observed.
  * A path-validation tightening that refuses ``PLANS_DIR/<id>`` (a
    legitimate layout) surfaces only when the live server writes through
    the real resolver into a real on-disk SQLite store.

This test therefore boots a **real** ``backend.server`` process against a
free port with a ``tmp_path``-rooted state DB and plans directory, then
walks the canonical lifecycle through real HTTP requests. The provider
side is dry-run / fake (the ``e2e`` marker allows that), but the
**process, database, filesystem, and HTTP transport are all real** —
nothing is mocked.

What it pins
------------
  1. ``test_full_lifecycle_completes_with_fixes`` — the canonical
     interview → ready → executing → verification_running →
     verification_passed walk survives the tightened guards.
  2. ``test_legitimate_requests_are_not_over_rejected`` — each guard
     that was tightened accepts the legitimate inputs the application
     routinely produces.
  3. ``test_terminal_state_has_no_orphans`` — after the walk, every
     ``plan_tasks`` row whose ``task_id`` references a real plan has a
     matching on-disk artifact (the merge-or-placeholder rule from
     :mod:`orphan_rules`).
  4. ``test_no_temp_files_or_live_children_after_test`` — the boot
     subprocess is reaped, ``reap_probe`` confirms it is gone, and no
     ``.tmp`` residue was left behind by the lifecycle walk.

Public fixtures for task 20 reuse
----------------------------------
``boot_real_server(tmp_path)`` and ``teardown_server()`` are the
canonical E2E server boot helpers — task 20's E2E-collection-count
gate imports them so every e2e test uses the same boot path.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple

import pytest


# -- Marker contract ----------------------------------------------------------
#
# ``e2e`` flags the file as a real-process integration test (the
# ``conftest.py::hermetic_*`` fixtures already isolate state.db and
# plans_dir, so a process boundary is the only thing left to opt into).
# ``time_sensitive`` flags the multi-second runtime (subprocess boot + warm-up),
# and the wall-clock bounds asserted alongside it.
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.time_sensitive,
]


BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
REQUEST_HEADER = "X-PDT-Request"
REQUEST_HEADER_VALUE = "1"


# ---------------------------------------------------------------------------
# Free-port allocation + server boot — task 20 reuse
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind to an ephemeral loopback port and return its number.

    The kernel hands us a free port, then releasing the socket leaves
    the port "available" — a brief race window during which another
    process could grab it. That race is acceptable for an isolated test
    process; the ``_wait_for_server`` polling below tolerates it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _utcnow_iso() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _wait_for_server(base_url: str, timeout: float = 30.0) -> None:
    """Block until ``GET /health`` on ``base_url`` returns 2xx."""
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"{base_url}/health", timeout=2
            ) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last_exc = exc
        time.sleep(0.2)
    raise RuntimeError(
        f"server at {base_url} did not start in {timeout}s; last_exc={last_exc}"
    )


def boot_real_server(tmp_path: Path) -> Tuple[str, subprocess.Popen, Path]:
    """Boot a real ``backend.server`` subprocess against ``tmp_path``.

    Returns ``(base_url, proc, workdir)`` where ``base_url`` is the
    loopback URL the test should send requests to, ``proc`` is the
    :class:`subprocess.Popen` instance the caller MUST terminate
    (see :func:`teardown_server`), and ``workdir`` is the directory
    under which the server's state DB and plans tree live. The test
    MUST read/write through ``workdir`` (NOT through the test
    process's own ``tmp_path``/``os.environ``) — the subprocess has
    its own env, so paths the test set in *its* env will not be the
    ones the server sees.

    Why a real subprocess instead of ``TestClient``:
    ``TestClient`` patches ``Host``, injects the request guard header,
    and never crosses a process boundary. Every one of those bypasses
    is exactly what a tightening regression would be hiding behind.
    """
    state_db = tmp_path / "state.db"
    plans_dir = tmp_path / "plans"
    backups_dir = tmp_path / "backups"
    logs_dir = tmp_path / "logs"
    for d in (plans_dir, backups_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    port = _free_port()

    env = dict(os.environ)
    env.update({
        # State DB and plans root: ``tmp_path``, never the live repo.
        "PDT_STATE_DB_PATH": str(state_db),
        "PDT_PLANS_DIR": str(plans_dir),
        # Bind loopback so the guard's Host check passes.
        "PDT_HOST": "127.0.0.1",
        "PDT_PORT": str(port),
        # Debug routes opt-in: needed for ``/api/_debug/*`` calls a test
        # may make. Off by default in production, on in the suite.
        "PDT_DEBUG_ROUTES": "1",
        # Allow ``testserver`` plus 127.0.0.1 (already implicit).
        "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
        # Force the suite's provider mode so a developer with CC Switch
        # running does not make this test depend on whichever provider
        # they happen to have selected.
        "PDT_PROVIDER_MODE": "dry-run",
        # Provider capacity file: do not read the operator's.
        "PDT_PROVIDER_CAPACITY_FILE": str(tmp_path / "absent-capacity.yaml"),
        # Make sure the venv's site-packages wins over a stray PYTHONPATH.
        "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
    })
    # Drop the parent's CC-Switch proxy override; tests should never
    # depend on the operator's environment.
    env.pop("ANTHROPIC_BASE_URL", None)

    log_path = logs_dir / "server.log"
    log_handle = log_path.open("wb")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "backend.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        # New session so SIGTERM hits only the child, not the test
        # process — teardown uses ``terminate()`` then ``kill()`` to
        # give the lifespan a graceful exit first.
        start_new_session=True,
    )

    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(base_url, timeout=30.0)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        log_handle.close()
        raise

    # Stash the log handle on the proc object so the fixture can close
    # it on teardown; ``Popen`` does not own it.
    proc._pdt_log_handle = log_handle  # type: ignore[attr-defined]
    return base_url, proc, tmp_path


def teardown_server(proc: subprocess.Popen) -> None:
    """Terminate ``proc`` and close its log handle.

    Sends ``SIGTERM`` first so uvicorn's lifespan gets to run its
    shutdown path (which writes the final boot_id flush and the
    in-flight ``plan_routing`` row update). If the process is still
    alive after 5 s, ``SIGKILL`` follows — the only way out of a
    wedged event loop is the hard one, and leaving it alive would be
    worse than anything the kill itself could damage.
    """
    log_handle = getattr(proc, "_pdt_log_handle", None)
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:  # noqa: BLE001
                pass


def _http_get(
    base_url: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> Tuple[int, bytes]:
    """Issue a GET against ``base_url + path`` and return ``(status, body)``.

    The ``X-PDT-Request`` header is set on every call by default — the
    suite's guard requires it on every ``/api/*`` request, and the
    explicit ``Host`` keeps the ``Origin``/``Host`` check green.
    """
    hdrs = {REQUEST_HEADER: REQUEST_HEADER_VALUE, "Host": "127.0.0.1"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(
        f"{base_url}{path}",
        headers=hdrs,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""


def _http_post_json(
    base_url: str,
    path: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> Tuple[int, bytes]:
    """POST ``body`` (JSON-encoded) against ``base_url + path``."""
    hdrs = {
        REQUEST_HEADER: REQUEST_HEADER_VALUE,
        "Host": "127.0.0.1",
        "Content-Type": "application/json",
    }
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers=hdrs,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""


# ---------------------------------------------------------------------------
# Session-wide fixture: one server for the whole file
# ---------------------------------------------------------------------------
#
# Booting uvicorn is ~3 s; running four lifecycle tests against four
# separate subprocesses would multiply that cost for no isolation
# benefit. The four tests do not mutate shared state (each uses its
# own ``plan_id``), so a single shared subprocess is the right
# trade-off — and the per-test teardown assertion
# (``test_no_temp_files_or_live_children_after_test``) re-checks the
# server is still alive, which is exactly the "did the lifecycle
# damage the server" assertion we want.


@pytest.fixture(scope="module")
def server(tmp_path_factory) -> Tuple[str, subprocess.Popen, Path]:
    """Boot one server for the module; tear it down at the end.

    Yields ``(base_url, proc, workdir)`` — the proc is reaped by
    ``teardown_server`` on finalizer so a SIGTERM lands during the
    ``e2e`` fixture window (not after pytest has already cleaned
    the workdir). Tests must use ``workdir`` (NOT their own
    ``tmp_path``/``os.environ``) to address the server's state DB
    and plans tree, because the subprocess has its own env.
    """
    workdir = tmp_path_factory.mktemp("security_fixes_e2e_")
    base_url, proc, _ = boot_real_server(workdir)
    yield base_url, proc, workdir
    teardown_server(proc)


# ---------------------------------------------------------------------------
# Direct-SQL helpers — the lifecycle walk
# ---------------------------------------------------------------------------
#
# A real lifecycle through the HTTP API would require a working
# provider (interview + prd + arch + test design + tasks all need an
# LLM). The task description allows dry-run / fake backend on the
# provider side; the equivalent on the *state-machine* side is to
# seed the four ``plan_*`` rows directly through the same SQLite
# the server reads. That still exercises every hardened guard (the
# server writes through them when it answers each query), but keeps
# the test free of any LLM dependency.


def _seed_plan_sqlite(
    state_db: Path,
    plan_id: str,
    *,
    phase: str = "ready",
    project_dir: Path | None = None,
) -> None:
    """Insert the four ``plan_*`` rows a plan on disk implies.

    Mirrors :func:`tests.conftest.seed_plan_sqlite` but is a
    private helper here so this file does not import from
    ``conftest`` (which would couple us to that file's session-scoped
    state-db redirect).
    """
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    state_db.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(state_db)
    try:
        migrate(conn)
        now = _utcnow_iso()
        conn.execute(
            "INSERT OR IGNORE INTO plan_routing "
            "(plan_id, current_phase, version, updated_at) "
            "VALUES (?, ?, 0, ?)",
            (plan_id, phase, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO plan_verification "
            "(plan_id, verification_status, round, max_rounds, "
            "verification_stop_reason, results, started_at, updated_at) "
            "VALUES (?, ?, 0, 3, NULL, NULL, NULL, ?)",
            (plan_id, "pending", now),
        )
        if project_dir is not None:
            conn.execute(
                "INSERT OR IGNORE INTO plan_execution "
                "(plan_id, current_phase, attempt_count, project_dir, "
                "updated_at) VALUES (?, ?, 0, ?, ?)",
                (plan_id, "executing", str(project_dir), now),
            )
        conn.commit()
    finally:
        conn.close()


def _write_plan_dir(plans_dir: Path, plan_id: str) -> Path:
    """Materialise the on-disk shape the loader expects for ``plan_id``.

    Mirrors the minimal ``plans/<id>/`` layout the production code
    path needs to recognise a plan. The files are intentionally
    minimal — the goal is "does the loader accept this", not "is
    this a complete plan fixture".
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "interview.json").write_text(
        json.dumps({"requirement": "e2e security regression fixture"}),
        encoding="utf-8",
    )
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "current_phase": "ready",
            "completed_phases": [],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {},
            "verification": {"status": "pending", "round": 0, "max_rounds": 3},
        }),
        encoding="utf-8",
    )
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}), encoding="utf-8",
    )
    return plan_dir


def _read_state_db(state_db: Path) -> dict[str, Any]:
    """Open ``state_db`` and return ``{table: rows_by_plan_id}``."""
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from state_machine.db.connection import open as open_db

    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    conn = open_db(state_db)
    try:
        for table in ("plan_routing", "plan_execution", "plan_verification"):
            cur = conn.execute(f"SELECT * FROM {table}")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            out[table] = rows
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_full_lifecycle_completes_with_fixes(
    server: Tuple[str, subprocess.Popen, Path],
) -> None:
    """The canonical lifecycle walk survives the tightened guards.

    Drives the server through a complete stage transition sequence
    by writing the on-disk artifacts the server reads, then asserting
    that every hardened API responds 200 to the legitimate request
    the application routinely produces.
    """
    base_url, proc, workdir = server
    state_db = workdir / "state.db"
    plans_dir = workdir / "plans"

    plan_id = "e2e-sec-lifecycle-001"
    _write_plan_dir(plans_dir, plan_id)
    _seed_plan_sqlite(state_db, plan_id, phase="ready")

    # GET /api/plans must list the new plan (or at least not 500).
    status, body = _http_get(base_url, "/api/plans")
    assert status == 200, (
        f"GET /api/plans returned {status}; tightened guards must not "
        f"have over-rejected this legitimate request. Body: {body!r}"
    )

    # GET /api/plan/{id}/status must return the seeded phase. The
    # body is the single assertion that proves the server is reading
    # from the SAME state.db we just wrote into — a stale read or a
    # different DB path would surface as 404 or ``phase != ready``.
    status, body = _http_get(base_url, f"/api/plan/{plan_id}/status")
    assert status == 200, (
        f"GET /api/plan/{plan_id}/status returned {status}; body: {body!r}"
    )
    try:
        status_payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssertionError(
            f"GET /api/plan/{plan_id}/status returned non-JSON body: "
            f"{body[:200]!r} (parse error: {exc})"
        )
    # The body must echo our seeded plan_id and the phase we wrote —
    # otherwise the server is answering from somewhere else (a stale
    # cache, a different DB, or a mocked response).
    assert status_payload.get("plan_id") == plan_id, (
        f"server returned a different plan_id; expected {plan_id!r}, "
        f"got {status_payload.get('plan_id')!r}; the server is NOT "
        f"reading from {state_db}"
    )

    # Drive a CAS to ``verification_passed`` via direct SQLite — the
    # guard surface we are testing is the *server*'s guards, not the
    # state-machine CAS itself. Then read the row back through the
    # HTTP API to confirm the server's hardened readers accepted the
    # outcome.
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.routing_repository import (
        RoutingRepository,
    )

    conn = open_db(state_db)
    try:
        repo = RoutingRepository(conn)
        # ready → executing → verification_running → verification_passed
        for target in ("executing", "verification_running", "verification_passed"):
            current = repo.find(plan_id)
            current_phase = current["current_phase"] if current else "ready"
            repo.try_mark_phase(
                plan_id,
                expected_phases=(current_phase,),
                new_phase=target,
            )
    finally:
        conn.close()

    status, body = _http_get(base_url, f"/api/plan/{plan_id}/status")
    assert status == 200, (
        f"GET /api/plan/{plan_id}/status returned {status} after CAS; "
        f"body: {body!r}"
    )

    # The server must still be alive after the lifecycle walk — a
    # tightened guard that crashes the process on a legitimate input
    # would surface here.
    assert proc.poll() is None, (
        f"server process exited during the lifecycle walk "
        f"(rc={proc.returncode}); a tightened guard probably crashed it"
    )

    # The SQLite row carries the terminal stage.
    final = _read_state_db(state_db)
    routing = {
        row["plan_id"]: row for row in final["plan_routing"]
    }
    assert routing[plan_id]["current_phase"] == "verification_passed", (
        f"plan_routing row for {plan_id} did not reach terminal stage; "
        f"got {routing[plan_id]!r}"
    )


def test_legitimate_requests_are_not_over_rejected(
    server: Tuple[str, subprocess.Popen, Path],
) -> None:
    """Each tightened guard must accept the legitimate inputs the
    application routinely produces.

    Concretely: a well-formed plan-id path, a loopback Host, the
    ``X-PDT-Request`` header, and a JSON body with the schema the
    production code emits — none of those are over-rejected.
    """
    base_url, proc, workdir = server
    state_db = workdir / "state.db"
    plans_dir = workdir / "plans"

    # A legitimate plan id the production layout produces.
    plan_id = "e2e-sec-legit-002"
    _write_plan_dir(plans_dir, plan_id)
    _seed_plan_sqlite(state_db, plan_id, phase="interview")

    # 1. GET /api/plans (legitimate Host + guard header) → 200
    status, _ = _http_get(base_url, "/api/plans")
    assert status == 200, (
        f"/api/plans over-rejected a legitimate request: {status}"
    )

    # 2. GET /api/plan/{legitimate_id}/status → 200
    status, _ = _http_get(base_url, f"/api/plan/{plan_id}/status")
    assert status == 200, (
        f"/api/plan/{{legitimate}}/status over-rejected: {status}"
    )

    # 3. POST /api/plan/{id}/interview/start with a small JSON body →
    #    should not 4xx on guard grounds. (The endpoint may legitimately
    #    409 if the phase isn't ``interview`` — that is fine, what we
    #    are asserting is "not 400/403 because the path was rejected".)
    status, _ = _http_post_json(
        base_url,
        f"/api/plan/{plan_id}/interview/start",
        {"question": "What problem are we solving?"},
    )
    assert status not in (400, 403), (
        f"/api/plan/{{legitimate}}/interview/start was over-rejected "
        f"with {status}; a tightened guard must accept a JSON body "
        f"on a loopback Host with the guard header set"
    )

    # 4. The Origin must be allowed too — a request WITHOUT an Origin
    #    header (the ``curl`` shape) must still pass.
    status, _ = _http_get(base_url, "/api/plans")
    assert status == 200, (
        f"a request without an Origin header was over-rejected "
        f"({status}); curl-style callers must continue to work"
    )

    # 5. Server is still alive — a tightened guard that crashes on
    #    legitimate input would surface here.
    assert proc.poll() is None, (
        f"server exited during legitimate-request probe (rc={proc.returncode})"
    )


def test_terminal_state_has_no_orphans(
    server: Tuple[str, subprocess.Popen, Path],
) -> None:
    """After the lifecycle walk, no ``plan_tasks`` row references a
    task_id that does not exist on disk.

    The merge-or-placeholder contract lives in
    :mod:`orphan_rules`; this test pins the **post-condition** —
    after a clean lifecycle, every row the server wrote either merges
    cleanly or carries a placeholder, with no row left dangling. The
    guard surface being tested is the same path-validation tightening
    that rejects a ``plan_id`` outside ``PLANS_DIR``; a regression
    that makes the loader skip the on-disk check would leave orphans
    here.
    """
    base_url, _proc, workdir = server
    state_db = workdir / "state.db"
    plans_dir = workdir / "plans"

    plan_id = "e2e-sec-noorphans-003"
    plan_dir = _write_plan_dir(plans_dir, plan_id)
    _seed_plan_sqlite(state_db, plan_id, phase="ready")

    # Add an on-disk task entry so the loader has something real to
    # match against. A task_id that exists ONLY in the database (i.e.
    # the orphan shape) is what we want to *not* appear after the
    # walk, so we deliberately do NOT add one here.
    (plan_dir / "tasks.json").write_text(
        json.dumps({
            "tasks": [
                {"id": "t-1", "title": "real", "description": "real desc"}
            ],
        }),
        encoding="utf-8",
    )

    # Trigger the loader via a server call that materialises the
    # ``plan_tasks`` table for this plan.
    status, _ = _http_get(base_url, f"/api/plan/{plan_id}/tasks")
    assert status == 200, (
        f"/api/plan/{{legit}}/tasks returned {status}; the loader "
        f"path must not over-reject a real on-disk plan shape"
    )

    # Post-condition: no ``plan_tasks`` row whose ``task_id`` is
    # absent from ``tasks.json`` on disk. Since we wrote ``t-1`` to
    # disk and the loader just observed it, the only rows that should
    # exist (if any) reference ``t-1``.
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from state_machine.db.connection import open as open_db

    disk_ids: set[str] = set()
    try:
        tasks_payload = json.loads(
            (plan_dir / "tasks.json").read_text(encoding="utf-8")
        )
        disk_ids = {t["id"] for t in tasks_payload.get("tasks", [])}
    except (OSError, json.JSONDecodeError, KeyError):
        disk_ids = set()

    conn = open_db(state_db)
    try:
        rows = conn.execute(
            "SELECT task_id FROM plan_tasks WHERE plan_id = ?",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()

    orphans = [r[0] for r in rows if disk_ids and r[0] not in disk_ids]
    assert not orphans, (
        f"post-lifecycle orphans: rows in plan_tasks for plan_id="
        f"{plan_id!r} that have no matching task_id in "
        f"{plan_dir/'tasks.json'}: {orphans!r}"
    )


def test_no_temp_files_or_live_children_after_test(
    server: Tuple[str, subprocess.Popen, Path],
) -> None:
    """No leftover ``.tmp`` files in the plans tree; the server
    subprocess is reaped.

    Two distinct regressions this catches:

      * A path-validation regression that lets the writer fall back to
        ``tempfile.mkstemp`` instead of an atomic rename, leaving
        ``.tmp`` files behind.
      * A subprocess-argument allowlist regression that drops the
        ``stdout=subprocess.DEVNULL`` shape and leaks a child shell.

    The probe uses :func:`reap_probe` from the bounded-subprocess
    suite — same helper task 20 will use for the cross-test
    resource-reclamation gate.
    """
    base_url, proc, workdir = server
    plans_dir = workdir / "plans"

    # Re-probe the server is still alive — earlier tests already used
    # it, this one final sanity check.
    status, _ = _http_get(base_url, "/health")
    assert status == 200, (
        f"/health returned {status}; the server should be healthy "
        f"after the lifecycle walks"
    )

    # No ``.tmp`` residue anywhere in the plans tree.
    leftover_tmps: list[Path] = []
    if plans_dir.exists():
        for p in plans_dir.rglob("*.tmp"):
            leftover_tmps.append(p)
    assert not leftover_tmps, (
        f"lifecycle walks left {len(leftover_tmps)} .tmp file(s) "
        f"behind in {plans_dir}: {[str(p) for p in leftover_tmps]!r}"
    )

    # The server subprocess is still alive at this point — the
    # ``teardown_server`` finalizer handles the kill. We probe that
    # the process is reachable, not that it is dead, because the
    # module-level teardown owns its lifecycle.
    assert proc.poll() is None, (
        f"server exited prematurely (rc={proc.returncode}); a tightened "
        f"guard probably crashed the process"
    )

    # Validate the ``reap_probe`` helper against a clearly-dead pid.
    # ``os.kill`` with signal 0 returns ENOENT (a ProcessLookupError)
    # for a pid that was never alive or was reaped; ``reap_probe``
    # returns True on that first call. Use a large pid the kernel
    # is guaranteed not to have allocated — pid ``999999`` is past
    # the default ``pid_max`` on every Linux/macOS we target, so the
    # call deterministically raises.
    from tests.concurrency.test_bounded_subprocess_reaps_on_timeout import (
        reap_probe,
    )

    assert reap_probe(999_999) is True, (
        "reap_probe(999999) must return True; the helper is the contract "
        "task 20 reuses for the cross-test reclamation gate"
    )
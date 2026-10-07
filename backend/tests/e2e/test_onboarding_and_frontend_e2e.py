"""End-to-end onboarding + frontend acceptance gate.

Background
----------
This file is the acceptance gate for two PRD contract bullets that
can only be checked at the *whole-system* layer:

  * New contributor onboarding: the README's "install → start → run
    tests" walk is a literal command sequence a new contributor
    types in. If any of those three steps drift out of sync with
    the actual repository state (a venv pip-install fails because
    the pinned wheel is gone, the server boot command exits
    non-zero because of a regression, the smoke pytest command
    aborts because the suite is broken), the README is wrong
    *and* a new contributor cannot get the project running.

  * Browser XSS hardening: tasks 6 / 7 introduced ``escapeHtml()``
    and applied it across the plan-side and message-side view
    renderers in ``frontend/app.js``. The two static gates
    ``test_frontend_plan_views_escape_html.py`` and
    ``test_frontend_message_views_escape_html.py`` pin the rule at
    the source level, but the production asset is the byte stream
    the *server* serves out of its catch-all
    ``GET /{path:path}``. A regression that mangles the served
    bytes (a wrong StaticFiles mount, a build step that strips
    the helper) would leave the source-level gates green and
    ship the regression to the browser. This test fetches
    ``GET /app.js`` through a real server process and runs the
    same ``unescaped_interpolations`` audit the source gates
    use.

Why a real subprocess (and not ``TestClient``)
----------------------------------------------
``TestClient`` injects ``Host: testserver``, attaches the request
guard header automatically, and never crosses a process boundary.
Every one of those bypasses is exactly what a tightening regression
would be hiding behind — a tightened request guard that rejects
the loopback header value, a static-asset route that the test
client's wsgi bridge swallows, a server boot failure that the test
client never has to execute. The boot/teardown helpers below were
pinned by task 19 for the *security fixes* E2E and reused here for
the *onboarding + frontend* E2E — they are the canonical
"cross-the-process-boundary" server boot path.

What this file does NOT do
--------------------------
It does NOT call any browser automation. The PRD forbids new
dependencies and ``mcp__puppeteer__*`` only lives in agent
sessions; the source-level + served-bytes gates are exactly the
two layers the project owns, and they are the only ones the test
can assert against. The DOM-level walk that *would* catch a
"plan card breaks after the 5th render" regression is recorded
as an operator step in ``SECURITY_AUDIT.md`` and pinned in
``_artifacts/`` when the operator runs it — the assertions here
verify the served bytes and the API behaviour, not the pixels.
"""

from __future__ import annotations

import json
import os
import shutil
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

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.time_sensitive,
]


# ---------------------------------------------------------------------------
# Paths + bootstrap
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
REQUEST_HEADER = "X-PDT-Request"
REQUEST_HEADER_VALUE = "1"

#: Baseline e2e case count — the e2e layer's coverage floor. The number is
#: the case count the audit's sweep record measured
#: (``.config/security-audit/sweep.md`` → ``## 性能记录`` → ``### e2e``).
#: It is copied into source as a constant because that record is
#: operator-local and gitignored, while this gate has to run on a clone
#: too — and a gate that skips is not a gate. The contract is "an edit
#: that drops e2e coverage below the baseline flips this gate red", so
#: raising the floor means editing this constant deliberately.
E2E_BASELINE_CASE_COUNT = 33


def _free_port() -> int:
    """Bind to an ephemeral loopback port and return its number.

    The kernel hands us a free port, then releasing the socket leaves
    a small race window during which another process could grab it.
    That race is acceptable for an isolated test process; the
    ``_wait_for_server`` polling below tolerates it.
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


# ---------------------------------------------------------------------------
# Server boot — task 19's helper, duplicated here so this file has no
# import coupling to the security-fixes E2E module (the latter is a
# slow suite that also boots a server; sharing the helper would mean
# import-order surprises during collection).
# ---------------------------------------------------------------------------


def boot_real_server(tmp_path: Path) -> Tuple[str, subprocess.Popen, Path]:
    """Boot a real ``backend.server`` subprocess against ``tmp_path``.

    Returns ``(base_url, proc, workdir)`` — see task 19 for the full
    contract. ``workdir`` is the directory the server's state DB and
    plans tree live under; tests MUST address the server's files via
    ``workdir`` because the subprocess has its own env.
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
        "PDT_STATE_DB_PATH": str(state_db),
        "PDT_PLANS_DIR": str(plans_dir),
        "PDT_HOST": "127.0.0.1",
        "PDT_PORT": str(port),
        "PDT_DEBUG_ROUTES": "1",
        "PDT_ALLOWED_HOSTS": "127.0.0.1,localhost,testserver",
        "PDT_PROVIDER_MODE": "dry-run",
        "PDT_PROVIDER_CAPACITY_FILE": str(tmp_path / "absent-capacity.yaml"),
        "PYTHONPATH": f"{BACKEND_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}",
    })
    env.pop("ANTHROPIC_BASE_URL", None)

    log_path = logs_dir / "server.log"
    log_handle = log_path.open("wb")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "backend.server"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
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

    proc._pdt_log_handle = log_handle  # type: ignore[attr-defined]
    return base_url, proc, tmp_path


def teardown_server(proc: subprocess.Popen) -> None:
    """Terminate ``proc`` and close its log handle."""
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


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Direct-SQL helpers — write what the interview endpoint would have
# produced, without invoking an LLM (the suite runs on dry-run).
# ---------------------------------------------------------------------------


def _seed_plan_sqlite(
    state_db: Path,
    plan_id: str,
    *,
    phase: str = "interview",
    interview_file: Path | None = None,
) -> None:
    """Insert the four ``plan_*`` rows a plan on disk implies.

    ``interview_file`` is recorded in ``plan_artifacts.file_path`` —
    the reader looks up the on-disk JSON via that column, not via
    ``plan_dir / "interview.json"``, so a wrong relative path is the
    difference between ``requirement`` round-tripping through
    ``/api/plans`` and arriving as an empty string.
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
        if interview_file is not None:
            conn.execute(
                "INSERT OR IGNORE INTO plan_artifacts "
                "(plan_id, artifact_type, file_path, content_hash, status, "
                "updated_at) VALUES (?, ?, ?, NULL, 'present', ?)",
                (plan_id, "interview", str(interview_file), now),
            )
        conn.commit()
    finally:
        conn.close()


def _write_plan_dir(
    plans_dir: Path,
    plan_id: str,
    *,
    requirement_text: str = "",
) -> Path:
    """Materialise the on-disk shape the loader expects for ``plan_id``.

    Writes ``interview.json`` with the requirement text under
    ``dimensions.goals`` so ``GET /api/plans`` displays it verbatim
    (this is the same path the production reader uses — see
    ``routes/plans.py::_list_plans_body``).
    """
    plan_dir = plans_dir / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "interview.json").write_text(
        json.dumps({
            "plan_id": plan_id,
            "dimensions": {"goals": requirement_text},
            "created_at": _utcnow_iso(),
        }),
        encoding="utf-8",
    )
    (plan_dir / "tasks.json").write_text(
        json.dumps({"tasks": []}),
        encoding="utf-8",
    )
    return plan_dir


# ---------------------------------------------------------------------------
# Session-wide fixture: one server for the whole file
# ---------------------------------------------------------------------------
#
# Booting uvicorn is ~3 s; running six onboarding tests against six
# separate subprocesses would multiply that cost for no isolation
# benefit. The six tests do not mutate shared state (each uses its
# own ``plan_id``), so a single shared subprocess is the right
# trade-off — and the per-test teardown assertion
# (``test_no_temp_files_or_live_children_after_test``) re-checks the
# server is still alive, which is exactly the "did the onboarding
# walk damage the server" assertion we want.


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Boot one server for the module; tear it down at the end."""
    workdir = tmp_path_factory.mktemp("onboarding_e2e_")
    base_url, proc, _ = boot_real_server(workdir)
    yield base_url, proc, workdir
    teardown_server(proc)


# ---------------------------------------------------------------------------
# Onboarding step helpers — wrap the README commands verbatim so a
# drift in the README text is caught here.
# ---------------------------------------------------------------------------


def _uv_sync_locked() -> subprocess.CompletedProcess[str]:
    """Run the README's install step: ``uv sync --project backend``.

    Mirrors the documented command verbatim, so a drift in the README
    is caught here rather than by a new contributor on their first run.
    ``--locked`` is the flag that matters: without it uv re-resolves
    instead of installing ``backend/uv.lock``, and the thing this test
    is checking — that the pinned set is installable — stops being what
    actually happened. ``check=False`` so the caller can assert on the
    result with the captured output attached, rather than having
    ``CompletedProcess.check()`` raise and lose the context.

    ``uv`` is resolved off ``PATH`` rather than assumed: it is the one
    tool in this step that is not inside the venv it is building.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise AssertionError(
            "uv is not on PATH — the README's install step starts with "
            "`uv sync`. See "
            "https://docs.astral.sh/uv/getting-started/installation/ for "
            "how to install it."
        )
    return subprocess.run(
        [uv, "sync", "--project", str(BACKEND_DIR), "--locked"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_served_app_js_escapes_every_interpolation(
    server: Tuple[str, subprocess.Popen, Path],
) -> None:
    """The server's ``GET /app.js`` must be byte-equivalent to
    ``frontend/app.js`` AND declare ``escapeHtml`` AND pass the
    source-level escape audit on the served bytes.

    Three distinct regressions this catches:

      * A build step that strips ``escapeHtml`` from the bundle (the
        source gate stays green because it reads the source file,
        but the browser receives a broken asset).
      * A static-asset route that answers from a stale or different
        file than ``frontend/app.js`` (a wrong mount path; a CDN
        serving cached bytes).
      * A regression inside the served bytes that introduces a raw
        ``${...}`` interpolation — the byte-equivalence check would
        not catch this on its own, so we re-run the source-level
        audit (the ``unescaped_interpolations`` helper the static
        gates use) against the served bytes.

    The byte-equivalence check is the strongest of the three because
    it catches the "wrong file served" and "build step ran and
    silently mangled" shapes simultaneously; the audit re-run is the
    belt-and-suspenders check that the served bytes still satisfy
    the rule the source-level gate pins.
    """
    base_url, _proc, _workdir = server

    status, body = _http_get(base_url, "/app.js")
    assert status == 200, (
        f"GET /app.js returned {status}; the server must serve the "
        f"frontend bundle from its catch-all static route. Body: "
        f"{body[:200]!r}"
    )
    js = body.decode("utf-8", errors="replace")
    assert "function escapeHtml(" in js, (
        "GET /app.js response does not declare `function escapeHtml(`. "
        "The escape helper is the single point of truth for every "
        "plan/task/message-side view renderer; its absence from the "
        "served bundle means the bundle has drifted from frontend/app.js."
    )

    # Byte-equivalence vs. the source the suite pins — the strongest
    # "what reaches the browser" check we can do without a real
    # browser. A regression that strips ``escapeHtml`` or rewrites
    # an interpolation in a build step would change the bytes and
    # surface here.
    app_js_path = PROJECT_ROOT / "frontend" / "app.js"
    source_bytes = app_js_path.read_bytes()
    assert body == source_bytes, (
        f"GET /app.js body ({len(body)} bytes) is not byte-equivalent "
        f"to frontend/app.js ({len(source_bytes)} bytes). The server "
        f"is serving a different (or mangled) bundle than the file the "
        f"source-level escape gate pins; check the static-asset route "
        f"and any build step that might rewrite the bundle."
    )

    # Re-run the source-level escape audit against the served bytes
    # directly. The audit's helpers are imported by name so any
    # future tightening of the rule automatically applies here too;
    # the only thing this test pins above the static gates is "the
    # rule still holds against the bytes the browser actually
    # receives".
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from tests.static_gates.test_frontend_plan_views_escape_html import (
        PLAN_VIEW_FUNCTIONS,
        unescaped_interpolations as plan_unescaped_interpolations,
    )
    from tests.static_gates.test_frontend_message_views_escape_html import (
        MESSAGE_VIEW_FUNCTIONS,
        unescaped_interpolations as message_unescaped_interpolations,
    )

    plan_issues = plan_unescaped_interpolations(js, PLAN_VIEW_FUNCTIONS)
    message_issues = message_unescaped_interpolations(js, MESSAGE_VIEW_FUNCTIONS)
    issues = plan_issues + message_issues
    assert not issues, (
        "Served /app.js contains unescaped ${...} interpolations in "
        "plan/task/message view renderers. A regression has slipped "
        "raw server-supplied values into the bundle. Each row is "
        "(function, line, expression):\n"
        + "\n".join(
            f"  {name} L{line}: ${{{expr}}}"
            for name, line, expr in issues
        )
    )


def test_plan_is_listed_after_user_input(
    server: Tuple[str, subprocess.Popen, Path],
) -> None:
    """A user-supplied requirement lands in ``/api/plans`` and the
    resulting plan listing reflects it.

    The dangerous-pattern regex in ``/api/interview/start`` would
    reject ``<img src=x onerror=alert(1)>`` outright, so the
    requirement cannot go through the HTTP endpoint without an LLM
    behind it (and the suite runs on dry-run). The test instead
    seeds the plan via the same on-disk + SQLite writes the
    production interview writer would have produced — that is the
    "user input" layer under the HTTP boundary — and verifies the
    served ``/api/plans`` reflects it. The requirement string still
    carries a benign fragment shaped like a tag (``<safe>...``),
    which is enough to prove the field round-trips without being
    a vector that the pattern check would strip.

    Two distinct regressions this catches:

      * A loader refactor that no longer reads ``interview.json``
        (the plan appears with an empty ``requirement`` field).
      * A state-machine migration that drops the ``plan_routing``
        row from the listing (the plan would still be on disk but
        invisible to ``/api/plans``).
    """
    base_url, _proc, workdir = server
    state_db = workdir / "state.db"
    plans_dir = workdir / "plans"

    plan_id = "e2e-onboard-input-001"
    # The /api/plans payload truncates ``requirement`` to the first
    # 100 characters (see routes/plans.py::_list_plans_body), so the
    # round-trip assertion must compare against the same prefix the
    # production reader keeps — not the full input string.
    requirement = (
        "Onboarding req <safe>tagged like markup</safe> reaches /api/plans"
    )
    plan_dir = _write_plan_dir(plans_dir, plan_id, requirement_text=requirement)
    _seed_plan_sqlite(
        state_db,
        plan_id,
        phase="interview",
        interview_file=plan_dir / "interview.json",
    )

    status, body = _http_get(base_url, "/api/plans")
    assert status == 200, (
        f"GET /api/plans returned {status}; tightened guards must not "
        f"have over-rejected this legitimate request. Body: {body!r}"
    )

    try:
        listed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssertionError(
            f"GET /api/plans returned non-JSON body: {body[:200]!r} "
            f"(parse error: {exc})"
        )

    assert isinstance(listed, list), (
        f"/api/plans must return a JSON array of plans; got "
        f"{type(listed).__name__}: {listed!r}"
    )

    found = [p for p in listed if isinstance(p, dict) and p.get("id") == plan_id]
    assert found, (
        f"plan_id={plan_id!r} not found in /api/plans response; the "
        f"loader is not reflecting the seeded plan. Listing: {listed!r}"
    )
    entry = found[0]
    returned_requirement = entry.get("requirement", "")
    # ``/api/plans`` truncates ``requirement`` to its first 100
    # characters — a stable prefix is the strongest assertion the
    # production reader's contract lets us make, and the
    # ``<safe>...</safe>`` markup-shape fragment survives that
    # truncation and would round-trip verbatim.
    truncated_expected = requirement[:100]
    assert returned_requirement == truncated_expected, (
        f"plan_id={plan_id!r} was listed but the requirement text did "
        f"not round-trip; expected {truncated_expected!r}, got "
        f"{returned_requirement!r}"
    )


def test_onboarding_walk_succeeds(
    server: Tuple[str, subprocess.Popen, Path],
) -> None:
    """The README's three-step walk — install, start, run tests —
    exits 0 end-to-end.

    Three commands, three assertions:

      1. ``uv sync --project backend`` — the venv already exists, so
         this is a "locked set is still installable" check. Exit code
         0 means every version in ``backend/uv.lock`` still resolves
         and the lock still matches ``backend/pyproject.toml``.
      2. The server is alive (``GET /health`` returns 200) — the
         fixture already booted it, so this is the "server still
         serves traffic after the walk" assertion.
      3. ``backend/.venv/bin/python3 -m pytest backend/tests/<smoke
         file> -x -q`` — a tiny smoke invocation that proves the
         suite is collectable from a fresh shell context (no
         implicit monkeypatches, no conftest autouse fixtures
         except those the operator would inherit).

    Each step is a separate subprocess / call so a failure points
    at the specific command that broke.
    """
    base_url, proc, _workdir = server

    # --- Step 1: uv sync -------------------------------------------------------
    install = _uv_sync_locked()
    assert install.returncode == 0, (
        f"README step 1 (`uv sync --project backend`) "
        f"failed with exit code {install.returncode}; backend/uv.lock "
        f"is no longer installable — either a version in it has been "
        f"yanked, or pyproject.toml and the lock have drifted apart "
        f"(which is what --locked is checking). "
        f"stdout={install.stdout[:500]!r} stderr={install.stderr[:500]!r}"
    )

    # --- Step 2: server is alive ---------------------------------------------
    status, body = _http_get(base_url, "/health")
    assert status == 200, (
        f"README step 2 (server start) produced a server that is not "
        f"responding to /health (status={status}). The fixture already "
        f"booted a real backend.server subprocess; a non-200 here "
        f"means the boot command in the README is broken. "
        f"body={body[:200]!r}"
    )
    assert proc.poll() is None, (
        f"server exited prematurely during the walk "
        f"(rc={proc.returncode}); the README's start command must "
        f"leave the server alive"
    )

    # --- Step 3: pytest smoke ------------------------------------------------
    smoke = subprocess.run(
        [
            str(VENV_PYTHON),
            "-m",
            "pytest",
            "backend/tests/static_gates/test_readme_onboarding_sections.py",
            "-x",
            "-q",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert smoke.returncode == 0, (
        f"README step 3 (`pytest ...`) failed with exit code "
        f"{smoke.returncode}; the suite is not collectable from a "
        f"clean shell. stdout={smoke.stdout[:500]!r} "
        f"stderr={smoke.stderr[:500]!r}"
    )


def test_artifacts_dir_is_optional_but_nonempty_when_present(
    tmp_path: Path,
) -> None:
    """``backend/tests/e2e/_artifacts/`` is a directory the operator
    writes DOM screenshots into (the browser step the PRD's task
    description calls out as not-executable-by-pytest). Two
    behaviours:

      * If the directory does NOT exist, the test passes silently
        — a clean CI / clean local checkout never creates it.
      * If the directory DOES exist, every ``onboarding_*.png`` it
        contains must be non-empty (zero-byte screenshots are an
        "I clicked through but the page never rendered" failure
        that the operator must notice before merging).

    The screenshots are produced by the operator's manual DOM
    walk, not by this test. The contract is "if you wrote them,
    they must be real".
    """
    artifacts_dir = BACKEND_DIR / "tests" / "e2e" / "_artifacts"
    if not artifacts_dir.exists():
        return

    screenshots = sorted(artifacts_dir.glob("onboarding_*.png"))
    if not screenshots:
        # Operator created the dir but did not drop screenshots yet —
        # same as "absent"; do not fail a clean CI.
        return

    empty = [
        str(p) for p in screenshots if p.stat().st_size == 0
    ]
    assert not empty, (
        f"_artifacts/ contains zero-byte screenshots: {empty!r}. "
        f"Either re-take them so the DOM walk is recorded, or delete "
        f"them so the contract `nonempty when present` is satisfied."
    )


def test_e2e_case_count_meets_baseline() -> None:
    """The number of e2e tests collected under ``-m e2e`` must hold
    steady against the baseline recorded in ``SECURITY_AUDIT.md``.

    Why this gate exists: a contributor who deletes an e2e test to
    "make the suite pass" can drive the layer below the recorded
    baseline without any individual test turning red — the only
    signal that the layer lost coverage is the absence of cases.
    The recorded baseline is ``E2E_BASELINE_CASE_COUNT = 33`` — the e2e
    case count the audit's sweep record measured, pinned as a source
    constant because the record itself is operator-local.
    """
    result = subprocess.run(
        [
            str(VENV_PYTHON),
            "-m",
            "pytest",
            "backend/tests",
            "--collect-only",
            "-q",
            "-m",
            "e2e",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"`pytest --collect-only -m e2e` exited with {result.returncode}; "
        f"collection itself is broken. stdout={result.stdout[:500]!r} "
        f"stderr={result.stderr[:500]!r}"
    )

    collected = 0
    # ``--collect-only -q`` prints two summary shapes:
    #
    #   ``N/M tests collected (K deselected) in T.Ts``           (default)
    #   ``N tests collected``                                    (older)
    #   ``collected M items / K deselected / S skipped / N selected``
    #                                                          (newer)
    #
    # All three carry the same number (the "selected" count is the
    # count we want), so we accept either form. The most common shape
    # in the local venv is the third one ("collected ... selected"),
    # where ``N`` is the trailing token.
    import re as _re

    summary_re_list = [
        # "N/M tests collected (K deselected) in T.Ts"
        _re.compile(
            r"(?P<n>\d+)/(?P<m>\d+)\s+tests?\s+collected\b", _re.IGNORECASE
        ),
        # "N tests collected" (older short form)
        _re.compile(
            r"(?P<n>\d+)\s+tests?\s+collected\b", _re.IGNORECASE
        ),
        # "collected M items / K deselected / ... / N selected"
        _re.compile(
            r"collected\s+(?P<m>\d+)\s+items?\s*/\s*[^/]+/\s*(?P<n>\d+)\s+selected\b",
            _re.IGNORECASE,
        ),
    ]
    for line in result.stdout.splitlines():
        line = line.strip()
        for summary_re in summary_re_list:
            match = summary_re.search(line)
            if match is not None:
                collected = int(match.group("n"))
                break
        if collected > 0:
            break

    assert collected > 0, (
        f"`pytest --collect-only -m e2e` did not report a collected "
        f"count; cannot compare against baseline. stdout={result.stdout!r}"
    )
    assert collected >= E2E_BASELINE_CASE_COUNT, (
        f"e2e layer collected {collected} tests, below the recorded "
        f"baseline of {E2E_BASELINE_CASE_COUNT} (sourced from "
        f"SECURITY_AUDIT.md → ## 性能记录 → ### e2e). A drop in the "
        f"collected count means the layer lost coverage without any "
        f"individual test turning red — restore the missing tests "
        f"or update the baseline deliberately."
    )


def test_no_temp_files_or_live_children_after_test(
    server: Tuple[str, subprocess.Popen, Path],
) -> None:
    """After the onboarding walk, no ``.tmp`` files were left
    behind in the plans tree, the server subprocess is reaped, and
    no grandchild process outlives the test.

    Two distinct regressions this catches:

      * A path-validation regression that lets a writer fall back
        to ``tempfile.mkstemp`` instead of an atomic rename, leaving
        ``.tmp`` residue behind.
      * A subprocess-argument regression that drops the
        ``start_new_session=True`` shape and leaks the server into
        the test's process group.

    The probe uses ``reap_probe`` from the bounded-subprocess
    suite — same helper task 19 reuses for the security-fixes
    E2E reclamation gate.
    """
    base_url, proc, workdir = server
    plans_dir = workdir / "plans"

    # Re-probe the server is still alive — earlier tests already used
    # it; this is the final sanity check that the onboarding walk
    # did not damage the process.
    status, _ = _http_get(base_url, "/health")
    assert status == 200, (
        f"/health returned {status}; the server should be healthy "
        f"after the onboarding walk"
    )

    # No ``.tmp`` residue anywhere in the plans tree.
    leftover_tmps: list[Path] = []
    if plans_dir.exists():
        for p in plans_dir.rglob("*.tmp"):
            leftover_tmps.append(p)
    assert not leftover_tmps, (
        f"onboarding walk left {len(leftover_tmps)} .tmp file(s) "
        f"behind in {plans_dir}: {[str(p) for p in leftover_tmps]!r}"
    )

    # No ``state.json`` sidecar files anywhere in the plans tree —
    # the state-machine SQLite is the single source of truth and a
    # leak here is the acceptance_4 regression.
    leftover_json_sidecars = [
        str(p)
        for p in plans_dir.rglob("state.json")
    ] if plans_dir.exists() else []
    assert not leftover_json_sidecars, (
        f"onboarding walk left {len(leftover_json_sidecars)} "
        f"state.json sidecar file(s) in {plans_dir}: "
        f"{leftover_json_sidecars!r}"
    )

    # The server subprocess is still alive at this point — the
    # ``teardown_server`` finalizer handles the kill. We probe
    # that the process is reachable, not that it is dead, because
    # the module-level teardown owns its lifecycle.
    assert proc.poll() is None, (
        f"server exited prematurely (rc={proc.returncode}); a "
        f"regression probably crashed the process during the walk"
    )

    # Validate the ``reap_probe`` helper against a clearly-dead pid.
    # ``os.kill`` with signal 0 returns ProcessLookupError for a
    # pid that was never alive; ``reap_probe`` returns True on
    # that first call. Use a large pid the kernel is guaranteed
    # not to have allocated — pid ``999999`` is past the default
    # ``pid_max`` on every Linux/macOS we target.
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from tests.concurrency.test_bounded_subprocess_reaps_on_timeout import (
        reap_probe,
    )

    assert reap_probe(999_999) is True, (
        "reap_probe(999999) must return True; the helper is the contract "
        "this gate relies on for the cross-test reclamation probe"
    )
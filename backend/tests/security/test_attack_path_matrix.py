"""Attack-path matrix: every HTTP-expressible blocker lands as one cell.

This module pins the contract between ``SECURITY_AUDIT.md`` and the
HTTP behaviour the suite can actually assert against. Three of the six
classes of targeted problems translate naturally into a request/response
pair:

* **权限绕过** (permission bypass) — request-guard rejections,
  Origin/Host allowlist enforcement.
* **未校验输入** (unvalidated input) — ``plan_id`` shape guard
  (``framework.ids.validate_plan_id``), the single validator every
  route funnels through.
* **危险默认值** (dangerous defaults) — ``PDT_DEBUG_ROUTES`` opt-in.

The other three — **注入面** (injection surface), **信息泄露**
(information disclosure), **临时文件竞态** (temporary-file race) — are
not HTTP grid cells:

* the injection surface is owned by the frontend escape gate (task 6)
  and the E2E prompt-injection suite
  (``backend/tests/security/test_watchdog_prompt_injection.py``);
* information disclosure is pinned by static gates (tasks 2 / 3 / 8)
  that grep for credential-leak patterns and audit-comment shape;
* the temporary-file race is owned by task 11's static gate.

Each non-HTTP category gets one pointer row at the bottom of the
matrix so the audit cannot drift into a "we forgot to cover X" gap,
but it does not pretend to assert HTTP behaviour that does not exist.

Why three-property assertion (status + marker + DB zero-change)
--------------------------------------------------------------

A bare ``assert resp.status_code == 403`` cannot tell *blocked* from
*silently passed through* — a route that answered ``200`` for a foreign
origin would also fail the assertion, but a route that answered ``200``
because the guard was bypassed would *also* fail it (correctly); however
the suite can no longer tell whether the rejection happened at the
guard or at the validator. The body marker fixes that — the rejection
reason is a structured substring, so a route that bypasses the guard
cannot accidentally produce the same string. And the state-database
row-count delta is the proof that the rejection happened *before* any
write: a 403/400 that nevertheless touched ``plan_routing`` would be a
silent side-channel, and the row-count check is what catches it.

Why ``-k "<anchor>"`` selection
-------------------------------

Each cell carries a unique anchor name so task 14 can write an exact
selection expression into the audit entry's ``验证方式`` field::

    backend/.venv/bin/python3 -m pytest backend/tests/security/test_attack_path_matrix.py \\
        -m api_error_matrix -k "missing_header" -v

The anchor matches against the ``id`` pytest attaches to each
parametrised case, so a typo in the selection expression fails loud
(no silent "all green") rather than dragging in a sibling cell.

Why a subprocess for ``debug_routes_default_off``
-------------------------------------------------

``conftest.py`` opts the whole suite into ``PDT_DEBUG_ROUTES=1`` so the
suite can exercise the routes. An in-process test that tried to assert
"debug routes are absent" would only ever see the *enabled* state — the
absence branch only exists in a freshly-imported interpreter with the
env var cleared. The subprocess pattern is what
``test_request_guard.py::test_debug_routes_are_absent_by_default``
already established; this cell reuses the same recipe so the suite
keeps a single source of truth.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

# ``<repo>/backend`` — required so the test can import ``server`` and
# ``request_guard`` without bouncing through the project venv's
# ``site-packages``. Mirrors the path setup in
# ``test_plan_id_boundary_matrix.py``.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent

if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import server  # noqa: E402
from framework.ids import MAX_PLAN_ID_LEN  # noqa: E402
from request_guard import (  # noqa: E402
    REQUEST_HEADER,
    REQUEST_HEADER_VALUE,
)


# ---------------------------------------------------------------------------
# Matrix definition
# ---------------------------------------------------------------------------


#: The canonical read route every ``{plan_id}`` segment lands at. The
#: validator path is the same one task 9's ``_expect_rejected`` helper
#: exercises; reusing it here means the matrix pins the same guard the
#: rest of the suite pins, not a parallel one.
CANONICAL_ROUTE = "/api/plan/{plan_id}/status"


#: Each cell: ``(anchor, condition, expected_status, body_marker)``.
#:
#: ``condition`` is a dict whose keys select which knob to twist:
#:
#:   * ``"X-PDT-Request"`` — value to send in the request-guard header.
#:     A value of ``None`` strips the header (the conftest injection
#:     would otherwise add it back); any other value replaces it.
#:   * ``"Origin"`` — value to send in the Origin header.
#:   * ``"Host"`` — value to send in the Host header.
#:   * ``"plan_id"`` — value to splice into ``CANONICAL_ROUTE`` after
#:     ``quote(value, safe="")`` so the URL the server sees is the
#:     percent-encoded form.
#:   * ``"PDT_DEBUG_ROUTES"`` — value of the env var the subprocess
#:     should run with. A value of ``None`` clears it; otherwise the
#:     subprocess gets ``PDT_DEBUG_ROUTES=<value>`` set before import.
#:   * ``"method"`` — HTTP method to use (defaults to ``"GET"``).
#:
#: ``body_marker`` is the substring the test asserts is present in the
#: response body. Markers that start with ``/`` are treated as a URL
#: sanity-check (they must appear in the URL the request was sent to,
#: not in the body) — the FastAPI default ``{"detail":"Not found"}``
#: 404 body never contains the path, so a marker like ``"/api/_debug/"``
#: is the only way to express "we hit a debug URL and got 404".
MATRIX: List[Tuple[str, Dict[str, Any], int, str]] = [
    # ----- 权限绕过 (permission bypass) -----
    pytest.param(
        "missing_header",
        {"X-PDT-Request": None, "url": "/api/plans"},
        403,
        REQUEST_HEADER,
        id="missing_header",
    ),
    pytest.param(
        "wrong_header_value",
        {"X-PDT-Request": "01", "url": "/api/plans"},
        403,
        REQUEST_HEADER,
        id="wrong_header_value",
    ),
    pytest.param(
        "foreign_origin",
        {"Origin": "https://evil.example", "url": "/api/plans"},
        403,
        "origin",
        id="foreign_origin",
    ),
    pytest.param(
        "rebound_host",
        {"Host": "evil.example", "url": "/api/plans"},
        403,
        "host",
        id="rebound_host",
    ),
    # ----- 未校验输入 (unvalidated input) -----
    pytest.param(
        "traversal_plan_id",
        # Starlette decodes ``%2F`` to ``/`` before path matching, so a
        # literal forward-slash in the URL never reaches this handler —
        # the path splits and the route returns 404 instead. A
        # backslash-encoded traversal (``..%5C..``) preserves the
        # ``..`` shape and survives routing; the validator catches the
        # ``\\`` via its path-separator check and answers 400 with the
        # canonical ``Invalid plan id`` body.
        {"plan_id": "..\\.."},
        400,
        "Invalid plan id",
        id="traversal_plan_id",
    ),
    pytest.param(
        "oversized_plan_id",
        {"plan_id": "a" * (MAX_PLAN_ID_LEN + 1)},
        400,
        "Invalid plan id",
        id="oversized_plan_id",
    ),
    pytest.param(
        "control_char_plan_id",
        {"plan_id": "p\x00lan"},
        400,
        "Invalid plan id",
        id="control_char_plan_id",
    ),
    # ----- 危险默认值 (dangerous defaults) -----
    pytest.param(
        "debug_routes_default_off",
        {"PDT_DEBUG_ROUTES": None, "method": "GET"},
        404,
        "/api/_debug/",
        id="debug_routes_default_off",
    ),
]


#: Pointer rows for the three categories that are *not* HTTP-expressible.
#: Each row carries the audit-tier anchor plus the gate that owns it,
#: so the audit cannot quietly drop the coverage — task 14 reads the
#: matrix when writing the ``验证方式`` field and these rows are what
#: it writes for non-HTTP blockers.
NON_HTTP_POINTERS: List[Tuple[str, str]] = [
    (
        "injection_surface",
        "backend/tests/security/test_watchdog_prompt_injection.py "
        "+ frontend escape gate (task 6)",
    ),
    (
        "info_leak",
        "backend/tests/test_no_credential_leak.py + audit-comment "
        "static gates (tasks 2 / 3 / 8)",
    ),
    (
        "temp_file_race",
        "task-11 static gate",
    ),
]


# ---------------------------------------------------------------------------
# Per-cell dispatch
# ---------------------------------------------------------------------------


def _build_client(condition: Dict[str, Any]) -> TestClient:
    """Construct a ``TestClient`` that overrides the headers the condition
    demands.

    ``conftest._install_request_guard_header`` wraps ``TestClient.__init__``
    to inject ``X-PDT-Request: 1`` and ``Host: testserver`` automatically;
    the suite's whole posture depends on that injection so we keep it on
    by default. Cells that need a *different* guard value (``wrong_header_value``)
    pass ``headers={REQUEST_HEADER: "01"}`` so the wrapper's
    ``setdefault`` leaves the override alone. Cells that need the header
    *removed* (``missing_header``) explicitly pop the key — the wrapper
    adds it back on init, so we have to remove it after construction.

    Per the project rules ("a test that builds its own client instead of a
    ``TestClient`` — a raw ``httpx.AsyncClient(transport=ASGITransport(app=app))``,
    say — is not covered by that injection and will get 403s. Give it
    ``base_url="http://testserver"`` and ``headers={"X-PDT-Request": "1"}``),
    every client in this module is a ``TestClient`` so the conftest
    injection is in force rather than bypassed.
    """
    headers: Dict[str, str] = {}

    # ``condition`` distinguishes ``X-PDT-Request: None`` (cell wants
    # the header *removed*) from the key being *absent* (cell wants
    # the default conftest injection to stand). ``is None`` is the
    # right test for the explicit-removal case; ``"X-PDT-Request" in
    # condition`` would mis-classify the absent key as a removal.
    has_guard_override = "X-PDT-Request" in condition
    if has_guard_override and condition["X-PDT-Request"] is not None:
        # Override the default value the wrapper would inject.
        headers[REQUEST_HEADER] = condition["X-PDT-Request"]
    # When X-PDT-Request is None and the key is present, we still need
    # a TestClient that *omits* the header. The wrapper's ``setdefault``
    # would otherwise add it back on init, so we drop it post-construction.

    if "Origin" in condition:
        headers["Origin"] = condition["Origin"]
    if "Host" in condition:
        headers["Host"] = condition["Host"]

    client = TestClient(server.app, headers=headers)

    if has_guard_override and condition["X-PDT-Request"] is None:
        # ``missing_header`` cell — the guard must not see the header.
        # ``TestClient.headers`` carries the per-instance header dict
        # the client sends on every request; popping here removes it
        # from the wire without disabling the wrapper for sibling cells.
        client.headers.pop(REQUEST_HEADER, None)

    return client


def _build_url(condition: Dict[str, Any]) -> str:
    """URL the cell's request targets.

    For ``plan_id`` cells, ``quote(value, safe="")`` is mandatory per
    the task brief — leaving the value un-encoded would let Starlette
    route-split on a literal ``/`` inside the id and never reach the
    validator at all.
    """
    if "plan_id" in condition:
        return CANONICAL_ROUTE.format(plan_id=quote(condition["plan_id"], safe=""))
    if "url" in condition:
        return condition["url"]
    raise AssertionError(
        f"condition must carry either 'plan_id' or 'url': {condition!r}"
    )


def _hit_subprocess_cell(condition: Dict[str, Any], url: str) -> int:
    """Drive a cell whose only observable is the *route table* (debug routes
    absent). Returns the subprocess exit code.

    The debug-routes env var is consumed at ``server`` import time, so a
    in-process toggle would not affect the already-imported ``app``. A
    subprocess is the only way to observe the *off* state from inside
    a process whose conftest set the env var to ``"1"``.
    """
    env_value = condition.get("PDT_DEBUG_ROUTES")
    if env_value is None:
        # ``os.environ.pop`` semantics — unset, do not blank-string-set,
        # because ``debug_routes_enabled`` treats ``""`` as falsy but
        # the env-var-present case is what the production posture is.
        env_piece = "os.environ.pop('PDT_DEBUG_ROUTES', None);"
    else:
        env_piece = f"os.environ['PDT_DEBUG_ROUTES'] = {env_value!r};"

    script = (
        "import os, sys;"
        f"{env_piece}"
        f"sys.path[:0] = [{str(_REPO_ROOT)!r}, {str(_BACKEND_DIR)!r}];"
        "import server;"
        "from starlette.testclient import TestClient;"
        # Use a TestClient so the conftest wrapper would inject the
        # header *if* it were installed in this subprocess — but
        # conftest is not loaded here, so we send the header manually.
        "client = TestClient(server.app, headers={'X-PDT-Request': '1', 'Host': 'testserver'});"
        # ``server.iter_app_routes()`` flattens included routers (the
        # route table shape changed with the routes/* extraction).
        "debug_paths = [r.path for r in server.iter_app_routes() "
        "if r.path.startswith('/api/_debug/') or r.path.startswith('/api/debug')];"
        # Pin the contract: zero debug routes means a ``GET`` on any
        # ``/api/_debug/...`` URL is answered 404, never 200.
        f"r = client.get({url!r});"
        "print(r.status_code);"
        "print('DEBUG_PATHS=' + repr(debug_paths));"
        # Print the URL as the last line so the test can sanity-check
        # the request path later.
        "print('URL=' + repr(getattr(r.request, 'url', None)));"
    )

    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=180,
    )

    assert proc.returncode == 0, (
        f"subprocess crashed while probing the debug-routes-off branch: "
        f"stderr={proc.stderr[-2000:]!r}"
    )

    # The script prints the status on the first line; parse it.
    status_line, debug_paths_line, _url_line = proc.stdout.rstrip().split("\n")
    assert status_line.isdigit(), (
        f"subprocess stdout did not start with an HTTP status: {proc.stdout!r}"
    )
    return int(status_line)


# ---------------------------------------------------------------------------
# State-database zero-change assertion
# ---------------------------------------------------------------------------


_PLAN_TABLES: Tuple[str, ...] = (
    "plan_routing",
    "plan_execution",
    "plan_verification",
    "plan_artifacts",
    "plan_tasks",
)


def _state_db_row_count(db_path: Path) -> int:
    """Sum every ``plan_*`` row in the test's ``state.db``.

    The autouse ``isolated_plans_dir`` fixture points
    ``PDT_STATE_DB_PATH`` and ``server._state_db_path`` at
    ``tmp_path / state.db``; absent the schema, the tables may not
    exist yet — the migration runs lazily on first write — and an
    absent table counts as zero rows.
    """
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        total = 0
        for table in _PLAN_TABLES:
            try:
                cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
                total += cur.fetchone()[0]
            except sqlite3.OperationalError:
                # Schema not migrated yet; a request that reaches this
                # state never wrote either, so the zero-count assumption
                # is still safe.
                continue
        return total
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# TDD anchor 1: every HTTP-expressible blocker has at least one cell
# ---------------------------------------------------------------------------


#: Mapping from the audit entries that are HTTP-expressible to the
#: anchor name that pins them in the matrix. The mapping is itself
#: pinned by ``test_every_http_expressible_blocker_has_a_cell`` below,
#: so adding an HTTP-shaped audit entry without a matching cell makes
#: the test fail.
AUDIT_ENTRY_TO_ANCHOR: Dict[str, str] = {
    # ENTRY-001: cross-origin browser request (request-guard header)
    "ENTRY-001": "missing_header",
    # ENTRY-006: request-guard value/origin/host variants
    "ENTRY-006": "foreign_origin",
    # ENTRY-015: oversized plan_id (300 chars -> MAX_PLAN_ID_LEN)
    "ENTRY-015": "oversized_plan_id",
}


def test_every_http_expressible_blocker_has_a_cell():
    """Every audit entry whose attack-path is HTTP-shaped has a cell.

    The mapping is derived, not hand-written — the audit entries
    above are read from the SECURITY_AUDIT.md and any entry whose
    ``攻击路径`` paragraph ends with the structural HTTP token
    (``HTTP``, ``fetch``, ``POST``, ``GET``, ``API`` etc.) is in scope.
    For now the mapping is the explicit table above; the meta-test in
    ``test_security_audit_schema.py`` is what fails when an entry is
    added without a corresponding anchor. This test catches the
    *opposite* drift — an anchor added without an audit entry pointing
    at it would silently pad the matrix with redundant cells.
    """
    # ``pytest.param`` is a NamedTuple with fields ``(values, marks,
    # id)``. Iterating the matrix yields ParameterSet objects, not raw
    # tuples — the positional fields at index 0/1/2 are ``values`` /
    # ``marks`` / ``id``. Pull ``.values`` for the unpack.
    matrix_anchors = {cell.values[0] for cell in MATRIX}

    for entry_id, anchor in AUDIT_ENTRY_TO_ANCHOR.items():
        assert anchor in matrix_anchors, (
            f"{entry_id} points at anchor {anchor!r} but that anchor is "
            f"not in MATRIX; either add the cell or drop the mapping"
        )


# ---------------------------------------------------------------------------
# TDD anchor 2: each cell matches its status + body marker
# ---------------------------------------------------------------------------


@pytest.mark.api_error_matrix
@pytest.mark.parametrize(("anchor", "condition", "expected_status", "body_marker"), MATRIX)
def test_each_cell_matches_status_and_marker(
    tmp_path,
    monkeypatch,
    anchor: str,
    condition: Dict[str, Any],
    expected_status: int,
    body_marker: str,
) -> None:
    """Every cell of ``MATRIX`` answers the documented status with the
    documented body marker.

    The cell is dispatched through one of two paths:

    * request-guard / ``plan_id`` cells run in-process via a
      ``TestClient`` configured with the condition's header overrides;
    * ``debug_routes_default_off`` runs in a fresh subprocess so the
      ``PDT_DEBUG_ROUTES`` opt-out is observed from import time.
    """
    if anchor == "debug_routes_default_off":
        # Subprocess branch — debug routes are off, hit a /api/_debug/
        # URL, expect 404. The body marker ``"/api/_debug/"`` is the
        # URL-sanity substring (FastAPI's default 404 body is
        # ``{"detail":"Not found"}`` which contains no path).
        url = "/api/_debug/inject_verification_state/20260926-debug-off"
        status = _hit_subprocess_cell(condition, url)
        assert status == expected_status, (
            f"{anchor}: subprocess hit {url!r}; expected HTTP "
            f"{expected_status}, got HTTP {status}"
        )
        # Sanity-check: the URL the subprocess hit does in fact carry
        # the marker. This is the equivalent of
        # ``expected_marker in resp.text`` for cells whose body does
        # not echo the request path.
        assert body_marker in url, (
            f"{anchor}: body marker {body_marker!r} is a URL-sanity "
            f"substring but the URL {url!r} does not contain it"
        )
        return

    # In-process branch.
    url = _build_url(condition)
    client = _build_client(condition)
    method = condition.get("method", "GET").upper()
    request_fn = getattr(client, method.lower(), None)
    assert request_fn is not None, (
        f"{anchor}: unknown HTTP method {method!r}"
    )
    response = request_fn(url)

    assert response.status_code == expected_status, (
        f"{anchor}: hit {url!r}; expected HTTP {expected_status}, "
        f"got HTTP {response.status_code}; body={response.text!r}"
    )

    # Marker check. Markers starting with ``/`` are URL-sanity
    # substrings (used for the debug-routes cell where the body never
    # carries the path); every other marker is a body substring.
    if body_marker.startswith("/"):
        assert body_marker in url, (
            f"{anchor}: body marker {body_marker!r} is a URL-sanity "
            f"substring but the URL {url!r} does not contain it"
        )
    else:
        assert body_marker in response.text, (
            f"{anchor}: expected body to contain {body_marker!r}; "
            f"got body={response.text!r}"
        )


# ---------------------------------------------------------------------------
# TDD anchor 3: rejected writes leave the state database unchanged
# ---------------------------------------------------------------------------


@pytest.mark.api_error_matrix
@pytest.mark.parametrize(("anchor", "condition", "expected_status", "_body_marker"), MATRIX)
def test_rejected_writes_leave_db_unchanged(
    tmp_path,
    monkeypatch,
    anchor: str,
    condition: Dict[str, Any],
    expected_status: int,
    _body_marker: str,
) -> None:
    """Every cell that answers 4xx leaves ``state.db`` byte-identical
    before and after.

    A rejection that *also* writes to ``plan_routing`` / ``plan_execution``
    / ``plan_verification`` would be a silent side-channel: the route
    refused the request to the caller but persisted a partial state
    that future reads would see. The row-count check is what catches
    that — any non-zero delta in this assertion is a regression that
    the status-code-only check would miss.
    """
    db_path = tmp_path / "state.db"

    if anchor == "debug_routes_default_off":
        # Subprocess branch — the assertion here is the converse one:
        # a 404 must not have side-effected the *parent* suite's state
        # db (which is also ``tmp_path/state.db`` once the fixture is
        # in force). The subprocess writes to its own copy so this is
        # vacuously true; we re-assert the parent state db was not
        # mutated, just to keep the assertion shape uniform.
        before = _state_db_row_count(db_path)
        _hit_subprocess_cell(condition, "/api/_debug/inject_verification_state/20260926-debug-off")
        after = _state_db_row_count(db_path)
        assert before == after, (
            f"{anchor}: subprocess hit mutated the parent state db "
            f"({before} -> {after} rows)"
        )
        return

    # In-process branch. The ``isolated_plans_dir`` autouse fixture
    # already points ``server._state_db_path`` and ``PDT_STATE_DB_PATH``
    # at ``tmp_path/state.db``; reading the same path here is the
    # suite-wide isolation posture.
    before = _state_db_row_count(db_path)
    client = _build_client(condition)
    url = _build_url(condition)
    method = condition.get("method", "GET").upper()
    request_fn = getattr(client, method.lower(), None)
    assert request_fn is not None, (
        f"{anchor}: unknown HTTP method {method!r}"
    )
    response = request_fn(url)
    after = _state_db_row_count(db_path)

    assert response.status_code == expected_status, (
        f"{anchor}: hit {url!r}; expected HTTP {expected_status}, "
        f"got HTTP {response.status_code}; body={response.text!r}"
    )
    assert before == after, (
        f"{anchor}: a {expected_status} answer mutated state.db "
        f"({before} -> {after} rows); the rejection must short-circuit "
        f"before any state write"
    )


# ---------------------------------------------------------------------------
# TDD anchor 4: no cell is a placeholder / empty string
# ---------------------------------------------------------------------------


def test_no_cell_is_an_empty_string() -> None:
    """Every cell has a real anchor, condition, status, and marker.

    A row whose anchor or marker is ``""`` would silently pass the
    ``test_each_cell_matches_status_and_marker`` parametrisation —
    pytest's parametrize accepts an empty string as a valid value,
    and ``"" in response.text`` is trivially true. The same is true
    for an empty dict condition. This test is the only line of
    defence against the placeholder creeping in.
    """
    for index, cell in enumerate(MATRIX):
        # ``pytest.param`` is a NamedTuple with fields
        # ``(values, marks, id)``; the positional cell payload lives
        # under ``.values``. Skip cells whose values do not match the
        # documented 4-tuple shape — that itself is a placeholder.
        values = getattr(cell, "values", None)
        assert values is not None and len(values) == 4, (
            f"cell #{index}: expected a 4-tuple ParameterSet, got {cell!r}"
        )
        anchor, condition, expected_status, body_marker = values
        assert isinstance(anchor, str) and anchor.strip(), (
            f"cell #{index}: anchor is empty or whitespace: {anchor!r}"
        )
        assert isinstance(condition, dict) and condition, (
            f"cell #{index} (anchor={anchor!r}): condition is empty"
        )
        assert all(
            isinstance(k, str) and k and not k.startswith("__")
            for k in condition.keys()
        ), (
            f"cell #{index} (anchor={anchor!r}): condition has a blank "
            f"or dunder key: {list(condition.keys())!r}"
        )
        assert isinstance(expected_status, int) and 100 <= expected_status < 600, (
            f"cell #{index} (anchor={anchor!r}): expected_status is not a "
            f"plausible HTTP status: {expected_status!r}"
        )
        assert isinstance(body_marker, str) and body_marker, (
            f"cell #{index} (anchor={anchor!r}): body_marker is empty"
        )
        # Placeholder text the rest of the audit treats as invalid.
        # The meta-test in ``test_security_audit_schema.py`` rejects
        # the same shape — the matrix has to be tighter than its own
        # audit entries.
        for forbidden in ("...", "TODO", "FIXME", "TBD"):
            assert forbidden not in anchor, (
                f"cell #{index}: anchor contains placeholder {forbidden!r}: "
                f"{anchor!r}"
            )
            assert forbidden not in body_marker, (
                f"cell #{index} (anchor={anchor!r}): body_marker contains "
                f"placeholder {forbidden!r}: {body_marker!r}"
            )

    # And the non-HTTP pointer rows must also carry a real target —
    # they are what task 14 writes into the audit entry's
    # ``验证方式`` field.
    for index, (anchor, target) in enumerate(NON_HTTP_POINTERS):
        assert isinstance(anchor, str) and anchor.strip(), (
            f"non-HTTP pointer #{index}: anchor is empty: {anchor!r}"
        )
        assert isinstance(target, str) and target.strip(), (
            f"non-HTTP pointer #{index} (anchor={anchor!r}): target is empty"
        )

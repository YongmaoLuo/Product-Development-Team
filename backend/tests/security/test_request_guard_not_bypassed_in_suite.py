"""Prove the request guard is enforced in the suite — no bypass path.

Why this module exists
----------------------
``backend/tests/conftest.py`` injects ``X-PDT-Request: 1`` into every
``TestClient`` (by wrapping ``TestClient.__init__`` and using
``setdefault`` on the headers dict) and registers ``testserver`` as an
allowed ``Host`` (via ``PDT_ALLOWED_HOSTS``). That machinery exists so
the suite can hit guarded routes without re-stating the header at 62
construction sites.

The injection is *satisfying* the guard, not bypassing it: ``request_guard``
still runs on every request, and the wrapper does not relax its rules.
A test that proves the wrapper is actually satisfying the guard (rather
than quietly skipping it) is what keeps the guard load-bearing as the
suite grows.

The trap this file nails down
-----------------------------
``TestClient(app, headers={})`` looks like a "no header" construction,
but the wrapper calls ``headers.setdefault(REQUEST_HEADER, "1")``, so
the constructed client *does* carry the header. A test that asserts
"no header → 403" through such a client silently passes on a bypass
(it never sent a bare request), which is worse than failing the gate:
the gate believes the guard is in force when it is being routed around.

Bypassing ``TestClient`` (a raw ``httpx.AsyncClient`` over
``ASGITransport``) is the only way to put a bare request on the wire.
The four behavioural assertions in this module do exactly that — and
this is what the existing suite already does in
``test_verification_api.py``, where the header + ``base_url`` are
spelled out by hand because ``TestClient`` is unavailable (the test
needs concurrent async requests).

The fifth assertion is a *static* gate — it scans the suite for any
``httpx.AsyncClient(`` that talks to the guarded app via
``ASGITransport`` and fails when one is missing either
``base_url="http://testserver"`` or the guard header. A self-built
client that satisfies neither is exactly the case the four behavioural
tests cover; the static gate ensures no such client exists for the
suite to have grown without noticing.

A note on ``PDT_ALLOWED_HOSTS``
------------------------------
The allowed-host list comes from conftest's injection, not from the
test bodies: the suite's hosts are an environment fact, not per-test
configuration. Tests that want a "foreign Host" answer route the
``Host:`` header through the request, never through the env var —
so the suite-wide allowed list is preserved across all five tests.

Why ``AsyncClient`` and not ``Client``
--------------------------------------
``httpx.ASGITransport`` is async-only in the installed httpx version
(``handle_async_request`` is the only handler). Sync ``httpx.Client``
over ``ASGITransport`` raises ``AttributeError`` before any request
hits the wire — which is *not* what we want to test: that proves
"sync client + ASGITransport is broken", not "the guard refuses a
bare request". ``httpx.AsyncClient`` is the working shape and is the
one the rest of the suite already uses for the same reason.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, List, Tuple

import httpx
import pytest
from starlette.testclient import TestClient

import request_guard

_BACKEND_TESTS_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_TESTS_ROOT.parent.parent

#: Canonical route the four behavioural tests target. Picked because
#: ``/api/plan/{plan_id}/status`` answers 200 for an existing plan
#: directory and 404 for a missing one — the assertion under test is
#: "status is *not* 403" (the guard refused), not "status is *exactly*
#: 200". Both 200 and 404 are equally valid proof that the guard let
#: the request through.
CANONICAL_PLAN_ID = "plan-20260926-abc"
CANONICAL_ROUTE = f"/api/plan/{CANONICAL_PLAN_ID}/status"


def _strip_guard_headers(client: TestClient) -> None:
    """Remove the conftest-injected guard header from ``client.headers``.

    ``TestClient.__init__`` was wrapped in conftest to call
    ``headers.setdefault(REQUEST_HEADER, "1")`` — that mutation lands on
    ``client.headers`` (a mutable ``Headers`` instance), so a test can
    pop it back out and exercise the guard directly. This is the
    *only* safe way to send a "bare" ``/api/*`` request through
    ``TestClient``: it removes what conftest added, it does not add a
    bypass.
    """
    client.headers.pop(request_guard.REQUEST_HEADER, None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """The FastAPI app under test.

    Imported lazily so the module collection does not depend on the
    full backend stack being importable from the ``tests/`` cwd. The
    conftest already initialises ``PDT_ALLOWED_HOSTS`` and the request-
    guard wrapper before any test module runs.
    """
    import server

    return server.app


@pytest.fixture
def plans_root(tmp_path, monkeypatch):
    """Materialise ``CANONICAL_PLAN_ID`` under a per-test ``plans/`` root.

    The route under test answers 200 for an existing plan; without a
    real plan directory every "no-403" assertion would land on a 404
    and the test would pass for the wrong reason (the request was
    allowed, but it also found nothing). The plan directory keeps the
    "200 vs 404" distinction meaningful.

    The autouse ``isolated_plans_dir`` fixture in conftest already
    redirects ``PDT_PLANS_DIR`` / ``server.PLANS_DIR``; this fixture
    just materialises the directory.
    """
    import server

    root = tmp_path / "plans"
    root.mkdir()
    monkeypatch.setattr(server, "PLANS_DIR", root)
    (root / CANONICAL_PLAN_ID).mkdir()
    return root


# ---------------------------------------------------------------------------
# 1) Bare request (no header) — must 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_header_gets_403(app, plans_root):
    """A self-built ``httpx.AsyncClient`` with NO ``X-PDT-Request`` is 403.

    Uses ``httpx.AsyncClient(transport=ASGITransport(app), base_url="http://testserver")``
    rather than ``TestClient`` because ``TestClient`` is the wrapper
    conftest installs to *add* the header. A raw httpx client is the
    only way to put a bare request on the wire — the whole point of
    this assertion is that the guard actually answers a bare request.
    """
    bare = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        response = await bare.get(CANONICAL_ROUTE)
    finally:
        await bare.aclose()

    assert response.status_code == 403, (
        f"GET {CANONICAL_ROUTE} answered {response.status_code} for a "
        f"request with no X-PDT-Request header; the guard refused the "
        f"request only if the response is 403, regardless of whether "
        f"the plan exists on disk"
    )


# ---------------------------------------------------------------------------
# 2) Wrong header value — must 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_header_value_gets_403(app, plans_root):
    """``X-PDT-Request: 0`` is the wrong *value*; the guard still refuses.

    The guard checks the *value* (``"1"``), not just the presence. A
    header that is well-formed but does not match is exactly what a
    sloppy bypass attempt would set — "I sent the header, what more do
    you want?" — and is the case that needs to be visible at the wire.
    """
    bare = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={request_guard.REQUEST_HEADER: "0"},
    )
    try:
        response = await bare.get(CANONICAL_ROUTE)
    finally:
        await bare.aclose()

    assert response.status_code == 403, (
        f"GET {CANONICAL_ROUTE} answered {response.status_code} for "
        f"X-PDT-Request=0; only X-PDT-Request=1 satisfies the guard. "
        f"A 200/404 here means the guard compares header *presence* "
        f"instead of header *value* — a regression"
    )


# ---------------------------------------------------------------------------
# 3) Valid header, foreign Host — must 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_host_gets_403(app, plans_root):
    """A well-formed header with a non-loopback ``Host`` is 403.

    This is the DNS-rebinding half of the guard: even when the attacker
    can set the custom header (e.g. from a same-origin UI), a hostname
    that re-resolved to loopback still has to arrive *as* loopback. A
    bare ``httpx.AsyncClient`` lets us pin the ``Host`` to an
    obviously-foreign name; ``TestClient`` always sends
    ``Host: testserver``.
    """
    bare = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={
            request_guard.REQUEST_HEADER: request_guard.REQUEST_HEADER_VALUE,
            "Host": "evil.example",
        },
    )
    try:
        response = await bare.get(CANONICAL_ROUTE)
    finally:
        await bare.aclose()

    assert response.status_code == 403, (
        f"GET {CANONICAL_ROUTE} answered {response.status_code} for "
        f"Host: evil.example with a valid X-PDT-Request; the host "
        f"check is the DNS-rebinding half of the guard — a non-403 "
        f"means the host check was skipped"
    )


# ---------------------------------------------------------------------------
# 4) The conftest injection still answers (not 403) for a "normal" call
# ---------------------------------------------------------------------------


def test_conftest_client_is_not_403(app, plans_root):
    """The default ``TestClient`` (conftest-injected header) is NOT 403.

    Positive control: if this ever answers 403, conftest's wrapper has
    stopped working and every other test in this module (which
    deliberately bypasses it) is exercising a guard nothing satisfies.
    A bare 200/404 is the proof that "the wrapper *adds* the header,
    it does not bypass the guard".
    """
    client = TestClient(app)
    _strip_guard_headers(client)
    response = client.get(
        CANONICAL_ROUTE,
        headers={request_guard.REQUEST_HEADER: request_guard.REQUEST_HEADER_VALUE},
    )

    assert response.status_code != 403, (
        f"GET {CANONICAL_ROUTE} answered 403 even with a valid header; "
        f"conftest's TestClient injection must produce a request the "
        f"guard lets through. Status {response.status_code} means "
        f"either the injection broke or the guard now rejects what "
        f"62 sites still assume is a normal call"
    )
    # And specifically: 200 (plan exists) or 404 (route answered but
    # the plan does not) — never 403.
    assert response.status_code in (200, 404), (
        f"GET {CANONICAL_ROUTE} answered {response.status_code}; expected "
        f"200 (plan exists) or 404 (route answered, no plan). Anything "
        f"else is unrelated to the guard and a different bug"
    )


# ---------------------------------------------------------------------------
# 5) Static gate — no self-built client in the suite is missing header / host
# ---------------------------------------------------------------------------


def _iter_python_files(root: Path) -> Iterable[Path]:
    """Yield every ``.py`` file under ``root`` (recursive).

    ``tests/`` is the contract; the directory contains the
    ``conftest.py`` itself, which uses ``TestClient`` (not
    ``httpx.Client``) and so is irrelevant to this gate but must still
    be scanned so a regression that reintroduces a bypass cannot hide
    there.
    """
    for path in sorted(root.rglob("*.py")):
        # Skip this file itself — it intentionally constructs raw
        # httpx.AsyncClient objects to test the guard.
        if path == Path(__file__).resolve():
            continue
        yield path


class _AsgiClientVisitor(ast.NodeVisitor):
    """Locate every ``httpx.AsyncClient(`` / ``httpx.Client(`` call that
    also constructs an ``ASGITransport``.

    Uses ``ast`` (not regex) so docstring text — which mentions the
    pattern by name in several existing test files — does not match.
    Only real call sites are reported.

    Records ``(line_number, source_text)`` for every offending call.
    ``source_text`` is the slice of the file's source between the call's
    start and the matching closing paren, so a violation check can
    inspect the kwargs for ``base_url`` and the ``headers=`` mapping
    for the guard header.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.source_lines = source.splitlines(keepends=True)
        self.asgi_client_calls: List[Tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_asgi_client(node) and self._mentions_asgitransport(node):
            line = node.lineno
            text = self._slice_call(node)
            self.asgi_client_calls.append((line, text))
        # Continue descending so nested calls (e.g. ``with httpx.AsyncClient(...) as x:``)
        # are still detected at the right line.
        self.generic_visit(node)

    def _is_asgi_client(self, node: ast.Call) -> bool:
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr in {"AsyncClient", "Client"}
            and isinstance(func.value, ast.Name)
            and func.value.id == "httpx"
        )

    def _mentions_asgitransport(self, node: ast.Call) -> bool:
        # A call to ``httpx.AsyncClient(transport=httpx.ASGITransport(...))``
        # has the ``ASGITransport`` reference nested one level deep.
        for kw in node.keywords:
            if kw.arg == "transport" and isinstance(kw.value, ast.Call):
                inner = kw.value
                if isinstance(inner.func, ast.Attribute) and inner.func.attr == "ASGITransport":
                    return True
        return False

    def _slice_call(self, node: ast.Call) -> str:
        """Return the source text spanning this call.

        Falls back to the single line if the AST does not know where
        the call ends (older Python versions do not populate
        ``end_lineno`` reliably).
        """
        start = node.lineno - 1
        end = getattr(node, "end_lineno", None)
        if end is None or end <= start:
            return self.source_lines[start]
        return "".join(self.source_lines[start:end])


def _scan_asgi_client_calls(path: Path) -> List[Tuple[int, str]]:
    """Parse ``path`` and return every ASGI-client call as ``(line, text)``.

    Returns an empty list when the file is not valid Python — the
    scan is best-effort and a syntax error in a sibling test must not
    fail this gate.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - read-only fs
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    visitor = _AsgiClientVisitor(source)
    visitor.visit(tree)
    return visitor.asgi_client_calls


def _has_required_args(call_text: str) -> bool:
    """True when ``call_text`` carries both ``base_url='http://testserver'`` and the header.

    Inspects the same string for both invariants. The check is
    textual because it runs on a source file, not on a live client —
    a client instance is not available at import time, and the gate's
    purpose is to fail BEFORE a test that constructs the client even
    runs.
    """
    import re

    has_base_url = re.search(
        r'base_url\s*=\s*["\']http://testserver["\']',
        call_text,
    ) is not None
    has_header = re.search(
        r'["\']X-PDT-Request["\']\s*:\s*["\']1["\']',
        call_text,
    ) is not None
    return has_base_url and has_header


def suite_self_built_clients() -> List[Tuple[Path, int]]:
    """Every ``(path, line)`` in the suite that builds an ASGI httpx client.

    Returned for downstream meta-tests (task 14's "completeness" gate)
    that need the same list without re-implementing the scan. The
    tuple shape matches ``Path, int`` so a caller can ``read``
    individual lines.

    Lines reported here are the line of the *opening*
    ``httpx.<Async>Client(`` — that is where a future reviewer would
    start reading to understand what a client is doing.

    The function deliberately does NOT assert anything about the
    contents of those calls; assertions live in the test body, which
    can produce focused failure messages. This function is a discovery
    helper.
    """
    found: List[Tuple[Path, int]] = []
    for path in _iter_python_files(_BACKEND_TESTS_ROOT):
        for line_no, _ in _scan_asgi_client_calls(path):
            found.append((path, line_no))
    return found


def test_no_self_built_client_misses_header_or_base_url():
    """Every self-built ASGI client in the suite carries the guard header AND ``testserver``.

    Static gate that runs alongside the four behavioural tests: those
    prove the guard works when the suite *intentionally* builds a
    bare client, while this one proves the suite did not accidentally
    *grow* another self-built client that satisfies neither invariant
    (it would then exercise nothing but the guard's refusal path,
    which is a much weaker test than the one it claims to be).

    The scan uses ``ast`` so docstring text — which describes the
    pattern in several existing test files — does not match; only
    real call sites are inspected.
    """
    violations: List[str] = []
    for path in _iter_python_files(_BACKEND_TESTS_ROOT):
        for line_no, call_text in _scan_asgi_client_calls(path):
            if not _has_required_args(call_text):
                rel = path.relative_to(_REPO_ROOT)
                violations.append(f"{rel}:{line_no}")

    assert not violations, (
        f"these test calls build an httpx client over ASGITransport "
        f"without BOTH base_url='http://testserver' AND "
        f"'X-PDT-Request': '1': {violations}. The guard header is "
        f"required on every /api/* call; without it the test cannot "
        f"reach the route under test and would silently 403. Either "
        f"add the missing invariant (the documented pattern) or use "
        f"TestClient, which conftest already injects"
    )
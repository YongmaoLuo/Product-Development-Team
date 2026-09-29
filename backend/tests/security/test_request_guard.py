"""The loopback API refuses browser-originated cross-origin calls.

Regression guard for the request guard added 2026-09-26.

What was wrong
--------------
Every endpoint was reachable by any page the operator visited. The
server bound loopback and the README treated that as the safety
property, but loopback is not a boundary a browser respects: a forged
page issues its requests *from* 127.0.0.1, so a bind-host check cannot
tell it apart from the bundled UI. Measured against the app as it was::

    GET  /api/plans                              -> 200  (enumerate ids)
    POST /api/execution/<id>/start
         {"project_dir": "<any path>"}           -> 200  (spawn the agent)

The second one runs ``cli.py --recover -w <dir>`` — an agent that writes
files and runs shell commands in that directory — and only ``backend/``
was refused as a target. ``Content-Type: text/plain`` keeps the request
a CORS "simple request", so no preflight intervenes, and Starlette's
``Request.json()`` parses the body without consulting Content-Type, so
the parsed payload still lands. The attacker cannot read the response;
they do not need to.

What this pins
--------------
* the header requirement, on every method (a GET leaks the plan list);
* Origin rejection, including the ``null`` origin a sandboxed iframe or
  a ``file://`` page sends;
* Host rejection, which is what answers DNS rebinding;
* static assets staying open, because browser navigation cannot attach
  a header and the UI must remain loadable;
* debug routes staying out of the default route table.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import request_guard
from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE

_BACKEND_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture()
def app():
    import server

    return server.app


@pytest.fixture()
def raw_client(app):
    """A client that sends *no* guard header.

    ``conftest`` injects the header into every ``TestClient`` so the rest
    of the suite keeps testing routes rather than plumbing. This fixture
    exists to test the plumbing.
    """
    return TestClient(app, headers={})


def _strip_guard_headers(client: TestClient) -> TestClient:
    """Remove the default header so a call goes out bare."""
    client.headers.pop(REQUEST_HEADER, None)
    return client


# ---------------------------------------------------------------------------
# The header requirement
# ---------------------------------------------------------------------------


def test_api_without_header_is_refused(raw_client):
    _strip_guard_headers(raw_client)
    response = raw_client.get("/api/plans")
    assert response.status_code == 403
    assert REQUEST_HEADER in response.json()["detail"]


def test_api_with_wrong_header_value_is_refused(raw_client):
    _strip_guard_headers(raw_client)
    response = raw_client.get("/api/plans", headers={REQUEST_HEADER: "0"})
    assert response.status_code == 403


def test_api_with_header_is_served(raw_client):
    _strip_guard_headers(raw_client)
    response = raw_client.get("/api/plans", headers={REQUEST_HEADER: REQUEST_HEADER_VALUE})
    assert response.status_code == 200


@pytest.mark.parametrize("method", ["get", "post", "put", "delete"])
def test_every_method_is_guarded(raw_client, method):
    """A read leaks as much as a write here — ``/api/plans`` names every
    plan, and ``/api/execution/{id}/files`` walks the project tree."""
    _strip_guard_headers(raw_client)
    response = getattr(raw_client, method)("/api/plans")
    assert response.status_code == 403


def test_static_assets_stay_reachable_without_header(raw_client):
    """Browser navigation cannot set a header. If this breaks, the UI
    cannot load at all — which is why the guard covers ``/api/`` only."""
    _strip_guard_headers(raw_client)
    response = raw_client.get("/")
    assert response.status_code == 200
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# Origin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "https://evil.example",
        "null",  # sandboxed iframe / file://
        "http://127.0.0.1.evil.example",  # suffix-confusion attempt
        "http://localhost.evil.example",
    ],
)
def test_foreign_origin_is_refused(raw_client, origin):
    _strip_guard_headers(raw_client)
    response = raw_client.get(
        "/api/plans",
        headers={REQUEST_HEADER: REQUEST_HEADER_VALUE, "Origin": origin},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "origin",
    ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"],
)
def test_loopback_origin_is_served(raw_client, origin):
    _strip_guard_headers(raw_client)
    response = raw_client.get(
        "/api/plans",
        headers={REQUEST_HEADER: REQUEST_HEADER_VALUE, "Origin": origin},
    )
    assert response.status_code == 200


def test_absent_origin_is_served(raw_client):
    """``curl`` and local scripts send no Origin; they keep working."""
    _strip_guard_headers(raw_client)
    response = raw_client.get("/api/plans", headers={REQUEST_HEADER: REQUEST_HEADER_VALUE})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Host — the DNS-rebinding half
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["evil.example", "attacker.test:8000", "127.0.0.1.evil.example"],
)
def test_foreign_host_is_refused(raw_client, host):
    _strip_guard_headers(raw_client)
    response = raw_client.get(
        "/api/plans",
        headers={REQUEST_HEADER: REQUEST_HEADER_VALUE, "Host": host},
    )
    assert response.status_code == 403


def test_configured_extra_host_is_served(raw_client, monkeypatch):
    """``PDT_ALLOWED_HOSTS`` is the escape hatch for a container name or
    a reverse proxy's hostname — without it the guard would be routed
    around rather than configured."""
    _strip_guard_headers(raw_client)
    monkeypatch.setenv(request_guard.ENV_ALLOWED_HOSTS, "pdt.internal")
    response = raw_client.get(
        "/api/plans",
        headers={REQUEST_HEADER: REQUEST_HEADER_VALUE, "Host": "pdt.internal:8000"},
    )
    assert response.status_code == 200


def test_host_header_without_port_is_matched():
    assert request_guard._split_host_header("localhost") == "localhost"
    assert request_guard._split_host_header("localhost:8000") == "localhost"
    # A naive split(":") on the bracketed form returns "[".
    assert request_guard._split_host_header("[::1]:8000") == "::1"


def test_unparseable_origin_is_refused():
    assert request_guard._origin_hostname("http://[not-an-ip") == ""


# ---------------------------------------------------------------------------
# Debug routes
# ---------------------------------------------------------------------------


def test_debug_routes_are_absent_by_default():
    """``PDT_DEBUG_ROUTES`` is opt-in.

    Checked in a subprocess because registration happens at import of
    ``server`` — the running suite has the env var set (``conftest``),
    so an in-process assertion would only ever see the enabled state.
    """
    repo_root = _BACKEND_DIR.parent
    script = (
        "import os, sys;"
        "os.environ.pop('PDT_DEBUG_ROUTES', None);"
        f"sys.path[:0] = [{str(repo_root)!r}, {str(_BACKEND_DIR)!r}];"
        "import server;"
        "print([r.path for r in server.iter_app_routes()"
        " if r.path.startswith('/api/_debug') or r.path.startswith('/api/debug')])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "[]", proc.stdout


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("YES", True), ("on", True),
     ("0", False), ("", False), ("no", False)],
)
def test_debug_routes_enabled_parsing(monkeypatch, value, expected):
    import server

    monkeypatch.setenv(server.ENV_DEBUG_ROUTES, value)
    assert server.debug_routes_enabled() is expected


def test_debug_routes_are_reachable_when_opted_in():
    """The other branch of the switch, so "off by default" cannot be
    satisfied by a route table that never had them either way.

    Enumerated through ``iter_app_routes`` rather than ``app.routes``:
    an ``include_router`` call lands as one ``_IncludedRouter`` wrapper,
    so the flat list is the only place an included route is visible.
    """
    import server

    assert server.debug_routes_enabled(), (
        "conftest must opt this suite into the debug routes for the rest "
        "of the suite to exercise them"
    )
    paths = [route.path for route in server.iter_app_routes()]
    assert any(p.startswith("/api/_debug/") for p in paths), paths[:5]

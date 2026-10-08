"""The static route serves the frontend bundle, and nothing else.

Regression guard for the arbitrary-file-read closed on 2026-09-25.

What was wrong
--------------
``serve_static`` was registered as ``@app.get("/{path:path}")`` and did a
bare ``FRONTEND_DIR / path``. Two properties of that combination make it
a filesystem-wide file server:

* the ``:path`` converter matches ``/``, so the parameter may contain
  path separators at all; and
* Starlette decodes percent escapes *before* matching, so ``%2e%2e%2f``
  is already ``../`` by the time the handler sees it.

Measured against the app as it was, on the developer's own machine::

    GET /%2e%2e%2fREADME.md                       -> 200
    GET /%2e%2e%2fbackend%2f.env.example          -> 200
    GET /<8 x %2e%2e%2f>etc%2fpasswd              -> 200
    GET /<5 x %2e%2e%2f>.cc-switch%2fcc-switch.db -> 200, 93 MB

The last one is the one that matters. This project reads CC Switch by
design, so on any machine that runs it, ``~/.cc-switch/cc-switch.db``
holds every provider's ``ANTHROPIC_AUTH_TOKEN`` in clear text — and a
single unauthenticated GET returned the whole file.

Why nothing caught it
---------------------
``tests/security/test_path_traversal.py`` covers ``provider_order``'s
path handling, not the HTTP layer. No test ever sent a request to this
process, so the route's capability was never compared against its
purpose. These tests send real requests.

The checks are deliberately of two kinds — a direct unit call on the
containment helper, and end-to-end requests through the real ASGI app —
so that neither a refactor of the helper nor a change to route
registration can quietly restore the hole.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


#: A file that certainly exists and is certainly *not* in the frontend
#: bundle: the server module itself. Chosen over a fixture so that a
#: missing target can never make a traversal assertion pass vacuously —
#: ``test_the_traversal_target_really_exists`` pins that.
OUTSIDE_TARGET = Path(server.__file__).resolve()

#: One ``../`` per component between the frontend root and ``/``.
_CLIMB = "%2e%2e%2f" * (len(server.FRONTEND_DIR.resolve().parts) - 1)


def _encoded(p: Path) -> str:
    """Percent-encode an absolute path for use as a URL path."""
    return str(p).lstrip("/").replace("/", "%2f")


@pytest.fixture(scope="module")
def client():
    # NOTE: no ``with`` block. Entering the context manager runs the
    # lifespan, which starts the supervisor fleet and the scheduler — a
    # test that only wants to ask the router a question must not do that.
    return TestClient(server.app)


def test_the_traversal_target_really_exists():
    """Without this, every traversal test below could pass on a 404 that
    has nothing to do with containment."""
    assert OUTSIDE_TARGET.is_file(), (
        f"{OUTSIDE_TARGET} must exist for the traversal tests to mean "
        "anything"
    )
    assert not OUTSIDE_TARGET.is_relative_to(server.FRONTEND_DIR.resolve())


# ---------------------------------------------------------------------------
# End to end, through the real app
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        f"/{_CLIMB}{_encoded(OUTSIDE_TARGET)}",
        "/%2e%2e%2fbackend%2fserver.py",
        "/..%2f..%2fREADME.md",
        "/%2e%2e%2fREADME.md",
        "/" + "%2e%2e%2f" * 8 + "etc%2fpasswd",
        "/" + "%2e%2e%2f" * 8 + "etc%2fhosts",
        "/etc/passwd",
    ],
)
def test_traversal_payloads_are_refused(client, payload):
    """Encoded traversal must not reach outside the frontend bundle."""
    response = client.get(payload)
    assert response.status_code == 404, (
        f"GET {payload} returned {response.status_code} — the static "
        f"route served a file outside the frontend bundle"
    )


def test_the_repository_is_not_reachable_as_static_content(client):
    """The repo root sits one level up and was reachable before the fix.

    ``plans/``, ``.config/`` and ``.env`` all live beside the frontend
    directory, so a single ``../`` was enough to start reading the
    checkout. ``.env`` is probed bare as well as through ``backend/``:
    it moved to the repository root in 2026-10-08, and a containment
    probe that only ever named the old location would keep passing
    while the file it names is one that no longer exists.
    """
    probes = (
        "README.md",
        "CLAUDE.md",
        ".env",
        "%2eenv",
        "backend%2f.env",
        "backend%2fserver.py",
    )
    for probe in probes:
        assert client.get(f"/%2e%2e%2f{probe}").status_code == 404, (
            f"the static route served a repository file through ../{probe}"
        )


def test_legitimate_assets_still_work(client):
    """The fix must not break the thing the route is for.

    The version query strings are the ones ``index.html`` actually
    carries (``style.css?v=15`` / ``app.js?v=17``), because the project
    relies on them for cache busting — Safari caches CSS aggressively.
    """
    for asset in ("/", "/index.html", "/style.css", "/app.js",
                  "/style.css?v=15", "/app.js?v=17"):
        assert client.get(asset).status_code == 200, f"{asset} stopped serving"


def test_cachable_assets_keep_their_no_cache_header(client):
    """The containment must not cost the cache headers.

    The frontend ships a ``Cache-Control`` meta tag in ``<head>`` *and*
    this response header. The header is the one that survives a hard
    reload and the one Safari honours, so it is behaviour worth pinning
    while the handler is being rewritten.
    """
    for asset in ("/", "/app.js", "/style.css"):
        header = client.get(asset).headers.get("cache-control", "")
        assert "no-cache" in header, f"{asset} lost its Cache-Control: {header!r}"


def test_a_directory_is_not_served(client, tmp_path):
    """A directory is not a file, even when the path is inside the bundle."""
    (server.FRONTEND_DIR / "__containment_probe_dir__").mkdir(exist_ok=True)
    try:
        assert client.get("/__containment_probe_dir__").status_code == 404
    finally:
        (server.FRONTEND_DIR / "__containment_probe_dir__").rmdir()


# ---------------------------------------------------------------------------
# The containment helper on its own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "escape",
    ["../backend/server.py", "../../README.md", "/etc/passwd", "", "."],
)
def test_resolver_refuses_anything_outside_the_bundle(escape):
    """Direct contract: the helper either returns an in-bundle file or 404s."""
    with pytest.raises(HTTPException) as excinfo:
        server._resolve_frontend_path(escape)
    assert excinfo.value.status_code == 404


def test_resolver_accepts_a_real_asset():
    """The helper's happy path, so the tests above are not just asserting
    that it always raises."""
    resolved = server._resolve_frontend_path("index.html")
    assert resolved.is_file()
    assert resolved.is_relative_to(server.FRONTEND_DIR.resolve())


def test_resolver_refuses_an_out_of_bundle_symlink(tmp_path):
    """``resolve()`` follows links, so an in-bundle link pointing out is
    caught by the same containment test rather than being served as if
    it were part of the bundle."""
    link = server.FRONTEND_DIR / "__containment_probe_link__"
    link.unlink(missing_ok=True)
    link.symlink_to(OUTSIDE_TARGET)
    try:
        with pytest.raises(HTTPException) as excinfo:
            server._resolve_frontend_path("__containment_probe_link__")
        assert excinfo.value.status_code == 404
    finally:
        link.unlink(missing_ok=True)

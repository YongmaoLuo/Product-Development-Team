"""Unit-level rejection matrix for :func:`request_guard.rejection_reason`.

What this pins
--------------
The integration suite (``test_request_guard.py``) drives the rejection
guard through ``TestClient``, which covers header presence, header
value ``"1"``, foreign ``Origin`` / ``Host``, and the ``/api/`` prefix.
That path is necessary but not sufficient: ``TestClient`` *injects* the
guard header into every request and registers ``testserver`` as an
allowed host (see ``conftest._install_request_guard_header``), so it
silently covers the cases that depend on those two pieces being set.

The cases that integration tests cannot see without falling out of the
testclient's wrapper are exactly the cases a refactor would silently
break:

* the **literal-value variants** of the header — ``"1 "`` (trailing
  whitespace), ``"01"`` (leading zero), ``"true"`` (truthy looking) —
  none of which is ``"1"``;
* the **header-name case-insensitivity** — Starlette lower-cases
  headers in scope, but a refactor that reads ``request.headers`` via a
  different code path could regress;
* the **host variants** — ``Host: ""`` (an upstream proxy stripping
  the header), ``Host: [::1]:8000`` (IPv6 loopback with port, which
  a naive ``split(":")`` would mishandle);
* the **Origin variants** — ``Origin: null`` (sandboxed iframe,
  ``file://``, redirect chain), absence of the header (``curl``);
* the **path variants** — any non-``/api/`` path is exempt, so a
  ``GET /index.html`` must always pass;
* a custom ``PDT_ALLOWED_HOSTS`` entry — the escape hatch the README
  advertises for a container / reverse-proxy hostname.

This file constructs every request through a real
``fastapi.Request(scope)`` so the function under test sees exactly what
it would in production, and never patches the guard itself (a
``monkeypatch.setattr("request_guard.rejection_reason", ...)`` here
would only assert against the patch).

A note on the ``TestClient`` trap
---------------------------------
``conftest._install_request_guard_header`` wraps
``TestClient.__init__`` so a client that constructs itself with
``headers={}`` still ends up sending ``X-PDT-Request: 1`` — the
"missing header" case is therefore unreachable through ``TestClient``
without first popping the injected value. Bypassing ``TestClient``
entirely, the way the tests below do, sidesteps that wrapper
completely, so a real "no header" request is what reaches
``rejection_reason``.

Marker
------
``@pytest.mark.unit`` — no subprocess, no network, no filesystem side
effects. The PR gate's unit shard runs this on every push.
"""

from __future__ import annotations

from typing import Iterable

import pytest
from fastapi import Request

import request_guard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(path: str, headers: Iterable[tuple[str, str]] | dict[str, str] | None = None) -> Request:
    """Build a real ``fastapi.Request`` from a Starlette ``scope``.

    Parameters mirror the task brief's input example::

        req = make_request(path="/api/plans",
                            headers={"host": "evil.example"})

    Header names are case-insensitive in HTTP and Starlette normalises
    them to lowercase bytes — the tests rely on this when the same
    logical header is spelled ``X-PDT-Request`` instead of
    ``x-pdt-request``. Accepting ``Iterable[tuple[str, str]]`` (and not
    just ``dict``) lets a test spell the same header twice on purpose
    (Starlette keeps the first), which is what the ``"1 "//" and the
    "01"/"true" variants share: the *value* shape that has to be
    refused, not the name.

    ``scope`` carries only what ``rejection_reason`` reads: ``type``
    (so ``Request.__init__`` accepts the scope), ``method`` and
    ``path`` (so ``request.url.path`` is well-formed), ``headers``,
    and an empty ``query_string``.
    """
    if headers is None:
        header_pairs: list[tuple[bytes, bytes]] = []
    elif isinstance(headers, dict):
        header_pairs = [
            (name.lower().encode("utf-8"), value.encode("utf-8"))
            for name, value in headers.items()
        ]
    else:
        header_pairs = [
            (name.lower().encode("utf-8"), value.encode("utf-8"))
            for name, value in headers
        ]

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": header_pairs,
        "query_string": b"",
    }
    return Request(scope)


@pytest.fixture
def make_request():
    """Per-test handle that delegates to :func:`_make_request`.

    Lives as a fixture (rather than a module-level helper) so a future
    test that wants to override it — say, to install a custom
    ``receive`` callable for ``body=`` cases — does not have to
    rewrite every call site.
    """
    return _make_request


# ---------------------------------------------------------------------------
# Header presence and value
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_header_is_rejected(make_request):
    """A request with no ``X-PDT-Request`` header is refused.

    This is the headline case the guard exists for — without the
    header, the request is indistinguishable from a cross-origin
    browser call, so it must be refused.
    """
    req = make_request(
        path="/api/plans",
        headers={"host": "127.0.0.1:8000"},
    )
    reason = request_guard.rejection_reason(req)
    assert reason is not None, "missing guard header must be rejected"
    assert request_guard.REQUEST_HEADER in reason, (
        f"rejection message should name {request_guard.REQUEST_HEADER!r}; "
        f"got: {reason!r}"
    )


@pytest.mark.unit
def test_header_case_variant_is_accepted(make_request):
    """``x-pdt-request: 1`` (lower-case) is accepted.

    Header names are case-insensitive per RFC 7230 §3.2 and Starlette
    lower-cases them in ``scope``, so any case-spelling must work. The
    guard reads via ``request.headers.get(...)``, which delegates to
    Starlette's case-insensitive lookup — this test pins that contract
    so a future refactor that switches to ``request.scope["headers"]``
    directly cannot regress it silently.
    """
    req = make_request(
        path="/api/plans",
        headers={
            "x-pdt-request": request_guard.REQUEST_HEADER_VALUE,
            "host": "127.0.0.1:8000",
        },
    )
    reason = request_guard.rejection_reason(req)
    assert reason is None, (
        f"lower-case header spelling must be accepted, got: {reason!r}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("value", ["1 ", "01", "true"])
def test_header_value_variants_are_rejected(make_request, value):
    """Only the literal ``"1"`` is the accepted value.

    * ``"1 "`` (trailing space) — what an HTTP client that appends a
      separator might send.
    * ``"01"`` (leading zero) — what a JSON-encoder that zero-pads
      strings might emit.
    * ``"true"`` — what a developer reading ``"boolean true"`` instead
      of ``"string 1"`` might guess.

    All three are non-empty and truthy-looking but not ``"1"``; the
    guard compares with ``!=``, so they all fail. A refactor that
    switched the check to ``if not header_value`` would silently let
    every one of these through.
    """
    req = make_request(
        path="/api/plans",
        headers={
            "x-pdt-request": value,
            "host": "127.0.0.1:8000",
        },
    )
    reason = request_guard.rejection_reason(req)
    assert reason is not None, (
        f"header value {value!r} must be rejected — only the literal "
        f"{request_guard.REQUEST_HEADER_VALUE!r} is accepted"
    )


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_foreign_host_is_rejected(make_request):
    """A loopback header but a foreign ``Host`` is refused.

    This is the DNS-rebinding half of the guard: the request-guard
    header is in place, but the host the request claims to be coming
    from is not loopback. A browser page rebinding its hostname can
    produce exactly this shape.
    """
    req = make_request(
        path="/api/plans",
        headers={
            "x-pdt-request": request_guard.REQUEST_HEADER_VALUE,
            "host": "evil.example",
        },
    )
    reason = request_guard.rejection_reason(req)
    assert reason is not None, (
        "foreign host with valid header must be rejected (DNS rebinding)"
    )
    assert "host" in reason.lower(), (
        f"rejection message should name the host check; got: {reason!r}"
    )


@pytest.mark.unit
def test_empty_host_is_rejected(make_request):
    """``Host: ""`` is rejected, not accepted as a wildcard.

    An empty host is not a loopback alias. A reverse proxy that
    stripped the ``Host`` header (or a misconfigured client) would
    send this; the guard must not let it through on the strength of
    "the value matches nothing, so the default applies".
    """
    req = make_request(
        path="/api/plans",
        headers={
            "x-pdt-request": request_guard.REQUEST_HEADER_VALUE,
            "host": "",
        },
    )
    reason = request_guard.rejection_reason(req)
    assert reason is not None, (
        "empty host header must be rejected — it is not a loopback alias"
    )


@pytest.mark.unit
def test_ipv6_loopback_host_is_accepted(make_request):
    """``Host: [::1]:8000`` is accepted (bracketed IPv6 form).

    A naive ``host.split(":")`` returns ``["[", "1]", "8000"]`` on this
    input and the first element is ``"["`` — which would fail the
    ``in allowed`` membership check and refuse a legitimate loopback
    request. The guard's ``_split_host_header`` strips the brackets;
    this test pins that contract end-to-end through
    ``rejection_reason``.
    """
    req = make_request(
        path="/api/plans",
        headers={
            "x-pdt-request": request_guard.REQUEST_HEADER_VALUE,
            "host": "[::1]:8000",
        },
    )
    reason = request_guard.rejection_reason(req)
    assert reason is None, (
        f"bracketed IPv6 loopback host must be accepted, got: {reason!r}"
    )


# ---------------------------------------------------------------------------
# Origin
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_null_origin_is_rejected(make_request):
    """``Origin: null`` (the literal four-character string) is refused.

    A sandboxed iframe, a ``file://`` page, and a redirect chain that
    strips the Origin all send ``Origin: null`` per the Fetch spec.
    ``urlsplit("null").hostname`` is ``""``, which is not in the
    allowed set, so the guard refuses the request. A refactor that
    let an empty hostname through "because there is nothing there"
    would re-open the sandboxed-iframe hole.
    """
    req = make_request(
        path="/api/plans",
        headers={
            "x-pdt-request": request_guard.REQUEST_HEADER_VALUE,
            "host": "127.0.0.1:8000",
            "origin": "null",
        },
    )
    reason = request_guard.rejection_reason(req)
    assert reason is not None, (
        "Origin: null must be rejected — sandboxed iframe / file:// "
        "pages all send this"
    )
    assert "origin" in reason.lower(), (
        f"rejection message should name the origin check; got: {reason!r}"
    )


# ---------------------------------------------------------------------------
# Path exemption
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("path", ["/", "/index.html", "/static/app.js", "/favicon.ico"])
def test_non_api_path_is_unguarded(make_request, path):
    """A path outside ``/api/`` is never refused, regardless of headers.

    Browser navigation cannot attach ``X-PDT-Request``, so a guard
    that ran on every path would make the UI unloadable. Static
    assets are also read-only copies of what ships in this repo, so
    leaving them open costs nothing.

    Every test in this parametrize runs with no guard header and a
    foreign host, both of which would be rejected on an ``/api/`` path.
    The point is precisely that they are NOT rejected here.
    """
    req = make_request(
        path=path,
        headers={
            "host": "evil.example",
            "origin": "http://evil.example",
            # Deliberately no x-pdt-request — this is the curl/navigation
            # shape the exemption exists for.
        },
    )
    reason = request_guard.rejection_reason(req)
    assert reason is None, (
        f"non-api path {path!r} must be unguarded, got: {reason!r}"
    )


# ---------------------------------------------------------------------------
# Configured extra host
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_custom_allowed_host_accepted(make_request, monkeypatch):
    """``PDT_ALLOWED_HOSTS`` lets an operator open one extra hostname.

    The container / reverse-proxy case the README documents: the
    guard binds to loopback, but a deployment running behind
    ``pdt.internal`` (or any other legitimate hostname) needs the
    ``Host`` check to accept that name. ``monkeypatch.setenv`` is the
    right tool here because ``allowed_hosts()`` re-resolves on every
    call — a module-level cache would make this test flaky, and the
    absence of a cache is itself the property the test pins.
    """
    monkeypatch.setenv(request_guard.ENV_ALLOWED_HOSTS, "testserver")
    req = make_request(
        path="/api/plans",
        headers={
            "x-pdt-request": request_guard.REQUEST_HEADER_VALUE,
            "host": "testserver",
        },
    )
    reason = request_guard.rejection_reason(req)
    assert reason is None, (
        f"host listed in PDT_ALLOWED_HOSTS must be accepted, got: {reason!r}"
    )
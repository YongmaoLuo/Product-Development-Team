"""Loopback request guard for the local API.

Why this module exists
----------------------
The server binds loopback by default, and the README used to present
that as the safety property. It is not one. **Loopback is not a security
boundary against a browser.** A page the operator merely *visits* can
issue requests to ``127.0.0.1:<port>`` from inside that browser, and the
server cannot tell those apart from the operator's own UI — both arrive
from 127.0.0.1 with no credentials to check.

Two protections close that, and they cover different halves of the
attack:

1. **A custom request header** (:data:`REQUEST_HEADER`). A cross-origin
   request that carries a custom header is no longer a CORS "simple
   request", so the browser must send a preflight first — and a
   preflight only succeeds if the server answers it with CORS headers,
   which this one deliberately never does. The forged call is therefore
   never sent at all. Note the header value is *not* a secret and is not
   meant to be: the defence is that a foreign origin cannot set it
   without a preflight, not that the value is hard to guess.
2. **Origin and Host validation.** Origin catches a same-origin-but-
   foreign page; Host catches DNS rebinding, where the attacker's
   hostname re-resolves to 127.0.0.1 and the request arrives with that
   hostname in ``Host`` (and, once the rebind lands, in ``Origin`` too).

A plain ``curl`` to the API keeps working: it sends no ``Origin``
header, and it can set the request header on the command line. What
stops working is exactly the case this exists to stop — a browser page
that is not this UI.

What is deliberately *not* guarded
----------------------------------
Static assets. ``index.html`` and its siblings are fetched by browser
*navigation*, which cannot attach a custom header, so requiring one
would make the UI unloadable. They are read-only copies of what already
ships in this repository, so leaving them open costs nothing.
"""

from __future__ import annotations

import os
from typing import FrozenSet, List, Optional
from urllib.parse import urlsplit

from fastapi import Request

from config_paths import resolve_server_host

#: Header every ``/api/*`` request must carry.
REQUEST_HEADER: str = "X-PDT-Request"

#: Required value for :data:`REQUEST_HEADER`. Public on purpose — see the
#: module docstring for why guessing it is not the threat model.
REQUEST_HEADER_VALUE: str = "1"

#: Only paths under this prefix are guarded.
GUARDED_PREFIX: str = "/api/"

#: Hostnames that mean "this machine". A request whose ``Host`` or
#: ``Origin`` resolves to anything else is refused.
_LOOPBACK_HOSTNAMES: FrozenSet[str] = frozenset(
    {"127.0.0.1", "localhost", "::1"}
)

#: Env var for hostnames beyond loopback and the bound interface — a
#: container hostname, a reverse proxy's name, or a test client's
#: default ``testserver``.
ENV_ALLOWED_HOSTS: str = "PDT_ALLOWED_HOSTS"


def _normalise_hostname(value: str) -> str:
    """Lower-case ``value`` and strip a trailing IPv6 bracket pair."""
    host = value.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host


def _split_host_header(host_header: str) -> str:
    """Hostname out of a ``Host`` header, without its port.

    Handles the bracketed IPv6 form (``[::1]:8000``), where a naive
    ``split(":")`` would return ``[``.
    """
    value = host_header.strip()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return _normalise_hostname(value[: end + 1]) if end != -1 else ""
    return _normalise_hostname(value.split(":", 1)[0])


def allowed_hosts() -> FrozenSet[str]:
    """Hostnames the guard accepts, resolved on every call.

    Resolution is deliberately not cached: tests and long-lived
    processes both change ``PDT_HOST`` / ``PDT_ALLOWED_HOSTS`` after
    import, and a stale snapshot would turn a correct configuration into
    a confusing 403.
    """
    hosts = set(_LOOPBACK_HOSTNAMES)
    bound = _normalise_hostname(resolve_server_host())
    if bound and bound not in ("0.0.0.0", "::", ""):
        hosts.add(bound)
    raw_extra = os.environ.get(ENV_ALLOWED_HOSTS, "")
    for entry in raw_extra.split(","):
        normalised = _normalise_hostname(entry)
        if normalised:
            hosts.add(normalised)
    return frozenset(hosts)


def _origin_hostname(origin: str) -> str:
    """Hostname out of an ``Origin`` header, or ``""`` if unusable."""
    try:
        return _normalise_hostname(urlsplit(origin).hostname or "")
    except ValueError:
        # ``urlsplit`` raises on malformed IPv6 literals in some
        # runtimes; an unparseable Origin is not an allowed one.
        return ""


def rejection_reason(request: Request) -> Optional[str]:
    """Why this request must be refused, or ``None`` to let it through.

    Returns a short operator-facing string rather than a boolean so the
    403 body names the check that failed — a guard you cannot debug
    from the client side is one an operator routes around.
    """
    if not request.url.path.startswith(GUARDED_PREFIX):
        return None

    if request.headers.get(REQUEST_HEADER) != REQUEST_HEADER_VALUE:
        return (
            f"missing {REQUEST_HEADER} header. This API is driven by the "
            f"bundled UI or by a local script; a browser page from "
            f"another origin cannot set this header without a CORS "
            f"preflight, which this server never grants. Send "
            f"'{REQUEST_HEADER}: {REQUEST_HEADER_VALUE}'."
        )

    allowed = allowed_hosts()

    origin = request.headers.get("origin")
    if origin is not None:
        origin_host = _origin_hostname(origin)
        # ``Origin: null`` (sandboxed iframe, file://, some redirect
        # chains) parses to an empty hostname and is refused here.
        if origin_host not in allowed:
            return f"origin {origin!r} is not served by this instance."

    host_header = request.headers.get("host", "")
    if _split_host_header(host_header) not in allowed:
        return (
            f"host {host_header!r} is not this machine. Loopback-only "
            f"instances answer loopback hostnames; add an entry to "
            f"{ENV_ALLOWED_HOSTS} if this hostname is legitimate."
        )

    return None


def header_hint() -> List[str]:
    """``curl``-ready header pair, for the README and for 403 bodies."""
    return [f"{REQUEST_HEADER}: {REQUEST_HEADER_VALUE}"]

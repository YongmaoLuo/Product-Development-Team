"""No production code may bind a wildcard address.

Why this gate exists
--------------------
The backend's HTTP surface has **no authentication**. Every endpoint
is reachable by anyone who can reach the socket, and the endpoints are
not read-only: they spawn subprocesses, write files, start executions
and shell out to agent CLIs.

`server.py` used to call ``uvicorn.run(app, host="0.0.0.0", ...)`` and
probe for a free port with ``s.bind(("0.0.0.0", candidate))``. On a
laptop joined to a cafe/office/coworking network that publishes the UI,
unauthenticated, to every other device on it — with the operator having
chosen nothing. ``lsof`` shows ``TCP *:8010`` rather than
``127.0.0.1:8010``.

The default is now loopback (``config_paths.DEFAULT_SERVER_HOST``), and
an operator who genuinely wants LAN access — a container, a VM, a
deliberately shared box — opts in with ``PDT_HOST``. Exposure has to be
asked for.

Why a *gate* and not just the fix
---------------------------------
"bind everything" is the default thing to write; it is one word shorter
than the safe version and it works perfectly on the developer's own
machine, so nothing local ever complains. Three lines were easy to fix;
keeping them fixed is the part that needs a test.

Why AST rather than a grep for the literal
------------------------------------------
Recognising a wildcard is not the same as binding one, and code
sometimes needs the former — a helper that normalises a probe target has
to ask "is this host the wildcard?". A literal-scanning version of this
gate flagged exactly that line on the day it was written. A gate that
cries wolf gets allowlisted, and an allowlisted gate protects nothing,
so this one looks only at the two places where a wildcard actually
becomes reachable: a socket ``.bind(...)`` call and a ``host=`` keyword
argument (``uvicorn.run``, ``http.server``, ...).

A wildcard reaching a bind through an intermediate variable is not
caught — that is the deliberate cost of not flagging comparisons. The
behavioural half (``resolve_server_host`` defaults to loopback) covers
the path production actually takes.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import config_paths  # noqa: E402

WILDCARD = "0.0.0.0"

#: Directories that are not production source.
_SKIP_DIRS = {"tests", "__pycache__", ".venv", "node_modules", "logs"}

#: Files allowed to bind a wildcard, each with a reason. Empty, and it
#: should stay that way: a wildcard bind in production source is the
#: thing this gate forbids. If a genuine exception ever appears, add an
#: entry here *and* explain why that endpoint is safe to expose — an
#: unexplained allowlist entry is how this gate would quietly stop
#: working.
ALLOWED: dict[str, str] = {}


def _production_files():
    for path in sorted(BACKEND_DIR.rglob("*.py")):
        rel = path.relative_to(BACKEND_DIR)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        yield rel, path


def _binds_wildcard(node: ast.AST) -> bool:
    """True when ``node`` is a value that carries the wildcard literal.

    Covers a bare string and a tuple/collection containing one — the two
    shapes an address argument takes.
    """
    if isinstance(node, ast.Constant):
        return node.value == WILDCARD
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_binds_wildcard(e) for e in node.elts)
    return False


def _wildcard_bind_sites(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, description)`` for each reachable wildcard bind."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # s.bind(("0.0.0.0", port)) / socket.bind(...)
        if isinstance(func, ast.Attribute) and func.attr == "bind":
            if any(_binds_wildcard(arg) for arg in node.args):
                found.append((node.lineno, "bind(...)"))
        # uvicorn.run(app, host="0.0.0.0") and friends
        for kw in node.keywords:
            if kw.arg == "host" and _binds_wildcard(kw.value):
                found.append((node.lineno, "host=..."))
    return found


def test_no_production_module_binds_a_wildcard_address():
    offenders = []
    for rel, path in _production_files():
        if str(rel) in ALLOWED:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            sites = _wildcard_bind_sites(source)
        except SyntaxError:  # pragma: no cover - a broken file is another test's problem
            continue
        for lineno, what in sites:
            offenders.append(f"{rel}:{lineno}: {what}")

    assert not offenders, (
        "production code must not bind a wildcard address — the HTTP "
        "surface is unauthenticated, so 0.0.0.0 publishes every endpoint "
        "(including the ones that spawn subprocesses) to the whole "
        "network, with the operator having opted into nothing.\n"
        "Use config_paths.resolve_server_host() and let the operator set "
        "PDT_HOST if they really want LAN exposure.\n"
        "Offending site(s):\n  " + "\n  ".join(offenders)
    )


def test_the_gate_recognises_the_shapes_it_is_meant_to_catch():
    """A gate that cannot fail is indistinguishable from a passing one.

    Cheaper than injecting a file into the tree: drive the scanner with
    the exact source shapes it exists to reject.
    """
    assert _wildcard_bind_sites('s.bind(("0.0.0.0", 80))')
    assert _wildcard_bind_sites("uvicorn.run(app, host='0.0.0.0', port=1)")
    assert _wildcard_bind_sites('sock.bind(("0.0.0.0", port))')
    # ...and does NOT fire on merely comparing against the wildcard,
    # which is how the probe helper legitimately normalises its target.
    assert not _wildcard_bind_sites('if host not in ("0.0.0.0", "::", ""): pass')
    assert not _wildcard_bind_sites('uvicorn.run(app, host="127.0.0.1")')


# ---------------------------------------------------------------------------
# Behavioural half
# ---------------------------------------------------------------------------


def test_the_resolved_host_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("PDT_HOST", raising=False)
    assert config_paths.resolve_server_host() == "127.0.0.1"


def test_the_default_constant_is_loopback():
    """The constant is the thing a future edit would be tempted to widen."""
    assert config_paths.DEFAULT_SERVER_HOST == "127.0.0.1"


def test_an_operator_can_opt_into_a_wider_bind(monkeypatch):
    """Exposure must remain *possible* — just never accidental.

    A container or VM deployment legitimately needs this, so the escape
    hatch is part of the contract, not a loophole.
    """
    monkeypatch.setenv("PDT_HOST", "0.0.0.0")
    assert config_paths.resolve_server_host() == "0.0.0.0"


def test_the_host_is_reread_on_every_call(monkeypatch):
    """Same guarantee the other resolvers make: setting the env var
    after import must still take effect, so an operator flipping it (or
    a test) is not pinned to an import-time snapshot."""
    monkeypatch.delenv("PDT_HOST", raising=False)
    assert config_paths.resolve_server_host() == "127.0.0.1"
    monkeypatch.setenv("PDT_HOST", "10.0.0.7")
    assert config_paths.resolve_server_host() == "10.0.0.7"

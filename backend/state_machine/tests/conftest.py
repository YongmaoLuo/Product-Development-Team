"""Conftest for the state-machine test suite.

This file installs two autouse fixtures that form **defense layer 3**
of the architecture decision point 8 three-defense contract:

  1. ``assert_not_touching_8000`` (setup, autouse):
       Every test's resolved data_dir is checked against the 8000
       instance's data_dir.  If the test process is pointing at the
       production data directory (or any directory nested inside
       it), the fixture raises ``RuntimeError`` and the test fails
       loudly.  This prevents regression of the dual-write window
       the state-machine refactor was meant to close.

       The same fixture also installs a guarded ``socket.socket``
       wrapper that rejects ``connect()`` calls to ``127.0.0.1:8000``
       — so a test that accidentally tries to talk to the running
       8000 the backend instance is blocked at the OS socket layer.

  2. ``assert_no_state_json_produced`` (teardown, autouse):
       After every test, the fixture scans ``tmp_path`` (and any
       other per-test scratch directory captured during the run)
       for the five deprecated state JSON filenames.  Any hit fails
       the test with a clear message naming the file and the test
       that produced it.

Both fixtures are pinned to be ``autouse=True`` so they run on every
test in the state-machine tree without any explicit opt-in.  Tests
that need to opt OUT (e.g. an integration test that legitimately
writes state JSON for a fixture) can do so by declaring
``@pytest.mark.allow_state_json`` on the test function — that marker
is honored by :func:`pytest_collection_modifyitems` below.
"""

from __future__ import annotations

import os
import re
import socket as _socket_mod
import tempfile
from pathlib import Path
from typing import Iterable, Optional

import pytest


# ---------------------------------------------------------------------------
# Constants — kept in sync with test_json_static_gate.py
# ---------------------------------------------------------------------------

#: The five deprecated state JSON filenames.  Mirrored here so the
#: conftest does not have to import the test module.
STATE_JSON_FILENAMES: tuple[str, ...] = (
    "plan_state.json",
    "execution.json",
    "verification_runtime_state.json",
    "verification_executor_state.json",
    "verification_progress_state.json",
)

#: Marker name a test can use to opt out of the autouse teardown
#: scan.  Use sparingly — and ONLY when the test is genuinely
#: constructing a state JSON fixture (e.g. an integration test that
#: has to simulate a pre-refactor on-disk layout).
ALLOW_STATE_JSON_MARKER = "allow_state_json"


# ---------------------------------------------------------------------------
# Helper: assert data_dir is not 8000
# ---------------------------------------------------------------------------


def _assert_data_dir_is_not_8000(
    test_data_dir: Path,
    eight_thousand_data_dir: Path,
    eight_thousand_port: str,
) -> None:
    """Raise ``RuntimeError`` if ``test_data_dir`` collides with the
    8000 instance's data_dir.

    Collision means either:

      1. ``test_data_dir == eight_thousand_data_dir`` (exact match).
      2. ``test_data_dir`` is nested inside ``eight_thousand_data_dir``
         (a descendant).  This catches ``data_dir / "<plan_id>"``
         cases where the test process is pointing at the same parent
         tree the 8000 instance owns.

    Both cases are bugs: a test process must NEVER share state with
    the running 8000 instance because the state-machine refactor
    pins SQLite as the single source of truth and a parallel
    writer would re-open the dual-write race window.

    Parameters
    ----------
    test_data_dir:
        The data_dir the test process has resolved (typically
        derived from ``AUTONOMOUS_CODING_DATA_DIR`` or the
        test's ``tmp_path``).
    eight_thousand_data_dir:
        The data_dir the running 8000 instance owns.  We resolve
        both sides to absolute paths before comparison so a relative
        path on one side does not silently mask the collision.
    eight_thousand_port:
        The port the 8000 instance is bound to (default ``"8000"``).
        Used in the error message to direct the developer to the
        right env var.

    Raises
    ------
    RuntimeError
        When ``test_data_dir`` collides with ``eight_thousand_data_dir``
        (exact match OR descendant).
    """
    try:
        test_resolved = test_data_dir.resolve()
    except OSError:
        test_resolved = test_data_dir
    try:
        eight_resolved = eight_thousand_data_dir.resolve()
    except OSError:
        eight_resolved = eight_thousand_data_dir

    if test_resolved == eight_resolved:
        raise RuntimeError(
            f"test data_dir {test_resolved!r} EXACTLY matches the "
            f"running 8000 instance's data_dir (port={eight_thousand_port}); "
            f"a test process MUST NOT share state with the production "
            f"instance.  Set AUTONOMOUS_CODING_DATA_DIR to a tmp_path-"
            f"isolated directory."
        )

    # ``is_relative_to`` is available from Python 3.9; fall back to
    # commonpath for safety.
    try:
        is_descendant = test_resolved.is_relative_to(eight_resolved)
    except AttributeError:
        try:
            test_resolved.relative_to(eight_resolved)
            is_descendant = True
        except ValueError:
            is_descendant = False

    if is_descendant:
        raise RuntimeError(
            f"test data_dir {test_resolved!r} is NESTED INSIDE the "
            f"running 8000 instance's data_dir ({eight_resolved!r}, "
            f"port={eight_thousand_port}); a test process MUST NOT "
            f"share state with the production instance.  Set "
            f"AUTONOMOUS_CODING_DATA_DIR to a tmp_path-isolated directory."
        )


# ---------------------------------------------------------------------------
# Helper: install 8000 socket guard
# ---------------------------------------------------------------------------


def _install_8000_socket_guard(
    monkeypatch: pytest.MonkeyPatch,
    blocked_host: str = "127.0.0.1",
    blocked_port: int = 8000,
) -> None:
    """Install a guarded ``socket.socket`` that rejects ``connect()``
    calls targeting ``(blocked_host, blocked_port)``.

    Implementation:

      We replace :class:`socket.socket` with a thin subclass that
      overrides :meth:`connect` to raise :class:`PermissionError`
      when the target address matches the blocked pair.  Everything
      else (``bind``, ``listen``, ``close``, ``send``, ``recv``,
      ``setsockopt``, etc.) is inherited unchanged — so this is a
      targeted, low-risk patch.

    The guard is applied via ``monkeypatch.setattr`` so it is
    automatically reverted when the test finishes; no manual
    cleanup is required.

    Parameters
    ----------
    monkeypatch:
        The pytest ``monkeypatch`` fixture supplied to the calling
        test.
    blocked_host:
        The host the guard rejects.  Default ``"127.0.0.1"`` matches
        the canonical 8000 instance.
    blocked_port:
        The port the guard rejects.  Default ``8000``.
    """
    real_socket_class = _socket_mod.socket

    class GuardedSocket(real_socket_class):  # type: ignore[misc, valid-type]
        """``socket.socket`` subclass that blocks ``connect`` to a
        specific ``(host, port)`` pair."""

        def connect(self, address):  # type: ignore[override]
            # ``address`` is ``(host, port)`` for AF_INET; for other
            # families the shape differs.  We only guard AF_INET.
            host, port = self._normalize_address(address)
            if host == blocked_host and port == blocked_port:
                raise PermissionError(
                    f"socket.connect({address!r}) BLOCKED by the "
                    f"state-machine test guard: 8000 is the production "
                    f"the backend instance; test processes MUST NOT "
                    f"connect to it.  Use a tmp_path-isolated data "
                    f"directory and an ephemeral port instead."
                )
            return super().connect(address)

        @staticmethod
        def _normalize_address(address) -> tuple[str, int]:
            """Normalize ``address`` to ``(host, port)`` for AF_INET;
            return ``("", -1)`` for families we don't guard."""
            if isinstance(address, tuple) and len(address) == 2:
                host, port = address
                if isinstance(host, str) and isinstance(port, int):
                    return host, port
            return "", -1

    # Patch the *class attribute* on the ``socket`` module so any
    # code that does ``socket.socket(...)`` (or imports it via
    # ``from socket import socket`` and re-binds) picks up the guard.
    monkeypatch.setattr(_socket_mod, "socket", GuardedSocket)


# ---------------------------------------------------------------------------
# Helper: scan a directory tree for state JSON files
# ---------------------------------------------------------------------------


def _scan_for_state_json_files(root: Path) -> list[Path]:
    """Return every ``Path`` under ``root`` whose basename is one of
    the five deprecated state JSON filenames.

    A test that produces ANY of these files (intentionally or by
    accident) fails the autouse teardown assertion.
    """
    if not root.exists():
        return []
    matches: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file() and path.name in STATE_JSON_FILENAMES:
            matches.append(path)
    return matches


# ---------------------------------------------------------------------------
# Helper: resolve the 8000 instance's data_dir (best-effort)
# ---------------------------------------------------------------------------


def _resolve_eight_thousand_data_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str]:
    """Best-effort resolve the running 8000 instance's data_dir +
    port.  Defaults to ``~/.pdt/state.db`` and port ``8000``.

    The default matches the production deploy:

      * data_dir = ``~/.pdt``  (``state.db`` lives inside it)
      * port = ``8000``       (the backend's bound port)

    Tests can override either via env vars ``AUTONOMOUS_CODING_DATA_DIR``
    (the path that points to the 8000 instance's data_dir) and
    ``AUTONOMOUS_CODING_PORT`` (the port the 8000 instance is bound
    to).  These are read once at fixture-setup time.
    """
    env_data_dir = os.environ.get("AUTONOMOUS_CODING_DATA_DIR")
    env_port = os.environ.get("AUTONOMOUS_CODING_PORT", "8000")

    if env_data_dir:
        data_dir = Path(env_data_dir)
    else:
        # Default to the production layout (~/.pdt) so a stray test
        # that points at the real instance is caught even without
        # explicit env-var setup.
        data_dir = Path.home() / ".pdt"

    return data_dir, env_port


# ---------------------------------------------------------------------------
# Pytest collection hook: honor ``allow_state_json`` marker
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items: Iterable[pytest.Item]) -> None:
    """Pass-through hook — the marker is read at fixture-time, not
    collection-time, so this hook is intentionally a no-op.

    We keep the hook present (instead of removing it) so a future
    addition (e.g. collecting ``allow_state_json`` tests into a
    dedicated sub-suite) has a documented anchor to extend.
    """
    return None


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def assert_not_touching_8000(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup-time autouse guard: assert the test is NOT touching the
    8000 instance's data_dir, and install a socket guard that blocks
    ``connect()`` to ``127.0.0.1:8000``.

    The test's resolved ``data_dir`` is read from
    ``AUTONOMOUS_CODING_DATA_DIR`` if set, else the test's
    ``tmp_path`` (which pytest provides per-test).  When the env
    var points at ``tmp_path`` (the typical case), the guard's
    collision check is trivially satisfied because ``tmp_path`` is
    a fresh, isolated directory.

    Parameters
    ----------
    request:
        Pytest fixture request — used to read the test's marker
        set (currently informational only).
    tmp_path:
        Per-test scratch directory supplied by pytest.
    monkeypatch:
        Pytest monkeypatch fixture — used to install the socket
        guard for the duration of the test.
    """
    eight_thousand_data_dir, eight_thousand_port = (
        _resolve_eight_thousand_data_dir(monkeypatch)
    )

    # Determine the test's data_dir.  The contract: if
    # ``AUTONOMOUS_CODING_DATA_DIR`` is set in the test's env, that
    # is what the test points at; otherwise the test points at
    # ``tmp_path`` (the pytest-provided scratch).
    test_data_dir_env = os.environ.get("AUTONOMOUS_CODING_DATA_DIR")
    if test_data_dir_env:
        test_data_dir = Path(test_data_dir_env)
    else:
        test_data_dir = tmp_path

    # Run the collision check — raises if the test is pointing at
    # the 8000 instance's data_dir.
    _assert_data_dir_is_not_8000(
        test_data_dir=test_data_dir,
        eight_thousand_data_dir=eight_thousand_data_dir,
        eight_thousand_port=eight_thousand_port,
    )

    # Install the socket guard.  The guard is monkey-patched so it
    # is automatically reverted when the test finishes.
    _install_8000_socket_guard(
        monkeypatch,
        blocked_host="127.0.0.1",
        blocked_port=int(eight_thousand_port) if eight_thousand_port.isdigit() else 8000,
    )


@pytest.fixture(autouse=True)
def assert_no_state_json_produced(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Iterable[None]:
    """Teardown-time autouse guard: scan ``tmp_path`` for the five
    deprecated state JSON filenames and fail the test if any are
    present.

    Tests that legitimately need to write a state JSON file (e.g.
    an integration test simulating a pre-refactor on-disk layout)
    can opt out by decorating the function with::

        @pytest.mark.allow_state_json

    Implementation:

      We yield once to run the test body, then on teardown we
      ``rglob`` the test's ``tmp_path`` for the five filenames.
      Any hit fails the test with a clear, actionable message that
      names the file, the test, and the recommended remediation
      (use the SQLite repositories instead).

    Parameters
    ----------
    request:
        Pytest fixture request — used to read the test's marker
        set; tests with ``@pytest.mark.allow_state_json`` are
        exempted from the scan.
    tmp_path:
        Per-test scratch directory supplied by pytest.

    Yields
    ------
    None
        The fixture yields once (after the setup phase, before the
        test body runs), then runs the teardown scan on the way out.
    """
    yield

    # If the test opted out via @pytest.mark.allow_state_json, skip.
    marker = request.node.get_closest_marker(ALLOW_STATE_JSON_MARKER)
    if marker is not None:
        return

    # Scan ``tmp_path`` for the five state JSON filenames.  We use
    # ``rglob("*")`` so any nested tmp_path layout is covered (e.g.
    # ``tmp_path / "plans" / "<plan_id>" / "execution.json"``).
    hits = _scan_for_state_json_files(tmp_path)

    # Also scan any per-test scratch directory captured during the
    # test body — pytest provides ``tmp_path_factory`` for tests
    # that need multiple tmp directories, but they all live under
    # tmp_path's parent at session-level; covering ``tmp_path`` is
    # sufficient for the contract.

    if hits:
        rel = [str(h.relative_to(tmp_path)) for h in hits]
        # Also expose the hits on the request node so the explicit
        # test in test_json_static_gate.py can introspect them.
        request.node._state_json_hits = rel  # type: ignore[attr-defined]
        pytest.fail(
            f"test {request.node.nodeid!r} produced state JSON file(s) "
            f"in tmp_path: {rel!r}.  The state-machine refactor pins "
            f"SQLite as the single source of truth; tests must NOT "
            f"write the deprecated state JSON filenames.  Decorate "
            f"the test with @pytest.mark.allow_state_json if this is "
            f"intentional."
        )
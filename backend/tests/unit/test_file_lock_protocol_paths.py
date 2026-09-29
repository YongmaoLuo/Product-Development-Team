"""Where the workspace-derived lock state lands (2026-09-28).

``file_lock_protocol`` derives two machine-wide locations from a workspace
digest rather than from the plan directory:

* ``fallback_locks_dir`` — for callers with no plan directory to sit
  beside, and
* ``socket_path`` — the broker socket, kept short because ``AF_UNIX``
  paths are capped.

"Derived from the workspace" is what makes every process on the machine
agree without being told, and it is also why the default root
accumulates: one directory per workspace the machine has ever run
against. A test suite that gives every case its own ``tmp_path`` therefore
mints one per case, in the machine's temp root, for workspaces deleted
seconds later. ``PDT_LOCK_ROOT`` is the way out — same shape as
``PDT_PLANS_DIR`` / ``PDT_STATE_DB_PATH``.

The socket-path length check below is not incidental. Nested under the
suite's state directory the derived socket path measures 108 characters,
over macOS's 104-byte ``sun_path`` cap, and ``bind()`` fails with
"AF_UNIX path too long" — the lock layer silently gone, which is the
failure ``socket_path``'s own docstring exists to warn about. A redirect
root that is merely short *enough for the prefix* is not enough.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from file_lock_protocol import (  # noqa: E402
    LOCK_ROOT_ENV_VAR,
    fallback_locks_dir,
    socket_path,
)

#: macOS caps ``sun_path`` at 104 bytes; Linux at 108. The stricter of
#: the two is the one to design against.
_AF_UNIX_PATH_CAP = 104


def test_the_default_root_is_the_system_temp_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset means machine-wide, which is the production contract."""
    monkeypatch.delenv(LOCK_ROOT_ENV_VAR, raising=False)
    assert fallback_locks_dir("/tmp/ws").parent == Path(tempfile.gettempdir())
    assert socket_path("/tmp/ws").parent == Path(tempfile.gettempdir())


def test_the_override_moves_both_derivations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """One variable governs the lock directory *and* the socket.

    Both are workspace-derived and both land in the machine temp root, so
    a harness that redirected only one would still be seeding the other.
    """
    monkeypatch.setenv(LOCK_ROOT_ENV_VAR, str(tmp_path))
    assert fallback_locks_dir("/tmp/ws").parent == tmp_path
    assert socket_path("/tmp/ws").parent == tmp_path


def test_the_override_is_read_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A fixture that sets it after import must still take effect.

    ``conftest`` sets the variable at import time, but a test that points
    it elsewhere for one case has to work too — caching the root at import
    would make the variable a lie for everything imported earlier.
    """
    monkeypatch.delenv(LOCK_ROOT_ENV_VAR, raising=False)
    default_first = fallback_locks_dir("/tmp/ws")
    monkeypatch.setenv(LOCK_ROOT_ENV_VAR, str(tmp_path))
    assert fallback_locks_dir("/tmp/ws") != default_first
    assert fallback_locks_dir("/tmp/ws").parent == tmp_path


def test_an_empty_override_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PDT_LOCK_ROOT=""`` means unset, not "root at the empty path".

    An empty value would otherwise resolve to the current working
    directory — a lock directory inside whatever tree the process happens
    to be in, which is the one placement this must never produce.
    """
    monkeypatch.setenv(LOCK_ROOT_ENV_VAR, "")
    assert fallback_locks_dir("/tmp/ws").parent == Path(tempfile.gettempdir())


def test_the_derived_socket_path_fits_the_af_unix_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The redirect root has to be short enough for the socket *inside* it.

    Pinned as a length assertion rather than a live ``bind()`` because the
    failure is an ``OSError`` at bind time, far from the constant that
    caused it — and because the natural first choice (a subdirectory of
    the suite's existing state dir) is over the cap.
    """
    monkeypatch.delenv(LOCK_ROOT_ENV_VAR, raising=False)
    derived = socket_path("/tmp/some-workspace")
    assert len(str(derived)) < _AF_UNIX_PATH_CAP, (
        f"derived socket path is {len(str(derived))} chars, over the "
        f"{_AF_UNIX_PATH_CAP}-byte sun_path cap: {derived}"
    )


def test_the_suite_redirects_the_lock_root() -> None:
    """Asserted from a test rather than trusted to conftest.

    Without the redirect every case's ``tmp_path`` workspace mints a lock
    directory in the machine's temp root, and nothing collects it —
    deleting the line would silently re-open that, so its presence is
    pinned.
    """
    import os

    assert os.environ.get(LOCK_ROOT_ENV_VAR), (
        "the suite is deriving lock state under the machine's temp root; "
        "each case's tmp_path workspace would leave a directory behind"
    )

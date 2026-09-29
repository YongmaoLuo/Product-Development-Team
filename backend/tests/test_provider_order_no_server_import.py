"""``provider_order._default_order_file()`` must not import ``server``.

Root cause of the 2026-09-14 swallowed-SIGTERM incident: the old body
did a lazy ``from server import PROVIDER_ORDER_FILE``, and
``provider_order`` is reached from ``server._lifespan`` →
``load_providers()`` → ``load_fallback_order()`` on every boot. Under
``python -m backend.server`` the server body is already running as
``__main__``, so that import executed the whole ~10k-line module a
*second* time under the name ``server`` — roughly a second after
``uvicorn.run()``, i.e. after uvicorn had installed ``Server.handle_exit``
for SIGTERM. The duplicate body's module-level
``signal.signal(SIGTERM, _signal_handler)`` replaced uvicorn's handler,
so the signal stopped shutting the server down.

``config_paths`` is a leaf utility that already exposes the identical
constant (documented there as "mirroring ``server.PROVIDER_ORDER_FILE``")
without dragging in FastAPI. ``_default_order_file`` now uses it, and
only consults ``server`` when that module happens to already be loaded.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import provider_order  # noqa: E402 — pytest rootdir puts backend/ on sys.path
import server  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.mark.unit
class TestProviderOrderDoesNotImportServer:
    def test_default_order_file_does_not_import_server(self):
        """Needs a subprocess: this test session has already imported
        ``server`` itself.

        The ``find_spec`` guard is load-bearing. Without a sys.path on
        which ``server`` is importable, the pre-fix ``from server import
        PROVIDER_ORDER_FILE`` blows up, the surrounding ``except
        Exception`` swallows it, and the test reports "did not import
        server" for entirely the wrong reason — verified while the fix
        was stashed.
        """
        code = (
            "import importlib.util, sys; "
            "assert importlib.util.find_spec('server') is not None, "
            "'server must be importable for this assertion to mean anything'; "
            "import provider_order; "
            "provider_order._default_order_file(); "
            "print(int('server' in sys.modules))"
        )
        # Same two entries the real ``-m backend.server`` launch gets:
        # the repo root (for ``from backend.framework...``) and
        # ``backend/`` (for the flat ``import server``).
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(BACKEND_DIR), str(BACKEND_DIR.parent), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)

        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "0", (
            f"_default_order_file() imported server.py: {proc.stdout!r}"
        )

    def test_default_order_file_prefers_an_already_loaded_server(
        self, tmp_path, monkeypatch
    ):
        """Backward compatibility: callers that boot the server normally
        — and tests that patch ``server.PROVIDER_ORDER_FILE``, e.g.
        ``tests/test_server_providers.py`` — must keep seeing the
        server's snapshot rather than a re-resolved one."""
        override = tmp_path / "order.json"
        monkeypatch.setattr(server, "PROVIDER_ORDER_FILE", override)

        assert provider_order._default_order_file() == override

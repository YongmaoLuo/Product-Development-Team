"""``provider_order`` must not import ``server``.

Root cause of the 2026-09-14 swallowed-SIGTERM incident: the body did a
lazy ``from server import ...``, and ``provider_order`` is reached from
``server._lifespan`` → ``load_providers()`` → ``load_fallback_order()``
on every boot. Under ``python -m backend.server`` the server body is
already running as ``__main__``, so that import executed the whole
~10k-line module a *second* time under the name ``server`` — roughly a
second after ``uvicorn.run()``, i.e. after uvicorn had installed
``Server.handle_exit`` for SIGTERM. The duplicate body's module-level
``signal.signal(SIGTERM, _signal_handler)`` replaced uvicorn's handler,
so the signal stopped shutting the server down.

``config_paths`` is a leaf utility that declares the path without
dragging in FastAPI, and ``_default_order_file`` goes through it. There
is no fallback to ``server`` for it to fall back *to*.

The chain itself no longer touches the filesystem at all, so this is
narrower than it was: what remains is the 5-hour quota payload's
default path. The property still has to hold for the whole module —
``load_fallback_order`` is on the boot path, and any import of ``server``
from here would do the same damage whatever it was importing.
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

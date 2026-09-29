"""
Backend Module
==============

Backend services and business logic.

Shim-friendly sys.path bootstrap
--------------------------------

The backend package historically runs with ``cwd=backend/`` so that
top-level imports such as ``from coding_tool import ...`` resolve
against the package directory. Several spec-mandated verification
points (notably VP-035 - "shim-compatible legacy import paths still
work") invoke the package from the project root, e.g.::

    python3 -c "from backend.server import app; ..."

In that mode ``sys.path[0]`` is the project root, so ``backend/server.py``
cannot find ``backend/coding_tool.py`` via a top-level import. To keep
the legacy import surface (``backend.server`` / ``backend.agent`` /
``backend.verification_agent``) working without rewriting every
``from coding_tool import ...`` to a relative import, this package
init ensures the backend directory itself is on ``sys.path`` before
any submodule runs its module-level imports. The insertion is
idempotent (a path already on ``sys.path`` is skipped) and runs
only once, so importing the package from any entry point - pytest,
uvicorn, or a bare ``python3 -c`` - produces a usable environment.
"""

from __future__ import annotations

import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

__version__ = "0.1.0"

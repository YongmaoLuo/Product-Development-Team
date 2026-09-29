"""Extracted modules must not derive paths from their own ``__file__``.

Why this gate exists
--------------------
On 2026-09-25 the workflow routes moved out of ``server.py`` into
``backend/routes/`` (and the verification loop into ``backend/verification_loop.py``).
Three path guards moved with them and silently changed meaning, because
``Path(__file__).parent`` — "the directory I live in" — is no longer the
backend root once a file lives one level down:

  * ``routes/execution.py`` computed ``_self_root = Path(__file__).parent.parent``
    for the "never execute inside ``backend/``" refusal. From ``backend/routes/``
    that is ``backend/``, so the forbidden directory became ``backend/backend``
    and **the refusal stopped firing** — measured: ``POST
    /api/execution/{id}/start`` with ``project_dir=<repo>/backend`` answered
    ``200 started`` and spawned an executor pointed at the running server's own
    source tree;
  * ``routes/phases.py`` derived ``formal_repo_dir`` the same way, and
    ``validate_workspace`` routes tasks *away* from that directory — so it
    started treating ``backend/`` as the formal repo.

Neither is a typo, and neither fails loudly: both keep producing a
well-formed absolute path, just the wrong one, and both fail only for inputs
the suite did not happen to use. That is the reason this is a gate rather
than three one-line fixes.

The rule
--------
Application-root paths are defined once, in ``server.py``
(``_PROJECT_ROOT`` / ``_BACKEND_DIR``), and extracted modules reach them
through the module object: ``_server._PROJECT_ROOT``. A module that lives in
``backend/`` is free to keep using its own ``__file__`` — the point is that
the *answer* must not depend on where the file happens to sit.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_APP_ROOT = _BACKEND_DIR.parent


def _extracted_modules() -> list[Path]:
    """Every module that was pulled out of ``server.py``."""
    modules = sorted((_BACKEND_DIR / "routes").glob("*.py"))
    loop = _BACKEND_DIR / "verification_loop.py"
    if loop.exists():
        modules.append(loop)
    return modules


def _uses_dunder_file(tree: ast.Module) -> list[int]:
    """Lines where ``__file__`` is read."""
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "__file__"
    )


@pytest.mark.parametrize("path", _extracted_modules(), ids=lambda p: p.name)
def test_extracted_module_does_not_read_its_own_dunder_file(path: Path) -> None:
    """No extracted module may build a path from ``__file__``."""
    lines = _uses_dunder_file(ast.parse(path.read_text(encoding="utf-8")))
    assert not lines, (
        f"{path.name} reads ``__file__`` at line(s) {lines}. An extracted module "
        f"lives one directory deeper than ``server.py``, so any root derived "
        f"that way points at the wrong place while still looking correct — the "
        f"``backend/`` execution refusal and the ``formal_repo_dir`` workspace "
        f"guard both broke exactly this way. Use ``server._PROJECT_ROOT`` (the "
        f"repository root) or ``server._BACKEND_DIR`` instead."
    )


def test_the_shared_roots_resolve_to_the_application() -> None:
    """The constants the extracted modules rely on point where they claim."""
    import server

    assert server._PROJECT_ROOT == _APP_ROOT.resolve(), (
        "server._PROJECT_ROOT must be the repository root (the directory "
        "holding ``backend/``); workspace routing treats it as the formal repo"
    )
    assert server._BACKEND_DIR == _BACKEND_DIR.resolve(), (
        "server._BACKEND_DIR must be ``backend/`` — it is the directory "
        "POST /api/execution/{id}/start refuses to execute inside"
    )
    assert server._BACKEND_DIR == server._PROJECT_ROOT / "backend"

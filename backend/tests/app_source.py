"""The application's own source, for the gates that pin it statically.

Several contracts in this suite cannot be observed from the running app —
"which literal hangs off which call", "which closure imports what", "does
this branch still exist" — so the tests read the source instead. Until
2026-09-25 that source *was* ``server.py``. It is now split:

  * ``backend/server.py``            — app wiring, models, lifespan, shared state
  * ``backend/routes/*.py``          — the HTTP surface (see ``routes/phases.py``)
  * ``backend/verification_loop.py`` — the background verification machinery

A gate that keeps reading ``server.py`` alone does not fail as "the code moved
somewhere I cannot see"; it fails as *"the code is gone"* — which reads like a
real regression, invites deleting the test, and loses the contract. Worse, a
scan that silently matches nothing still passes: ``test_auto_loop_pure_split``
and friends assert on the *absence or presence* of literals, so an empty
haystack is a vacuous green.

Use these helpers rather than reading a file path directly. When more of
``server.py`` is extracted later, the gates follow the code for free.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

#: ``backend/`` — this file lives in ``backend/tests/``.
BACKEND_DIR = Path(__file__).resolve().parents[1]

#: The extracted background module; kept explicit rather than globbed so a
#: missing file is a deliberate change, not a silently shorter haystack.
_NON_ROUTE_MODULES = ("verification_loop.py",)


def app_files() -> list[Path]:
    """Every file the application is defined in, in a stable order."""
    files = [BACKEND_DIR / "server.py"]
    files += sorted((BACKEND_DIR / "routes").glob("*.py"))
    files += [BACKEND_DIR / name for name in _NON_ROUTE_MODULES]
    return [p for p in files if p.exists()]


def app_source() -> str:
    """The application's source, concatenated.

    Concatenation is enough for the literal-level checks (``"x" in src``);
    use :func:`find_def` when the check is scoped to one function, because a
    concatenated blob has no AST of its own.
    """
    return "\n\n".join(p.read_text(encoding="utf-8") for p in app_files())


@dataclass(frozen=True)
class AppDef:
    """A function/class found somewhere in the application's source."""

    path: Path
    tree: ast.Module
    node: ast.AST

    @property
    def source(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def text(self) -> str:
        """The definition's own source segment.

        ``get_source_segment`` starts at the ``def`` line, so decorators are
        not included — the gates that use this look at a function body.
        """
        return ast.get_source_segment(self.source, self.node) or ""

    @property
    def qualname(self) -> str:
        return f"{self.path.name}::{getattr(self.node, 'name', '?')}"


def iter_defs(name: str | None = None) -> Iterator[AppDef]:
    """Every function/class definition, innermost closures included."""
    for path in app_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if name is None or node.name == name:
                yield AppDef(path=path, tree=tree, node=node)


def find_def(name: str) -> AppDef:
    """The first definition called ``name``, or a clear failure.

    Raises ``LookupError`` rather than ``StopIteration``: a bare
    ``next(...)`` in a test reports the miss as a generator exhaustion, which
    says nothing about *what* went missing when the code moves again.
    """
    for found in iter_defs(name):
        return found
    raise LookupError(
        f"{name!r} is not defined in any application module "
        f"({', '.join(p.name for p in app_files())}). If it moved, this gate "
        f"follows the source — check the name, not the file."
    )

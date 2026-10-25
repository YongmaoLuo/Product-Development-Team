"""The application's own source, for the gates that pin it statically.

Several contracts in this suite cannot be observed from the running app —
"which literal hangs off which call", "which closure imports what", "does
this branch still exist" — so the tests read the source instead. Until
2026-09-25 that source *was* ``server.py``. It is now split:

  * ``backend/server.py``            — app wiring, models, lifespan, shared state
  * ``backend/routes/*.py``          — the HTTP surface (see ``routes/phases.py``)
  * ``backend/verification_loop.py`` — the background verification machinery
  * every other production module under ``backend/`` — the packages the
    surface is built from (``hooks/``, ``hitl/``, ``state_machine/``, …)

A gate that keeps reading ``server.py`` alone does not fail as "the code moved
somewhere I cannot see"; it fails as *"the code is gone"* — which reads like a
real regression, invites deleting the test, and loses the contract. Worse, a
scan that silently matches nothing still passes: ``test_auto_loop_pure_split``
and friends assert on the *absence or presence* of literals, so an empty
haystack is a vacuous green.

The production tree is scanned whole, not a curated subset of it.  A gate
that has to answer "which module owns this file?" cannot answer it from a
list of files somebody remembered to add: the answer changes when a module
is added, and a list that is not updated with it answers the previous
question.  The test tree is excluded — a gate that scans the application
must not scan the gates that check the application, or every gate would
report every other gate.

Use these helpers rather than reading a file path directly. When more of
``server.py`` is extracted later, the gates follow the code for free.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

#: ``backend/`` — this file lives in ``backend/tests/``.
BACKEND_DIR = Path(__file__).resolve().parents[1]

#: The extracted background module; kept explicit rather than globbed so a
#: missing file is a deliberate change, not a silently shorter haystack.
_NON_ROUTE_MODULES = ("verification_loop.py",)

#: Directory names under ``backend/`` that never hold application source:
#: the test tree, the interpreter's caches and the virtualenv.
_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {"tests", "__pycache__", ".venv", ".pytest_cache", ".git", "node_modules"}
)


def _production_modules() -> list[Path]:
    """Every production module under ``backend/``, sorted.

    The walk prunes the excluded directories rather than filtering them
    out afterwards, so a virtualenv in the tree does not cost a full
    traversal of its thousands of files.
    """
    modules: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(BACKEND_DIR):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIR_NAMES)
        for name in sorted(filenames):
            if name.endswith(".py"):
                modules.append(Path(dirpath) / name)
    return modules


def app_files() -> list[Path]:
    """Every file the application is defined in, in a stable order.

    The leading files keep the order the gates that read the source as
    one blob already rely on — ``server.py`` first, then the HTTP
    surface, then the extracted background module — and the rest of the
    production tree follows, sorted.  A gate that searches the
    concatenated source therefore finds the same definition it found
    before, and a gate that has to name the file a hit came from can
    name it.
    """
    leading = [BACKEND_DIR / "server.py"]
    leading += sorted((BACKEND_DIR / "routes").glob("*.py"))
    leading += [BACKEND_DIR / name for name in _NON_ROUTE_MODULES]
    leading = [p for p in leading if p.exists()]
    seen = set(leading)
    rest = [p for p in _production_modules() if p not in seen]
    return leading + rest


def app_source() -> str:
    """The application's source, concatenated.

    Concatenation is enough for the literal-level checks (``"x" in src``);
    use :func:`find_def` when the check is scoped to one function, because a
    concatenated blob has no AST of its own.
    """
    return "\n\n".join(p.read_text(encoding="utf-8") for p in app_files())


def app_sources() -> list[tuple[Path, str]]:
    """Every application file paired with its source, in :func:`app_files` order.

    :func:`app_source` is the right shape for a check that asks *does
    this literal appear?* and the wrong shape for a check that has to
    answer *which file did it come from?* — a concatenated blob has no
    file boundaries left.  This is the same source, kept per file.
    """
    return [(path, path.read_text(encoding="utf-8")) for path in app_files()]


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

        ``get_source_segment`` starts at the ``def`` line, so decorators
        are not included — the gates that use this look at a function body.
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
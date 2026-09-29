"""No production module may resolve a module path outside this repository.

Why this gate exists
--------------------
The Telegram mirror used to fetch its transport with

    importlib.import_module("tools.helper.transport")

``tools/`` is operator-owned and gitignored — it ships with nothing. So
on a clean checkout that line raised, the notifier disabled the channel,
and the failure looked exactly like "the operator never configured
Telegram". Two separate harms, one line:

* **It cannot work where it matters.** A dependency that resolves only
  on the machine that happens to own the directory is not a dependency.
* **It is an execution slot.** Whoever can create that file in the
  server's working directory gets arbitrary code run inside the server
  process, with the server's privileges, on the next card push. A module
  path assembled from a string has no owner and no review.

The rule this gate enforces is deliberately narrow and checks only
**string literals**: an import target written as a literal must name
either a module of this application or a standard-library module. That
leaves ordinary ``import x`` statements, ``importlib.import_module(some_var)``
and third-party imports untouched — what it forbids is a hard-coded path
to somewhere that is not part of the repository.

If a legitimate need for a third-party dynamic import appears, add the
import name to ``THIRD_PARTY_ALLOWED`` and say why in the commit.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]

#: Distributions whose *import* name is not standard library and not part
#: of this application. Empty on purpose — nothing needs one today, and an
#: entry here should be argued for rather than assumed.
THIRD_PARTY_ALLOWED: frozenset[str] = frozenset()

_SKIP_DIR_PARTS = {"tests", "__pycache__", ".venv", "venv"}


def _stdlib_module_names() -> frozenset[str]:
    """Return the canonical set of top-level standard-library module names.

    Python 3.10+ ships :data:`sys.stdlib_module_names` — the source of
    truth. The CI PR gate runs a 3.9 venv, where the attribute is
    missing, so we fall back to ``builtin_module_names`` plus every
    ``*.py`` file under the stdlib directory returned by
    :func:`sysconfig.get_path`. The fallback is approximate but
    tight enough for this gate: the only thing the set is used for is
    to *exclude* obviously-stdlib names from the "off-repo import
    target" list, so over-inclusion (i.e. falsely accepting a non-stdlib
    name as stdlib) is the safe direction — the production-files walk
    still reports any non-stdlib, non-app-root literal it finds.
    """
    builtin = sys.builtin_module_names
    stdlib_attr = getattr(sys, "stdlib_module_names", None)
    if stdlib_attr is not None:
        return frozenset(stdlib_attr) | frozenset(builtin)
    import sysconfig
    stdlib_dir = Path(sysconfig.get_path("stdlib")).resolve()
    found: set[str] = set(builtin)
    if stdlib_dir.is_dir():
        for entry in stdlib_dir.iterdir():
            name = entry.stem
            if entry.is_file() and name.endswith(".py"):
                found.add(name)
            elif entry.is_dir() and (entry / "__init__.py").exists():
                found.add(entry.name)
    return frozenset(found)


def _application_roots() -> set[str]:
    """Top-level import names this application provides.

    Every module directly under ``backend/`` plus every package directly
    under it — the set a dotted literal may legitimately start with.
    """
    roots = {p.stem for p in BACKEND_DIR.glob("*.py")}
    roots |= {p.name for p in BACKEND_DIR.iterdir() if p.is_dir()}
    return roots


def _production_files() -> list[Path]:
    return sorted(
        p
        for p in BACKEND_DIR.rglob("*.py")
        if not (_SKIP_DIR_PARTS & set(p.relative_to(BACKEND_DIR).parts))
    )


def _literal_import_targets(tree: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, module_path)`` for every string-literal import target."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute) else None
        )
        if name not in {"import_module", "__import__"}:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append((node.lineno, first.value))
    return found


def test_no_production_module_paths_point_outside_the_repository():
    allowed = _application_roots() | set(_stdlib_module_names()) | THIRD_PARTY_ALLOWED
    offenders: list[str] = []

    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, target in _literal_import_targets(tree):
            root = target.split(".")[0]
            if root not in allowed:
                rel = path.relative_to(BACKEND_DIR.parent)
                offenders.append(f"{rel}:{lineno}: {target}")

    assert not offenders, (
        "string-literal import target(s) outside this repository — a path "
        "that is not part of the checkout cannot be a dependency, and it "
        "turns a module name into an execution slot:\n  "
        + "\n  ".join(offenders)
    )


def test_the_gate_would_catch_the_shape_it_was_written_for():
    """A gate that cannot fail is decoration — feed it the real defect."""
    snippet = ast.parse(
        'import importlib\n'
        'importlib.import_module("tools.helper.transport")\n'
    )
    assert _literal_import_targets(snippet) == [
        (2, "tools.helper.transport")
    ]

    allowed = _application_roots() | set(_stdlib_module_names())
    assert "tools" not in allowed
    assert "notifications" in allowed
    assert "sys" in allowed

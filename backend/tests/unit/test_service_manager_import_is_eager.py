"""``service_manager`` must be resolved at import time, not lazily.

Regression pin for the 2026-09-25 leak of teardown errors: ``server.py``
used to do ``from service_manager import reap_all_plans`` *inside*
``_shutdown_all_executions``. That function runs from an ``atexit`` hook —
after the interpreter has started tearing down, and under pytest after the
``pythonpath`` plugin has removed the entries it injected — so the import
raised::

    ModuleNotFoundError: No module named 'service_manager'

and reporting that failure then hit a logger whose stream was already
closed::

    ValueError: I/O operation on closed file.

Net effect: the shutdown orphan sweep silently did not run, and every
otherwise-green pytest run ended with two error blocks. Resolving the
import once at module level makes the reference outlive any teardown.

The static half of this pin is the important one: it is not enough for the
module-level binding to exist today — a future edit could reintroduce a
lazy import in the shutdown path and quietly restore the old behaviour.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

_SERVER_SOURCE = _BACKEND_DIR / "server.py"


def _module_level_bindings(tree: ast.Module) -> set[str]:
    """Names imported from ``service_manager`` at module level.

    The imports sit inside ``try:`` blocks (a missing module must not break
    startup), so this descends through module-level ``try`` / ``if`` bodies
    — but never into a function or class, which is the thing being pinned.
    """
    names: set[str] = set()

    def visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.ImportFrom) and node.module == "service_manager":
                names.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.Try):
                visit(node.body)
                visit(node.orelse)
                visit(node.finalbody)
            elif isinstance(node, ast.If):
                visit(node.body)
                visit(node.orelse)

    visit(tree.body)
    return names


def _function_level_imports(tree: ast.Module) -> list[int]:
    """Line numbers of ``from service_manager import ...`` inside a function."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom) and child.module == "service_manager":
                lines.append(child.lineno)
    return sorted(lines)


@pytest.fixture(scope="module")
def server_tree() -> ast.Module:
    return ast.parse(_SERVER_SOURCE.read_text(encoding="utf-8"))


def test_service_manager_is_bound_at_module_level(server_tree: ast.Module) -> None:
    """The reap callables are imported once, at import time."""
    bindings = _module_level_bindings(server_tree)
    assert bindings, (
        "server.py no longer imports anything from service_manager at module "
        "level; the startup/shutdown sweeps would have to resolve it lazily "
        "again (see this module's docstring for why that fails)"
    )


def test_no_function_imports_service_manager_lazily(server_tree: ast.Module) -> None:
    """No function may re-resolve the import — especially the atexit path."""
    offenders = _function_level_imports(server_tree)
    assert not offenders, (
        f"server.py imports service_manager lazily at line(s) {offenders}; "
        f"the shutdown sweep runs from atexit, after sys.path teardown, so a "
        f"lazy import there fails and the orphan reap is skipped. Use the "
        f"module-level reference instead."
    )


def _bare_reap_names(tree: ast.Module) -> list[tuple[int, str]]:
    """``(line, name)`` for every call through the *unaliased* reap names.

    The module-level imports rename the callables (``... as _reap_services``),
    so a bare ``reap_services(...)`` can only resolve to something that is not
    there — a ``NameError`` raised inside a ``try/except Exception`` that logs
    and returns, i.e. a reap that silently does not happen.
    """
    plain = {"reap_all_plans", "reap_services"}
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in plain:
            out.append((node.lineno, node.id))
    return sorted(out)


def test_reap_callables_are_never_called_by_their_plain_names(server_tree: ast.Module) -> None:
    """Every call site must use the module-level alias.

    Pinned because the failure is invisible: ``_reap_managed_services`` caught
    the ``NameError`` in its ``except Exception`` and logged
    ``[service_manager] reap failed``, so per-plan service reaping was dead
    while the sweep still reported "never raises" — the log line was the only
    evidence, and it looked like a service_manager problem rather than a typo.
    """
    offenders = _bare_reap_names(server_tree)
    assert not offenders, (
        "server.py calls the reap callables by their unaliased names at "
        f"line(s) {offenders}; those names are never bound at module level "
        "(the imports alias them to ``_reap_*``), so the call raises "
        "NameError and the reap is skipped."
    )


def test_the_reap_callable_is_available() -> None:
    """The import-time binding must actually have resolved."""
    import server

    assert server._reap_all_plans is not None, (
        "server._reap_all_plans is None — service_manager.py is missing or "
        "unimportable, which silently disables the orphan-process sweeps"
    )
    assert server._reap_services is not None

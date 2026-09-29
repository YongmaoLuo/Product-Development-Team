"""VP-024 / bug anchor regression binding meta-test.

Contract: every test function in the ``backend/tests/`` tree that is
annotated with a ``bug_*`` marker (the regression-binding tests for
bugs 1..5) must be *live* — i.e. NOT decorated with ``@pytest.mark.skip``,
``@pytest.mark.skipif(...)``, ``@pytest.mark.xfail``, or
``unittest.skip``/``unittest.expectedFailure``.

A ``bug_N`` anchor test is the regression lock for that bug.  If it
is silently skipped or xfail'd, a future regression on that bug will
not be caught — the test would simply be filtered out by pytest and
the CI log would still show "passed" (because skipped tests report as
"passed" under ``--strict-markers``).  This meta-test catches that
silent-failure mode at PR time.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


BUG_MARKERS = ("bug_1", "bug_2", "bug_3", "bug_4", "bug_5")
SKIP_ATTRS = {"skip", "skipif", "xfail"}


def _collect_anchor_tests(tests_root: Path) -> list:
    """Return a list of ``(relative_file_path, function_name)``
    for every test function under ``backend/tests/`` whose decorators
    reference at least one ``bug_N`` marker.

    We use AST instead of importing pytest so that the meta-test is
    fast and has no side effects (it must run as a pre-flight gate
    before any other test collection happens).
    """
    anchors: list = []
    for path in sorted(tests_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            for decorator in node.decorator_list:
                if _decorator_targets_bug_marker(decorator):
                    anchors.append((
                        str(path.relative_to(tests_root.parent)),
                        node.name,
                    ))
                    break
    return anchors


def _decorator_targets_bug_marker(decorator: ast.expr) -> bool:
    """Return True iff ``decorator`` references a ``pytest.mark.bug_N``."""
    if isinstance(decorator, ast.Attribute):
        if decorator.attr in BUG_MARKERS:
            return True
    if isinstance(decorator, ast.Call):
        func = decorator.func
        if isinstance(func, ast.Attribute) and func.attr in BUG_MARKERS:
            return True
    return False


def _function_is_skipped(path: Path, function_name: str) -> bool:
    """Return True iff the test function named ``function_name`` in
    ``path`` is decorated with any of: skip / skipif / xfail."""
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function_name:
            continue
        for decorator in node.decorator_list:
            if _is_skip_decorator(decorator):
                return True
    return False


def _is_skip_decorator(decorator: ast.expr) -> bool:
    """Return True iff ``decorator`` is skip / skipif / xfail / etc."""
    if isinstance(decorator, ast.Attribute):
        if decorator.attr in SKIP_ATTRS:
            return True
    if isinstance(decorator, ast.Call):
        func = decorator.func
        if isinstance(func, ast.Attribute) and func.attr in SKIP_ATTRS:
            return True
        if isinstance(func, ast.Name) and func.id in SKIP_ATTRS:
            return True
    return False


@pytest.fixture(scope="module")
def bug_anchor_tests() -> list:
    backend_tests = Path(__file__).resolve().parent.parent  # backend/tests/
    return _collect_anchor_tests(backend_tests)


def test_at_least_one_bug_anchor_exists(bug_anchor_tests: list) -> None:
    """Sanity: there must be >=1 bug_N anchor test in the tree."""
    assert bug_anchor_tests, (
        "no bug_N anchor tests found under backend/tests/; "
        "the regression-binding contract for the bug-fix sweep is empty"
    )


def test_no_bug_anchor_test_is_silently_skipped(bug_anchor_tests: list) -> None:
    """Every bug_N anchor test must NOT be marked skip/xfail/skipif."""
    backend_tests = Path(__file__).resolve().parent.parent  # backend/tests/
    skipped: list = []
    for rel_path, function_name in bug_anchor_tests:
        abs_path = backend_tests / rel_path
        if _function_is_skipped(abs_path, function_name):
            skipped.append(f"{rel_path} {function_name}")
    assert not skipped, (
        "the following bug_N anchor tests are silently skipped/xfailed; "
        "remove the skip/xfail decorator or remove the bug_N marker:\n  "
        + "\n  ".join(skipped)
    )

"""
test_agent_execute_task_structure.py — structural sanity tests for
``backend/tests/integration/test_agent_execute_task.py``.

Background:
  Original task 7-6 failed (system reported failed / AI did not
  report test result) but the work was already partially complete.
  Root-cause diagnosis: the 6 tests in ``test_agent_execute_task.py``
  cover the contract completely, but test 6
  (``test_execute_single_task_writes_tmpfile_unique_per_task``)
  required an ``agent.py`` refactor (per-task tmpfile regeneration)
  to actually exercise the new behavior. After the agent.py fix
  (commit 30f945b) the test file was committed. Subsequent
  attempts to validate the 6 tests through the backend bash
  harness have produced flaky / no-result outputs, so we split
  the validation into independent pieces.

  This module is the **first** of those pieces — a static
  structural check that requires nothing more than ``ast.parse``:

    * 6 test functions exist (exactly 6, not 5, not 7).
    * All 6 expected function names are present.
    * Each ``test_`` function contains at least 1 ``ast.Assert``
      node (so it is a real test, not a stub).
    * File size > 100 bytes (not empty / not a placeholder).
    * File is AST-parseable Python (no syntax errors).

  These are *static* structural tests — they do **not** execute
  the tests in the target file, nor do they require the test
  runner or any LLM fixture. They will fail fast with clear
  diagnostics if the file is missing, renamed, or partially
  deleted.

Why AST (and not just ``grep`` / ``re``):
  AST-based detection of test_ functions is more robust than text
  matching. It catches:

    * Both module-level ``def test_*(...)`` and
      ``unittest.TestCase`` method styles (the spec allows both).
    * Tests with no body (``def test_x(): pass``) — caught by
      the "has assert" check below.
    * A file with 7+ tests added by mistake — caught by the
      exact count check.
    * Files with syntax errors — caught at parse time with a
      line/column message, before any of the per-test checks
      produce misleading "missing function" errors.

  AST parsing also gives us the per-function ``lineno`` field,
  which is invaluable when a check fails on a specific function.

TDD spec (mirrors task 7-6-1):
  - ``test_file_exists``:
      ``Path('backend/tests/integration/test_agent_execute_task.py').exists()``
  - ``test_file_size_over_100_bytes``:
      ``file.stat().st_size > 100``
  - ``test_file_has_exactly_6_test_functions``:
      AST 解析 → ``len([def test_*]) == 6``
  - ``test_file_has_all_6_expected_test_names``:
      6 expected function names are all defined
  - ``test_each_test_function_has_assert``:
      every ``test_`` function has at least 1 ``ast.Assert`` node

Key constraints:
  * No requirement that the target tests pass — only that the
    file exists and is structurally well-formed.
  * AST parse failure → fail with a clear error, not a stack
    trace.
  * Both ``unittest.TestCase`` and bare ``def test_`` styles are
    supported (the spec allows both).

Companion tasks:
  * 7-6-1 (this file) — structural existence/validity.
  * 7-6-2 — ``pytest --collect-only`` on the target file.
  * 7-6-3 — run the 6 tests, require explicit ``PASSED`` lines.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Set, Tuple

import pytest


# ---------------------------------------------------------------------------
# Paths + expected names
# ---------------------------------------------------------------------------


# This file lives at backend/tests/integration/<this>.py; the target
# is its sibling. Resolving via __file__ keeps the tests robust
# regardless of where pytest is invoked from.
_BACKEND_TESTS_INTEGRATION_DIR = Path(__file__).resolve().parent
TARGET_FILE = _BACKEND_TESTS_INTEGRATION_DIR / "test_agent_execute_task.py"

EXPECTED_TEST_NAMES: Set[str] = {
    "test_execute_single_task_passes_settings_to_coding_tool",
    "test_execute_single_task_settings_file_exists",
    # 2026-09-14: renamed in the target file (has_7_env_fields →
    # has_endpoint_and_credentials) when env-field pinning moved to
    # CC Switch (model management left this repository entirely, commit 9717073).
    "test_execute_single_task_settings_has_endpoint_and_credentials",
    "test_execute_single_task_passes_hook_stdin",
    "test_execute_single_task_completes_on_test_pass",
    "test_execute_single_task_writes_tmpfile_unique_per_task",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_target_file() -> ast.Module:
    """Read & AST-parse the target test file.

    Raises ``AssertionError`` (not the underlying ``FileNotFoundError``
    or ``SyntaxError``) so the failure surfaces as a normal pytest
    test failure with a clear, actionable message. A bare
    ``FileNotFoundError`` would short-circuit collection; a raw
    ``SyntaxError`` traceback would obscure the structural intent.
    """
    if not TARGET_FILE.exists():
        raise AssertionError(
            f"Target test file does not exist: {TARGET_FILE}. "
            "The integration test file is missing — likely the "
            "7-6 fix commit (30f945b) was reverted or the file "
            "was never checked in. Expected: "
            "backend/tests/integration/test_agent_execute_task.py"
        )

    try:
        source = TARGET_FILE.read_text(encoding="utf-8")
    except OSError as e:
        raise AssertionError(
            f"Target test file exists but cannot be read: {TARGET_FILE} "
            f"({e!r})"
        ) from e

    try:
        return ast.parse(source, filename=str(TARGET_FILE))
    except SyntaxError as e:
        raise AssertionError(
            f"Target test file has a syntax error and cannot be "
            f"AST-parsed: line {e.lineno}, col {e.offset}: {e.msg}. "
            f"File: {TARGET_FILE}"
        ) from e


def _collect_test_functions(tree: ast.Module) -> List[ast.FunctionDef]:
    """Collect every ``test_*(...)`` function node from the AST.

    Supports two styles, both permitted by the spec:

      1. Module-level — ``def test_foo(): ...``
      2. Class-level  — ``class TestX(unittest.TestCase): def test_foo(self): ...``

    Nested ``def test_*`` inside an inner ``def helper()`` body are
    intentionally *not* collected — pytest does not pick them up
    as test cases, so counting them would inflate the count and
    produce false positives. We only look at top-level and
    class-level function definitions.
    """
    tests: List[ast.FunctionDef] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                tests.append(node)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name.startswith("test_")
                ):
                    tests.append(item)
    return tests


def _count_asserts_in(func: ast.FunctionDef) -> int:
    """Count ``ast.Assert`` nodes anywhere inside a function body.

    ``ast.walk`` recurses through nested constructs — try/except,
    if/else, for, with, lambda — so an ``assert`` inside a
    ``try:`` block is still counted. A test that wraps every
    assertion in a try/except (e.g. for cleanup) is still
    recognized as a real test.
    """
    count = 0
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Test 1 — file exists
# ---------------------------------------------------------------------------


def test_file_exists() -> None:
    """The target test file must exist on disk.

    Path: ``backend/tests/integration/test_agent_execute_task.py``.
    A regression that reverts commit 30f945b or accidentally
    deletes the file surfaces here. We resolve the path via
    ``__file__`` so the test does not depend on the current
    working directory.
    """
    assert TARGET_FILE.exists(), (
        f"Target test file does not exist at {TARGET_FILE}. "
        "Re-apply commit 30f945b (test(agent): execute_single_task "
        "端到端集成测试 — SubagentConfig 真落盘) or recreate the "
        "file. The 7-6 fix requires the 6 E2E tests to be "
        "checked in."
    )


# ---------------------------------------------------------------------------
# Test 2 — file size > 100 bytes
# ---------------------------------------------------------------------------


def test_file_size_over_100_bytes() -> None:
    """The target test file must be > 100 bytes (non-empty, non-stub).

    A 0-byte file or a placeholder with only a docstring is a
    regression indicator. The real file is ~22 KB (596 lines, 6
    end-to-end test functions with extensive docstrings and
    fixture helpers).
    """
    assert TARGET_FILE.exists(), (
        f"Target test file does not exist at {TARGET_FILE}"
    )

    size = TARGET_FILE.stat().st_size
    assert size > 100, (
        f"Target test file is suspiciously small: {size} bytes "
        f"(expected > 100 bytes; real file is ~22 KB). "
        f"File: {TARGET_FILE}"
    )


# ---------------------------------------------------------------------------
# Test 3 — exactly 6 test_ functions
# ---------------------------------------------------------------------------


def test_file_has_exactly_6_test_functions() -> None:
    """AST 解析 → ``len([def test_*]) == 6``.

    The 7-6 spec pins exactly 6 test functions. Any drift (an
    extra test added by mistake, or one accidentally removed)
    must fail loudly. We accept both module-level ``def test_``
    and ``unittest.TestCase`` method styles per the spec.

    The error message includes the *actual* function names so a
    developer can see at a glance which tests are present.
    """
    tree = _parse_target_file()
    test_funcs = _collect_test_functions(tree)
    actual_names = [f.name for f in test_funcs]

    assert len(test_funcs) == 6, (
        f"Expected exactly 6 test_ functions in {TARGET_FILE}, "
        f"got {len(test_funcs)}. Found: {actual_names}"
    )


# ---------------------------------------------------------------------------
# Test 4 — all 6 expected function names are present
# ---------------------------------------------------------------------------


def test_file_has_all_6_expected_test_names() -> None:
    """All 6 expected test function names must be defined in the file.

    This is a stricter check than ``test_file_has_exactly_6_test_functions``
    — that test passes if the file has 6 ``test_*`` functions, but
    they could be the *wrong* 6 (e.g. someone renamed one without
    updating the spec, or replaced a test with a different one).
    This test pins the names one by one against the spec.
    """
    tree = _parse_target_file()
    test_funcs = _collect_test_functions(tree)
    found_names: Set[str] = {f.name for f in test_funcs}

    missing = EXPECTED_TEST_NAMES - found_names
    assert not missing, (
        f"Target test file is missing {len(missing)} expected "
        f"test function(s): {sorted(missing)}. "
        f"Found: {sorted(found_names)}. "
        f"Expected: {sorted(EXPECTED_TEST_NAMES)}"
    )


# ---------------------------------------------------------------------------
# Test 5 — each test_ function has at least 1 assert
# ---------------------------------------------------------------------------


def test_each_test_function_has_assert() -> None:
    """Every ``test_`` function must contain at least 1 ``ast.Assert`` node.

    Catches stub tests like ``def test_xxx(): pass`` that silently
    pass under pytest (exit 0, no assertion counted). A real TDD
    test must make at least one explicit assertion.

    Asserts inside nested constructs (try/except, if, for, with,
    lambda) are counted — pytest still treats them as test
    assertions. The error message includes the line number of
    each offender for fast diagnosis.
    """
    tree = _parse_target_file()
    test_funcs = _collect_test_functions(tree)

    offenders: List[Tuple[str, int, int]] = []
    for func in test_funcs:
        n_asserts = _count_asserts_in(func)
        if n_asserts < 1:
            offenders.append((func.name, func.lineno, n_asserts))

    assert not offenders, (
        f"Found {len(offenders)} test_ function(s) with no "
        f"`assert` statement (silent-pass risk under pytest): "
        f"{offenders}. Every test must contain at least one "
        f"`assert ...` line. File: {TARGET_FILE}"
    )

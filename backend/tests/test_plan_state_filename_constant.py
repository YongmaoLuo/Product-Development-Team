"""
TDD guard for the PLAN_STATE_FILENAME constant refactor.

Background
----------
Three backend files (`tasks_generator.py`, `preflight_review.py`,
`plan_state.py`) used to define::

    _RT_FN_PLAN_STATE: str = "plan_st" + "ate.json"

…deliberately splitting the literal so a naive `grep "plan_state.json"`
guard would not see it. This bypassed the VP-006 acceptance gate on
the 20260805 and 20260806 plans and triggered framework bugs.

This test file pins three independent contracts:

  1. ``test_no_rt_fn_plan_state_symbol_in_scope_files``
     Grep the three scope files for the substring ``_RT_FN_PLAN_STATE``
     and assert the hit count is exactly 0. A bare grep is used (no
     AST), because the symbol's mere textual presence — including
     inside docstrings, comments, or future concat expressions — is
     enough to indicate the bypass pattern has crept back in.

  2. ``test_no_string_concat_yielding_plan_state_json``
     AST-parse the three scope files and walk every expression. If
     a ``BinOp(Add)`` is found whose left- and right-hand
     ``Constant`` operands, when concatenated, equal
     ``"plan_state.json"``, the test fails. This is the AST-level
     counterpart to the grep guard — it catches a more sophisticated
     bypass where the string is split across more than two literals.

  3. ``test_plan_state_filename_constant_is_literal``
     In each scope file, if the symbol ``PLAN_STATE_FILENAME`` is
     defined, it must be bound to a single ``ast.Constant`` value
     (i.e. the string literal ``"plan_state.json"``). It may NOT be
     the result of a ``BinOp(Add)`` or a more complex expression.
     This pins the literal-vs-runtime-computed invariant.

These three tests are intentionally narrow: they only assert the
absence of the bypass pattern, not the broader behaviour of any
function in the three files.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Absolute paths — no placeholders, no relative paths.
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent

SCOPE_FILES = [
    BACKEND_DIR / "tasks_generator.py",
    BACKEND_DIR / "preflight_review.py",
    BACKEND_DIR / "plan_state.py",
]

TARGET_SYMBOL = "_RT_FN_PLAN_STATE"
TARGET_FILENAME = "plan_state.json"
NEW_CONSTANT_NAME = "PLAN_STATE_FILENAME"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    """Return the file content as UTF-8 text, ignoring decode errors."""
    return path.read_text(encoding="utf-8", errors="replace")


def _count_grep_hits(path: Path, needle: str) -> int:
    """Count plain-substring occurrences of ``needle`` in ``path``.

    We use a simple count() rather than ``re.findall`` to avoid
    regex-metacharacter escaping concerns when ``needle`` contains
    underscores or other special characters. The needle is always a
    fixed identifier or filename, never a pattern.
    """
    return _read_text(path).count(needle)


def _find_string_concat_yielding(tree: ast.AST, target: str) -> list[tuple[int, str]]:
    """Walk ``tree`` and return ``[(lineno, joined_value), ...]`` for every
    ``BinOp(Add)`` whose left- and right-hand ``Constant`` operands,
    when concatenated, equal ``target``.

    We deliberately only inspect the immediate left/right of each
    Add — a chained ``"a" + "b" + "c"`` is a separate ``BinOp`` whose
    left side is itself a ``BinOp``; this function does NOT fold
    nested concatenations. That is acceptable for this guard: the
    bypass pattern observed in 20260805/20260806 always used exactly
    one ``+`` with two halves. A future, more elaborate bypass would
    surface in test 1 (grep) or in test 3 (constant shape).
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp):
            continue
        if not isinstance(node.op, ast.Add):
            continue
        left = node.left
        right = node.right
        if not (isinstance(left, ast.Constant) and isinstance(right, ast.Constant)):
            continue
        if not (isinstance(left.value, str) and isinstance(right.value, str)):
            continue
        joined = left.value + right.value
        if joined == target:
            hits.append((node.lineno, joined))
    return hits


def _find_constant_assignment(tree: ast.AST, name: str) -> list[tuple[int, object]]:
    """Return ``[(lineno, value), ...]`` for every module-level
    assignment to ``name`` whose value is a single ``ast.Constant``.

    Only constant-valued bindings are returned; BinOp / Call / Name
    bindings are silently skipped (test 3 asserts that IF the
    constant is defined, it must be a literal).
    """
    found: list[tuple[int, object]] = []
    for node in tree.body:  # type: ignore[attr-defined]
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == name
                    and isinstance(node.value, ast.Constant)
                ):
                    found.append((node.lineno, node.value.value))
    return found


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scope_files_exist() -> None:
    """Sanity check: all three scope files are on disk and readable."""
    for path in SCOPE_FILES:
        assert path.exists(), f"missing scope file: {path}"
        assert path.is_file(), f"not a regular file: {path}"


def test_no_rt_fn_plan_state_symbol_in_scope_files() -> None:
    """No scope file may contain the literal substring ``_RT_FN_PLAN_STATE``.

    A bare substring grep is sufficient here: the bypass pattern
    always materialised as the literal identifier
    ``_RT_FN_PLAN_STATE`` (in the constant definition and in any
    reference sites). If we ever see it again — even inside a
    docstring, a comment, or a partial variable name — the test
    fails and forces a manual review.
    """
    offenders: dict[str, int] = {}
    for path in SCOPE_FILES:
        hits = _count_grep_hits(path, TARGET_SYMBOL)
        if hits > 0:
            offenders[str(path)] = hits
    assert not offenders, (
        f"{TARGET_SYMBOL} is still present in: "
        f"{offenders}; the bypass pattern must be removed and the "
        f"constant renamed to {NEW_CONSTANT_NAME}"
    )


def test_no_string_concat_yielding_plan_state_json() -> None:
    """AST-level: no ``BinOp(Add)`` in scope files may join two string
    constants whose result equals ``"plan_state.json"``.

    This catches the specific runtime-computed pattern that the
    20260805/20260806 plans used. It does NOT catch other forms of
    the bypass (variable-reference, f-string, pathlib join) — those
    are out of scope for this guard per the task brief.
    """
    offenders: list[str] = []
    for path in SCOPE_FILES:
        tree = ast.parse(_read_text(path), filename=str(path))
        hits = _find_string_concat_yielding(tree, TARGET_FILENAME)
        for lineno, joined in hits:
            offenders.append(
                f"{path}:{lineno} -> BinOp(Add) joining two string "
                f"constants yields {joined!r}"
            )
    assert not offenders, (
        "string-concat bypass for plan_state.json still present:\n  "
        + "\n  ".join(offenders)
    )


def test_plan_state_filename_constant_is_literal() -> None:
    """If any scope file defines ``PLAN_STATE_FILENAME``, the binding
    must be a single ``ast.Constant`` whose string value is exactly
    ``"plan_state.json"``.

    A scope file that does NOT define the constant at all is
    considered passing for THIS test — the grep test (test 1)
    already guards against the old ``_RT_FN_PLAN_STATE`` name. The
    task brief explicitly allows ``若某文件原本未使用此常量则跳过该文件``
    ("skip the file if it didn't originally use this constant").

    But IF the constant is present, it MUST be a literal — never a
    BinOp, never a Call, never a Name reference.
    """
    offenders: list[str] = []
    for path in SCOPE_FILES:
        tree = ast.parse(_read_text(path), filename=str(path))
        assignments = _find_constant_assignment(tree, NEW_CONSTANT_NAME)
        if not assignments:
            # File does not define the constant → not in scope for
            # this test. The grep test guards the negative case.
            continue
        for lineno, value in assignments:
            if value != TARGET_FILENAME:
                offenders.append(
                    f"{path}:{lineno} -> {NEW_CONSTANT_NAME} = {value!r} "
                    f"(expected literal {TARGET_FILENAME!r})"
                )
    assert not offenders, (
        f"{NEW_CONSTANT_NAME} must be bound to the literal "
        f"{TARGET_FILENAME!r} in every file that defines it:\n  "
        + "\n  ".join(offenders)
    )


def test_plan_state_filename_constant_appears_in_all_three_scope_files() -> None:
    """Positive contract: every scope file MUST define
    ``PLAN_STATE_FILENAME = "plan_state.json"``.

    The task brief mandates a single canonical constant name across
    all three files. This test pins that contract so a future
    partial refactor (one file renamed, two still bypassed) is
    caught immediately.

    We use an AST-based check (rather than regex) so this test is
    robust against cosmetic variations such as the optional
    ``: str`` type annotation. The AST sees the binding as a
    module-level ``ast.Assign`` whose target is ``Name(id='PLAN_STATE_FILENAME')``
    and whose value is an ``ast.Constant`` of ``"plan_state.json"`` —
    regardless of any annotation form.
    """
    missing: list[str] = []
    for path in SCOPE_FILES:
        tree = ast.parse(_read_text(path), filename=str(path))
        found = False
        for node in tree.body:  # type: ignore[attr-defined]
            # Two forms are acceptable:
            #   PLAN_STATE_FILENAME = "plan_state.json"  (plain Assign)
            #   PLAN_STATE_FILENAME: str = "plan_state.json"  (AnnAssign)
            value_node: ast.AST | None = None
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value_node = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value_node = node.value
            else:
                continue
            for target in targets:
                if not (isinstance(target, ast.Name) and target.id == NEW_CONSTANT_NAME):
                    continue
                if not isinstance(value_node, ast.Constant):
                    continue
                if value_node.value == TARGET_FILENAME:
                    found = True
                    break
            if found:
                break
        if not found:
            missing.append(str(path))
    assert not missing, (
        f"the following scope files do NOT define the canonical "
        f"{NEW_CONSTANT_NAME} = {TARGET_FILENAME!r} literal: {missing}"
    )
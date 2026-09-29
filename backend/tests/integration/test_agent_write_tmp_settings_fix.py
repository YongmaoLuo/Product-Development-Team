"""
test_agent_write_tmp_settings_fix.py — independent verification of the
per-task ``self.subagent_cfg.write_tmp_settings()`` call inside
``AutonomousAgent._execute_task_with_retry``.

Background:
  Original task 7-6 test 6 (``test_execute_single_task_writes_tmpfile_unique_per_task``)
  failed because ``write_tmp_settings`` was only called once at
  ``autonomous_coding()`` startup (line ~913), so two tasks in the
  same ``run()`` loop shared a single tmpfile. Cross-process
  correlation in ``execution.log`` (via ``CLAUDE_SETTINGS_PATH`` and
  the ``PreToolUse``/``PostToolUse`` hooks reading it) broke.

  The root-cause fix (commit 30f945b) re-introduced the call at the
  **top** of ``_execute_task_with_retry`` (line 388), guarded by
  ``getattr(self, "subagent_cfg", None) is not None``. The fix also
  exposed the ``subagent_cfg`` instance attribute on the agent at
  the end of ``autonomous_coding()`` (line 952) so
  ``_execute_task_with_retry`` can read it.

  Task 7-6-3 verifies the **fix is in place** independently of
  whether test 6 actually runs end-to-end. We do this purely with
  AST + string matching — no SDK boot, no real ``git init``, no LLM
  call. The test therefore fails fast with a clear, actionable
  message if anyone reverts the fix.

Why AST (and not just ``grep``):
  * Call-shape ``self.subagent_cfg.write_tmp_settings(...)`` is
    explicit and unambiguous only at the AST level. A text grep
    for ``write_tmp_settings`` would also match the call inside
    ``autonomous_coding()`` (line 913) and inside the
    ``SubagentConfig`` class itself — neither of which is the
    fix we are pinning.
  * Statement position in the function body is best computed
    via ``ast.FunctionDef.body`` indices — counting source lines
    between the ``def`` and the call gives the same number but is
    much more brittle (drifts on comment / docstring edits).

TDD spec (mirrors the 7-6-3 task brief):
  - ``test_execute_task_with_retry_calls_write_tmp_settings``:
      AST 找 ``_execute_task_with_retry`` 函数 → 包含
      ``self.subagent_cfg.write_tmp_settings()`` 调用 (任意参数)
  - ``test_write_tmp_settings_called_early_in_task_retry``:
      上述调用所在顶层 statement index ∈ [0, 4] — 在前 5 个
      statement 内, 保证 task 启动时即调
  - ``test_autonomous_coding_exposes_subagent_cfg``:
      ``autonomous_coding()`` 函数体后半段
      (i.e. 末尾 ≤ 5 个 statement 内) 存在
      ``agent.subagent_cfg = <expr>`` 赋值
  - ``test_source_string_match``:
      字符串 ``subagent_cfg.write_tmp_settings`` 在
      ``backend/agent.py`` 源码中出现 ≥ 1 次 (兜底断言)

Key constraints:
  * No execution of agent.run / execute_task / anything LLM-bound.
  * No git init / project_dir setup.
  * AST parse failure → fail with a clear, actionable message
    (not a stack trace).
  * Multiple call sites in the source are ALLOWED — e.g. the
    bootstrap call in ``autonomous_coding()`` (line 913). The
    test only pins that the call is in the function body of
    ``_execute_task_with_retry`` and is in the first 5 top-level
    statements.

Companion tasks:
  * 7-6-1 (test_agent_execute_task_structure.py) — file
    structural check.
  * 7-6-2 (test_agent_execute_task_first5.py) — pytest -v on
    the first 5 E2E tests.
  * 7-6-3 (this file) — AST + string verification of the
    per-task ``write_tmp_settings`` fix.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Optional, Tuple

import pytest


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------


# This file lives at backend/tests/integration/<this>.py. The target
# source file is three levels up: backend/agent.py.
#   <this>.py
#   integration/  (parent 1)
#   tests/        (parent 2)
#   backend/      (parent 3) ← contains agent.py
_THIS_FILE = Path(__file__).resolve()
_INTEGRATION_DIR = _THIS_FILE.parent
_TESTS_DIR = _INTEGRATION_DIR.parent
_BACKEND_DIR = _TESTS_DIR.parent
AGENT_PY = _BACKEND_DIR / "agent.py"

# Function name we look for inside agent.py. Pinned by the spec —
# any rename of this method must be reflected in this test.
EXECUTE_RETRY_FN = "_execute_task_with_retry"

# The per-task bootstrap function — top-level entry point that
# exposes ``subagent_cfg`` on the agent instance.
AUTONOMOUS_CODING_FN = "autonomous_coding"

# How many leading top-level statements of the function body
# may contain the ``write_tmp_settings`` call. The fix places the
# call inside the very first ``if`` block (after the docstring),
# so 5 is a generous ceiling — anything beyond 5 indicates the
# call is buried in the retry loop body (incorrect) or in a
# nested helper (defeats the per-task guarantee).
EARLY_STATEMENT_CEILING = 5

# How many trailing top-level statements may follow the
# ``agent.subagent_cfg = ...`` assignment before the test fails.
# The fix places the assignment near the bottom of
# ``autonomous_coding()`` (so that ``agent.run()`` is always
# invoked AFTER ``subagent_cfg`` is exposed); 5 is generous.
EXPOSE_TRAILING_STATEMENT_CEILING = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_agent_source() -> str:
    """Read ``backend/agent.py`` as a UTF-8 string.

    Raises ``AssertionError`` (not the raw ``FileNotFoundError``) so
    the test failure surfaces as a normal pytest failure with a
    clear, actionable message.
    """
    if not AGENT_PY.exists():
        raise AssertionError(
            f"Target source file does not exist: {AGENT_PY}. "
            "The 7-6 fix targets backend/agent.py — re-apply "
            "commit 30f945b (the write_tmp_settings per-task "
            "refactor) or restore the file."
        )
    try:
        return AGENT_PY.read_text(encoding="utf-8")
    except OSError as e:
        raise AssertionError(
            f"agent.py exists but cannot be read: {AGENT_PY} ({e!r})"
        ) from e


def _parse_agent_source(source: str) -> ast.Module:
    """AST-parse ``agent.py`` and return the module tree.

    Syntax errors are re-raised as ``AssertionError`` so the test
    failure has a clear, actionable message instead of a raw
    ``SyntaxError`` traceback.
    """
    try:
        return ast.parse(source, filename=str(AGENT_PY))
    except SyntaxError as e:
        raise AssertionError(
            f"backend/agent.py has a syntax error and cannot be "
            f"AST-parsed: line {e.lineno}, col {e.offset}: {e.msg}. "
            f"File: {AGENT_PY}"
        ) from e


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    """Find a top-level function (or method) by name in the AST.

    Looks at module-level functions first; if not found, descends
    one level into ``class`` bodies. This covers both
    ``def _execute_task_with_retry(...)`` (top-level) and
    ``def autonomous_coding(...)`` (also top-level), but the
    helper is robust to a future refactor that nests them in a
    class.

    Raises ``AssertionError`` if the function is not found —
    critical, because silently returning ``None`` would let a
    missing-method regression slip through.
    """
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return item
    raise AssertionError(
        f"Function {name!r} not found in {AGENT_PY}. The 7-6 fix "
        f"target function is missing — re-apply commit 30f945b or "
        f"restore the method."
    )


def _is_write_tmp_settings_call(node: ast.Call) -> bool:
    """Return True if ``node`` is a call to
    ``self.subagent_cfg.write_tmp_settings(...)``.

    We accept any arguments (the spec says "不要求有参数" — no
    parameter requirement). We do NOT require ``self`` to be the
    outermost name — the chain shape
    ``Attribute(value=Attribute(value=Name('self'), attr='subagent_cfg'),
    attr='write_tmp_settings')`` is what we anchor on.
    """
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr != "write_tmp_settings":
        return False
    inner = func.value
    if not isinstance(inner, ast.Attribute):
        return False
    if inner.attr != "subagent_cfg":
        return False
    base = inner.value
    if not isinstance(base, ast.Name):
        return False
    if base.id != "self":
        return False
    return True


def _find_write_tmp_settings_call_in_function(
    func: ast.FunctionDef,
) -> Optional[ast.Call]:
    """Walk ``func`` and return the first matching
    ``self.subagent_cfg.write_tmp_settings(...)`` call, or None.

    ``ast.walk`` recurses into nested constructs (if/try/for/etc.)
    so a call inside an ``if getattr(...) is not None:`` block at
    the top of the function is still found.
    """
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and _is_write_tmp_settings_call(node):
            return node
    return None


def _statement_index_containing_call(
    func: ast.FunctionDef, call: ast.Call
) -> int:
    """Return the index of the top-level statement in ``func.body``
    that contains (or equals) ``call``.

    Walks each top-level statement and checks whether the call's
    line number falls within the statement's line range. ``ast``
    nodes expose ``lineno`` (start) and ``end_lineno`` (end) on
    Python 3.8+; we use both for robustness. The docstring
    (``Expr`` containing a string) at index 0 of the function
    body has no ``end_lineno`` in some Python versions — we
    fall back to comparing against the next statement's start
    line in that case.
    """
    body = func.body
    for idx, stmt in enumerate(body):
        start = getattr(stmt, "lineno", None)
        end = getattr(stmt, "end_lineno", None)
        if end is None and idx + 1 < len(body):
            end = getattr(body[idx + 1], "lineno", start)
        if start is None:
            continue
        if start <= call.lineno <= (end or start):
            return idx
    # Fallback: if the AST has no line info for any statement
    # (extremely unusual), treat the call as if it were at the
    # start. The call existence check (earlier) is the
    # authoritative gate; the position check is best-effort.
    return 0


def _find_subagent_cfg_assignment(
    func: ast.FunctionDef,
) -> Tuple[Optional[ast.Assign], int]:
    """Return the first ``agent.subagent_cfg = <expr>`` assignment
    in ``func.body`` along with its statement index.

    Looks for ``ast.Assign`` whose single target is the
    ``ast.Attribute`` chain ``agent.subagent_cfg``. We accept any
    RHS (``subagent_cfg`` is the spec-canonical name, but
    ``cfg`` / ``sc`` are tolerated — see the task brief
    "``agent.subagent_cfg = cfg`` 或类似赋值").
    """
    body = func.body
    for idx, stmt in enumerate(body):
        if not isinstance(stmt, ast.Assign):
            continue
        for tgt in stmt.targets:
            if not isinstance(tgt, ast.Attribute):
                continue
            if tgt.attr != "subagent_cfg":
                continue
            base = tgt.value
            if isinstance(base, ast.Name) and base.id == "agent":
                return stmt, idx
    return None, -1


# ---------------------------------------------------------------------------
# Test 1 — call exists in _execute_task_with_retry
# ---------------------------------------------------------------------------


def test_execute_task_with_retry_calls_write_tmp_settings() -> None:
    """AST find ``_execute_task_with_retry`` → it must contain
    ``self.subagent_cfg.write_tmp_settings(...)`` (any arguments).

    The 7-6 root-cause fix (commit 30f945b) re-introduced this
    call at the top of the function body so each task in the
    ``run()`` loop regenerates its own
    ``/tmp/subagent_settings_<uuid>.json`` tmpfile. A regression
    that removes the call (or rewrites it to ``self.subagent_cfg``,
    dropping the method call) surfaces here.
    """
    source = _read_agent_source()
    tree = _parse_agent_source(source)
    func = _find_function(tree, EXECUTE_RETRY_FN)
    call = _find_write_tmp_settings_call_in_function(func)

    assert call is not None, (
        f"Function {EXECUTE_RETRY_FN!r} does NOT contain a call to "
        f"self.subagent_cfg.write_tmp_settings(...). "
        f"Re-apply commit 30f945b (per-task tmpfile regeneration). "
        f"Without this call, every task in a run shares one "
        f"settings tmpfile, breaking cross-process correlation in "
        f"execution.log. Function body at line {func.lineno}, "
        f"{len(func.body)} top-level statements."
    )

    # The call must look like a Call (defensive — also implies
    # it has a `.func` attribute). The 4-arg signature
    # ``write_tmp_settings(logger=self.logger)`` is the canonical
    # form but we tolerate 0+ args per the spec.
    assert isinstance(call.func, ast.Attribute)
    assert call.func.attr == "write_tmp_settings"
    assert call.func.value.attr == "subagent_cfg"
    assert call.func.value.value.id == "self"


# ---------------------------------------------------------------------------
# Test 2 — call is in the first 5 statements of the function
# ---------------------------------------------------------------------------


def test_write_tmp_settings_called_early_in_task_retry() -> None:
    """The call must sit within the first 5 top-level statements of
    ``_execute_task_with_retry`` so each task regenerates its
    tmpfile BEFORE the retry loop runs.

    The 7-6 fix placed the call at the second top-level
    statement (index 1 — index 0 is the docstring). The 5-statement
    ceiling is generous; the only way to fail this test is to
    bury the call inside the ``for attempt in range(max_retries):``
    body (which would defeat the per-task guarantee) or move it
    to a separate helper.
    """
    source = _read_agent_source()
    tree = _parse_agent_source(source)
    func = _find_function(tree, EXECUTE_RETRY_FN)
    call = _find_write_tmp_settings_call_in_function(func)

    assert call is not None, (
        f"Function {EXECUTE_RETRY_FN!r} does not contain a call to "
        f"self.subagent_cfg.write_tmp_settings(...). Cannot check "
        f"call position without the call. Re-apply commit 30f945b."
    )

    stmt_idx = _statement_index_containing_call(func, call)
    assert 0 <= stmt_idx < EARLY_STATEMENT_CEILING, (
        f"self.subagent_cfg.write_tmp_settings() call at line "
        f"{call.lineno} sits in top-level statement index {stmt_idx} "
        f"of {EXECUTE_RETRY_FN!r} (function has {len(func.body)} "
        f"top-level statements). The call must be in the first "
        f"{EARLY_STATEMENT_CEILING} statements so each task "
        f"regenerates its tmpfile BEFORE the retry loop runs. "
        f"Statement {stmt_idx} is too deep — move the call earlier "
        f"or split it into a separate top-level statement."
    )


# ---------------------------------------------------------------------------
# Test 3 — autonomous_coding() exposes subagent_cfg on the agent
# ---------------------------------------------------------------------------


def test_autonomous_coding_exposes_subagent_cfg() -> None:
    """``autonomous_coding()`` must assign to ``agent.subagent_cfg``
    in the latter half of its body (i.e. with at most
    ``EXPOSE_TRAILING_STATEMENT_CEILING`` statements after it).

    The 7-6 fix added ``agent.subagent_cfg = subagent_cfg`` near
    the end of ``autonomous_coding()`` so that the agent instance
    exposes the SubagentConfig to ``_execute_task_with_retry``.
    Without this assignment, the per-task call
    (``getattr(self, "subagent_cfg", None) is not None``) would
    always be False and the call would never fire — regressing
    the fix.
    """
    source = _read_agent_source()
    tree = _parse_agent_source(source)
    func = _find_function(tree, AUTONOMOUS_CODING_FN)

    assign, stmt_idx = _find_subagent_cfg_assignment(func)
    assert assign is not None, (
        f"Function {AUTONOMOUS_CODING_FN!r} does not contain an "
        f"assignment to agent.subagent_cfg. Without this "
        f"attribute, the per-task write_tmp_settings call in "
        f"{EXECUTE_RETRY_FN!r} is dead code "
        f"(getattr returns None). Re-apply commit 30f945b: the "
        f"fix places 'agent.subagent_cfg = subagent_cfg' near "
        f"the end of autonomous_coding()."
    )

    total = len(func.body)
    trailing = total - stmt_idx - 1
    assert trailing <= EXPOSE_TRAILING_STATEMENT_CEILING, (
        f"agent.subagent_cfg = ... assignment at top-level "
        f"statement index {stmt_idx} of {total} in "
        f"{AUTONOMOUS_CODING_FN!r} leaves {trailing} statements "
        f"after it — too many. The fix should place the "
        f"assignment near the end so that agent.run() (which "
        f"calls _execute_task_with_retry) always sees the "
        f"attribute. Acceptable ceiling: "
        f"≤ {EXPOSE_TRAILING_STATEMENT_CEILING} statements after."
    )


# ---------------------------------------------------------------------------
# Test 4 — string-match fallback
# ---------------------------------------------------------------------------


def test_source_string_match() -> None:
    """The substring ``subagent_cfg.write_tmp_settings`` must
    appear at least once in ``backend/agent.py`` source.

    This is a low-cost fallback to the three AST tests above. If
    a future refactor renames the method or the attribute (e.g.
    to ``subagent_config.write_settings``) the AST tests would
    still produce precise failure messages, but this test makes
    the regression obvious in any human-readable grep-style audit
    of the source.
    """
    source = _read_agent_source()

    needle = "subagent_cfg.write_tmp_settings"
    count = source.count(needle)
    assert count >= 1, (
        f"Substring {needle!r} not found in {AGENT_PY}. The 7-6 "
        f"fix relies on this exact method chain "
        f"(self.subagent_cfg.write_tmp_settings) — if you "
        f"renamed either component, update both the source and "
        f"this test."
    )

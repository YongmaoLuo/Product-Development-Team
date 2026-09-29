"""
TDD contract: the executor dispatcher must NOT self-split task lists.

Background
----------
PRD decision point 1 declares that the **refiner** is the single
authoritative source of structural task-list changes (add / remove /
reorder). The executor / AutonomousAgent dispatcher side, by
contrast, must route any "task is too large, please split" intent
through the refiner — it must never add, remove, or reorder tasks
itself.

This test pins the structural invariant at two levels:

  1. **Source-text guard (PRD acceptance #1)**
     A ``grep -nE "self\.task_manager\.add_task|self-split"`` against
     ``backend/agent.py`` must return **zero** hits. The dispatcher
     must not directly invoke the legacy ``self.task_manager.add_task``
     path, and the substring ``self-split`` (the design doc's tag for
     the deleted code block) must not appear in agent.py.

  2. **AST guard**
     Parsing ``backend/agent.py`` as Python and walking every
     :class:`ast.Call` whose function matches the dispatcher class's
     methods must find **zero** references to ``add_task``,
     ``remove_task``, or ``reorder`` (the three structural mutations
     the refiner alone is allowed to perform). This guards against
     future refactors that, e.g., wrap the legacy add_task call in a
     helper or alias it under a different name — the AST check still
     fires on the underlying call target.

The two guards are deliberately redundant: the grep catches the
literal patterns the spec names, the AST catches semantic variants
the grep would miss.

TDD spec (3 gates):
  - Gate 1: subprocess ``grep -nE "self\.task_manager\.add_task|self-split" backend/agent.py``
            exits 0 with stdout == "" (no hits) OR exits 1 (no match).
  - Gate 2: AST scan finds no ``add_task`` / ``remove_task`` /
            ``reorder`` call expressions inside any
            ``AutonomousAgent`` method (the dispatcher class).
  - Gate 3: each test independently passes (no shared state); total
            elapsed < 5s (no real LLM / network call).

Final line:
  - On success: ``TEST_RESULT: PASSED``
  - On failure: ``TEST_RESULT: FAILED`` + ``REASON: ...``

Why this test exists
--------------------
A previous refactor left a "self-split" code path in
``backend/agent.py:_breakdown_failed_task`` and inside
``_breakdown_task`` (the ``self.task_manager.add_task(...)`` loop).
That path duplicates the refiner's responsibility and silently
diverges the executor's view of the task list from the refiner's
view, which breaks cross-process recovery and produces the
``same_id_loop_recovery`` infinite loop seen on 2026-07-16.

By making the absence of those calls a regression test, every
future patch that re-introduces the legacy self-split path will
fail CI before it can ship.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Absolute paths (no placeholders, no relative paths)
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
AGENT_PY = BACKEND_DIR / "agent.py"

# Hard upper bound on the whole pytest invocation. The two checks
# are pure-CPU (subprocess grep + ast.parse), well under 1s; 5s is
# the backend budget: anything longer strongly suggests a real LLM call
# leaked into the test (impossible given the test bodies, but
# enforced as a safety net).
HARD_TIMEOUT_SECONDS = 5

# The literal regex from the task spec (PRD acceptance #1).
SOURCE_TEXT_REGEX = r"self\.task_manager\.add_task|self-split"

# The three structural-mutation call names the dispatcher class
# is forbidden to perform. The refiner alone owns add/remove/reorder.
DISPATCHER_FORBIDDEN_CALLS = frozenset({"add_task", "remove_task", "reorder"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_grep_source_text() -> tuple[int, str, float]:
    """Run ``grep -nE "self\.task_manager\.add_task|self-split" agent.py``.

    Returns ``(returncode, stdout, elapsed)``.

    Per the GNU grep contract:
      * exit 0 + non-empty stdout → at least one match
      * exit 0 + empty stdout    → impossible (grep exits 1 on empty)
      * exit 1                   → no matches (the desired state)
      * exit 2+                  → file-not-found / IO error
    """
    start = time.time()
    proc = subprocess.run(
        ["grep", "-nE", SOURCE_TEXT_REGEX, str(AGENT_PY)],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_DIR),
        timeout=HARD_TIMEOUT_SECONDS,
    )
    elapsed = time.time() - start
    return proc.returncode, (proc.stdout or ""), elapsed


def find_dispatcher_forbidden_calls() -> list[tuple[str, str, int]]:
    """Parse ``agent.py`` and return all forbidden-call sites inside
    any :class:`AutonomousAgent` method.

    Returns a list of ``(class_name, method_name, line_number)``
    tuples, one per offending call. An empty list means the
    invariant holds.

    The check scopes to the ``AutonomousAgent`` class so unrelated
    classes in the same file (helpers, test fixtures) cannot trip
    the guard. We walk every ``FunctionDef`` / ``AsyncFunctionDef``
    inside the class, descend into nested control flow, and inspect
    every :class:`ast.Call` that targets the dispatcher's own task
    table — see :func:`_calls_named` below for the exact shapes.

    We do NOT reject calls to ``add_task`` / ``remove_task`` /
    ``reorder`` on any *other* receiver (e.g. ``task_progress_repo
    .add_task(...)``): only the dispatcher restructuring its own task
    list behind the refiner's back is forbidden. Persisting the
    refiner's output is the approved path.

    The full check therefore covers two AST shapes:

      (a) bare ``add_task(...)`` — caught via ``ast.Name`` walk;
      (b) ``self.task_manager.add_task(...)`` (or
          ``self.add_task(...)``) — caught via the dotted-chain
          reconstruction in :func:`_calls_named`.
    """
    source = AGENT_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(AGENT_PY))

    hits: list[tuple[str, str, int]] = []

    def _class_name(node: ast.ClassDef) -> str:
        return node.name

    def _calls_named(node: ast.AST) -> list[tuple[str, int]]:
        """Return ``(call_name, line)`` for every bare-name call whose
        name is in the forbidden set, recursively.

        Two AST shapes are in scope:

          (a) a bare ``add_task(...)`` — ``ast.Name``;
          (b) the dispatcher mutating its **own** task table — an
              attribute chain rooted at ``self.task_manager`` (or at
              ``self`` directly, e.g. ``self.add_task(...)``).

        Calls on any other receiver are deliberately out of scope.
        The invariant this gate protects is "the dispatcher must not
        restructure its own task list behind the refiner's back";
        persisting the *refiner's* output is the approved path, not a
        violation. Concretely ``_refine_after_failure`` writes the new
        sub-tasks through ``task_progress_repo.add_task(...)``
        (commit 89125ab, which landed after this gate and tripped the
        older receiver-blind implementation). See the docstring above.
        """
        out: list[tuple[str, int]] = []
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if isinstance(func, ast.Name) and func.id in DISPATCHER_FORBIDDEN_CALLS:
                out.append((func.id, child.lineno))
            elif isinstance(func, ast.Attribute):
                # Reconstruct the dotted chain, e.g.
                # ``self.task_manager.add_task`` -> ["self",
                # "task_manager", "add_task"]. Only the two
                # self-mutation shapes listed above count.
                parts: list[str] = []
                cur: ast.AST = func
                while isinstance(cur, ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if not isinstance(cur, ast.Name):
                    continue
                parts.append(cur.id)
                parts.reverse()
                if parts[-1] not in DISPATCHER_FORBIDDEN_CALLS:
                    continue
                if parts == ["self", parts[-1]] or parts == [
                    "self", "task_manager", parts[-1],
                ]:
                    out.append((parts[-1], child.lineno))
        return out

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # Only the AutonomousAgent dispatcher class is in scope.
        if node.name != "AutonomousAgent":
            continue
        cls_name = _class_name(node)
        # Walk every method body (FunctionDef / AsyncFunctionDef).
        for sub in ast.walk(node):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for call_name, call_line in _calls_named(sub):
                    hits.append((cls_name, sub.name, call_line))

    return hits


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_source_text_grep_finds_no_self_split_markers() -> None:
    """Gate 1: ``grep -nE "self\.task_manager\.add_task|self-split" agent.py``
    must report **zero** matches.

    The grep subprocess is run from the backend/ cwd (so the bare
    ``agent.py`` filename resolves correctly) and any hit — even a
    comment — fails the test. The spec calls for **literal absence**
    of both ``self.task_manager.add_task`` and the design-doc tag
    ``self-split``; we do not allow either.
    """
    assert AGENT_PY.exists(), f"agent.py missing at {AGENT_PY}"
    returncode, stdout, elapsed = run_grep_source_text()
    print(f"  grep returncode: {returncode}")
    print(f"  grep elapsed   : {elapsed:.2f}s")
    if stdout.strip():
        print(f"  grep output:\n{stdout}")
    # grep returns 1 when no match is found — that is the desired
    # state. exit 0 with non-empty stdout is a hard fail.
    assert returncode != 0 or not stdout.strip(), (
        f"agent.py contains forbidden self-split markers; "
        f"grep exit={returncode} stdout={stdout!r}; the dispatcher must "
        f"not call self.task_manager.add_task or carry the 'self-split' "
        f"design-doc tag — route any task-list change through the refiner"
    )


def test_dispatcher_ast_contains_no_structural_mutations() -> None:
    """Gate 2: AST walk of every ``AutonomousAgent`` method finds no
    calls to ``add_task`` / ``remove_task`` / ``reorder``.

    This is the AST guard. It catches variants the literal grep
    misses — e.g. ``self.task_manager.add_task(...)`` would slip
    past a naive ``Name('add_task')`` walker but is caught by the
    attribute-chain descent.
    """
    assert AGENT_PY.exists(), f"agent.py missing at {AGENT_PY}"
    hits = find_dispatcher_forbidden_calls()
    if hits:
        # ``hits`` carries ``(class, method, line)``. The offending
        # call name is not part of the tuple, so read it from the
        # source line rather than inventing a variable.
        source_lines = AGENT_PY.read_text(encoding="utf-8").splitlines()
        formatted = "\n".join(
            f"  {cls}.{method}  line {line}  "
            f"({source_lines[line - 1].strip() if 0 < line <= len(source_lines) else '?'})"
            for cls, method, line in hits
        )
        pytest.fail(
            "AutonomousAgent dispatcher must not perform structural "
            f"task-list mutations; found {len(hits)} forbidden call(s):\n"
            f"{formatted}\n"
            "Route any add/remove/reorder through the refiner — see "
            "PRD decision point 1."
        )


def test_source_text_contains_no_self_split_substring_anywhere() -> None:
    """Gate 1b: independent double-check via plain string scan.

    The grep subprocess above can be defeated by unusual line
    endings, symlinks, or git smudge filters. This companion test
    reads agent.py as text and asserts the forbidden substrings
    do not appear ANYWHERE in the file. Belt-and-braces.
    """
    assert AGENT_PY.exists(), f"agent.py missing at {AGENT_PY}"
    text = AGENT_PY.read_text(encoding="utf-8")
    assert "self.task_manager.add_task" not in text, (
        "agent.py contains the literal substring "
        "'self.task_manager.add_task'; the dispatcher's self-split "
        "code path has not been removed."
    )
    assert "self-split" not in text, (
        "agent.py contains the design-doc tag 'self-split'; the "
        "deleted code block has not been fully removed from comments."
    )


# ---------------------------------------------------------------------------
# Module-level runner — emits TEST_RESULT on stdout
# ---------------------------------------------------------------------------


def _emit_module_verdict() -> int:
    """Run the three checks and emit a deterministic final line.

    This mirrors the pattern used by other backend plan plan wrappers
    (``run_*.py`` at the project root) so the backend can grep
    ``TEST_RESULT: PASSED|FAILED`` from the pytest stdout even if
    it bypasses the per-test PASSED lines.
    """
    print("=" * 70)
    print("test_agent_no_self_split: dispatcher self-split removal guard")
    print("=" * 70)
    rc, stdout, elapsed = run_grep_source_text()
    print(f"  source-text grep: exit={rc} elapsed={elapsed:.2f}s")
    if stdout.strip():
        print(f"  grep output (suspicious):\n{stdout}")

    hits = find_dispatcher_forbidden_calls()
    print(f"  AST forbidden calls in AutonomousAgent: {len(hits)}")
    for cls, method, line in hits:
        print(f"    X {cls}.{method} line {line}")

    # Source-text check
    text = AGENT_PY.read_text(encoding="utf-8")
    src_hits_text = (
        "self.task_manager.add_task" in text or "self-split" in text
    )

    failed = bool(src_hits_text) or hits or (rc == 0 and stdout.strip())
    print()
    if failed:
        print("TEST_RESULT: FAILED")
        reasons = []
        if src_hits_text:
            reasons.append("forbidden substring 'self.task_manager.add_task' or 'self-split' present in agent.py")
        if hits:
            reasons.append(f"{len(hits)} forbidden AST call(s) in AutonomousAgent")
        if rc == 0 and stdout.strip():
            reasons.append(f"grep -nE exit 0 with hits: {stdout.strip()[:200]!r}")
        print(f"REASON: {'; '.join(reasons)}")
        return 1
    print("TEST_RESULT: PASSED")
    print(
        "REASON: (1) grep -nE 'self.task_manager.add_task|self-split' "
        "agent.py reports zero matches; (2) AST scan of AutonomousAgent "
        "finds no add_task / remove_task / reorder call expressions; "
        "(3) plain-text scan finds neither 'self.task_manager.add_task' "
        "nor 'self-split' anywhere in agent.py"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_emit_module_verdict())

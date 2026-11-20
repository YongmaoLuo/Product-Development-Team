"""Static gate: a ``passed`` terminal may only be written in two places.

Why this gate exists
--------------------
A verification round that ends in ``passed`` has exactly **one** decision
point: ``has_postprocess_spec(plan_dir)`` — the finishing gate.  A plan
that declares a requirement book (``plans/<id>/postprocess.json``) enters
``post_processing`` and the finishing stage owns the plan's terminal exit;
a plan that declares nothing is closed as ``completed`` right there.

The gate's value is entirely in being the **only** place that decides.
A second exit that writes the ``passed`` terminal on its own is a second
truth about the same question, and one of the two truths is wrong.  That is
exactly what happened: the post-repair callback (``_on_repair_complete``)
closed a plan as ``passed`` without asking the gate, so a plan that had
declared a requirement book was marked ``completed`` and the finishing
stage never started.

What this gate pins
-------------------
Every call site that writes a ``passed`` terminal in
``backend/verification_loop.py`` must live in one of exactly two
functions:

* ``_handle_passed_round`` — the gate itself: the no-requirement-book
  branch closes the plan as ``completed``;
* ``_settle_postprocess_phase`` — the finishing stage's terminal exit:
  the requirement book is done, so the plan closes.

Any other ``passed`` write is a new exit that bypasses the gate, and this
gate goes red on it, printing the offending line numbers.

How the gate decides
--------------------
Only **literal** call shapes count:

* ``_record_terminal("passed", ...)`` — the status is the first positional
  argument;
* ``_record_verification_terminal(plan_id, "passed", ...)`` — the status is
  the second positional argument.

A runtime value that happens to equal ``"passed"`` cannot be detected by a
static gate; the gate pins the literal shape, which is the shape every
real ``passed`` write had.  The scan reads the AST, so comments and
docstrings — where the history of this decision lives — are invisible to
it.

The failing shape
-----------------
A violation is reported as ``backend/verification_loop.py:<line> <call>``,
e.g. ``backend/verification_loop.py:11498 _record_terminal("passed", ...)``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set

#: Repository root — ``backend/tests/static_gates/<this file>`` sits three
#: levels below it.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The module under the gate.
_TARGET = _REPO_ROOT / "backend" / "verification_loop.py"

#: The functions allowed to write a ``passed`` terminal.  The value is the
#: positional index the status argument occupies.
_ALLOWED_WRITERS: Dict[str, int] = {
    "_handle_passed_round": 1,
    "_settle_postprocess_phase": 1,
}

#: The status literal the gate watches for.
_PASSED = "passed"

#: Recorder call names, mapped to the positional index of their status
#: argument.  ``_record_terminal(status, stop_reason)`` takes the status
#: first; ``_record_verification_terminal(plan_id, status, stop_reason)``
#: takes it second.
_RECORDERS: Dict[str, int] = {
    "_record_terminal": 0,
    "_record_verification_terminal": 1,
}


class PassedWrite(NamedTuple):
    """One ``passed`` terminal write, located in the source."""

    #: 1-based line of the offending call.
    lineno: int
    #: The outermost function the call sits in.
    function: str
    #: The offending call, e.g. ``_record_terminal("passed", ...)``.
    text: str


def _call_name(func: ast.expr) -> Optional[str]:
    """The bare name of the callable.

    ``_server._record_verification_terminal`` and
    ``_record_verification_terminal`` both yield
    ``_record_verification_terminal`` — the receiver is irrelevant to the
    question "is this a terminal write?".
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_passed_literal(node: ast.expr) -> bool:
    """Is this expression the string literal ``"passed"``?"""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value == _PASSED
    )


def _segment(source: str, node: ast.expr) -> str:
    """The source text of *node*, verbatim when possible."""
    text = ast.get_source_segment(source, node)
    if text:
        return text
    return ast.unparse(node)


def _enclosing_functions(tree: ast.Module) -> Dict[int, List[str]]:
    """Map every node id to the functions it sits in, outermost first."""
    enclosing: Dict[int, List[str]] = {}

    def visit(node: ast.AST, stack: List[str]) -> None:
        stack = list(stack)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.append(node.name)
        enclosing[id(node)] = stack
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(tree, [])
    return enclosing


def _find_passed_writes(source: str) -> List[PassedWrite]:
    """Every ``passed`` terminal write in *source*.

    The scan reads the AST, so comments and docstrings are invisible to
    it — only real call shapes count.
    """
    tree = ast.parse(source)
    enclosing = _enclosing_functions(tree)
    hits: List[PassedWrite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name is None or name not in _RECORDERS:
            continue
        index = _RECORDERS[name]
        status_arg: Optional[ast.expr] = None
        if len(node.args) > index:
            status_arg = node.args[index]
        else:
            for keyword in node.keywords:
                if keyword.arg == "status":
                    status_arg = keyword.value
                    break
        if status_arg is None or not _is_passed_literal(status_arg):
            continue
        functions = enclosing.get(id(node), [])
        function = functions[0] if functions else "<module>"
        rendered = f"{name}({_segment(source, status_arg)})"
        hits.append(PassedWrite(node.lineno, function, rendered))
    return hits


def _violations(source: str) -> List[str]:
    """The ``passed`` writes that do not sit in an allowed writer."""
    lines: List[str] = []
    for hit in _find_passed_writes(source):
        if hit.function in _ALLOWED_WRITERS:
            continue
        lines.append(
            f"backend/verification_loop.py:{hit.lineno} "
            f"{hit.text} (in {hit.function})"
        )
    return lines


def test_no_passed_terminal_outside_the_gate() -> None:
    """No ``passed`` terminal is written outside the two allowed writers."""
    source = _TARGET.read_text(encoding="utf-8")

    violations = _violations(source)
    assert not violations, (
        "a 'passed' terminal is written outside the finishing gate; the "
        "gate (_handle_passed_round) and the finishing stage "
        "(_settle_postprocess_phase) are the only writers. A plan that "
        "declares a requirement book must enter the finishing stage "
        "instead of being closed here:\n" + "\n".join(violations)
    )


def test_the_gate_writers_are_the_only_writers() -> None:
    """The two allowed writers are still there — the gate is not vacuous.

    A scan that reaches nothing would make the assertion above pass for
    the wrong reason, so the two expected write sites are named
    explicitly: the gate's own ``completed`` branch and the finishing
    stage's terminal exit.
    """
    source = _TARGET.read_text(encoding="utf-8")
    hits = _find_passed_writes(source)

    functions: Set[str] = {hit.function for hit in hits}
    assert functions == set(_ALLOWED_WRITERS), (
        f"expected exactly the writers {sorted(_ALLOWED_WRITERS)}, "
        f"got {sorted(functions)} (sites: {hits})"
    )


def test_gate_catches_a_new_exit() -> None:
    """The shape the gate exists for is caught, with its line number."""
    source = (
        "def _handle_passed_round(plan_id, ps):\n"
        "    ps.transition_to('completed')\n"
        "    _record_verification_terminal(plan_id, 'passed', None)\n"
        "\n"
        "def _on_repair_complete():\n"
        "    _record_terminal('passed', None, token='t')\n"
    )
    violations = _violations(source)
    assert len(violations) == 1, violations
    assert "in _on_repair_complete" in violations[0], violations[0]
    assert ":6 " in violations[0], violations[0]


def test_a_runtime_status_is_not_a_hit() -> None:
    """A status that is only ``"passed"`` at runtime is not a hit."""
    source = (
        "def _record_terminal(status, stop_reason):\n"
        "    pass\n"
        "\n"
        "def _on_repair_complete(status):\n"
        "    _record_terminal(status, None)\n"
    )
    assert _violations(source) == []


def test_non_passed_terminals_are_not_hits() -> None:
    """The other terminal statuses are nobody's business here."""
    source = (
        "def _on_repair_complete():\n"
        "    _record_terminal('failed', 'boom')\n"
        "    _record_terminal('loop_stopped', 'max_rounds_reached')\n"
    )
    assert _violations(source) == []


def test_a_helper_call_is_not_a_hit() -> None:
    """A helper that writes the terminal is a hit — that is the point.

    The call below sits in a helper that is not an allowed writer, so it
    IS a violation: the helper is exactly the kind of new exit the gate
    is meant to see.
    """
    source = (
        "def _close_the_plan(plan_id):\n"
        "    _record_verification_terminal(plan_id, 'passed', None)\n"
    )
    violations = _violations(source)
    assert len(violations) == 1, violations
    assert "in _close_the_plan" in violations[0], violations[0]

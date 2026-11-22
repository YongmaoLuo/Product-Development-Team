"""Execution and coverage checking for Tier A decision tables.

Background
----------
A Tier A decision table is a list of rows that pins what a judgment
function returns for a given input (§4.2 of the judgment-surface
design).  The table lives in the test file that owns the function, so
it runs on every commit — but a table is only worth running if it can
*fail*, and a table that cannot fail is exactly the failure mode §4.4
is written against.

This module is the machinery that gives a table the power to fail:

* :class:`Case` — one row: an id, the arguments, the expected value;
* :func:`assert_decision_table` — run the table, assert every row's
  value, and assert the table reaches every branch of the function;
* :func:`assert_full_branch_coverage` — the coverage half on its own.

The three rules it enforces are §4.4's first three:

1. **A concrete value.**  The expected value is compared by *type* as
   well as by equality, so ``0`` does not satisfy an expectation of
   ``False`` and ``1`` does not satisfy ``True``.  There is no way to
   write "the result is not None" here: a row states the value it
   expects, and a value that is not stated is a row that cannot fail.
2. **An id that says something.**  Every row carries an id, the ids
   are unique, and an id that is only a counter (``case1`` / ``情况2``)
   is rejected — a row whose name says nothing is a row nobody reads.
3. **Every branch.**  Each ``if`` body, each ``else`` branch and
   each ``return`` of the function has to be reached by at least one
   row.  A table that only walks the happy path leaves the rest of
   the function unpinned, and §4.3 wants that to be red.  An ``if``
   with no ``else`` has only its body to reach; the way out of it is
   pinned by the ``return`` that follows it.

Why the coverage is measured with a trace of our own
------------------------------------------------------
The design (§4.3) asks for "per-function branch coverage".  The
``coverage`` package can do that, but not from inside a test: it owns
``sys.settrace`` itself, and the CI unit lane already runs under
``pytest-cov``, so a second ``Coverage`` instance would fight the
outer one for the trace hook and leave the lane's numbers wrong.

So this module traces the one function it is asked about with
``sys.settrace``, restores the previous tracer in a ``finally``, and
reads the executed lines itself.  The measurement is per function and
per line, which is what the rule needs; the lane's own coverage
numbers keep working, minus the handful of lines executed inside the
traced call.

A trace function belongs to the thread that installed it, and an
``async`` judgment function may have to be driven in a worker thread
(``asyncio.run`` refuses to run inside a loop that is already
running, which is the case under ``pytest-asyncio``).  The tracer is
therefore carried into that worker and the worker's own tracer is put
back afterwards, so the measurement of a coroutine is the measurement
of an ordinary function.

Scope, stated honestly: this module **reads** the function's source
and runs the function.  It never writes to either.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import sys
import textwrap
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

__all__ = [
    "Case",
    "assert_decision_table",
    "assert_full_branch_coverage",
]

#: An id that is only a counter says nothing about why the row exists.
#: §4.4 requires every row to carry the *reason* it is there, so these
#: names are refused rather than merely discouraged.
_PLACEHOLDER_ID = re.compile(r"^(case|情况)\d+$")


@dataclass(frozen=True)
class Case:
    """One row of a decision table.

    ``id`` names the row and has to say why the row exists — a reader
    who has never seen the function must be able to tell what the row
    pins.  ``inputs`` are the positional arguments the function is
    called with; ``expected`` is the value it has to return, compared
    by type *and* by value.

    Both ``inputs`` and ``expected`` are required: a row that leaves
    the expected value out is a row that asserts nothing, which is
    the shape §4.4 forbids.
    """

    id: str
    inputs: tuple[Any, ...]
    expected: Any

    def __post_init__(self) -> None:
        """Accept any sequence of arguments and keep it as a tuple."""
        object.__setattr__(self, "inputs", tuple(self.inputs))


@dataclass(frozen=True)
class _Branch:
    """One thing the table has to reach.

    ``line`` is the first file line of the branch, ``end`` its last —
    both are file lines, not offsets into the parsed source, so the
    message a reader gets points at the file they can open.
    """

    kind: str
    line: int
    end: int
    detail: str


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------


def _validate_cases(func: Callable[..., Any], cases: Sequence[Case]) -> None:
    """Refuse a table that could not carry weight.

    The rules are §4.4's second one: every row has an id, the ids are
    unique, and an id that is only a counter is refused.  A table that
    breaks them is not a table that describes behaviour — it is a list
    of rows nobody can point at.
    """
    name = _name_of(func)
    problems: list[str] = []
    seen: dict[str, int] = {}

    for index, case in enumerate(cases):
        if not isinstance(case, Case):
            problems.append(
                f"row #{index} is not a Case: {case!r}"
            )
            continue
        if not isinstance(case.id, str) or not case.id.strip():
            problems.append(
                f"row #{index} has no id: every row must say why it is there"
            )
            continue
        if _PLACEHOLDER_ID.match(case.id):
            problems.append(
                f"row #{index} has the placeholder id {case.id!r}: an id has "
                "to say what the row pins, not how many rows there are"
            )
        if case.id in seen:
            problems.append(
                f"row #{index} repeats the id {case.id!r}, which row "
                f"#{seen[case.id]} already uses: ids are how a reader "
                "finds a row"
            )
        else:
            seen[case.id] = index

    if problems:
        raise AssertionError(
            f"the decision table for {name!r} cannot be used:\n  - "
            + "\n  - ".join(problems)
        )


def _same_value(actual: Any, expected: Any) -> bool:
    """Is ``actual`` the value the row expects — type and all?

    The type is part of the contract: a judgment function that returns
    ``0`` did not return ``False``, and a table that cannot tell the
    difference is a table that green-lights a wrong answer.  Equality
    alone would say ``0 == False`` is true.
    """
    if type(actual) is not type(expected):
        return False
    try:
        return bool(actual == expected)
    except Exception:  # a value whose comparison raises is not a match
        return False


def _assert_values_match(
    func: Callable[..., Any],
    cases: Sequence[Case],
    actuals: Sequence[Any],
) -> None:
    """Every row's value is the value the row expects."""
    problems: list[str] = []
    for case, actual in zip(cases, actuals):
        if not _same_value(actual, case.expected):
            problems.append(
                f"case {case.id!r}: expected {case.expected!r} "
                f"(type {type(case.expected).__name__}), got {actual!r} "
                f"(type {type(actual).__name__})"
            )
    if problems:
        raise AssertionError(
            f"{len(problems)} of {len(cases)} rows of the decision table "
            f"for {_name_of(func)!r} disagree with the function:\n  - "
            + "\n  - ".join(problems)
        )


# ---------------------------------------------------------------------------
# Running the table
# ---------------------------------------------------------------------------


async def _await(value: Any) -> Any:
    """Await ``value`` inside a coroutine, so any awaitable can be driven."""
    return await value


def _drive(awaitable: Any) -> Any:
    """Drive ``awaitable`` to completion, with or without a running loop.

    ``asyncio.run`` wants a thread with no running loop.  A test that
    runs inside ``pytest-asyncio`` has one, so the awaitable is handed
    to a worker thread that has a loop of its own.

    A trace function belongs to the thread that installed it, so the
    tracer :func:`_execute` set up would not follow the coroutine into
    the worker: every line of the function under test would look
    unreached and the table would report branches that were in fact
    walked.  The tracer is therefore carried into the worker, and the
    worker's own tracer — the CI lane's ``pytest-cov``, if there is one
    — is put back when the worker is done.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await(awaitable))

    outcome: dict[str, Any] = {}
    tracer = sys.gettrace()

    def _worker() -> None:
        previous = sys.gettrace()
        if tracer is not None:
            sys.settrace(tracer)
        try:
            outcome["value"] = asyncio.run(_await(awaitable))
        except BaseException as exc:  # re-raised on the calling thread
            outcome["error"] = exc
        finally:
            if tracer is not None:
                sys.settrace(previous)

    thread = threading.Thread(target=_worker, name="judgment-table-async")
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _call(func: Callable[..., Any], inputs: tuple[Any, ...]) -> Any:
    """Call ``func`` and give back its value, driving it if it is async."""
    result = func(*inputs)
    if inspect.isawaitable(result):
        return _drive(result)
    return result


def _execute(
    func: Callable[..., Any],
    cases: Sequence[Case],
) -> tuple[set[int], list[Any]]:
    """Run every row, and say which lines of ``func`` were reached.

    The tracer only follows ``func``'s own code object, so the
    measurement is the function's lines and not the lines of whatever
    it called.  The previous tracer — the CI lane's ``pytest-cov``, if
    there is one — is put back in the ``finally``, so the lane's own
    coverage keeps counting.

    An async ``func`` may be driven in another thread (see
    :func:`_drive`), and a trace function belongs to the thread that
    installed it, so the tracer is carried across with the coroutine.
    """
    code = getattr(func, "__code__", None)
    if code is None:
        raise TypeError(
            f"{_name_of(func)!r} has no code object; a decision table "
            "needs a plain function"
        )

    reached: set[int] = set()

    def _tracer(frame: Any, event: str, arg: Any) -> Any:
        if frame.f_code is code:
            if event == "line":
                reached.add(frame.f_lineno)
            return _tracer
        return None

    previous = sys.gettrace()
    sys.settrace(_tracer)
    try:
        actuals = [_call(func, tuple(case.inputs)) for case in cases]
    finally:
        sys.settrace(previous)
    return reached, actuals


# ---------------------------------------------------------------------------
# Reading the function's branches
# ---------------------------------------------------------------------------


def _function_node(func: Callable[..., Any]) -> tuple[ast.AST, int]:
    """The function's AST node, and the file line its source starts at.

    The source is read through :func:`inspect.getsourcelines`, so the
    node's line numbers are relative to the block that was read; the
    offset turns them back into file lines, which is what a reader
    needs in order to open the file and look.
    """
    try:
        source_lines, start = inspect.getsourcelines(func)
    except (OSError, TypeError) as exc:
        raise AssertionError(
            f"the source of {_name_of(func)!r} could not be read, so its "
            f"branches cannot be checked: {exc}"
        ) from exc
    tree = ast.parse(textwrap.dedent("".join(source_lines)))
    if not tree.body or not isinstance(
        tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        raise AssertionError(
            f"the source of {_name_of(func)!r} does not parse as a function "
            "definition"
        )
    return tree.body[0], start


def _span(node: ast.AST, offset: int) -> tuple[int, int]:
    """The file lines a statement covers."""
    first = offset + node.lineno - 1
    last = offset + (node.end_lineno or node.lineno) - 1
    return first, last


def _block_span(statements: list[ast.stmt], offset: int) -> tuple[int, int]:
    """The file lines a block of statements covers.

    A branch is reached when *one* of its lines was executed, and a
    block's first statement always runs when the block is entered — so
    the block's whole span says the same thing as its first line, and
    says it better: a ``return`` further down the block falls inside
    the span, so an unreached block is reported once instead of once
    per statement.
    """
    first = offset + statements[0].lineno - 1
    last = offset + (statements[-1].end_lineno or statements[-1].lineno) - 1
    return first, last


def _branches(node: ast.AST, offset: int) -> Iterator[_Branch]:
    """Every branch of ``node`` the table has to reach.

    Nested functions, lambdas and classes are not walked into: they
    are separate code objects, and the tracer follows only the
    function under test, so their branches would be reported as
    uncovered no matter what the table did.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            continue
        if isinstance(child, ast.If):
            if_line = offset + child.lineno - 1
            body_first, body_last = _block_span(child.body, offset)
            yield _Branch(
                kind="if-body",
                line=body_first,
                end=body_last,
                detail=f"the body of the 'if' at line {if_line}",
            )
            if child.orelse:
                else_first, else_last = _block_span(child.orelse, offset)
                yield _Branch(
                    kind="if-else",
                    line=else_first,
                    end=else_last,
                    detail=f"the 'else' of the 'if' at line {if_line}",
                )
        elif isinstance(child, ast.Return):
            first, last = _span(child, offset)
            yield _Branch(
                kind="return",
                line=first,
                end=last,
                detail="a 'return'",
            )
        yield from _branches(child, offset)


def _uncovered(
    func: Callable[..., Any], reached: set[int]
) -> list[_Branch]:
    """The branches of ``func`` that no row reached.

    A ``return`` that sits inside a branch which is already reported is
    the same gap said twice — the branch's wording says more than
    "a 'return'" does — so it is left out of the report.
    """
    node, offset = _function_node(func)
    missing = [
        branch
        for branch in _branches(node, offset)
        if not any(branch.line <= line <= branch.end for line in reached)
    ]
    spans = [
        (branch.line, branch.end)
        for branch in missing
        if branch.kind != "return"
    ]
    return [
        branch
        for branch in missing
        if branch.kind != "return"
        or not any(first <= branch.line <= last for first, last in spans)
    ]


def _assert_branches_covered(
    func: Callable[..., Any], reached: set[int]
) -> None:
    """Every branch of ``func`` was reached by at least one row."""
    missing = _uncovered(func, reached)
    if not missing:
        return
    name = _name_of(func)
    raise AssertionError(
        f"the decision table for {name!r} does not cover every branch of "
        f"{name!r}: {len(missing)} branch(es) were never reached:\n  - "
        + "\n  - ".join(f"line {b.line}: {b.detail}" for b in missing)
    )


def _name_of(func: Callable[..., Any]) -> str:
    """The function's name, for a message a reader can act on."""
    return getattr(func, "__name__", repr(func))


# ---------------------------------------------------------------------------
# What a table file calls
# ---------------------------------------------------------------------------


def assert_decision_table(
    func: Callable[..., Any], cases: Sequence[Case]
) -> None:
    """Assert that ``func`` behaves as ``cases`` says, on every row.

    Three things are checked, in this order:

    1. the table itself is usable — every row has an id, the ids are
       unique, and none of them is only a counter;
    2. every row's value is the value the row expects, compared by
       type as well as by equality;
    3. the rows reach every branch of ``func``.

    The third check is part of this call on purpose.  A table that
    only pins the values it happens to have tried is a table that
    lets a new branch in unnoticed, and §4.3 wants the table to be
    red the moment the function grows a branch.  Use
    :func:`assert_full_branch_coverage` when only the coverage half is
    wanted.
    """
    _validate_cases(func, cases)
    reached, actuals = _execute(func, cases)
    _assert_values_match(func, cases, actuals)
    _assert_branches_covered(func, reached)


def assert_full_branch_coverage(
    func: Callable[..., Any], cases: Sequence[Case]
) -> None:
    """Assert that ``cases`` reach every branch of ``func``.

    The rows are run to find out which lines they reach; their values
    are not judged here.  A branch is covered when at least one of its
    lines was executed: each ``if`` body, each ``else`` branch, and
    each ``return`` of the function.
    """
    _validate_cases(func, cases)
    reached, _ = _execute(func, cases)
    _assert_branches_covered(func, reached)

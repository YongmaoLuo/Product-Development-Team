"""Unit tests for the decision-table helper.

Background
----------
``backend/tests/judgment_tables.py`` is the machinery a Tier A
decision table is run with (§4.2 of the judgment-surface design).  A
table is only worth running if it can fail, and the three ways it can
fail are the three rules of §4.4:

1. the expected value is compared by type as well as by value, so a
   row cannot be satisfied by a value of the wrong shape;
2. every row has an id, the ids are unique, and an id that is only a
   counter is refused;
3. the rows reach every branch of the function under test, so a table
   that only walks the happy path is red.

What is pinned here
-------------------
1. ``test_duplicate_case_id_is_rejected`` — two rows cannot share an id.
2. ``test_value_mismatch_names_the_case_id`` — a wrong value is
   reported against the row that expected it.
3. ``test_missing_branch_is_reported`` — a branch no row reached is
   reported, with the file line it is on.
4. ``test_async_function_is_supported`` — a coroutine function is
   driven to completion, and its branches are measured like any other.

Around those four are the cases that keep the rules from decaying:
the empty id, the counter id, the type comparison, the order of the
rows, and a function that is defined in another module.

Three things §4.4 asks for that the four above do not reach on their
own are pinned at the end of the file:

* which *kind* of branch was missed — the body of an ``if`` and its
  ``else`` are two different gaps, and the report says which one it is
  (``test_the_unreached_if_body_is_reported_by_name``,
  ``test_the_unreached_else_is_reported_by_name``);
* that the helper gives the caller's tracer back, on the way out of a
  table and on the way out of an exception
  (``test_the_previous_tracer_is_restored_after_a_successful_table``,
  ``test_the_previous_tracer_is_restored_after_the_function_raises``);
* that a coroutine table is measured the same way when the caller is
  already inside an event loop
  (``test_an_async_table_is_measured_under_a_running_event_loop``).

Scope, stated honestly: this file **reads** the helper and the source
of the functions it defines.  It never writes to either.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

from tests.judgment_tables import (
    Case,
    assert_decision_table,
    assert_full_branch_coverage,
)


def _sentinel_tracer(frame: object, event: str, arg: object) -> None:
    """A tracer that records nothing — only its identity matters here.

    Whether the helper gives the caller's tracer back is the one thing
    a reader cannot check by reading a report: the report says which
    branches were missed, not what the helper left behind.  A sentinel
    with a known identity answers it — install it, run the table, and
    ask whether that very object is still installed.
    """
    return None


def _if_branch_lines(func: object) -> dict[str, int]:
    """The file lines of the ``if`` under test, its body and its ``else``.

    The helper reports an uncovered branch as "line N: <what it is>",
    so the test has to know N — and it has to know it from the source
    rather than from a number typed in by hand, or the assertion would
    drift the moment the fixture moved.  ``inspect.getsourcelines``
    gives the block the function was defined in and the line that
    block starts on, ``ast`` gives the statements inside the ``if``,
    and the two together are the line in the file.
    """
    source_lines, start = inspect.getsourcelines(func)
    tree = ast.parse(textwrap.dedent("".join(source_lines)))
    function = tree.body[0]
    if_node = next(
        node
        for node in ast.iter_child_nodes(function)
        if isinstance(node, ast.If)
    )
    return {
        "if": start + if_node.lineno - 1,
        "body": start + if_node.body[0].lineno - 1,
        "else": start + if_node.orelse[0].lineno - 1,
    }


def _return_lines(func: object) -> dict[str, int]:
    """The file line of each constant ``func`` returns, keyed by that constant.

    The helper reports an uncovered branch by its file line, so the
    test has to know which line that is — and it has to know it from
    the source rather than from a number typed in by hand, or the
    assertion would drift the moment the fixture moved.  The constants
    are the key because ``ast.walk`` yields nodes breadth-first, so
    the two ``return`` statements of a fixture do not come out in
    source order.
    """
    source_lines, start = inspect.getsourcelines(func)
    tree = ast.parse(textwrap.dedent("".join(source_lines)))
    lines: dict[str, int] = {}
    for node in ast.walk(tree.body[0]):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant):
            lines[str(node.value.value)] = start + node.lineno - 1
    return lines


# ---------------------------------------------------------------------------
# The four cases the material names
# ---------------------------------------------------------------------------


def test_duplicate_case_id_is_rejected() -> None:
    """Two rows cannot share an id.

    The id is how a reader finds the row that failed, and how a
    reviewer reads the table's intent.  Two rows with one id make both
    of those impossible: the failure report would name a row that
    exists twice, and the table would say the same thing twice.
    """
    def f(x: int) -> bool:
        return x > 0

    cases = [
        Case(id="positive-is-true", inputs=(1,), expected=True),
        Case(id="positive-is-true", inputs=(2,), expected=True),
    ]

    with pytest.raises(AssertionError) as excinfo:
        assert_decision_table(f, cases)

    assert "positive-is-true" in str(excinfo.value)


def test_value_mismatch_names_the_case_id() -> None:
    """A wrong value is reported against the row that expected it.

    A failure that says "the function returned the wrong thing" sends
    the reader looking for a bug in the function.  A failure that names
    the row sends the reader to the row, which is where the intent is
    written down.
    """
    def f(x: int) -> bool:
        return x > 0

    cases = [Case(id="negative-is-false", inputs=(-1,), expected=True)]

    with pytest.raises(AssertionError) as excinfo:
        assert_decision_table(f, cases)

    message = str(excinfo.value)
    assert "negative-is-false" in message
    assert "True" in message
    assert "False" in message


def test_missing_branch_is_reported() -> None:
    """A branch no row reached is reported, with the line it is on.

    This is the rule that stops a table from shrinking to the happy
    path: the function's second ``return`` is never reached, so the
    table does not yet pin what the function does when the condition
    is false.  The report names the file line, because "some branch
    was missed" is not something anybody can act on.
    """
    def f(x: int) -> str:
        if x:
            return "yes"
        return "no"

    cases = [Case(id="positive-is-yes", inputs=(1,), expected="yes")]

    with pytest.raises(AssertionError) as excinfo:
        assert_full_branch_coverage(f, cases)

    message = str(excinfo.value)
    returns = _return_lines(f)
    assert set(returns) == {"yes", "no"}
    uncovered = returns["no"]
    assert f"line {uncovered}" in message
    assert "never reached" in message

    # The same gap is reported by the table call, which checks the
    # coverage as well as the values.
    with pytest.raises(AssertionError) as table_excinfo:
        assert_decision_table(f, cases)

    assert f"line {uncovered}" in str(table_excinfo.value)


def test_async_function_is_supported() -> None:
    """A coroutine function is driven to completion and measured.

    Some judgment points are async.  A helper that only knew how to
    call a function would compare a coroutine object with the expected
    value and report a mismatch that is really a missing ``await``.
    """
    async def f(x: int) -> str:
        if x:
            return "yes"
        return "no"

    cases = [
        Case(id="async-positive-is-yes", inputs=(1,), expected="yes"),
        Case(id="async-zero-is-no", inputs=(0,), expected="no"),
    ]

    assert_decision_table(f, cases)


# ---------------------------------------------------------------------------
# The rules that keep the three above from decaying
# ---------------------------------------------------------------------------


def test_empty_case_id_is_rejected() -> None:
    """A row without an id cannot be pointed at, so it is refused."""
    def f(x: int) -> bool:
        return x > 0

    cases = [Case(id="", inputs=(1,), expected=True)]

    with pytest.raises(AssertionError) as excinfo:
        assert_decision_table(f, cases)

    assert "no id" in str(excinfo.value)


def test_placeholder_case_id_is_rejected() -> None:
    """An id that is only a counter says nothing, so it is refused.

    ``case1`` / ``情况2`` are what a table looks like before anybody
    has written down what the rows are for.  §4.4 asks the id to
    carry the reason the row exists.
    """
    def f(x: int) -> bool:
        return x > 0

    for placeholder in ("case1", "情况2"):
        cases = [Case(id=placeholder, inputs=(1,), expected=True)]

        with pytest.raises(AssertionError) as excinfo:
            assert_decision_table(f, cases)

        assert placeholder in str(excinfo.value)


def test_a_descriptive_id_is_accepted() -> None:
    """The counter rule does not swallow ids that merely start the same way."""
    def f(x: int) -> bool:
        return x > 0

    cases = [Case(id="case-1-is-allowed", inputs=(1,), expected=True)]

    assert_decision_table(f, cases)


def test_expected_value_is_compared_by_type() -> None:
    """``0`` is not ``False``: the type is part of the expectation.

    A judgment function that returns ``0`` has not returned ``False``,
    and a table that cannot tell the difference is a table that
    green-lights a wrong answer.  Equality alone would accept it.
    """
    def f(x: int) -> int:
        return x

    cases = [Case(id="zero-is-zero", inputs=(0,), expected=False)]

    with pytest.raises(AssertionError) as excinfo:
        assert_decision_table(f, cases)

    message = str(excinfo.value)
    assert "zero-is-zero" in message
    assert "int" in message
    assert "bool" in message


def test_the_table_does_not_depend_on_row_order() -> None:
    """Which row fails does not depend on where the row sits.

    The CI shards shuffle the execution order and every file is run
    beside other files, so nothing may depend on the order of the
    rows — and least of all the report, which is what a reader reads
    at three in the morning.
    """
    def f(x: int) -> bool:
        return x > 0

    cases = [
        Case(id="positive-is-true", inputs=(1,), expected=True),
        Case(id="negative-is-false", inputs=(-1,), expected=True),
    ]

    with pytest.raises(AssertionError) as first:
        assert_decision_table(f, cases)

    with pytest.raises(AssertionError) as second:
        assert_decision_table(f, list(reversed(cases)))

    assert "negative-is-false" in str(first.value)
    assert "negative-is-false" in str(second.value)


def test_a_function_from_another_module_is_supported(tmp_path: Path) -> None:
    """The helper reads the function's own source, wherever it lives.

    A judgment point is a function in the production tree, not in the
    test file that pins it.  The helper therefore has to find the
    source by the function itself, not by assuming it was defined
    next to the table.
    """
    module_path = tmp_path / "elsewhere.py"
    module_path.write_text(
        "def classify(value):\n"
        "    if value > 0:\n"
        "        return 'positive'\n"
        "    return 'not-positive'\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location(
        "judgment_table_elsewhere", module_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cases = [
        Case(id="positive-is-positive", inputs=(1,), expected="positive"),
        Case(id="zero-is-not-positive", inputs=(0,), expected="not-positive"),
    ]

    assert_decision_table(module.classify, cases)


def test_an_async_mismatch_names_the_case_id() -> None:
    """The async path reports the row the same way the sync path does."""
    async def f(x: int) -> bool:
        return x > 0

    cases = [Case(id="async-negative-is-false", inputs=(-1,), expected=True)]

    with pytest.raises(AssertionError) as excinfo:
        assert_decision_table(f, cases)

    assert "async-negative-is-false" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Which kind of branch was missed
# ---------------------------------------------------------------------------


def test_the_unreached_if_body_is_reported_by_name() -> None:
    """The body of an ``if`` that no row reached is named as such.

    "Some branch was missed" is not something anybody can act on.  The
    report has to say *which* branch — the body of the ``if`` — and
    where it is, so that the row that should have reached it can be
    written.  The line numbers come out of the source, not out of a
    number typed in by hand, and the wording is pinned exactly.
    """
    def f(x: int) -> str:
        if x > 0:
            return "positive"
        else:
            return "not-positive"

    lines = _if_branch_lines(f)
    cases = [
        Case(
            id="negative-input-takes-the-else-branch",
            inputs=(-1,),
            expected="not-positive",
        )
    ]

    with pytest.raises(AssertionError) as excinfo:
        assert_full_branch_coverage(f, cases)

    message = str(excinfo.value)
    assert "1 branch(es) were never reached" in message
    assert (
        f"line {lines['body']}: the body of the 'if' at line {lines['if']}"
        in message
    )


def test_the_unreached_else_is_reported_by_name() -> None:
    """The ``else`` of an ``if`` that no row reached is named as such.

    The ``else`` is a gap of its own: the rows walked into the body of
    the ``if`` and never came out the other side, so the function's
    other half is unpinned.  The report says it is the ``else`` and
    gives the line it is on — not merely "a branch".
    """
    def f(x: int) -> str:
        if x > 0:
            return "positive"
        else:
            return "not-positive"

    lines = _if_branch_lines(f)
    cases = [
        Case(
            id="positive-input-takes-the-if-branch",
            inputs=(1,),
            expected="positive",
        )
    ]

    with pytest.raises(AssertionError) as excinfo:
        assert_full_branch_coverage(f, cases)

    message = str(excinfo.value)
    assert "1 branch(es) were never reached" in message
    assert (
        f"line {lines['else']}: the 'else' of the 'if' at line {lines['if']}"
        in message
    )


# ---------------------------------------------------------------------------
# What the helper leaves behind
# ---------------------------------------------------------------------------


def test_the_previous_tracer_is_restored_after_a_successful_table() -> None:
    """The tracer that was installed before the table ran comes back.

    The CI lane counts the lines of the suite with its own tracer, and
    the helper measures a function by putting a tracer of its own in
    place.  A helper that swapped in its tracer and left it there
    would silence the lane's coverage for everything that ran after
    it — so the previous tracer has to be put back, and the test proves
    it by identity: the sentinel installed before the table ran is the
    one installed after it.
    """
    def f(x: int) -> bool:
        return x > 0

    cases = [Case(id="positive-is-true", inputs=(1,), expected=True)]

    previous = sys.gettrace()
    sys.settrace(_sentinel_tracer)
    try:
        assert_decision_table(f, cases)
        assert sys.gettrace() is _sentinel_tracer
    finally:
        sys.settrace(previous)


def test_the_previous_tracer_is_restored_after_the_function_raises() -> None:
    """A function that raises still leaves the tracer as it found it.

    The restore lives in a ``finally``, so it has to run on the way out
    of an exception too — otherwise a judgment function that raises
    would take the lane's coverage down with it for every test that
    runs after it.
    """
    def f(x: int) -> bool:
        raise ValueError("the judgment function refused the input")

    cases = [Case(id="raising-input-is-reported", inputs=(1,), expected=True)]

    previous = sys.gettrace()
    sys.settrace(_sentinel_tracer)
    try:
        with pytest.raises(ValueError) as excinfo:
            assert_decision_table(f, cases)

        assert str(excinfo.value) == "the judgment function refused the input"
        assert sys.gettrace() is _sentinel_tracer
    finally:
        sys.settrace(previous)


# ---------------------------------------------------------------------------
# A coroutine table under a running event loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_async_table_is_measured_under_a_running_event_loop() -> None:
    """A coroutine table is measured even when a loop is already running.

    ``pytest-asyncio`` runs this case inside an event loop, so
    ``asyncio.run`` is not available and the coroutine is driven in a
    worker thread instead.  A trace function belongs to the thread
    that installed it, so the branches the coroutine really walked
    would look unreached and the table would report gaps that are not
    there.  Both branches are covered by the rows below, so the report
    has to come back empty.
    """
    async def f(x: int) -> str:
        if x > 0:
            return "yes"
        else:
            return "no"

    cases = [
        Case(id="async-positive-is-yes", inputs=(1,), expected="yes"),
        Case(id="async-zero-is-no", inputs=(0,), expected="no"),
    ]

    assert_decision_table(f, cases)


@pytest.mark.asyncio
async def test_an_unreached_async_branch_is_reported_under_a_loop() -> None:
    """Under a running loop the report still names the branch it missed.

    The measurement has to be the same as the synchronous one: the row
    below walks into the body of the ``if`` and never reaches its
    ``else``, so exactly one branch is missing and the report says
    which one — the ``else``, on the line the source puts it on.
    """
    async def f(x: int) -> str:
        if x > 0:
            return "yes"
        else:
            return "no"

    lines = _if_branch_lines(f)
    cases = [Case(id="async-positive-is-yes", inputs=(1,), expected="yes")]

    with pytest.raises(AssertionError) as excinfo:
        assert_decision_table(f, cases)

    message = str(excinfo.value)
    assert "1 branch(es) were never reached" in message
    assert (
        f"line {lines['else']}: the 'else' of the 'if' at line {lines['if']}"
        in message
    )

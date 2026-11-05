"""
Tests for the description-reference parser.

Background
----------
A real plan (``20261104-CPU``, task 5) wrote its prerequisite as a
bare range::

    前置条件：任务 1-4 已完成。

The parser took every id-shaped token after the ``任务`` keyword and
treated it as a literal task id, so ``1-4`` became the id ``"1-4"``.
``validate_desc_consistency`` then compared that id against the task's
dependency closure, found nothing, and aborted the whole plan::

    Task 5 desc 提到前置条件 1-4 但 depends_on 是 ['1','2','3','4']

That was PRD defect 2.  The fix is **a range is a range**: "1-4" (and
"1–4" and "1~4") is expanded into the ids it spans, whether it is
written bare or inside braces.  ``{1-4}`` and ``任务 1-4`` both name
tasks 1, 2, 3 and 4.

The rule that replaced it for one day — "only a brace group is a task
reference" — silenced the whole check instead of fixing the range
bug: a desc that named a prerequisite it did not depend on was
accepted, and the load-time gate stopped catching anything at all.
Bare ids are references again ("前置条件：任务 1 已完成" → ``["1"]``),
and a bare range is expanded rather than ignored.

Nothing else is relaxed: an id that is not in the plan is still
rejected — "``{9}``", "``任务 9``" and a range whose member is not a
dependency ("``{1-4}``" while the task does not depend on 4) all fail.

TDD spec — the contract tests
------------------------------
1. ``test_bare_range_expands``:
   bare ``1-4`` → ``['1','2','3','4']``, and passes with the four deps.
2. ``test_bare_range_missing_member_rejected``:
   bare ``1-4`` while the task depends on 1 only → ``ValueError``.
3. ``test_braced_range_expands``:
   ``{1-4}`` → ``['1','2','3','4']``.
4. ``test_braced_missing_dep_rejected``:
   ``{1-4}`` while the task does not depend on 4 → ``ValueError``.
5. ``test_braced_single_missing_rejected``:
   ``{2}`` while the task does not depend on 2 → ``ValueError``.
6. ``test_comma_list_expands``:
   ``{2,5}`` → ``['2','5']``.
7. ``test_hierarchical_id_not_expanded``:
   ``{1-2}`` with ``1-2`` in ``known_ids`` → ``['1-2']``.
"""

import sys
from pathlib import Path
from typing import List, Optional

import pytest

# Ensure backend/ is on sys.path so ``import agent`` works regardless of
# which test runner entry point is used.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from task import SubTask  # noqa: E402
from agent import _extract_desc_dep_ids, validate_desc_consistency  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str,
    description: str,
    depends_on: Optional[List[str]] = None,
) -> SubTask:
    """Build a minimal SubTask with the given id / description / depends_on.

    Only the fields read by the desc-reference parser and by
    ``validate_desc_consistency`` (``id``, ``description``,
    ``depends_on``) are populated.
    """
    return SubTask(
        id=task_id,
        title=f"task {task_id}",
        description=description,
        test_command="",
        status="pending",
        depends_on=list(depends_on) if depends_on is not None else [],
    )


#: The id set of a five-task plan — the ``known_ids`` the validator
#: hands to the parser so it can tell a hierarchical id from a range.
KNOWN_IDS = {"1", "2", "3", "4", "5"}


# ---------------------------------------------------------------------------
# Test 1: a bare range is expanded into its members
# ---------------------------------------------------------------------------


def test_bare_range_expands():
    """bare ``1-4`` → ``['1','2','3','4']``.

    The exact shape that aborted plan ``20261104-CPU``: the desc says
    ``前置条件：任务 1-4 已完成`` while ``depends_on`` carries the four
    ids separately.  The range is expanded into the ids it spans, so
    the four declared edges satisfy it.
    """
    desc = "前置条件：任务 1-4 已完成。"

    assert _extract_desc_dep_ids(desc, known_ids=KNOWN_IDS) == [
        "1",
        "2",
        "3",
        "4",
    ], "a bare range must be expanded into its members"

    task = _make_task(
        task_id="5",
        description=desc,
        depends_on=["1", "2", "3", "4"],
    )
    result = validate_desc_consistency([task])
    assert result is None, (
        f"a fully-satisfied bare range must pass, got result={result!r}"
    )


def test_bare_range_without_task_keyword_expands():
    """``前置条件：1-4`` (no ``任务`` keyword) is expanded too.

    The PRD's own wording for defect 2 is "前置条件 1-4" — the id list
    may sit right behind the trigger's colon.
    """
    desc = "前置条件：1-4 已完成。"

    assert _extract_desc_dep_ids(desc, known_ids=KNOWN_IDS) == [
        "1",
        "2",
        "3",
        "4",
    ]


def test_bare_range_missing_member_rejected():
    """bare ``1-4`` while the task depends on 1 only → ``ValueError``.

    The negative control for the bare range: expanding a range must
    not turn into a way to smuggle an unsatisfied prerequisite past
    the check.  The same rule applies to the braced spelling.
    """
    task = _make_task(
        task_id="5",
        description="前置条件：任务 1-4 已完成。",
        depends_on=["1"],
    )

    with pytest.raises(ValueError) as exc_info:
        validate_desc_consistency([task])

    msg = str(exc_info.value)
    assert "2" in msg, (
        f"expected a missing member of the expanded range to be named, "
        f"got: {msg!r}"
    )
    assert "5" in msg, (
        f"expected the offending task id '5' to be named, got: {msg!r}"
    )


def test_bare_single_id_is_a_reference():
    """A bare id names a prerequisite — the check is not silenced.

    "前置条件：任务 1 已完成" with ``depends_on == []`` is the drift the
    load-time gate exists to catch, so the bare id is parsed and the
    missing dependency is reported.
    """
    desc = "前置条件：任务 1 已完成。"

    assert _extract_desc_dep_ids(desc, known_ids=KNOWN_IDS) == ["1"], (
        "a bare id is a task reference"
    )

    task = _make_task(task_id="9", description=desc, depends_on=[])
    with pytest.raises(ValueError) as exc_info:
        validate_desc_consistency([task])

    msg = str(exc_info.value)
    assert "1" in msg, (
        f"expected the missing dependency '1' to be named, got: {msg!r}"
    )
    assert "9" in msg, (
        f"expected the offending task id '9' to be named, got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: a braced range expands into its members
# ---------------------------------------------------------------------------


def test_braced_range_expands():
    """``{1-4}`` → ``['1','2','3','4']``.

    The brace form is an explicit declaration, so the range is
    expanded into the ids it names instead of being read as one
    literal id (``"1-4"``) that exists in no plan.
    """
    desc = "前置条件：任务 {1-4} 已完成。"

    assert _extract_desc_dep_ids(desc, known_ids=KNOWN_IDS) == [
        "1",
        "2",
        "3",
        "4",
    ]


def test_braced_range_passes_when_every_member_is_a_dependency():
    """``{1-4}`` with deps 1..4 → no error."""
    task = _make_task(
        task_id="5",
        description="前置条件：任务 {1-4} 已完成。",
        depends_on=["1", "2", "3", "4"],
    )
    result = validate_desc_consistency([task])
    assert result is None, (
        f"a fully-satisfied braced range must pass, got result={result!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: a braced range whose member is missing is still rejected
# ---------------------------------------------------------------------------


def test_braced_missing_dep_rejected():
    """``{1-4}`` while the task does not depend on 4 → ``ValueError``.

    The brace side is NOT relaxed: expanding the range must not turn
    into a way to smuggle an unsatisfied prerequisite past the check.
    """
    task = _make_task(
        task_id="5",
        description="前置条件：任务 {1-4} 已完成。",
        depends_on=["1", "2", "3"],
    )

    with pytest.raises(ValueError) as exc_info:
        validate_desc_consistency([task])

    msg = str(exc_info.value)
    assert "4" in msg, (
        f"expected the missing dependency '4' to be named, got: {msg!r}"
    )
    assert "5" in msg, (
        f"expected the offending task id '5' to be named, got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: a braced id that is not a dependency is still rejected
# ---------------------------------------------------------------------------


def test_braced_single_missing_rejected():
    """``{2}`` while the task does not depend on 2 → ``ValueError``."""
    task = _make_task(
        task_id="3",
        description="依赖：任务 {2} 已完成。",
        depends_on=[],
    )

    with pytest.raises(ValueError) as exc_info:
        validate_desc_consistency([task])

    msg = str(exc_info.value)
    assert "2" in msg, (
        f"expected the missing dependency '2' to be named, got: {msg!r}"
    )
    assert "3" in msg, (
        f"expected the offending task id '3' to be named, got: {msg!r}"
    )


def test_braced_unknown_id_rejected():
    """``{9}`` — an id that is in no plan — is still rejected.

    The relaxation applies to the BARE side only.  A brace is an
    explicit declaration, so an id it names that does not exist in the
    plan is still a hard error.
    """
    task = _make_task(
        task_id="3",
        description="前置条件：任务 {9} 已完成。",
        depends_on=[],
    )

    with pytest.raises(ValueError) as exc_info:
        validate_desc_consistency([task])

    assert "9" in str(exc_info.value), (
        f"expected the unknown id '9' to be named, got: {exc_info.value!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: a braced comma list expands
# ---------------------------------------------------------------------------


def test_comma_list_expands():
    """``{2,5}`` → ``['2','5']``."""
    desc = "前置条件：任务 {2,5} 已完成。"

    assert _extract_desc_dep_ids(desc, known_ids=KNOWN_IDS) == ["2", "5"]


def test_comma_list_passes_when_every_member_is_a_dependency():
    """``{2,5}`` with deps 2 and 5 → no error."""
    task = _make_task(
        task_id="6",
        description="前置条件：任务 {2,5} 已完成。",
        depends_on=["2", "5"],
    )
    result = validate_desc_consistency([task])
    assert result is None, (
        f"a satisfied braced comma list must pass, got result={result!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: a hierarchical id inside braces is not expanded
# ---------------------------------------------------------------------------


def test_hierarchical_id_not_expanded():
    """``{1-2}`` with ``1-2`` in ``known_ids`` → ``['1-2']``.

    ``1-2`` is a legal task id in this project (the ``<parent>-<N>``
    convention), so it must be compared as a whole.  Expanding it into
    ``['1','2']`` would check two ids the plan never declared.
    """
    desc = "前置条件：任务 {1-2} 已完成。"

    assert _extract_desc_dep_ids(desc, known_ids={"1-2", "3"}) == ["1-2"], (
        "a hierarchical id that exists in the plan must stay whole"
    )


def test_hierarchical_id_passes_when_it_is_a_dependency():
    """``{1-2}`` with ``depends_on == ["1-2"]`` → no error.

    The plan's id set holds the task ids *and* the ids its dependency
    edges name, so ``1-2`` stays whole even when no task of its own
    carries that id (a child of a refiner split depends on its parent
    id).  Reading it as a range would expand it into two ids the
    graph never declared and reject a consistent plan.
    """
    task = _make_task(
        task_id="1-2-1",
        description="前置条件：任务 {1-2} 已完成。",
        depends_on=["1-2"],
    )
    result = validate_desc_consistency([task])
    assert result is None, (
        f"a satisfied hierarchical reference must pass, got result={result!r}"
    )


def test_unknown_hierarchical_id_expands_as_a_range():
    """``{1-2}`` with no ``1-2`` in the plan → expanded to 1 and 2.

    Without a matching id in the plan, ``1-2`` can only be a range, so
    it names the two tasks ``1`` and ``2``.
    """
    desc = "前置条件：任务 {1-2} 已完成。"

    assert _extract_desc_dep_ids(desc, known_ids={"1", "2", "3"}) == ["1", "2"]


# ---------------------------------------------------------------------------
# Boundary: the trigger word still gates the whole thing
# ---------------------------------------------------------------------------


def test_no_trigger_word_returns_empty():
    """Braces without a trigger word are not a prerequisite declaration.

    The trigger (``前置条件`` / ``依赖`` + a colon) says "this is a
    prerequisite declaration"; without it, a brace group is just text.
    """
    desc = "本任务把 {1,2} 的实现合并进主分支。"

    assert _extract_desc_dep_ids(desc, known_ids=KNOWN_IDS) == []


def test_braces_outside_the_trigger_sentence_are_ignored():
    """A code block after the trigger sentence is not a reference.

    The parser only looks inside the trigger's own sentence, so a
    fenced code block further down the description cannot inject
    phantom prerequisites.
    """
    desc = (
        "## 背景\n"
        "前置条件：任务 {1} 已完成。\n"
        "\n"
        "```python\n"
        "deps = {1, 2}\n"
        "```\n"
    )

    assert _extract_desc_dep_ids(desc, known_ids=KNOWN_IDS) == ["1"], (
        "only the trigger sentence's braces may be parsed"
    )


# ---------------------------------------------------------------------------
# Boundary: ranges that cannot be expanded keep their literal token
# ---------------------------------------------------------------------------


def test_backwards_range_keeps_literal_token():
    """``{5-2}`` (a >= b) is kept as the literal token ``'5-2'``.

    There is nothing to expand, so the token stays exactly as written
    and is then rejected as an id that exists in no plan.
    """
    desc = "前置条件：任务 {5-2} 已完成。"

    assert _extract_desc_dep_ids(desc, known_ids={"2", "5"}) == ["5-2"]


def test_oversized_range_keeps_literal_token():
    """``{1-500}`` (span > 200) is kept as the literal token ``'1-500'``.

    Expanding it would name 500 ids; the cap keeps the parser bounded
    and leaves the token to be rejected like any unknown id.
    """
    desc = "前置条件：任务 {1-500} 已完成。"

    assert _extract_desc_dep_ids(desc, known_ids={"1", "500"}) == ["1-500"]


def test_unexpandable_range_is_rejected_by_the_validator():
    """The literal token of an unexpandable range is still an unknown id."""
    task = _make_task(
        task_id="9",
        description="前置条件：任务 {5-2} 已完成。",
        depends_on=[],
    )

    with pytest.raises(ValueError) as exc_info:
        validate_desc_consistency([task])

    assert "5-2" in str(exc_info.value), (
        f"expected the literal token '5-2' to be named, got: {exc_info.value!r}"
    )


# ---------------------------------------------------------------------------
# Boundary: without known_ids the parser cannot tell id from range
# ---------------------------------------------------------------------------


def test_without_known_ids_tokens_stay_literal():
    """No ``known_ids`` → every token is kept as written.

    Without the plan's id set the parser cannot tell a hierarchical id
    (``1-2``) from a range (``1-4``), so it keeps both literal.  The
    validator always passes the id set, so this only affects callers
    that ask for a bare parse.
    """
    desc = "前置条件：任务 {1-2} 已完成。"

    assert _extract_desc_dep_ids(desc) == ["1-2"]
    assert _extract_desc_dep_ids("前置条件：任务 {1-4} 已完成。") == ["1-4"]

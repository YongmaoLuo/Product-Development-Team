"""Lock contention and undeclared writes are never split (2026-10-19).

Two failure classes joined the split gate's kind table, and both of them
answer the gate's question NO for every occurrence count.

Why that matters
----------------
The gate's premise is "the same failure twice means the task is too big
for one subagent". That premise is simply false for these two:

* ``lock_conflict`` — two tasks wanted the same file. The children of a
  split inherit their parents' declarations, so they want the same file
  again; splitting reproduces the contention one level deeper and
  produces MORE tasks in the pile-up, not fewer.
* ``undeclared_write`` — the task reached for a path its
  ``files_to_modify`` never named. Splitting carries the same incomplete
  declaration into every child, so all of them reach for the same
  undeclared path. This one is not un-splittable for symmetry's sake: it
  has a precise one-path fix, which is why ``detail`` carries the refused
  path and the refiner is told to declare it.

The ordering of the rules is load-bearing and is pinned here too: a file
lock TIMEOUT contains the word "timeout", so a check placed after
``infra`` would file every contention as a generic environment hiccup —
a failure class the refiner is explicitly told is fine to keep retrying.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from failure_classification import (  # noqa: E402
    INFRA,
    LOCK_CONFLICT,
    UNDECLARED_WRITE,
    classify_failure,
    is_split_allowed,
)


# ---------------------------------------------------------------------------
# the two kinds
# ---------------------------------------------------------------------------
def test_a_file_lock_deadlock_is_a_lock_conflict():
    failure = classify_failure(
        "File lock deadlock while acquiring backend/a.py: another task "
        "holds it and is itself waiting on a file this task holds"
    )
    assert failure.kind == LOCK_CONFLICT


def test_a_file_lock_timeout_is_a_lock_conflict_not_infra():
    """The ordering trap, as a test.

    "File lock timeout after 300s" contains "Timeout", so a check placed
    after ``infra`` classifies a contention as an environment problem.
    ``infra`` is the one kind the refiner is told to retry freely, so
    getting this wrong sends a permanently-contended task back into the
    queue instead of reporting it.
    """
    failure = classify_failure(
        "File lock timeout after 300s: backend/a.py is held by another "
        "task or process"
    )
    assert failure.kind == LOCK_CONFLICT, (
        "a lock timeout is a contention, not an infrastructure problem"
    )
    assert failure.kind != INFRA


def test_the_hook_busy_message_is_a_lock_conflict():
    """The refusal the sub-agent actually sees mid-task."""
    failure = classify_failure(
        "[pdt] another task is editing this file right now: "
        "/w/backend/a.py -- wait for it to finish and retry this edit."
    )
    assert failure.kind == LOCK_CONFLICT


def test_an_undeclared_write_is_its_own_kind():
    failure = classify_failure("undeclared_write: /w/backend/b.py")
    assert failure.kind == UNDECLARED_WRITE


def test_the_guard_refusal_sentence_is_an_undeclared_write():
    """The shape that arrives when the sub-agent DID relay the message."""
    failure = classify_failure(
        "[pdt] refusing to edit a file this task did not declare: "
        "/w/backend/b.py. This is not a lock conflict and waiting will "
        "not clear it. The task's files_to_modify must list this path."
    )
    assert failure.kind == UNDECLARED_WRITE


# ---------------------------------------------------------------------------
# neither may ever be split
# ---------------------------------------------------------------------------
def test_neither_lock_kind_is_split_allowed_even_on_a_repeat():
    """The gate must stay shut for a SECOND occurrence too.

    This is the assertion that would break if someone wired the new kinds
    into ``occurrence >= 2 and ...`` without the exclusion: a repeat is
    exactly when the gate would otherwise open, and for these two kinds
    opening it is the failure mode.
    """
    for error in (
        "File lock deadlock while acquiring backend/a.py",
        "File lock timeout after 300s: backend/a.py is held by another task",
        "[pdt] another task is editing this file right now: /w/a.py",
        "undeclared_write: /w/backend/b.py",
        "[pdt] refusing to edit a file this task did not declare: "
        "/w/backend/b.py. This is not a lock conflict and waiting will "
        "not clear it.",
    ):
        failure = classify_failure(error)
        assert not is_split_allowed(failure), (
            f"{failure.kind} must never open the split gate, even on a "
            f"second occurrence: {error!r}"
        )


def test_split_allowed_still_follows_the_occurrence_rule_for_other_kinds():
    """The new kinds must not have closed the gate for everything.

    ``is_split_allowed`` is about the KIND; the occurrence count stays
    the executor's rule. A real test failure remains splittable, which is
    the one case splitting is for.
    """
    failure = classify_failure("AssertionError: expected 3 got 4")
    assert is_split_allowed(failure), (
        "a genuine test failure is still the case splitting exists for"
    )


def test_an_infra_failure_is_still_not_split_allowed():
    failure = classify_failure("HARD TIMEOUT after 3600s")
    assert not is_split_allowed(failure)


# ---------------------------------------------------------------------------
# detail: the machine-readable payload
# ---------------------------------------------------------------------------
def test_detail_carries_the_path_from_the_executor_spelling():
    """``undeclared_write: <path>`` — what the executor itself writes."""
    failure = classify_failure("undeclared_write: /w/backend/b.py")
    assert failure.detail == "/w/backend/b.py", failure.detail


def test_detail_carries_the_path_from_the_guard_original_wording():
    """The other shape: the refusal text as the sub-agent relayed it.

    Both shapes have to yield the path, because which one arrives is not
    something the executor controls — it depends on whether the sub-agent
    quoted the hook's stderr.
    """
    failure = classify_failure(
        "[pdt] refusing to edit a file this task did not declare: "
        "/w/backend/b.py. This is not a lock conflict and waiting will "
        "not clear it. The task's files_to_modify must list this path "
        "(relative to the project dir) if it really needs to change it."
    )
    assert failure.detail == "/w/backend/b.py", failure.detail


def test_detail_carries_every_refused_path():
    """The executor joins up to five; all of them are the diagnosis."""
    failure = classify_failure(
        "undeclared_write: /w/a.py, /w/b.py, /w/c.py"
    )
    assert failure.detail == "/w/a.py, /w/b.py, /w/c.py", failure.detail


def test_detail_is_empty_for_kinds_that_have_none():
    for error in (
        "File lock deadlock while acquiring backend/a.py",
        "empty_diff_no_changes",
        "AssertionError: expected 3 got 4",
        "",
    ):
        assert classify_failure(error).detail == "", error


def test_the_detail_is_kept_out_of_the_signature():
    """Two refusals of DIFFERENT files are two different failures.

    The signature is the occurrence counter's key. If the path leaked
    into it, a task that was refused for file A and later for file B would
    be counted as two FIRST occurrences, and neither would ever reach the
    gate — which for an un-splittable kind is the one outcome that still
    has to be right.
    """
    one = classify_failure("undeclared_write: /w/a.py")
    two = classify_failure("undeclared_write: /w/b.py")
    assert one.signature != two.signature
    assert one.detail != two.detail
    again = classify_failure("undeclared_write: /w/a.py")
    assert again.signature == one.signature, (
        "the same refused path must hash to the same signature"
    )


# ---------------------------------------------------------------------------
# what actually reaches the refiner
# ---------------------------------------------------------------------------
def _refiner_block(failure, occurrence):
    """Drive the real ``_refine_after_failure`` and return its block.

    The block is what the refiner is told to read FIRST (its prompt says
    so), so a field that exists on ``Failure`` but never reaches
    ``last_result`` is a field the refiner cannot use. Building a full
    executor to observe this would prove nothing extra: the block is
    assembled by one method and read at one call.
    """
    from unittest.mock import MagicMock

    from agent import AutonomousAgent
    from task import SubTask

    agent = AutonomousAgent.__new__(AutonomousAgent)
    agent.logger = None
    agent._refinement_count = 0
    # ``__new__`` skips ``__init__``, so the fake carries exactly the
    # attributes the method under test reads -- and ``project_dir`` is one
    # of them: ``_refine_after_failure`` hands the refiner a necessity
    # index built from the project, and building that index needs the
    # directory.  ``None`` is the honest value here -- it says "the audit
    # knows nothing about this project", which is the reading
    # ``_necessity_project_index`` already gives an absent project -- and
    # it keeps the fake's shape equal to the real object's.
    agent.project_dir = None
    agent._failure_occurrences = {}
    agent._persist_refiner_output = MagicMock()
    task = SubTask(
        id="3", title="t", description="d", test_command="echo ok",
        status="pending",
    )
    agent.task_manager = MagicMock()
    agent.task_manager.requirement = "req"
    agent.task_manager.tasks = [task]
    agent.refiner = MagicMock()
    agent.refiner.refine = MagicMock(side_effect=lambda **kw: kw["tasks"])

    agent._refine_after_failure(
        task,
        coder_response="TEST_RESULT: FAILED\nREASON: nope",
        result_context="raw output",
        file_context="",
        exit_code=1,
        failure=failure,
        occurrence=occurrence,
    )
    return agent.refiner.refine.call_args.kwargs["last_result"]


def test_the_refiner_block_carries_the_refused_path():
    """The whole point of ``detail``: it names WHICH path.

    Without it the refiner is told "this task wrote a file it did not
    declare" and asked to add a targeted task — to a file it must
    invent. It would either guess wrong or ask a human, and the write
    stays refused.
    """
    failure = classify_failure("undeclared_write: /w/backend/b.py")
    block = _refiner_block(failure, occurrence=2)
    assert "detail: /w/backend/b.py" in block, block
    assert block.startswith("[FAILURE CLASSIFICATION]"), (
        "the block must lead the context — the prompt tells the refiner "
        "to read it first"
    )


def test_the_refiner_block_says_do_not_split_for_an_undeclared_write_repeat():
    """A REPEAT is exactly when the gate would otherwise open.

    The refiner's own rule is "split on the second occurrence of the same
    failure". ``split_allowed`` is the executor overriding that rule for
    the cases where its premise does not hold, so this is the line that
    has to read ``false`` for a second undeclared write.
    """
    failure = classify_failure("undeclared_write: /w/backend/b.py")
    block = _refiner_block(failure, occurrence=2)
    assert "occurrences_in_lineage: 2" in block, block
    assert "split_allowed: false" in block, block


def test_the_refiner_block_says_do_not_split_for_a_repeated_lock_conflict():
    failure = classify_failure(
        "File lock deadlock while acquiring backend/a.py"
    )
    block = _refiner_block(failure, occurrence=2)
    assert "split_allowed: false" in block, block


def test_the_refiner_block_omits_detail_when_there_is_none():
    """An empty ``detail:`` line reads as a field with no value.

    The refiner prompt says to take the refused path "from the
    classification block's `detail`", so an empty line would be an
    instruction to look at nothing rather than an honest absence.
    """
    failure = classify_failure("AssertionError: expected 3 got 4")
    block = _refiner_block(failure, occurrence=2)
    assert "detail:" not in block, block


def test_a_genuine_test_failure_is_still_split_allowed_on_a_repeat():
    """The gate must not have closed for everything.

    ``split_allowed: true`` on a second real test failure is the case
    splitting exists for; a blanket ``false`` would silently disable the
    whole refiner.
    """
    failure = classify_failure("AssertionError: expected 3, got 2")
    block = _refiner_block(failure, occurrence=2)
    assert "split_allowed: true" in block, block

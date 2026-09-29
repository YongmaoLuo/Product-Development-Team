"""
Tests for ``validate_desc_consistency`` — desc ⇄ depends_on alignment.

Background
----------
After the post-processing fix (task 3) that injects ``depends_on`` from
``desc`` whenever the LLM omits the field, the planner can still drift:
the LLM may emit a non-empty ``desc`` line like
``前置条件：任务 1 已完成`` but supply a ``depends_on`` that does NOT
include ``"1"``. The result is a plan whose prose and whose data
disagree — the layer builder walks the data path and ignores the
desc, so the task is scheduled before the prerequisite finishes.

The fix is a load-time check in ``agent._load_tasks`` that reuses the
desc-parsing logic from ``TasksGenerator._postprocess_extract_from_desc``
and rejects any plan whose ``desc`` mentions a task id absent from the
declared ``depends_on`` list. The error is fail-fast: the first
inconsistency aborts the load with a ``ValueError`` naming both the
offending task id and the missing dependency.

TDD spec — 5 contract tests
----------------------------
1. ``test_validate_desc_consistency_fail``:
   desc mentions '前置条件：任务 1 已完成' but ``depends_on == []`` →
   ``ValueError`` is raised; message names the offending task id
   ("1-2") and the missing dependency ("1").
2. ``test_validate_desc_consistency_pass``:
   desc mentions '前置条件：任务 1 已完成' and ``depends_on == ["1"]`` →
   no error; function returns ``None``.
3. ``test_validate_desc_consistency_no_trigger_passes``:
   desc has no '前置条件' / '依赖' / '前置条件/依赖' trigger word and
   ``depends_on == []`` → no error (the empty desc + empty deps is the
   happy path for a root task).
4. ``test_validate_desc_consistency_hierarchical_id``:
   desc mentions '前置条件：任务 1-2 已完成' and
   ``depends_on == ["1"]`` → ``ValueError`` names "1-2" as the missing
   dependency (the parser keeps hierarchical ids intact).
5. ``test_validate_desc_consistency_hierarchical_passes``:
   desc mentions '前置条件：任务 1-2 已完成' and
   ``depends_on == ["1-2"]`` → no error.
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
from agent import validate_desc_consistency  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str,
    description: str,
    depends_on: Optional[List[str]] = None,
) -> SubTask:
    """Build a minimal SubTask with the given id / description / depends_on.

    Only the fields read by ``validate_desc_consistency`` (``id``,
    ``description``, ``depends_on``) are populated; everything else uses
    ``SubTask``'s defaults so the test pins the helper's behaviour
    independently of the rest of the model.
    """
    return SubTask(
        id=task_id,
        title=f"task {task_id}",
        description=description,
        test_command="",
        status="pending",
        depends_on=list(depends_on) if depends_on is not None else [],
    )


# ---------------------------------------------------------------------------
# Test 1: fail case — desc mentions a prerequisite that depends_on omits
# ---------------------------------------------------------------------------


def test_validate_desc_consistency_fail():
    """desc mentions '任务 1' but ``depends_on == []`` → ValueError.

    The exact example pinned by the task spec: a SubTask whose
    description explicitly states ``前置条件：任务 1 已完成`` but whose
    ``depends_on`` field is empty. The validator must reject the
    inconsistency at load time so the executor cannot accidentally
    schedule the task before the prerequisite is done.

    The error message must:
      * name the offending task id (the task carrying the bad
        description);
      * name the missing dependency id (the prerequisite that the
        desc mentions but the data omits);
      * be detectable by a substring check so a grep-friendly error
        format is preserved.
    """
    bad_task = _make_task(
        task_id="1-2",
        description="前置条件：任务 1 已完成",
        depends_on=[],
    )

    with pytest.raises(ValueError) as exc_info:
        validate_desc_consistency([bad_task])

    msg = str(exc_info.value)
    # The example wording from the task spec is exact:
    #   "Task 1-2 desc 提到前置条件 1 但 depends_on 是 []"
    # We check for the specific fragments rather than a full-string
    # match, mirroring the pattern in
    # ``test_agent_load.py::test_load_validates_rejects_missing``.
    assert "1-2" in msg, (
        f"expected offending task id '1-2' in error message, got: {msg!r}"
    )
    assert "1" in msg, (
        f"expected missing dep '1' in error message, got: {msg!r}"
    )
    assert "desc" in msg.lower() or "前置" in msg, (
        f"error should mention desc (or 前置) as the source of the "
        f"discrepancy, got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: pass case — desc-mentioned prerequisite is present in depends_on
# ---------------------------------------------------------------------------


def test_validate_desc_consistency_pass():
    """desc mentions '任务 1' and ``depends_on == ['1']`` → no error.

    The matching positive case: when the desc-parser's output is a
    subset of ``depends_on``, the data has already declared the
    prerequisite, so the desc is documentation rather than a data
    drift. The validator returns ``None`` silently.
    """
    good_task = _make_task(
        task_id="1-2",
        description="前置条件：任务 1 已完成",
        depends_on=["1"],
    )

    # The function must not raise.
    result = validate_desc_consistency([good_task])
    assert result is None, (
        f"validate_desc_consistency should return None on the happy "
        f"path, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: no trigger phrase in desc + empty depends_on → no error
# ---------------------------------------------------------------------------


def test_validate_desc_consistency_no_trigger_passes():
    """desc without trigger word + empty depends_on → no error.

    The boundary case: a root task whose description simply does
    not mention any prerequisite. ``depends_on == []`` is the correct
    data — the validator must not invent a missing dependency out of
    thin air, only enforce the inverse direction (data is a
    superset-or-equal of the desc hints).
    """
    root_task = _make_task(
        task_id="1",
        description="本任务实现新功能。无依赖关系。",
        depends_on=[],
    )

    # Must not raise.
    result = validate_desc_consistency([root_task])
    assert result is None, (
        f"validator must not raise on root task with no trigger word, "
        f"got result={result!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: hierarchical id is kept intact by the parser (failure case)
# ---------------------------------------------------------------------------


def test_validate_desc_consistency_hierarchical_id():
    """desc mentions '任务 1-2' but ``depends_on == ['1']`` → ValueError.

    The parser keeps hierarchical ids intact: ``前置条件：任务 1-2``
    yields the single token ``"1-2"``, not ``["1", "2"]``. The
    validator must compare against the full id, so a ``depends_on``
    of ``["1"]`` (the parent only) is insufficient — the desc
    clearly says the prerequisite is ``1-2``, not just ``1``.
    """
    bad_task = _make_task(
        task_id="1-2-3",
        description="前置条件：任务 1-2 已完成。",
        depends_on=["1"],
    )

    with pytest.raises(ValueError) as exc_info:
        validate_desc_consistency([bad_task])

    msg = str(exc_info.value)
    # Hierarchical id must appear in full (not be split into "1" / "2").
    assert "1-2" in msg, (
        f"expected hierarchical id '1-2' in error message, got: {msg!r}"
    )
    # The offending task id is "1-2-3".
    assert "1-2-3" in msg, (
        f"expected offending task id '1-2-3' in error, got: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: hierarchical id pass case
# ---------------------------------------------------------------------------


def test_validate_desc_consistency_hierarchical_passes():
    """desc mentions '任务 1-2' and ``depends_on == ['1-2']`` → no error.

    Positive control for the hierarchical-id branch: when the data
    declares the full hierarchical prerequisite, the validator stays
    silent.
    """
    good_task = _make_task(
        task_id="1-2-3",
        description="前置条件：任务 1-2 已完成。",
        depends_on=["1-2"],
    )

    # Must not raise.
    result = validate_desc_consistency([good_task])
    assert result is None, (
        f"validator must accept depends_on=['1-2'] when desc says "
        f"'任务 1-2', got result={result!r}"
    )


# ---------------------------------------------------------------------------
# Smoke v4 follow-on: graph-aware transitive-dep acceptance
# ---------------------------------------------------------------------------
#
# Background: smoke v4 produced the chain task_1 → task_2 → task_3 with
# depends_on = [[], ["1"], ["2"]].  task_3's description named BOTH
# "任务 1" and "任务 2" as prerequisites ("前置条件：任务 1、任务 2 已完成"),
# but only listed "2" in depends_on.  The previous validator rejected
# this as a desc/data inconsistency — but the plan was actually fine:
# task_3 is transitively dependent on task_1 via task_2.  The new
# validator compares desc-deps against the *transitive closure* of
# depends_on, so this plan now succeeds.


def test_validate_desc_consistency_transitive_chain_passes():
    """desc names a transitive ancestor — validator accepts.

    Chain:  task_1 → task_2 → task_3  (depends_on = [], ['1'], ['2']).

    task_3's description names BOTH "任务 1" and "任务 2" — the full
    upstream chain.  task_3's declared ``depends_on`` only carries
    the immediate predecessor ("2"), but the transitive closure
    includes "1" via "2", so the validator must accept.
    """
    tasks = [
        _make_task(task_id="1", description="基础实现", depends_on=[]),
        _make_task(task_id="2", description="单元测试", depends_on=["1"]),
        _make_task(
            task_id="3",
            description="前置条件：任务 1、任务 2 已完成。E2E 测试。",
            depends_on=["2"],
        ),
    ]
    # Must not raise.
    result = validate_desc_consistency(tasks)
    assert result is None, (
        "transitive ancestor in desc must be accepted when reachable "
        f"via the depends_on chain, got result={result!r}"
    )


def test_validate_desc_consistency_transitive_missing_still_fails():
    """desc names an id unreachable from the depends_on chain → ValueError.

    Negative control for the transitive branch: when ``task_3`` has
    ``depends_on=["2"]`` and ``task_2`` has ``depends_on=[]`` (no chain
    to ``1``), a desc that mentions "任务 1" must STILL fail — the
    validator only relaxes the *transitive reachability* check, not
    the *semantic* one (desc still has to match the graph).
    """
    tasks = [
        _make_task(task_id="1", description="独立任务", depends_on=[]),
        # task_2 deliberately does NOT depend on task_1
        _make_task(task_id="2", description="独立任务", depends_on=[]),
        _make_task(
            task_id="3",
            description="前置条件：任务 1、任务 2 已完成。",
            depends_on=["2"],
        ),
    ]
    with pytest.raises(ValueError) as exc_info:
        validate_desc_consistency(tasks)
    msg = str(exc_info.value)
    # "1" is named in desc and not in task_3's closure {2}.
    assert "1" in msg, (
        f"missing transitive dep '1' must be named in error, got: {msg!r}"
    )
    assert "3" in msg, (
        f"offending task '3' must be named in error, got: {msg!r}"
    )


def test_validate_desc_consistency_self_dep_not_counted():
    """A self-dependency in ``depends_on`` does not pollute the closure.

    ``task_1`` lists ``"1"`` in its own ``depends_on`` (self-loop).
    The transitive closure of task_1's deps is just ``{"1"}`` — no
    further upstream.  task_1's desc naming "任务 1" is consistent
    with the closure (the self-reference satisfies itself), so the
    validator does not raise.

    Cycles (genuine cycles across multiple tasks) are detected
    elsewhere by ``_validate_dependencies``; the desc consistency
    check intentionally tolerates self-references so its error
    message stays specific to desc/data drift.
    """
    tasks = [
        _make_task(
            task_id="1",
            description="前置条件：任务 1 已完成。",
            depends_on=["1"],  # self-loop, filtered by the closure
        ),
    ]
    result = validate_desc_consistency(tasks)
    assert result is None, (
        "self-dep in depends_on should not raise from desc validator, "
        f"got result={result!r}"
    )


def test_validate_desc_consistency_diamond_passes():
    """Diamond-shaped graph: D depends on B and C; both depend on A.

    A's desc mentions nothing.  B/C/D each describe their own chain.
    Every desc-dep is reachable in the graph; the validator must
    accept the whole plan.
    """
    tasks = [
        _make_task(task_id="A", description="根任务", depends_on=[]),
        _make_task(
            task_id="B",
            description="前置条件：任务 A 已完成。",
            depends_on=["A"],
        ),
        _make_task(
            task_id="C",
            description="前置条件：任务 A 已完成。",
            depends_on=["A"],
        ),
        _make_task(
            task_id="D",
            # D names the full upstream set — A (transitive) + B + C (direct).
            description="前置条件：任务 A、任务 B、任务 C 已完成。",
            depends_on=["B", "C"],
        ),
    ]
    result = validate_desc_consistency(tasks)
    assert result is None, (
        "diamond graph where every desc-dep is reachable must pass, "
        f"got result={result!r}"
    )


# ---------------------------------------------------------------------------
# Regression: the trigger must be a prerequisite DECLARATION, not any
# occurrence of the word 依赖.
# ---------------------------------------------------------------------------
#
# A task description can carry a line that states a constraint the task
# itself is supposed to satisfy:
#
#     不新增依赖、不改 `.github/workflows/ci.yml`（归任务 10）
#
# — "add no new *library dependencies*, don't touch ci.yml (that's task
# 10's job)". The trigger regex matched the 依赖 inside 不新增依赖, the
# sentence-bounded window ran on past the 、 and the parenthetical to the
# 任务 10 its sibling was named in, and the parser therefore declared task
# 10 a prerequisite. ``depends_on`` was ``['1', '2']``, so the load-time
# validator rejected the ENTIRE tasks.json — one misread word blocked the
# plan from starting at all, and the message blamed a dependency the
# author never stated.
#
# All five documented patterns put a colon between the trigger and the
# id list; prose uses of 依赖 do not. Requiring the colon separates the
# two without narrowing any real declaration.


def test_bare_dependency_word_is_not_a_prerequisite_declaration():
    """Prose 依赖 with no colon must not be read as a prerequisite.

    ``不新增依赖`` means "no new library dependencies". The parser must
    not pair it with a 任务 N mentioned later in the same line.
    """
    task = _make_task(
        task_id="10-2",
        description=(
            "## 边界条件\n"
            "- 不新增依赖、不改 `.github/workflows/ci.yml`（归任务 10）\n"
        ),
        depends_on=["1", "2"],
    )

    result = validate_desc_consistency([task])
    assert result is None, (
        "a mention of 任务 10 inside a sentence about dependency policy is "
        f"not a declared prerequisite; got {result!r}"
    )


def test_colon_form_still_declares_a_prerequisite():
    """The documented ``依赖：任务 N`` form must keep working."""
    task = _make_task(
        task_id="3",
        description="依赖：任务 1 已完成",
        depends_on=[],
    )

    with pytest.raises(ValueError) as exc:
        validate_desc_consistency([task])

    assert "1" in str(exc.value), (
        f"the colon form must still be parsed as a prerequisite; got {exc.value!r}"
    )


def test_both_parsers_agree_on_the_colon_rule():
    """``agent`` and ``tasks_generator`` must parse identically.

    The validator and the generator's ``depends_on`` injection are two
    hand-copied parsers; a divergence lets an inconsistency through the
    load-time gate. The module contract says keep them byte-identical, so
    this pins the shared behaviour on the discriminating input.
    """
    from agent import _extract_desc_dep_ids
    from tasks_generator import TasksGenerator

    samples = [
        "前置条件：任务 1 已完成。",
        "依赖：任务 1 已完成",
        "前置条件/依赖：已完成 任务 1-2。",
        "不新增依赖、不改 ci.yml（归任务 10）",
        "没有触发词的普通描述，提到了任务 3",
    ]
    for text in samples:
        assert _extract_desc_dep_ids(text) == (
            TasksGenerator._postprocess_extract_from_desc(text)
        ), f"the two desc parsers disagree on {text!r}"

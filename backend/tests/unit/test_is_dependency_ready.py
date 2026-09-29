"""Tests for ``agent.is_dependency_ready`` — the dispatcher DAG gate.

The gate decides whether a task is ready to be scheduled based on
the status of every upstream task referenced in its
``depends_on``. Forcing the refiner to rewrite ``depends_on`` every
time a parent task is split is fragile: the desc/depends_on
disagreement deadlocks the retry loop. The smarter fix is a
**prefix-aware dependency resolver**: a reference to a parent id
resolves to that id OR any of its hierarchical children
(``"1"`` matches ``"1-1"``/``"1-2"``/...). This suite pins the
contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeTask:
    """Minimal stand-in for ``SubTask`` (the real one requires pydantic)."""
    id: str
    depends_on: list = field(default_factory=list)
    status: str = "pending"


def _is_ready(task, all_tasks):
    """Import the real production function on first call."""
    from agent import is_dependency_ready
    return is_dependency_ready(task, all_tasks)


# ---------------------------------------------------------------------------
# Baseline: explicit-id deps still work
# ---------------------------------------------------------------------------


def test_explicit_child_id_dep_is_ready_when_child_completed():
    """Sanity check: a depends_on that names a child id directly
    resolves to the child (existing behavior, no regression)."""
    a = FakeTask(id="a", depends_on=["1-1"], status="completed")
    b = FakeTask(id="b", depends_on=["a"])
    ready, _ = _is_ready(b, [a, b])
    assert ready


# ---------------------------------------------------------------------------
# New contract: prefix-aware resolver
# ---------------------------------------------------------------------------


def test_parent_id_dep_resolves_via_children_when_parent_absent():
    """A ``depends_on`` of ``"1"`` resolves to task 1 OR any of
    its children (1-1, 1-2, ...) when the parent id is NOT
    present in the plan (refiner split it away)."""
    # Plan after refiner split: task 1 has been replaced by
    # 1-1 / 1-2. Task 1 itself is no longer in the plan.
    c1 = FakeTask(id="1-1", status="completed")
    c2 = FakeTask(id="1-2", status="pending")
    a = FakeTask(id="a", depends_on=["1"])  # <-- still references "1"
    plan = [c1, c2, a]

    # Only one child is completed; the other is still pending.
    # The gate must consider a NOT-ready because not every matching
    # candidate upstream is in a success terminal status.
    ready, reason = _is_ready(a, plan)
    assert not ready, (
        "When any matching child is not in completed/skipped, "
        "the parent-id dep must NOT be considered ready."
    )
    assert reason, "ready=False must carry a reason string"


def test_parent_id_dep_resolves_via_children_when_all_completed():
    """When ALL children of a removed parent are completed, the
    parent-id dep on the downstream task resolves to ready."""
    c1 = FakeTask(id="1-1", status="completed")
    c2 = FakeTask(id="1-2", status="completed")
    c3 = FakeTask(id="1-3", status="completed")
    c4 = FakeTask(id="1-4", status="completed")
    a = FakeTask(id="a", depends_on=["1"])
    plan = [c1, c2, c3, c4, a]

    ready, _ = _is_ready(a, plan)
    assert ready, (
        "All children completed must satisfy a parent-id dep — "
        "the planner should not have to rewrite depends_on after "
        "every refiner split."
    )


def test_parent_id_dep_with_skipped_children():
    """``skipped`` is a success terminal status and should also
    unlock the parent-id dep, matching the existing
    ``is_dependency_ready`` contract."""
    c1 = FakeTask(id="1-1", status="completed")
    c2 = FakeTask(id="1-2", status="skipped")
    a = FakeTask(id="a", depends_on=["1"])
    plan = [c1, c2, a]

    ready, _ = _is_ready(a, plan)
    assert ready


def test_explicit_child_id_still_works_alongside_parent_id():
    """A downstream task that explicitly names a child id should
    still resolve to that child. Mixed deps (parent + child) must
    each resolve correctly."""
    c1 = FakeTask(id="1-1", status="completed")
    c2 = FakeTask(id="1-2", status="completed")
    a = FakeTask(id="a", depends_on=["1", "1-1"])
    plan = [c1, c2, a]

    ready, _ = _is_ready(a, plan)
    assert ready


def test_nested_prefix_2_dash_1_does_match_2():
    """The prefix resolver MUST treat ``"2-1"`` as a child of ``"2"``:
    a downstream task that depends on ``"2"`` resolves to the
    surviving child ``"2-1"`` when task 2 was split away.

    This is the central correctness guarantee of the prefix-aware
    resolver: any task id with a ``-`` suffix is a descendant of
    the prefix before the ``-``. If the parent id has been
    removed by the refiner, the children step in transparently.
    """
    c21 = FakeTask(id="2-1", status="completed")
    a = FakeTask(id="a", depends_on=["2"])
    plan = [c21, a]

    # ``"2"`` is not present in the plan but ``"2-1"`` (its only
    # surviving child) is completed. The resolver expands the dep
    # and the downstream task is ready.
    ready, _ = _is_ready(a, plan)
    assert ready, (
        "Bare parent id '2' must resolve to surviving child '2-1'. "
        "If this fails, the prefix resolver is broken — the "
        "refiner split would silently break every downstream task."
    )


def test_token_boundary_does_not_match_unrelated_id():
    """A bare id must NOT match a token that just happens to share
    a numeric prefix substring. ``"1"`` must not match the unrelated
    ``"10-1"`` (which starts with ``"1"`` but is a child of ``"10"``,
    not ``"1"``)."""
    # Plan with task 10 split into 10-1/10-2/10-3/10-4 but NO
    # task 1 in the plan.
    c1 = FakeTask(id="10-1", status="completed")
    a = FakeTask(id="a", depends_on=["1"])
    plan = [c1, a]

    # ``"1"`` is not a parent of ``"10-1"`` (10-1's parent is
    # "10"), so the resolver must NOT match. The dep fails as
    # missing_upstream:1.
    ready, reason = _is_ready(a, plan)
    assert not ready
    assert reason == "missing_upstream:1", (
        f"Bare id '1' must NOT match '10-1' (which is a child of "
        f"'10', not '1'). Got reason: {reason!r}"
    )


def test_parent_id_with_partial_children_done_still_blocked():
    """If only SOME children of a removed parent are completed,
    the dep is still blocked. The resolver must require every
    matching child to be in a success terminal status."""
    c1 = FakeTask(id="1-1", status="completed")
    c2 = FakeTask(id="1-2", status="pending")  # still running
    c3 = FakeTask(id="1-3", status="completed")
    c4 = FakeTask(id="1-4", status="completed")
    a = FakeTask(id="a", depends_on=["1"])
    plan = [c1, c2, c3, c4, a]

    ready, reason = _is_ready(a, plan)
    assert not ready
    assert "1-2" in reason, (
        f"Reason should mention the blocking child 1-2. Got: {reason!r}"
    )


def test_self_reference_still_treated_as_cycle():
    """The self-dependency cycle guard must continue to fire.
    Adding prefix resolution must not regress this."""
    a = FakeTask(id="a", depends_on=["a"])
    plan = [a]

    ready, reason = _is_ready(a, plan)
    assert not ready
    assert "missing_upstream" in reason


def test_existing_parent_still_resolves_to_itself():
    """A ``depends_on`` of a parent id that DOES exist must
    continue to resolve to that parent (no regression)."""
    p = FakeTask(id="1", status="completed")
    a = FakeTask(id="a", depends_on=["1"])
    plan = [p, a]

    ready, _ = _is_ready(a, plan)
    assert ready


def test_unknown_id_still_fails():
    """A ``depends_on`` referencing a totally unknown id (not
    in the plan, no children) must still fail as missing."""
    a = FakeTask(id="a", depends_on=["ghost"])
    plan = [a]

    ready, reason = _is_ready(a, plan)
    assert not ready
    assert "missing_upstream:ghost" in reason


def test_parent_id_with_zero_children_fails():
    """A ``depends_on`` of a removed parent with no surviving
    children (refiner deleted them all) must fail as missing."""
    a = FakeTask(id="a", depends_on=["1"])
    plan = [a]

    ready, reason = _is_ready(a, plan)
    assert not ready
    assert "missing_upstream:1" in reason


def test_failed_child_blocks_parent_id_dep():
    """A child in a failed terminal status must block the
    parent-id dep, matching the existing per-id contract."""
    c1 = FakeTask(id="1-1", status="completed")
    c2 = FakeTask(id="1-2", status="failed")  # one child failed
    c3 = FakeTask(id="1-3", status="completed")
    c4 = FakeTask(id="1-4", status="completed")
    a = FakeTask(id="a", depends_on=["1"])
    plan = [c1, c2, c3, c4, a]

    ready, reason = _is_ready(a, plan)
    assert not ready
    assert "1-2" in reason


def test_parent_id_mixed_with_other_dep():
    """A downstream task with multiple deps, one of which is a
    parent id, must require ALL of them to be ready (parent-id
    resolution does not weaken the all-must-be-ready contract)."""
    c1 = FakeTask(id="1-1", status="completed")
    c2 = FakeTask(id="1-2", status="completed")
    other = FakeTask(id="99", status="pending")
    a = FakeTask(id="a", depends_on=["1", "99"])
    plan = [c1, c2, other, a]

    ready, reason = _is_ready(a, plan)
    assert not ready
    assert "99" in reason

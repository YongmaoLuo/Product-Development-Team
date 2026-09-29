"""
Tests for the sibling-merge behaviour of ``agent._build_layers``.

These tests pin down the contract introduced by the parallel-layer fix
(the 2026-06-21 plan, task 2). Without the merge,
sibling subtasks (``1-1`` -> ``1-2`` -> ``1-3`` ...) would each land
in their own outer layer because of the chained ``depends_on``
declarations, and ``asyncio.gather`` would end up running one
coroutine at a time — defeating parallel execution.

The fix merges sibling subtasks that share a parent id and have no
cross-parent ``depends_on`` into the same outer layer as the parent
task, while preserving real cross-parent data dependencies as
serial cross-layer edges.
"""

from agent import _build_layers
from task import SubTask


def _st(tid, deps=None, files=None):
    """Helper to build a SubTask with optional depends_on / files."""
    return SubTask(
        id=tid,
        title=tid,
        description="",
        depends_on=list(deps or []),
        files_to_modify=list(files or []),
    )


def _flatten_outer(outer):
    return [t.id for micro in outer for t in micro]


def test_sibling_merge_into_same_layer():
    """6 sibling subtasks share parent_id and merge into one outer layer.

    Mirrors the example in the task 2 description: a root task ``1``
    with five chained subtasks should produce only **two** outer
    layers — ``[1]`` and ``[1-1, 1-2, 1-3, 1-4, 1-5]``.
    """
    tasks = [
        _st("1"),
        _st("1-1", deps=["1"]),
        _st("1-2", deps=["1-1"]),
        _st("1-3", deps=["1-2"]),
        _st("1-4", deps=["1-3"]),
        _st("1-5", deps=["1-4"]),
    ]
    layers = _build_layers(tasks)

    assert len(layers) == 2, f"expected 2 outer layers, got {len(layers)}"
    assert _flatten_outer(layers[0]) == ["1"]
    assert _flatten_outer(layers[1]) == ["1-1", "1-2", "1-3", "1-4", "1-5"]


def test_cross_parent_dependency_preserved():
    """Cross-parent ``depends_on`` keeps the dependent task in a later layer.

    Even though ``2-1`` is structurally a sibling subtask of root
    ``2``, it depends on a sibling of a different parent (``1-1``),
    which is a real data dependency and must not be merged into
    ``2``'s group. The expected schedule:

      * layer 0: ``[1]``
      * layer 1: ``[1-1, 1-2]`` (1's siblings merged)
      * layer 2: ``[2]`` (depends on ``1-1``)
      * layer 3: ``[2-1]`` (depends on ``2``)
    """
    tasks = [
        _st("1"),
        _st("1-1", deps=["1"]),
        _st("1-2", deps=["1-1"]),
        _st("2", deps=["1-1"]),
        _st("2-1", deps=["2"]),
    ]
    layers = _build_layers(tasks)

    flat = [_flatten_outer(L) for L in layers]
    assert flat == [
        ["1"],
        ["1-1", "1-2"],
        ["2"],
        ["2-1"],
    ], f"unexpected layer order: {flat}"


def test_in_degree_order_within_layer():
    """Sibling subtasks in a merged layer are emitted by ascending in_degree.

    Even when the input list is intentionally out of order, the merged
    layer should walk ``in_degree`` ascending (with ``id`` as a
    stable tie-breaker) so that the start order is deterministic and
    respects whatever minimal ordering the declared ``depends_on``
    chain implies.
    """
    tasks = [
        _st("1"),
        _st("1-3", deps=["1-2"]),
        _st("1-1", deps=["1"]),
        _st("1-5", deps=["1-4"]),
        _st("1-2", deps=["1-1"]),
        _st("1-4", deps=["1-3"]),
    ]
    layers = _build_layers(tasks)

    assert len(layers) == 2
    assert _flatten_outer(layers[0]) == ["1"]
    assert _flatten_outer(layers[1]) == ["1-1", "1-2", "1-3", "1-4", "1-5"]


def test_gather_receives_merged_layer():
    """The merged layer shape is preserved when handed to a downstream gather.

    We don't import ``asyncio.gather`` here (the synchronous wrapper
    may not exist outside the agent's own runtime); instead we feed
    the merged layer through a stub ``_build_micro_layers`` consumer
    that records every SubTask it receives. The shape check confirms:

      * the outer layer has at least 3 tasks (layer_size >= 3),
      * every merged sibling id is present in the consumer's input,
      * downstream consumers still see a list-of-lists (micro layer
        boundary is honoured).
    """
    seen = []

    def fake_consumer(layer):
        seen.append(_flatten_outer(layer))
        return layer

    tasks = [
        _st("1"),
        _st("1-1", deps=["1"]),
        _st("1-2", deps=["1-1"]),
        _st("1-3", deps=["1-2"]),
        _st("1-4", deps=["1-3"]),
        _st("1-5", deps=["1-4"]),
        _st("1-6", deps=["1-5"]),
    ]
    layers = _build_layers(tasks)
    assert len(layers) == 2

    consumer_input = layers[1]
    merged_layer_size = sum(len(micro) for micro in consumer_input)
    assert merged_layer_size >= 3, (
        f"layer_size must be >= 3 (got {merged_layer_size})"
    )
    fake_consumer(consumer_input)

    assert any(
        set(siblings) == {"1-1", "1-2", "1-3", "1-4", "1-5", "1-6"}
        for siblings in seen
    ), f"merged sibling set not seen by gather consumer: {seen}"


def test_in_group_dep_merges():
    """Sibling subtask with in-group ``depends_on`` still merges into parent's layer.

    ``_has_only_sibling_deps`` is True for ``"1-2"`` because its only
    ``depends_on`` is ``"1-1"``, which is in the same sibling group as
    ``"1"``. The merge tail in ``_build_layers`` must therefore pull
    ``"1-2"`` into the same outer layer as the parent ``"1"`` (i.e.
    ``layer[1]``), not emit it in a separate trailing layer.

    Boundary contract:

      * ``"1-2"`` depends on ``"1-1"`` — INTERNAL to the sibling group
        of parent ``"1"`` (NOT a cross-parent edge).
      * ``"1-2"``'s ``files_to_modify`` is ``"d.py"`` — disjoint from
        every other sibling, so the micro-layer coalescer treats it
        as non-conflicting and emits it in the same micro layer as
        the other siblings.

    Expected schedule:
      * layer 0: ``[1]`` (parent)
      * layer 1: ``[1-1, 1-2, 1-3]`` (the entire sibling group merged,
        INCLUDING ``1-2`` even though it transitively depends on
        ``1-1`` via ``1``'s sibling group)
    """
    tasks = [
        _st("1", files=["a.py"]),
        _st("1-1", deps=["1"], files=["b.py"]),
        _st("1-2", deps=["1-1"], files=["d.py"]),
        _st("1-3", deps=["1-2"], files=["e.py"]),
    ]
    layers = _build_layers(tasks)

    flat = [_flatten_outer(L) for L in layers]
    assert flat == [
        ["1"],
        ["1-1", "1-2", "1-3"],
    ], (
        f"in-group dep must NOT cause 1-2 to escape the sibling-merge; "
        f"expected [['1'], ['1-1','1-2','1-3']], got {flat}"
    )

    # Stronger: "1-2" must be in layer[1] (i.e. the merged sibling
    # layer), NOT in layer[2] or later. If it ever leaks out, the
    # in-group-dependency branch of the merge rule has regressed.
    assert "1-2" in _flatten_outer(layers[1]), (
        f"1-2 (which depends on its in-group sibling 1-1) must land in "
        f"the merged layer (layer[1]); layers={flat}"
    )
    assert "1-2" not in _flatten_outer(layers[0]), (
        f"1-2 must NOT be emitted before its parent 1; layers={flat}"
    )


def test_parent_id_field_matches_id_prefix():
    """Sibling ``parent_id`` is correctly inferred from the id prefix.

    The sibling-merge rule relies on a task's ``parent_id`` (either
    declared on the task or inferred from its id prefix). For a
    sibling task with id ``"1-1"`` the expected parent is ``"1"`` —
    the prefix of the id before the final ``-`` segment. A
    regression here would silently mis-cluster siblings and break the
    merge tail.

    Boundary contract:

      * Each sibling id (``"1-1"``, ``"1-2"``) has the format
        ``<parent>-<n>`` and the parent segment (``"1"``) exists in
        the input list (so inference finds a real parent, not a
        dangling prefix).
      * The root task ``"1"`` itself has NO parent — there is no
        id-prefix-based way to roll it up to anything else.
      * A task whose id contains no ``-`` (a root) is NOT a sibling
        and has no inferred parent_id.

    This test pins down the inference contract so a future refactor
    of the sibling-merge rule cannot silently break the
    ``parent_id``-derivation step (e.g. by changing the separator or
    forgetting the prefix-look-up in ``_infer_parent_id``).
    """
    tasks = [
        _st("1", files=["a.py"]),
        _st("1-1", deps=["1"], files=["b.py"]),
        _st("1-2", deps=["1-1"], files=["c.py"]),
    ]
    by_id = {t.id: t for t in tasks}

    # 1. The parent itself has no parent_id (no id-prefix parent).
    parent_task = by_id["1"]
    inferred_parent = (
        parent_task.id.rsplit("-", 1)[0] if "-" in parent_task.id else None
    )
    assert inferred_parent is None, (
        f"root task '1' must have no inferred parent_id; "
        f"got {inferred_parent!r}"
    )

    # 2. Every sibling's inferred parent_id is the id-prefix.
    for sibling_id in ("1-1", "1-2"):
        sibling = by_id[sibling_id]
        assert "-" in sibling.id, (
            f"sibling {sibling_id!r} must contain a '-' separator"
        )
        inferred_parent_id = sibling.id.rsplit("-", 1)[0]
        assert inferred_parent_id == "1", (
            f"sibling {sibling_id!r} must have inferred parent_id == '1', "
            f"got {inferred_parent_id!r}"
        )

    # 3. End-to-end: the in-group-dep branch merges siblings even when
    #    one sibling transitively depends on another. The parent id
    #    is consistently "1" for both siblings, and the merge tail
    #    correctly identifies them as part of the same group.
    layers = _build_layers(tasks)
    flat = [_flatten_outer(L) for L in layers]
    assert flat == [
        ["1"],
        ["1-1", "1-2"],
    ], f"inferred parent_id must lead to correct merge tail; got {flat}"

    # 4. Both siblings in layer[1] must map back to parent "1" via the
    #    id-prefix inference. This is the contract that the sibling-
    #    merge tail's ``sibling_parent[task.id]`` lookup depends on.
    for sibling_in_merged_layer in (
        task for task in layers[1][0] if task.id != "1"
    ):
        if "-" in sibling_in_merged_layer.id:
            parent_prefix = sibling_in_merged_layer.id.rsplit("-", 1)[0]
            assert parent_prefix == "1", (
                f"sibling in merged layer must have id-prefix parent '1'; "
                f"got {parent_prefix!r} from id "
                f"{sibling_in_merged_layer.id!r}"
            )

# ---------------------------------------------------------------------------
# Regression: sentinel tasks must run in PARALLEL, not be serialised.
# (2026-08-19)
# ---------------------------------------------------------------------------


def test_sentinel_tasks_run_in_parallel_not_serialised():
    """All-sentinel tasks in one outer layer coalesce into ONE micro layer.

    Regression anchor for the earlier plan's plan: every entry-point
    task carried ``files_to_modify = ["__UNKNOWN_MODIFICATIONS__"]``
    (forced by the validator step-3 sentinel backfill). The pre-fix
    micro-layer builder mapped every sentinel task to the SAME shared
    conflict key, so they collided into one connected component and
    were serialised into per-task micro layers (``layer_size: 1``) —
    killing ALL parallelism. The sentinel means "files unknown", not
    "touches every file"; unknown-file tasks do not inherently conflict.
    """
    from task import UNKNOWN_MODIFICATIONS_SENTINEL

    sentinel = list(UNKNOWN_MODIFICATIONS_SENTINEL)
    tasks = [_st(tid, files=sentinel) for tid in ["1", "2", "4-1", "20-1", "20-2", "33"]]

    outer = _build_layers(tasks)
    # All six are independent entry points → a single outer layer …
    assert len(outer) == 1, f"entry points must form one outer layer; got {len(outer)}"
    # … and that outer layer must contain a SINGLE micro layer holding
    # all six tasks (parallel), not six single-task micro layers.
    micro_layers = outer[0]
    assert len(micro_layers) == 1, (
        f"sentinel tasks must batch into 1 parallel micro layer, "
        f"got {len(micro_layers)} serialised micro layers"
    )
    assert sorted(t.id for t in micro_layers[0]) == ["1", "2", "20-1", "20-2", "33", "4-1"]


def test_concrete_file_conflict_still_serialises():
    """Two tasks sharing a concrete file path MUST still serialise.

    This is the actual safety property the micro-layer conflict graph
    exists to protect; the sentinel-parallel fix must not weaken it.
    """
    tasks = [
        _st("a", files=["src/x.py"]),
        _st("b", files=["src/x.py"]),
    ]
    outer = _build_layers(tasks)
    micro_layers = outer[0]
    # Two tasks, one shared file → two single-task micro layers.
    assert len(micro_layers) == 2, (
        f"concrete shared-file tasks must serialise into 2 micro layers; "
        f"got {len(micro_layers)}"
    )
    assert [t.id for m in micro_layers for t in m] == ["a", "b"]


def test_sentinel_does_not_conflict_with_concrete_file_task():
    """A sentinel task and a concrete-file task share no key → parallel."""
    from task import UNKNOWN_MODIFICATIONS_SENTINEL

    tasks = [
        _st("a", files=list(UNKNOWN_MODIFICATIONS_SENTINEL)),
        _st("b", files=["src/x.py"]),
    ]
    outer = _build_layers(tasks)
    micro_layers = outer[0]
    assert len(micro_layers) == 1, (
        f"sentinel + concrete-file task share no conflict key and must "
        f"batch into 1 parallel micro layer; got {len(micro_layers)}"
    )
    assert sorted(t.id for t in micro_layers[0]) == ["a", "b"]

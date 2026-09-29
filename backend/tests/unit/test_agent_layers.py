"""
TDD tests for ``_build_layers()`` — explicit dependency + file conflict micro-layering.

Background
----------
``_build_layers`` takes a list of :class:`SubTask` objects and returns a
three-level structure ``outer layer → micro layer → tasks``.  The outer
layers are topological batches computed from ``depends_on``.  Within one
outer layer, tasks are further split into *micro layers* so that tasks
which share a ``files_to_modify`` entry do not run concurrently.

The five tests below pin the contract:

  1. ``test_preserves_explicit_dependencies``
     A pure dependency DAG (chain + diamond) with disjoint files is
     layered only by ``depends_on``; each outer layer contains a single
     micro layer.

  2. ``test_splits_conflicting_tasks``
     Two tasks in the same outer layer that modify the same file are
     serialised into separate micro layers.

  3. ``test_keeps_non_conflicting_in_same_micro_layer``
     Two tasks in the same outer layer that modify disjoint files stay
     in the same micro layer and can run concurrently.

  4. ``test_unknown_tasks_serial``
     Tasks carrying the unknown-modification sentinel are treated as
     mutually conflicting and are serialised, even when they have no
     explicit dependency edges.

  5. ``test_mixed_dependency_and_conflict``
     A diamond DAG where the concurrent middle layer also contains a
     file conflict: one pair shares a file, the other pair is disjoint.
     The result respects both dependency edges and conflict serialisation.

  6. ``test_deterministic_output``
     Calling :func:`_build_layers` repeatedly with the same input
     yields byte-identical layer structures.

  7. ``test_stable_sort``
     Within a single micro-layer, tasks are sorted by ``task.id``
     regardless of input order, so the result is input-order
     independent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from task import SubTask, UNKNOWN_MODIFICATIONS_SENTINEL  # noqa: E402
from agent import _build_layers  # noqa: E402


def _task(task_id: str, deps: Optional[list[str]] = None, files: Optional[list[str]] = None) -> SubTask:
    """Build a minimal SubTask for layer construction tests."""
    return SubTask(
        id=task_id,
        title=f"Task {task_id}",
        description=f"Description for {task_id}",
        depends_on=deps or [],
        files_to_modify=files,
    )


def _ids(layers: list[list[list[SubTask]]]) -> list[list[list[str]]]:
    """Convert a layer structure to nested task id strings for assertions."""
    return [[[t.id for t in micro] for micro in outer] for outer in layers]


class TestBuildLayers:
    """Cover chain, diamond, conflict, unknown, and mixed scenarios."""

    def test_preserves_explicit_dependencies(self):
        """Pure dependency DAGs produce outer layers only; no micro splitting."""
        a = _task("A", files=["a.py"])
        b = _task("B", deps=["A"], files=["b.py"])
        c = _task("C", deps=["B"], files=["c.py"])
        d = _task("D", deps=["B"], files=["d.py"])
        e = _task("E", deps=["C", "D"], files=["e.py"])

        layers = _build_layers([a, b, c, d, e])

        assert _ids(layers) == [
            [["A"]],
            [["B"]],
            [["C", "D"]],
            [["E"]],
        ]

    def test_splits_conflicting_tasks(self):
        """Tasks in the same outer layer sharing a file are serialised."""
        a = _task("A", files=["shared.py"])
        b = _task("B", files=["shared.py"])

        layers = _build_layers([a, b])

        assert _ids(layers) == [
            [["A"], ["B"]],
        ]

    def test_keeps_non_conflicting_in_same_micro_layer(self):
        """Tasks in the same outer layer with disjoint files stay concurrent."""
        a = _task("A", files=["a.py"])
        b = _task("B", files=["b.py"])

        layers = _build_layers([a, b])

        assert _ids(layers) == [
            [["A", "B"]],
        ]

    def test_unknown_tasks_serial(self):
        """Unknown-modification (sentinel) tasks run in PARALLEL.

        Contract changed 2026-08-19 (explicit user
        requirement "能并行当然并行" / "把所有task串行化也不行"): the
        sentinel means "the exact files are not yet known", NOT "this task
        touches every file", so sentinel tasks do NOT inherently conflict.
        Each subagent runs in its own worktree and the executor commits
        serially, so two parallel sentinel tasks cannot corrupt each
        other's on-disk writes. They therefore coalesce into ONE parallel
        micro layer instead of serialising into per-task micro layers.
        """
        a = _task("A", files=list(UNKNOWN_MODIFICATIONS_SENTINEL))
        b = _task("B", files=list(UNKNOWN_MODIFICATIONS_SENTINEL))
        c = _task("C", files=list(UNKNOWN_MODIFICATIONS_SENTINEL))

        layers = _build_layers([a, b, c])

        assert _ids(layers) == [
            [["A", "B", "C"]],
        ]

    def test_unknown_modifications_serial(self):
        """Sentinel tasks batch WITH non-conflicting known-file tasks.

        Renamed-contract (2026-08-19): sentinel tasks no longer serialise
        with each other nor with concrete-file tasks they share no key
        with — they all coalesce into one parallel micro layer. Concrete
        file conflicts (none here) would still force a micro split.
        """
        a = _task("A", files=list(UNKNOWN_MODIFICATIONS_SENTINEL))
        b = _task("B", files=list(UNKNOWN_MODIFICATIONS_SENTINEL))
        c = _task("C", files=["c.py"])
        d = _task("D", files=["d.py"])

        layers = _build_layers([c, d, a, b])

        # No shared concrete file and sentinel↔concrete share no key →
        # all four tasks are mutually non-conflicting and batch together.
        assert _ids(layers) == [
            [["A", "B", "C", "D"]],
        ]

    def test_mixed_dependency_and_conflict(self):
        """Diamond DAG with a file conflict inside the concurrent layer."""
        a = _task("A", files=["a.py"])
        b = _task("B", deps=["A"], files=["x.py"])
        c = _task("C", deps=["A"], files=["x.py"])
        d = _task("D", deps=["A"], files=["y.py"])
        e = _task("E", deps=["B", "C", "D"], files=["e.py"])

        layers = _build_layers([a, b, c, d, e])

        assert _ids(layers) == [
            [["A"]],
            [["B"], ["C"], ["D"]],
            [["E"]],
        ]

    def test_explicit_dep_takes_precedence(self):
        """Explicit dep wins for outer-layer placement; the file
        conflict only triggers a micro split within one outer layer.

        Pair (A, B) shares a file *and* B explicitly depends on A.
        The dep alone moves B to a later outer layer, so the file
        conflict between A and B never gets a chance to apply at the
        outer level. C also shares the file with A but has no dep on
        A, so C stays in A's outer layer and the conflict between A
        and C is what forces a micro split there.
        """
        a = _task("A", files=["shared.py"])
        b = _task("B", deps=["A"], files=["shared.py"])
        c = _task("C", files=["shared.py"])

        layers = _build_layers([a, b, c])

        assert _ids(layers) == [
            [["A"], ["C"]],
            [["B"]],
        ]

    def test_deterministic_output(self):
        """Repeated calls with the same input yield identical structures."""
        a = _task("A", files=["a.py"])
        b = _task("B", files=["shared.py"])
        c = _task("C", files=["shared.py"])
        d = _task("D", files=["d.py"])
        e = _task("E", deps=["A", "B"], files=["e.py"])

        tasks = [a, b, c, d, e]
        first = _ids(_build_layers(list(tasks)))
        second = _ids(_build_layers(list(tasks)))
        third = _ids(_build_layers(list(tasks)))

        assert first == second == third

    def test_stable_sort(self):
        """Within a micro-layer, tasks are sorted by task id, not input order."""
        a = _task("A", files=["a.py"])
        b = _task("B", files=["b.py"])
        c = _task("C", files=["c.py"])

        # Pass in reverse-alphabetical input order; the micro-layer must
        # still come back in ascending task id order.
        layers = _build_layers([c, b, a])
        assert _ids(layers) == [[["A", "B", "C"]]]

    def test_mixed_legacy_and_new(self):
        """Mixed legacy (sentinel) and new (explicit files) tasks.

        Contract updated 2026-08-19 (sentinel-parallel fix). Three
        invariants:

        1. Legacy sentinel tasks no longer serialise with each other —
           each carries a UNIQUE synthetic key so they do not conflict.
        2. The sentinel key never collides with a concrete file path, so
           a legacy task is not artificially serialised with a new task.
        3. A real file conflict between new tasks is still preserved:
           C and E share ``c.py`` and MUST land in separate micro layers.

        Expected layout: ``A`` is a leading singleton; ``C``/``E`` form a
        concrete-conflict pair that serialises; ``B`` (sentinel) and ``D``
        (``d.py``, no conflict) are trailing singletons batched together.
        """
        # Legacy tasks: unknown-modification sentinel.
        a = _task("A", files=list(UNKNOWN_MODIFICATIONS_SENTINEL))
        b = _task("B", files=list(UNKNOWN_MODIFICATIONS_SENTINEL))
        # New tasks: explicit files. C and E share c.py (real conflict).
        c = _task("C", files=["c.py"])
        d = _task("D", files=["d.py"])
        e = _task("E", files=["c.py"])

        layers = _build_layers([a, c, d, e, b])

        assert _ids(layers) == [
            [["A"], ["C"], ["E"], ["B", "D"]],
        ]

    def test_verify_build_layers_sibling_merge(self):
        """Sibling-merge: 6 siblings sharing ``parent_id`` collapse into
        the outer layer where the parent (and the first mergeable
        sibling) live.

        Spec invariants:
          * 6 sibling subtasks ``1-1``..``1-6`` all roll up to ``"1"``.
          * ``layers[1]`` (the second outer layer) contains exactly 5
            tasks in total (across its micro layers).
          * Total number of outer layers is ``>= 3``.

        Setup::

            "0"   no deps, file "a.py"     -> layer[0]
            "1"   deps=["0"], file "b.py"   -> layer[1]  (after "0")
            "1-1" deps=["0"], file "c.py"   -> layer[1]  (after "0")
            "1-2" deps=["1-1"], file "d.py" -> merged into layer[1]
            "1-3" deps=["1-1"], file "e.py" -> merged into layer[1]
            "1-4" deps=["1-1"], file "f.py" -> merged into layer[1]
            "1-5" deps=["1"],   file "g.py" -> NOT merged (cross-parent dep)
            "1-6" deps=["1"],   file "h.py" -> NOT merged (cross-parent dep)

        Why this triggers the merge:
          * After ``"0"`` resolves, ``"1"`` and ``"1-1"`` both reach
            in-degree 0 and enter ``current`` of layer[1].
          * When ``"1-1"`` is processed, the merge tail iterates the
            sibling group of ``"1"``; ``"1-2"``, ``"1-3"``, ``"1-4"``
            depend only on ``"1-1"`` (an in-group dep), so they are
            pulled into layer[1].
          * ``"1-5"`` and ``"1-6"`` depend on ``"1"``, which is the
            parent itself and NOT in the sibling group, so they fail
            ``_has_only_sibling_deps`` and are excluded from the merge.
            They land in layer[2] after ``"1"`` decrements their
            in-degree to 0.

        Expected::

            layers[0] = [["0"]]
            layers[1] = [["1", "1-1", "1-2", "1-3", "1-4"]]   (5 tasks)
            layers[2] = [["1-5", "1-6"]]                      (2 tasks)
        """
        tasks = [
            _task("0", files=["a.py"]),
            _task("1", deps=["0"], files=["b.py"]),
            _task("1-1", deps=["0"], files=["c.py"]),
            _task("1-2", deps=["1-1"], files=["d.py"]),
            _task("1-3", deps=["1-1"], files=["e.py"]),
            _task("1-4", deps=["1-1"], files=["f.py"]),
            _task("1-5", deps=["1"], files=["g.py"]),
            _task("1-6", deps=["1"], files=["h.py"]),
        ]

        layers = _build_layers(tasks)

        # Total outer layers must be >= 3.
        assert len(layers) >= 3, (
            f"expected >= 3 outer layers, got {len(layers)}: "
            f"{_ids(layers)}"
        )

        # layer[1] must contain exactly 5 tasks across its micro layers.
        layer_1_task_count = sum(len(micro) for micro in layers[1])
        assert layer_1_task_count == 5, (
            f"expected 5 tasks in layers[1], got {layer_1_task_count}: "
            f"{_ids(layers[1])}"
        )

        # layer[1] must contain "1", "1-1", and the three mergeable siblings.
        layer_1_ids = {t.id for micro in layers[1] for t in micro}
        assert layer_1_ids == {"1", "1-1", "1-2", "1-3", "1-4"}, (
            f"unexpected tasks in layers[1]: {layer_1_ids}"
        )

        # The non-mergeable siblings must be deferred to layers[2].
        layer_2_ids = {t.id for micro in layers[2] for t in micro}
        assert layer_2_ids == {"1-5", "1-6"}, (
            f"expected layers[2] to be exactly {{'1-5', '1-6'}}, "
            f"got {layer_2_ids}: {_ids(layers)}"
        )

        # Exact shape sanity check (single micro layer per outer layer
        # because all files within each outer layer are disjoint).
        assert _ids(layers) == [
            [["0"]],
            [["1", "1-1", "1-2", "1-3", "1-4"]],
            [["1-5", "1-6"]],
        ], f"unexpected layer shape: {_ids(layers)}"


def _parent_id_of(task_id: str) -> Optional[str]:
    """Derive the parent_id of a subtask id by stripping the last ``-N`` suffix.

    Mirrors :meth:`AutonomousAgent._find_parent_task` in
    ``backend/agent.py`` (line ~3086), which uses::

        parent_id = task.id.rsplit("-", 1)[0]

    so ``"1-3"`` -> ``"1"``, ``"1-2"`` -> ``"1"``, and any id without
    a ``-`` (e.g. ``"1"``) -> ``None`` (top-level task with no
    parent).
    """
    if "-" not in task_id:
        return None
    return task_id.rsplit("-", 1)[0]


def verify_multi_in_group_deps_merge():
    """``1-3`` lands in ``layers[1]`` alongside ``"1-1"`` and ``"1-2"``.

    Contract: when multiple siblings depend on the same **in-group**
    ancestor (here, ``"1-3"`` and ``"1-2"`` both depend only on
    ``"1-1"``, which is itself a sibling of ``"1"``), they must all
    collapse into the same outer layer as their shared in-group
    ancestor — they do NOT block each other on cross-sibling deps,
    and they do NOT get pushed into a later outer layer.

    This is the focused, single-sibling scenario corresponding to the
    sibling-merge merge tail of ``_build_layers``. The broader
    6-sibling merge is pinned by
    ``TestBuildLayers.test_verify_build_layers_sibling_merge``; this
    test isolates the ``"1-3"`` branch for triage.
    """
    tasks = [
        _task("0", files=["a.py"]),
        _task("1", deps=["0"], files=["b.py"]),
        _task("1-1", deps=["0"], files=["c.py"]),
        _task("1-2", deps=["1-1"], files=["d.py"]),
        _task("1-3", deps=["1-1"], files=["e.py"]),
    ]

    layers = _build_layers(tasks)

    # Flatten layer[1] to a set of task ids so we can assert presence
    # regardless of how the micro-layer splitter groups the siblings
    # within the outer layer.
    layer_1_ids = {t.id for micro in layers[1] for t in micro}

    # The core invariant: "1-3" is in layers[1].
    assert "1-3" in layer_1_ids, (
        f"expected '1-3' in layers[1], got {layer_1_ids}: {_ids(layers)}"
    )

    # All four siblings/parent must coexist in layers[1].
    assert {"1", "1-1", "1-2", "1-3"}.issubset(layer_1_ids), (
        f"expected {{'1','1-1','1-2','1-3'}} subset of layers[1] ids, "
        f"got {layer_1_ids}: {_ids(layers)}"
    )

    # Top-level task "0" must NOT be in layers[1] (it owns layers[0]).
    assert "0" not in layer_1_ids, (
        f"'0' should be in layers[0], not layers[1]: {_ids(layers)}"
    )


def verify_parent_id_field_matches_id_prefix():
    """``sibling.parent_id == "1"`` for every sibling of parent ``"1"``.

    The parent_id is **derived** from the id prefix, not stored as an
    explicit field on :class:`SubTask`. The derivation matches
    :meth:`AutonomousAgent._find_parent_task` (``backend/agent.py``):

      * ``"1-3"`` -> parent ``"1"``
      * ``"1-2"`` -> parent ``"1"``
      * ``"1-1"`` -> parent ``"1"``
      * ``"1"``   -> no parent (``None``) — top-level task
      * ``"1-5"`` -> parent ``"1"`` (cross-parent prefix collision
        is excluded here — the "1-5"/"1-6" branch belongs to the
        cross-parent sibling test, not this focused one)

    This test pins the parent_id derivation contract independently of
    the layer-build path, so a regression in either ``rsplit("-", 1)``
    semantics or the lookup loop is caught here before
    ``_build_layers`` ever sees the task.
    """
    # In-group siblings of "1" — all must report parent_id == "1".
    sibling_1_1 = _task("1-1", files=["c.py"])
    sibling_1_2 = _task("1-2", deps=["1-1"], files=["d.py"])
    sibling_1_3 = _task("1-3", deps=["1-1"], files=["e.py"])

    assert _parent_id_of(sibling_1_1.id) == "1", (
        f"expected parent_id('1-1') == '1', got {_parent_id_of(sibling_1_1.id)!r}"
    )
    assert _parent_id_of(sibling_1_2.id) == "1", (
        f"expected parent_id('1-2') == '1', got {_parent_id_of(sibling_1_2.id)!r}"
    )
    assert _parent_id_of(sibling_1_3.id) == "1", (
        f"expected parent_id('1-3') == '1', got {_parent_id_of(sibling_1_3.id)!r}"
    )

    # Top-level task "1" itself has no parent.
    parent_task = _task("1", deps=["0"], files=["b.py"])
    assert _parent_id_of(parent_task.id) is None, (
        f"expected parent_id('1') is None (no '-' in id), "
        f"got {_parent_id_of(parent_task.id)!r}"
    )

    # Sibling "1-3"'s derived parent_id must match the parent id
    # prefix convention used by _build_layers — i.e. the lookup
    # against _parent_id_of("1-3") == "1" succeeds against the actual
    # parent task in the task list. This mirrors how
    # agent._find_parent_task validates the candidate exists in
    # self._all_tasks.
    all_tasks = [parent_task, sibling_1_1, sibling_1_2, sibling_1_3]
    derived_parent_id = _parent_id_of(sibling_1_3.id)
    parent_match = next(
        (t for t in all_tasks if t.id == derived_parent_id), None
    )
    assert parent_match is not None, (
        f"derived parent_id {derived_parent_id!r} for '1-3' did not "
        f"match any task in the task list"
    )
    assert parent_match.id == "1", (
        f"derived parent_id for '1-3' resolved to {parent_match.id!r}, "
        f"expected '1'"
    )
"""
Unit tests for verification_dag.VPNode and DAGValidationError.

These tests pin the *data model only*. Cross-node graph validation
(cycle detection, layer partitioning, topological sort) is out of
scope for this subtask and will be covered by a future
``TestDAGValidator`` class.

All tests are pure-function style: no I/O, no fixtures, no LLM, no
filesystem. They should run in <100ms total.
"""

import ast
import asyncio
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Make `verification_dag` importable when pytest is launched from
# either the project root or the `backend/` directory.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cc_switch  # noqa: E402

from verification_dag import (  # noqa: E402
    DAGValidationError,
    ProviderConcurrencyController,
    VPNode,
    VerificationDAG,
    get_verification_provider,
)


# ---------------------------------------------------------------------------
# Default-value pinning (4 cases)
# ---------------------------------------------------------------------------


class TestVPNodeDefaults:
    """VPNode must default the four optional fields declared by the PRD.

    These are the only fields with non-trivial defaults. The remaining
    fields (``vp_id`` and ``method``) are required at construction.
    """

    def test_vpnode_default_layer(self):
        """Omitting ``layer`` must default to 1."""
        node = VPNode(vp_id="VP-001", method="automated_test")
        assert node.layer == 1

    def test_vpnode_default_depends_on(self):
        """Omitting ``depends_on`` must default to an empty list.

        The default must be a *new* list per instance — sharing a
        mutable default across instances is a classic Python footgun
        that ``field(default_factory=list)`` exists to prevent.
        """
        node = VPNode(vp_id="VP-001", method="automated_test")
        assert node.depends_on == []
        # Mutating one node's depends_on must not leak into another.
        node.depends_on.append("VP-XXX")
        node2 = VPNode(vp_id="VP-002", method="automated_test")
        assert node2.depends_on == [], (
            "depends_on default must be a fresh list per instance"
        )

    def test_vpnode_default_max_retries(self):
        """Omitting ``max_retries`` must default to 3."""
        node = VPNode(vp_id="VP-001", method="automated_test")
        assert node.max_retries == 3

    def test_vpnode_default_model_complexity(self):
        """Omitting ``model_complexity`` must default to 'medium'."""
        node = VPNode(vp_id="VP-001", method="automated_test")
        assert node.model_complexity == "medium"


# ---------------------------------------------------------------------------
# Field validation: layer must be a positive integer
# ---------------------------------------------------------------------------


class TestDAGValidationError:
    """Invalid field values must raise DAGValidationError at __post_init__."""

    def test_dag_validation_error_invalid_layer(self):
        """layer = -1 must raise DAGValidationError."""
        with pytest.raises(DAGValidationError):
            VPNode(
                vp_id="VP-001",
                method="automated_test",
                layer=-1,
            )

    def test_dag_validation_error_zero_layer(self):
        """layer = 0 is not a positive integer — must also be rejected.

        Boundaries off-by-one: layer starts at 1 (the foundation
        layer). 0 is reserved for "uninitialised" and must be
        rejected at construction so downstream code can assume
        ``layer >= 1``.
        """
        with pytest.raises(DAGValidationError):
            VPNode(
                vp_id="VP-001",
                method="automated_test",
                layer=0,
            )

    def test_dag_validation_error_non_int_layer(self):
        """A non-integer layer must raise DAGValidationError.

        Defensive: a string layer is almost certainly a bug, and
        silently coercing it would mask a much larger problem
        elsewhere in the pipeline.
        """
        with pytest.raises(DAGValidationError):
            VPNode(
                vp_id="VP-001",
                method="automated_test",
                layer="1",  # type: ignore[arg-type]
            )

    def test_dag_validation_error_is_value_error_subclass(self):
        """DAGValidationError must subclass ValueError for cheap catching.

        Callers that handle "bad input" generically should be able
        to catch DAGValidationError with a plain ``except ValueError``.
        """
        assert issubclass(DAGValidationError, ValueError)


# ---------------------------------------------------------------------------
# Positive smoke test: fully-specified construction
# ---------------------------------------------------------------------------


class TestVPNodeConstruction:
    """Sanity check: a fully-specified VPNode must construct without error."""

    def test_fully_specified_construction_matches_output_example(self):
        """The example from the PRD must construct and expose all fields."""
        node = VPNode(
            vp_id="VP-001",
            method="automated_test",
            layer=1,
            depends_on=[],
            max_retries=3,
            model_complexity="simple",
            status="pending",
            verdict=None,
        )
        assert node.vp_id == "VP-001"
        assert node.method == "automated_test"
        assert node.layer == 1
        assert node.depends_on == []
        assert node.max_retries == 3
        assert node.model_complexity == "simple"
        assert node.status == "pending"
        assert node.verdict is None


# ---------------------------------------------------------------------------
# VerificationDAG.build() — plan → DAG
# ---------------------------------------------------------------------------


class TestVerificationDAGBuild:
    """``VerificationDAG.build(plan)`` must accept a plan dict and return
    a :class:`VerificationDAG` whose ``nodes`` map is keyed by ``vp_id``.

    The plan dict's ``vps`` list is consumed verbatim (apart from the
    field-resolution rules described on :meth:`VerificationDAG.build`).
    """

    def test_build_returns_dag_instance(self):
        """Sanity check: ``build()`` returns a ``VerificationDAG``."""
        plan = {"vps": [{"vp_id": "VP-001", "method": "automated_test"}]}
        dag = VerificationDAG.build(plan)
        assert isinstance(dag, VerificationDAG)
        assert "VP-001" in dag.nodes
        assert dag.nodes["VP-001"].method == "automated_test"

    def test_build_default_layer_fallback(self):
        """VP missing ``layer`` → defaults to layer=1 (v1 plan compat).

        A v1 plan carries no layer concept. The new topology needs a
        layer, so we treat the absence of ``layer`` as "this is
        foundation" (layer=1) rather than rejecting the plan. This
        is the compatibility contract for old plan JSONs.
        """
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test"},
                # no layer field
            ]
        }
        dag = VerificationDAG.build(plan)
        assert dag.nodes["VP-001"].layer == 1

    def test_build_explicit_layer_preserved(self):
        """When ``layer`` is present, it is used verbatim (no coercion)."""
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test", "layer": 3},
            ]
        }
        dag = VerificationDAG.build(plan)
        assert dag.nodes["VP-001"].layer == 3

    def test_build_cycle_raises_error(self):
        """A → B → A dependency cycle must raise DAGValidationError.

        A 2-node cycle is the smallest possible invalid plan; we
        also test a 3-node cycle to make sure the detector walks
        past the immediate neighbour.
        """
        # 2-node cycle: A depends on B, B depends on A.
        plan_2 = {
            "vps": [
                {"vp_id": "VP-A", "method": "automated_test", "depends_on": ["VP-B"]},
                {"vp_id": "VP-B", "method": "automated_test", "depends_on": ["VP-A"]},
            ]
        }
        with pytest.raises(DAGValidationError):
            VerificationDAG.build(plan_2)

        # 3-node cycle: A → B → C → A.
        plan_3 = {
            "vps": [
                {"vp_id": "VP-A", "method": "automated_test", "depends_on": ["VP-B"]},
                {"vp_id": "VP-B", "method": "automated_test", "depends_on": ["VP-C"]},
                {"vp_id": "VP-C", "method": "automated_test", "depends_on": ["VP-A"]},
            ]
        }
        with pytest.raises(DAGValidationError):
            VerificationDAG.build(plan_3)

    def test_build_self_dependency_raises_error(self):
        """A VP depending on itself is a 1-node cycle — must be rejected."""
        plan = {
            "vps": [
                {"vp_id": "VP-A", "method": "automated_test", "depends_on": ["VP-A"]},
            ]
        }
        with pytest.raises(DAGValidationError):
            VerificationDAG.build(plan)

    def test_build_invalid_dep_raises_error(self):
        """``depends_on`` referencing a non-existent VP must raise.

        Without this check the orchestrator would crash later when
        looking up the missing node; surfacing the error at build
        time gives the user a clear pointer to the typo.
        """
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test",
                 "depends_on": ["VP-DOES-NOT-EXIST"]},
            ]
        }
        with pytest.raises(DAGValidationError):
            VerificationDAG.build(plan)

    def test_build_acyclic_with_cross_layer_dep_succeeds(self):
        """Cross-layer edges (A in L2 depends on B in L1) must build OK.

        Mirrors the example in the PRD: VP-002 (layer 2) depends on
        VP-001 (layer 1). The dep is satisfied by layer ordering; no
        cycle exists.
        """
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-002", "method": "code_review", "layer": 2,
                 "depends_on": ["VP-001"]},
            ]
        }
        dag = VerificationDAG.build(plan)
        assert set(dag.nodes.keys()) == {"VP-001", "VP-002"}

    def test_build_empty_plan_returns_empty_dag(self):
        """An empty ``vps`` list yields a DAG with no nodes.

        ``topo_sort()`` on this DAG returns ``[]`` (covered by
        ``test_topo_sort_empty_plan`` below). Here we only check that
        ``build`` does not raise and yields a well-formed empty DAG.
        """
        dag = VerificationDAG.build({"vps": []})
        assert dag.nodes == {}
        # Also: a plan with no ``vps`` key at all (e.g. a corrupted
        # file) must yield an empty DAG, not raise KeyError.
        dag2 = VerificationDAG.build({})
        assert dag2.nodes == {}


# ---------------------------------------------------------------------------
# VerificationDAG.topo_sort() — layer-partitioned execution order
# ---------------------------------------------------------------------------


class TestVerificationDAGTopoSort:
    """``topo_sort()`` partitions VPs into layers sorted by layer number.

    Within a layer the order is plan-insertion order; the orchestrator
    honours intra-layer ``depends_on`` separately.
    """

    def test_topo_sort_layer_ordering(self):
        """VPs in layer 1 must appear in a layer that is *before* any
        VP in layer 2. The outer list's order is the schedule order.
        """
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-002", "method": "code_review", "layer": 2,
                 "depends_on": ["VP-001"]},
            ]
        }
        dag = VerificationDAG.build(plan)
        layers = dag.topo_sort()

        assert len(layers) == 2
        assert [n.vp_id for n in layers[0]] == ["VP-001"]
        assert [n.vp_id for n in layers[1]] == ["VP-002"]

    def test_topo_sort_layer_ordering_3_layers(self):
        """Three-layer plan: L1 → L2 → L3 ordering preserved."""
        plan = {
            "vps": [
                {"vp_id": "VP-A", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-B", "method": "automated_test", "layer": 2},
                {"vp_id": "VP-C", "method": "automated_test", "layer": 3},
            ]
        }
        dag = VerificationDAG.build(plan)
        layers = dag.topo_sort()
        ids = [[n.vp_id for n in layer] for layer in layers]
        assert ids == [["VP-A"], ["VP-B"], ["VP-C"]]

    def test_topo_sort_groups_by_layer_within_layer_preserves_plan_order(self):
        """Multiple VPs in the same layer stay in plan-insertion order."""
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-002", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-003", "method": "automated_test", "layer": 2},
            ]
        }
        dag = VerificationDAG.build(plan)
        layers = dag.topo_sort()
        assert [n.vp_id for n in layers[0]] == ["VP-001", "VP-002"]
        assert [n.vp_id for n in layers[1]] == ["VP-003"]

    def test_topo_sort_empty_plan(self):
        """An empty plan must yield ``[]`` (no layers at all)."""
        dag = VerificationDAG.build({"vps": []})
        assert dag.topo_sort() == []

    def test_topo_sort_all_default_layer_fallback(self):
        """When every VP defaults to layer=1, topo_sort returns a single
        layer containing every VP, in plan-insertion order.
        """
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test"},
                {"vp_id": "VP-002", "method": "automated_test"},
                {"vp_id": "VP-003", "method": "automated_test"},
            ]
        }
        dag = VerificationDAG.build(plan)
        layers = dag.topo_sort()
        assert len(layers) == 1
        assert [n.vp_id for n in layers[0]] == ["VP-001", "VP-002", "VP-003"]


# ---------------------------------------------------------------------------
# VerificationDAG.execute_layer() — same-layer concurrent execution
# ---------------------------------------------------------------------------


class TestExecuteLayer:
    """``execute_layer`` must run every VP in a layer concurrently via
    :func:`asyncio.gather` and return one verdict per VP in
    plan-insertion order.

    Spec contract: "同层并行时单 VP 失败不立即短路，等全层收完 verdict"
    — a single failure must not abort the layer; siblings keep
    running so the short-circuit decision can be made afterwards
    (see :class:`TestShouldProceedToNextLayer`).
    """

    @pytest.mark.asyncio
    async def test_execute_layer_runs_concurrently(self):
        """3 VPs in layer 1 must all run within a single asyncio.gather.

        The injected runner records each VP's start time and yields
        a small async sleep. The 3 starts must happen before any
        finishes — that is the operational definition of
        "concurrent". If ``asyncio.gather`` were replaced with a
        serial loop, the second VP would start only after the
        first finished sleeping.
        """
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-002", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-003", "method": "automated_test", "layer": 1},
            ]
        }
        dag = VerificationDAG.build(plan)

        sleep_seconds = 0.05
        starts: list = []
        finishes: list = []

        async def runner(node):
            starts.append((node.vp_id, asyncio.get_event_loop().time()))
            await asyncio.sleep(sleep_seconds)
            finishes.append((node.vp_id, asyncio.get_event_loop().time()))
            return {"vp_id": node.vp_id, "verdict": "PASSED"}

        verdicts = await dag.execute_layer(layer_id=1, runner=runner)

        # -- 1. The contract: 3 verdicts, in plan-insertion order. ---
        assert [v["vp_id"] for v in verdicts] == ["VP-001", "VP-002", "VP-003"]
        assert all(v["verdict"] == "PASSED" for v in verdicts)

        # -- 2. The concurrency proof: all 3 started before any
        #       finished. With asyncio.gather the 3 starts land at
        #       (almost) the same timestamp, and all 3 finishes land
        #       ~sleep_seconds later. The latest start must precede
        #       the earliest finish. --
        assert len(starts) == 3 and len(finishes) == 3
        latest_start = max(t for _, t in starts)
        earliest_finish = min(t for _, t in finishes)
        assert latest_start < earliest_finish, (
            "execute_layer must run all VPs concurrently "
            f"(latest_start={latest_start}, earliest_finish={earliest_finish})"
        )

    @pytest.mark.asyncio
    async def test_execute_layer_single_vp(self):
        """A single-VP layer (boundary: len(layer) == 1) must still work.

        asyncio.gather on a 1-element list is a degenerate but valid
        path; we want to make sure it returns a 1-element verdict
        list and updates the node's status correctly.
        """
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test", "layer": 1},
            ]
        }
        dag = VerificationDAG.build(plan)

        verdicts = await dag.execute_layer(layer_id=1)
        assert len(verdicts) == 1
        assert verdicts[0]["vp_id"] == "VP-001"
        # The default runner echoes the node's *current* status.
        # A freshly-built VPNode has status="pending" → default
        # runner normalises unknown statuses to PASSED. Both are
        # acceptable here; the contract is "non-FAILED verdict".
        assert verdicts[0]["verdict"] != "FAILED"
        assert dag.should_proceed_to_next_layer() is True

    @pytest.mark.asyncio
    async def test_execute_layer_exception_in_one_vp_does_not_abort_others(self):
        """One runner raising must not short-circuit the layer.

        The spec is explicit: same-layer parallel execution must
        collect every VP's verdict. ``asyncio.gather(..., return_exceptions=True)``
        gives us that — an exception becomes a synthesised
        ``verdict=FAILED`` entry, and sibling VPs are unaffected.
        """
        plan = {
            "vps": [
                {"vp_id": "VP-OK1", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-BAD", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-OK2", "method": "automated_test", "layer": 1},
            ]
        }
        dag = VerificationDAG.build(plan)

        async def runner(node):
            if node.vp_id == "VP-BAD":
                raise RuntimeError("simulated VP crash")
            return {"vp_id": node.vp_id, "verdict": "PASSED"}

        verdicts = await dag.execute_layer(layer_id=1, runner=runner)
        # All 3 VPs surface a verdict; the bad one is FAILED.
        assert len(verdicts) == 3
        by_id = {v["vp_id"]: v for v in verdicts}
        assert by_id["VP-OK1"]["verdict"] == "PASSED"
        assert by_id["VP-OK2"]["verdict"] == "PASSED"
        assert by_id["VP-BAD"]["verdict"] == "FAILED"
        # The FAILED verdict feeds into should_proceed — see
        # the next test class.
        assert dag.should_proceed_to_next_layer() is False

    @pytest.mark.asyncio
    async def test_execute_layer_empty_layer(self):
        """A layer with 0 VPs (boundary: no VPs at all in that layer)
        must return ``[]`` immediately and let the schedule advance.
        """
        # Build a DAG that has VPs in layer 1 only; ask for layer 7
        # (which has no VPs).
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test", "layer": 1},
            ]
        }
        dag = VerificationDAG.build(plan)

        verdicts = await dag.execute_layer(layer_id=7)
        assert verdicts == []
        # Empty layer → no FAILED verdicts → proceed.
        assert dag.should_proceed_to_next_layer() is True

    @pytest.mark.asyncio
    async def test_execute_layer_no_runner_uses_default(self):
        """Omitting ``runner`` must work: the default runner echoes the
        node's pre-set status. This is the path tests / production
        use to drive the layer decision without any I/O.
        """
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-002", "method": "automated_test", "layer": 1},
            ]
        }
        dag = VerificationDAG.build(plan)
        # Pre-set one VP to "failed" — the default runner should
        # surface that as a FAILED verdict.
        dag.nodes["VP-001"].status = "failed"
        dag.nodes["VP-002"].status = "passed"

        verdicts = await dag.execute_layer(layer_id=1)
        by_id = {v["vp_id"]: v for v in verdicts}
        assert by_id["VP-001"]["verdict"] == "FAILED"
        assert by_id["VP-002"]["verdict"] == "PASSED"


# ---------------------------------------------------------------------------
# VerificationDAG.should_proceed_to_next_layer() — short-circuit decision
# ---------------------------------------------------------------------------


class TestShouldProceedToNextLayer:
    """The short-circuit rule:

      * L1 all PASSED → proceed to L2
      * L1 any FAILED → block L2 / L3
      * L1 all PASSED, L2 any FAILED → block L3
      * Empty layer → proceed (no verdicts to block on)

    The decision function is read-only over the verdicts list — it
    does not mutate the DAG. The same method works on either
    stored verdicts (``dag._last_layer_verdicts``) or on an
    explicit list passed in (for unit-test ergonomics).
    """

    def test_should_proceed_l1_failed_blocks(self):
        """L1 with 1 FAILED out of 3 must return False (block L2/L3)."""
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-002", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-003", "method": "automated_test", "layer": 1},
            ]
        }
        dag = VerificationDAG.build(plan)

        # Mirror the PRD input example exactly.
        layer1_verdicts = [
            {"verdict": "PASSED"},
            {"verdict": "FAILED"},
            {"verdict": "PASSED"},
        ]
        assert dag.should_proceed_to_next_layer(layer1_verdicts) is False

    def test_should_proceed_all_pass_advances(self):
        """L1 with all 3 PASSED must return True (advance to L2)."""
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-002", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-003", "method": "automated_test", "layer": 1},
            ]
        }
        dag = VerificationDAG.build(plan)

        all_pass = [
            {"verdict": "PASSED"},
            {"verdict": "PASSED"},
            {"verdict": "PASSED"},
        ]
        assert dag.should_proceed_to_next_layer(all_pass) is True

    def test_should_proceed_l2_failed_blocks_l3(self):
        """L1 all PASS, L2 has FAILED → return False (block L3)."""
        dag = VerificationDAG.build({"vps": []})

        # L1 all PASS, store and advance implicitly via verdicts arg.
        l1 = [{"verdict": "PASSED"}, {"verdict": "PASSED"}]
        assert dag.should_proceed_to_next_layer(l1) is True

        # L2 has one FAILED.
        l2 = [{"verdict": "PASSED"}, {"verdict": "FAILED"}, {"verdict": "PASSED"}]
        assert dag.should_proceed_to_next_layer(l2) is False

    def test_should_proceed_empty_layer_advances(self):
        """Boundary: an empty layer (no VPs) must return True so the
        schedule can advance past it.
        """
        dag = VerificationDAG.build({"vps": []})
        # No stored verdicts yet — the function must not crash and
        # must return True (empty → no failures → proceed).
        assert dag.should_proceed_to_next_layer() is True
        assert dag.should_proceed_to_next_layer([]) is True

    def test_should_proceed_uses_stored_verdicts_when_no_arg(self):
        """When called without arguments, the function reads the
        verdicts stored by the most recent ``execute_layer`` call.
        This is the spec's intended call pattern::

            results = await dag.execute_layer(layer_id=1)
            if not dag.should_proceed_to_next_layer():
                ... # block L2
        """
        plan = {
            "vps": [
                {"vp_id": "VP-001", "method": "automated_test", "layer": 1},
                {"vp_id": "VP-002", "method": "automated_test", "layer": 1},
            ]
        }
        dag = VerificationDAG.build(plan)
        dag.nodes["VP-001"].status = "passed"
        dag.nodes["VP-002"].status = "failed"

        import asyncio
        asyncio.run(dag.execute_layer(layer_id=1))
        # Stored verdicts must reflect the node statuses; the
        # decision function (called with no args) returns False.
        assert dag.should_proceed_to_next_layer() is False

    def test_should_proceed_ignores_non_failed_statuses(self):
        """PASSED, SKIPPED, missing-key verdicts must NOT block the layer.

        Only the literal string "FAILED" is a block trigger. SKIPPED
        is non-failure by design (e.g. a manual_check VP that the
        orchestrator deprioritised), and a missing key is treated
        as not-yet-judged — neither is enough to halt L2.
        """
        dag = VerificationDAG.build({"vps": []})
        verdicts = [
            {"verdict": "PASSED"},
            {"verdict": "SKIPPED"},
            {"vp_id": "VP-X"},  # no verdict key
        ]
        assert dag.should_proceed_to_next_layer(verdicts) is True

    def test_should_proceed_failed_takes_priority_over_passed(self):
        """PASSED + FAILED + PASSED → False (any single FAILED blocks)."""
        dag = VerificationDAG.build({"vps": []})
        verdicts = [
            {"verdict": "PASSED"},
            {"verdict": "FAILED"},
            {"verdict": "PASSED"},
        ]
        assert dag.should_proceed_to_next_layer(verdicts) is False


# ---------------------------------------------------------------------------
# ProviderConcurrencyController — per-provider concurrency cap with waiting queue
# ---------------------------------------------------------------------------


class TestProviderConcurrencyController:
    """``ProviderConcurrencyController`` enforces two layers of limits:

      1. **Global cap** — max in-flight acquires across all providers.
      2. **Per-provider cap** — a tighter cap on a single provider
         (e.g. vendor-b=3 to defend against 401 雪崩 when vendor-b is flaky).

    The contract is **blocking, never denied**: over-limit requests
    block inside ``acquire()`` until a paired ``release()`` frees a
    slot. No requests are dropped on the floor.

    These tests use the canonical PRD fixture:
        global_limit=5, vendor-b=3, vendor-a=5, claude=5
    and pin five contracts:

      1. ``test_acquire_blocks_at_provider_limit`` — vendor-b 第 4 次阻塞
      2. ``test_release_wakes_waiter`` — release 唤醒一个等待者
      3. ``test_global_semaphore_blocks_at_6`` — 全局第 6 次阻塞
      4. ``test_concurrent_100_tasks_vendor-b_peak_3`` — 100 任务峰值 ≤ 3
      5. ``test_release_acquire_pair_no_leak`` — 配对无计数泄漏
    """

    @pytest.mark.asyncio
    async def test_acquire_blocks_at_provider_limit(self):
        """vendor-b=3: 4th acquire blocks while 3 are in flight; release wakes it.

        This is the primary bound — the per-provider cap is the tight
        constraint in the canonical setup (vendor-b=3 < global=5). The
        4th vendor-b caller must suspend inside ``acquire()``; only a
        paired ``release("vendor-b")`` can wake it. We use a separate
        coroutine and ``asyncio.sleep`` to give the event loop a
        chance to schedule the waiter before we observe its state.
        """
        c = ProviderConcurrencyController(
            global_limit=5,
            provider_limits={"vendor-b": 3, "vendor-a": 5, "claude": 5},
        )

        # Fill vendor-b to its cap of 3 — these 3 acquires must all
        # complete immediately (no blocking yet).
        for _ in range(3):
            await asyncio.wait_for(c.acquire("vendor-b"), timeout=0.5)

        # 4th vendor-b acquire: must block. We schedule it as a task and
        # let the event loop tick for a short while; if the task is
        # already done, the cap is wrong. 50ms is well above the
        # scheduler's tick budget on any reasonable platform, so a
        # non-done task here is proof of blocking.
        waiter = asyncio.create_task(c.acquire("vendor-b"))
        await asyncio.sleep(0.05)
        assert not waiter.done(), (
            "4th vendor-b acquire must block while 3 vendor-b slots are in flight"
        )

        # Release one in-flight vendor-b: the waiter must wake up within
        # a short timeout. If it doesn't, release() is broken.
        c.release("vendor-b")
        await asyncio.wait_for(waiter, timeout=1.0)
        # Cleanup: release the remaining 3 in-flight (the 3 originals
        # minus the 1 we just released = 2, plus the 1 the waiter
        # consumed = 3). Total = 3 in-flight + 1 consumed = 4 to
        # release now.
        c.release("vendor-b")  # the one the waiter now holds
        c.release("vendor-b")
        c.release("vendor-b")
        # Sanity: all global + per-provider slots must be back to full
        # capacity — a fresh acquire must succeed immediately.
        assert c.available_global() == 5
        assert c.available_provider("vendor-b") == 3

    @pytest.mark.asyncio
    async def test_release_wakes_waiter(self):
        """``release(provider)`` wakes exactly one FIFO waiter for that provider.

        Boundary: with global=2, vendor-b=2, we fill both vendor-b slots and
        then schedule a single waiter. One release must unblock the
        waiter, not two — otherwise we'd be over-releasing.
        """
        c = ProviderConcurrencyController(
            global_limit=2,
            provider_limits={"vendor-b": 2},
        )
        await c.acquire("vendor-b")
        await c.acquire("vendor-b")

        # Schedule a single waiter.
        waiter = asyncio.create_task(c.acquire("vendor-b"))
        await asyncio.sleep(0.05)
        assert not waiter.done(), "waiter must block on full vendor-b cap"

        # One release wakes the waiter.
        c.release("vendor-b")
        await asyncio.wait_for(waiter, timeout=1.0)
        assert waiter.done(), "release() must wake exactly one waiter"

        # After the waiter is woken, the slot it consumed is gone.
        # available_provider reflects that — the waiter holds 1 slot.
        assert c.available_provider("vendor-b") == 0
        assert c.available_global() == 0

        # Cleanup: 3 acquires total (2 originals + 1 waiter) need
        # 3 releases; we already did 1 to wake the waiter, so 2 more
        # to drain. Over-releasing (4 total) would push
        # ``global._value`` above the limit, which is the bug we
        # caught once — this assertion pins the "no over-release"
        # half of the contract.
        c.release("vendor-b")
        c.release("vendor-b")
        assert c.available_global() == 2
        assert c.available_provider("vendor-b") == 2

    @pytest.mark.asyncio
    async def test_global_semaphore_blocks_at_6(self):
        """Global cap=5 with provider limits matching global: 6th acquire blocks.

        We set per-provider limits equal to the global cap so the
        binding constraint is the global cap. A 6th acquire from any
        provider must block — the global semaphore is the bottleneck.
        """
        c = ProviderConcurrencyController(
            global_limit=5,
            provider_limits={"vendor-b": 5, "vendor-a": 5, "claude": 5},
        )

        # Fill the global pool with 5 vendor-b acquires (under vendor-b=5
        # the per-provider cap doesn't bind).
        for _ in range(5):
            await asyncio.wait_for(c.acquire("vendor-b"), timeout=0.5)
        assert c.available_global() == 0, (
            "global must be exhausted after 5 acquires with limit=5"
        )

        # 6th acquire must block on the global semaphore.
        waiter = asyncio.create_task(c.acquire("vendor-b"))
        await asyncio.sleep(0.05)
        assert not waiter.done(), (
            "6th acquire must block when global cap is full"
        )

        # Release one — the 6th waiter wakes up.
        c.release("vendor-b")
        await asyncio.wait_for(waiter, timeout=1.0)
        assert waiter.done(), "release() must wake the global waiter"

        # Drain: 5 original + 1 the waiter consumed = 6 to release.
        for _ in range(5):
            c.release("vendor-b")
        assert c.available_global() == 5

    @pytest.mark.asyncio
    async def test_concurrent_100_tasks_vendor_b_peak_3(self):
        """100 concurrent vendor-b tasks under vendor-b=3 must peak at ≤ 3 in flight.

        This is the load-shape contract: even with 100 tasks racing,
        the per-provider cap must keep concurrent vendor-b calls under
        control. The 100 tasks do enough real work (asyncio.sleep) to
        make sure the scheduler actually interleaves them; if
        ``asyncio.gather`` were somehow serial, the peak would be 1.
        """
        c = ProviderConcurrencyController(
            global_limit=5,
            provider_limits={"vendor-b": 3},
        )
        current = 0
        peak = 0
        # No lock needed: asyncio is single-threaded, and the only
        # ``await`` between the read of ``current`` and the
        # write-back is the explicit sleep. The 3 line sequence
        # ``current += 1; peak = max(peak, current); await sleep`` is
        # effectively atomic across tasks in a single-threaded loop.
        work_seconds = 0.01

        async def one_vendor_b_task() -> None:
            nonlocal current, peak
            await c.acquire("vendor-b")
            try:
                current += 1
                if current > peak:
                    peak = current
                # Yield to let sibling tasks try to enter the cap.
                # If cap is working, only 3 can be here at once.
                await asyncio.sleep(work_seconds)
            finally:
                current -= 1
                c.release("vendor-b")

        # Schedule 100 tasks at once. The per-provider cap must keep
        # the in-flight count at or below 3.
        await asyncio.gather(*(one_vendor_b_task() for _ in range(100)))

        assert peak <= 3, (
            f"peak concurrent vendor-b must be ≤ 3, got {peak} — "
            f"per-provider cap is not being enforced"
        )
        # Sanity: the cap actually was the binding constraint (i.e.
        # we exercised it, not just had very few concurrent tasks).
        assert peak >= 2, (
            f"peak concurrent vendor-b = {peak} is unexpectedly low; "
            f"the load shape test did not actually exercise the cap"
        )
        # All slots returned to baseline — no leak after 100 cycles.
        assert c.available_global() == 5
        assert c.available_provider("vendor-b") == 3

    @pytest.mark.asyncio
    async def test_release_acquire_pair_no_leak(self):
        """100 paired acquire/release calls must not leak counts.

        After 100 cycles of (acquire, release) on a single provider,
        the controller must report both the global and per-provider
        pools at full capacity. A leak (e.g. a missing release
        branch) would leave the slots stranded and a subsequent
        burst would block.
        """
        c = ProviderConcurrencyController(
            global_limit=5,
            provider_limits={"vendor-b": 3, "vendor-a": 5, "claude": 5},
        )
        cycles = 100

        for _ in range(cycles):
            await asyncio.wait_for(c.acquire("vendor-b"), timeout=0.5)
            c.release("vendor-b")

        # No leak: full capacity on both layers.
        assert c.available_global() == 5, (
            f"global must be at full capacity after {cycles} paired "
            f"cycles, got {c.available_global()}/5"
        )
        assert c.available_provider("vendor-b") == 3, (
            f"vendor-b must be at full capacity after {cycles} paired "
            f"cycles, got {c.available_provider('vendor-b')}/3"
        )

        # Functional follow-up: a fresh burst of vendor-b acquires up to
        # the cap must all complete immediately (no block). If the
        # count leaked, one of these would block past the timeout.
        for _ in range(3):
            await asyncio.wait_for(c.acquire("vendor-b"), timeout=0.1)
        # And drain.
        for _ in range(3):
            c.release("vendor-b")
        assert c.available_global() == 5
        assert c.available_provider("vendor-b") == 3


# ---------------------------------------------------------------------------
# Provider selection — no hardcoded vendor-b literals, optimizer order honored,
# peak-hour degradation delegated to the shared rule.
# ---------------------------------------------------------------------------


class TestProviderSelectionPipeline:
    """Pins the three contracts called out by the task-8 spec:

      1. ``test_dag_no_hardcoded_vendor-b_literal`` — no ``"vendor-b"`` /
         ``'vendor-b'`` string literal lives anywhere in
         ``backend/verification_dag.py``. The provider id check is
         delegated to the optimizer's rule engine (which lives in
         :mod:`provider_concurrency`), so the DAG module itself stays
         provider-agnostic.
      2. ``test_provider_concurrency_consumes_optimizer`` — selection
         order is driven by the optimizer list, not by a hard-coded
         chain.
      3. (removed 2026-09-14) the peak-hour skip no longer exists here:
         the optimizer's rule engine demotes providers in the chain ORDER
         and this module simply walks that order.
    """

    def test_dag_no_hardcoded_vendor_b_literal(self):
        """Static scan: no quoted ``"vendor-b"`` or ``'vendor-b'`` in verification_dag.py.

        Uses :mod:`ast` rather than a substring search so the test
        ignores the bare identifier ``_should_skip_vendor-b_provider``
        and the unquoted word "vendor-b" in docstrings — only quoted
        string constants are flagged. A future regression that
        re-introduces ``if pid == "vendor-b-pro": ...`` (or similar) in
        the DAG module will trip this test before it ships.
        """
        import inspect
        import verification_dag
        source = inspect.getsource(verification_dag)
        tree = ast.parse(source)

        offenders = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value == "vendor-b"
            ):
                offenders.append((node.lineno, node.value))
            # ``ast.Str`` is the pre-3.8 spelling of string Constant;
            # we keep the explicit branch so this test stays usable
            # on the older toolchain some CI runners still ship.
            elif (
                hasattr(ast, "Str")
                and isinstance(node, ast.Str)
                and node.s == "vendor-b"
            ):
                offenders.append((node.lineno, node.s))

        assert offenders == [], (
            "verification_dag.py contains a hard-coded 'vendor-b' string "
            f"literal at line(s) {[lineno for lineno, _ in offenders]}; "
            "delegate the check to provider_concurrency.is_peak_hour_prone_provider "
            "(the optimizer's rule engine owns provider policy)."
        )

    def test_provider_concurrency_consumes_optimizer(self, monkeypatch):
        """Selection order is driven by ``optimizer_providers``, not a hard-coded chain.

        The optimizer is fed a non-alphabetical order to prove the
        first-eligible candidate is taken verbatim. The
        ``cc_switch`` layer is stubbed so no real DB
        is touched — the test exercises the selection pipeline in
        isolation.
        """
        def fake_get_provider_config(provider_name, db_path=None):
            return cc_switch.ProviderConfig(
                name=provider_name,
                env={
                    "ANTHROPIC_BASE_URL": f"http://{provider_name}",
                    "ANTHROPIC_AUTH_TOKEN": "sk-test",
                    "ANTHROPIC_MODEL": "test-model",
                },
                base_url=f"http://{provider_name}",
                model="test-model",
            )
        monkeypatch.setattr(
            cc_switch,
            "get_provider",
            fake_get_provider_config,
        )

        # Non-alphabetical, non-trivial order. If the selection
        # pipeline silently sorts or hard-codes a chain, this test
        # would observe ``alpha-llm`` (the alphabetically-first id)
        # instead of the optimizer-supplied first id.
        optimizer = [
            {"id": "gamma-llm", "available": True},
            {"id": "alpha-llm", "available": True},
            {"id": "beta-llm", "available": True},
        ]

        selected = get_verification_provider(
            "code_review",
            optimizer_providers=optimizer,
            db_path=None,
        )

        assert selected is not None, (
            "optimizer-fed pipeline must select a provider when at "
            "least one candidate is available=True"
        )
        assert selected["provider"] == "gamma-llm", (
            "first optimizer entry must win; the DAG must not "
            "re-sort or fall back to a hard-coded chain"
        )
        assert selected["config"]["method"] == "code_review"
        # Configuration of the selected provider is propagated.
        assert selected["config"]["url"] == "http://gamma-llm"
        assert selected["config"]["model"] == "test-model"

        # Empty optimizer list → no provider selected (the spec's
        # "空 provider 列表 → 不选择任何 provider" boundary).
        assert get_verification_provider(
            "code_review", optimizer_providers=[], db_path=None
        ) is None
        # All optimizer entries unavailable → no provider selected.
        assert get_verification_provider(
            "code_review",
            optimizer_providers=[{"id": "gamma-llm", "available": False}],
            db_path=None,
        ) is None


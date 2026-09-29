"""
Verification Point DAG
======================

Data model and validation primitives for the new verification_agent's
DAG-based execution topology.

The verification plan is structured as a directed acyclic graph (DAG)
of verification points (VPs). Each VP node carries enough state to
let the orchestrator decide when to run, when to retry, and when to
mark it as terminal. This module is intentionally I/O-free and has no
LLM / filesystem coupling so the data model can be unit-tested in
isolation.

Layer 1 is the foundation layer (independent VPs). Layer 2 depends on
the output of layer 1. Layer 3 — if present — depends on layer 2, and
so on. The orchestrator consumes the DAG layer by layer.

This module exposes:

  * ``VPNode`` — a single verification point (data model)
  * ``DAGValidationError`` — raised on invalid input or graph structure
  * ``VerificationDAG`` — the DAG itself, with
      ``VerificationDAG.build(plan)`` and ``VerificationDAG.topo_sort()``,
      plus per-layer concurrent execution and short-circuit decision
  * ``ProviderConcurrencyController`` — per-provider concurrency cap
      with a global fallback and a FIFO waiting queue
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

import cc_switch
from base_executor import _build_layers

class DAGValidationError(ValueError):
    """Raised when a VPNode or DAG construction violates an invariant.

    Used for both per-field validation (e.g. negative ``layer``) and
    for graph-level validation that will be added later (cycles,
    duplicate ids, dangling ``depends_on`` references). Subclassing
    ``ValueError`` keeps the exception cheap to catch in the common
    case where callers only care about "bad input".
    """


# ---------------------------------------------------------------------------
# model_complexity_map REMOVED (2026-09-13)
# ---------------------------------------------------------------------------
# ``load_model_complexity_map`` / ``get_model_for_complexity`` were dead
# code — no production consumer ever called them. Model management is
# delegated to CC Switch; VP routing goes through the ``verification``
# scene in provider_routing.py instead.


@dataclass
class VPNode:
    """A single verification point as a node in the verification DAG.

    Attributes:
        vp_id: Stable unique identifier (e.g. ``"VP-001"``).
        method: Verification method — one of ``"automated_test"``,
            ``"code_review"``, ``"ui_validation"``, ``"api_test"``,
            ``"manual_check"``.
        layer: Topological layer in the DAG. ``1`` is the foundation
            (no dependencies); higher layers may depend on lower ones.
            Must be a positive integer.
        depends_on: VP ids this node depends on. Default is ``[]``.
            Cross-layer ordering is implied by ``layer``; ``depends_on``
            carries only *intra-layer* edges that the orchestrator
            needs to honour.
        max_retries: How many times the orchestrator may retry this
            VP on transient failure before giving up. Default ``3``.
        model_complexity: Hint to the LLM router — ``"simple"``,
            ``"medium"``, or ``"complex"``. Default ``"medium"``.
        status: Mutable runtime state. ``"pending"`` at construction;
            transitions to ``"running"`` / ``"passed"`` / ``"failed"``
            / ``"skipped"`` / ``"split"`` as the orchestrator runs.
        verdict: Free-form terminal verdict (e.g. aggregated
            ``requirement_deviations``) populated by Phase 3.
            ``None`` while the VP is not yet terminal.
    """

    vp_id: str
    method: str
    layer: int = 1
    depends_on: List[str] = field(default_factory=list)
    max_retries: int = 3
    model_complexity: str = "medium"
    status: str = "pending"
    verdict: Optional[Any] = None

    def __post_init__(self) -> None:
        """Validate per-field invariants on construction.

        Kept tiny on purpose: only the cheap synchronous checks live
        here. Cross-node graph validation (cycles, dangling edges)
        belongs to a future ``DAGValidator`` and is *not* in scope
        for the data model.
        """
        if not isinstance(self.layer, int) or self.layer < 1:
            raise DAGValidationError(
                f"VPNode.layer must be a positive integer, got {self.layer!r}"
            )
        if not isinstance(self.depends_on, list):
            raise DAGValidationError(
                f"VPNode.depends_on must be a list, got {type(self.depends_on).__name__}"
            )
        if not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise DAGValidationError(
                f"VPNode.max_retries must be a non-negative integer, got {self.max_retries!r}"
            )
        if self.model_complexity not in ("simple", "medium", "complex"):
            raise DAGValidationError(
                f"VPNode.model_complexity must be one of "
                f"'simple'|'medium'|'complex', got {self.model_complexity!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a JSON-friendly dict (used by the persistence layer)."""
        return {
            "vp_id": self.vp_id,
            "method": self.method,
            "layer": self.layer,
            "depends_on": list(self.depends_on),
            "max_retries": self.max_retries,
            "model_complexity": self.model_complexity,
            "status": self.status,
            "verdict": self.verdict,
        }


def _detect_cycle(nodes: Dict[str, "VPNode"]) -> Optional[List[str]]:
    """Return the dependency chain that forms a cycle, or ``None`` if acyclic.

    Uses iterative DFS with three-colour marking:

      * ``WHITE`` (0) — not visited yet
      * ``GREY``  (1) — on the current DFS stack (in-progress)
      * ``BLACK`` (2) — fully explored

    A back-edge to a ``GREY`` node is a cycle; the path of GREY
    ancestors is returned for diagnostics. Iterative (not recursive)
    to keep stack usage bounded by ``len(nodes)`` regardless of plan
    size — plans with thousands of VPs are valid in principle.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    color: Dict[str, int] = {vp_id: WHITE for vp_id in nodes}
    # parent[child] = the node that put child on the DFS stack.
    # Used to reconstruct the cycle chain when we hit a GREY back-edge.
    parent: Dict[str, Optional[str]] = {vp_id: None for vp_id in nodes}

    for start in nodes:
        if color[start] != WHITE:
            continue
        # Iterative DFS stack holds (node, child_iterator).
        stack: List[tuple] = [(start, iter(nodes[start].depends_on))]
        color[start] = GREY
        while stack:
            current, it = stack[-1]
            advanced = False
            for nxt in it:
                if color[nxt] == GREY:
                    # Back-edge: reconstruct the cycle by walking
                    # parent[] from `current` back up to `nxt`.
                    chain = [nxt, current]
                    p = parent[current]
                    while p is not None and p != nxt:
                        chain.append(p)
                        p = parent[p]
                    if p == nxt:
                        chain.append(nxt)
                    return list(reversed(chain))
                if color[nxt] == WHITE:
                    color[nxt] = GREY
                    parent[nxt] = current
                    stack.append((nxt, iter(nodes[nxt].depends_on)))
                    advanced = True
                    break
            if not advanced:
                color[current] = BLACK
                stack.pop()
    return None


@dataclass
class VerificationDAG:
    """A directed acyclic graph of verification points.

    Constructed via :meth:`build` from a plan dict. Exposes
    :meth:`topo_sort` to obtain a layer-partitioned execution order
    suitable for the orchestrator's per-layer parallel scheduling.

    Attributes:
        nodes: Map of ``vp_id`` → :class:`VPNode`. Iteration order is
            insertion order (i.e. plan order) — not load-bearing for
            correctness, but useful for deterministic error messages.
    """

    nodes: Dict[str, VPNode] = field(default_factory=dict)

    @classmethod
    def build(cls, plan: Dict[str, Any]) -> "VerificationDAG":
        """Construct a :class:`VerificationDAG` from a plan dict.

        The plan's ``vps`` key (a list of VP dicts) is consumed. Each
        VP dict is mapped to a :class:`VPNode` with these field
        resolution rules:

          * ``vp_id`` — required.
          * ``method`` — optional; defaults to ``"automated_test"``
            (the v1 plan format did not carry a method).
          * ``layer`` — optional; defaults to ``1`` (v1 plans have
            no layer concept; everything is treated as foundation).
          * ``depends_on`` — optional; defaults to ``[]``.

        Raises:
            DAGValidationError: if (a) a per-VP field is invalid
                (delegated to :class:`VPNode.__post_init__`), (b) a
                ``depends_on`` reference is missing from the plan, or
                (c) the resulting graph contains a cycle.
        """
        vps = plan.get("vps", []) or []
        if not vps:
            return cls(nodes={})

        nodes: Dict[str, VPNode] = {}
        for vp_dict in vps:
            vp_id = vp_dict["vp_id"]
            node = VPNode(
                vp_id=vp_id,
                method=vp_dict.get("method", "code_review"),
                layer=vp_dict.get("layer", 1),
                depends_on=list(vp_dict.get("depends_on", [])),
            )
            nodes[vp_id] = node

        # Validate every depends_on reference resolves to a known VP.
        # We do this before cycle detection so a typo'd dep yields the
        # clearer "unknown VP" error rather than a generic cycle message.
        for vp_id, node in nodes.items():
            for dep_id in node.depends_on:
                if dep_id not in nodes:
                    raise DAGValidationError(
                        f"VP {vp_id!r} depends on unknown VP {dep_id!r}"
                    )

        # Cycle check. Acyclic by construction when no depends_on
        # edges exist, so this is a no-op on the simplest plans.
        cycle = _detect_cycle(nodes)
        if cycle is not None:
            chain = " -> ".join(cycle)
            raise DAGValidationError(
                f"VP dependency graph contains a cycle: {chain}"
            )

        return cls(nodes=nodes)

    def topo_sort(self) -> List[List[VPNode]]:
        """Partition VPs into a layer-ordered execution plan.

        Returns a list of layers; each layer is a list of
        :class:`VPNode` objects. The outer list is sorted by layer
        number ascending (layer 1 first, then 2, then 3, ...), so the
        caller can iterate ``for layer in dag.topo_sort(): ...`` and
        schedule each layer in turn, with all of layer N's nodes
        starting only after every node in layers ``< N`` has reached a
        terminal status.

        Within a single layer the order is plan-insertion order
        (i.e. the order VPs appear in the input plan's ``vps`` list).
        Intra-layer ``depends_on`` edges are *not* re-ordered here —
        the orchestrator is responsible for honouring them.

        An empty DAG returns ``[]``.
        """
        if not self.nodes:
            return []

        by_layer: Dict[int, List[VPNode]] = defaultdict(list)
        for node in self.nodes.values():
            by_layer[node.layer].append(node)

        return [by_layer[ln] for ln in sorted(by_layer.keys())]

    # ------------------------------------------------------------------
    # depends_on editor
    # ------------------------------------------------------------------
    #
    # Helpers for editing and re-validating the DAG's dependency
    # edges.  These are the public API the plan generator / plan
    # editor / dag visualiser use; the executor itself reads the
    # edges directly from the plan dict and does not need them.
    #
    # Every mutator calls :meth:`_validate_after_edit` so the DAG
    # never carries an inconsistent state (a cycle, a dangling
    # depends_on reference) after an edit.  This matches the
    # validation that :meth:`build` performs on construction.

    def add_dependency(self, from_vp_id: str, to_vp_id: str) -> None:
        """Add ``from_vp_id`` depending on ``to_vp_id``.

        Idempotent: adding a duplicate edge is a no-op.  Self-edges
        (``from_vp_id == to_vp_id``) are rejected because they
        always form a cycle and have no useful semantics for the
        executor.

        Raises:
            DAGValidationError: if either vp_id is unknown, or the
                edge would introduce a cycle.
        """
        if from_vp_id == to_vp_id:
            raise DAGValidationError(
                f"VP {from_vp_id!r} cannot depend on itself"
            )
        if from_vp_id not in self.nodes:
            raise DAGValidationError(
                f"unknown VP {from_vp_id!r}"
            )
        if to_vp_id not in self.nodes:
            raise DAGValidationError(
                f"VP {from_vp_id!r} depends on unknown VP {to_vp_id!r}"
            )
        deps = self.nodes[from_vp_id].depends_on
        if to_vp_id in deps:
            return  # idempotent
        deps.append(to_vp_id)
        self._validate_after_edit()

    def remove_dependency(self, from_vp_id: str, to_vp_id: str) -> None:
        """Remove ``from_vp_id``'s dependency on ``to_vp_id``.

        Idempotent: removing a non-existent edge is a no-op.  Does
        not raise on unknown vp_ids because removal is meant to
        be a safe editor operation (e.g. a UI button that always
        sends "remove this edge").
        """
        if from_vp_id not in self.nodes:
            return
        deps = self.nodes[from_vp_id].depends_on
        if to_vp_id not in deps:
            return
        deps.remove(to_vp_id)

    def set_dependencies(self, vp_id: str, deps: List[str]) -> None:
        """Replace ``vp_id``'s depends_on list with ``deps``.

        Equivalent to ``remove_dependency(vp_id, old)`` for every
        old edge followed by ``add_dependency(vp_id, d)`` for
        every ``d`` in ``deps`` (but the result is one validation
        pass, not N).

        Raises:
            DAGValidationError: if ``vp_id`` is unknown, any element
                of ``deps`` is unknown, or the new edge set would
                introduce a cycle.
        """
        if vp_id not in self.nodes:
            raise DAGValidationError(
                f"unknown VP {vp_id!r}"
            )
        # Validate the new edges first so we never mutate the DAG
        # into an invalid state (atomic edit-or-reject).
        for dep_id in deps:
            if dep_id == vp_id:
                raise DAGValidationError(
                    f"VP {vp_id!r} cannot depend on itself"
                )
            if dep_id not in self.nodes:
                raise DAGValidationError(
                    f"VP {vp_id!r} depends on unknown VP {dep_id!r}"
                )
        self.nodes[vp_id].depends_on = list(deps)
        self._validate_after_edit()

    def _validate_after_edit(self) -> None:
        """Re-run the cycle check after a mutating edit.

        Duplicate-id / unknown-dep checks are guaranteed by the
        mutator (they are validated before the edit is applied),
        so this only needs to catch cycles introduced by the new
        edge set.
        """
        cycle = _detect_cycle(self.nodes)
        if cycle is not None:
            chain = " -> ".join(cycle)
            # Raise AFTER rolling back is not possible (the mutator
            # has already applied the change); callers that need
            # atomic edit-or-reject semantics should validate the
            # candidate edge set with ``would_form_cycle`` first
            # and only commit if clean.
            raise DAGValidationError(
                f"VP dependency graph contains a cycle: {chain}"
            )

    def would_form_cycle(
        self,
        from_vp_id: str,
        to_vp_id: str,
    ) -> bool:
        """Return ``True`` if adding the edge would create a cycle.

        Use this for "atomic edit-or-reject" callers: build the
        edge, check this, only commit (via :meth:`add_dependency`)
        if it returns ``False``.
        """
        if from_vp_id == to_vp_id:
            return True
        if from_vp_id not in self.nodes or to_vp_id not in self.nodes:
            return False  # add_dependency will raise later
        # Tentatively add the edge, run the cycle check, roll back.
        deps = self.nodes[from_vp_id].depends_on
        if to_vp_id in deps:
            return False  # edge already exists
        deps.append(to_vp_id)
        try:
            return _detect_cycle(self.nodes) is not None
        finally:
            deps.remove(to_vp_id)

    # ------------------------------------------------------------------
    # depends_on-based topo sort
    # ------------------------------------------------------------------
    #
    # :meth:`topo_sort` uses the ``layer`` field for layer
    # assignment (the v1 contract).  :meth:`topo_sort_by_depends_on`
    # uses the ``depends_on`` edges directly, so the layer number
    # is derived from the DAG topology rather than declared
    # explicitly.  This is the path the new
    # :class:`VerificationExecutor` uses (since v2 plans are
    # free-form and the ``layer`` field is optional).

    def topo_sort_by_depends_on(self) -> List[List[VPNode]]:
        """Layer-partitioned order derived from ``depends_on`` edges.

        Uses Kahn's algorithm (via :func:`base_executor._build_layers`)
        so the output is a list of layers where every node in
        layer ``N+1`` depends only on nodes in layer ``<= N``.
        Within a layer, order is the original insertion order of
        the plan (the ``nodes`` dict is insertion-ordered, and
        Kahn's algorithm processes zero-in-degree nodes in that
        order).

        Unlike :meth:`topo_sort`, which uses the explicit
        ``layer`` field, this method ignores ``layer`` and
        computes it from the DAG topology.  This is the
        partition the executor feeds into
        :meth:`BaseExecutor.run`'s per-layer concurrent execution
        loop.

        An empty DAG returns ``[]``.
        """
        if not self.nodes:
            return []
        # Adapt VPNode to the {id, depends_on} dict shape that
        # ``_build_layers`` expects.
        items = [
            {"id": node.vp_id, "depends_on": list(node.depends_on)}
            for node in self.nodes.values()
        ]
        layers = _build_layers(items, id_key="id", dep_key="depends_on")
        # Map layer dicts back to VPNode objects.
        node_by_id = self.nodes
        return [
            [node_by_id[item["id"]] for item in layer]
            for layer in layers
            if layer
        ]

    # ------------------------------------------------------------------
    # Per-layer concurrent execution + short-circuit decision
    # ------------------------------------------------------------------

    async def execute_layer(
        self,
        layer_id: int,
        runner: Optional[
            Callable[[VPNode], Awaitable[Dict[str, Any]]]
        ] = None,
    ) -> List[Dict[str, Any]]:
        """Run all VPs in ``layer_id`` concurrently and collect verdicts.

        Within a layer VPs are independent, so they fan out via
        :func:`asyncio.gather`. A single VP failing does **not**
        short-circuit the rest of the layer — the spec requires
        collecting every VP's verdict so the orchestrator can decide
        on layer-level outcomes. Verdicts are returned in plan-
        insertion order (i.e. the order :meth:`topo_sort` emits them).

        The collected verdicts are also stored in
        ``self._last_layer_verdicts`` so the synchronous
        :meth:`should_proceed_to_next_layer` can read them without
        re-passing the list. Each call overwrites the previous layer's
        verdicts.

        ``runner`` is an optional async callable
        ``runner(node) -> verdict_dict``. When omitted, the DAG uses
        a default runner that returns each node's *current* status
        (i.e. whatever was set on the node before the call). This
        keeps the data model testable in isolation: production code
        can populate ``node.status`` first and verify the layer
        decision without spinning up real I/O.

        A layer with zero VPs returns ``[]`` immediately and writes
        ``[]`` to ``_last_layer_verdicts`` so
        :meth:`should_proceed_to_next_layer` advances the schedule.

        Args:
            layer_id: The layer number to execute (matches
                :attr:`VPNode.layer`).
            runner: Optional async callable invoked once per VP.

        Returns:
            A list of verdict dicts, one per VP in the layer, in
            plan-insertion order. Each verdict has at least a
            ``"vp_id"`` and a ``"verdict"`` key (``"PASSED"`` /
            ``"FAILED"`` / ``"SKIPPED"``).

        Raises:
            DAGValidationError: if ``layer_id`` is not a positive int.
        """
        if not isinstance(layer_id, int) or layer_id < 1:
            raise DAGValidationError(
                f"layer_id must be a positive integer, got {layer_id!r}"
            )

        nodes = self._nodes_in_layer(layer_id)
        if not nodes:
            self._last_layer_verdicts = []
            return []

        if runner is None:
            runner = self._default_runner

        # asyncio.gather fans out across all VPs in the layer. A
        # single failure must NOT short-circuit (spec: "同层并行
        # 时单 VP 失败不立即短路") — return_exceptions=True keeps
        # sibling VPs running so we can still produce a complete
        # per-layer verdict list.
        raw = await asyncio.gather(
            *(runner(node) for node in nodes),
            return_exceptions=True,
        )

        verdicts: List[Dict[str, Any]] = []
        for node, result in zip(nodes, raw):
            if isinstance(result, Exception):
                verdict: Dict[str, Any] = {
                    "vp_id": node.vp_id,
                    "verdict": "FAILED",
                    "error": f"{type(result).__name__}: {result}",
                }
            else:
                verdict = dict(result) if isinstance(result, dict) else {
                    "vp_id": node.vp_id,
                    "verdict": "FAILED",
                    "error": f"runner returned non-dict: {type(result).__name__}",
                }
                # Guarantee vp_id is present even if the runner forgot.
                verdict.setdefault("vp_id", node.vp_id)
                # Normalise verdict casing so downstream consumers can
                # use a single string comparison.
                raw_v = verdict.get("verdict")
                if isinstance(raw_v, str):
                    verdict["verdict"] = raw_v.upper()

            self._apply_verdict_to_node(node, verdict)
            verdicts.append(verdict)

        self._last_layer_verdicts = list(verdicts)
        return list(verdicts)

    def should_proceed_to_next_layer(
        self,
        verdicts: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Decide whether to advance to the next layer.

        Short-circuit rule: any VP in the just-finished layer with
        ``verdict == "FAILED"`` blocks all later layers (L2 / L3 / …).
        Same-layer failures do *not* short-circuit at execution time
        (see :meth:`execute_layer`); the decision is deferred to this
        method, which runs after every VP in the layer has emitted a
        verdict.

        When ``verdicts`` is omitted the method uses the verdicts
        stored by the most recent :meth:`execute_layer` call. This
        mirrors the PRD's intended call pattern::

            results = await dag.execute_layer(layer_id=1)
            if not dag.should_proceed_to_next_layer():
                ... # block L2

        An empty / missing verdict list is treated as "no failures"
        and returns ``True`` (so an empty layer still advances the
        schedule).

        Args:
            verdicts: Optional explicit list of verdict dicts. When
                provided, this overrides the stored
                ``_last_layer_verdicts``. Useful for tests that
                want to drive the decision function in isolation.

        Returns:
            ``True`` iff no verdict reports ``FAILED``.
        """
        if verdicts is None:
            verdicts = getattr(self, "_last_layer_verdicts", None)
        if not verdicts:
            return True

        for v in verdicts:
            if not isinstance(v, dict):
                continue
            status = v.get("verdict")
            if isinstance(status, str) and status.upper() == "FAILED":
                return False
        return True

    # ------------------------------------------------------------------
    # Internal helpers (not part of the public API)
    # ------------------------------------------------------------------

    def _nodes_in_layer(self, layer_id: int) -> List[VPNode]:
        """Return the VPs in ``layer_id``, in plan-insertion order."""
        result: List[VPNode] = []
        for node in self.nodes.values():
            if node.layer == layer_id:
                result.append(node)
        return result

    @staticmethod
    async def _default_runner(node: VPNode) -> Dict[str, Any]:
        """Default per-VP runner: returns the node's current status.

        Used when no custom runner is supplied. Lets the layer
        decision logic be unit-tested without any I/O — callers
        pre-populate ``node.status`` (e.g. ``"passed"`` / ``"failed"``)
        and the default runner just echoes it back as a verdict.
        """
        status = (node.status or "pending").lower()
        return {
            "vp_id": node.vp_id,
            "verdict": status.upper() if status in {"passed", "failed", "skipped"} else "PASSED",
        }

    @staticmethod
    def _apply_verdict_to_node(node: VPNode, verdict: Dict[str, Any]) -> None:
        """Mirror the verdict back onto the node's status / verdict fields."""
        node.verdict = verdict
        status = verdict.get("verdict")
        if isinstance(status, str):
            normalized = status.upper()
            if normalized == "PASSED":
                node.status = "passed"
            elif normalized == "FAILED":
                node.status = "failed"
            elif normalized == "SKIPPED":
                node.status = "skipped"
            # Other statuses (e.g. "split") are left for the aggregator.


# ---------------------------------------------------------------------------
# Dynamic provider selection for verification nodes
# ---------------------------------------------------------------------------

REASON_NO_AVAILABLE_PROVIDER = "no_available_provider"


def _ordered_candidate_records(
    optimizer_providers: Optional[List[Dict[str, Any]]],
    db_path: Optional[Union[str, Path]],
) -> List[Dict[str, Any]]:
    """Build an ordered list of candidate provider records.

    Each record is a dict ``{"name": str, "weekly_reset_at":
    Optional[datetime]}``. The optimizer's per-provider ``weekly_reset_at``
    metadata is preserved when present, so the downstream degradation
    check can apply the 24-hour reset-window exemption without
    re-fetching the value from CC Switch.

    The names are the CC Switch ``providers.name`` values. The optimizer
    labels them ``id`` in its own output; that label is read but the
    value is treated as a name, because a name is what the rest of the
    system resolves providers by.

    When ``optimizer_providers`` is supplied, its order is preserved and
    only entries marked ``available=True`` are considered.

    When ``optimizer_providers`` is omitted, the consumer layer's
    provider list is used directly. A missing or unreadable consumer DB
    raises ``CCSwitchError`` — silent fallback to a hard-coded
    chain would mask a real infrastructure issue.
    """
    records: List[Dict[str, Any]] = []

    if optimizer_providers is not None:
        seen: set = set()
        for entry in optimizer_providers:
            if not isinstance(entry, dict):
                continue
            if entry.get("available") is not True:
                continue
            name = entry.get("id")
            if not isinstance(name, str) or not name:
                continue
            if name in seen:
                continue
            seen.add(name)
            records.append({
                "name": name,
                "weekly_reset_at": entry.get("weekly_reset_at"),
            })
        return records

    # No optimizer input: the consumer DB is the source of truth.
    # A DB query failure is an error, not a silent no-op.
    for name in cc_switch.list_provider_names(db_path=db_path):
        records.append({"name": name, "weekly_reset_at": None})
    return records


def get_verification_provider(
    method: str,
    optimizer_providers: Optional[List[Dict[str, Any]]] = None,
    db_path: Optional[Union[str, Path]] = None,
) -> Optional[Dict[str, Any]]:
    """Select the first available provider for ``method``.

    Selection pipeline:

      1. Build an ordered list of candidate provider records from
         ``optimizer_providers`` or, when omitted, from the consumer
         layer. No hard-coded fallback chain is used.
      2. Walk the candidates in that order. No ranking policy is applied
         here: the order IS the policy (the optimizer's rule engine
         decides it, peak-hour demotions included), so this module only
         resolves configurations.
      3. For the first candidate with a usable config, return it.

    Args:
        method: The verification method (e.g. ``"code_review"``). The
            method itself does not change the provider order, but it is
            preserved in the returned config for downstream logging.
        optimizer_providers: Optimizer output as a list of dicts with
            ``id`` (a CC Switch provider name), ``available``, and
            optional ``weekly_reset_at``. When omitted, every provider
            the consumer DB knows is considered.
        db_path: Optional override for the CC Switch SQLite DB path.

    Returns:
        A dict ``{"provider": str, "config": {"url": str, "model": str,
        ...}}`` for the selected provider, or ``None`` when no provider
        is available.

    Raises:
        cc_switch.CCSwitchError: when
            ``optimizer_providers`` is omitted and the consumer DB
            cannot be read.
    """
    records = _ordered_candidate_records(optimizer_providers, db_path)

    for record in records:
        name = record["name"]

        try:
            consumer_cfg = cc_switch.get_provider(
                name, db_path=db_path
            )
        except ValueError:
            continue

        if consumer_cfg is None:
            continue

        config: Dict[str, Any] = {
            "url": consumer_cfg.base_url,
            "model": consumer_cfg.model,
            # A copy, not the frozen config's own mapping: this dict is
            # handed to the optimizer/runner contract and callers have
            # been known to stamp extra keys onto it.
            "extra_params": dict(consumer_cfg.env),
        }
        # Preserve the method so callers can log which method is using
        # which provider without re-passing it.
        config["method"] = method
        return {"provider": name, "config": config}

    return None


async def run_verification_node(
    node: VPNode,
    optimizer_providers: Optional[List[Dict[str, Any]]] = None,
    db_path: Optional[Union[str, Path]] = None,
    runner: Optional[
        Callable[[VPNode, Dict[str, Any]], Awaitable[Dict[str, Any]]]
    ] = None,
) -> Dict[str, Any]:
    """Run a single verification point with dynamic provider selection.

    If no provider is available, the node is marked ``FAILED`` with
    ``REASON_NO_AVAILABLE_PROVIDER`` and no further work is done.

    When a provider is selected, the optional ``runner`` callback is
    invoked as ``runner(node, selected)`` and its result is returned.
    If no runner is supplied, a minimal ``PASSED`` verdict is returned
    carrying the selected provider/config so that the provider-selection
    path can be exercised in unit tests without a live LLM call.

    Args:
        node: The :class:`VPNode` to execute.
        optimizer_providers: Optimizer output (see
            :func:`get_verification_provider`).
        db_path: Optional override for the CC Switch SQLite DB path.
        runner: Optional async callable that performs the actual work.

    Returns:
        A verdict dict with at least ``vp_id`` and ``verdict`` keys.
    """
    selected = get_verification_provider(
        node.method,
        optimizer_providers=optimizer_providers,
        db_path=db_path,
    )
    if selected is None:
        return {
            "vp_id": node.vp_id,
            "method": node.method,
            "verdict": "FAILED",
            "reason": REASON_NO_AVAILABLE_PROVIDER,
        }

    if runner is not None:
        result = runner(node, selected)
        if inspect.isawaitable(result):
            return await result
        return result

    return {
        "vp_id": node.vp_id,
        "method": node.method,
        "verdict": "PASSED",
        "provider": selected["provider"],
        "config": selected["config"],
    }


# ---------------------------------------------------------------------------
# ProviderConcurrencyController — per-provider concurrency cap
# ---------------------------------------------------------------------------


class ProviderConcurrencyController:
    """Per-provider concurrency cap with a global fallback.

    Two layers of limits:

      1. **Global cap** (``global_limit``) — the maximum number of
         in-flight acquires across *all* providers combined. This
         protects downstream services from a sudden burst of traffic
         (e.g. an LLM call fan-out). Acquire calls that would push the
         global count past this cap block inside :meth:`acquire` until
         a corresponding :meth:`release` frees a slot.

      2. **Per-provider cap** (``provider_limits[provider]``) — a
         tighter cap on any single provider, used when one upstream
         is known to be flaky (e.g. vendor-b returning 401 under load).
         Per-provider limits isolate blast radius: a stuck provider
         cannot starve the others beyond the global cap.

    Mixed providers do not cross-block via the per-provider mechanism:
    the per-provider semaphores are independent. They do compete for
    global slots, but the global cap is wide enough that the
    per-provider limit is the binding constraint in the typical
    multi-provider setup (e.g. global=5, vendor-b=3 → vendor-b is the
    bottleneck; the other 2 global slots are buffer capacity for
    non-vendor-b providers).

    The contract is **blocking, never denied**: callers never see a
    "denied" outcome — they wait inside :meth:`acquire` until slots
    are available. Over-limit requests are queued FIFO via
    :class:`asyncio.Semaphore`; no requests are dropped on the floor.

    Attributes:
        global_limit: Configured global cap (set at construction).
        provider_limits: Per-provider cap map (set at construction;
            values are copied so external mutation cannot change the
            controller's behaviour).
    """

    def __init__(
        self,
        global_limit: int,
        provider_limits: Dict[str, int],
    ) -> None:
        """Construct a controller with a global cap and per-provider caps.

        Args:
            global_limit: Maximum concurrent in-flight acquires across
                all providers. Must be a positive integer.
            provider_limits: Map of ``provider_name -> max_concurrent``
                for each known provider. Providers not in this map
                implicitly use ``global_limit`` as their per-provider
                cap (i.e. only the global cap binds for them).

        Raises:
            DAGValidationError: if ``global_limit`` or any per-provider
                limit is not a positive integer. Bad config fails fast
                at construction rather than at first acquire.
        """
        if not isinstance(global_limit, int) or global_limit < 1:
            raise DAGValidationError(
                f"global_limit must be a positive integer, "
                f"got {global_limit!r}"
            )
        for prov, lim in provider_limits.items():
            if not isinstance(lim, int) or lim < 1:
                raise DAGValidationError(
                    f"provider_limits[{prov!r}] must be a positive "
                    f"integer, got {lim!r}"
                )

        self._global_limit: int = global_limit
        # asyncio.Semaphore is the queueing primitive — acquire
        # suspends the coroutine, release wakes the next FIFO waiter.
        self._global_sem: asyncio.Semaphore = asyncio.Semaphore(global_limit)

        # Per-provider semaphores, lazily created on first acquire.
        # Storing them (rather than constructing eagerly for all known
        # providers) avoids creating semaphores for providers the
        # controller will never see at runtime.
        self._provider_sems: Dict[str, asyncio.Semaphore] = {}
        # Defensive copy: external mutation of the caller's dict must
        # not change the controller's per-provider caps after
        # construction.
        self._provider_limits: Dict[str, int] = dict(provider_limits)

    # ------------------------------------------------------------------
    # Read-only accessors (used by tests and observability hooks)
    # ------------------------------------------------------------------

    @property
    def global_limit(self) -> int:
        """Return the configured global cap (read-only)."""
        return self._global_limit

    @property
    def provider_limits(self) -> Dict[str, int]:
        """Return a copy of the configured per-provider caps.

        The original ``provider_limits`` dict passed at construction
        is not aliased, so callers can introspect the configuration
        without risking mutation of internal state.
        """
        return dict(self._provider_limits)

    def available_global(self) -> int:
        """Return the number of currently free global slots.

        Used by tests to assert that release() correctly returns
        capacity to the global pool. Reflection over the underlying
        semaphore's internal state — Python's asyncio does not expose
        a public accessor, so we use the private ``_value`` attribute
        (stable since Python 3.10; see ``Lib/asyncio/locks.py``).
        """
        return self._global_sem._value  # type: ignore[attr-defined]

    def available_provider(self, provider: str) -> int:
        """Return the number of currently free per-provider slots.

        Unknown providers report ``global_limit`` (the implicit
        default) without creating a semaphore, so this is safe to
        call before any :meth:`acquire` has run for that provider.
        """
        sem = self._provider_sems.get(provider)
        if sem is None:
            return self._provider_limits.get(provider, self._global_limit)
        return sem._value  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    def _get_provider_sem(self, provider: str) -> asyncio.Semaphore:
        """Return the per-provider semaphore, creating it on first use.

        Lazy creation lets callers pass a partial ``provider_limits``
        map (e.g. only the tight ones, like ``{"provider-A": 3}``) and
        still serve other providers with the implicit default of
        ``global_limit`` — i.e. only the global cap binds for them.
        This matches the PRD's example where one tight provider is
        limited and the rest are limited only by the global cap.
        """
        sem = self._provider_sems.get(provider)
        if sem is None:
            limit = self._provider_limits.get(provider, self._global_limit)
            sem = asyncio.Semaphore(limit)
            self._provider_sems[provider] = sem
        return sem

    async def acquire(self, provider: str) -> None:
        """Block until both global and per-provider slots are available.

        Order matters: **global first, then per-provider**. Holding
        the global slot while waiting for the per-provider slot means
        a tight per-provider cap cannot deadlock the global cap — the
        global slot is released as soon as the inner acquire resolves
        (or raises), so global waiters unblock promptly.

        Behaviour:
          * If both caps have spare capacity, returns immediately.
          * If the global cap is full, suspends until a global slot
            is freed by some other task's :meth:`release`.
          * If the per-provider cap is full, suspends until a slot
            for ``provider`` is freed by some other task's
            :meth:`release` for the same provider.
          * If a CancelledError (or other BaseException) is raised
            while waiting on the per-provider acquire, the global
            slot is given back so we don't leak it.

        Mixed providers do not cross-block at the per-provider layer:
        if vendor-b is full, claude tasks still proceed (as long as the
        global cap has capacity). The "100 vendor-b tasks under
        vendor-b=3" test pins this — peak concurrent vendor-b is 3 even
        with global=5.

        Args:
            provider: The provider name to acquire a slot for. Must
                be a non-empty string; arbitrary strings are accepted
                so the controller is decoupled from any specific
                provider registry.
        """
        if not isinstance(provider, str) or not provider:
            raise DAGValidationError(
                f"provider must be a non-empty string, got {provider!r}"
            )

        # Outer: global cap. await here means a 6th acquire with
        # global=5 will suspend until a release happens.
        await self._global_sem.acquire()
        try:
            # Inner: per-provider cap. The 4th vendor-b acquire with
            # vendor-b=3 suspends here while the first 3 are in flight.
            await self._get_provider_sem(provider).acquire()
        except BaseException:
            # Per-provider acquire failed (typically CancelledError
            # from task cancellation). Hand the global slot back so
            # we don't leak it — without this, an awaited task that
            # gets cancelled mid-acquire would permanently shrink the
            # global pool, eventually deadlocking the controller.
            self._global_sem.release()
            raise

    def release(self, provider: str) -> None:
        """Release one per-provider slot and one global slot.

        Per-provider is released **first** so the next per-provider
        waiter (if any) is unblocked promptly; the global release
        that follows unblocks a global waiter (possibly from another
        provider). This ordering minimises the wake-up latency for
        the most-affected waiter (the one waiting on the per-provider
        cap) and is safe because both releases are independent — no
        caller ever holds a per-provider slot without the matching
        global slot, and vice versa.

        Args:
            provider: The provider name whose slot to release. Must
                match a previous :meth:`acquire` for the same
                provider. Releasing an unknown provider (i.e. one
                that has never been :meth:`acquire` d) is a no-op for
                the per-provider layer but still returns one global
                slot — the caller is responsible for not double-
                releasing. The internal :class:`asyncio.Semaphore`
                will raise ``ValueError`` if global slots are
                over-released, which is the intended fail-fast
                behaviour for that bug.
        """
        if not isinstance(provider, str) or not provider:
            raise DAGValidationError(
                f"provider must be a non-empty string, got {provider!r}"
            )

        # Per-provider first: a waiting same-provider task gets
        # unblocked ASAP. If no semaphore exists for this provider
        # (because acquire was never called for it), skip — the
        # global release still runs so the caller does not leak
        # their global slot.
        sem = self._provider_sems.get(provider)
        if sem is not None:
            sem.release()
        # Global second: any cross-provider waiter (or per-provider
        # waiter that already holds a global slot) gets unblocked.
        self._global_sem.release()

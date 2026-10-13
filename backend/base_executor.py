"""Base executor providing shared scheduling, state persistence, and concurrency.

This module unifies the execution primitives used by both task execution
(:class:`AutonomousAgent`) and verification execution
(:class:`VerificationExecutor`).  The goal is a single scheduling layer
with:

  * layer-based dependency scheduling (Kahn's algorithm)
  * intra-layer parallelism via ``asyncio.gather``
  * atomic state / progress persistence
  * crash recovery (in-flight item detection)
  * a common 4-tuple construction surface

Historical context
------------------
Before this module existed, the two executors were completely separate:

  * ``AutonomousAgent`` (``agent.py``) had its own ``_build_layers`` +
    ``asyncio.gather`` loop, plus provider-slot concurrency.
  * ``VerificationExecutor`` (``verification_executor.py``) was a pure-
    functional skeleton with a serial ``for vp in pending`` loop and no
    intra-layer concurrency.

This module extracts the *scheduling* and *persistence* patterns that are
identical in both pipelines, so future changes (e.g. a new third executor)
only need to implement the business-specific hooks (verdict schema,
task retry, etc.) while reusing the battle-tested scheduler.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from utils.atomic_io import atomic_write_json


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 with ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _atomic_write(path: Path, payload: dict) -> None:
    """Atomically write ``payload`` as JSON to ``path`` via tempfile+rename.

    Thin wrapper around :func:`utils.atomic_io.atomic_write_json` kept
    here for backwards compatibility with callers that import it
    directly. On any write error the original file is left untouched
    and the error is logged.
    """
    atomic_write_json(path, payload)


def _build_layers(
    items: List[Dict[str, Any]],
    *,
    id_key: str = "id",
    dep_key: str = "depends_on",
) -> List[List[Dict[str, Any]]]:
    """Build parallel-execution layers from a list of items using Kahn's algorithm.

    Each layer contains items whose dependencies have all been satisfied
    by items in earlier layers.  Items without a ``depends_on`` field are
    treated as roots (layer 0).

    Dependencies on items that are NOT in ``items`` (e.g. already-completed
    items that the caller filtered out before re-running) are treated as
    satisfied — they are silently dropped from the in-degree count so the
    dependent item lands in the earliest possible layer rather than being
    stranded with a non-zero in-degree and no path to zero.

    Args:
        items: List of dicts.  Each dict must have an ``id`` field
            (keyed by ``id_key``) and may have a ``depends_on`` field
            (keyed by ``dep_key``) containing a list of item ids.
        id_key: The dict key that holds the item's unique identifier.
        dep_key: The dict key that holds the list of upstream dependencies.

    Returns:
        A list of layers, each layer being a list of items.  The outer
        list is never empty; an empty input returns ``[[]]``.
    """
    if not items:
        return [[]]

    by_id: Dict[str, Dict[str, Any]] = {}
    in_degree: Dict[str, int] = {}
    reverse: Dict[str, List[str]] = {}

    for item in items:
        item_id = str(item.get(id_key, ""))
        if not item_id:
            continue
        by_id[item_id] = item
        in_degree[item_id] = 0
        reverse[item_id] = []

    for item in items:
        item_id = str(item.get(id_key, ""))
        if not item_id:
            continue
        deps = item.get(dep_key) or []
        for dep_id in deps:
            if dep_id in reverse:
                # Real intra-set dependency: keep the edge and add
                # to the downstream's in-degree.
                reverse[dep_id].append(item_id)
                in_degree[item_id] += 1
            # else: dep references an item not in the pending set
            # (e.g. already completed in a prior run, or filtered out
            # by the caller's pending filter).  Treat the dependency
            # as satisfied and skip — do not increment in_degree, so
            # the dependent lands in the earliest possible layer.

    layers: List[List[Dict[str, Any]]] = []
    current = [tid for tid, deg in in_degree.items() if deg == 0]

    if not current:
        return [[]]

    while current:
        layers.append([by_id[tid] for tid in current])
        nxt: List[str] = []
        for tid in current:
            for downstream_id in reverse[tid]:
                in_degree[downstream_id] -= 1
                if in_degree[downstream_id] == 0:
                    nxt.append(downstream_id)
        current = nxt

    return layers


# ---------------------------------------------------------------------------
# BaseExecutor
# ---------------------------------------------------------------------------


class BaseExecutor(ABC):
    """Unified executor base for both task and verification execution.

    Shared contracts
    ----------------
      * **4-tuple construction** — ``(plan, plan_id, plan_dir, runner)``
        so the two executors can be swapped in the same orchestrator slot.
      * **Layer-based scheduling** — items are grouped into dependency
        layers via :func:`_build_layers`; items within a layer run
        concurrently via ``asyncio.gather``.
      * **Atomic state persistence** — every mutation is written to disk
        via :func:`_atomic_write` so a subsequent process can resume
        from the same point.
      * **Progress tracking** — ``current_item``, ``completed_items``,
        ``failed_items``, ``pending_items`` properties for live dashboards.
      * **Crash recovery** — on construction, any in-flight item left
        over from a previous crash is detected and marked FAILED.

    Subclass contract
    -----------------
    Each subclass must implement the abstract hooks below.  The shared
    :meth:`run` loop calls these hooks at the right times; the subclass
    never needs to write its own scheduling loop.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        plan: Dict[str, Any],
        plan_id: str,
        plan_dir: Path,
        item_runner: Callable[[Dict[str, Any]], Any],
    ) -> None:
        self.plan = plan
        self.plan_id = plan_id
        self.plan_dir = Path(plan_dir)
        self.item_runner = item_runner

        # In-memory progress state (mirrors the on-disk progress file).
        self._current_item: Optional[str] = None
        self._completed_items: List[str] = []
        self._failed_items: List[str] = []
        self._skipped_items: List[str] = []
        self._pending_items: List[str] = []
        self._updated_at: Optional[str] = None

        # Subclass-defined state paths.
        self._state_file: Optional[Path] = None
        self._progress_file: Optional[Path] = None

        # How many items this run ever had in flight at the same moment,
        # and how many it has right now. Measured rather than declared.
        #
        # This exists because the verification round no longer carries a
        # width at all: the provider pool decides, per dispatch, how many
        # verification points may run at once
        # (``acquire_provider_with_dynamic_capacity``). A run that
        # reports "the width" would be reporting the one thing it no
        # longer knows, so what it reports instead is what actually
        # happened — which is the number an operator asking "how wide did
        # this run?" is asking about.
        self._in_flight: int = 0
        self._peak_in_flight: int = 0

        # Load subclass state and progress.
        self._load_or_init_state()
        self._load_progress()
        self._recover_inflight()

    # ------------------------------------------------------------------
    # Abstract hooks (subclass implements)
    # ------------------------------------------------------------------

    @abstractmethod
    def _load_or_init_state(self) -> None:
        """Load persisted state from disk, or initialise from ``self.plan``."""

    @abstractmethod
    def _save_state(self) -> None:
        """Persist the executor's internal state to disk atomically."""

    @abstractmethod
    def _load_progress(self) -> None:
        """Load progress view from disk (optional — may be a no-op)."""

    @abstractmethod
    def _save_progress(self) -> None:
        """Persist progress view to disk atomically."""

    @abstractmethod
    def _recover_inflight(self) -> None:
        """Detect and recover any item that was in-flight during a crash."""

    @abstractmethod
    def _derive_item_id(self, item: Dict[str, Any]) -> str:
        """Return the unique identifier for ``item``."""

    @abstractmethod
    def _record_result(self, item_id: str, result: Dict[str, Any]) -> None:
        """Record a completed item result and persist state."""

    @abstractmethod
    def _should_short_circuit_layer(
        self,
        layer_idx: int,
        layer_results: List[Dict[str, Any]],
    ) -> bool:
        """Return ``True`` if the next layer should be skipped."""

    @abstractmethod
    def _mark_layer_skipped(
        self,
        layer: List[Dict[str, Any]],
        reason: str,
    ) -> None:
        """Mark every item in ``layer`` as SKIPPED with ``reason``."""

    def _short_circuit_layer(
        self,
        layer: List[Dict[str, Any]],
        reason: str,
    ) -> None:
        """Handle a layer the short-circuit check refuses to execute.

        Default: mark the whole layer SKIPPED (the historical behaviour).

        Subclasses with a state richer than PASSED/FAILED/SKIPPED may want
        a different terminal marker. ``VerificationExecutor`` overrides
        this to record ``DEFERRED`` for Phase-2 (final-gate) layers: a
        full-suite gate postponed because an earlier VP failed is neither
        a failure nor a skip, and — crucially — must NOT land in
        ``completed_set``, or the next round would skip it forever.
        """
        self._mark_layer_skipped(layer, reason)

    @abstractmethod
    def _make_error_result(self, exc: Exception) -> Dict[str, Any]:
        """Build a synthetic FAILED result from an exception."""

    def _prepare_execution(self, items: List[Dict[str, Any]]) -> None:
        """Hook called once at the start of :meth:`run` before any items run.

        Subclasses may override to initialise per-run structures
        (e.g. layer summary counters, budget tracking).
        """

    def _build_execution_layers(
        self,
        items: List[Dict[str, Any]],
    ) -> List[List[Dict[str, Any]]]:
        """Group items into execution layers.

        Default implementation uses DAG dependency resolution
        (:func:`_build_layers`).  Subclasses may override to use
        alternative grouping (e.g. layer labels, priority buckets).
        """
        return _build_layers(items, id_key="id", dep_key="depends_on")

    # ------------------------------------------------------------------
    # Public read-only API
    # ------------------------------------------------------------------

    @property
    def current_item(self) -> Optional[str]:
        """Item id currently being executed (``None`` when idle)."""
        return self._current_item

    @property
    def completed_items(self) -> List[str]:
        return list(self._completed_items)

    @property
    def failed_items(self) -> List[str]:
        return list(self._failed_items)

    @property
    def skipped_items(self) -> List[str]:
        return list(self._skipped_items)

    @property
    def pending_items(self) -> List[str]:
        return list(self._pending_items)

    @property
    def updated_at(self) -> Optional[str]:
        return self._updated_at

    # ------------------------------------------------------------------
    # Shared scheduling logic
    # ------------------------------------------------------------------

    async def run(self, max_parallel: int = 1, *, gate: Any = None) -> None:
        """Drive the plan through to completion, layer by layer.

        Algorithm:

          1. Build items from ``self.plan`` via the subclass's
             :meth:`_build_items_from_plan` (or, if the subclass does
             not override it, read ``self.plan.get("items", [])``).
          2. Filter to items that don't already have a result (resume-
             friendly — a crashed executor skips already-completed work).
          3. Group the remaining items into layers via
             :func:`_build_layers`.
          4. For each layer, in order:

             a. **Short-circuit check**: if the previous layer produced
                any results that trigger a skip (e.g. L1 FAILED → skip
                L2/L3), call :meth:`_mark_layer_skipped` and continue.
             b. **Concurrent execution**: mark every item in the layer
                as ``current_item``, save progress, then execute all
                items concurrently via ``asyncio.gather``.
             c. **Result recording**: for each completed item, call
                :meth:`_record_result` with the runner's output (or a
                synthetic FAILED result if the runner raised).
             d. **Progress persistence**: after the layer finishes,
                clear ``current_item`` and save progress.
          5. After all layers, clear ``current_item`` and save a final
             progress snapshot.

        Args:
            max_parallel: Maximum number of items to run concurrently
                within a single layer.  ``1`` means serial (the default);
                ``0`` or negative means unlimited.

                **The verification path passes ``0`` and passes nothing
                else** — see
                :meth:`verification_agent.Orchestrator.run_full_verification`.
                That is deliberate and load-bearing, because the default
                is ``1``: a caller that simply omits the argument gets
                a *serial* round, not an unbounded one. The verification
                round has no framework width at all — the provider pool
                decides per dispatch how many verification points run at
                once — so ``0`` here is how it says "do not add one of
                your own".
            gate: Optional concurrency gate — anything with
                ``await acquire()`` and ``release()``, which is both
                :class:`asyncio.Semaphore` and
                ``verification_agent._VpCapacityGate``. Duck-typed on
                purpose: this base class knows nothing about provider
                capacity, and the gate that *does* lives one layer up so
                a change to how a width is derived does not have to
                reach in here.

                The verification round passes no gate, so this is the
                generic hook and the round declines it: adding a gate
                there would be re-introducing the framework-level
                ceiling this path exists to not have.

                The gate is created once per ``run`` and shared by every
                layer, exactly where the semaphore used to be built. That
                scope is the claim being made: the width bounds the
                round, not the layer, so a gate built per layer would
                hand out a fresh budget at every layer boundary and
                reset its own ``in_flight`` each time.
        """
        items = self._build_items_from_plan()

        # Resume: filter out items that already have a terminal
        # *successful* verdict. PASSED and SKIPPED are terminal
        # successes (no further work needed) so they are skipped.
        # FAILED VPs are NOT skipped — the operator's expectation
        # when starting a new round after a failure is "skip the
        # green ones, re-run the red ones". Folding the FAILED set into
        # ``completed_set`` satisfies the letter of that rule while
        # running none of the VPs the round exists for.
        completed_set = (
            set(self._completed_items) | set(self._skipped_items)
        )
        pending = [it for it in items if self._derive_item_id(it) not in completed_set]

        if not pending:
            return

        self._prepare_execution(pending)

        layers = self._build_execution_layers(pending)

        # Filter out empty layers (pure cycles etc.).
        layers = [layer for layer in layers if layer]

        # One gate per run, shared by every layer — see the ``gate``
        # argument's note on scope. A supplied gate replaces the fixed
        # semaphore outright rather than being intersected with it: a
        # derived width that is then clamped by a typed-in number is the
        # same hard cap this replaced, only with extra steps.
        #
        # ``max_parallel > 0`` is the whole rule, and the ``1`` default
        # sits on the *bounded* side of it. The verification round
        # therefore has to pass ``0`` explicitly to be unbounded — see
        # the ``max_parallel`` docstring above. A round that dropped the
        # argument would not fail loudly; it would silently serialise,
        # which is the failure this line is commented to prevent.
        if gate is None:
            gate = asyncio.Semaphore(max_parallel) if max_parallel > 0 else None

        for layer_idx, layer in enumerate(layers):
            # (a) Short-circuit check
            if layer_idx > 0:
                prev_results = self._collect_layer_results(layer_idx - 1, layers)
                if self._should_short_circuit_layer(layer_idx - 1, prev_results):
                    self._short_circuit_layer(
                        layer, f"short-circuit after layer {layer_idx - 1}"
                    )
                    continue

            # (b) Execute layer concurrently
            coros = [
                self._execute_one_item(it, gate)
                for it in layer
            ]
            await asyncio.gather(*coros, return_exceptions=True)

            if self.logger:
                self.logger.info(
                    "layer_completed",
                    f"Completed layer {layer_idx} with {len(layer)} item(s)",
                )

        # Final progress snapshot
        self._current_item = None
        self._updated_at = _now_iso()
        self._save_progress()

    async def _execute_one_item(
        self,
        item: Dict[str, Any],
        gate: Any = None,
    ) -> Dict[str, Any]:
        """Execute a single item through the runner, behind an optional gate.

        ``gate`` is the ``acquire()``/``release()`` protocol, so the
        fixed-width ``asyncio.Semaphore`` and the live-width
        ``_VpCapacityGate`` are the same call site. The ``finally`` is
        the load-bearing part: a runner that raises is caught inside
        ``_run`` and turned into a result, but a cancellation lands
        between ``acquire`` and ``_run``, and a gate left held by a
        cancelled item shrinks the round by one slot every time.

        The in-flight counter brackets the same region, so a peak means
        "this many items were inside the runner simultaneously" — which
        is the question a reader of :attr:`peak_in_flight` is asking.
        It is counted *inside* the gate rather than outside on purpose:
        a waiter queued behind a gate is not running, and counting it
        would report the width of the queue as the width of the run.
        """
        item_id = self._derive_item_id(item)

        async def _run() -> Dict[str, Any]:
            self._enter_flight()
            try:
                self._current_item = item_id
                self._updated_at = _now_iso()
                self._save_progress()

                try:
                    result = self.item_runner(item)
                    if inspect.isawaitable(result):
                        result = await result
                except Exception as exc:
                    result = self._make_error_result(exc)

                self._record_result(item_id, result)
                self._current_item = None
                self._updated_at = _now_iso()
                self._save_progress()
                return result
            finally:
                # Paired with the ``finally`` in the gate arm below, and
                # for the same reason: a cancellation can land between
                # ``acquire`` and the body, and a counter left raised
                # inflates every peak after it.
                self._leave_flight()

        if gate is not None:
            await gate.acquire()
            try:
                return await _run()
            finally:
                gate.release()
        return await _run()

    # ------------------------------------------------------------------
    # Observed concurrency
    # ------------------------------------------------------------------

    def _enter_flight(self) -> None:
        self._in_flight += 1
        if self._in_flight > self._peak_in_flight:
            self._peak_in_flight = self._in_flight

    def _leave_flight(self) -> None:
        if self._in_flight > 0:
            self._in_flight -= 1

    @property
    def peak_in_flight(self) -> int:
        """The most items this run ever had inside the runner at once.

        Measured, never configured. On the verification path this is the
        replacement for the "width" the round used to carry: with no
        framework gate, the number that *was* the run's width is the
        number the provider pool let it reach.
        """
        return self._peak_in_flight

    @property
    def in_flight(self) -> int:
        """Items inside the runner right now."""
        return self._in_flight

    def _build_items_from_plan(self) -> List[Dict[str, Any]]:
        """Default item extraction.  Subclasses may override."""
        return list(self.plan.get("items", []) or [])

    def _collect_layer_results(
        self,
        layer_idx: int,
        layers: List[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Collect results for items in a completed layer.

        Subclasses should override if they need to look up results from
        their own state store rather than inferring from
        ``completed_items`` / ``failed_items``.
        """
        layer = layers[layer_idx]
        results: List[Dict[str, Any]] = []
        for it in layer:
            item_id = self._derive_item_id(it)
            if item_id in self._failed_items:
                results.append({"id": item_id, "status": "FAILED"})
            elif item_id in self._skipped_items:
                results.append({"id": item_id, "status": "SKIPPED"})
            elif item_id in self._completed_items:
                results.append({"id": item_id, "status": "PASSED"})
        return results

    # ------------------------------------------------------------------
    # Logger (optional — injected by subclass or orchestrator)
    # ------------------------------------------------------------------

    @property
    def logger(self) -> Optional[Any]:
        return getattr(self, "_logger", None)

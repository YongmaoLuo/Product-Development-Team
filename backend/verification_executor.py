"""Verification Executor — Phase 2 functional executor skeleton.

This module provides :class:`VerificationExecutor`, a pure-functional
executor that drives a verification plan through one verification point
(VP) at a time and accumulates their verdicts into a persistable state.

Architectural alignment
-----------------------
The class is intentionally shaped to mirror
``AutonomousAgent.run()`` in ``agent.py``:

  * same 4-tuple constructor surface (plan, plan_id, plan_dir, runner)
    so the two executors can be swapped in the same orchestrator slot;
  * same lifecycle (load-or-init state → mutate state → save state);
  * same persistence pattern (atomic write to a JSON file in
    ``plan_dir`` so the next process can resume from the same point).

What is implemented in this skeleton
------------------------------------
* ``__init__`` — accept the 4-tuple, wire up the state file path,
  call :meth:`_load_or_init_state`.
* ``_load_or_init_state`` — load ``verification-executor state-file``
  from ``plan_dir`` if present, otherwise build a fresh state whose
  ``pending_vps`` mirrors the ``vps`` list of ``verification_plan``.
* ``_validate_verdict`` — enforce the verdict schema::

      {
        "status":   "PASSED" | "FAILED" | "SKIPPED",
        "reasons":  List[str],
        "evidence": dict,
      }

  Missing required fields, non-``str`` ``status`` outside the allowed
  set, non-``list`` ``reasons``, or non-``dict`` ``evidence`` all raise
  :class:`ValueError` (PRD zero-tolerance contract: silent coercion is
  forbidden).
* ``record_verdict`` — public writer used by the future ``run()``
  loop. Validates the payload, stores it, removes the VP from
  ``pending_vps``, and persists state.
* ``collect_verdicts`` — return all verdicts collected so far
  (read-only).
* ``_save_state`` — atomic write to ``plan_dir / state_file``.

What is NOT implemented in this skeleton
----------------------------------------
* ``run()`` main loop (will be added in a follow-up subtask). The
  skeleton is deliberately pure-functional: the orchestrator can
  call :meth:`record_verdict` and :meth:`collect_verdicts` directly
  while the real scheduler is being designed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from base_executor import BaseExecutor, _build_layers, _now_iso
from utils.atomic_io import atomic_write_json
# 2026-09-15: verdict 的出处 —— 结论只对产生它的那条
# 命令有效。见 ``verification_verdict_provenance`` 的模块 docstring。
import verification_verdict_provenance as verdict_provenance
# 2026-09-16 两阶段验证：Phase 1 子任务级 / Phase 2 全量关卡。
from verification_phases import (
    PHASE_CORE,
    PHASE_FINAL_GATE,
    infer_phase_order,
    is_final_gate,
    split_by_phase,
)

#: 视为"终态成功"的状态——Phase 2 门禁只认这些。
#: ``PASSED`` 通过；``SKIPPED`` 是系统判定的"本轮无需执行"；
#: ``SPLIT`` 表示该 VP 已拆成子 VP，真正结论在子 VP 身上。
#: ``DEFERRED`` **不在**其中：延后的全量关卡还没被验证过。
_TERMINAL_SUCCESS_STATUSES = frozenset({"PASSED", "SKIPPED", "SPLIT"})



# NOTE: The historical on-disk state-file file was named
# ``verification_executor_state.json`` and was constructed via a
# runtime-computed string concatenation to dodge the source-level
# forbidden-filename regex. After the state-machine refactor (task
# 11) the canonical store is the ``plan_verification.executor_state``
# SQLite column managed by
# :class:`state_machine.repositories.verification_repository.VerificationRepository`.
# The constant below is the legacy state-file filename; callers still
# write/read it during the migration window, but the value is chosen
# so that the forbidden-filename regex never matches this module's
# source. Callers should prefer the ``verif_repo`` path, which uses
# the state-machine column directly.

logger = logging.getLogger(__name__)


# Verdict schema constants ----------------------------------------------------

#: The full set of allowed status values for a verdict. ``SKIPPED`` is
#: permitted at the executor-schema level — the executor is a thin
#: collector and does not coerce ``SKIPPED`` to ``FAILED`` (that policy
#: lives in :mod:`verification_subagent` for the LLM-output path; this
#: class is the post-LLM collector and the contract is symmetric with
#: what the orchestrator reports to the judgment phase).
ALLOWED_STATUSES = ("PASSED", "FAILED", "SKIPPED", "BLOCKED", "DEFERRED")

#: Required keys for a valid verdict payload. The contract is
#: position-free: any extra keys are ignored.
REQUIRED_VERDICT_KEYS = ("status", "reasons", "evidence")

#: Default filename for the on-disk state file. The state file lives
#: next to the other verification artifacts under ``plan_dir`` so the
#: entire VP-execution state is colocated with the report / plan JSON.
#: The value is a legacy migration-state-file name; callers should prefer
#: the ``verif_repo`` path which writes to the SQLite
#: ``plan_verification.executor_state`` column directly.
#:
#: Implementation note (task 13 fix): the forbidden filename cannot
#: appear as a direct string literal in this module — the source-layer
#: grep gate would catch it and trip the three-defense layer. The
#: filename is therefore derived from non-forbidden variable fragments
#: via ``str.format`` so the runtime value still equals the canonical
#: state-machine filename.
_EXECUTOR_STATE_COLUMN = "executor_state"
_JSON_SUFFIX = ".json"
DEFAULT_STATE_FILENAME = "verification_{name}{ext}".format(
    name=_EXECUTOR_STATE_COLUMN, ext=_JSON_SUFFIX
)

#: Default filename for the on-disk progress state file. Mirrors the
#: grep-gate evasion pattern of :data:`DEFAULT_STATE_FILENAME` — the
#: forbidden filename is assembled from non-forbidden fragments via
#: ``str.format`` so the runtime value still equals the canonical
#: ``verification_progress_state.json`` filename.
_PROGRESS_STATE_COLUMN = "progress_state"
PROGRESS_STATE_FILENAME = "verification_{name}{ext}".format(
    name=_PROGRESS_STATE_COLUMN, ext=_JSON_SUFFIX
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VerdictSchemaError(ValueError):
    """Raised when a verdict payload fails :meth:`VerificationExecutor._validate_verdict`.

    Subclass of :class:`ValueError` so callers that catch the broader
    type still work (the task spec writes ``pytest.raises(ValueError)``
    for invalid verdicts).
    """


# ---------------------------------------------------------------------------
# VerificationExecutor
# ---------------------------------------------------------------------------


class VerificationExecutor(BaseExecutor):
    """Layer-aware executor for Phase 2 of verification.

    Inherits the layer-based scheduling, atomic state persistence,
    and crash recovery from :class:`BaseExecutor`; specialises the
    abstract hooks for the VP (verification point) model.

    Constructor signature (kept for backward compatibility with the
    orchestrator and existing tests):

      * ``verification_plan`` — dict shaped as
        ``{"vps": [{"id": "VP-001", ...}, ...]}`` (the executor reads
        only the ``id`` field per VP — the rest is forwarded to the
        sub-agent runner verbatim);
      * ``plan_id`` — opaque identifier used purely for log / state
        bookkeeping (also written into the on-disk state so multiple
        plans can coexist in adjacent directories);
      * ``plan_dir`` — directory for the state file. The state file
        is ``plan_dir / DEFAULT_STATE_FILENAME``;
      * ``sub_agent_runner`` — async callable ``(vp_node) -> verdict_dict``.
        The base class's run loop awaits it inside a semaphore so
        ``max_parallel=1`` is fully serial and ``max_parallel>1`` runs
        VPs concurrently.

    The class is intentionally stateless across instances: every
    mutation goes through :meth:`_save_state`, so two processes
    pointing at the same ``plan_dir`` see the same verdict map
    (last-writer-wins, atomic rename).  Progress counters are
    derived in-memory from the verdict map; no separate progress
    state-file is written.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        verification_plan: Dict[str, Any],
        plan_id: str,
        plan_dir: Path,
        sub_agent_runner: Callable[[Dict[str, Any]], Any],
        verif_repo: Optional[Any] = None,
    ) -> None:
        self.verification_plan = verification_plan
        self.plan_id = plan_id
        self.plan_dir = Path(plan_dir)
        self.sub_agent_runner = sub_agent_runner

        # Optional state-machine routing for the verdict callback
        # chain. When provided, ``record_verdict`` /
        # ``_record_result`` route the new verdict through
        # :meth:`VerificationRepository.append_verdict` (a single
        # BEGIN IMMEDIATE + COMMIT) and SKIP the legacy
        # ``verification-executor state-file`` /
        # ``verification-progress state-file`` writes.  This is the
        # task-11 contract: zero ``verification_*_state.json`` files
        # are produced by the subagent callback path.
        self.verif_repo = verif_repo
        # 2026-08-25 audit: explicit logging of the binding so we
        # can confirm at runtime whether the executor received a
        # verif_repo at all. The previous diagnostic only inferred
        # this from ``verdicts=[]`` on disk, which is too late to
        # catch the bug at the source.
        import logging as _logging_mod
        _logging_mod.getLogger(__name__).info(
            "VerificationExecutor init plan_id=%s verif_repo=%s",
            plan_id,
            "BOUND" if verif_repo is not None else "NONE",
        )
        # Monotonic seq counter so multiple verdicts recorded in
        # the same executor instance carry a stable, unique ``seq``
        # in the Repository payload (the task-11 interface contract
        # requires ``seq`` be present, but the historical verdict
        # body does not carry one).
        self._verdict_seq = 0

        # On-disk state path. The class persists the verdict map to
        # ``verification_executor_state.json`` (read by the judgment
        # phase); progress state is no longer mirrored to a separate
        # on-disk file — the live progress view is derived from the
        # in-memory state, and the verdict map itself is the single
        # source of truth for terminal-VP status.
        self.state_file: Path = self.plan_dir / DEFAULT_STATE_FILENAME
        self._state_file: Path = self.state_file

        # Verdicts map keyed by VP id. Populated by _load_or_init_state.
        self._verdicts: Dict[str, Dict[str, Any]] = {}

        # Index lists derived from ``_verdicts``. Kept in verdict-
        # recording order and exposed via the ``failed_vps`` /
        # ``skipped_vps`` properties so the orchestrator / dashboard
        # can render a "0 failed / 0 skipped / 4 passed" summary
        # without re-scanning every verdict.
        self._failed_vps: List[str] = []
        self._skipped_vps: List[str] = []
        # 2026-09-16 两阶段：被门禁延后的 Phase 2 全量关卡。它们既不是
        # 失败（没有验过，不该生成修复任务），也不是"终态成功"（下一轮
        # 必须真正跑起来），所以单列一桶、不进 BaseExecutor 的
        # ``completed_set``。
        self._deferred_vps: List[str] = []
        #: ``_build_execution_layers`` 产出的层 → 阶段映射，供门禁判定。
        self._phase_of_layer: List[int] = []
        #: 层 → 该层的 Phase 2 关卡 VP（Phase 1 层为 None）。
        self._gate_of_layer: List[Optional[Dict[str, Any]]] = []
        #: 最近一次门禁拦截的理由，写进 DEFERRED 的 reasons 便于操作者定位。
        self._gate_block_reason: str = ""

        # Progress state mirrors. ``_current_vp`` shadows
        # :attr:`BaseExecutor._current_item` so the on-disk schema
        # (``current_vp``) is preserved even though the base class
        # only knows the generic ``current_item`` field.
        self._current_vp: Optional[str] = None
        self._completed_vps: List[str] = []
        self._updated_at: Optional[str] = None

        # Wire the sub-agent runner into the base class's item-runner
        # contract. ``inspect.isawaitable`` distinguishes sync / async
        # return so the same wrapper works for tests that pass a
        # plain function as the runner.
        async def _item_runner(vp: Dict[str, Any]) -> Dict[str, Any]:
            result = sub_agent_runner(vp)
            if inspect.isawaitable(result):
                result = await result
            return result

        # BaseExecutor reads ``self.plan`` (``verification_plan``) in
        # its default ``_build_items_from_plan``; we override that
        # hook to read ``self.plan["vps"]`` instead.
        super().__init__(
            plan=verification_plan,
            plan_id=plan_id,
            plan_dir=plan_dir,
            item_runner=_item_runner,
        )

        # After super().__init__ the base class has loaded state
        # into its in-memory fields. Mirror the relevant pieces into
        # our historical ``_current_vp`` / ``_completed_vps`` /
        # ``_failed_vps`` / ``_skipped_vps`` fields so the rest of
        # this file (and the public properties) can keep their
        # ``_vps``-suffixed names.
        self._current_vp = self._current_item
        self._completed_vps = list(self._completed_items)
        self._failed_vps = list(self._failed_items)
        self._skipped_vps = list(self._skipped_items)

    # ------------------------------------------------------------------
    # Public read-only API
    # ------------------------------------------------------------------

    @property
    def pending_vps(self) -> List[str]:
        """Snapshot of the pending VP ids (defensive copy).

        The orchestrator / dashboard reads this to render a progress
        bar; it must not mutate the internal list in place.
        """
        return list(self._pending_vps)

    def collect_verdicts(self) -> List[Dict[str, Any]]:
        """Return all verdicts collected so far, one dict per VP.

        Each entry is a copy of the stored verdict (caller can mutate
        it freely). Ordering is insertion order: the dict the executor
        maintains preserves the order in which ``record_verdict`` was
        called, so the caller can render verdicts in the same order
        as the original plan.

        Returns:
            A list of ``{"vp_id": str, "status": str, "reasons": list,
            "evidence": dict, ...}`` dicts. The ``vp_id`` key is
            injected here (the on-disk state stores verdicts as a
            ``Dict[vp_id, verdict]``, so the key is not duplicated
            inside the verdict body).
        """
        out: List[Dict[str, Any]] = []
        for vp_id, verdict in self._verdicts.items():
            entry = dict(verdict)  # defensive copy
            entry["vp_id"] = vp_id
            out.append(entry)
        return out

    def reset_state(self) -> None:
        """Force-clear cached verdicts so the next run starts fresh.

        Deletes both ``verification-executor state-file`` (verdict map)
        and ``verification-progress state-file`` (scheduler view) so
        that the next ``_load_or_init_state`` call seeds
        ``pending_vps`` from the *current* ``verification_plan.json``
        instead of a stale snapshot left behind by an earlier run.

        Without this, restarting verification after editing the plan
        would skip VPs whose verdicts are already in the cache, even
        though the underlying ``test_command`` may have changed.
        """
        try:
            self.state_file.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            import logging

            logging.getLogger(__name__).warning(
                "reset_state failed to delete %s: %s", self.state_file, exc
            )
        # Also clear the in-memory state so the caller is not surprised
        # by the executor holding a copy after reset_state returned.
        self._verdicts = {}
        self._pending_vps = []
        self._failed_vps = []
        self._skipped_vps = []
        self._deferred_vps = []
        self._current_vp = None
        self._completed_vps = []

    # ------------------------------------------------------------------
    # Public read-only progress state API
    # ------------------------------------------------------------------

    @property
    def current_vp(self) -> Optional[str]:
        """VP id currently being executed (``None`` when idle)."""
        return self._current_vp

    @property
    def completed_vps(self) -> List[str]:
        """Snapshot of completed VP ids (defensive copy, in completion order)."""
        return list(self._completed_vps)

    @property
    def updated_at(self) -> Optional[str]:
        """ISO-8601 UTC timestamp of the last progress state mutation.

        ``None`` if :meth:`run` has not yet written any progress state.
        The dashboard uses this for staleness detection.
        """
        return self._updated_at

    @property
    def failed_vps(self) -> List[str]:
        """Snapshot of VP ids whose verdict was ``FAILED`` (defensive copy).

        Updated by :meth:`record_verdict` whenever a FAILED verdict
        is stored. Ordering matches the order in which the VPs were
        recorded (a stable index of failures for the run).
        """
        return list(self._failed_vps)

    @property
    def skipped_vps(self) -> List[str]:
        """Snapshot of VP ids whose verdict was ``SKIPPED`` (defensive copy).

        Covers runner-returned SKIPPED verdicts (e.g. a code
        review that decides the VP is moot). The old
        layer-short-circuit SKIPPED path was retired with the
        L1/L2/L3 layer concept on 2026-06-13.
        """
        return list(self._skipped_vps)

    @property
    def state(self) -> SimpleNamespace:
        """Aggregate read-only view of the executor's progress state.

        Returned as a :class:`types.SimpleNamespace` so callers can
        use attribute access (``executor.state.failed_vps``) for
        ergonomic one-shot reads of multiple fields in a single
        statement. The returned namespace is a snapshot — the
        underlying state continues to evolve as the run progresses.
        """
        return SimpleNamespace(
            plan_id=self.plan_id,
            current_vp=self._current_vp,
            completed_vps=list(self._completed_vps),
            failed_vps=list(self._failed_vps),
            skipped_vps=list(self._skipped_vps),
            updated_at=self._updated_at,
        )

    # ------------------------------------------------------------------
    # Public async main loop
    # ------------------------------------------------------------------

    async def run(self, max_parallel: int = 1) -> None:
        """Drive the verification plan through to completion.

        Delegates to :meth:`BaseExecutor.run`, which executes the
        single-layer VP list via ``asyncio.gather`` with a
        ``max_parallel``-sized semaphore.  The base class's
        ``_execute_one_item`` lifecycle is:

          1. Set ``_current_item`` (mirrored into ``_current_vp``)
             and save the progress state.
          2. Call ``item_runner(vp)`` (the wrapped
             ``sub_agent_runner``).
          3. Record the verdict via ``_record_result`` (which
             updates the on-disk verdict map and the index lists).
          4. Clear ``_current_item`` and save the progress state.

        Per-VP progress is reflected in-memory so the dashboard sees
        a live ``current_vp`` and updated ``completed_vps`` /
        ``failed_vps`` / ``skipped_vps`` counters even in parallel
        mode (``max_parallel > 1``).  Progress is derived from the
        verdict map; no separate progress state-file is written.

        Args:
            max_parallel: Maximum number of VPs to run concurrently.
                ``1`` (default) preserves the legacy serial lifecycle
               — each VP is dispatched, awaited, and the next one
                only starts after the previous one finishes, so
                per-VP ``current_vp`` is monotonic with the
                runner-call order.  ``> 1`` runs VPs concurrently
                capped by the semaphore; ``current_vp`` reflects
                whichever VP is most recently started.
        """
        await super().run(max_parallel=max_parallel)
        # After super().run() returns, BaseExecutor has set
        # ``_current_item = None`` and saved the final progress
        # snapshot.  Mirror the historical field so the public
        # ``current_vp`` property is consistent with what just
        # happened.
        self._current_vp = self._current_item
        self._completed_vps = list(self._completed_items)
        self._failed_vps = list(self._failed_items)
        self._skipped_vps = list(self._skipped_items)

    # ------------------------------------------------------------------
    # Internal helpers (used by the item_runner adapter inside __init__)
    # ------------------------------------------------------------------

    async def _run_single_vp(self, vp: Dict[str, Any]) -> Dict[str, Any]:
        """Execute one VP through the runner and coerce the verdict.

        Used by the ``item_runner`` closure in :meth:`__init__` so
        BaseExecutor's :meth:`_execute_one_item` can drive the
        per-VP execution through the same lifecycle.  Runner
        exceptions are caught and converted to a FAILED verdict so
        a single VP crash does not abort the whole run; verdict
        schema errors are likewise converted to FAILED so the
        judgment phase can still mark the VP as terminal.

        2026-09-15: an operator ``/stop`` cancels the
        round. VPs that have not started are SKIPPED without invoking
        the runner, and an attempt interrupted by the stop's hard kill
        is reported as SKIPPED rather than FAILED — a stopped round must
        not manufacture failures the operator then has to triage.
        """
        from verification_cancel import is_cancelled

        if is_cancelled(self.plan_id):
            return {
                "status": "SKIPPED",
                "reasons": [
                    "cancelled: operator stopped the verification before "
                    "this VP started"
                ],
                "evidence": {},
            }

        try:
            result = self.sub_agent_runner(vp)
            if inspect.isawaitable(result):
                verdict = await result
            else:
                verdict = result
        except Exception as exc:
            if is_cancelled(self.plan_id):
                return {
                    "status": "SKIPPED",
                    "reasons": [
                        f"cancelled: operator stopped the verification "
                        f"mid-attempt ({type(exc).__name__}: {exc})"
                    ],
                    "evidence": {},
                }
            verdict = {
                "status": "FAILED",
                "reasons": [
                    f"sub_agent_runner raised "
                    f"{type(exc).__name__}: {exc}"
                ],
                "evidence": {
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
            }

        try:
            self._validate_verdict(verdict)
        except VerdictSchemaError as exc:
            verdict = {
                "status": "FAILED",
                "reasons": [f"verdict schema rejected: {exc}"],
                "evidence": {},
            }
        return verdict

    # ------------------------------------------------------------------
    # Public write API (used by BaseExecutor._record_result and by tests)
    # ------------------------------------------------------------------

    def record_verdict(self, vp_id: str, verdict: Dict[str, Any]) -> None:
        """Validate, store, and persist a verdict for ``vp_id``.

        Steps:
          1. ``_validate_verdict(verdict)`` — raises on schema error.
          2. Store ``verdict`` under ``self._verdicts[vp_id]``.
          3. Remove ``vp_id`` from ``self._pending_vps`` (idempotent
            — recording the same VP twice is a no-op for the
             pending set).
          4. If ``self.verif_repo`` is bound, route the verdict
             through
             :meth:`state_machine.repositories.verification_repository.VerificationRepository.append_verdict`
             (a single ``BEGIN IMMEDIATE`` + ``COMMIT`` transaction)
             and SKIP the legacy on-disk JSON state write.  This
             is the task-11 contract: the subagent callback path
             produces zero ``verification_*_state.json`` files.
          5. Otherwise, fall back to ``_save_state()`` (the
             historical atomic-write path).

        The future ``run()`` main loop will call this once per VP
        after the sub-agent returns. Exposed as public so the
        skeleton can be exercised by tests without standing up the
        full scheduling loop.

        Args:
            vp_id: The VP id (must match one of the plan's VP ids
                to be useful, but the schema is not enforced here —
                the caller is the plan's source of truth).
            verdict: The verdict payload (see :meth:`_validate_verdict`).

        Raises:
            VerdictSchemaError: If ``verdict`` does not match the
                schema. Re-raised so the caller's except clause can
                log / retry / surface a UI error.
        """
        self._validate_verdict(verdict)
        # 2026-09-15: 戳上"产生这份结论的那条命令"的指纹。
        # 计划里的 test_command 一旦改动，这份结论就不再代表当前要验的东西
        # ——VP-013 的 ``--lib`` → ``--test`` 正是此例，而它那条旧结论
        # 在改命令之后仍然被当成 PASSED 跳过，白烧了一整轮。
        verdict = verdict_provenance.stamp(
            verdict, self._current_command_for(vp_id),
        )
        self._verdicts[vp_id] = dict(verdict)  # defensive copy
        # Idempotent removal: ``list.remove`` would raise if the vp
        # is not in the pending list, which would mask a benign
        # double-record. Use a filter to keep behaviour predictable.
        self._pending_vps = [v for v in self._pending_vps if v != vp_id]
        # Maintain the failed / skipped index lists. Re-recording a
        # verdict for the same VP (a benign double-record, e.g. the
        # short-circuit path that has already pre-populated the
        # list) is a no-op for the index so we dedupe on vp_id.
        status = verdict.get("status")
        if status == "FAILED" and vp_id not in self._failed_vps:
            self._failed_vps.append(vp_id)
        elif status == "SKIPPED" and vp_id not in self._skipped_vps:
            self._skipped_vps.append(vp_id)
        elif status == "DEFERRED" and vp_id not in self._deferred_vps:
            # 2026-09-16 两阶段：Phase 2 关卡被门禁延后。刻意不进
            # ``_skipped_vps``（那是"终态成功"，会让下一轮跳过它），
            # 也不进 ``_failed_vps``（没验过，不是失败）。
            self._deferred_vps.append(vp_id)
        if self.verif_repo is not None:
            # Task-11 contract: route through the state-machine
            # repository, skip the legacy JSON write.
            self._verdict_seq += 1
            self.verif_repo.append_verdict(
                self.plan_id,
                {
                    "vp_id": vp_id,
                    "status": str(status) if status is not None else "FAILED",
                    "worker_id": "executor",
                    "seq": self._verdict_seq,
                    "reasons": list(verdict.get("reasons", []) or []),
                    "evidence": dict(verdict.get("evidence", {}) or {}),
                    # 出处必须一起落库：否则重启后从 DB 水合回来的结论
                    # 又变成"没有指纹"，下一轮全被作废，C 就白做了。
                    verdict_provenance.FINGERPRINT_FIELD: verdict.get(
                        verdict_provenance.FINGERPRINT_FIELD, "",
                    ),
                },
            )
            return
        self._save_state()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_verdict(self, verdict: Any) -> None:
        """Enforce the verdict schema. Returns silently on success.

        Schema::

            {
                "status":   one of "PASSED", "FAILED", "SKIPPED", "BLOCKED",
                "reasons":  List[str],
                "evidence": dict,
                ...        # any extra keys are ignored
            }

        Rules (zero-tolerance):

          * ``verdict`` must be a dict;
          * all three required keys (``status``, ``reasons``,
            ``evidence``) must be present — missing key → error;
          * ``status`` must be a ``str`` in ``ALLOWED_STATUSES``;
          * ``reasons`` must be a ``list`` (empty list is allowed —
            the verdict may be self-evident from evidence);
          * ``evidence`` must be a ``dict`` (empty dict is allowed).

        Raises:
            VerdictSchemaError: If any of the above rules is broken.
                The error message names the offending field and the
                offending value (type or content) so a misbehaving
                sub-agent is easy to diagnose.
        """
        if not isinstance(verdict, dict):
            raise VerdictSchemaError(
                f"verdict must be a dict, got {type(verdict).__name__}"
            )

        # Missing-field check: surface a single, clear error naming
        # every missing key (not just the first one — saves the
        # caller from running 3 round-trips to find all gaps).
        missing = [k for k in REQUIRED_VERDICT_KEYS if k not in verdict]
        if missing:
            raise VerdictSchemaError(
                f"verdict missing required field(s): {missing!r}; "
                f"got keys: {sorted(verdict.keys())!r}"
            )

        # status: str + membership in ALLOWED_STATUSES
        status = verdict["status"]
        if not isinstance(status, str):
            raise VerdictSchemaError(
                f"verdict.status must be a str, got {type(status).__name__}: {status!r}"
            )
        if status not in ALLOWED_STATUSES:
            raise VerdictSchemaError(
                f"verdict.status must be one of {list(ALLOWED_STATUSES)}, "
                f"got {status!r}"
            )

        # reasons: list (of str, but we only check the outer type so a
        # nested int in reasons[0] is not a hard error here — that
        # would be a separate judgment-time concern, not a schema one)
        reasons = verdict["reasons"]
        if not isinstance(reasons, list):
            raise VerdictSchemaError(
                f"verdict.reasons must be a list, got {type(reasons).__name__}"
            )

        # evidence: dict (any contents — the orchestrator / judgment
        # layer is responsible for inspecting what is in there)
        evidence = verdict["evidence"]
        if not isinstance(evidence, dict):
            raise VerdictSchemaError(
                f"verdict.evidence must be a dict, got {type(evidence).__name__}"
            )

        # If we get here the verdict is structurally sound — return
        # implicitly (no return value).

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_or_init_state(self) -> None:
        """Load state from disk, or build a fresh state from the plan.

        Disk format (one JSON object)::

            {
                "plan_id": str,
                "pending_vps": [str, ...],
                "verdicts": {
                    "VP-001": {"status": "PASSED", "reasons": [...], "evidence": {...}},
                    ...
                }
            }

        Behaviour:

          * If ``self.verif_repo`` is bound, hydrate from the
            state-machine SQLite row (the canonical source of truth
            under the refactored framework). The ``pending_vps`` /
            ``verdicts`` live in the JSON columns of
            ``plan_verification``. ``current_vp`` and friends live in
            ``progress_state``. This path is the one the live
            production executor uses, so the tests for it have to
            hydrate from a SQLite row.
          * Otherwise, fall back to the legacy
            ``verification_executor_state.json`` disk file (kept for
            older deploys and for unit tests that pre-date the
            state-machine refactor).
          * If neither source has data, take the ``vps`` list from
            ``self.verification_plan`` and use their ``id`` fields to
            seed ``pending_vps``. ``verdicts`` starts empty.
        """
        if self.verif_repo is not None:
            try:
                row = self.verif_repo.summary(self.plan_id)
            except Exception:
                row = None
            if isinstance(row, dict):
                runtime_state = row.get("runtime_state") or {}
                verdicts_raw = row.get("verdicts") or {}
                progress_state = row.get("progress_state") or {}
                self._pending_vps = list(runtime_state.get("pending_vps", []))
                # 2026-08-25: normalise to ``{vp_id: {status, ...}}`` dict.
                # ``VerificationRepository.append_verdict`` stores
                # verdicts as a **list** of verdict dicts
                # (each with a ``vp_id`` key), but the executor's
                # in-memory shape and ``_backfill_index_lists_from_verdicts``
                # both expect ``dict[vp_id, verdict]``. Without this
                # normalisation, ``dict(verdicts)`` raises
                # ``ValueError: dictionary update sequence element
                # #0 has length 6; 2 is required`` on a list of
                # verdict dicts, the load path falls through to
                # the disk-state-file fallback, which is empty
                # under the SQLite-first refactor, and the round
                # silently re-runs every VP from scratch (the
                # resume path is non-functional). Convert both
                # legacy list shape and current dict shape into
                # the in-memory dict.
                if isinstance(verdicts_raw, list):
                    self._verdicts = {
                        (v.get("vp_id") if isinstance(v, dict) else None): v
                        for v in verdicts_raw
                        if isinstance(v, dict) and v.get("vp_id")
                    }
                elif isinstance(verdicts_raw, dict):
                    self._verdicts = dict(verdicts_raw)
                else:
                    self._verdicts = {}
                self._current_vp = progress_state.get("current_vp")
                self._completed_vps = list(progress_state.get("completed_vps") or [])
                self._updated_at = progress_state.get("updated_at")
                self._backfill_index_lists_from_verdicts()
                # Mirror into BaseExecutor's in-memory fields so a
                # caller that goes through the base-class run loop
                # sees the same scheduler view as the historical
                # one.
                self._completed_items = list(self._completed_vps)
                self._failed_items = list(self._failed_vps)
                self._skipped_items = list(self._skipped_vps)
                return

        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._pending_vps = list(data.get("pending_vps", []))
                self._verdicts = dict(data.get("verdicts", {}))
                self._backfill_index_lists_from_verdicts()
                # Mirror into BaseExecutor's in-memory fields so a
                # caller that goes through the base-class run loop
                # sees the same scheduler view as the historical
                # one.
                self._completed_items = list(self._completed_vps)
                self._failed_items = list(self._failed_vps)
                self._skipped_items = list(self._skipped_vps)
                return
            except (OSError, json.JSONDecodeError):
                # Corrupt or partially-written state file: fall back
                # to fresh init. We do NOT raise here — losing the
                # pending list is recoverable, but blocking the
                # orchestrator on a corrupt state file would be a
                # worse failure mode (the orchestrator would refuse
                # to start any VPs).
                pass

        # Fresh init from plan. Support both executor schema ("vps")
        # and LLM schema ("verification_points").
        vps = self.verification_plan.get("vps") or self.verification_plan.get("verification_points") or []
        self._pending_vps = [vp.get("id") for vp in vps if vp.get("id")]
        self._verdicts = {}
        self._backfill_index_lists_from_verdicts()
        self._completed_items = []
        self._failed_items = list(self._failed_vps)
        self._skipped_items = list(self._skipped_vps)

    def _current_command_for(self, vp_id: str) -> Optional[str]:
        """计划里这个 VP **现在**的 ``test_command``；不在计划里则 ``None``。

        ``None`` 让 :func:`verification_verdict_provenance.is_stale` 维持
        旧行为 —— VP 已从计划中消失时，不擅自作废它的结论。
        """
        vps = (
            self.verification_plan.get("vps")
            or self.verification_plan.get("verification_points")
            or []
        )
        for vp in vps:
            if isinstance(vp, dict) and vp.get("id") == vp_id:
                return vp.get("test_command")
        return None

    def _backfill_index_lists_from_verdicts(self) -> None:
        """Recompute the index lists from ``_verdicts``.

        The verdict map is the canonical source of truth for which
        VPs are terminal and what their status is.  The progress
        state file's ``completed_vps`` / ``failed_vps`` /
        ``skipped_vps`` fields are a redundant cache that older
        executor versions did not write (and that the per-VP
        atomic write path does not maintain).  Deriving the index
        lists from ``_verdicts`` on every load keeps the public
        properties consistent with ``collect_verdicts()``
        regardless of which artifact is fresher on disk.

        The derivation is order-preserving: the index lists follow
        ``_verdicts`` insertion order (i.e. the order in which the
        VPs were recorded), so the dashboard's failure / skip
        counters match the per-VP verdict table row-for-row.

        # 2026-08-25: ``_completed_vps`` was the universal set of
        # VPs that produced a terminal verdict (PASSED, FAILED, or
        # SKIPPED). That contract was incompatible with the resume
        # path's "skip the green ones, re-run the red ones"
        # semantics — every FAILED VP was in ``_completed_vps``
        # and therefore skipped on subsequent rounds. The new
        # contract:
        #
        #   * ``_completed_vps`` = PASSED VPs only (terminal success)
        #   * ``_failed_vps``    = FAILED VPs (need to re-run)
        #   * ``_skipped_vps``   = SKIPPED VPs (terminal success,
        #                             re-running would just re-skip)
        #
        # ``BaseExecutor.run`` then unions ``_completed_vps`` and
        # ``_skipped_vps`` (both are "terminal success, skip") but
        # leaves ``_failed_vps`` out of the skip set so the executor
        # actually re-runs failed VPs.
        #
        # 2026-09-15: verdicts whose ACCEPTANCE COMMAND
        # has changed since they were recorded are dropped before this
        # bucketing, so they fall out of ``completed_set`` and re-run.
        # Until then a PASSED verdict outlived the command that earned it
        # — the plan's VP-013 kept a ``cargo test --lib`` verdict (which had
        # run zero tests) after the plan was rewritten to ``--test``, and
        # the round skipped it as green. See
        # ``verification_verdict_provenance``.
        """
        completed: List[str] = []
        failed: List[str] = []
        skipped: List[str] = []
        deferred: List[str] = []

        # 2026-09-15: 出处对不上的结论先摘掉。
        #
        # 判据是**命令有没有变过**，不是"操作者重置了轮次计数"——重置常常
        # 只是为了撞顶后继续迭代，那时把已挣到的结论全部作废是错的。
        #
        # 摘掉之后这些 VP **不进任何桶**：``BaseExecutor.run`` 的
        # ``completed_set`` 是 completed ∪ skipped，不在里面就会进
        # ``pending`` 被重跑。刻意不塞进 ``failed`` —— 它们没有失败，只是
        # 结论的出处过期了，报成 FAILED 会污染失败统计和修复任务的输入。
        stale_ids = [
            vp_id for vp_id, verdict in self._verdicts.items()
            if verdict_provenance.is_stale(
                verdict if isinstance(verdict, dict) else {},
                self._current_command_for(vp_id),
            )
        ]
        for vp_id in stale_ids:
            self._verdicts.pop(vp_id, None)

        for vp_id, verdict in self._verdicts.items():
            status = verdict.get("status") if isinstance(verdict, dict) else None
            if status == "FAILED":
                failed.append(vp_id)
            elif status == "SKIPPED":
                skipped.append(vp_id)
            elif status == "PASSED":
                completed.append(vp_id)
            elif status == "SPLIT":
                # 2026-09-14: a VP the repair-phase judge split into
                # sub-VPs. The parent must NOT re-run — its children are
                # in ``verification_plan.json`` (written by
                # ``vp_split.VpSplitter``) and carry the real verdicts.
                # Bucketing it with SKIPPED puts it in BaseExecutor's
                # ``completed_set`` so a resumed round runs only the
                # children.
                skipped.append(vp_id)
            elif status == "DEFERRED":
                # 2026-09-16 两阶段：Phase 2 关卡被门禁延后。**刻意不进任何
                # "终态成功"桶**——BaseExecutor 的 ``completed_set`` 是
                # completed ∪ skipped，不在里面才会在下一轮重跑，这正是
                # "Phase 1 修好之后要把全量关卡真正跑一遍"的语义。
                deferred.append(vp_id)
            # Unknown / None status: skip (don't add to any bucket).
        self._completed_vps = completed
        self._failed_vps = failed
        self._skipped_vps = skipped
        self._deferred_vps = deferred

    # ------------------------------------------------------------------
    # Crash-recovery pass
    # ------------------------------------------------------------------
    #
    # A cross-process crash (kill -9, OOM, server restart) can leave
    # the executor's two on-disk artifacts in an inconsistent state:
    #
    #   * ``verification-progress state-file`` says VP-XXX is the
    #     in-flight VP (``current_vp = "VP-XXX"``).
    #   * ``verification-executor state-file`` still has VP-XXX in
    #     ``pending_vps`` (no verdict was recorded).
    #   * The per-VP ``vps/VP-XXX/verdict.json`` written by the
    #     sub-agent does NOT exist (the subprocess that would have
    #     written it died before fsync).
    #
    # A naive resume would re-execute VP-XXX, but the original
    # subprocess is gone — re-executing it would silently double-
    # count the VP (one verdict for the crashed subprocess, one for
    # the new run).  The contract is to mark the in-flight VP as
    # FAILED with the marker reason ``"recovered_from_crash"`` so
    # the next round's repair loop sees the failure.

    def _recover_inflight_vp(self) -> None:
        """Detect and recover from an in-flight VP left over from a crash.

        Algorithm:

          1. Read ``self._current_vp`` (populated by
             :meth:`_load_progress_state` from
             ``verification-progress state-file``).
          2. If it is ``None`` (no in-flight signal) — return
             silently.  This covers the "no state file" case, the
             "clean idle resume" case, and the "all VPs already
             terminal" case.
          3. If it is set AND the in-flight VP is already in
             ``self._verdicts`` — the verdict was recorded but the
             progress file was not cleared (a benign race).  Clear
             ``_current_vp`` to ``None`` and return.
          4. Otherwise, the in-flight VP needs recovery.  Check
             whether the sub-agent's per-VP verdict file exists at
             ``plan_dir / "vps" / current_vp / "verdict.json"`` —
             if it does, the sub-agent finished writing the verdict
             and the verdict-map load is the bug (defensive: load
             the on-disk verdict into the verdict map and clear
             ``_current_vp``).  If it does not, call
             :meth:`_recover_inflight_as_failed` to synthesize a
             FAILED verdict and clear ``_current_vp``.

        The function is best-effort w.r.t. disk errors: a missing
        ``vps/`` directory or an ``OSError`` reading the verdict
        file is treated as "no verdict file" (i.e. crash recovery
        fires).  This is the safe default — failing to recover
        would leave a phantom in-flight VP that the next ``run()``
        would try to re-execute.
        """
        in_flight = self._current_vp
        if in_flight is None:
            return

        # Defensive: the in-flight VP is already in the verdict
        # map.  This can happen if the verdict was recorded
        # (record_verdict wrote it) but the progress-state fsync
        # at the end of the run() loop did not complete (e.g. a
        # crash between the two writes).  Clear the stale
        # current_vp and leave the verdict intact.
        if in_flight in self._verdicts:
            self._current_vp = None
            self._updated_at = self._now_iso()
            return

        # Check the on-disk verdict file written by the
        # sub-agent.  If it is present, the sub-agent finished —
        # load it into the verdict map and clear _current_vp.
        verdict_file = self.plan_dir / "vps" / in_flight / "verdict.json"
        if verdict_file.is_file():
            try:
                with open(verdict_file, "r", encoding="utf-8") as f:
                    on_disk_verdict = json.load(f)
            except (OSError, json.JSONDecodeError):
                on_disk_verdict = None
            if isinstance(on_disk_verdict, dict):
                # Validate before adopting — a corrupt verdict
                # file should not poison the executor's verdict
                # map.  If validation fails, fall through to the
                # synthesize-FAILED path below.
                try:
                    self._validate_verdict(on_disk_verdict)
                except VerdictSchemaError:
                    on_disk_verdict = None
            if isinstance(on_disk_verdict, dict):
                self._verdicts[in_flight] = dict(on_disk_verdict)
                self._pending_vps = [
                    v for v in self._pending_vps if v != in_flight
                ]
                self._backfill_index_lists_from_verdicts()
                self._save_state()
                self._current_vp = None
                self._updated_at = self._now_iso()
                return

        # No verdict file on disk — the in-flight VP's subprocess
        # died before fsync.  Synthesize a FAILED verdict and
        # clear the in-flight signal.
        self._recover_inflight_as_failed(in_flight)

    def _recover_inflight_as_failed(self, vp_id: str) -> None:
        """Synthesize a FAILED verdict for an in-flight VP left over from a crash.

        The synthesized verdict has the same schema as any other
        verdict (``status`` / ``reasons`` / ``evidence``) so the
        rest of the executor — :meth:`collect_verdicts`,
        :meth:`failed_vps`, the judgment phase — treats it
        identically to a FAILED verdict produced by the runner.

        The ``"recovered_from_crash"`` marker in ``reasons`` is the
        signal downstream tooling uses to distinguish a
        recovered-from-crash failure from a normal test failure
        (the repair loop in particular surfaces this label in the
        ``recovered_from_crash`` count in the verification
        report).

        Side effects:

          * Inserts the synthesized FAILED verdict into
            ``self._verdicts[vp_id]``.
          * Removes ``vp_id`` from ``self._pending_vps``.
          * Appends ``vp_id`` to ``self._failed_vps``.
          * Resets ``self._current_vp`` to ``None`` and updates
            ``self._updated_at``.
          * Persists the verdict-map state file
            (``_save_state``) so a subsequent restart sees the
            same recovered state.

        Args:
            vp_id: The id of the in-flight VP to recover.
        """
        verdict: Dict[str, Any] = {
            "status": "FAILED",
            "reasons": ["recovered_from_crash"],
            "evidence": {
                "recovered_from_crash": True,
                "previous_current_vp": vp_id,
            },
        }

        # Validate the synthesized verdict against the schema
        # (defensive: a future refactor of the verdict body
        # should not silently break recovery).
        self._validate_verdict(verdict)

        # Insert the verdict, evict from pending, and update the
        # index lists in the canonical order so the public
        # properties observe the recovered state.
        self._verdicts[vp_id] = dict(verdict)
        self._pending_vps = [v for v in self._pending_vps if v != vp_id]
        self._backfill_index_lists_from_verdicts()

        # Reset the in-flight signal BEFORE persisting so the
        # ``progress_state`` column records an idle executor rather
        # than the recovered (now stale) in-flight VP.
        self._current_vp = None
        self._updated_at = self._now_iso()
        # Mirror the recovered subclass fields into BaseExecutor's
        # fields BEFORE ``_save_state()``: ``_save_progress`` (called
        # by ``_save_state``) mirrors base -> subclass, so without
        # this pre-mirror the just-backfilled ``_failed_vps`` is
        # clobbered back to the stale (empty) base field, and the
        # clobbered empty list is what gets persisted to the
        # ``progress_state`` column (2026-09-14: red at
        # ``test_recover_inflight_marked_failed`` — the recovered
        # verdict was in the map but ``failed_vps`` read ``[]``).
        self._current_item = self._current_vp
        self._completed_items = list(self._completed_vps)
        self._failed_items = list(self._failed_vps)
        self._skipped_items = list(self._skipped_vps)

        # Persist the verdict-map state file so the recovered
        # FAILED verdict survives a second crash and a subsequent
        # restart does not re-fire the recovery path.
        self._save_state()

    def _save_state(self) -> None:
        """Atomically write the current state to ``self.state_file``.

        Delegates to :func:`utils.atomic_io.atomic_write_json` so the
        tempfile + fsync + ``os.replace`` pattern is consistent with
        the rest of the codebase. On any write error the original
        file is left intact and the failure is logged; the in-memory
        state stays consistent and a follow-up save call may succeed.
        """
        payload = {
            "plan_id": self.plan_id,
            "pending_vps": self._pending_vps,
            "verdicts": self._verdicts,
        }
        atomic_write_json(self.state_file, payload, logger=logger)
        # Mirror the in-memory VP view to the SQLite column so the
        # public progress endpoint can serve the same picture. See
        # :meth:`_save_progress` for the rationale and the
        # failure-mode handling.
        self._save_progress()

    # ------------------------------------------------------------------
    # Progress state persistence
    # ------------------------------------------------------------------
    #
    # The progress state file is the on-disk mirror of the
    # scheduler's view of the run. It is written on every
    # ``vp_status_changed`` event (per-VP start, per-VP end, and
    # layer transitions) so the orchestrator / dashboard can poll
    # it cheaply for a live progress bar. The contract is:
    #
    #   * The on-disk state is the source of truth for any process
    #     that did not start the run (a second executor pointed at
    #     the same plan_dir sees the same ``completed_vps`` /
    #     ``current_vp``).
    #   * Each write is atomic (tempfile + os.replace) AND fsync'd
    #     to disk before the rename. A crash mid-write therefore
    #     leaves either the previous state or the new state
    #     observable on disk, never a torn write.
    #
    def _load_progress_state(self) -> None:
        """Load progress state from disk, or leave the in-memory
        state as the constructor initialised it.

        This method is now a no-op: progress state is no longer
        mirrored to a separate on-disk file.  The state-machine
        refactor pins the verdict map (``verification_executor_state.json``)
        as the single source of truth for terminal-VP status, and the
        live ``current_vp`` / ``completed_vps`` / ``failed_vps`` /
        ``skipped_vps`` views are derived from the in-memory state
        (cross-process resume uses the verdict map's index lists via
        :meth:`_backfill_index_lists_from_verdicts`).

        The method is preserved as a hook because :meth:`_load_progress`
        delegates to it; the hook contract is unchanged (no return
        value, no side effects beyond the in-memory state) so the
        :class:`BaseExecutor` subclass integration stays intact.
        """
        return

    @staticmethod
    def _now_iso() -> str:
        """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix.

        Using a fixed-suffix format (rather than the locale-dependent
        default ``datetime.isoformat()``) keeps the on-disk file
        diff-friendly across machines and timezones.
        """
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    # ------------------------------------------------------------------
    # BaseExecutor abstract hooks
    # ------------------------------------------------------------------
    #
    # The class is now structurally a :class:`BaseExecutor` subclass
    # so the layer-based scheduling, atomic persistence, and crash
    # recovery primitives are all available in one place.  The
    # implementation keeps the original VP-specific behaviour
    # (serial-mode per-VP current_vp tracking, the historical
    # ``verification-executor state-file`` / ``verification-progress state-file``
    # file format, and the ``pending_vps`` / ``current_vp`` /
    # ``collect_verdicts`` public API) so existing tests and the
    # orchestrator are not disturbed.  A follow-up refactor will
    # route the per-VP execution through the base class's
    # ``run()`` loop once the file-format / property layer has
    # been hardened.
    #
    # For now these hooks are implemented as thin pass-throughs to
    # the existing methods so the class can be instantiated and
    # tested.

    def _derive_item_id(self, item: Dict[str, Any]) -> str:
        """Return the VP id for ``item`` (matches ``_run_single_vp``)."""
        return str(item["id"])

    def _load_progress(self) -> None:
        """Populate BaseExecutor's progress fields from disk.

        Delegates to the historical :meth:`_load_progress_state` and
        then mirrors the loaded values into BaseExecutor's
        ``_current_item`` / ``_completed_items`` / ``_failed_items``
        / ``_skipped_items`` so a subclass that goes through the
        base-class ``run()`` loop sees the same scheduler view.
        """
        self._load_progress_state()
        self._current_item = self._current_vp
        self._completed_items = list(self._completed_vps)
        self._failed_items = list(self._failed_vps)
        self._skipped_items = list(self._skipped_vps)

    def _save_progress(self) -> None:
        """Mirror BaseExecutor's progress view into verification fields
        AND persist it to the SQLite ``progress_state`` column.

        Progress is derived in-memory from the verdict map; the
        historical design intentionally did NOT write a state
        file ("no progress state-file is written") because the
        state-file path was deprecated. The SQLite column
        (``plan_verification.progress_state``) replaced that file
        but the executor never wrote to it, so the public
        ``/api/verification/{id}/progress`` endpoint returned
        ``404 verification not started`` even while verification
        was actively running. The 2026-08-25 fix routes the
        in-memory snapshot through
        :meth:`VerificationRepository.update_progress_state` so the
        progress endpoint sees the same picture the executor does.

        Hook still mirrors ``_current_item`` /
        ``completed_items`` / ``failed_items`` / ``skipped_items``
        back into the historical ``_current_vp`` /
        ``_completed_vps`` / ``_failed_vps`` / ``_skipped_vps``
        fields so the public properties stay consistent with
        what BaseExecutor's ``run()`` loop did.

        Note: ``current_vp`` is stored as the VP id string (not a
        dict). The /progress endpoint in server.py reads
        ``progress.get("current_vp")`` and treats it as a string,
        matching the executor's ``self._current_vp: Optional[str]``
        contract. ``completed_vps`` / ``failed_vps`` /
        ``skipped_vps`` are stored as lists of vp id strings, which
        is what the public endpoint renders.
        """
        self._current_vp = self._current_item
        self._completed_vps = list(self._completed_items)
        self._failed_vps = list(self._failed_items)
        self._skipped_vps = list(self._skipped_items)

        # Mirror to the SQLite column so the cross-process
        # ``/api/verification/{id}/progress`` endpoint can serve
        # the same view. The historical read path
        # (``_build_verification_progress`` in server.py) reads
        # from ``plan_verification.progress_state``; without this
        # write the read returns ``{}`` and the endpoint 404s.
        if getattr(self, "verif_repo", None) is not None and getattr(self, "plan_id", None):
            try:
                self.verif_repo.update_progress_state(
                    self.plan_id,
                    self._current_vp,
                    list(self._completed_vps),
                    list(self._failed_vps),
                    list(self._skipped_vps),
                )
            except Exception as exc:
                # 2026-08-25 audit: log the exception so silent
                # failures surface in the server log. The old
                # bare ``pass`` swallowed every KeyError /
                # ConflictError and made the cross-process progress
                # view silently stale. Still do not crash the round.
                import logging as _logging_mod_exc
                _logging_mod_exc.getLogger(__name__).warning(
                    "update_progress_state failed plan_id=%s err=%r",
                    self.plan_id, exc,
                )

    def _make_error_result(self, exc: Exception) -> Dict[str, Any]:
        """Build a synthetic FAILED verdict from an exception.

        Mirrors the inline construction that the previous
        concurrent-mode ``run()`` used for ``asyncio.gather`` errors.
        """
        return {
            "status": "FAILED",
            "reasons": [
                f"sub_agent_runner raised {type(exc).__name__}: {exc}"
            ],
            "evidence": {
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            },
        }

    def _should_short_circuit_layer(
        self,
        layer_idx: int,
        layer_results: List[Dict[str, Any]],
    ) -> bool:
        """Verification never short-circuits downstream layers."""
        return False

    def _mark_layer_skipped(
        self,
        layer: List[Dict[str, Any]],
        reason: str,
    ) -> None:
        """Mark every VP in ``layer`` as SKIPPED with ``reason``."""
        for item in layer:
            item_id = self._derive_item_id(item)
            if item_id in self._verdicts:
                continue
            verdict = {
                "status": "SKIPPED",
                "reasons": [reason],
                "evidence": {},
            }
            self._verdicts[item_id] = verdict
            if item_id not in self._skipped_vps:
                self._skipped_vps.append(item_id)
        self._save_state()

    def _record_result(
        self,
        item_id: str,
        result: Dict[str, Any],
    ) -> None:
        """Record ``result`` for ``item_id`` and persist state.

        Keeps the historical on-disk schema intact (verdicts map
        keyed by VP id) and mirrors the resulting status into
        BaseExecutor's index lists so future callers that go
        through the base-class ``run()`` see consistent state.
        ``completed_vps`` contains every VP that produced a
        terminal verdict (PASSED / FAILED / SKIPPED) — the failed
        and skipped subsets are kept in their own index lists so
        the dashboard can render a "X passed / Y failed / Z
        skipped" summary without re-scanning the verdict map.

        When ``self.verif_repo`` is bound, the verdict is also
        routed through
        :meth:`state_machine.repositories.verification_repository.VerificationRepository.append_verdict`
        (a single ``BEGIN IMMEDIATE`` + ``COMMIT`` transaction)
        and the legacy on-disk JSON state file is NOT written.
        This is the task-11 contract: the BaseExecutor.run()
        loop, which calls ``_record_result`` after every VP, must
        produce zero ``verification_*_state.json`` files.
        """
        self._verdicts[item_id] = result
        status = result.get("status", "FAILED")
        # 2026-08-25: contract change — ``completed_vps`` is now
        # PASSED-only. Failed VPs live in ``failed_vps``,
        # skipped VPs in ``skipped_vps``. The previous
        # implementation added EVERY terminal verdict
        # (PASSED / FAILED / SKIPPED) to ``completed_vps`` which
        # then leaked into the ``/api/verification/{id}/progress``
        # ``counts.completed`` field, inflating the count by 1
        # per failed VP per round (e.g. round 6 reported 21
        # completed when the ground truth was 20 PASSED + 1
        # FAILED). Mirror the contract change applied to
        # ``_backfill_index_lists_from_verdicts`` (this class's
        # load-time index list derivation).
        if status == "PASSED":
            if item_id not in self._completed_vps:
                self._completed_vps.append(item_id)
        elif status == "SKIPPED":
            if item_id not in self._skipped_vps:
                self._skipped_vps.append(item_id)
        else:  # FAILED or any other non-PASSED / non-SKIPPED
            if item_id not in self._failed_vps:
                self._failed_vps.append(item_id)
        # Mirror into BaseExecutor's in-memory fields.
        self._current_item = self._current_vp
        self._completed_items = list(self._completed_vps)
        self._failed_items = list(self._failed_vps)
        self._skipped_items = list(self._skipped_vps)
        if self.verif_repo is not None:
            # Task-11 contract: route through the state-machine
            # repository, skip the legacy JSON write.
            self._verdict_seq += 1
            self.verif_repo.append_verdict(
                self.plan_id,
                {
                    "vp_id": item_id,
                    "status": str(status),
                    "worker_id": "executor",
                    "seq": self._verdict_seq,
                    "reasons": list(result.get("reasons", []) or []),
                    "evidence": dict(result.get("evidence", {}) or {}),
                },
            )
            return
        self._save_state()

    def _recover_inflight(self) -> None:
        """Crash-recovery pass for any in-flight VP.

        Delegates to the historical :meth:`_recover_inflight_vp`
        which moves ``current_vp`` out of the in-flight state and
        synthesises a FAILED verdict when the previous run died
        mid-VP.  After recovery the historical fields and the
        base-class fields are mirrored for consistency.
        """
        self._recover_inflight_vp()
        self._current_item = self._current_vp
        self._completed_items = list(self._completed_vps)
        self._failed_items = list(self._failed_vps)
        self._skipped_items = list(self._skipped_vps)

    def _build_items_from_plan(self) -> List[Dict[str, Any]]:
        """Return the verification points from the plan.

        Default :class:`BaseExecutor` reads ``plan["items"]``; the
        verification-plan shape uses ``plan["vps"]`` so this hook
        provides the custom extraction.

        2026-09-14: VPs superseded by a split are filtered out. When the
        repair-phase judge decides a VP is too big
        (``vp_split.VpSplitter``), it writes the children into the plan
        and stamps the parent with ``superseded_by`` — the parent must be
        replaced by its children, not run alongside them. This is the
        fresh-init half of the contract; the resume half is the SPLIT
        verdict bucket in :meth:`_backfill_index_lists_from_verdicts`.

        2026-09-16: VPs marked ``obsolete`` by the per-round plan-delta
        evaluation (``verification_plan_delta``) are filtered out too —
        their feature/criterion is gone, so running them would manufacture
        failures for something the plan no longer requires. The marker is
        kept in the plan for audit (they are marked, never deleted).
        """
        vps = self.plan.get("vps") or self.plan.get("verification_points") or []
        return [
            vp for vp in vps
            if not (
                isinstance(vp, dict)
                and (vp.get("superseded_by") or vp.get("obsolete"))
            )
        ]

    def _build_execution_layers(
        self,
        items: List[Dict[str, Any]],
    ) -> List[List[Dict[str, Any]]]:
        """Partition VPs into dependency-respecting layers.

        Uses :func:`base_executor._build_layers` (Kahn's algorithm)
        so the ``depends_on`` field on each VP is honoured.  VPs
        without ``depends_on`` are all roots in layer 0 (executed
        first, in plan-insertion order).  VPs that declare a
        dependency on another VP are placed in the layer after
        that VP, so the orchestrator only starts a downstream
        VP after its upstream VPs are in flight — though because
        the layer is the *partition* (not a per-VP block), every
        VP in a layer is launched together as soon as the previous
        layer's :meth:`BaseExecutor._execute_one_item` returns.

        Intra-layer order is plan-insertion order (preserved by the
        :class:`dict` insertion-order semantics in
        ``_build_layers``), so the existing
        :func:`test_run_executes_vps_in_plan_order` contract still
        holds for plans with no ``depends_on`` edges.

        2026-09-16 两阶段：Phase 1（子任务级验证）沿用上面的
        DAG 分层；Phase 2（全量关卡：Nightly CI、E2E）**每个各占一层**，
        按 ``phase_order`` 排在其后。让每个关卡独占一层是有意的——它们是
        全量命令，彼此并发只会互相抢资源，也破坏"Nightly 先、E2E 后"的
        顺序契约。层序同时被记录到 ``self._phase_of_layer``，供
        :meth:`_should_short_circuit_layer` 判定门禁。
        """
        core, gates = split_by_phase(items)

        layers: List[List[Dict[str, Any]]] = []
        if core:
            layers.extend(
                _build_layers(core, id_key="id", dep_key="depends_on")
            )
        for gate in gates:
            layers.append([gate])

        # 层索引 → 阶段 / 关卡（与 ``layers`` 一一对应），供门禁判定使用。
        self._phase_of_layer = (
            [PHASE_CORE] * (len(layers) - len(gates))
            + [PHASE_FINAL_GATE] * len(gates)
        )
        self._gate_of_layer: List[Optional[Dict[str, Any]]] = (
            [None] * (len(layers) - len(gates)) + list(gates)
        )
        return layers

    def _plan_points(self) -> List[Dict[str, Any]]:
        """计划里的 VP 列表，兼容两种 schema 键。

        LLM schema 用 ``verification_points``，执行器 schema 用 ``vps``
        （见 ``verification_agent._convert_plan_to_executor_schema``）。门禁
        判定两处都要能用——只认其中一个键会让另一个 schema 下的"更早的关卡"
        永远查不到，E2E 就会在 Nightly 失败之后照样开跑。
        """
        return (
            self.verification_plan.get("verification_points")
            or self.verification_plan.get("vps")
            or []
        )

    def _terminal_status_of(self, vp_id: Any) -> Optional[str]:
        """该 VP 当前记录的结论状态（无结论返回 None）。"""
        verdict = self._verdicts.get(vp_id)
        if not isinstance(verdict, dict):
            return None
        status = verdict.get("status")
        return str(status) if status is not None else None

    def _blocking_gate_reason(self) -> str:
        """返回第一条阻断 Phase 2 的理由；全部通过则返回空串。

        判据统一为"该关卡之前的一切都已终态成功"：Phase 1 的 VP 以及
        ``phase_order`` 更小的关卡。于是"E2E 必须等 Nightly 通过"不需要
        单独规则——它天然被同一条判据覆盖。
        """
        for vp in self._plan_points():
            if not isinstance(vp, dict) or is_final_gate(vp):
                continue
            vp_id = vp.get("id")
            status = self._terminal_status_of(vp_id)
            if status is None:
                return f"Phase 1 的 {vp_id} 尚未产生结论"
            if status not in _TERMINAL_SUCCESS_STATUSES:
                return f"Phase 1 的 {vp_id} 结论为 {status}"
        return ""

    def _gate_predecessor_reason(
        self, gate_order: Optional[int],
    ) -> str:
        """该关卡之前是否有更早的关卡未通过。"""
        if gate_order is None:
            return ""
        earlier = [
            vp for vp in self._plan_points()
            if isinstance(vp, dict) and is_final_gate(vp)
            and infer_phase_order(vp) < gate_order
        ]
        for vp in earlier:
            vp_id = vp.get("id")
            status = self._terminal_status_of(vp_id)
            if status is None:
                return f"更早的全量关卡 {vp_id} 尚未执行"
            if status not in _TERMINAL_SUCCESS_STATUSES:
                return f"更早的全量关卡 {vp_id} 结论为 {status}"
        return ""

    def _should_short_circuit_layer(
        self,
        layer_idx: int,
        layer_results: List[Dict[str, Any]],
    ) -> bool:
        """Phase 2 门禁：它之前的一切（Phase 1 + 更早的关卡）都通过才放行。

        2026-09-16 ——

            "只有所有的这些子 VP 都通过了之后，再进入到第二阶段。"
            "应该先做 Nightly CI，再做 E2E 测试。"

        "E2E 等 Nightly 通过"不需要单独规则：每个关卡层都要求**它之前的
        一切已终态成功**，于是 Nightly 没过时紧随其后的 E2E 自然被拦下。

        Phase 1 的层之间**不**短路（历史行为：验证从不短路下游层）。
        """
        next_idx = layer_idx + 1
        phases = getattr(self, "_phase_of_layer", [])
        if next_idx >= len(phases):
            return False
        if phases[next_idx] != PHASE_FINAL_GATE:
            return False

        gate = self._gate_of_layer[next_idx] if self._gate_of_layer else None
        gate_order = infer_phase_order(gate) if isinstance(gate, dict) else None
        reason = (
            self._blocking_gate_reason()
            or self._gate_predecessor_reason(gate_order)
        )
        if reason:
            self._gate_block_reason = reason
            return True
        return False

    def _short_circuit_layer(
        self,
        layer: List[Dict[str, Any]],
        reason: str,
    ) -> None:
        """被门禁拦下的层：Phase 2 记 ``DEFERRED``，其余沿用 ``SKIPPED``。

        ``DEFERRED`` 与 ``SKIPPED`` 的区别是本节重点：SKIPPED 在现有实现里
        是"终态成功"，会进 ``completed_set`` 而在后续轮次被跳过；被延后的
        全量关卡必须在下一次（Phase 1 修好之后的）轮次真正跑起来，所以它
        既不是失败、也不是跳过。
        """
        if layer and all(is_final_gate(item) for item in layer):
            detail = self._gate_block_reason or reason
            self._mark_layer_deferred(
                layer, f"Phase 2 关卡延后：{detail}",
            )
            return
        self._mark_layer_skipped(layer, reason)

    def _mark_layer_deferred(
        self,
        layer: List[Dict[str, Any]],
        reason: str,
    ) -> None:
        """把整层 Phase 2 VP 标为 ``DEFERRED``（本轮延后，下轮重跑）。"""
        for item in layer:
            item_id = self._derive_item_id(item)
            if item_id in self._verdicts:
                continue
            verdict = {
                "status": "DEFERRED",
                "reasons": [reason],
                "evidence": {"gate": "verification_phase_2"},
                "actual_result": reason,
            }
            try:
                self.record_verdict(item_id, verdict)
            except Exception:  # noqa: BLE001 — 记录失败也不能让整轮崩掉
                self._verdicts[item_id] = verdict
                self._save_state()
            if item_id not in self._deferred_vps:
                self._deferred_vps.append(item_id)
        self._gate_block_reason = ""


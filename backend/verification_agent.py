"""
Verification Agent — Three-Phase Quality Verification
======================================================

Analyzes PRD/architecture/test documents and executes verification
through automated tests, code review, and UI validation.

Phases:
1. Planning — Generate verification plan JSON with verification points
2. Execution — Run pytest, code review, puppeteer UI tests, API boundary tests
3. Judgment — Generate verification report with pass/fail status
"""

import asyncio
import hashlib
import json
import logging

logger = logging.getLogger(__name__)
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass
# 2026-10-26 (merge): main 的 ``_service_recovery_locks`` / ``_recover_service_port``
# 用到 ``threading.Lock``，本分支用到 ``uuid`` / ``dataclass`` —— 两边都要。
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any, Set, Tuple
from urllib.parse import urlsplit, urlunsplit

from coding_tool import (
    ClaudeCodingTool,
    CodingTool,
    ApiError,
    HardTimeoutError,
    ProviderSlotWaitExpired,
    is_deterministic_local_error,
)
from config_paths import resolve_plans_dir
import provider_capacity
from verification_config import TimeoutPolicy
from verification_executor import VerificationExecutor
from verification_persistence import VerificationPersistenceManager
from verification_profile import ExecutionProfileGenerator
from verification_split import SplitDecision
from verification_subagent import (
    DEFAULT_FAILURE_CLASS,
    DEFAULT_FAILURE_CLASS_SOURCE,
    VerificationSubAgent,
    Verdict,
)
from verification_buckets import blocked_reason
from binary_freshness import (
    FreshnessReport,
    FreshnessCache,
    check_binary_freshness,
    rebuild_binary,
)
from prompts import (  # noqa: E402 — canonical home for the framework-shared
    # inline prompt constants (Phase C2, hoisted out of this module's
    # module top). Importing from the new ``prompts`` module instead
    # of defining the strings inline here is a single-source-of-truth
    # measure: the planning and judgment prompts are read directly by
    # :meth:`VerificationAgent.generate_verification_plan` and
    # :meth:`VerificationAgent.generate_verification_report`, and
    # keeping them in one place stops drift between the call sites.
    VERIFICATION_COMMAND_AUDIT_SYSTEM_PROMPT,
    VERIFICATION_JUDGMENT_SYSTEM_PROMPT,
    VERIFICATION_PLAN_SYSTEM_PROMPT,
)
# 2026-09-15: the bounded-command guard. It reads the same
# ``GENERATION_RULES`` constant the planning prompt interpolates, so the
# rule text the LLM sees and the rule the code enforces cannot drift.
from verification_command_guard import (
    annotate_plan as _annotate_command_plan,
    needs_semantic_review as _needs_semantic_review,
)
from verification_phases import (  # noqa: E402 — 两阶段验证（2026-09-16）
    normalize_plan_phases,
    is_final_gate as _is_final_gate,
)
# 2026-09-18（D5）：全量关卡完整性护栏。D2/D4 补回了 Phase 2 两种关卡的
# 表达方式与提示词；这个护栏是"提示词说了但没人核对"的那层兜底 ——
# 项目里有 CI / E2E 入口却没有对应关卡时，把缺口喂回生成回路。
from verification_plan_completeness import (  # noqa: E402
    annotate_plan as _annotate_gate_completeness,
    detect_full_gate_entries,
    find_missing_gates,
    render_gap_feedback,
)
from verification_ac_coverage import (  # noqa: E402
    annotate_acceptance_gap as _annotate_acceptance_gap,
    find_uncovered_criteria,
    render_gap_feedback as render_acceptance_gap_feedback,
)
# 2026-10-25（验证分支输入路径）：分支判定只有一个判据 ——
# plan 目录里躺着的是验证计划书还是任务清单。判定收敛在
# ``plan_artifacts`` 里（纯函数、只读目录、每次重算），这里只消费结果。
from plan_artifacts import is_verification_branch  # noqa: E402
# 2026-10-23（工作树指纹）：依据核对读的是**活的**工作树。多个验证进程
# 可以共用同一个 worktree，读取 citations/checkpoints 期间可能有并发写入
# （"变异负控"）——那时核对读到的是一棵不属于这个 VP 的树。指纹让"树动过
# 了"这件事可判定。
#
# 模块级导入是刻意的：护栏要在 ``_apply_evidence_check`` 里对照本轮基线，
# 而测试要能 monkeypatch ``verification_agent.tree_mutated``。
#
# ``apply_to_verdict`` 同样在模块级绑定：轮次结束时要把挂起的核对重跑一遍，
# 那一步要能被打桩 —— 而打桩的位置有两个合理的选择（源模块
# ``verification_evidence`` 或本模块），见 ``_resolve_apply_to_verdict``。
from verification_evidence import (  # noqa: E402
    apply_to_verdict,
    tree_fingerprint,
    tree_mutated,
)

#: 导入那一刻的 ``apply_to_verdict``。留着它才能分辨"源模块被打桩了"和
#: "本模块的全局被打桩了"——两者都是合法的打桩位置。
_ORIGINAL_APPLY_TO_VERDICT = apply_to_verdict


def _resolve_apply_to_verdict():
    """The callable that re-runs an evidence check, resolved at call time.

    Resolving at call time (instead of binding once at import) is what
    lets a test stub the check through *either* module: the source
    (``verification_evidence.apply_to_verdict``) or this one
    (``verification_agent.apply_to_verdict``).  A stub on the source
    wins — that is where the real implementation lives — and a stub on
    this module is used when the source is untouched.

    Returns ``None`` when the source module cannot be imported at all;
    the caller then skips the check rather than guessing.
    """
    try:
        import verification_evidence
        live = getattr(verification_evidence, "apply_to_verdict", None)
    except Exception:  # pragma: no cover - defensive
        live = None
    if live is not None and live is not _ORIGINAL_APPLY_TO_VERDICT:
        return live
    return apply_to_verdict


#: The machine-readable signal a verification point's **missing definition**
#: carries (2026-10-24, VP-031).
#:
#: It is the name ``verification.failure_triage`` maps to
#: ``criterion_defect``: a verification point with an id but no criterion is
#: a broken criterion, not a broken deliverable. So it escalates to a human
#: and it does **not** become a repair task — reworking the deliverable can
#: never satisfy a criterion that was never written down.
VP_DEFINITION_MISSING_SIGNAL = "vp_definition_missing"


def vp_dispatch_gaps(
    vp: Any,
    definitions: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[str]:
    """Why this verification point must not be dispatched (empty = may dispatch).

    A verification point is judged against a **declared criterion**: an id, a
    title that says what is being asserted, and a method the framework can
    run.  A node that carries an id but none of those is not a verification
    point — it is a placeholder.  Dispatching it would have the framework
    **invent** a criterion: the plan-to-executor conversion defaults a
    missing method to ``code_review`` (see
    :meth:`Orchestrator._convert_plan_to_executor_schema`), so the empty
    shell would be sent to the coding tool, and a review of nothing produces
    nothing worth recording.

    That is exactly what happened to ``VP-031`` (2026-10-24): the round was
    cancelled while the dispatch was in flight, and the attempt was filed as
    FAILED — a broken criterion counted as a broken deliverable, and it
    produced a repair task for a criterion that never existed.

    ``definitions`` is the plan file's ``{vp_id: entry}`` map, and it is the
    **authority**: the id must be one the plan declares, and the plan's own
    entry is what says whether the criterion is complete.  ``None`` means
    "there is no plan on disk" — nothing to compare against, so every point
    would look undefined and the caller stands the guard down.

    The gaps are returned, never raised: the caller turns them into a BLOCKED
    verdict that names what is missing.
    """
    if not isinstance(vp, dict):
        return ["verification point is not an object"]
    gaps: List[str] = []
    vp_id = str(vp.get("id") or "").strip()
    if not vp_id:
        # Without an id there is nothing to look up — the point is not
        # a verification point at all, whatever else it carries.
        return ["verification point has no id"]
    if definitions is None:
        return gaps
    entry = definitions.get(vp_id)
    if entry is None:
        gaps.append(
            f"{vp_id} is not defined in verification_plan.json's "
            f"verification_points"
        )
        return gaps
    from verification_plan_delta import vp_definition_gaps
    gaps.extend(vp_definition_gaps(entry))
    return gaps


class ApiTestSchemaViolation(RuntimeError):
    """A plan whose api_test VPs cannot be graded by the deterministic
    runner (a ``verification_api_runner`` violation) is not safe to
    persist or execute.

    Raised by :meth:`VerificationAgent.generate_verification_plan` both
    after the retry budget is exhausted with the same defect still in
    the plan, and on the disk-load path when the persisted plan was
    hand-edited or written by a runner whose vocabulary drifted from the
    canonical one.

    Why this is a hard gate rather than the "keep the least-bad plan"
    fallback the other violation families use: a service-reference
    violation or a missing Phase-2 gate still yields a *runnable* plan,
    whereas an ``api_test`` VP the runner cannot parse is guaranteed
    FAILED no matter what the live service does. Falling back would
    launder a plan defect into what looks like a product failure — the
    2026-09-26 plan's VP-007 sent no request at all for four consecutive
    rounds that way.

    Carries a structured ``violations`` list so callers and tests can
    inspect the offending VPs and their schema issues without re-walking
    the plan.
    """

    def __init__(
        self,
        message: str,
        *,
        violations: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.violations: List[Dict[str, Any]] = list(violations or [])


class VerificationBranchPlanError(RuntimeError):
    """验证分支的机器计划读不出来，或结构上不是一份计划。

    验证分支的验证点集合**不是**模型生成的：它是人工审核过的验证计划书
    （十章节契约）翻译出来的机器格式 JSON，并且在 ``/start`` 管线里与计划书
    逐条比对过（等价门禁）。所以验证回路对它的职责只有一件 ——
    **读出来并校验**。

    读不出来时唯一正确的动作是停下来报错。退回 LLM 重新生成等于把审核过
    的那批验证点换成一批谁都没审过的：报告里"验了什么"从此与审核时说的不
    是同一件事，而没有任何一步会告诉操作者这件事发生过。

    与 :class:`ApiTestSchemaViolation` 的分工：那一条管的是「计划读出来了，
    但里面的 ``api_test`` 验证点运行器认不出来」；这一条管的是「计划本身
    读不出来 / 不是一份计划」。
    """


# Boundary: the parent ``VerificationAgent`` does NOT directly import
# the ``subprocess`` module or invoke the non-JSON coding tool
# surface. Per-VP work is delegated to :class:`VerificationSubAgent.run`,
# which builds a temp settings.json with the 8-hook security net and
# drives the LLM inside its own self-heal loop. The parent only
# speaks the JSON-coding-tool surface for plan/report generation.


#: The scene every verification sub-agent dispatches through. Read from
#: one place so the width this module derives and the chain the
#: sub-agent walks are the same chain, and so a future scene change
#: cannot move one without the other.
VERIFICATION_SCENE = "verification"


def _verification_vp_capacity(
    scene: str = VERIFICATION_SCENE,
) -> Optional[int]:
    """How many VPs the verification scene's providers can serve right now.

    Derived, never written down. It is the sum of what each provider in
    ``scene``'s own chain can take — the operator's declared cap for that
    provider, scaled by the 5-hour budget it has left — which is the same
    arithmetic execution uses for its own pools and is read from the same
    ``quota_map`` (:func:`dynamic_provider_concurrency.fleet_capacity`).

    So it tracks the fleet. One provider declaring ten yields ten; two
    yielding ten each yield twenty; one down to 40% of its window yields
    four; one that has been shut loses its share rather than keeping a
    reservation, and when the whole chain is shut the answer is ``0`` —
    not the old figure, because ``0`` is what the meters say and any
    other number here would be the framework overruling them.

    ``None`` means *no bound is derivable*, which is a different fact
    from ``0``: it says the scene's chain could not be resolved at all —
    CC Switch unreachable, no tier matching, no live provider rows. That
    is a configuration failure the dispatch path already reports loudly
    (``SceneProviderUnavailable``), so it must not also be turned into a
    gate that admits nobody: a misrouted scene then hangs the plan
    silently instead of failing with the reason.

    Scope note (2026-10-13): the live verification round no longer calls
    this. ``run_full_verification`` builds no width at all — the provider
    slot pool decides per dispatch — so nothing derives a total to be a
    ceiling. What remains here serves the legacy per-group path
    (:meth:`Orchestrator.execute_verification_plan_async` →
    :meth:`Orchestrator._execute_group`), which is production-dead but
    still exercised by tests and e2e. Deleting that path was out of
    scope; deleting this reader with it would have taken the tests with
    it.

    Every read degrades rather than raises — an unreadable quota payload
    leaves every provider selectable (fail-open, the dispatch path's own
    rule), and an unresolvable chain yields ``None``.
    """
    try:
        from provider_routing import resolve_provider_chain
        chain = list(resolve_provider_chain(scene) or [])
    except Exception:
        chain = []
    if not chain:
        return None

    from dynamic_provider_concurrency import (
        fleet_capacity,
        get_shared_cooldown,
    )
    from provider_order import load_provider_5h_usage, load_provider_quota_map

    try:
        quota_map = load_provider_quota_map()
    except Exception:
        quota_map = {}
    try:
        usage_map = load_provider_5h_usage()
    except Exception:
        usage_map = {}

    try:
        return fleet_capacity(
            chain, usage_map, quota_map, cooldown=get_shared_cooldown()
        ).total
    except Exception:
        # A derivation that raises is not a width of zero, and must not
        # become one: it means "no bound derivable", i.e. the fallback.
        return None


class _VpCapacityGate:
    """A bound whose width is read from somewhere live, every time it binds.

    .. warning::
       **Not used by the live verification round** (2026-10-13). The live
       round builds no width and no gate: ``run_full_verification`` calls
       ``executor.run(max_parallel=0)`` and lets
       :func:`dynamic_provider_concurrency.acquire_provider_with_dynamic_capacity`
       decide per dispatch. This class survives only for the legacy
       per-group path (:meth:`Orchestrator._execute_group`), which is
       production-dead but still exercised by tests and e2e; deleting
       that path was out of scope for this change.

    ``asyncio.Semaphore`` takes its width once, at construction, and
    holds it for the life of the run. That is the right shape for a
    number an operator typed and the wrong shape for a number that
    describes what the providers are serving this second: a window that
    closes mid-round shrinks the fleet, and a window that opens grows
    it, and neither can be seen by an object that stopped asking.

    The queueing is the same as a semaphore's — one waiter admitted per
    release — with one addition. A waiter that is not released within
    ``recheck`` goes and asks the one process-wide quota watchdog for a
    fresh reading, because there is a kind of "full" no release will fix:
    every provider shut. That ask is single-flighted, so N verification
    points queued on the same closed window cost one request between
    them, the same way N dispatches do.

    ``width`` returning ``None`` means "nothing derivable" and the
    ``fallback`` decides; ``fallback`` returning ``None`` means no bound
    at all, which is how the plan-wide layer is optional.
    """

    def __init__(
        self,
        width: Callable[[], Optional[int]],
        fallback: Optional[Callable[[], Optional[int]]] = None,
        *,
        recheck_sec: Optional[float] = None,
    ) -> None:
        self._width = width
        self._fallback = fallback
        self._recheck_sec = recheck_sec
        self._in_flight = 0
        self._waiting = 0
        self._changed: Optional[asyncio.Event] = None

    # ----- the width --------------------------------------------------

    def capacity(self) -> Optional[int]:
        """The width right now; ``None`` means unbounded."""
        for reader in (self._width, self._fallback):
            if reader is None:
                continue
            try:
                value = reader()
            except Exception:
                continue
            if value is not None:
                return max(0, int(value))
        return None

    def _recheck_interval(self) -> float:
        if self._recheck_sec is not None:
            return self._recheck_sec
        from quota_watchdog import probe_interval_sec

        return probe_interval_sec()

    # ----- the queue --------------------------------------------------

    def _event(self) -> asyncio.Event:
        if self._changed is None:
            self._changed = asyncio.Event()
        return self._changed

    async def acquire(self) -> None:
        while True:
            event = self._event()
            # Cleared *before* the width is read, never after. A release
            # landing between the read and the clear would be erased by
            # the clear, and the waiter would sit out a full recheck on a
            # slot that had already come free.
            event.clear()
            width = self.capacity()
            if width is None or self._in_flight < width:
                self._in_flight += 1
                return

            self._waiting += 1
            try:
                try:
                    await asyncio.wait_for(
                        event.wait(), timeout=self._recheck_interval()
                    )
                    # A slot came back. Go round and take it; re-reading
                    # the width is what tells a wider fleet from a freed
                    # slot, and the two are the same case to a waiter.
                    continue
                except asyncio.TimeoutError:
                    pass
                # Nothing was released for the whole interval and there
                # is still no room, so the answer is not going to arrive
                # from a release: the fleet itself has nothing to give.
                # Ask the one watchdog — shared, so the whole queue asks
                # once between them.
                await self._refresh()
            finally:
                self._waiting -= 1

    def release(self) -> None:
        if self._in_flight > 0:
            self._in_flight -= 1
        if self._waiting:
            # One wake-up per release, like a semaphore: the waiter that
            # takes it re-reads the width itself, so waking more than one
            # would only repeat that read.
            self._event().set()

    async def _refresh(self) -> None:
        """Go and read the fleet again, on a worker thread.

        The read is a blocking HTTP call — ``urlopen`` — so asking for it
        inline would stall every other coroutine in the round for the
        length of the request: the other verification points, the
        orchestrator, the whole loop. Worse, it would stall them out of
        reaching the watchdog at the same moment, which is precisely the
        overlap the merge needs to collapse N asks into one. Handed to a
        thread, the ask joins whatever the dispatch workers are already
        doing against the same single reader.

        Never raises. A watchdog that cannot be reached costs this
        waiter one interval and nothing else.
        """
        try:
            from quota_watchdog import get_shared_quota_watchdog

            await asyncio.to_thread(get_shared_quota_watchdog().probe)
        except Exception:
            # Nothing to do but try again on the next interval; a
            # watchdog that cannot be reached must not take the queue
            # down with it.
            pass

    # ----- introspection ----------------------------------------------

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def waiting(self) -> int:
        return self._waiting


def _plan_wide_vp_cap() -> Optional[int]:
    """Declared fleet ceiling on concurrently-running VPs, or ``None``.

    The plan-wide bound, read from what the operator *declared*: the sum
    of the per-provider caps in ``provider_capacity.yaml``
    (:func:`provider_capacity.configured_capacity_ceiling`).

    Its role changed when the per-group bound stopped being a written
    number. It used to be the only derived bound in the system, and it
    sat *under* a per-group ``parallelism_cap`` of four — so on a machine
    with one provider declaring twenty slots, the plan could run twenty
    verification points at once and the bound said nothing about it,
    while the per-group figure held the whole plan to four. That is the
    bottleneck this order was asked to remove, and leaving it in place
    would have rebuilt it one layer up: a declared ceiling is a sum over
    *rules*, which over-counts when two rules match one provider and
    under-counts a provider nobody wrote a rule for, so it is not the
    fleet the dispatch path will actually use.

    So it is the fallback, and only that, on the legacy per-group path.
    While a bound is derivable from the live scene
    (:func:`_verification_vp_capacity`) that number is both the plan's
    width and each group's — the same figure at two layers, which is
    what keeps the plan bound from becoming the new ceiling the
    per-group one just stopped being. This function answers for the case
    where nothing is derivable: the chain could not be resolved, so the
    declared capacity is the only fleet size on record.

    ``None`` when no capacity is configured either. Then there is no
    declared fleet size at all, and inventing one would put the same
    kind of hand-written number back — so the gate it feeds admits
    everyone, and the provider slot pool is what bounds the run. That is
    the same answer the live round gives unconditionally.

    2026-10-13: the operator's own ``parallelism_cap`` used to stand
    behind this one as a second fallback. It is gone, so an unresolvable
    chain on this legacy path now falls through to ``None`` (unbounded)
    rather than to a typed-in four. The live round was unaffected either
    way — it consults no width at all.
    """
    ceiling = provider_capacity.configured_capacity_ceiling()
    return ceiling if ceiling > 0 else None


def _plan_wide_bound(width: Optional[Callable[[], Optional[int]]] = None):
    """The plan-wide bound, as a gate that re-reads its width.

    A gate sized by the live scene (:func:`_verification_vp_capacity`)
    whenever the scene can be resolved, and by the declared ceiling
    (:func:`_plan_wide_vp_cap`) when it cannot. A width of ``None``
    everywhere means no plan-wide bound at all — the gate then admits
    every VP, which is the same as the shared no-op it replaced and needs
    no special case at the call site.
    """
    return _VpCapacityGate(
        width or _verification_vp_capacity,
        _plan_wide_vp_cap,
    )


# ---------------------------------------------------------------------------
# Verification Agent
# ---------------------------------------------------------------------------


def _log_phase_distribution(context: str, summary: dict) -> None:
    """打印两阶段分布（可审计）。

    ``missing_phase_order`` 是**审计线索**，不是分类依据：Phase 2 的 VP 若
    没写 ``phase_order``，顺序退回计划顺序，这时把 id 打出来，方便回溯
    门禁执行顺序究竟来自模型还是计划。阶段判定本身不做任何字符串推断。
    """
    print(
        f"[Verification] 阶段分布（{context}）："
        f"Phase 1 = {summary.get('phase1', 0)}, "
        f"Phase 2 = {summary.get('phase2', 0)}"
    )
    for gate in summary.get("gates", []):
        print(f"    · 关卡 {gate.get('id')}（phase_order="
              f"{gate.get('phase_order')}）")
    missing = summary.get("missing_phase_order") or []
    if missing:
        print(f"    ! 未标注 phase_order 的关卡：{', '.join(map(str, missing))}"
              "（按计划顺序执行）")


# 2026-10-11: cap on evidence handed to the Phase 3 judgment LLM.
# VP-016 round 4 shipped 7141 chars of evidence and the old 500-char
# cap cut it mid-sentence right before the conclusion the reporter
# needed. 8000 comfortably covers the observed worst case; anything
# beyond it is clipped — but *declared*, see ``_truncate_annotated``.
_MAX_PROMPT_EVIDENCE_CHARS = 8000


def _truncate_annotated(text: str, limit: int, label: str = "evidence") -> str:
    """Clip ``text`` to ``limit`` chars, declaring the cut when one happens.

    The point of the declaration is that a silent slice is
    indistinguishable from the whole thing: the reader cannot tell
    "that is all there was" from "that is the first N chars", so an
    LLM downstream reasons about a truncated fragment as if it were a
    complete argument and reports "I could not verify the FAILED
    conclusion because the text was cut" — the round then closes FAILED
    over a sentence about the harness's own output rather than about the
    code under test. Losing the tail is acceptable; losing the *fact*
    that the tail is missing is not, so the annotation states the
    original length too.
    """
    if len(text) <= limit:
        return text
    notice = (
        f"…[{label} 已截断，原长 {len(text)} 字符，"
        f"此处保留前 {limit}]"
    )
    return text[:limit] + notice


def _render_evidence_for_prompt(
    evidence: Any, limit: int = _MAX_PROMPT_EVIDENCE_CHARS
) -> str:
    """Render a VP ``evidence`` value as readable prompt text, bounded.

    ``evidence`` arrives in two shapes:

    * ``str`` — the legacy / executor path, already flattened;
    * ``dict`` — the sub-agent path, ``{"details": str, "raw": list[str]}``
      (see :meth:`Orchestrator._verdict_to_result`).

    ``str(dict)`` on the second shape renders a Python repr — escaped
    quotes, a leading ``{``, a trailing ``}'`` glued to the end of the
    text — which reads badly and is easy for a reporter to mis-parse.
    Here ``raw`` is listed one entry per line (those entries are the
    actual observations), falling back to ``details`` when ``raw`` is
    absent or empty. A ``list`` is joined the same way.

    The result is then passed through :func:`_truncate_annotated`, so
    the bound applies *after* rendering and the annotation reports the
    post-render length — the number the reader actually has to reason
    about.
    """
    rendered: str
    if isinstance(evidence, dict):
        raw = evidence.get("raw")
        if isinstance(raw, (list, tuple)) and raw:
            rendered = "\n".join(str(e) for e in raw)
        else:
            rendered = str(evidence.get("details") or "")
    elif isinstance(evidence, (list, tuple)):
        rendered = "\n".join(str(e) for e in evidence) if evidence else ""
    else:
        rendered = str(evidence or "")
    return _truncate_annotated(rendered, limit)


# ---------------------------------------------------------------------------
# Which ``service_manager`` does the spawn come from? (2026-10-24)
# ---------------------------------------------------------------------------


#: The two entry points the declared-services preflight needs from a
#: ``service_manager`` module.  A project module that does not offer both
#: is not usable here, and the loader falls back to the harness' copy.
_SERVICE_MANAGER_ENTRY_POINTS = ("ensure_services", "record_runtimes")


def _project_service_manager_path(project_dir: Any) -> Optional[Path]:
    """The project's own ``service_manager``, if it ships one.

    ``<project_dir>/backend/service_manager.py`` — the layout every
    ``backend/*.py`` module of this project family uses.
    """
    if not project_dir:
        return None
    try:
        candidate = Path(project_dir) / "backend" / "service_manager.py"
    except TypeError:
        return None
    return candidate if candidate.is_file() else None


def _load_service_manager(project_dir: Any = None) -> Any:
    """The ``service_manager`` module that belongs to *project_dir*.

    Why this exists
    ---------------
    A declared service's ``start_cmd`` runs the *project's* own code
    (``python -m backend.server``, with ``cwd`` inside the project), so
    the project is what a verification round is about.  The machinery
    that launches that code has to come from the same place: while the
    preflight called the **harness' own** ``service_manager``, a project
    that had fixed its spawn shape was still verified through the
    backend's *old* one — and the round failed for reasons that had
    nothing to do with the project.

    Resolution order:

      1. ``<project_dir>/backend/service_manager.py`` — the project's
         own module, loaded **from its path** (never from ``sys.path``,
         so the harness' copy cannot shadow it).
      2. The harness' own ``service_manager`` — the fallback, used when
         the project ships no such module or when loading it fails.

    The boundary this leaves behind
    -------------------------------
    The project's module does *flat* imports (``from credentials import
    ...``, ``from service_declaration import ...``, ``from
    service_freshness import ...``) — the convention every ``backend/*.py``
    module follows, and the reason ``backend/`` sits on ``sys.path``.
    Those names therefore resolve to the **harness'** copies, not to the
    project's.  That is acceptable as long as the shared constants stay
    in step (``credentials._FD_ENV_PREFIX``, ``service_freshness.
    PROTECTED_PORTS`` and the rest carry the same values in both trees),
    and it is spelled out here so it does not quietly become an implicit
    contract: if a project ever needs its *own* ``credentials``, the
    flat-import convention has to change first.

    Never raises — a project that cannot be loaded falls back to the
    harness' module rather than taking the round down.
    """
    path = _project_service_manager_path(project_dir)
    if path is None:
        import service_manager

        return service_manager

    try:
        import hashlib
        import importlib.util

        # A name that is not ``service_manager``: ``sys.modules`` already
        # holds the harness' module under that name, and shadowing it
        # would swap the harness' spawn machinery for the project's —
        # the exact confusion this loader exists to prevent.  The digest
        # keeps two projects apart when both are loaded in one process.
        digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
        module_name = f"pdt_project_service_manager_{digest}"
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"no loader for {path}")
        module = importlib.util.module_from_spec(spec)
        # Registered while it executes, so a module that looks itself up
        # by ``__name__`` finds itself; removed again afterwards, so the
        # registry keeps describing the harness' own modules.
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(module_name, None)
        missing = [
            name for name in _SERVICE_MANAGER_ENTRY_POINTS
            if not hasattr(module, name)
        ]
        if missing:
            raise AttributeError(
                f"{path} does not offer {', '.join(missing)}"
            )
        return module
    except Exception as exc:  # noqa: BLE001 — a fallback, never a failure
        logger.warning(
            "[service_freshness] could not load the project's own "
            "service_manager (%s); falling back to the harness' copy: %s",
            path, exc,
        )
        import service_manager

        return service_manager


# ---------------------------------------------------------------------------
# The round's service binding (2026-10-25, repair-r1-22)
# ---------------------------------------------------------------------------
#
# "Where does a request go" is decided **once**, by the ``ensure`` that
# produced the instance, and it is decided by an **object** — not by
# re-reading a map every time a plan is resolved.
#
# The distinction matters because the map is a *record*: anything that
# merges into it may rewrite it, and a rewritten map moves the address
# under a VP that is talking to a perfectly healthy instance.  The
# 2026-10-24 production round is what that looks like: the ensure
# produced port 58396, and the address a VP was later dispatched to
# was one the round had never verified.  Nothing in the log said how
# the address got there.
#
# So the round keeps a binding per service — the runtime object the
# ensure returned — and every VP's request is built from it.  The map
# is still written (it is the record of what happened); it just no
# longer decides where a request goes.


@dataclass
class _BoundService:
    """The service instance a declared service is bound to this round.

    ``runtime`` is the object the ``ensure`` returned — the binding is
    to *that instance*, not to a port number that some later step may
    re-derive.
    """

    name: str
    #: The port the bound instance listens on.
    port: int
    #: The pid behind it — the same port can be a different process
    #: after a restart, and "which process" is half of the identity.
    pid: Optional[int] = None
    #: The health URL of the bound instance.
    health_url: str = ""
    #: The **boot identity** of the instance: pid + process start
    #: epoch.  A pid alone cannot tell two runs of the same pid apart,
    #: and "the service was restarted" is exactly the question a log
    #: reader has to be able to answer.
    boot_id: str = ""
    #: The identity of the binding record itself.
    runtime_id: str = ""
    #: The runtime object the ensure returned.
    runtime: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service": self.name,
            "port": self.port,
            "pid": self.pid,
            "health_url": self.health_url,
            "boot_id": self.boot_id,
            "runtime_id": self.runtime_id,
        }


#: The hosts a locally started service listens on.  Only these are
#: retargeted: a URL that names another host is not this service's
#: address to rewrite.
_LOCAL_SERVICE_HOSTS = frozenset({"127.0.0.1", "localhost", "0.0.0.0", "::1"})


def _url_port(url: str) -> Optional[int]:
    """The port a URL names, or ``None`` when it names none."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    try:
        return parts.port
    except ValueError:
        return None


def _retarget_url_port(url: str, port: int) -> Optional[str]:
    """The same URL, pointed at *port*.

    ``None`` when the URL is not a local address — retargeting a
    remote URL would send the request to a host the round never
    started.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host not in _LOCAL_SERVICE_HOSTS:
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    userinfo = ""
    if "@" in parts.netloc:
        userinfo = parts.netloc.rsplit("@", 1)[0] + "@"
    netloc = f"{userinfo}{host}:{int(port)}"
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


# ---------------------------------------------------------------------------
# The address every VP's evidence records (2026-10-25, repair-r1-31)
# ---------------------------------------------------------------------------
#
# The production round pointed its ``api_test`` VPs at an address, and
# the evidence said exactly one thing about it: ``Errno 61``.  That is
# a transport's complaint — it is not an address, and it cannot answer
# the two questions a reader has:
#
#   * **which port** did the request go to?
#   * **which instance** was listening on that port — the one this
#     round's ``ensure`` started, or a stranger that took the port
#     behind the round's back?
#
# Without those two answers the round cannot tell a **product** defect
# from an **environment** defect.  The same ``Errno 61`` means "the
# service is down" when the port was the round's own instance, and
# "the request never reached the code under test" when it was not —
# and the report cannot tell those apart, so the repair loop repairs
# the wrong thing.
#
# So every VP's recorded evidence carries the address it was sent to.
# Two identities are recorded, and they are **not** the same question:
#
#   * ``boot_id`` — the boot identity of the instance the round's
#     ``ensure`` bound.  It is the expectation.
#   * ``answering_boot_id`` — the boot identity the *responder* says
#     it has (``/health/data``).  It is the observation.  When the
#     two differ, somebody else's instance answered.
#
# ``request_id`` is the round's own id: every VP of one round carries
# the same one, so "were these twelve failures the same address?" is
# a question the evidence can answer on its own.

# ---------------------------------------------------------------------------
# The instance behind the address (2026-10-26)
# ---------------------------------------------------------------------------
#
# The 02:25 round asked a port for a service and was refused; the port
# had been handed over to another instance while the round was running,
# and the round had no way to say so — it knew the address it aimed at
# and nothing about **who** was answering there.  The same round's
# ``ui_validation`` point was served on that very address minutes
# later, so the address was alive and the round's own reading of it
# was stale.
#
# The fix is a snapshot on **both** sides of every dispatch: the
# instance that was serving the address before the request, and the
# one serving it after.  A snapshot is four facts the service publishes
# about itself, so two snapshots can be compared field by field.

#: The four facts that identify the instance serving an address.  They
#: come from the ``instance`` block the service publishes about
#: itself, so they are that process's own statements, not the round's
#: guesses.
SERVING_SNAPSHOT_FIELDS = ("pid", "boot_id", "plans_dir", "state_db_inode")

#: The evidence names for the two sides of one dispatch.  They are part
#: of the address contract, so every VP's evidence carries them — a
#: field that is absent reads as "nobody looked".
SERVING_BEFORE_FIELD = "serving_before"
SERVING_AFTER_FIELD = "serving_after"

#: The round-level event: which instance served the round, and which
#: one is serving it now.  It is written to the plan's own log, so a
#: reader can tell that the round's address belonged to somebody else
#: by the end without reading the round's internals.
ROUND_INSTANCE_HANDOVER_EVENT = "round_instance_handover"

#: The address fields every VP's evidence carries.  Written in this
#: order, and always written — a field that is absent reads as "nobody
#: looked", which is not the same as "there was nothing there".
ADDRESS_EVIDENCE_FIELDS = (
    "requested_url",
    "resolved_port",
    "ensure_port",
    "pid",
    "boot_id",
    "attempt_no",
    "error_kind",
    "errno",
    "request_id",
    "answering_boot_id",
    SERVING_BEFORE_FIELD,
    SERVING_AFTER_FIELD,
)


def _instance_state_db_inode(instance: Any) -> Optional[int]:
    """The inode of the database the answering instance reads.

    The instance publishes the inode of **its own** database, and that
    is the authoritative number: the path it names may live somewhere
    this process cannot stat (another container, another mount).  The
    published inode is preferred; the path is only stat'ed when the
    instance did not publish one.
    """
    if not isinstance(instance, dict):
        return None
    published = instance.get("state_db_inode")
    if isinstance(published, int) and not isinstance(published, bool):
        return int(published)
    reported = str(instance.get("state_db_path") or "")
    if not reported:
        return None
    try:
        return int(Path(reported).stat().st_ino)
    except OSError:
        return None


def _serving_snapshot(instance: Any) -> Optional[Dict[str, Any]]:
    """The snapshot of the instance serving an address.

    ``None`` when the service does not identify itself: there is no
    instance to snapshot, and "nobody said" is not the same as
    "somebody else said".
    """
    if not isinstance(instance, dict):
        return None
    values = {
        "pid": instance.get("pid"),
        "boot_id": str(instance.get("boot_id") or ""),
        "plans_dir": str(instance.get("plans_dir") or ""),
        "state_db_inode": _instance_state_db_inode(instance),
    }
    # The contract's field list, in the contract's order — so two
    # snapshots can be compared field by field, and a reader that knows
    # the list finds every field where it expects it.
    return {field: values.get(field) for field in SERVING_SNAPSHOT_FIELDS}


def _serving_handover(
    before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]],
) -> bool:
    """Did the instance serving this address change hands?

    Three answers, and the third one is the careful one:

      * **both sides identify an instance** — the handover happened
        when the two snapshots differ;
      * **exactly one side identifies an instance** — the handover
        happened too: the instance either appeared or disappeared, and
        a request that crossed that boundary cannot be attributed to
        the instance the round started;
      * **neither side identifies an instance** — there was no instance
        to have changed hands.  "Not comparable" is not "wrong", and a
        service that publishes no ``instance`` block is never gated.
    """
    if before is None and after is None:
        return False
    if before is None or after is None:
        return True
    return before != after


#: The round's **instance epoch** — the identity of the instance whose
#: answers this round accepts.
#:
#: A round asks **one** instance a question.  When the process behind
#: the round's address dies and somebody else takes the port, the round
#: is no longer asking the instance it started: it is asking a
#: stranger, and the answer it gets is a statement about the stranger's
#: data.  The 2026-10-26 round was graded on exactly that: it never got
#: an HTTP answer at all, so it could not tell whether the criterion it
#: was verifying had been looked at.
#:
#: The epoch is the round's answer to "which instance is that?".  It is
#: established from the first successful read of ``/health/data`` — the
#: same road a verification point's request travels — and it is
#: compared before every dispatch after that.
INSTANCE_EPOCH_EVENT = "instance_epoch"

#: The conflict kind of an epoch change: the round asking a stranger.
#:
#: It is a contradiction of the same kind as a handover, so it is
#: handled by the same rule — the point is not a verdict about the
#: product, and it gets the round's one second look.
EPOCH_CHANGED_CONFLICT_KIND = "instance_epoch_changed"

#: Why a round was blocked: it could not say which instance it was
#: asking.
EPOCH_UNRESOLVED_ERROR_KIND = "instance_epoch_unresolved"


def _instance_epoch_token(instance: Any) -> str:
    """The comparable identity of an instance: its pid and its boot id.

    Two instances are the same instance when this token is the same.  An
    instance that publishes neither has **no** token: "nobody said" is
    not the same as "somebody else said", so such an instance is never
    treated as a different one.
    """
    if not isinstance(instance, dict):
        return ""
    pid = str(instance.get("pid") or "")
    boot_id = str(instance.get("boot_id") or "")
    if not pid and not boot_id:
        return ""
    return f"{pid}@{boot_id}"


def _mint_request_id(plan_id: str, round_number: int) -> str:
    """One id for a whole round — the thread every VP's evidence hangs on.

    The round's number is in it, so a reader can tell the rounds
    apart without the log; the random part is what makes two rounds of
    the same plan distinguishable even when they ran within the same
    second.
    """
    return f"{plan_id}-r{int(round_number)}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# A runtime that names no address (2026-10-25, repair-r1-29)
# ---------------------------------------------------------------------------
#
# ``service_manager._relocate_service`` gives up when it cannot place a
# service anywhere: the declared port is held by a process that is not
# the service, and the ports it tried are gone too.  The runtime it
# hands back still carries the **declared** port — the stranger's — and
# used to say so only in prose (``origin="failed"``).
#
# A reader that asks "is it ready?" and does not read the prose has an
# address in its hand.  So the failure is explicit: the runtime says
# ``unusable``, and every reader here refuses it.  The declared port is
# **not** a fallback for it — falling back to it would aim the round's
# VPs at the process squatting the port, which is how a round ends up
# graded on somebody else's server.


def _runtime_is_unusable(runtime: Any) -> bool:
    """Does the runtime say it names **no** address at all?

    Duck-typed on purpose: the runtime may come from the project's own
    copy of ``service_manager`` (see :func:`_load_service_manager`),
    which may predate the field — and then the runtime simply is not
    marked unusable, which is the behaviour of every earlier round.
    """
    return bool(getattr(runtime, "unusable", False))


def _runtime_is_usable(runtime: Any) -> bool:
    """May the runtime's port be used as an address?

    Only a runtime that is ready, is not a failure, is not marked
    unusable, and carries a port qualifies.  "Not ready yet" does not
    qualify either — but it is a different case, and the two are never
    conflated.
    """
    if runtime is None:
        return False
    if _runtime_is_unusable(runtime):
        return False
    if not getattr(runtime, "ready", False):
        return False
    if str(getattr(runtime, "origin", "") or "") == "failed":
        return False
    return bool(getattr(runtime, "port", None))


def _vp_service_names(vp: Dict[str, Any]) -> Set[str]:
    """The service names a VP names.

    Two ways, and both are honoured: ``uses_services`` (the plan's own
    list) and a ``{{svc.<name>.<field>}}`` placeholder in the VP's text.
    The placeholder is the one that matters for a service that could not
    be placed: the placeholder never resolved, and the unresolved text is
    itself the evidence that the VP needs that service.
    """
    names: Set[str] = set()
    uses = vp.get("uses_services")
    if isinstance(uses, list):
        for name in uses:
            names.add(str(name).lower())
    try:
        from service_declaration import iter_placeholders
    except Exception:  # pragma: no cover - defensive
        return names
    try:
        body = json.dumps(vp, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        body = str(vp)
    for _raw, name, _field in iter_placeholders(body):
        names.add(str(name).lower())
    return names


# ---------------------------------------------------------------------------
# The adoption gates of a re-ensure's result (2026-10-25, repair-r1-24)
# ---------------------------------------------------------------------------
#
# The production round ended with its api_test pointing at
# 127.0.0.1:51725 — a port nothing had ever listened on, and one no log
# had ever recorded as a service's address.  There is only one way that
# address becomes a VP's request address: a re-ensure allocated it, wrote
# it into the runtime map **without a liveness probe**, and the plan's
# placeholders followed it there.
#
# So a re-ensure's result is adopted only after it has been *verified*:
# the runtime has to say the service is ready and carry a port, that port
# has to accept a TCP connection, and the service's health URL has to
# answer 200.  A result that fails any of the three is not adopted — the
# old map stands, the address does not move, and the VPs that need the
# service are BLOCKED (``service_unreachable``) instead of being sent to
# a port nobody serves.

#: How long the gate waits for the TCP connect it makes to the port a
#: re-ensure reported.
_REENSURE_CONNECT_TIMEOUT_SECONDS = 1.0

#: How long the gate waits for the health URL to answer.
_REENSURE_HEALTH_TIMEOUT_SECONDS = 3.0

#: The reason code a VP carries when the round could not reach the
#: service a re-ensure produced.  It is a **code**, not prose: a report
#: reader has to be able to match on it.
SERVICE_UNREACHABLE = "service_unreachable"

#: The reason code a VP carries when the round could not **place** the
#: service the VP needs: the declared port is held by a process that is
#: not the service, and no other port would do.  A **code**, not prose.
SERVICE_UNUSABLE = "service_unusable"


# ---------------------------------------------------------------------------
# The identity of the instance a request is about to reach (2026-10-25)
# ---------------------------------------------------------------------------
#
# "Something is listening on the port" and "the thing listening is the
# instance this round started" are two different questions, and the
# production round could not tell them apart: the address on the wire
# (51725) belonged to an instance the round had never started, and the
# verdict it produced said nothing about the plan it was supposed to
# be about.
#
# A liveness probe cannot answer that — a stranger answers a health URL
# just as well as the right instance does.  So the service is asked
# **who it is**: ``/health/data`` publishes an ``instance`` block about
# the answering process (the boot id minted when it started, the plans
# tree it serves, the state database it reads).  Three facts are
# compared before a request goes out:
#
#   * the **boot id** — the one this round's ``ensure`` recorded.  A
#     different boot id is a different process: something restarted the
#     service behind the round's back, or a stranger took the port;
#   * the **plans tree** — the instance must serve the tree this
#     round's plan lives in.  Another tree is another set of plans;
#   * the **state database's inode** — the same path with another
#     inode is another file, i.e. another data plane.  This is the
#     comparison the production round needed and could not make.
#
# Any of them failing means the request is **not** sent: a verdict
# about somebody else's instance is worse than no verdict at all.  The
# VP is BLOCKED with ``error_kind = service_instance_mismatch`` — never
# FAILED, which would say "the project answered wrong" about a project
# that was never asked — and the round log carries an event of the same
# name, with both sides of every comparison.

#: The ``error_kind`` a VP carries when the instance that answered is
#: **not** the instance this round started.  A **code**, not prose: a
#: report reader has to be able to match on it.
SERVICE_INSTANCE_MISMATCH = "service_instance_mismatch"


# ---------------------------------------------------------------------------
# The classification of a failed request (2026-10-25, repair-r1-26)
# ---------------------------------------------------------------------------
#
# The production round pointed twelve ``api_test`` VPs at an address
# nothing was serving.  All twelve died inside twenty-two seconds with
# the same ``URLError [Errno 61]`` and every one of them was written
# down as **FAILED** — twelve independent contract violations,
# according to the report.  There was exactly one event there: the
# environment was not serving.
#
# So a request that never got an answer is not a verdict about the
# product.  It is a verdict about the plumbing, and it says so:
# ``BLOCKED`` with ``error_kind = service_unreachable``, and the
# evidence keeps the two facts that make the claim checkable — the URL
# that was actually called and the errno the transport reported.
#
# A request that **did** get an answer (2xx or 4xx) keeps the old
# semantics: the assertions decide, and a violated assertion is
# FAILED.  The classification layer never re-judges a contract — it
# only separates "the service was not there" from "the service said
# no".

#: The ``error_kind`` a VP carries when its request never reached the
#: service.  A **code**, not prose: a report reader has to be able to
#: match on it.
ENVIRONMENT_ERROR_KIND = SERVICE_UNREACHABLE

#: The round-level marker for "the environment took this round out".
#: The round is re-run by the upper layer; it is not repaired.
ENVIRONMENT_BLOCKED = "environment_blocked"

#: The event a round writes when the environment blocked it.
ROUND_BLOCKED_EVENT = "verification_round_blocked"

#: The transport failures that mean "the service was never reached".
#: A refused connection, a name that does not resolve, a socket that
#: died before a response — these are statements about the
#: **environment**.  The evidence for each of them is the transport's
#: own words, which is what the api runner leaves in
#: ``evidence["error"]``.
_TRANSPORT_FAILURE_MARKERS = (
    "urlerror",
    "connectionrefused",
    "connection refused",
    "gaierror",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "connectionreseterror",
    "connection reset by peer",
    "remote end closed connection",
    "brokenpipeerror",
)

#: A timeout is **not** one of them, on purpose.  A timeout means the
#: request was sent and no answer came back in time: the service may
#: be up and simply too slow, which can be a real product defect (a
#: slow endpoint).  Hiding it behind "the environment" would silently
#: stop the repair path from ever seeing it.  A timeout therefore keeps
#: the verdict it has always had.
_TIMEOUT_MARKERS = ("timed out", "timeout")


def _errno_from_error(text: str) -> Optional[int]:
    """The errno the transport reported, if it reported one.

    ``[Errno 61]`` → ``61``; ``[Errno -2]`` → ``-2``.  ``None`` when
    the transport named no errno — the evidence then carries the
    message alone, which is still enough to say *what* happened.
    """
    match = re.search(r"Errno\s+(-?\d+)", text or "")
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - the regex only matches digits
        return None


def _is_transport_failure(error_text: str) -> bool:
    """Is *error_text* the transport saying "the service was not there"?"""
    if not error_text:
        return False
    lowered = error_text.lower()
    if any(marker in lowered for marker in _TIMEOUT_MARKERS):
        return False
    return any(marker in lowered for marker in _TRANSPORT_FAILURE_MARKERS)


#: The reason code a VP carries when the service behind the port is
#: serving **another data plane** than the one this round recorded: the
#: instance was replaced, or the state database it reads is another
#: file.  The request is never sent — a verdict about somebody else's
#: data is worse than no verdict — and the block has to say **why** in
#: a code the accounting layer can match on, not in prose.
SERVICE_DATA_PLANE_MISMATCH = "service_data_plane_mismatch"


#: The reason codes that mean "the environment stopped this, not the
#: product".  ``service_unreachable`` is the per-point code the
#: classification layer writes; ``environment_blocked`` is the round's
#: own code, written when one refused connection takes the whole round
#: out; ``service_data_plane_mismatch`` is the per-point code for a
#: service that serves another database than the round's fixture.
ENVIRONMENT_REASON_CODES = frozenset({
    SERVICE_UNREACHABLE, ENVIRONMENT_BLOCKED, SERVICE_DATA_PLANE_MISMATCH,
})


def _result_evidence(result: Any) -> Dict[str, Any]:
    """One result's evidence as a dict.

    The evidence travels as a **string** on the legacy
    ``execution_results`` envelope (the Phase 3 judgment prompt slices
    it), so every reader has to parse it before it can ask a question
    of it.  Evidence that is not JSON is not evidence this module can
    read — it comes back as an empty dict rather than raising.
    """
    if not isinstance(result, dict):
        return {}
    evidence = result.get("evidence")
    if isinstance(evidence, dict):
        return evidence
    if isinstance(evidence, str):
        text = evidence.strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _result_serving_handover(result: Any) -> bool:
    """Did the address change hands during the look that produced *result*?

    The answer is in the result's own evidence — the two snapshots the
    dispatch took around the request.  A result that carries no
    snapshot at all (an older entry, a point that never reached a
    service) has no handover to report.
    """
    evidence = _result_evidence(result)
    before = evidence.get(SERVING_BEFORE_FIELD)
    after = evidence.get(SERVING_AFTER_FIELD)
    if not isinstance(before, dict) and not isinstance(after, dict):
        return False
    return _serving_handover(before, after)


def _environment_block_reason_code(result: Any) -> str:
    """The reason code that says the environment stopped this result.

    The **per-point** code wins over the round's own.  A round that was
    blocked as a whole rewrites every result's ``reason_code`` to
    ``environment_blocked`` — and a point that genuinely failed (the
    product answered and broke the contract) must not be re-read as
    "the environment stopped it" just because it sat in that round.
    ``original_reason_code`` is where the per-point code is kept.
    """
    evidence = _result_evidence(result)
    if "original_reason_code" in evidence:
        code = evidence.get("original_reason_code")
    else:
        code = evidence.get("reason_code") or evidence.get("error_kind")
    return str(code or "")


def _is_environment_blocked_result(result: Any) -> bool:
    """Is this result a BLOCKED entry whose cause is the environment?

    The two halves have to agree: the status says the verdict was not
    a verdict about the product, and the reason code says **why**.
    A BLOCKED entry with no reason code (a stale binary, a retired
    method) is blocked for a different reason and is left alone.
    """
    if not isinstance(result, dict):
        return False
    if str(result.get("status") or "") != "BLOCKED":
        return False
    return _environment_block_reason_code(result) in ENVIRONMENT_REASON_CODES


def _entry_is_environment_blocked(entry: Any) -> bool:
    """Is this entry one the environment took out of the round?

    The **status is not consulted** here, and that is the whole point:
    the accounting layer rewrites a blocked entry's status before the
    round's verdict is derived, so a rule that read the status would
    read a blocked entry as an ordinary skip — and call the round
    green.  Two things say the environment stopped this entry instead:

      * the entry says so itself (``environment_blocked`` is True);
      * its evidence carries a reason code the environment owns.

    This is deliberately **not** :func:`_is_environment_blocked_result`:
    that one insists on the status being ``BLOCKED``, which is exactly
    the condition the accounting layer does not leave standing.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("environment_blocked") is True:
        return True
    evidence = _result_evidence(entry)
    return str(evidence.get("reason_code") or "") in ENVIRONMENT_REASON_CODES


def _derive_overall_status(entries: Any) -> str:
    """The round's verdict, derived from the round's entries.

    **One** derivation, two call sites: where the report is built, and
    where the environment-blocked entries are taken out of the failure
    statistics
    (:meth:`Orchestrator._exclude_environment_blocked_from_failures`).
    Both used to answer the same question with an ad-hoc branch of
    their own, and both read "otherwise" as ``PASSED`` — so a round
    that judged **nothing** (ten points passed, eleven skipped, two
    deferred) came back as a verdict about a product nobody asked.

    The order of the decision:

      * a FAILED entry          -> ``FAILED``
      * a PARTIAL entry         -> ``PARTIAL``
      * a DEFERRED entry, or an
        environment-blocked one -> ``SKIPPED``
      * anything else           -> ``PASSED``

    An entry the round could not judge does not make the round red —
    it makes the round **unjudged**, and an unjudged round says
    ``SKIPPED``.  A plain ``SKIPPED`` is not that: it is a point the
    round did not need to ask, and it leaves the verdict alone.

    A round with no entry at all stays ``PASSED``: a round with no
    verification point has no verdict to give, and that behaviour is
    out of scope here.  An entry that is not a dict, or that carries
    no status, takes part in no tier at all.
    """
    if not isinstance(entries, (list, tuple)):
        return "PASSED"
    statuses: List[str] = []
    unjudged = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if not status:
            continue
        statuses.append(str(status))
        if str(status) == "DEFERRED" or _entry_is_environment_blocked(entry):
            unjudged = True
    if any(status == "FAILED" for status in statuses):
        return "FAILED"
    if any(status == "PARTIAL" for status in statuses):
        return "PARTIAL"
    if unjudged:
        return "SKIPPED"
    return "PASSED"


# ---------------------------------------------------------------------------
# The round's own evidence, checked against itself (2026-10-26)
# ---------------------------------------------------------------------------
#
# The 02:25 round held two facts that cannot both be true.  Twelve
# ``api_test`` points were refused the connection on port 64469 at
# 02:25:01 — every one of them with the transport's own ``URLError
# [Errno 61]``.  The ``ui_validation`` point verified **the same
# address** from 02:25:01 onwards for twelve minutes without a hitch,
# and the round's own probe answered 200 at 02:37:49.
#
# A refused connection and a served one cannot both be true of one
# address at one moment.  The framework took both at face value, so the
# twelve refusals were written down as twelve product defects.
#
# So the round now checks its own evidence before the report is built.  A
# connection-class failure whose address was **served** somewhere in the
# same round is not a verdict about the product: it is marked
# ``invalid_env`` and gets one more look — the service is re-ensured and
# the point is run again by the round's own dispatcher, under the same
# criteria.  What the second look says is what lands.
#
# The contradiction is written down as it is found
# (``round_evidence_conflict``), because a contradiction nobody can find
# afterwards is a contradiction that will be found again.

#: The status a verification point carries when the round's own evidence
#: contradicts its failure.  A **code**, not prose: a report reader has
#: to be able to match on it.
INVALID_ENV = "invalid_env"

#: The event a round writes when it finds its own evidence contradicting
#: itself.
ROUND_EVIDENCE_CONFLICT_EVENT = "round_evidence_conflict"


def _invalid_env_message(conflict: Dict[str, Any]) -> str:
    """The sentence that says "this is not a verdict about the product".

    Two kinds of contradiction say it, and they say it differently:

      * the address was **refused and served** in the same round — the
        round's own evidence disagrees with itself; and
      * the instance serving the address **changed hands** while the
        point was being verified — the answer cannot be attributed to
        the instance the round started.

    A third says it from the round's side: the round's **instance
    epoch** moved, so the answer was obtained under an epoch the round
    no longer holds.

    All end the same way: the point is given one more look before
    anything is written down about it.
    """
    if str(conflict.get("kind") or "") == "instance_handover":
        before = conflict.get(SERVING_BEFORE_FIELD) or {}
        after = conflict.get(SERVING_AFTER_FIELD) or {}
        return (
            f"{INVALID_ENV}: the instance serving "
            f"{conflict.get('url') or 'the address'} changed hands while "
            f"this point was being verified — boot "
            f"{before.get('boot_id') or '?'} before, boot "
            f"{after.get('boot_id') or '?'} after. The answer this point "
            f"got cannot be attributed to the instance the round started, "
            f"so it is not a verdict about the product; the point is "
            f"given one more look before anything is written down about it."
        )
    if str(conflict.get("kind") or "") == EPOCH_CHANGED_CONFLICT_KIND:
        before = conflict.get("epoch_before") or {}
        after = conflict.get("epoch_after") or {}
        return (
            f"{INVALID_ENV}: the round's instance epoch moved while this "
            f"point was being verified — boot "
            f"{before.get('boot_id') or '?'} before, boot "
            f"{after.get('boot_id') or '?'} after. The answer this point "
            f"got was obtained under an epoch the round no longer holds, "
            f"so it is not a verdict about the product; the point is "
            f"given one more look before anything is written down about it."
        )
    return (
        f"{INVALID_ENV}: the round's own evidence contradicts this "
        f"failure — {conflict.get('url') or 'the address'} was refused "
        f"here but was served for {conflict.get('succeeded_vp')} in the "
        f"same round. This is not a verdict about the product; the "
        f"point is given one more look before anything is written "
        f"down about it."
    )


def _url_origin(url: str) -> str:
    """The address a URL names, reduced to ``scheme://host:port``.

    Two URLs that name the same address are the same address, whatever
    path they carry: a refusal to ``/api/plans`` and a 200 from
    ``/health`` are two facts about one place, and one place cannot
    refuse one request and serve another at the same moment.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        # A URL with a port that is not a number names no address.
        return ""
    if not host:
        return ""
    scheme = (parts.scheme or "http").lower()
    # The default port names the same address as no port at all.
    default_port = {"http": 80, "https": 443, "ws": 80, "wss": 443}.get(scheme)
    if port and port != default_port:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _evidence_url(evidence: Dict[str, Any]) -> str:
    """The URL a piece of evidence says was called."""
    if not isinstance(evidence, dict):
        return ""
    request = evidence.get("request")
    if isinstance(request, dict):
        url = str(request.get("url") or "")
        if url:
            return url
    return str(evidence.get("url") or "")


def _vp_url(vp: Any) -> str:
    """The address a verification point declares it will talk to.

    The ``ui_validation`` point names its address in ``target_url``;
    the ``api_test`` point names it inside its ``request``.  Both are
    declarations the plan made, and both are what a reader checks the
    round's evidence against.
    """
    if not isinstance(vp, dict):
        return ""
    for key in ("target_url", "url"):
        url = str(vp.get(key) or "")
        if url:
            return url
    request = vp.get("request")
    if isinstance(request, dict):
        return str(request.get("url") or "")
    return ""


def _is_invalid_env_result(result: Any) -> bool:
    """Is this result one the round's own evidence contradicted?

    The status is the whole of the claim: ``invalid_env`` says the
    round could not judge this point, and says nothing about the
    product.
    """
    if not isinstance(result, dict):
        return False
    return str(result.get("status") or "") == INVALID_ENV


def _is_passed_result(result: Any) -> bool:
    """Did this point really pass?

    A round that was blocked as a whole rewrites every one of its
    results to BLOCKED and keeps the verdict each one really got in
    the evidence (``original_status``).  The witness is the **verdict**,
    not the rewrite: a point that really passed is still a point that
    passed, and it can still say that the address was served.

    This is the production shape: the round was blocked as a whole
    because twelve points were refused, and the one point that was
    served on the same address was rewritten to BLOCKED with it.  The
    two facts cannot both be true, and the point that was served is
    the one that says which of them is wrong.
    """
    if not isinstance(result, dict):
        return False
    if str(result.get("status") or "") == "PASSED":
        return True
    return str(_result_evidence(result).get("original_status") or "") == "PASSED"


def _result_is_connection_failure(result: Any) -> bool:
    """Did this result's request never reach the service?

    The rule the classification layer uses: the evidence must carry the
    transport's error and **no** ``status_code`` — a response came back
    iff the runner recorded one, and a response that came back means
    the transport reached the service and the assertions decided.
    """
    if not isinstance(result, dict):
        return False
    evidence = _result_evidence(result)
    if evidence.get("status_code") is not None:
        return False
    error_text = str(evidence.get("error") or "")
    if not error_text:
        error_text = str(result.get("actual_result") or "")
    return _is_transport_failure(error_text)


def _tcp_reachable(
    port: int,
    timeout: float = _REENSURE_CONNECT_TIMEOUT_SECONDS,
) -> bool:
    """Can a TCP connection be opened to *port* on the loopback?

    The fast answer to "is anything serving there".  It says nothing
    about *who* is serving — that is the health URL's job.
    """
    import socket

    try:
        with socket.create_connection(
            ("127.0.0.1", int(port)), timeout=timeout,
        ):
            return True
    except Exception:  # noqa: BLE001 - a refused connect is the answer
        return False


def _health_url_answers(
    url: str,
    timeout: float = _REENSURE_HEALTH_TIMEOUT_SECONDS,
) -> bool:
    """Does *url* answer **200** when asked with the request guard's header?

    The header is the one a VP's own request carries (:mod:`request_guard`),
    so the probe travels the same road the request will travel.
    """
    import urllib.error
    import urllib.request

    try:
        from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE
    except Exception:  # pragma: no cover - defensive
        REQUEST_HEADER, REQUEST_HEADER_VALUE = "X-PDT-Request", "1"
    request = urllib.request.Request(url, method="GET")
    request.add_header(REQUEST_HEADER, REQUEST_HEADER_VALUE)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status) == 200
    except urllib.error.HTTPError as exc:
        # A non-200 is an answer too — it just is not the answer the
        # health URL was supposed to give.
        return int(exc.code) == 200
    except Exception:  # noqa: BLE001 - a probe never raises
        return False


def _health_data_instance(
    port: int,
    timeout: float = _REENSURE_HEALTH_TIMEOUT_SECONDS,
) -> Optional[Dict[str, Any]]:
    """The ``instance`` block the service on *port* publishes about itself.

    ``None`` when the service does not answer, does not answer with
    JSON, or answers without an ``instance`` block — a service that
    does not identify itself cannot be compared with anything, so it is
    left alone rather than blocked.

    The block is the answering process's own fact sheet: the boot id
    minted when it started, the plans tree it serves, the state
    database it reads.  Every field is a fact about **that** process,
    so two answers can be compared field by field.
    """
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{int(port)}/health/data"
    request = urllib.request.Request(url, method="GET")
    try:
        from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE
    except Exception:  # pragma: no cover - defensive
        REQUEST_HEADER, REQUEST_HEADER_VALUE = "X-PDT-Request", "1"
    request.add_header(REQUEST_HEADER, REQUEST_HEADER_VALUE)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if int(response.status) != 200:
                return None
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError:
        return None
    except Exception:  # noqa: BLE001 - a probe never raises
        return None
    try:
        body = json.loads(raw)
    except Exception:  # noqa: BLE001 - a probe never raises
        return None
    if not isinstance(body, dict):
        return None
    instance = body.get("instance")
    if not isinstance(instance, dict):
        return None
    return instance


# ---------------------------------------------------------------------------
# The ONE service precondition (2026-10-26)
# ---------------------------------------------------------------------------
#
# A verification round has exactly one question before it may dispatch a
# verification point: **is the service this point is about to talk to
# actually serving?**  On 2026-10-26 the round answered that question
# three different ways, and the disagreement is what cost it:
#
#   * the round-start ensure wrote ``services_ensured`` and said the
#     ``api`` service was ready on port 64469;
#   * every verification point was preceded by ``binary_freshness_pass``
#     — a statement about the binary **on disk**, not about the port;
#   * the twelve ``api_test`` points that followed were all refused the
#     connection.
#
# The same URL was verified for twelve minutes by the ``ui_validation``
# path and passed, so the service *was* there — and the ``api_test``
# path's own precondition did not know it.
#
# So there is now **one** precondition, and every path uses it:
#
#   1. probe the port — is anybody serving there?;
#   2. if nobody is, bring the service up through the round's own start
#      path, stamped with **this round's** token, so the ledger says who
#      owns the instance and a later reap sweep leaves it alone;
#   3. send **the request a verification point would send** — the same
#      client, the same headers — and require a 200.
#
# A round that cannot get that 200 dispatches nothing: a refused
# connection is a statement about the plumbing, not about the product.

#: How long the self-check waits for the health URL.  A local service
#: answers in milliseconds; a slow answer is a broken service.
SERVICE_SELF_CHECK_TIMEOUT_SECONDS = 10.0


def _service_health_url(decl: Any, port: int) -> str:
    """Where to ask the service whether it is alive.

    The declaration's own health URL, moved to the port the service
    really listens on — a service that was relocated answers on its
    new address.

    **Empty** when the declaration names no health URL.  That is the
    plan's own convention, spelled out in the plan prompt: ``health_url``
    is optional, and a service that declares none is judged by TCP
    reachability alone.  Guessing ``/health`` for such a service would
    ask a question the plan never asked, and a service that serves no
    ``/health`` would be blocked for not answering one.
    """
    url = str(getattr(decl, "health_url", "") or "")
    if not url:
        return ""
    try:
        declared = int(getattr(decl, "port", 0) or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared and int(port) != declared:
        try:
            from service_manager import rewrite_port

            url = rewrite_port(url, declared, int(port))
        except Exception:  # pragma: no cover - defensive
            pass
    return url


def _service_port_is_live(port: int) -> bool:
    """Is anything serving on *port*?

    Two oracles, and either one saying "yes" is enough: the direct
    TCP connect (the fast answer) and the round's own liveness probe,
    which names the process behind the port.  The probe is the one the
    round already trusts everywhere else, so the shared precondition
    asks the same question the rest of the round asks — and a project
    that stubs the probe in a test gets the same answer here.
    """
    if _tcp_reachable(int(port)):
        return True
    try:
        from service_freshness import probe_port

        return bool(getattr(probe_port(int(port)), "listening", False))
    except Exception as exc:  # noqa: BLE001 - a probe never raises
        logger.warning(
            "[service_precondition] could not probe port %s: %s", port, exc,
        )
        return False


def _service_self_check(
    url: str, timeout: float = SERVICE_SELF_CHECK_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Send **the request a verification point would send** and read it.

    Same client (:mod:`urllib.request` — the one the ``api_test``
    runner uses) and the same headers (the request guard's), so the
    probe travels the same road the request will travel: a service
    that would refuse that request is refused here too.

    Never raises — a probe that fails is a service that is not ready,
    not a broken round.
    """
    import urllib.error
    import urllib.request

    result: Dict[str, Any] = {
        "asked": False,
        "status_code": None,
        "content_type": "",
        "body_status": "",
        "error": "",
        # 2026-10-26: the address that answered.  The round's own
        # probe is a **witness** — it says this address was being
        # served — and a witness that does not say which address it
        # was cannot contradict anything.
        "url": "",
    }
    if not url:
        result["error"] = "the service declares no health URL to ask"
        return result
    result["asked"] = True
    result["url"] = url
    try:
        from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE
    except Exception:  # pragma: no cover - defensive
        REQUEST_HEADER, REQUEST_HEADER_VALUE = "X-PDT-Request", "1"
    request = urllib.request.Request(url, method="GET")
    request.add_header(REQUEST_HEADER, REQUEST_HEADER_VALUE)
    raw = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result["status_code"] = int(response.status)
            result["content_type"] = str(
                response.headers.get("Content-Type") or ""
            )
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # A non-200 is an answer too — it just is not the answer the
        # health URL was supposed to give.
        result["status_code"] = int(exc.code)
        try:
            result["content_type"] = str(
                (exc.headers or {}).get("Content-Type") or ""
            )
        except Exception:  # pragma: no cover - defensive
            result["content_type"] = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - a body is a nicety
            raw = ""
    except Exception as exc:  # noqa: BLE001 - a probe never raises
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    try:
        body = json.loads(raw)
    except Exception:  # noqa: BLE001 - a non-JSON body is a fact
        body = None
    if isinstance(body, dict):
        result["body_status"] = str(body.get("status") or "")
    return result


def _claim_round_token(
    service_manager: Any, plan_dir: Optional[Path], round_token: str,
) -> None:
    """Make the round's token the plan's **current** owner, if nobody holds it.

    The ledger stamps the services it records with the plan's current
    owner token, and a service recorded without one belongs to nobody —
    so the first reaper that comes along may stop it.  That is one of
    the ways the 2026-10-26 round lost its api service between the
    round's own ensure and its first verification point.

    A token is only claimed when the plan has **no** current owner:
    stealing it from a live round would make that round's services
    reapable, which is the very damage this is here to prevent.
    """
    if not round_token or plan_dir is None:
        return
    register = getattr(service_manager, "register_service_owner", None)
    read = getattr(service_manager, "read_owners", None)
    if register is None or read is None:
        return
    try:
        owners = read(Path(plan_dir))
    except Exception:  # noqa: BLE001 - a damaged registry must not block
        return
    if not isinstance(owners, dict):
        return
    if owners.get("current_owner"):
        return
    try:
        register(Path(plan_dir), round_token, current=True)
    except Exception:  # noqa: BLE001 - a claim that fails is not fatal
        logger.warning(
            "[service_precondition] could not claim the round's token "
            "for %s", plan_dir,
        )


def _default_service_starter(
    decl: Any,
    *,
    plan_dir: Optional[Path],
    round_token: str,
    project_dir: Optional[Path],
    round_number: int,
    plan_id: str,
    service_manager: Any,
) -> Dict[str, Any]:
    """Bring the service up through the project's own service manager.

    The start happens inside the module's own address-event context, so
    the address event carries **this round's** token: the ledger then
    says who owns the instance, and the reap sweep leaves it alone.
    """
    sm = service_manager
    if sm is None:
        try:
            sm = _load_service_manager(project_dir)
        except Exception as exc:  # noqa: BLE001 - a fallback, never a failure
            return {
                "ok": False, "port": 0, "pid": None,
                "detail": f"could not load the service manager: {exc}",
            }
    root = Path(project_dir) if project_dir else Path(".")
    _claim_round_token(sm, plan_dir, round_token)
    context = getattr(sm, "address_event_context", None)
    try:
        if context is not None and plan_dir is not None:
            with context(
                Path(plan_dir), run_id=plan_id, round_token=round_token,
            ):
                runtime_map = sm.ensure_services(
                    root, {decl.name: decl}, round_number=round_number,
                    plan_dir=Path(plan_dir), plan_id=plan_id,
                )
        else:
            runtime_map = sm.ensure_services(
                root, {decl.name: decl}, round_number=round_number,
                plan_dir=Path(plan_dir) if plan_dir is not None else None,
                plan_id=plan_id,
            )
    except TypeError:
        # A project whose service manager has a narrower signature.
        try:
            runtime_map = sm.ensure_services(
                root, {decl.name: decl}, round_number=round_number,
            )
        except Exception as exc:  # noqa: BLE001 - a failed start is a fact
            return {
                "ok": False, "port": 0, "pid": None,
                "detail": f"{type(exc).__name__}: {exc}",
            }
    except Exception as exc:  # noqa: BLE001 - a failed start is a fact
        return {
            "ok": False, "port": 0, "pid": None,
            "detail": f"{type(exc).__name__}: {exc}",
        }
    runtime = (getattr(runtime_map, "runtimes", None) or {}).get(decl.name)
    if runtime is None:
        return {
            "ok": False, "port": 0, "pid": None,
            "detail": "the start attempt reported no runtime for the service",
        }
    port = int(getattr(runtime, "port", 0) or 0)
    pid = getattr(runtime, "pid", None)
    ready = bool(getattr(runtime, "ready", False))
    detail = str(getattr(runtime, "detail", "") or "")
    if not ready:
        return {
            "ok": False, "port": port, "pid": pid,
            "detail": detail or "the service did not come up",
        }
    return {"ok": True, "port": port, "pid": pid, "detail": detail}


def _default_health_check(decl: Any, port: int) -> Dict[str, Any]:
    """Ask the service whether it is alive, over the guarded road.

    The declaration's own health URL, asked with the request guard's
    header — the same header a verification point's request carries, so
    the probe travels the same road the request will travel.  A
    declaration that names no health URL is asked nothing.
    """
    return _service_self_check(_service_health_url(decl, port))


def ensure_service_ready(
    decl: Any,
    plan_dir: Optional[Path] = None,
    round_token: str = "",
    *,
    project_dir: Optional[Path] = None,
    round_number: int = 0,
    plan_id: str = "",
    service_manager: Any = None,
    starter: Optional[Callable[[Any], Dict[str, Any]]] = None,
    liveness: Optional[Callable[[int], bool]] = None,
    health: Optional[Callable[[Any, int], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The one precondition every verification path shares.

    Three steps, in this order:

      1. **Probe the port.**  Is anybody serving there?
      2. **Bring the service up** if nobody is — through the round's
         own start path, stamped with *round_token* so the ledger says
         who owns the instance.
      3. **Ask the service itself.**  The very request a verification
         point would send — same client, same headers — and a 200 is
         required.  A fresh binary on disk is not an answer; the
         service answering is.

    A service that declares no health URL is asked nothing: the plan's
    convention is that such a service is judged by TCP reachability
    alone (see :func:`_service_health_url`).

    Args:
        decl: the service declaration (``port``, ``name``, ``health_url``).
        plan_dir: the plan's directory — where the ledger and the
            address events live.
        round_token: the token this round's services are recorded
            under.
        project_dir: the project the service belongs to.
        round_number: the round this start belongs to.
        plan_id: the plan's id (the ledger's ``run_id``).
        service_manager: the project's own ``service_manager`` module.
        starter: an optional callable that performs the start instead of
            the default one.  It is called with the declaration and
            returns ``{"ok": bool, "port": int, "pid": int | None,
            "detail": str, "health_record": dict}``.
        liveness: an optional callable answering "is anything serving
            on this port?".  Defaults to :func:`_service_port_is_live`.
        health: an optional callable that asks the service whether it
            is alive and returns the self-check record.  It is called
            with the declaration and the port.

    Returns:
        A record with ``ready`` (the verdict), ``service``, ``port``,
        ``pid``, ``error_kind``, ``detail``, ``status_code``,
        ``content_type`` and ``body_status`` — the last three are what
        the service actually answered, so a reader can see the 200 (or
        the absence of one) rather than take it on trust.
    """
    record: Dict[str, Any] = {
        "ready": False,
        "service": str(getattr(decl, "name", "") or ""),
        "port": 0,
        "pid": None,
        "error_kind": "",
        "detail": "",
        "status_code": None,
        "content_type": "",
        "body_status": "",
        "start_attempted": False,
    }
    try:
        port = int(getattr(decl, "port", 0) or 0)
    except (TypeError, ValueError):
        port = 0
    record["port"] = port
    if not port:
        record["error_kind"] = "no_address"
        record["detail"] = "the declaration carries no port to probe"
        return record

    is_live = liveness or _service_port_is_live
    ask_health = health or _default_health_check
    probe: Dict[str, Any] = {}

    if not is_live(port):
        record["start_attempted"] = True
        if starter is not None:
            try:
                outcome = starter(decl)
            except Exception as exc:  # noqa: BLE001 - a failed start is a fact
                outcome = {
                    "ok": False, "port": 0, "pid": None,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            if not isinstance(outcome, dict):
                outcome = {
                    "ok": False, "port": 0, "pid": None,
                    "detail": "the start attempt returned no result",
                }
        else:
            outcome = _default_service_starter(
                decl,
                plan_dir=plan_dir,
                round_token=round_token,
                project_dir=project_dir,
                round_number=round_number,
                plan_id=plan_id,
                service_manager=service_manager,
            )
        record["pid"] = outcome.get("pid")
        started_port = int(outcome.get("port") or 0)
        if started_port:
            record["port"] = started_port
            port = started_port
        if not outcome.get("ok"):
            record["error_kind"] = "tcp_connect_failed"
            record["detail"] = (
                f"nothing accepted a TCP connection on port {port}"
                + (
                    f"; the start attempt did not bring it back: "
                    f"{outcome.get('detail')}"
                    if outcome.get("detail") else ""
                )
            )
            return record
        # The service says it is up.  Ask the port, not the runtime:
        # a runtime that says "ready" on a port nobody listens on is
        # not a service.
        if not is_live(port):
            record["error_kind"] = "tcp_connect_failed"
            record["detail"] = (
                f"nothing accepted a TCP connection on port {port} after "
                f"the start attempt"
            )
            return record
        # The start asked the service itself already — the round's
        # adoption gates do exactly that.  Asking a second time would
        # be a second check, and one check is the point.
        asked = outcome.get("health_record")
        if isinstance(asked, dict):
            probe = asked

    if not probe:
        try:
            probe = ask_health(decl, port)
        except Exception as exc:  # noqa: BLE001 - a probe never raises
            record["error_kind"] = "health_unanswered"
            record["detail"] = (
                f"the health check of service {record['service']!r} raised: "
                f"{type(exc).__name__}: {exc}"
            )
            return record
    if not isinstance(probe, dict):
        probe = {}
    record["status_code"] = probe.get("status_code")
    record["content_type"] = str(probe.get("content_type") or "")
    record["body_status"] = str(probe.get("body_status") or "")
    if probe.get("asked") and probe.get("status_code") != 200:
        record["error_kind"] = "health_unanswered"
        record["detail"] = (
            f"the health URL of service {record['service']!r} did not "
            f"answer 200"
            + (
                f" ({probe.get('error')})" if probe.get("error")
                else f" (it answered {probe.get('status_code')})"
            )
        )
        return record
    record["ready"] = True
    record["detail"] = (
        "the service answers 200 on its health URL"
        if probe.get("asked")
        else "the service accepts connections and declares no health URL"
    )
    return record


#
# Refactor note (Phase C7 → Orchestrator): the historical class name
# ``VerificationAgent`` is kept as a backward-compatibility alias of the
# canonical ``Orchestrator`` class. The 6 phase classes live under
# ``backend/verification/{interview,prd,arch,test,tasks,verification}.py``
# and ``Orchestrator`` is the thin wiring layer that delegates to them.
# Downstream callers that already imported ``VerificationAgent`` keep
# working unchanged because ``VerificationAgent = Orchestrator`` at
# the bottom of this module resolves the symbol for them.

class _RebuildOutcome:
    """What this round's ONE rebuild attempt produced for a key.

    ``result`` is the :class:`binary_rebuild_agent.RebuildResult`;
    ``error`` is set instead when the attempt itself raised. Both are
    recorded even on failure, because the single-flight contract is
    "attempted at most once per round, win or lose" — a failure must
    not turn into a retry storm.

    The error travels inside the outcome rather than as the future's
    own exception on purpose: a leader that dies with no follower
    attached would otherwise leave an unretrieved exception on a future
    nobody awaits, which asyncio reports at GC time as a spurious
    error on an otherwise healthy round.
    """

    __slots__ = ("result", "error")

    def __init__(self, result: Any = None, error: Optional[BaseException] = None):
        self.result = result
        self.error = error

    def unwrap(self) -> Any:
        """The attempt's result, or re-raise the attempt's error."""
        if self.error is not None:
            raise self.error
        return self.result


class _BinaryRebuildCoordinator:
    """Per-round single-flight for binary rebuilds.

    N VPs in ONE round can all judge the same native library stale, each
    spawns its own ``BinaryRebuildAgent``, and each runs ``cargo build``
    against the same ``target/debug``. cargo's artifact-directory file
    lock turns those into a queue as deep as the VP count; while it is
    queued not one VP emits a log line, and the watchdog fails the round
    by design.

    The build being slow is not the root cause. The root cause is that a
    round had no notion of "this binary has already been rebuilt", so
    every verification point that reached a stale verdict started its own
    rebuild. The check is deterministic, and two points that ask while
    the binary is still untouched both see the same mtime and both
    answer "stale" — that is a correct check doing its job, not one point
    interfering with another.

    :class:`binary_freshness.FreshnessCache` is not what fanned them out.
    It records every check and is never read back — it has no read path —
    so it cannot hand one point a verdict that another point computed.
    Its key embedding the binary's mtime is the same phenomenon seen
    from the other side: nothing has rebuilt yet, so the mtime, and with
    it the key, cannot move. That is a consequence of the missing
    single-flight, not a second cause of it. What actually collapses the
    fan-out is this coordinator.

    Semantics (the intended shape: one VP per round exists purely to
    rebuild; once it has rebuilt, the VPs after it must not):

      * key = ``(str(project_dir), kind)`` — the same two quantities the
        stale verdict was derived from;
      * the first caller to reach a key actually runs the rebuild, in
        ``asyncio.to_thread`` (it shells out to cargo / maturin and can
        spawn an LLM sub-agent; a synchronous call freezes every sibling
        VP on the round's single event loop);
      * callers arriving while it runs await the same future and get the
        SAME ``RebuildResult`` object;
      * a key runs at most once per round. Once it has run — succeeded or
        failed — later VPs trigger no new rebuild, but they still run
        their OWN ``check_binary_freshness``: fresh passes, stale is
        BLOCKED.

    Lifecycle: one instance on :class:`Orchestrator` (built in
    ``__init__``), ``clear()``-ed at the top of every round by
    :meth:`Orchestrator.run_full_verification`. The clear must happen
    BEFORE the round-start preflight, or the preflight's own rebuild
    looks like last round's residue, gets wiped, and the VPs of this
    round get a second build.

    The preflight arm (:meth:`Orchestrator._maybe_rebuild_before_service_start`)
    uses :meth:`run_once_sync`: it runs before the loop exists, so it stays
    synchronous — pinned by ``test_preflight_rebuild_really_runs_with_no_event_loop``
    in ``tests/unit/test_verification_rebuild_offloop.py``. Both arms write
    the same table, which is the whole point: a binary the preflight rebuilt
    is not rebuilt again by a VP.
    """

    def __init__(self) -> None:
        # key -> the one attempt that already finished (failed ones included).
        self._done: Dict[Tuple[str, str], _RebuildOutcome] = {}
        # key -> the in-flight attempt's future. The leader creates it,
        # the followers await it.
        self._inflight: Dict[Tuple[str, str], "asyncio.Future"] = {}

    @staticmethod
    def key_for(project_dir: Path, kind: str) -> Tuple[str, str]:
        """The single-flight key: which binary family, in which project."""
        return (str(project_dir), kind or "")

    def attempted(self, key: Tuple[str, str]) -> bool:
        """Whether this round already ran (or is running) a rebuild."""
        return key in self._done or key in self._inflight

    def clear(self) -> None:
        """Start a new round. Called once per round by the orchestrator."""
        self._done.clear()
        self._inflight.clear()

    async def run_once(self, key: Tuple[str, str], factory: Callable[[], Any]) -> Any:
        """Run ``factory`` at most once per key per round; share the result.

        Awaitable. The leader runs ``factory`` via ``asyncio.to_thread``;
        concurrent callers await the leader's future and receive the
        leader's own ``RebuildResult`` object.
        """
        done = self._done.get(key)
        if done is not None:
            return done.unwrap()

        fut = self._inflight.get(key)
        if fut is not None:
            # ``shield`` is load-bearing: a follower being cancelled must
            # not cancel the shared future out from under the other
            # followers and the leader.
            return (await asyncio.shield(fut)).unwrap()

        fut = asyncio.get_event_loop().create_future()
        self._inflight[key] = fut
        try:
            result = await asyncio.to_thread(factory)
        except BaseException as exc:  # noqa: BLE001 — recorded, then re-raised
            outcome = _RebuildOutcome(error=exc)
            self._done[key] = outcome
            if not fut.done():
                fut.set_result(outcome)
            self._inflight.pop(key, None)
            raise
        outcome = _RebuildOutcome(result=result)
        self._done[key] = outcome
        if not fut.done():
            fut.set_result(outcome)
        self._inflight.pop(key, None)
        return result

    def run_once_sync(self, key: Tuple[str, str], factory: Callable[[], Any]) -> Any:
        """Synchronous arm for the preflight — no event loop is running.

        Only reachable from :meth:`Orchestrator._maybe_rebuild_before_service_start`,
        which ``run_full_verification`` calls BEFORE handing the round to
        the executor, so no VP can be inside :meth:`run_once` yet and there
        is nothing to share with. The record it writes is the one the VP
        arms read.
        """
        done = self._done.get(key)
        if done is not None:
            return done.unwrap()
        if key in self._inflight:
            # Unreachable today (see above). If a refactor ever moves the
            # preflight onto the loop, "do not start a second rebuild" is
            # the conservative reading of the contract — but a sync caller
            # cannot await the leader, so refuse rather than build twice.
            raise RuntimeError(
                f"binary rebuild already in flight for {key!r}; the "
                f"synchronous preflight arm cannot join it"
            )
        try:
            result = factory()
        except BaseException as exc:  # noqa: BLE001 — recorded, then re-raised
            self._done[key] = _RebuildOutcome(error=exc)
            raise
        self._done[key] = _RebuildOutcome(result=result)
        return result


# ---------------------------------------------------------------------------
# api_test — the service has to be there before the question is asked
# ---------------------------------------------------------------------------
#
# 2026-10-24: the round-start preflight starts the declared services
# **once**.  A service that dies half-way through the round is then left
# to the VPs that need it, and the first one to arrive finds a dead
# port: the request is refused at the TCP level, **not one assertion
# runs**, and the verdict still comes out as a plain FAILED — which the
# four-class triage files as ``deliverable_defect``, the class that
# counts against the same-cause threshold.  The environment was broken;
# the deliverable was not.
#
# So an api_test checks its declared service immediately before it asks,
# starts it if it is down, and blocks the point (rather than faking an
# assertion failure) when it cannot be brought up.

#: The signal an api_test carries when the service it needs is not
#: answering.  It is one of ``verification.failure_triage``'s known
#: signals, so tier A decides it by rule: ``infra``, which does not
#: count against the same-cause threshold.
API_TEST_SERVICE_NOT_READY_SIGNAL = "service_not_ready"

#: The port of a request URL, or ``None`` when the URL names none.
#: ``{{svc.<name>.url}}`` has already been resolved by the time an
#: api_test runs, so the URL is the only place the port is left.
_URL_PORT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^/]*?:(\d{1,5})")


def _port_from_url(url: str) -> Optional[int]:
    """The port a request URL points at, or ``None``."""
    match = _URL_PORT_RE.match(str(url or "").strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):  # pragma: no cover - the regex is tight
        return None


def _health_endpoint_answers(health_url: str, timeout: float = 3.0) -> bool:
    """Whether the service's health endpoint answers with a 2xx/3xx.

    A port that accepts a connection is not a service that works: the
    2026-09-23 outage had ``/health`` answering 200 while every data
    endpoint returned 500.  So the port check is corroborated by the
    endpoint the plan declared.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(health_url, timeout=timeout) as resp:
            return 200 <= int(resp.status) < 400
    except Exception:  # noqa: BLE001 - any failure means "not answering"
        return False


class Orchestrator:
    """Three-phase verification orchestrator for quality assurance.

    The orchestrator owns the end-to-end verification pipeline (planning
    → execution → judgment) and delegates per-VP work to a sub-agent
    surface (``VerificationSubAgent``).  After the Phase C7 split the
    6 phase concerns live under ``backend/verification/``; this class
    is the wire-up point that orchestrates them.
    """

    def __init__(self, plan_dir: Path, project_dir: Path,
                 coding_tool: Optional[CodingTool] = None,
                 verif_repo: Optional[Any] = None,
                 vp_capacity_resolver: Optional[Callable[[], Optional[int]]] = None):
        """
        Initialize verification agent.

        Args:
            plan_dir: Plan directory containing PRD/arch/test documents
            project_dir: Project directory to verify
            coding_tool: Optional coding tool (defaults to ClaudeCodingTool)
            verif_repo: Optional VerificationRepository handle. When
                provided, the per-VP verdict callback routes through
                ``verif_repo.append_verdict`` (single BEGIN IMMEDIATE +
                COMMIT) and produces zero ``verification_*_state.json``
                files.  ``None`` keeps the legacy JSON persistence path.
            vp_capacity_resolver: Returns how many VPs the verification
                scene's providers can serve right now, or ``None`` when
                nothing is derivable. Defaults to
                :func:`_verification_vp_capacity`, which reads the live
                provider set and what is left of their 5-hour budget.
                Injectable because a test that wants to pin a *width*
                should not have to stand up a CC Switch database to do
                it — and because "no bound is derivable" is a real
                answer, which the fallback path has to be exercised
                against.

                **Legacy-path width only (2026-10-13).** The live round
                does not read this: :meth:`run_full_verification` builds
                no width at all, because the provider slot pool decides
                per dispatch. This resolver feeds the gate on the
                production-dead per-group path
                (:meth:`execute_verification_plan_async` →
                :meth:`_execute_group`), which tests and e2e still drive.
                It is retained for that path rather than deleted with it.
        """
        self.plan_dir = Path(plan_dir)
        self.project_dir = Path(project_dir)
        self.coding_tool = coding_tool or ClaudeCodingTool()
        self.vp_capacity_resolver = (
            vp_capacity_resolver or _verification_vp_capacity
        )
        # The concurrency the last round actually reached — measured, not
        # declared (see :meth:`_note_vp_capacity`). ``None`` until a
        # round has run; the report paths publish it as-is rather than
        # deriving something, so a report cannot describe a round with a
        # number the round never had.
        self._last_vp_capacity: Optional[int] = None
        # 2026-10-23（工作树指纹基线）：本轮开始时工作树的内容指纹。依据
        # 核对读的是活的树，基线用来判断"读到的还是不是这一轮的那棵树"。
        # ``None`` = 没有基线（非 git 目录 / 探测失败）→ 沿用核对的前一种
        # 行为，不挂起。
        self._round_tree_fingerprint: Optional[str] = None
        # 2026-10-23（挂起的依据核对）：树在核对期间被改过时，这次核对
        # 不能据以降级 —— 它被挂到这里等人工复核，子 agent 的原结论保留。
        # 每一项是 ``(vp_id, vp, verdict)``；不去重、不覆盖：同一个 VP
        # 多次进入核对是多次独立观察，都要留住。
        self._deferred_evidence_checks: list[tuple[str, dict, dict]] = []
        # Task-11 subagent callback chain: when set, the executor's
        # verdict recording path writes via VerificationRepository
        # instead of ``atomic_write_json`` to a per-plan JSON file.
        self.verif_repo = verif_repo

        # Prepend project's venv bin to PATH so that the LLM sub-agent's
        # Bash tool calls (which inherit the backend's environment via
        # subprocess inheritance) can resolve `pytest`, `python`, etc.
        # without requiring the LLM to manually `source` the venv.
        # _wrap_command_with_env handles the automated_test path; this
        # handles the ui_validation/code_review LLM-Bash path.
        # The venv-detection logic is duplicated here intentionally —
        # keeping it minimal (one line of PATH prepend) and idempotent
        # (if the venv bin is already on PATH, this is a no-op).
        if self.project_dir:
            venv_bin = None
            for rel in ["venv1/bin", "venv/bin", ".venv/bin"]:
                candidate = self.project_dir / rel
                if candidate.is_dir():
                    venv_bin = str(candidate.resolve())
                    break
            if venv_bin:
                current_path = os.environ.get("PATH", "")
                if venv_bin not in current_path.split(":"):
                    os.environ["PATH"] = f"{venv_bin}:{current_path}"
                    if hasattr(self, 'persistence') and self.persistence:
                        try:
                            self.persistence.write_verification_point_log(
                                "__init__",
                                "venv_path_prepended",
                                {"venv_bin": venv_bin, "new_path_prefix": venv_bin},
                            )
                        except Exception:
                            pass

        # File paths
        self.prd_file = self.plan_dir / "prd.md"
        # Structured logger for the hard-timeout routing events
        # (``vp_hard_timeout_routing_to_split`` etc.). The call sites
        # use the ``ExecutionLogger`` event-style signature — without
        # a logger assigned, the ``if self.logger`` guards in
        # ``_run_single_vp_async`` raised AttributeError on the
        # *first* hard timeout and the split path silently degraded
        # to a generic FAILED result (2026-09-13 bugfix, surfaced by
        # TestSplitOnTimeout). Falls back to a stdlib logger when the
        # ExecutionLogger cannot write (e.g. read-only plans dir) so
        # the attribute always exists.
        self.prd_json_file = self.plan_dir / "prd.json"
        self.arch_file = self.plan_dir / "arch-design.md"
        self.test_file = self.plan_dir / "test-design.md"
        self.verification_plan_file = self.plan_dir / "verification_plan.json"
        self.verification_report_file = self.plan_dir / "verification_report.json"

        # Persistence manager
        self.persistence = VerificationPersistenceManager(self.plan_dir)

        # Structured event logger (same convention as ``agent.py``):
        # writes JSON lines to ``<plans_dir>/<plan_id>/execution.log``.
        # The hard-timeout call sites pass ``vp_id=...`` which only
        # ``ExecutionLogger``-style loggers understand, so the stdlib
        # fallback is wrapped in an adapter that drops unknown kwargs.
        try:
            from execution_logger import ExecutionLogger
            self.logger = ExecutionLogger(self.plan_dir.name)
        except Exception:  # noqa: BLE001 — logging must never break init
            self.logger = logging.getLogger(
                f"verification.{self.plan_dir.name}"
            )

        # Timeout policy (per-VP override > per-method default > global default).
        # Loaded from verification.yaml when present; falls back to the
        # hard-coded numbers that were previously inlined in
        # ``_execute_*`` so behaviour is unchanged for callers that do
        # not provide a yaml.
        self.timeout_policy: TimeoutPolicy = TimeoutPolicy.from_yaml(
            os.getenv(
                "VERIFICATION_CONFIG_PATH",
                str(Path(__file__).parent / "configs" / "verification.yaml"),
            )
        )

        # Current round number (will be set when running verification)
        self._current_round = 0

        # Per-verification-round memoisation for the binary
        # freshness check.
        #
        # 2026-10-14: the comment that used to sit here claimed
        # ``run_two_phase_loop`` cleared this at the start of each
        # round. It never did — nothing in the codebase called
        # ``FreshnessCache.clear()``, so one cache lived from ``__init__``
        # until the orchestrator died and last round's stale verdict was
        # carried into the next one. ``run_full_verification`` now calls
        # ``clear()`` at the top of each round (see there).
        self._freshness_cache = FreshnessCache()

        # 2026-10-14: at most ONE rebuild per
        # (project, kind) per round, shared by the preflight arm and
        # every VP arm. Before this, N VPs judging the same binary stale
        # each ran their own BinaryRebuildAgent against the same
        # target/debug, and cargo's file lock serialised them into a
        # queue that starved the round. See
        # :class:`_BinaryRebuildCoordinator`.
        self._rebuild_coordinator = _BinaryRebuildCoordinator()

        # 2026-09-16 service-freshness preflight ("VP0") state. The
        # round-start preflight (``_preflight_service_freshness``)
        # fills ``_blocked_service_ports`` when a service port the
        # plan depends on could not be restarted; the per-VP runner
        # then short-circuits any VP referencing a blocked port to a
        # BLOCKED verdict instead of burning a sub-agent on a
        # connection-refused failure. Reset on every preflight run.
        self._blocked_service_ports: set = set()
        self._last_service_freshness_report: Optional[Any] = None
        #: 2026-09-18 plan-level service declarations (C1). Parsed once
        #: per round by :meth:`_normalize_service_declarations` from the
        #: plan's ``services`` block; keyed by service name. The
        #: preflight starts these services and the executor resolves
        #: ``{{svc.<name>.<field>}}`` placeholders through them.
        #: Empty for legacy plans (no ``services`` block), which keep
        #: the old regex-scrape behaviour.
        self._service_declarations: Dict[str, Any] = {}
        self._service_declaration_report: Optional[Any] = None
        #: 2026-09-18 C2: what the round's declared services look like
        #: after the preflight tried to bring them up (who was spawned,
        #: who was adopted, which ports are blocked). ``None`` for
        #: legacy plans, which use the scrape-and-restart path instead.
        self._service_runtime_map: Optional[Any] = None
        #: 2026-10-25 (repair-r1-22) — the service instance each
        #: declared service is **bound** to for this round.  The
        #: binding is what a VP's request is built from: it is the
        #: runtime object the ``ensure`` returned, so a runtime map
        #: that is rewritten later cannot move the address under a VP
        #: that is talking to a healthy instance.  Keyed by service
        #: name; empty for legacy plans (no ``services`` block).
        self._service_bindings: Dict[str, _BoundService] = {}
        #: 2026-10-24 — per-VP service liveness recovery.  The
        #: round-start preflight starts the declared services **once**;
        #: a service that dies *after* that stays dead for the rest of
        #: the round, and every VP that talks to it is graded on the
        #: connection refusal.  These carry the recovery's state:
        #: the lock that keeps N concurrent VPs from starting N copies
        #: of the same service, and the services whose recovery already
        #: failed this round (one attempt each — a service that cannot
        #: be brought back must not be retried by every later VP).
        self._service_reensure_lock: Optional["asyncio.Lock"] = None
        self._service_reensure_lock_loop: Optional[Any] = None
        self._service_reensure_failed: set = set()
        #: 2026-10-26 — the **self-check** records: what each service
        #: answered when it was last asked over the guarded road, keyed
        #: by ``(service, port)``.  The record is what the round's
        #: adoption gates produce, and the one precondition reuses it
        #: instead of asking the same service a second time.
        self._service_self_check_records: Dict[Tuple[str, int], Dict[str, Any]] = {}
        #: 2026-10-25 (repair-r1-24) — the ports whose service the round
        #: could **not** reach after a re-ensure, and why.  The
        #: blocked-port gate turns these into BLOCKED verdicts that carry
        #: the ``service_unreachable`` code, so a report reader can tell
        #: "the plumbing died" from "the project answered wrong".
        self._service_unreachable: Dict[int, str] = {}
        #: 2026-10-25 (repair-r1-26) — did the **environment** fail this
        #: round?  A request that never reached its service is not a
        #: verdict about the product, so it is BLOCKED, and the round
        #: that produced it is blocked as a whole: every one of its
        #: results is recorded BLOCKED and the upper layer re-runs the
        #: round instead of repairing a product that was never asked.
        #: The per-VP details say which address was called and what the
        #: transport said about it.
        self._environment_blocked: bool = False
        self._environment_blocked_details: List[Dict[str, Any]] = []
        #: The round event is written once per round, not once per VP.
        self._environment_blocked_event_written: bool = False
        #: 2026-10-25 (repair-r1-32) — has this round already spent its
        #: one automatic re-run of the points the environment blocked?
        #: At most one: a second look at a service that is still not
        #: serving buys nothing but cost.
        self._environment_rerun_attempted: bool = False
        #: 2026-10-26 — has this round already checked its own evidence
        #: against itself?  At most once: a second look at the same
        #: evidence buys nothing but cost, and the point of the check is
        #: that it happens **before** the report is built.
        self._round_evidence_reconciled: bool = False
        #: The round's own per-point dispatcher, kept so the re-run can
        #: grade the points it re-runs by exactly the rules the first
        #: attempt used.
        self._vp_dispatch_runner: Optional[Any] = None
        #: The plan **before** its ``{{svc.*}}`` placeholders were
        #: resolved.  Re-resolving after a service moved needs the
        #: unresolved source — a resolved copy cannot be resolved again.
        self._unresolved_plan_data: Optional[dict] = None
        #: The executor's VPs by id — where a re-resolution writes the
        #: service's new address.
        self._executor_vps_by_id: Dict[str, dict] = {}
        #: 2026-10-24 — the data plane each declared service was serving
        #: when the round started.  A live port is **not** proof that the
        #: right instance is behind it: the production round talked to
        #: an instance whose ``/health/data`` answered with an empty
        #: database, and every assertion after that ran against the
        #: wrong data.  Keyed by service name; only the services that
        #: answered the probe are recorded — a service that does not
        #: speak ``/health/data`` is never gated.
        self._data_plane_fingerprints: Dict[str, Any] = {}
        #: The services whose one re-ensure this round already spent.
        #: The round may bring a lost instance back **once**; the VPs
        #: after that go straight to BLOCKED instead of each paying for
        #: another start.
        self._data_plane_reensure_spent: set = set()
        #: 2026-10-25 — the **identity** of the instance each declared
        #: service was serving when the round's ``ensure`` produced it.
        #: A live port is not proof that the round's own instance is
        #: behind it: the production round's address belonged to an
        #: instance it had never started, and no amount of ``200 OK``
        #: makes the two the same.  The ``instance`` block the service
        #: publishes about itself is what settles it — the boot id says
        #: *which process*, the plans tree and the state database's
        #: inode say *which data*.  Keyed by service name; a service
        #: that publishes no block is never gated.
        self._service_instance_identity: Dict[str, Dict[str, Any]] = {}
        #: 2026-10-25 (repair-r1-31) — **where every VP of this round
        #: was sent**.  The production round's evidence said only
        #: ``Errno 61``, which cannot tell "the service is down" from
        #: "the request never reached the code under test".  Each
        #: record carries the URL, the port it names, the port the
        #: round's ``ensure`` bound, the pid and boot id behind that
        #: binding, and how many times the request was attempted.
        #: Keyed by vp id; empty for a VP that never dispatched.
        self._vp_address_records: Dict[str, Dict[str, Any]] = {}
        #: How many times each VP's request was **attempted** this
        #: round.  ``0`` means "no request was ever sent" — the
        #: difference between a request that failed and a request that
        #: never happened.
        self._vp_attempts: Dict[str, int] = {}
        #: The round's own request id — one for every VP of the round,
        #: so their evidence can be compared horizontally.
        self._round_request_id: str = ""
        #: The instance blocks the round has **observed** this round,
        #: keyed by the port they were read from.  The round reads
        #: ``/health/data`` anyway when it gates on an instance's
        #: identity; the answer it gets there is the answer to "who is
        #: listening on that port", and a bookkeeping field must not
        #: make the service answer a **new** question.
        self._observed_instances: Dict[int, Dict[str, Any]] = {}
        #: 2026-10-26 — **who was serving the address**, on both sides of
        #: every dispatch.  The production round knew the address it
        #: aimed at and nothing about the instance answering there, so a
        #: refusal on that address could not be told apart from "the
        #: port changed hands while the request was in flight".  Each
        #: record carries the snapshot taken before the request and the
        #: one taken after it.  Keyed by vp id.
        self._vp_serving_snapshots: Dict[str, Dict[str, Any]] = {}
        #: The instance that served the round's **first** dispatch — the
        #: "before" of the round-level handover record.
        self._round_serving_snapshot: Optional[Dict[str, Any]] = None
        #: The port that snapshot was read from, so the round can ask
        #: the same address again when the round ends.
        self._round_serving_port: Optional[int] = None
        #: The handovers the round saw, one per dispatch whose address
        #: changed hands.  The round's own reconciliation reads them, so
        #: a contradicted point gets the **one** second look every other
        #: contradicted point gets.
        self._instance_handover_conflicts: List[Dict[str, Any]] = []
        #: 2026-10-26 — the round's **instance epoch**: the identity of the
        #: instance whose answers this round accepts.  Established from the
        #: first successful read of ``/health/data`` and compared before
        #: every dispatch after that.  ``None`` means the round has not
        #: named an instance yet — either it has no service to ask, or the
        #: service does not identify itself.
        self._instance_epoch: Optional[Dict[str, Any]] = None
        #: The comparable form of the epoch — ``pid@boot_id``.
        self._instance_epoch_token: str = ""
        #: The epoch each verification point was dispatched under, keyed by
        #: vp id.  A point whose epoch is no longer the round's was
        #: answered by an instance the round cannot place.
        self._vp_epoch_tokens: Dict[str, str] = {}
        #: The epoch changes the round saw.  The round's own reconciliation
        #: reads them, so a point answered under a stale epoch gets the
        #: **one** second look every other contradicted point gets.
        self._instance_epoch_conflicts: List[Dict[str, Any]] = []

        #: 2026-10-24 (repair-r3-16): **why** a port is blocked.  The
        #: preflight's diagnosis (exit code, first error, log tail) used
        #: to die in a ``logger.warning`` with the process; a blocked
        #: port now carries its reason all the way into the verdict's
        #: evidence, so the next round — and the operator — can read it.
        self._blocked_service_details: Dict[int, str] = {}
        #: 2026-10-24: per-round single-flight for the **recoverable**
        #: service gate.  A round has a dozen VPs pointing at the same
        #: declared service; they must share ONE start attempt, or they
        #: start a dozen copies that fight over the port — and the
        #: losers of that fight are the ones whose start command exits
        #: with EADDRINUSE, which is how "the service can start" turns
        #: into "the service cannot start".
        self._service_recovery_locks: Dict[int, threading.Lock] = {}
        self._service_recovery_guard = threading.Lock()
        #: port → whether this round's recovery attempt brought it up.
        self._service_recovery_outcomes: Dict[int, bool] = {}
        #: 阶段归一化摘要（Phase 1/2 计数 + 被兜底提升的 VP），由
        #: :meth:`generate_verification_plan` 填充，供审计/展示读取。
        self._last_phase_summary: Dict[str, Any] = {}
        #: 2026-10-25 (repair-r1-33) — the round's **service
        #: preflight**: the question "may this round dispatch its
        #: verification points at all?".  Installed by the verification
        #: loop (which owns the question) and called by the agent
        #: (which owns the services) right after the round-start
        #: preflight has brought the services up, and before the first
        #: verification point is dispatched.  ``None`` means no
        #: preflight was installed — the round runs as it always did.
        self._round_service_preflight: Optional[
            Callable[[], Optional[Dict[str, Any]]]
        ] = None

    def _note_vp_capacity(self, peak: Optional[int]) -> None:
        """Record the concurrency the round reached, for the report to quote.

        **Measured, not declared.** The round carries no width any more,
        so there is nothing to note *before* it and nothing to derive
        during it: the provider slot pool decides, per dispatch, how many
        verification points may be in flight
        (:func:`dynamic_provider_concurrency.acquire_provider_with_dynamic_capacity`).
        What the round actually reached is
        :attr:`base_executor.BaseExecutor.peak_in_flight` — items inside
        the runner at the same moment — and that is what this records.

        The alternative was to re-read the live pool at report time, and
        it is wrong twice over: it costs a CC Switch round-trip to
        describe a run that is already over, and it answers a *different*
        question — what the fleet looks like now — while claiming to
        describe what the run did. A measured peak cannot have that
        problem, because it is not a claim about anything but the past.

        ``None`` means "nothing measured" and is never recorded: a report
        built before the round (or on a plan with nothing to run) has no
        peak, and publishing a number there would be inventing one.
        """
        if peak is not None:
            self._last_vp_capacity = int(peak)

    def _report_vp_capacity(self) -> Optional[int]:
        """The concurrency to publish: what the round measured, else ``None``.

        No live fallback. The old one answered "what would the gate use
        if a round started now", which was a width; this field is a
        measurement of a round that happened. Substituting one for the
        other would put the old lie back under a new name.
        """
        return self._last_vp_capacity

    def _normalize_service_declarations(self, plan_data: dict) -> dict:
        """Parse + validate the plan's ``services`` block and annotate
        the plan with what it says (2026-09-18 C1).

        Runs on **both** plan paths — the fresh-generation path and the
        on-disk-reuse path — so a plan written before this schema
        existed is inspected with exactly the same rules as a new one.
        A legacy plan simply has no ``services`` key, parses to an
        empty :class:`DeclarationParseResult` with ``block_present =
        False``, and keeps the old scrape-the-command-text behaviour.

        What it does:

          * stores the declarations on ``self`` for the preflight
            (C2 start-once) and the executor (placeholder resolution);
          * logs one ``service_declarations_parsed`` event with the
            full parse result, so the round's JSONL shows what the
            system believed the plan's services were;
          * logs each rejected entry (``service_declaration_rejected``)
           — a silently dropped declaration is how a round ends up
            running against a port nobody started;
          * annotates every VP that references a service wrongly
            (unknown name, unknown field, or a hardcoded port belonging
            to a declared service) with ``service_reference_issues``,
            and logs each one. The annotation is *not* a verdict — C2
            turns an unresolved reference into a BLOCKED verdict at
            execution time; here it is only made visible.

        Never raises: a malformed block must not take the round down.
        """
        try:
            from service_declaration import (
                find_reference_issues,
                parse_declarations,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[service_declaration] unavailable (%s); plan services "
                "will not be validated this round", exc,
            )
            return plan_data

        try:
            result = parse_declarations(plan_data, self.project_dir)
        except Exception as exc:  # pragma: no cover - parse never raises
            logger.warning(
                "[service_declaration] parse failed (%s); continuing", exc,
            )
            return plan_data

        self._service_declaration_report = result
        self._service_declarations = result.by_name()

        self._safe_log_freshness_event(
            "__plan__", "service_declarations_parsed", result.to_dict(),
        )
        for issue in result.issues:
            logger.warning(
                "[service_declaration] rejected entry #%s (%s): %s",
                issue.index, issue.name or "<unnamed>", issue.reason,
            )
            self._safe_log_freshness_event(
                "__plan__", "service_declaration_rejected", issue.to_dict(),
            )

        if not isinstance(plan_data, dict):
            return plan_data
        try:
            ref_issues = find_reference_issues(plan_data, self._service_declarations)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[service_declaration] reference check failed (%s)", exc,
            )
            return plan_data
        if ref_issues:
            by_vp: Dict[str, List[Dict[str, Any]]] = {}
            for issue in ref_issues:
                by_vp.setdefault(issue.vp_id, []).append(issue.to_dict())
                self._safe_log_freshness_event(
                    issue.vp_id, "service_reference_issue", issue.to_dict(),
                )
            annotated_vps = plan_data.get("verification_points")
            if not isinstance(annotated_vps, list):
                return plan_data
            for vp in annotated_vps:
                if isinstance(vp, dict) and str(vp.get("id")) in by_vp:
                    vp["service_reference_issues"] = by_vp[str(vp.get("id"))]
        return plan_data

    def _service_reference_report(self, plan_data: dict) -> List[Dict[str, Any]]:
        """Per-VP service-reference violations, shaped for the plan
        regeneration feedback (2026-09-18 C2).

        Reads the ``service_reference_issues`` annotations that
        :meth:`_normalize_service_declarations` just wrote. Kept as a
        separate pass rather than returned from the normaliser because
        the reuse path and the generate path both call the normaliser
        but only the generate path can regenerate.
        """
        if not isinstance(plan_data, dict):
            return []
        report: List[Dict[str, Any]] = []
        vps = plan_data.get("verification_points")
        if not isinstance(vps, list):
            return report
        for vp in vps:
            if not isinstance(vp, dict):
                continue
            issues = vp.get("service_reference_issues")
            if not issues:
                continue
            report.append({
                "id": str(vp.get("id", "unknown")),
                "title": str(vp.get("title", "")),
                "issues": [
                    str(i.get("detail", i)) if isinstance(i, dict) else str(i)
                    for i in issues
                ],
            })
        return report

    def _api_schema_report(self, plan_data: dict) -> List[Dict[str, Any]]:
        """``api_test`` VPs whose request/assertions schema is unusable.

        Shaped like :meth:`_service_reference_report` so both can ride
        the same regeneration loop. Without this the planner could emit
        an ``api_test`` with no assertions — which the deterministic
        runner would then grade FAILED — and the round would burn a
        cycle discovering a plan defect the generator could have been
        told about for free.
        """
        if not isinstance(plan_data, dict):
            return []
        try:
            from verification_api_runner import validate_vp
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[api_runner] schema validator unavailable: %s", exc)
            return []
        report: List[Dict[str, Any]] = []
        vps = plan_data.get("verification_points")
        if not isinstance(vps, list):
            return report
        for vp in vps:
            if not isinstance(vp, dict):
                continue
            if str(vp.get("verification_method") or "") != "api_test":
                continue
            issues = validate_vp(vp)
            if not issues:
                continue
            report.append({
                "id": str(vp.get("id", "unknown")),
                "title": str(vp.get("title", "")),
                "issues": [i.detail for i in issues],
            })
        return report

    def _collect_prd_acceptance(self) -> List[str]:
        """The PRD's acceptance criteria, as plain strings.

        Read from ``prd.json`` rather than from the markdown so the
        coverage check sees the same list the planner was shown. An
        unreadable or absent PRD yields ``[]``, which disables the
        check rather than failing the round — a plan that exists
        without a PRD is already reported elsewhere.
        """
        from verification_plan_delta import collect_acceptance

        try:
            return collect_acceptance(self.plan_dir)
        except Exception:  # noqa: BLE001 - never fail planning on this
            logger.warning(
                "[verification] could not read PRD acceptance criteria; "
                "the coverage check is disabled for this round",
                exc_info=True,
            )
            return []

    def _violation_fix_guidance(
        self,
        service_violations: List[Dict[str, Any]],
        api_schema_violations: Optional[List[Dict[str, Any]]] = None,
        gate_gaps: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """The "how to fix it" half of the regeneration prompt.

        Split out so every violation family can carry its own guidance
        without one family's wording leaking into another's case — a
        plan rejected only for a missing service declaration should not
        be lectured about bounded commands.
        """
        parts: List[str] = []
        if service_violations:
            names = ", ".join(sorted(self._service_declarations)) or "(none)"
            parts.append(
                "对「服务引用违规」的那些：端口只能在计划顶层的 "
                "`services` 数组里声明一次，VP 不得写死端口、也不得用 "
                "`nohup ... &` 之类自己启动服务——服务由系统统一启动一次，"
                f"VP 只能按名字引用。当前已声明的服务：{names}。"
                "可用的占位符：`{{svc.<name>.url}}` / `{{svc.<name>.port}}` "
                "/ `{{svc.<name>.host}}` / `{{svc.<name>.health_url}}`。"
                "如果某条 VP 依赖的服务还没有声明，请先把它加进 `services` "
                "（给出 `name` / `port` / `start_cmd`），再在 VP 里按名字引用。"
            )
        if api_schema_violations:
            parts.append(
                "对「api_test 断言不合规」的那些：`api_test` 由框架执行、"
                "没有 LLM 参与判定，所以每条 VP 必须给出：\n"
                "  · `request`：`{\"method\": \"GET|POST|PUT|PATCH|DELETE\", "
                "\"url\": \"{{svc.<name>.url}}/路径\"}`，url 里不要写死主机端口；\n"
                "  · `assertions`：非空列表。每条 = 一个主语 + 一个比较符。\n"
                "      自带期望值的主语：`{\"status\": 200}` / "
                "`{\"status\": [200, 201]}` / `{\"body_contains\": \"…\"}` / "
                "`{\"body_not_contains\": \"…\"}`（不要再加比较符）；\n"
                "      需要比较符的主语：`{\"json_path\": \"$.a.b\", \"equals\": \"…\"}`、"
                "`{\"header\": \"content-type\", \"contains\": \"json\"}`。\n"
                "    可选比较符：equals / not_equals / contains / not_contains / "
                "exists / matches / in / not_in / gt / gte / lt / lte / "
                "length_gte / length_lte / length_equals。\n"
                "  **主语只能用上面这几个，不要自己发明拼法。** 表外的键（哪怕"
                "看起来很对称）会让**整条 VP** 在 schema 阶段被拒，一条断言都发不"
                "出去，包括同一 VP 里本来正确的那些。负向契约（响应体里不得出现"
                "某个串）写成 `{\"body_not_contains\": \"…\"}` —— "
                "`json_path` + `not_contains` **不是替代品**：它要求响应是 JSON "
                "且该路径可解析，而泄露发生在原始响应体上。\n"
                "  只写主语不写比较符（如 `{\"json_path\": \"$.a\"}`）不合规 —— "
                "它永远不会失败。期望的 4xx/5xx 也是合法断言。"
            )
        if gate_gaps:
            parts.append(
                "对「Phase 2 全量关卡缺失」的那些：项目里存在全量入口就必须有"
                "对应关卡 —— \n"
                "  · CI 总入口 → `verification_phase: 2` / `phase_order: 1` / "
                "`verification_method: \"full_ci\"`，并给出 `ci_entry`"
                "（该入口在项目根目录下可直接执行的命令）；\n"
                "  · 全量 E2E 套件 → `verification_phase: 2` / "
                "`phase_order: 2` / `verification_method: \"e2e\"`，"
                "`target_url` 用服务占位符写。\n"
                "  **只补缺的那几条关卡，其余 VP 原样保留。**"
            )
        return "".join("\n\n" + p for p in parts)

    def _normalize_legacy_methods(self, plan_data: dict) -> dict:
        """Retire VPs whose ``verification_method`` no longer exists.

        ``automated_test`` and ``manual_check`` were removed on
        2026-09-18 (see ``verification_subagent.SUPPORTED_METHODS`` for
        why). A plan written before that — or one the planner still
        emits them in, until the prompt change takes effect — would
        otherwise blow up at the template lookup.

        Retiring rather than rewriting is deliberate, and it follows the
        2026-09-18 decision that a VP's declared fields are immutable:
        the honest treatment of "this assertion's verification method no
        longer exists" is to mark the point unverified and say so, not
        to silently re-express it as something the framework can pass.
        **The assertion still needs coverage** — the log line and the
        report both name the VP and what it was asserting, so an
        operator can re-express it as ``api_test`` (preferred),
        ``ui_validation`` or ``code_review``.

        Never raises: a malformed plan must not take the round down.
        """
        if not isinstance(plan_data, dict):
            return plan_data
        # 2026-09-18（D2）：这里原本有一个 `except Exception` 兜底，把方法
        # 清单复制了一份字面量。词汇表扩到 5 个方法之后那份拷贝就是陷阱 ——
        # 它会让用新方法的 VP 在归一化路径上被判 obsolete，而执行器照样会
        # 跑它。`verification_subagent` 在同一条 import 路径上、执行期本来
        # 就必须可导入（下面第 3600 行附近就是无条件导入），所以兜底是死
        # 代码，直接去掉。
        from verification_subagent import LEGACY_METHODS, SUPPORTED_METHODS

        vps = plan_data.get("verification_points")
        if not isinstance(vps, list):
            return plan_data

        retired: List[Dict[str, Any]] = []
        for vp in vps:
            if not isinstance(vp, dict):
                continue
            method = str(vp.get("verification_method") or "").strip()
            if method in SUPPORTED_METHODS:
                continue
            if method in LEGACY_METHODS:
                reason = (
                    f"verification_method {method!r} was retired on "
                    f"2026-09-18; this assertion is no longer verified — "
                    f"re-express it as api_test / ui_validation / code_review"
                )
            elif not method:
                reason = (
                    "verification_method is missing; this VP cannot be "
                    "executed — declare one of api_test / ui_validation / "
                    "code_review"
                )
            else:
                reason = (
                    f"verification_method {method!r} is not one of "
                    f"{list(SUPPORTED_METHODS)}; this VP cannot be executed"
                )
            vp["obsolete"] = True
            vp["obsolete_reason"] = reason
            retired.append({
                "id": str(vp.get("id", "unknown")),
                "title": str(vp.get("title", "")),
                "method": method or "(missing)",
            })

        if retired:
            logger.warning(
                "[method_retirement] %d VP(s) cannot be executed and will "
                "NOT run: %s",
                len(retired),
                ", ".join(f"{r['id']}({r['method']})" for r in retired),
            )
            for item in retired:
                self._safe_log_freshness_event(
                    item["id"], "verification_method_retired", item,
                )
        return plan_data

    def _maybe_evaluate_plan_delta(
        self,
        plan_data: dict,
        expected_digest: Optional[str] = None,
    ) -> Dict[str, Any]:
        """每轮验证开始前评估 VP 方案的增量（新增 / 废弃）。

        （2026-09-16）：验证是以黑盒角度进行的，VP 阶段的一个 VP 对应的
        是功能而不是某个 task，所以每轮验证开始前都要重新评估当前 VP
        方案的合理性、是否需要增减 VP。新增和废弃 VP 只需要有日志记录，
        不允许修改已有的 VP。

        语义约束（全部由 :mod:`verification_plan_delta` 就地强制执行）：

        * 只允许 ``add`` / ``obsolete``，**修改已有 VP 一律拒绝**并留痕；
        * 新增必须有依据（缺 ``reason`` 直接丢弃）；
        * 新增的 VP 同样要过有界命令门禁 + 语义审计，违规的丢弃并记日志。

        只在**确实有新增任务**时才调用 LLM（第一轮只记基线），失败绝不阻断
        验证——评估是增益，不是门禁。

        增量必须**落盘**（2026-10-22）：以前这里调的是裸 ``apply_delta``
        （只改内存），磁盘上的 ``verification_plan.json`` 一个字节都没动 ——
        于是"作废"下一轮必然复活，替换 VP 也跟着消失，修复任务照旧围绕一条
        已经不成立的判据生成。现在走 :func:`apply_delta_persisted`：读-改-写
        合成一步，失败时磁盘与内存都保持原样。

        Args:
            plan_data: 调用方持有的计划对象（复用路径上是从磁盘读进来的
                那份）。落盘成功后它的内容被换成磁盘上的那一份。
            expected_digest: 读入计划时记下的 sha256。写盘前复核一次：
                计划在增量评估期间被外部改过就放弃这一轮增量，而不是覆盖
                掉别人的改动。
        """
        try:
            from verification_plan_delta import (
                DELTA_SYSTEM_PROMPT,
                PlanDeltaPersistError,
                allocated_vp_ids,
                apply_delta_persisted,
                build_delta_prompt,
                collect_acceptance,
                collect_new_tasks,
                load_delta_state,
                merge_allocated_ids,
                parse_delta_payload,
                save_delta_state,
            )

            state = load_delta_state(self.plan_dir)
            seen = state.get("seen_task_ids")
            # 2026-10-24: 历轮分配出去的 VP id。基础计划（磁盘上的
            # ``verification_plan.json``）每轮被整份复用，增量条目只活在内存
            # 里 —— 不把它们当占位，同一个 id 就会被分两轮（第 3 轮的
            # VP-021/VP-022 在第 4 轮被又分了一次）。而这两个 id 上一轮已经
            # PASSED，resume 路径按 id 跳过执行，报告里留下的就是上一轮的
            # actual_result，被拿去和新 expected_result 比对后判成「验证对象
            # 错位」。占位只落在 delta state，**不改写基础计划**。
            reserved = allocated_vp_ids(state)
            # 2026-10-25（VP-031）：**发号账本**。
            #
            # 这些号已经发出去过 —— 有的落进了计划，有的没有（VP-031 那样
            # 被护栏整条丢掉，只剩一个号）。它们必须被避开：重新发同一个号，
            # 新验证点会继承那个号在 SQLite 里的旧裁决 —— 一次都没跑就记成
            # 失败，或者一次都没跑就被当成"验证通过"而跳过。
            #
            # 账本与上面那份占位读的是**同一个键**（``allocated_vp_ids``）：
            # 一份是过滤过的 id 集合（跳过非 VP-0NN 的脏值），一份是原样的
            # 列表。两边都要 —— 占位用来拒绝"复用旧号"，账本用来避开"发过的
            # 号"。
            avoid_ids = state.get("allocated_vp_ids") or []
            new_tasks, all_ids = collect_new_tasks(
                self.plan_dir, seen or [],
            )
            round_number = self._current_round or 1

            # 首轮（尚无基线）：只记录"已经看过哪些任务"，不评估——此时
            # 不存在"新增任务"，评估只会浪费一次 LLM 调用。
            if not seen:
                save_delta_state(self.plan_dir, {
                    "seen_task_ids": all_ids,
                    "round": round_number,
                    "allocated_vp_ids": merge_allocated_ids(state),
                })
                return {}

            if not new_tasks:
                save_delta_state(self.plan_dir, {
                    "seen_task_ids": all_ids,
                    "round": round_number,
                    "allocated_vp_ids": merge_allocated_ids(state),
                })
                return {}

            acceptance = collect_acceptance(self.plan_dir)
            raw = self.coding_tool.query_json(
                prompt=build_delta_prompt(
                    plan_data, new_tasks, acceptance, round_number,
                ),
                system_instruction=DELTA_SYSTEM_PROMPT,
                scene="verification_plan_delta",
            )
            delta = parse_delta_payload(raw, plan_data, reserved_ids=reserved)

            # 2026-10-22：增量必须**落盘**。
            #
            # 以前这里调的是裸 ``apply_delta`` —— 只改内存。复用路径跑完就
            # 返回，磁盘上的 ``verification_plan.json`` 一个字节都没动，于是
            # "作废"下一轮必然复活，替换 VP 也跟着消失。
            #
            # ``apply_delta_persisted`` 是"应用增量 + 让增量留下来"合成的一
            # 步：先在深拷贝上应用，校验通过后原子替换磁盘上的计划文件，
            # **只有写成功了**才让本函数手里的 ``plan_data`` 换内容。任何一
            # 步失败，磁盘与内存都保持调用前的原样 —— 内存永远不领先磁盘。
            try:
                summary = apply_delta_persisted(
                    self.verification_plan_file,
                    plan_data,
                    delta,
                    round_number,
                    post_apply=self._plan_delta_guard,
                    expected_digest=expected_digest,
                    avoid_ids=avoid_ids,
                    reserved_ids=reserved,
                )
            except PlanDeltaPersistError as exc:
                # 这次增量**没有**落盘（写失败，或计划在评估期间被外部改写）。
                # 磁盘与内存都还是原样：验证继续用现有计划（增量评估是增益，
                # 不是门禁），而且增量状态不更新 —— 任务不算"已看过"，下一
                # 轮会再评估一次，这次增量不会因为一次写失败就永远丢掉。
                logger.warning(
                    "[Verification] plan delta was NOT persisted: %s; "
                    "continuing with the existing plan", exc,
                )
                self._safe_log_freshness_event(
                    "__plan_delta__", "plan_delta_persist_failed",
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
                return {}

            # 摘要里带上"这次增量真的落盘了吗"：审计时能一眼分清"评估过"
            # 与"生效了"（全被拒的增量不写盘）。
            summary["persisted"] = bool(
                summary.get("added") or summary.get("obsoleted")
            )

            # 2026-10-25（VP-031）：发号账本。**本函数是这个状态文件的
            # 主人**（"看过哪些任务"每轮都写它），所以账本也在这里落盘 ——
            # 而且要写**合并后**的整份账本：只写这一轮新发的号会把之前
            # 发出去的号抹掉。
            ledger: List[str] = []
            for vp_id in list(state.get("allocated_vp_ids") or []) + list(
                summary.get("allocated_vp_ids") or []
            ):
                key = str(vp_id or "").strip()
                if key and key not in ledger:
                    ledger.append(key)

            save_delta_state(self.plan_dir, {
                "seen_task_ids": all_ids,
                "round": round_number,
                "last_delta": summary,
                # 本轮分配出去的 id 累加进去 —— 下一轮据此跳过它们，id 才
                # 跨轮单调（2026-10-24）。写的是**合并后**的整份账本：只写
                # 这一轮新发的号会把之前发出去的号抹掉。
                "allocated_vp_ids": ledger,
            })
            self._safe_log_freshness_event(
                "__plan_delta__", "plan_delta_evaluated", summary,
            )
            if summary["added"] or summary["obsoleted"]:
                print(
                    "[Verification] 计划增量评估："
                    f"新增 {len(summary['added'])} 条、"
                    f"废弃 {len(summary['obsoleted'])} 条"
                    + ("（已落盘）" if summary.get("persisted") else "")
                )
            return summary
        except Exception as exc:  # noqa: BLE001 — 评估失败绝不阻断验证
            logger.warning(
                "[Verification] plan delta evaluation failed: %s; "
                "continuing with the existing plan", exc,
            )
            self._safe_log_freshness_event(
                "__plan_delta__", "plan_delta_failed",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            return {}

    def _plan_delta_guard(
        self, candidate: dict, summary: Dict[str, Any],
    ) -> None:
        """增量护栏：在**落盘之前**判定新增的 VP 能不能用。

        它拿到的是 :func:`verification_plan_delta.apply_delta_persisted` 的
        **候选计划**（深拷贝），不是调用方手里那份 —— 护栏在写盘之前跑，
        所以"不合格的 VP"不会先落盘再被撤。

        判定内容与主生成路径一致：

        * ``api_test`` 的 request/assertions 必须过 schema；
        * ``verification_method`` 必须在 ``SUPPORTED_METHODS`` 里；
        * 不合格的**整条丢弃**（宁可少一条 VP，也不要一条永远跑不出结论的
          验收点），丢弃的 id 记进 ``summary["rejected_by_guard"]``；
        * 新增条目还要过阶段归一化（见下）。

        新增的 VP 必须和初次生成时一样过护栏（2026-09-18 定下）：每轮跑之前
        都要先过护栏，因为这一轮会生成新的 VP。护栏的内容随方法收敛而变：
        VP 不再有 test_command，所以查的是 **method 的契约** —— 不支持的
        method，以及 api_test 的 request/assertions 是否合规。
        """
        if not summary.get("added"):
            return

        added_ids = [a["id"] for a in summary["added"]]
        by_id = {
            str(vp.get("id")): vp
            for vp in candidate.get("verification_points") or []
            if isinstance(vp, dict)
        }
        candidates = {
            "verification_points": [by_id[i] for i in added_ids if i in by_id],
        }
        bad_ids = {f["id"] for f in self._api_schema_report(candidates)}
        from verification_subagent import SUPPORTED_METHODS
        for vp in candidates["verification_points"]:
            if str(vp.get("verification_method") or "") not in SUPPORTED_METHODS:
                bad_ids.add(str(vp.get("id")))
        if bad_ids:
            candidate["verification_points"] = [
                vp for vp in candidate.get("verification_points") or []
                if str(vp.get("id")) not in bad_ids
            ]
            summary["added"] = [
                a for a in summary["added"] if a["id"] not in bad_ids
            ]
            summary["rejected_by_guard"] = sorted(bad_ids)

        # 2026-09-19: 新增条目也要过阶段归一化。主生成路径本来就会跑它 ——
        # 那条"声明驱动"的规则是"Phase 1 上写了 e2e / full_ci 就说明它自己
        # 声明了'我是全量关卡'，就地提升到 Phase 2"。增量路径漏了这一步，
        # 于是一条整仓门禁型的新增会被按 Phase 1 排进去，在每条子功能都还
        # 没验完时就开跑，把整轮堵死（那一轮的「Nightly CI 全过」就是这个
        # 下场）。
        try:
            from verification_phases import (
                normalize_plan_phases as _normalize_phases,
            )
            _phase_summary = _normalize_phases(candidate)
            if _phase_summary.get("phase_promoted"):
                summary["phase_promoted"] = _phase_summary["phase_promoted"]
        except Exception:
            logger.warning(
                "[plan_delta] phase normalization failed; "
                "added VPs keep their declared phase",
                exc_info=True,
            )

    # -----------------------------------------------------------------------
    # Phase 1: Planning
    # -----------------------------------------------------------------------

    def generate_verification_plan(self, retry_llm: int = 3) -> dict:
        """
        Phase 1: Generate verification plan from PRD/arch/test documents.

        验证分支不走生成路径（2026-10-25）：那条路上读的是审核过的验证计划书
        翻译出来、并且过了等价门禁的机器计划 JSON —— 验证回路对它只有「读出
        来并校验」一件事。读不出来就报错（见
        :meth:`_load_verification_branch_plan`）。

        Args:
            retry_llm: Number of retries for LLM calls

        Returns:
            Verification plan dict
        """
        # 2026-10-25（验证分支输入路径）：验证分支的验证点集合不是这里生成
        # 的 —— 它是审核过的计划书翻译出来、逐条比对过的一份 JSON。生成它会
        # 把审核过的那批验证点换成一批谁都没审过的，而没有任何一步会告诉
        # 操作者这件事发生过。所以验证分支直接读盘上那份。
        if is_verification_branch(self.plan_dir):
            return self._load_verification_branch_plan()

        print("[Verification] Phase 1: Generating verification plan...")

        # Reuse the on-disk plan when present and structurally valid so a
        # restart after a failed round does NOT silently re-run the LLM
        # Phase-1 (which can introduce new VPs the executor hasn't seen
        # before and which the on-disk progress / executor state cannot
        # correlate). This is the canonical "skip already-run VPs"
        # optimisation: the disk plan is the LLM's last answer and is
        # at least as authoritative as a fresh regeneration.
        if self.verification_plan_file.exists():
            try:
                # 2026-10-22：读入时记下计划文件的 sha256，作为增量落盘的
                # ``expected_digest`` 透传下去（写回前复核一次：计划在增量
                # 评估期间被外部改写过就放弃这一轮增量，而不是覆盖它）。
                #
                # 摘要取自**本次读到的字节**，不是另算一次文件内容：另算会
                # 留下一个窗口 —— 文件在"读"与"算"之间被改写时，算出来的摘要
                # 描述的是新内容，写回时就会把别人的改动覆盖掉。
                _plan_bytes = self.verification_plan_file.read_bytes()
                expected_digest = hashlib.sha256(_plan_bytes).hexdigest()
                disk_plan = json.loads(_plan_bytes)
                if isinstance(disk_plan, dict) and disk_plan.get("verification_points"):
                    print(
                        f"[Verification] Reusing on-disk plan "
                        f"({len(disk_plan['verification_points'])} VPs) — skipping Phase-1 LLM"
                    )
                    # Apply the manual_check auto-downgrade pass even on
                    # the cache path: a plan cached before this fix landed
                    # carries the over-conservative manual_check labels,
                    # so the executor would otherwise SKIP them. Re-running
                    # the deterministic regex is cheap (microseconds) and
                    # idempotent.
                    disk_plan = self._normalize_legacy_methods(disk_plan)
                    # 2026-09-16: 阶段字段在复用路径上同样要归一化——老计划
                    # 没有 ``verification_phase``，一律按 Phase 1 处理。
                    # 归一化只让字段自洽，**不判定阶段**：阶段由规划 LLM 在
                    # 生成时给出（见 verification_phases 模块 docstring 里
                    # 关于字符匹配兜底被实测推翻的记录）。
                    _phase_summary = normalize_plan_phases(disk_plan)
                    self._last_phase_summary = _phase_summary
                    _log_phase_distribution("复用磁盘计划", _phase_summary)
                    # 2026-09-16：每轮验证开始前评估一次 VP 方案的增量。
                    # 修复轮新增的任务（repair-r*）不会自动带出 VP，没有这一步
                    # "修好了一个没人验的东西"在机制上不可见。评估只允许
                    # 新增/废弃，绝不动已有 VP（见 verification_plan_delta）。
                    #
                    # 2026-10-22：增量现在会**落盘**（此前只改内存，于是"作废"
                    # 下一轮必然复活）。写回前用读入时记下的 sha256 复核磁盘
                    # 上的计划没被别人改过。
                    self._maybe_evaluate_plan_delta(
                        disk_plan, expected_digest=expected_digest,
                    )
                    # 2026-09-18 C1：服务声明在增量评估**之后**解析，
                    # 这样 delta 新加进来的 VP 也会被引用检查覆盖。
                    self._normalize_service_declarations(disk_plan)
                    # 2026-09-18（D5）：复用路径上**只标注、不重问**。
                    # 磁盘上的计划是上一轮已经跑过的东西，重生成会引入
                    # 执行器没见过的 VP，而 resume / 增量语义都建在"计划
                    # 稳定"之上。但缺口必须可见 —— 它决定了下一轮是不是
                    # 少跑了一道关。
                    _reused_missing = find_missing_gates(
                        disk_plan, detect_full_gate_entries(self.project_dir),
                    )
                    if _annotate_gate_completeness(disk_plan, _reused_missing):
                        logger.warning(
                            "[Verification] reused plan is missing %d Phase-2 "
                            "gate(s): %s",
                            len(_reused_missing),
                            [m.evidence for m in _reused_missing],
                        )
                        self._safe_log_freshness_event(
                            "__plan__", "phase2_gate_missing_on_reused_plan",
                            {"missing": [m.to_dict() for m in _reused_missing]},
                        )
                    # 2026-09-27 api_test schema gate on the load path.
                    # A persisted plan whose api_test VPs fail schema
                    # validation against the *current* ``verify_vp``
                    # vocabulary cannot be re-used — the runner will
                    # mark every such VP FAILED regardless of the live
                    # service. Catching the issue here (instead of at
                    # execution time) surfaces the drift to the operator
                    # who decides whether to re-generate or hand-edit.
                    _disk_api_violations = self._api_schema_report(disk_plan)
                    if _disk_api_violations:
                        _ids = ", ".join(
                            v["id"] for v in _disk_api_violations
                        )
                        _first_issues = "; ".join(
                            f"{v['id']}: {d}"
                            for v in _disk_api_violations
                            for d in v["issues"]
                        )
                        raise ApiTestSchemaViolation(
                            f"on-disk plan has api_test VPs that fail schema "
                            f"validation (runner vocabulary drift, hand "
                            f"edit, or stale persisted plan): offending VPs: "
                            f"{_ids}. First issues: {_first_issues}",
                            violations=_disk_api_violations,
                        )
                    return disk_plan
            except (OSError, json.JSONDecodeError) as e:
                print(f"[Verification] Disk plan unreadable, regenerating: {e}")

        # Load documents
        documents = self._load_documents()

        # Generate plan using LLM
        prompt = self._build_planning_prompt(documents)

        # Plan generation reads full PRD/arch/test docs, so the LLM needs
        # headroom. 2026-09-15: do NOT pass ``timeout`` —
        # the call inherits the unified coding-tool rules (900s adaptive
        # silence watcher: it only fires when NO stdout line arrives for
        # the whole window, so a streaming call never trips it; plus the
        # 1800s idle-pipe guard and the verification layer's 1-hour
        # ceiling). An explicit value REPLACES that window instead of
        # nesting inside it, which is how healthy-but-slow calls died.
        #
        # 2026-09-15 (bounded-command guard): ``base_prompt`` is kept
        # pristine so a rejected plan can be re-asked with the violation
        # list appended, without accumulating feedback across attempts.
        base_prompt = prompt
        #: 拿到过的「违规最少」的那一版计划。重试若更差就回退到它——
        #: 反馈重试有可能把一份只有 1 个无界 VP 的计划，换成一份有 3 个的。
        _best_plan: dict | None = None
        _best_violations: int | None = None
        # 2026-09-18（D5）：全量关卡的存在性是**项目事实**，与本次生成无关，
        # 所以在循环外探测一次即可（几十次 stat）。缺失的关卡进与「服务引用
        # 违规」「api_test 断言不合规」同一条重生成回路。
        _gate_entries = detect_full_gate_entries(self.project_dir)
        for attempt in range(retry_llm):
            try:
                plan_data = self.coding_tool.query_json(
                    prompt=prompt,
                    system_instruction=VERIFICATION_PLAN_SYSTEM_PROMPT,
                )

                # Validate structure
                if "verification_points" not in plan_data:
                    raise ValueError("Missing verification_points in response")

                # 2026-09-18（method 收敛）：`manual_check` 与
                # `automated_test` 都已退役。前者必然失败（parse_verdict
                # 把 SKIPPED 无条件强转 FAILED），后者是执行阶段自测的重复
                # 且要为此拉一个完整子 agent。用统一的老方法归一化处理：
                # 标注 obsolete + 大声记录，而不是就地改写（VP 的字段不可变）。
                plan_data = self._normalize_legacy_methods(plan_data)

                # 2026-09-18 C1：解析并校验计划级 `services` 声明。
                # 放在这里（早于护栏与阶段归一化）是因为后续所有基于端口
                # 的判定都要读它；声明本身非法只会丢弃该条目，不会中断
                # 生成。
                plan_data = self._normalize_service_declarations(plan_data)

                # 2026-09-16: 阶段归一化（两阶段迭代）。
                #
                # **判定权在规划 LLM**——它在同一个生成调用里已经写下了每个
                # VP 的 ``verification_phase``（见 prompts 的"两阶段结构"）。
                # 这里只做字段自洽，不做任何字符匹配推断。第一版曾按命令/
                # 标题特征做"确定性提升"，on that plan把 9 条本该留在
                # Phase 1 的 VP 误提升（裸 `e2e` 命中路径 `tests/e2e/*.spec.ts`、
                # 标题 `端到端` 把方法词当范围词），Phase 2 从 2 条涨到 11 条。
                # 详见 verification_phases 模块 docstring。
                #
                # 必须在护栏之前跑：护栏对 Phase 2（全量关卡）豁免，
                # 而"哪些 VP 是 Phase 2"由模型标注决定。
                _phase_summary = normalize_plan_phases(plan_data)
                _log_phase_distribution("生成路径", _phase_summary)
                self._last_phase_summary = _phase_summary

                # 2026-09-15: reject acceptance commands
                # that would run an unbounded test tree — the whole suite,
                # ``cargo test --workspace``, bare ``pytest``.
                #
                #     "我觉得以后 VP 其实没有必要去跑整个 nightly CI，
                #      太久了。把门禁的 CI 跑过就可以。"
                #
                # Why mechanical, when the prompt already says so:
                # ``VERIFICATION_PLAN_SYSTEM_PROMPT`` has carried a
                # "single test_command ≤15 min" rule the whole time, and
                # commands that blow that budget ship anyway — the prompt
                # says it and nothing enforces it. The prompt is not a
                # gate, and reading it as one is what let the overruns
                # through.
                #
                # The guard only REPORTS — it never rewrites a command.
                # Guessing a "correct" command in the LLM's place is its
                # own kind of overreach: VP-013's ``cargo test --lib
                # <integration test>`` was exactly that, and it took a
                # whole round plus a 51-minute repair execution before the
                # system noticed its own spec was wrong.
                # 2026-09-18：VP 侧的「有界命令」门禁整条撤除 —— VP 不再有
                # test_command，护栏没有对象可查。命令有界性仍然管**任务**的
                # test_command（``verification_command_guard`` 由
                # ``test_command_quality`` 复用），只是不再管 VP。
                #
                # 2026-09-18 C2：服务引用违规（写死已声明端口 / 引用了不存在
                # 的服务或字段）。C1 只做检测并把问题挂在 VP 上，这里把它接进
                # 同一条重生成回路 —— 否则一份「每条 VP 各自 nohup 起服务」的
                # 计划会照原样落盘，preflight 起的那个实例被三条 VP 各起一份
                # 抢端口，正是那一轮 VP-019/020/021 的场面。
                _service_violations = self._service_reference_report(plan_data)
                # 2026-09-18（VP 判定重构）：api_test 由框架执行，断言不合规
                # 的计划跑起来只会烧掉一轮。跟上面两类走同一条重生成回路。
                _api_schema_violations = self._api_schema_report(plan_data)
                # 2026-09-18（D5）：项目里有全量 CI / E2E 入口，计划里却没有
                # 对应的 Phase 2 关卡 —— that run's Nightly CI 关卡就是这么丢的。
                _gate_gaps = find_missing_gates(plan_data, _gate_entries)
                # 2026-10-06：PRD 验收标准里有、计划里没有任何 VP 引用它。
                # 同一条重生成回路。that run 的「FD 传递·多级」在计划阶段
                # 一条 VP 都没有，靠执行期的增量评估才补上 —— 也就是说测
                # 试设计阶段漏掉一整条验收标准，报告仍然 PASSED。
                _acceptance_items = self._collect_prd_acceptance()
                _ac_gaps = (
                    find_uncovered_criteria(plan_data, _acceptance_items)
                    if _acceptance_items else []
                )
                _violation_count = (
                    len(_service_violations) + len(_api_schema_violations)
                    + len(_gate_gaps) + len(_ac_gaps)
                )
                if _violation_count:
                    _sections: List[str] = []
                    if _service_violations:
                        _sections.append(
                            "### 服务引用违规\n"
                            + "\n".join(
                                f"  - {f['id']}「{f['title']}」"
                                + "".join(
                                    f"\n      · {d}" for d in f["issues"]
                                )
                                for f in _service_violations
                            )
                        )
                    if _api_schema_violations:
                        _sections.append(
                            "### api_test 断言不合规\n"
                            + "\n".join(
                                f"  - {f['id']}「{f['title']}」"
                                + "".join(
                                    f"\n      · {d}" for d in f["issues"]
                                )
                                for f in _api_schema_violations
                            )
                        )
                    if _gate_gaps:
                        _sections.append(
                            render_gap_feedback(_gate_gaps)
                        )
                    if _ac_gaps:
                        _sections.append(
                            render_acceptance_gap_feedback(_ac_gaps)
                        )
                    _report = "\n".join(_sections)
                    print(
                        f"[Verification] {_violation_count} verification "
                        f"point(s) violate the plan contract "
                        f"({len(_service_violations)} service reference "
                        f"issue(s), {len(_api_schema_violations)} api_test "
                        f"schema issue(s), {len(_gate_gaps)} missing "
                        f"Phase-2 gate(s), {len(_ac_gaps)} uncovered PRD "
                        f"acceptance criterion/criteria):\n{_report}"
                    )
                    if (
                        _best_violations is None
                        or _violation_count < _best_violations
                    ):
                        _best_plan = plan_data
                        _best_violations = _violation_count
                    if attempt < retry_llm - 1:
                        print(
                            "[Verification] re-generating with the "
                            f"violation list fed back "
                            f"(attempt {attempt + 2}/{retry_llm})"
                        )
                        prompt = (
                            base_prompt
                            + "\n\n## 上一版计划被护栏拒绝\n\n"
                            + _report
                            + "\n\n请针对上面每一条修正，重新输出完整 JSON"
                            + "（其余部分保持不变）。"
                            + self._violation_fix_guidance(
                                _service_violations, _api_schema_violations,
                                _gate_gaps,
                            )
                        )
                        continue
                    # 重试额度用尽：先看 api_test 断言是否合规。
                    # 服务引用违规 / Phase 2 缺关卡可以走"最不坏"路径——
                    # 这两类**不会**让框架无法跑 VP。
                    # 但 api_test 断言不合规时，运行器认不出该主语或比较符，
                    # 该 VP 永远判不了，必须**回到生成方**，不允许带着
                    # 这条 VP 进入执行阶段（VP-007 就是这样走过了一条回路
                    # 才被记 FAILED）。
                    if _api_schema_violations:
                        # 不调用 plan_data = _best_plan / 不 ``continue``
                        # 到下面的写盘——直接抛异常，调用方必须看到。
                        _ids = ", ".join(
                            v["id"] for v in _api_schema_violations
                        )
                        _first_issues = "; ".join(
                            f"{v['id']}: {d}"
                            for v in _api_schema_violations
                            for d in v["issues"]
                        )
                        raise ApiTestSchemaViolation(
                            f"api_test VPs failed schema validation after "
                            f"{retry_llm} retries — refusing to persist or "
                            f"execute a plan the deterministic runner cannot "
                            f"grade. Offending VPs: {_ids}. First issues: "
                            f"{_first_issues}",
                            violations=_api_schema_violations,
                        )
                    # 重试额度用尽：保存「最不坏」的那一版，并把违规标注
                    # 留在计划里（``annotate_plan`` 已就地写入
                    # ``command_guard``），便于操作者一眼看到。
                    print(
                        "[Verification] out of retries — keeping the plan "
                        "with the fewest violations "
                        f"({_best_violations}); it stays annotated"
                    )
                    plan_data = _best_plan if _best_plan else plan_data
                else:
                    _best_plan = plan_data

                # Enrich each VP with execution metadata (timeout_seconds
                # + execution_group) so the orchestrator and the bridge
                # UI can render a per-VP execution budget without
                # re-walking the plan. The enrichment is deterministic
                # and uses the same TimeoutPolicy the executor uses, so
                # a VP's planned timeout always matches the executor's
                # resolved timeout.
                plan_data = self._enrich_vps_with_execution_metadata(plan_data)

                # 2026-09-18（D5）：重试额度用尽后仍然缺关卡 → 标注在计划上
                # （``phase2_gap``）并大声记录。计划照存 —— 一份能跑的计划
                # 好过没有计划 —— 但操作员必须能一眼看到这一轮少了哪道关，
                # 而不是从一份"全绿"的报告里反推。
                _missing_gates = find_missing_gates(plan_data, _gate_entries)
                if _annotate_gate_completeness(plan_data, _missing_gates):
                    logger.warning(
                        "[Verification] plan is missing %d Phase-2 gate(s) "
                        "the project has entry points for: %s",
                        len(_missing_gates),
                        [m.evidence for m in _missing_gates],
                    )

                # 同理：重试额度用尽后仍有 PRD 验收标准没有任何 VP 覆盖 →
                # 标注在计划上（``acceptance_gap``）并大声记录。这不是
                # 阻断性的 —— 计划照存，VP 照跑 —— 但"这轮少验了一条验收
                # 标准"必须写在计划里，而不是让人从一份全绿的报告反推。
                _uncovered = find_uncovered_criteria(
                    plan_data, self._collect_prd_acceptance(),
                )
                if _annotate_acceptance_gap(plan_data, _uncovered):
                    logger.warning(
                        "[Verification] plan leaves %d PRD acceptance "
                        "criterion/criteria unverified by any VP: %s",
                        len(_uncovered),
                        [m.evidence for m in _uncovered],
                    )
                    print(
                        f"[Verification] WARNING: {len(_uncovered)} PRD "
                        f"acceptance criterion/criteria have no VP — "
                        f"recorded in the plan as acceptance_gap:\n"
                        + "\n".join(f"  - {m.evidence}" for m in _uncovered)
                    )

                # Save plan
                self.verification_plan_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.verification_plan_file, "w", encoding="utf-8") as f:
                    json.dump(plan_data, f, indent=2, ensure_ascii=False)

                # NOTE: legacy double-write to
                # ``plan_dir/project/verification_plan.json`` was removed
                # (task #3.6). The verification-point test commands use
                # the ``project/`` prefix via the executor's path
                # resolution layer; that layer must now derive the plan
                # from ``self.verification_plan_file`` (the canonical
                # location) instead of from ``project/`` — see
                # ``verification_executor._select_pending_vps``.
                print(f"[Verification] Generated {len(plan_data['verification_points'])} verification points")
                return plan_data

            except ApiError as e:
                print(f"[Verification] LLM API error (attempt {attempt + 1}/{retry_llm}): {e}")
                if attempt == retry_llm - 1:
                    raise
            except ApiTestSchemaViolation:
                # Hard gate: a plan with api_test VPs the runner cannot
                # grade must NEVER fall back to a minimal plan or get
                # saved as 'best of bad'. Surface the violation straight
                # through so the operator sees exactly which spellings
                # the LLM produced and which the runner rejects (a
                # downstream of the 2026-09-27 contract: the framework
                # refuses to ship an unjudgeable plan).
                raise
            except Exception as e:
                print(f"[Verification] Error generating plan (attempt {attempt + 1}/{retry_llm}): {e}")
                if attempt == retry_llm - 1:
                    # Return minimal plan on total failure
                    return self._generate_minimal_plan(documents)

        return self._generate_minimal_plan(documents)

    def _load_verification_branch_plan(self) -> dict:
        """验证分支的机器计划：读盘上那一份，校验它，**不问模型**。

        与开发分支的「磁盘复用」路径相比，只有三处不同，其余处理一致：

        * **不退回 LLM** —— 读不出来 / 校验不过一律抛错；
        * **不做增量评估**（``_maybe_evaluate_plan_delta``）—— 增量条目是
          「这轮多了什么任务」的产物，而任务清单是开发分支的产物；验证分支
          没有任务清单，凭空加出来的验证点也不在审核过的计划书里；
        * **不做方法归一化**（``_normalize_legacy_methods``）—— 那一趟是为
          「模型生成的计划」准备的：模型用的是框架的方法词表
          （``api_test`` / ``code_review`` / …），词表外的方法说明它是早就
          退休的方法。而验证分支的机器计划里，方法字段是**计划书里人写的
          自由文本**（"单元测试" / "以手工核验为主"），它与框架词表之间的
          映射是另一件事。拿退休检查去处理它，会把审核过的每一条验证点都
          标成 ``obsolete`` —— 验证回路于是"跑完了"，一条也没验。

        盘上那份**原样留着**：它是审核过的产物，这一趟只读不写。

        Returns:
            盘上那份计划（就地归一化过阶段字段与服务声明）。

        Raises:
            VerificationBranchPlanError: 计划文件不存在 / 读不出来 / 不是
                一个对象 / 没有验证点 / 有一条验证点没有 id。
            ApiTestSchemaViolation: ``api_test`` 验证点的断言不合规 ——
                运行器认不出来，那条 VP 永远判不了。
        """
        path = self.verification_plan_file
        if not path.exists():
            raise VerificationBranchPlanError(
                f"验证分支缺少机器计划：{path} 不存在。验证分支的验证点集合"
                "来自审核过的验证计划书，不退回 LLM 重新生成。"
            )

        try:
            with open(path, "r", encoding="utf-8") as f:
                plan = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise VerificationBranchPlanError(
                f"验证分支的机器计划读不出来：{path} —— {exc}"
            ) from exc

        if not isinstance(plan, dict):
            raise VerificationBranchPlanError(
                f"验证分支的机器计划不是一个对象：{path}"
            )

        points = plan.get("verification_points")
        if not isinstance(points, list) or not points:
            raise VerificationBranchPlanError(
                f"验证分支的机器计划里没有验证点数组（verification_points）："
                f"{path}"
            )
        for vp in points:
            if not isinstance(vp, dict) or not str(vp.get("id") or "").strip():
                raise VerificationBranchPlanError(
                    f"验证分支的机器计划里有一条验证点没有 id：{path}"
                )

        # 阶段归一化（纯函数：不问模型、不碰磁盘）。老计划没有
        # ``verification_phase``，一律按 Phase 1 处理。
        _phase_summary = normalize_plan_phases(plan)
        self._last_phase_summary = _phase_summary
        _log_phase_distribution("验证分支磁盘计划", _phase_summary)

        # 服务声明：解析 + 引用检查。执行器解析 ``{{svc.<name>.<field>}}``
        # 占位符时读的就是这里解析出来的声明 —— 不跑这一趟，占位符永远解析
        # 不成，api_test 验证点会对着字面量 ``{{svc.quota.url}}`` 发请求。
        self._normalize_service_declarations(plan)

        # api_test 断言契约：运行器认不出来的 VP 一条都不许进执行阶段
        # （它会被永远判成 FAILED —— 那是计划缺陷，不是产品缺陷）。
        _api_violations = self._api_schema_report(plan)
        if _api_violations:
            _ids = ", ".join(v["id"] for v in _api_violations)
            _first_issues = "; ".join(
                f"{v['id']}: {d}"
                for v in _api_violations
                for d in v["issues"]
            )
            raise ApiTestSchemaViolation(
                f"验证分支的机器计划里 api_test 验证点不合规（运行器词表漂移"
                f"或手工改动）：{_ids}. First issues: {_first_issues}",
                violations=_api_violations,
            )

        print(
            f"[Verification] Verification branch: loaded "
            f"{len(points)} VPs from {path.name} — no Phase-1 LLM call"
        )
        return plan

    def _enrich_vps_with_execution_metadata(self, plan_data: dict) -> dict:
        """Annotate each verification point with ``timeout_seconds`` and ``execution_group``.

        Two fields are added in-place on every VP:

        * ``timeout_seconds`` — the per-VP timeout as resolved by
          :class:`TimeoutPolicy` (per-VP override > per-method default
          > global default). Mirrors the executor's ``wait_for`` boundary
          so the saved plan is the single source of truth.
        * ``execution_group`` — the 0-based index of the group this VP
          belongs to in :class:`ExecutionProfileGenerator`'s group
          order. VPs sharing an index run in the same concurrency slot.

        The enrichment is a pure function over ``plan_data`` and the
        agent's :attr:`timeout_policy` — no I/O, no LLM. A plan with no
        ``verification_points`` (or non-dict entries) is returned
        unchanged so this method is safe to call on partially-parsed
        LLM output.

        Required field validation: For ``automated_test`` VPs, ``test_command``
        is mandatory; ``title`` and ``expected_result`` are mandatory for all VPs.
        Missing fields are filled with sensible defaults to prevent downstream
        failures from empty LLM output.
        """
        verification_points = plan_data.get("verification_points") if isinstance(plan_data, dict) else None
        if not isinstance(verification_points, list) or not verification_points:
            return plan_data

        # Build a vp_id → group_index map by walking the profile's
        # group_profiles (which preserves first-seen method order).
        #
        # No capacity rides along, and this is the whole story now: the
        # round has no width to declare, so the value object is not told
        # one. It used to be handed ``self._live_vp_capacity()`` here —
        # a live read whose only purpose was to stop the profile
        # publishing some other number as if it were the width, and with
        # ``parallelism_cap`` gone there is no other number left to
        # contradict. Passing nothing also saves a CC Switch round-trip
        # on a path that only needs group indices.
        profile = ExecutionProfileGenerator(
            plan_data,
            timeout_policy=self.timeout_policy,
        ).build()
        vp_to_group_index: Dict[str, int] = {}
        for group_index, group in enumerate(profile.get("group_profiles", []) or []):
            for vp_id in group.get("vp_ids", []) or []:
                vp_to_group_index[str(vp_id)] = int(group_index)

        for vp in verification_points:
            if not isinstance(vp, dict):
                continue
            method = str(vp.get("verification_method", "") or "").strip() or "manual_check"
            vp_id = str(vp.get("id", "") or "")

            # Required field validation: fill defaults for empty fields
            if not vp.get("title"):
                vp["title"] = f"验证点 {vp_id}"
            if not vp.get("expected_result"):
                vp["expected_result"] = "验证通过"
            # 2026-09-18（method 收敛）：`echo 'Basic functionality check'`
            # 兜底整条删除。它退出 0 却什么都没验，而零测试门禁随后必然把
            # 这样一条 VP 判死 —— an earlier plan 28 条 VP 里有 10 条是这么来的，
            # 且从生成那一刻起就注定 FAILED。现在缺命令/缺断言的 VP 会被
            # `_api_schema_report`（api_test）或 `_normalize_legacy_methods`
            # （退役 method）显式报出来，不再用假命令糊过去。

            vp["timeout_seconds"] = int(self.timeout_policy.resolve(method))
            vp["execution_group"] = int(vp_to_group_index.get(vp_id, 0))

        return plan_data

    def _load_documents(self) -> Dict[str, str]:
        """Load PRD, architecture, and test design documents."""
        documents = {}

        # Try PRD JSON first, fallback to markdown
        if self.prd_json_file.exists():
            with open(self.prd_json_file, "r", encoding="utf-8") as f:
                prd_data = json.load(f)
            documents["prd"] = json.dumps(prd_data, ensure_ascii=False, indent=2)
        elif self.prd_file.exists():
            with open(self.prd_file, "r", encoding="utf-8") as f:
                documents["prd"] = f.read()

        # Architecture design
        if self.arch_file.exists():
            with open(self.arch_file, "r", encoding="utf-8") as f:
                documents["arch"] = f.read()

        # Test design
        if self.test_file.exists():
            with open(self.test_file, "r", encoding="utf-8") as f:
                documents["test"] = f.read()

        return documents

    #: 语义审计一次最多送多少个 VP 进去（防止单次 prompt 过长）。
    _SEMANTIC_AUDIT_BATCH = 12

    def _llm_audit_commands(self, plan_data: dict) -> List[Dict[str, Any]]:
        """LLM semantic audit of VP acceptance commands.

        **No longer on any live path (2026-09-18).** VP 已经没有
        ``test_command`` 了，这个方法没有对象可查，生成回路与增量回路都不再
        调用它。保留而不删除是因为它连同 :mod:`verification_command_guard`
        是一套完整、有测试的护栏（静态形态 + LLM 语义两层），删掉它是一个
        独立的判断：要么彻底退役这套护栏，要么让 ``evidence_command`` 之类
        的 VP 侧命令重新用上它。等这个决定做了再删，别顺手删。

        The method is kept working (its tests still pass) so that decision
        stays cheap to reverse.
        """

        """对静态层 abstain 的命令做一次 LLM **语义**审计（2026-09-15）。

        当固定代码无法判断一条验收命令是否真的界定了范围时，就只能读懂它
        再去判断。这类判断的要求高，所以走 high 档（``HiProvider`` 在旧
        命名下对应的档位），通过 per-call 场景覆盖请求。

        触发条件是 :func:`needs_semantic_review` —— 命令里没有任何认得出来的
        测试 runner，静态规则无从判断它的范围。典型是自定义脚本 / shell 审计：
        VP-020 的 ``python tools/verify_fixture_integrity.py`` 退出 0 但采样
        0 个 fixture；VP-017 的 ``grep ... frontend/ || echo CLEAN`` 扫一个
        不存在的目录还把退出码吞掉。**两者都不是"无界"，是"看起来跑了、其实
        什么都没验"** —— 形态检查抓不到，只能读懂语义。

        走 ``verification_command_audit`` 场景（``provider_routing.yaml``
        映射到 ``high`` 档）。

        返回与 :func:`annotate_plan` 同形状的 findings，并**就地**给判定无效的
        VP 写 ``command_guard``。审计是**增益**：provider 不可用、返回不是
        JSON、判定结果缺字段 —— 任何失败都静默跳过，绝不能让计划生成因此挂掉。
        """
        points = plan_data.get("verification_points")
        if not isinstance(points, list):
            return []

        # 2026-09-16: Phase 2 豁免语义审计。全量关卡（nightly CI / 全量 E2E）
        # 的"范围"本来就是整仓，语义审计按"这条命令证明得了它的断言吗"去判
        # 会把合法的全量命令判成无效，与提示词要求直接冲突。
        from verification_phases import is_final_gate

        candidates = [
            vp for vp in points
            if isinstance(vp, dict)
            and not is_final_gate(vp)
            and _needs_semantic_review(vp.get("test_command"))
        ]
        if not candidates:
            return []

        findings: List[Dict[str, Any]] = []
        for start in range(0, len(candidates), self._SEMANTIC_AUDIT_BATCH):
            findings.extend(
                self._audit_command_batch(
                    candidates[start:start + self._SEMANTIC_AUDIT_BATCH]
                )
            )
        return findings

    def _audit_command_batch(
        self, batch: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """审计一批 VP；返回被判 ``bounded: false`` 的那些。"""
        payload = {
            "verification_points": [
                {
                    "id": vp.get("id"),
                    "title": vp.get("title"),
                    "expected_result": vp.get("expected_result"),
                    "test_command": vp.get("test_command"),
                }
                for vp in batch
            ],
        }
        try:
            # 复用 agent 自己的 coding_tool，只做 **per-call 场景覆盖**。
            #
            # 早先的写法是另起一个 ``create_coding_tool(scene=...)`` —— 那在
            # 测试里会建出**真实**工具、真的打一次 provider（实测发生过，
            # 单测里跑出了真实的 LLM 判定）。复用同一个工具就没有这个缝。
            #
            # 不传 timeout —— 继承统一的 900s 静默 / 1800s idle / 1h 规则
            # （2026-09-15，：显式值会**替换**而不是嵌套那套窗口）。
            result = self.coding_tool.query_json(
                prompt=json.dumps(payload, ensure_ascii=False, indent=2),
                system_instruction=VERIFICATION_COMMAND_AUDIT_SYSTEM_PROMPT,
                scene="verification_command_audit",
            )
        except Exception as exc:  # noqa: BLE001 — 审计失败绝不阻断计划生成
            print(
                "[Verification] command audit skipped "
                f"({type(exc).__name__}: {exc})"
            )
            return []

        if not isinstance(result, dict):
            return []
        by_id = {
            str(item.get("id")): item
            for item in (result.get("results") or [])
            if isinstance(item, dict)
        }

        findings: List[Dict[str, Any]] = []
        for vp in batch:
            verdict = by_id.get(str(vp.get("id")))
            # 只有**明确的 false** 才作数：字段缺失、类型不对、true —— 一律
            # 当没判过。宁可漏报也不误报（误报会让 LLM 为过检查空转）。
            if not isinstance(verdict, dict) or verdict.get("bounded") is not False:
                continue
            reason = str(verdict.get("reason") or "").strip() or "(未给理由)"
            violation = {
                "code": "llm_semantic_audit",
                "detail": (
                    "语义审计判定这条命令不能真正验证该断言："
                    f"{reason}。请改写成一条确实会检查这条断言的命令。"
                ),
            }
            vp["command_guard"] = {"violations": [violation]}
            findings.append({
                "id": vp.get("id"),
                "title": vp.get("title"),
                "test_command": vp.get("test_command"),
                "violations": [violation["code"]],
            })
        return findings

    def _build_planning_prompt(self, documents: Dict[str, str]) -> str:
        """Build prompt for verification plan generation."""
        parts = []

        if "prd" in documents:
            parts.append(f"## PRD 文档\n\n{documents['prd']}")

        if "arch" in documents:
            parts.append(f"## 架构设计文档\n\n{documents['arch']}")

        if "test" in documents:
            parts.append(f"## 测试设计文档\n\n{documents['test']}")

        if not parts:
            return "请基于当前项目生成基础验证计划。"

        return "\n\n".join(parts)

    def _generate_minimal_plan(self, documents: Dict[str, str]) -> dict:
        """Fallback plan for when Phase 1 planning fails outright.

        2026-09-18: this used to emit a single ``automated_test`` VP whose
        ``test_command`` was ``echo 'Basic functionality check'``. That
        method is retired, and the echo proved nothing — it exited 0 and
        the zero-tests gate then failed the VP, so the round failed for a
        reason nobody could act on.

        The honest shape of "we could not plan this round" is a VP that
        says so. ``code_review`` is used because it is the one remaining
        method that can be judged without a runnable artifact, and its
        criterion here is literally "planning failed", which a reviewer
        will correctly grade FAILED.
        """
        plan = {
            "verification_points": [
                {
                    "id": "VP-001",
                    "title": "验证计划生成失败",
                    "related_prd_criteria": "(无法生成验证计划)",
                    "verification_method": "code_review",
                    "priority": "high",
                    "expected_result": (
                        "本轮的验证计划未能生成（Phase 1 LLM 多次失败）。"
                        "本 VP 存在的唯一目的是把这一失败显式暴露出来，"
                        "而不是让它伪装成一次通过。"
                    ),
                }
            ],
            "plan_generation_failed": True,
        }
        print(
            "[Verification] WARNING: falling back to the minimal plan — "
            "Phase 1 could not produce a real verification plan this round"
        )
        return self._enrich_vps_with_execution_metadata(plan)

    # -----------------------------------------------------------------------
    # Phase 2: Execution
    # -----------------------------------------------------------------------

    def start_verification_round(self, round_number: int) -> Path:
        """
        Start a new verification round. Creates log file and archives old data.

        Args:
            round_number: Current round number (1-indexed)

        Returns:
            Path to the new log file
        """
        self._current_round = round_number
        # 2026-10-25 (repair-r1-26): the environment verdict is a
        # **round** verdict.  Last round's blocked round says nothing
        # about this one — the service may be up now — so the record
        # starts empty every round, exactly like the round's other
        # per-round state.
        self._environment_blocked = False
        self._environment_blocked_details = []
        self._environment_blocked_event_written = False
        # 2026-10-25 (repair-r1-32): the one automatic re-run is a
        # **round's** allowance — a new round gets a new one.
        self._environment_rerun_attempted = False
        # 2026-10-26: the round's own evidence is checked **once** per
        # round.  A new round gets a new check: last round's evidence
        # says nothing about this one.
        self._round_evidence_reconciled = False
        # 2026-10-25 (repair-r1-31): the address record is a **round's**
        # record.  Last round's addresses say nothing about this one —
        # the service may be on another port now — so the table starts
        # empty, and the round gets its own request id to hang its
        # evidence on.
        self._vp_address_records = {}
        self._vp_attempts = {}
        self._observed_instances = {}
        self._round_request_id = _mint_request_id(
            self.plan_dir.name, round_number,
        )
        return self.persistence.start_round(round_number)

    def execute_verification_plan(self, plan_data: Optional[dict] = None) -> dict:
        """
        Phase 2: Execute verification plan (sync entry point).

        Args:
            plan_data: Optional verification plan (loads from file if not provided)

        Returns:
            Execution results dict with verification point results

        This is the synchronous wrapper around :meth:`execute_verification_plan_async`
        so existing call-sites (and tests) that treat verification as a
        blocking step keep working unchanged. The async version is the
        canonical implementation that groups VPs by method and runs
        each group concurrently behind a gate sized from the live
        provider fleet.

        As with :meth:`_execute_verification_point`, when this method
        is called from inside a running event loop (under
        ``pytest-asyncio`` or any async context), ``asyncio.run``
        would raise. We fall back to ``nest_asyncio`` (now installed
        in the dev environment) which patches the running loop to
        allow nested ``run_until_complete`` calls.
        """
        if self.persistence._log_file is None:
            self.start_verification_round(self._current_round or 1)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.execute_verification_plan_async(plan_data))
        # Loop is running: schedule on it and wait via run_coroutine_threadsafe
        # (which works even from the same thread for completion
        # notification).
        import nest_asyncio  # type: ignore
        nest_asyncio.apply()
        return asyncio.run(self.execute_verification_plan_async(plan_data))

    async def execute_verification_plan_async(
        self, plan_data: Optional[dict] = None
    ) -> dict:
        """Async version of :meth:`execute_verification_plan`.

        .. warning::
           **Legacy path — not the live verification round.** The live
           round is :meth:`run_full_verification`, which goes through
           :class:`VerificationExecutor` and builds no width at all
           (2026-10-13). This method is kept because tests and e2e still
           drive it, and deleting it was not in scope. It retains a
           gate-based bound, so its width semantics are *not* the live
           round's.

        Pipeline:

        1. Load the plan (or use the one passed in).
        2. :meth:`_partition_vps_by_method` buckets VPs by their
           ``verification_method`` field, preserving first-seen order.
        3. ``manual_check`` groups short-circuit to ``SKIPPED`` and
           never enter the gather / gate path.
        4. Each remaining group is dispatched via
           :func:`asyncio.gather` so multiple groups run in parallel,
           and the in-flight VPs are rate-limited by two bounds, both
           re-read every time either binds: the plan-wide one and each
           group's own. Both come from the verification scene's
           providers — how many VPs the fleet can actually serve this
           second — and both fall back to the declared fleet ceiling
           only when that scene cannot be resolved at all
           (:func:`_verification_vp_capacity`).
        5. :meth:`_execute_group` emits a ``group_started`` and
           ``group_completed`` event on the persistence log so
           downstream observers (the JSONL log parser, the
           ExecutionProfileGenerator, the bridge UI) can correlate
           per-VP events with the group they ran in.
        """
        print("[Verification] Phase 2: Executing verification plan...")

        if plan_data is None:
            if not self.verification_plan_file.exists():
                raise FileNotFoundError("Verification plan not found. Run generate_verification_plan first.")
            with open(self.verification_plan_file, "r", encoding="utf-8") as f:
                plan_data = json.load(f)

        verification_points = plan_data.get("verification_points", [])

        groups = self._partition_vps_by_method(plan_data)

        all_results: List[Dict[str, Any]] = []
        # Track per-VP errors so a single misbehaving VP doesn't
        # bring the whole batch down — the same defensive policy as
        # the legacy sync loop.
        for vp in verification_points:
            vp_id = vp.get("id", "unknown")
            method = vp.get("verification_method", "manual_check")
            print(f"[Verification] Executing {vp_id}: {vp.get('title', '')} ({method})")

        # manual_check: short-circuit to SKIPPED, do not enter the
        # parallel pipeline (no async work, no timeout, no
        # group events — they would be misleading because the
        # "group" never actually ran anything).
        for method, group_vps in groups.items():
            if method == "manual_check":
                for vp in group_vps:
                    all_results.append(
                        {
                            "id": vp.get("id", "unknown"),
                            "status": "SKIPPED",
                            "actual_result": "需要人工验证",
                            "reasons": ["需要人工验证"],
                            "evidence": "验证方法为 manual_check，需要人工介入",
                        }
                    )

        non_manual_groups = [
            (method, vps) for method, vps in groups.items() if method != "manual_check"
        ]

        if non_manual_groups:
            # ONE plan-wide bound, shared by every group, layered over
            # each group's own bound.
            #
            # The per-group bound alone does not bound the plan: it is
            # applied once per group and the groups are gathered
            # together, so the real fan-out is ``group_cap ×
            # group_count``. Four groups at 4 gives 16, and a fifth and
            # sixth group would give 24 — each in-flight VP is a
            # ``claude`` subprocess competing for the same provider slots.
            #
            # Both bounds are the *same live number* while the scene can
            # be resolved (:func:`_verification_vp_capacity`), read
            # again each time either one binds. That is deliberate: two
            # layers reading different figures is how the plan bound
            # becomes the new ceiling — the per-group bound used to hold
            # a machine with twenty provider slots to four, and a
            # declared ceiling underneath it would have held it to
            # whatever the operator happened to have written in
            # ``provider_capacity.yaml`` instead. When the scene cannot
            # be resolved, both fall back to that declared ceiling
            # (``parallelism_cap``, the per-group fallback until
            # 2026-10-13, is gone).
            plan_gate = _plan_wide_bound(self.vp_capacity_resolver)
            coros = [
                self._execute_group(method, vps, plan_gate)
                for method, vps in non_manual_groups
            ]
            group_results = await asyncio.gather(*coros, return_exceptions=True)
            for result in group_results:
                if isinstance(result, Exception):
                    # Group-level failure (shouldn't happen, but
                    # don't let one bad group kill the batch).
                    self.persistence.write_verification_point_log(
                        "__group__",
                        "group_exception",
                        {"error": str(result)},
                    )
                    continue
                all_results.extend(result)

        # Stable, plan-order output: the gather order is the
        # group-partition order, but the per-VP order within a
        # group is the order the coroutines completed. Re-sort by
        # the plan's original index so downstream consumers (the
        # report, the bridge UI) see a predictable sequence.
        original_index = {
            vp.get("id", ""): index for index, vp in enumerate(verification_points)
        }
        all_results.sort(
            key=lambda r: original_index.get(r.get("id", ""), len(original_index))
        )

        # 2026-10-25 (repair-r1-26): same rule as the live round — a
        # round that could not reach the service is blocked as a whole.
        if self._environment_blocked:
            self._write_environment_blocked_event()
            all_results = self._mark_results_environment_blocked(all_results)

        print(f"[Verification] Completed {len(all_results)} verification points")

        execution_results = {
            "verification_points": verification_points,
            "execution_results": all_results,
            "executed_at": datetime.now().isoformat(),
        }

        # Persist the full execution envelope to SQLite
        # (``plan_verification.execution_results``) so Phase 3 can read it
        # back without touching disk. The legacy on-disk cache at
        # ``plans/{id}/verification_execution_results.json`` is no
        # longer written here — see task #3.5.
        if self.verif_repo is not None:
            try:
                self.verif_repo.save_execution_results(self.plan_dir.name, execution_results)
            except Exception:  # noqa: BLE001
                # SQLite write failures should not abort the verification
                # pipeline; the legacy cache fall-through (Phase 3 reader
                # will fall back to disk if SQLite is unavailable) is
                # the historical safety net. Logged via the surrounding
                # caller's exception handler.
                pass

        return execution_results

    async def execute_phase2_parallel(
        self, vps: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Top-level concurrent runner for Phase 2 execution.

        Where :meth:`execute_verification_plan_async` already gives us
        *intra-group* concurrency (VPs sharing a method run in parallel
        under a per-group :class:`asyncio.Semaphore`), this method
        layers *inter-group* concurrency on top — the four method
        groups (``ui_validation``, ``code_review``, ``automated_test``,
        ``api_test``) are dispatched in parallel via
        :func:`asyncio.gather` so the total wall-clock time is
        ``max(group_time)`` rather than the serial sum.

        Pipeline:

        1. Empty input short-circuits to ``{"results": [], "wall_seconds": 0.0}``
          — the documented "no VPs to run" contract that the
           orchestrator relies on for empty plans.
        2. :meth:`_partition_vps_by_method` buckets VPs by
           ``verification_method`` (preserving first-seen order).
        3. ``manual_check`` groups are short-circuited to ``SKIPPED``
           entries — they never enter the gather / semaphore path.
        4. **Empty groups are skipped** — a method with zero VPs in
           the input list does not produce a gather task at all.
           This is the TDD-pinned boundary that prevents zero-iteration
           coroutines from showing up as ``group_started`` events with
           ``count=0``.
        5. Each remaining non-empty group is scheduled as
           ``self._execute_group(method, vps)`` and the resulting
           coroutines are dispatched via
           ``asyncio.gather(..., return_exceptions=True)`` so a
           misbehaving group cannot terminate its peers.
        6. Group-level exceptions are caught at the boundary and
           logged via :meth:`persistence.write_verification_point_log`
           with a ``group_exception`` event so the JSONL audit trail
           captures the failure without crashing the batch.
        7. Results are re-sorted into the original plan order before
           being returned so downstream consumers (the report
           generator, the bridge UI) see a predictable sequence.

        Args:
            vps: Flat list of verification points, each carrying at
                minimum ``id`` and ``verification_method``. This is
                the same shape that ``verification_plan.json``
                ``["verification_points"]`` stores, so callers can
                pass the plan's list directly without wrapping it.

        Returns:
            ``{"results": [<one entry per input VP>], "wall_seconds": <float>}``

            ``wall_seconds`` is the wall-clock duration of the gather
            (start of the first group task to completion of the last).
            For an empty input it is ``0.0``.
        """
        if not vps:
            return {"results": [], "wall_seconds": 0.0}

        groups = self._partition_vps_by_method({"verification_points": vps})

        all_results: List[Dict[str, Any]] = []

        # manual_check: short-circuit to SKIPPED, do not enter the
        # parallel pipeline. Mirrors the policy in
        # execute_verification_plan_async — manual review cannot be
        # driven by an automated sub-agent, so we synthesise the
        # SKIPPED row here instead of scheduling a no-op coroutine.
        for method, group_vps in groups.items():
            if method == "manual_check":
                for vp in group_vps:
                    all_results.append(
                        {
                            "id": vp.get("id", "unknown"),
                            "status": "SKIPPED",
                            "actual_result": "需要人工验证",
                            "reasons": ["需要人工验证"],
                            "evidence": "验证方法为 manual_check，需要人工介入",
                        }
                    )

        # Build the gather payload: one coroutine per *non-empty,
        # non-manual* group. Empty groups are intentionally skipped
        # so we don't schedule a zero-iteration coroutine (which
        # would still emit a misleading ``group_started`` /
        # ``group_completed`` event pair with count=0).
        non_manual_groups = [
            (method, group_vps)
            for method, group_vps in groups.items()
            if method != "manual_check" and group_vps
        ]

        started_at = datetime.now()

        if non_manual_groups:
            coros = [
                self._execute_group(method, group_vps)
                for method, group_vps in non_manual_groups
            ]
            group_outcomes = await asyncio.gather(
                *coros, return_exceptions=True
            )
            for outcome in group_outcomes:
                if isinstance(outcome, Exception):
                    # Group-level failure — log the failure but do
                    # not abort the batch. ``return_exceptions=True``
                    # guarantees that other groups' results are
                    # delivered to us intact.
                    try:
                        self.persistence.write_verification_point_log(
                            "__group__",
                            "group_exception",
                            {"error": str(outcome)},
                        )
                    except Exception:
                        pass
                    continue
                all_results.extend(outcome)

        finished_at = datetime.now()
        wall_seconds = (finished_at - started_at).total_seconds()

        # Stable, plan-order output: the gather order is the
        # group-partition order, but per-VP order within a group is
        # the order coroutines completed. Re-sort by the plan's
        # original index so downstream consumers see a predictable
        # sequence (matches the convention in
        # execute_verification_plan_async).
        original_index = {
            vp.get("id", ""): index for index, vp in enumerate(vps)
        }
        all_results.sort(
            key=lambda r: original_index.get(r.get("id", ""), len(original_index))
        )

        return {"results": all_results, "wall_seconds": wall_seconds}

    def _partition_vps_by_method(
        self, plan_data: Optional[dict]
    ) -> "Dict[str, List[Dict[str, Any]]]":
        """Group verification points by ``verification_method``.

        Returns an :class:`OrderedDict`-style mapping
        ``{method: [vp, ...]}`` whose keys appear in the order the
        methods are first encountered in the plan. This is the
        stability guarantee downstream log parsers and the
        ExecutionProfileGenerator rely on — the group order in the
        persistence log must match the plan order so an observer
        can reconstruct the timeline.

        Boundary semantics:

        * ``plan_data is None`` or empty → ``{}``.
        * Missing ``verification_points`` key → ``{}``.
        * VP with no ``verification_method`` → bucketed under
          ``"manual_check"`` (the documented fallback in
          :meth:`_run_single_vp_async`).
        * Duplicate methods are *not* deduplicated — the VPs are
          appended to whichever group matches their method.
        """
        if not plan_data or not isinstance(plan_data, dict):
            return {}

        verification_points = plan_data.get("verification_points", [])
        if not verification_points:
            return {}

        # Plain dict preserves insertion order in Python 3.7+,
        # which is the order the methods are first encountered.
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for vp in verification_points:
            if not isinstance(vp, dict):
                continue
            method = vp.get("verification_method", "manual_check")
            if not isinstance(method, str) or not method:
                method = "manual_check"
            groups.setdefault(method, []).append(vp)
        return groups

    def _emit_group_log(
        self,
        event_name: str,
        group_name: str,
        vps: List[Dict[str, Any]],
        ts: Any,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit a group-level event to the active round log as a JSON line.

        Writes one entry per call, appending to the same log file the
        per-VP :meth:`verification_persistence.VerificationPersistenceManager.write_verification_point_log`
        writes to, but with a different schema so the round-level
        trace can be queried directly:

        ``GET /api/execution/{id}/logs?event=group_started`` already
        exists for the old ``event_type`` schema; the new schema
        makes ``event`` a top-level field and surfaces ``group_name``
        + ``vp_count`` + ``data`` next to the timestamp so that an
        operator scanning the log can size each group at a glance.

        Output schema (one JSON object, one line, ``\\n`` terminated):

        ::

            {
              "ts": "<ISO-8601 timestamp>",
              "level": "INFO",
              "event": "<event_name>",
              "group_name": "<group_name>",
              "vp_count": <len(vps)>,
              "data": {<caller-supplied dict>}
            }

        For ``event_name == "group_completed"`` the caller is
        expected to pass ``data={"wall_seconds": <float>}``; for
        ``group_started`` an empty ``data`` (``{}``) is the default.

        Boundary contract:

        * **Log file unwritable** — ``OSError``/``PermissionError``
          from the underlying ``open(..., "a")`` propagates to the
          caller verbatim. The caller is expected to catch it
          locally so the VP execution loop is not affected; this
          function deliberately does **not** swallow the error.
        * **Same group, multiple emits** — the timestamp is taken
          from the ``ts`` argument verbatim (ISO-formatted for
          ``datetime`` inputs). The function does not deduplicate or
          overwrite prior entries, so consecutive emits with
          strictly-increasing ``ts`` values produce strictly-
          increasing entries in the log file.
        * **Missing fields** — the function unconditionally writes
          all six top-level keys (``ts``, ``level``, ``event``,
          ``group_name``, ``vp_count``, ``data``). There is no code
          path that omits ``ts``/``level``/``event``/``group_name``,
          so a JSON-line parser can rely on the schema holding.
        """
        log_file = self.persistence._log_file
        if log_file is None:
            raise RuntimeError(
                "No log file active. Call persistence.start_round() first."
            )

        if isinstance(ts, datetime):
            ts_str = ts.isoformat()
        else:
            ts_str = str(ts)

        if data is None:
            data = {}

        entry = {
            "ts": ts_str,
            "level": "INFO",
            "event": event_name,
            "group_name": group_name,
            "vp_count": len(vps),
            "data": data,
        }

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def _execute_group(
        self,
        method: str,
        vps: List[Dict[str, Any]],
        plan_gate: Optional["_VpCapacityGate"] = None,
    ) -> List[Dict[str, Any]]:
        """Run all VPs in a single method-group in parallel.

        Two bounds apply, nested:

        * ``plan_gate`` — the plan-wide fleet ceiling, built once in
          :meth:`execute_verification_plan_async` and shared by every
          group. Without it the per-group caps multiply (cap × groups).
        * this group's own gate — the same live number as the plan's
          while the verification scene resolves, so the plan bound can
          never become the tighter of the two.

        Both are :class:`_VpCapacityGate`, which re-reads its width each
        time it binds rather than fixing it at construction. That is the
        point: a group used to be held to a number typed into
        ``verification.yaml``, and four was far below what one provider
        declaring ten can serve — so four verification points could be
        in flight while doing nothing at all, each asleep on a closed
        quota window, and the fifth would never get a turn. The width now
        tracks the fleet: what the scene's providers can serve this
        second, scaling with their remaining 5-hour budget and losing a
        provider's share entirely the moment that provider is shut.

        ``plan_gate`` is passed in rather than constructed here, but still
        defaulted: a caller driving a single group directly (tests,
        ad-hoc repros) gets a correctly-sized one instead of an unbounded
        gather. For a single group the two coincide anyway.

        Two persistence events bracket the run:

        * ``group_started`` — emitted *before* the gather, with
          ``method`` and ``count`` so consumers can size the group
          up front.
        * ``group_completed`` — emitted *after* the gather, with
          ``method``/``count``/``status_counts``/``duration_sec``
          so the JSONL log parser and the bridge UI can render
          "X passed, Y failed" badges per group.

        The ``__group__`` sentinel ``vp_id`` is reused for every
        group event so the existing log-reader query
        ``.filter(event="group_started")`` picks them up without
        needing a new schema.
        """
        if not vps:
            return []

        # Emit group_started before the gather so a downstream
        # consumer that starts reading mid-run can still see what
        # the agent is about to do.
        self.persistence.write_verification_point_log(
            "__group__",
            "group_started",
            {"method": method, "count": len(vps)},
        )

        # New-schema group_started entry: top-level ``event`` +
        # ``group_name`` + ``vp_count`` so
        # ``GET /api/execution/{id}/logs?event=group_started``
        # surfaces the group trace directly. The OSError is caught
        # locally so an unwritable log directory never blocks the
        # VP execution loop.
        try:
            self._emit_group_log(
                "group_started",
                method,
                vps,
                datetime.now(),
            )
        except OSError:
            pass

        if plan_gate is None:
            plan_gate = _plan_wide_bound(self.vp_capacity_resolver)

        # Two bounds, and they say the same thing whenever the scene can
        # be resolved. The group's own reader is the live scene with the
        # declared fleet ceiling behind it — the same pair
        # :func:`_plan_wide_bound` uses. Its fallback used to be
        # ``parallelism_cap``, an operator-typed number kept alive
        # precisely for a chain that cannot be resolved; that number is
        # gone (2026-10-13), and the declared ceiling is what is left to
        # stand behind an unresolvable chain.
        #
        # NOTE: this is the legacy per-group path, not the live round.
        # ``run_full_verification`` builds no gate at all — see there.
        # This survives because :meth:`execute_verification_plan_async`
        # and :meth:`execute_phase2_parallel` still call it, and deleting
        # that path is not this change's business. It is production-dead
        # (no production caller reaches either of those two methods).
        group_gate = _plan_wide_bound(self.vp_capacity_resolver)
        # No ``_note_vp_capacity`` here, unlike the live path. That field
        # is a *measurement* of a round that ran; a gate's configured
        # width is not one, and recording it would publish a derived
        # number under a name that promises an observed one.
        _ = group_gate

        async def _run_with_semaphore(vp: Dict[str, Any]) -> Dict[str, Any]:
            # The semantic this loop exists to establish: a gate slot means
            # "a verification point is WORKING", not "a verification point
            # is in flight". It used to mean the latter, and the two come
            # apart the moment a point is asleep in the provider slot queue
            # — which it is, for as long as the tier stays saturated, while
            # both of its gates stay taken. A round whose points are all
            # queued reads from the outside as a fully-busy fleet and
            # produces nothing, so the gate was spending its width on points
            # that were not doing anything with it.
            #
            # ``ProviderSlotWaitExpired`` is the signal that separates the
            # two, and it can only be raised while a point is waiting: the
            # dispatch raises it inside the queue loop, which is reached
            # only before a provider is chosen, and the loop is left behind
            # the moment one is. Nothing that is running can raise it. So
            # re-queueing on it costs at most the wait that already
            # happened, and it can never interrupt a working point — which
            # is exactly the property an ``asyncio.wait_for`` around the
            # whole VP could not have.
            #
            # The two ``finally`` arms do the hand-back; the ``except`` arm
            # only decides what happens next, and re-queueing is a decision,
            # not a verdict: no result is written, no FAILED is recorded, and
            # the same ``vp`` goes round again. The point that cannot get a
            # slot is not a point that did the work badly.
            #
            # The flag, rather than a bare ``continue`` in the ``except``
            # arm, is load-bearing. The hand-back lives in the ``finally``
            # arms, and a ``finally`` runs *after* the ``except`` body: a
            # yield point written inside the ``except`` arm would therefore
            # resume while this point still holds the group gate, so the
            # waiter it yielded to would bounce off a gate that was about to
            # open, and the yield would achieve the opposite of what it is
            # there for. Deciding here and yielding after the block means
            # both gates are already back before anything else runs.
            while True:
                requeue = False
                await group_gate.acquire()
                try:
                    await plan_gate.acquire()
                    try:
                        return await self._run_single_vp_async(vp)
                    finally:
                        plan_gate.release()
                except ProviderSlotWaitExpired:
                    requeue = True
                finally:
                    group_gate.release()

                # Back of the queue, not straight back to the front.
                # ``acquire`` hands the slot straight back when the gate has
                # room, and the arm above returns into it without a yield
                # point -- so a point that re-queued without this would take
                # its own gate again immediately and starve whichever point
                # was already queued behind it, repeating that for as long as
                # it kept missing a slot. Yielding once puts this point
                # behind every waiter that a release has already woken, which
                # is what re-queued means. The alternative -- a fairness
                # counter on the gate -- would change the gate for every
                # caller, including the ones that never re-queue.
                await asyncio.sleep(0)

        started_at = datetime.now()
        try:
            results = await asyncio.gather(
                *[_run_with_semaphore(vp) for vp in vps],
                return_exceptions=True,
            )
        except Exception as e:  # noqa: BLE001
            # ``gather(..., return_exceptions=True)`` shouldn't
            # raise, but a defensive outer try keeps the
            # group_completed event honest even if it does.
            self.persistence.write_verification_point_log(
                "__group__",
                "group_exception",
                {"method": method, "error": str(e)},
            )
            results = []

        finished_at = datetime.now()
        duration_sec = (finished_at - started_at).total_seconds()

        # Collapse exceptions into FAILED entries so the result
        # list always has one entry per input VP — downstream
        # code (the report generator, the orchestrator's
        # _extract_failed_ids) iterates with the assumption that
        # the length matches the plan's VP count.
        normalised: List[Dict[str, Any]] = []
        status_counts: Dict[str, int] = {}
        for index, result in enumerate(results):
            if isinstance(result, Exception):
                vp_id = vps[index].get("id", "unknown") if index < len(vps) else "unknown"
                exc_msg = str(result)
                result = {
                    "id": vp_id,
                    "status": "FAILED",
                    "actual_result": f"group execution raised {type(result).__name__}: {exc_msg}",
                    "reasons": [f"group execution raised {type(result).__name__}: {exc_msg}"],
                    "evidence": exc_msg,
                }
            normalised.append(result)
            status = result.get("status", "UNKNOWN")
            status_counts[status] = status_counts.get(status, 0) + 1

        self.persistence.write_verification_point_log(
            "__group__",
            "group_completed",
            {
                "method": method,
                "count": len(vps),
                "status_counts": status_counts,
                "duration_sec": duration_sec,
            },
        )

        # New-schema group_completed entry: carries ``wall_seconds``
        # in ``data`` so the JSONL parser and the bridge UI can
        # render the per-group wall-clock without re-deriving it
        # from log mtime. Same OSError-swallow contract as
        # group_started above.
        try:
            self._emit_group_log(
                "group_completed",
                method,
                vps,
                datetime.now(),
                data={"wall_seconds": duration_sec},
            )
        except OSError:
            pass

        return normalised

    def _execute_verification_point(self, vp: dict) -> dict:
        """Synchronous entry point — delegates to the async executor.

        Kept as the legacy entry point so existing call-sites (the
        in-test ``verification_agent._execute_verification_point(vp)``
        invocations, ad-hoc scripts) keep working unchanged.

        ``asyncio.run`` cannot be called from inside a running event
        loop (raises ``RuntimeError: asyncio.run() cannot be called
        from a running event loop``). When this method is invoked
        inside an async test (under ``pytest-asyncio``) or any
        async-context, we fall back to ``nest_asyncio`` if available,
        otherwise to the slow-but-safe path of running a fresh loop
        in a separate thread. The original sync callers (scripts,
        ``execute_verification_plan``'s sync wrapper) hit the
        ``asyncio.run`` branch unchanged.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop: original sync path is correct.
            return asyncio.run(self._run_single_vp_async(vp))

        # We are inside a running loop (async test or async caller).
        # Use nest_asyncio if it's installed; otherwise fail loud
        # with a clear message rather than silently re-entering
        # asyncio.run().
        try:
            import nest_asyncio  # type: ignore
            nest_asyncio.apply()
            return asyncio.run(self._run_single_vp_async(vp))
        except ImportError:
            raise RuntimeError(
                "_execute_verification_point was called from inside a "
                "running event loop, but nest_asyncio is not installed. "
                "Either install nest_asyncio (`pip install nest_asyncio`) "
                "or call ``await agent._run_single_vp_async(vp)`` "
                "directly from async code."
            )

    def _attach_evidence_command(self, vp: dict, verdict: dict) -> dict:
        """Run the VP's optional ``evidence_command`` and attach the result.

        2026-09-18: the sanctioned residue of the retired VP
        ``test_command`` — the minority case where an existing test *is*
        the only evidence for an assertion. The **framework** runs it (no
        sub-agent, no LLM), so its result cannot be reported dishonestly.

        It does **not** influence the verdict. The old design let a
        command's exit code override the reviewer's conclusion — and
        because the reviewer both produced and interpreted that number,
        the same artifact could flip verdict between rounds. Evidence is
        evidence.

        Never raises: a timeout or missing binary is attached as a note.
        """
        command = str(vp.get("evidence_command") or "").strip()
        if not command:
            return verdict
        vp_id = str(vp.get("id", "unknown"))
        try:
            from verification_evidence import attach_evidence_command
            return attach_evidence_command(verdict, vp, self.project_dir)
        except Exception:  # pragma: no cover - attach never raises
            logger.exception(
                "[evidence_command] attach failed for %s", vp_id,
            )
            return verdict

    def _effective_declarations(self) -> Dict[str, Any]:
        """The declarations as this round actually runs them.

        Identical to the plan's declarations except where the preflight had
        to relocate a service off a port a foreign process was holding —
        then the entry carries the port the service really listens on.
        Everything downstream (placeholder resolution, the VP prompt's
        service block) reads through here, so there is exactly one place
        that knows where a service actually lives.
        """
        runtime_map = self._service_runtime_map
        if runtime_map is None and not self._service_bindings:
            return self._service_declarations
        try:
            from service_manager import render_declaration
        except Exception:  # pragma: no cover - defensive
            return self._service_declarations

        effective: Dict[str, Any] = {}
        for name, decl in self._service_declarations.items():
            runtime = None
            if runtime_map is not None:
                runtime = (runtime_map.runtimes or {}).get(name)
            if _runtime_is_unusable(runtime):
                # 2026-10-25 (repair-r1-29): this service has **no
                # address**.  The port its runtime names is the
                # declared one, and the declared one belongs to
                # somebody else — so it is not a fallback: falling
                # back to it would aim the round's VPs at the
                # stranger.  The entry is left out of the table, and
                # the VPs that need this service are blocked by name
                # (see :meth:`_unusable_services`).
                continue
            # The **binding** first: it is the instance the round's
            # ensure produced, and it is what the VPs are dispatched
            # to.  The runtime map is only consulted for a service
            # that was never bound (a legacy path, or a test that
            # installs a map directly) — a map that is rewritten
            # afterwards must not be able to move a bound service.
            port = self._bound_service_port(name)
            if port is None and _runtime_is_usable(runtime):
                port = getattr(runtime, "port", None)
            if port and int(port) != decl.port:
                effective[name] = render_declaration(decl, int(port))
            else:
                effective[name] = decl
        return effective

    def _unusable_services(self) -> Dict[str, Dict[str, Any]]:
        """The services the round could not place.

        ``{name: {"port": int, "reason": str}}`` — the name is
        lower-cased, the port is the **declared** one (the port the
        stranger holds), and the reason is the runtime's own sentence.

        A runtime that is explicitly unusable names no address: there is
        nowhere to send a VP that needs it, and the VPs that name it
        are blocked — by name, because the placeholder that named it
        never resolved and there is no port left to match on.
        """
        runtime_map = self._service_runtime_map
        if runtime_map is None:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        runtimes = getattr(runtime_map, "runtimes", None) or {}
        for name, runtime in runtimes.items():
            if not _runtime_is_unusable(runtime):
                continue
            declared = getattr(runtime, "declared_port", None)
            port = getattr(runtime, "port", None)
            out[str(name).lower()] = {
                "port": int(declared or port or 0),
                "reason": str(
                    getattr(runtime, "failure_reason", "")
                    or getattr(runtime, "detail", "")
                    or "no detail"
                ),
            }
        return out

    # ------------------------------------------------------------------
    # The round's service binding (2026-10-25, repair-r1-22)
    # ------------------------------------------------------------------
    #
    # The address a VP is dispatched to is decided **once**, by the
    # ``ensure`` that produced the instance, and it is carried by that
    # instance's runtime object.  Everything below exists so that no
    # later step can quietly re-derive the address from the runtime
    # map — the map is a record of what happened, not an instruction
    # about where to send a request.

    def _bind_service_runtime(
        self, name: str, runtime: Any,
    ) -> Optional[_BoundService]:
        """Bind *name* to the instance *runtime* describes.

        Only a runtime that is **ready** and carries a port can be
        bound: an address nobody listens on is not a service, and
        binding one would send the round's VPs to a dead port.

        Returns the binding, or ``None`` when the runtime was refused
        (a previous binding — if any — is left alone; the caller that
        knows the service is gone releases it explicitly).
        """
        if runtime is None:
            return None
        if _runtime_is_unusable(runtime):
            # 2026-10-25 (repair-r1-29): a runtime that says "this
            # names no address" is never bound — the port it carries is
            # the declared one, and the declared one is somebody else's.
            return None
        if not getattr(runtime, "ready", False):
            return None
        port = getattr(runtime, "port", None)
        if not port:
            return None
        key = str(name).lower()
        pid = getattr(runtime, "pid", None)
        binding = _BoundService(
            name=key,
            port=int(port),
            pid=int(pid) if pid else None,
            health_url=self._bound_health_url(key, int(port)),
            boot_id=self._runtime_boot_id(runtime),
            runtime_id=f"{key}:{int(port)}:{pid}",
            runtime=runtime,
        )
        self._service_bindings[key] = binding
        return binding

    @staticmethod
    def _runtime_boot_id(runtime: Any) -> str:
        """The **boot identity** of a runtime: pid + process start epoch.

        A pid alone cannot tell two runs of the same pid apart, and
        "the service was restarted" is exactly the question a log reader
        has to be able to answer.
        """
        pid = getattr(runtime, "pid", None)
        start_epoch = getattr(runtime, "start_epoch", None)
        if pid and start_epoch:
            return f"{int(pid)}@{int(start_epoch)}"
        if pid:
            return str(int(pid))
        return ""

    def _release_service_binding(self, name: str) -> None:
        """Unbind *name*: the round has no usable instance of it.

        A released service falls back to its declared address, which
        the blocked-port gate turns into a BLOCKED verdict — which is
        the truth: the round has no service to talk to.
        """
        self._service_bindings.pop(str(name).lower(), None)

    def _bound_service_port(self, name: str) -> Optional[int]:
        """The port the bound instance of *name* listens on."""
        binding = self._service_bindings.get(str(name).lower())
        if binding is None:
            return None
        return binding.port

    def _bound_health_url(self, name: str, port: int) -> str:
        """The health URL of the bound instance (the declaration, moved
        to the bound port)."""
        decl = self._service_declarations.get(str(name).lower())
        if decl is None:
            return f"http://127.0.0.1:{int(port)}"
        try:
            from service_manager import render_declaration

            return render_declaration(decl, int(port)).health_url
        except Exception:  # pragma: no cover - defensive
            return f"http://127.0.0.1:{int(port)}"

    def _service_for_vp(self, vp: dict, url: str) -> Optional[str]:
        """Which bound service does this VP's request talk to?

        Two ways a VP names a service, and both are honoured:

          * ``uses_services`` — the plan's explicit list of service
            names;
          * a **port literal** in the request URL that belongs to a
            declared service.
        """
        uses = vp.get("uses_services")
        if isinstance(uses, list):
            for name in uses:
                key = str(name).lower()
                if key in self._service_bindings:
                    return key
        port = _url_port(url)
        if port is None:
            return None
        for key, binding in self._service_bindings.items():
            if binding.port == port:
                return key
        for key, decl in self._service_declarations.items():
            if getattr(decl, "port", None) == port:
                return key
        return None

    def _bind_vp_service_address(self, vp: dict) -> dict:
        """Point this VP's request at the instance the round bound.

        The URL a VP carries was resolved when the plan was resolved —
        against whatever the declarations said *then*.  The bound
        instance is the one the round's ``ensure`` produced, and it is
        the one the VP must talk to: a rewritten runtime map must not
        be able to move the request to an address the round never
        verified.

        Returns the VP — a copy when the URL had to be retargeted, the
        same dict when it was already pointing at the bound instance.
        """
        if not self._service_bindings:
            return vp
        request = vp.get("request")
        if not isinstance(request, dict):
            return vp
        url = request.get("url")
        if not isinstance(url, str) or not url.strip():
            return vp
        vp_id = str(vp.get("id", "unknown"))

        name = self._service_for_vp(vp, url)
        if name is None:
            return vp
        binding = self._service_bindings.get(name)
        if binding is None:
            return vp

        bound_url = _retarget_url_port(url, binding.port)
        if bound_url is not None and bound_url != url:
            new_request = dict(request)
            new_request["url"] = bound_url
            new_vp = dict(vp)
            new_vp["request"] = new_request
            self._log_service_dispatch_bound(
                vp_id, binding, request_url=bound_url, retargeted_from=url,
            )
            return new_vp

        self._log_service_dispatch_bound(vp_id, binding, request_url=url)
        return vp

    def _log_service_dispatch_bound(
        self, vp_id: str, binding: _BoundService, **extra: Any,
    ) -> None:
        """Say which instance this VP was dispatched to.

        The 2026-10-24 round could not answer "which service did this
        VP talk to" from its log — the address was there, but nothing
        said which instance it belonged to.  The binding says it, and
        says it per VP.
        """
        data = {
            "plan_id": self.plan_dir.name,
            "vp_id": vp_id,
        }
        data.update(binding.to_dict())
        data.update(extra)
        self._safe_log_freshness_event(vp_id, "service_dispatch_bound", data)

    # ------------------------------------------------------------------
    # The address every VP's evidence records (2026-10-25, repair-r1-31)
    # ------------------------------------------------------------------

    def _address_records(self) -> Dict[str, Dict[str, Any]]:
        """The address records of the round, keyed by vp id.

        ``getattr`` for the same reason as :meth:`_serving_snapshots`:
        an orchestrator built without ``__init__`` has recorded no
        address yet, and "no address recorded" means "no address facts"
        — not an ``AttributeError``.  The attribute is created on first
        use so the writers and the readers always share one dict.
        """
        records = getattr(self, "_vp_address_records", None)
        if records is None:
            records = {}
            self._vp_address_records = records
        return records

    def _record_vp_address(self, vp_id: str, vp: dict) -> Dict[str, Any]:
        """Remember where this VP's request went (or was going to go).

        The record is written **twice** per api_test VP on purpose:
        once when the VP is about to be dispatched (the address the
        plan asked for) and once after the request was bound (the
        address it really went to).  The second write wins, because it
        is the one the transport saw.

        The service is looked up the way the dispatch looks it up —
        by ``uses_services`` first, then by the port the URL names —
        so the address and the binding can never disagree about which
        service they are talking about.
        """
        request = vp.get("request") if isinstance(vp, dict) else None
        request = request if isinstance(request, dict) else {}
        url = str(request.get("url") or "")
        resolved_port = _url_port(url) if url else None

        name = self._service_for_vp(vp, url) if url else None
        binding = self._service_bindings.get(name) if name else None
        declaration = self._service_declarations.get(name) if name else None

        ensure_port: Optional[int] = None
        pid: Optional[int] = None
        boot_id = ""
        if binding is not None:
            ensure_port = int(binding.port)
            pid = binding.pid
            boot_id = str(binding.boot_id or "")
        elif declaration is not None:
            # The service was never bound (it could not be placed, or
            # the round released it).  The **declared** port is still
            # worth recording: it is the address the plan expected.
            try:
                ensure_port = int(declaration.port)
            except (TypeError, ValueError):
                ensure_port = None

        record = {
            "requested_url": url,
            "resolved_port": resolved_port,
            "ensure_port": ensure_port,
            "pid": pid,
            "boot_id": boot_id,
            "service": str(name or ""),
        }
        self._address_records()[str(vp_id)] = record
        return record

    def _attempt_counts(self) -> Dict[str, int]:
        """How many times each VP was asked this round.

        ``getattr`` for the same reason as :meth:`_address_records`:
        an orchestrator built without ``__init__`` has asked nobody
        anything, and "no attempt" is a fact — not an
        ``AttributeError``.
        """
        counts = getattr(self, "_vp_attempts", None)
        if counts is None:
            counts = {}
            self._vp_attempts = counts
        return counts

    def _note_vp_attempt(self, vp_id: str) -> int:
        """Count this VP's request attempt in this round.

        ``1`` means the request was sent once.  ``0`` — the value a
        VP that was blocked before dispatch keeps — means **no request
        was ever made**, which is a different fact from "the request
        failed".
        """
        key = str(vp_id)
        counts = self._attempt_counts()
        attempt = int(counts.get(key, 0)) + 1
        counts[key] = attempt
        return attempt

    def _answering_boot_id(self, port: Optional[int]) -> str:
        """The boot id of the instance that answered on *port*.

        The round does not ask the service a **new** question for a
        bookkeeping field: the answer is the instance block the round
        has already read for that address — the one its own pre-dispatch
        identity check fetches from ``/health/data``, moments before
        the request goes out.  A service's request pattern must not
        change because a report wants one more field.

        Empty when the round has not identified the instance at that
        address: "not obtainable" is a real answer, and it is not the
        same as "the answering instance is the one the round started".
        """
        if not port:
            return ""
        instance = self._observed_instances.get(int(port)) or {}
        return str(instance.get("boot_id") or "")

    def _address_evidence_block(
        self, vp_id: str, evidence: Dict[str, Any],
    ) -> Dict[str, Any]:
        """The address facts one VP's evidence carries.

        ``error_kind`` and ``errno`` are read **out of** the evidence
        the verdict already produced — the classification layer owns
        those two, and this layer only makes sure they are present
        under a name every reader can match on.
        """
        record = self._address_records().get(str(vp_id)) or {}
        requested_url = str(record.get("requested_url") or "")
        resolved_port = record.get("resolved_port")
        if resolved_port is None and requested_url:
            resolved_port = _url_port(requested_url)

        errno_value = evidence.get("errno")
        if errno_value is None:
            error_text = str(evidence.get("error") or "")
            if error_text:
                errno_value = _errno_from_error(error_text)

        answering = record.get("answering_boot_id")
        serving = (
            getattr(self, "_vp_serving_snapshots", None) or {}
        ).get(str(vp_id)) or {}
        values: Dict[str, Any] = {
            "requested_url": requested_url,
            "resolved_port": resolved_port,
            "ensure_port": record.get("ensure_port"),
            "pid": record.get("pid"),
            "boot_id": str(record.get("boot_id") or evidence.get("boot_id") or ""),
            "attempt_no": int(self._attempt_counts().get(str(vp_id), 0)),
            "error_kind": evidence.get("error_kind"),
            "errno": errno_value,
            "request_id": getattr(self, "_round_request_id", "") or "",
            # ``None`` when no response came back: there was no
            # answering instance to name.  That is a fact, not a gap.
            "answering_boot_id": str(answering) if answering else None,
            # The instance on **both** sides of this dispatch.  ``None``
            # means the service did not identify itself — not "there was
            # nothing there".
            SERVING_BEFORE_FIELD: serving.get(SERVING_BEFORE_FIELD),
            SERVING_AFTER_FIELD: serving.get(SERVING_AFTER_FIELD),
        }
        # The contract's field list, in the contract's order: a reader
        # that knows the list finds every field where it expects it,
        # and a field that is absent never reads as "nobody looked".
        return {field: values.get(field) for field in ADDRESS_EVIDENCE_FIELDS}

    def _enrich_evidence_with_address(
        self, vp_id: str, evidence: Any, vp: Optional[dict] = None,
    ) -> str:
        """One VP's recorded evidence, with the address attached.

        The evidence stays a **string** — the legacy Phase 3 judgment
        prompt slices it — but it now carries the address facts as a
        serialized fragment.  Evidence that was not JSON becomes
        ``{"raw_evidence": ...}`` plus the block, so the shape is the
        same for every VP of the round.
        """
        parsed: Dict[str, Any]
        if isinstance(evidence, str):
            text = evidence
            try:
                loaded = json.loads(text) if text.strip() else {}
            except (json.JSONDecodeError, TypeError, ValueError):
                loaded = None
            parsed = loaded if isinstance(loaded, dict) else {"raw_evidence": text}
        elif isinstance(evidence, dict):
            parsed = dict(evidence)
        else:
            parsed = {}

        # A VP that never dispatched has no record of its own; the
        # plan's request is still the address it was *going to* use,
        # and saying so is what makes "never sent" readable.
        if not parsed.get("requested_url") and isinstance(vp, dict):
            request = vp.get("request")
            if isinstance(request, dict):
                planned_url = str(request.get("url") or "")
                if planned_url:
                    record = self._address_records().setdefault(
                        str(vp_id), {},
                    )
                    if not record.get("requested_url"):
                        record["requested_url"] = planned_url
                        if record.get("resolved_port") is None:
                            record["resolved_port"] = _url_port(planned_url)

        parsed.update(self._address_evidence_block(vp_id, parsed))
        try:
            return json.dumps(parsed, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return str(evidence)

    # ------------------------------------------------------------------
    # The instance behind the address (2026-10-26)
    # ------------------------------------------------------------------
    #
    # The production round aimed at an address and was refused, while a
    # later point of the **same** round was served on that very
    # address.  The only explanation the framework had was "the port
    # was taken over by another instance", and it could not say so —
    # because it had never asked **who** was serving the address.
    #
    # These four methods are that question, asked on both sides of
    # every dispatch.  The answer is written into the VP's evidence, so
    # a reader can place the answer they are looking at.

    def _serving_snapshots(self) -> Dict[str, Dict[str, Any]]:
        """The snapshots the round has taken, keyed by vp id.

        ``getattr`` for the same reason as elsewhere in this module: an
        orchestrator built without ``__init__`` has taken no snapshots
        yet, and "no snapshots" means "nothing to compare".
        """
        snapshots = getattr(self, "_vp_serving_snapshots", None)
        if snapshots is None:
            snapshots = {}
            self._vp_serving_snapshots = snapshots
        return snapshots

    def _handover_conflicts(self) -> List[Dict[str, Any]]:
        """The handovers the round has seen, one per dispatch.

        ``getattr`` for the same reason as :meth:`_serving_snapshots`.
        """
        conflicts = getattr(self, "_instance_handover_conflicts", None)
        if conflicts is None:
            conflicts = []
            self._instance_handover_conflicts = conflicts
        return conflicts

    def _serving_port_for_vp(
        self, vp: dict, vp_id: str,
    ) -> Optional[int]:
        """The port whose instance this VP's snapshot is about.

        The same three sources the address record uses, in the same
        order: what the request really went to, what the plan's URL
        names, and what the round bound for the service the VP uses.
        """
        record = self._address_records().get(str(vp_id)) or {}
        resolved = record.get("resolved_port")
        if resolved:
            try:
                return int(resolved)
            except (TypeError, ValueError):
                pass

        request = vp.get("request") if isinstance(vp, dict) else None
        request = request if isinstance(request, dict) else {}
        url = str(request.get("url") or "")
        port = _url_port(url) if url else None
        if port:
            return int(port)

        name = self._service_for_vp(vp, url) if url else None
        if name is None:
            uses = vp.get("uses_services") if isinstance(vp, dict) else None
            if isinstance(uses, list):
                for candidate in uses:
                    key = str(candidate).lower()
                    if key in self._service_bindings or key in self._service_declarations:
                        name = key
                        break
        if name:
            binding = self._service_bindings.get(name)
            if binding is not None:
                try:
                    return int(binding.port)
                except (TypeError, ValueError):
                    return None
            declaration = self._service_declarations.get(name)
            if declaration is not None:
                try:
                    return int(declaration.port)
                except (TypeError, ValueError):
                    return None
        return None

    def _capture_serving_snapshot(
        self, vp: dict, vp_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Ask the address who is serving it, right now.

        The question is asked over the same road the request travels
        (``/health/data`` on the port the VP is about to use), and the
        answer is the service's **own** statement about itself.

        A fresh read is what makes this worth anything: the round's
        cached observation of a port is a statement about the past, and
        a handover that happened **during** the dispatch is invisible
        to it.  The snapshot is therefore taken on both sides of the
        request, not once at the start of the round.

        Only a service the round has already seen identify itself is
        asked again.  A service that publishes no ``instance`` block has
        no instance to have changed hands, so it is not asked a second
        question and it is never gated — the same rule the round's own
        data-plane fingerprint follows.
        """
        port = self._serving_port_for_vp(vp, vp_id)
        if not port:
            return None
        if int(port) not in (getattr(self, "_observed_instances", None) or {}):
            # The round has never seen this address identify itself, so
            # there is nothing to compare and nothing to gate.
            return None
        snapshot = _serving_snapshot(_health_data_instance(int(port)))
        if getattr(self, "_round_serving_port", None) is None:
            # The round's own "before": the first instance that served
            # one of its addresses.  The round-level handover record is
            # built from this one and a fresh read at the round's end.
            self._round_serving_port = int(port)
            self._round_serving_snapshot = snapshot
        return snapshot

    def _apply_serving_handover(
        self,
        vp_id: str,
        before: Optional[Dict[str, Any]],
        after: Optional[Dict[str, Any]],
        verdict: Any,
    ) -> Any:
        """Say whose instance answered — and refuse to judge a handover.

        The two snapshots go into the evidence under their contract
        names, whatever the verdict says.

        When the address changed hands, the round's own evidence
        contradicts the verdict: the answer came from an instance the
        round cannot place (or the instance could not be placed at
        all).  So the handover is recorded as a **contradiction** — the
        same shape as the "refused here but served there" one — and the
        round's own reconciliation marks the point ``invalid_env`` and
        gives it the **one** second look every other contradicted point
        gets.  What the second look says is what lands.

        The verdict's own status is not touched here.  ``invalid_env``
        is the round's word for "this is not about the product", and it
        is written on the round's **results** — after the executor has
        collected the verdicts, which only accept the statuses they
        accept.
        """
        key = str(vp_id)
        self._serving_snapshots()[key] = {
            SERVING_BEFORE_FIELD: before,
            SERVING_AFTER_FIELD: after,
        }
        if not isinstance(verdict, dict):
            return verdict

        # The verdict itself is **not** rewritten: the dispatcher's
        # verdict is what the downstream handed over, and the round's own
        # evidence contract is where the address facts live (see
        # ``_address_evidence_block``).  The two snapshots travel with
        # the address block, which every VP's recorded evidence carries.
        evidence = verdict.get("evidence")
        evidence = dict(evidence) if isinstance(evidence, dict) else {}

        if not _serving_handover(before, after):
            return verdict

        # The address the request went to — the record the dispatch
        # wrote, because the verdict's own evidence does not carry the
        # URL (that is the result layer's job).
        record = self._address_records().get(key) or {}
        url = str(
            evidence.get("requested_url") or record.get("requested_url") or ""
        )
        conflict = {
            "kind": "instance_handover",
            "url": url,
            "origin": _url_origin(url),
            "failed_vps": [key],
            "succeeded_vp": None,
            SERVING_BEFORE_FIELD: before,
            SERVING_AFTER_FIELD: after,
        }
        self._handover_conflicts().append(conflict)
        self._safe_log_freshness_event(
            key, "service_instance_handover",
            {
                "plan_id": self.plan_dir.name,
                "vp_id": key,
                SERVING_BEFORE_FIELD: before,
                SERVING_AFTER_FIELD: after,
            },
        )
        logger.warning(
            "[verification] the instance serving %s changed hands while %s "
            "was being verified (boot %s before, boot %s after); the answer "
            "is not a verdict about the product",
            conflict.get("url") or "the address", key,
            (before or {}).get("boot_id") or "?", (after or {}).get("boot_id") or "?",
        )
        return verdict

    def round_instance_handover(self) -> Dict[str, Any]:
        """The instance that served this round, and the one serving it now.

        ``before`` is the snapshot the round took when it first talked
        to its own address; ``after`` is a fresh read of that same
        address.  Both are ``None`` when the round never reached a
        service that identifies itself — "nobody said" is an answer,
        and it is not the same as "somebody else said".
        """
        before = getattr(self, "_round_serving_snapshot", None)
        after: Optional[Dict[str, Any]] = None
        port = getattr(self, "_round_serving_port", None)
        if port:
            after = _serving_snapshot(_health_data_instance(int(port)))
        return {
            "before": before,
            "after": after,
        }

    def _service_env_for_vps(self) -> Dict[str, str]:
        """``PDT_SVC_<NAME>_*`` for every ready service, for the VP prompt.

        The env vars are the belt to the placeholder braces: a VP (or a
        script it invokes) can read where a service actually listens even
        if the round had to relocate it.
        """
        runtime_map = self._service_runtime_map
        if runtime_map is None:
            return {}
        try:
            from service_manager import service_env
        except Exception:  # pragma: no cover - defensive
            return {}
        env: Dict[str, str] = {}
        for name, decl in self._effective_declarations().items():
            runtime = (runtime_map.runtimes or {}).get(name)
            if runtime is not None and runtime.ready:
                env.update(service_env(decl))
        return env

    def _apply_evidence_check(self, vp: dict, verdict: dict) -> dict:
        """Verify the artifact ``verification_evidence`` requires.

        ``code_review`` and ``ui_validation`` both rest on an LLM's
        conclusion. The anti-false-positive job the old cross-verify
        overlay did — refusing to accept a PASSED that nothing supports
       — is re-anchored here on the artifact the method was supposed to
        produce, instead of on a self-reported exit code.

        Never raises: an evidence-check failure downgrades the verdict
        (when it was PASSED) and records why; it never aborts the VP.

        **A mutated working tree defers the check** (2026-10-23).  The
        check reads the *live* tree, and several verification processes
        can share one worktree: a concurrent writer (a "变异负控"
        running in parallel) can rewrite a file between the moment a
        verdict was produced and the moment its citations are read — and
        the check then reads a tree that belongs to nobody.  The 2026-10-23
        production round hit exactly that: a ``code_review`` VP handed
        back PASSED, and 7.5 ms later the framework downgraded it
        because ``citations[8]`` pointed at a line the concurrent writer
        had already removed.  That downgrade is a *false* FAILED — the
        reviewer said nothing untrue, the tree simply moved under it.

        So when the round's baseline fingerprint says the tree moved, the
        check does not downgrade: the sub-agent's original verdict stands,
        the check is held for review in ``_deferred_evidence_checks``, and
        an auditable event records the deferral.
        """
        method = str(
            vp.get("verification_method") or vp.get("method") or ""
        ).strip()
        if method not in ("code_review", "ui_validation"):
            return verdict

        vp_id = str(vp.get("id", "unknown"))

        # The tree moved under the check → do not downgrade on that basis.
        # This is *before* ``apply_to_verdict``: the check must not run at
        # all against a tree that is not the one the round produced its
        # citations against.
        if self._tree_mutated_since_baseline():
            self._defer_evidence_check(vp_id, vp, verdict, method)
            return verdict

        try:
            from verification_evidence import apply_to_verdict
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[verification_evidence] unavailable (%s); skipping the "
                "evidence check for %s", exc, vp_id,
            )
            return verdict

        try:
            checked = apply_to_verdict(
                verdict, method, self.plan_dir, self.project_dir, vp_id,
            )
        except Exception:  # pragma: no cover - apply never raises
            logger.exception(
                "[verification_evidence] check crashed for %s", vp_id,
            )
            return verdict

        if checked is not None and checked is not verdict and (
            str(checked.get("status")) != str((verdict or {}).get("status"))
        ):
            self._safe_log_freshness_event(
                vp_id, "evidence_check_downgraded",
                {
                    "method": method,
                    "from": (verdict or {}).get("status"),
                    "to": checked.get("status"),
                    "issues": (checked.get("evidence") or {}).get(
                        "evidence_check", {}
                    ),
                },
            )
        return checked if checked is not None else verdict

    def _tree_mutated_since_baseline(self) -> bool:
        """Has the working tree moved away from the round's baseline?

        ``False`` — "cannot tell, so do not defer" — when there is no
        baseline (the project is not a git tree, or the round never
        recorded one), when the project directory is unknown, or when the
        probe itself fails.  A caller that cannot establish a baseline
        keeps the behaviour it had before the fingerprint existed: it
        does not start refusing to check work on the strength of a
        missing measurement.

        ``True`` means the tree moved: either HEAD changed or the status
        changed.  That is the signal the caller needs to hold the check
        until the concurrent writer is done.
        """
        baseline = getattr(self, "_round_tree_fingerprint", None)
        if not baseline:
            return False
        project_dir = getattr(self, "project_dir", None)
        if not project_dir:
            return False
        try:
            return bool(tree_mutated(project_dir, baseline))
        except Exception:  # pragma: no cover - the probe never raises
            logger.exception(
                "[verification_evidence] tree probe failed for %s",
                project_dir,
            )
            return False

    def _defer_evidence_check(
        self, vp_id: str, vp: dict, verdict: dict, method: str,
    ) -> None:
        """Hold one evidence check for review; keep every entry.

        The deferred check is *not* dropped and *not* de-duplicated: the
        same VP may enter the check more than once in a round, and each
        occurrence is a separate observation of the tree moving under a
        verdict.  Every one of them is kept.

        The append happens on the round's single event loop (the
        executor dispatches VPs through one loop), so no lock is needed —
        and a lock here would be a lie about where the concurrency is.
        """
        deferred = getattr(self, "_deferred_evidence_checks", None)
        if deferred is None:
            deferred = []
            self._deferred_evidence_checks = deferred
        deferred.append((vp_id, vp, verdict))
        self._safe_log_freshness_event(
            vp_id, "evidence_check_deferred_tree_mutated",
            {
                "method": method,
                "status": (verdict or {}).get("status"),
                "baseline": getattr(self, "_round_tree_fingerprint", None),
                "current": self._current_tree_fingerprint(),
            },
        )

    def _current_tree_fingerprint(self) -> Optional[str]:
        """The tree's fingerprint right now, for the audit trail.

        ``None`` when it cannot be read — an unreadable tree is not a
        claim about the tree.
        """
        project_dir = getattr(self, "project_dir", None)
        if not project_dir:
            return None
        try:
            return tree_fingerprint(project_dir)
        except Exception:  # pragma: no cover - the probe never raises
            logger.exception(
                "[verification_evidence] tree fingerprint failed for %s",
                project_dir,
            )
            return None

    def _flush_deferred_evidence_checks(
        self, executor: Any,
    ) -> List[str]:
        """Re-run the evidence checks this round deferred, and write back.

        A deferred check is this round's debt: the check was held back
        because the working tree moved under it, and the round owes the
        operator an answer.  By the end of the round the concurrent
        writer is normally done, so the tree is back where the round
        found it — and the check can be re-run against *that* tree.

        Two outcomes, and they are not symmetric:

          * **tree restored** → the check runs again.  If it now says
            the basis does not hold, the verdict is downgraded and
            written back into the executor's verdict table (through
            the executor, so the index buckets follow the verdict).
          * **tree still different** → the check stays *inconclusive*.
            The sub-agent's original conclusion is kept: an unverified
            PASSED is a smaller lie than a FAILED nobody can
            reproduce.  The inconclusive state is recorded as an event
            so the operator knows a verdict is unverified.

        Never raises.  The flush runs in a ``finally`` next to
        ``executor.run`` — an exception here would replace the round's
        own exception and hide the real failure.

        Args:
            executor: The executor that ran the round — the holder of
                the verdict table the corrections are written back to.

        Returns:
            The ids that were downgraded and written back.
        """
        deferred = list(getattr(self, "_deferred_evidence_checks", None) or [])
        # The table is this round's debt and it is settled here: clear it
        # first, so a flush that dies half-way cannot bill the same
        # check twice (and a second flush is a no-op).
        self._deferred_evidence_checks = []
        if not deferred:
            return []

        # One reading for the whole flush: the tree either came back or
        # it did not.  Re-probing per VP would race against a writer
        # that is still running.
        tree_still_moved = self._tree_mutated_since_baseline()

        updates: Dict[str, Dict[str, Any]] = {}

        for vp_id, vp, verdict in deferred:
            method = str(
                (vp or {}).get("verification_method")
                or (vp or {}).get("method")
                or ""
            ).strip()
            try:
                if tree_still_moved:
                    self._log_inconclusive_evidence_check(
                        vp_id, method, verdict,
                    )
                    continue

                checked = self._recheck_deferred_evidence(
                    vp_id, method, verdict,
                )
                if checked is None:
                    continue
                if str(checked.get("status")) == str(
                    (verdict or {}).get("status")
                ):
                    # The check ran and the basis holds: nothing changes,
                    # so nothing is written and nothing is logged.
                    continue
                self._safe_log_freshness_event(
                    vp_id, "evidence_check_downgraded",
                    {
                        "method": method,
                        "from": (verdict or {}).get("status"),
                        "to": checked.get("status"),
                        "issues": (checked.get("evidence") or {}).get(
                            "evidence_check", {},
                        ),
                        "phase": "round_end_flush",
                    },
                )
                updates[vp_id] = checked
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "[verification_evidence] deferred check failed for %s",
                    vp_id,
                )
                continue

        if not updates:
            return []
        if executor is None:
            return []
        try:
            applied = executor.apply_verdict_updates(updates)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "[verification_evidence] writing back the downgraded "
                "verdicts failed",
            )
            return []
        logger.info(
            "[verification_evidence] round-end flush downgraded %s",
            ", ".join(str(vp_id) for vp_id in applied) if applied else "(nothing)",
        )
        return [str(vp_id) for vp_id in applied]

    def _recheck_deferred_evidence(
        self, vp_id: str, method: str, verdict: dict,
    ) -> Optional[dict]:
        """Re-run one deferred evidence check; ``None`` when it cannot run.

        A check that crashes is not a downgrade — it is a check that did
        not happen.  The original conclusion stands and the crash is
        logged; the round must not die over it.
        """
        apply_fn = _resolve_apply_to_verdict()
        if apply_fn is None:  # pragma: no cover - defensive
            return None
        try:
            checked = apply_fn(
                verdict, method, self.plan_dir, self.project_dir, vp_id,
            )
        except Exception:
            logger.exception(
                "[verification_evidence] deferred check crashed for %s",
                vp_id,
            )
            return None
        if checked is None:
            return None
        return checked

    def _log_inconclusive_evidence_check(
        self, vp_id: str, method: str, verdict: dict,
    ) -> None:
        """Record a check that stays inconclusive (the tree never came back).

        The verdict is **not** touched: the sub-agent's conclusion is
        kept, and the operator gets an event saying the conclusion was
        never checked.
        """
        self._safe_log_freshness_event(
            vp_id, "evidence_check_inconclusive",
            {
                "method": method,
                "status": (verdict or {}).get("status"),
                "baseline": getattr(self, "_round_tree_fingerprint", None),
                "current": self._current_tree_fingerprint(),
                "reason": (
                    "the working tree still differs from the round "
                    "baseline at round end — the evidence check could "
                    "not be re-run"
                ),
            },
        )

    def _run_api_test_vp(self, vp: dict) -> dict:
        """Grade an ``api_test`` VP without an LLM (2026-09-18).

        Runs on the executor's thread pool, so it stays synchronous —
        the round already has a plan-wide concurrency cap and the
        request is bounded by the VP's own ``request.timeout_seconds``.

        The verdict is a pure function of (request, response,
        assertions), which is what makes it reproducible across rounds;
        the raw response is written to
        ``plans/<id>/vp_artifacts/<vp_id>/api_response.json`` so an
        operator can check the verdict's basis afterwards.

        2026-10-24: two things changed around that purity, both of
        them about *where* the verdict comes from rather than what it
        says.

        * The service is checked **before** the request goes out, and
          started if it is down (see
          :meth:`_ensure_api_test_services`).  A request to a dead
          port answers nothing, so there is no verdict to compute.
        * A request that fails at the transport layer carries a
          machine-readable ``signal``, and the verdict is classified by
          that signal: the environment failed, not the deliverable.

        Never raises — :func:`verification_api_runner.run_api_verification`
        turns every failure mode into a FAILED verdict carrying its
        reason, and the guard below turns an unreachable service into a
        BLOCKED verdict.
        """
        from verification_api_runner import run_api_verification

        vp_id = str(vp.get("id", "unknown"))
        # 2026-10-26 (merge): main 的 api_test 前置门禁 + 本分支的绑定/记账，
        # 两边都要，顺序有讲究：
        #
        # 1) **先**问 main 的门禁（``_ensure_api_test_services``）：它可能直接
        #    判 BLOCKED —— 那时请求一次都没发出去，所以本分支的 attempt 计数
        #    必须是 0（「0 = 从没发过请求」这条不变量不能破）。
        # 2) **再**做本分支的绑定与记账：门禁放行之后请求确实会发出去，
        #    这时才记录地址（这个 VP 会被发到哪儿、绑定的是哪个实例）并把
        #    attempt 计数 +1。

        # The service has to be there before the question is asked.
        blocked_ports, detail = self._ensure_api_test_services(vp)
        if blocked_ports:
            self._safe_log_freshness_event(
                vp_id, "api_test_service_blocked",
                {"ports": blocked_ports, "detail": detail},
            )
            return self._build_api_test_blocked_verdict(
                vp_id, blocked_ports, detail,
            )

        # 2026-10-25 (repair-r1-22): the request is built from the
        # instance the round bound — the one its ensure produced.  The
        # URL the VP carries was resolved when the plan was resolved;
        # this is the last word on where it points.
        vp = self._bind_vp_service_address(vp)
        # 2026-10-25 (repair-r1-31): the address this request goes to
        # is recorded **before** it goes there — the transport's own
        # complaint ("Errno 61") says nothing about which port it was
        # aimed at, and the reader needs both.
        address = self._record_vp_address(vp_id, vp)
        self._note_vp_attempt(vp_id)
        try:
            result = run_api_verification(
                vp,
                artifact_dir=self.plan_dir / "vp_artifacts",
            )
        except Exception as exc:  # noqa: BLE001 - runner never raises
            logger.exception(
                "[api_runner] unexpected failure for %s", vp_id,
            )
            return {
                "status": "FAILED",
                "reasons": [
                    f"api_test runner crashed: {type(exc).__name__}: {exc}"
                ],
                "evidence": {"vp_id": vp_id},
            }

        # 2026-10-25 (repair-r1-31): a response came back, so the
        # instance that answered can be named.  Its boot id is what
        # separates "the round's own instance answered" from "somebody
        # else answered" — and it is the round's **own observation** of
        # that port (the block it read from ``/health/data`` while it
        # gated on the instance), not a new question to the service.
        if result.evidence.get("status_code") is not None:
            answering = self._answering_boot_id(address.get("resolved_port"))
            if answering:
                address["answering_boot_id"] = answering

        verdict = result.to_verdict_dict()
        # 2026-10-25 (repair-r1-26): a request that never reached the
        # service is not a verdict about the product.  The
        # classification layer runs **before** the verdict is recorded,
        # so what lands on disk is BLOCKED, not FAILED.
        verdict = self._classify_transport_failure(vp_id, verdict)

        # 2026-10-26 (merge): main 的分类在**之后**：它只往裁决上补两个键
        # （``failure_class`` / ``failure_class_source``），是就地改写；而本分支
        # 的分类会把 FAILED 换成一个**新的**裁决字典 —— 所以必须本分支先跑，
        # 否则 main 补的键会被新字典丢掉。
        self._classify_api_test_verdict(verdict, result)
        self._safe_log_freshness_event(
            vp_id, "api_test_executed",
            {
                "status": verdict["status"],
                "status_code": result.evidence.get("status_code"),
                "assertions_failed": result.evidence.get("assertions_failed"),
                "signal": result.evidence.get("signal"),
            },
        )
        return verdict

    def _classify_transport_failure(
        self, vp_id: str, verdict: dict,
    ) -> dict:
        """Turn "the service was not there" into BLOCKED.

        The api runner never raises: a request that could not be sent
        comes back as a FAILED verdict carrying the transport's own
        words in ``evidence["error"]``.  That is a statement about the
        **plumbing**, not about the product's behaviour, and the
        production round paid for the difference: twelve identical
        connection refusals were recorded as twelve contract
        violations.

        The rule is deliberately narrow:

          * the verdict must be FAILED (a PASSED is never touched);
          * the evidence must carry the transport's error and **no**
            ``status_code`` — a response came back iff the runner
            recorded one, and a response that came back means the
            assertions decided, which this layer does not re-judge;
          * the error must name a transport failure (a refused
            connection, a name that does not resolve, a socket that
            died).  A timeout is excluded on purpose: the service may
            be up and too slow, and that can be a real defect.

        A verdict that passes all of those is reclassified to BLOCKED
        with ``error_kind = service_unreachable``, and the round is
        marked environment blocked (see
        :meth:`_mark_round_environment_blocked`).
        """
        if not isinstance(verdict, dict):
            return verdict
        if str(verdict.get("status")) != "FAILED":
            return verdict
        evidence = verdict.get("evidence")
        if not isinstance(evidence, dict):
            return verdict
        error_text = str(evidence.get("error") or "")
        if not error_text:
            return verdict
        if "status_code" in evidence:
            # A response came back — the assertions decided, and the
            # verdict stands as it is.
            return verdict
        if not _is_transport_failure(error_text):
            return verdict

        request = evidence.get("request")
        if not isinstance(request, dict):
            request = {}
        url = str(request.get("url") or evidence.get("url") or "")
        errno_value = _errno_from_error(error_text)

        new_evidence = dict(evidence)
        new_evidence["error_kind"] = ENVIRONMENT_ERROR_KIND
        new_evidence["reason_code"] = SERVICE_UNREACHABLE
        new_evidence["url"] = url
        if errno_value is not None:
            new_evidence["errno"] = errno_value

        self._mark_round_environment_blocked(
            vp_id, url=url, errno=errno_value, error=error_text,
        )
        return {
            "status": "BLOCKED",
            "reasons": [blocked_reason(
                f"{SERVICE_UNREACHABLE} — the request never reached the "
                f"service ({url or 'no url'}: {error_text})"
            )],
            "actual_result": (
                f"{SERVICE_UNREACHABLE}: the request to {url or '(no url)'} "
                f"never reached the service — {error_text}. The environment, "
                f"not the product, is what failed; this verification point "
                f"is BLOCKED and the round is re-run rather than repaired."
            ),
            "evidence": new_evidence,
            "framework_checks": [],
        }

    def _mark_round_environment_blocked(
        self,
        vp_id: str,
        *,
        url: str = "",
        errno: Optional[int] = None,
        error: str = "",
    ) -> None:
        """Record that the environment — not the product — failed a VP.

        The record is what turns one blocked VP into a blocked **round**:
        the round's results are all rewritten to BLOCKED and the round
        writes its ``verification_round_blocked`` event, so the upper
        layer re-runs the round instead of repairing against verdicts
        that were measured against a broken environment.
        """
        self._environment_blocked = True
        self._environment_blocked_details.append({
            "vp_id": vp_id,
            "url": url,
            "errno": errno,
            "error": error,
        })

    def _environment_blocked_summary(self) -> str:
        """One sentence naming what the environment did to this round."""
        details = self._environment_blocked_details or []
        if not details:
            return (
                "the round could not reach the service the verification "
                "points were sent to"
            )
        parts = []
        for detail in details:
            url = detail.get("url") or "(no url)"
            errno_value = detail.get("errno")
            if errno_value is None:
                parts.append(f"{url}: {detail.get('error') or 'unreachable'}")
            else:
                parts.append(f"{url}: errno {errno_value}")
        return (
            f"the round could not reach the service "
            f"({len(details)} blocked verification point(s): "
            + "; ".join(parts)
            + ")"
        )

    def _write_environment_blocked_event(self) -> None:
        """Tell the round log that the environment took this round out.

        One event per round — not one per VP: the question a log reader
        has is "was this round about the product or about the
        plumbing", and the answer is a property of the round.
        """
        if self._environment_blocked_event_written:
            return
        self._environment_blocked_event_written = True
        self._safe_log_freshness_event(
            "__round__",
            ROUND_BLOCKED_EVENT,
            {
                "plan_id": self.plan_dir.name,
                "round": self._current_round,
                "status": ENVIRONMENT_BLOCKED,
                "error_kind": ENVIRONMENT_ERROR_KIND,
                "reason": self._environment_blocked_summary(),
                "blocked_vps": [
                    detail.get("vp_id")
                    for detail in self._environment_blocked_details
                ],
                "details": list(self._environment_blocked_details),
            },
        )

    def _mark_results_environment_blocked(
        self, results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Every result of an environment-blocked round is BLOCKED.

        The round measured itself against a service that was not
        serving.  Whatever the other verification points concluded, they
        concluded it against that environment — so none of it describes
        the product, and none of it may become a repair task.  The
        round is recorded as blocked and the upper layer re-runs it.

        The verdict each result really got is kept in the evidence
        (``original_status``): blocking a round must not erase what
        happened, it must say what happened *and* that it does not
        count.

        One status is left alone: ``SKIPPED``.  A ``manual_check`` VP
        was never going to be executed — a human has to look at it —
        and the environment is not why.  Rewriting it to BLOCKED would
        throw away the one thing the status says.
        """
        summary = self._environment_blocked_summary()
        marked: List[Dict[str, Any]] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            evidence = result.get("evidence")
            if isinstance(evidence, str):
                try:
                    parsed = json.loads(evidence) if evidence.strip() else {}
                except (json.JSONDecodeError, TypeError, ValueError):
                    parsed = {"raw_evidence": evidence}
                if not isinstance(parsed, dict):
                    parsed = {"raw_evidence": evidence}
            elif isinstance(evidence, dict):
                parsed = dict(evidence)
            else:
                parsed = {}
            original_status = result.get("status")
            # 2026-10-25 (repair-r1-32): the rewrite below replaces the
            # per-point reason code with the round's own, and that code
            # is the **only** thing that tells "the environment stopped
            # this point" apart from "the product broke the contract".
            # It is therefore kept — under its own name, so the rewrite
            # cannot overwrite it a second time.
            if "original_reason_code" not in parsed:
                parsed["original_reason_code"] = parsed.get("reason_code")
            parsed["error_kind"] = ENVIRONMENT_ERROR_KIND
            parsed["reason_code"] = ENVIRONMENT_BLOCKED
            parsed["environment_blocked"] = True
            parsed["environment_blocked_reason"] = summary
            if original_status != "BLOCKED":
                parsed["original_status"] = original_status
            actual = (
                f"{ENVIRONMENT_BLOCKED}: {summary} — this result does not "
                f"describe the product; the round is re-run, not repaired."
            )
            original_result = str(result.get("actual_result") or "").strip()
            if original_result:
                actual = f"{actual}\n(original: {original_result})"
            marked_result = {
                "id": result.get("id"),
                "status": original_status,
                "actual_result": actual,
                "evidence": json.dumps(parsed, ensure_ascii=False),
            }
            reasons = result.get("reasons")
            if reasons:
                marked_result["reasons"] = list(reasons)
            if original_status == "SKIPPED":
                # It was never going to run — that stays true.
                marked_result["status"] = "SKIPPED"
                marked_result["actual_result"] = (
                    f"{original_result or '需要人工验证'} "
                    f"({ENVIRONMENT_BLOCKED}: {summary})"
                )
            else:
                marked_result["status"] = "BLOCKED"
            marked.append(marked_result)
        return marked

    # -----------------------------------------------------------------------
    # The accounting of environment-blocked entries (2026-10-25, repair-r1-32)
    # -----------------------------------------------------------------------
    #
    # One refused connection is one event.  The production round pointed
    # twelve ``api_test`` VPs at ``127.0.0.1:51725`` — a port nothing
    # had ever listened on — and all twelve died inside twenty-two
    # seconds with the same ``URLError [Errno 61]``.  The report then
    # said the product broke twelve times over.
    #
    # The verdict table already says the truth (BLOCKED, not FAILED), but
    # the verdict table is not what the numbers are read from: the
    # FAILED count is derived from the **report's**
    # ``verification_results``, and the judgment LLM is free to write a
    # BLOCKED point down as FAILED.  So the exclusion has to happen
    # where the report is built — the artifact every downstream reader
    # takes its numbers from.
    #
    # The rule is deliberately narrow, because the failure mode it
    # guards against is the opposite one: a real defect must never be
    # re-read as "the environment got in the way".  An entry is excluded
    # only when BOTH halves agree — the status is BLOCKED *and* the
    # reason code names the environment.

    def _exclude_environment_blocked_from_failures(
        self, report_data: dict, execution_results: dict,
    ) -> dict:
        """Keep the entries the round could not judge out of the statistics.

        Two kinds of entry are in that class, and they are counted
        separately because they mean different things:

          * the **environment-blocked** ones — the service was not
            there, so the round never asked the product anything;
          * the **invalid_env** ones — the round's own evidence
            contradicted the failure, so the round does not know what
            happened and will not pretend it does.

        The entries are **kept** — a reader has to be able to see that
        the round could not judge them, and why — and they are
        **annotated** with the reason they were blocked.  What they are
        not is a failure: they leave the FAILED count, and they leave
        the repair path (which reads the report's FAILED entries to
        choose its targets).

        The executor's verdicts are the authority here, not the
        judgment LLM's report: the executor knows whether the request
        ever reached the service, and the LLM only saw a summary of it.

        The round's verdict is re-derived by the same derivation the
        report itself uses (:func:`_derive_overall_status`), so the
        two places cannot drift apart:

          * a genuine failure keeps the round red — this is the whole
            point of the exercise, and the negative control the tests
            pin;
          * a partial verdict stays partial;
          * an entry the environment took out of the round makes it
            ``SKIPPED`` rather than ``PASSED`` — claiming ``PASSED``
            for a round that could not judge everything it set out to
            judge would be a verdict about a product nobody asked.

        A point the round's own evidence refuted (``invalid_env``) is
        annotated the same way and kept out of the failure statistics,
        but it is not an environment block — it says nothing about the
        environment, and on its own it does not take the round's verdict
        down.
        """
        blocked: Dict[str, Dict[str, Any]] = {}
        invalid_env: Dict[str, Dict[str, Any]] = {}
        for result in execution_results.get("execution_results", []) or []:
            if _is_environment_blocked_result(result):
                blocked[str(result.get("id"))] = result
            elif _is_invalid_env_result(result):
                invalid_env[str(result.get("id"))] = result
        if not blocked and not invalid_env:
            return report_data

        report_results = report_data.get("verification_results")
        if not isinstance(report_results, list):
            return report_data

        for entry in report_results:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("id") or "")
            source = blocked.get(entry_id)
            if source is not None:
                entry["status"] = "BLOCKED"
                entry["environment_blocked"] = True
                entry["reason_code"] = (
                    _environment_block_reason_code(source) or SERVICE_UNREACHABLE
                )
                entry["actual_result"] = self._blocked_entry_annotation(source)
                continue
            source = invalid_env.get(entry_id)
            if source is None:
                continue
            entry["status"] = INVALID_ENV
            entry["invalid_env"] = True
            entry["reason_code"] = INVALID_ENV
            entry["actual_result"] = self._invalid_env_entry_annotation(source)

        # The FAILED statistics: the entries the round could not judge
        # are not in them.  The round's verdict comes from the same
        # derivation the report itself uses (:func:`_derive_overall_status`)
        # — one derivation, one answer.
        report_data["overall_status"] = _derive_overall_status(report_results)

        if blocked:
            report_data["environment_blocked"] = True
            report_data["environment_blocked_reason"] = (
                self._environment_blocked_summary()
                if self._environment_blocked
                else self._environment_blocked_entries_summary(blocked)
            )
            report_data["environment_blocked_vps"] = sorted(blocked)
        if invalid_env:
            report_data["invalid_env"] = True
            report_data["invalid_env_reason"] = (
                self._invalid_env_entries_summary(invalid_env)
            )
            report_data["invalid_env_vps"] = sorted(invalid_env)
        return report_data

    @staticmethod
    def _invalid_env_entries_summary(
        invalid_env: Dict[str, Dict[str, Any]],
    ) -> str:
        """One sentence naming the points the round's own evidence refuted."""
        if not invalid_env:
            return "no verification point was refuted by the round's own evidence"
        parts = []
        for vp_id, result in sorted(invalid_env.items()):
            conflict = _result_evidence(result).get("evidence_conflict") or {}
            if str(conflict.get("kind") or "") == "instance_handover":
                before = conflict.get(SERVING_BEFORE_FIELD) or {}
                after = conflict.get(SERVING_AFTER_FIELD) or {}
                parts.append(
                    f"{vp_id}: the instance serving "
                    f"{conflict.get('url') or 'the address'} changed hands "
                    f"(boot {before.get('boot_id') or '?'} before, boot "
                    f"{after.get('boot_id') or '?'} after)"
                )
                continue
            served = conflict.get("succeeded_vp") or "another point"
            parts.append(f"{vp_id}: {conflict.get('url') or 'the address'} "
                         f"was served for {served}")
        return (
            "the round's own evidence contradicts "
            f"{len(invalid_env)} verification point(s) "
            f"({'; '.join(parts)})"
        )

    @staticmethod
    def _invalid_env_entry_annotation(source: Dict[str, Any]) -> str:
        """The text an invalid-env entry carries in the report."""
        conflict = _result_evidence(source).get("evidence_conflict") or {}
        url = conflict.get("url") or "the address"
        if str(conflict.get("kind") or "") == "instance_handover":
            before = conflict.get(SERVING_BEFORE_FIELD) or {}
            after = conflict.get(SERVING_AFTER_FIELD) or {}
            text = (
                f"{INVALID_ENV}: the instance serving {url} changed hands "
                f"while this verification point was being verified — boot "
                f"{before.get('boot_id') or '?'} before, boot "
                f"{after.get('boot_id') or '?'} after. The answer this point "
                f"got cannot be attributed to the instance the round "
                f"started, so it is kept in the report and annotated with "
                f"the reason, and it is not counted as a failure."
            )
        else:
            served = conflict.get("succeeded_vp") or "another point"
            text = (
                f"{INVALID_ENV}: the round's own evidence contradicts this "
                f"verification point — {url} was refused here but was served "
                f"for {served} in the same round. It is kept in the report and "
                f"annotated with the reason, and it is not counted as a "
                f"failure."
            )
        original = str(source.get("actual_result") or "").strip()
        if original:
            text = f"{text}\n(original: {original})"
        return text

    @staticmethod
    def _environment_blocked_entries_summary(
        blocked: Dict[str, Dict[str, Any]],
    ) -> str:
        """One sentence naming the points the environment stopped."""
        if not blocked:
            return "no verification point was blocked by the environment"
        parts = []
        for vp_id, result in sorted(blocked.items()):
            code = _environment_block_reason_code(result) or SERVICE_UNREACHABLE
            parts.append(f"{vp_id}: {code}")
        return (
            "the environment, not the product, stopped "
            f"{len(blocked)} verification point(s) ({'; '.join(parts)})"
        )

    @staticmethod
    def _blocked_entry_annotation(source: Dict[str, Any]) -> str:
        """The text an environment-blocked entry carries in the report."""
        code = _environment_block_reason_code(source) or SERVICE_UNREACHABLE
        text = (
            f"{code}: the environment, not the product, stopped this "
            f"verification point. It is kept in the report and annotated "
            f"with the reason, and it is not counted as a failure."
        )
        original = str(source.get("actual_result") or "").strip()
        if original:
            text = f"{text}\n(original: {original})"
        return text

    # -----------------------------------------------------------------------
    # The round's one automatic re-run (2026-10-25, repair-r1-32)
    # -----------------------------------------------------------------------
    #
    # A single environment hiccup used to end the round: the service was
    # not serving, every point that needed it was BLOCKED, and the round
    # was over.  The round now gets **one** more look:
    #
    #   1. bring the service back (``ensure``) and probe it — the same
    #      three gates a per-point recovery uses: the runtime says it is
    #      ready, the port accepts a connection, the health URL answers;
    #   2. only if it is healthy, re-run the points the environment
    #      blocked;
    #   3. at most once per round.
    #
    # If the second look says the same thing, the round is marked
    # ``environment_blocked`` and the **original evidence is kept** — the
    # first attempt's record of what happened is the record.

    def _rerun_environment_blocked_vps(
        self, plan_data: dict, execution_results: dict,
    ) -> dict:
        """Re-run, once, the points the environment blocked.

        Returns the execution results the report must be built from —
        the same dict when there was nothing to re-run.
        """
        # ``getattr`` and not a plain attribute: an orchestrator that was
        # built without ``__init__`` (the round tests build one that way)
        # never ran the reset, and the flag's absence means "this round
        # has not spent its re-run yet" — which is what it means.
        if getattr(self, "_environment_rerun_attempted", False):
            # One attempt per round.  A second one would buy nothing
            # but cost: the service is either up by now or it is not.
            return execution_results

        results = execution_results.get("execution_results") or []
        blocked = [
            result for result in results if _is_environment_blocked_result(result)
        ]
        if not blocked:
            return execution_results

        self._environment_rerun_attempted = True
        blocked_ids = sorted({str(result.get("id")) for result in blocked})
        logger.warning(
            "[verification] %d verification point(s) were blocked by the "
            "environment (%s); looking at the service once more",
            len(blocked_ids), ", ".join(blocked_ids),
        )

        healthy = self._ensure_and_probe_blocked_services(plan_data, blocked_ids)
        if not healthy:
            # The second look says the same thing.  The round is blocked
            # as a whole and the original evidence stays where it is.
            self._mark_round_environment_blocked(
                ", ".join(blocked_ids),
                error=(
                    "the service was still unreachable after the round's "
                    "one re-ensure"
                ),
            )
            self._write_environment_blocked_event()
            self._save_execution_results(execution_results)
            return execution_results

        rerun = self._dispatch_vps_again(blocked_ids)
        if not rerun:
            return execution_results

        still_blocked = self._merge_rerun_results(
            execution_results, rerun, blocked_ids,
        )
        if still_blocked:
            self._mark_round_environment_blocked(
                ", ".join(still_blocked),
                error=(
                    "the service was reachable again, but the re-run still "
                    "could not reach it"
                ),
            )
            self._write_environment_blocked_event()
        self._save_execution_results(execution_results)
        return execution_results

    def _ensure_and_probe_blocked_services(
        self, plan_data: dict, blocked_ids: List[str],
    ) -> bool:
        """Bring the blocked points' services back and probe them.

        ``ensure`` first, probe second — a service that was not asked to
        come back is not a service the round may talk to, and a service
        that came back but does not answer is not a service either.

        Returns ``True`` when every service the blocked points need is
        healthy.  A round that declares no service has nothing to bring
        back, so it goes straight to the re-run.
        """
        if not self._service_declarations:
            return True

        vps_by_id = {
            str(vp.get("id")): vp
            for vp in (plan_data or {}).get("verification_points", []) or []
            if isinstance(vp, dict)
        }
        if not vps_by_id:
            vps_by_id = {
                str(vp.get("id")): vp
                for vp in (getattr(self, "_executor_vps_by_id", None) or {}).values()
            }

        targets: Dict[str, Any] = {}
        for vp_id in blocked_ids:
            vp = vps_by_id.get(vp_id)
            if vp is None:
                continue
            try:
                targets.update(self._vp_service_targets(vp))
            except Exception as exc:  # noqa: BLE001 — a probe never aborts
                logger.warning(
                    "[verification] could not read the services of %s: %s",
                    vp_id, exc,
                )
        if not targets:
            # Nothing declared that these points need — there is no
            # service to bring back, and the points may be blocked for
            # another reason entirely.
            return True

        runtime_map = None
        try:
            runtime_map = self._ensure_declared_services(targets)
        except Exception as exc:  # noqa: BLE001 — a failed start is a
            # failed start, not a reason to take the round down
            logger.warning(
                "[verification] the round's re-ensure errored: %s", exc,
            )

        verdicts = self._adopt_reensure_runtimes("__rerun__", runtime_map)
        for name, verdict in (verdicts or {}).items():
            if not verdict.get("accepted"):
                continue
            runtime = verdict.get("runtime")
            port = int(getattr(runtime, "port", 0) or 0)
            if port:
                # The service is alive again: it is not a blocked port
                # any more, and its one failed recovery is spent.
                self._unblock_service_port(name, port)
                self._service_reensure_failed.discard(name)
        self._refresh_resolved_service_placeholders()

        for name, decl in targets.items():
            port = int(getattr(decl, "port", 0) or 0)
            if port and not self._service_port_is_live(port):
                logger.warning(
                    "[verification] the service %r is still not listening on "
                    "port %s", name, port,
                )
                return False
            # The one check: the service is asked over the guarded road,
            # with the same client and the same headers the round's own
            # precondition uses.  A declaration that names no health URL
            # is asked nothing — the plan's convention is that such a
            # service is judged by TCP reachability alone.
            record = self._service_health_check(decl, port)
            if record.get("asked") and record.get("status_code") != 200:
                logger.warning(
                    "[verification] the health URL of service %r did not "
                    "answer 200: %s", name,
                    record.get("error") or record.get("status_code"),
                )
                return False
        return True

    def _dispatch_vps_again(self, vp_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Re-dispatch the given points through the round's own dispatcher.

        The dispatcher is the very closure the executor used, so a
        re-run is graded by the same rules as the first attempt — the
        same gates, the same evidence, the same verdicts.
        """
        runner = getattr(self, "_vp_dispatch_runner", None)
        if runner is None:
            return {}
        wanted = set(vp_ids)
        vps = [
            vp for vp in (getattr(self, "_executor_vps_by_id", None) or {}).values()
            if str(vp.get("id")) in wanted
        ]

        async def _dispatch() -> Dict[str, Dict[str, Any]]:
            verdicts: Dict[str, Dict[str, Any]] = {}
            for vp in vps:
                vp_id = str(vp.get("id"))
                try:
                    verdict = await runner(vp)
                except Exception as exc:  # noqa: BLE001 — one point's
                    # failure must not take the re-run down
                    logger.warning(
                        "[verification] the re-run of %s raised: %s",
                        vp_id, exc,
                    )
                    verdict = {
                        "status": "FAILED",
                        "reasons": [
                            f"the re-run raised {type(exc).__name__}: {exc}"
                        ],
                        "evidence": {
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                        },
                    }
                if isinstance(verdict, dict):
                    verdicts[vp_id] = verdict
            return verdicts

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_dispatch())
        import nest_asyncio  # type: ignore
        nest_asyncio.apply()
        return asyncio.run(_dispatch())

    def _merge_rerun_results(
        self, execution_results: dict, rerun: Dict[str, Dict[str, Any]],
        blocked_ids: List[str],
    ) -> List[str]:
        """Fold the re-run's verdicts into the round's results.

        A point the re-run could not reach keeps its **original**
        entry — the first attempt's evidence is the record of what
        happened, and a second refusal does not make it more true.

        Returns the ids of the points that are still blocked.
        """
        results = execution_results.get("execution_results") or []
        positions = {
            str(result.get("id")): index
            for index, result in enumerate(results)
            if isinstance(result, dict)
        }
        vps_by_id = {
            str(vp.get("id")): vp
            for vp in (getattr(self, "_executor_vps_by_id", None) or {}).values()
        }
        still_blocked: List[str] = []
        for vp_id, verdict in rerun.items():
            new_result = self._project_verdict_to_result(
                vp_id, verdict, vps_by_id.get(vp_id),
            )
            if _is_environment_blocked_result(new_result):
                still_blocked.append(vp_id)
                continue
            index = positions.get(vp_id)
            if index is None:
                continue
            results[index] = new_result
        if still_blocked:
            logger.warning(
                "[verification] the re-run could still not reach the service "
                "for %s; the original evidence is kept",
                ", ".join(sorted(still_blocked)),
            )
        return sorted(still_blocked)

    # -----------------------------------------------------------------------
    # The round's own evidence, checked against itself (2026-10-26)
    # -----------------------------------------------------------------------
    #
    # The rationale is at the top of the module: the 02:25 round refused
    # twelve requests on an address it was simultaneously being served
    # on, and the framework believed both halves.  The check below is
    # what the round owes itself before it writes anything down.
    #
    # The rule is deliberately narrow, because the failure mode it
    # guards against is the opposite one: a real defect must never be
    # re-read as "the environment got in the way".  A point qualifies
    # only when
    #
    #   * its own evidence says the transport never reached the service
    #     (a refused connection, a name that does not resolve, a socket
    #     that died — a timeout is excluded, as everywhere else), and
    #   * the round has evidence of that **same address** being served
    #     in the same round.
    #
    # A qualified point is not a failure and not a pass: it is
    # ``invalid_env``, and it gets one more look before the report is
    # built.  What the second look says is what lands.

    def _vps_by_id(self, plan_data: dict) -> Dict[str, dict]:
        """The round's verification points, by id.

        The plan's own points come first — they are what the round was
        asked to verify — and the executor's copy fills the gaps.
        """
        by_id: Dict[str, dict] = {}
        for vp in (plan_data or {}).get("verification_points", []) or []:
            if isinstance(vp, dict) and vp.get("id"):
                by_id[str(vp["id"])] = vp
        for vp_id, vp in (getattr(self, "_executor_vps_by_id", None) or {}).items():
            by_id.setdefault(str(vp_id), vp)
        return by_id

    def _collect_served_addresses(
        self, plan_data: dict, execution_results: dict,
    ) -> Dict[str, Dict[str, Any]]:
        """The addresses the round has evidence of being **served**.

        Three kinds of witness, in the order they are trusted:

          * a result carrying a ``status_code`` — a response came back,
            whatever it said (a 500 proves the service was up just as
            well as a 200 does);
          * a PASSED point whose own declaration names the address it
            drove — the ``ui_validation`` case, where the sub-agent
            drove a browser and the evidence it leaves behind carries
            no status code of its own.  The verdict is what counts here,
            not the round-level rewrite that may have replaced it (see
            :func:`_is_passed_result`);
          * the round's own probe records — the health URL the round
            asked, which answers on the very address that refused the
            verification point.

        Keyed by origin, because that is what a refusal and a success
        have to agree about.
        """
        served: Dict[str, Dict[str, Any]] = {}

        def _remember(url: str, witness: Dict[str, Any]) -> None:
            origin = _url_origin(url)
            if not origin:
                return
            served.setdefault(origin, dict(witness, url=url))

        results = execution_results.get("execution_results") or []

        for result in results:
            if not isinstance(result, dict):
                continue
            evidence = _result_evidence(result)
            if evidence.get("status_code") is None:
                continue
            url = _evidence_url(evidence)
            if not url:
                continue
            _remember(url, {
                "vp_id": str(result.get("id")),
                "kind": "response",
                "status_code": evidence.get("status_code"),
            })

        vps_by_id = self._vps_by_id(plan_data)
        for result in results:
            if not isinstance(result, dict):
                continue
            if not _is_passed_result(result):
                continue
            url = _vp_url(vps_by_id.get(str(result.get("id"))))
            if not url:
                continue
            _remember(url, {
                "vp_id": str(result.get("id")),
                "kind": "passed_point",
            })

        for key, record in (
            getattr(self, "_service_self_check_records", None) or {}
        ).items():
            if not isinstance(record, dict):
                continue
            if record.get("status_code") is None:
                continue
            url = str(record.get("url") or "")
            if not url:
                continue
            name = key[0] if isinstance(key, tuple) else str(key)
            _remember(url, {
                "vp_id": f"service:{name}",
                "kind": "probe",
                "status_code": record.get("status_code"),
            })
        return served

    def _collect_refused_addresses(
        self, execution_results: dict,
    ) -> Dict[str, Dict[str, Any]]:
        """The addresses the round has evidence of being **refused**.

        A result qualifies when it is a verdict about the point (not
        ``PASSED``) and when its own evidence says the transport never
        reached the service.

        A result that carries a ``status_code`` never qualifies: a
        response came back, which means the transport *did* reach the
        service and the assertions decided.  That is the same rule the
        classification layer uses, and for the same reason — the two
        halves have to agree.
        """
        refused: Dict[str, Dict[str, Any]] = {}
        for result in execution_results.get("execution_results") or []:
            if not isinstance(result, dict):
                continue
            if str(result.get("status") or "") not in ("FAILED", "BLOCKED"):
                continue
            if not _result_is_connection_failure(result):
                continue
            evidence = _result_evidence(result)
            url = _evidence_url(evidence)
            if not url:
                continue
            origin = _url_origin(url)
            if not origin:
                continue
            entry = refused.setdefault(origin, {"url": url, "vp_ids": []})
            vp_id = str(result.get("id"))
            if vp_id and vp_id not in entry["vp_ids"]:
                entry["vp_ids"].append(vp_id)
        return refused

    def _detect_round_evidence_conflicts(
        self, plan_data: dict, execution_results: dict,
    ) -> List[Dict[str, Any]]:
        """The places where the round's own evidence contradicts itself.

        Two kinds, and both are the round disagreeing with itself:

          * one conflict per address: the address that was refused, the
            points that were refused on it, and the point that was
            served on it; and
          * the handovers the dispatch saw — the instance serving an
            address changed hands while a point was being verified, so
            the answer that point got belongs to nobody the round can
            name.

        A third joins them (2026-10-26): the round's **instance epoch**
        moved — the instance behind the round's own address changed
        hands between two dispatches — so the points answered under the
        old epoch were answered by somebody the round cannot name.  It
        is the same contradiction, seen from the round's side instead of
        the point's.

        All of them are contradictions of the same kind, so all of them
        are handled by the same rule: the point is not a verdict about
        the product, and it gets the round's one second look.
        """
        served = self._collect_served_addresses(plan_data, execution_results)
        refused = self._collect_refused_addresses(execution_results)
        conflicts: List[Dict[str, Any]] = []
        for origin, entry in refused.items():
            witness = served.get(origin)
            if not witness:
                continue
            conflicts.append({
                "url": entry["url"],
                "origin": origin,
                "failed_vps": sorted(entry["vp_ids"]),
                "succeeded_vp": witness.get("vp_id"),
                "witness": witness,
            })
        conflicts.extend(self._handover_conflicts())
        conflicts.extend(self._epoch_conflicts())
        return conflicts

    def _reconcile_round_evidence_conflicts(
        self, plan_data: dict, execution_results: dict,
    ) -> dict:
        """Check the round's evidence against itself, and act on it.

        The contradicted points are marked ``invalid_env`` — a status
        that says one thing: this is not about the product — and they
        get **one** more look: the service is re-ensured, probed, and
        the point is run again by the round's own dispatcher, under the
        same criteria as the first attempt.  What the second look says
        is what lands; a failure that survives it may be written down as
        a failure, and a refusal that survives it is still a refusal —
        it stays ``invalid_env`` and the round says the environment
        blocked it.

        At most once per round: a second look at the same evidence buys
        nothing but cost.
        """
        if getattr(self, "_round_evidence_reconciled", False):
            return execution_results

        conflicts = self._detect_round_evidence_conflicts(
            plan_data, execution_results,
        )
        if not conflicts:
            return execution_results
        self._round_evidence_reconciled = True

        for conflict in conflicts:
            if str(conflict.get("kind") or "") == "instance_handover":
                before = conflict.get(SERVING_BEFORE_FIELD) or {}
                after = conflict.get(SERVING_AFTER_FIELD) or {}
                logger.warning(
                    "[verification] the instance serving %s changed hands "
                    "while %s was being verified (boot %s before, boot %s "
                    "after); the answer is not a verdict about the product",
                    conflict.get("origin") or conflict.get("url") or "the address",
                    ", ".join(conflict.get("failed_vps") or []),
                    before.get("boot_id") or "?",
                    after.get("boot_id") or "?",
                )
            elif str(conflict.get("kind") or "") == EPOCH_CHANGED_CONFLICT_KIND:
                before = conflict.get("epoch_before") or {}
                after = conflict.get("epoch_after") or {}
                logger.warning(
                    "[verification] the round's instance epoch moved "
                    "(boot %s before, boot %s after); the answers obtained "
                    "under the old epoch are not verdicts about the product",
                    before.get("boot_id") or "?",
                    after.get("boot_id") or "?",
                )
            else:
                logger.warning(
                    "[verification] the round's own evidence contradicts itself "
                    "on %s: %s was refused there while %s was served; the "
                    "refusal is not a verdict about the product",
                    conflict.get("origin"),
                    ", ".join(conflict.get("failed_vps") or []),
                    conflict.get("succeeded_vp"),
                )
            self._safe_log_freshness_event(
                "__round__",
                ROUND_EVIDENCE_CONFLICT_EVENT,
                {
                    "plan_id": self.plan_dir.name,
                    "round": self._current_round,
                    "kind": conflict.get("kind"),
                    "url": conflict.get("url"),
                    "origin": conflict.get("origin"),
                    "failed_vps": conflict.get("failed_vps"),
                    "succeeded_vp": conflict.get("succeeded_vp"),
                    "witness": conflict.get("witness"),
                    SERVING_BEFORE_FIELD: conflict.get(SERVING_BEFORE_FIELD),
                    SERVING_AFTER_FIELD: conflict.get(SERVING_AFTER_FIELD),
                },
            )

        conflicted_ids = self._mark_invalid_env_results(
            execution_results, conflicts,
        )
        if not conflicted_ids:
            return execution_results

        healthy = self._ensure_and_probe_blocked_services(
            plan_data, conflicted_ids,
        )
        if not healthy:
            self._mark_round_environment_blocked(
                ", ".join(conflicted_ids),
                error=(
                    "the service was still unreachable after the round's "
                    "one re-ensure; the contradicted points stay invalid_env"
                ),
            )
            self._write_environment_blocked_event()
            self._save_execution_results(execution_results)
            return execution_results

        rerun = self._dispatch_vps_again(conflicted_ids)
        if not rerun:
            # There was no dispatcher to give the points a second look
            # with.  The round therefore still does not know what
            # happened to them, and it says so.
            self._mark_round_environment_blocked(
                ", ".join(conflicted_ids),
                error=(
                    "the round had no dispatcher to give the contradicted "
                    "points a second look with"
                ),
            )
            self._write_environment_blocked_event()
            self._save_execution_results(execution_results)
            return execution_results

        still_invalid = self._merge_conflict_rerun_results(
            execution_results, rerun, conflicted_ids,
        )
        if still_invalid:
            self._mark_round_environment_blocked(
                ", ".join(still_invalid),
                error=(
                    "the service was reachable again, but the re-run still "
                    "could not produce an answer that belongs to the "
                    "instance this round started"
                ),
            )
            self._write_environment_blocked_event()
        self._save_execution_results(execution_results)
        return execution_results

    def _mark_invalid_env_results(
        self, execution_results: dict, conflicts: List[Dict[str, Any]],
    ) -> List[str]:
        """Mark the contradicted points ``invalid_env``.

        Returns the ids that were marked.
        """
        by_vp: Dict[str, Dict[str, Any]] = {}
        for conflict in conflicts:
            for vp_id in conflict.get("failed_vps") or []:
                by_vp[str(vp_id)] = conflict

        results = execution_results.get("execution_results") or []
        marked: List[str] = []
        for index, result in enumerate(results):
            if not isinstance(result, dict):
                continue
            vp_id = str(result.get("id"))
            conflict = by_vp.get(vp_id)
            if conflict is None:
                continue
            if str(result.get("status") or "") == INVALID_ENV:
                # Already marked — the round's own reconciliation wrote
                # the marker once.  It is not written twice, but the
                # point still counts as contradicted: it gets the
                # round's one second look like every other.
                marked.append(vp_id)
                continue
            results[index] = self._mark_invalid_env(result, conflict)
            marked.append(vp_id)
        return sorted(set(marked))

    @staticmethod
    def _mark_invalid_env(
        result: Dict[str, Any], conflict: Dict[str, Any],
    ) -> Dict[str, Any]:
        """One contradicted point → the entry that says so.

        The status is the point of the marker: ``invalid_env`` is not a
        failure and not a pass, it is "the round's own evidence
        contradicts this one".  Everything else about the entry is kept
        — the refusal, the address, the point that was served on it — so
        that a reader can check the claim instead of taking it.
        """
        evidence = _result_evidence(result)
        new_evidence = dict(evidence)
        original_code = (
            evidence.get("reason_code") or evidence.get("error_kind")
        )
        if original_code and "original_reason_code" not in new_evidence:
            # The per-point code is kept under its own name — a round
            # that was blocked as a whole already put one there, and it
            # says more than the round's own code does.
            new_evidence["original_reason_code"] = original_code
        new_evidence["reason_code"] = INVALID_ENV
        new_evidence["invalid_env"] = True
        new_evidence["evidence_conflict"] = conflict
        if not new_evidence.get("url"):
            new_evidence["url"] = conflict.get("url") or ""

        original_status = result.get("status")
        if original_status != INVALID_ENV:
            new_evidence["original_status"] = original_status

        actual = _invalid_env_message(conflict)
        original_result = str(result.get("actual_result") or "").strip()
        if original_result:
            actual = f"{actual}\n(original: {original_result})"

        marked = {
            "id": result.get("id"),
            "status": INVALID_ENV,
            "actual_result": actual,
            "evidence": json.dumps(new_evidence, ensure_ascii=False),
        }
        reasons = result.get("reasons")
        if reasons:
            marked["reasons"] = list(reasons)
        return marked

    def _merge_conflict_rerun_results(
        self, execution_results: dict, rerun: Dict[str, Dict[str, Any]],
        conflicted_ids: List[str],
    ) -> List[str]:
        """Fold the second look's verdicts into the round's results.

        A verdict that is about the **product** replaces the entry — the
        second look is the one that counts.  A verdict that is about the
        **plumbing** (the service was still not there) leaves the entry
        where it is: ``invalid_env``, because a second refusal does not
        make the first one a product defect.

        A second look that found the address in **another pair of hands**
        is the plumbing case too: the answer still cannot be attributed
        to the instance the round started, so the point stays
        ``invalid_env`` — it is not a verdict about the product, and it
        is not one about the product just because the look was repeated.

        Returns the ids that are still ``invalid_env``.
        """
        results = execution_results.get("execution_results") or []
        positions = {
            str(result.get("id")): index
            for index, result in enumerate(results)
            if isinstance(result, dict)
        }
        vps_by_id = {
            str(vp.get("id")): vp
            for vp in (getattr(self, "_executor_vps_by_id", None) or {}).values()
        }
        still_invalid: List[str] = []
        still_handover: List[str] = []
        for vp_id, verdict in rerun.items():
            new_result = self._project_verdict_to_result(
                vp_id, verdict, vps_by_id.get(vp_id),
            )
            if _result_serving_handover(new_result):
                # The second look found the address in another pair of
                # hands.  The answer still cannot be attributed, so the
                # point stays ``invalid_env``.
                still_handover.append(vp_id)
                still_invalid.append(vp_id)
                continue
            if (
                _is_environment_blocked_result(new_result)
                or _result_is_connection_failure(new_result)
            ):
                # The second look found the service still not there.  A
                # second refusal does not make the first one a product
                # defect, so the point stays ``invalid_env``.
                still_invalid.append(vp_id)
                continue
            index = positions.get(vp_id)
            if index is None:
                continue
            results[index] = new_result
        if still_handover:
            logger.warning(
                "[verification] the second look found the address in another "
                "pair of hands for %s; they stay %s",
                ", ".join(sorted(still_handover)), INVALID_ENV,
            )
        still_refused = sorted(set(still_invalid) - set(still_handover))
        if still_refused:
            logger.warning(
                "[verification] the second look could still not reach the "
                "service for %s; they stay %s",
                ", ".join(still_refused), INVALID_ENV,
            )
        return sorted(set(still_invalid))

    def _project_verdict_to_result(
        self, vp_id: str, verdict: dict, vp: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """One executor verdict → the legacy ``execution_results`` entry."""
        status = verdict.get("status", "FAILED")
        actual_result = verdict.get("actual_result")
        if actual_result is None or actual_result == "":
            reasons = verdict.get("reasons", []) or []
            if isinstance(reasons, list):
                actual_result = "\n".join(str(reason) for reason in reasons)
            else:
                actual_result = str(reasons)
        evidence = verdict.get("evidence", "")
        if not isinstance(evidence, str):
            evidence = json.dumps(evidence, ensure_ascii=False)
        evidence = self._enrich_evidence_with_address(str(vp_id), evidence, vp)
        return {
            "id": vp_id,
            "status": status,
            "actual_result": actual_result,
            "evidence": evidence,
        }

    def _save_execution_results(self, execution_results: dict) -> None:
        """Write the round's results back to SQLite — best effort.

        The report is built from the dict in memory, so this is not on
        the critical path; it is here so a reader that comes along
        later (the next round's resume, the bridge UI) sees the same
        results the report was built from.
        """
        # ``getattr`` for the same reason as elsewhere in this module: an
        # orchestrator built without ``__init__`` has no repository, and
        # "no repository" means "nothing to write to".
        if getattr(self, "verif_repo", None) is None:
            return
        try:
            self.verif_repo.save_execution_results(
                self.plan_dir.name, execution_results,
            )
        except Exception:  # noqa: BLE001 — a write failure is not a
            # reason to fail the round
            pass

    # ------------------------------------------------------------------
    # api_test — the service guard
    # ------------------------------------------------------------------

    def _declared_services_for_vp(self, vp: dict) -> Dict[str, Any]:
        """The declared services this ``api_test`` VP depends on.

        Two independent ways a VP names a service, and both are read:
        the ``uses_services`` names the plan declares, and the port in
        the request URL.  The second one matters because the URL is
        where the port survives — a VP that names its service correctly
        carries the ``{{svc.<name>.url}}`` placeholder, which the
        dispatcher has already resolved by the time the VP gets here.
        """
        declarations = getattr(self, "_service_declarations", {}) or {}
        if not declarations:
            return {}

        wanted: Dict[str, Any] = {}
        uses = vp.get("uses_services")
        if isinstance(uses, list):
            for name in uses:
                key = str(name).strip()
                decl = declarations.get(key) or declarations.get(key.lower())
                if decl is not None:
                    wanted[str(decl.name)] = decl

        port = _port_from_url(str(((vp.get("request") or {}).get("url")) or ""))
        if port is not None:
            for decl in declarations.values():
                try:
                    declared_port = int(getattr(decl, "port", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if declared_port == port:
                    wanted[str(decl.name)] = decl
        return wanted

    @staticmethod
    def _declared_port(decl: Any) -> int:
        """The port a declaration names, or 0 when it names none."""
        try:
            return int(getattr(decl, "port", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _declared_service_is_usable(decl: Any) -> bool:
        """Whether the declared service is there to be asked.

        Two questions, and both have to pass: is something listening on
        the port, and does the service answer the health endpoint the
        plan declared?  A process that holds the port while its data
        plane is dead is exactly the 2026-09-23 failure this area
        exists for — the port answers, the endpoint does not.
        """
        from service_freshness import probe_port

        port = Orchestrator._declared_port(decl)
        if port <= 0:
            return False
        try:
            probe = probe_port(port)
        except Exception:  # noqa: BLE001 - a probe never raises
            return False
        if not probe.listening:
            return False
        health_url = str(getattr(decl, "health_url", "") or "")
        if not health_url:
            return True
        return _health_endpoint_answers(health_url)

    def _ensure_api_test_services(self, vp: dict) -> Tuple[List[int], str]:
        """Make sure the services this VP declares are up before it asks.

        The round-start preflight starts the declared services **once**,
        at the beginning of the round.  A service that dies half-way
        through the round is then left to the VPs that need it: the
        first one to arrive finds a dead port and every api_test after
        it finds the same dead port.

        So the check happens here, immediately before the request: a
        service that is not answering is started with its own declared
        ``start_cmd`` (:func:`service_manager.ensure_services`), and
        only a service that is still not answering afterwards blocks
        the point.

        Returns ``(blocked_ports, detail)``.  ``blocked_ports`` is
        empty when every declared service is usable; ``detail`` says
        why the ones that are not could not be brought up.
        """
        declarations = self._declared_services_for_vp(vp)
        if not declarations:
            return [], ""

        unusable = [
            decl for decl in declarations.values()
            if not self._declared_service_is_usable(decl)
        ]
        if not unusable:
            return [], ""

        try:
            from service_manager import ensure_services

            runtime_map = ensure_services(
                self.project_dir,
                declarations,
                round_number=getattr(self, "_current_round", 0) or 0,
            )
        except Exception as exc:  # noqa: BLE001 - a guard never raises
            ports = sorted({
                self._declared_port(decl) for decl in unusable
            } - {0})
            return ports, (
                f"could not start the declared service(s): "
                f"{type(exc).__name__}: {exc}"
            )

        still: List[Any] = []
        details: List[str] = []
        for decl in declarations.values():
            runtime = (runtime_map.runtimes or {}).get(str(decl.name))
            if runtime is not None and getattr(runtime, "ready", False):
                # ``service_manager`` called it ready, and it only does
                # that after the declared health endpoint answered — so
                # it has already answered the question this guard is
                # asking.  Asking again would turn a slow service into a
                # false BLOCKED.
                continue
            if self._declared_service_is_usable(decl):
                continue
            still.append(decl)
            why = (getattr(runtime, "detail", "") or "") or "not ready"
            details.append(f"port {getattr(decl, 'port', '?')}: {why}")
        if not still:
            return [], ""

        return sorted({
            self._declared_port(decl) for decl in still
        } - {0}), "; ".join(details)

    def _build_api_test_blocked_verdict(
        self, vp_id: str, ports: List[int], detail: str,
    ) -> dict:
        """A BLOCKED verdict for an api_test whose service never came up.

        The envelope is the one the round-start preflight already uses
        (:meth:`_build_service_freshness_blocked_verdict`), so a reader
        sees the same shape whichever gate blocked the point.  Two
        things are added, and both of them are what makes this gate's
        verdict actionable instead of a dead end:

        * the machine-readable ``signal``, so the failure is classified
          by rule rather than by whoever reads the log;
        * the class, which is ``infra`` — the environment failed, so
          the attempt is worth another try and does **not** count
          against the same-cause threshold.

        2026-10-24 (repair-r3-16): the body now lives in
        :meth:`_build_service_gate_blocked_verdict`, which the
        round-start gate uses too — one shape for "the service was not
        reachable", whichever gate noticed.
        """
        verdict = self._build_service_gate_blocked_verdict(
            vp_id, ports, detail,
        )
        verdict["actual_result"] = (
            f"service not ready on port(s) {ports}: "
            f"{detail or 'no listener'}; the api_test request was not "
            f"sent — the environment was not reachable, so no assertion "
            f"ran and the deliverable is not implicated."
        )
        return verdict

    # ------------------------------------------------------------------
    # The service gate — recoverable, and diagnosable when it blocks
    # ------------------------------------------------------------------

    def _build_service_gate_blocked_verdict(
        self, vp_id: str, ports: List[int], detail: str,
    ) -> dict:
        """A BLOCKED verdict for a service that is still unreachable.

        Same envelope as the other gates, plus the two things that make
        it actionable rather than a dead end:

        * ``signal`` — the machine-readable marker the four-class triage
          reads (``service_not_ready`` → infra): the environment
          failed, so the attempt is worth retrying and does **not**
          count against the same-cause threshold;
        * ``service_detail`` — the diagnosis the service manager
          already produced (exit code, first error, log tail).  A
          verdict that carries only a port number leaves the next round
          with nothing to act on, which is exactly how this failed five
          rounds in a row.
        """
        from verification.failure_triage import INFRA, SOURCE_DETERMINISTIC

        verdict = self._build_service_freshness_blocked_verdict(vp_id, ports)
        evidence = verdict.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        evidence["signal"] = API_TEST_SERVICE_NOT_READY_SIGNAL
        evidence["blocked_ports"] = list(ports)
        if detail:
            evidence["service_detail"] = detail
        verdict["evidence"] = evidence
        verdict["actual_result"] = (
            f"service not ready on port(s) {ports} after a recovery "
            f"attempt: {detail or 'no listener'}; the environment was "
            f"not reachable, so no assertion ran and the deliverable is "
            f"not implicated."
        )
        verdict["failure_class"] = INFRA
        verdict["failure_class_source"] = SOURCE_DETERMINISTIC
        return verdict

    def _vp_service_ports(self, vp: dict) -> set:
        """The ports a VP touches.

        Two sources, and both are needed:

        * the ports in the VP's own text — the legacy path, where the
          preflight scraped the ports out of the command text;
        * the ports of the declared services the VP names.  A VP that
          uses the declaration carries a ``{{svc.<name>.url}}``
          placeholder instead of a literal port, so the declaration
          table is the only place its port survives.
        """
        from service_freshness import extract_service_ports

        ports = set(extract_service_ports(
            {"verification_points": [vp]}, self.project_dir,
        ))
        uses = vp.get("uses_services")
        if isinstance(uses, list):
            for name in uses:
                key = str(name).strip()
                decl = (
                    self._service_declarations.get(key)
                    or self._service_declarations.get(key.lower())
                )
                if decl is not None:
                    ports.add(self._declared_port(decl))
        return ports

    def _service_gate_verdict(self, vp: dict) -> Optional[dict]:
        """The round-start service gate, asked per VP — and asked again.

        2026-10-24 (repair-r3-16).  The gate used to be a **one-shot
        decision**: the round-start preflight tried to bring the plan's
        declared services up once, and whatever it concluded at that
        instant became the verdict of every VP that touched the port —
        for the whole round, with no second attempt.

        That is what made VP-001 fail five rounds in a row with
        ``blocked_ports`` and not one assertion executed: the service
        was slow to start, the preflight gave up on it, and the VPs
        inherited the give-up instead of trying themselves.

        So the gate tries **again** before it blocks:

          * the service may already be up — the preflight looked before
            the service finished starting;
          * otherwise the declared service is started once more, and
            the VPs that arrive together share that one attempt (see
            :meth:`_recover_service_port`).

        Only a service that is still unreachable after the recovery
        blocks the VP — and then the verdict says **why**: the
        machine-readable ``signal`` the four-class triage reads
        (``service_not_ready`` → infra → retry, do not count) and the
        diagnosis the service manager already produced.

        Returns the BLOCKED verdict, or ``None`` when the VP may run.
        """
        if not self._blocked_service_ports:
            return None

        vp_id = str(vp.get("id", "unknown"))
        blocked = sorted(
            self._vp_service_ports(vp) & self._blocked_service_ports
        )
        if not blocked:
            return None

        still_blocked, detail = self._recover_blocked_service_ports(
            vp, blocked,
        )
        if not still_blocked:
            # The service came up on the retry: the VP runs, and its own
            # request and assertions decide the verdict.
            self._safe_log_freshness_event(
                vp_id, "service_gate_recovered", {"ports": blocked},
            )
            return None

        self._safe_log_freshness_event(
            vp_id, "service_freshness_blocked",
            {"ports": still_blocked, "detail": detail},
        )
        return self._build_service_gate_blocked_verdict(
            vp_id, still_blocked, detail,
        )

    def _recover_blocked_service_ports(
        self, vp: dict, ports: List[int],
    ) -> Tuple[List[int], str]:
        """Try once more to bring the blocked ports up.

        One attempt per port per round, shared by every VP that needs
        it: a round has a dozen VPs pointing at the same declared
        service, and a dozen start attempts do not make a service come
        up — they make a dozen copies fight over the port, and the
        losers of that fight are the ones whose start command exits with
        EADDRINUSE.

        Returns ``(still_blocked, detail)``.
        """
        still: List[int] = []
        details: List[str] = []
        for port in sorted(ports):
            recovered, detail = self._recover_service_port(port)
            if recovered:
                continue
            still.append(port)
            if detail:
                details.append(f"port {port}: {detail}")
        return still, "; ".join(details)

    def _service_recovery_lock_for(self, port: int) -> threading.Lock:
        """The lock that serialises this round's recovery of one port."""
        with self._service_recovery_guard:
            lock = self._service_recovery_locks.get(port)
            if lock is None:
                lock = threading.Lock()
                self._service_recovery_locks[port] = lock
            return lock

    def _recover_service_port(self, port: int) -> Tuple[bool, str]:
        """Bring one port up — at most once per round.

        The lock is what makes "at most once" true: the first caller
        starts the service, every other caller waits for that attempt
        and then reads its outcome instead of starting a second copy.
        """
        with self._service_recovery_lock_for(port):
            outcome = self._service_recovery_outcomes.get(port)
            if outcome is not None:
                return outcome, self._blocked_service_details.get(port, "")

            declarations = {
                name: decl
                for name, decl in self._service_declarations.items()
                if self._declared_port(decl) == port
            }
            if not declarations:
                # A port this plan never declared (the legacy
                # scrape-the-command path): there is no start command to
                # retry with, so the preflight's verdict stands.
                self._service_recovery_outcomes[port] = False
                return False, self._blocked_service_details.get(port, "")

            # The service may have finished starting after the preflight
            # looked.  Ask it before starting anything: a service that
            # is already up must not be restarted by its own gate.
            for decl in declarations.values():
                if self._declared_service_is_usable(decl):
                    self._service_recovery_outcomes[port] = True
                    self._blocked_service_ports.discard(port)
                    self._blocked_service_details.pop(port, None)
                    return True, ""

            try:
                from service_manager import ensure_services

                runtime_map = ensure_services(
                    self.project_dir, declarations,
                    round_number=getattr(self, "_current_round", 0) or 0,
                )
            except Exception as exc:  # noqa: BLE001 - a guard never raises
                detail = (
                    f"could not start the declared service(s): "
                    f"{type(exc).__name__}: {exc}"
                )
                self._service_recovery_outcomes[port] = False
                self._blocked_service_details[port] = detail
                return False, detail

            detail = ""
            ready = False
            for decl in declarations.values():
                runtime = (runtime_map.runtimes or {}).get(str(decl.name))
                if runtime is not None and getattr(runtime, "ready", False):
                    ready = True
                    break
                if runtime is not None:
                    detail = getattr(runtime, "detail", "") or detail
            if not ready:
                # The manager only calls a service ready after its
                # health endpoint answered, but ask the service directly
                # before giving up — the port may have come up between
                # the manager's check and this one.
                for decl in declarations.values():
                    if self._declared_service_is_usable(decl):
                        ready = True
                        break

            if ready:
                self._service_recovery_outcomes[port] = True
                self._blocked_service_ports.discard(port)
                self._blocked_service_details.pop(port, None)
                return True, ""

            detail = detail or "not ready"
            self._service_recovery_outcomes[port] = False
            self._blocked_service_details[port] = detail
            return False, detail

    @staticmethod
    def _classify_api_test_verdict(verdict: dict, result: Any) -> None:
        """Classify a verdict the runner produced, in place.

        A verdict that carries a ``signal`` never reached the service,
        so the failure belongs to the environment.  The class comes
        from :func:`verification.failure_triage.classify_deterministic`
        — a rule, not a guess — and the source says the class was
        decided by that rule rather than assumed.
        """
        evidence = getattr(result, "evidence", None) or {}
        signal = evidence.get("signal")
        if not signal:
            return
        from verification.failure_triage import (
            SOURCE_DETERMINISTIC,
            classify_deterministic,
        )

        failure_class = classify_deterministic(
            signal, str(evidence.get("error") or ""),
        )
        if not failure_class:
            return
        verdict["failure_class"] = failure_class
        verdict["failure_class_source"] = SOURCE_DETERMINISTIC

    def _run_full_ci_vp(self, vp: dict) -> dict:
        """Grade a ``full_ci`` VP without an LLM (2026-09-18, D2).

        The Phase-2 sibling of :meth:`_run_api_test_vp`: the framework
        runs the repo's CI entry and the exit code decides. Same reason
        for being LLM-free — the verdict must be a pure function of a
        declared input, so it is reproducible across rounds, and nothing
        about it rests on a sub-agent's self-report.

        Runs on the executor's thread pool. A whole-repo gate is the one
        command in the system allowed to take tens of minutes; it is
        bounded by the VP's own ``ci_timeout_seconds`` (default 3600) and
        is killed at that ceiling rather than left to eat the round.

        Never raises — :func:`verification_ci_runner.run_ci_verification`
        turns every failure mode into a FAILED verdict carrying its reason.
        """
        from verification_ci_runner import run_ci_verification

        vp_id = str(vp.get("id", "unknown"))
        try:
            result = run_ci_verification(
                vp,
                project_dir=self.project_dir,
                artifact_dir=self.plan_dir / "vp_artifacts",
            )
        except Exception as exc:  # noqa: BLE001 - runner never raises
            logger.exception(
                "[ci_runner] unexpected failure for %s", vp_id,
            )
            return {
                "status": "FAILED",
                "reasons": [
                    f"full_ci runner crashed: {type(exc).__name__}: {exc}"
                ],
                "evidence": {"vp_id": vp_id, "method": "full_ci"},
            }

        verdict = result.to_verdict_dict()
        self._safe_log_freshness_event(
            vp_id, "full_ci_executed",
            {
                "status": verdict["status"],
                "exit_code": result.evidence.get("exit_code"),
                "timed_out": result.evidence.get("timed_out"),
            },
        )
        return verdict

    async def _run_single_vp_async(self, vp: dict) -> dict:
        """Run a single verification point by delegating to a sub-agent.

        The parent agent no longer shells out directly — every per-VP
        execution is delegated to :class:`VerificationSubAgent.run`,
        which builds a temp settings.json with the 8-hook security
        net and invokes the coding tool inside its own self-heal
        loop. The parent observes the outcome via the returned
        :class:`Verdict` and converts it back to a result dict with
        the same shape the legacy per-method executors produced.

        Boundary semantics pinned by the TDD spec:

        * ``manual_check`` VPs that survived Phase 1 filtering (e.g.
          a plan loaded from an older on-disk version) short-circuit
          to ``SKIPPED`` — the canonical Phase 1 behaviour. The
          sub-agent's :class:`MethodTemplateRegistry` retains a
          ``manual_check`` template for completeness, but the
          sub-agent itself does not run for ``manual_check`` because
          the spec delegates the "drop" decision to Phase 1.
        * No per-VP ``asyncio.wait_for`` wrapper. The cap is enforced
          one layer up by :class:`VerificationSubAgent._execute_attempt`
          (1-hour wall-clock via ``HARD_WALL_CLOCK_CAP_SECONDS=3600``).
          Adding a tighter inner cap (e.g. 60-300s) used to fire
          ``asyncio.TimeoutError`` long before the 1-hour outer cap,
          which routed to a recursive LLM-split pipeline that ran
          away 8 levels deep (VP-034, 2026-09-08). The 15-min inner
          idle detector (``coding_tool._total_timer``) catches hung
          subprocesses via :class:`HardTimeoutError` instead.
        * On :class:`HardTimeoutError` (inner idle 15-min SIGKILL)
          we attempt clause-level split, falling back to LLM-driven
          split for single-clause VPs. If both decline, we return
          ``status="timeout"`` (not ``"FAILED"``) so the repair-task
          generator can distinguish a hung VP from a genuinely-
          failing one.
        * On any other exception (sub-agent raised, coding tool
          transport error, etc.) we return ``status="FAILED"`` with
          the exception message as evidence.
        """
        method = vp.get("verification_method", "manual_check")
        vp_id = vp.get("id", "unknown")
        # 2026-09-13: per-VP timeout override deleted — resolve() is
        # method-level only and always returns the flat 1-hour cap value
        # (surface metadata for the vp_start event, not enforcement).
        timeout_seconds = self.timeout_policy.resolve(method)

        # Log verification point start
        self.persistence.write_verification_point_log(
            vp_id,
            "vp_start",
            {
                "title": vp.get("title", ""),
                "method": method,
                "priority": vp.get("priority", ""),
                "expected_result": vp.get("expected_result", ""),
                "timeout_seconds": timeout_seconds,
            }
        )

        # ``manual_check`` is the trivial case — no async work, no
        # timeout. Keep it as a plain return so the wrapped path
        # doesn't needlessly pay the asyncio.wait_for overhead. In
        # the canonical pipeline Phase 1 has already dropped these
        # from the plan, but the guard is kept for plans loaded
        # from older on-disk versions.
        if method == "manual_check":
            result = {
                "id": vp_id,
                "status": "SKIPPED",
                "actual_result": "需要人工验证",
                "reasons": ["需要人工验证"],
                "evidence": "验证方法为 manual_check，需要人工介入"
            }
        else:
            try:
                # Legacy backwards-compatible bridge: tests monkey-patch
                # ``_execute_{method}`` or ``_execute_{method}_async``
                # on the agent instance (plain functions taking only
                # ``(vp)``).  Class-defined stubs are bound methods
                # whose ``self`` is handled by Python automatically.
                # We detect the two shapes by checking
                # ``type(executor).__name__`` — ``method`` for class-
                # defined (the canonical path) and ``function`` for
                # monkey-patched test stubs.
                #
                # The ``_async`` variant is checked FIRST because tests
                # patch both spellings to drive the timeout/split path.
                executor_async = getattr(self, f"_execute_{method}_async", None)
                executor_sync = getattr(self, f"_execute_{method}", None)
                if callable(executor_async) and type(executor_async).__name__ == "function":
                    raw = await executor_async(vp)
                    if isinstance(raw, dict):
                        result = raw
                    else:
                        result = self._verdict_to_result(vp_id, raw)
                elif callable(executor_sync) and type(executor_sync).__name__ == "function":
                    raw = await executor_sync(vp)
                    if isinstance(raw, dict):
                        result = raw
                    else:
                        result = self._verdict_to_result(vp_id, raw)
                else:
                    # Canonical sub-agent path.
                    # NOTE: NO ``asyncio.wait_for`` here. Per-VP
                    # timeout is enforced one layer up by
                    # ``VerificationSubAgent._execute_attempt`` (1-hour
                    # wall-clock cap). The inner 15-min idle detector
                    # in ``coding_tool._total_timer`` catches hung
                    # subprocesses via ``HardTimeoutError``.
                    verdict = await self._delegate_to_sub_agent(vp, method, vp_id)
                    result = self._verdict_to_result(vp_id, verdict)
            except HardTimeoutError as hte:
                # 2026-09-08: inner coding_tool SIGKILL fired
                # (subprocess silent for ``total_sec``) OR the outer
                # 1-hour cap fired. Either way, the task is too big to
                # complete in budget → route to auto-split.
                #
                # HardTimeoutError IS NOT asyncio.TimeoutError
                # (they are siblings under the builtin TimeoutError),
                # so this branch is distinct from the existing
                # ``except asyncio.TimeoutError`` below.
                error_msg = (
                    f"HARD TIMEOUT: {hte} (method={method}, vp_id={vp_id})"
                )
                self.persistence.write_verification_point_log(
                    vp_id,
                    "vp_hard_timeout",
                    {
                        "method": method,
                        "error": error_msg,
                        "hard_timeout_total_sec": getattr(hte, "total_sec", None),
                        "hard_timeout_elapsed": getattr(hte, "elapsed", None),
                    },
                )
                if self.logger:
                    self.logger.warning(
                        "vp_hard_timeout_routing_to_split",
                        f"[HARD TIMEOUT] vp_id={vp_id} — triggering auto-split "
                        f"(total_sec={getattr(hte, 'total_sec', '?')}, "
                        f"elapsed={getattr(hte, 'elapsed', '?'):.1f}s)",
                        task_id=vp_id,
                    )
                split_result = await self._split_vp_on_timeout(vp, {
                    "id": vp_id,
                    "status": "hard_timeout",
                    "actual_result": error_msg,
                    "reasons": [error_msg],
                    "evidence": "HardTimeoutError",
                })
                if split_result is not None:
                    # Mark plan as in repair so the operator / Feishu
                    # card sees a meaningful state instead of "stalled".
                    self._enter_repairing_state(vp_id)
                    return split_result
                # Single-clause VP and split declined — fall back to
                # LLM-driven split (asks LLM how to decompose this
                # single-clause VP into 2-4 sub-VPs).
                llm_split_result = await self._llm_split_vp_on_hard_timeout(vp, hte)
                if llm_split_result is not None:
                    self._enter_repairing_state(vp_id)
                    return llm_split_result
                # All split attempts declined — return timeout verdict.
                return {
                    "id": vp_id,
                    "status": "timeout",
                    "actual_result": error_msg,
                    "reasons": [error_msg],
                    "evidence": "HardTimeoutError",
                }
            # NOTE: No ``except asyncio.TimeoutError`` branch. The
            # per-VP ``asyncio.wait_for`` wrapper was removed
            # (2026-09-08: the outer 1-hour + 15-minute caps are enough).
            # Any ``asyncio.TimeoutError``
            # reaching this scope would mean the outer 1-hour cap in
            # ``VerificationSubAgent._execute_attempt`` fired — that
            # layer catches it and routes to the summarizer fallback,
            # not here.
            except ProviderSlotWaitExpired:
                # The verification point never reached a provider: its
                # scene's tier was saturated past the slot wait, so the
                # dispatch gave the wait back rather than sleep on it.
                # The arm below would record that as FAILED, which would be
                # a claim about the work that nothing observed. Re-raise to
                # the gate wrapper, which hands the capacity gates back and
                # puts the point on the queue again.
                raise
            except Exception as e:
                # Defensive catch — surface as FAILED so a buggy
                # method doesn't take the whole batch down.
                self.persistence.write_verification_point_log(
                    vp_id,
                    "vp_exception",
                    {"method": method, "error": str(e)}
                )
                error_msg = str(e)
                result = {
                    "id": vp_id,
                    "status": "FAILED",
                    "actual_result": f"执行失败: {error_msg}",
                    "reasons": [f"执行失败: {error_msg}"],
                    "evidence": error_msg,
                }

        # Log verification point completion — the single vp_complete
        # emission for this VP. The payload is built by
        # ``_vp_complete_payload`` so its shape has one definition;
        # this call site supplies only what varies per run.
        self.persistence.write_verification_point_log(
            vp_id,
            "vp_complete",
            self._vp_complete_payload(
                vp_id,
                # ``getattr`` rather than ``self._current_round``: this
                # method is reached by tests that build an Orchestrator
                # without running ``__init__``, and a missing round
                # number must not cost them the whole completion event.
                getattr(self, "_current_round", 0) or 1,
                # ``result`` reaches this point from several branches
                # (the manual_check short-circuit, monkey-patched
                # executors, the timeout/split paths, the defensive
                # catch) and only the canonical sub-agent branch built
                # it from a Verdict. Re-expressing it as one here means
                # the payload builder has a single input type, and the
                # three classification fields ride along on the branch
                # that produced them while the others fall back to the
                # conservative defaults rather than inventing a class.
                Verdict(
                    verdict=str(result.get("status", "UNKNOWN")),
                    reasons=[str(result.get("actual_result", ""))],
                    failure_class=result.get("failure_class", DEFAULT_FAILURE_CLASS),
                    failure_class_source=result.get(
                        "failure_class_source", DEFAULT_FAILURE_CLASS_SOURCE
                    ),
                    evidence_quote=result.get("evidence_quote", ""),
                ),
            ),
        )

        return result

    # ------------------------------------------------------------------
    # Per-method legacy executor stubs (tests monkey-patch these).
    # ------------------------------------------------------------------

    async def _execute_ui_validation(self, vp: dict, vp_id: Optional[str] = None) -> Any:
        """Legacy stub for ui_validation — tests replace this."""
        return await self._delegate_to_sub_agent(vp, "ui_validation", vp_id or vp.get("id", "unknown"))

    async def _execute_code_review(self, vp: dict, vp_id: Optional[str] = None) -> Any:
        """Legacy stub for code_review — tests replace this."""
        return await self._delegate_to_sub_agent(vp, "code_review", vp_id or vp.get("id", "unknown"))

    async def _delegate_to_sub_agent(
        self, vp: dict, method: str, vp_id: str
    ) -> Verdict:
        """Construct a :class:`VerificationSubAgent` and call :meth:`run`.

        This is the single boundary between the parent
        :class:`VerificationAgent` and the per-method sub-agent. The
        parent does not call the non-JSON coding-tool surface or
        import the ``subprocess`` module — it constructs the
        sub-agent with the appropriate method hint and lets
        :meth:`VerificationSubAgent.run` drive the work, including
        the 8-hook security net, the self-heal loop, and the verdict
        parse.

        Args:
            vp: The verification-point dict (passed straight through
                as ``vp_node``).
            method: The verification method string, used to pick
                the sub-agent's method template and to hint the
                model-complexity tier.
            vp_id: Convenience copy of ``vp["id"]`` so log lines
                don't repeat the dict lookup.

        Returns:
            The :class:`Verdict` produced by the sub-agent. The
            verdict has already been coerced (``SKIPPED`` →
            ``FAILED``) and validated by :func:`parse_verdict` inside
            the sub-agent — the parent can trust the verdict
            contract without re-parsing.
        """
        # Model-complexity tier: simple for low-cognitive methods,
        # complex for puppeteer / API boundary / LLM-judged checks.
        # 2026-09-13 provider routing: the hint is now a REAL scene —
        # every VP routes via ``verification`` (medium tier, per user
        # directive there is no separate low tier for "simple" VPs).
        # The complexity value stays on the Verdict for observability.
        if method in ("automated_test", "code_review"):
            model_complexity = "simple"
        else:
            model_complexity = "complex"

        # 2026-08-25: pass plan_id + the global sub-agent watchdog
        # registry into ``VerificationSubAgent`` so that hung
        # ``scoped_tool.query_json(...)`` calls can be detected by the
        # HeartbeatMonitor. ``sub_agent_registry`` is None-safe — if
        # the importer is unavailable the legacy behaviour is preserved.
        from sub_agent_registry import sub_agent_registry

        sub_agent = VerificationSubAgent(
            method=method,
            # 2026-09-14: 2 retries + initial try = 3
            # total attempts, hard cap. Beyond that the VP goes to
            # repair / split instead of re-running the suite again.
            max_retries=2,
            model_complexity=model_complexity,
            scene="verification",
            plan_id=self.plan_dir.name,
            registry=sub_agent_registry,
        )

        log_dir = self.plan_dir / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # If the log dir can't be created, let the sub-agent
            # fall back to its default ``Path("logs")``. Don't
            # propagate — this is best-effort.
            log_dir = None  # type: ignore[assignment]

        verdict = await sub_agent.run(
            vp_node=vp,
            coding_tool=self.coding_tool,
            log_dir=log_dir,
            project_dir=self.project_dir,
            plan_id=self.plan_dir.name,
        )

        # Persist a sub-agent_completed event so the JSONL audit
        # log can correlate the sub-agent's verdict with the
        # parent's vp_start / vp_complete envelope.
        try:
            self.persistence.write_verification_point_log(
                vp_id,
                "sub_agent_completed",
                {
                    "method": method,
                    "verdict": verdict.verdict,
                    "reasons": list(verdict.reasons),
                    "evidence_count": len(verdict.evidence),
                    "provider": verdict.provider,
                    "chosen_model": verdict.chosen_model,
                    "model_complexity": verdict.model_complexity,
                },
            )
        except Exception:
            pass

        return verdict

    @staticmethod
    def _verdict_to_result(vp_id: str, verdict: Verdict) -> dict:
        """Map a :class:`Verdict` to the legacy result-dict shape.

        The Phase 2 pipeline (and the report generator) consume a
        dict with at least ``id`` / ``status`` / ``actual_result`` /
        ``evidence``. The sub-agent returns a :class:`Verdict` with
        a richer shape (``verdict`` / ``reasons`` / ``evidence``
        list / provider metadata), so this helper collapses the
        two:

        * ``verdict.verdict`` → ``status`` (already coerced —
          SKIPPED never reaches here).
        * ``verdict.reasons`` (**all entries, newline-joined**) →
          ``actual_result``.
        * ``verdict.evidence`` (joined) → ``evidence``.
        * Provider metadata is dropped at this layer; it is
          surfaced in the persistence log via
          ``sub_agent_completed`` for auditability, not in the
          per-VP result dict.
        * ``failure_class`` / ``failure_class_source`` /
          ``evidence_quote`` are carried through unchanged so the
          result dict is self-sufficient for classification. See the
          fallback block below for what a verdict without them
          resolves to.

        2026-10-11: ``actual_result`` used to be ``reasons[0]``
        only. That is systematically wrong: the sub-agent emits
        one reason per acceptance criterion, **satisfied criteria
        first, the failing one last** (confirmed on VP-016 round
        4: ``reasons[0]`` was criterion ① passing at 246 chars,
        while the real failure — criterion ④'s second half — sat
        at ``reasons[4]``, 623 chars). Taking index 0 therefore
        throws away the failure reason and hands the report writer
        / repair-task generator a VP that "passed" every check
        while the verdict says FAILED. Across 34 FAILED verdicts
        ``reasons[0]`` carried only ~25% (median) of the reason
        text, and in 41% of cases mentioned the failure not at all.
        Joining all reasons — the same thing
        :meth:`_build_execution_results_from_executor` already did
        on the executor path — makes the failure reason survive to
        the downstream consumers.
        """
        status = str(verdict.verdict) if verdict.verdict else "FAILED"
        # Keep every reason, in order. The failing criterion is the
        # LAST one, so truncating to a prefix (or keeping only
        # reasons[0]) is exactly how the failure reason got lost.
        # The "satisfied criteria first" convention is what makes
        # this a prefix-vs-suffix problem rather than a "pick the
        # best line" one. If no reasons were provided at all, fall
        # back to a generic message so the downstream report always
        # has *something* to show.
        if verdict.reasons:
            actual_result = "\n".join(str(r) for r in verdict.reasons)
        else:
            actual_result = f"verdict={status}"
        evidence = " | ".join(str(e) for e in verdict.evidence) if verdict.evidence else ""

        # The three classification fields travel with the projection
        # because a caller holding only the result dict cannot recompute
        # them: ``failure_class`` is what the loop counts, the source
        # says whether that class was earned or assumed, and the quote
        # is the anchor a human needs before acting on the claim.
        #
        # The fallback keys on a **missing attribute**, not on an empty
        # one. That distinction is load-bearing in both directions: a
        # verdict that predates these fields has genuinely not been
        # classified, and absence must not read as ``out_of_scope`` /
        # ``infra`` / ``criterion_defect`` (each of which refuses to
        # count the attempt) — it reads as ``deliverable_defect``, the
        # class that does count, marked ``"defaulted"`` so a reader can
        # tell it was assumed. But a *passed* verdict carries an empty
        # class on purpose, and reading that empty string as "default
        # it" would report every green VP as a broken deliverable.
        raw_class = getattr(verdict, "failure_class", None)
        failure_class = DEFAULT_FAILURE_CLASS if raw_class is None else str(raw_class)

        raw_source = getattr(verdict, "failure_class_source", None)
        failure_class_source = (
            DEFAULT_FAILURE_CLASS_SOURCE if raw_source is None else str(raw_source)
        )

        raw_quote = getattr(verdict, "evidence_quote", None)
        evidence_quote = "" if raw_quote is None else str(raw_quote)
        return {
            "id": vp_id,
            "status": status,
            "reasons": list(verdict.reasons) if verdict.reasons else [],
            "evidence": {"details": evidence, "raw": list(verdict.evidence) if verdict.evidence else []},
            "actual_result": actual_result,
            "failure_class": failure_class,
            "failure_class_source": failure_class_source,
            "evidence_quote": evidence_quote,
        }

    @staticmethod
    def _vp_complete_payload(
        vp_id: str, round_number: int, verdict: Verdict
    ) -> dict:
        """Build the one ``vp_complete`` payload for a finished VP.

        The event log is what the loop, the progress endpoints and the
        notifier read to decide whether a point finished, so the payload
        has exactly one definition. Every field is derived from the
        :class:`Verdict` through :meth:`_verdict_to_result` rather than
        recomputed here, so the event log and the result dict can never
        disagree about what a verdict said.

        The key set is **closed**, not additive: ``vp_id`` (which point),
        ``round`` (which attempt — the log is per-round, but a reader
        holding events from several rounds cannot otherwise tell which
        round a stale completion belongs to), the status and the three
        classification fields the consumers cannot recompute, and
        ``actual_result``. The previous payload carried the joined
        evidence string here; it is not part of the contract and
        nothing read it off this event — the per-VP evidence lives on
        the result dict and in the ``verdict`` records.
        """
        result = Orchestrator._verdict_to_result(vp_id, verdict)
        return {
            "vp_id": vp_id,
            "round": round_number,
            "status": result["status"],
            "failure_class": result["failure_class"],
            "failure_class_source": result["failure_class_source"],
            "evidence_quote": result["evidence_quote"],
            # Bounded because this lands in a JSONL event log that is
            # read whole on every progress request; an unbounded
            # multi-reason join grows that read without bound.
            "actual_result": str(result["actual_result"])[:500],
        }

    async def _split_vp_on_timeout(self, vp: dict, partial_result: dict) -> Optional[dict]:
        """Decompose a timed-out VP into clause-level sub-VPs.

        Returns a synthesised "parent" result summarising the children
        when the splitter accepts, else ``None`` so the caller can
        fall back to the original timeout result. Side effects:

        * Emits a ``vp_subtask_split`` event with the child ids so the
          repair-task generator and downstream observers see that
          decomposition happened.
        * Runs each child through the async single-VP pipeline in
          parallel via :meth:`_run_split_children`.
        * Sets the *parent*'s status to ``"SPLIT"`` in the returned
          summary so consumers can distinguish "the original VP timed
          out and was chunked" from "the original VP timed out and
          nothing more was done".

        The boundary conditions pinned by the TDD spec are:

        * Single-clause ``expected_result`` → splitter returns
          ``None``, this method returns ``None``, the original
          timeout result is preserved.
        * Status other than ``timeout`` → splitter returns ``None``,
          this method returns ``None``.
        * All children time out → we still emit one
          ``vp_subtask_split`` event; the children are recorded with
          ``status="timeout"`` so the partial-progress trail is not
          lost.
        """
        children = SplitDecision.should_split(vp, partial_result)
        if not children:
            return None

        # Annotate every child with the trace-back metadata the
        # downstream pipeline (and the result consumers) rely on:
        # ``parent_vp_id`` and ``original_vp_id`` are redundant on
        # purpose — both names are part of the contract surfaced in
        # the TDD spec, and one of them is the historical field the
        # existing code path emitted, the other the new alias.
        # ``split_clause_index`` is the 1-based position of the
        # clause within the original ``expected_result``.
        for index, child in enumerate(children, start=1):
            child.setdefault("parent_vp_id", str(vp.get("id", "")))
            child.setdefault("original_vp_id", str(vp.get("id", "")))
            child.setdefault("split_clause_index", index)

        child_ids = [c["id"] for c in children]
        self.persistence.write_verification_point_log(
            str(vp.get("id", "")),
            "vp_subtask_split",
            {
                "vp_id": str(vp.get("id", "")),
                "parent_id": str(vp.get("id", "")),
                "N": len(children),
                "child_vp_ids": child_ids,
            },
        )

        child_results = await self._run_split_children(vp, children)

        return {
            "id": str(vp.get("id", "")),
            "status": "SPLIT",
            "actual_result": (
                f"parent timed out; decomposed into {len(children)} sub-VPs: "
                f"{', '.join(child_ids)}"
            ),
            "reasons": [
                f"parent timed out; decomposed into {len(children)} sub-VPs: "
                f"{', '.join(child_ids)}"
            ],
            "evidence": "asyncio.TimeoutError",
            "parent_vp_id": str(vp.get("id", "")),
            "original_vp_id": str(vp.get("id", "")),
            "child_results": child_results,
        }

    async def _run_split_children(
        self, parent_vp: dict, children: List[dict]
    ) -> List[dict]:
        """Execute a list of sub-VPs in parallel and tag each result.

        The children are dispatched through the same per-method
        timeout policy that the parent used, with the per-VP
        ``timeout_seconds`` from the splitter (which mirrors the
        parent's timeout via :class:`SplitDecision`). Running them
        via :func:`asyncio.gather` keeps the total wall-clock cost at
        roughly one child's duration rather than N, which is the
        whole point of splitting.

        Each returned result is augmented with
        ``parent_vp_id``/``original_vp_id``/``split_clause_index``
        pulled from the child definition so consumers can roll
        results back up to the original VP without re-parsing the
        plan.
        """
        if not children:
            return []

        async def _run_one(child: dict) -> dict:
            # ``_run_single_vp_async`` honours the per-VP
            # ``timeout_seconds`` set by SplitDecision and threads
            # the original ``verification_method`` through, so each
            # child runs under the same per-method cap as a fresh
            # top-level VP would.
            result = await self._run_single_vp_async(child)
            # Propagate the trace-back metadata onto the result so
            # downstream consumers can correlate child → parent
            # without re-reading the plan.
            result["parent_vp_id"] = child.get("parent_vp_id", parent_vp.get("id", ""))
            result["original_vp_id"] = child.get("original_vp_id", parent_vp.get("id", ""))
            result["split_clause_index"] = child.get("split_clause_index")
            return result

        return await asyncio.gather(*[_run_one(child) for child in children])

    @staticmethod
    def _aggregate_split_results(
        parent_vp: Dict[str, Any],
        child_results: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Roll sub-VP results back up into a single parent verdict.

        Triggered after :meth:`_split_vp_on_timeout` decomposes a
        timed-out parent VP into clause-level sub-VPs. The parent's
        ``status`` was ``"SPLIT"`` while children were running; once
        the children are done we need a single terminal status to
        surface in the report (the LLM judgment step doesn't know how
        to roll a SPLIT up to PASSED/FAILED on its own).

        Terminal-state matrix:

        * All children ``PASSED`` → parent ``PASSED`` (the split
          was a false alarm; the underlying expectation is met).
        * Any child ``FAILED`` → parent ``FAILED``. The
          ``requirement_deviations`` list is populated **only with
          the failed clauses** so already-passed clauses don't
          pollute the deviation list.
        * All children ``SKIPPED`` → parent stays ``SPLIT``
          (inconclusive; the original timeout wasn't resolved and
          no deviations are recorded). This is the new fourth
          terminal state, distinct from the
          single-VP-internal-partial-failure ``PARTIAL`` case.
        * Mixed (some ``PASSED`` + some ``SKIPPED``, no
          ``FAILED``) → parent ``SPLIT`` with full
          ``subtask_results``/``child_count`` so the report reader
          can see what did and didn't run.

        Boundary semantics:

        * Empty ``child_results`` → :class:`ValueError`. A SPLIT
          with zero children is an illegal state — the splitter
          always produces at least 2 — and silently returning
          ``"SPLIT"`` here would mask a logic bug in the
          splitter.
        * Children with status other than ``PASSED``/``FAILED``/
          ``SKIPPED`` (e.g. ``"timeout"``, ``"PARTIAL"``) are
          treated as not-yet-terminal; they are *not* aggregated
          into PASSED/FAILED/SPLIT, and their presence forces the
          parent to ``SPLIT`` so downstream retry paths know the
          split didn't fully resolve.
        """
        if not child_results:
            raise ValueError(
                "child_results is empty — cannot aggregate split results "
                "(a SPLIT VP must have at least one child result)"
            )

        parent_id = parent_vp.get("id", "unknown") if isinstance(parent_vp, dict) else "unknown"

        statuses = [c.get("status", "UNKNOWN") for c in child_results]
        non_terminal = {"timeout", "PARTIAL", "UNKNOWN"}
        has_non_terminal = any(s in non_terminal for s in statuses)

        failed_children = [c for c in child_results if c.get("status") == "FAILED"]
        passed_children = [c for c in child_results if c.get("status") == "PASSED"]
        skipped_children = [c for c in child_results if c.get("status") == "SKIPPED"]

        # --- All PASSED → parent PASSED ---
        if passed_children and not failed_children and not skipped_children and not has_non_terminal:
            return {
                "id": parent_id,
                "status": "PASSED",
                "actual_result": "All split children PASSED",
                "reasons": ["All split children PASSED"],
                "subtask_results": list(child_results),
                "child_count": len(child_results),
            }

        # --- Any FAILED → parent FAILED (deviations only from failed clauses) ---
        if failed_children:
            deviations: List[Dict[str, Any]] = []
            for child in failed_children:
                deviations.append(
                    {
                        "verification_point_id": child.get("id", ""),
                        "clause_index": child.get("split_clause_index"),
                        "type": "missing",
                        "description": child.get("actual_result", ""),
                        "severity": "high",
                        "evidence": child.get("evidence", ""),
                    }
                )
            return {
                "id": parent_id,
                "status": "FAILED",
                "actual_result": f"{len(failed_children)} split child(ren) FAILED",
                "reasons": [f"{len(failed_children)} split child(ren) FAILED"],
                "subtask_results": list(child_results),
                "child_count": len(child_results),
                "requirement_deviations": deviations,
            }

        # --- All SKIPPED → parent SPLIT (no deviations, no subtask_results) ---
        if skipped_children and not passed_children and not has_non_terminal:
            return {
                "id": parent_id,
                "status": "SPLIT",
                "actual_result": "All split children SKIPPED",
                "reasons": ["All split children SKIPPED"],
            }

        # --- Mixed (PASSED+SKIPPED, or any non-terminal status) → SPLIT ---
        return {
            "id": parent_id,
            "status": "SPLIT",
            "actual_result": "Mixed split children statuses",
            "reasons": ["Mixed split children statuses"],
            "subtask_results": list(child_results),
            "child_count": len(child_results),
        }

    def _enter_repairing_state(self, vp_id: str) -> None:
        """Transition the plan to ``verification_repairing`` after a
        hard-timeout-triggered auto-split so the operator / Feishu
        card sees a meaningful state instead of "stalled".

        Single-writer rule: this is the ONLY call site that routes
        hard-timeout-driven auto-split into the plan state machine.
        All other plan-state writes go through ``transition_to``.
        The transition is a no-op if the plan is not in
        ``verification_running`` (e.g. already past the round) —
        the auto-split children still run, the operator just won't
        see a phase change.

        Best-effort: never crashes the split over a transition failure.
        The verification results themselves are already persisted
        via :meth:`_split_vp_on_timeout` / ``_run_split_children``.
        """
        plan_state = getattr(self, "plan_state", None)
        if plan_state is None:
            return
        try:
            if plan_state.current_phase == "verification_running":
                plan_state.transition_to("verification_repairing")
                self.persistence.write_verification_point_log(
                    vp_id, "vp_hard_timeout_repair_started",
                    {"vp_id": vp_id},
                )
                if self.logger:
                    self.logger.info(
                        "vp_hard_timeout_repair_started",
                        f"plan transitioned to verification_repairing "
                        f"after hard-timeout split vp_id={vp_id}",
                        task_id=vp_id,
                    )
        except Exception as exc:
            if self.logger:
                self.logger.warning(
                    "vp_hard_timeout_repair_transition_failed",
                    f"could not transition to verification_repairing "
                    f"after vp={vp_id} hard timeout: {exc}",
                    task_id=vp_id,
                )

    async def _llm_split_vp_on_hard_timeout(
        self, vp: dict, hard_timeout_exc: HardTimeoutError,
    ) -> Optional[dict]:
        """Single-clause VP that hit hard timeout → ask LLM how to split.

        Mirrors the contract of :meth:`_split_vp_on_timeout`: returns a
        dict with ``status="SPLIT"`` and ``child_results``, or ``None``
        if the LLM also declines. The LLM is given the VP's
        ``test_command`` + ``expected_result`` and asked to produce N
        smaller test commands (e.g. ``pytest -k`` split, file-glob split,
        ``--timeout``-respecting sub-suites).

        On LLM failure or non-parseable JSON response, returns ``None``
        so the caller falls through to the legacy ``status="timeout"``
        result. The LLM call itself routes through the agent's
        ``coding_tool`` so it inherits the same provider / API key /
        logging as the original VP run.
        """
        from verification_split_llm import LLMVPSplitDecision
        try:
            children = await LLMVPSplitDecision.should_split(
                self, vp,
                {
                    "status": "hard_timeout",
                    "reasons": [str(hard_timeout_exc)],
                    "elapsed": getattr(hard_timeout_exc, "elapsed", None),
                    "total_sec": getattr(hard_timeout_exc, "total_sec", None),
                },
            )
        except HardTimeoutError:
            # LLM split itself hit a hard cap. Bail out — don't recurse
            # into another 1-hour cap.
            if self.logger:
                self.logger.warning(
                    "vp_hard_timeout_llm_split_bail",
                    f"LLMVPSplitDecision itself hit HardTimeoutError for "
                    f"vp_id={vp.get('id', '?')} — falling through to "
                    f"plain timeout verdict",
                    task_id=vp.get("id", "?"),
                )
            return None
        except Exception as exc:
            if self.logger:
                self.logger.warning(
                    "vp_hard_timeout_llm_split_failed",
                    f"LLMVPSplitDecision raised for "
                    f"vp_id={vp.get('id', '?')}: {exc}",
                    task_id=vp.get("id", "?"),
                )
            return None

        if not children:
            return None
        self.persistence.write_verification_point_log(
            str(vp.get("id", "")), "vp_subtask_llm_split",
            {
                "vp_id": str(vp.get("id", "")),
                "N": len(children),
                "child_vp_ids": [c.get("id") for c in children],
            },
        )
        child_results = await self._run_split_children(vp, children)
        return {
            "id": str(vp.get("id", "")),
            "status": "SPLIT",
            "method": str(vp.get("verification_method", "")),
            "child_results": child_results,
            "split_kind": "llm_hard_timeout",
        }

    async def _execute_verification_point_inner(
        self, vp: dict, method: str, vp_id: str
    ) -> dict:  # noqa: D401 — kept for backward-compat introspection
        """Backwards-compat shim; the canonical path is :meth:`_run_single_vp_async`.

        Per the PRD decision point on parent/sub-agent boundary, the
        per-method executors (``_execute_code_review``,
        ``_execute_ui_validation``, ``_execute_api_test``) have been
        removed and per-VP work is delegated to
        :class:`VerificationSubAgent`. This shim is retained so legacy
        call-sites (and any introspection) that import the attribute
        do not blow up with :class:`AttributeError`. It is not part of
        the runtime path.
        """
        return {
            "id": vp_id,
            "status": "FAILED",
            "actual_result": (
                "per-method executors retired; use VerificationSubAgent.run()"
            ),
            "reasons": [
                "per-method executors retired; use VerificationSubAgent.run()"
            ],
            "evidence": (
                "verification_agent._execute_verification_point_inner is a "
                "backward-compat shim; the canonical path is "
                "_run_single_vp_async → _delegate_to_sub_agent"
            ),
        }

    def _detect_venv_activate(self) -> Optional[str]:  # noqa: D401 — kept for backward compat
        """Backward-compat shim. Per-VP execution is delegated to
        :class:`VerificationSubAgent` which builds its own shell
        environment. This shim is retained only so legacy call-sites
        that introspect the attribute don't blow up with
        :class:`AttributeError`; it always returns ``None``.
        """
        return None

    def _wrap_command_with_env(self, test_command: str) -> str:  # noqa: D401 — kept for backward compat
        """Backward-compat shim. Per-VP execution is delegated to
        :class:`VerificationSubAgent` which builds its own shell
        environment. This shim is a no-op that returns
        ``test_command`` unchanged.
        """
        return test_command

    # -----------------------------------------------------------------------
    # Phase 3: Judgment
    # -----------------------------------------------------------------------

    def generate_verification_report(self, execution_results: Optional[dict] = None,
                                      retry_llm: int = 3) -> dict:
        """
        Phase 3: Generate verification report with pass/fail judgment.

        Args:
            execution_results: Optional execution results (loads from file if not provided)
            retry_llm: Number of retries for LLM calls

        Returns:
            Verification report dict
        """
        print("[Verification] Phase 3: Generating verification report...")

        # Load execution results if not provided
        if execution_results is None:
            # Preferred path: read from SQLite
            # (``plan_verification.execution_results``). Falls back to
            # the legacy on-disk cache only when SQLite is unavailable
            # (e.g. fresh install before migrate() has run).
            execution_results = self._load_execution_results()
            if execution_results is None:
                raise FileNotFoundError(
                    "Execution results not found. Run execute_verification_plan first."
                )

        # Aggregate any SPLIT VPs into terminal statuses (PASSED / FAILED /
        # SPLIT) so the LLM judgment step and downstream consumers see a
        # single, settled status per VP instead of an unfolded
        # ``status="SPLIT" + child_results=[...]`` envelope.
        execution_results = self._aggregate_split_vps(execution_results)

        # Generate report using LLM
        prompt = self._build_judgment_prompt(execution_results)

        for attempt in range(retry_llm):
            # 2026-09-14: keep the round log fresh while the judgment LLM
            # call runs (provider retries can take minutes) — otherwise
            # the watchdog's staleness check sees a silent plan dir and
            # kills a healthy round.
            #
            # 2026-09-15: no explicit ``timeout`` — the
            # call inherits the unified 900s silence / 1800s idle / 1h
            # ceiling rules rather than replacing the 900s window with a
            # smaller number.
            self._judgment_heartbeat("report_judgment")
            try:
                report_data = self.coding_tool.query_json(
                    prompt=prompt,
                    system_instruction=VERIFICATION_JUDGMENT_SYSTEM_PROMPT,
                )

                # Validate structure
                required_fields = ["overall_status", "verification_results"]
                for field in required_fields:
                    if field not in report_data:
                        raise ValueError(f"Missing required field: {field}")

                # Trust the executor's PASSED verdicts. The Phase 3 LLM
                # is instructed to act as a "senior QA engineer" and may
                # downgrade PASSED VPs to PARTIAL based on subjective
                # concerns (test_command design choices, grep-marker
                # patterns, coverage interpretation). When the executor
                # has no FAILED verdicts, those LLM-emitted PARTIAL
                # downgrades are noise — restore the executor's verdict
                # so overall_status reflects reality (the spec's
                # zero-tolerance-for-skipping contract still leaves room
                # for actual failures, but speculation about test
                # methodology is not a failure).
                #
                # 2026-09-11: the previous code
                # used ``if not any(s == "FAILED" for s in execution_verdicts)``
                # which suppressed LLM FAILED verdicts whenever the
                # executor passed every VP. That was wrong — a VP can have
                # an executor verdict PASSED (cargo test exit 0) and an
                # LLM verdict FAILED (the diff violates a frozen-behaviour
                # constraint). The auto-loop
                # overwrote FAILED→PASSED, skipped the
                # repair-task-generation path, and the plan never got
                # repair tasks added to state.db.plan_tasks. Tighten
                # the gate: only restore PARTIAL downgrades, never
                # FAILED. An executor PASS + LLM FAILED must surface
                # as FAILED so the orchestrator routes to repair.
                execution_verdicts = {
                    er.get("id"): er.get("status")
                    for er in execution_results.get("execution_results", [])
                }

                # 2026-09-16 两阶段：被门禁延后的 Phase 2 全量关卡，
                # 判定权在执行器，不在 Phase 3 LLM。这些关卡**根本没执行**，
                # LLM 只能对着"没有结果"猜一个 FAILED/SKIPPED —— 猜成 FAILED
                # 会污染失败统计并把修复任务引到错误靶子（去"修"一个没跑过的
                # 全量门禁）。这里强制还原为 DEFERRED，并从需求偏离里摘掉。
                deferred_ids = {
                    vid for vid, st in execution_verdicts.items()
                    if st == "DEFERRED"
                }
                if deferred_ids:
                    for vr in report_data.get("verification_results", []):
                        if vr.get("id") in deferred_ids:
                            vr["status"] = "DEFERRED"
                            if not (vr.get("actual_result") or "").strip():
                                vr["actual_result"] = (
                                    "Phase 2 全量关卡本轮延后："
                                    "Phase 1 未全部通过"
                                )
                    report_data["requirement_deviations"] = [
                        d for d in (report_data.get("requirement_deviations") or [])
                        if d.get("verification_point_id") not in deferred_ids
                    ]
                    # The round's verdict comes from the one derivation
                    # (:func:`_derive_overall_status`, R4).  A deferred
                    # gate was never asked, so the round did not judge
                    # it — and a round that did not judge it cannot say
                    # it passed.  (The verdict is derived from the
                    # entries, never from the judgment LLM's own
                    # summary: the entries are the evidence.)
                    report_data["overall_status"] = _derive_overall_status(
                        report_data.get("verification_results", [])
                    )

                if not any(s == "FAILED" for s in execution_verdicts.values()):
                    for vr in report_data.get("verification_results", []):
                        vid = vr.get("id")
                        if vid and execution_verdicts.get(vid) == "PASSED" and vr.get("status") == "PARTIAL":
                            # LLM-emitted subjective PARTIAL on a VP
                            # the executor marked PASSED → restore.
                            vr["status"] = "PASSED"
                    # Re-derive overall_status from the corrected list,
                    # by the same derivation the accounting layer uses.
                    report_data["overall_status"] = _derive_overall_status(
                        report_data.get("verification_results", [])
                    )

                # 2026-10-25 (repair-r1-32): the accounting of the
                # environment-blocked entries.  It runs **after** every
                # verdict correction above, so it has the last word on
                # the round's verdict: a point the environment stopped
                # is kept in the report and annotated, but it is not a
                # failure and it does not make the round red.  A genuine
                # failure is not touched.
                report_data = self._exclude_environment_blocked_from_failures(
                    report_data, execution_results,
                )

                # Add metadata
                report_data["generated_at"] = datetime.now().isoformat()
                report_data["plan_id"] = self.plan_dir.name
                report_data["project_dir"] = str(self.project_dir)
                # 2026-10-25 (repair-r1-26): the report says out loud
                # when the round was about the environment rather than
                # about the product.  The upper layer reads this to
                # decide between re-running the round and repairing the
                # product — and the answer must not depend on reading
                # twelve identical reasons.
                if self._environment_blocked:
                    report_data["environment_blocked"] = True
                    report_data["environment_blocked_reason"] = (
                        self._environment_blocked_summary()
                    )

                # Compute execution_profile (static budget projection
                # consumed by the bridge UI). The profile mirrors
                # ExecutionProfileGenerator's contract and is derived
                # from the aggregated execution_results so the LLM-
                # emitted report and the profile agree on the same
                # set of verification points.
                report_data["execution_profile"] = ExecutionProfileGenerator(
                    execution_results,
                    vp_capacity=self._report_vp_capacity(),
                ).build()

                # Save report using persistence manager
                self.persistence.update_report(report_data)

                print(f"[Verification] Generated report: {report_data['overall_status']}")
                return report_data

            except ApiError as e:
                print(f"[Verification] LLM API error (attempt {attempt + 1}/{retry_llm}): {e}")
                if attempt == retry_llm - 1:
                    raise
            except Exception as e:
                print(f"[Verification] Error generating report (attempt {attempt + 1}/{retry_llm}): {e}")
                # 2026-10-23 (incident A, second retry chain): a
                # DETERMINISTIC local error — the dispatch's working
                # directory is gone, is not a directory, or cannot be
                # entered — cannot be healed by another attempt. The
                # generic handler used to spend the whole retry budget
                # on it (three dispatches, no backoff) and then produce
                # the same fallback report, so the only thing the extra
                # attempts bought was cost and a "retrying" log line
                # that buried the real cause. Short-circuit to the
                # fallback report instead.
                #
                # Only THIS class of failure is short-circuited.
                # ``ApiError`` keeps its own branch above (provider-side,
                # with its own retry semantics), and everything else
                # — timeouts, provider outages, programming mistakes —
                # still runs the full ``retry_llm`` budget below.
                if is_deterministic_local_error(e):
                    print(
                        "[Verification] Deterministic local error "
                        "(not retrying): "
                        f"{e}"
                    )
                    return self._persist_fallback_report(execution_results)
                if attempt == retry_llm - 1:
                    # Generate minimal report on total failure
                    return self._persist_fallback_report(execution_results)

        return self._persist_fallback_report(execution_results)

    def _build_judgment_prompt(self, execution_results: dict) -> str:
        """Build prompt for verification report generation."""
        verification_points = execution_results.get("verification_points", [])
        execution_results_list = execution_results.get("execution_results", [])

        # Format results for LLM
        parts = ["## 验证计划\n\n"]

        for vp in verification_points:
            parts.append(f"### {vp.get('id')}: {vp.get('title', '')}")
            parts.append(f"- PRD 验收标准: {vp.get('related_prd_criteria', '')}")
            parts.append(f"- 验证方法: {vp.get('verification_method', '')}")
            parts.append(f"- 优先级: {vp.get('priority', '')}")
            parts.append(f"- 预期结果: {vp.get('expected_result', '')}")
            parts.append("")

        parts.append("## 执行结果\n\n")

        for result in execution_results_list:
            vp_id = result.get("id", "unknown")
            status = result.get("status", "UNKNOWN")
            actual = result.get("actual_result", "")
            evidence = result.get("evidence", "")

            parts.append(f"### {vp_id}: {status}")
            parts.append(f"- 实际结果: {actual}")
            parts.append(
                f"- 判定依据: {_render_evidence_for_prompt(evidence)}"
            )
            parts.append("")

        return "\n".join(parts)

    def _judgment_heartbeat(self, stage: str, vp_id: str = "") -> None:
        """Append a liveness line to the round log during Phase 3.

        2026-09-14 (observed live on a production plan): after the last VP
        reports, the round enters the judgment phase — one LLM call for
        the report plus a per-VP supplementary spec/code review — and
        NOTHING in that phase wrote to the plan directory. With a 3600s
        ``VERIFICATION_WATCHDOG_STALENESS_SECONDS`` the watchdog declared
        a healthy, actively-working round ``verification_log_stale`` and
        terminalised it. The watchdog's staleness signal is only as good
        as the log it reads, so the judgment phase now keeps that log
        fresh.

        Best-effort: a missing log file (persistence not started) must
        never break the judgment phase.
        """
        try:
            self.persistence.write_verification_point_log(
                vp_id or "-",
                "judgment_heartbeat",
                {"stage": stage, "vp_id": vp_id},
            )
        except Exception:
            pass

    def _generate_minimal_report(self, execution_results: dict) -> dict:
        """Generate minimal verification report when LLM fails."""
        execution_results_list = execution_results.get("execution_results", [])

        # Count statuses
        status_counts = {"PASSED": 0, "FAILED": 0, "SKIPPED": 0, "PARTIAL": 0}
        for result in execution_results_list:
            status = result.get("status", "SKIPPED")
            if status in status_counts:
                status_counts[status] += 1

        # Determine overall status
        if status_counts["FAILED"] > 0:
            overall_status = "FAILED"
        elif status_counts["PARTIAL"] > 0:
            overall_status = "PARTIAL"
        elif status_counts["PASSED"] > 0:
            overall_status = "PASSED"
        else:
            overall_status = "SKIPPED"

        return {
            "overall_status": overall_status,
            "summary": f"自动生成报告: {status_counts['PASSED']} 通过, {status_counts['FAILED']} 失败, {status_counts['SKIPPED']} 跳过, {status_counts['PARTIAL']} 部分",
            "verification_results": execution_results_list,
            "requirement_deviations": [],
            "execution_profile": ExecutionProfileGenerator(
                execution_results, vp_capacity=self._report_vp_capacity()
            ).build(),
            "generated_at": datetime.now().isoformat(),
            "plan_id": self.plan_dir.name,
            "project_dir": str(self.project_dir)
        }

    def _persist_fallback_report(self, execution_results: dict) -> dict:
        """Build the minimal report AND write it to disk.

        2026-09-14 (Bug B) — ``_generate_minimal_report`` returns its
        dict to the caller only; nothing ever wrote it.  When all
        Phase-3 judgment attempts fail (bogus ``HardTimeoutError``,
        unparseable JSON, provider outage, …) that left
        ``plans/{id}/verification_report.json`` holding the PREVIOUS
        round's snapshot: ``snapshot_round_results`` +
        ``clear_round_results`` run at round start, so the on-disk
        top-level ``verification_results`` is already empty by then.

        Downstream, ``extract_failed_vps_with_paths`` reads that stale
        file to decide which VPs the repair generator must address.  For
        an earlier plan's round 1 actually failed VP-021 /
        VP-034 / VP-036, but the file still described the 2026-09-08
        round (VP-034 only) — two real failures would have been silently
        dropped even if repair generation had succeeded.

        The fallback report carries the authoritative
        ``execution_results`` for the round that just ran, so persisting
        it is what keeps the repair path grounded in reality.  Writing
        is best-effort: a disk error must not turn a degraded report
        into a crashed verification run.
        """
        report = self._generate_minimal_report(execution_results)

        # ``rounds`` is owned by ``snapshot_round_results``; a bare
        # overwrite would drop every earlier round's audit trail.
        try:
            existing = self.persistence.load_report() or {}
            if isinstance(existing.get("rounds"), list):
                report["rounds"] = existing["rounds"]
        except Exception as read_exc:  # noqa: BLE001 — best-effort
            logging.getLogger(__name__).warning(
                "fallback_report_read_error: %s", read_exc,
            )

        try:
            self.persistence.update_report(report)
        except Exception as persist_exc:  # noqa: BLE001 — best-effort
            logging.getLogger(__name__).error(
                "fallback_report_persist_error: verification_report.json "
                "still holds the previous round's results — repair "
                "generation will read stale failed-VP evidence: %s",
                persist_exc,
            )
        else:
            logging.getLogger(__name__).warning(
                "fallback_report_persisted: Phase 3 judgment failed for "
                "all attempts; wrote the deterministic minimal report "
                "(%s) from execution_results so downstream repair "
                "generation sees this round's verdicts",
                report.get("overall_status"),
            )
        return report

    def _aggregate_split_vps(self, execution_results: dict) -> dict:
        """Roll up SPLIT VPs in ``execution_results`` to terminal statuses.

        Iterates over every entry in ``execution_results["execution_results"]``
        and, for any entry whose ``status == "SPLIT"`` and that carries a
        non-empty ``child_results`` list, calls
        :meth:`_aggregate_split_results` and replaces the entry's
        status/payload with the aggregated verdict.

        The dict is mutated in place and also returned, mirroring the
        convention used elsewhere in this module (e.g.
        :meth:`_build_judgment_prompt`).

        VPs that are not SPLIT — or SPLIT VPs without child results
        (e.g. a split was rejected by the splitter and the parent was
        left in SPLIT by a future refactor) — pass through untouched,
        so the call is safe to make unconditionally.
        """
        if not isinstance(execution_results, dict):
            return execution_results

        for result in execution_results.get("execution_results", []) or []:
            if not isinstance(result, dict):
                continue
            if result.get("status") != "SPLIT":
                continue
            child_results = result.get("child_results")
            if not child_results:
                continue
            try:
                aggregated = self._aggregate_split_results(
                    parent_vp={
                        "id": result.get("id", "unknown"),
                        "parent_vp_id": result.get("parent_vp_id"),
                        "status": "SPLIT",
                    },
                    child_results=child_results,
                )
            except ValueError:
                # Empty child list is an illegal state but must not
                # take the whole report down — leave the entry alone
                # and let the LLM surface it.
                continue
            # Replace the SPLIT envelope with the terminal verdict.
            # Keep id/parent_vp_id from the original; everything else
            # comes from the aggregation.
            original_id = result.get("id", aggregated.get("id", "unknown"))
            result.clear()
            result.update(aggregated)
            result["id"] = original_id
        return execution_results

    # -----------------------------------------------------------------------
    # Full Workflow
    # -----------------------------------------------------------------------

    def run_full_verification(self, round_number: int = 1, resume: bool = False) -> dict:
        """3-step skeleton: plan → executor → report.

        The parent :class:`VerificationAgent` is a pure orchestrator
        after this refactor. Phase 1 (plan) and Phase 3 (report) LLM
        calls live on the parent; Phase 2 (per-VP execution) is
        delegated to :class:`VerificationExecutor` so the parent
        itself never invokes ``coding_tool.query`` for per-VP work.

        Pipeline:

          1. Open a new verification round (creates the JSONL log).
          2. Phase 1: ask the LLM for a verification plan; the
             existing Phase 1 ``manual_check`` filter is invoked
             inside :meth:`generate_verification_plan` so the plan
             reaching Phase 2 is already clean of
             ``manual_check`` VPs.
          3. Build a :class:`VerificationExecutor` whose
             ``sub_agent_runner`` routes per-VP work to
             :meth:`_run_single_vp_async` (which already delegates
             to :class:`VerificationSubAgent`). Drive the executor
             to completion via :meth:`VerificationExecutor.run` —
             the executor owns the layer-by-layer traversal and
             the L1→L2 short-circuit.
          4. Project the executor's collected verdicts back to the
             legacy ``execution_results`` envelope so Phase 3 sees
             the same shape it has always seen. On any exception
             inside the executor, build a minimal FAILED envelope
             so the orchestrator still receives a report with
             ``overall_status``.
          5. Phase 3: produce the verification report.

        Args:
            round_number: 1-indexed verification round number.
            resume: When ``True``, skip the ``_clear_verification_state_files``
                call so the on-disk verdict cache (loaded into
                ``_completed_items``) lets :meth:`BaseExecutor.run`
                skip already-PASSED VPs via its
                ``completed_set = self._completed_items | ...`` filter
                (line 344). The default ``False`` keeps the historic
                "always start from a clean slate" behaviour: clearing
                the cache ensures that a VP whose ``test_command``
                changed since the last run gets re-executed rather
                than silently suppressed by a stale verdict. The
                resume path is opt-in because the safety guarantee
                is more important than the LLM-token savings in
                most cases.

        Returns:
            The final :func:`generate_verification_report` dict with
            ``overall_status`` / ``verification_results`` /
            ``requirement_deviations`` / ``execution_profile`` etc.
        """
        print(
            f"[Verification] Starting 3-step workflow (Round {round_number}, "
            f"resume={resume})..."
        )

        # The "clean slate" guard at the start of run_full_verification
        # is what makes a stale cache (verdicts from a previous run
        # with a different verification_plan.json) safe. Skipping it
        # on resume is the entire point of the resume=True path —
        # without that, the executor would re-run every VP from
        # scratch even when the verdict cache has stable results.
        if not resume:
            self._clear_verification_state_files()

        self.start_verification_round(round_number)

        # 2026-10-14：每一轮开头重置两个「本轮」状态。
        #
        # ``_freshness_cache`` 的 docstring 一直声称自己由 orchestrator
        # 在轮次之间清空，但全仓没有任何一处调用 ``clear()`` —— 它从
        # ``__init__`` 活到 orchestrator 析构，上一轮的 stale 判决会被带进
        # 下一轮。现在这里真的清，文档与代码才对得上。
        #
        # ``_rebuild_coordinator`` 是本轮「同一二进制只重建一次」的依据。
        #
        # 顺序是承重的：这两行必须在下面的 round-start preflight **之前**。
        # preflight 会通过 ``_maybe_rebuild_before_service_start`` 登记本轮的
        # 那次重建；如果先清再 preflight 就对了，但如果把 clear 放到
        # preflight 之后，preflight 的重建会被当成上一轮的残留抹掉，本轮的
        # VP 就会拿到第二次重建。
        self._freshness_cache.clear()
        self._rebuild_coordinator.clear()

        plan_data = self.generate_verification_plan()

        # 2026-09-16 service-freshness preflight ("VP0"): before the
        # executor dispatches the FIRST VP, make sure every service
        # port referenced by the plan is live and running current
        # code. Without this, api_test / ui_validation VPs race a
        # dead or stale server process and fail with
        # connection-refused noise (2026earlier production plan: VP-001/002
        # failed on port 8080 while the disk artefact was fresh and a
        # later VP that restarted the server itself PASSED). Runs
        # once per round — repair rounds re-run it against the new
        # code. Never blocks the round: failures degrade to
        # BLOCKED verdicts on the affected VPs.
        self._preflight_service_freshness(plan_data)

        # 2026-10-25 (repair-r1-33): the services are up — now ask
        # whether they may be **used** at all.  The round's service
        # preflight is asked here, before the executor dispatches
        # anything, so a round whose services are not there spends no
        # verification point on a connection refusal.  A round that is
        # refused produces **no** verdict about the product: it is
        # BLOCKED, and it says so.
        preflight_failure = self._run_round_service_preflight()
        if preflight_failure is not None:
            report = self._build_preflight_blocked_report(preflight_failure)
            try:
                self.persistence.update_report(report)
            except Exception as exc:  # noqa: BLE001 - a report is a nicety
                logger.warning(
                    "[verification_preflight] could not write the blocked "
                    "report: %s", exc,
                )
            print(
                f"[Verification] Round blocked before dispatch: "
                f"{report['summary']}"
            )
            return report

        # 2026-09-18 C2：服务已经由 preflight 起好并登记，这里把 VP 命令里的
        # ``{{svc.<name>.<field>}}`` 展开成真实地址再交给执行器。替换只在
        # 内存里做——磁盘上的计划保留占位符，每轮重新解析，避免换端口后
        # 留下一份写死端口的旧计划。
        plan_data = self._resolve_service_placeholders(plan_data)

        # 2026-10-23（工作树指纹基线）：本轮的树基线在这里取，而且必须取在
        # preflight **之后** —— preflight 可能重建二进制（那会改动工作树），
        # 把重建本身算成"并发变异"就等于一开始就误判。
        #
        # 挂起表在这里清空：它装的是"这一轮里因树动过而没跑成的核对"，
        # 跨轮留着就是上一轮的旧账。
        self._round_tree_fingerprint = self._current_tree_fingerprint()
        self._deferred_evidence_checks = []

        # Step 2: delegate Phase 2 to VerificationExecutor. The
        # parent only sequences the call — per-VP work, layer
        # ordering, and short-circuit all live on the executor.
        try:
            executor = self._build_verification_executor(plan_data)
            # No width, and no gate. How many verification points run at
            # once is decided per dispatch by the provider slot pool —
            # ``acquire_provider_with_dynamic_capacity`` hands out a slot
            # or does not, from the fleet as it stands that second, and it
            # does so *before* the ``claude`` subprocess is spawned
            # (``coding_tool._run_claude_interactive``), so the pool bounds
            # the LLM calls and the subprocesses together.
            #
            # ``max_parallel=0`` is explicit and is NOT the same as
            # omitting it. ``BaseExecutor.run`` defaults to ``1``, and
            # ``1`` is on the bounded side of ``if max_parallel > 0``:
            # a round that dropped the argument would run one
            # verification point at a time and look perfectly healthy.
            # "0" is the docstring's unbounded sentinel.
            try:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(executor.run(max_parallel=0))
                else:
                    # Loop is running (e.g. pytest-asyncio test).
                    import nest_asyncio  # type: ignore
                    nest_asyncio.apply()
                    asyncio.run(executor.run(max_parallel=0))
            finally:
                # 2026-10-23（轮次结束重跑挂起的核对）：挂起的核对是"这一轮
                # 欠下的账"——轮次崩了也不能赖掉，所以这一步放在 finally 里。
                #
                # 位置承重：必须在 ``executor.run`` **之后**（树该还原了）、
                # 在 ``_build_execution_results_from_executor`` **之前**
                # （报告要看到改判后的裁决）。
                #
                # 这里再兜一层：finally 里的异常会**替换**正在飞的那一个，
                # 所以 flush 自己抛异常的话，轮次真正的失败原因就被它盖住了。
                # flush 内部已经逐条吞掉异常，这里防的是它自己崩掉。
                try:
                    self._flush_deferred_evidence_checks(executor)
                except Exception:
                    logger.exception(
                        "[verification_evidence] round-end flush failed",
                    )
            # What the round actually reached, measured inside the
            # runner. This is the observable that replaces the width:
            # nothing here is a declared number, so it cannot disagree
            # with the run it describes.
            self._note_vp_capacity(executor.peak_in_flight)
            execution_results = self._build_execution_results_from_executor(
                plan_data, executor
            )
        except Exception as exc:
            execution_results = self._build_failure_execution_results(
                plan_data, exc
            )

        # 2026-10-26: the round checks its **own** evidence before it
        # writes anything down.  A connection-class failure whose
        # address was served in the same round is not a verdict about
        # the product: it is marked ``invalid_env`` and given one more
        # look — the service is re-ensured, probed, and the point is run
        # again by the round's own dispatcher.  What the second look
        # says is what lands.
        #
        # This runs **before** the environment-blocked re-run below, and
        # that order is load-bearing: a contradicted point is taken out
        # of the environment-blocked set by being marked ``invalid_env``,
        # so it gets exactly one second look — this one — instead of two.
        execution_results = self._reconcile_round_evidence_conflicts(
            plan_data, execution_results,
        )

        # 2026-10-25 (repair-r1-32): the round's one automatic re-run.
        # The points the environment blocked get one more look — the
        # service is brought back and probed, and only if it is healthy
        # are they run again.  At most once per round.
        execution_results = self._rerun_environment_blocked_vps(
            plan_data, execution_results,
        )

        # Step 3: Phase 3 LLM.
        report = self.generate_verification_report(execution_results)

        print(
            f"[Verification] Workflow completed: {report['overall_status']}"
        )
        return report

    def _plan_vp_definitions(self) -> Optional[Dict[str, Dict[str, Any]]]:
        """The verification points as ``verification_plan.json`` defines them.

        The plan file is the **authority** on what a verification point is:
        the id has to be one the plan declares, and the plan's own entry is
        what says whether the criterion is complete.  The in-memory plan
        cannot answer that — the plan-to-executor conversion defaults a
        missing method to ``code_review``, so a definition-less entry
        arrives at the dispatcher wearing a method it never declared.

        ``None`` means "there is no plan on disk" (missing or unreadable).
        The dispatch guard stands down in that case: with nothing to
        compare against, every verification point would look undefined,
        and blocking the whole round would be worse than the defect it
        guards against.
        """
        try:
            raw = json.loads(self.verification_plan_file.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "[Verification] %s is unreadable; the VP definition guard "
                "stands down for this round",
                self.verification_plan_file,
            )
            return None
        if not isinstance(raw, dict):
            return None
        points = raw.get("verification_points")
        if not isinstance(points, list):
            return None
        return {
            str(vp.get("id")): vp
            for vp in points
            if isinstance(vp, dict)
        }

    def _build_vp_definition_blocked_verdict(
        self, vp_id: str, gaps: List[str],
    ) -> Dict[str, Any]:
        """The verdict for a verification point that has no criterion to judge.

        BLOCKED, not FAILED: nothing ran, so there is no evidence to grade
        and no deliverable to blame.  The class is ``criterion_defect`` —
        the criterion is what is missing — and it is decided **by rule**
        (the signal names it in ``verification.failure_triage``), so it
        counts against the same-cause threshold and escalates to a human
        instead of becoming a repair task.  That is the contract of
        this plan's acceptance item (f).

        The verdict also says what to do about it: the operator voids or
        re-expresses the verification point.  The executor never invents a
        criterion on its own — an assertion nobody wrote down cannot be
        verified by anybody.
        """
        from verification.failure_triage import (
            CRITERION_DEFECT,
            SOURCE_DETERMINISTIC,
            classify_deterministic,
        )

        detail = "; ".join(gaps)
        failure_class = (
            classify_deterministic(VP_DEFINITION_MISSING_SIGNAL, detail)
            or CRITERION_DEFECT
        )
        return {
            "status": "BLOCKED",
            "reasons": [
                f"verification point {vp_id} has no usable definition: {detail}"
            ],
            "actual_result": (
                f"验证点 {vp_id} 没有可判定的判据（{detail}）。"
                "执行器不会替它发明判据：请由计划属主作废或重新表达这条"
                "验证点（判据写错或缺失属于 criterion_defect，不生成修复"
                "任务）。"
            ),
            "evidence": {
                "signal": VP_DEFINITION_MISSING_SIGNAL,
                "vp_id": vp_id,
                "gaps": list(gaps),
            },
            "failure_class": failure_class,
            "failure_class_source": SOURCE_DETERMINISTIC,
            "framework_checks": [],
        }

    def _build_verification_executor(self, plan_data: dict) -> "VerificationExecutor":
        """Build a :class:`VerificationExecutor` wired to this agent.

        The plan dict is converted from the LLM schema
        (``verification_points`` / ``id`` / ``verification_method``)
        to the executor schema (``vps`` / ``id`` / ``method``) so the
        executor can drive the VPs layer-by-layer.

        The ``sub_agent_runner`` bridges the executor's call shape
        (``runner(vp_node)`` with executor-schema keys) to the
        canonical :meth:`_run_single_vp_async` (which expects
        LLM-schema keys like ``verification_method``). The
        translation is local to the runner closure — the parent
        agent's other entry points are not affected.
        """
        executor_plan = self._convert_plan_to_executor_schema(plan_data)
        # 2026-10-24: the executor's VPs are where a service's new
        # address is written when a per-VP recovery had to move it
        # (see ``_refresh_resolved_service_placeholders``).  These are
        # the very dicts the executor dispatches, so writing into them
        # is what the next VP reads.
        self._executor_vps_by_id = {
            str(vp.get("id")): vp for vp in executor_plan.get("vps", [])
        }

        #: The plan file's own definitions — the authority on what a
        #: verification point is.  Read once per round: Phase 1 has already
        #: written the file by the time the executor is built, and the
        #: round's plan does not change under it.
        plan_definitions = self._plan_vp_definitions()

        async def _dispatch_vp(vp_node: dict) -> dict:
            # 2026-10-25 (repair-r1-32): the round keeps its own
            # dispatcher, so the one automatic re-run of the points the
            # environment blocked is graded by exactly the rules the
            # first attempt used — the same gates, the same evidence.
            vp = dict(vp_node)
            vp_id = str(vp.get("id", "unknown"))
            # 2026-10-24（VP-031）：**分派前**先问"这条验证点有判据吗"。
            #
            # 有 id、没有定义的条目不是验证点，是占位。派给它等于让框架
            # 发明判据：计划转执行器 schema 时会给缺 method 的条目套上缺省
            # 方法 ``code_review``，于是这条空壳被派给了 coding tool ——
            # 整轮随后在分派飞行中被取消，那一次尝试被记成 FAILED：一条
            # 从没写下来过的判据，被算成了"交付物坏了"。
            #
            # 门禁在最前面：它必须先于任何一次 coding tool 调用（连
            # 二进制重建那条路上的子 agent 也不能碰到）。
            _gaps = vp_dispatch_gaps(vp, plan_definitions)
            if _gaps:
                self._safe_log_freshness_event(
                    vp_id, VP_DEFINITION_MISSING_SIGNAL, {"gaps": _gaps},
                )
                return self._build_vp_definition_blocked_verdict(vp_id, _gaps)

            # 2026-10-25 (repair-r1-31): the address this VP is about
            # to use is recorded **before** anything can block it — a
            # VP that never got a request out still has to say which
            # address it would have used, and which instance the round
            # had bound for it.
            self._record_vp_address(vp_id, vp)
            # Binary freshness pre-check (runs before every VP).
            # Audit-logged via ``write_verification_point_log`` so the
            # operator can audit stale-binary detection per VP from
            # ``plans/<id>/logs/verification_*_*.log``. The check is
            # wrapped in try/except so an unexpected error in the
            # freshness path never blocks VP execution; the operator
            # sees the warning in stderr instead.
            #
            # 2026-10-14（事件循环不再被重建冻死）：the two blocking calls
            # here — ``check_binary_freshness`` (stat/git over the whole
            # project) and ``attempt_intelligent_rebuild`` (fast-path
            # ``cargo build`` + an LLM rebuild sub-agent that can run for
            # minutes) — are dispatched through ``asyncio.to_thread``.
            # This closure runs on the round's single event loop
            # (``run_full_verification`` → ``asyncio.run(executor.run())``
            # → ``BaseExecutor._execute_one_item``), so a synchronous call
            # here freezes EVERY sibling VP: one VP waiting on a rebuild
            # stalls the whole round for as long as the build takes, and
            # the round then emits no log line at all until the watchdog
            # fails it. The audit logging stays on the loop so the event
            # ordering is unchanged.
            try:
                freshness = await asyncio.to_thread(
                    check_binary_freshness,
                    self.project_dir, cache=self._freshness_cache,
                )
                if freshness.is_stale:
                    logger.warning(
                        "vp=%s binary stale: %s; attempting rebuild",
                        vp_id, freshness.detail,
                    )
                    self._safe_log_freshness_event(
                        vp_id, "binary_stale_detected",
                        {
                            "kind": freshness.kind,
                            "detail": freshness.detail,
                            "evidence": freshness.evidence,
                        },
                    )
                    # 2026-09-07 BinaryRebuildAgent plan: replace the bare
                    # hardcoded ``rebuild_binary`` call with the intelligent
                    # orchestrator. It tries the cheap deterministic fast
                    # path first, then (on failure) spawns a tool-restricted
                    # LLM sub-agent that discovers the project's build
                    # system / venv / toolchain and rebuilds generically.
                    # The orchestrator independently re-checks freshness, so
                    # ``rebuild_result.success`` is trustworthy.
                    #
                    # 2026-10-14 (single-flight): this call goes through
                    # ``_BinaryRebuildCoordinator``, so the FIRST VP to
                    # reach this key builds and every other VP that
                    # reaches it in the same round awaits that one attempt
                    # and receives the SAME RebuildResult. Without it, N
                    # VPs judging one stale binary start N rebuilds
                    # against the same target/debug and cargo's artifact
                    # file lock serialises them into a queue that starves
                    # the round: N builds, a long silence, and a watchdog
                    # CAS to failed.
                    #
                    # Only the REBUILD is single-flighted. The freshness
                    # check above stays per-VP — it is cheap, and it is
                    # each VP's own evidence for deciding BLOCKED. The
                    # coordinator still runs its work in a thread.
                    from binary_rebuild_agent import attempt_intelligent_rebuild
                    from sub_agent_registry import sub_agent_registry

                    def _rebuild_this_vp() -> Any:
                        return attempt_intelligent_rebuild(
                            self.project_dir,
                            freshness,
                            coding_tool=self.coding_tool,
                            plan_id=self.plan_dir.name,
                            vp_id=vp_id,
                            registry=sub_agent_registry,
                        )

                    rebuild_result = await self._rebuild_coordinator.run_once(
                        self._rebuild_coordinator.key_for(
                            self.project_dir, freshness.kind,
                        ),
                        _rebuild_this_vp,
                    )
                    if rebuild_result.success:
                        freshness = await asyncio.to_thread(
                            check_binary_freshness,
                            self.project_dir, cache=self._freshness_cache,
                        )
                    if freshness.is_stale:
                        self._safe_log_freshness_event(
                            vp_id,
                            "binary_stale_blocked",
                            {
                                "command": freshness.rebuild_command,
                                "final_evidence": freshness.evidence,
                                "rebuild_agent_used": rebuild_result.agent_used,
                                "rebuild_diagnostics": rebuild_result.diagnostics,
                                "rebuild_commands_run": rebuild_result.commands_run,
                            },
                        )
                        return self._build_binary_freshness_blocked_verdict(
                            vp_id, freshness, rebuild_result,
                        )
                self._safe_log_freshness_event(
                    vp_id,
                    "binary_freshness_pass",
                    {"kind": freshness.kind, "status": freshness.status},
                )
            except Exception as exc:
                logger.warning(
                    "vp=%s binary freshness check errored: %s; proceeding",
                    vp_id, exc,
                )
            # Service-freshness gate (2026-09-16; **recoverable** since
            # 2026-10-24).  The round-start preflight could not bring a
            # service port back to life → the gate tries ONCE more
            # before it blocks, and blocks with a verdict that says why
            # (the machine-readable ``signal`` the four-class triage
            # reads, plus the start attempt's diagnosis).
            #
            # The gate is asked through ``asyncio.to_thread`` for the
            # same reason the binary check above is: the recovery may
            # start a service, and that takes seconds the event loop
            # must not spend.
            gate_verdict = await asyncio.to_thread(
                self._service_gate_verdict, vp,
            )
            if gate_verdict is not None:
                return gate_verdict

            # Service liveness (2026-10-24): the round-start preflight
            # starts the declared services **once**.  A service that
            # dies after that stays dead for the rest of the round,
            # and every VP that talks to it is graded on the
            # connection refusal — ``api_test`` calls that FAILED,
            # which reads as "the project is broken" when the truth is
            # "the plumbing died".  Probe the ports this VP is about
            # to use and bring a dead one back before the VP runs; if
            # it cannot be brought back, its port joins
            # ``_blocked_service_ports`` and the gate below says
            # BLOCKED.
            verified_services = await self._recover_declared_services(
                vp_id, vp_node,
            )
            # The recovery may have moved a service to a new address,
            # and the plan's placeholders were re-resolved into the
            # executor's VP — so the copy is taken *after* it.
            vp = dict(vp_node)
            vp_id = str(vp.get("id", "unknown"))
            # Service-freshness gate (2026-09-16): the round-start
            # preflight could not bring a service port back to life →
            # any VP referencing that port is BLOCKED up front rather
            # than spending a sub-agent on a guaranteed
            # connection-refused. Ports are matched with the same
            # extractor the preflight used, so the semantics agree.
            if self._blocked_service_ports:
                from service_freshness import extract_service_ports
                vp_ports = set(extract_service_ports(
                    {"verification_points": [vp]}, self.project_dir,
                ))
                # 2026-09-18 C1: a VP that references a declared service
                # by name carries no literal port (the placeholder is
                # resolved at dispatch time), so match on the
                # declaration table as well — otherwise the one VP that
                # uses the declaration *correctly* is the one VP the
                # blocked-port gate cannot see.
                uses = vp.get("uses_services")
                if isinstance(uses, list):
                    for name in uses:
                        decl = self._service_declarations.get(str(name).lower())
                        if decl is not None:
                            vp_ports.add(decl.port)
                blocked = sorted(vp_ports & self._blocked_service_ports)
                if blocked:
                    self._safe_log_freshness_event(
                        vp_id, "service_freshness_blocked",
                        {"ports": blocked},
                    )
                    return self._build_service_freshness_blocked_verdict(
                        vp_id, blocked,
                    )
            # 2026-10-25 (repair-r1-29): a service the round could not
            # place has **no address** at all — the port it declares
            # belongs to somebody else.  The VPs that need such a
            # service are blocked by **name**: the placeholder naming
            # it never resolved, so there is no port left to match on
            # and the blocked-port gate cannot see them.
            unusable = self._unusable_services()
            if unusable:
                hit = sorted(set(unusable) & _vp_service_names(vp))
                if hit:
                    ports = sorted(
                        unusable[name]["port"] for name in hit
                        if unusable[name]["port"]
                    )
                    self._safe_log_freshness_event(
                        vp_id, "service_unusable_blocked",
                        {"services": hit, "ports": ports},
                    )
                    return self._build_unusable_service_blocked_verdict(
                        vp_id, hit, ports,
                    )
            # 2026-10-26: the ONE service precondition.  It runs here —
            # after the gates above (which answer "is this port
            # blocked?" and "could the service be placed at all?") and
            # before the method branch, so the ``api_test`` path and the
            # ``ui_validation`` path are gated by the **same** check.
            #
            # The recovery above only asked whether the port accepted a
            # connection; this asks the service itself, over HTTP, with
            # the request a verification point would send.  The
            # 2026-10-26 round dispatched twelve api_test points at a
            # port that was refusing connections while its
            # ui_validation point was talking to the very same URL
            # minutes later — two paths, two checks, two answers.
            blocked = await self.ensure_vp_services_ready(
                vp_id, vp, verified=verified_services,
            )
            if blocked is not None:
                return blocked
            if "method" in vp and "verification_method" not in vp:
                vp["verification_method"] = vp["method"]
            # 2026-09-18（VP 判定重构）：api_test 由框架确定性地执行，
            # 不再拉子 agent。它要回答的是"这个服务是否真的按契约应答"，
            # 而这个问题有一个不需要 LLM 的答案：发请求、核断言。
            #
            # 之前这条路走子 agent，判定却依赖子 agent 自报的
            # pytest_exit_code / tests_run —— 同一个工件在两轮里判出相反
            # 结果的直接原因（one run's VP-001）。现在 (请求, 响应, 断言) 决定
            # 判定，跨轮可复现。
            if str(vp.get("verification_method") or "") == "api_test":
                # 2026-10-24（数据面身份）：端口有人在听、HTTP 回 200，并不
                # 说明回答的就是本轮起的那个实例 —— 生产那一轮就是这样把
                # VP 打到了一个空库上。api_test 下发前再取一次 /health/data，
                # 与预检记录的指纹比对；取不到或对不上就是实例丢失，最多
                # 重新 ensure 一次，还不行就 BLOCKED（绝不下发请求、绝不记
                # 成 api_test FAILED）。
                blocked = await self._guard_api_test_data_plane(vp_id, vp)
                if blocked is not None:
                    return blocked
                # 门禁可能重新起了一个搬了地址的服务，占位符已按新端口
                # 重新解析进执行器的 VP —— 所以副本在门禁**之后**取。
                vp = dict(vp_node)
                if "method" in vp and "verification_method" not in vp:
                    vp["verification_method"] = vp["method"]
                return self._run_api_test_vp(vp)
            # 2026-09-18（D2）：Phase 2 的整仓 CI 门禁同理由框架确定性地
            # 执行 —— 判定依据就是仓库 CI 总入口的退出码，没有任何需要
            # LLM 判断的成分，也没有自报环节。
            if str(vp.get("verification_method") or "") == "full_ci":
                return self._run_full_ci_vp(vp)
            # 2026-09-18（method 收敛）：退役/未知 method 的 VP 本应在计划
            # 归一化时就被标 obsolete，这里再兜一层 —— `MethodTemplateRegistry`
            # 找不到模板会抛 ValueError，那会让一条本该"干净地不跑"的 VP
            # 变成一条带 traceback 的崩溃。
            from verification_subagent import SUPPORTED_METHODS
            if str(vp.get("verification_method") or "") not in SUPPORTED_METHODS:
                retired_method = str(vp.get("verification_method") or "(missing)")
                self._safe_log_freshness_event(
                    vp_id, "unsupported_method_blocked",
                    {"method": retired_method},
                )
                return self._build_retired_method_blocked_verdict(
                    vp_id, retired_method,
                )
            # 2026-09-18（依据核对）：把本 VP 的证据产物目录告诉子 agent。
            # 它必须把结论的出处写到这里，框架随后逐条核对
            # （``verification_evidence``）。目录随 VP 走，不新增函数签名。
            vp["evidence_artifact_dir"] = str(
                self.plan_dir / "vp_artifacts" / vp_id
            )
            # 2026-09-18（端口让路）：把服务实际地址告诉 VP。
            service_env = self._service_env_for_vps()
            if service_env:
                vp["service_env"] = service_env
            # 2026-09-18（依据核对）：code_review / ui_validation 的结论必须
            # 带着可核对的产物回来。产物缺失或站不住 → PASSED 改判 FAILED
            # （见 ``verification_evidence``）。api_test 不走这条路，它的
            # 依据在 ``verification_api_runner`` 里内联产生。
            return self._apply_evidence_check(
                vp, self._attach_evidence_command(
                    vp, await self._run_single_vp_async(vp),
                ),
            )

        async def _sub_agent_runner(vp_node: dict) -> dict:
            """One dispatch — with the instance on **both** sides of it.

            2026-10-26: the address a request goes to can change hands
            while the request is in flight, and the round has to be able
            to say so.  So the instance serving the address is looked at
            **before** the dispatch and **after** it, and the two
            snapshots go into the point's evidence under their contract
            names.

            A handover is not a verdict about the product: the answer
            came from an instance the round cannot place.  The verdict's
            own status is left alone — the executor only accepts the
            statuses it accepts — but the handover is recorded as one of
            the round's own contradictions, and the round's reconciliation
            marks the point ``invalid_env`` and gives it the **one**
            second look every other contradicted point gets.  What the
            second look says is what lands.
            """
            self._vp_dispatch_runner = _sub_agent_runner
            vp_id = str(vp_node.get("id", "unknown"))
            before = self._capture_serving_snapshot(vp_node, vp_id)
            # 2026-10-26: the round's **instance epoch** is compared
            # before the request goes out.  A request sent to the wrong
            # instance measures the wrong data plane, and the verdict it
            # produces is worse than no verdict at all — it is a
            # confident statement about somebody else's data.  So the
            # epoch is checked first: when it moved, the service is
            # re-ensured and the epoch updated before the dispatch, and
            # the points answered under the old epoch are recorded as
            # contradicted (they get the round's one second look).
            await self.ensure_instance_epoch(
                vp_id, self._epoch_observation(vp_node, vp_id, before),
            )
            token = str(getattr(self, "_instance_epoch_token", "") or "")
            if token:
                self._vp_epoch_tokens[vp_id] = token
            verdict = await _dispatch_vp(vp_node)
            after = self._capture_serving_snapshot(vp_node, vp_id)
            return self._apply_serving_handover(vp_id, before, after, verdict)

        return VerificationExecutor(
            verification_plan=executor_plan,
            plan_id=self.plan_dir.name,
            plan_dir=self.plan_dir,
            sub_agent_runner=_sub_agent_runner,
            verif_repo=getattr(self, "verif_repo", None),
        )

    # ------------------------------------------------------------------
    # BLOCKED verdicts — retired / unknown verification_method
    # ------------------------------------------------------------------

    def _build_retired_method_blocked_verdict(
        self, vp_id: str, retired_method: str,
    ) -> dict:
        """Synthesize a BLOCKED verdict for a retired / unknown method.

        2026-09-18（method 收敛）：``verification_method`` 已退役（例如
        ``automated_test``）或根本不存在时，这条 VP **验不下去** —— 它不是
        「验了没过」，而是「根本没验」。这两种语义都落在失败桶里（桶规则
        见 :func:`verification_buckets.bucket_for_status`），所以 reason
        必须带 ``BLOCKED — `` 前缀，读报告的人才分得清。

        与 :meth:`_build_binary_freshness_blocked_verdict` /
        :meth:`_build_service_freshness_blocked_verdict` 同形：``status`` /
        ``reasons`` / ``actual_result`` / ``evidence`` / ``framework_checks``。
        """
        from verification_subagent import SUPPORTED_METHODS
        return {
            "status": "BLOCKED",
            "reasons": [
                blocked_reason(
                    f"verification_method {retired_method!r} is not one "
                    f"of {list(SUPPORTED_METHODS)}"
                )
            ],
            "actual_result": (
                f"该 VP 的 verification_method（{retired_method}）已退役"
                f"或不存在，无法执行；请把它重新表达为 api_test / "
                f"ui_validation / code_review。"
            ),
            "evidence": {"method": retired_method},
            "framework_checks": [],
        }

    # ------------------------------------------------------------------
    # Binary freshness — pre-flight check (per-VP) + report mutation
    # ------------------------------------------------------------------
    #
    # Two related entry points on this class:
    #
    # 1. ``_binary_freshness_pre_check`` — runs in ``_sub_agent_runner``
    #    (above) BEFORE every VP, with its blocking calls dispatched via
    #    ``asyncio.to_thread`` so the round's event loop keeps serving
    #    the sibling VPs. Returns the ``FreshnessReport`` so the caller
    #    can decide to BLOCK on a stale result.
    #
    # 2. ``_append_binary_freshness_result`` — the TDD-contract
    #    surface that mutates an existing ``verification_report.json``
    #    dict in place. Used by the original Phase-3 post-check path
    #    that the TDD test pins; the pre-flight check above is the
    #    primary defense, but this method stays so the audit
    #    framework_checks entry still lands in the saved report.

    def _build_binary_freshness_blocked_verdict(
        self, vp_id: str, freshness: FreshnessReport,
        rebuild_result: Optional[Any] = None,
    ) -> dict:
        """Synthesize a BLOCKED verdict for a stale-binary VP.

        Shape matches the executor's verdict contract
        (``status`` ∈ {PASSED, FAILED, BLOCKED}, ``reasons``,
        ``evidence``). The ``framework_checks`` payload mirrors
        :meth:`FreshnessReport.to_framework_check_entry` so the
        downstream report-mutation path can carry the same data
        forward.

        2026-09-07: include ``actual_result`` so the report
        carries the freshness detail forward verbatim instead of
        collapsing into a generic ``verdict schema rejected`` message
        when downstream consumers filter ``status in {FAILED, SKIPPED}``
        (see ``server.py:_build_verification_progress`` and
        ``repair_generator.collect_failure_evidence``). The rebuild
        command is included so a repair-task generator can emit a
        concrete ``cargo build --release`` / ``maturin develop`` step
        without re-deriving from disk.

        2026-09-07 BinaryRebuildAgent plan: when the intelligent rebuild
        orchestrator ran, its ``diagnostics`` and ``commands_run`` are
        folded into ``evidence['rebuild_attempt']`` so the Feishu card
        shows *why* the rebuild failed (e.g. "maturin not on PATH, agent
        tried 3 approaches") instead of a bare "binary stale".
        """
        rebuild = freshness.rebuild_command or "(no rebuild command registered)"
        evidence: Dict[str, Any] = {
            "binary_freshness": freshness.to_dict(),
        }
        if rebuild_result is not None:
            evidence["rebuild_attempt"] = {
                "agent_used": getattr(rebuild_result, "agent_used", False),
                "diagnostics": getattr(rebuild_result, "diagnostics", ""),
                "commands_run": list(getattr(rebuild_result, "commands_run", []) or []),
                "post_check_passed": getattr(rebuild_result, "post_check_passed", False),
            }
        return {
            "status": "BLOCKED",
            "reasons": [blocked_reason(f"binary stale: {freshness.detail}")],
            "actual_result": (
                f"binary stale ({freshness.kind}): {freshness.detail}\n"
                f"rebuild: {rebuild}"
            ),
            "evidence": evidence,
            "framework_checks": [freshness.to_framework_check_entry()],
        }

    def _append_binary_freshness_result(
        self, report: dict, plan_data: dict,
    ) -> dict:
        """TDD-contract surface: append a ``VP-binary-freshness``
        entry to ``report['framework_checks']`` and downgrade
        ``overall_status`` to ``FAILED`` when the binary is stale.

        The actual check delegates to
        :func:`binary_freshness.check_binary_freshness` so the
        pre-flight hook (above) and this report-mutation method
        share the same FreshnessReport.
        """
        freshness = check_binary_freshness(self.project_dir)
        entry = freshness.to_framework_check_entry()
        report.setdefault("framework_checks", []).append(entry)
        if freshness.is_stale:
            report["overall_status"] = "FAILED"
        return report

    def _safe_log_freshness_event(
        self, vp_id: str, event_type: str, data: dict,
    ) -> None:
        """Persist a binary-freshness audit entry; swallow the
        ``RuntimeError`` raised when ``start_round()`` hasn't run yet
        (e.g. the pre-check fires before the round is fully
        initialised).

        The freshness check must NEVER abort VP execution because
        the audit log isn't open — the operator catches the
        warning via stderr instead.
        """
        try:
            self.persistence.write_verification_point_log(
                vp_id, event_type, data,
            )
        except RuntimeError as exc:
            logger.debug(
                "vp=%s freshness event %s not logged: %s",
                vp_id, event_type, exc,
            )

    # ------------------------------------------------------------------
    # Service freshness — round-start preflight ("VP0", 2026-09-16)
    # ------------------------------------------------------------------

    def _preflight_service_freshness(self, plan_data: dict) -> Any:
        """Bring the plan's services up BEFORE any VP runs.

        Two paths, chosen by whether the plan carries a ``services``
        declaration (C1, 2026-09-18):

        * **Declared** (``self._service_declarations`` non-empty) —
          :meth:`_preflight_declared_services` starts each declared
          service **once** with its declared ``start_cmd``, records who
          it spawned for exit-time reaping, and blocks only the ports
          it could not bring up. This is the "start it once and point
          every VP at it" model; no VP starts anything itself.

        * **Legacy** (no ``services`` key) — the original
          scrape-ports-out-of-the-command-text path via
          :func:`service_freshness.assess_service_freshness` +
          :func:`service_restart_agent.attempt_service_restart`.
          Unchanged, so a plan written before the declaration schema
          behaves exactly as it did.

        Both paths fill ``self._blocked_service_ports`` on failure; the
        per-VP runner converts any VP touching a blocked port into a
        BLOCKED verdict so the round produces diagnostic signal instead
        of N connection-refused failures polluting repair-task
        generation.

        Never raises — a broken preflight must not take the round down.
        """
        self._blocked_service_ports = set()
        self._blocked_service_details = {}
        self._service_recovery_locks = {}
        self._service_recovery_outcomes = {}
        self._service_runtime_map = None
        # 2026-10-25 (repair-r1-22): 本轮的绑定也清 —— 上一轮绑的实例是上一轮
        # 的事，这一轮的 ensure 会重新绑定。留着不放，这一轮的 VP 就会去上一轮
        # 的实例。
        self._service_bindings = {}
        # 2026-10-24: 本轮「服务起不回来」的账也要清 —— 上一轮起不回来的
        # 服务，这一轮要重新试一次（服务可能已经被人修好了）。留着不放，
        # 这一轮的 VP 就直接进 BLOCKED 门禁，连试都不会试。
        self._service_reensure_failed = set()
        # 2026-10-26: 同一个道理，服务「自检过」的记录也要清 —— 上一轮
        # 问过的答案不能替这一轮作答。
        self._service_self_check_records = {}
        # 2026-10-25 (repair-r1-24): the round's "could not reach the
        # service" record starts empty too — a service that was
        # unreachable last round may be reachable now.
        self._service_unreachable = {}
        try:
            if self._service_declarations:
                return self._preflight_declared_services(plan_data)
            return self._preflight_legacy_service_freshness(plan_data)
        except Exception as exc:  # noqa: BLE001 — preflight never aborts
            logger.warning(
                "[service_freshness] preflight errored: %s; proceeding",
                exc,
            )
            self._safe_log_freshness_event(
                "__preflight__", "service_freshness_error",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            # An empty report (never None) so callers can always read
            # ``.action`` / ``.detail`` for the audit trail — the
            # historical contract this replaced.
            from service_freshness import ServiceFreshnessReport
            empty = ServiceFreshnessReport()
            empty.action = "error"
            empty.detail = f"{type(exc).__name__}: {exc}"
            self._last_service_freshness_report = empty
            return empty

    def _preflight_declared_services(self, plan_data: dict) -> Any:
        """Start the plan's declared services once and record ownership.

        Unlike the legacy path this does **not** guess a start command:
        the declaration's ``start_cmd`` is authoritative, and a port
        held by a process that does not match that command is refused
        rather than killed (see :mod:`service_manager` for the three-way
        ownership rule).

        The spawn itself comes from the **project's** ``service_manager``
        (see :func:`_load_service_manager`): the service that the
        declaration starts is the project's own code, so the code that
        starts it has to be the project's too.  Otherwise a project that
        fixed its spawn shape would still be verified through the
        backend's old one.

        A stale build artefact is rebuilt first — relaunching a service
        against yesterday's binary would just serve old code again,
        which is the ordering the legacy path already had.
        """
        service_manager = _load_service_manager(self.project_dir)
        ensure_services = service_manager.ensure_services
        record_runtimes = service_manager.record_runtimes

        self._maybe_rebuild_before_service_start()
        runtime_map = ensure_services(
            self.project_dir,
            self._service_declarations,
            round_number=self._current_round,
        )
        self._service_runtime_map = runtime_map
        # 2026-10-25 (repair-r1-22): 本轮 ensure 产出的实例就是本轮的绑定 ——
        # VP 的请求地址由它决定，而不是由 runtime map 每次重新推导。map 仍然
        # 照记（它是"发生过什么"的记录），但它不再决定请求往哪儿发。
        for name, runtime in (runtime_map.runtimes or {}).items():
            if self._bind_service_runtime(name, runtime) is None:
                self._release_service_binding(name)

        # 2026-10-24 (repair-r3-16): the reason a service is not up
        # belongs in the event, not only in a log line that dies with the
        # process.  ``to_dict`` buries it under
        # ``services.<name>.detail``; ``unready_details`` says it
        # outright, keyed by the port the gate will block.
        unready_details = {
            str(runtime.port): runtime.detail
            for runtime in runtime_map.runtimes.values()
            if not runtime.ready
        }
        self._safe_log_freshness_event(
            "__preflight__", "services_ensured",
            {
                **runtime_map.to_dict(),
                "unready_details": unready_details,
            },
        )
        self._safe_log_freshness_event(
            "__preflight__",
            "service_bindings",
            {
                name: binding.to_dict()
                for name, binding in sorted(self._service_bindings.items())
            },
        )
        print(
            "[Verification] Declared services: "
            f"ready={runtime_map.ready_names} "
            f"unready_ports={runtime_map.unready_ports}"
        )
        if runtime_map.unready_ports:
            self._blocked_service_ports = set(runtime_map.unready_ports)
            for name, runtime in sorted(runtime_map.runtimes.items()):
                if not runtime.ready:
                    self._blocked_service_details[runtime.port] = (
                        runtime.detail
                    )
                    logger.warning(
                        "[service_manager] service %s on port %s is not "
                        "ready: %s", name, runtime.port, runtime.detail,
                    )
        # Record *after* the readiness verdict: a service that failed to
        # come up may still have left a half-started process behind, and
        # that process is exactly the orphan C3 has to reap.
        record_runtimes(
            self.plan_dir,
            self.plan_dir.name,
            runtime_map,
            project_dir=self.project_dir,
            round_number=self._current_round,
        )
        # 2026-10-24: the services are up — now record **which data
        # plane** each of them is serving.  The preflight is the only
        # moment the round knows the instance it started; every later
        # api_test compares against this fingerprint.
        self._capture_data_plane_fingerprints()
        # 2026-10-26: the round's **own** instance — the one its
        # ``ensure`` just bound.  The round-level handover record is
        # built from this snapshot and a fresh read at the round's end,
        # so a reader can tell that the address the round used belonged
        # to somebody else by the end.
        self._capture_round_serving_snapshot()
        # 2026-10-26: the round names the instance it is asking, before
        # it dispatches anything.  From here on every dispatch is
        # compared against this name, and a name that changed hands is
        # a contradiction of the round's own evidence.
        self.round_instance_epoch()
        return runtime_map

    def _capture_round_serving_snapshot(self) -> None:
        """Record the instance serving the round's own address.

        The round's ``ensure`` has just bound its services, and the
        preflight has just asked each of them who it is.  The answer
        the round already holds is the one this record is built from —
        the service is not asked a second time.

        A service that does not identify itself yields no snapshot —
        "nobody said" is an answer, and it is not the same as "somebody
        else said".
        """
        observed = getattr(self, "_observed_instances", None) or {}
        for name in sorted(self._service_bindings):
            binding = self._service_bindings.get(name)
            port = getattr(binding, "port", None)
            if not port:
                continue
            instance = observed.get(int(port))
            if instance is None:
                continue
            self._round_serving_port = int(port)
            self._round_serving_snapshot = _serving_snapshot(instance)
            return

    # ------------------------------------------------------------------
    # The round's instance epoch (2026-10-26)
    # ------------------------------------------------------------------
    #
    # "Which instance is this round asking?" is a question the round has
    # to answer **before** it dispatches anything, and to keep answering
    # while it dispatches.
    #
    # On 2026-10-26 the round asked it too late: a verification point was
    # refused a connection on port 64469 at 02:25:01, and the round could
    # not say whether the refusal was about the product or about a
    # service that had been replaced behind its back.  The criterion the
    # round was verifying — *the waiting event must be readable* — was
    # never looked at, because the round never learned who was answering
    # it.
    #
    # So the round names the instance it is asking, once, and compares
    # that name before every dispatch after it.  A name that changed is
    # a contradiction of the round's own evidence: the points answered
    # under the old name are not verdicts about the product, and they
    # get the round's one second look under the new one.

    def instance_epoch(self) -> Dict[str, Any]:
        """The round's epoch — the instance whose answers this round accepts.

        Empty when the round has not named an instance yet: either it has
        no service to ask, or the service does not identify itself.  The
        empty answer is what the loop's gate reads — a round that cannot
        say which instance it is asking must not dispatch anything.
        """
        epoch = getattr(self, "_instance_epoch", None)
        return dict(epoch) if isinstance(epoch, dict) else {}

    def round_instance_epoch(self) -> Dict[str, Any]:
        """The instance the round is asking — named now, or named already.

        The **first** successful read of ``/health/data`` names the
        epoch; from then on the round keeps the name it has and compares
        every dispatch against it.  A later read never silently replaces
        the name: replacing it would throw away the very fact the round
        needs — that the instance behind its address changed hands.

        So this method names the instance only when the round has not
        named it yet.  :meth:`establish_instance_epoch` is the operation
        that (re)names it, and it is called on purpose: at the round's
        start, and after a re-ensure that brought the service back.
        """
        epoch = self.instance_epoch()
        if epoch:
            return epoch
        return self.establish_instance_epoch()

    def _epoch_port(self) -> Optional[int]:
        """The port the round's epoch is about.

        The round's own address — the one it recorded when it first talked
        to its own service — is the port the epoch belongs to.  A round
        that never reached a service falls back to the address it is bound
        to, because that is the address its verification points will go
        to.
        """
        port = getattr(self, "_round_serving_port", None)
        if port:
            return int(port)
        for name in sorted(getattr(self, "_service_bindings", None) or {}):
            binding = self._service_bindings.get(name)
            bound = getattr(binding, "port", None)
            if bound:
                return int(bound)
        for name in sorted(getattr(self, "_service_declarations", None) or {}):
            decl = self._service_declarations.get(name)
            declared = getattr(decl, "port", None)
            if declared:
                return int(declared)
        return None

    def _epoch_service_name(self, port: Optional[int]) -> str:
        """The name of the service the epoch's port belongs to.

        The round may talk to several services; the epoch belongs to the
        one whose address it was read from.  The name is what the
        round's re-ensure needs to say which service to bring back.
        """
        if not port:
            return ""
        for name in sorted(getattr(self, "_service_bindings", None) or {}):
            binding = self._service_bindings.get(name)
            if int(getattr(binding, "port", 0) or 0) == int(port):
                return str(name)
        for name in sorted(getattr(self, "_service_declarations", None) or {}):
            decl = self._service_declarations.get(name)
            try:
                declared = int(getattr(decl, "port", 0) or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared == int(port):
                return str(name)
        return ""

    def _epoch_serving_instance(self) -> Optional[Dict[str, Any]]:
        """The instance block the round's own address is served by.

        Read over ``/health/data`` — the same road a verification point's
        request travels — so the round's idea of who is answering is the
        service's own statement about itself.
        """
        port = self._epoch_port()
        if not port:
            return None
        return _health_data_instance(int(port))

    def _epoch_ensured_token(self) -> str:
        """The instance the round's **own** ensure produced.

        The round's record of the process it started (or adopted): the
        binding it holds, or the runtime the service manager reported.
        It names the instance the round **asked** for — which is not
        the same as naming the instance that **answered**, and the
        round never pretends the two are the same.

        It is the second road to a name.  A service that does not speak
        ``/health/data`` (an application of the project's own, a static
        preview, a third-party server) cannot say who it is — and the
        round does not gate it for that, because "not comparable" is
        not "wrong".  But the round still knows **which** instance it
        asked for, and that is enough to know that it is not reading an
        address nobody can account for.
        """
        for name in sorted(getattr(self, "_service_bindings", None) or {}):
            binding = self._service_bindings.get(name)
            token = str(getattr(binding, "boot_id", "") or "")
            if token:
                return token
        runtimes = getattr(
            getattr(self, "_service_runtime_map", None), "runtimes", None,
        ) or {}
        for name in sorted(runtimes):
            token = self._runtime_boot_id(runtimes.get(name))
            if token:
                return token
        return ""

    def establish_instance_epoch(self) -> Dict[str, Any]:
        """Name the instance the round is asking, right now.

        The epoch is read from ``/health/data`` and carries the pid and
        the boot id — the two facts that tell two runs of the same port
        apart.  That is the **comparable** part of the epoch: the
        instance's own statement about itself, which the round can
        compare with the next statement it gets.

        A service that does not identify itself cannot be compared —
        "nobody said" is not "somebody else said" — so the comparable
        part stays empty.  The round still names the instance it asked
        for, through its own record of the process it started, and that
        name is what keeps the epoch from being empty: a round that
        cannot name the instance it is asking would be reading an
        address whose owner nobody can say.
        """
        instance = self._epoch_serving_instance()
        token = _instance_epoch_token(instance)
        ensured_token = self._epoch_ensured_token()
        if not token and not ensured_token:
            self._instance_epoch = None
            self._instance_epoch_token = ""
            self._safe_log_freshness_event(
                "__preflight__", INSTANCE_EPOCH_EVENT,
                {
                    "plan_id": self.plan_dir.name,
                    "round": self._current_round,
                    "epoch": None,
                    "detail": (
                        "the service did not identify itself and the round "
                        "has no record of an instance; the round has nobody "
                        "to ask"
                    ),
                },
            )
            return {}
        port = self._epoch_port()
        epoch = {
            "service": self._epoch_service_name(port),
            "port": port,
            "pid": (
                instance.get("pid")
                if isinstance(instance, dict) and instance.get("pid")
                else None
            ),
            "boot_id": str((instance or {}).get("boot_id") or ""),
            "plans_dir": str((instance or {}).get("plans_dir") or ""),
            #: The comparable identity — the service's own statement.
            #: Empty when the service does not identify itself: there is
            #: nothing to compare, so the round compares nothing.
            "token": token,
            #: The instance the round's own ensure produced.  It says
            #: which instance the round asked for; it does not say that
            #: this instance answered, and it is never used for that.
            "ensured_token": ensured_token,
        }
        self._instance_epoch = epoch
        self._instance_epoch_token = token
        self._safe_log_freshness_event(
            "__preflight__", INSTANCE_EPOCH_EVENT,
            {
                "plan_id": self.plan_dir.name,
                "round": self._current_round,
                "epoch": dict(epoch),
            },
        )
        return dict(epoch)

    def _epoch_conflicts(self) -> List[Dict[str, Any]]:
        """The epoch changes the round has seen.

        ``getattr`` for the same reason as :meth:`_serving_snapshots`: an
        orchestrator built without ``__init__`` has seen none.
        """
        conflicts = getattr(self, "_instance_epoch_conflicts", None)
        if conflicts is None:
            conflicts = []
            self._instance_epoch_conflicts = conflicts
        return conflicts

    def _stale_epoch_vps(self, token: str) -> List[str]:
        """The points that were dispatched under *token*.

        They are the points whose answers belong to an instance the round
        cannot place any more.
        """
        tokens = getattr(self, "_vp_epoch_tokens", None) or {}
        return sorted(
            str(vp_id)
            for vp_id, seen in tokens.items()
            if str(seen or "") == str(token or "")
        )

    async def ensure_instance_epoch(
        self, vp_id: str, observed: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Is the instance still the round's?  And what if it is not?

        Called **before** every dispatch.  When the instance behind the
        round's address changed hands, the round is asking somebody
        else: the answers obtained so far belong to a process the round
        cannot place, and the request about to go out would be measured
        against the wrong data plane.

        So the round re-ensures the service, adopts the new instance and
        updates its epoch **before** the dispatch goes out — and it
        records the contradiction, so the points answered under the old
        epoch are marked ``invalid_env`` and get the round's one second
        look under the new one.

        Args:
            vp_id: the point about to be dispatched — it names the
                contradiction in the round's own log.
            observed: the instance block the caller has already read
                (the "before" side of the dispatch).  ``None`` means the
                caller has no observation, and the round reads one.

        Returns:
            ``{"changed": bool, "epoch": dict, ...}`` — ``changed`` says
            whether the instance behind the round's address changed hands.
        """
        epoch = self.instance_epoch()
        token = str(epoch.get("token") or "")
        if not token and epoch:
            # The round named the instance it is asking, but the service
            # does not say who is answering: there is nothing to
            # compare, so there is nothing to decide.  "Nobody said" is
            # not "somebody else said".
            return {"changed": False, "epoch": epoch}
        if not token:
            # The round has not named an instance yet: this is the first
            # successful read of ``/health/data``, and it establishes
            # the epoch the rest of the round is measured against.
            epoch = self.establish_instance_epoch()
            token = str(epoch.get("token") or "")
            if not token:
                return {"changed": False, "epoch": dict(epoch)}

        instance = observed
        if instance is None:
            instance = self._epoch_serving_instance()
        current = _instance_epoch_token(instance)
        if not current or current == token:
            # "Nobody said" is not "somebody else said": an instance
            # that does not identify itself is never treated as a
            # different one.
            return {"changed": False, "epoch": epoch}

        # The instance changed hands.
        before = dict(epoch)
        affected = self._stale_epoch_vps(token)
        logger.warning(
            "[verification] the instance serving the round's address "
            "changed hands before %s was dispatched (boot %s before, boot "
            "%s after); the answers obtained under the old epoch are not "
            "verdicts about the product",
            vp_id, before.get("boot_id") or "?", current,
        )
        restored = await self._reensure_epoch_service(before)
        after = self.establish_instance_epoch() if restored else {}
        conflict = {
            "kind": EPOCH_CHANGED_CONFLICT_KIND,
            "url": self._epoch_url(before),
            "origin": _url_origin(self._epoch_url(before)),
            "failed_vps": affected,
            "succeeded_vp": None,
            "epoch_before": before,
            "epoch_after": dict(after),
            SERVING_BEFORE_FIELD: self._epoch_snapshot(before),
            SERVING_AFTER_FIELD: self._epoch_snapshot(after),
        }
        self._epoch_conflicts().append(conflict)
        self._safe_log_freshness_event(
            vp_id, INSTANCE_EPOCH_EVENT,
            {
                "plan_id": self.plan_dir.name,
                "round": self._current_round,
                "epoch_before": before,
                "epoch_after": dict(after),
                "restored": bool(restored),
                "stale_vps": affected,
            },
        )
        return {
            "changed": True,
            "epoch": dict(after),
            "before": before,
            "conflict": conflict,
        }

    def _epoch_observation(
        self, vp: dict, vp_id: str, snapshot: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """The observation the epoch check may use, if it is the right one.

        The snapshot the dispatch took is about **the point's** address.
        The epoch is about the round's own address — and in a round that
        talks to several services those are not the same question.  So
        the snapshot is only handed over when it was read from the
        epoch's port; otherwise the epoch check reads its own.
        """
        if snapshot is None:
            return None
        epoch_port = self._epoch_port()
        if not epoch_port:
            return None
        vp_port = self._serving_port_for_vp(vp, vp_id)
        if not vp_port or int(vp_port) != int(epoch_port):
            return None
        return snapshot

    def _epoch_url(self, epoch: Dict[str, Any]) -> str:
        """The address the epoch belongs to."""
        port = epoch.get("port") if isinstance(epoch, dict) else None
        if not port:
            return ""
        return f"http://127.0.0.1:{int(port)}"

    @staticmethod
    def _epoch_snapshot(epoch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The snapshot shape the round's handover records use."""
        if not isinstance(epoch, dict) or not epoch:
            return None
        return {
            "pid": epoch.get("pid"),
            "boot_id": str(epoch.get("boot_id") or ""),
            "plans_dir": str(epoch.get("plans_dir") or ""),
            "state_db_inode": None,
        }

    async def _reensure_epoch_service(self, epoch: Dict[str, Any]) -> bool:
        """Ask the round's own precondition for the service behind the epoch.

        The round re-ensures through the **one** precondition every
        verification path shares (:meth:`ensure_service_ready`): the
        same port probe, the same start attempt, the same HTTP
        self-check.  That is deliberate, and it is what keeps the two
        questions apart:

          * a service the round itself **restarted** is adopted — the
            expectation follows it, exactly as it follows a service the
            round brought back in any other way; and
          * a service that is merely **alive** under a stranger's boot
            id is not: the round did not start it, so it may not
            claim it, and the identity gate still refuses to ask it.

        So a port taken over by a stranger leaves the round's
        expectation where it was — the request is not sent to the
        stranger — while a service that died is brought back by the
        round's own start path, and the round's address follows it.

        Returns ``True`` when the service is serving again, so the
        epoch may be re-read.
        """
        name = str(epoch.get("service") or "")
        declaration = self._service_declarations.get(name)
        if declaration is None:
            # The bindings are keyed in lower case, the declarations keep
            # the name the plan wrote — so the round asks for the
            # declaration by the name it knows, whatever its case.
            for key, decl in sorted(self._service_declarations.items()):
                if str(key).lower() == name.lower():
                    declaration = decl
                    name = str(key)
                    break
        if declaration is None:
            return False
        try:
            outcome = await asyncio.to_thread(
                self.ensure_service_ready, declaration,
            )
        except Exception as exc:  # noqa: BLE001 - a failed start is a fact
            logger.warning(
                "[verification] the epoch's re-ensure of service %r "
                "errored: %s", name, exc,
            )
            return False
        if not outcome.get("ready"):
            logger.warning(
                "[verification] the epoch's re-ensure of service %r did "
                "not bring it back: %s", name,
                outcome.get("detail") or "no detail",
            )
            return False
        # The service may have come back **somewhere else**: the round's
        # own address follows it, and the epoch is read there.
        binding = self._service_bindings.get(name.lower())
        new_port = int(getattr(binding, "port", 0) or 0)
        if new_port and new_port != int(epoch.get("port") or 0):
            self._round_serving_port = new_port
        return True

    # ------------------------------------------------------------------
    # Data-plane identity (2026-10-24)
    # ------------------------------------------------------------------
    #
    # "Something is listening on the port" and "the thing listening is
    # the instance this round started, reading the fixture this round
    # seeded" are two different questions.  The 2026-10-24 production
    # round was graded on the first one: the port was busy, the HTTP
    # answer was 200 with ``status: ok``, and the data behind it was
    # another database — ``/health/data`` reported 0 routing rows and
    # every later assertion ran against an empty one.
    #
    # The round therefore identifies an instance by its **data plane**:
    # the port it listens on, the pid behind it, and the number of
    # ``plan_routing`` rows its ``/health/data`` reports — read over
    # the very same HTTP path an ``api_test`` VP uses, because a probe
    # that took a different road could answer a different question than
    # the VP's request does.
    #
    # 2026-10-26 (R2): the fingerprint also has to be **honest**.  The
    # 2026-10-26 production round recorded a fingerprint that said two
    # contradictory things at once — ``routing_rows=11`` (what the
    # instance answered) next to ``expected_rows=49`` (how many rows the
    # *agent's own* database held).  The two numbers come from two
    # different databases, so they can never be equal, and the record
    # phase stored the contradiction silently: every ``api_test`` VP of
    # the round then rediscovered it on its own, as an opaque
    # ``data_plane_mismatch`` block.  So the record phase now *says so*,
    # once, where it is the only place that still knows both numbers.

    def _capture_data_plane_fingerprints(self) -> Dict[str, Any]:
        """Record which data plane each declared service is serving.

        Called once, at the end of the round-start preflight — the
        moment the round knows which instance it started.  A service
        that does not answer ``/health/data`` (a project with no data
        plane of its own) is **not** recorded and is therefore never
        gated: not knowing the data plane is not the same as knowing
        it is wrong.

        The record phase is also the only place that knows **both**
        numbers at once: the count the instance answered with, and the
        count the round's own fixture holds.  When they disagree, the
        instance is not serving this round's fixture — so the phase says
        so, once, in a ``data_plane_fixture_mismatch`` event, instead of
        storing the contradiction silently and letting every ``api_test``
        VP of the round rediscover it on its own.

        Never raises — a probe that fails is a service that is not
        recorded, not a broken preflight.
        """
        from service_freshness import expected_routing_rows, probe_data_plane

        # The fixture is the state database the round's own service
        # reads; its row count is what "the right data plane" means.
        # The path is resolved **here** so that the count and the
        # database it was read from travel together: "49 rows" means
        # nothing without "in *this* file", and the mismatch event has
        # to name the file it read.
        db_path: Optional[Path] = None
        try:
            from config_paths import resolve_state_db_path

            db_path = resolve_state_db_path()
        except Exception as exc:  # noqa: BLE001 - defensive
            logger.warning(
                "[data_plane] could not resolve the fixture's database: %s",
                exc,
            )
        expected: Optional[int] = None
        try:
            expected = expected_routing_rows(db_path)
        except Exception as exc:  # noqa: BLE001 - defensive
            logger.warning(
                "[data_plane] could not read the fixture's row count: %s", exc,
            )
        try:
            effective = self._effective_declarations()
        except Exception as exc:  # noqa: BLE001 - defensive
            logger.warning(
                "[data_plane] could not read the effective declarations: %s",
                exc,
            )
            effective = self._service_declarations

        fingerprints: Dict[str, Any] = {}
        for name, decl in sorted(effective.items()):
            try:
                port = int(decl.port)
            except (TypeError, ValueError):
                continue
            try:
                fingerprint = probe_data_plane(port, expected_rows=expected)
            except Exception as exc:  # noqa: BLE001 - a probe never raises
                logger.warning(
                    "[data_plane] could not probe service %s on port %s: %s",
                    name, port, exc,
                )
                continue
            if not fingerprint.answered:
                # Not an error: a service that does not speak
                # ``/health/data`` has no data plane to compare, so it
                # is never gated.
                logger.info(
                    "[data_plane] service %s on port %s did not answer "
                    "/health/data (%s); it is not data-plane gated",
                    name, port,
                    fingerprint.error or fingerprint.status or "no answer",
                )
                continue
            if (
                expected is not None
                and fingerprint.routing_rows is not None
                and fingerprint.routing_rows != expected
            ):
                # 2026-10-26 (R2): the instance answered with a count
                # that is not the fixture's — it is serving another
                # database.  Say so **once**, here: the record phase is
                # the only moment that still holds both numbers, and
                # the round must not make every VP rediscover the same
                # contradiction on its own.  One event per service, so
                # the event stays actionable.
                try:
                    self._safe_log_freshness_event(
                        "__preflight__", "data_plane_fixture_mismatch",
                        {
                            "service": name,
                            "db_path": (
                                str(db_path) if db_path is not None else ""
                            ),
                            "expected_rows": expected,
                            "reported_rows": fingerprint.routing_rows,
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - a report never raises
                    logger.warning(
                        "[data_plane] could not report the fixture mismatch "
                        "of service %s: %s", name, exc,
                    )
                # The fingerprint stops carrying the foreign count: a
                # count read from another database can never answer "is
                # this still the data plane I recorded", so every later
                # comparison falls back to the recorded count — the one
                # the instance itself reported.
                fingerprint.expected_rows = None
            fingerprints[name] = fingerprint
            # 2026-10-25: the round now also records **which instance**
            # that is — the ``instance`` block the service publishes
            # about itself.  The fingerprint says "which data plane";
            # the block says "which process".  A service that does not
            # publish one is left alone: nothing to compare, nothing to
            # gate.
            self._record_service_instance_identity(name, port)
        self._data_plane_fingerprints = fingerprints
        if fingerprints:
            self._safe_log_freshness_event(
                "__preflight__", "data_plane_fingerprints",
                {
                    name: fingerprint.to_dict()
                    for name, fingerprint in fingerprints.items()
                },
            )
        return fingerprints

    # ------------------------------------------------------------------
    # The identity of the instance a request is about to reach
    # ------------------------------------------------------------------

    def _record_service_instance_identity(self, name: str, port: int) -> None:
        """Remember which instance answered for *name* on *port*.

        The block is fetched over the very road a VP's request travels.
        A service that publishes none is **forgotten** rather than
        remembered as empty: a stale expectation is worse than none,
        because it would block a VP against an instance the round
        itself has just started.
        """
        key = str(name).lower()
        instance = _health_data_instance(port)
        if instance is None:
            self._service_instance_identity.pop(key, None)
            self._observed_instances.pop(int(port), None)
            return
        self._service_instance_identity[key] = instance
        # 2026-10-25 (repair-r1-31): the block is also the round's
        # **observation** of that address — "who is listening here".
        # The evidence of every VP that talks to the port quotes it,
        # instead of making the service answer a second question.
        self._observed_instances[int(port)] = instance

    def _expected_plans_dirs(self, name: str) -> List[str]:
        """The plans trees this round's service may be serving.

        The **first** entry is the tree the round's own service declared
        about itself when it was started: the round records that
        declaration (the ``instance`` block of ``/health/data``) and a
        service answering with anything else is a different instance.

        The declaration leads and the round's own directories follow
        because the two are not the same question.  A service started
        against a **fixture data plane** — the plan's ``start_cmd``
        pointing ``PDT_PLANS_DIR`` at e.g. ``.pdt/vp_quota/plans`` — is
        a legal deployment: it serves the plans the round gave it,
        honestly, and it says so.  Judging it by where *this round's*
        plan directory happens to live blocked every verification point
        of the 2026-10-27 round at ``identity_mismatch``: the service
        was the round's own, and the gate did not know it.

        The round's ``plan_dir.parent`` and ``plan_dir`` are still
        accepted, so a deployment whose service serves the round's own
        tree keeps working.  A service the round recorded **no**
        declaration for has no first entry; the round's own directories
        are then the whole expectation, exactly as before.
        """
        dirs: List[str] = []
        recorded = self._service_instance_identity.get(str(name).lower()) or {}
        candidates: List[Any] = []
        declared = str(recorded.get("plans_dir") or "")
        if declared:
            candidates.append(declared)
        candidates.extend((self.plan_dir.parent, self.plan_dir))
        for candidate in candidates:
            try:
                resolved = str(Path(candidate).resolve())
            except OSError:  # pragma: no cover - defensive
                resolved = str(candidate)
            if resolved not in dirs:
                dirs.append(resolved)
        return dirs

    def _expected_state_db_inodes(self, name: str) -> List[int]:
        """The state databases this round's service may be reading.

        The **first** entry is the database the round's own service
        declared about itself when it was started — the same source the
        plans tree uses, and for the same reason: a plan whose
        ``start_cmd`` points ``PDT_STATE_DB_PATH`` at a fixture data
        plane is a legal deployment, and judging it by which database
        *this backend* happens to have open blocked the whole
        2026-10-28 round of 20261024-429-plan at ``state_db_inode``.

        The backend's own database follows as a tolerated alternative,
        so a service that serves the round's own data plane keeps
        working.  A service the round recorded no declaration for has
        no first entry; the backend's own inode is then the whole
        expectation, exactly as before.
        """
        inodes: List[int] = []
        recorded = self._service_instance_identity.get(str(name).lower()) or {}
        declared = _instance_state_db_inode(recorded)
        if declared is not None:
            inodes.append(int(declared))
        try:
            from config_paths import resolve_state_db_path

            own = int(resolve_state_db_path().stat().st_ino)
        except Exception:  # noqa: BLE001 - no database, no expectation
            own = None
        if own is not None and own not in inodes:
            inodes.append(own)
        return inodes

    @staticmethod
    def _observed_state_db_inode(instance: Dict[str, Any]) -> Optional[int]:
        """The inode of the database the answering instance reads.

        The instance publishes the inode of **its own** database, and
        that is the authoritative number: the path it names may live
        somewhere this process cannot stat (another container, another
        mount).  The published inode is preferred; the path is only
        stat'ed when the instance did not publish one.

        The body is the module-level helper, so the round's own
        comparison and the serving snapshot cannot drift apart.
        """
        return _instance_state_db_inode(instance)

    def _service_instance_mismatch(
        self, name: str, decl: Any,
    ) -> Optional[Dict[str, Any]]:
        """Is the instance answering for *name* still the round's own?

        ``None`` when it is — or when there is nothing to compare.  A
        service that publishes no ``instance`` block is not gated, and
        neither is a field the service did not answer: "not comparable"
        is not "wrong".

        The three comparisons catch three different ways of being on
        the wrong instance:

          * **boot id** — the round's ``ensure`` recorded the instance
            it started; a different boot id is a different process
            (something restarted the service behind the round's back,
            or a stranger took the port);
          * **plans tree** — the instance serves another tree, so what
            it answers about is not this round's plan;
          * **state database inode** — the same path with another
            inode is another file, i.e. another data plane.
        """
        try:
            port = int(getattr(decl, "port", 0) or 0)
        except (TypeError, ValueError):
            return None
        if not port:
            return None
        actual = _health_data_instance(port)
        if actual is None:
            return None
        return self._compare_service_instance(name, actual, port)

    def _compare_service_instance(
        self, name: str, actual: Dict[str, Any], port: int,
    ) -> Optional[Dict[str, Any]]:
        """Compare the instance that answered with the round's own.

        The body of the comparison, split out of
        :meth:`_service_instance_mismatch` so the round's service
        preflight asks **exactly** the question the dispatch asks and
        gets exactly the same answer — one comparison, one meaning.
        """
        # 2026-10-25 (repair-r1-31): this block was read **right before
        # the request** — it is the freshest statement about who is
        # listening on that port, and the evidence of the VP quotes it
        # as the answering instance.
        self._observed_instances[int(port)] = actual

        recorded = self._service_instance_identity.get(str(name).lower()) or {}
        expected_dirs = self._expected_plans_dirs(name)
        # The first entry is the primary expectation — the plans tree
        # the round's own service declared when it was started (or the
        # round's plans root when it declared none).  That is the value
        # the mismatch record reports as "expected", because it is the
        # one a reader is trying to match against.
        expected_plans_dir = expected_dirs[0] if expected_dirs else ""
        actual_plans_dir = str(actual.get("plans_dir") or "")
        expected_boot_id = str(recorded.get("boot_id") or "")
        actual_boot_id = str(actual.get("boot_id") or "")

        mismatched: List[str] = []
        reasons: List[str] = []

        if expected_boot_id and actual_boot_id and expected_boot_id != actual_boot_id:
            mismatched.append("boot_id")
            reasons.append(
                f"boot id {actual_boot_id!r} instead of {expected_boot_id!r}"
            )

        if actual_plans_dir and not any(
            self._same_path(actual_plans_dir, candidate)
            for candidate in expected_dirs
        ):
            mismatched.append("plans_dir")
            reasons.append(
                f"plans tree {actual_plans_dir!r} instead of "
                f"{expected_plans_dir!r}"
            )

        expected_inodes = self._expected_state_db_inodes(name)
        # The first entry is the primary expectation — the database the
        # round's own service declared (or the backend's own when it
        # declared none) — and it is the number a mismatch record
        # reports as "expected", for the same reason the plans tree
        # reports its own primary entry.
        expected_inode = expected_inodes[0] if expected_inodes else None
        observed_inode = self._observed_state_db_inode(actual)
        if (
            expected_inodes
            and observed_inode is not None
            and observed_inode not in expected_inodes
        ):
            mismatched.append("state_db_inode")
            reasons.append(
                f"state database inode {observed_inode} instead of "
                f"{expected_inode}"
            )

        if not mismatched:
            return None
        return {
            "service": str(name).lower(),
            "port": port,
            "mismatched_fields": mismatched,
            "reasons": reasons,
            "expected_boot_id": expected_boot_id,
            "actual_boot_id": actual_boot_id,
            "expected_plans_dir": expected_plans_dir,
            "actual_plans_dir": actual_plans_dir,
            "expected_state_db_inode": expected_inode,
            "actual_state_db_inode": observed_inode,
        }

    # ------------------------------------------------------------------
    # The round's service preflight (2026-10-25, repair-r1-33)
    # ------------------------------------------------------------------
    #
    # The question "may this round dispatch its verification points at
    # all?" belongs to the verification loop.  The facts the answer
    # needs — the round's services, the address each one is at, and the
    # identity each one is expected to answer with — belong to the
    # agent.  The two meet through a hook: the loop installs it, the
    # agent calls it right after the round-start preflight has brought
    # the services up, and before the first verification point is
    # dispatched.
    #
    # The order is load-bearing.  The services are only there — and
    # only identified — **after** the round's own preflight has
    # ensured them, so a preflight placed before it would compare the
    # round against an instance it has not started yet.

    def set_round_service_preflight(
        self, hook: Optional[Callable[[], Optional[Dict[str, Any]]]],
    ) -> None:
        """Install the round's service preflight.

        *hook* is called once per round, after the round-start
        preflight has brought the services up and before the first
        verification point is dispatched.  It returns ``None`` when the
        round may run, or a failure record (``service``, ``port``,
        ``pid``, ``error_kind``, ``detail``) when it may not.
        """
        self._round_service_preflight = hook

    def _run_round_service_preflight(self) -> Optional[Dict[str, Any]]:
        """Ask the round's preflight whether this round may run.

        ``None`` when no preflight is installed, or when the preflight
        says the round may run.

        A preflight that **could not answer** is not a round that may
        run: the round could not establish that its services are
        usable, and the honest answer to "may this round dispatch its
        verification points" is no.  Failing closed here is what keeps
        a broken gate from turning into a round full of connection
        refusals — which is the very thing this gate exists to stop.
        """
        # ``getattr`` and not a plain attribute: an orchestrator that was
        # built without ``__init__`` (the round tests build one that way)
        # never installed a preflight, and "no preflight" means "this
        # round runs as it always did" — which is what it means.  The
        # same reading this module gives ``_executor_vps_by_id``,
        # ``_vp_dispatch_runner`` and ``verif_repo``.
        hook = getattr(self, "_round_service_preflight", None)
        if hook is None:
            return None
        try:
            failure = hook()
        except Exception as exc:  # noqa: BLE001 - a broken gate blocks
            logger.warning(
                "[verification_preflight] the round's service preflight "
                "raised (%s: %s); the round is blocked",
                type(exc).__name__, exc,
            )
            return {
                "service": "",
                "port": 0,
                "pid": None,
                "error_kind": "preflight_error",
                "detail": (
                    f"the round's service preflight raised: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }
        if failure is None:
            return None
        if not isinstance(failure, dict):
            logger.warning(
                "[verification_preflight] the round's service preflight "
                "returned %s instead of a failure record; the round is blocked",
                type(failure).__name__,
            )
            return {
                "service": "",
                "port": 0,
                "pid": None,
                "error_kind": "preflight_error",
                "detail": (
                    "the round's service preflight returned no usable "
                    "failure record"
                ),
            }
        return failure

    def service_preflight_targets(self) -> List[Dict[str, Any]]:
        """The services this round is about to use, and who they must be.

        One record per service the round is going to talk to:

          * ``service`` — the name;
          * ``port`` — the address the round is bound to;
          * ``pid`` — the process behind that address, when the round
            knows it;
          * ``health_url`` — where to ask whether it is alive;
          * ``expected_identity`` — the ``instance`` block the round
            recorded when it started the service.

        The identity is the *expectation*.  A service that publishes no
        block is never gated: there is nothing to compare, and "not
        comparable" is not "wrong".
        """
        targets: List[Dict[str, Any]] = []
        try:
            effective = self._effective_declarations()
        except Exception:  # pragma: no cover - defensive
            effective = self._service_declarations
        for name, decl in sorted(effective.items()):
            key = str(name).lower()
            try:
                port = int(getattr(decl, "port", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not port:
                continue
            binding = self._service_bindings.get(key)
            pid = (
                getattr(binding, "pid", None)
                if binding is not None
                else None
            )
            if pid is None:
                runtimes = (
                    getattr(self._service_runtime_map, "runtimes", None) or {}
                )
                runtime = runtimes.get(key)
                if runtime is not None:
                    pid = getattr(runtime, "pid", None)
            health_url = str(getattr(decl, "health_url", "") or "")
            if not health_url:
                health_url = f"http://127.0.0.1:{port}/health"
            targets.append({
                "service": key,
                "port": port,
                "pid": pid,
                "health_url": health_url,
                "expected_identity": dict(
                    self._service_instance_identity.get(key) or {},
                ),
            })
        return targets

    def service_declaration(self, name: str) -> Optional[Any]:
        """The declaration of the service *name*, as this round runs it.

        The **effective** declaration: the plan's, with the port the
        round's own instance really listens on.  The round-level gate
        in the verification loop asks about a service by name, and this
        is where the name is turned into the declaration the shared
        precondition needs — so the loop and the per-verification-point
        gate look at the same address.
        """
        key = str(name or "").lower()
        try:
            effective = self._effective_declarations()
        except Exception:  # pragma: no cover - defensive
            effective = self._service_declarations
        return effective.get(key) or self._service_declarations.get(key)

    def reensure_preflight_service(
        self, name: str, port: int,
    ) -> Dict[str, Any]:
        """Bring one service back, and say whether it came.

        The round's **one** attempt at a service that is not there: the
        project is asked to start it once more, and the result goes
        through the same adoption gates a per-VP recovery uses — a
        runtime that says the service is ready is not enough, the port
        has to accept a connection and the health URL has to answer.

        Returns ``{"restored": bool, "port": int, "pid": int | None,
        "detail": str}``.
        """
        key = str(name).lower()
        try:
            effective = self._effective_declarations()
        except Exception:  # pragma: no cover - defensive
            effective = self._service_declarations
        decl = effective.get(key) or self._service_declarations.get(key)
        if decl is None:
            return {
                "restored": False,
                "port": int(port),
                "pid": None,
                "detail": f"the round has no declaration for service {key!r}",
            }
        try:
            runtime_map = self._ensure_declared_services({key: decl})
        except Exception as exc:  # noqa: BLE001 - a failed re-ensure blocks
            return {
                "restored": False,
                "port": int(port),
                "pid": None,
                "detail": (
                    f"the re-ensure of {key!r} raised: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }
        verdicts = self._adopt_reensure_runtimes("__preflight__", runtime_map)
        verdict = verdicts.get(key) or {}
        runtime = verdict.get("runtime")
        if not verdict.get("accepted"):
            return {
                "restored": False,
                "port": int(port),
                "pid": None,
                "detail": str(
                    verdict.get("detail") or "the service did not come back",
                ),
            }
        return {
            "restored": True,
            "port": int(getattr(runtime, "port", port) or port),
            "pid": getattr(runtime, "pid", None),
            "detail": "",
        }

    def probe_service_liveness(self, port: int) -> bool:
        """Does a TCP connection to *port* succeed?"""
        return _tcp_reachable(int(port))

    def probe_service_health(self, health_url: str) -> bool:
        """Does *health_url* answer 200 when asked with the request header?

        The header is the one a verification point's own request carries,
        so the probe travels the same road the request will travel: a
        service that would refuse that request is refused here too.
        """
        if not health_url:
            return True
        return _health_url_answers(health_url)

    def probe_service_instance(
        self, port: int,
    ) -> Optional[Dict[str, Any]]:
        """The ``instance`` block the service on *port* publishes.

        ``None`` when the service does not answer, does not answer with
        JSON, or answers without an ``instance`` block.
        """
        return _health_data_instance(int(port))

    def service_identity_mismatch(
        self, name: str, port: int,
        observed: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Is the instance answering on *port* still the round's own?

        ``None`` when it is — or when there is nothing to compare.

        *observed* is the block the caller already read; when it is
        ``None`` the block is read here.
        """
        try:
            port = int(port)
        except (TypeError, ValueError):
            return None
        if not port:
            return None
        if observed is None:
            observed = _health_data_instance(port)
        if observed is None:
            return None
        return self._compare_service_instance(name, observed, port)

    # ------------------------------------------------------------------
    # The round's service precondition (2026-10-26)
    # ------------------------------------------------------------------
    #
    # The round has **one** precondition, and both the ``api_test`` path
    # and the ``ui_validation`` path go through it (see
    # :meth:`ensure_vp_services_ready`).  The round-level gate the
    # verification loop installs asks the same question through the
    # same function (see ``verification_loop._run_service_preflight``).
    #
    # The 2026-10-26 round had three answers to "is the service there?"
    # — the ensure said yes, the freshness check said yes (about the
    # binary, not the port), and the connection said no.  Twelve
    # verification points were graded on the connection.

    def _round_service_token(self) -> str:
        """The token this round's services are recorded under.

        The round's service-owner token: the same token the lease
        carries, the same one the ledger stamps the services with.
        ``""`` when the plan has no live owner — the ledger then
        records the service without one, which is what let the reap
        sweep stop a service the round was still using.
        """
        try:
            service_manager = _load_service_manager(self.project_dir)
            return str(
                service_manager.current_service_owner(Path(self.plan_dir)) or ""
            )
        except Exception:  # noqa: BLE001 - a token is not worth a failure
            return ""

    def _start_service_for_round(self, decl: Any) -> Dict[str, Any]:
        """Bring the service up through the round's own start path.

        The same machinery the round-start preflight uses (the
        **project's** ``service_manager``), narrowed to this one
        service, and the same adoption gates a per-VP recovery uses: a
        runtime that says "ready" is not adopted until the port accepts
        a connection and the health URL answers.

        The adoption is what moves the round's **binding** — the
        address the verification points are dispatched to — so a
        service that came back on another port is reached on that port
        and not on the one that died.
        """
        key = str(getattr(decl, "name", "") or "").lower()
        try:
            runtime_map = self._ensure_declared_services({key: decl})
        except Exception as exc:  # noqa: BLE001 - a failed start is a fact
            return {
                "ok": False,
                "port": int(getattr(decl, "port", 0) or 0),
                "pid": None,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        verdicts = self._adopt_reensure_runtimes("__preflight__", runtime_map)
        self._refresh_resolved_service_placeholders()
        verdict = verdicts.get(key) or {}
        runtime = verdict.get("runtime")
        if not verdict.get("accepted"):
            return {
                "ok": False,
                "port": int(getattr(decl, "port", 0) or 0),
                "pid": None,
                "detail": str(
                    verdict.get("detail") or "the service did not come back"
                ),
            }
        port = int(getattr(runtime, "port", 0) or 0)
        return {
            "ok": True,
            "port": port,
            "pid": getattr(runtime, "pid", None),
            "detail": "",
            # The adoption gates already asked the service itself; the
            # precondition reuses that answer instead of asking again.
            "health_record": self._service_self_check_records.get(
                (key, port), {},
            ),
        }

    def _service_health_check(self, decl: Any, port: int) -> Dict[str, Any]:
        """Ask the service whether it is alive, over the guarded road.

        The declaration's own health URL — moved to the port the service
        really answers on — asked with the request guard's header.  A
        declaration that names no health URL is asked nothing: the
        plan's convention is that such a service is judged by TCP
        reachability alone.
        """
        name = str(getattr(decl, "name", "") or "").lower()
        url = _service_health_url(decl, int(port))
        if not url:
            url = self._runtime_health_url(name, int(port))
        if not url:
            return _service_self_check("")
        record = _service_self_check(url)
        self._service_self_check_records[(name, int(port))] = record
        return record

    def ensure_service_ready(
        self, decl: Any, plan_dir: Optional[Path] = None,
        round_token: str = "",
    ) -> Dict[str, Any]:
        """The round's own service precondition.

        The round-flavoured wrapper around
        :func:`ensure_service_ready`: it supplies the project, the
        round number, the plan and the **project's** service manager,
        so the service is started by the same code the project ships —
        and the start goes through the round's adoption gates, so the
        round's binding follows the service when it comes back
        somewhere else.

        See :func:`ensure_service_ready` for the three steps and for
        the shape of the returned record.
        """
        token = round_token or self._round_service_token()
        if round_token:
            # The round's own token owns the services it starts — and
            # owns them in the ledger, so the reap sweep leaves them
            # alone.  Claimed only when nobody else holds the plan.
            _claim_round_token(
                _load_service_manager(self.project_dir),
                Path(self.plan_dir), round_token,
            )
        return ensure_service_ready(
            decl,
            Path(plan_dir) if plan_dir is not None else Path(self.plan_dir),
            token,
            project_dir=Path(self.project_dir),
            round_number=self._current_round,
            plan_id=self.plan_dir.name,
            service_manager=_load_service_manager(self.project_dir),
            starter=self._start_service_for_round,
            liveness=self._service_port_is_live,
            health=self._service_health_check,
        )

    async def ensure_vp_services_ready(
        self, vp_id: str, vp: dict,
        verified: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """The one precondition every verification point shares.

        Both the ``api_test`` path and the ``ui_validation`` path come
        through here, before the point is dispatched.  A path that had
        a check of its own is a path that can disagree with the other
        one — and that disagreement is what cost the 2026-10-26 round
        its twelve verification points.

        *verified* carries what the round's own recovery has already
        established for this dispatch — the services it brought back
        and asked over the guarded road.  Those answers are reused:
        asking the same service a second time would be a second check,
        and one check is the point.

        Returns a BLOCKED verdict when the service the point is about
        to talk to is not serving, or ``None`` when the point may be
        dispatched.  The verdict is BLOCKED and never FAILED: a refused
        connection is a statement about the plumbing, not about the
        product.
        """
        targets = self._vp_service_targets(vp)
        if not targets:
            return None
        already = verified or {}
        blocked: List[Tuple[str, Any, Dict[str, Any]]] = []
        for key, decl in sorted(targets.items()):
            reused = already.get(key)
            if isinstance(reused, dict) and reused.get("ready"):
                self._safe_log_freshness_event(
                    vp_id, "service_ready",
                    {
                        "service": key,
                        "port": reused.get("port"),
                        "status_code": reused.get("status_code"),
                        "content_type": reused.get("content_type"),
                        "body_status": reused.get("body_status"),
                        "reused": True,
                    },
                )
                continue
            outcome = await asyncio.to_thread(self.ensure_service_ready, decl)
            if outcome.get("ready"):
                self._safe_log_freshness_event(
                    vp_id, "service_ready",
                    {
                        "service": key,
                        "port": outcome.get("port"),
                        "status_code": outcome.get("status_code"),
                        "content_type": outcome.get("content_type"),
                        "body_status": outcome.get("body_status"),
                    },
                )
                continue
            blocked.append((key, decl, outcome))
        if not blocked:
            return None

        ports: List[int] = []
        for key, decl, outcome in blocked:
            port = int(outcome.get("port") or getattr(decl, "port", 0) or 0)
            detail = str(outcome.get("detail") or "")
            if port:
                self._block_service_port(key, port, vp_id, None, detail=detail)
                ports.append(port)
            self._safe_log_freshness_event(
                vp_id, "service_not_ready",
                {
                    "service": key,
                    "port": port,
                    "error_kind": outcome.get("error_kind"),
                    "detail": detail,
                },
            )
        return self._build_service_freshness_blocked_verdict(
            vp_id, sorted(set(ports)),
        )

    def _build_preflight_blocked_report(
        self, failure: Dict[str, Any],
    ) -> Dict[str, Any]:
        """The report of a round that could not start.

        A round that dispatched nothing has measured nothing about the
        product, so it has no verdict to give: the report says
        ``BLOCKED`` and carries **no** verification results — not an
        empty ``FAILED``, not a skipped one.  A FAILED verdict would
        send the repair chain after a product nobody ever looked at.
        """
        failure = failure or {}
        service = str(failure.get("service") or "")
        port = failure.get("port")
        error_kind = str(failure.get("error_kind") or "")
        detail = str(failure.get("detail") or "")
        where = f"the service {service!r}" if service else "the round's services"
        if port:
            where = f"{where} on port {port}"
        summary = (
            f"The round dispatched no verification point: {where} was not "
            f"usable ({error_kind or 'unknown reason'}). {detail}"
        ).strip()
        return {
            "overall_status": "BLOCKED",
            "verification_results": [],
            "requirement_deviations": [],
            "summary": summary,
            "stop_reason": "service_preflight_failed",
            "service_preflight_failed": {
                "service": service,
                "port": int(port) if port else 0,
                "pid": failure.get("pid"),
                "error_kind": error_kind,
                "detail": detail,
            },
            "execution_profile": {"mode": "service_preflight_blocked"},
        }

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        """Do two paths name the same place?

        Resolved on both sides, because one of them may reach the same
        directory through a symlink (``/tmp`` → ``/private/tmp`` on
        macOS) — and the round's plan directory and the service's
        plans root have to be recognised as the same tree.
        """
        if not left or not right:
            return False
        try:
            return Path(left).resolve() == Path(right).resolve()
        except OSError:  # pragma: no cover - defensive
            return str(left) == str(right)

    def _build_service_instance_mismatch_verdict(
        self, vp_id: str, mismatch: Dict[str, Any],
    ) -> dict:
        """The verdict for a VP whose service answered as another instance.

        The request is **not** sent: a response from somebody else's
        instance is a confident verdict about somebody else's data.
        And the verdict is BLOCKED, never FAILED — FAILED would say
        "the project answered wrong", and the project was never asked.

        The evidence carries both sides of every comparison — the
        identity the round expected and the identity that answered —
        so a reader can tell "the service was restarted behind the
        round's back" from "the service serves another tree".
        """
        service = str(mismatch.get("service") or "")
        reasons = "; ".join(mismatch.get("reasons") or [])
        evidence: Dict[str, Any] = {
            "error_kind": SERVICE_INSTANCE_MISMATCH,
            "reason_code": SERVICE_INSTANCE_MISMATCH,
            "service": service,
            "expected_boot_id": mismatch.get("expected_boot_id"),
            "actual_boot_id": mismatch.get("actual_boot_id"),
            "expected_plans_dir": mismatch.get("expected_plans_dir"),
            "actual_plans_dir": mismatch.get("actual_plans_dir"),
            "mismatched_fields": list(mismatch.get("mismatched_fields") or []),
        }
        return {
            "status": "BLOCKED",
            "reasons": [blocked_reason(
                f"{SERVICE_INSTANCE_MISMATCH} — the instance answering for "
                f"service {service!r} on port {mismatch.get('port')} is not "
                f"the instance this round started ({reasons})"
            )],
            "actual_result": (
                f"{SERVICE_INSTANCE_MISMATCH}: the service {service!r} on "
                f"port {mismatch.get('port')} answered as another instance "
                f"(boot id {mismatch.get('actual_boot_id') or 'unknown'} "
                f"instead of {mismatch.get('expected_boot_id') or 'unknown'}; "
                f"plans tree {mismatch.get('actual_plans_dir') or 'unknown'} "
                f"instead of {mismatch.get('expected_plans_dir') or 'unknown'}) "
                f"— so the request was not sent. This verification point is "
                f"BLOCKED: the project was never asked."
            ),
            "evidence": evidence,
            "framework_checks": [],
        }

    async def _probe_data_plane(self, decl: Any, recorded: Any) -> Any:
        """Ask the service on *decl*'s port which data plane it serves.

        ``None`` when the service cannot be asked at all — the same
        thing the fingerprint records as "no answer", and equally a
        reason to treat the instance as lost.
        """
        from service_freshness import probe_data_plane

        try:
            port = int(decl.port)
        except (TypeError, ValueError):
            return None
        try:
            return await asyncio.to_thread(
                probe_data_plane, port,
                expected_rows=getattr(recorded, "expected_rows", None),
            )
        except Exception as exc:  # noqa: BLE001 - a probe never raises
            logger.warning(
                "[data_plane] probe on port %s errored: %s", port, exc,
            )
            return None

    async def _guard_api_test_data_plane(
        self, vp_id: str, vp: dict,
    ) -> Optional[dict]:
        """Is the instance this VP is about to talk to still the round's?

        Returns a BLOCKED verdict when the answer is no, ``None`` when
        the VP may go out.  The check happens **before** the request: a
        request sent to the wrong instance measures the wrong database,
        and the verdict it produces is worse than no verdict at all —
        it is a confident statement about somebody else's data.

        The rule the check enforces:

          * the instance must answer ``/health/data`` at all (a service
            that stopped answering is lost, whatever its port says);
          * the answer must be the fixture's — the same number of
            ``plan_routing`` rows the round recorded;
          * a service that does not speak ``/health/data`` is not
            gated at all (there is no data plane to compare).
        """
        if not self._data_plane_fingerprints:
            return None
        try:
            targets = self._vp_service_targets(vp)
        except Exception as exc:  # noqa: BLE001 - defensive
            logger.warning(
                "[data_plane] vp=%s could not resolve the VP's services: %s; "
                "proceeding", vp_id, exc,
            )
            return None

        for name, decl in sorted(targets.items()):
            recorded = self._data_plane_fingerprints.get(name)
            if recorded is None:
                continue
            observed = await self._probe_data_plane(decl, recorded)
            if recorded.matches(observed):
                if observed is not None and observed.port != recorded.port:
                    # The service moved (a liveness recovery relocated
                    # it): the fingerprint follows it to its new address,
                    # so the pid check keeps working for the rest of
                    # the round.
                    self._data_plane_fingerprints[name] = observed
                # 2026-10-25: the data plane is the round's — but is the
                # **instance**?  The data plane says "which data"; the
                # instance block says "which process".  A service that
                # serves the right fixture from another process (a
                # stranger that took the port, or a restart behind the
                # round's back) would pass the fingerprint check and
                # still be the wrong instance to ask.
                mismatch = self._service_instance_mismatch(name, decl)
                if mismatch is not None:
                    self._safe_log_freshness_event(
                        vp_id, "service_instance_mismatch",
                        {
                            "service": name,
                            "expected_boot_id": mismatch.get(
                                "expected_boot_id",
                            ),
                            "actual_boot_id": mismatch.get("actual_boot_id"),
                            "expected_plans_dir": mismatch.get(
                                "expected_plans_dir",
                            ),
                            "actual_plans_dir": mismatch.get(
                                "actual_plans_dir",
                            ),
                            "mismatched_fields": list(
                                mismatch.get("mismatched_fields") or [],
                            ),
                        },
                    )
                    logger.warning(
                        "[service_freshness] vp=%s: service %r answered as "
                        "another instance (%s)", vp_id, name,
                        "; ".join(mismatch.get("reasons") or []),
                    )
                    return self._build_service_instance_mismatch_verdict(
                        vp_id, mismatch,
                    )
                continue
            self._safe_log_freshness_event(
                vp_id, "data_plane_mismatch",
                {
                    "service": name,
                    "recorded": recorded.to_dict(),
                    "observed": observed.to_dict() if observed else None,
                },
            )
            logger.warning(
                "[data_plane] vp=%s: service %r no longer serves the "
                "round's fixture (recorded %s, observed %s)",
                vp_id, name, recorded.to_dict(),
                observed.to_dict() if observed else None,
            )
            recovered, restored = await self._recover_data_plane_instance(
                vp_id, name, decl, recorded,
            )
            if restored and recovered is not None:
                # The instance is back and serving the fixture: the
                # round's fingerprint follows it to its new address.
                self._data_plane_fingerprints[name] = recovered
                self._safe_log_freshness_event(
                    vp_id, "data_plane_reensure_ready",
                    {"service": name, "fingerprint": recovered.to_dict()},
                )
                continue
            return self._build_data_plane_blocked_verdict(
                vp_id, name, recorded, observed, recovered,
            )
        return None

    async def _recover_data_plane_instance(
        self, vp_id: str, name: str, decl: Any, recorded: Any,
    ) -> Tuple[Any, bool]:
        """Bring the lost instance back — once — and re-probe it.

        Returns ``(fingerprint, restored)``.  ``fingerprint`` is the
        observation of the service after the re-ensure (``None`` when
        it could not be asked); ``restored`` says whether that
        observation is the round's data plane again.

        The re-ensure goes through the same machinery the preflight
        uses (the **project's** ``service_manager``), so the service
        may end up on a **new port** — the declarations and the plan's
        resolved placeholders are refreshed to follow it, and the
        fingerprint is taken at the address the service really serves.
        """
        if name in self._data_plane_reensure_spent:
            # The round already spent its one re-ensure on this
            # service: the VPs after this one go straight to BLOCKED
            # rather than each starting a copy of it.
            return None, False
        self._data_plane_reensure_spent.add(name)

        try:
            runtime_map = await asyncio.to_thread(
                self._ensure_declared_services, {name: decl},
            )
        except Exception as exc:  # noqa: BLE001 — a failed start is a
            # failed start, not a reason to let the VP fail
            logger.warning(
                "[data_plane] vp=%s: re-ensuring service %r errored: %s",
                vp_id, name, exc,
            )
            runtime_map = None
        # 2026-10-25 (repair-r1-24): the re-ensure's result is
        # **verified** before it is adopted.  A result that names a port
        # nobody serves is not an instance, and the round must not move
        # its address there.
        verdicts = self._adopt_reensure_runtimes(vp_id, runtime_map)
        verdict = verdicts.get(name)
        if verdict is not None and not verdict.get("accepted"):
            detail = str(verdict.get("detail") or "")
            runtime = verdict.get("runtime")
            self._block_service_port(
                name,
                self._reensure_failed_port(name, runtime, decl),
                vp_id, runtime, detail=detail,
            )
            self._safe_log_freshness_event(
                vp_id, "data_plane_reensure_failed",
                {
                    "service": name,
                    "reason_code": SERVICE_UNREACHABLE,
                    "detail": detail,
                },
            )
            return None, False
        # The service may have moved: the declarations and the plan's
        # resolved placeholders have to follow it.
        self._refresh_resolved_service_placeholders()
        effective = self._effective_declarations().get(name, decl)
        fingerprint = await self._probe_data_plane(effective, recorded)
        if fingerprint is None or not fingerprint.answered:
            self._safe_log_freshness_event(
                vp_id, "data_plane_reensure_failed",
                {
                    "service": name,
                    "detail": "the service did not answer /health/data "
                              "after the re-ensure",
                },
            )
            return fingerprint, False

        expected = getattr(recorded, "expected_rows", None)
        if expected is not None:
            restored = fingerprint.routing_rows == expected
        else:
            # No row count to compare: the instance the round just
            # started is the one that answers, and it is up.
            restored = self._service_runtime_ready(name)
        if not restored:
            self._safe_log_freshness_event(
                vp_id, "data_plane_reensure_failed",
                {
                    "service": name,
                    "fingerprint": fingerprint.to_dict(),
                    "detail": "the instance came back with another data plane",
                },
            )
        return fingerprint, restored

    def _service_runtime_ready(self, name: str) -> bool:
        """Is the service *name* up, as the round's runtime map says?"""
        runtime_map = self._service_runtime_map
        if runtime_map is None:
            return False
        runtime = (getattr(runtime_map, "runtimes", None) or {}).get(name)
        return bool(getattr(runtime, "ready", False))

    def _build_data_plane_blocked_verdict(
        self, vp_id: str, service: str, recorded: Any,
        observed: Any, recovered: Any,
    ) -> dict:
        """The verdict for a VP whose service stopped serving the fixture.

        The request is **not** sent: a response from the wrong instance
        would be a confident verdict about somebody else's data.  And
        the verdict is BLOCKED, never FAILED — FAILED would say "the
        project answered wrong", and the project was never asked.

        The observed fingerprint travels with the verdict: without it
        nobody can tell "the instance was replaced" from "the instance
        serves another database".

        The verdict also carries a **reason code** — the code the
        environment accounting matches on, so that the block is counted
        as an environment block and the round can be re-run rather than
        read as a contract violation.  Which code depends on why the
        block happened: ``service_unreachable`` when the round could not
        reach the service at all, ``service_data_plane_mismatch`` when
        the service answered with another data plane.
        """
        recorded_dict = recorded.to_dict() if recorded is not None else None
        observed_dict = observed.to_dict() if observed is not None else None
        recovered_dict = recovered.to_dict() if recovered is not None else None
        evidence: Dict[str, Any] = {
            "data_plane_service": service,
            "recorded_data_plane": recorded_dict,
            "observed_data_plane": observed_dict,
        }
        if recovered_dict is not None:
            evidence["recovered_data_plane"] = recovered_dict
        recorded_rows = (
            recorded_dict.get("routing_rows") if recorded_dict else None
        )
        observed_rows = (
            observed_dict.get("routing_rows") if observed_dict else None
        )
        # 2026-10-25 (repair-r1-24): the re-ensure may have failed for a
        # sharper reason than "the data plane did not come back" — the
        # round could not **reach** the service at all.  The verdict has
        # to say which one it is.
        unreachable = {
            port: text
            for port, text in (
                getattr(self, "_service_unreachable", None) or {}
            ).items()
            if recorded is not None and port == int(recorded.port)
        }
        if unreachable:
            detail = "; ".join(
                f"port {port}: {text}"
                for port, text in sorted(unreachable.items())
            )
            evidence["reason_code"] = SERVICE_UNREACHABLE
            return {
                "status": "BLOCKED",
                "reasons": [
                    blocked_reason(
                        f"{SERVICE_UNREACHABLE} — the round could not reach "
                        f"service {service!r} after the re-ensure ({detail})"
                    )
                ],
                "actual_result": (
                    f"{SERVICE_UNREACHABLE}: the re-ensure of service "
                    f"{service!r} did not produce an instance the round could "
                    f"reach, so the api_test request was not sent. detail: "
                    f"{detail}"
                ),
                "evidence": evidence,
                "framework_checks": [],
            }
        # 2026-10-27 (R3): the block has to say **why** in a code the
        # accounting layer matches on.  The unreachable branch above
        # already writes ``service_unreachable``; this branch was the
        # one that forgot, so its BLOCKED entry was invisible to the
        # environment accounting: the round was not marked
        # environment-blocked and the block was not counted as one.
        evidence["reason_code"] = SERVICE_DATA_PLANE_MISMATCH
        return {
            "status": "BLOCKED",
            "reasons": [
                blocked_reason(
                    f"service {service!r} no longer serves the round's "
                    f"fixture (recorded {recorded_rows} routing rows, "
                    f"observed {observed_rows})"
                )
            ],
            "actual_result": (
                f"data-plane identity check BLOCKED this VP: the instance "
                f"behind service {service!r} answered with a different data "
                f"plane than the one this round recorded, and one re-ensure "
                f"did not restore it. The api_test request was not sent."
            ),
            "evidence": evidence,
            "framework_checks": [],
        }

    # ------------------------------------------------------------------
    # Service liveness — per-VP recovery (2026-10-24)
    # ------------------------------------------------------------------
    #
    # The round-start preflight brings the declared services up **once**.
    # A service that dies after that stays dead for the rest of the
    # round: every VP that talks to it is graded on the connection
    # refusal, and ``api_test`` reports that refusal as FAILED — which
    # reads as "the project is broken" when the truth is "the plumbing
    # died".  One dead service cost a production round twelve VPs.
    #
    # So the per-VP runner probes the ports it is about to use and
    # brings a dead one back before the VP runs.  What it must not do
    # is restart a service that is alive: a restart re-seeds the
    # fixture and clears the execution state, so a round that
    # restarts its service per VP destroys the very state its VPs are
    # verifying.

    def _service_reensure_lock_for_round(self) -> "asyncio.Lock":
        """The lock that keeps N concurrent VPs from starting N copies
        of the same service.

        One lock per **loop**: a round runs its VPs on one event loop
        and a lock belongs to the loop it was first awaited on, so a
        new round (new loop) gets a new lock rather than an error.
        """
        loop = asyncio.get_running_loop()
        if (
            self._service_reensure_lock is None
            or self._service_reensure_lock_loop is not loop
        ):
            self._service_reensure_lock = asyncio.Lock()
            self._service_reensure_lock_loop = loop
        return self._service_reensure_lock

    def _vp_service_targets(self, vp: dict) -> Dict[str, Any]:
        """The declared services this VP depends on, keyed by name.

        Two ways a VP names a service, and both are honoured:

          * ``uses_services`` — the plan's explicit list of service
            names;
          * a **port literal** in the VP's text that belongs to a
            declared service — the ``{{svc.*}}`` placeholder was
            already expanded, so the address the VP will actually hit
            is in the text.

        A VP that names no declared service is not this method's
        business: it must not drag an unrelated service back to life.
        """
        if not self._service_declarations:
            return {}
        effective = self._effective_declarations()
        targets: Dict[str, Any] = {}

        uses = vp.get("uses_services")
        if isinstance(uses, list):
            for name in uses:
                key = str(name).lower()
                decl = effective.get(key)
                if decl is not None:
                    targets[key] = decl

        try:
            from service_freshness import (
                PROTECTED_PORTS,
                extract_service_ports,
            )
            ports = set(extract_service_ports(
                {"verification_points": [vp]}, self.project_dir,
            ))
        except Exception as exc:  # noqa: BLE001 — best effort
            logger.debug(
                "[service_freshness] could not read the VP's ports: %s", exc,
            )
            return targets
        for key, decl in effective.items():
            try:
                port = int(decl.port)
            except (TypeError, ValueError):
                continue
            # A backend-runtime port is never a service of the
            # project's — it is the orchestrator itself.
            if port in PROTECTED_PORTS:
                continue
            if port in ports:
                targets[key] = decl
        # 2026-10-25 (repair-r1-29): a service the round could not place
        # is still worth **probing**: the process that held the declared
        # port may be gone, and then the service can be placed there.
        # This is a probe, not an address — the declaration becomes the
        # effective one only if the re-ensure really produced an
        # instance.
        named = _vp_service_names(vp)
        for key, info in self._unusable_services().items():
            if key in targets:
                continue
            decl = self._service_declarations.get(key)
            if decl is None:
                continue
            if key not in named and info["port"] not in ports:
                continue
            targets[key] = decl
        return targets

    async def _recover_declared_services(
        self, vp_id: str, vp: dict,
    ) -> Dict[str, Dict[str, Any]]:
        """Make the services this VP depends on alive before it runs.

        The round-start preflight starts the declared services once
        (see :meth:`_preflight_declared_services`); this is the
        recovery for a service that dies *after* that.  A dead port is
        brought back through ``service_manager.ensure_services`` — the
        same machinery the preflight uses, and the **project's** copy
        of it (see :func:`_load_service_manager`) — and the plan's
        ``{{svc.*}}`` placeholders are re-resolved against the
        service's new address.

        Three properties this has to keep:

          * **Only the dead ones are re-ensured.**  ``ensure_services``
            is called with the dead services alone, never with the
            whole declaration set: a healthy service must not be
            restarted, and ``ensure_services`` is free to restart a
            service it considers stale.
          * **One attempt per service per round.**  A service that
            could not be brought back is remembered, so the VPs after
            it go straight to the BLOCKED gate instead of each paying
            for another failed start.
          * **One start at a time.**  The VPs of a round run
            concurrently; without the lock, every VP that sees the
            dead port would start its own copy of the service.

        Returns what the recovery **established**: the services it
        brought back, with the answer the adoption gates got from the
        service itself.  The one precondition reuses those answers
        instead of asking the same service a second time.

        Never raises: a recovery that fails must not take the round
        down — the blocked-port gate is what turns an unusable service
        into a BLOCKED verdict.
        """
        if not self._service_declarations:
            return {}
        try:
            return await self._recover_declared_services_inner(vp_id, vp)
        except Exception as exc:  # noqa: BLE001 — recovery never aborts a VP
            logger.warning(
                "[service_freshness] vp=%s service recovery errored: %s; "
                "proceeding", vp_id, exc,
            )
            return {}

    async def _recover_declared_services_inner(
        self, vp_id: str, vp: dict,
    ) -> Dict[str, Dict[str, Any]]:
        """The body of :meth:`_recover_declared_services`."""
        from service_freshness import probe_port

        verified: Dict[str, Dict[str, Any]] = {}
        targets = self._vp_service_targets(vp)
        if not targets:
            return verified

        dead: Dict[str, Any] = {}
        for key, decl in targets.items():
            if key in self._service_reensure_failed:
                continue
            probe = await asyncio.to_thread(probe_port, int(decl.port))
            if not getattr(probe, "listening", False):
                dead[key] = decl
        if not dead:
            return verified

        async with self._service_reensure_lock_for_round():
            # Re-probe under the lock: another VP may have brought the
            # service back while this one was waiting for the lock.
            still_dead: Dict[str, Any] = {}
            for key, decl in dead.items():
                if key in self._service_reensure_failed:
                    continue
                probe = await asyncio.to_thread(probe_port, int(decl.port))
                if not getattr(probe, "listening", False):
                    still_dead[key] = decl
            if not still_dead:
                return verified

            logger.warning(
                "[service_freshness] vp=%s: declared service(s) %s are not "
                "listening; bringing them back up",
                vp_id, sorted(still_dead),
            )
            try:
                runtime_map = await asyncio.to_thread(
                    self._ensure_declared_services, still_dead,
                )
            except Exception as exc:  # noqa: BLE001 — a failed start is
                # a failed start, not a reason to let the VP fail
                logger.warning(
                    "[service_freshness] vp=%s: bringing service(s) %s back "
                    "up errored: %s", vp_id, sorted(still_dead), exc,
                )
                runtime_map = None
            # 2026-10-25 (repair-r1-24): the re-ensure's result is
            # **verified** before it is adopted.  A runtime that says
            # "ready" on a port nobody listens on is not a service, and
            # adopting it would point the round's VPs at a dead address
            # — which is exactly what the production round did.
            verdicts = self._adopt_reensure_runtimes(vp_id, runtime_map)
            self._refresh_resolved_service_placeholders()

            for key, decl in still_dead.items():
                verdict = verdicts.get(key, {})
                runtime = verdict.get("runtime")
                if verdict.get("accepted"):
                    port = int(getattr(runtime, "port"))
                    # It is back — and if the preflight had blocked the
                    # port, it is not blocked any more.
                    self._unblock_service_port(key, port)
                    # The adoption gates asked the service itself; the
                    # answer is the one the precondition reuses.
                    asked = self._service_self_check_records.get(
                        (key, port), {},
                    )
                    verified[key] = {
                        "ready": True,
                        "service": key,
                        "port": port,
                        "pid": getattr(runtime, "pid", None),
                        "status_code": asked.get("status_code"),
                        "content_type": str(asked.get("content_type") or ""),
                        "body_status": str(asked.get("body_status") or ""),
                    }
                    self._safe_log_freshness_event(
                        vp_id, "service_reensure_ready",
                        {"service": key, "port": port},
                    )
                    continue
                self._service_reensure_failed.add(key)
                port = self._reensure_failed_port(key, runtime, decl)
                self._block_service_port(
                    key, port, vp_id, runtime,
                    detail=str(verdict.get("detail") or ""),
                )
                self._safe_log_freshness_event(
                    vp_id, "service_reensure_failed",
                    {
                        "service": key,
                        "port": port,
                        "reason_code": SERVICE_UNREACHABLE,
                        "detail": str(
                            verdict.get("detail")
                            or getattr(runtime, "detail", "") or ""
                        ),
                    },
                )
        return verified

    def _ensure_declared_services(self, declarations: Dict[str, Any]) -> Any:
        """Bring *declarations* back up through the project's own
        ``service_manager``.

        The same call the round-start preflight makes (same module, same
        arguments, same ownership recording), narrowed to the services
        that are actually down — a healthy service must not be touched.
        """
        service_manager = _load_service_manager(self.project_dir)
        runtime_map = service_manager.ensure_services(
            self.project_dir,
            declarations,
            round_number=self._current_round,
        )
        record_runtimes = getattr(service_manager, "record_runtimes", None)
        if record_runtimes is not None:
            record_runtimes(
                self.plan_dir,
                self.plan_dir.name,
                runtime_map,
                project_dir=self.project_dir,
                round_number=self._current_round,
            )
        return runtime_map

    # ------------------------------------------------------------------
    # The adoption gates of a re-ensure's result (2026-10-25, repair-r1-24)
    # ------------------------------------------------------------------
    #
    # A re-ensure reports what it *thinks* it produced.  The production
    # round's re-ensure reported a service that was **ready**, on a port
    # (51725) nothing had ever listened on, and the round adopted the
    # address without asking.  The VPs then pointed at it and were graded
    # on the connection refusal.
    #
    # So the result has to be **verified** before it is adopted:
    #
    #   1. the runtime says the service is ready and carries a port;
    #   2. that port accepts a TCP connection;
    #   3. the service's health URL answers 200.
    #
    # A result that fails any of them leaves the old map alone, does not
    # move the address, and blocks the VPs that need the service with the
    # ``service_unreachable`` code.

    def _service_reensure_gates(
        self, name: str, runtime: Any,
    ) -> Tuple[bool, str]:
        """The three gates a re-ensure's result has to pass.

        Returns ``(passed, detail)``; *detail* says which gate refused
        and why — it is the text the blocked verdict carries.
        """
        if runtime is None:
            return False, "the re-ensure reported no runtime for the service"
        if _runtime_is_unusable(runtime):
            # 2026-10-25 (repair-r1-29): the re-ensure could not place
            # the service, and says so.  The port it names is the
            # declared one — somebody else's — so it is not an address
            # and the round does not move there.
            reason = str(
                getattr(runtime, "failure_reason", "")
                or getattr(runtime, "detail", "")
                or "no detail"
            )
            return False, (
                "the re-ensure reported an unusable runtime: the service "
                f"was not placed, so it names no address ({reason})"
            )
        port = getattr(runtime, "port", None)
        if not getattr(runtime, "ready", False):
            return False, (
                "the re-ensure did not report the service as ready"
                + (f": {getattr(runtime, 'detail', '') or 'no detail'}")
            )
        if not port:
            return False, "the re-ensure reported no port for the service"
        if str(getattr(runtime, "origin", "") or "") == "failed":
            # ``_relocate_service`` returns exactly this when it could
            # not place the service anywhere: the port it names is the
            # declared one, and there is no instance behind it.
            return False, (
                "the re-ensure reported a failed runtime (origin=failed); "
                "the service was not placed"
            )

        port = int(port)
        if not self._service_port_is_live(port):
            return False, (
                f"nothing accepted a TCP connection on port {port}"
            )

        health_url = self._runtime_health_url(name, port)
        if health_url:
            # The service is asked **itself**, over the guarded road —
            # the same request a verification point would send.  The
            # record is kept so the one precondition does not ask a
            # second time: one service, one question, one answer.
            record = _service_self_check(health_url)
            self._service_self_check_records[(str(name).lower(), port)] = record
            if record.get("status_code") != 200:
                return False, (
                    f"the health URL {health_url} did not answer 200"
                )
        return True, ""

    def _service_port_is_live(self, port: int) -> bool:
        """Gate 2 — is a connection accepted on *port*?

        Two oracles, and either one saying "yes" is enough: the direct
        TCP connect (1 s — the fast answer) and the round's own liveness
        probe, which names the process behind the port.
        """
        return _service_port_is_live(int(port))

    def _runtime_health_url(self, name: str, port: int) -> str:
        """The health URL of *name*'s instance at *port*.

        The declaration's own health URL, moved to the port the runtime
        reports — a service that was relocated answers on its new
        address.  Empty when the declaration declares none: there is
        then no health URL to ask, and the service is judged by the
        other two gates.
        """
        decl = self._service_declarations.get(str(name).lower())
        if decl is None:
            return ""
        try:
            from service_manager import render_declaration

            return render_declaration(decl, int(port)).health_url
        except Exception:  # pragma: no cover - defensive
            return ""

    def _adopt_reensure_runtimes(
        self, vp_id: str, runtime_map: Any,
    ) -> Dict[str, Dict[str, Any]]:
        """Verify every runtime a re-ensure produced, and adopt the ones
        that pass.

        The **accepted** runtimes are the only ones folded into
        ``_service_runtime_map``: a rejected one leaves the old entry
        standing, so the round's address does not move to a port nobody
        serves.  The address the VPs are pointed at follows the
        binding, and only an accepted runtime rebinds it.

        Returns ``{name: {"accepted": bool, "detail": str, "runtime": ...}}``
        — the caller turns a refusal into a BLOCKED verdict.
        """
        verdicts: Dict[str, Dict[str, Any]] = {}
        accepted: Dict[str, Any] = {}
        runtimes = getattr(runtime_map, "runtimes", None) or {}
        for name, runtime in runtimes.items():
            passed, detail = self._service_reensure_gates(name, runtime)
            verdicts[name] = {
                "accepted": passed,
                "detail": detail,
                "runtime": runtime,
            }
            if not passed:
                logger.warning(
                    "[service_freshness] vp=%s: the re-ensure of service %r "
                    "was not adopted: %s", vp_id, name, detail,
                )
                continue
            accepted[name] = runtime
            # 2026-10-25: the round's own re-ensure started a **new**
            # instance — a new process, a new boot id.  The expectation
            # follows it, or the next VP would compare the new instance
            # against the old one's boot id and block a service the
            # round itself has just brought back.  Only a service the
            # round had recorded an identity for is re-read: there is
            # nothing to move otherwise, and a service the round never
            # identified is not gated in the first place.
            if str(name).lower() in self._service_instance_identity:
                self._record_service_instance_identity(
                    name, int(getattr(runtime, "port")),
                )
            self._safe_log_freshness_event(
                vp_id, "service_reensure_verified",
                {
                    "service": name,
                    "port": int(getattr(runtime, "port")),
                    "pid": getattr(runtime, "pid", None),
                    "boot_id": self._runtime_boot_id(runtime),
                },
            )
        if accepted:
            self._merge_service_runtime_map(
                self._runtime_map_subset(runtime_map, accepted),
            )
        return verdicts

    @staticmethod
    def _runtime_map_subset(runtime_map: Any, runtimes: Dict[str, Any]) -> Any:
        """A runtime map carrying only *runtimes*."""
        from service_manager import ServiceRuntimeMap

        subset = ServiceRuntimeMap(runtimes=dict(runtimes))
        try:
            subset.duration_seconds = float(
                getattr(runtime_map, "duration_seconds", 0.0) or 0.0,
            )
        except Exception:  # pragma: no cover - defensive
            pass
        return subset

    def _merge_service_runtime_map(self, runtime_map: Any) -> None:
        """Fold a re-ensure's runtime map into the round's.

        The re-ensure only knows about the services it was asked to
        bring back; the healthy ones stay in the map the preflight
        built.  The merged map is the **record** of what the re-ensure
        found.

        The address the VPs are pointed at follows the **binding**,
        not the map: a re-ensure that really produced a live instance
        rebinds the round (the address is allowed to move exactly once,
        explicitly), and a re-ensure that failed releases the binding
        (there is no instance to talk to, and the blocked-port gate
        turns that into a BLOCKED verdict instead of a request to a
        dead port).

        Only **verified** runtimes reach this method — the adoption
        gates live in :meth:`_adopt_reensure_runtimes`, so a runtime
        that never came up is not merged into the map at all.
        """
        if runtime_map is None:
            return
        new_runtimes = getattr(runtime_map, "runtimes", None) or {}
        current = self._service_runtime_map
        if current is None:
            self._service_runtime_map = runtime_map
        else:
            try:
                current.runtimes.update(new_runtimes)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "[service_freshness] could not merge the runtime map: %s",
                    exc,
                )
        # The binding is what the VPs are dispatched to.  A re-ensure
        # that produced a live instance rebinds the round; one that
        # did not releases it — an address nobody listens on is not
        # an address a request may be sent to.
        for name, runtime in new_runtimes.items():
            if self._bind_service_runtime(name, runtime) is None:
                self._release_service_binding(name)

    def _refresh_resolved_service_placeholders(self) -> None:
        """Re-resolve the plan's ``{{svc.*}}`` placeholders.

        A service that had to move (its port was taken by a foreign
        process) must not leave the VPs pointing at the address it used
        to have.  The plan on disk keeps its placeholders, so the
        re-resolution starts from the **unresolved** plan — a resolved
        copy cannot be resolved again.
        """
        source = self._unresolved_plan_data
        if source is None or not self._executor_vps_by_id:
            return
        try:
            resolved = self._resolve_service_placeholders(source)
            fresh = self._convert_plan_to_executor_schema(resolved)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[service_freshness] placeholder refresh failed: %s", exc,
            )
            return
        by_id = {str(vp.get("id")): vp for vp in fresh.get("vps", [])}
        for vp_id, vp in self._executor_vps_by_id.items():
            new_vp = by_id.get(vp_id)
            if new_vp is None:
                continue
            vp.update(new_vp)

    def _reensure_failed_port(
        self, key: str, runtime: Any, decl: Any,
    ) -> int:
        """The port to block when a re-ensure of *key* did not deliver.

        The port the runtime named (the address the round was about to
        point its VPs at) — and, when the runtime named none, the
        declared port.  The address the VPs are *currently* pointed at
        is blocked as well: it is the one the request would have gone
        to.
        """
        port = getattr(runtime, "port", None)
        if port:
            return int(port)
        declared_port = getattr(decl, "port", None)
        if declared_port:
            return int(declared_port)
        bound = self._bound_service_port(key)
        return int(bound) if bound else 0

    def _block_service_port(
        self, key: str, port: int, vp_id: str, runtime: Any,
        detail: str = "",
    ) -> None:
        """Record that *key*'s port is unusable, and say why.

        The blocked-port gate turns this into a BLOCKED verdict; the
        report it carries has to name the failure, not just the port.

        Every address this service could be reached at is blocked — the
        one the re-ensure named, the declared one, and the one the
        round is currently bound to.  A request must not escape to any
        of them.
        """
        detail = str(detail or getattr(runtime, "detail", "") or "")
        ports = {int(port)}
        declared = self._service_declarations.get(key)
        declared_port = getattr(declared, "port", None)
        if declared_port is not None:
            ports.add(int(declared_port))
        bound = self._bound_service_port(key)
        if bound:
            ports.add(int(bound))
        self._blocked_service_ports.update(ports)
        # 2026-10-25 (repair-r1-24): the round could not **reach** the
        # service.  That is a different failure from "the service is
        # not ready", and the verdict has to say which one it is.
        for blocked_port in ports:
            self._service_unreachable[blocked_port] = detail
        try:
            from service_freshness import ServiceFreshnessReport
            report = ServiceFreshnessReport()
        except Exception:  # pragma: no cover - defensive
            return
        report.action = "blocked"
        report.detail = (
            f"service {key!r} on port {port} could not be brought back up "
            f"before VP {vp_id}"
            + (f": {detail}" if detail else "")
        )
        self._last_service_freshness_report = report
        logger.warning(
            "[service_freshness] service %s on port %s could not be "
            "revived: %s", key, port, detail or "no detail",
        )

    def _unblock_service_port(self, key: str, port: int) -> None:
        """A service that is alive again is not blocked."""
        self._blocked_service_ports.discard(port)
        declared = self._service_declarations.get(key)
        declared_port = getattr(declared, "port", None)
        if declared_port is not None:
            self._blocked_service_ports.discard(int(declared_port))


    def _maybe_rebuild_before_service_start(self) -> None:
        """Rebuild a stale build artefact before (re)starting services.

        Shared by both preflight paths. Never raises — a rebuild failure
        is logged and the caller proceeds; the per-VP freshness check
        will retry it with the full rebuild agent.

        2026-10-14 (single-flight): the rebuild is registered in the
        round's ``_BinaryRebuildCoordinator`` like any VP arm would be,
        so the VPs dispatched after this point can SEE it. That
        visibility is the semantics the round is built on: preflight
        rebuilds once, and the VPs after it do not rebuild again — they
        just re-check freshness and block if it is still stale.

        The call stays synchronous. ``run_full_verification`` reaches
        this before the round's event loop exists, so there is no loop
        to starve and nothing to await yet; that fact is pinned by
        ``test_preflight_rebuild_really_runs_with_no_event_loop`` in
        ``tests/unit/test_verification_rebuild_offloop.py``. (The old
        "the per-VP freshness check will retry it" promise above no
        longer holds after a FAILED preflight rebuild: that is the
        single-flight contract, and it is what stops the 20-VP retry
        storm.)
        """
        freshness = check_binary_freshness(
            self.project_dir, cache=self._freshness_cache,
        )
        if not freshness.is_stale:
            return
        logger.warning(
            "[service_freshness] binary stale before service restart: "
            "%s; rebuilding first", freshness.detail,
        )
        self._safe_log_freshness_event(
            "__preflight__", "binary_stale_detected",
            {
                "kind": freshness.kind,
                "detail": freshness.detail,
                "evidence": freshness.evidence,
            },
        )
        from binary_rebuild_agent import attempt_intelligent_rebuild
        from sub_agent_registry import sub_agent_registry

        def _rebuild_for_services() -> Any:
            return attempt_intelligent_rebuild(
                self.project_dir,
                freshness,
                coding_tool=self.coding_tool,
                plan_id=self.plan_dir.name,
                vp_id="__preflight__",
                registry=sub_agent_registry,
            )

        rebuild_result = self._rebuild_coordinator.run_once_sync(
            self._rebuild_coordinator.key_for(
                self.project_dir, freshness.kind,
            ),
            _rebuild_for_services,
        )
        self._safe_log_freshness_event(
            "__preflight__", "binary_rebuild_attempted",
            rebuild_result.to_dict(),
        )

    def _resolve_service_placeholders(self, plan_data: dict) -> dict:
        """Expand ``{{svc.<name>.<field>}}`` in the in-memory plan.

        Deliberately **in-memory only**: the on-disk plan keeps its
        placeholders so every round re-resolves them against the
        current declaration, and a persisted resolved copy can never go
        stale against a changed port. Only the plan handed to the
        executor is resolved.
        """
        if not self._service_declarations:
            return plan_data
        try:
            from service_declaration import apply_resolution
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[service_declaration] cannot resolve placeholders (%s); "
                "VPs will see the raw {{svc.*}} text", exc,
            )
            return plan_data
        # 2026-10-24: keep the **unresolved** plan.  A service that
        # moves has to be re-resolved, and a resolved copy cannot be
        # resolved again — the placeholders are gone by then.
        self._unresolved_plan_data = plan_data
        # 2026-09-18（端口让路）：解析必须按**实际**端口，不是声明端口 ——
        # 端口被外人占着时 preflight 会另起一个（``_relocate_service``），
        # 此时 {{svc.<name>.url}} 必须指向新端口，否则 VP 会去打那个别人的
        # 进程。
        resolved = apply_resolution(plan_data, self._effective_declarations())
        self._safe_log_freshness_event(
            "__preflight__", "service_placeholders_resolved",
            {"services": sorted(self._service_declarations)},
        )
        return resolved

    def _preflight_legacy_service_freshness(self, plan_data: dict) -> Any:
        """The pre-2026-09-18 preflight, for plans with no ``services``
        declaration: scrape ports out of the VP command text, then
        restart whatever is dead or stale."""
        from service_freshness import (
            ServiceFreshnessReport,
            assess_service_freshness,
        )

        report = ServiceFreshnessReport()
        self._last_service_freshness_report = report
        report = assess_service_freshness(self.project_dir, plan_data)
        self._last_service_freshness_report = report
        self._safe_log_freshness_event(
            "__preflight__", "service_freshness_assessed",
            report.to_dict(),
        )
        if not report.needs_restart:
            return report

        # Build first, restart second.
        self._maybe_rebuild_before_service_start()

        from service_restart_agent import attempt_service_restart
        from sub_agent_registry import sub_agent_registry
        restart_result = attempt_service_restart(
            self.project_dir,
            report.needs_restart,
            report.probes,
            coding_tool=self.coding_tool,
            plan_id=self.plan_dir.name,
            vp_id="__preflight__",
            registry=sub_agent_registry,
        )
        self._safe_log_freshness_event(
            "__preflight__", "service_freshness_restart",
            restart_result.to_dict(),
        )
        if restart_result.success:
            report.action = "restarted"
            report.detail = (
                f"restarted port(s) {restart_result.ports_restarted}; "
                f"post-check passed"
            )
            print(
                "[Verification] Service freshness preflight: "
                f"restarted {restart_result.ports_restarted}"
            )
        else:
            report.action = "blocked"
            report.detail = restart_result.diagnostics
            self._blocked_service_ports = set(report.needs_restart)
            logger.warning(
                "[service_freshness] preflight blocked: %s",
                restart_result.diagnostics,
            )
        return report

    def _build_unusable_service_blocked_verdict(
        self, vp_id: str, services: List[str], ports: List[int],
    ) -> dict:
        """Synthesize a BLOCKED verdict for a VP whose service could not
        be placed.

        The service is **missing**, not broken: the round found the
        declared port held by a process that is not the service, could
        not place it anywhere else, and therefore has no address to
        send this VP to.  The verdict names the service — a blocked
        port on its own does not say which service was missing.
        """
        detail = "; ".join(
            f"{name}: {self._unusable_services().get(name, {}).get('reason', '')}"
            for name in services
        )
        return {
            "status": "BLOCKED",
            "reasons": [
                blocked_reason(
                    f"{SERVICE_UNUSABLE} — the round could not place the "
                    f"service(s) {list(services)}, so it has no address to "
                    f"send this VP to ({detail})"
                )
            ],
            "actual_result": (
                f"service(s) {', '.join(services)} could not be placed, so "
                f"the round has no address for this VP: {detail}"
            ),
            "evidence": {
                "reason_code": SERVICE_UNUSABLE,
                "unusable_services": list(services),
                "blocked_ports": list(ports),
            },
            "framework_checks": [],
        }

    def _build_service_freshness_blocked_verdict(
        self, vp_id: str, ports: List[int],
    ) -> dict:
        """Synthesize a BLOCKED verdict for a VP whose service port the
        round-start preflight could not revive.

        Mirrors :meth:`_build_binary_freshness_blocked_verdict` —
        ``actual_result`` carries the preflight detail so downstream
        consumers (report, bridge UI, repair generator) show *why*
        instead of a generic schema-rejection message.
        """
        report = self._last_service_freshness_report
        detail = (
            (getattr(report, "detail", "") or "")
            or "service restart failed at round-start preflight"
        )
        evidence: Dict[str, Any] = {"blocked_ports": list(ports)}
        if report is not None:
            evidence["service_freshness"] = report.to_dict()
        # 2026-10-25 (repair-r1-24): the round could not **reach** the
        # service — the re-ensure produced no instance to talk to.  That
        # is a different failure from "the service is not ready", and a
        # report reader has to be able to tell them apart.
        unreachable = {
            port: self._service_unreachable[port]
            for port in ports
            if port in (getattr(self, "_service_unreachable", None) or {})
        }
        if unreachable:
            detail = "; ".join(
                f"port {port}: {text}" for port, text in sorted(unreachable.items())
            )
            evidence["reason_code"] = SERVICE_UNREACHABLE
            evidence["unreachable_ports"] = sorted(unreachable)
            return {
                "status": "BLOCKED",
                "reasons": [
                    blocked_reason(
                        f"{SERVICE_UNREACHABLE} — the round could not reach "
                        f"the service on port(s) {ports} after the re-ensure "
                        f"({detail})"
                    )
                ],
                "actual_result": (
                    f"{SERVICE_UNREACHABLE}: the re-ensure of the service on "
                    f"port(s) {ports} did not produce an instance the round "
                    f"could reach, so the request was not sent. detail: "
                    f"{detail}"
                ),
                "evidence": evidence,
                "framework_checks": [],
            }
        return {
            "status": "BLOCKED",
            "reasons": [blocked_reason(f"service not ready on port(s) {ports}")],
            "actual_result": (
                f"service freshness preflight BLOCKED this VP: port(s) "
                f"{ports} could not be restarted before the round. "
                f"detail: {detail}"
            ),
            "evidence": evidence,
            "framework_checks": [],
        }

    @staticmethod
    def _convert_plan_to_executor_schema(plan_data: dict) -> dict:
        """Convert the LLM plan to the executor's expected schema.

        LLM emits ``verification_points`` with ``id`` /
        ``verification_method``; the executor expects ``vps`` with
        ``id`` / ``method``. ``depends_on`` passes through unchanged
        (used by the executor's intra-batch ordering). The
        ``layer`` field was removed in 2026-06-13 — the executor no
        longer partitions VPs into L1/L2/L3 tiers.
        """
        verification_points = plan_data.get("verification_points", [])
        vps: List[Dict[str, Any]] = []
        for vp in verification_points:
            # Copy all relevant VP fields for the sub-agent runner.
            # 2026-09-13: per-VP timeout interface
            # DELETED. The plan's ``timeout_seconds`` value (which let a
            # legacy plan shrink VP-023 to 120s and stall round 4) is
            # NO LONGER forwarded — the field is fixed at 3600 for
            # event-observability purposes only. Enforcement is the
            # flat ``HARD_WALL_CLOCK_CAP_SECONDS=3600`` outer cap in
            # VerificationSubAgent plus the 15-min idle detector in
            # coding_tool.
            vps.append(
                {
                    "id": vp.get("id", "unknown"),
                    "method": vp.get("verification_method", "code_review"),
                    "depends_on": list(vp.get("depends_on", []) or []),
                    "title": vp.get("title", ""),
                    "expected_result": vp.get("expected_result", ""),
                    # 2026-09-18：`target_url` 之前**根本没有被带过来** ——
                    # 计划提示词把它写成 ui_validation 的必填字段，但转换器
                    # 和 VP prompt 都不带它，子 agent 从来不知道要打开哪个
                    # URL（只能从 expected_result 的散文里猜）。同时它也是
                    # 被阻断端口门禁的一条判定依据。
                    "target_url": vp.get("target_url", ""),
                    # 2026-09-18：VP 不再有 test_command。它的判定依据是
                    # method 自己的产物（api_test 的 request+assertions、
                    # code_review 的 citations、ui_validation 的
                    # checkpoints），见 verification_api_runner /
                    # verification_evidence。可选的 ``evidence_command``
                    # 由框架直跑、只作佐证，不参与判定。
                    "evidence_command": vp.get("evidence_command", ""),
                    # 2026-09-18（VP 判定重构）：api_test 的判定输入。
                    # 不带过去的话 `_run_api_test_vp` 会看到一份没有
                    # request/assertions 的 VP，只能判 schema 失败。
                    "request": vp.get("request"),
                    "assertions": vp.get("assertions"),
                    # 2026-09-18（D2）：full_ci 的判定输入。不带过去的话
                    # `_run_full_ci_vp` 会看到一份没有 ci_entry 的 VP，
                    # 只能判 schema 失败 —— 和上面 request/assertions 同
                    # 一个坑。
                    "ci_entry": vp.get("ci_entry", ""),
                    "ci_timeout_seconds": vp.get("ci_timeout_seconds"),
                    # 2026-09-18：依赖服务名要带过去。被阻断端口的判定允许
                    # 按名字匹配（占位符在派发前已展开，但 name 匹配是更直接
                    # 的一条路），丢了它这条门禁就只能靠正则去刮 URL。
                    "uses_services": list(vp.get("uses_services") or []),
                    "priority": vp.get("priority", "medium"),
                    "timeout_seconds": 3600,
                    # 2026-09-16 两阶段：阶段字段必须带进执行器 schema，
                    # 否则执行器看到的所有 VP 都是 Phase 1，门禁永不生效
                    # （全量关卡会在 Phase 1 里跟着一起跑）。
                    "verification_phase": vp.get("verification_phase", 1),
                    "phase_order": vp.get("phase_order"),
                    # 2026-09-16 计划增量：被本轮评估废弃的 VP 要带进去，
                    # 否则执行器会照跑一条已经不需要的验证点。
                    "obsolete": bool(vp.get("obsolete")),
                }
            )
        return {"vps": vps}

    def _build_execution_results_from_executor(
        self, plan_data: dict, executor: "VerificationExecutor"
    ) -> dict:
        """Project executor verdicts back to the legacy ``execution_results`` shape.

        The legacy shape consumed by :meth:`generate_verification_report` is::

            {
              "verification_points": [...],   # LLM-schema VP list
              "execution_results": [
                {"id": ..., "status": ..., "actual_result": ..., "evidence": ...},
                ...
              ],
              "executed_at": "...",
            }

        The executor's verdicts are::

            [
              {"vp_id": ..., "status": "PASSED|FAILED|SKIPPED",
               "reasons": [...], "evidence": {...}, ...},
              ...
            ]

        Mapping: ``vp_id`` → ``id``, ``reasons`` (list[str]) →
        ``actual_result`` (newline-joined string), ``evidence``
        (dict) → ``evidence`` (stringified for the legacy Phase 3
        judgment prompt which does string slicing).

        2026-09-07: prefer a verdict-level ``actual_result``
        field over a reasons-join when present, so BLOCKED
        (binary-stale) verdicts keep their original detail
        (``binary stale (rust): ... rebuild: cargo build``)
        instead of collapsing into ``"\n".join(reasons)`` which loses
        the rebuild command. See
        ``_build_binary_freshness_blocked_verdict``.
        """
        # 2026-10-14：交给 Phase 3 的 ``verification_points`` 只包含本轮
        # **真的会跑**的 VP —— 计划自己标记了 ``obsolete`` / ``superseded_by``
        # 的那些被排除在外（``verification_executor._plan_retired_vps`` 是
        # 同一个判据）。
        #
        # 为什么必须在信封这一层也摘掉，而不只是让
        # ``executor.collect_verdicts()`` 不返回它们的裁决：Phase 3 的判定
        # 提示词（``_build_judgment_prompt``）会把 ``verification_points``
        # 原样列成「## 验证计划」。一个只有计划条目、没有任何执行结果的
        # 作废 VP 对判定 LLM 来说是一张空白支票——它会替它编一个状态出来
        # （同一个计划的第 4 轮里，判定 LLM 给被门禁延后的 VP-028 编了
        # "记为 FAILED" 的散文，框架随后才把它强制还原成 DEFERRED）。
        #
        # 计划文件本身保留作废标记（打标记、不删除），审计痕迹不受影响。
        retired = executor._plan_retired_vps()
        verification_points = [
            vp for vp in plan_data.get("verification_points", []) or []
            if not (isinstance(vp, dict) and str(vp.get("id")) in retired)
        ]

        executor_verdicts = executor.collect_verdicts()

        # 2026-10-25 (repair-r1-31): the plan's own VPs are the
        # fallback for a VP that never dispatched — the address it was
        # *going to* use is still worth recording.
        plan_vps_by_id = {
            str(vp.get("id")): vp for vp in verification_points
            if isinstance(vp, dict)
        }

        results: List[Dict[str, Any]] = []
        for v in executor_verdicts:
            vp_id = v.get("vp_id", "unknown")
            status = v.get("status", "FAILED")
            # Preserve explicit actual_result if the verdict carries one;
            # otherwise fall back to the legacy reasons-join behavior.
            actual_result = v.get("actual_result")
            if actual_result is None or actual_result == "":
                reasons = v.get("reasons", []) or []
                if isinstance(reasons, list):
                    actual_result = "\n".join(str(r) for r in reasons)
                else:
                    actual_result = str(reasons)
            evidence = v.get("evidence", "")
            if not isinstance(evidence, str):
                evidence = json.dumps(evidence, ensure_ascii=False)
            # 2026-10-25 (repair-r1-31): every VP's recorded evidence
            # carries **where it was sent** — the URL, the port it
            # names, the port the round's ``ensure`` bound, the pid and
            # boot id behind that binding, the attempt count, the
            # classification code, the errno, and the round's request
            # id.  The production round's evidence said only
            # ``Errno 61``, which cannot tell a product defect from a
            # broken environment.
            evidence = self._enrich_evidence_with_address(
                str(vp_id), evidence, plan_vps_by_id.get(str(vp_id)),
            )
            result = {
                "id": vp_id,
                "status": status,
                "actual_result": actual_result,
                "evidence": evidence,
            }
            # 2026-10-24（api_test 的分类不能在这里丢掉）：裁决里带的
            # failure_class / failure_class_source 是四分类判定的结果 ——
            # "环境不可达"（infra）与"交付物坏了"（deliverable_defect）
            # 在同因阈值上的待遇完全相反。不带过去的话，历史里每一条失败
            # 都记成 deliverable_defect，也就是那条会计数、会生成修复任务
            # 的类（见 verification/orchestrator.py 的 _extract_failure_records）。
            #
            # 2026-10-26 (merge): 本分支的地址富化与 main 的分类**都要** ——
            # 地址回答"发到哪儿了"，分类回答"这是谁的错"，两个都是裁决
            # 的一部分，少一个都会把一半的证据丢掉。
            for key in ("failure_class", "failure_class_source"):
                value = v.get(key)
                if value:
                    result[key] = str(value)
            results.append(result)

        # 2026-10-25 (repair-r1-26): the environment verdict is a round
        # verdict.  If the round could not reach the service, none of
        # its results describes the product — they are all recorded
        # BLOCKED, and the round says so in its log.
        #
        # ``getattr`` for the same reason as elsewhere in this module:
        # an orchestrator built without ``__init__`` has judged no
        # environment, and "not blocked" is the fact that says so.
        if getattr(self, "_environment_blocked", False):
            self._write_environment_blocked_event()
            results = self._mark_results_environment_blocked(results)

        # Stable, plan-order output: re-sort by the plan's original
        # index so downstream consumers (the report, the bridge UI)
        # see a predictable sequence.
        original_index = {
            vp.get("id", ""): index
            for index, vp in enumerate(verification_points)
        }
        results.sort(
            key=lambda r: original_index.get(
                r.get("id", ""), len(original_index)
            )
        )

        execution_results = {
            "verification_points": verification_points,
            "execution_results": results,
            "executed_at": datetime.now().isoformat(),
        }

        # Persist to SQLite (``plan_verification.execution_results``)
        # so Phase 3 can reload from there. The legacy on-disk cache at
        # ``plans/{id}/verification_execution_results.json`` is no
        # longer written — see task #3.5.
        if self.verif_repo is not None:
            try:
                self.verif_repo.save_execution_results(self.plan_dir.name, execution_results)
            except Exception:  # noqa: BLE001
                pass

        return execution_results

    def _build_failure_execution_results(
        self, plan_data: dict, exc: Exception
    ) -> dict:
        """Build a minimal FAILED execution_results envelope on Phase 2 crash.

        The orchestrator needs a report dict with ``overall_status``
        even when Phase 2 raises — this builds the same shape as
        the success path so :meth:`generate_verification_report` can
        still produce a minimal FAILED report. Without this, an
        executor crash would propagate out of
        :meth:`run_full_verification` and leave the orchestrator
        state machine with no report to inspect.
        """
        verification_points = plan_data.get("verification_points", [])
        results = [
            {
                "id": vp.get("id", "unknown"),
                "status": "FAILED",
                "actual_result": (
                    f"executor.run() raised "
                    f"{type(exc).__name__}: {exc}"
                ),
                "reasons": [
                    f"executor.run() raised {type(exc).__name__}: {exc}"
                ],
                # 2026-10-25 (repair-r1-31): a crashed round's evidence
                # carries the address too — "the round crashed" is not
                # an answer to "which port was it talking to".
                "evidence": self._enrich_evidence_with_address(
                    str(vp.get("id", "unknown")),
                    {
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    },
                    vp,
                ),
            }
            for vp in verification_points
        ]
        # 2026-10-25 (repair-r1-26): a round whose environment was
        # broken is blocked, whatever the crash was.  A crash caused by
        # the environment must not be filed as a product failure — it is
        # the same event the blocked verdict describes.
        if self._environment_blocked:
            self._write_environment_blocked_event()
            results = self._mark_results_environment_blocked(results)
        execution_results = {
            "verification_points": verification_points,
            "execution_results": results,
            "executed_at": datetime.now().isoformat(),
        }

        # Persist to SQLite (``plan_verification.execution_results``)
        # so Phase 3 can reload from there. The legacy on-disk cache at
        # ``plans/{id}/verification_execution_results.json`` is no
        # longer written — see task #3.5.
        if self.verif_repo is not None:
            try:
                self.verif_repo.save_execution_results(self.plan_dir.name, execution_results)
            except Exception:  # noqa: BLE001
                pass

        return execution_results

    def _clear_verification_state_files(self) -> None:
        """Delete the executor's on-disk state so the next cycle starts fresh.

        Without this, a stale ``verification-executor sidecar`` left
        over from an earlier run causes the executor to skip VPs whose
        verdicts were already cached — even when ``verification_plan.json``
        has been edited since. Deleting both the verdict map and the
        progress view forces the next ``VerificationExecutor`` to seed
        ``pending_vps`` from the *current* plan.

        Best-effort: missing files are silently ignored; permission
        errors are logged via ``logging`` but do not raise, since this
        runs at the start of every verification cycle.
        """
        from verification_executor import DEFAULT_STATE_FILENAME, PROGRESS_STATE_FILENAME
        for filename in (DEFAULT_STATE_FILENAME, PROGRESS_STATE_FILENAME):
            path = self.plan_dir / filename
            try:
                path.unlink()
                logging.getLogger(__name__).debug(
                    "Cleared stale verification state file: %s", path
                )
            except FileNotFoundError:
                pass
            except OSError as exc:
                logging.getLogger(__name__).warning(
                    "Failed to clear %s on verification start: %s",
                    path, exc,
                )

    def _load_execution_results(self) -> Optional[Dict[str, Any]]:
        """Read the Phase 1 execution envelope (verification_points +
        execution_results + executed_at) from SQLite.

        Returns ``None`` when SQLite is unavailable or no row exists for
        ``self.plan_id`` (e.g. fresh install before the first verification
        round). Callers should fall back to the legacy on-disk cache
        (or raise if neither source has the data).
        """
        if self.verif_repo is None:
            return None
        try:
            row = self.verif_repo.current(self.plan_dir.name)
        except Exception:  # noqa: BLE001
            return None
        if row is None:
            return None
        envelope = row.get("execution_results")
        return envelope if isinstance(envelope, dict) else None


# ---------------------------------------------------------------------------
# Per-VP Timeout Wrapper
# ---------------------------------------------------------------------------

#: Module-level logger for per-VP timeout events (soft_warn,
#: completion). Imported lazily inside the wrapper so the module
#: remains importable in environments where ``logging`` is not yet
#: configured (e.g. some test harnesses that patch the root
#: logger).
_VP_TIMEOUT_LOGGER: "logging.Logger" = logging.getLogger("verification.vp")


async def _execute_single_vp_with_timeout(
    vp: dict,
    config: Optional[dict] = None,
) -> dict:
    """Run a single verification point under a per-method timeout.

    The wrapper is the per-VP boundary that applies yaml-driven
    per-method timeouts to a single verification point. It is
    deliberately small and dependency-free (no LLM, no persistence,
    no :class:`VerificationAgent` instance) so it can be
    unit-tested in isolation and reused from any per-VP call site.

    Args:
        vp: Dict with at least ``id``, ``verification_method`` and
            ``execute_fn`` (a callable that returns either a value
            or a coroutine). Missing ``execute_fn`` short-circuits
            to a benign ``PASSED`` no-op so callers that build a
            "no-op" VP (e.g. ``manual_check``) still get a
            well-formed result.
        config: Optional dict shaped like::

            {
                "timeouts": {
                    "<verification_method>": <seconds>,
                    "default": <seconds>,
                },
                "soft_warn_seconds": <int>,
            }

            ``config`` may be ``None``, in which case the wrapper
            falls back to a hard-coded 1800s global default and no
            soft-warn.

    Returns:
        A dict with at least ``id`` and ``status``. On timeout,
        the result is::

            {
                "id": <vp_id>,
                "status": "FAILED",
                "failure_reason": "timeout (Xs)",
                "deviation": "timeout_exceeded",
            }

        On normal completion, the result is whatever ``execute_fn``
        returned (assumed to be a dict-like with ``status``).

    Boundary semantics (TDD-pinned):

    * **Timeout → ``status='FAILED'``, NEVER ``'SKIPPED'``.** The
      zero-tolerance-for-skipping contract is preserved so the
      repair-task generator can pick up hung VPs. A misclassified
      ``SKIPPED`` would silently drop a hung VP from the
      deviation list and let the next verification round run
      against the same hung code.
    * **soft_warn → WARNING log at the threshold; process keeps
      running.** The wrapper does NOT short-circuit at
      ``soft_warn_seconds``; it only emits a single WARNING record
      and continues waiting up to the full timeout. The flag is
      advisory so a long-running VP can be observed in real time
      without aborting the work prematurely.
    * **sync ``execute_fn`` → wrapped in
      :func:`asyncio.to_thread`** so the event loop is not
      blocked by a blocking call.
    * **async ``execute_fn`` → awaited directly** (the result of
      calling an async function is a coroutine).
    * **Unknown / missing ``verification_method`` → falls through
      to the ``"default"`` key, then to a hard-coded 1800s.**
      Mirrors :meth:`verification_config.TimeoutPolicy.resolve`'s
      graceful-degradation contract.
    """
    if not isinstance(vp, dict):
        vp = {}
    if not isinstance(config, dict):
        config = {}

    vp_id = str(vp.get("id", "unknown"))
    method_raw = vp.get("verification_method", "")
    method = str(method_raw).strip() if method_raw is not None else ""
    if not method:
        method = "manual_check"

    timeouts_cfg = config.get("timeouts", {})
    if not isinstance(timeouts_cfg, dict):
        timeouts_cfg = {}

    try:
        soft_warn = int(config.get("soft_warn_seconds", 0) or 0)
    except (TypeError, ValueError):
        soft_warn = 0
    if soft_warn < 0:
        soft_warn = 0

    # Resolve timeout: method-specific key > "default" key > 1800s
    if method in timeouts_cfg:
        try:
            timeout = int(timeouts_cfg[method])
        except (TypeError, ValueError):
            timeout = 1800
    elif "default" in timeouts_cfg:
        try:
            timeout = int(timeouts_cfg["default"])
        except (TypeError, ValueError):
            timeout = 1800
    else:
        timeout = 1800
    if timeout <= 0:
        timeout = 1800

    execute_fn = vp.get("execute_fn")

    # No execute_fn → benign PASSED no-op
    if execute_fn is None:
        return {
            "id": vp_id,
            "status": "PASSED",
            "actual_result": "No execute_fn provided; treating as benign PASSED no-op",
            "reasons": ["No execute_fn provided; treating as benign PASSED no-op"],
            "failure_reason": None,
            "deviation": None,
        }

    async def _runner() -> Any:
        """Invoke ``execute_fn`` and await its result if it's a coroutine.

        Sync callables are dispatched to the default thread pool via
        :func:`asyncio.to_thread` so a blocking ``time.sleep(200)``
        does not stall the event loop. Async callables (the result
        of calling an ``async def`` function) are awaited directly
       — there is no thread-pool hop in that case.
        """
        result = execute_fn()
        if asyncio.iscoroutine(result):
            return await result
        return await asyncio.to_thread(result)

    # soft_warn: schedule a parallel coroutine that emits a single
    # WARNING log at the threshold and returns. It is cancelled
    # when the main task finishes before the threshold (success
    # or timeout) so the soft_warn task does not outlive the VP.
    soft_warn_task: Optional[asyncio.Task] = None
    if soft_warn and soft_warn > 0:

        async def _soft_warn_timer() -> None:
            try:
                await asyncio.sleep(soft_warn)
                _VP_TIMEOUT_LOGGER.warning(
                    "VP %s (%s) exceeded soft_warn threshold (%ss); "
                    "still waiting up to %ss timeout",
                    vp_id,
                    method,
                    soft_warn,
                    timeout,
                )
            except asyncio.CancelledError:
                # Cancellation is the normal exit path when the
                # main runner finishes before the threshold; swallow
                # it so the task does not log a noisy traceback.
                pass

        soft_warn_task = asyncio.create_task(_soft_warn_timer())

    async def _cancel_soft_warn() -> None:
        if soft_warn_task is None:
            return
        if not soft_warn_task.done():
            soft_warn_task.cancel()
        try:
            await soft_warn_task
        except (asyncio.CancelledError, Exception):
            # CancelledError is the normal exit; any other
            # exception inside the timer is already logged and
            # must not propagate up to the caller.
            pass

    try:
        result = await asyncio.wait_for(_runner(), timeout=timeout)
        await _cancel_soft_warn()
        return result
    except asyncio.TimeoutError:
        await _cancel_soft_warn()
        timeout_reason = f"timeout ({timeout}s)"
        return {
            "id": vp_id,
            "status": "FAILED",
            "actual_result": timeout_reason,
            "reasons": [timeout_reason],
            "failure_reason": timeout_reason,
            "deviation": "timeout_exceeded",
        }


# ---------------------------------------------------------------------------
# UI Validation — Three-Gate Fast-Fail Wrapper
# ---------------------------------------------------------------------------

#: Module-level logger for UI validation gate events (navigate
#: retry, screenshot soft-fail, selector hard-fail). Separate from
#: the per-VP ``verification.vp`` logger so gate-specific issues
#: can be filtered independently of general VP timeout noise.
_UI_VALIDATION_LOGGER: "logging.Logger" = logging.getLogger("verification.ui")


#: Default per-stage timeouts (seconds) used when the YAML config
#: omits the relevant ``puppeteer.*`` key. Values mirror the
#: production spec (navigate 15s, screenshot 10s, selector 8s) and
#: are intentionally short so a single hung VP is bounded at
#: roughly 30s worst case (15s navigate + 10s screenshot + 8s
#: selector) instead of the legacy 300s full-window budget.
_UI_DEFAULT_NAVIGATE_TIMEOUT = 15
_UI_DEFAULT_SCREENSHOT_TIMEOUT = 10
_UI_DEFAULT_SELECTOR_TIMEOUT = 8
_UI_DEFAULT_NAVIGATE_RETRY_MAX = 1


def _resolve_puppeteer_config(config: Optional[dict]) -> dict:
    """Normalise the ``config['puppeteer']`` block with safe defaults.

    Returns a dict with four integer keys
    (``navigate_timeout``, ``screenshot_timeout``,
    ``selector_timeout``, ``navigate_retry_max``). Bad / missing
    values fall back to the module-level defaults so the wrapper
    never crashes the verification round on a malformed YAML
    override.
    """
    if not isinstance(config, dict):
        config = {}
    pup = config.get("puppeteer", {})
    if not isinstance(pup, dict):
        pup = {}

    def _as_float(value, default):
        try:
            out = float(value)
        except (TypeError, ValueError):
            return default
        return out

    def _as_int(value, default):
        try:
            out = int(value)
        except (TypeError, ValueError):
            return default
        return out

    # Timeouts must be ``float`` so sub-second values from the
    # config (e.g. ``0.2s`` in tests, or ``1.5s`` in production)
    # survive the round-trip. ``asyncio.wait_for`` accepts floats
    # natively, so the downstream call site needs no change.
    navigate_timeout = _as_float(
        pup.get("navigate_timeout"), float(_UI_DEFAULT_NAVIGATE_TIMEOUT)
    )
    screenshot_timeout = _as_float(
        pup.get("screenshot_timeout"), float(_UI_DEFAULT_SCREENSHOT_TIMEOUT)
    )
    selector_timeout = _as_float(
        pup.get("selector_timeout"), float(_UI_DEFAULT_SELECTOR_TIMEOUT)
    )
    navigate_retry_max = _as_int(
        pup.get("navigate_retry_max"), _UI_DEFAULT_NAVIGATE_RETRY_MAX
    )

    # Force sane bounds: a zero / negative timeout is meaningless for
    # ``asyncio.wait_for`` (it fires immediately) and would mask the
    # real failure mode, so we reset to the default. Retry is bounded
    # at zero — negative retries are not meaningful.
    if navigate_timeout <= 0:
        navigate_timeout = _UI_DEFAULT_NAVIGATE_TIMEOUT
    if screenshot_timeout <= 0:
        screenshot_timeout = _UI_DEFAULT_SCREENSHOT_TIMEOUT
    if selector_timeout <= 0:
        selector_timeout = _UI_DEFAULT_SELECTOR_TIMEOUT
    if navigate_retry_max < 0:
        navigate_retry_max = 0

    return {
        "navigate_timeout": navigate_timeout,
        "screenshot_timeout": screenshot_timeout,
        "selector_timeout": selector_timeout,
        "navigate_retry_max": navigate_retry_max,
    }


async def _run_ui_validation_with_fastfail(
    vp: dict,
    config: Optional[dict] = None,
    mcp: Optional[Any] = None,
) -> dict:
    """Run a single UI validation VP through three hard/soft timeout gates.

    The wrapper compresses the legacy 300s single-VP budget down to
    roughly 30s worst case by applying three explicit per-stage
    timeouts around the puppeteer MCP calls:

    1. **Gate 1 (navigate) — hard-fail, retried.** The puppeteer
       ``navigate`` call is wrapped in
       :func:`asyncio.wait_for` with
       ``puppeteer.navigate_timeout`` (default 15s). On timeout the
       call is retried up to ``puppeteer.navigate_retry_max`` times
       (default 1, i.e. one initial + one retry). If every attempt
       fails the wrapper returns ``FAILED`` with
       ``stage_failed='navigate'`` and a failure reason that names
       the resolved timeout — it does NOT enter the screenshot or
       selector stages.

    2. **Gate 2 (screenshot) — soft-fail.** The puppeteer
       ``screenshot`` call is wrapped with
       ``puppeteer.screenshot_timeout`` (default 10s). On
       timeout or any exception the wrapper logs a WARNING and
       continues to the selector stage, marking the result with
       ``screenshot_unavailable=True`` so downstream consumers
       (e.g. the report renderer) know the screenshot was skipped.

    3. **Gate 3 (selectors) — hard-fail on first miss.** Each
       selector in ``vp['selectors']`` is awaited via
       :func:`asyncio.wait_for` with
       ``puppeteer.selector_timeout`` (default 8s). On the first
       timeout the wrapper returns ``FAILED`` with
       ``stage_failed='selector'`` and a deviation string of the
       form ``selector_not_found:<selector>`` so the repair-task
       generator can attribute the failure to a specific UI
       element.

    The function is intentionally dependency-injectable: the
    ``mcp`` parameter accepts any object exposing the three async
    methods ``navigate(url)``, ``screenshot()`` and
    ``wait_for_selector(selector)``. Production callers wire a
    real MCP client; unit tests wire a stub (see
    ``tests/test_verification.py::_MCPStub``).

    Args:
        vp: Dict with at least ``id``. ``url`` and ``selectors``
            are read; missing ``url`` is treated as an empty
            string (the navigate call still happens and may
            fail), missing or non-list ``selectors`` is treated
            as empty (the gate is a no-op).
        config: Optional dict shaped like::

            {
                "puppeteer": {
                    "navigate_timeout": <int seconds>,
                    "screenshot_timeout": <int seconds>,
                    "selector_timeout": <int seconds>,
                    "navigate_retry_max": <int retries>,
                }
            }

            Missing / malformed keys fall back to the
            module-level defaults.
        mcp: Optional object exposing async ``navigate``,
            ``screenshot`` and ``wait_for_selector`` methods. If
            ``None``, the wrapper returns a FAILED result with
            ``stage_failed='navigate'`` and a clear
            ``failure_reason`` rather than crashing the
            verification round.

    Returns:
        A dict with at least ``id`` and ``status``. On a
        navigate exhaustion::

            {
                "id": <vp_id>,
                "status": "FAILED",
                "stage_failed": "navigate",
                "failure_reason": "navigate timeout (15s) after 2 attempts",
            }

        On a selector miss::

            {
                "id": <vp_id>,
                "status": "FAILED",
                "stage_failed": "selector",
                "failure_reason": "selector '#main-button' timeout (8s)",
                "deviation": "selector_not_found:#main-button",
                "screenshot_unavailable": <bool>,
            }

        On full success::

            {
                "id": <vp_id>,
                "status": "PASSED",
                "screenshot_unavailable": <bool>,  # only if true
            }

    Boundary semantics (TDD-pinned):

    * **Gate 1 is a hard-fail.** A hung navigate MUST yield
      ``status='FAILED'`` with ``stage_failed='navigate'``. The
      retry is bounded — a misconfigured
      ``navigate_retry_max=999`` cannot deadlock the round.
    * **Gate 2 is a soft-fail.** A timeout / exception at the
      screenshot stage MUST NOT terminate the VP. The wrapper
      records ``screenshot_unavailable=True`` and proceeds to
      Gate 3.
    * **Gate 3 is a hard-fail on the first miss.** The wrapper
      stops iterating selectors at the first timeout / exception
     — it does NOT collect "passed N out of M" partial
      results, because a missing selector is a contract
      violation that the repair-task generator needs to act on
      as a unit.
    * **The wrapper is single-VP-scoped.** It does NOT call
      ``puppeteer`` tools outside the three stages above (no
      page.evaluate, no cookies reset, no extra navigations),
      so the 15s/10s/8s timeouts compose to a worst-case
      budget of roughly 33s + retry overhead, not the legacy
      300s full-window timeout.
    """
    if not isinstance(vp, dict):
        vp = {}
    if not isinstance(config, dict):
        config = {}

    vp_id = str(vp.get("id", "unknown"))
    url = vp.get("url", "")
    if url is None:
        url = ""
    url = str(url)

    raw_selectors = vp.get("selectors", [])
    if isinstance(raw_selectors, list):
        selectors = [str(s) for s in raw_selectors if s is not None]
    elif raw_selectors is None:
        selectors = []
    else:
        # Non-list, non-None value: best-effort coerce so the gate
        # still has something to iterate (a single-selector string
        # is a common typo). Anything else is treated as empty.
        selectors = [str(raw_selectors)]

    pup = _resolve_puppeteer_config(config)
    navigate_timeout = pup["navigate_timeout"]
    screenshot_timeout = pup["screenshot_timeout"]
    selector_timeout = pup["selector_timeout"]
    navigate_retry_max = pup["navigate_retry_max"]

    if mcp is None:
        # No MCP client wired in. We do NOT raise — the verification
        # round must stay alive so the other VPs can still report.
        # The repair-task generator reads stage_failed='navigate' and
        # can produce a "wire the MCP client" follow-up task.
        _UI_VALIDATION_LOGGER.warning(
            "VP %s skipped: no mcp client provided", vp_id
        )
        return {
            "id": vp_id,
            "status": "FAILED",
            "stage_failed": "navigate",
            "failure_reason": "no mcp client provided",
        }

    # ---- Gate 1: navigate (hard-fail, retried) ----
    total_attempts = 1 + navigate_retry_max
    navigate_ok = False
    last_was_timeout = False
    last_exception: Optional[BaseException] = None
    for attempt in range(1, total_attempts + 1):
        try:
            await asyncio.wait_for(
                mcp.navigate(url), timeout=navigate_timeout
            )
            navigate_ok = True
            break
        except asyncio.TimeoutError:
            last_was_timeout = True
            last_exception = asyncio.TimeoutError(
                f"navigate timeout ({navigate_timeout}s)"
            )
            _UI_VALIDATION_LOGGER.warning(
                "VP %s navigate attempt %d/%d timed out after %ss",
                vp_id,
                attempt,
                total_attempts,
                navigate_timeout,
            )
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            last_was_timeout = False
            last_exception = exc
            _UI_VALIDATION_LOGGER.warning(
                "VP %s navigate attempt %d/%d raised %s: %s",
                vp_id,
                attempt,
                total_attempts,
                type(exc).__name__,
                exc,
            )

    if not navigate_ok:
        if last_was_timeout:
            reason = (
                f"navigate timeout ({navigate_timeout}s) "
                f"after {total_attempts} attempts"
            )
        elif last_exception is not None:
            reason = (
                f"navigate error after {total_attempts} attempts: "
                f"{type(last_exception).__name__}: {last_exception}"
            )
        else:
            reason = (
                f"navigate failed after {total_attempts} attempts"
            )
        return {
            "id": vp_id,
            "status": "FAILED",
            "stage_failed": "navigate",
            "failure_reason": reason,
        }

    # ---- Gate 2: screenshot (soft-fail) ----
    screenshot_unavailable = False
    try:
        await asyncio.wait_for(
            mcp.screenshot(), timeout=screenshot_timeout
        )
    except asyncio.TimeoutError:
        screenshot_unavailable = True
        _UI_VALIDATION_LOGGER.warning(
            "VP %s screenshot timed out after %ss; continuing",
            vp_id,
            screenshot_timeout,
        )
    except Exception as exc:  # noqa: BLE001 — surface, don't crash
        screenshot_unavailable = True
        _UI_VALIDATION_LOGGER.warning(
            "VP %s screenshot failed (%s: %s); continuing",
            vp_id,
            type(exc).__name__,
            exc,
        )

    # ---- Gate 3: selectors (hard-fail on first miss) ----
    for selector in selectors:
        try:
            await asyncio.wait_for(
                mcp.wait_for_selector(selector),
                timeout=selector_timeout,
            )
        except asyncio.TimeoutError:
            return {
                "id": vp_id,
                "status": "FAILED",
                "stage_failed": "selector",
                "failure_reason": (
                    f"selector {selector!r} timeout ({selector_timeout}s)"
                ),
                "deviation": f"selector_not_found:{selector}",
                "screenshot_unavailable": screenshot_unavailable,
            }
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            return {
                "id": vp_id,
                "status": "FAILED",
                "stage_failed": "selector",
                "failure_reason": (
                    f"selector {selector!r} error: "
                    f"{type(exc).__name__}: {exc}"
                ),
                "deviation": f"selector_error:{selector}",
                "screenshot_unavailable": screenshot_unavailable,
            }

    # All gates passed.
    result: dict = {
        "id": vp_id,
        "status": "PASSED",
    }
    if screenshot_unavailable:
        result["screenshot_unavailable"] = True
    return result


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------

def verify_plan(plan_id: str, project_dir: str,
                plan_base_dir: Optional[str] = None) -> dict:
    """
    Convenience function to verify a plan.

    Args:
        plan_id: Plan identifier
        project_dir: Project directory to verify
        plan_base_dir: Base directory for plans (defaults to ../plans)

    Returns:
        Verification report dict
    """
    if plan_base_dir is None:
        # 2026-09-13: ``PDT_PLANS_DIR``-aware (see
        # ``config_paths.resolve_plans_dir``) so a test that verifies a
        # fixture plan_id does not create directories in the operator's
        # live plans tree.
        plan_base_dir = resolve_plans_dir()
    else:
        plan_base_dir = Path(plan_base_dir)

    plan_dir = plan_base_dir / plan_id
    agent = VerificationAgent(plan_dir, Path(project_dir))
    return agent.run_full_verification()


# ---------------------------------------------------------------------------
# Dry-run backend (FakeVerifierBackend) and _select_backend() factory
# ---------------------------------------------------------------------------

#: Environment variable that flips the verification agent into dry-run
#: mode. When set to ``"dry_run"``, :func:`_select_backend` returns a
#: :class:`FakeVerifierBackend` that replaces the expensive per-method
#: executors with a pure-asyncio ``asyncio.sleep``. The rest of the
#: pipeline (orchestrator, real events, real group partition, real
#: split decision) is unchanged, so end-to-end exercises of the
#: verification loop are possible without LLM, puppeteer, or
#: filesystem costs.
VERIFICATION_PROFILE_ENV: str = "VERIFICATION_PROFILE"
VERIFICATION_PROFILE_DRY_RUN: str = "dry_run"


class FakeVerifierBackend:
    """In-process dry-run replacement for the per-method executors.

    Replaces :meth:`VerificationAgent._execute_automated_test_async`,
    :meth:`VerificationAgent._execute_code_review`,
    :meth:`VerificationAgent._execute_ui_validation`, and
    :meth:`VerificationAgent._execute_api_test` with a single
    ``asyncio.sleep(fake_sleep_seconds)`` so the full verification
    pipeline can be exercised without spinning up subprocesses.

    The class is deliberately a small, standalone unit — no
    filesystem, no LLM, no :class:`VerificationAgent` reference — so
    it can be unit-tested in isolation (see
    ``tests/test_verification_fake_backend.py``) and reused outside
    the agent if needed.

    Boundary semantics pinned by the TDD spec:

    * ``fake_sleep_seconds < timeout_seconds`` → returns
      ``{"status": "PASSED", ...}`` after sleeping.
    * ``fake_sleep_seconds >= timeout_seconds`` → raises
      :class:`asyncio.TimeoutError` (the same exception the real
      ``asyncio.wait_for`` path raises), so the agent's
      split-on-timeout branch is exercised.
    * ``should_fail=True`` short-circuits the sleep and returns
      ``{"status": "FAILED", ...}`` regardless of the timeout.
    """

    DEFAULT_FAKE_SLEEP_SECONDS: float = 1.0

    def __init__(
        self,
        fake_sleep_seconds: float = DEFAULT_FAKE_SLEEP_SECONDS,
        should_fail: bool = False,
    ):
        self.fake_sleep_seconds = float(fake_sleep_seconds)
        self.should_fail = bool(should_fail)

    async def execute(self, vp: dict, timeout_seconds: float) -> dict:
        """Run a fake per-method execution under a real ``asyncio.wait_for``.

        Two short-circuit rules are applied before sleeping so a test
        with ``fake_sleep_seconds > timeout_seconds`` does not have to
        wait the full timeout in real time:

        1. If ``fake_sleep_seconds >= timeout_seconds``, raise
           :class:`asyncio.TimeoutError` immediately. This matches
           what :func:`asyncio.wait_for` would do after the timeout
           fires, but without paying the wall-clock cost — critical
           for tests where ``fake_sleep`` is e.g. 1900 and
           ``timeout`` is 1800.
        2. If ``fake_sleep_seconds == 0`` (or negative), skip the
           sleep entirely and return PASSED — useful for unit tests
           that just want to assert the result shape, not measure
           real time.

        Otherwise, the sleep is wrapped in
        :func:`asyncio.wait_for` so the per-VP timeout boundary is
        observed at the same point as the real backend's. On
        timeout, :class:`asyncio.TimeoutError` propagates to the
        caller; on success, a result dict with ``status="PASSED"``
        (or ``"FAILED"`` when ``should_fail`` is set) is returned.
        """
        vp_id = str(vp.get("id", "unknown"))

        if self.should_fail:
            return {
                "id": vp_id,
                "status": "FAILED",
                "actual_result": (
                    f"dry-run backend forced FAILED (fake_sleep="
                    f"{self.fake_sleep_seconds}s, should_fail=True)"
                ),
                "evidence": "FakeVerifierBackend.should_fail=True",
            }

        # Short-circuit: if the fake sleep would obviously exceed the
        # timeout, raise TimeoutError immediately. This keeps tests
        # like ``fake_sleep=1900, timeout=1800`` from waiting 30
        # minutes in real time.
        if self.fake_sleep_seconds >= float(timeout_seconds):
            raise asyncio.TimeoutError(
                f"FakeVerifierBackend: fake_sleep_seconds="
                f"{self.fake_sleep_seconds} >= timeout_seconds="
                f"{timeout_seconds}"
            )

        if self.fake_sleep_seconds > 0:
            await asyncio.wait_for(
                asyncio.sleep(self.fake_sleep_seconds),
                timeout=timeout_seconds,
            )

        return {
            "id": vp_id,
            "status": "PASSED",
            "actual_result": (
                f"dry-run backend slept {self.fake_sleep_seconds}s "
                f"(timeout={timeout_seconds}s)"
            ),
            "evidence": "FakeVerifierBackend",
        }


def _select_backend(
    default_sleep_seconds: float = FakeVerifierBackend.DEFAULT_FAKE_SLEEP_SECONDS,
    default_should_fail: bool = False,
) -> "FakeVerifierBackend | None":
    """Return a backend instance based on the ``VERIFICATION_PROFILE`` env var.

    Returns :class:`FakeVerifierBackend` only when
    ``os.environ[VERIFICATION_PROFILE] == "dry_run"``; otherwise
    returns ``None`` (caller falls through to the real per-method
    executors on the :class:`VerificationAgent`).

    The two ``default_*`` parameters are exposed so tests can size
    the fake-sleep budget deterministically without having to mutate
    module globals. They are not read from environment — the env
    var controls the *choice* of backend, not the *configuration* of
    the fake one (configuration is per-VP via the splitter / plan).
    """
    if os.environ.get(VERIFICATION_PROFILE_ENV) == VERIFICATION_PROFILE_DRY_RUN:
        return FakeVerifierBackend(
            fake_sleep_seconds=default_sleep_seconds,
            should_fail=default_should_fail,
        )
    return None


# ---------------------------------------------------------------------------
# write_verification_report — module-level report writer with optional
# execution_profile observability field.
#
# Older report files (V1, and any in-flight plans that pre-date
# the observability field) do NOT contain ``execution_profile``. Readers
# MUST use ``dict.get("execution_profile")`` which returns ``None`` for
# missing keys — never a KeyError. The shape is intentionally small
# (3 keys: mode / per_group_concurrency / total_wall_seconds) so it can
# be added without breaking JSON consumers that only care about
# ``results`` or other top-level fields.
# ---------------------------------------------------------------------------


def write_verification_report(
    report_path,
    results,
    execution_profile=None,
):
    """Write a verification report JSON to ``report_path``.

    The report has the shape::

        {
            "results": [...],                  # required
            "execution_profile": {...} | null  # optional, observability
        }

    ``execution_profile`` is always present in the written JSON. When
    the caller passes ``None`` (e.g. legacy serial mode), the field is
    serialised as JSON ``null`` rather than omitted — this keeps the
    field position stable for downstream readers.

    Old reports on disk that pre-date this field can still be loaded
    with ``json.load`` + ``dict.get("execution_profile")`` (returns
    ``None`` when the key is absent — never raises).

    Args:
        report_path: Destination path for the JSON report. Parent
            directories are created if they do not exist.
        results: Iterable of verification-result dicts. Stored under
            the top-level ``results`` key. ``None`` is normalised to
            an empty list.
        execution_profile: Optional observability dict with keys
            ``mode``, ``per_group_concurrency``, ``total_wall_seconds``
            (or ``None`` to emit JSON ``null``).

    Returns:
        The dict that was serialised to disk (useful for callers that
        want to inspect the in-memory shape, e.g. tests).
    """
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    report_data = {
        "results": list(results) if results is not None else [],
        "execution_profile": execution_profile,
    }

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)

    return report_data


# ---------------------------------------------------------------------------
# Backward-compatibility alias
# ---------------------------------------------------------------------------
#
# Phase C7 renamed ``VerificationAgent`` to ``Orchestrator`` to reflect
# that this class is now a thin wiring layer over the 6 phase classes
# in ``backend/verification/``.  A large surface of code (the
# ``VerificationOrchestrator`` in ``backend/verification/orchestrator.py``,
# ``backend/server.py``, ~12 test files) still imports the historical
# name; the alias keeps that surface working unchanged so the rename
# is a non-breaking refactor.

VerificationAgent = Orchestrator

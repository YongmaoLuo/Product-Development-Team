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
import time
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


#: 裁决 / 结果里记录"**判它的那次提交**"的字段。
#:
#: 2026-10-26（VP-031）：一条 FAILED 结论绑在 dfd1c27 上，而当前 HEAD 已经是
#: 876fb19 —— 那是**旧代码上的**失败，当前这份代码还没被它验过。而结果里
#: 不记"判它的是哪次提交"，跨提交沿用这件事根本无从发现：报告里看到的是
#: 一条"失败"，没人知道它是哪棵树上失败的。
#:
#: 与 :mod:`verification_verdict_provenance` 的命令指纹同源同向：结论只对
#: 产生它的那份代码有效。
JUDGED_HEAD_FIELD = "judged_head"

#: 没有定义就不许沿用旧结论的标记。
#:
#: 2026-10-26（VP-031）：增量评估发过 VP-031 这个号，但它的定义从未落进
#: ``verification_plan.json``。执行器**无定义可分派** —— 那条 VP 这一轮
#: 一次都没跑；它此前挣到的失败被原样沿用，报告里 VP-031 的 title /
#: expected_result 是空的，失败却是"真的"。
#:
#: 框架必须把这件事**显式**记下来，而不是让一条旧失败冒充今天的结论。
BLOCKED_DEFINITION_MISSING = "BLOCKED_DEFINITION_MISSING"


def stamp_judged_head(verdict: Dict[str, Any], head: str) -> Dict[str, Any]:
    """给裁决盖上"判它的那次提交"（返回副本，不改原对象）。

    两处都写：

    * 顶层 —— 内存路径（``collect_verdicts`` → 报告信封）读它；
    * ``evidence`` 里 —— 落库路径。``VerificationExecutor.record_verdict``
      只把固定几个字段写进 SQLite（``vp_id`` / ``status`` / ``reasons`` /
      ``evidence`` / 命令指纹），``evidence`` 是唯一能原样落库的地方。

    ``head`` 为空时**不盖** —— "不知道判它的是哪次提交"与"知道是某次提交"
    是两件事，把空串盖上去会让两者看起来一样。

    2026-10-27：裁决**已经**带着出处时不覆盖。全量门禁的裁决自带它
    **跑的时候**那棵树的 HEAD（框架执行的，不是子 agent 自报），那是
    更贴切的一个 —— 覆盖它等于用本轮开头的缓存 HEAD 冒充门禁真的跑过
    的那棵树。顶层没有但 ``evidence`` 里有的，以 ``evidence`` 里那个为
    准（同一个道理：更具体的出处胜出）。
    """
    if not isinstance(verdict, dict) or not head:
        return verdict
    existing = str(verdict.get(JUDGED_HEAD_FIELD) or "")
    if existing:
        return verdict
    evidence = verdict.get("evidence")
    evidence_head = ""
    if isinstance(evidence, dict):
        evidence_head = str(evidence.get(JUDGED_HEAD_FIELD) or "")
    stamped = dict(verdict)
    stamped[JUDGED_HEAD_FIELD] = evidence_head or head
    if isinstance(evidence, dict):
        stamped["evidence"] = {
            **evidence,
            JUDGED_HEAD_FIELD: stamped[JUDGED_HEAD_FIELD],
        }
    return stamped


def definition_missing(
    definitions: Dict[str, Dict[str, Any]], vp_id: str,
) -> bool:
    """这个 id 在计划里**查不到定义**吗？

    计划一条 VP 都没有时**不下**这个判断：没有计划可比对，"查不到"就分不清
    是"定义没落盘"（VP-031）还是"压根没有计划"（退化输入）。这种时候下
    判断只会凭空造出一条 ``BLOCKED_DEFINITION_MISSING`` —— 那不是任何人的
    结论。

    分派前与投影前用的是**同一个**判据（只写一处）：两处对"有没有定义"
    的看法一旦漂移，同一条 VP 会在一处被记成"跑不了"、在另一处被记成
    "跑过了"。
    """
    if not definitions:
        return False
    return str(vp_id) not in definitions


def requires_reexecution(result: Any, current_head: str) -> bool:
    """这条失败必须重跑吗？

    判据：**判它的那次提交**不是当前 HEAD —— 那是旧代码上的失败，当前
    这份代码还没被它验过。

    缺 ``judged_head`` 的旧结论按同一条路处理（"无法证明出处"），与
    :func:`verification_verdict_provenance.is_stale` 的"没有指纹 = 失效"
    同一个方向。

    只对 FAILED 生效：PASSED / SKIPPED 是终态成功，不重跑。``current_head``
    为空（不是 git 仓库 / git 不可用）时**不**下判断 —— 无从比较时不猜。
    """
    if not isinstance(result, dict):
        return False
    if str(result.get("status") or "") != "FAILED":
        return False
    if not current_head:
        return False
    judged_head = str(result.get(JUDGED_HEAD_FIELD) or "")
    return judged_head != current_head


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


# 2026-10-25: cap on the command summary a service-relaunch event
# carries.  The command is copied verbatim — it is the audit record of
# *what was re-run*, and the mcode/claude difference lives entirely in
# it — so the cap exists only to keep a pathologically long command
# from growing the event log without bound.  A real declaration is a
# few hundred characters, so it is never trimmed.
MAX_EVENT_COMMAND_CHARS = 4000


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


#: The reason codes that mean "the environment stopped this, not the
#: product".  ``service_unreachable`` is the per-point code the
#: classification layer writes; ``environment_blocked`` is the round's
#: own code, written when one refused connection takes the whole round
#: out.
ENVIRONMENT_REASON_CODES = frozenset({
    SERVICE_UNREACHABLE, ENVIRONMENT_BLOCKED,
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
        # 2026-10-27 (merge): the body the service answered with, kept
        # so that the api phase's registration can quote it without
        # asking the same endpoint a second time (see
        # :meth:`_recorded_health_answer`).
        "body": "",
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
    # 2026-10-27 (merge): the body is kept so that the api phase's
    # registration can quote the same answer instead of asking the
    # endpoint a second time.
    result["body"] = raw
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


# Execution surface — which code is actually running this round
# ---------------------------------------------------------------------------
#
# A round's verdict says what the request did.  It used to say nothing
# about *who* ran it: which tree the framework was imported from, and
# which commit that tree was at.  A round graded by a copy of the
# framework that did not contain the fix under repair therefore looked
# exactly like a round whose service had been killed — and those two
# need completely different repairs.
#
# The facts below are recorded as evidence, once per round, so the
# question can be answered by reading a log instead of by
# reconstructing a timeline.


def _framework_file() -> str:
    """The file of the backend package this process actually imported.

    ``import backend`` resolves against ``sys.path``, so it answers the
    question that matters — *which checkout is running* — rather than
    the question this file's own location answers.  The fallback (this
    module's own file) is the same tree in every layout where the
    backend package is importable as a top-level module.
    """
    try:
        import backend as _backend
    except Exception:  # noqa: BLE001 - the package may not be importable
        return os.path.realpath(__file__)
    raw = getattr(_backend, "__file__", None)
    if not raw:
        return os.path.realpath(__file__)
    return os.path.realpath(str(raw))


def _framework_root(framework_file: str) -> str:
    """The tree the framework file belongs to.

    ``<tree>/backend/__init__.py`` → ``<tree>``: the tree is the unit
    a ``git rev-parse`` and a checkout comparison both mean.
    """
    parent = Path(framework_file).resolve().parent
    if parent.name == "backend":
        return str(parent.parent)
    return str(parent)


def _git_head(directory: Optional[str]) -> str:
    """The commit ``directory`` is checked out at, or ``"unknown"``.

    Read through :class:`git_manager.GitManager` rather than through a
    ``subprocess`` call: this module does not shell out (see the
    boundary note above), GitPython is already a dependency, and a tree
    that is not a repository at all has no HEAD to report.
    """
    if not directory:
        return "unknown"
    try:
        from git_manager import GitManager

        head = str(GitManager(str(directory)).rev_parse("HEAD") or "").strip()
    except Exception:  # noqa: BLE001 - no git, no repo, no HEAD
        return "unknown"
    return head or "unknown"


def _path_is_inside(child: str, parent: str) -> bool:
    """Whether ``child`` is inside ``parent`` (both already real paths)."""
    try:
        return Path(child).is_relative_to(Path(parent))
    except (ValueError, OSError):  # pragma: no cover - defensive
        return False


def _harness_env_var() -> str:
    """The environment variable that says which backend a service runs under.

    Read out of :mod:`service_manager` — the module that owns the
    variable — so there is one name in the code base and not two that
    have to be kept in step.
    """
    try:
        from service_manager import HARNESS_ENV_VAR

        return str(HARNESS_ENV_VAR)
    except Exception:  # pragma: no cover - defensive
        return "PDT_HARNESS"


# ---------------------------------------------------------------------------
# Data plane — was the field observed, and if not, why not
# ---------------------------------------------------------------------------
#
# ``/health/data`` answers ``{"status": "ok", "plan_routing_rows": N}``.
# The count is the only thing that says the **SQLite data plane** is
# readable: a server with no database at all answers
# ``{"status": "ok", "plan_routing_rows": 0}``, and a server whose
# endpoint does not carry the field answers ``{"status": "ok"}``.  So
# "the field was not observed" is three different faults wearing one
# sentence:
#
#   * nothing answered — the service was not there;
#   * something answered, and the field was not in it;
#   * the field was observed, with its value.
#
# The framework records which one happened.  It never *asserts* the
# field on the VP's behalf: the VP's own assertions (200, ``status ==
# ok``, the field itself) stay the only basis for a PASSED — the PRD's
# decision point 4 requires the default claude path to be unchanged,
# and a probe that could pass a VP would change it.


#: The data-plane field the ``/health/data`` endpoint reports.
DATA_PLANE_FIELD = "plan_routing_rows"

#: The three states a data-plane observation can be in.  ``observed``
#: carries the value; the two ``not-observed`` states carry the reason
#: the field was missing.
OBSERVATION_OBSERVED = "observed"
OBSERVATION_SERVICE_UNREACHABLE = "not-observed(service-unreachable)"
OBSERVATION_FIELD_ABSENT = "not-observed(field-absent)"

#: The two further states a **negative** expectation can be in.  A VP
#: that expects a rejection (``{"status": 403}``) is asserting that a
#: guard is in effect, and the two ways that expectation can go
#: unproven are different faults with different repairs:
#:
#:   * ``not-observed(status-mismatch)`` — a response came back, but
#:     with another status: the guard was **bypassed**;
#:   * ``not-observed(body-absent)`` — the expected status arrived, but
#:     the answer no longer carries the text the criterion demands: the
#:     guard **degraded** silently.
OBSERVATION_STATUS_MISMATCH = "not-observed(status-mismatch)"
OBSERVATION_BODY_ABSENT = "not-observed(body-absent)"

#: The response header the criterion names.  A header is a **dimension**
#: of the answer, not a property of its body: the same answer can carry
#: a JSON body and declare ``text/plain``, and it can be refused before
#: it declares anything at all.  Booked down on its own, the header says
#: which of those happened.
CONTENT_TYPE_HEADER = "content-type"

#: The two further states a **header** observation can be in:
#:
#:   * ``not-observed(header-absent)`` — a response came back and it
#:     carried no such header: the endpoint answered without declaring
#:     what it answered;
#:   * ``not-observed(headers-unreadable)`` — a response came back, but
#:     the framework could not read its headers, so the header was
#:     never observed.  "I could not look" is not "it was not there",
#:     and the two have different repairs.
OBSERVATION_HEADER_ABSENT = "not-observed(header-absent)"
OBSERVATION_HEADERS_UNREADABLE = "not-observed(headers-unreadable)"

# ---------------------------------------------------------------------------
# The static entry — three halves, three books
# ---------------------------------------------------------------------------
#
# The static UI entry is the one path behind PRD decision point 4 ("默认
# claude 路径零变化") together with constraint 2: the root path is the
# one a browser navigates to, so it has to keep answering exactly as it
# did before.  Its criterion has three halves — the status, the media
# type and the title — and the three are **separate facts**: a ``200``
# with a JSON envelope answers the status half and neither of the
# others, and an answer that never came answers none of them.
#
# So the three are booked down on their own, each saying whether it was
# observed or why it was not.  One sentence for the three is what made
# the faults indistinguishable in the first place: "connection refused"
# reads the same whether the entry was never reached, was reached and
# answered with the wrong document, or was reached and answered with no
# title at all.

#: The media type the static entry answers with.  A criterion that
#: expects it is grading the document a browser navigates to.
STATIC_ENTRY_MEDIA_TYPE = "text/html"

#: The further state the **title** observation can be in when the
#: response came back without the title the criterion names: the
#: endpoint answered, and what it answered was not the document.  "The
#: service did not answer" and "the answer was not the page" are
#: different faults with different repairs.
OBSERVATION_TITLE_ABSENT = "not-observed(title-absent)"

#: The three halves of the static entry's criterion.  They are booked
#: down separately, and all three are always present together — a cell
#: that only appears when one of them was seen cannot be checked
#: afterwards.
STATIC_ENTRY_DIMENSIONS = ("status_code", "content_type", "title")

#: The side condition a **negative**, plan-scoped criterion carries: a
#: plan that does not exist must not be created by the request that asked
#: for it.  "The endpoint answered 404" and "the endpoint left the plan
#: alone" are two different facts, and only the first one is a status
#: code — the caller can read the status, but it can only *infer* the
#: second one, and it may not infer it from a request that never got
#: an answer.
ZERO_SIDE_EFFECT_CONDITION = "no-plan-created"

#: The further state the side-condition observation can be in when the
#: plans tree could not be read at all: the framework cannot say whether
#: the plan was created when it could not look.  "I could not look" is
#: not "nothing was created", and the two have different repairs.
OBSERVATION_PLANS_TREE_UNREADABLE = "not-observed(plans-tree-unreadable)"

#: The vocabulary of the **harness identity** cell: what is known about
#: the backend the process answering a service's port really runs under.
#:
#: The three answers are deliberately distinct, because they are three
#: different faults:
#:
#:   * ``observed`` — the backend was read out of the operating system
#:     and it is the declared one;
#:   * ``mismatch`` — it was read, and it is **not** the declared one:
#:     the "configured but not actually in effect" case, which is an
#:     acceptance failure and not something to paper over;
#:   * ``not-observed(service-unreachable)`` — nothing was listening, so
#:     there was no process to read: this round has **no evidence**
#:     either way, and that is not the same answer as the two above.
HARNESS_IDENTITY_OBSERVED = "observed"
HARNESS_IDENTITY_MISMATCH = "mismatch"
HARNESS_IDENTITY_NOT_OBSERVED_UNREACHABLE = (
    "not-observed(service-unreachable)"
)
HARNESS_IDENTITY_NOT_OBSERVED_UNREADABLE = "not-observed(unreadable)"
HARNESS_IDENTITY_NOT_OBSERVED_UNCHECKABLE = (
    "not-observed(uncheckable-harness)"
)

#: What the snapshot writes when the environment of the listener could
#: not be read at all.  Distinct from ``None``: ``None`` is a fact the
#: operating system reported (the variable is not set in that process),
#: ``"unknown"`` is the absence of a fact.
HARNESS_IDENTITY_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# Service identity — which process answered, and which code it runs
# ---------------------------------------------------------------------------
#
# The **harness identity** above answers "which backend is the process
# answering this port running under".  It does not answer the other
# half: *which code* is that process running.  A service can be running
# the right backend from a tree that is three commits behind, and a
# verdict graded on it says nothing about the code under repair — the
# report reads "PASSED" and the reader cannot tell that the request was
# answered by a process that predates the change.
#
# So the service identity is a second, independent observation, and it
# has three answers:
#
#   * ``confirmed`` — the process answering the port is the one the
#     round holds the claim on, and the tree it runs in is the tree the
#     round started it from;
#   * ``mismatch`` — the observation contradicts the claim: a different
#     process is answering the port, or the answering process runs a
#     different tree than the one the round started;
#   * ``unconfirmed`` — one side of the comparison is **blank**: nobody
#     is listening, the round holds no claim, the tree could not be
#     read.  A blank is never agreement — "we could not check" and
#     "it agrees" are two different sentences, and only the second one
#     may be read as a confirmation.
SERVICE_IDENTITY_CONFIRMED = "confirmed"
SERVICE_IDENTITY_MISMATCH = "mismatch"
SERVICE_IDENTITY_UNCONFIRMED = "unconfirmed"

#: The phases whose **dispatch record** carries the service identity.
#: The api phase has a snapshot and an event of its own; these two are
#: dispatched to a sub-agent, and nothing else in their path says which
#: process the criterion is graded against — a ``target_url`` is a page
#: to open, not a port the framework owns.
SERVICE_IDENTITY_DISPATCH_METHODS = ("ui_validation", "e2e")

#: "The environment of this process has not been read yet."  Distinct
#: from ``None``, which is the answer "it was read and it could not be
#: read" — the two must never be confused, because one of them is a
#: missing fact and the other is a missing *possibility*.
_ENV_UNREAD = object()

#: Markers that identify an evidence field carrying a **side
#: observation** — a fact gathered somewhere else (another VP, a
#: sub-agent, a neighbouring round) and pasted into this VP's verdict.
#: Such a field may be logged; it may never be part of the evidence a
#: verdict is read from, because a verdict that says "observed" on the
#: strength of somebody else's answer is a verdict nobody can check.
SIDE_EVIDENCE_KEY_MARKERS = (
    "corroborat",
    "side_evidence",
    "side_observation",
    "旁证",
    "other_vp",
    "neighbour",
    "sub_agent",
)

#: The audit artifact the api runner writes for every api_test VP: the
#: record of the response — status, **headers**, body — that lets an
#: operator re-check a verdict afterwards.  The verdict's own evidence
#: keeps the status and the body; the headers live here.
API_RESPONSE_ARTIFACT = "api_response.json"

#: How long the read-only health registration waits.  It is a
#: diagnostic, not the criterion: it must never delay a round by the
#: VP's own timeout.
HEALTH_PROBE_TIMEOUT_SECONDS = 5.0

#: How much of the health endpoint's answer is kept in the evidence.
HEALTH_PROBE_BODY_CHARS = 500


# ---------------------------------------------------------------------------
# Failure class — which of the three faults a failed api_test was
# ---------------------------------------------------------------------------
#
# "connection refused" is one sentence for three faults with three
# different repairs:
#
#   * the round never brought the service up;
#   * the service was up and died before the request went out;
#   * the service is listening and the request failed anyway.
#
# The class is decided by three facts the framework already holds: the
# round's **ready record** for the service (``services_ensured``), the
# **listener pid** the api-phase snapshot found at request time, and
# whether the **request itself** raised.


#: The round never had a ready service for the VP's target.
FAILURE_CLASS_NEVER_STARTED = "never-started"

#: The service was ready, and nothing is listening on the port the
#: request uses any more.
FAILURE_CLASS_DIED_BEFORE_REQUEST = "died-before-request"

#: Something is listening on that port and the request still failed.
FAILURE_CLASS_REQUEST_FAILED = "request-failed"


#: The exception types that mean *the request never reached a response*:
#: the connection could not be made, or the peer went away.  A request
#: that fails this way says the service is **not there** — which is a
#: fact about the round, not about the verification point that happened
#: to ask.
#:
#: A **timeout** is deliberately not in this set.  A service that
#: answers too late is still there, and calling that "the service is
#: gone" would be a wrong attribution — the very confusion this
#: classification exists to remove.
TRANSPORT_LOSS_ERROR_TYPES = frozenset({
    "URLError",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "ConnectionError",
    "BrokenPipeError",
    "gaierror",
    "RemoteDisconnected",
    "BadStatusLine",
    "IncompleteRead",
    "ProxyError",
})


def _read_only_get(url: str, timeout: float) -> Dict[str, Any]:
    """One read-only GET, reduced to what a verdict may record.

    A 4xx/5xx is a **response**: the endpoint answered, so the service
    is reachable — only a transport failure means "not reachable".  The
    distinction is the whole point of the registration.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url, method="GET", headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - an unreadable body is no body
            body = ""
    except Exception as exc:  # noqa: BLE001 - the probe never raises
        return {
            "reachable": False,
            "status_code": None,
            "error": f"{type(exc).__name__}: {exc}",
            "body": "",
        }
    return {"reachable": True, "status_code": status, "error": None, "body": body}


def _field_in_body(body: Optional[str], field: str) -> Tuple[bool, Any]:
    """Whether ``field`` is in ``body``, and its value when it is.

    The body is parsed as JSON when it can be.  When it cannot — a body
    the api runner truncated, a payload that is not JSON at all — the
    field *name* is still looked for, so a truncated answer is never
    reported as "the field was absent".
    """
    if not body:
        return False, None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        if field in payload:
            return True, payload[field]
        return False, None
    if f'"{field}"' in body:
        return True, None
    return False, None


def _declared_plans_dir(start_cmd: str) -> Optional[str]:
    """The plans tree a service's start command exports, if it names one.

    A service is told which plans tree it reads — ``export
    PDT_PLANS_DIR=/tmp/…/plans`` — and that tree is where a plan's
    directory would appear.  It is the only tree the framework may look
    in when it checks that a missing plan stayed missing: the tree *this*
    framework keeps its own plans in is a different tree whenever the
    service was pointed elsewhere, and looking in the wrong one answers
    a question nobody asked.
    """
    match = re.search(
        r"PDT_PLANS_DIR=(?P<path>[^;\s'\"]+)", str(start_cmd or ""),
    )
    if match is None:
        return None
    path = match.group("path").strip()
    return path or None


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
        #: 2026-10-27: which declared service each VP's URL came from.
        #: Keyed by VP id, valued by service name.
        #:
        #: A plan writes its api_test URL against the **placeholder**
        #: (``{{svc.api.url}}/health``), and the round expands it once,
        #: at preflight.  The expanded URL says *where* to go but no
        #: longer says *which service* that is — and the difference
        #: matters as soon as the service dies: the port alone can be
        #: answered by a stranger's process, so the round needs the
        #: name the plan asked for.
        #:
        #: **In memory only.**  The binding is the round's own
        #: bookkeeping; the plan on disk keeps its placeholders and is
        #: re-resolved every round, so nothing here may ever be written
        #: back to ``verification_plan.json``.
        self._vp_service_bindings: Dict[str, str] = {}
        #: 2026-10-27: the ports this round has already been told about —
        #: the ones that belong to no declared service.  Reported once
        #: each: a round asks about a port a dozen times, and that is
        #: one fact, not a dozen.
        self._unowned_ports_reported: set = set()
        #: 2026-10-25: when the round brought its services up. The
        #: api phase reports how long ago that was, so a verdict can
        #: say "the service has been dead for 40 seconds" instead of
        #: leaving the reader to guess. ``None`` until the preflight
        #: runs.
        self._services_ensured_at: Optional[float] = None
        #: 2026-10-25: the execution surface of this round (which tree
        #: the framework was imported from, which commit it is at, and
        #: whether that is the tree under test). Computed once and
        #: cached — the framework does not change under a running round.
        self._execution_surface_cache: Optional[Dict[str, Any]] = None
        #: 2026-10-26: the round's **lease loss** — set once, when the
        #: round discovers that it no longer owns the services it
        #: started (see :meth:`_assert_round_lease`). A round-level
        #: incident, not a per-VP one: it is recorded once, it stops the
        #: remaining VPs, and it keeps the round from dressing one
        #: accident up as nine identical per-VP failures.
        self._round_lease_lost: Optional[Dict[str, Any]] = None
        #: 2026-10-26: the round's **api-phase service loss** — set once,
        #: when a request went out and produced *no response at all*
        #: (the connection was refused, the peer went away).  A
        #: round-level incident for the same reason the lease loss is
        #: one: the service is gone, so the fact belongs to the round,
        #: not to the verification point that happened to ask first.
        #: It is recorded once, it stops the remaining api VPs instead
        #: of asking a dead port the same question nine times, and it
        #: keeps one accident from being dressed up as nine identical
        #: per-VP failures.
        self._api_phase_service_lost: Optional[Dict[str, Any]] = None
        #: 阶段归一化摘要（Phase 1/2 计数 + 被兜底提升的 VP），由
        #: :meth:`generate_verification_plan` 填充，供审计/展示读取。
        #: 2026-10-26：本轮判结论的**那棵树的 HEAD**（被验代码的提交）。
        #: 结论只对产生它的那份代码有效 —— 与
        #: :mod:`verification_verdict_provenance` 的命令指纹同一个方向。
        #: ``None`` = 还没查（不是"没有 HEAD"）。
        self._round_head: Optional[str] = None
        #: 2026-10-26：计划里的 VP 定义（id → 定义）。分派前按 id 查它：
        #: 查不到就不分派（见 :meth:`_build_verification_executor`）。
        self._vp_definitions: Dict[str, Dict[str, Any]] = {}

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
                build_delta_state,
                collect_acceptance,
                collect_new_tasks,
                load_delta_state,
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
                save_delta_state(
                    self.plan_dir,
                    build_delta_state(
                        state,
                        seen_task_ids=all_ids,
                        round_number=round_number,
                    ),
                )
                return {}

            if not new_tasks:
                save_delta_state(
                    self.plan_dir,
                    build_delta_state(
                        state,
                        seen_task_ids=all_ids,
                        round_number=round_number,
                    ),
                )
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
            # 2026-10-26（VP-031）：发出去的号必须**带着定义**落进计划
            # 文件，并在 state 里记下写入时的 HEAD。只在内存里发号，下一轮
            # 读到的仍是一份没有这条定义的计划 —— 那条 VP 于是无定义可
            # 分派，它此前挣到的失败又被原样沿用。
            head = self._current_project_head()

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
                    head=head,
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
            # 发出去的号抹掉。``build_delta_state`` 就是那份合并：它把上一轮
            # 账上的号与这一轮新发的号并成一份写下去。
            #
            # 2026-10-26：护栏丢弃的 VP **不会**留在计划文件里 ——
            # ``apply_delta_persisted`` 是**先过护栏、再落盘**（原子写 + 摘要
            # 校验），所以文件里从来不会有一条已经被判"跑不了"的定义，
            # 下一轮也就不会分派它。

            save_delta_state(
                self.plan_dir,
                build_delta_state(
                    state,
                    seen_task_ids=all_ids,
                    round_number=round_number,
                    summary=summary,
                    head=head,
                ),
            )
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

        The runtime map is the round's own record of that, and it keeps
        recording the port after the process dies.  ``ready`` says
        whether the service answers **right now**; it does not say where
        the service lives.  Gating on it (as this did until 2026-10-27)
        made the round forget the port it had relocated to the moment the
        service died — and a VP whose URL still named that port could
        then no longer be healed: the port was nobody's, so nothing was
        restarted, and the request ended in "connection refused" with
        nothing in the round's log saying a service had been killed.

        The declared port is **not** the fallback here either: it is the
        port a stranger was holding, and pointing a verification point at
        a stranger's process is the hazard the relocation exists to
        avoid.
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
    def _declaration_for_port(self, port: int) -> Tuple[str, Any]:
        """The declared service that listens on ``port``.

        Read through :meth:`_effective_declarations`, so the answer is
        the port the service *really* listens on — the relocated one
        when the preflight had to move it — which is the port the VP's
        URL was resolved to.

        When the effective declarations do not carry the port, the
        round's own **ledgers** are asked as well: the runtime map in
        memory and the ledger on disk.  Both record the port a service
        really listens on, and the ledger on disk records it next to the
        port it was declared on, so a reader can tell "it moved" from
        "it failed".

        A port that neither the declarations nor the ledgers name is
        **reported**, not swallowed: "no declared service" is the answer
        a reader needs, and a silent ``None`` reads like "nothing to
        do here" — which is how a VP whose service had died ended up
        asking a dead port the same question nine times.
        """
        try:
            wanted = int(port)
        except (TypeError, ValueError):
            return "", None
        for name, decl in self._effective_declarations().items():
            try:
                if int(getattr(decl, "port", 0) or 0) == wanted:
                    return str(name), decl
            except (TypeError, ValueError):
                continue

        from_ledger = self._declaration_from_ledger(wanted)
        if from_ledger is not None:
            return from_ledger

        # The port is nobody's.  Say so — once per port, so a round that
        # asks about it a dozen times still reads as one fact.
        if self._service_declarations:
            if wanted not in self._unowned_ports_reported:
                self._unowned_ports_reported.add(wanted)
                logger.warning(
                    "[verification_agent] port %s belongs to no declared "
                    "service of this round (declared: %s); the request "
                    "targets an endpoint this round does not own",
                    wanted, sorted(self._service_declarations),
                )
        else:
            # The default claude path declares no service at all, so
            # every port is nobody's — that is not news, and the round's
            # behaviour must not change because of it.
            logger.debug(
                "[verification_agent] port %s belongs to no declared "
                "service; this round declares none", wanted,
            )
        return "", None

    def _declaration_from_ledger(self, port: int) -> Optional[Tuple[str, Any]]:
        """The declared service this round's ledger says owns ``port``.

        Two ledgers are read: the round's in-memory runtime map, and the
        persisted one under the plan.  The **declaration** is still what
        is returned — the ledger says where the service lives, the
        declaration says which backend it runs under, and only the
        declaration may say that.

        ``None`` when neither ledger names this port.
        """
        runtime_map = self._service_runtime_map
        runtimes = getattr(runtime_map, "runtimes", None) or {}
        for name, runtime in runtimes.items():
            try:
                runtime_port = int(getattr(runtime, "port", 0) or 0)
            except (TypeError, ValueError):
                continue
            if runtime_port != int(port):
                continue
            key = str(name).strip().lower()
            decl = self._service_declarations.get(key)
            if decl is None:
                continue
            return key, self._declaration_at_port(decl, runtime_port)

        # The persisted ledger: the record of what this round started,
        # kept next to the port the service was declared on.
        for entry in self._ledger_entries():
            try:
                entry_port = int(entry.get("port") or 0)
            except (TypeError, ValueError):
                continue
            if entry_port != int(port):
                continue
            key = str(entry.get("name") or "").strip().lower()
            decl = self._service_declarations.get(key)
            if decl is None:
                continue
            return key, self._declaration_at_port(decl, entry_port)
        return None

    def _ledger_entries(self) -> List[Dict[str, Any]]:
        """The service entries of this plan's persisted ledger."""
        plan_dir = getattr(self, "plan_dir", None)
        if plan_dir is None:
            return []
        try:
            from service_manager import read_ledger
        except Exception:  # pragma: no cover - defensive
            return []
        try:
            ledger = read_ledger(plan_dir)
        except Exception:  # noqa: BLE001 - a broken record is no record
            return []
        entries = ledger.get("services") if isinstance(ledger, dict) else None
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    @staticmethod
    def _declaration_at_port(decl: Any, port: int) -> Any:
        """``decl`` as it runs on ``port``."""
        try:
            from service_manager import render_declaration
        except Exception:  # pragma: no cover - defensive
            return decl
        try:
            return render_declaration(decl, int(port))
        except Exception:  # pragma: no cover - defensive
            return decl

    def _known_service_pid(self, name: str) -> Optional[int]:
        """The pid the round last saw for ``name``, or ``None``."""
        runtime_map = self._service_runtime_map
        if runtime_map is None:
            return None
        runtime = (runtime_map.runtimes or {}).get(name)
        return getattr(runtime, "pid", None) if runtime is not None else None

    def _remember_service_runtime(self, name: str, runtime: Any) -> None:
        """Point the round's runtime map at a service's new process."""
        runtime_map = self._service_runtime_map
        if runtime_map is None:
            return
        try:
            runtime_map.runtimes[name] = runtime
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "[service_manager] could not update the runtime map for %s",
                name,
            )

    @staticmethod
    def _start_cmd_summary(decl: Any) -> str:
        """The command a relaunch of *decl* re-runs, as an event records it.

        Verbatim, because the command **is** the audit record: the
        mcode-side declaration differs from the claude-side one by an
        export (``PDT_HARNESS=mcode``) and two paths and nothing else,
        so a summary that was rewritten or trimmed would hide exactly
        the fact a reader of the round log has to check.

        Capped, so a pathologically long command cannot grow the event
        log without bound.  The cap sits far above any real
        declaration, so a real command is never trimmed.
        """
        command = str(getattr(decl, "start_cmd", "") or "")
        if len(command) > MAX_EVENT_COMMAND_CHARS:
            return command[:MAX_EVENT_COMMAND_CHARS] + " …[truncated]"
        return command

    def _verify_relaunched_harness(
        self, name: str, decl: Any, runtime: Any,
    ) -> Optional[Dict[str, Any]]:
        """Check that the relaunched process runs the declared backend.

        A declaration that pins ``PDT_HARNESS=mcode`` is a declaration
        about the **mcode** backend: the service it starts is the mcode
        side of the round.  A relaunch that comes up *without* that
        export still answers the service's port, so a VP graded on that
        process would be grading the endpoint and ignoring the backend —
        a "configured but not actually in effect" service, which the
        hard constraints call an acceptance failure.

        The expectation comes from the **declaration**, because the
        declaration is the contract: the command that was re-run
        verbatim is the one that says which backend the service is.  The
        observation comes from the **operating system**: the environment
        of the process that is actually answering the port.

        Returns ``None`` when the declaration says nothing about the
        harness — the default claude path pins none, and inventing a
        requirement there would change that path's behaviour for no
        reason.  Otherwise a dict with ``ok`` and, when it failed, the
        reason.  A value that cannot be read is a failure and not a
        pass: "I could not check" is not a reason to let a configured
        backend through unverified.
        """
        try:
            from service_manager import (
                HARNESS_ENV_VAR,
                declared_harness,
                harness_assignment,
                process_environment,
            )
        except Exception:  # pragma: no cover - defensive
            return None

        assignment = harness_assignment(decl)
        if assignment is None:
            # The declaration pins no harness: nothing to verify.
            return None

        expected = declared_harness(decl)
        if expected is None:
            return {
                "ok": False,
                "expected": None,
                "actual": None,
                "detail": (
                    f"service {name!r} declares {HARNESS_ENV_VAR}="
                    f"{assignment!r}, which is not a name this framework "
                    f"can check; the backend the relaunched service runs "
                    f"under cannot be verified"
                ),
            }

        pid = getattr(runtime, "pid", None)
        if not pid:
            return {
                "ok": False,
                "expected": expected,
                "actual": None,
                "detail": (
                    f"service {name!r} was relaunched but the framework "
                    f"has no pid to read {HARNESS_ENV_VAR} from; the "
                    f"{expected} backend is configured but not actually in "
                    f"effect, so it cannot be verified"
                ),
            }

        env = process_environment(pid)
        if env is None:
            return {
                "ok": False,
                "expected": expected,
                "actual": None,
                "detail": (
                    f"the environment of the relaunched process (pid {pid}) "
                    f"could not be read, so {HARNESS_ENV_VAR}={expected!r} "
                    f"could not be verified; the {expected} backend is "
                    f"configured but not actually in effect"
                ),
            }

        actual = env.get(HARNESS_ENV_VAR)
        if actual != expected:
            return {
                "ok": False,
                "expected": expected,
                "actual": actual,
                "detail": (
                    f"the relaunched process (pid {pid}) runs with "
                    f"{HARNESS_ENV_VAR}={actual!r}, but the declaration of "
                    f"service {name!r} pins {HARNESS_ENV_VAR}={expected!r}; "
                    f"the {expected} backend is configured but not actually "
                    f"in effect — the process answering the port is not the "
                    f"declared backend"
                ),
            }
        return {
            "ok": True, "expected": expected, "actual": actual, "pid": pid,
        }

    def _vp_request_url(self, vp: dict) -> str:
        """The VP's request URL, with its ``{{svc.*}}`` placeholders
        expanded.

        The plan on disk keeps the placeholders — they are re-resolved
        every round, against the port the service *really* listens on
        (the preflight may have had to relocate it).  A VP that reaches
        the relaunch path before that expansion happened still names the
        same service, so the same expansion is applied here: the
        effective declarations, which is what the preflight resolved
        the plan against.
        """
        request = vp.get("request") or {}
        url = str(request.get("url") or "").strip()
        if not url or "{{" not in url:
            return url
        try:
            from service_declaration import resolve_text
        except Exception:  # pragma: no cover - defensive
            return url
        try:
            resolved, _unresolved = resolve_text(
                url, self._effective_declarations(),
            )
        except Exception:  # pragma: no cover - defensive
            return url
        return str(resolved or url)

    def _vp_with_resolved_request_url(self, vp: dict) -> dict:
        """The VP with its request URL resolved against this round.

        A plan writes its api_test URL against the service
        **placeholder** — ``{{svc.api.url}}/health/data`` — because the
        port is declared once and re-resolved every round.  The
        placeholder is not a URL, so a request made against it never
        leaves the process: ``urllib`` raises ``unknown url type`` and
        the criterion is never observed, no matter how well the service
        is running.  That is what made the data plane invisible in the
        rounds that failed: the service was brought back, and the
        request still went nowhere.

        The resolution is the one the relaunch path already uses
        (:meth:`_vp_request_url`, against the **effective**
        declarations), so the request and the relaunch cannot disagree
        about which port the VP is about to talk to.

        A VP whose URL carries no placeholder is returned **unchanged**:
        the default claude path keeps byte-for-byte the behaviour it
        had.  An unresolvable placeholder is also left alone — the
        request then fails with its own reason, instead of being
        rewritten into something that looks like a URL but points
        nowhere.
        """
        request = vp.get("request")
        if not isinstance(request, dict):
            return vp
        url = str(request.get("url") or "")
        if "{{" not in url:
            return vp
        resolved = self._vp_request_url(vp)
        if not resolved or resolved == url:
            return vp
        resolved_vp = dict(vp)
        resolved_vp["request"] = dict(request)
        resolved_vp["request"]["url"] = resolved
        return resolved_vp

    def _vp_placeholder_service(self, vp: dict) -> str:
        """The service a ``{{svc.*}}`` placeholder in the VP's URL names.

        Empty when the URL names no service — a literal address, or a
        placeholder the vocabulary does not recognise.

        A URL that has **already been expanded** names no service
        literally, and that is the case that used to end the search
        here: the round resolves the placeholders once, at preflight,
        and the VP the executor dispatches carries the resolved URL.  A
        service that died after that had no name left to be found by,
        so the round could not tell *which* service to bring back — and
        a port alone is not an answer, because a stranger's process can
        hold a port too.

        So the name falls back to the **binding** the round recorded
        when it expanded the URL (see
        :meth:`_record_vp_service_bindings`).  The binding is the
        round's own bookkeeping and lives in memory only.
        """
        request = vp.get("request") or {}
        url = str(request.get("url") or "")
        if "{{" in url:
            try:
                from service_declaration import iter_placeholders
            except Exception:  # pragma: no cover - defensive
                pass
            else:
                try:
                    for _raw, name, _field in iter_placeholders(url):
                        return str(name)
                except Exception:  # pragma: no cover - defensive
                    pass
        return self._bound_service_for_vp(vp)

    def _bound_service_for_vp(self, vp: dict) -> str:
        """The service this VP's URL was resolved from, or ``""``.

        Read out of the round's own record of the resolution — the
        binding written by :meth:`_record_vp_service_bindings` when the
        placeholders were expanded.  Empty when the VP was never bound
        to a declared service.
        """
        vp_id = str(vp.get("id") or "").strip()
        if not vp_id:
            return ""
        return str(self._vp_service_bindings.get(vp_id) or "")

    def _declaration_for_service(self, name: str) -> Tuple[str, Any]:
        """The declared service called ``name``, as this round runs it.

        Read through :meth:`_effective_declarations`, so the answer is
        the port the service really listens on — the relocated one when
        the preflight had to move it.
        """
        key = str(name or "").strip().lower()
        if not key:
            return "", None
        decl = self._effective_declarations().get(key)
        if decl is None:
            return "", None
        return key, decl

    @staticmethod
    def _loopback_port(url: str) -> Optional[int]:
        """The port of a loopback URL, or ``None``.

        ``None`` for a URL that does not parse, carries no port, or
        points at another machine — only a service this round declared
        is ours to touch.
        """
        import urllib.parse

        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError:
            return None
        if not port:
            return None
        host = (parts.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return None
        return int(port)

    @staticmethod
    def _guard_vp_port(port: int) -> Optional[int]:
        """``port``, unless it is the backend's own control plane."""
        try:
            from service_freshness import PROTECTED_PORTS
        except Exception:  # pragma: no cover - defensive
            return None
        if int(port) in PROTECTED_PORTS:
            # The backend's own control plane — never ours to touch.
            return None
        return int(port)

    def _vp_target_port(self, vp: dict) -> Optional[int]:
        """The loopback port this VP's request is about to use.

        ``None`` for a request that does not target a service this round
        could own: no URL, a URL that does not parse, a host that is not
        this machine, or the backend's own control plane.

        The URL a plan writes is usually a **placeholder** —
        ``{{svc.api.url}}/health`` — and a placeholder has no netloc, so
        it cannot be ``urlsplit``-ed into a port.  The placeholder is
        therefore expanded first, against the same effective
        declarations the preflight resolved the plan against, so the
        port is the one the request will really use.  When the
        placeholder cannot be expanded into a parseable URL at all, the
        service it *names* still gives the port: giving up there would
        leave every placeholder URL unhealable, which is exactly the
        bug this fixes.

        One reader for "which port is this VP about to talk to", so the
        relaunch path and the backend-confirmation path cannot disagree
        about the port they are looking at.
        """
        request = vp.get("request") or {}
        url = str(request.get("url") or "").strip()
        if not url:
            return None

        resolved = self._vp_request_url(vp)
        port = self._loopback_port(resolved)
        if port is not None:
            return self._guard_vp_port(port)

        # The URL did not yield a loopback port.  Either it points
        # somewhere that is not ours — a third-party endpoint, which is
        # none of this round's business — or it still carries a
        # placeholder this round could not expand.  Only the second is
        # healable, and only when it names a service we declared.
        if "{{" not in resolved:
            return None
        name = self._vp_placeholder_service(vp)
        if not name:
            return None
        _service, decl = self._declaration_for_service(name)
        if decl is None:
            return None
        try:
            port = int(getattr(decl, "port", 0) or 0)
        except (TypeError, ValueError):
            return None
        if not port:
            return None
        return self._guard_vp_port(port)

    def _relaunch_service_for_vp(self, vp: dict) -> Optional[Dict[str, Any]]:
        """Make sure the service this VP is about to call is alive.

        The round resolved the service URL once, at preflight. A service
        that died after that — a foreign sweep taking it, a crash, an
        OOM — used to turn into a bare "connection refused" minutes
        later, with nothing in the round's log saying the service had
        been killed. This probes the port the request is about to use
        and, if it is dead, starts the service again **on the same
        port**, so the URL the VP was given stays true.

        Returns ``None`` when there is nothing to do — the port answers,
        or the request does not target a service this round declared —
        and a dict with ``ok`` when a restart was attempted. Never
        raises: a broken probe must not take the VP down, it must only
        mean "no relaunch".
        """
        vp_id = str(vp.get("id", "unknown"))
        port = self._vp_target_port(vp)
        if port is None:
            return None
        try:
            from service_freshness import probe_port
        except Exception:  # pragma: no cover - defensive
            return None
        try:
            probe = probe_port(int(port))
        except Exception:  # noqa: BLE001 - the probe never raises
            return None
        if probe.listening:
            return None

        # The service the URL *names* wins over the port: the name is
        # what the plan asked for, and a port alone can be answered by a
        # stranger's process.  A URL with no placeholder falls back to
        # the port, which is the only thing it carries.
        name, decl = "", None
        service_name = self._vp_placeholder_service(vp)
        if service_name:
            name, decl = self._declaration_for_service(service_name)
        if decl is None:
            name, decl = self._declaration_for_port(int(port))
        if decl is None:
            # Not a service this round declared — nothing to restart.
            return None

        # The service has to come back on the port the VP's URL names:
        # the URL is never rewritten to meet the service.  When the round
        # had to **move** the service — the declared port was held by a
        # stranger — the declaration still names the port it was
        # declared with, and restarting it there would leave the URL
        # pointing at nothing.  ``render_declaration`` is how the
        # service manager itself renders a declaration for a port, so
        # the command, the health URL and the log file all move with it.
        from service_manager import render_declaration

        decl = render_declaration(decl, int(port))

        old_pid = self._known_service_pid(name)
        reason = "port was not listening before the request"
        # The command that is about to be re-run, recorded with the
        # outcome.  The declaration's command is the *only* thing that
        # says which backend the relaunched service runs under — the
        # mcode side differs from the claude side by an export and two
        # paths and nothing else — so a round log that says "service
        # relaunched" without it cannot be checked afterwards.
        start_cmd = self._start_cmd_summary(decl)
        try:
            from service_manager import ensure_services, round_lease_context

            # The relaunched service is leased to this round too: a
            # sweep that cannot see the round must not stop it either.
            with round_lease_context(
                getattr(self, "plan_dir", None),
                round_id=self._round_id(),
            ):
                runtime_map = ensure_services(
                    self.project_dir, {name: decl},
                )
        except Exception as exc:  # noqa: BLE001 - ensure never raises
            logger.exception(
                "[service_manager] relaunch of service %s failed", name,
            )
            detail = f"{type(exc).__name__}: {exc}"
            self._safe_log_freshness_event(
                vp_id, "service_relaunch",
                {
                    "ok": False, "service": name, "port": int(port),
                    "old_pid": old_pid, "new_pid": None, "reason": reason,
                    "detail": detail, "start_cmd": start_cmd,
                },
            )
            return {
                "ok": False, "service": name, "port": int(port),
                "old_pid": old_pid, "new_pid": None, "reason": reason,
                "detail": detail, "start_cmd": start_cmd,
            }

        runtime = runtime_map.runtimes.get(name)
        new_pid = getattr(runtime, "pid", None)
        started_port = getattr(runtime, "port", None)
        ready = bool(getattr(runtime, "ready", False))
        if not ready:
            # The service did not come up.  The URL this VP holds names
            # the port it was supposed to answer on, and nothing is
            # answering it, so the request would fail for a reason the
            # verdict must name.
            detail = str(
                getattr(runtime, "detail", "") or "service did not come up"
            )
            if started_port is not None:
                detail = f"{detail} (listening port: {started_port})"
            self._safe_log_freshness_event(
                vp_id, "service_relaunch",
                {
                    "ok": False, "service": name, "port": int(port),
                    "old_pid": old_pid, "new_pid": new_pid, "reason": reason,
                    "detail": detail, "start_cmd": start_cmd,
                },
            )
            return {
                "ok": False, "service": name, "port": int(port),
                "old_pid": old_pid, "new_pid": new_pid, "reason": reason,
                "detail": detail, "start_cmd": start_cmd,
            }
        if started_port != int(port):
            # The service came up on a *different* port, which happens
            # when the port was taken by a stranger in the meantime and
            # the relocation rules moved us.  The URL this VP holds
            # names ``port`` and only ``port``; it is never rewritten to
            # follow the service, because a criterion graded against a
            # different endpoint is not the criterion.  So this is a
            # failure, and the reason says which port the service took.
            detail = str(getattr(runtime, "detail", "") or "")
            mismatch_reason = (
                f"the service came up on port {started_port}, not on port "
                f"{int(port)} that the request uses; the request's URL is "
                f"not rewritten to follow the service"
            )
            self._safe_log_freshness_event(
                vp_id, "service_relaunch",
                {
                    "ok": False, "service": name, "port": int(port),
                    "listening_port": started_port,
                    "old_pid": old_pid, "new_pid": new_pid,
                    "reason": mismatch_reason, "detail": detail,
                    "start_cmd": start_cmd,
                },
            )
            return {
                "ok": False, "service": name, "port": int(port),
                "old_pid": old_pid, "new_pid": new_pid,
                "reason": mismatch_reason, "detail": detail,
                "listening_port": started_port, "start_cmd": start_cmd,
            }

        # The process that just came up must be the service the
        # declaration describes — including the backend it runs under.
        # The relaunch re-runs the declaration's own command verbatim,
        # so the command is not in question; what is in question is the
        # process that came up from it.
        harness = self._verify_relaunched_harness(name, decl, runtime)
        if harness is not None and not harness.get("ok"):
            # The service is up but it is the *wrong* service: it answers
            # the port without running the backend the declaration pins.
            # Stop it rather than leave it holding the port, so the next
            # round starts from a free port instead of inheriting a
            # service that answers but is not the declared backend.
            try:
                from service_manager import stop_service

                stop_service(
                    runtime, decl,
                    plan_dir=getattr(self, "plan_dir", None),
                    round_token=self._round_id(),
                    reason="harness_check_failed",
                )
            except Exception:  # noqa: BLE001 - a stop never raises
                logger.warning(
                    "[service_manager] could not stop the relaunched "
                    "service %s after the harness check failed", name,
                )
            harness_detail = str(harness.get("detail") or "")
            self._safe_log_freshness_event(
                vp_id, "service_relaunch",
                {
                    "ok": False, "service": name, "port": int(port),
                    "old_pid": old_pid, "new_pid": new_pid, "reason": reason,
                    "detail": harness_detail, "start_cmd": start_cmd,
                },
            )
            return {
                "ok": False, "service": name, "port": int(port),
                "old_pid": old_pid, "new_pid": new_pid, "reason": reason,
                "detail": harness_detail, "start_cmd": start_cmd,
                "harness_check": harness,
            }

        # The round's runtime map still points at the dead process; the
        # next VP's placeholder resolution and PDT_SVC_* env must see the
        # new one.
        self._remember_service_runtime(name, runtime)
        try:
            from service_manager import record_runtime

            record_runtime(
                self.plan_dir, self.plan_dir.name, runtime,
                project_dir=self.project_dir,
                round_number=self._current_round,
            )
        except Exception:  # noqa: BLE001 - a ledger write never raises
            logger.warning(
                "[service_manager] could not record the relaunched "
                "service %s", name,
            )
        self._safe_log_freshness_event(
            vp_id,
            "service_relaunch",
            {
                "ok": True, "service": name, "port": int(port),
                "old_pid": old_pid, "new_pid": new_pid, "reason": reason,
                "start_cmd": start_cmd,
            },
        )
        self._safe_log_freshness_event(
            vp_id,
            "service_relaunched_before_vp",
            {
                "service": name,
                "port": int(port),
                "old_pid": old_pid,
                "new_pid": new_pid,
                "reason": reason,
                # The command that was re-run, verbatim.  The
                # declaration's command is what says which backend the
                # relaunched service runs under, so an event without it
                # cannot be checked afterwards — and a relaunch that
                # re-ran a *different* command is precisely the silent
                # downgrade this round has to catch.
                "start_cmd": start_cmd,
            },
        )
        return {
            "ok": True, "service": name, "port": int(port),
            "old_pid": old_pid, "new_pid": new_pid, "reason": reason,
            "detail": str(getattr(runtime, "detail", "") or ""),
            "start_cmd": start_cmd,
        }

    def _round_lease_for_service(
        self, name: str, port: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """The lease this round holds on one service, or ``None``.

        Read out of the plan's own records — the ledger, and the
        liveness marker behind it — so the answer is the one a reaper in
        another process would read too.  ``None`` when the plan holds no
        lease for the service: nobody claimed it, so nothing can be
        said about who is supposed to be serving it.
        """
        plan_dir = getattr(self, "plan_dir", None)
        if plan_dir is None:
            return None
        try:
            from service_manager import read_service_lease
        except Exception:  # pragma: no cover - defensive
            return None
        try:
            return read_service_lease(plan_dir, name=name, port=port)
        except Exception:  # noqa: BLE001 - a broken record is no lease
            return None

    @staticmethod
    def _lease_listener_mismatch(
        lease: Optional[Dict[str, Any]],
        pids: List[int],
        expected: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Is the process answering the port the one the round leased?

        ``None`` when there is nothing to contradict:

          * no lease, or a lease without a listener — nobody claimed
            the service, so nobody can say the answer came from the
            wrong process;
          * the listener is the process the round ensured;
          * the listener is in the process group the round started it
            in — the round starts its services through a shell, and the
            process that binds the port may be that shell's child
            rather than the shell itself;
          * the holder could not be named.

        "I could not check" is not "it is not mine", and a check that
        accused the round on a degraded probe would stop every round
        that declared a service.
        """
        if not lease:
            return None
        try:
            expected_pid = int(lease.get("listener_pid") or 0)
        except (TypeError, ValueError):
            return None
        if not expected_pid:
            return None
        live: List[int] = []
        for pid in (pids or []):
            try:
                live.append(int(pid))
            except (TypeError, ValueError):
                continue
        if not live:
            return None
        if expected_pid in live:
            return None
        try:
            pgid = int(lease.get("pgid") or 0)
        except (TypeError, ValueError):
            pgid = 0
        if pgid:
            for pid in live:
                try:
                    if os.getpgid(pid) == pgid:
                        return None
                except (ProcessLookupError, PermissionError, OSError):
                    continue
        service = str(lease.get("service") or "")
        backend = str(expected or "declared")
        return {
            "expected_pid": expected_pid,
            "actual_pid": live[0],
            "detail": (
                f"the process answering port {lease.get('port')} "
                f"(pid {live[0]}) is not the instance this round leased "
                f"for service {service!r} (pid {expected_pid}); its answer "
                f"is not the {service!r} service's answer, and no other "
                f"instance's answer may stand in for the {backend} "
                f"backend this verification point grades"
            ),
        }

    def _pinned_backend_capability(self, name: str) -> Dict[str, Any]:
        """What a declared service pins — for the verdict that lost it.

        ``{}`` when the service pins no backend: the default claude
        path, whose behaviour must not change.  The plan that declares
        an ``api-mcode`` service gets a sentence that names the backend
        by name, because "the mcode backend is unavailable" and "the
        service could not be restarted" are not the same fault and the
        repair loop acts on the difference.
        """
        if not name:
            return {}
        try:
            from service_manager import (
                HARNESS_ENV_VAR, declared_harness,
            )
        except Exception:  # pragma: no cover - defensive
            return {}
        _key, decl = self._declaration_for_service(name)
        if decl is None:
            return {}
        try:
            backend = str(declared_harness(decl) or "")
        except Exception:  # noqa: BLE001 - a broken declaration says none
            return {}
        if not backend:
            return {}
        return {
            "service": name,
            "backend": backend,
            "env_var": HARNESS_ENV_VAR,
            "detail": (
                f"service {name!r} — the one whose declaration pins "
                f"{HARNESS_ENV_VAR}={backend!r} — is not available: the "
                f"{backend} backend this verification point grades is not "
                f"running, and the answer of any other instance may not "
                f"stand in for it"
            ),
        }

    def _verify_service_backend_for_vp(
        self, vp: dict,
    ) -> Optional[Dict[str, Any]]:
        """Confirm the backend the service this VP grades on runs under.

        :meth:`_verify_relaunched_harness` checks the backend of the
        process the framework just started.  This is the other half: a
        service that was **already alive** was never checked, so a VP
        could be graded on a process that answers the port without
        running the backend its declaration pins — a service that is
        *configured* for the mcode backend but is not actually running
        under it, which the hard constraints call an acceptance failure
        rather than something to paper over.

        The endpoint cannot make that distinction itself: the same
        server code answers ``/api/system/active`` on both sides, with
        the same five fields.  So the confirmation has to be about the
        **process**: the expectation comes from the declaration (the
        contract) and the observation from the operating system (the
        environment of the process that is actually answering the port).

        Returns ``None`` when there is nothing to confirm:

          * the declaration pins no harness — the default claude path,
            which must keep behaving exactly as it did;
          * the request does not target a service this round declared.

        Otherwise a dict with ``ok`` and, when it failed, the reason.  A
        value that cannot be read is a failure and not a pass: "I could
        not check" is not a reason to let a configured backend through
        unverified.

        Three questions, in this order, and the first two are about
        the **instance** rather than about its environment:

          * **is the service there at all?** — a service that pins a
            backend and is not answering means the capability is
            unavailable.  The hard constraints call that an explicit
            failure; it must not be answered with a shrug ("there is no
            process to read an environment from"), and the request must
            not be sent to whatever else may be listening on the port;
          * **is the process answering the port the one this round
            leased for this service?** — the lease names it, and the
            answer of anybody else may not stand in for it.  The
            environment alone cannot say this: the default backend
            inherits the harness variable from the backend that started
            it, so it *claims* the mcode backend while running the
            claude one;
          * **does that process really run the declared backend?** — the
            environment, read out of the operating system.
        """
        try:
            from service_manager import (
                HARNESS_ENV_VAR,
                declared_harness,
                harness_assignment,
                process_environment,
            )
        except Exception:  # pragma: no cover - defensive
            return None

        port = self._vp_target_port(vp)
        if port is None:
            return None
        name, decl = self._declaration_for_port(port)
        if decl is None:
            # Not a service this round declared — nothing to confirm.
            return None
        assignment = harness_assignment(decl)
        if assignment is None:
            # The declaration pins no harness: nothing to verify.
            return None
        expected = declared_harness(decl)
        if expected is None:
            return {
                "ok": False, "expected": None, "actual": None,
                "service": name, "port": port,
                "detail": (
                    f"service {name!r} declares {HARNESS_ENV_VAR}="
                    f"{assignment!r}, which is not a name this framework "
                    f"can check; the backend the service runs under cannot "
                    f"be verified"
                ),
            }

        # 2026-10-26: the round's **lease** on this service.  It names
        # the process this round ensured, and it is the only record
        # that says which service that process was ensured *for* — the
        # round's two services share an owner and differ in the port.
        lease = self._round_lease_for_service(name, port)

        try:
            from service_freshness import probe_port

            probe = probe_port(port)
        except Exception:  # noqa: BLE001 - the probe never raises
            return None
        if not probe.listening:
            # The service this verification point grades is **not
            # there**.  For a declaration that pins a backend that is
            # the capability being unavailable, and the hard constraints
            # call it an explicit failure: the request must not go to
            # whatever else may be listening, and the verdict has to
            # name the capability that is missing.
            return {
                "ok": False, "expected": expected, "actual": None,
                "service": name, "port": port,
                "service_absent": True,
                "kind": "service_absent",
                "expected_pid": (lease or {}).get("listener_pid"),
                "actual_pid": None,
                "lease": lease or {},
                "detail": (
                    f"service {name!r} — the one whose declaration pins "
                    f"{HARNESS_ENV_VAR}={expected!r} — is not answering on "
                    f"port {port}: the {expected} backend this verification "
                    f"point grades is not available, and the answer of any "
                    f"other instance may not stand in for it"
                ),
            }
        pids = list(probe.pids or [])
        if not pids:
            return {
                "ok": False, "expected": expected, "actual": None,
                "service": name, "port": port,
                "detail": (
                    f"port {port} answers but the process behind it could "
                    f"not be identified, so {HARNESS_ENV_VAR}={expected!r} "
                    f"could not be verified; the {expected} backend is "
                    f"configured but not actually in effect"
                ),
            }

        # 2026-10-26: the process answering the port must be the one
        # this round leased **for this service**.  The environment
        # cannot say this on its own — the default backend inherits
        # the harness variable from the backend that started it, so it
        # claims the mcode backend while running the claude one — and a
        # criterion about the mcode backend must never be graded on the
        # claude instance's answer.
        mismatch = self._lease_listener_mismatch(lease, pids, expected)
        if mismatch is not None:
            return {
                "ok": False, "expected": expected, "actual": None,
                "service": name, "port": port,
                "listener_mismatch": True,
                "kind": "listener_mismatch",
                "expected_pid": mismatch["expected_pid"],
                "actual_pid": mismatch["actual_pid"],
                "lease": lease or {},
                "detail": mismatch["detail"],
            }

        pid = pids[0]
        env = process_environment(pid)
        if env is None:
            return {
                "ok": False, "expected": expected, "actual": None,
                "service": name, "port": port, "pid": pid,
                "detail": (
                    f"the environment of the process answering port {port} "
                    f"(pid {pid}) could not be read, so "
                    f"{HARNESS_ENV_VAR}={expected!r} could not be verified; "
                    f"the {expected} backend is configured but not actually "
                    f"in effect"
                ),
            }
        actual = env.get(HARNESS_ENV_VAR)
        if actual != expected:
            return {
                "ok": False, "expected": expected, "actual": actual,
                "service": name, "port": port, "pid": pid,
                "detail": (
                    f"the process answering port {port} (pid {pid}) runs "
                    f"with {HARNESS_ENV_VAR}={actual!r}, but the declaration "
                    f"of service {name!r} pins {HARNESS_ENV_VAR}={expected!r}; "
                    f"the {expected} backend is configured but not actually "
                    f"in effect — the process answering the port is not the "
                    f"declared backend"
                ),
            }
        return {
            "ok": True, "expected": expected, "actual": actual,
            "service": name, "port": port, "pid": pid,
        }

    # ------------------------------------------------------------------
    # Execution surface — which code is grading this round
    # ------------------------------------------------------------------

    def _execution_surface(self) -> Dict[str, Any]:
        """Which code is running this round, and which code is under test.

        ``framework_*`` describes the tree this process actually
        imported; ``project_*`` describes the tree the round is
        verifying.  ``same_tree`` answers the question an operator
        really has after a failed round: *is the code that graded this
        VP the code under repair?*

        Cached per agent — the framework does not change under a running
        round, and every answer costs two repository reads.
        """
        cached = self._execution_surface_cache
        if cached is not None:
            return cached
        framework_file = _framework_file()
        framework_root = _framework_root(framework_file)
        project_dir = os.path.realpath(str(self.project_dir))
        surface = {
            "framework_file": framework_file,
            "framework_root": framework_root,
            "framework_head": _git_head(framework_root),
            "project_dir": project_dir,
            "project_head": _git_head(project_dir),
            "same_tree": _path_is_inside(framework_file, project_dir),
            "pid": os.getpid(),
        }
        self._execution_surface_cache = surface
        return surface

    def _emit_execution_surface_provenance(self) -> Dict[str, Any]:
        """Record the execution surface of this round, once.

        Emitted when the verification executor is built — before any
        VP runs — so the round log says which tree graded it even when
        every VP passes.  A round whose log carries
        ``verification_executor_provenance`` with ``same_tree=false``
        was graded by a copy of the framework that is not the code
        under test, and its verdicts say nothing about the product.
        """
        surface = self._execution_surface()
        self._safe_log_freshness_event(
            "__executor__", "verification_executor_provenance", surface,
        )
        logger.info(
            "verification_executor_provenance %s",
            json.dumps(surface, ensure_ascii=False),
        )
        return surface

    def _seconds_since_services_ensured(self) -> Optional[float]:
        """How long ago the round brought its services up.

        ``None`` when the round never ensured them — a plan with no
        service declaration, or a preflight that did not run.
        """
        ensured_at = self._services_ensured_at
        if ensured_at is None:
            return None
        return round(time.monotonic() - ensured_at, 3)

    def _api_phase_service_snapshot(self, vp: dict) -> Dict[str, Any]:
        """What was listening on the services, at the moment before the request.

        The api phase is where "the request failed" and "the service was
        not there" have to be told apart, so the snapshot is taken
        **before** the request goes out — not after the failure is
        known.

        ``listener_pid`` is the process the operating system says is
        listening on the port (``null`` when nothing is), and
        ``known_pid`` is the process the round had started.  The pair
        separates "the service never came up" (both null) from "the
        service was killed between the phases" (a known pid, no
        listener).

        ``harness_identity`` books down, for every declared service that
        pins a **backend**, which backend the process answering that
        service's port really runs under.  The observation comes from the
        operating system — the environment of the very process the
        snapshot names as the listener — so an mcode copy on another
        port can never stand in for it.

        ``service_identity`` books down the **other** half: which
        process answered, which **tree** it runs in and which commit
        that tree is checked out at.  The harness identity says *which
        backend*; this one says *which code* — and the two together are
        what make "the change was made but the request ran against the
        old process" readable instead of invisible.
        """
        services: Dict[str, Any] = {}
        harness_identity: Dict[str, Any] = {}
        identities: Dict[str, Any] = {}
        for name, decl in sorted(self._effective_declarations().items()):
            port = int(getattr(decl, "port", 0) or 0)
            identity = self._service_identity(name, decl, port)
            # The raw environment of the answering process is a working
            # detail, not an observation: it holds whatever the operator
            # put in it.  Only the one variable that says which backend
            # the service runs under is booked down.
            env = identity.get("env")
            identity = self._without_raw_env(identity)
            listener_pid = identity.get("listener_pid")
            services[name] = {
                "port": port,
                # The port the service **really** listens on: the
                # preflight may have had to relocate it off a port a
                # stranger was holding, and the declared port is then
                # the stranger's.
                "actual_port": identity.get("port"),
                "listener_pid": listener_pid,
                "known_pid": self._known_service_pid(name),
                "seconds_since_services_ensured": (
                    self._seconds_since_services_ensured()
                ),
                # The tree the answering process runs in and the commit
                # that tree is checked out at.  Blank when it could not
                # be read — never a guess.
                "service_cwd": identity.get("service_cwd"),
                "service_head": identity.get("service_head"),
                "harness_env": identity.get("harness_env"),
                "start_cmd": identity.get("start_cmd"),
                "cmdlines": identity.get("cmdlines"),
                "process_start_epoch": identity.get("process_start_epoch"),
                "identity_status": identity.get("status"),
                "identity_head_status": identity.get("head_status"),
                "identity_detail": identity.get("detail"),
            }
            identities[str(name)] = identity
            harness = self._harness_identity_observation(
                decl, listener_pid, env=env,
            )
            if harness is not None:
                harness = dict(harness)
                harness["service"] = str(name)
                harness_identity[str(name)] = harness
        # The port the request is about to use, when the round declared
        # no service for it.  The reader still wants to know whether
        # anything was listening there — "no service was declared" is an
        # answer about the *plan*, not about the *port*.
        declared_ports = {
            int(info.get("port") or 0) for info in services.values()
        }
        target_port = self._vp_target_port(vp)
        target: Optional[Dict[str, Any]] = None
        if target_port is not None and int(target_port) not in declared_ports:
            target = {
                "port": int(target_port),
                "listener_pid": self._listener_pid(int(target_port)),
                "declared": False,
            }
        return {
            "vp_id": str(vp.get("id", "unknown")),
            "services": services,
            "target_port": target,
            "seconds_since_services_ensured": (
                self._seconds_since_services_ensured()
            ),
            "harness_identity": harness_identity,
            "service_identity": identities,
        }

    def _api_phase_service_snapshot_summary(
        self,
        vp: dict,
        identity: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Which instance answered this VP's request — the facts.

        A verdict says what the endpoint **answered**.  It does not say
        **who** answered, and without that a negative criterion is only
        evidence when the answer came from the instance this round
        declared: the same endpoint answers 404 on a stranger's process
        too, and "the route answered wrong" and "there was nobody
        listening" have to be told apart afterwards.

        The facts, every one of them readable afterwards:

          * ``port`` — the port the request went to;
          * ``listener_pid`` — the process the operating system says is
            answering that port (``null`` when nothing is);
          * ``lease_owner`` — who the ledger says owns the service on
            that port (the round that ensured it);
          * ``seconds_since_services_ensured`` — how long ago the round
            brought its services up.

        They ride on the ``api_test_executed`` event, so the event
        itself answers "which instance produced this verdict" instead of
        sending the reader to another file to find out.  The **service
        identity** — the process, the tree it runs in, the commit that
        tree is checked out at — rides on the same event, beside them.

        ``identity`` is the identity already observed for this request.
        When it is given its listener is the one that is reported, so
        the port is not probed a second time: the two facts on the
        event have to describe the **same** observation, and two probes
        are two observations.
        """
        port = self._vp_target_port(vp)
        if identity is not None:
            listener_pid = identity.get("listener_pid")
            if listener_pid is not None:
                listener_pid = int(listener_pid)
        else:
            listener_pid = self._listener_pid(port) if port else None
        lease_owner = ""
        if port is not None:
            leased = self._leased_listener_for_port(
                port, service_name=self._vp_service_key(vp),
            )
            if leased is not None:
                lease_owner = str(leased.get("owner") or "")
        return {
            "port": port,
            "listener_pid": listener_pid,
            "lease_owner": lease_owner,
            "seconds_since_services_ensured": (
                self._seconds_since_services_ensured()
            ),
        }

    # ------------------------------------------------------------------
    # Service identity — which process answered, and which code it runs
    # ------------------------------------------------------------------

    def _service_runtime(self, name: str) -> Any:
        """The round's own record of the service called ``name``."""
        runtime_map = self._service_runtime_map
        if runtime_map is None:
            return None
        return (runtime_map.runtimes or {}).get(str(name))

    def _probe_service_port(self, port: int) -> Any:
        """The operating system's answer about ``port``.

        ``None`` when the probe itself failed — a broken probe is no
        observation, and it must never be read as "nothing is
        listening".
        """
        if not port:
            return None
        try:
            from service_freshness import probe_port

            return probe_port(int(port))
        except Exception:  # noqa: BLE001 - a broken probe is no observation
            return None

    def _process_environment(
        self, pid: Optional[int],
    ) -> Optional[Dict[str, str]]:
        """The environment of *pid*, or ``None`` when it cannot be read."""
        if not pid:
            return None
        try:
            from service_manager import process_environment
        except Exception:  # pragma: no cover - defensive
            return None
        try:
            return process_environment(int(pid))
        except Exception:  # noqa: BLE001 - unreadable is a fact, not a crash
            return None

    @staticmethod
    def _tree_head(directory: str) -> Optional[str]:
        """The commit the tree at ``directory`` is checked out at.

        ``None`` when the directory is not inside a repository, or the
        repository has no commit yet.  Never the **executor's** head:
        this is the head of the tree the answering process runs in, and
        the two are different trees whenever the service runs from
        somewhere else.
        """
        if not directory:
            return None
        head = _git_head(directory)
        if not head or head == "unknown":
            return None
        return head

    def _resolve_claim_cwd(self, cwd: str) -> str:
        """The claimed working directory, as an absolute path.

        A declaration's ``cwd`` is **relative** to the project (that is
        the containment rule in :mod:`service_declaration`), so a
        relative claim is resolved against the project directory before
        it is compared with an observed one.  A claim that is not a
        directory at all resolves to ``""`` — and a blank claim is never
        read as agreement.
        """
        text = str(cwd or "").strip()
        if not text:
            return ""
        project_dir = getattr(self, "project_dir", None)
        if not project_dir:
            return ""
        try:
            path = Path(text)
            if not path.is_absolute():
                path = Path(os.path.realpath(str(project_dir))) / path
            resolved = os.path.realpath(str(path))
        except (OSError, ValueError):  # pragma: no cover - defensive
            return ""
        return resolved if os.path.isdir(resolved) else ""

    def _service_claim(self, name: str, port: int) -> Dict[str, Any]:
        """What the round's own records claim about this service.

        Two records of the same claim: the **lease** in the plan's
        ledger (what every reaper reads, and who owns the service) and
        the round's own **runtime** map (the process the round started,
        and the directory it started it in).

        Both are consulted, and neither may stand in for the other: a
        lease that names one process and a runtime map that names
        another are two claims, and the observation has to be compared
        with both of them.
        """
        claim: Dict[str, Any] = {
            "listener_pids": [],
            "cwd": "",
            "start_cmd": "",
            "owner": "",
            "sources": [],
        }
        if port:
            leased = self._leased_listener_for_port(
                int(port), service_name=str(name or ""),
            )
            if leased is not None:
                pid = leased.get("listener_pid")
                if pid:
                    claim["listener_pids"].append(int(pid))
                claim["owner"] = str(leased.get("owner") or "")
                claim["sources"].append("lease")
        runtime = self._service_runtime(name) if name else None
        if runtime is not None:
            pid = getattr(runtime, "pid", None)
            if pid:
                value = int(pid)
                if value not in claim["listener_pids"]:
                    claim["listener_pids"].append(value)
            cwd = str(getattr(runtime, "cwd", "") or "")
            if cwd:
                claim["cwd"] = cwd
            command = str(getattr(runtime, "start_cmd", "") or "")
            if command:
                claim["start_cmd"] = command
            claim["sources"].append("runtime")
        return claim

    def _service_identity(
        self, name: str, decl: Any = None, port: int = 0,
    ) -> Dict[str, Any]:
        """Which process is answering this service's port — and which code.

        The **observation** comes from the operating system: the pid
        that holds the port, the command line it was started with, the
        directory it runs in, the commit that directory is checked out
        at, and the backend its environment pins.

        The **comparison** comes from the round's own records — the
        lease and the runtime map.  Three answers, and the third one is
        the point:

          * ``confirmed`` — the process answering the port is the one
            the round holds the claim on, and it runs the tree the
            round started it from;
          * ``mismatch`` — the observation contradicts the claim: a
            different process is answering the port, or the answering
            process runs a different tree;
          * ``unconfirmed`` — one side of the comparison is **blank**:
            nobody is listening, the round holds no claim, or the tree
            could not be read.  "We could not check" and "it agrees" are
            two different sentences, and only the second one may be
            read as a confirmation.

        ``env`` carries the raw environment of the answering process.
        It is **not** part of the observation: the environment of a
        service holds whatever the operator put in it, and the event log
        is a file on disk.  Only ``harness_env`` — the one variable
        that says which backend the service runs under — is booked
        down.
        """
        runtime = self._service_runtime(name) if name else None
        declared_port = 0
        if decl is not None:
            try:
                declared_port = int(getattr(decl, "port", 0) or 0)
            except (TypeError, ValueError):
                declared_port = 0
        actual_port = int(port or 0) or declared_port
        if runtime is not None:
            try:
                actual_port = (
                    int(getattr(runtime, "port", 0) or 0) or actual_port
                )
            except (TypeError, ValueError):
                pass
        start_cmd = ""
        if decl is not None:
            start_cmd = self._start_cmd_summary(decl)
        if not start_cmd and runtime is not None:
            start_cmd = str(getattr(runtime, "start_cmd", "") or "")

        probe = self._probe_service_port(actual_port)
        listener_pid: Optional[int] = None
        cmdlines: List[str] = []
        start_epoch: Optional[float] = None
        service_cwd = ""
        if probe is not None:
            if probe.pids:
                listener_pid = int(probe.pids[0])
            cmdlines = list(probe.cmdlines)
            start_epoch = probe.process_start_epoch
            if probe.processes:
                service_cwd = str(probe.processes[0].get("cwd") or "")

        env = self._process_environment(listener_pid)
        harness_env: Optional[str] = None
        if env is not None:
            harness_env = env.get(_harness_env_var()) or None

        service_head = self._tree_head(service_cwd)
        claim = self._service_claim(name, actual_port)
        claimed_cwd = self._resolve_claim_cwd(claim.get("cwd") or "")
        claimed_head = self._tree_head(claimed_cwd)
        status, head_status, detail = self._service_identity_status(
            listener_pid=listener_pid,
            claimed_pids=list(claim.get("listener_pids") or []),
            service_head=service_head,
            claimed_head=claimed_head,
        )
        return {
            "service": str(name or ""),
            "declared_port": declared_port,
            "port": actual_port,
            "listener_pid": listener_pid,
            "cmdlines": cmdlines,
            "process_start_epoch": start_epoch,
            "service_cwd": service_cwd or None,
            "service_head": service_head,
            "claimed_listener_pids": list(claim.get("listener_pids") or []),
            "claimed_cwd": claimed_cwd or None,
            "claimed_head": claimed_head,
            "claimed_start_cmd": claim.get("start_cmd") or "",
            "claim_sources": list(claim.get("sources") or []),
            "lease_owner": claim.get("owner") or "",
            "harness_env": harness_env,
            "start_cmd": start_cmd,
            "status": status,
            "head_status": head_status,
            "detail": detail,
            # The raw environment — stripped before anything is written
            # down.  See the docstring.
            "env": env,
        }

    @staticmethod
    def _service_identity_status(
        *,
        listener_pid: Optional[int],
        claimed_pids: List[int],
        service_head: Optional[str],
        claimed_head: Optional[str],
    ) -> Tuple[str, str, str]:
        """The three answers a service identity can have.

        Returns ``(status, head_status, detail)``.

        The **head status** answers "is the answering process running
        the tree the round started it from".  The **status** answers the
        whole question, and it is the **worse** of the two halves: a
        confirmed process running an unreadable tree is not a confirmed
        identity, because half of the answer is blank.

        A blank is never agreement.  No listener, no claim, or an
        unreadable tree all land on ``unconfirmed`` — never on
        ``confirmed`` — because "we could not check" and "it agrees" are
        two different sentences and only the second one is evidence.
        """
        if listener_pid is None:
            return (
                SERVICE_IDENTITY_UNCONFIRMED,
                SERVICE_IDENTITY_UNCONFIRMED,
                "nothing is listening on the port: there is no process "
                "whose identity could be observed",
            )
        if not claimed_pids:
            return (
                SERVICE_IDENTITY_UNCONFIRMED,
                SERVICE_IDENTITY_UNCONFIRMED,
                "the round holds no claim on the process answering the "
                "port: there is nothing to compare the observation with, "
                "and no claim is not agreement",
            )
        if int(listener_pid) not in {int(pid) for pid in claimed_pids}:
            return (
                SERVICE_IDENTITY_MISMATCH,
                SERVICE_IDENTITY_UNCONFIRMED,
                f"the process answering the port (pid {listener_pid}) is "
                f"not the one the round holds the claim on "
                f"(pid {', '.join(str(pid) for pid in claimed_pids)})",
            )
        if service_head is None:
            return (
                SERVICE_IDENTITY_UNCONFIRMED,
                SERVICE_IDENTITY_UNCONFIRMED,
                "the process answering the port is the one the round "
                "holds the claim on, but the tree it runs in has no "
                "readable commit: which code it runs cannot be confirmed",
            )
        if claimed_head is None:
            return (
                SERVICE_IDENTITY_UNCONFIRMED,
                SERVICE_IDENTITY_UNCONFIRMED,
                "the process answering the port is the one the round "
                "holds the claim on, but the tree the round started it "
                "from has no readable commit: the two trees cannot be "
                "compared, and no comparison is not agreement",
            )
        if service_head != claimed_head:
            return (
                SERVICE_IDENTITY_MISMATCH,
                SERVICE_IDENTITY_MISMATCH,
                "the process answering the port is the one the round "
                "holds the claim on, but it runs a different commit "
                f"({service_head}) than the tree the round started it "
                f"from ({claimed_head})",
            )
        return (
            SERVICE_IDENTITY_CONFIRMED,
            SERVICE_IDENTITY_CONFIRMED,
            "the process answering the port is the one the round holds "
            "the claim on, and it runs the tree the round started it "
            "from",
        )

    def _vp_service_identity(self, vp: dict) -> Dict[str, Any]:
        """The identity of the service this VP is about.

        The api phase reads it off the request URL.  The ui / e2e
        phases read it off the VP's ``target_url`` — the field the plan
        prompt writes for them — and fall back to the services the VP
        says it uses.
        """
        name, decl, port = self._vp_service_target(vp)
        if not port and not name:
            return {}
        return self._without_raw_env(
            self._service_identity(name, decl, port),
        )

    @staticmethod
    def _without_raw_env(identity: Dict[str, Any]) -> Dict[str, Any]:
        """The identity without the raw environment of the process.

        The environment of a service holds whatever the operator put in
        it.  The event log and the verdict are files on disk, and the
        one fact about the environment that belongs in them — which
        backend the service runs under — is already booked down on its
        own.
        """
        return {
            key: value for key, value in identity.items() if key != "env"
        }

    def _snapshot_service_identity(
        self, snapshot: Optional[Dict[str, Any]], vp: dict,
    ) -> Dict[str, Any]:
        """The identity of the service this VP is about, out of the snapshot.

        The snapshot was taken **before** the request went out, so this
        is the identity of the process that was serving the port when
        the criterion was graded — which is the question a reader of a
        verdict has.

        When the snapshot carries no identity for this VP's service —
        the request went to a port no declared service holds — the
        identity is read now, so the cell says "unconfirmed" rather
        than vanishing.
        """
        identities = dict((snapshot or {}).get("service_identity") or {})
        if identities:
            name = self._vp_service_key(vp)
            identity = identities.get(name)
            if identity is None and len(identities) == 1:
                identity = next(iter(identities.values()))
            return dict(identity or {})
        return self._vp_service_identity(vp)

    def _vp_service_target(
        self, vp: dict,
    ) -> Tuple[str, Any, int]:
        """The service this VP is about: its name, its declaration, its port.

        Three sources, in this order:

          * the ``{{svc.*}}`` placeholder in the VP's URL — the name the
            plan itself wrote;
          * the VP's ``target_url`` (ui / e2e), whose port names the
            declaration the round is running;
          * the VP's ``uses_services`` — the services the plan says the
            criterion depends on.
        """
        name = self._vp_placeholder_service(vp)
        if not name:
            name = self._vp_uses_service(vp)
        port = self._vp_target_port(vp)
        if not port:
            port = self._vp_target_url_port(vp)
        decl = None
        if name:
            _key, decl = self._declaration_for_service(name)
            if decl is not None:
                name = _key
        if not name and port:
            name, decl = self._declaration_for_port(int(port))
        return str(name or ""), decl, int(port or 0)

    def _vp_uses_service(self, vp: dict) -> str:
        """The first declared service the VP says it uses, or ``""``."""
        uses = vp.get("uses_services") or []
        if not isinstance(uses, (list, tuple)):
            return ""
        for candidate in uses:
            key, decl = self._declaration_for_service(str(candidate))
            if decl is not None:
                return key
        return ""

    def _vp_target_url(self, vp: dict) -> str:
        """The URL this VP is about, with its ``{{svc.*}}`` placeholders
        expanded.

        The api phase keeps its URL in ``request.url``; the ui / e2e
        phases keep theirs in ``target_url``.  Both are answered here so
        that "which service does this criterion talk to" has one answer
        for every phase.
        """
        url = str(vp.get("target_url") or "").strip()
        if not url:
            url = self._vp_request_url(vp)
        if not url or "{{" not in url:
            return url
        try:
            from service_declaration import resolve_text
        except Exception:  # pragma: no cover - defensive
            return url
        try:
            resolved, _unresolved = resolve_text(
                url, self._effective_declarations(),
            )
        except Exception:  # noqa: BLE001 - defensive
            return url
        return str(resolved or url)

    def _vp_target_url_port(self, vp: dict) -> Optional[int]:
        """The loopback port the VP's ``target_url`` names, or ``None``."""
        url = self._vp_target_url(vp)
        if not url:
            return None
        port = self._loopback_port(url)
        if port is None:
            return None
        return self._guard_vp_port(port)

    @staticmethod
    def _service_identity_reason(identity: Dict[str, Any]) -> str:
        """One line naming the process and the code behind a service."""
        if not identity:
            return "service_identity: no service was observed"
        return (
            "service_identity: "
            f"service={json.dumps(identity.get('service') or None)} "
            f"port={json.dumps(identity.get('port'))} "
            f"listener_pid={json.dumps(identity.get('listener_pid'))} "
            f"service_cwd={json.dumps(identity.get('service_cwd'))} "
            f"service_head={json.dumps(identity.get('service_head'))} "
            f"harness_env={json.dumps(identity.get('harness_env'))} "
            f"status={json.dumps(identity.get('status'))} "
            f"head_status={json.dumps(identity.get('head_status'))}"
        )

    @staticmethod
    def _harness_identity_observation(
        decl: Any, listener_pid: Optional[int], env: Any = _ENV_UNREAD,
    ) -> Optional[Dict[str, Any]]:
        """The backend the process answering this service's port really runs.

        A declaration that pins ``PDT_HARNESS=mcode`` is a declaration
        about the **mcode** backend: the service it starts is the mcode
        side of the round.  Whether that backend was *actually* in
        effect cannot be read off the endpoint — the same server code
        answers ``/health`` on both sides — so it has to be read out of
        the **process**: the environment of the very pid that is
        answering the port.

        The three answers are distinct, and the third one is the one
        this cell exists for:

          * ``observed`` — the backend was read and it is the declared
            one;
          * ``mismatch`` — it was read and it is **not** the declared
            one: the "configured but not actually in effect" case, which
            the hard constraints call an acceptance failure;
          * ``not-observed(service-unreachable)`` — nothing was
            listening, so there was no process to read.  This round has
            **no evidence** either way, and saying so is not the same
            as saying the backend was in effect — nor as saying it was
            not.

        ``env`` is the environment of the listener, when the caller has
        already read it: reading it twice is two more processes for the
        same answer.  ``_ENV_UNREAD`` means "not read yet", and
        ``None`` means "read, and it could not be read".

        Returns ``None`` when the declaration pins no backend at all:
        the default claude path has nothing to observe, and inventing a
        requirement there would change that path's behaviour for no
        reason.
        """
        try:
            from service_manager import (
                HARNESS_ENV_VAR,
                declared_harness,
                harness_assignment,
                process_environment,
            )
        except Exception:  # pragma: no cover - defensive
            return None

        declared = harness_assignment(decl)
        if declared is None:
            # The declaration pins no backend: nothing to observe.
            return None

        expected = declared_harness(decl)
        identity: Dict[str, Any] = {
            "env_var": HARNESS_ENV_VAR,
            "declared": declared,
            "expected": expected,
            "observed": HARNESS_IDENTITY_UNKNOWN,
            "status": HARNESS_IDENTITY_NOT_OBSERVED_UNREADABLE,
            "listener_pid": listener_pid,
        }
        if not listener_pid:
            # Nothing is listening: there is no process to read, so this
            # round has no evidence — which is not the same answer as
            # "the backend was not in effect".
            identity["status"] = HARNESS_IDENTITY_NOT_OBSERVED_UNREACHABLE
            return identity
        if env is _ENV_UNREAD:
            env = process_environment(int(listener_pid))
        if env is None:
            # The listener is there and its environment could not be
            # read.  "I could not check" is not a reason to let a
            # configured backend through unverified, and it is not a
            # reason to declare it wrong either.
            identity["status"] = HARNESS_IDENTITY_NOT_OBSERVED_UNREADABLE
            return identity
        actual = env.get(HARNESS_ENV_VAR)
        identity["observed"] = actual
        if expected is None:
            # The declaration pins a value this framework cannot read
            # as a backend name: it cannot say whether the backend is in
            # effect, and it may not say it is.
            identity["status"] = HARNESS_IDENTITY_NOT_OBSERVED_UNCHECKABLE
        elif actual == expected:
            identity["status"] = HARNESS_IDENTITY_OBSERVED
        else:
            identity["status"] = HARNESS_IDENTITY_MISMATCH
        return identity

    @staticmethod
    def _harness_identity_reason(identity: Dict[str, Any]) -> str:
        """One line naming what is known about the backend behind a port."""
        service = identity.get("service")
        declared = identity.get("declared")
        observed = identity.get("observed")
        status = identity.get("status")
        listener_pid = identity.get("listener_pid")
        detail = (
            f"service {service!r} declares "
            f"{identity.get('env_var')}={declared!r}; "
            f"observed={json.dumps(observed)}; status={status}; "
            f"listener_pid={json.dumps(listener_pid)}"
        )
        if status == HARNESS_IDENTITY_NOT_OBSERVED_UNREACHABLE:
            detail += (
                f"; whether the {declared} backend was actually in effect "
                f"this round has no evidence: the service was unreachable "
                f"at request time, so there was no process to read"
            )
        elif status == HARNESS_IDENTITY_NOT_OBSERVED_UNREADABLE:
            detail += (
                f"; whether the {declared} backend was actually in effect "
                f"this round has no evidence: the environment of the "
                f"process answering the port could not be read"
            )
        elif status == HARNESS_IDENTITY_NOT_OBSERVED_UNCHECKABLE:
            detail += (
                f"; whether the {declared} backend was actually in effect "
                f"this round has no evidence: the declared value is not a "
                f"backend name this framework can check"
            )
        elif status == HARNESS_IDENTITY_MISMATCH:
            detail += (
                f"; the {declared} backend is configured but not actually "
                f"in effect: the process answering the port runs "
                f"{identity.get('env_var')}={observed!r}"
            )
        elif status == HARNESS_IDENTITY_OBSERVED:
            detail += f"; the {declared} backend was observed to be in effect"
        return "harness_identity: " + detail

    @staticmethod
    def _listener_pid(port: int) -> Optional[int]:
        """The process listening on ``port``, or ``None``.

        ``None`` for "nothing is listening" and for "the port could not
        be probed" alike: both mean the framework has no pid to name,
        and the liveness section says so either way.
        """
        if not port:
            return None
        try:
            from service_freshness import probe_port

            probe = probe_port(int(port))
        except Exception:  # noqa: BLE001 - a broken probe is no pid
            return None
        if not probe.pids:
            return None
        return int(probe.pids[0])

    def _record_api_phase_service_snapshot(self, vp: dict) -> Dict[str, Any]:
        """Take the api-phase service snapshot and record it in the log.

        The event is what makes the facts reviewable even when nobody
        reads the verdict: it sits in the round's own log, next to the
        request it describes.
        """
        snapshot = self._api_phase_service_snapshot(vp)
        self._safe_log_freshness_event(
            str(vp.get("id", "unknown")),
            "api_phase_service_snapshot",
            snapshot,
        )
        return snapshot

    @staticmethod
    def _execution_surface_reason(surface: Dict[str, Any]) -> str:
        """One line naming the code that graded the VP."""
        return (
            "execution_surface: "
            f"same_tree={surface.get('same_tree')} "
            f"framework_head={surface.get('framework_head')} "
            f"project_head={surface.get('project_head')} "
            f"framework_file={surface.get('framework_file')} "
            f"framework_root={surface.get('framework_root')} "
            f"project_dir={surface.get('project_dir')} "
            f"pid={surface.get('pid')}"
        )

    @staticmethod
    def _service_liveness_reason(snapshot: Dict[str, Any]) -> str:
        """One line naming what was alive when the request went out."""
        services = snapshot.get("services") or {}
        parts = [
            f"{name}(port={info.get('port')},"
            f"listener_pid={json.dumps(info.get('listener_pid'))},"
            f"known_pid={json.dumps(info.get('known_pid'))})"
            for name, info in sorted(services.items())
        ]
        target = snapshot.get("target_port")
        target_part = ""
        if target:
            target_part = (
                f" target_port={target.get('port')}"
                f"(listener_pid={json.dumps(target.get('listener_pid'))})"
            )
        return (
            "service_liveness: "
            f"seconds_since_services_ensured="
            f"{snapshot.get('seconds_since_services_ensured')} "
            f"services={'; '.join(parts) if parts else '(none declared)'}"
            f"{target_part}"
        )

    # ------------------------------------------------------------------
    # Failure class — which of the three faults a failed api_test was
    # ------------------------------------------------------------------

    def _vp_service_key(self, vp: dict) -> str:
        """The name of the service this VP talks to, or ``""``.

        The name the URL *names* wins over the port: a port alone can
        be answered by a stranger's process, and a stranger's process
        is not a service this round ever started.
        """
        name = self._vp_placeholder_service(vp)
        if name:
            key, decl = self._declaration_for_service(name)
            if decl is not None:
                return key
        port = self._vp_target_port(vp)
        if port is None:
            return ""
        key, _decl = self._declaration_for_port(port)
        return key

    def _services_ready_record(self) -> Dict[str, bool]:
        """Which services the round had ready, as it last recorded it.

        This is the round's runtime map — the map the ``services_ensured``
        event carries — read **before** this VP's relaunch: a relaunch
        that succeeds rewrites the map, and the question the failure
        class answers is what the round had *before* it started healing
        itself.
        """
        runtime_map = self._service_runtime_map
        if runtime_map is None:
            return {}
        record: Dict[str, bool] = {}
        for name, runtime in (runtime_map.runtimes or {}).items():
            record[str(name)] = bool(getattr(runtime, "ready", False))
        return record

    @staticmethod
    def _snapshot_listener_pid(
        snapshot: Dict[str, Any], port: Optional[int],
    ) -> Optional[int]:
        """The pid listening on ``port`` when the snapshot was taken.

        ``None`` for "nothing is listening" and for "the snapshot knows
        of no such port" alike — both mean the framework has no process
        to name for that port.
        """
        if port is None:
            return None
        for info in (snapshot.get("services") or {}).values():
            try:
                if int(info.get("port") or 0) == int(port):
                    return info.get("listener_pid")
            except (TypeError, ValueError):
                continue
        target = snapshot.get("target_port") or {}
        try:
            if int(target.get("port") or 0) == int(port):
                return target.get("listener_pid")
        except (TypeError, ValueError):
            return None
        return None

    def _classify_api_failure(
        self,
        vp: dict,
        snapshot: Dict[str, Any],
        ready_record: Dict[str, bool],
        request_error: Optional[str] = None,
        response_received: bool = False,
        note: str = "",
    ) -> Dict[str, Any]:
        """Which of the three faults a failed api_test was.

        The three facts, and the class each combination decides:

          * **never-started** — the round has no ready record for the
            service this VP talks to: it never had one, so the request
            had nothing to talk to;
          * **died-before-request** — the service *was* ready and
            nothing is listening on the port the request is about to
            use: it was up, and it is gone;
          * **request-failed** — something is listening on that port
            and the request still failed: the fault is in the request
            or in what it answered, not in the service's absence.

        The basis travels with the class, because the class alone cannot
        be checked afterwards: which service, which port, whether the
        round had it ready, who was listening, and what the request
        said.
        """
        service_key = self._vp_service_key(vp)
        ready = bool(ready_record.get(service_key, False))
        port = self._vp_target_port(vp)
        listener_pid = self._snapshot_listener_pid(snapshot, port)
        listening = listener_pid is not None
        target = service_key or f"port {port}"
        if not ready:
            failure_class = FAILURE_CLASS_NEVER_STARTED
            detail = f"the round never had a ready service for {target}"
        elif not listening:
            failure_class = FAILURE_CLASS_DIED_BEFORE_REQUEST
            detail = (
                f"service {service_key!r} was ready and nothing is "
                f"listening on port {port} any more"
            )
        else:
            failure_class = FAILURE_CLASS_REQUEST_FAILED
            detail = f"something is listening on port {port}"
            if request_error:
                detail += f" and the request failed: {request_error}"
            elif response_received:
                detail += (
                    " and answered, but the answer did not satisfy the "
                    "criterion"
                )
            else:
                detail += " and the request produced no response"
        if note:
            detail += f"; {note}"
        return {
            "failure_class": failure_class,
            "basis": {
                "service": service_key or None,
                "port": port,
                "service_ready": ready,
                "listener_pid": listener_pid,
                "request_error": request_error,
                "response_received": bool(response_received),
                "detail": detail,
            },
        }

    def _attach_failure_class(
        self,
        verdict: dict,
        vp: dict,
        snapshot: Dict[str, Any],
        ready_record: Dict[str, bool],
        request_error: Optional[str] = None,
        response_received: bool = False,
        note: str = "",
    ) -> dict:
        """Say which of the three faults a failed api_test was.

        The class goes into the verdict's evidence — it is what a reader
        checks afterwards — and the sentence goes into its reasons: the
        reason is what a reader reads.
        """
        classification = self._classify_api_failure(
            vp, snapshot, ready_record, request_error, response_received,
            note,
        )
        evidence = dict(verdict.get("evidence") or {})
        evidence["failure_class"] = classification["failure_class"]
        evidence["failure_class_basis"] = classification["basis"]
        verdict["evidence"] = evidence
        reasons = list(verdict.get("reasons") or [])
        reasons.append(
            f"failure_class: {classification['failure_class']} "
            f"({classification['basis']['detail']})"
        )
        verdict["reasons"] = reasons
        return verdict

    # ------------------------------------------------------------------
    # The request's own outcome — an HTTP verdict, or a transport fault
    # ------------------------------------------------------------------
    #
    # 2026-10-25, observed on a live round: the api service was taken
    # away two minutes into the round, and the round sent nine requests
    # at the dead port.  Every one came back "connection refused", every
    # one was written as that verification point's own FAILED verdict,
    # and the report carried nine identical failures for what was one
    # accident.  The framework had no way to tell the two apart: a
    # transport fault and an HTTP verdict both arrive as "the request
    # did not succeed".

    @staticmethod
    def _api_result_transport_error(result: Any) -> str:
        """The transport fault this result carries — or ``""``.

        The runner turns every failure into a verdict, so the request's
        own outcome has to be read back out of the evidence.  A request
        that produced **no response** has no ``status_code`` and an
        ``error``; a request that produced a response always has a
        status code, even when the response is a 500.  The two can
        therefore never be confused.
        """
        evidence = getattr(result, "evidence", None) or {}
        error = str(evidence.get("error") or "")
        if not error:
            return ""
        if evidence.get("status_code") is not None:
            # A response came back; whatever else went wrong, this was
            # not a transport fault.
            return ""
        return error

    @staticmethod
    def _assertions_executed(result: Any) -> int:
        """How many of the VP's assertions actually ran.

        A request that produced **no response** ran none of them: there
        was no response to compare against.  The number is written into
        the verdict's evidence so that a transport fault cannot be read
        afterwards as a broken contract (assertions failed) or as a
        satisfied one (assertions passed) — it is a fault of the
        transport, and the criterion was not observed at all.
        """
        evidence = getattr(result, "evidence", None) or {}
        passed = evidence.get("assertions_passed")
        failed = evidence.get("assertions_failed")
        if passed is None and failed is None:
            return 0
        try:
            return len(list(passed or [])) + len(list(failed or []))
        except TypeError:
            return 0

    @staticmethod
    def _transport_error_kind(error: str) -> str:
        """The kind of transport fault an error names — or ``""``.

        Only the faults that mean *the service is not there* count: the
        connection was refused, the peer went away, the name did not
        resolve.  A timeout is not one of them — a service that answers
        too late is still there — and an error this framework cannot
        classify is not one either: "I could not tell" must never be
        written down as "the service is gone".

        The refused connection is named apart from the rest, because it
        is the one fault the operating system vouches for: nothing was
        listening on that port.  Every other kind has to be asked about
        the port again before it may be called a loss.
        """
        text = str(error or "")
        name = text.split(":", 1)[0].strip()
        if name not in TRANSPORT_LOSS_ERROR_TYPES:
            return ""
        lowered = text.lower()
        if "timed out" in lowered or "timeout" in lowered:
            # A slow service is still there.
            return ""
        if "connection refused" in lowered or "errno 61" in lowered:
            return "connection-refused"
        if "reset by peer" in lowered or "errno 54" in lowered:
            return "connection-reset"
        return name

    def _round_service_port_claim(self, vp: dict) -> bool:
        """Is the port this request used **the round's own** service?

        The round's runtime map is the round's own record of the
        services it brought up — the same map the ``services_ensured``
        event carries.  A service on that map is the round's business: a
        request that got no answer from **it** is a fact about the
        round.

        An endpoint the round never brought up is a different thing.
        It may be a service somebody else runs, or an address outside
        this machine entirely, and a request that got no answer there
        says something about **that** endpoint, not about this round.
        The round must not stop its api phase — and must not call it a
        loss — over an endpoint it does not own.

        A service the round brought up but never had ready is not a
        claim either: that is the ``never-started`` fault, and it
        belongs to the verification point.
        """
        port = self._vp_target_port(vp)
        if port is None:
            return False
        runtime_map = getattr(self, "_service_runtime_map", None)
        runtimes = getattr(runtime_map, "runtimes", None) or {}
        for runtime in runtimes.values():
            try:
                runtime_port = int(getattr(runtime, "port", 0) or 0)
            except (TypeError, ValueError):
                continue
            if runtime_port != int(port):
                continue
            if not bool(getattr(runtime, "ready", False)):
                continue
            return True
        return False

    def _api_phase_service_gone(self, vp: dict, kind: str) -> bool:
        """Is the service this request was about really gone?

        A request that produced no response does not say the service is
        gone: a listener that hangs up on every connection is still
        there, and that fault belongs to the request — the
        ``request-failed`` class — not to the round.

        A refused connection is the operating system saying the port was
        free, and that is taken at its word.  Every other kind is asked
        about the port once more, and only a port that is provably free
        counts: a probe that could not read the port says nothing
        either way, and "I could not check" is not "it is gone".
        """
        if kind == "connection-refused":
            return True
        port = self._vp_target_port(vp)
        if port is None:
            return False
        try:
            from service_freshness import probe_port

            probe = probe_port(int(port))
        except Exception:  # noqa: BLE001 - a broken probe says nothing
            return False
        if probe.error:
            return False
        return not bool(probe.listening)

    @staticmethod
    def _vp_contract_fields(vp: dict) -> List[str]:
        """The top-level fields the VP's own assertions name.

        The contract belongs to the VP: the five fields of
        ``/api/system/active`` are the five ``json_path``s VP-003's
        assertions carry, and a framework that hard-coded them would be
        grading a criterion the plan never wrote.
        """
        fields: List[str] = []
        for assertion in vp.get("assertions") or []:
            if not isinstance(assertion, dict):
                continue
            path = str(assertion.get("json_path") or "").strip()
            if not path.startswith("$."):
                continue
            name = path[2:].split("[", 1)[0].split(".", 1)[0].strip()
            if name and name not in fields:
                fields.append(name)
        return fields

    def _contract_observation(
        self, vp: dict, response_body: Optional[str],
        source: Optional[Dict[str, Any]] = None,
        attestation: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """What the VP's own response said about each field it asserts.

        When the fields were observed, each one is recorded **with its
        type**: the contract is about shapes, and a body carrying
        ``"execution_running": "0"`` is broken in a way a body carrying
        ``0`` is not.

        When they were not observed, they are listed under
        ``not_observed`` rather than left out.  "The request failed" and
        "the request answered without these fields" are different
        faults, and an entry that is simply absent cannot tell them
        apart — which is the whole reason this cell exists.

        ``source`` is the backend the body came from, when the
        declaration pins one.  The fields and the backend are **one**
        fact: ``/api/system/active`` is served by the same server code
        on both sides of the round, so a body carrying the fields
        proves nothing about the instance that produced it.

        ``attestation`` is the round's **claim** on the instance (see
        :meth:`_contract_instance_attestation`).  When the claim does
        not hold, the body is somebody else's and **not one field is
        booked from it**: every field is listed under ``not_observed``
        and gets its own ``unobserved`` entry saying why, with the two
        pids that disagree.  A field booked from a stranger's answer is
        worse than a field booked as missing — it looks like a pass.
        """
        fields = self._vp_contract_fields(vp)
        observed: Dict[str, str] = {}
        not_observed: List[str] = []
        unobserved: Dict[str, Dict[str, Any]] = {}
        # 2026-10-26 (VP-009): the round's claim on the instance.  When
        # it does not hold, the answer is not this round's answer, and
        # no field of it may be booked.
        refuted = bool(attestation) and attestation.get("attested") is False
        payload: Any = None
        if response_body and not refuted:
            try:
                payload = json.loads(response_body)
            except (TypeError, ValueError):
                payload = None
        if refuted:
            for field in fields:
                not_observed.append(field)
                unobserved[field] = {
                    "status": "unobserved",
                    "reason": str(
                        attestation.get("reason") or "service_lost"
                    ),
                    "expected_pid": attestation.get("expected_pid"),
                    "actual_pid": attestation.get("actual_pid"),
                }
        elif isinstance(payload, dict):
            for field in fields:
                if field in payload:
                    observed[field] = type(payload[field]).__name__
                else:
                    not_observed.append(field)
        else:
            # Not a JSON object: a body the runner truncated, or a
            # payload that is not JSON at all.  The field *names* may
            # still be in it, so a truncated body is never reported as
            # "the field was absent" — the type, though, cannot be told.
            for field in fields:
                if f'"{field}"' in (response_body or ""):
                    observed[field] = "unknown"
                else:
                    not_observed.append(field)
        if not fields:
            status = "no-contract"
            detail = "the VP asserts no json_path field"
        elif refuted:
            status = "not-observed"
            detail = (
                f"not one of the {len(fields)} field(s) the VP asserts was "
                f"booked from the response: the instance that answered is "
                f"not the one this round holds the claim on, so its answer "
                f"is not this criterion's answer"
            )
        elif not not_observed:
            status = "observed"
            detail = (
                f"the VP's own response carried all {len(fields)} field(s) "
                f"it asserts"
            )
        elif not observed:
            status = "not-observed"
            detail = (
                f"the VP's own response carried none of the {len(fields)} "
                f"field(s) it asserts"
            )
        else:
            status = "partially-observed"
            detail = (
                f"the VP's own response carried {len(observed)} of the "
                f"{len(fields)} field(s) it asserts"
            )
        if response_body is None:
            detail += "; the request produced no response"
        if refuted:
            reason_detail = str(attestation.get("detail") or "")
            if reason_detail:
                detail += "; " + reason_detail
        source_detail = self._contract_source_detail(source, bool(observed))
        if source_detail:
            detail += "; " + source_detail
        observation = {
            "fields": fields,
            "observed": observed,
            "not_observed": not_observed,
            "status": status,
            "detail": detail,
        }
        if unobserved:
            observation["unobserved"] = unobserved
        if source is not None:
            source = dict(source)
            if refuted:
                # Nothing was booked, so nothing may be credited to the
                # declared backend either: the body is not its answer.
                source["confirmed"] = False
            observation["source"] = source
        return observation

    def _contract_source(
        self, vp: dict, snapshot: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """The backend the service this VP's contract comes from runs under.

        ``None`` when there is nothing to attribute: the declaration
        pins no backend — the default claude path, which must keep
        behaving exactly as it did — or the VP's own service cannot be
        named.

        The identity is the one the **snapshot** booked down: read out
        of the operating system, from the very process that was
        answering the port when the request went out.  The contract is
        therefore attributed to the process that really answered it,
        and never to a neighbour's copy on another port.
        """
        identity = dict((snapshot or {}).get("harness_identity") or {})
        if not identity:
            return None
        key = self._vp_service_key(vp)
        if not key:
            return None
        booked = identity.get(key)
        if not isinstance(booked, dict):
            return None
        source = dict(booked)
        # ``confirmed`` is the one thing the fields alone cannot say:
        # the declared backend was read out of the listener, so the
        # contract may be credited to that instance.
        source["confirmed"] = (
            str(source.get("status") or "") == HARNESS_IDENTITY_OBSERVED
        )
        return source

    @staticmethod
    def _pid_of(value: Any) -> Optional[int]:
        """A pid (or a process group id) as an int, or ``None``.

        ``None`` for "the record names no process" — which is not the
        same as "the process is 0", and must not be read as it.
        """
        try:
            pid = int(value)
        except (TypeError, ValueError):
            return None
        return pid or None

    def _contract_instance_claim(self, vp: dict) -> Optional[Dict[str, Any]]:
        """The instance this round holds a claim on, for this VP's service.

        A claim is a **process**: the one the round ensured for the
        service, named by the lease it holds on it.  Two records carry
        it, and in a live round they say the same thing — the ledger
        entry is written from the very runtime the round holds in
        memory:

          * the **lease**, when the ledger holds one: the record a
            reaper in another process reads;
          * the round's **own runtime record**, when it holds none: the
            map the ``services_ensured`` event carries.

        ``None`` when there is nothing to claim — the request does not
        target a service this round declared, or the service pins **no
        backend**.  The default claude path pins no backend and has
        always been graded on the answer of the process that was
        listening; that behaviour does not change.
        """
        port = self._vp_target_port(vp)
        if port is None:
            return None
        name, decl = self._declaration_for_port(port)
        if decl is None:
            return None
        if not self._pinned_backend_capability(name):
            # The service pins no backend: the default claude path.
            return None
        lease = self._round_lease_for_service(name, port)
        listener_pid: Optional[int] = None
        pgid = 0
        source = ""
        if lease:
            listener_pid = self._pid_of(lease.get("listener_pid"))
            pgid = self._pid_of(lease.get("pgid")) or 0
            source = str(lease.get("source") or "ledger")
        if listener_pid is None:
            runtime = None
            runtime_map = getattr(self, "_service_runtime_map", None)
            if runtime_map is not None:
                runtime = (runtime_map.runtimes or {}).get(name)
            listener_pid = self._pid_of(getattr(runtime, "pid", None))
            pgid = self._pid_of(getattr(runtime, "pgid", None)) or 0
            source = "runtime"
        return {
            "service": name,
            "port": int(port),
            "listener_pid": listener_pid,
            "pgid": pgid,
            "source": source,
        }

    def _contract_instance_attestation(
        self, vp: dict, snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """May the contract of this verification point be booked at all?

        The four fields of a contract are a fact about an **instance**.
        ``/api/system/active`` is served by the same server code on both
        sides of the round, so a body carrying the four fields proves
        nothing about the backend that produced it — and the
        **environment** of the listener does not prove it either: the
        default backend inherits ``PDT_HARNESS`` from the backend that
        started it, so a claude instance *claims* the mcode harness
        while running the claude one.

        What does prove it is the round's **claim**: the lease it holds
        on the service, which names the process it ensured for it.  So
        an answer may be booked only when the process that was
        answering the port **is** the one the claim names.

        Three answers, and the middle one is the point:

          * ``attested`` — the claim holds: the fields may be booked,
            and they belong to the instance the round holds;
          * ``refuted`` — the claim does not hold: the service is gone,
            or the port answers for somebody else.  The fields may
            **not** be booked, and every one of them says so, with the
            two pids that disagree;
          * ``not-applicable`` — the service pins no backend (the
            default claude path) or the request does not target a
            declared service: there is nothing to attest, and that
            path's behaviour does not change.
        """
        claim = self._contract_instance_claim(vp)
        if claim is None:
            return {
                "attested": None, "reason": "", "detail": "",
                "service": "", "port": None,
                "expected_pid": None, "actual_pid": None,
            }
        service = str(claim["service"])
        port = int(claim["port"])
        expected_pid = claim["listener_pid"]

        # The listener: the process that was answering the port when the
        # request went out.  The snapshot holds it; without a snapshot
        # the port is asked now.
        actual_pid: Optional[int] = None
        entry = (dict((snapshot or {}).get("services") or {})).get(service)
        if isinstance(entry, dict):
            actual_pid = self._pid_of(entry.get("listener_pid"))
        else:
            actual_pid = self._listener_pid(port)

        if expected_pid is None:
            return {
                "attested": False,
                "reason": "service_lost",
                "service": service,
                "port": port,
                "expected_pid": None,
                "actual_pid": actual_pid,
                "detail": (
                    f"the round holds no claim on the instance answering "
                    f"port {port} for service {service!r}, so the answer "
                    f"cannot be booked as that service's own; no other "
                    f"instance's answer may stand in for the backend this "
                    f"verification point grades"
                ),
            }
        if actual_pid is None:
            return {
                "attested": False,
                "reason": "service_lost",
                "service": service,
                "port": port,
                "expected_pid": expected_pid,
                "actual_pid": None,
                "detail": (
                    f"nothing is answering port {port} for service "
                    f"{service!r}: the instance this round holds the claim "
                    f"on (pid {expected_pid}) is not serving it"
                ),
            }
        mismatch = self._lease_listener_mismatch(
            {
                "listener_pid": expected_pid,
                "pgid": claim["pgid"],
                "port": port,
                "service": service,
            },
            [actual_pid],
            str(
                (self._pinned_backend_capability(service) or {}).get(
                    "backend",
                ) or "",
            ),
        )
        if mismatch is not None:
            return {
                "attested": False,
                "reason": "service_lost",
                "service": service,
                "port": port,
                "expected_pid": mismatch["expected_pid"],
                "actual_pid": mismatch["actual_pid"],
                "detail": mismatch["detail"],
            }
        return {
            "attested": True,
            "reason": "",
            "service": service,
            "port": port,
            "expected_pid": expected_pid,
            "actual_pid": actual_pid,
            "detail": "",
        }

    @staticmethod
    def _contract_source_detail(
        source: Optional[Dict[str, Any]], contract_observed: bool,
    ) -> str:
        """One clause naming the backend the contract came from.

        The four fields and the backend they came from are **one**
        fact, and the clause answers the question the fields cannot:

          * the contract was observed and the listener was the declared
            backend → it came from there;
          * the contract was not observed, and the listener was the
            declared backend → the cell says so, instead of leaving a
            reader to guess whether a contract was expected at all;
          * the listener was **not** the declared backend → the
            contract did not come from the declared instance, and the
            clause names the backend that answered the port instead;
          * nothing was listening → there is no evidence which backend
            the contract would have come from, and the clause says
            exactly that.
        """
        if not source:
            return ""
        status = str(source.get("status") or "")
        declared = source.get("declared")
        observed = source.get("observed")
        pid = source.get("listener_pid")
        env_var = str(source.get("env_var") or "PDT_HARNESS")
        if status == HARNESS_IDENTITY_OBSERVED:
            if contract_observed:
                return (
                    f"the contract came from the declared {declared} "
                    f"backend (pid {pid})"
                )
            return (
                f"no contract was observed; the service answering the port "
                f"runs the declared {declared} backend (pid {pid})"
            )
        if status == HARNESS_IDENTITY_MISMATCH:
            return (
                f"the contract did not come from the declared {declared} "
                f"backend: the service answering the port runs "
                f"{env_var}={observed!r} (pid {pid})"
            )
        if status == HARNESS_IDENTITY_NOT_OBSERVED_UNREACHABLE:
            return (
                f"no contract was observed: the service was unreachable at "
                f"request time, so there is no evidence which backend the "
                f"contract would have come from"
            )
        if status == HARNESS_IDENTITY_NOT_OBSERVED_UNREADABLE:
            return (
                f"no contract was observed: the environment of the process "
                f"answering the port could not be read, so there is no "
                f"evidence which backend the contract came from"
            )
        if status == HARNESS_IDENTITY_NOT_OBSERVED_UNCHECKABLE:
            return (
                f"no contract was observed: the declared value is not a "
                f"backend this framework can check, so there is no evidence "
                f"which backend the contract came from"
            )
        return ""

    # ------------------------------------------------------------------
    # Negative expectation — was the expected rejection observed?
    # ------------------------------------------------------------------

    @staticmethod
    def _vp_expected_statuses(vp: dict) -> List[int]:
        """The status codes the VP's own assertions expect.

        The status assertion is self-contained (``{"status": 403}``, or
        ``{"status": [401, 403]}`` for "either will do"), so the
        expectation is read straight off the declaration.  The framework
        never decides on its own what a VP should have seen.
        """
        expected: List[int] = []
        for assertion in vp.get("assertions") or []:
            if not isinstance(assertion, dict) or "status" not in assertion:
                continue
            declared = assertion.get("status")
            candidates = (
                declared if isinstance(declared, list) else [declared]
            )
            for candidate in candidates:
                if (
                    isinstance(candidate, int)
                    and not isinstance(candidate, bool)
                    and candidate not in expected
                ):
                    expected.append(candidate)
        return expected

    @staticmethod
    def _vp_body_targets(vp: dict) -> List[str]:
        """The body texts the VP's assertions require to be present.

        ``body_contains`` is the only subject that speaks about the raw
        body, and it is self-contained: the text *is* the expectation.
        """
        targets: List[str] = []
        for assertion in vp.get("assertions") or []:
            if not isinstance(assertion, dict):
                continue
            text = assertion.get("body_contains")
            if isinstance(text, str) and text and text not in targets:
                targets.append(text)
        return targets

    @staticmethod
    def _expected_status_text(expected: List[int]) -> str:
        """How the expected status reads in a sentence."""
        if not expected:
            return "(none)"
        return " or ".join(str(status) for status in expected)

    def _negative_expectation_observation(
        self,
        vp: dict,
        status_code: Optional[int],
        response_body: Optional[str],
        request_note: str = "",
        body_truncated: bool = False,
    ) -> Dict[str, Any]:
        """What the VP's own response said about the status it expects.

        A VP that expects a **rejection** — ``{"status": 403}`` — is
        asserting that a guard is in effect.  "The guard held" and "no
        response came back" are different facts, and only the first one
        may ever be reported as the expectation being met:

          * ``observed`` — the response carried the status the VP
            expects, and the body carried every text the VP requires;
          * ``not-observed(service-unreachable)`` — the request produced
            no response at all, so nothing proved the guard is in
            effect;
          * ``not-observed(status-mismatch)`` — a response came back,
            but with another status: the guard was **bypassed**;
          * ``not-observed(body-absent)`` — the expected status arrived,
            but the answer no longer carries the text the criterion
            demands: the guard **degraded**.

        The body texts the VP declares travel with it.  When the body
        was never seen, the texts it must carry are listed under
        ``not_observed`` rather than dropped: an expectation that
        vanishes from the report is a silent failure.
        """
        expected_statuses = self._vp_expected_statuses(vp)
        error_statuses = [
            status for status in expected_statuses
            if status < 200 or status >= 300
        ]
        targets = self._vp_body_targets(vp)
        expected_text = self._expected_status_text(expected_statuses)
        if status_code is None:
            observed = OBSERVATION_SERVICE_UNREACHABLE
            not_observed = list(targets)
            detail = request_note or "the request produced no response"
            if expected_statuses:
                detail += (
                    f"; the expected status {expected_text} was not observed"
                )
        else:
            not_observed = [
                target for target in targets
                if target not in (response_body or "")
            ]
            if expected_statuses and status_code not in expected_statuses:
                observed = OBSERVATION_STATUS_MISMATCH
                detail = (
                    f"the response answered {status_code} instead of the "
                    f"expected {expected_text}"
                )
            elif not_observed:
                observed = OBSERVATION_BODY_ABSENT
                detail = (
                    f"the response answered {status_code}, but the body "
                    f"text(s) "
                    + ", ".join(repr(text) for text in not_observed)
                    + " were not observed"
                )
            else:
                observed = OBSERVATION_OBSERVED
                detail = (
                    f"the response answered {status_code}, the status the "
                    f"VP expects"
                )
            if not_observed and observed != OBSERVATION_BODY_ABSENT:
                detail += (
                    "; the body text(s) "
                    + ", ".join(repr(text) for text in not_observed)
                    + " were not observed"
                )
        if body_truncated and not_observed:
            detail += (
                "; the body the runner kept is truncated, so the text may "
                "have been in the part that was cut"
            )
        return {
            "expects_error_status": bool(error_statuses),
            "expected_status": (
                expected_statuses[0] if len(expected_statuses) == 1
                else (expected_statuses or None)
            ),
            "observed_status": status_code,
            "observed": observed,
            "body_targets": targets,
            "not_observed": not_observed,
            "detail": detail,
        }

    @staticmethod
    def _negative_expectation_reason(observation: Dict[str, Any]) -> str:
        """The sentence a reader of the report gets about the expectation."""
        not_observed = observation.get("not_observed") or []
        reason = (
            f"negative_expectation: "
            f"expected_status={observation.get('expected_status')}, "
            f"observed={observation.get('observed')}"
        )
        if observation.get("observed_status") is not None:
            reason += f", observed_status={observation.get('observed_status')}"
        if not_observed:
            reason += ", not_observed=" + ", ".join(
                repr(text) for text in not_observed
            )
        reason += f"; {observation.get('detail')}"
        return reason

    @staticmethod
    def _guard_negative_expectation(
        verdict: dict, observation: Dict[str, Any],
    ) -> dict:
        """A negative expectation that was never observed cannot be met.

        Only the *no response* case is guarded here, because that is
        the one the criterion forbids being read as a pass: a report
        that says "the guard is in effect" when nothing ever answered.

        The body texts are **not** grounds for overturning the runner:
        the runner grades the body against the whole answer, while the
        framework only keeps an excerpt of it, so "the text is not in
        the excerpt" is not a reason to overturn a verdict the runner
        reached on the full body.
        """
        if str(verdict.get("status") or "") != "PASSED":
            return verdict
        if observation.get("observed") != OBSERVATION_SERVICE_UNREACHABLE:
            return verdict
        verdict["status"] = "FAILED"
        reasons = list(verdict.get("reasons") or [])
        reasons.append(
            "the negative expectation was never observed, so the criterion "
            f"cannot be met: {observation.get('detail')}"
        )
        verdict["reasons"] = reasons
        return verdict

    def _attach_negative_expectation(
        self,
        verdict: dict,
        vp: dict,
        status_code: Optional[int],
        response_body: Optional[str],
        request_note: str = "",
        body_truncated: bool = False,
    ) -> dict:
        """Book the negative expectation down in the verdict's evidence.

        The cell decides nothing: the verdict's status is the one the
        VP's own assertions produced.  What the cell adds is the
        bookkeeping a *negative* criterion needs — an expectation that
        was never observed has to be visible, or "the guard is in
        effect" and "nobody ever asked" read the same in the report.
        """
        observation = self._negative_expectation_observation(
            vp, status_code, response_body, request_note, body_truncated,
        )
        evidence = dict(verdict.get("evidence") or {})
        evidence["negative_expectation"] = observation
        verdict["evidence"] = evidence
        verdict = self._guard_negative_expectation(verdict, observation)
        if str(verdict.get("status") or "") != "PASSED":
            reasons = list(verdict.get("reasons") or [])
            reasons.append(self._negative_expectation_reason(observation))
            verdict["reasons"] = reasons
        return verdict

    # ------------------------------------------------------------------
    # The side condition — a plan that does not exist must stay missing
    # ------------------------------------------------------------------
    #
    # ``GET /api/plan/<id>/state`` answers 404 for a plan id it does not
    # know, and that 404 is what lets the caller tell "does not exist"
    # from "exists" without a second request.  The status code alone
    # does not carry that promise: a server that mints an empty plan for
    # an unknown id and then answers 404 has broken it, and the caller
    # cannot tell the difference from the outside.
    #
    # So the criterion carries a **side condition** — the missing plan
    # must not be created by the request that asked for it — and the
    # framework checks it against the plans tree, before and after the
    # request.  When the request produced no answer the condition is
    # **not observed**: a tree that did not change because nothing ran
    # is not evidence that the endpoint left the plan alone, and it may
    # never be reported as though it were.

    @staticmethod
    def _request_path(url: str) -> str:
        """The path a request URL addresses, placeholder or not.

        A plan writes ``{{svc.api.url}}/api/plan/<id>/state`` — the
        placeholder is not a URL, so the path is read off the string
        rather than off a parsed URL.  A literal address is stripped of
        its scheme and authority the same way, so both forms are read
        by one rule.
        """
        text = str(url or "").strip()
        if not text:
            return ""
        if "://" in text:
            rest = text.split("://", 1)[1]
            slash = rest.find("/")
            return rest[slash:] if slash >= 0 else "/"
        marker = text.find("}}")
        if marker >= 0:
            text = text[marker + 2:]
        if not text.startswith("/"):
            text = "/" + text
        return text

    @classmethod
    def _vp_plan_target(cls, vp: dict) -> Optional[str]:
        """The plan a request URL addresses, when it addresses one.

        The shape is the server's own: ``/api/plan/<plan_id>/…``.  A URL
        that addresses no plan — a health probe, the plan list — carries
        no plan-scoped side condition.
        """
        request = vp.get("request") or {}
        path = cls._request_path(str(request.get("url") or ""))
        match = re.match(r"^/api/plan/(?P<plan_id>[^/?#]+)", path)
        if match is None:
            return None
        return match.group("plan_id").strip() or None

    def _service_plans_dir(self, vp: dict) -> Tuple[Path, str]:
        """The plans tree the service this VP talks to reads.

        Three sources, in the order in which they answer that question
        directly:

          * the service's own start command exports ``PDT_PLANS_DIR`` —
            that is the tree the service was told to use;
          * the framework's own environment: the service is spawned by
            this process and inherits it;
          * the project's ``plans/`` tree, which is what the server
            falls back to when nothing says otherwise.

        The source travels with the answer, so a reader can tell a tree
        the service named from one the framework inferred.
        """
        _name, decl = self._declared_service_for_vp(vp)
        if decl is not None:
            declared = _declared_plans_dir(getattr(decl, "start_cmd", ""))
            if declared:
                return Path(os.path.expandvars(declared)).expanduser(), (
                    "declaration"
                )
        env_value = os.environ.get("PDT_PLANS_DIR")
        if env_value:
            return Path(env_value).expanduser(), "process-env"
        return Path(self.project_dir) / "plans", "project-default"

    @staticmethod
    def _plan_directory_snapshot(
        plans_dir: Path, plan_id: str,
    ) -> Dict[str, Any]:
        """Whether the plan's own directory is in the plans tree.

        "It is not there" and "I could not look" are different facts:
        only the first one may be read as "the plan was not created".
        """
        target = Path(plans_dir) / str(plan_id)
        try:
            os.stat(str(target))
        except FileNotFoundError:
            return {"exists": False}
        except OSError as exc:
            return {
                "exists": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {"exists": True}

    def _side_effect_snapshot(
        self, target: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """One snapshot of the plan's directory, when there is a target."""
        if target is None:
            return None
        return self._plan_directory_snapshot(
            target.get("plans_dir"), target.get("plan_id"),
        )

    def _zero_side_effect_target(self, vp: dict) -> Optional[Dict[str, Any]]:
        """The plan this VP's criterion says must not be created.

        The side condition belongs to a **negative**, plan-scoped
        criterion: the VP asks for the state of a plan and expects a
        rejection, so the plan has to still be missing afterwards.  A
        criterion that expects a success, or one that addresses no plan,
        carries no such condition — there is nothing to compare, and the
        framework does not invent a criterion the plan never wrote.
        """
        plan_id = self._vp_plan_target(vp)
        if not plan_id:
            return None
        expected = self._vp_expected_statuses(vp)
        if not any(400 <= status < 500 for status in expected):
            return None
        plans_dir, source = self._service_plans_dir(vp)
        return {
            "plan_id": plan_id,
            "plans_dir": plans_dir,
            "plans_dir_source": source,
        }

    def _zero_side_effect_observation(
        self,
        target: Optional[Dict[str, Any]],
        before: Optional[Dict[str, Any]],
        after: Optional[Dict[str, Any]],
        status_code: Optional[int] = None,
        request_note: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Whether the side condition was observed, and what it said.

        Three states, and they are the whole vocabulary:

          * ``observed`` — the request was answered, so the comparison of
            the two snapshots says something about the endpoint: the
            missing plan was left alone, or the request created it;
          * ``not-observed(service-unreachable)`` — the request produced
            no response at all, so nothing proved the endpoint leaves
            the missing plan alone.  The side effect stays **unknown**:
            it is not ``False``, because "nothing ran" is not "nothing
            was created";
          * ``not-observed(plans-tree-unreadable)`` — the request was
            answered, but the plans tree could not be read, so the
            comparison could not be made.
        """
        if target is None:
            return None
        plan_id = target.get("plan_id")
        plans_dir = target.get("plans_dir")
        base = {
            "condition": ZERO_SIDE_EFFECT_CONDITION,
            "plan_id": plan_id,
            "plans_dir": str(plans_dir) if plans_dir else None,
            "plans_dir_source": target.get("plans_dir_source"),
        }
        if status_code is None:
            return {
                **base,
                "status": OBSERVATION_SERVICE_UNREACHABLE,
                "side_effect": None,
                "before": before,
                "after": after,
                "detail": (
                    (request_note or "the request produced no response")
                    + "; the side condition was not observed: a plans tree "
                    "that did not change because nothing ran is not "
                    "evidence that the endpoint left the missing plan "
                    "alone"
                ),
            }
        before_exists = (before or {}).get("exists")
        after_exists = (after or {}).get("exists")
        if before is None or after is None or (
            before_exists is None or after_exists is None
        ):
            return {
                **base,
                "status": OBSERVATION_PLANS_TREE_UNREADABLE,
                "side_effect": None,
                "before": before,
                "after": after,
                "detail": (
                    f"the request answered {status_code}, but the plans tree "
                    f"could not be read, so it could not be said whether "
                    f"{plan_id!r} was created"
                ),
            }
        side_effect = bool(
            before_exists is False and after_exists is True,
        )
        if side_effect:
            detail = (
                f"the request answered {status_code} and the plans tree "
                f"gained {plan_id!r}: the answer says the plan does not "
                "exist while the request created it"
            )
        else:
            detail = (
                f"the request answered {status_code} and the plans tree "
                f"did not gain {plan_id!r}: the missing plan was left alone"
            )
        return {
            **base,
            "status": OBSERVATION_OBSERVED,
            "side_effect": side_effect,
            "before": before,
            "after": after,
            "detail": detail,
        }

    @staticmethod
    def _zero_side_effect_reason(observation: Dict[str, Any]) -> str:
        """The sentence a reader of the report gets about the condition."""
        return (
            f"zero_side_effect_observation: "
            f"condition={observation.get('condition')!r}, "
            f"plan_id={observation.get('plan_id')!r}, "
            f"observed={observation.get('status')}, "
            f"side_effect={observation.get('side_effect')!r}; "
            f"{observation.get('detail')}"
        )

    @staticmethod
    def _guard_zero_side_effect(
        verdict: dict, observation: Optional[Dict[str, Any]],
    ) -> dict:
        """A side condition that was not met cannot be a PASSED.

        Two ways it is not met:

          * it was **never observed** — the request produced no answer,
            so nothing proved the endpoint leaves the missing plan
            alone, and "the service was unreachable, so nothing was
            created" is a conclusion drawn from an execution that never
            happened;
          * it was **violated** — the answer said the plan does not
            exist while the request created it, which is exactly the
            ambiguity the criterion exists to rule out.
        """
        if observation is None:
            return verdict
        if str(verdict.get("status") or "") != "PASSED":
            return verdict
        if observation.get("status") != OBSERVATION_OBSERVED:
            reason = (
                "the zero-side-effect condition was never observed, so the "
                "criterion cannot be met"
            )
        elif observation.get("side_effect") is True:
            reason = (
                "the zero-side-effect condition was violated: the request "
                "created the plan the answer said does not exist"
            )
        else:
            return verdict
        verdict["status"] = "FAILED"
        reasons = list(verdict.get("reasons") or [])
        reasons.append(reason)
        verdict["reasons"] = reasons
        return verdict

    def _attach_zero_side_effect(
        self, verdict: dict, observation: Optional[Dict[str, Any]],
    ) -> dict:
        """Book the side condition down in the verdict's evidence.

        The cell decides nothing on its own: the verdict's status is the
        one the VP's own assertions produced.  What the cell adds is the
        bookkeeping a negative, plan-scoped criterion needs — a condition
        that was never observed has to be visible, or "the plan was left
        alone" and "nobody ever asked" read the same in the report.
        """
        if observation is None:
            return verdict
        evidence = dict(verdict.get("evidence") or {})
        evidence["zero_side_effect_observation"] = observation
        verdict["evidence"] = evidence
        verdict = self._guard_zero_side_effect(verdict, observation)
        if str(verdict.get("status") or "") != "PASSED":
            reasons = list(verdict.get("reasons") or [])
            reasons.append(self._zero_side_effect_reason(observation))
            verdict["reasons"] = reasons
        return verdict

    # ------------------------------------------------------------------
    # Data plane — was the field observed, and if not, why not
    # ------------------------------------------------------------------

    def _declared_service_for_vp(self, vp: dict) -> Tuple[str, Any]:
        """The service this VP talks to, as this round runs it.

        The service the URL *names* wins over the one that happens to
        answer the port: the name is what the plan asked for, and a
        port alone can be answered by a stranger's process.
        """
        name = self._vp_placeholder_service(vp)
        if name:
            key, decl = self._declaration_for_service(name)
            if decl is not None:
                return key, decl
        port = self._vp_target_port(vp)
        if port is None:
            return "", None
        return self._declaration_for_port(port)

    @staticmethod
    def _is_loopback_url(url: str) -> bool:
        """Whether ``url`` points at this machine.

        The registration only ever talks to a service this round owns,
        and the round only ever owns loopback ones.  A health URL that
        points elsewhere is therefore skipped rather than fetched: the
        plan is generated content, and the framework does not make
        outbound calls on its word — least of all a call the default
        claude path never made before.
        """
        import urllib.parse

        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            return False
        if parts.scheme not in ("http", "https"):
            return False
        host = (parts.hostname or "").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def _probe_declared_health_url(self, vp: dict) -> Dict[str, Any]:
        """Register, read-only, whether the declared health endpoint answers.

        One GET against the health URL the plan's declaration names,
        taken **before** the VP's own request.  It is a *registration*,
        not an assertion: it decides nothing about the verdict.  It
        exists so that a data-plane field which was not observed can say
        *why* — the service was not answering, or it was answering
        without the field.

        Taken before the request rather than after, so it records what
        the round **found** rather than what the round fixed: a service
        that had to be restarted is registered as "not answering", which
        is the fact the report needs.

        Never raises and never retries.  A health endpoint that does not
        answer is a fact worth recording, not an error to swallow.
        """
        name, decl = self._declared_service_for_vp(vp)
        if decl is None:
            return {
                "performed": False,
                "service": None,
                "health_url": None,
                "reachable": False,
                "status_code": None,
                "error": None,
                "reason": "no-declared-service",
                "body": "",
            }
        try:
            health_url = str(decl.resolved_health_url() or "").strip()
        except Exception:  # noqa: BLE001 - defensive
            health_url = ""
        if not health_url:
            return {
                "performed": False,
                "service": name or None,
                "health_url": None,
                "reachable": False,
                "status_code": None,
                "error": None,
                "reason": "no-declared-health-url",
                "body": "",
            }
        if not self._is_loopback_url(health_url):
            return {
                "performed": False,
                "service": name or None,
                "health_url": health_url,
                "reachable": False,
                "status_code": None,
                "error": None,
                "reason": "health-url-not-loopback",
                "body": "",
            }
        # 2026-10-27 (merge): the round asks the declared health
        # endpoint over the guarded road when it re-ensures a service
        # (:meth:`_service_reensure_gates`) and **keeps the answer**,
        # precisely so that "one service, one question, one answer"
        # holds.  This registration asks the very same endpoint one
        # moment later — so it reuses that answer instead of asking a
        # second time: two answers to one question could disagree, and
        # then the round's own precondition and its own report would
        # tell two different stories about the same service.
        recorded = self._recorded_health_answer(health_url)
        if recorded is not None:
            return {
                "performed": True,
                "service": name or None,
                "health_url": health_url,
                "reachable": bool(recorded.get("reachable")),
                "status_code": recorded.get("status_code"),
                "error": recorded.get("error") or None,
                "reason": None,
                "body": str(recorded.get("body") or ""),
                "source": "round-precondition",
            }
        answer = _read_only_get(health_url, HEALTH_PROBE_TIMEOUT_SECONDS)
        return {
            "performed": True,
            "service": name or None,
            "health_url": health_url,
            "reachable": bool(answer.get("reachable")),
            "status_code": answer.get("status_code"),
            "error": answer.get("error"),
            "reason": None,
            "body": answer.get("body") or "",
        }

    def _recorded_health_answer(self, health_url: str) -> Optional[Dict[str, Any]]:
        """The answer the round's own precondition already got for this URL.

        The round's precondition asks a service whether it is alive over
        the guarded road and keeps the answer
        (``_service_self_check_records``).  The api phase's registration
        asks the **same** endpoint, so it reads that answer instead of
        asking again.

        ``None`` when the round never asked this endpoint — then the
        registration asks it itself, and that answer is the freshest one
        there is.
        """
        wanted = str(health_url or "").strip().rstrip("/")
        if not wanted:
            return None
        records = getattr(self, "_service_self_check_records", None) or {}
        for record in records.values():
            if not isinstance(record, dict) or not record.get("asked"):
                continue
            url = str(record.get("url") or "").strip().rstrip("/")
            if url != wanted:
                continue
            status = record.get("status_code")
            return {
                "status_code": status,
                # A 4xx/5xx is still an answer: only a transport
                # failure means "not reachable" — the same rule as
                # :func:`_read_only_get`.
                "reachable": status is not None,
                "error": str(record.get("error") or ""),
                "body": str(record.get("body") or ""),
            }
        return None

    def _data_plane_observation(
        self,
        probe: Dict[str, Any],
        response_body: Optional[str],
        request_note: str = "",
    ) -> Dict[str, Any]:
        """Whether the data-plane field was observed, and when it was not, why.

        The three states are the whole vocabulary:

          * ``observed`` — the field was seen, with its value;
          * ``not-observed(service-unreachable)`` — the VP's own request
            produced no response, so there was no field to be seen in;
          * ``not-observed(field-absent)`` — a response came back, and
            the field was not in it.

        The label is decided by the VP's **own** traffic and by nothing
        else: the registration is an explanation, never a substitute
        assertion.  A health endpoint that happens to answer with the
        field does not make the observation "observed" — the VP's own
        request is what the criterion is about, and the VP's own
        assertions (200, ``status == ok``, the field itself) remain the
        only basis for a PASSED.
        """
        field = DATA_PLANE_FIELD
        vp_answered = response_body is not None

        vp_has, vp_value = _field_in_body(response_body, field)
        if vp_has:
            return {
                "field": field,
                "status": OBSERVATION_OBSERVED,
                "value": vp_value,
                "detail": "the VP's own response carried the field",
            }

        if vp_answered:
            return {
                "field": field,
                "status": OBSERVATION_FIELD_ABSENT,
                "value": None,
                "detail": (
                    "the VP's own response did not carry the field"
                    + self._probe_hint(probe)
                ),
            }

        return {
            "field": field,
            "status": OBSERVATION_SERVICE_UNREACHABLE,
            "value": None,
            "detail": (
                request_note or "the VP's own request produced no response"
            ) + self._probe_hint(probe),
        }

    @staticmethod
    def _probe_hint(probe: Dict[str, Any]) -> str:
        """One clause naming what the health registration saw."""
        if not probe.get("performed"):
            return (
                f"; no health endpoint was declared to register "
                f"({probe.get('reason')})"
            )
        if probe.get("reachable"):
            return (
                f"; the declared health endpoint {probe.get('health_url')!r} "
                f"answered {probe.get('status_code')}, so the service was up"
            )
        return (
            f"; the declared health endpoint {probe.get('health_url')!r} "
            f"did not answer ({probe.get('error')})"
        )

    @staticmethod
    def _public_probe(probe: Dict[str, Any]) -> Dict[str, Any]:
        """The registration as it is recorded: no raw body, just an excerpt."""
        body = str(probe.get("body") or "")
        public = {key: value for key, value in probe.items() if key != "body"}
        public["body_excerpt"] = body[:HEALTH_PROBE_BODY_CHARS]
        public["body_truncated"] = len(body) > HEALTH_PROBE_BODY_CHARS
        return public

    def _attach_data_plane_evidence(
        self,
        verdict: dict,
        vp: dict,
        probe: Dict[str, Any],
        response_body: Optional[str] = None,
        request_note: str = "",
        source: Optional[Dict[str, Any]] = None,
        attestation: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Put the api phase's evidence cells into the verdict's evidence.

        Two cells, and they stay separate on purpose:

          * the **data-plane cell** — the read-only registration says
            whether the service was answering, the observation says
            what happened to the field.  Collapsing them into one
            sentence is what made the two faults indistinguishable in
            the first place;
          * the **contract cell** — what the VP's own response said
            about each field its assertions name, field by field, with
            the fields that were not observed listed rather than
            dropped, and — when the declaration pins a backend — with
            the backend the body came from.

        Neither of them decides anything — the verdict's status is the
        one the VP's own assertions produced.
        """
        observation = self._data_plane_observation(
            probe, response_body, request_note,
        )
        evidence = dict(verdict.get("evidence") or {})
        evidence["health_url_probe"] = self._public_probe(probe)
        evidence["plan_routing_rows_observation"] = observation
        evidence["contract_observation"] = self._contract_observation(
            vp, response_body, source, attestation,
        )
        verdict["evidence"] = evidence
        return verdict

    # ------------------------------------------------------------------
    # The response header — a dimension of its own
    # ------------------------------------------------------------------

    def _api_artifact_path(self, vp_id: str) -> Path:
        """Where the api runner writes the audit artifact of this VP."""
        return (
            self.plan_dir / "vp_artifacts" / str(vp_id) / API_RESPONSE_ARTIFACT
        )

    def _api_artifact_stamp(self, vp_id: str) -> Optional[Tuple[int, int]]:
        """A fingerprint of the audit artifact, taken **before** the request.

        The artifact is rewritten by every api_test run, so its identity
        is what tells *this* round's copy from the previous round's.
        The runner writes through a temporary file and renames it into
        place, so a fresh copy always has a fresh identity — the stamp
        changes even when the answer is byte-for-byte the same one.

        ``None`` when there is no artifact yet, which is the ordinary
        state of the first round.
        """
        try:
            stat = self._api_artifact_path(vp_id).stat()
        except OSError:
            return None
        return (int(stat.st_ino), int(stat.st_mtime_ns))

    def _read_response_content_type(
        self, vp_id: str, stamp: Optional[Tuple[int, int]],
    ) -> Tuple[Optional[str], bool]:
        """The ``Content-Type`` of **this VP's own** response.

        The runner keeps the response **headers** in the audit artifact
        it writes for the VP — the verdict's own evidence holds the
        status and the body, not the headers — so the header dimension
        is read back from there.

        It is this VP's own traffic and nothing else: the artifact
        belongs to this VP's own id, and it is read only when it was
        written **during this call**.  A copy left over from an earlier
        round is a neighbour's observation, and a neighbour's
        observation may never be booked in as if it were this VP's.

        Returns ``(content_type, readable)``:

          * ``readable`` is ``False`` when there is no audit copy of
            *this* request — it was never written, or what is on disk
            is the previous round's.  The header is then unknown, and
            the cell says so instead of borrowing an answer;
          * ``content_type`` is the header's value, or ``None`` when the
            response carried no such header.
        """
        path = self._api_artifact_path(vp_id)
        try:
            stat = path.stat()
        except OSError:
            return None, False
        current = (int(stat.st_ino), int(stat.st_mtime_ns))
        if stamp is not None and current == stamp:
            # The copy on disk is the one that was already there: this
            # call did not write one.
            return None, False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, False
        headers = (
            payload.get("response_headers")
            if isinstance(payload, dict) else None
        )
        if not isinstance(headers, dict):
            return None, True
        value = headers.get(CONTENT_TYPE_HEADER)
        return (str(value) if value else None), True

    def _content_type_observation(
        self,
        status_code: Optional[int],
        content_type: Optional[str],
        headers_read: bool = True,
        request_note: str = "",
    ) -> Dict[str, Any]:
        """Whether the response's content type was observed, and when not, why.

        The four states are the whole vocabulary:

          * ``observed`` — the VP's own response carried the header,
            with its value.  Observing the header is **not** the same
            as it matching the criterion: a ``text/plain`` answer is
            observed, and it is the VP's own assertion that fails;
          * ``not-observed(service-unreachable)`` — the VP's own request
            produced no response, so there was no header to observe;
          * ``not-observed(header-absent)`` — a response came back and
            it carried no such header;
          * ``not-observed(headers-unreadable)`` — a response came
            back, but the framework could not read its headers, so the
            header was never observed at all.

        The label is decided by this VP's own traffic and by nothing
        else.  Another VP's answer, a sub-agent's ``curl``, a
        neighbouring round's note — those are side observations: they
        may be logged, they may never stand in for what this VP did not
        see.
        """
        if status_code is None:
            return {
                "header": CONTENT_TYPE_HEADER,
                "status": OBSERVATION_SERVICE_UNREACHABLE,
                "value": None,
                "detail": (
                    request_note or "the VP's own request produced no response"
                ),
            }
        if not headers_read:
            return {
                "header": CONTENT_TYPE_HEADER,
                "status": OBSERVATION_HEADERS_UNREADABLE,
                "value": None,
                "detail": (
                    f"the VP's own response answered {status_code}, but the "
                    "framework could not read its headers, so the content "
                    "type was not observed"
                ),
            }
        if not content_type:
            return {
                "header": CONTENT_TYPE_HEADER,
                "status": OBSERVATION_HEADER_ABSENT,
                "value": None,
                "detail": (
                    f"the VP's own response answered {status_code} without a "
                    f"{CONTENT_TYPE_HEADER!r} header"
                ),
            }
        return {
            "header": CONTENT_TYPE_HEADER,
            "status": OBSERVATION_OBSERVED,
            "value": content_type,
            "detail": (
                f"the VP's own response answered {status_code} and carried "
                f"{CONTENT_TYPE_HEADER!r}: {content_type!r}"
            ),
        }

    @staticmethod
    def _content_type_reason(observation: Dict[str, Any]) -> str:
        """The sentence a reader of the report gets about the header."""
        return (
            f"content_type_observation: header="
            f"{observation.get('header')!r}, "
            f"observed={observation.get('status')}, "
            f"value={observation.get('value')!r}; "
            f"{observation.get('detail')}"
        )

    # ------------------------------------------------------------------
    # The static entry — three halves, three books
    # ------------------------------------------------------------------

    @staticmethod
    def _vp_static_entry_media_type(vp: dict) -> Optional[str]:
        """The media type the criterion expects of the response.

        ``None`` for a criterion that does not grade a document.  The
        dimensions are the **criterion's**, and the framework never
        invents one: a JSON endpoint's dimensions are its status and its
        fields, not a page title, and booking a title down for it would
        be a fact about a document nobody asked for.
        """
        for assertion in vp.get("assertions") or []:
            if not isinstance(assertion, dict):
                continue
            name = str(assertion.get("header") or "").strip().lower()
            if name != CONTENT_TYPE_HEADER:
                continue
            for comparator in ("contains", "equals"):
                if comparator not in assertion:
                    continue
                expected = assertion.get(comparator)
                if (
                    isinstance(expected, str)
                    and STATIC_ENTRY_MEDIA_TYPE in expected.lower()
                ):
                    return expected
                break
        return None

    @staticmethod
    def _vp_static_entry_title(vp: dict) -> Optional[str]:
        """The title text the criterion requires the document to carry.

        Read off the criterion's own ``body_contains`` assertions — the
        same texts the runner grades the body against.  The framework
        never decides what the title is: it reports whether the title
        the criterion names was seen.
        """
        targets = VerificationAgent._vp_body_targets(vp)
        return targets[0] if targets else None

    @staticmethod
    def _document_title(body: Optional[str]) -> Optional[str]:
        """The ``<title>`` a document carries, if it carries one.

        Only used when the criterion names no title of its own: the
        framework then reports the document's own title, and never a
        title it made up.
        """
        if not body:
            return None
        match = re.search(
            r"<title[^>]*>(.*?)</title>", body,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None
        return match.group(1).strip() or None

    def _static_entry_observation(
        self,
        vp: dict,
        status_code: Optional[int],
        response_body: Optional[str],
        response_content_type: Optional[str] = None,
        response_headers_read: bool = True,
        request_note: str = "",
        body_truncated: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """The static entry's three halves, booked down one by one.

        ``None`` for a criterion that does not grade a document — the
        cell belongs to the criterion that declares it.

        The three halves are **separate books**, and each one says
        whether it was observed:

          * ``status_code`` — ``observed`` with the status the VP's own
            response carried, or ``not-observed(service-unreachable)``
            when the request produced no response at all;
          * ``content_type`` — the same two ways, plus the two ways a
            response can come back without saying what it is;
          * ``title`` — the same two ways, plus
            ``not-observed(title-absent)`` for an answer that came back
            without the title the criterion names.

        "Observed" is not "matching": a ``200`` with a JSON envelope is
        an observed status and an observed media type, and it is the
        criterion's own assertions that say the entry is broken.  The
        cell decides nothing.

        Everything here comes from **this VP's own** traffic.  A
        browser's answer on a copy of the service, another VP's answer,
        a neighbouring round's note — those are side observations, and
        they may never be booked in as if this request had answered.
        """
        media_type = self._vp_static_entry_media_type(vp)
        if media_type is None:
            return None
        title = self._vp_static_entry_title(vp)
        answered = status_code is not None

        if answered:
            status_cell = {
                "status": OBSERVATION_OBSERVED,
                "value": int(status_code),
                "detail": f"the VP's own response answered {status_code}",
            }
        else:
            status_cell = {
                "status": OBSERVATION_SERVICE_UNREACHABLE,
                "value": None,
                "detail": (
                    request_note or "the VP's own request produced no response"
                ),
            }

        content_type_cell = self._content_type_observation(
            status_code, response_content_type, response_headers_read,
            request_note,
        )

        if not answered:
            title_cell = {
                "status": OBSERVATION_SERVICE_UNREACHABLE,
                "value": None,
                "detail": (
                    request_note or "the VP's own request produced no response"
                ),
            }
        elif title is not None:
            if title in (response_body or ""):
                title_cell = {
                    "status": OBSERVATION_OBSERVED,
                    "value": title,
                    "detail": (
                        f"the VP's own response carried the title {title!r}"
                    ),
                }
            else:
                title_cell = {
                    "status": OBSERVATION_TITLE_ABSENT,
                    "value": None,
                    "detail": (
                        f"the VP's own response did not carry the title "
                        f"{title!r}"
                    ),
                }
        else:
            document_title = self._document_title(response_body)
            if document_title is not None:
                title_cell = {
                    "status": OBSERVATION_OBSERVED,
                    "value": document_title,
                    "detail": (
                        "the VP's own response carried a document titled "
                        f"{document_title!r}"
                    ),
                }
            else:
                title_cell = {
                    "status": OBSERVATION_TITLE_ABSENT,
                    "value": None,
                    "detail": "the VP's own response carried no title",
                }
        if (
            title_cell["status"] == OBSERVATION_TITLE_ABSENT
            and body_truncated
        ):
            # The runner grades the **whole** body; the framework only
            # keeps an excerpt of it.  The title may well be in the part
            # that was cut, so the cut says so rather than pretending
            # the document had none.
            title_cell["detail"] += (
                "; the body the runner kept is truncated, so the title may "
                "have been in the part that was cut"
            )

        cells = (status_cell, content_type_cell, title_cell)
        observed = sum(
            1 for cell in cells if cell.get("status") == OBSERVATION_OBSERVED
        )
        return {
            "criterion": "static-entry-document",
            "expected_media_type": media_type,
            "expected_title": title,
            "status_code": status_cell,
            "content_type": content_type_cell,
            "title": title_cell,
            "observed_dimensions": observed,
            "detail": (
                f"{observed} of {len(STATIC_ENTRY_DIMENSIONS)} dimension(s) of "
                "the static entry were observed in the VP's own response"
            ),
        }

    @staticmethod
    def _static_entry_reason(observation: Dict[str, Any]) -> str:
        """The sentence a reader of the report gets about the entry."""
        parts = []
        for dimension in STATIC_ENTRY_DIMENSIONS:
            cell = observation.get(dimension) or {}
            parts.append(
                f"{dimension}={cell.get('status')}({cell.get('value')!r})"
            )
        return (
            "static_entry_observation: "
            + ", ".join(parts)
            + f"; {observation.get('detail')}"
        )

    def _attach_static_entry_observation(
        self,
        verdict: dict,
        vp: dict,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
        response_content_type: Optional[str] = None,
        response_headers_read: bool = True,
        request_note: str = "",
        body_truncated: bool = False,
    ) -> dict:
        """Put the static entry's three halves into the verdict's evidence.

        The cell goes on **every** path the api phase can end on — the
        request was made and answered, the request never went out, the
        runner crashed — so a verdict always carries the same
        bookkeeping, whatever the fault was.  A cell that only appears
        on the happy path is a cell that cannot be checked afterwards.

        It decides nothing: the verdict's status is the one the VP's
        own assertions produced.
        """
        observation = self._static_entry_observation(
            vp, status_code, response_body, response_content_type,
            response_headers_read, request_note, body_truncated,
        )
        if observation is None:
            return verdict
        evidence = dict(verdict.get("evidence") or {})
        evidence["static_entry_observation"] = observation
        verdict["evidence"] = evidence
        if str(verdict.get("status") or "") != "PASSED":
            reasons = list(verdict.get("reasons") or [])
            reasons.append(self._static_entry_reason(observation))
            verdict["reasons"] = reasons
        return verdict

    @staticmethod
    def _is_side_evidence_key(key: Any) -> bool:
        """Whether an evidence field carries a side observation.

        A side observation is a fact gathered somewhere else — another
        VP's answer, a sub-agent's ``curl``, a neighbouring round's
        note — and pasted into this VP's verdict.  It may be logged; it
        may never be part of the evidence a verdict is read from.
        """
        name = str(key).lower()
        return any(marker in name for marker in SIDE_EVIDENCE_KEY_MARKERS)

    @classmethod
    def _strip_side_evidence(
        cls, evidence: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Drop every side observation from a verdict's evidence.

        The dropped fields are **named** in the log, so the drop is
        visible rather than silent: a round that produced one has
        something to say about it, and the report that reads the
        verdict never has to wonder whether the fact was borrowed.
        """
        dropped: List[str] = []

        def _walk(node: Any) -> Any:
            if isinstance(node, dict):
                kept: Dict[str, Any] = {}
                for key, value in node.items():
                    if cls._is_side_evidence_key(key):
                        dropped.append(str(key))
                        continue
                    kept[key] = _walk(value)
                return kept
            if isinstance(node, list):
                return [_walk(item) for item in node]
            return node

        return _walk(evidence or {}), dropped

    def _finish_api_verdict(
        self,
        verdict: dict,
        vp: dict,
        probe: Dict[str, Any],
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
        request_note: str = "",
        body_truncated: bool = False,
        response_content_type: Optional[str] = None,
        response_headers_read: bool = True,
        side_effect_target: Optional[Dict[str, Any]] = None,
        side_effect_before: Optional[Dict[str, Any]] = None,
        side_effect_after: Optional[Dict[str, Any]] = None,
        snapshot: Optional[Dict[str, Any]] = None,
        attestation: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Attach every api-phase evidence cell and hand the verdict back.

        The cells go on **every** path the api phase can end on — the
        request was made and answered, the request never went out, the
        runner crashed — so a verdict always carries the same
        bookkeeping, whatever the fault was.  A cell that only appears
        on the happy path is a cell that cannot be checked afterwards.

        Two things happen here that are not about the VP's own
        assertions:

          * the **header dimension** is booked down on its own, so a
            content type that was never observed says so instead of
            vanishing from the report;
          * every **side observation** is dropped from the evidence.
            A fact borrowed from another VP or from a sub-agent may be
            logged; it may never be part of what a verdict is read
            from.

        And one more cell rides along with them: the **side condition**
        of a negative, plan-scoped criterion — the missing plan must
        not be created by the request that asked for it.  It is booked
        down on the same terms as the others: a condition that was
        never observed says so, and it is never reported as satisfied
        because nothing ran.

        The **harness identity** rides along with them too: which
        backend the process answering a service's port really ran
        under, read out of that process at snapshot time.  It is a cell
        about the *service*, so it is booked down on every path — a
        criterion that was never graded against the mcode backend must
        say so, and "we could not tell" must never be read as "it was".

        ``attestation`` is the round's **claim** on the instance the
        contract would be booked from.  When the claim does not hold,
        the contract cell books no field value at all: every field is
        listed as ``unobserved`` with the reason and the two pids, so
        an answer that came from somebody else's instance can never be
        read as this criterion's own.
        """
        verdict = self._attach_data_plane_evidence(
            verdict, vp, probe, response_body, request_note,
            source=self._contract_source(vp, snapshot),
            attestation=attestation,
        )
        verdict = self._attach_negative_expectation(
            verdict, vp, status_code, response_body, request_note,
            body_truncated,
        )
        verdict = self._attach_zero_side_effect(
            verdict,
            self._zero_side_effect_observation(
                side_effect_target, side_effect_before, side_effect_after,
                status_code, request_note,
            ),
        )
        observation = self._content_type_observation(
            status_code, response_content_type, response_headers_read,
            request_note,
        )
        evidence = dict(verdict.get("evidence") or {})
        evidence["content_type_observation"] = observation
        # 2026-10-25 (VP-008): the **harness identity** — which backend
        # the process answering a service's port really ran under, read
        # out of that very process when the snapshot was taken.  It is
        # booked down on every path, so a verdict always says whether
        # the mcode backend was *observed* to be in effect, was observed
        # **not** to be, or could not be observed at all.  The last
        # answer is the one that used to be missing, and it is the one
        # that must never be read as the first.
        identity = dict((snapshot or {}).get("harness_identity") or {})
        evidence["harness_identity"] = identity
        # 2026-10-27: the **service identity** rides along with them:
        # which process answered this VP's request, which tree it runs
        # in, which commit that tree is checked out at, and whether the
        # round's own claim agrees with what was observed.  The harness
        # identity says *which backend*; this one says *which code* —
        # and without it a PASSED verdict cannot be told apart from one
        # that was answered by a process running a tree three commits
        # behind.
        service_identity = self._snapshot_service_identity(snapshot, vp)
        evidence["service_identity"] = service_identity
        verdict["evidence"] = evidence
        if str(verdict.get("status") or "") != "PASSED":
            reasons = list(verdict.get("reasons") or [])
            reasons.append(self._content_type_reason(observation))
            verdict["reasons"] = reasons
        if identity:
            # The reason names the service **this** VP is about: the
            # identity of a service the criterion does not depend on is
            # not this verdict's business.  When the VP's own service
            # cannot be named, every identity is stated — the fact is
            # never dropped.
            target_service = self._vp_service_key(vp)
            if target_service and target_service in identity:
                named = [target_service]
            elif target_service:
                named = []
            else:
                named = sorted(identity)
            if named:
                reasons = list(verdict.get("reasons") or [])
                for name in named:
                    reasons.append(
                        self._harness_identity_reason(identity[name]),
                    )
                verdict["reasons"] = reasons
        # 2026-10-25 (VP-007): the **static entry** rides along with the
        # header dimension.  Its three halves — the status, the media
        # type and the title — are booked down one by one, out of this
        # VP's own response, so a criterion that was never observed
        # says which of the three it never saw.  The cell decides
        # nothing: the verdict above is the one the VP's own assertions
        # produced.
        verdict = self._attach_static_entry_observation(
            verdict, vp,
            status_code=status_code,
            response_body=response_body,
            response_content_type=response_content_type,
            response_headers_read=response_headers_read,
            request_note=request_note,
            body_truncated=body_truncated,
        )
        verdict["evidence"], dropped = self._strip_side_evidence(
            verdict.get("evidence") or {},
        )
        if dropped:
            vp_id = str(vp.get("id", "unknown"))
            logger.warning(
                "[api_runner] dropped %d side-observation evidence field(s) "
                "from %s: %s",
                len(dropped), vp_id, ", ".join(dropped),
            )
            self._safe_log_freshness_event(
                vp_id, "side_evidence_dropped", {"fields": dropped},
            )
        return verdict

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
        # 2026-10-26: the round already lost its lease (see
        # :meth:`_assert_round_lease`) — this VP is **stopped**, not
        # failed: it never ran, so it has no verdict of its own. Failing
        # it would put one more red verification point in front of the
        # repair loop for a fault that already happened once.
        if getattr(self, "_round_lease_lost", None):
            return self._round_lease_lost_stop_verdict(vp_id)
        # 2026-10-26: the api phase already lost its service — a request
        # went out and produced no response at all.  This VP is
        # **stopped**, not run: asking a dead port the same question
        # again is what turned one accident into nine identical
        # failures.
        if getattr(self, "_api_phase_service_lost", None):
            return self._api_phase_service_lost_stop_verdict(vp_id)
        # 2026-10-26: the round's **lease** on its services, checked
        # before the request goes out.  On 2026-10-25 the api service was
        # taken away by another backend's shutdown hook two minutes into
        # the round, and the round never noticed: it sent nine requests
        # at a dead port and reported nine identical "connection
        # refused" failures.  The lease says who the service belongs to
        # — reading it back is what turns "the service is gone" into
        # "the service was taken away from me", and one round-level
        # accident into one round-level conclusion.
        loss = self._assert_round_lease()
        if loss is not None:
            return self._round_lease_lost_verdict(vp_id)
        # 2026-10-26: the round's heartbeat, refreshed before the request
        # goes out.  The lease is what keeps a foreign sweep from taking
        # the service while this request is in flight, so the heartbeat
        # has to be fresh at every request — not only at the preflight,
        # where it was written once and then left to age.
        self._touch_round_liveness()
        # 2026-10-26 (merge): main 的 api_test 前置门禁 + 本分支的租约、
        # 重启与绑定/记账，两边都要，顺序有讲究：
        #
        # 1) **先**问本分支的轮次级事故（租约丢失 / api 阶段服务丢失）：
        #    那是**轮次级**的一次事故，不是这条验证点自己的失败。
        # 2) **再**是本分支的注册与重启：注册把「服务当时答没答」记下来，
        #    重启把死在半路的服务拉回来 —— 拉不回来是 FAILED（判据根本没有
        #    被观测到，不能藏起来不交给修复循环）。
        # 3) **然后**问 main 的门禁（``_ensure_api_test_services``）：它判
        #    BLOCKED —— 环境没就绪，不是产品的锅。
        # 4) **最后**才绑定与记账：门禁放行之后请求确实会发出去，这时才
        #    记录地址（这个 VP 会被发到哪儿、绑定的是哪个实例）并把 attempt
        #    计数 +1。
        #
        #    判 BLOCKED —— 那时请求一次都没发出去，所以本分支的 attempt 计数
        #    必须是 0（「0 = 从没发过请求」这条不变量不能破）。
        #    这时才记录地址（这个 VP 会被发到哪儿、绑定的是哪个实例）并把
        #    attempt 计数 +1。
        #
        # 2026-10-27 (merge): 第 2 步与第 3 步的先后是被两边各自的测试
        # 钉住的，两条规则说的**不是**同一件事：
        #
        # * main 的门禁问的是「这条验证点声明的服务现在能用吗」——端口在听
        #   **且**健康端点答话；不能用就是环境没就绪 → BLOCKED（infra，可重试，
        #   不计同因阈值）。
        # * 本分支的重启问的是「这条请求要打的那个服务还在吗」——不在就拉它，
        #   拉不回来同样是环境没就绪 → BLOCKED（见下面 2026-10-27 的更正）。
        #
        # 所以顺序是**先重启、后门禁**：服务被拉回来之后门禁照样问它（端口在
        # 听但健康端点哑了 → 仍然是 BLOCKED），而拉都拉不回来的那种情况由
        # 重启自己给出 BLOCKED —— 它比门禁更具体（它知道是哪个服务、哪个
        # 端口、为什么没拉回来），但**判据不变**。反过来（门禁在前）的话，
        # 门禁会先判成 BLOCKED，重启这一次“先把服务拉回来”的努力就白费了。
        #
        # 2026-10-27 (merge): 本分支原来那条「拉不回来就是 FAILED」的规则
        # 已改为「拉不回来就是 BLOCKED」—— **以 main 的语义为准**：只有断言
        # 真的被证据证伪才判 FAILED。请求一次都没发出去，就谈不上「判据被
        # 证伪」；把环境没就绪记成交付物失败，会让修复循环去修一条根本没
        # 跑过的判据。
        #
        # 唯一保留 FAILED 的是**钉了后端**的服务（mcode）：那条判据量的就是
        # 那个后端，后端不在 = 能力缺失 = 明确的失败（2026-10-26 的
        # ``api_phase_service_lost``）。

        # 2026-10-25 (repair-r1-22): the request is built from the
        # instance the round bound — the one its ensure produced.  The
        # URL the VP carries was resolved when the plan was resolved;
        # this is the last word on where it points.
        # 2026-10-25: the round's **ready record**, read before this
        # VP's relaunch.  A relaunch that succeeds rewrites the
        # runtime map, and the question the failure class answers is
        # what the round had *before* it started healing itself.
        ready_record = self._services_ready_record()
        # 2026-10-25: one **read-only** registration of the declared
        # health endpoint, taken before the request and before the
        # relaunch — so it records what the round *found*, not what the
        # round fixed.  It says whether the service was answering, which
        # is what turns "the data-plane field was not observed" into a
        # diagnosable sentence instead of "the request failed".  It
        # decides nothing: the VP's own assertions stay the only basis
        # for a PASSED.
        probe = self._probe_declared_health_url(vp)
        self._safe_log_freshness_event(
            vp_id, "data_plane_probe", self._public_probe(probe),
        )
        # 2026-10-25 (VP-006): the **side condition** of a negative,
        # plan-scoped criterion — a plan that does not exist must not be
        # created by the request that asked for it.  The target is
        # resolved here, before the relaunch, so the cell is booked down
        # on every path the api phase can end on; the snapshots that go
        # with it are taken around the request itself.
        side_effect_target = self._zero_side_effect_target(vp)
        # 2026-10-25 (Leg B): the round resolved the service URL once,
        # at preflight, and never looked at the port again. A service
        # that died in between — a foreign sweep taking it, a crash —
        # turned into a connection-refused minutes later, with nothing
        # in the round's log to say why. Probe the port the request is
        # about to use, and if it is dead, start the service again **on
        # the same port**, so the URL the VP was given stays true.
        relaunch = self._relaunch_service_for_vp(vp)
        if relaunch is not None and not relaunch.get("ok"):
            snapshot = self._record_api_phase_service_snapshot(vp)
            # 2026-10-26: a service that **pins a backend** and cannot be
            # brought back is a missing capability, and the hard
            # constraints call that an explicit failure by name: the
            # mcode backend is unavailable, so the criterion that grades
            # it was not observed.  The loss is written down under the
            # service's own name, so "the mcode service is not there" and
            # "the api service is not there" stay two different facts —
            # and the default claude path, which pins no backend, keeps
            # the verdict it always had.
            capability = self._pinned_backend_capability(
                str(relaunch.get("service") or ""),
            )
            if not capability:
                # 2026-10-27 (merge): **main 的语义** —— 服务不可用且重试
                # 后仍不可达 → BLOCKED（infra，可重试，不计同因阈值）。
                # 只有断言真的被证据证伪才判 FAILED；请求一次都没发出去，
                # 谈不上「判据被证伪」。
                #
                # 重启尝试保留在前面（先重启、再判 BLOCKED）：先尽力把服务
                # 拉回来，拉不回来才判 BLOCKED —— 判据仍然是环境没就绪。
                #
                # 这个 BLOCKED 走的是和别的路径**同一个**收尾
                # （:meth:`_finish_api_verdict`）：证据格是每一格都要订的
                # —— 请求没发出去/health 探针看到了什么/负向判据有没有被
                # 观测到，都得落在裁决上，否则"环境没就绪"这条结论就只剩
                # 一句话，没有可核对的凭据。
                return self._finish_api_verdict(
                    self._attach_failure_class(
                        self._build_api_test_relaunch_blocked_verdict(
                            vp_id, relaunch, snapshot,
                        ),
                        vp, snapshot, ready_record,
                        note="the service could not be restarted",
                    ),
                    vp, probe,
                    request_note=(
                        "the request was never made: the service could not "
                        "be restarted"
                    ),
                    side_effect_target=side_effect_target,
                    snapshot=snapshot,
                )
            # A service that cannot be brought back is a FAILED verdict,
            # never a SKIPPED one: the VP's acceptance criterion (the
            # endpoint's contract) was not observed, and pretending it
            # was not applicable would hide it from the repair loop.
            surface = self._execution_surface()
            reasons = [
                "service unavailable at request time",
                (
                    f"service {relaunch.get('service')!r} on port "
                    f"{relaunch.get('port')} could not be restarted: "
                    f"{relaunch.get('reason')}"
                    + (
                        f"; {relaunch.get('detail')}"
                        if relaunch.get("detail") else ""
                    )
                ),
            ]
            evidence: Dict[str, Any] = {
                "vp_id": vp_id,
                "service_relaunch": relaunch,
                "execution_surface": surface,
                "service_liveness": snapshot,
            }
            loss = self._record_api_phase_service_lost(
                vp,
                port=relaunch.get("port"),
                expected_pid=None,
                actual_pid=None,
                leased=None,
                kind="service_not_restarted",
                scope="vp",
                detail=capability["detail"],
            )
            reasons.append(capability["detail"])
            evidence["api_phase_service_lost"] = loss
            reasons.append(self._execution_surface_reason(surface))
            reasons.append(self._service_liveness_reason(snapshot))
            verdict = {
                "status": "FAILED",
                "reasons": reasons,
                "evidence": evidence,
            }
            return self._finish_api_verdict(
                self._attach_failure_class(
                    verdict, vp, snapshot, ready_record,
                    note="the service could not be restarted",
                ),
                vp, probe,
                request_note=(
                    "the request was never made: the service could not be "
                    "restarted"
                ),
                side_effect_target=side_effect_target,
                snapshot=snapshot,
            )
        # 2026-10-27 (merge): main 的门禁在重启之后 —— 见上面那段顺序说明。
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
        # 2026-10-25 (VP-009): the relaunch path checks the backend of
        # the process it starts, but a service that was **already alive**
        # was never checked at all. The endpoint answers the same way on
        # both backends, so a VP could be graded on a claude backend
        # process while its declaration names the mcode one — the
        # "configured but not actually in effect" service the hard
        # constraints call an acceptance failure. Confirm the backend of
        # the process that is actually answering the port before the
        # request is graded.
        backend = self._verify_service_backend_for_vp(vp)
        if backend is not None and not backend.get("ok"):
            snapshot = self._record_api_phase_service_snapshot(vp)
            surface = self._execution_surface()
            # 2026-10-26: the headline depends on **what** is wrong.
            # The service being gone and the service answering for
            # somebody else are two different accidents, and the verdict
            # has to say which one it is.
            if backend.get("service_absent"):
                headline = (
                    "the backend this verification point grades is not "
                    "available: its service is not answering"
                )
            elif backend.get("listener_mismatch"):
                headline = (
                    "the service answering the request is not the "
                    "instance this round leased for it"
                )
            else:
                headline = (
                    "the service answering the request does not run the "
                    "backend its declaration pins"
                )
            reasons = [headline, str(backend.get("detail") or "")]
            evidence: Dict[str, Any] = {
                "vp_id": vp_id,
                "service_backend": backend,
                "execution_surface": surface,
                "service_liveness": snapshot,
            }
            # 2026-10-26: the service is **gone** (or answering for
            # somebody else).  The round writes that down under the
            # service's own name, because "the mcode service is not
            # there" and "the api service is not there" are two
            # different repairs, and because the loss has to be in the
            # log before the repair loop reads the report.
            if backend.get("service_absent") or backend.get("listener_mismatch"):
                loss = self._record_api_phase_service_lost(
                    vp,
                    port=backend.get("port"),
                    expected_pid=backend.get("expected_pid"),
                    actual_pid=backend.get("actual_pid"),
                    leased=backend.get("lease") or None,
                    kind=str(backend.get("kind") or ""),
                    scope="vp",
                    detail=str(backend.get("detail") or ""),
                )
                evidence["api_phase_service_lost"] = loss
            reasons.append(self._execution_surface_reason(surface))
            reasons.append(self._service_liveness_reason(snapshot))
            verdict = {
                "status": "FAILED",
                "reasons": reasons,
                "evidence": evidence,
            }
            # The sentence says **which** fault it was.  The failure
            # class it feeds is read by a repair loop that acts on the
            # difference between "the service is gone" and "somebody else
            # answered", and a note that says the same thing for both
            # sends it back to the same repair.
            if backend.get("service_absent"):
                request_note = (
                    "the request was never made: the service this "
                    "verification point grades is not answering"
                )
            elif backend.get("listener_mismatch"):
                request_note = (
                    "the request was never made: the service answering the "
                    "port is not the instance this round leased for it"
                )
            else:
                request_note = (
                    "the request was never made: the service answering the "
                    "port does not run the backend its declaration pins"
                )
            return self._finish_api_verdict(
                self._attach_failure_class(
                    verdict, vp, snapshot, ready_record,
                    note=request_note,
                ),
                vp, probe,
                request_note=request_note,
                side_effect_target=side_effect_target,
                snapshot=snapshot,
            )
        # 2026-10-25: the snapshot is taken **before** the request goes
        # out. A failed request used to say only "connection refused",
        # which is the same sentence for three different faults: the
        # service never came up, the service was killed between the
        # phases, and the code that graded this VP is not the code
        # under test. The snapshot says what was listening, and the
        # execution surface says who was asking.
        snapshot = self._record_api_phase_service_snapshot(vp)
        # 2026-10-26 (VP-009): the round's **claim** on the instance.
        # The lease is the record a reaper in another process reads; the
        # round's own runtime map is the record it keeps when the ledger
        # holds none (a foreign reap finalised the ledger, and the
        # round still knows which process it started).  When the claim
        # names the process that is answering the port, the answer is
        # this round's answer.  When it does not — the service was
        # taken away and somebody else took the port — the request is
        # **not made**: a criterion about the mcode backend graded on
        # the claude instance's answer is worse than no answer, because
        # it looks like an observation.
        #
        # The default claude path pins no backend and has no claim to
        # attest: its behaviour does not change.
        attestation = self._contract_instance_attestation(vp, snapshot)
        if attestation.get("attested") is False:
            surface = self._execution_surface()
            detail = str(attestation.get("detail") or "")
            reasons = [
                "the backend this verification point grades is not "
                "available: the instance that answered the port is not "
                "the one this round holds the claim on for it",
                detail,
            ]
            evidence: Dict[str, Any] = {
                "vp_id": vp_id,
                "execution_surface": surface,
                "service_liveness": snapshot,
                "contract_instance_attestation": attestation,
            }
            # 2026-10-26: the loss is written down under the service's
            # own name, with the two pids that disagree — the round
            # expected one process to be serving the port and another
            # one is.  That is the fact the repair loop acts on.
            loss = self._record_api_phase_service_lost(
                vp,
                port=attestation.get("port"),
                expected_pid=attestation.get("expected_pid"),
                actual_pid=attestation.get("actual_pid"),
                leased=None,
                kind=str(
                    "listener_mismatch"
                    if attestation.get("actual_pid") is not None
                    else "service_absent"
                ),
                scope="vp",
                detail=detail,
            )
            evidence["api_phase_service_lost"] = loss
            reasons.append(self._execution_surface_reason(surface))
            reasons.append(self._service_liveness_reason(snapshot))
            verdict = {
                "status": "FAILED",
                "reasons": reasons,
                "evidence": evidence,
            }
            request_note = (
                "the request was never made: the instance answering the "
                "port is not the one this round holds the claim on for it"
            )
            return self._finish_api_verdict(
                self._attach_failure_class(
                    verdict, vp, snapshot, ready_record,
                    note=request_note,
                ),
                vp, probe,
                request_note=request_note,
                side_effect_target=side_effect_target,
                snapshot=snapshot,
                attestation=attestation,
            )
        # 2026-10-25: the audit artifact is stamped **before** the
        # request.  The runner keeps the response headers there, and
        # the stamp is what tells this round's copy of it from the
        # previous round's — a copy left over from an earlier round is
        # a neighbour's observation, and it may not be booked in as if
        # it were this VP's own.
        artifact_stamp = self._api_artifact_stamp(vp_id)
        # 2026-10-25 (VP-006): the **before** snapshot of the side
        # condition.  It is taken here, right before the request goes
        # out, so the comparison that follows is about the request and
        # about nothing else.
        side_effect_before = self._side_effect_snapshot(side_effect_target)
        # 2026-10-26 (VP-004): the **listener's identity**.  The
        # criterion asks whether *this* instance refuses a request that
        # does not carry the guard header; a refusal from any process on
        # the port would say nothing about the backend the plan
        # declared.  The port is checked against the process this round
        # leased before the request goes out, and if it answers for
        # somebody else the request is **not made**: a verdict graded on
        # a stranger's answer is worse than no verdict, because it looks
        # like an observation.
        #
        # Nothing is restarted here.  A service that is gone is
        # reported as gone: restarting it so the criterion comes out
        # right would hide the very crash the criterion is about.
        service_lost = self._assert_service_listener_identity(vp)
        if service_lost is not None:
            snapshot = self._record_api_phase_service_snapshot(vp)
            surface = self._execution_surface()
            verdict = {
                "status": "BLOCKED",
                "status_detail": "BLOCKED_SERVICE_LOST",
                "reasons": [
                    "BLOCKED_SERVICE_LOST: the port this request uses is "
                    "answered by a process this round did not ensure, so "
                    "the criterion was not observed",
                    f"port: {service_lost.get('port')}",
                    f"expected_pid: {service_lost.get('expected_pid')}",
                    f"actual_pid: {service_lost.get('actual_pid')}",
                    "last_reap_reason: "
                    f"{service_lost.get('last_reap_reason')}",
                    f"lease_owner: {service_lost.get('lease_owner')}",
                    self._execution_surface_reason(surface),
                    self._service_liveness_reason(snapshot),
                ],
                "actual_result": (
                    "本轮租约里的服务实例已不在这个端口上（监听者与租约不符），"
                    "请求没有发出；该判据没有被观测，也不是它自身的失败。"
                ),
                "evidence": {
                    "vp_id": vp_id,
                    "status_detail": "BLOCKED_SERVICE_LOST",
                    "api_phase_service_lost": service_lost,
                    "execution_surface": surface,
                    "service_liveness": snapshot,
                },
            }
            return self._finish_api_verdict(
                verdict, vp, probe,
                request_note=(
                    "the request was never made: the port is answered by a "
                    "process this round did not ensure"
                ),
                side_effect_target=side_effect_target,
                side_effect_before=side_effect_before,
                snapshot=snapshot,
            )
        vp = self._bind_vp_service_address(vp)
        # 2026-10-25 (repair-r1-31): the address this request goes to
        # is recorded **before** it goes there — the transport's own
        # complaint ("Errno 61") says nothing about which port it was
        # aimed at, and the reader needs both.
        address = self._record_vp_address(vp_id, vp)
        self._note_vp_attempt(vp_id)
        try:
            result = run_api_verification(
                # 2026-10-25 (VP-002): the plan's URL is the service
                # **placeholder** (``{{svc.api.url}}/health/data``), and
                # a placeholder is not a URL — the request died with
                # ``unknown url type`` even when the relaunch had just
                # put the service back.  Resolve it against the same
                # effective declarations the relaunch used, so the
                # request and the restart agree on the port.
                self._vp_with_resolved_request_url(vp),
                artifact_dir=self.plan_dir / "vp_artifacts",
            )
        except Exception as exc:  # noqa: BLE001 - runner never raises
            logger.exception(
                "[api_runner] unexpected failure for %s", vp_id,
            )
            surface = self._execution_surface()
            verdict = {
                "status": "FAILED",
                "reasons": [
                    f"api_test runner crashed: {type(exc).__name__}: {exc}",
                    self._execution_surface_reason(surface),
                    self._service_liveness_reason(snapshot),
                ],
                "evidence": {
                    "vp_id": vp_id,
                    "execution_surface": surface,
                    "service_liveness": snapshot,
                },
            }
            return self._finish_api_verdict(
                self._attach_failure_class(
                    verdict, vp, snapshot, ready_record,
                    request_error=f"{type(exc).__name__}: {exc}",
                    note="the api_test runner crashed",
                ),
                vp, probe,
                request_note=(
                    "the request produced no response: the api_test runner "
                    "crashed"
                ),
                side_effect_target=side_effect_target,
                side_effect_before=side_effect_before,
                snapshot=snapshot,
            )

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

        # 2026-10-25 (VP-006): the **after** snapshot of the side
        # condition — taken once the request has been answered, so the
        # comparison is about this request and about nothing else.
        side_effect_after = self._side_effect_snapshot(side_effect_target)
        verdict = result.to_verdict_dict()
        # 2026-10-27: the accounting of what the request actually did.
        # A request that produced **no response** ran none of the VP's
        # assertions — there was nothing to compare against.  Writing
        # that number down keeps a transport fault from being read as a
        # broken contract (assertions failed) or as a satisfied one
        # (assertions passed): it is neither, it is a fault of the
        # transport, and the criterion was not observed at all.
        assertions_executed = self._assertions_executed(result)
        verdict.setdefault("evidence", {})["assertions_executed"] = (
            assertions_executed
        )
        # 2026-10-25 (repair-r1-26): a request that never reached the
        # service is not a verdict about the product.  The
        # classification layer runs **before** the verdict is recorded,
        # so what lands on disk is BLOCKED, not FAILED.
        verdict = self._classify_transport_failure(vp_id, verdict)

        # 2026-10-26: the request's own outcome, split into the two
        # kinds that must never be confused again.  A request that came
        # back — with any status code, even a 500 — was **answered**,
        # and its verdict belongs to this verification point.  A request
        # that produced no response at all was not answered by anybody.
        #
        # When the port that did not answer is **the round's own
        # service** — the one this round brought up, now gone — that is
        # a fact about the **round**, and it is escalated as one (see
        # below) instead of being written down as this verification
        # point's own failure.  An endpoint the round does not own is
        # left alone: its silence is that endpoint's business, and the
        # round must not stop for it.
        transport_error = self._api_result_transport_error(result)
        transport_kind = self._transport_error_kind(transport_error)
        if (
            transport_kind
            and self._round_service_port_claim(vp)
            and self._api_phase_service_gone(vp, transport_kind)
        ):
            loss = self._record_api_phase_service_lost(
                vp,
                port=self._vp_target_port(vp),
                expected_pid=None,
                actual_pid=None,
                leased=None,
                error=transport_error,
                kind=transport_kind,
                scope="round",
            )
            self._api_phase_service_lost = loss
            verdict = self._api_phase_service_lost_verdict(vp_id, loss)
            # The escalation replaces the verdict, not the accounting:
            # the criterion was still not observed, and the number of
            # assertions that ran is still zero.
            verdict.setdefault("evidence", {})["assertions_executed"] = (
                assertions_executed
            )
        # 2026-10-26 (merge): main 的分类在**之后**：它只往裁决上补两个键
        # （``failure_class`` / ``failure_class_source``），是就地改写；而本分支
        # 的分类会把 FAILED 换成一个**新的**裁决字典 —— 所以必须本分支先跑，
        # 否则 main 补的键会被新字典丢掉。
        self._classify_api_test_verdict(verdict, result)
        # 2026-10-26: the **service snapshot** rides on this event, so
        # the event itself says which instance answered: the port, the
        # process that was listening on it, the round that owns the
        # lease on it, and how long ago the round brought its services
        # up.  Without it the event says "the request failed" and
        # nothing about *what* answered — and a reader cannot tell a
        # wrong answer from no answer at all without going to look in
        # another file.
        service_identity = self._vp_service_identity(vp)
        self._safe_log_freshness_event(
            vp_id, "api_test_executed",
            {
                "status": verdict["status"],
                "status_code": result.evidence.get("status_code"),
                "assertions_failed": result.evidence.get("assertions_failed"),
                "signal": result.evidence.get("signal"),
                # How many assertions ran.  Zero when the request
                # produced no response: there was nothing to compare
                # against, so the criterion was not observed — which is
                # not the same thing as a criterion that failed.
                "assertions_executed": assertions_executed,
                "service_snapshot": (
                    self._api_phase_service_snapshot_summary(
                        vp, identity=service_identity
                    )
                ),
                # 2026-10-27: the **service identity** — which process
                # answered this request, which **tree** it runs in, which
                # commit that tree is checked out at, and whether the
                # round's own claim agrees with what was observed.  The
                # four facts above say *who* answered; this one says
                # *which code* answered, and without it "the change was
                # made but the request ran against the old process" is
                # not readable after the fact.
                "service_identity": service_identity,
                # The transport fault, when there was one: "no response"
                # and "the service is gone" are different sentences, and
                # the event is where the difference is written down.
                "error": transport_error or None,
            },
        )
        # 2026-10-27: the execution surface and the service liveness
        # are booked down on **every** verdict, a PASSED one included.
        # They used to ride only on the failed ones, and that is
        # exactly the case where they are worth nothing: a PASSED
        # verdict is read as "the code under repair works", and the
        # reader has no way to see that the answer came from a process
        # running a tree three commits behind.  The fork is gone — a
        # fact that is only written down when it is inconvenient is not
        # a fact.
        surface = self._execution_surface()
        evidence = dict(verdict.get("evidence") or {})
        evidence["execution_surface"] = surface
        evidence["service_liveness"] = snapshot
        verdict["evidence"] = evidence
        if str(verdict.get("status") or "") != "PASSED":
            # 2026-10-25: a failed request names the execution surface
            # and the service liveness, so the report can tell the
            # three faults apart instead of repeating "connection
            # refused" nine times.
            reasons = list(verdict.get("reasons") or [])
            reasons.append(self._execution_surface_reason(surface))
            reasons.append(self._service_liveness_reason(snapshot))
            verdict["reasons"] = reasons
            # 2026-10-25: and it says **which** of the three faults
            # this was — the round never had the service, the service
            # died before the request, or the request failed against a
            # service that is there.  "connection refused" is one
            # sentence for all three, and they have three different
            # repairs.
            verdict = self._attach_failure_class(
                verdict, vp, snapshot, ready_record,
                request_error=result.evidence.get("error"),
                response_received="response_body" in result.evidence,
            )
        # 2026-10-25: the data-plane cell. The registration and the
        # observation go into the evidence of **every** verdict — a
        # PASSED one included — so the reader can see that the field
        # was observed without having to look for the audit artifact.
        # Neither of them touches the status: the verdict above is the
        # one the VP's own assertions produced.
        #
        # The negative-expectation cell rides along with it: a VP that
        # expects a rejection has to say whether the rejection was
        # really seen, and "the request produced no response" is not it.
        request_error = result.evidence.get("error")
        schema_issues = result.evidence.get("schema_issues") or []
        if schema_issues:
            request_note = (
                "the request was never made: the api_test declaration is "
                f"not executable ({schema_issues[0].get('detail')})"
            )
        elif request_error:
            request_note = (
                f"the request produced no response: {request_error}"
            )
        else:
            request_note = ""
        # 2026-10-25: the **header dimension**.  The response headers
        # live in the audit artifact the runner has just written, so
        # they are read back from there — but only when the copy on
        # disk is the one this request wrote.  The header is booked
        # down on its own, apart from the body: a content type that was
        # never observed has to say so, and it must not be confused with
        # a body field that was not observed.
        content_type, headers_read = self._read_response_content_type(
            vp_id, artifact_stamp,
        )
        return self._finish_api_verdict(
            verdict, vp, probe,
            status_code=result.evidence.get("status_code"),
            response_body=result.evidence.get("response_body"),
            request_note=request_note,
            body_truncated=bool(
                result.evidence.get("response_body_truncated")
            ),
            response_content_type=content_type,
            response_headers_read=headers_read,
            side_effect_target=side_effect_target,
            side_effect_before=side_effect_before,
            side_effect_after=side_effect_after,
            snapshot=snapshot,
        )

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

        The round's verdict is re-derived from what is left:

          * a genuine failure keeps the round red — this is the whole
            point of the exercise, and the negative control the tests
            pin;
          * a partial verdict stays partial;
          * a round in which nothing was judged says ``SKIPPED`` —
            claiming ``PASSED`` there would be a verdict about a
            product nobody asked.
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
        # are not in them, and the round's verdict is derived from what
        # is left.
        unjudged = set(blocked) | set(invalid_env)
        judged = [
            entry.get("status")
            for entry in report_results
            if isinstance(entry, dict)
            and str(entry.get("id") or "") not in unjudged
        ]
        if any(status == "FAILED" for status in judged):
            report_data["overall_status"] = "FAILED"
        elif any(status == "PARTIAL" for status in judged):
            report_data["overall_status"] = "PARTIAL"
        elif any(status == "PASSED" for status in judged):
            report_data["overall_status"] = "PASSED"
        else:
            report_data["overall_status"] = "SKIPPED"

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

    def _build_api_test_relaunch_blocked_verdict(
        self,
        vp_id: str,
        relaunch: Dict[str, Any],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """A BLOCKED verdict for a service that could not be brought back.

        2026-10-27 (merge): **main 的语义** —— 服务不可用且重试后仍不可达
        → BLOCKED。只有断言真的被证据证伪才判 FAILED；这里请求一次都没发
        出去，谈不上「判据被证伪」。把环境没就绪记成交付物失败，会让修复
        循环去修一条根本没跑过的判据。

        信封是 main 的那个门禁的同一个（:meth:`_build_api_test_blocked_verdict`）：
        signal 是 ``service_not_ready``，failure_class 是 ``infra`` —— 环境
        没就绪，可重试，不计同因阈值。

        重启的事实（哪个服务、哪个端口、为什么没拉回来）挂在 evidence 上：
        它是**诊断**，不是判定依据 —— 判定依据只有一条：服务不可达。

        钉了后端的服务不走这里：那种服务拉不回来是**能力缺失**，由
        ``_pinned_backend_capability`` 判成 FAILED（见调用处）。
        """
        port = relaunch.get("port")
        try:
            ports = [int(port)] if port is not None else []
        except (TypeError, ValueError):
            ports = []
        detail = (
            f"service {relaunch.get('service')!r} on port {port} could not "
            f"be restarted: {relaunch.get('reason')}"
            + (
                f"; {relaunch.get('detail')}"
                if relaunch.get("detail") else ""
            )
        )
        verdict = self._build_api_test_blocked_verdict(vp_id, ports, detail)
        evidence = verdict.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {}
        evidence["service_relaunch"] = relaunch
        if snapshot is not None:
            evidence["service_liveness"] = snapshot
        verdict["evidence"] = evidence
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

        # 2026-10-27: the **service identity** of the ui / e2e phases.
        # Those two phases are dispatched to a sub-agent, and nothing
        # else in their path says which process the criterion will be
        # graded against: their url is a page to open, not a port the
        # framework owns.  So the dispatch record carries the identity
        # of the service that url names — the process, the tree it runs
        # in, the commit that tree is checked out at — and a reader can
        # tell "the page was checked" from "the page was checked
        # against the code under repair".
        dispatch_identity: Dict[str, Any] = {}
        if method in SERVICE_IDENTITY_DISPATCH_METHODS:
            dispatch_identity = self._vp_service_identity(vp)

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
                "service_identity": dispatch_identity,
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

        # 2026-10-26: the round's lease, checked once more before the
        # judgment.  The judgment phase can run for minutes, and a round
        # whose services were taken away in the meantime must publish
        # that as a round-level error — not as a verdict about a service
        # nobody was serving any more.
        self._assert_round_lease()

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
                    # overall_status 只看真正得出过结论的 VP；延后的关卡既不加
                    # 分也不减分（它上面一定有 Phase 1 的失败在决定结论）。
                    judged = [
                        vr.get("status")
                        for vr in report_data.get("verification_results", [])
                        if vr.get("status") != "DEFERRED"
                    ]
                    if any(s == "FAILED" for s in judged):
                        report_data["overall_status"] = "FAILED"
                    elif any(s == "PARTIAL" for s in judged):
                        report_data["overall_status"] = "PARTIAL"

                if not any(s == "FAILED" for s in execution_verdicts.values()):
                    for vr in report_data.get("verification_results", []):
                        vid = vr.get("id")
                        if vid and execution_verdicts.get(vid) == "PASSED" and vr.get("status") == "PARTIAL":
                            # LLM-emitted subjective PARTIAL on a VP
                            # the executor marked PASSED → restore.
                            vr["status"] = "PASSED"
                    # Re-derive overall_status from the corrected list
                    statuses = [
                        vr.get("status") for vr in report_data.get("verification_results", [])
                    ]
                    if statuses and all(s == "PASSED" for s in statuses):
                        report_data["overall_status"] = "PASSED"
                    elif any(s == "FAILED" for s in statuses):
                        report_data["overall_status"] = "FAILED"
                    elif any(s == "PARTIAL" for s in statuses):
                        report_data["overall_status"] = "PARTIAL"
                    else:
                        report_data["overall_status"] = "PASSED"

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
                # 2026-10-27：把"判它的是哪次提交"盖到报告行上。
                #
                # 这个字段只能由框架盖 —— 判定 LLM 不知道 HEAD，也不该编一
                # 个。执行结果里带着的按执行结果盖（本轮真跑出来的那棵树上
                # 的结论）；执行结果里没有的（本轮延后的关卡：什么都没判）
                # 把 LLM 可能编出来的值抹掉。
                self._stamp_judged_heads(report_data, execution_results)

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

                # 2026-10-26: a round that lost its lease on its services
                # is an **error**.  It is published at the top level of
                # the report, because the per-VP verdicts alone ("nine
                # api_test VPs failed with connection refused") point the
                # repair loop at nine features that were never broken.
                round_error = self._round_error_report()
                if round_error:
                    report_data["round_error"] = round_error
                    report_data["overall_status"] = "FAILED"

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
            # 2026-10-27：判定 LLM 要能看到"这条结论判在哪次提交上" ——
            # 结论只对产生它的那份代码有效。看不到它，LLM 就无法区分
            # "这一轮真的跑过了"和"沿用了一份别的提交上的结论"。
            judged_head = str(result.get(JUDGED_HEAD_FIELD) or "")
            if judged_head:
                parts.append(f"- 判定提交 (judged_head): {judged_head}")
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
        # The judgment phase can run for minutes with no VP activity;
        # the round is still alive, so the heartbeat that protects its
        # services has to keep saying so.
        self._touch_round_liveness()

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

        # 2026-10-26: the round-level error travels with the fallback
        # report too — a degraded judgment must not hide the fact that
        # the round lost its lease on its services.
        round_error = self._round_error_report()
        if round_error:
            report["round_error"] = round_error
            report["overall_status"] = "FAILED"

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

    def _plan_vp_definitions(
        self, plan_data: Any = None,
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        """The verification points as the plan defines them (id → entry).

        Two callers, two sources, one authority:

        * ``plan_data`` given → the **in-memory** plan the executor was
          built from (this branch: an id issued by the delta evaluation
          must be looked up before it is dispatched);
        * ``plan_data`` omitted → the plan **file** on disk (main: the
          file is the authority on what a verification point is — the
          in-memory plan cannot answer that, because the plan-to-executor
          conversion defaults a missing method to ``code_review``, so a
          definition-less entry arrives wearing a method it never
          declared).

        ``None`` means "there is no plan on disk" (missing or unreadable).
        The dispatch guard stands down in that case: with nothing to
        compare against, every verification point would look undefined,
        and blocking the whole round would be worse than the defect it
        guards against.

        Keys use the same id derivation as the executor
        (``_convert_plan_to_executor_schema`` uses ``vp.get("id",
        "unknown")``), otherwise "look the definition up" would read a
        nameless VP as "no definition".
        """
        if plan_data is None:
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
        else:
            if not isinstance(plan_data, dict):
                return {}
            points = plan_data.get("verification_points") or []
        return {
            str(vp.get("id") or "unknown"): vp
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

    # ------------------------------------------------------------------
    # 2026-10-26：裁决的出处 —— "这条结论是哪条定义、哪次提交挣来的"
    # ------------------------------------------------------------------
    #
    # 事故（VP-031）：增量评估发过 VP-031 这个号，但它的定义从未落进
    # ``verification_plan.json``。于是
    #
    #   * 执行器**无定义可分派** —— 那条 VP 这一轮一次都没跑；
    #   * 它此前挣到的失败被原样沿用，报告里 VP-031 的 title /
    #     expected_result 是空的，失败却是"真的"；
    #   * 而结果里不记"判它的是哪次提交"，跨提交沿用根本无从发现。
    #
    # 下面这几个方法是那三件事的落地：分派前查定义、结论盖上 HEAD、
    # 投影时不沿用无定义的旧结论。

    def _current_project_head(self) -> str:
        """被验代码当前所在的提交（判结论的"这份代码"）。

        空串 = 查不到（不是 git 仓库 / git 不可用）。查不到时**不下**
        "要不要重跑"的判断 —— 无从比较时不猜（见
        :func:`requires_reexecution`）。

        缓存一次：一次 round 里代码不会变，而每条 VP 都查一次 git 是
        白烧的进程。
        """
        if getattr(self, "_round_head", None) is None:
            from verification_plan_delta import current_git_head
            # ``getattr`` 而不是直接取属性：这个方法会被
            # ``VerificationAgent.__new__`` 造出来的（没跑 ``__init__``）
            # 实例调用 —— 那样的实例没有 ``project_dir``。
            self._round_head = current_git_head(
                getattr(self, "project_dir", None)
            )
        return self._round_head or ""

    @staticmethod
    def _judged_head_of(verdict: Any) -> str:
        """一条裁决里"判它的那次提交"。

        两处都读：顶层（内存路径）与 ``evidence`` 里（落库路径 ——
        ``VerificationRepository.append_verdict`` 只保留固定几个字段，
        ``evidence`` 是唯一能原样落库的地方）。
        """
        if not isinstance(verdict, dict):
            return ""
        head = str(verdict.get(JUDGED_HEAD_FIELD) or "")
        if head:
            return head
        evidence = verdict.get("evidence")
        if isinstance(evidence, dict):
            return str(evidence.get(JUDGED_HEAD_FIELD) or "")
        return ""

    @staticmethod
    def _is_unexecuted_final_gate(
        verdict: Any,
        definition: Any,
    ) -> bool:
        """这条 Phase 2 全量关卡的结论是**本轮没跑**时沿用下来的吗？

        2026-10-27：执行器从库里水合回来的历史结论带 ``executed_this_round
        = False``。整仓门禁的结论只对产生它的那份代码有效 —— 拿一份没跑过
        的结论当本轮的结论，报告就成了一张空白支票（VP-029 的整仓门禁在
        一轮里一次都没跑，却仍被判过）。

        缺这个键的旧裁决按"跑过了"处理：不是所有执行器都会写它（测试里
        的假执行器就没有），宁可沿用旧行为，也不凭空把别人的结论作废。
        """
        if not isinstance(verdict, dict):
            return False
        if verdict.get("executed_this_round") is not False:
            return False
        if not isinstance(definition, dict):
            return False
        return _is_final_gate(definition)

    def _build_deferred_gate_row(
        self, verdict: Any, vp_id: str, current_head: str,
    ) -> Dict[str, Any]:
        """本轮没跑的 Phase 2 全量关卡 → ``DEFERRED``（不是旧结论）。

        旧结论留在 ``evidence`` 里作审计痕迹："被丢掉的是哪条旧结论、它
        判在哪次提交上"。报告行上**不**写 ``judged_head`` —— 这一轮没有
        任何东西被判过，写上 HEAD 等于给一条没跑过的关卡盖一个假出处。

        状态只能是 ``DEFERRED``：一份本轮没跑出来的结论不许带着
        ``PASSED`` 或 ``FAILED`` 进报告 —— 前者会让"整仓门禁通过"这个
        合入判据成立在一次没跑的门禁上，后者会为一个没跑过的关卡生成
        修复任务。
        """
        old_status = str(verdict.get("status") or "") if isinstance(
            verdict, dict
        ) else ""
        old_head = self._judged_head_of(verdict)
        if old_head and current_head and old_head == current_head:
            why = (
                f"上一轮那份结论判在 {old_head} 上（同一棵树），"
                f"但本轮没有真的执行，已作废"
            )
        else:
            why = (
                f"上一轮那份结论判在 {old_head or '(未知提交)'} 上，"
                f"与当前 HEAD {current_head or '(未知)'} 不是同一棵树，已作废"
            )
        actual = f"Phase 2 全量关卡本轮未执行（{why}）"
        evidence = {
            "gate": "verification_phase_2",
            "executed_this_round": False,
            "discarded_status": old_status,
            "discarded_judged_head": old_head,
        }
        return {
            "id": vp_id,
            "status": "DEFERRED",
            "actual_result": actual,
            "evidence": json.dumps(evidence, ensure_ascii=False),
        }

    @staticmethod
    def _stamp_judged_heads(report_data: dict, execution_results: dict) -> None:
        """把"判它的是哪次提交"盖到报告行上。

        2026-10-27：结论只对产生它的那份代码有效 —— 报告行必须写明它是
        判在哪次提交上的。这个字段**只能由框架盖**：判定 LLM 不知道
        HEAD，也不该编一个。

        * 执行结果里带着的 → 按执行结果盖（本轮跑出来的，或同一棵树上
          沿用的旧结论）；
        * 执行结果里没有的 → 把 LLM 可能编出来的值抹掉（本轮延后的
          Phase 2 关卡：这一轮什么都没判，写上 HEAD 等于给一条没跑过的
          关卡盖一个假出处）。
        """
        by_id: Dict[str, Dict[str, Any]] = {}
        for er in (execution_results or {}).get("execution_results") or []:
            if not isinstance(er, dict):
                continue
            vid = er.get("id")
            if vid:
                by_id[str(vid)] = er
        for vr in (report_data or {}).get("verification_results") or []:
            if not isinstance(vr, dict):
                continue
            er = by_id.get(str(vr.get("id"))) or {}
            head = str(er.get(JUDGED_HEAD_FIELD) or "")
            if head:
                vr[JUDGED_HEAD_FIELD] = head
            else:
                vr.pop(JUDGED_HEAD_FIELD, None)

    def _build_definition_missing_verdict(
        self, vp_id: str, missing: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """一条"没有定义"的 VP 的 BLOCKED 裁决。

        2026-10-26（VP-031）：占了号却没有定义的条目，执行器无定义可
        分派 —— 它既没跑，也没被记成"跑不了"，只剩一条历史失败被沿用。

        框架必须把这件事**显式**记成"没有定义"，而不是让一条旧失败冒充
        今天的结论。
        """
        detail = "、".join(missing) if missing else "计划里查不到这个 id"
        return {
            "status": "BLOCKED",
            "reasons": [
                f"{BLOCKED_DEFINITION_MISSING}: {vp_id} 在计划里没有可用定义"
                f"（{detail}），本轮未分派"
            ],
            "actual_result": (
                f"{BLOCKED_DEFINITION_MISSING} —— {vp_id} 在计划里没有可用定义"
                f"（{detail}），本轮**未分派**；此前那条旧结论不作为本轮结论沿用。"
            ),
            "evidence": {
                "blocked_reason": BLOCKED_DEFINITION_MISSING,
                "missing_definition_fields": list(missing or []),
            },
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

        # 2026-10-26（VP-031）：分派前按 id 查定义。查不到 → **不分派**。
        #
        # 增量评估发出去的号必须带着定义一起落进计划文件（见
        # :mod:`verification_plan_delta`）；查不到定义的 id 说明这条定义
        # 没落下来 —— 分派它等于让子 agent 去猜"这条 VP 要验什么"，猜
        # 出来的结论与这条 VP 无关。
        self._vp_definitions = self._plan_vp_definitions(plan_data) or {}

        async def _dispatch_vp(vp_node: dict) -> dict:
            vp = dict(vp_node)
            vp_id = str(vp.get("id", "unknown"))
            # 2026-10-26: the round lost its lease on its services — a
            # foreign process took them away (see
            # :meth:`_assert_round_lease`).  The VPs that come after the
            # loss are **stopped**, not run: each of them would report
            # the same symptom ("connection refused") and dress one
            # round-level accident up as one failure per VP.
            if getattr(self, "_round_lease_lost", None):
                return self._round_lease_lost_stop_verdict(vp_id)
            # 2026-10-25 (repair-r1-32): the round keeps its own
            # dispatcher, so the one automatic re-run of the points the
            # environment blocked is graded by exactly the rules the
            # first attempt used — the same gates, the same evidence.
            #
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
            #
            # 2026-10-26 (merge): 两边的定义门禁**都要**，而且顺序有讲究 ——
            # main 的门禁问的是"计划文件里这条验证点完整吗"（更全：它还查
            # 标题与方法），本分支的门禁问的是"计划里查得到这个 id 吗"。
            # main 的门禁**先**跑：它读的是计划文件（权威），而且计划文件
            # 不在时它会**让门禁退下**（没有可比对的东西时，拦住整轮比它要
            # 防的缺陷更糟）—— 本分支的门禁读的是内存里的计划，正好补上
            # "定义还没落盘"这一种情况。
            _gaps = vp_dispatch_gaps(vp, plan_definitions)
            if _gaps:
                self._safe_log_freshness_event(
                    vp_id, VP_DEFINITION_MISSING_SIGNAL, {"gaps": _gaps},
                )
                return self._build_vp_definition_blocked_verdict(vp_id, _gaps)

            # 2026-10-26（VP-031）：分派前按 id 查定义。查不到 → **不分派**。
            #
            # 增量评估发出去的号必须带着定义一起落进计划文件（见
            # :mod:`verification_plan_delta`）；查不到定义的 id 说明这条定义
            # 没落下来 —— 分派它等于让子 agent 去猜"这条 VP 要验什么"，猜
            # 出来的结论与这条 VP 无关。
            #
            # 门禁排在租约检查**之后**：租约丢失是**轮次级**事故（服务被
            # 别的进程收走以后本轮什么都不该再跑，被停下来的验证点"没有
            # 裁决"），定义缺失是**每条**验证点的事。两者换序，一次轮次级
            # 事故就会被改写成一条"判据缺陷"，把"没跑"的原因改得面目全非。
            if definition_missing(
                getattr(self, "_vp_definitions", {}), vp_id,
            ):
                self._safe_log_freshness_event(
                    vp_id,
                    "definition_missing_blocked",
                    {"blocked_reason": BLOCKED_DEFINITION_MISSING},
                )
                return self._build_definition_missing_verdict(vp_id)

            # 2026-10-25 (repair-r1-31): the address this VP is about
            # to use is recorded **before** anything can block it — a
            # VP that never got a request out still has to say which
            # address it would have used, and which instance the round
            # had bound for it.
            self._record_vp_address(vp_id, vp)
            # The round is alive and working: refresh the heartbeat that
            # keeps a foreign sweep from taking the services this VP is
            # about to use.
            self._touch_round_liveness()
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


            2026-10-26（VP-031）：每条裁决都盖上 ``judged_head`` —— 结论
            只对产生它的那份代码有效（与
            :mod:`verification_verdict_provenance` 的命令指纹同向）。

            "这条 VP 有没有定义"的门禁在 :func:`_dispatch_vp` 里（租约
            检查之后）：分派一条没有定义的 VP，等于让子 agent 去猜"这条要
            验什么" —— 猜出来的结论与这条 VP 无关，却会占着这条 VP 的名字
            进报告。
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
            verdict = self._apply_serving_handover(
                vp_id, before, after, verdict,
            )
            # 2026-10-26（VP-031）：每条裁决都盖上 ``judged_head`` —— 结论
            # 只对产生它的那份代码有效（与
            # :mod:`verification_verdict_provenance` 的命令指纹同向）。
            return stamp_judged_head(verdict, self._current_project_head())

        executor = VerificationExecutor(
            verification_plan=executor_plan,
            plan_id=self.plan_dir.name,
            plan_dir=self.plan_dir,
            sub_agent_runner=_sub_agent_runner,
            verif_repo=getattr(self, "verif_repo", None),
            # 2026-10-27：这一轮跑的是哪次提交。Phase 2 全量关卡的结论只对
            # 产生它的那份代码有效 —— 执行器据此拒绝把"判在别的提交上"的
            # 整仓门禁当成终态成功跳过。
            current_head=self._current_project_head(),
        )
        # 2026-10-25: the executor is up — say which tree it is running
        # from, before any VP can be graded by it. A round whose log
        # shows ``same_tree=false`` was graded by a copy of the
        # framework that does not contain the fix under repair, and its
        # verdicts are evidence about that copy, not about the product.
        self._emit_execution_surface_provenance()
        return executor

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
                report = self._preflight_declared_services(plan_data)
            else:
                report = self._preflight_legacy_service_freshness(plan_data)
            # 2026-10-25: the round now owns the state of its services.
            # This is the moment the api phase measures "how long ago
            # were the services ensured" from, so a verdict can say how
            # long a port has been dead instead of leaving the reader to
            # reconstruct it.
            self._services_ensured_at = time.monotonic()
            return report
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

        The round takes a **lease** on every service it starts: the lease
        names the round (``<round_id>@<backend_pid>``) and lands in the
        plan's ledger, so a reaper in another backend process can tell
        "a round is using this" from "this is an orphan". On 2026-10-25
        that evidence did not exist, a foreign sweep stopped the round's
        api service two minutes after the round had started it, and the
        round's first ``api_test`` found a refused connection.
        """
        service_manager = _load_service_manager(self.project_dir)
        ensure_services = service_manager.ensure_services
        record_runtimes = service_manager.record_runtimes
        # 2026-10-26: the lease context has to come from the **same**
        # module as ``ensure_services`` — the lease is a ContextVar in
        # that module, so a context manager taken from another copy of it
        # would install its own variable and the services would start
        # without a lease.
        round_lease_context = getattr(
            service_manager, "round_lease_context", None,
        )
        if round_lease_context is None:
            from service_manager import round_lease_context

        self._maybe_rebuild_before_service_start()
        # 2026-10-25: claim the plan BEFORE the services start. The
        # window between "the api service is up" and "the round owns it"
        # is exactly the window a foreign backend's startup sweep used to
        # take it in — that sweep reaped this plan's services 14 seconds
        # after they came up, and the round only found out seven minutes
        # later, as a connection-refused.
        self._touch_round_liveness()
        round_id = self._round_id()
        # The round's identity travels with the call: the services it
        # starts are leased to it, and the ledger says whose they are.
        with round_lease_context(
            self.plan_dir,
            round_id=round_id,
        ):
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
        # 2026-10-26: the round's heartbeat, refreshed **after** the
        # services are up.  Starting them can take a minute (the
        # readiness probe is what takes the time), and the claim written
        # before them is already that old by the time the first request
        # goes out — so the round says "still here" again the moment it
        # actually owns something.
        self._touch_round_liveness()
        ensured = runtime_map.to_dict()
        if not ensured.get("lease_owner"):
            # The map was built without a round (a stub, a test): the
            # event still has to name the round that asked for it.
            ensured["lease_owner"] = self._round_lease_owner()
        # 2026-10-26 (merge): 一个事件，两边的事实 —— ``unready_details``
        # 说服务为什么没起来，``lease_owner`` 说这一轮拿的是谁的租约。
        self._safe_log_freshness_event(
            "__preflight__", "services_ensured",
            {
                **ensured,
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

    def _capture_data_plane_fingerprints(self) -> Dict[str, Any]:
        """Record which data plane each declared service is serving.

        Called once, at the end of the round-start preflight — the
        moment the round knows which instance it started.  A service
        that does not answer ``/health/data`` (a project with no data
        plane of its own) is **not** recorded and is therefore never
        gated: not knowing the data plane is not the same as knowing
        it is wrong.

        Never raises — a probe that fails is a service that is not
        recorded, not a broken preflight.
        """
        from service_freshness import expected_routing_rows, probe_data_plane

        expected: Optional[int] = None
        try:
            # The fixture is the state database the round's own service
            # reads; its row count is what "the right data plane"
            # means.
            expected = expected_routing_rows()
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

    def _expected_plans_dirs(self) -> List[str]:
        """The plans trees this round's service may be serving.

        The service publishes the plans **root** it serves; the round
        lives in ``<root>/<plan_id>``, so the root the round expects is
        the plan directory's parent.  The plan directory itself is
        accepted as well — a deployment that serves the round's plan
        directory as its own root is serving the round's plans either
        way.

        The first entry is the *primary* expectation (the plans root);
        the second is the tolerated alternative.
        """
        dirs: List[str] = []
        for candidate in (self.plan_dir.parent, self.plan_dir):
            try:
                resolved = str(Path(candidate).resolve())
            except OSError:  # pragma: no cover - defensive
                resolved = str(candidate)
            if resolved not in dirs:
                dirs.append(resolved)
        return dirs

    def _expected_state_db_inode(self) -> Optional[int]:
        """The inode of the state database **this process** has open.

        The round runs inside the backend, and the backend reads the
        state database through
        :func:`config_paths.resolve_state_db_path`.  A service reading
        a *different* file — the same path with another inode — is a
        different data plane, and no amount of ``200 OK`` makes the two
        the same.
        """
        try:
            from config_paths import resolve_state_db_path

            return int(resolve_state_db_path().stat().st_ino)
        except Exception:  # noqa: BLE001 - no database, no verdict
            return None

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
        expected_dirs = self._expected_plans_dirs()
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

        expected_inode = self._expected_state_db_inode()
        observed_inode = self._observed_state_db_inode(actual)
        if (
            expected_inode is not None
            and observed_inode is not None
            and observed_inode != expected_inode
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

    def _round_id(self) -> str:
        """The id of the round that is running right now.

        It names the round in every lease it takes and in every event
        it writes, so a log line can be traced back to the round that
        produced it — and so a reaper in another backend process can
        tell "this service belongs to a running round" from "this
        service is an orphan".

        The id is derived from the plan directory and the round number:
        the plan is already unique per verification plan, and the round
        number distinguishes one round from the next.

        Defensive on purpose: a half-built agent (the tests build one
        with ``__new__``) may have no ``plan_dir`` at all, and a round
        id that raises would take the whole preflight down.
        """
        plan_dir = getattr(self, "plan_dir", None)
        plan_name = getattr(plan_dir, "name", "") or ""
        if not plan_name:
            plan_name = "plan"
        try:
            round_number = int(getattr(self, "_current_round", 0) or 0)
        except (TypeError, ValueError):
            round_number = 0
        return f"{plan_name}-r{round_number}"

    def _round_lease_owner(self) -> str:
        """The owner token of this round's lease: ``<round_id>@<backend_pid>``.

        The backend pid is part of the token on purpose: two backend
        processes can run rounds for the same plan (a restart while the
        old one drains), and only the pid tells the two apart.
        """
        return f"{self._round_id()}@{os.getpid()}"

    def _touch_round_liveness(self) -> None:
        """Refresh this plan's "my round is running" heartbeat.

        The heartbeat is what keeps another backend process's startup /
        shutdown sweep from reaping the services this round is using
        (see :func:`service_manager.round_is_live`). It is refreshed
        before the services start, before every VP, and during the
        judgment phase, so it stays fresh for as long as the round is
        alive.

        The same heartbeat refreshes the **leases** the round holds on
        its services: a lease that stopped saying "still here" would
        expire mid-round and expose a running round's service to the
        next sweep. The lease lives in two places — the plan's ledger
        (one lease per service, the record a reaper reads) and the
        plan-level marker (the same leases, plus the round's own
        heartbeat) — and both are stamped together, with one timestamp.
        A refresh that only stamped one of them would leave the other
        reading as stale, and whichever reader consulted the stale one
        would conclude the round is gone.

        Best-effort: a marker that cannot be written costs the round its
        protection from foreign sweeps, which is strictly better than
        taking the round down.
        """
        try:
            from service_manager import refresh_round_liveness

            refresh_round_liveness(
                self.plan_dir,
                round_number=self._current_round,
                round_id=self._round_id(),
            )
        except Exception:  # noqa: BLE001 - the marker is best-effort
            # NOTE: the handler must not touch ``self`` either — a
            # half-built agent (the tests build one with ``__new__``) has
            # no ``plan_dir`` at all, and an except block that raises
            # turns a best-effort refresh into a crash.
            logger.debug(
                "[service_manager] could not refresh the round liveness "
                "marker",
            )

    # ------------------------------------------------------------------
    # The round's lease on its services — "is this still mine?"
    # ------------------------------------------------------------------
    #
    # 2026-10-25, observed live on a real round:
    # the round's api service was SIGTERM'd by another backend's
    # shutdown hook two minutes into the round, and the round did not
    # notice. It sent nine api_test requests over the next minute and
    # every one of them came back "connection refused" — so the report
    # carried nine identical failures and nothing else, and the repair
    # loop was pointed at nine features that had never been broken.
    #
    # The lease existed (it says "this service belongs to this round"),
    # but the round never read it back. Reading it back is what turns
    # "the service is gone" into "the service was taken away from me",
    # and a round-level incident into a round-level conclusion.

    def _round_lease_expectations(self) -> List[Dict[str, Any]]:
        """The leases this round believes it holds.

        The round's own runtime map is the record of what it started:
        every service it spawned carries the lease it took on it. An
        adopted service (somebody else's listener) and a service that
        never came up carry none — nobody claimed them, so nobody can
        lose them.
        """
        runtime_map = getattr(self, "_service_runtime_map", None)
        if runtime_map is None:
            return []
        expectations: List[Dict[str, Any]] = []
        runtimes = getattr(runtime_map, "runtimes", None) or {}
        for name, runtime in runtimes.items():
            lease = getattr(runtime, "lease", None)
            if not isinstance(lease, dict) or not lease.get("owner"):
                continue
            try:
                port = int(lease.get("port") or getattr(runtime, "port", 0) or 0)
            except (TypeError, ValueError):
                port = 0
            expectations.append({
                "name": str(name),
                "port": port,
                "owner": str(lease.get("owner") or ""),
                "listener_pid": lease.get("listener_pid"),
                "heartbeat_at": float(lease.get("heartbeat_at") or 0.0),
            })
        return expectations

    def _assert_round_lease(self) -> Optional[Dict[str, Any]]:
        """Is this round still the owner of the services it started?

        Returns ``None`` when the round still holds its leases, and the
        loss record when it does not. The loss record is written down
        once (see :meth:`_record_round_lease_lost`) and kept on the
        agent, so every later call says the same thing.

        Two ways to lose a lease, and both are read out of the ledger —
        the record the reapers read:

          * **the lease is gone** — the ledger no longer holds a lease
            for this round's service, or holds one owned by somebody
            else. That is what a foreign reap looks like: the reaper
            finalises the ledger, and the round's claim goes with it;
          * **the lease went stale** — the lease is still there, but
            nobody has stamped it for longer than
            :data:`service_manager.LEASE_TTL_SECONDS`. A reaper would
            read that lease as dead, so the round must not read it as
            alive.

        Never raises: a check that cannot read the ledger finds no
        lease to complain about and says nothing, because a broken read
        is not evidence of a lost lease.
        """
        recorded = getattr(self, "_round_lease_lost", None)
        if recorded:
            return recorded
        expectations = self._round_lease_expectations()
        if not expectations:
            # The round owns no leased service — nothing to lose.
            return None
        plan_dir = getattr(self, "plan_dir", None)
        if plan_dir is None:
            return None
        try:
            from service_manager import (
                LEASE_KEY, LEASE_TTL_SECONDS, ServiceLease,
                lease_heartbeat_age, read_ledger, read_round_liveness,
            )
        except Exception:  # pragma: no cover - defensive
            return None

        round_id = self._round_id()
        ledger_leases: Dict[Any, Any] = {}
        for entry in read_ledger(plan_dir).get("services") or []:
            if not isinstance(entry, dict):
                continue
            lease = ServiceLease.from_dict(entry.get(LEASE_KEY))
            if lease is None:
                continue
            try:
                port = int(entry.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            ledger_leases[(str(entry.get("name") or ""), port)] = lease

        # The marker keeps its own copy of the leases. It is the round's
        # last word on when it said "still here", so it is where
        # ``last_heartbeat_at`` comes from once the ledger's copy is
        # gone.
        marker_leases: Dict[int, Any] = {}
        for item in (read_round_liveness(plan_dir).get("leases") or []):
            if not isinstance(item, dict):
                continue
            try:
                marker_leases[int(item.get("port") or 0)] = item
            except (TypeError, ValueError):
                continue

        now = time.time()
        for expectation in expectations:
            key = (expectation["name"], expectation["port"])
            lease = ledger_leases.get(key)
            if lease is None:
                return self._record_round_lease_lost(
                    expectation,
                    reason=(
                        "the plan's ledger no longer holds a lease for this "
                        "round's service"
                    ),
                    marker_lease=marker_leases.get(expectation["port"]),
                )
            if not str(lease.owner or "").startswith(f"{round_id}@"):
                return self._record_round_lease_lost(
                    expectation,
                    reason=(
                        f"the lease on this round's service is now held by "
                        f"{lease.owner!r}"
                    ),
                    last_lease=lease,
                    marker_lease=marker_leases.get(expectation["port"]),
                )
            age = lease_heartbeat_age(lease, now=now)
            if age is None or age > float(LEASE_TTL_SECONDS):
                return self._record_round_lease_lost(
                    expectation,
                    reason=(
                        f"the lease's heartbeat is {age!r}s old, which is "
                        f"outside the {LEASE_TTL_SECONDS}s window a reaper "
                        f"reads it with"
                    ),
                    last_lease=lease,
                    marker_lease=marker_leases.get(expectation["port"]),
                )
        return None

    def _record_round_lease_lost(
        self,
        expectation: Dict[str, Any],
        *,
        reason: str,
        last_lease: Optional[Any] = None,
        marker_lease: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Write down that the round lost its lease, and stop the round.

        The record carries the three facts a reader needs to understand
        what happened, all of them read out of evidence that already
        exists rather than guessed:

          * ``last_reap_reason`` — why the service went away, as the
            plan's service-event log records it (the reaper's own
            words: "stopped (reason=server_shutdown)");
          * ``expected_pid`` — the process the round expected to be
            serving the port;
          * ``last_heartbeat_at`` — the last time the round said it was
            still here.

        The event is written to the plan's service-event log (where the
        reap that caused it is also recorded) **and** to the round's own
        log. Written once: the loss is a fact about the round, not about
        every VP that follows it.
        """
        from service_manager import last_reap_reason, record_service_event

        name = str(expectation.get("name") or "")
        port = expectation.get("port")
        reap_reason = ""
        try:
            reap_reason = last_reap_reason(
                self.plan_dir, name=name, port=port,
            )
        except Exception:  # noqa: BLE001 - the evidence is best-effort
            reap_reason = ""

        heartbeats: List[float] = [float(expectation.get("heartbeat_at") or 0.0)]
        expected_pid = expectation.get("listener_pid")
        if last_lease is not None:
            heartbeats.append(float(getattr(last_lease, "heartbeat_at", 0.0) or 0.0))
            if getattr(last_lease, "listener_pid", None):
                expected_pid = last_lease.listener_pid
        if isinstance(marker_lease, dict):
            heartbeats.append(float(marker_lease.get("heartbeat_at") or 0.0))
            if marker_lease.get("listener_pid") and not expected_pid:
                expected_pid = marker_lease.get("listener_pid")
        last_heartbeat_at = max(heartbeats) if heartbeats else 0.0

        loss = {
            "round_id": self._round_id(),
            "service": name,
            "port": port,
            "owner": expectation.get("owner"),
            "expected_pid": expected_pid,
            "last_heartbeat_at": last_heartbeat_at,
            "last_reap_reason": reap_reason,
            "reason": reason,
        }
        self._round_lease_lost = loss
        logger.error(
            "[verification] round_lease_lost: %s (service %r on port %s, "
            "expected_pid=%s, last_reap_reason=%r)",
            reason, name, port, expected_pid, reap_reason,
        )
        try:
            record_service_event(
                self.plan_dir,
                "round_lease_lost",
                round_token=str(expectation.get("owner") or ""),
                run_id=self.plan_dir.name,
                name=name,
                port=port if isinstance(port, int) else None,
                pid=expected_pid if isinstance(expected_pid, int) else None,
                reason=reason,
                details={
                    "last_reap_reason": reap_reason,
                    "expected_pid": expected_pid,
                    "last_heartbeat_at": last_heartbeat_at,
                    "round_id": self._round_id(),
                },
            )
        except Exception:  # noqa: BLE001 - an event write never raises
            logger.debug(
                "[service_manager] could not write the round_lease_lost "
                "event for %s", name,
            )
        self._safe_log_freshness_event("__round__", "round_lease_lost", loss)
        return loss

    def _round_lease_lost_verdict(self, vp_id: str = "") -> Dict[str, Any]:
        """The verdict for a VP that must not run: the round lost its lease.

        The VP's own criterion was not observed — the request was never
        made — so the verdict says exactly that, and names the
        round-level fault instead of repeating the symptom nine times.
        """
        loss = getattr(self, "_round_lease_lost", None) or {}
        reasons = [
            "round_lease_lost: this round no longer holds the lease on its "
            "services, so the verification point was not executed",
            f"lease: {loss.get('reason')}",
            f"expected_pid: {loss.get('expected_pid')}",
            f"last_heartbeat_at: {loss.get('last_heartbeat_at')}",
        ]
        if loss.get("last_reap_reason"):
            reasons.append(f"last_reap_reason: {loss.get('last_reap_reason')}")
        return {
            "status": "FAILED",
            "reasons": reasons,
            "actual_result": (
                "本轮丢失了对声明服务的租约，请求没有发出；这不是该验证点"
                "自身的失败。"
            ),
            "evidence": {"round_lease_lost": loss},
        }

    def _round_lease_lost_stop_verdict(self, vp_id: str = "") -> Dict[str, Any]:
        """The verdict for a VP that is **stopped** by the lease loss.

        These VPs never ran: the round found out that its services were
        taken away and stopped instead of asking a dead port the same
        question nine times. A stopped VP has no verdict of its own, so
        it is reported as skipped — with the round-level fault named,
        so a reader can see *why* it did not run.
        """
        loss = getattr(self, "_round_lease_lost", None) or {}
        reasons = [
            "round_lease_lost: the round lost the lease on its services, so "
            "this verification point was not executed",
            f"lease: {loss.get('reason')}",
        ]
        if loss.get("last_reap_reason"):
            reasons.append(f"last_reap_reason: {loss.get('last_reap_reason')}")
        return {
            "status": "SKIPPED",
            "reasons": reasons,
            "actual_result": (
                "本轮丢失了对声明服务的租约，该验证点没有执行。"
            ),
            "evidence": {"round_lease_lost": loss},
        }

    def _api_phase_service_lost_verdict(
        self, vp_id: str, loss: Dict[str, Any],
    ) -> Dict[str, Any]:
        """The verdict for the VP whose request produced no response.

        The request went out and nobody answered it — the service was
        gone.  The criterion was therefore **not observed**, and this
        verification point did not fail: the fault belongs to the round.
        The verdict says so, and names the round-level fact instead of
        repeating the symptom.
        """
        return {
            "status": "BLOCKED",
            "status_detail": "BLOCKED_SERVICE_LOST",
            "reasons": [
                "BLOCKED_SERVICE_LOST: the request produced no response — "
                "the service this round declared is not answering any "
                "more, so the criterion was not observed",
                f"port: {loss.get('port')}",
                f"expected_pid: {loss.get('expected_pid')}",
                f"actual_pid: {loss.get('actual_pid')}",
                f"error: {loss.get('transport_error')}",
                "last_reap_reason: "
                f"{loss.get('last_reap_reason')}",
                f"lease_owner: {loss.get('lease_owner')}",
            ],
            "actual_result": (
                "本轮 api 阶段的请求没有拿到任何响应（服务已不在监听）；"
                "判据没有被观测，也不是该验证点自身的失败。"
            ),
            "evidence": {
                "vp_id": vp_id,
                "status_detail": "BLOCKED_SERVICE_LOST",
                "api_phase_service_lost": loss,
            },
        }

    def _api_phase_service_lost_stop_verdict(
        self, vp_id: str = "",
    ) -> Dict[str, Any]:
        """The verdict for a VP that is **stopped** by the api-phase loss.

        These VPs never ran: the round found out that the service was
        gone and stopped instead of asking a dead port the same question
        nine times.  A stopped VP has no verdict of its own, so it is
        reported as skipped — with the round-level fault named, so a
        reader can see *why* it did not run.
        """
        loss = getattr(self, "_api_phase_service_lost", None) or {}
        reasons = [
            "api_phase_service_lost: the api phase lost its service, so "
            "this verification point was not executed",
            f"port: {loss.get('port')}",
            f"error: {loss.get('transport_error')}",
        ]
        if loss.get("last_reap_reason"):
            reasons.append(f"last_reap_reason: {loss.get('last_reap_reason')}")
        return {
            "status": "SKIPPED",
            "reasons": reasons,
            "actual_result": (
                "本轮 api 阶段丢失了服务（请求没有拿到任何响应），"
                "该验证点没有执行。"
            ),
            "evidence": {"api_phase_service_lost": loss},
        }

    def _round_error_report(self) -> Optional[Dict[str, Any]]:
        """The round-level error of this round, when it has one.

        A round that lost its lease on its services is an **error**,
        not a set of per-VP failures: the report has to say so at the
        top level, because "nine api_test VPs failed with connection
        refused" and "the api service was taken away by another
        backend" call for completely different repairs.

        The same is true of a request that produced no response: the
        service was gone, so the fact belongs to the round as well.
        """
        loss = getattr(self, "_round_lease_lost", None)
        if loss:
            return {
                "kind": "round_lease_lost",
                "message": (
                    "the round lost the lease on its services; the "
                    "verification points after the loss were not executed"
                ),
                "lease": loss,
            }
        api_loss = getattr(self, "_api_phase_service_lost", None)
        if api_loss:
            return {
                "kind": "api_phase_service_lost",
                "message": (
                    "the api phase lost its service — a request produced "
                    "no response; the verification points after the loss "
                    "were not executed"
                ),
                "api_phase_service_lost": api_loss,
            }
        return None

    # ------------------------------------------------------------------
    # The listener's identity — "is the answer this round's own?"
    # ------------------------------------------------------------------
    #
    # 2026-10-25, observed on a live run: an api criterion asked
    # whether the default backend refuses a request that does not carry
    # the guard header.  The criterion is about **this** instance: a
    # 403 from any process on the port would say nothing about the
    # backend the plan declared, and the round never asked who was
    # answering — it sent the request and graded whatever came back.
    # So a stranger's refusal (or a stranger's 200) was booked as the
    # declared service's answer.

    def _leased_listener_for_port(
        self, port: int, service_name: str = "",
    ) -> Optional[Dict[str, Any]]:
        """The lease the plan's ledger holds for ``port``.

        The ledger is the record every reaper reads, and the lease in
        it names the process that was serving the port when this round
        ensured the service — together with the round that owns it.

        ``None`` when the ledger holds no lease for that port: an
        adopted service (somebody else's listener), a service this
        round never started, or a ledger that cannot be read. None of
        those is a contradiction, and a check that invented one would
        stop every round that declared a service.
        """
        plan_dir = getattr(self, "plan_dir", None)
        if plan_dir is None:
            return None
        try:
            from service_manager import (
                LEASE_KEY, ServiceLease, read_ledger,
            )
        except Exception:  # pragma: no cover - defensive
            return None
        try:
            ledger = read_ledger(plan_dir)
        except Exception:  # noqa: BLE001 - a broken ledger is no lease
            return None

        wanted = str(service_name or "").strip().lower()
        fallback: Optional[Dict[str, Any]] = None
        for entry in ledger.get("services") or []:
            if not isinstance(entry, dict):
                continue
            try:
                entry_port = int(entry.get("port") or 0)
            except (TypeError, ValueError):
                continue
            if entry_port != int(port):
                continue
            lease = ServiceLease.from_dict(entry.get(LEASE_KEY))
            if lease is None or not lease.listener_pid:
                continue
            found = {
                "service": str(entry.get("name") or ""),
                "port": entry_port,
                "owner": str(lease.owner or ""),
                "listener_pid": int(lease.listener_pid),
            }
            if wanted and found["service"].strip().lower() == wanted:
                return found
            if fallback is None:
                fallback = found
        return fallback

    def _assert_service_listener_identity(
        self, vp: dict,
    ) -> Optional[Dict[str, Any]]:
        """Is the process answering this VP's port the one the round leased?

        Returns ``None`` when the identity cannot be contradicted, and
        the loss record when it can.

        Three answers, and the third one is the point:

          * **the port answers for the leased process** — the request
            is graded on the instance this round ensured, which is what
            the criterion is about;
          * **nothing to compare** — the ledger holds no lease for this
            port, so nobody claimed it and nobody can say the answer
            came from the wrong process;
          * **the port answers for somebody else** — the answer is not
            this round's, and a criterion graded on it would be graded
            on a stranger's behaviour.

        The third answer is what the 2026-10-25 round could not give:
        the service was taken away, another process took the port, and
        the 403 that came back was the stranger's.

        The holder is looked up with **lsof**, because the pid is the
        thing being compared. A probe that cannot name the holder says
        "I could not check", which is not "it is not mine" — and a
        check that accused the round on a degraded probe would stop
        every round that declared a service.
        """
        port = self._vp_target_port(vp)
        if port is None:
            return None
        leased = self._leased_listener_for_port(
            port, service_name=self._vp_service_key(vp),
        )
        if leased is None:
            return None
        expected_pid = int(leased["listener_pid"])
        try:
            from service_freshness import probe_port

            probe = probe_port(int(port))
        except Exception:  # noqa: BLE001 - the probe never raises
            return None
        if probe.error:
            # The holder could not be named: this round has no evidence
            # either way, and "I could not check" must never be read as
            # "it is not mine".
            return None
        actual_pid = int(probe.pids[0]) if probe.pids else None
        if actual_pid == expected_pid:
            return None
        return self._record_api_phase_service_lost(
            vp,
            port=int(port),
            expected_pid=expected_pid,
            actual_pid=actual_pid,
            leased=leased,
        )

    def _record_api_phase_service_lost(
        self,
        vp: dict,
        *,
        port: Optional[int],
        expected_pid: Optional[int] = None,
        actual_pid: Optional[int] = None,
        leased: Optional[Dict[str, Any]] = None,
        error: str = "",
        kind: str = "",
        scope: str = "vp",
        detail: str = "",
    ) -> Dict[str, Any]:
        """Write down that the api phase lost its service.

        Two shapes, one record and one event:

          * ``scope="vp"`` — the port answers for a process this round
            did not ensure.  The request was never made, and the
            verification point is blocked: its criterion was not
            observed.
          * ``scope="round"`` — the request went out and produced **no
            response** (the connection was refused, the peer went away).
            That is not this verification point's failure but the
            round's: the service is gone, so the api phase stops here
            and the verification points after it are not asked the same
            question at a dead port.

        The record carries the facts a reader needs and cannot guess:
        the port, the process the round expected to be serving it, the
        process that is serving it now, the round that owns the lease,
        and why the expected process went away — as the plan's own
        service-event log records it, in the reaper's own words.

        Written to the round's log, next to the request it describes.
        A round-level loss is written under the round (``__round__``),
        because it is a fact about the round and not about one request.
        """
        from service_manager import last_reap_reason

        if leased is None:
            leased = (
                self._leased_listener_for_port(
                    int(port), service_name=self._vp_service_key(vp),
                )
                if port is not None else None
            ) or {}
        name = str(leased.get("service") or "") or self._vp_service_key(vp)
        if expected_pid is None:
            expected_pid = leased.get("listener_pid")
        if actual_pid is None and port is not None:
            actual_pid = self._listener_pid(int(port))
        reap_reason = ""
        try:
            reap_reason = last_reap_reason(
                self.plan_dir, name=name, port=port,
            )
            if not reap_reason and name:
                # The reap may have named the service on another port
                # (the preflight relocated it); the reason is about the
                # **service**, so the port alone must not hide it.
                reap_reason = last_reap_reason(
                    self.plan_dir, name=name, port=None,
                )
        except Exception:  # noqa: BLE001 - the evidence is best-effort
            reap_reason = ""

        if scope == "round":
            detail = detail or (
                f"the request to port {port} produced no response "
                f"({error}); the service this round declared is gone, so "
                f"the api phase was stopped here"
            )
        else:
            detail = detail or (
                f"the process answering port {port} is not the instance "
                f"this round ensured (expected pid {expected_pid}, found "
                f"{actual_pid}); the request was not made"
            )
        record = {
            "scope": scope,
            "round_id": self._round_id(),
            "vp_id": str(vp.get("id", "unknown")),
            "service": name,
            "port": int(port) if port is not None else None,
            "expected_pid": (
                int(expected_pid) if expected_pid is not None else None
            ),
            "actual_pid": actual_pid,
            "last_reap_reason": reap_reason,
            "lease_owner": str(leased.get("owner") or ""),
            "transport_error": error,
            "transport_error_kind": kind,
            "detail": detail,
        }
        logger.error(
            "[verification] api_phase_service_lost: %s", record["detail"],
        )
        self._safe_log_freshness_event(
            "__round__" if scope == "round" else record["vp_id"],
            "api_phase_service_lost", record,
        )
        return record

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
        # 2026-10-27：解析的同时记下「这个 VP 的 URL 来自哪个声明服务」。
        # 展开之后 URL 里就没有占位符了，VP 自己说不出服务是谁 —— 而服务
        # 死掉以后要把它按原端口拉起来，正需要这个名字（见
        # :meth:`_vp_placeholder_service`）。绑定只存在内存里。
        self._record_vp_service_bindings(plan_data)
        self._safe_log_freshness_event(
            "__preflight__", "service_placeholders_resolved",
            {"services": sorted(self._service_declarations)},
        )
        return resolved

    def _record_vp_service_bindings(self, plan_data: dict) -> None:
        """Bind every VP whose URL names a service to that service.

        The binding answers a question the expanded URL can no longer
        answer: *which* declared service is this VP about to talk to.
        It is the round's own bookkeeping, so it is kept **in memory
        only** — the plan on disk keeps its placeholders and is
        re-resolved every round, and nothing here may ever be written
        back to it.
        """
        points = (
            plan_data.get("verification_points")
            if isinstance(plan_data, dict) else None
        )
        if not isinstance(points, list):
            return
        try:
            from service_declaration import iter_placeholders
        except Exception:  # pragma: no cover - defensive
            return
        for vp in points:
            if not isinstance(vp, dict):
                continue
            vp_id = str(vp.get("id") or "").strip()
            if not vp_id:
                continue
            request = vp.get("request") or {}
            url = str(request.get("url") or "")
            if "{{" not in url:
                # The VP names no service any more.  Drop the binding
                # a previous round left for it: a stale name would
                # attribute this VP to a service it does not talk to.
                self._vp_service_bindings.pop(vp_id, None)
                continue
            try:
                for _raw, name, _field in iter_placeholders(url):
                    key = str(name).strip().lower()
                    if key in self._service_declarations:
                        self._vp_service_bindings[vp_id] = key
                    break
            except Exception:  # pragma: no cover - defensive
                continue

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
        # 2026-10-26（VP-031）：投影这一层要钉住两件事 ——
        #
        #   * 这条结论是**哪条定义**的结论（计划里查得到吗）；
        #   * 它是在**哪次提交**上判出来的（与当前 HEAD 一致吗）。
        #
        # 查不到定义 → 旧结论**不许沿用**：那条 id 这一轮压根没被分派
        # （执行器的计划就是这份计划），它带着的失败是**别的轮次、别的
        # 提交**上挣来的。判据是 :func:`definition_missing` —— 计划里一条
        # VP 都没有时不下这个判断（那是输入退化，没有计划可比对），分派前
        # 与这里用的是同一条规则。
        definitions = self._plan_vp_definitions(plan_data)
        current_head = self._current_project_head()

        results: List[Dict[str, Any]] = []
        for v in executor_verdicts:
            vp_id = str(v.get("vp_id", "unknown"))
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
            judged_head = self._judged_head_of(v)
            row: Dict[str, Any] = {
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
                    row[key] = str(value)
            if judged_head:
                row[JUDGED_HEAD_FIELD] = judged_head
            if definition_missing(definitions, vp_id):
                # 无定义 → 不沿用旧结论。旧结论判在哪次提交上要留痕
                # （审计要能回答"被丢掉的是哪条旧结论"）。
                discarded = self._build_definition_missing_verdict(vp_id)
                discarded_evidence = dict(discarded.get("evidence") or {})
                if judged_head:
                    discarded_evidence["discarded_judged_head"] = judged_head
                discarded["evidence"] = discarded_evidence
                row = {
                    "id": vp_id,
                    "status": discarded["status"],
                    "actual_result": discarded["actual_result"],
                    "evidence": json.dumps(discarded_evidence, ensure_ascii=False),
                }
                if current_head:
                    row[JUDGED_HEAD_FIELD] = current_head
            elif self._is_unexecuted_final_gate(
                v, definitions.get(vp_id),
            ):
                # 2026-10-27：本轮**没跑**的 Phase 2 全量关卡。
                #
                # 报告里只能记 ``DEFERRED``（本轮延后，下轮真跑），旧结论
                # 留在 evidence 里作审计痕迹。**不能**让一条本轮没跑出来的
                # 结论带着 ``PASSED`` / ``FAILED`` 进报告：前者会让"整仓
                # 门禁通过"这个合入判据成立在一次没跑的门禁上，后者会为
                # 一个没跑过的关卡生成修复任务。
                row = self._build_deferred_gate_row(v, vp_id, current_head)
            elif requires_reexecution(row, current_head):
                # 旧提交上判出来的失败：这一轮**没验过**当前这份代码。
                # 它必须重跑，不能当成本轮的结论。
                row["reexecute_required"] = True
                row["reexecute_reason"] = (
                    f"这条失败判在 {row.get(JUDGED_HEAD_FIELD) or '(未知提交)'} 上，"
                    f"当前 HEAD 是 {current_head} —— 当前这份代码还没被它验过，必须重跑。"
                )
            results.append(row)

        # 2026-10-26（VP-031，合并后的口径）：被隔离的孤儿裁决（main 的
        # ``executor._quarantine_orphan_verdicts``）不进 ``_verdicts`` ——
        # 它不是任何东西的失败，旧结论也不许沿用（那是别的轮次、别的提交上
        # 挣来的）。但它也**不许静默消失**：报告里要看得见"有个 id 谁也跑
        # 不了"（本分支的 BLOCKED 行），否则操作者看到的是一份少了一条点
        # 的报告，看不出这一轮少跑了一个。
        #
        # 判据与分派前是同一条（:func:`definition_missing`）：计划里查得到
        # 这个 id 的不算孤儿 —— 它只是上一轮被作废过，这一轮会重跑并挣到
        # 一条新结论。
        for orphan_id in sorted(
            getattr(executor, "_orphaned_vps", {}) or {}
        ):
            if not definition_missing(definitions, orphan_id):
                continue
            if any(str(r.get("id")) == orphan_id for r in results):
                continue
            discarded = self._build_definition_missing_verdict(orphan_id)
            orphan_row = {
                "id": orphan_id,
                "status": discarded["status"],
                "actual_result": discarded["actual_result"],
                "evidence": json.dumps(
                    dict(discarded.get("evidence") or {}), ensure_ascii=False,
                ),
            }
            if current_head:
                orphan_row[JUDGED_HEAD_FIELD] = current_head
            results.append(orphan_row)

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

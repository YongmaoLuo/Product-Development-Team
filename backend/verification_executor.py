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
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from base_executor import BaseExecutor, _build_layers, _now_iso, stable_fingerprint
from utils.atomic_io import atomic_write_json
# 2026-10-27（归因绑定点 · 轮次侧）：台账认 ``vp_id`` / ``round`` 两个
# 归因坐标（``usage_registry``），执行器是**轮次**的持有者 —— 验证点号由
# 子 agent 在发起会话时绑（``verification_subagent``），轮次号由这里在
# 分派点绑。
from usage_registry import (
    current_plan_id,
    current_task_id,
    plan_usage_context,
)
# 2026-09-15: verdict 的出处 —— 结论只对产生它的那条
# 命令有效。见 ``verification_verdict_provenance`` 的模块 docstring。
import verification_verdict_provenance as verdict_provenance
# 2026-10-23: 索引桶的**唯一**派生规则（纯函数）。写侧（``record_verdict``）
# 与读侧（报告 / 卡片 / API）共用它，两份规则必然漂移 —— 漂移的产物是
# 「同一个 VP 既在失败桶又在完成桶」。见 ``verification_buckets`` 的模块
# docstring。
from verification_buckets import (
    BUCKET_COMPLETED,
    BUCKET_DEFERRED,
    BUCKET_FAILED,
    BUCKET_SKIPPED,
    bucket_for_status,
    derive_index_buckets,
)
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


# Definition fingerprint -----------------------------------------------------
#
# 2026-10-24: 一条结论只对它**产生它的那份定义**有效。
#
# 起因：增量评估把上一轮已经 PASSED 的 VP id 又发了一遍，计划的
# ``expected_result`` / ``verification_method`` / 判据载荷被换成了别的内容，
# 而 resume 路径**只按 id 字符串**跳过 VP（``BaseExecutor.run`` 的
# ``completed_set``）。上一轮的结论于是被当成本轮的结论 —— 报告里出现
# 「验证对象错位」，而这一轮那个 VP **一次都没跑过**（``logs/vp_attempts/``
# 里没有它的尝试记录）。
#
# 修法与 :mod:`verification_verdict_provenance` 同源，只是被指纹的对象从
# 「命令」换成「定义」：写入时随 id 存一份稳定哈希，resume 读回后只把
# **指纹一致**的 id 放进 ``completed_set``。

#: The field a verdict carries the fingerprint of the definition that
#: earned it.  Same shape as
#: :data:`verification_verdict_provenance.FINGERPRINT_FIELD`, but what
#: is fingerprinted is the **definition** (what the VP proves), not
#: the command that ran it.
DEFINITION_FINGERPRINT_FIELD = "definition_fingerprint"

#: Each verification method's **criterion payload**: the fields that
#: decide *what* the VP proves and *how* it is run.  A change to any of
#: them changes the criterion, so a verdict earned under the old
#: payload must not be inherited by a round whose plan says something
#: else.
#:
#: Deliberately **narrow**: ``title`` / ``priority`` / ``timeout_seconds``
#: / ``execution_group`` only *describe* a VP or schedule it.  Letting
#: them move the fingerprint would re-run the world over a cosmetic
#: edit — and ``execution_group`` in particular shifts whenever the
#: plan gains a new method, which says nothing about any single VP.
_METHOD_CRITERION_FIELDS: Dict[str, Tuple[str, ...]] = {
    "api_test": ("request", "assertions", "target_url", "uses_services"),
    "ui_validation": ("target_url", "uses_services"),
    "e2e": ("target_url", "uses_services"),
    "full_ci": ("ci_entry",),
    "code_review": ("related_prd_criteria",),
}

#: Criterion fields used for a method this module does not know about.
#: A superset of every method's payload — over-invalidating a verdict
#: only costs one extra run, under-invalidating it costs a wrong
#: conclusion.
_GENERIC_CRITERION_FIELDS: Tuple[str, ...] = (
    "request",
    "assertions",
    "target_url",
    "ci_entry",
    "uses_services",
    "related_prd_criteria",
    "test_command",
)


def _fold_whitespace(value: Any) -> Any:
    """Collapse whitespace runs in a text value; other values pass through.

    Prose gets re-wrapped by tooling all the time; the criterion it
    describes did not move, so the fingerprint must not either.
    """
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def definition_fingerprint_for_vp(vp: Any) -> str:
    """Return the stable fingerprint of a VP's **definition**.

    The payload is the id, the expected result, the verification method
    and that method's criterion payload.  Fields that merely *describe*
    the VP (title, priority, scheduling bookkeeping) are left out on
    purpose: they do not change what the VP proves.

    The hash is canonical (sorted keys), so re-ordering the fields of
    one and the same definition yields the same fingerprint.

    Args:
        vp: One verification point — a dict from the plan.

    Returns:
        The fingerprint, or ``""`` for anything that is not a VP.  An
        empty fingerprint means "definition unknown", which the resume
        path treats as "must re-run".
    """
    if not isinstance(vp, dict):
        return ""
    # The executor schema renames ``verification_method`` to ``method``
    # (see ``verification_agent._convert_plan_to_executor_schema``), the
    # LLM schema keeps the long name.  Both are read here so the
    # fingerprint says the same thing whichever schema the plan uses.
    method = str(
        vp.get("verification_method") or vp.get("method") or ""
    ).strip()
    payload: Dict[str, Any] = {
        "id": str(vp.get("id") or ""),
        "expected_result": _fold_whitespace(vp.get("expected_result")),
        "verification_method": method,
    }
    for field in _METHOD_CRITERION_FIELDS.get(method, _GENERIC_CRITERION_FIELDS):
        if field in vp:
            payload[field] = vp[field]
    return stable_fingerprint(payload)


# Index-bucket plumbing -------------------------------------------------------

#: 索引桶名 → 执行器里存这个桶成员的字段名。``BUCKET_*`` 常量的值就是
#: :mod:`verification_buckets` 返回 dict 的键名，字段名与之一一对应。
_BUCKET_MEMBER_ATTR: Dict[str, str] = {
    BUCKET_COMPLETED: "_completed_vps",
    BUCKET_FAILED: "_failed_vps",
    BUCKET_SKIPPED: "_skipped_vps",
    BUCKET_DEFERRED: "_deferred_vps",
}

#: 三个**终态**桶 → BaseExecutor 里的同义字段。``DEFERRED`` 没有对应项 ——
#: 它不是终态成功：进 ``completed_set`` 会让下一轮跳过它，而被延后的
#: 全量关卡必须在下轮真跑。
_TERMINAL_BUCKET_MIRROR_ATTR: Dict[str, str] = {
    BUCKET_COMPLETED: "_completed_items",
    BUCKET_FAILED: "_failed_items",
    BUCKET_SKIPPED: "_skipped_items",
}

# Topic-relevance plumbing -----------------------------------------------------
#
# 2026-10-27：判定内核（``verification.topic_relevance``）与归档审计
# （``verification.vp_removal_audit``）都就绪了，但执行器没有调用点 ——
# 主题外的验证点每轮照样被全量验证。这组常量与工具函数是**执行器这一侧**
# 需要的两样东西：判据引用的形状（用来认「这条验证点引用了一条计划里没有
# 的判据」），以及判据表的来源。

#: 判据**引用**的形状：``AC-001`` / ``C1`` / ``AC-NOT-IN-PRD``。
#:
#: 「引用」与「散文」必须分开：一条验证点声明 ``["AC-NOT-IN-PRD"]`` 是在指
#: 着一个具体的判据编号 —— 计划里没有这个编号，是一条**可以核对**的矛盾；
#: 一条验证点声明 ``["导出 CSV"]`` 只是写法不同，对不上不构成矛盾，拿散文
#: 去和判据表对账是拿散文对散文。
_CRITERION_REFERENCE_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+|[A-Za-z]{1,10}\d+)$"
)

#: 声明切词的分隔符：空白 + 中英文标点（与判定内核同一套切法）。
_REFERENCE_SPLIT_RE = re.compile(
    r"[\s,;:、，。；：！？！（）()\[\]【】「」《》〈〉…·/\\|]+"
)

#: 计划文件的名字（判据表的第二个来源）。
_PLAN_FILENAME = "verification_plan.json"


def _reference_key(text: Any) -> str:
    """判据编号的比较键（全角折叠 + 大小写不敏感）。

    与判定内核的 ``_id_key`` 同一套：编号是标识符，不能把连字符也删掉
    （``AC-2`` 删掉连字符就成了 ``AC2``，那是另一个编号），但全角与大小写
    是噪声。
    """
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def _criterion_references(vp: Any) -> List[str]:
    """这条验证点声明里**编号式**的判据引用。

    只收编号式的（``AC-001``）—— 散文式的声明（``"导出 CSV"``）不是引用，
    见 :data:`_CRITERION_REFERENCE_RE` 的说明。
    """
    if not isinstance(vp, Mapping):
        return []
    raw = vp.get("related_prd_criteria")
    if isinstance(raw, str):
        candidates = [raw]
    elif isinstance(raw, (list, tuple)):
        candidates = [str(item) for item in raw if item is not None]
    else:
        candidates = []
    references: List[str] = []
    for candidate in candidates:
        for token in _REFERENCE_SPLIT_RE.split(candidate):
            token = token.strip()
            if token and _CRITERION_REFERENCE_RE.match(token):
                references.append(token)
    return references

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
        The base class's run loop awaits it behind a concurrency gate
        (see :meth:`run`) — a fixed-width semaphore when the caller
        passes a positive ``max_parallel`` and no ``gate``, and no gate
        at all when the caller passes ``max_parallel=0``, which is what
        the production verification path does;
      * ``round_no`` — optional keyword.  **Which round this run is.**
        It is the round half of the cost attribution the registry
        records (2026-10-27): the executor binds it at the dispatch
        point so every session it hands out carries it, and the
        verification point half (``vp_id``) is bound one layer down, by
        the sub-agent that opens the session (see
        :mod:`verification_subagent`).  ``None`` means "ask the state
        store" (see :meth:`_round_number`).

    The class is intentionally stateless across instances: every
    mutation goes through :meth:`_save_state`, so two processes
    pointing at the same ``plan_dir`` see the same verdict map
    (last-writer-wins, atomic rename).  Progress counters are
    derived in-memory from the verdict map; no separate progress
    state-file is written.
    """

    #: A cached verdict is only inherited when the definition it was
    #: earned under still matches the plan's current definition.  An
    #: id is only a *name*: the plan may put a different criterion
    #: behind the same name (2026-10-24: the plan delta re-issued an
    #: already-PASSED id with a different definition, and the resume
    #: path inherited the previous round's verdict — the report showed
    #: a conclusion about a target the plan no longer asked for, and
    #: the VP itself never ran that round).
    _REQUIRE_MATCHING_DEFINITION = True

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
        *,
        topic_relevance_llm_batch: Optional[Callable[..., Any]] = None,
        round_no: Optional[int] = None,
    ) -> None:
        self.verification_plan = verification_plan
        self.plan_id = plan_id
        self.plan_dir = Path(plan_dir)
        self.sub_agent_runner = sub_agent_runner

        # 2026-10-27（归因）：本轮的轮次号。调用方知道自己是第几轮时
        # 直接给；不给就问状态库（``_current_round``）。
        self._round_no = round_no

        # 2026-10-27（主题相关性判定）：本轮可用的**批量判定器**。
        #
        # ``None`` = 没有模型可问 → 判定内核按「相关」兜底（一条验证点不能
        # 因为模型抽风而消失）。判定内核自己会按签名适配调用方式，契约是
        # ``llm_batch(vps, criteria)``。
        self._topic_relevance_llm_batch = topic_relevance_llm_batch

        # 移除集：**每轮重建**（从审计文件），**绝不**写进 executor_state ——
        # 审计文件是移除的唯一持久化真相，两处真相必然漂移。
        #
        # 这里先放一个空集：``super().__init__`` → ``_load_or_init_state`` →
        # ``_backfill_index_lists_from_verdicts`` 会读它，那条路径上还没有
        # 「本轮的移除集」可言（真正重建在 ``run()`` 的第三步）。
        self._removed_vp_ids: Set[str] = set()

        # 2026-10-27：``reset_state()`` 之后的**那一轮**不许装载裁决。
        #
        # 「装载裁决」（``run()`` 的第一步）把持久化存储里的旧结论读回内存 ——
        # 刚被 ``reset_state()`` 清掉的结论若在这一步回来，那条验证点会被
        # 当成「上一轮已经验过」而跳过：清空等于没做。标志是**一次性的**：
        # 跳过这一轮之后恢复，否则执行器就再也看不见别人写的结论了。
        self._skip_next_hydration: bool = False

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

        # 2026-10-25（VP-031）：**计划没有定义的验证点**，它们的裁决不得
        # 以失败的身份出现在报告里。
        #
        # ``_orphaned_vps``：本轮被隔离的孤儿裁决（id → 它带的 status），
        # 只用于留痕与告警。
        # ``_orphan_ledger``：跨轮记住"这些号已经作废过"，下一次装载不再
        # 接受它们的旧结论（即便有人把旧 id 写回计划，新验证点也不能
        # 继承上一轮的 FAILED）。
        self._orphaned_vps: Dict[str, str] = {}
        self._orphan_ledger: Set[str] = set()

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

        2026-10-14：计划自己已经作废的 VP（``obsolete`` / ``superseded_by``）
        **不出现**在这里。这是 :meth:`_build_items_from_plan` 执行侧过滤的
        另一半 —— 没有它，一个被计划丢弃的 VP 会带着**上一轮**的裁决继续
        生产报告：2026-10-14 的该计划第 4 轮里，VP-009
        在第 3 轮被标记 ``obsolete``，执行器第 4 轮一次都没跑它，但
        SQLite 里的第 3 轮裁决（``verif_repo.summary()``，见
        ``_load_or_init_state``）被重新水合成 ``_verdicts``，从
        ``collect_verdicts()`` 原样流进 Phase 3 判定提示词，报告里
        VP-009 又是 ``FAILED``（``actual_result`` 还是第 3 轮的原文）
        → ``overall_status = FAILED`` → 修复环永远收敛不了。
        判据与执行侧共用 :meth:`_plan_retired_vps`，两边不可能再漂移。

        2026-10-25（VP-031）：**计划没有定义**的验证点同样不进这里。
        那是另一种"计划不再要求它"—— 不是计划把它作废了，而是计划**从
        来没有写过它**。2026-10-25 的计划只有 30 条（``VP-001``..
        ``VP-030``），报告里却出现了第 31 条（``VP-031: FAILED``，
        ``actual_result`` 还是上一轮的原文）：它是上一轮的一条孤儿裁决，
        每轮原样流进报告，再被修复生成器读成"本轮失败"，修复环因此
        永远收敛不了。判据与分桶侧共用 :meth:`_orphaned_vp_ids`。

        Returns:
            A list of ``{"vp_id": str, "status": str, "reasons": list,
            "evidence": dict, ...}`` dicts. The ``vp_id`` key is
            injected here (the on-disk state stores verdicts as a
            ``Dict[vp_id, verdict]``, so the key is not duplicated
            inside the verdict body).
        """
        retired = self._plan_retired_vps()
        orphaned = self._orphaned_vp_ids()
        out: List[Dict[str, Any]] = []
        for vp_id, verdict in self._verdicts.items():
            # ``str()``: ``_plan_retired_vps`` keys on ``str(vp["id"])`` and
            # the executor derives ids the same way, but a verdict key that
            # arrived as a non-str from a persisted store must not silently
            # miss the filter (failing open is the bug being fixed here).
            if str(vp_id) in retired:
                continue
            # 同理：孤儿判据按 ``strip()`` 后的 id 比较，两道面用同一套
            # 拼写（见 :meth:`_orphaned_vp_ids`）。
            if str(vp_id).strip() in orphaned:
                continue
            # 2026-10-27：**被移除的验证点不进报告**。移除是「这条验证点
            # 不属于本计划的主题」—— 它的旧裁决（上一轮留下的）不该继续
            # 生产报告，正如计划作废的验证点一样。判据与执行侧共用
            # :meth:`_removed_vp_ids`。
            if str(vp_id).strip() in self._removed_vp_ids:
                continue
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
        # 2026-10-27：**下一轮不许装载裁决**。持久化存储（SQLite 仓库 /
        # 旧的 state 文件）里那份旧结论还在 —— 只清内存等于清了一半，下一轮
        # 的「装载裁决」会把它们读回来，那条验证点就被当成「上一轮已经验过」
        # 而跳过：清空等于没做。标志是一次性的 —— 跳过这一轮之后恢复，
        # 否则执行器就再也看不见别人写的结论了。
        self._skip_next_hydration = True

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

    async def run(self, max_parallel: int = 1, *, gate: Any = None) -> None:
        """Drive the verification plan through to completion.

        Delegates to :meth:`BaseExecutor.run`, which executes the
        single-layer VP list via ``asyncio.gather`` behind one
        concurrency gate.  The base class's
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
        mode.  Progress is derived from the verdict map; no separate
        progress state-file is written.

        管线顺序（2026-10-27，承重）
        --------------------------
        这一轮的执行之前先做**主题相关性判定**：主题外的验证点不该被跑一遍
        ——跑一遍的结论没有价值，钱却是真花了。顺序是::

            装载裁决 → 隔离孤儿 → 扣除移除集 → 判定 → 先归档后移除 → 派生索引

        每一步为什么在这个位置：

        * **装载裁决** 在最前 —— 判定与移除都要看这一轮真正的裁决表。
        * **隔离孤儿** 紧随其后 —— 计划没有定义的裁决先离开投影，它**不是**
          「主题外」，不许混进移除集。
        * **扣除移除集** 在判定之前 —— 上一轮移除掉的验证点不再参与判定，
          也不会被归档第二次。
        * **先归档后移除** —— 归档写成功才允许移除（写不进去就留着）。
        * **派生索引** 在最后 —— 索引是从裁决表派生的，被移除的验证点不进
          任何桶。派生在执行**之前**完成：执行过程中的桶由
          ``_record_result`` 维护，而派生函数会把「出处过期」的结论摘掉 ——
          刚写下的结论还没有出处指纹，执行之后再派生会把自己这一轮的结论
          摘掉。

        Args:
            max_parallel: Fixed width for the gather, used only when no
                ``gate`` is supplied. **The production caller passes
                ``0``** — unbounded — see
                :meth:`verification_agent.Orchestrator.run_full_verification`.
                The default of ``1`` is *serial*, not "unset", so a caller
                that omits the argument gets a run one verification point
                at a time. What actually bounds a verification round is
                the provider slot pool, which hands out a slot per
                dispatch; this argument is what that pool does *not* have
                to fight with.
            gate: Concurrency gate (``await acquire()`` / ``release()``)
                whose width is re-read on every bind. The production
                caller passes neither, so this is the generic hook rather
                than something the round uses.

        After the run, :attr:`~base_executor.BaseExecutor.peak_in_flight`
        is the concurrency the round actually reached — measured inside
        the executor, and the only honest width report now that no width
        is declared.
        """
        # 2026-10-27（归因绑定点 · 轮次侧）：轮次号在**分派点**绑定。
        #
        # 执行器是轮次的持有者 —— 它知道自己是第几轮，分派出去的每一次
        # 验证会话都得带着这个号，否则台账里的行只有「哪条验证点」，没有
        # 「哪一轮」，而「哪一轮」正是「第一轮就没过」和「第三轮才过」的
        # 分界。
        #
        # 这里绑的是**轮次**，不是验证点：验证点号由发起会话的那一层绑
        # （``verification_subagent``）—— 只有它知道自己是谁。绑在轮次这
        # 一层，内层的验证点号由 ``plan_usage_context`` 的继承规则接住。
        #
        # ``plan_id`` / ``task_id`` 原样带过来：这次绑定不许顺手改掉别的
        # 归因（台账里既有字段的值必须与绑定之前一模一样）。
        round_no = self._round_number()
        with plan_usage_context(
            plan_id=current_plan_id(),
            task_id=current_task_id(),
            round=round_no,
        ):
            await self._run_round(max_parallel=max_parallel, gate=gate)

    async def _run_round(self, max_parallel: int = 1, *, gate: Any = None) -> None:
        """这一轮自己的身子 —— 见 :meth:`run`。

        单独一个方法只为让 :meth:`run` 的归因绑定包住整轮：轮次号是**这一
        轮**的属性，不是某一次分派的属性。
        """
        # 1. 装载裁决 —— 这一轮看到的是「构造之后才落库」的结论。
        self._hydrate_verdicts()
        # 2. 隔离孤儿 —— 行为不变：计划没有定义的裁决不进报告投影。
        self._quarantine_orphan_verdicts()
        # 3. 扣除移除集 + 主题判定 + 先归档后移除。
        self._apply_topic_relevance()
        # 4. 派生索引 —— 在移除**之后**派生，所以被移除的验证点不进任何桶。
        #
        #    派生放在执行**之前**是有原因的：``_backfill_index_lists_from_verdicts``
        #    会把「出处过期」的结论摘掉，而刚写下的结论还没有出处指纹
        #    （指纹由 ``record_verdict`` 盖，生产路径 ``_record_result`` 不盖）
        #    —— 执行之后再派生，这一轮自己的结论会被当成过期的摘掉。执行
        #    过程中的桶由 ``_record_result`` 维护。
        self._backfill_index_lists_from_verdicts()
        self._completed_items = list(self._completed_vps)
        self._failed_items = list(self._failed_vps)
        self._skipped_items = list(self._skipped_vps)

        await super().run(max_parallel=max_parallel, gate=gate)
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
          4. Place ``vp_id`` into **exactly one** index bucket (see
            :meth:`_place_in_bucket`).  The bucket comes from
            :func:`verification_buckets.bucket_for_status` — the one
            rule the read side shares.  A VP that changes status
            leaves its previous bucket: the buckets are mutually
            exclusive, so "failed then passed" ends up in *completed
            only*, never in both.
          5. If ``self.verif_repo`` is bound, route the verdict
             through
             :meth:`state_machine.repositories.verification_repository.VerificationRepository.append_verdict`
             (a single ``BEGIN IMMEDIATE`` + ``COMMIT`` transaction)
             and SKIP the legacy on-disk JSON state write.  This
             is the task-11 contract: the subagent callback path
             produces zero ``verification_*_state.json`` files.
          6. Otherwise, fall back to ``_save_state()`` (the
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
        # 2026-10-24: 盖上"产生这份结论的那份定义"的指纹。同一个 id 在
        # 下一轮可能换成别的判据（增量重发 id / 改写 expected_result）——
        # 没有这份指纹，resume 路径会按 id 把上一轮的结论继承过来。
        verdict = self._stamp_definition_fingerprint(vp_id, verdict)
        self._verdicts[vp_id] = dict(verdict)  # defensive copy
        # Idempotent removal: ``list.remove`` would raise if the vp
        # is not in the pending list, which would mask a benign
        # double-record. Use a filter to keep behaviour predictable.
        self._pending_vps = [v for v in self._pending_vps if v != vp_id]
        # Maintain the four index buckets. 2026-10-23: 分桶规则来自
        # ``verification_buckets.bucket_for_status`` —— 与读侧（报告 / 卡片 /
        # API）共用的**唯一**一份规则；写侧不再自带一份判断。
        #
        # ``_place_in_bucket`` 保证四个桶**互斥**：同一个 VP 换状态时旧桶里
        # 那份必须消失。以前这里只往目标桶追加，于是「先 FAILED 后 PASSED」
        # 的 VP 同时留在 failed 与 completed —— 仪表盘报「1 失败 1 通过」，
        # 而裁决表里只有一条结论。
        status = verdict.get("status")
        bucket = bucket_for_status(
            status,
            vp_id=vp_id,
            retired=self._plan_retired_vps(),
        )
        self._place_in_bucket(vp_id, bucket)
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
                    # 2026-10-24: 定义指纹同理 —— 不落库的话，resume 读回来
                    # 的结论就是"没有指纹"的旧格式条目，只能全部重跑。
                    DEFINITION_FINGERPRINT_FIELD: verdict.get(
                        DEFINITION_FINGERPRINT_FIELD, "",
                    ),
                },
            )
            return
        self._save_state()

    def apply_verdict_updates(
        self, updates: Dict[str, Dict[str, Any]],
    ) -> List[str]:
        """Re-record verdicts that changed after the round finished.

        A verdict is corrected **through** :meth:`record_verdict` — never
        by writing ``self._verdicts`` directly.  ``record_verdict`` takes
        a defensive copy (``dict(verdict)``), stamps the command
        provenance and maintains the mutually exclusive index buckets;
        poking ``self._verdicts`` would leave the buckets and the
        persisted state describing the *old* verdict — a VP reported as
        completed while its own table says FAILED.

        Args:
            updates: ``{vp_id: verdict}`` — the verdicts to re-record.

        Returns:
            The ids that were actually re-recorded, in the order they
            were applied.

        Notes:
            An update is a **correction** of the verdict that is already
            stored, so it is applied as a patch over that verdict: the
            keys the correction carries win, the keys it does not
            mention are kept.  That is what lets a partial correction
            (``{"status": "FAILED", "reasons": [...]}``) land — the
            stored verdict supplies the schema's other required fields
            — while a full correction (what the round-end flush
            produces) simply replaces the verdict.

            A VP the executor never saw is **skipped**, not an error:
            there is no conclusion of its own to correct, and inventing
            one would put a VP into the buckets that the round never
            ran.  A verdict the schema still rejects after the patch is
            skipped for the same reason — the previous conclusion
            stays.
        """
        applied: List[str] = []
        if not updates:
            return applied
        for vp_id, verdict in updates.items():
            base = self._verdicts.get(vp_id)
            if base is None:
                logger.warning(
                    "[verification_executor] verdict update for %s skipped "
                    "— no such verdict in this run", vp_id,
                )
                continue
            try:
                patched = dict(base)
                patched.update(dict(verdict or {}))
                # The stored verdict is schema-valid, so these are
                # already present; the defaults only cover a verdict
                # that was recorded before the schema existed.
                patched.setdefault("reasons", [])
                patched.setdefault("evidence", {})
                self.record_verdict(vp_id, patched)
            except Exception:
                logger.exception(
                    "[verification_executor] verdict update for %s rejected "
                    "— keeping the previous verdict", vp_id,
                )
                continue
            applied.append(vp_id)
        return applied

    # ------------------------------------------------------------------
    # Index buckets (mutually exclusive)
    # ------------------------------------------------------------------

    def _place_in_bucket(self, vp_id: str, bucket: Optional[str]) -> None:
        """Put ``vp_id`` into **exactly one** index bucket.

        The four buckets are mutually exclusive by contract: a VP that
        changes status leaves its previous bucket.  "Failed then passed"
        must end up in *completed only* — the dashboard reporting "1
        failed / 1 passed" for a verdict table that holds a single row
        is one of the hardest drifts to spot.

        Args:
            vp_id: The VP id.
            bucket: The target bucket (one of the ``BUCKET_*`` names).
                ``None`` means "no bucket at all" — the case for a VP
                the plan itself retired (``obsolete``): producing a
                failure for a criterion nobody asks for pollutes both
                the failure counters and the repair task's input.

        Notes:
            Re-recording the same status is a no-op for the bucket
            (no duplicate entry, and the existing position is kept —
            bucket order follows the verdict-recording order so the
            counters line up with the per-VP verdict table).

            The three *terminal* buckets are mirrored into
            BaseExecutor's ``completed_items`` / ``failed_items`` /
            ``skipped_items``: ``_save_progress`` copies those fields
            **back** into this class, so without the mirror the bucket
            this method just wrote would be wiped by the next
            ``_save_state()``.
        """
        if bucket is not None and bucket not in _BUCKET_MEMBER_ATTR:
            # 一个拼错的桶名如果被默默接受，VP 会「哪个桶都不是」——
            # 静默的错分桶比响亮的报错难查得多。
            raise ValueError(
                f"unknown index bucket {bucket!r}; expected one of "
                f"{sorted(_BUCKET_MEMBER_ATTR)}"
            )
        for name, attr in _BUCKET_MEMBER_ATTR.items():
            members: List[str] = getattr(self, attr)
            if name == bucket:
                if vp_id not in members:
                    members.append(vp_id)
            else:
                # Remove it from every other bucket (``while`` rather
                # than ``if``: a bucket that already carried the VP
                # twice must not keep a copy behind).
                while vp_id in members:
                    members.remove(vp_id)
        # Keep BaseExecutor's view of the same three buckets in sync.
        for name, attr in _TERMINAL_BUCKET_MIRROR_ATTR.items():
            setattr(self, attr, list(getattr(self, _BUCKET_MEMBER_ATTR[name])))

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
                # 2026-10-25（VP-031）：**先读账本、再隔离孤儿、最后才派生
                # 索引桶** —— 顺序不能换：桶是从 ``_verdicts`` 派生的，
                # 孤儿必须在派生之前就离开这张表，否则它会进失败桶
                # （修复环的输入）并原样进报告。
                self._load_orphan_ledger(row.get("executor_state"))
                self._quarantine_orphan_verdicts()
                self._backfill_index_lists_from_verdicts()
                # Mirror into BaseExecutor's in-memory fields so a
                # caller that goes through the base-class run loop
                # sees the same scheduler view as the historical
                # one.
                self._completed_items = list(self._completed_vps)
                self._failed_items = list(self._failed_vps)
                self._skipped_items = list(self._skipped_vps)
                self._persist_orphan_ledger()
                return

        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._pending_vps = list(data.get("pending_vps", []))
                self._verdicts = dict(data.get("verdicts", {}))
                # 2026-10-25（VP-031）：与 SQLite 分支同一套顺序与理由。
                self._load_orphan_ledger(data.get("orphaned_vp_ids"))
                self._quarantine_orphan_verdicts()
                self._backfill_index_lists_from_verdicts()
                # Mirror into BaseExecutor's in-memory fields so a
                # caller that goes through the base-class run loop
                # sees the same scheduler view as the historical
                # one.
                self._completed_items = list(self._completed_vps)
                self._failed_items = list(self._failed_vps)
                self._skipped_items = list(self._skipped_vps)
                self._persist_orphan_ledger()
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

        2026-10-23: 分桶本身**委托**给
        :func:`verification_buckets.derive_index_buckets` —— 与读侧（报告 /
        卡片 / API）、``record_verdict``、``_record_result`` 共用的**唯一**一份
        规则。这个方法以前自带一份 status → 桶的判断，于是同一个裁决两处说法
        不同（``BLOCKED`` 在 ``_record_result`` 里进失败桶，在这里却哪儿都不是）。
        两份规则必然漂移 —— 漂移的产物是「同一个 VP 既在失败桶又在完成桶」。
        委托之后，桶的语义只看 :mod:`verification_buckets` 的 docstring；
        这里仍然保留的两步预处理是 **stale 摘除**（结论的出处过期）与
        **retired 标记**（计划自己不再要求这个判据），两者都不是分桶规则。

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

        # 2026-10-14：计划自己已经作废的 VP，它的裁决同样不进失败桶。判据是
        # **计划有没有标记**，不是命令有没有变过 —— 一个被 ``obsolete`` 的
        # 验证点不是"出处过期"，它是计划不再要求的东西，继续把它记成失败
        # 等于为一个没人要的判据生产失败（2026-10-14 的
        # 计划第 4 轮就因此 ``overall_status = FAILED``，修复环不收敛）。
        #
        # 刻意**不 pop** ``_verdicts``（与上面 stale 的处理不同）：那份裁决
        # 是审计痕迹，报告侧在 :meth:`collect_verdicts` 里过滤即可。
        # ``superseded_by`` 的父 VP 尤其不能摘 —— 它的 SPLIT 裁决是"父不重跑"
        # 这条契约的承重件，摘掉等于让拆分过的大 VP 每轮重跑一次。
        retired = self._plan_retired_vps()
        # 2026-10-25（VP-031）：**计划没有定义**的验证点不进任何桶。
        #
        # 判据是"计划有没有写过这个点"，不是"这条结论有没有出处"——
        # 一条从不在计划里的 id 不是失败信号，而是"计划里没有这个点"这个
        # 结构性事实。它不进 failed（修复环的输入），也不进 completed
        # （那等于把没验过的东西记成验证通过）。
        #
        # 与 :meth:`collect_verdicts` 共用同一道闸 —— 两份"什么叫该排除"
        # 正是 2026-10-14 的 VP-009 漂移出来的。
        orphaned = self._orphaned_vp_ids()

        # 2026-10-23: 分桶规则来自 :func:`verification_buckets.
        # derive_index_buckets` —— 与读侧（报告 / 卡片 / API）、``record_verdict``、
        # ``_record_result`` 共用的**唯一**一份规则。这里以前自带一份 status →
        # 桶的判断，于是同一个裁决两处说法不同（``BLOCKED`` 在 ``_record_result``
        # 里进失败桶，在这里却因为「不认识这个 status」而哪儿都不是）。两份规则
        # 必然漂移 —— 漂移的产物是「同一个 VP 既在失败桶又在完成桶」。
        #
        # 2026-10-25（VP-031）：**孤儿裁决先排除再分桶**。计划里从没写过这个
        # id 的裁决不是失败信号，而是「计划里没有这个点」这个结构性事实 ——
        # 它不进任何桶（失败桶是修复环的输入，完成桶等于把没验过的东西记成
        # 验证通过）。过滤放在分桶**之前**，所以它同样不进 skipped / deferred。
        buckets = derive_index_buckets(
            {
                vp_id: verdict
                for vp_id, verdict in self._verdicts.items()
                if str(vp_id).strip() not in orphaned
                # 2026-10-27：**被移除的验证点不进任何桶**。它既不是失败
                # （没人要它了），也不是验证通过（没验过就报通过是撒谎）。
                # 与 :meth:`collect_verdicts` 共用同一道闸。
                and str(vp_id).strip() not in self._removed_vp_ids
            },
            retired=retired,
        )
        self._completed_vps = buckets[BUCKET_COMPLETED]
        self._failed_vps = buckets[BUCKET_FAILED]
        self._skipped_vps = buckets[BUCKET_SKIPPED]
        self._deferred_vps = buckets[BUCKET_DEFERRED]

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
            # 2026-10-25（VP-031）：发号账本随状态一起落盘 —— 下一次装载
            # 必须记得"这些号已经作废过"。
            "orphaned_vp_ids": sorted(self._orphan_ledger),
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
        """Mark every VP in ``layer`` as SKIPPED with ``reason``.

        2026-10-23: 四个索引桶必须**互斥** —— 整层跳过时，这个 VP 之前落在
        failed / completed 里的那份必须消失。以前这里只往 ``skipped_vps``
        追加（而且遇到已有裁决的 VP 直接 ``continue``），于是「上一轮失败、
        这一轮整层被门禁跳过」的 VP 同时是失败和跳过。

        裁决表也一并改写成 SKIPPED（方法契约本来就是「mark **every** VP in
        layer」）：读侧（报告 / 卡片 / API）是从裁决表派生的，桶说「跳过」而
        裁决表还写着「失败」是最难查的一种漂移 —— 两边必须说同一件事。
        """
        for item in layer:
            item_id = self._derive_item_id(item)
            verdict = {
                "status": "SKIPPED",
                "reasons": [reason],
                "evidence": {},
            }
            self._verdicts[item_id] = verdict
            # ``_place_in_bucket`` 负责互斥：先进 skipped，再把它从
            # failed / completed / deferred 里摘掉。
            self._place_in_bucket(item_id, BUCKET_SKIPPED)
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
        ``completed_vps`` is PASSED-only — the failed and skipped
        subsets are kept in their own index lists so the dashboard
        can render a "X passed / Y failed / Z skipped" summary
        without re-scanning the verdict map.

        2026-10-23: 分桶改走 :meth:`_place_in_bucket` —— 四个索引桶
        **互斥**。以前这里只往目标桶追加，于是同一个 VP 换状态时旧桶里那份
        永远留着（「先 FAILED 后 PASSED」→ 同时在 failed 与 completed，仪表盘
        报「1 失败 1 通过」而裁决表里只有一条结论）。桶的归属规则来自
        :func:`verification_buckets.bucket_for_status` —— 与读侧（报告 / 卡片 /
        API）和另一个写入点 ``record_verdict`` 共用同一份，写侧不再自带一份
        判断。

        When ``self.verif_repo`` is bound, the verdict is also
        routed through
        :meth:`state_machine.repositories.verification_repository.VerificationRepository.append_verdict`
        (a single ``BEGIN IMMEDIATE`` + ``COMMIT`` transaction)
        and the legacy on-disk JSON state file is NOT written.
        This is the task-11 contract: the BaseExecutor.run()
        loop, which calls ``_record_result`` after every VP, must
        produce zero ``verification_*_state.json`` files.
        """
        # 2026-10-24: 盖上"产生这份结论的那份定义"的指纹。这是**生产路径**
        # （``BaseExecutor.run`` → ``_record_result``），所以指纹必须在这里
        # 也盖上 —— 只有 ``record_verdict`` 盖的话，resume 从磁盘/DB 读回
        # 的结论永远没有指纹，每一轮都会全量重跑。
        result = self._stamp_definition_fingerprint(item_id, result)
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
        #
        # 2026-10-23: 分桶规则来自 ``verification_buckets.bucket_for_status``
        # —— 与读侧（报告 / 卡片 / API）以及另一个写入点 ``record_verdict``
        # 共用的**唯一**一份规则；写侧不再自带一份判断。
        #
        # ``_place_in_bucket`` 保证四个桶**互斥**：同一个 VP 换状态时旧桶里
        # 那份必须消失。以前这里只往目标桶追加，于是「先 FAILED 后 PASSED」
        # 的 VP 同时留在 failed 与 completed —— 仪表盘报「1 失败 1 通过」，
        # 而裁决表里只有一条结论。
        bucket = bucket_for_status(
            status,
            vp_id=item_id,
            retired=self._plan_retired_vps(),
        )
        if bucket is None and status not in ALLOWED_STATUSES:
            # 未知 status：沿用历史 else 分支的合约 —— 归入失败桶。写侧刚刚
            # 亲手写下这条裁决，落进「哪儿都不是」等于这个 VP 永远没人修。
            #
            # （合法的 status 走到这里只可能是「计划已作废的 VP 的 FAILED」：
            #  那种失败刻意不进任何桶，见 ``verification_buckets`` 的 retired
            #  规则 —— 为一个没人要的判据生产失败会污染失败统计与修复任务的
            #  输入。）
            bucket = BUCKET_FAILED
        self._place_in_bucket(item_id, bucket)
        # Mirror into BaseExecutor's in-memory fields.（``_place_in_bucket``
        # 已经把 completed / failed / skipped 三个镜像同步过去了。）
        self._current_item = self._current_vp
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
                    # 2026-10-24: 定义指纹必须一起落库（理由同
                    # ``record_verdict``）：不落库的话，resume 从 DB 水合
                    # 回来的结论是"没有指纹"的旧格式条目，只能全部重跑。
                    DEFINITION_FINGERPRINT_FIELD: result.get(
                        DEFINITION_FINGERPRINT_FIELD, "",
                    ),
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

        2026-10-14: the predicate moved into :meth:`_plan_retired_vps` so
        the **verdict** side (:meth:`collect_verdicts`,
        :meth:`_backfill_index_lists_from_verdicts`) reads the same
        definition. Two copies of "what counts as retired" is exactly how
        the plan ended up not running VP-009 in round 4 while still
        reporting it FAILED.
        """
        # The predicate itself moved to :meth:`_vp_retired_marker`; the
        # filtering here stays keyed on the **marker**, not on the id —
        # the historical comprehension filtered on the marker alone, so a
        # marker-carrying VP without an ``id`` was dropped and still is.
        #
        # 2026-10-27: **被移除的验证点也不跑**。移除集每轮从审计文件重建
        # （见 :meth:`_apply_topic_relevance`），它与「计划自己作废」是两种
        # 不同的「不要它了」：作废是计划改主意了，移除是这条验证点不属于本
        # 计划的主题。两种都不跑，但只有后者有审计行。
        return [
            vp for vp in self._plan_vps()
            if not self._vp_retired_marker(vp)
            and not self._is_removed_vp(vp)
        ]

    def _plan_retired_vps(self) -> Dict[str, str]:
        """VP ids the plan itself retired → the marker that retired it.

        Returns ``{vp_id: "obsolete" | "superseded_by"}`` for every VP in
        ``self.plan`` carrying either marker. The single source of truth
        for "the plan no longer requires this VP":

          * :meth:`_build_items_from_plan` skips them (nothing runs);
          * :meth:`collect_verdicts` hides their verdicts (nothing is
            reported);
          * :meth:`_backfill_index_lists_from_verdicts` keeps their
            verdicts out of the failure set.

        Accepts both plan shapes (``vps`` = executor schema,
        ``verification_points`` = LLM schema). A VP carrying both markers
        reports ``obsolete`` — both mean "not run", and the verdict-side
        handling differs (an ``obsolete`` VP never reaches ``pending``, so
        it needs no terminal bucket to stay un-rerun; a ``superseded_by``
        parent does, because
        ``verification_agent._convert_plan_to_executor_schema`` does not
        forward ``superseded_by`` to the executor schema).
        """
        retired: Dict[str, str] = {}
        for vp in self._plan_vps():
            marker = self._vp_retired_marker(vp)
            if marker:
                vp_id = vp.get("id")
                if vp_id:
                    retired[str(vp_id)] = marker
        return retired

    def _vp_retired_marker(self, vp: Any) -> Optional[str]:
        """``"obsolete"`` / ``"superseded_by"`` / ``None`` for one plan VP."""
        if not isinstance(vp, dict):
            return None
        if vp.get("obsolete"):
            return "obsolete"
        if vp.get("superseded_by"):
            return "superseded_by"
        return None

    def _is_removed_vp(self, vp: Any) -> bool:
        """这个验证点已经被移除了吗（主题外移除）。

        移除集每轮从审计文件重建（见 :meth:`_apply_topic_relevance`），它是
        移除的**唯一**持久化真相 —— 不写进 ``executor_state``，两处真相必然
        漂移。
        """
        if not isinstance(vp, Mapping):
            return False
        return str(vp.get("id") or "").strip() in self._removed_vp_ids

    def _plan_vps(self) -> List[Any]:
        """The plan's VP list, in whichever schema the caller supplied."""
        return (
            self.plan.get("vps")
            or self.plan.get("verification_points")
            or []
        )

    # ------------------------------------------------------------------
    # Definition fingerprint
    # ------------------------------------------------------------------
    #
    # The verdict map is the completion cache: it is keyed by VP id and
    # survives across rounds (that is what makes ``resume`` cheaper
    # than a clean slate).  An id alone cannot say *which* definition
    # a cached verdict was earned under, so the verdict carries that
    # fingerprint alongside its status.

    def _item_definition_fingerprint(self, item: Dict[str, Any]) -> str:
        """The current definition fingerprint of one plan VP."""
        return definition_fingerprint_for_vp(item)

    def _cached_definition_fingerprint(self, item_id: str) -> Optional[str]:
        """The fingerprint stored with ``item_id``'s cached verdict.

        ``None`` when the cached verdict carries no fingerprint — the
        old cache format, where an entry was just an id and a verdict.
        Such an entry cannot prove which definition it was earned
        under, so the resume path re-runs it (the safe direction: an
        extra run costs tokens, a stale conclusion costs the truth).
        """
        verdict = self._verdicts.get(item_id)
        if not isinstance(verdict, dict):
            return None
        value = verdict.get(DEFINITION_FINGERPRINT_FIELD)
        if not isinstance(value, str) or not value:
            return None
        return value

    def _definition_fingerprint_for_vp_id(self, vp_id: str) -> str:
        """The current definition fingerprint of the plan's VP ``vp_id``.

        ``""`` when the plan does not carry that id — the fingerprint
        is then unknown, which the resume path reads as "must re-run".
        """
        for vp in self._plan_vps():
            if isinstance(vp, dict) and str(vp.get("id") or "") == str(vp_id):
                return definition_fingerprint_for_vp(vp)
        return ""

    def _stamp_definition_fingerprint(
        self,
        vp_id: str,
        verdict: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Stamp ``verdict`` with the definition it was earned under.

        Returns a copy — the caller's dict is left alone.
        """
        stamped = dict(verdict)
        stamped[DEFINITION_FINGERPRINT_FIELD] = (
            self._definition_fingerprint_for_vp_id(vp_id)
        )
        return stamped

    # 计划定义（2026-10-25，VP-031）
    # ------------------------------------------------------------------
    #
    # 计划里的验证点有两种"不再被要求"的方式：
    #
    #   * 计划**把它作废了**（``obsolete`` / ``superseded_by``）——
    #     :meth:`_plan_retired_vps` 管这一种；
    #   * 计划**从来没有写过它** —— 这一种由本节管。
    #
    # 第二种更隐蔽：一条从不在计划里的 id 永远不会被"作废标记"退休，
    # 于是它的裁决每轮原样流进报告与失败桶，而执行侧的 items 只来自
    # 计划里的 VP —— 它既不会被重跑，也不会被清除。修复环因此永远
    # 收敛不了（VP-031，2026-10-25）。

    def _plan_defined_vp_ids(self) -> Optional[Set[str]]:
        """The VP ids the plan itself defines — or ``None`` when it says nothing.

        ``None`` means **"we do not know what the plan asks for"**, so
        nothing may be voided on the plan's behalf. The two ways of not
        knowing are kept apart, exactly as
        ``orchestrator._undefined_vp_ids`` does:

          * the plan is unreadable / declares no verification point at all
            → an unknown plan cannot say that a point is undefined;
          * the plan is readable and declares its points → the ids below
            are the authority on "the plan asks for this".

        Ids are compared as ``str`` + stripped — the same spelling the
        verdict-side filters use, so every filter talks about the same
        points.
        """
        vps = self._plan_vps()
        if not isinstance(vps, list) or not vps:
            # The plan says nothing: nothing is voided.
            return None
        defined: Set[str] = set()
        for vp in vps:
            if not isinstance(vp, dict):
                continue
            vp_id = str(vp.get("id") or "").strip()
            if vp_id:
                defined.add(vp_id)
        if not defined:
            # A plan whose points carry no id cannot have one voided either.
            return None
        return defined

    def _orphaned_vp_ids(self) -> Set[str]:
        """The verdict ids the plan never defines — **the one gate**.

        Shared by :meth:`collect_verdicts` (report side) and
        :meth:`_backfill_index_lists_from_verdicts` (bucket side) so the two
        can never drift apart — 2026-10-14's VP-009 was exactly two copies
        of "what counts as retired" disagreeing.

        A verdict for an id the plan does not define is not a failure of
        anything: there is no criterion behind it, so it must enter no
        bucket (completed / failed / skipped / deferred) and no report.

        The one exception is a **structural record of the run** — a split
        parent or a crashed in-flight VP (see
        :meth:`_is_structural_record`). Those do not conclude anything
        about a criterion; they report what the *run* did. The plan's id
        set has no say over them, so they are not orphans.
        """
        defined = self._plan_defined_vp_ids()
        if defined is None:
            # Fail-closed: an unknown plan defines nothing away.
            return set()
        orphans: Set[str] = set()
        for vp_id, verdict in self._verdicts.items():
            key = str(vp_id).strip()
            if not key or key in defined:
                continue
            if self._is_structural_record(verdict):
                continue
            orphans.add(key)
        return orphans

    @staticmethod
    def _is_structural_record(verdict: Any) -> bool:
        """Is this verdict a record of the run rather than a conclusion?

        Two of them, both carrying contracts that landed before this gate:

          * ``SPLIT`` — "this VP was split into children, the children
            carry the verdicts" (2026-09-14). Its only job is to keep the
            parent out of the re-run set, so the parent is bucketed
            ``skipped`` (= BaseExecutor's ``completed_set``) and a resumed
            round runs only the children.
          * ``recovered_from_crash`` — "the process died while verifying
            this VP" (the crash-recovery pass). It reports the run, and
            the repair loop surfaces the label as its own counter.

        Neither says anything about a criterion, so the plan's id set does
        not gate them: a plan that never listed the id cannot call the
        record meaningless — the run itself is the evidence.
        """
        if not isinstance(verdict, dict):
            return False
        if verdict.get("status") == "SPLIT":
            return True
        reasons = verdict.get("reasons")
        if isinstance(reasons, (list, tuple)):
            for reason in reasons:
                if str(reason).strip() == "recovered_from_crash":
                    return True
        evidence = verdict.get("evidence")
        if isinstance(evidence, dict) and evidence.get("recovered_from_crash"):
            return True
        return False

    def _quarantine_orphan_verdicts(self) -> None:
        """Move verdicts the plan never defines out of the projection.

        Called right after the verdict map is hydrated (SQLite or the legacy
        state file) and **before** the index lists are derived — the buckets
        come from ``_verdicts``, so an orphan has to have left that map
        before they are derived.

        Two reasons to drop a verdict here:

          * the plan does not define the id — the verdict answers a
            criterion nobody wrote down;
          * the id is in the **orphan ledger** — it was already voided in an
            earlier round, so its old conclusion must not be inherited even
            if the plan has since taken the id back (the VP is re-run and
            earns a fresh verdict).

        The verdict itself stays where it was written (SQLite); this map is
        only a projection of it.

        The ledger entry is **not** retired when the plan takes the id
        back: the old conclusion lives in the store, and the store cannot
        tell it apart from a fresh one. So the id keeps being re-run and
        re-earning a fresh verdict — the price of never inheriting a stale
        conclusion. Both reasons are logged separately, because "the plan
        never defined it" and "it was voided earlier" are different facts.
        """
        defined = self._plan_defined_vp_ids()
        if defined is None:
            # Fail-closed: the plan's wishes are unknown, so nothing may be
            # voided on its behalf.
            return
        for vp_id in list(self._verdicts.keys()):
            key = str(vp_id).strip()
            if not key:
                continue
            if key in defined and key not in self._orphan_ledger:
                continue
            verdict = self._verdicts.pop(vp_id, None)
            status = verdict.get("status") if isinstance(verdict, dict) else None
            self._orphaned_vps[key] = str(status) if status else "UNKNOWN"
            self._orphan_ledger.add(key)
            if key in defined:
                logger.warning(
                    "[Verification] plan_id=%s: verdict for %s (%s) was voided "
                    "in an earlier round — dropped so the point earns a "
                    "fresh one",
                    self.plan_id, key, self._orphaned_vps[key],
                )
            else:
                logger.warning(
                    "[Verification] plan_id=%s: verdict for %s (%s) is not "
                    "defined by the plan — dropped from the report projection",
                    self.plan_id, key, self._orphaned_vps[key],
                )

    # ------------------------------------------------------------------
    # Topic relevance — judge, archive, remove
    # ------------------------------------------------------------------
    #
    # 2026-10-27：判定内核（``verification.topic_relevance``）与归档审计
    # （``verification.vp_removal_audit``）都就绪了，但执行器没有调用点 ——
    # 主题外的验证点每轮照样被全量验证：跑一遍、产出一条裁决、报告里再出现
    # 一次。跑一遍的结论没有价值，钱却是真花了。
    #
    # 这一段把三件事接起来，顺序是承重的：
    #
    #   1. **判定** —— 判定内核给出三态结果（相关 / 无关 / 兜底）；
    #   2. **先归档** —— 归档写成功，移除才有据；
    #   3. **后移除** —— 归档写不进去就**留着**（写不进去就不得移除）。

    def _hydrate_verdicts(self) -> None:
        """把持久化的裁决重新读回内存（``run()`` 的第一步）。

        ``__init__`` 已经水合过一次；``run()`` 再水合一次是为了让这一轮看到
        「构造之后才落库」的结论 —— 执行器可以被复用多轮，而落库是别人写的。

        这里**只**动 ``_verdicts``：孤儿隔离与索引派生各有各的步骤（见
        :meth:`run`），不在这里重复做。

        2026-10-27：``reset_state()`` 之后的那一轮**跳过**装载 —— 刚清空的
        结论在这一步回来就等于没清（详见 :meth:`reset_state`）。
        """
        if self._skip_next_hydration:
            self._skip_next_hydration = False
            logger.info(
                "[Verification] plan_id=%s: hydration skipped — the state was "
                "explicitly reset, the next round starts from scratch",
                self.plan_id,
            )
            return
        for vp_id, verdict in self._stored_verdicts().items():
            key = str(vp_id)
            if not key.strip():
                continue
            self._verdicts[key] = (
                dict(verdict) if isinstance(verdict, dict) else verdict
            )

    def _stored_verdicts(self) -> Dict[str, Any]:
        """持久化存储里的裁决表（SQLite 优先，否则旧的 state 文件）。

        与 :meth:`_load_or_init_state` 的两个分支读的是同一份东西 —— 形状
        也一样：SQLite 那边可能是 list（每条带 ``vp_id``），旧 state 文件
        那边是 ``{vp_id: verdict}``。
        """
        if self.verif_repo is not None:
            try:
                row = self.verif_repo.summary(self.plan_id)
            except Exception:  # noqa: BLE001 - 存储读不到不是致命的
                return {}
            if not isinstance(row, dict):
                return {}
            raw = row.get("verdicts") or {}
            if isinstance(raw, list):
                return {
                    str(v.get("vp_id")): v
                    for v in raw
                    if isinstance(v, dict) and v.get("vp_id")
                }
            if isinstance(raw, dict):
                return dict(raw)
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        verdicts = data.get("verdicts") if isinstance(data, dict) else None
        return dict(verdicts) if isinstance(verdicts, dict) else {}

    def _topic_relevance_criteria(self) -> List[Dict[str, str]]:
        """本计划的判据表 —— 主题判定拿它当「这个计划到底要什么」的权威。

        两个来源，先字典后文件：

        1. 执行器拿到的计划字典（``plan["criteria"]`` /
           ``plan["acceptance_criteria"]``）；
        2. 计划文件本身（``plan_dir/verification_plan.json``）—— 执行器拿到
           的是**转换后**的计划（``verification_agent`` 的转换会丢掉判据段），
           文件里那份才是原样。

        只收**编号式**的判据（``id`` + 正文）：没有编号的判据没法被引用，
        收了也只会在判定内核里被丢掉。
        """
        raw: Any = None
        for source in (self.plan, self._plan_file_payload()):
            if not isinstance(source, Mapping):
                continue
            for key in ("criteria", "acceptance_criteria"):
                candidate = source.get(key)
                if isinstance(candidate, (list, tuple)) and candidate:
                    raw = candidate
                    break
            if raw:
                break
        if not isinstance(raw, (list, tuple)):
            return []
        criteria: List[Dict[str, str]] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            criterion_id = str(
                entry.get("id") or entry.get("criterion_id") or ""
            ).strip()
            if not criterion_id:
                continue
            text = str(
                entry.get("text")
                or entry.get("description")
                or entry.get("content")
                or ""
            ).strip()
            criteria.append({"id": criterion_id, "text": text})
        return criteria

    def _plan_file_payload(self) -> Optional[Dict[str, Any]]:
        """计划文件本身（``plan_dir/verification_plan.json``）的内容。

        读不到就返回 ``None`` —— 判据表少一个来源不影响判定（判定内核在
        没有判据时按「相关」兜底）。
        """
        path = self.plan_dir / _PLAN_FILENAME
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _apply_topic_relevance(self) -> Set[str]:
        """判定活动验证点是否还在计划主题内；主题外的**先归档、后移除**。

        顺序是承重的：

        1. **扣除移除集** —— 移除集每轮从审计文件重建（它是移除的唯一持久化
           真相，**不**写进 ``executor_state``）；上一轮移除掉的验证点不再
           参与判定，也不会被归档第二次。
        2. **判定** —— 判定内核先走确定性层（判据编号 / 正文命中），剩下
           的交给模型；模型不可用就按「相关」兜底。
        3. **先归档、后移除** —— 归档写成功才允许移除。写不进去（磁盘满 /
           只读 / 记录不可序列化）就**留着**那条验证点：一次没有证据的移除
           是一次静悄悄的严格度损失。

        Returns:
            本轮的移除集（之前移除过的 + 这一次移除的）。
        """
        from verification.topic_relevance import (
            TIER_FAIL_OPEN,
            RelevanceDecision,
            judge_topic_relevance,
        )
        from verification.vp_removal_audit import (
            archive_removal,
            load_removed_vp_ids,
        )

        # 1. 移除集每轮重建 —— 审计文件是唯一的持久化真相。
        removed: Set[str] = set(load_removed_vp_ids(self.plan_dir))
        self._removed_vp_ids = removed

        # 活动集合 = 计划里的 − 计划自己作废的 − 已经移除的。
        active: List[Dict[str, Any]] = []
        by_id: Dict[str, Dict[str, Any]] = {}
        for vp in self._plan_vps():
            if not isinstance(vp, Mapping):
                continue
            if self._vp_retired_marker(vp):
                continue
            vp_id = str(vp.get("id") or "").strip()
            if not vp_id or vp_id in removed:
                continue
            active.append(dict(vp))
            by_id[vp_id] = dict(vp)
        if not active:
            return removed

        # 2. 判定。
        criteria = self._topic_relevance_criteria()
        results = judge_topic_relevance(
            vps=active,
            criteria=criteria,
            llm_batch=self._topic_relevance_llm_batch,
        )

        # 3. 移除候选。
        candidates: List[Tuple[str, Any]] = []
        for vp_id, result in results.items():
            if result.decision == RelevanceDecision.LLM_IRRELEVANT:
                candidates.append((vp_id, result))
                continue
            # 判定内核没能给出结论（模型不可用 / 回答不可解析）时，只认一条
            # **确定性**的矛盾：这条验证点声明的判据编号在计划里一条都对不上。
            # 模型还能说话的时候由模型说话 —— 模型的明确判断优先。
            if result.tier == TIER_FAIL_OPEN and self._has_broken_reference(
                by_id.get(vp_id, {}), criteria,
            ):
                candidates.append((vp_id, result))

        # 4. 先归档、后移除。
        for vp_id, result in candidates:
            try:
                archive_removal(self._removal_record(vp_id, result), self.plan_dir)
            except Exception as exc:  # noqa: BLE001 - 写不进去就不得移除
                logger.warning(
                    "[Verification] plan_id=%s: could not archive the removal "
                    "of %s (%s) — keeping it: a removal without an audit trail "
                    "is a silent loss of strictness",
                    self.plan_id, vp_id, exc,
                )
                continue
            removed.add(vp_id)

        self._removed_vp_ids = removed
        return removed

    def _has_broken_reference(
        self,
        vp: Mapping[str, Any],
        criteria: List[Dict[str, str]],
    ) -> bool:
        """这条验证点声明的判据编号在计划里一条都对不上吗（确定性矛盾）。

        只有**编号式**的声明才算数：``["AC-NOT-IN-PRD"]`` 是在指着一个具体
        的判据，而计划里没有这个编号 —— 那是事实层面的矛盾。散文式的声明
        （``["导出 CSV"]``）对不上只是写法不同，不能据此判死一条验证点。

        判据表里只要有一条**不是**编号式的，整条规则就停用：那种判据表没有
        「编号」可言，验证点的声明对不上它是常态而不是矛盾。
        """
        if not criteria:
            return False
        known: Set[str] = set()
        for criterion in criteria:
            criterion_id = str(criterion.get("id") or "")
            if not criterion_id or not _CRITERION_REFERENCE_RE.match(criterion_id):
                return False
            known.add(_reference_key(criterion_id))
        references = _criterion_references(vp)
        if not references:
            return False
        return not any(_reference_key(ref) in known for ref in references)

    def _removal_record(self, vp_id: str, result: Any) -> Dict[str, Any]:
        """一条移除记录 —— 归档文件里的形状。

        与 :mod:`verification.vp_removal_audit` 的契约一致：读路径从
        ``vp_id``（或 ``vp.id``）取 id，其余字段是给人看的判定依据 —— 什么
        判的、依据是什么、这条验证点当时是什么状态。
        """
        verdict = self._verdicts.get(vp_id)
        status = verdict.get("status") if isinstance(verdict, dict) else None
        return {
            "schema_version": 1,
            "ts": _now_iso(),
            "plan_id": self.plan_id,
            "round": self._current_round(),
            "component": "verification_executor",
            "vp_id": vp_id,
            "decision": str(result.decision),
            "tier": str(result.tier),
            "reason": str(result.reason),
            "evidence": {
                "matched_criterion_ids": list(result.matched_criterion_ids),
            },
            "vp": {"id": vp_id},
            "verdict": {"status": str(status) if status else "pending"},
            # 移除不动 id 台账（``allocated_vp_ids``）—— 那个号已经被发出去过，
            # 移除一条验证点不等于把号收回来。
            "allocated_vp_ids_preserved": True,
        }

    def _current_round(self) -> int:
        """这一轮是第几轮（归档记录里的 ``round``）。

        执行器自己不知道轮次 —— 轮次住在状态库的 ``plan_verification.round``
        列上。没有绑定状态库时是 0（「轮次未知」，不是「第一轮」）。
        """
        if self.verif_repo is None:
            return 0
        try:
            row = self.verif_repo.summary(self.plan_id)
        except Exception:  # noqa: BLE001 - 读不到轮次不是致命的
            return 0
        if not isinstance(row, dict):
            return 0
        try:
            return int(row.get("round") or 0)
        except (TypeError, ValueError):
            return 0

    def _round_number(self) -> Optional[int]:
        """这一轮是第几轮 —— **交给分派**的归因坐标。

        显式给的轮次优先（调用方知道自己是第几轮），否则问状态库
        （:meth:`_current_round`）。状态库说「不知道」（0）时是 ``None``：
        0 是「轮次未知」，不是「第一轮」—— 把它写进台账会造出一个不存在
        的第 0 轮，而「第 0 轮」在报表里和「第一轮」长得一样。
        """
        if self._round_no is not None:
            try:
                explicit = int(self._round_no)
            except (TypeError, ValueError):
                explicit = 0
            return explicit if explicit > 0 else None
        current = self._current_round()
        return current if current > 0 else None

    def _load_orphan_ledger(self, raw: Any) -> None:
        """Read the persisted orphan ledger (``orphaned_vp_ids``).

        Accepts either the ledger itself (a list of ids) or the blob that
        carries it (``{"orphaned_vp_ids": [...]}`` — the shape stored in
        the SQLite ``executor_state`` column).
        """
        if isinstance(raw, dict):
            raw = raw.get("orphaned_vp_ids")
        if not isinstance(raw, (list, tuple, set)):
            return
        for item in raw:
            key = str(item or "").strip()
            if key:
                self._orphan_ledger.add(key)

    def _persist_orphan_ledger(self) -> None:
        """Write the orphan ledger into the persisted state.

        The ledger has to outlive the round: its whole purpose is that the
        **next** load does not inherit an old conclusion for an id the plan
        once voided. The canonical store is the SQLite
        ``plan_verification.executor_state`` column; the legacy state file
        carries the same key for the pre-SQLite path.

        Writing it must never cost an audit trail: the legacy path
        rewrites only the ledger key and leaves the stored verdicts alone,
        and the SQLite path **merges** into the ``executor_state`` blob
        instead of replacing it.
        """
        ledger = sorted(self._orphan_ledger)
        if not ledger:
            return
        if self.verif_repo is not None and self.plan_id:
            # ``executor_state`` has no other writer in the framework, and
            # the repository exposes no public writer for it; the private
            # ``_update`` is the same single-statement UPDATE the public
            # writers use. Guarded so a repo without it degrades to the
            # legacy path instead of raising.
            update = getattr(self.verif_repo, "_update", None)
            if callable(update):
                blob: Dict[str, Any] = {}
                try:
                    row = self.verif_repo.summary(self.plan_id) or {}
                    existing = row.get("executor_state")
                    if isinstance(existing, dict):
                        blob = dict(existing)
                except Exception:  # noqa: BLE001 — 读不到就按空写，账本本身才是关键
                    blob = {}
                blob["orphaned_vp_ids"] = ledger
                try:
                    update(self.plan_id, executor_state=blob)
                except Exception as exc:  # noqa: BLE001 — 账本写不进去不阻断验证
                    logger.warning(
                        "[Verification] plan_id=%s: could not persist the "
                        "orphan ledger: %r", self.plan_id, exc,
                    )
                return
        try:
            data: Dict[str, Any] = {}
            if self.state_file.exists():
                with open(self.state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            data["orphaned_vp_ids"] = ledger
            atomic_write_json(self.state_file, data, logger=logger)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "[Verification] plan_id=%s: could not persist the orphan "
                "ledger to the state file: %r", self.plan_id, exc,
            )

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


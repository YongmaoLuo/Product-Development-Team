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
import json
import logging

logger = logging.getLogger(__name__)
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from coding_tool import ClaudeCodingTool, CodingTool, ApiError, HardTimeoutError
from config_paths import resolve_plans_dir
import provider_capacity
from verification_config import TimeoutPolicy
from verification_executor import VerificationExecutor
from verification_persistence import VerificationPersistenceManager
from verification_profile import ExecutionProfileGenerator
from verification_split import SplitDecision
from verification_subagent import VerificationSubAgent, Verdict
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


# Boundary: the parent ``VerificationAgent`` does NOT directly import
# the ``subprocess`` module or invoke the non-JSON coding tool
# surface. Per-VP work is delegated to :class:`VerificationSubAgent.run`,
# which builds a temp settings.json with the 8-hook security net and
# drives the LLM inside its own self-heal loop. The parent only
# speaks the JSON-coding-tool surface for plan/report generation.


def _plan_wide_vp_cap() -> Optional[int]:
    """Plan-wide ceiling on concurrently-running VPs, or ``None``.

    An ADDITIONAL bound layered over the existing per-group
    ``parallelism_cap``, not a replacement for it.

    Why it is needed at all: ``_execute_group`` applies
    ``parallelism_cap`` per group, and ``execute_verification_plan_async``
    gathers every group at once, so the plan-wide fan-out is
    ``parallelism_cap × group_count``. Four method-groups at cap 4 is 16,
    and a fifth and sixth group would make it 24 — nothing in the
    per-group throttle bounds that, and each in-flight VP is a ``claude``
    subprocess competing for the same provider slots.

    The bound is **derived from configuration**, not asserted here: it is
    the sum of the per-provider caps declared in
    ``provider_capacity.yaml`` (see
    :func:`provider_capacity.configured_capacity_ceiling`), which is the
    most this installation can usefully have in flight. Every VP beyond
    that would only queue on a provider slot.

    ``None`` when no capacity is configured. Then there is no declared
    fleet size to derive a bound from, and inventing one would put the
    same kind of hand-written number back — the operator's own
    ``parallelism_cap`` is the only throttle that exists, and it applies.

    Keeping the per-group cap means an operator who sets
    ``parallelism_cap`` still gets exactly the throttle they asked for
    (that behaviour is pinned by
    ``test_orchestrator_respects_parallelism_cap_2``).
    """
    ceiling = provider_capacity.configured_capacity_ceiling()
    return ceiling if ceiling > 0 else None


class _NoPlanBound:
    """Reentrant no-op stand-in for the plan-wide semaphore.

    A class rather than ``@contextlib.asynccontextmanager`` because ONE
    instance is shared by every VP in the group, and a generator-based
    context manager can only be entered once — the second ``async with``
    would raise, or (worse) silently serialise the group.
    """

    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc_info) -> bool:
        return False


_NO_PLAN_BOUND = _NoPlanBound()


def _plan_wide_bound():
    """The plan-wide bound as an async context manager.

    A semaphore when :func:`_plan_wide_vp_cap` found a configured
    ceiling, a shared no-op otherwise. Handing both cases back in the
    same shape keeps the two call sites readable — neither has to branch
    on whether capacity was configured.
    """
    ceiling = _plan_wide_vp_cap()
    if ceiling is None:
        return _NO_PLAN_BOUND
    return asyncio.Semaphore(ceiling)


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


#
# Refactor note (Phase C7 → Orchestrator): the historical class name
# ``VerificationAgent`` is kept as a backward-compatibility alias of the
# canonical ``Orchestrator`` class. The 6 phase classes live under
# ``backend/verification/{interview,prd,arch,test,tasks,verification}.py``
# and ``Orchestrator`` is the thin wiring layer that delegates to them.
# Downstream callers that already imported ``VerificationAgent`` keep
# working unchanged because ``VerificationAgent = Orchestrator`` at
# the bottom of this module resolves the symbol for them.

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
                 max_parallel: int = 1,
                 verif_repo: Optional[Any] = None):
        """
        Initialize verification agent.

        Args:
            plan_dir: Plan directory containing PRD/arch/test documents
            project_dir: Project directory to verify
            coding_tool: Optional coding tool (defaults to ClaudeCodingTool)
            max_parallel: VP-level concurrency for VerificationExecutor
            verif_repo: Optional VerificationRepository handle. When
                provided, the per-VP verdict callback routes through
                ``verif_repo.append_verdict`` (single BEGIN IMMEDIATE +
                COMMIT) and produces zero ``verification_*_state.json``
                files.  ``None`` keeps the legacy JSON persistence path.
        """
        self.plan_dir = Path(plan_dir)
        self.project_dir = Path(project_dir)
        self.coding_tool = coding_tool or ClaudeCodingTool()
        self.max_parallel = max_parallel
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
        # freshness check. Cleared at the start of each round by
        # ``run_two_phase_loop`` so stale cache from a prior round
        # never bleeds across rebuilds.
        self._freshness_cache = FreshnessCache()

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
        #: 阶段归一化摘要（Phase 1/2 计数 + 被兜底提升的 VP），由
        #: :meth:`generate_verification_plan` 填充，供审计/展示读取。
        self._last_phase_summary: Dict[str, Any] = {}

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

    def _maybe_evaluate_plan_delta(self, plan_data: dict) -> Dict[str, Any]:
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
        """
        try:
            from verification_plan_delta import (
                DELTA_SYSTEM_PROMPT,
                apply_delta,
                build_delta_prompt,
                collect_acceptance,
                collect_new_tasks,
                load_delta_state,
                parse_delta_payload,
                save_delta_state,
            )

            state = load_delta_state(self.plan_dir)
            seen = state.get("seen_task_ids")
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
                })
                return {}

            if not new_tasks:
                save_delta_state(self.plan_dir, {
                    "seen_task_ids": all_ids,
                    "round": round_number,
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
            delta = parse_delta_payload(raw, plan_data)
            summary = apply_delta(plan_data, delta, round_number)

            # 新增的 VP 必须和初次生成时一样过护栏。用户 2026-09-18：
            # "考虑到会生成新的 VP，所以每一轮跑之前都应该去跑护栏"。
            #
            # 护栏的内容随方法收敛而变：VP 不再有 test_command，所以查的是
            # **method 的契约** —— 不支持的 method，以及 api_test 的
            # request/assertions 是否合规。违规的**整条丢弃**：宁可少一条
            # VP，也不要一条永远跑不出结论的验收点。
            if summary["added"]:
                added_ids = [a["id"] for a in summary["added"]]
                by_id = {
                    str(vp.get("id")): vp
                    for vp in plan_data.get("verification_points") or []
                    if isinstance(vp, dict)
                }
                candidates = {
                    "verification_points": [by_id[i] for i in added_ids if i in by_id],
                }
                bad_ids = {
                    f["id"] for f in self._api_schema_report(candidates)
                }
                from verification_subagent import SUPPORTED_METHODS
                for vp in candidates["verification_points"]:
                    if str(vp.get("verification_method") or "") not in SUPPORTED_METHODS:
                        bad_ids.add(str(vp.get("id")))
                if bad_ids:
                    plan_data["verification_points"] = [
                        vp for vp in plan_data.get("verification_points") or []
                        if str(vp.get("id")) not in bad_ids
                    ]
                    summary["added"] = [
                        a for a in summary["added"] if a["id"] not in bad_ids
                    ]
                    summary["rejected_by_guard"] = sorted(bad_ids)

                # 2026-09-19: 新增条目也要过阶段归一化。主生成路径本来就会
                # 跑它 —— 那条"声明驱动"的规则是"Phase 1 上写了
                # e2e / full_ci 就说明它自己声明了'我是全量关卡'，就地提升
                # 到 Phase 2"。增量路径漏了这一步，于是一条整仓门禁型的新增
                # 会被按 Phase 1 排进去，在每条子功能都还没验完时就开跑，
                # 把整轮堵死（那一轮的「Nightly CI 全过」就是这个下场）。
                try:
                    from verification_phases import (
                        normalize_plan_phases as _normalize_phases,
                    )
                    _phase_summary = _normalize_phases(plan_data)
                    if _phase_summary.get("phase_promoted"):
                        summary["phase_promoted"] = _phase_summary["phase_promoted"]
                except Exception:
                    logger.warning(
                        "[plan_delta] phase normalization failed; "
                        "added VPs keep their declared phase",
                        exc_info=True,
                    )

            save_delta_state(self.plan_dir, {
                "seen_task_ids": all_ids,
                "round": round_number,
                "last_delta": summary,
            })
            self._safe_log_freshness_event(
                "__plan_delta__", "plan_delta_evaluated", summary,
            )
            if summary["added"] or summary["obsoleted"]:
                print(
                    "[Verification] 计划增量评估："
                    f"新增 {len(summary['added'])} 条、"
                    f"废弃 {len(summary['obsoleted'])} 条"
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

    # -----------------------------------------------------------------------
    # Phase 1: Planning
    # -----------------------------------------------------------------------

    def generate_verification_plan(self, retry_llm: int = 3) -> dict:
        """
        Phase 1: Generate verification plan from PRD/arch/test documents.

        Args:
            retry_llm: Number of retries for LLM calls

        Returns:
            Verification plan dict
        """
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
                with open(self.verification_plan_file, "r", encoding="utf-8") as f:
                    disk_plan = json.load(f)
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
                    self._maybe_evaluate_plan_delta(disk_plan)
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
        profile = ExecutionProfileGenerator(plan_data, timeout_policy=self.timeout_policy).build()
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
        each group concurrently under a ``parallelism_cap`` semaphore.

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

        Pipeline:

        1. Load the plan (or use the one passed in).
        2. :meth:`_partition_vps_by_method` buckets VPs by their
           ``verification_method`` field, preserving first-seen order.
        3. ``manual_check`` groups short-circuit to ``SKIPPED`` and
           never enter the gather / semaphore path.
        4. Each remaining group is dispatched via
           :func:`asyncio.gather` so multiple groups run in parallel,
           and inside each group an :class:`asyncio.Semaphore`
           (``TimeoutPolicy.parallelism_cap``) rate-limits the
           in-flight VPs to the configured cap.
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
            # 2026-09-17 — ONE plan-wide semaphore, shared by every
            # group, layered over each group's own ``parallelism_cap``
            # semaphore.
            #
            # The per-group cap alone does not bound the plan: it is
            # applied once per group and the groups are gathered
            # together, so the real fan-out is ``parallelism_cap ×
            # group_count``. Four groups at cap 4 gives 16, and a fifth
            # and sixth group would give 24 — each in-flight VP is a
            # ``claude`` subprocess competing for the same provider slots.
            #
            # ``parallelism_cap`` keeps its meaning (see
            # ``_plan_wide_vp_cap``); this only fires above the ceiling
            # the configured per-provider caps add up to.
            plan_semaphore = _plan_wide_bound()
            coros = [
                self._execute_group(method, vps, plan_semaphore)
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
        plan_semaphore: Optional[asyncio.Semaphore] = None,
    ) -> List[Dict[str, Any]]:
        """Run all VPs in a single method-group in parallel.

        Two bounds apply, nested:

        * ``TimeoutPolicy.parallelism_cap`` — this group's own throttle,
          unchanged since it was introduced.
        * ``plan_semaphore`` — the plan-wide fleet ceiling, created once
          in :meth:`execute_verification_plan_async` and shared by every
          group. Without it the per-group caps multiply (cap × groups).

        ``plan_semaphore`` is passed in rather than constructed here,
        but still defaulted: a caller driving a single group directly
        (tests, ad-hoc repros) gets a correctly-sized one instead of an
        unbounded gather. For a single group the two coincide anyway.

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

        if plan_semaphore is None:
            plan_semaphore = _plan_wide_bound()

        # Two bounds, and they mean different things. The group
        # semaphore is the operator's ``parallelism_cap`` throttle and is
        # unchanged. The plan bound is the fleet ceiling: a running VP is
        # mostly a ``claude`` subprocess, so letting the per-group caps
        # multiply past it just parks coroutines in the provider slot
        # wait. It is a no-op when no capacity is configured.
        cap = max(1, int(self.timeout_policy.parallelism_cap))
        semaphore = asyncio.Semaphore(cap)

        async def _run_with_semaphore(vp: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                async with plan_semaphore:
                    return await self._run_single_vp_async(vp)

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
        if runtime_map is None:
            return self._service_declarations
        try:
            from service_manager import render_declaration
        except Exception:  # pragma: no cover - defensive
            return self._service_declarations

        effective: Dict[str, Any] = {}
        for name, decl in self._service_declarations.items():
            runtime = (runtime_map.runtimes or {}).get(name)
            port = getattr(runtime, "port", None)
            if runtime is not None and runtime.ready and port and port != decl.port:
                effective[name] = render_declaration(decl, port)
            else:
                effective[name] = decl
        return effective

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
        """
        method = str(
            vp.get("verification_method") or vp.get("method") or ""
        ).strip()
        if method not in ("code_review", "ui_validation"):
            return verdict

        vp_id = str(vp.get("id", "unknown"))
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

        Never raises — :func:`verification_api_runner.run_api_verification`
        turns every failure mode into a FAILED verdict carrying its
        reason.
        """
        from verification_api_runner import run_api_verification

        vp_id = str(vp.get("id", "unknown"))
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

        verdict = result.to_verdict_dict()
        self._safe_log_freshness_event(
            vp_id, "api_test_executed",
            {
                "status": verdict["status"],
                "status_code": result.evidence.get("status_code"),
                "assertions_failed": result.evidence.get("assertions_failed"),
            },
        )
        return verdict

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

        # Log verification point completion
        self.persistence.write_verification_point_log(
            vp_id,
            "vp_complete",
 {
                "status": result.get("status", "UNKNOWN"),
                "actual_result": str(result.get("actual_result", ""))[:500],
                "evidence": str(result.get("evidence", ""))[:500]
            }
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
        * ``verdict.reasons`` (first entry) → ``actual_result``.
        * ``verdict.evidence`` (joined) → ``evidence``.
        * Provider metadata is dropped at this layer; it is
          surfaced in the persistence log via
          ``sub_agent_completed`` for auditability, not in the
          per-VP result dict.
        """
        status = str(verdict.verdict) if verdict.verdict else "FAILED"
        # ``actual_result`` is a short human-readable summary; the
        # first reason is usually the most informative. If no
        # reasons were provided, fall back to a generic message
        # so the downstream report always has *something* to show.
        if verdict.reasons:
            actual_result = str(verdict.reasons[0])
        else:
            actual_result = f"verdict={status}"
        evidence = " | ".join(str(e) for e in verdict.evidence) if verdict.evidence else ""
        return {
            "id": vp_id,
            "status": status,
            "reasons": list(verdict.reasons) if verdict.reasons else [],
            "evidence": {"details": evidence, "raw": list(verdict.evidence) if verdict.evidence else []},
            "actual_result": actual_result,
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

                # Add metadata
                report_data["generated_at"] = datetime.now().isoformat()
                report_data["plan_id"] = self.plan_dir.name
                report_data["project_dir"] = str(self.project_dir)

                # Compute execution_profile (static budget projection
                # consumed by the bridge UI). The profile mirrors
                # ExecutionProfileGenerator's contract and is derived
                # from the aggregated execution_results so the LLM-
                # emitted report and the profile agree on the same
                # set of verification points.
                report_data["execution_profile"] = ExecutionProfileGenerator(
                    execution_results
                ).build()

                # Save report using persistence manager
                self.persistence.update_report(report_data)

                # DP3 (2): post-judgment supplement spec/code review.
                # Runs AFTER the main PASSED/FAILED verdict is settled
                # and is purely advisory — its findings never modify
                # ``overall_status`` or per-VP statuses. Best-effort:
                # any failure degrades to an empty list so the main
                # verdict is unaffected.
                try:
                    report_data["supplement_findings"] = (
                        self._supplement_spec_code_review(execution_results)
                    )
                except Exception as sup_exc:  # noqa: BLE001 — best-effort
                    logging.getLogger(__name__).warning(
                        "supplement_review_error: %s", sup_exc,
                    )
                    report_data["supplement_findings"] = []
                # Re-persist with the supplement attached so on-disk
                # report consumers (bridge UI, downstream tooling) see
                # the same shape as the in-memory return value.
                try:
                    self.persistence.update_report(report_data)
                except Exception as persist_exc:  # noqa: BLE001
                    logging.getLogger(__name__).warning(
                        "supplement_persist_error: %s", persist_exc,
                    )

                print(f"[Verification] Generated report: {report_data['overall_status']}")
                return report_data

            except ApiError as e:
                print(f"[Verification] LLM API error (attempt {attempt + 1}/{retry_llm}): {e}")
                if attempt == retry_llm - 1:
                    raise
            except Exception as e:
                print(f"[Verification] Error generating report (attempt {attempt + 1}/{retry_llm}): {e}")
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
            parts.append(f"- 判定依据: {str(evidence)[:500]}")
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

    def _supplement_spec_code_review(self, execution_results: dict) -> list:
        """DP3 (2): run a per-VP adversarial spec/code review at judgment time.

        For every verification point, re-prompts the LLM with
        :data:`framework.prompts.INLINE_SPEC_CODE_REVIEW_PROMPT` and parses the
        same ``{spec_compliance, code_quality, should_block, reason}``
        envelope the agent-level pre-commit review uses. The findings
        are appended to the verification report as
        ``supplement_findings``; the main ``overall_status`` is NEVER
        touched — this is an advisory layer only.

        Output shape::

            [
              {
                "vp_id":          "VP-001",
                "spec_findings":  [{"severity": "low", "reason": "..."}],
                "quality_findings":[{"severity": "low", "reason": "..."}],
              },
              ...
            ]

        Boundary conditions:

          * ``verification_points`` is empty -> return ``[]`` without
            calling the LLM at all. The bridge UI relies on this so
            empty plans never burn tokens on a no-op review.
          * Any single VP call fails (network blip, ApiError, JSON
            parse error, empty response) -> that VP is silently
            skipped; the rest are still processed. If every VP
            fails, the returned list is empty.
          * This method itself never raises; callers can rely on the
            contract "always returns a list" for safe post-merge
            decoration of ``report_data``.
        """
        # Import the prompt template directly from the framework leaf
        # module. The historical lazy ``from agent import ...`` cycle
        # was broken by moving the constant to ``framework/prompts.py``;
        # neither ``verification_agent`` nor ``agent`` need to import
        # the other for this template.
        from framework.prompts import INLINE_SPEC_CODE_REVIEW_PROMPT

        import re as _re

        verification_points = execution_results.get("verification_points", []) or []
        if not verification_points:
            return []

        def _degraded(reason: str) -> dict:
            return {
                "spec_compliance": "low",
                "code_quality": "low",
                "should_block": False,
                "reason": reason,
            }

        findings: list = []
        for vp in verification_points:
            vp_id = vp.get("id", "unknown")
            task_desc = (
                vp.get("related_prd_criteria")
                or vp.get("title")
                or vp.get("expected_result")
                or ""
            )
            git_diff_stat = "(verification judgment phase — no per-VP git diff available)"

            # 2026-09-14: per-VP supplement reviews are sequential LLM
            # calls — heartbeat so the watchdog keeps seeing a live round
            # instead of a silent plan directory.
            #
            # NO explicit ``timeout``. This call used to pass
            # ``timeout=60``, which REPLACES the unified 900s silence
            # window with 60s — and a healthy provider answering slowly
            # then looks like a dead one: every VP that overran the 60s
            # was aborted, the abort burned the failover budget across
            # all three providers, and the failure surfaced as a provider
            # fault while CC Switch logged a 100% success rate. The
            # requests we were aborting were our own. Inheriting the
            # unified rules is both safer and faster in aggregate.
            self._judgment_heartbeat("supplement_review", vp_id=vp_id)

            prompt = INLINE_SPEC_CODE_REVIEW_PROMPT.format(
                task_desc=task_desc,
                git_diff_stat=git_diff_stat,
            )

            # -- LLM call (best-effort) -----------------------------
            try:
                response = self.coding_tool.query(
                    prompt=prompt,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                logging.getLogger(__name__).warning(
                    "supplement_review_llm_error vp=%s err=%s",
                    vp_id, exc,
                )
                continue

            if not isinstance(response, str) or not response.strip():
                continue

            # -- Parse JSON (mirrors ``_inline_spec_code_review``) ---
            text = response.strip()
            text = _re.sub(r"^```(?:json)?\s*", "", text)
            text = _re.sub(r"\s*```$", "", text)
            match = _re.search(r"\{.*\}", text, _re.DOTALL)
            candidate = match.group(0) if match else text

            try:
                verdict = json.loads(candidate)
            except (ValueError, TypeError):
                continue

            if not isinstance(verdict, dict):
                continue

            spec = str(verdict.get("spec_compliance", "low")).lower()
            if spec not in ("high", "medium", "low"):
                spec = "low"
            quality = str(verdict.get("code_quality", "low")).lower()
            if quality not in ("high", "medium", "low"):
                quality = "low"
            reason = str(verdict.get("reason", "") or "")[:500]

            findings.append({
                "vp_id": vp_id,
                "spec_findings": [{"severity": spec, "reason": reason}],
                "quality_findings": [{"severity": quality, "reason": reason}],
            })

        return findings

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
            "execution_profile": ExecutionProfileGenerator(execution_results).build(),
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

        # 2026-09-18 C2：服务已经由 preflight 起好并登记，这里把 VP 命令里的
        # ``{{svc.<name>.<field>}}`` 展开成真实地址再交给执行器。替换只在
        # 内存里做——磁盘上的计划保留占位符，每轮重新解析，避免换端口后
        # 留下一份写死端口的旧计划。
        plan_data = self._resolve_service_placeholders(plan_data)

        # Step 2: delegate Phase 2 to VerificationExecutor. The
        # parent only sequences the call — per-VP work, layer
        # ordering, and short-circuit all live on the executor.
        try:
            executor = self._build_verification_executor(plan_data)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(executor.run(max_parallel=self.max_parallel))
            else:
                # Loop is running (e.g. pytest-asyncio test).
                import nest_asyncio  # type: ignore
                nest_asyncio.apply()
                asyncio.run(executor.run(max_parallel=self.max_parallel))
            execution_results = self._build_execution_results_from_executor(
                plan_data, executor
            )
        except Exception as exc:
            execution_results = self._build_failure_execution_results(
                plan_data, exc
            )

        # Step 3: Phase 3 LLM.
        report = self.generate_verification_report(execution_results)

        print(
            f"[Verification] Workflow completed: {report['overall_status']}"
        )
        return report

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

        async def _sub_agent_runner(vp_node: dict) -> dict:
            vp = dict(vp_node)
            vp_id = str(vp.get("id", "unknown"))
            # Binary freshness pre-check (runs before every VP).
            # Audit-logged via ``write_verification_point_log`` so the
            # operator can audit stale-binary detection per VP from
            # ``plans/<id>/logs/verification_*_*.log``. The check is
            # wrapped in try/except so an unexpected error in the
            # freshness path never blocks VP execution; the operator
            # sees the warning in stderr instead.
            try:
                freshness = check_binary_freshness(
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
                    from binary_rebuild_agent import attempt_intelligent_rebuild
                    from sub_agent_registry import sub_agent_registry
                    rebuild_result = attempt_intelligent_rebuild(
                        self.project_dir,
                        freshness,
                        coding_tool=self.coding_tool,
                        plan_id=self.plan_dir.name,
                        vp_id=vp_id,
                        registry=sub_agent_registry,
                    )
                    if rebuild_result.success:
                        freshness = check_binary_freshness(
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
                return {
                    "status": "BLOCKED",
                    "reasons": [
                        f"verification_method {retired_method!r} is not one "
                        f"of {list(SUPPORTED_METHODS)}"
                    ],
                    "actual_result": (
                        f"该 VP 的 verification_method（{retired_method}）已退役"
                        f"或不存在，无法执行；请把它重新表达为 api_test / "
                        f"ui_validation / code_review。"
                    ),
                    "evidence": {"method": retired_method},
                    "framework_checks": [],
                }
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

        return VerificationExecutor(
            verification_plan=executor_plan,
            plan_id=self.plan_dir.name,
            plan_dir=self.plan_dir,
            sub_agent_runner=_sub_agent_runner,
            verif_repo=getattr(self, "verif_repo", None),
        )

    # ------------------------------------------------------------------
    # Binary freshness — pre-flight check (per-VP) + report mutation
    # ------------------------------------------------------------------
    #
    # Two related entry points on this class:
    #
    # 1. ``_binary_freshness_pre_check`` — runs synchronously in
    #    ``_sub_agent_runner`` (above) BEFORE every VP. Returns the
    #    ``FreshnessReport`` so the caller can decide to BLOCK on
    #    a stale result.
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
            "reasons": [f"binary stale: {freshness.detail}"],
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
        self._service_runtime_map = None
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

        A stale build artefact is rebuilt first — relaunching a service
        against yesterday's binary would just serve old code again,
        which is the ordering the legacy path already had.
        """
        from service_manager import ensure_services, record_runtimes

        self._maybe_rebuild_before_service_start()
        runtime_map = ensure_services(
            self.project_dir,
            self._service_declarations,
            round_number=self._current_round,
        )
        self._service_runtime_map = runtime_map
        self._safe_log_freshness_event(
            "__preflight__", "services_ensured", runtime_map.to_dict(),
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
        return runtime_map

    def _maybe_rebuild_before_service_start(self) -> None:
        """Rebuild a stale build artefact before (re)starting services.

        Shared by both preflight paths. Never raises — a rebuild failure
        is logged and the caller proceeds; the per-VP freshness check
        will retry it with the full rebuild agent.
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
        rebuild_result = attempt_intelligent_rebuild(
            self.project_dir,
            freshness,
            coding_tool=self.coding_tool,
            plan_id=self.plan_dir.name,
            vp_id="__preflight__",
            registry=sub_agent_registry,
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
        return {
            "status": "BLOCKED",
            "reasons": [f"service not ready on port(s) {ports}"],
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
        verification_points = plan_data.get("verification_points", [])

        executor_verdicts = executor.collect_verdicts()

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
            results.append(
                {
                    "id": vp_id,
                    "status": status,
                    "actual_result": actual_result,
                    "evidence": evidence,
                }
            )

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
        execution_results = {
            "verification_points": verification_points,
            "execution_results": [
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
                    "evidence": json.dumps(
                        {
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                }
                for vp in verification_points
            ],
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

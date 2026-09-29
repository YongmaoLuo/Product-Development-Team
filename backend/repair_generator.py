"""
Repair Task Generator — Requirement-Driven Repair Task Generation
================================================================

Generates repair tasks based on verification failures using a three-step
requirement-driven process:
1. Requirement Understanding — Extract acceptance criteria, constraints, verification strategies
2. Evidence Collection — Map technical failures to requirement deviations
3. Task Generation — Generate repair tasks referencing original PRD criteria

Single-call content-only flow (2026-09-07)
------------------------------------------
:meth:`RepairTaskGenerator.generate_repair_contents` and
:class:`RepairTaskAssembler` together replace the 3-step LLM chain
with a cleaner LLM-only-for-content / local-code-for-tags split:

  * ``generate_repair_contents(failed_vps, round_number)`` — single
    LLM call that returns one content dict per failed VP
    (``title``, ``description``, ``acceptance_criteria``,
    ``failed_vp_id``). The LLM never sees a task_id / priority /
    execution_group / depends_on field; those are stamped by local
    code in :class:`RepairTaskAssembler`. This separation makes the
    state machine able to rely on locally-stamped task IDs even when
    the LLM misbehaves.

The legacy 3-step chain (``understand_requirements``,
``collect_failure_evidence``, ``_generate_repair_tasks``,
``generate_verification_tasks``) is preserved unchanged because it
is exercised by ``tests/test_blocked_verdict_handling.py``,
``tests/test_repair_generator_deterministic.py``, and
``tests/test_verification_orchestrator.py``. Those tests would need
to be updated as a separate, dedicated refactor — out of scope for
the 2026-09-07 fix that closes the stuck-verification-loop bug.
"""

import json
import re
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from coding_tool import CodingTool
from config_paths import resolve_plans_dir
from test_command_quality import check_falsifiable, inspect_command
from verification_persistence import VerificationPersistenceManager


REPAIR_SYSTEM_PROMPT = """你是一位资深技术架构师。你的任务是基于验证失败证据，生成需求导向的修复任务。

**核心原则：不要针对pytest错误修复，必须回到原始PRD验收标准理解需求偏离**
**重要约束：只生成修复目标项目代码的任务，不要生成修复执行框架、后端基础设施、API路由、状态机等任务。**

修复任务必须：
1. 引用原始PRD验收标准（而非技术错误描述）
2. 说明需求偏离点（如"并发登录响应时间超过PRD要求的2秒"而非"pytest超时"）
3. 提供符合架构约束的修复建议
4. 包含TDD规格（test_xxx: 条件 → 预期）
5. **只修改目标项目代码，不修改执行框架代码**

输出格式（纯 JSON）：
{
  "tasks": [
    {
      "id": "1-1",
      "title": "修复标题（指向需求偏离点）",
      "description": "完整描述，包含：\\n\\n**原始PRD验收标准：**\\n...\\n\\n**架构约束：**\\n...\\n\\n**需求偏离证据：**\\n...\\n\\n**修复建议：**\\n...\\n\\n**TDD规格：**\\n- test_xxx: 条件 → 预期\\n- test_yyy: 条件 → 预期",
      "vp_id": "这条任务对应的失败验证点 id（如 VP-013）",
      "test_command": "复现需求偏离的验证命令。必填且必须是一条**可执行、且修复正确时 exit code 为 0** 的命令。不要写 cd ~/... 这类硬编码 home 路径；不要写只有 ls/grep 的探测链。"
    }
  ]
}

只输出 JSON，不要 markdown 代码块标记。"""

REQUIREMENT_UNDERSTANDING_PROMPT = """你是一位资深需求分析师。请分析提供的PRD、架构设计和测试设计文档，提取以下结构化信息：

1. acceptance_criteria: 从PRD中提取的验收标准列表，每个包含 id, description, category, priority
2. technical_constraints: 从架构设计文档中提取的技术约束列表，每个包含 id, description, category
3. verification_strategies: 从测试设计文档中提取的验证策略列表，每个包含 id, description, method

输出格式（纯JSON）：
{
  "acceptance_criteria": [
    {"id": "AC-001", "description": "...", "category": "performance|security|usability|functionality", "priority": "high|medium|low"}
  ],
  "technical_constraints": [
    {"id": "TC-001", "description": "...", "category": "performance|security|architecture"}
  ],
  "verification_strategies": [
    {"id": "VS-001", "description": "...", "method": "api_test|code_review|ui_validation"}
  ]
}

只输出JSON，不要markdown代码块标记。"""


class RepairGenerationError(RuntimeError):
    """The repair LLM call ran but produced nothing usable.

    2026-09-14 — raised instead of returning an empty list. The two
    outcomes are NOT the same and must not be collapsed:

      * ``[]`` — there were no failed VPs to repair (or the caller
        already holds pending repair tasks). The chain has genuinely
        converged and stopping is correct.
      * ``RepairGenerationError`` — the generator *failed*: the LLM
        call raised (provider outage, hard timeout), or the reply was
        unparseable / contained no usable content while failures were
        pending. Stopping with ``no_repair_tasks`` here reports
        "nothing to fix" for a round that has real failures — on an
        earlier plan that silently terminalised a plan with
        three failed VPs (VP-021 / VP-034 / VP-036) after a bogus
        ``HardTimeoutError``.

    ``check_cycle_conditions`` catches this and surfaces it on the
    result payload as ``repair_generation_error`` so the auto-loop can
    park the plan resumable instead of recording a terminal verdict.
    """

    def __init__(self, message: str, *, round_number: int = 0,
                 failed_vp_ids: Optional[List[str]] = None):
        super().__init__(message)
        self.round_number = round_number
        self.failed_vp_ids = list(failed_vp_ids or [])


class RepairTaskGenerator:
    """Generates requirement-driven repair tasks from verification failures."""

    #: How long the pre-repair falsifiability probe may run before it is
    #: treated as inconclusive. Deliberately short: a *vacuous* command
    #: returns in milliseconds (it only reads a file), so anything still
    #: running after a minute is a real test and the probe has already
    #: got its answer. We never wait for a real suite to finish.
    FALSIFIABILITY_TIMEOUT_S = 60

    def __init__(self, coding_tool: CodingTool, plan_dir: Path, project_dir: Path):
        """
        Initialize repair task generator.

        Args:
            coding_tool: Coding tool for LLM queries
            plan_dir: Plan directory containing PRD/arch/test/verification documents
            project_dir: Project directory to verify
        """
        self.coding_tool = coding_tool
        self.plan_dir = Path(plan_dir)
        self.project_dir = Path(project_dir)

        # ``command -> passes the pre-repair probe``. Repair rounds
        # routinely produce several tasks sharing one command shape; the
        # probe runs a subprocess, so it must not be repeated per task.
        self._falsifiability_cache: Dict[str, bool] = {}

        # File paths
        self.prd_file = self.plan_dir / "prd.json"
        self.prd_md_file = self.plan_dir / "prd.md"
        self.arch_file = self.plan_dir / "arch-design.md"
        self.test_file = self.plan_dir / "test-design.md"
        self.verification_report_file = self.plan_dir / "verification_report.json"

        # Persistence manager for reading verification reports
        self.persistence = VerificationPersistenceManager(self.plan_dir)

    def _get_verification_tasks_file(self, round_number: int) -> Path:
        """Get the path for verification tasks file for a given round."""
        return self.project_dir / f"verification_tasks_round_{round_number}.json"

    @property
    def tasks_file(self) -> Path:
        """Legacy ``project_dir / tasks.json`` mirror of the round file from ``_get_verification_tasks_file``; the round file is the source of truth (written by ``generate_verification_tasks``) and this mirror is written only by ``generate_and_append_tasks`` for TaskManager / older tests."""
        return self.project_dir / "tasks.json"

    def _append_to_tasks_json(self, repair_tasks: List[Dict[str, Any]]) -> None:
        """Append ``repair_tasks`` to ``project_dir / tasks.json``.

        Loads the existing file (if any), preserves its task list, and
        writes the union back. Missing fields are initialised to safe
        defaults. This is the test-facing legacy path used by callers
        that drive the three-step workflow manually and want the
        ``tasks.json`` (TaskManager-readable) side-effect that
        :meth:`generate_and_append_tasks` also produces.

        Args:
            repair_tasks: New repair task dicts to append.
        """
        existing: Dict[str, Any] = {"tasks": []}
        try:
            if self.tasks_file.exists():
                with open(self.tasks_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            # Corrupt or missing file: start from a clean slate rather
            # than propagate the read error.
            existing = {"tasks": []}

        existing_tasks = existing.get("tasks", [])
        if not isinstance(existing_tasks, list):
            existing_tasks = []

        existing_tasks.extend(repair_tasks)
        existing["tasks"] = existing_tasks

        self.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.tasks_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)

    # -----------------------------------------------------------------------
    # Step 1: Requirement Understanding
    # -----------------------------------------------------------------------

    def understand_requirements(self) -> Dict[str, Any]:
        """
        Step 1: Analyze PRD/architecture/test documents to extract requirement context.
        Uses LLM to read documents via file paths and extract structured data.
        Caches result to requirement_context.json to avoid repeated calls.
        On LLM failure, returns empty structure and logs warning.

        Returns:
            Dictionary containing acceptance criteria, technical constraints, and verification strategies
        """
        cache_file = self.plan_dir / "requirement_context.json"

        # Check cache first
        if cache_file.exists():
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass  # Cache corrupt, continue to regenerate

        print("[Repair] Step 1: Understanding requirements (via file paths)...")

        # Build prompt that points to files instead of embedding content
        prompt = self._build_requirement_prompt()

        # 2026-09-15: no explicit ``timeout`` on any LLM
        # call in this module. The previous values (1800 here and below,
        # 600 for repair-content generation) REPLACED the coding tool's
        # unified windows — 900s adaptive silence / 1800s idle pipe / the
        # layer's ceiling — instead of nesting inside them. Omit the kwarg
        # to inherit; see tests/unit/test_llm_calls_inherit_unified_timeout.py.
        try:
            result = self.coding_tool.query_json(
                prompt=prompt,
                system_instruction=REQUIREMENT_UNDERSTANDING_PROMPT,
            )

            requirement_context = {
                "acceptance_criteria": result.get("acceptance_criteria", []),
                "technical_constraints": result.get("technical_constraints", []),
                "verification_strategies": result.get("verification_strategies", [])
            }

            # Save cache to avoid repeated LLM calls
            self.plan_dir.mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(requirement_context, f, indent=2, ensure_ascii=False)

            print(f"[Repair] Extracted {len(requirement_context['acceptance_criteria'])} acceptance criteria, "
                  f"{len(requirement_context['technical_constraints'])} constraints, "
                  f"{len(requirement_context['verification_strategies'])} verification strategies")

            return requirement_context

        except Exception as e:
            print(f"[Repair] Warning: LLM requirement understanding failed: {e}")
            return {
                "acceptance_criteria": [],
                "technical_constraints": [],
                "verification_strategies": []
            }

    def _build_requirement_prompt(self) -> str:
        """Build prompt that instructs LLM to read documents via file paths."""
        parts = []

        parts.append("请使用 Read 工具读取以下文档，提取验收标准、技术约束和验证策略：")
        parts.append("")

        if self.prd_file.exists():
            parts.append(f"- PRD文档: {self.prd_file}")
        elif self.prd_md_file.exists():
            parts.append(f"- PRD文档: {self.prd_md_file}")

        if self.arch_file.exists():
            parts.append(f"- 架构设计文档: {self.arch_file}")

        if self.test_file.exists():
            parts.append(f"- 测试设计文档: {self.test_file}")

        parts.append("")
        parts.append("读取后，提取以下结构化信息并输出JSON：")
        parts.append("1. acceptance_criteria (验收标准): 验收标准列表（从PRD提取）")
        parts.append("   每个包含 id, description, category, priority")
        parts.append("2. technical_constraints (技术约束): 技术约束列表（从架构设计提取）")
        parts.append("   每个包含 id, description, category")
        parts.append("3. verification_strategies (验证策略): 验证策略列表（从测试设计提取）")
        parts.append("   每个包含 id, description, method")

        return "\n".join(parts)

    # -----------------------------------------------------------------------
    # Step 2: Evidence Collection
    # -----------------------------------------------------------------------

    def collect_failure_evidence(self, requirement_context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Step 2: Collect verification failure evidence from verification report.

        Reads verification_report.json via persistence manager, extracts verification points
        with status=FAILED, and builds technical failure evidence for each failed point.
        Can also read detailed logs for additional context.

        Args:
            requirement_context: Requirement context from Step 1 (unused but kept for API consistency)

        Returns:
            List of failure evidence items with keys: vp_id, actual_result, evidence, test_command
        """
        print("[Repair] Step 2: Collecting failure evidence...")

        # Load verification report via persistence manager
        verification_report = self.persistence.load_report()
        if not verification_report:
            print("[Repair] No verification report found, skipping evidence collection")
            return []

        # Load verification plan to get test_command for each verification point
        test_commands = {}
        plan_file = self.plan_dir / "verification_plan.json"
        if plan_file.exists():
            try:
                with open(plan_file, "r", encoding="utf-8") as f:
                    plan_data = json.load(f)
                for vp in plan_data.get("verification_points", []):
                    vp_id = vp.get("id")
                    if vp_id:
                        test_commands[vp_id] = vp.get("test_command", "")
            except Exception:
                pass

        evidence_items = []

        # Extract failed verification results
        # 2026-09-07: include BLOCKED alongside FAILED so the
        # binary-stale pre-flight surface is reflected as repair-task
        # evidence. Without this branch, ``_build_deterministic_repair_tasks``
        # below cannot emit a ``cargo build`` / ``maturin develop``
        # task and the LLM-driven path keeps hallucinating pytest
        # fixes that aren't applicable.
        for result in verification_report.get("verification_results", []):
            if result.get("status") in ("FAILED", "BLOCKED"):
                vp_id = result.get("id", "unknown")

                # Get detailed logs for this verification point if available
                detailed_logs = self.persistence.get_verification_point_logs(vp_id)
                log_context = ""
                if detailed_logs:
                    # Extract relevant log entries
                    for log_entry in detailed_logs[-5:]:  # Last 5 log entries
                        if log_entry.get("event_type") in ("pytest_output", "code_review", "test_error"):
                            log_context += f"\n[{log_entry.get('event_type')}]\n"
                            log_context += str(log_entry.get("data", {}))[:500]

                evidence_items.append({
                    "vp_id": vp_id,
                    "actual_result": result.get("actual_result", ""),
                    "evidence": result.get("evidence", "") + log_context,
                    "test_command": test_commands.get(vp_id, "")
                })

        print(f"[Repair] Collected {len(evidence_items)} failure evidence items")
        return evidence_items

    def _find_related_criteria(self, vp_id: str, requirement_context: Dict[str, Any]) -> List[str]:
        """Find PRD acceptance criteria related to a verification point."""
        # Try to load verification plan to get verification point details
        plan_file = self.plan_dir / "verification_plan.json"
        if plan_file.exists():
            try:
                with open(plan_file, "r", encoding="utf-8") as f:
                    plan_data = json.load(f)
                for vp in plan_data.get("verification_points", []):
                    if vp.get("id") == vp_id:
                        # Extract related PRD criteria from verification point
                        criteria = vp.get("related_prd_criteria", "")
                        if criteria:
                            return [criteria]
            except Exception:
                pass

        # Fallback: return all acceptance criteria descriptions
        criteria = requirement_context.get("acceptance_criteria", [])
        result = []
        for c in criteria:
            if isinstance(c, dict):
                result.append(c.get("description", str(c)))
            else:
                result.append(str(c))
        return result

    def _format_requirement_deviation(
        self,
        deviation: Dict[str, Any],
        requirement_context: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Format requirement deviation from verification report."""
        vp_id = deviation.get("verification_point_id", "unknown")
        deviation_type = deviation.get("type", "unknown")
        description = deviation.get("description", "")
        severity = deviation.get("severity", "medium")

        # Find related criteria
        related_criteria = self._find_related_criteria(vp_id, requirement_context)

        return {
            "verification_point_id": vp_id,
            "technical_failure": f"需求偏离类型: {deviation_type}",
            "requirement_deviation": description,
            "related_prd_criteria": related_criteria[0] if related_criteria else "未知",
            "severity": severity
        }

    def map_to_requirement_deviations(
        self,
        requirement_context: Dict[str, Any],
        failure_evidence: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Map technical failures to requirement deviation descriptions.

        Uses LLM to map each technical failure to a requirement deviation,
        referencing original PRD acceptance criteria.

        Args:
            requirement_context: Requirement context with acceptance_criteria
            failure_evidence: List of failure evidence items from collect_failure_evidence

        Returns:
            List of deviation mappings with keys:
            - deviation_description: How the failure violates PRD acceptance criteria
            - related_acceptance_criteria: Related acceptance criteria from PRD
            - severity: high/medium/low
        """
        deviations = []

        for evidence in failure_evidence:
            vp_id = evidence.get("vp_id", "unknown")
            actual_result = evidence.get("actual_result", "")

            # Find related acceptance criteria
            related_criteria = self._find_related_criteria(vp_id, requirement_context)
            criteria_text = related_criteria[0] if related_criteria else "未知"

            # Build prompt for LLM (file-path based, no embedded content)
            prompt = self._build_deviation_mapping_prompt(evidence)

            system_message = (
                "你是一位资深QA工程师，擅长分析技术失败并映射到需求偏离。"
                "不要针对pytest错误修复，必须回到原始PRD验收标准理解需求偏离。"
                "你可以使用 Read 工具读取文件获取完整上下文。"
            )

            try:
                result = self.coding_tool.query_json(
                    prompt=prompt,
                    system_instruction=system_message,
                )

                deviation = {
                    "deviation_description": result.get(
                        "deviation_description",
                        result.get("requirement_deviation", f"验证失败：{actual_result[:100]}")
                    ),
                    "related_acceptance_criteria": result.get(
                        "related_acceptance_criteria",
                        result.get("related_prd_criteria", criteria_text)
                    ),
                    "severity": result.get("severity", "medium")
                }
                deviations.append(deviation)

            except Exception as e:
                print(f"[Repair] Error mapping failure for {vp_id}: {e}")
                deviations.append({
                    "deviation_description": f"验证失败：{actual_result[:100]}",
                    "related_acceptance_criteria": criteria_text,
                    "severity": "medium"
                })

        return deviations

    def _build_deviation_mapping_prompt(self, evidence: Dict[str, Any]) -> str:
        """Build prompt instructing LLM to read files and map failures to deviations.

        Instead of embedding full document content, provides file paths so the LLM
        can use Read/Grep tools to fetch only the relevant sections.
        """
        parts = []

        parts.append("请使用 Read 工具读取以下文件来分析需求偏离：")
        parts.append("")

        # Point to verification report for full failure context
        if self.verification_report_file.exists():
            parts.append(f"- 验证报告: {self.verification_report_file}")

        # Point to PRD for acceptance criteria
        if self.prd_file.exists():
            parts.append(f"- PRD文档: {self.prd_file}")
        elif self.prd_md_file.exists():
            parts.append(f"- PRD文档: {self.prd_md_file}")

        # Point to cached requirement context for quick reference
        cache_file = self.plan_dir / "requirement_context.json"
        if cache_file.exists():
            parts.append(f"- 已提取的需求上下文: {cache_file}")

        parts.append("")
        parts.append("聚焦以下验证失败：")
        parts.append(f"- 验证点ID: {evidence.get('vp_id', 'unknown')}")
        parts.append(f"- 技术失败描述: {evidence.get('actual_result', '')[:300]}")
        parts.append(f"- 严重程度: {evidence.get('severity', 'medium')}")
        parts.append("")
        parts.append("任务：读取PRD中的相关验收标准，将上述技术失败映射为需求偏离描述。")
        parts.append("")
        parts.append("请输出JSON格式：")
        parts.append("{")
        parts.append('  "deviation_description": "需求偏离描述（引用PRD验收标准）",')
        parts.append('  "related_acceptance_criteria": "最相关的PRD验收标准原文",')
        parts.append('  "severity": "high|medium|low"')
        parts.append("}")
        parts.append("")
        parts.append("只输出 JSON，不要 markdown 代码块标记。")

        return "\n".join(parts)

    # -----------------------------------------------------------------------
    # Step 3: Task Generation
    # -----------------------------------------------------------------------

    def generate_repair_tasks(
        self,
        requirement_context: Dict[str, Any],
        evidence_items: List[Dict[str, Any]],
        round_number: int = 1,
    ) -> List[Dict[str, Any]]:
        """Public alias for :meth:`_generate_repair_tasks`.

        Kept for backwards-compat with test code and external callers
        that drive the three-step workflow manually (skipping the
        round-filed orchestration in :meth:`generate_verification_tasks`).
        Internally delegates to the private method.
        """
        return self._generate_repair_tasks(
            requirement_context, evidence_items, round_number
        )

    #: Where a repair task's ``test_command`` came from. Recorded on the
    #: task as ``test_command_source`` so an operator reading
    #: ``tasks.json`` (or the execution log) can tell an LLM-authored
    #: command apart from a derived one — the two carry different
    #: confidence.
    TEST_COMMAND_SOURCES = ("llm", "vp", "single_evidence", "missing")

    def _resolve_repair_test_command(
        self,
        task: Dict[str, Any],
        evidence_items: List[Dict[str, Any]],
    ) -> Tuple[str, str]:
        """Return ``(command, source)`` for a generated repair task.

        The contract is that every repair task ships with a runnable
        command: the completion gate cross-checks the subagent's
        ``TEST_RESULT`` claim against the command's exit code, and with
        no command the verdict falls back to the subagent's word alone.

        Resolution order:

        1. ``llm`` — the model supplied one that passes the command-quality
           check. Preferred: only the model knows what the *fixed* command
           should look like, which may differ from the failing one (e.g. a
           repair whose whole point is that the verification point's
           command was wrong).
        2. ``vp`` — the task named its ``vp_id`` and that verification
           point has a command. Derived, so weaker: re-running the
           failing command may not be the right check if the command
           itself was the defect.
        3. ``single_evidence`` — exactly one verification point failed,
           so the task can only be about that one and its command is
           unambiguous. Same caveat as (2).
        4. ``missing`` — nothing usable could be derived. Returned as an
           empty command plus an explicit source rather than silently
           substituting ``""``, so the caller can surface it.

        A model-authored command that ``test_command_quality`` flags is
        treated as absent rather than shipped. A structurally-broken
        command is worse than none: it guarantees a FALSE failure every
        time (the chain exits non-zero even when the fix is correct),
        whereas no command degrades to the audit second pass, which can
        actually pass.

        The model-authored candidate additionally has to survive the
        pre-repair probe (:meth:`_passes_falsifiability`): a command the
        LLM wrote that already exits 0 on the current tree proves
        nothing about the repair, so it is discarded the same way a
        structurally-broken one is. Only source (1) is probed — the
        derived fallbacks come from the verification point's own
        command, and a vacuous command *there* means the verification
        point itself is broken, which is a different defect that should
        surface rather than be silently routed around.
        """
        command = str(task.get("test_command") or "").strip()
        if command:
            issues = inspect_command(command)
            if issues:
                print(
                    f"[Repair] Discarding unusable test_command from the LLM "
                    f"({issues[0].code}): {command[:120]}"
                )
            elif not self._passes_falsifiability(command):
                print(
                    f"[Repair] Discarding vacuous test_command from the LLM "
                    f"(it already passes before the repair): {command[:120]}"
                )
            else:
                return command, "llm"

        by_vp = {
            str(e.get("vp_id")): str(e.get("test_command") or "").strip()
            for e in evidence_items
            if isinstance(e, dict) and e.get("vp_id")
        }

        vp_id = str(task.get("vp_id") or "").strip()
        if vp_id and by_vp.get(vp_id):
            return by_vp[vp_id], "vp"

        usable = [c for c in by_vp.values() if c]
        if len(usable) == 1:
            return usable[0], "single_evidence"

        return "", "missing"

    #: Set ``PDT_DISABLE_FALSIFIABILITY_PROBE=1`` to skip the pre-repair
    #: probe. It runs each distinct candidate command as a subprocess, so
    #: an operator debugging generation in an environment where the
    #: project cannot execute (missing toolchain, offline) needs a way
    #: out that is not "edit the code".
    FALSIFIABILITY_PROBE_ENV = "PDT_DISABLE_FALSIFIABILITY_PROBE"

    def _passes_falsifiability(self, command: str) -> bool:
        """True when ``command`` fails on the current, pre-repair tree.

        This is the RED invariant. A command that already exits 0 before
        the repair runs cannot distinguish a correct fix from no change
        at all, so shipping it would make the task's completion verdict
        vacuous — the failure mode behind an earlier plan's 142 empty-diff
        commits. ``inspect_command`` cannot see it: the shape is legal,
        only the *outcome* is wrong.

        The probe runs in ``self.project_dir`` and is memoised per
        command string, because repair rounds routinely emit several
        tasks with the same command. A timeout counts as passing (see
        :func:`check_falsifiable`): something real was executing, which
        is all this check needs to know.
        """
        if os.environ.get(self.FALSIFIABILITY_PROBE_ENV) not in (None, "", "0"):
            return True

        cached = self._falsifiability_cache.get(command)
        if cached is not None:
            return cached

        ok, detail = check_falsifiable(
            command,
            cwd=str(self.project_dir),
            timeout=self.FALSIFIABILITY_TIMEOUT_S,
        )
        self._falsifiability_cache[command] = ok
        if not ok:
            print(f"[Repair] Falsifiability probe rejected a command: {detail}")
        return ok

    def _generate_repair_tasks(
        self,
        requirement_context: Dict[str, Any],
        evidence_items: List[Dict[str, Any]],
        round_number: int
    ) -> List[Dict[str, Any]]:
        """
        Step 3: Generate repair tasks based on requirement context and evidence.

        Args:
            requirement_context: Requirement context from Step 1
            evidence_items: Requirement deviation evidence from Step 2
            round_number: Current verification round number

        Returns:
            List of repair tasks
        """
        print("[Repair] Step 3: Generating repair tasks...")

        if not evidence_items:
            print("[Repair] No requirement deviations found, no repair tasks generated")
            return []

        # Heuristic pass: detect specific test_command failure patterns and
        # emit deterministic repair tasks that bypass the LLM's "add more
        # tests" bias. The LLM repeatedly misidentified "pytest collected 0
        # items" as a missing-test problem and produced tasks that added or
        # renamed tests instead of fixing the verification_plan.json -k filter.
        deterministic_tasks = self._build_deterministic_repair_tasks(
            evidence_items, round_number
        )
        if deterministic_tasks:
            print(
                f"[Repair] Generated {len(deterministic_tasks)} deterministic "
                f"repair task(s) from heuristics"
            )
            return deterministic_tasks

        # Get next task ID prefix for this round
        next_id_prefix = self._get_next_repair_task_id(round_number)

        # Build prompt for task generation (file-path based, no embedded content)
        prompt = self._build_repair_task_prompt(evidence_items, next_id_prefix)

        try:
            result = self.coding_tool.query_json(
                prompt=prompt,
                system_instruction=REPAIR_SYSTEM_PROMPT,
            )

            tasks = result.get("tasks", [])

            tasks = [t for t in tasks if isinstance(t, dict)]

            # Ensure tasks have required fields and project_dir, and force the
            # R{round}- prefix so the ID matches the current verification round
            # regardless of what the LLM produced.
            #
            # 2026-09-16: ``test_command`` used to be filled
            # with ``setdefault(..., "")``, so an LLM that omitted the field
            # produced a task with no command at all — silently disabling the
            # command-line half of the dual-signal completion rule. Such
            # tasks ship with
            # ``test_command = NULL``; the ones that "complete" do so on the
            # subagent's self-report alone (``test_cross_verify_unverified``),
            # which is exactly the single-signal mode the rule exists to
            # prevent. See ``_resolve_repair_test_command`` for the fallbacks.
            unverifiable: list[str] = []
            derived: list[str] = []
            for i, task in enumerate(tasks, 1):
                task["id"] = f"{next_id_prefix}-{i}"
                task.setdefault("status", "pending")
                task.setdefault("updated_time", None)
                task.setdefault("failure_reason", None)
                task.setdefault("project_dir", str(self.project_dir))
                command, source = self._resolve_repair_test_command(
                    task, evidence_items,
                )
                task["test_command"] = command
                # NOTE: the source is deliberately NOT written onto the task
                # dict. ``PlanTaskRepository.add_task`` rejects any key outside
                # ``ALLOWED_STATIC_TASK_FIELDS`` (there is no
                # ``test_command_source`` column), so persisting it would break
                # the single-writer path. It goes to the log instead.
                if source == "missing":
                    unverifiable.append(str(task["id"]))
                elif source != "llm":
                    derived.append(f"{task['id']}:{source}")

            if derived:
                print(
                    f"[Repair] {len(derived)} repair task(s) got a derived "
                    f"test_command (not LLM-authored): {derived}"
                )

            if unverifiable:
                # Not fatal: the repair still needs to happen, and a task
                # with no command degrades to the audit second pass rather
                # than failing outright. But it must be visible — the
                # previous behaviour was silent, which is why four tasks
                # reached the executor unverifiable without anyone noticing.
                print(
                    f"[Repair] WARNING: {len(unverifiable)} repair task(s) "
                    f"have no derivable test_command: {unverifiable}. "
                    f"Completion will rely on the audit second pass."
                )

            print(f"[Repair] Generated {len(tasks)} repair tasks")
            return tasks

        except Exception as e:
            print(f"[Repair] Error generating repair tasks: {e}")
            # Fallback: generate simplified repair tasks directly from evidence
            return self._generate_fallback_repair_tasks(evidence_items, next_id_prefix)

    def _generate_fallback_repair_tasks(
        self, evidence_items: List[Dict[str, Any]], id_prefix: str
    ) -> List[Dict[str, Any]]:
        """Generate simplified repair tasks directly from evidence when LLM fails."""
        tasks = []
        for i, evidence in enumerate(evidence_items, 1):
            vp_id = evidence.get("vp_id", f"VP-{i}")
            title = f"修复 {vp_id}: {evidence.get('actual_result', '验证失败')[:60]}"
            tasks.append({
                "id": f"{id_prefix}-{i}" if "-" not in id_prefix else f"{id_prefix.rsplit('-', 1)[0]}-{i}",
                "title": title,
                "description": (
                    f"验证点 {vp_id} 失败。\n\n"
                    f"**实际结果：** {evidence.get('actual_result', '未知')}\n\n"
                    f"**证据：** {evidence.get('evidence', '无')}\n\n"
                    f"请根据上述验证失败信息修复代码，确保验证点通过。"
                ),
                # 2026-09-16: ``.get(key, "pytest")`` returns ``""`` when the
                # evidence item carries an EMPTY command (which happens
                # whenever the verification point has none) — the default
                # only fires when the key is absent. ``or`` is what the
                # intent requires.
                "test_command": evidence.get("test_command") or "pytest",
                "status": "pending",
                "updated_time": None,
                "failure_reason": None,
            })
        if tasks:
            print(f"[Repair] Fallback: generated {len(tasks)} simplified repair tasks")
        return tasks

    def _build_deterministic_repair_tasks(
        self,
        evidence_items: List[Dict[str, Any]],
        round_number: int
    ) -> List[Dict[str, Any]]:
        """Pattern-match evidence to emit precise, LLM-free repair tasks.

        The LLM agent has a strong bias toward "add/rename tests" when it sees
        "0 tests selected". This pass catches the most common classes of
        non-code failures up front so the loop doesn't waste tokens on a bad
        repair suggestion.

        Returns a list of repair tasks (possibly empty). When non-empty, the
        caller short-circuits the LLM-driven path and uses these directly.
        """
        tasks: List[Dict[str, Any]] = []
        next_id_prefix = self._get_next_repair_task_id(round_number)
        idx = 0

        import re as _re

        for evidence in evidence_items:
            vp_id = evidence.get("vp_id", "")
            actual_result = evidence.get("actual_result", "") or ""
            actual_evidence = evidence.get("evidence", "") or ""
            original_cmd = evidence.get("test_command", "")
            combined = (actual_result + "\n" + actual_evidence).lower()

            idx += 1
            task_id = (
                f"{next_id_prefix}-{idx}" if "-" not in next_id_prefix
                else f"{next_id_prefix.rsplit('-', 1)[0]}-{idx}"
            )

            # 2026-09-18：Pattern 1 / Pattern 2 已删除。它们分别让修复 agent
            # 去改 `verification_plan.json` 里某条 VP 的 `-k` 关键字和
            # shell 占位符 —— 也就是**改写验收判据本身**。
            #
            # 那次重构已经确认这是错的，有三层理由：
            #   1. 违反 2026-09-18 的不可变原则：VP 的声明字段不许被改，
            #      要变只能作废 + 新增（`verification_plan_delta` 里早就写着
            #      "绝不修改已有验证点——标题、断言、test_command 都不许改"，
            #      只有 repair 这一侧没遵守）；
            #   2. 交付物是 this repository `plans/` 下的文件，在 `project_dir` 之外，
            #      修复生成器给不出 project 相对路径 → `files_to_modify: []`
            #      → 空 diff 门禁必然失败 → 强制拆分，而拆分又只能靠发明
            #      项目侧产物来满足门禁（that plan
            #      `tools/verify_vp_rust_case.py` 被"新建"了四次）；
            #   3. VP 现在根本没有 test_command 了 —— 判定依据是 method 自己
            #      的产物（api_test 的 request+assertions / code_review 的
            #      citations / ui_validation 的 checkpoints）。
            #
            # 验收判据坏掉时的正确处理是：作废该 VP 并新增一条表达正确的，
            # 而不是让被判分的对象去改判据。


            # Pattern 3: binary stale (BLOCKED verdict). Detect via the
            # ``binary stale`` keyword that ``FreshnessReport.detail`` and
            # the BLOCKED verdict's ``actual_result`` both carry. Pull
            # the rebuild command out of the evidence block so the
            # suggested fix is concrete (cargo build vs maturin develop
            # vs pytest refresh fixture).
            #
            # 2026-09-07: without this branch the deterministic
            # repair-task generator emits 0 tasks for binary-stale VPs,
            # the orchestrator hits ``no_repair_tasks`` and terminates
            # the round as failed — leaving the operator with no
            # actionable hint on how to fix the BLOCKED state.
            is_binary_stale = (
                "binary stale" in combined
                or "binary_stale" in combined
                or "binary_freshness" in actual_evidence.lower()
            )
            if is_binary_stale:
                # Try to extract the rebuild command from the evidence
                # JSON. The freshness payload embeds ``rebuild_command``
                # for downstream automation.
                rebuild_match = _re.search(
                    r"rebuild[_\"']?\s*:?\s*?[\"']?([^\"'\n]{4,200})",
                    actual_evidence + "\n" + actual_result,
                    flags=_re.IGNORECASE,
                )
                suggested_rebuild = (
                    rebuild_match.group(1).strip().rstrip(",}")
                    if rebuild_match
                    else "<run the project's documented rebuild command>"
                )
                tasks.append({
                    "id": task_id,
                    "title": f"修复 {vp_id} 的 binary stale 状态：重新编译/刷新 binary",
                    "description": (
                        f"## 背景\n"
                        f"`{vp_id}` 触发了 binary_freshness 预检失败（BLOCKED verdict），binary 的 mtime 早于源码 mtime 或根本没编译。\n\n"
                        f"**证据摘录 (actual_result):**\n```\n{actual_result[:600]}\n```\n\n"
                        f"**底层类型:** `{evidence.get('vp_id', '')}`\n\n"
                        f"## 目标\n"
                        f"在 `{self.project_dir}` 项目根里运行 fresh rebuild，让 binary mtime ≥ source mtime，让 binary_freshness 预检 PASSED。\n\n"
                        f"## 修复建议\n"
                        f"运行（按 FreshnessReport 提供的命令，截断到第一行）：\n```\n{suggested_rebuild.splitlines()[0]}\n```\n\n"
                        f"## TDD 规格\n"
                        f"- rebuild 后 `stat -f '%Sm %N' <binary_path>` 的 mtime 必须在最新 source 文件 mtime 之后\n"
                        f"- 同一 `{vp_id}` 在新一轮 verification 必须收到 `binary_freshness_pass` 而非 `binary_freshness_blocked`\n"
                        f"- 不要修改 test_command — 这是 binary 层面的问题，不是测试命令的问题"
                    ),
                    "test_command": (
                        f"cd {self.project_dir} && {suggested_rebuild.splitlines()[0]}"
                    ),
                    "status": "pending",
                    "updated_time": None,
                    "failure_reason": None,
                    "project_dir": str(self.project_dir),
                    "test_commands": [
                        f"cd {self.project_dir} && {suggested_rebuild.splitlines()[0]}",
                    ],
                    "model_type": "small",
                })
                continue

        return tasks

    def _get_next_repair_task_id(self, round_number: int) -> str:
        """Determine the next repair task ID prefix for a verification round."""
        return f"R{round_number}"

    def append_to_tasks(self, repair_tasks: List[Dict[str, Any]], round_number: int) -> Path:
        """Append repair tasks to the existing project tasks.json without overwriting.

        Preserves the existing task list and appends the new repair tasks with
        R{round}- prefixed IDs. If project_dir/tasks.json does not exist, it
        is created with just the repair tasks.

        Args:
            repair_tasks: List of repair task dicts to append.
            round_number: Current verification round (used as ID prefix).

        Returns:
            Path to the updated tasks.json file.
        """
        tasks_file = self.project_dir / "tasks.json"

        existing_tasks: List[Dict[str, Any]] = []
        if tasks_file.exists():
            try:
                with open(tasks_file, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, list):
                    existing_tasks = payload
                elif isinstance(payload, dict):
                    existing_tasks = payload.get("tasks", []) or []
            except Exception:
                existing_tasks = []

        prefixed: List[Dict[str, Any]] = []
        prefix = self._get_next_repair_task_id(round_number)
        for i, task in enumerate(repair_tasks, 1):
            task_copy = dict(task)
            task_copy.setdefault("id", f"{prefix}-{i}")
            prefixed.append(task_copy)

        combined = list(existing_tasks) + prefixed

        self.project_dir.mkdir(parents=True, exist_ok=True)
        with open(tasks_file, "w", encoding="utf-8") as f:
            json.dump(combined, f, indent=2, ensure_ascii=False)

        return tasks_file

    def _build_repair_task_prompt(
        self,
        evidence_items: List[Dict[str, Any]],
        next_id_prefix: str
    ) -> str:
        """Build prompt for repair task generation using file paths instead of embedded content.

        The LLM uses Read/Grep tools to fetch only the relevant sections from
        PRD, architecture, and verification documents.
        """
        parts = []

        parts.append("请使用 Read 工具读取以下文件获取完整上下文：")
        parts.append("")

        if self.prd_file.exists():
            parts.append(f"- PRD文档: {self.prd_file}")
        elif self.prd_md_file.exists():
            parts.append(f"- PRD文档: {self.prd_md_file}")

        if self.arch_file.exists():
            parts.append(f"- 架构设计文档: {self.arch_file}")

        if self.test_file.exists():
            parts.append(f"- 测试设计文档: {self.test_file}")

        cache_file = self.plan_dir / "requirement_context.json"
        if cache_file.exists():
            parts.append(f"- 已提取的验收标准缓存: {cache_file}")

        if self.verification_report_file.exists():
            parts.append(f"- 验证报告: {self.verification_report_file}")

        parts.append("")
        parts.append("## 需求偏离摘要")
        parts.append(f"共 {len(evidence_items)} 个需求偏离需要修复：")
        parts.append("")

        # Load requirement_deviations from verification report to get proper deviation descriptions
        deviation_map = {}
        try:
            report = self.persistence.load_report()
            if report:
                for d in report.get("requirement_deviations", []):
                    vp_id = d.get("verification_point_id", "")
                    deviation_map[vp_id] = d
        except Exception:
            pass

        for i, evidence in enumerate(evidence_items, 1):
            vp_id = evidence.get('vp_id', 'unknown')
            # Prefer requirement_deviations from verification report
            deviation_info = deviation_map.get(vp_id, {})
            deviation = deviation_info.get('description', evidence.get('actual_result', '未知偏离'))
            severity = deviation_info.get('severity', 'medium')
            parts.append(f"{i}. VP {vp_id} ({severity}): {str(deviation)[:200]}")

        parts.append("")
        parts.append("## 任务生成要求")
        parts.append("")
        parts.append(f"目标项目目录: {self.project_dir}")
        parts.append(f"项目名称: {self.project_dir.name}")
        parts.append("")
        parts.append(f"起始任务ID: {next_id_prefix}")
        parts.append("")
        parts.append("请读取PRD和验证报告文件后，为每个需求偏离生成一个修复任务。")
        parts.append("任务必须：")
        parts.append("1. title 指向需求偏离点（而非技术错误）")
        parts.append("2. description 包含完整的PRD验收标准引用、偏离证据、修复建议、TDD规格")
        parts.append("3. test_command 能复现该需求偏离")
        parts.append("4. **只修改目标项目代码，不要修改执行框架代码**")
        parts.append("5. **test_command 应直接在项目目录下运行，不要包含 cd backend 等切换目录的前缀**")
        parts.append("")
        parts.append("## 重要：失败类型的修复目标选择")
        parts.append("")
        parts.append("请根据 evidence 中的 `actual_result` 字段判断失败类型，选择正确的修复目标：")
        parts.append("- **代码逻辑失败**（如 assertion 失败、TypeError、空指针等）→ 修改 `project_dir` 下的源代码或测试代码")
        parts.append("- **验收判据本身写错了**（如期望值填错、断言不成立）→ 这是 **VP 的问题，不是源码的问题**。修复任务应该改源码让断言成立；如果确实是判据写错了，正确做法是**作废该 VP 并新增一条**，而不是原地改写（2026-09-18：VP 的声明字段不可变）")
        parts.append("- **配置缺失**（如缺失依赖、缺环境变量、缺 fixture）→ 修改项目配置文件或新增 fixture")
        parts.append("- **需求描述不清**（PRD 写得不具体导致实现偏离）→ 修改 `prd.json` 中的对应验收标准")
        parts.append("")
        parts.append("判断依据：`evidence` / `assertions_failed` 里会直接给出**期望值与实际值**。照着它去改源码，不要去改判据。")

        return "\n".join(parts)

    # -----------------------------------------------------------------------
    # Full Workflow
    # -----------------------------------------------------------------------

    def generate_verification_tasks(self, round_number: int) -> Tuple[Path, int]:
        """
        Run complete three-step repair task generation workflow and save to a
        dedicated verification tasks file.

        Args:
            round_number: Current verification round number (used in filename).

        Returns:
            (file_path, number_of_tasks_generated)
        """
        print(f"[Repair] Starting requirement-driven repair task generation for round {round_number}...")

        # Step 1: Understand requirements
        requirement_context = self.understand_requirements()

        # Step 2: Collect failure evidence
        evidence_items = self.collect_failure_evidence(requirement_context)

        # Step 3: Generate repair tasks
        repair_tasks = self._generate_repair_tasks(requirement_context, evidence_items, round_number)

        if not repair_tasks:
            print("[Repair] No repair tasks generated")
            return self._get_verification_tasks_file(round_number), 0

        # Save to dedicated verification tasks file
        tasks_file = self._save_verification_tasks(repair_tasks, round_number)

        print(f"[Repair] Successfully saved {len(repair_tasks)} repair tasks to {tasks_file.name}")
        return tasks_file, len(repair_tasks)

    def _save_verification_tasks(self, repair_tasks: List[Dict[str, Any]], round_number: int) -> Path:
        """Save repair tasks to a dedicated verification tasks file."""
        tasks_file = self._get_verification_tasks_file(round_number)
        tasks_data = {
            "requirement": f"Verification repair tasks - Round {round_number}",
            "verification_round": round_number,
            "generated_at": datetime.now().isoformat(),
            "tasks": repair_tasks
        }
        with open(tasks_file, "w", encoding="utf-8") as f:
            json.dump(tasks_data, f, indent=2, ensure_ascii=False)
        return tasks_file

    def generate_and_append_tasks(self, round_number: int = 1) -> int:
        """Convenience wrapper used by tests: returns just the task count.

        Performs the same three-step workflow as
        :meth:`generate_verification_tasks` and additionally writes a
        mirror copy of the result to ``project_dir / tasks.json`` (the
        legacy path that older test code and external tooling still
        reads). The mirror is APPENDED to any existing tasks (the
        executor's TaskManager keeps a long-lived tasks.json that
        pre-existing implementation tasks live in) — never overwrites
        them. Returns the number of repair tasks generated.

        The orchestrator-facing path is :meth:`generate_verification_tasks`,
        which returns ``(tasks_file, count)``; this wrapper collapses to
        just the count for tests that only care about the integer.

        Args:
            round_number: Verification round number. Defaults to 1.

        Returns:
            The number of repair tasks generated (0 if none).
        """
        tasks_file, task_count = self.generate_verification_tasks(round_number)

        if task_count > 0:
            # Read back the just-written round file, then APPEND its
            # repair tasks to the legacy tasks.json (TaskManager-readable).
            try:
                with open(tasks_file, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                repair_tasks = payload.get("tasks", [])
                self._append_to_tasks_json(repair_tasks)
            except Exception:
                # Mirror is best-effort: a failure here must not mask
                # the round-file write, which is the source of truth.
                pass

        return task_count


# ---------------------------------------------------------------------------
# Convenience Functions
# ---------------------------------------------------------------------------

def generate_repair_tasks(
    plan_id: str,
    project_dir: str,
    plan_base_dir: Optional[str] = None,
    coding_tool: Optional[CodingTool] = None,
    round_number: int = 1
) -> Tuple[Path, int]:
    """
    Convenience function to generate repair tasks for a plan.

    Args:
        plan_id: Plan identifier
        project_dir: Project directory
        plan_base_dir: Base directory for plans (defaults to ../plans)
        coding_tool: Optional coding tool (defaults to ClaudeCodingTool)
        round_number: Current verification round number

    Returns:
        (file_path, number_of_tasks_generated)
    """
    if plan_base_dir is None:
        # 2026-09-13: ``PDT_PLANS_DIR``-aware (see
        # ``config_paths.resolve_plans_dir``) so a test that generates
        # repair tasks for a fixture plan_id does not create directories
        # in the operator's live plans tree.
        plan_base_dir = resolve_plans_dir()
    else:
        plan_base_dir = Path(plan_base_dir)

    plan_dir = plan_base_dir / plan_id

    if coding_tool is None:
        from coding_tool import ClaudeCodingTool
        # 2026-09-13 provider routing: repair-task generation is a
        # high-stakes replanning step — route via the strong tier.
        coding_tool = ClaudeCodingTool(scene="repair_generation")

    generator = RepairTaskGenerator(coding_tool, plan_dir, Path(project_dir))
    return generator.generate_verification_tasks(round_number)


# ---------------------------------------------------------------------------
# Single-call content flow (2026-09-07 plan)
# ---------------------------------------------------------------------------
#
# Why this block exists
# ----------------------
# The legacy :meth:`RepairTaskGenerator.generate_verification_tasks` runs
# three independent LLM calls — understand_requirements, then
# collect_failure_evidence, then _generate_repair_tasks — and the
# middle step asks the LLM to *re-derive* the failure from PRD context
# rather than reading the on-disk ``verification_report.json`` directly.
# The net effect was that ``repair_tasks`` came back empty for any plan
# whose evidence chain didn't perfectly line up with PRD criteria, and
# the orchestrator's ``no_repair_tasks`` branch then terminated the
# auto-loop as failed — leaving the plan stuck in
# ``verification_failed`` with no path forward.
#
# This block replaces that flow with a single LLM call that produces
# *content* only (``title``, ``description``, ``acceptance_criteria``,
# ``failed_vp_id``), while :class:`RepairTaskAssembler` stamps the
# schema-stable tags locally (``id``, ``task_group``, ``priority``,
# ``depends_on``, ``status``). The state machine can therefore trust
# the tags even when the LLM misbehaves.

REPAIR_CONTENT_SYSTEM_PROMPT = """你是一位资深技术架构师，专注于把验证失败的 VP 转写成可执行的修复任务。

**严格输出 JSON**：
{
  "tasks": [
    {
      "failed_vp_id": "VP-034",
      "title": "修复 VP-034 的具体偏差",
      "description": "完整描述：包含背景、失败证据、修复建议（指向源码位置或 verification_plan.json 字段）",
      "acceptance_criteria": "TDD 规格：test_xxx 条件 → 预期",
      "files_to_modify": ["相对项目根的路径", "..."],
      "priority_hint": "high|medium|low",
      "execution_group_hint": 0
    }
  ]
}

**约束**：
1. 不要产出 task_id / depends_on — 这些由系统本地代码生成。
2. description 必须引用 PRD 验收标准（不要引用 pytest 错误）。
3. 一个 failed VP 一个 task；不要把多个 VP 合成一个 task。
4. **不要去改验收判据。** VP 的 `request` / `assertions` / `target_url` 等声明字段是不可变的 —— 它们是"被测对象"，不是"可调参数"。修复任务的职责永远是改**源码**去满足判据。判据确实写错时，正确做法是作废该 VP 并新增一条表达正确的（见 `verification_plan_delta` 的"绝不修改已有验证点"）。
5. `files_to_modify` 必须是**相对项目根目录**的路径（不要绝对路径、不要 `..`）。修复任务的验收命令由系统自动带上，你不需要产出 test_command。路径拿不准时给出你最可能改动的那个文件，不要编造不存在的深层目录。
6. 只输出 JSON，不要 markdown 代码块标记。
"""


#: Matches the part of a generated repair-task id that carries its
#: round and sequence — ``repair-r1-06`` and the breakdown children it
#: spawns (``repair-r1-06-1``, ``repair-r3-03-1-1-2``) all resolve to
#: round 1 / seq 6 and round 3 / seq 3 respectively.
_REPAIR_TASK_ID_RE = re.compile(r"^repair-r(\d+)-(\d+)")


def parse_repair_task_id(task_id: Any) -> Optional[Tuple[int, int]]:
    """``"repair-r1-06"`` → ``(1, 6)``; ``None`` for anything else."""
    match = _REPAIR_TASK_ID_RE.match(str(task_id or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def next_repair_seq_base(
    existing_task_ids: Any, round_number: int,
) -> int:
    """Lowest ``seq`` this round's batch can use without colliding.

    2026-09-20 (post-mortem). Repair ids were ``repair-r{N}-{seq}``
    with ``seq`` always starting at 1, so every *batch* for the same
    round produced the same ids. ``plan_tasks`` is keyed on
    ``(plan_id, task_id)``, so the second batch did not merely duplicate
    the first — it **overwrote** it, taking that row's ``attempt`` /
    ``failure_reason`` / ``commit_sha`` history with it. The breakdown
    children left behind then described a parent whose row had become an
    unrelated task.

    Every counter reset hands the plan a fresh round 1, so batches from
    different rounds end up numbered from ``repair-r1-01`` again; the
    earlier batch's children then sit in ``tasks.json`` under a parent
    written later.

    Returns a base such that seq keeps increasing monotonically for a
    given ``(plan, round)`` forever, so an id is never reused. ``1``
    when nothing has been allocated yet — the pre-existing behaviour,
    which is correct for a plan's first repair batch.
    """
    highest = 0
    for task_id in existing_task_ids or ():
        parsed = parse_repair_task_id(task_id)
        if parsed is None:
            continue
        task_round, seq = parsed
        if task_round != int(round_number):
            continue
        highest = max(highest, seq)
    return highest + 1


#: Halfwidth/fullwidth punctuation pairs the two stores render
#: differently. ``tasks.json`` keeps the LLM's ASCII colon and
#: parentheses; ``plan_tasks`` carries a version normalised to CJK
#: fullwidth. Without folding them, six of the thirteen repair ids
#: looked like conflicts when their titles were identical but for
#: ``:`` vs ``：`` and ``(`` vs ``（``.
_TITLE_PUNCTUATION_FOLD = str.maketrans({
    "：": ":", "（": "(", "）": ")", "，": ",", "；": ";",
    "！": "!", "？": "?", "、": ",", "。": ".",
})


def _normalise_title(title: Any) -> str:
    """Fold the punctuation/whitespace the two stores disagree on."""
    text = str(title or "").translate(_TITLE_PUNCTUATION_FOLD)
    return "".join(text.split())


def cross_store_task_conflicts(
    disk_titles: Any, db_titles: Any,
) -> Dict[str, Tuple[str, str]]:
    """Ids present in both stores whose titles disagree.

    ``plans/<id>/tasks.json`` and
    ``state.db::plan_tasks`` are written by different code paths (the
    executor's load-time reconcile folds DB orphans into ``tasks.json``),
    so an id collision shows up as the *same id describing two different
    tasks* depending on which store you read: a repair row for one
    verification point in ``plan_tasks``, and the breakdown children of an
    unrelated task still hanging off that id in ``tasks.json``.

    Two filters keep the report readable:

    * **A blank title on either side is not a conflict.** Rows that
      predate the ``title`` column being populated carry ``title=NULL``
      in ``plan_tasks``, and flagging every one of them buries the real
      collisions.
    * **Punctuation and whitespace are folded** (see
      :data:`_TITLE_PUNCTUATION_FOLD`). Colliding ids routinely differ
      only in ASCII vs CJK punctuation.

    Returns ``{task_id: (disk_title, db_title)}``. Callers log it;
    nothing here repairs the split, because the fix is to stop
    generating colliding ids in the first place (see
    :func:`next_repair_seq_base`).
    """
    disk = {str(k): str(v or "") for k, v in (disk_titles or {}).items()}
    db = {str(k): str(v or "") for k, v in (db_titles or {}).items()}
    conflicts: Dict[str, Tuple[str, str]] = {}
    for task_id, disk_title in disk.items():
        db_title = db.get(task_id)
        if db_title is None:
            continue
        if not disk_title.strip() or not db_title.strip():
            continue
        if _normalise_title(disk_title) != _normalise_title(db_title):
            conflicts[task_id] = (disk_title, db_title)
    return conflicts


class RepairTaskAssembler:
    """Local-code task object assembler.

    Takes the content dicts produced by
    :meth:`RepairTaskGenerator.generate_repair_contents` plus the
    failed-VP list from
    :func:`verification.verification_report_reader.extract_failed_vps_from_report`,
    and stamps every locally-owned field (``id``, ``task_group``,
    ``priority``, ``depends_on``, ``status``) in a deterministic way
    the state machine can recognise.

    The LLM never sees these tags, so a misbehaving LLM cannot
    fabricate a duplicate ``R1-1`` for round 2, an out-of-bounds
    ``priority`` enum, or a circular ``depends_on`` graph.

    Args:
        round_number: Current verification round (1-based). Used as the
            task ID prefix so round 1 → ``repair-r1-01``, round 2 →
            ``repair-r2-01``, etc.
        failed_vps: The list returned by
            :func:`extract_failed_vps_from_report`. Used as the
            priority fallback source and the ``failed_vp_id`` ↔
            priority lookup table.
        seq_base: First sequence number this batch may use. Defaults to
            ``1`` (a plan's first repair batch). Callers that may be
            emitting a *second* batch for the same round must pass
            :func:`next_repair_seq_base`'s result so ids never repeat.

    Note:
        Task IDs use the ``repair-r{N}-{seq}`` prefix (not the legacy
        ``R{N}-{seq}``). Both forms coexist on disk because the
        executor's TaskManager does not parse IDs — but the new prefix
        lets the dashboard and watchdog grep for repair tasks
        distinctly from the original-stream tasks.

        ``seq`` is **not** the task's position in the batch — it is a
        never-reused allocation counter for ``(plan, round)``. A second
        batch for round 1 might therefore start at ``repair-r1-19``.
        That is deliberate: the number is an identity, not a label.
    """

    def __init__(
        self,
        round_number: int,
        failed_vps: List[Dict[str, Any]],
        vp_test_commands: Optional[Dict[str, str]] = None,
        plan_path: Optional[str] = None,
        seq_base: int = 1,
    ):
        if round_number < 1:
            raise ValueError(f"round_number must be >= 1, got {round_number}")
        if seq_base < 1:
            raise ValueError(f"seq_base must be >= 1, got {seq_base}")
        self.round = round_number
        #: First ``seq`` this batch may use. ``1`` unless the caller has
        #: seen prior batches for this ``(plan, round)`` — see
        #: :func:`next_repair_seq_base` for why reusing seq 1 corrupts
        #: the persisted task history.
        self.seq_base = seq_base
        self.failed_vps: Dict[str, Dict[str, Any]] = {
            str(vp.get("id", "")): vp for vp in failed_vps if vp.get("id")
        }
        # Raw ``{vp_id: test_command}`` read from ``verification_plan.json``
        # (see ``extract_vp_test_commands``). The failed-VP dicts the
        # assembler receives only carry a *bounded summary* of the command
        # plus a JSON-pointer path, neither of which is runnable — so the
        # orchestrator hands the raw map in separately.
        self.vp_test_commands: Dict[str, str] = dict(vp_test_commands or {})
        # Used only to build the "was the plan entry actually changed?"
        # guard command for repairs whose target is the plan file itself.
        self.plan_path: str = str(plan_path or "")

    def assemble(self, contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Stamps schema-stable fields onto each content dict.

        Args:
            contents: Output of
                :meth:`RepairTaskGenerator.generate_repair_contents`.

        Returns:
            List of fully-formed repair-task dicts, each carrying the
            keys consumed by the downstream executor
            (``id``, ``title``, ``description``,
            ``acceptance_criteria``, ``priority``, ``depends_on``,
            ``task_group``, ``execution_group``, ``failed_vp_id``,
            ``round``, ``status``, ``test_command``,
            ``files_to_modify``).

            Empty list if ``contents`` is empty.
        """
        if not contents:
            return []
        tasks: List[Dict[str, Any]] = []
        emitted_seq = self.seq_base - 1
        for content in contents:
            if not isinstance(content, dict):
                continue
            failed_vp_id = str(content.get("failed_vp_id", "") or "")
            # Reject entries that name a VP we never saw as FAILED —
            # the LLM hallucinated the ID. The assembler is the last
            # line of defence here: even if ``_parse_repair_contents_response``
            # let it through, we drop it now.
            if failed_vp_id not in self.failed_vps:
                continue
            vp_plan = self.failed_vps[failed_vp_id]
            title = str(content.get("title", "") or "").strip()
            description = str(content.get("description", "") or "").strip()
            # Half-formed content is salvaged rather than dropped. The LLM
            # occasionally returns the right ``failed_vp_id`` with an empty
            # ``title`` / ``description``; discarding the row used to silently
            # shrink the repair round (and, when *every* row came back
            # half-formed, produce "0 repair tasks" for a round that had real
            # failures). A minimal task anchored on the evidence paths is
            # strictly more useful than nothing — the executor can still Read
            # the referenced report/log files and act on them.
            if not title:
                title = f"[{failed_vp_id}] 修复验证失败项 {failed_vp_id}"
            if not description:
                description = self._fallback_description(failed_vp_id, vp_plan)
            priority = (
                str(content.get("priority_hint") or "").strip().lower()
                or str(vp_plan.get("priority", "medium") or "medium")
            )
            if priority not in {"high", "medium", "low"}:
                priority = "medium"
            try:
                exec_group = int(content.get("execution_group_hint", 0) or 0)
            except (TypeError, ValueError):
                exec_group = 0
            emitted_seq += 1
            acceptance_criteria = str(
                content.get("acceptance_criteria", "") or ""
            ).strip()
            test_command = self._resolve_test_command(
                failed_vp_id, description, acceptance_criteria,
            )
            files_to_modify = self._resolve_files_to_modify(content)
            tasks.append({
                "id": f"repair-r{self.round}-{emitted_seq:02d}",
                "task_group": f"repair-round-{self.round}",
                "execution_group": exec_group,
                "priority": priority,
                "depends_on": self._infer_depends_on(emitted_seq),
                "title": title,
                "description": description,
                "acceptance_criteria": acceptance_criteria,
                # 2026-09-14: the content-only refactor (2026-09-07) dropped
                # ``test_command`` and ``files_to_modify`` from the emitted
                # task, so every repair task reached the executor with no
                # command at all — which made the dual-criterion completion
                # rule silently degrade to "trust the AI claim" (see
                # ``agent._cross_verify`` → ``test_cross_verify_unverified``).
                "test_command": test_command,
                "files_to_modify": files_to_modify,
                "failed_vp_id": failed_vp_id,
                "round": self.round,
                "status": "pending",
            })
        return tasks

    # ------------------------------------------------------------------
    # Command + file-scope resolution (2026-09-14)
    # ------------------------------------------------------------------

    # Phrases that mean "the VP's own test_command is what needs fixing"
    # rather than "the code under test needs fixing". Mirrors the
    # guidance already in ``REPAIR_CONTENT_SYSTEM_PROMPT`` rule 4 and in
    # the legacy deterministic builder.

    def _resolve_test_command(
        self, failed_vp_id: str, description: str, acceptance_criteria: str,
    ) -> str:
        """The command that must exit 0 for this repair to count as done.

        Three cases, in order:

        1. **No command for this VP** — nothing to run. The task still
           gets an empty command rather than a fabricated one; F3's
           executor-side warning (``test_cross_verify_unverified``) is
           what stops that from silently counting as a verified pass.
        2. **The VP's own command is the thing being repaired** — the
           repair rewrites ``verification_plan.json``, so re-running the
           *old* string would fail even on a correct repair. Emit a guard
           that re-reads the VP's command at run time and exits 0 only
           once it differs from the broken one.
        3. **Ordinary repair** — the VP's command verbatim. Re-running it
           is exactly the proof that the deviation is gone.
        """
        command = (self.vp_test_commands.get(failed_vp_id) or "").strip()
        if command:
            return command
        # 2026-09-18: a VP no longer carries a ``test_command``, so a repair
        # task's acceptance command is derived from the *method's* judging
        # basis instead:
        #
        #  * ``api_test`` — replay the VP's own request + assertions. This
        #    is the ideal shape for a repair's gate: the criterion comes
        #    from the requirement (not from the implementation), and the
        #    framework executes it deterministically.
        #  * ``ui_validation`` / ``code_review`` — no command. Their basis
        #    is the evidence artifact (checkpoints / citations), which a
        #    command cannot reproduce; the task runs with an empty command
        #    rather than a fabricated one, and the executor's
        #    ``test_cross_verify_unverified`` warning is what stops that
        #    from silently counting as a verified pass.
        #
        # What is gone for good is case 2 of the old behaviour: "the VP's
        # own command is the thing being repaired". Repair tasks must not
        # edit the acceptance criterion — see the module docstring of
        # ``verification_plan_delta`` and the C8 commit message.
        return self._api_replay_command(failed_vp_id)

    def _api_replay_command(self, failed_vp_id: str) -> str:
        """A runnable command that replays an ``api_test`` VP's assertions.

        Exits 0 only if every assertion still holds, which is exactly the
        question a repair has to answer. Returns "" for VPs of any other
        method.
        """
        vp = self.failed_vps.get(str(failed_vp_id)) or {}
        if str(vp.get("verification_method") or vp.get("method") or "") != "api_test":
            return ""
        if not self.plan_path:
            return ""
        # The runner lives in the backend, not in the target project, so
        # the command cds there before invoking it. The request URL is
        # absolute (service placeholders resolve to 127.0.0.1:port), so the
        # target project's cwd is irrelevant to the replay itself.
        backend_dir = Path(__file__).resolve().parent
        return (
            f"cd {self._shell_quote(str(backend_dir))} && "
            f"{self._shell_quote(sys.executable)} -m verification_api_runner "
            f"--plan {self._shell_quote(str(Path(self.plan_path).parent))} "
            f"--vp {self._shell_quote(str(failed_vp_id))}"
        )


    @staticmethod
    def _shell_quote(value: str) -> str:
        """POSIX single-quote ``value`` (safe for arbitrary bytes)."""
        return "'" + str(value).replace("'", "'\"'\"'") + "'"

    def _resolve_files_to_modify(self, content: Dict[str, Any]) -> List[str]:
        """Normalised file scope for the repair task.

        Preferred source is the LLM's ``files_to_modify`` hint (the same
        shape the main task generator emits). Entries are normalised and
        filtered: relative, no ``..`` traversal, no absolute paths, no
        empty strings. An empty list means "scope unknown" — the executor
        then gives the subagent no file hint and lets it discover the
        paths, which is strictly better than a scope pointing at the
        wrong file.
        """
        raw = content.get("files_to_modify")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        cleaned: List[str] = []
        for entry in raw:
            if not isinstance(entry, str):
                continue
            path = entry.strip().replace("\\", "/")
            if not path or path.startswith("/") or ".." in path.split("/"):
                continue
            if path not in cleaned:
                cleaned.append(path)
        return cleaned

    def _fallback_description(
        self, failed_vp_id: str, vp_plan: Dict[str, Any]
    ) -> str:
        """Minimal-but-actionable description for a half-formed LLM row.

        Anchored on what the on-disk evidence already gives us: a summary
        of what was observed, and the paths an agent can Read for the full
        output. Deliberately terse — the executor subagent reads the
        referenced files rather than trusting prose written blind.
        """
        lines = [
            f"验证点 {failed_vp_id} 在最近一轮验证中判定为 FAILED，"
            f"需要修复其根因后重新验证。",
        ]
        summary = str(vp_plan.get("actual_result_summary", "") or "").strip()
        if summary:
            lines.append(f"实际结果摘要：{summary}")
        if vp_plan.get("title"):
            lines.append(f"验证点标题：{vp_plan['title']}")
        paths = vp_plan.get("evidence_paths") or []
        if paths:
            lines.append("证据（用 Read 工具读取）：")
            lines.extend(f"  - {p}" for p in paths)
        command = str(vp_plan.get("test_command_summary", "") or "").strip()
        if command:
            lines.append(f"复现命令：{command}")
        return "\n".join(lines)

    def _infer_depends_on(self, seq: int) -> List[str]:
        """Return the list of task IDs this task must wait for.

        Tasks within the same round depend on the previous task in the
        same round (sequential chain). This protects against racing two
        repair tasks against the same ``verification_plan.json`` write.

        2026-09-16: the cross-round edge is gone. Round
        N's first task used to depend on a ``repair-r{round - 1}-99``
        sentinel standing for "the last task of the previous round".
        That id is never assigned by any code path — same-round ids are
        ``repair-r{round}-{seq:02d}`` starting at ``01`` — so the edge
        never resolved, and ``Agent._validate_dependencies`` hard-rejects
        the entire DAG (``"Task X depends on missing task Y"``) before a
        single task runs. Every plan with a ≥2 repair round died at load
        (an earlier plan, round 3).

        The intent it encoded — "do not start round N until round N-1's
        executor subprocess has exited" — cannot be expressed as a
        ``depends_on`` edge in the first place: repair rounds are
        generated by the verification orchestrator *after* the previous
        execution run ended, and a same-DAG edge cannot order against a
        subprocess that no longer exists. Ordering against a ``pending``
        task left behind by a killed run is a real concern, but the fix
        for that would be a *real* id, not a sentinel.

        2026-09-20: the edge is scoped to the batch. With ``seq_base``
        above 1 the previous id (``seq - 1``) belongs to an *earlier*
        batch, so linking to it would make this task wait on an
        unrelated, already-finished repair from days ago. Only ids this
        batch itself emitted are valid predecessors.
        """
        deps: List[str] = []
        if seq > self.seq_base:
            deps.append(f"repair-r{self.round}-{seq - 1:02d}")
        return deps


def _build_repair_contents_prompt(
    failed_vps: List[Dict[str, Any]],
    round_number: int,
    plan_dir: Path,
    project_dir: Path,
    previous_failure_feedback: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    repair_outcomes: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """Build the single LLM prompt for repair-content generation.

    The prompt is intentionally short — the on-disk
    ``verification_report.json`` and ``verification_plan.json``
    already carry every fact the LLM needs, and embedding that data
    inline avoids burning tokens re-deriving it from PRD context. The
    LLM's only job is to turn each failed VP's ``actual_result`` /
    ``evidence`` into a concrete ``title / description /
    acceptance_criteria`` triple.

    2026-09-12: when ``previous_failure_feedback``
    is non-empty, the prompt prepends a "previous attempts" section
    listing each prior round's actual_result + evidence for the same
    VP. This replaced an earlier "cap at 3 rounds" design: feeding the
    failure back gives the generating agent information to learn from,
    whereas a hard cap simply ends the chain without a hint.
    """
    parts = [
        f"Round {round_number}: generate one repair task per FAILED verification point.",
        "",
        f"Plan directory: {plan_dir}",
        f"Project directory: {project_dir}",
        "",
    ]
    if previous_failure_feedback:
        # Filter to VPs that still appear in this round's failures —
        # otherwise we'd show feedback for already-fixed VPs which
        # would just confuse the agent.
        current_vp_ids = {str(vp.get("id", "")) for vp in failed_vps}
        relevant_feedback = {
            vp_id: history
            for vp_id, history in previous_failure_feedback.items()
            if vp_id in current_vp_ids and history
        }
        if relevant_feedback:
            parts.append(
                "## ⚠️ 上次修复尝试未生效 — 必须尝试新方案"
            )
            parts.append("")
            parts.append(
                "以下 VP 在前几轮已经生成过 repair task 并由 executor 执行，"
                "但仍然失败。每个 VP 下方列出了每次失败时的实际结果和证据。"
                "请基于这些信息重新分析根本原因，**不要重复相同的方案**——"
                "上次那条路已经被验证无效。"
            )
            parts.append("")
            for vp_id, history in relevant_feedback.items():
                parts.append(f"### {vp_id} — 之前 {len(history)} 次失败")
                for h in history:
                    # 2026-09-14: history entries seeded from state.db's
                    # accumulated verdicts carry no round number (the
                    # column has none) — they use an ordinal ``label``
                    # ("既往失败 #2") instead of a fabricated round.
                    heading = h.get("label") or f"Round {h.get('round', '?')}"
                    parts.append(
                        f"- **{heading}** "
                        f"actual_result: "
                        f"{(h.get('actual_result') or '')[:600]}"
                    )
                    parts.append(
                        f"  evidence: "
                        f"{(h.get('evidence') or '')[:600]}"
                    )
                parts.append("")
    parts.append(f"共 {len(failed_vps)} 个失败 VP：")
    parts.append("")
    for i, vp in enumerate(failed_vps, start=1):
        parts.append(f"--- VP {i} ({vp.get('id')}) ---")
        parts.append(f"title: {vp.get('title', '')}")
        parts.append(f"priority: {vp.get('priority', 'medium')}")
        if vp.get("evidence_paths"):
            # Path-based shape (2026-09-10 plan). The long-form text is NOT
            # inlined: the previous shape truncated ``actual_result`` to
            # 1000 chars, which for a VP whose whole point is "which of the
            # 245 tests failed" threw away exactly the actionable part.
            # The agent Reads these files instead, so the prompt stays
            # bounded however large the captured output is.
            parts.append("evidence_paths (用 Read 工具读取):")
            for path in vp["evidence_paths"]:
                parts.append(f"  - {path}")
            if vp.get("actual_result_summary"):
                parts.append(
                    f"actual_result_summary: {vp['actual_result_summary']}"
                )
            if vp.get("evidence_summary"):
                parts.append(f"evidence_summary: {vp['evidence_summary']}")
            if vp.get("test_command_path"):
                parts.append(
                    f"test_command_path (用 Read 工具读取): "
                    f"{vp['test_command_path']}"
                )
            if vp.get("test_command_summary"):
                parts.append(
                    f"test_command_summary: {vp['test_command_summary']}"
                )
            if vp.get("expected_result_summary"):
                parts.append(
                    f"expected_result_summary: "
                    f"{vp['expected_result_summary']}"
                )
        else:
            # Legacy inlined shape — still supported so an older
            # ``verification_report.json`` reader (or a caller that has
            # not migrated yet) keeps producing a usable prompt.
            parts.append(f"actual_result: {vp.get('actual_result', '')[:1000]}")
            parts.append(f"evidence: {vp.get('evidence', '')[:1000]}")
            if vp.get("test_command"):
                parts.append(f"test_command (from plan): {vp.get('test_command')[:500]}")
            if vp.get("expected_result"):
                parts.append(f"expected_result (from plan): {vp.get('expected_result')[:500]}")
        # 2026-09-14:
        # what the repair task GENERATED FOR THIS VP last round actually
        # did when the executor ran it. The VRD only says "the VP failed
        # again"; this says "we already tried `repair-r4-03`, it ran X and
        # ended <status>". Without it the LLM rewrites the same failed
        # remedy with different wording.
        outcome = (repair_outcomes or {}).get(str(vp.get("id", "")))
        if isinstance(outcome, dict) and outcome.get("repair_task_id"):
            status = outcome.get("repair_status") or "?"
            parts.append(
                f"上轮为该 VP 生成的修复任务: "
                f"`{outcome['repair_task_id']}` (执行结果: {status})"
            )
            if outcome.get("repair_task_title"):
                parts.append(f"  任务标题: {outcome['repair_task_title']}")
            if outcome.get("repair_test_command"):
                parts.append(
                    f"  验证命令: {outcome['repair_test_command']}"
                )
            if outcome.get("repair_attempt"):
                parts.append(f"  执行尝试次数: {outcome['repair_attempt']}")
            if outcome.get("repair_failure_reason"):
                parts.append(
                    f"  失败原因: {outcome['repair_failure_reason']}"
                )
            parts.append(
                "  → 该修复已被验证为无效或未完成，请给出**不同的**修复思路。"
            )
        parts.append("")
    parts.append(
        "为每个失败 VP 生成一条修复任务（纯 JSON，不要 markdown 代码块标记）："
    )
    return "\n".join(parts)


class _RepairContentGeneratorMixin:
    """Mixin exposing :meth:`generate_repair_contents` on
    :class:`RepairTaskGenerator` without inheriting from a new base.

    Kept as a thin mixin so the public method lands next to the rest
    of :class:`RepairTaskGenerator`'s API surface, but its
    implementation is small enough to live in one place.
    """

    def generate_repair_contents(
        self,
        failed_vps: List[Dict[str, Any]],
        round_number: int,
        previous_failure_feedback: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        repair_outcomes: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Single LLM call producing repair content for each failed VP.

        Args:
            failed_vps: The list returned by
                :func:`verification.verification_report_reader.extract_failed_vps_from_report`.
                Each item carries ``id``, ``title``, ``priority``,
                ``actual_result``, ``evidence``, and optionally
                ``test_command`` / ``expected_result``.
            round_number: Current verification round number (1-based).
            previous_failure_feedback: 2026-09-12:
                ``{vp_id: [{round, actual_result, evidence}, ...]}``
                carrying the failure history of each VP across prior
                rounds. When a VP has been attempted before (the LLM
                generated a repair task for it but the executor's fix
                didn't take), the prompt includes those prior
                ``actual_result`` / ``evidence`` blocks verbatim with
                a "上次方案完全无效" instruction so the LLM is steered
                away from repeating the same approach. Without this
                feedback the LLM would just regenerate the same
                failing repair task each round.
            repair_outcomes: 2026-09-14: ``{vp_id:
                {repair_task_id, repair_task_title,
                repair_test_command, repair_status,
                repair_failure_reason, repair_attempt}}`` — what the
                repair task generated for this VP in a PREVIOUS round
                actually did when the executor ran it (read from
                state.db ``plan_tasks``; repair tasks are not in
                ``tasks.json``). Rendered under the VP so the LLM stops
                re-proposing a remedy that is already proven ineffective.

        Returns:
            A list of content dicts, one per failed VP, each carrying:

            * ``failed_vp_id`` — the VP ID this content addresses
            * ``title`` — short label of the fix
            * ``description`` — full failure → fix narrative
            * ``acceptance_criteria`` — TDD-style acceptance spec
            * ``priority_hint`` — optional hint, the assembler
              reconciles with the plan-side ``priority``
            * ``execution_group_hint`` — optional integer, the
              assembler passes it through

            Returns an empty list only when ``failed_vps`` is empty —
            i.e. there is genuinely nothing to repair.

            Raises:
                RepairGenerationError: the LLM call failed, or it
                    returned a shape with no usable content while
                    failures were pending. 2026-09-14 — this used to be
                    collapsed into ``[]``. The caller
                    (``check_cycle_conditions``) then treated "the
                    generator broke" as "no repair tasks available" and
                    recorded a terminal ``no_repair_tasks`` stop reason,
                    silently ending the chain on rounds that had real
                    failures.
        """
        if not failed_vps:
            return []
        failed_vp_ids = [
            str(vp.get("id")) for vp in failed_vps if vp.get("id")
        ]
        prompt = _build_repair_contents_prompt(
            failed_vps,
            round_number,
            self.plan_dir,
            self.project_dir,
            previous_failure_feedback=previous_failure_feedback,
            repair_outcomes=repair_outcomes,
        )
        try:
            response = self.coding_tool.query_json(
                prompt=prompt,
                system_instruction=REPAIR_CONTENT_SYSTEM_PROMPT,
            )
        except Exception as exc:  # noqa: BLE001 — re-raised as a typed error
            print(
                f"[Repair] generate_repair_contents LLM call failed "
                f"(round={round_number}): {exc}"
            )
            raise RepairGenerationError(
                f"repair-content LLM call failed for round "
                f"{round_number}: {exc}",
                round_number=round_number,
                failed_vp_ids=failed_vp_ids,
            ) from exc
        contents = _parse_repair_contents_response(response, failed_vps)
        if not contents:
            # The model answered, but with nothing we can turn into a
            # task. With >=1 failed VP in hand that is a generation
            # failure, not convergence: ``{"tasks": []}`` / a malformed
            # envelope here would otherwise strand the failures.
            raise RepairGenerationError(
                f"repair-content LLM returned no usable task for "
                f"{len(failed_vps)} failed VP(s) "
                f"({', '.join(failed_vp_ids) or 'no ids'}) in round "
                f"{round_number}",
                round_number=round_number,
                failed_vp_ids=failed_vp_ids,
            )
        return contents


def _parse_repair_contents_response(
    response: Any,
    failed_vps: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Normalise the LLM response into a content list.

    Defensive against:

    * non-dict responses (``None``, ``list``, ``str``) — returns ``[]``
    * missing ``tasks`` key — returns ``[]``
    * ``tasks`` items missing ``failed_vp_id`` — fills it from the
      position in the input list (positional fallback so a
      well-behaved LLM that forgets the field still produces usable
      tasks)
    * items missing any of ``title`` / ``description`` /
      ``acceptance_criteria`` — drops the item rather than emit a
      half-formed task that would mislead the assembler
    """
    if not isinstance(response, dict):
        return []
    raw_tasks = response.get("tasks")
    if not isinstance(raw_tasks, list):
        return []
    failed_by_id: Dict[str, Dict[str, Any]] = {
        str(vp.get("id", "")): vp for vp in failed_vps if vp.get("id")
    }
    parsed: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            continue
        failed_vp_id = str(item.get("failed_vp_id", "") or "")
        if not failed_vp_id and idx < len(failed_vps):
            # Positional fallback: the i-th task addresses the i-th
            # failed VP. Only applies when the LLM omits the field
            # entirely.
            failed_vp_id = str(failed_vps[idx].get("id", ""))
        if not failed_vp_id or failed_vp_id not in failed_by_id:
            # LLM hallucinated a VP ID we don't recognise — skip.
            continue
        title = str(item.get("title", "") or "").strip()
        description = str(item.get("description", "") or "").strip()
        criteria = str(item.get("acceptance_criteria", "") or "").strip()
        if not title or not description:
            # Half-formed task — the assembler has no useful content
            # to wrap, and emitting a row with an empty description
            # would mislead the executor. Drop it; the deterministic
            # pattern-match path can still emit a fallback task if
            # the orchestrator chooses to retry.
            continue
        parsed.append({
            "failed_vp_id": failed_vp_id,
            "title": title,
            "description": description,
            "acceptance_criteria": criteria,
            "priority_hint": str(item.get("priority_hint", "") or ""),
            "execution_group_hint": item.get("execution_group_hint", 0),
        })
    return parsed


# Attach the mixin method to RepairTaskGenerator without breaking
# the rest of the class. Done as a module-level assignment so
# existing tests that introspect ``RepairTaskGenerator.__dict__``
# see both the original methods and ``generate_repair_contents``.
RepairTaskGenerator.generate_repair_contents = (
    _RepairContentGeneratorMixin.generate_repair_contents
)

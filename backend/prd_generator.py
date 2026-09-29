"""
PRD Generator — CPEA-structured Product Requirements Document (JSON)
====================================================================

Generates a PRD from interview.json using CPEA framework, stored as JSON.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

from coding_tool import CodingTool
from framework.text import circled_list
from self_review import run_doc_self_review, write_self_review_report
from execution_logger import get_logger


log = logging.getLogger("prd_generator")

# File patterns to scan for code context
_CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".sh"}
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist",
    "build", ".pytest_cache", ".mypy_cache", ".tox",
}
_MAX_FILES = 15
_MAX_LINES_PER_FILE = 50
_MAX_TOTAL_CHARS = 12000

# VP-001: Required interview dimensions (PRD generation pre-validation).
# A non-empty answer in each is required before PRD generation can proceed.
REQUIRED_INTERVIEW_DIMENSIONS = (
    "background",
    "goals",
    "scope",
    "constraints",
    "acceptance",
)


PRD_SYSTEM_PROMPT_SOFTWARE = """你是一位资深产品经理。根据需求访谈结果和已有代码分析，生成结构化的 PRD 文档。

重要：直接输出 JSON 内容，不要输出任何对话、解释、确认或提问。

关键要求：如果提供了已有代码上下文，你必须基于已有实现来生成决策点，而不是凭空设计。已有代码中已实现的功能不需要作为决策点，只需对需要修改/新增的部分生成决策点。

PRD 以 JSON 格式输出，包含以下结构：
- title: 项目名称
- overview: 项目概述（简要描述目标和核心价值）
- constraints: 约束条件（业务约束、时间、资源、外部依赖等；架构选型相关约束如果必须写应该只描述事实，不展开为决策点）
- acceptance: 验收标准（明确的可执行验收条件，聚焦业务可观测的产物）
- decision_points: 决策点数组，每个决策点使用 CPEA 框架；**每个决策点必须填写 category 字段**

每个 decision_point 包含以下字段：
- title: 决策点标题（简短）
- context: 背景与约束（当前情况、已有条件、限制）
- problem: 需要解决的核心问题（从背景和约束中自然导出的问题）
- evidence: 选择方案的证据与理由（数据、经验、权衡分析等）
- action: 具体决策/行动（明确的方案）
- impact: 影响范围（该决策影响哪些模块/功能）
- alternatives: 备选方案列表（1-2 个）
- category: 决策点分类，**必须是以下 4 个需求类值之一**（PRD 阶段硬约束，arch/test_design 阶段不允许出现）

**decision_point.category 合法值（PRD review 会机械校验，不在白名单内会被标记需修订）**：
- "requirement"：用户功能需求、用户场景、用户旅程
- "scope_boundary"：范围边界（in/out）、与已有系统的关系
- "acceptance_criterion"：业务可验收标准（不包括性能数字、测试覆盖率、部署流水线）
- "business_rule"：业务领域规则、合规要求、假设与外部依赖

**禁止在 PRD 决策点中出现以下主题**（这些属于后续 arch / test_design 阶段，PRD 阶段不要越界）：
- 技术栈选型（语言/框架/关键库/版本号选型）→ arch 阶段
- 系统分层与模块划分（目录结构、组件拆分、责任分配）→ arch 阶段
- 数据模型与存储策略（数据库选型、表结构、索引、Schema）→ arch 阶段
- 关键接口与契约设计（API 端点形态、序列化格式、跨语言 FFI）→ arch 阶段
- CI/CD 流水线与部署架构（Dockerfile、K8s manifest、Stage 划分、回滚策略）→ 部署工程 / arch 阶段
- 测试策略、Mock 策略、覆盖率目标、压测场景 → test_design 阶段
- 错误处理模式、异常恢复策略、边界场景处理 → 实现细节 / test_design
- 性能基准与目标数字、负载指标 → test_design 阶段或非功能需求追踪

如果 PRD 阶段必须提及某个主题（例如某些技术栈约束在访谈时已确定），应将其放入顶层 "constraints" 字段作为事实陈述，而不是作为 decision_point；否则视为越界、PRD review 会要求重写。

CPEA 逻辑链要求：
- context 必须能自然导出 problem
- problem 必须能由 evidence 支撑
- evidence 必须能推导出 action
- 避免 context 和 problem 脱节

输出要求：
1. 必须是合法的 JSON 对象
2. decision_points 至少包含 3 个决策点
3. 每个字段使用中文
4. alternatives 是字符串数组
5. 不要输出 markdown 代码块标记，只输出纯 JSON"""

PRD_SYSTEM_PROMPT_SKILL = """你是一位资深产品架构师，专门设计 Claude Skill 编排方案。根据需求访谈结果和已有代码分析，生成结构化的 Skill PRD 文档。

重要：直接输出 JSON 内容，不要输出任何对话、解释、确认或提问。

关键要求：如果提供了已有代码上下文，你必须基于已有实现来生成决策点，而不是凭空设计。已有代码中已实现的功能不需要作为决策点，只需对需要修改/新增的部分生成决策点。

该产品是一个 Claude Skill（不是独立软件），PRD 以 JSON 格式输出，包含以下结构：
- title: Skill 名称
- overview: Skill 概述（描述 skill 解决什么问题、触发条件、核心价值）
- constraints: 约束条件（业务层面的依赖、上下文限制、外部接口要求；架构选型约束如果必须写应该只描述事实，不展开为决策点）
- acceptance: 验收标准（skill 能被正确触发、工作流完整、输出正确）
- decision_points: 决策点数组，每个决策点使用 CPEA 框架；**每个决策点必须填写 category 字段**

**decision_point.category 合法值（PRD review 会机械校验；Skill 阶段专用）**：
- "skill_flow_step"：skill 工作流步骤划分（哪些步骤由 Claude 直接处理 vs 辅助脚本）
- "skill_capability_reuse"：现有 skill 复用（如 notion-api、cc-cron、tushare 等的复用边界）
- "skill_trigger_condition"：触发词/触发场景/激活条件
- "skill_error_isolation"：单点失败隔离、上下文超限分批、重试策略

每个决策点必须围绕 "skill 编排" 的核心问题展开，例如：
- 执行模式：哪些步骤由 Claude 直接处理 vs 哪些需要辅助脚本？
- 能力复用：哪些现有 skill 可以被复用（如 notion-api、cc-cron、tushare）？
- 智能层：数据清洗/语义理解/质量判断由 Claude 处理还是外部 LLM？
- 数据流：输入（fetch 脚本输出）→ 处理（Claude 理解）→ 输出（其他 skill 写入）
- 错误处理：单点失败如何隔离？Claude 上下文超限如何分批？

每个决策点包含以下字段：
- title: 决策点标题（简短）
- context: 背景与约束
- problem: 需要解决的核心问题
- evidence: 选择方案的证据与理由
- action: 具体决策/行动
- impact: 影响范围（影响 skill 的哪些步骤/哪些依赖 skill）
- alternatives: 备选方案列表（1-2 个）
- category: 决策点分类（必须是以上 4 个 skill 类值之一）

**禁止在 PRD 决策点中出现以下主题**（这些属于后续 arch / test_design 阶段，不要越界）：
- 编程语言/框架/底层库的具体选型 → arch 阶段
- 数据库表结构、索引、Schema 设计 → arch 阶段
- 跨语言 FFI、序列化格式、网络协议栈 → arch 阶段
- CI/CD 流水线、自动化测试覆盖率目标 → 部署工程 / test_design 阶段
- 错误重试的具体实现机制（指数退避、断路器细节等）→ 实现细节 / arch 阶段
- 性能基准数字、负载测试场景 → test_design 阶段

CPEA 逻辑链要求：
- context 必须能自然导出 problem
- problem 必须能由 evidence 支撑
- evidence 必须能推导出 action
- 避免 context 和 problem 脱节

输出要求：
1. 必须是合法的 JSON 对象
2. decision_points 至少包含 3 个决策点
3. 每个字段使用中文
4. alternatives 是字符串数组
5. 不要输出 markdown 代码块标记，只输出纯 JSON

关键提醒：
- 决策点必须体现 "skill 编排" 的思维（复用、组合、Claude 作为智能层）
- 不要生成传统软件架构的决策点（如"数据库选型""API 框架选择"）
- 如果涉及数据存储，优先考虑复用现有 skill（如 notion-api）而非自建存储"""


# Prompt registry keyed by product form
PRD_PROMPTS = {
    "software": PRD_SYSTEM_PROMPT_SOFTWARE,
    "skill": PRD_SYSTEM_PROMPT_SKILL,
    "agent": PRD_SYSTEM_PROMPT_SOFTWARE,  # Fallback to software prompt for now
    "script": PRD_SYSTEM_PROMPT_SOFTWARE,  # Fallback to software prompt for now
    "library": PRD_SYSTEM_PROMPT_SOFTWARE,  # Fallback to software prompt for now
}


def _to_str(val) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False, indent=2)
    return str(val)


class PRDGenerator:
    """Generates CPEA-structured PRD JSON from interview results."""

    @staticmethod
    def _is_dimension_empty(value) -> bool:
        """Return True if an interview dimension value is empty."""
        if value is None:
            return True
        if isinstance(value, str):
            return value.strip() == ""
        if isinstance(value, (list, dict, tuple, set)):
            return len(value) == 0
        return False

    @classmethod
    def validate_interview(cls, interview) -> Tuple[bool, List[str]]:
        """Validate interview.json before PRD generation.

        Returns ``(is_invalid, missing_dimensions)``. ``is_invalid`` is
        True when ``interview`` is empty (``{}``) or any of the five
        required dimensions (background/goals/scope/constraints/acceptance)
        is missing or empty.

        ``missing_dimensions`` enumerates the names of every required
        dimension that is missing or empty. When ``interview`` is
        completely empty, all five dimensions are reported missing so
        the placeholder PRD is informative.
        """
        if not isinstance(interview, dict) or not interview:
            return True, list(REQUIRED_INTERVIEW_DIMENSIONS)

        dimensions = interview.get("dimensions")
        if not isinstance(dimensions, dict):
            return True, list(REQUIRED_INTERVIEW_DIMENSIONS)

        missing: List[str] = []
        for dim in REQUIRED_INTERVIEW_DIMENSIONS:
            if dim not in dimensions:
                missing.append(dim)
                continue
            if cls._is_dimension_empty(dimensions.get(dim)):
                missing.append(dim)

        return bool(missing), missing

    @staticmethod
    def build_placeholder_prd(interview, missing_dimensions) -> dict:
        """Build a placeholder PRD emitted when interview is empty/invalid.

        The placeholder satisfies two contracts:

        1. ``decision_points`` carries only meta-decisions about the
           placeholder mechanism itself (reset / prohibit-fake /
           release-criteria / user-notification). No business
           decisions are invented from the empty interview.
        2. ``_placeholder`` is True and ``_missing_dimensions`` lists
           the offending fields so downstream phases can detect the
           placeholder instead of treating it as a real PRD.
        """
        missing_text = "、".join(missing_dimensions) if missing_dimensions else ""
        if missing_text:
            overview_text = (
                "interview.json 为空或缺失必要维度（"
                + missing_text
                + "），PRD 生成已拒绝进入决策点生成阶段并输出占位 PRD。"
                "请先补全 interview 维度后重新触发 PRD 生成。"
            )
        else:
            overview_text = (
                "interview.json 为空，PRD 生成已拒绝进入决策点生成阶段并输出占位 PRD。"
            )

        decision_points = []

        return {
            "title": "未命名项目 (Placeholder PRD)",
            "overview": overview_text,
            "constraints": "",
            "acceptance": "",
            "decision_points": decision_points,
            "_placeholder": True,
            "_missing_dimensions": list(missing_dimensions),
            "_interview_keys": sorted(interview.keys()) if isinstance(interview, dict) else [],
        }

    def __init__(
        self,
        coding_tool: CodingTool,
        plan_dir: Path,
        project_dir: Optional[Path] = None,
        self_review_enabled: bool = True,
    ):
        self.coding_tool = coding_tool
        self.plan_dir = plan_dir
        self.project_dir = project_dir
        self.self_review_enabled = self_review_enabled
        self.interview_file = plan_dir / "interview.json"
        self.prd_file = plan_dir / "prd.json"
        self.prd_md_file = plan_dir / "prd.md"

    @staticmethod
    def _scan_existing_code(project_dir: Path, requirement: str = "") -> str:
        """Scan existing codebase and return a summary for PRD context.

        Reads key source files (up to _MAX_FILES, _MAX_LINES_PER_FILE each)
        to provide the LLM with ground truth about what already exists.
        """
        if not project_dir or not project_dir.exists():
            return ""

        # Try to find relevant subdirectory from requirement text
        scan_dir = project_dir
        req_lower = requirement.lower()
        # If the requirement mentions a subdirectory, focus on it.
        #
        # Derived from the tree, not a hardcoded list. The previous version
        # enumerated three specific directory names taken from the author's
        # own projects — which both leaked those names into a public repo and
        # meant the behaviour silently worked only for those three.
        try:
            for sub in sorted(p for p in project_dir.iterdir() if p.is_dir()):
                if sub.name.lower() in req_lower:
                    scan_dir = sub
                    break
        except OSError:
            pass

        snippets: List[str] = []
        total_chars = 0

        for root, dirs, files in os.walk(scan_dir):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1]
                if ext not in _CODE_EXTENSIONS:
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, project_dir)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                except Exception:
                    continue

                # Take first N lines
                chunk = "".join(lines[:_MAX_LINES_PER_FILE])
                entry = f"### {rel}\n```\n{chunk}\n```\n"

                if total_chars + len(entry) > _MAX_TOTAL_CHARS:
                    break
                snippets.append(entry)
                total_chars += len(entry)

                if len(snippets) >= _MAX_FILES:
                    break
            if len(snippets) >= _MAX_FILES:
                break

        if not snippets:
            return ""

        return (
            "## 已有代码分析\n\n"
            f"以下是项目 `{project_dir.name}` 中的关键源文件摘要。\n"
            "请基于这些已有实现来生成决策点——已实现的功能不要重复设计，"
            "只需关注需要修改或新增的部分。\n\n"
            + "\n".join(snippets)
        )

    def generate(self) -> dict:
        """Generate PRD JSON from interview.json. Returns PRD dict.

        VP-001: pre-validate ``interview.json``. If the interview is
        empty or any of the five required dimensions is missing/empty,
        emit a placeholder PRD without invoking the LLM. The placeholder
        is written to both ``prd.json`` and ``prd.md`` so downstream
        phases can detect ``_placeholder`` and refuse to consume it.
        """
        with open(self.interview_file, "r") as f:
            interview = json.load(f)

        # VP-001: empty/missing-dimension pre-validation
        is_invalid, missing = self.validate_interview(interview)
        if is_invalid:
            placeholder = self.build_placeholder_prd(interview, missing)
            self.plan_dir.mkdir(parents=True, exist_ok=True)
            with open(self.prd_file, "w") as f:
                json.dump(placeholder, f, ensure_ascii=False, indent=2)
            prd_markdown = self.prd_to_markdown(placeholder)
            with open(self.prd_md_file, "w", encoding="utf-8") as f:
                f.write(prd_markdown)
            log.warning(
                "prd_generator skipped decision-point generation: "
                "interview.json is empty or missing dimensions %s; "
                "placeholder PRD written to %s",
                missing,
                self.prd_file,
            )
            return placeholder

        dimensions = interview.get("dimensions", {})
        # `product_form` may be stored either as a string (e.g.
        # "SOFTWARE") or as a dict ({form: "software", ...}).  Handle
        # both shapes — earlier versions wrote the bare string and the
        # `.get("form", ...)` call below would AttributeError.
        pf = interview.get("product_form", "software")
        product_form = pf.get("form", "software") if isinstance(pf, dict) else pf
        requirement = interview.get("chat_history", [{}])[0].get("content", "")
        requirement_text = str(requirement) if requirement else ""

        prompt = f"请根据以下需求访谈结果生成完整的 PRD 文档：\n\n{json.dumps(dimensions, ensure_ascii=False, indent=2)}"

        # When project_dir is available, instruct the agent to search the codebase
        # using its native tools (Grep, Glob, Read) rather than static file scanning.
        # The coding_tool runs Claude Code with bypassPermissions, so tool access is available.
        if self.project_dir and self.project_dir.exists():
            prompt += f"""

## 代码库探索指令

当前工作目录已设置为项目根目录：`{self.project_dir}`

在生成 PRD 之前，请先主动使用你的工具探索现有代码库：
1. 用 Glob 工具列出项目结构（如 `**/*.py`、`**/*.ts` 等）
2. 用 Grep 工具搜索与需求直接相关的关键词、函数名、类名
3. 用 Read 工具读取最相关的源文件（重点关注入口文件、配置和核心逻辑）
4. 基于探索结果，判断哪些功能已实现、哪些需要新增或修改

**重要**：决策点只需覆盖需要变更的部分，已实现的功能不要作为决策点重复设计。"""

        system_prompt = PRD_PROMPTS.get(product_form, PRD_PROMPTS["software"])
        # Append the JSON output contract to the system prompt so
        # the LLM sees "your reply must be a JSON object" as the
        # very last instruction (smoke v5 surfaced silent empty
        # replies — this is the lowest-cost mitigation).
        from llm_prompts import append_json_output_contract
        system_prompt = append_json_output_contract(system_prompt)

        prd_data = self.coding_tool.query_json(
            prompt=prompt,
            system_instruction=system_prompt,
        )

        # Defensive: some providers occasionally return a raw string
        # instead of a parsed dict (e.g. when the model emits prose
        # around the JSON and the extractor falls back to the raw
        # response).  Try to salvage by parsing the FIRST balanced
        # JSON object from the string via raw_decode (which stops at
        # the end of the first complete value); otherwise re-raise so
        # the caller sees a clear error.
        if isinstance(prd_data, str):
            import json as _json
            decoder = _json.JSONDecoder()
            try:
                prd_data, _ = decoder.raw_decode(prd_data)
            except _json.JSONDecodeError:
                raise ValueError(
                    f"PRD generator expected a JSON dict, got str (no parseable JSON): {prd_data[:200]!r}"
                )

        # Ensure decision_points have index fields
        for i, dp in enumerate(prd_data.get("decision_points", [])):
            dp["index"] = i

        self.plan_dir.mkdir(parents=True, exist_ok=True)
        with open(self.prd_file, "w") as f:
            json.dump(prd_data, f, ensure_ascii=False, indent=2)

        # -- DP1: mandatory post-emit self-review ------------------
        # The first pass emitted ``prd.json`` (canonical source of
        # truth). Render it to markdown and run the shared second
        # LLM pass — the agent decides whether the draft needs
        # repair. Failures from the mandatory pass abort generation
        # so we never promote a draft the second LLM has not seen.
        prd_markdown = self.prd_to_markdown(prd_data)
        content_to_write = prd_markdown
        report: Optional[Dict[str, Any]] = None

        if self.self_review_enabled:
            try:
                report = run_doc_self_review(
                    doc_content=prd_markdown,
                    doc_type="prd",
                    coding_tool=self.coding_tool,
                )
            except Exception as exc:  # noqa: BLE001 — mandatory path
                log.warning(
                    "prd_generator self-review raised %s: %s; "
                    "aborting generation so the unaudited draft is "
                    "not promoted",
                    type(exc).__name__,
                    exc,
                )
                raise

            # Save the first-pass prd.md as a baseline before
            # self_review. The self_review rewrite (if any) is the
            # final version the user reviews — same identity model
            # as arch_generator.
            try:
                baseline_file = self.plan_dir / "prd.original.md"
                if not baseline_file.exists():
                    with open(baseline_file, "w", encoding="utf-8") as f:
                        f.write(prd_markdown)
            except OSError:
                pass

            # The self_review rewrite is the version the user sees.
            # We keep writing to self.prd_md_file (the canonical
            # filename the rest of the pipeline reads).
            fixed_content = report.get("fixed_content") or ""
            rewrote = bool(report.get("rewrote"))
            succeeded = bool(report.get("succeeded"))
            if rewrote and succeeded and fixed_content.strip():
                content_to_write = fixed_content
            # else: keep original — self_review effectively failed.
            # The audit report already carries the error reason.

            write_self_review_report(self.plan_dir, "prd", report)
            logger = get_logger(plan_id=self.plan_dir.name)
            if logger is not None:
                logger.info(
                    "prd_self_review",
                    f"PRD self-review produced "
                    f"{len(report.get('findings') or [])} finding(s); "
                    f"rewrote={bool(report.get('rewrote'))}, "
                    f"succeeded={bool(report.get('succeeded'))}",
                    phase="prd_generation",
                    data={
                        "doc_type": "prd",
                        "findings_count": len(
                            report.get("findings") or []
                        ),
                        "severity_high_count": report.get(
                            "severity_high_count", 0
                        ),
                        "rewrote": bool(report.get("rewrote")),
                        "succeeded": bool(report.get("succeeded")),
                    },
                )

        # prd.md is rewritten from ``content_to_write`` (which is
        # either the original first-pass draft or the second-pass
        # self_review rewrite if it succeeded).
        with open(self.prd_md_file, "w", encoding="utf-8") as f:
            f.write(content_to_write)

        return prd_data

    def load_prd(self) -> Optional[dict]:
        """Load existing PRD JSON if available."""
        if self.prd_file.exists():
            with open(self.prd_file, "r") as f:
                return json.load(f)
        return None

    @staticmethod
    def decision_point_to_markdown(dp: dict) -> str:
        """Render a decision point as markdown text for AI consumption.

        Supports both legacy schema (context/problem/evidence/action/impact/alternatives)
        and current schema (topic/context/problem/evaluation/action).
        """
        title = dp.get("title") or dp.get("topic", "")
        lines = [
            f"### 决策点 {dp.get('index', 0) + 1}: {title}",
            "",
            f"**[C] 背景：** {dp.get('context', '')}",
            f"**[P] 问题：** {dp.get('problem', '')}",
        ]

        # Evidence may be a top-level field or nested under evaluation.rationale
        evidence = dp.get("evidence")
        if not evidence:
            evaluation = dp.get("evaluation", {})
            evidence = evaluation.get("rationale") or _to_str(evaluation)
        lines.append(f"**[E] 证据：** {_to_str(evidence)}")

        lines.append(f"**[A] 行动：** {dp.get('action', '')}")
        lines.append("")

        impact = dp.get("impact", "")
        if impact:
            lines.append(f"影响范围：{impact}")

        # Alternatives may be top-level or nested under evaluation.options
        alts = dp.get("alternatives", [])
        if not alts:
            evaluation = dp.get("evaluation", {})
            options = evaluation.get("options", [])
            alts = [opt.get("name", _to_str(opt)) for opt in options]
        if alts:
            lines.append(f"备选方案：{circled_list(alts)}")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def prd_to_markdown(prd_data: dict) -> str:
        """Render full PRD JSON as markdown text for AI consumption.

        Supports both the legacy schema (overview/decision_points/constraints)
        and the current schema produced by the generator
        (background/goals/scope/decisions/acceptance).
        """
        lines = [
            f"# PRD — {prd_data.get('title', '未命名项目')}",
            "",
        ]

        # Legacy schema support
        if "overview" in prd_data:
            lines.extend([
                "## 概述",
                _to_str(prd_data.get("overview", "")),
                "",
            ])

        # Current schema: structured background / goals / scope
        background = prd_data.get("background")
        if background:
            lines.append("## 背景")
            if isinstance(background, dict):
                for k, v in background.items():
                    lines.append(f"### {k}")
                    lines.append(_to_str(v))
                    lines.append("")
            else:
                lines.append(_to_str(background))
                lines.append("")

        goals = prd_data.get("goals")
        if goals:
            lines.append("## 目标")
            if isinstance(goals, dict):
                for k, v in goals.items():
                    lines.append(f"### {k}")
                    lines.append(_to_str(v))
                    lines.append("")
            else:
                lines.append(_to_str(goals))
                lines.append("")

        scope = prd_data.get("scope")
        if scope:
            lines.append("## 范围")
            if isinstance(scope, dict):
                for k, v in scope.items():
                    lines.append(f"### {k}")
                    lines.append(_to_str(v))
                    lines.append("")
            else:
                lines.append(_to_str(scope))
                lines.append("")

        # Decision points: current schema uses "decisions", legacy uses "decision_points"
        decisions = prd_data.get("decisions") or prd_data.get("decision_points", [])
        if decisions:
            lines.append("## 决策点列表")
            lines.append("")
            for dp in decisions:
                lines.append(PRDGenerator.decision_point_to_markdown(dp))

        # Constraints / acceptance
        constraints = prd_data.get("constraints")
        if constraints:
            lines.append("## 约束条件")
            if isinstance(constraints, dict):
                for k, v in constraints.items():
                    lines.append(f"### {k}")
                    lines.append(_to_str(v))
                    lines.append("")
            else:
                lines.append(_to_str(constraints))
                lines.append("")

        acceptance = prd_data.get("acceptance")
        if acceptance:
            lines.append("## 验收标准")
            lines.append(_to_str(acceptance))
            lines.append("")

        return "\n".join(lines)

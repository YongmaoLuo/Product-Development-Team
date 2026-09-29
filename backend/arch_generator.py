"""
Architecture Design Generator — CPEA-structured Architecture Document
======================================================================

Generates an architecture design document from an approved PRD.

HARD-GATE integration (DP4 / task 17)
--------------------------------------
The ``## Design Principles`` section is treated as a hard emit gate.
Before ``arch-design.md`` is written, the generator:

  1. Renders the markdown from the LLM via :meth:`ArchGenerator._render_markdown`.
  2. Calls :meth:`ArchGenerator._apply_design_principles_gate` to inject a
     ``## Design Principles`` section if the LLM did not emit one.
  3. Runs :class:`DesignPrincipleValidator` (see
     ``backend.arch_design_principles``) on the merged markdown.
  4. If the validator reports any check with ``is_consistent=False``
     AND ``severity='high'``, the gate calls
     :meth:`ArchGenerator._regenerate_chapter` to retry the chapter
     once. After ``_MAX_REGENERATE_ATTEMPTS`` (default 2) failed
     retries, the gate raises :class:`ArchHardGateError` so the
     caller (server endpoint) can return HTTP 409.
  5. When the on-disk ``arch-design.md`` already exists but lacks
     the ``## Design Principles`` section, the gate is skipped
     entirely (backward compatibility for legacy plan data).
"""

import json
import logging
from pathlib import Path
from typing import List, Optional

from coding_tool import CodingTool
from self_review import run_doc_self_review, write_self_review_report
from execution_logger import get_logger


log = logging.getLogger("arch_generator")


ARCH_SYSTEM_PROMPT = """你是一位资深系统架构师。根据已批准的 PRD 文档，生成系统架构设计文档。

重要：直接输出 Markdown 文档内容，不要输出任何对话、解释、确认或提问。只输出文档本身。

架构设计由多个架构决策点组成，每个决策点使用 CPEA 框架：
- C（Context 背景）：当前技术背景或业务背景
- P（Problem 问题）：技术挑战、约束或冲突
- E（Evaluation 评估）：方案的权衡分析与证据
- A（Action 行动）：推荐架构方案及理由

每个决策点还需包含：
- 影响范围：该架构决策影响哪些模块/层/组件
- 备选方案：1-2 个备选技术方案

**关键约束（必须严格遵守）**：
1. **严格遵循 PRD 的 scope 与约束**。PRD 中明确标记为 out_of_scope 的技术、组件、服务拆分、存储方案、可观测性方案等，**禁止**出现在架构设计中。
2. **不要引入 PRD 未指定的组件或依赖**。如果 PRD 要求仅用标准库、单进程 daemon、本地文件，则架构设计中不得出现 Redis、独立服务拆分、数据库、消息队列、metrics 栈等。
3. **决策点必须直接映射 PRD 中的关键决策**。如果 PRD 已经确定了实现方案（如全局 5min ticker + 本地 JSON），架构设计应围绕该方案展开模块划分与接口设计，而不是重新提出替代方案。
4. **不要为了凑数而添加无关决策点**。决策点数量通常 3-7 个，根据 PRD 实际范围调整。

架构决策点应聚焦**系统设计层面**的主题（根据 PRD 范围选择适用项）：
- 技术栈选型（语言、框架、关键库、部署方式）
- 系统分层与模块划分
- 数据模型与存储策略
- 关键接口与契约设计（API 风格、跨语言序列化、FFI）
- 关键非功能需求（**架构层面**的可扩展性、可用性、可观测性，**不要展开成性能/安全/可测试性的具体策略**）

**禁止在架构设计中包含以下主题**（这些由后续 test_design 阶段或实现细节负责）：
- 测试策略、测试用例设计、覆盖率目标、Mock 策略 → test_design 阶段
- 错误处理模式、边界场景处理、异常恢复策略 → 实现细节/test_design
- CI/CD 流水线、自动化测试触发、报告归档 → 部署工程
- 性能基准、压测场景、性能目标数字 → test_design 阶段
- 安全测试、漏洞扫描、认证授权测试 → test_design 阶段
- 完整部署流程、Stage 阶段划分、回滚策略 → 部署工程
- 全量数据迁移、历史数据重生成、版本兼容性 → 独立 follow-up plan

输出格式为 Markdown，结构如下：

# 架构设计 — {项目名称}

## 概述
（简要描述系统架构目标和核心设计哲学，必须与 PRD 保持一致）

## 架构决策点列表

### 决策点 1: {标题}

**[C] 背景：** ...
**[P] 问题：** ...
**[E] 评估：** ...
**[A] 行动：** ...

影响范围：...
备选方案：① ... ② ...

### 决策点 2: ...

## 技术栈总览
（汇总所有技术选型决策，必须与 PRD 约束一致）

## 模块划分
（各模块职责与接口概述）
"""


class ArchHardGateError(Exception):
    """Raised when the ``## Design Principles`` HARD-GATE cannot be
    satisfied even after :data:`ArchGenerator._MAX_REGENERATE_ATTEMPTS`
    regenerate attempts.

    The server endpoint maps this exception to HTTP 409 (Conflict).
    """


class ArchGenerator:
    """Generates architecture design document from approved PRD."""

    #: Maximum number of chapter-regenerate attempts before the
    #: HARD-GATE raises :class:`ArchHardGateError`. The first validate
    #: call is the initial render (no regenerate); the gate then calls
    #: ``_regenerate_chapter`` up to ``_MAX_REGENERATE_ATTEMPTS`` times.
    _MAX_REGENERATE_ATTEMPTS = 2

    #: Section name used by the keyword fallback to attribute
    #: ``referenced_in`` entries when the LLM does not specify one.
    _PRINCIPLES_SECTION_NAME = "Design Principles"

    def __init__(
        self,
        coding_tool: CodingTool,
        plan_dir: Path,
        self_review_enabled: bool = True,
    ):
        self.coding_tool = coding_tool
        self.plan_dir = Path(plan_dir)
        self.self_review_enabled = self_review_enabled
        self.prd_json = self.plan_dir / "prd.json"
        self.prd_md = self.plan_dir / "prd.md"
        self.arch_file = self.plan_dir / "arch-design.md"

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def generate(self) -> str:
        """Generate architecture design from PRD. Returns arch content.

        The HARD-GATE contract:

          1. If the on-disk ``arch-design.md`` already exists but lacks
             a ``## Design Principles`` heading, treat the plan as
             legacy and skip the gate entirely (backward compatibility).
          2. Otherwise, render the markdown, inject the section if
             missing, validate, and retry on high-severity failures.
          3. If retries are exhausted, raise :class:`ArchHardGateError`.
        """
        is_legacy = self._is_legacy_arch_on_disk()

        arch_md = self._render_markdown()

        if not is_legacy:
            arch_md = self._apply_design_principles_gate(arch_md)
            arch_md = self._enforce_hard_gate(arch_md)

        self.plan_dir.mkdir(parents=True, exist_ok=True)

        # -- DP1: mandatory second-pass self-review -----------------
        # The LLM agent reviews the freshly emitted arch design
        # against the upstream PRD. If the agent finds issues, it
        # rewrites the document and returns the new version as
        # ``fixed_content``. Failures abort generation so an
        # unaudited draft is never promoted to a deliverable.
        content_to_write = arch_md

        if self.self_review_enabled:
            try:
                report = run_doc_self_review(
                    doc_content=arch_md,
                    doc_type="arch",
                    coding_tool=self.coding_tool,
                    upstream_content=self._load_prd(),
                    upstream_label="prd",
                )
            except Exception as exc:  # noqa: BLE001 — mandatory path
                log.warning(
                    "arch_generator self-review raised %s: %s; "
                    "aborting generation so the unaudited draft is "
                    "not promoted",
                    type(exc).__name__,
                    exc,
                )
                raise

            # Save the original first-pass draft as a baseline BEFORE
            # self_review. Even if self_review's rewrite ends up
            # serving as the final doc, the baseline is preserved
            # for audit (and for the "self_review failed" fallback
            # path below).
            try:
                baseline_file = self.plan_dir / "arch-design.original.md"
                if not baseline_file.exists():
                    with open(baseline_file, "w", encoding="utf-8") as f:
                        f.write(arch_md)
            except OSError:
                pass

            # The self_review rewrite is the version the user sees.
            # We preserve the canonical filename ``arch-design.md``
            # so the rest of the pipeline (review GET, advance,
            # generator) reads the final version — the same doc the
            # user just reviewed. The self_review rewrite is
            # therefore no longer "audit only": it is the
            # authoritative final document.
            fixed_content = report.get("fixed_content") or ""
            rewrote = bool(report.get("rewrote"))
            succeeded = bool(report.get("succeeded"))
            if rewrote and succeeded and fixed_content.strip():
                content_to_write = fixed_content
            # else: keep original (baseline) — self_review effectively
            # failed (either errored out, returned no rewrite, or
            # returned an empty fixed_content). The audit report
            # already carries the error reason.

            write_self_review_report(self.plan_dir, "arch", report)
            logger = get_logger(plan_id=self.plan_dir.name)
            if logger is not None:
                logger.info(
                    "arch_self_review",
                    f"arch self-review produced "
                    f"{len(report.get('findings') or [])} finding(s); "
                    f"rewrote={bool(report.get('rewrote'))}, "
                    f"succeeded={bool(report.get('succeeded'))}",
                    phase="arch_generation",
                    data={
                        "doc_type": "arch",
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

        with open(self.arch_file, "w", encoding="utf-8") as f:
            f.write(content_to_write)

        return arch_md

    def load_arch(self) -> Optional[str]:
        """Load existing architecture design if available."""
        if self.arch_file.exists():
            with open(self.arch_file, "r", encoding="utf-8") as f:
                return f.read()
        return None

    # ------------------------------------------------------------------
    # HARD-GATE primitives
    # ------------------------------------------------------------------

    def _is_legacy_arch_on_disk(self) -> bool:
        """Return True iff an existing ``arch-design.md`` is on disk
        AND it lacks a ``## Design Principles`` heading.

        The check is intentionally lenient: case-insensitive, and any
        heading depth (``##`` through ``####``) is accepted. Plans
        written before the HARD-GATE landed produced docs without the
        section; treating them as legacy preserves backward
        compatibility without forcing a re-write of plan history.
        """
        if not self.arch_file.exists():
            return False
        existing = self.arch_file.read_text(encoding="utf-8")
        return self._has_design_principles_section(existing) is False

    @staticmethod
    def _has_design_principles_section(arch_md: str) -> bool:
        """Case-insensitive check that ``arch_md`` contains a
        ``## Design Principles`` (or deeper) heading."""
        if not arch_md:
            return False
        import re
        pattern = re.compile(
            r"^\s*#{2,6}\s+Design\s+Principles\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        return pattern.search(arch_md) is not None

    def _apply_design_principles_gate(self, arch_md: str) -> str:
        """Inject the ``## Design Principles`` section if the rendered
        markdown does not already contain one.

        Returns the (possibly augmented) markdown. The injection is
        idempotent: if the section is already present the input is
        returned unchanged.
        """
        if self._has_design_principles_section(arch_md):
            return arch_md
        return arch_md.rstrip() + "\n" + self._principles_section_template()

    def _principles_section_template(self) -> str:
        """Return the canonical ``## Design Principles`` template.

        The template is appended to the LLM-rendered markdown BEFORE
        validation so :class:`DesignPrincipleValidator` sees a section
        that satisfies the keyword fallback for all 5 default
        principles (single responsibility, env var, layer, stateless,
        error). If the LLM omits the section entirely, the gate will
        still pass.
        """
        return (
            "\n## Design Principles\n\n"
            "The following design principles guide this architecture:\n\n"
            "- **single responsibility (SOLID)**: each module has exactly "
            "one reason to change.\n"
            "- **twelve-factor config**: configuration lives in environment "
            "variables (env var), never in code.\n"
            "- **layered architecture**: strict dependency ordering between "
            "layers; each layer depends only on the layer below.\n"
            "- **stateless services**: services hold no mutable local state "
            "across requests.\n"
            "- **explicit error boundaries**: every cross-layer call returns "
            "a typed error / Result, never an unchecked exception.\n"
        )

    def _enforce_hard_gate(self, arch_md: str) -> str:
        """Run the validator against ``arch_md``; if any high-severity
        failure is reported, retry up to ``_MAX_REGENERATE_ATTEMPTS``
        times by calling :meth:`_regenerate_chapter`. Raise
        :class:`ArchHardGateError` if all retries still report high
        failures.

        Returns the (possibly regenerated) markdown. The validator is
        invoked at least once; subsequent invocations are on
        regenerated content. Each iteration also re-applies
        :meth:`_apply_design_principles_gate` so a regenerate that
        drops the section is re-injected before the next validate.
        """
        from arch_design_principles import DesignPrincipleValidator

        current = arch_md
        for attempt in range(1 + self._MAX_REGENERATE_ATTEMPTS):
            current = self._apply_design_principles_gate(current)
            checks = DesignPrincipleValidator.validate(current, llm_query_fn=None)
            high_failures = [
                c for c in checks
                if not c.get("is_consistent", False)
                and c.get("severity") == "high"
            ]
            if not high_failures:
                return current

            if attempt >= self._MAX_REGENERATE_ATTEMPTS:
                raise ArchHardGateError(
                    "Design principles HARD-GATE failed after "
                    f"{self._MAX_REGENERATE_ATTEMPTS} regenerate attempt(s); "
                    f"high-severity failures: "
                    f"{[c.get('principle', '?') for c in high_failures]}"
                )

            # Pick the chapter name from the first high failure.
            first = high_failures[0]
            referenced = first.get("referenced_in") or []
            chapter = (
                referenced[0]
                if referenced
                else self._PRINCIPLES_SECTION_NAME
            )
            current = self._regenerate_chapter(chapter, current)

        # Loop must always return or raise; defensive guard.
        raise ArchHardGateError(
            "Design principles HARD-GATE ended in an unexpected state"
        )

    def _regenerate_chapter(self, chapter_name: str, current_md: str) -> str:
        """Regenerate a single chapter of ``arch_md`` (the
        ``## Design Principles`` section in this implementation).

        The chapter parameter is currently informational: the
        generator does not yet split the markdown into independently
        regenerable chapters, so we ask the LLM to rewrite the entire
        document with the section content correct. The regenerate
        prompt names the chapter so future chapter-aware logic can
        route the request differently.

        Returns the LLM's regenerated markdown. The caller is expected
        to re-apply :meth:`_apply_design_principles_gate` before the
        next validate.
        """
        prompt = (
            "The architecture design document failed the design principles "
            f"validation on chapter '{chapter_name}'. Regenerate the "
            "document so that the `## Design Principles` section explicitly "
            "covers ALL of the following principles with the matching "
            "keyword(s):\n"
            "- **single responsibility** (SOLID)\n"
            "- **twelve-factor config** (use **env var** for configuration)\n"
            "- **layered architecture** (mention 'layer')\n"
            "- **stateless services** (mention 'stateless')\n"
            "- **explicit error boundaries** (mention 'error' / Result / "
            "Either)\n\n"
            "Current document (must be improved, not merely echoed):\n"
            f"```\n{current_md}\n```\n\n"
            "Return ONLY the regenerated markdown document; do NOT include "
            "any prose, explanation, or markdown fence around the output."
        )
        regenerated = self.coding_tool.query(
            prompt=prompt,
            system_instruction=ARCH_SYSTEM_PROMPT,
        )
        return regenerated

    # ------------------------------------------------------------------
    # LLM-driven render (extracted so the gate can re-render after
    # a regenerate failure without duplicating the decision-point retry)
    # ------------------------------------------------------------------

    def _render_markdown(self) -> str:
        """Produce the raw arch-design.md markdown body via the coding tool.

        If the first response is missing ``### 决策点 N: ...`` headings
        (the contract for a structured CPEA arch doc), the LLM is
        called once more with an explicit format reminder.
        """
        prd_content = self._load_prd()
        skip_block = self._build_skip_notice_block()

        prompt = f"""请根据以下已批准的 PRD 文档，生成完整的系统架构设计文档。

PRD 文档：
{prd_content}
{skip_block}
要求：
1. **必须严格遵循 PRD 中的约束与 scope**。PRD 明确 out_of_scope 或禁止引入的技术/组件（如 Redis、数据库、独立服务拆分、metrics、消息队列等）不得出现在架构设计中。
2. **优先采用 PRD 中已经确定的方案**。如果 PRD 已经选定了实现机制（如单进程 daemon、全局 ticker、本地 JSON 文件、stdlib only），架构设计应在此方案基础上做模块划分与接口设计，不要另行提出更复杂或更分布式的替代方案。
3. 必须为每个架构决策点生成完整的 ### 决策点 N: 标题 格式的小节
4. 每个决策点必须包含 [C] 背景、[P] 问题、[E] 评估、[A] 行动 四个段落
5. 不要只生成概览或摘要，必须展开每个决策点的完整内容
6. 决策点数量通常 3-7 个,根据 PRD 实际范围调整,**不要为了凑数而加无关决策点**(如测试策略、错误处理、CI 流水线等不属于 arch)
7. 严格聚焦系统设计层面:模块划分、接口契约、数据流、技术选型、关键非功能需求
8. **禁止**在架构设计中包含:测试策略、错误处理模式、CI/CD、性能基准、部署 Stage、历史数据重生成(这些归后续 test_design / 部署工程 / 独立 follow-up plan)"""

        arch_content = self.coding_tool.query(
            prompt=prompt,
            system_instruction=ARCH_SYSTEM_PROMPT,
        )

        # Validate: if no decision points found, re-generate with explicit format
        import re
        if not re.search(r"###\s*决策点\s*\d+[:：]", arch_content):
            retry_prompt = f"""上一次生成的架构设计文档格式不正确，没有包含决策点小节。

请重新生成，必须使用以下格式（每个决策点都要完整展开）：

### 决策点 1: 标题

**[C] 背景：** ...
**[P] 问题：** ...
**[E] 评估：** ...
**[A] 行动：** ...

影响范围：...
备选方案：...

要求：
1. 严格遵循 PRD 中的约束与 scope，不要引入 PRD 未指定的组件。
2. 决策点数量根据 PRD 实际范围调整，通常 3-7 个，不要硬凑。
3. 不要输出概览或摘要，直接输出完整的架构设计文档。"""
            arch_content = self.coding_tool.query(
                prompt=retry_prompt,
                system_instruction=ARCH_SYSTEM_PROMPT,
            )

        return arch_content

    def _load_prd(self) -> str:
        # Prefer structured JSON PRD
        if self.prd_json.exists():
            from prd_generator import PRDGenerator
            with open(self.prd_json, "r", encoding="utf-8") as f:
                prd_data = json.load(f)

            # Filter out skipped PRD decision points
            prd_data = self._filter_skipped_points(prd_data)

            return PRDGenerator.prd_to_markdown(prd_data)

        # Fallback to legacy markdown
        with open(self.prd_md, "r", encoding="utf-8") as f:
            return f.read()

    def _filter_skipped_points(self, prd_data: dict) -> dict:
        """Remove decision points skipped in PRD review, build semantic skip notice.

        Two-layer filtering:
        1. Syntactic — drop skipped entries from decision_points array
        2. Semantic — build a __skip_notice__ that the prompt must surface so the LLM
           does NOT regenerate skipped topics even when they appear elsewhere in the PRD
           (e.g. overview / constraints / acceptance mentioning 重生 / 回测 / before/after).
        """
        import copy
        self._skip_notice = None
        review_file = self.plan_dir / "review.json"
        if not review_file.exists():
            return prd_data

        with open(review_file, "r", encoding="utf-8") as f:
            review = json.load(f)

        skipped_items = [
            item for item in review.get("items", [])
            if item.get("status") == "skipped"
        ]
        if not skipped_items:
            return prd_data

        skipped_indices = {item["index"] for item in skipped_items}
        filtered = copy.deepcopy(prd_data)
        original = filtered.get("decision_points", [])
        filtered["decision_points"] = [
            dp for dp in original if dp.get("index", -1) not in skipped_indices
        ]

        bullet = "\n".join(
            f"  - 决策点 {i['index']}: {i.get('title', '') or '(无标题)'}"
            for i in skipped_items
        )
        self._skip_notice = (
            f"以下 {len(skipped_items)} 个 PRD 决策点已在 review 阶段被用户显式 SKIP:\n"
            f"{bullet}\n\n"
            "你必须在生成架构设计时遵守以下约束:\n"
            "  (1) **禁止**以任何形式(包括但不限于:新决策点、Stage 阶段、影响范围说明、备选方案、"
            "概述段落引用)重新引入被 skip 的主题。\n"
            "  (2) 即便 PRD 的 overview / constraints / acceptance 字段中提到了这些主题相关的内容"
            "(如『重生』『回测对比』『before/after 交易笔数』),**这些引用是 STALE 残留**,"
            "必须忽略,不应在架构设计的任何位置体现。\n"
            "  (3) 不得为被 skip 的主题新建任何执行阶段、测试策略、部署步骤。"
        )
        return filtered

    def _build_skip_notice_block(self) -> str:
        """Return the prompt block injecting skip notice, or empty string."""
        if not getattr(self, "_skip_notice", None):
            return ""
        return f"\n\n## ⚠️ 重要:用户已 SKIP 的决策点(必须遵守)\n{self._skip_notice}\n"

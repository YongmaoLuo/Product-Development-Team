"""
Test Design Generator — CPEA-structured Test Design Document
=============================================================

Generates a test design document from an approved architecture design.
"""

import logging
from pathlib import Path
from typing import Optional

from coding_tool import CodingTool
from self_review import run_doc_self_review, write_self_review_report
from execution_logger import get_logger


log = logging.getLogger("test_design_generator")


TEST_DESIGN_SYSTEM_PROMPT = """你是一位资深测试架构师。根据已批准的架构设计文档，生成测试设计文档。

重要：直接输出 Markdown 文档内容，不要输出任何对话、解释、确认或提问。只输出文档本身。

测试设计由多个测试策略决策点组成，每个决策点使用 CPEA 框架：
- C（Context 背景）：当前测试背景或代码特征
- P（Problem 问题）：测试挑战、风险或覆盖缺口
- E（Evaluation 评估）：方案权衡分析与证据
- A（Action 行动）：推荐测试方案及理由

每个决策点还需包含：
- 测试类型：单元测试 / 集成测试 / E2E 测试 / 性能测试 / 安全测试
- 覆盖范围：该测试策略覆盖哪些模块/层
- 备选方案：1-2 个备选测试策略

测试策略决策点应覆盖以下关键领域（根据架构设计选择适用项）：
- 单元测试策略（覆盖率目标、Mock 策略、测试框架）
- 集成测试策略（模块集成顺序、测试数据管理）
- API 测试策略（接口契约测试、边界值、异常场景）
- **E2E 测试策略（强制必须包含）**：必须包含至少一个真实的端到端测试决策点，验证完整用户流程（非 mock 驱动）。E2E 测试必须使用真实的外部依赖（数据库、API、文件系统等）或可切换的 dry-run/预览模式，确保代码在集成后能真正运行。纯 mock 测试不满足 E2E 要求。如果项目有 CLI 入口或 main 函数，E2E 测试应通过调用该入口验证完整流程
- 性能测试策略（负载指标、压测场景、工具选择）
- 安全测试策略（认证/授权测试、输入验证、漏洞扫描）
- 测试数据策略（数据生成、隔离、清理）
- CI/CD 测试流水线（自动化触发、报告、门禁）

输出格式为 Markdown，结构如下：

# 测试设计 — {项目名称}

## 概述
（简要描述测试目标和核心测试哲学）

## 测试策略决策点列表

### 决策点 1: {标题}

**[C] 背景：** ...
**[P] 问题：** ...
**[E] 评估：** ...
**[A] 行动：** ...

测试类型：...
覆盖范围：...
备选方案：① ... ② ...

### 决策点 2: ...

## 测试矩阵
（模块 × 测试类型 的覆盖矩阵）

## 验收标准
（各测试类型的通过标准和度量指标）
"""


class TestDesignGenerator:
    """Generates test design document from approved architecture design."""

    # Pytest's default class collector (config: ``python_classes = Test*``)
    # would otherwise try to collect this as a test class and fail with
    # ``PytestCollectionWarning: cannot collect test class 'TestDesignGenerator'
    # because it has a __init__ constructor`` every time the test suite
    # imports the production module. ``__test__ = False`` is the
    # documented opt-out for production classes whose name happens to
    # start with ``Test`` (this class generates the workflow's
    # "Test Design" phase document — a deliverable, not a pytest test).
    __test__ = False

    def __init__(
        self,
        coding_tool: CodingTool,
        plan_dir: Path,
        self_review_enabled: bool = True,
    ):
        self.coding_tool = coding_tool
        self.plan_dir = Path(plan_dir)
        self.self_review_enabled = self_review_enabled
        self.arch_file = self.plan_dir / "arch-design.md"
        self.test_design_file = self.plan_dir / "test-design.md"

    def _load_project_context(self) -> dict:
        """Load project name and requirement from interview.json."""
        interview_file = self.plan_dir / "interview.json"
        if interview_file.exists():
            import json
            with open(interview_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            dims = data.get("dimensions", {})
            scope = dims.get("scope", "")
            if isinstance(scope, dict):
                scope_in = scope.get("in", [])
            elif isinstance(scope, str):
                # Legacy shape: scope was a free-form string.  Wrap
                # as a single-element "in" so the LLM prompt still
                # gets a list (the test-design generator only reads
                # ``scope_in`` to seed the LLM, not to validate).
                scope_in = [scope] if scope else []
            else:
                scope_in = []
            return {
                "name": data.get("plan_id", "未知项目"),
                "background": dims.get("background", ""),
                "goals": dims.get("goals", ""),
                "scope_in": scope_in,
            }
        return {"name": "未知项目", "background": "", "goals": "", "scope_in": []}

    def _load_arch_review_skips(self) -> list:
        """Aggregate skip notices from BOTH PRD review and arch review.

        Why both: PRD review skip means "this PRD decision point should not
        propagate to arch / test / tasks". Arch review skip means "this arch
        decision point should not propagate to test / tasks". The test design
        must honor BOTH upstream skip signals to avoid the LLM regenerating
        skipped topics (e.g. the B1 plan's PRD review skipped "全量历史信号重生"
        but test design still produced 决策点 6: 历史信号回归验证策略 because
        arch review didn't have the skip — arch only saw a filtered prompt).
        """
        import json
        self._skip_notice = None
        sections = []

        # PRD review skips
        prd_review_file = self.plan_dir / "review.json"
        if prd_review_file.exists():
            try:
                with open(prd_review_file, "r", encoding="utf-8") as f:
                    review = json.load(f)
                prd_skipped = [
                    i for i in review.get("items", []) if i.get("status") == "skipped"
                ]
                if prd_skipped:
                    bullet = "\n".join(
                        f"  - 决策点 {i['index']}: {i.get('title', '') or '(无标题)'}"
                        for i in prd_skipped
                    )
                    sections.append(
                        f"以下 {len(prd_skipped)} 个 PRD 决策点已被用户在 PRD review 阶段显式 SKIP:\n"
                        f"{bullet}"
                    )
            except Exception:
                pass

        # Arch review skips
        arch_review_file = self.plan_dir / "arch-review.json"
        if arch_review_file.exists():
            try:
                with open(arch_review_file, "r", encoding="utf-8") as f:
                    review = json.load(f)
                arch_skipped = [
                    i for i in review.get("items", []) if i.get("status") == "skipped"
                ]
                if arch_skipped:
                    bullet = "\n".join(
                        f"  - {i.get('title', '') or '(无标题)'}"
                        for i in arch_skipped
                    )
                    sections.append(
                        f"以下 {len(arch_skipped)} 个架构决策点已被用户在 arch review 阶段显式 SKIP:\n"
                        f"{bullet}"
                    )
            except Exception:
                pass

        if not sections:
            return []

        # Track count for backwards compat with the old single-source logic
        self._skip_notice = (
            "\n\n".join(sections)
            + "\n\n你必须在生成测试设计时遵守以下约束:\n"
            "  (1) **禁止**为被 skip 的 PRD/架构决策点生成任何测试策略、测试用例、覆盖矩阵项。\n"
            "  (2) 即便 PRD/架构文档的某些章节提到了这些被 skip 的主题(如『全量重生』『回测对比』"
            "『before/after 交易笔数』),必须忽略,不得在测试设计中体现。\n"
            "  (3) 不得为被 skip 的主题新建任何测试类型、Stage 阶段、回归验证策略或验收标准。"
        )
        return sections

    def _build_skip_notice_block(self) -> str:
        if not getattr(self, "_skip_notice", None):
            return ""
        return f"\n\n## ⚠️ 重要:用户已 SKIP 的决策点(必须遵守)\n{self._skip_notice}\n"

    def generate(self) -> str:
        """Generate test design from architecture. Returns test design content."""
        arch_content = self._load_arch()
        project = self._load_project_context()
        self._load_arch_review_skips()
        skip_block = self._build_skip_notice_block()

        scope_text = "\n".join(f"- {s}" for s in project["scope_in"]) if project["scope_in"] else ""

        prompt = f"""请为以下项目生成完整的测试设计文档。

项目名称：{project["name"]}

项目背景：
{project["background"]}

项目目标：
{project["goals"]}

项目范围（In-Scope）：
{scope_text}

参考设计文档（可能是架构设计或 PRD）：
{arch_content}
{skip_block}

要求：
1. 测试设计必须严格围绕上述项目名称、背景、目标和范围展开，不得偏离到与该项目无关的领域
2. 必须为每个测试策略决策点生成完整的 ### 决策点 N: 标题 格式的小节
3. 每个决策点必须包含 [C] 背景、[P] 问题、[E] 评估、[A] 行动 四个段落
4. 不要只生成概览或摘要，必须展开每个决策点的完整内容
5. 至少包含 6 个决策点"""

        test_content = self.coding_tool.query(
            prompt=prompt,
            system_instruction=TEST_DESIGN_SYSTEM_PROMPT,
        )

        # Validate: if no decision points found, re-generate with explicit format
        import re
        if not re.search(r"###\s*决策点\s*\d+[:：]", test_content):
            retry_prompt = f"""上一次生成的测试设计文档格式不正确，没有包含决策点小节。

请重新生成，必须使用以下格式（每个决策点都要完整展开）：

### 决策点 1: 标题

**[C] 背景：** ...
**[P] 问题：** ...
**[E] 评估：** ...
**[A] 行动：** ...

测试类型：...
覆盖范围：...
备选方案：...

不要输出概览或摘要，直接输出完整的测试设计文档，包含至少 6 个决策点。"""
            test_content = self.coding_tool.query(
                prompt=retry_prompt,
                system_instruction=TEST_DESIGN_SYSTEM_PROMPT,
            )

        self.plan_dir.mkdir(parents=True, exist_ok=True)

        # -- DP1: mandatory second-pass self-review -----------------
        # The LLM agent reviews the freshly emitted test design
        # against the upstream arch design. Failures abort the
        # generator so an unaudited draft is never promoted.
        content_to_write = test_content

        if self.self_review_enabled:
            try:
                report = run_doc_self_review(
                    doc_content=test_content,
                    doc_type="test",
                    coding_tool=self.coding_tool,
                    upstream_content=arch_content,
                    upstream_label="arch",
                )
            except Exception as exc:  # noqa: BLE001 — mandatory path
                log.warning(
                    "test_design_generator self-review raised %s: %s; "
                    "aborting generation so the unaudited draft is "
                    "not promoted",
                    type(exc).__name__,
                    exc,
                )
                raise

            # Save the first-pass test-design.md as a baseline before
            # self_review. The self_review rewrite (if any) is the
            # final version the user reviews — same identity model
            # as arch_generator / prd_generator.
            try:
                baseline_file = self.plan_dir / "test-design.original.md"
                if not baseline_file.exists():
                    with open(baseline_file, "w", encoding="utf-8") as f:
                        f.write(test_content)
            except OSError:
                pass

            # The self_review rewrite is the version the user sees.
            fixed_content = report.get("fixed_content") or ""
            rewrote = bool(report.get("rewrote"))
            succeeded = bool(report.get("succeeded"))
            if rewrote and succeeded and fixed_content.strip():
                content_to_write = fixed_content
            # else: keep original — self_review effectively failed.

            write_self_review_report(self.plan_dir, "test", report)
            logger = get_logger(plan_id=self.plan_dir.name)
            if logger is not None:
                logger.info(
                    "test_self_review",
                    f"test-design self-review produced "
                    f"{len(report.get('findings') or [])} finding(s); "
                    f"rewrote={bool(report.get('rewrote'))}, "
                    f"succeeded={bool(report.get('succeeded'))}",
                    phase="test_generation",
                    data={
                        "doc_type": "test",
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

        with open(self.test_design_file, "w", encoding="utf-8") as f:
            f.write(content_to_write)

        return test_content

    def load_test_design(self) -> Optional[str]:
        """Load existing test design if available."""
        if self.test_design_file.exists():
            with open(self.test_design_file, "r", encoding="utf-8") as f:
                return f.read()
        return None

    def _load_arch(self) -> str:
        if self.arch_file.exists():
            with open(self.arch_file, "r", encoding="utf-8") as f:
                return f.read()
        # Fallback to PRD if no architecture design was generated
        prd_file = self.plan_dir / "prd.md"
        if prd_file.exists():
            with open(prd_file, "r", encoding="utf-8") as f:
                return f.read()
        return ""

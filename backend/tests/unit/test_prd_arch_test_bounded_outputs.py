"""
Bounded-output tests for the PRD / Arch / Test-Design generator boundary.

Background
----------
Before this fix, ``PRD_SYSTEM_PROMPT_SOFTWARE`` and ``PRD_SYSTEM_PROMPT_SKILL``
emitted *only* "CPEA + few soft constraints" with no hard prohibition on
architecture-class topics (tech_stack / data_model / module_layout / interface_design
/ ci_cd_pipeline). Consequently the LLM routinely returned PRD decision_points
that should have lived in ``arch-design.md``. This regression leaked into every
plan reviewed since the workflow landed — the 5 default tech-stack / interface /
module-layout decisions in the project migration PRD are a representative case.

These tests assert (a) the PRD prompts now declare a hard prohibition list and
require the ``category`` field on each decision point, (b) ``prd_review.PRDReviewer``
mechanically rejects decision_points whose category falls outside the PRD-allowed
set, and (c) the Arch and Test-Design prompts retain their own boundaries so the
fix does not regress them.

TDD spec
--------
1. ``test_prd_software_prompt_lists_arch_forbidden_topics``:
   ``PRD_SYSTEM_PROMPT_SOFTWARE`` includes the literal 禁止 / arch stage hand-off
   for each of: 技术栈选型, 系统分层与模块划分, 数据模型与存储策略,
   关键接口与契约设计, CI/CD 流水线, 测试策略, 错误处理模式, 性能基准.

2. ``test_prd_software_prompt_requires_category_field``:
   ``PRD_SYSTEM_PROMPT_SOFTWARE`` explicitly says 每个决策点必须填写 category
   字段 and lists the 4 software-form category values.

3. ``test_prd_skill_prompt_lists_arch_forbidden_topics``:
   Same as (1) for ``PRD_SYSTEM_PROMPT_SKILL`` with the 6 skill-relevant
   forbidden topics.

4. ``test_prd_skill_prompt_requires_skill_categories``:
   ``PRD_SYSTEM_PROMPT_SKILL`` defines the 4 skill-form category values.

5. ``test_prd_review_allowed_categories_constant_matches_prompts``:
   :data:`PRD_ALLOWED_CATEGORIES` exactly matches the category strings each
   PRD prompt advertises. Drift between prompts and constant = silent breakage.

6. ``test_prd_reviewer_flags_missing_category``:
   Given a fake ``prd.json`` with one decision_point lacking ``category``,
   :meth:`PRDReviewer.validate_prd_categories` returns one violation with
   ``current_category == "<missing>"`` and persists it under
   ``review.json``'s ``category_violations``.

7. ``test_prd_reviewer_flags_arch_category``:
   Given a fake ``prd.json`` whose decision_point carries the
   arch-class ``category="tech_stack"``, the violation list contains that
   decision point and the reason names the allowed set.

8. ``test_prd_reviewer_accepts_requirement_category``:
   Given a fake ``prd.json`` whose decision_point carries the
   PRD-allowed ``category="requirement"``, ``validate_prd_categories``
   returns an empty list.

9. ``test_arch_prompt_still_has_design_boundary``:
   ``ARCH_SYSTEM_PROMPT`` still contains its existing 7-item forbidden list
   (test strategy, error handling mode, CI/CD pipeline, performance benchmark,
   security test, deployment flow, data migration). The PRD-side fix must
   not regress this.

10. ``test_test_design_prompt_still_includes_e2e_requirement``:
    ``TEST_DESIGN_SYSTEM_PROMPT`` still contains its mandatory E2E strategy
    requirement. The PRD-side fix must not regress this.
"""

import os
import json
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] \
    if not os.environ.get("PDT_DEV_REPO") \
    else Path(os.environ["PDT_DEV_REPO"]) / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from prd_generator import PRD_SYSTEM_PROMPT_SOFTWARE, PRD_SYSTEM_PROMPT_SKILL  # noqa: E402
from arch_generator import ARCH_SYSTEM_PROMPT  # noqa: E402
from test_design_generator import TEST_DESIGN_SYSTEM_PROMPT  # noqa: E402
from prd_review import PRDReviewer, PRD_ALLOWED_CATEGORIES  # noqa: E402


# Shared category vocabularies. Drift between these and the prompts =
# silent breakage of the category gate.
EXPECTED_SOFTWARE_CATEGORIES = {
    "requirement",
    "scope_boundary",
    "acceptance_criterion",
    "business_rule",
}
EXPECTED_SKILL_CATEGORIES = {
    "skill_flow_step",
    "skill_capability_reuse",
    "skill_trigger_condition",
    "skill_error_isolation",
}

ARCH_FORBIDDEN_KEYWORDS = [
    "技术栈选型",
    "系统分层与模块划分",
    "数据模型与存储策略",
    "关键接口与契约设计",
    "CI/CD",
    "测试策略",
    "错误处理模式",
    "性能基准",
]


# ----- PRD prompt content ----------------------------------------------------


def test_prd_software_prompt_lists_arch_forbidden_topics():
    """PRD must declare each arch/test topic as forbidden and hand it off."""
    prompt = PRD_SYSTEM_PROMPT_SOFTWARE
    for keyword in ARCH_FORBIDDEN_KEYWORDS:
        assert keyword in prompt, (
            f"PRD_SYSTEM_PROMPT_SOFTWARE missing forbidden topic '{keyword}'"
        )
    # Each forbidden topic should be explicitly marked as not-PD's job.
    # We accept either "禁止" or "不属于" / "→" hand-off marker.
    assert "禁止" in prompt, (
        "PRD_SYSTEM_PROMPT_SOFTWARE must include explicit 禁止 list"
    )
    assert "arch 阶段" in prompt or "arch阶段" in prompt, (
        "PRD_SYSTEM_PROMPT_SOFTWARE must hand off forbidden topics to arch"
    )


def test_prd_software_prompt_requires_category_field():
    prompt = PRD_SYSTEM_PROMPT_SOFTWARE
    assert "category" in prompt, (
        "PRD_SYSTEM_PROMPT_SOFTWARE must require a `category` field on every decision_point"
    )
    # The four expected category values must be enumerated verbatim.
    for cat in EXPECTED_SOFTWARE_CATEGORIES:
        assert cat in prompt, (
            f"PRD_SYSTEM_PROMPT_SOFTWARE must list category '{cat}' as legal"
        )


def test_prd_skill_prompt_lists_arch_forbidden_topics():
    prompt = PRD_SYSTEM_PROMPT_SKILL
    # Skill prompt forbids a tighter set focused on software-arch leakage.
    skill_required = [
        "编程语言/框架/底层库",  # tech stack
        "数据库表结构",
        "跨语言 FFI",
        "CI/CD",
        "错误重试",
        "性能基准",
    ]
    for keyword in skill_required:
        assert keyword in prompt, (
            f"PRD_SYSTEM_PROMPT_SKILL missing forbidden topic '{keyword}'"
        )
    assert "禁止" in prompt, (
        "PRD_SYSTEM_PROMPT_SKILL must include explicit 禁止 list"
    )


def test_prd_skill_prompt_requires_skill_categories():
    prompt = PRD_SYSTEM_PROMPT_SKILL
    for cat in EXPECTED_SKILL_CATEGORIES:
        assert cat in prompt, (
            f"PRD_SYSTEM_PROMPT_SKILL must list category '{cat}' as legal"
        )


# ----- Constant / prompt coherence -------------------------------------------


def test_prd_review_allowed_categories_constant_matches_prompts():
    """The review-time constant must mirror the prompt-time vocabulary."""
    assert set(PRD_ALLOWED_CATEGORIES["software"]) == EXPECTED_SOFTWARE_CATEGORIES, (
        f"PRD_ALLOWED_CATEGORIES['software'] drift: "
        f"{set(PRD_ALLOWED_CATEGORIES['software'])} vs {EXPECTED_SOFTWARE_CATEGORIES}"
    )
    assert set(PRD_ALLOWED_CATEGORIES["skill"]) == EXPECTED_SKILL_CATEGORIES, (
        f"PRD_ALLOWED_CATEGORIES['skill'] drift: "
        f"{set(PRD_ALLOWED_CATEGORIES['skill'])} vs {EXPECTED_SKILL_CATEGORIES}"
    )
    # agent / script / library fall back to SOFTWARE; they must match too.
    for form in ("agent", "script", "library"):
        assert set(PRD_ALLOWED_CATEGORIES[form]) == EXPECTED_SOFTWARE_CATEGORIES


# ----- PRDReviewer.validate_prd_categories() --------------------------------


class _FakeCodingTool:
    """Minimal stand-in — validator must not invoke the LLM."""

    def __getattr__(self, name):
        raise AssertionError(
            f"PRDReviewer.validate_prd_categories must not call coding_tool.{name}"
        )


def _write_prd_and_interview(tmpdir: Path, *, prd: dict, product_form: str = "software"):
    (tmpdir / "interview.json").write_text(
        json.dumps(
            {
                "plan_id": "test-plan",
                "product_form": {"form": product_form},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmpdir / "prd.json").write_text(
        json.dumps(prd, ensure_ascii=False), encoding="utf-8"
    )


def test_prd_reviewer_flags_missing_category(tmp_path):
    _write_prd_and_interview(
        tmp_path,
        prd={
            "title": "demo",
            "overview": "demo",
            "decision_points": [
                {
                    "index": 0,
                    "title": "用户场景",
                    "context": "背景",
                    "problem": "问题",
                    "evidence": "证据",
                    "action": "行动",
                    "impact": "影响",
                    "alternatives": [],
                    # category deliberately omitted
                }
            ],
        },
    )
    reviewer = PRDReviewer(_FakeCodingTool(), tmp_path)
    violations = reviewer.validate_prd_categories()

    assert len(violations) == 1, violations
    assert violations[0]["index"] == 0
    assert violations[0]["current_category"] == "<missing>"
    assert "category" in violations[0]["reason"]

    persisted = json.loads((tmp_path / "review.json").read_text(encoding="utf-8"))
    assert persisted["category_violations"] == violations
    assert persisted["category_violations_product_form"] == "software"


def test_prd_reviewer_flags_arch_category(tmp_path):
    _write_prd_and_interview(
        tmp_path,
        prd={
            "title": "demo",
            "decision_points": [
                {
                    "index": 0,
                    "title": "subplot 联动机制",
                    "context": "background",
                    "problem": "problem",
                    "evidence": "evidence",
                    "action": "action",
                    "impact": "impact",
                    "alternatives": [],
                    "category": "tech_stack",  # arch-class
                },
                {
                    "index": 1,
                    "title": "用户旅程",
                    "category": "requirement",
                },
            ],
        },
    )
    reviewer = PRDReviewer(_FakeCodingTool(), tmp_path)
    violations = reviewer.validate_prd_categories()

    assert len(violations) == 1, violations
    assert violations[0]["index"] == 0
    assert violations[0]["current_category"] == "tech_stack"
    assert "tech_stack" in violations[0]["reason"]
    assert "requirement" in violations[0]["reason"]


def test_prd_reviewer_accepts_requirement_category(tmp_path):
    _write_prd_and_interview(
        tmp_path,
        prd={
            "title": "demo",
            "decision_points": [
                {"index": 0, "title": "用户场景", "category": "requirement"},
                {"index": 1, "title": "范围", "category": "scope_boundary"},
                {"index": 2, "title": "业务验收", "category": "acceptance_criterion"},
                {"index": 3, "title": "业务规则", "category": "business_rule"},
            ],
        },
    )
    reviewer = PRDReviewer(_FakeCodingTool(), tmp_path)
    assert reviewer.validate_prd_categories() == []


def test_prd_reviewer_handles_skill_form(tmp_path):
    _write_prd_and_interview(
        tmp_path,
        prd={
            "title": "demo skill",
            "decision_points": [
                {"index": 0, "title": "触发词", "category": "skill_trigger_condition"},
                {"index": 1, "title": "错误隔离", "category": "skill_error_isolation"},
            ],
        },
        product_form="skill",
    )
    reviewer = PRDReviewer(_FakeCodingTool(), tmp_path)
    assert reviewer.validate_prd_categories() == []

    # Now poison one with software-arch category — skill form should flag it.
    (tmp_path / "prd.json").write_text(
        json.dumps(
            {
                "title": "demo skill",
                "decision_points": [
                    {
                        "index": 0,
                        "title": "技术栈",
                        "category": "tech_stack",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    violations = PRDReviewer(_FakeCodingTool(), tmp_path).validate_prd_categories()
    assert len(violations) == 1
    assert violations[0]["current_category"] == "tech_stack"


def test_prd_reviewer_handles_missing_prd_file(tmp_path):
    """No prd.json yet — validator must return [] silently, not crash."""
    reviewer = PRDReviewer(_FakeCodingTool(), tmp_path)
    assert reviewer.validate_prd_categories() == []


# ----- Arch / Test-Design prompt regression guards --------------------------


def test_arch_prompt_still_has_design_boundary():
    prompt = ARCH_SYSTEM_PROMPT
    # Arch prompt forbids these test-related topics. If any of them disappear,
    # the test-design leak starts.
    arch_must_forbid = [
        "测试策略",
        "覆盖率",
        "CI/CD",
        "性能基准",
        "安全测试",
        "部署流程",
        "数据迁移",
    ]
    for keyword in arch_must_forbid:
        assert keyword in prompt, (
            f"ARCH_SYSTEM_PROMPT lost forbidden topic '{keyword}'"
        )
    # And must still declare the four arch-class topics it owns.
    for keyword in (
        "技术栈选型",
        "系统分层与模块划分",
        "数据模型与存储策略",
        "关键接口与契约设计",
    ):
        assert keyword in prompt, (
            f"ARCH_SYSTEM_PROMPT lost owned topic '{keyword}'"
        )


def test_test_design_prompt_still_includes_e2e_requirement():
    prompt = TEST_DESIGN_SYSTEM_PROMPT
    # The mandatory E2E test rule must remain — losing it breaks test_design's
    # own boundary.
    assert "E2E" in prompt
    assert "端到端" in prompt
    assert "纯 mock" in prompt, (
        "TEST_DESIGN_SYSTEM_PROMPT must still reject pure-mock E2E"
    )

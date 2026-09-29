"""
Integration test: verify skip status propagates from review → arch / test / tasks.

Tests three layers:
  1. arch_generator — given PRD review with one skipped decision point, the prompt
     to the LLM must include the skip notice block.
  2. test_design_generator — given arch-review with skipped items, the prompt must
     include the skip notice block.
  3. tasks_generator — given PRD review + test review with skipped items, the prompt
     must include the skip notice block.

Uses a MockCodingTool that records the actual prompt sent. No LLM call, no cost.
"""
import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional

# Add the backend directory to import path
BACKEND_DIR = Path(__file__).resolve().parents[1] \
    if not os.environ.get("PDT_DEV_REPO") \
    else Path(os.environ["PDT_DEV_REPO"]) / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from arch_generator import ArchGenerator
from test_design_generator import TestDesignGenerator
from tasks_generator import TasksGenerator


def _passthrough_self_review(*args, **kwargs):
    """No-op stub for the mandatory second-pass self-review.

    These tests only care about the prompt text the generator sends
    to the LLM for the *first* pass. The second pass is mandatory,
    but its prompt is not under test here — so stub it with a
    pass-through that always succeeds. The new tasks second pass
    sends the canonical tasks.json as the doc and expects the LLM
    to return a complete rewrite; we mirror that by parsing the
    JSON the generator handed us and returning the largest JSON
    object (the canonical tasks payload) unchanged.
    """
    doc_content = kwargs.get("doc_content") or (
        args[0] if args else ""
    )
    rewritten: object = doc_content
    best_size = 0
    if isinstance(doc_content, str):
        decoder = json.JSONDecoder()
        idx = 0
        while True:
            pos = doc_content.find("{", idx)
            if pos == -1:
                break
            try:
                obj, _ = decoder.raw_decode(doc_content[pos:])
                size = len(json.dumps(obj, ensure_ascii=False))
                if size > best_size:
                    rewritten = obj
                    best_size = size
                idx = pos + 1
            except json.JSONDecodeError:
                idx = pos + 1
    return {
        "doc_type": kwargs.get("doc_type") or "tasks",
        "attempted": True,
        "succeeded": True,
        "rewrote": True,
        "findings": [],
        "fixed_content": rewritten,
        "input_content_hash": "sha256:" + "x" * 64,
        "fixed_content_hash": "sha256:" + "x" * 64,
        "severity_high_count": 0,
        "severity_medium_count": 0,
        "severity_low_count": 0,
        "mandatory": True,
        "error": None,
    }


# Apply the stub to every generator module that imports self_review.
# This is the legacy escape hatch: tests that pre-date the
# mandatory second pass can opt out by patching the entry point.
import arch_generator as _arch_gen
import test_design_generator as _td_gen
import tasks_generator as _ts_gen
import self_review as _self_review_mod

_arch_gen.run_doc_self_review = _passthrough_self_review
_td_gen.run_doc_self_review = _passthrough_self_review
# tasks_generator imports ``run_doc_self_review`` into its own
# namespace (``from self_review import run_doc_self_review``),
# so the passthrough must replace BOTH the source module and
# the local binding.
_tg_passthrough = _passthrough_self_review
_tg_passthrough.__module__ = _self_review_mod.__name__
_ts_gen.run_doc_self_review = _tg_passthrough
_self_review_mod.run_doc_self_review = _tg_passthrough


class MockCodingTool:
    """Mock that records every prompt/query it receives and returns canned output."""

    def __init__(self, canned_response: str = ""):
        self.queries: List[dict] = []
        self.canned_response = canned_response

    def query(self, prompt: str, system_instruction: Optional[str] = None,
              retries: int = 3, timeout: Optional[int] = None) -> str:
        self.queries.append({
            "method": "query",
            "prompt": prompt,
            "system_instruction": system_instruction,
        })
        return self.canned_response

    def query_json(self, prompt: str, system_instruction: Optional[str] = None,
                   retries: int = 3, timeout: Optional[int] = None) -> dict:
        self.queries.append({
            "method": "query_json",
            "prompt": prompt,
            "system_instruction": system_instruction,
        })
        # 2026-09-13: generate() hard-fails on 0 tasks
        # (TasksGenerationError), so return one minimal valid task to
        # let the flow reach the prompt assertions. Prompt-content
        # tests don't care about the task payload itself.
        return {
            "tasks": [
                {
                    "id": "1",
                    "title": "mock task",
                    "description": "mock description",
                    "test_command": "echo 1",
                    "depends_on": [],
                }
            ]
        }


SAMPLE_PRD = {
    "title": "测试 PRD 包含 skip",
    "overview": "本 PRD 包含一个会被 skip 的『全量历史重生』主题,用来验证 arch/test/tasks generator 是否正确传播 skip。",
    "constraints": [
        "技术栈:Python + Rust",
        "共享代码:本测试 plan 无共享代码",
    ],
    "acceptance": [
        "1. 单元测试覆盖主功能",
        "2. 全量重生成历史信号,旧错误信号不保留",
        "3. 回测对比 before/after 交易笔数变化",
    ],
    "decision_points": [
        {
            "index": 0,
            "title": "修复 B1 信号逻辑",
            "context": "B1 锚点修复",
            "problem": "如何修复",
            "evidence": "规范原文",
            "action": "改 signal.rs",
            "impact": "影响 B1 信号",
            "alternatives": ["不改", "改 signals.rs"],
        },
        {
            "index": 1,
            "title": "全量历史信号重生",  # 这个会被 skip
            "context": "重生历史信号",
            "problem": "如何重生",
            "evidence": "PRD 要求",
            "action": "regenerate_signals",
            "impact": "影响历史数据",
            "alternatives": ["不重生"],
        },
        {
            "index": 2,
            "title": "回测对比 before/after",  # 这个也会被 skip
            "context": "回测对比",
            "problem": "如何对比",
            "evidence": "验收要求",
            "action": "对比 before/after",
            "impact": "影响回测",
            "alternatives": ["不对比"],
        },
    ],
}

# Arch items 5, 6 (skipped test scenarios)  +  test items 3, 4, 6 (skipped test)
SAMPLE_ARCH_REVIEW = {
    "items": [
        {"index": 0, "title": "修复 B1 信号逻辑", "status": "accepted"},
        {"index": 5, "title": "全量历史重生与回测对比", "status": "skipped"},
        {"index": 6, "title": "部署与执行流(含重生)", "status": "skipped"},
    ]
}

SAMPLE_TEST_REVIEW = {
    "items": [
        {"index": 0, "title": "L1 cargo test", "status": "accepted"},
        {"index": 3, "title": "全量重生与回测回归", "status": "skipped"},
        {"index": 4, "title": "性能测试", "status": "skipped"},
    ]
}


def write_sample_prd(plan_dir: Path) -> None:
    """Write a sample PRD.json mimicking a real plan."""
    with open(plan_dir / "prd.json", "w", encoding="utf-8") as f:
        json.dump(SAMPLE_PRD, f, ensure_ascii=False, indent=2)


def write_prd_review_with_skip(plan_dir: Path) -> None:
    """Write a PRD review.json that skips decision_points[1] and [2]."""
    review = {
        "items": [
            {"index": 0, "title": "修复 B1 信号逻辑", "status": "accepted"},
            {"index": 1, "title": "全量历史信号重生", "status": "skipped"},
            {"index": 2, "title": "回测对比 before/after", "status": "skipped"},
        ]
    }
    with open(plan_dir / "review.json", "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2)


def write_arch_design(plan_dir: Path) -> None:
    """Write a fake arch-design.md (what arch generator would have produced)."""
    arch = (
        "# 架构设计 — 测试\n\n"
        "## 架构决策点列表\n\n"
        "### 决策点 1: 修复 B1 信号逻辑\n"
        "CPEA 内容...\n\n"
        "### 决策点 2: 测试金字塔\n"
        "CPEA 内容...\n\n"
    )
    with open(plan_dir / "arch-design.md", "w", encoding="utf-8") as f:
        f.write(arch)


def write_arch_review(plan_dir: Path) -> None:
    with open(plan_dir / "arch-review.json", "w", encoding="utf-8") as f:
        json.dump(SAMPLE_ARCH_REVIEW, f, ensure_ascii=False, indent=2)


def write_test_design(plan_dir: Path) -> None:
    test = (
        "# 测试设计 — 测试\n\n"
        "## 测试策略决策点列表\n\n"
        "### 决策点 1: L1 cargo test\n"
        "CPEA 内容...\n\n"
    )
    with open(plan_dir / "test-design.md", "w", encoding="utf-8") as f:
        f.write(test)


def write_test_review(plan_dir: Path) -> None:
    with open(plan_dir / "test-review.json", "w", encoding="utf-8") as f:
        json.dump(SAMPLE_TEST_REVIEW, f, ensure_ascii=False, indent=2)


def write_interview(plan_dir: Path) -> None:
    """interview.json needed for TestDesignGenerator._load_project_context."""
    interview = {
        "plan_id": "test-skip-propagation",
        "dimensions": {
            "background": "测试 skip 传播",
            "goals": "验证 skip 状态从 review 传到下游",
            "scope": {
                "in": ["修复 B1", "测试覆盖"],
            },
        },
    }
    with open(plan_dir / "interview.json", "w", encoding="utf-8") as f:
        json.dump(interview, f, ensure_ascii=False, indent=2)


class TestArchSkipPropagation(unittest.TestCase):
    """arch_generator must inject skip notice into prompt when PRD review has skips."""

    def setUp(self):
        self.plan_dir = Path(tempfile.mkdtemp(prefix="ac-skip-test-arch-"))
        write_sample_prd(self.plan_dir)
        write_prd_review_with_skip(self.plan_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.plan_dir, ignore_errors=True)

    def test_prompt_contains_skip_notice(self):
        mock = MockCodingTool(canned_response="# 架构设计 (mock)\n\n### 决策点 1: 修复 B1")
        gen = ArchGenerator(mock, self.plan_dir)
        gen.generate()
        self.assertEqual(len(mock.queries), 1)
        prompt = mock.queries[0]["prompt"]
        # Must include skip notice
        self.assertIn("⚠️ 重要:用户已 SKIP 的决策点", prompt)
        self.assertIn("全量历史信号重生", prompt)
        self.assertIn("回测对比 before/after", prompt)
        # Must include the strong constraints
        self.assertIn("禁止", prompt)
        self.assertIn("STALE 残留", prompt)

    def test_no_skip_notice_when_review_empty(self):
        # Remove review.json → no skip
        (self.plan_dir / "review.json").unlink()
        mock = MockCodingTool(canned_response="# 架构设计 (mock)\n\n### 决策点 1: B1")
        gen = ArchGenerator(mock, self.plan_dir)
        gen.generate()
        prompt = mock.queries[0]["prompt"]
        self.assertNotIn("⚠️ 重要:用户已 SKIP 的决策点", prompt)

    def test_no_skip_notice_when_no_items_skipped(self):
        # All items accepted, no skip
        review = {
            "items": [
                {"index": 0, "title": "修复 B1 信号逻辑", "status": "accepted"},
                {"index": 1, "title": "全量历史信号重生", "status": "accepted"},
                {"index": 2, "title": "回测对比 before/after", "status": "accepted"},
            ]
        }
        with open(self.plan_dir / "review.json", "w", encoding="utf-8") as f:
            json.dump(review, f, ensure_ascii=False, indent=2)
        mock = MockCodingTool(canned_response="# 架构设计 (mock)\n\n### 决策点 1: B1")
        gen = ArchGenerator(mock, self.plan_dir)
        gen.generate()
        prompt = mock.queries[0]["prompt"]
        self.assertNotIn("⚠️ 重要:用户已 SKIP 的决策点", prompt)

    def test_prompt_forbids_test_strategy_in_arch(self):
        """arch prompt must explicitly forbid test strategies, error handling, etc.

        This guards against the LLM padding arch with test-design concerns
        (决策点 6: 测试与验证架构, 决策点 7: 错误处理与边界场景策略).
        """
        mock = MockCodingTool(canned_response="# 架构设计 (mock)\n\n### 决策点 1: B1")
        gen = ArchGenerator(mock, self.plan_dir)
        gen.generate()
        prompt = mock.queries[0]["prompt"]
        # System prompt + user prompt should both contain the explicit forbid list
        # Sample a few key forbidden terms that should appear in the forbid instructions
        self.assertIn("测试策略", prompt)
        self.assertIn("错误处理", prompt)
        self.assertIn("CI/CD", prompt)
        self.assertIn("性能基准", prompt)
        # And explicit padding warning
        self.assertIn("不要为了凑数", prompt)
        # And the "system_instruction" passed to the coding tool should also have
        # the forbid list (so it's enforced at the system level too)
        sys_inst = mock.queries[0]["system_instruction"]
        self.assertIn("测试策略", sys_inst)
        self.assertIn("错误处理", sys_inst)
        self.assertIn("不要为了凑数", sys_inst)


class TestTestDesignSkipPropagation(unittest.TestCase):
    """test_design_generator must aggregate PRD+arch review skip notices."""

    def setUp(self):
        self.plan_dir = Path(tempfile.mkdtemp(prefix="ac-skip-test-test-"))
        write_sample_prd(self.plan_dir)
        write_arch_design(self.plan_dir)
        write_arch_review(self.plan_dir)
        write_interview(self.plan_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.plan_dir, ignore_errors=True)

    def test_prompt_contains_arch_skip_notice(self):
        mock = MockCodingTool(canned_response="# 测试设计 (mock)\n\n### 决策点 1: cargo test")
        gen = TestDesignGenerator(mock, self.plan_dir)
        gen.generate()
        self.assertEqual(len(mock.queries), 1)
        prompt = mock.queries[0]["prompt"]
        # The aggregator emits the umbrella "用户已 SKIP 的决策点" header
        self.assertIn("⚠️ 重要:用户已 SKIP 的决策点", prompt)
        # Should mention arch-review skips
        self.assertIn("全量历史重生与回测对比", prompt)
        self.assertIn("部署与执行流(含重生)", prompt)
        self.assertIn("禁止", prompt)

    def test_no_skip_notice_when_both_reviews_empty(self):
        (self.plan_dir / "arch-review.json").unlink()
        mock = MockCodingTool(canned_response="# 测试设计 (mock)\n\n### 决策点 1: cargo")
        gen = TestDesignGenerator(mock, self.plan_dir)
        gen.generate()
        prompt = mock.queries[0]["prompt"]
        self.assertNotIn("⚠️ 重要:用户已 SKIP 的决策点", prompt)

    def test_prompt_contains_prd_skip_notice_even_when_no_arch_skips(self):
        """The B1 plan scenario: PRD review skipped, arch review all-accepted.

        Without aggregating PRD review, test design would not know about the
        historical-regeneration skip and would regenerate 决策点 6 about it.
        """
        # Replace arch-review with one that has NO skips (all accepted)
        no_skip_arch_review = {
            "items": [
                {"index": 0, "title": "锚点所有权集中", "status": "accepted"},
                {"index": 1, "title": "分型触发的四步流水线", "status": "accepted"},
                {"index": 2, "title": "跨语言序列化契约", "status": "accepted"},
            ]
        }
        with open(self.plan_dir / "arch-review.json", "w", encoding="utf-8") as f:
            json.dump(no_skip_arch_review, f, ensure_ascii=False, indent=2)
        # Add PRD review with skip
        write_prd_review_with_skip(self.plan_dir)
        mock = MockCodingTool(canned_response="# 测试设计 (mock)\n\n### 决策点 1: cargo")
        gen = TestDesignGenerator(mock, self.plan_dir)
        gen.generate()
        prompt = mock.queries[0]["prompt"]
        # PRD skip must surface
        self.assertIn("⚠️ 重要:用户已 SKIP 的决策点", prompt)
        self.assertIn("PRD 决策点已被用户在 PRD review 阶段显式 SKIP", prompt)
        self.assertIn("全量历史信号重生", prompt)
        self.assertIn("回测对比 before/after", prompt)
        # The umbrella forbid clauses from the aggregator
        self.assertIn("禁止", prompt)
        self.assertIn("回归验证策略", prompt)  # explicit clause added by aggregator


class TestTasksSkipPropagation(unittest.TestCase):
    """tasks_generator must inject PRD+test review skip notice into prompt."""

    def setUp(self):
        self.plan_dir = Path(tempfile.mkdtemp(prefix="ac-skip-test-tasks-"))
        write_sample_prd(self.plan_dir)
        write_prd_review_with_skip(self.plan_dir)
        write_arch_design(self.plan_dir)
        write_test_design(self.plan_dir)
        write_test_review(self.plan_dir)
        write_interview(self.plan_dir)
        # Disable preflight so the test exercises only the
        # query_json call inside generate() (not the additional LLM
        # call inside PreFlightReviewer). The skip-notice logic
        # itself does not depend on preflight.
        with open(self.plan_dir / "plan_state.json", "w", encoding="utf-8") as f:
            json.dump(
                {"flags": {"preflight_enabled": False}},
                f,
                ensure_ascii=False,
            )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.plan_dir, ignore_errors=True)

    def test_prompt_contains_prd_and_test_skip_notice(self):
        mock = MockCodingTool()
        gen = TasksGenerator(mock, self.plan_dir)
        gen.generate()
        # First call is the tasks JSON emit (query_json). The
        # mandatory second-pass self-review then issues a
        # follow-up ``query`` call (the old test was written
        # before the second pass landed), so we expect 2 calls.
        methods = [q["method"] for q in mock.queries]
        self.assertIn("query_json", methods)
        prompt = mock.queries[0]["prompt"]
        # PRD skip section
        self.assertIn("⚠️ 重要:用户已 SKIP 的决策点", prompt)
        self.assertIn("PRD 决策点已被用户 SKIP", prompt)
        self.assertIn("全量历史信号重生", prompt)
        self.assertIn("回测对比 before/after", prompt)
        # Test skip section
        self.assertIn("测试决策点已被用户 SKIP", prompt)
        self.assertIn("全量重生与回测回归", prompt)
        self.assertIn("性能测试", prompt)

    def test_no_skip_notice_when_both_reviews_empty(self):
        (self.plan_dir / "review.json").unlink()
        (self.plan_dir / "test-review.json").unlink()
        mock = MockCodingTool()
        gen = TasksGenerator(mock, self.plan_dir)
        gen.generate()
        prompt = mock.queries[0]["prompt"]
        self.assertNotIn("⚠️ 重要:用户已 SKIP 的决策点", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)

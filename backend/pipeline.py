"""
Pipeline — End-to-End Planning Chain with Review-Correction Loops
===================================================================

Orchestrates: Interviewer → PRD Generator → PRD Review Loop →
              [Arch Design → Arch Review Loop] →
              [Test Design → Test Review Loop] →
              tasks.json Generator

Usage:
    python pipeline.py --project-dir ./myproject --config coding
    python pipeline.py --resume --project-dir ./myproject
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from coding_tool import ClaudeCodingTool
from interviewer import Interviewer
from prd_generator import PRDGenerator
from prd_review import PRDReviewer
from prd_refiner import PRDRefiner
from plan_state import PlanState
from arch_generator import ArchGenerator
from arch_reviewer import ArchReviewer
from arch_refiner import ArchRefiner
from test_design_generator import TestDesignGenerator
from test_design_reviewer import TestDesignReviewer
from test_design_refiner import TestDesignRefiner
from tasks_generator import TasksGenerator


MAX_REVIEW_ROUNDS = 3


class Pipeline:
    """End-to-end planning pipeline with review-correction loops."""

    def __init__(self, project_dir: Path, config_name: Optional[str] = None):
        self.project_dir = project_dir.resolve()
        self.config_name = config_name
        self.coding_tool = ClaudeCodingTool()

        # Determine plan ID from project dir name or generate one
        plan_id = self.project_dir.name
        self.plan_dir = Path(__file__).parent / "plans" / plan_id
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        self.plan_state = PlanState(self.plan_dir)

    def run(self, resume: bool = False):
        """Run the full pipeline."""
        print(f"Pipeline: {self.plan_dir.name}")
        print(f"Project:  {self.project_dir}\n")

        # Step 1: Interview
        interviewer = Interviewer(self.coding_tool, self.plan_dir)
        if not resume or not interviewer.is_complete():
            self._run_interview(interviewer)
        else:
            print("[跳过] Interview 已完成\n")

        # Step 2: Generate PRD
        prd_gen = PRDGenerator(self.coding_tool, self.plan_dir)
        prd_content = prd_gen.load_prd()
        if not resume or prd_content is None:
            print("生成 PRD...")
            prd_content = prd_gen.generate()
            self.plan_state.transition_to("prd_generation")
            print("PRD 已生成\n")
        else:
            print("[跳过] PRD 已存在\n")

        # Step 3: PRD Review Loop
        reviewer = PRDReviewer(self.coding_tool, self.plan_dir)
        review = reviewer.load_review()
        if not resume or review is None or reviewer.has_rejections():
            self._run_prd_review_loop(reviewer, prd_content)
        else:
            print(f"[跳过] PRD 已审阅: {review['accepted']} 接受, {review['rejected']} 拒绝\n")

        # Step 4: Optional Architecture Design
        if self.plan_state.is_prd_approved():
            self._maybe_run_arch_design()

        # Step 5: Optional Test Design
        if self.plan_state.is_arch_approved() or (
            self.plan_state.is_arch_enabled() and self.plan_state.is_arch_approved()
        ):
            self._maybe_run_test_design()

        # Step 6: Generate tasks.json
        if self.plan_state.is_ready_for_tasks():
            print("生成 tasks.json...")
            tasks_gen = TasksGenerator(
                self.coding_tool, self.plan_dir,
                self_review_enabled=self.plan_state.is_self_review_enabled(),
            )
            tasks_data = tasks_gen.generate()
            self.plan_state.transition_to("tasks_generation")
            print(f"已生成 {len(tasks_data.get('tasks', []))} 个任务")
            print(f"tasks.json 已保存到: {self.plan_dir / 'tasks.json'}\n")
            self.plan_state.transition_to("ready")
        else:
            print("[跳过] 尚未满足任务生成条件\n")

        # Next steps
        print("=" * 50)
        print("规划完成。下一步执行：")
        print(
            f"  autonomous-coding --recover -w {self.project_dir} "
            f"--tasks-file {self.plan_dir / 'tasks.json'}"
        )
        if self.config_name:
            print(
                f"  autonomous-coding --recover -w {self.project_dir} "
                f"--config {self.config_name} "
                f"--tasks-file {self.plan_dir / 'tasks.json'}"
            )

    def _run_interview(self, interviewer: Interviewer):
        """Run the interactive interview."""
        initial = input("\n请描述你的需求（一句话即可）： ").strip()
        if not initial:
            print("需求不能为空")
            sys.exit(1)

        questions = interviewer.start(initial)
        while not interviewer.is_complete() and questions:
            print("\n请回答以下问题：")
            for i, q in enumerate(questions, 1):
                print(f"  {i}. {q}")

            reply = input("\n你的回答： ").strip()
            if not reply:
                continue
            questions = interviewer.continue_interview(reply)

        if interviewer.is_complete():
            print("\n需求收集完成")
            self.plan_state.transition_to("interview_complete")
            with open(interviewer.interview_file, "r") as f:
                data = json.load(f)
            dims = data.get("dimensions", {})
            print(f"  背景: {dims.get('background', 'N/A')[:80]}...")
            print(f"  目标: {dims.get('goals', 'N/A')[:80]}...")
            print(f"  范围: in={dims.get('scope', {}).get('in', [])}")
            print()

    def _run_prd_review_loop(self, reviewer: PRDReviewer, prd_content: str):
        """Run PRD review with correction loop (max 3 rounds)."""
        round_num = 0

        while True:
            round_num += 1
            print(f"\n{'=' * 60}")
            print(f"PRD 审阅 — 第 {round_num} 轮")
            print(f"{'=' * 60}\n")

            items = reviewer.extract_decision_points(prd_content)
            if not items:
                print("未找到决策点，跳过审阅。")
                self.plan_state.transition_to("prd_approved")
                return

            accepted = 0
            rejected = 0
            skipped = 0

            for item in items:
                print(f"── 决策点 ({item.index + 1}/{len(items)}) ──")
                print(item.content)
                print(f"\n{'─' * 40}")

                while True:
                    action = input("[✓ 接受] [✗ 拒绝] [? 追问] [跳过]: ").strip().lower()

                    if action in ("y", "yes", "接受", "✓", "a", "accept"):
                        item.status = "accepted"
                        accepted += 1
                        break
                    elif action in ("n", "no", "拒绝", "✗", "r", "reject"):
                        item.status = "rejected"
                        item.note = input("拒绝原因: ").strip()
                        rejected += 1
                        break
                    elif action in ("?", "追问", "q", "question"):
                        question = input("你的问题: ").strip()
                        item.note = reviewer._ask_followup(item.content, question)
                        print(f"\n补充回答: {item.note}\n")
                        continue
                    elif action in ("s", "skip", "跳过"):
                        item.status = "skipped"
                        skipped += 1
                        break
                    else:
                        print("无效输入，请重新选择")

                print()

            # Save review results
            reviewer._save_review(items)
            print(f"审阅完成: {accepted} 接受, {rejected} 拒绝, {skipped} 跳过")

            if rejected == 0:
                print("所有决策点已通过审阅。")
                self.plan_state.transition_to("prd_approved")
                return

            if round_num >= MAX_REVIEW_ROUNDS:
                print(f"\n已达到最大审阅轮数 ({MAX_REVIEW_ROUNDS})，仍有 {rejected} 个决策点被拒绝。")
                print("请在 Web UI 中继续审阅，或手动修改 PRD 后重新运行。")
                self.plan_state.transition_to("prd_refining")
                return

            # Refine PRD based on rejected items
            print(f"\n有 {rejected} 个决策点被拒绝，正在根据反馈修订 PRD...")
            self.plan_state.transition_to("prd_refining")

            refiner = PRDRefiner(self.coding_tool, self.plan_dir)
            prd_content = refiner.refine()
            reviewer.reset_for_refinement()
            self.plan_state.increment_review_round("prd")
            self.plan_state.transition_to("prd_review")

            print("PRD 已修订，请重新审阅。\n")

    def _maybe_run_arch_design(self):
        """Optionally run architecture design phase."""
        choice = input("\n是否生成架构设计文档？ (y/n): ").strip().lower()
        if choice not in ("y", "yes", "是"):
            print("[跳过] 架构设计\n")
            return

        print("\n生成架构设计文档...")
        gen = ArchGenerator(self.coding_tool, self.plan_dir)
        arch_content = gen.generate()
        self.plan_state.transition_to("arch_generation")
        self.plan_state.enable_arch(True)
        print("架构设计文档已生成\n")

        # Architecture review loop
        reviewer = ArchReviewer(self.coding_tool, self.plan_dir)
        self._run_arch_review_loop(reviewer, arch_content)

    def _run_arch_review_loop(self, reviewer: ArchReviewer, arch_content: str):
        """Run architecture review with correction loop."""
        round_num = 0

        while True:
            round_num += 1
            print(f"\n{'=' * 60}")
            print(f"架构审阅 — 第 {round_num} 轮")
            print(f"{'=' * 60}\n")

            items = reviewer.extract_decision_points(arch_content)
            if not items:
                print("未找到决策点，跳过审阅。")
                self.plan_state.transition_to("arch_approved")
                return

            accepted = 0
            rejected = 0
            skipped = 0

            for item in items:
                print(f"── 决策点 ({item.index + 1}/{len(items)}) ──")
                print(item.content)
                print(f"\n{'─' * 40}")

                while True:
                    action = input("[✓ 接受] [✗ 拒绝] [? 追问] [跳过]: ").strip().lower()

                    if action in ("y", "yes", "接受", "✓", "a", "accept"):
                        item.status = "accepted"
                        accepted += 1
                        break
                    elif action in ("n", "no", "拒绝", "✗", "r", "reject"):
                        item.status = "rejected"
                        item.note = input("拒绝原因: ").strip()
                        rejected += 1
                        break
                    elif action in ("?", "追问", "q", "question"):
                        question = input("你的问题: ").strip()
                        item.note = reviewer._ask_followup(item.content, question)
                        print(f"\n补充回答: {item.note}\n")
                        continue
                    elif action in ("s", "skip", "跳过"):
                        item.status = "skipped"
                        skipped += 1
                        break
                    else:
                        print("无效输入，请重新选择")

                print()

            # Save review
            self._save_arch_review(reviewer, items)
            print(f"架构审阅完成: {accepted} 接受, {rejected} 拒绝, {skipped} 跳过")

            if rejected == 0:
                print("所有架构决策点已通过审阅。")
                self.plan_state.transition_to("arch_approved")
                return

            if round_num >= MAX_REVIEW_ROUNDS:
                print(f"\n已达到最大审阅轮数 ({MAX_REVIEW_ROUNDS})，仍有 {rejected} 个决策点被拒绝。")
                print("请在 Web UI 中继续审阅，或手动修改后重新运行。")
                self.plan_state.transition_to("arch_refining")
                return

            # Refine architecture
            print(f"\n有 {rejected} 个决策点被拒绝，正在根据反馈修订架构设计...")
            self.plan_state.transition_to("arch_refining")

            refiner = ArchRefiner(self.coding_tool, self.plan_dir)
            arch_content = refiner.refine()
            reviewer.reset_for_refinement()
            self.plan_state.increment_review_round("arch")
            self.plan_state.transition_to("arch_review")

            print("架构设计已修订，请重新审阅。\n")

    def _save_arch_review(self, reviewer: ArchReviewer, items):
        """Save architecture review results."""
        import json
        review_data = {
            "items": [item.to_dict() for item in items],
            "total": len(items),
            "accepted": sum(1 for i in items if i.status == "accepted"),
            "rejected": sum(1 for i in items if i.status == "rejected"),
            "skipped": sum(1 for i in items if i.status == "skipped"),
        }
        review_file = self.plan_dir / "arch-review.json"
        with open(review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, indent=2, ensure_ascii=False)

    def _maybe_run_test_design(self):
        """Optionally run test design phase."""
        choice = input("\n是否生成测试设计文档？ (y/n): ").strip().lower()
        if choice not in ("y", "yes", "是"):
            print("[跳过] 测试设计\n")
            return

        print("\n生成测试设计文档...")
        gen = TestDesignGenerator(self.coding_tool, self.plan_dir)
        test_content = gen.generate()
        self.plan_state.transition_to("test_generation")
        self.plan_state.enable_test(True)
        print("测试设计文档已生成\n")

        # Test design review loop
        reviewer = TestDesignReviewer(self.coding_tool, self.plan_dir)
        self._run_test_review_loop(reviewer, test_content)

    def _run_test_review_loop(self, reviewer: TestDesignReviewer, test_content: str):
        """Run test design review with correction loop."""
        round_num = 0

        while True:
            round_num += 1
            print(f"\n{'=' * 60}")
            print(f"测试设计审阅 — 第 {round_num} 轮")
            print(f"{'=' * 60}\n")

            items = reviewer.extract_decision_points(test_content)
            if not items:
                print("未找到决策点，跳过审阅。")
                self.plan_state.transition_to("test_approved")
                return

            accepted = 0
            rejected = 0
            skipped = 0

            for item in items:
                print(f"── 决策点 ({item.index + 1}/{len(items)}) ──")
                print(item.content)
                print(f"\n{'─' * 40}")

                while True:
                    action = input("[✓ 接受] [✗ 拒绝] [? 追问] [跳过]: ").strip().lower()

                    if action in ("y", "yes", "接受", "✓", "a", "accept"):
                        item.status = "accepted"
                        accepted += 1
                        break
                    elif action in ("n", "no", "拒绝", "✗", "r", "reject"):
                        item.status = "rejected"
                        item.note = input("拒绝原因: ").strip()
                        rejected += 1
                        break
                    elif action in ("?", "追问", "q", "question"):
                        question = input("你的问题: ").strip()
                        item.note = reviewer._ask_followup(item.content, question)
                        print(f"\n补充回答: {item.note}\n")
                        continue
                    elif action in ("s", "skip", "跳过"):
                        item.status = "skipped"
                        skipped += 1
                        break
                    else:
                        print("无效输入，请重新选择")

                print()

            # Save review
            self._save_test_review(reviewer, items)
            print(f"测试设计审阅完成: {accepted} 接受, {rejected} 拒绝, {skipped} 跳过")

            if rejected == 0:
                print("所有测试决策点已通过审阅。")
                self.plan_state.transition_to("test_approved")
                return

            if round_num >= MAX_REVIEW_ROUNDS:
                print(f"\n已达到最大审阅轮数 ({MAX_REVIEW_ROUNDS})，仍有 {rejected} 个决策点被拒绝。")
                print("请在 Web UI 中继续审阅，或手动修改后重新运行。")
                self.plan_state.transition_to("test_refining")
                return

            # Refine test design
            print(f"\n有 {rejected} 个决策点被拒绝，正在根据反馈修订测试设计...")
            self.plan_state.transition_to("test_refining")

            refiner = TestDesignRefiner(self.coding_tool, self.plan_dir)
            test_content = refiner.refine()
            reviewer.reset_for_refinement()
            self.plan_state.increment_review_round("test")
            self.plan_state.transition_to("test_review")

            print("测试设计已修订，请重新审阅。\n")

    def _save_test_review(self, reviewer: TestDesignReviewer, items):
        """Save test design review results."""
        import json
        review_data = {
            "items": [item.to_dict() for item in items],
            "total": len(items),
            "accepted": sum(1 for i in items if i.status == "accepted"),
            "rejected": sum(1 for i in items if i.status == "rejected"),
            "skipped": sum(1 for i in items if i.status == "skipped"),
        }
        review_file = self.plan_dir / "test-review.json"
        with open(review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, indent=2, ensure_ascii=False)


def main():
    from env_config import load_env

    load_env()

    parser = argparse.ArgumentParser(description="Spec-Driven Development Pipeline")
    parser.add_argument("--project-dir", "-w", required=True, help="Target project directory")
    parser.add_argument("--config", "-c", default=None, help="Configuration name (e.g., 'coding')")
    parser.add_argument("--resume", "-r", action="store_true", help="Resume from saved state")
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    if not project_dir.exists():
        print(f"Project directory not found: {project_dir}")
        sys.exit(1)

    pipeline = Pipeline(project_dir, config_name=args.config)
    pipeline.run(resume=args.resume)


if __name__ == "__main__":
    main()

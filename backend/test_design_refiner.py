"""
Test Design Refiner — Review-Correction Loop for Test Design
=============================================================

Revises test design based on rejected review items.
Preserves accepted and skipped decision points.
"""

from pathlib import Path
from typing import List, Optional

from coding_tool import CodingTool


TEST_DESIGN_REFINER_SYSTEM_PROMPT = """你是一位资深测试架构师。你的任务是修订测试设计文档，根据用户的反馈调整决策点。

规则：
1. **优先**根据被拒绝（rejected）的决策点进行修改
2. 当用户提供了额外反馈（没有具体被拒绝项或反馈涉及整体方向）时，**按照反馈整体修订测试设计**
3. **保留**所有已接受（accepted）和已跳过（skipped）的决策点，除非用户的反馈明确要求修改
4. 如果用户的反馈涉及测试策略，提供替代方案并说明权衡
5. 如果用户的反馈涉及范围或影响，调整对应内容
6. 保持 Markdown 格式一致
7. 输出完整的测试设计文档（包含所有决策点）

输出格式：完整的 Markdown 测试设计文档。"""


class TestDesignRefiner:
    """Revises test design based on rejected review items."""

    def __init__(self, coding_tool: CodingTool, plan_dir: Path):
        self.coding_tool = coding_tool
        self.plan_dir = Path(plan_dir)
        self.test_design_file = self.plan_dir / "test-design.md"
        self.review_file = self.plan_dir / "test-review.json"

    def refine(self, feedback: Optional[str] = None) -> str:
        """Revise test design based on rejected items and/or direct feedback.

        Parameters
        ----------
        feedback:
            Optional free-form feedback from the HTTP request body. When review
            state files are missing or empty, this feedback is still applied so
            the refinement endpoint does not silently return the original
            document.
        """
        test_content = self._load_test_design()
        rejected_items = self._load_rejected_items()

        feedback_parts = []
        if rejected_items:
            feedback_parts.append(self._build_feedback_text(rejected_items))
        if feedback:
            feedback_parts.append(f"用户额外反馈：\n{feedback}")

        if not feedback_parts:
            return test_content

        feedback_text = "\n\n".join(feedback_parts)

        if not rejected_items:
            instruction = (
                "请根据以下用户的整体反馈，重新设计测试方案。用户反馈可能涉及范围、测试策略或实现方式的调整，"
                "请输出完整的修订后测试设计文档。"
            )
        else:
            instruction = (
                "请根据以下用户的反馈，修订测试设计文档中被拒绝的决策点。"
                "保留所有已接受和已跳过的决策点不变，只修改被拒绝的决策点。"
            )

        prompt = f"""{instruction}

当前测试设计文档：
{test_content}

用户反馈：
{feedback_text}

请输出完整的修订后测试设计文档，保持 Markdown 格式一致。"""

        refined_test = self.coding_tool.query(
            prompt=prompt,
            system_instruction=TEST_DESIGN_REFINER_SYSTEM_PROMPT,
        )

        self.plan_dir.mkdir(parents=True, exist_ok=True)
        with open(self.test_design_file, "w", encoding="utf-8") as f:
            f.write(refined_test)

        return refined_test

    def _load_test_design(self) -> str:
        with open(self.test_design_file, "r", encoding="utf-8") as f:
            return f.read()

    def _load_rejected_items(self) -> List[dict]:
        import json
        if not self.review_file.exists():
            return []

        with open(self.review_file, "r", encoding="utf-8") as f:
            review = json.load(f)

        return [
            item for item in review.get("items", [])
            if item.get("status") == "rejected"
        ]

    def _build_feedback_text(self, rejected_items: List[dict]) -> str:
        lines = []
        for item in rejected_items:
            title = item.get("title", "未命名决策点")
            note = item.get("note", "无原因")
            lines.append(f"- [{title}] 拒绝原因: {note}")
        return "\n".join(lines)

"""
PRD Refiner — Review-Correction Loop
=====================================

Revises PRD based on rejected review items. Preserves accepted and skipped
decision points, only modifies rejected ones according to review feedback.
Operates on prd.json structured decision points.
"""

import json
from pathlib import Path
from typing import Optional

from coding_tool import CodingTool


REFINER_SYSTEM_PROMPT = """你是一位资深产品经理。你的任务是修订 PRD 文档中**被拒绝的决策点**，保留其他所有决策点不变。

规则：
1. **保留**所有已接受（accepted）和已跳过（skipped）的决策点，原文不动
2. **只修改**被拒绝（rejected）的决策点，根据用户的拒绝原因进行调整
3. 如果用户的拒绝原因涉及方案本身，提供新的 CPEA 分析
4. 如果用户的拒绝原因涉及范围或影响，调整对应内容
5. 保持 CPEA 逻辑链完整：context → problem → evidence → action
6. 输出完整的决策点 JSON 对象

输出格式：
{
  "title": "...",
  "context": "...",
  "problem": "...",
  "evidence": "...",
  "action": "...",
  "impact": "...",
  "alternatives": ["..."]
}"""


class PRDRefiner:
    """Revises PRD based on rejected review items."""

    def __init__(self, coding_tool: CodingTool, plan_dir: Path):
        self.coding_tool = coding_tool
        self.plan_dir = Path(plan_dir)
        self.prd_file = self.plan_dir / "prd.json"
        self.review_file = self.plan_dir / "review.json"

    def refine(self) -> dict:
        """Revise PRD based on rejected items. Returns updated PRD dict."""
        prd_data = self._load_prd()
        rejected_items = self._load_rejected_items()

        if not rejected_items:
            return prd_data

        dps = prd_data.get("decision_points", [])
        for item in rejected_items:
            index = item.get("index", 0)
            if index >= len(dps):
                continue

            current_dp = dps[index]
            feedback = item.get("note", "无原因")
            refined_dp = self._refine_single_decision_point(current_dp, feedback)
            if refined_dp:
                refined_dp["index"] = index
                dps[index] = refined_dp

        self._save_prd(prd_data)
        return prd_data

    def _refine_single_decision_point(self, current_dp: dict, feedback: str) -> Optional[dict]:
        """Call AI to refine a single decision point based on feedback."""
        current_text = json.dumps(current_dp, ensure_ascii=False, indent=2)

        prompt = f"""请根据以下用户的反馈，修订 PRD 决策点。

当前决策点：
{current_text}

用户反馈（拒绝原因）：{feedback}

请输出修订后的决策点 JSON 对象，保持 CPEA 格式。"""

        try:
            refined_dp = self.coding_tool.query_json(
                prompt=prompt,
                system_instruction=REFINER_SYSTEM_PROMPT,
            )
            return refined_dp
        except Exception:
            return None

    def _load_prd(self) -> dict:
        with open(self.prd_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_prd(self, prd_data: dict) -> None:
        with open(self.prd_file, "w", encoding="utf-8") as f:
            json.dump(prd_data, f, ensure_ascii=False, indent=2)

    def _load_rejected_items(self) -> list:
        if not self.review_file.exists():
            return []

        with open(self.review_file, "r", encoding="utf-8") as f:
            review = json.load(f)

        return [
            item for item in review.get("items", [])
            if item.get("status") == "rejected"
        ]

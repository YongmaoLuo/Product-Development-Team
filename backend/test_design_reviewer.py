"""
Test Design Reviewer — Interactive Review for Test Strategy Decisions
======================================================================

Item-by-item test design review with accept/skip/revise actions.
Operates on test-design.md with CPEA format extraction.
"""

import re
import json
from pathlib import Path
from typing import List, Optional

from coding_tool import CodingTool


class TestDesignReviewItem:
    """A single test strategy decision point."""

    def __init__(self, index: int, title: str, content: str):
        self.index = index
        self.title = title
        self.content = content
        self.status = "pending"
        self.note = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "title": self.title,
            "status": self.status,
            "note": self.note,
        }


class TestDesignReviewer:
    """Test design review."""

    def __init__(self, coding_tool: CodingTool, plan_dir: Path):
        self.coding_tool = coding_tool
        self.plan_dir = Path(plan_dir)
        self.test_design_file = self.plan_dir / "test-design.md"
        self.review_file = self.plan_dir / "test-review.json"

    def extract_decision_points(self, test_content: str) -> List[TestDesignReviewItem]:
        """Extract CPEA decision points from test design markdown."""
        items = []
        pattern = re.compile(r"###\s*决策点\s*\d+[:：]\s*(.+?)(?=\n)", re.IGNORECASE)
        matches = list(pattern.finditer(test_content))

        for i, match in enumerate(matches):
            title = match.group(1).strip()
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(test_content)
            content = test_content[start:end].strip()
            items.append(TestDesignReviewItem(index=i, title=title, content=content))

        if items:
            return items

        # Fallback: extract from numbered list under a heading
        overview_pattern = re.compile(r"###\s*\d+\s*个.*决策点\s*\n", re.IGNORECASE)
        overview_match = overview_pattern.search(test_content)
        if overview_match:
            start_pos = overview_match.end()
            next_heading = re.search(r"\n#{2,3}\s", test_content[start_pos:])
            end_pos = start_pos + next_heading.start() if next_heading else len(test_content)
            section = test_content[start_pos:end_pos]
            item_pattern = re.compile(r"^\s*(\d+)\.\s*\*\*(.+?)\*\*\s*[:：—\-]\s*(.+)$", re.MULTILINE)
            for i, m in enumerate(item_pattern.finditer(section)):
                title = m.group(2).strip()
                desc = m.group(3).strip()
                items.append(TestDesignReviewItem(index=i, title=title, content=f"**{title}**：{desc}"))

        return items

    def get_items(self, test_content: str) -> List[TestDesignReviewItem]:
        """Get all review items with existing state applied."""
        items = self.extract_decision_points(test_content)
        review_data = self.load_review()
        if review_data:
            item_map = {i["index"]: i for i in review_data.get("items", [])}
            for item in items:
                if item.index in item_map:
                    item.status = item_map[item.index].get("status", "pending")
                    item.note = item_map[item.index].get("note", "")
        return items

    def submit_action(self, item_index: int, action: str, note: str = "", question: str = "") -> dict:
        """Submit a review action."""
        review_data = self.load_review() or {"items": [], "total": 0}
        items_map = {i["index"]: i for i in review_data.get("items", [])}

        if item_index in items_map:
            item = items_map[item_index]
        else:
            item = {"index": item_index, "title": "", "status": "pending", "note": ""}

        if action == "accept":
            item["status"] = "accepted"
        elif action == "skip":
            item["status"] = "skipped"
        elif action == "revise":
            if question:
                self._revise_single_decision_point(item_index, question)
        elif action == "reset":
            item["status"] = "pending"
            item["note"] = ""

        items_map[item_index] = item
        review_data["items"] = list(items_map.values())
        review_data["total"] = len(review_data["items"])
        review_data["accepted"] = sum(1 for i in review_data["items"] if i.get("status") == "accepted")
        review_data["skipped"] = sum(1 for i in review_data["items"] if i.get("status") == "skipped")

        with open(self.review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, indent=2, ensure_ascii=False)

        return {"status": item["status"], "note": item.get("note", "")}

    def register_new_items(self, new_items: List[dict]) -> None:
        """Register newly appended test-design decision points as
        ``pending`` in test-review.json."""
        review_data = self.load_review() or {"items": []}
        existing_indices = {
            i.get("index") for i in review_data.get("items", [])
        }
        for it in new_items:
            idx = it["index"]
            if idx in existing_indices:
                continue
            review_data.setdefault("items", []).append(
                {
                    "index": idx,
                    "title": it.get("title", ""),
                    "status": "pending",
                    "note": "",
                }
            )
        items = review_data.get("items", [])
        review_data["total"] = len(items)
        review_data["accepted"] = sum(
            1 for i in items if i.get("status") == "accepted"
        )
        review_data["skipped"] = sum(
            1 for i in items if i.get("status") == "skipped"
        )
        review_data["rejected"] = sum(
            1 for i in items if i.get("status") == "rejected"
        )
        with open(self.review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, indent=2, ensure_ascii=False)

    def _revise_single_decision_point(self, item_index: int, feedback: str) -> None:
        """Revise a single decision point in test-design.md from the review feedback."""
        content = self._load_test_content()
        if not content:
            return

        items = self.extract_decision_points(content)
        if item_index >= len(items):
            return

        current_dp = items[item_index].content
        prompt = f"""你是一位资深测试架构师。用户对一个测试策略决策点提出了修改意见，请根据反馈优化该决策点。

当前决策点：
{current_dp}

用户反馈：{feedback}

请输出优化后的完整决策点内容（包含标题 ### 决策点 行和 [C][P][E][A] 各段落），保持 CPEA 框架。只输出决策点内容，不要输出其他内容。"""

        refined = self.coding_tool.query(
            prompt=prompt,
            system_instruction="你是一位测试架构师，正在根据用户反馈优化测试设计决策点。",
        )

        old_dp = items[item_index].content
        updated = content.replace(old_dp, refined.strip() if refined.strip() else old_dp)

        with open(self.test_design_file, "w", encoding="utf-8") as f:
            f.write(updated)

    def get_review_summary(self) -> dict:
        review_data = self.load_review()
        if not review_data:
            return {"total": 0, "accepted": 0, "skipped": 0, "pending": 0}

        items = review_data.get("items", [])
        total = len(items)
        accepted = sum(1 for i in items if i.get("status") == "accepted")
        skipped = sum(1 for i in items if i.get("status") == "skipped")
        pending = total - accepted - skipped
        return {"total": total, "accepted": accepted, "skipped": skipped, "pending": pending}

    def reset_for_refinement(self):
        """Reset review state after refinement."""
        review_data = self.load_review()
        if review_data:
            for item in review_data.get("items", []):
                item["status"] = "pending"
                item["note"] = ""
            review_data["accepted"] = 0
            review_data["rejected"] = 0
            review_data["skipped"] = 0
            with open(self.review_file, "w", encoding="utf-8") as f:
                json.dump(review_data, f, indent=2, ensure_ascii=False)

    def load_review(self) -> Optional[dict]:
        if self.review_file.exists():
            with open(self.review_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def _load_test_content(self) -> str:
        if self.test_design_file.exists():
            with open(self.test_design_file, "r", encoding="utf-8") as f:
                return f.read()
        return ""

"""
Architecture Reviewer — Interactive Review for Architecture Decisions
======================================================================

Item-by-item architecture review with accept/skip/revise actions.
Operates on arch-design.md with CPEA format extraction.
"""

import re
import json
from pathlib import Path
from typing import List, Optional

from coding_tool import CodingTool


class ArchReviewItem:
    """A single architecture decision point."""

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


class ArchReviewer:
    """Architecture design review."""

    def __init__(self, coding_tool: CodingTool, plan_dir: Path):
        self.coding_tool = coding_tool
        self.plan_dir = Path(plan_dir)
        self.arch_file = self.plan_dir / "arch-design.md"
        self.review_file = self.plan_dir / "arch-review.json"

    def extract_decision_points(self, arch_content: str) -> List[ArchReviewItem]:
        """Extract CPEA decision points from architecture markdown."""
        items = []
        pattern = re.compile(r"###\s*决策点\s*\d+[:：]\s*(.+?)(?=\n)", re.IGNORECASE)
        matches = list(pattern.finditer(arch_content))

        for i, match in enumerate(matches):
            title = match.group(1).strip()
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(arch_content)
            content = arch_content[start:end].strip()
            items.append(ArchReviewItem(index=i, title=title, content=content))

        if items:
            return items

        # Fallback: extract from numbered list under a heading
        overview_pattern = re.compile(r"###\s*\d+\s*个.*决策点\s*\n", re.IGNORECASE)
        overview_match = overview_pattern.search(arch_content)
        if overview_match:
            start_pos = overview_match.end()
            next_heading = re.search(r"\n#{2,3}\s", arch_content[start_pos:])
            end_pos = start_pos + next_heading.start() if next_heading else len(arch_content)
            section = arch_content[start_pos:end_pos]
            item_pattern = re.compile(r"^\s*(\d+)\.\s*\*\*(.+?)\*\*\s*[:：—\-]\s*(.+)$", re.MULTILINE)
            for i, m in enumerate(item_pattern.finditer(section)):
                title = m.group(2).strip()
                desc = m.group(3).strip()
                items.append(ArchReviewItem(index=i, title=title, content=f"**{title}**：{desc}"))

        return items

    def get_items(self, arch_content: str) -> List[ArchReviewItem]:
        """Get all review items with existing state applied."""
        items = self.extract_decision_points(arch_content)
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

    def _revise_single_decision_point(self, item_index: int, feedback: str) -> None:
        """Revise a single decision point in arch-design.md from the review feedback."""
        content = self._load_arch_content()
        if not content:
            return

        items = self.extract_decision_points(content)
        if item_index >= len(items):
            return

        current_dp = items[item_index].content
        prompt = f"""你是一位资深系统架构师。用户对一个架构决策点提出了修改意见，请根据反馈优化该决策点。

当前决策点：
{current_dp}

用户反馈：{feedback}

请输出优化后的完整决策点内容（包含标题 ### 决策点 行和 [C][P][E][A] 各段落），保持 CPEA 框架。只输出决策点内容，不要输出其他内容。"""

        revised = self.coding_tool.query(
            prompt=prompt,
            system_instruction="你是一位系统架构师，正在根据用户反馈优化架构设计决策点。",
        )

        # Replace the old decision point in the markdown
        old_dp = items[item_index].content
        refined = revised.strip()
        updated = content.replace(old_dp, refined if refined else old_dp)

        with open(self.arch_file, "w", encoding="utf-8") as f:
            f.write(updated)

    def register_new_items(self, new_items: List[dict]) -> None:
        """Register decision points newly appended by the adder.

        ``new_items`` is a list of ``{"index", "title"}`` dicts (the
        ``content`` field is loaded lazily from arch-design.md on the
        next ``get_items`` call). Existing entries with the same
        index are preserved (so we don't wipe an accepted status).
        """
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

    def _load_arch_content(self) -> str:
        if self.arch_file.exists():
            with open(self.arch_file, "r", encoding="utf-8") as f:
                return f.read()
        return ""

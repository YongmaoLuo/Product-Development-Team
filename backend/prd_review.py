"""
PRD Reviewer — Interactive Review for PRD Decision Points
==========================================================

Item-by-item PRD review with accept/reject/question/skip actions.
Operates on prd.json structured decision points in CPEA format.

Boundary enforcement (DP boundary-fix 2026-07-16)
--------------------------------------------------
Each decision_point carries a ``category`` field that the upstream
PRD_SYSTEM_PROMPT is now required to emit. Validating this field here
prevents PRD from leaking architecture-class decisions (tech_stack /
data_model / interface_design / module_layout / ci_cd_pipeline) into
review before arch review can catch them downstream. See
:data:`PRD_ALLOWED_CATEGORIES` and :meth:`PRDReviewer.validate_prd_categories`.
"""

import json
from pathlib import Path
from typing import List, Optional

from coding_tool import CodingTool
from framework.text import circled_list


# Allowed ``category`` values for PRD decision points, keyed by product_form.
# These come from PRD_SYSTEM_PROMPT_SOFTWARE / _SKILL — keep in sync.
PRD_ALLOWED_CATEGORIES = {
    "software": {
        "requirement",
        "scope_boundary",
        "acceptance_criterion",
        "business_rule",
    },
    "skill": {
        "skill_flow_step",
        "skill_capability_reuse",
        "skill_trigger_condition",
        "skill_error_isolation",
    },
    # agent / script / library currently fall back to the SOFTWARE prompt
    # (see PRD_PROMPTS in prd_generator.py), so the same category set applies.
    "agent": {
        "requirement",
        "scope_boundary",
        "acceptance_criterion",
        "business_rule",
    },
    "script": {
        "requirement",
        "scope_boundary",
        "acceptance_criterion",
        "business_rule",
    },
    "library": {
        "requirement",
        "scope_boundary",
        "acceptance_criterion",
        "business_rule",
    },
}


class PRDReviewItem:
    """A single decision point under review."""

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


class PRDReviewer:
    """Interactive PRD review."""

    def __init__(self, coding_tool: CodingTool, plan_dir: Path):
        self.coding_tool = coding_tool
        self.plan_dir = Path(plan_dir)
        self.prd_file = self.plan_dir / "prd.json"
        self.review_file = self.plan_dir / "review.json"

    def _load_prd(self) -> Optional[dict]:
        """Load prd.json if available."""
        if self.prd_file.exists():
            with open(self.prd_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def _load_prd_legacy(self) -> str:
        """Load legacy prd.md content."""
        md_file = self.plan_dir / "prd.md"
        if md_file.exists():
            with open(md_file, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _save_prd(self, prd_data: dict) -> None:
        """Save prd.json."""
        with open(self.prd_file, "w", encoding="utf-8") as f:
            json.dump(prd_data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _render_decision_point(dp: dict) -> str:
        """Render a decision point as markdown for display."""
        lines = [
            f"**[C] 背景：** {dp.get('context', '')}",
            f"**[P] 问题：** {dp.get('problem', '')}",
            f"**[E] 证据：** {dp.get('evidence', '')}",
            f"**[A] 行动：** {dp.get('action', '')}",
            "",
            f"影响范围：{dp.get('impact', '')}",
        ]
        alts = dp.get("alternatives", [])
        if alts:
            lines.append(f"备选方案：{circled_list(alts)}")
        return "\n\n".join(lines)

    def extract_decision_points(self) -> List[PRDReviewItem]:
        """Extract decision points from prd.json, fallback to legacy prd.md."""
        prd_data = self._load_prd()
        if prd_data:
            items = []
            for dp in prd_data.get("decision_points", []):
                index = dp.get("index", 0)
                title = dp.get("title", "未命名决策点")
                content = self._render_decision_point(dp)
                items.append(PRDReviewItem(index=index, title=title, content=content))
            return items

        # Fallback: legacy markdown PRD
        return self._extract_from_legacy_md()

    def _extract_from_legacy_md(self) -> List[PRDReviewItem]:
        """Extract decision points from legacy PRD markdown."""
        import re
        items = []
        prd_content = self._load_prd_legacy()
        if not prd_content:
            return items

        pattern = re.compile(r"###\s*决策点\s*\d+[:：]\s*(.+?)(?=\n)", re.IGNORECASE)
        matches = list(pattern.finditer(prd_content))

        if matches:
            for i, match in enumerate(matches):
                title = match.group(1).strip()
                start = match.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(prd_content)
                content = prd_content[start:end].strip()
                items.append(PRDReviewItem(index=i, title=title, content=content))
            return items

        # Format 2: Overview "### N 个关键决策点" with numbered list
        overview_pattern = re.compile(r"###\s*\d+\s*个关键决策点\s*\n", re.IGNORECASE)
        overview_match = overview_pattern.search(prd_content)
        if overview_match:
            start_pos = overview_match.end()
            next_heading = re.search(r"\n#{2,3}\s", prd_content[start_pos:])
            end_pos = start_pos + next_heading.start() if next_heading else len(prd_content)
            section = prd_content[start_pos:end_pos]

            item_pattern = re.compile(r"^\s*(\d+)\.\s*\*\*(.+?)\*\*\s*[:：—\-]\s*(.+)$", re.MULTILINE)
            for i, m in enumerate(item_pattern.finditer(section)):
                title = m.group(2).strip()
                desc = m.group(3).strip()
                content = f"**{title}**：{desc}"
                items.append(PRDReviewItem(index=i, title=title, content=content))
            return items

        return items

    def get_items(self) -> List[PRDReviewItem]:
        """Get all review items with existing state applied."""
        items = self.extract_decision_points()
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
                self._refine_single_decision_point(item_index, question)
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

    def _refine_single_decision_point(self, item_index: int, question: str) -> Optional[dict]:
        """Refine a single decision point based on user question and update prd.json."""
        prd_data = self._load_prd()
        if not prd_data:
            return None

        dps = prd_data.get("decision_points", [])
        if item_index >= len(dps):
            return None

        current_dp = dps[item_index]
        current_content = self._render_decision_point(current_dp)

        prompt = f"""你是一位资深产品经理。用户对一个决策点提出了修改意见，请根据用户反馈优化该决策点的内容。

当前决策点内容：
{current_content}

用户反馈：{question}

请输出优化后的决策点，必须是以下 JSON 对象格式：
{{
  "title": "决策点标题",
  "context": "背景与约束",
  "problem": "需要解决的核心问题",
  "evidence": "方案选择的证据与理由",
  "action": "具体决策/行动",
  "impact": "影响范围",
  "alternatives": ["备选方案1", "备选方案2"]
}}

要求：
1. 保持 CPEA 逻辑链完整：context → problem → evidence → action
2. 直接回应用户的反馈
3. 只输出 JSON，不要 markdown 代码块标记"""

        try:
            refined_dp = self.coding_tool.query_json(
                prompt=prompt,
                system_instruction="你是一位产品经理，正在根据用户反馈优化 PRD 决策点。输出合法 JSON。",
            )
            # Preserve index
            refined_dp["index"] = item_index
            # Replace in prd.json
            dps[item_index] = refined_dp
            self._save_prd(prd_data)
            return refined_dp
        except Exception:
            return None

    def register_new_items(self, new_items: List[dict]) -> None:
        """Register newly appended PRD decision points as ``pending``
        in review.json. PRD allows the ``rejected`` status too — but
        newly added items always start at ``pending`` for the user to
        explicitly accept/skip/revise."""
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
        review_data["rejected"] = sum(
            1 for i in items if i.get("status") == "rejected"
        )
        review_data["skipped"] = sum(
            1 for i in items if i.get("status") == "skipped"
        )
        with open(self.review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, indent=2, ensure_ascii=False)

    def get_review_summary(self) -> dict:
        """Return summary of review status."""
        review_data = self.load_review()
        if not review_data:
            return {"total": 0, "accepted": 0, "rejected": 0, "skipped": 0, "pending": 0}

        items = review_data.get("items", [])
        total = len(items)
        accepted = sum(1 for i in items if i.get("status") == "accepted")
        rejected = sum(1 for i in items if i.get("status") == "rejected")
        skipped = sum(1 for i in items if i.get("status") == "skipped")
        pending = total - accepted - rejected - skipped
        return {"total": total, "accepted": accepted, "rejected": rejected, "skipped": skipped, "pending": pending}

    def has_rejections(self) -> bool:
        return self.get_review_summary()["rejected"] > 0

    def all_reviewed(self) -> bool:
        summary = self.get_review_summary()
        return summary["total"] > 0 and summary["pending"] == 0

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

    # --- Boundary enforcement: PRD category validation -----------------

    def _detect_product_form(self) -> str:
        """Read product_form from interview.json (best-effort, defaults to 'software')."""
        interview_file = self.plan_dir / "interview.json"
        if not interview_file.exists():
            return "software"
        try:
            with open(interview_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return "software"
        return (
            data.get("product_form", {}).get("form")
            or data.get("product_form_form")
            or "software"
        )

    def validate_prd_categories(self) -> List[dict]:
        """Walk prd.json decision_points and check ``category`` is PRD-allowed.

        Returns a list of violation dicts::

            {
                "index": int,
                "title": str,
                "current_category": str,   # "<missing>" when absent
                "reason": str,             # human-readable explanation
            }

        Categories listed in :data:`PRD_ALLOWED_CATEGORIES` for the plan's
        detected product_form pass. Everything else (including missing)
        is reported. This list is *advisory* — review actions still flow
        normally — but the violations are persisted into ``review.json``
        under ``category_violations`` so the operator sees them and can
        ``revise`` or ``skip`` flagged items.
        """
        prd_data = self._load_prd()
        if not prd_data:
            return []

        product_form = self._detect_product_form()
        allowed = PRD_ALLOWED_CATEGORIES.get(
            product_form, PRD_ALLOWED_CATEGORIES["software"]
        )

        violations: List[dict] = []
        for dp in prd_data.get("decision_points", []):
            idx = dp.get("index")
            if idx is None:
                continue
            title = dp.get("title", "未命名决策点")
            cat = dp.get("category")
            if not cat:
                violations.append({
                    "index": idx,
                    "title": title,
                    "current_category": "<missing>",
                    "reason": (
                        "PRD 阶段必须为每个决策点填写 category 字段；"
                        f"合法值为 {sorted(allowed)}"
                    ),
                })
            elif cat not in allowed:
                violations.append({
                    "index": idx,
                    "title": title,
                    "current_category": str(cat),
                    "reason": (
                        f"category='{cat}' 属于 arch / test_design 阶段，"
                        f"不应出现在 PRD 决策点中；合法值为 {sorted(allowed)}"
                    ),
                })

        # Persist alongside review.json so the UI / API can surface it.
        review_data = self.load_review() or {"items": [], "total": 0}
        review_data["category_violations"] = violations
        review_data["category_violations_product_form"] = product_form
        with open(self.review_file, "w", encoding="utf-8") as f:
            json.dump(review_data, f, ensure_ascii=False, indent=2)

        return violations

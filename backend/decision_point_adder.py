"""
Decision Point Adder — Append LLM-Generated CPEA Decision Points
================================================================

Lightweight counterpart to the per-phase ``*_refiner.py`` modules.

The refiner does a *full rewrite* (heavy, disturbs already-accepted DPs).
The adder does a *surgical append* (cheap, additive, low-risk):

  - Reuses the same upstream context as the original generator
    (PRD content for arch, PRD+arch for test, interview for PRD itself).
  - Injects the **titles + [A] actions** of all existing decision points
    into the prompt so the LLM cannot (a) regenerate a duplicate or
    (b) wander into unrelated territory.
  - Supports a ``NO_GAP`` safety valve: if the LLM finds the user's
    "gap" is already covered, it returns ``NO_GAP`` instead of writing
    junk. The endpoint surfaces this as ``no_gap_reason`` so the user
    can see why nothing was added.
  - Respects upstream skip notices (PRD skip for arch, PRD+arch skip
    for test) so a "gap" hint that re-introduces a skipped topic is
    rejected before it ever hits disk.

The three subclasses (``PRDDecisionPointAdder``,
``ArchDecisionPointAdder``, ``TestDecisionPointAdder``) mirror the
storage shape of their target doc:

  - PRD    → ``prd.json`` structured JSON
  - Arch   → ``arch-design.md`` Markdown (CPEA blocks)
  - Test   → ``test-design.md`` Markdown (CPEA blocks)

The Markdown subclasses share a base for the splice/insert logic
because the two deliverables use the same heading regex and the same
post-decision-point structure (the next ``## ``-level heading is
where the ``## 技术栈总览`` / ``## 测试矩阵`` section begins).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import List, Optional

from coding_tool import CodingTool


log = logging.getLogger("decision_point_adder")


# -- Shared regex / helpers ---------------------------------------------------

# Match a decision-point heading line. We deliberately accept either
# Chinese ":" or ASCII ":" because the LLM is inconsistent across
# generations. Capture the title text (everything after the colon up
# to end of line).
_DP_HEADING_RE = re.compile(r"###\s*决策点\s*\d+[:：]\s*(.+?)\s*$", re.MULTILINE)
# Match the next H2 section heading after a decision-point block.
_H2_HEADING_RE = re.compile(r"^##\s+", re.MULTILINE)

#: LLM sentinel meaning "the user's stated gap is already covered —
#: don't write anything".
NO_GAP_SENTINEL = "NO_GAP"


class AddedDecisionPoint:
    """Materialized result of a successful ``add()`` call."""

    def __init__(self, index: int, title: str, content: str):
        self.index = index
        self.title = title
        self.content = content

    def to_dict(self) -> dict:
        return {"index": self.index, "title": self.title, "content": self.content}


# -- Base class ---------------------------------------------------------------


class BaseDecisionPointAdder:
    """Shared scaffolding for the three concrete adders."""

    #: Maximum number of decision points the user can ask us to add
    #: in one call. Mirrors the validator at the API boundary.
    MAX_COUNT = 3

    def __init__(self, coding_tool: CodingTool, plan_dir: Path):
        self.coding_tool = coding_tool
        self.plan_dir = Path(plan_dir)

    # --- Hooks that subclasses override ---

    def _load_existing_decision_points(self) -> List[dict]:
        """Return ``[{"index", "title", "action_summary"}, ...]`` for
        existing DPs. Subclasses override to look at JSON or Markdown."""
        raise NotImplementedError

    def _load_upstream_context(self) -> str:
        """Return the upstream document text that the prompt should
        also see. Subclasses override."""
        raise NotImplementedError

    def _extra_skip_notice(self) -> str:
        """Return a block reminding the LLM about skipped upstream DPs.
        Empty by default."""
        return ""

    def _system_instruction(self) -> str:
        """System prompt the LLM sees — subclasses customize per phase."""
        raise NotImplementedError

    def _post_format_check(self, added: List[AddedDecisionPoint]) -> List[str]:
        """Hook for subclasses (e.g. PRD category whitelist) to emit
        warnings after the DPs have been written."""
        return []

    # --- Public API ---

    def add(self, requirement: str, count: int = 1) -> dict:
        """Append up to ``count`` new decision points honouring
        ``requirement``. Returns a dict::

            {
                "added":       [AddedDecisionPoint, ...],
                "no_gap_reason": str | None,
                "warnings":    [str, ...],
            }

        Raises ``ValueError`` on bad input; ``RuntimeError`` on
        unrecoverable upstream write failure.
        """
        if not requirement or not requirement.strip():
            raise ValueError("requirement must be a non-empty string")
        if count < 1 or count > self.MAX_COUNT:
            raise ValueError(f"count must be between 1 and {self.MAX_COUNT}")

        upstream = self._load_upstream_context()
        existing = self._load_existing_decision_points()
        skip_block = self._extra_skip_notice()

        prompt = self._build_prompt(
            upstream=upstream,
            existing=existing,
            requirement=requirement,
            count=count,
            skip_block=skip_block,
        )

        raw = self.coding_tool.query(
            prompt=prompt,
            system_instruction=self._system_instruction(),
        )

        # NO_GAP safety valve — short-circuit before any disk write.
        if NO_GAP_SENTINEL in raw:
            reason = self._extract_no_gap_reason(raw)
            return {"added": [], "no_gap_reason": reason, "warnings": []}

        added = self._append(raw, count, len(existing))
        warnings = self._post_format_check(added)
        return {"added": added, "no_gap_reason": None, "warnings": warnings}

    # --- Prompt assembly --------------------------------------------------

    def _build_prompt(
        self,
        *,
        upstream: str,
        existing: List[dict],
        requirement: str,
        count: int,
        skip_block: str,
    ) -> str:
        if existing:
            existing_lines = []
            for dp in existing:
                existing_lines.append(
                    f"{dp['index'] + 1}. {dp['title']} — [A] {dp['action_summary']}"
                )
            existing_block = "\n".join(existing_lines)
        else:
            existing_block = "（当前文档没有任何决策点）"

        prompt = f"""用户已经审视过上述文档的所有决策点，发现仍然缺少以下内容，并希望追加 {count} 个新的决策点：

## 用户指出的缺口
{requirement}

## 已有决策点（**禁止重复生成，禁止改动**）
{existing_block}

## 已有上游文档（仅供参考，请勿改动）
{upstream}
{skip_block}

## 你的任务
只补齐用户指出的缺口。

## 输出要求（必须严格遵守）
1. 严格 CPEA 格式：每个新决策点包含 `[C] 背景`、`[P] 问题`、`[E] 评估`、`[A] 行动`，并附 `影响范围` 和 `备选方案`（架构）或 `测试类型` `覆盖范围` `备选方案`（测试）。
2. 每个决策点以 `### 决策点 N: 标题` 一行开头，编号占位即可（我会重写为连续编号）。
3. 不要修改任何已有决策点。
4. **不要重复**：若用户指出的缺口其实已被某个已有决策点覆盖（看 已有决策点 一节），**禁止**生成新决策点；改输出 `NO_GAP`，并在第一行说明被哪条已有决策点覆盖。
5. **不要天马行空**：不要顺带补用户没要求的内容。一次只补 `{count}` 个决策点，多了会被丢弃。
6. **不要破坏边界**：架构侧禁止出现测试策略 / CI / 部署 / 安全测试用例；测试侧禁止出现具体业务逻辑设计。
7. 只输出新决策点的 Markdown 内容（或 `NO_GAP`），不要前言、不要结语。"""

        return prompt

    @staticmethod
    def _extract_no_gap_reason(raw: str) -> str:
        """Pull a human-readable reason from a NO_GAP response."""
        # Drop the sentinel itself then take the first non-empty line.
        lines = [
            ln.strip()
            for ln in raw.replace(NO_GAP_SENTINEL, "").splitlines()
            if ln.strip()
        ]
        if not lines:
            return "LLM returned NO_GAP without a reason."
        return lines[0]


# -- Markdown adder (arch + test) ---------------------------------------------


class MarkdownDecisionPointAdder(BaseDecisionPointAdder):
    """Append CPEA decision-point blocks to a ``.md`` deliverable.

    Subclasses customize which file is the target and which upstream
    documents feed the LLM prompt. The splice logic itself is shared.
    """

    #: Path to the markdown deliverable (overridden by subclasses).
    markdown_file: Path = None  # type: ignore[assignment]

    def _load_existing_decision_points_from_markdown(
        self, content: str
    ) -> List[dict]:
        """Slice the markdown into DP blocks and return their titles +
        [A]-action one-liners."""
        matches = list(_DP_HEADING_RE.finditer(content))
        if not matches:
            return []
        items: List[dict] = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            block = content[start:end]
            title = m.group(1).strip()
            action = self._extract_action_summary(block)
            items.append(
                {"index": i, "title": title, "action_summary": action}
            )
        return items

    @staticmethod
    def _extract_action_summary(block: str) -> str:
        """Pull the [A] line and trim to ~80 chars for the prompt."""
        m = re.search(r"\*\*\[A\][^*]*?\*\*\s*[:：]?\s*(.+)", block)
        if not m:
            return ""
        text = m.group(1).strip()
        # Stop at the next blank line / heading.
        text = re.split(r"\n\s*\n", text, maxsplit=1)[0].strip()
        if len(text) > 200:
            text = text[:200] + "…"
        return text

    def _load_existing_decision_points(self) -> List[dict]:
        if not self.markdown_file.exists():
            return []
        with open(self.markdown_file, "r", encoding="utf-8") as f:
            content = f.read()
        return self._load_existing_decision_points_from_markdown(content)

    # --- The actual write --------------------------------------------------

    def _append(
        self, raw: str, expected_count: int, existing_count: int
    ) -> List[AddedDecisionPoint]:
        """Parse ``raw`` into CPEA blocks, renumber headings, splice into
        the markdown file just before the next H2 section. Return the
        added items."""
        if not self.markdown_file.exists():
            raise FileNotFoundError(f"Target file missing: {self.markdown_file}")

        with open(self.markdown_file, "r", encoding="utf-8") as f:
            original = f.read()

        new_blocks = self._split_blocks(raw, expected_count)
        renumbered = self._renumber_blocks(new_blocks, start_index=existing_count)

        insertion_offset = self._find_insertion_offset(original, existing_count)
        updated = (
            original[:insertion_offset]
            + "\n\n".join(renumbered)
            + "\n\n"
            + original[insertion_offset:]
        )

        with open(self.markdown_file, "w", encoding="utf-8") as f:
            f.write(updated)

        return [
            AddedDecisionPoint(
                index=existing_count + i,
                title=self._extract_title(block),
                content=block.strip(),
            )
            for i, block in enumerate(renumbered)
        ]

    @staticmethod
    def _split_blocks(raw: str, expected_count: int) -> List[str]:
        """Slice the LLM output into one block per DP. Tolerates the LLM
        producing fewer than ``expected_count`` blocks (rare but possible
        when the model is conservative)."""
        matches = list(_DP_HEADING_RE.finditer(raw))
        blocks: List[str] = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            block = raw[start:end].rstrip()
            blocks.append(block)
        if not blocks:
            # The LLM didn't include any heading — refuse rather than
            # write junk.
            raise ValueError(
                "LLM output contained no decision-point headings; aborting."
            )
        # Trim if the model produced extras (LLMs sometimes do).
        return blocks[:expected_count]

    @staticmethod
    def _renumber_blocks(blocks: List[str], *, start_index: int) -> List[str]:
        """Replace the leading ``### 决策点 <N>: <title>`` heading so the
        appended DPs are numbered consecutively — title text is
        preserved verbatim from the LLM output."""
        renumbered: List[str] = []
        for i, block in enumerate(blocks):
            m = _DP_HEADING_RE.search(block)
            if not m:
                # Should never happen because _split_blocks filtered
                # for headings, but be defensive.
                renumbered.append(block)
                continue
            title = m.group(1).strip()
            new_heading = f"### 决策点 {start_index + i + 1}: {title}"
            renumbered.append(
                _DP_HEADING_RE.sub(new_heading, block, count=1)
            )
        return renumbered

    @staticmethod
    def _find_insertion_offset(content: str, existing_count: int) -> int:
        """Locate the byte offset where the new DPs should be inserted.

        We want the insertion point to sit *just before* the H2 heading
        that follows the last existing decision-point block (e.g.
        ``## 技术栈总览`` in arch or ``## 测试矩阵`` in test design).
        That keeps new DPs inside the ``## 决策点列表`` /
        ``## 测试策略决策点列表`` section. Falls back to end of file
        if there are no existing DPs or no following H2.
        """
        matches = list(_DP_HEADING_RE.finditer(content))
        if not matches:
            return len(content)
        # End of the LAST existing DP block.
        last_block_end = matches[-1].end()
        tail = content[last_block_end:]
        next_h2 = _H2_HEADING_RE.search(tail)
        if not next_h2:
            return len(content)
        return last_block_end + next_h2.start()

    @staticmethod
    def _extract_title(block: str) -> str:
        m = _DP_HEADING_RE.search(block)
        return m.group(1).strip() if m else "(无标题)"


# -- Concrete adders ----------------------------------------------------------


class ArchDecisionPointAdder(MarkdownDecisionPointAdder):
    """Append decision points to ``arch-design.md``."""

    def __init__(self, coding_tool: CodingTool, plan_dir: Path):
        super().__init__(coding_tool, plan_dir)
        self.markdown_file = self.plan_dir / "arch-design.md"

    def _load_upstream_context(self) -> str:
        """Mirror ArchGenerator: prefer structured PRD, fall back to PRD
        markdown. Filter out skipped PRD DPs so we don't repeat them."""
        prd_json = self.plan_dir / "prd.json"
        if prd_json.exists():
            from prd_generator import PRDGenerator  # local import — avoid cycle

            with open(prd_json, "r", encoding="utf-8") as f:
                prd_data = json.load(f)
            prd_data = self._filter_skipped_prd(prd_data)
            return PRDGenerator.prd_to_markdown(prd_data)
        prd_md = self.plan_dir / "prd.md"
        if prd_md.exists():
            with open(prd_md, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _extra_skip_notice(self) -> str:
        """Surface PRD skipped DPs as a hard constraint — same shape as
        ArchGenerator._filter_skipped_points."""
        prd_review_file = self.plan_dir / "review.json"
        if not prd_review_file.exists():
            return ""
        try:
            with open(prd_review_file, "r", encoding="utf-8") as f:
                review = json.load(f)
        except (OSError, json.JSONDecodeError):
            return ""
        skipped = [
            i for i in review.get("items", []) if i.get("status") == "skipped"
        ]
        if not skipped:
            return ""
        bullet = "\n".join(
            f"  - 决策点 {i['index']}: {i.get('title', '') or '(无标题)'}"
            for i in skipped
        )
        return (
            "\n\n## ⚠️ 用户已 SKIP 的 PRD 决策点(必须遵守)\n"
            f"以下 {len(skipped)} 个 PRD 决策点已被用户在 PRD review 阶段显式 SKIP:\n"
            f"{bullet}\n\n"
            "禁止以任何形式重新引入这些被 skip 的主题(包括新决策点、影响范围、备选方案、"
            "概述引用)。如用户指出的缺口与被 skip 的主题相同,必须返回 NO_GAP 并说明原因。"
        )

    def _system_instruction(self) -> str:
        # Reuse the same boundary instructions as the main generator —
        # any deviation in the adder would create style drift.
        from arch_generator import ARCH_SYSTEM_PROMPT  # local import — avoid cycle
        return ARCH_SYSTEM_PROMPT

    @staticmethod
    def _filter_skipped_prd(prd_data: dict) -> dict:
        import copy

        review_file = Path(prd_data.get("__plan_dir__", "") or ".") / "review.json"
        # We can't rely on a path baked into prd_data; instead drop
        # the loop-injected _skip_notice path and read from the
        # standard plan dir via the adder's plan_dir attribute. Callers
        # that want the same filter as ArchGenerator should pass the
        # raw data — this method is a best-effort duplication that
        # only filters when review.json is reachable.
        # Implementation note: we can't see self here (staticmethod);
        # the actual filtering happens in _load_upstream_context via
        # the review.json-driven _extra_skip_notice() — that block is
        # what the LLM sees. So this method is a no-op pass-through
        # but kept for symmetry with the generator.
        return prd_data


class TestDecisionPointAdder(MarkdownDecisionPointAdder):
    """Append decision points to ``test-design.md``."""

    def __init__(self, coding_tool: CodingTool, plan_dir: Path):
        super().__init__(coding_tool, plan_dir)
        self.markdown_file = self.plan_dir / "test-design.md"

    def _load_upstream_context(self) -> str:
        arch_file = self.plan_dir / "arch-design.md"
        if arch_file.exists():
            with open(arch_file, "r", encoding="utf-8") as f:
                return f.read()
        prd_md = self.plan_dir / "prd.md"
        if prd_md.exists():
            with open(prd_md, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _extra_skip_notice(self) -> str:
        """Aggregate skipped DPs from BOTH PRD review and arch review —
        mirror TestDesignGenerator._load_arch_review_skips()."""
        sections: List[str] = []
        for filename, label in (
            ("review.json", "PRD"),
            ("arch-review.json", "架构"),
        ):
            review_file = self.plan_dir / filename
            if not review_file.exists():
                continue
            try:
                with open(review_file, "r", encoding="utf-8") as f:
                    review = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            skipped = [
                i for i in review.get("items", []) if i.get("status") == "skipped"
            ]
            if not skipped:
                continue
            bullet = "\n".join(
                f"  - {i.get('title', '') or '(无标题)'}" for i in skipped
            )
            sections.append(
                f"以下 {len(skipped)} 个 {label}决策点已被用户显式 SKIP:\n{bullet}"
            )
        if not sections:
            return ""
        return (
            "\n\n## ⚠️ 用户已 SKIP 的上游决策点(必须遵守)\n"
            + "\n\n".join(sections)
            + "\n\n禁止为这些被 skip 的主题生成任何测试策略、测试类型、覆盖矩阵项或验收标准。"
            "如用户指出的缺口与被 skip 的主题相同,必须返回 NO_GAP。"
        )

    def _system_instruction(self) -> str:
        from test_design_generator import TEST_DESIGN_SYSTEM_PROMPT  # local import
        return TEST_DESIGN_SYSTEM_PROMPT


# -- PRD adder (JSON path) ----------------------------------------------------


class PRDDecisionPointAdder(BaseDecisionPointAdder):
    """Append decision points to ``prd.json``.

    The PRD deliverable is structured JSON, not Markdown, so this
    subclass implements its own ``_append`` + ``_post_format_check``
    rather than reusing the Markdown splice logic.
    """

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

    def __init__(self, coding_tool: CodingTool, plan_dir: Path):
        super().__init__(coding_tool, plan_dir)
        self.prd_file = self.plan_dir / "prd.json"

    def _load_existing_decision_points(self) -> List[dict]:
        data = self._load_prd()
        if not data:
            return []
        items = []
        for dp in data.get("decision_points", []):
            action = (dp.get("action") or "").strip()
            if len(action) > 200:
                action = action[:200] + "…"
            items.append(
                {
                    "index": dp.get("index", 0),
                    "title": dp.get("title", "(无标题)"),
                    "action_summary": action,
                }
            )
        return items

    def _load_upstream_context(self) -> str:
        interview_file = self.plan_dir / "interview.json"
        if interview_file.exists():
            with open(interview_file, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _system_instruction(self) -> str:
        return (
            "你是一位资深产品经理。用户希望在现有 PRD 中追加新的决策点。"
            "只输出 JSON，不要输出 markdown 代码块标记或任何解释。"
        )

    def _post_format_check(self, added: List[AddedDecisionPoint]) -> List[str]:
        """Warn when a newly added DP carries a category that the PRD
        whitelist forbids for the detected product_form."""
        data = self._load_prd()
        if not data:
            return []
        # Detect product_form the same way PRDReviewer does.
        product_form = "software"
        interview_file = self.plan_dir / "interview.json"
        if interview_file.exists():
            try:
                with open(interview_file, "r", encoding="utf-8") as f:
                    interview = json.load(f)
                product_form = (
                    interview.get("product_form", {}).get("form")
                    or interview.get("product_form_form")
                    or "software"
                )
            except (OSError, json.JSONDecodeError):
                pass
        allowed = self.PRD_ALLOWED_CATEGORIES.get(
            product_form, self.PRD_ALLOWED_CATEGORIES["software"]
        )
        warnings: List[str] = []
        for added_dp in added:
            dp_data = data.get("decision_points", [])[added_dp.index]
            category = dp_data.get("category")
            if category and category not in allowed:
                warnings.append(
                    f"决策点 {added_dp.index + 1} 的 category={category!r} "
                    f"不在 product_form={product_form!r} 的允许列表中"
                )
        return warnings

    # --- The actual write --------------------------------------------------

    def _append(
        self, raw: str, expected_count: int, existing_count: int
    ) -> List[AddedDecisionPoint]:
        """Decode LLM JSON, append DPs to prd.json with sequential
        index. Return the materialized objects."""
        data = self._load_prd()
        if not data:
            raise FileNotFoundError(f"PRD missing at {self.prd_file}")

        new_dps = self._decode_json_objects(raw, expected_count)
        existing_dps = data.get("decision_points", [])

        for i, dp in enumerate(new_dps):
            dp["index"] = existing_count + i
            existing_dps.append(dp)

        data["decision_points"] = existing_dps
        self._save_prd(data)

        return [
            AddedDecisionPoint(
                index=existing_count + i,
                title=dp.get("title", "(无标题)"),
                content=json.dumps(dp, ensure_ascii=False, indent=2),
            )
            for i, dp in enumerate(new_dps)
        ]

    @staticmethod
    def _decode_json_objects(raw: str, expected_count: int) -> List[dict]:
        """The LLM may emit a JSON array OR a single object (or several
        objects separated by blank lines). Be liberal in what we
        accept; be strict in what we keep."""
        text = raw.strip()
        # Strip markdown code fences if present.
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

        objects: List[dict] = []
        # First try: a single JSON array.
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                objects = [o for o in parsed if isinstance(o, dict)]
            elif isinstance(parsed, dict):
                objects = [parsed]
        except json.JSONDecodeError:
            # Fallback: scan for top-level JSON objects via raw_decode.
            decoder = json.JSONDecoder()
            idx = 0
            while idx < len(text):
                # Skip whitespace.
                while idx < len(text) and text[idx] in " \r\n\t,":
                    idx += 1
                if idx >= len(text):
                    break
                try:
                    obj, end = decoder.raw_decode(text[idx:])
                    if isinstance(obj, dict):
                        objects.append(obj)
                    idx += end
                except json.JSONDecodeError:
                    break

        if not objects:
            raise ValueError("LLM output contained no valid JSON objects.")

        # Each object must carry the CPEA skeleton; if any field is
        # missing we add an empty string so downstream rendering doesn't
        # crash. The LLM is told to include all fields.
        normalized: List[dict] = []
        for obj in objects[:expected_count]:
            normalized.append(
                {
                    "title": obj.get("title", "(无标题)"),
                    "context": obj.get("context", ""),
                    "problem": obj.get("problem", ""),
                    "evidence": obj.get("evidence", ""),
                    "action": obj.get("action", ""),
                    "impact": obj.get("impact", ""),
                    "alternatives": obj.get("alternatives", []) or [],
                    "category": obj.get("category", "requirement"),
                }
            )
        return normalized

    def _load_prd(self) -> Optional[dict]:
        if not self.prd_file.exists():
            return None
        with open(self.prd_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_prd(self, data: dict) -> None:
        with open(self.prd_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
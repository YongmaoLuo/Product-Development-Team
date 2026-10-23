"""Unit tests for the plan-intent section injected into the repair prompt.

Why this section exists
------------------------
The repair path used to see only the verification evidence, so a plan
whose own material says "不碰 Rust" still had Rust files rewritten. The
extractor (:func:`repair_generator._plan_intent`) already pulls those
constraint sentences out of the plan directory; this file pins the two
places they have to land:

1. the user prompt — a "原计划的初衷与硬约束" section, rendered *before*
   the VP list, so the model reads the constraints before it reads the
   failures it is about to fix;
2. the system prompt — rule 7, which declares that section a hard
   constraint and says a conflict has to be reported instead of being
   silently fixed.

What is pinned here
-------------------
* the section appears (with the original wording) when the intent is
  non-empty;
* the section is absent by default and when every entry is blank;
* the section precedes the VP list;
* the system prompt states the constraint / conflict rule;
* ``generate_repair_contents`` falls back to reading the intent from
  the plan directory, and forwards an explicit list unchanged;
* the VP block itself is untouched by the injection.

No network / database — pure local-disk unit tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import repair_generator as rg  # noqa: E402


#: The heading the intent section carries. Pinned as a constant because
#: the system prompt refers to it by name.
INTENT_HEADING = "## 原计划的初衷与硬约束"


def _failed_vp() -> dict:
    """One failed verification point, in the legacy inlined shape."""
    return {
        "id": "VP-1",
        "title": "x",
        "priority": "medium",
        "actual_result": "boom",
        "evidence": "trace",
    }


def _prompt(**kwargs) -> str:
    """Build the repair-content prompt with sane defaults."""
    return rg._build_repair_contents_prompt(
        failed_vps=[_failed_vp()],
        round_number=1,
        plan_dir=Path("/tmp/plan"),
        project_dir=Path("/tmp/proj"),
        **kwargs,
    )


class _RecordingTool:
    """Coding-tool double that records every prompt it is asked."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def query_json(self, prompt, system_instruction, timeout=None):
        self.prompts.append(prompt)
        return {
            "tasks": [{
                "failed_vp_id": "VP-1",
                "title": "T",
                "description": "D",
                "acceptance_criteria": "A",
            }],
        }


# ---------------------------------------------------------------------------
# prompt rendering
# ---------------------------------------------------------------------------


def test_section_rendered_when_intent_provided():
    """A non-empty intent renders the heading and the original wording."""
    prompt = _prompt(plan_intent=["不碰 Rust"])

    assert INTENT_HEADING in prompt
    assert "不碰 Rust" in prompt


def test_section_absent_when_empty():
    """The default argument renders no section at all."""
    prompt = _prompt()

    assert INTENT_HEADING not in prompt


def test_section_precedes_vp_list():
    """The constraints are read before the failures they constrain."""
    prompt = _prompt(plan_intent=["不碰 Rust"])

    assert prompt.index(INTENT_HEADING) < prompt.index("共 1 个失败 VP")


def test_section_absent_when_every_entry_is_blank():
    """Blank entries carry no constraint, so the section is dropped."""
    prompt = _prompt(plan_intent=["", "   "])

    assert INTENT_HEADING not in prompt


def test_entries_are_numbered_in_input_order():
    """The list keeps the order it was handed over in."""
    prompt = _prompt(plan_intent=["不碰 Rust", "只改 Python"])

    assert "1. 不碰 Rust" in prompt
    assert "2. 只改 Python" in prompt
    assert prompt.index("1. 不碰 Rust") < prompt.index("2. 只改 Python")


def test_non_string_entries_are_stringified_and_stripped():
    """A non-string entry is rendered as its stripped string form."""
    prompt = _prompt(plan_intent=[123, "  不碰 Rust  "])

    assert "1. 123" in prompt
    assert "2. 不碰 Rust" in prompt


def test_section_carries_the_conflict_instruction():
    """The section says what to do when a fix would violate a constraint."""
    prompt = _prompt(plan_intent=["不碰 Rust"])

    assert "在做决定之前，先判断这条修复是否与上面任何一条冲突。" in prompt
    assert "冲突时不要生成修复任务" in prompt


def test_vp_block_is_untouched_by_the_injection():
    """Adding the section does not change what the VP block says."""
    without = _prompt()
    with_intent = _prompt(plan_intent=["不碰 Rust"])

    for prompt in (without, with_intent):
        assert "--- VP 1 (VP-1) ---" in prompt
        assert "title: x" in prompt
        assert "actual_result: boom" in prompt
        assert "evidence: trace" in prompt


# ---------------------------------------------------------------------------
# system prompt
# ---------------------------------------------------------------------------


def test_system_prompt_states_hard_constraint():
    """The system prompt declares the section a hard constraint."""
    system_prompt = rg.REPAIR_CONTENT_SYSTEM_PROMPT

    assert "硬约束" in system_prompt
    assert "冲突" in system_prompt


def test_system_prompt_rule_seven_mentions_reporting_conflicts():
    """Rule 7 tells the model to report a conflict, not to fix anyway."""
    system_prompt = rg.REPAIR_CONTENT_SYSTEM_PROMPT

    assert "7." in system_prompt
    assert "上报" in system_prompt


# ---------------------------------------------------------------------------
# generate_repair_contents — pass-through
# ---------------------------------------------------------------------------


def test_generate_repair_contents_reads_intent_from_plan_dir(tmp_path):
    """With no explicit intent, the plan directory is the source."""
    (tmp_path / "import.json").write_text(
        "只改 Python（src/、api/）；不碰 Rust。",
        encoding="utf-8",
    )
    tool = _RecordingTool()

    gen = rg.RepairTaskGenerator(tool, tmp_path, tmp_path / "proj")
    gen.generate_repair_contents([_failed_vp()], round_number=1)

    content_prompt = tool.prompts[0]
    assert INTENT_HEADING in content_prompt
    assert "不碰 Rust" in content_prompt


def test_generate_repair_contents_forwards_explicit_intent(tmp_path):
    """An explicit intent reaches the prompt unchanged."""
    tool = _RecordingTool()

    gen = rg.RepairTaskGenerator(tool, tmp_path, tmp_path / "proj")
    gen.generate_repair_contents(
        [_failed_vp()], round_number=1, plan_intent=["不碰 Rust"],
    )

    content_prompt = tool.prompts[0]
    assert "不碰 Rust" in content_prompt


def test_generate_repair_contents_without_intent_has_no_section(tmp_path):
    """A plan directory without constraints yields no section."""
    tool = _RecordingTool()

    gen = rg.RepairTaskGenerator(tool, tmp_path, tmp_path / "proj")
    gen.generate_repair_contents([_failed_vp()], round_number=1)

    assert INTENT_HEADING not in tool.prompts[0]


if __name__ == "__main__":  # pragma: no cover - manual run helper
    raise SystemExit(pytest.main([__file__, "-v"]))

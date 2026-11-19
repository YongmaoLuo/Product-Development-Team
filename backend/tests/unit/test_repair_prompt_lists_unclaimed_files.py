"""Unit tests for the unclaimed-failing-files section of the repair prompt.

Why this section exists
-----------------------
Some files fail in the verification round but are claimed by *no* repair
task: no verification point names them, so the repair-content generator
never saw them and never produced a task that touches them. Before this
section existed the model could only read the repair candidates — a
"unclaimed file" was invisible, so it stayed broken for round after
round.

The fix is to hand the list to the refiner explicitly: the section lists
the unclaimed files and tells the model to either produce a repair task
for each of them or fold them into an existing task's
``files_to_modify``.

What is pinned here
-------------------
* the section appears with its heading and every listed path;
* the section is absent for ``None`` / empty input (and the helper
  returns an empty string);
* duplicate paths are collapsed and a pytest node id
  (``path::Class::test``) is truncated to its file path, order preserved;
* ``generate_repair_contents`` forwards the list into the prompt it
  dispatches.

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


#: The heading the unclaimed-files section carries.
SECTION_HEADING = "[UNCLAIMED FAILING FILES]"

#: What the section asks the model to do with the listed files.
SECTION_INSTRUCTION = (
    "为每个文件产出修复任务，或把它并入某个修复任务的 files_to_modify"
)


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


def test_prompt_lists_unclaimed_files():
    """A non-empty list renders the heading and every listed path."""
    prompt = _prompt(
        unclaimed_files=["backend/coding_tool.py", "backend/agent.py"],
    )

    assert SECTION_HEADING in prompt
    assert "backend/coding_tool.py" in prompt
    assert "backend/agent.py" in prompt
    assert SECTION_INSTRUCTION in prompt


def test_no_section_when_empty():
    """An empty list renders no section at all."""
    prompt = _prompt(unclaimed_files=[])

    assert SECTION_HEADING not in prompt


def test_no_section_when_none():
    """The default argument renders no section at all."""
    prompt = _prompt()

    assert SECTION_HEADING not in prompt


def test_section_dedupes_paths():
    """Duplicates collapse and a node id is truncated to its file path."""
    section = rg._unclaimed_files_section([
        "backend/a.py",
        "backend/a.py",
        "backend/tests/unit/test_b.py::TestX::test_y",
    ])

    assert section.count("- backend/a.py") == 1
    assert "- backend/tests/unit/test_b.py" in section
    # The node id never survives into the section: only file paths do.
    assert "::" not in section


def test_section_preserves_input_order():
    """The list keeps the order it was handed over in."""
    section = rg._unclaimed_files_section(
        ["backend/b.py", "backend/a.py"],
    )

    assert section.index("- backend/b.py") < section.index("- backend/a.py")


def test_section_helper_returns_empty_string_for_no_input():
    """``None`` and an empty list both render to an empty string."""
    assert rg._unclaimed_files_section(None) == ""
    assert rg._unclaimed_files_section([]) == ""


def test_section_helper_drops_blank_entries():
    """A blank entry names no file, so it carries nothing."""
    section = rg._unclaimed_files_section(["", "   ", "backend/a.py"])

    assert SECTION_HEADING in section
    assert section.count("\n- ") == 1
    assert "- backend/a.py" in section


def test_prompt_dedupes_paths():
    """The prompt carries the de-duplicated list, not the raw input."""
    prompt = _prompt(
        unclaimed_files=[
            "backend/a.py",
            "backend/a.py",
            "backend/b.py::test_x",
        ],
    )

    assert prompt.count("- backend/a.py") == 1
    assert "- backend/b.py" in prompt
    assert "backend/b.py::test_x" not in prompt


# ---------------------------------------------------------------------------
# generate_repair_contents — pass-through
# ---------------------------------------------------------------------------


def test_generate_repair_contents_forwards_unclaimed_files(tmp_path):
    """The list reaches the prompt the generator dispatches."""
    tool = _RecordingTool()

    gen = rg.RepairTaskGenerator(tool, tmp_path, tmp_path / "proj")
    gen.generate_repair_contents(
        [_failed_vp()],
        round_number=1,
        unclaimed_files=["backend/coding_tool.py"],
    )

    content_prompt = tool.prompts[0]
    assert SECTION_HEADING in content_prompt
    assert "backend/coding_tool.py" in content_prompt


def test_generate_repair_contents_without_unclaimed_files_has_no_section(tmp_path):
    """The default list renders no section."""
    tool = _RecordingTool()

    gen = rg.RepairTaskGenerator(tool, tmp_path, tmp_path / "proj")
    gen.generate_repair_contents([_failed_vp()], round_number=1)

    assert SECTION_HEADING not in tool.prompts[0]


if __name__ == "__main__":  # pragma: no cover - manual run helper
    raise SystemExit(pytest.main([__file__, "-v"]))

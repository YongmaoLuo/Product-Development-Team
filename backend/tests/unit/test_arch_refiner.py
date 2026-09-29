"""
TDD tests for ``ArchRefiner.refine`` — direct feedback path.

Background
----------
``ArchRefiner.refine`` used to return the original architecture document
unchanged whenever ``arch-review.json`` had no rejected items. In the Web
UI / the workflow flow, review state can be missing while the caller still
sends free-form feedback in the HTTP request body. The endpoint already had
a ``RefineRequest(feedback=...)`` model, but the refiner ignored the field.

TDD spec
--------
1. ``test_refine_uses_direct_feedback_when_no_rejected_items``:
   When no ``arch-review.json`` exists but ``feedback`` is supplied,
   ``coding_tool.query`` is invoked and the prompt contains the feedback.

2. ``test_refine_returns_original_when_no_feedback_and_no_rejected_items``:
   The original short-circuit behaviour is preserved when there is truly
   nothing to act on.

3. ``test_refine_combines_rejected_items_and_direct_feedback``:
   When both rejected items and direct feedback are present, both appear
   in the prompt sent to the coding tool.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


@pytest.fixture
def sample_plan(tmp_path):
    """Create a minimal plan directory with an architecture design file."""
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    arch_file = plan_dir / "arch-design.md"
    arch_file.write_text("# Architecture\n\nInitial design.", encoding="utf-8")
    return plan_dir


def _make_refiner(plan_dir, refined_text="# Architecture\n\nRefined design."):
    coding_tool = MagicMock()
    coding_tool.query.return_value = refined_text
    from arch_refiner import ArchRefiner

    return ArchRefiner(coding_tool, plan_dir), coding_tool


def test_refine_uses_direct_feedback_when_no_rejected_items(sample_plan):
    refiner, coding_tool = _make_refiner(sample_plan)

    result = refiner.refine(feedback="Use a flat module structure.")

    assert coding_tool.query.call_count == 1
    prompt = coding_tool.query.call_args.kwargs["prompt"]
    assert "Use a flat module structure." in prompt
    assert result == "# Architecture\n\nRefined design."
    # The refined content must be written back to disk.
    assert (sample_plan / "arch-design.md").read_text(encoding="utf-8") == result


def test_refine_returns_original_when_no_feedback_and_no_rejected_items(sample_plan):
    refiner, coding_tool = _make_refiner(sample_plan)

    result = refiner.refine()

    assert coding_tool.query.call_count == 0
    assert result == "# Architecture\n\nInitial design."


def test_refine_combines_rejected_items_and_direct_feedback(sample_plan):
    review = {
        "items": [
            {
                "index": 0,
                "title": "Module depth",
                "status": "rejected",
                "note": "Too many layers",
            }
        ]
    }
    (sample_plan / "arch-review.json").write_text(
        json.dumps(review), encoding="utf-8"
    )

    refiner, coding_tool = _make_refiner(sample_plan)
    result = refiner.refine(feedback="Also use dependency injection.")

    assert coding_tool.query.call_count == 1
    prompt = coding_tool.query.call_args.kwargs["prompt"]
    assert "Module depth" in prompt
    assert "Too many layers" in prompt
    assert "Also use dependency injection." in prompt
    assert result == "# Architecture\n\nRefined design."

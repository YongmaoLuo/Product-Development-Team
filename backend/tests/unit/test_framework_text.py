"""Tests for :mod:`framework.text`.

The regression these guard
--------------------------
A decision point's ``备选方案`` line is rendered in two places — the PRD
generator (into ``prd.md``) and the PRD reviewer (into the review surface
the operator approves). Both hand-rolled the join with the bullet
**hard-coded** to ``①``:

    '；'.join(f'① {a}' for a in alts)

so a decision point with two alternatives rendered as ``① 甲；① 乙`` —
two mutually exclusive options that read as the same option, and the
reviewer cannot tell which is which. The two copies are what let it
survive a self-review pass: the pass fixed the markdown it was reading
while the review surface re-introduced the defect from ``prd.json``.

These tests pin the shared helper both call sites now use.
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from framework.text import circled_list  # noqa: E402


def test_each_alternative_gets_its_own_marker():
    """The bug: every entry carried ①, so two options looked like one."""
    rendered = circled_list(["方案甲", "方案乙"])

    assert rendered == "① 方案甲；② 方案乙"
    assert rendered.count("①") == 1, (
        "the first bullet must appear exactly once; a second '①' means the "
        "marker is not derived from the item's position"
    )
    assert "②" in rendered


def test_three_alternatives_are_all_distinct():
    rendered = circled_list(["a", "b", "c"])

    assert rendered == "① a；② b；③ c"


def test_empty_input_renders_empty_string():
    """Callers test the result, not the input."""
    assert circled_list([]) == ""


def test_single_alternative_is_still_marked():
    assert circled_list(["only"]) == "① only"


def test_past_the_tenth_falls_back_to_parenthesised_number():
    """``⑪`` is not reliably present in every font, so don't emit it."""
    rendered = circled_list([str(n) for n in range(1, 13)])

    assert rendered.endswith("⑩ 10；(11) 11；(12) 12")
    assert rendered.count("①") == 1


def test_renderers_do_not_hardcode_the_bullet():
    """Neither call site may reintroduce the hand-rolled join.

    A source-level gate rather than a behaviour test: the defect was a
    duplicated expression, and the way it comes back is someone pasting
    the old line into a third renderer.
    """
    generators = [
        _BACKEND_DIR / "prd_generator.py",
        _BACKEND_DIR / "prd_review.py",
    ]
    for path in generators:
        source = path.read_text(encoding="utf-8")
        assert "join(f'① " not in source, (
            f"{path.name} hard-codes the alternative bullet again; use "
            f"framework.text.circled_list so both surfaces agree"
        )

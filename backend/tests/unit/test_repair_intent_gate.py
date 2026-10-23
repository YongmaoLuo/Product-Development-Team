"""Unit tests for the repair intent gate.

Why this exists
---------------
The repair prompt asks the model to judge whether a proposed fix crosses
one of the plan's own hard constraints, and the answer comes back as the
structured ``intent_check`` block (see
``test_repair_intent_check_parse.py``). The plan's constraint sentences
are *also* re-read deterministically — the regions they put off limits
are extracted, and the repair's declared files are checked against those
regions by path segment (see ``test_repair_intent_regions.py``).

:func:`repair_generator.apply_intent_gate` is where the two signals
become one gate. A row either signal rejects does not become a repair
task, and every rejection is *recorded* as a conflict rather than raised
as an error: a plan that names a constraint the repair cannot respect
has to be reported to the operator, not crashed on. The model stays the
primary signal; the deterministic check is the floor under it (R5).

What is pinned here
-------------------
* the model's own verdict (``violates=true``) drops the row;
* a deterministic hit on a banned region drops the row even when the
  model said the fix was fine;
* the conflict record has a fixed shape — ``vp_id`` / ``constraint`` /
  ``reason`` — so the caller can report it without guessing;
* a clean row, a plan with no constraints at all, and a row that
  declares no scope all survive;
* the gate never rewrites or deletes the paths the model declared (R6);
* every deterministic conflict is printed as a WARNING.

No network / database — pure in-process unit tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import repair_generator as rg  # noqa: E402


#: The constraint sentence the task brief uses as its worked example.
PLAN_INTENT = ["不碰 Rust"]

#: The two rows the brief works with: VP-1 wants to touch the Rust
#: crate the plan puts off limits, VP-2 wants to touch the backend.
EXAMPLE_CONTENTS = [
    {
        "failed_vp_id": "VP-1",
        "intent_check": {"violates": False},
        "files_to_modify": ["engine_rs/src/signals.rs"],
    },
    {
        "failed_vp_id": "VP-2",
        "intent_check": {"violates": False},
        "files_to_modify": ["backend/repair_generator.py"],
    },
]


# ---------------------------------------------------------------------------
# The model's own verdict
# ---------------------------------------------------------------------------


def test_model_violation_is_dropped():
    """``violates=true`` means the row never becomes a repair task."""
    contents = [
        {
            "failed_vp_id": "VP-1",
            "intent_check": {
                "violates": True,
                "constraint": "不碰 Rust",
                "reason": "必须改 engine_rs",
            },
            "files_to_modify": ["engine_rs/src/signals.rs"],
        },
        {
            "failed_vp_id": "VP-2",
            "intent_check": {"violates": False},
            "files_to_modify": ["backend/repair_generator.py"],
        },
    ]

    kept, conflicts = rg.apply_intent_gate(contents, PLAN_INTENT)

    assert [row["failed_vp_id"] for row in kept] == ["VP-2"]
    assert len(conflicts) == 1
    assert conflicts[0]["vp_id"] == "VP-1"
    assert conflicts[0]["constraint"] == "不碰 Rust"


def test_model_violation_is_dropped_even_without_a_constraint():
    """A model that reports a violation without naming the constraint
    is still a violation — the row is dropped and a conflict recorded."""
    contents = [
        {
            "failed_vp_id": "VP-1",
            "intent_check": {"violates": True, "constraint": "", "reason": ""},
        },
    ]

    kept, conflicts = rg.apply_intent_gate(contents, [])

    assert kept == []
    assert len(conflicts) == 1
    assert conflicts[0]["vp_id"] == "VP-1"
    assert conflicts[0]["reason"]


# ---------------------------------------------------------------------------
# The deterministic floor under the model's judgement
# ---------------------------------------------------------------------------


def test_deterministic_mismatch_is_forced():
    """The model said it was fine; the path check says it was not.

    The model stays the primary signal, but a row whose declared file
    falls into a region the plan puts off limits is dropped anyway —
    that is the whole point of the deterministic floor.
    """
    kept, conflicts = rg.apply_intent_gate(EXAMPLE_CONTENTS, PLAN_INTENT)

    assert [row["failed_vp_id"] for row in kept] == ["VP-2"]
    assert conflicts == [
        {
            "vp_id": "VP-1",
            "constraint": "不碰 Rust",
            "reason": (
                "模型判断与确定性检查不一致："
                "files_to_modify 命中禁改区域 rust"
            ),
        },
    ]


def test_rust_file_is_blocked_without_a_private_directory_name():
    """The plan says "不碰 Rust" — a Rust file is Rust, wherever it lives.

    The declared file names no project directory at all: it is a
    ``.rs`` file under a generic crate path, and it still crosses the
    constraint the plan stated.
    """
    contents = [
        {
            "failed_vp_id": "VP-1",
            "intent_check": {"violates": False},
            "files_to_modify": ["crates/core/src/lib.rs"],
        },
    ]

    kept, conflicts = rg.apply_intent_gate(contents, PLAN_INTENT)

    assert kept == []
    assert len(conflicts) == 1
    assert conflicts[0]["vp_id"] == "VP-1"
    assert conflicts[0]["constraint"] == "不碰 Rust"
    assert "模型判断与确定性检查不一致" in conflicts[0]["reason"]


def test_deterministic_mismatch_does_not_mutate_the_dropped_row():
    """The gate drops a row; it does not rewrite what the model said."""
    contents = [
        {
            "failed_vp_id": "VP-1",
            "intent_check": {"violates": False},
            "files_to_modify": ["engine_rs/src/signals.rs"],
        },
    ]

    rg.apply_intent_gate(contents, PLAN_INTENT)

    assert contents[0]["files_to_modify"] == ["engine_rs/src/signals.rs"]
    assert contents[0]["intent_check"] == {"violates": False}


def test_mismatch_prints_warning(capsys):
    """A forced rejection is printed — it is a decision, not a silence."""
    rg.apply_intent_gate(EXAMPLE_CONTENTS, PLAN_INTENT)

    captured = capsys.readouterr()

    assert "WARNING" in captured.out
    assert "模型判断与确定性检查不一致" in captured.out
    assert "VP-1" in captured.out


# ---------------------------------------------------------------------------
# Rows that survive
# ---------------------------------------------------------------------------


def test_clean_content_is_kept():
    """A row whose scope misses every banned region is untouched."""
    contents = [
        {
            "failed_vp_id": "VP-9",
            "intent_check": {"violates": False},
            "files_to_modify": ["backend/repair_generator.py"],
        },
    ]

    kept, conflicts = rg.apply_intent_gate(contents, PLAN_INTENT)

    assert kept == contents
    assert conflicts == []


def test_empty_plan_intent_keeps_content_with_unknown_files():
    """No constraint means no region to hit — only the model can object."""
    contents = [
        {
            "failed_vp_id": "VP-1",
            "intent_check": {"violates": False},
            "files_to_modify": ["engine_rs/src/signals.rs"],
        },
    ]

    kept, conflicts = rg.apply_intent_gate(contents, [])

    assert kept == contents
    assert conflicts == []


def test_content_without_declared_scope_is_kept():
    """A row that declares no file carries no evidence of a violation."""
    contents = [
        {
            "failed_vp_id": "VP-1",
            "intent_check": {"violates": False},
        },
    ]

    kept, conflicts = rg.apply_intent_gate(contents, PLAN_INTENT)

    assert kept == contents
    assert conflicts == []


def test_missing_intent_check_is_read_as_no_violation():
    """A row the model did not judge is not a row the model rejected."""
    contents = [
        {
            "failed_vp_id": "VP-1",
            "files_to_modify": ["backend/repair_generator.py"],
        },
    ]

    kept, conflicts = rg.apply_intent_gate(contents, PLAN_INTENT)

    assert kept == contents
    assert conflicts == []


def test_gate_keeps_the_order_of_the_rows_it_keeps():
    """The gate filters; it does not reorder."""
    contents = [
        {
            "failed_vp_id": "VP-1",
            "intent_check": {"violates": False},
            "files_to_modify": ["backend/a.py"],
        },
        {
            "failed_vp_id": "VP-2",
            "intent_check": {"violates": False},
            "files_to_modify": ["engine_rs/src/signals.rs"],
        },
        {
            "failed_vp_id": "VP-3",
            "intent_check": {"violates": False},
            "files_to_modify": ["backend/b.py"],
        },
    ]

    kept, conflicts = rg.apply_intent_gate(contents, PLAN_INTENT)

    assert [row["failed_vp_id"] for row in kept] == ["VP-1", "VP-3"]
    assert [conflict["vp_id"] for conflict in conflicts] == ["VP-2"]


if __name__ == "__main__":  # pragma: no cover - manual run helper
    raise SystemExit(pytest.main([__file__, "-v"]))

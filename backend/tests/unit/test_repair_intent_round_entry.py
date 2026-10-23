"""Unit tests for the intent facts a verification round snapshot must carry.

Why this exists
---------------
The intent gate (R4) drops a repair row that crosses the plan's own
constraint, and reports the dropped row on the round's *result* as
``intent_conflicts`` / ``needs_operator_decision``. That result lives in
memory for the length of one round: ``verification_repair_tasks.json``
and the in-memory ``_verification_state`` entry never learned about it,
so a conflict was gone the moment the round ended — and a server restart
erased the only trace an operator could have read.

What is pinned here
-------------------
* the round entry written to ``verification_repair_tasks.json`` carries
  the conflicts and a summary flag;
* the flag is True exactly when the round has a conflict to report;
* BOTH write sites (the repair-resume callback and the auto-loop) go
  through the same two helpers, so the snapshot cannot drift between
  them;
* the degenerate inputs the round can hand over (a result that is not a
  dict, a result without the fields, a ``tasks`` value that is not a
  list) produce an empty conflict list and a False flag instead of
  raising.

No network / database — pure in-process unit tests.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: The result a round hands over when the intent gate dropped a row.
CONFLICT_RESULT = {
    "intent_conflicts": [
        {"vp_id": "VP-1", "constraint": "不碰 Rust", "reason": "x"},
    ],
    "needs_operator_decision": True,
}

#: The result of a round where nothing crossed the constraint.
CLEAN_RESULT = {
    "intent_conflicts": [],
    "needs_operator_decision": False,
}


# ---------------------------------------------------------------------------
# The round entry
# ---------------------------------------------------------------------------


def test_round_entry_carries_intent_conflicts():
    """The round entry — the thing written to
    ``verification_repair_tasks.json`` — carries the conflicts the round
    reported, so they survive the round and a restart."""
    from verification_loop import _build_repair_round_entry

    entry = _build_repair_round_entry(2, [], CONFLICT_RESULT)

    assert entry["round"] == 2
    assert entry["tasks"] == []
    assert entry["intent_conflicts"] == [
        {"vp_id": "VP-1", "constraint": "不碰 Rust", "reason": "x"},
    ]
    assert entry["generated_at"], "the round entry has no timestamp"


def test_summary_flag_true():
    """A round with a conflict asks for an operator decision."""
    from verification_loop import _build_repair_round_entry

    entry = _build_repair_round_entry(2, [], CONFLICT_RESULT)

    assert entry["summary"]["needs_operator_decision"] is True


def test_summary_flag_false_without_conflicts():
    """A clean round must not be handed a decision to make."""
    from verification_loop import _build_repair_round_entry

    entry = _build_repair_round_entry(2, [], CLEAN_RESULT)

    assert entry["intent_conflicts"] == []
    assert entry["summary"]["needs_operator_decision"] is False


# ---------------------------------------------------------------------------
# Both write sites go through the same helpers
# ---------------------------------------------------------------------------


def test_both_writers_use_helpers():
    """The repair-resume callback and the auto-loop must not each grow
    their own idea of what a round entry looks like — one helper builds
    the entry, one helper extracts the intent facts."""
    import verification_loop as vl

    resume_src = inspect.getsource(vl._make_repair_on_complete)
    loop_src = inspect.getsource(vl._run_auto_verification_loop_inner)

    for name, src in (("repair-resume", resume_src), ("auto-loop", loop_src)):
        assert "_build_repair_round_entry" in src, (
            f"{name} does not build its round entry through the helper"
        )
        assert "_repair_intent_round_fields" in src, (
            f"{name} does not read the intent facts through the helper"
        )


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------


def test_non_dict_result_yields_empty_conflicts():
    """A result that is not a dict carries no conflicts — and does not
    raise on the way to the snapshot."""
    from verification_loop import _repair_intent_round_fields

    fields = _repair_intent_round_fields(None)

    assert fields["intent_conflicts"] == []
    assert fields["summary"]["needs_operator_decision"] is False


def test_result_without_fields_yields_empty_conflicts():
    """A result from a round that predates the gate simply has no
    conflicts to report."""
    from verification_loop import _repair_intent_round_fields

    fields = _repair_intent_round_fields({"repair_tasks": []})

    assert fields["intent_conflicts"] == []
    assert fields["summary"]["needs_operator_decision"] is False


def test_non_list_tasks_become_empty_list():
    """A ``tasks`` value that is not a list is dropped, not written out
    as-is — the file must stay readable by the round aggregation."""
    from verification_loop import _build_repair_round_entry

    entry = _build_repair_round_entry(3, "not a list", CONFLICT_RESULT)

    assert entry["tasks"] == []
    assert entry["intent_conflicts"] == CONFLICT_RESULT["intent_conflicts"]


def test_conflicts_without_the_flag_still_raise_it():
    """The flag is derived from the conflicts when the result does not
    carry one — a conflict must never be silently swallowed."""
    from verification_loop import _repair_intent_round_fields

    fields = _repair_intent_round_fields({
        "intent_conflicts": CONFLICT_RESULT["intent_conflicts"],
    })

    assert fields["summary"]["needs_operator_decision"] is True


if __name__ == "__main__":  # pragma: no cover - manual run helper
    raise SystemExit(pytest.main([__file__, "-v"]))

"""2026-10-23: ``stopped`` must be reachable from every stoppable phase.

A stop that cannot park the phase leaves a plan that still reads as
live. The stop endpoint's own transition used to raise
``ValueError`` from a phase with no edge to ``stopped`` — and the
exception was swallowed, so the endpoint answered a clean 200 for a
stop that did not hold.

What is pinned here:

  1. every phase an operator can stop a plan in has an edge to
     ``stopped``;
  2. ``completed`` still has NO edge to ``stopped`` — a finished plan
     is not "stopped", it is finished;
  3. ``verification_passed`` stays an empty row (the security
     boundary) and ``verification_loop_stopped`` is untouched;
  4. ``stopped`` stays a terminal phase whose only exits are the
     manual resume (``executing`` / ``queued``; ``ready`` is the
     legacy input alias of ``queued``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plan_state import (
    PHASE_TRANSITIONS,
    TERMINAL_PHASES,
    VALID_PHASES,
    VERIFICATION_PHASE_TRANSITIONS,
    PlanState,
)


def _write_plan_state(plan_dir: Path, phase: str) -> None:
    (plan_dir / "plan_state.json").write_text(
        json.dumps({
            "plan_id": "p",
            "current_phase": phase,
            "completed_phases": ["execution"],
            "review_rounds": {"prd": 0, "arch": 0, "test": 0},
            "flags": {"arch_enabled": False, "test_enabled": False},
        }),
        encoding="utf-8",
    )


@pytest.fixture
def plan_dir(tmp_path):
    d = tmp_path / "plan"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# 1) every stoppable phase can reach ``stopped``
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase",
    [
        "verification_repairing",
        "verification_failed",
        "verification",
        "verification_running",
        "verify_first_pass",
        "verify_recheck",
        "failed",
        "ready",
        "queued",
        "executing",
    ],
)
def test_transition_to_stopped_succeeds(plan_dir, phase):
    """Every phase an operator can stop a plan in can be parked."""
    _write_plan_state(plan_dir, phase)
    ps = PlanState(plan_dir)
    ps.reload()

    ps.transition_to("stopped")

    assert ps.get_current_phase() == "stopped", (
        f"{phase} -> stopped must be a legal transition"
    )


def test_transition_to_stopped_from_completed_still_raises(plan_dir):
    """``completed`` has no edge to ``stopped`` — and must keep none.

    A finished plan is not "stopped". The stop endpoint reports the
    refusal (``phase_transition_error``) instead of pretending the
    stop held.
    """
    _write_plan_state(plan_dir, "completed")
    ps = PlanState(plan_dir)
    ps.reload()

    with pytest.raises(ValueError):
        ps.transition_to("stopped")


# ---------------------------------------------------------------------------
# 2) the table itself
# ---------------------------------------------------------------------------


def test_the_tables_declare_the_stopped_edges():
    """The edges are declared, not just reachable by accident."""
    assert "stopped" in PHASE_TRANSITIONS["failed"], PHASE_TRANSITIONS["failed"]
    for phase in (
        "verification",
        "verification_running",
        "verify_first_pass",
        "verify_recheck",
        "verification_failed",
        "verification_repairing",
    ):
        assert "stopped" in VERIFICATION_PHASE_TRANSITIONS[phase], (
            f"{phase} must declare a 'stopped' edge; it has "
            f"{VERIFICATION_PHASE_TRANSITIONS[phase]!r}"
        )


def test_verification_passed_stays_terminal():
    """``verification_passed`` keeps its empty row.

    It is the security boundary: a passing verdict may not be
    laundered into a terminal phase by any edge, and the stop must
    not open one.
    """
    assert VERIFICATION_PHASE_TRANSITIONS["verification_passed"] == []


def test_verification_loop_stopped_is_untouched():
    """``verification_loop_stopped`` keeps exactly its two edges."""
    assert VERIFICATION_PHASE_TRANSITIONS["verification_loop_stopped"] == [
        "failed", "verification_passed",
    ]


def test_stopped_is_terminal_with_only_manual_exits():
    """``stopped`` is terminal — the exits are the manual resume.

    2026-10-25 (R10): 等待相位唯一拼写是 ``queued`` —— ``ready`` 只作输入
    别名（磁盘上的历史状态照旧可读），所以「回到等人」这条边的落点是
    ``queued``。``ready`` 仍然留在表里：既有行照旧要能读，所以这里是
    「集合含三个值」，不是「只剩 executing / queued」。
    """
    assert "stopped" in TERMINAL_PHASES
    assert "stopped" in VALID_PHASES
    assert set(PHASE_TRANSITIONS["stopped"]) == {"executing", "ready", "queued"}
    assert set(VERIFICATION_PHASE_TRANSITIONS["stopped"]) == {
        "executing", "ready", "queued",
    }


def test_a_stopped_plan_can_be_resumed(plan_dir):
    """The manual resume out of ``stopped`` still works."""
    _write_plan_state(plan_dir, "stopped")
    ps = PlanState(plan_dir)
    ps.reload()

    ps.transition_to("executing")
    assert ps.get_current_phase() == "executing"

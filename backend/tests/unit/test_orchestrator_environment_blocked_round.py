"""RED regression tests: a round that judged nothing must not dispatch repairs.

Background
----------
``check_cycle_conditions`` routes a round on one word: ``overall_status``.
``PASSED`` -- or a ``PARTIAL`` with no failures -- ends the round as a
pass; **everything else falls into the failure path**: the plan is stamped
``verification_failed``, the repair channel is opened, and the repair model
is asked to rework the deliverable for every point that failed.

A round that could not judge what it set out to judge is not a failure of
the deliverable.  The report says so in three places:

* the blocked point carries the reason code the environment owns
  (``service_data_plane_mismatch``);
* the round's verdict is ``SKIPPED`` -- not ``PASSED`` (R4,
  ``verification_agent._derive_overall_status``);
* the accounting layer flagged the round itself with
  ``environment_blocked``.

On the base point such a round still falls into the failure path.  Nothing
was broken, so there is nothing to repair -- but the round asks the repair
model to fix the product for a defect nobody found.  The plan is then
recorded as a failed verification and handed to the executor.

What these tests pin
--------------------
 1. an unjudged, environment-blocked round ends as ``loop_stopped``:
    ``should_stop`` is true, there are no repair tasks, and the exit says
    which kind of round it was (``environment_blocked``);
 2. the plan is not recorded as a verification failure, and it is not put
    into the repair phase -- there is nothing to repair;
 3. the rounds that already say the truth keep saying it: an all-PASSED
    round still passes, and a real failure still produces repair tasks;
 4. no new ``stop_reason`` literal is introduced.  The state machine's
    reason vocabulary is closed -- the AST guard in
    ``tests/unit/test_verification_terminal_state_machine.py`` reads this
    function's literals and refuses any reason it cannot classify as
    chain-ending -- so this stop carries the verdict, not a new word.

The interface contract
-----------------------
``check_cycle_conditions(report)`` gains one more exit::

    {"status": "loop_stopped", "should_stop": True, "stop_reason": None,
     "repair_tasks": [], "environment_blocked": True}

``stop_reason`` stays ``None``: the round's *status* is the verdict, and
a new reason string would have to enter the closed vocabulary.

Why the cases carry a marker
----------------------------
The reproduction has to fail on the base point -- that failure is the
evidence this file exists for -- but a test command that exits non-zero is
a **failed task**, and a failed task blocks every task that depends on it
(the implementation of this contract is the next task).  The marker is
therefore a ratchet rather than a waiver.  It applies only while the
implementation has not landed at all -- and it switches itself off the
moment it has (see ``_implementation_is_missing``).  An implementation that
lands with the wrong value therefore fails these tests outright instead of
being absorbed by the marker.

To read the raw red on the base point, run the module with the markers
switched off::

    pytest tests/unit/test_orchestrator_environment_blocked_round.py --runxfail -v

Pure rule tests: no model, no subprocess -- the only side effect is
pytest's own ``tmp_path``.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest


# Ensure backend/ is on sys.path so ``import verification.orchestrator``
# works regardless of the test runner's entry point.
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from plan_state import PlanState  # noqa: E402
from verification.orchestrator import VerificationOrchestrator  # noqa: E402


#: The reason code the data-plane block carries.  Spelled out here so the
#: test pins the code itself rather than whatever the constant holds.
DATA_PLANE_MISMATCH_CODE = "service_data_plane_mismatch"

#: The string ``stop_reason`` literals ``check_cycle_conditions`` is
#: allowed to return.  A new one has to be added to the state machine's
#: closed vocabulary (``server.VERIFICATION_TERMINAL_STOP_REASONS``) --
#: and the AST guard in ``tests/unit/test_verification_terminal_state_machine.py``
#: reads this function's literals to prove that every reason it can
#: return is classified as chain-ending.  The environment-blocked stop
#: carries no new word: its verdict is the ``status``.
#:
#: The set is the **whole** closed vocabulary, not the two literals this
#: function happens to return today.  Its source is
#: ``plan_state.PlanState.stop_verification_loop`` -- the one place that
#: decides which reasons the state machine accepts (``valid_reasons``).
#: Pinning the full vocabulary here means the guard below asks the right
#: question ("did a reason appear that the state machine does not know?")
#: instead of the wrong one ("did a reason appear that this file did not
#: happen to list when it was written?").  A partial list turns every
#: pre-existing reason into a false positive -- ``repair_execution_failed``
#: has been returned by ``check_cycle_conditions`` since long before this
#: file existed (``verification/orchestrator.py``), and it is a member of
#: the state machine's vocabulary (``plan_state.py`` lists it in
#: ``valid_reasons``), so it is not a new word.
KNOWN_STOP_REASON_LITERALS = {
    # plan_state.PlanState.stop_verification_loop -> valid_reasons
    "max_rounds_reached",
    "user_stopped",
    "same_failure_repeated",
    "same_failure_repeated_after_max_attempts",
    "repair_execution_failed",
    "exception",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_plan_dir(tmp_path):
    plan_dir = tmp_path / "plans" / "test-plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    project_dir = tmp_path / "projects" / "test-project"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


@pytest.fixture
def mock_coding_tool():
    return Mock()


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Point plan-state persistence at a hermetic per-test SQLite file.

    ``PlanState`` keeps the plan's state in the ``plan_routing`` row of
    ``state.db``.  Without an override the orchestrator would write its
    ``test-plan`` row into the repo's real ``state.db`` and read back
    whatever an earlier test left behind.  Autouse, so no test in this
    module can leak into -- or inherit from -- another.
    """
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_plan_state(plan_dir, phase="executing", verification_round=0, max_rounds=3,
                      stop_reason=None, status="pending"):
    """Helper to bootstrap plan_state.json for a test."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"] if phase not in ("interview",) else [],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": status,
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": stop_reason,
        },
    }
    (plan_dir / "plan_state.json").write_text(json.dumps(state), encoding="utf-8")


def _environment_blocked_report():
    """A round the environment took two points out of.

    ``VP-1`` was blocked by the environment (it carries the reason code
    the environment owns); ``VP-2`` was deferred.  Neither was judged, so
    the round's verdict is ``SKIPPED`` -- and the accounting layer flagged
    the round itself.
    """
    return {
        "overall_status": "SKIPPED",
        "failed_count": 0,
        "environment_blocked": True,
        "verification_results": [
            {
                "id": "VP-1",
                "status": "BLOCKED",
                "evidence": {"reason_code": DATA_PLANE_MISMATCH_CODE},
            },
            {"id": "VP-2", "status": "DEFERRED"},
        ],
    }


def _stop_reason_literals() -> set:
    """The string ``stop_reason`` literals ``check_cycle_conditions`` returns.

    Read off the function's own source, the way the state machine's
    vocabulary guard reads it: a rename (or a new reason) is only visible
    here, and it is visible the day it lands.
    """
    source = (_BACKEND_DIR / "verification" / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "check_cycle_conditions"
    )
    literals = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "stop_reason"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                literals.add(value.value)
    return literals


def _implementation_is_missing() -> bool:
    """Has the environment-blocked round handling landed yet?

    The structural probe: the round's contract says the exit carries an
    ``environment_blocked`` field, so an implementation has to spell that
    field out in the orchestrator module.  While the orchestrator has
    never heard of the flag, the handling has not landed.

    The probe is structural on purpose.  A behavioural probe would ask
    the shipped code whether it already stops the round -- and a
    half-landed implementation (the branch is there, the verdict is
    wrong) would answer "no", the marker would stay on, and the broken
    implementation would be absorbed by it.  A structural probe flips the
    marker off as soon as the handling is *there*, so a wrong value
    fails these tests outright.
    """
    source = (_BACKEND_DIR / "verification" / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    return "environment_blocked" not in source


#: The marker the reproduction carries.  See the module docstring: it is
#: a ratchet, not a waiver -- it switches itself off the moment the
#: implementation lands.
_ROUND_STOP_NOT_LANDED = pytest.mark.xfail(
    strict=True,
    reason=(
        "the environment-blocked round handling has not landed yet: an "
        "unjudged round still falls into the failure path and dispatches "
        "repair tasks for a defect nobody found"
    ),
    condition=_implementation_is_missing(),
)


# ---------------------------------------------------------------------------
# The reproduction
# ---------------------------------------------------------------------------


@_ROUND_STOP_NOT_LANDED
def test_an_unjudged_round_stops_as_environment_blocked(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """A round the environment took points out of ends as ``loop_stopped``.

    Nothing was judged, so nothing failed, so there is nothing to repair.
    The round says so instead of handing the executor a repair task for a
    product nobody found a defect in.
    """
    _write_plan_state(temp_plan_dir, phase="executing")
    report = _environment_blocked_report()

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = VerificationOrchestrator(
            temp_plan_dir, temp_project_dir, mock_coding_tool,
        )
        orch.start_verification_cycle(round_number=1)
        result = orch.check_cycle_conditions(report)

    assert result["status"] == "loop_stopped", (
        f"一轮没判满的轮次必须以环境阻断收尾，实际是 {result!r}"
    )
    assert result["should_stop"] is True, (
        f"环境阻断的轮次必须停链，实际是 {result!r}"
    )
    assert result["stop_reason"] is None, (
        f"这一停的理由由 status 承担，不得是新字面量，实际是 {result!r}"
    )
    assert result["repair_tasks"] == [], (
        f"没被判过的产品不得派修复任务，实际是 {result!r}"
    )
    assert result["environment_blocked"] is True, (
        f"出口必须写明这一轮是被环境阻断的，实际是 {result!r}"
    )

    ps = PlanState(temp_plan_dir)
    assert ps.get_current_phase() != "verification_failed", (
        f"没判满的一轮不得被记成验证失败，实际相位是 {ps.get_current_phase()!r}"
    )
    assert ps.get_current_phase() != "verification_repairing", (
        f"没判满的一轮不得把计划推进修复相位，实际相位是 {ps.get_current_phase()!r}"
    )
    assert ps.get_verification_status() != "failed", (
        f"没判满的一轮不得被记成一次失败的验证，实际状态是 "
        f"{ps.get_verification_status()!r}"
    )


# ---------------------------------------------------------------------------
# The rounds that already say the truth
# ---------------------------------------------------------------------------


def test_a_passed_round_still_passes(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """Every point passed: the round passes, and nothing is repaired."""
    _write_plan_state(temp_plan_dir, phase="executing")
    report = {
        "overall_status": "PASSED",
        "verification_results": [{"id": "VP-1", "status": "PASSED"}],
    }

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = VerificationOrchestrator(
            temp_plan_dir, temp_project_dir, mock_coding_tool,
        )
        orch.start_verification_cycle(round_number=1)
        result = orch.check_cycle_conditions(report)

    assert result["status"] == "passed", (
        f"全 PASSED 的一轮必须照旧判过，实际是 {result!r}"
    )
    assert result["repair_tasks"] == [], (
        f"判过的一轮不得派修复任务，实际是 {result!r}"
    )


def test_a_failed_round_still_generates_repair_tasks(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """A real failure is still a failure: it still produces a repair task.

    The negative control for the whole file: the environment-blocked exit
    must not swallow the rounds that really did fail.
    """
    _write_plan_state(temp_plan_dir, phase="executing")
    report = {
        "overall_status": "FAILED",
        "verification_results": [{"id": "VP-9", "status": "FAILED"}],
    }

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"), \
         patch("verification.verification_report_reader.extract_failed_vps_with_paths") as mock_extract, \
         patch("repair_generator.RepairTaskAssembler") as mock_assembler:
        mock_extract.return_value = [
            {"id": "VP-9", "title": "fix the thing", "priority": "high"},
        ]
        mock_assembler.return_value.assemble.return_value = [
            {"id": "RP-1", "title": "fix the thing", "failed_vp_id": "VP-9"},
        ]
        orch = VerificationOrchestrator(
            temp_plan_dir, temp_project_dir, mock_coding_tool,
        )
        orch.repair_generator.generate_repair_contents.return_value = [
            {"failed_vp_id": "VP-9", "title": "fix the thing"},
        ]
        orch.start_verification_cycle(round_number=1)
        result = orch.check_cycle_conditions(report)

    assert result["status"] == "verification_failed", (
        f"真的失败了就得留在失败通道里，实际是 {result!r}"
    )
    assert result["repair_tasks"], (
        f"真的失败的点必须拿到修复任务，实际是 {result!r}"
    )


def test_no_new_stop_reason_literal_is_introduced(
    temp_plan_dir, temp_project_dir, mock_coding_tool,
):
    """The reason vocabulary is closed -- this stop carries no new word.

    The state machine classifies a stop by the reason string it reads
    off the function's own literals (see
    ``tests/unit/test_verification_terminal_state_machine.py``).  A new
    literal would have to enter that vocabulary -- so the environment
    blocked stop says what happened in the ``status`` and leaves
    ``stop_reason`` alone.
    """
    literals = _stop_reason_literals()
    assert literals, (
        "no string ``stop_reason`` literal found in check_cycle_conditions "
        "— this guard would otherwise pass vacuously"
    )
    assert literals <= KNOWN_STOP_REASON_LITERALS, (
        f"check_cycle_conditions gained stop_reason literal(s) "
        f"{sorted(literals - KNOWN_STOP_REASON_LITERALS)} — the state "
        f"machine's reason vocabulary is closed"
    )

    _write_plan_state(temp_plan_dir, phase="executing")
    report = _environment_blocked_report()

    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator"):
        orch = VerificationOrchestrator(
            temp_plan_dir, temp_project_dir, mock_coding_tool,
        )
        orch.start_verification_cycle(round_number=1)
        result = orch.check_cycle_conditions(report)

    assert result["stop_reason"] is None, (
        f"环境阻断的这一停不得带新理由字面量，实际是 {result!r}"
    )
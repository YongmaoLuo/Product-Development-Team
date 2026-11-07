"""A gate that ran out of its budget did not prove the code is broken.

Why this file exists
--------------------
``run_ci_verification`` graded a ``full_ci`` gate on one bit: exit 0 is
PASSED, everything else is FAILED.  But the gate has three exit codes
(``scripts/run_tests.sh``): 0 — the gate passed, 1 — a phase failed,
and 3 — the run could not decide (the budget ran out, or the host could
not be measured).

Exit 3 says nothing about the code.  Reporting it as FAILED put "the
machine was too slow" into the same bucket as "the code is broken", and
the repair loop turns every FAILED verdict into a fix task — so a slow
host generated work for a deliverable nobody had proven defective.

What is pinned here
-------------------
* a gate that ran out of its own budget is INCONCLUSIVE, not FAILED;
* exit 3 with no parsable phase is INCONCLUSIVE too — the gate's own
  verdict line stands on its own;
* exit 1 is still FAILED — a phase that failed is a code failure;
* a watchdog timeout is still FAILED — the gate was killed from the
  outside, which is a different thing from "the gate stopped itself";
* the evidence carries the phase that ran out of budget and how long
  the gate said it had been running.

And, since the gate learned to measure its host before it judged a
phase, the runner has to hear that measurement too:

* the summary carries the host's own account of itself
  (``host_measurable``: True / False / None — a run that never
  measured is not a run that found the host untrustworthy);
* a gate that says "the host cannot measure wall-clock" is a gate that
  could not decide — **even when its exit code is 1**;
* the evidence carries the host fact, so an operator reading the
  artifact can see *why* this machine's verdict is worth less;
* **the gate's exit code is not the only witness**: the runner re-reads
  the failures the gate already wrote down, and a failure that is not a
  wall-clock shape is the code's — so it stays FAILED even when the
  gate said INCONCLUSIVE;
* the classification layer still says what it always said:
  ``gate_inconclusive`` is an infrastructure problem, ``gate_failed``
  is a deliverable defect.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification.failure_triage import classify_deterministic  # noqa: E402
from verification_ci_runner import (  # noqa: E402
    STATUS_INCONCLUSIVE,
    parse_gate_summary,
    run_ci_verification,
)

#: A gate that ran out of its own budget, in the real gate's own words.
#: The three summary lines it prints (``gate budget`` / ``gate elapsed``
#: / ``gate budget exhausted in``) are what the runner reads back.
GATE_OUT_OF_BUDGET = """
echo '=== phase 1/3 (parallel) ==='
echo '=== gate summary ==='
echo 'phase 1/3 (parallel):             INCONCLUSIVE (the gate budget was exhausted)'
echo 'host measurability:               OK (sleep(0.05)=0.051s, loadavg=3.10)'
echo 'gate budget: 7200s'
echo 'gate elapsed: 7200s'
echo 'gate budget exhausted in: 1/3 (parallel)'
echo 'gate: INCONCLUSIVE (exit 3)'
exit 3
"""

#: A gate that could not decide *without* naming a phase — the host was
#: not measurable, so the real gate prints no "budget exhausted in" line.
GATE_INCONCLUSIVE_WITHOUT_PHASE = """
echo '=== gate summary ==='
echo 'gate budget: 7200s'
echo 'gate elapsed: 7200s'
echo 'gate: INCONCLUSIVE (exit 3)'
exit 3
"""


def _gate(tmp_path: Path, body: str) -> Path:
    """A gate script that runs *body*."""
    gate = tmp_path / "gate.sh"
    gate.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    gate.chmod(0o755)
    return gate


def _vp(entry: str, **extra) -> Dict:
    vp = {
        "id": "VP-017",
        "verification_method": "full_ci",
        "verification_phase": 2,
        "ci_entry": entry,
    }
    vp.update(extra)
    return vp


# --------------------------------------------------------------------------
# A gate that stopped itself is not a code failure
# --------------------------------------------------------------------------


def test_a_gate_that_ran_out_of_its_budget_is_inconclusive(tmp_path):
    """The gate stopped at its own budget — that is not a code failure.

    The gate declares its own budget and stops at it; the machine was too
    slow, the code was never given a chance to be judged.  Reporting
    that as FAILED is what sends the repair loop after a deliverable
    nobody has proven defective.
    """
    gate = _gate(tmp_path, GATE_OUT_OF_BUDGET)

    result = run_ci_verification(
        _vp(f"bash {gate}", ci_timeout_seconds=60), project_dir=tmp_path,
    )

    assert result.status == STATUS_INCONCLUSIVE, (
        f"a gate that ran out of its own budget is not a code failure:\n"
        f"{result.reasons}"
    )
    assert result.to_verdict_dict()["status"] == "INCONCLUSIVE"


def test_an_exit_three_without_a_parsable_phase_is_still_inconclusive(tmp_path):
    """The gate's own verdict line stands on its own.

    The real gate prints no "budget exhausted in" line when the host
    could not be measured — but it still says ``gate: INCONCLUSIVE
    (exit 3)``, and that alone is enough to refuse to call the run a
    failure.
    """
    gate = _gate(tmp_path, GATE_INCONCLUSIVE_WITHOUT_PHASE)

    result = run_ci_verification(
        _vp(f"bash {gate}", ci_timeout_seconds=60), project_dir=tmp_path,
    )

    assert result.status == STATUS_INCONCLUSIVE, (
        f"exit 3 with no parsable phase is still inconclusive:\n"
        f"{result.reasons}"
    )
    assert result.evidence["budget_exhausted_in"] == ""


# --------------------------------------------------------------------------
# Everything else keeps its verdict
# --------------------------------------------------------------------------


def test_an_exit_one_is_still_failed(tmp_path):
    """A phase that failed is a code failure, and stays one."""
    gate = _gate(tmp_path, "echo 'a phase failed'\nexit 1\n")

    result = run_ci_verification(
        _vp(f"bash {gate}", ci_timeout_seconds=60), project_dir=tmp_path,
    )

    assert result.status == "FAILED", (
        f"exit 1 is a code failure:\n{result.reasons}"
    )


def test_a_watchdog_timeout_is_still_failed(tmp_path):
    """The gate was killed from the outside — a different thing entirely.

    A watchdog that fires says "something went wrong between the gate
    and the watchdog".  It is not the gate's own "I ran out of budget",
    and it keeps its FAILED verdict.
    """
    result = run_ci_verification(
        _vp("sleep 30", ci_timeout_seconds=2), project_dir=tmp_path,
    )

    assert result.status == "FAILED", (
        f"a watchdog timeout is still a failure:\n{result.reasons}"
    )
    assert result.evidence["timed_out"] is True


def test_an_exit_three_that_said_nothing_is_still_failed(tmp_path):
    """A run with no summary at all declared nothing.

    The gate's own summary is the evidence that a run could not decide
    — it is where the gate says which phase ran out of budget, or that
    the host could not be measured.  An entry that exits 3 without
    printing a summary has said nothing, so there is no declaration to
    read and the historical FAILED verdict stands.
    """
    result = run_ci_verification(
        _vp("exit 3", ci_timeout_seconds=30), project_dir=tmp_path,
    )

    assert result.status == "FAILED", (
        f"an entry that declared nothing is not an inconclusive gate:\n"
        f"{result.reasons}"
    )


# --------------------------------------------------------------------------
# The evidence carries what the gate said about itself
# --------------------------------------------------------------------------


def test_the_evidence_carries_the_exhausted_phase_and_elapsed(tmp_path):
    """Which phase ran out of budget, and how long the gate had been running.

    Both are the gate's own account of itself.  Without them the verdict
    says only "something went wrong", and the reader has to go dig for
    the summary that was there all along.
    """
    gate = _gate(tmp_path, GATE_OUT_OF_BUDGET)

    result = run_ci_verification(
        _vp(f"bash {gate}", ci_timeout_seconds=60), project_dir=tmp_path,
    )

    assert result.evidence["budget_exhausted_in"] == "1/3 (parallel)"
    assert result.evidence["gate_elapsed_seconds"] == 7200

    reason = " ".join(result.reasons)
    assert "1/3 (parallel)" in reason, (
        f"the reason does not name the phase that ran out of budget:\n"
        f"{reason}"
    )
    assert "7200" in reason, (
        f"the reason does not say how long the gate had been running:\n"
        f"{reason}"
    )


# --------------------------------------------------------------------------
# The host's own account of itself
# --------------------------------------------------------------------------

#: The gate's host-measurability note, in the two forms it writes.
HOST_UNMEASURABLE_NOTE = (
    "the host cannot measure wall-clock (sleep(0.05)=1.20s, loadavg=31.00)"
)
HOST_MEASURABLE_NOTE = "OK (sleep(0.05)=0.051s, loadavg=3.10)"

#: A failure that only says the machine was too slow to answer.
WALL_CLOCK_MESSAGE = "service was not ready after 90.0s"

#: A failure that says something about the code, not about the clock.
DETERMINISTIC_MESSAGE = "AssertionError: 1 + 1 == 3"

#: A gate that says the host cannot be measured, and exits 1 — the shape
#: of the failure this round was actually made of.
GATE_HOST_UNMEASURABLE_EXIT_ONE = """
echo '=== phase 1/3 (parallel) ==='
echo '=== gate summary ==='
echo 'phase 1/3 (parallel):             FAILED (exit 1)'
echo 'host measurability:               the host cannot measure wall-clock (sleep(0.05)=1.20s, loadavg=31.00)'
echo 'gate budget: 7200s'
echo 'gate elapsed: 300s'
echo 'gate: FAILED (exit 1)'
exit 1
"""


def test_the_summary_carries_the_host_fact():
    """The gate's own account of the host it ran on.

    Three-valued, because the three states are three different things:
    the host could be measured, the host could not be measured, and
    the gate never looked.  Only the middle one makes the machine's
    verdict worth less — a run that never measured has not declared the
    host untrustworthy.
    """
    measurable = parse_gate_summary(
        "host measurability: OK (sleep(0.05)=0.051s, loadavg=3.10)\n"
    )
    assert measurable["host_measurable"] is True, (
        f"a measurable host was not read as measurable: {measurable}"
    )

    unmeasurable = parse_gate_summary(
        "host measurability: the host cannot measure wall-clock "
        "(sleep(0.05)=1.20s, loadavg=31.00)\n"
    )
    assert unmeasurable["host_measurable"] is False, (
        f"an unmeasurable host was not read as unmeasurable: {unmeasurable}"
    )

    never_measured = parse_gate_summary(
        "host measurability: not measured (no phase was selected)\n"
    )
    assert never_measured["host_measurable"] is None, (
        f"a host that was never measured was read as unmeasurable: "
        f"{never_measured}"
    )

    silent = parse_gate_summary("gate: FAILED (exit 1)\n")
    assert silent["host_measurable"] is None, (
        f"a gate that said nothing about the host was read as declaring "
        f"it untrustworthy: {silent}"
    )


def test_a_gate_that_says_the_host_cannot_be_measured_is_inconclusive(
    tmp_path,
):
    """The machine said it could not answer — that is not a code failure.

    The gate's exit code is 1 here, and on its own that would be a
    failure.  But the gate also said, in its own summary, that the host
    could not measure wall-clock time at all — a declaration about the
    machine, made by the only party that could have measured it.  The
    runner has to hear it even though the exit code says otherwise.
    """
    gate = _gate(tmp_path, GATE_HOST_UNMEASURABLE_EXIT_ONE)

    result = run_ci_verification(
        _vp(f"bash {gate}", ci_timeout_seconds=60), project_dir=tmp_path,
    )

    assert result.status == STATUS_INCONCLUSIVE, (
        f"a gate that said the host cannot be measured was graded as a "
        f"code failure:\n{result.reasons}"
    )
    assert result.evidence["host_measurable"] is False, (
        f"the evidence does not carry the host's own account of itself:\n"
        f"{result.evidence}"
    )


def _gate_with_junit(tmp_path: Path, *, message: str, exit_code: str) -> Path:
    """A gate that writes one phase's junit report and exits.

    The report is what the runner re-reads: the gate's exit code is not
    allowed to be the only witness.
    """
    body = (
        "mkdir -p \"$PDT_GATE_JUNIT_DIR\"\n"
        "cat > \"$PDT_GATE_JUNIT_DIR/phase-1.xml\" <<'XML'\n"
        '<testsuites><testsuite name="pytest" tests="1" failures="1" '
        'errors="0" skipped="0">'
        '<testcase classname="tests.unit.test_widget" name="test_widget">'
        f'<failure message="{message}">trace</failure>'
        "</testcase></testsuite></testsuites>\n"
        "XML\n"
        "echo '=== gate summary ==='\n"
        "echo 'phase 1/3 (parallel):             INCONCLUSIVE (exit "
        f"{exit_code})'\n"
        "echo 'host measurability:               "
        f"{HOST_UNMEASURABLE_NOTE}'\n"
        "echo 'gate budget: 7200s'\n"
        "echo 'gate elapsed: 300s'\n"
        f"echo 'gate: INCONCLUSIVE (exit {exit_code})'\n"
        f"exit {exit_code}\n"
    )
    return _gate(tmp_path, body)


def test_a_wall_clock_failure_stays_inconclusive(tmp_path):
    """Every failure says the machine ran out of time — so it was the machine."""
    gate = _gate_with_junit(
        tmp_path, message=WALL_CLOCK_MESSAGE, exit_code="3",
    )

    result = run_ci_verification(
        _vp(f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=tmp_path / "artifacts",
    )

    assert result.status == STATUS_INCONCLUSIVE, (
        f"a wall-clock failure was graded as a code failure:\n"
        f"{result.reasons}"
    )


def test_the_negative_control_keeps_a_deterministic_failure_failed(tmp_path):
    """The gate's exit code is not the only witness.

    The gate said INCONCLUSIVE and exited 3, but the failure it wrote
    down is an assertion about behaviour — not about the clock.  The
    machine being busy does not explain ``1 + 1 == 3``, so the verdict
    is FAILED and the reason names the case that says so.
    """
    gate = _gate_with_junit(
        tmp_path, message=DETERMINISTIC_MESSAGE, exit_code="3",
    )

    result = run_ci_verification(
        _vp(f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=tmp_path / "artifacts",
    )

    assert result.status == "FAILED", (
        f"a deterministic failure was excused by the host:\n{result.reasons}"
    )
    reason = " ".join(result.reasons)
    assert "test_widget" in reason, (
        f"the reason does not name the failing case:\n{reason}"
    )
    assert DETERMINISTIC_MESSAGE in reason, (
        f"the reason does not carry the failing message:\n{reason}"
    )


def test_the_classification_layer_still_says_what_it_always_said():
    """``gate_inconclusive`` is the machine's problem, not the code's.

    The classification layer is not what broke, and it is not what this
    change touches — but it is what turns the runner's verdict into a
    repair task, so it is worth pinning: an INCONCLUSIVE gate is an
    infrastructure problem, and a FAILED gate is a deliverable defect.
    """
    assert classify_deterministic("gate_inconclusive", "") == "infra", (
        "an inconclusive gate is no longer classified as infrastructure"
    )
    assert classify_deterministic("gate_failed", "") == "deliverable_defect", (
        "a failed gate is no longer classified as a deliverable defect"
    )

# --------------------------------------------------------------------------
# Not in this file: the repair chain's delivery channel
# --------------------------------------------------------------------------
#
# The diagnosis task hands the executor a **report file**, and for a
# long time the executor did not know that: the task said ``deliverable``
# was a *checklist* (a list of five Chinese strings), while the executor's
# model expects a type discriminant (``"code"`` / ``"report"``).  The
# report sat on disk and nobody looked for it.
#
# Fixing that channel needs three things at once:
#
#   * ``backend/orphan_rules.py`` — the marker that carries the contract
#     (``report_deliverable_marker`` / ``parse_report_deliverable``) and
#     the merge that turns a stored row back into a ``SubTask``;
#   * ``backend/state_machine/db/schema.py`` and
#     ``backend/state_machine/repositories/plan_task_repository.py`` — the
#     two fields have to survive the trip through the state database,
#     which narrows the payload to the fields the table can hold;
#   * ``backend/verification/orchestrator.py`` — ``report_path`` has to be
#     the absolute path the orchestrator itself reads the report from.
#
# 2026-11-06: the root-cause task builder that used to own this channel
# is deleted together with the chain — so the contract is **generic**
# now: it travels in the task's own description (the marker line) and is
# recovered from there by the orphan merge.  ``tests/unit/
# test_report_deliverable.py`` pins that channel.
#
# The "L6" layer of the root-cause report is still **not** implemented
# here: it cannot be.  Two of the files above are frozen surfaces
# (``backend/state_machine/**`` is byte-frozen by
# ``backend/tests/static_gates/test_verification_mode_scope_gates.py``).
# The report itself allows this layer to be split into its own round
# ("L6 与 L1~L5 没有共享文件"), and the plan gives that round to a later
# task that owns all of those files.  This file therefore pins only the
# layers it can pin.
